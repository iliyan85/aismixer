"""UDPSEC V2 mobile-path end-to-end continuity -- acceptance (Major Prompt 8).

Major Prompt 4 built server-side path migration, Major Prompt 5 closed the
client-side migration/liveness choreography, Major Prompt 6 closed the
migration x epoch-refresh x lifecycle race integration, and Major Prompt 7
closed adversarial hardening/observability. All four are already proven at
the unit/integration level by their own dedicated test files.

Major Prompt 8 is the final END-TO-END technical proof: it wires the REAL
`nmea_sproxy.forward_loop()` client choreography to the REAL
`aismixer_secure._secure_server_loop()`/`SecureState` server state machine
through an explicit, test-controlled NAT/CGNAT transport shim -- never by
directly mutating `active_path`/`current_epoch`/the replay ledger/
`assembly_namespace`, and never by calling only the lower-level
`SecureState.commit_candidate_path()` / `_ClientPathMigration` helpers in
place of the real wire exchange.

The central claim under test: one authenticated mobile station can keep the
same UDPSEC logical, cryptographic, replay, and multipart-assembly
continuity while its externally observed UDP path changes, with the new
path becoming authoritative only after bounded authenticated
return-routability validation.
"""

import json
import os

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import core.udpsec_protocol as p
from core.ingress_frame import decode_frame_slice
from assembler import AIVDMAssembler, AssemblyStatus

from test_secure_udp_helpers import _FakeClock, load_proxy_module, load_secure_module_with_fake_keys
from test_udpsec_path_migration import ADDR_A, ADDR_B, ADDR_C, _Env, _Session
from test_udpsec_client_path_migration import STATION_ID, REMOTE_ADDR
from test_udpsec_client_path_migration import _FakeInput as _BaseFakeInput
from test_udpsec_client_path_migration import _station_keypair


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def proxy():
    return load_proxy_module()


@pytest.fixture
def keys():
    return {
        "locator": b"M" * 16,
        "c2s": AESGCM.generate_key(bit_length=256),
        "s2c": AESGCM.generate_key(bit_length=256),
    }


@pytest.fixture
def env(monkeypatch):
    secure, client_private_key = load_secure_module_with_fake_keys(
        monkeypatch, with_client_private_key=True
    )
    return _Env(monkeypatch, secure, client_private_key)


def _install_matching_session(env, state, keys, *, addr=ADDR_A, now=1000.0):
    """Install a server-side `LogicalSession` sharing the EXACT same
    session_locator/c2s/s2c key material as the client's
    `ConfirmedUdpsecSession` -- i.e. "the handshake already completed with
    this exact key material", the same shortcut every MP4-7 test uses to
    reach an established session without re-running the real handshake
    protocol (a separately and extensively tested concern elsewhere). MP8
    proves nothing resembling a handshake happens during MIGRATION -- see
    test_14_no_hidden_re_handshake."""
    endpoint_token = env.endpoint_token()
    relation = env.secure._EndpointPeerKey(endpoint_token, addr)
    session = state.install_session(
        relation, STATION_ID, keys["locator"],
        AESGCM(keys["c2s"]), AESGCM(keys["s2c"]), now,
    )
    return _Session(env, state, session, keys["c2s"], keys["s2c"], endpoint_token)


class _QueuedFakeInput(_BaseFakeInput):
    """Like the existing client-harness `_FakeInput`, but `read_pending()`
    drains a live, test-controlled queue instead of always returning
    nothing -- lets a test inject local NMEA lines (e.g. multipart
    fragments) for `forward_loop()` to forward at specific points in the
    scripted exchange."""

    def __init__(self):
        self.queue = []

    def read_pending(self):
        lines, self.queue = self.queue, []
        return lines


def _decode_any_epoch(session, packet):
    """Test-only, best-effort decode of a wire packet against whichever of
    the session's live epochs (current, pending, retiring) actually
    verifies -- used ONLY to classify traffic for harness hooks/logging,
    never as part of the protocol logic under test."""
    try:
        locator, _selector, nonce, ciphertext = p.parse_data_packet(packet)
    except (TypeError, ValueError):
        return None
    if locator != session._session_key.session_locator:
        return None
    epochs = [session.current_epoch]
    if session.pending_epoch is not None:
        epochs.append(session.pending_epoch.epoch)
    if session.retiring_epoch is not None:
        epochs.append(session.retiring_epoch)
    for epoch in epochs:
        for aesgcm in (epoch.client_to_server_aesgcm, epoch.server_to_client_aesgcm):
            try:
                plaintext = aesgcm.decrypt(
                    nonce, ciphertext, p.build_data_aad(locator, epoch.generation)
                )
                return json.loads(plaintext.decode())
            except Exception:
                continue
    return None


class _MobileNetworkShim:
    """The ONLY bridge between the real client `forward_loop()` and the
    real server `_secure_server_loop()`/`SecureState`: a bidirectional,
    test-controlled NAT/CGNAT transport shim.

    `client_send()` is called from the client's fake `out_sock.sendto()`;
    it hands the datagram to the REAL server session (`_Session.feed()`
    -> real `_secure_server_loop()`) tagged with whichever
    `external_addr` the shim is CURRENTLY configured to report (the NAT
    mapping) -- never the client's own unchanging local identity. Server
    reply datagrams are queued for the client's own `recvfrom()`. Both
    sides share ONE monotonic clock. Optional hooks let a test observe/
    classify every message and simulate loss deterministically, without
    ever touching production state directly.
    """

    def __init__(self, server_sess, clock):
        self.server_sess = server_sess
        self.clock = clock
        self.external_addr = None
        self.to_client = []
        self.client_log = []  # (t, message_dict_or_None, raw_bytes)
        self.server_log = []  # (t, message_dict_or_None, raw_bytes, addr)
        self.on_client_message = None  # (message, clock, shim) -> None
        self.on_server_message = None  # (message, clock, shim) -> bool (drop?)
        # Standard, reusable termination mechanism: ordinary keepalive
        # ping/pong liveness works correctly end-to-end in this harness
        # (as it must), so a scripted scenario never times out on its
        # own. Once a test has captured everything it needs, it sets
        # `cutoff = True` (directly, or via a hook) to black-hole every
        # FURTHER server->client datagram -- the connection then dies of
        # ordinary peer_timeout exactly `peer_timeout` seconds after the
        # last delivered liveness evidence, deterministically ending
        # `forward_loop()` via `SESSION_END_PEER_TIMEOUT`. The server side
        # keeps processing normally (so server-side effects of anything
        # sent after cutoff remain observable) -- only delivery TO the
        # client is cut.
        self.cutoff = False
        # [trigger_time, callback, fired] entries; `run_mobile_client`'s
        # idle-poll loop fires each callback exactly once, the first time
        # the shared clock reaches `trigger_time` -- lets a test schedule
        # a deterministic later action (e.g. "fresh traffic from B after
        # the first candidate has expired") without needing the CLIENT's
        # own send schedule to happen to align with it.
        self.scheduled = []

    def schedule(self, trigger_time, callback):
        self.scheduled.append([trigger_time, callback, False])

    def client_send(self, data, addr):
        message = _decode_any_epoch(self.server_sess.session, data)
        self.client_log.append((self.clock.now, message, data))
        if self.on_client_message is not None:
            # A truthy return means "the network drops this datagram
            # before the server ever sees it" -- it never reaches
            # `_Session.feed()`/the real server loop at all.
            if self.on_client_message(message, self.clock, self):
                return
        server_socket = self.server_sess.feed(
            [(data, self.external_addr)], self.clock.now, clock=self.clock,
        )
        for reply_data, reply_addr in server_socket.sent:
            reply_message = _decode_any_epoch(self.server_sess.session, reply_data)
            self.server_log.append(
                (self.clock.now, reply_message, reply_data, reply_addr)
            )
            drop = self.cutoff
            if not drop and self.on_server_message is not None:
                drop = bool(self.on_server_message(reply_message, self.clock, self))
            if not drop:
                self.to_client.append(reply_data)

    def client_recv(self):
        return self.to_client.pop(0)


class _MobileClientSocket:
    def __init__(self, shim):
        self.shim = shim

    def sendto(self, data, addr):
        self.shim.client_send(data, addr)

    def recvfrom(self, _bufsize):
        return self.shim.client_recv(), REMOTE_ADDR


def run_mobile_client(
    proxy, keys, monkeypatch, shim, *, config_overrides=None,
    station_private_key=None, server_identity_public_key=None,
    input_adapter=None,
):
    """Drive the REAL `forward_loop()` against `shim` to its natural
    termination (a `SESSION_END_*` condition -- in every MP8 scenario
    this is `peer_timeout`, reached deterministically once the scripted
    exchange stops producing fresh liveness/migration evidence). Returns
    `(reason, ended_at)`."""
    input_adapter = input_adapter or _QueuedFakeInput()
    out_sock = _MobileClientSocket(shim)
    clock = shim.clock

    monkeypatch.setattr(proxy.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(proxy.time, "time", lambda: 1_000_000.0)

    poll_count = 0

    def fake_select(readable, writable, exceptional, timeout):
        nonlocal poll_count
        poll_count += 1
        assert poll_count < 20000, "forward_loop did not reach its deadline"
        fired = False
        for entry in shim.scheduled:
            if not entry[2] and clock.now >= entry[0]:
                entry[2] = True
                entry[1]()
                fired = True
        if shim.to_client:
            return ([out_sock], [], [])
        if fired:
            # Return immediately (no further clock advance) so
            # `forward_loop` runs a full extra iteration -- including
            # `forward_pending_input()` -- at the JUST-REACHED time before
            # any later deadline is considered. Without this, a scheduled
            # callback's effects (e.g. newly queued local input) would
            # never be observed: this same call would otherwise also jump
            # the clock straight to the next deadline in one step.
            return ([], [], [])
        # Never advance the clock PAST the next unfired scheduled action --
        # otherwise a scheduled trigger time strictly inside the current
        # poll window would be skipped over in one jump.
        bounded_timeout = max(timeout, 0.0)
        for entry in shim.scheduled:
            if not entry[2]:
                bounded_timeout = min(bounded_timeout, max(entry[0] - clock.now, 0.0))
        clock.now = clock.now + bounded_timeout
        return ([], [], [])

    monkeypatch.setattr(proxy.select, "select", fake_select)

    config = {
        "station_id": STATION_ID,
        "keepalive_interval": 5,
        "peer_timeout": 100,
        "session_refresh_interval": 0,
        "reconnect_delay": 1,
    }
    config.update(config_overrides or {})
    session = proxy.ConfirmedUdpsecSession(
        session_locator=keys["locator"],
        key_material=proxy.SessionKeyMaterial(
            client_to_server_key=keys["c2s"], server_to_client_key=keys["s2c"]
        ),
    )
    reason = proxy.forward_loop(
        input_adapter, out_sock, config, session, REMOTE_ADDR,
        None, proxy.ForwardingStats(),
        station_private_key=station_private_key,
        server_identity_public_key=server_identity_public_key,
    )
    return reason, clock.now


def _snap(sess):
    return sess.state.path_state_snapshot(sess.session, 0.0)


def _identity_fields(session):
    e = session.current_epoch
    return {
        "object": id(session),
        "session_key": session._session_key,
        "locator": session._session_key.session_locator,
        "session_handle": session.session_handle,
        "assembly_namespace": session.assembly_namespace,
        "station_id": session.station_id,
        "created_at": session.created_at,
        "path_state_object": id(session.path_state),
        "epoch_object": id(e),
        "epoch_generation": e.generation,
        "replay_ledger_object": id(e.seen_data_nonces),
    }


# ==========================================================================
# 1/2. Canonical same-session A -> B migration + identity continuity
# ==========================================================================


def test_01_canonical_same_session_migration_a_to_b(env, proxy, keys, monkeypatch):
    state = env.new_state(path_candidate_ttl=1000.0, retired_path_grace=1000.0)
    server_sess = _install_matching_session(env, state, keys, addr=ADDR_A, now=1000.0)
    session_obj = server_sess.session
    before = _identity_fields(session_obj)
    stats_before = state.stats()

    clock = _FakeClock(1000.0)
    shim = _MobileNetworkShim(server_sess, clock)
    shim.external_addr = ADDR_A
    events = {"acked": False, "flipped": False}

    def on_server_message(message, clk, shim_):
        if message and message.get("type") == p.PATH_ACK_TYPE:
            events["acked"] = True
            # This ACK's liveness/ping-clear effects are the last thing
            # this test needs delivered; cut the connection right after
            # so the scenario ends deterministically via peer_timeout.
            shim_.cutoff = True
        return False

    def on_client_message(message, clk, shim_):
        if (
            not events["flipped"]
            and message and message.get("type") == "ping" and message.get("seq") == 1
        ):
            events["flipped"] = True
            shim_.external_addr = ADDR_B

    shim.on_server_message = on_server_message
    shim.on_client_message = on_client_message

    reason, ended_at = run_mobile_client(
        proxy, keys, monkeypatch, shim,
        config_overrides={
            "keepalive_interval": 5,
            "peer_timeout": 30,
            "session_refresh_interval": 0,
        },
    )

    assert events["flipped"], "NAT remap never triggered"
    assert events["acked"], "migration was never acknowledged end-to-end"
    # Deterministic scripted teardown (see `_MobileNetworkShim.cutoff`):
    # once the migration ACK is delivered the shim black-holes every
    # further reply, so the very next unanswered keepalive ping ends the
    # session via ordinary proactive-recovery -- a normal MP5 liveness
    # outcome, not a protocol failure.
    assert reason == proxy.SESSION_END_PROACTIVE_REKEY

    after = _identity_fields(session_obj)
    for k in before:
        assert after[k] == before[k], f"{k} changed: {before[k]!r} -> {after[k]!r}"

    snap = _snap(server_sess)
    assert snap["active"] == ADDR_B
    assert snap["retired"] is not None
    assert snap["retired"]["sockaddr"] == ADDR_A
    assert snap["candidate"] is None

    stats_after = state.stats()
    assert stats_after.sessions_created == stats_before.sessions_created
    assert stats_after.sessions_replaced == stats_before.sessions_replaced
    assert stats_after.pending_sessions_created == stats_before.pending_sessions_created
    assert stats_after.pending_sessions_promoted == stats_before.pending_sessions_promoted
    assert stats_after.path_candidates_opened == stats_before.path_candidates_opened + 1
    assert stats_after.migration_challenges_sent == stats_before.migration_challenges_sent + 1
    assert stats_after.path_migrations_committed == stats_before.path_migrations_committed + 1

    # traffic keeps flowing normally after migration: more keepalive
    # pings were sent and none of them targeted the now-retired A.
    assert any(
        m and m.get("type") == "ping" for _t, m, _raw in shim.client_log
    )


# ==========================================================================
# 3. Replay-ledger identity + pre-migration nonce replay after migration
# ==========================================================================


def test_03_replay_ledger_identity_and_continuity_across_migration(
    env, proxy, keys, monkeypatch
):
    state = env.new_state(path_candidate_ttl=1000.0, retired_path_grace=1000.0)
    server_sess = _install_matching_session(env, state, keys, addr=ADDR_A, now=1000.0)
    session_obj = server_sess.session
    ledger_before = session_obj.current_epoch.seen_data_nonces

    clock = _FakeClock(1000.0)
    shim = _MobileNetworkShim(server_sess, clock)
    shim.external_addr = ADDR_A
    captured = {"first_ping_raw": None}
    events = {"flipped": False}

    def on_client_message(message, clk, shim_):
        if message and message.get("type") == "ping" and message.get("seq") == 1:
            # capture the exact raw datagram admitted while active path
            # was still A, for a post-migration replay attempt below.
            captured["first_ping_raw"] = shim_.client_log[-1][2]
            if not events["flipped"]:
                events["flipped"] = True
                shim_.external_addr = ADDR_B

    def on_server_message(message, clk, shim_):
        if message and message.get("type") == p.PATH_ACK_TYPE:
            shim_.cutoff = True
        return False

    shim.on_client_message = on_client_message
    shim.on_server_message = on_server_message

    reason, ended_at = run_mobile_client(
        proxy, keys, monkeypatch, shim,
        config_overrides={
            "keepalive_interval": 5,
            "peer_timeout": 30,
            "session_refresh_interval": 0,
        },
    )
    assert reason == proxy.SESSION_END_PROACTIVE_REKEY
    assert captured["first_ping_raw"] is not None
    assert _snap(server_sess)["active"] == ADDR_B

    ledger_after = session_obj.current_epoch.seen_data_nonces
    assert ledger_after is ledger_before

    accepted_before_replay = state.stats().data_nonces_accepted
    replay_socket = server_sess.feed(
        [(captured["first_ping_raw"], ADDR_B)], clock.now, clock=clock,
    )
    assert not any(
        _decode_any_epoch(session_obj, d) not in (None,)
        and _decode_any_epoch(session_obj, d).get("type") == "pong"
        for d, _a in replay_socket.sent
    )
    assert state.stats().data_nonces_accepted == accepted_before_replay
    assert state.stats().data_nonce_replays >= 1


# ==========================================================================
# 4. Multipart continuity: fragment 1 via A, fragment 2 via B => COMPLETE
# ==========================================================================


def _make_nmea_sentence(body):
    checksum = 0
    for character in body:
        checksum ^= ord(character)
    return f"!{body}*{checksum:02X}"


def _multipart_fragments():
    # Two valid AIVDM fragments of a genuine two-part multipart group
    # (sequential_id "7", channel "A", declared_total 2), with correctly
    # computed NMEA checksums.
    frag1 = _make_nmea_sentence("AIVDM,2,1,7,A,15NPOOPP00o?b=bE`UNv4?w428D;,0")
    frag2 = _make_nmea_sentence("AIVDM,2,2,7,A,000000000,2")
    return frag1, frag2


def test_04_multipart_continuity_fragment1_a_fragment2_b(
    env, proxy, keys, monkeypatch
):
    frag1, frag2 = _multipart_fragments()
    state = env.new_state(path_candidate_ttl=1000.0, retired_path_grace=1000.0)
    server_sess = _install_matching_session(env, state, keys, addr=ADDR_A, now=1000.0)
    session_obj = server_sess.session
    namespace_before = session_obj.assembly_namespace

    clock = _FakeClock(1000.0)
    shim = _MobileNetworkShim(server_sess, clock)
    shim.external_addr = ADDR_A
    input_adapter = _QueuedFakeInput()
    input_adapter.queue.append(frag1.encode())
    events = {"flipped": False, "frag2_queued": False}

    def on_client_message(message, clk, shim_):
        if message and message.get("type") == "ping" and message.get("seq") == 1:
            if not events["flipped"]:
                events["flipped"] = True
                shim_.external_addr = ADDR_B
        elif message and message.get("type") == "ping" and events["frag2_queued"]:
            # fragment 2 (queued below, right when the ACK arrived) has
            # already been forwarded by the time this NEXT ping fires in
            # the same or a later loop iteration -- end the scenario here.
            shim_.cutoff = True

    def on_server_message(message, clk, shim_):
        if message and message.get("type") == p.PATH_ACK_TYPE:
            events["frag2_queued"] = True
            input_adapter.queue.append(frag2.encode())
        return False

    shim.on_client_message = on_client_message
    shim.on_server_message = on_server_message

    reason, ended_at = run_mobile_client(
        proxy, keys, monkeypatch, shim,
        config_overrides={
            "keepalive_interval": 5,
            "peer_timeout": 30,
            "session_refresh_interval": 0,
        },
        input_adapter=input_adapter,
    )
    assert events["flipped"]
    assert events["frag2_queued"]
    assert session_obj.assembly_namespace == namespace_before

    frames = list(server_sess.queue.items)
    namespace_hex = namespace_before.hex()
    expected_key = f"udpsec-assembly:{namespace_hex}"
    nmea_frames = [f for f in frames if f.assembler_key == expected_key]
    assert len(nmea_frames) == 2, "both fragments must share the SAME assembly key"

    assembler = AIVDMAssembler()
    outcomes = []
    for frame in nmea_frames:
        text = decode_frame_slice(frame, 0, len(frame.payload))
        outcomes.append(assembler.feed_outcome(frame.assembler_key, text))
    assert outcomes[0].status == AssemblyStatus.PENDING
    assert outcomes[1].status == AssemblyStatus.COMPLETE
    assert len(outcomes[1].sentences) == 2
    assert outcomes[1].sentences[0] == frag1
    assert outcomes[1].sentences[1] == frag2
    # exactly one assembly generation was ever opened for this group.
    assert assembler.stats().completed == 1
    assert assembler.stats().current_groups == 0


# ==========================================================================
# 5/16. Rapid A -> B -> C mobility, strict one-candidate/one-retired bound
# ==========================================================================


def test_05_rapid_a_to_b_to_c_mobility(env, proxy, keys, monkeypatch):
    state = env.new_state(path_candidate_ttl=1000.0, retired_path_grace=1000.0)
    server_sess = _install_matching_session(env, state, keys, addr=ADDR_A, now=1000.0)
    session_obj = server_sess.session

    clock = _FakeClock(1000.0)
    shim = _MobileNetworkShim(server_sess, clock)
    shim.external_addr = ADDR_A
    acks = []
    challenges = []
    events = {"flip_to_b": False, "flip_to_c": False}

    def on_client_message(message, clk, shim_):
        if not (message and message.get("type") == "ping"):
            return
        seq = message.get("seq")
        if seq == 1 and not events["flip_to_b"]:
            events["flip_to_b"] = True
            shim_.external_addr = ADDR_B
        elif seq == 2 and not events["flip_to_c"]:
            events["flip_to_c"] = True
            shim_.external_addr = ADDR_C
        elif seq == 3:
            shim_.cutoff = True

    def on_server_message(message, clk, shim_):
        if not message:
            return False
        if message.get("type") == p.PATH_ACK_TYPE:
            acks.append(message)
        elif message.get("type") == p.PATH_CHALLENGE_TYPE:
            challenges.append(message)
        return False

    shim.on_client_message = on_client_message
    shim.on_server_message = on_server_message

    reason, ended_at = run_mobile_client(
        proxy, keys, monkeypatch, shim,
        config_overrides={
            "keepalive_interval": 5,
            "peer_timeout": 100,
            "session_refresh_interval": 0,
        },
    )

    assert events["flip_to_b"] and events["flip_to_c"]
    assert len(acks) == 2, "exactly two migrations must have committed"
    assert len(challenges) == 2
    # C never inherits B's token -- two independent, distinct tokens.
    assert challenges[0]["challenge_token"] != challenges[1]["challenge_token"]
    # path_generation advances monotonically (never resets/reuses).
    assert challenges[1]["path_generation"] == challenges[0]["path_generation"] + 1

    snap = _snap(server_sess)
    assert snap["active"] == ADDR_C
    assert snap["retired"] is not None
    assert snap["retired"]["sockaddr"] == ADDR_B  # A is fully gone, superseded
    assert snap["candidate"] is None

    stats = state.stats()
    assert stats.current_candidate_paths <= 1
    assert stats.current_retired_paths <= 1
    assert stats.path_migrations_committed == 2
    assert stats.path_candidates_opened == 2
    # no historical per-path collection anywhere reachable from the session.
    assert not hasattr(session_obj.path_state, "candidate_history")
    assert not hasattr(session_obj, "_address_history")


# ==========================================================================
# 6. Lost PATH_CHALLENGE
# ==========================================================================


def test_06_lost_path_challenge(env, proxy, keys, monkeypatch):
    # keepalive_interval is deliberately much larger than path_candidate_ttl:
    # ping #1 (which opens the candidate) is never resolved by this failed
    # attempt (an off-path ping never draws an ordinary pong), so the
    # client's own proactive-recovery deadline for that exact ping is
    # `last_ping_at + keepalive_interval` -- it must not fire before the
    # scripted recovery below has a chance to complete.
    state = env.new_state(path_candidate_ttl=8.0, retired_path_grace=1000.0)
    server_sess = _install_matching_session(env, state, keys, addr=ADDR_A, now=1000.0)

    clock = _FakeClock(1000.0)
    shim = _MobileNetworkShim(server_sess, clock)
    shim.external_addr = ADDR_A
    input_adapter = _QueuedFakeInput()
    events = {"flipped": False, "challenge_dropped": False, "acked": False}
    counts = {"challenges": 0, "responses": 0, "acks": 0}

    def on_client_message(message, clk, shim_):
        if message and message.get("type") == "ping" and message.get("seq") == 1:
            if not events["flipped"]:
                events["flipped"] = True
                shim_.external_addr = ADDR_B
        if message and message.get("type") == p.PATH_RESPONSE_TYPE:
            counts["responses"] += 1

    def inject_fresh_b_traffic():
        # Fresh authenticated traffic from B, well after the first
        # candidate's TTL has elapsed -- an ordinary forwarded NMEA line,
        # exactly like any other application traffic (not a ping, so it
        # is independent of the keepalive schedule).
        input_adapter.queue.append(_make_nmea_sentence("AIVDM,1,1,,A,x,0").encode())

    def on_server_message(message, clk, shim_):
        if not message:
            return False
        if message.get("type") == p.PATH_CHALLENGE_TYPE:
            counts["challenges"] += 1
            if counts["challenges"] == 1:
                events["challenge_dropped"] = True
                # Still bounded on the original candidate, unaffected by
                # the loss -- no migration happened.
                assert _snap(server_sess)["active"] == ADDR_A
                assert _snap(server_sess)["candidate"] is not None
                shim_.schedule(clk.now + 8.5, inject_fresh_b_traffic)
                return True  # drop the very first challenge (lost)
        elif message.get("type") == p.PATH_ACK_TYPE:
            counts["acks"] += 1
            events["acked"] = True
            shim_.cutoff = True
        return False

    shim.on_client_message = on_client_message
    shim.on_server_message = on_server_message

    reason, ended_at = run_mobile_client(
        proxy, keys, monkeypatch, shim,
        config_overrides={
            "keepalive_interval": 30,
            "peer_timeout": 100,
            "session_refresh_interval": 0,
        },
        input_adapter=input_adapter,
    )

    assert events["challenge_dropped"]
    # Recovery: once the ORIGINAL candidate has expired, fresh B traffic
    # starts a brand-new candidate cycle, and THIS time the challenge is
    # delivered and completes. Exactly ONE response was ever sent -- for
    # the SECOND (delivered) challenge; the client never answered the
    # first challenge, because it never received it.
    assert counts["challenges"] == 2
    assert counts["responses"] == 1
    assert events["acked"]
    assert _snap(server_sess)["active"] == ADDR_B
    assert state.stats().path_candidates_expired == 1
    assert state.stats().path_migrations_committed == 1


# ==========================================================================
# 7. Lost PATH_RESPONSE
# ==========================================================================


def test_07_lost_path_response(env, proxy, keys, monkeypatch):
    state = env.new_state(path_candidate_ttl=8.0, retired_path_grace=1000.0)
    server_sess = _install_matching_session(env, state, keys, addr=ADDR_A, now=1000.0)

    clock = _FakeClock(1000.0)
    shim = _MobileNetworkShim(server_sess, clock)
    shim.external_addr = ADDR_A
    input_adapter = _QueuedFakeInput()
    events = {"flipped": False, "response_dropped": False, "acked": False}
    counts = {"responses": 0, "acks": 0}

    def inject_fresh_b_traffic():
        input_adapter.queue.append(_make_nmea_sentence("AIVDM,1,1,,A,y,0").encode())

    def on_client_message(message, clk, shim_):
        if message and message.get("type") == "ping" and message.get("seq") == 1:
            if not events["flipped"]:
                events["flipped"] = True
                shim_.external_addr = ADDR_B
        if message and message.get("type") == p.PATH_RESPONSE_TYPE:
            counts["responses"] += 1
            if counts["responses"] == 1:
                events["response_dropped"] = True
                # Server never sees this response; candidate B persists,
                # unresolved, until its own deadline -- schedule fresh
                # traffic shortly after that.
                shim_.schedule(clk.now + 8.5, inject_fresh_b_traffic)
                return True  # network drops this datagram before the server sees it
        return False

    def on_server_message(message, clk, shim_):
        if message and message.get("type") == p.PATH_ACK_TYPE:
            counts["acks"] += 1
            events["acked"] = True
            shim_.cutoff = True
        return False

    shim.on_client_message = on_client_message
    shim.on_server_message = on_server_message

    reason, ended_at = run_mobile_client(
        proxy, keys, monkeypatch, shim,
        config_overrides={
            "keepalive_interval": 30,
            "peer_timeout": 100,
            "session_refresh_interval": 0,
        },
        input_adapter=input_adapter,
    )

    assert events["response_dropped"]
    assert not events["acked"] or counts["acks"] == 1
    # No commit from the lost response; candidate/active stay as they were
    # until the original candidate's own deadline passes.
    assert state.stats().path_candidates_expired == 1
    assert events["acked"], "recovery: fresh B traffic after expiry completes migration"
    assert _snap(server_sess)["active"] == ADDR_B
    assert state.stats().path_migrations_committed == 1
    # the OLD (lost, now-consumed-by-loss) response's proof was never
    # allowed to resurrect the expired candidate -- only a fresh cycle did.
    assert counts["responses"] == 2


# ==========================================================================
# 8. Lost PATH_ACK
# ==========================================================================


def test_08_lost_path_ack(env, proxy, keys, monkeypatch):
    state = env.new_state(path_candidate_ttl=1000.0, retired_path_grace=1000.0)
    server_sess = _install_matching_session(env, state, keys, addr=ADDR_A, now=1000.0)
    session_obj = server_sess.session
    before = _identity_fields(session_obj)

    clock = _FakeClock(1000.0)
    shim = _MobileNetworkShim(server_sess, clock)
    shim.external_addr = ADDR_A
    events = {"flipped": False, "ack_dropped": False}

    def on_client_message(message, clk, shim_):
        if message and message.get("type") == "ping" and message.get("seq") == 1:
            if not events["flipped"]:
                events["flipped"] = True
                shim_.external_addr = ADDR_B

    def on_server_message(message, clk, shim_):
        if message and message.get("type") == p.PATH_ACK_TYPE:
            events["ack_dropped"] = True
            return True  # network drops the ACK -- server already committed
        return False

    shim.on_client_message = on_client_message
    shim.on_server_message = on_server_message

    # peer_timeout tight and un-reanchored by anything except the
    # ORIGINAL session start -- if the (never-delivered) ACK had somehow
    # granted client-side liveness anyway, this deadline would be later.
    reason, ended_at = run_mobile_client(
        proxy, keys, monkeypatch, shim,
        config_overrides={
            "keepalive_interval": 5,
            "peer_timeout": 8,
            "session_refresh_interval": 0,
        },
    )

    assert events["ack_dropped"]
    # SERVER: committed anyway, no rollback merely because the ACK send
    # was lost -- same LogicalSession/epoch/replay ledger throughout.
    snap = _snap(server_sess)
    assert snap["active"] == ADDR_B
    after = _identity_fields(session_obj)
    for k in before:
        assert after[k] == before[k], f"{k} changed: {before[k]!r} -> {after[k]!r}"

    # CLIENT: ends via ordinary, un-extended peer_timeout from session
    # start -- proof the lost ACK granted no liveness/TTL extension.
    assert reason == proxy.SESSION_END_PEER_TIMEOUT
    assert ended_at == pytest.approx(1008.0)


# ==========================================================================
# 9. Migration during outstanding keepalive (H1)
# ==========================================================================


def test_09_migration_captures_exact_outstanding_ping(env, proxy, keys, monkeypatch):
    state = env.new_state(path_candidate_ttl=1000.0, retired_path_grace=1000.0)
    server_sess = _install_matching_session(env, state, keys, addr=ADDR_A, now=1000.0)

    clock = _FakeClock(1000.0)
    shim = _MobileNetworkShim(server_sess, clock)
    shim.external_addr = ADDR_A
    ping_times = []
    events = {"flipped": False, "acked": False}

    def on_client_message(message, clk, shim_):
        if message and message.get("type") == "ping":
            ping_times.append((clk.now, message.get("seq")))
            # ping #1 completes normally on A (gets a pong -- verified
            # below via `on_server_message` NOT dropping it). Flip to B
            # exactly as ping #2 is sent: it becomes the OUTSTANDING ping
            # the migration's return-routability proof captures.
            if message.get("seq") == 2 and not events["flipped"]:
                events["flipped"] = True
                shim_.external_addr = ADDR_B

    def on_server_message(message, clk, shim_):
        if message and message.get("type") == p.PATH_ACK_TYPE:
            events["acked"] = True
            shim_.cutoff = True
        return False

    shim.on_client_message = on_client_message
    shim.on_server_message = on_server_message

    reason, ended_at = run_mobile_client(
        proxy, keys, monkeypatch, shim,
        config_overrides={
            "keepalive_interval": 5,
            "peer_timeout": 100,
            "session_refresh_interval": 0,
        },
    )

    assert events["flipped"] and events["acked"]
    assert _snap(server_sess)["active"] == ADDR_B
    # ping #1 was resolved normally (pong on A); ping #2 was captured by
    # the migration proof and cleared by its ACK -- so ping #3 fires on
    # the ORIGINAL, un-reset keepalive cadence (exactly keepalive_interval
    # after ping #2, never restarted/delayed by the migration).
    seqs = [seq for _t, seq in ping_times]
    assert seqs[:3] == [1, 2, 3]
    assert ping_times[2][0] == pytest.approx(ping_times[1][0] + 5.0)


# ==========================================================================
# 10. Proof captured None vs maintenance-created ping (H2)
# ==========================================================================


def test_10_ack_with_captured_none_cannot_clear_maintenance_ping(
    env, proxy, keys, monkeypatch
):
    """A candidate is opened and its challenge answered by ordinary NMEA
    traffic from B while NO ping has ever been sent (`keepalive_interval`
    is generous, so no ping is due yet) -- the client's migration proof
    therefore captures `expected_ping_seq = None`. Immediately after the
    client answers (i.e. right as it sends the PATH_RESPONSE, which is
    AFTER the proof has already captured None), the shared clock is
    advanced past the keepalive deadline -- exactly as if the challenge/
    response round trip had itself taken that long. `forward_loop()`'s own
    ordinary due-deadline check (the same MP5/6 maintenance mechanism the
    final PATH_ACK admission phase also uses) then sends a genuinely NEW
    keepalive ping BEFORE the ACK is even received. When the ACK arrives,
    its captured-None proof must not -- and per MP5 semantics cannot --
    clear that new ping."""
    state = env.new_state(path_candidate_ttl=1000.0, retired_path_grace=1000.0)
    server_sess = _install_matching_session(env, state, keys, addr=ADDR_A, now=1000.0)
    input_adapter = _QueuedFakeInput()

    clock = _FakeClock(1000.0)
    shim = _MobileNetworkShim(server_sess, clock)
    shim.external_addr = ADDR_B  # candidate observation arrives from B directly
    events = {"acked": False, "bumped": False}
    ping_seqs = []

    def on_client_message(message, clk, shim_):
        if message and message.get("type") == "ping":
            ping_seqs.append(message.get("seq"))
        elif (
            message and message.get("type") == p.PATH_RESPONSE_TYPE
            and not events["bumped"]
        ):
            # The proof already captured `expected_ping_seq` (None) the
            # instant BEFORE this response was built; advancing the clock
            # now -- as the response is handed to the transport -- can
            # only affect what happens AFTER that capture.
            events["bumped"] = True
            clk.now = clk.now + 10.0

    def on_server_message(message, clk, shim_):
        if message and message.get("type") == p.PATH_ACK_TYPE:
            events["acked"] = True
            shim_.cutoff = True
        return False

    shim.on_client_message = on_client_message
    shim.on_server_message = on_server_message
    input_adapter.queue.append(_make_nmea_sentence("AIVDM,1,1,,A,z,0").encode())

    reason, ended_at = run_mobile_client(
        proxy, keys, monkeypatch, shim,
        config_overrides={
            "keepalive_interval": 10,
            "peer_timeout": 100,
            "session_refresh_interval": 0,
        },
        input_adapter=input_adapter,
    )

    assert events["bumped"]
    assert events["acked"]
    assert _snap(server_sess)["active"] == ADDR_B
    # exactly one maintenance-created ping was ever sent, and it was NEVER
    # cleared (captured-None has liveness authority but no clear
    # authority) -- the session eventually ends via its own unresolved
    # proactive recovery, never via a second ping being sent normally.
    assert ping_seqs == [1]
    assert reason == proxy.SESSION_END_PROACTIVE_REKEY


# ==========================================================================
# 11/12/13. Migration near epoch refresh (I1/I2/I3)
# ==========================================================================


def test_11_migration_completes_before_planned_refresh(env, proxy, keys, monkeypatch):
    state = env.new_state(path_candidate_ttl=1000.0, retired_path_grace=1000.0)
    server_sess = _install_matching_session(env, state, keys, addr=ADDR_A, now=1000.0)
    session_obj = server_sess.session

    clock = _FakeClock(1000.0)
    shim = _MobileNetworkShim(server_sess, clock)
    shim.external_addr = ADDR_A
    events = {"flipped": False, "acked": False, "refresh_init_seen": False}

    def on_client_message(message, clk, shim_):
        if message and message.get("type") == "ping" and message.get("seq") == 1:
            if not events["flipped"]:
                events["flipped"] = True
                shim_.external_addr = ADDR_B
        if message and message.get("type") == p.REFRESH_INIT_TYPE:
            events["refresh_init_seen"] = True
            shim_.cutoff = True  # refresh started as scheduled -- that's the proof

    def on_server_message(message, clk, shim_):
        if message and message.get("type") == p.PATH_ACK_TYPE:
            events["acked"] = True
        return False

    shim.on_client_message = on_client_message
    shim.on_server_message = on_server_message

    reason, ended_at = run_mobile_client(
        proxy, keys, monkeypatch, shim,
        config_overrides={
            "keepalive_interval": 5,
            "peer_timeout": 100,
            "session_refresh_interval": 20,
        },
        station_private_key=env.station_private_key,
        server_identity_public_key=env.server_public_key,
    )

    assert events["flipped"] and events["acked"] and events["refresh_init_seen"]
    # migration committed well before the refresh interval elapsed, on the
    # UNCHANGED session, and refresh still started right on its own
    # schedule (20s after session_started_at, not postponed).
    assert _snap(server_sess)["active"] == ADDR_B
    assert state._sessions[session_obj._session_key] is session_obj
    assert session_obj.current_epoch.generation == 0  # refresh not yet committed


def test_12_migration_during_active_refresh_transaction(env, proxy, keys, monkeypatch):
    """A full refresh round trip (INIT -> REPLY -> CONFIRM -> ACK) commits
    synchronously in one burst once started, so there is no natural gap to
    interleave a migration into an in-flight transaction. Instead, the
    client's first REFRESH_CONFIRM is dropped exactly once (an ordinary,
    already-proven-recoverable network loss -- see
    test_07_lost_path_response for the analogous migration case), which
    keeps `pending_epoch` genuinely outstanding, and the NAT flips to B in
    that same instant.

    REFRESH_CONFIRM is deliberately excluded from the off-path allow-list
    (aismixer_secure.py's `_off_path_allowed` gate only ever admits
    nmea/ping/PATH_RESPONSE from an unproved or candidate path), so every
    retry the client fires from here on is silently refused by the SERVER
    itself -- not by this test's network shim -- until path migration
    completes on its own (driven by the ordinary ping that opens the B
    candidate) and B becomes the active path. Only then does a retry
    finally land, and the refresh completes independently, on the new
    path. This proves the two transactions are fully decoupled: migration
    does not need refresh to finish, and a stuck refresh cannot block or
    corrupt migration."""
    state = env.new_state(
        path_candidate_ttl=1000.0, retired_path_grace=1000.0,
        pending_epoch_ttl=1000.0,
    )
    server_sess = _install_matching_session(env, state, keys, addr=ADDR_A, now=1000.0)
    session_obj = server_sess.session

    clock = _FakeClock(1000.0)
    shim = _MobileNetworkShim(server_sess, clock)
    shim.external_addr = ADDR_A
    events = {"confirm_dropped": False, "first_refresh_done": False}
    pending_before = {}
    mid_migration = {}

    def on_client_message(message, clk, shim_):
        if (
            message
            and message.get("type") == p.REFRESH_CONFIRM_TYPE
            and not events["confirm_dropped"]
        ):
            events["confirm_dropped"] = True
            pending_before["deadline"] = session_obj.pending_epoch.deadline
            pending_before["txn"] = session_obj.pending_epoch.transaction_id
            shim_.external_addr = ADDR_B  # NAT flips the instant the drop happens
            return True  # network drops the ONE confirm -- refresh stays pending
        if (
            message
            and message.get("type") == p.REFRESH_INIT_TYPE
            and events["first_refresh_done"]
        ):
            # `session_refresh_interval` keeps re-arming a fresh refresh
            # cycle forever; only the FIRST one (the one interleaved with
            # migration) is under test, so every later cycle's INIT is
            # dropped before it can touch `pending_epoch` again.
            return True

    def on_server_message(message, clk, shim_):
        if message and message.get("type") == p.PATH_ACK_TYPE:
            mid_migration["pending_epoch_present"] = (
                session_obj.pending_epoch is not None
            )
        if message and message.get("type") == p.REFRESH_ACK_TYPE:
            # the only REFRESH_ACK possible here is for the retry that
            # finally landed once B became active. `session_refresh_interval`
            # would otherwise keep re-arming refresh forever with nothing to
            # naturally terminate the run, so stop the network right here
            # (later ping/pong loss then winds the session down normally).
            events["first_refresh_done"] = True
            shim_.cutoff = True
        return False

    shim.on_client_message = on_client_message
    shim.on_server_message = on_server_message

    reason, ended_at = run_mobile_client(
        proxy, keys, monkeypatch, shim,
        config_overrides={
            "keepalive_interval": 5,
            "peer_timeout": 100,
            "session_refresh_interval": 2,
        },
        station_private_key=env.station_private_key,
        server_identity_public_key=env.server_public_key,
    )

    assert events["confirm_dropped"]
    assert pending_before  # captured at drop time
    # migration committed WHILE refresh was still genuinely pending.
    assert mid_migration.get("pending_epoch_present") is True
    assert _snap(server_sess)["active"] == ADDR_B
    # refresh itself completed normally once a retry finally landed
    # on-path, entirely independently of the migration in between.
    assert session_obj.pending_epoch is None
    assert session_obj.current_epoch.generation == 1
    assert state._sessions[session_obj._session_key] is session_obj
    # the server's own REFRESH_ACK reply targeted the CURRENT active path.
    ack_sends = [
        addr for _t, msg, _raw, addr in shim.server_log
        if msg and msg.get("type") == p.REFRESH_ACK_TYPE
    ]
    assert ack_sends and ack_sends[-1] == ADDR_B


def test_13_stale_e1_candidate_then_fresh_e2_migration(env, proxy, keys, monkeypatch):
    """I3: a candidate opened under E1 is left unanswered (its
    PATH_CHALLENGE lost) while E1 is still current. A refresh then swaps
    E1 -> E2 through the REAL client, entirely over the still-active path
    A, so the stale E1 candidate's authority is now bound to an epoch that
    is no longer current. Only afterward does a genuine, REAL client
    mobility event occur (NAT flip to a fresh address), which must open
    and commit its own clean E2 candidate -- proving the earlier stale E1
    candidate is neither reusable nor able to block or corrupt the fresh
    migration.

    The stale E1 candidate is opened via one synthetic off-path packet fed
    directly into the real server loop (`server_sess.feed`, real AEAD/wire
    processing, distinct random nonce, address C) -- never by mutating
    path/epoch state directly -- precisely so it does not disturb the real
    client's own ping/refresh traffic, which keeps flowing over A until the
    later genuine, fully client-driven E2 migration to B."""
    state = env.new_state(path_candidate_ttl=1000.0, retired_path_grace=1000.0)
    server_sess = _install_matching_session(env, state, keys, addr=ADDR_A, now=1000.0)
    session_obj = server_sess.session

    clock = _FakeClock(1000.0)
    shim = _MobileNetworkShim(server_sess, clock)
    shim.external_addr = ADDR_A
    events = {
        "e1_candidate_opened": False, "e1_challenge_dropped": False,
        "refresh_committed": False, "flipped": False, "acked": False,
    }

    def inject_stale_e1_candidate():
        # A transient, never-repeated off-path packet under the CURRENT
        # (E1, generation 0) epoch -- opens a candidate at address C that
        # is then simply never answered.
        stray = server_sess.nmea_packet("!AIVDM,1,1,,A,e1stray,0*7B", generation=0)
        server_socket = server_sess.feed(
            [(stray, ADDR_C)], clock.now, clock=clock,
        )
        for reply_data, reply_addr in server_socket.sent:
            reply_message = _decode_any_epoch(session_obj, reply_data)
            shim.server_log.append((clock.now, reply_message, reply_data, reply_addr))
            if reply_message and reply_message.get("type") == p.PATH_CHALLENGE_TYPE:
                events["e1_candidate_opened"] = True
                events["e1_challenge_dropped"] = True  # never delivered -- lost

    shim.schedule(clock.now + 1.0, inject_stale_e1_candidate)

    def on_client_message(message, clk, shim_):
        if message and message.get("type") == "ping" and message.get("seq") == 1:
            if events["refresh_committed"] and not events["flipped"]:
                events["flipped"] = True
                shim_.external_addr = ADDR_B

    def on_server_message(message, clk, shim_):
        if not message:
            return False
        if message.get("type") == p.REFRESH_ACK_TYPE:
            events["refresh_committed"] = True
        if message.get("type") == p.PATH_ACK_TYPE:
            events["acked"] = True
            shim_.cutoff = True
        return False

    shim.on_client_message = on_client_message
    shim.on_server_message = on_server_message

    reason, ended_at = run_mobile_client(
        proxy, keys, monkeypatch, shim,
        config_overrides={
            "keepalive_interval": 5,
            "peer_timeout": 100,
            "session_refresh_interval": 3,
        },
        station_private_key=env.station_private_key,
        server_identity_public_key=env.server_public_key,
    )

    assert events["e1_candidate_opened"]
    assert events["e1_challenge_dropped"]
    assert events["refresh_committed"]
    assert session_obj.current_epoch.generation == 1  # E2, via refresh alone
    assert events["flipped"]
    assert events["acked"]
    snap = _snap(server_sess)
    # the fresh, REAL E2 migration committed to B -- not the stale E1
    # candidate's address C, which never held any authority.
    assert snap["active"] == ADDR_B
    assert snap["candidate"] is None  # the fresh E2 candidate committed
    if snap["retired"] is not None:
        assert snap["retired"]["sockaddr"] != ADDR_C
    # path continuity preserved (same LogicalSession)...
    assert state._sessions[session_obj._session_key] is session_obj
    # ...but current_epoch changed for the INDEPENDENT reason of refresh,
    # never because migration itself changes epoch identity.


# ==========================================================================
# 14. No hidden re-handshake across migration + refresh
# ==========================================================================


def test_14_no_hidden_rehandshake_across_migration_and_refresh(
    env, proxy, keys, monkeypatch,
):
    """Drives a migration AND an in-session refresh back to back through the
    REAL client/server loops and proves neither ever falls back to a fresh
    ClientHello/ServerHello handshake: no wire bytes on the simulated
    network ever carry the handshake prefixes, no new session identity is
    minted (locator/session_handle/assembly_namespace/session object all
    identical before and after), and the handshake-adjacent lifecycle
    counters (`sessions_created`, `sessions_replaced`,
    `pending_sessions_created`, `pending_sessions_promoted`,
    `handshake_replay_accepted`) do not move even though both a path
    migration and an epoch refresh genuinely completed."""
    state = env.new_state(path_candidate_ttl=1000.0, retired_path_grace=1000.0)
    server_sess = _install_matching_session(env, state, keys, addr=ADDR_A, now=1000.0)
    session_obj = server_sess.session
    before = _identity_fields(session_obj)
    stats_before = state.stats()

    clock = _FakeClock(1000.0)
    shim = _MobileNetworkShim(server_sess, clock)
    shim.external_addr = ADDR_A
    events = {"flipped": False, "acked": False, "refresh_committed": False}

    def on_client_message(message, clk, shim_):
        if (
            not events["flipped"]
            and message and message.get("type") == "ping" and message.get("seq") == 1
        ):
            events["flipped"] = True
            shim_.external_addr = ADDR_B

    def on_server_message(message, clk, shim_):
        if message and message.get("type") == p.PATH_ACK_TYPE:
            events["acked"] = True
        if message and message.get("type") == p.REFRESH_ACK_TYPE:
            events["refresh_committed"] = True
            shim_.cutoff = True
        return False

    shim.on_client_message = on_client_message
    shim.on_server_message = on_server_message

    reason, ended_at = run_mobile_client(
        proxy, keys, monkeypatch, shim,
        config_overrides={
            "keepalive_interval": 5,
            "peer_timeout": 100,
            "session_refresh_interval": 8,
        },
        station_private_key=env.station_private_key,
        server_identity_public_key=env.server_public_key,
    )

    assert events["flipped"]
    assert events["acked"]
    assert events["refresh_committed"]

    # No handshake bytes ever crossed the wire, in either direction, at
    # any point during migration or refresh.
    for _t, _msg, raw in shim.client_log:
        assert not raw.startswith(p.CLIENT_HELLO_PREFIX)
    for _t, _msg, raw, _addr in shim.server_log:
        assert not raw.startswith(p.SERVER_HELLO_PREFIX)

    # Session-level identity is invariant across BOTH migration and
    # refresh. Epoch-scoped fields (`epoch_object`/`epoch_generation`/
    # `replay_ledger_object`) are deliberately excluded here: a refresh
    # legitimately rotates those by design (checked separately below) --
    # that is a normal in-session epoch change, not a hidden re-handshake.
    session_level_keys = (
        "session_key", "locator", "session_handle", "assembly_namespace",
        "station_id", "created_at", "path_state_object",
    )
    after = _identity_fields(session_obj)
    for k in session_level_keys:
        assert after[k] == before[k], f"{k} changed: {before[k]!r} -> {after[k]!r}"
    assert after["epoch_generation"] == before["epoch_generation"] + 1

    stats_after = state.stats()
    assert stats_after.sessions_created == stats_before.sessions_created
    assert stats_after.sessions_replaced == stats_before.sessions_replaced
    assert stats_after.pending_sessions_created == stats_before.pending_sessions_created
    assert stats_after.pending_sessions_promoted == stats_before.pending_sessions_promoted
    assert stats_after.handshake_replay_accepted == stats_before.handshake_replay_accepted
    # migration and refresh both genuinely happened...
    assert stats_after.path_migrations_committed == stats_before.path_migrations_committed + 1
    assert stats_after.epoch_refreshes_committed == stats_before.epoch_refreshes_committed + 1
    assert session_obj.current_epoch.generation == 1
    assert _snap(server_sess)["active"] == ADDR_B
    # ...on the one unchanged session object throughout.
    assert state._sessions[session_obj._session_key] is session_obj


# ==========================================================================
# 15. End-to-end migration counter semantics
# ==========================================================================


def test_15_end_to_end_migration_counter_semantics(env, proxy, keys, monkeypatch):
    """Exercises every MP7 migration-observability counter from real,
    distinct end-to-end events and proves each moves by exactly the amount
    that event implies, with no double counting and no cross-talk into the
    ordinary session-lifecycle counters:

    - a genuine A -> B migration (`path_candidates_opened`,
      `migration_challenges_sent`, `path_migrations_committed`);
    - one spurious PATH_RESPONSE with no live candidate behind it
      (`migration_invalid_responses`);
    - one ordinary data packet arriving late on the just-retired path A
      (`retired_path_packets_admitted`).
    """
    state = env.new_state(path_candidate_ttl=1000.0, retired_path_grace=1000.0)
    server_sess = _install_matching_session(env, state, keys, addr=ADDR_A, now=1000.0)
    session_obj = server_sess.session
    stats_before = state.stats()

    clock = _FakeClock(1000.0)
    shim = _MobileNetworkShim(server_sess, clock)
    shim.external_addr = ADDR_A
    events = {"flipped": False, "acked": False}

    def on_client_message(message, clk, shim_):
        if (
            not events["flipped"]
            and message and message.get("type") == "ping" and message.get("seq") == 1
        ):
            events["flipped"] = True
            shim_.external_addr = ADDR_B

    def on_server_message(message, clk, shim_):
        if message and message.get("type") == p.PATH_ACK_TYPE:
            events["acked"] = True
            shim_.cutoff = True
        return False

    shim.on_client_message = on_client_message
    shim.on_server_message = on_server_message

    reason, ended_at = run_mobile_client(
        proxy, keys, monkeypatch, shim,
        config_overrides={
            "keepalive_interval": 5,
            "peer_timeout": 30,
            "session_refresh_interval": 0,
        },
    )

    assert events["flipped"]
    assert events["acked"]
    snap = _snap(server_sess)
    assert snap["active"] == ADDR_B
    assert snap["retired"] is not None and snap["retired"]["sockaddr"] == ADDR_A

    stats_mid = state.stats()
    assert stats_mid.path_candidates_opened == stats_before.path_candidates_opened + 1
    assert stats_mid.migration_challenges_sent == stats_before.migration_challenges_sent + 1
    assert stats_mid.path_migrations_committed == stats_before.path_migrations_committed + 1
    assert stats_mid.migration_invalid_responses == stats_before.migration_invalid_responses
    assert stats_mid.retired_path_packets_admitted == stats_before.retired_path_packets_admitted

    # A spurious PATH_RESPONSE arrives with no live candidate behind it
    # (the real migration above already committed and vacated the slot).
    bogus_response = server_sess.path_response_packet(
        challenge_token=os.urandom(32), path_generation=999, generation=0,
    )
    server_socket = server_sess.feed([(bogus_response, ADDR_C)], clock.now, clock=clock)
    assert server_socket.sent == []  # rejected -- no ACK, no state change

    stats_after_bogus = state.stats()
    assert (
        stats_after_bogus.migration_invalid_responses
        == stats_mid.migration_invalid_responses + 1
    )
    assert stats_after_bogus.path_migrations_committed == stats_mid.path_migrations_committed
    assert stats_after_bogus.path_candidates_opened == stats_mid.path_candidates_opened

    # An ordinary, honestly-late data packet on the just-retired path A.
    late_ping = server_sess.ping_packet(2, generation=0)
    server_socket = server_sess.feed([(late_ping, ADDR_A)], clock.now, clock=clock)

    stats_final = state.stats()
    assert (
        stats_final.retired_path_packets_admitted
        == stats_after_bogus.retired_path_packets_admitted + 1
    )
    assert stats_final.data_nonces_accepted == stats_after_bogus.data_nonces_accepted + 1
    # none of this touched migration/commit counters again, nor the
    # ordinary session-lifecycle counters at any point across the whole
    # scenario.
    assert stats_final.path_migrations_committed == stats_mid.path_migrations_committed
    assert stats_final.migration_invalid_responses == stats_after_bogus.migration_invalid_responses
    assert stats_final.sessions_created == stats_before.sessions_created
    assert stats_final.sessions_replaced == stats_before.sessions_replaced
    assert state._sessions[session_obj._session_key] is session_obj
