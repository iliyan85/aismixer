"""MP1 CLIENT LIVENESS RECOVERY -- acceptance tests written before MP1.

Each test encodes one FUTURE requirement from UDPSEC_V2_RECOVERY_BASELINE.md
(section "Future MP1 contract") against the real client/server path in the
MP0 lab, and fails today for exactly one behavioural reason: the current
single-shot keepalive policy replaces the logical session. That reason is
pinned, scenario by scenario, in test_pre_mp1_policy_pins.py.

Every marker is ``xfail(strict=True, raises=AssertionError)``:

* strict -- an unexpected pass (XPASS) FAILS the run, so MP1 must remove
  the marker (and delete or invert the paired pin) deliberately;
* raises=AssertionError -- a harness fault (`HarnessError`) or any other
  exception is reported as a failure, never as the expected xfail.

Preconditions use `lab.require(...)` (HarnessError) so a scenario whose
fault was never injected cannot pass or xfail silently. Assertions state
outcomes only; they do not choose MP1 state names, retry counts or timing
constants. MP1-L9 (long outages still terminate) and MP1-L10 (no wire
change is needed) already hold and are ordinary tests in
test_recovery_invariants.py.
"""

import pytest

from .harness import expect
from .harness.lab import ADDR_B
from .harness.scenarios import run


def mp1(requirement):
    return pytest.mark.xfail(strict=True, raises=AssertionError, reason=requirement)


@mp1(
    "MP1-L1 [A2/T02]: pre-MP1, one lost PING ends the session with "
    "proactive_rekey at ping#2 + keepalive_interval (1090.2) and a fresh ECDHE "
    "session replaces it; MP1 must regain liveness inside the same LogicalSession"
)
def test_mp1_l1_lost_ping_keeps_the_logical_session(monkeypatch):
    lab = run("A2", monkeypatch)
    ping2 = lab.first_sent("c2s", "ping#2")
    lab.require(ping2.verdict == "DROP:hook", "ping#2 was not dropped")
    expect.require_window_proves_regained_liveness(lab, ping2.t)
    expect.assert_logical_session_survived(lab)
    expect.assert_delivered_promptly(lab, lab.input.produced)


@mp1(
    "MP1-L2 [A3/T03]: pre-MP1, one lost PONG ends the session with "
    "proactive_rekey at 1090.2 although the server received the PING and "
    "still holds the session; MP1 must keep the LogicalSession"
)
def test_mp1_l2_lost_pong_keeps_the_logical_session(monkeypatch):
    lab = run("A3", monkeypatch)
    pong2 = lab.first_sent("s2c", "pong#2")
    lab.require(pong2.verdict == "DROP:hook", "pong#2 was not dropped")
    expect.require_window_proves_regained_liveness(lab, pong2.t)
    expect.assert_logical_session_survived(lab)
    expect.assert_delivered_promptly(lab, lab.input.produced)


@mp1(
    "MP1-L3 [A4/T04]: pre-MP1, a 5 s two-way blackhole (1058-1063) around one "
    "keepalive exchange forces proactive_rekey at 1090.2, 27 s after the path "
    "recovered; MP1 must keep the LogicalSession"
)
def test_mp1_l3_short_two_way_blackhole_keeps_the_logical_session(monkeypatch):
    lab = run("A4", monkeypatch)
    ping2 = lab.first_sent("c2s", "ping#2")
    lab.require(ping2.verdict == "DROP:blackhole", "ping#2 was not inside the blackhole")
    expect.require_window_proves_regained_liveness(lab, 1058.0)
    expect.assert_logical_session_survived(lab)
    outside = [line for line in lab.input.produced if not 1058.0 <= line[1] < 1063.0]
    expect.assert_delivered_promptly(lab, outside)


@mp1(
    "MP1-L4a [A5b/T05b]: pre-MP1, a matching PONG that is readable exactly at "
    "the keepalive deadline is never read -- the deadline is classified first "
    "and proactive_rekey wins; MP1 must process already-readable authenticated "
    "evidence before any terminal verdict and keep the LogicalSession"
)
def test_mp1_l4a_pong_readable_at_the_keepalive_deadline_is_processed(monkeypatch):
    lab = run("A5b", monkeypatch)
    pong2 = lab.first_sent("s2c", "pong#2")
    lab.require(
        pong2.deliver_at == expect.keepalive_deadline(lab, 2),
        "pong#2 was not delivered exactly at the keepalive deadline",
    )
    expect.require_window_proves_regained_liveness(lab, pong2.t)
    expect.assert_logical_session_survived(lab)
    assert pong2.consumed_by == "forward_loop"


@mp1(
    "MP1-L4b [A5d]: pre-MP1, a matching PONG readable exactly at the "
    "peer_timeout boundary loses to the peer_timeout verdict and is discarded "
    "by the next handshake; MP1 must drain and process already-readable "
    "authenticated evidence before a terminal verdict at that same instant"
)
def test_mp1_l4b_pong_readable_at_the_peer_timeout_boundary_is_processed_first(monkeypatch):
    lab = run("A5d", monkeypatch)
    pong1 = lab.first_sent("s2c", "pong#1")
    boundary = lab.sessions[0].confirmed_at + float(lab.config["peer_timeout"])
    lab.require(pong1.deliver_at == boundary, "pong#1 was not delivered at the peer_timeout boundary")
    expect.require_window_proves_regained_liveness(lab, pong1.t)
    expect.assert_logical_session_survived(lab)
    assert pong1.consumed_by == "forward_loop"
    assert pong1.consumed_at == boundary


@mp1(
    "MP1-L4c [A5c/T05c]: pre-MP1, a matching PONG arriving 10 ms after the "
    "keepalive deadline is too late -- proactive_rekey already fired; MP1 must "
    "not treat one missed keepalive deadline as terminal"
)
def test_mp1_l4c_pong_just_after_the_keepalive_deadline_keeps_the_session(monkeypatch):
    lab = run("A5c", monkeypatch)
    pong2 = lab.first_sent("s2c", "pong#2")
    lab.require(
        pong2.deliver_at == pytest.approx(expect.keepalive_deadline(lab, 2) + 0.01),
        "pong#2 was not delivered 10 ms after the keepalive deadline",
    )
    expect.require_window_proves_regained_liveness(lab, pong2.t)
    expect.assert_logical_session_survived(lab)


@mp1(
    "MP1-L5 [A6/T07]: pre-MP1, a 35 s client stall with the matching PONG "
    "already in the socket buffer ends the session with proactive_rekey on "
    "resume (1095.0); MP1 must use that buffered authenticated evidence and "
    "keep the LogicalSession"
)
def test_mp1_l5_bounded_client_stall_with_buffered_evidence_keeps_the_session(monkeypatch):
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


@mp1(
    "MP1-L6 [A7/T06a]: pre-MP1, a reverse-path-only loss ends the session at "
    "the first missed keepalive deadline (1090.2) although client->server "
    "delivery is healthy, and forwarding then waits ~70 s for a new session; "
    "MP1 must keep forwarding in the original session past one missed "
    "deadline and still end it within peer_timeout of the last evidence"
)
def test_mp1_l6_reverse_path_loss_does_not_stop_forwarding_at_the_first_miss(monkeypatch):
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


@mp1(
    "MP1-L7 [A9/T14]: pre-MP1, an in-session epoch refresh that committed "
    "25 s before the deadline (authenticated REPLY/ACK liveness evidence) does "
    "not prevent proactive_rekey at 1090.2 after one lost PONG; MP1 failure "
    "accounting must agree with that evidence and keep the LogicalSession"
)
def test_mp1_l7_refresh_evidence_and_a_lost_pong_keep_the_session(monkeypatch):
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


@mp1(
    "MP1-L8 [A10/B5/T09d]: pre-MP1, a proof-matched PATH_ACK that captured no "
    "outstanding ping advances liveness, but ping#2 -- which overtook the "
    "PATH_RESPONSE and drew no PONG from the unproved path -- still forces "
    "proactive_rekey at 1090.2 and discards the just-committed migration; MP1 "
    "must keep the migrated LogicalSession"
)
def test_mp1_l8_ping_overtaking_path_response_keeps_the_migrated_session(monkeypatch):
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
