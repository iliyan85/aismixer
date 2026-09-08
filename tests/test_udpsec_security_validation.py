import asyncio
import ast
import base64
import importlib.util
import io
import itertools
import json
import os
import queue
import socket
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import core.udpsec_crypto as udpsec_crypto
from core.ingress_frame import (
    IngressFrame,
    PayloadTextMode,
    decode_frame_slice,
)
from core.udpsec_crypto import SessionKeyMaterial
from core.udpsec_protocol import (
    ClientHello,
    SESSION_LOCATOR_BYTES,
    ServerHello,
    UDPSEC_PROTOCOL_VERSION,
    build_client_hello_packet,
    build_server_hello_packet,
    parse_client_hello_packet,
    parse_server_hello_packet,
)


ROOT = Path(__file__).resolve().parents[1]
NMEA_SPROXY_DIR = ROOT / "nmea_sproxy"
STATION_ID = "boat_001"
NETWORK_TIMEOUT = 3.0
NMEA_PAYLOAD = "!AIVDM,1,1,,A,13aG?P0000PD;88MD5MTDwvN0<0l,0*7D"


def _load_proxy_module():
    sys.path.insert(0, str(NMEA_SPROXY_DIR))
    try:
        spec = importlib.util.spec_from_file_location(
            "nmea_sproxy_security_validation",
            NMEA_SPROXY_DIR / "nmea_sproxy.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(NMEA_SPROXY_DIR))


def _load_secure_module(monkeypatch, server_private_key, station_public_key):
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
        name = os.path.basename(os.fspath(path))
        if name == "authorized_keys.yaml":
            return io.StringIO(authorized_yaml)
        return real_open(path, mode, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os.path, "exists", lambda _path: False)
        patch.setattr("builtins.open", fake_open)
        spec = importlib.util.spec_from_file_location(
            "aismixer_secure_security_validation",
            ROOT / "aismixer_secure.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


@pytest.fixture
def real_udpsec_endpoints(monkeypatch):
    server_private_key = ec.generate_private_key(ec.SECP256R1())
    station_private_key = ec.generate_private_key(ec.SECP256R1())
    secure = _load_secure_module(
        monkeypatch,
        server_private_key,
        station_private_key.public_key(),
    )
    proxy = _load_proxy_module()
    return SimpleNamespace(
        secure=secure,
        proxy=proxy,
        server_private_key=server_private_key,
        server_public_key=server_private_key.public_key(),
        station_private_key=station_private_key,
    )


class _ThreadSafeIngressSink:
    def __init__(self):
        self._items = queue.Queue()

    async def put(self, item):
        self._items.put(item)

    def get(self):
        return self._items.get(timeout=NETWORK_TIMEOUT)

    def empty(self):
        return self._items.empty()


class _ThreadSafeTrafficCounter:
    def __init__(self):
        self._lock = threading.Lock()
        self._received = 0
        self._accepted = 0

    def transport_received(self, _data):
        with self._lock:
            self._received += 1

    def frame_accepted(self, _payload):
        with self._lock:
            self._accepted += 1

    def snapshot(self):
        with self._lock:
            return self._received, self._accepted


class _IdleInput:
    def selectable_sockets(self):
        return []

    def poll_interval(self):
        return None

    def read_ready(self, _ready_socket):
        raise AssertionError("idle input must not become readable")

    def read_pending(self):
        return ()


class _LoopbackSecureServer:
    def __init__(
        self,
        secure,
        server_private_key,
        family,
        host,
        port=0,
        *,
        graceful_close=False,
        state=None,
        monotonic_clock=None,
    ):
        self.secure = secure
        self.server_private_key = server_private_key
        self.host = host
        self.port = port
        self.graceful_close = graceful_close
        self.monotonic_clock = monotonic_clock
        self.socket = socket.socket(family, socket.SOCK_DGRAM)
        self.state = secure.SecureState() if state is None else state
        self.endpoint_token = secure._new_endpoint_token()
        self.owned_sessions = {}
        self.owned_pending_sessions = {}
        self.ingress = _ThreadSafeIngressSink()
        self.traffic = _ThreadSafeTrafficCounter()
        self.remote_addr = None

        self._loop = None
        self._task = None
        self._thread = None
        self._ready = threading.Event()
        self._finished = threading.Event()
        self._error = None

    def _publish_ready(self):
        try:
            if self._task.done():
                self._task.result()
                raise RuntimeError("secure listener stopped before startup")
            address = self.socket.getsockname()
            if len(address) < 2 or address[1] == 0:
                raise RuntimeError("secure listener did not bind a UDP port")
            self.remote_addr = address
        except BaseException as exc:
            self._error = exc
        finally:
            self._ready.set()

    async def _supervise(self):
        self._task = asyncio.create_task(
            self.secure._secure_server_loop(
                self.socket,
                self.ingress,
                self.host,
                self.port,
                endpoint_token=self.endpoint_token,
                sec_input_id="loopback-validation",
                input_traffic=self.traffic,
                state=self.state,
                server_private_key=self.server_private_key,
                owned_sessions=self.owned_sessions,
                owned_pending_sessions=self.owned_pending_sessions,
                monotonic_clock=self.monotonic_clock,
            )
        )
        # create_task() and call_soon() are FIFO on this loop. The listener
        # therefore executes its synchronous bind before readiness is
        # published, then suspends in sock_recvfrom().
        asyncio.get_running_loop().call_soon(self._publish_ready)
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            if self.graceful_close:
                self.secure.close_owned_sessions(
                    self.socket,
                    self.state,
                    self.owned_sessions,
                )

    def _thread_main(self):
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._supervise())
        except BaseException as exc:
            if self._error is None:
                self._error = exc
            self._ready.set()
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            loop.close()
            asyncio.set_event_loop(None)
            self._finished.set()
            self._ready.set()

    def start(self):
        self._thread = threading.Thread(
            target=self._thread_main,
            name=f"udpsec-loopback-{self.host}",
            daemon=False,
        )
        self._thread.start()
        if not self._ready.wait(NETWORK_TIMEOUT):
            self.close()
            raise AssertionError("timed out waiting for secure listener startup")
        if self._error is not None:
            self.close()
            raise RuntimeError(
                "secure listener failed during startup"
            ) from self._error
        return self

    def call_in_loop(self, callback):
        results = queue.Queue()

        def invoke():
            try:
                results.put((True, callback()))
            except BaseException as exc:
                results.put((False, exc))

        self._loop.call_soon_threadsafe(invoke)
        succeeded, result = results.get(timeout=NETWORK_TIMEOUT)
        if not succeeded:
            raise result
        return result

    def relation_key(self, peer_address):
        return self.secure._EndpointPeerKey(
            self.endpoint_token,
            peer_address,
        )

    def active_session_for(self, peer_address):
        """The current active session at one relation, via the bounded
        relation index -- the active store itself is keyed by
        (endpoint_token, session_locator), not by peer address. Delegates
        to the production `_active_session_at_relation` method so this
        helper follows the same IPv6-flowinfo canonicalization production
        code applies, rather than duplicating a raw (and therefore
        flowinfo-sensitive) dict lookup."""

        return self.state._active_session_at_relation(
            self.relation_key(peer_address)
        )

    def close(self):
        loop = self._loop
        task = self._task
        if loop is not None and task is not None and not self._finished.is_set():
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass

        if not self._finished.wait(NETWORK_TIMEOUT):
            self.socket.close()
            if loop is not None and task is not None:
                try:
                    loop.call_soon_threadsafe(task.cancel)
                except RuntimeError:
                    pass

        if self._thread is not None:
            self._thread.join(NETWORK_TIMEOUT)
            if self._thread.is_alive():
                raise AssertionError("secure listener thread did not stop")

        self.socket.close()
        if self._error is not None:
            raise RuntimeError("secure listener failed") from self._error


@contextmanager
def _running_secure_server(
    secure,
    server_private_key,
    family,
    host,
    port=0,
    *,
    graceful_close=False,
    state=None,
    monotonic_clock=None,
):
    server = _LoopbackSecureServer(
        secure,
        server_private_key,
        family,
        host,
        port,
        graceful_close=graceful_close,
        state=state,
        monotonic_clock=monotonic_clock,
    )
    try:
        yield server.start()
    finally:
        server.close()


def _require_ipv6_loopback():
    if not socket.has_ipv6:
        pytest.skip("IPv6 loopback unavailable: Python reports no IPv6 support")

    probe = None
    try:
        probe = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        probe.bind(("::1", 0))
    except OSError as exc:
        pytest.skip(f"IPv6 loopback unavailable: {exc}")
    finally:
        if probe is not None:
            probe.close()


def _client_socket(family, host):
    sock = socket.socket(family, socket.SOCK_DGRAM)
    sock.bind((host, 0))
    sock.settimeout(NETWORK_TIMEOUT)
    return sock


class _OneShotDroppingSocket:
    def __init__(self, sock, drop):
        self._sock = sock
        self.drop = drop
        self.dropped = False
        self.client_hello_packets = []

    def sendto(self, data, address):
        if data.startswith(b"NMEA-H"):
            self.client_hello_packets.append(data)
        if (
            self.drop == "confirmation_ping"
            and data.startswith(b"NMEA-D")
            and not self.dropped
        ):
            self.dropped = True
            return len(data)
        return self._sock.sendto(data, address)

    def recvfrom(self, size):
        data, address = self._sock.recvfrom(size)
        should_drop = (
            self.drop == "server_hello"
            and data.startswith(b"OK|")
        ) or (
            self.drop == "confirmation_pong"
            and data.startswith(b"NMEA-D")
        )
        if should_drop and not self.dropped:
            self.dropped = True
            raise socket.timeout(f"intentionally dropped {self.drop}")
        return data, address

    def gettimeout(self):
        return self._sock.gettimeout()

    def settimeout(self, timeout):
        return self._sock.settimeout(timeout)

    def getsockname(self):
        return self._sock.getsockname()


def _nonce(marker):
    return marker.to_bytes(12, "big")


_TEST_LOCATOR_COUNTER = itertools.count(1)


def _fresh_test_locator():
    # A monotonic counter, not os.urandom: some tests monkeypatch/inspect
    # os.urandom call sequencing for client ephemeral/nonce generation and
    # must not have it perturbed by test-harness locator bookkeeping.
    return next(_TEST_LOCATOR_COUNTER).to_bytes(SESSION_LOCATOR_BYTES, "big")


def _encrypted_plaintext_packet(
    proxy,
    key,
    nonce,
    plaintext,
    session_locator,
    *,
    aad=None,
):
    associated_data = (
        proxy.build_data_aad(session_locator) if aad is None else aad
    )
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, associated_data)
    return proxy.build_data_packet(session_locator, nonce, ciphertext)


def _encrypted_json_packet(
    proxy, key, nonce, message, session_locator, *, aad=None
):
    return _encrypted_plaintext_packet(
        proxy,
        key,
        nonce,
        json.dumps(message, separators=(",", ":")).encode(),
        session_locator,
        aad=aad,
    )


def _signed_client_hello(
    endpoints,
    *,
    station_id=STATION_ID,
    timestamp=None,
    client_random=None,
    ephemeral_scalar=17,
    identity_private_key=None,
    protocol_version=None,
):
    if timestamp is None:
        timestamp = int(time.time())
    if client_random is None:
        client_random = b"\x31" * 32
    if identity_private_key is None:
        identity_private_key = endpoints.station_private_key
    if protocol_version is None:
        protocol_version = UDPSEC_PROTOCOL_VERSION
    ephemeral_private_key = ec.derive_private_key(
        ephemeral_scalar,
        ec.SECP256R1(),
    )
    ephemeral_public_key = udpsec_crypto.serialize_ephemeral_public_key(
        ephemeral_private_key.public_key()
    )
    digest = udpsec_crypto.build_client_auth_digest(
        protocol_version=protocol_version,
        station_id=station_id,
        timestamp=timestamp,
        client_random=client_random,
        client_ephemeral_public_key=ephemeral_public_key,
    )
    signature = udpsec_crypto.sign_transcript_digest(
        identity_private_key,
        digest,
    )
    hello = ClientHello(
        protocol_version=protocol_version,
        station_id=station_id,
        timestamp=timestamp,
        client_random=client_random,
        client_ephemeral_public_key=ephemeral_public_key,
        client_signature=signature,
    )
    return build_client_hello_packet(hello), hello, ephemeral_private_key


def _replace_wire_field(packet, index, replacement):
    fields = packet.split(b"|")
    fields[index] = replacement
    return b"|".join(fields)


def _compose_controlled_handshake(
    client_identity_private_key,
    server_identity_private_key,
    *,
    client_ephemeral_scalar,
    server_ephemeral_scalar,
    session_locator=None,
):
    station_id = STATION_ID
    timestamp = 1_700_000_000
    protocol_version = UDPSEC_PROTOCOL_VERSION
    client_random = b"\x51" * 32
    server_random = b"\x52" * 32
    if session_locator is None:
        session_locator = _fresh_test_locator()
    client_ephemeral_private_key = ec.derive_private_key(
        client_ephemeral_scalar,
        ec.SECP256R1(),
    )
    server_ephemeral_private_key = ec.derive_private_key(
        server_ephemeral_scalar,
        ec.SECP256R1(),
    )
    client_ephemeral_public_key = (
        udpsec_crypto.serialize_ephemeral_public_key(
            client_ephemeral_private_key.public_key()
        )
    )
    server_ephemeral_public_key = (
        udpsec_crypto.serialize_ephemeral_public_key(
            server_ephemeral_private_key.public_key()
        )
    )
    client_digest = udpsec_crypto.build_client_auth_digest(
        protocol_version=protocol_version,
        station_id=station_id,
        timestamp=timestamp,
        client_random=client_random,
        client_ephemeral_public_key=client_ephemeral_public_key,
    )
    client_signature = udpsec_crypto.sign_transcript_digest(
        client_identity_private_key,
        client_digest,
    )
    server_digest = udpsec_crypto.build_server_auth_digest(
        protocol_version=protocol_version,
        station_id=station_id,
        timestamp=timestamp,
        client_random=client_random,
        client_ephemeral_public_key=client_ephemeral_public_key,
        client_signature=client_signature,
        session_locator=session_locator,
        server_random=server_random,
        server_ephemeral_public_key=server_ephemeral_public_key,
    )
    server_signature = udpsec_crypto.sign_transcript_digest(
        server_identity_private_key,
        server_digest,
    )
    client_shared_secret = udpsec_crypto.derive_ephemeral_shared_secret(
        client_ephemeral_private_key,
        udpsec_crypto.parse_ephemeral_public_key(
            server_ephemeral_public_key
        ),
    )
    server_shared_secret = udpsec_crypto.derive_ephemeral_shared_secret(
        server_ephemeral_private_key,
        udpsec_crypto.parse_ephemeral_public_key(
            client_ephemeral_public_key
        ),
    )
    assert client_shared_secret == server_shared_secret
    transcript_hash = udpsec_crypto.build_session_transcript_hash(
        protocol_version=protocol_version,
        station_id=station_id,
        timestamp=timestamp,
        client_random=client_random,
        client_ephemeral_public_key=client_ephemeral_public_key,
        client_signature=client_signature,
        session_locator=session_locator,
        server_random=server_random,
        server_ephemeral_public_key=server_ephemeral_public_key,
        server_signature=server_signature,
    )
    key_material = udpsec_crypto.derive_session_key_material(
        client_shared_secret,
        transcript_hash,
    )
    client_hello = ClientHello(
        protocol_version=protocol_version,
        station_id=station_id,
        timestamp=timestamp,
        client_random=client_random,
        client_ephemeral_public_key=client_ephemeral_public_key,
        client_signature=client_signature,
    )
    server_hello = ServerHello(
        protocol_version=protocol_version,
        session_locator=session_locator,
        server_random=server_random,
        server_ephemeral_public_key=server_ephemeral_public_key,
        server_signature=server_signature,
    )
    return SimpleNamespace(
        client_hello=client_hello,
        server_hello=server_hello,
        client_packet=build_client_hello_packet(client_hello),
        server_packet=build_server_hello_packet(server_hello),
        client_digest=client_digest,
        server_digest=server_digest,
        session_locator=session_locator,
        shared_secret=client_shared_secret,
        transcript_hash=transcript_hash,
        key_material=key_material,
    )


def _perform_real_handshake(endpoints, client, remote_addr):
    confirmed_session = endpoints.proxy.perform_handshake(
        client,
        {"station_id": STATION_ID},
        endpoints.station_private_key,
        endpoints.server_public_key,
        remote_addr,
    )
    assert isinstance(confirmed_session, endpoints.proxy.ConfirmedUdpsecSession)
    assert isinstance(confirmed_session.key_material, SessionKeyMaterial)
    return confirmed_session


def _assert_single_confirmed_session(server):
    stats = server.state.stats()
    assert stats.current_sessions == 1
    assert stats.current_pending_sessions == 0
    assert len(server.state._sessions) == 1
    session = next(iter(server.state._sessions.values()))
    assert session._session_key.endpoint_token is server.endpoint_token
    return session


def _wait_for_server_stats(server, predicate, failure_message):
    deadline = time.monotonic() + NETWORK_TIMEOUT
    while True:
        stats = server.call_in_loop(server.state.stats)
        if predicate(stats):
            return stats
        if time.monotonic() >= deadline:
            raise AssertionError(failure_message)
        time.sleep(0.01)


def _wait_for_server_traffic(server, minimum_received, failure_message):
    deadline = time.monotonic() + NETWORK_TIMEOUT
    while True:
        snapshot = server.traffic.snapshot()
        if snapshot[0] >= minimum_received:
            return snapshot
        if time.monotonic() >= deadline:
            raise AssertionError(failure_message)
        time.sleep(0.01)


def _receive_authenticated_pong(
    proxy,
    client,
    remote_addr,
    confirmed_session,
    sequence,
):
    packet, sender = client.recvfrom(8192)
    assert proxy.remote_addresses_match(sender, remote_addr)
    assert proxy.handle_server_packet(
        packet,
        sender,
        remote_addr,
        confirmed_session.key_material.server_to_client_key,
        confirmed_session.session_locator,
        STATION_ID,
        sequence,
    ) == proxy.SERVER_PACKET_AUTHENTICATED
    return packet, sender


@pytest.mark.parametrize(
    ("family", "host"),
    (
        pytest.param(socket.AF_INET, "127.0.0.1", id="ipv4"),
        pytest.param(socket.AF_INET6, "::1", id="ipv6"),
    ),
)
def test_real_udp_loopback_interoperability(
    real_udpsec_endpoints,
    family,
    host,
):
    if family == socket.AF_INET6:
        _require_ipv6_loopback()

    endpoints = real_udpsec_endpoints
    with _running_secure_server(
        endpoints.secure,
        endpoints.server_private_key,
        family,
        host,
    ) as server:
        with _client_socket(family, host) as client:
            confirmed_session = _perform_real_handshake(
                endpoints,
                client,
                server.remote_addr,
            )
            key_material = confirmed_session.key_material
            session_locator = confirmed_session.session_locator

            session = _assert_single_confirmed_session(server)
            active_path = session.path_state.active_path
            assert active_path[:2] == client.getsockname()[:2]
            if family == socket.AF_INET6:
                assert len(active_path) == 4
                assert len(server.remote_addr) == 4
            assert (
                confirmed_session.key_material.client_to_server_key
                != confirmed_session.key_material.server_to_client_key
            )
            assert (
                session.current_epoch.client_to_server_aesgcm
                is not session.current_epoch.server_to_client_aesgcm
            )
            assert session._session_key.session_locator == session_locator

            endpoints.proxy.send_udpsec_nmea_sentence(
                NMEA_PAYLOAD,
                client,
                {"station_id": STATION_ID},
                confirmed_session.key_material.client_to_server_key,
                session_locator,
                server.remote_addr,
            )
            frame = server.ingress.get()
            assert isinstance(frame, IngressFrame)
            assert frame.kind == "sec"
            assert frame.source_id == f"udpsec:{STATION_ID}"
            assert frame.alias_for_s == "loopback-validation"
            assert frame.remote_ip == host
            assert frame.payload == NMEA_PAYLOAD.encode()
            assert frame.text_mode is PayloadTextMode.UTF8_SURROGATEPASS
            assert (
                decode_frame_slice(frame, 0, len(frame.payload))
                == NMEA_PAYLOAD
            )

            sequence = 17
            endpoints.proxy.send_ping(
                client,
                server.remote_addr,
                confirmed_session.key_material.client_to_server_key,
                session_locator,
                STATION_ID,
                sequence,
            )
            pong_packet, _ = _receive_authenticated_pong(
                endpoints.proxy,
                client,
                server.remote_addr,
                confirmed_session,
                sequence,
            )
            with pytest.raises(InvalidTag):
                endpoints.proxy.decrypt_secure_json_message(
                    pong_packet,
                    confirmed_session.key_material.client_to_server_key,
                    session_locator,
                )


def test_real_client_graceful_close_removes_server_session_without_ack(
    real_udpsec_endpoints,
):
    endpoints = real_udpsec_endpoints
    with _running_secure_server(
        endpoints.secure,
        endpoints.server_private_key,
        socket.AF_INET,
        "127.0.0.1",
    ) as server:
        with _client_socket(socket.AF_INET, "127.0.0.1") as client:
            confirmed_session = _perform_real_handshake(
                endpoints,
                client,
                server.remote_addr,
            )
            _assert_single_confirmed_session(server)

            endpoints.proxy.send_session_close(
                client,
                server.remote_addr,
                confirmed_session.key_material.client_to_server_key,
                confirmed_session.session_locator,
                STATION_ID,
            )

            deadline = time.monotonic() + NETWORK_TIMEOUT
            while True:
                stats = server.call_in_loop(server.state.stats)
                if stats.current_sessions == 0:
                    break
                if time.monotonic() >= deadline:
                    raise AssertionError(
                        "server did not consume authenticated client close"
                    )
                time.sleep(0.01)

            assert stats.sessions_closed == 1
            assert stats.current_pending_sessions == 0
            assert stats.current_data_nonces == 0
            client.settimeout(0.1)
            with pytest.raises(socket.timeout):
                client.recvfrom(8192)


def test_real_server_graceful_close_ends_proxy_session_with_backoff(
    real_udpsec_endpoints,
):
    endpoints = real_udpsec_endpoints
    server = _LoopbackSecureServer(
        endpoints.secure,
        endpoints.server_private_key,
        socket.AF_INET,
        "127.0.0.1",
        graceful_close=True,
    ).start()
    try:
        with _client_socket(socket.AF_INET, "127.0.0.1") as client:
            confirmed_session = _perform_real_handshake(
                endpoints,
                client,
                server.remote_addr,
            )
            _assert_single_confirmed_session(server)

            server.close()

            config = {
                "station_id": STATION_ID,
                "keepalive_interval": 30,
                "peer_timeout": 90,
                "session_refresh_interval": 0,
                "reconnect_delay": 0.01,
            }
            reason = endpoints.proxy.forward_loop(
                _IdleInput(),
                client,
                config,
                confirmed_session,
                server.remote_addr,
            )

            assert reason == (
                endpoints.proxy.SESSION_END_PEER_GRACEFUL_CLOSE
            )
            assert (
                endpoints.proxy.retry_delay_for_reason(reason, config)
                == config["reconnect_delay"]
            )
            assert server.state.stats().sessions_closed == 1
            assert server.state.stats().current_sessions == 0
    finally:
        server.close()


def test_real_client_proactively_recovers_after_server_restart(
    real_udpsec_endpoints,
):
    endpoints = real_udpsec_endpoints

    with _client_socket(socket.AF_INET, "127.0.0.1") as client:
        with _running_secure_server(
            endpoints.secure,
            endpoints.server_private_key,
            socket.AF_INET,
            "127.0.0.1",
        ) as initial_server:
            initial_remote_addr = initial_server.remote_addr
            initial_confirmed_session = _perform_real_handshake(
                endpoints,
                client,
                initial_remote_addr,
            )
            _assert_single_confirmed_session(initial_server)

        with _running_secure_server(
            endpoints.secure,
            endpoints.server_private_key,
            socket.AF_INET,
            "127.0.0.1",
            initial_remote_addr[1],
        ) as restarted_server:
            assert restarted_server.remote_addr == initial_remote_addr
            config = {
                "station_id": STATION_ID,
                "keepalive_interval": 0.03,
                "peer_timeout": 0.12,
                "session_refresh_interval": 0,
                "reconnect_delay": 0.01,
            }

            recovery_started = time.monotonic()
            reason = endpoints.proxy.forward_loop(
                _IdleInput(),
                client,
                config,
                initial_confirmed_session,
                restarted_server.remote_addr,
            )
            recovery_elapsed = time.monotonic() - recovery_started

            assert reason == endpoints.proxy.SESSION_END_PROACTIVE_REKEY
            assert (
                endpoints.proxy.retry_delay_for_reason(reason, config)
                is None
            )
            assert recovery_elapsed < config["peer_timeout"]
            lost_state_stats = restarted_server.call_in_loop(
                restarted_server.state.stats
            )
            assert lost_state_stats.current_sessions == 0
            assert lost_state_stats.current_pending_sessions == 0
            assert lost_state_stats.data_nonces_accepted == 0
            assert restarted_server.ingress.empty()

            recovered_confirmed_session = _perform_real_handshake(
                endpoints,
                client,
                restarted_server.remote_addr,
            )
            recovered_session = _assert_single_confirmed_session(
                restarted_server
            )
            assert (
                recovered_session.path_state.active_path
                == client.getsockname()
            )
            assert (
                recovered_confirmed_session != initial_confirmed_session
            )

            recovered_payload = (
                "!AIVDM,1,1,,A,recovered-after-server-restart,0*00"
            )
            endpoints.proxy.send_udpsec_nmea_sentence(
                recovered_payload,
                client,
                {"station_id": STATION_ID},
                recovered_confirmed_session.key_material.client_to_server_key,
                recovered_confirmed_session.session_locator,
                restarted_server.remote_addr,
            )
            frame = restarted_server.ingress.get()
            assert (
                decode_frame_slice(frame, 0, len(frame.payload))
                == recovered_payload
            )

            sequence = 18
            endpoints.proxy.send_ping(
                client,
                restarted_server.remote_addr,
                recovered_confirmed_session.key_material.client_to_server_key,
                recovered_confirmed_session.session_locator,
                STATION_ID,
                sequence,
            )
            _receive_authenticated_pong(
                endpoints.proxy,
                client,
                restarted_server.remote_addr,
                recovered_confirmed_session,
                sequence,
            )


def test_real_nonce_exhaustion_recovers_with_fresh_replay_epoch(
    real_udpsec_endpoints,
):
    endpoints = real_udpsec_endpoints
    state = endpoints.secure.SecureState(
        data_nonce_max_per_session=2,
    )

    with _running_secure_server(
        endpoints.secure,
        endpoints.server_private_key,
        socket.AF_INET,
        "127.0.0.1",
        state=state,
    ) as server:
        with _client_socket(socket.AF_INET, "127.0.0.1") as client:
            initial_confirmed_session = _perform_real_handshake(
                endpoints,
                client,
                server.remote_addr,
            )
            initial_session = _assert_single_confirmed_session(server)
            after_confirmation = server.call_in_loop(server.state.stats)

            # The authenticated sequence-zero confirmation owns the first
            # replay slot in this traffic-key epoch.
            assert len(initial_session.current_epoch.seen_data_nonces) == 1
            assert after_confirmation.current_data_nonces == 1
            assert after_confirmation.data_nonces_accepted == 1

            nonce_a = _nonce(950)
            assert not initial_session.current_epoch.seen_data_nonces.contains(nonce_a)
            payload_a = "!AIVDM,1,1,,A,nonce-capacity-a,0*00"
            packet_a = _encrypted_json_packet(
                endpoints.proxy,
                initial_confirmed_session.key_material.client_to_server_key,
                nonce_a,
                {
                    "type": "nmea",
                    "payload": payload_a,
                    "timestamp": 1000,
                    "source_id": STATION_ID,
                },
                initial_confirmed_session.session_locator,
            )
            client.sendto(packet_a, server.remote_addr)

            frame_a = server.ingress.get()
            assert (
                decode_frame_slice(frame_a, 0, len(frame_a.payload))
                == payload_a
            )
            assert server.ingress.empty()
            after_a = server.call_in_loop(server.state.stats)
            assert (
                server.active_session_for(client.getsockname())
                is initial_session
            )
            assert len(initial_session.current_epoch.seen_data_nonces) == 2
            assert initial_session.current_epoch.seen_data_nonces.contains(nonce_a)
            assert (
                after_a.data_nonces_accepted
                == after_confirmation.data_nonces_accepted + 1
            )
            assert after_a.current_data_nonces == 2

            # Membership is authoritative before the capacity check: exact A
            # remains a replay at size == max and cannot exhaust the epoch.
            client.sendto(packet_a, server.remote_addr)
            after_replay = _wait_for_server_stats(
                server,
                lambda stats: (
                    stats.data_nonce_replays
                    == after_a.data_nonce_replays + 1
                ),
                "server did not classify exact full-capacity replay",
            )
            assert (
                server.active_session_for(client.getsockname())
                is initial_session
            )
            assert len(initial_session.current_epoch.seen_data_nonces) == 2
            assert after_replay.current_sessions == 1
            assert after_replay.current_data_nonces == 2
            assert (
                after_replay.data_nonce_exhaustions
                == after_a.data_nonce_exhaustions
            )
            assert after_replay.sessions_touched == after_a.sessions_touched
            assert server.ingress.empty()

            nonce_b = _nonce(951)
            payload_b = "!AIVDM,1,1,,A,must-drop-on-exhaustion,0*00"
            packet_b = _encrypted_json_packet(
                endpoints.proxy,
                initial_confirmed_session.key_material.client_to_server_key,
                nonce_b,
                {
                    "type": "nmea",
                    "payload": payload_b,
                    "timestamp": 1001,
                    "source_id": STATION_ID,
                },
                initial_confirmed_session.session_locator,
            )
            last_seen_before_exhaustion = initial_session.last_seen
            client.sendto(packet_b, server.remote_addr)
            exhausted = _wait_for_server_stats(
                server,
                lambda stats: (
                    stats.data_nonce_exhaustions
                    == after_replay.data_nonce_exhaustions + 1
                    and stats.current_sessions == 0
                ),
                "server did not fail closed on DATA nonce exhaustion",
            )

            assert server.state._sessions == {}
            assert server.owned_sessions == {}
            assert len(initial_session.current_epoch.seen_data_nonces) == 0
            assert initial_session.last_seen == last_seen_before_exhaustion
            assert exhausted.current_pending_sessions == 0
            assert exhausted.current_data_nonces == 0
            assert (
                exhausted.data_nonces_accepted
                == after_replay.data_nonces_accepted
            )
            assert (
                exhausted.data_nonce_replays
                == after_replay.data_nonce_replays
            )
            assert exhausted.sessions_touched == after_replay.sessions_touched
            assert (
                exhausted.data_nonces_session_discarded
                == after_replay.data_nonces_session_discarded + 2
            )
            assert server.ingress.empty()

            # Exact old ciphertext is inert once its key epoch is gone. The
            # existing unanswered-ping lifecycle then requests a fresh epoch.
            client.sendto(packet_a, server.remote_addr)
            config = {
                "station_id": STATION_ID,
                "keepalive_interval": 0.03,
                "peer_timeout": 0.12,
                "session_refresh_interval": 0,
                "reconnect_delay": 0.01,
            }
            recovery_started = time.monotonic()
            reason = endpoints.proxy.forward_loop(
                _IdleInput(),
                client,
                config,
                initial_confirmed_session,
                server.remote_addr,
            )
            recovery_elapsed = time.monotonic() - recovery_started

            assert reason == endpoints.proxy.SESSION_END_PROACTIVE_REKEY
            assert (
                endpoints.proxy.retry_delay_for_reason(reason, config)
                is None
            )
            assert recovery_elapsed < config["peer_timeout"]
            after_silent_loss = server.call_in_loop(server.state.stats)
            assert after_silent_loss.current_sessions == 0
            assert after_silent_loss.current_data_nonces == 0
            assert (
                after_silent_loss.data_nonces_accepted
                == exhausted.data_nonces_accepted
            )
            assert (
                after_silent_loss.sessions_touched
                == exhausted.sessions_touched
            )
            assert server.ingress.empty()

            recovered_confirmed_session = _perform_real_handshake(
                endpoints,
                client,
                server.remote_addr,
            )
            recovered_session = _assert_single_confirmed_session(server)
            recovered = server.call_in_loop(server.state.stats)
            assert recovered_session is not initial_session
            assert recovered_confirmed_session != initial_confirmed_session
            assert len(recovered_session.current_epoch.seen_data_nonces) == 1
            assert not recovered_session.current_epoch.seen_data_nonces.contains(nonce_a)
            assert recovered.current_data_nonces == 1

            fresh_nonce = _nonce(952)
            fresh_payload = "!AIVDM,1,1,,A,fresh-after-exhaustion,0*00"
            fresh_packet = _encrypted_json_packet(
                endpoints.proxy,
                recovered_confirmed_session.key_material.client_to_server_key,
                fresh_nonce,
                {
                    "type": "nmea",
                    "payload": fresh_payload,
                    "timestamp": 1002,
                    "source_id": STATION_ID,
                },
                recovered_confirmed_session.session_locator,
            )

            # The old packet uses an unseen nonce in the fresh ledger, so its
            # rejection demonstrates key-epoch isolation rather than a nonce
            # duplicate fast path. Only the fresh packet may reach ingress.
            received_before, accepted_before = server.traffic.snapshot()
            client.sendto(packet_a, server.remote_addr)
            client.sendto(fresh_packet, server.remote_addr)
            fresh_frame = server.ingress.get()
            assert (
                decode_frame_slice(
                    fresh_frame,
                    0,
                    len(fresh_frame.payload),
                )
                == fresh_payload
            )
            received_after, accepted_after = _wait_for_server_traffic(
                server,
                received_before + 2,
                "server did not receive both old- and fresh-epoch packets",
            )
            assert received_after == received_before + 2
            assert accepted_after == accepted_before + 1
            assert server.ingress.empty()

            final = server.call_in_loop(server.state.stats)
            assert (
                server.active_session_for(client.getsockname())
                is recovered_session
            )
            assert final.current_sessions == 1
            assert final.current_data_nonces == 2
            assert (
                final.data_nonce_exhaustions
                == exhausted.data_nonce_exhaustions
            )
            assert final.data_nonce_replays == recovered.data_nonce_replays
            assert (
                final.data_nonces_accepted
                == recovered.data_nonces_accepted + 1
            )
            assert final.sessions_touched == recovered.sessions_touched + 1


def test_real_confirmed_same_address_rekey_replaces_traffic_keys(
    real_udpsec_endpoints,
):
    endpoints = real_udpsec_endpoints
    with _running_secure_server(
        endpoints.secure,
        endpoints.server_private_key,
        socket.AF_INET,
        "127.0.0.1",
    ) as server:
        with _client_socket(socket.AF_INET, "127.0.0.1") as client:
            client_addr = client.getsockname()
            first = _perform_real_handshake(
                endpoints,
                client,
                server.remote_addr,
            )
            first_session = _assert_single_confirmed_session(server)
            assert first_session.path_state.active_path == client_addr

            second = _perform_real_handshake(
                endpoints,
                client,
                server.remote_addr,
            )
            assert client.getsockname() == client_addr
            second_session = _assert_single_confirmed_session(server)
            assert second_session.path_state.active_path == client_addr
            assert second_session is not first_session
            assert first.session_locator != second.session_locator
            assert (
                first.key_material.client_to_server_key
                != second.key_material.client_to_server_key
            )
            assert (
                first.key_material.server_to_client_key
                != second.key_material.server_to_client_key
            )
            assert (
                second.key_material.client_to_server_key
                != second.key_material.server_to_client_key
            )

            touches_before_traffic = server.state.stats().sessions_touched
            old_payload = "!AIVDM,1,1,,A,old-session-payload,0*00"
            client.sendto(
                endpoints.proxy.encrypt_secure_json_message(
                    {
                        "type": "nmea",
                        "payload": old_payload,
                        "timestamp": 1,
                        "source_id": STATION_ID,
                    },
                    first.key_material.client_to_server_key,
                    first.session_locator,
                ),
                server.remote_addr,
            )

            new_payload = "!AIVDM,1,1,,A,new-session-payload,0*00"
            endpoints.proxy.send_udpsec_nmea_sentence(
                new_payload,
                client,
                {"station_id": STATION_ID},
                second.key_material.client_to_server_key,
                second.session_locator,
                server.remote_addr,
            )
            sequence = 23
            endpoints.proxy.send_ping(
                client,
                server.remote_addr,
                second.key_material.client_to_server_key,
                second.session_locator,
                STATION_ID,
                sequence,
            )
            pong_packet, sender = _receive_authenticated_pong(
                endpoints.proxy,
                client,
                server.remote_addr,
                second,
                sequence,
            )

            frame = server.ingress.get()
            assert (
                decode_frame_slice(frame, 0, len(frame.payload))
                == new_payload
            )
            assert server.ingress.empty()
            assert (
                server.state.stats().sessions_touched
                == touches_before_traffic + 2
            )
            assert endpoints.proxy.handle_server_packet(
                pong_packet,
                sender,
                server.remote_addr,
                first.key_material.server_to_client_key,
                first.session_locator,
                STATION_ID,
                sequence,
            ) == endpoints.proxy.SERVER_PACKET_IGNORED
            # The wrong key still fails cryptographically even when paired
            # with the packet's own (correct) locator.
            with pytest.raises(InvalidTag):
                endpoints.proxy.decrypt_secure_json_message(
                    pong_packet,
                    first.key_material.server_to_client_key,
                    second.session_locator,
                )
            # The wrong locator alone is also rejected, regardless of key.
            with pytest.raises(ValueError):
                endpoints.proxy.decrypt_secure_json_message(
                    pong_packet,
                    second.key_material.server_to_client_key,
                    first.session_locator,
                )


@pytest.mark.parametrize(
    "loss_point",
    ("server_hello", "confirmation_ping", "confirmation_pong"),
)
def test_real_packet_loss_requires_fresh_handshake_retry(
    real_udpsec_endpoints,
    loss_point,
):
    endpoints = real_udpsec_endpoints
    with _running_secure_server(
        endpoints.secure,
        endpoints.server_private_key,
        socket.AF_INET,
        "127.0.0.1",
    ) as server:
        with _client_socket(socket.AF_INET, "127.0.0.1") as client:
            client.settimeout(0.25)
            dropping_client = _OneShotDroppingSocket(client, loss_point)
            first_result = endpoints.proxy.perform_handshake(
                dropping_client,
                {"station_id": STATION_ID},
                endpoints.station_private_key,
                endpoints.server_public_key,
                server.remote_addr,
            )
            assert first_result is None
            assert dropping_client.dropped
            assert len(dropping_client.client_hello_packets) == 1

            failed_stats = server.call_in_loop(server.state.stats)
            if loss_point in ("server_hello", "confirmation_ping"):
                assert failed_stats.current_sessions == 0
                assert failed_stats.current_pending_sessions == 1
                assert len(server.state._pending_sessions) == 1
            else:
                assert failed_stats.current_sessions == 1
                assert failed_stats.current_pending_sessions == 0
                first_active = next(iter(server.state._sessions.values()))

            if loss_point == "confirmation_ping":
                pending = server.call_in_loop(
                    lambda: next(iter(server.state._pending_sessions.values()))
                )
                exact_expiry = (
                    pending.created_at
                    + server.state._pending_session_ttl
                )
                expired = server.call_in_loop(
                    lambda: server.state.cleanup_expired_pending_sessions(
                        exact_expiry
                    )
                )
                assert expired == [
                    server.relation_key(client.getsockname())
                ]
                assert (
                    server.call_in_loop(server.state.stats)
                    .pending_sessions_expired
                    == 1
                )

            second_result = _perform_real_handshake(
                endpoints,
                dropping_client,
                server.remote_addr,
            )
            assert isinstance(second_result, endpoints.proxy.ConfirmedUdpsecSession)
            assert isinstance(second_result.key_material, SessionKeyMaterial)
            assert len(dropping_client.client_hello_packets) == 2
            first_hello = parse_client_hello_packet(
                dropping_client.client_hello_packets[0]
            )
            second_hello = parse_client_hello_packet(
                dropping_client.client_hello_packets[1]
            )
            assert first_hello.client_random != second_hello.client_random
            assert (
                first_hello.client_ephemeral_public_key
                != second_hello.client_ephemeral_public_key
            )

            final_session = _assert_single_confirmed_session(server)
            final_stats = server.call_in_loop(server.state.stats)
            assert final_stats.handshake_replay_accepted == 2
            assert final_stats.pending_sessions_created == 2
            if loss_point == "server_hello":
                assert final_stats.pending_sessions_replaced == 1
            elif loss_point == "confirmation_ping":
                assert final_stats.pending_sessions_expired == 1
            else:
                assert final_session is not first_active
                assert final_stats.sessions_replaced == 1


def test_real_sessions_are_isolated_by_complete_udp_peer_address(
    real_udpsec_endpoints,
):
    endpoints = real_udpsec_endpoints
    with _running_secure_server(
        endpoints.secure,
        endpoints.server_private_key,
        socket.AF_INET,
        "127.0.0.1",
    ) as server:
        with (
            _client_socket(socket.AF_INET, "127.0.0.1") as client_a,
            _client_socket(socket.AF_INET, "127.0.0.1") as client_b,
            _client_socket(socket.AF_INET, "127.0.0.1") as client_c,
            _client_socket(socket.AF_INET, "127.0.0.1") as client_d,
        ):
            recording_client_a = _OneShotDroppingSocket(client_a, "none")
            material_a = _perform_real_handshake(
                endpoints,
                recording_client_a,
                server.remote_addr,
            )
            material_b = _perform_real_handshake(
                endpoints,
                client_b,
                server.remote_addr,
            )
            assert client_a.getsockname() != client_b.getsockname()
            session_a = server.active_session_for(client_a.getsockname())
            session_b = server.active_session_for(client_b.getsockname())
            assert set(server.state._sessions) == {
                session_a._session_key,
                session_b._session_key,
            }
            assert material_a != material_b
            assert material_a.session_locator != material_b.session_locator

            shared_nonce = _nonce(700)
            payload_a = "!AIVDM,1,1,,A,client-a,0*00"
            payload_b = "!AIVDM,1,1,,A,client-b,0*00"
            packet_a = _encrypted_json_packet(
                endpoints.proxy,
                material_a.key_material.client_to_server_key,
                shared_nonce,
                {
                    "type": "nmea",
                    "payload": payload_a,
                    "timestamp": 1000,
                    "source_id": STATION_ID,
                },
                material_a.session_locator,
            )
            packet_b = _encrypted_json_packet(
                endpoints.proxy,
                material_b.key_material.client_to_server_key,
                shared_nonce,
                {
                    "type": "nmea",
                    "payload": payload_b,
                    "timestamp": 1000,
                    "source_id": STATION_ID,
                },
                material_b.session_locator,
            )
            client_a.sendto(packet_a, server.remote_addr)
            client_b.sendto(packet_b, server.remote_addr)
            received_payloads = {
                decode_frame_slice(
                    frame,
                    0,
                    len(frame.payload),
                )
                for frame in (server.ingress.get(), server.ingress.get())
            }
            assert received_payloads == {payload_a, payload_b}
            assert server.ingress.empty()

            assert shared_nonce in session_a.current_epoch.seen_data_nonces._live_by_key
            assert shared_nonce in session_b.current_epoch.seen_data_nonces._live_by_key

            before_cross_key = server.call_in_loop(server.state.stats)
            cross_key_nonce = _nonce(701)
            # Correct (b's) locator/path but the WRONG key -- isolates key
            # authentication specifically, distinct from locator/path
            # routing which is exercised separately below.
            cross_key_packet = _encrypted_json_packet(
                endpoints.proxy,
                material_a.key_material.client_to_server_key,
                cross_key_nonce,
                {
                    "type": "nmea",
                    "payload": "must-not-authenticate-for-b",
                    "timestamp": 1000,
                    "source_id": STATION_ID,
                },
                material_b.session_locator,
            )
            client_b.sendto(cross_key_packet, server.remote_addr)
            endpoints.proxy.send_ping(
                client_b,
                server.remote_addr,
                material_b.key_material.client_to_server_key,
                material_b.session_locator,
                STATION_ID,
                31,
            )
            _receive_authenticated_pong(
                endpoints.proxy,
                client_b,
                server.remote_addr,
                material_b,
                31,
            )
            after_cross_key = server.call_in_loop(server.state.stats)
            assert (
                after_cross_key.sessions_touched
                == before_cross_key.sessions_touched + 1
            )
            assert (
                after_cross_key.data_nonces_accepted
                == before_cross_key.data_nonces_accepted + 1
            )
            assert (
                cross_key_nonce
                not in session_b.current_epoch.seen_data_nonces._live_by_key
            )
            assert server.ingress.empty()

            before_port_change = server.call_in_loop(server.state.stats)
            client_c.sendto(packet_a, server.remote_addr)
            client_c.settimeout(0.1)
            with pytest.raises(socket.timeout):
                client_c.recvfrom(8192)
            client_c.settimeout(NETWORK_TIMEOUT)
            assert server.ingress.empty()
            after_port_change = server.call_in_loop(server.state.stats)
            assert (
                after_port_change.sessions_touched
                == before_port_change.sessions_touched
            )
            assert (
                after_port_change.data_nonces_accepted
                == before_port_change.data_nonces_accepted
            )

            material_c = _perform_real_handshake(
                endpoints,
                client_c,
                server.remote_addr,
            )
            assert isinstance(material_c, endpoints.proxy.ConfirmedUdpsecSession)
            assert isinstance(material_c.key_material, SessionKeyMaterial)
            assert len(server.state._sessions) == 3

            exact_client_hello = recording_client_a.client_hello_packets[0]
            replay_before = server.call_in_loop(server.state.stats)
            client_d.sendto(exact_client_hello, server.remote_addr)
            endpoints.proxy.send_ping(
                client_a,
                server.remote_addr,
                material_a.key_material.client_to_server_key,
                material_a.session_locator,
                STATION_ID,
                32,
            )
            _receive_authenticated_pong(
                endpoints.proxy,
                client_a,
                server.remote_addr,
                material_a,
                32,
            )
            client_d.settimeout(0.1)
            with pytest.raises(socket.timeout):
                client_d.recvfrom(8192)
            replay_after = server.call_in_loop(server.state.stats)
            assert (
                replay_after.handshake_replay_rejected
                == replay_before.handshake_replay_rejected + 1
            )
            assert replay_after.current_sessions == 3
            assert replay_after.current_pending_sessions == 0


def test_real_same_peer_is_isolated_across_physical_listeners(
    real_udpsec_endpoints,
):
    endpoints = real_udpsec_endpoints
    shared_state = endpoints.secure.SecureState()

    with (
        _running_secure_server(
            endpoints.secure,
            endpoints.server_private_key,
            socket.AF_INET,
            "127.0.0.1",
            state=shared_state,
        ) as server_a,
        _running_secure_server(
            endpoints.secure,
            endpoints.server_private_key,
            socket.AF_INET,
            "127.0.0.1",
            state=shared_state,
        ) as server_b,
        _client_socket(socket.AF_INET, "127.0.0.1") as client,
    ):
        client_address = client.getsockname()
        material_a = _perform_real_handshake(
            endpoints,
            client,
            server_a.remote_addr,
        )
        relation_a = server_a.relation_key(client_address)
        session_a = server_a.active_session_for(client_address)

        material_b = _perform_real_handshake(
            endpoints,
            client,
            server_b.remote_addr,
        )
        relation_b = server_b.relation_key(client_address)
        session_b = server_b.active_session_for(client_address)

        assert server_a.remote_addr != server_b.remote_addr
        assert server_a.endpoint_token is not server_b.endpoint_token
        assert relation_a.peer_address == relation_b.peer_address
        assert relation_a != relation_b
        assert session_a is not session_b
        assert session_a.path_state.active_path == client_address
        assert session_b.path_state.active_path == client_address
        assert material_a != material_b
        assert material_a.session_locator != material_b.session_locator
        assert set(shared_state._sessions) == {
            session_a._session_key,
            session_b._session_key,
        }
        assert server_a.owned_sessions == {session_a._session_key: session_a}
        assert server_b.owned_sessions == {session_b._session_key: session_b}

        confirmed = server_a.call_in_loop(shared_state.stats)
        assert confirmed.current_sessions == 2
        assert confirmed.current_pending_sessions == 0
        assert confirmed.sessions_replaced == 0
        assert confirmed.pending_sessions_replaced == 0
        assert confirmed.handshake_replay_accepted == 2
        assert confirmed.handshake_replay_rejected == 0

        shared_nonce = _nonce(750)
        payload_a = "!AIVDM,1,1,,A,listener-a,0*00"
        payload_b = "!AIVDM,1,1,,A,listener-b,0*00"
        packet_a = _encrypted_json_packet(
            endpoints.proxy,
            material_a.key_material.client_to_server_key,
            shared_nonce,
            {
                "type": "nmea",
                "payload": payload_a,
                "timestamp": 1000,
                "source_id": STATION_ID,
            },
            material_a.session_locator,
        )
        packet_b = _encrypted_json_packet(
            endpoints.proxy,
            material_b.key_material.client_to_server_key,
            shared_nonce,
            {
                "type": "nmea",
                "payload": payload_b,
                "timestamp": 1000,
                "source_id": STATION_ID,
            },
            material_b.session_locator,
        )

        client.sendto(packet_a, server_a.remote_addr)
        frame_a = server_a.ingress.get()
        assert decode_frame_slice(frame_a, 0, len(frame_a.payload)) == payload_a
        assert server_b.ingress.empty()

        client.sendto(packet_b, server_b.remote_addr)
        frame_b = server_b.ingress.get()
        assert decode_frame_slice(frame_b, 0, len(frame_b.payload)) == payload_b
        assert server_a.ingress.empty()
        assert session_a.current_epoch.seen_data_nonces.contains(shared_nonce)
        assert session_b.current_epoch.seen_data_nonces.contains(shared_nonce)

        endpoints.proxy.send_ping(
            client,
            server_a.remote_addr,
            material_a.key_material.client_to_server_key,
            material_a.session_locator,
            STATION_ID,
            41,
        )
        _receive_authenticated_pong(
            endpoints.proxy,
            client,
            server_a.remote_addr,
            material_a,
            41,
        )
        endpoints.proxy.send_ping(
            client,
            server_b.remote_addr,
            material_b.key_material.client_to_server_key,
            material_b.session_locator,
            STATION_ID,
            42,
        )
        _receive_authenticated_pong(
            endpoints.proxy,
            client,
            server_b.remote_addr,
            material_b,
            42,
        )

        before_cross = server_a.call_in_loop(shared_state.stats)
        a_last_seen = session_a.last_seen
        a_nonces = set(session_a.current_epoch.seen_data_nonces._live_by_key)
        b_nonce_count = len(session_b.current_epoch.seen_data_nonces)
        cross_nmea_nonce = _nonce(751)
        cross_ping_nonce = _nonce(752)
        # These carry listener A's own locator but are physically delivered
        # to listener B: (endpoint_token_b, locator_a) must match nothing in
        # the shared session store, so they are dropped as an unknown
        # locator -- proving cross-listener locator values do not select
        # another listener's session.
        cross_nmea = _encrypted_json_packet(
            endpoints.proxy,
            material_a.key_material.client_to_server_key,
            cross_nmea_nonce,
            {
                "type": "nmea",
                "payload": "must-not-cross-endpoints",
                "timestamp": 1000,
                "source_id": STATION_ID,
            },
            material_a.session_locator,
        )
        cross_ping = _encrypted_json_packet(
            endpoints.proxy,
            material_a.key_material.client_to_server_key,
            cross_ping_nonce,
            {
                "type": "ping",
                "seq": 43,
                "timestamp": 1000,
                "source_id": STATION_ID,
            },
            material_a.session_locator,
        )

        client.sendto(cross_nmea, server_b.remote_addr)
        client.sendto(cross_ping, server_b.remote_addr)
        endpoints.proxy.send_session_close(
            client,
            server_b.remote_addr,
            material_a.key_material.client_to_server_key,
            material_a.session_locator,
            STATION_ID,
        )
        endpoints.proxy.send_ping(
            client,
            server_b.remote_addr,
            material_b.key_material.client_to_server_key,
            material_b.session_locator,
            STATION_ID,
            44,
        )
        _receive_authenticated_pong(
            endpoints.proxy,
            client,
            server_b.remote_addr,
            material_b,
            44,
        )

        after_cross = server_a.call_in_loop(shared_state.stats)
        assert server_a.active_session_for(client_address) is session_a
        assert server_b.active_session_for(client_address) is session_b
        assert session_a.last_seen == a_last_seen
        assert set(session_a.current_epoch.seen_data_nonces._live_by_key) == a_nonces
        assert len(session_b.current_epoch.seen_data_nonces) == b_nonce_count + 1
        assert not session_b.current_epoch.seen_data_nonces.contains(cross_nmea_nonce)
        assert not session_b.current_epoch.seen_data_nonces.contains(cross_ping_nonce)
        assert after_cross.data_nonces_accepted == (
            before_cross.data_nonces_accepted + 1
        )
        assert after_cross.sessions_touched == before_cross.sessions_touched + 1
        assert after_cross.sessions_closed == before_cross.sessions_closed
        assert server_a.ingress.empty()
        assert server_b.ingress.empty()

        endpoints.proxy.send_session_close(
            client,
            server_a.remote_addr,
            material_a.key_material.client_to_server_key,
            material_a.session_locator,
            STATION_ID,
        )
        closed = _wait_for_server_stats(
            server_a,
            lambda stats: stats.current_sessions == 1,
            "listener A did not close its endpoint-scoped session",
        )
        assert server_a.active_session_for(client_address) is None
        assert server_b.active_session_for(client_address) is session_b
        assert closed.sessions_closed == before_cross.sessions_closed + 1

        endpoints.proxy.send_ping(
            client,
            server_b.remote_addr,
            material_b.key_material.client_to_server_key,
            material_b.session_locator,
            STATION_ID,
            45,
        )
        _receive_authenticated_pong(
            endpoints.proxy,
            client,
            server_b.remote_addr,
            material_b,
            45,
        )
        assert server_b.active_session_for(client_address) is session_b


class _GatedClock:
    """Thread-safe monotonic-clock stand-in whose value the test sets
    explicitly, read by two real listener threads sharing one
    `SecureState`. Lets a test force a specific, otherwise-improbable
    `now` ordering across two genuinely concurrent listeners without any
    real-wall-clock timing dependency."""

    def __init__(self, initial):
        self._lock = threading.Lock()
        self._value = initial

    def set(self, value):
        with self._lock:
            self._value = value

    def __call__(self):
        with self._lock:
            return self._value


def test_real_two_listeners_racing_commit_order_does_not_defeat_expiry(
    real_udpsec_endpoints,
):
    """F2, reproduced through the REAL `_secure_server_loop` (not only a
    direct SecureState dictionary test): two real background-thread
    secure listeners share one `SecureState`. Listener A's DATA-triggered
    touch is made to COMMIT FIRST while reading the LARGER `now` (6);
    listener B's commits SECOND while reading the SMALLER `now` (5) --
    exactly the pathological OrderedDict order [6, 5] the audit reported.
    At `session_ttl=10`, checking at `now=15` must still find B genuinely
    expired despite B sitting behind A in commit/position order, and must
    leave A (genuinely not yet expired) untouched."""
    endpoints = real_udpsec_endpoints
    shared_state = endpoints.secure.SecureState(session_ttl=10.0)
    clock = _GatedClock(0.0)

    with (
        _running_secure_server(
            endpoints.secure,
            endpoints.server_private_key,
            socket.AF_INET,
            "127.0.0.1",
            state=shared_state,
            monotonic_clock=clock,
        ) as server_a,
        _running_secure_server(
            endpoints.secure,
            endpoints.server_private_key,
            socket.AF_INET,
            "127.0.0.1",
            state=shared_state,
            monotonic_clock=clock,
        ) as server_b,
        _client_socket(socket.AF_INET, "127.0.0.1") as client_a,
        _client_socket(socket.AF_INET, "127.0.0.1") as client_b,
    ):
        material_a = _perform_real_handshake(
            endpoints, client_a, server_a.remote_addr
        )
        material_b = _perform_real_handshake(
            endpoints, client_b, server_b.remote_addr
        )
        session_a = server_a.active_session_for(client_a.getsockname())
        session_b = server_b.active_session_for(client_b.getsockname())
        assert session_a is not None and session_b is not None

        # A commits first, reading the LARGER `now`.
        clock.set(6.0)
        endpoints.proxy.send_ping(
            client_a,
            server_a.remote_addr,
            material_a.key_material.client_to_server_key,
            material_a.session_locator,
            STATION_ID,
            41,
        )
        _receive_authenticated_pong(
            endpoints.proxy, client_a, server_a.remote_addr, material_a, 41
        )
        assert session_a.last_seen == 6.0

        # B commits second, reading the SMALLER `now` -- the exact
        # pathology: B is chronologically older (more overdue for
        # expiry) but was touched/moved-to-end AFTER A.
        clock.set(5.0)
        endpoints.proxy.send_ping(
            client_b,
            server_b.remote_addr,
            material_b.key_material.client_to_server_key,
            material_b.session_locator,
            STATION_ID,
            42,
        )
        _receive_authenticated_pong(
            endpoints.proxy, client_b, server_b.remote_addr, material_b, 42
        )
        assert session_b.last_seen == 5.0
        assert tuple(shared_state._sessions) == (
            session_a._session_key,
            session_b._session_key,
        )

        expired = shared_state.cleanup_expired_sessions(15.0)

        assert session_b._session_key in expired
        assert session_a._session_key not in expired
        assert shared_state.is_live_session_handle(session_a, 15.0)
        assert server_b.active_session_for(client_b.getsockname()) is None


def test_real_secure_server_loop_stale_local_now_cannot_admit_past_authoritative_clock(
    real_udpsec_endpoints,
):
    """R5/F2 through the REAL `_secure_server_loop`, not a direct
    SecureState call: the receive loop's own `local_now` sampling (its
    injected `monotonic_clock`) is deliberately held stale (5.0) while the
    shared SecureState's OWN, independently configured authoritative
    clock has already advanced to 11.0 -- past this session's TTL(10)
    deadline. A real encrypted ping processed under these conditions must
    be rejected: no pong arrives, and the session is gone from the shared
    state, proving the authoritative floor applies even when a real
    production receive loop (real crypto, real frame parsing) supplies
    the stale value, not only a direct unit-level call."""
    endpoints = real_udpsec_endpoints
    authoritative_clock = _GatedClock(0.0)
    shared_state = endpoints.secure.SecureState(
        session_ttl=10.0, clock=authoritative_clock
    )
    loop_clock = _GatedClock(0.0)

    with (
        _running_secure_server(
            endpoints.secure,
            endpoints.server_private_key,
            socket.AF_INET,
            "127.0.0.1",
            state=shared_state,
            monotonic_clock=loop_clock,
        ) as server,
        _client_socket(socket.AF_INET, "127.0.0.1") as client,
    ):
        material = _perform_real_handshake(
            endpoints, client, server.remote_addr
        )
        session = server.active_session_for(client.getsockname())
        assert session is not None
        assert session.last_seen == 0.0

        # Real elapsed time moves the authoritative clock past the
        # deadline, but the receive loop's own local_now sampling for the
        # next packet stays stale, well behind it.
        authoritative_clock.set(11.0)
        loop_clock.set(5.0)

        endpoints.proxy.send_ping(
            client,
            server.remote_addr,
            material.key_material.client_to_server_key,
            material.session_locator,
            STATION_ID,
            41,
        )
        with pytest.raises(socket.timeout):
            client.recvfrom(8192)

        assert server.active_session_for(client.getsockname()) is None


def test_real_concurrent_handshakes_respect_aggregate_session_capacity(
    real_udpsec_endpoints,
):
    """Positive real-loopback evidence, real-client-concurrency variant:
    many real client sockets, on distinct OS threads, complete a full real
    signed handshake against ONE real secure server (one real asyncio
    receive loop, hence one OS thread on the SERVER side) concurrently,
    all racing to install an active session on a shared `SecureState`
    capped at `max_sessions=2`.

    IMPORTANT SCOPE NOTE: because the server side here is a single
    asyncio event loop, `install_session` calls can never actually
    interleave with each other at the Python level regardless of whether
    `SecureState` holds any lock at all -- asyncio's own single-threaded
    cooperative scheduling already serializes them, since `install_session`
    contains no `await`. This test therefore does NOT by itself prove
    `SecureState`'s lock is necessary; it proves the more modest but still
    useful property that capacity accounting stays exact when many
    concurrent CLIENT connections are funneled through one real server.
    The genuine cross-thread SecureState-mutation proof requires two or
    more real listeners (their own OS threads) sharing one state -- see
    `test_real_two_listeners_racing_commit_order_does_not_defeat_expiry`
    and the direct-thread tests in tests/test_secure_udp_helpers.py (e.g.
    `test_install_session_never_admits_two_threads_into_its_transaction_at_once`)
    for that.
    """
    endpoints = real_udpsec_endpoints
    state = endpoints.secure.SecureState(max_sessions=2)
    client_count = 6

    with _running_secure_server(
        endpoints.secure,
        endpoints.server_private_key,
        socket.AF_INET,
        "127.0.0.1",
        state=state,
    ) as server:
        clients = [
            _client_socket(socket.AF_INET, "127.0.0.1")
            for _ in range(client_count)
        ]
        try:
            barrier = threading.Barrier(client_count)
            results = [None] * client_count
            errors = [None] * client_count

            def worker(index):
                try:
                    barrier.wait(timeout=NETWORK_TIMEOUT)
                    results[index] = _perform_real_handshake(
                        endpoints, clients[index], server.remote_addr
                    )
                except BaseException as exc:  # noqa: BLE001 - captured for assertion
                    errors[index] = exc

            threads = [
                threading.Thread(target=worker, args=(index,))
                for index in range(client_count)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(NETWORK_TIMEOUT * 3)

            assert all(
                thread_error is None for thread_error in errors
            ), errors
            assert all(result is not None for result in results)

            stats = server.call_in_loop(state.stats)
            assert stats.sessions_created == client_count
            assert stats.sessions_capacity_evicted == client_count - 2
            assert stats.current_sessions == 2
            assert stats.peak_sessions == 2
            assert len(state._sessions) == 2

            # Cleanup and stats snapshotting remain safe and consistent
            # immediately afterward -- no deadlock, no corrupted state left
            # by the concurrent installs above.
            cleaned = server.call_in_loop(
                lambda: state.cleanup_expired_sessions(time.monotonic() + 3600)
            )
            assert len(cleaned) == 2
            final_stats = server.call_in_loop(state.stats)
            assert final_stats.current_sessions == 0
            assert final_stats.sessions_expired == 2
        finally:
            for client in clients:
                client.close()


def test_real_concurrent_pending_handshakes_respect_pending_capacity(
    real_udpsec_endpoints,
):
    """Pending-capacity equivalent of the aggregate-active-capacity test
    above, with the same scope note: many real client sockets send only
    their initial ClientHello (never completing the confirmation round-
    trip) concurrently against ONE real server (one asyncio event loop, so
    `install_pending_session` calls cannot actually interleave with each
    other regardless of locking) whose shared `SecureState` caps
    `max_pending_sessions=2`. This proves pending-capacity accounting
    stays exact under concurrent CLIENT load against one server -- not
    that `SecureState`'s lock is required, which needs two or more real
    listener threads (see the note on the active-capacity test above)."""
    endpoints = real_udpsec_endpoints
    state = endpoints.secure.SecureState(max_pending_sessions=2)
    client_count = 6

    with _running_secure_server(
        endpoints.secure,
        endpoints.server_private_key,
        socket.AF_INET,
        "127.0.0.1",
        state=state,
    ) as server:
        clients = [
            _client_socket(socket.AF_INET, "127.0.0.1")
            for _ in range(client_count)
        ]
        try:
            barrier = threading.Barrier(client_count)
            errors = [None] * client_count

            def send_hello_only(index, client):
                station_id = STATION_ID
                timestamp = int(time.time())
                client_random = os.urandom(32)
                client_ephemeral_private_key = ec.generate_private_key(
                    ec.SECP256R1()
                )
                client_ephemeral_public_key = (
                    client_ephemeral_private_key.public_key().public_bytes(
                        encoding=serialization.Encoding.X962,
                        format=serialization.PublicFormat.CompressedPoint,
                    )
                )
                digest = endpoints.proxy.build_client_auth_digest(
                    protocol_version=UDPSEC_PROTOCOL_VERSION,
                    station_id=station_id,
                    timestamp=timestamp,
                    client_random=client_random,
                    client_ephemeral_public_key=client_ephemeral_public_key,
                )
                signature = endpoints.proxy.sign_transcript_digest(
                    endpoints.station_private_key, digest
                )
                hello = ClientHello(
                    protocol_version=UDPSEC_PROTOCOL_VERSION,
                    station_id=station_id,
                    timestamp=timestamp,
                    client_random=client_random,
                    client_ephemeral_public_key=client_ephemeral_public_key,
                    client_signature=signature,
                )
                try:
                    barrier.wait(timeout=NETWORK_TIMEOUT)
                    client.sendto(
                        build_client_hello_packet(hello), server.remote_addr
                    )
                    client.recvfrom(8192)
                except BaseException as exc:  # noqa: BLE001
                    errors[index] = exc

            threads = [
                threading.Thread(
                    target=send_hello_only, args=(index, clients[index])
                )
                for index in range(client_count)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(NETWORK_TIMEOUT * 3)

            assert all(
                thread_error is None for thread_error in errors
            ), errors

            def snapshot():
                return state.stats(), len(state._pending_sessions)

            stats, pending_count = server.call_in_loop(snapshot)
            assert stats.pending_sessions_created == client_count
            assert (
                stats.pending_sessions_capacity_evicted
                == client_count - 2
            )
            assert stats.current_pending_sessions == 2
            assert stats.peak_pending_sessions == 2
            assert pending_count == 2
        finally:
            for client in clients:
                client.close()


def test_r6_sustained_pending_replacement_churn_via_real_secure_receive_path(
    real_udpsec_endpoints,
):
    """Blocker A (R6), reproduced through the real, production
    `_secure_server_loop` receive path with real signed ClientHello
    packets from one physical client address -- not direct `SecureState`
    manipulation. Matches the audit's own real-path reproduction (100
    ClientHellos, one live pending candidate, 100 heap entries): before
    this fix, a same-relation pending replacement's stale heap entry
    (keyed only by relation, not by the specific candidate incarnation)
    would be found "still occupied" by whatever candidate currently held
    that relation and recompute/reschedule THAT candidate's deadline
    rather than being discarded -- so the heap grew by one entry per
    ClientHello despite only one pending candidate ever being live."""
    endpoints = real_udpsec_endpoints
    state = endpoints.secure.SecureState(
        pending_session_ttl=3.0, max_pending_sessions=1
    )
    replacement_count = 100
    # A real socket round-trip is far faster than a 3-second TTL, so a
    # real wall clock would never actually reach cleanup due-ness within
    # this test -- a shared gated clock lets each ClientHello advance
    # time by exactly one second, deterministically reproducing the
    # audit's own "replacement every second" pacing.
    clock = _GatedClock(0.0)

    with _running_secure_server(
        endpoints.secure,
        endpoints.server_private_key,
        socket.AF_INET,
        "127.0.0.1",
        state=state,
        monotonic_clock=clock,
    ) as server:
        client = _client_socket(socket.AF_INET, "127.0.0.1")
        try:
            for step in range(replacement_count):
                clock.set(float(step))
                client_random = os.urandom(32)
                client_ephemeral_private_key = ec.generate_private_key(
                    ec.SECP256R1()
                )
                client_ephemeral_public_key = (
                    client_ephemeral_private_key.public_key().public_bytes(
                        encoding=serialization.Encoding.X962,
                        format=serialization.PublicFormat.CompressedPoint,
                    )
                )
                digest = endpoints.proxy.build_client_auth_digest(
                    protocol_version=UDPSEC_PROTOCOL_VERSION,
                    station_id=STATION_ID,
                    timestamp=int(time.time()),
                    client_random=client_random,
                    client_ephemeral_public_key=client_ephemeral_public_key,
                )
                signature = endpoints.proxy.sign_transcript_digest(
                    endpoints.station_private_key, digest
                )
                hello = ClientHello(
                    protocol_version=UDPSEC_PROTOCOL_VERSION,
                    station_id=STATION_ID,
                    timestamp=int(time.time()),
                    client_random=client_random,
                    client_ephemeral_public_key=client_ephemeral_public_key,
                    client_signature=signature,
                )
                client.sendto(
                    build_client_hello_packet(hello), server.remote_addr
                )
                client.recvfrom(8192)

            def snapshot():
                return (
                    state.stats(),
                    len(state._pending_sessions),
                    len(state._pending_expiry_heap),
                )

            stats, pending_count, heap_size = server.call_in_loop(snapshot)
            assert stats.pending_sessions_created == replacement_count
            assert pending_count == 1
            assert heap_size <= 5, (
                f"pending expiry heap retained {heap_size} entries after "
                f"{replacement_count} real ClientHello replacements from "
                "one address"
            )
        finally:
            client.close()


def test_ipv6_remote_comparison_uses_ip_port_and_scope_from_four_tuple(
    real_udpsec_endpoints,
):
    """Structured IPv6 path equality: flowinfo (tuple index 2) never
    distinguishes an otherwise-identical path, but scope_id (index 3) is
    significant because it disambiguates link-local addresses. Global-
    unicast/scope-0 behavior is unchanged."""
    proxy = real_udpsec_endpoints.proxy
    remote = ("::1", 19999, 7, 11)

    assert proxy.remote_addresses_match(("::1", 19999, 0, 11), remote)
    assert proxy.remote_addresses_match(("::1", 19999, 99, 11), remote)
    assert not proxy.remote_addresses_match(("::1", 19999, 7, 0), remote)
    assert not proxy.remote_addresses_match(("::1", 19999, 7, 12), remote)
    assert not proxy.remote_addresses_match(("::1", 20000, 7, 11), remote)
    assert not proxy.remote_addresses_match(("::2", 19999, 7, 11), remote)

    global_remote = ("2001:db8::1", 19999, 0, 0)
    assert proxy.remote_addresses_match(
        ("2001:db8::1", 19999, 5, 0), global_remote
    )


def test_real_listener_survives_deterministic_client_hello_corpus(
    real_udpsec_endpoints,
):
    endpoints = real_udpsec_endpoints
    now = int(time.time())
    valid_packet, valid_hello, _ = _signed_client_hello(
        endpoints,
        timestamp=now,
    )
    valid_fields = valid_packet.split(b"|")

    stale_packet, _, _ = _signed_client_hello(
        endpoints,
        timestamp=now - 31,
        client_random=b"\x32" * 32,
        ephemeral_scalar=18,
    )

    off_curve_point = b"\x02" + b"\xff" * 32
    off_curve_digest = udpsec_crypto.build_client_auth_digest(
        protocol_version=UDPSEC_PROTOCOL_VERSION,
        station_id=STATION_ID,
        timestamp=now,
        client_random=b"\x33" * 32,
        client_ephemeral_public_key=off_curve_point,
    )
    off_curve_packet = build_client_hello_packet(
        ClientHello(
            protocol_version=UDPSEC_PROTOCOL_VERSION,
            station_id=STATION_ID,
            timestamp=now,
            client_random=b"\x33" * 32,
            client_ephemeral_public_key=off_curve_point,
            client_signature=udpsec_crypto.sign_transcript_digest(
                endpoints.station_private_key,
                off_curve_digest,
            ),
        )
    )

    signature_r, signature_s = utils.decode_dss_signature(
        valid_hello.client_signature
    )
    high_s_signature = utils.encode_dss_signature(
        signature_r,
        udpsec_crypto._P256_ORDER - signature_s,
    )
    high_s_packet = build_client_hello_packet(
        ClientHello(
            protocol_version=valid_hello.protocol_version,
            station_id=valid_hello.station_id,
            timestamp=valid_hello.timestamp,
            client_random=valid_hello.client_random,
            client_ephemeral_public_key=(
                valid_hello.client_ephemeral_public_key
            ),
            client_signature=high_s_signature,
        )
    )

    wrong_identity_packet, _, _ = _signed_client_hello(
        endpoints,
        timestamp=now,
        client_random=b"\x34" * 32,
        ephemeral_scalar=19,
        identity_private_key=ec.derive_private_key(
            901,
            ec.SECP256R1(),
        ),
    )
    unknown_station_packet, _, _ = _signed_client_hello(
        endpoints,
        station_id="unknown_station",
        timestamp=now,
        client_random=b"\x35" * 32,
        ephemeral_scalar=20,
    )
    changed_field_packet = build_client_hello_packet(
        ClientHello(
            protocol_version=valid_hello.protocol_version,
            station_id=valid_hello.station_id,
            timestamp=valid_hello.timestamp,
            client_random=b"\x36" * 32,
            client_ephemeral_public_key=(
                valid_hello.client_ephemeral_public_key
            ),
            client_signature=valid_hello.client_signature,
        )
    )

    corpus = [
        ("empty", b""),
        ("prefix-only", b"NMEA-H"),
        (
            "old-format",
            b"NMEA-H|boat_001|"
            + str(now).encode()
            + b"|"
            + valid_fields[6],
        ),
        ("missing-field", b"|".join(valid_fields[:-1])),
        ("extra-field", valid_packet + b"|extra"),
        ("trailing-delimiter", valid_packet + b"|"),
        (
            "invalid-station-utf8",
            _replace_wire_field(valid_packet, 2, b"\xff"),
        ),
        (
            "embedded-station-nul",
            _replace_wire_field(valid_packet, 2, b"boat\x00_001"),
        ),
        (
            "leading-zero-timestamp",
            _replace_wire_field(
                valid_packet,
                3,
                b"0" + str(now).encode(),
            ),
        ),
        (
            "plus-timestamp",
            _replace_wire_field(
                valid_packet,
                3,
                b"+" + str(now).encode(),
            ),
        ),
        ("stale-signed-timestamp", stale_packet),
        ("empty-random", _replace_wire_field(valid_packet, 4, b"")),
        ("empty-point", _replace_wire_field(valid_packet, 5, b"")),
        ("empty-signature", _replace_wire_field(valid_packet, 6, b"")),
        (
            "bad-base64-alphabet",
            _replace_wire_field(valid_packet, 4, b"%%%%"),
        ),
        (
            "bad-base64-padding",
            _replace_wire_field(
                valid_packet,
                4,
                valid_fields[4] + b"=",
            ),
        ),
        (
            "oversized-bounded-base64",
            _replace_wire_field(
                valid_packet,
                4,
                base64.b64encode(b"\x41" * 4096),
            ),
        ),
        (
            "short-random",
            _replace_wire_field(
                valid_packet,
                4,
                base64.b64encode(b"\x41" * 31),
            ),
        ),
        (
            "long-random",
            _replace_wire_field(
                valid_packet,
                4,
                base64.b64encode(b"\x41" * 33),
            ),
        ),
        (
            "short-point",
            _replace_wire_field(
                valid_packet,
                5,
                base64.b64encode(b"\x02" + b"\x41" * 31),
            ),
        ),
        (
            "long-point",
            _replace_wire_field(
                valid_packet,
                5,
                base64.b64encode(b"\x02" + b"\x41" * 33),
            ),
        ),
        (
            "uncompressed-point-prefix",
            _replace_wire_field(
                valid_packet,
                5,
                base64.b64encode(b"\x04" + b"\x41" * 32),
            ),
        ),
        ("signed-off-curve-point", off_curve_packet),
        (
            "malformed-der",
            _replace_wire_field(
                valid_packet,
                6,
                base64.b64encode(b"\x30\x00"),
            ),
        ),
        ("high-s-signature", high_s_packet),
        ("wrong-identity-signature", wrong_identity_packet),
        ("changed-field-after-signing", changed_field_packet),
        ("unknown-station", unknown_station_packet),
        ("random-one-byte", b"\x01"),
        ("random-63-bytes", bytes(range(63))),
        ("random-1024-bytes", bytes(range(256)) * 4),
        (
            "random-listener-limit",
            (bytes(range(256)) * 32)[:8192],
        ),
        (
            "hello-prefix-at-listener-limit",
            b"NMEA-H|" + b"X" * (8192 - len(b"NMEA-H|")),
        ),
    ]
    assert len(corpus) == 33
    assert max(len(packet) for _, packet in corpus) == 8192

    with _running_secure_server(
        endpoints.secure,
        endpoints.server_private_key,
        socket.AF_INET,
        "127.0.0.1",
    ) as server:
        with _client_socket(socket.AF_INET, "127.0.0.1") as client:
            for _, packet in corpus:
                client.sendto(packet, server.remote_addr)

            client.settimeout(0.25)
            with pytest.raises(socket.timeout):
                client.recvfrom(8192)

            invalid_stats = server.call_in_loop(server.state.stats)
            assert invalid_stats.current_sessions == 0
            assert invalid_stats.current_pending_sessions == 0
            assert invalid_stats.current_handshake_replays == 0
            assert invalid_stats.handshake_replay_accepted == 0
            assert invalid_stats.handshake_replay_rejected == 0
            assert server.ingress.empty()

            client.settimeout(NETWORK_TIMEOUT)
            confirmed_session = _perform_real_handshake(
                endpoints,
                client,
                server.remote_addr,
            )
            assert isinstance(confirmed_session.key_material, SessionKeyMaterial)
            final_stats = server.call_in_loop(server.state.stats)
            assert final_stats.handshake_replay_accepted == 1
            assert final_stats.handshake_replay_rejected == 0
            assert final_stats.current_handshake_replays == 1
            assert final_stats.pending_sessions_created == 1
            assert final_stats.pending_sessions_promoted == 1
            assert final_stats.current_pending_sessions == 0
            assert final_stats.current_sessions == 1


def test_real_listener_rejects_data_corpus_without_state_mutation(
    real_udpsec_endpoints,
):
    endpoints = real_udpsec_endpoints
    with _running_secure_server(
        endpoints.secure,
        endpoints.server_private_key,
        socket.AF_INET,
        "127.0.0.1",
    ) as server:
        with _client_socket(socket.AF_INET, "127.0.0.1") as client:
            confirmed_session = _perform_real_handshake(
                endpoints,
                client,
                server.remote_addr,
            )
            session = _assert_single_confirmed_session(server)
            before = server.call_in_loop(server.state.stats)

            valid_shape = {
                "type": "nmea",
                "payload": "not-accepted",
                "timestamp": 1000,
                "source_id": STATION_ID,
            }
            valid_base = _encrypted_json_packet(
                endpoints.proxy,
                confirmed_session.key_material.client_to_server_key,
                _nonce(800),
                valid_shape,
                confirmed_session.session_locator)
            corrupted_nonce = bytearray(valid_base)
            corrupted_nonce[len(endpoints.proxy.DATA_PREFIX)] ^= 1
            corrupted_ciphertext = bytearray(valid_base)
            corrupted_ciphertext[-17] ^= 1
            corrupted_tag = bytearray(valid_base)
            corrupted_tag[-1] ^= 1

            invalid_semantic_nonce = _nonce(840)
            corpus = [
                ("prefix-only", endpoints.proxy.DATA_PREFIX),
                (
                    "short-tag",
                    endpoints.proxy.DATA_PREFIX
                    + _nonce(801)
                    + b"\x00" * 15,
                ),
                ("corrupted-nonce", bytes(corrupted_nonce)),
                ("corrupted-ciphertext", bytes(corrupted_ciphertext)),
                ("corrupted-tag", bytes(corrupted_tag)),
                (
                    "reverse-direction-key",
                    _encrypted_json_packet(
                        endpoints.proxy,
                        confirmed_session.key_material.server_to_client_key,
                        _nonce(802),
                        valid_shape,
                        confirmed_session.session_locator),
                ),
                (
                    "unrelated-key",
                    _encrypted_json_packet(
                        endpoints.proxy,
                        b"\xf0" * 32,
                        _nonce(803),
                        valid_shape,
                        confirmed_session.session_locator),
                ),
                (
                    "wrong-aad",
                    _encrypted_json_packet(
                        endpoints.proxy,
                        confirmed_session.key_material.client_to_server_key,
                        _nonce(804),
                        valid_shape,
                        confirmed_session.session_locator,
                        aad=b"wrong-aad"),
                ),
                (
                    "invalid-utf8",
                    _encrypted_plaintext_packet(
                        endpoints.proxy,
                        confirmed_session.key_material.client_to_server_key,
                        _nonce(805),
                        b"\xff",
                        confirmed_session.session_locator),
                ),
                (
                    "invalid-json",
                    _encrypted_plaintext_packet(
                        endpoints.proxy,
                        confirmed_session.key_material.client_to_server_key,
                        _nonce(806),
                        b"{",
                        confirmed_session.session_locator),
                ),
                *[
                    (
                        f"non-dict-{index}",
                        _encrypted_plaintext_packet(
                            endpoints.proxy,
                            confirmed_session.key_material.client_to_server_key,
                            _nonce(807 + index),
                            json.dumps(value).encode(),
                            confirmed_session.session_locator),
                    )
                    for index, value in enumerate(
                        (None, "text", 7, ["list"])
                    )
                ],
                (
                    "missing-type",
                    _encrypted_json_packet(
                        endpoints.proxy,
                        confirmed_session.key_material.client_to_server_key,
                        _nonce(811),
                        {
                            "payload": "missing-type",
                            "source_id": STATION_ID,
                        },
                        confirmed_session.session_locator),
                ),
                (
                    "unknown-type-reusable-nonce",
                    _encrypted_json_packet(
                        endpoints.proxy,
                        confirmed_session.key_material.client_to_server_key,
                        invalid_semantic_nonce,
                        {
                            "type": "unknown",
                            "source_id": STATION_ID,
                        },
                        confirmed_session.session_locator),
                ),
                (
                    "wrong-source",
                    _encrypted_json_packet(
                        endpoints.proxy,
                        confirmed_session.key_material.client_to_server_key,
                        _nonce(812),
                        {
                            "type": "nmea",
                            "payload": "wrong-source",
                            "source_id": "other-station",
                        },
                        confirmed_session.session_locator),
                ),
                (
                    "active-confirmation-sequence",
                    _encrypted_json_packet(
                        endpoints.proxy,
                        confirmed_session.key_material.client_to_server_key,
                        _nonce(813),
                        {
                            "type": "ping",
                            "seq": 0,
                            "timestamp": 1000,
                            "source_id": STATION_ID,
                        },
                        confirmed_session.session_locator),
                ),
                    *[
                        (
                            f"invalid-sequence-{index}",
                            _encrypted_json_packet(
                                endpoints.proxy,
                                confirmed_session.key_material.client_to_server_key,
                                _nonce(820 + index),
                                {
                                    "type": "ping",
                                    "seq": sequence,
                                    "timestamp": 1000,
                                    "source_id": STATION_ID,
                                },
                                confirmed_session.session_locator),
                        )
                        for index, sequence in enumerate(
                            (
                                True,
                                1.0,
                                "1",
                                [1],
                                {"value": 1},
                                None,
                            )
                        )
                    ],
                    (
                        "missing-sequence",
                        _encrypted_json_packet(
                            endpoints.proxy,
                            confirmed_session.key_material.client_to_server_key,
                            _nonce(826),
                            {
                                "type": "ping",
                                "timestamp": 1000,
                                "source_id": STATION_ID,
                            },
                            confirmed_session.session_locator),
                    ),
                    (
                        "missing-nmea-payload",
                        _encrypted_json_packet(
                            endpoints.proxy,
                            confirmed_session.key_material.client_to_server_key,
                            _nonce(827),
                            {
                                "type": "nmea",
                                "source_id": STATION_ID,
                        },
                            confirmed_session.session_locator),
                ),
                ("truncated-authenticated-packet", valid_base[:-1]),
                (
                    "authenticated-listener-limit-invalid-json",
                        _encrypted_plaintext_packet(
                            endpoints.proxy,
                            confirmed_session.key_material.client_to_server_key,
                            _nonce(828),
                            b"x" * 8141,
                            confirmed_session.session_locator),
                    ),
                ]
            assert len(corpus) == 28
            assert len(corpus[-1][1]) == 8192

            for _, packet in corpus:
                client.sendto(packet, server.remote_addr)

            accepted_payload = "!AIVDM,1,1,,A,after-adversarial,0*00"
            accepted_packet = _encrypted_json_packet(
                endpoints.proxy,
                confirmed_session.key_material.client_to_server_key,
                invalid_semantic_nonce,
                {
                    "type": "nmea",
                    "payload": accepted_payload,
                    "timestamp": 1000,
                    "source_id": STATION_ID,
                },
                confirmed_session.session_locator)
            client.sendto(accepted_packet, server.remote_addr)
            client.sendto(accepted_packet, server.remote_addr)
            endpoints.proxy.send_ping(
                client,
                server.remote_addr,
                confirmed_session.key_material.client_to_server_key,
                confirmed_session.session_locator,
                STATION_ID,
                41)

            frame = server.ingress.get()
            assert (
                decode_frame_slice(frame, 0, len(frame.payload))
                == accepted_payload
            )
            assert server.ingress.empty()
            received_sequences = []
            while 41 not in received_sequences:
                response_packet, response_addr = client.recvfrom(8192)
                assert endpoints.proxy.remote_addresses_match(
                    response_addr,
                    server.remote_addr,
                )
                response_message = (
                    endpoints.proxy.decrypt_secure_json_message(
                        response_packet,
                        confirmed_session.key_material.server_to_client_key,
                        confirmed_session.session_locator)
                )
                assert response_message.get("type") == "pong"
                received_sequences.append(response_message.get("seq"))
            client.settimeout(0.05)
            with pytest.raises(socket.timeout):
                client.recvfrom(8192)

            after = server.call_in_loop(server.state.stats)
            assert server.active_session_for(client.getsockname()) is session
            assert after.current_sessions == 1
            assert after.current_pending_sessions == 0
            assert after.sessions_touched == before.sessions_touched + 2
            assert (
                after.data_nonces_accepted
                == before.data_nonces_accepted + 2
            )
            assert after.data_nonce_replays == before.data_nonce_replays + 1
            assert (
                invalid_semantic_nonce
                in session.current_epoch.seen_data_nonces._live_by_key
            )
            assert all(
                _nonce(marker)
                not in session.current_epoch.seen_data_nonces._live_by_key
                for marker in (813, *range(820, 827))
            )
            assert received_sequences == [41]


def test_real_active_ping_timestamp_requires_exact_integer_before_mutation(
    real_udpsec_endpoints,
):
    endpoints = real_udpsec_endpoints
    with _running_secure_server(
        endpoints.secure,
        endpoints.server_private_key,
        socket.AF_INET,
        "127.0.0.1",
    ) as server:
        with _client_socket(socket.AF_INET, "127.0.0.1") as client:
            confirmed_session = _perform_real_handshake(
                endpoints,
                client,
                server.remote_addr,
            )
            session = _assert_single_confirmed_session(server)
            before = server.call_in_loop(server.state.stats)
            invalid_messages = [
                {
                    "type": "ping",
                    "seq": 51,
                    "source_id": STATION_ID,
                },
                *[
                    {
                        "type": "ping",
                        "seq": 51,
                        "timestamp": timestamp,
                        "source_id": STATION_ID,
                    }
                    for timestamp in (
                        True,
                        1.0,
                        "1000",
                        [1000],
                        {"value": 1000},
                        None,
                    )
                ],
            ]
            invalid_nonces = [
                _nonce(870 + index)
                for index in range(len(invalid_messages))
            ]

            for nonce, message in zip(
                invalid_nonces,
                invalid_messages,
                strict=True,
            ):
                client.sendto(
                    _encrypted_json_packet(
                        endpoints.proxy,
                        confirmed_session.key_material.client_to_server_key,
                        nonce,
                        message,
                        confirmed_session.session_locator),
                    server.remote_addr,
                )

            valid_nonce = _nonce(879)
            client.sendto(
                _encrypted_json_packet(
                    endpoints.proxy,
                    confirmed_session.key_material.client_to_server_key,
                    valid_nonce,
                    {
                        "type": "ping",
                        "seq": 52,
                        "timestamp": 1000,
                        "source_id": STATION_ID,
                    },
                    confirmed_session.session_locator),
                server.remote_addr,
            )

            packet, sender = client.recvfrom(8192)
            assert endpoints.proxy.remote_addresses_match(
                sender,
                server.remote_addr,
            )
            message = endpoints.proxy.decrypt_secure_json_message(
                packet,
                confirmed_session.key_material.server_to_client_key,
                confirmed_session.session_locator)
            assert message["type"] == "pong"
            assert message["seq"] == 52
            assert type(message["timestamp"]) is int
            assert message["source_id"] == STATION_ID
            client.settimeout(0.05)
            with pytest.raises(socket.timeout):
                client.recvfrom(8192)

            after = server.call_in_loop(server.state.stats)
            assert after.sessions_touched == before.sessions_touched + 1
            assert (
                after.data_nonces_accepted
                == before.data_nonces_accepted + 1
            )
            assert after.data_nonce_replays == before.data_nonce_replays
            assert (
                after.current_data_nonces
                == before.current_data_nonces + 1
            )
            assert valid_nonce in session.current_epoch.seen_data_nonces._live_by_key
            assert all(
                nonce not in session.current_epoch.seen_data_nonces._live_by_key
                for nonce in invalid_nonces
            )
            assert server.ingress.empty()


def test_real_active_ping_sequence_requires_exact_positive_integer(
    real_udpsec_endpoints,
):
    endpoints = real_udpsec_endpoints
    with _running_secure_server(
        endpoints.secure,
        endpoints.server_private_key,
        socket.AF_INET,
        "127.0.0.1",
    ) as server:
        with _client_socket(socket.AF_INET, "127.0.0.1") as client:
            confirmed_session = _perform_real_handshake(
                endpoints,
                client,
                server.remote_addr,
            )
            session = _assert_single_confirmed_session(server)
            before = server.call_in_loop(server.state.stats)
            negative_sequences = (-1, -(2**31))
            positive_sequences = (1, 2**31)
            sequences = (*negative_sequences, *positive_sequences)
            nonces = [
                _nonce(860 + index)
                for index in range(len(sequences))
            ]

            for nonce, sequence in zip(
                nonces,
                sequences,
                strict=True,
            ):
                client.sendto(
                    _encrypted_json_packet(
                        endpoints.proxy,
                        confirmed_session.key_material.client_to_server_key,
                        nonce,
                        {
                            "type": "ping",
                            "seq": sequence,
                            "timestamp": 1000,
                            "source_id": STATION_ID,
                        },
                        confirmed_session.session_locator),
                    server.remote_addr,
                )

            received_sequences = []
            while positive_sequences[-1] not in received_sequences:
                packet, sender = client.recvfrom(8192)
                assert endpoints.proxy.remote_addresses_match(
                    sender,
                    server.remote_addr,
                )
                message = endpoints.proxy.decrypt_secure_json_message(
                    packet,
                    confirmed_session.key_material.server_to_client_key,
                    confirmed_session.session_locator)
                assert message.get("type") == "pong"
                received_sequences.append(message.get("seq"))
            client.settimeout(0.05)
            with pytest.raises(socket.timeout):
                client.recvfrom(8192)

            after = server.call_in_loop(server.state.stats)
            assert received_sequences == list(positive_sequences)
            assert after.sessions_touched == before.sessions_touched + 2
            assert (
                after.data_nonces_accepted
                == before.data_nonces_accepted + 2
            )
            assert all(
                nonce not in session.current_epoch.seen_data_nonces._live_by_key
                for nonce in nonces[: len(negative_sequences)]
            )
            assert all(
                nonce in session.current_epoch.seen_data_nonces._live_by_key
                for nonce in nonces[len(negative_sequences) :]
            )
            assert server.ingress.empty()


@pytest.mark.parametrize(
    "invalid_sequence",
    (True, 1.0, "1", [1], {"value": 1}, None),
)
def test_proxy_rejects_non_integer_authenticated_pong_sequence(
    real_udpsec_endpoints,
    invalid_sequence,
):
    proxy = real_udpsec_endpoints.proxy
    key = b"\x71" * 32
    locator = _fresh_test_locator()
    remote_addr = ("127.0.0.1", 19999)
    packet = _encrypted_json_packet(
        proxy,
        key,
        _nonce(900),
        {
            "type": "pong",
            "seq": invalid_sequence,
            "timestamp": 1000,
            "source_id": STATION_ID,
        },
        locator,
    )

    assert proxy.handle_server_packet(
        packet,
        remote_addr,
        remote_addr,
        key,
        locator,
        STATION_ID,
        1,
    ) == proxy.SERVER_PACKET_IGNORED


def test_expired_pending_handle_cannot_replace_live_active_session(
    real_udpsec_endpoints,
):
    secure = real_udpsec_endpoints.secure
    state = secure.SecureState(pending_session_ttl=5)
    address = ("127.0.0.1", 51001)
    relation_key = secure._EndpointPeerKey(
        secure._new_endpoint_token(),
        address,
    )
    active = state.install_session(
        relation_key,
        STATION_ID,
        _fresh_test_locator(),
        AESGCM(b"\x61" * 32),
        AESGCM(b"\x62" * 32),
        0,
    )
    pending = state.install_pending_session(
        relation_key,
        STATION_ID,
        _fresh_test_locator(),
        AESGCM(b"\x63" * 32),
        AESGCM(b"\x64" * 32),
        1,
    )

    assert state.promote_pending_session(pending, 6) is None
    assert state.get_active_session(active._session_key, 6) is active
    assert state.get_pending_session(relation_key, 6) is None
    stats = state.stats()
    assert stats.pending_sessions_expired == 1
    assert stats.pending_sessions_promoted == 0
    assert stats.sessions_replaced == 0
    assert stats.current_sessions == 1
    assert stats.current_pending_sessions == 0


def test_session_state_retains_only_directional_cipher_contexts(
    real_udpsec_endpoints,
):
    secure = real_udpsec_endpoints.secure
    state = secure.SecureState()
    address = ("127.0.0.1", 51002)
    relation_key = secure._EndpointPeerKey(
        secure._new_endpoint_token(),
        address,
    )
    active = state.install_session(
        relation_key,
        STATION_ID,
        _fresh_test_locator(),
        AESGCM(b"\x65" * 32),
        AESGCM(b"\x66" * 32),
        10,
    )
    pending = state.install_pending_session(
        relation_key,
        STATION_ID,
        _fresh_test_locator(),
        AESGCM(b"\x67" * 32),
        AESGCM(b"\x68" * 32),
        11,
    )

    assert active._session_key.endpoint_token is relation_key.endpoint_token
    assert pending._relation_key is relation_key
    assert active.path_state.active_path == address
    assert pending._address == address
    assert set(vars(active)) == {
        "_session_key",
        "station_id",
        "created_at",
        "last_seen",
        "session_handle",
        "assembly_namespace",
        "current_epoch",
        "path_state",
    }
    assert set(vars(pending)) == {
        "_relation_key",
        "_address",
        "station_id",
        "session_locator",
        "created_at",
        "current_epoch",
    }
    assert set(vars(active.current_epoch)) == {
        "client_to_server_aesgcm",
        "server_to_client_aesgcm",
        "seen_data_nonces",
        "created_at",
    }
    assert set(vars(pending.current_epoch)) == {
        "client_to_server_aesgcm",
        "server_to_client_aesgcm",
        "seen_data_nonces",
        "created_at",
    }
    assert set(vars(active.path_state)) == {"active_path"}
    forbidden_field_fragments = {
        "ephemeral",
        "shared_secret",
        "transcript",
        "identity_private",
        "client_to_server_key",
        "server_to_client_key",
    }
    for retained in (
        active,
        pending,
        active.current_epoch,
        pending.current_epoch,
    ):
        assert not (
            forbidden_field_fragments
            & set(vars(retained))
        )
        # session_locator, session_handle, and assembly_namespace are the
        # intentionally public, non-credential bytes values retained
        # directly: session_locator is server-minted and transcript-bound,
        # while session_handle/assembly_namespace are opaque identifiers
        # reserved through the shared `_SESSION_IDENTITY_REGISTRY` (see
        # `core.session_identity_registry`) -- none of the three is ever
        # proof of authentication or usable as key material. Everything
        # else bytes-typed here would be raw key/secret material.
        non_identifier_fields = {"session_locator", "session_handle", "assembly_namespace"}
        non_identifier_values = {
            name: value
            for name, value in vars(retained).items()
            if name not in non_identifier_fields
        }
        assert not any(
            isinstance(value, bytes)
            for value in non_identifier_values.values()
        )

    key_material = SessionKeyMaterial(b"\x69" * 32, b"\x6a" * 32)
    material_repr = repr(key_material)
    assert key_material.client_to_server_key.hex() not in material_repr
    assert key_material.server_to_client_key.hex() not in material_repr

    client_hello = ClientHello(
        protocol_version=UDPSEC_PROTOCOL_VERSION,
        station_id=STATION_ID,
        timestamp=1,
        client_random=b"\x01" * 32,
        client_ephemeral_public_key=b"\x02" + b"\x02" * 32,
        client_signature=b"client-secret-signature",
    )
    server_hello = ServerHello(
        protocol_version=UDPSEC_PROTOCOL_VERSION,
        session_locator=_fresh_test_locator(),
        server_random=b"\x03" * 32,
        server_ephemeral_public_key=b"\x03" + b"\x04" * 32,
        server_signature=b"server-secret-signature",
    )
    assert "client-secret-signature" not in repr(client_hello)
    assert "server-secret-signature" not in repr(server_hello)


def test_controlled_ephemeral_pairs_provide_forward_secrecy_evidence():
    client_identity = ec.derive_private_key(101, ec.SECP256R1())
    server_identity = ec.derive_private_key(202, ec.SECP256R1())
    first = _compose_controlled_handshake(
        client_identity,
        server_identity,
        client_ephemeral_scalar=301,
        server_ephemeral_scalar=302,
    )
    second = _compose_controlled_handshake(
        client_identity,
        server_identity,
        client_ephemeral_scalar=303,
        server_ephemeral_scalar=304,
    )

    assert udpsec_crypto.verify_transcript_signature(
        client_identity.public_key(),
        first.client_hello.client_signature,
        first.client_digest,
    )
    assert udpsec_crypto.verify_transcript_signature(
        server_identity.public_key(),
        first.server_hello.server_signature,
        first.server_digest,
    )
    assert first.shared_secret != second.shared_secret
    assert (
        first.key_material.client_to_server_key
        != second.key_material.client_to_server_key
    )
    assert (
        first.key_material.server_to_client_key
        != second.key_material.server_to_client_key
    )

    captured_client = parse_client_hello_packet(first.client_packet)
    captured_server = parse_server_hello_packet(first.server_packet)
    candidate_secrets_available_after_identity_compromise = (
        udpsec_crypto.derive_ephemeral_shared_secret(
            client_identity,
            udpsec_crypto.parse_ephemeral_public_key(
                captured_server.server_ephemeral_public_key
            ),
        ),
        udpsec_crypto.derive_ephemeral_shared_secret(
            server_identity,
            udpsec_crypto.parse_ephemeral_public_key(
                captured_client.client_ephemeral_public_key
            ),
        ),
        udpsec_crypto.derive_ephemeral_shared_secret(
            client_identity,
            server_identity.public_key(),
        ),
    )
    assert first.shared_secret not in (
        candidate_secrets_available_after_identity_compromise
    )
    for candidate_secret in (
        candidate_secrets_available_after_identity_compromise
    ):
        assert udpsec_crypto.derive_session_key_material(
            candidate_secret,
            first.transcript_hash,
        ) != first.key_material


def _source_tree(relative_path):
    return ast.parse(
        (ROOT / relative_path).read_text(encoding="utf-8"),
        filename=relative_path,
    )


def _definition(tree, name):
    return next(
        node
        for node in tree.body
        if isinstance(
            node,
            (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
        )
        and node.name == name
    )


def _dotted_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def _calls(node, dotted_name):
    return [
        call
        for call in ast.walk(node)
        if isinstance(call, ast.Call)
        and _dotted_name(call.func) == dotted_name
    ]


def _referenced_identifiers(node):
    identifiers = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            identifiers.add(child.id)
        elif isinstance(child, ast.Attribute):
            identifiers.add(child.attr)
        elif isinstance(child, ast.arg):
            identifiers.add(child.arg)
        elif isinstance(
            child,
            (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
        ):
            identifiers.add(child.name)
    return identifiers


def test_static_crypto_ownership_and_ephemeral_runtime_call_sites():
    sources = {
        "aismixer_secure.py": _source_tree("aismixer_secure.py"),
        "nmea_sproxy/nmea_sproxy.py": _source_tree(
            "nmea_sproxy/nmea_sproxy.py"
        ),
        "core/udpsec_crypto.py": _source_tree("core/udpsec_crypto.py"),
        "core/udpsec_protocol.py": _source_tree(
            "core/udpsec_protocol.py"
        ),
    }
    primitive_call_names = {
        "exchange",
        "HKDF",
        "ECDH",
        "ECDSA",
        "Prehashed",
        "decode_dss_signature",
        "encode_dss_signature",
        "sign",
        "verify",
    }
    primitive_hits = [
        (path, _dotted_name(call.func).rsplit(".", 1)[-1])
        for path, tree in sources.items()
        for call in ast.walk(tree)
        if isinstance(call, ast.Call)
        and _dotted_name(call.func).rsplit(".", 1)[-1]
        in primitive_call_names
    ]
    assert primitive_hits
    assert {path for path, _ in primitive_hits} == {
        "core/udpsec_crypto.py"
    }

    server_tree = sources["aismixer_secure.py"]
    proxy_tree = sources["nmea_sproxy/nmea_sproxy.py"]
    for runtime_tree in (server_tree, proxy_tree):
        assert not [
            call
            for call in ast.walk(runtime_tree)
            if isinstance(call, ast.Call)
            and _dotted_name(call.func).endswith(".exchange")
        ]

    server_derivation = _calls(
        _definition(server_tree, "_build_server_handshake"),
        "derive_ephemeral_shared_secret",
    )
    proxy_derivation = _calls(
        _definition(proxy_tree, "perform_handshake"),
        "derive_ephemeral_shared_secret",
    )
    assert len(server_derivation) == 1
    assert len(proxy_derivation) == 1
    assert [
        _dotted_name(argument)
        for argument in server_derivation[0].args
    ] == [
        "server_ephemeral_private_key",
        "client_ephemeral_public_key",
    ]
    assert [
        _dotted_name(argument)
        for argument in proxy_derivation[0].args
    ] == [
        "client_ephemeral_private_key",
        "server_ephemeral_public_key",
    ]


def test_static_runtime_has_no_legacy_helpers_or_secret_logging():
    runtime_trees = (
        _source_tree("aismixer_secure.py"),
        _source_tree("nmea_sproxy/nmea_sproxy.py"),
    )
    obsolete_identifiers = {
        "CONTEXT_STRING",
        "KEEPALIVE_PREFIX",
        "NOSESSION_PREFIX",
        "SERVER_PACKET_NO_SESSION",
        "SESSION_END_NOSESSION",
        "build_no_session_hint",
        "build_current_handshake_payload",
        "build_handshake_context_v1",
        "build_session_transcript_v1",
        "compute_session_hash",
        "derive_session_key",
        "handle_keepalive",
        "is_no_session_hint",
        "parse_keepalive_packet",
        "parse_keepalive_station_id",
        "server_pub_bytes",
        "sign_message",
        "verify_signature",
    }
    logging_function_names = {
        "print",
        "debug",
        "info",
        "warning",
        "error",
        "exception",
        "critical",
        "log",
    }
    secret_identifiers = {
        "SessionKeyMaterial",
        "client_to_server_key",
        "key_material",
        "private_scalar",
        "r",
        "s",
        "server_to_client_key",
        "session_key_material",
        "shared_secret",
        "session_transcript_hash",
    }
    secret_phrases = {
        "client to server key",
        "private scalar",
        "server to client key",
        "session key material",
        "shared secret",
        "signature scalar",
        "transcript hash",
    }

    for tree in runtime_trees:
        identifiers = _referenced_identifiers(tree)
        # "session_key" is now a legitimate, non-secret identifier: an
        # _EndpointSessionKey(endpoint_token, session_locator) is a lookup
        # identity, never cryptographic key material, so it is
        # deliberately absent from secret_identifiers/secret_phrases
        # below rather than banned outright here.
        assert not (obsolete_identifiers & identifiers)
        for obsolete_wire_text in ("KEEPALIVE", "NOSESSION"):
            assert not [
                value
                for value in ast.walk(tree)
                if isinstance(value, ast.Constant)
                and isinstance(value.value, str)
                and obsolete_wire_text in value.value
            ]

        for call in (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and _dotted_name(node.func).rsplit(".", 1)[-1]
            in logging_function_names
        ):
            logged_nodes = [
                *call.args,
                *(keyword.value for keyword in call.keywords),
            ]
            logged_identifiers = set().union(
                *(
                    _referenced_identifiers(node)
                    for node in logged_nodes
                ),
                set(),
            )
            assert not (secret_identifiers & logged_identifiers)
            logged_strings = {
                child.value.lower().replace("_", " ").replace("-", " ")
                for node in logged_nodes
                for child in ast.walk(node)
                if isinstance(child, ast.Constant)
                and isinstance(child.value, str)
            }
            assert not {
                phrase
                for phrase in secret_phrases
                if any(phrase in text for text in logged_strings)
            }


def test_static_server_admission_order_policy_and_pending_fallback():
    server_tree = _source_tree("aismixer_secure.py")
    server_loop = _definition(server_tree, "_secure_server_loop")
    hello_branch = next(
        node
        for node in ast.walk(server_loop)
        if isinstance(node, ast.If)
        and _calls(node.test, "data.startswith")
        and any(
            isinstance(argument, ast.Name)
            and argument.id == "CLIENT_HELLO_PREFIX"
            for call in _calls(node.test, "data.startswith")
            for argument in call.args
        )
    )

    ordered_calls = (
        "parse_client_hello_packet",
        "wall_now",
        "AUTHORIZED_KEYS.get",
        "verify_transcript_signature",
        "parse_ephemeral_public_key",
        "state_owner.accept_handshake_replay",
        "_build_server_handshake",
        "state_owner.install_pending_session",
        "sock.sendto",
    )
    ordered_lines = []
    for call_name in ordered_calls:
        matching = _calls(hello_branch, call_name)
        assert matching, call_name
        ordered_lines.append(min(call.lineno for call in matching))
    assert ordered_lines == sorted(ordered_lines)
    assert not _calls(hello_branch, "state_owner.install_session")

    policy_guard = next(
        node
        for node in ast.walk(server_loop)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.UnaryOp)
        and isinstance(node.test.op, ast.Not)
        and isinstance(node.test.operand, ast.Call)
        and _dotted_name(node.test.operand.func) == "policy.allows"
    )
    assert len(policy_guard.body) == 1
    assert isinstance(policy_guard.body[0], ast.Continue)
    post_policy_calls = (
        _calls(server_loop, "monotonic_now")
        + _calls(server_loop, "parse_client_hello_packet")
        + _calls(server_loop, "verify_transcript_signature")
        + _calls(server_loop, "parse_ephemeral_public_key")
        + _calls(server_loop, "state_owner.accept_handshake_replay")
    )
    assert post_policy_calls
    assert policy_guard.end_lineno < min(
        call.lineno for call in post_policy_calls
    )

    pending_decrypt_tries = [
        node
        for node in ast.walk(server_loop)
        if isinstance(node, ast.Try)
        and _calls(
            node,
            "pending.current_epoch.client_to_server_aesgcm.decrypt",
        )
    ]
    pending_decrypt_try = min(
        pending_decrypt_tries,
        key=lambda node: node.end_lineno - node.lineno,
    )
    assert [
        _dotted_name(handler.type)
        for handler in pending_decrypt_try.handlers
    ] == ["InvalidTag"]


def test_legacy_handshake_wire_shapes_remain_rejected():
    with pytest.raises(ValueError):
        parse_client_hello_packet(
            b"NMEA-H|boat_001|1700000000|"
            + base64.b64encode(b"old-signature")
        )
    with pytest.raises(ValueError):
        parse_server_hello_packet(
            b"OK|" + base64.b64encode(b"old-signature")
        )
