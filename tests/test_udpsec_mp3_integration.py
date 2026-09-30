"""UDPSEC V2 MP3 -- liveness x migration x epoch/lifecycle integration.

MP1 (bounded client liveness recovery) and MP2 (bounded PATH_CHALLENGE
retransmission) were each validated on their own. These tests prove that
they compose with the epoch and lifecycle boundaries deferred after MP2:

- C1  a PATH_CHALLENGE retry racing a committing in-session epoch refresh;
- C2  retry / candidate expiry against the client's MP1 terminal
      `peer_timeout` verdict and its fresh authenticated establishment;
- C3  listener shutdown while a retry is admitted, in flight or scheduled;
- C4  the per-listener retry driver under event-loop and multi-listener
      load.

Real `SecureState`, the real retry pass and driver, the real epoch-refresh
helpers, the real `nmea_sproxy` client (through the recovery lab) and real
asyncio cancellation are used throughout. Narrow monkeypatches only place a
race exactly between claim, build, final revalidation and `sendto`. Test
names follow the MP3 acceptance matrix (`test_mp3_c<N><case>_`); see
tests/udpsec_recovery/UDPSEC_V2_RECOVERY_BASELINE.md section 11.
"""

import ast
import asyncio
import collections
import errno
import inspect
import textwrap
import time

import pytest

import core.udpsec_protocol as p

from test_secure_udp_helpers import (
    _FakeClock,
    _FakeQueue,
    _FakeSecureSocketFactory,
    load_secure_module_with_fake_keys,
)
from test_udpsec_path_migration import (
    ADDR_A,
    ADDR_B,
    ADDR_C,
    _Env,
    _refresh_helpers,
)
from udpsec_recovery.harness.lab import ADDR_A as LAB_ADDR_A
from udpsec_recovery.harness.lab import ADDR_B as LAB_ADDR_B
from udpsec_recovery.harness.lab import STILL_RUNNING, Lab, nmea_sentence

T0 = 1000.0
WALL = 1_000_000.0
ADDR_D = ("198.51.100.99", 42999)


@pytest.fixture
def env(monkeypatch):
    secure, client_private_key = load_secure_module_with_fake_keys(
        monkeypatch, with_client_private_key=True
    )
    return _Env(monkeypatch, secure, client_private_key)


# ------------------------------------------------------------------ helpers


class _WireSocket:
    """A listener socket. Records every datagram that reaches `sendto`,
    the owning session's CURRENT epoch generation at that instant (the
    authority it actually left under) and, with a `clock`, the send time.
    Once closed it behaves like a closed real socket: `sendto` raises EBADF
    and nothing reaches the wire."""

    def __init__(self, session=None, clock=None):
        self.session = session
        self.clock = clock
        self.sent = []
        self.authority = []
        self.times = []
        self.close_count = 0
        self.on_close = None

    def sendto(self, data, addr):
        if self.close_count:
            raise OSError(errno.EBADF, "Bad file descriptor")
        self.sent.append((data, addr))
        if self.session is not None:
            self.authority.append(self.session.current_epoch.generation)
        if self.clock is not None:
            self.times.append(self.clock())

    def close(self):
        if self.on_close is not None:
            self.on_close()
        self.close_count += 1


def _nmea(sess, tag, **kwargs):
    return sess.nmea_packet(f"!AIVDM,1,1,,A,{tag},0*00", **kwargs)


def _open(sess, *, addr=ADDR_B, now=T0, tag="open", nonce=None):
    """Open a candidate through the real receive loop; returns its initial
    challenge."""
    sock = sess.feed([(_nmea(sess, tag, nonce=nonce), addr)], now)
    return sess.sole_challenge(sock, expect_addr=addr)


def _open_direct(sess, addr, sock, clock, *, wakeup=None, wall=None):
    """What the receive loop runs for one authenticated off-path datagram,
    called directly so no loop monkeypatch is installed."""
    sess.env.secure._process_candidate_path_observation(
        sess.state,
        sess.session,
        sess.session.current_epoch,
        addr,
        sess.locator,
        sock,
        _FakeClock(WALL) if wall is None else wall,
        clock,
        clock(),
        retry_wakeup=wakeup,
    )


def _candidate(sess):
    return sess.session.path_state.candidate_path


def _retry_pass(sess, now, sock):
    return sess.env.secure._send_due_path_challenge_retries(
        sess.state, sess.endpoint_token, sock, _FakeClock(WALL), _FakeClock(now)
    )


def _challenges(sess, sent, *, generation=0, s2c_key=None):
    """(addr, token, path_generation) of each PATH_CHALLENGE in `sent` that
    authenticates under the given epoch of `sess`."""
    out = []
    for data, addr in sent:
        if not sess._is_type(data, p.PATH_CHALLENGE_TYPE, generation, s2c_key=s2c_key):
            continue
        challenge = p.parse_path_challenge_message(
            sess.decode_server_message(data, generation=generation, s2c_key=s2c_key)
        )
        out.append((addr, challenge.challenge_token, challenge.path_generation))
    return out


def _message_type(sessions, data):
    locator = p.parse_data_packet(data)[0]
    (sess,) = [s for s in sessions if s.locator == locator]
    return sess.decode_server_message(data)["type"]


def _session_identity(session):
    """The LogicalSession identity no epoch refresh or retry may change."""
    return {
        "object": id(session),
        "path_state": id(session.path_state),
        "session_key": session._session_key,
        "session_handle": session.session_handle,
        "assembly_namespace": session.assembly_namespace,
        "station_id": session.station_id,
        "created_at": session.created_at,
    }


def _run_driver(sess, clock, sock, wake):
    """Run the REAL listener retry driver (`_run_path_challenge_retries`)
    with a scripted wait: every wait with a retry due advances the fake
    clock to `wake(due)` -- a driver can only ever be late, never early --
    and once nothing is scheduled the driver is stopped. Returns the due
    instants it waited for."""
    dues = []

    async def scripted_wait(_wakeup, timeout):
        if timeout is None:
            raise asyncio.CancelledError()
        due = clock.now + timeout
        dues.append(due)
        clock.now = wake(due)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            sess.env.secure._run_path_challenge_retries(
                sock,
                sess.endpoint_token,
                sess.state,
                asyncio.Event(),
                wall_clock=_FakeClock(WALL),
                monotonic_clock=clock,
                wait=scripted_wait,
            )
        )
    return dues


# ============================================================ C1: retry x epoch refresh


def _commit_refresh(env, sess, now):
    """One real in-session epoch refresh G0 -> G1, INIT through CONFIRM, via
    the production helpers, committed at `now`. Returns G1's raw keys."""
    build_init, process_init, derive, build_confirm, process_confirm = (
        _refresh_helpers()
    )
    init = build_init(env, sess.session)
    reply, _nonce = process_init(env, sess.state, sess.session, init, now=now)
    assert reply is not None
    keys, _reply = derive(env, sess.session, init, reply, sess.s2c_key)
    confirm = build_confirm(env, sess.session, init, keys)
    _ack, newly, _nonce = process_confirm(
        env, sess.state, sess.session, confirm, now=now
    )
    assert newly is True
    return keys


class _C1:
    """Candidate B opened under G0 at T0 (initial challenge sent, first retry
    due at T0+2), plus everything a refresh must leave untouched."""

    OPENER_NONCE = b"\x5a" * 12

    def __init__(self, env):
        self.env = env
        self.state = env.new_state(retiring_epoch_overlap=50.0)
        self.sess = env.install_session(self.state, addr=ADDR_A, now=T0)
        self.first = _open(self.sess, nonce=self.OPENER_NONCE)
        self.candidate = _candidate(self.sess)
        self.g0 = self.sess.session.current_epoch
        self.g0_ledger = self.g0.seen_data_nonces
        self.identity = _session_identity(self.sess.session)
        assert self.state.next_path_challenge_retry_at(self.sess.endpoint_token) == T0 + 2

    @property
    def old_challenge(self):
        return (ADDR_B, self.first.challenge_token, self.first.path_generation)

    def assert_refresh_left_migration_state_alone(self):
        session = self.sess.session
        assert _session_identity(session) == self.identity
        assert session.current_epoch.generation == 1
        assert session.retiring_epoch is self.g0
        # G0's replay ledger is the same object and still remembers the
        # datagram that opened the candidate: nothing was reset.
        assert self.g0.seen_data_nonces is self.g0_ledger
        assert self.g0_ledger.contains(self.OPENER_NONCE)
        # The refresh itself created, renumbered or re-bound nothing.
        assert _candidate(self.sess) is self.candidate
        assert (
            self.candidate.challenge_token,
            self.candidate.path_generation,
            self.candidate.deadline,
            self.candidate.epoch,
        ) == (self.first.challenge_token, self.first.path_generation, T0 + 10, self.g0)
        assert session.path_state.path_generation == self.first.path_generation
        assert self.state.stats().path_candidates_opened == 1


def test_mp3_c1a_refresh_committed_before_the_retry_claim_sends_nothing(env):
    c1 = _C1(env)
    _commit_refresh(env, c1.sess, T0 + 2)  # at the due instant, before the claim
    sock = _WireSocket(c1.sess.session)
    assert _retry_pass(c1.sess, T0 + 2, sock) == 0
    assert sock.sent == []
    # No claim was made: the budget was not charged and the stale schedule
    # was dropped, not carried into G1.
    assert (c1.candidate.challenge_sends, c1.candidate.next_challenge_at) == (1, None)
    assert c1.state._path_challenge_retry_keys == set()
    assert c1.state.next_path_challenge_retry_at(c1.sess.endpoint_token) is None
    c1.assert_refresh_left_migration_state_alone()


@pytest.mark.parametrize("window", ["claim_to_build", "build_to_revalidation"])
def test_mp3_c1b_refresh_between_claim_and_send_sends_nothing(env, monkeypatch, window):
    c1 = _C1(env)
    real_build = env.secure._build_path_challenge_packet
    sealed = []

    def build(station_id, candidate, locator, wall_now):
        if window == "claim_to_build":
            _commit_refresh(env, c1.sess, T0 + 2)
        packet = real_build(station_id, candidate, locator, wall_now)
        sealed.append((candidate.epoch_generation, packet))
        if window == "build_to_revalidation":
            _commit_refresh(env, c1.sess, T0 + 2)
        return packet

    monkeypatch.setattr(env.secure, "_build_path_challenge_packet", build)
    sock = _WireSocket(c1.sess.session)
    assert _retry_pass(c1.sess, T0 + 2, sock) == 0
    assert sock.sent == []
    # The admitted retry was sealed only under its own epoch G0 -- never
    # re-encrypted under G1 -- and the final revalidation discarded it.
    ((sealed_generation, packet),) = sealed
    assert sealed_generation == 0
    assert _challenges(c1.sess, [(packet, ADDR_B)]) == [c1.old_challenge]
    assert (c1.candidate.challenge_sends, c1.candidate.next_challenge_at) == (2, None)
    assert c1.state._path_challenge_retry_keys == set()
    later = _WireSocket(c1.sess.session)
    for now in (T0 + 4, T0 + 6, T0 + 8):
        assert _retry_pass(c1.sess, now, later) == 0
    assert later.sent == []
    stats = c1.state.stats()
    assert (stats.migration_challenges_sent, stats.migration_challenge_retries_sent) == (1, 0)
    c1.assert_refresh_left_migration_state_alone()


def test_mp3_c1c_retry_sent_before_the_refresh_commit_is_its_last(env):
    c1 = _C1(env)
    sock = _WireSocket(c1.sess.session)
    assert _retry_pass(c1.sess, T0 + 2, sock) == 1
    assert _challenges(c1.sess, sock.sent) == [c1.old_challenge]
    assert sock.authority == [0]  # it left while G0 was still current
    assert c1.state.next_path_challenge_retry_at(c1.sess.endpoint_token) == T0 + 4
    keys = _commit_refresh(env, c1.sess, T0 + 2)  # same instant, after the send
    # The stale schedule costs at most one empty wake-up, then is dropped.
    later = _WireSocket(c1.sess.session)
    assert _retry_pass(c1.sess, T0 + 4, later) == 0
    assert later.sent == []
    assert c1.state.next_path_challenge_retry_at(c1.sess.endpoint_token) is None
    assert c1.candidate.challenge_sends == 2
    # Answering the retried challenge migrates nothing, whether under the
    # now-retiring G0 or re-encrypted under G1.
    token, generation = c1.first.challenge_token, c1.first.path_generation
    for i, response in enumerate(
        [
            c1.sess.path_response_packet(token, generation, generation=0),
            c1.sess.path_response_packet(
                token, generation, generation=1, key=keys.client_to_server_key
            ),
        ]
    ):
        replies = c1.sess.feed([(response, ADDR_B)], T0 + 4.5 + i)
        assert not any(
            c1.sess._is_type(d, p.PATH_ACK_TYPE, 0)
            or c1.sess._is_type(d, p.PATH_ACK_TYPE, 1, s2c_key=keys.server_to_client_key)
            for d, _a in replies.sent
        )
    assert c1.state.path_state_snapshot(c1.sess.session, T0 + 6)["active"] == ADDR_A
    assert c1.state.stats().path_migrations_committed == 0
    c1.assert_refresh_left_migration_state_alone()


@pytest.mark.parametrize(
    "order", ["refresh_then_claim", "claim_then_refresh", "send_then_refresh"]
)
def test_mp3_c1d_exact_instant_ordering_never_sends_under_stale_authority(
    env, monkeypatch, order
):
    """Refresh commit and retry due at the SAME fake instant, in each order
    the owner lock can serialize them. Whatever wins, a stale-candidate
    challenge reaches the wire only if it left while its own epoch was
    still current, and nothing of the old incarnation is carried into G1."""
    c1 = _C1(env)
    sock = _WireSocket(c1.sess.session)
    keys = {}
    if order == "refresh_then_claim":
        keys["g1"] = _commit_refresh(env, c1.sess, T0 + 2)
        _retry_pass(c1.sess, T0 + 2, sock)
    elif order == "claim_then_refresh":
        real_build = env.secure._build_path_challenge_packet

        def build(*args):
            packet = real_build(*args)
            keys["g1"] = _commit_refresh(env, c1.sess, T0 + 2)
            return packet

        monkeypatch.setattr(env.secure, "_build_path_challenge_packet", build)
        _retry_pass(c1.sess, T0 + 2, sock)
        monkeypatch.setattr(env.secure, "_build_path_challenge_packet", real_build)
    else:
        _retry_pass(c1.sess, T0 + 2, sock)
        keys["g1"] = _commit_refresh(env, c1.sess, T0 + 2)
    for now in (T0 + 4, T0 + 6, T0 + 8, T0 + 9.999):
        _retry_pass(c1.sess, now, sock)
    g1 = keys["g1"]
    expected = [c1.old_challenge] if order == "send_then_refresh" else []
    assert _challenges(c1.sess, sock.sent) == expected
    assert sock.authority == [0] * len(expected)
    assert _challenges(
        c1.sess, sock.sent, generation=1, s2c_key=g1.server_to_client_key
    ) == []
    assert c1.state._path_challenge_retry_keys == set()
    c1.assert_refresh_left_migration_state_alone()
    # Only NEW current-epoch traffic opens a NEW incarnation under G1, with
    # its own token, generation and fresh budget.
    opened = c1.sess.feed(
        [(_nmea(c1.sess, "g1", generation=1, key=g1.client_to_server_key), ADDR_B)],
        T0 + 9.999,
    )
    ((addr, token, generation),) = _challenges(
        c1.sess, opened.sent, generation=1, s2c_key=g1.server_to_client_key
    )
    assert (addr, generation) == (ADDR_B, c1.first.path_generation + 1)
    assert token != c1.first.challenge_token
    fresh = _candidate(c1.sess)
    assert fresh.epoch is c1.sess.session.current_epoch
    assert fresh.challenge_sends == 1


# ============================================ C2: retry/expiry x terminal liveness


@pytest.mark.parametrize(
    "late_wake, sent_at",
    [
        (T0 + 10.0, [T0, T0 + 2]),  # woken exactly at the deadline: expiry wins
        (T0 + 9.999, [T0, T0 + 2, T0 + 9.999]),  # strictly before: still sent
    ],
)
def test_mp3_c2a_retry_due_at_candidate_expiry_is_never_sent(env, late_wake, sent_at):
    state = env.new_state()
    sess = env.install_session(state, addr=ADDR_A, now=T0)
    clock = _FakeClock(T0)
    sock = _WireSocket(clock=clock)
    _open_direct(sess, ADDR_B, sock, clock)
    candidate = _candidate(sess)
    # The retry due at T0+4 only runs when the driver finally wakes at
    # `late_wake`; every other retry runs on time.
    dues = _run_driver(sess, clock, sock, lambda due: late_wake if due == T0 + 4 else due)
    assert dues[:2] == [T0 + 2, T0 + 4]
    assert sock.times == sent_at
    assert all(t < candidate.deadline for t in sock.times)
    expired = state.stats().path_candidates_expired
    if late_wake == candidate.deadline:
        assert expired == 1 and _candidate(sess) is None
        assert candidate.challenge_sends == 2  # the due retry was not charged
    else:
        assert expired == 0 and _candidate(sess) is candidate
    assert state._path_challenge_retry_keys == set()


def test_mp3_c2a_a_retry_that_would_fall_on_the_deadline_is_never_scheduled(env):
    # TTL 6 s with the fixed 2 s interval: after the attempt at +4 the next
    # one would be due exactly at the deadline, so it is never scheduled.
    state = env.new_state(path_candidate_ttl=6.0)
    sess = env.install_session(state, addr=ADDR_A, now=T0)
    clock = _FakeClock(T0)
    sock = _WireSocket(clock=clock)
    _open_direct(sess, ADDR_B, sock, clock)
    dues = _run_driver(sess, clock, sock, lambda due: due)
    assert dues == [T0 + 2, T0 + 4]
    assert sock.times == [T0, T0 + 2, T0 + 4]
    assert state.path_state_snapshot(sess.session, T0 + 5.999)["candidate"] is not None
    assert state.path_state_snapshot(sess.session, T0 + 6.0)["candidate"] is None


# The client-side C2 timelines run the real nmea_sproxy client against the
# real server in the recovery lab. `peer_timeout: 45` (valid, as in MP0's
# A5d) puts the terminal verdict at confirmed_at + 45 = 1045.2 when PONG#1
# is lost; the A->B remap makes every server reply to the old path A
# undeliverable, so migration evidence is the only thing that can save it.
# With ping#1 still outstanding at that bound, MP1's verdict is
# `proactive_rekey`: an IMMEDIATE fresh authenticated establishment, whose
# ClientHello leaves from B at 1045.2 and reaches the server at 1045.25.

PEER_TIMEOUT = 45


def _verdict_at(lab):
    """The client's MP1 terminal instant for its first session (last
    authenticated evidence = confirmation), computed as forward_loop does."""
    return lab.sessions[0].confirmed_at + float(lab.config["peer_timeout"])


def _drop_s2c(*, first_challenge=False, challenges_before_verdict=False):
    state = {"challenge_dropped": False}

    def hook(message, t, lab):
        if not message or not lab.sessions:
            return False  # the handshake itself is never disturbed
        kind = message.get("type")
        if kind == "pong" and t < _verdict_at(lab):
            lab.log("fault", f"dropped pong#{message.get('seq')} of the first session")
            return True
        if kind != "path_challenge":
            return False
        if challenges_before_verdict and t < _verdict_at(lab):
            lab.log("fault", "dropped path_challenge before the client verdict")
            return True
        if first_challenge and not state["challenge_dropped"]:
            state["challenge_dropped"] = True
            lab.log("fault", "dropped first path_challenge")
            return True
        return False

    return hook


def _hold_first_path_response_until(offset_after_verdict):
    """Deliver the client's FIRST PATH_RESPONSE to the server exactly
    `offset_after_verdict` s after the client's terminal verdict; drop every
    later one (answers to later retries), so only that late proof exists."""
    state = {"responses": 0}

    def drop(message, t, lab):
        if message and message.get("type") == "path_response":
            state["responses"] += 1
            if state["responses"] > 1:
                lab.log("fault", "dropped later path_response")
                return True
        return False

    def hold(message, t, lab):
        if message and message.get("type") == "path_response" and state["responses"] == 1:
            arrive = _verdict_at(lab) + offset_after_verdict
            lab.log("fault", f"held first path_response until {arrive!r}")
            return arrive - (t + lab.delay)
        return 0.0

    return drop, hold


def _migrating_lab(name, monkeypatch):
    """A->B remap at 1035; NMEA from B at 1040.0 opens candidate B at
    1040.05 (deadline 1050.05); its first challenge is lost, the retry at
    1042.05 is answered."""
    lab = Lab(name, monkeypatch)
    lab.remap(1035.0, LAB_ADDR_B)
    lab.drop_s2c = _drop_s2c(first_challenge=True)
    return lab


def _assert_terminated_and_not_resurrected(lab):
    old, new = lab.sessions[0], lab.sessions[1]
    assert old.reason == lab.proxy.SESSION_END_PROACTIVE_REKEY
    assert old.ended_at == _verdict_at(lab)
    assert new.locator != old.locator
    assert new.confirmed_at > old.ended_at
    assert new.reason == STILL_RUNNING
    assert len(lab.sessions) == 2
    # The terminated session never speaks again.
    assert [
        pkt for pkt in lab.packets
        if pkt.direction == "c2s" and pkt.locator == old.locator and pkt.t > old.ended_at
    ] == []
    # No half-old/half-new session: the new client session's server peer is
    # a distinct LogicalSession on a fresh epoch, with no migration state.
    new_server = lab.server_session(new.locator)
    assert new_server is not None
    assert new_server is not lab.server_session(old.locator)
    assert new_server.current_epoch.generation == 0
    assert new_server.path_state.candidate_path is None
    assert new_server.path_state.active_path == LAB_ADDR_B
    assert lab.nonce_reuse() == []
    return old, new


def test_mp3_c2b_terminal_verdict_before_migration_recovery_starts_fresh(monkeypatch):
    """Candidate B opens at ~1043 under the old session; every challenge
    sent before the client's verdict (1045.2) is lost. The client times out
    while the server candidate is still live, re-establishes from B, and the
    old incarnation's remaining retries then reach the NEW session: they
    are inert and bind nothing."""
    lab = Lab("MP3_C2B_terminal_before_migration_recovery", monkeypatch)
    lab.remap(1042.9, LAB_ADDR_B)
    lab.at(
        1043.0,
        lambda lab_: lab_.input.extra.append(
            (nmea_sentence("AIVDM,1,1,,A,MP3-C2B,0") + "\r\n").encode()
        ),
    )
    lab.drop_s2c = _drop_s2c(challenges_before_verdict=True)
    lab.run_client(until=1080.0, peer_timeout=PEER_TIMEOUT)
    old, new = _assert_terminated_and_not_resurrected(lab)
    challenges = lab.sent("s2c", "path_challenge")
    assert challenges and {pkt.locator for pkt in challenges} == {old.locator}
    # The server candidate was live when the client gave up.
    assert challenges[0].t < old.ended_at < challenges[0].t + 10.0
    delivered = [pkt for pkt in challenges if pkt.consumed_at is not None]
    assert delivered and all(pkt.consumed_at >= old.ended_at for pkt in delivered)
    assert any(
        pkt.consumed_by == "forward_loop" and pkt.consumed_at > new.confirmed_at
        for pkt in delivered
    ), "no stale retry reached the new session"
    # Nothing answered them, nothing was committed or acknowledged.
    assert lab.sent("c2s", "path_response") == []
    assert lab.sent("s2c", "path_ack") == []
    assert lab.client_lines("path migration acknowledged") == []
    assert lab.state.stats().path_migrations_committed == 0
    old_server = lab.server_session(old.locator)
    assert old_server is not None
    assert old_server.path_state.active_path == LAB_ADDR_A


@pytest.mark.parametrize("offset", [-0.01, 0.0])
def test_mp3_c2c_migration_ack_before_or_at_the_verdict_keeps_the_session(
    monkeypatch, offset
):
    """The server commits at ~1042.15; its PATH_ACK is readable 10 ms
    before, or exactly at, the client's terminal instant. Existing MP1
    rules decide: an authenticated, proof-matched PATH_ACK is liveness
    evidence, and evidence readable when the verdict falls due is credited
    first (as MP0 A5d for a PONG). No MP3-specific grace exists."""
    lab = _migrating_lab(f"MP3_C2C_ack_at_verdict{offset:+.2f}", monkeypatch)
    lab.deliver_s2c_at = lambda message, t, lab_: (
        _verdict_at(lab_) + offset
        if message and message.get("type") == "path_ack"
        else None
    )
    lab.run_client(until=1100.0, peer_timeout=PEER_TIMEOUT)
    (session,) = lab.sessions
    assert session.reason == STILL_RUNNING
    assert lab.hello_times() == [T0]  # no fresh establishment
    (ack,) = lab.sent("s2c", "path_ack")
    assert ack.deliver_at == _verdict_at(lab) + offset
    assert ack.consumed_by == "forward_loop"
    assert [t for t, _text in lab.client_lines("path migration acknowledged")] == [
        ack.deliver_at
    ]
    server = lab.original_server_session()
    assert server is lab.server_session(session.locator)
    assert server.path_state.active_path == LAB_ADDR_B
    stats = lab.state.stats()
    assert stats.path_migrations_committed == 1
    assert stats.migration_challenge_retries_sent == 1
    assert lab.nonce_reuse() == []


def test_mp3_c2d_ack_after_the_verdict_cannot_resurrect_the_session(monkeypatch):
    """As C2C, but the PATH_ACK is readable 10 ms after the verdict. The
    server legitimately committed (1042.15), yet the client already took
    its terminal verdict: the late ACK is read by the next handshake and
    ignored, and the fresh session from B replaces the old, migrated one."""
    lab = _migrating_lab("MP3_C2D_ack_after_verdict", monkeypatch)
    lab.deliver_s2c_at = lambda message, t, lab_: (
        _verdict_at(lab_) + 0.01
        if message and message.get("type") == "path_ack"
        else None
    )
    lab.run_client(until=1100.0, peer_timeout=PEER_TIMEOUT)
    old, _new = _assert_terminated_and_not_resurrected(lab)
    (ack,) = lab.sent("s2c", "path_ack")
    assert ack.consumed_by == "handshake" and ack.consumed_at > old.ended_at
    assert lab.client_lines("path migration acknowledged") == []
    stats = lab.state.stats()
    assert stats.path_migrations_committed == 1
    assert stats.sessions_replaced == 1
    assert lab.server_session(old.locator) is None


@pytest.mark.parametrize(
    "case, arrive_after_verdict",
    [
        # before the fresh ClientHello reaches the server (1045.25): the
        # server commits the OLD session to B and ACKs; the client's new
        # handshake ignores that ACK, and the fresh session from B then
        # replaces the migrated old one at B
        ("before_fresh_hello", 0.02),
        # after the fresh session from B is confirmed (~1045.4): B is now
        # owned by the new session, so the old candidate can never commit
        ("after_fresh_session", 1.0),
    ],
)
def test_mp3_c2d_late_migration_proof_after_the_verdict_binds_nothing(
    monkeypatch, case, arrive_after_verdict
):
    """The client sent its PATH_RESPONSE (1042.10) while alive, but it
    reaches the server only after the client's terminal verdict, while the
    server candidate is still live (deadline 1050.05)."""
    lab = _migrating_lab(f"MP3_C2D_late_proof_{case}", monkeypatch)
    lab.drop_c2s, lab.hold_c2s = _hold_first_path_response_until(arrive_after_verdict)
    lab.run_client(until=1100.0, peer_timeout=PEER_TIMEOUT)
    old, new = _assert_terminated_and_not_resurrected(lab)
    (proof, *_dropped) = lab.sent("c2s", "path_response")
    assert proof.t < old.ended_at  # sent while the session was alive...
    assert proof.deliver_at == pytest.approx(old.ended_at + arrive_after_verdict)
    (fresh_hello,) = [h for h in lab.sent("c2s", "client_hello") if h.t >= old.ended_at]
    assert lab.client_lines("path migration acknowledged") == []
    stats = lab.state.stats()
    acks = lab.sent("s2c", "path_ack")
    if case == "before_fresh_hello":
        assert proof.deliver_at < fresh_hello.deliver_at
        (ack,) = acks
        assert ack.t == proof.deliver_at and ack.consumed_by == "handshake"
        assert stats.path_migrations_committed == 1
        assert stats.sessions_replaced == 1
        assert lab.server_session(old.locator) is None
    else:
        assert new.confirmed_at < proof.deliver_at
        assert acks == []
        assert stats.path_migrations_committed == 0
        old_server = lab.server_session(old.locator)
        assert old_server is not None
        assert old_server.path_state.active_path == LAB_ADDR_A


# ================================================= C3: shutdown with an admitted retry


def _teardown_listener(sess, sock, now):
    """Exactly what `secure_server`'s `finally` does after stopping its
    retry driver: close the listener's own sessions (best-effort
    SESSION_CLOSE), its pending ones, then its socket."""
    secure = sess.env.secure
    secure.close_owned_sessions(
        sock,
        sess.state,
        {sess.session._session_key: sess.session},
        wall_clock=_FakeClock(WALL),
        monotonic_clock=_FakeClock(now),
    )
    secure.close_owned_pending_sessions(
        sess.state, {}, monotonic_clock=_FakeClock(now)
    )
    sock.close()


def _admitted_retry(env):
    state = env.new_state()
    sess = env.install_session(state, addr=ADDR_A, now=T0)
    sock = _WireSocket()
    _open_direct(sess, ADDR_B, sock, _FakeClock(T0))
    assert [addr for _data, addr in sock.sent] == [ADDR_B]  # initial challenge
    sock.sent.clear()
    return state, sess, sock, _candidate(sess)


def _inject_after(monkeypatch, sess, window, action):
    """Run `action()` inside the retry pass, right after `window` ends:
    'claim', 'build' or 'revalidation' (the final `path_challenge_is_current`
    check, just before `sendto`)."""
    secure, state = sess.env.secure, sess.state
    if window == "claim":
        real = state.claim_due_path_challenge_retries

        def claim(token, now):
            claims = real(token, now)
            assert len(claims) == 1
            action()
            return claims

        monkeypatch.setattr(state, "claim_due_path_challenge_retries", claim)
    elif window == "build":
        real_build = secure._build_path_challenge_packet

        def build(*args):
            packet = real_build(*args)
            action()
            return packet

        monkeypatch.setattr(secure, "_build_path_challenge_packet", build)
    else:
        real_current = state.path_challenge_is_current

        def current(session, candidate, now):
            verdict = real_current(session, candidate, now)
            assert verdict is True  # the retry passed its final revalidation
            action()
            return verdict

        monkeypatch.setattr(state, "path_challenge_is_current", current)


@pytest.mark.parametrize(
    "case, window",
    [
        ("c3a", "claim"),  # shutdown after the claim, before the build
        ("c3b", "build"),  # after the build, before the final revalidation
        ("c3c", "revalidation"),  # after the final revalidation, before sendto
    ],
)
def test_mp3_c3abc_listener_teardown_inside_an_admitted_retry_sends_no_challenge(
    env, monkeypatch, case, window
):
    """Forces the listener's own teardown into each window of one admitted
    retry. asyncio can never do this -- the pass is synchronous (see
    `test_mp3_c3c_...interleave...`) -- so this is defense in depth: the
    final revalidation refuses a closed session, and a closed socket
    refuses the send. Nothing but the listener's SESSION_CLOSE leaves."""
    state, sess, sock, candidate = _admitted_retry(env)
    torn = []

    def teardown():
        torn.append(case)
        _teardown_listener(sess, sock, T0 + 2)

    _inject_after(monkeypatch, sess, window, teardown)
    assert _retry_pass(sess, T0 + 2, sock) == 0  # contained, nothing raised
    assert torn == [case]
    assert [addr for _data, addr in sock.sent] == [ADDR_A]
    assert _message_type([sess], sock.sent[0][0]) == p.SESSION_CLOSE_TYPE
    assert sock.close_count == 1
    assert (candidate.challenge_sends, candidate.next_challenge_at) == (2, None)
    assert state._path_challenge_retry_keys == set()
    assert state.stats().migration_challenge_retries_sent == 0


@pytest.mark.parametrize("window", ["claim", "build"])
def test_mp3_c3d_session_removed_after_claim_is_refused_by_final_revalidation(
    env, monkeypatch, window
):
    """The socket stays open, so a send WOULD be recorded: only the final
    exact-session revalidation stops it."""
    state, sess, sock, candidate = _admitted_retry(env)
    _inject_after(
        monkeypatch, sess, window, lambda: state.close_session(sess.session, T0 + 2)
    )
    assert _retry_pass(sess, T0 + 2, sock) == 0
    assert sock.sent == []
    assert (candidate.challenge_sends, candidate.next_challenge_at) == (2, None)
    assert state._path_challenge_retry_keys == set()
    assert state.next_path_challenge_retry_at(sess.endpoint_token) is None


def test_mp3_c3c_nothing_can_interleave_between_final_revalidation_and_sendto(env):
    """claim -> build -> revalidate -> sendto -> finish is one synchronous
    call, and so is the receive loop's initial send. asyncio can deliver a
    cancellation, or run any other task, only at an await; the retry
    driver has exactly one, its wait between passes."""
    secure = env.secure
    assert not inspect.iscoroutinefunction(secure._send_due_path_challenge_retries)
    assert not inspect.iscoroutinefunction(secure._process_candidate_path_observation)
    tree = ast.parse(textwrap.dedent(inspect.getsource(secure._run_path_challenge_retries)))
    (only_await,) = [node for node in ast.walk(tree) if isinstance(node, ast.Await)]
    assert ast.unparse(only_await) == "await wait(wakeup, timeout)"


async def _pre_312_wait_for(aw, timeout):
    """`asyncio.wait_for` as CPython 3.8-3.11 implement it (Lib/asyncio/
    tasks.py), with its documented defect (CPython gh-86296, fixed in 3.12
    by the rewrite onto `asyncio.timeout`): when the awaited operation
    completes in the same loop turn the caller is cancelled, it RETURNS the
    result and the cancellation is lost. Debian 12, Raspberry Pi OS 12 and
    OpenWrt 23.05/24.10 ship Python 3.11."""
    loop = asyncio.get_running_loop()
    if timeout is None:
        return await aw
    fut = asyncio.ensure_future(aw)

    def release(waiter):
        if not waiter.done():
            waiter.set_result(None)

    async def cancel_and_wait(inner):
        waiter = loop.create_future()
        callback = lambda _f: release(waiter)  # noqa: E731
        inner.add_done_callback(callback)
        try:
            inner.cancel()
            await waiter
        finally:
            inner.remove_done_callback(callback)

    if timeout <= 0:
        if fut.done():
            return fut.result()
        await cancel_and_wait(fut)
        try:
            return fut.result()
        except asyncio.CancelledError as exc:
            raise asyncio.TimeoutError() from exc

    waiter = loop.create_future()
    handle = loop.call_later(timeout, release, waiter)
    callback = lambda _f: release(waiter)  # noqa: E731
    fut.add_done_callback(callback)
    try:
        try:
            await waiter
        except asyncio.CancelledError:
            if fut.done():
                return fut.result()  # the swallowed cancellation
            fut.remove_done_callback(callback)
            await cancel_and_wait(fut)
            raise
        if fut.done():
            return fut.result()
        fut.remove_done_callback(callback)
        await cancel_and_wait(fut)
        try:
            return fut.result()
        except asyncio.CancelledError as exc:
            raise asyncio.TimeoutError() from exc
    finally:
        handle.cancel()


WAIT_FOR_SEMANTICS = ["native", "pre-3.12"]


def _use_wait_for(monkeypatch, semantics):
    if semantics == "pre-3.12":
        monkeypatch.setattr(asyncio, "wait_for", _pre_312_wait_for)


@pytest.mark.parametrize("wait_for", WAIT_FOR_SEMANTICS)
def test_mp3_c3e_retry_wait_never_swallows_a_cancellation_racing_its_wakeup(
    env, monkeypatch, wait_for
):
    _use_wait_for(monkeypatch, wait_for)

    async def main():
        wakeup = asyncio.Event()
        waiter = asyncio.create_task(
            env.secure._wait_for_path_challenge_retry(wakeup, 30.0)
        )
        for _ in range(3):
            await asyncio.sleep(0)
        wakeup.set()  # the wakeup and the cancellation land in one turn
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        return waiter.cancelled()

    assert asyncio.run(main()) is True


def test_mp3_c3e_retry_wait_ends_on_timeout_or_wakeup_and_leaves_no_timer(env):
    wait = env.secure._wait_for_path_challenge_retry

    async def main():
        wakeup = asyncio.Event()
        await asyncio.wait_for(wait(wakeup, 0.02), 5.0)  # times out
        wakeup.clear()
        early = asyncio.create_task(wait(wakeup, 0.05))
        await asyncio.sleep(0)
        wakeup.set()  # a wakeup ends it before its timeout...
        await asyncio.wait_for(early, 5.0)
        wakeup.clear()
        await asyncio.sleep(0.15)  # ...and its timer never fires later
        stale = wakeup.is_set()
        unbounded = asyncio.create_task(wait(wakeup, None))
        await asyncio.sleep(0.05)
        still_waiting = not unbounded.done()
        wakeup.set()
        await asyncio.wait_for(unbounded, 5.0)
        return stale, still_waiting

    assert asyncio.run(main()) == (False, True)


def _listener_stop_race(env, monkeypatch, ending):
    """Run the REAL `secure_server` -- real retry driver, real wait helper,
    real teardown -- with only the receive loop scripted, so that a
    candidate wakes the driver in the very loop turn the listener stops,
    while another candidate's retry is already due by the clock.

    `ending` is 'listener_error' (the receive loop raises) or
    'parent_cancel' (the listener task is cancelled by its owner)."""
    secure = env.secure
    state = env.new_state()
    clock = _FakeClock(T0)
    sock = _WireSocket()
    token = env.endpoint_token()
    monkeypatch.setattr(secure, "_new_endpoint_token", lambda: token)
    monkeypatch.setattr(
        secure, "create_udp_listener_socket", _FakeSecureSocketFactory(sock)
    )
    first = env.install_session(state, addr=ADDR_A, endpoint_token=token, now=T0)
    second = env.install_session(state, addr=ADDR_C, endpoint_token=token, now=T0)
    seen = {}

    real_driver = secure._run_path_challenge_retries

    async def driver(*args, **kwargs):
        seen["driver"] = asyncio.current_task()
        return await real_driver(*args, **kwargs)

    real_close_owned = secure.close_owned_sessions

    def close_owned(*args, **kwargs):
        seen["driver_done_at_session_close"] = seen["driver"].done()
        return real_close_owned(*args, **kwargs)

    def on_socket_close():
        seen["driver_done_at_socket_close"] = seen["driver"].done()

    monkeypatch.setattr(secure, "_run_path_challenge_retries", driver)
    monkeypatch.setattr(secure, "close_owned_sessions", close_owned)
    sock.on_close = on_socket_close

    def open_candidate(sess, addr):
        _open_direct(sess, addr, sock, clock, wakeup=seen["wakeup"])

    async def receive_loop(_sock, _queue, _ip, _port, **kwargs):
        for sess in (first, second):
            kwargs["owned_sessions"][sess.session._session_key] = sess.session
        seen["wakeup"] = kwargs["path_challenge_retry_wakeup"]
        open_candidate(first, ADDR_B)  # its first retry is due at T0+2
        for _ in range(3):
            await asyncio.sleep(0)  # the driver now waits for T0+2
        clock.now = T0 + 2  # due by the clock; the driver's timer has not fired
        if ending == "listener_error":
            open_candidate(second, ADDR_D)  # wakes the driver...
            seen["shutdown_index"] = len(sock.sent)
            raise RuntimeError("listener failed")  # ...in the turn it dies
        seen["ready"].set_result(None)
        await asyncio.Event().wait()

    monkeypatch.setattr(secure, "_secure_server_loop", receive_loop)

    async def main():
        seen["ready"] = asyncio.get_running_loop().create_future()
        server = asyncio.create_task(
            secure.secure_server(
                _FakeQueue(),
                "127.0.0.1",
                9999,
                state=state,
                wall_clock=_FakeClock(WALL),
                monotonic_clock=clock,
                server_private_key=env.server_private_key,
            )
        )
        if ending == "parent_cancel":
            await seen["ready"]
            server.cancel()  # the owner stops the listener...
            open_candidate(second, ADDR_D)  # ...as a candidate wakes the driver
            seen["shutdown_index"] = len(sock.sent)
        done, _pending = await asyncio.wait({server}, timeout=5.0)
        seen["server_finished"] = server in done
        if server in done:
            seen["outcome"] = (
                "cancelled" if server.cancelled() else repr(server.exception())
            )
        for _ in range(3):
            await asyncio.sleep(0)
        seen["driver_done_after"] = seen["driver"].done()
        for task in (server, seen["driver"]):
            task.cancel()  # a stuck or zombie task must not outlive the test
        await asyncio.gather(server, seen["driver"], return_exceptions=True)

    asyncio.run(main())
    after = sock.sent[seen["shutdown_index"]:]
    seen["after_shutdown"] = [
        (_message_type([first, second], data), addr) for data, addr in after
    ]
    return seen, sock


@pytest.mark.parametrize("wait_for", WAIT_FOR_SEMANTICS)
@pytest.mark.parametrize("ending", ["listener_error", "parent_cancel"])
def test_mp3_c3e_listener_shutdown_ends_the_retry_driver_before_cleanup(
    env, monkeypatch, ending, wait_for
):
    _use_wait_for(monkeypatch, wait_for)
    seen, sock = _listener_stop_race(env, monkeypatch, ending)
    assert seen["server_finished"], "listener shutdown waited indefinitely"
    # The original listener outcome is preserved.
    assert seen["outcome"] == (
        "cancelled" if ending == "parent_cancel" else "RuntimeError('listener failed')"
    )
    # Once shutdown began, only the listener's own SESSION_CLOSEs left.
    assert seen["after_shutdown"] == [
        (p.SESSION_CLOSE_TYPE, ADDR_A),
        (p.SESSION_CLOSE_TYPE, ADDR_C),
    ]
    # The driver had actually finished -- not merely been asked to -- before
    # any listener session or the socket was closed, and nothing outlives it.
    assert seen["driver_done_at_session_close"] is True
    assert seen["driver_done_at_socket_close"] is True
    assert seen["driver_done_after"] is True
    assert sock.close_count == 1


def test_mp3_retry_driver_failure_stops_only_the_driver(env, monkeypatch, capsys):
    """MP2 failure policy, inside the real `secure_server`: an unexpected
    error in a retry pass is logged once and stops only that listener's
    driver (no busy loop, no zombie); the listener keeps receiving, the
    candidate keeps its single initial challenge and still expires on time,
    and shutdown is clean."""
    secure = env.secure
    state = env.new_state()
    clock = _FakeClock(T0)
    sock = _WireSocket()
    token = env.endpoint_token()
    monkeypatch.setattr(secure, "_new_endpoint_token", lambda: token)
    monkeypatch.setattr(
        secure, "create_udp_listener_socket", _FakeSecureSocketFactory(sock)
    )
    sess = env.install_session(state, addr=ADDR_A, endpoint_token=token, now=T0)
    real_claim = state.claim_due_path_challenge_retries
    claims = []

    def claim(endpoint_token, now):
        claims.append(now)
        if len(claims) > 1:
            raise RuntimeError("boom")
        return real_claim(endpoint_token, now)

    monkeypatch.setattr(state, "claim_due_path_challenge_retries", claim)
    seen = {}
    real_driver = secure._run_path_challenge_retries

    async def driver(*args, **kwargs):
        seen["driver"] = asyncio.current_task()
        return await real_driver(*args, **kwargs)

    monkeypatch.setattr(secure, "_run_path_challenge_retries", driver)

    async def receive_loop(_sock, _queue, _ip, _port, **kwargs):
        kwargs["owned_sessions"][sess.session._session_key] = sess.session
        wakeup = kwargs["path_challenge_retry_wakeup"]
        _open_direct(sess, ADDR_B, sock, clock, wakeup=wakeup)
        for _ in range(3):
            await asyncio.sleep(0)  # the driver now waits for T0+2
        clock.now = T0 + 2
        wakeup()  # its retry pass runs -- and fails
        for _ in range(3):
            await asyncio.sleep(0)
        seen["driver_stopped_while_listening"] = seen["driver"].done()
        clock.now = T0 + 10.0  # the listener keeps running to the deadline
        seen["candidate_at_deadline"] = state.path_state_snapshot(
            sess.session, clock.now
        )["candidate"]

    monkeypatch.setattr(secure, "_secure_server_loop", receive_loop)
    asyncio.run(
        secure.secure_server(
            _FakeQueue(),
            "127.0.0.1",
            9999,
            state=state,
            wall_clock=_FakeClock(WALL),
            monotonic_clock=clock,
            server_private_key=env.server_private_key,
        )
    )
    assert len(claims) == 2  # the startup pass, then the failing one: no retry loop
    assert seen["driver_stopped_while_listening"] is True
    assert not seen["driver"].cancelled() and seen["driver"].exception() is None
    out = capsys.readouterr().out
    assert out.count("PATH_CHALLENGE retry driver stopped: RuntimeError: boom") == 1
    assert seen["candidate_at_deadline"] is None
    assert state.stats().path_candidates_expired == 1
    challenges = [
        addr for data, addr in sock.sent
        if _message_type([sess], data) == p.PATH_CHALLENGE_TYPE
    ]
    assert challenges == [ADDR_B]  # pre-MP2 behaviour: the initial send only
    assert sock.close_count == 1


# ======================================================== C4: retry driver under load

# Real time, real event loop, real driver and wait helper. The production
# 10 s TTL / 2 s interval ratio is kept at 0.5 s / 0.1 s so a run takes
# well under a second; MAX_SENDS stays 4.
C4_INTERVAL = 0.1
C4_TTL = 0.5
C4_WINDOW = C4_TTL + 0.2


class _TimedSocket:
    def __init__(self):
        self.sent = []  # (monotonic send time, data, addr)

    def sendto(self, data, addr):
        self.sent.append((time.monotonic(), data, addr))


def _spin(seconds):
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        pass


async def _busy(stop_at, chunk):
    """One synthetic busy callback source: `chunk` s of CPU per loop turn."""
    while time.monotonic() < stop_at:
        _spin(chunk)
        await asyncio.sleep(0)


def _instrument(monkeypatch, secure, state):
    """Record, without changing behaviour: every retry pass per listener,
    every claim (which listener asked, which listeners' sessions it got),
    and the intended due instant of every scheduled retry attempt."""
    passes = collections.Counter()
    claims = []
    due = {}
    real_pass = secure._send_due_path_challenge_retries
    real_claim = state.claim_due_path_challenge_retries
    real_finish = state.finish_path_challenge_attempt

    def counting_pass(state_owner, endpoint_token, *args):
        passes[endpoint_token] += 1
        return real_pass(state_owner, endpoint_token, *args)

    def claim(endpoint_token, now):
        admitted = real_claim(endpoint_token, now)
        claims.append(
            (endpoint_token, [c.session._session_key.endpoint_token for c in admitted])
        )
        return admitted

    def finish(session, candidate, outcome, now):
        scheduled = real_finish(session, candidate, outcome, now)
        if scheduled:
            due[(id(candidate), candidate.challenge_sends + 1)] = candidate.next_challenge_at
        return scheduled

    monkeypatch.setattr(secure, "_send_due_path_challenge_retries", counting_pass)
    monkeypatch.setattr(state, "claim_due_path_challenge_retries", claim)
    monkeypatch.setattr(state, "finish_path_challenge_attempt", finish)
    return passes, claims, due


def _attempts(sock, sess, candidate):
    return [
        t for t, data, addr in sock.sent
        if p.parse_data_packet(data)[0] == sess.locator and addr == candidate.sockaddr
    ]


def _check_attempts(label, attempts, candidate, due):
    """Correctness under load: bounded, never early, never at/after expiry,
    at least one interval apart, and at least one retry (no starvation).
    Returns (attempt, intended due, actual send, lateness) rows."""
    assert 2 <= len(attempts) <= 4, (label, attempts)
    rows = []
    for attempt, sent in enumerate(attempts[1:], start=2):
        intended = due[(id(candidate), attempt)]
        assert sent >= intended, (label, attempt, sent, intended)  # never early
        rows.append((attempt, intended, sent, sent - intended))
    assert all(t < candidate.deadline for t in attempts), label
    assert all(b - a >= C4_INTERVAL for a, b in zip(attempts, attempts[1:])), label
    assert candidate.challenge_sends == len(attempts), label
    return rows


def _report(label, rows, candidate, attempts):
    opened = attempts[0]
    lateness = ", ".join(
        f"#{n} due +{d - opened:.3f}s sent +{s - opened:.3f}s late {late * 1000:.1f}ms"
        for n, d, s, late in rows
    )
    fit = "all fit" if len(attempts) == 4 else f"{len(attempts)}/4 fit"
    print(
        f"[MP3-{label}] {fit} before expiry +{candidate.deadline - opened:.3f}s; "
        f"{lateness}"
    )


def test_mp3_c4a_retries_under_a_busy_event_loop_are_late_never_early(
    env, monkeypatch
):
    secure = env.secure
    monkeypatch.setattr(secure, "PATH_CHALLENGE_RETRY_INTERVAL_SECONDS", C4_INTERVAL)
    state = env.new_state(path_candidate_ttl=C4_TTL)
    sess = env.install_session(state, addr=ADDR_A, now=time.monotonic())
    passes, _claims, due = _instrument(monkeypatch, secure, state)

    async def main():
        sock, wakeup = _TimedSocket(), asyncio.Event()
        driver = asyncio.create_task(
            secure._run_path_challenge_retries(
                sock, sess.endpoint_token, state, wakeup, monotonic_clock=time.monotonic
            )
        )
        await asyncio.sleep(0)
        stop_at = time.monotonic() + C4_WINDOW
        # 8 callback sources x 2 ms of CPU each: every loop turn is ~16 ms.
        load = [asyncio.create_task(_busy(stop_at, 0.002)) for _ in range(8)]
        _open_direct(sess, ADDR_B, sock, time.monotonic, wakeup=wakeup.set, wall=time.time)
        max_tasks = max_keys = 0
        while time.monotonic() < stop_at:
            max_tasks = max(max_tasks, len(asyncio.all_tasks()))
            max_keys = max(max_keys, len(state._path_challenge_retry_keys))
            await asyncio.sleep(0)
        await asyncio.gather(*load)
        driver.cancel()
        await asyncio.gather(driver, return_exceptions=True)
        return sock, max_tasks, max_keys

    sock, max_tasks, max_keys = asyncio.run(main())
    candidate = _candidate(sess)
    attempts = _attempts(sock, sess, candidate)
    rows = _check_attempts("C4A", attempts, candidate, due)
    assert max_tasks == 10  # main + driver + 8 load sources: no task growth
    assert max_keys <= 1
    # No busy loop: a handful of passes per attempt at most (the startup
    # pass, the wake-up after the initial send, and a pass whose timer fired
    # within the loop's clock resolution of the due instant).
    assert passes[sess.endpoint_token] <= 3 * len(attempts) + 2
    _report("C4A", rows, candidate, attempts)


def test_mp3_c4b_a_heavy_listener_cannot_starve_or_steal_another_listeners_retries(
    env, monkeypatch
):
    """Listener 1: 8 sessions whose candidates all retry on the same
    schedule plus 8 sessions under continuous candidate churn (every churn
    step sends an initial challenge and wakes driver 1), and 4 busy
    callback sources. Listener 2: one quiet candidate. Both drivers share
    one SecureState."""
    secure = env.secure
    monkeypatch.setattr(secure, "PATH_CHALLENGE_RETRY_INTERVAL_SECONDS", C4_INTERVAL)
    state = env.new_state(path_candidate_ttl=C4_TTL)
    now = time.monotonic()
    token1, token2 = env.endpoint_token(), env.endpoint_token()
    stable = [
        env.install_session(state, addr=(f"192.0.2.{10 + i}", 41000), endpoint_token=token1, now=now)
        for i in range(8)
    ]
    churned = [
        env.install_session(state, addr=(f"192.0.2.{30 + i}", 41000), endpoint_token=token1, now=now)
        for i in range(8)
    ]
    quiet = env.install_session(state, addr=ADDR_C, endpoint_token=token2, now=now)
    passes, claims, due = _instrument(monkeypatch, secure, state)

    async def main():
        sock1, sock2 = _TimedSocket(), _TimedSocket()
        wake1, wake2 = asyncio.Event(), asyncio.Event()
        drivers = [
            asyncio.create_task(
                secure._run_path_challenge_retries(
                    sock, token, state, wake, monotonic_clock=time.monotonic
                )
            )
            for sock, token, wake in ((sock1, token1, wake1), (sock2, token2, wake2))
        ]
        await asyncio.sleep(0)
        stop_at = time.monotonic() + C4_WINDOW
        for i, sess in enumerate(stable):
            _open_direct(sess, (f"198.51.100.{10 + i}", 42000), sock1, time.monotonic,
                         wakeup=wake1.set, wall=time.time)
        _open_direct(quiet, ADDR_D, sock2, time.monotonic, wakeup=wake2.set, wall=time.time)

        async def churn():
            steps = 0
            while time.monotonic() < stop_at:
                sess = churned[steps % 8]
                addr = (f"203.0.113.{10 + steps % 8}", 43000 + (steps // 8) % 2)
                _open_direct(sess, addr, sock1, time.monotonic, wakeup=wake1.set, wall=time.time)
                steps += 1
                _spin(0.0005)
                await asyncio.sleep(0)
            return steps

        load = [asyncio.create_task(_busy(stop_at, 0.002)) for _ in range(4)]
        churn_task = asyncio.create_task(churn())
        max_tasks = max_keys = 0
        while time.monotonic() < stop_at:
            max_tasks = max(max_tasks, len(asyncio.all_tasks()))
            max_keys = max(max_keys, len(state._path_challenge_retry_keys))
            await asyncio.sleep(0)
        await asyncio.gather(*load)
        steps = await churn_task
        for driver in drivers:
            driver.cancel()
        await asyncio.gather(*drivers, return_exceptions=True)
        return sock1, sock2, steps, max_tasks, max_keys

    sock1, sock2, steps, max_tasks, max_keys = asyncio.run(main())
    listener1 = {s.locator for s in stable + churned}
    # Isolation: each listener's socket carries only its own sessions'
    # challenges; listener 2's go only to its own candidate address.
    assert {p.parse_data_packet(d)[0] for _t, d, _a in sock1.sent} <= listener1
    assert {(p.parse_data_packet(d)[0], a) for _t, d, a in sock2.sent} == {
        (quiet.locator, ADDR_D)
    }
    assert all(asker == got for asker, admitted in claims for got in admitted)
    # Listener 2 was neither starved nor robbed by listener 1's load.
    quiet_candidate = _candidate(quiet)
    rows = _check_attempts("C4B-quiet", _attempts(sock2, quiet, quiet_candidate), quiet_candidate, due)
    worst_stable = []
    for sess in stable:
        candidate = _candidate(sess)
        attempts = _attempts(sock1, sess, candidate)
        tokens = {
            ch[1] for ch in _challenges(sess, [(d, a) for _t, d, a in sock1.sent if p.parse_data_packet(d)[0] == sess.locator])
        }
        assert tokens == {candidate.challenge_token}
        worst_stable.append(max(r[3] for r in _check_attempts("C4B-stable", attempts, candidate, due)))
    assert steps > 50  # the churn really ran
    assert max_tasks == 8  # main + 2 drivers + 4 load + churn: no task growth
    assert max_keys <= len(stable) + len(churned) + 1
    assert passes[token2] <= 3 * quiet_candidate.challenge_sends + 2
    _report("C4B-quiet", rows, quiet_candidate, _attempts(sock2, quiet, quiet_candidate))
    print(
        f"[MP3-C4B] churn steps {steps}; listener-1 passes {passes[token1]}; "
        f"worst stable-candidate lateness {max(worst_stable) * 1000:.1f}ms"
    )


@pytest.mark.parametrize(
    "lateness, sent_offsets",
    [
        (0.0, [0.0, 2.0, 4.0, 6.0]),
        (0.5, [0.0, 2.5, 5.0, 7.5]),
        (1.5, [0.0, 3.5, 7.0]),  # the 4th would be due +9.0, runs +10.5: omitted
        (4.0, [0.0, 6.0]),  # the 3rd would be due +8.0, runs +12.0: omitted
    ],
)
def test_mp3_c4_a_late_driver_fits_fewer_attempts_but_never_early_or_expired(
    env, lateness, sent_offsets
):
    """Deterministic lateness model: every driver wake-up is `lateness` s
    late. Each next retry is scheduled from the actual attempt, so lateness
    only reduces how many attempts fit before the FIXED deadline; a retry
    that would run at or after it is omitted and the candidate expires on
    time."""
    state = env.new_state()
    sess = env.install_session(state, addr=ADDR_A, now=T0)
    clock = _FakeClock(T0)
    sock = _WireSocket(clock=clock)
    _open_direct(sess, ADDR_B, sock, clock)
    candidate = _candidate(sess)
    dues = _run_driver(sess, clock, sock, lambda due: due + lateness)
    assert sock.times == [T0 + offset for offset in sent_offsets]
    for sent, intended in zip(sock.times[1:], dues):
        assert sent == intended + lateness >= intended
    assert all(t < candidate.deadline for t in sock.times)
    omitted = len(sent_offsets) < 4
    assert state.stats().path_candidates_expired == (1 if omitted else 0)
    assert candidate.deadline == T0 + 10
    assert state._path_challenge_retry_keys == set()
