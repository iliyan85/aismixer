"""UDPSEC V2 migration adversarial hardening -- acceptance (Major Prompt 7).

Major Prompt 4 built server-side path migration, Major Prompt 5 closed the
client-side migration/liveness choreography, and Major Prompt 6 closed the
migration x epoch-refresh x lifecycle race integration. Both server and
client code already carry substantial TOCTOU/epoch-boundary/lifecycle
hardening (see the lettered groups in `test_udpsec_path_migration.py`, the
A-G TOCTOU regressions there, and the corrective-round regressions in
`test_udpsec_client_path_migration.py`).

This file is the focused Major Prompt 7 ADVERSARIAL layer: it attacks the
REAL production state machine (never a shadow reimplementation) across the
required matrix (A1-A13), proves the hard per-session state bounds (no
address/token/response history, O(1) migration state per session), and
exercises the new MP7 observability counters -- all using the same
`_Env`/`_Session` harness `test_udpsec_path_migration.py` already
established, and the scripted-loop client harness
`test_udpsec_client_path_migration.py` already established.

Central security statement under test:

    An attacker may cause bounded rejection work, but must not gain
    authority, amplify traffic, or create history-scaled state.
"""

import json
import os

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import core.udpsec_protocol as p
from core.network_policy import NetworkPolicy

from test_secure_udp_helpers import (
    _FakeAsyncioModule,
    _FakeClock,
    _FakeQueue,
    _FakeSecureLoop,
    _FakeSecureSocket,
    _fresh_test_locator,
    load_secure_module_with_fake_keys,
)
from test_udpsec_path_migration import (
    ADDR_A,
    ADDR_B,
    ADDR_C,
    STATION_ID,
    _Env,
    _assert_no_commit,
    _open_candidate,
    _snapshot,
)
from test_udpsec_client_path_migration import (
    _ack_packet,
    _challenge_packet,
    _epochs,
    _establish,
    _handle,
)
from test_secure_udp_helpers import load_proxy_module

import asyncio as _asyncio


@pytest.fixture
def env(monkeypatch):
    secure, client_private_key = load_secure_module_with_fake_keys(
        monkeypatch, with_client_private_key=True
    )
    return _Env(monkeypatch, secure, client_private_key)


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


def _refresh_helpers():
    from test_udpsec_refresh import (
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


def _feed_with_policy(env, sess, state, packets, now, *, policy):
    """Drive one `_secure_server_loop` pass with an explicit ingress
    policy -- `_Session.feed()` has no policy hook, so this mirrors it
    directly (same pattern as the existing
    `test_denied_source_performs_no_migration_crypto_or_state`)."""
    fake_socket = _FakeSecureSocket()
    env.monkeypatch.setattr(
        env.secure, "asyncio",
        _FakeAsyncioModule(_FakeSecureLoop(list(packets))),
    )
    with pytest.raises(_asyncio.CancelledError):
        _asyncio.run(env.secure._secure_server_loop(
            fake_socket, sess.queue, "127.0.0.1", 9999,
            ingress_policy=policy,
            endpoint_token=sess.endpoint_token,
            state=state,
            wall_clock=_FakeClock(1_000_000.0),
            monotonic_clock=_FakeClock(now),
            server_private_key=env.server_private_key,
            owned_sessions=dict(state._sessions),
            owned_pending_sessions={},
        ))
    return fake_socket


# ==========================================================================
# A1. guessed session locator
# ==========================================================================


def test_a1_many_guessed_locators_create_no_retained_state(env):
    state = env.new_state()
    sess = env.install_session(state, addr=ADDR_A, now=1000.0)
    sessions_before = len(state._sessions)
    relation_index_before = len(state._relation_index)
    pending_before = len(state._pending_sessions)

    for i in range(500):
        bogus_locator = _fresh_test_locator()
        nonce = os.urandom(12)
        aad = p.build_data_aad(bogus_locator, 0)
        # A genuinely valid AEAD ciphertext (real key, matching nonce) --
        # the ONLY thing wrong with this datagram is the locator, so a
        # rejection here can only be attributed to the locator lookup miss,
        # never to an incidentally-malformed ciphertext.
        ciphertext = AESGCM(sess.c2s_key).encrypt(
            nonce,
            json.dumps({
                "type": "nmea", "payload": f"x{i}", "timestamp": 1,
                "source_id": STATION_ID,
            }).encode(),
            aad,
        )
        packet = p.build_data_packet(bogus_locator, 0, nonce, ciphertext)
        socket = sess.feed([(packet, ADDR_B)], 1000.0 + i)
        assert socket.sent == []
        # no retained per-guess state: every container stays exactly the
        # same size across the whole sweep.
        assert len(state._sessions) == sessions_before
        assert len(state._relation_index) == relation_index_before
        assert len(state._pending_sessions) == pending_before

    assert _snapshot(sess)["candidate"] is None
    assert sess.queue.items == []
    stats = state.stats()
    assert stats.path_candidates_opened == 0
    assert stats.sessions_touched == 0
    assert stats.data_nonces_accepted == 0


# ==========================================================================
# A2. authentic-but-never-admitted captured data
# ==========================================================================


def test_a2_authentic_but_never_admitted_response_gains_no_authority(env):
    """A cryptographically authentic PATH_RESPONSE whose candidate has
    ALREADY expired by the time it is processed is rejected before its
    nonce is ever admitted (deadline check precedes nonce admission in
    `commit_candidate_path`). Capturing and replaying that exact
    ciphertext later -- even from a different address -- must still fail,
    and for the RIGHT reason: it was never live authority, not a replay
    of previously-accepted evidence."""
    state = env.new_state(path_candidate_ttl=10.0)
    sess, challenge = _open_candidate(env, state, now=1000.0)
    response = sess.path_response_packet(
        challenge.challenge_token, challenge.path_generation
    )
    # authentic but arrives only after the candidate's deadline: never
    # admitted.
    _assert_no_commit(sess, response, ADDR_B, 1010.0)
    assert state.stats().data_nonce_replays == 0
    assert state.stats().migration_invalid_responses == 1

    # capture-and-replay later, and from an unrelated address C.
    replay_later = sess.feed([(response, ADDR_B)], 1020.0)
    assert replay_later.sent == []
    replay_elsewhere = sess.feed([(response, ADDR_C)], 1021.0)
    assert replay_elsewhere.sent == []
    # still never classified as a nonce replay -- it was never admitted.
    assert state.stats().data_nonce_replays == 0
    assert _snapshot(sess)["active"] == ADDR_A
    assert _snapshot(sess)["candidate"] is None


# ==========================================================================
# A3. previously admitted replay, across every path role
# ==========================================================================


def test_a3_admitted_replay_fails_from_every_path_role(env):
    state = env.new_state(retired_path_grace=1000.0)
    sess = env.install_session(state, addr=ADDR_A, now=1000.0)
    nonce = os.urandom(12)
    packet = sess.nmea_packet("!AIVDM,1,1,,A,shared,0*00", nonce=nonce)

    sess.feed([(packet, ADDR_A)], 1000.0)  # admitted once, from active A
    assert sess.session.current_epoch.seen_data_nonces.contains(nonce)

    # replay from the active path itself
    r_active = sess.feed([(packet, ADDR_A)], 1001.0)
    assert r_active.sent == []

    # migrate A -> B, so a candidate/active/retired triad exists
    sess.migrate(ADDR_A, ADDR_B, now=1002.0)
    accepted_after_migration = state.stats().data_nonces_accepted

    # replay from the (now active) B
    r_active_new = sess.feed([(packet, ADDR_B)], 1003.0)
    assert r_active_new.sent == []
    # replay from the (now retired) A
    r_retired = sess.feed([(packet, ADDR_A)], 1004.0)
    assert r_retired.sent == []
    # replay from a totally unrelated, never-seen address D
    r_unrelated = sess.feed([(packet, ("203.0.113.77", 4444))], 1005.0)
    assert r_unrelated.sent == []

    assert state.stats().data_nonces_accepted == accepted_after_migration
    assert state.stats().data_nonce_replays >= 4
    # no migration counters falsely record success/attempt from replay.
    assert state.stats().path_candidates_opened == 1  # only the real B open
    assert state.stats().path_migrations_committed == 1  # only the real commit


# ==========================================================================
# A4. outer-header / source-path rewrite
# ==========================================================================


def test_a4_fresh_traffic_from_new_source_only_proposes_a_candidate(env):
    """Genuinely fresh, never-before-seen authenticated traffic arriving
    from a brand-new source address (the only thing an outer-path rewrite
    can actually produce, since ciphertext/AAD cannot be forged) opens a
    CANDIDATE only -- it can never immediately become the active path, no
    matter how authentic the ciphertext is."""
    state = env.new_state()
    sess = env.install_session(state, addr=ADDR_A, now=1000.0)
    socket = sess.feed(
        [(sess.nmea_packet("!AIVDM,1,1,,A,fresh,0*00"), ADDR_B)], 1000.0
    )
    snap = _snapshot(sess)
    assert snap["active"] == ADDR_A  # unchanged -- no immediate authority
    assert snap["candidate"] is not None
    assert snap["candidate"]["sockaddr"] == ADDR_B
    challenges = [
        a for d, a in socket.sent if sess._is_type(d, p.PATH_CHALLENGE_TYPE, 0)
    ]
    assert challenges == [ADDR_B]

    # return-routability proof is still required before B gains authority:
    # authentic traffic alone (already proven above) is not enough, and
    # only a matching PATH_RESPONSE from the exact candidate address can
    # commit.
    challenge = sess.sole_challenge(socket, expect_addr=ADDR_B)
    response = sess.path_response_packet(
        challenge.challenge_token, challenge.path_generation
    )
    commit = sess.feed([(response, ADDR_B)], 1000.5)
    assert any(sess._is_type(d, p.PATH_ACK_TYPE, 0) for d, _ in commit.sent)
    assert _snapshot(sess)["active"] == ADDR_B


# ==========================================================================
# A5. redirection attempts
# ==========================================================================


def test_a5_redirection_requires_exact_current_candidate_proof(env):
    """Composite anti-redirection matrix: an attacker at victim address V
    (who is NEVER the live candidate) cannot redirect the session to V by
    any combination of stolen token, guessed token, right token from the
    wrong address, or a stale response replayed after candidate
    replacement. `active_path` only ever changes via an exact match from
    the live candidate's own address."""
    state = env.new_state(path_candidate_ttl=1000.0)
    sess = env.install_session(state, addr=ADDR_A, now=1000.0)
    V = ("203.0.113.66", 6666)

    open_b = sess.feed(
        [(sess.nmea_packet("!AIVDM,1,1,,A,x,0*00"), ADDR_B)], 1000.0
    )
    challenge_b = sess.sole_challenge(open_b, expect_addr=ADDR_B)

    # (a) V replays/guesses B's real token+generation, but answers from V.
    stolen = sess.path_response_packet(
        challenge_b.challenge_token, challenge_b.path_generation
    )
    r = sess.feed([(stolen, V)], 1001.0)
    assert not any(sess._is_type(d, p.PATH_ACK_TYPE, 0) for d, _ in r.sent)
    assert _snapshot(sess)["active"] == ADDR_A

    # (b) V sends a random token with the right generation.
    random_token = sess.path_response_packet(
        os.urandom(p.PATH_CHALLENGE_TOKEN_BYTES), challenge_b.path_generation
    )
    r = sess.feed([(random_token, V)], 1001.5)
    assert not any(sess._is_type(d, p.PATH_ACK_TYPE, 0) for d, _ in r.sent)
    assert _snapshot(sess)["active"] == ADDR_A

    # the REAL B now proves return routability and legitimately migrates.
    real = sess.path_response_packet(
        challenge_b.challenge_token, challenge_b.path_generation
    )
    commit = sess.feed([(real, ADDR_B)], 1002.0)
    assert any(sess._is_type(d, p.PATH_ACK_TYPE, 0) for d, _ in commit.sent)
    assert _snapshot(sess)["active"] == ADDR_B

    # (c) V now replays the (already-consumed) B token after commit.
    stale = sess.feed([(real, V)], 1003.0)
    assert stale.sent == []
    assert _snapshot(sess)["active"] == ADDR_B

    # (d) a fresh candidate C opens; V tries C's token/generation from V.
    open_c = sess.feed(
        [(sess.nmea_packet("!AIVDM,1,1,,A,c,0*00"), ADDR_C)], 1004.0
    )
    challenge_c = sess.sole_challenge(open_c, expect_addr=ADDR_C)
    stolen_c = sess.path_response_packet(
        challenge_c.challenge_token, challenge_c.path_generation
    )
    r = sess.feed([(stolen_c, V)], 1005.0)
    assert not any(sess._is_type(d, p.PATH_ACK_TYPE, 0) for d, _ in r.sent)
    assert _snapshot(sess)["active"] == ADDR_B

    # V never received a single PATH_ACK across the whole scenario.
    assert state.stats().path_migrations_committed == 1


# ==========================================================================
# A6 / A13 / Part H. high candidate churn -- O(1) retained state
# ==========================================================================


def test_a6_high_candidate_churn_retains_o1_state(env):
    """Thousands of legitimate candidate replacements on one live session,
    driven directly through the REAL `open_or_replace_candidate_path`
    state-machine method (bypassing AEAD/asyncio overhead -- this is a
    structural/state-retention proof, not a timing benchmark). After the
    churn: exactly one live candidate object, no historical
    address/token/deadline collection, no candidate-deadline heap, and
    stats counters equal exactly the expected event counts."""
    state = env.new_state()
    sess = env.install_session(state, addr=ADDR_A, now=1000.0)
    session = sess.session
    epoch = session.current_epoch

    assert not hasattr(state, "_path_transition_heap")
    assert not hasattr(state, "_candidate_history")
    assert not hasattr(state, "_push_path_transition")

    CHURN = 5000
    seen_token_ids = set()
    last_candidate = None
    for i in range(CHURN):
        # `40000 + i` alone makes every (ip, port) tuple globally distinct
        # across the whole loop, so every iteration is a genuine
        # replacement (never a same-address "duplicate").
        addr = (f"198.51.{(i // 250) % 250}.{(i % 250) + 1}", 40000 + i)
        candidate, outcome, replaced = state.open_or_replace_candidate_path(
            session, epoch, addr, os.urandom(p.PATH_CHALLENGE_TOKEN_BYTES),
            1000.0 + i,
        )
        assert outcome == "installed"
        # F1 field-diagnostics correction: `replaced` is read straight off
        # this same locked call's own decision (i == 0 is the session's
        # first-ever candidate; every later iteration genuinely displaces
        # the previous live one -- see the loop comment above).
        assert replaced is (i > 0)
        last_candidate = candidate
        # exactly one live candidate object at all times.
        assert session.path_state.candidate_path is last_candidate
        assert session.path_state.candidate_path is not None
        seen_token_ids.add(id(candidate.challenge_token))

    stats = state.stats()
    assert stats.current_candidate_paths == 1
    assert stats.current_retired_paths == 0  # no commit ever happened
    # `path_candidates_opened` counts every successful install (a fresh
    # open OR a replacement); `path_candidates_replaced` is the strict
    # subset of those installs that displaced an existing live candidate.
    assert stats.path_candidates_opened == CHURN
    assert stats.path_candidates_replaced == CHURN - 1
    assert session.path_state.path_generation == CHURN

    # no historical address/token collection anywhere on the session or
    # state object -- only the single live `CandidatePath`.
    assert not hasattr(session.path_state, "candidate_history")
    assert not hasattr(session, "candidate_history")
    assert not hasattr(state, "_candidate_token_ledger")


def test_a13_candidate_churn_does_not_grow_process_wide_heaps(env):
    """Companion to A6: candidate replacement never pushes anything onto
    the process-wide epoch-transition heap (that heap exists only for
    pending/retiring EPOCH transitions, which are wholly independent of
    path-migration state)."""
    state = env.new_state()
    sess = env.install_session(state, addr=ADDR_A, now=1000.0)
    session = sess.session
    epoch = session.current_epoch
    heap_len_before = len(state._epoch_transition_heap)

    for i in range(2000):
        addr = (f"198.51.100.{(i % 250) + 1}", 41000 + i)
        state.open_or_replace_candidate_path(
            session, epoch, addr, os.urandom(p.PATH_CHALLENGE_TOKEN_BYTES),
            1000.0 + i,
        )

    assert len(state._epoch_transition_heap) == heap_len_before


# ==========================================================================
# A7. stale token / generation response permutations
# ==========================================================================


@pytest.mark.parametrize(
    "case",
    [
        "right_token_old_generation",
        "old_token_right_generation",
        "old_token_old_generation",
        "random_token_right_generation",
        "duplicate_after_commit",
    ],
)
def test_a7_stale_token_generation_permutations_never_commit(env, case):
    state = env.new_state(path_candidate_ttl=1000.0)
    sess = env.install_session(state, addr=ADDR_A, now=1000.0)

    open_b = sess.feed(
        [(sess.nmea_packet("!AIVDM,1,1,,A,b1,0*00"), ADDR_B)], 1000.0
    )
    challenge_1 = sess.sole_challenge(open_b, expect_addr=ADDR_B)

    open_b2 = sess.feed(
        [(sess.nmea_packet("!AIVDM,1,1,,A,b2,0*00"), ADDR_C)], 1001.0
    )
    challenge_2 = sess.sole_challenge(open_b2, expect_addr=ADDR_C)
    deadline_before = _snapshot(sess)["candidate"]["deadline"]

    invalid_before = state.stats().migration_invalid_responses

    if case == "right_token_old_generation":
        bad = sess.path_response_packet(
            challenge_2.challenge_token, challenge_1.path_generation
        )
        addr = ADDR_C
    elif case == "old_token_right_generation":
        bad = sess.path_response_packet(
            challenge_1.challenge_token, challenge_2.path_generation
        )
        addr = ADDR_C
    elif case == "old_token_old_generation":
        bad = sess.path_response_packet(
            challenge_1.challenge_token, challenge_1.path_generation
        )
        addr = ADDR_C
    elif case == "random_token_right_generation":
        bad = sess.path_response_packet(
            os.urandom(p.PATH_CHALLENGE_TOKEN_BYTES), challenge_2.path_generation
        )
        addr = ADDR_C
    else:  # duplicate_after_commit
        good = sess.path_response_packet(
            challenge_2.challenge_token, challenge_2.path_generation
        )
        commit = sess.feed([(good, ADDR_C)], 1002.0)
        assert any(
            sess._is_type(d, p.PATH_ACK_TYPE, 0) for d, _ in commit.sent
        )
        bad = good  # replay the exact same (now-consumed) response
        addr = ADDR_C

    socket = sess.feed([(bad, addr)], 1003.0)
    assert not any(sess._is_type(d, p.PATH_ACK_TYPE, 0) for d, _ in socket.sent)
    if case != "duplicate_after_commit":
        assert _snapshot(sess)["active"] == ADDR_A
        # invalid response never refreshes/extends the surviving candidate.
        assert _snapshot(sess)["candidate"]["path_generation"] == (
            challenge_2.path_generation
        )
        assert _snapshot(sess)["candidate"]["deadline"] == deadline_before
        assert state.stats().migration_invalid_responses == invalid_before + 1
    else:
        assert _snapshot(sess)["active"] == ADDR_C  # from the real commit
        assert state.stats().data_nonce_replays >= 1


# ==========================================================================
# A8. cross-epoch response
# ==========================================================================


def test_a8_response_under_pending_e2_before_commit_cannot_migrate(env):
    """A response encrypted under the PENDING (not-yet-committed) E2
    candidate epoch cannot authorize migration even though the candidate
    itself is still bound to the live current E1: `commit_candidate_path`
    requires the response's own epoch to have live role 'current', and a
    merely-pending epoch's role is 'pending', not 'current'."""
    build_init, process_init, derive_e2, build_confirm, process_confirm = (
        _refresh_helpers()
    )
    state = env.new_state(path_candidate_ttl=1000.0)
    sess = env.install_session(state, addr=ADDR_A, now=1000.0)
    open_b = sess.feed(
        [(sess.nmea_packet("!AIVDM,1,1,,A,x,0*00"), ADDR_B)], 1000.0
    )
    challenge = sess.sole_challenge(open_b, expect_addr=ADDR_B)

    init = build_init(env, sess.session)
    reply_packet, _ = process_init(env, state, sess.session, init, now=1001.0)
    km, _ = derive_e2(env, sess.session, init, reply_packet, sess.s2c_key)
    assert sess.session.pending_epoch is not None
    assert sess.session.current_epoch.generation == 0

    pending_c2s = km.client_to_server_key
    response_under_pending = sess.path_response_packet(
        challenge.challenge_token, challenge.path_generation,
        generation=1, key=pending_c2s,
    )
    r = sess.feed([(response_under_pending, ADDR_B)], 1002.0)
    assert r.sent == []
    assert _snapshot(sess)["active"] == ADDR_A
    assert _snapshot(sess)["candidate"] is not None


# ==========================================================================
# A9. cross-listener locator / endpoint confusion
# ==========================================================================


@pytest.mark.parametrize("same_raw_address", [True, False])
def test_a9_cross_listener_locator_confusion_isolated(env, same_raw_address):
    state = env.new_state()
    token_x = env.endpoint_token()
    token_y = env.endpoint_token()
    sess_x = env.install_session(
        state, addr=ADDR_A, endpoint_token=token_x, now=1000.0
    )
    addr_for_y = ADDR_A if same_raw_address else ADDR_B
    sess_y = env.install_session(
        state, addr=addr_for_y, endpoint_token=token_y,
        locator=sess_x.locator, now=1000.0,
    )
    assert sess_x.locator == sess_y.locator  # colliding raw bytes on purpose

    before_x = state.stats()

    # traffic addressed under listener Y's endpoint token, using X's
    # keys/locator, must not select X's session (nor Y's, since the AEAD
    # tag won't verify under Y's independently-generated keys either --
    # the point is no cross-namespace SESSION SELECTION occurs at all).
    packet = sess_x.nmea_packet("!AIVDM,1,1,,A,x,0*00")
    fake_socket = _FakeSecureSocket()
    env.monkeypatch.setattr(
        env.secure, "asyncio",
        _FakeAsyncioModule(_FakeSecureLoop([(packet, ADDR_A)])),
    )
    with pytest.raises(_asyncio.CancelledError):
        _asyncio.run(env.secure._secure_server_loop(
            fake_socket, sess_y.queue, "127.0.0.1", 9999,
            endpoint_token=token_y,
            state=state,
            wall_clock=_FakeClock(1_000_000.0),
            monotonic_clock=_FakeClock(1001.0),
            server_private_key=env.server_private_key,
            owned_sessions=dict(state._sessions),
            owned_pending_sessions={},
        ))
    assert fake_socket.sent == []
    assert sess_y.queue.items == []
    after = state.stats()
    assert after.sessions_touched == before_x.sessions_touched
    assert after.path_candidates_opened == before_x.path_candidates_opened
    assert _snapshot(sess_x)["candidate"] is None
    assert _snapshot(sess_y)["candidate"] is None


# ==========================================================================
# A10. source-policy bypass attempt
# ==========================================================================


def test_a10_denied_source_blocks_ordinary_data_before_crypto(env):
    state = env.new_state()
    sess = env.install_session(state, addr=ADDR_A, now=1000.0)
    policy = NetworkPolicy.from_entries(
        [ADDR_A[0]], context="sec_inputs[0].allow_from"
    )
    before = state.stats()

    socket = _feed_with_policy(
        env, sess, state,
        [(sess.nmea_packet("!AIVDM,1,1,,A,x,0*00"), ADDR_B)], 1000.0,
        policy=policy,
    )
    assert socket.sent == []
    assert sess.queue.items == []
    after = state.stats()
    assert after.sessions_touched == before.sessions_touched
    assert after.path_candidates_opened == before.path_candidates_opened
    assert after.data_nonces_accepted == before.data_nonces_accepted
    assert _snapshot(sess)["candidate"] is None


def test_a10_denied_source_blocks_path_response_before_crypto(env):
    """Same ordering guarantee for a PATH_RESPONSE specifically -- the
    source-policy gate at the very top of `_secure_server_loop` applies
    uniformly to every message kind, so a denied source cannot commit a
    migration even with a perfectly valid token/generation."""
    state = env.new_state()
    sess, challenge = _open_candidate(env, state, now=1000.0)
    policy = NetworkPolicy.from_entries(
        [ADDR_A[0]], context="sec_inputs[0].allow_from"
    )
    response = sess.path_response_packet(
        challenge.challenge_token, challenge.path_generation
    )
    before = state.stats()

    socket = _feed_with_policy(
        env, sess, state, [(response, ADDR_B)], 1001.0, policy=policy,
    )
    assert socket.sent == []
    after = state.stats()
    assert after.path_migrations_committed == before.path_migrations_committed
    assert after.migration_invalid_responses == before.migration_invalid_responses
    assert after.data_nonces_accepted == before.data_nonces_accepted
    assert _snapshot(sess)["active"] == ADDR_A
    assert _snapshot(sess)["candidate"] is not None  # untouched, still live


def test_a10_denied_source_blocks_any_message_type_before_crypto(env):
    """The source-policy gate is the very first check in
    `_secure_server_loop` (`if not policy.allows(source_ip): continue`),
    before any packet parsing or message-type dispatch at all -- so it
    applies identically to ordinary DATA, PATH_RESPONSE, and refresh
    controls (REFRESH_INIT/CONFIRM) alike. Proven content-agnostically:
    even structurally arbitrary bytes (which could be any message type on
    the wire) from a denied source never reach parsing, decrypt, or any
    state mutation."""
    state = env.new_state()
    sess = env.install_session(state, addr=ADDR_A, now=1000.0)
    policy = NetworkPolicy.from_entries(
        [ADDR_A[0]], context="sec_inputs[0].allow_from"
    )
    before = state.stats()

    arbitrary_bytes = os.urandom(64)
    socket = _feed_with_policy(
        env, sess, state, [(arbitrary_bytes, ADDR_B)], 1000.0, policy=policy,
    )
    assert socket.sent == []
    after = state.stats()
    assert after.sessions_touched == before.sessions_touched
    assert after.pending_epochs_created == before.pending_epochs_created
    assert after.data_nonces_accepted == before.data_nonces_accepted
    assert sess.session.pending_epoch is None


# ==========================================================================
# A11. anti-amplification
# ==========================================================================


def test_a11_response_counts_bounded_by_category(env):
    state = env.new_state(path_candidate_ttl=1000.0)
    sess = env.install_session(state, addr=ADDR_A, now=1000.0)

    # unknown locator -> 0 responses
    bogus = p.build_data_packet(
        _fresh_test_locator(), 0, os.urandom(12), os.urandom(32)
    )
    assert sess.feed([(bogus, ADDR_B)], 1000.0).sent == []

    # malformed / failed AEAD -> 0 responses
    garbage = p.build_data_packet(
        sess.locator, 0, os.urandom(12), os.urandom(32)
    )
    assert sess.feed([(garbage, ADDR_B)], 1000.1).sent == []

    # first valid new-candidate DATA -> bounded challenge behaviour (<=1)
    first = sess.feed(
        [(sess.nmea_packet("!AIVDM,1,1,,A,first,0*00"), ADDR_B)], 1000.2
    )
    challenge = sess.sole_challenge(first, expect_addr=ADDR_B)
    assert len(first.sent) == 1

    # duplicate traffic from the SAME live candidate -> 0 additional output
    dup1 = sess.feed(
        [(sess.nmea_packet("!AIVDM,1,1,,A,dup1,0*00"), ADDR_B)], 1000.3
    )
    assert dup1.sent == []
    dup2 = sess.feed(
        [(sess.nmea_packet("!AIVDM,1,1,,A,dup2,0*00"), ADDR_B)], 1000.4
    )
    assert dup2.sent == []

    # stale/invalid response -> 0 responses
    stale = sess.path_response_packet(os.urandom(32), challenge.path_generation)
    assert sess.feed([(stale, ADDR_B)], 1000.5).sent == []

    # replacing candidate DATA (from C) -> bounded challenge behaviour (<=1)
    replace = sess.feed(
        [(sess.nmea_packet("!AIVDM,1,1,,A,replace,0*00"), ADDR_C)], 1000.6
    )
    assert len(replace.sent) == 1
    challenge_c = sess.sole_challenge(replace, expect_addr=ADDR_C)

    # the old B token is now doubly stale (replaced) -> 0 responses
    stale_b = sess.path_response_packet(
        challenge.challenge_token, challenge.path_generation
    )
    assert sess.feed([(stale_b, ADDR_B)], 1000.7).sent == []

    # retired-path traffic -> existing ordinary behaviour only (no pong,
    # no challenge) once a real commit has happened
    real = sess.path_response_packet(
        challenge_c.challenge_token, challenge_c.path_generation
    )
    commit = sess.feed([(real, ADDR_C)], 1001.0)
    assert len(commit.sent) == 1  # exactly the PATH_ACK
    retired_traffic = sess.feed(
        [(sess.nmea_packet("!AIVDM,1,1,,A,retired-ping,0*00"), ADDR_A)], 1001.1
    )
    assert retired_traffic.sent == []
    retired_ping = sess.feed([(sess.ping_packet(1), ADDR_A)], 1001.2)
    assert not any(
        sess._is_type(d, "pong", 0) for d, _ in retired_ping.sent
    )


# ==========================================================================
# A12. nonce exhaustion during migration
# ==========================================================================


def test_a12_nonce_exhaustion_during_migration_is_terminal_and_uncounted_twice(
    env,
):
    state = env.new_state(data_nonce_max_per_session=3)
    sess = env.install_session(state, addr=ADDR_A, now=1000.0)
    session_obj = sess.session
    used = len(session_obj.current_epoch.seen_data_nonces)
    for i in range(state._data_nonce_max_per_session - used - 1):
        sess.feed(
            [(sess.nmea_packet(f"!AIVDM,1,1,,A,fill-{i},0*00"), ADDR_A)],
            1000.0 + i,
        )
    open_b = sess.feed(
        [(sess.nmea_packet("!AIVDM,1,1,,A,open-b,0*00"), ADDR_B)], 1001.0
    )
    challenge = sess.sole_challenge(open_b, expect_addr=ADDR_B)
    assert len(session_obj.current_epoch.seen_data_nonces) == (
        state._data_nonce_max_per_session
    )

    invalid_before = state.stats().migration_invalid_responses
    response = sess.path_response_packet(
        challenge.challenge_token, challenge.path_generation
    )
    commit_socket = sess.feed([(response, ADDR_B)], 1002.0)
    assert not any(
        sess._is_type(d, p.PATH_ACK_TYPE, 0) for d, _ in commit_socket.sent
    )
    stats = state.stats()
    assert stats.data_nonce_exhaustions == 1
    # exhaustion must not ALSO be double-counted as an "invalid response".
    assert stats.migration_invalid_responses == invalid_before
    assert session_obj._session_key not in state._sessions

    # delayed retry after teardown cannot resurrect it.
    retry = sess.feed([(response, ADDR_B)], 1003.0)
    assert retry.sent == []


# ==========================================================================
# Part E. session lifecycle counters remain clean
# ==========================================================================


def test_e_session_lifecycle_counters_unchanged_through_full_migration(env):
    state = env.new_state(retired_path_grace=5.0)
    sess = env.install_session(state, addr=ADDR_A, now=1000.0)
    baseline = state.stats()

    def assert_unchanged(stats):
        assert stats.sessions_created == baseline.sessions_created
        assert stats.sessions_replaced == baseline.sessions_replaced
        assert stats.pending_sessions_created == baseline.pending_sessions_created
        assert stats.pending_sessions_promoted == (
            baseline.pending_sessions_promoted
        )

    open_b = sess.feed(
        [(sess.nmea_packet("!AIVDM,1,1,,A,x,0*00"), ADDR_B)], 1000.0
    )
    assert_unchanged(state.stats())
    challenge = sess.sole_challenge(open_b, expect_addr=ADDR_B)

    response = sess.path_response_packet(
        challenge.challenge_token, challenge.path_generation
    )
    commit = sess.feed([(response, ADDR_B)], 1001.0)
    assert any(sess._is_type(d, p.PATH_ACK_TYPE, 0) for d, _ in commit.sent)
    assert_unchanged(state.stats())

    late = sess.feed(
        [(sess.nmea_packet("!AIVDM,1,1,,A,late,0*00"), ADDR_A)], 1002.0
    )
    assert_unchanged(state.stats())

    state.cleanup_expired_path_transitions(1010.0)
    assert_unchanged(state.stats())


# ==========================================================================
# Part D/16. migration counters -- exact semantics
# ==========================================================================


def test_d_migration_counter_exact_semantics(env):
    state = env.new_state(path_candidate_ttl=5.0, retired_path_grace=5.0)
    sess = env.install_session(state, addr=ADDR_A, now=1000.0)

    s0 = state.stats()
    assert (
        s0.migration_challenges_sent
        == s0.migration_invalid_responses
        == s0.retired_path_packets_admitted
        == s0.path_candidates_opened
        == s0.path_candidates_replaced
        == s0.path_candidates_expired
        == s0.path_migrations_committed
        == s0.retired_paths_expired
        == 0
    )

    # open -> +1 opened, +1 challenge sent
    open_b = sess.feed(
        [(sess.nmea_packet("!AIVDM,1,1,,A,x,0*00"), ADDR_B)], 1000.0
    )
    challenge_b = sess.sole_challenge(open_b, expect_addr=ADDR_B)
    s1 = state.stats()
    assert s1.path_candidates_opened == 1
    assert s1.migration_challenges_sent == 1
    assert s1.path_candidates_replaced == 0

    # duplicate -> no change at all
    sess.feed([(sess.nmea_packet("!AIVDM,1,1,,A,x2,0*00"), ADDR_B)], 1000.5)
    s2 = state.stats()
    assert s2.path_candidates_opened == 1
    assert s2.migration_challenges_sent == 1

    # replace -> +1 opened (every successful install counts), +1 replaced
    # (the strict displaced-a-live-candidate subset), +1 challenge sent
    open_c = sess.feed(
        [(sess.nmea_packet("!AIVDM,1,1,,A,c,0*00"), ADDR_C)], 1001.0
    )
    challenge_c = sess.sole_challenge(open_c, expect_addr=ADDR_C)
    s3 = state.stats()
    assert s3.path_candidates_opened == 2
    assert s3.path_candidates_replaced == 1
    assert s3.migration_challenges_sent == 2

    # invalid response -> +1 invalid, nothing else
    bad = sess.path_response_packet(os.urandom(32), challenge_c.path_generation)
    sess.feed([(bad, ADDR_C)], 1001.5)
    s4 = state.stats()
    assert s4.migration_invalid_responses == 1
    assert s4.path_migrations_committed == 0

    # commit -> +1 committed, nothing else changes
    good = sess.path_response_packet(
        challenge_c.challenge_token, challenge_c.path_generation
    )
    commit = sess.feed([(good, ADDR_C)], 1002.0)
    assert any(sess._is_type(d, p.PATH_ACK_TYPE, 0) for d, _ in commit.sent)
    s5 = state.stats()
    assert s5.path_migrations_committed == 1
    assert s5.migration_invalid_responses == 1  # unchanged
    assert s5.path_candidates_opened == 2  # unchanged
    assert s5.path_candidates_replaced == 1  # unchanged

    # retired-path admitted packet -> +1
    sess.feed(
        [(sess.nmea_packet("!AIVDM,1,1,,A,retired,0*00"), ADDR_A)], 1002.5
    )
    s6 = state.stats()
    assert s6.retired_path_packets_admitted == 1

    # candidate expiry (idle sweep) and retired expiry -> +1 each
    open_d = sess.feed(
        [(sess.nmea_packet("!AIVDM,1,1,,A,d,0*00"), ("198.51.100.99", 9))],
        1003.0,
    )
    sess.sole_challenge(open_d, expect_addr=("198.51.100.99", 9))
    state.cleanup_expired_path_transitions(1010.0)
    s7 = state.stats()
    assert s7.path_candidates_expired == 1
    assert s7.retired_paths_expired == 1
    # commit/invalid/challenge-sent counters unaffected by pure expiry.
    assert s7.path_migrations_committed == 1
    assert s7.migration_invalid_responses == 1


# ==========================================================================
# Part K. bounded token / generation semantics -- MAX_PATH_GENERATION
# ==========================================================================


def test_k_path_generation_boundary_fails_closed_without_wrap(env):
    state = env.new_state()
    sess = env.install_session(state, addr=ADDR_A, now=1000.0)
    session = sess.session
    epoch = session.current_epoch

    session.path_state.path_generation = p.MAX_PATH_GENERATION - 1
    candidate, outcome, replaced = state.open_or_replace_candidate_path(
        session, epoch, ADDR_B, os.urandom(p.PATH_CHALLENGE_TOKEN_BYTES),
        1000.0,
    )
    assert outcome == "installed"
    assert replaced is False
    assert candidate.path_generation == p.MAX_PATH_GENERATION
    assert session.path_state.path_generation == p.MAX_PATH_GENERATION

    # one more legitimate replacement must fail closed rather than wrap.
    candidate2, outcome2, replaced2 = state.open_or_replace_candidate_path(
        session, epoch, ADDR_C, os.urandom(p.PATH_CHALLENGE_TOKEN_BYTES),
        1001.0,
    )
    assert (candidate2, outcome2, replaced2) == (None, None, None)
    assert session.path_state.candidate_path is candidate  # unchanged
    assert session.path_state.path_generation == p.MAX_PATH_GENERATION


# ==========================================================================
# Part J. client-side adversarial regressions
# ==========================================================================


def test_j_path_challenge_under_pending_epoch_is_ignored(proxy, keys):
    """Mirror of the existing `test_path_ack_under_pending_epoch_is_ignored`
    for PATH_CHALLENGE: the same exact-current-epoch gate in
    `_try_handle_path_message` applies to both message kinds."""
    epochs = _epochs(proxy, keys)
    migration = proxy._ClientPathMigration()
    from test_udpsec_client_path_migration import _RecordingSock

    sock = _RecordingSock(proxy, keys)
    pending_c2s = AESGCM.generate_key(bit_length=256)
    pending_s2c = AESGCM.generate_key(bit_length=256)
    epochs.set_pending(1, pending_c2s, pending_s2c)

    token = os.urandom(32)
    challenge_packet = _challenge_packet(
        proxy, keys, token=token, generation=1, key=pending_s2c,
        epoch_generation=1,
    )
    result, ping_seq = _handle(proxy, keys, epochs, migration, sock, challenge_packet)
    assert result is None
    assert sock.sent == []  # no PATH_RESPONSE drawn from a pending-epoch challenge


def test_j_adversarial_ack_cannot_grant_liveness_or_clear_unrelated_ping(
    proxy, keys
):
    """Composite regression: a wrong-token ACK and a wrong-station-id ACK
    both fail to grant liveness or clear an outstanding ping; only the
    exact matching ACK does either."""
    epochs = _epochs(proxy, keys)
    migration = proxy._ClientPathMigration()
    from test_udpsec_client_path_migration import _RecordingSock

    sock = _RecordingSock(proxy, keys)
    token = os.urandom(32)
    _establish(
        proxy, keys, epochs, migration, sock, token=token, generation=1,
        expected_ping_seq=17,
    )

    wrong_token_ack = _ack_packet(proxy, keys, token=os.urandom(32), generation=1)
    result, ping_seq = _handle(
        proxy, keys, epochs, migration, sock, wrong_token_ack,
        expected_ping_seq=17,
    )
    assert result == proxy.SERVER_PACKET_IGNORED
    assert ping_seq is None

    wrong_station_ack = _ack_packet(
        proxy, keys, token=token, generation=1, station_id="other_boat",
    )
    result, ping_seq = _handle(
        proxy, keys, epochs, migration, sock, wrong_station_ack,
        expected_ping_seq=17,
    )
    assert result is None
    assert ping_seq is None

    # only the exact matching ACK grants liveness and clears ping 17.
    good_ack = _ack_packet(proxy, keys, token=token, generation=1)
    result, ping_seq = _handle(
        proxy, keys, epochs, migration, sock, good_ack, expected_ping_seq=17,
    )
    assert result == proxy.SERVER_PACKET_PATH_MIGRATED
    assert ping_seq == 17
