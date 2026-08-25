import asyncio
import base64
import builtins
import hashlib
import importlib.util
import io
import os
import socket
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import core.udpsec_crypto as udpsec_crypto
from core.ingress_frame import (
    IngressFrame,
    PayloadTextMode,
    decode_frame_slice,
)
from core.network_policy import NetworkPolicy
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

SERVER_PUBLIC_KEY_FOR_PROXY_FILENAME = "aismixer_public.pem"
STATION_CANONICAL_PRIVATE_KEY_PATH = "/etc/nmea_sproxy/keys/station_private.pem"
STATION_PRIVATE_KEY_FILENAME = "station_private.key"
STATION_PUBLIC_KEY_FILENAME = "station_public.pem"
REMOTE_CANONICAL_PUBLIC_KEY_PATH = "/etc/nmea_sproxy/keys/aismixer_public.pem"
_PREPARED_SERVER_PRIVATE_KEY = ec.derive_private_key(23, ec.SECP256R1())


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
):
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
        return real_open(path, mode, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os.path, "exists", lambda _path: False)
        patch.setattr("builtins.open", fake_open)
        spec = importlib.util.spec_from_file_location(
            "aismixer_secure_test_helpers", ROOT / "aismixer_secure.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if with_client_private_key:
            return module, client_private_key
        return module


def test_authorized_station_identity_keys_are_validated_once(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)

    public_key = secure.AUTHORIZED_KEYS["boat_001"]

    assert isinstance(public_key, ec.EllipticCurvePublicKey)
    assert isinstance(public_key.curve, ec.SECP256R1)


def test_secure_module_has_no_default_server_private_key_loader(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)

    assert not hasattr(secure, "_load_default_server_private_key")
    assert not hasattr(secure, "priv_key_path")
    assert not hasattr(secure, "server_priv")


def test_secure_loop_rejects_missing_identity_before_binding(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    fake_socket = _FakeSecureSocket()

    with pytest.raises(
        RuntimeError,
        match="identity was not prepared before activation",
    ):
        asyncio.run(
            secure._secure_server_loop(
                fake_socket,
                _FakeQueue(),
                "127.0.0.1",
                19999,
            )
        )

    assert fake_socket.bound is None
    assert fake_socket.blocking is None


@pytest.mark.parametrize(
    ("encoded", "message"),
    (
        pytest.param(None, "must be base64 text", id="non-text"),
        pytest.param("", "must not be empty", id="empty"),
        pytest.param("%%%%", "must be valid base64", id="alphabet"),
        pytest.param(
            "AB==",
            "must use canonical base64",
            id="noncanonical",
        ),
        pytest.param(
            base64.b64encode(b"\x02" * 32).decode(),
            "must be a 33-byte compressed",
            id="length",
        ),
        pytest.param(
            base64.b64encode(b"\x04" + b"\x01" * 32).decode(),
            "must use compressed",
            id="prefix",
        ),
        pytest.param(
            base64.b64encode(b"\x02" + b"\xff" * 32).decode(),
            "is not a valid P-256 point",
            id="point",
        ),
    ),
)
def test_authorized_station_identity_loader_is_strict(
    monkeypatch,
    encoded,
    message,
):
    secure = load_secure_module_with_fake_keys(monkeypatch)

    expected_error = TypeError if encoded is None else ValueError
    with pytest.raises(expected_error, match=message):
        secure._load_authorized_identity_public_key(encoded)


def test_obsolete_static_handshake_helpers_are_removed(monkeypatch):
    proxy = load_proxy_module()
    secure = load_secure_module_with_fake_keys(monkeypatch)
    obsolete_server_names = (
        "CONTEXT_STRING",
        "build_current_handshake_payload",
        "build_handshake_context_v1",
        "build_session_transcript_v1",
        "verify_signature",
        "derive_session_key",
        "server_pub_bytes",
    )
    obsolete_proxy_names = (
        "sign_message",
        "verify_signature",
        "derive_session_key",
        "compute_session_hash",
    )

    assert all(
        not hasattr(secure, name) for name in obsolete_server_names
    )
    assert all(
        not hasattr(proxy, name) for name in obsolete_proxy_names
    )


def test_obsolete_plaintext_keepalive_symbols_are_removed(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)

    assert not hasattr(secure, "KEEPALIVE_PREFIX")
    assert not hasattr(secure, "parse_keepalive_packet")
    assert not hasattr(secure, "parse_keepalive_station_id")
    assert not hasattr(secure.SecureState, "handle_keepalive")


def _reference_replay_key(domain_context, client_digest, client_signature):
    digest = hashlib.sha256()
    for value in (
        domain_context,
        b"HANDSHAKE-REPLAY",
        client_digest,
        client_signature,
    ):
        digest.update(len(value).to_bytes(4, "big"))
        digest.update(value)
    return digest.digest()


def test_handshake_replay_key_matches_complete_framed_identity(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    client_digest = bytes(range(32))
    client_signature = b"client signature"

    replay_key = secure.build_handshake_replay_key(
        client_digest,
        client_signature,
    )

    assert replay_key == _reference_replay_key(
        secure.DOMAIN_CONTEXT,
        client_digest,
        client_signature,
    )
    assert replay_key == secure.build_handshake_replay_key(
        client_digest,
        client_signature,
    )


@pytest.mark.parametrize(
    ("changed_digest", "changed_signature"),
    (
        pytest.param(b"\xff" + bytes(range(1, 32)), b"signature", id="digest"),
        pytest.param(bytes(range(32)), b"other signature", id="signature"),
    ),
)
def test_handshake_replay_key_changes_with_each_authenticated_input(
    monkeypatch,
    changed_digest,
    changed_signature,
):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    baseline = secure.build_handshake_replay_key(
        bytes(range(32)),
        b"signature",
    )

    assert baseline != secure.build_handshake_replay_key(
        changed_digest,
        changed_signature,
    )


def test_handshake_replay_key_binds_client_random_and_ephemeral_key(
    monkeypatch,
):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    base_arguments = {
        "station_id": "boat_001",
        "timestamp": 1234567890,
        "client_random": b"\x01" * 32,
        "client_ephemeral_public_key": b"\x02" + b"\x03" * 32,
    }
    baseline_digest = secure.build_client_auth_digest(**base_arguments)
    changed_random_digest = secure.build_client_auth_digest(
        **{**base_arguments, "client_random": b"\x02" * 32}
    )
    changed_ephemeral_digest = secure.build_client_auth_digest(
        **{
            **base_arguments,
            "client_ephemeral_public_key": b"\x03" + b"\x04" * 32,
        }
    )
    signature = b"signature"

    baseline = secure.build_handshake_replay_key(
        baseline_digest,
        signature,
    )

    assert baseline != secure.build_handshake_replay_key(
        changed_random_digest,
        signature,
    )
    assert baseline != secure.build_handshake_replay_key(
        changed_ephemeral_digest,
        signature,
    )


def test_handshake_replay_key_does_not_depend_on_source_address(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    addr_a = ("192.0.2.10", 50000)
    addr_b = ("192.0.2.11", 50001)
    client_digest = bytes(range(32))
    client_signature = b"signature"

    assert addr_a != addr_b
    assert secure.build_handshake_replay_key(
        client_digest,
        client_signature,
    ) == secure.build_handshake_replay_key(
        client_digest,
        client_signature,
    )


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
        self.close_count = 0

    def bind(self, addr):
        self.bound = addr

    def setblocking(self, blocking):
        self.blocking = blocking

    def sendto(self, data, addr):
        self.sent.append((data, addr))

    def close(self):
        self.close_count += 1


class _FakeSecureLoop:
    def __init__(self, packets):
        self.packets = list(packets)

    async def sock_recvfrom(self, sock, size):
        if self.packets:
            return self.packets.pop(0)
        raise asyncio.CancelledError()


class _FakeSecureSocketFactory:
    def __init__(self, fake_socket):
        self._fake_socket = fake_socket
        self.calls = []

    def __call__(self, listen_ip, *, reuse_address):
        self.calls.append((listen_ip, reuse_address))
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


def _signed_client_hello(
    secure,
    client_identity_private_key,
    station_id,
    timestamp,
    *,
    client_random=None,
    client_ephemeral_private_key=None,
):
    if client_random is None:
        client_random = b"\x11" * 32
    if client_ephemeral_private_key is None:
        client_ephemeral_private_key = ec.derive_private_key(
            2,
            ec.SECP256R1(),
        )
    client_ephemeral_public_bytes = (
        secure.serialize_ephemeral_public_key(
            client_ephemeral_private_key.public_key()
        )
    )
    client_auth_digest = secure.build_client_auth_digest(
        station_id=station_id,
        timestamp=timestamp,
        client_random=client_random,
        client_ephemeral_public_key=client_ephemeral_public_bytes,
    )
    client_signature = secure.sign_transcript_digest(
        client_identity_private_key,
        client_auth_digest,
    )
    client_hello = ClientHello(
        station_id=station_id,
        timestamp=timestamp,
        client_random=client_random,
        client_ephemeral_public_key=client_ephemeral_public_bytes,
        client_signature=client_signature,
    )
    return (
        build_client_hello_packet(client_hello),
        client_hello,
        client_ephemeral_private_key,
    )


def _signed_handshake_packet(
    secure,
    client_identity_private_key,
    station_id,
    timestamp,
):
    packet, _, _ = _signed_client_hello(
        secure,
        client_identity_private_key,
        station_id,
        timestamp,
    )
    return packet


def _encrypted_data_packet(
    secure,
    client_to_server_key,
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
    ciphertext = secure.AESGCM(client_to_server_key).encrypt(
        nonce,
        plaintext,
        b"NMEA",
    )
    return secure.DATA_PREFIX + nonce + ciphertext


def _encrypted_control_packet(
    secure,
    client_to_server_key,
    nonce,
    message,
):
    plaintext = secure.json.dumps(message).encode()
    ciphertext = secure.AESGCM(client_to_server_key).encrypt(
        nonce,
        plaintext,
        secure.DATA_AAD,
    )
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
    server_private_key=_PREPARED_SERVER_PRIVATE_KEY,
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
    socket_factory = _FakeSecureSocketFactory(fake_socket)
    monkeypatch.setattr(
        secure,
        "create_udp_listener_socket",
        socket_factory,
    )
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
                server_private_key=server_private_key,
            )
        )

    assert fake_socket.close_count == 1
    assert socket_factory.calls == [("127.0.0.1", False)]
    return fake_queue, fake_socket


def test_secure_server_closes_owned_socket_when_bind_fails(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    bind_failure = OSError("bind failed")

    class BindFailingSocket(_FakeSecureSocket):
        def bind(self, _addr):
            raise bind_failure

    fake_socket = BindFailingSocket()
    socket_factory = _FakeSecureSocketFactory(fake_socket)
    monkeypatch.setattr(
        secure,
        "create_udp_listener_socket",
        socket_factory,
    )

    with pytest.raises(OSError) as excinfo:
        asyncio.run(
            secure.secure_server(
                _FakeQueue(),
                "127.0.0.1",
                9999,
                server_private_key=_PREPARED_SERVER_PRIVATE_KEY,
            )
        )

    assert excinfo.value is bind_failure
    assert fake_socket.close_count == 1
    assert socket_factory.calls == [("127.0.0.1", False)]


def test_secure_server_closes_owned_socket_when_runtime_fails(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    runtime_failure = RuntimeError("receive loop failed")
    fake_socket = _FakeSecureSocket()

    async def fail_runtime(*_args, **_kwargs):
        raise runtime_failure

    socket_factory = _FakeSecureSocketFactory(fake_socket)
    monkeypatch.setattr(
        secure,
        "create_udp_listener_socket",
        socket_factory,
    )
    monkeypatch.setattr(secure, "_secure_server_loop", fail_runtime)

    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(
            secure.secure_server(
                _FakeQueue(),
                "127.0.0.1",
                9999,
                server_private_key=_PREPARED_SERVER_PRIVATE_KEY,
            )
        )

    assert excinfo.value is runtime_failure
    assert fake_socket.close_count == 1
    assert socket_factory.calls == [("127.0.0.1", False)]


def _install_test_session(
    secure,
    state,
    addr,
    client_to_server_key,
    server_to_client_key,
    now=1000.0,
    station_id="boat_001",
):
    client_to_server_aesgcm = secure.AESGCM(client_to_server_key)
    server_to_client_aesgcm = secure.AESGCM(server_to_client_key)
    session = state.install_session(
        addr,
        station_id,
        client_to_server_aesgcm,
        server_to_client_aesgcm,
        now,
    )
    return (
        session,
        client_to_server_aesgcm,
        server_to_client_aesgcm,
    )


def test_secure_server_rejects_verified_duplicate_handshake_replay(monkeypatch):
    secure, client_identity_private_key = load_secure_module_with_fake_keys(
        monkeypatch, with_client_private_key=True)
    timestamp = 1000
    station_id = "boat_001"
    addr = ("127.0.0.1", 50123)
    packet, client_hello, client_ephemeral_private_key = (
        _signed_client_hello(
            secure,
            client_identity_private_key,
            station_id,
            timestamp,
        )
    )
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
    server_hello = parse_server_hello_packet(fake_socket.sent[0][0])
    server_digest = secure.build_server_auth_digest(
        station_id=client_hello.station_id,
        timestamp=client_hello.timestamp,
        client_random=client_hello.client_random,
        client_ephemeral_public_key=(
            client_hello.client_ephemeral_public_key
        ),
        client_signature=client_hello.client_signature,
        server_random=server_hello.server_random,
        server_ephemeral_public_key=(
            server_hello.server_ephemeral_public_key
        ),
    )
    assert secure.verify_transcript_signature(
        _PREPARED_SERVER_PRIVATE_KEY.public_key(),
        server_hello.server_signature,
        server_digest,
    )
    server_ephemeral_public_key = secure.parse_ephemeral_public_key(
        server_hello.server_ephemeral_public_key
    )
    shared_secret = secure.derive_ephemeral_shared_secret(
        client_ephemeral_private_key,
        server_ephemeral_public_key,
    )
    transcript_hash = secure.build_session_transcript_hash(
        station_id=client_hello.station_id,
        timestamp=client_hello.timestamp,
        client_random=client_hello.client_random,
        client_ephemeral_public_key=(
            client_hello.client_ephemeral_public_key
        ),
        client_signature=client_hello.client_signature,
        server_random=server_hello.server_random,
        server_ephemeral_public_key=(
            server_hello.server_ephemeral_public_key
        ),
        server_signature=server_hello.server_signature,
    )
    client_key_material = secure.derive_session_key_material(
        shared_secret,
        transcript_hash,
    )
    pending = state._pending_sessions[addr]

    nonce = b"\x01" * 12
    plaintext = b"direction check"
    ciphertext = secure.AESGCM(
        client_key_material.client_to_server_key
    ).encrypt(nonce, plaintext, secure.DATA_AAD)
    assert pending.client_to_server_aesgcm.decrypt(
        nonce,
        ciphertext,
        secure.DATA_AAD,
    ) == plaintext
    assert (
        pending.client_to_server_aesgcm
        is not pending.server_to_client_aesgcm
    )
    assert (
        client_key_material.client_to_server_key
        != client_key_material.server_to_client_key
    )
    assert fake_socket.sent[0][1] == addr
    assert stats.handshake_replay_accepted == 1
    assert stats.handshake_replay_rejected == 1
    assert stats.sessions_created == 0
    assert stats.pending_sessions_created == 1
    assert stats.current_handshake_replays == 1
    assert stats.current_sessions == 0
    assert stats.current_pending_sessions == 1
    assert wall_clock.calls == 2
    assert monotonic_clock.calls == 2


def test_secure_server_rejects_exact_replay_from_different_address(
    monkeypatch,
):
    secure, client_identity_private_key = load_secure_module_with_fake_keys(
        monkeypatch,
        with_client_private_key=True,
    )
    packet = _signed_handshake_packet(
        secure,
        client_identity_private_key,
        "boat_001",
        1000,
    )
    first_addr = ("127.0.0.1", 50123)
    second_addr = ("127.0.0.2", 50124)
    state = secure.SecureState()

    _, fake_socket = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(packet, first_addr), (packet, second_addr)],
        state=state,
        wall_clock=_FakeClock(1000.0),
        monotonic_clock=_FakeClock(10.0),
    )

    assert len(fake_socket.sent) == 1
    assert fake_socket.sent[0][1] == first_addr
    assert tuple(state._sessions) == ()
    assert tuple(state._pending_sessions) == (first_addr,)
    assert state.stats().handshake_replay_accepted == 1
    assert state.stats().handshake_replay_rejected == 1
    assert state.stats().pending_sessions_created == 1


def test_invalid_hellos_do_not_consume_replay_or_generate_server_ephemeral(
    monkeypatch,
):
    secure, client_identity_private_key = load_secure_module_with_fake_keys(
        monkeypatch,
        with_client_private_key=True,
    )
    valid_packet, valid_hello, _ = _signed_client_hello(
        secure,
        client_identity_private_key,
        "boat_001",
        1000,
    )
    unknown_packet = _signed_handshake_packet(
        secure,
        client_identity_private_key,
        "unknown_station",
        1000,
    )
    wrong_signature_packet = build_client_hello_packet(
        ClientHello(
            station_id=valid_hello.station_id,
            timestamp=valid_hello.timestamp,
            client_random=valid_hello.client_random,
            client_ephemeral_public_key=(
                valid_hello.client_ephemeral_public_key
            ),
            client_signature=valid_hello.client_signature + b"\x00",
        )
    )
    malformed_public_bytes = b"\x02" + b"\xff" * 32
    malformed_digest = secure.build_client_auth_digest(
        station_id="boat_001",
        timestamp=1000,
        client_random=b"\x22" * 32,
        client_ephemeral_public_key=malformed_public_bytes,
    )
    malformed_signature = secure.sign_transcript_digest(
        client_identity_private_key,
        malformed_digest,
    )
    malformed_point_packet = build_client_hello_packet(
        ClientHello(
            station_id="boat_001",
            timestamp=1000,
            client_random=b"\x22" * 32,
            client_ephemeral_public_key=malformed_public_bytes,
            client_signature=malformed_signature,
        )
    )
    old_packet = (
        b"NMEA-H|boat_001|1000|"
        + base64.b64encode(valid_hello.client_signature)
    )
    cases = (
        (old_packet, 1000.0),
        (unknown_packet, 1000.0),
        (valid_packet, 1030.001),
        (wrong_signature_packet, 1000.0),
        (malformed_point_packet, 1000.0),
    )
    generation_calls = []

    def fail_ephemeral_generation():
        generation_calls.append(True)
        raise AssertionError("server ephemeral generation must not run")

    monkeypatch.setattr(
        secure,
        "generate_ephemeral_private_key",
        fail_ephemeral_generation,
    )

    for packet, wall_time in cases:
        state = secure.SecureState()
        _, fake_socket = _run_secure_server_with_packets(
            monkeypatch,
            secure,
            [(packet, ("127.0.0.1", 50123))],
            state=state,
            wall_clock=_FakeClock(wall_time),
            monotonic_clock=_FakeClock(10.0),
        )

        assert fake_socket.sent == []
        assert state.stats().current_handshake_replays == 0
        assert state.stats().current_sessions == 0
        assert state.stats().current_pending_sessions == 0

    assert generation_calls == []


@pytest.mark.parametrize(
    "field_name",
    (
        "station_id",
        "timestamp",
        "client_random",
        "client_ephemeral_public_key",
    ),
)
def test_server_rejects_clienthello_field_changed_after_signing(
    monkeypatch,
    field_name,
):
    secure, client_identity_private_key = load_secure_module_with_fake_keys(
        monkeypatch,
        with_client_private_key=True,
    )
    _, client_hello, _ = _signed_client_hello(
        secure,
        client_identity_private_key,
        "boat_001",
        1000,
    )
    replacements = {
        "station_id": "boat_002",
        "timestamp": 1001,
        "client_random": b"\x23" * 32,
        "client_ephemeral_public_key": (
            secure.serialize_ephemeral_public_key(
                ec.derive_private_key(
                    3,
                    ec.SECP256R1(),
                ).public_key()
            )
        ),
    }
    arguments = {
        "station_id": client_hello.station_id,
        "timestamp": client_hello.timestamp,
        "client_random": client_hello.client_random,
        "client_ephemeral_public_key": (
            client_hello.client_ephemeral_public_key
        ),
        "client_signature": client_hello.client_signature,
    }
    arguments[field_name] = replacements[field_name]
    changed_packet = build_client_hello_packet(ClientHello(**arguments))
    secure.AUTHORIZED_KEYS["boat_002"] = (
        client_identity_private_key.public_key()
    )
    state = secure.SecureState()

    _, fake_socket = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(changed_packet, ("127.0.0.1", 50123))],
        state=state,
        wall_clock=_FakeClock(1000.0),
        monotonic_clock=_FakeClock(10.0),
    )

    assert fake_socket.sent == []
    assert state.stats().current_handshake_replays == 0
    assert state.stats().current_sessions == 0
    assert state.stats().current_pending_sessions == 0


def test_server_ecdhe_uses_only_one_validated_ephemeral_private_key(
    monkeypatch,
):
    secure, client_identity_private_key = load_secure_module_with_fake_keys(
        monkeypatch,
        with_client_private_key=True,
    )
    packet = _signed_handshake_packet(
        secure,
        client_identity_private_key,
        "boat_001",
        1000,
    )
    generated_server_private_key = ec.derive_private_key(
        9,
        ec.SECP256R1(),
    )
    parse_calls = []
    derive_calls = []
    original_parse = secure.parse_ephemeral_public_key
    original_derive = secure.derive_ephemeral_shared_secret

    def record_parse(encoded):
        parse_calls.append(encoded)
        return original_parse(encoded)

    def record_derive(private_key, public_key):
        derive_calls.append((private_key, public_key))
        return original_derive(private_key, public_key)

    monkeypatch.setattr(
        secure,
        "generate_ephemeral_private_key",
        lambda: generated_server_private_key,
    )
    monkeypatch.setattr(
        secure,
        "parse_ephemeral_public_key",
        record_parse,
    )
    monkeypatch.setattr(
        secure,
        "derive_ephemeral_shared_secret",
        record_derive,
    )

    _, fake_socket = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(packet, ("127.0.0.1", 50123))],
        wall_clock=_FakeClock(1000.0),
        monotonic_clock=_FakeClock(10.0),
    )

    assert len(fake_socket.sent) == 1
    assert len(parse_calls) == 1
    assert len(derive_calls) == 1
    assert derive_calls[0][0] is generated_server_private_key
    assert derive_calls[0][0] is not _PREPARED_SERVER_PRIVATE_KEY


def test_fresh_same_timestamp_hellos_get_fresh_server_state(monkeypatch):
    secure, client_identity_private_key = load_secure_module_with_fake_keys(
        monkeypatch,
        with_client_private_key=True,
    )
    first_packet, _, _ = _signed_client_hello(
        secure,
        client_identity_private_key,
        "boat_001",
        1000,
        client_random=b"\x31" * 32,
        client_ephemeral_private_key=ec.derive_private_key(
            2,
            ec.SECP256R1(),
        ),
    )
    second_packet, _, _ = _signed_client_hello(
        secure,
        client_identity_private_key,
        "boat_001",
        1000,
        client_random=b"\x32" * 32,
        client_ephemeral_private_key=ec.derive_private_key(
            3,
            ec.SECP256R1(),
        ),
    )
    random_values = iter((b"\x41" * 32, b"\x42" * 32))
    ephemeral_keys = iter(
        (
            ec.derive_private_key(4, ec.SECP256R1()),
            ec.derive_private_key(5, ec.SECP256R1()),
        )
    )
    recorded_keys = []

    class _RecordingAESGCM:
        def __init__(self, key):
            self.key = key
            recorded_keys.append(key)

    monkeypatch.setattr(secure.os, "urandom", lambda length: next(random_values))
    monkeypatch.setattr(
        secure,
        "generate_ephemeral_private_key",
        lambda: next(ephemeral_keys),
    )
    monkeypatch.setattr(secure, "AESGCM", _RecordingAESGCM)
    state = secure.SecureState()
    addr = ("127.0.0.1", 50123)

    _, fake_socket = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(first_packet, addr), (second_packet, addr)],
        state=state,
        wall_clock=_FakeClock(1000.0),
        monotonic_clock=_FakeClock(10.0),
    )

    first_server_hello = parse_server_hello_packet(
        fake_socket.sent[0][0]
    )
    second_server_hello = parse_server_hello_packet(
        fake_socket.sent[1][0]
    )
    assert first_server_hello.server_random != (
        second_server_hello.server_random
    )
    assert first_server_hello.server_ephemeral_public_key != (
        second_server_hello.server_ephemeral_public_key
    )
    assert len(recorded_keys) == 4
    assert recorded_keys[0] != recorded_keys[1]
    assert recorded_keys[0] != recorded_keys[2]
    assert recorded_keys[1] != recorded_keys[3]
    assert state.stats().handshake_replay_accepted == 2
    assert state.stats().handshake_replay_rejected == 0
    assert tuple(state._sessions) == ()
    assert tuple(state._pending_sessions) == (addr,)
    assert state.stats().pending_sessions_created == 2
    assert state.stats().pending_sessions_replaced == 1
    assert state.stats().current_pending_sessions == 1


def test_secure_server_sends_no_session_for_data_without_session(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    addr = ("127.0.0.1", 50123)
    packet = secure.DATA_PREFIX + (b"\x00" * 28)

    fake_queue, fake_socket = _run_secure_server_with_packets(
        monkeypatch, secure, [(packet, addr)]
    )

    assert fake_queue.items == []
    assert fake_socket.sent == [(secure.NOSESSION_PREFIX, addr)]


@pytest.mark.parametrize(
    "packet",
    (
        b"KEEPALIVE",
        b"KEEPALIVE|boat_001|1000",
    ),
)
def test_plaintext_keepalive_is_silently_ignored_without_touch_or_promotion(
    monkeypatch,
    packet,
):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    addr = ("127.0.0.1", 50123)
    active = state.install_session(
        addr,
        "boat_001",
        object(),
        object(),
        now=1000.0,
    )
    pending = state.install_pending_session(
        addr,
        "boat_001",
        object(),
        object(),
        now=1005.0,
    )
    wall_clock = _FakeClock(5000.0)
    monotonic_clock = _FakeClock(1010.0)

    fake_queue, fake_socket = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(packet, addr)],
        state=state,
        wall_clock=wall_clock,
        monotonic_clock=monotonic_clock,
    )

    assert fake_queue.items == []
    assert fake_socket.sent == []
    assert state._sessions[addr] is active
    assert state._pending_sessions[addr] is pending
    assert active.last_seen == 1000.0
    assert state.stats().sessions_touched == 0
    assert state.stats().pending_sessions_promoted == 0
    assert wall_clock.calls == 0
    assert monotonic_clock.calls == 1


def test_secure_server_replies_with_encrypted_pong_for_valid_ping(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    client_to_server_key = b"\x01" * 32
    server_to_client_key = b"\x03" * 32
    nonce = b"\x02" * 12
    addr = ("127.0.0.1", 50123)
    state = secure.SecureState()
    session, _, _ = _install_test_session(
        secure,
        state,
        addr,
        client_to_server_key,
        server_to_client_key,
    )
    packet = _encrypted_control_packet(
        secure,
        client_to_server_key,
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
        secure.AESGCM(server_to_client_key).decrypt(
            response_nonce, ciphertext, secure.DATA_AAD
        ).decode()
    )
    with pytest.raises(InvalidTag):
        secure.AESGCM(client_to_server_key).decrypt(
            response_nonce,
            ciphertext,
            secure.DATA_AAD,
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
    client_to_server_key = b"\x01" * 32
    server_to_client_key = b"\x03" * 32
    nonce = b"\x02" * 12
    addr = ("127.0.0.1", 50123)
    state = secure.SecureState()
    session, _, _ = _install_test_session(
        secure,
        state,
        addr,
        client_to_server_key,
        server_to_client_key,
    )
    packet = _encrypted_data_packet(
        secure,
        client_to_server_key,
        nonce,
    )

    fake_queue, _ = _run_secure_server_with_packets(
        monkeypatch, secure, [(packet, addr)], state=state)

    stats = state.stats()
    assert len(fake_queue.items) == 1
    frame = fake_queue.items[0]
    assert isinstance(frame, IngressFrame)
    assert frame.kind == "sec"
    assert frame.source_id == "udpsec:boat_001"
    assert frame.alias_for_s == "boat_001"
    assert frame.remote_ip == "127.0.0.1"
    assert frame.assembler_key == "127.0.0.1:50123"
    assert frame.payload == b"!AIVDM,1,1,,A,payload,0*00"
    assert frame.text_mode is PayloadTextMode.UTF8_SURROGATEPASS
    assert (
        decode_frame_slice(frame, 0, len(frame.payload))
        == "!AIVDM,1,1,,A,payload,0*00"
    )
    assert session.last_seen == 1010.0
    assert len(session.seen_data_nonces) == 1
    assert stats.sessions_touched == 1
    assert stats.data_nonces_accepted == 1


def test_secure_server_allowed_peer_preserves_data_behavior(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    client_to_server_key = b"\x01" * 32
    server_to_client_key = b"\x03" * 32
    nonce = b"\x02" * 12
    addr = ("127.0.0.1", 50123)
    policy = NetworkPolicy.from_entries(
        ["127.0.0.1"],
        context="sec_inputs[0].allow_from",
    )
    state = secure.SecureState()
    _install_test_session(
        secure,
        state,
        addr,
        client_to_server_key,
        server_to_client_key,
    )
    packet = _encrypted_data_packet(
        secure,
        client_to_server_key,
        nonce,
    )

    fake_queue, _ = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(packet, addr)],
        state=state,
        ingress_policy=policy,
    )

    assert len(fake_queue.items) == 1
    assert fake_queue.items[0].source_id == "udpsec:boat_001"
    assert fake_queue.items[0].payload == b"!AIVDM,1,1,,A,payload,0*00"
    assert (
        fake_queue.items[0].text_mode
        is PayloadTextMode.UTF8_SURROGATEPASS
    )


def test_secure_server_denied_data_peer_gets_no_no_session_response(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(session_ttl=1.0)
    retained_addr = ("198.51.100.20", 50000)
    state.install_session(
        retained_addr,
        "retained",
        object(),
        object(),
        now=0.0,
    )
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

    monkeypatch.setattr(
        secure,
        "verify_transcript_signature",
        fail_verify,
    )
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
    client_to_server_key = b"\x01" * 32
    server_to_client_key = b"\x03" * 32
    nonce = b"\x02" * 12
    addr = ("127.0.0.1", 50123)
    state = secure.SecureState()
    _install_test_session(
        secure,
        state,
        addr,
        client_to_server_key,
        server_to_client_key,
    )
    packet = _encrypted_data_packet(
        secure,
        client_to_server_key,
        nonce,
    )

    fake_queue, _ = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(packet, addr)],
        state=state,
        sec_input_id="configured_listener_alias",
    )

    assert len(fake_queue.items) == 1
    frame = fake_queue.items[0]
    assert isinstance(frame, IngressFrame)
    assert frame.source_id == "udpsec:boat_001"
    assert frame.alias_for_s == "configured_listener_alias"


def test_secure_server_preserves_unstripped_surrogate_payload(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    client_to_server_key = b"\x01" * 32
    server_to_client_key = b"\x03" * 32
    nonce = b"\x02" * 12
    addr = ("127.0.0.1", 50123)
    payload = " before\ud800after "
    state = secure.SecureState()
    session, _, _ = _install_test_session(
        secure,
        state,
        addr,
        client_to_server_key,
        server_to_client_key,
    )
    packet = _encrypted_data_packet(
        secure,
        client_to_server_key,
        nonce,
        payload=payload,
    )
    surrogate_log_attempts = []

    def strict_console_print(*values, **_kwargs):
        rendered = " ".join(str(value) for value in values)
        if "\ud800" in rendered:
            surrogate_log_attempts.append(rendered)
            raise UnicodeEncodeError(
                "charmap",
                "\ud800",
                0,
                1,
                "character maps to undefined",
            )

    monkeypatch.setattr(builtins, "print", strict_console_print)

    fake_queue, _ = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(packet, addr)],
        state=state,
    )

    assert len(fake_queue.items) == 1
    frame = fake_queue.items[0]
    assert isinstance(frame, IngressFrame)
    assert frame.payload == str.encode(
        payload,
        "utf-8",
        errors="surrogatepass",
    )
    assert frame.text_mode is PayloadTextMode.UTF8_SURROGATEPASS
    assert decode_frame_slice(frame, 0, len(frame.payload)) == payload
    assert len(surrogate_log_attempts) == 1
    assert session.last_seen == 1010.0
    assert state.stats().data_nonces_accepted == 1
    assert state.stats().sessions_touched == 1


@pytest.mark.parametrize(
    "payload",
    [None, 123, False, [], {}],
    ids=["null", "number", "boolean", "list", "object"],
)
def test_secure_server_non_string_payload_retains_state_and_later_valid_works(
    monkeypatch,
    payload,
):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    client_to_server_key = b"\x01" * 32
    server_to_client_key = b"\x03" * 32
    first_nonce = b"\x02" * 12
    second_nonce = b"\x03" * 12
    addr = ("127.0.0.1", 50123)
    state = secure.SecureState()
    session, _, _ = _install_test_session(
        secure,
        state,
        addr,
        client_to_server_key,
        server_to_client_key,
    )
    first_packet = _encrypted_data_packet(
        secure,
        client_to_server_key,
        first_nonce,
        payload=payload,
    )
    second_packet = _encrypted_data_packet(
        secure,
        client_to_server_key,
        second_nonce,
        payload="later valid",
    )
    construction_observations = []
    original_constructor = secure.frame_from_text_payload

    def record_constructor(**kwargs):
        stats = state.stats()
        construction_observations.append(
            (
                kwargs["payload"],
                stats.data_nonces_accepted,
                stats.sessions_touched,
                len(session.seen_data_nonces),
            )
        )
        return original_constructor(**kwargs)

    monkeypatch.setattr(
        secure,
        "frame_from_text_payload",
        record_constructor,
    )

    fake_queue, _ = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(first_packet, addr), (second_packet, addr)],
        state=state,
    )

    assert construction_observations == [
        (payload, 1, 1, 1),
        ("later valid", 2, 2, 2),
    ]
    assert len(fake_queue.items) == 1
    frame = fake_queue.items[0]
    assert isinstance(frame, IngressFrame)
    assert decode_frame_slice(frame, 0, len(frame.payload)) == "later valid"
    assert frame.text_mode is PayloadTextMode.UTF8_SURROGATEPASS
    assert len(session.seen_data_nonces) == 2
    assert session.last_seen == 1010.0
    stats = state.stats()
    assert stats.data_nonces_accepted == 2
    assert stats.sessions_touched == 2


def test_secure_server_rejects_duplicate_data_nonce_after_first_valid_packet(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    client_to_server_key = b"\x01" * 32
    server_to_client_key = b"\x03" * 32
    nonce = b"\x02" * 12
    addr = ("127.0.0.1", 50123)
    state = secure.SecureState()
    session, _, _ = _install_test_session(
        secure,
        state,
        addr,
        client_to_server_key,
        server_to_client_key,
    )
    packet = _encrypted_data_packet(
        secure,
        client_to_server_key,
        nonce,
    )

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
    client_to_server_key = b"\x01" * 32
    server_to_client_key = b"\x03" * 32
    nonce = b"\x02" * 12
    addr = ("127.0.0.1", 50123)
    state = secure.SecureState()
    session, _, _ = _install_test_session(
        secure,
        state,
        addr,
        client_to_server_key,
        server_to_client_key,
    )
    packet = secure.DATA_PREFIX + nonce + (b"\x00" * 16)

    fake_queue, _ = _run_secure_server_with_packets(
        monkeypatch, secure, [(packet, addr)], state=state)

    stats = state.stats()
    assert fake_queue.items == []
    assert session.last_seen == 1000.0
    assert len(session.seen_data_nonces) == 0
    assert stats.sessions_touched == 0
    assert stats.data_nonces_accepted == 0


def test_secure_server_rejects_data_encrypted_with_server_to_client_key(
    monkeypatch,
):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    client_to_server_key = b"\x01" * 32
    server_to_client_key = b"\x03" * 32
    nonce = b"\x02" * 12
    addr = ("127.0.0.1", 50123)
    state = secure.SecureState()
    session, _, _ = _install_test_session(
        secure,
        state,
        addr,
        client_to_server_key,
        server_to_client_key,
    )
    packet = _encrypted_data_packet(
        secure,
        server_to_client_key,
        nonce,
    )

    fake_queue, fake_socket = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(packet, addr)],
        state=state,
    )

    assert fake_queue.items == []
    assert fake_socket.sent == []
    assert session.last_seen == 1000.0
    assert len(session.seen_data_nonces) == 0
    assert state.stats().sessions_touched == 0
    assert state.stats().data_nonces_accepted == 0


def test_secure_server_malformed_framing_does_not_record_nonce_or_touch(
    monkeypatch,
):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    client_to_server_key = b"\x01" * 32
    server_to_client_key = b"\x03" * 32
    addr = ("127.0.0.1", 50123)
    state = secure.SecureState()
    session, _, _ = _install_test_session(
        secure,
        state,
        addr,
        client_to_server_key,
        server_to_client_key,
    )
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
    client_to_server_key = b"\x01" * 32
    server_to_client_key = b"\x03" * 32
    nonce = b"\x02" * 12
    addr = ("127.0.0.1", 50123)
    state = secure.SecureState()
    session, _, _ = _install_test_session(
        secure,
        state,
        addr,
        client_to_server_key,
        server_to_client_key,
    )
    packet = _encrypted_data_packet(
        secure,
        client_to_server_key,
        nonce,
        source_id="other_station",
    )

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
    assert state.stats().sessions_created == 0
    assert state.stats().pending_sessions_created == int(accepted)
    assert state.stats().current_sessions == 0
    assert state.stats().current_pending_sessions == int(accepted)
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
    assert stats.sessions_created == 0
    assert stats.sessions_replaced == 0
    assert stats.pending_sessions_created == 2
    assert stats.pending_sessions_replaced == 0
    assert stats.pending_sessions_expired == 1
    assert stats.current_sessions == 0
    assert stats.current_pending_sessions == 1


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

    sign_calls = []

    def fail_server_signing(private_key, digest):
        sign_calls.append((private_key, digest))
        raise RuntimeError("server signing failed")

    monkeypatch.setattr(
        secure,
        "sign_transcript_digest",
        fail_server_signing,
    )

    _, fake_socket = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(packet, addr), (packet, addr)],
        state=state,
        wall_clock=_FakeClock(1000.0),
        monotonic_clock=_FakeClock(10.0),
    )

    assert fake_socket.sent == []
    assert len(sign_calls) == 1
    assert sign_calls[0][0] is _PREPARED_SERVER_PRIVATE_KEY
    stats = state.stats()
    assert stats.handshake_replay_accepted == 1
    assert stats.handshake_replay_rejected == 1
    assert stats.current_handshake_replays == 1
    assert stats.sessions_created == 0
    assert stats.pending_sessions_created == 0
    assert stats.current_pending_sessions == 0


def test_secure_server_signs_with_prepared_activation_identity(monkeypatch):
    secure, client_private_key = load_secure_module_with_fake_keys(
        monkeypatch,
        with_client_private_key=True,
    )
    packet = _signed_handshake_packet(
        secure,
        client_private_key,
        "boat_001",
        1000,
    )
    prepared_private_key = ec.derive_private_key(17, ec.SECP256R1())
    sign_calls = []

    def fail_after_recording(private_key, _digest):
        sign_calls.append(private_key)
        raise RuntimeError("stop after identity selection")

    monkeypatch.setattr(
        secure,
        "sign_transcript_digest",
        fail_after_recording,
    )

    _, fake_socket = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(packet, ("127.0.0.1", 50123))],
        wall_clock=_FakeClock(1000.0),
        monotonic_clock=_FakeClock(10.0),
        server_private_key=prepared_private_key,
    )

    assert fake_socket.sent == []
    assert sign_calls == [prepared_private_key]
    assert prepared_private_key is not _PREPARED_SERVER_PRIVATE_KEY


def test_authenticated_hello_preserves_active_and_installs_pending_candidate(
    monkeypatch,
):
    secure, client_private_key = load_secure_module_with_fake_keys(
        monkeypatch, with_client_private_key=True
    )
    state = secure.SecureState(max_sessions=2)
    addr = ("127.0.0.1", 50123)
    other_addr = ("127.0.0.1", 50124)
    old = state.install_session(
        addr,
        "boat_001",
        object(),
        object(),
        now=0.0,
    )
    other = state.install_session(
        other_addr,
        "other",
        object(),
        object(),
        now=1.0,
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

    pending = state._pending_sessions[addr]
    response, response_addr = fake_socket.sent[0]
    assert response_addr == addr
    assert response.startswith(b"OK|")
    assert state._sessions[addr] is old
    assert pending is not old
    assert (
        pending.client_to_server_aesgcm
        is not old.client_to_server_aesgcm
    )
    assert (
        pending.server_to_client_aesgcm
        is not old.server_to_client_aesgcm
    )
    assert state._sessions[other_addr] is other
    assert tuple(state._sessions) == (addr, other_addr)
    assert tuple(state._pending_sessions) == (addr,)
    assert state.data_nonce_seen(old, nonce, now=2.0)
    assert not state.pending_data_nonce_seen(pending, nonce, now=2.0)

    stats = state.stats()
    assert stats.sessions_created == 2
    assert stats.sessions_replaced == 0
    assert stats.sessions_capacity_evicted == 0
    assert stats.pending_sessions_created == 1
    assert stats.current_sessions == 2
    assert stats.current_pending_sessions == 1
    assert stats.data_nonces_session_discarded == 0


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
    client_to_server_key = b"\x01" * 32
    server_to_client_key = b"\x03" * 32
    nonce = b"\x02" * 12
    addr = ("127.0.0.1", 50123)
    state = secure.SecureState()
    _install_test_session(
        secure,
        state,
        addr,
        client_to_server_key,
        server_to_client_key,
        now=1000.0,
    )
    packet = _encrypted_data_packet(
        secure,
        client_to_server_key,
        nonce,
    )

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
    client_to_server_key = b"\x01" * 32
    server_to_client_key = b"\x03" * 32
    nonce = b"\x02" * 12
    addr = ("127.0.0.1", 50123)
    state = secure.SecureState(session_ttl=1000.0, data_nonce_ttl=10.0)
    _install_test_session(
        secure,
        state,
        addr,
        client_to_server_key,
        server_to_client_key,
        now=0.0,
    )
    packet = _encrypted_data_packet(
        secure,
        client_to_server_key,
        nonce,
    )

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
    client_to_server_key = b"\x01" * 32
    nonce = b"\x02" * 12
    addr = ("127.0.0.1", 50123)
    real_client_to_server_aesgcm = secure.AESGCM(client_to_server_key)

    class CountingClientToServerAESGCM:
        def __init__(self):
            self.decrypt_calls = 0

        def decrypt(self, *args):
            self.decrypt_calls += 1
            return real_client_to_server_aesgcm.decrypt(*args)

    client_to_server_aesgcm = CountingClientToServerAESGCM()
    server_to_client_aesgcm = object()
    state = secure.SecureState()
    state.install_session(
        addr,
        "boat_001",
        client_to_server_aesgcm,
        server_to_client_aesgcm,
        now=1000.0,
    )
    packet = _encrypted_data_packet(
        secure,
        client_to_server_key,
        nonce,
    )

    fake_queue, _ = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(packet, addr), (packet, addr)],
        state=state,
    )

    assert len(fake_queue.items) == 1
    assert client_to_server_aesgcm.decrypt_calls == 1
    assert state.stats().data_nonce_replays == 1
    assert state.stats().sessions_touched == 1


def test_secure_server_invalid_json_does_not_record_nonce_or_touch(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    client_to_server_key = b"\x01" * 32
    server_to_client_key = b"\x03" * 32
    nonce = b"\x02" * 12
    addr = ("127.0.0.1", 50123)
    state = secure.SecureState()
    session, _, _ = _install_test_session(
        secure,
        state,
        addr,
        client_to_server_key,
        server_to_client_key,
    )
    ciphertext = secure.AESGCM(client_to_server_key).encrypt(
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
    client_to_server_key = b"\x01" * 32
    server_to_client_key = b"\x03" * 32
    nonce = b"\x02" * 12
    addr = ("127.0.0.1", 50123)
    state = secure.SecureState()
    session, _, _ = _install_test_session(
        secure,
        state,
        addr,
        client_to_server_key,
        server_to_client_key,
    )
    packet = _encrypted_control_packet(
        secure,
        client_to_server_key,
        nonce,
        message,
    )

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
    state.install_session(
        silent_addr,
        "silent",
        object(),
        object(),
        now=0.0,
    )
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
    state.install_session(
        silent_addr,
        "silent",
        object(),
        object(),
        now=0.0,
    )
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

    assert tuple(state._sessions) == ()
    assert tuple(state._pending_sessions) == (handshake_addr,)
    assert state.stats().sessions_expired == 1
    assert state.stats().pending_sessions_created == 1
    assert len(fake_socket.sent) == 1
    assert fake_socket.sent[0][0].startswith(b"OK|")


def test_unknown_allowed_packet_proactively_cleans_without_wall_clock(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(session_ttl=10.0)
    silent_addr = ("127.0.0.1", 50122)
    state.install_session(
        silent_addr,
        "silent",
        object(),
        object(),
        now=0.0,
    )
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


def test_exactly_expired_address_receives_no_session_for_data(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(session_ttl=10.0)
    addr = ("127.0.0.1", 50123)
    state.install_session(
        addr,
        "boat_001",
        object(),
        object(),
        now=0.0,
    )

    _, fake_socket = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(secure.DATA_PREFIX + (b"\x00" * 28), addr)],
        state=state,
        monotonic_clock=_FakeClock(10.0),
    )

    assert fake_socket.sent == [(secure.NOSESSION_PREFIX, addr)]
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
            ("192.0.2.10", 50000),
            "boat_001",
            object(),
            object(),
            now=0.0,
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


def _proxy_session_key_material(
    proxy,
    *,
    client_to_server_key=b"\x01" * 32,
    server_to_client_key=b"\x02" * 32,
):
    return proxy.SessionKeyMaterial(
        client_to_server_key=client_to_server_key,
        server_to_client_key=server_to_client_key,
    )


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
    client_to_server_key = b"\x01" * 32
    key_material = _proxy_session_key_material(
        proxy,
        client_to_server_key=client_to_server_key,
    )
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
        key_material,
        remote_addr,
        policy,
    )

    assert reason == proxy.SESSION_END_SOCKET_ERROR
    assert len(out_sock.sent) == 1
    packet, destination = out_sock.sent[0]
    assert destination == remote_addr
    assert proxy.decrypt_secure_json_message(
        packet,
        client_to_server_key,
    ) == {
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
        _proxy_session_key_material(proxy),
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
    client_to_server_key = b"\x01" * 32
    server_to_client_key = b"\x02" * 32
    remote_addr = ("192.0.2.10", 17777)
    packet = proxy.encrypt_secure_json_message(
        {
            "type": "pong",
            "seq": 123,
            "timestamp": 1000,
            "source_id": "boat_001",
        },
        server_to_client_key,
    )

    assert proxy.handle_server_packet(
        packet,
        remote_addr,
        remote_addr,
        server_to_client_key,
        "boat_001",
        123,
    ) == proxy.SERVER_PACKET_AUTHENTICATED
    assert proxy.handle_server_packet(
        packet,
        remote_addr,
        remote_addr,
        client_to_server_key,
        "boat_001",
        123,
    ) == proxy.SERVER_PACKET_IGNORED
    assert proxy.handle_server_packet(
        packet,
        ("192.0.2.10", 17778),
        remote_addr,
        server_to_client_key,
        "boat_001",
        123,
    ) == proxy.SERVER_PACKET_IGNORED
    assert proxy.handle_server_packet(
        b"PONG|123",
        remote_addr,
        remote_addr,
        server_to_client_key,
        "boat_001",
        123,
    ) == proxy.SERVER_PACKET_IGNORED
    assert proxy.handle_server_packet(
        packet,
        remote_addr,
        remote_addr,
        server_to_client_key,
        "boat_001",
        124,
    ) == proxy.SERVER_PACKET_IGNORED


class _FakeHandshakeSocket:
    def __init__(self, responses):
        self.responses = list(responses)
        self.sent = []
        self.timeout = 5.0
        self.timeouts = []

    def sendto(self, data, addr):
        self.sent.append((data, addr))

    def recvfrom(self, size):
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if callable(response):
            return response(self.sent[-1][0])
        return response

    def gettimeout(self):
        return self.timeout

    def settimeout(self, timeout):
        self.timeouts.append(timeout)
        self.timeout = timeout


def _build_test_server_response(
    client_packet,
    server_identity_private_key,
    *,
    server_random=b"\x51" * 32,
    server_ephemeral_private_key=None,
    client_signature_binding=None,
):
    client_hello = parse_client_hello_packet(client_packet)
    client_ephemeral_public_key = (
        udpsec_crypto.parse_ephemeral_public_key(
            client_hello.client_ephemeral_public_key
        )
    )
    if server_ephemeral_private_key is None:
        server_ephemeral_private_key = ec.derive_private_key(
            7,
            ec.SECP256R1(),
        )
    server_ephemeral_public_bytes = (
        udpsec_crypto.serialize_ephemeral_public_key(
            server_ephemeral_private_key.public_key()
        )
    )
    bound_client_signature = (
        client_hello.client_signature
        if client_signature_binding is None
        else client_signature_binding
    )
    server_digest = udpsec_crypto.build_server_auth_digest(
        station_id=client_hello.station_id,
        timestamp=client_hello.timestamp,
        client_random=client_hello.client_random,
        client_ephemeral_public_key=(
            client_hello.client_ephemeral_public_key
        ),
        client_signature=bound_client_signature,
        server_random=server_random,
        server_ephemeral_public_key=server_ephemeral_public_bytes,
    )
    server_signature = udpsec_crypto.sign_transcript_digest(
        server_identity_private_key,
        server_digest,
    )
    server_hello = ServerHello(
        server_random=server_random,
        server_ephemeral_public_key=server_ephemeral_public_bytes,
        server_signature=server_signature,
    )
    shared_secret = udpsec_crypto.derive_ephemeral_shared_secret(
        server_ephemeral_private_key,
        client_ephemeral_public_key,
    )
    transcript_hash = udpsec_crypto.build_session_transcript_hash(
        station_id=client_hello.station_id,
        timestamp=client_hello.timestamp,
        client_random=client_hello.client_random,
        client_ephemeral_public_key=(
            client_hello.client_ephemeral_public_key
        ),
        client_signature=client_hello.client_signature,
        server_random=server_hello.server_random,
        server_ephemeral_public_key=(
            server_hello.server_ephemeral_public_key
        ),
        server_signature=server_hello.server_signature,
    )
    key_material = udpsec_crypto.derive_session_key_material(
        shared_secret,
        transcript_hash,
    )
    return (
        build_server_hello_packet(server_hello),
        client_hello,
        server_hello,
        key_material,
    )


class _TestConfirmingServer:
    def __init__(
        self,
        proxy,
        server_identity_private_key,
        remote_addr,
        *,
        pong_nonce=b"\x91" * 12,
    ):
        self.proxy = proxy
        self.server_identity_private_key = server_identity_private_key
        self.remote_addr = remote_addr
        self.pong_nonce = pong_nonce
        self.client_hello = None
        self.server_hello = None
        self.key_material = None
        self.confirmation_packet = None
        self.confirmation_message = None
        self.confirmation_nonce = None
        self.pong_packet = None

    def server_hello_response(self, client_packet):
        response, client_hello, server_hello, key_material = (
            _build_test_server_response(
                client_packet,
                self.server_identity_private_key,
            )
        )
        self.client_hello = client_hello
        self.server_hello = server_hello
        self.key_material = key_material
        return response, self.remote_addr

    def read_confirmation_ping(self, confirmation_packet):
        assert self.key_material is not None
        message = self.proxy.decrypt_secure_json_message(
            confirmation_packet,
            self.key_material.client_to_server_key,
        )
        with pytest.raises(InvalidTag):
            self.proxy.decrypt_secure_json_message(
                confirmation_packet,
                self.key_material.server_to_client_key,
            )
        assert isinstance(message, dict)
        assert message.get("type") == "ping"
        assert message.get("seq") == (
            self.proxy.SESSION_CONFIRMATION_SEQUENCE
        )
        assert message.get("source_id") == self.client_hello.station_id
        assert isinstance(message.get("timestamp"), int)
        self.confirmation_packet = confirmation_packet
        self.confirmation_message = message
        self.confirmation_nonce = confirmation_packet[
            len(self.proxy.DATA_PREFIX):
            len(self.proxy.DATA_PREFIX) + 12
        ]
        return message

    def encrypt_server_packet(
        self,
        message,
        *,
        key=None,
        nonce=None,
    ):
        if key is None:
            key = self.key_material.server_to_client_key
        if nonce is None:
            nonce = self.pong_nonce
        return _encrypted_control_packet(
            self.proxy,
            key,
            nonce,
            message,
        )

    def confirmation_pong_response(self, confirmation_packet):
        message = self.read_confirmation_ping(confirmation_packet)
        self.pong_packet = self.encrypt_server_packet({
            "type": "pong",
            "seq": self.proxy.SESSION_CONFIRMATION_SEQUENCE,
            "timestamp": message["timestamp"],
            "source_id": self.client_hello.station_id,
        })
        return self.pong_packet, self.remote_addr


def test_proxy_handshake_succeeds_after_stale_no_session_hint(
    monkeypatch,
    capsys,
):
    proxy = load_proxy_module()
    timestamp = 1000
    station_id = "boat_001"
    remote_addr = ("192.0.2.10", 17777)
    station_identity_private_key = ec.derive_private_key(
        11,
        ec.SECP256R1(),
    )
    server_identity_private_key = ec.derive_private_key(
        12,
        ec.SECP256R1(),
    )
    confirming_server = _TestConfirmingServer(
        proxy,
        server_identity_private_key,
        remote_addr,
    )
    output_before_confirmation = []

    def confirmation_response(confirmation_packet):
        output_before_confirmation.append(capsys.readouterr().out)
        return confirming_server.confirmation_pong_response(
            confirmation_packet
        )

    sock = _FakeHandshakeSocket((
        (b"NOSESSION|boat_001", remote_addr),
        confirming_server.server_hello_response,
        confirmation_response,
    ))
    monkeypatch.setattr(proxy.time, "time", lambda: timestamp)
    parse_calls = []
    derive_calls = []
    original_parse = proxy.parse_ephemeral_public_key
    original_derive = proxy.derive_ephemeral_shared_secret

    def record_parse(encoded):
        parse_calls.append(encoded)
        return original_parse(encoded)

    def record_derive(private_key, public_key):
        derive_calls.append((private_key, public_key))
        return original_derive(private_key, public_key)

    monkeypatch.setattr(
        proxy,
        "parse_ephemeral_public_key",
        record_parse,
    )
    monkeypatch.setattr(
        proxy,
        "derive_ephemeral_shared_secret",
        record_derive,
    )

    key_material = proxy.perform_handshake(
        sock,
        {"station_id": station_id},
        station_identity_private_key,
        server_identity_private_key.public_key(),
        remote_addr,
    )

    client_hello = parse_client_hello_packet(sock.sent[0][0])
    client_digest = udpsec_crypto.build_client_auth_digest(
        station_id=client_hello.station_id,
        timestamp=client_hello.timestamp,
        client_random=client_hello.client_random,
        client_ephemeral_public_key=(
            client_hello.client_ephemeral_public_key
        ),
    )
    assert udpsec_crypto.verify_transcript_signature(
        station_identity_private_key.public_key(),
        client_hello.client_signature,
        client_digest,
    )
    assert key_material == confirming_server.key_material
    assert isinstance(key_material, SessionKeyMaterial)
    assert (
        key_material.client_to_server_key
        != key_material.server_to_client_key
    )
    assert len(client_hello.client_random) == 32
    assert len(client_hello.client_ephemeral_public_key) == 33
    assert len(parse_calls) == 1
    assert len(derive_calls) == 1
    assert derive_calls[0][0] is not station_identity_private_key
    assert len(sock.sent) == 2
    confirmation_packet, confirmation_addr = sock.sent[1]
    assert confirmation_addr == remote_addr
    assert confirmation_packet.startswith(proxy.DATA_PREFIX)
    assert confirming_server.confirmation_message == {
        "type": "ping",
        "seq": proxy.SESSION_CONFIRMATION_SEQUENCE,
        "timestamp": timestamp,
        "source_id": station_id,
    }
    assert len(confirming_server.confirmation_nonce) == 12
    assert "Mutual ECDHE session confirmed." not in (
        output_before_confirmation[0]
    )
    output = output_before_confirmation[0] + capsys.readouterr().out
    assert "Mutual ECDHE session confirmed." in output
    assert "Mutual ECDHE handshake established." not in output
    assert "Session hash" not in output
    assert key_material.client_to_server_key.hex() not in output
    assert key_material.server_to_client_key.hex() not in output
    with pytest.raises(InvalidTag):
        proxy.decrypt_secure_json_message(
            confirming_server.pong_packet,
            key_material.client_to_server_key,
        )
    assert sock.timeout == 5.0


def test_proxy_handshake_ignores_valid_reply_from_unexpected_remote(monkeypatch):
    proxy = load_proxy_module()
    timestamp = 1000
    station_id = "boat_001"
    remote_addr = ("192.0.2.10", 17777)
    other_addr = ("192.0.2.10", 17778)
    client_private_key = ec.derive_private_key(11, ec.SECP256R1())
    server_private_key = ec.derive_private_key(12, ec.SECP256R1())
    response_packets = []
    confirming_server = _TestConfirmingServer(
        proxy,
        server_private_key,
        remote_addr,
    )

    def response_from(address):
        def build_response(client_packet):
            response, _ = confirming_server.server_hello_response(
                client_packet
            )
            response_packets.append(response)
            return response, address

        return build_response

    sock = _FakeHandshakeSocket((
        response_from(other_addr),
        response_from(remote_addr),
        confirming_server.confirmation_pong_response,
    ))
    monkeypatch.setattr(proxy.time, "time", lambda: timestamp)

    key_material = proxy.perform_handshake(
        sock,
        {"station_id": station_id},
        client_private_key,
        server_private_key.public_key(),
        remote_addr,
    )

    assert isinstance(key_material, SessionKeyMaterial)
    assert len(response_packets) == 2
    assert len(sock.sent) == 2


@pytest.mark.parametrize(
    "mutation",
    (
        "wrong-identity",
        "server-random",
        "server-ephemeral",
        "client-signature-binding",
        "malformed-server-point",
        "old-response",
    ),
)
def test_proxy_handshake_rejects_unauthenticated_or_old_server_response(
    monkeypatch,
    mutation,
):
    proxy = load_proxy_module()
    timestamp = 1000
    remote_addr = ("192.0.2.10", 17777)
    station_identity_private_key = ec.derive_private_key(
        11,
        ec.SECP256R1(),
    )
    server_identity_private_key = ec.derive_private_key(
        12,
        ec.SECP256R1(),
    )
    other_identity_private_key = ec.derive_private_key(
        13,
        ec.SECP256R1(),
    )

    def invalid_response(client_packet):
        if mutation == "old-response":
            return b"OK|b2xkLXNpZ25hdHVyZQ==", remote_addr
        if mutation == "malformed-server-point":
            client_hello = parse_client_hello_packet(client_packet)
            malformed_public_bytes = b"\x02" + b"\xff" * 32
            server_random = b"\x51" * 32
            digest = udpsec_crypto.build_server_auth_digest(
                station_id=client_hello.station_id,
                timestamp=client_hello.timestamp,
                client_random=client_hello.client_random,
                client_ephemeral_public_key=(
                    client_hello.client_ephemeral_public_key
                ),
                client_signature=client_hello.client_signature,
                server_random=server_random,
                server_ephemeral_public_key=malformed_public_bytes,
            )
            signature = udpsec_crypto.sign_transcript_digest(
                server_identity_private_key,
                digest,
            )
            return build_server_hello_packet(
                ServerHello(
                    server_random=server_random,
                    server_ephemeral_public_key=malformed_public_bytes,
                    server_signature=signature,
                )
            ), remote_addr

        signing_key = (
            other_identity_private_key
            if mutation == "wrong-identity"
            else server_identity_private_key
        )
        client_signature_binding = (
            b"different-client-signature"
            if mutation == "client-signature-binding"
            else None
        )
        response, _, server_hello, _ = _build_test_server_response(
            client_packet,
            signing_key,
            client_signature_binding=client_signature_binding,
        )
        if mutation == "server-random":
            response = build_server_hello_packet(
                ServerHello(
                    server_random=b"\x52" * 32,
                    server_ephemeral_public_key=(
                        server_hello.server_ephemeral_public_key
                    ),
                    server_signature=server_hello.server_signature,
                )
            )
        elif mutation == "server-ephemeral":
            changed_public_bytes = (
                udpsec_crypto.serialize_ephemeral_public_key(
                    ec.derive_private_key(
                        8,
                        ec.SECP256R1(),
                    ).public_key()
                )
            )
            response = build_server_hello_packet(
                ServerHello(
                    server_random=server_hello.server_random,
                    server_ephemeral_public_key=changed_public_bytes,
                    server_signature=server_hello.server_signature,
                )
            )
        return response, remote_addr

    sock = _FakeHandshakeSocket(
        (invalid_response, proxy.socket.timeout())
    )
    monkeypatch.setattr(proxy.time, "time", lambda: timestamp)

    key_material = proxy.perform_handshake(
        sock,
        {"station_id": "boat_001"},
        station_identity_private_key,
        server_identity_private_key.public_key(),
        remote_addr,
    )

    assert key_material is None
    assert len(sock.sent) == 1
    assert sock.timeout == 5.0


def test_separate_proxy_handshakes_use_fresh_random_ephemeral_and_keys(
    monkeypatch,
):
    proxy = load_proxy_module()
    timestamp = 1000
    remote_addr = ("192.0.2.10", 17777)
    station_identity_private_key = ec.derive_private_key(
        11,
        ec.SECP256R1(),
    )
    server_identity_private_key = ec.derive_private_key(
        12,
        ec.SECP256R1(),
    )
    random_lengths = []
    random_values = iter((
        b"\x61" * 32,
        b"\x71" * 12,
        b"\x62" * 32,
        b"\x72" * 12,
    ))
    client_ephemeral_keys = iter(
        (
            ec.derive_private_key(14, ec.SECP256R1()),
            ec.derive_private_key(15, ec.SECP256R1()),
        )
    )

    def next_random(length):
        random_lengths.append(length)
        return next(random_values)

    monkeypatch.setattr(proxy.time, "time", lambda: timestamp)
    monkeypatch.setattr(proxy.os, "urandom", next_random)
    monkeypatch.setattr(
        proxy,
        "generate_ephemeral_private_key",
        lambda: next(client_ephemeral_keys),
    )
    sent_hellos = []
    confirmation_nonces = []

    def new_socket():
        confirming_server = _TestConfirmingServer(
            proxy,
            server_identity_private_key,
            remote_addr,
        )

        def server_hello_response(client_packet):
            sent_hellos.append(parse_client_hello_packet(client_packet))
            return confirming_server.server_hello_response(
                client_packet
            )

        def confirmation_response(confirmation_packet):
            response = confirming_server.confirmation_pong_response(
                confirmation_packet
            )
            confirmation_nonces.append(
                confirming_server.confirmation_nonce
            )
            return response

        return _FakeHandshakeSocket((
            server_hello_response,
            confirmation_response,
        ))

    first_material = proxy.perform_handshake(
        new_socket(),
        {"station_id": "boat_001"},
        station_identity_private_key,
        server_identity_private_key.public_key(),
        remote_addr,
    )
    second_material = proxy.perform_handshake(
        new_socket(),
        {"station_id": "boat_001"},
        station_identity_private_key,
        server_identity_private_key.public_key(),
        remote_addr,
    )

    assert random_lengths == [32, 12, 32, 12]
    assert sent_hellos[0].client_random != sent_hellos[1].client_random
    assert sent_hellos[0].client_ephemeral_public_key != (
        sent_hellos[1].client_ephemeral_public_key
    )
    assert first_material != second_material
    assert first_material.client_to_server_key != (
        second_material.client_to_server_key
    )
    assert first_material.server_to_client_key != (
        second_material.server_to_client_key
    )
    assert confirmation_nonces == [b"\x71" * 12, b"\x72" * 12]


def test_runtime_end_to_end_ecdhe_and_directional_encryption(monkeypatch):
    secure, station_identity_private_key = (
        load_secure_module_with_fake_keys(
            monkeypatch,
            with_client_private_key=True,
        )
    )
    proxy = load_proxy_module()
    timestamp = 1000
    remote_addr = ("192.0.2.10", 17777)
    client_addr = ("192.0.2.20", 50123)
    state = secure.SecureState()
    recorded_server_keys = []

    class _RecordingAESGCM:
        def __init__(self, key):
            self.key = key
            self.delegate = AESGCM(key)
            recorded_server_keys.append(key)

        def encrypt(self, *args):
            return self.delegate.encrypt(*args)

        def decrypt(self, *args):
            return self.delegate.decrypt(*args)

    monkeypatch.setattr(secure, "AESGCM", _RecordingAESGCM)
    monkeypatch.setattr(proxy.time, "time", lambda: timestamp)

    class _RuntimeBridgeSocket:
        def __init__(self):
            self.client_packets = []
            self.timeout = 5.0

        def sendto(self, packet, destination):
            assert destination == remote_addr
            self.client_packets.append(packet)

        def recvfrom(self, _size):
            client_packet = self.client_packets.pop(0)
            _, server_socket = _run_secure_server_with_packets(
                monkeypatch,
                secure,
                [(client_packet, client_addr)],
                state=state,
                wall_clock=_FakeClock(float(timestamp)),
                monotonic_clock=_FakeClock(10.0),
            )
            assert len(server_socket.sent) == 1
            response, response_addr = server_socket.sent[0]
            assert response_addr == client_addr
            return response, remote_addr

        def gettimeout(self):
            return self.timeout

        def settimeout(self, timeout):
            self.timeout = timeout

    bridge_socket = _RuntimeBridgeSocket()
    client_key_material = proxy.perform_handshake(
        bridge_socket,
        {"station_id": "boat_001"},
        station_identity_private_key,
        _PREPARED_SERVER_PRIVATE_KEY.public_key(),
        remote_addr,
    )

    assert isinstance(client_key_material, SessionKeyMaterial)
    assert recorded_server_keys == [
        client_key_material.client_to_server_key,
        client_key_material.server_to_client_key,
    ]
    assert (
        client_key_material.client_to_server_key
        != client_key_material.server_to_client_key
    )
    server_session = state._sessions[client_addr]
    assert (
        server_session.client_to_server_aesgcm
        is not server_session.server_to_client_aesgcm
    )

    nmea_packet = proxy.encrypt_secure_json_message(
        {
            "type": "nmea",
            "payload": "!AIVDM,1,1,,A,payload,0*00",
            "timestamp": timestamp,
            "source_id": "boat_001",
        },
        client_key_material.client_to_server_key,
    )
    nmea_queue, nmea_socket = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(nmea_packet, client_addr)],
        state=state,
        wall_clock=_FakeClock(float(timestamp)),
        monotonic_clock=_FakeClock(11.0),
    )

    assert len(nmea_queue.items) == 1
    assert nmea_queue.items[0].payload == (
        b"!AIVDM,1,1,,A,payload,0*00"
    )
    assert nmea_socket.sent == []

    ping_packet = proxy.encrypt_secure_json_message(
        {
            "type": "ping",
            "seq": 7,
            "timestamp": timestamp,
            "source_id": "boat_001",
        },
        client_key_material.client_to_server_key,
    )
    ping_queue, ping_socket = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(ping_packet, client_addr)],
        state=state,
        wall_clock=_FakeClock(float(timestamp)),
        monotonic_clock=_FakeClock(12.0),
    )

    assert ping_queue.items == []
    assert len(ping_socket.sent) == 1
    pong_packet, pong_addr = ping_socket.sent[0]
    assert pong_addr == client_addr
    assert proxy.handle_server_packet(
        pong_packet,
        remote_addr,
        remote_addr,
        client_key_material.server_to_client_key,
        "boat_001",
        7,
    ) == proxy.SERVER_PACKET_AUTHENTICATED
    assert proxy.handle_server_packet(
        pong_packet,
        remote_addr,
        remote_addr,
        client_key_material.client_to_server_key,
        "boat_001",
        7,
    ) == proxy.SERVER_PACKET_IGNORED
    assert len(ping_packet[len(proxy.DATA_PREFIX):][:12]) == 12
    assert len(pong_packet[len(proxy.DATA_PREFIX):][:12]) == 12


def test_proxy_handshake_ignores_stale_active_pong_before_confirmation(
    monkeypatch,
):
    proxy = load_proxy_module()
    timestamp = 1000
    station_id = "boat_001"
    remote_addr = ("192.0.2.10", 17777)
    station_identity_private_key = ec.derive_private_key(
        11,
        ec.SECP256R1(),
    )
    server_identity_private_key = ec.derive_private_key(
        12,
        ec.SECP256R1(),
    )
    old_active_keys = _proxy_session_key_material(
        proxy,
        client_to_server_key=b"\x31" * 32,
        server_to_client_key=b"\x32" * 32,
    )
    confirming_server = _TestConfirmingServer(
        proxy,
        server_identity_private_key,
        remote_addr,
    )
    stale_packets = []

    def stale_active_pong(confirmation_packet):
        message = confirming_server.read_confirmation_ping(
            confirmation_packet
        )
        packet = confirming_server.encrypt_server_packet(
            {
                "type": "pong",
                "seq": proxy.SESSION_CONFIRMATION_SEQUENCE,
                "timestamp": message["timestamp"],
                "source_id": station_id,
            },
            key=old_active_keys.server_to_client_key,
            nonce=b"\xb1" * 12,
        )
        stale_packets.append(packet)
        return packet, remote_addr

    sock = _FakeHandshakeSocket((
        confirming_server.server_hello_response,
        stale_active_pong,
        confirming_server.confirmation_pong_response,
    ))
    monkeypatch.setattr(proxy.time, "time", lambda: timestamp)

    key_material = proxy.perform_handshake(
        sock,
        {"station_id": station_id},
        station_identity_private_key,
        server_identity_private_key.public_key(),
        remote_addr,
    )

    assert key_material == confirming_server.key_material
    assert (
        key_material.server_to_client_key
        != old_active_keys.server_to_client_key
    )
    assert proxy.decrypt_secure_json_message(
        stale_packets[0],
        old_active_keys.server_to_client_key,
    )["seq"] == proxy.SESSION_CONFIRMATION_SEQUENCE
    with pytest.raises(InvalidTag):
        proxy.decrypt_secure_json_message(
            stale_packets[0],
            key_material.server_to_client_key,
        )
    assert len(sock.sent) == 2
    assert sock.responses == []
    assert sock.timeout == 5.0


@pytest.mark.parametrize(
    "mutation",
    (
        "reverse-direction-key",
        "wrong-sequence",
        "bool-sequence",
        "wrong-source",
        "wrong-type",
        "malformed-json",
        "non-dict-json",
    ),
)
def test_proxy_handshake_ignores_invalid_confirmation_before_valid(
    monkeypatch,
    mutation,
    capsys,
):
    proxy = load_proxy_module()
    timestamp = 1000
    station_id = "boat_001"
    remote_addr = ("192.0.2.10", 17777)
    station_identity_private_key = ec.derive_private_key(
        11,
        ec.SECP256R1(),
    )
    server_identity_private_key = ec.derive_private_key(
        12,
        ec.SECP256R1(),
    )
    confirming_server = _TestConfirmingServer(
        proxy,
        server_identity_private_key,
        remote_addr,
    )

    def invalid_confirmation(confirmation_packet):
        message = confirming_server.read_confirmation_ping(
            confirmation_packet
        )
        pong = {
            "type": "pong",
            "seq": proxy.SESSION_CONFIRMATION_SEQUENCE,
            "timestamp": message["timestamp"],
            "source_id": station_id,
        }
        key = confirming_server.key_material.server_to_client_key
        if mutation == "reverse-direction-key":
            key = confirming_server.key_material.client_to_server_key
        elif mutation == "wrong-sequence":
            pong["seq"] = 1
        elif mutation == "bool-sequence":
            pong["seq"] = False
        elif mutation == "wrong-source":
            pong["source_id"] = "other_station"
        elif mutation == "wrong-type":
            pong["type"] = "status"

        if mutation == "malformed-json":
            nonce = b"\xa1" * 12
            encrypted = AESGCM(key).encrypt(
                nonce,
                b"not-json",
                proxy.DATA_AAD,
            )
            packet = proxy.DATA_PREFIX + nonce + encrypted
        elif mutation == "non-dict-json":
            packet = confirming_server.encrypt_server_packet(
                ["pong", proxy.SESSION_CONFIRMATION_SEQUENCE],
                key=key,
                nonce=b"\xa2" * 12,
            )
        else:
            packet = confirming_server.encrypt_server_packet(
                pong,
                key=key,
                nonce=b"\xa3" * 12,
            )
        return packet, remote_addr

    sock = _FakeHandshakeSocket((
        confirming_server.server_hello_response,
        invalid_confirmation,
        confirming_server.confirmation_pong_response,
    ))
    monkeypatch.setattr(proxy.time, "time", lambda: timestamp)

    key_material = proxy.perform_handshake(
        sock,
        {"station_id": station_id},
        station_identity_private_key,
        server_identity_private_key.public_key(),
        remote_addr,
    )

    output = capsys.readouterr().out
    assert key_material == confirming_server.key_material
    assert len(sock.sent) == 2
    assert sock.responses == []
    assert "Mutual ECDHE session confirmed." in output
    assert "Invalid secure session confirmation." not in output
    assert confirming_server.key_material.client_to_server_key.hex() not in (
        output
    )
    assert confirming_server.key_material.server_to_client_key.hex() not in (
        output
    )
    assert sock.timeout == 5.0


def test_proxy_handshake_ignores_confirmation_from_unexpected_remote(
    monkeypatch,
):
    proxy = load_proxy_module()
    timestamp = 1000
    station_id = "boat_001"
    remote_addr = ("192.0.2.10", 17777)
    other_addr = ("192.0.2.10", 17778)
    station_identity_private_key = ec.derive_private_key(
        11,
        ec.SECP256R1(),
    )
    server_identity_private_key = ec.derive_private_key(
        12,
        ec.SECP256R1(),
    )
    confirming_server = _TestConfirmingServer(
        proxy,
        server_identity_private_key,
        remote_addr,
    )

    def confirmation_from_other_addr(confirmation_packet):
        packet, _ = confirming_server.confirmation_pong_response(
            confirmation_packet
        )
        return packet, other_addr

    def confirmation_from_expected_addr(_confirmation_packet):
        return confirming_server.pong_packet, remote_addr

    sock = _FakeHandshakeSocket((
        confirming_server.server_hello_response,
        confirmation_from_other_addr,
        confirmation_from_expected_addr,
    ))
    monkeypatch.setattr(proxy.time, "time", lambda: timestamp)

    key_material = proxy.perform_handshake(
        sock,
        {"station_id": station_id},
        station_identity_private_key,
        server_identity_private_key.public_key(),
        remote_addr,
    )

    assert key_material == confirming_server.key_material
    assert len(sock.sent) == 2
    assert sock.timeout == 5.0


def test_proxy_handshake_fails_on_no_session_during_confirmation(
    monkeypatch,
    capsys,
):
    proxy = load_proxy_module()
    timestamp = 1000
    station_id = "boat_001"
    remote_addr = ("192.0.2.10", 17777)
    station_identity_private_key = ec.derive_private_key(
        11,
        ec.SECP256R1(),
    )
    server_identity_private_key = ec.derive_private_key(
        12,
        ec.SECP256R1(),
    )
    confirming_server = _TestConfirmingServer(
        proxy,
        server_identity_private_key,
        remote_addr,
    )
    sock = _FakeHandshakeSocket((
        confirming_server.server_hello_response,
        (b"NOSESSION|boat_001", remote_addr),
        confirming_server.confirmation_pong_response,
    ))
    monkeypatch.setattr(proxy.time, "time", lambda: timestamp)

    key_material = proxy.perform_handshake(
        sock,
        {"station_id": station_id},
        station_identity_private_key,
        server_identity_private_key.public_key(),
        remote_addr,
    )

    assert key_material is None
    assert len(sock.sent) == 2
    assert len(sock.responses) == 1
    assert "Mutual ECDHE session confirmed." not in (
        capsys.readouterr().out
    )
    assert sock.timeout == 5.0


@pytest.mark.parametrize(
    "confirmation_failure",
    ("timeout", "socket-error"),
)
def test_proxy_handshake_confirmation_failure_returns_to_retry_loop(
    monkeypatch,
    confirmation_failure,
    capsys,
):
    proxy = load_proxy_module()
    timestamp = 1000
    remote_addr = ("192.0.2.10", 17777)
    station_identity_private_key = ec.derive_private_key(
        11,
        ec.SECP256R1(),
    )
    server_identity_private_key = ec.derive_private_key(
        12,
        ec.SECP256R1(),
    )
    confirming_server = _TestConfirmingServer(
        proxy,
        server_identity_private_key,
        remote_addr,
    )
    failure = (
        proxy.socket.timeout()
        if confirmation_failure == "timeout"
        else OSError("confirmation receive failed")
    )
    sock = _FakeHandshakeSocket((
        confirming_server.server_hello_response,
        failure,
    ))
    monkeypatch.setattr(proxy.time, "time", lambda: timestamp)

    key_material = proxy.perform_handshake(
        sock,
        {"station_id": "boat_001"},
        station_identity_private_key,
        server_identity_private_key.public_key(),
        remote_addr,
    )

    assert key_material is None
    assert len(sock.sent) == 2
    assert "Mutual ECDHE session confirmed." not in (
        capsys.readouterr().out
    )
    assert sock.timeout == 5.0


def test_proxy_handshake_confirmation_send_error_returns_to_retry_loop(
    monkeypatch,
    capsys,
):
    proxy = load_proxy_module()
    timestamp = 1000
    remote_addr = ("192.0.2.10", 17777)
    station_identity_private_key = ec.derive_private_key(
        11,
        ec.SECP256R1(),
    )
    server_identity_private_key = ec.derive_private_key(
        12,
        ec.SECP256R1(),
    )
    confirming_server = _TestConfirmingServer(
        proxy,
        server_identity_private_key,
        remote_addr,
    )

    class ConfirmationSendFailingSocket(_FakeHandshakeSocket):
        def sendto(self, data, addr):
            if self.sent:
                raise OSError("confirmation send failed")
            super().sendto(data, addr)

    sock = ConfirmationSendFailingSocket((
        confirming_server.server_hello_response,
    ))
    monkeypatch.setattr(proxy.time, "time", lambda: timestamp)

    key_material = proxy.perform_handshake(
        sock,
        {"station_id": "boat_001"},
        station_identity_private_key,
        server_identity_private_key.public_key(),
        remote_addr,
    )

    assert key_material is None
    assert len(sock.sent) == 1
    assert "Mutual ECDHE session confirmed." not in (
        capsys.readouterr().out
    )
    assert sock.timeout == 5.0


def test_proxy_handshake_uses_fresh_confirmation_deadline(monkeypatch):
    proxy = load_proxy_module()
    timestamp = 1000
    remote_addr = ("192.0.2.10", 17777)
    station_identity_private_key = ec.derive_private_key(
        11,
        ec.SECP256R1(),
    )
    server_identity_private_key = ec.derive_private_key(
        12,
        ec.SECP256R1(),
    )
    confirming_server = _TestConfirmingServer(
        proxy,
        server_identity_private_key,
        remote_addr,
    )
    sock = _FakeHandshakeSocket((
        confirming_server.server_hello_response,
        confirming_server.confirmation_pong_response,
    ))
    monotonic_values = iter((100.0, 101.0, 200.0, 201.0))
    monkeypatch.setattr(proxy.time, "time", lambda: timestamp)
    monkeypatch.setattr(
        proxy.time,
        "monotonic",
        lambda: next(monotonic_values),
    )

    key_material = proxy.perform_handshake(
        sock,
        {"station_id": "boat_001"},
        station_identity_private_key,
        server_identity_private_key.public_key(),
        remote_addr,
    )

    assert key_material == confirming_server.key_material
    assert sock.timeouts == [4.0, 4.0, 5.0]


def test_proxy_ignored_datagrams_do_not_extend_confirmation_deadline(
    monkeypatch,
    capsys,
):
    proxy = load_proxy_module()
    timestamp = 1000
    remote_addr = ("192.0.2.10", 17777)
    station_identity_private_key = ec.derive_private_key(
        11,
        ec.SECP256R1(),
    )
    server_identity_private_key = ec.derive_private_key(
        12,
        ec.SECP256R1(),
    )
    confirming_server = _TestConfirmingServer(
        proxy,
        server_identity_private_key,
        remote_addr,
    )
    sock = _FakeHandshakeSocket((
        confirming_server.server_hello_response,
        (b"malformed-confirmation", remote_addr),
        (b"unrelated-same-address-datagram", remote_addr),
    ))
    monotonic_values = iter((100.0, 101.0, 200.0, 201.0, 204.0, 205.0))
    monkeypatch.setattr(proxy.time, "time", lambda: timestamp)
    monkeypatch.setattr(
        proxy.time,
        "monotonic",
        lambda: next(monotonic_values),
    )

    key_material = proxy.perform_handshake(
        sock,
        {"station_id": "boat_001"},
        station_identity_private_key,
        server_identity_private_key.public_key(),
        remote_addr,
    )

    output = capsys.readouterr().out
    assert key_material is None
    assert sock.responses == []
    assert sock.timeouts == [4.0, 4.0, 1.0, 5.0]
    assert "No session confirmation from server." in output
    assert "Mutual ECDHE session confirmed." not in output


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
        _proxy_session_key_material(proxy),
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
        _proxy_session_key_material(proxy),
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
        _proxy_session_key_material(proxy),
        ("192.0.2.10", 17777),
    )

    assert reason == proxy.SESSION_END_SOCKET_ERROR


def test_proxy_healthy_ping_pong_runs_past_old_refresh_interval(monkeypatch):
    proxy = load_proxy_module()
    client_to_server_key = b"\x01" * 32
    server_to_client_key = b"\x02" * 32
    key_material = _proxy_session_key_material(
        proxy,
        client_to_server_key=client_to_server_key,
        server_to_client_key=server_to_client_key,
    )
    remote_addr = ("192.0.2.10", 17777)
    clock = [0.0]

    class FakeLocalSocket:
        pass

    class FakeOutSocket:
        def __init__(self):
            self.responses = []
            self.pong_count = 0

        def sendto(self, data, addr):
            ping = proxy.decrypt_secure_json_message(
                data,
                client_to_server_key,
            )
            self.responses.append(
                proxy.encrypt_secure_json_message(
                    {
                        "type": "pong",
                        "seq": ping["seq"],
                        "timestamp": int(clock[0]),
                        "source_id": "boat_001",
                    },
                    server_to_client_key,
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
        key_material,
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


def test_session_ttl_seconds_is_300(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)

    assert secure.SESSION_TTL_SECONDS == 300
    assert secure.SESSION_MAX == 100000
    assert secure.PENDING_SESSION_TTL_SECONDS == 30
    assert secure.PENDING_SESSION_MAX == secure.SESSION_MAX


def test_data_nonce_constants(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)

    assert secure.DATA_NONCE_TTL_SECONDS == secure.SESSION_TTL_SECONDS
    assert secure.DATA_NONCE_MAX_PER_SESSION == 100000


@pytest.mark.parametrize(
    "field",
    [
        "max_sessions",
        "max_pending_sessions",
        "handshake_replay_max",
        "data_nonce_max_per_session",
    ],
)
@pytest.mark.parametrize("value", [True, 1.0, 1.5, "2", None])
def test_secure_state_rejects_non_integer_maximums(monkeypatch, field, value):
    secure = load_secure_module_with_fake_keys(monkeypatch)

    with pytest.raises(TypeError):
        secure.SecureState(**{field: value})


@pytest.mark.parametrize(
    "field",
    [
        "max_sessions",
        "max_pending_sessions",
        "handshake_replay_max",
        "data_nonce_max_per_session",
    ],
)
@pytest.mark.parametrize("value", [0, -1])
def test_secure_state_rejects_non_positive_maximums(monkeypatch, field, value):
    secure = load_secure_module_with_fake_keys(monkeypatch)

    with pytest.raises(ValueError):
        secure.SecureState(**{field: value})


@pytest.mark.parametrize(
    "field",
    [
        "session_ttl",
        "pending_session_ttl",
        "handshake_replay_ttl",
        "data_nonce_ttl",
    ],
)
@pytest.mark.parametrize("value", [True, "1", None])
def test_secure_state_rejects_non_numeric_ttls(monkeypatch, field, value):
    secure = load_secure_module_with_fake_keys(monkeypatch)

    with pytest.raises(TypeError):
        secure.SecureState(**{field: value})


@pytest.mark.parametrize(
    "field",
    [
        "session_ttl",
        "pending_session_ttl",
        "handshake_replay_ttl",
        "data_nonce_ttl",
    ],
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
        pending_session_ttl=1.25,
        max_pending_sessions=2,
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
    client_to_server_aesgcm = object()
    server_to_client_aesgcm = object()

    session = state.install_session(
        addr,
        "boat_001",
        client_to_server_aesgcm,
        server_to_client_aesgcm,
        now=100.0,
    )

    assert session.station_id == "boat_001"
    assert session.client_to_server_aesgcm is client_to_server_aesgcm
    assert session.server_to_client_aesgcm is server_to_client_aesgcm
    assert session.created_at == 100.0
    assert session.last_seen == 100.0
    assert len(session.seen_data_nonces) == 0
    assert tuple(state._sessions) == (addr,)
    assert state.stats().sessions_created == 1


def test_data_nonce_accepts_first_validated_nonce(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(data_nonce_ttl=60.0)
    session = state.install_session(
        ("192.0.2.10", 50000),
        "boat_001",
        object(),
        object(),
        now=100.0,
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
        ("192.0.2.10", 50000),
        "boat_001",
        object(),
        object(),
        now=100.0,
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
        ("192.0.2.10", 50000),
        "boat_001",
        object(),
        object(),
        now=0.0,
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
        ("192.0.2.10", 50000),
        "boat_001",
        object(),
        object(),
        now=100.0,
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
        ("192.0.2.10", 50000),
        "boat_001",
        object(),
        object(),
        now=0.0,
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
        ("192.0.2.10", 50000),
        "boat_001",
        object(),
        object(),
        now=100.0,
    )
    second = state.install_session(
        ("192.0.2.11", 50001),
        "boat_002",
        object(),
        object(),
        now=100.0,
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
    session = state.install_session(
        addr,
        "boat_001",
        object(),
        object(),
        now=100.0,
    )

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
    first = state.install_session(
        first_addr,
        "first",
        object(),
        object(),
        now=100.0,
    )
    state.install_session(
        second_addr,
        "second",
        object(),
        object(),
        now=110.0,
    )

    assert tuple(state._sessions) == (first_addr, second_addr)
    assert state.touch_session(first_addr, first, now=125.0)

    assert first.created_at == 100.0
    assert first.last_seen == 125.0
    assert tuple(state._sessions) == (second_addr, first_addr)
    assert state.stats().sessions_touched == 1


def test_session_cleanup_removes_expired_lru_prefix_and_stops_at_live(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(session_ttl=30.0)
    expired_addr = ("192.0.2.10", 50000)
    active_addr = ("192.0.2.11", 50001)
    state.install_session(
        expired_addr,
        "expired",
        object(),
        object(),
        now=90.0,
    )
    state.install_session(
        active_addr,
        "active",
        object(),
        object(),
        now=100.0,
    )

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
    first = state.install_session(
        first_addr,
        "first",
        object(),
        object(),
        now=100.0,
    )
    state.install_session(
        second_addr,
        "second",
        object(),
        object(),
        now=110.0,
    )
    assert state.touch_session(first_addr, first, now=120.0)

    state.install_session(
        third_addr,
        "third",
        object(),
        object(),
        now=130.0,
    )

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
    state.install_session(
        first_addr,
        "first",
        object(),
        object(),
        now=100.0,
    )
    state.install_session(
        second_addr,
        "second",
        object(),
        object(),
        now=100.0,
    )

    state.install_session(
        third_addr,
        "third",
        object(),
        object(),
        now=100.0,
    )

    assert tuple(state._sessions) == (second_addr, third_addr)
    assert state.stats().sessions_capacity_evicted == 1


def test_expired_sessions_are_removed_before_capacity_eviction(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(session_ttl=30.0, max_sessions=2)
    expired_addr = ("192.0.2.10", 50000)
    active_addr = ("192.0.2.11", 50001)
    new_addr = ("192.0.2.12", 50002)
    state.install_session(
        expired_addr,
        "expired",
        object(),
        object(),
        now=90.0,
    )
    state.install_session(
        active_addr,
        "active",
        object(),
        object(),
        now=100.0,
    )

    state.install_session(
        new_addr,
        "new",
        object(),
        object(),
        now=120.0,
    )

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
    old_client_to_server_aesgcm = object()
    old_server_to_client_aesgcm = object()
    new_client_to_server_aesgcm = object()
    new_server_to_client_aesgcm = object()
    old = state.install_session(
        replaced_addr,
        "old",
        old_client_to_server_aesgcm,
        old_server_to_client_aesgcm,
        now=100.0,
    )
    state.install_session(
        other_addr,
        "other",
        object(),
        object(),
        now=110.0,
    )
    nonce = b"\x01" * 12
    assert state.accept_data_nonce(old, nonce, now=115.0)

    new = state.install_session(
        replaced_addr,
        "new",
        new_client_to_server_aesgcm,
        new_server_to_client_aesgcm,
        now=120.0,
    )

    assert new is state._sessions[replaced_addr]
    assert new is not old
    assert (
        new.client_to_server_aesgcm
        is new_client_to_server_aesgcm
    )
    assert (
        new.server_to_client_aesgcm
        is new_server_to_client_aesgcm
    )
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
    old = state.install_session(
        addr,
        "old",
        object(),
        object(),
        now=100.0,
    )
    assert state.accept_data_nonce(old, b"\x01" * 12, now=100.0)

    new = state.install_session(
        addr,
        "new",
        object(),
        object(),
        now=130.0,
    )

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
        ("192.0.2.10", 50000),
        "first",
        object(),
        object(),
        now=100.0,
    )
    assert state.accept_data_nonce(first, b"\x01" * 12, now=100.0)
    assert state.accept_data_nonce(first, b"\x02" * 12, now=101.0)

    state.install_session(
        ("192.0.2.11", 50001),
        "second",
        object(),
        object(),
        now=102.0,
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
        old_addr,
        "old",
        object(),
        object(),
        now=100.0,
    )
    assert state.accept_data_nonce(old, b"\x01" * 12, now=100.0)

    state.install_session(
        ("192.0.2.11", 50001),
        "new",
        object(),
        object(),
        now=101.0,
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
        stale_addr,
        "stale",
        object(),
        object(),
        now=0.0,
    )
    expiring = state.install_session(
        expiring_addr,
        "expiring",
        object(),
        object(),
        now=2.0,
    )
    expiring_nonce = b"\x01" * 12
    assert state.accept_data_nonce(
        expiring, expiring_nonce, now=2.0
    )
    replacement = state.install_session(
        stale_addr,
        "replacement",
        object(),
        object(),
        now=3.0,
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
        stale_addr,
        "stale",
        object(),
        object(),
        now=0.0,
    )
    assert state.accept_data_nonce(
        stale, b"\x01" * 12, now=0.0
    )
    expiring = state.install_session(
        expiring_addr,
        "expiring",
        object(),
        object(),
        now=2.0,
    )
    expiring_nonce = b"\x02" * 12
    assert state.accept_data_nonce(
        expiring, expiring_nonce, now=2.0
    )
    state.install_session(
        replacement_addr,
        "replacement",
        object(),
        object(),
        now=3.0,
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
        stale_addr,
        "stale",
        object(),
        object(),
        now=0.0,
    )
    expiring = state.install_session(
        expiring_addr,
        "expiring",
        object(),
        object(),
        now=2.0,
    )
    state.install_session(
        stale_addr,
        "replacement",
        object(),
        object(),
        now=3.0,
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
        expiring_addr,
        "expiring",
        object(),
        object(),
        now=2.0,
    )
    current = state.install_session(
        current_addr,
        "current",
        object(),
        object(),
        now=3.0,
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
        addr,
        "old",
        object(),
        object(),
        now=0.0,
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
        "pending_sessions_created",
        "pending_sessions_replaced",
        "pending_sessions_promoted",
        "pending_sessions_expired",
        "pending_sessions_capacity_evicted",
        "data_nonces_accepted",
        "data_nonce_replays",
        "data_nonces_expired",
        "data_nonces_capacity_evicted",
        "data_nonces_session_discarded",
        "current_handshake_replays",
        "peak_handshake_replays",
        "current_sessions",
        "peak_sessions",
        "current_pending_sessions",
        "peak_pending_sessions",
        "current_data_nonces",
        "peak_data_nonces",
    }
    assert all(value == 0 for value in vars(initial).values())
    with pytest.raises(FrozenInstanceError):
        initial.current_sessions = 1

    state.install_session(
        ("192.0.2.10", 50000),
        "boat_001",
        object(),
        object(),
        now=100.0,
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
    state.install_session(
        addr,
        "boat_001",
        object(),
        object(),
        now=0.0,
    )

    def fail_clock():
        raise AssertionError("stats must not read a clock")

    monkeypatch.setattr(secure.time, "time", fail_clock)
    monkeypatch.setattr(secure.time, "monotonic", fail_clock)

    stats = state.stats()

    assert stats.current_sessions == 1
    assert tuple(state._sessions) == (addr,)
    assert stats.sessions_expired == 0


@pytest.mark.parametrize(
    ("existing_member", "legacy_exists", "expected_path"),
    (
        ("private", True, "canonical"),
        ("public", False, "canonical"),
        ("public", True, "legacy"),
    ),
)
def test_proxy_default_station_identity_honors_canonical_and_legacy_precedence(
    monkeypatch,
    tmp_path,
    existing_member,
    legacy_exists,
    expected_path,
):
    proxy = load_proxy_module()
    keys_dir = tmp_path / "keys"
    keys_dir.mkdir()
    canonical_private = keys_dir / "station_private.pem"
    canonical_public = keys_dir / "station_public.pem"
    legacy_private = keys_dir / "station_private.key"
    existing_path = (
        canonical_private if existing_member == "private" else canonical_public
    )
    existing_path.write_bytes(b"operator canonical material")
    if legacy_exists:
        legacy_private.write_bytes(b"operator legacy material")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "remote_host: 192.0.2.10\n"
        "remote_port: 19999\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        proxy,
        "CANONICAL_STATION_PRIVATE_KEY_PATH",
        str(canonical_private),
    )
    monkeypatch.setattr(
        proxy,
        "CANONICAL_STATION_PUBLIC_KEY_PATH",
        str(canonical_public),
    )
    monkeypatch.setattr(
        proxy,
        "LEGACY_STATION_PRIVATE_KEY_PATH",
        str(legacy_private),
    )

    config = proxy.load_config(str(config_path))

    expected = canonical_private if expected_path == "canonical" else legacy_private
    assert config["station_private_key"] == str(expected)


def test_proxy_default_station_private_key_uses_canonical_when_no_key_exists(
    monkeypatch,
    tmp_path,
):
    proxy = load_proxy_module()
    canonical_path = tmp_path / "keys" / "station_private.pem"
    canonical_public_path = tmp_path / "keys" / "station_public.pem"
    legacy_path = tmp_path / "keys" / "station_private.key"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "remote_host: 192.0.2.10\n"
        "remote_port: 19999\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        proxy,
        "CANONICAL_STATION_PRIVATE_KEY_PATH",
        str(canonical_path),
    )
    monkeypatch.setattr(
        proxy,
        "CANONICAL_STATION_PUBLIC_KEY_PATH",
        str(canonical_public_path),
    )
    monkeypatch.setattr(
        proxy,
        "LEGACY_STATION_PRIVATE_KEY_PATH",
        str(legacy_path),
    )

    config = proxy.load_config(str(config_path))

    assert not canonical_path.exists()
    assert not legacy_path.exists()
    assert config["output"]["type"] == "udpsec"
    assert config["station_private_key"] == str(canonical_path)


def test_proxy_plain_output_does_not_inspect_or_resolve_udpsec_key_paths(
    monkeypatch,
    tmp_path,
):
    proxy = load_proxy_module()
    config_path = tmp_path / "plain.yaml"
    config_path.write_text(
        "station_private_key: operator/station.pem\n"
        "remote_public_key: trust/aismixer.pem\n"
        "output:\n"
        "  type: udp\n"
        "  host: 192.0.2.10\n"
        "  port: 17777\n",
        encoding="utf-8",
    )
    real_exists = proxy.os.path.exists

    def guarded_exists(path):
        if os.path.normpath(os.fspath(path)) == os.path.normpath(str(config_path)):
            return real_exists(path)
        raise AssertionError(f"plain UDP inspected UDPSEC path: {path}")

    monkeypatch.setattr(proxy.os.path, "exists", guarded_exists)
    monkeypatch.setattr(
        proxy.os.path,
        "lexists",
        lambda path: (_ for _ in ()).throw(
            AssertionError(f"plain UDP inspected UDPSEC path: {path}")
        ),
    )

    config = proxy.load_config(str(config_path))

    assert config["output"]["type"] == "udp"
    assert config["station_private_key"] == "operator/station.pem"
    assert config["remote_public_key"] == "trust/aismixer.pem"


def test_proxy_configured_legacy_station_private_key_still_works(tmp_path):
    proxy = load_proxy_module()
    config_path = tmp_path / "config.yaml"
    config_path.write_text("station_private_key: station_private.key\n", encoding="utf-8")

    config = proxy.load_config(str(config_path))

    assert config["station_private_key"] == str(tmp_path / "station_private.key")


def test_proxy_canonical_station_private_key_falls_back_to_legacy_sibling(
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
    legacy_path.write_bytes(b"existing legacy operator key")

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

    assert SERVER_PUBLIC_KEY_FOR_PROXY_FILENAME == "aismixer_public.pem"
    assert STATION_CANONICAL_PRIVATE_KEY_PATH.endswith("station_private.pem")
    assert STATION_PRIVATE_KEY_FILENAME == "station_private.key"
    assert STATION_PUBLIC_KEY_FILENAME == "station_public.pem"
    assert REMOTE_CANONICAL_PUBLIC_KEY_PATH.endswith("aismixer_public.pem")
    assert proxy.CANONICAL_STATION_PRIVATE_KEY_PATH == STATION_CANONICAL_PRIVATE_KEY_PATH
    assert proxy.CANONICAL_REMOTE_PUBLIC_KEY_PATH == REMOTE_CANONICAL_PUBLIC_KEY_PATH
    assert proxy.DEFAULT_CONFIG["remote_public_key"] == REMOTE_CANONICAL_PUBLIC_KEY_PATH
    assert proxy.DEFAULT_CONFIG["station_private_key"] == STATION_CANONICAL_PRIVATE_KEY_PATH


# D.6.6 server pending-session lifecycle and dispatch coverage.


def _d66_keys(marker):
    return bytes((marker,)) * 32, bytes((marker + 1,)) * 32


def _d66_install_pending(
    secure,
    state,
    addr,
    *,
    marker=40,
    now=0.0,
    station_id="boat_001",
):
    client_to_server_key, server_to_client_key = _d66_keys(marker)
    pending = state.install_pending_session(
        addr,
        station_id,
        secure.AESGCM(client_to_server_key),
        secure.AESGCM(server_to_client_key),
        now,
    )
    return pending, client_to_server_key, server_to_client_key


def _d66_confirmation_packet(
    secure,
    client_to_server_key,
    nonce,
    *,
    station_id="boat_001",
    seq=0,
    timestamp=1000,
):
    return _encrypted_control_packet(
        secure,
        client_to_server_key,
        nonce,
        {
            "type": "ping",
            "seq": seq,
            "timestamp": timestamp,
            "source_id": station_id,
        },
    )


def _d66_decrypt_json(secure, packet, key):
    nonce, ciphertext = secure.parse_secure_data_packet(packet)
    plaintext = secure.AESGCM(key).decrypt(
        nonce,
        ciphertext,
        secure.DATA_AAD,
    )
    return secure.json.loads(plaintext.decode())


def _d66_derive_client_material(
    secure,
    client_hello,
    client_ephemeral_private_key,
    server_packet,
):
    server_hello = parse_server_hello_packet(server_packet)
    server_ephemeral_public_key = secure.parse_ephemeral_public_key(
        server_hello.server_ephemeral_public_key
    )
    shared_secret = secure.derive_ephemeral_shared_secret(
        client_ephemeral_private_key,
        server_ephemeral_public_key,
    )
    transcript_hash = secure.build_session_transcript_hash(
        station_id=client_hello.station_id,
        timestamp=client_hello.timestamp,
        client_random=client_hello.client_random,
        client_ephemeral_public_key=(
            client_hello.client_ephemeral_public_key
        ),
        client_signature=client_hello.client_signature,
        server_random=server_hello.server_random,
        server_ephemeral_public_key=(
            server_hello.server_ephemeral_public_key
        ),
        server_signature=server_hello.server_signature,
    )
    return secure.derive_session_key_material(
        shared_secret,
        transcript_hash,
    )


def _d66_run_authenticated_handshake(
    monkeypatch,
    secure,
    client_identity_private_key,
    state,
    addr,
    *,
    timestamp,
    monotonic_time,
    random_marker,
    ephemeral_scalar,
):
    packet, client_hello, client_ephemeral_private_key = (
        _signed_client_hello(
            secure,
            client_identity_private_key,
            "boat_001",
            timestamp,
            client_random=bytes((random_marker,)) * 32,
            client_ephemeral_private_key=ec.derive_private_key(
                ephemeral_scalar,
                ec.SECP256R1(),
            ),
        )
    )
    queue, fake_socket = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(packet, addr)],
        state=state,
        wall_clock=_FakeClock(float(timestamp)),
        monotonic_clock=_FakeClock(monotonic_time),
    )
    assert queue.items == []
    assert len(fake_socket.sent) == 1
    assert fake_socket.sent[0][1] == addr
    server_packet = fake_socket.sent[0][0]
    server_hello = parse_server_hello_packet(server_packet)
    assert build_server_hello_packet(server_hello) == server_packet
    key_material = _d66_derive_client_material(
        secure,
        client_hello,
        client_ephemeral_private_key,
        server_packet,
    )
    return key_material, fake_socket


def test_d66_pending_representation_and_creation_stats(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    addr = ("127.0.0.1", 51001)

    pending, _, _ = _d66_install_pending(
        secure,
        state,
        addr,
        now=12.5,
    )

    assert isinstance(pending, secure._PendingSecureSession)
    assert set(vars(pending)) == {
        "_address",
        "station_id",
        "client_to_server_aesgcm",
        "server_to_client_aesgcm",
        "created_at",
        "seen_data_nonces",
    }
    assert pending._address == addr
    assert pending.station_id == "boat_001"
    assert pending.created_at == 12.5
    assert (
        pending.client_to_server_aesgcm
        is not pending.server_to_client_aesgcm
    )
    for forbidden_name in (
        "ephemeral_private_key",
        "shared_secret",
        "session_transcript_hash",
        "client_to_server_key",
        "server_to_client_key",
    ):
        assert not hasattr(pending, forbidden_name)

    stats = state.stats()
    assert state.get_active_session(addr, 12.5) is None
    assert state.get_pending_session(addr, 12.5) is pending
    assert stats.pending_sessions_created == 1
    assert stats.current_pending_sessions == 1
    assert stats.peak_pending_sessions == 1
    assert stats.sessions_created == 0
    assert stats.current_sessions == 0


def test_d66_pending_exact_ttl_prefix_cleanup_without_min(
    monkeypatch,
):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(
        pending_session_ttl=10.0,
        max_pending_sessions=3,
    )
    addresses = [
        ("127.0.0.1", 51010),
        ("127.0.0.1", 51011),
        ("127.0.0.1", 51012),
    ]
    for index, addr in enumerate(addresses):
        _d66_install_pending(
            secure,
            state,
            addr,
            marker=10 + (index * 2),
            now=float(index),
        )

    with monkeypatch.context() as patch:
        patch.setattr(
            builtins,
            "min",
            lambda *args, **kwargs: pytest.fail(
                "pending cleanup must not scan with min()"
            ),
        )
        expired = state.cleanup_expired_pending_sessions(11.0)

    assert expired == addresses[:2]
    assert tuple(state._pending_sessions) == (addresses[2],)
    assert state.cleanup_expired_pending_sessions(11.999) == []
    assert state.cleanup_expired_pending_sessions(12.0) == [
        addresses[2]
    ]
    stats = state.stats()
    assert stats.pending_sessions_expired == 3
    assert stats.pending_sessions_capacity_evicted == 0
    assert stats.current_pending_sessions == 0
    assert stats.peak_pending_sessions == 3
    assert stats.sessions_expired == 0


def test_d66_pending_capacity_is_independent_and_cleans_expired_first(
    monkeypatch,
):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(
        session_ttl=100.0,
        max_sessions=1,
        pending_session_ttl=10.0,
        max_pending_sessions=2,
    )
    active_addr = ("127.0.0.1", 51020)
    active_keys = _d66_keys(2)
    active, _, _ = _install_test_session(
        secure,
        state,
        active_addr,
        *active_keys,
        now=0.0,
    )
    pending_addresses = [
        ("127.0.0.1", 51021),
        ("127.0.0.1", 51022),
        ("127.0.0.1", 51023),
        ("127.0.0.1", 51024),
    ]
    _d66_install_pending(
        secure, state, pending_addresses[0], marker=10, now=0.0
    )
    _d66_install_pending(
        secure, state, pending_addresses[1], marker=12, now=1.0
    )

    _d66_install_pending(
        secure, state, pending_addresses[2], marker=14, now=10.0
    )
    assert tuple(state._pending_sessions) == (
        pending_addresses[1],
        pending_addresses[2],
    )
    assert state.stats().pending_sessions_expired == 1
    assert state.stats().pending_sessions_capacity_evicted == 0

    with monkeypatch.context() as patch:
        patch.setattr(
            builtins,
            "min",
            lambda *args, **kwargs: pytest.fail(
                "pending capacity eviction must use OrderedDict order"
            ),
        )
        _d66_install_pending(
            secure,
            state,
            pending_addresses[3],
            marker=16,
            now=10.5,
        )

    assert tuple(state._pending_sessions) == (
        pending_addresses[2],
        pending_addresses[3],
    )
    assert state._sessions == {active_addr: active}
    stats = state.stats()
    assert stats.pending_sessions_created == 4
    assert stats.pending_sessions_expired == 1
    assert stats.pending_sessions_capacity_evicted == 1
    assert stats.current_pending_sessions == 2
    assert stats.peak_pending_sessions == 2
    assert stats.sessions_capacity_evicted == 0
    assert stats.current_sessions == 1


def test_d66_pending_replacement_preserves_active_and_accounts_cache(
    monkeypatch,
):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    addr = ("127.0.0.1", 51030)
    active_keys = _d66_keys(2)
    active, _, _ = _install_test_session(
        secure,
        state,
        addr,
        *active_keys,
        now=0.0,
    )
    active_nonce = b"\x01" * 12
    assert state.accept_data_nonce(active, active_nonce, 0.1)
    first_pending, _, _ = _d66_install_pending(
        secure,
        state,
        addr,
        marker=20,
        now=1.0,
    )
    pending_nonce = b"\x02" * 12
    assert state.accept_pending_data_nonce(
        first_pending,
        pending_nonce,
        1.1,
    )

    second_pending, _, _ = _d66_install_pending(
        secure,
        state,
        addr,
        marker=30,
        now=2.0,
    )

    assert state._sessions[addr] is active
    assert state._pending_sessions[addr] is second_pending
    assert len(first_pending.seen_data_nonces) == 0
    assert active.seen_data_nonces.contains(active_nonce, 2.0)[0]
    stats = state.stats()
    assert stats.pending_sessions_created == 2
    assert stats.pending_sessions_replaced == 1
    assert stats.current_pending_sessions == 1
    assert stats.sessions_created == 1
    assert stats.sessions_replaced == 0
    assert stats.current_sessions == 1
    assert stats.current_data_nonces == 1
    assert stats.data_nonces_session_discarded == 1

    assert state.cleanup_expired_pending_sessions(31.999) == []
    assert state.cleanup_expired_pending_sessions(32.0) == [addr]
    assert state._sessions[addr] is active
    stats = state.stats()
    assert stats.pending_sessions_expired == 1
    assert stats.current_pending_sessions == 0
    assert stats.sessions_expired == 0
    assert stats.sessions_replaced == 0
    assert stats.current_sessions == 1
    assert stats.current_data_nonces == 1


def test_d66_stale_pending_handle_cannot_promote_or_replace_active(
    monkeypatch,
):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    addr = ("127.0.0.1", 51040)
    active_keys = _d66_keys(2)
    active, _, _ = _install_test_session(
        secure,
        state,
        addr,
        *active_keys,
        now=0.0,
    )
    stale, _, _ = _d66_install_pending(
        secure, state, addr, marker=20, now=1.0
    )
    current, _, _ = _d66_install_pending(
        secure, state, addr, marker=30, now=2.0
    )

    assert state.promote_pending_session(addr, stale, 3.0) is None
    assert state._sessions[addr] is active
    assert state._pending_sessions[addr] is current
    stats = state.stats()
    assert stats.pending_sessions_promoted == 0
    assert stats.sessions_replaced == 0
    assert stats.current_sessions == 1
    assert stats.current_pending_sessions == 1


def test_d66_promotion_transfers_nonce_cache_and_discards_old_once(
    monkeypatch,
):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    addr = ("127.0.0.1", 51050)
    active_keys = _d66_keys(2)
    old_active, _, _ = _install_test_session(
        secure,
        state,
        addr,
        *active_keys,
        now=0.0,
    )
    old_nonce = b"\x11" * 12
    assert state.accept_data_nonce(old_active, old_nonce, 0.1)
    pending, _, _ = _d66_install_pending(
        secure, state, addr, marker=20, now=1.0
    )
    confirmation_nonce = b"\x22" * 12
    assert state.accept_pending_data_nonce(
        pending,
        confirmation_nonce,
        1.1,
    )
    transferred_cache = pending.seen_data_nonces

    promoted = state.promote_pending_session(addr, pending, 2.0)

    assert promoted is state._sessions[addr]
    assert addr not in state._pending_sessions
    assert promoted.seen_data_nonces is transferred_cache
    assert promoted.seen_data_nonces.contains(
        confirmation_nonce,
        2.0,
    )[0]
    assert len(old_active.seen_data_nonces) == 0
    stats = state.stats()
    assert stats.pending_sessions_promoted == 1
    assert stats.current_pending_sessions == 0
    assert stats.sessions_created == 2
    assert stats.sessions_replaced == 1
    assert stats.current_sessions == 1
    assert stats.current_data_nonces == 1
    assert stats.data_nonces_session_discarded == 1


def test_d66_authenticated_hello_is_pending_until_exact_expiry(
    monkeypatch,
):
    secure, client_identity_private_key = load_secure_module_with_fake_keys(
        monkeypatch,
        with_client_private_key=True,
    )
    state = secure.SecureState(pending_session_ttl=30.0)
    addr = ("127.0.0.1", 51060)

    key_material, _ = _d66_run_authenticated_handshake(
        monkeypatch,
        secure,
        client_identity_private_key,
        state,
        addr,
        timestamp=1000,
        monotonic_time=100.0,
        random_marker=61,
        ephemeral_scalar=11,
    )

    pending = state._pending_sessions[addr]
    assert state.get_active_session(addr, 100.0) is None
    assert (
        pending.client_to_server_aesgcm
        is not pending.server_to_client_aesgcm
    )
    nonce = b"\x31" * 12
    plaintext = b"directional pending key check"
    ciphertext = secure.AESGCM(
        key_material.client_to_server_key
    ).encrypt(nonce, plaintext, secure.DATA_AAD)
    assert pending.client_to_server_aesgcm.decrypt(
        nonce,
        ciphertext,
        secure.DATA_AAD,
    ) == plaintext
    with pytest.raises(InvalidTag):
        pending.server_to_client_aesgcm.decrypt(
            nonce,
            ciphertext,
            secure.DATA_AAD,
        )
    for forbidden_name in (
        "ephemeral_private_key",
        "shared_secret",
        "session_transcript_hash",
    ):
        assert not hasattr(pending, forbidden_name)

    assert state.cleanup_expired_pending_sessions(129.999) == []
    assert state.cleanup_expired_pending_sessions(130.0) == [addr]
    stats = state.stats()
    assert stats.pending_sessions_created == 1
    assert stats.pending_sessions_expired == 1
    assert stats.current_pending_sessions == 0
    assert stats.sessions_created == 0
    assert stats.current_sessions == 0


def test_d66_valid_confirmation_promotes_and_sends_directional_pong(
    monkeypatch,
):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    addr = ("127.0.0.1", 51070)
    pending, client_to_server_key, server_to_client_key = (
        _d66_install_pending(
            secure,
            state,
            addr,
            marker=40,
            now=0.0,
        )
    )
    nonce = b"\x41" * 12
    packet = _d66_confirmation_packet(
        secure,
        client_to_server_key,
        nonce,
        timestamp=1000,
    )

    queue, fake_socket = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(packet, addr)],
        state=state,
        wall_clock=_FakeClock(1001.0),
        monotonic_clock=_FakeClock(1.0),
    )

    assert queue.items == []
    assert addr not in state._pending_sessions
    active = state._sessions[addr]
    assert active.seen_data_nonces is pending.seen_data_nonces
    assert active.seen_data_nonces.contains(nonce, 1.0)[0]
    assert active.last_seen == 1.0
    assert len(fake_socket.sent) == 1
    response_packet, response_addr = fake_socket.sent[0]
    assert response_addr == addr
    assert _d66_decrypt_json(
        secure,
        response_packet,
        server_to_client_key,
    ) == {
        "type": "pong",
        "seq": secure.SESSION_CONFIRMATION_SEQUENCE,
        "timestamp": 1001,
        "source_id": "boat_001",
    }
    with pytest.raises(InvalidTag):
        _d66_decrypt_json(
            secure,
            response_packet,
            client_to_server_key,
        )

    stats = state.stats()
    assert stats.pending_sessions_promoted == 1
    assert stats.current_pending_sessions == 0
    assert stats.sessions_created == 1
    assert stats.sessions_touched == 1
    assert stats.current_sessions == 1
    assert stats.data_nonces_accepted == 1
    assert stats.current_data_nonces == 1


def test_d66_duplicate_confirmation_nonce_promotes_and_responds_once(
    monkeypatch,
):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    addr = ("127.0.0.1", 51071)
    _, client_to_server_key, _ = _d66_install_pending(
        secure,
        state,
        addr,
        marker=40,
        now=0.0,
    )
    nonce = b"\x42" * 12
    packet = _d66_confirmation_packet(
        secure,
        client_to_server_key,
        nonce,
    )

    queue, fake_socket = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(packet, addr), (packet, addr)],
        state=state,
        monotonic_clock=_FakeClock(1.0),
    )

    assert queue.items == []
    assert len(fake_socket.sent) == 1
    assert state.stats().pending_sessions_promoted == 1
    assert state.stats().sessions_created == 1
    assert state.stats().data_nonces_accepted == 1
    assert state.stats().data_nonce_replays == 1
    assert state.stats().current_data_nonces == 1


def test_d66_active_session_rejects_fresh_reserved_sequence_zero(
    monkeypatch,
):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    addr = ("127.0.0.1", 51073)
    client_to_server_key, server_to_client_key = _d66_keys(2)
    active, _, _ = _install_test_session(
        secure,
        state,
        addr,
        client_to_server_key,
        server_to_client_key,
        now=0.0,
    )
    nonce = b"\x4f" * 12
    packet = _d66_confirmation_packet(
        secure,
        client_to_server_key,
        nonce,
    )

    queue, fake_socket = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(packet, addr)],
        state=state,
        monotonic_clock=_FakeClock(1.0),
    )

    assert queue.items == []
    assert fake_socket.sent == []
    assert state._sessions[addr] is active
    assert active.last_seen == 0.0
    assert not active.seen_data_nonces.contains(nonce, 1.0)[0]
    assert state.stats().sessions_touched == 0
    assert state.stats().data_nonces_accepted == 0


@pytest.mark.parametrize(
    "case",
    (
        "wrong-key",
        "malformed-json",
        "non-dict-json",
        "wrong-station",
        "wrong-seq",
        "false-seq",
        "missing-timestamp",
        "nmea",
        "unknown-type",
    ),
)
def test_d66_invalid_pending_packets_do_not_promote_or_consume_nonce(
    monkeypatch,
    case,
):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    addr = ("127.0.0.1", 51072)
    pending, client_to_server_key, _ = _d66_install_pending(
        secure,
        state,
        addr,
        marker=40,
        now=0.0,
    )
    nonce = b"\x43" * 12

    if case == "wrong-key":
        packet = _d66_confirmation_packet(
            secure,
            bytes((99,)) * 32,
            nonce,
        )
    elif case == "malformed-json":
        ciphertext = secure.AESGCM(client_to_server_key).encrypt(
            nonce,
            b"{not-json",
            secure.DATA_AAD,
        )
        packet = secure.DATA_PREFIX + nonce + ciphertext
    elif case == "non-dict-json":
        packet = _encrypted_control_packet(
            secure,
            client_to_server_key,
            nonce,
            ["ping", secure.SESSION_CONFIRMATION_SEQUENCE],
        )
    elif case == "wrong-station":
        packet = _d66_confirmation_packet(
            secure,
            client_to_server_key,
            nonce,
            station_id="other_station",
        )
    elif case == "wrong-seq":
        packet = _d66_confirmation_packet(
            secure,
            client_to_server_key,
            nonce,
            seq=1,
        )
    elif case == "false-seq":
        packet = _d66_confirmation_packet(
            secure,
            client_to_server_key,
            nonce,
            seq=False,
        )
    elif case == "missing-timestamp":
        packet = _encrypted_control_packet(
            secure,
            client_to_server_key,
            nonce,
            {
                "type": "ping",
                "seq": secure.SESSION_CONFIRMATION_SEQUENCE,
                "source_id": "boat_001",
            },
        )
    elif case == "nmea":
        packet = _encrypted_data_packet(
            secure,
            client_to_server_key,
            nonce,
        )
    else:
        packet = _encrypted_control_packet(
            secure,
            client_to_server_key,
            nonce,
            {
                "type": "status",
                "seq": secure.SESSION_CONFIRMATION_SEQUENCE,
                "timestamp": 1000,
                "source_id": "boat_001",
            },
        )

    queue, fake_socket = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(packet, addr)],
        state=state,
        monotonic_clock=_FakeClock(1.0),
    )

    assert queue.items == []
    assert fake_socket.sent == []
    assert state._pending_sessions[addr] is pending
    assert state.get_active_session(addr, 1.0) is None
    assert not pending.seen_data_nonces.contains(nonce, 1.0)[0]
    stats = state.stats()
    assert stats.pending_sessions_promoted == 0
    assert stats.current_pending_sessions == 1
    assert stats.sessions_created == 0
    assert stats.current_sessions == 0
    assert stats.data_nonces_accepted == 0
    assert stats.current_data_nonces == 0


def test_d66_pending_auth_failure_and_seen_nonce_fall_back_to_active(
    monkeypatch,
):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    addr = ("127.0.0.1", 51080)
    active_client_key, active_server_key = _d66_keys(2)
    active, _, _ = _install_test_session(
        secure,
        state,
        addr,
        active_client_key,
        active_server_key,
        now=0.0,
    )
    pending, _, _ = _d66_install_pending(
        secure,
        state,
        addr,
        marker=40,
        now=0.5,
    )
    shared_nonce = b"\x51" * 12
    assert state.accept_pending_data_nonce(
        pending,
        shared_nonce,
        0.5,
    )
    first_nonce = b"\x50" * 12
    ping_nonce = b"\x52" * 12
    packets = [
        (
            _encrypted_data_packet(
                secure,
                active_client_key,
                first_nonce,
                payload="!AIVDM,1,1,,A,first,0*00",
            ),
            addr,
        ),
        (
            _encrypted_data_packet(
                secure,
                active_client_key,
                shared_nonce,
                payload="!AIVDM,1,1,,A,second,0*00",
            ),
            addr,
        ),
        (
            _encrypted_control_packet(
                secure,
                active_client_key,
                ping_nonce,
                {
                    "type": "ping",
                    "seq": 1,
                    "timestamp": 1000,
                    "source_id": "boat_001",
                },
            ),
            addr,
        ),
    ]
    parse_calls = []
    original_parse_secure_data_packet = secure.parse_secure_data_packet

    def record_parse_secure_data_packet(data):
        parse_calls.append(data)
        return original_parse_secure_data_packet(data)

    monkeypatch.setattr(
        secure,
        "parse_secure_data_packet",
        record_parse_secure_data_packet,
    )

    queue, fake_socket = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        packets,
        state=state,
        wall_clock=_FakeClock(1001.0),
        monotonic_clock=_FakeClock(1.0),
    )

    assert parse_calls == [packet for packet, _ in packets]
    assert len(queue.items) == 2
    assert state._sessions[addr] is active
    assert state._pending_sessions[addr] is pending
    assert active.last_seen == 1.0
    assert pending.seen_data_nonces.contains(shared_nonce, 1.0)[0]
    assert active.seen_data_nonces.contains(shared_nonce, 1.0)[0]
    assert len(fake_socket.sent) == 1
    assert _d66_decrypt_json(
        secure,
        fake_socket.sent[0][0],
        active_server_key,
    ) == {
        "type": "pong",
        "seq": 1,
        "timestamp": 1001,
        "source_id": "boat_001",
    }
    assert state.stats().current_sessions == 1
    assert state.stats().current_pending_sessions == 1
    assert state.stats().data_nonce_replays == 0


def test_d66_authenticated_invalid_pending_plaintext_is_exclusive(
    monkeypatch,
):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    addr = ("127.0.0.1", 51081)
    shared_client_key, active_server_key = _d66_keys(2)
    active, _, _ = _install_test_session(
        secure,
        state,
        addr,
        shared_client_key,
        active_server_key,
        now=0.0,
    )
    pending_server_key = bytes((30,)) * 32
    pending = state.install_pending_session(
        addr,
        "boat_001",
        secure.AESGCM(shared_client_key),
        secure.AESGCM(pending_server_key),
        0.5,
    )
    nonce = b"\x53" * 12
    packet = _encrypted_data_packet(
        secure,
        shared_client_key,
        nonce,
        payload="!AIVDM,1,1,,A,must-not-queue,0*00",
    )

    queue, fake_socket = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(packet, addr)],
        state=state,
        monotonic_clock=_FakeClock(1.0),
    )

    assert queue.items == []
    assert fake_socket.sent == []
    assert state._sessions[addr] is active
    assert state._pending_sessions[addr] is pending
    assert active.last_seen == 0.0
    assert not active.seen_data_nonces.contains(nonce, 1.0)[0]
    assert not pending.seen_data_nonces.contains(nonce, 1.0)[0]
    assert state.stats().sessions_touched == 0
    assert state.stats().data_nonces_accepted == 0


def test_d66_only_invalid_tag_selects_active_key_fallback(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    addr = ("127.0.0.1", 51083)
    active_client_key, active_server_key = _d66_keys(2)
    active, _, _ = _install_test_session(
        secure,
        state,
        addr,
        active_client_key,
        active_server_key,
        now=0.0,
    )

    class UnexpectedDecryptFailure:
        def decrypt(self, nonce, ciphertext, aad):
            raise RuntimeError("unexpected pending decrypt failure")

    pending = state.install_pending_session(
        addr,
        "boat_001",
        UnexpectedDecryptFailure(),
        secure.AESGCM(bytes((30,)) * 32),
        0.5,
    )
    nonce = b"\x54" * 12
    packet = _encrypted_data_packet(
        secure,
        active_client_key,
        nonce,
        payload="!AIVDM,1,1,,A,must-not-fallback,0*00",
    )

    queue, fake_socket = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(packet, addr)],
        state=state,
        monotonic_clock=_FakeClock(1.0),
    )

    assert queue.items == []
    assert fake_socket.sent == []
    assert state._sessions[addr] is active
    assert state._pending_sessions[addr] is pending
    assert active.last_seen == 0.0
    assert not active.seen_data_nonces.contains(nonce, 1.0)[0]
    assert not pending.seen_data_nonces.contains(nonce, 1.0)[0]
    assert state.stats().sessions_touched == 0
    assert state.stats().data_nonces_accepted == 0


def test_d66_plaintext_keepalive_is_absent_and_silent(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    addr = ("127.0.0.1", 51082)
    active_keys = _d66_keys(2)
    active, _, _ = _install_test_session(
        secure,
        state,
        addr,
        *active_keys,
        now=0.0,
    )
    pending, _, _ = _d66_install_pending(
        secure,
        state,
        addr,
        marker=40,
        now=0.5,
    )

    queue, fake_socket = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(b"KEEPALIVE|boat_001|1000", addr)],
        state=state,
        monotonic_clock=_FakeClock(1.0),
    )

    assert not hasattr(secure, "KEEPALIVE_PREFIX")
    assert not hasattr(secure, "parse_keepalive_packet")
    assert not hasattr(secure, "parse_keepalive_station_id")
    assert not hasattr(state, "handle_keepalive")
    assert queue.items == []
    assert fake_socket.sent == []
    assert state._sessions[addr] is active
    assert state._pending_sessions[addr] is pending
    assert active.last_seen == 0.0
    assert state.stats().sessions_touched == 0
    assert state.stats().pending_sessions_promoted == 0


def test_d66_same_address_confirmed_rekey_preserves_then_replaces_active(
    monkeypatch,
):
    secure, client_identity_private_key = load_secure_module_with_fake_keys(
        monkeypatch,
        with_client_private_key=True,
    )
    state = secure.SecureState(
        session_ttl=500.0,
        pending_session_ttl=30.0,
    )
    addr = ("127.0.0.1", 51090)

    first_keys, _ = _d66_run_authenticated_handshake(
        monkeypatch,
        secure,
        client_identity_private_key,
        state,
        addr,
        timestamp=1000,
        monotonic_time=0.0,
        random_marker=71,
        ephemeral_scalar=21,
    )
    first_confirmation_nonce = b"\x61" * 12
    first_confirmation = _d66_confirmation_packet(
        secure,
        first_keys.client_to_server_key,
        first_confirmation_nonce,
        timestamp=1000,
    )
    first_queue, first_confirmation_socket = (
        _run_secure_server_with_packets(
            monkeypatch,
            secure,
            [(first_confirmation, addr)],
            state=state,
            wall_clock=_FakeClock(1001.0),
            monotonic_clock=_FakeClock(1.0),
        )
    )
    assert first_queue.items == []
    assert _d66_decrypt_json(
        secure,
        first_confirmation_socket.sent[0][0],
        first_keys.server_to_client_key,
    )["seq"] == secure.SESSION_CONFIRMATION_SEQUENCE
    first_active = state._sessions[addr]
    first_cache = first_active.seen_data_nonces

    # Treat the first pong as lost: the server is active, and a later fresh
    # handshake must retain it while installing the next pending candidate.
    second_keys, _ = _d66_run_authenticated_handshake(
        monkeypatch,
        secure,
        client_identity_private_key,
        state,
        addr,
        timestamp=1002,
        monotonic_time=2.0,
        random_marker=72,
        ephemeral_scalar=22,
    )
    assert second_keys.client_to_server_key != (
        first_keys.client_to_server_key
    )
    assert second_keys.server_to_client_key != (
        first_keys.server_to_client_key
    )
    assert state._sessions[addr] is first_active
    second_pending = state._pending_sessions[addr]

    old_data_nonce = b"\x62" * 12
    old_ping_nonce = b"\x63" * 12
    old_packets = [
        (
            _encrypted_data_packet(
                secure,
                first_keys.client_to_server_key,
                old_data_nonce,
                payload="!AIVDM,1,1,,A,old-active,0*00",
            ),
            addr,
        ),
        (
            _encrypted_control_packet(
                secure,
                first_keys.client_to_server_key,
                old_ping_nonce,
                {
                    "type": "ping",
                    "seq": 1,
                    "timestamp": 1003,
                    "source_id": "boat_001",
                },
            ),
            addr,
        ),
    ]
    old_queue, old_socket = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        old_packets,
        state=state,
        wall_clock=_FakeClock(1003.0),
        monotonic_clock=_FakeClock(3.0),
    )
    assert len(old_queue.items) == 1
    assert len(old_socket.sent) == 1
    assert _d66_decrypt_json(
        secure,
        old_socket.sent[0][0],
        first_keys.server_to_client_key,
    )["seq"] == 1
    assert state._sessions[addr] is first_active
    assert state._pending_sessions[addr] is second_pending
    assert len(first_cache) == 3

    second_confirmation_nonce = b"\x64" * 12
    second_confirmation = _d66_confirmation_packet(
        secure,
        second_keys.client_to_server_key,
        second_confirmation_nonce,
        timestamp=1004,
    )
    second_queue, second_confirmation_socket = (
        _run_secure_server_with_packets(
            monkeypatch,
            secure,
            [(second_confirmation, addr)],
            state=state,
            wall_clock=_FakeClock(1004.0),
            monotonic_clock=_FakeClock(4.0),
        )
    )
    assert second_queue.items == []
    assert _d66_decrypt_json(
        secure,
        second_confirmation_socket.sent[0][0],
        second_keys.server_to_client_key,
    )["seq"] == secure.SESSION_CONFIRMATION_SEQUENCE
    second_active = state._sessions[addr]
    assert second_active is not first_active
    assert addr not in state._pending_sessions
    assert second_active.seen_data_nonces is (
        second_pending.seen_data_nonces
    )
    assert second_active.seen_data_nonces.contains(
        second_confirmation_nonce,
        4.0,
    )[0]
    assert len(first_cache) == 0

    late_old_nonce = b"\x65" * 12
    new_data_nonce = b"\x66" * 12
    new_ping_nonce = b"\x67" * 12
    post_promotion_packets = [
        (
            _encrypted_data_packet(
                secure,
                first_keys.client_to_server_key,
                late_old_nonce,
                payload="!AIVDM,1,1,,A,late-old,0*00",
            ),
            addr,
        ),
        (
            _encrypted_data_packet(
                secure,
                second_keys.client_to_server_key,
                new_data_nonce,
                payload="!AIVDM,1,1,,A,new-active,0*00",
            ),
            addr,
        ),
        (
            _encrypted_control_packet(
                secure,
                second_keys.client_to_server_key,
                new_ping_nonce,
                {
                    "type": "ping",
                    "seq": 1,
                    "timestamp": 1005,
                    "source_id": "boat_001",
                },
            ),
            addr,
        ),
    ]
    new_queue, new_socket = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        post_promotion_packets,
        state=state,
        wall_clock=_FakeClock(1005.0),
        monotonic_clock=_FakeClock(5.0),
    )

    assert len(new_queue.items) == 1
    assert len(new_socket.sent) == 1
    assert _d66_decrypt_json(
        secure,
        new_socket.sent[0][0],
        second_keys.server_to_client_key,
    )["seq"] == 1
    with pytest.raises(InvalidTag):
        _d66_decrypt_json(
            secure,
            new_socket.sent[0][0],
            first_keys.server_to_client_key,
        )
    assert not second_active.seen_data_nonces.contains(
        late_old_nonce,
        5.0,
    )[0]
    assert second_active.seen_data_nonces.contains(
        new_data_nonce,
        5.0,
    )[0]
    assert second_active.seen_data_nonces.contains(
        new_ping_nonce,
        5.0,
    )[0]

    stats = state.stats()
    assert stats.pending_sessions_created == 2
    assert stats.pending_sessions_promoted == 2
    assert stats.current_pending_sessions == 0
    assert stats.sessions_created == 2
    assert stats.sessions_replaced == 1
    assert stats.current_sessions == 1
    assert stats.data_nonces_session_discarded == 3
    assert stats.current_data_nonces == 3
