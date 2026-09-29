"""Deterministic UDPSEC V2 recovery lab (MP0 recovery baseline).

Repository-local port of the independent Fable audit harness
(`udpsec_lab.py`, audit of `de513674`, 2026-09-28). It drives the REAL
`nmea_sproxy` client -- `perform_handshake()` then `forward_loop()`, with
`main()`'s relation loop replicated around the real
`retry_delay_for_reason()` -- against the REAL
`aismixer_secure._secure_server_loop()` and `SecureState`, joined by a
test-controlled datagram shim on one shared fake monotonic clock.

Nothing here re-implements protocol logic. Every handshake, AEAD
operation, nonce admission, keepalive/deadline decision, path-migration
step and epoch refresh is production code; the lab only decides WHEN a
datagram is delivered (one-way delay, blackhole windows, per-packet
drop/hold hooks, NAT remapping, local send errors) and WHEN the local
input produces NMEA.

Fidelity limits (also recorded in UDPSEC_V2_RECOVERY_BASELINE.md):

* `main()` is replicated, not executed: config/key loading, socket
  creation, `setproctitle`, SIGTERM handling and its best-effort close are
  not exercised (a drift guard checks the replicated call order);
* the server runs one datagram per `asyncio.run()` with a fake
  `sock_recvfrom`; its maintenance task never runs (lazy expiry only);
* `time.monotonic`, `time.time` and `select.select` are patched
  process-wide for the test's duration through pytest's `monkeypatch`;
* the one-way delay is constant, loss is decided at send time, the server
  processes a datagram in zero time, and the local input is a serial-like
  fake (no reader thread, no 256-line drop-oldest queue);
* no sockets and no network access.

Set the environment variable named by `TRACE_DIR_ENV` to a directory to
write each scenario's merged event/packet/ingress trace there (off by
default; nothing is written during a normal test run).
"""

import asyncio
import dataclasses
import errno
import json
import os
import socket
import weakref
from dataclasses import dataclass

from cryptography.hazmat.primitives.asymmetric import ec

import core.udpsec_protocol as protocol
from test_secure_udp_helpers import (
    _FakeAsyncioModule,
    _FakeSecureLoop,
    _FakeSecureSocket,
    load_proxy_module,
    load_secure_module_with_fake_keys,
)

STATION_ID = "boat_001"
# The client's pinned server endpoint. Documentation addresses only
# (RFC 5737 / RFC 3849) -- never field addresses.
SERVER_ADDR = ("192.0.2.50", 19999)
# Server-observed (external) client tuples, shaped like real recvfrom().
ADDR_A = ("2001:db8:a::1", 46770, 0, 0)
ADDR_B = ("2001:db8:b::7", 46770, 0, 0)
ADDR_C = ("203.0.113.30", 43000)
ADDR_A2 = ("2001:db8:a::1", 46770, 12345, 0)  # A with a different flowinfo
# Stand-ins for the IPv4/CGNAT field observation: the same public address
# with a different public UDP port after a long outage (field port numbers).
CGNAT_BEFORE = ("198.51.100.195", 54654)
CGNAT_AFTER = ("198.51.100.195", 54104)

T0 = 1000.0
WALL0 = 1_000_000.0
ONE_WAY_DELAY = 0.05
STILL_RUNNING = "still-running-at-scenario-end"
TRACE_DIR_ENV = "UDPSEC_RECOVERY_TRACE_DIR"

# Production client defaults (`nmea_sproxy.DEFAULT_CONFIG`); a drift-guard
# test keeps these in sync.
DEFAULT_CLIENT_CONFIG = {
    "station_id": STATION_ID,
    "keepalive_interval": 30,
    "peer_timeout": 90,
    "session_refresh_interval": 0,
    "reconnect_delay": 5,
}
# `main()` sets this timeout on its output socket before the first handshake.
CLIENT_SOCKET_TIMEOUT = 5.0


class HarnessError(RuntimeError):
    """The lab itself misbehaved (busy loop, fault never injected, ...).

    Deliberately NOT an AssertionError: the strict-xfail MP1 acceptance
    tests are marked ``raises=AssertionError``, so a broken harness can
    never masquerade as the expected pre-MP1 behavioural failure."""


class _StopScenario(BaseException):
    """Raised from the fake clock when the scenario window ends. A
    BaseException so production ``except Exception`` blocks cannot swallow
    it."""


class _HarnessAbort(BaseException):
    """A harness fault detected on a production call path (inside a fake
    select/recv/sendto the client is executing). A BaseException for the
    same reason as `_StopScenario`: `forward_loop()` would otherwise turn it
    into `socket_error`. `run_client()` re-raises it as HarnessError."""


def nmea_sentence(body):
    checksum = 0
    for char in body:
        checksum ^= ord(char)
    return f"!{body}*{checksum:02X}"


class Clock:
    """The one fake monotonic clock shared by client, server and network."""

    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now

    def wall(self):
        return WALL0 + (self.now - T0)


class LabInput:
    """Serial-like local input for `forward_loop()`: no selectable sockets,
    a fixed poll interval, and one valid ``!AIVDM`` sentence every
    `interval` seconds of fake time. Each sentence carries a unique 6-digit
    marker so its arrival at server ingress can be traced."""

    def __init__(self, clock, interval=10.0, poll=0.2):
        self.clock = clock
        self.poll = poll
        self.produced = []  # (marker, due_at, read_at)
        self.extra = []
        self.set_interval(interval)

    def set_interval(self, interval):
        self.interval = interval
        self.next_at = self.clock.now + interval if interval else None

    def selectable_sockets(self):
        return []

    def poll_interval(self):
        return self.poll

    def read_ready(self, _sock):
        return []

    def read_pending(self):
        lines = []
        while self.next_at is not None and self.clock.now >= self.next_at:
            marker = f"{len(self.produced) + 1:06d}"
            self.produced.append((marker, self.next_at, self.clock.now))
            lines.append(
                (nmea_sentence(f"AIVDM,1,1,,A,{marker},0") + "\r\n").encode()
            )
            self.next_at += self.interval
        lines.extend(self.extra)
        self.extra = []
        return lines

    def next_line_time(self):
        return self.next_at

    def start(self):
        pass

    def close(self):
        pass


@dataclass
class ClientSession:
    """One confirmed client session, as seen by the replicated main loop."""

    label: str
    locator: bytes
    handshake_started: float
    confirmed_at: float
    observed: object
    path_gen: object
    ended_at: float = None
    reason: str = None
    # The `epoch_state_ref` list handed to this session's forward_loop();
    # forward_loop() publishes its live `_ClientEpochSet` into it.
    epoch_ref: list = dataclasses.field(default_factory=list, repr=False)

    @property
    def epochs(self):
        return self.epoch_ref[0] if self.epoch_ref else None


@dataclass
class Packet:
    """One datagram offered to the simulated network."""

    t: float
    direction: str  # "c2s" or "s2c"
    kind: str  # client_hello, server_hello, ping#N, pong#N, nmea, path_ack, ...
    message: object  # decoded plaintext dict (test-only decode) or None
    addr: object  # c2s: server-observed source; s2c: destination
    raw: bytes
    verdict: str = "sent"  # "sent", "DROP:<why>", "LOCAL-ERROR:<errno>"
    deliver_at: float = None
    locator: bytes = None
    generation: int = None
    nonce: bytes = None
    consumed_at: float = None
    consumed_by: str = None  # s2c only: "handshake" or "forward_loop"

    @property
    def transmitted(self):
        return not self.verdict.startswith("LOCAL-ERROR")


class _LabClientSocket:
    """The client's single UDP socket. `main()` creates it once and reuses
    it for every handshake, so every session shares one local port."""

    def __init__(self, lab):
        self.lab = lab
        self.timeout = CLIENT_SOCKET_TIMEOUT

    def sendto(self, data, addr):
        self.lab._guarded(self.lab._client_send, data, addr)
        return len(data)

    def settimeout(self, timeout):
        self.timeout = timeout

    def gettimeout(self):
        return self.timeout

    def recvfrom(self, _bufsize):
        data = self.lab._guarded(self.lab._recv_blocking, self.timeout)
        if data is None:
            raise socket.timeout("timed out")
        return data, SERVER_ADDR


class _LabIngressQueue:
    """Stands in for the aismixer ingress queue and its last consumer:
    records each admitted frame, then releases its admission lease."""

    def __init__(self, lab):
        self.lab = lab

    async def put(self, frame):
        self.lab.ingress.append(
            (
                self.lab.clock.now,
                frame.payload.decode("ascii", "replace"),
                frame.assembler_key,
            )
        )
        frame.release_admission_lease(self.lab.clock.now)


class Lab:
    """One scenario run: real client + real server + simulated network."""

    def __init__(self, name, monkeypatch, *, delay=ONE_WAY_DELAY, nmea_interval=10.0):
        self.name = name
        self.monkeypatch = monkeypatch
        self.clock = Clock()
        self.delay = delay
        self.secure, self.station_private_key = load_secure_module_with_fake_keys(
            monkeypatch, with_client_private_key=True
        )
        self.proxy = load_proxy_module()
        self.server_private_key = ec.derive_private_key(0xF4B1E, ec.SECP256R1())
        self.server_public_key = self.server_private_key.public_key()
        # Production wiring (`aismixer_secure.secure_server`): one listener
        # endpoint token and per-listener weak ownership maps that live as
        # long as the listener does.
        self.state = self.secure.SecureState(clock=self.clock)
        self.endpoint_token = self.secure._new_endpoint_token()
        self.owned_sessions = weakref.WeakValueDictionary()
        self.owned_pending = weakref.WeakValueDictionary()
        self._fake_asyncio = _FakeAsyncioModule(_FakeSecureLoop([]))
        monkeypatch.setattr(self.secure, "asyncio", self._fake_asyncio)
        monkeypatch.setattr(self.secure, "print", self._server_print, raising=False)
        monkeypatch.setattr(self.proxy, "print", self._client_print, raising=False)

        self.input = LabInput(self.clock, interval=nmea_interval)
        self.sock = _LabClientSocket(self)
        self.queue = _LabIngressQueue(self)
        self.stats = self.proxy.ForwardingStats()
        self.config = None

        # Network model.
        self.external_addr = ADDR_A
        self.blackholes = []  # (start, end, "c2s" | "s2c" | "both")
        self.send_errors = []  # (start, end, errno): local sendto() raises
        self.drop_c2s = None  # hook(message, t, lab) -> bool
        self.drop_s2c = None
        self.hold_c2s = None  # hook(message, t, lab) -> extra seconds
        self.hold_s2c = None
        self.deliver_s2c_at = None  # hook(message, t, lab) -> absolute time | None
        self.poll_limit = 200_000
        self.until = None
        self._pending_c2s = []  # [deliver_at, seq, data, source_addr]
        self._inbox = []  # [deliver_at, seq, data, Packet]
        self._scheduled = []  # [t, callback(lab), fired]
        self._seq = 0
        self._polls = 0
        self._consumer = "handshake"

        # Evidence.
        self.events = []  # (t, source, text)
        self.packets = []  # Packet
        self.ingress = []  # (t, payload_text, assembler_key)
        self.sessions = []  # ClientSession
        self.handshake_failures = []  # fake time of each failed attempt
        self.snapshots = {}  # label -> [server session identity record]
        self._server_session_refs = {}  # locator -> weakref(LogicalSession)
        self._epoch_cache = {}  # locator -> {generation: (c2s, s2c) AESGCM}

    # ------------------------------------------------------------ scenario API

    def blackhole(self, start, end, direction="both"):
        self.blackholes.append((start, end, direction))

    def local_send_error(self, start, end, error=errno.ENETUNREACH):
        self.send_errors.append((start, end, error))

    def at(self, t, callback):
        self._scheduled.append([t, callback, False])

    def remap(self, t, new_addr):
        """NAT/CGNAT remapping: from `t` the server sees `new_addr`, and a
        reply to any other address is undeliverable."""

        def apply(lab):
            lab.log("net", f"external address {lab.external_addr} -> {new_addr}")
            lab.external_addr = new_addr

        self.at(t, apply)

    def snapshot_at(self, t, label):
        """At `t`, record the identity of every live server session: weak
        references to the `LogicalSession`, its current `CryptoEpoch` and
        that epoch's replay ledger, plus its assembly namespace, epoch
        generation and active path."""
        self.at(t, lambda lab: lab.snapshot(label))

    def snapshot(self, label):
        self.snapshots[label] = [
            {
                "locator": session._session_key.session_locator,
                "session": weakref.ref(session),
                "epoch": weakref.ref(session.current_epoch),
                "ledger": weakref.ref(session.current_epoch.seen_data_nonces),
                "assembly_namespace": session.assembly_namespace,
                "epoch_generation": session.current_epoch.generation,
                "active_path": session.path_state.active_path,
            }
            for session in self.state._sessions.values()
        ]

    def run_client(self, until, **config_overrides):
        """Run `nmea_sproxy.main()`'s UDPSEC relation loop, replicated
        around the real `perform_handshake()`, `forward_loop()` and
        `retry_delay_for_reason()`, until fake time `until`."""
        if self.config is not None:
            raise HarnessError("run_client() may only be called once per lab")
        config = dict(DEFAULT_CLIENT_CONFIG)
        config.update(config_overrides)
        self.config = config
        self.until = until
        self.monkeypatch.setattr(self.proxy.time, "monotonic", self.clock)
        self.monkeypatch.setattr(self.proxy.time, "time", self.clock.wall)
        self.monkeypatch.setattr(
            self.proxy.select,
            "select",
            lambda *args: self._guarded(self._fake_select, *args),
        )
        proxy = self.proxy
        try:
            while True:
                started = self.clock.now
                self._consumer = "handshake"
                confirmed = proxy.perform_handshake(
                    self.sock,
                    config,
                    self.station_private_key,
                    self.server_public_key,
                    SERVER_ADDR,
                )
                if confirmed:
                    session = ClientSession(
                        label=confirmed.session_locator.hex()[:8],
                        locator=confirmed.session_locator,
                        handshake_started=started,
                        confirmed_at=self.clock.now,
                        observed=confirmed.observed_endpoint,
                        path_gen=confirmed.active_path_generation,
                    )
                    self.sessions.append(session)
                    self._remember_server_session(confirmed.session_locator)
                    self.log("lab", f"client session {session.label} confirmed")
                    # main() clears one shared list per session; a list per
                    # session is equivalent for forward_loop() (it only
                    # publishes into it) and keeps each session's keys.
                    self._consumer = "forward_loop"
                    reason = proxy.forward_loop(
                        self.input,
                        self.sock,
                        config,
                        confirmed,
                        SERVER_ADDR,
                        None,
                        self.stats,
                        station_private_key=self.station_private_key,
                        server_identity_public_key=self.server_public_key,
                        epoch_state_ref=session.epoch_ref,
                    )
                    session.ended_at = self.clock.now
                    session.reason = reason
                    self.log("lab", f"client session {session.label} ended: {reason}")
                else:
                    reason = proxy.HANDSHAKE_FAILURE
                    self.handshake_failures.append(self.clock.now)
                retry_delay = proxy.retry_delay_for_reason(reason, config)
                if retry_delay is None:
                    self.log("main", "Refreshing secure session immediately.")
                    continue
                self.log("main", f"Retrying in {retry_delay} seconds...")
                self._guarded(self._advance_to, self.clock.now + retry_delay)
        except _StopScenario:
            if self.sessions and self.sessions[-1].reason is None:
                self.sessions[-1].ended_at = self.clock.now
                self.sessions[-1].reason = STILL_RUNNING
        except _HarnessAbort as abort:
            raise HarnessError(str(abort)) from abort
        self._maybe_write_trace()
        return self

    def inject(self, data, source_addr):
        """Feed one datagram straight to the real server loop (an off-path
        or replaying sender, never the client) and return its replies as
        (bytes, destination) pairs. Replies are recorded, not delivered."""
        replies = self._feed_server([(data, source_addr)])
        for reply, dest in replies:
            message, locator, generation, nonce = self.decode(reply)
            self.packets.append(
                Packet(
                    self.clock.now, "s2c", self._kind(message), message, dest,
                    reply, verdict="INJECTED-REPLY", locator=locator,
                    generation=generation, nonce=nonce,
                )
            )
        return replies

    # ------------------------------------------------------------ evidence

    def require(self, condition, message):
        """Harness precondition (fault really injected, ...): raises
        HarnessError, never AssertionError."""
        if not condition:
            raise HarnessError(f"{self.name}: {message}")

    def log(self, source, text):
        self.events.append((self.clock.now, source, text))

    def client_lines(self, needle):
        return [
            (t, text) for t, source, text in self.events
            if source == "client" and needle in text
        ]

    def sent(self, direction, kind=None, *, locator=None):
        """Packets offered in `direction`; `kind` matches exactly or, for a
        bare "ping"/"pong", any sequence number."""
        out = []
        for packet in self.packets:
            if packet.direction != direction or packet.verdict == "INJECTED-REPLY":
                continue
            if kind is not None and packet.kind != kind and packet.kind.split("#")[0] != kind:
                continue
            if locator is not None and packet.locator != locator:
                continue
            out.append(packet)
        return out

    def first_sent(self, direction, kind, *, locator=None):
        packets = self.sent(direction, kind, locator=locator)
        self.require(packets, f"no {direction} {kind} was ever sent")
        return packets[0]

    def hello_times(self):
        """Fake times of every ClientHello that actually left the host."""
        return [p.t for p in self.sent("c2s", "client_hello") if p.transmitted]

    def hello_attempt_times(self):
        """Every ClientHello send attempt, including local send errors."""
        return [p.t for p in self.sent("c2s", "client_hello")]

    def server_session(self, locator):
        for session in self.state._sessions.values():
            if session._session_key.session_locator == locator:
                return session
        return None

    def original_server_session(self):
        """The server `LogicalSession` object promoted for the FIRST client
        session, or None if it is gone (removed and collected)."""
        self.require(self.sessions, "no client session was ever confirmed")
        ref = self._server_session_refs.get(self.sessions[0].locator)
        self.require(ref is not None, "first server session was never observed")
        return ref()

    def delivered_at(self, marker):
        for t, payload, _key in self.ingress:
            if f",{marker}," in payload:
                return t
        return None

    def delivery_latencies(self):
        """(marker, due_at, delivered_at or None) for every produced line."""
        return [
            (marker, due_at, self.delivered_at(marker))
            for marker, due_at, _read_at in self.input.produced
        ]

    def nonce_reuse(self):
        """Transmitted DATA datagrams that reused an AEAD nonce within the
        same (direction, locator, epoch generation). Must stay empty."""
        seen = {}
        duplicates = []
        for packet in self.packets:
            if packet.nonce is None or not packet.transmitted:
                continue
            if packet.verdict == "INJECTED-REPLY":
                continue
            key = (packet.direction, packet.locator, packet.generation, packet.nonce)
            if key in seen:
                duplicates.append((seen[key], packet))
            else:
                seen[key] = packet
        return duplicates

    def trace_lines(self):
        merged = [(t, "EVENT", f"{source:<6} {text}") for t, source, text in self.events]
        for p in self.packets:
            line = f"{p.direction:>3} {p.kind:<16} addr={p.addr} -> {p.verdict}"
            if p.deliver_at is not None:
                line += f" deliver_at={p.deliver_at:.3f}"
            if p.consumed_by is not None:
                line += f" consumed_by={p.consumed_by}@{p.consumed_at:.3f}"
            merged.append((p.t, "PKT", line))
        merged += [(t, "INGRESS", payload[:28]) for t, payload, _key in self.ingress]
        merged.sort(key=lambda entry: entry[0])
        return [f"[t={t:10.3f}] {kind:<7} {text}" for t, kind, text in merged]

    def summary(self):
        return {
            "scenario": self.name,
            "end_time": self.clock.now,
            "client_sessions": [
                {
                    "label": s.label,
                    "handshake_started": s.handshake_started,
                    "confirmed_at": s.confirmed_at,
                    "observed": s.observed,
                    "path_gen": s.path_gen,
                    "ended_at": s.ended_at,
                    "reason": s.reason,
                }
                for s in self.sessions
            ],
            "handshake_failures": list(self.handshake_failures),
            "server_stats": dataclasses.asdict(self.state.stats()),
            "input_produced": len(self.input.produced),
            "ingress_frames": len(self.ingress),
        }

    # ------------------------------------------------------------ internals

    def _guarded(self, function, *args):
        """Run lab code that production is currently calling (fake socket,
        select, sleep). A deliberate local send error (OSError) and the end
        of the scenario pass through; any other exception -- a harness bug
        or a broken fault hook -- becomes `_HarnessAbort`, which production
        `except Exception` blocks cannot turn into a session outcome."""
        try:
            return function(*args)
        except OSError:
            raise
        except Exception as exc:
            raise _HarnessAbort(f"{self.name}: harness fault: {exc!r}") from exc

    def _client_print(self, *args, **_kwargs):
        self.log("client", " ".join(str(arg) for arg in args))

    def _server_print(self, *args, **_kwargs):
        text = " ".join(str(arg) for arg in args)
        if text.startswith("[+] Secure listener started"):
            return  # once per fed datagram in the one-datagram server model
        self.log("server", text)

    def _remember_server_session(self, locator):
        session = self.server_session(locator)
        if session is not None and locator not in self._server_session_refs:
            self._server_session_refs[locator] = weakref.ref(session)

    def _learn_epochs(self):
        sessions = list(self.state._sessions.values()) + list(
            self.state._pending_sessions.values()
        )
        for session in sessions:
            session_key = getattr(session, "_session_key", None)
            locator = (
                session_key.session_locator
                if session_key is not None else session.session_locator
            )
            epochs = [session.current_epoch]
            pending = getattr(session, "pending_epoch", None)
            if pending is not None:
                epochs.append(pending.epoch)
            retiring = getattr(session, "retiring_epoch", None)
            if retiring is not None:
                epochs.append(retiring)
            known = self._epoch_cache.setdefault(locator, {})
            for epoch in epochs:
                known[epoch.generation] = (
                    epoch.client_to_server_aesgcm,
                    epoch.server_to_client_aesgcm,
                )

    def decode(self, data):
        """Test-only classification of one wire datagram -- used for hooks
        and evidence, never as part of the protocol under test. Returns
        (message_or_None, locator, generation, nonce)."""
        if data.startswith(protocol.CLIENT_HELLO_PREFIX):
            return {"type": "client_hello"}, None, None, None
        if data.startswith(protocol.SERVER_HELLO_PREFIX + b"|"):
            return {"type": "server_hello"}, None, None, None
        try:
            locator, selector, nonce, ciphertext = protocol.parse_data_packet(data)
        except (TypeError, ValueError):
            return None, None, None, None
        self._learn_epochs()
        for generation, aeads in self._epoch_cache.get(locator, {}).items():
            if protocol.epoch_selector_for_generation(generation) != selector:
                continue
            for aead in aeads:
                try:
                    plaintext = aead.decrypt(
                        nonce, ciphertext, protocol.build_data_aad(locator, generation)
                    )
                    return json.loads(plaintext.decode()), locator, generation, nonce
                except Exception:
                    continue
        return {"type": "undecodable"}, locator, None, nonce

    @staticmethod
    def _kind(message):
        if message is None:
            return "malformed"
        kind = message.get("type") or "?"
        if kind in ("ping", "pong"):
            return f"{kind}#{message.get('seq')}"
        return kind

    def _in_blackhole(self, direction, t):
        return any(
            start <= t < end and dirs in ("both", direction)
            for start, end, dirs in self.blackholes
        )

    def _client_send(self, data, addr):
        if addr != SERVER_ADDR:
            raise HarnessError(f"client sent to an unpinned address {addr!r}")
        t = self.clock.now
        message, locator, generation, nonce = self.decode(data)
        packet = Packet(
            t, "c2s", self._kind(message), message, self.external_addr, data,
            locator=locator, generation=generation, nonce=nonce,
        )
        self.packets.append(packet)
        for start, end, error in self.send_errors:
            if start <= t < end:
                packet.verdict = f"LOCAL-ERROR:{errno.errorcode.get(error, error)}"
                text = "Network is unreachable" if error == errno.ENETUNREACH else os.strerror(error)
                raise OSError(error, text)
        if self._in_blackhole("c2s", t):
            packet.verdict = "DROP:blackhole"
            return
        if self.drop_c2s is not None and self.drop_c2s(message, t, self):
            packet.verdict = "DROP:hook"
            return
        hold = self.hold_c2s(message, t, self) if self.hold_c2s is not None else 0.0
        packet.deliver_at = t + self.delay + (hold or 0.0)
        self._seq += 1
        self._pending_c2s.append([packet.deliver_at, self._seq, data, self.external_addr])

    def _feed_server(self, datagrams):
        """Run the real `_secure_server_loop()` over `datagrams` at the
        current fake time; return its replies as (bytes, destination)."""
        fake_socket = _FakeSecureSocket()
        self._fake_asyncio._fake_loop = _FakeSecureLoop(list(datagrams))
        try:
            asyncio.run(
                self.secure._secure_server_loop(
                    fake_socket,
                    self.queue,
                    "::",
                    SERVER_ADDR[1],
                    endpoint_token=self.endpoint_token,
                    state=self.state,
                    wall_clock=self.clock.wall,
                    monotonic_clock=self.clock,
                    server_private_key=self.server_private_key,
                    owned_sessions=self.owned_sessions,
                    owned_pending_sessions=self.owned_pending,
                )
            )
        except asyncio.CancelledError:
            pass
        else:
            raise HarnessError("server loop returned before draining its datagrams")
        return fake_socket.sent

    def _server_send(self, data, dest):
        t = self.clock.now
        message, locator, generation, nonce = self.decode(data)
        packet = Packet(
            t, "s2c", self._kind(message), message, dest, data,
            locator=locator, generation=generation, nonce=nonce,
        )
        self.packets.append(packet)
        if self._in_blackhole("s2c", t):
            packet.verdict = "DROP:blackhole"
            return
        if self.drop_s2c is not None and self.drop_s2c(message, t, self):
            packet.verdict = "DROP:hook"
            return
        # A reply addressed to anything but the client's CURRENT external
        # mapping (an old NAT mapping or address) is undeliverable.
        if self.secure.normalize_sockaddr(dest) != self.secure.normalize_sockaddr(
            self.external_addr
        ):
            packet.verdict = "DROP:unreachable-old-path"
            return
        deliver_at = None
        if self.deliver_s2c_at is not None:
            deliver_at = self.deliver_s2c_at(message, t, self)
        if deliver_at is None:
            hold = self.hold_s2c(message, t, self) if self.hold_s2c is not None else 0.0
            deliver_at = t + self.delay + (hold or 0.0)
        if deliver_at < t:
            raise HarnessError("a datagram cannot arrive before it was sent")
        packet.deliver_at = deliver_at
        self._seq += 1
        self._inbox.append([deliver_at, self._seq, data, packet])

    def _pump(self):
        """Fire due scheduled actions, then run the real server over every
        client datagram whose delivery time has arrived, in order."""
        for entry in self._scheduled:
            if not entry[2] and self.clock.now >= entry[0]:
                entry[2] = True
                entry[1](self)
        while True:
            due = [e for e in self._pending_c2s if e[0] <= self.clock.now]
            if not due:
                return
            due.sort(key=lambda e: (e[0], e[1]))
            first = due[0]
            self._pending_c2s.remove(first)
            for reply, dest in self._feed_server([(first[2], first[3])]):
                self._server_send(reply, dest)

    def _inbox_due(self):
        return any(entry[0] <= self.clock.now for entry in self._inbox)

    def _pop_inbox(self):
        due = [entry for entry in self._inbox if entry[0] <= self.clock.now]
        due.sort(key=lambda entry: (entry[0], entry[1]))
        entry = due[0]
        self._inbox.remove(entry)
        entry[3].consumed_at = self.clock.now
        entry[3].consumed_by = self._consumer
        return entry[2]

    def _next_event_time(self, include_input=True):
        candidates = [e[0] for e in self._pending_c2s] + [e[0] for e in self._inbox]
        candidates += [e[0] for e in self._scheduled if not e[2]]
        if include_input and self.input.next_line_time() is not None:
            candidates.append(self.input.next_line_time())
        return min(candidates) if candidates else None

    def _advance_to(self, target):
        if self.until is not None and target >= self.until:
            self.clock.now = max(self.clock.now, self.until)
            self._pump()
            raise _StopScenario()
        if target > self.clock.now:
            self.clock.now = target
        self._pump()

    def _recv_blocking(self, timeout):
        """Blocking `recvfrom()` semantics, used by `perform_handshake()`."""
        deadline = self.clock.now + (CLIENT_SOCKET_TIMEOUT if timeout is None else timeout)
        while True:
            self._pump()
            if self._inbox_due():
                return self._pop_inbox()
            upcoming = self._next_event_time(include_input=False)
            if upcoming is None or upcoming > deadline:
                self._advance_to(deadline)
                if self._inbox_due():
                    return self._pop_inbox()
                return None
            self._advance_to(upcoming)

    def _fake_select(self, readable, writable, exceptional, timeout):
        self._polls += 1
        if self._polls > self.poll_limit:
            raise HarnessError(
                f"{self.name}: more than {self.poll_limit} forward_loop polls (busy loop?)"
            )
        self._pump()
        if self._inbox_due():
            return ([self.sock], [], [])
        target = self.clock.now + max(timeout, 0.0)
        upcoming = self._next_event_time()
        if upcoming is not None and upcoming < target:
            target = upcoming
        self._advance_to(target)
        if self._inbox_due():
            return ([self.sock], [], [])
        return ([], [], [])

    def _maybe_write_trace(self):
        directory = os.environ.get(TRACE_DIR_ENV)
        if not directory:
            return
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, f"{self.name}.log")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(f"# scenario {self.name}\n")
            handle.write("\n".join(self.trace_lines()) + "\n")
