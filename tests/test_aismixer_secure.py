import asyncio
import json
import weakref

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import aismixer_secure as secure
from core.network_policy import NetworkPolicy
from core.runtime_statistics import InputTrafficMetrics
from core.udpsec_protocol import ClientHello, build_client_hello_packet


PREPARED_SERVER_PRIVATE_KEY = ec.derive_private_key(29, ec.SECP256R1())


class _PacketLoop:
    def __init__(self, packets):
        self._packets = list(packets)

    async def sock_recvfrom(self, _sock, _size):
        if self._packets:
            return self._packets.pop(0)
        raise asyncio.CancelledError()


class _AsyncioWithLoop:
    def __init__(self, loop):
        self._loop = loop

    def get_running_loop(self):
        return self._loop


class _Socket:
    def __init__(self):
        self.bound = None
        self.blocking = None
        self.sent = []
        self.close_count = 0

    def bind(self, address):
        self.bound = address

    def setblocking(self, blocking):
        self.blocking = blocking

    def sendto(self, data, address):
        self.sent.append((data, address))

    def close(self):
        self.close_count += 1


class _OrderedSocket(_Socket):
    def __init__(self, *, send_error=None):
        super().__init__()
        self.events = []
        self.send_error = send_error

    def sendto(self, data, address):
        self.events.append(("send", address, self.close_count))
        if self.send_error is not None:
            raise self.send_error
        super().sendto(data, address)

    def close(self):
        self.events.append(("close", self.close_count))
        super().close()


class _Queue:
    def __init__(self):
        self.items = []

    async def put(self, frame):
        self.items.append(frame)


def _relation_key(endpoint_token, address):
    return secure._EndpointPeerKey(endpoint_token, address)


def _install_session(
    state,
    endpoint_token,
    address,
    client_key,
    server_key,
):
    state.install_session(
        _relation_key(endpoint_token, address),
        "boat_001",
        AESGCM(client_key),
        AESGCM(server_key),
        now=1000.0,
    )


def _secure_data_packet(key, nonce, message):
    plaintext = json.dumps(message).encode()
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, secure.DATA_AAD)
    return secure.DATA_PREFIX + nonce + ciphertext


def _decrypt_server_message(packet, server_to_client_key):
    nonce, ciphertext = secure.parse_secure_data_packet(packet)
    plaintext = AESGCM(server_to_client_key).decrypt(
        nonce,
        ciphertext,
        secure.DATA_AAD,
    )
    return json.loads(plaintext.decode())


def _install_owned_session(
    state,
    owned_sessions,
    endpoint_token,
    address,
    *,
    station_id="boat_001",
    client_key=b"\x01" * 32,
    server_key=b"\x02" * 32,
    now=1000.0,
):
    relation_key = _relation_key(endpoint_token, address)
    session = state.install_session(
        relation_key,
        station_id,
        AESGCM(client_key),
        AESGCM(server_key),
        now=now,
    )
    owned_sessions[relation_key] = session
    return session


def _run_packets(
    monkeypatch,
    packets,
    *,
    queue,
    traffic,
    state=None,
    ingress_policy=None,
    endpoint_token=None,
):
    fake_socket = _Socket()
    fake_loop = _PacketLoop(packets)
    monkeypatch.setattr(secure, "asyncio", _AsyncioWithLoop(fake_loop))
    monkeypatch.setattr(secure, "DEBUG", False)
    endpoint_token = (
        secure._new_endpoint_token()
        if endpoint_token is None
        else endpoint_token
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            secure._secure_server_loop(
                fake_socket,
                queue,
                "127.0.0.1",
                9999,
                ingress_policy=ingress_policy,
                endpoint_token=endpoint_token,
                input_traffic=traffic,
                state=secure.SecureState() if state is None else state,
                wall_clock=lambda: 1010.0,
                monotonic_clock=lambda: 1010.0,
                server_private_key=PREPARED_SERVER_PRIVATE_KEY,
            )
        )

    return fake_socket


def test_secure_server_passes_input_owner_to_owned_receive_loop(monkeypatch):
    fake_socket = _Socket()
    loop_calls = []
    traffic = InputTrafficMetrics("udpsec-ingress:0:secure", "udpsec")

    def create_socket(listen_ip, *, reuse_address):
        assert listen_ip == "127.0.0.1"
        assert reuse_address is False
        return fake_socket

    async def capture_loop(*args, **kwargs):
        loop_calls.append((args, kwargs))

    monkeypatch.setattr(secure, "create_udp_listener_socket", create_socket)
    monkeypatch.setattr(secure, "_secure_server_loop", capture_loop)

    asyncio.run(
        secure.secure_server(
            _Queue(),
            "127.0.0.1",
            9999,
            input_traffic=traffic,
            server_private_key=PREPARED_SERVER_PRIVATE_KEY,
        )
    )

    assert len(loop_calls) == 1
    assert loop_calls[0][0][0] is fake_socket
    assert loop_calls[0][1]["input_traffic"] is traffic
    assert (
        loop_calls[0][1]["server_private_key"]
        is PREPARED_SERVER_PRIVATE_KEY
    )
    assert loop_calls[0][1]["endpoint_token"] is not None
    assert isinstance(
        loop_calls[0][1]["owned_sessions"],
        weakref.WeakValueDictionary,
    )
    assert isinstance(
        loop_calls[0][1]["owned_pending_sessions"],
        weakref.WeakValueDictionary,
    )
    assert (
        loop_calls[0][1]["owned_pending_sessions"]
        is not loop_calls[0][1]["owned_sessions"]
    )
    assert fake_socket.close_count == 1


def test_secure_server_uses_fresh_endpoint_token_per_socket_incarnation(
    monkeypatch,
):
    sockets = [_Socket(), _Socket()]
    endpoint_tokens = []

    def create_socket(listen_ip, *, reuse_address):
        assert listen_ip == "127.0.0.1"
        assert reuse_address is False
        return sockets[len(endpoint_tokens)]

    async def capture_loop(*_args, **kwargs):
        endpoint_tokens.append(kwargs["endpoint_token"])

    monkeypatch.setattr(secure, "create_udp_listener_socket", create_socket)
    monkeypatch.setattr(secure, "_secure_server_loop", capture_loop)

    for _ in sockets:
        asyncio.run(
            secure.secure_server(
                _Queue(),
                "127.0.0.1",
                9999,
                server_private_key=PREPARED_SERVER_PRIVATE_KEY,
            )
        )

    assert endpoint_tokens[0] is not endpoint_tokens[1]
    assert [sock.close_count for sock in sockets] == [1, 1]


def test_secure_server_runtime_failure_sends_owned_close_before_socket_close(
    monkeypatch,
):
    address = ("127.0.0.1", 50123)
    server_key = b"\x12" * 32
    runtime_failure = RuntimeError("secure receive loop failed")
    state = secure.SecureState()
    fake_socket = _OrderedSocket()

    def create_socket(listen_ip, *, reuse_address):
        assert listen_ip == "127.0.0.1"
        assert reuse_address is False
        return fake_socket

    async def fail_after_session(*_args, **kwargs):
        _install_owned_session(
            kwargs["state"],
            kwargs["owned_sessions"],
            kwargs["endpoint_token"],
            address,
            server_key=server_key,
        )
        raise runtime_failure

    monkeypatch.setattr(secure, "create_udp_listener_socket", create_socket)
    monkeypatch.setattr(secure, "_secure_server_loop", fail_after_session)

    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(
            secure.secure_server(
                _Queue(),
                "127.0.0.1",
                9999,
                state=state,
                wall_clock=lambda: 1010.0,
                monotonic_clock=lambda: 1010.0,
                server_private_key=PREPARED_SERVER_PRIVATE_KEY,
            )
        )

    assert excinfo.value is runtime_failure
    assert fake_socket.events == [
        ("send", address, 0),
        ("close", 0),
    ]
    assert fake_socket.close_count == 1
    assert len(fake_socket.sent) == 1
    packet, destination = fake_socket.sent[0]
    assert destination == address
    assert _decrypt_server_message(packet, server_key) == (
        secure.build_session_close_message("boat_001", 1010)
    )
    stats = state.stats()
    assert stats.sessions_closed == 1
    assert stats.current_sessions == 0


def test_secure_server_cancellation_sends_owned_close_before_socket_close(
    monkeypatch,
):
    address = ("127.0.0.1", 50124)
    server_key = b"\x22" * 32
    state = secure.SecureState()
    fake_socket = _OrderedSocket()

    monkeypatch.setattr(
        secure,
        "create_udp_listener_socket",
        lambda _listen_ip, *, reuse_address: fake_socket,
    )

    async def scenario():
        started = asyncio.Event()

        async def wait_after_session(*_args, **kwargs):
            _install_owned_session(
                kwargs["state"],
                kwargs["owned_sessions"],
                kwargs["endpoint_token"],
                address,
                server_key=server_key,
            )
            started.set()
            await asyncio.Future()

        monkeypatch.setattr(
            secure,
            "_secure_server_loop",
            wait_after_session,
        )
        task = asyncio.create_task(
            secure.secure_server(
                _Queue(),
                "127.0.0.1",
                9999,
                state=state,
                wall_clock=lambda: 1010.0,
                monotonic_clock=lambda: 1010.0,
                server_private_key=PREPARED_SERVER_PRIVATE_KEY,
            )
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    assert fake_socket.events == [
        ("send", address, 0),
        ("close", 0),
    ]
    assert fake_socket.close_count == 1
    assert len(fake_socket.sent) == 1
    packet, destination = fake_socket.sent[0]
    assert destination == address
    assert _decrypt_server_message(packet, server_key) == (
        secure.build_session_close_message("boat_001", 1010)
    )
    assert state.stats().sessions_closed == 1
    assert state.stats().current_sessions == 0


def test_close_owned_sessions_ignores_stale_replaced_handle():
    address = ("127.0.0.1", 50125)
    unrelated_address = ("127.0.0.1", 50199)
    endpoint_token = secure._new_endpoint_token()
    relation_key = _relation_key(endpoint_token, address)
    unrelated_relation_key = _relation_key(
        endpoint_token,
        unrelated_address,
    )
    state = secure.SecureState(session_ttl=5.0)
    state.install_session(
        unrelated_relation_key,
        "unrelated",
        AESGCM(b"\x31" * 32),
        AESGCM(b"\x32" * 32),
        now=0.0,
    )
    owned_sessions = weakref.WeakValueDictionary()
    old_session = _install_owned_session(
        state,
        owned_sessions,
        endpoint_token,
        address,
        server_key=b"\x32" * 32,
        now=1.0,
    )
    replacement = state.install_session(
        relation_key,
        "boat_001",
        AESGCM(b"\x41" * 32),
        AESGCM(b"\x42" * 32),
        now=4.0,
    )
    fake_socket = _OrderedSocket()

    secure.close_owned_sessions(
        fake_socket,
        state,
        owned_sessions,
        wall_clock=lambda: 1010.0,
        monotonic_clock=lambda: 5.0,
    )

    assert old_session is not replacement
    assert state._sessions[relation_key] is replacement
    assert unrelated_relation_key in state._sessions
    assert fake_socket.sent == []
    assert fake_socket.events == []
    stats = state.stats()
    assert stats.sessions_replaced == 1
    assert stats.sessions_expired == 0
    assert stats.sessions_closed == 0
    assert stats.current_sessions == 2


def test_shared_state_listener_close_sends_only_exact_owned_session():
    address = ("127.0.0.1", 50126)
    first_endpoint_token = secure._new_endpoint_token()
    second_endpoint_token = secure._new_endpoint_token()
    first_relation_key = _relation_key(first_endpoint_token, address)
    second_relation_key = _relation_key(second_endpoint_token, address)
    first_server_key = b"\x52" * 32
    second_server_key = b"\x62" * 32
    state = secure.SecureState()
    first_owned = weakref.WeakValueDictionary()
    second_owned = weakref.WeakValueDictionary()
    first_session = _install_owned_session(
        state,
        first_owned,
        first_endpoint_token,
        address,
        station_id="boat_first",
        client_key=b"\x51" * 32,
        server_key=first_server_key,
    )
    second_session = _install_owned_session(
        state,
        second_owned,
        second_endpoint_token,
        address,
        station_id="boat_second",
        client_key=b"\x61" * 32,
        server_key=second_server_key,
    )
    first_socket = _OrderedSocket()
    second_socket = _OrderedSocket()

    secure.close_owned_sessions(
        first_socket,
        state,
        first_owned,
        wall_clock=lambda: 1010.0,
        monotonic_clock=lambda: 1010.0,
    )

    assert len(first_socket.sent) == 1
    first_packet, first_destination = first_socket.sent[0]
    assert first_destination == address
    assert _decrypt_server_message(first_packet, first_server_key) == (
        secure.build_session_close_message("boat_first", 1010)
    )
    assert second_socket.sent == []
    assert state.get_active_session(first_relation_key, 1010.0) is None
    assert (
        state.get_active_session(second_relation_key, 1010.0)
        is second_session
    )
    assert first_session is not second_session
    assert state.stats().sessions_closed == 1
    assert state.stats().current_sessions == 1


def test_secure_server_close_send_failure_is_best_effort_and_closes_socket(
    monkeypatch,
):
    addresses = (
        ("127.0.0.1", 50128),
        ("127.0.0.1", 50129),
    )
    state = secure.SecureState()
    send_error = OSError("simulated close send failure")
    fake_socket = _OrderedSocket(send_error=send_error)

    monkeypatch.setattr(
        secure,
        "create_udp_listener_socket",
        lambda _listen_ip, *, reuse_address: fake_socket,
    )

    async def return_after_sessions(*_args, **kwargs):
        for index, address in enumerate(addresses, start=1):
            _install_owned_session(
                kwargs["state"],
                kwargs["owned_sessions"],
                kwargs["endpoint_token"],
                address,
                station_id=f"boat_{index}",
                client_key=bytes((index,)) * 32,
                server_key=bytes((index + 10,)) * 32,
            )

    monkeypatch.setattr(
        secure,
        "_secure_server_loop",
        return_after_sessions,
    )

    asyncio.run(
        secure.secure_server(
            _Queue(),
            "127.0.0.1",
            9999,
            state=state,
            wall_clock=lambda: 1010.0,
            monotonic_clock=lambda: 1010.0,
            server_private_key=PREPARED_SERVER_PRIVATE_KEY,
        )
    )

    assert fake_socket.sent == []
    assert fake_socket.events == [
        ("send", addresses[0], 0),
        ("send", addresses[1], 0),
        ("close", 0),
    ]
    assert fake_socket.close_count == 1
    assert state.stats().sessions_closed == 2
    assert state.stats().current_sessions == 0


def test_udpsec_handshake_ping_replay_and_rejected_data_are_transport_only(
    monkeypatch,
):
    address = ("127.0.0.1", 50123)
    client_key = b"\x01" * 32
    server_key = b"\x02" * 32
    wrong_key = b"\x03" * 32
    state = secure.SecureState()
    endpoint_token = secure._new_endpoint_token()
    _install_session(
        state,
        endpoint_token,
        address,
        client_key,
        server_key,
    )

    malformed_handshake = secure.CLIENT_HELLO_PREFIX + b"malformed"
    ping = _secure_data_packet(
        client_key,
        b"\x10" * 12,
        {
            "type": "ping",
            "seq": 1,
            "timestamp": 1000,
            "source_id": "boat_001",
        },
    )
    rejected = _secure_data_packet(
        wrong_key,
        b"\x11" * 12,
        {
            "type": "nmea",
            "payload": "rejected",
            "timestamp": 1000,
            "source_id": "boat_001",
        },
    )
    packets = [
        (malformed_handshake, address),
        (ping, address),
        (ping, address),
        (rejected, address),
    ]
    traffic = InputTrafficMetrics("udpsec-ingress:0:secure", "udpsec")
    queue = _Queue()

    fake_socket = _run_packets(
        monkeypatch,
        packets,
        queue=queue,
        traffic=traffic,
        state=state,
        endpoint_token=endpoint_token,
    )

    snapshot = traffic.input_traffic_snapshot()
    assert snapshot.transport_packets == len(packets)
    assert snapshot.transport_bytes == sum(
        len(packet) for packet, _address in packets
    )
    assert snapshot.accepted_frames == 0
    assert snapshot.payload_bytes == 0
    assert queue.items == []
    assert len(fake_socket.sent) == 1


def test_udpsec_successful_handshake_is_transport_only(monkeypatch):
    address = ("127.0.0.1", 50123)
    station_id = "traffic_test_station"
    timestamp = 1010
    identity_private_key = ec.generate_private_key(ec.SECP256R1())
    ephemeral_private_key = ec.generate_private_key(ec.SECP256R1())
    client_random = b"\x40" * 32
    client_ephemeral_public_key = secure.serialize_ephemeral_public_key(
        ephemeral_private_key.public_key()
    )
    client_auth_digest = secure.build_client_auth_digest(
        station_id=station_id,
        timestamp=timestamp,
        client_random=client_random,
        client_ephemeral_public_key=client_ephemeral_public_key,
    )
    packet = build_client_hello_packet(
        ClientHello(
            station_id=station_id,
            timestamp=timestamp,
            client_random=client_random,
            client_ephemeral_public_key=client_ephemeral_public_key,
            client_signature=secure.sign_transcript_digest(
                identity_private_key,
                client_auth_digest,
            ),
        )
    )
    monkeypatch.setitem(
        secure.AUTHORIZED_KEYS,
        station_id,
        identity_private_key.public_key(),
    )
    state = secure.SecureState()
    traffic = InputTrafficMetrics("udpsec-ingress:0:secure", "udpsec")
    queue = _Queue()

    fake_socket = _run_packets(
        monkeypatch,
        [(packet, address)],
        queue=queue,
        traffic=traffic,
        state=state,
    )

    snapshot = traffic.input_traffic_snapshot()
    assert snapshot.transport_packets == 1
    assert snapshot.transport_bytes == len(packet)
    assert snapshot.accepted_frames == 0
    assert snapshot.payload_bytes == 0
    assert queue.items == []
    assert len(fake_socket.sent) == 1
    assert state.stats().current_pending_sessions == 1


def test_udpsec_valid_nmea_accounts_admitted_frame_payload_bytes(monkeypatch):
    address = ("127.0.0.1", 50123)
    client_key = b"\x01" * 32
    server_key = b"\x02" * 32
    payload = " before\ud800after "
    state = secure.SecureState()
    endpoint_token = secure._new_endpoint_token()
    _install_session(
        state,
        endpoint_token,
        address,
        client_key,
        server_key,
    )
    packet = _secure_data_packet(
        client_key,
        b"\x20" * 12,
        {
            "type": "nmea",
            "payload": payload,
            "timestamp": 1000,
            "source_id": "boat_001",
        },
    )
    traffic = InputTrafficMetrics("udpsec-ingress:0:secure", "udpsec")
    queue = _Queue()

    _run_packets(
        monkeypatch,
        [(packet, address)],
        queue=queue,
        traffic=traffic,
        state=state,
        endpoint_token=endpoint_token,
    )

    assert len(queue.items) == 1
    frame = queue.items[0]
    assert frame.payload == payload.encode("utf-8", errors="surrogatepass")
    snapshot = traffic.input_traffic_snapshot()
    assert snapshot.transport_packets == 1
    assert snapshot.transport_bytes == len(packet)
    assert snapshot.accepted_frames == 1
    assert snapshot.payload_bytes == len(frame.payload)


def test_udpsec_policy_rejection_still_accounts_raw_transport(monkeypatch):
    address = ("192.0.2.10", 50123)
    packet = secure.DATA_PREFIX + b"denied-before-classification"
    traffic = InputTrafficMetrics("udpsec-ingress:0:secure", "udpsec")
    queue = _Queue()

    fake_socket = _run_packets(
        monkeypatch,
        [(packet, address)],
        queue=queue,
        traffic=traffic,
        ingress_policy=NetworkPolicy.deny_all(),
    )

    snapshot = traffic.input_traffic_snapshot()
    assert snapshot.transport_packets == 1
    assert snapshot.transport_bytes == len(packet)
    assert snapshot.accepted_frames == 0
    assert snapshot.payload_bytes == 0
    assert queue.items == []
    assert fake_socket.sent == []


@pytest.mark.parametrize(
    "queue_exit",
    [
        pytest.param(RuntimeError("queue failed"), id="failed"),
        pytest.param(asyncio.CancelledError(), id="cancelled"),
    ],
)
def test_udpsec_queue_exit_does_not_account_frame_as_accepted(
    monkeypatch,
    queue_exit,
):
    address = ("127.0.0.1", 50123)
    client_key = b"\x01" * 32
    server_key = b"\x02" * 32
    state = secure.SecureState()
    endpoint_token = secure._new_endpoint_token()
    _install_session(
        state,
        endpoint_token,
        address,
        client_key,
        server_key,
    )
    packet = _secure_data_packet(
        client_key,
        b"\x30" * 12,
        {
            "type": "nmea",
            "payload": "not admitted",
            "timestamp": 1000,
            "source_id": "boat_001",
        },
    )
    traffic = InputTrafficMetrics("udpsec-ingress:0:secure", "udpsec")

    class ExitingQueue:
        async def put(self, _frame):
            raise queue_exit

    _run_packets(
        monkeypatch,
        [(packet, address)],
        queue=ExitingQueue(),
        traffic=traffic,
        state=state,
        endpoint_token=endpoint_token,
    )

    snapshot = traffic.input_traffic_snapshot()
    assert snapshot.transport_packets == 1
    assert snapshot.transport_bytes == len(packet)
    assert snapshot.accepted_frames == 0
    assert snapshot.payload_bytes == 0
