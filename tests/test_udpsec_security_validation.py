import asyncio
import ast
import base64
import importlib.util
import io
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
    ServerHello,
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


class _LoopbackSecureServer:
    def __init__(self, secure, server_private_key, family, host):
        self.secure = secure
        self.server_private_key = server_private_key
        self.host = host
        self.socket = socket.socket(family, socket.SOCK_DGRAM)
        self.state = secure.SecureState()
        self.ingress = _ThreadSafeIngressSink()
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
                0,
                sec_input_id="loopback-validation",
                state=self.state,
                server_private_key=self.server_private_key,
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
def _running_secure_server(secure, server_private_key, family, host):
    server = _LoopbackSecureServer(
        secure,
        server_private_key,
        family,
        host,
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


def _encrypted_plaintext_packet(
    proxy,
    key,
    nonce,
    plaintext,
    *,
    aad=None,
):
    associated_data = proxy.DATA_AAD if aad is None else aad
    return (
        proxy.DATA_PREFIX
        + nonce
        + AESGCM(key).encrypt(nonce, plaintext, associated_data)
    )


def _encrypted_json_packet(proxy, key, nonce, message, *, aad=None):
    return _encrypted_plaintext_packet(
        proxy,
        key,
        nonce,
        json.dumps(message, separators=(",", ":")).encode(),
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
):
    if timestamp is None:
        timestamp = int(time.time())
    if client_random is None:
        client_random = b"\x31" * 32
    if identity_private_key is None:
        identity_private_key = endpoints.station_private_key
    ephemeral_private_key = ec.derive_private_key(
        ephemeral_scalar,
        ec.SECP256R1(),
    )
    ephemeral_public_key = udpsec_crypto.serialize_ephemeral_public_key(
        ephemeral_private_key.public_key()
    )
    digest = udpsec_crypto.build_client_auth_digest(
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
):
    station_id = STATION_ID
    timestamp = 1_700_000_000
    client_random = b"\x51" * 32
    server_random = b"\x52" * 32
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
        station_id=station_id,
        timestamp=timestamp,
        client_random=client_random,
        client_ephemeral_public_key=client_ephemeral_public_key,
        client_signature=client_signature,
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
        station_id=station_id,
        timestamp=timestamp,
        client_random=client_random,
        client_ephemeral_public_key=client_ephemeral_public_key,
        client_signature=client_signature,
        server_random=server_random,
        server_ephemeral_public_key=server_ephemeral_public_key,
        server_signature=server_signature,
    )
    key_material = udpsec_crypto.derive_session_key_material(
        client_shared_secret,
        transcript_hash,
    )
    client_hello = ClientHello(
        station_id=station_id,
        timestamp=timestamp,
        client_random=client_random,
        client_ephemeral_public_key=client_ephemeral_public_key,
        client_signature=client_signature,
    )
    server_hello = ServerHello(
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
        shared_secret=client_shared_secret,
        transcript_hash=transcript_hash,
        key_material=key_material,
    )


def _perform_real_handshake(endpoints, client, remote_addr):
    key_material = endpoints.proxy.perform_handshake(
        client,
        {"station_id": STATION_ID},
        endpoints.station_private_key,
        endpoints.server_public_key,
        remote_addr,
    )
    assert isinstance(key_material, SessionKeyMaterial)
    return key_material


def _assert_single_confirmed_session(server):
    stats = server.state.stats()
    assert stats.current_sessions == 1
    assert stats.current_pending_sessions == 0
    assert len(server.state._sessions) == 1
    return next(iter(server.state._sessions.values()))


def _receive_authenticated_pong(
    proxy,
    client,
    remote_addr,
    key_material,
    sequence,
):
    packet, sender = client.recvfrom(8192)
    assert proxy.remote_addresses_match(sender, remote_addr)
    assert proxy.handle_server_packet(
        packet,
        sender,
        remote_addr,
        key_material.server_to_client_key,
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
            key_material = _perform_real_handshake(
                endpoints,
                client,
                server.remote_addr,
            )

            session = _assert_single_confirmed_session(server)
            assert session._address[:2] == client.getsockname()[:2]
            if family == socket.AF_INET6:
                assert len(session._address) == 4
                assert len(server.remote_addr) == 4
            assert (
                key_material.client_to_server_key
                != key_material.server_to_client_key
            )
            assert (
                session.client_to_server_aesgcm
                is not session.server_to_client_aesgcm
            )

            endpoints.proxy.send_udpsec_nmea_sentence(
                NMEA_PAYLOAD,
                client,
                {"station_id": STATION_ID},
                key_material.client_to_server_key,
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
                key_material.client_to_server_key,
                STATION_ID,
                sequence,
            )
            pong_packet, _ = _receive_authenticated_pong(
                endpoints.proxy,
                client,
                server.remote_addr,
                key_material,
                sequence,
            )
            with pytest.raises(InvalidTag):
                endpoints.proxy.decrypt_secure_json_message(
                    pong_packet,
                    key_material.client_to_server_key,
                )


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
            assert first_session._address == client_addr

            second = _perform_real_handshake(
                endpoints,
                client,
                server.remote_addr,
            )
            assert client.getsockname() == client_addr
            second_session = _assert_single_confirmed_session(server)
            assert second_session._address == client_addr
            assert second_session is not first_session
            assert (
                first.client_to_server_key
                != second.client_to_server_key
            )
            assert (
                first.server_to_client_key
                != second.server_to_client_key
            )
            assert (
                second.client_to_server_key
                != second.server_to_client_key
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
                    first.client_to_server_key,
                ),
                server.remote_addr,
            )

            new_payload = "!AIVDM,1,1,,A,new-session-payload,0*00"
            endpoints.proxy.send_udpsec_nmea_sentence(
                new_payload,
                client,
                {"station_id": STATION_ID},
                second.client_to_server_key,
                server.remote_addr,
            )
            sequence = 23
            endpoints.proxy.send_ping(
                client,
                server.remote_addr,
                second.client_to_server_key,
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
                first.server_to_client_key,
                STATION_ID,
                sequence,
            ) == endpoints.proxy.SERVER_PACKET_IGNORED
            with pytest.raises(InvalidTag):
                endpoints.proxy.decrypt_secure_json_message(
                    pong_packet,
                    first.server_to_client_key,
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
                assert expired == [client.getsockname()]
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
            assert isinstance(second_result, SessionKeyMaterial)
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
            assert set(server.state._sessions) == {
                client_a.getsockname(),
                client_b.getsockname(),
            }
            assert material_a != material_b

            shared_nonce = _nonce(700)
            payload_a = "!AIVDM,1,1,,A,client-a,0*00"
            payload_b = "!AIVDM,1,1,,A,client-b,0*00"
            packet_a = _encrypted_json_packet(
                endpoints.proxy,
                material_a.client_to_server_key,
                shared_nonce,
                {
                    "type": "nmea",
                    "payload": payload_a,
                    "timestamp": 1000,
                    "source_id": STATION_ID,
                },
            )
            packet_b = _encrypted_json_packet(
                endpoints.proxy,
                material_b.client_to_server_key,
                shared_nonce,
                {
                    "type": "nmea",
                    "payload": payload_b,
                    "timestamp": 1000,
                    "source_id": STATION_ID,
                },
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

            session_a = server.state._sessions[client_a.getsockname()]
            session_b = server.state._sessions[client_b.getsockname()]
            assert shared_nonce in session_a.seen_data_nonces._live_by_key
            assert shared_nonce in session_b.seen_data_nonces._live_by_key

            before_cross_key = server.call_in_loop(server.state.stats)
            cross_key_nonce = _nonce(701)
            cross_key_packet = _encrypted_json_packet(
                endpoints.proxy,
                material_a.client_to_server_key,
                cross_key_nonce,
                {
                    "type": "nmea",
                    "payload": "must-not-authenticate-for-b",
                    "timestamp": 1000,
                    "source_id": STATION_ID,
                },
            )
            client_b.sendto(cross_key_packet, server.remote_addr)
            endpoints.proxy.send_ping(
                client_b,
                server.remote_addr,
                material_b.client_to_server_key,
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
                not in session_b.seen_data_nonces._live_by_key
            )
            assert server.ingress.empty()

            before_port_change = server.call_in_loop(server.state.stats)
            client_c.sendto(packet_a, server.remote_addr)
            no_session, sender = client_c.recvfrom(8192)
            assert endpoints.proxy.remote_addresses_match(
                sender,
                server.remote_addr,
            )
            assert endpoints.proxy.is_no_session_hint(no_session)
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
            assert isinstance(material_c, SessionKeyMaterial)
            assert len(server.state._sessions) == 3

            exact_client_hello = recording_client_a.client_hello_packets[0]
            replay_before = server.call_in_loop(server.state.stats)
            client_d.sendto(exact_client_hello, server.remote_addr)
            endpoints.proxy.send_ping(
                client_a,
                server.remote_addr,
                material_a.client_to_server_key,
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


def test_ipv6_remote_comparison_uses_ip_and_port_from_four_tuple(
    real_udpsec_endpoints,
):
    proxy = real_udpsec_endpoints.proxy
    remote = ("::1", 19999, 7, 11)

    assert proxy.remote_addresses_match(("::1", 19999, 0, 0), remote)
    assert not proxy.remote_addresses_match(("::1", 20000, 7, 11), remote)
    assert not proxy.remote_addresses_match(("::2", 19999, 7, 11), remote)


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
        station_id=STATION_ID,
        timestamp=now,
        client_random=b"\x33" * 32,
        client_ephemeral_public_key=off_curve_point,
    )
    off_curve_packet = build_client_hello_packet(
        ClientHello(
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
            + valid_fields[5],
        ),
        ("missing-field", b"|".join(valid_fields[:-1])),
        ("extra-field", valid_packet + b"|extra"),
        ("trailing-delimiter", valid_packet + b"|"),
        (
            "invalid-station-utf8",
            _replace_wire_field(valid_packet, 1, b"\xff"),
        ),
        (
            "embedded-station-nul",
            _replace_wire_field(valid_packet, 1, b"boat\x00_001"),
        ),
        (
            "leading-zero-timestamp",
            _replace_wire_field(
                valid_packet,
                2,
                b"0" + str(now).encode(),
            ),
        ),
        (
            "plus-timestamp",
            _replace_wire_field(
                valid_packet,
                2,
                b"+" + str(now).encode(),
            ),
        ),
        ("stale-signed-timestamp", stale_packet),
        ("empty-random", _replace_wire_field(valid_packet, 3, b"")),
        ("empty-point", _replace_wire_field(valid_packet, 4, b"")),
        ("empty-signature", _replace_wire_field(valid_packet, 5, b"")),
        (
            "bad-base64-alphabet",
            _replace_wire_field(valid_packet, 3, b"%%%%"),
        ),
        (
            "bad-base64-padding",
            _replace_wire_field(
                valid_packet,
                3,
                valid_fields[3] + b"=",
            ),
        ),
        (
            "oversized-bounded-base64",
            _replace_wire_field(
                valid_packet,
                3,
                base64.b64encode(b"\x41" * 4096),
            ),
        ),
        (
            "short-random",
            _replace_wire_field(
                valid_packet,
                3,
                base64.b64encode(b"\x41" * 31),
            ),
        ),
        (
            "long-random",
            _replace_wire_field(
                valid_packet,
                3,
                base64.b64encode(b"\x41" * 33),
            ),
        ),
        (
            "short-point",
            _replace_wire_field(
                valid_packet,
                4,
                base64.b64encode(b"\x02" + b"\x41" * 31),
            ),
        ),
        (
            "long-point",
            _replace_wire_field(
                valid_packet,
                4,
                base64.b64encode(b"\x02" + b"\x41" * 33),
            ),
        ),
        (
            "uncompressed-point-prefix",
            _replace_wire_field(
                valid_packet,
                4,
                base64.b64encode(b"\x04" + b"\x41" * 32),
            ),
        ),
        ("signed-off-curve-point", off_curve_packet),
        (
            "malformed-der",
            _replace_wire_field(
                valid_packet,
                5,
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
            key_material = _perform_real_handshake(
                endpoints,
                client,
                server.remote_addr,
            )
            assert isinstance(key_material, SessionKeyMaterial)
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
            key_material = _perform_real_handshake(
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
                key_material.client_to_server_key,
                _nonce(800),
                valid_shape,
            )
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
                        key_material.server_to_client_key,
                        _nonce(802),
                        valid_shape,
                    ),
                ),
                (
                    "unrelated-key",
                    _encrypted_json_packet(
                        endpoints.proxy,
                        b"\xf0" * 32,
                        _nonce(803),
                        valid_shape,
                    ),
                ),
                (
                    "wrong-aad",
                    _encrypted_json_packet(
                        endpoints.proxy,
                        key_material.client_to_server_key,
                        _nonce(804),
                        valid_shape,
                        aad=b"wrong-aad",
                    ),
                ),
                (
                    "invalid-utf8",
                    _encrypted_plaintext_packet(
                        endpoints.proxy,
                        key_material.client_to_server_key,
                        _nonce(805),
                        b"\xff",
                    ),
                ),
                (
                    "invalid-json",
                    _encrypted_plaintext_packet(
                        endpoints.proxy,
                        key_material.client_to_server_key,
                        _nonce(806),
                        b"{",
                    ),
                ),
                *[
                    (
                        f"non-dict-{index}",
                        _encrypted_plaintext_packet(
                            endpoints.proxy,
                            key_material.client_to_server_key,
                            _nonce(807 + index),
                            json.dumps(value).encode(),
                        ),
                    )
                    for index, value in enumerate(
                        (None, "text", 7, ["list"])
                    )
                ],
                (
                    "missing-type",
                    _encrypted_json_packet(
                        endpoints.proxy,
                        key_material.client_to_server_key,
                        _nonce(811),
                        {
                            "payload": "missing-type",
                            "source_id": STATION_ID,
                        },
                    ),
                ),
                (
                    "unknown-type-reusable-nonce",
                    _encrypted_json_packet(
                        endpoints.proxy,
                        key_material.client_to_server_key,
                        invalid_semantic_nonce,
                        {
                            "type": "unknown",
                            "source_id": STATION_ID,
                        },
                    ),
                ),
                (
                    "wrong-source",
                    _encrypted_json_packet(
                        endpoints.proxy,
                        key_material.client_to_server_key,
                        _nonce(812),
                        {
                            "type": "nmea",
                            "payload": "wrong-source",
                            "source_id": "other-station",
                        },
                    ),
                ),
                (
                    "active-confirmation-sequence",
                    _encrypted_json_packet(
                        endpoints.proxy,
                        key_material.client_to_server_key,
                        _nonce(813),
                        {
                            "type": "ping",
                            "seq": 0,
                            "source_id": STATION_ID,
                        },
                    ),
                ),
                    *[
                        (
                            f"invalid-sequence-{index}",
                            _encrypted_json_packet(
                                endpoints.proxy,
                                key_material.client_to_server_key,
                                _nonce(820 + index),
                                {
                                    "type": "ping",
                                    "seq": sequence,
                                    "source_id": STATION_ID,
                                },
                            ),
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
                            key_material.client_to_server_key,
                            _nonce(826),
                            {
                                "type": "ping",
                                "source_id": STATION_ID,
                            },
                        ),
                    ),
                    (
                        "missing-nmea-payload",
                        _encrypted_json_packet(
                            endpoints.proxy,
                            key_material.client_to_server_key,
                            _nonce(827),
                            {
                                "type": "nmea",
                                "source_id": STATION_ID,
                        },
                    ),
                ),
                ("truncated-authenticated-packet", valid_base[:-1]),
                (
                    "authenticated-listener-limit-invalid-json",
                        _encrypted_plaintext_packet(
                            endpoints.proxy,
                            key_material.client_to_server_key,
                            _nonce(828),
                            b"x" * 8158,
                        ),
                    ),
                ]
            assert len(corpus) == 28
            assert len(corpus[-1][1]) == 8192

            for _, packet in corpus:
                client.sendto(packet, server.remote_addr)

            accepted_payload = "!AIVDM,1,1,,A,after-adversarial,0*00"
            accepted_packet = _encrypted_json_packet(
                endpoints.proxy,
                key_material.client_to_server_key,
                invalid_semantic_nonce,
                {
                    "type": "nmea",
                    "payload": accepted_payload,
                    "timestamp": 1000,
                    "source_id": STATION_ID,
                },
            )
            client.sendto(accepted_packet, server.remote_addr)
            client.sendto(accepted_packet, server.remote_addr)
            endpoints.proxy.send_ping(
                client,
                server.remote_addr,
                key_material.client_to_server_key,
                STATION_ID,
                41,
            )

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
                        key_material.server_to_client_key,
                    )
                )
                assert response_message.get("type") == "pong"
                received_sequences.append(response_message.get("seq"))
            client.settimeout(0.05)
            with pytest.raises(socket.timeout):
                client.recvfrom(8192)

            after = server.call_in_loop(server.state.stats)
            assert server.state._sessions[client.getsockname()] is session
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
                in session.seen_data_nonces._live_by_key
            )
            assert received_sequences == [41]


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
            key_material = _perform_real_handshake(
                endpoints,
                client,
                server.remote_addr,
            )
            before = server.call_in_loop(server.state.stats)
            negative_sequences = (-1, -(2**31))
            positive_sequences = (1, 2**31)

            for index, sequence in enumerate(
                (*negative_sequences, *positive_sequences)
            ):
                client.sendto(
                    _encrypted_json_packet(
                        endpoints.proxy,
                        key_material.client_to_server_key,
                        _nonce(860 + index),
                        {
                            "type": "ping",
                            "seq": sequence,
                            "source_id": STATION_ID,
                        },
                    ),
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
                    key_material.server_to_client_key,
                )
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
    remote_addr = ("127.0.0.1", 19999)
    packet = _encrypted_json_packet(
        proxy,
        key,
        _nonce(900),
        {
            "type": "pong",
            "seq": invalid_sequence,
            "source_id": STATION_ID,
        },
    )

    assert proxy.handle_server_packet(
        packet,
        remote_addr,
        remote_addr,
        key,
        STATION_ID,
        1,
    ) == proxy.SERVER_PACKET_IGNORED


def test_expired_pending_handle_cannot_replace_live_active_session(
    real_udpsec_endpoints,
):
    secure = real_udpsec_endpoints.secure
    state = secure.SecureState(pending_session_ttl=5)
    address = ("127.0.0.1", 51001)
    active = state.install_session(
        address,
        STATION_ID,
        AESGCM(b"\x61" * 32),
        AESGCM(b"\x62" * 32),
        0,
    )
    pending = state.install_pending_session(
        address,
        STATION_ID,
        AESGCM(b"\x63" * 32),
        AESGCM(b"\x64" * 32),
        1,
    )

    assert state.promote_pending_session(address, pending, 6) is None
    assert state.get_active_session(address, 6) is active
    assert state.get_pending_session(address, 6) is None
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
    active = state.install_session(
        address,
        STATION_ID,
        AESGCM(b"\x65" * 32),
        AESGCM(b"\x66" * 32),
        10,
    )
    pending = state.install_pending_session(
        address,
        STATION_ID,
        AESGCM(b"\x67" * 32),
        AESGCM(b"\x68" * 32),
        11,
    )

    assert set(vars(active)) == {
        "_address",
        "station_id",
        "client_to_server_aesgcm",
        "server_to_client_aesgcm",
        "created_at",
        "last_seen",
        "seen_data_nonces",
    }
    assert set(vars(pending)) == {
        "_address",
        "station_id",
        "client_to_server_aesgcm",
        "server_to_client_aesgcm",
        "created_at",
        "seen_data_nonces",
    }
    forbidden_field_fragments = {
        "ephemeral",
        "shared_secret",
        "transcript",
        "identity_private",
        "client_to_server_key",
        "server_to_client_key",
    }
    for retained in (active, pending):
        assert not (
            forbidden_field_fragments
            & set(vars(retained))
        )
        assert not any(
            isinstance(value, bytes)
            for value in vars(retained).values()
        )

    key_material = SessionKeyMaterial(b"\x69" * 32, b"\x6a" * 32)
    material_repr = repr(key_material)
    assert key_material.client_to_server_key.hex() not in material_repr
    assert key_material.server_to_client_key.hex() not in material_repr

    client_hello = ClientHello(
        station_id=STATION_ID,
        timestamp=1,
        client_random=b"\x01" * 32,
        client_ephemeral_public_key=b"\x02" + b"\x02" * 32,
        client_signature=b"client-secret-signature",
    )
    server_hello = ServerHello(
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
        "build_current_handshake_payload",
        "build_handshake_context_v1",
        "build_session_transcript_v1",
        "compute_session_hash",
        "derive_session_key",
        "handle_keepalive",
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
        assert "session_key" not in identifiers
        assert not (obsolete_identifiers & identifiers)
        assert not [
            value
            for value in ast.walk(tree)
            if isinstance(value, ast.Constant)
            and isinstance(value.value, str)
            and "KEEPALIVE" in value.value
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
            "pending.client_to_server_aesgcm.decrypt",
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
