"""UDPSEC V2 field-diagnostics addendum -- acceptance (pre-trip observability
prompt: authenticated observed endpoint in PONG, active_path_generation,
client anti-stale observation, sparse client heartbeat, sparse server
migration-lifecycle logs, and the ServerHello handshake-noise fix).

This is diagnostic/display-only work: it introduces no new packet types, no
protocol-version bump, and touches no handshake crypto, source policy,
nonce replay, session identity, epoch-refresh choreography, or migration
candidate/challenge/response/ACK decision logic. These tests exist to prove
that boundary -- that the new observability surfaces real, already-
authenticated data without ever gaining its own authority -- not to
re-verify the underlying UDPSEC V2 protocol machinery, which is already
covered exhaustively by test_udpsec_path_migration.py,
test_udpsec_client_path_migration.py, test_udpsec_refresh.py, and
test_udpsec_mobile_path_e2e.py.

Numbered G1-G11 sections below correspond to the field-diagnostics prompt's
own test contract.
"""

import asyncio
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import traceback
import types
from collections import OrderedDict
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import core.udpsec_protocol as p

from test_secure_udp_helpers import (  # noqa: E402
    _FakeClock,
    _FakeHandshakeSocket,
    _FakeSecureSocket,
    _TestConfirmingServer,
    _encrypted_control_packet,
    _install_test_session,
    _run_secure_server_with_packets,
    load_proxy_module,
    load_secure_module_with_fake_keys,
)
from test_udpsec_path_migration import ADDR_A, ADDR_B, ADDR_C, STATION_ID, _Env
from test_udpsec_refresh import _full_refresh
from test_udpsec_mobile_path_e2e import (  # noqa: E402
    _MobileNetworkShim,
    _decode_any_epoch,
    _identity_fields,
    _install_matching_session,
    run_mobile_client,
)


@pytest.fixture
def proxy():
    return load_proxy_module()


@pytest.fixture
def env(monkeypatch):
    secure, client_private_key = load_secure_module_with_fake_keys(
        monkeypatch, with_client_private_key=True
    )
    return _Env(monkeypatch, secure, client_private_key)


@pytest.fixture
def keys():
    return {
        "locator": b"M" * 16,
        "c2s": AESGCM.generate_key(bit_length=256),
        "s2c": AESGCM.generate_key(bit_length=256),
    }


def _server_diagnostics_output(state, capsys):
    """Server migration diagnostics are written by an off-event-loop
    consumer, never on the packet path. Deterministically deliver whatever
    this owner has queued, on the test thread, then return all captured
    stdout. Only the observation method changes; event counts do not."""
    state.migration_diagnostics.flush()
    return capsys.readouterr().out


# ==========================================================================
# G1. Server PONG observed IP/port is from the actual admitted recvfrom()
#     source (IPv4 case is also re-proven with an exact-dict assertion in
#     test_secure_udp_helpers.py; this file adds the IPv6 case).
# ==========================================================================


def test_g1_server_pong_observed_endpoint_matches_ipv6_recvfrom_source(
    monkeypatch,
):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    client_to_server_key = b"\x01" * 32
    server_to_client_key = b"\x03" * 32
    nonce = b"\x02" * 12
    # Native OS recvfrom() 4-tuple shape for IPv6 (flowinfo, scope_id).
    addr = ("2001:db8::10", 53142, 0, 0)
    state = secure.SecureState()
    session, _, _ = _install_test_session(
        secure, state, addr, client_to_server_key, server_to_client_key,
    )
    packet = _encrypted_control_packet(
        secure,
        client_to_server_key,
        nonce,
        {
            "type": "ping",
            "seq": 5,
            "timestamp": 1000,
            "source_id": STATION_ID,
        },
        session._session_key.session_locator,
    )
    wall_clock = _FakeClock(2020.0)
    monotonic_clock = _FakeClock(1010.0)

    _fake_queue, fake_socket = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(packet, addr)],
        state=state,
        wall_clock=wall_clock,
        monotonic_clock=monotonic_clock,
    )

    assert len(fake_socket.sent) == 1
    response, _response_addr = fake_socket.sent[0]
    response_locator, _sel, response_nonce, ciphertext = secure.parse_data_packet(
        response
    )
    pong = secure.json.loads(
        secure.AESGCM(server_to_client_key)
        .decrypt(
            response_nonce,
            ciphertext,
            secure.build_data_aad(response_locator, 0),
        )
        .decode()
    )
    # flowinfo/scope_id (both 0 here) must never leak into the port, and
    # the IPv6 literal is the canonical `ipaddress`-normalized form.
    assert pong["observed_endpoint"] == {"ip": "2001:db8::10", "port": 53142}
    assert pong["active_path_generation"] == 0


# ==========================================================================
# G2. Confirmation PONG (seq=0) diagnostics propagate into the first client
#     heartbeat via ConfirmedUdpsecSession -> forward_loop's
#     _ClientObservedEndpoint seed.
# ==========================================================================


def _first_heartbeat_after_confirmation(
    proxy, monkeypatch, capsys, observed_endpoint, active_path_generation
):
    """Run the REAL forward_loop() for one freshly confirmed session whose
    confirmation PONG (seq=0) carried the given diagnostics, until its
    first sparse heartbeat; no ordinary PONG is ever received. Returns the
    captured stdout."""
    import itertools

    confirmed_session = proxy.ConfirmedUdpsecSession(
        session_locator=b"\x04" * 16,
        key_material=proxy.SessionKeyMaterial(
            client_to_server_key=b"\x01" * 32,
            server_to_client_key=b"\x02" * 32,
        ),
        observed_endpoint=observed_endpoint,
        active_path_generation=active_path_generation,
    )
    remote_addr = ("192.0.2.10", 19999)

    class _FakeAdapter:
        def selectable_sockets(self):
            return []

        def poll_interval(self):
            return 0.0

        def read_ready(self, _ready_socket):
            return []

        def read_pending(self):
            return []

    class _OutSocket:
        def __init__(self):
            self.sent = []

        def sendto(self, data, destination):
            self.sent.append((data, destination))

    out_sock = _OutSocket()
    # Four warm-up ticks at t=0 (matches forward_loop's per-iteration
    # double time.monotonic() sampling before any deadline is due), then a
    # jump straight to the 60s heartbeat cadence.
    clock = itertools.chain([0.0, 0.0, 0.0, 0.0, 61.0], itertools.repeat(61.0))
    monkeypatch.setattr(proxy.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(proxy.time, "time", lambda: 1000)

    select_calls = []

    def fake_select(_r, _w, _e, _t):
        select_calls.append(1)
        if len(select_calls) >= 2:
            raise OSError("end test")
        return ([], [], [])

    monkeypatch.setattr(proxy.select, "select", fake_select)

    reason = proxy.forward_loop(
        _FakeAdapter(),
        out_sock,
        {
            "station_id": "boat_001",
            "keepalive_interval": 30,
            "peer_timeout": 90,
            "session_refresh_interval": 0,
        },
        confirmed_session,
        remote_addr,
    )
    assert reason == proxy.SESSION_END_SOCKET_ERROR
    return capsys.readouterr().out


def test_g2_confirmation_diagnostics_propagate_to_first_heartbeat(
    proxy, monkeypatch, capsys
):
    out = _first_heartbeat_after_confirmation(
        proxy, monkeypatch, capsys, ("203.0.113.9", 53142), 0
    )
    assert out.count("Runtime:") == 1
    # No ordinary PONG was ever received in this scenario -- the displayed
    # observation can only have come from the confirmation-time seed.
    assert "observed=203.0.113.9:53142" in out
    assert "age=" in out
    assert "last-known" not in out
    # ... and so can its generation: 0 on the first heartbeat (PG).
    assert "observed=203.0.113.9:53142 path_gen=0 age=" in out


# ==========================================================================
# G3. Wire-tolerance: the existing pong matcher accepts additive fields; new
#     diagnostic parsing tolerates an old PONG lacking them and rejects
#     malformed/out-of-range/injected members without raising or granting
#     authority.
# ==========================================================================


def test_g3_existing_pong_matcher_tolerates_additive_diagnostic_fields():
    message = {
        "type": "pong",
        "seq": 3,
        "timestamp": 1000,
        "source_id": "boat_001",
        "observed_endpoint": {"ip": "203.0.113.9", "port": 53142},
        "active_path_generation": 2,
    }
    assert p.is_matching_pong_message(message, "boat_001", 3) is True


@pytest.mark.parametrize(
    "message,expected",
    [
        pytest.param(
            {
                "observed_endpoint": {"ip": "203.0.113.9", "port": 53142},
                "active_path_generation": 7,
            },
            (("203.0.113.9", 53142), 7),
            id="valid-both",
        ),
        pytest.param(
            {"observed_endpoint": {"ip": "203.0.113.9", "port": 53142}},
            (("203.0.113.9", 53142), None),
            id="old-server-no-generation",
        ),
        pytest.param({}, (None, None), id="old-server-no-diagnostics"),
        pytest.param(
            {"observed_endpoint": "not-a-dict"},
            (None, None),
            id="endpoint-not-a-dict",
        ),
        pytest.param(
            {"observed_endpoint": {"ip": "not-an-ip", "port": 1}},
            (None, None),
            id="unparsable-ip",
        ),
        pytest.param(
            {"observed_endpoint": {"ip": "203.0.113.9", "port": -1}},
            (None, None),
            id="negative-port",
        ),
        pytest.param(
            {"observed_endpoint": {"ip": "203.0.113.9", "port": 70000}},
            (None, None),
            id="port-out-of-range",
        ),
        pytest.param(
            {"observed_endpoint": {"ip": "203.0.113.9", "port": "53142"}},
            (None, None),
            id="port-not-an-int",
        ),
        pytest.param(
            {"observed_endpoint": {"ip": "203.0.113.9", "port": True}},
            (None, None),
            id="port-is-bool",
        ),
        pytest.param(
            {"observed_endpoint": {"ip": "203.0.113.9"}},
            (None, None),
            id="endpoint-missing-port",
        ),
        pytest.param(
            {"active_path_generation": -1},
            (None, None),
            id="negative-generation",
        ),
        pytest.param(
            {"active_path_generation": "7"},
            (None, None),
            id="generation-not-an-int",
        ),
        pytest.param(
            {"active_path_generation": True},
            (None, None),
            id="generation-is-bool",
        ),
        pytest.param(
            {"active_path_generation": p.MAX_PATH_GENERATION + 1},
            (None, None),
            id="generation-out-of-range",
        ),
        pytest.param("not-a-dict-at-all", (None, None), id="message-not-a-dict"),
    ],
)
def test_g3_parse_pong_diagnostics_rejects_malformed_members(proxy, message, expected):
    assert proxy._parse_pong_diagnostics(message) == expected


# ==========================================================================
# G4/G5. active_path_generation: candidate open/replace/expire without
#     commit never advances it; a successful commit advances it to exactly
#     the committed generation; in-session refresh leaves it unchanged; a
#     fresh LogicalSession from a full re-handshake starts back at 0.
# ==========================================================================


def test_g4_candidate_lifecycle_without_commit_leaves_generation_at_zero(env):
    state = env.new_state(path_candidate_ttl=5.0, retired_path_grace=5.0)
    sess = env.install_session(state, addr=ADDR_A, now=1000.0)
    assert state.path_state_snapshot(sess.session, 1000.0)["active_path_generation"] == 0

    # OPENED: a brand-new candidate from B.
    sess.feed([(sess.nmea_packet("!AIVDM,1,1,,A,x,0*00"), ADDR_B)], 1000.0)
    snap = state.path_state_snapshot(sess.session, 1000.0)
    assert snap["candidate"] is not None
    assert snap["active_path_generation"] == 0

    # REPLACED: fresh traffic from a different new path C displaces B.
    sess.feed([(sess.nmea_packet("!AIVDM,1,1,,A,x,0*00"), ADDR_C)], 1000.0)
    snap = state.path_state_snapshot(sess.session, 1000.0)
    assert snap["candidate"]["sockaddr"] == ADDR_C
    assert snap["active_path_generation"] == 0

    # EXPIRED: past the candidate TTL, lazily discarded, still no advance.
    snap = state.path_state_snapshot(sess.session, 1010.0)
    assert snap["candidate"] is None
    assert snap["active_path_generation"] == 0


def test_g5_commit_advances_generation_and_rehandshake_resets(env):
    state = env.new_state(path_candidate_ttl=1000.0, retired_path_grace=1000.0)
    sess = env.install_session(state, addr=ADDR_A, now=1000.0)
    assert state.path_state_snapshot(sess.session, 1000.0)["active_path_generation"] == 0

    challenge, _ = sess.migrate(ADDR_A, ADDR_B, now=1000.0)
    snap = state.path_state_snapshot(sess.session, 1000.0)
    assert snap["active"] == ADDR_B
    assert snap["active_path_generation"] == challenge.path_generation == 1

    # A second real commit (B -> C) advances again, to ITS committed
    # generation -- never merely "increment by one" as an independent rule.
    challenge2, _ = sess.migrate(ADDR_B, ADDR_C, now=1000.0)
    snap2 = state.path_state_snapshot(sess.session, 1000.0)
    assert snap2["active"] == ADDR_C
    assert snap2["active_path_generation"] == challenge2.path_generation == 2

    # A fresh LogicalSession from a full re-handshake (a distinct
    # install_session call, distinct endpoint token/locator) starts at 0,
    # independent of any other session's generation.
    fresh = env.install_session(state, addr=ADDR_A, now=5000.0, locator=b"F" * 16)
    assert state.path_state_snapshot(fresh.session, 5000.0)["active_path_generation"] == 0


def test_g5_in_session_refresh_leaves_active_path_generation_unchanged(env):
    state = env.new_state(path_candidate_ttl=1000.0, retired_path_grace=1000.0)
    sess = env.install_session(state, addr=ADDR_A, now=1000.0)
    sess.migrate(ADDR_A, ADDR_B, now=1000.0)
    generation_before = state.path_state_snapshot(sess.session, 1000.0)[
        "active_path_generation"
    ]
    assert generation_before == 1
    path_state_object_before = sess.session.path_state

    result = _full_refresh(env, state, sess.session, s2c_key=sess.s2c_key)
    assert result["newly"] is True
    assert sess.session.current_epoch.generation == 1

    # Refresh mutates current_epoch/pending_epoch/retiring_epoch only; the
    # exact same PathState object, unmutated, is still installed.
    assert sess.session.path_state is path_state_object_before
    assert (
        state.path_state_snapshot(sess.session, 1000.0)["active_path_generation"]
        == generation_before
    )


# ==========================================================================
# G6. Client anti-stale observation: _ClientObservedEndpoint generation
#     ordering, floor-raising, and last-known marking.
# ==========================================================================


def test_g6_equal_and_higher_generation_refresh_lower_is_rejected(proxy):
    observed = proxy._ClientObservedEndpoint()
    observed.observe_pong(("203.0.113.9", 1), 3, 100.0)
    assert observed.display(100.0) == (("203.0.113.9", 1), 0.0, False)

    # A lower generation than already accepted is stale -- rejected.
    observed.observe_pong(("198.51.100.1", 2), 2, 110.0)
    assert observed.display(110.0) == (("203.0.113.9", 1), 10.0, False)

    # Equal generation refreshes the observation (new receipt time).
    observed.observe_pong(("203.0.113.9", 1), 3, 120.0)
    assert observed.display(120.0) == (("203.0.113.9", 1), 0.0, False)

    # A strictly higher generation advances it.
    observed.observe_pong(("198.51.100.1", 2), 4, 130.0)
    assert observed.display(130.0) == (("198.51.100.1", 2), 0.0, False)


def test_g6_unversioned_pong_never_overwrites_generation_aware_observation(proxy):
    observed = proxy._ClientObservedEndpoint()
    # An old-server unversioned PONG can fill in an otherwise-unknown
    # display...
    observed.observe_pong(("203.0.113.9", 1), None, 100.0)
    assert observed.display(100.0)[0] == ("203.0.113.9", 1)
    # ...but once ANY generation-aware observation has been accepted
    # (including generation 0), a later unversioned PONG can never
    # overwrite it, even with a "fresher" address.
    observed.observe_pong(("203.0.113.9", 1), 0, 101.0)
    observed.observe_pong(("198.51.100.1", 2), None, 200.0)
    assert observed.display(200.0) == (("203.0.113.9", 1), 99.0, False)


def test_g6_raise_floor_marks_last_known_only_when_stale_never_moves_backwards(proxy):
    observed = proxy._ClientObservedEndpoint()
    observed.observe_pong(("203.0.113.9", 1), 1, 100.0)

    # Raising the floor above the last accepted generation marks the
    # existing observation last-known.
    observed.raise_floor(3)
    assert observed.display(150.0) == (("203.0.113.9", 1), 50.0, True)

    # The floor never moves backwards: a lower/duplicate ack generation is
    # a no-op.
    observed.raise_floor(2)
    observed.raise_floor(3)
    assert observed.display(150.0)[2] is True

    # A late PONG at the OLD (pre-migration) generation cannot roll the
    # display back or clear last-known -- it is below the floor.
    observed.observe_pong(("203.0.113.9", 1), 1, 160.0)
    assert observed.display(160.0) == (("203.0.113.9", 1), 60.0, True)

    # Only a PONG at or above the new floor clears last-known.
    observed.observe_pong(("198.51.100.1", 2), 3, 170.0)
    assert observed.display(170.0) == (("198.51.100.1", 2), 0.0, False)


def test_g6_raise_floor_does_not_mark_last_known_if_already_current(proxy):
    observed = proxy._ClientObservedEndpoint()
    # A PONG already reflecting the NEW generation arrives (e.g. racing
    # ahead of the ACK); the subsequent floor-raise for that same
    # generation must not retroactively mark it stale.
    observed.observe_pong(("198.51.100.1", 2), 3, 100.0)
    observed.raise_floor(3)
    assert observed.display(100.0) == (("198.51.100.1", 2), 0.0, False)


def test_g6_extract_authenticated_ack_generation_is_defensive(proxy, keys):
    epochs = proxy._ClientEpochSet(
        keys["locator"],
        proxy.SessionKeyMaterial(
            client_to_server_key=keys["c2s"], server_to_client_key=keys["s2c"]
        ),
    )
    # Garbage that cannot possibly decrypt/parse must yield None, not raise
    # -- this helper has no admission authority of its own.
    assert proxy._extract_authenticated_ack_generation(b"not a packet", epochs) is None


# ==========================================================================
# G8. ServerHello handshake-noise fix: a delayed old-session DATA datagram
#     is skipped silently; a genuinely malformed ServerHello-shaped packet
#     still warns and fails closed.
# ==========================================================================


def _handshake_fixture(proxy, remote_addr=("192.0.2.10", 17777)):
    station_identity_private_key = ec.derive_private_key(21, ec.SECP256R1())
    server_identity_private_key = ec.derive_private_key(22, ec.SECP256R1())
    confirming_server = _TestConfirmingServer(
        proxy, server_identity_private_key, remote_addr,
    )
    return (
        station_identity_private_key,
        server_identity_private_key,
        confirming_server,
    )


def test_g8_delayed_old_session_data_datagram_is_silently_skipped(
    proxy, monkeypatch, capsys
):
    remote_addr = ("192.0.2.10", 17777)
    station_private_key, server_private_key, confirming_server = _handshake_fixture(
        proxy, remote_addr
    )
    # An encrypted DATA-shaped datagram from an old session, arriving from
    # the pinned server address during a fresh handshake on the reused
    # socket -- definitely not ServerHello-shaped.
    stale_data_packet = proxy.DATA_PREFIX + os.urandom(48)

    sock = _FakeHandshakeSocket((
        (stale_data_packet, remote_addr),
        confirming_server.server_hello_response,
        confirming_server.confirmation_pong_response,
    ))
    monkeypatch.setattr(proxy.time, "time", lambda: 1000)

    confirmed_session = proxy.perform_handshake(
        sock,
        {"station_id": "boat_001"},
        station_private_key,
        server_private_key.public_key(),
        remote_addr,
    )

    assert confirmed_session is not None
    out = capsys.readouterr().out
    assert "Invalid handshake response format" not in out
    assert "Mutual ECDHE session confirmed." in out
    # Fixed handshake deadline / retries / pinning are all untouched: the
    # skip is a plain `continue`, not a socket drain or timeout change.
    assert sock.timeout == 5.0


def test_g8_malformed_serverhello_shaped_packet_still_warns(
    proxy, monkeypatch, capsys
):
    remote_addr = ("192.0.2.10", 17777)
    station_private_key, server_private_key, confirming_server = _handshake_fixture(
        proxy, remote_addr
    )
    # Starts with the ServerHello wire prefix but is otherwise malformed --
    # must still hit full parsing and the existing warning/fail-closed path.
    malformed_serverhello = p.SERVER_HELLO_PREFIX + b"|not-a-real-server-hello"

    sock = _FakeHandshakeSocket((
        (malformed_serverhello, remote_addr),
        confirming_server.server_hello_response,
        confirming_server.confirmation_pong_response,
    ))
    monkeypatch.setattr(proxy.time, "time", lambda: 1000)

    confirmed_session = proxy.perform_handshake(
        sock,
        {"station_id": "boat_001"},
        station_private_key,
        server_private_key.public_key(),
        remote_addr,
    )

    assert confirmed_session is not None
    out = capsys.readouterr().out
    assert "Invalid handshake response format" in out
    assert "Mutual ECDHE session confirmed." in out


# ==========================================================================
# G9. Sparse client heartbeat: short locator label, epoch, real peer state,
#     IPv4 `ipv4:port` / IPv6 `ipv6.port` observed display, age, last-known,
#     unknown.
# ==========================================================================


def test_g9_heartbeat_displays_ipv4_endpoint_with_age(proxy, capsys):
    stats = proxy.ForwardingStats()
    stats.record_forwarded("!AIVDM,1,1,,A,x,0*00")
    proxy.print_forwarding_heartbeat(
        object(),
        proxy.UDPSEC_OUTPUT_TYPE,
        stats,
        session_up=True,
        session_locator=b"\x4e\x8a\x2c\x71" + b"\x00" * 12,
        epoch_generation=0,
        observed_endpoint=("203.0.113.9", 53142),
        observed_age=12.4,
    )
    out = capsys.readouterr().out
    assert "session=4e8a2c71" in out
    assert "epoch=0" in out
    assert "peer=alive" in out
    assert "observed=203.0.113.9:53142" in out
    assert "age=12s" in out
    assert "last-known" not in out


def test_g9_heartbeat_displays_ipv6_endpoint_last_known(proxy, capsys):
    stats = proxy.ForwardingStats()
    proxy.print_forwarding_heartbeat(
        object(),
        proxy.UDPSEC_OUTPUT_TYPE,
        stats,
        session_up=True,
        session_locator=b"\x4e\x8a\x2c\x71" + b"\x00" * 12,
        epoch_generation=1,
        observed_endpoint=("2001:db8::10", 53142),
        observed_age=55.0,
        observed_last_known=True,
    )
    out = capsys.readouterr().out
    # Exact ipv6.port rendering -- never bracketed, never bare `:port`.
    assert "observed=2001:db8::10.53142" in out
    assert "[2001:db8::10]" not in out
    assert "last-known" in out
    assert "age=55s" in out


def test_g9_heartbeat_displays_unknown_and_dead_peer(proxy, capsys):
    stats = proxy.ForwardingStats()
    proxy.print_forwarding_heartbeat(
        object(),
        proxy.UDPSEC_OUTPUT_TYPE,
        stats,
        session_up=False,
        session_locator=b"\x00" * 16,
        epoch_generation=0,
        observed_endpoint=None,
    )
    out = capsys.readouterr().out
    assert "peer=dead" in out
    assert "observed=unknown" in out


def test_g9_plain_udp_heartbeat_unchanged_no_session_suffix(proxy, capsys):
    # session_up=None (the plain-UDP call shape) must print no session
    # suffix at all -- exactly as before this feature.
    stats = proxy.ForwardingStats()
    proxy.print_forwarding_heartbeat(object(), proxy.UDP_OUTPUT_TYPE, stats)
    out = capsys.readouterr().out
    assert "session=" not in out
    assert "peer=" not in out
    assert "observed=" not in out


# ==========================================================================
# G10. Sparse server migration-lifecycle logs: OPENED/REPLACED/EXPIRED/
#     COMMITTED emitted exactly once per real transition, no secrets.
# ==========================================================================


def test_g10_migration_lifecycle_logs_emitted_once_no_secrets(env, capsys):
    state = env.new_state(path_candidate_ttl=5.0, retired_path_grace=1000.0)
    sess = env.install_session(state, addr=ADDR_A, now=1000.0)
    _server_diagnostics_output(state, capsys)  # discard install-time noise, if any

    sess.feed([(sess.nmea_packet("!AIVDM,1,1,,A,x,0*00"), ADDR_B)], 1000.0)
    out = _server_diagnostics_output(state, capsys)
    assert out.count("Path candidate OPENED") == 1
    assert "Path candidate REPLACED" not in out
    assert f"session={sess.locator.hex()[:8]}" in out
    assert "active=192.0.2.10:41000" in out
    assert "candidate=198.51.100.20:42000" in out
    assert "generation=1" in out

    sess.feed([(sess.nmea_packet("!AIVDM,1,1,,A,x,0*00"), ADDR_C)], 1000.0)
    out = _server_diagnostics_output(state, capsys)
    assert out.count("Path candidate REPLACED") == 1
    assert out.count("Path candidate OPENED") == 0
    assert "candidate=203.0.113.30:43000" in out
    assert "generation=2" in out

    state.cleanup_expired_path_transitions(1010.0)
    out = _server_diagnostics_output(state, capsys)
    assert out.count("Path candidate EXPIRED") == 1
    assert "candidate=203.0.113.30:43000" in out

    _challenge, _commit_socket = sess.migrate(ADDR_A, ADDR_B, now=2000.0)
    out = _server_diagnostics_output(state, capsys)
    assert out.count("Path migration COMMITTED") == 1
    assert "old=192.0.2.10:41000" in out
    assert "new=198.51.100.20:42000" in out

    for secret in (sess.c2s_key.hex(), sess.s2c_key.hex()):
        assert secret not in out


# ==========================================================================
# IMPORTANT TEST DESIGN: production-path end-to-end probe. Real client
# forward_loop() against the real server _secure_server_loop()/SecureState
# through the MP8 NAT/CGNAT shim: establish, observe diagnostics, migrate
# for real, observe post-migration diagnostics, and prove exact
# LogicalSession/CryptoEpoch/replay-ledger continuity throughout -- new
# diagnostics never decide liveness or force migration, and never regress
# across the real wire exchange.
# ==========================================================================


def test_e2e_production_path_diagnostics_survive_real_migration(
    env, proxy, keys, monkeypatch
):
    state = env.new_state(path_candidate_ttl=1000.0, retired_path_grace=1000.0)
    server_sess = _install_matching_session(env, state, keys, addr=ADDR_A, now=1000.0)
    session_obj = server_sess.session
    before = _identity_fields(session_obj)

    clock = _FakeClock(1000.0)
    shim = _MobileNetworkShim(server_sess, clock)
    shim.external_addr = ADDR_A
    pong_diagnostics = []
    events = {"flipped": False}

    def on_client_message(message, _clk, shim_):
        # Flip on the SECOND ping, not the first: the first keepalive
        # ping (seq=1) must still land on the original path A so an
        # ordinary pre-migration (generation 0) PONG is actually observed
        # before the NAT remap.
        if (
            not events["flipped"]
            and message
            and message.get("type") == "ping"
            and message.get("seq") == 2
        ):
            events["flipped"] = True
            shim_.external_addr = ADDR_B

    def on_server_message(message, _clk, _shim_):
        if message and message.get("type") == "pong":
            pong_diagnostics.append(
                (
                    message.get("observed_endpoint"),
                    message.get("active_path_generation"),
                )
            )
        return False

    shim.on_client_message = on_client_message
    shim.on_server_message = on_server_message
    # Decoupled from message content on purpose: stop the scenario at a
    # fixed later time, well after both the migration commit and at least
    # one further ordinary keepalive round, so a genuine post-migration
    # PONG is actually captured (not just the ACK).
    shim.schedule(1025.0, lambda: setattr(shim, "cutoff", True))

    reason, _ended_at = run_mobile_client(
        proxy,
        keys,
        monkeypatch,
        shim,
        config_overrides={
            "keepalive_interval": 5,
            "peer_timeout": 60,
            "session_refresh_interval": 0,
        },
    )

    assert events["flipped"], "NAT remap never triggered"
    assert reason == proxy.SESSION_END_PROACTIVE_REKEY

    # Exact same LogicalSession/CryptoEpoch/replay-ledger identity
    # throughout -- diagnostics work never mints, replaces, or rekeys any
    # of it.
    after = _identity_fields(session_obj)
    for k in before:
        assert after[k] == before[k], f"{k} changed: {before[k]!r} -> {after[k]!r}"

    snap = state.path_state_snapshot(session_obj, clock.now)
    assert snap["active"] == ADDR_B
    assert snap["active_path_generation"] == 1

    assert pong_diagnostics, "no PONGs observed on the wire"
    pre_migration = [(ep, gen) for ep, gen in pong_diagnostics if gen == 0]
    post_migration = [(ep, gen) for ep, gen in pong_diagnostics if gen and gen >= 1]
    assert pre_migration, "expected at least one pre-migration (generation 0) PONG"
    assert post_migration, "expected at least one post-migration PONG"
    for endpoint, _gen in pre_migration:
        assert endpoint == {"ip": "192.0.2.10", "port": 41000}
    for endpoint, gen in post_migration:
        assert endpoint == {"ip": "198.51.100.20", "port": 42000}
        assert gen == 1

    # The real wire sequence of active_path_generation values is
    # monotonic -- no observed PONG ever reported a generation lower than
    # an earlier one (this is the server's own authoritative label; the
    # client-side rejection of a late/stale value is proven directly by
    # the G6 _ClientObservedEndpoint unit tests above).
    generations = [gen for _ep, gen in pong_diagnostics]
    assert generations == sorted(generations)


# ==========================================================================
# CORRECTIVE ROUND (independent Astra audit, F1/F2/F3): targeted regressions
# for the three reported defects, added rather than rewriting the G1-G11
# assertions above. See aismixer_secure.py's `_process_candidate_path_
# observation`/`open_or_replace_candidate_path`/`drain_and_log_path_expiry_
# events`/`_expire_session_path_state`, and nmea_sproxy.py's
# `_ClientObservedEndpoint.observe_pong`.
# ==========================================================================


class _CountingValuesDict(OrderedDict):
    """A faithful `OrderedDict` (production's `SecureState._sessions` type --
    `touch_session` relies on `move_to_end`) that counts every aggregate
    access path: `.values()`, `.items()`, `.keys()` and direct iteration.
    Used to prove the per-packet diagnostic path performs zero aggregate
    session scans (F1). An earlier plain-`dict` version lacked
    `move_to_end`, so packet processing could fail and be swallowed by the
    server loop, making "zero scans" pass vacuously -- every probe using
    this class must therefore ALSO assert a real packet outcome.

    Only accesses made directly by production code (`aismixer_secure.py`)
    are counted: the test harness itself copies `_sessions` with `dict()`
    (which goes through `keys()` for an `OrderedDict` subclass) and must
    not be mistaken for a production scan. `_assert_counter_is_live`
    proves the counter does see real production scans."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.values_calls = 0

    def _note(self):
        if sys._getframe(2).f_code.co_filename.endswith("aismixer_secure.py"):
            self.values_calls += 1

    def values(self):
        self._note()
        return super().values()

    def items(self):
        self._note()
        return super().items()

    def keys(self):
        self._note()
        return super().keys()

    def __iter__(self):
        self._note()
        return super().__iter__()


def _assert_counter_is_live(state, counting_sessions):
    """Positive control: `SecureState.stats()` performs exactly four
    aggregate `_sessions.values()` scans, and the counter must see them."""
    before = counting_sessions.values_calls
    state.stats()
    assert counting_sessions.values_calls == before + 4


# --------------------------------------------------------------------------
# F1. O(1) candidate OPENED/REPLACED diagnostics -- no aggregate stats()
# scans, atomic classification straight from the locked decision.
# --------------------------------------------------------------------------


def test_f1_candidate_diagnostics_never_call_stats(env, monkeypatch, capsys):
    """`stats()` (four full `self._sessions.values()` scans) must never be
    reached from the candidate-observation diagnostic path, for a duplicate
    packet OR a genuine OPENED/REPLACED transition."""
    state = env.new_state(path_candidate_ttl=1000.0, retired_path_grace=1000.0)
    sess = env.install_session(state, addr=ADDR_A, now=1000.0)

    def _forbidden_stats():
        raise AssertionError(
            "stats() must not be called from the candidate diagnostic path"
        )

    monkeypatch.setattr(state, "stats", _forbidden_stats)

    # genuine OPENED
    sess.feed([(sess.nmea_packet("!AIVDM,1,1,,A,x,0*00"), ADDR_B)], 1000.0)
    # duplicate traffic on the SAME candidate, same current epoch
    sess.feed([(sess.nmea_packet("!AIVDM,1,1,,A,y,0*00"), ADDR_B)], 1000.0)
    # genuine REPLACED
    sess.feed([(sess.nmea_packet("!AIVDM,1,1,,A,z,0*00"), ADDR_C)], 1000.0)

    out = _server_diagnostics_output(state, capsys)
    assert out.count("Path candidate OPENED") == 1
    assert out.count("Path candidate REPLACED") == 1


def test_f1_no_aggregate_session_iteration_with_multiple_live_sessions(env):
    """Astra reproduced 4 scans / 12 session visits for ONE duplicate
    packet with 3 live sessions. With the fix, neither a duplicate nor a
    genuine transition may touch `self._sessions.values()` at all --
    proven directly by instrumenting the dict, not merely stats()."""
    state = env.new_state(path_candidate_ttl=1000.0, retired_path_grace=1000.0)
    sess1 = env.install_session(state, addr=("192.0.2.101", 21000), now=1000.0)
    env.install_session(state, addr=("192.0.2.102", 22000), now=1000.0)
    env.install_session(state, addr=("192.0.2.103", 23000), now=1000.0)

    counting_sessions = _CountingValuesDict(state._sessions)
    state._sessions = counting_sessions
    diag = state.migration_diagnostics
    published_before = diag.stats().published

    # Genuine OPENED: exactly one PATH_CHALLENGE, to B.
    open_socket = sess1.feed(
        [(sess1.nmea_packet("!AIVDM,1,1,,A,x,0*00"), ADDR_B)], 1000.0
    )
    assert counting_sessions.values_calls == 0
    opened = sess1.sole_challenge(open_socket, expect_addr=ADDR_B)
    candidate = sess1.session.path_state.candidate_path
    assert candidate is not None and candidate.sockaddr == ADDR_B

    # Duplicate: no datagram, the exact same candidate object/token/gen.
    duplicate_socket = sess1.feed(
        [(sess1.nmea_packet("!AIVDM,1,1,,A,y,0*00"), ADDR_B)], 1000.0
    )
    assert counting_sessions.values_calls == 0
    assert duplicate_socket.sent == []
    assert sess1.session.path_state.candidate_path is candidate
    assert candidate.challenge_token == opened.challenge_token

    # Genuine REPLACED: exactly one PATH_CHALLENGE, to C, next generation.
    replace_socket = sess1.feed(
        [(sess1.nmea_packet("!AIVDM,1,1,,A,z,0*00"), ADDR_C)], 1000.0
    )
    assert counting_sessions.values_calls == 0
    replaced = sess1.sole_challenge(replace_socket, expect_addr=ADDR_C)
    assert replaced.path_generation == opened.path_generation + 1
    assert sess1.session.path_state.candidate_path.sockaddr == ADDR_C

    stats = diag.stats()
    assert stats.published == published_before + 2  # OPENED + REPLACED
    _assert_counter_is_live(state, counting_sessions)


def test_f1_opened_duplicate_replaced_sequence_logs_exactly_once_each(
    env, capsys
):
    state = env.new_state(path_candidate_ttl=1000.0, retired_path_grace=1000.0)
    sess = env.install_session(state, addr=ADDR_A, now=1000.0)

    open_socket = sess.feed(
        [(sess.nmea_packet("!AIVDM,1,1,,A,x,0*00"), ADDR_B)], 1000.0
    )
    out = _server_diagnostics_output(state, capsys)
    assert out.count("Path candidate OPENED") == 1
    challenges_after_open = sum(
        1
        for d, _a in open_socket.sent
        if sess._is_type(d, p.PATH_CHALLENGE_TYPE, 0)
    )
    assert challenges_after_open == 1

    duplicate_socket = sess.feed(
        [(sess.nmea_packet("!AIVDM,1,1,,A,y,0*00"), ADDR_B)], 1000.0
    )
    out = _server_diagnostics_output(state, capsys)
    assert "Path candidate" not in out
    assert duplicate_socket.sent == []

    replace_socket = sess.feed(
        [(sess.nmea_packet("!AIVDM,1,1,,A,z,0*00"), ADDR_C)], 1000.0
    )
    out = _server_diagnostics_output(state, capsys)
    assert out.count("Path candidate REPLACED") == 1
    assert out.count("Path candidate OPENED") == 0
    challenges_after_replace = sum(
        1
        for d, _a in replace_socket.sent
        if sess._is_type(d, p.PATH_CHALLENGE_TYPE, 0)
    )
    assert challenges_after_replace == 1


def test_f1_independent_sessions_are_never_cross_attributed(env, capsys):
    """Two live sessions on one owner, interleaved: each session's OPENED/
    REPLACED must carry its OWN station/locator/address data -- classified
    straight from that exact call's locked decision, never inferred from a
    shared mutable counter that could race across sessions/listeners."""
    state = env.new_state(path_candidate_ttl=1000.0, retired_path_grace=1000.0)
    sess1 = env.install_session(
        state, addr=("192.0.2.111", 31000), now=1000.0, station_id="boat_001",
    )
    sess2 = env.install_session(
        state, addr=("192.0.2.112", 32000), now=1000.0, station_id="boat_001",
    )

    sess1.feed(
        [(sess1.nmea_packet("!AIVDM,1,1,,A,a,0*00"), ("198.51.100.201", 41000))],
        1000.0,
    )
    sess2.feed(
        [(sess2.nmea_packet("!AIVDM,1,1,,A,b,0*00"), ("198.51.100.202", 42000))],
        1000.0,
    )
    out = _server_diagnostics_output(state, capsys)
    assert out.count("Path candidate OPENED") == 2
    assert f"session={sess1.locator.hex()[:8]}" in out
    assert f"session={sess2.locator.hex()[:8]}" in out
    assert "candidate=198.51.100.201:41000" in out
    assert "candidate=198.51.100.202:42000" in out

    # sess1 gets a genuine REPLACED; sess2 gets only a duplicate. The
    # labels must not cross over.
    sess1.feed(
        [(sess1.nmea_packet("!AIVDM,1,1,,A,c,0*00"), ("198.51.100.203", 43000))],
        1000.0,
    )
    sess2.feed(
        [(sess2.nmea_packet("!AIVDM,1,1,,A,d,0*00"), ("198.51.100.202", 42000))],
        1000.0,
    )
    out = _server_diagnostics_output(state, capsys)
    assert out.count("Path candidate REPLACED") == 1
    assert "Path candidate OPENED" not in out
    assert f"session={sess1.locator.hex()[:8]}" in out
    assert "candidate=198.51.100.203:43000" in out


# --------------------------------------------------------------------------
# F2. Lazy packet-path candidate/retired-path expiry must be logged exactly
# once, regardless of which call site physically discards the record, with
# no duplicate from a later maintenance sweep (or vice versa).
# --------------------------------------------------------------------------


def test_f2_lazy_candidate_expiry_via_real_active_ping_logs_once(env, capsys):
    """Astra's own reproduction: a REAL encrypted active-path PING (not
    helper-level seeded state) causes lazy expiry of an unrelated
    off-path candidate. The ordinary PONG must still be sent, and the
    EXPIRED event -- previously invisible -- must now be logged exactly
    once."""
    state = env.new_state(path_candidate_ttl=5.0, retired_path_grace=1000.0)
    sess = env.install_session(state, addr=ADDR_A, now=1000.0)

    sess.feed([(sess.nmea_packet("!AIVDM,1,1,,A,x,0*00"), ADDR_B)], 1000.0)
    _server_diagnostics_output(state, capsys)  # discard OPENED noise

    pong_socket = sess.feed([(sess.ping_packet(1), ADDR_A)], 1010.0)
    out = _server_diagnostics_output(state, capsys)
    assert out.count("Path candidate EXPIRED") == 1
    assert "candidate=198.51.100.20:42000" in out
    assert any(sess._is_type(d, "pong", 0) for d, _a in pong_socket.sent)

    # a later maintenance sweep must not re-log the same, already-gone
    # candidate.
    state.cleanup_expired_path_transitions(1020.0)
    out2 = _server_diagnostics_output(state, capsys)
    assert "EXPIRED" not in out2


def test_f2_lazy_retired_path_expiry_via_real_packet_logs_once(env, capsys):
    state = env.new_state(path_candidate_ttl=1000.0, retired_path_grace=5.0)
    sess = env.install_session(state, addr=ADDR_A, now=1000.0)
    sess.migrate(ADDR_A, ADDR_B, now=1000.0)
    _server_diagnostics_output(state, capsys)  # discard OPENED/COMMITTED noise

    pong_socket = sess.feed([(sess.ping_packet(1), ADDR_B)], 1010.0)
    out = _server_diagnostics_output(state, capsys)
    assert out.count("Retired path EXPIRED") == 1
    assert "retired=192.0.2.10:41000" in out
    assert any(sess._is_type(d, "pong", 0) for d, _a in pong_socket.sent)

    state.cleanup_expired_path_transitions(1020.0)
    out2 = _server_diagnostics_output(state, capsys)
    assert "EXPIRED" not in out2


def test_f2_maintenance_first_expiry_then_packet_logs_no_duplicate(env, capsys):
    state = env.new_state(path_candidate_ttl=5.0, retired_path_grace=1000.0)
    sess = env.install_session(state, addr=ADDR_A, now=1000.0)
    sess.feed([(sess.nmea_packet("!AIVDM,1,1,,A,x,0*00"), ADDR_B)], 1000.0)
    _server_diagnostics_output(state, capsys)

    state.cleanup_expired_path_transitions(1010.0)
    out = _server_diagnostics_output(state, capsys)
    assert out.count("Path candidate EXPIRED") == 1

    pong_socket = sess.feed([(sess.ping_packet(1), ADDR_A)], 1011.0)
    out2 = _server_diagnostics_output(state, capsys)
    assert "EXPIRED" not in out2
    assert any(sess._is_type(d, "pong", 0) for d, _a in pong_socket.sent)


def test_f2_expiry_boundary_semantics_unchanged(env):
    """`now >= deadline` is still the exact expiry boundary -- the event
    queue is purely observational and must not perturb it."""
    state = env.new_state(path_candidate_ttl=5.0, retired_path_grace=1000.0)
    sess = env.install_session(state, addr=ADDR_A, now=1000.0)
    sess.feed([(sess.nmea_packet("!AIVDM,1,1,,A,x,0*00"), ADDR_B)], 1000.0)
    deadline = state.path_state_snapshot(sess.session, 1000.0)["candidate"][
        "deadline"
    ]
    assert deadline == 1005.0

    snap_before = state.path_state_snapshot(sess.session, deadline - 0.001)
    assert snap_before["candidate"] is not None

    snap_at = state.path_state_snapshot(sess.session, deadline)
    assert snap_at["candidate"] is None


def test_f2_hot_paths_stay_o1_even_when_expiry_actually_fires(env):
    """Multiple live sessions share the owner; a packet that ACTUALLY
    triggers lazy expiry must still perform zero aggregate iteration over
    `self._sessions`."""
    state = env.new_state(path_candidate_ttl=5.0, retired_path_grace=1000.0)
    sess = env.install_session(state, addr=ADDR_A, now=1000.0)
    env.install_session(state, addr=("192.0.2.121", 51000), now=1000.0)
    env.install_session(state, addr=("192.0.2.122", 52000), now=1000.0)

    sess.feed([(sess.nmea_packet("!AIVDM,1,1,,A,x,0*00"), ADDR_B)], 1000.0)

    counting_sessions = _CountingValuesDict(state._sessions)
    state._sessions = counting_sessions
    diag = state.migration_diagnostics
    published_before = diag.stats().published

    # this packet's processing lazily expires the candidate above.
    pong_socket = sess.feed([(sess.ping_packet(1), ADDR_A)], 1010.0)
    assert counting_sessions.values_calls == 0

    # ...and the packet genuinely succeeded: one PONG to the active path
    # that decrypts under the session keys, the candidate is gone, and
    # exactly one EXPIRED event was published.
    assert len(pong_socket.sent) == 1
    pong_packet, pong_addr = pong_socket.sent[0]
    assert pong_addr == ADDR_A
    pong = sess.decode_server_message(pong_packet)
    assert pong["type"] == "pong" and pong["seq"] == 1
    assert sess.session.path_state.candidate_path is None
    assert diag.stats().published == published_before + 1
    _assert_counter_is_live(state, counting_sessions)


# --------------------------------------------------------------------------
# F3. An unversioned PONG must never clear a 'last-known' marker (or
# refresh the display) once the diagnostic floor has advanced past zero.
# --------------------------------------------------------------------------


def test_f3_unversioned_pong_cannot_clear_last_known_after_floor_raised(proxy):
    """Byte-for-byte reproduction of the reported sequence: authenticated
    unversioned observation A, a validly (finally) admitted PATH_ACK at
    generation 1 (floor -> 1, A marked last-known), then ANOTHER
    authenticated unversioned observation of the SAME address A. Before
    the fix this incorrectly cleared last-known and refreshed the display
    as if A were current."""
    observed = proxy._ClientObservedEndpoint()

    observed.observe_pong(("203.0.113.9", 1), None, 100.0)
    assert observed.display(100.0) == (("203.0.113.9", 1), 0.0, False)

    observed.raise_floor(1)
    assert observed.display(150.0) == (("203.0.113.9", 1), 50.0, True)

    # the exact reported regression trigger.
    observed.observe_pong(("203.0.113.9", 1), None, 200.0)
    assert observed.display(200.0) == (("203.0.113.9", 1), 100.0, True)

    # a genuinely later unversioned observation of a DIFFERENT address is
    # rejected identically -- floor>0 blocks any unversioned admission,
    # not merely a same-address one.
    observed.observe_pong(("198.51.100.1", 9), None, 250.0)
    assert observed.display(250.0) == (("203.0.113.9", 1), 150.0, True)

    # only a valid versioned observation at/above the floor can clear it.
    observed.observe_pong(("198.51.100.1", 2), 1, 260.0)
    assert observed.display(260.0) == (("198.51.100.1", 2), 0.0, False)


def test_f3_unversioned_still_fills_unknown_display_while_floor_is_zero(proxy):
    """Regression guard for the OTHER half of the fix: old-server
    interoperability while nothing generation-aware has ever been
    accepted and no migration has ever committed (floor == 0)."""
    observed = proxy._ClientObservedEndpoint()
    assert observed.display(0.0) == (None, None, False)

    observed.observe_pong(("203.0.113.9", 1), None, 100.0)
    assert observed.display(100.0) == (("203.0.113.9", 1), 0.0, False)

    # a later unversioned refresh of the SAME address is still fine while
    # the floor is still zero and nothing generation-aware exists yet.
    observed.observe_pong(("203.0.113.9", 1), None, 120.0)
    assert observed.display(120.0) == (("203.0.113.9", 1), 0.0, False)


def test_f3_rejected_or_stale_ack_cannot_raise_floor(proxy):
    observed = proxy._ClientObservedEndpoint()
    observed.observe_pong(("203.0.113.9", 1), None, 100.0)

    # None (no matched proof -- e.g. a forged/expired/unmatched ACK, as
    # `_extract_authenticated_ack_generation`/`on_ack` yield for anything
    # that isn't a freshly, finally admitted match) must never raise the
    # floor.
    observed.raise_floor(None)
    assert observed.display(100.0) == (("203.0.113.9", 1), 0.0, False)

    # a subsequent unversioned observation is therefore still accepted --
    # proving the floor genuinely never moved.
    observed.observe_pong(("203.0.113.9", 1), None, 130.0)
    assert observed.display(130.0) == (("203.0.113.9", 1), 0.0, False)


def test_f3_forward_loop_real_migration_then_stale_unversioned_pong(
    env, proxy, keys, monkeypatch, capsys
):
    """Real `forward_loop()` reproduction: an authenticated unversioned
    PONG establishes A; a REAL server-driven migration (genuine
    candidate/challenge/response/commit/ACK, all real crypto) finally
    admits the PATH_ACK and raises the diagnostic floor; a later
    hand-crafted authenticated unversioned PONG re-claiming stale address
    A must never clear 'last-known' or silently present A as current
    again. Only the ORDINARY PONG replies (never the migration
    choreography itself) are substituted, to independently exercise this
    exact defect against real production code."""
    state = env.new_state(path_candidate_ttl=1000.0, retired_path_grace=1000.0)
    server_sess = _install_matching_session(env, state, keys, addr=ADDR_A, now=1000.0)

    clock = _FakeClock(1000.0)
    shim = _MobileNetworkShim(server_sess, clock)
    shim.external_addr = ADDR_A
    events = {"flipped": False, "acked": False}

    def _unversioned_pong_claiming_a(seq):
        message = p.build_pong_message(STATION_ID, seq, 1_000_000)
        message["observed_endpoint"] = {"ip": "192.0.2.10", "port": 41000}
        return proxy.encrypt_secure_json_message(
            message, keys["s2c"], keys["locator"], 0
        )

    def on_client_message(message, _clk, shim_):
        if (
            not events["flipped"]
            and message
            and message.get("type") == "ping"
            and message.get("seq") == 2
        ):
            events["flipped"] = True
            shim_.external_addr = ADDR_B

    def on_server_message(message, _clk, shim_):
        if message and message.get("type") == p.PATH_ACK_TYPE:
            events["acked"] = True
            return False
        if message and message.get("type") == "pong":
            seq = message.get("seq")
            if seq in (1, 3):
                # substitute the server's real (versioned) ordinary pong
                # with a hand-crafted UNVERSIONED one -- the migration
                # choreography (challenge/response/ack) is entirely real
                # and untouched.
                shim_.to_client.append(_unversioned_pong_claiming_a(seq))
                return True
        return False

    shim.on_client_message = on_client_message
    shim.on_server_message = on_server_message
    shim.schedule(1022.0, lambda: setattr(shim, "cutoff", True))

    # Force frequent heartbeats so the diagnostic display is observable
    # without needing to introspect forward_loop's local state.
    monkeypatch.setattr(proxy, "HEARTBEAT_INTERVAL_SECONDS", 3.0)

    reason, _ended_at = run_mobile_client(
        proxy,
        keys,
        monkeypatch,
        shim,
        config_overrides={
            "keepalive_interval": 5,
            "peer_timeout": 60,
            "session_refresh_interval": 0,
        },
    )

    assert events["flipped"], "NAT remap never triggered"
    assert events["acked"], "migration was never acknowledged end-to-end"
    assert reason == proxy.SESSION_END_PROACTIVE_REKEY

    runtime_lines = [
        line
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("Runtime:")
    ]
    assert len(runtime_lines) >= 2, "expected several heartbeats during the run"

    stale_a_current = [
        line
        for line in runtime_lines
        if "observed=192.0.2.10:41000" in line and "last-known" not in line
    ]
    stale_a_last_known = [
        line
        for line in runtime_lines
        if "observed=192.0.2.10:41000" in line and "last-known" in line
    ]
    # Before the migration ACK: the unversioned PONG for A filled the
    # previously-unknown display as CURRENT (floor was still 0).
    assert stale_a_current, "expected an early heartbeat showing A as current"
    # After the ACK finally admits and raises the floor: A must be shown
    # last-known, and the LATER hand-crafted unversioned PONG re-claiming
    # A must never clear that marker or refresh it back to current.
    assert stale_a_last_known, "expected a later heartbeat marking A last-known"

    # Monotonic staleness: once last-known is first observed, no LATER
    # heartbeat may ever show A as current again -- the exact regression
    # this correction closes.
    first_last_known_index = runtime_lines.index(stale_a_last_known[0])
    for line in runtime_lines[first_last_known_index:]:
        if "observed=192.0.2.10:41000" in line:
            assert "last-known" in line, (
                "a later heartbeat presented stale address A as current "
                f"again: {line!r}"
            )

    # PG: A only ever came from UNVERSIONED PONGs, so its generation is
    # unknown on every line -- in particular never relabelled with the
    # admitted PATH_ACK's floor (1) once it became last-known.
    for line in runtime_lines:
        if "observed=192.0.2.10:41000" in line:
            assert "observed=192.0.2.10:41000 path_gen=unknown" in line, line


# ==========================================================================
# F2 FINAL CORRECTIVE ROUND (Astra closure audit F2-A / F2-B). Server
# migration diagnostics are an O(1) bounded, nonblocking enqueue inside the
# exact locked transition, delivered by one off-event-loop consumer thread
# per owner. A slow, broken, or backpressured sink must never delay, raise
# into, or suppress a packet response, nor change a candidate's final
# challenge-send eligibility.
# ==========================================================================

_DIAG_THREAD_NAME = "udpsec-migration-diagnostics"
ADDR_C2 = ("203.0.113.31", 43001)


def _diag_threads():
    return [t for t in threading.enumerate() if t.name == _DIAG_THREAD_NAME]


class _ScriptedSink:
    """Deterministic stand-in for stdout: records attempts; can fail the
    first N attempts or every attempt, block until released, advance a
    shared fake monotonic clock (simulated slow output), and detect being
    called while the owner's authority lock is held by the writing
    thread."""

    def __init__(
        self,
        *,
        fail_first=0,
        fail_forever=False,
        exc=OSError,
        block=None,
        clock=None,
        advance=0.0,
        forbid_lock=None,
    ):
        self.attempts = []
        self.lines = []
        self.fail_first = fail_first
        self.fail_forever = fail_forever
        self.exc = exc
        self.block = block
        self.entered = threading.Event()
        self.clock = clock
        self.advance = advance
        self.forbid_lock = forbid_lock
        self.lock_violations = 0

    def __call__(self, line):
        self.attempts.append(line)
        self.entered.set()
        if self.forbid_lock is not None and self.forbid_lock._is_owned():
            self.lock_violations += 1
        if self.clock is not None:
            self.clock.now += self.advance
        if self.block is not None:
            self.block.wait(10.0)
        if self.fail_forever or len(self.attempts) <= self.fail_first:
            raise self.exc("simulated diagnostic output failure")
        self.lines.append(line)


def _valid_event(i):
    return (
        "candidate_expired",
        STATION_ID,
        (0xB0000000 + i).to_bytes(4, "big") + b"\x00" * 12,
        ("198.51.100.1", 40000 + i),
        i + 1,
    )


def _accounting_balances(stats):
    return stats.published == (
        stats.delivered
        + stats.dropped_overflow
        + stats.undeliverable
        + stats.dropped_on_shutdown
        + stats.queued
        + stats.in_flight
    )


def _record_send_times(monkeypatch, clock):
    """Timestamp every server datagram with the shared fake monotonic
    clock at the instant `sendto` is called."""
    records = []
    original = _FakeSecureSocket.sendto

    def sendto(self, data, addr):
        records.append((clock.now, data, addr))
        return original(self, data, addr)

    monkeypatch.setattr(_FakeSecureSocket, "sendto", sendto)
    return records


def _migrated_session_with_expiring_candidate_and_retired(env):
    """Active B; retired A (grace deadline 1005.0); live candidate C
    (deadline 1006.0). The next packet at >= 1006 lazily expires both."""
    state = env.new_state(path_candidate_ttl=5.0, retired_path_grace=5.0)
    sess = env.install_session(state, addr=ADDR_A, now=1000.0)
    sess.migrate(ADDR_A, ADDR_B, now=1000.0)
    sess.feed([(sess.nmea_packet("!AIVDM,1,1,,A,c,0*00"), ADDR_C)], 1001.0)
    snap = state.path_state_snapshot(sess.session, 1001.0)
    assert snap["active"] == ADDR_B
    assert snap["retired"]["deadline"] == 1005.0
    assert snap["candidate"]["deadline"] == 1006.0
    # deliver the setup OPENED/COMMITTED/OPENED events out of the way.
    state.migration_diagnostics.sink = _ScriptedSink()
    state.migration_diagnostics.flush()
    return state, sess


# --------------------------------------------------------------------------
# 1. F2-A: controlled output failure on a real encrypted active-path PING
#    that lazily expires BOTH a candidate and a retired path.
# --------------------------------------------------------------------------


def test_f2a_output_failure_cannot_suppress_pong_or_lose_second_expiry(env):
    state, sess = _migrated_session_with_expiring_candidate_and_retired(env)
    diag = state.migration_diagnostics
    sink = _ScriptedSink(fail_first=1)
    diag.sink = sink
    before = diag.stats()

    pong_socket = sess.feed([(sess.ping_packet(1), ADDR_B)], 1010.0)

    # The authenticated PONG went out, to the active path, under the
    # correct key/locator/epoch -- and the sink was never touched on the
    # packet path at all.
    assert len(pong_socket.sent) == 1
    pong_packet, pong_addr = pong_socket.sent[0]
    assert pong_addr == ADDR_B
    pong = sess.decode_server_message(pong_packet)
    assert pong["type"] == "pong" and pong["seq"] == 1
    assert sink.attempts == []
    mid = diag.stats()
    assert mid.published == before.published + 2
    assert mid.queued == 2

    diag.flush()
    # The first expiry's write raised once and was retried; the second
    # expiry was handled independently, not lost with the first.
    assert len(sink.attempts) == 3
    assert sink.attempts[0] == sink.attempts[1]
    assert len(sink.lines) == 2
    assert sink.lines[0].startswith("[+] Path candidate EXPIRED")
    assert "candidate=203.0.113.30:43000" in sink.lines[0]
    assert sink.lines[1].startswith("[+] Retired path EXPIRED")
    assert "retired=192.0.2.10:41000" in sink.lines[1]
    after = diag.stats()
    assert after.delivered == before.delivered + 2
    assert after.write_errors == before.write_errors + 1
    assert after.undeliverable == before.undeliverable
    assert after.queued == 0
    assert _accounting_balances(after)

    # Physically removed records cannot be regenerated by maintenance.
    state.cleanup_expired_path_transitions(1020.0)
    diag.flush()
    assert diag.stats().published == after.published
    assert len(sink.lines) == 2

    # And the following PING still works.
    next_socket = sess.feed([(sess.ping_packet(2), ADDR_B)], 1021.0)
    assert len(next_socket.sent) == 1


def test_f2a_consumer_thread_survives_persistent_output_failure(env):
    state, sess = _migrated_session_with_expiring_candidate_and_retired(env)
    diag = state.migration_diagnostics
    attempts = env.secure.MIGRATION_DIAGNOSTIC_WRITE_ATTEMPTS
    sink = _ScriptedSink(fail_forever=True, exc=BrokenPipeError)
    diag.sink = sink
    before = diag.stats()
    assert diag.start()
    try:
        pong_socket = sess.feed([(sess.ping_packet(1), ADDR_B)], 1010.0)
        assert len(pong_socket.sent) == 1
        assert sess.decode_server_message(pong_socket.sent[0][0])["type"] == "pong"
        assert diag.wait_idle(5.0)
        broken = diag.stats()
        assert broken.consumer_running
        assert broken.undeliverable == before.undeliverable + 2
        assert broken.write_errors == before.write_errors + 2 * attempts
        assert len(sink.attempts) == 2 * attempts  # bounded retries, no loop

        # Output recovers: the next genuine transition is delivered by the
        # same, still-running consumer.
        sink.fail_forever = False
        sess.feed([(sess.nmea_packet("!AIVDM,1,1,,A,d,0*00"), ADDR_C2)], 1011.0)
        assert diag.wait_idle(5.0)
        assert any(
            line.startswith("[+] Path candidate OPENED") for line in sink.lines
        )
        assert diag.stats().consumer_running
    finally:
        assert diag.stop()
    final = diag.stats()
    assert not final.consumer_running
    assert _accounting_balances(final)


# --------------------------------------------------------------------------
# 2. F2-B: slow diagnostic output can never sit between the final
#    candidate revalidation and the PATH_CHALLENGE send.
# --------------------------------------------------------------------------


def test_f2b_slow_output_cannot_delay_challenge_send(env, monkeypatch):
    state = env.new_state(path_candidate_ttl=4.9, retired_path_grace=2.0)
    sess = env.install_session(state, addr=ADDR_A, now=1000.0)
    sess.migrate(ADDR_A, ADDR_B, now=1000.0)  # retired A deadline 1002.0
    diag = state.migration_diagnostics
    diag.sink = _ScriptedSink()
    diag.flush()

    clock = _FakeClock(1002.0)
    slow = _ScriptedSink(clock=clock, advance=5.0)  # every write "takes" 5 s
    diag.sink = slow
    sends = _record_send_times(monkeypatch, clock)
    challenges_before = state.stats().migration_challenges_sent

    # OPENED (+ the retired path's lazy EXPIRED in the SAME packet), then
    # REPLACED -- each must send its challenge at 1002, well before the
    # 1006.9 candidate deadline, with zero output on the packet path.
    sess.feed([(sess.nmea_packet("!AIVDM,1,1,,A,x,0*00"), ADDR_C)], 1002.0, clock=clock)
    sess.feed([(sess.nmea_packet("!AIVDM,1,1,,A,y,0*00"), ADDR_C2)], 1002.0, clock=clock)

    challenges = [
        (sent_at, addr)
        for sent_at, data, addr in sends
        if sess._is_type(data, p.PATH_CHALLENGE_TYPE, 0)
    ]
    assert challenges == [(1002.0, ADDR_C), (1002.0, ADDR_C2)]
    assert slow.attempts == []
    assert clock.now == 1002.0
    assert state.stats().migration_challenges_sent == challenges_before + 2
    # no implicit TTL extension
    snap = state.path_state_snapshot(sess.session, 1002.0)
    assert snap["candidate"]["deadline"] == 1002.0 + 4.9

    diag.flush()  # output happens only now, after both sends
    assert [line.split(" for ")[0] for line in slow.lines] == [
        "[+] Retired path EXPIRED",
        "[+] Path candidate OPENED",
        "[+] Path candidate REPLACED",
    ]


class _SlowEncryptOnce:
    """Wraps an AESGCM so its FIRST encrypt advances a fake clock -- a
    delay injected BEFORE the final challenge revalidation."""

    def __init__(self, real, clock, advance_to):
        self._real = real
        self._clock = clock
        self._advance_to = advance_to
        self._fired = False

    def encrypt(self, *args, **kwargs):
        if not self._fired:
            self._fired = True
            self._clock.now = max(self._clock.now, self._advance_to)
        return self._real.encrypt(*args, **kwargs)

    def decrypt(self, *args, **kwargs):
        return self._real.decrypt(*args, **kwargs)


def test_f2b_delay_before_final_validation_suppresses_expired_challenge(
    env, monkeypatch
):
    state = env.new_state(path_candidate_ttl=4.9, retired_path_grace=1000.0)
    sess = env.install_session(state, addr=ADDR_A, now=1000.0)
    diag = state.migration_diagnostics
    clock = _FakeClock(1002.0)
    slow = _ScriptedSink(clock=clock, advance=5.0)
    diag.sink = slow
    sends = _record_send_times(monkeypatch, clock)
    challenges_before = state.stats().migration_challenges_sent

    epoch = sess.session.current_epoch
    real = epoch.server_to_client_aesgcm
    epoch.server_to_client_aesgcm = _SlowEncryptOnce(real, clock, 1007.0)
    try:
        sess.feed(
            [(sess.nmea_packet("!AIVDM,1,1,,A,x,0*00"), ADDR_B)], 1002.0, clock=clock
        )
    finally:
        epoch.server_to_client_aesgcm = real

    # The final revalidation observed 1007 >= 1006.9: no challenge, no
    # datagram of any kind, no TTL extension, and still no output.
    assert sends == []
    assert state.stats().migration_challenges_sent == challenges_before
    assert slow.attempts == []
    assert state.path_state_snapshot(sess.session, 1007.0)["candidate"] is None

    diag.flush()
    assert [line.split(" for ")[0] for line in slow.lines] == [
        "[+] Path candidate OPENED",
        "[+] Path candidate EXPIRED",
    ]


# --------------------------------------------------------------------------
# 3. Full output failure and hard boundedness.
# --------------------------------------------------------------------------


def test_f2_queue_is_hard_bounded_drop_oldest_and_packets_flow_while_stalled(env):
    secure = env.secure
    capacity = secure.MIGRATION_DIAGNOSTIC_QUEUE_CAPACITY
    attempts = secure.MIGRATION_DIAGNOSTIC_WRITE_ATTEMPTS
    state = env.new_state(path_candidate_ttl=5.0, retired_path_grace=1000.0)
    diag = state.migration_diagnostics
    threads_before = len(_diag_threads())

    count = 200
    sessions = [
        env.install_session(
            state, addr=(f"192.0.2.{i % 250 + 1}", 20000 + i), now=1000.0
        )
        for i in range(count)
    ]
    # Consumer deliberately never started: 200 genuine OPENED + 200
    # genuine EXPIRED (via the real maintenance sweep) = 400 events.
    for i, s in enumerate(sessions):
        _cand, outcome, replaced = state.open_or_replace_candidate_path(
            s.session,
            s.session.current_epoch,
            (f"198.51.100.{i % 250 + 1}", 40000 + i),
            os.urandom(p.PATH_CHALLENGE_TOKEN_BYTES),
            1000.0,
        )
        assert (outcome, replaced) == ("installed", False)
        assert diag.stats().queued <= capacity
    state.cleanup_expired_path_transitions(1010.0)

    full = diag.stats()
    assert full.published == 2 * count
    assert full.queued == capacity
    assert full.dropped_overflow == 2 * count - capacity
    assert _accounting_balances(full)
    assert len(_diag_threads()) == threads_before

    # A full queue with no consumer never blocks or suppresses packets.
    first = sessions[0]
    pong_socket = first.feed([(first.ping_packet(1), ("192.0.2.1", 20000))], 1011.0)
    assert len(pong_socket.sent) == 1

    # Drop-oldest: the retained events are the most recent ones -- all 200
    # EXPIRED plus the last 56 OPENED.
    recorder = _ScriptedSink()
    diag.sink = recorder
    diag.flush()
    assert len(recorder.lines) == capacity
    assert sum("EXPIRED" in line for line in recorder.lines) == count
    assert sum("OPENED" in line for line in recorder.lines) == capacity - count

    # Persistent failure: every event gets exactly `attempts` bounded writes,
    # then is counted undeliverable -- no retry backlog.
    failing = _ScriptedSink(fail_forever=True)
    diag.sink = failing
    for s in sessions[:10]:
        state.open_or_replace_candidate_path(
            s.session,
            s.session.current_epoch,
            ("203.0.113.200", s.session.path_state.active_path[1]),
            os.urandom(p.PATH_CHALLENGE_TOKEN_BYTES),
            1020.0,
        )
    before_fail = diag.stats()
    diag.flush()
    failed = diag.stats()
    assert failed.undeliverable == before_fail.undeliverable + 10
    assert len(failing.attempts) == 10 * attempts
    assert failed.queued == 0

    # Recovery once the sink works again.
    healthy = _ScriptedSink()
    diag.sink = healthy
    state.cleanup_expired_path_transitions(1030.0)
    diag.flush()
    assert len(healthy.lines) == 10
    assert all("Path candidate EXPIRED" in line for line in healthy.lines)
    assert _accounting_balances(diag.stats())
    assert len(_diag_threads()) == threads_before


def test_f2_concurrent_producers_never_exceed_hard_bound(env):
    capacity = env.secure.MIGRATION_DIAGNOSTIC_QUEUE_CAPACITY
    state = env.new_state(path_candidate_ttl=5.0, retired_path_grace=1000.0)
    diag = state.migration_diagnostics
    per_thread, nthreads = 100, 4
    groups = [
        [
            env.install_session(
                state, addr=(f"192.0.{k}.{i + 1}", 21000 + i), now=1000.0
            )
            for i in range(per_thread)
        ]
        for k in range(nthreads)
    ]
    barrier = threading.Barrier(nthreads)
    queued_seen = []
    errors = []

    def producer(k, group):
        try:
            barrier.wait()
            for i, s in enumerate(group):
                state.open_or_replace_candidate_path(
                    s.session,
                    s.session.current_epoch,
                    (f"198.51.{k}.{i + 1}", 46000 + i),
                    os.urandom(p.PATH_CHALLENGE_TOKEN_BYTES),
                    1000.0,
                )
                queued_seen.append(diag.stats().queued)
        except BaseException as exc:  # surfaced below
            errors.append(exc)

    threads = [
        threading.Thread(target=producer, args=(k, group))
        for k, group in enumerate(groups)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10.0)
    assert not errors
    state.cleanup_expired_path_transitions(1010.0)

    total = 2 * per_thread * nthreads
    stats = diag.stats()
    assert stats.published == total
    assert max(queued_seen) <= capacity
    assert stats.queued == capacity
    assert stats.dropped_overflow == total - capacity
    assert _accounting_balances(stats)


def test_f2_per_event_formatting_and_encoding_failures_are_contained(env):
    state = env.new_state()
    diag = state.migration_diagnostics
    # A malformed snapshot or unknown kind can only poison its own event.
    diag.publish(("candidate_expired", STATION_ID, b"\x01" * 16, None, 1))
    diag.publish(("no_such_kind", STATION_ID, b"\x01" * 16))
    diag.publish(_valid_event(3))
    diag.publish(_valid_event(4))

    def unicode_failure(message):
        return UnicodeEncodeError("ascii", message, 0, 1, "simulated")

    sink = _ScriptedSink(fail_first=1, exc=unicode_failure)
    diag.sink = sink
    diag.flush()

    stats = diag.stats()
    # 2 formatting failures + 1 non-OSError write failure (never retried);
    # the unrelated fourth event is still delivered.
    assert stats.undeliverable == 3
    assert stats.write_errors == 1
    assert stats.delivered == 1
    assert len(sink.attempts) == 2
    assert len(sink.lines) == 1 and "generation=5" in sink.lines[0]
    assert _accounting_balances(stats)


# --------------------------------------------------------------------------
# 4. Concurrency / ownership.
# --------------------------------------------------------------------------


def test_f2_concurrent_lazy_and_maintenance_expiry_create_each_event_once(env):
    state = env.new_state(path_candidate_ttl=5.0, retired_path_grace=1000.0)
    diag = state.migration_diagnostics
    count = 60
    sessions = []
    expected = {}
    for i in range(count):
        locator = (0xA0000000 + i).to_bytes(4, "big") + b"\x00" * 12
        s = env.install_session(
            state, addr=(f"192.0.2.{i + 1}", 30000 + i), now=1000.0, locator=locator
        )
        candidate_addr = (f"198.51.100.{i + 1}", 45000 + i)
        state.open_or_replace_candidate_path(
            s.session,
            s.session.current_epoch,
            candidate_addr,
            os.urandom(p.PATH_CHALLENGE_TOKEN_BYTES),
            1000.0,
        )
        sessions.append(s)
        expected[locator.hex()[:8]] = candidate_addr
    diag.sink = _ScriptedSink()
    diag.flush()  # OPENED events out of the way

    barrier = threading.Barrier(5)
    errors = []

    def lazy(chunk):
        try:
            barrier.wait()
            for s in chunk:
                state.resolve_incoming_path_role(
                    s.session, s.session.path_state.active_path, 1010.0
                )
        except BaseException as exc:
            errors.append(exc)

    def maintenance():
        try:
            barrier.wait()
            state.cleanup_expired_path_transitions(1010.0)
        except BaseException as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=lazy, args=(sessions[k::4],)) for k in range(4)
    ] + [threading.Thread(target=maintenance)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10.0)
    assert not errors

    sink = _ScriptedSink(forbid_lock=state._lock)
    diag.sink = sink
    diag.flush()
    expired = [
        line for line in sink.lines if line.startswith("[+] Path candidate EXPIRED")
    ]
    assert len(expired) == count
    for label, candidate_addr in expected.items():
        matching = [line for line in expired if f"session={label} " in line]
        assert len(matching) == 1
        assert f"candidate={candidate_addr[0]}:{candidate_addr[1]}" in matching[0]
    assert sink.lock_violations == 0
    assert diag.stats().dropped_overflow == 0


def test_f2_consumer_start_is_idempotent_across_callers_and_listeners(env):
    state = env.new_state()
    diag = state.migration_diagnostics
    before = len(_diag_threads())

    # Two listeners (distinct endpoint tokens) sharing this owner process
    # real traffic: packet processing never spawns a consumer.
    s1 = env.install_session(state, addr=ADDR_A, now=1000.0)
    other = ("192.0.2.77", 47000)
    s2 = env.install_session(state, addr=other, now=1000.0)
    assert len(s1.feed([(s1.ping_packet(1), ADDR_A)], 1001.0).sent) == 1
    assert len(s2.feed([(s2.ping_packet(1), other)], 1001.0).sent) == 1
    assert len(_diag_threads()) == before

    barrier = threading.Barrier(8)
    results = []

    def starter():
        barrier.wait()
        results.append(diag.start())

    starters = [threading.Thread(target=starter) for _ in range(8)]
    for t in starters:
        t.start()
    for t in starters:
        t.join(10.0)
    try:
        assert results.count(True) == 1
        assert len(_diag_threads()) == before + 1
        assert diag.start() is False
    finally:
        assert diag.stop()
    assert len(_diag_threads()) == before


# --------------------------------------------------------------------------
# 5. Noninterference: a wedged or throwing sink changes nothing about the
#    real PING/PONG, candidate/PATH_CHALLENGE, PATH_RESPONSE/PATH_ACK or
#    replay behaviour, and adds no datagrams.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["stalled", "throwing"])
def test_f2_broken_or_stalled_output_never_changes_migration_exchange(env, mode):
    state = env.new_state(path_candidate_ttl=1000.0, retired_path_grace=1000.0)
    sess = env.install_session(state, addr=ADDR_A, now=1000.0)
    diag = state.migration_diagnostics
    release = threading.Event()
    if mode == "stalled":
        sink = _ScriptedSink(block=release)
    else:
        sink = _ScriptedSink(fail_forever=True)
    diag.sink = sink
    assert diag.start()
    try:
        ping_socket = sess.feed([(sess.ping_packet(1), ADDR_A)], 1000.0)
        assert [sess._is_type(d, "pong", 0) for d, _a in ping_socket.sent] == [True]

        open_socket = sess.feed(
            [(sess.nmea_packet("!AIVDM,1,1,,A,x,0*00"), ADDR_B)], 1000.0
        )
        assert len(open_socket.sent) == 1
        challenge = sess.sole_challenge(open_socket, expect_addr=ADDR_B)
        if mode == "stalled":
            # The consumer really is wedged inside the OPENED write while
            # every following exchange runs.
            assert sink.entered.wait(5.0)

        response = sess.path_response_packet(
            challenge.challenge_token, challenge.path_generation
        )
        commit_socket = sess.feed([(response, ADDR_B)], 1000.0)
        assert len(commit_socket.sent) == 1
        ack_packet, ack_addr = commit_socket.sent[0]
        assert ack_addr == ADDR_B
        assert sess._is_type(ack_packet, p.PATH_ACK_TYPE, 0)

        assert sess.feed([(response, ADDR_B)], 1000.0).sent == []  # replay

        pong_socket = sess.feed([(sess.ping_packet(2), ADDR_B)], 1000.0)
        assert len(pong_socket.sent) == 1 and pong_socket.sent[0][1] == ADDR_B

        assert state.path_state_snapshot(sess.session, 1000.0)["active"] == ADDR_B
        protocol_stats = state.stats()
        assert protocol_stats.migration_challenges_sent == 1
        assert protocol_stats.path_migrations_committed == 1
    finally:
        release.set()
        assert diag.wait_idle(5.0)
        assert diag.stop()
    final = diag.stats()
    assert final.published == 2  # OPENED + COMMITTED
    if mode == "stalled":
        assert final.delivered == 2
    else:
        assert final.undeliverable == 2
    assert _accounting_balances(final)


# --------------------------------------------------------------------------
# 7. Lifecycle.
# --------------------------------------------------------------------------


def test_f2_no_consumer_on_import_or_owner_construction(monkeypatch):
    before = len(_diag_threads())
    secure = load_secure_module_with_fake_keys(monkeypatch)
    assert len(_diag_threads()) == before
    assert not secure.secure_state.migration_diagnostics.stats().consumer_running
    owners = [secure.SecureState() for _ in range(2000)]
    assert len(_diag_threads()) == before
    assert not any(o.migration_diagnostics.stats().consumer_running for o in owners)


def test_f2_maintenance_task_owns_consumer_lifetime(env):
    state = env.new_state()
    diag = state.migration_diagnostics
    before = len(_diag_threads())
    observed = {}

    async def scenario():
        task = asyncio.create_task(state.run_periodic_maintenance(interval=3600.0))
        await asyncio.sleep(0)
        observed["running"] = diag.stats().consumer_running
        observed["threads"] = len(_diag_threads())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    started = time.monotonic()
    asyncio.run(scenario())
    assert observed == {"running": True, "threads": before + 1}
    assert not diag.stats().consumer_running
    assert len(_diag_threads()) == before
    assert time.monotonic() - started < 5.0


def test_f2_close_flushes_and_stops_consumer_idempotently(env):
    state = env.new_state()
    diag = state.migration_diagnostics
    before = len(_diag_threads())
    sink = _ScriptedSink()
    diag.sink = sink
    assert diag.start()
    diag.publish(_valid_event(0))
    state.close(1000.0)
    assert len(sink.lines) == 1  # delivered by the consumer or its shutdown flush
    assert not diag.stats().consumer_running
    assert len(_diag_threads()) == before
    state.close(1001.0)  # idempotent
    assert _accounting_balances(diag.stats())


def test_f2_stop_with_failing_sink_is_bounded_and_counted(env):
    state = env.new_state()
    diag = state.migration_diagnostics
    attempts = env.secure.MIGRATION_DIAGNOSTIC_WRITE_ATTEMPTS
    for i in range(5):
        diag.publish(_valid_event(i))
    sink = _ScriptedSink(fail_forever=True)
    diag.sink = sink
    started = time.monotonic()
    assert diag.start()
    assert diag.stop()
    assert time.monotonic() - started < 5.0
    stats = diag.stats()
    assert stats.undeliverable == 5
    assert stats.write_errors == 5 * attempts
    assert stats.queued == 0
    assert not stats.consumer_running
    assert _accounting_balances(stats)


def test_f2_stop_with_wedged_sink_is_bounded_then_restartable(env):
    state = env.new_state()
    diag = state.migration_diagnostics
    before = len(_diag_threads())
    release = threading.Event()
    sink = _ScriptedSink(block=release)
    diag.sink = sink
    for i in range(5):
        diag.publish(_valid_event(i))
    assert diag.start()
    wedged_thread = diag._worker
    assert sink.entered.wait(5.0)  # consumer is stuck inside the first write

    started = time.monotonic()
    assert diag.stop(timeout=0.2) is False
    assert time.monotonic() - started < 2.0
    stats = diag.stats()
    assert stats.dropped_on_shutdown == 4
    assert stats.queued == 0
    assert stats.in_flight == 1
    assert not stats.consumer_running
    # Truthful: the stop-requested thread is still alive and still owned,
    # so no replacement may start.
    assert stats.worker_alive
    assert diag.start() is False
    assert len(_diag_threads()) == before + 1

    # Once the write returns, the stale daemon thread exits on its own.
    release.set()
    wedged_thread.join(5.0)
    assert not wedged_thread.is_alive()
    assert len(_diag_threads()) == before
    assert diag.stats().delivered == 1
    assert not diag.stats().worker_alive

    # A fresh consumer can be started afterwards.
    sink.block = None
    assert diag.start()
    try:
        diag.publish(_valid_event(9))
        assert diag.wait_idle(5.0)
        assert diag.stats().delivered == 2
    finally:
        assert diag.stop()
    assert len(_diag_threads()) == before
    assert _accounting_balances(diag.stats())


# ==========================================================================
# F2 FINAL CLOSURE (Astra lifecycle / shared-stdout / harness audit).
# L1-L3: at most ONE live worker per owner, race-safe start/stop/close,
#        terminal close. L4-L6: real-path failure isolation and bounded
#        accounting under concurrency. L7: the production sink never holds
#        Python's buffered-stdout lock, proven in killable child processes.
# ==========================================================================


def _live_diag_threads_over(before):
    return len(_diag_threads()) - before


# --------------------------------------------------------------------------
# L1. Stalled stop/start cycles cannot accumulate workers.
# --------------------------------------------------------------------------


def test_l1_stalled_stop_start_cycles_never_accumulate_workers(env):
    state = env.new_state()
    diag = state.migration_diagnostics
    before = len(_diag_threads())
    release = threading.Event()
    sink = _ScriptedSink(block=release)
    diag.sink = sink
    diag.publish(_valid_event(0))
    assert diag.start()
    worker = diag._worker
    assert sink.entered.wait(5.0)  # blocked inside the first write
    try:
        for cycle in range(8):
            assert diag.stop(timeout=0.05) is False
            # A timed-out stop never authorizes a replacement.
            assert diag.start() is False
            diag.publish(_valid_event(cycle + 1))
            stats = diag.stats()
            assert _live_diag_threads_over(before) == 1
            assert diag._worker is worker
            assert stats.in_flight == 1
            assert stats.worker_alive and not stats.consumer_running
            assert stats.queued <= 1
            assert _accounting_balances(stats)
    finally:
        release.set()

    worker.join(5.0)
    assert not worker.is_alive()
    assert _live_diag_threads_over(before) == 0
    final = diag.stats()
    assert final.in_flight == 0 and not final.worker_alive
    # event 0 (in flight) + event 8 (queued after the last stop) delivered;
    # events 1..7 were each counted dropped by the NEXT timed-out stop.
    assert final.delivered == 2
    assert final.dropped_on_shutdown == 7
    assert _accounting_balances(final)

    # The dead worker is reaped and the OPEN owner can restart safely.
    sink.block = None
    assert diag.start()
    try:
        diag.publish(_valid_event(99))
        assert diag.wait_idle(5.0)
        assert _live_diag_threads_over(before) == 1
    finally:
        assert diag.stop()
    assert _live_diag_threads_over(before) == 0


# --------------------------------------------------------------------------
# L2. Concurrent start/stop: no join-before-start, no duplicate worker.
# --------------------------------------------------------------------------


def test_l2_concurrent_start_stop_is_race_free(env):
    state = env.new_state()
    diag = state.migration_diagnostics
    diag.sink = _ScriptedSink()
    before = len(_diag_threads())
    errors = []
    max_live = [0]
    sampling = threading.Event()
    sampling.set()

    def monitor():
        while sampling.is_set():
            max_live[0] = max(max_live[0], _live_diag_threads_over(before))
            time.sleep(0)

    monitor_thread = threading.Thread(target=monitor)
    monitor_thread.start()
    try:
        for round_index in range(40):
            barrier = threading.Barrier(6)

            def actor(op):
                try:
                    barrier.wait(5.0)
                    for i in range(5):
                        if op == "start":
                            diag.start()
                        else:
                            diag.stop(timeout=2.0)
                        diag.publish(_valid_event(round_index * 10 + i))
                except BaseException as exc:  # surfaced below
                    errors.append(exc)

            actors = [
                threading.Thread(target=actor, args=(op,))
                for op in ("start", "stop", "start", "stop", "start", "stop")
            ]
            for t in actors:
                t.start()
            for t in actors:
                t.join(10.0)
                assert not t.is_alive()
            assert diag.stop(timeout=5.0)
    finally:
        sampling.clear()
        monitor_thread.join(5.0)
    assert errors == []
    assert max_live[0] <= 1
    assert _live_diag_threads_over(before) == 0
    assert _accounting_balances(diag.stats())


def test_l2_stop_inside_slow_thread_start_never_joins_unstarted_worker(
    env, monkeypatch
):
    """Deterministic widening of the audited join-before-start window: the
    worker's `Thread.start()` pauses, and `stop()` arrives inside that
    pause. It must wait for start to complete (lifecycle lock) instead of
    raising 'cannot join thread before it is started'."""
    start_entered = threading.Event()

    class _SlowStartThread(threading.Thread):
        def start(self):
            start_entered.set()
            time.sleep(0.2)
            super().start()

    slow_threading = types.ModuleType("threading_slow_start")
    slow_threading.__dict__.update(threading.__dict__)
    slow_threading.Thread = _SlowStartThread
    monkeypatch.setattr(env.secure, "threading", slow_threading)

    state = env.new_state()
    diag = state.migration_diagnostics
    diag.sink = _ScriptedSink()
    before = len(_diag_threads())
    results = {}
    errors = []

    def starter():
        try:
            results["start"] = diag.start()
        except BaseException as exc:
            errors.append(exc)

    def stopper():
        try:
            assert start_entered.wait(5.0)
            results["stop"] = diag.stop(timeout=5.0)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=starter), threading.Thread(target=stopper)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10.0)
        assert not t.is_alive()
    assert errors == []
    assert results == {"start": True, "stop": True}
    assert not diag.stats().worker_alive
    assert _live_diag_threads_over(before) == 0


# --------------------------------------------------------------------------
# L3. close() is terminal and bounded: no start after close (direct,
#     racing, pending, or via a late maintenance entry); no resurrection.
# --------------------------------------------------------------------------


def _maintenance_blip(state):
    """Run a real maintenance task just long enough for its entry (which
    calls start()) to execute, then cancel it. Returns whether a live
    worker was observed while it ran."""
    observed = {}

    async def scenario():
        task = asyncio.create_task(state.run_periodic_maintenance(interval=3600.0))
        await asyncio.sleep(0)
        observed["alive"] = state.migration_diagnostics.stats().worker_alive
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())
    return observed["alive"]


def test_l3_close_is_terminal_against_racing_pending_and_late_starts(env):
    before = len(_diag_threads())
    for _round in range(25):
        state = env.new_state()
        diag = state.migration_diagnostics
        diag.sink = _ScriptedSink()
        barrier = threading.Barrier(3)
        closed = threading.Event()
        pending = []
        errors = []

        def racing_start():
            try:
                barrier.wait(5.0)
                diag.start()
            except BaseException as exc:
                errors.append(exc)

        def racing_maintenance():
            try:
                barrier.wait(5.0)
                _maintenance_blip(state)
            except BaseException as exc:
                errors.append(exc)

        def closer():
            try:
                barrier.wait(5.0)
                state.close(1000.0)
                closed.set()
            except BaseException as exc:
                errors.append(exc)

        def pending_start():
            closed.wait(10.0)
            pending.append(diag.start())

        threads = [
            threading.Thread(target=f)
            for f in (racing_start, racing_maintenance, closer, pending_start)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(15.0)
            assert not t.is_alive()
        assert errors == []
        assert pending == [False]
        stats = diag.stats()
        assert stats.closed
        assert not stats.worker_alive
        assert diag.start() is False
        assert _maintenance_blip(state) is False  # late maintenance entry
        assert not diag.stats().worker_alive
        state.close(1001.0)  # idempotent
        assert _accounting_balances(diag.stats())
    assert _live_diag_threads_over(before) == 0


def test_l3_close_with_wedged_worker_is_bounded_and_never_resurrects(env):
    state = env.new_state()
    diag = state.migration_diagnostics
    before = len(_diag_threads())
    release = threading.Event()
    sink = _ScriptedSink(block=release)
    diag.sink = sink
    diag.publish(_valid_event(0))
    assert diag.start()
    worker = diag._worker
    assert sink.entered.wait(5.0)
    try:
        started = time.monotonic()
        state.close(1000.0)
        assert time.monotonic() - started < 3.0  # bounded, never indefinite
        stats = diag.stats()
        assert stats.closed and stats.worker_alive and not stats.consumer_running
        assert stats.in_flight == 1
        assert diag.start() is False
        published = stats.published
        diag.publish(_valid_event(1))  # after close: counted, never queued
        after_close = diag.stats()
        assert after_close.published == published + 1
        assert after_close.queued == 0
        assert _accounting_balances(after_close)
    finally:
        release.set()

    worker.join(5.0)
    assert not worker.is_alive()  # late exit after close
    assert diag.stats().delivered == 1
    assert diag.start() is False  # no resurrection once the thread is gone
    assert _maintenance_blip(state) is False
    assert _live_diag_threads_over(before) == 0
    assert _accounting_balances(diag.stats())


# --------------------------------------------------------------------------
# L4. Real encrypted PING expiring candidate AND retired records with the
#     WORKER running and a transient-then-recovering or persistently broken
#     sink.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "failure",
    ["transient-oserror", "persistent-brokenpipe"],
)
def test_l4_worker_isolates_sink_failure_on_real_expiry_ping(env, failure):
    state, sess = _migrated_session_with_expiring_candidate_and_retired(env)
    diag = state.migration_diagnostics
    attempts = env.secure.MIGRATION_DIAGNOSTIC_WRITE_ATTEMPTS
    if failure == "transient-oserror":
        sink = _ScriptedSink(fail_first=1, exc=OSError)
    else:
        sink = _ScriptedSink(fail_forever=True, exc=BrokenPipeError)
    diag.sink = sink
    before = diag.stats()
    assert diag.start()
    try:
        pong_socket = sess.feed([(sess.ping_packet(1), ADDR_B)], 1010.0)
        assert len(pong_socket.sent) == 1
        pong_packet, pong_addr = pong_socket.sent[0]
        assert pong_addr == ADDR_B
        pong = sess.decode_server_message(pong_packet)
        assert pong["type"] == "pong" and pong["seq"] == 1
        assert diag.wait_idle(5.0)
        after = diag.stats()
        # exactly two publications (candidate + retired EXPIRED) ...
        assert after.published == before.published + 2
        # ... each ending in exactly one delivery or one recorded failure.
        if failure == "transient-oserror":
            assert after.delivered == before.delivered + 2
            assert after.undeliverable == before.undeliverable
            assert after.write_errors == before.write_errors + 1
            assert [line.split(" for ")[0] for line in sink.lines] == [
                "[+] Path candidate EXPIRED",
                "[+] Retired path EXPIRED",
            ]
        else:
            assert after.delivered == before.delivered
            assert after.undeliverable == before.undeliverable + 2
            assert after.write_errors == before.write_errors + 2 * attempts
        assert after.consumer_running

        # later maintenance can never re-emit physically removed records
        state.cleanup_expired_path_transitions(1020.0)
        assert diag.wait_idle(5.0)
        assert diag.stats().published == after.published
    finally:
        assert diag.stop()
    assert _accounting_balances(diag.stats())


# --------------------------------------------------------------------------
# L6. Hard bound + exact accounting under 1200 concurrent publishes, a
#     stalled then flaky consumer, repeated stop attempts, and close.
# --------------------------------------------------------------------------


class _StallThenFlakySink:
    """First write blocks until released (stalled consumer); afterwards
    every third attempt raises `OSError` (exercises bounded retries)."""

    def __init__(self, release):
        self.release = release
        self.entered = threading.Event()
        self.calls = 0
        self.lock = threading.Lock()

    def __call__(self, line):
        with self.lock:
            self.calls += 1
            call = self.calls
        if call == 1:
            self.entered.set()
            self.release.wait(10.0)
        if call % 3 == 0:
            raise OSError("simulated flaky diagnostic output")


def test_l6_bounded_accounting_under_concurrency_stalls_retries_and_close(env):
    capacity = env.secure.MIGRATION_DIAGNOSTIC_QUEUE_CAPACITY
    state = env.new_state()
    diag = state.migration_diagnostics
    before = len(_diag_threads())
    release = threading.Event()
    sink = _StallThenFlakySink(release)
    diag.sink = sink
    assert diag.start()
    diag.publish(_valid_event(0))
    assert sink.entered.wait(5.0)

    samples = []
    sampling = threading.Event()
    sampling.set()

    def sampler():
        while sampling.is_set():
            s = diag.stats()
            samples.append(
                (
                    s.queued,
                    s.in_flight,
                    _live_diag_threads_over(before),
                    _accounting_balances(s),
                )
            )
            time.sleep(0.0005)

    nthreads, per_thread = 4, 300
    barrier = threading.Barrier(nthreads + 1)
    errors = []

    def publisher(k):
        try:
            barrier.wait(5.0)
            for i in range(per_thread):
                diag.publish(_valid_event(1 + k * per_thread + i))
        except BaseException as exc:
            errors.append(exc)

    def stopper():
        try:
            barrier.wait(5.0)
            for _ in range(5):
                assert diag.stop(timeout=0.01) is False  # consumer stalled
        except BaseException as exc:
            errors.append(exc)

    sampler_thread = threading.Thread(target=sampler)
    workers = [threading.Thread(target=publisher, args=(k,)) for k in range(nthreads)]
    workers.append(threading.Thread(target=stopper))
    sampler_thread.start()
    try:
        for t in workers:
            t.start()
        for t in workers:
            t.join(15.0)
            assert not t.is_alive()
        assert errors == []
        mid = diag.stats()
        assert mid.worker_alive and not mid.consumer_running
        assert mid.published == 1 + nthreads * per_thread
        assert mid.queued <= capacity and mid.in_flight == 1
        assert _accounting_balances(mid)
    finally:
        release.set()
        state.close(1000.0)
        sampling.clear()
        sampler_thread.join(5.0)

    assert samples, "sampler observed nothing"
    assert max(q for q, _i, _l, _b in samples) <= capacity
    assert max(i for _q, i, _l, _b in samples) <= 1
    assert max(live for _q, _i, live, _b in samples) <= 1
    assert all(balanced for _q, _i, _l, balanced in samples)
    final = diag.stats()
    assert final.closed and not final.worker_alive
    assert final.queued == 0 and final.in_flight == 0
    assert final.dropped_overflow > 0 or final.dropped_on_shutdown > 0
    assert _accounting_balances(final)
    diag.publish(_valid_event(5000))  # after close
    assert diag.stats().dropped_on_shutdown == final.dropped_on_shutdown + 1
    assert _accounting_balances(diag.stats())
    assert _live_diag_threads_over(before) == 0


# --------------------------------------------------------------------------
# L7. The production sink never holds Python's buffered-stdout lock while
#     blocked. Run in killable child processes (hard timeouts): a real
#     migration's PATH_ACK, the legacy synchronous post-ACK print, and the
#     next PONG must not wait on a diagnostic write blocked at the OS
#     boundary. `os.write` is gated ONLY for the diagnostic worker thread,
#     modelling a write stuck on a full pipe.
# --------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[1]
_GATE_HOLD_SECONDS = 3.0

_L7_CHILD = r'''
import io, json, os, sys, threading, time

repo, stdout_mode, sink_mode, exit_mode, hold = sys.argv[1:6]
hold = float(hold)
sys.path[:0] = [repo, os.path.join(repo, "tests")]

import pytest
from test_secure_udp_helpers import load_secure_module_with_fake_keys
from test_udpsec_path_migration import ADDR_A, ADDR_B, _Env

WORKER = "udpsec-migration-diagnostics"
gate = threading.Event()
entered = threading.Event()
real_write = os.write


def gated_write(fd, data):
    # Models an OS write blocked on a full pipe -- for the diagnostic
    # worker thread only; every other thread writes normally.
    if threading.current_thread().name == WORKER:
        entered.set()
        gate.wait(30.0)
    return real_write(fd, data)


class GatedRaw(io.RawIOBase):
    # A Python raw layer under a REAL C BufferedWriter, so a write that
    # reaches the OS through it does so while BufferedWriter holds its lock.
    def __init__(self, fd):
        self._fd = fd

    def writable(self):
        return True

    def fileno(self):
        return self._fd

    def write(self, b):
        return os.write(self._fd, bytes(b))


if stdout_mode == "gated-raw":
    # write_through: every legacy print reaches the BufferedWriter (and so
    # needs its lock) instead of accumulating in TextIOWrapper's own
    # pending buffer first.
    sys.stdout = io.TextIOWrapper(
        io.BufferedWriter(GatedRaw(sys.stdout.fileno())),
        encoding="utf-8",
        line_buffering=False,
        write_through=True,
    )

mp = pytest.MonkeyPatch()
secure, station_key = load_secure_module_with_fake_keys(
    mp, with_client_private_key=True
)
env = _Env(mp, secure, station_key)
state = env.new_state(path_candidate_ttl=1000.0, retired_path_grace=1000.0)
sess = env.install_session(state, addr=ADDR_A, now=1000.0)
diag = state.migration_diagnostics
if sink_mode == "locked-control":
    def locked_sink(line):
        # The PRE-FIX default sink, verbatim: writes through buffered
        # sys.stdout and flushes while holding its lock.
        stream = sys.stdout
        stream.write(line + "\n")
        stream.flush()
    diag.sink = locked_sink

os.write = gated_write
report = {"buffer_type": type(sys.stdout.buffer).__name__}
assert diag.start()
open_socket = sess.feed([(sess.nmea_packet("!AIVDM,1,1,,A,x,0*00"), ADDR_B)], 1000.0)
report["worker_entered"] = entered.wait(10.0)
challenge = sess.sole_challenge(open_socket, expect_addr=ADDR_B)
response = sess.path_response_packet(
    challenge.challenge_token, challenge.path_generation
)
if exit_mode == "release":
    timer = threading.Timer(hold, gate.set)
    timer.daemon = True
    timer.start()
started = time.monotonic()
commit_socket = sess.feed([(response, ADDR_B)], 1000.0)
# Legacy output reaching the BufferedWriter on the event-loop thread (as it
# does at TextIOWrapper's chunk threshold in a buffered stdout).
sys.stdout.flush()
ack_done = time.monotonic()
pong_socket = sess.feed([(sess.ping_packet(1), ADDR_B)], 1000.0)
pong_done = time.monotonic()
report["gate_released_before_pong"] = gate.is_set()
report["ack_sent"] = len(commit_socket.sent)
report["pong_sent"] = len(pong_socket.sent)
report["ack_seconds"] = ack_done - started
report["ack_and_pong_seconds"] = pong_done - started
report["worker_alive"] = diag.stats().worker_alive
if exit_mode == "release":
    gate.set()
    report["stopped"] = diag.stop(timeout=5.0)
sys.stderr.write("REPORT " + json.dumps(report) + "\n")
sys.stderr.flush()
# exit_mode == "wedged-exit": leave the worker blocked in its write and let
# the interpreter shut down (it must flush buffered stdout without waiting
# on any lock the worker holds).
'''


def _run_l7_child(tmp_path, stdout_mode, sink_mode, exit_mode, *, unbuffered=False):
    script = tmp_path / "l7_child.py"
    script.write_text(_L7_CHILD, encoding="utf-8")
    command = [sys.executable]
    if unbuffered:
        command.append("-u")
    command += [
        str(script),
        str(_REPO_ROOT),
        stdout_mode,
        sink_mode,
        exit_mode,
        str(_GATE_HOLD_SECONDS),
    ]
    child_env = dict(os.environ)
    child_env.pop("PYTHONUNBUFFERED", None)
    completed = subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=120,
        env=child_env,
        cwd=str(_REPO_ROOT),
    )
    report_lines = [
        line
        for line in completed.stderr.decode("utf-8", "replace").splitlines()
        if line.startswith("REPORT ")
    ]
    report = json.loads(report_lines[-1][len("REPORT "):]) if report_lines else None
    return completed, report


_L7_PRODUCTION_MODES = [
    pytest.param("buffered-real", False, id="buffered-stdout"),
    pytest.param("gated-raw", False, id="buffered-writer-over-gated-raw"),
    pytest.param("unbuffered-real", True, id="python-u"),
]


@pytest.mark.parametrize("stdout_mode,unbuffered", _L7_PRODUCTION_MODES)
def test_l7_blocked_diagnostic_write_never_delays_ack_print_or_pong(
    tmp_path, stdout_mode, unbuffered
):
    completed, report = _run_l7_child(
        tmp_path, stdout_mode, "production", "release", unbuffered=unbuffered
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
    assert report is not None
    assert report["worker_entered"]  # the diagnostic write really is blocked
    assert report["ack_sent"] == 1 and report["pong_sent"] == 1
    # PATH_ACK, its legacy post-ACK print, and the next PONG all completed
    # while the diagnostic write was still blocked.
    assert not report["gate_released_before_pong"]
    assert report["ack_and_pong_seconds"] < _GATE_HOLD_SECONDS / 2
    assert report["stopped"]
    stdout = completed.stdout
    assert b"Committed UDPSEC path migration" in stdout  # the legacy print ran
    assert b"[+] Path candidate OPENED" in stdout  # delivered once unblocked
    assert b"[+] Path migration COMMITTED" in stdout


def test_l7_control_a_lock_holding_sink_does_stall_the_event_loop(tmp_path):
    """Control proving the harness detects the audited mechanism: the
    PRE-FIX sink, blocked inside its buffered flush, holds the
    BufferedWriter lock, so legacy prints on the event loop wait for it and
    the PATH_ACK/PONG exchange is delayed by the full blocked interval."""
    completed, report = _run_l7_child(
        tmp_path, "gated-raw", "locked-control", "release"
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
    assert report["worker_entered"]
    assert report["gate_released_before_pong"]
    assert report["ack_and_pong_seconds"] >= _GATE_HOLD_SECONDS * 2 / 3


@pytest.mark.parametrize("stdout_mode,unbuffered", _L7_PRODUCTION_MODES)
def test_l7_interpreter_exits_cleanly_while_diagnostic_write_is_blocked(
    tmp_path, stdout_mode, unbuffered
):
    completed, report = _run_l7_child(
        tmp_path, stdout_mode, "production", "wedged-exit", unbuffered=unbuffered
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
    assert report is not None and report["worker_entered"]
    assert report["worker_alive"]  # still blocked at interpreter shutdown
    assert report["ack_and_pong_seconds"] < _GATE_HOLD_SECONDS / 2
    assert b"Committed UDPSEC path migration" in completed.stdout


# ==========================================================================
# SIGTERM-INTERRUPTED WORKER STARTUP (Astra deployment blocker).
# An asynchronous KeyboardInterrupt on the event-loop thread -- what
# aismixer's SIGTERM handler raised anywhere before S7, what it still
# raises when no event loop is running, and what a second Ctrl+C under
# asyncio.run() raises -- can surface INSIDE `Thread.start()`: window A
# before the native thread exists, window B after it exists but before
# Thread's started flag is visible (`is_alive()` still False). Neither may
# cause a join-before-start, nor a replacement worker while the start is
# unproven. S1-S4 run in-process with a stand-in BaseException (a real
# signal must never reach the pytest process); S5 uses REAL SIGTERMs and
# aismixer's REAL handler in disposable child processes -- raising
# immediately in its loop-less scenarios, deferred to a loop callback
# boundary in its service scenario (see S7).
# ==========================================================================


class _SimulatedSigterm(BaseException):
    """In-process stand-in for the KeyboardInterrupt raised by aismixer's
    SIGTERM handler: a BaseException, so `Thread.start()`'s own
    `except Exception` does not catch it either."""


_THREAD_CREATE_PRIMITIVE = (
    "_start_joinable_thread"
    if hasattr(threading, "_start_joinable_thread")
    else "_start_new_thread"
)


class _StartInterruptInjector:
    """Wraps threading's native thread-creation primitive -- for the
    diagnostic worker only -- to interrupt `Thread.start()` deterministically:
    window "A" raises before the native thread is created; "B" creates it,
    holds it before Thread's started flag is set, then raises; "fail"
    raises Thread.start()'s documented creation failure (RuntimeError).
    Counts native worker threads from their first to last instruction,
    independently of `Thread.is_alive()`."""

    def __init__(self, monkeypatch):
        self._real = getattr(threading, _THREAD_CREATE_PRIMITIVE)
        self._lock = threading.Lock()
        self.armed = None
        self.gate = threading.Event()
        self.held = threading.Event()
        self.live = 0
        self.max_live = 0
        self.created = 0
        self.interrupted = []
        monkeypatch.setattr(threading, _THREAD_CREATE_PRIMITIVE, self._primitive)

    def reset(self):
        self.armed = None
        self.gate = threading.Event()
        self.held = threading.Event()

    def _counted(self, function, gate):
        def bootstrap():
            with self._lock:
                self.live += 1
                self.created += 1
                self.max_live = max(self.max_live, self.live)
            try:
                if gate is not None:
                    self.held.set()
                    gate.wait(30.0)
                function()
            finally:
                with self._lock:
                    self.live -= 1

        return bootstrap

    def _primitive(self, function, *args, **kwargs):
        thread = getattr(function, "__self__", None)
        if not (
            isinstance(thread, threading.Thread)
            and thread.name == _DIAG_THREAD_NAME
        ):
            return self._real(function, *args, **kwargs)
        window, self.armed = self.armed, None
        if window == "fail":
            raise RuntimeError("can't start new thread")
        if window is not None:
            self.interrupted.append(thread)
        if window == "A":
            raise _SimulatedSigterm("inside Thread.start(), before creation")
        gate = self.gate if window == "B" else None
        result = self._real(self._counted(function, gate), *args, **kwargs)
        if window == "B":
            assert self.held.wait(10.0)
            raise _SimulatedSigterm("inside Thread.start(), after creation")
        return result

    def wait_quiescent(self, timeout):
        deadline = time.monotonic() + timeout
        while self.live and time.monotonic() < deadline:
            time.sleep(0.005)
        return self.live == 0

    def cleanup(self):
        # Release any held thread, and drop never-created Thread objects
        # that an interrupted Thread.start() left in threading's own
        # registry (exactly as a real signal would).
        self.gate.set()
        self.wait_quiescent(5.0)
        with threading._active_limbo_lock:
            for thread in self.interrupted:
                if thread.ident is None:
                    threading._limbo.pop(thread, None)


@pytest.fixture
def start_interrupts(monkeypatch):
    injector = _StartInterruptInjector(monkeypatch)
    try:
        yield injector
    finally:
        injector.cleanup()


# --------------------------------------------------------------------------
# S1. Window A: interrupted BEFORE any native thread exists.
# --------------------------------------------------------------------------


def test_s1_interrupt_before_native_creation_keeps_reservation_never_joins(
    env, start_interrupts
):
    """Whether a native thread exists is undecidable from outside, so the
    reservation is kept (fail-closed): no join-before-start, no
    replacement, a bounded and truthful stop()/close(), no start after
    close -- and no thread ever appears."""
    state = env.new_state()
    diag = state.migration_diagnostics
    diag.sink = _ScriptedSink()
    start_interrupts.armed = "A"
    with pytest.raises(_SimulatedSigterm):
        diag.start()
    reserved = diag._worker
    assert reserved is not None and not reserved.is_alive()
    stats = diag.stats()
    assert stats.worker_starting and not stats.worker_alive
    assert not stats.consumer_running and not stats.closed

    assert diag.start() is False  # no replacement while unproven
    assert diag._worker is reserved
    assert diag.flush() == 0  # nor a second consumer via the test hook
    diag.publish(_valid_event(0))

    started = time.monotonic()
    assert diag.stop(timeout=0.2) is False  # never joins; start unproven
    assert time.monotonic() - started < 2.0
    stats = diag.stats()
    assert stats.worker_starting and stats.queued == 0
    assert stats.dropped_on_shutdown == 1
    assert _accounting_balances(stats)
    assert diag.start() is False

    started = time.monotonic()
    assert diag.close(timeout=0.2) is False
    assert time.monotonic() - started < 2.0
    assert diag.start() is False
    assert _maintenance_blip(state) is False  # late maintenance entry
    state.close(1000.0)  # owner close: bounded, idempotent, no exception
    diag.publish(_valid_event(1))
    final = diag.stats()
    assert final.closed and final.worker_starting and not final.worker_alive
    assert final.dropped_on_shutdown == 2
    assert _accounting_balances(final)
    assert start_interrupts.created == 0  # no native worker ever existed


def test_s1_documented_creation_failure_releases_the_reservation(
    env, start_interrupts
):
    """Contrast: Thread.start()'s documented RuntimeError proves no thread
    was created, so the reservation is released and a later start works."""
    state = env.new_state()
    diag = state.migration_diagnostics
    diag.sink = _ScriptedSink()
    start_interrupts.armed = "fail"
    assert diag.start() is False  # swallowed, as before
    stats = diag.stats()
    assert diag._worker is None
    assert not stats.worker_starting and not stats.consumer_running
    assert diag.start() is True
    try:
        diag.publish(_valid_event(0))
        assert diag.wait_idle(5.0)
    finally:
        assert diag.stop() is True
    assert start_interrupts.max_live == 1


# --------------------------------------------------------------------------
# S2. Window B: interrupted AFTER native creation, before the started flag.
# --------------------------------------------------------------------------


def test_s2_interrupt_after_native_creation_never_duplicates_the_worker(
    env, start_interrupts
):
    """The audited duplicate: a start() while the interrupted worker's
    started flag is not yet visible must NOT replace it. Once released, that
    thread proves its own start and is THE owned, stoppable worker; packets
    are unaffected throughout."""
    state = env.new_state(path_candidate_ttl=1000.0, retired_path_grace=1000.0)
    sess = env.install_session(state, addr=ADDR_A, now=1000.0)
    diag = state.migration_diagnostics
    sink = _ScriptedSink()
    diag.sink = sink
    start_interrupts.armed = "B"
    with pytest.raises(_SimulatedSigterm):
        diag.start()
    held = diag._worker
    try:
        assert start_interrupts.live == 1  # a native thread exists ...
        assert not held.is_alive()  # ... but is not visibly started
        assert diag.stats().worker_starting
        for _ in range(3):
            assert diag.start() is False
        assert diag._worker is held
        assert start_interrupts.created == 1

        opened = sess.feed(
            [(sess.nmea_packet("!AIVDM,1,1,,A,x,0*00"), ADDR_B)], 1000.0
        )
        sess.sole_challenge(opened, expect_addr=ADDR_B)
        pong = sess.feed([(sess.ping_packet(1), ADDR_A)], 1000.0)
        assert [addr for _data, addr in pong.sent] == [ADDR_A]
        assert sess.decode_server_message(pong.sent[0][0])["type"] == "pong"
        assert diag.stats().queued == 1  # the OPENED event, not yet consumed
    finally:
        start_interrupts.gate.set()

    assert diag.wait_idle(5.0)
    stats = diag.stats()
    assert stats.delivered == 1 and len(sink.lines) == 1
    assert stats.consumer_running and not stats.worker_starting
    assert diag._worker is held
    assert diag.start() is False
    assert diag.stop() is True
    assert start_interrupts.wait_quiescent(5.0)
    # Proven and dead: reaped, and the OPEN owner may start a fresh worker.
    assert diag.start() is True
    assert diag.stop() is True
    assert start_interrupts.max_live == 1
    assert start_interrupts.created == 2
    assert _accounting_balances(diag.stats())


def test_s2_close_during_unresolved_start_is_bounded_truthful_and_terminal(
    env, start_interrupts
):
    state = env.new_state()
    diag = state.migration_diagnostics
    diag.sink = _ScriptedSink()
    start_interrupts.armed = "B"
    with pytest.raises(_SimulatedSigterm):
        diag.start()
    held = diag._worker
    try:
        diag.publish(_valid_event(0))
        started = time.monotonic()
        assert diag.close(timeout=0.2) is False  # never joins; truthful
        assert time.monotonic() - started < 2.0
        stats = diag.stats()
        assert stats.closed and stats.worker_starting
        assert stats.queued == 0 and stats.dropped_on_shutdown == 1
        assert diag.start() is False
        assert _maintenance_blip(state) is False  # late maintenance entry
        assert start_interrupts.live == 1 and start_interrupts.created == 1
    finally:
        start_interrupts.gate.set()

    # The late thread proves its start, finds the owner closed and not
    # accepting, and exits on its own.
    assert start_interrupts.wait_quiescent(5.0)
    held.join(5.0)  # safe: its start is proven by now
    assert not held.is_alive()
    diag.publish(_valid_event(1))
    stats = diag.stats()
    assert not stats.worker_alive and not stats.worker_starting
    assert stats.delivered == 0 and stats.dropped_on_shutdown == 2
    assert diag.start() is False  # no resurrection
    assert diag.close(timeout=1.0) is True  # proven and dead: reaped
    assert start_interrupts.max_live == 1 and start_interrupts.created == 1
    assert _accounting_balances(diag.stats())


# --------------------------------------------------------------------------
# S3. Interrupted AFTER Thread.start() returned, before start() recorded
#     the proof: the worker's own proof resolves it.
# --------------------------------------------------------------------------


def test_s3_interrupt_after_thread_start_returned_is_resolved_by_the_worker(
    env, monkeypatch
):
    class _InterruptAfterStart(threading.Thread):
        def start(self):
            super().start()
            raise _SimulatedSigterm("after Thread.start() returned")

    interrupting = types.ModuleType("threading_interrupt_after_start")
    interrupting.__dict__.update(threading.__dict__)
    interrupting.Thread = _InterruptAfterStart
    monkeypatch.setattr(env.secure, "threading", interrupting)

    state = env.new_state()
    diag = state.migration_diagnostics
    sink = _ScriptedSink()
    diag.sink = sink
    before = len(_diag_threads())
    with pytest.raises(_SimulatedSigterm):
        diag.start()
    worker = diag._worker
    try:
        deadline = time.monotonic() + 5.0
        while diag.stats().worker_starting and time.monotonic() < deadline:
            time.sleep(0.005)
        stats = diag.stats()
        assert not stats.worker_starting and stats.consumer_running
        assert diag.start() is False
        diag.publish(_valid_event(0))
        assert diag.wait_idle(5.0) and len(sink.lines) == 1
    finally:
        assert diag.stop() is True
    assert not worker.is_alive()
    assert _live_diag_threads_over(before) == 0


# --------------------------------------------------------------------------
# S4. Racing start/stop/publish/maintenance/close around an interrupted
#     start, across its resolution, and after close.
# --------------------------------------------------------------------------


def test_s4_racing_lifecycle_calls_around_an_interrupted_start(
    env, start_interrupts
):
    for _round in range(5):
        start_interrupts.reset()
        state = env.new_state()
        diag = state.migration_diagnostics
        diag.sink = _ScriptedSink()
        start_interrupts.armed = "B"
        with pytest.raises(_SimulatedSigterm):
            diag.start()
        closed = threading.Event()
        late_starts = []
        errors = []
        barrier = threading.Barrier(6)

        def starter():
            deadline = time.monotonic() + 10.0
            while not closed.is_set() and time.monotonic() < deadline:
                diag.start()
                time.sleep(0.001)
            for _ in range(5):
                late_starts.append(diag.start())

        def stopper():
            for _ in range(10):
                diag.stop(timeout=0.02)

        def publisher():
            for i in range(300):
                diag.publish(_valid_event(i))

        def maintenance():
            _maintenance_blip(state)

        def resolver_then_closer():
            time.sleep(0.02)
            start_interrupts.gate.set()  # the held thread may now prove itself
            time.sleep(0.02)
            state.close(1000.0)
            closed.set()

        def guarded(fn):
            def run():
                try:
                    barrier.wait(5.0)
                    fn()
                except BaseException as exc:  # surfaced below
                    errors.append(exc)

            return run

        actors = [
            threading.Thread(target=guarded(fn))
            for fn in (
                starter,
                starter,
                stopper,
                publisher,
                maintenance,
                resolver_then_closer,
            )
        ]
        try:
            for t in actors:
                t.start()
            for t in actors:
                t.join(20.0)
                assert not t.is_alive()
        finally:
            start_interrupts.gate.set()
            closed.set()
        assert errors == []
        assert late_starts and not any(late_starts)  # nothing after close
        assert start_interrupts.wait_quiescent(5.0)
        assert diag.close(timeout=2.0) is True
        stats = diag.stats()
        assert stats.closed and not stats.worker_alive
        assert not stats.worker_starting and not stats.consumer_running
        assert _accounting_balances(stats)
    assert start_interrupts.max_live == 1  # never two native workers


# --------------------------------------------------------------------------
# S5. REAL SIGTERM through aismixer's REAL handler, in disposable child
#     processes (hard timeouts; the child always releases its held thread).
#     The same child, pointed at a pre-fix aismixer_secure.py via its
#     secure-dir argument, reproduces Astra's join-before-start and
#     duplicate-worker failures on Windows and Linux.
# --------------------------------------------------------------------------

_SIGTERM_CHILD = r'''
import asyncio, json, os, signal, sys, threading, time, traceback

repo, secure_dir, window, scenario = sys.argv[1:5]
sys.dont_write_bytecode = True
sys.path[:0] = [repo, os.path.join(repo, "tests")]
os.chdir(repo)
from pathlib import Path

import pytest
import test_secure_udp_helpers as helpers
from test_udpsec_path_migration import ADDR_A, ADDR_B, ADDR_C, _Env

import aismixer  # the REAL production module, for its REAL SIGTERM handler

WORKER = "udpsec-migration-diagnostics"
report = {
    "window": window,
    "scenario": scenario,
    "python": sys.version.split()[0],
    "platform": sys.platform,
}

# Deterministic SIGTERM delivery INSIDE Thread.start(), for the diagnostic
# worker only, by wrapping threading's native thread-creation primitive:
#   window A -- a real SIGTERM immediately BEFORE the native thread exists;
#   window B -- the native thread is created but held before Thread's
#               started flag is set, then a real SIGTERM.
# Every native worker thread is counted from its first to last instruction,
# independently of Thread.is_alive().
PRIMITIVE = (
    "_start_joinable_thread"
    if hasattr(threading, "_start_joinable_thread")
    else "_start_new_thread"
)
real_primitive = getattr(threading, PRIMITIVE)
native = {"live": 0, "max": 0, "created": 0}
native_lock = threading.Lock()
armed = {"window": window}
gate = threading.Event()
held = threading.Event()


def counted(function, hold):
    def bootstrap():
        with native_lock:
            native["live"] += 1
            native["created"] += 1
            native["max"] = max(native["max"], native["live"])
        try:
            if hold:
                held.set()
                gate.wait(60.0)
            function()
        finally:
            with native_lock:
                native["live"] -= 1
    return bootstrap


def injecting_primitive(function, *args, **kwargs):
    thread = getattr(function, "__self__", None)
    if not (isinstance(thread, threading.Thread) and thread.name == WORKER):
        return real_primitive(function, *args, **kwargs)
    injected = armed.pop("window", None)
    if injected == "A":
        signal.raise_signal(signal.SIGTERM)  # the handler raises right here
        report["handler_did_not_raise"] = True
    result = real_primitive(counted(function, injected == "B"), *args, **kwargs)
    if injected == "B":
        if not held.wait(10.0):
            report["native_thread_not_observed"] = True
        signal.raise_signal(signal.SIGTERM)  # the handler raises right here
        report["handler_did_not_raise"] = True
    return result


setattr(threading, PRIMITIVE, injecting_primitive)
# Exactly what aismixer.run_service() installs.
signal.signal(signal.SIGTERM, aismixer._raise_keyboard_interrupt_for_sigterm)

mp = pytest.MonkeyPatch()
saved_root = helpers.ROOT
helpers.ROOT = Path(secure_dir)
try:
    secure, station_key = helpers.load_secure_module_with_fake_keys(
        mp, with_client_private_key=True
    )
finally:
    helpers.ROOT = saved_root
report["secure_module"] = str(Path(secure.__file__).resolve())
env = _Env(mp, secure, station_key)
state = env.new_state(path_candidate_ttl=1000.0, retired_path_grace=1000.0)
sess = env.install_session(state, addr=ADDR_A, now=1000.0)
diag = state.migration_diagnostics
if scenario != "service":
    delivered = []
    diag.sink = delivered.append


def stats_dict():
    st = diag.stats()
    fields = {name: getattr(st, name) for name in st.__dataclass_fields__}
    fields["balanced"] = st.published == (
        st.delivered + st.dropped_overflow + st.undeliverable
        + st.dropped_on_shutdown + st.queued + st.in_flight
    )
    return fields


def snap(label):
    worker = diag._worker
    report[label] = {
        "worker_reserved": worker is not None,
        "worker_is_alive": bool(worker is not None and worker.is_alive()),
        "native_live": native["live"],
        "native_max": native["max"],
        "native_created": native["created"],
        "stats": stats_dict(),
    }


def call(label, fn):
    started = time.monotonic()
    try:
        result = fn()
    except KeyboardInterrupt:
        result = "KeyboardInterrupt"
    except BaseException as exc:
        result = f"{type(exc).__name__}: {exc}"
    report[label] = result
    report[label + "_seconds"] = round(time.monotonic() - started, 3)
    return result


def packets(label):
    # Real packet path while the lifecycle is interrupted: a genuine
    # candidate OPENED (publishes a diagnostic event, one PATH_CHALLENGE)
    # and an encrypted PING answered by a PONG that decrypts.
    started = time.monotonic()
    opened = sess.feed(
        [(sess.nmea_packet("!AIVDM,1,1,,A,x,0*00"), ADDR_B)], 1000.0
    )
    pong = sess.feed([(sess.ping_packet(1), ADDR_A)], 1000.0)
    report[label] = {
        "challenge_to_b": [addr for _data, addr in opened.sent] == [ADDR_B],
        "pong_ok": len(pong.sent) == 1
        and pong.sent[0][1] == ADDR_A
        and sess.decode_server_message(pong.sent[0][0])["type"] == "pong",
        "seconds": round(time.monotonic() - started, 3),
    }
    mp.undo()  # `feed` swaps the module's asyncio; restore the real one


def wait_quiescent(timeout):
    deadline = time.monotonic() + timeout
    while native["live"] and time.monotonic() < deadline:
        time.sleep(0.01)
    return native["live"] == 0


def late_maintenance_new_threads():
    async def run():
        task = asyncio.create_task(state.run_periodic_maintenance(interval=3600.0))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    created = native["created"]
    asyncio.run(run())
    return native["created"] - created


try:
    if scenario == "shutdown":
        call("start_1", diag.start)
        snap("after_interrupt")
        packets("packets_while_interrupted")
        call("close_1", lambda: state.close(1000.0))
        snap("after_close_1")
        call("close_2", lambda: state.close(1001.0))
        call("start_after_close", diag.start)
        call("late_maintenance_new_threads", late_maintenance_new_threads)
        gate.set()
        report["quiescent"] = wait_quiescent(5.0)
        snap("final")
    elif scenario == "restart":
        call("start_1", diag.start)
        interrupted = diag._worker
        snap("after_interrupt")
        call("start_2", diag.start)
        snap("after_start_2")
        packets("packets_while_interrupted")
        gate.set()
        deadline = time.monotonic() + 2.0
        while (
            interrupted is not None
            and interrupted.ident is None
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        time.sleep(0.2)  # harness only: let any consumer(s) enter their loops
        snap("after_release")
        call("stop", lambda: diag.stop(2.0))
        snap("after_stop")
        call("close", lambda: state.close(1000.0))
        report["quiescent"] = wait_quiescent(5.0)
        snap("final")
    elif scenario == "service":
        # Production route: the maintenance task's entry calls start() on
        # the RUNNING event loop, and the real SIGTERM is delivered inside
        # its Thread.start(). The real handler defers it to a loop callback
        # boundary (S7); asyncio.run() then tears down and the service's
        # final `finally` closes the owner, as aismixer.main() does.
        # Window B: the created thread proceeds 0.2 s later.
        if window == "B":
            timer = threading.Timer(0.2, gate.set)
            timer.daemon = True
            timer.start()

        async def service_main():
            maintenance = asyncio.create_task(
                state.run_periodic_maintenance(interval=3600.0)
            )
            try:
                for _ in range(5):
                    await asyncio.sleep(0.01)
                report["loop_continued_after_start"] = True
                maintenance.cancel()
            finally:
                started = time.monotonic()
                try:
                    report["finally_close"] = state.close(1000.0)
                except BaseException as exc:
                    report["finally_close"] = f"{type(exc).__name__}: {exc}"
                    raise
                finally:
                    report["finally_close_seconds"] = round(
                        time.monotonic() - started, 3
                    )

        try:
            asyncio.run(service_main())
            report["service_exit"] = "normal"
        except KeyboardInterrupt as exc:
            report["service_exit"] = "KeyboardInterrupt"
            report["service_exit_frames"] = [
                entry.name for entry in traceback.extract_tb(exc.__traceback__)
            ]
        gate.set()
        report["quiescent"] = wait_quiescent(5.0)
        snap("final")
    elif scenario == "join":
        # A REAL SIGTERM delivered while the main thread is inside
        # Thread.join() in stop(), with the worker's native thread alive
        # and held inside its sink write. A helper thread sends it only
        # once the main thread's stack shows it is in Thread.join().
        sink_gate = threading.Event()
        sink_entered = threading.Event()

        def stalled_sink(line):
            delivered.append(line)
            sink_entered.set()
            sink_gate.wait(60.0)

        diag.sink = stalled_sink
        call("start_1", diag.start)
        worker = diag._worker
        opened = sess.feed(
            [(sess.nmea_packet("!AIVDM,1,1,,A,x,0*00"), ADDR_B)], 1000.0
        )
        report["opened_challenge"] = [a for _d, a in opened.sent] == [ADDR_B]
        report["worker_blocked_in_sink"] = sink_entered.wait(10.0)
        delivery = {}

        def main_thread_frames():
            frame = sys._current_frames().get(threading.main_thread().ident)
            names = []
            while frame is not None:
                if frame.f_code.co_filename == threading.__file__:
                    names.append(frame.f_code.co_name)
                frame = frame.f_back
            return names

        def deliver_sigterm_inside_join():
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                if "join" in main_thread_frames():
                    time.sleep(0.05)  # let join() block in its native wait
                    delivery["main_frames"] = main_thread_frames()
                    if hasattr(signal, "pthread_kill"):  # POSIX
                        os.kill(os.getpid(), signal.SIGTERM)
                        delivery["how"] = "os.kill(os.getpid(), SIGTERM)"
                    else:  # Windows
                        signal.raise_signal(signal.SIGTERM)
                        delivery["how"] = "signal.raise_signal(SIGTERM)"
                    return
                time.sleep(0.002)
            delivery["how"] = "not sent: main thread never entered join()"

        sender = threading.Thread(target=deliver_sigterm_inside_join, daemon=True)
        sender.start()
        started = time.monotonic()
        try:
            report["stop_1"] = diag.stop(timeout=2.0)
        except KeyboardInterrupt as exc:
            report["stop_1"] = "KeyboardInterrupt"
            report["interrupt_frames"] = [
                entry.name for entry in traceback.extract_tb(exc.__traceback__)
            ]
        except BaseException as exc:
            report["stop_1"] = f"{type(exc).__name__}: {exc}"
        report["stop_1_seconds"] = round(time.monotonic() - started, 3)
        sender.join(10.0)
        report["delivery"] = delivery
        # Critical checkpoint: the original native worker is still alive.
        report["native_live_after_interrupt"] = native["live"]
        # Informational: CPython 3.12 reports False here for a LIVE thread.
        report["is_alive_after_interrupt"] = worker.is_alive()
        snap("after_interrupt")
        call("start_2", diag.start)

        race_results = []
        race_errors = []
        barrier = threading.Barrier(4)

        def racer(kind):
            try:
                barrier.wait(5.0)
                for _ in range(25):
                    if kind == "start":
                        race_results.append(diag.start())
                    elif kind == "stop":
                        diag.stop(timeout=0.02)
                    else:
                        diag.stats()
            except BaseException as exc:
                race_errors.append(repr(exc))

        racers = [
            threading.Thread(target=racer, args=(kind,))
            for kind in ("start", "start", "stop", "stats")
        ]
        for racer_thread in racers:
            racer_thread.start()
        for racer_thread in racers:
            racer_thread.join(30.0)
        report["race_start_true"] = sum(1 for r in race_results if r is True)
        report["race_errors"] = race_errors
        snap("after_race")

        # Real packets while the sink is still stalled: a genuine REPLACED
        # candidate (one PATH_CHALLENGE, to C) and a PONG that decrypts.
        started = time.monotonic()
        replaced = sess.feed(
            [(sess.nmea_packet("!AIVDM,1,1,,A,y,0*00"), ADDR_C)], 1000.0
        )
        pong = sess.feed([(sess.ping_packet(1), ADDR_A)], 1000.0)
        report["packets_while_stalled"] = {
            "challenge_to_c": [a for _d, a in replaced.sent] == [ADDR_C],
            "pong_ok": len(pong.sent) == 1
            and pong.sent[0][1] == ADDR_A
            and sess.decode_server_message(pong.sent[0][0])["type"] == "pong",
            "seconds": round(time.monotonic() - started, 3),
        }
        mp.undo()  # `feed` swaps the module's asyncio; restore the real one
        call("late_maintenance_new_threads", late_maintenance_new_threads)
        call("stop_2", lambda: diag.stop(0.2))
        snap("after_stop_2")

        sink_gate.set()  # the original native worker finishes and exits
        report["quiescent_after_release"] = wait_quiescent(5.0)
        call("start_after_native_exit", diag.start)
        snap("after_native_exit")
        call("close_1", lambda: state.close(1000.0))
        call("close_2", lambda: diag.close(0.2))
        call("start_after_close", diag.start)
        call("late_maintenance_after_close", late_maintenance_new_threads)
        report["quiescent"] = wait_quiescent(5.0)
        snap("final")
    else:
        raise SystemExit(f"unknown scenario {scenario!r}")
except BaseException:
    report["harness_error"] = traceback.format_exc()
finally:
    gate.set()
    sys.stderr.write("REPORT " + json.dumps(report) + "\n")
    sys.stderr.flush()
'''


def _run_sigterm_child(tmp_path, window, scenario, *, unbuffered=False):
    script = tmp_path / "sigterm_child.py"
    script.write_text(_SIGTERM_CHILD, encoding="utf-8")
    command = [sys.executable, "-B"]
    if unbuffered:
        command.append("-u")
    command += [str(script), str(_REPO_ROOT), str(_REPO_ROOT), window, scenario]
    child_env = dict(os.environ)
    child_env.pop("PYTHONUNBUFFERED", None)
    completed = subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=120,
        env=child_env,
        cwd=str(_REPO_ROOT),
    )
    report_lines = [
        line
        for line in completed.stderr.decode("utf-8", "replace").splitlines()
        if line.startswith("REPORT ")
    ]
    report = json.loads(report_lines[-1][len("REPORT "):]) if report_lines else None
    return completed, report


def _assert_sigterm_child_sound(
    completed, report, *, retained=False, deferred=False
):
    stderr = completed.stderr.decode("utf-8", "replace")
    assert completed.returncode == 0, stderr
    assert report is not None, stderr
    assert "harness_error" not in report, report["harness_error"]
    # Outside a running event loop the real handler raises at once; inside
    # the service's running loop (`deferred`) it must NOT raise there.
    assert report.get("handler_did_not_raise", False) is deferred
    assert Path(report["secure_module"]) == _REPO_ROOT / "aismixer_secure.py"
    assert "cannot join thread before it is started" not in stderr
    assert "RuntimeError" not in json.dumps(report)
    final = report["final"]
    assert report["quiescent"] and final["native_live"] == 0
    assert final["native_max"] <= 1  # never two native workers
    assert final["stats"]["closed"] and final["stats"]["balanced"]
    if retained:
        # Intentional fail-closed retention: the worker's exit was never
        # PROVEN, so it stays reserved and reported, never reaped.
        assert final["worker_reserved"]
        assert final["stats"]["worker_exit_uncertain"]
        assert final["stats"]["worker_alive"]
    else:
        assert not final["stats"]["worker_alive"]
        assert not final["stats"]["worker_exit_uncertain"]


@pytest.mark.parametrize("window", ["A", "B"])
def test_s5_real_sigterm_then_owner_close(tmp_path, window):
    completed, report = _run_sigterm_child(tmp_path, window, "shutdown")
    _assert_sigterm_child_sound(completed, report)
    assert report["start_1"] == "KeyboardInterrupt"  # propagated, not swallowed
    interrupted = report["after_interrupt"]
    assert interrupted["worker_reserved"] and not interrupted["worker_is_alive"]
    assert interrupted["stats"]["worker_starting"]
    assert interrupted["native_created"] == (1 if window == "B" else 0)
    packets = report["packets_while_interrupted"]
    assert packets["challenge_to_b"] and packets["pong_ok"]
    assert report["close_1"] is None and report["close_1_seconds"] < 5.0
    after_close = report["after_close_1"]["stats"]
    assert after_close["closed"] and after_close["worker_starting"]
    assert after_close["queued"] == 0
    assert report["close_2"] is None
    assert report["start_after_close"] is False
    assert report["late_maintenance_new_threads"] == 0
    # A: nothing ever existed (still unproven, truthfully). B: the held
    # thread proved its start once released, found the owner closed, exited.
    assert report["final"]["stats"]["worker_starting"] is (window == "A")
    assert report["final"]["native_created"] == (1 if window == "B" else 0)


@pytest.mark.parametrize("window", ["A", "B"])
def test_s5_real_sigterm_then_restart_attempt_never_duplicates(tmp_path, window):
    completed, report = _run_sigterm_child(tmp_path, window, "restart")
    _assert_sigterm_child_sound(completed, report)
    assert report["start_1"] == "KeyboardInterrupt"
    assert report["start_2"] is False  # no replacement while unproven
    assert report["after_start_2"]["native_created"] == (1 if window == "B" else 0)
    packets = report["packets_while_interrupted"]
    assert packets["challenge_to_b"] and packets["pong_ok"]
    if window == "B":
        released = report["after_release"]
        assert released["stats"]["consumer_running"]
        assert not released["stats"]["worker_starting"]
        assert released["native_live"] == 1
        assert report["stop"] is True
        assert not report["after_stop"]["worker_reserved"]
        assert report["final"]["native_max"] == 1
    else:
        assert report["stop"] is False  # truthful: start never proven
        assert report["final"]["stats"]["worker_starting"]
        assert report["final"]["native_created"] == 0


@pytest.mark.parametrize("unbuffered", [False, True], ids=["buffered", "python-u"])
@pytest.mark.parametrize("window", ["A", "B"])
def test_s5_real_sigterm_at_maintenance_entry_service_shutdown(
    tmp_path, window, unbuffered
):
    """Production route: a real SIGTERM arrives inside the maintenance
    task's Thread.start() on the RUNNING service loop. Since S7 the real
    handler no longer raises there: start() completes, and the
    KeyboardInterrupt is raised at the next loop callback boundary, before
    any other task resumes; asyncio.run() tears down and the service's
    final `finally` closes the owner, as aismixer.main() does; the signal
    still ends the service. Window B's thread proceeds 0.2 s later.

    Changed expectations (S7) -- each depended only on the pre-S7 unsafe
    delivery point, INSIDE Thread.start(): the handler now returns there
    (`deferred`), and window A's start therefore completes, so both windows
    end like window B always did: one native worker, proven, stopped and
    reaped. Kept: KeyboardInterrupt still ends the service promptly, the
    owner is closed within the bound, never two workers."""
    completed, report = _run_sigterm_child(
        tmp_path, window, "service", unbuffered=unbuffered
    )
    _assert_sigterm_child_sound(completed, report, deferred=True)
    assert report["service_exit"] == "KeyboardInterrupt"
    # Raised by the loop at a callback boundary, not inside start().
    frames = report["service_exit_frames"]
    assert frames[-1] == "_raise_keyboard_interrupt"
    assert "_run" in frames and "start" not in frames
    assert "loop_continued_after_start" not in report  # still prompt
    assert report["finally_close"] is None
    assert report["finally_close_seconds"] < 5.0
    final = report["final"]
    assert not final["worker_reserved"]  # proven, stopped and reaped
    assert not final["stats"]["worker_starting"]
    assert final["native_max"] == 1 and final["native_created"] == 1


# ==========================================================================
# S6. SIGTERM DURING Thread.join() (Astra Linux blocker). On CPython 3.12 a
# join()/is_alive() interrupted by a raising signal handler's
# KeyboardInterrupt can mark a still-running thread stopped (is_alive()
# False for good). No reaping path may then admit a replacement: the
# reservation is retained (fail-closed) and reported as
# `worker_exit_uncertain`. (The S6 child calls stop() with no event loop
# running, where aismixer's real handler still raises at once.)
# ==========================================================================


@pytest.mark.parametrize("attempt", range(4))
def test_s6_real_sigterm_during_join_never_admits_a_second_worker(
    tmp_path, attempt
):
    """A REAL SIGTERM, sent by a helper thread only once the main thread's
    stack is inside Thread.join(), interrupts stop() while the worker's
    native thread is alive and held in its sink write. Four independent
    child processes per run."""
    completed, report = _run_sigterm_child(tmp_path, "-", "join")
    _assert_sigterm_child_sound(completed, report, retained=True)
    # Non-vacuous: the worker really ran and blocked, the real signal was
    # sent while the main thread was inside join(), and the resulting
    # KeyboardInterrupt came out of join().
    assert report["start_1"] is True
    assert report["opened_challenge"] and report["worker_blocked_in_sink"]
    delivery = report["delivery"]
    assert delivery["how"].startswith(("os.kill", "signal.raise_signal")), delivery
    assert "join" in delivery["main_frames"]
    assert report["stop_1"] == "KeyboardInterrupt"  # propagated, not swallowed
    assert "join" in report["interrupt_frames"]
    # Critical checkpoint: the original native worker is still alive
    # (whatever this runtime's is_alive() now claims).
    assert report["native_live_after_interrupt"] == 1
    after = report["after_interrupt"]
    assert after["worker_reserved"]
    stats = after["stats"]
    assert stats["worker_exit_uncertain"] and stats["worker_alive"]
    assert not stats["consumer_running"] and stats["balanced"]
    # No replacement: immediate, racing, and late-maintenance starts.
    assert report["start_2"] is False
    assert report["race_start_true"] == 0 and report["race_errors"] == []
    assert report["late_maintenance_new_threads"] == 0
    # The real encrypted packet path stays prompt while the sink is stalled.
    packets = report["packets_while_stalled"]
    assert packets["challenge_to_c"] and packets["pong_ok"]
    assert packets["seconds"] < 1.0
    assert report["stop_2"] is False  # a repeated stop keeps the reservation
    # The sink is released and the native worker really exits; ownership
    # is intentionally NOT released (fail-closed until process exit).
    assert report["quiescent_after_release"]
    assert report["start_after_native_exit"] is False
    assert report["after_native_exit"]["stats"]["worker_exit_uncertain"]
    assert report["close_1"] is None and report["close_2"] is False
    assert report["start_after_close"] is False
    assert report["late_maintenance_after_close"] == 0
    final = report["final"]
    assert final["native_max"] == 1 and final["native_created"] == 1


class _EmulatedPy312Thread(threading.Thread):
    """Portable model of the CPython 3.12 runtime defect: a join() or
    is_alive() interrupted by an exception marks the thread stopped while
    it keeps running. Supplements -- never replaces -- the real-signal S6
    child test above, so every platform exercises both probe kinds."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.interrupt_next = None
        self.falsely_stopped = False

    def _maybe_interrupt(self, probe):
        if self.interrupt_next == probe:
            self.interrupt_next = None
            self.falsely_stopped = True
            raise _SimulatedSigterm(f"interrupted inside {probe}()")

    def join(self, timeout=None):
        self._maybe_interrupt("join")
        if not self.falsely_stopped:
            super().join(timeout)

    def is_alive(self):
        self._maybe_interrupt("is_alive")
        return False if self.falsely_stopped else super().is_alive()


@pytest.mark.parametrize("probe", ["join", "is_alive"])
def test_s6_any_interrupted_thread_probe_retains_the_reservation(
    env, monkeypatch, probe
):
    emulated = types.ModuleType("threading_emulated_py312")
    emulated.__dict__.update(threading.__dict__)
    emulated.Thread = _EmulatedPy312Thread
    monkeypatch.setattr(env.secure, "threading", emulated)

    state = env.new_state()
    diag = state.migration_diagnostics
    before = len(_diag_threads())
    release = threading.Event()
    sink = _ScriptedSink(block=release)
    diag.sink = sink
    diag.publish(_valid_event(0))
    assert diag.start()
    worker = diag._worker
    assert sink.entered.wait(5.0)  # native worker alive, held in its write
    try:
        worker.interrupt_next = probe
        with pytest.raises(_SimulatedSigterm):
            if probe == "join":
                diag.stop(timeout=5.0)  # interrupted inside its join()
            else:
                diag.start()  # interrupted inside the reaping probe
        assert not worker.is_alive()  # the modelled defect ...
        assert _live_diag_threads_over(before) == 1  # ... it is still alive
        stats = diag.stats()
        assert stats.worker_exit_uncertain and stats.worker_alive
        for _ in range(3):
            assert diag.start() is False
        assert diag.flush() == 0
        assert diag._worker is worker
        diag.publish(_valid_event(1))
        assert diag.stop(timeout=0.2) is False
        assert diag.start() is False
        assert _accounting_balances(diag.stats())
    finally:
        release.set()

    deadline = time.monotonic() + 5.0
    while _live_diag_threads_over(before) and time.monotonic() < deadline:
        time.sleep(0.005)
    assert _live_diag_threads_over(before) == 0  # the thread really exited
    # ... but ownership is intentionally retained (fail-closed).
    assert diag.start() is False
    assert diag.stats().worker_exit_uncertain
    _maintenance_blip(state)  # late maintenance entry
    assert _live_diag_threads_over(before) == 0
    assert diag.close(timeout=0.2) is False
    assert diag.start() is False
    final = diag.stats()
    assert final.closed and final.worker_exit_uncertain
    assert final.dropped_on_shutdown == 1 and final.queued == 0
    assert _accounting_balances(final)


# ==========================================================================
# S7. SIGTERM CAN STRAND NO LOCK; flush() IS THE SOLE CONSUMER (Astra
# blocker: a real SIGTERM, raised by the handler INSIDE
# threading.Condition.__enter__ after its acquire or __exit__ before its
# release, stranded the diagnostic lock -- the packet path then blocked in
# publish() under the owner lock and shutdown hung; and flush() admitted
# concurrent consumers and lost an event interrupted at its pop).
#
# Layer 1 (aismixer.py): while an event loop runs, the real SIGTERM handler
# raises nothing where the signal lands; it schedules the KeyboardInterrupt
# as a loop callback (once per loop), raised between two callbacks.
# Layer 2 (aismixer_secure.py): the channel takes only plain
# `threading.Lock`s in `with` statements and never waits on a
# Condition/Event on a caller thread; each counter precedes the one call
# that commits it; flush() consumes only as the sole consumer and hands
# that role back with one unskippable store.
#
# S7a  the handler's contract, in-process (no real signal).
# S7b  REAL SIGTERM through the REAL run_service() at every diagnostic
#      boundary of the running service, plus a control proving the harness
#      detects the pre-S7 mechanism.
# S7c  REAL SIGTERM with no event loop running (the handler raises):
#      interrupted acquisition at every entry point, and flush()'s pop,
#      write and settle -- no stranded lock, real packets stay prompt,
#      exact accounting.
# S7d  flush() consumer exclusion/interruption and the idle-poll safety
#      net, in-process.
# ==========================================================================


def _import_aismixer():
    import aismixer

    return aismixer


# --------------------------------------------------------------------------
# S7a. The handler: deferred to one loop callback while a loop runs.
# --------------------------------------------------------------------------


def test_s7a_handler_defers_to_one_loop_callback_per_running_loop():
    """Called as CPython calls it -- in the middle of synchronous code on
    the loop's thread -- the handler returns; the KeyboardInterrupt is
    raised by the loop at its next callback boundary, before the task
    resumes; asyncio.run() then cancels the task at its await point. A
    repeated SIGTERM schedules nothing more (a second KeyboardInterrupt
    would abort that cleanup), and a later service loop gets its own."""
    aismixer = _import_aismixer()
    handler = aismixer._raise_keyboard_interrupt_for_sigterm

    for _service_run in range(2):
        events = []

        async def service():
            assert handler(signal.SIGTERM, None) is None
            assert handler(signal.SIGTERM, None) is None  # repeated
            events.append("handler returned")
            try:
                await asyncio.sleep(0)
                events.append("resumed")  # must never happen
            except asyncio.CancelledError:
                events.append("cancelled at its await")
                raise

        with pytest.raises(KeyboardInterrupt) as info:
            asyncio.run(service())
        assert events == ["handler returned", "cancelled at its await"]
        frames = [entry.name for entry in traceback.extract_tb(info.tb)]
        assert frames[-1] == "_raise_keyboard_interrupt"
        assert "service" not in frames

    # With no running loop there is no safe boundary to wait for.
    with pytest.raises(KeyboardInterrupt):
        handler(signal.SIGTERM, None)


# --------------------------------------------------------------------------
# S7b/S7c child: REAL signals, the REAL handler, hard timeouts, a watchdog
# that reports (never hangs), and native worker threads counted
# independently of Thread.is_alive().
# --------------------------------------------------------------------------

_S7_CHILD = r'''
import asyncio, collections, json, os, signal, sys, threading, time, traceback

repo, mode, case = sys.argv[1:4]
sys.dont_write_bytecode = True
sys.path[:0] = [repo, os.path.join(repo, "tests")]
os.chdir(repo)

import pytest
import core.udpsec_protocol as p
import test_secure_udp_helpers as helpers
from test_udpsec_path_migration import ADDR_A, ADDR_B, ADDR_C, _Env

import aismixer  # the REAL module: its REAL handler and REAL run_service()

WORKER = "udpsec-migration-diagnostics"
POSIX = hasattr(signal, "pthread_kill")
MAIN = threading.main_thread().ident
report = {
    "mode": mode,
    "case": case,
    "python": sys.version.split()[0],
    "platform": sys.platform,
    "signals_sent": 0,
}


def emit():
    sys.stderr.write("REPORT " + json.dumps(report, default=str) + "\n")
    sys.stderr.flush()


def signal_main():
    """A REAL SIGTERM for the main thread: kernel-delivered to it on POSIX;
    on Windows the C runtime's raise(), the path console signals take."""
    report["signals_sent"] += 1
    report.setdefault("first_signal_at", time.monotonic())
    if POSIX:
        signal.pthread_kill(MAIN, signal.SIGTERM)
    else:
        signal.raise_signal(signal.SIGTERM)


def frames_of(exc):
    return [
        (os.path.basename(entry.filename), entry.name)
        for entry in traceback.extract_tb(exc.__traceback__)
    ]


STALL_EXTRA = {}


class Watchdog:
    def __init__(self, label, seconds):
        self.label, self.seconds = label, seconds
        self.done = threading.Event()

    def _watch(self):
        if not self.done.wait(self.seconds):
            frame = sys._current_frames().get(MAIN)
            report["stall"] = self.label
            report["stall_stack"] = traceback.format_stack(frame)[-8:]
            for key, probe in STALL_EXTRA.items():
                report[key] = probe()
            emit()
            os._exit(3)

    def __enter__(self):
        threading.Thread(target=self._watch, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.done.set()


PRIMITIVE = (
    "_start_joinable_thread"
    if hasattr(threading, "_start_joinable_thread")
    else "_start_new_thread"
)
real_primitive = getattr(threading, PRIMITIVE)
native = {"live": 0, "max": 0, "created": 0}
native_lock = threading.Lock()
fire_in_primitive = {"when": None}


def counting_primitive(function, *args, **kwargs):
    thread = getattr(function, "__self__", None)
    if not (isinstance(thread, threading.Thread) and thread.name == WORKER):
        return real_primitive(function, *args, **kwargs)

    def bootstrap():
        with native_lock:
            native["live"] += 1
            native["created"] += 1
            native["max"] = max(native["max"], native["live"])
        try:
            function()
        finally:
            with native_lock:
                native["live"] -= 1

    if fire_in_primitive["when"] == "before":
        fire_in_primitive["when"] = None
        signal_main()  # window A: no native thread exists yet
    result = real_primitive(bootstrap, *args, **kwargs)
    if fire_in_primitive["when"] == "after":
        fire_in_primitive["when"] = None
        signal_main()  # window B: native thread created, not yet started
    return result


setattr(threading, PRIMITIVE, counting_primitive)


class HookDeque(collections.deque):
    """The channel's queue, able to make a REAL SIGTERM pending (on the
    main thread, once) inside the channel's own locked critical sections."""

    fire = None  # (method, calling channel method)

    def _maybe(self, method):
        want = HookDeque.fire
        if want is None or want[0] != method or threading.get_ident() != MAIN:
            return
        if sys._getframe(2).f_code.co_name != want[1]:
            return
        HookDeque.fire = None
        signal_main()

    def append(self, item):
        super().append(item)
        self._maybe("append")

    def popleft(self):
        item = super().popleft()
        self._maybe("popleft")
        return item

    def clear(self):
        super().clear()
        self._maybe("clear")

    def __len__(self):
        self._maybe("len")
        return super().__len__()


mp = pytest.MonkeyPatch()
secure, station_key = helpers.load_secure_module_with_fake_keys(
    mp, with_client_private_key=True
)
REAL_ASYNCIO = secure.asyncio
report["secure_module"] = os.path.abspath(secure.__file__)
env = _Env(mp, secure, station_key)
state = env.new_state(path_candidate_ttl=1000.0, retired_path_grace=1000.0)
sess = env.install_session(state, addr=ADDR_A, now=1000.0)
diag = state.migration_diagnostics
diag._events = HookDeque(diag._events)
delivered = []
diag.sink = delivered.append


def queue_lock():
    return getattr(diag._lock, "_lock", diag._lock)  # the control wraps it


def stranded(window=0.3):
    locks = {"queue": queue_lock(), "lifecycle": diag._lifecycle}
    held = {name: lock.locked() for name, lock in locks.items()}
    deadline = time.monotonic() + window
    while any(held.values()) and time.monotonic() < deadline:
        time.sleep(0.005)
        for name, lock in locks.items():
            if not lock.locked():
                held[name] = False
    return sorted(name for name, value in held.items() if value)


def stats_dict():
    with Watchdog("stats", 10.0):
        st = diag.stats()
    fields = {name: getattr(st, name) for name in st.__dataclass_fields__}
    fields["balanced"] = st.published == (
        st.delivered + st.dropped_overflow + st.undeliverable
        + st.dropped_on_shutdown + st.queued + st.in_flight
    )
    return fields


def packet_check():
    # A REAL encrypted exchange through _secure_server_loop: an off-path
    # NMEA packet opens a candidate (one diagnostic publish, one
    # PATH_CHALLENGE); a PING on the active path gets a PONG that decrypts.
    started = time.monotonic()
    with Watchdog("packets", 10.0):
        opened = sess.feed(
            [(sess.nmea_packet("!AIVDM,1,1,,A,x,0*00"), ADDR_B)], 1000.0
        )
        secure.asyncio = REAL_ASYNCIO
        pong = sess.feed([(sess.ping_packet(1), ADDR_A)], 1000.0)
        secure.asyncio = REAL_ASYNCIO
    return {
        "challenge": [a for _d, a in opened.sent] == [ADDR_B],
        "pong": len(pong.sent) == 1
        and pong.sent[0][1] == ADDR_A
        and sess.decode_server_message(pong.sent[0][0])["type"] == "pong",
        "seconds": round(time.monotonic() - started, 4),
    }


def finish_owner():
    report["stranded"] = stranded()
    report["packets"] = packet_check()
    started = time.monotonic()
    with Watchdog("close", 10.0):
        state.close(1000.0)
    report["close_seconds"] = round(time.monotonic() - started, 4)
    deadline = time.monotonic() + 5.0
    while native["live"] and time.monotonic() < deadline:
        time.sleep(0.005)
    report["native"] = dict(native)
    report["final"] = stats_dict()


def contend_queue_lock(delay=0.1):
    """Hold the queue lock on a helper; once the main thread is blocked
    acquiring it, send the main thread a REAL SIGTERM; then release."""
    taken = threading.Event()

    def holder():
        with queue_lock():
            taken.set()
            time.sleep(delay)
            signal_main()
            time.sleep(delay)

    helper = threading.Thread(target=holder)
    helper.start()
    taken.wait(5.0)
    return helper


# ---------------------------------------------------------------- service
def run_service_case():
    session = sess.session

    async def producer():
        # The packet path's real candidate-install transition (publishes
        # OPENED/REPLACED under the owner lock), run on the service loop.
        addrs = (ADDR_B, ADDR_C)
        i = 0
        while True:
            state.open_or_replace_candidate_path(
                session, session.current_epoch, addrs[i % 2],
                os.urandom(p.PATH_CHALLENGE_TOKEN_BYTES), 1000.0,
            )
            if i % 5 == 0:
                diag.stats()
            i += 1
            if i == 30:
                arm()
                if case == "close":
                    raise RuntimeError("simulated fatal task failure")
            await asyncio.sleep(0.005 if case == "idle" else 0)

    async def churn():
        # start()/stop() on the running loop, as the maintenance entry and
        # exit do.
        while True:
            await asyncio.sleep(0.003)
            diag.stop(0.2)
            diag.start()

    def arm():
        if case == "publish":
            HookDeque.fire = ("append", "publish")
        elif case == "stats":
            HookDeque.fire = ("len", "stats")
        elif case == "start_before":
            fire_in_primitive["when"] = "before"
        elif case == "start_after":
            fire_in_primitive["when"] = "after"
        elif case == "stop_join":
            threading.Thread(target=signal_inside_join, daemon=True).start()
        elif case == "close":
            HookDeque.fire = ("clear", "close")
        elif case == "repeat":
            HookDeque.fire = ("clear", "close")  # the 2nd SIGTERM
            threading.Thread(target=delayed, args=(0.02,), daemon=True).start()
        elif case == "idle":
            threading.Thread(target=delayed, args=(0.05,), daemon=True).start()
        elif case == "natural":
            threading.Thread(target=natural, daemon=True).start()
        elif case == "control":
            control_lock.armed = True

    def delayed(seconds):
        time.sleep(seconds)
        signal_main()

    def natural():
        # Process-directed, at an arbitrary instant of the hot service.
        time.sleep(0.02 + (os.getpid() % 17) / 100.0)
        report["signals_sent"] += 1
        report.setdefault("first_signal_at", time.monotonic())
        if POSIX:
            os.kill(os.getpid(), signal.SIGTERM)
        else:
            signal.raise_signal(signal.SIGTERM)

    def signal_inside_join():
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            frame = sys._current_frames().get(MAIN)
            names = []
            while frame is not None:
                names.append(frame.f_code.co_name)
                frame = frame.f_back
            if "join" in names and "stop" in names:
                report["signalled_inside_join"] = True
                signal_main()
                return
            time.sleep(0.0005)

    if case == "stop_join":
        def slow_sink(line):  # keeps the worker busy, so stop() really joins
            time.sleep(0.02)
            delivered.append(line)
        diag.sink = slow_sink

    if case == "control":
        # Positive control: the PRE-S7 mechanism -- a Python-level
        # Condition around the queue lock, and a handler that raises
        # wherever the signal lands. The harness must detect the strand.
        class _PreS7Condition(threading.Condition):
            armed = False

            def __enter__(self):
                result = self._lock.__enter__()
                if self.armed and threading.get_ident() == MAIN:
                    self.armed = False
                    signal_main()  # raised HERE, after the acquire
                return result

        control_lock = _PreS7Condition(threading.Lock())
        diag._lock = control_lock
        STALL_EXTRA["stall_lock_held"] = lambda: control_lock._lock.locked()

    async def service_main():
        if case == "control":
            def pre_s7_handler(_signum, _frame):
                raise KeyboardInterrupt
            signal.signal(signal.SIGTERM, pre_s7_handler)
        specs = [
            aismixer._RuntimeTaskSpec(
                name="udpsec-state-maintenance",
                coroutine_factory=lambda: state.run_periodic_maintenance(
                    interval=0.02
                ),
            ),
            aismixer._RuntimeTaskSpec(name="producer", coroutine_factory=producer),
        ]
        if case in ("start_before", "start_after", "stop_join"):
            specs.append(
                aismixer._RuntimeTaskSpec(name="churn", coroutine_factory=churn)
            )
        try:
            await aismixer._supervise_named_tasks(specs)
        finally:
            state.close(1000.0)  # as aismixer.main()'s final `finally`

    aismixer.main = service_main
    watchdog = Watchdog("service", 10.0).__enter__()
    try:
        aismixer.run_service()
        report["service_exit"] = "normal"
    except KeyboardInterrupt as exc:
        report["service_exit"] = "KeyboardInterrupt"
        report["exit_frames"] = frames_of(exc)
    except BaseException as exc:
        report["service_exit"] = f"{type(exc).__name__}: {exc}"
        report["exit_frames"] = frames_of(exc)
    exited = time.monotonic()
    watchdog.__exit__()
    if "first_signal_at" in report:
        report["shutdown_seconds"] = round(exited - report["first_signal_at"], 4)
    report["handler_restored"] = (
        signal.getsignal(signal.SIGTERM)
        is not aismixer._raise_keyboard_interrupt_for_sigterm
    )
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    report["stranded"] = stranded()
    deadline = time.monotonic() + 5.0
    while native["live"] and time.monotonic() < deadline:
        time.sleep(0.005)
    report["native"] = dict(native)
    report["final"] = stats_dict()
    report["delivered"] = len(delivered)


# --------------------------------------------------------------- loopless
def event(i):
    return ("candidate_expired", "boat_001", bytes(16), ("198.51.100.1", 40000 + i), i + 1)


def run_loopless_case():
    signal.signal(signal.SIGTERM, aismixer._raise_keyboard_interrupt_for_sigterm)
    for i in range(3):
        diag.publish(event(i))
    kind, _, op = case.partition(":")
    if op in ("stop", "close", "stats", "wait_idle"):
        assert diag.start() and diag.wait_idle(5.0)
    report["before"] = stats_dict()
    helper = []
    calls = {
        "publish": lambda: diag.publish(event(9)),
        "start": diag.start,
        "stop": lambda: diag.stop(1.0),
        "close": lambda: diag.close(1.0),
        "stats": diag.stats,
        "wait_idle": lambda: diag.wait_idle(1.0),
        "flush": diag.flush,
    }
    if kind == "acquire":
        call = calls[op]
        helper.append(contend_queue_lock())
    else:
        call = diag.flush
        if kind == "flush_pop":
            HookDeque.fire = ("popleft", "flush")
        elif kind == "flush_write":
            def firing_sink(line, _once=[True]):
                if _once:
                    _once.clear()
                    signal_main()  # inside the write, before it completes
                delivered.append(line)
            diag.sink = firing_sink
        elif kind == "flush_settle":
            def contending_sink(line, _once=[True]):
                if _once:
                    _once.clear()
                    # flush()'s settle will block on this, and be interrupted.
                    helper.append(contend_queue_lock())
                delivered.append(line)
            diag.sink = contending_sink
    try:
        with Watchdog("interrupted-call", 10.0):
            report["result"] = repr(call())
    except KeyboardInterrupt as exc:
        report["result"] = "KeyboardInterrupt"
        report["frames"] = frames_of(exc)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    for thread in helper:
        thread.join(5.0)
    diag.sink = delivered.append
    report["written_before_recovery"] = len(delivered)
    report["flushing_after_interrupt"] = diag._flush_owner is not None
    report["after"] = stats_dict()
    report["flush_again"] = diag.flush() if kind.startswith("flush") else None
    report["restart"] = diag.start() if kind.startswith("flush") else None
    finish_owner()


try:
    if mode == "service":
        run_service_case()
    elif mode == "loopless":
        run_loopless_case()
    else:
        raise SystemExit(f"unknown mode {mode!r}")
except BaseException:
    report["harness_error"] = traceback.format_exc()
finally:
    emit()
    os._exit(0)
'''


def _run_s7_child(tmp_path, mode, case):
    script = tmp_path / "s7_child.py"
    script.write_text(_S7_CHILD, encoding="utf-8")
    child_env = dict(os.environ)
    child_env.pop("PYTHONUNBUFFERED", None)
    completed = subprocess.run(
        [sys.executable, "-B", str(script), str(_REPO_ROOT), mode, case],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=120,
        env=child_env,
        cwd=str(_REPO_ROOT),
    )
    lines = [
        line
        for line in completed.stderr.decode("utf-8", "replace").splitlines()
        if line.startswith("REPORT ")
    ]
    report = json.loads(lines[-1][len("REPORT "):]) if lines else None
    return completed, report


def _assert_s7_child_ok(completed, report):
    stderr = completed.stderr.decode("utf-8", "replace")
    assert completed.returncode == 0, stderr
    assert report is not None, stderr
    assert "harness_error" not in report, report["harness_error"]
    assert "stall" not in report, report
    assert Path(report["secure_module"]) == _REPO_ROOT / "aismixer_secure.py"


def _assert_s7_owner_sound(report):
    # No stranded diagnostic lock; the REAL encrypted packet path stays
    # prompt; close() is bounded; never two native workers; every event
    # accounted exactly once; nothing left in flight or owned.
    assert report["stranded"] == []
    packets = report["packets"]
    assert packets["challenge"] and packets["pong"], packets
    assert packets["seconds"] < 1.0
    assert report["close_seconds"] < 3.0
    assert report["native"]["max"] <= 1 and report["native"]["live"] == 0
    final = report["final"]
    assert final["closed"] and not final["worker_alive"]
    assert final["balanced"] and final["in_flight"] == 0
    assert final["queued"] == 0 and not final["flushing"]


# --------------------------------------------------------------------------
# S7b. REAL SIGTERM through the REAL run_service() at every diagnostic
#      boundary of the RUNNING service: never raised there, the service
#      still ends promptly, nothing stranded, one worker, exact counts.
# --------------------------------------------------------------------------

_S7B_CASES = [
    "publish",  # inside publish()'s locked section (packet-path transition)
    "stats",  # inside stats()'s locked section
    "start_before",  # inside Thread.start(), before the native thread exists
    "start_after",  # inside Thread.start(), after it exists, before started
    "stop_join",  # while stop() is inside Thread.join() of a busy worker
    "close",  # inside SecureState.close() -> close(), after a task failure
    "repeat",  # a 2nd SIGTERM inside close() while shutdown is under way
    "idle",  # the loop idle in its selector
    "natural",  # process-directed, at an arbitrary instant
]


@pytest.mark.parametrize("case", _S7B_CASES)
def test_s7b_real_sigterm_at_each_boundary_ends_the_service_cleanly(
    tmp_path, case
):
    completed, report = _run_s7_child(tmp_path, "service", case)
    _assert_s7_child_ok(completed, report)
    # Non-vacuous: the real signal really was sent into that boundary.
    assert report["signals_sent"] == (2 if case == "repeat" else 1)
    if case == "stop_join":
        assert report["signalled_inside_join"]
    # SIGTERM still ends the service through KeyboardInterrupt out of
    # run_service() -- raised by the loop at a callback boundary, never
    # inside diagnostic (or any other service) code.
    assert report["service_exit"] == "KeyboardInterrupt"
    frames = report["exit_frames"]
    assert frames[-1] == ["aismixer.py", "_raise_keyboard_interrupt"]
    assert not any(file == "aismixer_secure.py" for file, _name in frames)
    assert report["handler_restored"]
    # Prompt and bounded: the worst case waits out stop()'s 0.2 s join, then
    # the maintenance exit's bounded stop().
    assert report["shutdown_seconds"] < 3.0
    assert report["stranded"] == []
    assert report["native"]["max"] <= 1 and report["native"]["live"] == 0
    final = report["final"]
    assert final["closed"] and not final["worker_alive"]
    assert not final["worker_starting"] and not final["worker_exit_uncertain"]
    assert final["balanced"] and final["in_flight"] == 0 and final["queued"] == 0
    assert final["published"] > 0


def test_s7b_control_the_pre_s7_mechanism_is_detected(tmp_path):
    """Positive control for the S7b harness: with the pre-S7 mechanism
    (a raising handler, and a Python-level Condition around the queue
    lock), the same real SIGTERM inside publish() strands the lock and the
    service's shutdown stalls in the maintenance task's stop(); the
    watchdog reports that instead of hanging."""
    completed, report = _run_s7_child(tmp_path, "service", "control")
    assert completed.returncode == 3, completed.stderr.decode("utf-8", "replace")
    assert report["signals_sent"] == 1
    assert report["stall"] == "service"
    assert report["stall_lock_held"]
    assert any("in stop" in line for line in report["stall_stack"])


# --------------------------------------------------------------------------
# S7c. REAL SIGTERM with NO event loop running, where the real handler
#      raises at once (as a second Ctrl+C would): no entry point, and no
#      flush() step, can strand a lock, lose or duplicate an event, or
#      leave consumption owned.
# --------------------------------------------------------------------------

_posix_blocking_acquire = pytest.mark.skipif(
    not hasattr(signal, "pthread_kill"),
    reason=(
        "needs a SIGTERM that interrupts a blocked lock acquire (POSIX "
        "pthread_kill); on Windows the handler would only run after the "
        "acquisition, which a raising handler can strand on CPython 3.14 "
        "(every lock alike) -- there only the production handler's "
        "deferral (S7a/S7b) protects"
    ),
)


@_posix_blocking_acquire
@pytest.mark.parametrize(
    "op", ["publish", "start", "stop", "close", "stats", "wait_idle", "flush"]
)
def test_s7c_real_sigterm_in_a_blocked_acquire_strands_nothing(tmp_path, op):
    completed, report = _run_s7_child(tmp_path, "loopless", f"acquire:{op}")
    _assert_s7_child_ok(completed, report)
    assert report["signals_sent"] == 1
    # The real handler raised out of the blocked acquisition of this very
    # entry point (propagated, not swallowed).
    assert report["result"] == "KeyboardInterrupt"
    assert report["frames"][-2:] == [
        ["aismixer_secure.py", op],
        ["aismixer.py", "_raise_keyboard_interrupt_for_sigterm"],
    ]
    assert report["after"]["balanced"] and not report["flushing_after_interrupt"]
    _assert_s7_owner_sound(report)


@pytest.mark.parametrize(
    "window",
    [
        "flush_pop",  # at the return of the call that takes the event
        "flush_write",  # inside the sink write, before it completes
        pytest.param("flush_settle", marks=_posix_blocking_acquire),
    ],
)
def test_s7c_real_sigterm_inside_flush_accounts_exactly_once(tmp_path, window):
    completed, report = _run_s7_child(tmp_path, "loopless", window)
    _assert_s7_child_ok(completed, report)
    assert report["signals_sent"] == 1
    assert report["result"] == "KeyboardInterrupt"
    # Consumption was handed back on the way out; the interrupted event is
    # settled exactly once, as undeliverable; nothing lost or doubled.
    assert not report["flushing_after_interrupt"]
    before, after = report["before"], report["after"]
    assert after["balanced"] and after["in_flight"] == 0
    assert not after["flushing"]
    assert after["published"] == before["published"] == 3
    assert after["undeliverable"] == before["undeliverable"] + 1
    assert after["queued"] == 2
    # Honest best-effort limit: a settle interrupted AFTER the write counts
    # an event undeliverable that was in fact written.
    assert report["written_before_recovery"] == (
        1 if window == "flush_settle" else 0
    )
    # The next flush() and start() work.
    assert report["flush_again"] == 2 and report["restart"] is True
    _assert_s7_owner_sound(report)


# --------------------------------------------------------------------------
# S7d. flush() as the sole consumer, in-process (no real signal).
# --------------------------------------------------------------------------


class _ConcurrencySink:
    """Blocks every write until released; records the peak number of
    callers inside it at once (overlapping writers)."""

    def __init__(self):
        self.release = threading.Event()
        self.entered = threading.Event()
        self.lines = []
        self.inside = 0
        self.peak = 0
        self._lock = threading.Lock()

    def __call__(self, line):
        with self._lock:
            self.inside += 1
            self.peak = max(self.peak, self.inside)
        try:
            self.entered.set()
            self.release.wait(10.0)
            self.lines.append(line)
        finally:
            with self._lock:
                self.inside -= 1


def _flush_on_thread(diag, results, key):
    thread = threading.Thread(
        target=lambda: results.__setitem__(key, diag.flush())
    )
    thread.start()
    return thread


def test_s7d_racing_flush_callers_and_start_never_overlap_writes(env):
    state = env.new_state()
    diag = state.migration_diagnostics
    sink = _ConcurrencySink()
    diag.sink = sink
    for i in range(4):
        diag.publish(_valid_event(i))
    results = {}
    first = _flush_on_thread(diag, results, "first")
    try:
        assert sink.entered.wait(5.0)  # the first flush is inside a write
        assert diag.stats().flushing
        second = _flush_on_thread(diag, results, "second")
        second.join(5.0)
        assert results["second"] == 0  # refused: not the sole consumer
        assert diag.flush() == 0  # nor on this thread
        assert diag.start() is False  # no worker beside a flush
        assert sink.peak == 1
    finally:
        sink.release.set()
    first.join(5.0)
    assert results["first"] == 4 and sink.peak == 1
    stats = diag.stats()
    assert not stats.flushing and stats.delivered == 4
    assert _accounting_balances(stats)
    # Consumption handed back: a worker may start now.
    assert diag.start() is True
    assert diag.stop() is True
    assert diag.flush() == 0  # nothing left


@pytest.mark.parametrize("closer", ["close", "stop"])
def test_s7d_close_or_stop_during_flush_is_bounded_and_exact(env, closer):
    state = env.new_state()
    diag = state.migration_diagnostics
    sink = _ConcurrencySink()
    diag.sink = sink
    for i in range(4):
        diag.publish(_valid_event(i))
    results = {}
    flusher = _flush_on_thread(diag, results, "flush")
    try:
        assert sink.entered.wait(5.0)
        started = time.monotonic()
        assert getattr(diag, closer)(0.5) is True  # no worker to wait for
        assert time.monotonic() - started < 2.0
    finally:
        sink.release.set()
    flusher.join(5.0)
    stats = diag.stats()
    assert _accounting_balances(stats) and stats.in_flight == 0
    assert not stats.flushing and sink.peak == 1
    if closer == "close":
        assert results["flush"] == 1 and stats.dropped_on_shutdown == 3
        assert diag.start() is False
    else:
        assert results["flush"] == 4 and stats.delivered == 4


class _InterruptingLock:
    """Stand-in for the channel's queue lock that raises the in-process
    stand-in for a signal's KeyboardInterrupt out of ONE blocked acquire
    made by `_finish` (acquiring nothing), as an interrupted contended
    acquire does."""

    def __init__(self):
        self._lock = threading.Lock()
        self.armed = False

    def __enter__(self):
        if self.armed and sys._getframe(1).f_code.co_name == "_finish":
            self.armed = False
            raise _SimulatedSigterm("inside the settle's blocked acquire")
        return self._lock.__enter__()

    def __exit__(self, *args):
        return self._lock.__exit__(*args)

    def locked(self):
        return self._lock.locked()


@pytest.mark.parametrize("where", ["write", "settle"])
def test_s7d_interrupted_flush_hands_back_and_settles_exactly_once(env, where):
    state = env.new_state()
    diag = state.migration_diagnostics
    for i in range(3):
        diag.publish(_valid_event(i))
    sink = _ScriptedSink()
    diag.sink = sink
    if where == "write":
        sink.exc = _SimulatedSigterm
        sink.fail_first = 1
    else:
        diag._lock = _InterruptingLock()
        diag._lock.armed = True
    with pytest.raises(_SimulatedSigterm):
        diag.flush()
    assert diag._flush_owner is None  # consumption handed back
    stats = diag.stats()  # settles an interrupted settle
    assert not stats.flushing and stats.in_flight == 0
    assert stats.undeliverable == 1 and stats.queued == 2
    assert _accounting_balances(stats)
    assert diag.stats().undeliverable == 1  # settled exactly once
    assert diag.flush() == 2
    # The interrupted settle happened after its write (best-effort limit).
    assert len(sink.lines) == (2 if where == "write" else 3)
    assert diag.start() is True
    assert diag.stop() is True
    assert _accounting_balances(diag.stats())


def test_s7d_no_condition_or_event_on_the_channel(env):
    """Regression guard for the audited mechanism: the channel holds no
    threading.Condition/Event (their Python-level enter/exit/set/wait can
    be interrupted mid-acquire), only plain locks."""
    diag = env.new_state().migration_diagnostics
    for value in vars(diag).values():
        assert not isinstance(value, (threading.Condition, threading.Event))
    assert type(diag._lock) is type(threading.Lock())
    assert type(diag._lifecycle) is type(threading.Lock())


def test_s7d_a_lost_wakeup_is_recovered_by_the_idle_poll(env):
    state = env.new_state()
    diag = state.migration_diagnostics
    sink = _ScriptedSink()
    diag.sink = sink
    assert diag.start()
    try:
        assert diag.wait_idle(5.0)
        time.sleep(0.05)  # the worker is idle, asleep on its wake-up
        diag._wake = lambda: None  # an interrupted publish()'s lost wake-up
        diag.publish(_valid_event(0))
        poll = env.secure.MIGRATION_DIAGNOSTIC_IDLE_POLL_SECONDS
        assert diag.wait_idle(poll * 4 + 2.0)
        assert len(sink.lines) == 1
    finally:
        del diag._wake
        assert diag.stop() is True


# ==========================================================================
# PG. The client status line shows the ACCEPTED, COMMITTED path generation
# (Astra review gap 1): `path_gen=N` is the `active_path_generation` of the
# SAME accepted authenticated PONG whose endpoint is shown beside it (under
# the same last-known marker) -- never a candidate's `path_generation`, the
# PATH_ACK floor on its own, or a default 0 -- and is distinct from
# `epoch=` (the traffic-key generation). See BEHAVIORAL_CONTRACT.md's
# "UDPSEC field diagnostics".
# ==========================================================================

_HEARTBEAT_OBSERVATION = re.compile(
    r" epoch=(\d+) peer=\w+ observed=(\S+) path_gen=(\S+)( last-known)?"
)


def _shown(observed, now):
    endpoint, _age, last_known = observed.display(now)
    return endpoint, observed.display_path_generation(), last_known


def test_pg1_path_gen_travels_with_its_accepted_endpoint_and_never_rolls_back(
    proxy,
):
    observed = proxy._ClientObservedEndpoint()
    assert _shown(observed, 0.0) == (None, None, False)  # unknown, never 0

    observed.observe_pong(("203.0.113.9", 1), 3, 100.0)
    assert _shown(observed, 100.0) == (("203.0.113.9", 1), 3, False)
    # A stale (lower-generation) PONG moves neither endpoint nor generation.
    observed.observe_pong(("198.51.100.1", 2), 2, 110.0)
    assert _shown(observed, 110.0) == (("203.0.113.9", 1), 3, False)
    # Equal refreshes; higher advances both together.
    observed.observe_pong(("203.0.113.9", 1), 3, 120.0)
    assert _shown(observed, 120.0) == (("203.0.113.9", 1), 3, False)
    observed.observe_pong(("198.51.100.1", 2), 4, 130.0)
    assert _shown(observed, 130.0) == (("198.51.100.1", 2), 4, False)


def test_pg2_ack_floor_alone_never_becomes_the_displayed_generation(proxy):
    # An admitted PATH_ACK before any observation shows nothing, not 2.
    observed = proxy._ClientObservedEndpoint()
    observed.raise_floor(2)
    assert _shown(observed, 50.0) == (None, None, False)

    # After a migration's ACK the OLD observation keeps its OLD generation,
    # explicitly last-known; a late pre-migration PONG changes nothing;
    # only a PONG at/above the floor shows the new pair as current.
    observed = proxy._ClientObservedEndpoint()
    observed.observe_pong(("203.0.113.9", 1), 0, 100.0)
    observed.raise_floor(1)
    assert _shown(observed, 150.0) == (("203.0.113.9", 1), 0, True)
    observed.observe_pong(("203.0.113.9", 1), 0, 160.0)
    assert _shown(observed, 160.0) == (("203.0.113.9", 1), 0, True)
    observed.observe_pong(("198.51.100.1", 2), 1, 170.0)
    assert _shown(observed, 170.0) == (("198.51.100.1", 2), 1, False)


def test_pg3_old_server_observation_shows_unknown_generation(proxy):
    observed = proxy._ClientObservedEndpoint()
    # An unversioned (old-server) PONG fills the endpoint; its generation
    # is unknown -- never 0.
    observed.observe_pong(("203.0.113.9", 1), None, 100.0)
    assert _shown(observed, 100.0) == (("203.0.113.9", 1), None, False)
    # A generation-aware PONG then labels it...
    observed.observe_pong(("203.0.113.9", 1), 0, 110.0)
    assert _shown(observed, 110.0) == (("203.0.113.9", 1), 0, False)
    # ...and a later unversioned PONG can replace neither.
    observed.observe_pong(("198.51.100.1", 2), None, 120.0)
    assert _shown(observed, 120.0) == (("203.0.113.9", 1), 0, False)

    # Old server, then an admitted ACK: still unknown, now last-known.
    observed = proxy._ClientObservedEndpoint()
    observed.observe_pong(("203.0.113.9", 1), None, 100.0)
    observed.raise_floor(1)
    assert _shown(observed, 150.0) == (("203.0.113.9", 1), None, True)


@pytest.mark.parametrize(
    "diagnostics, suffix",
    [
        (
            {"observed_endpoint": ("203.0.113.9", 53142), "observed_age": 12.4,
             "observed_path_generation": 1},
            " epoch=2 peer=alive observed=203.0.113.9:53142 path_gen=1 age=12s",
        ),
        (
            {"observed_endpoint": ("2001:db8::10", 53142), "observed_age": 55.0,
             "observed_last_known": True, "observed_path_generation": 0},
            " epoch=2 peer=alive observed=2001:db8::10.53142 path_gen=0"
            " last-known age=55s",
        ),
        (
            {"observed_endpoint": ("203.0.113.9", 53142), "observed_age": 3.0},
            " epoch=2 peer=alive observed=203.0.113.9:53142 path_gen=unknown age=3s",
        ),
        (
            {"observed_endpoint": None},
            " epoch=2 peer=alive observed=unknown path_gen=unknown",
        ),
    ],
    ids=["current", "ipv6-last-known", "old-server", "unknown"],
)
def test_pg4_heartbeat_prints_path_gen_beside_its_endpoint(
    proxy, capsys, diagnostics, suffix
):
    stats = proxy.ForwardingStats()
    proxy.print_forwarding_heartbeat(
        object(),
        proxy.UDPSEC_OUTPUT_TYPE,
        stats,
        session_up=True,
        session_locator=b"\x4e\x8a\x2c\x71" + b"\x00" * 12,
        epoch_generation=2,
        **diagnostics,
    )
    line = capsys.readouterr().out.rstrip("\n")
    assert line.endswith(suffix), line
    assert line.count("path_gen=") == 1


def test_pg4_plain_udp_heartbeat_is_byte_for_byte_unchanged(proxy, capsys):
    stats = proxy.ForwardingStats()
    stats.record_forwarded("!AIVDM,1,1,,A,x,0*00")
    proxy.print_forwarding_heartbeat(object(), proxy.UDP_OUTPUT_TYPE, stats)
    assert capsys.readouterr().out == (
        "Runtime: input=udp output=udp forwarded=1 messages / 20B\n"
    )


@pytest.mark.parametrize(
    "endpoint, generation, expected",
    [
        (("203.0.113.9", 53142), 0, "observed=203.0.113.9:53142 path_gen=0 age="),
        (("203.0.113.9", 53142), None, "observed=203.0.113.9:53142 path_gen=unknown age="),
        (None, None, "observed=unknown path_gen=unknown"),
    ],
    ids=["confirmation-generation-0", "old-server-unversioned", "old-server-none"],
)
def test_pg5_first_heartbeat_shows_the_confirmation_pong_generation(
    proxy, monkeypatch, capsys, endpoint, generation, expected
):
    out = _first_heartbeat_after_confirmation(
        proxy, monkeypatch, capsys, endpoint, generation
    )
    assert out.count("Runtime:") == 1
    assert expected in out, out


def test_pg5_a_new_full_handshake_resets_the_displayed_generation(
    proxy, monkeypatch, capsys
):
    """Diagnostic state lives per confirmed session: a session that showed
    generation 4 leaves no floor or generation behind, so the next full
    handshake's confirmation (generation 0, another endpoint) shows at once
    -- it would be rejected as stale if the state leaked across sessions."""
    first = _first_heartbeat_after_confirmation(
        proxy, monkeypatch, capsys, ("198.51.100.20", 42000), 4
    )
    assert "observed=198.51.100.20:42000 path_gen=4 age=" in first
    monkeypatch.undo()
    second = _first_heartbeat_after_confirmation(
        proxy, monkeypatch, capsys, ("192.0.2.10", 41000), 0
    )
    assert "observed=192.0.2.10:41000 path_gen=0 age=" in second
    assert "path_gen=4" not in second


def _heartbeat_observations(out):
    """(epoch, observed, path_gen, last_known) per Runtime: line."""
    rows = []
    for line in out.splitlines():
        if line.startswith("Runtime:"):
            match = _HEARTBEAT_OBSERVATION.search(line)
            assert match, line
            epoch, endpoint, generation, last_known = match.groups()
            rows.append((int(epoch), endpoint, generation, bool(last_known)))
    return rows


def test_pg6_real_migration_heartbeats_show_each_endpoint_with_its_generation(
    env, proxy, keys, monkeypatch, capsys
):
    """The REAL client forward_loop() against the REAL server loop through a
    NAT remap A -> B and a genuine committed migration: the status line
    shows A with path_gen=0 before, and B with path_gen=1 after -- each
    endpoint only ever with the generation of the PONG that reported it --
    while epoch= stays the traffic-key generation (0: no refresh here)."""
    state = env.new_state(path_candidate_ttl=1000.0, retired_path_grace=1000.0)
    server_sess = _install_matching_session(env, state, keys, addr=ADDR_A, now=1000.0)
    clock = _FakeClock(1000.0)
    shim = _MobileNetworkShim(server_sess, clock)
    shim.external_addr = ADDR_A
    events = {"flipped": False}

    def on_client_message(message, _clk, shim_):
        if (
            not events["flipped"]
            and message
            and message.get("type") == "ping"
            and message.get("seq") == 2
        ):
            events["flipped"] = True
            shim_.external_addr = ADDR_B

    shim.on_client_message = on_client_message
    shim.schedule(1025.0, lambda: setattr(shim, "cutoff", True))
    monkeypatch.setattr(proxy, "HEARTBEAT_INTERVAL_SECONDS", 3.0)

    reason, _ended_at = run_mobile_client(
        proxy, keys, monkeypatch, shim,
        config_overrides={
            "keepalive_interval": 5,
            "peer_timeout": 60,
            "session_refresh_interval": 0,
        },
    )
    assert events["flipped"]
    assert reason == proxy.SESSION_END_PROACTIVE_REKEY
    assert state.path_state_snapshot(server_sess.session, clock.now)[
        "active_path_generation"
    ] == 1

    rows = _heartbeat_observations(capsys.readouterr().out)
    a, b = "192.0.2.10:41000", "198.51.100.20:42000"
    assert all(epoch == 0 for epoch, *_ in rows)
    # Before any PONG: unknown, never 0.
    assert rows[0][1:] == ("unknown", "unknown", False)
    assert (a, "0", False) in [row[1:] for row in rows]  # pre-migration
    assert (b, "1", False) in [row[1:] for row in rows]  # post-migration
    for _epoch, endpoint, generation, _last_known in rows:
        # Provenance: each endpoint only with its own PONG's generation.
        assert (endpoint, generation) in (
            ("unknown", "unknown"), (a, "0"), (b, "1")
        ), rows
    # Never rolled back: once B/1 is shown, A never is again.
    first_b = [row[1] for row in rows].index(b)
    assert all(row[1] == b for row in rows[first_b:]), rows


def test_pg7_in_session_refresh_keeps_the_displayed_generation(
    env, proxy, keys, monkeypatch, capsys
):
    """The REAL client and server complete an in-session CryptoEpoch refresh
    (epoch=0 -> epoch=1); delivery to the client is then cut, so no PONG
    can re-seed the display afterwards. The pre-refresh observation must
    still be shown with its generation under epoch=1 -- a refresh never
    resets diagnostic state (a full handshake does, see PG5)."""
    state = env.new_state(pending_epoch_ttl=1000.0)
    server_sess = _install_matching_session(env, state, keys, addr=ADDR_A, now=1000.0)
    clock = _FakeClock(1000.0)
    shim = _MobileNetworkShim(server_sess, clock)
    shim.external_addr = ADDR_A
    events = {"refreshed": False}

    def on_client_message(message, _clk, _shim):
        # Only the first refresh cycle is under test.
        return bool(
            events["refreshed"]
            and message
            and message.get("type") == p.REFRESH_INIT_TYPE
        )

    def on_server_message(message, _clk, shim_):
        if message and message.get("type") == p.REFRESH_ACK_TYPE:
            events["refreshed"] = True
            shim_.cutoff = True  # this ACK is still delivered; nothing after
        return False

    shim.on_client_message = on_client_message
    shim.on_server_message = on_server_message
    monkeypatch.setattr(proxy, "HEARTBEAT_INTERVAL_SECONDS", 3.0)

    run_mobile_client(
        proxy, keys, monkeypatch, shim,
        config_overrides={
            "keepalive_interval": 5,
            "peer_timeout": 30,
            "session_refresh_interval": 8,
        },
        station_private_key=env.station_private_key,
        server_identity_public_key=env.server_public_key,
    )
    assert events["refreshed"]
    assert server_sess.session.current_epoch.generation == 1

    rows = _heartbeat_observations(capsys.readouterr().out)
    a = "192.0.2.10:41000"
    assert (0, a, "0", False) in rows  # observed before the refresh
    after_refresh = [row for row in rows if row[0] == 1]
    assert after_refresh, rows
    # No PONG after the refresh, yet the same accepted pair is still shown.
    assert all(row[1:] == (a, "0", False) for row in after_refresh), rows
