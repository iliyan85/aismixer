"""MP1 client liveness recovery: focused unit tests.

The lab scenarios exercise these `nmea_sproxy` mechanisms only indirectly:
the pure liveness classification helpers, the bounded pre-verdict
evidence drain (including evidence that arrived while the process was
held up outside `select()`), the transient network error classification
and input pause, forwarded-sentence counting, and the retry cadence and
retransmission accounting corrected after Astra Gate A (F1, F2).
"""

import errno
import math
import os

import pytest

from test_secure_udp_helpers import load_proxy_module

REMOTE = ("192.0.2.10", 17777)
STATION = "boat_001"
C2S = b"\x01" * 32
S2C = b"\x02" * 32
LOCATOR = bytes(range(16))
LINE = "!AIVDM,1,1,,A,UNIT,0*00"


class _Stop(BaseException):
    """Ends a scripted run from inside a fake (forward_loop catches Exception)."""


def _config(**overrides):
    config = {
        "station_id": STATION,
        "keepalive_interval": 30,
        "peer_timeout": 90,
        "session_refresh_interval": 0,
        "reconnect_delay": 5,
    }
    config.update(overrides)
    return config


def _confirmed(proxy):
    return proxy.ConfirmedUdpsecSession(
        session_locator=LOCATOR,
        key_material=proxy.SessionKeyMaterial(
            client_to_server_key=C2S, server_to_client_key=S2C
        ),
    )


class _Input:
    """Local input polled like the serial adapter; with `sock` it also
    offers that object to select(), like the UDP input adapter."""

    def __init__(self, world, lines=(), *, sock=None, poll=0.5):
        self.world = world
        self.lines = list(lines)  # (due_at, raw bytes)
        self.sock = sock
        self.poll = poll

    def due(self):
        return [line for line in self.lines if line[0] <= self.world.now]

    def _take(self):
        due = self.due()
        for line in due:
            self.lines.remove(line)
        return [data for _t, data in due]

    def selectable_sockets(self):
        return [] if self.sock is None else [self.sock]

    def poll_interval(self):
        return self.poll

    def read_ready(self, _sock):
        return self._take()

    def read_pending(self):
        return self._take()


class _World:
    """Fake clock, session socket and select() for one forward_loop run.

    `inbox` holds (ready_at, datagram). A select() with a positive timeout
    advances the clock to the earlier of its timeout and the next arrival
    (or by at most `flood_step` while `flood` keeps the socket readable);
    a zero-timeout select() -- the pre-verdict drain -- never advances it.
    """

    def __init__(self, proxy, monkeypatch, *, stop_at):
        self.proxy = proxy
        self.now = 0.0
        self.stop_at = stop_at
        self.inbox = []
        self.input = None
        self.flood = None
        self.flood_step = 0.25
        self.on_send = None
        self.sent = []  # (t, decrypted message)
        self.received = []  # (t, received inside a zero-timeout drain)
        self.selects = []  # (t, timeout, offered sockets)
        self._draining = False
        monkeypatch.setattr(proxy.time, "monotonic", lambda: self.now)
        monkeypatch.setattr(proxy.select, "select", self.select)

    def sendto(self, data, _addr):
        message = self.proxy.decrypt_secure_json_message(data, C2S, LOCATOR)
        self.sent.append((self.now, message))
        if self.on_send is not None:
            self.on_send(message)
        return len(data)

    def recvfrom(self, _size):
        self.received.append((self.now, self._draining))
        if self.flood is not None:
            return self.flood, REMOTE
        ready = [item for item in self.inbox if item[0] <= self.now]
        if not ready:
            raise BlockingIOError()
        item = min(ready, key=lambda entry: entry[0])
        self.inbox.remove(item)
        return item[1], REMOTE

    def pong(self, seq, ready_at):
        packet = self.proxy.encrypt_secure_json_message(
            {"type": "pong", "seq": seq, "timestamp": 1, "source_id": STATION},
            S2C,
            LOCATOR,
        )
        self.inbox.append((ready_at, packet))

    def _ready(self, offered):
        ready = []
        if self in offered and (
            self.flood is not None or any(t <= self.now for t, _ in self.inbox)
        ):
            ready.append(self)
        if self.input is not None and self.input.sock in offered and self.input.due():
            ready.append(self.input.sock)
        return ready

    def select(self, offered, _writable, _exceptional, timeout):
        self._draining = timeout == 0
        self.selects.append((self.now, timeout, list(offered)))
        if not self._draining:
            if self.flood is not None:
                self.now += min(timeout, self.flood_step)
            elif not self._ready(offered):
                arrivals = [t for t, _ in self.inbox if t > self.now]
                if self.input is not None:
                    arrivals += [t for t, _ in self.input.lines if t > self.now]
                self.now = min([self.now + timeout] + arrivals)
            if self.now >= self.stop_at:
                raise _Stop()
        return self._ready(offered), [], []

    def run(self, local_input, config, stats=None):
        return self.proxy.forward_loop(
            local_input, self, config, _confirmed(self.proxy), REMOTE, None, stats
        )


# ------------------------------------------------------------ pure helpers


def test_liveness_verdict_is_peer_timeout_after_the_last_evidence_only():
    proxy = load_proxy_module()
    config = _config()
    assert proxy.liveness_verdict(89.999, 0.0, 1, config) is None
    assert proxy.liveness_verdict(90.0, 0.0, 1, config) == proxy.SESSION_END_PROACTIVE_REKEY
    assert proxy.liveness_verdict(90.0, 0.0, None, config) == proxy.SESSION_END_PEER_TIMEOUT
    # A missed keepalive deadline alone is never terminal.
    assert proxy.liveness_verdict(60.0, 0.0, 1, config) is None


def test_keepalive_action_retransmits_the_outstanding_ping_on_its_schedule():
    proxy = load_proxy_module()
    config = _config()
    send, retry = proxy.SESSION_ACTION_SEND_PING, proxy.SESSION_ACTION_RETRY_PING
    assert proxy.keepalive_action(59.9, 30.0, None, config) is None
    assert proxy.keepalive_action(60.0, 30.0, None, config) == send
    # Outstanding ping: first retransmission at its keepalive deadline ...
    assert proxy.keepalive_action(59.9, 30.0, 1, config) is None
    assert proxy.keepalive_action(60.0, 30.0, 1, config) == retry
    # ... then at the scheduled retry instant, never earlier.
    assert proxy.keepalive_action(64.9, 60.0, 1, config, ping_retry_at=65.0) is None
    assert proxy.keepalive_action(65.0, 60.0, 1, config, ping_retry_at=65.0) == retry
    # A short keepalive_interval also bounds the retry interval.
    assert proxy.keepalive_retry_interval(_config(keepalive_interval=3)) == 3.0


@pytest.mark.parametrize(
    "exc",
    [
        OSError(errno.ENETUNREACH, "Network is unreachable"),
        OSError(errno.EHOSTUNREACH, "No route to host"),
        OSError(errno.ENETDOWN, "Network is down"),
        OSError(errno.EADDRNOTAVAIL, "Cannot assign requested address"),
        OSError(errno.ENOBUFS, "No buffer space available"),
        ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused"),
        ConnectionResetError(errno.ECONNRESET, "Connection reset by peer"),
        ConnectionResetError(),
    ],
)
def test_transient_network_errors_are_classified_transient(exc):
    assert load_proxy_module().is_transient_network_error(exc)


@pytest.mark.parametrize(
    "exc",
    [
        OSError(errno.EBADF, "Bad file descriptor"),
        OSError(errno.EINVAL, "Invalid argument"),
        PermissionError(errno.EACCES, "Permission denied"),
        OSError(errno.EMSGSIZE, "Message too long"),
        OSError("no errno"),
        TimeoutError(),
        ValueError("not a socket error"),
    ],
)
def test_other_errors_are_not_transient(exc):
    assert not load_proxy_module().is_transient_network_error(exc)


@pytest.mark.parametrize("result, counted", [(None, 2), (True, 2), (False, 0)])
def test_only_a_sender_reporting_false_drops_the_count(result, counted):
    """Plain UDP output returns None and keeps counting every sentence; the
    UDPSEC sender returns False only for a sentence it dropped."""
    proxy = load_proxy_module()
    stats = proxy.ForwardingStats()
    sent = []

    def send_sentence(line):
        sent.append(line)
        return result

    proxy.forward_input_payload(f"{LINE}\r\n{LINE}\r\n".encode(), send_sentence, stats)
    assert len(sent) == 2
    assert stats.messages == counted


# ------------------------------------------------------------ forward_loop


@pytest.mark.parametrize(
    "pong_ready_at, survives",
    [(90.5, True), (None, False), (91.5, False)],
)
def test_evidence_that_arrived_during_a_stall_is_drained_before_the_verdict(
    monkeypatch, pong_ready_at, survives
):
    """Nothing answers ping#1 (sent at 30, retransmitted from 60); the
    peer_timeout bound is 90.0. At 89.5 an NMEA send is held up (fake)
    until 91.0, past the bound, without select() running. A matching PONG
    that became readable meanwhile (90.5) is drained and credited before
    the verdict, so the session goes on; with none readable yet -- absent,
    or arriving later (91.5) -- the verdict falls at 91.0."""
    proxy = load_proxy_module()
    world = _World(proxy, monkeypatch, stop_at=100.0)
    local = _Input(world, lines=[(89.5, f"{LINE}\r\n".encode())])
    world.input = local
    if pong_ready_at is not None:
        world.pong(1, pong_ready_at)

    def on_send(message):
        if message["type"] == "nmea":
            world.now = 91.0

    world.on_send = on_send
    stats = proxy.ForwardingStats()
    if survives:
        with pytest.raises(_Stop):
            world.run(local, _config(), stats)
        assert world.received == [(91.0, True)]
        assert world.now >= 100.0
    else:
        assert world.run(local, _config(), stats) == proxy.SESSION_END_PROACTIVE_REKEY
        assert world.now == 91.0
        assert world.received == []
    assert stats.messages == 1
    assert [t for t, m in world.sent if m["type"] == "ping"] == [30, 60, 65, 70, 75, 80, 85]


def test_pre_verdict_drain_is_bounded_under_a_datagram_flood(monkeypatch):
    """A flood of unauthenticated datagrams keeps the session socket
    readable. At the peer_timeout bound the drain reads at most
    EVIDENCE_DRAIN_MAX_DATAGRAMS of them, none of which is evidence, and
    the verdict is taken on time."""
    proxy = load_proxy_module()
    world = _World(proxy, monkeypatch, stop_at=1000.0)
    world.flood = b"\x00" * 64
    local = _Input(world, poll=None)
    config = _config(keepalive_interval=10000, peer_timeout=10)
    assert world.run(local, config) == proxy.SESSION_END_PEER_TIMEOUT
    assert world.now == 10.0
    drained = [t for t, draining in world.received if draining]
    assert drained == [10.0] * proxy.EVIDENCE_DRAIN_MAX_DATAGRAMS
    assert [t for t, timeout, _ in world.selects if timeout == 0] == drained


def test_transient_nmea_send_error_pauses_input_without_ending_the_session(monkeypatch):
    """ENETUNREACH on an NMEA send: the sentence is dropped (not counted,
    not retried) and the session stays up. While input is paused the input
    socket is not offered to select() and each wait ends by the end of the
    pause; the session socket stays watched."""
    proxy = load_proxy_module()
    world = _World(proxy, monkeypatch, stop_at=4.0)
    input_sock = object()
    local = _Input(world, lines=[(1.0, f"{LINE}\r\n".encode())], sock=input_sock, poll=None)
    world.input = local

    def on_send(message):
        if message["type"] == "nmea":
            raise OSError(errno.ENETUNREACH, "Network is unreachable")

    world.on_send = on_send
    stats = proxy.ForwardingStats()
    with pytest.raises(_Stop):
        world.run(local, _config(), stats)
    nmea = [t for t, m in world.sent if m["type"] == "nmea"]
    assert nmea == [1.0]
    assert stats.messages == 0
    paused = [(t, timeout, offered) for t, timeout, offered in world.selects if t >= 1.0]
    assert paused
    for t, timeout, offered in paused:
        assert input_sock not in offered and world in offered
        assert t + timeout <= 1.0 + proxy.keepalive_retry_interval(_config())


def test_lines_already_read_in_a_batch_are_each_attempted_never_held(monkeypatch):
    """The pause stops further reads; it does not hold back lines already
    read in the same batch (the serial adapter hands over its whole queue
    at once). Each is attempted once: the failed one is dropped, a later
    one that goes through is counted and ends the pause, so input read
    later is forwarded normally. Nothing is retried or replayed."""
    proxy = load_proxy_module()
    world = _World(proxy, monkeypatch, stop_at=4.0)
    batch = "".join(f"!AIVDM,1,1,,A,BATCH{n},0*00\r\n" for n in range(3))
    local = _Input(
        world,
        lines=[(1.0, batch.encode()), (2.0, f"{LINE}\r\n".encode())],
    )
    world.input = local
    failures = [OSError(errno.ENETUNREACH, "Network is unreachable")]

    def on_send(message):
        if message["type"] == "nmea" and failures:
            raise failures.pop()

    world.on_send = on_send
    stats = proxy.ForwardingStats()
    with pytest.raises(_Stop):
        world.run(local, _config(), stats)
    nmea = [(t, m["payload"]) for t, m in world.sent if m["type"] == "nmea"]
    assert nmea == [
        (1.0, "!AIVDM,1,1,,A,BATCH0,0*00"),
        (1.0, "!AIVDM,1,1,,A,BATCH1,0*00"),
        (1.0, "!AIVDM,1,1,,A,BATCH2,0*00"),
        (2.0, LINE),
    ]
    assert stats.messages == 3


def test_other_nmea_send_errors_still_end_the_session(monkeypatch):
    proxy = load_proxy_module()
    world = _World(proxy, monkeypatch, stop_at=4.0)
    local = _Input(world, lines=[(1.0, f"{LINE}\r\n".encode())])
    world.input = local

    def on_send(message):
        if message["type"] == "nmea":
            raise OSError(errno.EBADF, os.strerror(errno.EBADF))

    world.on_send = on_send
    assert world.run(local, _config()) == proxy.SESSION_END_SOCKET_ERROR
    assert world.now == 1.0


def test_slow_suspect_log_cannot_compress_the_retry_spacing(monkeypatch):
    """Astra Gate A F1: the first retransmission's "liveness suspect" line
    blocks for 4 s (a stalled console), so that retransmission leaves at
    64 instead of 60. The next retry is anchored at the attempt itself and
    follows a full retry interval later (69) -- not 1 s later (65), as when
    it was anchored at the instant sampled before the log line. Nothing
    answers, and the liveness bound (90, the session start + peer_timeout)
    does not move."""
    proxy = load_proxy_module()
    world = _World(proxy, monkeypatch, stop_at=1000.0)
    local = _Input(world, poll=None)

    def slow_print(*args, **_kwargs):
        if args and str(args[0]).startswith("Secure session liveness suspect"):
            world.now += 4.0

    monkeypatch.setattr(proxy, "print", slow_print, raising=False)
    config = _config()
    assert world.run(local, config) == proxy.SESSION_END_PROACTIVE_REKEY
    assert world.now == 90.0
    pings = [t for t, m in world.sent if m["type"] == "ping"]
    assert pings == [30.0, 64.0, 69.0, 74.0, 79.0, 84.0, 89.0]
    retry = proxy.keepalive_retry_interval(config)
    assert all(later - earlier >= retry for earlier, later in zip(pings, pings[1:]))
    assert len(world.selects) < 40  # one wake-up per deadline, no busy loop


def test_a11_reprobe_can_exceed_the_ordinary_retransmission_count(monkeypatch):
    """Astra Gate A F2, accounting: no fixed ceiling such as
    ceil((peer_timeout - keepalive_interval) / retry interval) -- 12 with
    the defaults -- holds for every episode. Here the last PONG and ping #2
    are both at 60, and a transient NMEA send failure at 60.1 makes the
    A11 reprobe retransmit ping #2 at 65.1, long before its keepalive
    deadline (90). Retries then follow every retry interval and nothing
    answers: 17 retransmissions, never closer than one retry interval, and
    the session still ends exactly at the PONG + peer_timeout (150). The
    send failure itself extends nothing, and the failed line is never
    resent. 17 is Astra's reproduction, not a protocol constant."""
    proxy = load_proxy_module()
    world = _World(proxy, monkeypatch, stop_at=1000.0)
    local = _Input(world, lines=[(60.1, f"{LINE}\r\n".encode())], poll=None)
    world.input = local
    world.pong(1, 60.0)
    failures = [OSError(errno.ENETUNREACH, "Network is unreachable")]

    def on_send(message):
        if message["type"] == "nmea" and failures:
            raise failures.pop()

    world.on_send = on_send
    stats = proxy.ForwardingStats()
    config = _config()
    retry = proxy.keepalive_retry_interval(config)
    assert world.run(local, config, stats) == proxy.SESSION_END_PROACTIVE_REKEY
    assert world.now == pytest.approx(60.0 + config["peer_timeout"])
    pings = [(t, m["seq"]) for t, m in world.sent if m["type"] == "ping"]
    assert pings[:2] == [(30.0, 1), (60.0, 2)]
    retransmissions = [t for t, seq in pings[2:] if seq == 2]
    assert len(retransmissions) == len(pings) - 2
    assert retransmissions == pytest.approx([65.1 + retry * k for k in range(17)])
    times = [t for t, _seq in pings]
    assert all(
        later - earlier >= retry - 1e-9 for earlier, later in zip(times, times[1:])
    )
    ordinary = math.ceil(
        (config["peer_timeout"] - config["keepalive_interval"]) / retry
    )
    assert ordinary == 12 < len(retransmissions)
    assert [t for t, m in world.sent if m["type"] == "nmea"] == [60.1]
    assert stats.messages == 0


def test_slow_initial_ping_send_cannot_compress_first_retry_spacing(monkeypatch):
    """Astra corrective recheck, F1 for `start_ping`: with
    keepalive_interval 2 s (so the retry interval is 2 s as well) and
    peer_timeout 20 s, the initial ping is due at 2.0 but its encryption is
    held up 1.5 s, so its send attempt happens at 3.5. The first retry is
    anchored at that attempt and follows keepalive_interval later (5.5) --
    not at 4.0, 0.5 s after it, as when it was anchored at the instant
    sampled before the attempt. Later retries keep the retry interval, all
    carry the same sequence under fresh nonces, and the liveness bound (the
    session start + peer_timeout = 20) does not move."""
    proxy = load_proxy_module()
    world = _World(proxy, monkeypatch, stop_at=1000.0)
    local = _Input(world, poll=None)
    real_encrypt = proxy.encrypt_secure_json_message
    ping_nonces = []

    def slow_first_ping_encrypt(message, *args, **kwargs):
        if message.get("type") == "ping" and not ping_nonces:
            world.now += 1.5  # the initial attempt's encryption is held up
        packet = real_encrypt(message, *args, **kwargs)
        if message.get("type") == "ping":
            ping_nonces.append(proxy.parse_data_packet(packet)[2])
        return packet

    monkeypatch.setattr(proxy, "encrypt_secure_json_message", slow_first_ping_encrypt)
    config = _config(keepalive_interval=2.0, peer_timeout=20.0)
    assert world.run(local, config) == proxy.SESSION_END_PROACTIVE_REKEY
    assert world.now == 20.0
    pings = [(t, m["seq"]) for t, m in world.sent if m["type"] == "ping"]
    times = [t for t, _seq in pings]
    assert times[0] == 3.5  # the actual initial attempt, due at 2.0
    assert times[1] != 4.0
    assert times[1] - times[0] >= config["keepalive_interval"]
    assert times == [3.5, 5.5, 7.5, 9.5, 11.5, 13.5, 15.5, 17.5, 19.5]
    retry = proxy.keepalive_retry_interval(config)
    assert all(later - earlier >= retry for earlier, later in zip(times[1:], times[2:]))
    assert {seq for _t, seq in pings} == {1}
    assert len(set(ping_nonces)) == len(ping_nonces) == len(pings)
    assert len(world.selects) < 40  # one wake-up per deadline, no busy loop
