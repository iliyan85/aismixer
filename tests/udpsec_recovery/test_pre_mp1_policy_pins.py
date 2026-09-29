"""PRE-MP1 (and pre-MP2) policy pins: the CURRENT behaviour, exactly.

These tests reproduce inside the repository the failure sequence the
independent Fable audit traced (harness T02-T16) and the IPv6 field
screenshot shows: a stable session -> "Secure session liveness
unresolved; starting authenticated re-handshake." -> an immediate fresh
ECDHE establishment -> a new session label, epoch 0, path_gen 0, the same
observed tuple.

They are NOT invariants. They exist so that each strict xfail in
test_mp1_liveness_acceptance.py fails for exactly one documented reason,
and so that any change to this policy is deliberate. MP1 is expected to
delete or invert each pre-MP1 pin together with its paired xfail; MP2 does
the same for the pre-MP2 migration pins at the end. See
UDPSEC_V2_RECOVERY_BASELINE.md, "MP1 impact inventory".
"""

import pytest

from .harness import expect
from .harness.lab import (
    ADDR_A,
    ADDR_B,
    CLIENT_SOCKET_TIMEOUT,
    ONE_WAY_DELAY,
    STILL_RUNNING,
    T0,
)
from .harness.scenarios import run

REKEY_LINE = "Secure session liveness unresolved; starting authenticated re-handshake."
TWO_RTT = 4 * ONE_WAY_DELAY
OBSERVED_A = (ADDR_A[0], ADDR_A[1])
OBSERVED_B = (ADDR_B[0], ADDR_B[1])


@pytest.mark.parametrize(
    "scenario_id, observed_after",
    [
        ("A2", OBSERVED_A),
        ("A3", OBSERVED_A),
        ("A4", OBSERVED_A),
        ("A5b", OBSERVED_A),
        ("A5c", OBSERVED_A),
        ("A9", OBSERVED_A),
        ("A10", OBSERVED_B),
    ],
)
def test_pre_mp1_one_unresolved_keepalive_forces_immediate_fresh_establishment(
    monkeypatch, scenario_id, observed_after
):
    lab = run(scenario_id, monkeypatch)
    deadline = expect.keepalive_deadline(lab, 2)
    assert len(lab.sessions) == 2
    first, second = lab.sessions
    assert first.reason == lab.proxy.SESSION_END_PROACTIVE_REKEY
    assert first.ended_at == deadline
    assert lab.client_lines(REKEY_LINE) == [(deadline, REKEY_LINE)]
    # Immediate: no reconnect_delay before the fresh ClientHello.
    assert lab.hello_times() == [T0, deadline]
    assert second.confirmed_at == pytest.approx(deadline + TWO_RTT)
    assert second.reason == STILL_RUNNING
    # A genuinely fresh session: new locator, path_gen 0, and the same
    # socket (one local port) gives the same server-observed tuple.
    assert second.locator != first.locator
    assert second.path_gen == 0
    assert second.observed == observed_after
    stats = lab.state.stats()
    assert (stats.sessions_created, stats.sessions_replaced) == (2, 1)
    assert lab.server_session(first.locator) is None


@pytest.mark.parametrize("scenario_id", ["A2", "A3"])
def test_pre_mp1_lost_liveness_packet_costs_no_nmea_but_a_new_session(
    monkeypatch, scenario_id
):
    lab = run(scenario_id, monkeypatch)
    assert len(lab.ingress) == len(lab.input.produced) == 19
    assert len(lab.sessions) == 2


@pytest.mark.parametrize("scenario_id, seq", [("A5b", 2), ("A6", 2), ("A5d", 1)])
def test_pre_mp1_readable_matching_pong_is_discarded_by_the_next_handshake(
    monkeypatch, scenario_id, seq
):
    """F2: the terminal verdict is taken before a matching PONG that is
    already readable is processed; the next handshake then skips it as
    old-session noise."""
    lab = run(scenario_id, monkeypatch)
    first, second = lab.sessions[0], lab.sessions[1]
    pong = lab.first_sent("s2c", f"pong#{seq}", locator=first.locator)
    assert pong.deliver_at <= first.ended_at
    assert pong.consumed_by == "handshake"
    assert pong.consumed_at == second.handshake_started


def test_pre_mp1_peer_timeout_verdict_precedes_a_pong_readable_at_the_boundary(monkeypatch):
    lab = run("A5d", monkeypatch)
    first = lab.sessions[0]
    assert first.reason == lab.proxy.SESSION_END_PEER_TIMEOUT
    assert first.ended_at == first.confirmed_at + float(lab.config["peer_timeout"])
    # peer_timeout waits reconnect_delay before the next attempt.
    assert lab.hello_times()[1] == first.ended_at + float(lab.config["reconnect_delay"])


def test_pre_mp1_client_stall_across_the_deadline_forces_rekey_on_resume(monkeypatch):
    lab = run("A6", monkeypatch)
    first = lab.sessions[0]
    assert first.reason == lab.proxy.SESSION_END_PROACTIVE_REKEY
    assert first.ended_at == 1095.0
    assert lab.hello_times() == [T0, 1095.0]


def test_pre_mp1_outage_past_the_deadline_adds_handshake_timeout_and_backoff(monkeypatch):
    lab = run("A4b", monkeypatch)
    deadline = expect.keepalive_deadline(lab, 2)
    first, second = lab.sessions
    delay = float(lab.config["reconnect_delay"])
    assert first.reason == lab.proxy.SESSION_END_PROACTIVE_REKEY
    assert first.ended_at == deadline
    assert lab.handshake_failures == [deadline + CLIENT_SOCKET_TIMEOUT]
    assert lab.hello_times() == [T0, deadline, deadline + CLIENT_SOCKET_TIMEOUT + delay]
    # Recovery lands 5.4 s after the 37 s blackhole (1058-1095) ended.
    assert second.confirmed_at - 1095.0 == pytest.approx(5.4)


def test_pre_mp1_reverse_path_loss_becomes_a_long_forward_data_outage(monkeypatch):
    lab = run("A7", monkeypatch)
    deadline = expect.keepalive_deadline(lab, 2)
    assert len(lab.sessions) == 2
    first, second = lab.sessions
    assert first.reason == lab.proxy.SESSION_END_PROACTIVE_REKEY
    assert first.ended_at == deadline
    # Every ServerHello is lost while server->client is down (until 1155).
    assert len(lab.handshake_failures) == 7
    assert second.confirmed_at == pytest.approx(1160.4)
    assert second.confirmed_at - deadline == pytest.approx(70.2)
    # client->server stayed healthy, yet every line produced after the
    # verdict waited for the new session.
    held = expect.lines_due_between(lab, deadline, 1155.0)
    assert len(held) == 6
    assert all(lab.delivered_at(marker) >= second.confirmed_at for marker, _, _ in held)


def test_pre_mp1_short_local_send_error_ends_the_session_with_backoff(monkeypatch):
    lab = run("A11", monkeypatch)
    first, second = lab.sessions
    delay = float(lab.config["reconnect_delay"])
    assert first.reason == lab.proxy.SESSION_END_SOCKET_ERROR
    assert first.ended_at == 1060.0  # first NMEA send inside the 1059.9-1062.0 flap
    failures = lab.client_lines("Forwarding error")
    assert failures and "Network is unreachable" in failures[0][1]
    assert lab.hello_times() == [T0, 1060.0 + delay]
    assert second.confirmed_at == pytest.approx(1060.0 + delay + TWO_RTT)
    # The sentence whose send failed is not retried.
    assert lab.delivered_at("000006") is None


def test_pre_mp1_server_restart_is_noticed_only_at_the_keepalive_deadline(monkeypatch):
    lab = run("C5", monkeypatch)
    deadline = expect.keepalive_deadline(lab, 2)
    first = lab.sessions[0]
    assert first.reason == lab.proxy.SESSION_END_PROACTIVE_REKEY
    assert first.ended_at == deadline
    dropped = expect.lines_due_between(lab, 1045.0, deadline)
    assert [lab.delivered_at(marker) for marker, _, _ in dropped] == [None] * 5


# ------------------------------------------------------------- pre-MP2 pins


@pytest.mark.parametrize("scenario_id", ["B1", "B2", "B2b", "B3", "B4", "B10"])
def test_pre_mp2_one_path_challenge_per_candidate_incarnation(monkeypatch, scenario_id):
    lab = run(scenario_id, monkeypatch)
    stats = lab.state.stats()
    assert stats.migration_challenges_sent == stats.path_candidates_opened


@pytest.mark.parametrize("scenario_id", ["B2", "B3"])
def test_pre_mp2_lost_challenge_or_response_waits_for_candidate_expiry(
    monkeypatch, scenario_id
):
    lab = run(scenario_id, monkeypatch)
    challenges = lab.sent("s2c", "path_challenge")
    assert len(challenges) == 2
    assert challenges[1].t - challenges[0].t >= lab.secure.PATH_CANDIDATE_TTL_SECONDS
    ack = lab.first_sent("s2c", "path_ack")
    assert ack.t - challenges[0].t == pytest.approx(10.1)
