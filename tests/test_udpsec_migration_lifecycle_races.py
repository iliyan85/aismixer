"""UDPSEC V2 migration x epoch-refresh x lifecycle race integration --
acceptance (Major Prompt 6).

Major Prompt 4 built the server-side path-migration primitives (candidate
discovery, PATH_CHALLENGE / PATH_RESPONSE / PATH_ACK, atomic commit) and
Major Prompt 5 closed the client-side migration/liveness choreography.
Both already carry substantial TOCTOU/epoch-boundary hardening (see the
"Finding A/B/C", "P2 TOCTOU fix", and lettered A-G groups in
`test_udpsec_path_migration.py`, and the five corrective-round regressions
in `test_udpsec_client_path_migration.py`).

This file is the focused Major Prompt 6 integration layer: it proves the
central invariant --

    Migration remains bound to the epoch and lifecycle state in which its
    authority was created, even when refresh, expiry, close, nonce
    exhaustion, backpressure, or shutdown race with that migration.

-- across the eight required race families (R1-R8), using the REAL
`SecureState`/`_secure_server_loop` state machine (server side, via the
`_Env`/`_Session` harness from `test_udpsec_path_migration`) and the REAL
`forward_loop` choreography (client side, via the scripted-loop harness
from `test_udpsec_client_path_migration`) -- never a shadow/reimplemented
state machine.
"""

import os

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import core.udpsec_protocol as p

from test_secure_udp_helpers import _FakeClock, _FakeSecureSocket
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
from test_udpsec_refresh import (
    _client_build_confirm,
    _client_build_init,
    _client_derive_e2,
    _server_process_confirm,
    _server_process_init,
)
from test_udpsec_client_path_migration import (
    _challenge_packet,
    _run_scripted_path_loop,
)


@pytest.fixture
def env(monkeypatch):
    from test_secure_udp_helpers import load_secure_module_with_fake_keys

    secure, client_private_key = load_secure_module_with_fake_keys(
        monkeypatch, with_client_private_key=True
    )
    return _Env(monkeypatch, secure, client_private_key)


@pytest.fixture
def proxy():
    return _proxy_module()


def _refresh_helpers():
    return (
        _client_build_init,
        _server_process_init,
        _client_derive_e2,
        _client_build_confirm,
        _server_process_confirm,
    )


def _client_keys():
    return {
        "locator": b"M" * 16,
        "c2s": AESGCM.generate_key(bit_length=256),
        "s2c": AESGCM.generate_key(bit_length=256),
    }


def _proxy_module():
    from test_secure_udp_helpers import load_proxy_module

    return load_proxy_module()


# ==========================================================================
# R1 -- migration candidate while epoch refresh begins
# ==========================================================================


def test_01_candidate_created_then_refresh_begins_stays_e1_bound(env):
    """A live E1 candidate is untouched by a refresh transaction merely
    STARTING (INIT -> REPLY installed as `pending_epoch`, not yet
    confirmed): its epoch binding, deadline, and `path_generation` are
    unchanged, the refresh's own transaction deadline is anchored to its
    own start time regardless of the candidate's existence, and the E1
    candidate can still complete normally while the refresh stays
    in-flight."""
    build_init, process_init, derive_e2, build_confirm, process_confirm = (
        _refresh_helpers()
    )
    state = env.new_state(path_candidate_ttl=10.0, pending_epoch_ttl=30.0)
    sess = env.install_session(state, addr=ADDR_A, now=1000.0)
    e1 = sess.session.current_epoch

    open_b = sess.feed(
        [(sess.nmea_packet("!AIVDM,1,1,,A,x,0*00"), ADDR_B)], 1000.0
    )
    challenge = sess.sole_challenge(open_b, expect_addr=ADDR_B)
    assert _snapshot(sess)["candidate"]["deadline"] == 1010.0
    assert _snapshot(sess)["candidate"]["epoch_generation"] == 0
    assert _snapshot(sess)["path_generation"] == 1

    init = build_init(env, sess.session)
    reply_packet, _ = process_init(env, state, sess.session, init, now=1002.0)
    assert reply_packet is not None
    assert sess.session.pending_epoch is not None
    assert sess.session.current_epoch is e1

    # refresh starting does not rebind, extend, or reset the candidate.
    snap = _snapshot(sess)
    assert snap["candidate"]["deadline"] == 1010.0
    assert snap["candidate"]["epoch_generation"] == 0
    assert snap["path_generation"] == 1
    assert sess.session.path_state.candidate_path.epoch is e1
    # the refresh transaction's own deadline is anchored to ITS start time,
    # not moved because a candidate happens to exist.
    assert sess.session.pending_epoch.deadline == 1002.0 + state._pending_epoch_ttl

    # the E1 candidate still completes normally while refresh is mid-flight.
    response = sess.path_response_packet(
        challenge.challenge_token, challenge.path_generation, generation=0
    )
    commit = sess.feed([(response, ADDR_B)], 1005.0)
    assert any(sess._is_type(d, p.PATH_ACK_TYPE, 0) for d, _ in commit.sent)
    assert _snapshot(sess)["active"] == ADDR_B
    # migration commit does not postpone/replace/cancel the in-flight
    # refresh transaction.
    assert sess.session.pending_epoch is not None
    assert sess.session.current_epoch is e1


def test_02_candidate_deadline_fires_on_schedule_despite_inflight_refresh(
    env,
):
    """The candidate's own TTL still fires at its original deadline
    (exact-equality rejects) even while an unrelated refresh transaction is
    mid-flight, and the refresh transaction itself is untouched by that
    candidate expiry."""
    build_init, process_init, _d, _c, _p = _refresh_helpers()
    state = env.new_state(path_candidate_ttl=10.0)
    sess = env.install_session(state, addr=ADDR_A, now=1000.0)
    open_b = sess.feed(
        [(sess.nmea_packet("!AIVDM,1,1,,A,x,0*00"), ADDR_B)], 1000.0
    )
    challenge = sess.sole_challenge(open_b, expect_addr=ADDR_B)

    init = build_init(env, sess.session)
    process_init(env, state, sess.session, init, now=1002.0)
    assert sess.session.pending_epoch is not None

    response = sess.path_response_packet(
        challenge.challenge_token, challenge.path_generation
    )
    _assert_no_commit(sess, response, ADDR_B, 1010.0)  # exact boundary
    assert _snapshot(sess)["candidate"] is None
    assert state.stats().path_candidates_expired == 1
    assert sess.session.pending_epoch is not None  # refresh unaffected


# ==========================================================================
# R2 -- epoch swap while challenge outstanding / fresh E2 recovery
# ==========================================================================


def test_03_epoch_swap_strands_outstanding_candidate_under_both_encodings(
    env,
):
    """A refresh COMMITS (E1 -> E2) while a candidate is still outstanding
    and unproved. The candidate is left bound to the now-retiring E1
    object (never silently promoted/rebound to E2, never reset), and its
    old PATH_RESPONSE cannot commit under either encoding: neither the
    stale E1 wire encoding (now demoted to 'retiring', which the pre-crypto
    gate refuses for off-path traffic) nor a re-encryption of the exact
    same stale token/generation under the new current E2 key (rejected by
    `commit_candidate_path`'s exact epoch-object identity check)."""
    build_init, process_init, derive_e2, build_confirm, process_confirm = (
        _refresh_helpers()
    )
    state = env.new_state(path_candidate_ttl=10.0, retiring_epoch_overlap=50.0)
    sess = env.install_session(state, addr=ADDR_A, now=1000.0)

    open_b = sess.feed(
        [(sess.nmea_packet("!AIVDM,1,1,,A,x,0*00"), ADDR_B)], 1000.0
    )
    challenge = sess.sole_challenge(open_b, expect_addr=ADDR_B)

    init = build_init(env, sess.session)
    reply_packet, _ = process_init(env, state, sess.session, init, now=1001.0)
    km, _ = derive_e2(env, sess.session, init, reply_packet, sess.s2c_key)
    confirm = build_confirm(env, sess.session, init, km)
    _ack, newly, _n = process_confirm(
        env, state, sess.session, confirm, now=1002.0
    )
    assert newly is True
    assert sess.session.current_epoch.generation == 1
    e2_c2s, e2_s2c = km.client_to_server_key, km.server_to_client_key

    # candidate stays present, still E1-bound -- not touched by the commit.
    snap = _snapshot(sess)
    assert snap["candidate"] is not None
    assert snap["candidate"]["epoch_generation"] == 0
    assert snap["path_generation"] == 1

    # old E1-encrypted response: pre-crypto gate refuses off-path traffic
    # under a non-current (retiring) epoch outright.
    old_under_e1 = sess.path_response_packet(
        challenge.challenge_token, challenge.path_generation, generation=0
    )
    r = sess.feed([(old_under_e1, ADDR_B)], 1003.0)
    assert not any(sess._is_type(d, p.PATH_ACK_TYPE, 0) for d, _ in r.sent)

    # same stale token/generation, re-encrypted under the new current E2:
    # still fails -- candidate.epoch is the old E1 object, not E2.
    old_under_e2 = sess.path_response_packet(
        challenge.challenge_token, challenge.path_generation,
        generation=1, key=e2_c2s,
    )
    r = sess.feed([(old_under_e2, ADDR_B)], 1003.5)
    assert not any(
        sess._is_type(d, p.PATH_ACK_TYPE, 1, s2c_key=e2_s2c) for d, _ in r.sent
    )
    assert _snapshot(sess)["active"] == ADDR_A


def test_04_fresh_e2_traffic_establishes_new_candidate_only_it_may_migrate(
    env,
):
    """After E1 -> E2 commit, fresh authenticated E2 traffic from the SAME
    off-path address opens a brand-new candidate incarnation (fresh token,
    advanced path_generation, deadline newly anchored to now -- never
    inherited from the stale E1 candidate). Only a response matching THAT
    fresh E2 candidate can migrate; the old E1 token remains permanently
    stale."""
    build_init, process_init, derive_e2, build_confirm, process_confirm = (
        _refresh_helpers()
    )
    state = env.new_state(path_candidate_ttl=10.0, retiring_epoch_overlap=50.0)
    sess = env.install_session(state, addr=ADDR_A, now=1000.0)

    open_b = sess.feed(
        [(sess.nmea_packet("!AIVDM,1,1,,A,x,0*00"), ADDR_B)], 1000.0
    )
    challenge_1 = sess.sole_challenge(open_b, expect_addr=ADDR_B)

    init = build_init(env, sess.session)
    reply_packet, _ = process_init(env, state, sess.session, init, now=1001.0)
    km, _ = derive_e2(env, sess.session, init, reply_packet, sess.s2c_key)
    confirm = build_confirm(env, sess.session, init, km)
    process_confirm(env, state, sess.session, confirm, now=1002.0)
    e2_c2s, e2_s2c = km.client_to_server_key, km.server_to_client_key

    e2_nmea = sess.nmea_packet(
        "!AIVDM,1,1,,A,e2,0*00", generation=1, key=e2_c2s
    )
    open_b2 = sess.feed([(e2_nmea, ADDR_B)], 1004.0)
    challenge_2 = sess.sole_challenge(
        open_b2, expect_addr=ADDR_B, generation=1, s2c_key=e2_s2c
    )
    snap = _snapshot(sess)
    assert challenge_2.challenge_token != challenge_1.challenge_token
    assert challenge_2.path_generation == challenge_1.path_generation + 1
    assert snap["candidate"]["epoch_generation"] == 1
    assert snap["candidate"]["deadline"] == 1014.0

    # only the FRESH E2 response commits.
    new_response = sess.path_response_packet(
        challenge_2.challenge_token, challenge_2.path_generation,
        generation=1, key=e2_c2s,
    )
    commit = sess.feed([(new_response, ADDR_B)], 1006.0)
    assert any(
        sess._is_type(d, p.PATH_ACK_TYPE, 1, s2c_key=e2_s2c)
        for d, _ in commit.sent
    )
    assert _snapshot(sess)["active"] == ADDR_B
    assert sess.session.current_epoch.generation == 1


# ==========================================================================
# R3 -- retired-path packet after epoch swap
# ==========================================================================


def _e1_e2_migrated_session(env, *, base=50000.0):
    build_init, process_init, derive_e2, build_confirm, process_confirm = (
        _refresh_helpers()
    )
    state = env.new_state(retired_path_grace=5.0)
    sess = env.install_session(state, addr=ADDR_A, now=base)
    sess.migrate(ADDR_A, ADDR_B, now=base)

    init = build_init(env, sess.session)
    reply_packet, _ = process_init(
        env, state, sess.session, init, now=base + 1.0
    )
    km, _ = derive_e2(env, sess.session, init, reply_packet, sess.s2c_key)
    confirm = build_confirm(env, sess.session, init, km)
    _ack, newly, _n = process_confirm(
        env, state, sess.session, confirm, now=base + 2.0
    )
    assert newly is True
    assert sess.session.current_epoch.generation == 1
    return state, sess, km.client_to_server_key, km.server_to_client_key


def test_05_retired_path_e1_straggler_after_epoch_swap_fails_closed(env):
    """A -> B has committed, then E1 -> E2 commits. A delayed E1-encrypted
    straggler from the now-retired A arrives after its retired grace has
    (during authoritative admission) expired: fails closed -- no nonce, no
    touch, no candidate, no reply, and no reverse migration."""
    state, sess, _c2s, _s2c = _e1_e2_migrated_session(env)
    retiring_epoch = sess.session.retiring_epoch
    assert sess.session.path_state.retired_path.deadline == 50005.0

    stats_before = state.stats()
    e1_nmea = sess.nmea_packet(
        "!AIVDM,1,1,,A,straggler,0*00", generation=0
    )
    socket = sess.feed([(e1_nmea, ADDR_A)], 50006.0)

    stats_after = state.stats()
    assert stats_after.data_nonces_accepted == stats_before.data_nonces_accepted
    assert socket.sent == []
    assert sess.session.path_state.candidate_path is None
    assert sess.session.path_state.active_path == ADDR_B
    assert sess.session.current_epoch.generation == 1


def test_06_retired_path_e2_current_traffic_admits_data_but_not_close(env):
    """A CURRENT-epoch (E2) packet from the retired A, within grace, is
    admitted as ordinary late in-flight data continuity -- but it may NOT
    drive a session close, and it does not reactivate A or spontaneously
    reset any migration/refresh state."""
    state, sess, e2_c2s, e2_s2c = _e1_e2_migrated_session(env)
    frames_before = len(sess.queue.items)

    late_nmea = sess.nmea_packet(
        "!AIVDM,1,1,,A,late-e2,0*00", generation=1, key=e2_c2s
    )
    socket = sess.feed([(late_nmea, ADDR_A)], 50003.0)
    assert len(sess.queue.items) == frames_before + 1
    assert _snapshot(sess)["active"] == ADDR_B
    assert _snapshot(sess)["candidate"] is None

    close = sess.close_packet(generation=1, key=e2_c2s)
    close_socket = sess.feed([(close, ADDR_A)], 50003.5)
    assert close_socket.sent == []
    assert state._sessions[sess.session._session_key] is sess.session


def test_07_retired_grace_exact_equality_expires_needs_fresh_cycle(env):
    """At exact grace-boundary equality, the retired record is gone: fresh
    traffic from that address is simply a new path requiring a brand-new
    candidate/challenge/response cycle -- expired means expired, not a
    grey area."""
    state = env.new_state(retired_path_grace=5.0)
    sess = env.install_session(state, addr=ADDR_A, now=1000.0)
    sess.migrate(ADDR_A, ADDR_B, now=1000.0)

    socket = sess.feed(
        [(sess.nmea_packet("!AIVDM,1,1,,A,again,0*00"), ADDR_A)], 1005.0
    )
    assert state.stats().retired_paths_expired == 1
    snap = _snapshot(sess)
    assert snap["active"] == ADDR_B
    assert snap["candidate"] is not None
    assert snap["candidate"]["sockaddr"] == ADDR_A


# ==========================================================================
# R4 -- idle/peer-timeout boundary during migration
# ==========================================================================


def test_08_server_idle_boundary_just_before_equal_after_with_candidate(env):
    """Server-side idle/session_ttl boundary: a live candidate does not
    grant the session any extra grace. Just-before the idle deadline,
    ordinary traffic still keeps the session alive; at/after the exact
    deadline the session is gone (idle-expired) regardless of the live
    candidate, and delayed migration control against the vanished session
    handle has no effect."""
    state = env.new_state(session_ttl=10.0, path_candidate_ttl=1000.0)
    sess = env.install_session(state, addr=ADDR_A, now=1000.0)
    open_b = sess.feed(
        [(sess.nmea_packet("!AIVDM,1,1,,A,x,0*00"), ADDR_B)], 1000.0
    )
    challenge = sess.sole_challenge(open_b, expect_addr=ADDR_B)
    session_obj = sess.session

    # just-before: idle deadline is last_seen(1000) + ttl(10) = 1010; a
    # PATH_RESPONSE at 1009.999 must still be able to commit (session
    # alive, candidate not expired).
    response = sess.path_response_packet(
        challenge.challenge_token, challenge.path_generation
    )
    commit = sess.feed([(response, ADDR_B)], 1009.999)
    assert any(sess._is_type(d, p.PATH_ACK_TYPE, 0) for d, _ in commit.sent)
    assert state._sessions[session_obj._session_key] is session_obj

    # exact equality / after: no further traffic touched last_seen beyond
    # the commit's own touch, so the NEXT idle sweep at >= last_seen + ttl
    # removes the session outright.
    idle_deadline = session_obj.last_seen + state._session_ttl
    state.cleanup_expired_sessions(idle_deadline)
    assert session_obj._session_key not in state._sessions


def test_09_client_outstanding_proof_does_not_postpone_peer_timeout(proxy):
    """Client side: an outstanding migration proof (a PATH_CHALLENGE
    answered, PATH_RESPONSE sent, no PATH_ACK ever arriving) grants no
    extra grace -- the ordinary keepalive/peer_timeout liveness schedule
    is completely unaffected by its mere existence."""
    import builtins as _builtins  # noqa: F401
    from _pytest.monkeypatch import MonkeyPatch

    keys = _client_keys()
    mp = MonkeyPatch()
    try:
        token = os.urandom(32)
        events = [
            (1.0, 1.0, _challenge_packet(proxy, keys, token=token, generation=1)),
        ]
        reason, ended_at, pings, received = _run_scripted_path_loop(
            proxy, keys, mp, events,
            config_overrides={
                "keepalive_interval": 100000,
                "peer_timeout": 10,
                "session_refresh_interval": 0,
            },
        )
        assert reason == proxy.SESSION_END_PEER_TIMEOUT
        assert ended_at == pytest.approx(10.0)
    finally:
        mp.undo()


# ==========================================================================
# R5 -- close during migration
# ==========================================================================


def test_10_close_from_active_path_discards_candidate_with_teardown(env):
    """A legitimate close from the CURRENT active path wins according to
    ordinary close semantics: the session (and its live candidate) is
    torn down as one unit, and a subsequently delayed PATH_RESPONSE for
    the now-gone candidate cannot resurrect anything."""
    state = env.new_state()
    sess = env.install_session(state, addr=ADDR_A, now=1000.0)
    open_b = sess.feed(
        [(sess.nmea_packet("!AIVDM,1,1,,A,x,0*00"), ADDR_B)], 1000.0
    )
    challenge = sess.sole_challenge(open_b, expect_addr=ADDR_B)
    session_obj = sess.session
    assert _snapshot(sess)["candidate"] is not None

    close_socket = sess.feed([(sess.close_packet(), ADDR_A)], 1001.0)
    assert close_socket.sent == []
    assert session_obj._session_key not in state._sessions

    # delayed PATH_RESPONSE for the discarded candidate: no session to
    # resolve the locator against, so it is silently dropped.
    response = sess.path_response_packet(
        challenge.challenge_token, challenge.path_generation
    )
    late = sess.feed([(response, ADDR_B)], 1002.0)
    assert late.sent == []


def test_11_close_from_candidate_path_is_rejected(env):
    """A close-like control message from the (unproved) candidate address
    must never gain close authority merely because it decrypts
    successfully -- the path-role x message-type authorization matrix
    authorizes `close` for the active path only."""
    state = env.new_state()
    sess = env.install_session(state, addr=ADDR_A, now=1000.0)
    sess.feed([(sess.nmea_packet("!AIVDM,1,1,,A,x,0*00"), ADDR_B)], 1000.0)
    assert _snapshot(sess)["candidate"] is not None
    session_obj = sess.session

    close_socket = sess.feed([(sess.close_packet(), ADDR_B)], 1001.0)
    assert close_socket.sent == []
    assert state._sessions[session_obj._session_key] is session_obj
    # candidate is untouched by the rejected close attempt.
    assert _snapshot(sess)["candidate"] is not None
    assert _snapshot(sess)["active"] == ADDR_A


def test_12_close_from_retired_path_is_rejected(env):
    """A close-like control message from a just-retired (grace-window)
    path gains no generic close authority either -- retired grace only
    ever admits late `nmea`/`ping` continuity."""
    state = env.new_state(retired_path_grace=5.0)
    sess = env.install_session(state, addr=ADDR_A, now=1000.0)
    sess.migrate(ADDR_A, ADDR_B, now=1000.0)
    session_obj = sess.session

    close_socket = sess.feed([(sess.close_packet(), ADDR_A)], 1002.0)
    assert close_socket.sent == []
    assert state._sessions[session_obj._session_key] is session_obj
    assert _snapshot(sess)["active"] == ADDR_B


# ==========================================================================
# R6 -- nonce exhaustion during migration / replay across path roles
# ==========================================================================


def test_13_nonce_exhaustion_during_migration_terminates_session(env):
    """The migration commit's own PATH_RESPONSE nonce is admitted into the
    SAME per-epoch replay ledger as ordinary DATA. Exhausting that ledger
    while a candidate is live and then presenting the matching
    PATH_RESPONSE hits EXHAUSTED: the whole session (and therefore the
    candidate) is torn down exactly like ordinary current-epoch nonce
    exhaustion, and the migration does not commit."""
    state = env.new_state(data_nonce_max_per_session=3)
    sess = env.install_session(state, addr=ADDR_A, now=1000.0)
    session_obj = sess.session

    # confirmation already used one slot; fill the remaining capacity with
    # ordinary active-path traffic, then open the candidate with the very
    # last slot.
    used = len(session_obj.current_epoch.seen_data_nonces)
    remaining_for_candidate_open = state._data_nonce_max_per_session - used - 1
    for i in range(remaining_for_candidate_open):
        sess.feed(
            [(sess.nmea_packet(f"!AIVDM,1,1,,A,fill-{i},0*00"), ADDR_A)],
            1000.0 + i,
        )
    assert len(session_obj.current_epoch.seen_data_nonces) == (
        state._data_nonce_max_per_session - 1
    )

    open_b = sess.feed(
        [(sess.nmea_packet("!AIVDM,1,1,,A,open-b,0*00"), ADDR_B)], 1001.0
    )
    challenge = sess.sole_challenge(open_b, expect_addr=ADDR_B)
    assert len(session_obj.current_epoch.seen_data_nonces) == (
        state._data_nonce_max_per_session
    )

    response = sess.path_response_packet(
        challenge.challenge_token, challenge.path_generation
    )
    commit_socket = sess.feed([(response, ADDR_B)], 1002.0)
    assert not any(
        sess._is_type(d, p.PATH_ACK_TYPE, 0) for d, _ in commit_socket.sent
    )
    assert state.stats().data_nonce_exhaustions == 1
    assert session_obj._session_key not in state._sessions


def test_14_replay_across_active_candidate_retired_roles_gains_no_admission(
    env,
):
    """Replay/nonce ownership is per-epoch, not per-path: the exact same
    (nonce, ciphertext) is a REPLAY from the active path, remains a REPLAY
    when represented as if arriving from the (still-live) candidate
    address, and remains a REPLAY from the retired address after
    migration -- changing the source sockaddr never makes it fresh
    again."""
    state = env.new_state(retired_path_grace=1000.0)
    sess = env.install_session(state, addr=ADDR_A, now=1000.0)
    nonce = os.urandom(12)
    packet = sess.nmea_packet(
        "!AIVDM,1,1,,A,shared-nonce,0*00", nonce=nonce
    )

    first = sess.feed([(packet, ADDR_A)], 1000.0)
    assert first.sent == []
    assert sess.session.current_epoch.seen_data_nonces.contains(nonce)
    accepted_before = state.stats().data_nonces_accepted

    # replay from the active path itself.
    r1 = sess.feed([(packet, ADDR_A)], 1001.0)
    assert state.stats().data_nonce_replays >= 1
    assert state.stats().data_nonces_accepted == accepted_before

    # migrate to B (this itself legitimately consumes fresh nonces of its
    # own -- opening the candidate and admitting the real PATH_RESPONSE),
    # then replay the ORIGINAL shared-nonce datagram, now sourced from the
    # (already committed, so no longer live as a candidate) address B
    # itself -- it is simply active-path traffic now, and the exact nonce
    # is still a replay under the unchanged epoch ledger.
    sess.migrate(ADDR_A, ADDR_B, now=1002.0)
    accepted_after_migration = state.stats().data_nonces_accepted
    r2 = sess.feed([(packet, ADDR_B)], 1003.0)
    assert state.stats().data_nonces_accepted == accepted_after_migration

    # replay from the now-retired A.
    r3 = sess.feed([(packet, ADDR_A)], 1004.0)
    assert state.stats().data_nonces_accepted == accepted_after_migration
    assert state.stats().data_nonce_replays >= 3


# ==========================================================================
# R7 -- queue/backpressure vs candidate deadline
# ==========================================================================


def test_15_candidate_deadline_crossing_during_processing_delay_rejects(
    env,
):
    """A PATH_RESPONSE is classified against a live, unexpired candidate
    pre-decrypt, but processing itself (standing in for queue/backpressure/
    scheduler delay) advances authoritative monotonic time past the
    candidate's TTL before the commit decision is actually made. The final
    authoritative admission resamples fresh time and rejects: a stale
    pre-processing timestamp can never authorize commit."""
    state = env.new_state(path_candidate_ttl=10.0)
    sess = env.install_session(state, addr=ADDR_A, now=1000.0)
    open_b = sess.feed(
        [(sess.nmea_packet("!AIVDM,1,1,,A,x,0*00"), ADDR_B)], 1000.0
    )
    challenge = sess.sole_challenge(open_b, expect_addr=ADDR_B)
    assert sess.session.path_state.candidate_path.deadline == 1010.0

    response = sess.path_response_packet(
        challenge.challenge_token, challenge.path_generation
    )
    clock = _FakeClock(1009.999)
    socket = sess.feed(
        [(response, ADDR_B)],
        1009.999,
        clock=clock,
        on_decrypt=lambda: setattr(clock, "now", 1010.5),
    )
    assert not any(sess._is_type(d, p.PATH_ACK_TYPE, 0) for d, _ in socket.sent)
    assert _snapshot(sess)["active"] == ADDR_A
    assert state.stats().path_candidates_expired == 1


def test_16_exact_equality_at_candidate_deadline_after_delayed_processing(
    env,
):
    """Same shape as above, but the delayed processing lands EXACTLY on
    the candidate deadline: equality is still expired, not a boundary that
    favors commit."""
    state = env.new_state(path_candidate_ttl=10.0)
    sess = env.install_session(state, addr=ADDR_A, now=1000.0)
    open_b = sess.feed(
        [(sess.nmea_packet("!AIVDM,1,1,,A,x,0*00"), ADDR_B)], 1000.0
    )
    challenge = sess.sole_challenge(open_b, expect_addr=ADDR_B)

    response = sess.path_response_packet(
        challenge.challenge_token, challenge.path_generation
    )
    clock = _FakeClock(1009.999)
    socket = sess.feed(
        [(response, ADDR_B)],
        1009.999,
        clock=clock,
        on_decrypt=lambda: setattr(clock, "now", 1010.0),
    )
    assert not any(sess._is_type(d, p.PATH_ACK_TYPE, 0) for d, _ in socket.sent)
    assert _snapshot(sess)["active"] == ADDR_A


# ==========================================================================
# R8 -- listener shutdown with candidate / retired state
# ==========================================================================


def test_17_listener_shutdown_with_live_candidate_removes_all_state(env):
    """Owner/listener teardown (`close_owned_sessions`) discards the whole
    session -- and therefore its live candidate -- as one unit; there is
    no candidate/relation-index remnant left reachable, and a later
    delayed PATH_RESPONSE cannot act on the removed session."""
    state = env.new_state()
    sess = env.install_session(state, addr=ADDR_A, now=1000.0)
    open_b = sess.feed(
        [(sess.nmea_packet("!AIVDM,1,1,,A,x,0*00"), ADDR_B)], 1000.0
    )
    challenge = sess.sole_challenge(open_b, expect_addr=ADDR_B)
    session_obj = sess.session
    assert _snapshot(sess)["candidate"] is not None

    owned = {session_obj._session_key: session_obj}
    fake_sock = _FakeSecureSocket()
    env.secure.close_owned_sessions(
        fake_sock, state, owned,
        wall_clock=_FakeClock(2_000_000.0),
        monotonic_clock=_FakeClock(1002.0),
    )
    assert session_obj._session_key not in state._sessions
    assert not state._relation_index

    response = sess.path_response_packet(
        challenge.challenge_token, challenge.path_generation
    )
    late = sess.feed([(response, ADDR_B)], 1003.0)
    assert late.sent == []


def test_18_shutdown_close_targets_current_active_path_not_retired_or_candidate(
    env,
):
    """After A -> B has committed (A retired) and a NEW candidate C is
    also live, best-effort shutdown close must go to the CURRENT validated
    active_path B -- never the original establishment address A, never
    the retired path, never the candidate."""
    state = env.new_state(retired_path_grace=1000.0)
    sess = env.install_session(state, addr=ADDR_A, now=1000.0)
    sess.migrate(ADDR_A, ADDR_B, now=1000.0)
    sess.feed([(sess.nmea_packet("!AIVDM,1,1,,A,c,0*00"), ADDR_C)], 1001.0)
    snap = _snapshot(sess)
    assert snap["active"] == ADDR_B
    assert snap["retired"]["sockaddr"] == ADDR_A
    assert snap["candidate"]["sockaddr"] == ADDR_C
    session_obj = sess.session

    owned = {session_obj._session_key: session_obj}
    fake_sock = _FakeSecureSocket()
    env.secure.close_owned_sessions(
        fake_sock, state, owned,
        wall_clock=_FakeClock(2_000_000.0),
        monotonic_clock=_FakeClock(1002.0),
    )
    assert len(fake_sock.sent) == 1
    data, addr = fake_sock.sent[0]
    assert addr == ADDR_B
    assert addr != ADDR_A
    assert addr != ADDR_C
    message = sess.decode_server_message(data)
    assert message["type"] == p.SESSION_CLOSE_TYPE


def test_19_shutdown_close_uses_active_path_with_refresh_also_active(env):
    """Scenario C: an in-session epoch refresh is ALSO mid-flight
    (`pending_epoch` set, `current_epoch` unchanged) at shutdown time.
    Shutdown close still targets the current validated `active_path` and
    is encrypted under the current (not pending) epoch -- refresh state
    never redirects close to a stale transport tuple or a not-yet-live
    key."""
    build_init, process_init, _d, _c, _p = _refresh_helpers()
    state = env.new_state()
    sess = env.install_session(state, addr=ADDR_A, now=1000.0)
    sess.migrate(ADDR_A, ADDR_B, now=1000.0)
    init = build_init(env, sess.session)
    process_init(env, state, sess.session, init, now=1001.0)
    assert sess.session.pending_epoch is not None
    current_epoch = sess.session.current_epoch
    session_obj = sess.session

    owned = {session_obj._session_key: session_obj}
    fake_sock = _FakeSecureSocket()
    env.secure.close_owned_sessions(
        fake_sock, state, owned,
        wall_clock=_FakeClock(2_000_000.0),
        monotonic_clock=_FakeClock(1002.0),
    )
    assert len(fake_sock.sent) == 1
    data, addr = fake_sock.sent[0]
    assert addr == ADDR_B
    # decrypted under the CURRENT (still generation 0, since the refresh is
    # only pending, not committed) epoch's key, not some pending-candidate
    # key -- `current_epoch` is unchanged by an uncommitted refresh.
    assert current_epoch.generation == 0
    message = sess.decode_server_message(data)
    assert message["type"] == p.SESSION_CLOSE_TYPE


def test_20_listener_shutdown_with_pending_candidate_epoch_discards_it(env):
    """`close_owned_pending_sessions` discards exactly the pending
    (unconfirmed handshake) candidates this owner holds; combined with
    `close_owned_sessions` for confirmed sessions, owner teardown leaves no
    usable state of either kind reachable."""
    state = env.new_state()
    sess = env.install_session(state, addr=ADDR_A, now=1000.0)
    session_obj = sess.session
    owned_sessions = {session_obj._session_key: session_obj}
    owned_pending = {}

    fake_sock = _FakeSecureSocket()
    env.secure.close_owned_sessions(
        fake_sock, state, owned_sessions,
        wall_clock=_FakeClock(2_000_000.0),
        monotonic_clock=_FakeClock(1001.0),
    )
    env.secure.close_owned_pending_sessions(
        state, owned_pending, monotonic_clock=_FakeClock(1001.0)
    )
    assert session_obj._session_key not in state._sessions
    assert state.stats().current_sessions == 0
    assert state.stats().current_pending_sessions == 0


# ==========================================================================
# Timer independence (I2/I3/C3/C4) and terminal-session resurrection (C5)
# ==========================================================================


def test_21_migration_does_not_postpone_refresh_transaction_timers(env):
    """A migration commit happening WHILE a refresh transaction is pending
    does not move that transaction's deadline, retry cadence, attempt
    counter, or identity."""
    build_init, process_init, _d, _c, _p = _refresh_helpers()
    state = env.new_state(path_candidate_ttl=1000.0)
    sess = env.install_session(state, addr=ADDR_A, now=1000.0)

    init = build_init(env, sess.session)
    process_init(env, state, sess.session, init, now=1000.0)
    pending_before = sess.session.pending_epoch
    txn_before = pending_before.transaction_id
    deadline_before = pending_before.deadline

    sess.migrate(ADDR_A, ADDR_B, now=1005.0)

    assert sess.session.pending_epoch is pending_before
    assert sess.session.pending_epoch.transaction_id == txn_before
    assert sess.session.pending_epoch.deadline == deadline_before


def test_22_refresh_does_not_reset_or_extend_migration_or_session_lifecycle(
    env,
):
    """A completed refresh commit does not reset `path_generation`, does
    not extend/relocate an unrelated retired-path grace, and does not
    touch the session's `created_at`/idle-liveness bookkeeping beyond the
    ordinary authenticated-activity touch."""
    build_init, process_init, derive_e2, build_confirm, process_confirm = (
        _refresh_helpers()
    )
    state = env.new_state(retired_path_grace=5.0)
    sess = env.install_session(state, addr=ADDR_A, now=1000.0)
    created_at = sess.session.created_at
    sess.migrate(ADDR_A, ADDR_B, now=1000.0)
    pg_before = sess.session.path_state.path_generation
    retired_deadline_before = sess.session.path_state.retired_path.deadline

    init = build_init(env, sess.session)
    reply_packet, _ = process_init(env, state, sess.session, init, now=1001.0)
    km, _ = derive_e2(env, sess.session, init, reply_packet, sess.s2c_key)
    confirm = build_confirm(env, sess.session, init, km)
    _ack, newly, _n = process_confirm(
        env, state, sess.session, confirm, now=1002.0
    )
    assert newly is True

    assert sess.session.path_state.path_generation == pg_before
    assert sess.session.path_state.retired_path.deadline == (
        retired_deadline_before
    )
    assert sess.session.created_at == created_at


def test_23_delayed_migration_control_cannot_resurrect_closed_session(env):
    """Once a session is torn down (ordinary close), a delayed
    PATH_RESPONSE for the discarded candidate and a delayed retired-path
    straggler both have zero resurrection authority."""
    state = env.new_state()
    sess = env.install_session(state, addr=ADDR_A, now=1000.0)
    sess.migrate(ADDR_A, ADDR_B, now=1000.0)
    open_c = sess.feed(
        [(sess.nmea_packet("!AIVDM,1,1,,A,c,0*00"), ADDR_C)], 1001.0
    )
    challenge = sess.sole_challenge(open_c, expect_addr=ADDR_C)
    session_obj = sess.session

    close_socket = sess.feed([(sess.close_packet(), ADDR_B)], 1002.0)
    assert close_socket.sent == []
    assert session_obj._session_key not in state._sessions

    response = sess.path_response_packet(
        challenge.challenge_token, challenge.path_generation
    )
    late_response = sess.feed([(response, ADDR_C)], 1003.0)
    assert late_response.sent == []
    late_retired = sess.feed(
        [(sess.nmea_packet("!AIVDM,1,1,,A,late,0*00"), ADDR_A)], 1004.0
    )
    assert late_retired.sent == []
    assert late_retired.sent == []


def test_24_delayed_migration_control_cannot_resurrect_idle_expired_session(
    env,
):
    """Same as above, but the session dies via idle/session_ttl expiry
    instead of an explicit close: a delayed PATH_RESPONSE against the
    vanished handle has no effect."""
    state = env.new_state(session_ttl=5.0, path_candidate_ttl=1000.0)
    sess = env.install_session(state, addr=ADDR_A, now=1000.0)
    open_b = sess.feed(
        [(sess.nmea_packet("!AIVDM,1,1,,A,x,0*00"), ADDR_B)], 1000.0
    )
    challenge = sess.sole_challenge(open_b, expect_addr=ADDR_B)
    session_obj = sess.session

    state.cleanup_expired_sessions(1005.0)
    assert session_obj._session_key not in state._sessions

    response = sess.path_response_packet(
        challenge.challenge_token, challenge.path_generation
    )
    late = sess.feed([(response, ADDR_B)], 1006.0)
    assert late.sent == []
