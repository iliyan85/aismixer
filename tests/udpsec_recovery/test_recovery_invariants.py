"""UDPSEC V2 recovery invariants that MUST SURVIVE MP1 and MP2 (MP0).

Ordinary (non-xfail) tests. Each runs the real `nmea_sproxy` client and the
real `aismixer_secure` server through the MP0 lab (harness/lab.py) and
asserts only policy-independent outcomes: facts that hold under the
current single-shot keepalive policy and must keep holding under any MP1
liveness recovery or MP2 migration change. Scenario IDs are those of
SCENARIO_MATRIX.md. Security properties that existing tests already cover
are cited in that matrix instead of being cloned here.
"""

import dataclasses
import inspect

import pytest

import core.udpsec_protocol as protocol
from test_secure_udp_helpers import load_proxy_module

from .harness import expect
from .harness.lab import (
    ADDR_A,
    ADDR_B,
    ADDR_C,
    CGNAT_AFTER,
    CGNAT_BEFORE,
    DEFAULT_CLIENT_CONFIG,
    STATION_ID,
    STILL_RUNNING,
    T0,
    HarnessError,
    Lab,
)
from .harness.scenarios import SCENARIOS, run


def _same_path(lab, first, second):
    return lab.secure.normalize_sockaddr(first) == lab.secure.normalize_sockaddr(second)


# ------------------------------------------------------------- harness guards


def test_lab_client_config_matches_production_defaults():
    defaults = load_proxy_module().DEFAULT_CONFIG
    for key in ("keepalive_interval", "peer_timeout", "session_refresh_interval", "reconnect_delay"):
        assert DEFAULT_CLIENT_CONFIG[key] == defaults[key], key


def test_lab_replica_of_main_relation_loop_matches_production():
    """The lab replicates `nmea_sproxy.main()`'s UDPSEC relation loop
    instead of executing `main()`. Fail loudly if production's loop changes
    shape, so the replica is updated with it."""
    source = inspect.getsource(load_proxy_module().main)
    ordered = (
        "out_sock = create_output_socket(",
        "out_sock.settimeout(5.0)",
        "while True:",
        "confirmed_session = perform_handshake(",
        "reason = forward_loop(",
        "reason = HANDSHAKE_FAILURE",
        "retry_delay = retry_delay_for_reason(reason, config)",
        "if retry_delay is None:",
        "continue",
        "time.sleep(retry_delay)",
    )
    position = 0
    for fragment in ordered:
        index = source.find(fragment, position)
        assert index >= 0, f"main() no longer has {fragment!r} in the expected order"
        position = index + len(fragment)
    # One UDP socket per relation, created before and never inside the loop:
    # every fresh establishment reuses the same local port.
    assert "create_output_socket(" not in source[source.find("while True:"):]


def test_harness_faults_can_never_satisfy_a_strict_xfail():
    assert not issubclass(HarnessError, AssertionError)


def test_harness_fault_on_a_production_call_path_is_not_a_session_outcome(monkeypatch):
    """`forward_loop()` turns any Exception from send/select into
    `socket_error`; a broken fault hook must still surface as HarnessError."""
    lab = Lab("selftest_broken_hook", monkeypatch)

    def broken_hook(message, t, lab_):
        if message and message.get("type") == "ping" and message.get("seq") == 1:
            raise ValueError("broken fault hook")
        return False

    lab.drop_c2s = broken_hook
    with pytest.raises(HarnessError, match="broken fault hook"):
        lab.run_client(until=T0 + 100.0)


def test_harness_busy_loop_guard_is_not_a_session_outcome(monkeypatch):
    lab = Lab("selftest_busy_loop", monkeypatch)
    lab.poll_limit = 5
    with pytest.raises(HarnessError, match="forward_loop polls"):
        lab.run_client(until=T0 + 100.0)


def test_nonce_reuse_detector_flags_a_repeated_datagram(monkeypatch):
    lab = run("A1", monkeypatch)
    assert lab.nonce_reuse() == []
    ping = lab.first_sent("c2s", "ping#1")
    lab.packets.append(dataclasses.replace(ping, t=ping.t + 1.0))
    assert [(a.kind, b.kind) for a, b in lab.nonce_reuse()] == [("ping#1", "ping#1")]


# ------------------------------------------------------------- A: same tuple


def test_a1_healthy_session_stays_one_session_without_extra_probes(monkeypatch):
    lab = run("A1", monkeypatch)
    expect.assert_logical_session_survived(lab)
    session = lab.sessions[0]
    pings = [p for p in lab.sent("c2s", "ping", locator=session.locator) if p.kind != "ping#0"]
    answered = [
        p for p in lab.sent("s2c", "pong", locator=session.locator)
        if p.kind != "pong#0" and p.consumed_by == "forward_loop"
    ]
    assert pings
    assert [p.kind for p in answered] == [p.kind.replace("ping", "pong") for p in pings]
    # A healthy session sends no liveness traffic beyond its keepalive cadence.
    keepalive = float(lab.config["keepalive_interval"])
    assert len(pings) <= (lab.until - session.confirmed_at) / keepalive + 1
    expect.assert_delivered_promptly(lab, lab.input.produced)


def test_a5a_pong_before_the_keepalive_deadline_is_accepted(monkeypatch):
    lab = run("A5a", monkeypatch)
    expect.assert_logical_session_survived(lab)
    pong2 = lab.first_sent("s2c", "pong#2")
    assert pong2.deliver_at < expect.keepalive_deadline(lab, 2)
    assert pong2.consumed_by == "forward_loop"


def test_a8_forwarding_continues_while_liveness_is_unresolved(monkeypatch):
    """A8 (T03 run): while ping#2's PONG is unresolved, NMEA forwarding is
    not paused -- every sentence produced in that window reaches server
    ingress promptly. Policy-neutral: MP0 does not decide whether delivered
    NMEA should ever count as liveness evidence (the client cannot observe
    delivery today)."""
    lab = run("A3", monkeypatch)
    ping2 = lab.first_sent("c2s", "ping#2", locator=lab.sessions[0].locator)
    window = expect.lines_due_between(lab, ping2.t, expect.keepalive_deadline(lab, 2))
    lab.require(len(window) == 3, "expected three NMEA lines inside the unresolved window")
    expect.assert_delivered_promptly(lab, window)


# ------------------------------------------------------------- B: tuple change

# scenario -> (final active path, committed migrations)
B_OUTCOMES = {
    "B1": (ADDR_B, 1),
    "B2": (ADDR_B, 1),
    "B2b": (ADDR_B, 1),
    "B3": (ADDR_B, 1),
    "B4": (ADDR_B, 1),
    "B6a": (ADDR_B, 1),
    "B6b": (ADDR_B, 1),
    "B7": (ADDR_B, 1),
    "B8": (ADDR_C, 2),
    "B10": (ADDR_A, 0),
    "B11": (ADDR_C, 1),
}


@pytest.mark.parametrize("scenario_id", sorted(B_OUTCOMES))
def test_b_tuple_change_keeps_one_logical_session(monkeypatch, scenario_id):
    lab = run(scenario_id, monkeypatch)
    expect.assert_logical_session_survived(lab)
    active, commits = B_OUTCOMES[scenario_id]
    session = lab.original_server_session()
    stats = lab.state.stats()
    assert _same_path(lab, session.path_state.active_path, active)
    assert stats.path_migrations_committed == commits
    # The crypto epoch moves only through in-session refresh, never migration.
    assert session.current_epoch.generation == stats.epoch_refreshes_committed
    expect.assert_delivered_promptly(lab, lab.input.produced)


def test_b1_migration_preserves_session_epoch_ledger_and_namespace(monkeypatch):
    lab = run("B1", monkeypatch)
    (before,) = lab.snapshots["before"]
    session = lab.original_server_session()
    assert before["session"]() is session
    assert before["epoch"]() is session.current_epoch
    assert before["ledger"]() is session.current_epoch.seen_data_nonces
    assert before["assembly_namespace"] == session.assembly_namespace
    assert before["epoch_generation"] == session.current_epoch.generation == 0
    assert _same_path(lab, before["active_path"], ADDR_A)
    assert _same_path(lab, session.path_state.active_path, ADDR_B)
    assert session.path_state.active_path_generation == 1


def test_b8_late_old_path_packet_is_admitted_without_reverse_migration(monkeypatch):
    lab = run("B8", monkeypatch)
    assert lab.state.stats().retired_path_packets_admitted == 1
    assert any("LATE-A" in payload for _t, payload, _key in lab.ingress)


def test_b11_flowinfo_only_change_is_not_a_path_change(monkeypatch):
    lab = run("B11", monkeypatch)
    assert lab.state.stats().path_candidates_opened == 1  # only the move to C


# ------------------------------------------------------------- C: terminal


@pytest.mark.parametrize(
    "scenario_id, final_tuple",
    [("C1", CGNAT_BEFORE), ("C2", CGNAT_AFTER)],
)
def test_c_long_local_loss_is_terminal_recovery_not_migration(
    monkeypatch, scenario_id, final_tuple
):
    """C1/C2 (field-derived): Wi-Fi lost for minutes. The old logical
    session is gone on both sides; recovery is a fresh authenticated
    establishment (path_gen 0) at whatever public tuple the NAT now
    presents, and no path migration is attempted."""
    lab = run(scenario_id, monkeypatch)
    outage_end = 1418.0
    first, last = lab.sessions[0], lab.sessions[-1]
    assert len(lab.sessions) == 2
    assert first.reason != STILL_RUNNING and first.ended_at < outage_end
    assert last.reason == STILL_RUNNING
    assert outage_end <= last.confirmed_at <= expect.fresh_session_bound_after(lab, outage_end)
    assert last.locator != first.locator and last.path_gen == 0
    assert last.observed == final_tuple
    assert lab.client_lines("Handshake send error")
    stats = lab.state.stats()
    assert stats.path_candidates_opened == 0
    assert stats.path_migrations_committed == 0
    assert lab.server_session(first.locator) is None
    assert _same_path(lab, lab.server_session(last.locator).path_state.active_path, final_tuple)
    expect.assert_bounded_retry_cadence(lab)


@pytest.mark.parametrize(
    "scenario_id, outage_start, restored",
    [("C3", 1058.0, 1300.0), ("C3b", 1055.0, 1155.0)],
)
def test_c_long_outage_still_terminates_and_re_establishes(
    monkeypatch, scenario_id, outage_start, restored
):
    """MP1-L9 guard (C3, C3b/T06b): an outage longer than `peer_timeout`
    still ends the session no later than `peer_timeout` after the last
    authenticated evidence -- no immortal session -- and a fresh
    authenticated session follows within one handshake timeout plus one
    `reconnect_delay` of the path returning."""
    lab = run(scenario_id, monkeypatch)
    first, last = lab.sessions[0], lab.sessions[-1]
    evidence = expect.last_evidence_before(lab, outage_start)
    assert first.reason != STILL_RUNNING
    assert first.ended_at <= evidence + float(lab.config["peer_timeout"])
    assert len(lab.sessions) == 2 and last.reason == STILL_RUNNING
    assert restored <= last.confirmed_at <= expect.fresh_session_bound_after(lab, restored)
    assert last.locator != first.locator and last.path_gen == 0
    expect.assert_bounded_retry_cadence(lab)


def test_c4_old_session_ciphertext_is_inert_after_fresh_establishment(monkeypatch):
    """C4 / D7: after a terminal recovery replaced the old session at the
    same relation, neither a replayed old-session datagram nor a freshly
    encrypted old-session ping draws a reply, reaches ingress or moves any
    counter (unknown locator: silent drop)."""
    lab = run("C3", monkeypatch)
    first, last = lab.sessions[0], lab.sessions[-1]
    lab.require(first.locator != last.locator, "no fresh establishment happened")
    old_nmea = [
        p for p in lab.sent("c2s", "nmea", locator=first.locator) if p.verdict == "sent"
    ][-1]
    old_ping = lab.proxy.encrypt_secure_json_message(
        protocol.build_ping_message(STATION_ID, 99, 1),
        first.epochs.client_to_server_key,
        first.locator,
        first.epochs.generation,
    )
    stats_before, ingress_before = lab.state.stats(), len(lab.ingress)
    assert lab.inject(old_nmea.raw, ADDR_A) == []
    assert lab.inject(old_ping, ADDR_A) == []
    assert lab.state.stats() == stats_before
    assert len(lab.ingress) == ingress_before
    assert lab.server_session(first.locator) is None
    # Positive control: the same injection path admits current-session data.
    current_nmea = lab.proxy.encrypt_secure_json_message(
        {"type": "nmea", "payload": "!AIVDM,1,1,,A,CTRL,0*00", "timestamp": 1, "source_id": STATION_ID},
        last.epochs.client_to_server_key,
        last.locator,
        last.epochs.generation,
    )
    assert lab.inject(current_nmea, ADDR_A) == []
    assert len(lab.ingress) == ingress_before + 1


def test_c5_server_restart_is_recovered_within_the_peer_timeout_bound(monkeypatch):
    """C5 (T16): the restarted server silently drops the unknown locator
    (anti-probing, D1); the client must still re-establish no later than
    `peer_timeout` after its last authenticated evidence plus one
    `reconnect_delay`."""
    lab = run("C5", monkeypatch)
    restart = 1045.0
    first, last = lab.sessions[0], lab.sessions[-1]
    evidence = expect.last_evidence_before(lab, restart)
    bound = (
        evidence
        + float(lab.config["peer_timeout"])
        + float(lab.config["reconnect_delay"])
        + 1.0
    )
    assert first.reason != STILL_RUNNING
    assert len(lab.sessions) == 2 and last.reason == STILL_RUNNING
    assert last.confirmed_at <= bound
    assert not [p for p in lab.sent("s2c", locator=first.locator) if p.t >= restart]


# ------------------------------------------------------------- D: sweep + wire


@pytest.mark.parametrize("scenario_id", sorted(SCENARIOS))
def test_every_scenario_keeps_nonce_uniqueness_retry_cadence_and_bounded_state(
    monkeypatch, scenario_id
):
    """D9 and bounded-recovery guards over the whole catalogue: no AEAD
    nonce is ever reused within one (direction, locator, epoch); no busy
    re-establishment loop; server path/epoch/pending state stays bounded."""
    lab = run(scenario_id, monkeypatch)
    expect.assert_no_nonce_reuse(lab)
    expect.assert_bounded_retry_cadence(lab)
    expect.assert_bounded_server_state(lab)


def test_l10_current_server_answers_a_fresh_nonce_ping_retransmission(monkeypatch):
    """MP1-L10: client-only liveness recovery needs no wire or version
    change. The CURRENT server answers every admitted active-path ping with
    a PONG echoing its seq -- a retransmission of the same seq under a
    fresh AEAD nonce, and a lower seq, included -- while a byte-identical
    replay is refused by the per-epoch replay ledger. The wire version and
    DATA prefix stay pinned by tests/test_udpsec_protocol.py."""
    lab = run("A1", monkeypatch)
    session = lab.sessions[0]
    epochs = session.epochs

    def ping(seq):
        return lab.proxy.encrypt_secure_json_message(
            protocol.build_ping_message(STATION_ID, seq, 1),
            epochs.client_to_server_key,
            session.locator,
            epochs.generation,
        )

    def answers(replies):
        out = []
        for data, destination in replies:
            assert _same_path(lab, destination, ADDR_A)
            message = lab.decode(data)[0]
            out.append((message["type"], message["seq"]))
        return out

    first, retransmission = ping(77), ping(77)
    assert first != retransmission
    replays = lab.state.stats().data_nonce_replays
    assert answers(lab.inject(first, ADDR_A)) == [("pong", 77)]
    assert answers(lab.inject(retransmission, ADDR_A)) == [("pong", 77)]
    assert answers(lab.inject(ping(5), ADDR_A)) == [("pong", 5)]
    assert lab.inject(first, ADDR_A) == []
    assert lab.state.stats().data_nonce_replays == replays + 1
