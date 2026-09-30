"""UDPSEC V2 MP2 -- bounded PATH_CHALLENGE retransmission (server side).

One live candidate incarnation may resend its SAME PATH_CHALLENGE -- same
token, path_generation and candidate address, freshly encrypted, never past
its fixed deadline -- at most `PATH_CHALLENGE_MAX_SENDS` times in total,
each attempt at least `PATH_CHALLENGE_RETRY_INTERVAL_SECONDS` after the
previous one. Retry is delivery, never authority.

These tests use the Prompt 4 `_Env`/`_Session` harness (real AES-GCM, the
real V2 DATA framing and the real `_secure_server_loop`). They drive the real
retry pass, `_send_due_path_challenge_retries`, at explicit monotonic
instants -- the job the listener's retry task (`_run_path_challenge_retries`,
spawned by `secure_server`) does in production. The test names
`test_mp2_b<N>_` follow the MP2 scenarios MP2-B1..MP2-B11.
"""

import asyncio
import errno
import math

import pytest

import core.udpsec_protocol as p

from test_secure_udp_helpers import (
    _FakeClock,
    _FakeSecureSocket,
    _FakeSecureSocketFactory,
    _FakeQueue,
    load_proxy_module,
    load_secure_module_with_fake_keys,
)
from test_udpsec_path_migration import (
    ADDR_A,
    ADDR_B,
    ADDR_C,
    STATION_ID,
    _Env,
    _identity_fields,
    _refresh_helpers,
)

T0 = 1000.0
WALL = 1_000_000.0


@pytest.fixture
def env(monkeypatch):
    secure, client_private_key = load_secure_module_with_fake_keys(
        monkeypatch, with_client_private_key=True
    )
    return _Env(monkeypatch, secure, client_private_key)


def _nmea(sess, tag, **kwargs):
    return sess.nmea_packet(f"!AIVDM,1,1,,A,{tag},0*00", **kwargs)


def _open(sess, *, addr=ADDR_B, now=T0, tag="open"):
    sock = sess.feed([(_nmea(sess, tag), addr)], now)
    return sock, sess.sole_challenge(sock, expect_addr=addr)


def _retry_pass(sess, now, *, sock=None):
    sock = _FakeSecureSocket() if sock is None else sock
    sess.env.secure._send_due_path_challenge_retries(
        sess.state, sess.endpoint_token, sock, _FakeClock(WALL), _FakeClock(now)
    )
    return sock


def _challenges(sess, sock, *, generation=0, s2c_key=None):
    """Every PATH_CHALLENGE in `sock.sent` as (addr, token, path_generation,
    nonce, raw datagram)."""
    out = []
    for data, addr in sock.sent:
        if not sess._is_type(data, p.PATH_CHALLENGE_TYPE, generation, s2c_key=s2c_key):
            continue
        challenge = p.parse_path_challenge_message(
            sess.decode_server_message(data, generation=generation, s2c_key=s2c_key)
        )
        nonce = p.parse_data_packet(data)[2]
        out.append((addr, challenge.challenge_token, challenge.path_generation, nonce, data))
    return out


def _candidate(sess):
    return sess.session.path_state.candidate_path


def _commit(sess, token, generation, *, addr=ADDR_B, now):
    sock = sess.feed([(sess.path_response_packet(token, generation), addr)], now)
    return [a for d, a in sock.sent if sess._is_type(d, p.PATH_ACK_TYPE, 0)]


def test_mp2_policy_constants(env):
    secure = env.secure
    assert secure.PATH_CHALLENGE_RETRY_INTERVAL_SECONDS == 2.0
    assert secure.PATH_CHALLENGE_MAX_SENDS == 4
    assert secure.PATH_CANDIDATE_TTL_SECONDS == 10.0
    # Every attempt of the default schedule fits strictly inside the TTL.
    last = (secure.PATH_CHALLENGE_MAX_SENDS - 1) * secure.PATH_CHALLENGE_RETRY_INTERVAL_SECONDS
    assert last < secure.PATH_CANDIDATE_TTL_SECONDS


# ------------------------------------------------------------ MP2 scenarios


def test_mp2_b1_normal_migration_commits_before_any_retry(env):
    state = env.new_state()
    sess = env.install_session(state, addr=ADDR_A, now=T0)
    _sock, challenge = _open(sess)
    assert _commit(sess, challenge.challenge_token, challenge.path_generation, now=T0 + 0.1) == [ADDR_B]
    for now in (T0 + 2, T0 + 4, T0 + 6):
        assert _retry_pass(sess, now).sent == []
    assert state.next_path_challenge_retry_at(sess.endpoint_token) is None
    stats = state.stats()
    assert (stats.migration_challenges_sent, stats.migration_challenge_retries_sent) == (1, 0)


def test_mp2_b2_lost_first_challenge_is_retried_in_the_same_incarnation(env):
    state = env.new_state()
    sess = env.install_session(state, addr=ADDR_A, now=T0)
    before = _identity_fields(sess.session)
    ledger = sess.session.current_epoch.seen_data_nonces
    _sock, first = _open(sess)  # this challenge is "lost"
    assert _retry_pass(sess, T0 + 1.999).sent == []  # not due yet
    ((addr, token, generation, _nonce, _raw),) = _challenges(sess, _retry_pass(sess, T0 + 2))
    assert (addr, token, generation) == (ADDR_B, first.challenge_token, first.path_generation)
    assert _commit(sess, token, generation, now=T0 + 2.1) == [ADDR_B]
    snap = state.path_state_snapshot(sess.session, T0 + 2.1)
    assert snap["active"] == ADDR_B
    assert snap["path_generation"] == 1  # no second incarnation was needed
    assert _identity_fields(sess.session) == before
    assert sess.session.current_epoch.seen_data_nonces is ledger
    stats = state.stats()
    assert (stats.path_candidates_opened, stats.path_candidates_expired) == (1, 0)
    assert stats.path_migrations_committed == 1


def test_mp2_b3_lost_response_is_answered_again_by_the_unchanged_client(env):
    """The real `nmea_sproxy` client answers the server's retry (a duplicate
    of the challenge it already answered) with a second PATH_RESPONSE; that
    one commits, and the client's original proof accepts the PATH_ACK."""
    proxy = load_proxy_module()
    state = env.new_state()
    sess = env.install_session(state, addr=ADDR_A, now=T0)
    keys = {"locator": sess.locator, "c2s": sess.c2s_key, "s2c": sess.s2c_key}
    epochs = proxy._ClientEpochSet(
        keys["locator"],
        proxy.SessionKeyMaterial(
            client_to_server_key=keys["c2s"], server_to_client_key=keys["s2c"]
        ),
    )
    migration = proxy._ClientPathMigration()
    remote = ("192.0.2.1", 17777)

    class RawSock:
        def __init__(self):
            self.sent = []

        def sendto(self, data, addr):
            self.sent.append(data)

    client_sock = RawSock()

    def client_receives(datagram, clock_now):
        return proxy._try_handle_path_message(
            datagram, remote, remote, epochs, migration, STATION_ID,
            client_sock, lambda: clock_now, None,
        )

    open_sock, _challenge = _open(sess)
    ((_a, _t, _g, _n, first_datagram),) = _challenges(sess, open_sock)
    client_receives(first_datagram, T0 + 0.05)
    lost_response = client_sock.sent[-1]  # never delivered
    ((_a, _t, _g, _n, retry_datagram),) = _challenges(sess, _retry_pass(sess, T0 + 2))
    client_receives(retry_datagram, T0 + 2.05)
    duplicate_response = client_sock.sent[-1]
    assert duplicate_response != lost_response  # a fresh encryption
    commit = sess.feed([(duplicate_response, ADDR_B)], T0 + 2.1)
    (ack,) = [d for d, a in commit.sent if sess._is_type(d, p.PATH_ACK_TYPE, 0)]
    result, _captured = client_receives(ack, T0 + 2.15)
    assert result == proxy.SERVER_PACKET_PATH_MIGRATED
    assert state.path_state_snapshot(sess.session, T0 + 2.2)["path_generation"] == 1


def test_mp2_b4_all_attempts_lost_exhaust_the_budget_then_expire_on_time(env):
    state = env.new_state()
    sess = env.install_session(state, addr=ADDR_A, now=T0)
    _sock, first = _open(sess)
    retries = []
    for now in (T0 + 2, T0 + 4, T0 + 6, T0 + 8, T0 + 9.9):
        retries += [(now, c) for c in _challenges(sess, _retry_pass(sess, now))]
    assert [now for now, _c in retries] == [T0 + 2, T0 + 4, T0 + 6]
    assert {(c[0], c[1], c[2]) for _n, c in retries} == {
        (ADDR_B, first.challenge_token, first.path_generation)
    }
    assert _candidate(sess).challenge_sends == env.secure.PATH_CHALLENGE_MAX_SENDS
    assert state.next_path_challenge_retry_at(sess.endpoint_token) is None
    assert state.path_state_snapshot(sess.session, T0 + 9.9)["candidate"]["deadline"] == T0 + 10
    # At its unchanged deadline the incarnation expires; later authenticated
    # off-path traffic opens a NEW incarnation with a fresh budget.
    assert state.path_state_snapshot(sess.session, T0 + 10)["candidate"] is None
    _sock, second = _open(sess, now=T0 + 10.5, tag="later")
    assert second.path_generation == first.path_generation + 1
    assert second.challenge_token != first.challenge_token
    assert _candidate(sess).challenge_sends == 1
    stats = state.stats()
    assert stats.path_candidates_expired == 1
    assert stats.migration_challenges_sent == 5  # 4 for the first incarnation + 1


def test_mp2_b4_a_delayed_response_after_exhaustion_still_commits_before_expiry(env):
    state = env.new_state()
    sess = env.install_session(state, addr=ADDR_A, now=T0)
    _sock, first = _open(sess)
    for now in (T0 + 2, T0 + 4, T0 + 6):
        _retry_pass(sess, now)
    assert _commit(sess, first.challenge_token, first.path_generation, now=T0 + 9.9) == [ADDR_B]


def test_mp2_b5_commit_just_before_the_retry_is_due_cancels_it(env):
    state = env.new_state()
    sess = env.install_session(state, addr=ADDR_A, now=T0)
    _sock, challenge = _open(sess)
    assert _commit(sess, challenge.challenge_token, challenge.path_generation, now=T0 + 1.999)
    assert state.next_path_challenge_retry_at(sess.endpoint_token) is None
    for now in (T0 + 2, T0 + 4):
        assert _retry_pass(sess, now).sent == []
    assert state._path_challenge_retry_keys == set()


def test_mp2_b6_no_retry_is_scheduled_or_sent_at_the_deadline(env):
    # TTL 4 s: the retry at +2 fits; one at +4 would equal the deadline, so
    # it is never scheduled.
    state = env.new_state(path_candidate_ttl=4.0)
    sess = env.install_session(state, addr=ADDR_A, now=T0)
    _open(sess)
    assert len(_challenges(sess, _retry_pass(sess, T0 + 2))) == 1
    assert state.next_path_challenge_retry_at(sess.endpoint_token) is None
    # A retry that is due but whose pass only runs AT the deadline: expiry
    # wins, nothing is sent.
    late = env.new_state(path_candidate_ttl=4.0)
    other = env.install_session(late, addr=ADDR_A, now=T0)
    _open(other)
    assert _retry_pass(other, T0 + 4.0).sent == []
    assert late.stats().path_candidates_expired == 1
    assert late._path_challenge_retry_keys == set()
    # Strictly before the deadline the same late pass still sends once.
    last = env.new_state(path_candidate_ttl=4.0)
    third = env.install_session(last, addr=ADDR_A, now=T0)
    _open(third)
    assert len(_challenges(third, _retry_pass(third, T0 + 3.999))) == 1
    assert last.next_path_challenge_retry_at(third.endpoint_token) is None


def test_mp2_b7_a_superseded_candidate_is_never_retried(env):
    state = env.new_state()
    sess = env.install_session(state, addr=ADDR_A, now=T0)
    _sock, b = _open(sess, addr=ADDR_B, now=T0)
    _sock, c = _open(sess, addr=ADDR_C, now=T0 + 1, tag="c")
    assert c.path_generation == b.path_generation + 1
    assert _retry_pass(sess, T0 + 2).sent == []  # B's old due time: nothing
    ((addr, token, generation, _n, _raw),) = _challenges(sess, _retry_pass(sess, T0 + 3))
    assert (addr, token, generation) == (ADDR_C, c.challenge_token, c.path_generation)
    assert state.stats().path_candidates_replaced == 1


def test_mp2_b7_a_replacement_between_claim_and_send_is_never_sent(env, monkeypatch):
    """The second guard: B's retry is admitted, but B is replaced by C while
    the retry is being encrypted (outside the lock). The revalidation just
    before `sendto` refuses it -- nothing reaches B -- and B's consumed
    attempt schedules nothing further."""
    state = env.new_state()
    sess = env.install_session(state, addr=ADDR_A, now=T0)
    _open(sess)
    stale = _candidate(sess)
    real_build = env.secure._build_path_challenge_packet

    def build_then_replace(station_id, candidate, locator, wall_now):
        packet = real_build(station_id, candidate, locator, wall_now)
        state.open_or_replace_candidate_path(
            sess.session, sess.session.current_epoch, ADDR_C, b"\x09" * 32, T0 + 2
        )
        return packet

    monkeypatch.setattr(env.secure, "_build_path_challenge_packet", build_then_replace)
    sock = _retry_pass(sess, T0 + 2)
    assert sock.sent == []
    assert stale.challenge_sends == 2 and stale.next_challenge_at is None
    assert _candidate(sess) is not stale
    assert state.stats().migration_challenge_retries_sent == 0


def test_mp2_b8_duplicate_candidate_traffic_changes_no_budget_token_or_deadline(env):
    state = env.new_state()
    sess = env.install_session(state, addr=ADDR_A, now=T0)
    _sock, first = _open(sess)
    candidate = _candidate(sess)
    for now in (T0 + 0.5, T0 + 1.0, T0 + 1.5):
        assert _challenges(sess, sess.feed([(_nmea(sess, f"dup{now}"), ADDR_B)], now)) == []
    assert _candidate(sess) is candidate
    assert (candidate.challenge_sends, candidate.next_challenge_at) == (1, T0 + 2)
    assert (candidate.challenge_token, candidate.path_generation) == (
        first.challenge_token, first.path_generation,
    )
    assert candidate.deadline == T0 + 10
    ((_a, token, _g, _n, _raw),) = _challenges(sess, _retry_pass(sess, T0 + 2))
    assert token == first.challenge_token


def test_mp2_b9_a_failed_retry_send_consumes_its_attempt_without_busy_retry(
    env, monkeypatch
):
    state = env.new_state()
    sess = env.install_session(state, addr=ADDR_A, now=T0)
    _open(sess)
    events = []
    monkeypatch.setattr(state.migration_diagnostics, "publish", events.append)

    class FailingSocket:
        def __init__(self):
            self.attempts = 0

        def sendto(self, data, addr):
            self.attempts += 1
            raise OSError(errno.ENETUNREACH, "Network is unreachable")

    failing = FailingSocket()
    _retry_pass(sess, T0 + 2, sock=failing)  # contained, not raised
    candidate = _candidate(sess)
    assert failing.attempts == 1
    assert candidate.challenge_sends == 2
    assert candidate.next_challenge_at == T0 + 4  # one interval later
    assert candidate.deadline == T0 + 10
    _retry_pass(sess, T0 + 2.5, sock=failing)
    assert failing.attempts == 1  # no immediate retry
    stats = state.stats()
    assert (stats.migration_challenges_sent, stats.migration_challenge_retries_sent) == (1, 0)
    (event,) = [e for e in events if e[0] == "challenge_retried"]
    line = env.secure._format_migration_diagnostic(event)
    assert "RETRY 2/4 send FAILED" in line and "generation=1" in line
    assert len(_challenges(sess, _retry_pass(sess, T0 + 4))) == 1


def test_mp2_b10_session_removed_while_a_retry_is_pending_sends_nothing(env):
    state = env.new_state()
    sess = env.install_session(state, addr=ADDR_A, now=T0)
    _open(sess)
    assert state.next_path_challenge_retry_at(sess.endpoint_token) == T0 + 2
    assert state.close_session(sess.session, T0 + 1)
    assert state._path_challenge_retry_keys == set()
    assert state.next_path_challenge_retry_at(sess.endpoint_token) is None
    assert _retry_pass(sess, T0 + 2).sent == []


def test_mp2_b11_every_attempt_is_a_fresh_encryption(env):
    state = env.new_state()
    sess = env.install_session(state, addr=ADDR_A, now=T0)
    open_sock, _first = _open(sess)
    attempts = _challenges(sess, open_sock)
    for now in (T0 + 2, T0 + 4, T0 + 6):
        attempts += _challenges(sess, _retry_pass(sess, now))
    assert len(attempts) == env.secure.PATH_CHALLENGE_MAX_SENDS
    assert len({a[3] for a in attempts}) == len(attempts)  # nonces
    assert len({a[4] for a in attempts}) == len(attempts)  # datagrams
    assert len({(a[0], a[1], a[2]) for a in attempts}) == 1  # same challenge


# ------------------------------------------------------------ amplification


def test_unknown_locator_forged_or_replayed_traffic_schedules_no_retry(env):
    state = env.new_state()
    sess = env.install_session(state, addr=ADDR_A, now=T0)
    stranger = env.install_session(env.new_state(), addr=ADDR_A, now=T0)
    forged_key = b"\x07" * 32
    sess.feed(
        [
            (_nmea(stranger, "unknown-locator"), ADDR_B),
            (_nmea(sess, "forged", key=forged_key), ADDR_B),
        ],
        T0,
    )
    assert _candidate(sess) is None
    assert state.next_path_challenge_retry_at(sess.endpoint_token) is None
    # A genuine candidate, then a byte replay of the packet that opened it:
    # the replay is refused and cannot reset or extend anything.
    opener = _nmea(sess, "opener")
    sess.feed([(opener, ADDR_B)], T0)
    _retry_pass(sess, T0 + 2)
    replays = state.stats().data_nonce_replays
    sess.feed([(opener, ADDR_B)], T0 + 3)
    assert state.stats().data_nonce_replays == replays + 1
    candidate = _candidate(sess)
    assert (candidate.challenge_sends, candidate.next_challenge_at) == (2, T0 + 4)


def test_retry_passes_create_no_candidates_and_state_stays_bounded(env):
    state = env.new_state()
    sessions = [
        env.install_session(state, addr=("192.0.2.10", 41000 + i), now=T0)
        for i in range(5)
    ]
    for i, sess in enumerate(sessions):
        _open(sess, addr=("198.51.100.20", 42000 + i), now=T0)
    assert len(state._path_challenge_retry_keys) == 5
    opened = state.stats().path_candidates_opened
    for now in (T0 + 2, T0 + 4, T0 + 6, T0 + 8):
        for sess in sessions:
            _retry_pass(sess, now)
    stats = state.stats()
    assert stats.path_candidates_opened == opened
    assert stats.migration_challenge_retries_sent == 5 * 3
    assert state._path_challenge_retry_keys == set()  # all budgets spent
    assert all(_candidate(s).challenge_sends == 4 for s in sessions)


def test_a_retry_pass_serves_only_its_own_listener(env):
    state = env.new_state()
    here = env.install_session(state, addr=ADDR_A, now=T0)
    there = env.install_session(state, addr=ADDR_C, now=T0)
    assert here.endpoint_token != there.endpoint_token
    _open(here, addr=ADDR_B)
    _open(there, addr=("198.51.100.99", 42999))
    ((addr, *_rest),) = _challenges(here, _retry_pass(here, T0 + 2))
    assert addr == ADDR_B
    assert _candidate(there).challenge_sends == 1  # untouched by that pass


def test_retry_never_crosses_an_epoch_refresh(env):
    build_init, process_init, derive_e2, build_confirm, process_confirm = (
        _refresh_helpers()
    )
    state = env.new_state(retiring_epoch_overlap=50.0)
    sess = env.install_session(state, addr=ADDR_A, now=T0)
    _sock, first = _open(sess)
    init = build_init(env, sess.session)
    reply_packet, _ = process_init(env, state, sess.session, init, now=T0 + 0.5)
    km, _ = derive_e2(env, sess.session, init, reply_packet, sess.s2c_key)
    confirm = build_confirm(env, sess.session, init, km)
    _ack, newly, _n = process_confirm(env, state, sess.session, confirm, now=T0 + 1)
    assert newly is True
    # The candidate is bound to the retired epoch: its retry is refused.
    assert _retry_pass(sess, T0 + 2).sent == []
    assert state.next_path_challenge_retry_at(sess.endpoint_token) is None
    # Fresh current-epoch traffic opens a NEW incarnation under E2.
    e2 = sess.feed(
        [(_nmea(sess, "e2", generation=1, key=km.client_to_server_key), ADDR_B)],
        T0 + 3,
    )
    ((_a, token, generation, _n, _raw),) = _challenges(
        sess, e2, generation=1, s2c_key=km.server_to_client_key
    )
    assert generation == first.path_generation + 1
    assert token != first.challenge_token


# ------------------------------------------------------------ retry driver


def test_retry_driver_waits_exactly_until_the_next_due_retry(env):
    state = env.new_state()
    sess = env.install_session(state, addr=ADDR_A, now=T0)
    clock = _FakeClock(T0)
    sock = _FakeSecureSocket()
    timeouts = []

    async def scripted_wait(wakeup, timeout):
        timeouts.append(timeout)
        if len(timeouts) == 1:
            assert timeout is None  # nothing scheduled: no polling
            # The listener's candidate install, through the real (sync)
            # helper; it wakes the driver once the first retry is scheduled.
            env.secure._process_candidate_path_observation(
                state, sess.session, sess.session.current_epoch, ADDR_B,
                sess.locator, sock, _FakeClock(WALL), clock, clock.now,
                retry_wakeup=wakeup.set,
            )
            assert wakeup.is_set()
            return
        if timeout is None:
            raise asyncio.CancelledError()  # nothing left to wait for
        clock.now += timeout

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            env.secure._run_path_challenge_retries(
                sock, sess.endpoint_token, state, asyncio.Event(),
                wall_clock=_FakeClock(WALL), monotonic_clock=clock,
                wait=scripted_wait,
            )
        )
    # Woken by the new candidate, then exact waits to +2, +4, +6; once the
    # budget is spent nothing is scheduled any more.
    assert timeouts == [None, 2.0, 2.0, 2.0, None]
    assert [c[2] for c in _challenges(sess, sock)] == [1, 1, 1, 1]


def test_retry_driver_stops_quietly_on_an_unexpected_error(env, capsys):
    class BrokenState:
        def claim_due_path_challenge_retries(self, *_args):
            raise RuntimeError("boom")

    asyncio.run(
        env.secure._run_path_challenge_retries(
            _FakeSecureSocket(), object(), BrokenState(), asyncio.Event()
        )
    )
    assert "PATH_CHALLENGE retry driver stopped: RuntimeError: boom" in capsys.readouterr().out


def test_secure_server_owns_and_cancels_its_retry_driver(env, monkeypatch):
    secure = env.secure
    fake_socket = _FakeSecureSocket()
    monkeypatch.setattr(
        secure, "create_udp_listener_socket", _FakeSecureSocketFactory(fake_socket)
    )
    seen = {}

    async def fake_driver(sock, token, state, wakeup, **_kwargs):
        seen["driver"] = (sock, token, wakeup)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            seen["cancelled"] = True
            raise

    async def fake_loop(*_args, **kwargs):
        seen["loop_kwargs"] = kwargs
        await asyncio.sleep(0)  # let the driver start

    monkeypatch.setattr(secure, "_run_path_challenge_retries", fake_driver)
    monkeypatch.setattr(secure, "_secure_server_loop", fake_loop)

    async def main():
        await secure.secure_server(
            _FakeQueue(), "127.0.0.1", 9999,
            state=env.new_state(), server_private_key=env.server_private_key,
        )
        await asyncio.sleep(0)  # deliver the cancellation

    asyncio.run(main())
    sock, token, wakeup = seen["driver"]
    assert sock is fake_socket
    assert token is seen["loop_kwargs"]["endpoint_token"]
    seen["loop_kwargs"]["path_challenge_retry_wakeup"]()
    assert wakeup.is_set()
    assert seen.get("cancelled") is True
    assert fake_socket.close_count == 1


def test_retry_diagnostic_line_carries_no_challenge_token(env):
    line = env.secure._format_migration_diagnostic(
        ("challenge_retried", STATION_ID, b"\xab" * 16, ADDR_B, 3, 2, 4, True)
    )
    assert line == (
        f"[+] Path challenge RETRY 2/4 for {STATION_ID} session=abababab "
        "candidate=198.51.100.20:42000 generation=3"
    )


def test_retry_count_and_spacing_bounds_per_incarnation(env):
    state = env.new_state()
    sess = env.install_session(state, addr=ADDR_A, now=T0)
    open_sock, _first = _open(sess)
    sent_at = [T0] * len(_challenges(sess, open_sock))
    now = T0
    while now < T0 + 10:
        now = round(now + 0.25, 2)
        sent_at += [now] * len(_challenges(sess, _retry_pass(sess, now)))
    interval = env.secure.PATH_CHALLENGE_RETRY_INTERVAL_SECONDS
    assert len(sent_at) == env.secure.PATH_CHALLENGE_MAX_SENDS
    assert all(b - a >= interval for a, b in zip(sent_at, sent_at[1:]))
    assert all(t < T0 + 10 for t in sent_at)
    assert math.isclose(sent_at[-1], T0 + 6)
