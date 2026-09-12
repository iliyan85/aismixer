"""UDPSEC V2 server-side path/session migration and return routability --
acceptance (Major Prompt 4).

Deterministic coverage of the candidate/active/retired path-state model on
one unchanged `LogicalSession`: candidate discovery from a new source path,
the encrypted PATH_CHALLENGE / PATH_RESPONSE / PATH_ACK return-routability
proof, the atomic A -> B active-path commit, candidate replacement, exact
response binding, retired-path grace and reverse-migration, full
LogicalSession/CryptoEpoch/replay/namespace identity preservation, the
conservative migration x epoch-refresh boundary, wrong-path security, and
the per-session bounds.

Real AES-GCM and the actual V2 DATA framing are used for every
security-relevant assertion; controlled monotonic clocks and explicit
ordering (never sleep-only races) drive every deadline/transition case.
The server receive path is exercised through the real
`aismixer_secure._secure_server_loop`.
"""

import asyncio as _asyncio
import base64
import json
import os

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import core.udpsec_protocol as p
from core.sockaddr_identity import normalize_sockaddr

from test_secure_udp_helpers import (  # noqa: E402
    _FakeAsyncioModule,
    _FakeClock,
    _FakeQueue,
    _FakeSecureLoop,
    _FakeSecureSocket,
    _fresh_test_locator,
    _relation_key,
    load_secure_module_with_fake_keys,
)

STATION_ID = "boat_001"
ADDR_A = ("192.0.2.10", 41000)
ADDR_B = ("198.51.100.20", 42000)
ADDR_C = ("203.0.113.30", 43000)

_PREPARED_SERVER_PRIVATE_KEY = None  # set lazily per module load


# --------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------


class _DecryptHook:
    """Wraps an AESGCM so each successful `decrypt` runs `on_decrypt()` once
    -- used to deterministically mutate a fake monotonic clock (or state)
    DURING the expensive AEAD step, reproducing a post-decrypt TOCTOU
    without any sleep. `encrypt` is passed straight through."""

    def __init__(self, real, on_decrypt):
        self._real = real
        self._on_decrypt = on_decrypt
        self.decrypt_calls = 0

    def decrypt(self, *args, **kwargs):
        plaintext = self._real.decrypt(*args, **kwargs)
        self.decrypt_calls += 1
        self._on_decrypt()
        return plaintext

    def encrypt(self, *args, **kwargs):
        return self._real.encrypt(*args, **kwargs)


@pytest.fixture
def env(monkeypatch):
    secure, client_private_key = load_secure_module_with_fake_keys(
        monkeypatch, with_client_private_key=True
    )
    return _Env(monkeypatch, secure, client_private_key)


class _Env:
    def __init__(self, monkeypatch, secure, client_private_key):
        self.monkeypatch = monkeypatch
        self.secure = secure
        self.station_private_key = client_private_key
        # A prepared P-256 server identity key; only the refresh-preservation
        # case actually needs it, but the loop validates it unconditionally.
        from cryptography.hazmat.primitives.asymmetric import ec

        self.server_private_key = ec.derive_private_key(0x5EC, ec.SECP256R1())
        self.server_public_key = self.server_private_key.public_key()

    def new_state(self, **kwargs):
        kwargs.setdefault("session_ttl", 100000.0)
        return self.secure.SecureState(**kwargs)

    def endpoint_token(self):
        return self.secure._new_endpoint_token()

    def install_session(
        self,
        state,
        *,
        addr=ADDR_A,
        endpoint_token=None,
        now=1000.0,
        locator=None,
        station_id=STATION_ID,
    ):
        endpoint_token = endpoint_token or self.endpoint_token()
        locator = locator or _fresh_test_locator()
        c2s = AESGCM.generate_key(bit_length=256)
        s2c = AESGCM.generate_key(bit_length=256)
        relation = self.secure._EndpointPeerKey(endpoint_token, addr)
        session = state.install_session(
            relation, station_id, locator, AESGCM(c2s), AESGCM(s2c), now
        )
        return _Session(self, state, session, c2s, s2c, endpoint_token)


class _Session:
    """One installed `LogicalSession` plus its raw directional keys and the
    listener endpoint token it lives on."""

    def __init__(self, env, state, session, c2s_key, s2c_key, endpoint_token):
        self.env = env
        self.state = state
        self.session = session
        self.c2s_key = c2s_key
        self.s2c_key = s2c_key
        self.endpoint_token = endpoint_token
        self.queue = _FakeQueue()

    @property
    def locator(self):
        return self.session._session_key.session_locator

    # -- packet construction ------------------------------------------------

    def data_packet(self, message, *, generation=0, nonce=None, key=None):
        key = self.c2s_key if key is None else key
        nonce = os.urandom(12) if nonce is None else nonce
        aad = p.build_data_aad(self.locator, generation)
        ciphertext = AESGCM(key).encrypt(
            nonce, json.dumps(message).encode(), aad
        )
        return p.build_data_packet(self.locator, generation, nonce, ciphertext)

    def nmea_packet(self, payload, *, generation=0, nonce=None, key=None):
        return self.data_packet(
            {
                "type": "nmea",
                "payload": payload,
                "timestamp": 1,
                "source_id": STATION_ID,
            },
            generation=generation,
            nonce=nonce,
            key=key,
        )

    def ping_packet(self, seq, *, generation=0, nonce=None, key=None):
        return self.data_packet(
            p.build_ping_message(STATION_ID, seq, 1),
            generation=generation,
            nonce=nonce,
            key=key,
        )

    def close_packet(self, *, generation=0, nonce=None, key=None):
        return self.data_packet(
            p.build_session_close_message(STATION_ID, 1),
            generation=generation,
            nonce=nonce,
            key=key,
        )

    def path_response_packet(
        self, challenge_token, path_generation, *, generation=0, nonce=None,
        key=None, station_id=STATION_ID,
    ):
        return self.data_packet(
            p.build_path_response_message(
                station_id=station_id,
                challenge_token=challenge_token,
                path_generation=path_generation,
                timestamp=1,
            ),
            generation=generation,
            nonce=nonce,
            key=key,
        )

    # -- server drive -----------------------------------------------------

    def feed(
        self, packets, now, *, wall=None, clock=None,
        on_decrypt=None, decrypt_epoch=None,
    ):
        """Run one `_secure_server_loop` pass over `packets` (each a
        `(bytes, addr)` pair) at monotonic time `now`. Returns the
        `_FakeSecureSocket` whose `.sent` list holds every server reply.

        `clock` reuses a caller-owned `_FakeClock` as the loop's monotonic
        clock (so a test can observe/advance it across the AEAD step).
        `on_decrypt` (with optional `decrypt_epoch` CryptoEpoch, default the
        current epoch) wraps that epoch's client->server AESGCM so the
        callback runs once after each successful decrypt -- used to
        deterministically advance `clock` DURING the expensive AEAD step and
        reproduce a post-decrypt deadline crossing without any sleep."""
        fake_socket = _FakeSecureSocket()
        loop_clock = clock if clock is not None else _FakeClock(now)
        wall_clock = _FakeClock(1_000_000.0 if wall is None else wall)
        restore = None
        if on_decrypt is not None:
            target = decrypt_epoch or self.session.current_epoch
            real_aesgcm = target.client_to_server_aesgcm
            target.client_to_server_aesgcm = _DecryptHook(
                real_aesgcm, on_decrypt
            )
            restore = (target, real_aesgcm)
        self.env.monkeypatch.setattr(
            self.env.secure, "asyncio", _FakeAsyncioModule(
                _FakeSecureLoop(list(packets))
            )
        )
        try:
            with pytest.raises(_asyncio.CancelledError):
                _asyncio.run(
                    self.env.secure._secure_server_loop(
                        fake_socket,
                        self.queue,
                        "127.0.0.1",
                        9999,
                        endpoint_token=self.endpoint_token,
                        state=self.state,
                        wall_clock=wall_clock,
                        monotonic_clock=loop_clock,
                        server_private_key=self.env.server_private_key,
                        owned_sessions=dict(self.state._sessions),
                        owned_pending_sessions={},
                    )
                )
        finally:
            if restore is not None:
                restore[0].client_to_server_aesgcm = restore[1]
        return fake_socket

    # -- reply decoding -------------------------------------------------

    def decode_server_message(self, packet, *, generation=0, s2c_key=None):
        locator, _selector, nonce, ciphertext = p.parse_data_packet(packet)
        assert locator == self.locator
        plaintext = AESGCM(s2c_key or self.s2c_key).decrypt(
            nonce, ciphertext, p.build_data_aad(self.locator, generation)
        )
        return json.loads(plaintext.decode())

    def sole_challenge(
        self, fake_socket, *, expect_addr, generation=0, s2c_key=None
    ):
        challenges = [
            (data, addr)
            for data, addr in fake_socket.sent
            if self._is_type(
                data, p.PATH_CHALLENGE_TYPE, generation, s2c_key=s2c_key
            )
        ]
        assert len(challenges) == 1, fake_socket.sent
        data, addr = challenges[0]
        assert addr == expect_addr
        return p.parse_path_challenge_message(
            self.decode_server_message(
                data, generation=generation, s2c_key=s2c_key
            )
        )

    def _is_type(self, packet, message_type, generation, *, s2c_key=None):
        try:
            message = self.decode_server_message(
                packet, generation=generation, s2c_key=s2c_key
            )
        except Exception:
            return False
        return isinstance(message, dict) and message.get("type") == message_type

    # -- convenience: full A -> B migration ------------------------------

    def migrate(self, from_addr, to_addr, *, now=1000.0, generation=0):
        open_socket = self.feed([(self.nmea_packet("!AIVDM,1,1,,A,x,0*00"),
                                  to_addr)], now)
        challenge = self.sole_challenge(
            open_socket, expect_addr=to_addr, generation=generation
        )
        response = self.path_response_packet(
            challenge.challenge_token, challenge.path_generation,
            generation=generation,
        )
        commit_socket = self.feed([(response, to_addr)], now)
        acks = [
            (data, addr)
            for data, addr in commit_socket.sent
            if self._is_type(data, p.PATH_ACK_TYPE, generation)
        ]
        assert len(acks) == 1
        assert acks[0][1] == to_addr
        return challenge, commit_socket


def _snapshot(sess):
    return sess.state.path_state_snapshot(sess.session, 0.0)


def _identity_fields(session):
    e = session.current_epoch
    return {
        "object": id(session),
        "path_state_object": id(session.path_state),
        "session_key": session._session_key,
        "locator": session._session_key.session_locator,
        "session_handle": session.session_handle,
        "assembly_namespace": session.assembly_namespace,
        "station_id": session.station_id,
        "created_at": session.created_at,
        "epoch_object": id(e),
        "epoch_generation": e.generation,
        "epoch_created_at": e.created_at,
        "epoch_c2s": id(e.client_to_server_aesgcm),
        "epoch_s2c": id(e.server_to_client_aesgcm),
        "replay_ledger": id(e.seen_data_nonces),
    }


# ==========================================================================
# A. happy A -> B migration
# ==========================================================================


def test_happy_path_migration_commits_and_preserves_session(env):
    state = env.new_state()
    sess = env.install_session(state, addr=ADDR_A, now=1000.0)
    before = _identity_fields(sess.session)
    epoch_before = sess.session.current_epoch
    ledger_before = epoch_before.seen_data_nonces

    # A valid current-epoch NMEA from B opens exactly one candidate and
    # draws exactly one PATH_CHALLENGE, addressed to B (not A).
    open_socket = sess.feed(
        [(sess.nmea_packet("!AIVDM,1,1,,A,from-B,0*00"), ADDR_B)], 1000.0
    )
    snap = _snapshot(sess)
    assert snap["active"] == ADDR_A
    assert snap["candidate"] is not None
    assert snap["candidate"]["sockaddr"] == ADDR_B
    assert snap["candidate"]["path_generation"] == 1
    assert snap["path_generation"] == 1
    challenge = sess.sole_challenge(open_socket, expect_addr=ADDR_B)
    assert challenge.station_id == STATION_ID
    assert len(challenge.challenge_token) == p.PATH_CHALLENGE_TOKEN_BYTES
    assert challenge.path_generation == 1
    # the candidate NMEA still flows to the data plane
    assert len(sess.queue.items) == 1

    # current epoch is untouched by candidate creation
    assert sess.session.current_epoch is epoch_before
    assert state.stats().epoch_refreshes_committed == 0

    # A valid PATH_RESPONSE from B commits the migration.
    response = sess.path_response_packet(
        challenge.challenge_token, challenge.path_generation
    )
    commit_socket = sess.feed([(response, ADDR_B)], 1001.0)

    snap = _snapshot(sess)
    assert snap["active"] == ADDR_B
    assert snap["candidate"] is None
    assert snap["retired"] is not None
    assert snap["retired"]["sockaddr"] == ADDR_A

    ack_message = None
    for data, addr in commit_socket.sent:
        if sess._is_type(data, p.PATH_ACK_TYPE, 0):
            assert addr == ADDR_B
            ack_message = p.parse_path_ack_message(
                sess.decode_server_message(data)
            )
    assert ack_message is not None
    assert ack_message.challenge_token == challenge.challenge_token
    assert ack_message.path_generation == 1

    # exact same LogicalSession / CryptoEpoch / replay ledger.
    assert state._sessions[sess.session._session_key] is sess.session
    after = _identity_fields(sess.session)
    for key in before:
        assert after[key] == before[key], key
    assert sess.session.current_epoch is epoch_before
    assert sess.session.current_epoch.seen_data_nonces is ledger_before

    # ordinary subsequent ping/pong now resolves to B.
    pong_socket = sess.feed([(sess.ping_packet(1), ADDR_B)], 1002.0)
    pongs = [
        (data, addr)
        for data, addr in pong_socket.sent
        if sess._is_type(data, "pong", 0)
    ]
    assert len(pongs) == 1
    assert pongs[0][1] == ADDR_B

    stats = state.stats()
    assert stats.path_candidates_opened == 1
    assert stats.path_migrations_committed == 1
    assert stats.sessions_created == 1
    assert stats.sessions_replaced == 0


def test_migration_updates_relation_index_to_new_active_path(env):
    state = env.new_state()
    sess = env.install_session(state, addr=ADDR_A)
    token = sess.endpoint_token
    assert state._active_session_at_relation(
        sess.env.secure._EndpointPeerKey(token, ADDR_A)
    ) is sess.session

    sess.migrate(ADDR_A, ADDR_B)

    assert state._active_session_at_relation(
        sess.env.secure._EndpointPeerKey(token, ADDR_B)
    ) is sess.session
    assert state._active_session_at_relation(
        sess.env.secure._EndpointPeerKey(token, ADDR_A)
    ) is None


# ==========================================================================
# B. candidate data continuity
# ==========================================================================


def test_candidate_nmea_shares_assembly_namespace_and_gets_no_pong(env):
    state = env.new_state()
    sess = env.install_session(state, addr=ADDR_A)
    namespace_hex = sess.session.assembly_namespace.hex()

    frag1 = sess.nmea_packet("!AIVDM,2,1,7,A,first-fragment,0*00")
    frag2 = sess.nmea_packet("!AIVDM,2,2,7,A,second-fragment,0*00")

    # fragment 1 from the active path A, fragment 2 from candidate B
    # (before any return-routability proof), plus a PING from B.
    sess.feed([(frag1, ADDR_A)], 1000.0)
    reply_socket = sess.feed(
        [(frag2, ADDR_B), (sess.ping_packet(5), ADDR_B)], 1000.0
    )

    keys = [frame.assembler_key for frame in sess.queue.items]
    assert len(keys) == 2
    assert keys[0] == keys[1] == f"udpsec-assembly:{namespace_hex}"

    # B receives ONLY the bounded PATH_CHALLENGE -- no ordinary pong.
    assert not any(
        sess._is_type(data, "pong", 0) for data, _ in reply_socket.sent
    )
    challenges = [
        addr for data, addr in reply_socket.sent
        if sess._is_type(data, p.PATH_CHALLENGE_TYPE, 0)
    ]
    assert challenges == [ADDR_B]
    # active path unchanged before proof
    assert _snapshot(sess)["active"] == ADDR_A


# ==========================================================================
# C. candidate replacement
# ==========================================================================


def test_newer_candidate_replaces_older_and_old_response_is_stale(env):
    state = env.new_state()
    sess = env.install_session(state, addr=ADDR_A)

    open_b = sess.feed([(sess.nmea_packet("!AIVDM,1,1,,A,b,0*00"), ADDR_B)],
                       1000.0)
    challenge_b = sess.sole_challenge(open_b, expect_addr=ADDR_B)

    open_c = sess.feed([(sess.nmea_packet("!AIVDM,1,1,,A,c,0*00"), ADDR_C)],
                       1001.0)
    challenge_c = sess.sole_challenge(open_c, expect_addr=ADDR_C)

    assert challenge_c.path_generation == challenge_b.path_generation + 1
    assert challenge_c.challenge_token != challenge_b.challenge_token
    snap = _snapshot(sess)
    assert snap["candidate"]["sockaddr"] == ADDR_C
    assert snap["path_generation"] == 2
    assert state.stats().path_candidates_replaced == 1

    # B's (now stale) response cannot commit.
    stale = sess.path_response_packet(
        challenge_b.challenge_token, challenge_b.path_generation
    )
    stale_socket = sess.feed([(stale, ADDR_B)], 1002.0)
    assert not any(
        sess._is_type(data, p.PATH_ACK_TYPE, 0) for data, _ in stale_socket.sent
    )
    assert _snapshot(sess)["active"] == ADDR_A

    # C's response commits A -> C.
    good = sess.path_response_packet(
        challenge_c.challenge_token, challenge_c.path_generation
    )
    commit_socket = sess.feed([(good, ADDR_C)], 1003.0)
    assert any(
        sess._is_type(data, p.PATH_ACK_TYPE, 0) for data, _ in commit_socket.sent
    )
    snap = _snapshot(sess)
    assert snap["active"] == ADDR_C
    assert snap["retired"]["sockaddr"] == ADDR_A


# ==========================================================================
# D. exact response binding
# ==========================================================================


def _open_candidate(env, state, *, addr=ADDR_B, now=1000.0):
    sess = env.install_session(state, addr=ADDR_A, now=now)
    socket = sess.feed([(sess.nmea_packet("!AIVDM,1,1,,A,x,0*00"), addr)], now)
    challenge = sess.sole_challenge(socket, expect_addr=addr)
    return sess, challenge


def _assert_no_commit(sess, response_packet, addr, now):
    socket = sess.feed([(response_packet, addr)], now)
    assert not any(
        sess._is_type(data, p.PATH_ACK_TYPE, 0) for data, _ in socket.sent
    )
    assert _snapshot(sess)["active"] == ADDR_A
    assert _snapshot(sess)["retired"] is None


def test_response_from_wrong_source_address_does_not_commit(env):
    state = env.new_state()
    sess, challenge = _open_candidate(env, state)
    response = sess.path_response_packet(
        challenge.challenge_token, challenge.path_generation
    )
    # correct token/generation, but arriving from C rather than candidate B
    _assert_no_commit(sess, response, ADDR_C, 1001.0)


def test_response_with_wrong_token_does_not_commit(env):
    state = env.new_state()
    sess, challenge = _open_candidate(env, state)
    response = sess.path_response_packet(
        os.urandom(p.PATH_CHALLENGE_TOKEN_BYTES), challenge.path_generation
    )
    _assert_no_commit(sess, response, ADDR_B, 1001.0)


def test_response_with_wrong_path_generation_does_not_commit(env):
    state = env.new_state()
    sess, challenge = _open_candidate(env, state)
    response = sess.path_response_packet(
        challenge.challenge_token, challenge.path_generation + 1
    )
    _assert_no_commit(sess, response, ADDR_B, 1001.0)


def test_response_with_wrong_station_does_not_commit(env):
    state = env.new_state()
    sess, challenge = _open_candidate(env, state)
    response = sess.path_response_packet(
        challenge.challenge_token, challenge.path_generation,
        station_id="other_boat",
    )
    _assert_no_commit(sess, response, ADDR_B, 1001.0)


def test_response_for_another_listener_endpoint_token_does_not_commit(env):
    state = env.new_state()
    sess, challenge = _open_candidate(env, state)
    # A second listener (fresh endpoint token) with a session sharing the
    # SAME raw locator bytes -- an independent identity.
    other = env.install_session(
        state, addr=ADDR_A, locator=sess.locator, now=1000.0
    )
    response = other.path_response_packet(
        challenge.challenge_token, challenge.path_generation, key=other.c2s_key
    )
    socket = other.feed([(response, ADDR_B)], 1001.0)
    assert not any(
        other._is_type(data, p.PATH_ACK_TYPE, 0) for data, _ in socket.sent
    )
    assert _snapshot(sess)["active"] == ADDR_A
    assert _snapshot(sess)["candidate"] is not None


def test_response_at_exact_candidate_deadline_does_not_commit(env):
    state = env.new_state(path_candidate_ttl=10.0)
    sess, challenge = _open_candidate(env, state, now=1000.0)
    assert _snapshot(sess)["candidate"]["deadline"] == 1010.0
    response = sess.path_response_packet(
        challenge.challenge_token, challenge.path_generation
    )
    # exact boundary: now >= deadline == expired
    _assert_no_commit(sess, response, ADDR_B, 1010.0)
    assert _snapshot(sess)["candidate"] is None
    assert state.stats().path_candidates_expired == 1


def test_response_just_before_candidate_deadline_commits(env):
    state = env.new_state(path_candidate_ttl=10.0)
    sess, challenge = _open_candidate(env, state, now=1000.0)
    response = sess.path_response_packet(
        challenge.challenge_token, challenge.path_generation
    )
    socket = sess.feed([(response, ADDR_B)], 1009.999)
    assert any(
        sess._is_type(data, p.PATH_ACK_TYPE, 0) for data, _ in socket.sent
    )
    assert _snapshot(sess)["active"] == ADDR_B


def test_byte_replay_of_path_response_does_not_recommit(env):
    state = env.new_state()
    sess, challenge = _open_candidate(env, state)
    response = sess.path_response_packet(
        challenge.challenge_token, challenge.path_generation
    )
    first = sess.feed([(response, ADDR_B)], 1001.0)
    assert any(sess._is_type(d, p.PATH_ACK_TYPE, 0) for d, _ in first.sent)
    assert state.stats().path_migrations_committed == 1

    # exact same datagram again: replay -- no second commit, no new ACK.
    replay = sess.feed([(response, ADDR_B)], 1002.0)
    assert not any(sess._is_type(d, p.PATH_ACK_TYPE, 0) for d, _ in replay.sent)
    assert state.stats().path_migrations_committed == 1
    assert state.stats().data_nonce_replays >= 1


def test_fresh_nonce_semantic_replay_after_replacement_does_not_commit(env):
    state = env.new_state()
    sess = env.install_session(state, addr=ADDR_A)
    open_b = sess.feed([(sess.nmea_packet("!AIVDM,1,1,,A,b,0*00"), ADDR_B)],
                       1000.0)
    challenge_b = sess.sole_challenge(open_b, expect_addr=ADDR_B)
    sess.feed([(sess.nmea_packet("!AIVDM,1,1,,A,c,0*00"), ADDR_C)], 1001.0)

    # a NEW datagram (fresh AEAD nonce) carrying B's stale token/generation
    stale = sess.path_response_packet(
        challenge_b.challenge_token, challenge_b.path_generation,
        nonce=os.urandom(12),
    )
    socket = sess.feed([(stale, ADDR_B)], 1002.0)
    assert not any(sess._is_type(d, p.PATH_ACK_TYPE, 0) for d, _ in socket.sent)
    assert _snapshot(sess)["active"] == ADDR_A


# ==========================================================================
# E. retired path
# ==========================================================================


def test_retired_path_admits_late_nmea_without_reactivating(env):
    state = env.new_state(retired_path_grace=5.0)
    sess = env.install_session(state, addr=ADDR_A, now=1000.0)
    sess.migrate(ADDR_A, ADDR_B, now=1000.0)
    assert _snapshot(sess)["retired"]["deadline"] == 1005.0
    frames_before = len(sess.queue.items)

    # late in-flight NMEA from retired A within grace: admitted as data,
    # but no reverse candidate and active stays B.
    late_socket = sess.feed(
        [(sess.nmea_packet("!AIVDM,1,1,,A,late,0*00"), ADDR_A)], 1002.0
    )
    assert len(sess.queue.items) == frames_before + 1
    snap = _snapshot(sess)
    assert snap["active"] == ADDR_B
    assert snap["candidate"] is None
    assert not any(
        sess._is_type(d, p.PATH_CHALLENGE_TYPE, 0) for d, _ in late_socket.sent
    )

    # a PING from retired A: late inbound activity, but no ordinary pong.
    ping_socket = sess.feed([(sess.ping_packet(9), ADDR_A)], 1003.0)
    assert not any(
        sess._is_type(d, "pong", 0) for d, _ in ping_socket.sent
    )

    # retired A cannot drive a graceful close.
    close_socket = sess.feed([(sess.close_packet(), ADDR_A)], 1004.0)
    assert state._sessions[sess.session._session_key] is sess.session
    assert close_socket.sent == []


def test_retired_path_expires_at_exact_grace_boundary_then_needs_fresh_cycle(
    env,
):
    state = env.new_state(retired_path_grace=5.0)
    sess = env.install_session(state, addr=ADDR_A, now=1000.0)
    sess.migrate(ADDR_A, ADDR_B, now=1000.0)

    # exact boundary now >= 1005.0 -> retired A is gone; fresh A traffic is
    # simply a NEW path and must run a fresh candidate/challenge cycle.
    socket = sess.feed(
        [(sess.nmea_packet("!AIVDM,1,1,,A,again,0*00"), ADDR_A)], 1005.0
    )
    assert state.stats().retired_paths_expired == 1
    snap = _snapshot(sess)
    assert snap["active"] == ADDR_B
    assert snap["candidate"] is not None
    assert snap["candidate"]["sockaddr"] == ADDR_A
    # brand-new incarnation: fresh generation, fresh token.
    challenge = sess.sole_challenge(socket, expect_addr=ADDR_A)
    assert challenge.path_generation == 2

    # B -> A now requires a fresh successful challenge/response.
    response = sess.path_response_packet(
        challenge.challenge_token, challenge.path_generation
    )
    commit_socket = sess.feed([(response, ADDR_A)], 1006.0)
    assert any(
        sess._is_type(d, p.PATH_ACK_TYPE, 0) for d, _ in commit_socket.sent
    )
    assert _snapshot(sess)["active"] == ADDR_A
    assert _snapshot(sess)["retired"]["sockaddr"] == ADDR_B


# ==========================================================================
# F. lifetime / identity preservation
# ==========================================================================


def test_migration_churns_nothing_in_the_identity_registry(env):
    state = env.new_state()
    sess = env.install_session(state, addr=ADDR_A)
    registry = env.secure._SESSION_IDENTITY_REGISTRY
    live_before = registry.live_count()
    handle = sess.session.session_handle
    namespace = sess.session.assembly_namespace

    sess.migrate(ADDR_A, ADDR_B)

    assert registry.live_count() == live_before
    assert registry.is_live(handle)
    assert registry.is_live(namespace)
    stats = state.stats()
    assert stats.sessions_created == 1
    assert stats.sessions_replaced == 0
    assert stats.epoch_refreshes_committed == 0
    assert stats.pending_epochs_created == 0


def test_migration_does_not_move_created_at_or_epoch_origin(env):
    state = env.new_state()
    sess = env.install_session(state, addr=ADDR_A, now=1000.0)
    created_at = sess.session.created_at
    epoch_created_at = sess.session.current_epoch.created_at

    sess.migrate(ADDR_A, ADDR_B, now=7000.0)

    assert sess.session.created_at == created_at
    assert sess.session.current_epoch.created_at == epoch_created_at


# ==========================================================================
# G. Prompt 3 preservation (epoch refresh unaffected by / composes with
#    path migration)
# ==========================================================================


def test_epoch_refresh_still_commits_on_a_stable_active_path_after_migration(
    env,
):
    from test_udpsec_refresh import (  # noqa: E402
        _client_build_confirm,
        _client_build_init,
        _client_derive_e2,
        _server_process_confirm,
        _server_process_init,
    )

    state = env.new_state()
    sess = env.install_session(state, addr=ADDR_A, now=1000.0)
    sess.migrate(ADDR_A, ADDR_B, now=1000.0)
    assert _snapshot(sess)["active"] == ADDR_B

    # A full in-session epoch refresh on the (now B) stable active path
    # still commits and leaves the exact same LogicalSession in place.
    init = _client_build_init(env, sess.session)
    reply_packet, _ = _server_process_init(
        env, state, sess.session, init, now=1001.0
    )
    assert reply_packet is not None
    km, _ = _client_derive_e2(
        env, sess.session, init, reply_packet, sess.s2c_key
    )
    confirm = _client_build_confirm(env, sess.session, init, km)
    ack_packet, newly, _ = _server_process_confirm(
        env, state, sess.session, confirm, now=1002.0
    )
    assert newly is True
    assert sess.session.current_epoch.generation == 1
    assert state._sessions[sess.session._session_key] is sess.session
    # migration did not perturb the refresh lifecycle counters
    assert state.stats().epoch_refreshes_committed == 1
    assert state.stats().path_migrations_committed == 1


def test_secure_module_still_uses_constant_time_bytes_eq(env):
    assert hasattr(env.secure, "constant_time")
    assert env.secure.constant_time.bytes_eq(b"abc", b"abc")
    assert not env.secure.constant_time.bytes_eq(b"abc", b"abd")


# ==========================================================================
# H. wrong-path security behaviour
# ==========================================================================


def test_denied_source_performs_no_migration_crypto_or_state(env):
    from core.network_policy import NetworkPolicy

    state = env.new_state()
    sess = env.install_session(state, addr=ADDR_A)
    policy = NetworkPolicy.from_entries(
        [ADDR_A[0]], context="sec_inputs[0].allow_from"
    )

    fake_socket = _FakeSecureSocket()
    env.monkeypatch.setattr(
        env.secure, "asyncio",
        _FakeAsyncioModule(_FakeSecureLoop(
            [(sess.nmea_packet("!AIVDM,1,1,,A,x,0*00"), ADDR_B)]
        )),
    )
    with pytest.raises(_asyncio.CancelledError):
        _asyncio.run(env.secure._secure_server_loop(
            fake_socket, sess.queue, "127.0.0.1", 9999,
            ingress_policy=policy,
            endpoint_token=sess.endpoint_token,
            state=state,
            wall_clock=_FakeClock(1_000_000.0),
            monotonic_clock=_FakeClock(1000.0),
            server_private_key=env.server_private_key,
            owned_sessions=dict(state._sessions),
            owned_pending_sessions={},
        ))

    assert fake_socket.sent == []
    assert sess.queue.items == []
    assert _snapshot(sess)["candidate"] is None
    assert state.stats().path_candidates_opened == 0


def test_guessed_locator_from_a_new_path_triggers_no_candidate(env):
    state = env.new_state()
    sess = env.install_session(state, addr=ADDR_A)
    # a plausible but unknown 16-byte locator, from a path that is not the
    # active path -- no live session resolves, so nothing happens.
    bogus_locator = _fresh_test_locator()
    aad = p.build_data_aad(bogus_locator, 0)
    ciphertext = AESGCM(sess.c2s_key).encrypt(
        os.urandom(12),
        json.dumps({"type": "nmea", "payload": "x", "timestamp": 1,
                    "source_id": STATION_ID}).encode(),
        aad,
    )
    packet = p.build_data_packet(bogus_locator, 0, os.urandom(12), ciphertext)
    socket = sess.feed([(packet, ADDR_B)], 1000.0)
    assert socket.sent == []
    assert _snapshot(sess)["candidate"] is None


def test_replayed_nonce_from_a_new_path_creates_no_candidate(env):
    state = env.new_state()
    sess = env.install_session(state, addr=ADDR_A)
    nonce = os.urandom(12)
    packet = sess.nmea_packet("!AIVDM,1,1,,A,x,0*00", nonce=nonce)
    # first: from the active path A -> admitted, nonce retained
    sess.feed([(packet, ADDR_A)], 1000.0)
    assert len(sess.queue.items) == 1

    # same nonce replayed from a new path B -> pre-decrypt replay drop,
    # no candidate, no challenge.
    socket = sess.feed([(packet, ADDR_B)], 1001.0)
    assert _snapshot(sess)["candidate"] is None
    assert not any(
        sess._is_type(d, p.PATH_CHALLENGE_TYPE, 0) for d, _ in socket.sent
    )
    assert state.stats().path_candidates_opened == 0


def test_retiring_epoch_traffic_from_a_new_path_cannot_open_a_candidate(env):
    """The candidate is bound to the exact CURRENT epoch; a DATA frame that
    selects a retiring epoch from an unproved path is dropped by the
    unproved-path current-epoch restriction before any candidate work."""
    from test_udpsec_refresh import (  # noqa: E402
        _client_build_confirm,
        _client_build_init,
        _client_derive_e2,
        _server_process_confirm,
        _server_process_init,
    )

    state = env.new_state(retiring_epoch_overlap=50.0)
    sess = env.install_session(state, addr=ADDR_A, now=1000.0)

    init = _client_build_init(env, sess.session)
    reply_packet, _ = _server_process_init(
        env, state, sess.session, init, now=1000.0
    )
    km, _ = _client_derive_e2(env, sess.session, init, reply_packet,
                              sess.s2c_key)
    confirm = _client_build_confirm(env, sess.session, init, km)
    _server_process_confirm(env, state, sess.session, confirm, now=1000.0)
    assert sess.session.current_epoch.generation == 1
    assert sess.session.retiring_epoch is not None

    # A generation-0 (now retiring) NMEA from a new path B.
    retiring_packet = sess.nmea_packet(
        "!AIVDM,1,1,,A,old-epoch,0*00", generation=0
    )
    socket = sess.feed([(retiring_packet, ADDR_B)], 1001.0)
    assert _snapshot(sess)["candidate"] is None
    assert not any(
        sess._is_type(d, p.PATH_CHALLENGE_TYPE, 0) for d, _ in socket.sent
    )


# ==========================================================================
# I. bounds
# ==========================================================================


def test_repeated_same_candidate_traffic_does_not_grow_or_extend_state(env):
    state = env.new_state(path_candidate_ttl=10.0)
    sess = env.install_session(state, addr=ADDR_A, now=1000.0)

    first = sess.feed([(sess.nmea_packet("!AIVDM,1,1,,A,1,0*00"), ADDR_B)],
                      1000.0)
    challenge = sess.sole_challenge(first, expect_addr=ADDR_B)
    deadline = _snapshot(sess)["candidate"]["deadline"]
    assert deadline == 1010.0

    for tick, payload in ((1002.0, "2"), (1004.0, "3"), (1006.0, "4")):
        again = sess.feed(
            [(sess.nmea_packet(f"!AIVDM,1,1,,A,{payload},0*00"), ADDR_B)], tick
        )
        # no fresh challenge on duplicate candidate traffic
        assert not any(
            sess._is_type(d, p.PATH_CHALLENGE_TYPE, 0) for d, _ in again.sent
        )
        snap = _snapshot(sess)
        assert snap["candidate"]["deadline"] == deadline  # never extended
        assert snap["candidate"]["path_generation"] == challenge.path_generation

    stats = state.stats()
    assert stats.path_candidates_opened == 1
    assert stats.path_candidates_replaced == 0
    assert stats.current_candidate_paths == 1


def test_candidate_churn_retains_at_most_one_candidate(env):
    state = env.new_state()
    sess = env.install_session(state, addr=ADDR_A)
    tokens = []
    for i, addr in enumerate(
        [ADDR_B, ADDR_C, ("192.0.2.99", 44000), ADDR_B]
    ):
        socket = sess.feed(
            [(sess.nmea_packet(f"!AIVDM,1,1,,A,{i},0*00"), addr)], 1000.0 + i
        )
        challenge = sess.sole_challenge(socket, expect_addr=addr)
        tokens.append(challenge)
        assert state.stats().current_candidate_paths == 1
        assert _snapshot(sess)["candidate"]["sockaddr"] == addr

    # every replacement advanced the generation and minted a fresh token
    generations = [c.path_generation for c in tokens]
    assert generations == [1, 2, 3, 4]
    assert len({c.challenge_token for c in tokens}) == 4
    assert state.stats().path_candidates_replaced == 3


def test_session_removal_makes_both_path_records_non_authoritative(env):
    state = env.new_state()
    sess = env.install_session(state, addr=ADDR_A, now=1000.0)
    sess.migrate(ADDR_A, ADDR_B, now=1000.0)
    # open a fresh candidate C on top of the retired A
    open_c = sess.feed([(sess.nmea_packet("!AIVDM,1,1,,A,c,0*00"), ADDR_C)],
                       1001.0)
    challenge_c = sess.sole_challenge(open_c, expect_addr=ADDR_C)
    assert _snapshot(sess)["candidate"] is not None
    assert _snapshot(sess)["retired"] is not None

    # the session is closed outright.
    assert state.close_session(sess.session, 1002.0)
    assert sess.session._session_key not in state._sessions

    # neither path record can authorize anything now.
    assert state.path_state_snapshot(sess.session, 1002.0) is None
    assert state.resolve_incoming_path_role(
        sess.session, ADDR_C, 1002.0
    ) == "unknown"
    response = sess.path_response_packet(
        challenge_c.challenge_token, challenge_c.path_generation
    )
    socket = sess.feed([(response, ADDR_C)], 1002.0)
    assert socket.sent == []


def test_cleanup_expired_path_transitions_drains_idle_records(env):
    state = env.new_state(path_candidate_ttl=10.0, retired_path_grace=5.0)
    sess = env.install_session(state, addr=ADDR_A, now=1000.0)
    sess.migrate(ADDR_A, ADDR_B, now=1000.0)
    sess.feed([(sess.nmea_packet("!AIVDM,1,1,,A,c,0*00"), ADDR_C)], 1000.0)
    assert _snapshot(sess)["candidate"] is not None
    assert _snapshot(sess)["retired"] is not None

    # drain purely through the idle-maintenance path, no packet involved.
    state.cleanup_expired_path_transitions(1010.0)
    assert state._sessions[sess.session._session_key] is sess.session
    assert sess.session.path_state.candidate_path is None
    assert sess.session.path_state.retired_path is None
    stats = state.stats()
    assert stats.path_candidates_expired == 1
    assert stats.retired_paths_expired == 1
    assert stats.current_candidate_paths == 0
    assert stats.current_retired_paths == 0


# ==========================================================================
# Pre-audit corrective pass -- Finding A: stale same-address candidate after
# an epoch refresh must be replaced by a fresh incarnation bound to E2
# ==========================================================================


def _refresh_helpers():
    from test_udpsec_refresh import (  # noqa: E402
        _client_build_confirm,
        _client_build_init,
        _client_derive_e2,
        _server_process_confirm,
        _server_process_init,
    )

    return (
        _client_build_init,
        _server_process_init,
        _client_derive_e2,
        _client_build_confirm,
        _server_process_confirm,
    )


def test_stale_e1_same_address_candidate_replaced_by_fresh_e2_incarnation(env):
    (
        build_init,
        process_init,
        derive_e2,
        build_confirm,
        process_confirm,
    ) = _refresh_helpers()

    state = env.new_state(path_candidate_ttl=10.0, retiring_epoch_overlap=50.0)
    sess = env.install_session(state, addr=ADDR_A, now=1000.0)

    # 1-2. Authenticated current-E1 NMEA from B opens Candidate(B, R1, PG1, E1).
    open_b = sess.feed(
        [(sess.nmea_packet("!AIVDM,1,1,,A,e1,0*00", generation=0), ADDR_B)],
        1000.0,
    )
    challenge_1 = sess.sole_challenge(open_b, expect_addr=ADDR_B, generation=0)
    snap1 = sess.state.path_state_snapshot(sess.session, 1000.0)
    assert snap1["candidate"]["epoch_generation"] == 0
    assert snap1["candidate"]["deadline"] == 1010.0
    e1_deadline = snap1["candidate"]["deadline"]

    # 3. Commit a normal Prompt 3 epoch refresh E1 -> E2 while active path
    # stays A.
    init = build_init(env, sess.session)
    reply_packet, _ = process_init(env, state, sess.session, init, now=1001.0)
    assert reply_packet is not None
    km, _ = derive_e2(env, sess.session, init, reply_packet, sess.s2c_key)
    confirm = build_confirm(env, sess.session, init, km)
    _ack, newly, _n = process_confirm(
        env, state, sess.session, confirm, now=1002.0
    )
    assert newly is True
    assert sess.session.current_epoch.generation == 1
    assert sess.state.path_state_snapshot(sess.session, 1003.0)["active"] == (
        ADDR_A
    )
    e2_c2s = km.client_to_server_key
    e2_s2c = km.server_to_client_key

    # 4. The old PATH_RESPONSE(R1, PG1) cannot migrate -- neither under the
    # now-retiring E1 nor re-encrypted under E2 (the candidate is still
    # bound to E1).
    old_under_e1 = sess.path_response_packet(
        challenge_1.challenge_token, challenge_1.path_generation, generation=0
    )
    r = sess.feed([(old_under_e1, ADDR_B)], 1003.0)
    assert not any(sess._is_type(d, p.PATH_ACK_TYPE, 0) for d, _ in r.sent)
    assert not any(
        sess._is_type(d, p.PATH_ACK_TYPE, 1, s2c_key=e2_s2c)
        for d, _ in r.sent
    )
    old_under_e2 = sess.path_response_packet(
        challenge_1.challenge_token, challenge_1.path_generation,
        generation=1, key=e2_c2s,
    )
    r = sess.feed([(old_under_e2, ADDR_B)], 1003.5)
    assert not any(
        sess._is_type(d, p.PATH_ACK_TYPE, 1, s2c_key=e2_s2c)
        for d, _ in r.sent
    )
    assert sess.state.path_state_snapshot(sess.session, 1004.0)["active"] == (
        ADDR_A
    )

    # 5. A fresh authenticated E2 NMEA from the SAME B.
    e2_nmea = sess.nmea_packet(
        "!AIVDM,1,1,,A,e2,0*00", generation=1, key=e2_c2s
    )
    open_b2 = sess.feed([(e2_nmea, ADDR_B)], 1004.0)

    # 6. It opens a NEW Candidate(B, R2, PG2, E2).
    challenge_2 = sess.sole_challenge(
        open_b2, expect_addr=ADDR_B, generation=1, s2c_key=e2_s2c
    )
    snap2 = sess.state.path_state_snapshot(sess.session, 1004.0)
    assert challenge_2.challenge_token != challenge_1.challenge_token
    assert challenge_2.path_generation == challenge_1.path_generation + 1
    assert snap2["candidate"]["epoch_generation"] == 1
    # deadline is newly anchored to now=1004, NOT inherited/extended from
    # the stale E1 object's 1010.0
    assert snap2["candidate"]["deadline"] == 1014.0
    assert snap2["candidate"]["deadline"] != e1_deadline
    assert state.stats().path_candidates_replaced == 1
    assert state.stats().path_candidates_opened == 2

    # 7. Old response remains stale; the matching NEW response commits A->B.
    r = sess.feed([(old_under_e2, ADDR_B)], 1005.0)
    assert not any(
        sess._is_type(d, p.PATH_ACK_TYPE, 1, s2c_key=e2_s2c)
        for d, _ in r.sent
    )
    assert sess.state.path_state_snapshot(sess.session, 1005.0)["active"] == (
        ADDR_A
    )

    new_response = sess.path_response_packet(
        challenge_2.challenge_token, challenge_2.path_generation,
        generation=1, key=e2_c2s,
    )
    commit = sess.feed([(new_response, ADDR_B)], 1006.0)
    assert any(
        sess._is_type(d, p.PATH_ACK_TYPE, 1, s2c_key=e2_s2c)
        for d, _ in commit.sent
    )
    final = sess.state.path_state_snapshot(sess.session, 1006.0)
    assert final["active"] == ADDR_B
    assert final["retired"]["sockaddr"] == ADDR_A
    # migration touched no epoch state
    assert sess.session.current_epoch.generation == 1
    assert state._sessions[sess.session._session_key] is sess.session


def test_same_address_duplicate_is_still_a_duplicate_without_a_refresh(env):
    """The Finding A change must not turn ordinary same-B duplicate traffic
    (no epoch refresh in between) into a replacement."""
    state = env.new_state()
    sess = env.install_session(state, addr=ADDR_A, now=1000.0)
    first = sess.feed([(sess.nmea_packet("!AIVDM,1,1,,A,1,0*00"), ADDR_B)],
                      1000.0)
    challenge = sess.sole_challenge(first, expect_addr=ADDR_B)
    again = sess.feed([(sess.nmea_packet("!AIVDM,1,1,,A,2,0*00"), ADDR_B)],
                      1003.0)
    assert not any(
        sess._is_type(d, p.PATH_CHALLENGE_TYPE, 0) for d, _ in again.sent
    )
    snap = _snapshot(sess)
    assert snap["candidate"]["path_generation"] == challenge.path_generation
    assert snap["candidate"]["deadline"] == 1010.0
    assert state.stats().path_candidates_replaced == 0


# ==========================================================================
# Pre-audit corrective pass -- Finding B: a migration target relation owned
# by another live session on the same endpoint token must fail closed
# ==========================================================================


def test_migration_target_occupied_by_sibling_session_fails_closed(env):
    state = env.new_state()
    token = env.endpoint_token()
    s1 = env.install_session(
        state, addr=ADDR_A, endpoint_token=token, now=1000.0
    )
    s2 = env.install_session(
        state, addr=ADDR_B, endpoint_token=token, now=1000.0
    )
    s1_obj, s2_obj = s1.session, s2.session
    stats_before = state.stats()

    # Authenticated S1 traffic from B -- B is S2's active path on the same
    # endpoint token. S1 can never migrate there.
    open_socket = s1.feed(
        [(s1.nmea_packet("!AIVDM,1,1,,A,x,0*00"), ADDR_B),
         (s1.ping_packet(3), ADDR_B)],
        1000.0,
    )
    assert open_socket.sent == []
    assert s1.queue.items == []  # dropped before crypto (role 'blocked')

    # both sessions untouched
    assert state._sessions[s1_obj._session_key] is s1_obj
    assert state._sessions[s2_obj._session_key] is s2_obj
    assert _snapshot(s1)["candidate"] is None
    assert _snapshot(s1)["active"] == ADDR_A
    assert _snapshot(s2)["active"] == ADDR_B
    assert state._active_session_at_relation(
        s1.env.secure._EndpointPeerKey(token, ADDR_A)
    ) is s1_obj
    assert state._active_session_at_relation(
        s1.env.secure._EndpointPeerKey(token, ADDR_B)
    ) is s2_obj

    stats_after = state.stats()
    assert stats_after.sessions_replaced == stats_before.sessions_replaced
    assert stats_after.sessions_closed == stats_before.sessions_closed
    assert (
        stats_after.sessions_capacity_evicted
        == stats_before.sessions_capacity_evicted
    )
    assert stats_after.path_candidates_opened == 0
    assert stats_after.path_migrations_committed == 0
    assert len(state._relation_index) == 2


def test_target_relation_becomes_occupied_after_candidate_opened(env):
    """Race-shaped form: S1 opens candidate C while C is free; before S1's
    PATH_RESPONSE another session becomes active at C; S1's response fails
    closed without mutating either session."""
    state = env.new_state()
    token = env.endpoint_token()
    s1 = env.install_session(
        state, addr=ADDR_A, endpoint_token=token, now=1000.0
    )

    open_c = s1.feed([(s1.nmea_packet("!AIVDM,1,1,,A,c,0*00"), ADDR_C)],
                     1000.0)
    challenge = s1.sole_challenge(open_c, expect_addr=ADDR_C)
    assert _snapshot(s1)["candidate"]["sockaddr"] == ADDR_C

    # a sibling session becomes active exactly at C
    s2 = env.install_session(
        state, addr=ADDR_C, endpoint_token=token, now=1000.5
    )
    s2_obj = s2.session

    response = s1.path_response_packet(
        challenge.challenge_token, challenge.path_generation
    )
    commit = s1.feed([(response, ADDR_C)], 1001.0)
    assert not any(s1._is_type(d, p.PATH_ACK_TYPE, 0) for d, _ in commit.sent)

    assert _snapshot(s1)["active"] == ADDR_A
    assert _snapshot(s1)["retired"] is None
    assert state._sessions[s2_obj._session_key] is s2_obj
    assert _snapshot(s2)["active"] == ADDR_C
    assert state._active_session_at_relation(
        s1.env.secure._EndpointPeerKey(token, ADDR_C)
    ) is s2_obj
    assert state.stats().path_migrations_committed == 0
    # the stale-but-valid candidate simply lingers until its own deadline
    assert _snapshot(s1)["candidate"] is not None
    assert _snapshot(s1)["candidate"]["deadline"] == 1010.0


# ==========================================================================
# Pre-audit corrective pass -- Finding C: path-transition housekeeping is
# hard-bounded independently of candidate replacement rate
# ==========================================================================


def test_candidate_churn_does_not_grow_any_auxiliary_path_state(env):
    state = env.new_state(path_candidate_ttl=1000.0)
    sess = env.install_session(state, addr=ADDR_A, now=1000.0)

    # There is no path-transition heap at all.
    assert not hasattr(state, "_path_transition_heap")
    assert not hasattr(state, "_push_path_transition")

    # A large number of candidate replacements, none of which can expire
    # (deadline 1000s away) and none of which advances the clock.
    addresses = [
        (f"198.51.100.{i % 200 + 1}", 40000 + i) for i in range(400)
    ]
    for i, addr in enumerate(addresses):
        socket = sess.feed(
            [(sess.nmea_packet(f"!AIVDM,1,1,,A,{i},0*00"), addr)], 1000.0
        )
        sess.sole_challenge(socket, expect_addr=addr)

    # PathState still holds exactly one candidate + zero retired.
    assert sess.session.path_state.candidate_path is not None
    assert sess.session.path_state.candidate_path.identity == (
        normalize_sockaddr(addresses[-1])
    )
    assert sess.session.path_state.retired_path is None
    assert state.stats().current_candidate_paths == 1
    assert state.stats().current_retired_paths == 0
    assert state.stats().path_candidates_replaced == 399

    # No auxiliary structure retains an unbounded history of the churned
    # addresses/objects. The only path-housekeeping storage is the two
    # optional PathState slots per live session.
    for name in vars(state):
        value = getattr(state, name)
        if isinstance(value, (list, dict, set)) and name not in (
            "_sessions", "_relation_index", "_pending_sessions",
            "_pending_locator_owners", "_session_expiry_heap",
            "_pending_expiry_heap", "_epoch_transition_heap",
        ):
            assert len(value) < 50, (name, len(value))

    # After the session is removed, nothing strongly retains its candidate
    # address history.
    assert state.close_session(sess.session, 1001.0)
    assert len(state._sessions) == 0
    assert len(state._relation_index) == 0
    # idle maintenance over zero sessions is a no-op
    state.cleanup_expired_path_transitions(2000.0)


def test_idle_maintenance_sweep_is_bounded_by_live_session_count(env):
    """`cleanup_expired_path_transitions` is one O(number of live sessions)
    sweep -- each session contributes a constant two optional records."""
    state = env.new_state(
        path_candidate_ttl=10.0, retired_path_grace=5.0, session_ttl=100000.0
    )
    token = env.endpoint_token()
    sessions = []
    for i in range(6):
        s = env.install_session(
            state,
            addr=(f"192.0.2.{i + 1}", 40000),
            endpoint_token=token,
            now=1000.0,
        )
        # give each session a candidate + (via migration) a retired path
        s.migrate((f"192.0.2.{i + 1}", 40000), (f"198.51.100.{i + 1}", 50000),
                  now=1000.0)
        s.feed([(s.nmea_packet("!AIVDM,1,1,,A,c,0*00"),
                 (f"203.0.113.{i + 1}", 60000))], 1000.0)
        sessions.append(s)

    for s in sessions:
        assert s.session.path_state.candidate_path is not None
        assert s.session.path_state.retired_path is not None

    # one bounded sweep past both deadlines clears every session's slots
    state.cleanup_expired_path_transitions(1010.0)
    for s in sessions:
        assert s.session.path_state.candidate_path is None
        assert s.session.path_state.retired_path is None
        assert state._sessions[s.session._session_key] is s.session
    assert state.stats().path_candidates_expired == 6
    assert state.stats().retired_paths_expired == 6


# ==========================================================================
# Pre-audit corrective pass -- naming cleanup
# ==========================================================================


def test_candidate_observation_helper_is_renamed(env):
    assert hasattr(env.secure, "_process_candidate_path_observation")
    assert not hasattr(env.secure, "_maybe_open_candidate_path")


# ==========================================================================
# protocol codec coverage for the three path control messages
# ==========================================================================


def _path_kwargs(**changes):
    values = dict(
        station_id=STATION_ID,
        challenge_token=os.urandom(p.PATH_CHALLENGE_TOKEN_BYTES),
        path_generation=1,
        timestamp=7,
    )
    values.update(changes)
    return values


@pytest.mark.parametrize(
    ("builder", "parser", "message_type"),
    (
        (p.build_path_challenge_message, p.parse_path_challenge_message,
         p.PATH_CHALLENGE_TYPE),
        (p.build_path_response_message, p.parse_path_response_message,
         p.PATH_RESPONSE_TYPE),
        (p.build_path_ack_message, p.parse_path_ack_message, p.PATH_ACK_TYPE),
    ),
)
def test_path_message_round_trip(builder, parser, message_type):
    kwargs = _path_kwargs()
    message = builder(**kwargs)
    assert set(message) == {
        "type", "source_id", "challenge_token", "path_generation", "timestamp"
    }
    assert message["type"] == message_type
    parsed = parser(message)
    assert parsed.station_id == STATION_ID
    assert parsed.challenge_token == kwargs["challenge_token"]
    assert parsed.path_generation == 1
    assert parsed.timestamp == 7
    # the secret token is not exposed in the repr
    assert "challenge_token" not in repr(parsed)


@pytest.mark.parametrize(
    "mutate",
    (
        lambda m: {**m, "extra": 1},
        lambda m: {k: v for k, v in m.items() if k != "challenge_token"},
        lambda m: {**m, "type": "path_ack"},
        lambda m: {**m, "path_generation": 0},
        lambda m: {**m, "path_generation": -1},
        lambda m: {**m, "path_generation": True},
        lambda m: {**m, "path_generation": 1 << 32},
        lambda m: {**m, "challenge_token": base64.b64encode(
            os.urandom(31)).decode()},
        lambda m: {**m, "challenge_token": "not base64!!"},
        lambda m: {**m, "challenge_token": base64.b64encode(
            os.urandom(32)).decode() + "="},
        lambda m: {**m, "timestamp": 1.5},
        lambda m: {**m, "timestamp": True},
        lambda m: {**m, "source_id": 123},
    ),
)
def test_path_challenge_rejects_malformed(mutate):
    message = p.build_path_challenge_message(**_path_kwargs())
    with pytest.raises((ValueError, TypeError)):
        p.parse_path_challenge_message(mutate(message))


def test_path_messages_are_disambiguated_by_type():
    challenge = p.build_path_challenge_message(**_path_kwargs())
    with pytest.raises(ValueError):
        p.parse_path_response_message(challenge)
    with pytest.raises(ValueError):
        p.parse_path_ack_message(challenge)


def test_path_generation_bounds_are_a_finite_closed_range():
    assert p.MIN_PATH_GENERATION == 1
    assert p.MAX_PATH_GENERATION == (1 << 32) - 1
    p.build_path_challenge_message(**_path_kwargs(path_generation=1))
    p.build_path_challenge_message(
        **_path_kwargs(path_generation=p.MAX_PATH_GENERATION)
    )
    with pytest.raises(ValueError):
        p.build_path_challenge_message(
            **_path_kwargs(path_generation=p.MAX_PATH_GENERATION + 1)
        )


# ==========================================================================
# P2 pre-audit corrective pass -- retired/candidate-path deadline TOCTOU
#
# Ordinary validated DATA (nmea/ping/close) must be admitted (and its path
# role authorized) at a FRESH authoritative monotonic observation taken
# AFTER the expensive AEAD -- never trusting the stale pre-decrypt role.
# A retired grace or candidate TTL that elapses during decrypt is honoured.
# ==========================================================================


def _e1_e2_migrated_session(env, *, base=50000.0, refresh_at=50002.0):
    """S established on A (E1), migrated A -> B, then E1 -> E2 refreshed on
    the (stable) active path B. Returns `(state, sess, e2_c2s, e2_s2c)`.

    Defaults: retired A deadline = base + 5.0 (RETIRED_PATH_GRACE_SECONDS);
    E1 stays retiring until refresh_at + 5.0 (RETIRING_EPOCH_OVERLAP)."""
    build_init, process_init, derive_e2, build_confirm, process_confirm = (
        _refresh_helpers()
    )
    state = env.new_state()
    sess = env.install_session(state, addr=ADDR_A, now=base)
    sess.migrate(ADDR_A, ADDR_B, now=base)
    assert sess.session.path_state.active_path == ADDR_B

    init = build_init(env, sess.session)
    reply_packet, _ = process_init(
        env, state, sess.session, init, now=refresh_at - 1.0
    )
    km, _ = derive_e2(env, sess.session, init, reply_packet, sess.s2c_key)
    confirm = build_confirm(env, sess.session, init, km)
    _ack, newly, _n = process_confirm(
        env, state, sess.session, confirm, now=refresh_at
    )
    assert newly is True
    assert sess.session.current_epoch.generation == 1
    assert sess.session.retiring_epoch is not None
    assert sess.session.path_state.active_path == ADDR_B
    return state, sess, km.client_to_server_key, km.server_to_client_key


@pytest.mark.parametrize("cross_to", (50005.0, 50005.001))
def test_A_retired_deadline_toctou_e1_retiring_straggler_fails_closed(
    env, cross_to,
):
    """Original P2 blocker. Retired A deadline 50005; E1 retiring until
    50007; a valid E1 NMEA from A is classified 'retired' at pre-decrypt
    50004.999 but decrypt advances authoritative time to >= 50005. The
    authoritative post-decrypt role is 'unknown', and 'unknown + retiring'
    is unauthorized -> fail closed, nothing admitted or touched."""
    state, sess, _e2_c2s, _e2_s2c = _e1_e2_migrated_session(env)
    retiring_epoch = sess.session.retiring_epoch
    assert sess.session.path_state.retired_path.deadline == 50005.0

    stats_before = state.stats()
    frames_before = len(sess.queue.items)

    nonce = os.urandom(12)
    e1_nmea = sess.nmea_packet(
        "!AIVDM,1,1,,A,e1-straggler,0*00", generation=0, nonce=nonce
    )
    clock = _FakeClock(50004.999)
    socket = sess.feed(
        [(e1_nmea, ADDR_A)],
        50004.999,
        clock=clock,
        on_decrypt=lambda: setattr(clock, "now", cross_to),
        decrypt_epoch=retiring_epoch,
    )

    stats_after = state.stats()
    # fail closed: no nonce, no activity, no queue, no candidate, no reply
    assert stats_after.data_nonces_accepted == stats_before.data_nonces_accepted
    assert stats_after.sessions_touched == stats_before.sessions_touched
    assert len(sess.queue.items) == frames_before
    assert socket.sent == []
    assert not retiring_epoch.seen_data_nonces.contains(nonce)
    assert sess.session.path_state.candidate_path is None
    assert sess.session.path_state.active_path == ADDR_B
    assert sess.session.current_epoch.generation == 1
    # the retired grace legitimately expired during the crossing
    assert stats_after.retired_paths_expired == (
        stats_before.retired_paths_expired + 1
    )
    assert sess.session.path_state.retired_path is None


def test_B_retired_deadline_no_delay_control_admits_e1_straggler_once(env):
    """Same state, but the packet is fully processed BEFORE the retired
    deadline (no decrypt-time advance): the E1 straggler is admitted once
    under the same LogicalSession / assembly_namespace, session activity
    follows the contract, and A gets no reverse candidate and no pong."""
    state, sess, _e2_c2s, _e2_s2c = _e1_e2_migrated_session(env)
    retiring_epoch = sess.session.retiring_epoch
    namespace_hex = sess.session.assembly_namespace.hex()

    stats_before = state.stats()
    frames_before = len(sess.queue.items)

    nonce = os.urandom(12)
    e1_nmea = sess.nmea_packet(
        "!AIVDM,1,1,,A,e1-late,0*00", generation=0, nonce=nonce
    )
    socket = sess.feed([(e1_nmea, ADDR_A)], 50004.999)

    stats_after = state.stats()
    assert stats_after.data_nonces_accepted == (
        stats_before.data_nonces_accepted + 1
    )
    assert stats_after.sessions_touched == stats_before.sessions_touched + 1
    assert retiring_epoch.seen_data_nonces.contains(nonce)
    assert len(sess.queue.items) == frames_before + 1
    assert sess.queue.items[-1].assembler_key == (
        f"udpsec-assembly:{namespace_hex}"
    )
    # no reverse candidate, no ordinary pong / outbound authority to A
    assert sess.session.path_state.candidate_path is None
    assert sess.session.path_state.active_path == ADDR_B
    assert socket.sent == []
    # replay of the exact same datagram is still a replay
    replay_socket = sess.feed([(e1_nmea, ADDR_A)], 50005.5)
    assert len(sess.queue.items) == frames_before + 1
    assert replay_socket.sent == []


def test_C_current_e2_crossing_retired_deadline_opens_fresh_reverse_candidate(
    env,
):
    """A valid CURRENT-epoch (E2) NMEA from A crosses the retired deadline
    during decrypt: authoritative role becomes 'unknown' under the current
    epoch, which is legitimate new-path continuity -- the NMEA is admitted
    AND a fresh reverse candidate A is opened with a new token /
    path_generation / deadline, with one PATH_CHALLENGE to A. Active stays
    B until proof."""
    state, sess, e2_c2s, e2_s2c = _e1_e2_migrated_session(env)
    pg_before = sess.session.path_state.path_generation
    frames_before = len(sess.queue.items)
    namespace_hex = sess.session.assembly_namespace.hex()

    nonce = os.urandom(12)
    e2_nmea = sess.nmea_packet(
        "!AIVDM,1,1,,A,e2-newpath,0*00", generation=1, nonce=nonce, key=e2_c2s
    )
    clock = _FakeClock(50004.999)
    socket = sess.feed(
        [(e2_nmea, ADDR_A)],
        50004.999,
        clock=clock,
        on_decrypt=lambda: setattr(clock, "now", 50005.0),
        decrypt_epoch=sess.session.current_epoch,
    )

    cand = sess.session.path_state.candidate_path
    assert cand is not None
    assert cand.identity == normalize_sockaddr(ADDR_A)
    assert cand.path_generation == pg_before + 1
    assert cand.epoch is sess.session.current_epoch
    assert cand.deadline == 50005.0 + state._path_candidate_ttl
    assert sess.session.path_state.active_path == ADDR_B  # unchanged until proof

    challenge = sess.sole_challenge(
        socket, expect_addr=ADDR_A, generation=1, s2c_key=e2_s2c
    )
    assert challenge.path_generation == cand.path_generation
    assert challenge.challenge_token == cand.challenge_token

    # the NMEA still reached the queue under the same assembly namespace
    assert len(sess.queue.items) == frames_before + 1
    assert sess.queue.items[-1].assembler_key == (
        f"udpsec-assembly:{namespace_hex}"
    )
    assert sess.session.current_epoch.client_to_server_aesgcm.__class__ is not (
        _DecryptHook
    )

    # and the fresh candidate can now be proved to complete A -> ... -> A
    response = sess.path_response_packet(
        challenge.challenge_token, challenge.path_generation,
        generation=1, key=e2_c2s,
    )
    commit = sess.feed([(response, ADDR_A)], 50006.0)
    assert any(
        sess._is_type(d, p.PATH_ACK_TYPE, 1, s2c_key=e2_s2c)
        for d, _ in commit.sent
    )
    assert sess.session.path_state.active_path == ADDR_A


def test_D_candidate_deadline_crossing_during_decrypt_opens_fresh_incarnation(
    env,
):
    """Candidate B is live just before its TTL. A valid current-epoch NMEA
    from B crosses the candidate deadline during decrypt: the stale
    candidate authority is not reused -- the authoritative role is
    'unknown' and a FRESH candidate incarnation opens (new token, advanced
    path_generation, new deadline); the old values are never revived or
    extended."""
    state = env.new_state(path_candidate_ttl=10.0)
    sess = env.install_session(state, addr=ADDR_A, now=1000.0)

    open_b = sess.feed(
        [(sess.nmea_packet("!AIVDM,1,1,,A,first,0*00"), ADDR_B)], 1000.0
    )
    ch1 = sess.sole_challenge(open_b, expect_addr=ADDR_B)
    assert sess.session.path_state.candidate_path.deadline == 1010.0
    assert ch1.path_generation == 1
    frames_before = len(sess.queue.items)

    nonce = os.urandom(12)
    b_nmea = sess.nmea_packet(
        "!AIVDM,1,1,,A,second,0*00", nonce=nonce, generation=0
    )
    clock = _FakeClock(1009.999)
    open_b2 = sess.feed(
        [(b_nmea, ADDR_B)],
        1009.999,
        clock=clock,
        on_decrypt=lambda: setattr(clock, "now", 1010.0),
        decrypt_epoch=sess.session.current_epoch,
    )

    assert state.stats().path_candidates_expired == 1
    cand = sess.session.path_state.candidate_path
    assert cand is not None
    assert cand.challenge_token != ch1.challenge_token
    assert cand.path_generation == 2
    assert cand.deadline == 1010.0 + 10.0
    assert cand.deadline != 1010.0
    ch2 = sess.sole_challenge(open_b2, expect_addr=ADDR_B)
    assert ch2.path_generation == 2
    assert ch2.challenge_token == cand.challenge_token
    # the NMEA itself still queued
    assert len(sess.queue.items) == frames_before + 1
    # the stale token/generation can never commit
    stale_response = sess.path_response_packet(
        ch1.challenge_token, ch1.path_generation
    )
    stale_commit = sess.feed([(stale_response, ADDR_B)], 1011.0)
    assert not any(
        sess._is_type(d, p.PATH_ACK_TYPE, 0) for d, _ in stale_commit.sent
    )
    assert sess.session.path_state.active_path == ADDR_A


def test_E_blocked_at_authoritative_admission_fails_closed(env):
    """A source that is permissive ('unknown') at pre-decrypt but becomes a
    sibling session's active path ('blocked') before authoritative
    admission: the stale 'unknown' role is not trusted -- no nonce, no
    touch, no queue, no candidate, no challenge, and the sibling is
    untouched."""
    state = env.new_state()
    token = env.endpoint_token()
    sess = env.install_session(
        state, addr=ADDR_A, endpoint_token=token, now=1000.0
    )
    frames_before = len(sess.queue.items)
    stats_before = state.stats()

    sibling_holder = {}

    def _install_sibling_at_c():
        if not sibling_holder:
            sibling_holder["s"] = env.install_session(
                state, addr=ADDR_C, endpoint_token=token, now=1000.0
            )

    nonce = os.urandom(12)
    c_nmea = sess.nmea_packet(
        "!AIVDM,1,1,,A,from-c,0*00", nonce=nonce, generation=0
    )
    clock = _FakeClock(1000.0)
    socket = sess.feed(
        [(c_nmea, ADDR_C)],
        1000.0,
        clock=clock,
        on_decrypt=_install_sibling_at_c,
        decrypt_epoch=sess.session.current_epoch,
    )

    assert "s" in sibling_holder
    sibling = sibling_holder["s"].session
    stats_after = state.stats()
    assert stats_after.data_nonces_accepted == stats_before.data_nonces_accepted
    assert stats_after.sessions_touched == stats_before.sessions_touched
    assert stats_after.path_candidates_opened == (
        stats_before.path_candidates_opened
    )
    assert len(sess.queue.items) == frames_before
    assert socket.sent == []
    assert sess.session.path_state.candidate_path is None
    assert not sess.session.current_epoch.seen_data_nonces.contains(nonce)
    # sibling untouched
    assert state._sessions[sibling._session_key] is sibling
    assert sibling.path_state.active_path == ADDR_C


def test_F_stable_active_current_epoch_traffic_is_unchanged(env):
    """Regression guard: ordinary active-path current-epoch nmea / ping /
    close still behave exactly as before the P2 fix."""
    state = env.new_state()
    sess = env.install_session(state, addr=ADDR_A, now=1000.0)

    nmea_socket = sess.feed(
        [(sess.nmea_packet("!AIVDM,1,1,,A,plain,0*00"), ADDR_A)], 1001.0
    )
    assert nmea_socket.sent == []
    assert len(sess.queue.items) == 1
    assert state.stats().data_nonces_accepted == 1
    assert state.stats().sessions_touched == 1

    ping_socket = sess.feed([(sess.ping_packet(1), ADDR_A)], 1002.0)
    pongs = [
        (d, a) for d, a in ping_socket.sent if sess._is_type(d, "pong", 0)
    ]
    assert len(pongs) == 1 and pongs[0][1] == ADDR_A

    close_socket = sess.feed([(sess.close_packet(), ADDR_A)], 1003.0)
    assert close_socket.sent == []
    assert sess.session._session_key not in state._sessions


def test_G_path_response_admission_stays_specialized_no_double_admit(env):
    """PATH_RESPONSE nonce admission stays inside `commit_candidate_path`
    (atomic with the migration commit) and is NOT double-admitted through
    the ordinary path-data transaction."""
    state = env.new_state()
    sess = env.install_session(state, addr=ADDR_A, now=1000.0)
    open_b = sess.feed(
        [(sess.nmea_packet("!AIVDM,1,1,,A,x,0*00"), ADDR_B)], 1000.0
    )
    challenge = sess.sole_challenge(open_b, expect_addr=ADDR_B)
    accepted_before = state.stats().data_nonces_accepted

    resp_nonce = os.urandom(12)
    response = sess.path_response_packet(
        challenge.challenge_token, challenge.path_generation, nonce=resp_nonce
    )
    commit = sess.feed([(response, ADDR_B)], 1001.0)
    assert any(sess._is_type(d, p.PATH_ACK_TYPE, 0) for d, _ in commit.sent)
    assert sess.session.path_state.active_path == ADDR_B
    # exactly one nonce accounted for the PATH_RESPONSE (via commit), and
    # its nonce lives in the (unchanged) current epoch ledger once each.
    assert state.stats().data_nonces_accepted == accepted_before + 1
    assert sess.session.current_epoch.seen_data_nonces.contains(resp_nonce)
    # a byte replay does not re-admit or re-commit
    replay = sess.feed([(response, ADDR_B)], 1002.0)
    assert not any(sess._is_type(d, p.PATH_ACK_TYPE, 0) for d, _ in replay.sent)
    assert state.stats().data_nonces_accepted == accepted_before + 1
    assert state.stats().path_migrations_committed == 1
