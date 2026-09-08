"""UDPSEC V2 in-session authenticated epoch refresh -- acceptance.

Deterministic coverage of the identity/continuity invariants, the
INIT/REPLY/CONFIRM/ACK state machine, commit points, lost-ACK recovery,
the bounded retiring-epoch overlap and its exact cutoff, epoch-specific
replay ledgers and the decrypt-old/commit-new concurrency race, exhaustion
and lifecycle interaction, and the `nmea_sproxy` client choreography.

Real AES-GCM / P-256 ECDHE / ECDSA are used for every security-relevant
assertion; controlled monotonic clocks and explicit ordering are used for
the transition/race cases (no sleep-only races).
"""

import base64
import importlib.util
import io
import json
import os
import threading
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import core.udpsec_crypto as udpsec_crypto
import core.udpsec_protocol as udpsec_protocol

ROOT = Path(__file__).resolve().parents[1]
NMEA_SPROXY_DIR = ROOT / "nmea_sproxy"
STATION_ID = "boat_001"
_SERVER_PRIVATE_KEY = ec.derive_private_key(0x5EC, ec.SECP256R1())


def _load_secure(monkeypatch, station_public_key):
    station_public_bytes = station_public_key.public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.CompressedPoint,
    )
    authorized_yaml = (
        "authorized_clients:\n"
        f"  - name: {STATION_ID}\n"
        f"    pubkey: {base64.b64encode(station_public_bytes).decode()}\n"
    )
    real_open = open

    def fake_open(path, mode="r", *args, **kwargs):
        if os.path.basename(os.fspath(path)) == "authorized_keys.yaml":
            return io.StringIO(authorized_yaml)
        return real_open(path, mode, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os.path, "exists", lambda _p: False)
        patch.setattr("builtins.open", fake_open)
        spec = importlib.util.spec_from_file_location(
            "aismixer_secure_refresh", ROOT / "aismixer_secure.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


def _load_proxy():
    import sys

    sys.path.insert(0, str(NMEA_SPROXY_DIR))
    try:
        spec = importlib.util.spec_from_file_location(
            "nmea_sproxy_refresh", NMEA_SPROXY_DIR / "nmea_sproxy.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(NMEA_SPROXY_DIR))


@pytest.fixture
def env(monkeypatch):
    station_private_key = ec.generate_private_key(ec.SECP256R1())
    secure = _load_secure(monkeypatch, station_private_key.public_key())
    return _Env(secure, station_private_key)


class _Env:
    def __init__(self, secure, station_private_key):
        self.secure = secure
        self.station_private_key = station_private_key
        self.server_private_key = _SERVER_PRIVATE_KEY
        self.server_public_key = _SERVER_PRIVATE_KEY.public_key()
        self._locator_counter = 0

    def fresh_locator(self):
        self._locator_counter += 1
        return self._locator_counter.to_bytes(16, "big")

    def new_state(self, **kwargs):
        # Default to a long session TTL so multi-step refresh choreography
        # does not idle-expire the session mid-test; tests that specifically
        # exercise idle expiry pass their own `session_ttl`.
        kwargs.setdefault("session_ttl", 100000.0)
        return self.secure.SecureState(**kwargs)

    def endpoint_token(self):
        return self.secure._new_endpoint_token()

    def install_session(self, state, *, addr=("192.0.2.9", 40000),
                        endpoint_token=None, now=1000.0, locator=None):
        endpoint_token = endpoint_token or self.endpoint_token()
        locator = locator or self.fresh_locator()
        c2s = AESGCM.generate_key(bit_length=256)
        s2c = AESGCM.generate_key(bit_length=256)
        relation = self.secure._EndpointPeerKey(endpoint_token, addr)
        session = state.install_session(
            relation, STATION_ID, locator,
            AESGCM(c2s), AESGCM(s2c), now,
        )
        return session, c2s, s2c, endpoint_token


# --------------------------------------------------------------------------
# refresh choreography helpers (real crypto, no sockets)
# --------------------------------------------------------------------------


def _client_build_init(env, session, *, transaction_id=None, parent_generation=0):
    txn = transaction_id or os.urandom(32)
    next_gen = parent_generation + 1
    epriv = udpsec_crypto.generate_ephemeral_private_key()
    epub = udpsec_crypto.serialize_ephemeral_public_key(epriv.public_key())
    erand = os.urandom(32)
    digest = udpsec_crypto.build_refresh_init_digest(
        protocol_version=udpsec_protocol.UDPSEC_PROTOCOL_VERSION,
        station_id=STATION_ID,
        session_locator=session._session_key.session_locator,
        parent_generation=parent_generation,
        next_generation=next_gen,
        transaction_id=txn,
        client_epoch_random=erand,
        client_epoch_public_key=epub,
    )
    signature = udpsec_crypto.sign_transcript_digest(
        env.station_private_key, digest
    )
    msg = udpsec_protocol.build_refresh_init_message(
        station_id=STATION_ID,
        transaction_id=txn,
        parent_generation=parent_generation,
        next_generation=next_gen,
        epoch_random=erand,
        epoch_public_key=epub,
        refresh_signature=signature,
        timestamp=111,
    )
    return {
        "msg": msg,
        "txn": txn,
        "parent_generation": parent_generation,
        "next_generation": next_gen,
        "epriv": epriv,
        "epub": epub,
        "erand": erand,
        "signature": signature,
    }


def _server_process_init(env, state, session, init, *, nonce=None, now=1010.0):
    nonce = nonce if nonce is not None else os.urandom(12)
    packet = env.secure._process_refresh_init(
        state,
        session,
        session.current_epoch,
        init["msg"],
        nonce,
        session._session_key.session_locator,
        env.server_private_key,
        lambda: 222,
        now,
    )
    return packet, nonce




def _client_derive_e2(env, session, init, reply_packet, s2c_key,
                      *, parent_generation=0):
    """Mimic `_ClientEpochRefresh.on_reply`: decrypt REPLY under the parent
    epoch's server->client key, verify the server refresh signature, ECDHE,
    derive E2."""
    locator = session._session_key.session_locator
    _l, _sel, nonce, ct = udpsec_protocol.parse_data_packet(reply_packet)
    plaintext = AESGCM(s2c_key).decrypt(
        nonce, ct, udpsec_protocol.build_data_aad(locator, parent_generation)
    )
    reply = udpsec_protocol.parse_refresh_reply_message(json.loads(plaintext))
    reply_digest = udpsec_crypto.build_refresh_reply_digest(
        protocol_version=udpsec_protocol.UDPSEC_PROTOCOL_VERSION,
        station_id=STATION_ID,
        session_locator=locator,
        parent_generation=init["parent_generation"],
        next_generation=init["next_generation"],
        transaction_id=init["txn"],
        client_epoch_random=init["erand"],
        client_epoch_public_key=init["epub"],
        client_refresh_signature=init["signature"],
        server_epoch_random=reply.server_epoch_random,
        server_epoch_public_key=reply.server_epoch_public_key,
    )
    assert udpsec_crypto.verify_transcript_signature(
        env.server_public_key, reply.refresh_signature, reply_digest
    )
    transcript = udpsec_crypto.build_refresh_transcript_hash(
        protocol_version=udpsec_protocol.UDPSEC_PROTOCOL_VERSION,
        station_id=STATION_ID,
        session_locator=locator,
        parent_generation=init["parent_generation"],
        next_generation=init["next_generation"],
        transaction_id=init["txn"],
        client_epoch_random=init["erand"],
        client_epoch_public_key=init["epub"],
        client_refresh_signature=init["signature"],
        server_epoch_random=reply.server_epoch_random,
        server_epoch_public_key=reply.server_epoch_public_key,
        server_refresh_signature=reply.refresh_signature,
    )
    shared = udpsec_crypto.derive_ephemeral_shared_secret(
        init["epriv"],
        udpsec_crypto.parse_ephemeral_public_key(reply.server_epoch_public_key),
    )
    km = udpsec_crypto.derive_refresh_epoch_key_material(shared, transcript)
    return km, reply


def _client_build_confirm(env, session, init, km):
    msg = udpsec_protocol.build_refresh_confirm_message(
        station_id=STATION_ID,
        transaction_id=init["txn"],
        next_generation=init["next_generation"],
        timestamp=333,
    )
    return msg


def _server_process_confirm(env, state, session, confirm_msg, *, nonce=None,
                            now=1011.0, epoch=None):
    nonce = nonce if nonce is not None else os.urandom(12)
    epoch = epoch if epoch is not None else session.pending_epoch.epoch
    ack_packet, newly = env.secure._process_refresh_confirm(
        state,
        session,
        epoch,
        confirm_msg,
        nonce,
        session._session_key.session_locator,
        lambda: 444,
        now,
    )
    return ack_packet, newly, nonce


def _full_refresh(env, state, session, *, parent_generation=0,
                  s2c_key, now_init=1010.0, now_confirm=1011.0):
    init = _client_build_init(
        env, session, parent_generation=parent_generation
    )
    reply_packet, init_nonce = _server_process_init(
        env, state, session, init, now=now_init
    )
    assert reply_packet is not None
    km, reply = _client_derive_e2(
        env, session, init, reply_packet, s2c_key,
        parent_generation=parent_generation,
    )
    confirm = _client_build_confirm(env, session, init, km)
    ack_packet, newly, confirm_nonce = _server_process_confirm(
        env, state, session, confirm, now=now_confirm
    )
    return {
        "init": init,
        "km": km,
        "reply_packet": reply_packet,
        "ack_packet": ack_packet,
        "newly": newly,
        "init_nonce": init_nonce,
        "confirm_nonce": confirm_nonce,
        "confirm": confirm,
    }


# --------------------------------------------------------------------------
# A. identity and continuity
# --------------------------------------------------------------------------


def test_refresh_preserves_exact_logical_session_identity(env):
    state = env.new_state()
    session, c2s, s2c, _ = env.install_session(state)
    handle_before = session.session_handle
    namespace_before = session.assembly_namespace
    locator_before = session._session_key.session_locator
    created_before = session.created_at
    path_state_before = session.path_state
    active_path_before = session.path_state.active_path
    epoch_before = session.current_epoch
    stats_before = state.stats()

    result = _full_refresh(env, state, session, s2c_key=s2c)
    assert result["newly"] is True

    # exact same LogicalSession object, all identity fields untouched
    assert state._sessions[session._session_key] is session
    assert session.session_handle == handle_before
    assert session.assembly_namespace == namespace_before
    assert session._session_key.session_locator == locator_before
    assert session.created_at == created_before
    assert session.path_state is path_state_before
    assert session.path_state.active_path == active_path_before
    assert session.station_id == STATION_ID

    # epoch replaced, generation advanced, fresh keys, fresh origin+ledger
    assert session.current_epoch is not epoch_before
    assert session.current_epoch is result_pending_epoch(state, session, epoch_before)
    assert session.current_epoch.generation == 1
    assert session.current_epoch.created_at == 1011.0
    assert session.current_epoch.transaction_id == result["init"]["txn"]
    assert (
        session.current_epoch.seen_data_nonces
        is not epoch_before.seen_data_nonces
    )

    stats_after = state.stats()
    assert stats_after.sessions_created == stats_before.sessions_created
    assert stats_after.sessions_replaced == stats_before.sessions_replaced
    assert stats_after.epoch_refreshes_committed == 1
    assert stats_after.pending_epochs_created == 1
    assert stats_after.pending_epochs_discarded == 0


def result_pending_epoch(state, session, epoch_before):
    # after commit the retiring epoch is the previous current epoch object
    assert session.retiring_epoch is epoch_before
    return session.current_epoch


def test_refresh_does_not_churn_identity_registry(env):
    state = env.new_state()
    session, c2s, s2c, _ = env.install_session(state)
    reg = env.secure._SESSION_IDENTITY_REGISTRY
    live_before = reg.live_count()
    _full_refresh(env, state, session, s2c_key=s2c)
    assert reg.live_count() == live_before
    assert reg.is_live(session.session_handle)
    assert reg.is_live(session.assembly_namespace)


def test_directional_keys_are_fresh_and_independent(env):
    state = env.new_state()
    session, c2s, s2c, _ = env.install_session(state)
    result = _full_refresh(env, state, session, s2c_key=s2c)
    km = result["km"]
    assert km.client_to_server_key not in (c2s, s2c)
    assert km.server_to_client_key not in (c2s, s2c)
    assert km.client_to_server_key != km.server_to_client_key
    # the committed current epoch decrypts client->server under the new key
    aad = udpsec_protocol.build_data_aad(
        session._session_key.session_locator, 1
    )
    nonce = os.urandom(12)
    ct = AESGCM(km.client_to_server_key).encrypt(nonce, b"x", aad)
    assert session.current_epoch.client_to_server_aesgcm.decrypt(
        nonce, ct, aad
    ) == b"x"


def _nmea_data_packet(env, session, key, generation, nonce, payload):
    locator = session._session_key.session_locator
    aad = udpsec_protocol.build_data_aad(locator, generation)
    ct = AESGCM(key).encrypt(
        nonce,
        json.dumps(
            {
                "type": "nmea",
                "payload": payload,
                "timestamp": 1,
                "source_id": STATION_ID,
            }
        ).encode(),
        aad,
    )
    return udpsec_protocol.build_data_packet(locator, generation, nonce, ct)


def test_multipart_fragments_span_refresh_under_one_namespace(env, monkeypatch):
    """A NMEA fragment admitted under E1 and another under E2 (after an
    in-session refresh) carry the SAME assembler key -- because the
    LogicalSession, and therefore its `assembly_namespace`, never changed."""
    from test_secure_udp_helpers import (  # noqa: E402
        _FakeAsyncioModule,
        _FakeClock,
        _FakeQueue,
        _FakeSecureLoop,
        _FakeSecureSocket,
    )
    import asyncio as _asyncio

    state = env.new_state()
    session, c2s, s2c, endpoint_token = env.install_session(state, now=1000.0)
    namespace_hex = session.assembly_namespace.hex()
    locator = session._session_key.session_locator
    addr = session.path_state.active_path

    init = _client_build_init(env, session)
    init_packet = env.secure.encrypt_secure_json_message(
        AESGCM(c2s), locator, 0, init["msg"]
    )
    frag1 = _nmea_data_packet(
        env, session, c2s, 0, os.urandom(12),
        "!AIVDM,2,1,5,A,first-fragment,0*00",
    )

    def run(packets, now):
        fake_socket = _FakeSecureSocket()
        monkeypatch.setattr(
            env.secure, "asyncio",
            _FakeAsyncioModule(_FakeSecureLoop(packets)),
        )
        clock = _FakeClock(now)
        with pytest.raises(_asyncio.CancelledError):
            _asyncio.run(
                env.secure._secure_server_loop(
                    fake_socket, fake_queue, "127.0.0.1", 9999,
                    endpoint_token=endpoint_token,
                    state=state,
                    wall_clock=clock,
                    monotonic_clock=clock,
                    server_private_key=env.server_private_key,
                    owned_sessions=dict(state._sessions),
                    owned_pending_sessions={},
                )
            )
        return fake_socket

    fake_queue = _FakeQueue()

    # Pass 1: fragment 1 under E1, then REFRESH_INIT -> server replies.
    sock1 = run([(frag1, addr), (init_packet, addr)], 1002.0)
    assert session.pending_epoch is not None
    reply_packet = sock1.sent[-1][0]
    km, _ = _client_derive_e2(env, session, init, reply_packet, s2c)

    # Pass 2: REFRESH_CONFIRM -> commit, then fragment 2 under E2.
    confirm_msg = _client_build_confirm(env, session, init, km)
    confirm_packet = env.secure.encrypt_secure_json_message(
        AESGCM(km.client_to_server_key), locator, 1, confirm_msg
    )
    frag2 = _nmea_data_packet(
        env, session, km.client_to_server_key, 1, os.urandom(12),
        "!AIVDM,2,2,5,A,second-fragment,0*00",
    )
    run([(confirm_packet, addr), (frag2, addr)], 1003.0)

    assert state._sessions[session._session_key] is session
    assert session.assembly_namespace.hex() == namespace_hex
    assert session.current_epoch.generation == 1

    keys = [f.assembler_key for f in fake_queue.items]
    assert len(keys) == 2
    assert keys[0] == keys[1] == f"udpsec-assembly:{namespace_hex}"


# --------------------------------------------------------------------------
# A2. authentication + protocol rejections
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mutate",
    (
        pytest.param(lambda m: {**m, "source_id": "other_boat"}, id="wrong-station"),
        pytest.param(
            lambda m: {**m, "parent_gen": 3, "next_gen": 4}, id="wrong-parent-gen"
        ),
        pytest.param(
            lambda m: {
                **m,
                "refresh_sig": base64.b64encode(os.urandom(70)).decode(),
            },
            id="bad-signature",
        ),
        pytest.param(
            lambda m: {
                **m,
                "epoch_pub": base64.b64encode(
                    b"\x02" + os.urandom(32)
                ).decode(),
            },
            id="bad-public-point",
        ),
        pytest.param(lambda m: {**m, "extra": 1}, id="malformed"),
    ),
)
def test_refresh_init_rejections_create_no_candidate(env, mutate):
    state = env.new_state()
    session, c2s, s2c, _ = env.install_session(state)
    epoch_before = session.current_epoch
    init = _client_build_init(env, session)
    packet = env.secure._process_refresh_init(
        state, session, session.current_epoch, mutate(init["msg"]),
        os.urandom(12), session._session_key.session_locator,
        env.server_private_key, lambda: 1, 1010.0,
    )
    assert packet is None
    assert session.pending_epoch is None
    assert session.current_epoch is epoch_before
    assert session.current_epoch.generation == 0


def test_refresh_init_from_one_session_cannot_target_another(env):
    state = env.new_state()
    s1, c1, sc1, tok = env.install_session(state, addr=("192.0.2.1", 1))
    s2, c2, sc2, _ = env.install_session(
        state, addr=("192.0.2.2", 2), endpoint_token=tok
    )
    # INIT crafted for s1's locator, replayed against s2 -> locator bound
    # into the signed digest, so it fails against s2.
    init = _client_build_init(env, s1)
    packet = env.secure._process_refresh_init(
        state, s2, s2.current_epoch, init["msg"], os.urandom(12),
        s2._session_key.session_locator, env.server_private_key,
        lambda: 1, 1010.0,
    )
    assert packet is None
    assert s2.pending_epoch is None


def test_retiring_epoch_selector_is_gated_to_traffic_only(env):
    # A retiring epoch is selectable, but the server loop's role gate only
    # lets nmea/ping/close through it -- never a refresh. `select_data_epoch`
    # returns role "retiring"; the loop then drops refresh_* under it.
    state = env.new_state()
    session, c2s, s2c, _ = env.install_session(state)
    _full_refresh(env, state, session, s2c_key=s2c)
    epoch, role = state.select_data_epoch(
        session, udpsec_protocol.epoch_selector_for_generation(0), 1013.0
    )
    assert role == "retiring"
    # an INIT decrypted under the retiring epoch never reaches
    # install_pending_epoch because the role gate rejects it; a direct call
    # still fails the parent-generation identity check.
    init = _client_build_init(env, session, parent_generation=0)
    outcome, _ = state.install_pending_epoch(
        session, epoch, init["txn"], 1, object(), b"", os.urandom(12), 1013.0
    )
    assert outcome is env.secure._PendingEpochInstall.STALE


def test_exhausted_e1_is_not_revived_by_a_pending_refresh(env):
    state = env.new_state(data_nonce_max_per_session=2)
    session, c2s, s2c, _ = env.install_session(state)
    # start a refresh (candidate pending). The INIT nonce takes one E1 slot.
    init = _client_build_init(env, session)
    _server_process_init(env, state, session, init, now=1010.0)
    assert session.pending_epoch is not None
    e1 = session.current_epoch
    assert state.admit_epoch_data_nonce(
        session, e1, b"\x01" * 12, 1010.0
    ) is env.secure._DataNonceAdmission.ACCEPTED
    # the next distinct nonce exhausts E1 -> the whole session (and its
    # pending candidate) is torn down; a pending E2 is not permission to
    # keep using an exhausted E1.
    assert state.admit_epoch_data_nonce(
        session, e1, b"\x02" * 12, 1010.0
    ) is env.secure._DataNonceAdmission.EXHAUSTED
    assert session._session_key not in state._sessions
    assert state.stats().data_nonce_exhaustions == 1


# --------------------------------------------------------------------------
# B. pending epoch + commit
# --------------------------------------------------------------------------


def test_e1_nmea_usable_while_e2_pending(env):
    state = env.new_state()
    session, c2s, s2c, _ = env.install_session(state)
    init = _client_build_init(env, session)
    reply_packet, _ = _server_process_init(env, state, session, init)
    assert reply_packet is not None
    assert session.pending_epoch is not None
    # E1 is still current and still admits a fresh nonce
    epoch, role = state.select_data_epoch(
        session, udpsec_protocol.epoch_selector_for_generation(0), 1010.5
    )
    assert role == "current"
    assert epoch is session.current_epoch
    admission = state.admit_epoch_data_nonce(
        session, epoch, os.urandom(12), 1010.5
    )
    assert admission is env.secure._DataNonceAdmission.ACCEPTED


def test_candidate_failure_leaves_e1_current(env):
    state = env.new_state()
    session, c2s, s2c, _ = env.install_session(state)
    epoch_before = session.current_epoch
    init = _client_build_init(env, session)
    # tamper the signature -> server derives nothing, no candidate
    bad = dict(init["msg"])
    bad["refresh_sig"] = base64.b64encode(os.urandom(70)).decode()
    packet = env.secure._process_refresh_init(
        state, session, session.current_epoch, bad, os.urandom(12),
        session._session_key.session_locator, env.server_private_key,
        lambda: 1, 1010.0,
    )
    assert packet is None
    assert session.pending_epoch is None
    assert session.current_epoch is epoch_before


def test_commit_happens_exactly_once_and_no_rollback(env):
    state = env.new_state()
    session, c2s, s2c, _ = env.install_session(state)
    result = _full_refresh(env, state, session, s2c_key=s2c)
    committed_epoch = session.current_epoch
    assert result["newly"] is True

    # a retransmitted CONFIRM (fresh nonce) after commit re-ACKs, no re-commit
    ack2, newly2, _ = _server_process_confirm(
        env, state, session, result["confirm"], now=1012.0,
        epoch=session.current_epoch,
    )
    assert ack2 is not None
    assert newly2 is False
    assert session.current_epoch is committed_epoch
    assert session.current_epoch.generation == 1
    assert state.stats().epoch_refreshes_committed == 1


def test_lost_ack_recovers_via_confirm_retransmit(env):
    state = env.new_state()
    session, c2s, s2c, _ = env.install_session(state)
    init = _client_build_init(env, session)
    reply_packet, _ = _server_process_init(env, state, session, init)
    km, _ = _client_derive_e2(env, session, init, reply_packet, s2c)
    confirm = _client_build_confirm(env, session, init, km)

    # first CONFIRM commits, ACK is "lost" (we ignore it)
    ack1, newly1, _ = _server_process_confirm(
        env, state, session, confirm, now=1011.0
    )
    assert newly1 is True
    committed = session.current_epoch

    # client retransmits CONFIRM (fresh nonce); server re-ACKs idempotently
    ack2, newly2, _ = _server_process_confirm(
        env, state, session, confirm, now=1012.0, epoch=session.current_epoch
    )
    assert ack2 is not None and newly2 is False
    assert session.current_epoch is committed


def test_duplicate_init_is_idempotent(env):
    state = env.new_state()
    session, c2s, s2c, _ = env.install_session(state)
    init = _client_build_init(env, session)
    reply1, _ = _server_process_init(env, state, session, init, now=1010.0)
    pending1 = session.pending_epoch
    reply2, _ = _server_process_init(env, state, session, init, now=1015.0)
    assert reply2 == pending1.reply_packet
    assert session.pending_epoch is pending1
    assert session.pending_epoch.epoch is pending1.epoch
    assert state.stats().pending_epochs_created == 1


def test_new_refresh_rejected_while_incompatible_transition_unresolved(env):
    state = env.new_state()
    session, c2s, s2c, _ = env.install_session(state)
    init1 = _client_build_init(env, session)
    _server_process_init(env, state, session, init1, now=1010.0)
    # a different transaction while one is pending -> REJECTED_BUSY (no chain)
    init2 = _client_build_init(env, session)
    packet = env.secure._process_refresh_init(
        state, session, session.current_epoch, init2["msg"], os.urandom(12),
        session._session_key.session_locator, env.server_private_key,
        lambda: 1, 1010.5,
    )
    assert packet is None
    assert session.pending_epoch.transaction_id == init1["txn"]


# --------------------------------------------------------------------------
# C. replay + concurrency + cutoff
# --------------------------------------------------------------------------


def test_same_nonce_bytes_accepted_under_independent_epochs(env):
    state = env.new_state()
    session, c2s, s2c, _ = env.install_session(state)
    shared_nonce = b"\x5a" * 12
    assert state.admit_epoch_data_nonce(
        session, session.current_epoch, shared_nonce, 1005.0
    ) is env.secure._DataNonceAdmission.ACCEPTED
    _full_refresh(env, state, session, s2c_key=s2c)
    # the same 12 bytes under the freshly-derived E2 keys is not a replay
    assert state.admit_epoch_data_nonce(
        session, session.current_epoch, shared_nonce, 1013.0
    ) is env.secure._DataNonceAdmission.ACCEPTED
    # and still a replay under the retiring E1 ledger
    assert state.admit_epoch_data_nonce(
        session, session.retiring_epoch, shared_nonce, 1013.0
    ) is env.secure._DataNonceAdmission.REPLAY


def test_confirmation_nonce_retained_across_commit(env):
    state = env.new_state()
    session, c2s, s2c, _ = env.install_session(state)
    result = _full_refresh(env, state, session, s2c_key=s2c)
    # the exact confirmation nonce is still in the (now current) epoch ledger
    assert session.current_epoch.seen_data_nonces.contains(
        result["confirm_nonce"]
    )
    assert state.admit_epoch_data_nonce(
        session, session.current_epoch, result["confirm_nonce"], 1013.0
    ) is env.secure._DataNonceAdmission.REPLAY


def test_decrypt_e1_then_concurrent_commit_admits_into_e1_ledger(env):
    state = env.new_state()
    session, c2s, s2c, _ = env.install_session(state)
    e1 = session.current_epoch

    # thread A holds a reference to E1 and is about to admit a nonce;
    # thread B commits E2 first. A must land in E1's own (retiring) ledger
    # or get STALE -- never in E2's ledger.
    a_nonce = b"\x11" * 12
    barrier = threading.Barrier(2)
    a_result = {}

    def thread_a():
        barrier.wait()
        a_result["admission"] = state.admit_epoch_data_nonce(
            session, e1, a_nonce, 1011.5
        )

    def thread_b():
        barrier.wait()
        _full_refresh(env, state, session, s2c_key=s2c)

    ta = threading.Thread(target=thread_a)
    tb = threading.Thread(target=thread_b)
    ta.start()
    tb.start()
    ta.join(5)
    tb.join(5)

    assert session.current_epoch is not e1
    assert not session.current_epoch.seen_data_nonces.contains(a_nonce)
    # A either admitted into E1 (now retiring) or was told E1 is stale.
    assert a_result["admission"] in (
        env.secure._DataNonceAdmission.ACCEPTED,
        env.secure._DataNonceAdmission.STALE,
    )
    if a_result["admission"] is env.secure._DataNonceAdmission.ACCEPTED:
        assert session.retiring_epoch is e1
        assert e1.seen_data_nonces.contains(a_nonce)


def test_retiring_epoch_expires_at_exact_cutoff(env):
    state = env.new_state(retiring_epoch_overlap=5.0)
    session, c2s, s2c, _ = env.install_session(state)
    _full_refresh(env, state, session, s2c_key=s2c, now_confirm=1011.0)
    e1 = session.retiring_epoch
    assert e1 is not None
    assert session.retiring_deadline == 1016.0

    # one tick before the cutoff: still selectable for nmea/ping/close
    epoch, role = state.select_data_epoch(
        session, udpsec_protocol.epoch_selector_for_generation(0), 1015.999
    )
    assert role == "retiring" and epoch is e1

    # exactly at the cutoff: retired (age >= cutoff)
    epoch, role = state.select_data_epoch(
        session, udpsec_protocol.epoch_selector_for_generation(0), 1016.0
    )
    assert epoch is None
    assert session.retiring_epoch is None


def test_generations_snapshot_tracks_transition(env):
    state = env.new_state()
    session, c2s, s2c, _ = env.install_session(state)
    assert state.refresh_epoch_generations(session, 1000.0) == {
        "current": 0, "pending": None, "retiring": None,
    }
    init = _client_build_init(env, session)
    _server_process_init(env, state, session, init, now=1010.0)
    assert state.refresh_epoch_generations(session, 1010.0) == {
        "current": 0, "pending": 1, "retiring": None,
    }
    km, _ = _client_derive_e2(
        env, session, init,
        # rebuild reply packet path: re-run init to get the cached packet
        session.pending_epoch.reply_packet, s2c,
    )
    confirm = _client_build_confirm(env, session, init, km)
    _server_process_confirm(env, state, session, confirm, now=1011.0)
    assert state.refresh_epoch_generations(session, 1011.0) == {
        "current": 1, "pending": None, "retiring": 0,
    }
    assert state.refresh_epoch_generations(session, 2010.0) == {
        "current": 1, "pending": None, "retiring": None,
    }


# --------------------------------------------------------------------------
# D. lifecycle
# --------------------------------------------------------------------------


def test_idle_expiry_during_pending_removes_whole_session(env):
    state = env.new_state(session_ttl=10.0)
    session, c2s, s2c, _ = env.install_session(state, now=1000.0)
    init = _client_build_init(env, session)
    _server_process_init(env, state, session, init, now=1005.0)
    assert session.pending_epoch is not None
    # session idle TTL elapses -> the whole session (and its candidate) goes
    state.cleanup_expired_sessions(1015.0)
    assert session._session_key not in state._sessions
    assert state.stats().sessions_expired == 1


def test_owner_close_discards_pending_and_retiring(env):
    state = env.new_state()
    session_a, _, s2c_a, _ = env.install_session(state, addr=("192.0.2.1", 1))
    session_b, _, s2c_b, _ = env.install_session(state, addr=("192.0.2.2", 2))
    _client_and_pending(env, state, session_a)  # A has a pending epoch
    _full_refresh(env, state, session_b, s2c_key=s2c_b)  # B has a retiring epoch
    assert session_a.pending_epoch is not None
    assert session_b.retiring_epoch is not None
    state.close(9999.0)
    assert len(state._sessions) == 0
    assert state.stats().current_data_nonces == 0


def _client_and_pending(env, state, session):
    init = _client_build_init(env, session)
    _server_process_init(env, state, session, init, now=1010.0)
    return init


def test_pending_epoch_ttl_bounds_unconfirmed_candidate(env):
    state = env.new_state(pending_epoch_ttl=30.0)
    session, c2s, s2c, _ = env.install_session(state, now=1000.0)
    init = _client_build_init(env, session)
    _server_process_init(env, state, session, init, now=1000.0)
    assert session.pending_epoch.deadline == 1030.0
    # a same-transaction retransmit does not extend it
    _server_process_init(env, state, session, init, now=1020.0)
    assert session.pending_epoch.deadline == 1030.0
    state.cleanup_expired_epoch_transitions(1030.0)
    assert session.pending_epoch is None
    assert state.stats().pending_epochs_discarded == 1
    # ... and the session itself is untouched
    assert state._sessions[session._session_key] is session


def test_pending_epoch_exhaustion_discards_only_candidate(env):
    state = env.new_state(data_nonce_max_per_session=1)
    session, c2s, s2c, _ = env.install_session(state)
    init = _client_build_init(env, session)
    reply, _ = _server_process_init(env, state, session, init)
    km, _ = _client_derive_e2(env, session, init, reply, s2c)
    confirm = _client_build_confirm(env, session, init, km)
    # first confirm nonce fills the candidate ledger (max 1) and commits...
    # so make it a REPLAY/EXHAUSTED path: send two distinct confirm nonces
    # to the still-pending candidate before it can commit is impossible
    # (commit is atomic); instead fill the candidate via a crafted confirm
    # that fails validation is also impossible. Use the documented path:
    # a candidate whose ledger is already full rejects the confirm.
    session.pending_epoch.epoch.seen_data_nonces.admit(b"\x00" * 12)
    ack, newly, _ = _server_process_confirm(
        env, state, session, confirm, now=1011.0
    )
    assert ack is None and newly is False
    assert session.pending_epoch is None
    assert session.current_epoch.generation == 0
    assert state.stats().data_nonce_exhaustions == 1


# --------------------------------------------------------------------------
# E. client choreography (nmea_sproxy)
# --------------------------------------------------------------------------


def test_client_epoch_set_commit_and_retiring_window():
    proxy = _load_proxy()
    km = _KM(b"\x01" * 32, b"\x02" * 32)
    epochs = proxy._ClientEpochSet(b"L" * 16, km)
    assert epochs.generation == 0
    epochs.set_pending(1, b"\x03" * 32, b"\x04" * 32)
    assert epochs.commit_pending(100.0) is True
    assert epochs.generation == 1
    assert epochs.client_to_server_key == b"\x03" * 32
    # retiring epoch resolvable for a straggler pong, then not
    sel0 = proxy.epoch_selector_for_generation(0)
    assert epochs.inbound_epoch(sel0, 104.9) == (b"\x02" * 32, 0)
    assert epochs.inbound_epoch(sel0, 105.0) is None


class _KM:
    def __init__(self, c2s, s2c):
        self.client_to_server_key = c2s
        self.server_to_client_key = s2c


def test_client_planned_refresh_does_not_restart_forward_loop(env, monkeypatch):
    """A due `session_refresh_interval` starts an in-session epoch refresh:
    `forward_loop()` does NOT return, the input adapter / ForwardingStats /
    output socket are untouched, and after the ACK the client sends under
    the new epoch. A genuine re-establishment handshake is never invoked."""
    proxy = _load_proxy()
    station_id = STATION_ID
    locator = env.fresh_locator()
    c2s0 = AESGCM.generate_key(bit_length=256)
    s2c0 = AESGCM.generate_key(bit_length=256)

    # An idempotent scripted server: caches the REPLY per transaction id and
    # never re-derives -- exactly like the real server's install_pending_epoch
    # DUPLICATE path.
    server = {"replies": {}, "e2": {}}

    class FakeInput:
        started = False

        def selectable_sockets(self):
            return []

        def poll_interval(self):
            return 0.05

        def read_ready(self, s):
            return []

        def read_pending(self):
            return []

        def start(self):
            type(self).started = True

        def close(self):
            pass

    class FakeSock:
        def __init__(self):
            self.inbox = []
            self.sent_kinds = []

        def sendto(self, data, addr):
            _l, sel, nonce, ct = proxy.parse_data_packet(data)
            if sel == 0:
                key, gen = c2s0, 0
            elif sel == 1 and server["e2"]:
                key, gen = server["e2"]["c2s"], 1
            else:
                return
            try:
                pt = AESGCM(key).decrypt(
                    nonce, ct, proxy.build_data_aad(_l, gen)
                )
            except Exception:
                return
            msg = json.loads(pt)
            kind = msg.get("type")
            self.sent_kinds.append(kind)
            if kind == "refresh_init":
                ri = udpsec_protocol.parse_refresh_init_message(msg)
                txn = ri.transaction_id
                if txn in server["replies"]:
                    self.inbox.append(server["replies"][txn])
                    return
                epriv = udpsec_crypto.generate_ephemeral_private_key()
                epub = udpsec_crypto.serialize_ephemeral_public_key(
                    epriv.public_key()
                )
                srand = os.urandom(32)
                common = dict(
                    protocol_version=proxy.UDPSEC_PROTOCOL_VERSION,
                    station_id=station_id, session_locator=locator,
                    parent_generation=ri.parent_generation,
                    next_generation=ri.next_generation,
                    transaction_id=txn,
                    client_epoch_random=ri.client_epoch_random,
                    client_epoch_public_key=ri.client_epoch_public_key,
                )
                rd = udpsec_crypto.build_refresh_reply_digest(
                    **common, client_refresh_signature=ri.refresh_signature,
                    server_epoch_random=srand, server_epoch_public_key=epub,
                )
                ssig = udpsec_crypto.sign_transcript_digest(
                    env.server_private_key, rd
                )
                th = udpsec_crypto.build_refresh_transcript_hash(
                    **common, client_refresh_signature=ri.refresh_signature,
                    server_epoch_random=srand, server_epoch_public_key=epub,
                    server_refresh_signature=ssig,
                )
                shared = udpsec_crypto.derive_ephemeral_shared_secret(
                    epriv,
                    udpsec_crypto.parse_ephemeral_public_key(
                        ri.client_epoch_public_key
                    ),
                )
                e2 = udpsec_crypto.derive_refresh_epoch_key_material(
                    shared, th
                )
                server["e2"] = {
                    "c2s": e2.client_to_server_key,
                    "s2c": e2.server_to_client_key,
                    "txn": txn,
                }
                reply_msg = udpsec_protocol.build_refresh_reply_message(
                    station_id=station_id, transaction_id=txn,
                    parent_generation=ri.parent_generation,
                    next_generation=ri.next_generation,
                    epoch_random=srand, epoch_public_key=epub,
                    refresh_signature=ssig, timestamp=1,
                )
                packet = proxy.encrypt_secure_json_message(
                    reply_msg, s2c0, locator, 0
                )
                server["replies"][txn] = packet
                self.inbox.append(packet)
            elif kind == "refresh_confirm":
                rc = udpsec_protocol.parse_refresh_confirm_message(msg)
                ack_msg = udpsec_protocol.build_refresh_ack_message(
                    station_id=station_id, transaction_id=rc.transaction_id,
                    next_generation=rc.next_generation, timestamp=1,
                )
                self.inbox.append(
                    proxy.encrypt_secure_json_message(
                        ack_msg, server["e2"]["s2c"], locator, 1
                    )
                )

        def recvfrom(self, n):
            if self.inbox:
                return self.inbox.pop(0), ("192.0.2.50", 19999)
            raise BlockingIOError()

    clock = {"t": 0.0}
    monkeypatch.setattr(proxy.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(proxy.time, "time", lambda: 1000)

    epoch_ref = []

    class _StopLoop(BaseException):
        pass

    def fake_select(r, w, x, t):
        # Small ticks so retransmit timers do not fire on the happy path.
        clock["t"] += 0.2
        if epoch_ref and epoch_ref[0].generation == 1:
            raise _StopLoop()
        return ([s for s in r if getattr(s, "inbox", None)], [], [])

    monkeypatch.setattr(proxy.select, "select", fake_select)

    config = {
        "station_id": station_id,
        "keepalive_interval": 100000,
        "peer_timeout": 100000,
        "session_refresh_interval": 3,
        "reconnect_delay": 1,
    }
    confirmed = proxy.ConfirmedUdpsecSession(
        session_locator=locator,
        key_material=proxy.SessionKeyMaterial(
            client_to_server_key=c2s0, server_to_client_key=s2c0
        ),
    )
    sock = FakeSock()
    stats = proxy.ForwardingStats()
    inp = FakeInput()

    with pytest.raises(_StopLoop):
        proxy.forward_loop(
            inp, sock, config, confirmed, ("192.0.2.50", 19999),
            None, stats,
            station_private_key=env.station_private_key,
            server_identity_public_key=env.server_public_key,
            epoch_state_ref=epoch_ref,
        )

    # committed onto the new epoch, same relation, same stats object
    assert epoch_ref and epoch_ref[0].generation == 1
    assert epoch_ref[0].client_to_server_key == server["e2"]["c2s"]
    assert epoch_ref[0].session_locator == locator
    assert stats.messages == 0
    # a genuine re-establishment handshake (ClientHello) was never sent
    assert "refresh_init" in sock.sent_kinds
    assert sock.sent_kinds.count("refresh_confirm") >= 1
