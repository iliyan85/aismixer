"""MP1 CLIENT LIVENESS RECOVERY -- acceptance tests.

Each test encodes one requirement from UDPSEC_V2_RECOVERY_BASELINE.md
(section 7.1, "MP1 -- client liveness recovery") against the real
client/server path in the MP0 lab. MP0 wrote MP1-L1..L8 before MP1 as
strict xfails, each failing for exactly one documented reason: the
pre-MP1 single-shot keepalive policy replaced the logical session. MP1
removed every marker without changing an assertion; the docstrings keep
the pre-MP1 outcome each test used to fail on. MP1 added L4d, L7b, L8b,
L11a, L11b and L12 (scenarios A5e, A9b, A10b, A11, A11b, A7).

Preconditions use `lab.require(...)` (HarnessError), so a scenario whose
fault was never injected cannot pass silently. Assertions state outcomes
only; they do not choose MP1 state names, retry counts or timing
constants -- the exact MP1 cadence is pinned separately in
test_mp1_policy_pins.py. MP1-L9 (long outages still terminate) and MP1-L10
(no wire change is needed) are guards in test_recovery_invariants.py.
"""

import math

import pytest

from .harness import expect
from .harness.lab import ADDR_B, STILL_RUNNING
from .harness.scenarios import run


def test_mp1_l1_lost_ping_keeps_the_logical_session(monkeypatch):
    """MP1-L1 [A2/T02]: pre-MP1, one lost PING ended the session with
    proactive_rekey at ping#2 + keepalive_interval (1090.2) and a fresh
    ECDHE session replaced it. MP1 regains liveness inside the same
    LogicalSession."""
    lab = run("A2", monkeypatch)
    ping2 = lab.first_sent("c2s", "ping#2")
    lab.require(ping2.verdict == "DROP:hook", "ping#2 was not dropped")
    expect.require_window_proves_regained_liveness(lab, ping2.t)
    expect.assert_logical_session_survived(lab)
    expect.assert_delivered_promptly(lab, lab.input.produced)


def test_mp1_l2_lost_pong_keeps_the_logical_session(monkeypatch):
    """MP1-L2 [A3/T03]: pre-MP1, one lost PONG ended the session with
    proactive_rekey at 1090.2 although the server received the PING and
    still held the session. MP1 keeps the LogicalSession."""
    lab = run("A3", monkeypatch)
    pong2 = lab.first_sent("s2c", "pong#2")
    lab.require(pong2.verdict == "DROP:hook", "pong#2 was not dropped")
    expect.require_window_proves_regained_liveness(lab, pong2.t)
    expect.assert_logical_session_survived(lab)
    expect.assert_delivered_promptly(lab, lab.input.produced)


def test_mp1_l3_short_two_way_blackhole_keeps_the_logical_session(monkeypatch):
    """MP1-L3 [A4/T04]: pre-MP1, a 5 s two-way blackhole (1058-1063) around
    one keepalive exchange forced proactive_rekey at 1090.2, 27 s after the
    path recovered. MP1 keeps the LogicalSession."""
    lab = run("A4", monkeypatch)
    ping2 = lab.first_sent("c2s", "ping#2")
    lab.require(ping2.verdict == "DROP:blackhole", "ping#2 was not inside the blackhole")
    expect.require_window_proves_regained_liveness(lab, 1058.0)
    expect.assert_logical_session_survived(lab)
    outside = [line for line in lab.input.produced if not 1058.0 <= line[1] < 1063.0]
    expect.assert_delivered_promptly(lab, outside)


def test_mp1_l4a_pong_readable_at_the_keepalive_deadline_is_processed(monkeypatch):
    """MP1-L4a [A5b/T05b]: pre-MP1, a matching PONG readable exactly at the
    keepalive deadline was never read -- the deadline was classified first
    and proactive_rekey won. MP1 processes already-readable authenticated
    evidence before any verdict and keeps the LogicalSession."""
    lab = run("A5b", monkeypatch)
    pong2 = lab.first_sent("s2c", "pong#2")
    lab.require(
        pong2.deliver_at == expect.keepalive_deadline(lab, 2),
        "pong#2 was not delivered exactly at the keepalive deadline",
    )
    expect.require_window_proves_regained_liveness(lab, pong2.t)
    expect.assert_logical_session_survived(lab)
    assert pong2.consumed_by == "forward_loop"


def test_mp1_l4b_pong_readable_at_the_peer_timeout_boundary_is_processed_first(monkeypatch):
    """MP1-L4b [A5d]: pre-MP1, a matching PONG readable exactly at the
    peer_timeout boundary lost to the peer_timeout verdict and was
    discarded by the next handshake. MP1 processes already-readable
    authenticated evidence before a terminal verdict at that same instant."""
    lab = run("A5d", monkeypatch)
    pong1 = lab.first_sent("s2c", "pong#1")
    boundary = lab.sessions[0].confirmed_at + float(lab.config["peer_timeout"])
    lab.require(pong1.deliver_at == boundary, "pong#1 was not delivered at the peer_timeout boundary")
    expect.require_window_proves_regained_liveness(lab, pong1.t)
    expect.assert_logical_session_survived(lab)
    assert pong1.consumed_by == "forward_loop"
    assert pong1.consumed_at == boundary


def test_mp1_l4c_pong_just_after_the_keepalive_deadline_keeps_the_session(monkeypatch):
    """MP1-L4c [A5c/T05c]: pre-MP1, a matching PONG arriving 10 ms after
    the keepalive deadline was too late -- proactive_rekey had already
    fired. MP1 does not treat one missed keepalive deadline as terminal."""
    lab = run("A5c", monkeypatch)
    pong2 = lab.first_sent("s2c", "pong#2")
    lab.require(
        pong2.deliver_at == pytest.approx(expect.keepalive_deadline(lab, 2) + 0.01),
        "pong#2 was not delivered 10 ms after the keepalive deadline",
    )
    expect.require_window_proves_regained_liveness(lab, pong2.t)
    expect.assert_logical_session_survived(lab)


def test_mp1_l4d_pong_readable_after_the_peer_timeout_boundary_stays_late(monkeypatch):
    """MP1-L4d [A5e] (R4 item 4): evidence before verdict admits only what
    is already readable when the terminal verdict falls due. A matching
    PONG that becomes readable 10 ms after the peer_timeout boundary has no
    effect on that verdict: the session ends exactly at the boundary, the
    PONG is left to the next handshake (which skips it as old-session
    DATA), and a fresh session follows."""
    lab = run("A5e", monkeypatch)
    first = lab.sessions[0]
    pong1 = lab.first_sent("s2c", "pong#1", locator=first.locator)
    boundary = first.confirmed_at + float(lab.config["peer_timeout"])
    lab.require(
        pong1.deliver_at == pytest.approx(boundary + 0.01),
        "pong#1 was not delivered 10 ms after the peer_timeout boundary",
    )
    assert first.ended_at == boundary
    assert pong1.consumed_by == "handshake"
    assert pong1.consumed_at > first.ended_at
    assert len(lab.sessions) == 2
    assert lab.sessions[1].reason == STILL_RUNNING


def test_mp1_l5_bounded_client_stall_with_buffered_evidence_keeps_the_session(monkeypatch):
    """MP1-L5 [A6/T07]: pre-MP1, a 35 s client stall with the matching PONG
    already in the socket buffer ended the session with proactive_rekey on
    resume (1095.0). MP1 uses that buffered authenticated evidence and
    keeps the LogicalSession."""
    lab = run("A6", monkeypatch)
    pong2 = lab.first_sent("s2c", "pong#2")
    lab.require(
        any(source == "fault" and "descheduled" in text for _t, source, text in lab.events),
        "the client stall was never injected",
    )
    lab.require(pong2.deliver_at < 1095.0, "pong#2 was not buffered during the stall")
    expect.require_window_proves_regained_liveness(lab, 1060.26)
    expect.assert_logical_session_survived(lab)
    assert pong2.consumed_by == "forward_loop"


def test_mp1_l6_reverse_path_loss_does_not_stop_forwarding_at_the_first_miss(monkeypatch):
    """MP1-L6 [A7/T06a]: pre-MP1, a reverse-path-only loss ended the
    session at the first missed keepalive deadline (1090.2) although
    client->server delivery was healthy, and forwarding then waited ~70 s
    for a new session. MP1 keeps forwarding in the original session past
    one missed deadline and still ends it within peer_timeout of the last
    evidence."""
    lab = run("A7", monkeypatch)
    first = lab.sessions[0]
    first_miss = expect.keepalive_deadline(lab, 2)
    evidence = expect.last_evidence_before(lab, 1055.0)
    lab.require(evidence < 1055.0 < first_miss, "reverse-path loss did not start before ping#2")
    assert first.ended_at > first_miss, (
        f"{lab.name}: original session ended at {first.ended_at}, its first "
        "missed keepalive deadline"
    )
    assert first.ended_at <= evidence + float(lab.config["peer_timeout"])
    carried = [line for line in lab.input.produced if line[2] < first.ended_at]
    expect.assert_delivered_promptly(lab, carried)


def test_mp1_l7_refresh_evidence_and_a_lost_pong_keep_the_session(monkeypatch):
    """MP1-L7 [A9/T14]: pre-MP1, an in-session epoch refresh that
    committed 25 s before the deadline (authenticated REPLY/ACK liveness
    evidence) did not prevent proactive_rekey at 1090.2 after one lost
    PONG. MP1 failure accounting agrees with that evidence and keeps the
    LogicalSession."""
    lab = run("A9", monkeypatch)
    pong2 = lab.first_sent("s2c", "pong#2")
    lab.require(pong2.verdict == "DROP:hook", "pong#2 was not dropped")
    committed = lab.client_lines("Secure epoch refresh committed")
    lab.require(
        committed and committed[0][0] < expect.keepalive_deadline(lab, 2),
        "the refresh did not commit before the keepalive deadline",
    )
    expect.require_window_proves_regained_liveness(lab, pong2.t)
    expect.assert_logical_session_survived(lab)
    assert lab.original_server_session().current_epoch.generation >= 1


def test_mp1_l7b_refresh_only_evidence_keeps_the_session(monkeypatch):
    """MP1-L7b [A9b] (R5, "L7-strong" in baseline 7.3): with every PONG
    after pong#1 lost, planned in-session epoch refreshes are the only
    authenticated liveness evidence. A terminal verdict must not
    contradict them: the session outlives the last PONG by more than
    peer_timeout, carried by refresh evidence alone. (Pre-MP1: a
    re-handshake at every ping#2 deadline, three in the window.)"""
    lab = run("A9b", monkeypatch)
    pong1 = lab.first_sent("s2c", "pong#1")
    later = [p for p in lab.sent("s2c", "pong") if p.kind not in ("pong#0", "pong#1")]
    lab.require(later and all(p.verdict == "DROP:hook" for p in later), "later PONGs were not all dropped")
    commits = lab.client_lines("Secure epoch refresh committed")
    lab.require(len(commits) >= 3, "fewer than three refresh commits in the window")
    peer_timeout = float(lab.config["peer_timeout"])
    lab.require(
        lab.until > pong1.consumed_at + 2 * peer_timeout,
        "window too short to prove refresh evidence carried the session",
    )
    expect.assert_logical_session_survived(lab)
    expect.assert_delivered_promptly(lab, lab.input.produced)


def test_mp1_l8_ping_overtaking_path_response_keeps_the_migrated_session(monkeypatch):
    """MP1-L8 [A10/B5/T09d]: pre-MP1, a proof-matched PATH_ACK that
    captured no outstanding ping advanced liveness, but ping#2 -- which
    overtook the PATH_RESPONSE and drew no PONG from the unproved path --
    still forced proactive_rekey at 1090.2 and discarded the just-committed
    migration. MP1 keeps the migrated LogicalSession."""
    lab = run("A10", monkeypatch)
    ping2 = lab.first_sent("c2s", "ping#2")
    response = lab.first_sent("c2s", "path_response")
    lab.require(ping2.deliver_at < response.deliver_at, "ping#2 did not overtake the PATH_RESPONSE")
    lab.require(
        lab.client_lines("path migration acknowledged by peer"),
        "the migration was never acknowledged",
    )
    expect.require_window_proves_regained_liveness(lab, ping2.t)
    expect.assert_logical_session_survived(lab)
    session = lab.original_server_session()
    assert lab.secure.normalize_sockaddr(session.path_state.active_path) == (
        lab.secure.normalize_sockaddr(ADDR_B)
    )
    assert lab.state.stats().path_migrations_committed == 1


def test_mp1_l8b_path_ack_evidence_counts_even_when_it_cannot_resolve_the_ping(monkeypatch):
    """MP1-L8b [A10b] (R5, "L8-strong" in baseline 7.3): as A10, but every
    PONG from pong#2 on is lost, so the proof-matched PATH_ACK -- which
    captured no ping and can never resolve ping#2 -- is the last
    authenticated evidence. The session must not end before ACK +
    peer_timeout (a verdict at last PONG + peer_timeout would contradict
    the ACK), and it must still end by then (no immortal session)."""
    lab = run("A10b", monkeypatch)
    first = lab.sessions[0]
    ack = lab.first_sent("s2c", "path_ack", locator=first.locator)
    lab.require(ack.consumed_by == "forward_loop", "the PATH_ACK was never read by the session")
    lab.require(
        lab.client_lines("path migration acknowledged by peer"),
        "the migration was never acknowledged",
    )
    pong1 = lab.first_sent("s2c", "pong#1", locator=first.locator)
    peer_timeout = float(lab.config["peer_timeout"])
    lab.require(lab.until > ack.consumed_at + peer_timeout, "window ends before ACK + peer_timeout")
    assert first.ended_at > pong1.consumed_at + peer_timeout
    assert first.ended_at <= ack.consumed_at + peer_timeout
    assert lab.state.stats().path_migrations_committed == 1


def _nmea_marker(packet):
    payload = (packet.message or {}).get("payload", "")
    return payload.split(",")[5] if payload.count(",") >= 5 else None


def test_mp1_l11a_short_local_send_error_keeps_the_logical_session(monkeypatch):
    """MP1-L11a [A11]: pre-MP1, a 2.1 s local ENETUNREACH (1059.9-1062.0)
    ended the session with socket_error at the first failed send, and a
    new session followed after reconnect_delay. A transient local send
    error must stay inside the liveness bound: the same LogicalSession
    continues; only the sentence whose send failed is lost, and no
    sentence is ever sent twice (NMEA is not buffered or replayed)."""
    lab = run("A11", monkeypatch)
    failed = [p for p in lab.sent("c2s") if not p.transmitted]
    lab.require(
        failed and failed[0].kind == "nmea" and failed[0].t == 1060.0,
        "the first failed local send was not the 1060.0 NMEA sentence",
    )
    expect.require_window_proves_regained_liveness(lab, 1062.0)
    expect.assert_logical_session_survived(lab)
    lost = [marker for marker, _due, _read in lab.input.produced if lab.delivered_at(marker) is None]
    assert lost == [_nmea_marker(failed[0])]
    offered = [_nmea_marker(p) for p in lab.sent("c2s", "nmea")]
    assert len(offered) == len(set(offered)), "an NMEA sentence was sent twice"
    expect.assert_delivered_promptly(lab, expect.lines_due_between(lab, 1062.0, lab.until))


def test_mp1_l11b_transient_receive_error_keeps_the_logical_session(monkeypatch):
    """MP1-L11b [A11b]: an ICMP-derived receive error (Windows raises
    ConnectionResetError on the next recvfrom after a port-unreachable) is
    an unauthenticated transient signal. Pre-MP1 it ended the session with
    socket_error. It must neither end the LogicalSession nor count as
    liveness evidence either way."""
    lab = run("A11b", monkeypatch)
    reset = lab.first_sent("s2c", "icmp-reset")
    lab.require(reset.consumed_by == "forward_loop", "the receive error was never raised")
    expect.require_window_proves_regained_liveness(lab, reset.t)
    expect.assert_logical_session_survived(lab)
    expect.assert_delivered_promptly(lab, lab.input.produced)


def test_mp1_l12_retransmissions_reuse_the_seq_under_fresh_nonces(monkeypatch):
    """MP1-L12 [A7] (R1): an unanswered ping is recovered by retransmitting
    the SAME logical ping: every transmission carries the same `seq`,
    each is a fresh AEAD encryption with its own nonce, and their number
    is bounded by the time between the ping's first transmission and the
    terminal verdict (at most one per second -- far above any retry
    cadence; the exact cadence is pinned in test_mp1_policy_pins.py)."""
    lab = run("A7", monkeypatch)
    first = lab.sessions[0]
    transmissions = lab.sent("c2s", "ping#2", locator=first.locator)
    lab.require(transmissions, "ping#2 was never sent")
    assert len(transmissions) > 1, "the unanswered ping was never retransmitted"
    assert {p.message["seq"] for p in transmissions} == {2}
    assert not lab.sent("c2s", "ping#3", locator=first.locator)
    nonces = [p.nonce for p in transmissions]
    assert None not in nonces and len(set(nonces)) == len(nonces)
    assert len({p.raw for p in transmissions}) == len(transmissions)
    window = first.ended_at - transmissions[0].t
    assert len(transmissions) <= math.floor(window) + 1
    expect.assert_no_nonce_reuse(lab)
