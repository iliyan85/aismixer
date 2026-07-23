import asyncio
import base64
import builtins
import importlib.util
import io
import os
import socket
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from core.network_policy import NetworkPolicy


ROOT = Path(__file__).resolve().parents[1]
NMEA_SPROXY_DIR = ROOT / "nmea_sproxy"

SERVER_CANONICAL_PRIVATE_KEY_PATH = "/etc/aismixer/keys/aismixer_private.pem"
SERVER_LEGACY_ETC_PRIVATE_KEY_PATH = "/etc/aismixer/aismixer_private.key"
SERVER_LOCAL_CANONICAL_PRIVATE_KEY_FILENAME = "aismixer_private.pem"
SERVER_PRIVATE_KEY_FILENAME = "aismixer_private.key"
SERVER_PUBLIC_KEY_FOR_PROXY_FILENAME = "aismixer_public.pem"
STATION_CANONICAL_PRIVATE_KEY_PATH = "/etc/nmea_sproxy/keys/station_private.pem"
STATION_PRIVATE_KEY_FILENAME = "station_private.key"
STATION_PUBLIC_KEY_FILENAME = "station_public.pem"
REMOTE_CANONICAL_PUBLIC_KEY_PATH = "/etc/nmea_sproxy/keys/aismixer_public.pem"


def load_proxy_module():
    sys.path.insert(0, str(NMEA_SPROXY_DIR))
    try:
        spec = importlib.util.spec_from_file_location(
            "nmea_sproxy_helpers", NMEA_SPROXY_DIR / "nmea_sproxy.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(NMEA_SPROXY_DIR))


def _normalize_path(path):
    return os.path.normcase(os.path.normpath(os.fspath(path)))


def load_secure_module_with_fake_keys(
    monkeypatch,
    with_client_private_key=False,
    existing_paths=None,
):
    server_private_key = ec.generate_private_key(ec.SECP256R1())
    server_private_bytes = server_private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )

    client_private_key = ec.generate_private_key(ec.SECP256R1())
    client_public_bytes = client_private_key.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.CompressedPoint,
    )
    authorized_yaml = (
        "authorized_clients:\n"
        "  - name: boat_001\n"
        f"    pubkey: {base64.b64encode(client_public_bytes).decode()}\n"
    )

    real_open = open

    def fake_open(path, mode="r", *args, **kwargs):
        name = os.path.basename(os.fspath(path))
        if name == "authorized_keys.yaml":
            return io.StringIO(authorized_yaml)
        if name in (
            SERVER_LOCAL_CANONICAL_PRIVATE_KEY_FILENAME,
            SERVER_PRIVATE_KEY_FILENAME,
        ):
            return io.BytesIO(server_private_bytes)
        return real_open(path, mode, *args, **kwargs)

    existing = {
        _normalize_path(path)
        for path in (existing_paths or ())
    }

    with monkeypatch.context() as patch:
        patch.setattr(os.path, "exists", lambda path: _normalize_path(path) in existing)
        patch.setattr("builtins.open", fake_open)
        spec = importlib.util.spec_from_file_location(
            "aismixer_secure_test_helpers", ROOT / "aismixer_secure.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if with_client_private_key:
            return module, client_private_key
        return module


def test_proxy_and_server_derive_session_key_are_compatible(monkeypatch):
    proxy = load_proxy_module()
    secure = load_secure_module_with_fake_keys(monkeypatch)
    shared_secret = b"fixed shared secret"
    client_signature = b"client signature bytes"
    server_signature = b"server signature bytes"

    assert proxy.derive_session_key(
        shared_secret, client_signature, server_signature
    ) == secure.derive_session_key(shared_secret, client_signature + server_signature)


def test_current_handshake_payload_matches_existing_signed_bytes(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    timestamp = 1234567890

    assert secure.build_current_handshake_payload("boat_001", timestamp) == (
        secure.HANDSHAKE_PREFIX + b"boat_001" + timestamp.to_bytes(8, "big")
    )


def test_context_string_is_not_part_of_current_handshake_payload(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)

    payload = secure.build_current_handshake_payload("boat_001", 1234567890)

    assert secure.CONTEXT_STRING == b"NMEA-AUTH-v1"
    assert secure.CONTEXT_STRING not in payload


def test_v1_handshake_context_is_deterministic_for_identical_inputs(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    args = ("boat_001", 1234567890, b"client pub", b"server pub")

    assert secure.build_handshake_context_v1(*args) == secure.build_handshake_context_v1(*args)


def test_v1_handshake_context_changes_when_context_string_changes(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)

    assert secure.build_handshake_context_v1(
        "boat_001", 1234567890, b"client pub", b"server pub"
    ) != secure.build_handshake_context_v1(
        "boat_001",
        1234567890,
        b"client pub",
        b"server pub",
        context_string=b"OTHER-CONTEXT",
    )


def test_v1_handshake_context_changes_when_station_id_changes(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)

    assert secure.build_handshake_context_v1(
        "boat_001", 1234567890, b"client pub", b"server pub"
    ) != secure.build_handshake_context_v1(
        "boat_002", 1234567890, b"client pub", b"server pub"
    )


def test_v1_handshake_context_changes_when_timestamp_changes(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)

    assert secure.build_handshake_context_v1(
        "boat_001", 1234567890, b"client pub", b"server pub"
    ) != secure.build_handshake_context_v1(
        "boat_001", 1234567891, b"client pub", b"server pub"
    )


def test_v1_handshake_context_changes_when_client_public_key_changes(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)

    assert secure.build_handshake_context_v1(
        "boat_001", 1234567890, b"client pub", b"server pub"
    ) != secure.build_handshake_context_v1(
        "boat_001", 1234567890, b"other client pub", b"server pub"
    )


def test_v1_handshake_context_changes_when_server_public_key_changes(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)

    assert secure.build_handshake_context_v1(
        "boat_001", 1234567890, b"client pub", b"server pub"
    ) != secure.build_handshake_context_v1(
        "boat_001", 1234567890, b"client pub", b"other server pub"
    )


def test_v1_session_transcript_is_deterministic_for_identical_inputs(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    context = secure.build_handshake_context_v1(
        "boat_001", 1234567890, b"client pub", b"server pub"
    )

    assert secure.build_session_transcript_v1(
        context, b"client sig", b"server sig"
    ) == secure.build_session_transcript_v1(context, b"client sig", b"server sig")


def test_v1_session_transcript_changes_when_client_signature_changes(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    context = secure.build_handshake_context_v1(
        "boat_001", 1234567890, b"client pub", b"server pub"
    )

    assert secure.build_session_transcript_v1(
        context, b"client sig", b"server sig"
    ) != secure.build_session_transcript_v1(context, b"other client sig", b"server sig")


def test_v1_session_transcript_changes_when_server_signature_changes(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    context = secure.build_handshake_context_v1(
        "boat_001", 1234567890, b"client pub", b"server pub"
    )

    assert secure.build_session_transcript_v1(
        context, b"client sig", b"server sig"
    ) != secure.build_session_transcript_v1(context, b"client sig", b"other server sig")


def test_handshake_replay_key_is_stable_for_same_inputs(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)

    assert secure.build_handshake_replay_key(
        "boat_001", 1234567890, b"client sig"
    ) == secure.build_handshake_replay_key("boat_001", 1234567890, b"client sig")


def test_handshake_replay_key_changes_when_station_changes(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)

    assert secure.build_handshake_replay_key(
        "boat_001", 1234567890, b"client sig"
    ) != secure.build_handshake_replay_key("boat_002", 1234567890, b"client sig")


def test_handshake_replay_key_changes_when_timestamp_changes(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)

    assert secure.build_handshake_replay_key(
        "boat_001", 1234567890, b"client sig"
    ) != secure.build_handshake_replay_key("boat_001", 1234567891, b"client sig")


def test_handshake_replay_key_changes_when_signature_changes(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)

    assert secure.build_handshake_replay_key(
        "boat_001", 1234567890, b"client sig"
    ) != secure.build_handshake_replay_key("boat_001", 1234567890, b"other client sig")


def test_handshake_replay_key_does_not_depend_on_addr(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    addr_a = ("192.0.2.10", 50000)
    addr_b = ("192.0.2.11", 50001)

    assert addr_a != addr_b
    assert secure.build_handshake_replay_key(
        "boat_001", 1234567890, b"client sig"
    ) == secure.build_handshake_replay_key("boat_001", 1234567890, b"client sig")


def test_handshake_replay_constants(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)

    assert secure.HANDSHAKE_REPLAY_TTL_SECONDS == 60
    assert secure.HANDSHAKE_REPLAY_MAX == 100000
    assert secure.SESSION_MAX == 100000


class _FakeSecureSocket:
    def __init__(self):
        self.bound = None
        self.blocking = None
        self.sent = []

    def bind(self, addr):
        self.bound = addr

    def setblocking(self, blocking):
        self.blocking = blocking

    def sendto(self, data, addr):
        self.sent.append((data, addr))


class _FakeSecureLoop:
    def __init__(self, packets):
        self.packets = list(packets)

    async def sock_recvfrom(self, sock, size):
        if self.packets:
            return self.packets.pop(0)
        raise asyncio.CancelledError()


class _FakeSocketModule:
    def __init__(self, fake_socket, real_socket_module):
        self._fake_socket = fake_socket
        self.AF_INET6 = real_socket_module.AF_INET6
        self.AF_INET = real_socket_module.AF_INET
        self.SOCK_DGRAM = real_socket_module.SOCK_DGRAM

    def socket(self, *args, **kwargs):
        return self._fake_socket


class _FakeAsyncioModule:
    def __init__(self, fake_loop):
        self._fake_loop = fake_loop

    def get_running_loop(self):
        return self._fake_loop


class _FakeQueue:
    def __init__(self):
        self.items = []

    async def put(self, item):
        self.items.append(item)


class _FakeClock:
    def __init__(self, now):
        self.now = now
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.now


def _signed_handshake_packet(secure, client_private_key, station_id, timestamp):
    digest = secure.hashes.Hash(
        secure.hashes.SHA256(), backend=secure.default_backend())
    digest.update(secure.build_current_handshake_payload(station_id, timestamp))
    to_sign = digest.finalize()
    signature = client_private_key.sign(
        to_sign,
        secure.ec.ECDSA(secure.utils.Prehashed(secure.hashes.SHA256())),
    )
    return b"|".join([
        secure.HANDSHAKE_PREFIX,
        station_id.encode(),
        str(timestamp).encode(),
        base64.b64encode(signature),
    ])


def _encrypted_data_packet(
    secure,
    key,
    nonce,
    source_id="boat_001",
    payload="!AIVDM,1,1,,A,payload,0*00",
):
    plaintext = secure.json.dumps({
        "type": "nmea",
        "payload": payload,
        "timestamp": 1000,
        "source_id": source_id,
    }).encode()
    ciphertext = secure.AESGCM(key).encrypt(nonce, plaintext, b"NMEA")
    return secure.DATA_PREFIX + nonce + ciphertext


def _encrypted_control_packet(secure, key, nonce, message):
    plaintext = secure.json.dumps(message).encode()
    ciphertext = secure.AESGCM(key).encrypt(nonce, plaintext, secure.DATA_AAD)
    return secure.DATA_PREFIX + nonce + ciphertext


def _run_secure_server_with_packets(
    monkeypatch,
    secure,
    packets,
    state=None,
    wall_clock=None,
    monotonic_clock=None,
    sec_input_id=None,
    ingress_policy=None,
):
    fake_socket = _FakeSecureSocket()
    fake_loop = _FakeSecureLoop(packets)
    fake_queue = _FakeQueue()

    state = secure.SecureState() if state is None else state
    wall_clock = _FakeClock(1010.0) if wall_clock is None else wall_clock
    monotonic_clock = (
        _FakeClock(1010.0)
        if monotonic_clock is None
        else monotonic_clock
    )
    monkeypatch.setattr(
        secure, "socket", _FakeSocketModule(fake_socket, secure.socket))
    monkeypatch.setattr(secure, "asyncio", _FakeAsyncioModule(fake_loop))

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            secure.secure_server(
                fake_queue,
                "127.0.0.1",
                9999,
                sec_input_id=sec_input_id,
                ingress_policy=ingress_policy,
                state=state,
                wall_clock=wall_clock,
                monotonic_clock=monotonic_clock,
            )
        )

    return fake_queue, fake_socket


def _install_test_session(
    secure,
    state,
    addr,
    key,
    now=1000.0,
    station_id="boat_001",
):
    aesgcm = secure.AESGCM(key)
    session = state.install_session(addr, station_id, aesgcm, now)
    return session, aesgcm


def test_secure_server_rejects_verified_duplicate_handshake_replay(monkeypatch):
    secure, client_private_key = load_secure_module_with_fake_keys(
        monkeypatch, with_client_private_key=True)
    timestamp = 1000
    station_id = "boat_001"
    addr = ("127.0.0.1", 50123)
    packet = _signed_handshake_packet(
        secure, client_private_key, station_id, timestamp)
    state = secure.SecureState()
    wall_clock = _FakeClock(float(timestamp))
    monotonic_clock = _FakeClock(10.0)

    _, fake_socket = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(packet, addr), (packet, addr)],
        state=state,
        wall_clock=wall_clock,
        monotonic_clock=monotonic_clock,
    )

    stats = state.stats()
    assert len(fake_socket.sent) == 1
    response_parts = fake_socket.sent[0][0].split(b"|")
    assert response_parts[0] == b"OK"
    assert len(response_parts) == 2
    server_signature = base64.b64decode(response_parts[1], validate=True)
    digest = secure.hashes.Hash(
        secure.hashes.SHA256(), backend=secure.default_backend()
    )
    digest.update(
        secure.build_current_handshake_payload(station_id, timestamp)
    )
    secure.verify_signature(
        secure.server_pub_bytes,
        server_signature,
        digest.finalize(),
    )
    assert fake_socket.sent[0][1] == addr
    assert stats.handshake_replay_accepted == 1
    assert stats.handshake_replay_rejected == 1
    assert stats.sessions_created == 1
    assert stats.current_handshake_replays == 1
    assert stats.current_sessions == 1
    assert wall_clock.calls == 2
    assert monotonic_clock.calls == 2


def test_secure_server_sends_no_session_for_data_without_session(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    addr = ("127.0.0.1", 50123)
    packet = secure.DATA_PREFIX + (b"\x00" * 28)

    fake_queue, fake_socket = _run_secure_server_with_packets(
        monkeypatch, secure, [(packet, addr)]
    )

    assert fake_queue.items == []
    assert fake_socket.sent == [(secure.NOSESSION_PREFIX, addr)]


def test_secure_server_sends_station_no_session_for_keepalive_without_session(
    monkeypatch,
):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    addr = ("127.0.0.1", 50123)
    packet = b"KEEPALIVE|boat_001|1000"

    fake_queue, fake_socket = _run_secure_server_with_packets(
        monkeypatch, secure, [(packet, addr)], state=state
    )

    assert fake_queue.items == []
    assert fake_socket.sent == [(b"NOSESSION|boat_001", addr)]
    assert state.stats().current_sessions == 0


def test_secure_server_sends_bare_no_session_for_unparseable_keepalive(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    addr = ("127.0.0.1", 50123)

    fake_queue, fake_socket = _run_secure_server_with_packets(
        monkeypatch, secure, [(b"KEEPALIVE", addr)]
    )

    assert fake_queue.items == []
    assert fake_socket.sent == [(secure.NOSESSION_PREFIX, addr)]


def test_secure_server_valid_keepalive_touches_once_with_separate_clocks(
    monkeypatch,
):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    addr = ("127.0.0.1", 50123)
    session = state.install_session(
        addr, "boat_001", object(), now=1000.0
    )
    wall_clock = _FakeClock(5000.0)
    monotonic_clock = _FakeClock(1010.0)

    fake_queue, fake_socket = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(b"KEEPALIVE|boat_001|1000", addr)],
        state=state,
        wall_clock=wall_clock,
        monotonic_clock=monotonic_clock,
    )

    assert fake_queue.items == []
    assert fake_socket.sent == []
    assert session.last_seen == 1010.0
    assert state.stats().sessions_touched == 1
    assert wall_clock.calls == int(secure.DEBUG)
    assert monotonic_clock.calls == 1


def test_secure_server_replies_with_encrypted_pong_for_valid_ping(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    key = b"\x01" * 32
    nonce = b"\x02" * 12
    addr = ("127.0.0.1", 50123)
    state = secure.SecureState()
    session, _ = _install_test_session(secure, state, addr, key)
    packet = _encrypted_control_packet(
        secure,
        key,
        nonce,
        {
            "type": "ping",
            "seq": 123,
            "timestamp": 1000,
            "source_id": "boat_001",
        },
    )
    wall_clock = _FakeClock(2020.0)
    monotonic_clock = _FakeClock(1010.0)

    fake_queue, fake_socket = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(packet, addr)],
        state=state,
        wall_clock=wall_clock,
        monotonic_clock=monotonic_clock,
    )

    stats = state.stats()
    assert fake_queue.items == []
    assert len(fake_socket.sent) == 1
    response, response_addr = fake_socket.sent[0]
    response_nonce, ciphertext = secure.parse_secure_data_packet(response)
    pong = secure.json.loads(
        secure.AESGCM(key).decrypt(
            response_nonce, ciphertext, secure.DATA_AAD
        ).decode()
    )
    assert response_addr == addr
    assert pong == {
        "type": "pong",
        "seq": 123,
        "timestamp": 2020,
        "source_id": "boat_001",
    }
    assert session.last_seen == 1010.0
    assert stats.sessions_touched == 1
    assert stats.data_nonces_accepted == 1
    assert wall_clock.calls == 1
    assert monotonic_clock.calls == 1


def test_secure_server_enqueues_first_time_valid_data_packet(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    key = b"\x01" * 32
    nonce = b"\x02" * 12
    addr = ("127.0.0.1", 50123)
    state = secure.SecureState()
    session, _ = _install_test_session(secure, state, addr, key)
    packet = _encrypted_data_packet(secure, key, nonce)

    fake_queue, _ = _run_secure_server_with_packets(
        monkeypatch, secure, [(packet, addr)], state=state)

    stats = state.stats()
    assert len(fake_queue.items) == 1
    assert fake_queue.items[0].source_id == "udpsec:boat_001"
    assert fake_queue.items[0].raw_line == "!AIVDM,1,1,,A,payload,0*00"
    assert session.last_seen == 1010.0
    assert len(session.seen_data_nonces) == 1
    assert stats.sessions_touched == 1
    assert stats.data_nonces_accepted == 1


def test_secure_server_allowed_peer_preserves_data_behavior(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    key = b"\x01" * 32
    nonce = b"\x02" * 12
    addr = ("127.0.0.1", 50123)
    policy = NetworkPolicy.from_entries(
        ["127.0.0.1"],
        context="sec_inputs[0].allow_from",
    )
    state = secure.SecureState()
    _install_test_session(secure, state, addr, key)
    packet = _encrypted_data_packet(secure, key, nonce)

    fake_queue, _ = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(packet, addr)],
        state=state,
        ingress_policy=policy,
    )

    assert len(fake_queue.items) == 1
    assert fake_queue.items[0].source_id == "udpsec:boat_001"
    assert fake_queue.items[0].raw_line == "!AIVDM,1,1,,A,payload,0*00"


def test_secure_server_denied_data_peer_gets_no_no_session_response(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(session_ttl=1.0)
    retained_addr = ("198.51.100.20", 50000)
    state.install_session(retained_addr, "retained", object(), now=0.0)
    addr = ("192.0.2.10", 50123)
    packet = secure.DATA_PREFIX + (b"\x00" * 28)
    policy = NetworkPolicy.from_entries(
        ["198.51.100.0/24"],
        context="sec_inputs[0].allow_from",
    )
    wall_clock = _FakeClock(1000.0)
    monotonic_clock = _FakeClock(1.0)

    fake_queue, fake_socket = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(packet, addr)],
        state=state,
        wall_clock=wall_clock,
        monotonic_clock=monotonic_clock,
        ingress_policy=policy,
    )

    assert fake_queue.items == []
    assert fake_socket.sent == []
    assert tuple(state._sessions) == (retained_addr,)
    assert state.stats().current_sessions == 1
    assert state.stats().sessions_expired == 0
    assert wall_clock.calls == 0
    assert monotonic_clock.calls == 0


def test_secure_server_denied_handshake_peer_is_dropped_before_crypto(monkeypatch):
    secure, client_private_key = load_secure_module_with_fake_keys(
        monkeypatch, with_client_private_key=True)
    timestamp = 1000
    station_id = "boat_001"
    addr = ("192.0.2.10", 50123)
    packet = _signed_handshake_packet(
        secure, client_private_key, station_id, timestamp)
    policy = NetworkPolicy.from_entries(
        ["198.51.100.0/24"],
        context="sec_inputs[0].allow_from",
    )

    def fail_verify(*_args, **_kwargs):
        raise AssertionError("signature verification should not run")

    monkeypatch.setattr(secure, "verify_signature", fail_verify)
    state = secure.SecureState()
    wall_clock = _FakeClock(float(timestamp))
    monotonic_clock = _FakeClock(10.0)
    fake_queue, fake_socket = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(packet, addr)],
        state=state,
        wall_clock=wall_clock,
        monotonic_clock=monotonic_clock,
        ingress_policy=policy,
    )

    assert fake_queue.items == []
    assert fake_socket.sent == []
    assert state.stats().current_sessions == 0
    assert state.stats().current_handshake_replays == 0
    assert wall_clock.calls == 0
    assert monotonic_clock.calls == 0


def test_secure_server_source_id_uses_station_not_sec_input_id(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    key = b"\x01" * 32
    nonce = b"\x02" * 12
    addr = ("127.0.0.1", 50123)
    state = secure.SecureState()
    _install_test_session(secure, state, addr, key)
    packet = _encrypted_data_packet(secure, key, nonce)

    fake_queue, _ = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(packet, addr)],
        state=state,
        sec_input_id="configured_listener_alias",
    )

    assert len(fake_queue.items) == 1
    event = fake_queue.items[0]
    assert event.source_id == "udpsec:boat_001"
    assert event.alias_for_s == "configured_listener_alias"


def test_secure_server_rejects_duplicate_data_nonce_after_first_valid_packet(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    key = b"\x01" * 32
    nonce = b"\x02" * 12
    addr = ("127.0.0.1", 50123)
    state = secure.SecureState()
    session, _ = _install_test_session(secure, state, addr, key)
    packet = _encrypted_data_packet(secure, key, nonce)

    fake_queue, _ = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(packet, addr), (packet, addr)],
        state=state,
    )

    stats = state.stats()
    assert len(fake_queue.items) == 1
    assert len(session.seen_data_nonces) == 1
    assert stats.data_nonces_accepted == 1
    assert stats.data_nonce_replays == 1
    assert stats.sessions_touched == 1


def test_secure_server_failed_decrypt_does_not_record_data_nonce(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    key = b"\x01" * 32
    nonce = b"\x02" * 12
    addr = ("127.0.0.1", 50123)
    state = secure.SecureState()
    session, _ = _install_test_session(secure, state, addr, key)
    packet = secure.DATA_PREFIX + nonce + (b"\x00" * 16)

    fake_queue, _ = _run_secure_server_with_packets(
        monkeypatch, secure, [(packet, addr)], state=state)

    stats = state.stats()
    assert fake_queue.items == []
    assert session.last_seen == 1000.0
    assert len(session.seen_data_nonces) == 0
    assert stats.sessions_touched == 0
    assert stats.data_nonces_accepted == 0


def test_secure_server_malformed_framing_does_not_record_nonce_or_touch(
    monkeypatch,
):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    key = b"\x01" * 32
    addr = ("127.0.0.1", 50123)
    state = secure.SecureState()
    session, _ = _install_test_session(secure, state, addr, key)
    packet = secure.DATA_PREFIX + (b"\x02" * 12)

    fake_queue, fake_socket = _run_secure_server_with_packets(
        monkeypatch, secure, [(packet, addr)], state=state
    )

    assert fake_queue.items == []
    assert fake_socket.sent == []
    assert session.last_seen == 1000.0
    assert len(session.seen_data_nonces) == 0
    assert state.stats().sessions_touched == 0
    assert state.stats().data_nonces_accepted == 0


def test_secure_server_source_mismatch_does_not_record_data_nonce_or_touch(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    key = b"\x01" * 32
    nonce = b"\x02" * 12
    addr = ("127.0.0.1", 50123)
    state = secure.SecureState()
    session, _ = _install_test_session(secure, state, addr, key)
    packet = _encrypted_data_packet(secure, key, nonce, source_id="other_station")

    fake_queue, _ = _run_secure_server_with_packets(
        monkeypatch, secure, [(packet, addr)], state=state)

    stats = state.stats()
    assert fake_queue.items == []
    assert session.last_seen == 1000.0
    assert len(session.seen_data_nonces) == 0
    assert stats.sessions_touched == 0
    assert stats.data_nonces_accepted == 0


@pytest.mark.parametrize(
    ("wall_now", "accepted"),
    [
        (970.0, True),
        (1030.0, True),
        (969.999, False),
        (1030.001, False),
    ],
)
def test_secure_server_handshake_freshness_uses_wall_clock_boundary(
    monkeypatch,
    wall_now,
    accepted,
):
    secure, client_private_key = load_secure_module_with_fake_keys(
        monkeypatch, with_client_private_key=True
    )
    timestamp = 1000
    addr = ("127.0.0.1", 50123)
    packet = _signed_handshake_packet(
        secure, client_private_key, "boat_001", timestamp
    )
    state = secure.SecureState()
    wall_clock = _FakeClock(wall_now)
    monotonic_clock = _FakeClock(1_000_000.0)

    _, fake_socket = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(packet, addr)],
        state=state,
        wall_clock=wall_clock,
        monotonic_clock=monotonic_clock,
    )

    assert bool(fake_socket.sent) is accepted
    assert state.stats().sessions_created == int(accepted)
    assert state.stats().handshake_replay_accepted == int(accepted)
    assert wall_clock.calls == 1
    assert monotonic_clock.calls == 1


def test_secure_server_handshake_replay_ttl_uses_monotonic_clock(monkeypatch):
    secure, client_private_key = load_secure_module_with_fake_keys(
        monkeypatch, with_client_private_key=True
    )
    timestamp = 1000
    addr = ("127.0.0.1", 50123)
    packet = _signed_handshake_packet(
        secure, client_private_key, "boat_001", timestamp
    )
    state = secure.SecureState(handshake_replay_ttl=60.0)

    _, first_socket = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(packet, addr)],
        state=state,
        wall_clock=_FakeClock(1000.0),
        monotonic_clock=_FakeClock(10.0),
    )
    _, duplicate_socket = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(packet, addr)],
        state=state,
        wall_clock=_FakeClock(1000.0),
        monotonic_clock=_FakeClock(69.999),
    )
    _, expired_socket = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(packet, addr)],
        state=state,
        wall_clock=_FakeClock(1000.0),
        monotonic_clock=_FakeClock(70.0),
    )

    assert len(first_socket.sent) == 1
    assert duplicate_socket.sent == []
    assert len(expired_socket.sent) == 1
    stats = state.stats()
    assert stats.handshake_replay_accepted == 2
    assert stats.handshake_replay_rejected == 1
    assert stats.handshake_replay_expired == 1
    assert stats.sessions_created == 2
    assert stats.sessions_replaced == 1


def test_secure_server_keeps_replay_record_after_post_acceptance_failure(monkeypatch):
    secure, client_private_key = load_secure_module_with_fake_keys(
        monkeypatch, with_client_private_key=True
    )
    timestamp = 1000
    addr = ("127.0.0.1", 50123)
    packet = _signed_handshake_packet(
        secure, client_private_key, "boat_001", timestamp
    )
    state = secure.SecureState()

    class FailingServerPrivateKey:
        def __init__(self):
            self.sign_calls = 0

        def sign(self, *_args, **_kwargs):
            self.sign_calls += 1
            raise RuntimeError("server signing failed")

    failing_key = FailingServerPrivateKey()
    monkeypatch.setattr(secure, "server_priv", failing_key)

    _, fake_socket = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(packet, addr), (packet, addr)],
        state=state,
        wall_clock=_FakeClock(1000.0),
        monotonic_clock=_FakeClock(10.0),
    )

    assert fake_socket.sent == []
    assert failing_key.sign_calls == 1
    stats = state.stats()
    assert stats.handshake_replay_accepted == 1
    assert stats.handshake_replay_rejected == 1
    assert stats.current_handshake_replays == 1
    assert stats.sessions_created == 0


def test_secure_server_successful_handshake_replaces_live_session_only(
    monkeypatch,
):
    secure, client_private_key = load_secure_module_with_fake_keys(
        monkeypatch, with_client_private_key=True
    )
    state = secure.SecureState(max_sessions=2)
    addr = ("127.0.0.1", 50123)
    other_addr = ("127.0.0.1", 50124)
    old = state.install_session(
        addr, "boat_001", object(), now=0.0
    )
    other = state.install_session(
        other_addr, "other", object(), now=1.0
    )
    nonce = b"\x01" * 12
    assert state.accept_data_nonce(old, nonce, now=1.0)
    packet = _signed_handshake_packet(
        secure, client_private_key, "boat_001", 1000
    )

    _, fake_socket = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(packet, addr)],
        state=state,
        wall_clock=_FakeClock(1000.0),
        monotonic_clock=_FakeClock(2.0),
    )

    replacement = state._sessions[addr]
    response, response_addr = fake_socket.sent[0]
    assert response_addr == addr
    assert response.startswith(b"OK|")
    assert replacement is not old
    assert replacement.aesgcm is not old.aesgcm
    assert state._sessions[other_addr] is other
    assert tuple(state._sessions) == (other_addr, addr)
    assert not state.data_nonce_seen(replacement, nonce, now=2.0)

    stats = state.stats()
    assert stats.sessions_created == 3
    assert stats.sessions_replaced == 1
    assert stats.sessions_capacity_evicted == 0
    assert stats.data_nonces_session_discarded == 1


@pytest.mark.parametrize(
    ("monotonic_now", "accepted"),
    [(1299.999, True), (1300.0, False)],
)
def test_secure_server_session_ttl_uses_exact_monotonic_boundary(
    monkeypatch,
    monotonic_now,
    accepted,
):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    key = b"\x01" * 32
    nonce = b"\x02" * 12
    addr = ("127.0.0.1", 50123)
    state = secure.SecureState()
    _install_test_session(secure, state, addr, key, now=1000.0)
    packet = _encrypted_data_packet(secure, key, nonce)

    fake_queue, fake_socket = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(packet, addr)],
        state=state,
        wall_clock=_FakeClock(9_999_999.0),
        monotonic_clock=_FakeClock(monotonic_now),
    )

    assert bool(fake_queue.items) is accepted
    assert fake_socket.sent == (
        [] if accepted else [(secure.NOSESSION_PREFIX, addr)]
    )
    assert state.stats().sessions_expired == int(not accepted)


def test_secure_server_nonce_ttl_uses_monotonic_clock_and_exact_boundary(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    key = b"\x01" * 32
    nonce = b"\x02" * 12
    addr = ("127.0.0.1", 50123)
    state = secure.SecureState(session_ttl=1000.0, data_nonce_ttl=10.0)
    _install_test_session(secure, state, addr, key, now=0.0)
    packet = _encrypted_data_packet(secure, key, nonce)

    first_queue, _ = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(packet, addr)],
        state=state,
        wall_clock=_FakeClock(1000.0),
        monotonic_clock=_FakeClock(0.0),
    )
    duplicate_queue, _ = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(packet, addr)],
        state=state,
        wall_clock=_FakeClock(50_000.0),
        monotonic_clock=_FakeClock(9.999),
    )
    expired_queue, _ = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(packet, addr)],
        state=state,
        wall_clock=_FakeClock(-50_000.0),
        monotonic_clock=_FakeClock(10.0),
    )

    assert len(first_queue.items) == 1
    assert duplicate_queue.items == []
    assert len(expired_queue.items) == 1
    stats = state.stats()
    assert stats.data_nonces_accepted == 2
    assert stats.data_nonce_replays == 1
    assert stats.data_nonces_expired == 1
    assert stats.sessions_touched == 2


def test_secure_server_rejects_repeated_nonce_before_second_decrypt(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    key = b"\x01" * 32
    nonce = b"\x02" * 12
    addr = ("127.0.0.1", 50123)
    real_aesgcm = secure.AESGCM(key)

    class CountingAESGCM:
        def __init__(self):
            self.decrypt_calls = 0

        def decrypt(self, *args):
            self.decrypt_calls += 1
            return real_aesgcm.decrypt(*args)

    aesgcm = CountingAESGCM()
    state = secure.SecureState()
    state.install_session(addr, "boat_001", aesgcm, now=1000.0)
    packet = _encrypted_data_packet(secure, key, nonce)

    fake_queue, _ = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(packet, addr), (packet, addr)],
        state=state,
    )

    assert len(fake_queue.items) == 1
    assert aesgcm.decrypt_calls == 1
    assert state.stats().data_nonce_replays == 1
    assert state.stats().sessions_touched == 1


def test_secure_server_invalid_json_does_not_record_nonce_or_touch(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    key = b"\x01" * 32
    nonce = b"\x02" * 12
    addr = ("127.0.0.1", 50123)
    state = secure.SecureState()
    session, _ = _install_test_session(secure, state, addr, key)
    ciphertext = secure.AESGCM(key).encrypt(
        nonce, b"not-json", secure.DATA_AAD
    )
    packet = secure.DATA_PREFIX + nonce + ciphertext

    fake_queue, fake_socket = _run_secure_server_with_packets(
        monkeypatch, secure, [(packet, addr)], state=state
    )

    assert fake_queue.items == []
    assert fake_socket.sent == []
    assert session.last_seen == 1000.0
    assert state.stats().data_nonces_accepted == 0
    assert state.stats().sessions_touched == 0


@pytest.mark.parametrize(
    "message",
    [
        {"type": "ping", "source_id": "boat_001"},
        {"type": "nmea", "source_id": "boat_001"},
        {"type": "unknown", "source_id": "boat_001"},
    ],
)
def test_secure_server_invalid_message_shape_does_not_record_nonce_or_touch(
    monkeypatch,
    message,
):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    key = b"\x01" * 32
    nonce = b"\x02" * 12
    addr = ("127.0.0.1", 50123)
    state = secure.SecureState()
    session, _ = _install_test_session(secure, state, addr, key)
    packet = _encrypted_control_packet(secure, key, nonce, message)

    fake_queue, fake_socket = _run_secure_server_with_packets(
        monkeypatch, secure, [(packet, addr)], state=state
    )

    assert fake_queue.items == []
    assert fake_socket.sent == []
    assert session.last_seen == 1000.0
    assert state.stats().data_nonces_accepted == 0
    assert state.stats().sessions_touched == 0


def test_allowed_peer_activity_proactively_cleans_silent_expired_session(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(session_ttl=10.0)
    silent_addr = ("127.0.0.1", 50122)
    active_addr = ("127.0.0.1", 50123)
    state.install_session(silent_addr, "silent", object(), now=0.0)
    packet = secure.DATA_PREFIX + (b"\x00" * 28)

    _, fake_socket = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(packet, active_addr)],
        state=state,
        monotonic_clock=_FakeClock(10.0),
    )

    assert tuple(state._sessions) == ()
    assert state.stats().sessions_expired == 1
    assert fake_socket.sent == [(secure.NOSESSION_PREFIX, active_addr)]


def test_allowed_handshake_proactively_cleans_silent_expired_session(monkeypatch):
    secure, client_private_key = load_secure_module_with_fake_keys(
        monkeypatch, with_client_private_key=True
    )
    state = secure.SecureState(session_ttl=10.0)
    silent_addr = ("127.0.0.1", 50122)
    handshake_addr = ("127.0.0.1", 50123)
    state.install_session(silent_addr, "silent", object(), now=0.0)
    packet = _signed_handshake_packet(
        secure, client_private_key, "boat_001", 1000
    )

    _, fake_socket = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(packet, handshake_addr)],
        state=state,
        wall_clock=_FakeClock(1000.0),
        monotonic_clock=_FakeClock(10.0),
    )

    assert tuple(state._sessions) == (handshake_addr,)
    assert state.stats().sessions_expired == 1
    assert len(fake_socket.sent) == 1
    assert fake_socket.sent[0][0].startswith(b"OK|")


def test_unknown_allowed_packet_proactively_cleans_without_wall_clock(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(session_ttl=10.0)
    silent_addr = ("127.0.0.1", 50122)
    state.install_session(silent_addr, "silent", object(), now=0.0)
    wall_clock = _FakeClock(1000.0)
    monotonic_clock = _FakeClock(10.0)

    fake_queue, fake_socket = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(b"UNKNOWN", ("127.0.0.1", 50123))],
        state=state,
        wall_clock=wall_clock,
        monotonic_clock=monotonic_clock,
    )

    assert fake_queue.items == []
    assert fake_socket.sent == []
    assert state.stats().sessions_expired == 1
    assert state.stats().current_sessions == 0
    assert wall_clock.calls == 0
    assert monotonic_clock.calls == 1


@pytest.mark.parametrize(
    ("packet_factory", "expected_response"),
    [
        (
            lambda secure: secure.DATA_PREFIX + (b"\x00" * 28),
            lambda secure, addr: (secure.NOSESSION_PREFIX, addr),
        ),
        (
            lambda _secure: b"KEEPALIVE|boat_001|1000",
            lambda _secure, addr: (b"NOSESSION|boat_001", addr),
        ),
    ],
)
def test_exactly_expired_address_receives_existing_no_session_format(
    monkeypatch,
    packet_factory,
    expected_response,
):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(session_ttl=10.0)
    addr = ("127.0.0.1", 50123)
    state.install_session(addr, "boat_001", object(), now=0.0)

    _, fake_socket = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(packet_factory(secure), addr)],
        state=state,
        monotonic_clock=_FakeClock(10.0),
    )

    assert fake_socket.sent == [expected_response(secure, addr)]
    assert state.stats().sessions_expired == 1


def test_handshake_replay_accepts_first_key(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(handshake_replay_ttl=60.0)
    key = b"key"

    assert state.accept_handshake_replay(key, now=100.0)
    stats = state.stats()
    assert stats.handshake_replay_accepted == 1
    assert stats.current_handshake_replays == 1
    assert stats.peak_handshake_replays == 1


def test_handshake_replay_rejects_live_duplicate_without_refresh(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(handshake_replay_ttl=60.0)
    key = b"key"

    assert state.accept_handshake_replay(key, now=100.0)
    assert not state.accept_handshake_replay(key, now=120.0)
    assert not state.accept_handshake_replay(key, now=159.999)
    assert state.accept_handshake_replay(key, now=160.0)
    stats = state.stats()
    assert stats.handshake_replay_accepted == 2
    assert stats.handshake_replay_rejected == 2
    assert stats.handshake_replay_expired == 1
    assert stats.current_handshake_replays == 1


def test_handshake_replay_accepts_key_again_at_exact_expiry(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(handshake_replay_ttl=60.0)
    key = b"key"

    assert state.accept_handshake_replay(key, now=100.0)
    assert state.accept_handshake_replay(key, now=160.0)
    assert key in state._handshake_replays._live_by_key


def test_handshake_replay_removes_expired_front_prefix(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(
        handshake_replay_ttl=30.0,
        handshake_replay_max=100,
    )
    assert state.accept_handshake_replay(b"expired", now=0.0)
    assert state.accept_handshake_replay(b"fresh", now=20.0)

    assert state.accept_handshake_replay(b"new", now=30.0)

    assert set(state._handshake_replays._live_by_key) == {b"fresh", b"new"}
    stats = state.stats()
    assert stats.handshake_replay_expired == 1
    assert stats.current_handshake_replays == 2


def test_handshake_replay_capacity_evicts_oldest_live_key(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(
        handshake_replay_ttl=60.0,
        handshake_replay_max=2,
    )

    assert state.accept_handshake_replay(b"one", now=100.0)
    assert state.accept_handshake_replay(b"two", now=101.0)
    assert state.accept_handshake_replay(b"three", now=102.0)

    assert set(state._handshake_replays._live_by_key) == {b"two", b"three"}
    stats = state.stats()
    assert stats.handshake_replay_capacity_evicted == 1
    assert stats.handshake_replay_expired == 0
    assert stats.current_handshake_replays == 2
    assert stats.peak_handshake_replays == 2


def test_handshake_replay_accepts_different_keys_independently(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()

    assert state.accept_handshake_replay(b"one", now=100.0)
    assert state.accept_handshake_replay(b"two", now=100.0)
    assert state.stats().current_handshake_replays == 2


def test_handshake_replay_expiry_precedes_capacity_eviction(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(
        handshake_replay_ttl=10.0,
        handshake_replay_max=2,
    )
    assert state.accept_handshake_replay(b"expired", now=0.0)
    assert state.accept_handshake_replay(b"live", now=5.0)

    assert state.accept_handshake_replay(b"new", now=10.0)

    assert set(state._handshake_replays._live_by_key) == {b"live", b"new"}
    stats = state.stats()
    assert stats.handshake_replay_expired == 1
    assert stats.handshake_replay_capacity_evicted == 0


def test_expiring_set_stale_record_identity_cannot_remove_new_incarnation(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    expiring_set = secure._BoundedExpiringSet(ttl=10.0, max_entries=2)
    key = b"same"
    assert expiring_set.accept(key, now=0.0).accepted
    stale_record = expiring_set._live_by_key.pop(key)
    assert expiring_set.accept(key, now=1.0).accepted
    current_record = expiring_set._live_by_key[key]
    assert stale_record is not current_record

    seen, expired = expiring_set.contains(key, now=10.0)

    assert seen
    assert expired == 0
    assert expiring_set._live_by_key[key] is current_record
    assert tuple(expiring_set._expiry_order) == (current_record,)


@pytest.mark.parametrize("kind", ["handshake", "nonce"])
def test_expiring_state_cleanup_and_capacity_do_not_scan_or_call_min(
    monkeypatch,
    kind,
):
    secure = load_secure_module_with_fake_keys(monkeypatch)

    class NoScanDict(dict):
        def __iter__(self):
            raise AssertionError("live dictionary must not be iterated")

        def items(self):
            raise AssertionError("live dictionary must not be scanned")

        def values(self):
            raise AssertionError("live dictionary must not be scanned")

    if kind == "handshake":
        state = secure.SecureState(
            handshake_replay_ttl=10.0,
            handshake_replay_max=2,
        )
        assert state.accept_handshake_replay(b"expired", now=0.0)
        assert state.accept_handshake_replay(b"live", now=5.0)
        expiring_set = state._handshake_replays
        operation = lambda: state.accept_handshake_replay(b"new", now=10.0)
        capacity_operation = lambda: state.accept_handshake_replay(
            b"newest", now=11.0
        )
    else:
        state = secure.SecureState(
            data_nonce_ttl=10.0,
            data_nonce_max_per_session=2,
        )
        session = state.install_session(
            ("192.0.2.10", 50000), "boat_001", object(), now=0.0
        )
        assert state.accept_data_nonce(session, b"\x01" * 12, now=0.0)
        assert state.accept_data_nonce(session, b"\x02" * 12, now=5.0)
        expiring_set = session.seen_data_nonces
        operation = lambda: state.accept_data_nonce(
            session, b"\x03" * 12, now=10.0
        )
        capacity_operation = lambda: state.accept_data_nonce(
            session, b"\x04" * 12, now=11.0
        )

    expiring_set._live_by_key = NoScanDict(expiring_set._live_by_key)

    def fail_min(*_args, **_kwargs):
        raise AssertionError("min() must not be used for eviction")

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "min", fail_min)
        assert operation()
        assert capacity_operation()

    assert len(expiring_set._live_by_key) == 2


def test_proxy_encrypt_message_aes_gcm_uses_12_byte_nonce_and_nmea_aad():
    proxy = load_proxy_module()
    key = b"\x01" * 32
    plaintext = b'{"type":"nmea","payload":"!AIVDM,1,1,,A,payload,0*00"}'

    encrypted = proxy.encrypt_message_aes_gcm(plaintext, key)
    nonce = encrypted[:12]
    ciphertext_and_tag = encrypted[12:]

    assert len(nonce) == 12
    assert AESGCM(key).decrypt(nonce, ciphertext_and_tag, b"NMEA") == plaintext


def test_proxy_treats_no_session_from_configured_remote_as_invalidation():
    proxy = load_proxy_module()
    remote_addr = ("192.0.2.10", 17777)

    assert proxy.handle_server_packet(
        b"NOSESSION|boat_001",
        remote_addr,
        remote_addr,
        b"\x01" * 32,
        "boat_001",
        1,
    ) == proxy.SERVER_PACKET_NO_SESSION


def test_proxy_ignores_no_session_from_unexpected_address():
    proxy = load_proxy_module()

    assert proxy.handle_server_packet(
        b"NOSESSION|boat_001",
        ("192.0.2.11", 17777),
        ("192.0.2.10", 17777),
        b"\x01" * 32,
        "boat_001",
        1,
    ) == proxy.SERVER_PACKET_IGNORED


def test_proxy_ignores_no_session_from_unexpected_port():
    proxy = load_proxy_module()

    assert proxy.handle_server_packet(
        b"NOSESSION|boat_001",
        ("192.0.2.10", 17778),
        ("192.0.2.10", 17777),
        b"\x01" * 32,
        "boat_001",
        1,
    ) == proxy.SERVER_PACKET_IGNORED


def test_proxy_resolves_configured_remote_for_address_filtering(monkeypatch):
    proxy = load_proxy_module()
    resolved = ("192.0.2.10", 17777)
    monkeypatch.setattr(
        proxy.socket,
        "getaddrinfo",
        lambda *args: [
            (
                proxy.socket.AF_INET,
                proxy.socket.SOCK_DGRAM,
                17,
                "",
                resolved,
            )
        ],
    )

    assert proxy.resolve_remote_addr(
        "mixer.example", 17777, proxy.socket.AF_INET
    ) == resolved


def test_proxy_omitted_allow_from_is_unrestricted():
    proxy = load_proxy_module()

    policy = proxy.compile_local_ingress_policy({})

    assert policy.is_unrestricted
    assert policy.allows("192.0.2.15")


def test_proxy_empty_allow_from_denies_all():
    proxy = load_proxy_module()

    policy = proxy.compile_local_ingress_policy({"allow_from": []})

    assert policy.is_deny_all
    assert not policy.allows("192.0.2.15")


@pytest.mark.parametrize(
    ("entries", "addr"),
    [
        (["192.0.2.15"], ("192.0.2.15", 50000)),
        (["2001:db8::15"], ("2001:db8::15", 50000, 0, 0)),
        (["198.51.100.0/24"], ("198.51.100.44", 50000)),
        (["2001:db8:42::/64"], ("2001:db8:42::1234", 50000, 0, 0)),
        (["192.0.2.0/24"], ("::ffff:192.0.2.15", 50000, 0, 0)),
    ],
)
def test_proxy_local_allow_from_allows_matching_senders(monkeypatch, entries, addr):
    proxy = load_proxy_module()
    key = b"\x01" * 32
    remote_addr = ("192.0.2.10", 17777)
    policy = proxy.NetworkPolicy.from_entries(
        entries,
        context="nmea_sproxy.allow_from",
    )

    class LocalSocket:
        def recvfrom(self, _size):
            return b"!AIVDM,1,1,,A,payload,0*00", addr

    class OutSocket:
        def __init__(self):
            self.sent = []

        def sendto(self, data, destination):
            self.sent.append((data, destination))

    udp_sock = LocalSocket()
    out_sock = OutSocket()
    select_calls = []

    def fake_select(_readable, _writable, _exceptional, _timeout):
        if not select_calls:
            select_calls.append("local")
            return [udp_sock], [], []
        raise OSError("end test")

    monkeypatch.setattr(proxy.select, "select", fake_select)
    monkeypatch.setattr(proxy.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(proxy.time, "time", lambda: 1000)

    reason = proxy.forward_loop(
        udp_sock,
        out_sock,
        {
            "station_id": "boat_001",
            "keepalive_interval": 30,
            "peer_timeout": 90,
            "session_refresh_interval": 0,
        },
        key,
        remote_addr,
        policy,
    )

    assert reason == proxy.SESSION_END_SOCKET_ERROR
    assert len(out_sock.sent) == 1
    packet, destination = out_sock.sent[0]
    assert destination == remote_addr
    assert proxy.decrypt_secure_json_message(packet, key) == {
        "type": "nmea",
        "payload": "!AIVDM,1,1,,A,payload,0*00",
        "timestamp": 1000,
        "source_id": "boat_001",
    }


def test_proxy_local_allow_from_drops_denied_packet_before_processing(
    monkeypatch,
    capsys,
):
    proxy = load_proxy_module()
    remote_addr = ("192.0.2.10", 17777)
    policy = proxy.NetworkPolicy.from_entries(
        ["198.51.100.0/24"],
        context="nmea_sproxy.allow_from",
    )

    class UndecodablePayload:
        def decode(self, *_args, **_kwargs):
            raise AssertionError("denied payload must not be decoded")

    class LocalSocket:
        def recvfrom(self, _size):
            return UndecodablePayload(), ("192.0.2.15", 50000)

    class OutSocket:
        def __init__(self):
            self.sent = []

        def sendto(self, data, destination):
            self.sent.append((data, destination))

    def fail_extract(_text):
        raise AssertionError("denied payload must not be extracted")

    def fail_encrypt(_message, _key):
        raise AssertionError("denied payload must not be encrypted")

    udp_sock = LocalSocket()
    out_sock = OutSocket()
    select_calls = []

    def fake_select(_readable, _writable, _exceptional, _timeout):
        if not select_calls:
            select_calls.append("local")
            return [udp_sock], [], []
        raise OSError("end test")

    monkeypatch.setattr(proxy.select, "select", fake_select)
    monkeypatch.setattr(proxy.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(proxy, "extract_nmea_sentences", fail_extract)
    monkeypatch.setattr(proxy, "encrypt_secure_json_message", fail_encrypt)

    reason = proxy.forward_loop(
        udp_sock,
        out_sock,
        {
            "station_id": "boat_001",
            "keepalive_interval": 30,
            "peer_timeout": 90,
            "session_refresh_interval": 0,
        },
        b"\x01" * 32,
        remote_addr,
        policy,
    )

    captured = capsys.readouterr()
    assert reason == proxy.SESSION_END_SOCKET_ERROR
    assert out_sock.sent == []
    assert "payload" not in captured.out


@pytest.mark.parametrize(
    "value",
    [
        ["receiver.example.net"],
        ["192.0.2.1/33"],
        ["2001:db8::1/129"],
        ["192.0.2.15/24"],
        None,
    ],
)
def test_proxy_malformed_allow_from_fails_configuration(value):
    proxy = load_proxy_module()

    with pytest.raises(proxy.NetworkPolicyConfigError, match="allow_from"):
        proxy.compile_local_ingress_policy({"allow_from": value})


class _FakeCreatedSocket:
    def __init__(self, family, sock_type, bind_error=None):
        self.family = family
        self.sock_type = sock_type
        self.bind_error = bind_error
        self.bound = None
        self.closed = False

    def bind(self, addr):
        if self.bind_error:
            raise self.bind_error
        self.bound = addr

    def close(self):
        self.closed = True


def test_proxy_omitted_source_ip_leaves_outbound_socket_unbound(monkeypatch):
    proxy = load_proxy_module()
    created = []

    def fake_socket(family, sock_type):
        sock = _FakeCreatedSocket(family, sock_type)
        created.append(sock)
        return sock

    monkeypatch.setattr(proxy.socket, "socket", fake_socket)

    sock = proxy.create_outbound_socket(proxy.socket.AF_INET)

    assert sock is created[0]
    assert sock.family == proxy.socket.AF_INET
    assert sock.bound is None


@pytest.mark.parametrize(
    ("source_ip", "family"),
    [
        ("192.0.2.20", socket.AF_INET),
        ("2001:db8::20", socket.AF_INET6),
    ],
)
def test_proxy_source_ip_binds_outbound_socket(monkeypatch, source_ip, family):
    proxy = load_proxy_module()
    created = []

    def fake_socket(socket_family, sock_type):
        sock = _FakeCreatedSocket(socket_family, sock_type)
        created.append(sock)
        return sock

    monkeypatch.setattr(proxy.socket, "socket", fake_socket)
    source_address = proxy.parse_source_ip({"source_ip": source_ip})

    sock = proxy.create_outbound_socket(
        proxy.family_for_ip_address(source_address),
        source_address,
    )

    assert sock is created[0]
    assert sock.family == family
    assert sock.bound == (source_ip, 0)


def test_proxy_literal_source_and_remote_family_mismatch_is_rejected():
    proxy = load_proxy_module()
    source_address = proxy.parse_source_ip({"source_ip": "192.0.2.20"})

    with pytest.raises(proxy.ProxyConfigError, match="source_ip"):
        proxy.resolve_remote_endpoint(
            {"remote_host": "2001:db8::10", "remote_port": 19999},
            source_address,
        )


def test_proxy_hostname_resolution_is_constrained_to_source_family(monkeypatch):
    proxy = load_proxy_module()
    calls = []

    def fake_getaddrinfo(host, port, family, sock_type):
        calls.append((host, port, family, sock_type))
        return [
            (
                family,
                sock_type,
                17,
                "",
                ("2001:db8::10", port, 0, 0),
            )
        ]

    monkeypatch.setattr(proxy.socket, "getaddrinfo", fake_getaddrinfo)
    source_address = proxy.parse_source_ip({"source_ip": "2001:db8::20"})

    remote_addr, family = proxy.resolve_remote_endpoint(
        {"remote_host": "mixer.example.net", "remote_port": 19999},
        source_address,
    )

    assert family == proxy.socket.AF_INET6
    assert calls == [
        (
            "mixer.example.net",
            19999,
            proxy.socket.AF_INET6,
            proxy.socket.SOCK_DGRAM,
        )
    ]
    assert remote_addr == ("2001:db8::10", 19999, 0, 0)


def test_proxy_hostname_without_source_ip_preserves_ipv4_default(monkeypatch):
    proxy = load_proxy_module()
    calls = []

    def fake_getaddrinfo(host, port, family, sock_type):
        calls.append((host, port, family, sock_type))
        return [(family, sock_type, 17, "", ("192.0.2.10", port))]

    monkeypatch.setattr(proxy.socket, "getaddrinfo", fake_getaddrinfo)

    remote_addr, family = proxy.resolve_remote_endpoint(
        {"remote_host": "mixer.example.net", "remote_port": 19999}
    )

    assert family == proxy.socket.AF_INET
    assert calls[0][2] == proxy.socket.AF_INET
    assert remote_addr == ("192.0.2.10", 19999)


def test_proxy_no_matching_hostname_family_is_rejected(monkeypatch):
    proxy = load_proxy_module()

    def fake_getaddrinfo(*_args):
        raise proxy.socket.gaierror("no address")

    monkeypatch.setattr(proxy.socket, "getaddrinfo", fake_getaddrinfo)
    source_address = proxy.parse_source_ip({"source_ip": "2001:db8::20"})

    with pytest.raises(proxy.ProxyConfigError, match="no IPv6 address"):
        proxy.resolve_remote_endpoint(
            {"remote_host": "mixer.example.net", "remote_port": 19999},
            source_address,
        )


@pytest.mark.parametrize(
    "value",
    ["mixer.example.net", "192.0.2.20/24", "", None],
)
def test_proxy_invalid_source_ip_is_rejected(value):
    proxy = load_proxy_module()

    with pytest.raises(proxy.ProxyConfigError, match="source_ip"):
        proxy.parse_source_ip({"source_ip": value})


def test_proxy_source_ip_bind_error_names_configured_source(monkeypatch):
    proxy = load_proxy_module()
    created = []

    def fake_socket(family, sock_type):
        sock = _FakeCreatedSocket(
            family,
            sock_type,
            bind_error=OSError("cannot assign requested address"),
        )
        created.append(sock)
        return sock

    monkeypatch.setattr(proxy.socket, "socket", fake_socket)
    source_address = proxy.parse_source_ip({"source_ip": "192.0.2.20"})

    with pytest.raises(proxy.ProxyConfigError, match="192.0.2.20"):
        proxy.create_outbound_socket(proxy.socket.AF_INET, source_address)

    assert created[0].closed


def test_proxy_accepts_only_authenticated_matching_pong_as_liveness():
    proxy = load_proxy_module()
    key = b"\x01" * 32
    remote_addr = ("192.0.2.10", 17777)
    packet = proxy.encrypt_secure_json_message(
        {
            "type": "pong",
            "seq": 123,
            "timestamp": 1000,
            "source_id": "boat_001",
        },
        key,
    )

    assert proxy.handle_server_packet(
        packet, remote_addr, remote_addr, key, "boat_001", 123
    ) == proxy.SERVER_PACKET_AUTHENTICATED
    assert proxy.handle_server_packet(
        packet,
        ("192.0.2.10", 17778),
        remote_addr,
        key,
        "boat_001",
        123,
    ) == proxy.SERVER_PACKET_IGNORED
    assert proxy.handle_server_packet(
        b"PONG|123", remote_addr, remote_addr, key, "boat_001", 123
    ) == proxy.SERVER_PACKET_IGNORED
    assert proxy.handle_server_packet(
        packet, remote_addr, remote_addr, key, "boat_001", 124
    ) == proxy.SERVER_PACKET_IGNORED


class _FakeHandshakeSocket:
    def __init__(self, responses):
        self.responses = list(responses)
        self.sent = []
        self.timeout = 5.0

    def sendto(self, data, addr):
        self.sent.append((data, addr))

    def recvfrom(self, size):
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def gettimeout(self):
        return self.timeout

    def settimeout(self, timeout):
        self.timeout = timeout


def test_proxy_handshake_succeeds_after_stale_no_session_hint(monkeypatch):
    proxy = load_proxy_module()
    timestamp = 1000
    station_id = "boat_001"
    remote_addr = ("192.0.2.10", 17777)
    client_private_key = ec.generate_private_key(ec.SECP256R1())
    server_private_key = ec.generate_private_key(ec.SECP256R1())
    server_public_key = server_private_key.public_key()
    payload = (
        proxy.HANDSHAKE_PREFIX
        + station_id.encode()
        + timestamp.to_bytes(8, "big")
    )
    server_signature = proxy.sign_message(payload, server_private_key)
    sock = _FakeHandshakeSocket([
        (b"NOSESSION|boat_001", remote_addr),
        (b"OK|" + base64.b64encode(server_signature), remote_addr),
    ])
    monkeypatch.setattr(proxy.time, "time", lambda: timestamp)

    session_key = proxy.perform_handshake(
        sock,
        {"station_id": station_id},
        client_private_key,
        server_public_key,
        remote_addr,
    )

    client_signature = base64.b64decode(sock.sent[0][0].split(b"|")[3])
    shared_secret = client_private_key.exchange(ec.ECDH(), server_public_key)
    assert session_key == proxy.derive_session_key(
        shared_secret, client_signature, server_signature
    )
    assert sock.timeout == 5.0


def test_proxy_handshake_ignores_valid_reply_from_unexpected_remote(monkeypatch):
    proxy = load_proxy_module()
    timestamp = 1000
    station_id = "boat_001"
    remote_addr = ("192.0.2.10", 17777)
    other_addr = ("192.0.2.10", 17778)
    client_private_key = ec.generate_private_key(ec.SECP256R1())
    server_private_key = ec.generate_private_key(ec.SECP256R1())
    server_public_key = server_private_key.public_key()
    payload = (
        proxy.HANDSHAKE_PREFIX
        + station_id.encode()
        + timestamp.to_bytes(8, "big")
    )
    server_signature = proxy.sign_message(payload, server_private_key)
    response = b"OK|" + base64.b64encode(server_signature)
    sock = _FakeHandshakeSocket([
        (response, other_addr),
        (response, remote_addr),
    ])
    monkeypatch.setattr(proxy.time, "time", lambda: timestamp)

    session_key = proxy.perform_handshake(
        sock,
        {"station_id": station_id},
        client_private_key,
        server_public_key,
        remote_addr,
    )

    assert session_key is not None
    assert len(sock.sent) == 1


def test_proxy_handshake_timeout_returns_to_retry_loop(monkeypatch):
    proxy = load_proxy_module()
    client_private_key = ec.generate_private_key(ec.SECP256R1())
    server_public_key = ec.generate_private_key(ec.SECP256R1()).public_key()
    sock = _FakeHandshakeSocket([proxy.socket.timeout()])
    monkeypatch.setattr(proxy.time, "time", lambda: 1000)

    session_key = proxy.perform_handshake(
        sock,
        {"station_id": "boat_001"},
        client_private_key,
        server_public_key,
        ("192.0.2.10", 17777),
    )

    assert session_key is None
    assert len(sock.sent) == 1
    assert sock.timeout == 5.0


def test_proxy_handshake_socket_error_returns_to_retry_loop(monkeypatch):
    proxy = load_proxy_module()
    client_private_key = ec.generate_private_key(ec.SECP256R1())
    server_public_key = ec.generate_private_key(ec.SECP256R1()).public_key()

    class FailingSocket:
        def sendto(self, data, addr):
            raise OSError("network unavailable")

    monkeypatch.setattr(proxy.time, "time", lambda: 1000)

    session_key = proxy.perform_handshake(
        FailingSocket(),
        {"station_id": "boat_001"},
        client_private_key,
        server_public_key,
        ("192.0.2.10", 17777),
    )

    assert session_key is None


def test_proxy_invalidates_session_on_peer_timeout():
    proxy = load_proxy_module()
    config = {"peer_timeout": 90, "session_refresh_interval": 240}

    assert proxy.session_expiration_reason(
        190, 100, 100, config
    ) == proxy.SESSION_END_PEER_TIMEOUT


def test_proxy_invalidates_session_on_session_refresh_interval():
    proxy = load_proxy_module()
    config = {"peer_timeout": 1000, "session_refresh_interval": 240}

    assert proxy.session_expiration_reason(
        340, 100, 300, config
    ) == proxy.SESSION_END_PLANNED_REFRESH


def test_proxy_session_refresh_interval_zero_disables_planned_refresh():
    proxy = load_proxy_module()
    config = {"peer_timeout": 90, "session_refresh_interval": 0}

    assert proxy.session_expiration_reason(10000, 100, 9990, config) is None


def test_proxy_normal_ping_pong_does_not_trigger_periodic_reconnect():
    proxy = load_proxy_module()
    config = {
        "keepalive_interval": 30,
        "peer_timeout": 90,
        "session_refresh_interval": 0,
    }

    assert proxy.session_expiration_reason(3600, 0, 3595, config) is None
    assert proxy.session_poll_timeout(3600, 0, 3595, 3595, config) == 25


def test_proxy_planned_refresh_does_not_wait_reconnect_delay():
    proxy = load_proxy_module()
    config = {"reconnect_delay": 5}

    assert proxy.retry_delay_for_reason(
        proxy.SESSION_END_PLANNED_REFRESH, config
    ) is None


@pytest.mark.parametrize(
    "reason",
    [
        "peer_timeout",
        "nosession",
        "socket_error",
        "handshake_failure",
    ],
)
def test_proxy_failure_reasons_wait_before_retry(reason):
    proxy = load_proxy_module()
    config = {"reconnect_delay": 5}

    assert proxy.retry_delay_for_reason(reason, config) == 5


def _run_idle_proxy_session(monkeypatch, config):
    proxy = load_proxy_module()
    clock = [0.0]

    class FakeSocket:
        def __init__(self):
            self.sent = []

        def sendto(self, data, addr):
            self.sent.append((data, addr))

    def fake_select(readable, writable, exceptional, timeout):
        clock[0] += timeout
        return [], [], []

    monkeypatch.setattr(proxy.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(proxy.select, "select", fake_select)
    udp_sock = FakeSocket()
    out_sock = FakeSocket()
    reason = proxy.forward_loop(
        udp_sock,
        out_sock,
        config,
        b"\x01" * 32,
        ("192.0.2.10", 17777),
    )
    return proxy, reason, out_sock


def test_proxy_forward_loop_exits_on_peer_timeout_without_local_udp(monkeypatch):
    config = {
        "station_id": "boat_001",
        "keepalive_interval": 30,
        "peer_timeout": 90,
        "session_refresh_interval": 240,
    }

    proxy, reason, out_sock = _run_idle_proxy_session(monkeypatch, config)

    assert reason == proxy.SESSION_END_PEER_TIMEOUT
    assert out_sock.sent
    assert all(packet.startswith(proxy.DATA_PREFIX) for packet, _ in out_sock.sent)


def test_proxy_forward_loop_exits_for_planned_refresh_without_local_udp(monkeypatch):
    config = {
        "station_id": "boat_001",
        "keepalive_interval": 30,
        "peer_timeout": 1000,
        "session_refresh_interval": 60,
    }

    proxy, reason, _ = _run_idle_proxy_session(monkeypatch, config)

    assert reason == proxy.SESSION_END_PLANNED_REFRESH


def test_proxy_forward_loop_exits_on_no_session(monkeypatch):
    proxy = load_proxy_module()
    remote_addr = ("192.0.2.10", 17777)

    class FakeLocalSocket:
        pass

    class FakeOutSocket:
        def recvfrom(self, size):
            return b"NOSESSION|boat_001", remote_addr

    udp_sock = FakeLocalSocket()
    out_sock = FakeOutSocket()
    monkeypatch.setattr(
        proxy.select,
        "select",
        lambda readable, writable, exceptional, timeout: ([out_sock], [], []),
    )

    reason = proxy.forward_loop(
        udp_sock,
        out_sock,
        {
            "station_id": "boat_001",
            "keepalive_interval": 30,
            "peer_timeout": 90,
            "session_refresh_interval": 0,
        },
        b"\x01" * 32,
        remote_addr,
    )

    assert reason == proxy.SESSION_END_NOSESSION


def test_proxy_forward_loop_reports_socket_error(monkeypatch):
    proxy = load_proxy_module()
    monkeypatch.setattr(
        proxy.select,
        "select",
        lambda *args: (_ for _ in ()).throw(OSError("network unavailable")),
    )

    reason = proxy.forward_loop(
        object(),
        object(),
        {
            "station_id": "boat_001",
            "keepalive_interval": 30,
            "peer_timeout": 90,
            "session_refresh_interval": 0,
        },
        b"\x01" * 32,
        ("192.0.2.10", 17777),
    )

    assert reason == proxy.SESSION_END_SOCKET_ERROR


def test_proxy_healthy_ping_pong_runs_past_old_refresh_interval(monkeypatch):
    proxy = load_proxy_module()
    key = b"\x01" * 32
    remote_addr = ("192.0.2.10", 17777)
    clock = [0.0]

    class FakeLocalSocket:
        pass

    class FakeOutSocket:
        def __init__(self):
            self.responses = []
            self.pong_count = 0

        def sendto(self, data, addr):
            ping = proxy.decrypt_secure_json_message(data, key)
            self.responses.append(
                proxy.encrypt_secure_json_message(
                    {
                        "type": "pong",
                        "seq": ping["seq"],
                        "timestamp": int(clock[0]),
                        "source_id": "boat_001",
                    },
                    key,
                )
            )

        def recvfrom(self, size):
            self.pong_count += 1
            return self.responses.pop(0), remote_addr

    udp_sock = FakeLocalSocket()
    out_sock = FakeOutSocket()

    def fake_select(readable, writable, exceptional, timeout):
        if out_sock.responses:
            return [out_sock], [], []
        if out_sock.pong_count >= 12:
            raise OSError("end test")
        clock[0] += timeout
        return [], [], []

    monkeypatch.setattr(proxy.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(proxy.select, "select", fake_select)

    reason = proxy.forward_loop(
        udp_sock,
        out_sock,
        {
            "station_id": "boat_001",
            "keepalive_interval": 30,
            "peer_timeout": 90,
            "session_refresh_interval": 0,
        },
        key,
        remote_addr,
    )

    assert clock[0] >= 360
    assert out_sock.pong_count == 12
    assert reason == proxy.SESSION_END_SOCKET_ERROR


def test_proxy_reconnect_lifecycle_has_no_keepalive_worker():
    proxy = load_proxy_module()

    assert not hasattr(proxy, "send_keepalive_loop")
    assert not hasattr(proxy, "threading")


def test_secure_data_packet_parser_rejects_packet_without_data_prefix(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)

    with pytest.raises(ValueError):
        secure.parse_secure_data_packet(b"not secure data")


def test_secure_data_packet_parser_rejects_only_data_prefix(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)

    with pytest.raises(ValueError):
        secure.parse_secure_data_packet(secure.DATA_PREFIX)


def test_secure_data_packet_parser_rejects_nonce_without_gcm_tag(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)

    with pytest.raises(ValueError):
        secure.parse_secure_data_packet(secure.DATA_PREFIX + (b"\x00" * 12))


def test_secure_data_packet_parser_accepts_minimum_structural_packet(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    nonce = b"\x01" * 12
    ciphertext_and_tag = b"\x02" * 16

    parsed_nonce, parsed_ciphertext = secure.parse_secure_data_packet(
        secure.DATA_PREFIX + nonce + ciphertext_and_tag
    )

    assert parsed_nonce == nonce
    assert parsed_ciphertext == ciphertext_and_tag


def test_secure_data_packet_parser_output_decrypts_valid_proxy_packet(monkeypatch):
    proxy = load_proxy_module()
    secure = load_secure_module_with_fake_keys(monkeypatch)
    key = b"\x01" * 32
    plaintext = b'{"type":"nmea","payload":"!AIVDM,1,1,,A,payload,0*00"}'
    encrypted = proxy.encrypt_message_aes_gcm(plaintext, key)

    nonce, ciphertext = secure.parse_secure_data_packet(secure.DATA_PREFIX + encrypted)

    assert AESGCM(key).decrypt(nonce, ciphertext, b"NMEA") == plaintext


def test_parse_keepalive_packet_accepts_valid_packet(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)

    assert secure.parse_keepalive_packet(b"KEEPALIVE|boat_001|1234567890") == (
        "boat_001",
        1234567890,
    )


def test_parse_keepalive_packet_rejects_wrong_prefix(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)

    with pytest.raises(ValueError):
        secure.parse_keepalive_packet(b"NOTKEEPALIVE|boat_001|1234567890")


def test_parse_keepalive_packet_rejects_missing_station_id(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)

    with pytest.raises(ValueError):
        secure.parse_keepalive_packet(b"KEEPALIVE||1234567890")


def test_parse_keepalive_packet_rejects_missing_timestamp(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)

    with pytest.raises(ValueError):
        secure.parse_keepalive_packet(b"KEEPALIVE|boat_001|")


def test_parse_keepalive_packet_rejects_non_numeric_timestamp(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)

    with pytest.raises(ValueError):
        secure.parse_keepalive_packet(b"KEEPALIVE|boat_001|not-a-timestamp")


def test_session_ttl_seconds_is_300(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)

    assert secure.SESSION_TTL_SECONDS == 300
    assert secure.SESSION_MAX == 100000


def test_data_nonce_constants(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)

    assert secure.DATA_NONCE_TTL_SECONDS == secure.SESSION_TTL_SECONDS
    assert secure.DATA_NONCE_MAX_PER_SESSION == 100000


@pytest.mark.parametrize(
    "field",
    ["max_sessions", "handshake_replay_max", "data_nonce_max_per_session"],
)
@pytest.mark.parametrize("value", [True, 1.0, 1.5, "2", None])
def test_secure_state_rejects_non_integer_maximums(monkeypatch, field, value):
    secure = load_secure_module_with_fake_keys(monkeypatch)

    with pytest.raises(TypeError):
        secure.SecureState(**{field: value})


@pytest.mark.parametrize(
    "field",
    ["max_sessions", "handshake_replay_max", "data_nonce_max_per_session"],
)
@pytest.mark.parametrize("value", [0, -1])
def test_secure_state_rejects_non_positive_maximums(monkeypatch, field, value):
    secure = load_secure_module_with_fake_keys(monkeypatch)

    with pytest.raises(ValueError):
        secure.SecureState(**{field: value})


@pytest.mark.parametrize(
    "field",
    ["session_ttl", "handshake_replay_ttl", "data_nonce_ttl"],
)
@pytest.mark.parametrize("value", [True, "1", None])
def test_secure_state_rejects_non_numeric_ttls(monkeypatch, field, value):
    secure = load_secure_module_with_fake_keys(monkeypatch)

    with pytest.raises(TypeError):
        secure.SecureState(**{field: value})


@pytest.mark.parametrize(
    "field",
    ["session_ttl", "handshake_replay_ttl", "data_nonce_ttl"],
)
@pytest.mark.parametrize("value", [0, 0.0, -1, -0.5])
def test_secure_state_rejects_non_positive_ttls(monkeypatch, field, value):
    secure = load_secure_module_with_fake_keys(monkeypatch)

    with pytest.raises(ValueError):
        secure.SecureState(**{field: value})


def test_secure_state_accepts_positive_integer_and_float_limits(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)

    state = secure.SecureState(
        session_ttl=1,
        max_sessions=1,
        handshake_replay_ttl=1.5,
        handshake_replay_max=2,
        data_nonce_ttl=2.5,
        data_nonce_max_per_session=3,
    )

    assert state.stats().current_sessions == 0


def test_secure_session_stores_identity_crypto_and_monotonic_timestamps(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    addr = ("192.0.2.10", 50000)
    aesgcm = object()

    session = state.install_session(addr, "boat_001", aesgcm, now=100.0)

    assert session.station_id == "boat_001"
    assert session.aesgcm is aesgcm
    assert session.created_at == 100.0
    assert session.last_seen == 100.0
    assert len(session.seen_data_nonces) == 0
    assert tuple(state._sessions) == (addr,)
    assert state.stats().sessions_created == 1


def test_data_nonce_accepts_first_validated_nonce(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(data_nonce_ttl=60.0)
    session = state.install_session(
        ("192.0.2.10", 50000), "boat_001", object(), now=100.0
    )
    nonce = b"\x01" * 12

    assert not state.data_nonce_seen(session, nonce, now=100.0)
    assert state.accept_data_nonce(session, nonce, now=100.0)
    stats = state.stats()
    assert stats.data_nonces_accepted == 1
    assert stats.data_nonce_replays == 0
    assert stats.current_data_nonces == 1
    assert stats.peak_data_nonces == 1


def test_data_nonce_replay_does_not_refresh_expiry(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(data_nonce_ttl=60.0)
    session = state.install_session(
        ("192.0.2.10", 50000), "boat_001", object(), now=100.0
    )
    nonce = b"\x01" * 12

    assert state.accept_data_nonce(session, nonce, now=100.0)
    assert state.data_nonce_seen(session, nonce, now=120.0)
    assert state.data_nonce_seen(session, nonce, now=159.999)
    assert not state.data_nonce_seen(session, nonce, now=160.0)
    assert state.accept_data_nonce(session, nonce, now=160.0)

    stats = state.stats()
    assert stats.data_nonces_accepted == 2
    assert stats.data_nonce_replays == 2
    assert stats.data_nonces_expired == 1
    assert stats.current_data_nonces == 1


def test_data_nonce_cleanup_removes_only_expired_front_prefix(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(data_nonce_ttl=30.0)
    session = state.install_session(
        ("192.0.2.10", 50000), "boat_001", object(), now=0.0
    )
    expired_nonce = b"\x01" * 12
    active_nonce = b"\x02" * 12

    assert state.accept_data_nonce(session, expired_nonce, now=0.0)
    assert state.accept_data_nonce(session, active_nonce, now=20.0)
    assert not state.data_nonce_seen(session, b"\x03" * 12, now=30.0)

    assert set(session.seen_data_nonces._live_by_key) == {active_nonce}
    stats = state.stats()
    assert stats.data_nonces_expired == 1
    assert stats.current_data_nonces == 1


def test_data_nonce_capacity_evicts_oldest_live_nonce(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(
        data_nonce_ttl=60.0,
        data_nonce_max_per_session=2,
    )
    session = state.install_session(
        ("192.0.2.10", 50000), "boat_001", object(), now=100.0
    )
    one = b"\x01" * 12
    two = b"\x02" * 12
    three = b"\x03" * 12

    assert state.accept_data_nonce(session, one, now=100.0)
    assert state.accept_data_nonce(session, two, now=101.0)
    assert state.accept_data_nonce(session, three, now=102.0)

    assert set(session.seen_data_nonces._live_by_key) == {two, three}
    stats = state.stats()
    assert stats.data_nonces_capacity_evicted == 1
    assert stats.data_nonces_expired == 0
    assert stats.current_data_nonces == 2
    assert stats.peak_data_nonces == 2


def test_data_nonce_expiry_precedes_capacity_eviction(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(
        data_nonce_ttl=10.0,
        data_nonce_max_per_session=2,
    )
    session = state.install_session(
        ("192.0.2.10", 50000), "boat_001", object(), now=0.0
    )
    expired = b"\x01" * 12
    live = b"\x02" * 12
    new = b"\x03" * 12
    assert state.accept_data_nonce(session, expired, now=0.0)
    assert state.accept_data_nonce(session, live, now=5.0)

    assert state.accept_data_nonce(session, new, now=10.0)

    assert set(session.seen_data_nonces._live_by_key) == {live, new}
    stats = state.stats()
    assert stats.data_nonces_expired == 1
    assert stats.data_nonces_capacity_evicted == 0


def test_data_nonce_caches_are_independent_per_session(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(data_nonce_ttl=60.0)
    first = state.install_session(
        ("192.0.2.10", 50000), "boat_001", object(), now=100.0
    )
    second = state.install_session(
        ("192.0.2.11", 50001), "boat_002", object(), now=100.0
    )
    nonce = b"\x01" * 12

    assert state.accept_data_nonce(first, nonce, now=100.0)
    assert state.data_nonce_seen(first, nonce, now=100.0)
    assert not state.data_nonce_seen(second, nonce, now=100.0)
    assert state.accept_data_nonce(second, nonce, now=100.0)
    assert state.stats().current_data_nonces == 2


def test_get_active_session_uses_exact_ttl_boundary(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(session_ttl=30.0)
    addr = ("192.0.2.10", 50000)
    session = state.install_session(addr, "boat_001", object(), now=100.0)

    assert state.get_active_session(addr, now=129.999) is session
    assert state.get_active_session(addr, now=130.0) is None
    stats = state.stats()
    assert stats.sessions_expired == 1
    assert stats.current_sessions == 0


def test_get_active_session_returns_none_for_missing_session(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()

    assert state.get_active_session(("192.0.2.10", 50000), now=120.0) is None
    assert state.stats().sessions_expired == 0


def test_touch_session_updates_lru_order_without_changing_creation(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    first_addr = ("192.0.2.10", 50000)
    second_addr = ("192.0.2.11", 50001)
    first = state.install_session(first_addr, "first", object(), now=100.0)
    state.install_session(second_addr, "second", object(), now=110.0)

    assert tuple(state._sessions) == (first_addr, second_addr)
    assert state.touch_session(first_addr, first, now=125.0)

    assert first.created_at == 100.0
    assert first.last_seen == 125.0
    assert tuple(state._sessions) == (second_addr, first_addr)
    assert state.stats().sessions_touched == 1


def test_invalid_keepalive_does_not_touch_or_reorder_session(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    first_addr = ("192.0.2.10", 50000)
    second_addr = ("192.0.2.11", 50001)
    first = state.install_session(first_addr, "first", object(), now=100.0)
    state.install_session(second_addr, "second", object(), now=110.0)

    assert not state.handle_keepalive(first_addr, "wrong", now=120.0)

    assert first.last_seen == 100.0
    assert tuple(state._sessions) == (first_addr, second_addr)
    assert state.stats().sessions_touched == 0


def test_valid_keepalive_touches_and_reorders_session(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    first_addr = ("192.0.2.10", 50000)
    second_addr = ("192.0.2.11", 50001)
    first = state.install_session(first_addr, "first", object(), now=100.0)
    state.install_session(second_addr, "second", object(), now=110.0)

    assert state.handle_keepalive(first_addr, "first", now=120.0)

    assert first.last_seen == 120.0
    assert tuple(state._sessions) == (second_addr, first_addr)
    assert state.stats().sessions_touched == 1


def test_session_cleanup_removes_expired_lru_prefix_and_stops_at_live(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(session_ttl=30.0)
    expired_addr = ("192.0.2.10", 50000)
    active_addr = ("192.0.2.11", 50001)
    state.install_session(expired_addr, "expired", object(), now=90.0)
    state.install_session(active_addr, "active", object(), now=100.0)

    removed = state.cleanup_expired_sessions(now=120.0)

    assert removed == [expired_addr]
    assert tuple(state._sessions) == (active_addr,)
    assert state.stats().sessions_expired == 1


def test_session_capacity_evicts_least_recently_seen(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(max_sessions=2)
    first_addr = ("192.0.2.10", 50000)
    second_addr = ("192.0.2.11", 50001)
    third_addr = ("192.0.2.12", 50002)
    first = state.install_session(first_addr, "first", object(), now=100.0)
    state.install_session(second_addr, "second", object(), now=110.0)
    assert state.touch_session(first_addr, first, now=120.0)

    state.install_session(third_addr, "third", object(), now=130.0)

    assert tuple(state._sessions) == (first_addr, third_addr)
    stats = state.stats()
    assert stats.sessions_capacity_evicted == 1
    assert stats.sessions_expired == 0
    assert stats.current_sessions == 2
    assert stats.peak_sessions == 2


def test_equal_session_timestamps_use_deterministic_activity_order(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(max_sessions=2)
    first_addr = ("192.0.2.10", 50000)
    second_addr = ("192.0.2.11", 50001)
    third_addr = ("192.0.2.12", 50002)
    state.install_session(first_addr, "first", object(), now=100.0)
    state.install_session(second_addr, "second", object(), now=100.0)

    state.install_session(third_addr, "third", object(), now=100.0)

    assert tuple(state._sessions) == (second_addr, third_addr)
    assert state.stats().sessions_capacity_evicted == 1


def test_expired_sessions_are_removed_before_capacity_eviction(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(session_ttl=30.0, max_sessions=2)
    expired_addr = ("192.0.2.10", 50000)
    active_addr = ("192.0.2.11", 50001)
    new_addr = ("192.0.2.12", 50002)
    state.install_session(expired_addr, "expired", object(), now=90.0)
    state.install_session(active_addr, "active", object(), now=100.0)

    state.install_session(new_addr, "new", object(), now=120.0)

    assert tuple(state._sessions) == (active_addr, new_addr)
    stats = state.stats()
    assert stats.sessions_expired == 1
    assert stats.sessions_capacity_evicted == 0


def test_live_session_replacement_discards_nonce_state_without_other_eviction(
    monkeypatch,
):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(max_sessions=2)
    replaced_addr = ("192.0.2.10", 50000)
    other_addr = ("192.0.2.11", 50001)
    old_aesgcm = object()
    new_aesgcm = object()
    old = state.install_session(replaced_addr, "old", old_aesgcm, now=100.0)
    state.install_session(other_addr, "other", object(), now=110.0)
    nonce = b"\x01" * 12
    assert state.accept_data_nonce(old, nonce, now=115.0)

    new = state.install_session(
        replaced_addr, "new", new_aesgcm, now=120.0
    )

    assert new is state._sessions[replaced_addr]
    assert new is not old
    assert new.aesgcm is new_aesgcm
    assert tuple(state._sessions) == (other_addr, replaced_addr)
    assert not state.data_nonce_seen(new, nonce, now=120.0)
    assert state.accept_data_nonce(new, nonce, now=120.0)
    stats = state.stats()
    assert stats.sessions_created == 3
    assert stats.sessions_replaced == 1
    assert stats.sessions_capacity_evicted == 0
    assert stats.data_nonces_session_discarded == 1


def test_expired_same_address_installation_is_not_live_replacement(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(session_ttl=30.0)
    addr = ("192.0.2.10", 50000)
    old = state.install_session(addr, "old", object(), now=100.0)
    assert state.accept_data_nonce(old, b"\x01" * 12, now=100.0)

    new = state.install_session(addr, "new", object(), now=130.0)

    assert new is state._sessions[addr]
    stats = state.stats()
    assert stats.sessions_created == 2
    assert stats.sessions_replaced == 0
    assert stats.sessions_expired == 1
    assert stats.data_nonces_session_discarded == 1


def test_session_capacity_discard_counts_retained_nonces_once(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(max_sessions=1)
    first = state.install_session(
        ("192.0.2.10", 50000), "first", object(), now=100.0
    )
    assert state.accept_data_nonce(first, b"\x01" * 12, now=100.0)
    assert state.accept_data_nonce(first, b"\x02" * 12, now=101.0)

    state.install_session(
        ("192.0.2.11", 50001), "second", object(), now=102.0
    )

    stats = state.stats()
    assert stats.sessions_capacity_evicted == 1
    assert stats.sessions_expired == 0
    assert stats.data_nonces_session_discarded == 2
    assert stats.data_nonces_expired == 0
    assert stats.data_nonces_capacity_evicted == 0
    assert stats.current_data_nonces == 0


def test_removed_session_handle_cannot_mutate_nonce_state_or_statistics(
    monkeypatch,
):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(max_sessions=1)
    old_addr = ("192.0.2.10", 50000)
    old = state.install_session(
        old_addr, "old", object(), now=100.0
    )
    assert state.accept_data_nonce(old, b"\x01" * 12, now=100.0)

    state.install_session(
        ("192.0.2.11", 50001), "new", object(), now=101.0
    )
    before = state.stats()

    assert not state.data_nonce_seen(old, b"\x01" * 12, now=102.0)
    assert not state.accept_data_nonce(old, b"\x02" * 12, now=102.0)
    assert state.stats() == before
    assert state.stats().current_data_nonces == 0


def test_stale_nonce_check_does_not_cleanup_unrelated_expired_session(
    monkeypatch,
):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(session_ttl=10.0, max_sessions=2)
    stale_addr = ("192.0.2.10", 50000)
    expiring_addr = ("192.0.2.11", 50001)
    stale = state.install_session(
        stale_addr, "stale", object(), now=0.0
    )
    expiring = state.install_session(
        expiring_addr, "expiring", object(), now=2.0
    )
    expiring_nonce = b"\x01" * 12
    assert state.accept_data_nonce(
        expiring, expiring_nonce, now=2.0
    )
    replacement = state.install_session(
        stale_addr, "replacement", object(), now=3.0
    )
    assert replacement is not stale

    before_stats = state.stats()
    before_sessions = tuple(state._sessions.items())
    before_expiring_nonces = set(
        expiring.seen_data_nonces._live_by_key
    )
    before_stale_nonces = set(stale.seen_data_nonces._live_by_key)

    assert not state.data_nonce_seen(
        stale, b"\x02" * 12, now=12.0
    )

    assert state.stats() == before_stats
    assert tuple(state._sessions.items()) == before_sessions
    assert state._sessions[expiring_addr] is expiring
    assert (
        set(expiring.seen_data_nonces._live_by_key)
        == before_expiring_nonces
    )
    assert (
        set(stale.seen_data_nonces._live_by_key)
        == before_stale_nonces
    )


def test_stale_nonce_accept_does_not_cleanup_unrelated_expired_session(
    monkeypatch,
):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(session_ttl=10.0, max_sessions=2)
    stale_addr = ("192.0.2.10", 50000)
    expiring_addr = ("192.0.2.11", 50001)
    replacement_addr = ("192.0.2.12", 50002)
    stale = state.install_session(
        stale_addr, "stale", object(), now=0.0
    )
    assert state.accept_data_nonce(
        stale, b"\x01" * 12, now=0.0
    )
    expiring = state.install_session(
        expiring_addr, "expiring", object(), now=2.0
    )
    expiring_nonce = b"\x02" * 12
    assert state.accept_data_nonce(
        expiring, expiring_nonce, now=2.0
    )
    state.install_session(
        replacement_addr, "replacement", object(), now=3.0
    )
    assert stale_addr not in state._sessions

    before_stats = state.stats()
    before_sessions = tuple(state._sessions.items())
    before_expiring_nonces = set(
        expiring.seen_data_nonces._live_by_key
    )
    before_stale_nonces = set(stale.seen_data_nonces._live_by_key)

    assert not state.accept_data_nonce(
        stale, b"\x03" * 12, now=12.0
    )

    assert state.stats() == before_stats
    assert tuple(state._sessions.items()) == before_sessions
    assert state._sessions[expiring_addr] is expiring
    assert (
        set(expiring.seen_data_nonces._live_by_key)
        == before_expiring_nonces
    )
    assert (
        set(stale.seen_data_nonces._live_by_key)
        == before_stale_nonces
    )


def test_stale_touch_does_not_cleanup_unrelated_expired_session(
    monkeypatch,
):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(session_ttl=10.0, max_sessions=2)
    stale_addr = ("192.0.2.10", 50000)
    expiring_addr = ("192.0.2.11", 50001)
    stale = state.install_session(
        stale_addr, "stale", object(), now=0.0
    )
    expiring = state.install_session(
        expiring_addr, "expiring", object(), now=2.0
    )
    state.install_session(
        stale_addr, "replacement", object(), now=3.0
    )

    before_stats = state.stats()
    before_sessions = tuple(state._sessions.items())

    assert not state.touch_session(
        stale_addr, stale, now=12.0
    )

    assert state.stats() == before_stats
    assert tuple(state._sessions.items()) == before_sessions
    assert state._sessions[expiring_addr] is expiring
    assert state.stats().sessions_touched == before_stats.sessions_touched


def test_touch_address_mismatch_is_side_effect_free_before_cleanup(
    monkeypatch,
):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(session_ttl=10.0)
    expiring_addr = ("192.0.2.10", 50000)
    current_addr = ("192.0.2.11", 50001)
    expiring = state.install_session(
        expiring_addr, "expiring", object(), now=2.0
    )
    current = state.install_session(
        current_addr, "current", object(), now=3.0
    )

    before_stats = state.stats()
    before_sessions = tuple(state._sessions.items())

    assert not state.touch_session(
        expiring_addr, current, now=12.0
    )

    assert state.stats() == before_stats
    assert tuple(state._sessions.items()) == before_sessions
    assert state._sessions[expiring_addr] is expiring
    assert current.last_seen == 3.0


def test_expired_session_handle_cannot_accept_or_check_nonces(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(session_ttl=10.0)
    addr = ("192.0.2.10", 50000)
    session = state.install_session(
        addr, "old", object(), now=0.0
    )
    assert state.accept_data_nonce(session, b"\x01" * 12, now=0.0)

    assert not state.data_nonce_seen(session, b"\x01" * 12, now=10.0)

    after_expiry = state.stats()
    assert addr not in state._sessions
    assert after_expiry.sessions_expired == 1
    assert after_expiry.data_nonces_session_discarded == 1
    assert after_expiry.data_nonces_accepted == 1
    assert after_expiry.current_sessions == 0
    assert after_expiry.current_data_nonces == 0

    assert not state.accept_data_nonce(
        session, b"\x02" * 12, now=10.0
    )
    assert not state.touch_session(addr, session, now=10.0)
    assert state.stats() == after_expiry
    assert addr not in state._sessions


def test_secure_state_stats_start_at_zero_and_are_frozen_snapshots(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()

    initial = state.stats()

    assert set(vars(initial)) == {
        "handshake_replay_accepted",
        "handshake_replay_rejected",
        "handshake_replay_expired",
        "handshake_replay_capacity_evicted",
        "sessions_created",
        "sessions_replaced",
        "sessions_touched",
        "sessions_expired",
        "sessions_capacity_evicted",
        "data_nonces_accepted",
        "data_nonce_replays",
        "data_nonces_expired",
        "data_nonces_capacity_evicted",
        "data_nonces_session_discarded",
        "current_handshake_replays",
        "peak_handshake_replays",
        "current_sessions",
        "peak_sessions",
        "current_data_nonces",
        "peak_data_nonces",
    }
    assert all(value == 0 for value in vars(initial).values())
    with pytest.raises(FrozenInstanceError):
        initial.current_sessions = 1

    state.install_session(
        ("192.0.2.10", 50000), "boat_001", object(), now=100.0
    )
    current = state.stats()
    assert initial.current_sessions == 0
    assert initial.sessions_created == 0
    assert current.current_sessions == 1
    assert current.sessions_created == 1


def test_secure_state_stats_do_not_read_clocks_or_cleanup(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(session_ttl=1.0)
    addr = ("192.0.2.10", 50000)
    state.install_session(addr, "boat_001", object(), now=0.0)

    def fail_clock():
        raise AssertionError("stats must not read a clock")

    monkeypatch.setattr(secure.time, "time", fail_clock)
    monkeypatch.setattr(secure.time, "monotonic", fail_clock)

    stats = state.stats()

    assert stats.current_sessions == 1
    assert tuple(state._sessions) == (addr,)
    assert stats.sessions_expired == 0


def test_secure_server_private_key_prefers_canonical_path(monkeypatch):
    secure = load_secure_module_with_fake_keys(
        monkeypatch,
        existing_paths=[
            SERVER_CANONICAL_PRIVATE_KEY_PATH,
            SERVER_LEGACY_ETC_PRIVATE_KEY_PATH,
        ],
    )

    assert secure.priv_key_path == SERVER_CANONICAL_PRIVATE_KEY_PATH


def test_secure_server_private_key_uses_legacy_etc_when_canonical_absent(monkeypatch):
    secure = load_secure_module_with_fake_keys(
        monkeypatch,
        existing_paths=[SERVER_LEGACY_ETC_PRIVATE_KEY_PATH],
    )

    assert secure.priv_key_path == SERVER_LEGACY_ETC_PRIVATE_KEY_PATH


def test_secure_server_private_key_uses_local_legacy_fallback(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)

    assert _normalize_path(secure.priv_key_path) == _normalize_path(
        ROOT / SERVER_PRIVATE_KEY_FILENAME
    )


def test_proxy_default_station_private_key_prefers_canonical_path(monkeypatch, tmp_path):
    proxy = load_proxy_module()
    monkeypatch.setattr(
        proxy.os.path,
        "exists",
        lambda path: _normalize_path(path) == _normalize_path(
            STATION_CANONICAL_PRIVATE_KEY_PATH
        ),
    )

    config = proxy.load_config(str(tmp_path / "missing.yaml"))

    assert config["station_private_key"] == STATION_CANONICAL_PRIVATE_KEY_PATH


def test_proxy_configured_legacy_station_private_key_still_works(tmp_path):
    proxy = load_proxy_module()
    config_path = tmp_path / "config.yaml"
    config_path.write_text("station_private_key: station_private.key\n", encoding="utf-8")

    config = proxy.load_config(str(config_path))

    assert config["station_private_key"] == str(tmp_path / "station_private.key")


def test_proxy_canonical_station_private_key_falls_back_to_legacy_sibling(
    monkeypatch,
    tmp_path,
):
    proxy = load_proxy_module()
    config_path = tmp_path / "config.yaml"
    canonical_path = tmp_path / "station_private.pem"
    legacy_path = tmp_path / "station_private.key"
    config_path.write_text(
        "station_private_key: station_private.pem\n",
        encoding="utf-8",
    )
    real_exists = proxy.os.path.exists
    monkeypatch.setattr(
        proxy.os.path,
        "exists",
        lambda path: (
            os.path.normpath(os.fspath(path)) == os.path.normpath(str(legacy_path))
            or real_exists(path)
        ),
    )

    config = proxy.load_config(str(config_path))

    assert not canonical_path.exists()
    assert config["station_private_key"] == str(legacy_path)


def test_proxy_manual_local_config_resolves_local_key_paths():
    proxy = load_proxy_module()

    config = proxy.load_config(proxy.LOCAL_CONFIG_PATH)

    canonical_path = NMEA_SPROXY_DIR / "station_private.pem"
    legacy_path = NMEA_SPROXY_DIR / "station_private.key"
    expected_station_path = legacy_path if legacy_path.exists() else canonical_path
    assert config["station_private_key"] == str(expected_station_path)
    assert config["remote_public_key"] == str(
        NMEA_SPROXY_DIR / "aismixer_public.pem"
    )


def test_proxy_default_remote_public_key_prefers_canonical_path(monkeypatch, tmp_path):
    proxy = load_proxy_module()
    monkeypatch.setattr(
        proxy.os.path,
        "exists",
        lambda path: _normalize_path(path) == _normalize_path(
            REMOTE_CANONICAL_PUBLIC_KEY_PATH
        ),
    )

    config = proxy.load_config(str(tmp_path / "missing.yaml"))

    assert config["remote_public_key"] == REMOTE_CANONICAL_PUBLIC_KEY_PATH


def test_proxy_load_config_uses_remote_public_key_as_canonical(tmp_path):
    proxy = load_proxy_module()
    config_path = tmp_path / "config.yaml"
    config_path.write_text("remote_public_key: canonical.pem\n", encoding="utf-8")

    config = proxy.load_config(str(config_path))

    assert config["remote_public_key"] == str(tmp_path / "canonical.pem")


def test_proxy_load_config_supports_legacy_aismixer_public_key_as_fallback(tmp_path):
    proxy = load_proxy_module()
    config_path = tmp_path / "config.yaml"
    config_path.write_text("aismixer_public_key: legacy.pem\n", encoding="utf-8")

    config = proxy.load_config(str(config_path))

    assert config["remote_public_key"] == str(tmp_path / "legacy.pem")


def test_proxy_load_config_prefers_canonical_key_when_both_names_are_present(tmp_path):
    proxy = load_proxy_module()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "remote_public_key: canonical.pem\n"
        "aismixer_public_key: legacy.pem\n",
        encoding="utf-8",
    )

    config = proxy.load_config(str(config_path))

    assert config["remote_public_key"] == str(tmp_path / "canonical.pem")


def test_proxy_lifecycle_config_defaults():
    proxy = load_proxy_module()

    assert proxy.DEFAULT_CONFIG["keepalive_interval"] == 30
    assert proxy.DEFAULT_CONFIG["peer_timeout"] == 90
    assert proxy.DEFAULT_CONFIG["session_refresh_interval"] == 0


def test_proxy_explicit_system_config_keeps_absolute_key_paths(tmp_path):
    proxy = load_proxy_module()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"station_private_key: {STATION_CANONICAL_PRIVATE_KEY_PATH}\n"
        f"remote_public_key: {REMOTE_CANONICAL_PUBLIC_KEY_PATH}\n",
        encoding="utf-8",
    )

    config = proxy.load_config(str(config_path))

    assert config["station_private_key"] == STATION_CANONICAL_PRIVATE_KEY_PATH
    assert config["remote_public_key"] == REMOTE_CANONICAL_PUBLIC_KEY_PATH


def test_proxy_relative_key_paths_resolve_from_instance_config_directory(
    monkeypatch,
    tmp_path,
):
    proxy = load_proxy_module()
    instance_dir = tmp_path / "instances"
    instance_dir.mkdir()
    config_path = instance_dir / "boat.yaml"
    config_path.write_text(
        "station_private_key: ../keys/station_private.pem\n"
        "remote_public_key: local/aismixer_public.pem\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path.parent)

    config = proxy.load_config(str(config_path))

    assert config["station_private_key"] == os.path.normpath(
        str(instance_dir / "../keys/station_private.pem")
    )
    assert config["remote_public_key"] == os.path.normpath(
        str(instance_dir / "local/aismixer_public.pem")
    )


def test_proxy_config_resolution_prefers_cli_path():
    proxy = load_proxy_module()

    assert proxy.resolve_config_path(
        "cli.yaml",
        {proxy.CONFIG_ENV_VAR: "environment.yaml"},
    ) == "cli.yaml"


def test_proxy_config_resolution_uses_environment_before_discovery(monkeypatch):
    proxy = load_proxy_module()
    monkeypatch.setattr(proxy.os.path, "exists", lambda path: True)

    assert proxy.resolve_config_path(
        environ={proxy.CONFIG_ENV_VAR: "environment.yaml"},
    ) == "environment.yaml"


def test_proxy_config_resolution_prefers_system_config_over_local(monkeypatch):
    proxy = load_proxy_module()
    monkeypatch.setattr(
        proxy.os.path,
        "exists",
        lambda path: path in (proxy.SYSTEM_CONFIG_PATH, proxy.LOCAL_CONFIG_PATH),
    )

    assert proxy.resolve_config_path(environ={}) == proxy.SYSTEM_CONFIG_PATH


def test_proxy_config_resolution_uses_local_config_when_system_missing(monkeypatch):
    proxy = load_proxy_module()
    monkeypatch.setattr(
        proxy.os.path,
        "exists",
        lambda path: path == proxy.LOCAL_CONFIG_PATH,
    )

    assert proxy.resolve_config_path(environ={}) == proxy.LOCAL_CONFIG_PATH


def test_proxy_config_resolution_returns_none_for_built_in_defaults(monkeypatch):
    proxy = load_proxy_module()
    monkeypatch.setattr(proxy.os.path, "exists", lambda path: False)

    assert proxy.resolve_config_path(environ={}) is None


def test_proxy_parser_defaults_process_title():
    proxy = load_proxy_module()

    args = proxy.build_parser().parse_args([])

    assert args.process_title == "nmea_sproxy"


def test_proxy_parser_accepts_custom_process_title():
    proxy = load_proxy_module()

    args = proxy.build_parser().parse_args(
        ["--process-title", "nmea_sproxy@balchik_roof"]
    )

    assert args.process_title == "nmea_sproxy@balchik_roof"


def test_proxy_sets_process_title_when_optional_dependency_is_available(
    monkeypatch,
):
    proxy = load_proxy_module()
    titles = []
    fake_module = type(
        "FakeSetproctitle",
        (),
        {"setproctitle": staticmethod(titles.append)},
    )
    monkeypatch.setitem(sys.modules, "setproctitle", fake_module)

    proxy.set_process_title("nmea_sproxy@yacht")

    assert titles == ["nmea_sproxy@yacht"]


def test_proxy_ignores_missing_optional_setproctitle(monkeypatch):
    proxy = load_proxy_module()
    real_import = builtins.__import__

    def import_without_setproctitle(name, *args, **kwargs):
        if name == "setproctitle":
            raise ImportError
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_setproctitle)

    proxy.set_process_title("nmea_sproxy")


def test_proxy_main_applies_custom_process_title(monkeypatch, tmp_path):
    proxy = load_proxy_module()
    missing = tmp_path / "missing.yaml"
    titles = []
    monkeypatch.setattr(proxy, "set_process_title", titles.append)

    rc = proxy.main(
        [
            "--config",
            str(missing),
            "--process-title",
            "nmea_sproxy@boat",
        ]
    )

    assert rc == 1
    assert titles == ["nmea_sproxy@boat"]


def test_proxy_main_rejects_missing_explicit_config(tmp_path, capsys):
    proxy = load_proxy_module()
    missing = tmp_path / "missing.yaml"

    rc = proxy.main(["--config", str(missing)])

    captured = capsys.readouterr()
    assert rc == 1
    assert f"Config file not found: {missing}" in captured.err


def test_proxy_main_rejects_missing_environment_config(monkeypatch, tmp_path, capsys):
    proxy = load_proxy_module()
    missing = tmp_path / "missing.yaml"
    monkeypatch.setenv(proxy.CONFIG_ENV_VAR, str(missing))

    rc = proxy.main([])

    captured = capsys.readouterr()
    assert rc == 1
    assert f"Config file not found: {missing}" in captured.err


def test_current_secure_udp_key_filename_expectations():
    proxy = load_proxy_module()

    assert SERVER_CANONICAL_PRIVATE_KEY_PATH.endswith("aismixer_private.pem")
    assert SERVER_PRIVATE_KEY_FILENAME == "aismixer_private.key"
    assert SERVER_PUBLIC_KEY_FOR_PROXY_FILENAME == "aismixer_public.pem"
    assert STATION_CANONICAL_PRIVATE_KEY_PATH.endswith("station_private.pem")
    assert STATION_PRIVATE_KEY_FILENAME == "station_private.key"
    assert STATION_PUBLIC_KEY_FILENAME == "station_public.pem"
    assert REMOTE_CANONICAL_PUBLIC_KEY_PATH.endswith("aismixer_public.pem")
    assert proxy.CANONICAL_STATION_PRIVATE_KEY_PATH == STATION_CANONICAL_PRIVATE_KEY_PATH
    assert proxy.CANONICAL_REMOTE_PUBLIC_KEY_PATH == REMOTE_CANONICAL_PUBLIC_KEY_PATH
    assert proxy.DEFAULT_CONFIG["remote_public_key"] == REMOTE_CANONICAL_PUBLIC_KEY_PATH
    assert proxy.DEFAULT_CONFIG["station_private_key"] == STATION_CANONICAL_PRIVATE_KEY_PATH
