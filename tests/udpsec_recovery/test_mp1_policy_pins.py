"""MP1 policy pins: the CURRENT client liveness behaviour, exactly.

MP1 replaced the pre-MP1 single-shot keepalive policy (one unanswered ping
ended the session at its keepalive deadline). These tests pin the MP1
client liveness policy with its exact constants and cadence:

* an unanswered ping is retransmitted -- same `seq`, fresh AEAD nonce -- at
  its keepalive deadline and then every `KEEPALIVE_RETRY_INTERVAL_SECONDS`;
* the session ends only at `peer_timeout` after the last authenticated
  evidence: `proactive_rekey` (immediate re-handshake) while a probe is
  outstanding, else `peer_timeout` (`reconnect_delay` first);
* authenticated evidence is credited when it is read, before any deadline;
* a transient local network error drops the affected datagram, pauses
  reading local input for one retry interval and probes the path with a
  keepalive transmission before reading input again.

They are NOT invariants. They exist so that any change of this policy is
deliberate, and they record the MP1 outcome of every scenario whose
pre-MP1 outcome MP0 pinned (UDPSEC_V2_RECOVERY_BASELINE.md, section 6).
The pre-MP2 migration pins that used to follow them were inverted by MP2
into test_mp2_policy_pins.py.
"""

import pytest

from test_secure_udp_helpers import load_proxy_module

from .harness import expect
from .harness.lab import ONE_WAY_DELAY, T0
from .harness.scenarios import run

REKEY_LINE = "Secure session liveness unresolved; starting authenticated re-handshake."
SUSPECT_LINE = (
    "Secure session liveness suspect: keepalive ping #2 not answered yet; "
    "retransmitting it (same sequence, fresh nonce) until peer_timeout."
)
RECOVERED_LINE = (
    "Secure session liveness recovered: keepalive ping #2 answered after "
    "{} retransmission(s)."
)
RTT = 2 * ONE_WAY_DELAY
TWO_RTT = 2 * RTT
KEEPALIVE = 30.0
PEER_TIMEOUT = 90.0
RETRY = 5.0


def _times(packets):
    return [packet.t for packet in packets]


def _lost(lab):
    return [marker for marker, _due, _read in lab.input.produced if lab.delivered_at(marker) is None]


def test_mp1_liveness_constants():
    proxy = load_proxy_module()
    assert proxy.KEEPALIVE_RETRY_INTERVAL_SECONDS == RETRY
    assert proxy.EVIDENCE_DRAIN_MAX_DATAGRAMS == 16
    assert proxy.keepalive_retry_interval({"keepalive_interval": KEEPALIVE}) == RETRY
    assert proxy.keepalive_retry_interval({"keepalive_interval": 2}) == 2.0
    assert proxy.DEFAULT_CONFIG["keepalive_interval"] == KEEPALIVE
    assert proxy.DEFAULT_CONFIG["peer_timeout"] == PEER_TIMEOUT
    delays = {"reconnect_delay": 5}
    assert proxy.retry_delay_for_reason(proxy.SESSION_END_PROACTIVE_REKEY, delays) is None
    assert proxy.retry_delay_for_reason(proxy.SESSION_END_PEER_TIMEOUT, delays) == 5


@pytest.mark.parametrize("scenario_id", ["A2", "A3", "A4", "A9", "A10"])
def test_mp1_one_lost_keepalive_exchange_costs_one_retransmission(monkeypatch, scenario_id):
    """Pre-MP1: proactive_rekey at the keepalive deadline and a fresh
    session. MP1: ping#2 is retransmitted once, at that deadline; its PONG
    one RTT later recovers liveness and the keepalive cadence restarts
    from the retransmission."""
    lab = run(scenario_id, monkeypatch)
    deadline = expect.keepalive_deadline(lab, 2)
    ping2 = lab.first_sent("c2s", "ping#2")
    assert _times(lab.sent("c2s", "ping#2")) == [ping2.t, deadline]
    assert lab.client_lines("liveness suspect") == [(deadline, SUSPECT_LINE)]
    ((recovered_at, recovered),) = lab.client_lines("liveness recovered")
    assert recovered == RECOVERED_LINE.format(1)
    assert recovered_at == pytest.approx(deadline + RTT)
    assert lab.first_sent("c2s", "ping#3").t == pytest.approx(deadline + KEEPALIVE)
    assert lab.hello_times() == [T0]
    assert not lab.client_lines(REKEY_LINE)


def test_mp1_late_original_pong_resolves_the_retransmitted_ping(monkeypatch):
    """A5c: pong#2 arrives 10 ms after the keepalive deadline, i.e. after
    the retransmission. It resolves the logical ping; the retransmission's
    own PONG is then a duplicate and changes nothing."""
    lab = run("A5c", monkeypatch)
    deadline = expect.keepalive_deadline(lab, 2)
    ping2 = lab.first_sent("c2s", "ping#2")
    assert _times(lab.sent("c2s", "ping#2")) == [ping2.t, deadline]
    original, duplicate = lab.sent("s2c", "pong#2")
    assert original.consumed_by == duplicate.consumed_by == "forward_loop"
    assert original.consumed_at == pytest.approx(deadline + 0.01)
    assert duplicate.consumed_at == pytest.approx(deadline + RTT)
    ((recovered_at, _),) = lab.client_lines("liveness recovered")
    assert recovered_at == pytest.approx(deadline + 0.01)
    assert lab.first_sent("c2s", "ping#3").t == pytest.approx(deadline + KEEPALIVE)


@pytest.mark.parametrize("scenario_id, seq", [("A5b", 2), ("A6", 2), ("A5d", 1)])
def test_mp1_readable_matching_pong_is_credited_before_any_deadline(
    monkeypatch, scenario_id, seq
):
    """Inverts the pre-MP1 pin: a matching PONG that is readable when a
    deadline falls due (A5b: the keepalive deadline; A6: the resume after
    a 35 s stall; A5d: the peer_timeout boundary) is read and credited by
    the session first, so its ping is never retransmitted."""
    lab = run(scenario_id, monkeypatch)
    first = lab.sessions[0]
    expected = {
        "A5b": lambda: expect.keepalive_deadline(lab, 2),
        "A6": lambda: 1095.0,
        "A5d": lambda: first.confirmed_at + float(lab.config["peer_timeout"]),
    }[scenario_id]()
    pong = lab.first_sent("s2c", f"pong#{seq}")
    assert pong.consumed_by == "forward_loop"
    assert pong.consumed_at == expected
    assert len(lab.sent("c2s", f"ping#{seq}")) == 1
    assert not lab.client_lines("liveness suspect")
    assert len(lab.sessions) == 1


def test_mp1_unanswered_probe_at_peer_timeout_rekeys_immediately(monkeypatch):
    """A5e (peer_timeout 45): ping#1 (1030.2) is still unanswered when the
    peer_timeout bound (1045.2) arrives -- before its first retransmission
    would be due (1060.2). A probe is outstanding, so the reason is
    proactive_rekey and the fresh ClientHello leaves at once."""
    lab = run("A5e", monkeypatch)
    first, second = lab.sessions
    boundary = first.confirmed_at + float(lab.config["peer_timeout"])
    assert first.reason == lab.proxy.SESSION_END_PROACTIVE_REKEY
    assert first.ended_at == boundary
    assert lab.client_lines(REKEY_LINE) == [(boundary, REKEY_LINE)]
    assert lab.hello_times() == [T0, boundary]
    assert second.confirmed_at == pytest.approx(boundary + TWO_RTT)
    assert len(lab.sent("c2s", "ping#1", locator=first.locator)) == 1


def test_mp1_blackhole_past_the_first_retransmission_recovers_in_session(monkeypatch):
    """A4b (PLANNED in MP0, decided by MP1's bound): a 37 s two-way
    blackhole (1058-1095) swallows ping#2 and its first retransmission;
    the second retransmission (1095.2) gets through and the same session
    continues. Pre-MP1: rekey at 1090.2, a lost ClientHello, 5 s backoff
    and a new session at 1100.4. The four lines produced inside the hole
    are lost either way."""
    lab = run("A4b", monkeypatch)
    deadline = expect.keepalive_deadline(lab, 2)
    ping2 = lab.first_sent("c2s", "ping#2")
    transmissions = lab.sent("c2s", "ping#2")
    assert _times(transmissions) == pytest.approx([ping2.t, deadline, deadline + RETRY])
    assert [p.verdict for p in transmissions] == ["DROP:blackhole", "DROP:blackhole", "sent"]
    ((recovered_at, recovered),) = lab.client_lines("liveness recovered")
    assert recovered == RECOVERED_LINE.format(2)
    assert recovered_at == pytest.approx(deadline + RETRY + RTT)
    expect.assert_logical_session_survived(lab)
    assert _lost(lab) == ["000006", "000007", "000008", "000009"]


def test_mp1_reverse_path_loss_retransmits_until_peer_timeout_then_rekeys(monkeypatch):
    """A7: server->client lost 1055-1155. ping#2 is sent at 1060.2 and
    retransmitted at 1090.2, then every 5 s until the verdict at the last
    evidence (pong#1, 1030.3) + peer_timeout = 1120.3: seven
    retransmissions, the ordinary steady-state count for the default
    configuration -- not a ceiling for every episode (see
    test_a11_reprobe_can_exceed_the_ordinary_retransmission_count in
    test_mp1_liveness_units.py). Forwarding continues in the original
    session until then; the forward-data outage is 40.2 s (pre-MP1: 70.2 s
    from 1090.2)."""
    lab = run("A7", monkeypatch)
    first, second = lab.sessions
    deadline = expect.keepalive_deadline(lab, 2)
    ping2 = lab.first_sent("c2s", "ping#2", locator=first.locator)
    terminal = expect.last_evidence_before(lab, 1055.0) + PEER_TIMEOUT
    assert _times(lab.sent("c2s", "ping#2", locator=first.locator)) == pytest.approx(
        [ping2.t] + [deadline + RETRY * k for k in range(7)]
    )
    assert first.reason == lab.proxy.SESSION_END_PROACTIVE_REKEY
    assert first.ended_at == pytest.approx(terminal)
    assert lab.client_lines(REKEY_LINE) == [(first.ended_at, REKEY_LINE)]
    # Every ServerHello is lost until 1155: four failed handshakes.
    assert lab.handshake_failures == pytest.approx([terminal + 5.0 + 10.0 * k for k in range(4)])
    assert lab.hello_times() == pytest.approx([T0] + [terminal + 10.0 * k for k in range(5)])
    assert second.confirmed_at == pytest.approx(terminal + 40.0 + TWO_RTT)
    assert second.confirmed_at - first.ended_at == pytest.approx(40.2)
    assert _lost(lab) == []
    held = expect.lines_due_between(lab, first.ended_at, second.confirmed_at)
    assert len(held) == 4
    assert all(lab.delivered_at(marker) >= second.confirmed_at for marker, _, _ in held)


def test_mp1_other_evidence_keeps_the_session_while_the_ping_is_retransmitted(monkeypatch):
    """A9b: every PONG from pong#2 on is lost; refresh commits every 65 s
    keep renewing liveness. ping#2 is never resolved, so it keeps being
    retransmitted at the retry cadence for as long as other evidence
    keeps the session alive: rate-bounded (one transmission per 5 s, 6x
    the healthy keepalive rate), not count-bounded."""
    lab = run("A9b", monkeypatch)
    deadline = expect.keepalive_deadline(lab, 2)
    ping2 = lab.first_sent("c2s", "ping#2")
    count = int((lab.until - deadline) // RETRY) + 1
    assert _times(lab.sent("c2s", "ping#2")) == pytest.approx(
        [ping2.t] + [deadline + RETRY * k for k in range(count)]
    )
    assert not lab.sent("c2s", "ping#3")
    assert len(lab.client_lines("Secure epoch refresh committed")) == 4
    assert len(lab.sessions) == 1


def test_mp1_path_ack_is_the_last_evidence_for_the_liveness_bound(monkeypatch):
    """A10b: the PATH_ACK read at 1060.6 is the last evidence; ping#2 is
    retransmitted until the verdict at 1060.6 + peer_timeout = 1150.6,
    and the fresh session is established from the migrated path B."""
    lab = run("A10b", monkeypatch)
    first, second = lab.sessions
    ack = lab.first_sent("s2c", "path_ack", locator=first.locator)
    assert ack.consumed_at == pytest.approx(1060.6)
    assert first.reason == lab.proxy.SESSION_END_PROACTIVE_REKEY
    assert first.ended_at == pytest.approx(ack.consumed_at + PEER_TIMEOUT)
    assert lab.hello_times() == pytest.approx([T0, first.ended_at])
    assert second.observed == ("2001:db8:b::7", 46770)


def test_mp1_server_restart_is_detected_at_peer_timeout(monkeypatch):
    """C5 (documented trade-off, baseline 7.3): the restarted server drops
    the unknown locator silently, so the client cannot tell a restart from
    a loss. It keeps probing until last evidence + peer_timeout (1120.3;
    pre-MP1 1090.2), and every line produced after the restart until then
    is dropped server-side: 8 lines (pre-MP1: 5)."""
    lab = run("C5", monkeypatch)
    first, second = lab.sessions
    terminal = expect.last_evidence_before(lab, 1045.0) + PEER_TIMEOUT
    assert first.reason == lab.proxy.SESSION_END_PROACTIVE_REKEY
    assert first.ended_at == pytest.approx(terminal)
    assert lab.hello_times() == pytest.approx([T0, terminal])
    assert second.confirmed_at == pytest.approx(terminal + TWO_RTT)
    dropped = expect.lines_due_between(lab, 1045.0, first.ended_at)
    assert len(dropped) == 8
    assert _lost(lab) == [marker for marker, _, _ in dropped]


@pytest.mark.parametrize("scenario_id, outage_start", [("C3", 1058.0), ("C3b", 1055.0)])
def test_mp1_dead_forward_path_costs_the_lines_sent_until_the_verdict(
    monkeypatch, scenario_id, outage_start
):
    """C3/C3b (documented trade-off): forwarding continues while liveness
    is unresolved (A8 invariant; MP1-L6), so with the forward path dead
    every line produced until the verdict (last evidence + peer_timeout =
    1120.3) is lost: 7 lines (pre-MP1: 4, because forwarding stopped at
    the first missed keepalive deadline and later lines waited for the
    new session)."""
    lab = run(scenario_id, monkeypatch)
    first = lab.sessions[0]
    terminal = expect.last_evidence_before(lab, outage_start) + PEER_TIMEOUT
    assert first.reason == lab.proxy.SESSION_END_PROACTIVE_REKEY
    assert first.ended_at == pytest.approx(terminal)
    unsent = expect.lines_due_between(lab, outage_start, first.ended_at)
    assert len(unsent) == 7
    assert _lost(lab) == [marker for marker, _, _ in unsent]


def test_mp1_transient_send_error_pauses_input_and_probes_before_resuming(monkeypatch):
    """A11: the 1060.0 NMEA send fails (ENETUNREACH): that line is dropped
    and reading input pauses for one retry interval. ping#2 fails too
    (1060.2) and pauses again. At 1065.2 the path is probed with ping#2
    before any input is read; it succeeds, input resumes, and its PONG
    recovers liveness. Pre-MP1: socket_error at 1060.0, 5 s backoff and a
    new session at 1065.2."""
    lab = run("A11", monkeypatch)
    ping2 = lab.first_sent("c2s", "ping#2")
    probe_at = ping2.t + RETRY
    failed = [(p.kind, p.t) for p in lab.sent("c2s") if not p.transmitted]
    assert failed == [("nmea", 1060.0), ("ping#2", ping2.t)]
    assert _times(lab.sent("c2s", "ping#2")) == pytest.approx([ping2.t, probe_at])
    ((failed_at, failure),) = lab.client_lines("failed transiently")
    assert failed_at == 1060.0
    assert failure.startswith("Secure NMEA send failed transiently (")
    assert "Network is unreachable" in failure
    assert lab.client_lines("usable again") == [
        (pytest.approx(probe_at), "Secure session network path usable again after 2 transient failure(s).")
    ]
    ((recovered_at, recovered),) = lab.client_lines("liveness recovered")
    assert recovered == RECOVERED_LINE.format(1)
    assert recovered_at == pytest.approx(probe_at + RTT)
    assert lab.first_sent("c2s", "ping#3").t == pytest.approx(probe_at + KEEPALIVE)
    assert _lost(lab) == ["000006"]
    assert lab.hello_times() == [T0]


def test_mp1_transient_receive_error_is_reported_and_changes_nothing(monkeypatch):
    """A11b: one ICMP-derived ConnectionResetError on recvfrom at 1070.0 is
    reported once and neither ends the session nor moves the keepalive
    cadence (it is not evidence either way)."""
    lab = run("A11b", monkeypatch)
    ((failed_at, failure),) = lab.client_lines("failed transiently")
    assert failed_at == 1070.0
    assert failure.startswith("Secure receive failed transiently (")
    first_ping = lab.first_sent("c2s", "ping#1").t
    pings = [p for p in lab.sent("c2s", "ping") if p.kind != "ping#0"]
    assert _times(pings) == pytest.approx([first_ping + KEEPALIVE * k for k in range(6)])
    assert [p.kind for p in pings] == [f"ping#{n}" for n in range(1, 7)]
    assert len(lab.sessions) == 1


def test_mp1_long_local_loss_keeps_input_paused_while_probing(monkeypatch):
    """C1: sendto fails for 6 minutes. Exactly one NMEA line meets the dead
    interface; afterwards input stays paused while ping#2 probes the path
    once per retry interval (13 failed transmissions, 1060.2-1120.2), until
    the verdict at last evidence + peer_timeout (1120.3). Handshake send
    errors then back off every reconnect_delay. The paused lines are read
    once the fresh session exists (1420.5); only the first line is lost,
    as pre-MP1."""
    lab = run("C1", monkeypatch)
    first, last = lab.sessions
    ping2 = lab.first_sent("c2s", "ping#2", locator=first.locator)
    terminal = expect.last_evidence_before(lab, 1058.0) + PEER_TIMEOUT
    assert _times([p for p in lab.sent("c2s", "nmea") if not p.transmitted]) == [1060.0]
    probes = lab.sent("c2s", "ping#2", locator=first.locator)
    assert _times(probes) == pytest.approx([ping2.t + RETRY * k for k in range(13)])
    assert not any(p.transmitted for p in probes)
    assert first.reason == lab.proxy.SESSION_END_PROACTIVE_REKEY
    assert first.ended_at == pytest.approx(terminal)
    # Handshake attempts every reconnect_delay from the verdict; the first
    # one after the outage ends (1418.0) is 300 s later.
    reachable_attempt = terminal + 300.0
    assert lab.hello_times() == pytest.approx([T0, reachable_attempt])
    assert last.confirmed_at == pytest.approx(reachable_attempt + TWO_RTT)
    assert _lost(lab) == ["000006"]
