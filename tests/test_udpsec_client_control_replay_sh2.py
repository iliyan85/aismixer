"""SH-2 reproductions retained as SH-3 client-authority regressions.

Real forward_loop, AES-GCM framing and signed refresh transactions; fake
transport/monotonic time only. E1/E2 below are wire generations 0/1.
Historical SH-2 accepted retiring CLOSE and E1 PONG for an E2-only PING,
with two strict XFAIL target assertions. SH-3 changes only those expected
outcomes to rejection and converts both target assertions to ordinary tests.
Original inputs, transport, timings and A-02 behavior are preserved.
"""

import base64
import errno
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import core.udpsec_protocol as protocol
from test_udpsec_client_path_migration import _FakeInput
from test_udpsec_refresh import _ClientServerRefresh, env  # noqa: F401


STATION_ID = "boat_001"
TOKEN = b"T" * 32


class _StopLoop(BaseException):
    pass


class _HarnessFault(BaseException):
    """Cannot be swallowed as a production socket_error inside fake I/O."""


def _proof_snapshot(migration):
    proof = migration._pending
    return (
        migration._watermark_generation,
        migration._watermark_token,
        proof,
        None if proof is None else tuple(
            getattr(proof, field) for field in proof.__slots__
        ),
    )


class _ControlLab(_ClientServerRefresh):
    """Reuse the refresh harness, adding timed delivery and passive recording.

    Only clocks, transport, and observation hooks are replaced. Admission,
    epoch commit, ping resolution, and deadline decisions delegate unchanged.
    All events, packet queues, and the poll count have finite test bounds.
    """

    def __init__(self, env, monkeypatch, *, keepalive=1000, refresh=6,
                 stop_at=111.5, peer_timeout=1000):
        super().__init__(env, now=100.0)
        self.e1_c2s = self.confirmed.key_material.client_to_server_key
        self.e1_s2c = self.confirmed.key_material.server_to_client_key
        self.epoch_ref = []
        self.events = []
        self.incoming = []
        self.received = []
        self.outgoing = []
        self.observations = []
        self.migration = None
        self.on_send = lambda message: None
        self.stop_at = stop_at
        self.polls = 0
        self.config = {
            "station_id": STATION_ID,
            "keepalive_interval": keepalive,
            "peer_timeout": peer_timeout,
            "session_refresh_interval": refresh,
            "reconnect_delay": 1,
        }
        proxy = self.proxy
        real_terminal = proxy.terminal_deadline_reason
        real_migration = proxy._ClientPathMigration

        def observe(now, started, last_peer, expected_seq, config, supported):
            self.observations.append({
                "now": now, "received": len(self.received),
                "last_peer": last_peer, "expected_seq": expected_seq,
                "proof": _proof_snapshot(self.migration),
            })
            return real_terminal(
                now, started, last_peer, expected_seq, config, supported
            )

        def capture_migration():
            self.migration = real_migration()
            return self.migration

        monkeypatch.setattr(proxy, "terminal_deadline_reason", observe)
        monkeypatch.setattr(proxy, "_ClientPathMigration", capture_migration)
        monkeypatch.setattr(proxy.time, "monotonic", self.clock)
        monkeypatch.setattr(proxy.time, "time", lambda: 1000)
        monkeypatch.setattr(proxy.select, "select", self.select)

    @property
    def live(self):
        return self.epoch_ref[0]

    def schedule(self, at, action):
        self.events.append((at, action))
        self.events.sort(key=lambda event: event[0])

    def inject(self, packet, addr=None):
        self.incoming.append((packet, self.remote if addr is None else addr))

    def packet(self, message, *, generation=0, key=None, locator=None):
        if key is None:
            key = self.e1_s2c if generation == 0 else self.live.server_to_client_key
        return self.proxy.encrypt_secure_json_message(
            message, key, self.locator if locator is None else locator, generation
        )

    def close_packet(self, **kwargs):
        return self.packet(protocol.build_session_close_message(STATION_ID, 1000),
                           **kwargs)

    def challenge_packet(self, *, generation=2, token=TOKEN, **kwargs):
        return self.packet(protocol.build_path_challenge_message(
            station_id=STATION_ID, challenge_token=token,
            path_generation=generation, timestamp=1000,
        ), **kwargs)

    def ack_packet(self):
        return self.packet(protocol.build_path_ack_message(
            station_id=STATION_ID, challenge_token=TOKEN,
            path_generation=2, timestamp=1000,
        ))

    def sendto(self, packet, addr):
        try:
            assert addr == self.remote
            message, epoch, _role, _nonce = self._decrypt_client(packet)
            assert message is not None, "outbound DATA must authenticate"
            self.outgoing.append((self.now, epoch.generation, packet, message))
            super().sendto(packet, addr)
            self.on_send(message)
        except Exception as exc:
            raise _HarnessFault("fake send failed") from exc

    def recvfrom(self, _size):
        try:
            item = self.incoming.pop(0) if self.incoming else (
                self.server_inbox.pop(0), self.remote
            )
            self.received.append((self.now, *item))
            return item
        except Exception as exc:
            raise _HarnessFault("fake receive failed") from exc

    def select(self, readable, _writable, _exceptional, timeout):
        try:
            self.polls += 1
            assert self.polls < 500, "finite SH-2 event script did not finish"
            if not self.incoming and not self.server_inbox:
                next_event = self.events[0][0] if self.events else self.stop_at
                self.now = min(self.now + timeout, next_event, self.stop_at)
                while self.events and self.events[0][0] <= self.now:
                    _at, action = self.events.pop(0)
                    action()
            if self.incoming or self.server_inbox:
                assert self in readable
                return [self], [], []
            if self.now >= self.stop_at:
                raise _StopLoop()
            return [], [], []
        except Exception as exc:
            raise _HarnessFault("fake select/event failed") from exc

    def run(self):
        try:
            return self.proxy.forward_loop(
                _FakeInput(), self, self.config, self.confirmed, self.remote,
                stats=self.proxy.ForwardingStats(),
                station_private_key=self.env.station_private_key,
                server_identity_public_key=self.env.server_public_key,
                epoch_state_ref=self.epoch_ref,
            )
        except _StopLoop:
            return "stopped_by_test"

    def after_receipt(self, count):
        return next(item for item in self.observations
                    if item["received"] == count)

    def responses(self):
        return [item for item in self.outgoing
                if item[3]["type"] == protocol.PATH_RESPONSE_TYPE]

    def shutdown_server(self):
        self.shutdown_generation = self.session.current_epoch.generation
        self.shutdown_packets = []

        def capture(packet, addr):
            assert addr == self.session.path_state.active_path
            self.shutdown_packets.append(packet)
            self.inject(packet)  # reversed direction: source is the pinned server

        self.secure.close_owned_sessions(
            SimpleNamespace(sendto=capture), self.state,
            {self.session._session_key: self.session},
            wall_clock=lambda: 1000, monotonic_clock=self.clock,
        )
        # close_owned_sessions is best effort and catches send failures.
        assert len(self.shutdown_packets) == 1


def _run_close(env, monkeypatch, case):
    lab = _ControlLab(env, monkeypatch)
    if case == "pending":
        lab.drop_client = lambda kind, _index: kind == "refresh_confirm"

        def on_confirm(message):
            if message["type"] == "refresh_confirm":
                assert lab.live.generation == lab.session.current_epoch.generation == 0
                assert lab.live.pending_generation == 1
                lab.inject(lab.close_packet(generation=1, key=lab.live._pending[2]))

        lab.on_send = on_confirm
    else:
        when = {"retiring": 107.0, "cutoff": 111.0,
                "expired": 111.25, "current": 107.0}[case]
        generation = 1 if case == "current" else 0

        def deliver():
            assert lab.live.generation == lab.session.current_epoch.generation == 1
            assert lab.live._retiring_until == 111.0
            lab.inject(lab.close_packet(generation=generation))

        lab.schedule(when, deliver)
    reason = lab.run()
    return lab, reason


@pytest.mark.parametrize("case,closes", [
    # SH-2 accepted retiring CLOSE; SH-3 rejects it with the same input.
    ("retiring", False), ("cutoff", False), ("expired", False),
    ("current", True), ("pending", True),
])
def test_a01_close_epoch_authority(env, monkeypatch, case, closes):
    """A01-a..d: characterize actual forward_loop termination, not a helper."""
    lab, reason = _run_close(env, monkeypatch, case)
    assert reason == (lab.proxy.SESSION_END_PEER_GRACEFUL_CLOSE
                      if closes else "stopped_by_test")
    assert lab.live.generation == (0 if case == "pending" else 1)
    if not closes:
        assert lab.observations[-1]["last_peer"] == 106.0


@pytest.mark.parametrize("invalid", ["tuple", "key", "locator", "shape"])
def test_a01_close_negative_controls(env, monkeypatch, invalid):
    lab = _ControlLab(env, monkeypatch)

    def deliver():
        message = protocol.build_session_close_message(STATION_ID, 1000)
        if invalid == "shape":
            message["extra"] = True
        packet = lab.packet(
            message, key=lab.e1_c2s if invalid == "key" else lab.e1_s2c,
            locator=b"X" * 16 if invalid == "locator" else lab.locator,
        )
        lab.inject(packet, ("192.0.2.51", 19999) if invalid == "tuple" else None)

    lab.schedule(107, deliver)
    assert lab.run() == "stopped_by_test"
    assert lab.live.generation == 1
    assert lab.observations[-1]["last_peer"] == 106


@pytest.mark.parametrize("server_commits", [False, True], ids=["E1", "E2-ACK-lost"])
def test_a01_real_shutdown_while_client_has_pending_epoch(
    env, monkeypatch, server_commits,
):
    """Actual close sender: E2 current at server can still be pending at client."""
    lab = _ControlLab(env, monkeypatch)
    lab.drop_client = lambda kind, _index: (
        kind == "refresh_confirm" and not server_commits
    )
    lab.drop_server = lambda index: index == 2  # hold the committing ACK

    def on_confirm(message):
        if message["type"] == "refresh_confirm":
            assert lab.live.generation == 0 and lab.live.pending_generation == 1
            lab.shutdown_server()

    lab.on_send = on_confirm
    assert lab.run() == lab.proxy.SESSION_END_PEER_GRACEFUL_CLOSE
    assert lab.shutdown_generation == int(server_commits)
    assert protocol.parse_data_packet(lab.shutdown_packets[0])[1] == int(server_commits)
    assert lab.live.generation == 0 and lab.live.pending_generation == 1
    assert lab.state.stats().current_sessions == 0


def test_a01_retiring_close_must_not_end_refreshed_session(env, monkeypatch):
    lab, reason = _run_close(env, monkeypatch, "retiring")
    assert reason == "stopped_by_test"
    assert lab.live.generation == 1
    assert lab.observations[-1]["last_peer"] == 106


def test_a02_identical_challenge_burst_counts_bytes_and_preserves_state(
    env, monkeypatch, record_property,
):
    lab = _ControlLab(env, monkeypatch, refresh=0, peer_timeout=20, stop_at=123)
    packet = lab.challenge_packet()
    lab.schedule(101, lambda: [lab.inject(packet) for _ in range(32)])
    assert lab.run() == lab.proxy.SESSION_END_PEER_TIMEOUT
    assert lab.now == 120
    assert len(lab.received) == len(lab.responses()) == 32
    assert all(raw is packet and when == 101 for when, raw, _addr in lab.received)
    snapshots = [lab.after_receipt(i)["proof"] for i in range(1, 33)]
    assert all(snapshot == snapshots[0] for snapshot in snapshots)
    assert all(item["last_peer"] == 100 for item in lab.observations)
    responses = lab.responses()
    assert all(addr == lab.remote for _when, _raw, addr in lab.received)
    assert all(item[3]["path_generation"] == 2
               and base64.b64decode(item[3]["challenge_token"]) == TOKEN
               for item in responses)
    assert len({protocol.parse_data_packet(item[2])[2] for item in responses}) == 32
    metrics = {
        "input_packets": 32, "output_packets": len(responses),
        "input_bytes": sum(len(raw) for _when, raw, _addr in lab.received),
        "output_bytes": sum(len(item[2]) for item in responses),
    }
    assert metrics["input_bytes"] == 32 * len(packet)
    for name, value in metrics.items():
        record_property("A02_" + name, value)
    print("A-02 measured DATA datagrams:", metrics)


@pytest.mark.parametrize("state", ["consumed", "expired"])
def test_a02_late_identical_deliveries_do_not_rearm_or_renew_liveness(
    env, monkeypatch, state,
):
    lab = _ControlLab(env, monkeypatch, refresh=0, peer_timeout=20, stop_at=123)
    packet = lab.challenge_packet()
    lab.schedule(101, lambda: lab.inject(packet))
    if state == "consumed":
        lab.schedule(102, lambda: lab.inject(lab.ack_packet()))
    late_at = 103 if state == "consumed" else 111  # exact proof cutoff
    saved = []

    def late():
        saved.append(_proof_snapshot(lab.migration))
        assert lab.migration._pending.consumed == (state == "consumed")
        assert lab.migration._pending.deadline == 111
        for _ in range(3):
            lab.inject(packet)
        lab.inject(lab.ack_packet())  # no fresh authority after expiry/consumption

    lab.schedule(late_at, late)
    assert lab.run() == lab.proxy.SESSION_END_PEER_TIMEOUT
    assert len(lab.responses()) == 4
    assert _proof_snapshot(lab.migration) == saved[0]
    expected_peer = 102 if state == "consumed" else 100
    assert lab.now == expected_peer + 20
    assert lab.observations[-1]["last_peer"] == expected_peer


@pytest.mark.parametrize("invalid", ["tuple", "key", "noncurrent", "stale", "token"])
def test_a02_challenge_negative_controls(env, monkeypatch, invalid):
    lab = _ControlLab(env, monkeypatch, refresh=0)
    lab.schedule(101, lambda: lab.inject(lab.challenge_packet()))
    saved = []

    def deliver():
        saved.append(_proof_snapshot(lab.migration))
        pending_s2c = AESGCM.generate_key(bit_length=256)
        if invalid == "noncurrent":
            # A pending key installed in the real resolver still has no
            # path-control authority, even when its ciphertext is authentic.
            lab.live.set_pending(
                1, AESGCM.generate_key(bit_length=256), pending_s2c
            )
        message = protocol.build_path_challenge_message(
            station_id=STATION_ID, challenge_token=b"U" * 32 if invalid == "token" else TOKEN,
            path_generation=1 if invalid == "stale" else 2, timestamp=1000,
        )
        packet = lab.packet(
            message, generation=1 if invalid == "noncurrent" else 0,
            key=pending_s2c if invalid == "noncurrent" else (
                lab.e1_c2s if invalid == "key" else lab.e1_s2c
            ),
        )
        lab.inject(packet, ("192.0.2.51", 19999) if invalid == "tuple" else None)

    lab.schedule(102, deliver)
    assert lab.run() == "stopped_by_test"
    assert len(lab.responses()) == 1
    assert _proof_snapshot(lab.migration) == saved[0]
    assert lab.observations[-1]["last_peer"] == 100


def _run_pong(env, monkeypatch, case):
    cross_refresh = case in (
        "cross-refresh-retry", "cross-refresh-current", "cross-refresh-expired"
    )
    legacy_ping = case == "delayed-E1" or cross_refresh
    lab = _ControlLab(env, monkeypatch, keepalive=4 if legacy_ping else 8)
    arrival = 111 if case in ("cutoff", "cross-refresh-expired") else 107 if case == "delayed-E1" else 109
    generation = 1 if case in ("current", "cross-refresh-current") else 0
    captured_pongs = []

    def capture_e1_reply(message):
        if legacy_ping and message["type"] == "ping" and not captured_pongs:
            assert lab.outgoing[-1][1] == 0 and lab.now == 104
            # The controlled server emits this while E1 is current; only
            # its delivery is delayed. Never manufacture a new E1 reply
            # after E2 commit for these positive compatibility controls.
            captured_pongs.append(lab.packet(protocol.build_pong_message(
                STATION_ID, message["seq"], 1000
            )))

    lab.on_send = capture_e1_reply

    def deliver():
        assert lab.live.generation == lab.session.current_epoch.generation == 1
        pings = [item for item in lab.outgoing if item[3]["type"] == "ping"]
        assert pings and {item[3]["seq"] for item in pings} == {1}
        expected_generations = [0, 1] if cross_refresh else [0] if legacy_ping else [1]
        assert [item[1] for item in pings] == expected_generations
        assert len({protocol.parse_data_packet(item[2])[2] for item in pings}) == len(pings)
        if legacy_ping and generation == 0:
            assert len(captured_pongs) == 1
            lab.inject(captured_pongs[0])
        else:
            message = protocol.build_pong_message(
                STATION_ID, 2 if case == "wrong-sequence" else 1, 1000
            )
            lab.inject(lab.packet(message, generation=generation))

    lab.schedule(arrival, deliver)
    assert lab.run() == "stopped_by_test"
    # Two real refresh replies precede the one deliberately injected PONG.
    assert len(lab.received) == 3
    if legacy_ping and generation == 0:
        assert lab.received[-1][1] is captured_pongs[0]
    return lab, lab.after_receipt(3), arrival


@pytest.mark.parametrize("case,accepted", [
    # SH-2 accepted the first case; SH-3 changes its oracle, not its input.
    ("E2-only-retiring", False), ("cutoff", False), ("wrong-sequence", False),
    ("delayed-E1", True), ("cross-refresh-retry", True), ("current", True),
    ("cross-refresh-current", True),
    ("cross-refresh-expired", False),
])
def test_a03_pong_epoch_ownership(env, monkeypatch, case, accepted):
    lab, observed, arrival = _run_pong(env, monkeypatch, case)
    assert observed["last_peer"] == (arrival if accepted else 106)
    assert observed["expected_seq"] == (None if accepted else 1)
    assert lab.live.generation == 1


def test_a03_e2_only_ping_must_not_accept_retiring_pong(env, monkeypatch):
    _lab, observed, _arrival = _run_pong(env, monkeypatch, "E2-only-retiring")
    assert observed["last_peer"] == 106
    assert observed["expected_seq"] == 1


def _capture_ping_ownership(lab, monkeypatch):
    """Passively observe the real bounded owner; never replace its decisions."""
    real_owner = lab.proxy._ClientPingEpochs
    captured = SimpleNamespace(instances=[], records=[], clears=[])

    class ObservedOwner(real_owner):
        __slots__ = ()

        def record_success(self, generation, epochs, now):
            result = super().record_success(generation, epochs, now)
            live = {epochs.generation}
            if epochs._retiring_until is not None and now < epochs._retiring_until:
                live.add(epochs._retiring_generation)
            captured.records.append((now, generation, set(self._generations), live))
            return result

        def clear(self):
            result = super().clear()
            captured.clears.append(set(self._generations))
            return result

    def make_owner():
        owner = ObservedOwner()
        assert not owner._generations
        captured.instances.append(owner)
        return owner

    monkeypatch.setattr(lab.proxy, "_ClientPingEpochs", make_owner)
    return captured


def test_a03_pending_pong_has_no_authority_before_commit(env, monkeypatch):
    lab = _ControlLab(env, monkeypatch, keepalive=4, stop_at=107.5)
    ownership = _capture_ping_ownership(lab, monkeypatch)
    lab.drop_client = lambda kind, _index: kind == "refresh_confirm"
    captured_e1 = []
    pending_sent = []

    def on_send(message):
        if message["type"] == "ping":
            assert lab.now == 104 and lab.outgoing[-1][1] == 0
            captured_e1.append(lab.packet(protocol.build_pong_message(STATION_ID, 1, 1000)))
        elif message["type"] == "refresh_confirm" and not pending_sent:
            assert lab.live.generation == 0 and lab.live.pending_generation == 1
            pending_sent.append(lab.packet(
                protocol.build_pong_message(STATION_ID, 1, 1000),
                generation=1, key=lab.live._pending[2],
            ))
            # Distinct from the REPLY's t=106 evidence, so a liveness-only
            # admission bug cannot hide behind the same timestamp.
            lab.schedule(106.5, lambda: lab.inject(pending_sent[0]))

    lab.on_send = on_send
    lab.schedule(107, lambda: lab.inject(captured_e1[0]))
    assert lab.run() == "stopped_by_test"
    assert len(lab.received) == 3
    assert lab.after_receipt(2)["last_peer"] == 106
    assert lab.after_receipt(2)["expected_seq"] == 1
    assert lab.after_receipt(3)["last_peer"] == 107
    assert lab.after_receipt(3)["expected_seq"] is None
    assert lab.live.generation == 0 and lab.live.pending_generation == 1
    assert ownership.records[0][2] == {0}
    assert not ownership.instances[0]._generations


@pytest.mark.parametrize("failure", ["initial", "retry", "retry-with-E1-reply"])
def test_a03_failed_send_does_not_authorize_epoch_and_recovery_works(
    env, monkeypatch, failure,
):
    old_reply = failure == "retry-with-E1-reply"
    lab = _ControlLab(
        env, monkeypatch, keepalive=8 if failure == "initial" else 4,
        stop_at=110 if old_reply else 114,
    )
    ownership = _capture_ping_ownership(lab, monkeypatch)
    real_send = lab.proxy.send_ping
    attempts = []
    captured_e1 = []

    def send_ping(*args, **kwargs):
        generation = args[6]
        attempts.append((lab.now, args[5], generation))
        if lab.now == 108:
            raise OSError(errno.ENETUNREACH, "scripted pre-send failure")
        return real_send(*args, **kwargs)

    def on_send(message):
        if old_reply and message["type"] == "ping":
            assert lab.now == 104 and lab.outgoing[-1][1] == 0
            captured_e1.append(lab.packet(protocol.build_pong_message(STATION_ID, 1, 1000)))

    lab.on_send = on_send
    monkeypatch.setattr(lab.proxy, "send_ping", send_ping)
    # One real refresh suffices; later retries stay in E2 for this scenario.
    lab.schedule(107, lambda: lab.config.update(session_refresh_interval=0))
    lab.schedule(109, lambda: lab.inject(lab.packet(
        protocol.build_pong_message(STATION_ID, 1, 1000), generation=1,
    )))
    recovery_at = 109.25 if old_reply else 113.25
    lab.schedule(recovery_at, lambda: lab.inject(
        captured_e1[0] if old_reply else lab.packet(
            protocol.build_pong_message(STATION_ID, 1, 1000), generation=1,
        )
    ))
    assert lab.run() == "stopped_by_test"
    assert len(lab.received) == 4
    assert lab.after_receipt(3)["last_peer"] == 106
    assert lab.after_receipt(3)["expected_seq"] == 1
    assert lab.after_receipt(4)["last_peer"] == recovery_at
    assert lab.after_receipt(4)["expected_seq"] is None
    successful = [item for item in lab.outgoing if item[3]["type"] == "ping"]
    assert all(item[3]["seq"] == 1 for item in successful)
    expected_successes = [(104, 0)] if old_reply else (
        [(113, 1)] if failure == "initial" else [(104, 0), (112, 1)]
    )
    assert [(item[0], item[1]) for item in successful] == expected_successes
    assert attempts == (
        [(108, 1, 1), (113, 1, 1)] if failure == "initial" else
        [(104, 1, 0), (108, 1, 1)] + ([] if old_reply else [(112, 1, 1)])
    )
    assert len({protocol.parse_data_packet(item[2])[2] for item in successful}) == len(successful)
    assert all(now != 108 for now, _generation, _owned, _live in ownership.records)
    assert not ownership.instances[0]._generations


@pytest.mark.parametrize("resolution", ["pong", "path_ack"])
def test_a03_resolved_ping_ownership_cannot_authorize_the_next_sequence(
    env, monkeypatch, resolution,
):
    lab = _ControlLab(env, monkeypatch, keepalive=4)
    ownership = _capture_ping_ownership(lab, monkeypatch)
    cleared_between = []
    if resolution == "pong":
        lab.schedule(107, lambda: lab.inject(lab.packet(
            protocol.build_pong_message(STATION_ID, 1, 1000),
        )))
        resolved_count, resolved_at = 3, 107
    else:
        lab.schedule(107, lambda: lab.inject(lab.packet(
            protocol.build_path_challenge_message(
                station_id=STATION_ID, challenge_token=TOKEN,
                path_generation=2, timestamp=1000,
            ), generation=1,
        )))
        lab.schedule(107.25, lambda: lab.inject(lab.packet(
            protocol.build_path_ack_message(
                station_id=STATION_ID, challenge_token=TOKEN,
                path_generation=2, timestamp=1000,
            ), generation=1,
        )))
        resolved_count, resolved_at = 4, 107.25
    lab.schedule(107.5, lambda: cleared_between.append(
        set(ownership.instances[0]._generations)
    ))
    for at, sequence, generation in [(109, 2, 0), (109.25, 1, 1), (109.5, 2, 1)]:
        lab.schedule(at, lambda sequence=sequence, generation=generation: lab.inject(
            lab.packet(protocol.build_pong_message(STATION_ID, sequence, 1000),
                       generation=generation)
        ))
    assert lab.run() == "stopped_by_test"
    assert len(lab.received) == resolved_count + 3
    resolved = lab.after_receipt(resolved_count)
    assert resolved["last_peer"] == resolved_at and resolved["expected_seq"] is None
    for count in (resolved_count + 1, resolved_count + 2):
        ignored = lab.after_receipt(count)
        assert ignored["last_peer"] == resolved_at and ignored["expected_seq"] == 2
    recovered = lab.after_receipt(resolved_count + 3)
    assert recovered["last_peer"] == 109.5 and recovered["expected_seq"] is None
    assert cleared_between == [set()]
    assert not ownership.instances[0]._generations
    assert [(item[0], item[1], item[3]["seq"]) for item in lab.outgoing
            if item[3]["type"] == "ping"] == [(104, 0, 1), (108, 1, 2)]


def test_a03_unanswered_ping_ownership_is_bounded_across_real_refreshes(
    env, monkeypatch,
):
    lab = _ControlLab(env, monkeypatch, keepalive=4, stop_at=134)
    ownership = _capture_ping_ownership(lab, monkeypatch)
    assert lab.run() == "stopped_by_test"
    assert lab.live.generation == lab.session.current_epoch.generation == 5
    assert lab.state.stats().epoch_refreshes_committed == 5
    pings = [item for item in lab.outgoing if item[3]["type"] == "ping"]
    assert pings and {item[3]["seq"] for item in pings} == {1}
    assert {item[1] for item in pings} == set(range(6))
    assert len({protocol.parse_data_packet(item[2])[2] for item in pings}) == len(pings)
    assert ownership.records and len(ownership.instances) == 1
    assert all(len(owned) <= 2 and owned <= live
               for _now, _generation, owned, live in ownership.records)
    assert set(ownership.instances[0]._generations) <= {4, 5}
    assert lab.observations[-1]["expected_seq"] == 1
    assert lab.observations[-1]["last_peer"] == 130


def test_refresh_ack_wrong_transaction_is_ignored(env):
    """Optional small SH-1 gap: correct pending key, wrong transaction."""
    lab = _ClientServerRefresh(env, now=100)
    lab.start()
    assert lab.deliver(lab.server_inbox.pop(0)) == lab.proxy.SERVER_PACKET_REFRESH_PROGRESS
    assert lab.epochs.pending_generation == 1
    genuine = lab.server_inbox.pop(0)
    wrong = bytes(value ^ 0xFF for value in lab.refresh._txn)
    ack = protocol.build_refresh_ack_message(
        station_id=STATION_ID, transaction_id=wrong, next_generation=1, timestamp=1000,
    )
    forged = lab.proxy.encrypt_secure_json_message(
        ack, lab.epochs._pending[2], lab.locator, 1
    )
    assert lab.deliver(forged) is None
    assert lab.epochs.generation == 0 and lab.epochs.pending_generation == 1
    assert lab.deliver(genuine) == lab.proxy.SERVER_PACKET_REFRESH_COMMITTED
    assert lab.epochs.generation == 1
