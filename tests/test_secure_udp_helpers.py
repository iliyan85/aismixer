import asyncio
import base64
import builtins
import hashlib
import importlib.util
import io
import itertools
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
_TEST_ENDPOINT_TOKENS = {}


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
    extra_stations=None,
):
    """Load a fresh, independently-imported `aismixer_secure` module with a
    fake `authorized_keys.yaml`.

    By default only ``boat_001`` is authorized, preserving every existing
    call site's exact two-shape return (`module`, or `(module,
    client_private_key)`). Pass `extra_stations` (an iterable of station-id
    strings) to additionally authorize one freshly generated EC identity
    per name -- needed for tests that must authenticate two genuinely
    different stations (for example, proving independent SecureState
    owners never cross-assemble unrelated stations' multipart traffic).
    When `extra_stations` is given, the extra station's private key(s) are
    returned as an additional `{name: private_key}` dict.
    """
    client_private_key = ec.generate_private_key(ec.SECP256R1())
    client_public_bytes = client_private_key.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.CompressedPoint,
    )
    authorized_entries = [("boat_001", client_public_bytes)]
    extra_private_keys = {}
    for name in extra_stations or ():
        extra_key = ec.generate_private_key(ec.SECP256R1())
        extra_private_keys[name] = extra_key
        extra_public_bytes = extra_key.public_key().public_bytes(
            encoding=serialization.Encoding.X962,
            format=serialization.PublicFormat.CompressedPoint,
        )
        authorized_entries.append((name, extra_public_bytes))

    authorized_yaml = "authorized_clients:\n" + "".join(
        f"  - name: {name}\n"
        f"    pubkey: {base64.b64encode(public_bytes).decode()}\n"
        for name, public_bytes in authorized_entries
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
        if extra_stations:
            if with_client_private_key:
                return module, client_private_key, extra_private_keys
            return module, extra_private_keys
        if with_client_private_key:
            return module, client_private_key
        return module


def _test_endpoint_token(secure):
    token = _TEST_ENDPOINT_TOKENS.get(secure)
    if token is None:
        token = secure._EndpointToken()
        _TEST_ENDPOINT_TOKENS[secure] = token
    return token


def _relation_key(secure, address, endpoint_token=None):
    token = (
        _test_endpoint_token(secure)
        if endpoint_token is None
        else endpoint_token
    )
    return secure._EndpointPeerKey(token, address)


def _relation_keys(secure, addresses, endpoint_token=None):
    return tuple(
        _relation_key(secure, address, endpoint_token)
        for address in addresses
    )


def _active_session_for_relation_key(state, relation_key):
    """The current active session at one relation, via the relation index.

    The active-session store is keyed by ``_EndpointSessionKey`` (endpoint
    token + session locator), not by relation, so tests that need "the
    session currently active at this endpoint/address" go through the
    bounded secondary relation index exactly like production code does for
    same-relation replacement detection. Delegates to the production
    `_active_session_at_relation` method (rather than indexing
    `_relation_index` directly) so this helper automatically follows the
    same IPv6-flowinfo canonicalization production code applies -- a raw
    dict lookup here would disagree with `_structured_paths_match` for a
    relation whose flowinfo changed but whose (ip, port, scope_id) did not.
    """

    return state._active_session_at_relation(relation_key)


def _active_session_at(secure, state, address, endpoint_token=None):
    return _active_session_for_relation_key(
        state, _relation_key(secure, address, endpoint_token)
    )


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
                endpoint_token=_test_endpoint_token(secure),
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


def test_obsolete_plaintext_session_reset_symbols_are_removed(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    proxy = load_proxy_module()

    assert not hasattr(secure, "NOSESSION_PREFIX")
    assert not hasattr(secure, "build_no_session_hint")
    assert not hasattr(proxy, "NOSESSION_PREFIX")
    assert not hasattr(proxy, "SESSION_END_NOSESSION")
    assert not hasattr(proxy, "SERVER_PACKET_NO_SESSION")
    assert not hasattr(proxy, "is_no_session_hint")


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
        "protocol_version": secure.UDPSEC_PROTOCOL_VERSION,
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
    protocol_version=None,
):
    if client_random is None:
        client_random = b"\x11" * 32
    if client_ephemeral_private_key is None:
        client_ephemeral_private_key = ec.derive_private_key(
            2,
            ec.SECP256R1(),
        )
    if protocol_version is None:
        protocol_version = secure.UDPSEC_PROTOCOL_VERSION
    client_ephemeral_public_bytes = (
        secure.serialize_ephemeral_public_key(
            client_ephemeral_private_key.public_key()
        )
    )
    client_auth_digest = secure.build_client_auth_digest(
        protocol_version=protocol_version,
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
        protocol_version=protocol_version,
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
    session_locator,
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
        secure.build_data_aad(session_locator),
    )
    return secure.build_data_packet(session_locator, nonce, ciphertext)


def _encrypted_control_packet(
    secure,
    client_to_server_key,
    nonce,
    message,
    session_locator,
):
    plaintext = secure.json.dumps(message).encode()
    ciphertext = secure.AESGCM(client_to_server_key).encrypt(
        nonce,
        plaintext,
        secure.build_data_aad(session_locator),
    )
    return secure.build_data_packet(session_locator, nonce, ciphertext)


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
    graceful_shutdown=False,
    owned_sessions=None,
    owned_pending_sessions=None,
    endpoint_token=None,
    debug=False,
):
    fake_socket = _FakeSecureSocket()
    fake_loop = _FakeSecureLoop(packets)
    fake_queue = _FakeQueue()

    state = secure.SecureState() if state is None else state
    endpoint_token = (
        _test_endpoint_token(secure)
        if endpoint_token is None
        else endpoint_token
    )
    wall_clock = _FakeClock(1010.0) if wall_clock is None else wall_clock
    monotonic_clock = (
        _FakeClock(1010.0)
        if monotonic_clock is None
        else monotonic_clock
    )
    if owned_sessions is None:
        owned_sessions = dict(state._sessions)
    if owned_pending_sessions is None:
        owned_pending_sessions = dict(state._pending_sessions)
    monkeypatch.setattr(secure, "asyncio", _FakeAsyncioModule(fake_loop))
    try:
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(
                secure._secure_server_loop(
                    fake_socket,
                    fake_queue,
                    "127.0.0.1",
                    9999,
                    sec_input_id=sec_input_id,
                    ingress_policy=ingress_policy,
                    endpoint_token=endpoint_token,
                    debug=debug,
                    state=state,
                    wall_clock=wall_clock,
                    monotonic_clock=monotonic_clock,
                    server_private_key=server_private_key,
                    owned_sessions=owned_sessions,
                    owned_pending_sessions=owned_pending_sessions,
                )
            )
    finally:
        if graceful_shutdown:
            secure.close_owned_sessions(
                fake_socket,
                state,
                owned_sessions,
                wall_clock=wall_clock,
                monotonic_clock=monotonic_clock,
            )
        fake_socket.close()

    assert fake_socket.close_count == 1
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


_TEST_LOCATOR_COUNTER = itertools.count(1)


def _fresh_test_locator():
    # A monotonic counter, not os.urandom: several tests monkeypatch
    # os.urandom (client ephemeral/nonce generation, server locator
    # minting) and must not have their call counts or return-value
    # sequencing perturbed by test-harness locator bookkeeping.
    return next(_TEST_LOCATOR_COUNTER).to_bytes(16, "big")


def _install_test_session(
    secure,
    state,
    addr,
    client_to_server_key,
    server_to_client_key,
    now=1000.0,
    station_id="boat_001",
    endpoint_token=None,
    session_locator=None,
):
    client_to_server_aesgcm = secure.AESGCM(client_to_server_key)
    server_to_client_aesgcm = secure.AESGCM(server_to_client_key)
    if session_locator is None:
        session_locator = _fresh_test_locator()
    session = state.install_session(
        _relation_key(secure, addr, endpoint_token),
        station_id,
        session_locator,
        client_to_server_aesgcm,
        server_to_client_aesgcm,
        now)
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
        protocol_version=client_hello.protocol_version,
        station_id=client_hello.station_id,
        timestamp=client_hello.timestamp,
        client_random=client_hello.client_random,
        client_ephemeral_public_key=(
            client_hello.client_ephemeral_public_key
        ),
        client_signature=client_hello.client_signature,
        session_locator=server_hello.session_locator,
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
        protocol_version=client_hello.protocol_version,
        station_id=client_hello.station_id,
        timestamp=client_hello.timestamp,
        client_random=client_hello.client_random,
        client_ephemeral_public_key=(
            client_hello.client_ephemeral_public_key
        ),
        client_signature=client_hello.client_signature,
        session_locator=server_hello.session_locator,
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
    pending = state._pending_sessions[_relation_key(secure, addr)]

    nonce = b"\x01" * 12
    plaintext = b"direction check"
    data_aad = secure.build_data_aad(pending.session_locator)
    ciphertext = secure.AESGCM(
        client_key_material.client_to_server_key
    ).encrypt(nonce, plaintext, data_aad)
    assert pending.current_epoch.client_to_server_aesgcm.decrypt(
        nonce,
        ciphertext,
        data_aad,
    ) == plaintext
    assert (
        pending.current_epoch.client_to_server_aesgcm
        is not pending.current_epoch.server_to_client_aesgcm
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
    assert tuple(state._pending_sessions) == _relation_keys(
        secure, (first_addr,)
    )
    assert state.stats().handshake_replay_accepted == 1
    assert state.stats().handshake_replay_rejected == 1
    assert state.stats().pending_sessions_created == 1


def test_secure_server_handshake_replay_cache_spans_endpoint_namespaces(
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
    peer = ("127.0.0.1", 50123)
    endpoint_a = secure._new_endpoint_token()
    endpoint_b = secure._new_endpoint_token()
    relation_a = _relation_key(secure, peer, endpoint_a)
    relation_b = _relation_key(secure, peer, endpoint_b)
    state = secure.SecureState()

    _, first_socket = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(packet, peer)],
        state=state,
        wall_clock=_FakeClock(1000.0),
        monotonic_clock=_FakeClock(10.0),
        endpoint_token=endpoint_a,
        owned_sessions={},
        owned_pending_sessions={},
    )
    pending_a = state._pending_sessions[relation_a]

    _, second_socket = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(packet, peer)],
        state=state,
        wall_clock=_FakeClock(1000.0),
        monotonic_clock=_FakeClock(10.0),
        endpoint_token=endpoint_b,
        owned_sessions={},
        owned_pending_sessions={},
    )

    assert len(first_socket.sent) == 1
    assert first_socket.sent[0][1] == peer
    assert second_socket.sent == []
    assert state._pending_sessions == {relation_a: pending_a}
    assert relation_b not in state._pending_sessions
    stats = state.stats()
    assert stats.handshake_replay_accepted == 1
    assert stats.handshake_replay_rejected == 1
    assert stats.pending_sessions_created == 1


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
            protocol_version=valid_hello.protocol_version,
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
        protocol_version=secure.UDPSEC_PROTOCOL_VERSION,
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
            protocol_version=secure.UDPSEC_PROTOCOL_VERSION,
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
        "protocol_version": client_hello.protocol_version,
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
    # Each successful ClientHello triggers exactly two os.urandom calls, in
    # order: the fresh 16-byte session locator, then the 32-byte
    # server_random. Two hellos therefore need four pre-sized values.
    random_values = iter(
        (b"\x40" * 16, b"\x41" * 32, b"\x43" * 16, b"\x42" * 32)
    )
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
    assert tuple(state._pending_sessions) == _relation_keys(secure, (addr,))
    assert state.stats().pending_sessions_created == 2
    assert state.stats().pending_sessions_replaced == 1
    assert state.stats().current_pending_sessions == 1


def test_secure_server_silently_drops_data_without_session(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    addr = ("127.0.0.1", 50123)
    packet = secure.build_data_packet(
        _fresh_test_locator(), b"\x00" * 12, b"\x00" * 16
    )
    signing_calls = []

    def fail_if_signing_runs(*args, **kwargs):
        signing_calls.append((args, kwargs))
        raise AssertionError("unknown secure data must not trigger signing")

    monkeypatch.setattr(
        secure,
        "sign_transcript_digest",
        fail_if_signing_runs,
    )

    fake_queue, fake_socket = _run_secure_server_with_packets(
        monkeypatch, secure, [(packet, addr)]
    )

    assert fake_queue.items == []
    assert fake_socket.sent == []
    assert signing_calls == []


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
        _relation_key(secure, addr),
        "boat_001",
        _fresh_test_locator(),
        object(),
        object(),
        now=1000.0)
    pending = state.install_pending_session(
        _relation_key(secure, addr),
        "boat_001",
        _fresh_test_locator(),
        object(),
        object(),
        now=1005.0)
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
    assert _active_session_at(secure, state, addr) is active
    assert state._pending_sessions[_relation_key(secure, addr)] is pending
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
        session._session_key.session_locator,
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
    response_locator, response_nonce, ciphertext = secure.parse_data_packet(
        response
    )
    assert response_locator == session._session_key.session_locator
    pong = secure.json.loads(
        secure.AESGCM(server_to_client_key).decrypt(
            response_nonce,
            ciphertext,
            secure.build_data_aad(response_locator),
        ).decode()
    )
    with pytest.raises(InvalidTag):
        secure.AESGCM(client_to_server_key).decrypt(
            response_nonce,
            ciphertext,
            secure.build_data_aad(response_locator),
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


def test_secure_server_valid_client_close_removes_exact_owned_active_session(
    monkeypatch,
):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    client_to_server_key = b"\x01" * 32
    server_to_client_key = b"\x02" * 32
    addr = ("127.0.0.1", 50123)
    nonce = b"\x30" * 12
    state = secure.SecureState()
    session, _, _ = _install_test_session(
        secure,
        state,
        addr,
        client_to_server_key,
        server_to_client_key,
    )
    pending = state.install_pending_session(
        _relation_key(secure, addr),
        "boat_001",
        _fresh_test_locator(),
        secure.AESGCM(b"\x03" * 32),
        secure.AESGCM(b"\x04" * 32),
        now=1005.0)
    packet = _encrypted_control_packet(
        secure,
        client_to_server_key,
        nonce,
        secure.build_session_close_message("boat_001", 1000),
        session._session_key.session_locator,
    )

    fake_queue, fake_socket = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(packet, addr)],
        state=state,
        monotonic_clock=_FakeClock(1010.0),
        owned_sessions={session._session_key: session},
    )

    stats = state.stats()
    assert fake_queue.items == []
    assert fake_socket.sent == []
    assert _active_session_at(secure, state, addr) is None
    assert state._pending_sessions[_relation_key(secure, addr)] is pending
    assert len(session.current_epoch.seen_data_nonces) == 0
    assert stats.sessions_closed == 1
    assert stats.sessions_touched == 0
    assert stats.current_sessions == 0
    assert stats.current_pending_sessions == 1
    assert stats.data_nonces_accepted == 1
    assert stats.data_nonces_session_discarded == 1
    assert stats.current_data_nonces == 0


@pytest.mark.parametrize(
    "case",
    ("plaintext", "wrong-key", "wrong-source", "malformed-shape"),
)
def test_secure_server_forged_client_close_cannot_remove_active_session(
    monkeypatch,
    case,
):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    client_to_server_key = b"\x11" * 32
    server_to_client_key = b"\x12" * 32
    addr = ("127.0.0.1", 50123)
    nonce = b"\x31" * 12
    state = secure.SecureState()
    session, _, _ = _install_test_session(
        secure,
        state,
        addr,
        client_to_server_key,
        server_to_client_key,
    )

    if case == "plaintext":
        packet = b'{"type":"close","reason":"shutdown"}'
    else:
        message = secure.build_session_close_message("boat_001", 1000)
        packet_key = client_to_server_key
        if case == "wrong-key":
            packet_key = b"\x13" * 32
        elif case == "wrong-source":
            message["source_id"] = "other_station"
        else:
            del message["timestamp"]
        packet = _encrypted_control_packet(
            secure,
            packet_key,
            nonce,
            message,
            session._session_key.session_locator,
        )

    fake_queue, fake_socket = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(packet, addr)],
        state=state,
        monotonic_clock=_FakeClock(1010.0),
        owned_sessions={session._session_key: session},
    )

    stats = state.stats()
    assert fake_queue.items == []
    assert fake_socket.sent == []
    assert _active_session_at(secure, state, addr) is session
    assert len(session.current_epoch.seen_data_nonces) == 0
    assert session.last_seen == 1000.0
    assert stats.sessions_closed == 0
    assert stats.sessions_touched == 0
    assert stats.data_nonces_accepted == 0
    assert stats.current_data_nonces == 0


def test_secure_server_valid_client_close_requires_exact_listener_owner(
    monkeypatch,
):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    addr = ("127.0.0.1", 50123)
    old_client_key = b"\x21" * 32
    current_client_key = b"\x22" * 32
    state = secure.SecureState()
    stale, _, _ = _install_test_session(
        secure,
        state,
        addr,
        old_client_key,
        b"\x23" * 32,
        now=999.0,
    )
    current, _, _ = _install_test_session(
        secure,
        state,
        addr,
        current_client_key,
        b"\x24" * 32,
        now=1000.0,
    )
    packet = _encrypted_control_packet(
        secure,
        current_client_key,
        b"\x32" * 12,
        secure.build_session_close_message("boat_001", 1000),
        current._session_key.session_locator,
    )

    fake_queue, fake_socket = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(packet, addr)],
        state=state,
        monotonic_clock=_FakeClock(1010.0),
        owned_sessions={stale._session_key: stale},
    )

    stats = state.stats()
    assert fake_queue.items == []
    assert fake_socket.sent == []
    assert _active_session_at(secure, state, addr) is current
    assert len(current.current_epoch.seen_data_nonces) == 0
    assert current.last_seen == 1000.0
    assert stats.sessions_replaced == 1
    assert stats.sessions_closed == 0
    assert stats.sessions_touched == 0
    assert stats.data_nonces_accepted == 0


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
        session._session_key.session_locator,
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
    assert frame.assembler_key == (
        f"udpsec-assembly:{session.assembly_namespace.hex()}"
    )
    assert frame.payload == b"!AIVDM,1,1,,A,payload,0*00"
    assert frame.text_mode is PayloadTextMode.UTF8_SURROGATEPASS
    assert (
        decode_frame_slice(frame, 0, len(frame.payload))
        == "!AIVDM,1,1,,A,payload,0*00"
    )
    assert session.last_seen == 1010.0
    assert len(session.current_epoch.seen_data_nonces) == 1
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
        session._session_key.session_locator,
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


def test_secure_server_denied_data_peer_gets_no_response(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(session_ttl=1.0)
    retained_addr = ("198.51.100.20", 50000)
    retained = state.install_session(
        _relation_key(secure, retained_addr),
        "retained",
        _fresh_test_locator(),
        object(),
        object(),
        now=0.0)
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
    assert tuple(state._sessions) == (retained._session_key,)
    assert _active_session_at(secure, state, retained_addr) is retained
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
        session._session_key.session_locator,
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
        session._session_key.session_locator,
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
        debug=True,
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
def test_secure_server_non_string_payload_is_rejected_and_later_valid_works(
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
        session._session_key.session_locator,
        payload=payload,
    )
    second_packet = _encrypted_data_packet(
        secure,
        client_to_server_key,
        second_nonce,
        session._session_key.session_locator,
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
                len(session.current_epoch.seen_data_nonces),
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

    assert construction_observations == [("later valid", 1, 1, 1)]
    assert len(fake_queue.items) == 1
    frame = fake_queue.items[0]
    assert isinstance(frame, IngressFrame)
    assert decode_frame_slice(frame, 0, len(frame.payload)) == "later valid"
    assert frame.text_mode is PayloadTextMode.UTF8_SURROGATEPASS
    assert len(session.current_epoch.seen_data_nonces) == 1
    assert session.last_seen == 1010.0
    stats = state.stats()
    assert stats.data_nonces_accepted == 1
    assert stats.sessions_touched == 1


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
        session._session_key.session_locator,
    )

    fake_queue, _ = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(packet, addr), (packet, addr)],
        state=state,
    )

    stats = state.stats()
    assert len(fake_queue.items) == 1
    assert len(session.current_epoch.seen_data_nonces) == 1
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
    packet = secure.build_data_packet(
        session._session_key.session_locator, nonce, b"\x00" * 16
    )

    fake_queue, _ = _run_secure_server_with_packets(
        monkeypatch, secure, [(packet, addr)], state=state)

    stats = state.stats()
    assert fake_queue.items == []
    assert session.last_seen == 1000.0
    assert len(session.current_epoch.seen_data_nonces) == 0
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
        session._session_key.session_locator,
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
    assert len(session.current_epoch.seen_data_nonces) == 0
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
    assert len(session.current_epoch.seen_data_nonces) == 0
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
        session._session_key.session_locator,
        source_id="other_station",
    )

    fake_queue, _ = _run_secure_server_with_packets(
        monkeypatch, secure, [(packet, addr)], state=state)

    stats = state.stats()
    assert fake_queue.items == []
    assert session.last_seen == 1000.0
    assert len(session.current_epoch.seen_data_nonces) == 0
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
        _relation_key(secure, addr),
        "boat_001",
        _fresh_test_locator(),
        object(),
        object(),
        now=0.0)
    other = state.install_session(
        _relation_key(secure, other_addr),
        "other",
        _fresh_test_locator(),
        object(),
        object(),
        now=1.0)
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

    pending = state._pending_sessions[_relation_key(secure, addr)]
    response, response_addr = fake_socket.sent[0]
    assert response_addr == addr
    assert response.startswith(b"OK|")
    assert _active_session_at(secure, state, addr) is old
    assert pending is not old
    assert (
        pending.current_epoch.client_to_server_aesgcm
        is not old.current_epoch.client_to_server_aesgcm
    )
    assert (
        pending.current_epoch.server_to_client_aesgcm
        is not old.current_epoch.server_to_client_aesgcm
    )
    assert _active_session_at(secure, state, other_addr) is other
    assert tuple(state._sessions) == (old._session_key, other._session_key)
    assert tuple(state._pending_sessions) == _relation_keys(secure, (addr,))
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
    session, _, _ = _install_test_session(
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
        session._session_key.session_locator,
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
    assert fake_socket.sent == []
    assert state.stats().sessions_expired == int(not accepted)


def test_secure_server_retains_nonce_for_live_traffic_key_epoch(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    client_to_server_key = b"\x01" * 32
    server_to_client_key = b"\x03" * 32
    first_nonce = b"\x02" * 12
    later_nonce = b"\x03" * 12
    addr = ("127.0.0.1", 50123)
    state = secure.SecureState(session_ttl=1000.0)
    session, _, _ = _install_test_session(
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
        first_nonce,
        session._session_key.session_locator,
        payload="first",
    )
    later_packet = _encrypted_data_packet(
        secure,
        client_to_server_key,
        later_nonce,
        session._session_key.session_locator,
        payload="later",
    )

    first_queue, _ = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(packet, addr)],
        state=state,
        wall_clock=_FakeClock(1000.0),
        monotonic_clock=_FakeClock(0.0),
    )
    later_queue, _ = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(later_packet, addr)],
        state=state,
        wall_clock=_FakeClock(50_000.0),
        monotonic_clock=_FakeClock(299.0),
    )
    replay_queue, _ = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(packet, addr)],
        state=state,
        wall_clock=_FakeClock(-50_000.0),
        monotonic_clock=_FakeClock(300.0),
    )

    assert len(first_queue.items) == 1
    assert len(later_queue.items) == 1
    assert replay_queue.items == []
    assert _active_session_at(secure, state, addr) is session
    assert session.last_seen == 299.0
    assert session.current_epoch.seen_data_nonces.contains(first_nonce)
    stats = state.stats()
    assert stats.data_nonces_accepted == 2
    assert stats.data_nonce_replays == 1
    assert stats.data_nonces_expired == 0
    assert stats.sessions_touched == 2


@pytest.mark.parametrize("message_type", ("nmea", "ping", "close"))
def test_secure_server_valid_new_nonce_at_capacity_invalidates_epoch_without_action(
    monkeypatch,
    message_type,
):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    client_to_server_key = b"\x11" * 32
    server_to_client_key = b"\x12" * 32
    retained_nonce = b"\x21" * 12
    triggering_nonce = b"\x22" * 12
    addr = ("127.0.0.1", 50123)
    state = secure.SecureState(data_nonce_max_per_session=1)
    session, _, _ = _install_test_session(
        secure,
        state,
        addr,
        client_to_server_key,
        server_to_client_key,
    )
    assert state.admit_data_nonce(
        session,
        retained_nonce,
        now=1000.0,
    ) is secure._DataNonceAdmission.ACCEPTED

    if message_type == "nmea":
        packet = _encrypted_data_packet(
            secure,
            client_to_server_key,
            triggering_nonce,
            session._session_key.session_locator,
            payload="!AIVDM,1,1,,A,must-not-queue,0*00",
        )
    elif message_type == "ping":
        packet = _encrypted_control_packet(
            secure,
            client_to_server_key,
            triggering_nonce,
            {
                "type": "ping",
                "seq": 7,
                "timestamp": 1000,
                "source_id": "boat_001",
            },
            session._session_key.session_locator,
        )
    else:
        packet = _encrypted_control_packet(
            secure,
            client_to_server_key,
            triggering_nonce,
            secure.build_session_close_message("boat_001", 1000),
            session._session_key.session_locator,
        )
    owned_sessions = {session._session_key: session}

    queue, fake_socket = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(packet, addr)],
        state=state,
        monotonic_clock=_FakeClock(1010.0),
        owned_sessions=owned_sessions,
    )

    stats = state.stats()
    assert queue.items == []
    assert fake_socket.sent == []
    assert _active_session_at(secure, state, addr) is None
    assert owned_sessions == {}
    assert session.last_seen == 1000.0
    assert len(session.current_epoch.seen_data_nonces) == 0
    assert stats.data_nonces_accepted == 1
    assert stats.data_nonce_exhaustions == 1
    assert stats.data_nonces_session_discarded == 1
    assert stats.current_data_nonces == 0
    assert stats.sessions_touched == 0
    assert stats.sessions_closed == 0
    assert stats.sessions_expired == 0
    assert stats.sessions_capacity_evicted == 0
    assert stats.sessions_replaced == 0


@pytest.mark.parametrize("case", ("invalid-tag", "invalid-semantic-data"))
def test_secure_server_invalid_packet_at_nonce_capacity_preserves_epoch(
    monkeypatch,
    case,
):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    client_to_server_key = b"\x31" * 32
    server_to_client_key = b"\x32" * 32
    retained_nonce = b"\x41" * 12
    rejected_nonce = b"\x42" * 12
    addr = ("127.0.0.1", 50123)
    state = secure.SecureState(data_nonce_max_per_session=1)
    session, _, _ = _install_test_session(
        secure,
        state,
        addr,
        client_to_server_key,
        server_to_client_key,
    )
    assert state.admit_data_nonce(
        session,
        retained_nonce,
        now=1000.0,
    ) is secure._DataNonceAdmission.ACCEPTED

    if case == "invalid-tag":
        packet = _encrypted_data_packet(
            secure,
            b"\x33" * 32,
            rejected_nonce,
            session._session_key.session_locator,
        )
    else:
        packet = _encrypted_data_packet(
            secure,
            client_to_server_key,
            rejected_nonce,
            session._session_key.session_locator,
            payload=None,
        )

    queue, fake_socket = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(packet, addr)],
        state=state,
        monotonic_clock=_FakeClock(1010.0),
    )

    stats = state.stats()
    assert queue.items == []
    assert fake_socket.sent == []
    assert _active_session_at(secure, state, addr) is session
    assert session.last_seen == 1000.0
    assert session.current_epoch.seen_data_nonces.contains(retained_nonce)
    assert not session.current_epoch.seen_data_nonces.contains(rejected_nonce)
    assert len(session.current_epoch.seen_data_nonces) == 1
    assert stats.data_nonces_accepted == 1
    assert stats.data_nonce_exhaustions == 0
    assert stats.data_nonces_session_discarded == 0
    assert stats.current_data_nonces == 1
    assert stats.sessions_touched == 0


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
    session = state.install_session(
        _relation_key(secure, addr),
        "boat_001",
        _fresh_test_locator(),
        client_to_server_aesgcm,
        server_to_client_aesgcm,
        now=1000.0)
    packet = _encrypted_data_packet(
        secure,
        client_to_server_key,
        nonce,
        session._session_key.session_locator,
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
        nonce,
        b"not-json",
        secure.build_data_aad(session._session_key.session_locator),
    )
    packet = secure.build_data_packet(
        session._session_key.session_locator, nonce, ciphertext
    )

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
        session._session_key.session_locator,
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
        _relation_key(secure, silent_addr),
        "silent",
        _fresh_test_locator(),
        object(),
        object(),
        now=0.0)
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
    assert fake_socket.sent == []


def test_allowed_handshake_proactively_cleans_silent_expired_session(monkeypatch):
    secure, client_private_key = load_secure_module_with_fake_keys(
        monkeypatch, with_client_private_key=True
    )
    state = secure.SecureState(session_ttl=10.0)
    silent_addr = ("127.0.0.1", 50122)
    handshake_addr = ("127.0.0.1", 50123)
    state.install_session(
        _relation_key(secure, silent_addr),
        "silent",
        _fresh_test_locator(),
        object(),
        object(),
        now=0.0)
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
    assert tuple(state._pending_sessions) == _relation_keys(
        secure, (handshake_addr,)
    )
    assert state.stats().sessions_expired == 1
    assert state.stats().pending_sessions_created == 1
    assert len(fake_socket.sent) == 1
    assert fake_socket.sent[0][0].startswith(b"OK|")


def test_unknown_allowed_packet_proactively_cleans_without_wall_clock(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(session_ttl=10.0)
    silent_addr = ("127.0.0.1", 50122)
    state.install_session(
        _relation_key(secure, silent_addr),
        "silent",
        _fresh_test_locator(),
        object(),
        object(),
        now=0.0)
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


def test_exactly_expired_address_data_is_silently_dropped(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(session_ttl=10.0)
    addr = ("127.0.0.1", 50123)
    state.install_session(
        _relation_key(secure, addr),
        "boat_001",
        _fresh_test_locator(),
        object(),
        object(),
        now=0.0)

    _, fake_socket = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(secure.DATA_PREFIX + (b"\x00" * 28), addr)],
        state=state,
        monotonic_clock=_FakeClock(10.0),
    )

    assert fake_socket.sent == []
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


def test_expiring_state_cleanup_and_capacity_do_not_scan_or_call_min(
    monkeypatch,
):
    secure = load_secure_module_with_fake_keys(monkeypatch)

    class NoScanDict(dict):
        def __iter__(self):
            raise AssertionError("live dictionary must not be iterated")

        def items(self):
            raise AssertionError("live dictionary must not be scanned")

        def values(self):
            raise AssertionError("live dictionary must not be scanned")

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

    expiring_set._live_by_key = NoScanDict(expiring_set._live_by_key)

    def fail_min(*_args, **_kwargs):
        raise AssertionError("min() must not be used for eviction")

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "min", fail_min)
        assert operation()
        assert capacity_operation()

    assert len(expiring_set._live_by_key) == 2


def test_proxy_encrypt_message_aes_gcm_uses_12_byte_nonce_and_locator_aad():
    proxy = load_proxy_module()
    key = b"\x01" * 32
    locator = _fresh_test_locator()
    plaintext = b'{"type":"nmea","payload":"!AIVDM,1,1,,A,payload,0*00"}'

    nonce, ciphertext_and_tag = proxy.encrypt_message_aes_gcm(
        plaintext, key, proxy.build_data_aad(locator)
    )

    assert len(nonce) == 12
    assert (
        AESGCM(key).decrypt(
            nonce, ciphertext_and_tag, proxy.build_data_aad(locator)
        )
        == plaintext
    )


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


def _proxy_confirmed_session(
    proxy,
    *,
    session_locator=None,
    client_to_server_key=b"\x01" * 32,
    server_to_client_key=b"\x02" * 32,
):
    if session_locator is None:
        session_locator = _fresh_test_locator()
    return proxy.ConfirmedUdpsecSession(
        session_locator=session_locator,
        key_material=_proxy_session_key_material(
            proxy,
            client_to_server_key=client_to_server_key,
            server_to_client_key=server_to_client_key,
        ),
    )


def test_proxy_ignores_forged_plaintext_no_session_from_configured_remote():
    proxy = load_proxy_module()
    remote_addr = ("192.0.2.10", 17777)

    assert proxy.handle_server_packet(
        b"NOSESSION|boat_001",
        remote_addr,
        remote_addr,
        b"\x01" * 32,
        _fresh_test_locator(),
        "boat_001",
        1,
    ) == proxy.SERVER_PACKET_IGNORED


def test_proxy_ignores_no_session_from_unexpected_address():
    proxy = load_proxy_module()

    assert proxy.handle_server_packet(
        b"NOSESSION|boat_001",
        ("192.0.2.11", 17777),
        ("192.0.2.10", 17777),
        b"\x01" * 32,
        _fresh_test_locator(),
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
        _fresh_test_locator(),
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
    confirmed_session = _proxy_confirmed_session(
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
        confirmed_session,
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
        confirmed_session.session_locator,
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
        _proxy_confirmed_session(proxy),
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
    locator = _fresh_test_locator()
    remote_addr = ("192.0.2.10", 17777)
    packet = proxy.encrypt_secure_json_message(
        {
            "type": "pong",
            "seq": 123,
            "timestamp": 1000,
            "source_id": "boat_001",
        },
        server_to_client_key,
        locator,
    )

    assert proxy.handle_server_packet(
        packet,
        remote_addr,
        remote_addr,
        server_to_client_key,
        locator,
        "boat_001",
        123,
    ) == proxy.SERVER_PACKET_AUTHENTICATED
    assert proxy.handle_server_packet(
        packet,
        remote_addr,
        remote_addr,
        client_to_server_key,
        locator,
        "boat_001",
        123,
    ) == proxy.SERVER_PACKET_IGNORED
    assert proxy.handle_server_packet(
        packet,
        ("192.0.2.10", 17778),
        remote_addr,
        server_to_client_key,
        locator,
        "boat_001",
        123,
    ) == proxy.SERVER_PACKET_IGNORED
    assert proxy.handle_server_packet(
        b"PONG|123",
        remote_addr,
        remote_addr,
        server_to_client_key,
        locator,
        "boat_001",
        123,
    ) == proxy.SERVER_PACKET_IGNORED
    assert proxy.handle_server_packet(
        packet,
        remote_addr,
        remote_addr,
        server_to_client_key,
        locator,
        "boat_001",
        124,
    ) == proxy.SERVER_PACKET_IGNORED
    assert proxy.handle_server_packet(
        packet,
        remote_addr,
        remote_addr,
        server_to_client_key,
        _fresh_test_locator(),
        "boat_001",
        123,
    ) == proxy.SERVER_PACKET_IGNORED


@pytest.mark.parametrize(
    "message",
    (
        {"type": "pong", "seq": 123, "source_id": "boat_001"},
        {
            "type": "pong",
            "seq": 123,
            "timestamp": True,
            "source_id": "boat_001",
        },
        {
            "type": "pong",
            "seq": 123,
            "timestamp": 1000.0,
            "source_id": "boat_001",
        },
        {
            "type": "pong",
            "seq": 123,
            "timestamp": "1000",
            "source_id": "boat_001",
        },
        {
            "type": "pong",
            "timestamp": 1000,
            "source_id": "boat_001",
        },
        {
            "type": "pong",
            "seq": True,
            "timestamp": 1000,
            "source_id": "boat_001",
        },
        {
            "type": "pong",
            "seq": 123,
            "timestamp": 1000,
        },
        {
            "type": "pong",
            "seq": 123,
            "timestamp": 1000,
            "source_id": "other_station",
        },
        {
            "type": "status",
            "seq": 123,
            "timestamp": 1000,
            "source_id": "boat_001",
        },
        ["pong", 123, 1000, "boat_001"],
    ),
)
def test_proxy_rejects_authenticated_structurally_invalid_pong(message):
    proxy = load_proxy_module()
    key = b"\x02" * 32
    locator = _fresh_test_locator()
    remote_addr = ("192.0.2.10", 17777)
    packet = proxy.encrypt_secure_json_message(message, key, locator)

    assert proxy.handle_server_packet(
        packet,
        remote_addr,
        remote_addr,
        key,
        locator,
        "boat_001",
        123,
    ) == proxy.SERVER_PACKET_IGNORED


@pytest.mark.parametrize("sequence", (122, 124, 0, -1))
def test_proxy_rejects_stale_future_reserved_or_negative_pong_sequence(
    sequence,
):
    proxy = load_proxy_module()
    key = b"\x02" * 32
    locator = _fresh_test_locator()
    remote_addr = ("192.0.2.10", 17777)
    packet = proxy.encrypt_secure_json_message(
        {
            "type": "pong",
            "seq": sequence,
            "timestamp": 1000,
            "source_id": "boat_001",
        },
        key,
        locator,
    )

    assert proxy.handle_server_packet(
        packet,
        remote_addr,
        remote_addr,
        key,
        locator,
        "boat_001",
        123,
    ) == proxy.SERVER_PACKET_IGNORED


def test_proxy_cleared_expectation_rejects_duplicate_matching_pong():
    proxy = load_proxy_module()
    key = b"\x02" * 32
    locator = _fresh_test_locator()
    remote_addr = ("192.0.2.10", 17777)
    packet = proxy.encrypt_secure_json_message(
        {
            "type": "pong",
            "seq": 123,
            "timestamp": 1000,
            "source_id": "boat_001",
        },
        key,
        locator,
    )

    assert proxy.handle_server_packet(
        packet,
        remote_addr,
        remote_addr,
        key,
        locator,
        "boat_001",
        123,
    ) == proxy.SERVER_PACKET_AUTHENTICATED
    assert proxy.handle_server_packet(
        packet,
        remote_addr,
        remote_addr,
        key,
        locator,
        "boat_001",
        None,
    ) == proxy.SERVER_PACKET_IGNORED


def test_proxy_authenticated_nmea_is_not_server_liveness():
    proxy = load_proxy_module()
    key = b"\x02" * 32
    locator = _fresh_test_locator()
    remote_addr = ("192.0.2.10", 17777)
    packet = proxy.encrypt_secure_json_message(
        {
            "type": "nmea",
            "payload": "!AIVDM,1,1,,A,payload,0*00",
            "timestamp": 1000,
            "source_id": "boat_001",
        },
        key,
        locator,
    )

    assert proxy.handle_server_packet(
        packet,
        remote_addr,
        remote_addr,
        key,
        locator,
        "boat_001",
        123,
    ) == proxy.SERVER_PACKET_IGNORED


def test_proxy_distinguishes_authenticated_peer_close_from_matching_pong():
    proxy = load_proxy_module()
    server_to_client_key = b"\x02" * 32
    locator = _fresh_test_locator()
    remote_addr = ("192.0.2.10", 17777)
    close_packet = proxy.encrypt_secure_json_message(
        proxy.build_session_close_message("boat_001", 1000),
        server_to_client_key,
        locator,
    )

    assert proxy.handle_server_packet(
        close_packet,
        remote_addr,
        remote_addr,
        server_to_client_key,
        locator,
        "boat_001",
        None,
    ) == proxy.SERVER_PACKET_PEER_CLOSE
    assert proxy.handle_server_packet(
        close_packet,
        remote_addr,
        remote_addr,
        server_to_client_key,
        locator,
        "boat_001",
        123,
    ) == proxy.SERVER_PACKET_PEER_CLOSE


def test_proxy_ignores_malformed_or_unauthenticated_peer_close():
    proxy = load_proxy_module()
    server_to_client_key = b"\x02" * 32
    locator = _fresh_test_locator()
    remote_addr = ("192.0.2.10", 17777)
    invalid_messages = (
        {
            "type": "close",
            "reason": "shutdown",
            "timestamp": 1000,
            "source_id": "other_station",
        },
        {
            "type": "close",
            "reason": "restart",
            "timestamp": 1000,
            "source_id": "boat_001",
        },
        {
            "type": "close",
            "reason": "shutdown",
            "source_id": "boat_001",
        },
        {
            "type": "close",
            "reason": "shutdown",
            "timestamp": 1000,
            "source_id": "boat_001",
            "seq": 1,
        },
    )

    for message in invalid_messages:
        packet = proxy.encrypt_secure_json_message(
            message,
            server_to_client_key,
            locator,
        )
        assert proxy.handle_server_packet(
            packet,
            remote_addr,
            remote_addr,
            server_to_client_key,
            locator,
            "boat_001",
            1,
        ) == proxy.SERVER_PACKET_IGNORED

    valid_close = proxy.encrypt_secure_json_message(
        proxy.build_session_close_message("boat_001", 1000),
        server_to_client_key,
        locator,
    )
    assert proxy.handle_server_packet(
        valid_close,
        ("192.0.2.10", 17778),
        remote_addr,
        server_to_client_key,
        locator,
        "boat_001",
        1,
    ) == proxy.SERVER_PACKET_IGNORED
    assert proxy.handle_server_packet(
        valid_close,
        remote_addr,
        remote_addr,
        b"\x03" * 32,
        locator,
        "boat_001",
        1,
    ) == proxy.SERVER_PACKET_IGNORED
    assert proxy.handle_server_packet(
        b'{"type":"close","reason":"shutdown"}',
        remote_addr,
        remote_addr,
        server_to_client_key,
        locator,
        "boat_001",
        1,
    ) == proxy.SERVER_PACKET_IGNORED
    assert proxy.handle_server_packet(
        valid_close,
        remote_addr,
        remote_addr,
        server_to_client_key,
        _fresh_test_locator(),
        "boat_001",
        1,
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
    session_locator=None,
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
    if session_locator is None:
        session_locator = _fresh_test_locator()
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
        protocol_version=client_hello.protocol_version,
        station_id=client_hello.station_id,
        timestamp=client_hello.timestamp,
        client_random=client_hello.client_random,
        client_ephemeral_public_key=(
            client_hello.client_ephemeral_public_key
        ),
        client_signature=bound_client_signature,
        session_locator=session_locator,
        server_random=server_random,
        server_ephemeral_public_key=server_ephemeral_public_bytes,
    )
    server_signature = udpsec_crypto.sign_transcript_digest(
        server_identity_private_key,
        server_digest,
    )
    server_hello = ServerHello(
        protocol_version=client_hello.protocol_version,
        session_locator=session_locator,
        server_random=server_random,
        server_ephemeral_public_key=server_ephemeral_public_bytes,
        server_signature=server_signature,
    )
    shared_secret = udpsec_crypto.derive_ephemeral_shared_secret(
        server_ephemeral_private_key,
        client_ephemeral_public_key,
    )
    transcript_hash = udpsec_crypto.build_session_transcript_hash(
        protocol_version=client_hello.protocol_version,
        station_id=client_hello.station_id,
        timestamp=client_hello.timestamp,
        client_random=client_hello.client_random,
        client_ephemeral_public_key=(
            client_hello.client_ephemeral_public_key
        ),
        client_signature=client_hello.client_signature,
        session_locator=server_hello.session_locator,
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
        self.session_locator = None
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
        self.session_locator = server_hello.session_locator
        return response, self.remote_addr

    def read_confirmation_ping(self, confirmation_packet):
        assert self.key_material is not None
        message = self.proxy.decrypt_secure_json_message(
            confirmation_packet,
            self.key_material.client_to_server_key,
            self.session_locator,
        )
        with pytest.raises(InvalidTag):
            self.proxy.decrypt_secure_json_message(
                confirmation_packet,
                self.key_material.server_to_client_key,
                self.session_locator,
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
        _, self.confirmation_nonce, _ = self.proxy.parse_data_packet(
            confirmation_packet
        )
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
            self.session_locator,
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


def test_proxy_handshake_ignores_forged_plaintext_before_server_hello(
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

    confirmed_session = proxy.perform_handshake(
        sock,
        {"station_id": station_id},
        station_identity_private_key,
        server_identity_private_key.public_key(),
        remote_addr,
    )
    key_material = confirmed_session.key_material

    client_hello = parse_client_hello_packet(sock.sent[0][0])
    client_digest = udpsec_crypto.build_client_auth_digest(
        protocol_version=client_hello.protocol_version,
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
    assert confirmed_session.session_locator == (
        confirming_server.session_locator
    )
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
            confirmed_session.session_locator,
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

    confirmed_session = proxy.perform_handshake(
        sock,
        {"station_id": station_id},
        client_private_key,
        server_private_key.public_key(),
        remote_addr,
    )

    assert isinstance(confirmed_session.key_material, SessionKeyMaterial)
    assert len(response_packets) == 2
    assert len(sock.sent) == 2


@pytest.mark.parametrize(
    "mutation",
    (
        "wrong-identity",
        "server-random",
        "server-ephemeral",
        "session-locator",
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
            locator = _fresh_test_locator()
            digest = udpsec_crypto.build_server_auth_digest(
                protocol_version=client_hello.protocol_version,
                station_id=client_hello.station_id,
                timestamp=client_hello.timestamp,
                client_random=client_hello.client_random,
                client_ephemeral_public_key=(
                    client_hello.client_ephemeral_public_key
                ),
                client_signature=client_hello.client_signature,
                session_locator=locator,
                server_random=server_random,
                server_ephemeral_public_key=malformed_public_bytes,
            )
            signature = udpsec_crypto.sign_transcript_digest(
                server_identity_private_key,
                digest,
            )
            return build_server_hello_packet(
                ServerHello(
                    protocol_version=client_hello.protocol_version,
                    session_locator=locator,
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
                    protocol_version=server_hello.protocol_version,
                    session_locator=server_hello.session_locator,
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
                    protocol_version=server_hello.protocol_version,
                    session_locator=server_hello.session_locator,
                    server_random=server_hello.server_random,
                    server_ephemeral_public_key=changed_public_bytes,
                    server_signature=server_hello.server_signature,
                )
            )
        elif mutation == "session-locator":
            changed_locator = bytes(
                byte ^ 0xFF for byte in server_hello.session_locator
            )
            response = build_server_hello_packet(
                ServerHello(
                    protocol_version=server_hello.protocol_version,
                    session_locator=changed_locator,
                    server_random=server_hello.server_random,
                    server_ephemeral_public_key=(
                        server_hello.server_ephemeral_public_key
                    ),
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

    first_session = proxy.perform_handshake(
        new_socket(),
        {"station_id": "boat_001"},
        station_identity_private_key,
        server_identity_private_key.public_key(),
        remote_addr,
    )
    second_session = proxy.perform_handshake(
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
    assert first_session != second_session
    assert first_session.session_locator != second_session.session_locator
    assert first_session.key_material.client_to_server_key != (
        second_session.key_material.client_to_server_key
    )
    assert first_session.key_material.server_to_client_key != (
        second_session.key_material.server_to_client_key
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
    confirmed_session = proxy.perform_handshake(
        bridge_socket,
        {"station_id": "boat_001"},
        station_identity_private_key,
        _PREPARED_SERVER_PRIVATE_KEY.public_key(),
        remote_addr,
    )
    client_key_material = confirmed_session.key_material
    session_locator = confirmed_session.session_locator

    assert isinstance(client_key_material, SessionKeyMaterial)
    assert recorded_server_keys == [
        client_key_material.client_to_server_key,
        client_key_material.server_to_client_key,
    ]
    assert (
        client_key_material.client_to_server_key
        != client_key_material.server_to_client_key
    )
    server_session = _active_session_at(secure, state, client_addr)
    assert (
        server_session.current_epoch.client_to_server_aesgcm
        is not server_session.current_epoch.server_to_client_aesgcm
    )
    # The active-session store is keyed by (endpoint_token, locator), not
    # by the client's remote tuple: the locator the client received in
    # ServerHello must be the exact same locator the server's own active
    # session is now stored under.
    assert server_session._session_key.session_locator == session_locator
    assert state._sessions[server_session._session_key] is server_session

    nmea_packet = proxy.encrypt_secure_json_message(
        {
            "type": "nmea",
            "payload": "!AIVDM,1,1,,A,payload,0*00",
            "timestamp": timestamp,
            "source_id": "boat_001",
        },
        client_key_material.client_to_server_key,
        session_locator,
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
        session_locator,
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
        session_locator,
        "boat_001",
        7,
    ) == proxy.SERVER_PACKET_AUTHENTICATED
    assert proxy.handle_server_packet(
        pong_packet,
        remote_addr,
        remote_addr,
        client_key_material.client_to_server_key,
        session_locator,
        "boat_001",
        7,
    ) == proxy.SERVER_PACKET_IGNORED
    assert (
        len(ping_packet[len(proxy.DATA_PREFIX) + proxy.SESSION_LOCATOR_BYTES:][:12])
        == 12
    )
    assert (
        len(pong_packet[len(proxy.DATA_PREFIX) + proxy.SESSION_LOCATOR_BYTES:][:12])
        == 12
    )
    ping_locator, _, _ = proxy.parse_data_packet(ping_packet)
    pong_locator, _, _ = proxy.parse_data_packet(pong_packet)
    assert ping_locator == session_locator
    assert pong_locator == session_locator


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

    confirmed_session = proxy.perform_handshake(
        sock,
        {"station_id": station_id},
        station_identity_private_key,
        server_identity_private_key.public_key(),
        remote_addr,
    )
    key_material = confirmed_session.key_material

    assert key_material == confirming_server.key_material
    assert (
        key_material.server_to_client_key
        != old_active_keys.server_to_client_key
    )
    assert proxy.decrypt_secure_json_message(
        stale_packets[0],
        old_active_keys.server_to_client_key,
        confirmed_session.session_locator,
    )["seq"] == proxy.SESSION_CONFIRMATION_SEQUENCE
    with pytest.raises(InvalidTag):
        proxy.decrypt_secure_json_message(
            stale_packets[0],
            key_material.server_to_client_key,
            confirmed_session.session_locator,
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
        "missing-timestamp",
        "bool-timestamp",
        "string-timestamp",
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
        elif mutation == "missing-timestamp":
            pong.pop("timestamp")
        elif mutation == "bool-timestamp":
            pong["timestamp"] = True
        elif mutation == "string-timestamp":
            pong["timestamp"] = str(pong["timestamp"])
        elif mutation == "wrong-source":
            pong["source_id"] = "other_station"
        elif mutation == "wrong-type":
            pong["type"] = "status"

        if mutation == "malformed-json":
            nonce = b"\xa1" * 12
            encrypted = AESGCM(key).encrypt(
                nonce,
                b"not-json",
                proxy.build_data_aad(confirming_server.session_locator),
            )
            packet = proxy.build_data_packet(
                confirming_server.session_locator, nonce, encrypted
            )
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

    confirmed_session = proxy.perform_handshake(
        sock,
        {"station_id": station_id},
        station_identity_private_key,
        server_identity_private_key.public_key(),
        remote_addr,
    )

    output = capsys.readouterr().out
    assert confirmed_session.key_material == confirming_server.key_material
    assert confirmed_session.session_locator == (
        confirming_server.session_locator
    )
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

    confirmed_session = proxy.perform_handshake(
        sock,
        {"station_id": station_id},
        station_identity_private_key,
        server_identity_private_key.public_key(),
        remote_addr,
    )

    assert confirmed_session.key_material == confirming_server.key_material
    assert len(sock.sent) == 2
    assert sock.timeout == 5.0


def test_proxy_handshake_ignores_forged_plaintext_during_confirmation(
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

    confirmed_session = proxy.perform_handshake(
        sock,
        {"station_id": station_id},
        station_identity_private_key,
        server_identity_private_key.public_key(),
        remote_addr,
    )

    assert confirmed_session.key_material == confirming_server.key_material
    assert len(sock.sent) == 2
    assert sock.responses == []
    assert "Mutual ECDHE session confirmed." in (
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

    confirmed_session = proxy.perform_handshake(
        sock,
        {"station_id": "boat_001"},
        station_identity_private_key,
        server_identity_private_key.public_key(),
        remote_addr,
    )

    assert confirmed_session.key_material == confirming_server.key_material
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


def test_proxy_deadline_action_has_deterministic_exact_boundary_priority():
    proxy = load_proxy_module()
    config = {
        "keepalive_interval": 30,
        "peer_timeout": 60,
        "session_refresh_interval": 60,
    }

    assert proxy.session_deadline_action(
        29.999,
        0,
        0,
        0,
        None,
        config,
    ) is None
    assert proxy.session_deadline_action(
        30,
        0,
        0,
        0,
        None,
        config,
    ) == proxy.SESSION_ACTION_SEND_PING
    assert proxy.session_deadline_action(
        60,
        0,
        0,
        30,
        1,
        config,
    ) == proxy.SESSION_END_PEER_TIMEOUT

    config["peer_timeout"] = 90
    assert proxy.session_deadline_action(
        60,
        0,
        0,
        30,
        1,
        config,
    ) == proxy.SESSION_END_PLANNED_REFRESH

    config["session_refresh_interval"] = 0
    assert proxy.session_deadline_action(
        60,
        0,
        0,
        30,
        1,
        config,
    ) == proxy.SESSION_END_PROACTIVE_REKEY


def test_proxy_normal_ping_pong_does_not_trigger_periodic_reconnect():
    proxy = load_proxy_module()
    config = {
        "keepalive_interval": 30,
        "peer_timeout": 90,
        "session_refresh_interval": 0,
    }

    assert proxy.session_expiration_reason(3600, 0, 3595, config) is None
    assert proxy.session_poll_timeout(3600, 0, 3595, 3595, config) == 25


@pytest.mark.parametrize(
    "reason_name",
    ("SESSION_END_PLANNED_REFRESH", "SESSION_END_PROACTIVE_REKEY"),
)
def test_proxy_planned_or_proactive_rekey_does_not_wait_reconnect_delay(
    reason_name,
):
    proxy = load_proxy_module()
    config = {"reconnect_delay": 5}

    assert proxy.retry_delay_for_reason(
        getattr(proxy, reason_name), config
    ) is None


@pytest.mark.parametrize(
    "reason",
    [
        "peer_timeout",
        "peer_graceful_close",
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
            self.sent_at = []

        def sendto(self, data, addr):
            self.sent.append((data, addr))
            self.sent_at.append(clock[0])

    def fake_select(readable, writable, exceptional, timeout):
        clock[0] += timeout
        return [], [], []

    monkeypatch.setattr(proxy.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(proxy.select, "select", fake_select)
    udp_sock = FakeSocket()
    out_sock = FakeSocket()
    confirmed_session = _proxy_confirmed_session(proxy)
    reason = proxy.forward_loop(
        udp_sock,
        out_sock,
        config,
        confirmed_session,
        ("192.0.2.10", 17777),
    )
    return proxy, reason, out_sock, clock[0], confirmed_session


def test_proxy_outstanding_ping_is_not_overwritten_and_proactively_rekeys(
    monkeypatch,
):
    config = {
        "station_id": "boat_001",
        "keepalive_interval": 30,
        "peer_timeout": 90,
        "session_refresh_interval": 240,
    }

    proxy, reason, out_sock, ended_at, confirmed_session = (
        _run_idle_proxy_session(
            monkeypatch,
            config,
        )
    )

    assert reason == proxy.SESSION_END_PROACTIVE_REKEY
    assert ended_at == 2 * config["keepalive_interval"]
    assert ended_at < config["peer_timeout"]
    assert len(out_sock.sent) == 1
    assert out_sock.sent_at == [config["keepalive_interval"]]
    ping = proxy.decrypt_secure_json_message(
        out_sock.sent[0][0],
        confirmed_session.key_material.client_to_server_key,
        confirmed_session.session_locator,
    )
    assert ping["type"] == "ping"
    assert ping["seq"] == 1


def test_proxy_exact_keepalive_deadline_rekeys_before_ready_matching_pong(
    monkeypatch,
):
    proxy = load_proxy_module()
    client_key = b"\x01" * 32
    server_key = b"\x02" * 32
    locator = _fresh_test_locator()
    remote_addr = ("192.0.2.10", 17777)
    clock = [0.0]

    class IdleInput:
        def selectable_sockets(self):
            return []

        def poll_interval(self):
            return None

        def read_ready(self, _ready_socket):
            raise AssertionError("idle input must not become readable")

        def read_pending(self):
            return ()

    class FakeOutSocket:
        def __init__(self):
            self.response = None
            self.recv_calls = 0
            self.sent = []

        def sendto(self, packet, addr):
            ping = proxy.decrypt_secure_json_message(
                packet, client_key, locator
            )
            self.sent.append((ping, clock[0], addr))
            self.response = proxy.encrypt_secure_json_message(
                {
                    "type": "pong",
                    "seq": ping["seq"],
                    "timestamp": int(clock[0]),
                    "source_id": "boat_001",
                },
                server_key,
                locator,
            )

        def recvfrom(self, _size):
            self.recv_calls += 1
            return self.response, remote_addr

    out_sock = FakeOutSocket()
    select_calls = [0]

    def fake_select(_readable, _writable, _exceptional, timeout):
        select_calls[0] += 1
        clock[0] += timeout
        if select_calls[0] == 2:
            return [out_sock], [], []
        return [], [], []

    monkeypatch.setattr(proxy.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(proxy.select, "select", fake_select)

    reason = proxy.forward_loop(
        IdleInput(),
        out_sock,
        {
            "station_id": "boat_001",
            "keepalive_interval": 30,
            "peer_timeout": 90,
            "session_refresh_interval": 0,
        },
        _proxy_confirmed_session(
            proxy,
            session_locator=locator,
            client_to_server_key=client_key,
            server_to_client_key=server_key,
        ),
        remote_addr,
    )

    assert reason == proxy.SESSION_END_PROACTIVE_REKEY
    assert clock[0] == 60
    assert [item[0]["seq"] for item in out_sock.sent] == [1]
    assert out_sock.recv_calls == 0


def test_proxy_matching_pong_before_deadline_schedules_next_normal_ping(
    monkeypatch,
):
    proxy = load_proxy_module()
    client_key = b"\x01" * 32
    server_key = b"\x02" * 32
    locator = _fresh_test_locator()
    remote_addr = ("192.0.2.10", 17777)
    clock = [0.0]

    class IdleInput:
        def selectable_sockets(self):
            return []

        def poll_interval(self):
            return None

        def read_ready(self, _ready_socket):
            raise AssertionError("idle input must not become readable")

        def read_pending(self):
            return ()

    class FakeOutSocket:
        def __init__(self):
            self.response = None
            self.sent = []

        def sendto(self, packet, addr):
            ping = proxy.decrypt_secure_json_message(
                packet, client_key, locator
            )
            self.sent.append((ping, clock[0], addr))
            if ping["seq"] == 1:
                self.response = proxy.encrypt_secure_json_message(
                    {
                        "type": "pong",
                        "seq": ping["seq"],
                        "timestamp": int(clock[0]),
                        "source_id": "boat_001",
                    },
                    server_key,
                    locator,
                )
            else:
                raise OSError("end test after second ping")

        def recvfrom(self, _size):
            return self.response, remote_addr

    out_sock = FakeOutSocket()
    select_calls = [0]

    def fake_select(_readable, _writable, _exceptional, timeout):
        select_calls[0] += 1
        if select_calls[0] == 1:
            clock[0] += timeout
            return [], [], []
        if select_calls[0] == 2:
            clock[0] = 59.999
            return [out_sock], [], []
        clock[0] += timeout
        return [], [], []

    monkeypatch.setattr(proxy.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(proxy.select, "select", fake_select)

    reason = proxy.forward_loop(
        IdleInput(),
        out_sock,
        {
            "station_id": "boat_001",
            "keepalive_interval": 30,
            "peer_timeout": 90,
            "session_refresh_interval": 0,
        },
        _proxy_confirmed_session(
            proxy,
            session_locator=locator,
            client_to_server_key=client_key,
            server_to_client_key=server_key,
        ),
        remote_addr,
    )

    assert reason == proxy.SESSION_END_SOCKET_ERROR
    assert [item[0]["seq"] for item in out_sock.sent] == [1, 2]
    assert [item[1] for item in out_sock.sent] == [30, 60]


def test_proxy_duplicate_pong_after_acceptance_does_not_refresh_liveness(
    monkeypatch,
):
    proxy = load_proxy_module()
    client_key = b"\x01" * 32
    server_key = b"\x02" * 32
    locator = _fresh_test_locator()
    remote_addr = ("192.0.2.10", 17777)
    clock = [0.0]

    class IdleInput:
        def selectable_sockets(self):
            return []

        def poll_interval(self):
            return None

        def read_ready(self, _ready_socket):
            raise AssertionError("idle input must not become readable")

        def read_pending(self):
            return ()

    class FakeOutSocket:
        def __init__(self):
            self.response = None
            self.sent = []
            self.recv_calls = 0

        def sendto(self, packet, addr):
            ping = proxy.decrypt_secure_json_message(
                packet, client_key, locator
            )
            self.sent.append((ping, clock[0], addr))
            if ping["seq"] == 1:
                self.response = proxy.encrypt_secure_json_message(
                    {
                        "type": "pong",
                        "seq": ping["seq"],
                        "timestamp": int(clock[0]),
                        "source_id": "boat_001",
                    },
                    server_key,
                    locator,
                )

        def recvfrom(self, _size):
            self.recv_calls += 1
            return self.response, remote_addr

    out_sock = FakeOutSocket()
    select_calls = [0]

    def fake_select(_readable, _writable, _exceptional, timeout):
        select_calls[0] += 1
        if select_calls[0] == 1:
            clock[0] += timeout
            return [], [], []
        if select_calls[0] == 2:
            clock[0] = 10.5
            return [out_sock], [], []
        if select_calls[0] == 3:
            clock[0] = 19
            return [out_sock], [], []
        clock[0] += timeout
        return [], [], []

    monkeypatch.setattr(proxy.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(proxy.select, "select", fake_select)

    reason = proxy.forward_loop(
        IdleInput(),
        out_sock,
        {
            "station_id": "boat_001",
            "keepalive_interval": 10,
            "peer_timeout": 15,
            "session_refresh_interval": 0,
        },
        _proxy_confirmed_session(
            proxy,
            session_locator=locator,
            client_to_server_key=client_key,
            server_to_client_key=server_key,
        ),
        remote_addr,
    )

    assert reason == proxy.SESSION_END_PEER_TIMEOUT
    assert clock[0] == 25.5
    assert [item[0]["seq"] for item in out_sock.sent] == [1, 2]
    assert out_sock.recv_calls == 2


def test_proxy_recomputes_poll_deadline_after_slow_pending_input(
    monkeypatch,
):
    proxy = load_proxy_module()
    clock = [0.0]

    class SlowPendingInput:
        def __init__(self):
            self.drained = False

        def selectable_sockets(self):
            return []

        def poll_interval(self):
            return None

        def read_ready(self, _ready_socket):
            raise AssertionError("input must not become readable")

        def read_pending(self):
            if not self.drained:
                self.drained = True
                clock[0] = 30
            return ()

    class FakeOutSocket:
        def __init__(self):
            self.sent_at = []

        def sendto(self, _packet, _addr):
            self.sent_at.append(clock[0])

    out_sock = FakeOutSocket()
    poll_timeouts = []

    def fake_select(_readable, _writable, _exceptional, timeout):
        poll_timeouts.append(timeout)
        raise OSError("end test after fresh timeout calculation")

    monkeypatch.setattr(proxy.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(proxy.select, "select", fake_select)

    reason = proxy.forward_loop(
        SlowPendingInput(),
        out_sock,
        {
            "station_id": "boat_001",
            "keepalive_interval": 30,
            "peer_timeout": 90,
            "session_refresh_interval": 0,
        },
        _proxy_confirmed_session(proxy),
        ("192.0.2.10", 17777),
    )

    assert reason == proxy.SESSION_END_SOCKET_ERROR
    assert out_sock.sent_at == [30]
    assert poll_timeouts == [30]


def test_proxy_peer_timeout_remains_fallback_before_first_ping(monkeypatch):
    config = {
        "station_id": "boat_001",
        "keepalive_interval": 120,
        "peer_timeout": 90,
        "session_refresh_interval": 0,
    }

    proxy, reason, out_sock, ended_at, _ = _run_idle_proxy_session(
        monkeypatch,
        config,
    )

    assert reason == proxy.SESSION_END_PEER_TIMEOUT
    assert ended_at == config["peer_timeout"]
    assert out_sock.sent == []


def test_proxy_forward_loop_exits_for_planned_refresh_without_local_udp(monkeypatch):
    config = {
        "station_id": "boat_001",
        "keepalive_interval": 30,
        "peer_timeout": 1000,
        "session_refresh_interval": 60,
    }

    proxy, reason, _, _, _ = _run_idle_proxy_session(monkeypatch, config)

    assert reason == proxy.SESSION_END_PLANNED_REFRESH


def test_proxy_forward_loop_ignores_forged_no_session_until_proactive_rekey(
    monkeypatch,
):
    proxy = load_proxy_module()
    remote_addr = ("192.0.2.10", 17777)
    clock = [0.0]

    class IdleInput:
        def selectable_sockets(self):
            return []

        def poll_interval(self):
            return None

        def read_ready(self, _ready_socket):
            raise AssertionError("idle input must not become readable")

        def read_pending(self):
            return ()

    class FakeOutSocket:
        def __init__(self):
            self.response_pending = True
            self.sent = []

        def sendto(self, data, addr):
            self.sent.append((data, addr))

        def recvfrom(self, size):
            assert self.response_pending
            self.response_pending = False
            return b"NOSESSION|boat_001", remote_addr

    out_sock = FakeOutSocket()

    def fake_select(readable, writable, exceptional, timeout):
        if out_sock.response_pending:
            return [out_sock], [], []
        clock[0] += timeout
        return [], [], []

    monkeypatch.setattr(proxy.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        proxy.select,
        "select",
        fake_select,
    )

    reason = proxy.forward_loop(
        IdleInput(),
        out_sock,
        {
            "station_id": "boat_001",
            "keepalive_interval": 30,
            "peer_timeout": 90,
            "session_refresh_interval": 0,
        },
        _proxy_confirmed_session(proxy),
        remote_addr,
    )

    assert reason == proxy.SESSION_END_PROACTIVE_REKEY
    assert clock[0] == 60
    assert not out_sock.response_pending
    assert len(out_sock.sent) == 1
    assert all(
        packet.startswith(proxy.DATA_PREFIX)
        for packet, _ in out_sock.sent
    )


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
        _proxy_confirmed_session(proxy),
        ("192.0.2.10", 17777),
    )

    assert reason == proxy.SESSION_END_SOCKET_ERROR


def test_proxy_healthy_ping_pong_runs_past_old_refresh_interval(monkeypatch):
    proxy = load_proxy_module()
    client_to_server_key = b"\x01" * 32
    server_to_client_key = b"\x02" * 32
    locator = _fresh_test_locator()
    confirmed_session = _proxy_confirmed_session(
        proxy,
        session_locator=locator,
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
                locator,
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
                    locator,
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
        confirmed_session,
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
        secure.parse_data_packet(b"not secure data")


def test_secure_data_packet_parser_rejects_only_data_prefix(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)

    with pytest.raises(ValueError):
        secure.parse_data_packet(secure.DATA_PREFIX)


def test_secure_data_packet_parser_rejects_nonce_without_gcm_tag(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)

    with pytest.raises(ValueError):
        secure.parse_data_packet(
            secure.DATA_PREFIX + (b"\x00" * secure.SESSION_LOCATOR_BYTES)
            + (b"\x00" * 12)
        )


def test_secure_data_packet_parser_accepts_minimum_structural_packet(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    locator = _fresh_test_locator()
    nonce = b"\x01" * 12
    ciphertext_and_tag = b"\x02" * 16

    parsed_locator, parsed_nonce, parsed_ciphertext = secure.parse_data_packet(
        secure.DATA_PREFIX + locator + nonce + ciphertext_and_tag
    )

    assert parsed_locator == locator
    assert parsed_nonce == nonce
    assert parsed_ciphertext == ciphertext_and_tag


def test_secure_data_packet_parser_output_decrypts_valid_proxy_packet(monkeypatch):
    proxy = load_proxy_module()
    secure = load_secure_module_with_fake_keys(monkeypatch)
    key = b"\x01" * 32
    locator = _fresh_test_locator()
    message = {"type": "nmea", "payload": "!AIVDM,1,1,,A,payload,0*00"}
    encrypted = proxy.encrypt_secure_json_message(message, key, locator)

    parsed_locator, nonce, ciphertext = secure.parse_data_packet(encrypted)

    assert parsed_locator == locator
    assert (
        AESGCM(key).decrypt(
            nonce, ciphertext, secure.build_data_aad(parsed_locator)
        )
        == secure.json.dumps(message, separators=(",", ":")).encode()
    )


def test_session_ttl_seconds_is_300(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)

    assert secure.SESSION_TTL_SECONDS == 300
    assert secure.SESSION_MAX == 100000
    assert secure.PENDING_SESSION_TTL_SECONDS == 30
    assert secure.PENDING_SESSION_MAX == secure.SESSION_MAX


def test_data_nonce_constants(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)

    assert not hasattr(secure, "DATA_NONCE_TTL_SECONDS")
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
        _relation_key(secure, addr),
        "boat_001",
        _fresh_test_locator(),
        client_to_server_aesgcm,
        server_to_client_aesgcm,
        now=100.0)

    assert session.station_id == "boat_001"
    assert session.current_epoch.client_to_server_aesgcm is client_to_server_aesgcm
    assert session.current_epoch.server_to_client_aesgcm is server_to_client_aesgcm
    assert session.created_at == 100.0
    assert session.last_seen == 100.0
    assert len(session.current_epoch.seen_data_nonces) == 0
    assert tuple(state._sessions) == (session._session_key,)
    assert state.stats().sessions_created == 1


def test_data_nonce_accepts_first_validated_nonce(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    session = state.install_session(
        _relation_key(secure, ("192.0.2.10", 50000)),
        "boat_001",
        _fresh_test_locator(),
        object(),
        object(),
        now=100.0)
    nonce = b"\x01" * 12

    assert not state.data_nonce_seen(session, nonce, now=100.0)
    assert state.accept_data_nonce(session, nonce, now=100.0)
    stats = state.stats()
    assert stats.data_nonces_accepted == 1
    assert stats.data_nonce_replays == 0
    assert stats.current_data_nonces == 1
    assert stats.peak_data_nonces == 1


def test_data_nonce_replay_is_retained_for_session_lifetime(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(session_ttl=20_000.0)
    session = state.install_session(
        _relation_key(secure, ("192.0.2.10", 50000)),
        "boat_001",
        _fresh_test_locator(),
        object(),
        object(),
        now=100.0)
    nonce = b"\x01" * 12

    assert state.accept_data_nonce(session, nonce, now=100.0)
    assert state.data_nonce_seen(session, nonce, now=120.0)
    assert state.data_nonce_seen(session, nonce, now=159.999)
    assert state.data_nonce_seen(session, nonce, now=10_000.0)
    assert not state.accept_data_nonce(session, nonce, now=10_000.0)

    stats = state.stats()
    assert stats.data_nonces_accepted == 1
    assert stats.data_nonce_replays == 4
    assert stats.data_nonces_expired == 0
    assert stats.current_data_nonces == 1


def test_data_nonce_lookup_does_not_expire_retained_records(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(session_ttl=1000.0)
    session = state.install_session(
        _relation_key(secure, ("192.0.2.10", 50000)),
        "boat_001",
        _fresh_test_locator(),
        object(),
        object(),
        now=0.0)
    first_nonce = b"\x01" * 12
    second_nonce = b"\x02" * 12

    assert state.accept_data_nonce(session, first_nonce, now=0.0)
    assert state.accept_data_nonce(session, second_nonce, now=20.0)
    assert not state.data_nonce_seen(session, b"\x03" * 12, now=900.0)

    assert set(session.current_epoch.seen_data_nonces._live_by_key) == {
        first_nonce,
        second_nonce,
    }
    stats = state.stats()
    assert stats.data_nonces_expired == 0
    assert stats.current_data_nonces == 2


def test_data_nonce_admission_checks_replay_before_capacity(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(
        data_nonce_max_per_session=2,
    )
    session = state.install_session(
        _relation_key(secure, ("192.0.2.10", 50000)),
        "boat_001",
        _fresh_test_locator(),
        object(),
        object(),
        now=100.0)
    one = b"\x01" * 12
    two = b"\x02" * 12
    three = b"\x03" * 12

    assert state.admit_data_nonce(
        session, one, now=100.0
    ) is secure._DataNonceAdmission.ACCEPTED
    assert state.admit_data_nonce(
        session, two, now=101.0
    ) is secure._DataNonceAdmission.ACCEPTED
    assert state.admit_data_nonce(
        session, one, now=102.0
    ) is secure._DataNonceAdmission.REPLAY

    assert (
        _active_session_at(secure, state, session.path_state.active_path)
        is session
    )
    assert set(session.current_epoch.seen_data_nonces._live_by_key) == {one, two}
    assert state.admit_data_nonce(
        session, three, now=103.0
    ) is secure._DataNonceAdmission.EXHAUSTED
    assert (
        _active_session_at(secure, state, session.path_state.active_path)
        is None
    )
    assert len(session.current_epoch.seen_data_nonces) == 0
    stats = state.stats()
    assert stats.data_nonce_replays == 1
    assert stats.data_nonce_exhaustions == 1
    assert stats.data_nonces_capacity_evicted == 0
    assert stats.data_nonces_expired == 0
    assert stats.data_nonces_session_discarded == 2
    assert stats.current_data_nonces == 0
    assert stats.peak_data_nonces == 2


def test_data_nonce_exhaustion_removes_only_exact_owning_session(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(
        data_nonce_max_per_session=1,
        max_sessions=2,
    )
    exhausted = state.install_session(
        _relation_key(secure, ("192.0.2.10", 50000)),
        "boat_001",
        _fresh_test_locator(),
        object(),
        object(),
        now=0.0)
    other = state.install_session(
        _relation_key(secure, ("192.0.2.11", 50001)),
        "boat_002",
        _fresh_test_locator(),
        object(),
        object(),
        now=0.0)
    retained = b"\x01" * 12
    triggering = b"\x02" * 12
    assert state.accept_data_nonce(exhausted, retained, now=0.0)

    assert state.admit_data_nonce(
        exhausted, triggering, now=1.0
    ) is secure._DataNonceAdmission.EXHAUSTED

    assert (
        _active_session_at(secure, state, exhausted.path_state.active_path)
        is None
    )
    assert (
        _active_session_at(secure, state, other.path_state.active_path)
        is other
    )
    stats = state.stats()
    assert stats.data_nonce_exhaustions == 1
    assert stats.data_nonces_capacity_evicted == 0


def test_data_nonce_caches_are_independent_per_session(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    first = state.install_session(
        _relation_key(secure, ("192.0.2.10", 50000)),
        "boat_001",
        _fresh_test_locator(),
        object(),
        object(),
        now=100.0)
    second = state.install_session(
        _relation_key(secure, ("192.0.2.11", 50001)),
        "boat_002",
        _fresh_test_locator(),
        object(),
        object(),
        now=100.0)
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
        _relation_key(secure, addr),
        "boat_001",
        _fresh_test_locator(),
        object(),
        object(),
        now=100.0)

    assert state.get_active_session(session._session_key, now=129.999) is session
    assert state.get_active_session(session._session_key, now=130.0) is None
    stats = state.stats()
    assert stats.sessions_expired == 1
    assert stats.current_sessions == 0


def test_get_active_session_returns_none_for_missing_session(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    missing_key = secure._EndpointSessionKey(
        _test_endpoint_token(secure), _fresh_test_locator()
    )

    assert state.get_active_session(missing_key, now=120.0) is None
    assert state.stats().sessions_expired == 0


def test_touch_session_updates_lru_order_without_changing_creation(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    first_addr = ("192.0.2.10", 50000)
    second_addr = ("192.0.2.11", 50001)
    first = state.install_session(
        _relation_key(secure, first_addr),
        "first",
        _fresh_test_locator(),
        object(),
        object(),
        now=100.0)
    second = state.install_session(
        _relation_key(secure, second_addr),
        "second",
        _fresh_test_locator(),
        object(),
        object(),
        now=110.0)

    assert tuple(state._sessions) == (
        first._session_key,
        second._session_key,
    )
    assert state.touch_session(first, now=125.0)

    assert first.created_at == 100.0
    assert first.last_seen == 125.0
    assert tuple(state._sessions) == (
        second._session_key,
        first._session_key,
    )
    assert state.stats().sessions_touched == 1


def test_session_cleanup_removes_expired_lru_prefix_and_stops_at_live(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(session_ttl=30.0)
    expired_addr = ("192.0.2.10", 50000)
    active_addr = ("192.0.2.11", 50001)
    expired = state.install_session(
        _relation_key(secure, expired_addr),
        "expired",
        _fresh_test_locator(),
        object(),
        object(),
        now=90.0)
    active = state.install_session(
        _relation_key(secure, active_addr),
        "active",
        _fresh_test_locator(),
        object(),
        object(),
        now=100.0)

    removed = state.cleanup_expired_sessions(now=120.0)

    assert removed == [expired._session_key]
    assert tuple(state._sessions) == (active._session_key,)
    assert state.stats().sessions_expired == 1


def test_install_session_never_admits_two_threads_into_its_transaction_at_once(
    monkeypatch,
):
    """Deterministic mutual-exclusion proof for Section 3, independent of
    real network timing: proves by direct measurement (not by a blocked/
    finished proxy signal, which an artificially-serializing test double
    can satisfy even with the lock removed -- a double that unconditionally
    parks every caller on the same gate blocks a second caller whether or
    not any real lock exists) that `install_session()`'s capacity check-
    evict-insert transaction never runs on two threads at once. Without
    `SecureState`'s own lock spanning that whole sequence, a second thread
    could observe stale `len(self._sessions)` before the first thread's
    eviction-or-insert commits, letting both installs believe there is
    room under the cap at once -- exactly the aggregate-capacity race
    `test_real_concurrent_handshakes_respect_aggregate_session_capacity`
    (tests/test_udpsec_security_validation.py) exercises over real
    sockets. This test proves the same property without depending on
    real-thread scheduling luck, and verifies the instrumentation itself
    against a deliberately-unlocked control."""
    import threading
    import time

    secure = load_secure_module_with_fake_keys(monkeypatch)

    def run(use_broken_lock):
        state = secure.SecureState(max_sessions=100)
        if use_broken_lock:
            class NullLock:
                def __enter__(self):
                    return self

                def __exit__(self, *_exc_info):
                    return False

            state._lock = NullLock()

        concurrent = 0
        peak = [0]
        counter_lock = threading.Lock()
        original_locator_is_free = state._locator_is_free

        def instrumented_locator_is_free(endpoint_token, candidate):
            nonlocal concurrent
            with counter_lock:
                concurrent += 1
                peak[0] = max(peak[0], concurrent)
            time.sleep(0.03)
            with counter_lock:
                concurrent -= 1
            return original_locator_is_free(endpoint_token, candidate)

        state._locator_is_free = instrumented_locator_is_free

        thread_count = 8
        barrier = threading.Barrier(thread_count)

        def worker(index):
            barrier.wait(timeout=5.0)
            state.install_session(
                _relation_key(secure, (f"192.0.2.{index}", 50000 + index)),
                f"station-{index}", _fresh_test_locator(), object(),
                object(), now=0.0)

        threads = [
            threading.Thread(target=worker, args=(index,))
            for index in range(thread_count)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10.0)
        return peak[0], state

    peak_locked, locked_state = run(use_broken_lock=False)
    assert peak_locked == 1
    assert len(locked_state._sessions) == 8

    peak_unlocked, _ = run(use_broken_lock=True)
    assert peak_unlocked > 1, (
        "the deliberately-unlocked control did not exhibit real concurrent "
        "entry -- this instrumentation would not catch a missing lock"
    )


def test_concurrent_colliding_locator_installs_have_exactly_one_winner_per_endpoint(
    monkeypatch,
):
    """Many real threads race to `install_session()` using the exact same
    raw locator bytes on the SAME `endpoint_token`, each at a distinct
    relation (so this exercises the locator-collision preflight, not the
    same-relation-replacement path): exactly one must win and every other
    must fail closed with `SessionLocatorCollisionError`, leaving no
    partial or duplicate installation behind. A concurrent thread racing
    with the exact same raw locator bytes on a DIFFERENT `endpoint_token`
    must be completely unaffected -- collision scoping is per
    `endpoint_token`, not global, even under real concurrent contention."""
    import threading

    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(max_sessions=100)
    endpoint_a = secure._new_endpoint_token()
    endpoint_b = secure._new_endpoint_token()
    shared_locator = _fresh_test_locator()
    thread_count_a = 8

    barrier = threading.Barrier(thread_count_a + 1)
    results = [None] * thread_count_a
    errors = [None] * thread_count_a

    def contend_on_endpoint_a(index):
        barrier.wait(timeout=5.0)
        try:
            results[index] = state.install_session(
                _relation_key(secure, (f"192.0.2.{index}", 50000 + index),
                              endpoint_a),
                f"station-{index}", shared_locator, object(), object(),
                now=0.0)
        except secure.SessionLocatorCollisionError as exc:
            errors[index] = exc

    b_result = {}

    def contend_on_endpoint_b():
        barrier.wait(timeout=5.0)
        b_result["session"] = state.install_session(
            _relation_key(secure, ("192.0.2.200", 50200), endpoint_b),
            "station-b", shared_locator, object(), object(), now=0.0)

    threads = [
        threading.Thread(target=contend_on_endpoint_a, args=(index,))
        for index in range(thread_count_a)
    ] + [threading.Thread(target=contend_on_endpoint_b)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10.0)

    winners = [result for result in results if result is not None]
    collisions = [error for error in errors if error is not None]
    assert len(winners) == 1
    assert len(collisions) == thread_count_a - 1
    assert all(
        isinstance(error, secure.SessionLocatorCollisionError)
        for error in collisions
    )
    assert b_result["session"] is not None
    assert b_result["session"].session_handle != winners[0].session_handle

    live_on_a = [
        session for session in state._sessions.values()
        if session._session_key.endpoint_token is endpoint_a
    ]
    live_on_b = [
        session for session in state._sessions.values()
        if session._session_key.endpoint_token is endpoint_b
    ]
    assert live_on_a == [winners[0]]
    assert live_on_b == [b_result["session"]]
    assert state.stats().sessions_created == 2


def test_concurrent_duplicate_nonce_admission_has_exactly_one_acceptance(
    monkeypatch,
):
    """Many real threads submit the EXACT SAME DATA nonce for one live
    session at the same time, through the real production
    `admit_data_nonce()` transaction (not a copy of the underlying bounded
    set exercised in isolation). Exactly one must be admitted; every other
    concurrent submission of the identical nonce must be rejected as a
    replay, with exact, non-duplicated accounting -- proving the
    replay-check-and-record step is one atomic transaction rather than
    three separable reads/writes a second thread could interleave with."""
    import threading

    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    session, _, _ = _install_test_session(
        secure, state, ("192.0.2.10", 50000), b"\x01" * 32, b"\x02" * 32,
    )
    nonce = b"\x09" * 12
    thread_count = 10
    barrier = threading.Barrier(thread_count)
    admissions = [None] * thread_count

    def worker(index):
        barrier.wait(timeout=5.0)
        admissions[index] = state.admit_data_nonce(session, nonce, now=0.0)

    threads = [
        threading.Thread(target=worker, args=(index,))
        for index in range(thread_count)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10.0)

    accepted = [
        a for a in admissions if a is secure._DataNonceAdmission.ACCEPTED
    ]
    replayed = [
        a for a in admissions if a is secure._DataNonceAdmission.REPLAY
    ]
    assert len(accepted) == 1
    assert len(replayed) == thread_count - 1
    stats = state.stats()
    assert stats.data_nonces_accepted == 1
    assert stats.data_nonce_replays == thread_count - 1
    assert stats.current_data_nonces == 1
    assert state.is_live_session_handle(session, now=0.0)


def test_concurrent_nonce_exhaustion_removes_the_session_exactly_once(
    monkeypatch,
):
    """Many real threads each submit a DISTINCT DATA nonce concurrently
    against a session whose nonce capacity is already full (so every
    submission is a genuine capacity-exhaustion event, not a replay of one
    shared nonce). The real `admit_data_nonce()` transaction must remove
    the exhausted session exactly once -- whichever thread's transaction
    wins the race sees EXHAUSTED and performs the one real removal, and
    every other thread's transaction, once it observes the session is no
    longer live, must report STALE rather than re-removing an already-gone
    session or resurrecting/duplicating any lifecycle accounting. No
    thread may see or touch a session another thread already removed as
    if it were still live."""
    import threading

    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(data_nonce_max_per_session=1)
    session, _, _ = _install_test_session(
        secure, state, ("192.0.2.10", 50000), b"\x01" * 32, b"\x02" * 32,
    )
    assert state.accept_data_nonce(session, b"\x00" * 12, now=0.0)

    thread_count = 10
    barrier = threading.Barrier(thread_count)
    admissions = [None] * thread_count

    def worker(index):
        distinct_nonce = bytes([index + 1]) + b"\x00" * 11
        barrier.wait(timeout=5.0)
        admissions[index] = state.admit_data_nonce(
            session, distinct_nonce, now=1.0
        )

    threads = [
        threading.Thread(target=worker, args=(index,))
        for index in range(thread_count)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10.0)

    exhausted = [
        a for a in admissions if a is secure._DataNonceAdmission.EXHAUSTED
    ]
    stale = [
        a for a in admissions if a is secure._DataNonceAdmission.STALE
    ]
    assert len(exhausted) == 1
    assert len(stale) == thread_count - 1
    stats = state.stats()
    assert stats.data_nonce_exhaustions == 1
    assert stats.sessions_capacity_evicted == 0
    assert len(state._sessions) == 0
    assert not secure._SESSION_IDENTITY_REGISTRY.is_live(session.session_handle)


def test_session_capacity_evicts_least_recently_seen(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(max_sessions=2)
    first_addr = ("192.0.2.10", 50000)
    second_addr = ("192.0.2.11", 50001)
    third_addr = ("192.0.2.12", 50002)
    first = state.install_session(
        _relation_key(secure, first_addr),
        "first",
        _fresh_test_locator(),
        object(),
        object(),
        now=100.0)
    state.install_session(
        _relation_key(secure, second_addr),
        "second",
        _fresh_test_locator(),
        object(),
        object(),
        now=110.0)
    assert state.touch_session(first, now=120.0)

    third = state.install_session(
        _relation_key(secure, third_addr),
        "third",
        _fresh_test_locator(),
        object(),
        object(),
        now=130.0)

    assert tuple(state._sessions) == (
        first._session_key,
        third._session_key,
    )
    stats = state.stats()
    assert stats.sessions_capacity_evicted == 1
    assert stats.sessions_expired == 0
    assert stats.current_sessions == 2
    assert stats.peak_sessions == 2


def test_concurrent_close_session_calls_on_the_same_session_are_idempotent(
    monkeypatch,
):
    """Many real threads call `close_session()` on the exact same live
    session object at once. Exactly one call may actually perform the
    removal and count it; every other concurrent call must see the
    session already gone (via the same exact-object stale-handle check
    used everywhere else) and report `False` without a second removal, a
    doubled `sessions_closed` count, or a corrupted registry reservation."""
    import threading

    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    session, _, _ = _install_test_session(
        secure, state, ("192.0.2.10", 50000), b"\x01" * 32, b"\x02" * 32,
    )

    thread_count = 10
    barrier = threading.Barrier(thread_count)
    results = [None] * thread_count

    def worker(index):
        barrier.wait(timeout=5.0)
        results[index] = state.close_session(session, now=0.0)

    threads = [
        threading.Thread(target=worker, args=(index,))
        for index in range(thread_count)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10.0)

    assert results.count(True) == 1
    assert results.count(False) == thread_count - 1
    stats = state.stats()
    assert stats.sessions_closed == 1
    assert stats.current_sessions == 0
    assert not secure._SESSION_IDENTITY_REGISTRY.is_live(
        session.session_handle
    )
    assert secure._SESSION_IDENTITY_REGISTRY.is_retiring(
        session.session_handle
    )


def test_equal_session_timestamps_use_deterministic_activity_order(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(max_sessions=2)
    first_addr = ("192.0.2.10", 50000)
    second_addr = ("192.0.2.11", 50001)
    third_addr = ("192.0.2.12", 50002)
    state.install_session(
        _relation_key(secure, first_addr),
        "first",
        _fresh_test_locator(),
        object(),
        object(),
        now=100.0)
    second = state.install_session(
        _relation_key(secure, second_addr),
        "second",
        _fresh_test_locator(),
        object(),
        object(),
        now=100.0)

    third = state.install_session(
        _relation_key(secure, third_addr),
        "third",
        _fresh_test_locator(),
        object(),
        object(),
        now=100.0)

    assert tuple(state._sessions) == (
        second._session_key,
        third._session_key,
    )
    assert state.stats().sessions_capacity_evicted == 1


def test_expired_sessions_are_removed_before_capacity_eviction(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(session_ttl=30.0, max_sessions=2)
    expired_addr = ("192.0.2.10", 50000)
    active_addr = ("192.0.2.11", 50001)
    new_addr = ("192.0.2.12", 50002)
    state.install_session(
        _relation_key(secure, expired_addr),
        "expired",
        _fresh_test_locator(),
        object(),
        object(),
        now=90.0)
    active = state.install_session(
        _relation_key(secure, active_addr),
        "active",
        _fresh_test_locator(),
        object(),
        object(),
        now=100.0)

    new = state.install_session(
        _relation_key(secure, new_addr),
        "new",
        _fresh_test_locator(),
        object(),
        object(),
        now=120.0)

    assert tuple(state._sessions) == (
        active._session_key,
        new._session_key,
    )
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
        _relation_key(secure, replaced_addr),
        "old",
        _fresh_test_locator(),
        old_client_to_server_aesgcm,
        old_server_to_client_aesgcm,
        now=100.0)
    other = state.install_session(
        _relation_key(secure, other_addr),
        "other",
        _fresh_test_locator(),
        object(),
        object(),
        now=110.0)
    nonce = b"\x01" * 12
    assert state.accept_data_nonce(old, nonce, now=115.0)

    new = state.install_session(
        _relation_key(secure, replaced_addr),
        "new",
        _fresh_test_locator(),
        new_client_to_server_aesgcm,
        new_server_to_client_aesgcm,
        now=120.0)

    assert new is _active_session_at(secure, state, replaced_addr)
    assert new is not old
    assert (
        new.current_epoch.client_to_server_aesgcm
        is new_client_to_server_aesgcm
    )
    assert (
        new.current_epoch.server_to_client_aesgcm
        is new_server_to_client_aesgcm
    )
    assert tuple(state._sessions) == (
        other._session_key,
        new._session_key,
    )
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
        _relation_key(secure, addr),
        "old",
        _fresh_test_locator(),
        object(),
        object(),
        now=100.0)
    assert state.accept_data_nonce(old, b"\x01" * 12, now=100.0)

    new = state.install_session(
        _relation_key(secure, addr),
        "new",
        _fresh_test_locator(),
        object(),
        object(),
        now=130.0)

    assert new is _active_session_at(secure, state, addr)
    stats = state.stats()
    assert stats.sessions_created == 2
    assert stats.sessions_replaced == 0
    assert stats.sessions_expired == 1
    assert stats.data_nonces_session_discarded == 1


def test_session_capacity_discard_counts_retained_nonces_once(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(max_sessions=1)
    first = state.install_session(
        _relation_key(secure, ("192.0.2.10", 50000)),
        "first",
        _fresh_test_locator(),
        object(),
        object(),
        now=100.0)
    assert state.accept_data_nonce(first, b"\x01" * 12, now=100.0)
    assert state.accept_data_nonce(first, b"\x02" * 12, now=101.0)

    state.install_session(
        _relation_key(secure, ("192.0.2.11", 50001)),
        "second",
        _fresh_test_locator(),
        object(),
        object(),
        now=102.0)

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
        _relation_key(secure, old_addr),
        "old",
        _fresh_test_locator(),
        object(),
        object(),
        now=100.0)
    assert state.accept_data_nonce(old, b"\x01" * 12, now=100.0)

    state.install_session(
        _relation_key(secure, ("192.0.2.11", 50001)),
        "new",
        _fresh_test_locator(),
        object(),
        object(),
        now=101.0)
    before = state.stats()

    assert not state.data_nonce_seen(old, b"\x01" * 12, now=102.0)
    assert not state.accept_data_nonce(old, b"\x02" * 12, now=102.0)
    assert state.stats() == before
    assert state.stats().current_data_nonces == 0


def test_same_address_stale_session_handle_cannot_admit_or_exhaust_nonce(
    monkeypatch,
):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(data_nonce_max_per_session=1)
    addr = ("192.0.2.10", 50000)
    stale = state.install_session(
        _relation_key(secure, addr),
        "old",
        _fresh_test_locator(),
        object(),
        object(),
        now=100.0)
    replacement = state.install_session(
        _relation_key(secure, addr),
        "new",
        _fresh_test_locator(),
        object(),
        object(),
        now=101.0)
    before = state.stats()

    admission = state.admit_data_nonce(
        stale,
        b"\x01" * 12,
        now=102.0,
    )

    assert admission is secure._DataNonceAdmission.STALE
    assert _active_session_at(secure, state, addr) is replacement
    assert len(stale.current_epoch.seen_data_nonces) == 0
    assert len(replacement.current_epoch.seen_data_nonces) == 0
    assert state.stats() == before


def test_same_address_stale_pending_handle_cannot_admit_or_exhaust_nonce(
    monkeypatch,
):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(data_nonce_max_per_session=1)
    addr = ("192.0.2.10", 50000)
    stale = state.install_pending_session(
        _relation_key(secure, addr),
        "old",
        _fresh_test_locator(),
        object(),
        object(),
        now=100.0)
    replacement = state.install_pending_session(
        _relation_key(secure, addr),
        "new",
        _fresh_test_locator(),
        object(),
        object(),
        now=101.0)
    before = state.stats()

    admission = state.admit_pending_data_nonce(
        stale,
        b"\x01" * 12,
        now=102.0,
    )

    assert admission is secure._DataNonceAdmission.STALE
    assert state._pending_sessions[_relation_key(secure, addr)] is replacement
    assert len(stale.current_epoch.seen_data_nonces) == 0
    assert len(replacement.current_epoch.seen_data_nonces) == 0
    assert state.stats() == before


def test_stale_nonce_check_does_not_cleanup_unrelated_expired_session(
    monkeypatch,
):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(session_ttl=10.0, max_sessions=2)
    stale_addr = ("192.0.2.10", 50000)
    expiring_addr = ("192.0.2.11", 50001)
    stale = state.install_session(
        _relation_key(secure, stale_addr),
        "stale",
        _fresh_test_locator(),
        object(),
        object(),
        now=0.0)
    expiring = state.install_session(
        _relation_key(secure, expiring_addr),
        "expiring",
        _fresh_test_locator(),
        object(),
        object(),
        now=2.0)
    expiring_nonce = b"\x01" * 12
    assert state.accept_data_nonce(
        expiring, expiring_nonce, now=2.0
    )
    replacement = state.install_session(
        _relation_key(secure, stale_addr),
        "replacement",
        _fresh_test_locator(),
        object(),
        object(),
        now=3.0)
    assert replacement is not stale

    before_stats = state.stats()
    before_sessions = tuple(state._sessions.items())
    before_expiring_nonces = set(
        expiring.current_epoch.seen_data_nonces._live_by_key
    )
    before_stale_nonces = set(stale.current_epoch.seen_data_nonces._live_by_key)

    assert not state.data_nonce_seen(
        stale, b"\x02" * 12, now=12.0
    )

    assert state.stats() == before_stats
    assert tuple(state._sessions.items()) == before_sessions
    assert _active_session_at(secure, state, expiring_addr) is expiring
    assert (
        set(expiring.current_epoch.seen_data_nonces._live_by_key)
        == before_expiring_nonces
    )
    assert (
        set(stale.current_epoch.seen_data_nonces._live_by_key)
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
        _relation_key(secure, stale_addr),
        "stale",
        _fresh_test_locator(),
        object(),
        object(),
        now=0.0)
    assert state.accept_data_nonce(
        stale, b"\x01" * 12, now=0.0
    )
    expiring = state.install_session(
        _relation_key(secure, expiring_addr),
        "expiring",
        _fresh_test_locator(),
        object(),
        object(),
        now=2.0)
    expiring_nonce = b"\x02" * 12
    assert state.accept_data_nonce(
        expiring, expiring_nonce, now=2.0
    )
    state.install_session(
        _relation_key(secure, replacement_addr),
        "replacement",
        _fresh_test_locator(),
        object(),
        object(),
        now=3.0)
    assert _active_session_at(secure, state, stale_addr) is None

    before_stats = state.stats()
    before_sessions = tuple(state._sessions.items())
    before_expiring_nonces = set(
        expiring.current_epoch.seen_data_nonces._live_by_key
    )
    before_stale_nonces = set(stale.current_epoch.seen_data_nonces._live_by_key)

    assert not state.accept_data_nonce(
        stale, b"\x03" * 12, now=12.0
    )

    assert state.stats() == before_stats
    assert tuple(state._sessions.items()) == before_sessions
    assert _active_session_at(secure, state, expiring_addr) is expiring
    assert (
        set(expiring.current_epoch.seen_data_nonces._live_by_key)
        == before_expiring_nonces
    )
    assert (
        set(stale.current_epoch.seen_data_nonces._live_by_key)
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
        _relation_key(secure, stale_addr),
        "stale",
        _fresh_test_locator(),
        object(),
        object(),
        now=0.0)
    expiring = state.install_session(
        _relation_key(secure, expiring_addr),
        "expiring",
        _fresh_test_locator(),
        object(),
        object(),
        now=2.0)
    state.install_session(
        _relation_key(secure, stale_addr),
        "replacement",
        _fresh_test_locator(),
        object(),
        object(),
        now=3.0)

    before_stats = state.stats()
    before_sessions = tuple(state._sessions.items())

    assert not state.touch_session(stale, now=12.0)

    assert state.stats() == before_stats
    assert tuple(state._sessions.items()) == before_sessions
    assert _active_session_at(secure, state, expiring_addr) is expiring
    assert state.stats().sessions_touched == before_stats.sessions_touched


def test_expired_session_handle_cannot_accept_or_check_nonces(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(session_ttl=10.0)
    addr = ("192.0.2.10", 50000)
    session = state.install_session(
        _relation_key(secure, addr),
        "old",
        _fresh_test_locator(),
        object(),
        object(),
        now=0.0)
    assert state.accept_data_nonce(session, b"\x01" * 12, now=0.0)

    assert not state.data_nonce_seen(session, b"\x01" * 12, now=10.0)

    after_expiry = state.stats()
    assert _active_session_at(secure, state, addr) is None
    assert after_expiry.sessions_expired == 1
    assert after_expiry.data_nonces_session_discarded == 1
    assert after_expiry.data_nonces_accepted == 1
    assert after_expiry.current_sessions == 0
    assert after_expiry.current_data_nonces == 0

    assert not state.accept_data_nonce(
        session, b"\x02" * 12, now=10.0
    )
    assert not state.touch_session(session, now=10.0)
    assert state.stats() == after_expiry
    assert _active_session_at(secure, state, addr) is None


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
        "sessions_closed",
        "sessions_capacity_evicted",
        "pending_sessions_created",
        "pending_sessions_replaced",
        "pending_sessions_promoted",
        "pending_sessions_expired",
        "pending_sessions_capacity_evicted",
        "pending_sessions_closed",
        "data_nonces_accepted",
        "data_nonce_replays",
        "data_nonces_expired",
        "data_nonces_capacity_evicted",
        "data_nonce_exhaustions",
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
        _relation_key(secure, ("192.0.2.10", 50000)),
        "boat_001",
        _fresh_test_locator(),
        object(),
        object(),
        now=100.0)
    current = state.stats()
    assert initial.current_sessions == 0
    assert initial.sessions_created == 0
    assert current.current_sessions == 1
    assert current.sessions_created == 1


def test_secure_state_stats_do_not_read_clocks_or_cleanup(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(session_ttl=1.0)
    addr = ("192.0.2.10", 50000)
    session = state.install_session(
        _relation_key(secure, addr),
        "boat_001",
        _fresh_test_locator(),
        object(),
        object(),
        now=0.0)

    def fail_clock():
        raise AssertionError("stats must not read a clock")

    monkeypatch.setattr(secure.time, "time", fail_clock)
    monkeypatch.setattr(secure.time, "monotonic", fail_clock)

    stats = state.stats()

    assert stats.current_sessions == 1
    assert tuple(state._sessions) == (session._session_key,)
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
    endpoint_token=None,
):
    client_to_server_key, server_to_client_key = _d66_keys(marker)
    pending = state.install_pending_session(
        _relation_key(secure, addr, endpoint_token),
        station_id,
        _fresh_test_locator(),
        secure.AESGCM(client_to_server_key),
        secure.AESGCM(server_to_client_key),
        now)
    return pending, client_to_server_key, server_to_client_key


def _d66_confirmation_packet(
    secure,
    client_to_server_key,
    nonce,
    session_locator,
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
        session_locator,
    )


def _d66_decrypt_json(secure, packet, key):
    locator, nonce, ciphertext = secure.parse_data_packet(packet)
    plaintext = secure.AESGCM(key).decrypt(
        nonce,
        ciphertext,
        secure.build_data_aad(locator),
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
        protocol_version=client_hello.protocol_version,
        station_id=client_hello.station_id,
        timestamp=client_hello.timestamp,
        client_random=client_hello.client_random,
        client_ephemeral_public_key=(
            client_hello.client_ephemeral_public_key
        ),
        client_signature=client_hello.client_signature,
        session_locator=server_hello.session_locator,
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
    return key_material, fake_socket, server_hello.session_locator


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
        "_relation_key",
        "station_id",
        "session_locator",
        "created_at",
        "current_epoch",
    }
    assert set(vars(pending.current_epoch)) == {
        "client_to_server_aesgcm",
        "server_to_client_aesgcm",
        "seen_data_nonces",
        "created_at",
    }
    assert pending._address == addr
    assert pending.station_id == "boat_001"
    assert pending.created_at == 12.5
    assert (
        pending.current_epoch.client_to_server_aesgcm
        is not pending.current_epoch.server_to_client_aesgcm
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
    assert (
        state.get_active_session(
            secure._EndpointSessionKey(
                _test_endpoint_token(secure), pending.session_locator
            ),
            12.5,
        )
        is None
    )
    assert state.get_pending_session(_relation_key(secure, addr), 12.5) is pending
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

    assert expired == list(_relation_keys(secure, addresses[:2]))
    assert tuple(state._pending_sessions) == _relation_keys(
        secure, (addresses[2],)
    )
    assert state.cleanup_expired_pending_sessions(11.999) == []
    assert state.cleanup_expired_pending_sessions(12.0) == list(
        _relation_keys(secure, (addresses[2],))
    )
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
    assert tuple(state._pending_sessions) == _relation_keys(
        secure,
        (pending_addresses[1], pending_addresses[2]),
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

    assert tuple(state._pending_sessions) == _relation_keys(
        secure,
        (pending_addresses[2], pending_addresses[3]),
    )
    assert state._sessions == {active._session_key: active}
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

    assert _active_session_at(secure, state, addr) is active
    assert state._pending_sessions[_relation_key(secure, addr)] is second_pending
    assert len(first_pending.current_epoch.seen_data_nonces) == 0
    assert active.current_epoch.seen_data_nonces.contains(active_nonce)
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
    assert state.cleanup_expired_pending_sessions(32.0) == [
        _relation_key(secure, addr)
    ]
    assert _active_session_at(secure, state, addr) is active
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

    assert state.promote_pending_session(stale, 3.0) is None
    assert _active_session_at(secure, state, addr) is active
    assert state._pending_sessions[_relation_key(secure, addr)] is current
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
    transferred_cache = pending.current_epoch.seen_data_nonces

    promoted = state.promote_pending_session(pending, 2.0)

    assert promoted is _active_session_at(secure, state, addr)
    assert _relation_key(secure, addr) not in state._pending_sessions
    assert promoted.current_epoch.seen_data_nonces is transferred_cache
    assert promoted.current_epoch.seen_data_nonces.contains(
        confirmation_nonce,
    )
    assert len(old_active.current_epoch.seen_data_nonces) == 0
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

    key_material, _, _ = _d66_run_authenticated_handshake(
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

    pending = state._pending_sessions[_relation_key(secure, addr)]
    assert (
        state.get_active_session(
            secure._EndpointSessionKey(
                _test_endpoint_token(secure), pending.session_locator
            ),
            100.0,
        )
        is None
    )
    assert (
        pending.current_epoch.client_to_server_aesgcm
        is not pending.current_epoch.server_to_client_aesgcm
    )
    nonce = b"\x31" * 12
    plaintext = b"directional pending key check"
    data_aad = secure.build_data_aad(pending.session_locator)
    ciphertext = secure.AESGCM(
        key_material.client_to_server_key
    ).encrypt(nonce, plaintext, data_aad)
    assert pending.current_epoch.client_to_server_aesgcm.decrypt(
        nonce,
        ciphertext,
        data_aad,
    ) == plaintext
    with pytest.raises(InvalidTag):
        pending.current_epoch.server_to_client_aesgcm.decrypt(
            nonce,
            ciphertext,
            data_aad,
        )
    for forbidden_name in (
        "ephemeral_private_key",
        "shared_secret",
        "session_transcript_hash",
    ):
        assert not hasattr(pending, forbidden_name)

    assert state.cleanup_expired_pending_sessions(129.999) == []
    assert state.cleanup_expired_pending_sessions(130.0) == [
        _relation_key(secure, addr)
    ]
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
        pending.session_locator,
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
    assert _relation_key(secure, addr) not in state._pending_sessions
    active = _active_session_at(secure, state, addr)
    assert active.current_epoch.seen_data_nonces is pending.current_epoch.seen_data_nonces
    assert active.current_epoch.seen_data_nonces.contains(nonce)
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


def test_d66_pending_confirmation_requires_exact_listener_owner(
    monkeypatch,
):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    addr = ("127.0.0.1", 51084)
    active_client_key, active_server_key = _d66_keys(2)
    old_active, _, _ = _install_test_session(
        secure,
        state,
        addr,
        active_client_key,
        active_server_key,
        now=0.0,
    )
    stale_pending, _, _ = _d66_install_pending(
        secure,
        state,
        addr,
        marker=30,
        now=0.25,
    )
    pending, client_to_server_key, server_to_client_key = _d66_install_pending(
        secure,
        state,
        addr,
        marker=40,
        now=0.5,
    )
    nonce = b"\x44" * 12
    packet = _d66_confirmation_packet(
        secure,
        client_to_server_key,
        nonce,
        pending.session_locator,
    )
    relation_key = _relation_key(secure, addr)
    other_active = {old_active._session_key: old_active}
    other_pending = {relation_key: stale_pending}

    queue, fake_socket = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(packet, addr)],
        state=state,
        monotonic_clock=_FakeClock(1.0),
        owned_sessions=other_active,
        owned_pending_sessions=other_pending,
    )

    assert queue.items == []
    assert fake_socket.sent == []
    assert _active_session_at(secure, state, addr) is old_active
    assert state._pending_sessions[_relation_key(secure, addr)] is pending
    assert other_active == {old_active._session_key: old_active}
    assert other_pending == {relation_key: stale_pending}
    assert stale_pending is not pending
    assert not pending.current_epoch.seen_data_nonces.contains(nonce)
    stats = state.stats()
    assert stats.pending_sessions_replaced == 1
    assert stats.pending_sessions_promoted == 0
    assert stats.sessions_touched == 0
    assert stats.data_nonces_accepted == 0

    owner_active = {}
    owner_pending = {relation_key: pending}
    queue, fake_socket = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(packet, addr)],
        state=state,
        monotonic_clock=_FakeClock(1.0),
        owned_sessions=owner_active,
        owned_pending_sessions=owner_pending,
    )

    assert queue.items == []
    assert len(fake_socket.sent) == 1
    assert _d66_decrypt_json(
        secure,
        fake_socket.sent[0][0],
        server_to_client_key,
    )["seq"] == secure.SESSION_CONFIRMATION_SEQUENCE
    replacement = _active_session_at(secure, state, addr)
    assert replacement is not old_active
    assert owner_active == {replacement._session_key: replacement}
    assert owner_pending == {}
    assert _relation_key(secure, addr) not in state._pending_sessions
    assert replacement.current_epoch.seen_data_nonces.contains(nonce)
    assert state.stats().pending_sessions_promoted == 1
    assert state.stats().sessions_replaced == 1


def test_d66_duplicate_confirmation_nonce_promotes_and_responds_once(
    monkeypatch,
):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(data_nonce_max_per_session=1)
    addr = ("127.0.0.1", 51071)
    pending, client_to_server_key, _ = _d66_install_pending(
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
        pending.session_locator,
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
    assert state.stats().data_nonce_exhaustions == 0
    assert state.stats().current_data_nonces == 1
    assert state.stats().current_sessions == 1


def test_d66_confirmation_nonce_counts_toward_active_capacity(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(data_nonce_max_per_session=2)
    addr = ("127.0.0.1", 51074)
    pending, client_to_server_key, _ = _d66_install_pending(
        secure,
        state,
        addr,
        marker=40,
        now=0.0,
    )
    confirmation_nonce = b"\x45" * 12
    accepted_nonce = b"\x46" * 12
    exhausting_nonce = b"\x47" * 12
    confirmation = _d66_confirmation_packet(
        secure,
        client_to_server_key,
        confirmation_nonce,
        pending.session_locator,
    )

    confirmation_queue, confirmation_socket = (
        _run_secure_server_with_packets(
            monkeypatch,
            secure,
            [(confirmation, addr)],
            state=state,
            monotonic_clock=_FakeClock(1.0),
        )
    )
    active = _active_session_at(secure, state, addr)
    assert confirmation_queue.items == []
    assert len(confirmation_socket.sent) == 1
    assert active.current_epoch.seen_data_nonces.contains(confirmation_nonce)
    assert len(active.current_epoch.seen_data_nonces) == 1

    accepted_packet = _encrypted_data_packet(
        secure,
        client_to_server_key,
        accepted_nonce,
        active._session_key.session_locator,
        payload="!AIVDM,1,1,,A,accepted,0*00",
    )
    accepted_queue, _ = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(accepted_packet, addr)],
        state=state,
        monotonic_clock=_FakeClock(2.0),
    )
    assert len(accepted_queue.items) == 1
    assert active.current_epoch.seen_data_nonces.contains(confirmation_nonce)
    assert active.current_epoch.seen_data_nonces.contains(accepted_nonce)
    assert len(active.current_epoch.seen_data_nonces) == 2

    exhausting_packet = _encrypted_data_packet(
        secure,
        client_to_server_key,
        exhausting_nonce,
        active._session_key.session_locator,
        payload="!AIVDM,1,1,,A,must-not-queue,0*00",
    )
    exhausting_queue, exhausting_socket = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(exhausting_packet, addr)],
        state=state,
        monotonic_clock=_FakeClock(3.0),
    )

    stats = state.stats()
    assert exhausting_queue.items == []
    assert exhausting_socket.sent == []
    assert _active_session_at(secure, state, addr) is None
    assert len(active.current_epoch.seen_data_nonces) == 0
    assert stats.pending_sessions_promoted == 1
    assert stats.data_nonces_accepted == 2
    assert stats.data_nonce_exhaustions == 1
    assert stats.data_nonces_session_discarded == 2
    assert stats.current_data_nonces == 0
    assert stats.sessions_touched == 2


def test_d66_pending_nonce_exhaustion_preserves_previous_active(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(data_nonce_max_per_session=1)
    addr = ("127.0.0.1", 51075)
    active_client_key, active_server_key = _d66_keys(2)
    active, _, _ = _install_test_session(
        secure,
        state,
        addr,
        active_client_key,
        active_server_key,
        now=0.0,
    )
    active_nonce = b"\x48" * 12
    assert state.admit_data_nonce(
        active,
        active_nonce,
        now=0.1,
    ) is secure._DataNonceAdmission.ACCEPTED
    pending, pending_client_key, _ = _d66_install_pending(
        secure,
        state,
        addr,
        marker=40,
        now=0.5,
    )
    retained_pending_nonce = b"\x49" * 12
    assert state.admit_pending_data_nonce(
        pending,
        retained_pending_nonce,
        now=0.6,
    ) is secure._DataNonceAdmission.ACCEPTED
    confirmation = _d66_confirmation_packet(
        secure,
        pending_client_key,
        b"\x4a" * 12,
        pending.session_locator,
    )
    relation_key = _relation_key(secure, addr)
    owned_sessions = {active._session_key: active}
    owned_pending_sessions = {relation_key: pending}

    queue, fake_socket = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(confirmation, addr)],
        state=state,
        monotonic_clock=_FakeClock(1.0),
        owned_sessions=owned_sessions,
        owned_pending_sessions=owned_pending_sessions,
    )

    stats = state.stats()
    assert queue.items == []
    assert fake_socket.sent == []
    assert _active_session_at(secure, state, addr) is active
    assert _relation_key(secure, addr) not in state._pending_sessions
    assert owned_sessions == {active._session_key: active}
    assert owned_pending_sessions == {}
    assert active.last_seen == 0.0
    assert active.current_epoch.seen_data_nonces.contains(active_nonce)
    assert len(active.current_epoch.seen_data_nonces) == 1
    assert len(pending.current_epoch.seen_data_nonces) == 0
    assert stats.pending_sessions_promoted == 0
    assert stats.pending_sessions_replaced == 0
    assert stats.pending_sessions_expired == 0
    assert stats.pending_sessions_capacity_evicted == 0
    assert stats.sessions_replaced == 0
    assert stats.sessions_touched == 0
    assert stats.data_nonces_accepted == 2
    assert stats.data_nonce_exhaustions == 1
    assert stats.data_nonces_session_discarded == 1
    assert stats.current_sessions == 1
    assert stats.current_pending_sessions == 0
    assert stats.current_data_nonces == 1


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
        active._session_key.session_locator,
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
    assert _active_session_at(secure, state, addr) is active
    assert active.last_seen == 0.0
    assert not active.current_epoch.seen_data_nonces.contains(nonce)
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
            pending.session_locator,
        )
    elif case == "malformed-json":
        ciphertext = secure.AESGCM(client_to_server_key).encrypt(
            nonce,
            b"{not-json",
            secure.build_data_aad(pending.session_locator),
        )
        packet = secure.build_data_packet(
            pending.session_locator, nonce, ciphertext
        )
    elif case == "non-dict-json":
        packet = _encrypted_control_packet(
            secure,
            client_to_server_key,
            nonce,
            ["ping", secure.SESSION_CONFIRMATION_SEQUENCE],
            pending.session_locator,
        )
    elif case == "wrong-station":
        packet = _d66_confirmation_packet(
            secure,
            client_to_server_key,
            nonce,
            pending.session_locator,
            station_id="other_station",
        )
    elif case == "wrong-seq":
        packet = _d66_confirmation_packet(
            secure,
            client_to_server_key,
            nonce,
            pending.session_locator,
            seq=1,
        )
    elif case == "false-seq":
        packet = _d66_confirmation_packet(
            secure,
            client_to_server_key,
            nonce,
            pending.session_locator,
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
            pending.session_locator,
        )
    elif case == "nmea":
        packet = _encrypted_data_packet(
            secure,
            client_to_server_key,
            nonce,
            pending.session_locator,
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
            pending.session_locator,
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
    assert state._pending_sessions[_relation_key(secure, addr)] is pending
    assert (
        state.get_active_session(
            secure._EndpointSessionKey(
                _test_endpoint_token(secure), pending.session_locator
            ),
            1.0,
        )
        is None
    )
    assert not pending.current_epoch.seen_data_nonces.contains(nonce)
    stats = state.stats()
    assert stats.pending_sessions_promoted == 0
    assert stats.current_pending_sessions == 1
    assert stats.sessions_created == 0
    assert stats.current_sessions == 0
    assert stats.data_nonces_accepted == 0
    assert stats.current_data_nonces == 0


def test_d66_data_dispatch_by_locator_has_no_cross_epoch_trial_decrypt(
    monkeypatch,
):
    """Active session (locator L1) and pending confirmation (locator L2)
    coexist at the same relation. Dispatch must be decided purely by
    which locator a DATA packet carries -- never by trying to decrypt
    against the "other" epoch first. This is the V2 replacement for the
    old tuple-only dispatch model, which used to select pending-vs-active
    by relation alone and could attempt cross-epoch trial decryption."""

    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    addr = ("127.0.0.1", 51080)
    active_client_key, active_server_key = _d66_keys(2)

    decrypted_with = []

    class _RecordingAESGCM:
        def __init__(self, key):
            self.key = key
            self.delegate = AESGCM(key)

        def encrypt(self, *args):
            return self.delegate.encrypt(*args)

        def decrypt(self, *args):
            decrypted_with.append(self.key)
            return self.delegate.decrypt(*args)

    monkeypatch.setattr(secure, "AESGCM", _RecordingAESGCM)

    active, _, _ = _install_test_session(
        secure,
        state,
        addr,
        active_client_key,
        active_server_key,
        now=0.0,
    )
    pending, pending_client_key, _ = _d66_install_pending(
        secure,
        state,
        addr,
        marker=40,
        now=0.5,
    )
    active_locator = active._session_key.session_locator
    pending_locator = pending.session_locator
    assert active_locator != pending_locator

    active_nonce = b"\x50" * 12
    confirmation_nonce = b"\x51" * 12
    active_data_packet = _encrypted_data_packet(
        secure,
        active_client_key,
        active_nonce,
        active_locator,
        payload="!AIVDM,1,1,,A,first,0*00",
    )
    confirmation_packet = _d66_confirmation_packet(
        secure,
        pending_client_key,
        confirmation_nonce,
        pending_locator,
    )

    queue, fake_socket = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(active_data_packet, addr), (confirmation_packet, addr)],
        state=state,
        wall_clock=_FakeClock(1001.0),
        monotonic_clock=_FakeClock(1.0),
    )

    # Exactly one decrypt attempt per packet, each against its own
    # locator's epoch -- never both epochs tried for either packet.
    assert decrypted_with == [active_client_key, pending_client_key]
    assert len(queue.items) == 1
    assert _relation_key(secure, addr) not in state._pending_sessions
    promoted = _active_session_at(secure, state, addr)
    assert promoted is not active
    assert promoted._session_key.session_locator == pending_locator
    assert promoted.current_epoch.seen_data_nonces.contains(
        confirmation_nonce
    )
    assert len(fake_socket.sent) == 1
    assert state.stats().sessions_created == 2
    assert state.stats().sessions_replaced == 1
    assert state.stats().pending_sessions_promoted == 1


def test_d66_pending_locator_confirmation_shape_check_is_exclusive(
    monkeypatch,
):
    """A DATA packet carrying the PENDING locator is evaluated exclusively
    as a pending-confirmation candidate -- even when (as engineered here)
    the same client key would also decrypt successfully under the active
    epoch. An NMEA-shaped plaintext fails the confirmation-shape check and
    must be dropped without ever additionally being tried against the
    active session."""

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
    pending_locator = _fresh_test_locator()
    pending = state.install_pending_session(
        _relation_key(secure, addr),
        "boat_001",
        pending_locator,
        secure.AESGCM(shared_client_key),
        secure.AESGCM(pending_server_key),
        0.5)
    nonce = b"\x53" * 12
    packet = _encrypted_data_packet(
        secure,
        shared_client_key,
        nonce,
        pending_locator,
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
    assert _active_session_at(secure, state, addr) is active
    assert state._pending_sessions[_relation_key(secure, addr)] is pending
    assert active.last_seen == 0.0
    assert not active.current_epoch.seen_data_nonces.contains(nonce)
    assert not pending.current_epoch.seen_data_nonces.contains(nonce)
    assert state.stats().sessions_touched == 0
    assert state.stats().data_nonces_accepted == 0


def test_d66_active_locator_never_attempts_pending_decrypt(monkeypatch):
    """A DATA packet carrying the ACTIVE locator must be processed
    exclusively through the active epoch. A pending session at the same
    relation with a decrypt method that raises if ever invoked proves
    the server never tries pending decryption for an active-locator
    packet."""

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
        _relation_key(secure, addr),
        "boat_001",
        _fresh_test_locator(),
        UnexpectedDecryptFailure(),
        secure.AESGCM(bytes((30,)) * 32),
        0.5)
    nonce = b"\x54" * 12
    packet = _encrypted_data_packet(
        secure,
        active_client_key,
        nonce,
        active._session_key.session_locator,
        payload="!AIVDM,1,1,,A,active-only,0*00",
    )

    queue, fake_socket = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(packet, addr)],
        state=state,
        monotonic_clock=_FakeClock(1.0),
    )

    assert len(queue.items) == 1
    assert fake_socket.sent == []
    assert _active_session_at(secure, state, addr) is active
    assert state._pending_sessions[_relation_key(secure, addr)] is pending
    assert active.current_epoch.seen_data_nonces.contains(nonce)
    assert state.stats().sessions_touched == 1
    assert state.stats().data_nonces_accepted == 1


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
    assert _active_session_at(secure, state, addr) is active
    assert state._pending_sessions[_relation_key(secure, addr)] is pending
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

    first_keys, _, first_locator = _d66_run_authenticated_handshake(
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
        first_locator,
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
    first_active = _active_session_at(secure, state, addr)
    first_cache = first_active.current_epoch.seen_data_nonces

    # Treat the first pong as lost: the server is active, and a later fresh
    # handshake must retain it while installing the next pending candidate.
    second_keys, _, second_locator = _d66_run_authenticated_handshake(
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
    assert second_locator != first_locator
    assert second_keys.client_to_server_key != (
        first_keys.client_to_server_key
    )
    assert second_keys.server_to_client_key != (
        first_keys.server_to_client_key
    )
    assert _active_session_at(secure, state, addr) is first_active
    second_pending = state._pending_sessions[_relation_key(secure, addr)]

    old_data_nonce = b"\x62" * 12
    old_ping_nonce = b"\x63" * 12
    old_packets = [
        (
            _encrypted_data_packet(
                secure,
                first_keys.client_to_server_key,
                old_data_nonce,
                first_locator,
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
                first_locator,
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
    assert _active_session_at(secure, state, addr) is first_active
    assert state._pending_sessions[_relation_key(secure, addr)] is second_pending
    assert len(first_cache) == 3

    second_confirmation_nonce = b"\x64" * 12
    second_confirmation = _d66_confirmation_packet(
        secure,
        second_keys.client_to_server_key,
        second_confirmation_nonce,
        second_locator,
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
    second_active = _active_session_at(secure, state, addr)
    assert second_active is not first_active
    assert _relation_key(secure, addr) not in state._pending_sessions
    assert second_active.current_epoch.seen_data_nonces is (
        second_pending.current_epoch.seen_data_nonces
    )
    assert second_active.current_epoch.seen_data_nonces.contains(
        second_confirmation_nonce,
    )
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
                first_locator,
                payload="!AIVDM,1,1,,A,late-old,0*00",
            ),
            addr,
        ),
        (
            _encrypted_data_packet(
                secure,
                second_keys.client_to_server_key,
                new_data_nonce,
                second_locator,
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
                second_locator,
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
    assert not second_active.current_epoch.seen_data_nonces.contains(
        late_old_nonce,
    )
    assert second_active.current_epoch.seen_data_nonces.contains(
        new_data_nonce,
    )
    assert second_active.current_epoch.seen_data_nonces.contains(
        new_ping_nonce,
    )

    stats = state.stats()
    assert stats.pending_sessions_created == 2
    assert stats.pending_sessions_promoted == 2
    assert stats.current_pending_sessions == 0
    assert stats.sessions_created == 2
    assert stats.sessions_replaced == 1
    assert stats.current_sessions == 1
    assert stats.data_nonces_session_discarded == 3
    assert stats.current_data_nonces == 3


# UDPSEC 1B physical-listener session namespace isolation.


def test_endpoint_namespace_same_peer_active_and_pending_are_independent(
    monkeypatch,
):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(max_sessions=4, max_pending_sessions=4)
    endpoint_a = secure._new_endpoint_token()
    endpoint_b = secure._new_endpoint_token()
    peer = ("192.0.2.10", 50000)
    relation_a = _relation_key(secure, peer, endpoint_a)
    relation_b = _relation_key(secure, peer, endpoint_b)

    active_a = state.install_session(
        relation_a, "boat_001", _fresh_test_locator(), object(), object(), 0.0
    )
    active_b = state.install_session(
        relation_b, "boat_001", _fresh_test_locator(), object(), object(), 0.0
    )
    pending_a = state.install_pending_session(
        relation_a, "boat_001", _fresh_test_locator(), object(), object(), 1.0
    )
    pending_b = state.install_pending_session(
        relation_b, "boat_001", _fresh_test_locator(), object(), object(), 1.0
    )

    assert secure._EndpointPeerKey(
        active_a._session_key.endpoint_token, active_a.path_state.active_path
    ) == relation_a
    assert secure._EndpointPeerKey(
        active_b._session_key.endpoint_token, active_b.path_state.active_path
    ) == relation_b
    assert pending_a._relation_key == relation_a
    assert pending_b._relation_key == relation_b
    assert {
        item.path_state.active_path for item in (active_a, active_b)
    } == {peer}
    assert {item._address for item in (pending_a, pending_b)} == {peer}
    assert state._sessions == {
        active_a._session_key: active_a,
        active_b._session_key: active_b,
    }
    assert state._pending_sessions == {
        relation_a: pending_a,
        relation_b: pending_b,
    }
    assert state.stats().sessions_replaced == 0
    assert state.stats().pending_sessions_replaced == 0

    assert state.touch_session(active_a, 1.5)
    assert tuple(state._sessions) == (
        active_b._session_key,
        active_a._session_key,
    )
    assert _active_session_for_relation_key(state, relation_b) is active_b
    assert active_a.last_seen == 1.5
    assert active_b.last_seen == 0.0
    assert state.stats().sessions_touched == 1

    replacement_a = state.install_session(
        relation_a, "boat_001", _fresh_test_locator(), object(), object(), 2.0
    )
    pending_replacement_a = state.install_pending_session(
        relation_a, "boat_001", _fresh_test_locator(), object(), object(), 2.0
    )

    assert _active_session_for_relation_key(state, relation_a) is replacement_a
    assert _active_session_for_relation_key(state, relation_b) is active_b
    assert state._pending_sessions[relation_a] is pending_replacement_a
    assert state._pending_sessions[relation_b] is pending_b
    assert state.stats().sessions_replaced == 1
    assert state.stats().pending_sessions_replaced == 1


def test_endpoint_namespace_fresh_incarnation_cannot_select_retained_state(
    monkeypatch,
):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    endpoint_a = secure._new_endpoint_token()
    endpoint_b = secure._new_endpoint_token()
    peer = ("127.0.0.1", 51089)
    relation_a = _relation_key(secure, peer, endpoint_a)
    relation_b = _relation_key(secure, peer, endpoint_b)
    key_a = b"\x3f" * 32
    active_a, _, _ = _install_test_session(
        secure,
        state,
        peer,
        key_a,
        b"\x40" * 32,
        now=0.0,
        endpoint_token=endpoint_a,
    )
    nonce = b"\x41" * 12
    locator_a = active_a._session_key.session_locator
    packet = _encrypted_data_packet(
        secure,
        key_a,
        nonce,
        locator_a,
        payload="!AIVDM,1,1,,A,old-incarnation,0*00",
    )

    assert (
        state.get_active_session(
            secure._EndpointSessionKey(endpoint_b, locator_a), 0.5
        )
        is None
    )
    queue, fake_socket = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(packet, peer)],
        state=state,
        monotonic_clock=_FakeClock(1.0),
        endpoint_token=endpoint_b,
        owned_sessions={},
        owned_pending_sessions={},
    )

    assert queue.items == []
    assert fake_socket.sent == []
    assert state._sessions == {active_a._session_key: active_a}
    assert _active_session_for_relation_key(state, relation_b) is None
    assert active_a.last_seen == 0.0
    assert not active_a.current_epoch.seen_data_nonces.contains(nonce)
    stats = state.stats()
    assert stats.sessions_touched == 0
    assert stats.data_nonces_accepted == 0
    assert stats.data_nonce_exhaustions == 0


def test_endpoint_namespace_promotion_replaces_only_same_relation(
    monkeypatch,
):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(data_nonce_max_per_session=4)
    endpoint_a = secure._new_endpoint_token()
    endpoint_b = secure._new_endpoint_token()
    peer = ("192.0.2.11", 50001)
    relation_a = _relation_key(secure, peer, endpoint_a)
    relation_b = _relation_key(secure, peer, endpoint_b)
    active_a = state.install_session(
        relation_a, "boat_001", _fresh_test_locator(), object(), object(), 0.0
    )
    active_b = state.install_session(
        relation_b, "boat_001", _fresh_test_locator(), object(), object(), 0.0
    )
    nonce_a = b"\x01" * 12
    nonce_b = b"\x02" * 12
    confirmation_nonce = b"\x03" * 12
    assert state.accept_data_nonce(active_a, nonce_a, 0.1)
    assert state.accept_data_nonce(active_b, nonce_b, 0.1)
    pending_b = state.install_pending_session(
        relation_b, "boat_001", _fresh_test_locator(), object(), object(), 1.0
    )
    assert state.accept_pending_data_nonce(
        pending_b, confirmation_nonce, 1.1
    )
    transferred_ledger = pending_b.current_epoch.seen_data_nonces

    promoted_b = state.promote_pending_session(pending_b, 2.0)

    assert _active_session_for_relation_key(state, relation_a) is active_a
    assert active_a.current_epoch.seen_data_nonces.contains(nonce_a)
    assert active_a.last_seen == 0.0
    assert promoted_b is _active_session_for_relation_key(state, relation_b)
    assert promoted_b.current_epoch.seen_data_nonces is transferred_ledger
    assert promoted_b.current_epoch.seen_data_nonces.contains(confirmation_nonce)
    assert len(active_b.current_epoch.seen_data_nonces) == 0
    assert state.stats().pending_sessions_promoted == 1
    assert state.stats().sessions_replaced == 1


def test_endpoint_namespace_exact_replacement_preserves_global_capacity_policy(
    monkeypatch,
):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(max_sessions=2, max_pending_sessions=2)
    peer = ("192.0.2.12", 50002)
    relation_a = _relation_key(
        secure, peer, secure._new_endpoint_token()
    )
    relation_b = _relation_key(
        secure, peer, secure._new_endpoint_token()
    )
    relation_c = _relation_key(
        secure, peer, secure._new_endpoint_token()
    )
    state.install_session(relation_a, "boat_001", _fresh_test_locator(), object(), object(), 0.0)
    active_b = state.install_session(
        relation_b, "boat_001", _fresh_test_locator(), object(), object(), 0.0
    )
    state.install_pending_session(
        relation_a, "boat_001", _fresh_test_locator(), object(), object(), 0.0
    )
    pending_b = state.install_pending_session(
        relation_b, "boat_001", _fresh_test_locator(), object(), object(), 0.0
    )

    replacement_a = state.install_session(relation_a, "boat_001", _fresh_test_locator(), object(), object(), 1.0)
    state.install_pending_session(
        relation_a, "boat_001", _fresh_test_locator(), object(), object(), 1.0
    )
    assert _active_session_for_relation_key(state, relation_b) is active_b
    assert state._pending_sessions[relation_b] is pending_b
    assert state.stats().sessions_capacity_evicted == 0
    assert state.stats().pending_sessions_capacity_evicted == 0

    replacement_c = state.install_session(relation_c, "boat_001", _fresh_test_locator(), object(), object(), 2.0)
    state.install_pending_session(
        relation_c, "boat_001", _fresh_test_locator(), object(), object(), 2.0
    )
    assert _active_session_for_relation_key(state, relation_b) is None
    assert relation_b not in state._pending_sessions
    assert tuple(state._sessions) == (
        replacement_a._session_key,
        replacement_c._session_key,
    )
    assert tuple(state._pending_sessions) == (relation_a, relation_c)
    assert state.stats().sessions_replaced == 1
    assert state.stats().pending_sessions_replaced == 1
    assert state.stats().sessions_capacity_evicted == 1
    assert state.stats().pending_sessions_capacity_evicted == 1


def test_endpoint_namespace_active_replay_and_exhaustion_are_isolated(
    monkeypatch,
):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(data_nonce_max_per_session=1)
    peer = ("192.0.2.13", 50003)
    relation_a = _relation_key(
        secure, peer, secure._new_endpoint_token()
    )
    relation_b = _relation_key(
        secure, peer, secure._new_endpoint_token()
    )
    active_a = state.install_session(
        relation_a, "boat_001", _fresh_test_locator(), object(), object(), 0.0
    )
    active_b = state.install_session(
        relation_b, "boat_001", _fresh_test_locator(), object(), object(), 0.0
    )
    shared_nonce = b"\x11" * 12

    assert state.admit_data_nonce(
        active_a, shared_nonce, 0.1
    ) is secure._DataNonceAdmission.ACCEPTED
    assert state.admit_data_nonce(
        active_a, shared_nonce, 0.2
    ) is secure._DataNonceAdmission.REPLAY
    assert state.admit_data_nonce(
        active_b, shared_nonce, 0.2
    ) is secure._DataNonceAdmission.ACCEPTED
    assert state.admit_data_nonce(
        active_a, b"\x12" * 12, 0.3
    ) is secure._DataNonceAdmission.EXHAUSTED

    assert _active_session_for_relation_key(state, relation_a) is None
    assert _active_session_for_relation_key(state, relation_b) is active_b
    assert active_b.current_epoch.seen_data_nonces.contains(shared_nonce)
    assert active_b.last_seen == 0.0
    stats = state.stats()
    assert stats.data_nonces_accepted == 2
    assert stats.data_nonce_replays == 1
    assert stats.data_nonce_exhaustions == 1
    assert stats.current_data_nonces == 1
    assert stats.data_nonces_expired == 0
    assert stats.data_nonces_capacity_evicted == 0


def test_endpoint_namespace_pending_exhaustion_preserves_all_active_state(
    monkeypatch,
):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(data_nonce_max_per_session=1)
    peer = ("192.0.2.14", 50004)
    relation_a = _relation_key(
        secure, peer, secure._new_endpoint_token()
    )
    relation_b = _relation_key(
        secure, peer, secure._new_endpoint_token()
    )
    active_a = state.install_session(
        relation_a, "boat_001", _fresh_test_locator(), object(), object(), 0.0
    )
    active_b = state.install_session(
        relation_b, "boat_001", _fresh_test_locator(), object(), object(), 0.0
    )
    pending_a = state.install_pending_session(
        relation_a, "boat_001", _fresh_test_locator(), object(), object(), 1.0
    )
    pending_b = state.install_pending_session(
        relation_b, "boat_001", _fresh_test_locator(), object(), object(), 1.0
    )
    shared_nonce = b"\x21" * 12
    assert state.accept_pending_data_nonce(pending_a, shared_nonce, 1.1)
    assert state.accept_pending_data_nonce(pending_b, shared_nonce, 1.1)

    assert state.admit_pending_data_nonce(
        pending_a, b"\x22" * 12, 1.2
    ) is secure._DataNonceAdmission.EXHAUSTED

    assert _active_session_for_relation_key(state, relation_a) is active_a
    assert _active_session_for_relation_key(state, relation_b) is active_b
    assert relation_a not in state._pending_sessions
    assert state._pending_sessions[relation_b] is pending_b
    assert pending_b.current_epoch.seen_data_nonces.contains(shared_nonce)
    assert active_a.last_seen == 0.0
    assert active_b.last_seen == 0.0


@pytest.mark.parametrize(
    "peer",
    (
        pytest.param(("192.0.2.15", 50005), id="ipv4"),
        pytest.param(("2001:db8::15", 50005, 17, 4), id="ipv6"),
    ),
)
def test_endpoint_namespace_expiry_uses_complete_relation_key(
    monkeypatch,
    peer,
):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(
        session_ttl=10.0,
        pending_session_ttl=10.0,
    )
    relation_a = _relation_key(
        secure, peer, secure._new_endpoint_token()
    )
    relation_b = _relation_key(
        secure, peer, secure._new_endpoint_token()
    )
    active_a = state.install_session(relation_a, "boat_001", _fresh_test_locator(), object(), object(), 0.0)
    active_b = state.install_session(
        relation_b, "boat_001", _fresh_test_locator(), object(), object(), 1.0
    )
    state.install_pending_session(
        relation_a, "boat_001", _fresh_test_locator(), object(), object(), 0.0
    )
    pending_b = state.install_pending_session(
        relation_b, "boat_001", _fresh_test_locator(), object(), object(), 1.0
    )

    assert state.cleanup_expired_sessions(10.0) == [active_a._session_key]
    assert state.cleanup_expired_pending_sessions(10.0) == [relation_a]
    assert _active_session_for_relation_key(state, relation_b) is active_b
    assert state._pending_sessions[relation_b] is pending_b
    assert active_b.path_state.active_path == peer
    assert pending_b._address == peer


def test_ipv6_active_relation_ignores_flowinfo_but_not_scope_id(monkeypatch):
    """Architecture audit: the active-relation index must agree with
    `_structured_paths_match` exactly. A same-relation authenticated
    replacement (or any other relation-index lookup) arriving with a
    different IPv6 flowinfo than the original must still be recognized as
    the SAME active relation; a different scope_id must be a genuinely
    DIFFERENT relation. Pending handshake lookup stays exactly tuple-bound
    throughout (flowinfo and scope_id both distinguish pending relations).
    """
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    endpoint_token = secure._new_endpoint_token()
    original_addr = ("2001:db8::10", 50000, 7, 3)
    same_relation_new_flowinfo = ("2001:db8::10", 50000, 99, 3)
    different_scope_addr = ("2001:db8::10", 50000, 7, 4)

    original_relation = _relation_key(
        secure, original_addr, endpoint_token
    )
    reflow_relation = _relation_key(
        secure, same_relation_new_flowinfo, endpoint_token
    )
    scoped_relation = _relation_key(
        secure, different_scope_addr, endpoint_token
    )

    original = state.install_session(
        original_relation,
        "boat_001",
        _fresh_test_locator(),
        object(),
        object(),
        now=0.0,
    )

    # Same (ip, port, scope_id), only flowinfo differs: the relation index
    # must resolve this to the SAME active session, matching
    # _structured_paths_match's own flowinfo-insensitive definition.
    assert (
        _active_session_for_relation_key(state, reflow_relation)
        is original
    )
    assert (
        _active_session_for_relation_key(state, original_relation)
        is original
    )

    # A same-relation authenticated replacement arriving under a different
    # flowinfo must be recognized as replacing THIS session (assembly
    # namespace carried forward, session_handle fresh), not treated as an
    # unrelated new relation.
    replacement = state.install_session(
        reflow_relation,
        "boat_001",
        _fresh_test_locator(),
        object(),
        object(),
        now=1.0,
    )
    assert replacement is not original
    assert replacement.session_handle != original.session_handle
    assert replacement.assembly_namespace == original.assembly_namespace
    assert (
        _active_session_for_relation_key(state, original_relation)
        is replacement
    )
    assert state.stats().sessions_replaced == 1
    assert state.stats().current_sessions == 1

    # A different scope_id is a genuinely different relation: it must NOT
    # resolve to the flowinfo/scope_id=3 session, and installing there
    # must not replace it.
    assert _active_session_for_relation_key(state, scoped_relation) is None
    other_scope = state.install_session(
        scoped_relation,
        "boat_001",
        _fresh_test_locator(),
        object(),
        object(),
        now=2.0,
    )
    assert (
        _active_session_for_relation_key(state, original_relation)
        is replacement
    )
    assert (
        _active_session_for_relation_key(state, scoped_relation)
        is other_scope
    )
    assert state.stats().sessions_replaced == 1
    assert state.stats().current_sessions == 2

    # Pending establishment remains exactly tuple-bound: flowinfo DOES
    # distinguish two pending relations, unlike the active relation index.
    pending_original = state.install_pending_session(
        original_relation,
        "boat_001",
        _fresh_test_locator(),
        object(),
        object(),
        now=3.0,
    )
    pending_reflow = state.install_pending_session(
        reflow_relation,
        "boat_001",
        _fresh_test_locator(),
        object(),
        object(),
        now=3.0,
    )
    assert pending_original is not pending_reflow
    assert state._pending_sessions[original_relation] is pending_original
    assert state._pending_sessions[reflow_relation] is pending_reflow
    assert state.stats().current_pending_sessions == 2


def test_ipv4_active_relation_identity_is_unchanged(monkeypatch):
    """Architecture audit regression guard: IPv4 (2-tuple) relation
    identity must be completely unaffected by the IPv6 flowinfo
    canonicalization -- it is not a tuple this canonicalization ever
    touches."""
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    endpoint_token = secure._new_endpoint_token()
    addr = ("192.0.2.99", 50009)
    relation = _relation_key(secure, addr, endpoint_token)

    original = state.install_session(
        relation, "boat_001", _fresh_test_locator(), object(), object(), 0.0
    )
    assert _active_session_for_relation_key(state, relation) is original

    replacement = state.install_session(
        relation, "boat_001", _fresh_test_locator(), object(), object(), 1.0
    )
    assert replacement is not original
    assert replacement.assembly_namespace == original.assembly_namespace
    assert _active_session_for_relation_key(state, relation) is replacement
    assert state.stats().sessions_replaced == 1
    assert state.stats().current_sessions == 1


def test_endpoint_namespace_stale_handles_cannot_cross_relations(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(data_nonce_max_per_session=1)
    peer = ("192.0.2.16", 50006)
    relation_a = _relation_key(
        secure, peer, secure._new_endpoint_token()
    )
    relation_b = _relation_key(
        secure, peer, secure._new_endpoint_token()
    )
    stale_active = state.install_session(
        relation_a, "boat_001", _fresh_test_locator(), object(), object(), 0.0
    )
    replacement_a = state.install_session(
        relation_a, "boat_001", _fresh_test_locator(), object(), object(), 1.0
    )
    active_b = state.install_session(
        relation_b, "boat_001", _fresh_test_locator(), object(), object(), 1.0
    )
    stale_pending = state.install_pending_session(
        relation_a, "boat_001", _fresh_test_locator(), object(), object(), 1.0
    )
    replacement_pending_a = state.install_pending_session(
        relation_a, "boat_001", _fresh_test_locator(), object(), object(), 2.0
    )
    pending_b = state.install_pending_session(
        relation_b, "boat_001", _fresh_test_locator(), object(), object(), 2.0
    )

    assert state.admit_data_nonce(
        stale_active, b"\x31" * 12, 3.0
    ) is secure._DataNonceAdmission.STALE
    assert state.admit_pending_data_nonce(
        stale_pending, b"\x32" * 12, 3.0
    ) is secure._DataNonceAdmission.STALE
    assert not state.touch_session(stale_active, 3.0)
    assert _active_session_for_relation_key(state, relation_a) is replacement_a
    assert _active_session_for_relation_key(state, relation_b) is active_b
    assert state._pending_sessions[relation_a] is replacement_pending_a
    assert state._pending_sessions[relation_b] is pending_b
    assert len(active_b.current_epoch.seen_data_nonces) == 0
    assert len(pending_b.current_epoch.seen_data_nonces) == 0


@pytest.mark.parametrize("message_type", ("nmea", "ping", "close"))
def test_endpoint_namespace_cross_listener_active_data_is_inert(
    monkeypatch,
    message_type,
):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    endpoint_a = secure._new_endpoint_token()
    endpoint_b = secure._new_endpoint_token()
    peer = ("127.0.0.1", 51090)
    relation_a = _relation_key(secure, peer, endpoint_a)
    relation_b = _relation_key(secure, peer, endpoint_b)
    key_a = b"\x41" * 32
    key_b = b"\x42" * 32
    active_a, _, _ = _install_test_session(
        secure,
        state,
        peer,
        key_a,
        b"\x51" * 32,
        now=0.0,
        endpoint_token=endpoint_a,
    )
    active_b, _, _ = _install_test_session(
        secure,
        state,
        peer,
        key_b,
        b"\x52" * 32,
        now=0.0,
        endpoint_token=endpoint_b,
    )
    nonce = bytes((0x60 + len(message_type),)) * 12
    locator_a = active_a._session_key.session_locator
    if message_type == "nmea":
        packet = _encrypted_data_packet(
            secure,
            key_a,
            nonce,
            locator_a,
            payload="!AIVDM,1,1,,A,cross-endpoint,0*00",
        )
    elif message_type == "ping":
        packet = _encrypted_control_packet(
            secure,
            key_a,
            nonce,
            {
                "type": "ping",
                "seq": 9,
                "timestamp": 1000,
                "source_id": "boat_001",
            },
            locator_a,
        )
    else:
        packet = _encrypted_control_packet(
            secure,
            key_a,
            nonce,
            secure.build_session_close_message("boat_001", 1000),
            locator_a,
        )

    queue, fake_socket = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(packet, peer)],
        state=state,
        monotonic_clock=_FakeClock(1.0),
        endpoint_token=endpoint_b,
        owned_sessions={active_b._session_key: active_b},
        owned_pending_sessions={},
    )

    assert queue.items == []
    assert fake_socket.sent == []
    assert _active_session_for_relation_key(state, relation_a) is active_a
    assert _active_session_for_relation_key(state, relation_b) is active_b
    assert active_a.last_seen == 0.0
    assert active_b.last_seen == 0.0
    assert len(active_a.current_epoch.seen_data_nonces) == 0
    assert len(active_b.current_epoch.seen_data_nonces) == 0
    stats = state.stats()
    assert stats.sessions_touched == 0
    assert stats.sessions_closed == 0
    assert stats.data_nonces_accepted == 0
    assert stats.data_nonce_exhaustions == 0


def test_endpoint_namespace_wrong_listener_confirmation_is_inert(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    endpoint_a = secure._new_endpoint_token()
    endpoint_b = secure._new_endpoint_token()
    peer = ("127.0.0.1", 51091)
    relation_a = _relation_key(secure, peer, endpoint_a)
    relation_b = _relation_key(secure, peer, endpoint_b)
    active_a, _, _ = _install_test_session(
        secure,
        state,
        peer,
        b"\x71" * 32,
        b"\x72" * 32,
        now=0.0,
        endpoint_token=endpoint_a,
    )
    active_b, _, _ = _install_test_session(
        secure,
        state,
        peer,
        b"\x73" * 32,
        b"\x74" * 32,
        now=0.0,
        endpoint_token=endpoint_b,
    )
    pending_a, pending_key_a, _ = _d66_install_pending(
        secure,
        state,
        peer,
        marker=80,
        now=0.5,
        endpoint_token=endpoint_a,
    )
    nonce = b"\x75" * 12
    confirmation = _d66_confirmation_packet(
        secure, pending_key_a, nonce, pending_a.session_locator
    )

    queue, fake_socket = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(confirmation, peer)],
        state=state,
        monotonic_clock=_FakeClock(1.0),
        endpoint_token=endpoint_b,
        owned_sessions={active_b._session_key: active_b},
        owned_pending_sessions={},
    )

    assert queue.items == []
    assert fake_socket.sent == []
    assert _active_session_for_relation_key(state, relation_a) is active_a
    assert _active_session_for_relation_key(state, relation_b) is active_b
    assert state._pending_sessions[relation_a] is pending_a
    assert not pending_a.current_epoch.seen_data_nonces.contains(nonce)
    assert active_a.last_seen == 0.0
    assert active_b.last_seen == 0.0
    assert state.stats().pending_sessions_promoted == 0
    assert state.stats().sessions_touched == 0
    assert state.stats().data_nonces_accepted == 0


# LogicalSession / CryptoEpoch / PathState ownership refactor.
#
# Active-session wire lookup is keyed by (endpoint_token, session_locator);
# there is still no migration (PathState.active_path is immutable once set).
# These tests prove the *internal* ownership split
# (session vs. crypto epoch vs. path) exists and behaves identically to the
# pre-refactor flat representation for every currently-supported scenario.


def test_logical_session_owns_distinct_crypto_epoch_and_path_state(
    monkeypatch,
):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    addr = ("192.0.2.10", 50000)
    session, c2s, s2c = _install_test_session(
        secure, state, addr, b"\x01" * 32, b"\x02" * 32
    )

    assert isinstance(session, secure.LogicalSession)
    assert isinstance(session.current_epoch, secure.CryptoEpoch)
    assert isinstance(session.path_state, secure.PathState)
    assert session.current_epoch.client_to_server_aesgcm is c2s
    assert session.current_epoch.server_to_client_aesgcm is s2c
    assert isinstance(session.current_epoch.seen_data_nonces, secure._BoundedNonceSet)


def test_active_path_initially_equals_relation_peer_address(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    addr = ("192.0.2.11", 50001)
    session, _, _ = _install_test_session(
        secure, state, addr, b"\x03" * 32, b"\x04" * 32
    )

    assert session.path_state.active_path == addr
    assert _active_session_at(secure, state, addr) is session


def test_promoted_session_epoch_and_path_state_are_fresh_objects(monkeypatch):
    """Promotion still replaces the whole LogicalSession in this stage, but
    the confirmed epoch (keys + already-admitted confirmation nonce) is the
    exact object transferred from the pending candidate, not re-derived."""
    secure, client_private_key = load_secure_module_with_fake_keys(
        monkeypatch, with_client_private_key=True
    )
    state = secure.SecureState()
    addr = ("192.0.2.12", 50002)
    packet = _signed_handshake_packet(
        secure, client_private_key, "boat_001", 1000
    )
    _, fake_socket = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(packet, addr)],
        state=state,
        wall_clock=_FakeClock(1000.0),
        monotonic_clock=_FakeClock(0.0),
    )
    pending = state._pending_sessions[_relation_key(secure, addr)]
    pending_epoch = pending.current_epoch
    nonce = b"\x05" * 12

    # Promote directly through SecureState to keep this test focused on
    # object identity rather than re-deriving a second real handshake.
    assert state.accept_pending_data_nonce(pending, nonce, now=0.0)
    session = state.promote_pending_session(pending, now=0.0)

    assert isinstance(session, secure.LogicalSession)
    assert session.current_epoch is pending_epoch
    assert isinstance(session.path_state, secure.PathState)
    assert session.path_state.active_path == addr


def test_session_handle_is_stable_and_distinct_per_logical_session(
    monkeypatch,
):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    session_a, _, _ = _install_test_session(
        secure, state, ("192.0.2.20", 50010), b"\x06" * 32, b"\x07" * 32
    )
    session_b, _, _ = _install_test_session(
        secure, state, ("192.0.2.21", 50011), b"\x08" * 32, b"\x09" * 32
    )

    assert isinstance(session_a.session_handle, bytes)
    assert isinstance(session_b.session_handle, bytes)
    assert session_a.session_handle != session_b.session_handle

    handle_before_touch = session_a.session_handle
    state.touch_session(session_a, now=1.0)
    assert session_a.session_handle == handle_before_touch


def test_created_at_last_seen_capacity_and_stale_handle_semantics_unchanged(
    monkeypatch,
):
    """Regression: the ownership refactor must not change created_at/
    last_seen bookkeeping, LRU/capacity eviction, or exact-object
    stale-handle protection."""
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(max_sessions=1)
    first, _, _ = _install_test_session(
        secure, state, ("192.0.2.30", 50020), b"\x0a" * 32, b"\x0b" * 32,
        now=5.0,
    )
    assert first.created_at == 5.0
    assert first.last_seen == 5.0

    assert state.touch_session(first, now=6.0)
    assert first.created_at == 5.0
    assert first.last_seen == 6.0

    # Capacity eviction (max_sessions=1): installing a second session evicts
    # the first; the evicted object becomes a stale handle everywhere.
    second, _, _ = _install_test_session(
        secure, state, ("192.0.2.31", 50021), b"\x0c" * 32, b"\x0d" * 32,
        now=7.0,
    )
    assert state.stats().sessions_capacity_evicted == 1
    assert not state.touch_session(first, now=8.0)
    assert not state.is_live_session_handle(first, now=8.0)
    assert state.is_live_session_handle(second, now=8.0)


# Established-session outbound destination authority.


def test_active_ping_pong_reply_resolves_through_path_state_active_path(
    monkeypatch,
):
    """Established-session traffic is addressed to the session's
    authoritative `path_state.active_path`. In this V2 stage, active_path
    is immutable once a LogicalSession is promoted and no migration
    exists yet, so a DATA packet's arriving address must exactly equal
    active_path for it to be processed at all (see the wrong-path drop
    test below) -- which means the reply destination and the arriving
    address are necessarily the same value here. This proves the reply
    is genuinely sourced from `active_path` and not merely copied from
    the incoming packet."""
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    addr = ("192.0.2.40", 50030)
    client_to_server_key = b"\x0e" * 32
    server_to_client_key = b"\x0f" * 32
    session, _, _ = _install_test_session(
        secure,
        state,
        addr,
        client_to_server_key,
        server_to_client_key,
    )

    ping_packet = _encrypted_control_packet(
        secure,
        client_to_server_key,
        b"\x10" * 12,
        {
            "type": "ping",
            "seq": 1,
            "timestamp": 1000,
            "source_id": "boat_001",
        },
        session._session_key.session_locator,
    )
    _, fake_socket = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(ping_packet, addr)],
        state=state,
        wall_clock=_FakeClock(1000.0),
        monotonic_clock=_FakeClock(1.0),
    )

    assert len(fake_socket.sent) == 1
    _, reply_addr = fake_socket.sent[0]
    assert reply_addr == session.path_state.active_path
    assert reply_addr == addr


def test_correct_locator_but_wrong_path_is_silently_dropped_before_decrypt(
    monkeypatch,
):
    """One of the most important tests in this stage: a DATA packet
    carrying the CORRECT locator for a live active session, but arriving
    from a different transport address than that session's
    `path_state.active_path`, must be dropped exactly like an unknown
    locator -- no decrypt, no nonce lookup/admission, no touch, no reply,
    no queued ingress frame, and no candidate-path state created. This is
    the explicit boundary that a later migration stage will replace with
    authenticated candidate-path evaluation; today, no migration exists."""
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    original_addr = ("192.0.2.44", 50034)
    different_addr = ("203.0.113.44", 60044)
    client_to_server_key = b"\x20" * 32
    server_to_client_key = b"\x21" * 32
    session, _, _ = _install_test_session(
        secure,
        state,
        original_addr,
        client_to_server_key,
        server_to_client_key,
    )
    nonce = b"\x22" * 12
    packet = _encrypted_data_packet(
        secure,
        client_to_server_key,
        nonce,
        session._session_key.session_locator,
        payload="!AIVDM,1,1,,A,wrong-path,0*00",
    )

    queue, fake_socket = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(packet, different_addr)],
        state=state,
        monotonic_clock=_FakeClock(1.0),
    )

    assert queue.items == []
    assert fake_socket.sent == []
    assert session.path_state.active_path == original_addr
    assert session.last_seen == 1000.0
    assert len(session.current_epoch.seen_data_nonces) == 0
    stats = state.stats()
    assert stats.sessions_touched == 0
    assert stats.data_nonces_accepted == 0
    assert stats.current_data_nonces == 0


class _SpyAESGCM:
    """Wraps a real AESGCM object to observe whether decrypt is actually
    invoked, while still performing genuine encryption/decryption so a
    correct-path control packet processes normally through the same spy."""

    def __init__(self, real):
        self._real = real
        self.decrypt_calls = 0

    def decrypt(self, *args, **kwargs):
        self.decrypt_calls += 1
        return self._real.decrypt(*args, **kwargs)

    def encrypt(self, *args, **kwargs):
        return self._real.encrypt(*args, **kwargs)


def _spy_on_state_method(monkeypatch, state, method_name):
    """Wrap one bound SecureState method with a call-counting spy that
    still delegates to the real implementation, so pre-decryption ordering
    can be observed without changing exercised behavior."""
    real_bound_method = getattr(state, method_name)
    calls = []

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return real_bound_method(*args, **kwargs)

    monkeypatch.setattr(state, method_name, spy)
    return calls


def test_wrong_path_correct_locator_never_reaches_decrypt_or_replay_lookup(
    monkeypatch,
):
    """Strengthens the state-only assertions above with OBSERVABLE spies
    directly on the AEAD decrypt call and the replay lookup/admission
    path, per the Codex audit's Finding E: proving the packet is dropped
    before ANY of that machinery runs, not merely that its externally
    visible side effects happen to be absent. A correct-path control
    packet using the exact same spies proves they are attached to the
    real exercised code path, not simply never invoked due to a broken
    wrapper."""
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    original_addr = ("192.0.2.45", 50035)
    different_addr = ("203.0.113.45", 60045)
    client_to_server_key = b"\x25" * 32
    server_to_client_key = b"\x26" * 32
    session, _, _ = _install_test_session(
        secure,
        state,
        original_addr,
        client_to_server_key,
        server_to_client_key,
    )

    spy_aesgcm = _SpyAESGCM(session.current_epoch.client_to_server_aesgcm)
    session.current_epoch.client_to_server_aesgcm = spy_aesgcm
    nonce_seen_calls = _spy_on_state_method(monkeypatch, state, "data_nonce_seen")
    admit_calls = _spy_on_state_method(monkeypatch, state, "admit_data_nonce")
    touch_calls = _spy_on_state_method(monkeypatch, state, "touch_session")

    wrong_path_nonce = b"\x27" * 12
    wrong_path_packet = _encrypted_data_packet(
        secure,
        client_to_server_key,
        wrong_path_nonce,
        session._session_key.session_locator,
        payload="!AIVDM,1,1,,A,wrong-path,0*00",
    )
    queue, fake_socket = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(wrong_path_packet, different_addr)],
        state=state,
        monotonic_clock=_FakeClock(1.0),
    )

    # No AEAD decrypt, no replay lookup, no admission, no session touch --
    # not merely no visible side effect of any of those.
    assert spy_aesgcm.decrypt_calls == 0
    assert nonce_seen_calls == []
    assert admit_calls == []
    assert touch_calls == []
    assert queue.items == []
    assert fake_socket.sent == []
    assert session.path_state.active_path == original_addr

    # Control: the exact same session, same spies, same locator -- but
    # from the session's own correct path -- must decrypt and admit
    # normally, proving the spies above are wired into the real path and
    # the wrong-path packet's silence is not an artifact of a broken test.
    correct_path_nonce = b"\x28" * 12
    correct_path_packet = _encrypted_data_packet(
        secure,
        client_to_server_key,
        correct_path_nonce,
        session._session_key.session_locator,
        payload="!AIVDM,1,1,,A,correct-path,0*00",
    )
    queue_2, _ = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(correct_path_packet, original_addr)],
        state=state,
        monotonic_clock=_FakeClock(2.0),
    )

    assert spy_aesgcm.decrypt_calls == 1
    assert len(nonce_seen_calls) == 1
    assert len(admit_calls) == 1
    assert len(touch_calls) == 1
    assert len(queue_2.items) == 1


def test_unknown_locator_never_reaches_decrypt_regardless_of_correct_path(
    monkeypatch,
):
    """An unrecognized locator must be silently dropped before any
    decryption is attempted, even when it arrives from a live session's
    own correct path address -- proving there is no fallback to tuple-
    based lookup or trial decryption once the locator itself fails to
    resolve to a live session."""
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    addr = ("192.0.2.46", 50036)
    client_to_server_key = b"\x29" * 32
    server_to_client_key = b"\x2a" * 32
    session, _, _ = _install_test_session(
        secure,
        state,
        addr,
        client_to_server_key,
        server_to_client_key,
    )

    spy_aesgcm = _SpyAESGCM(session.current_epoch.client_to_server_aesgcm)
    session.current_epoch.client_to_server_aesgcm = spy_aesgcm
    nonce_seen_calls = _spy_on_state_method(monkeypatch, state, "data_nonce_seen")

    unknown_locator = _fresh_test_locator()
    assert unknown_locator != session._session_key.session_locator
    packet = _encrypted_data_packet(
        secure,
        client_to_server_key,
        b"\x2b" * 12,
        unknown_locator,
        payload="!AIVDM,1,1,,A,unknown-locator,0*00",
    )

    queue, fake_socket = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(packet, addr)],
        state=state,
        monotonic_clock=_FakeClock(1.0),
    )

    assert spy_aesgcm.decrypt_calls == 0
    assert nonce_seen_calls == []
    assert queue.items == []
    assert fake_socket.sent == []


def test_shutdown_close_resolves_through_path_state_active_path(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    original_addr = ("192.0.2.41", 50031)
    relocated_path = ("203.0.113.10", 60001)
    session, _, _ = _install_test_session(
        secure, state, original_addr, b"\x11" * 32, b"\x12" * 32
    )
    session.path_state.active_path = relocated_path

    fake_socket = _FakeSecureSocket()
    owned = {session._session_key: session}
    secure.close_owned_sessions(
        fake_socket,
        state,
        owned,
        wall_clock=lambda: 1234.0,
        monotonic_clock=lambda: 2.0,
    )

    assert len(fake_socket.sent) == 1
    _, close_addr = fake_socket.sent[0]
    assert close_addr == relocated_path
    assert close_addr != original_addr


def test_initial_server_hello_still_replies_to_handshake_tuple(monkeypatch):
    """Initial establishment is still tuple-bound: there is no session yet
    to own a path, so the ServerHello must reply to the ClientHello's own
    address regardless of the ownership refactor."""
    secure, client_private_key = load_secure_module_with_fake_keys(
        monkeypatch, with_client_private_key=True
    )
    addr = ("192.0.2.42", 50032)
    packet = _signed_handshake_packet(
        secure, client_private_key, "boat_001", 1000
    )

    _, fake_socket = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(packet, addr)],
        wall_clock=lambda: 1000.0,
        monotonic_clock=_FakeClock(0.0),
    )

    assert len(fake_socket.sent) == 1
    _, reply_addr = fake_socket.sent[0]
    assert reply_addr == addr


def test_pending_confirmation_pong_still_replies_to_pending_tuple(
    monkeypatch,
):
    """Pending confirmation is still tuple-bound: the confirmation pong
    replies to the confirming packet's own address, not to any path_state,
    even though the freshly-promoted session now owns a PathState."""
    secure, client_private_key = load_secure_module_with_fake_keys(
        monkeypatch, with_client_private_key=True
    )
    state = secure.SecureState()
    addr = ("192.0.2.43", 50033)
    hello_packet, client_hello, client_ephemeral_private_key = (
        _signed_client_hello(
            secure, client_private_key, "boat_001", 1000
        )
    )
    _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(hello_packet, addr)],
        state=state,
        wall_clock=lambda: 1000.0,
        monotonic_clock=_FakeClock(0.0),
    )
    pending = state._pending_sessions[_relation_key(secure, addr)]
    confirmation = _confirmation_packet_for(secure, pending, b"\x13" * 12)

    _, fake_socket = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(confirmation, addr)],
        state=state,
        wall_clock=lambda: 1000.0,
        monotonic_clock=_FakeClock(1.0),
    )

    assert len(fake_socket.sent) == 1
    _, reply_addr = fake_socket.sent[0]
    assert reply_addr == addr
    session = _active_session_at(secure, state, addr)
    assert session.path_state.active_path == addr


def _confirmation_packet_for(secure, pending, nonce):
    message = {
        "type": "ping",
        "seq": secure.SESSION_CONFIRMATION_SEQUENCE,
        "timestamp": 1000,
        "source_id": pending.station_id,
    }
    plaintext = secure.json.dumps(message).encode()
    ciphertext = pending.current_epoch.client_to_server_aesgcm.encrypt(
        nonce, plaintext, secure.build_data_aad(pending.session_locator)
    )
    return secure.build_data_packet(pending.session_locator, nonce, ciphertext)


# Secure-ingress assembler namespace.


def test_assembler_namespace_is_session_scoped_not_path_derived(monkeypatch):
    """Proves `assembler_key` is derived from the LogicalSession's stable
    `assembly_namespace`, independent of `path_state.active_path` -- so a
    later migration stage changing only the path cannot split one multipart
    stream. No migration is implemented or faked here: the wire lookup
    address is unchanged between the two fragments; only the internal
    `active_path` field (irrelevant to assembler identity) is mutated."""
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    addr = ("192.0.2.50", 50040)
    client_to_server_key = b"\x14" * 32
    session, _, _ = _install_test_session(
        secure, state, addr, client_to_server_key, b"\x15" * 32
    )

    session_locator = session._session_key.session_locator
    packet_one = _encrypted_data_packet(
        secure, client_to_server_key, b"\x16" * 12, session_locator
    )
    fake_queue_one, _ = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(packet_one, addr)],
        state=state,
        wall_clock=_FakeClock(1000.0),
        monotonic_clock=_FakeClock(1.0),
    )
    assembler_key_before = fake_queue_one.items[0].assembler_key

    # Stand-in for a future path change: mutate only the path, not the
    # session's identity or wire-lookup relation. The second fragment must
    # then arrive at the mutated path -- a locator match from any other
    # path is dropped before decryption in this stage (see the dedicated
    # wrong-path test) -- so this is also an implicit boundary check.
    relocated_path = ("203.0.113.20", 61000)
    session.path_state.active_path = relocated_path

    packet_two = _encrypted_data_packet(
        secure, client_to_server_key, b"\x17" * 12, session_locator
    )
    fake_queue_two, _ = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(packet_two, relocated_path)],
        state=state,
        wall_clock=_FakeClock(1000.0),
        monotonic_clock=_FakeClock(2.0),
    )
    assembler_key_after = fake_queue_two.items[0].assembler_key

    assert assembler_key_before == assembler_key_after
    assert (
        assembler_key_before
        == f"udpsec-assembly:{session.assembly_namespace.hex()}"
    )
    # Not a rendered remote tuple: neither the original nor the mutated
    # active_path's host/port ever appears in the assembler namespace.
    assert "192.0.2.50" not in assembler_key_before
    assert "203.0.113.20" not in assembler_key_before


def test_assembler_namespace_differs_across_sessions_with_same_station_id(
    monkeypatch,
):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    key_one = b"\x18" * 32
    key_two = b"\x19" * 32
    session_one, _, _ = _install_test_session(
        secure, state, ("192.0.2.51", 50041), key_one, b"\x1a" * 32,
        station_id="boat_001",
    )
    session_two, _, _ = _install_test_session(
        secure, state, ("192.0.2.52", 50042), key_two, b"\x1b" * 32,
        station_id="boat_001",
    )
    assert session_one.station_id == session_two.station_id
    # Same station, different relation: distinct session_handle AND
    # distinct assembly_namespace -- no cross-session multipart mixing.
    assert session_one.session_handle != session_two.session_handle
    assert session_one.assembly_namespace != session_two.assembly_namespace

    queue_one, _ = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(
            _encrypted_data_packet(
                secure,
                key_one,
                b"\x1c" * 12,
                session_one._session_key.session_locator,
            ),
            ("192.0.2.51", 50041),
        )],
        state=state,
        wall_clock=_FakeClock(1000.0),
        monotonic_clock=_FakeClock(1.0),
    )
    queue_two, _ = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(
            _encrypted_data_packet(
                secure,
                key_two,
                b"\x1d" * 12,
                session_two._session_key.session_locator,
            ),
            ("192.0.2.52", 50042),
        )],
        state=state,
        wall_clock=_FakeClock(1000.0),
        monotonic_clock=_FakeClock(2.0),
    )

    key_one_ns = queue_one.items[0].assembler_key
    key_two_ns = queue_two.items[0].assembler_key
    assert key_one_ns != key_two_ns
    assert key_one_ns == (
        f"udpsec-assembly:{session_one.assembly_namespace.hex()}"
    )
    assert key_two_ns == (
        f"udpsec-assembly:{session_two.assembly_namespace.hex()}"
    )


def _nmea_data_packet_with_aesgcm(
    secure, aesgcm, nonce, payload, session_locator, source_id="boat_001"
):
    """Like `_encrypted_data_packet`, but encrypts under an already-derived
    AESGCM object instead of raw key bytes -- needed once a session's keys
    only exist as `current_epoch.client_to_server_aesgcm`."""
    plaintext = secure.json.dumps({
        "type": "nmea",
        "payload": payload,
        "timestamp": 1000,
        "source_id": source_id,
    }).encode()
    ciphertext = aesgcm.encrypt(
        nonce, plaintext, secure.build_data_aad(session_locator)
    )
    return secure.build_data_packet(session_locator, nonce, ciphertext)


def test_multipart_assembler_namespace_survives_same_relation_replacement(
    monkeypatch,
):
    """Regression: an authenticated replacement handshake (rekey) at the
    SAME relation, for the SAME authenticated station, must not split one
    multipart AIS message across two assembler namespaces -- even though it
    installs a brand-new LogicalSession object with a fresh `session_handle`
    and fresh traffic keys. Current wire/rekey semantics (full session
    replacement, fresh ECDHE, fresh keys) are unchanged; only
    `assembly_namespace` is carried forward across this same-station,
    same-relation replacement. `session_handle` itself is NOT reused -- it
    must always identify the exact LogicalSession incarnation."""
    secure, client_private_key = load_secure_module_with_fake_keys(
        monkeypatch, with_client_private_key=True
    )
    state = secure.SecureState()
    addr = ("192.0.2.70", 50060)
    wall_clock = _FakeClock(1000.0)
    monotonic_clock = _FakeClock(0.0)

    # --- Initial establishment: ClientHello -> confirm -> fragment 1. ---
    hello_1 = _signed_handshake_packet(
        secure, client_private_key, "boat_001", 1000
    )
    _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(hello_1, addr)],
        state=state,
        wall_clock=wall_clock,
        monotonic_clock=monotonic_clock,
    )
    pending_1 = state._pending_sessions[_relation_key(secure, addr)]

    monotonic_clock.now = 1.0
    confirmation_1 = _confirmation_packet_for(secure, pending_1, b"\x20" * 12)
    fragment_1_text = "!AIVDM,2,1,9,A,fragment-one,0*00"
    fragment_1 = _nmea_data_packet_with_aesgcm(
        secure,
        pending_1.current_epoch.client_to_server_aesgcm,
        b"\x21" * 12,
        fragment_1_text,
        pending_1.session_locator,
    )
    queue_1, _ = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(confirmation_1, addr), (fragment_1, addr)],
        state=state,
        wall_clock=wall_clock,
        monotonic_clock=monotonic_clock,
    )
    session_1 = _active_session_at(secure, state, addr)
    assembler_key_1 = queue_1.items[0].assembler_key

    # --- Same-relation authenticated replacement: a fresh, distinct
    #     ClientHello/confirmation from the same address, exactly like a
    #     proactive rekey or planned refresh. ---
    monotonic_clock.now = 2.0
    hello_2, _, _ = _signed_client_hello(
        secure,
        client_private_key,
        "boat_001",
        1010,
        client_random=b"\x22" * 32,
        client_ephemeral_private_key=ec.derive_private_key(3, ec.SECP256R1()),
    )
    _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(hello_2, addr)],
        state=state,
        wall_clock=wall_clock,
        monotonic_clock=monotonic_clock,
    )
    pending_2 = state._pending_sessions[_relation_key(secure, addr)]
    assert pending_2 is not pending_1

    monotonic_clock.now = 3.0
    confirmation_2 = _confirmation_packet_for(secure, pending_2, b"\x23" * 12)
    fragment_2_text = "!AIVDM,2,2,9,A,fragment-two,0*00"
    fragment_2 = _nmea_data_packet_with_aesgcm(
        secure,
        pending_2.current_epoch.client_to_server_aesgcm,
        b"\x24" * 12,
        fragment_2_text,
        pending_2.session_locator,
    )
    queue_2, _ = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(confirmation_2, addr), (fragment_2, addr)],
        state=state,
        wall_clock=wall_clock,
        monotonic_clock=monotonic_clock,
    )
    session_2 = _active_session_at(secure, state, addr)
    assembler_key_2 = queue_2.items[0].assembler_key

    # The replacement is a genuine rekey: a brand-new LogicalSession object
    # with fresh traffic keys, counted as an ordinary replacement -- wire
    # and rekey semantics are completely unchanged.
    assert session_2 is not session_1
    assert (
        session_2.current_epoch.client_to_server_aesgcm
        is not session_1.current_epoch.client_to_server_aesgcm
    )
    assert state.stats().sessions_replaced == 1

    # session_handle identifies the LogicalSession *incarnation* and is
    # never reused, even across this same-relation, same-station
    # replacement.
    assert session_2.session_handle != session_1.session_handle

    # ...but assembly_namespace -- and therefore the assembler key derived
    # from it -- is carried forward, because the replacement is the same
    # authenticated station at the same live relation.
    assert session_2.assembly_namespace == session_1.assembly_namespace
    assert assembler_key_1 == assembler_key_2

    # And a real assembler completes the multipart group spanning both
    # fragments despite the intervening key replacement.
    from assembler import AIVDMAssembler, AssemblyStatus

    real_assembler = AIVDMAssembler(timeout=30.0)
    outcome_1 = real_assembler.feed_outcome(assembler_key_1, fragment_1_text)
    assert outcome_1.status is AssemblyStatus.PENDING
    outcome_2 = real_assembler.feed_outcome(assembler_key_2, fragment_2_text)
    assert outcome_2.status is AssemblyStatus.COMPLETE
    assert outcome_2.sentences == (fragment_1_text, fragment_2_text)


# Cross-owner identity (Codex audit Finding A): assembly_namespace and
# session_handle are documented as process-wide identifiers, not merely
# unique within one SecureState owner. Two independent owners feeding a
# downstream assembler shared across them (as production code does: the
# assembler is a module-level singleton, not owned per-listener) must never
# mint the same value -- otherwise unrelated authenticated stations on
# different owners could have their multipart fragments combined.


def test_independent_secure_state_owners_never_mint_colliding_identifiers(
    monkeypatch,
):
    """Direct, fast proof of the underlying allocator invariant: two
    independently-constructed SecureState objects (as a multi-listener
    configuration, or simply two isolated test/owner instances, would
    create) never mint the same session_handle or assembly_namespace, so
    the assembler keys they derive can never collide either."""
    secure = load_secure_module_with_fake_keys(monkeypatch)
    owner_a = secure.SecureState()
    owner_b = secure.SecureState()

    session_a = owner_a.install_session(
        _relation_key(secure, ("192.0.2.150", 50040), secure._new_endpoint_token()),
        "boat_a",
        _fresh_test_locator(),
        object(),
        object(),
        now=0.0,
    )
    session_b = owner_b.install_session(
        _relation_key(secure, ("192.0.2.151", 50041), secure._new_endpoint_token()),
        "boat_b",
        _fresh_test_locator(),
        object(),
        object(),
        now=0.0,
    )

    assert session_a.session_handle != session_b.session_handle
    assert session_a.assembly_namespace != session_b.assembly_namespace
    assembler_key_a = f"udpsec-assembly:{session_a.assembly_namespace.hex()}"
    assembler_key_b = f"udpsec-assembly:{session_b.assembly_namespace.hex()}"
    assert assembler_key_a != assembler_key_b


def test_fresh_session_handle_draws_from_the_shared_identity_registry(
    monkeypatch,
):
    """`_fresh_session_handle()` is a thin wrapper over
    `_SESSION_IDENTITY_REGISTRY.reserve()`: fixed-size opaque bytes,
    distinct on every call, and immediately visible to the registry as
    live. An all-zero draw is not asserted to be impossible -- see
    tests/test_session_identity_registry.py for why a finite random space
    is a checked-and-retried probability, not a claim of mathematical
    impossibility."""
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()

    values = [state._fresh_session_handle() for _ in range(50)]

    for value in values:
        assert isinstance(value, bytes)
        assert len(value) == len(values[0])
        assert secure._SESSION_IDENTITY_REGISTRY.is_live(value)
    assert len(set(values)) == len(values)


def test_independently_loaded_module_copies_are_fully_separate(monkeypatch):
    """A test harness that deliberately loads `aismixer_secure.py` a
    second time under a synthetic module name (exactly what
    `load_secure_module_with_fake_keys` does for per-test isolation) gets
    its own independent module namespace -- `AUTHORIZED_KEYS`,
    `secure_state`, `_SESSION_IDENTITY_REGISTRY`, and every other piece of
    module state. This is the "separate simulated process" half of the
    sharing contract: see
    `test_two_secure_state_owners_from_one_import_share_the_identity_registry`
    below for the complementary "genuine sharing within one process"
    half."""
    first_copy = load_secure_module_with_fake_keys(monkeypatch)
    second_copy = load_secure_module_with_fake_keys(monkeypatch)

    assert first_copy is not second_copy
    assert first_copy.AUTHORIZED_KEYS is not second_copy.AUTHORIZED_KEYS
    assert first_copy.secure_state is not second_copy.secure_state
    assert (
        first_copy._SESSION_IDENTITY_REGISTRY
        is not second_copy._SESSION_IDENTITY_REGISTRY
    )

    # Independence is behavioral, not just object identity: a handle
    # reserved in one copy is not visible to the other at all.
    handle = first_copy.SecureState()._fresh_session_handle()
    assert first_copy._SESSION_IDENTITY_REGISTRY.is_live(handle)
    assert not second_copy._SESSION_IDENTITY_REGISTRY.is_live(handle)


def test_two_secure_state_owners_from_one_import_share_the_identity_registry(
    monkeypatch,
):
    """Two independent `SecureState()` instances constructed through the
    *same* loaded module (exactly like two physical listeners sharing one
    `secure_state` in production) reserve from the exact same registry
    object -- proving the sharing claim by observable behavior, not just
    by reading the module-level assignment. A value reserved via one
    owner's helper is immediately known to the other owner's registry
    view, and a forced collision across owners is resolved by retrying,
    never by both owners believing they hold the same value."""
    secure = load_secure_module_with_fake_keys(monkeypatch)
    owner_a = secure.SecureState()
    owner_b = secure.SecureState()

    handle_from_a = owner_a._fresh_session_handle()

    assert secure._SESSION_IDENTITY_REGISTRY.is_live(handle_from_a)

    draws = iter((handle_from_a, b"\x02" * len(handle_from_a)))
    monkeypatch.setattr(secure.os, "urandom", lambda _length: next(draws))

    handle_from_b = owner_b._fresh_session_handle()

    assert handle_from_b != handle_from_a
    assert secure._SESSION_IDENTITY_REGISTRY.is_live(handle_from_b)


def test_fresh_session_handle_is_safe_across_real_concurrent_threads(
    monkeypatch,
):
    """Baseline sanity check only: many real threads calling
    `_fresh_session_handle()` at once, with real (unmocked) `os.urandom`,
    produce no errors and no duplicates. This is necessary but not
    sufficient evidence of atomicity -- 128-bit random draws essentially
    never collide on their own regardless of locking, so this alone
    cannot distinguish a correctly-locked registry from an unlocked one.
    See `test_session_identity_registry_reserve_serializes_concurrent_callers`
    in tests/test_session_identity_registry.py for the deterministic,
    forced-collision proof of actual mutual exclusion."""
    import threading

    secure = load_secure_module_with_fake_keys(monkeypatch)
    thread_count = 8
    values_per_thread = 50
    results = [None] * thread_count
    errors = [None] * thread_count
    barrier = threading.Barrier(thread_count)

    state = secure.SecureState()

    def worker(index):
        try:
            barrier.wait(timeout=5.0)
            results[index] = [
                state._fresh_session_handle()
                for _ in range(values_per_thread)
            ]
        except BaseException as exc:  # noqa: BLE001 - captured for assertion
            errors[index] = exc

    threads = [
        threading.Thread(target=worker, args=(index,))
        for index in range(thread_count)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10.0)

    assert all(thread_errors is None for thread_errors in errors), errors
    assert all(thread_results is not None for thread_results in results)
    all_values = [value for batch in results for value in batch]
    assert len(all_values) == thread_count * values_per_thread
    assert len(set(all_values)) == len(all_values)


def test_capacity_eviction_releases_the_evicted_sessions_registry_reservations(
    monkeypatch,
):
    """Both identifiers of a capacity-evicted session must leave the
    "live" state (moving to "retiring", not vanishing outright, since a
    downstream assembler group may still be draining) -- while a
    surviving session's own identifiers are completely untouched."""
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(max_sessions=2)
    registry = secure._SESSION_IDENTITY_REGISTRY
    first = state.install_session(
        _relation_key(secure, ("192.0.2.10", 50000)),
        "first", _fresh_test_locator(), object(), object(), now=100.0)
    second = state.install_session(
        _relation_key(secure, ("192.0.2.11", 50001)),
        "second", _fresh_test_locator(), object(), object(), now=110.0)
    assert registry.is_live(first.session_handle)
    assert registry.is_live(first.assembly_namespace)

    third = state.install_session(
        _relation_key(secure, ("192.0.2.12", 50002)),
        "third", _fresh_test_locator(), object(), object(), now=130.0)

    assert state.stats().sessions_capacity_evicted == 1
    assert not registry.is_live(first.session_handle)
    assert registry.is_retiring(first.session_handle)
    assert not registry.is_live(first.assembly_namespace)
    assert registry.is_retiring(first.assembly_namespace)
    assert registry.is_live(second.session_handle)
    assert registry.is_live(second.assembly_namespace)
    assert registry.is_live(third.session_handle)
    assert registry.is_live(third.assembly_namespace)


def test_expiry_cleanup_releases_the_expired_sessions_registry_reservations(
    monkeypatch,
):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(session_ttl=30.0)
    registry = secure._SESSION_IDENTITY_REGISTRY
    expiring = state.install_session(
        _relation_key(secure, ("192.0.2.10", 50000)),
        "boat_001", _fresh_test_locator(), object(), object(), now=0.0)
    survivor = state.install_session(
        _relation_key(secure, ("192.0.2.11", 50001)),
        "boat_002", _fresh_test_locator(), object(), object(), now=25.0)

    state.cleanup_expired_sessions(now=31.0)

    assert not registry.is_live(expiring.session_handle)
    assert registry.is_retiring(expiring.session_handle)
    assert not registry.is_live(expiring.assembly_namespace)
    assert registry.is_live(survivor.session_handle)
    assert registry.is_live(survivor.assembly_namespace)


def test_close_session_releases_its_registry_reservations(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    registry = secure._SESSION_IDENTITY_REGISTRY
    session, _, _ = _install_test_session(
        secure, state, ("192.0.2.10", 50000), b"\x01" * 32, b"\x02" * 32,
    )
    assert registry.is_live(session.session_handle)

    assert state.close_session(session, now=1000.0)

    assert not registry.is_live(session.session_handle)
    assert registry.is_retiring(session.session_handle)
    assert not registry.is_live(session.assembly_namespace)
    assert registry.is_retiring(session.assembly_namespace)


def test_same_station_replacement_releases_old_handle_but_keeps_namespace_live(
    monkeypatch,
):
    """A same-relation, same-station replacement carries `assembly_namespace`
    forward (claim()s an extra live reference on the SAME reservation) but
    always mints a fresh `session_handle`: the old handle must leave the
    live set while the namespace value stays live throughout, owned now by
    the replacement rather than the replaced session."""
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    registry = secure._SESSION_IDENTITY_REGISTRY
    relation = _relation_key(secure, ("192.0.2.10", 50000))
    original = state.install_session(
        relation, "boat_001", _fresh_test_locator(), object(), object(),
        now=0.0)

    replacement = state.install_session(
        relation, "boat_001", _fresh_test_locator(), object(), object(),
        now=10.0)

    assert replacement.assembly_namespace == original.assembly_namespace
    assert replacement.session_handle != original.session_handle
    assert not registry.is_live(original.session_handle)
    assert registry.is_retiring(original.session_handle)
    assert registry.is_live(replacement.assembly_namespace)
    assert registry.is_live(replacement.session_handle)


def test_different_station_replacement_releases_both_old_identifiers(
    monkeypatch,
):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    registry = secure._SESSION_IDENTITY_REGISTRY
    relation = _relation_key(secure, ("192.0.2.10", 50000))
    original = state.install_session(
        relation, "boat_001", _fresh_test_locator(), object(), object(),
        now=0.0)

    replacement = state.install_session(
        relation, "boat_099", _fresh_test_locator(), object(), object(),
        now=10.0)

    assert replacement.assembly_namespace != original.assembly_namespace
    assert not registry.is_live(original.session_handle)
    assert not registry.is_live(original.assembly_namespace)
    assert registry.is_retiring(original.assembly_namespace)
    assert registry.is_live(replacement.session_handle)
    assert registry.is_live(replacement.assembly_namespace)


def test_nonce_exhaustion_releases_only_the_exact_owning_sessions_identifiers(
    monkeypatch,
):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(data_nonce_max_per_session=1, max_sessions=2)
    registry = secure._SESSION_IDENTITY_REGISTRY
    exhausted = state.install_session(
        _relation_key(secure, ("192.0.2.10", 50000)),
        "boat_001", _fresh_test_locator(), object(), object(), now=0.0)
    other = state.install_session(
        _relation_key(secure, ("192.0.2.11", 50001)),
        "boat_002", _fresh_test_locator(), object(), object(), now=0.0)
    assert state.accept_data_nonce(exhausted, b"\x01" * 12, now=0.0)

    assert state.admit_data_nonce(
        exhausted, b"\x02" * 12, now=1.0
    ) is secure._DataNonceAdmission.EXHAUSTED

    assert not registry.is_live(exhausted.session_handle)
    assert registry.is_retiring(exhausted.session_handle)
    assert not registry.is_live(exhausted.assembly_namespace)
    assert registry.is_live(other.session_handle)
    assert registry.is_live(other.assembly_namespace)


def test_pending_session_churn_never_touches_the_identity_registry(
    monkeypatch,
):
    """Pending candidates never reserve `session_handle`/`assembly_namespace`
    at all -- those are minted only at `install_session`/
    `promote_pending_session` time -- so a pending session that fails
    (collides, expires, or is capacity-evicted without ever promoting)
    must leave the registry completely untouched: no orphaned
    reservation, live or retiring."""
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(pending_session_ttl=30.0, max_pending_sessions=1)
    registry = secure._SESSION_IDENTITY_REGISTRY
    live_before = registry.live_count()
    retiring_before = registry.retiring_count()

    state.install_pending_session(
        _relation_key(secure, ("192.0.2.10", 50000)),
        "boat_001", _fresh_test_locator(), object(), object(), now=0.0)
    # Capacity-evicted before ever promoting.
    state.install_pending_session(
        _relation_key(secure, ("192.0.2.11", 50001)),
        "boat_002", _fresh_test_locator(), object(), object(), now=0.0)
    # Expired before ever promoting.
    state.cleanup_expired_pending_sessions(now=31.0)

    assert registry.live_count() == live_before
    assert registry.retiring_count() == retiring_before


# --- F1 (UDPSEC V2 Corrective Closure R4): exception-safe install/promote.
# `install_session()`/`promote_pending_session()` acquire an
# `assembly_namespace` (reserve or claim) and a `session_handle` BEFORE
# destructively removing the old owner at a relation, evicting a capacity
# victim, or popping a pending candidate. These tests deterministically
# force a failure at each fallible acquisition point (via controlled
# `os.urandom` draws or direct fault injection, never a statistical
# collision) and prove nothing valid was destroyed and nothing newly
# acquired was leaked.


def test_install_session_handle_exhaustion_preserves_old_session_same_station(
    monkeypatch,
):
    """Same-relation, same-station replacement: `_reused_or_fresh_assembly_
    namespace` claims the still-live old session's namespace (refcount 1
    -> 2) before `_fresh_session_handle()` is even attempted. Forcing that
    handle reservation to exhaust must release the claimed reference back
    to exactly 1 (still held solely by the untouched old session) and must
    not have removed that old session at all."""
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    registry = secure._SESSION_IDENTITY_REGISTRY
    relation = _relation_key(secure, ("192.0.2.10", 50000))
    original = state.install_session(
        relation, "boat_001", _fresh_test_locator(), object(), object(),
        now=0.0)

    always_occupied = original.session_handle
    monkeypatch.setattr(
        secure.os, "urandom", lambda _length: always_occupied
    )

    stats_before = state.stats()
    with pytest.raises(secure.SessionIdentityExhaustedError):
        state.install_session(
            relation, "boat_001", _fresh_test_locator(), object(),
            object(), now=1.0)

    assert state._sessions[original._session_key] is original
    assert state._active_session_at_relation(relation) is original
    assert state.stats() == stats_before
    assert registry.is_live(original.assembly_namespace)
    # No leaked extra reference: closing the untouched original must fully
    # release its namespace (retiring), not leave it live from a phantom
    # extra claim the failed replacement forgot to release.
    assert state.close_session(original, now=2.0)
    assert not registry.is_live(original.assembly_namespace)
    assert registry.is_retiring(original.assembly_namespace)


def test_install_session_handle_exhaustion_preserves_old_session_different_station(
    monkeypatch,
):
    """Same relation, but the replacement authenticates as a DIFFERENT
    station: `_reused_or_fresh_assembly_namespace` reserves a FRESH
    namespace (not a claim) before the handle reservation fails. That
    freshly reserved namespace must be released (not leaked as a
    permanently live reservation nobody owns), and the old session must
    be completely untouched."""
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    registry = secure._SESSION_IDENTITY_REGISTRY
    relation = _relation_key(secure, ("192.0.2.10", 50000))
    original = state.install_session(
        relation, "boat_001", _fresh_test_locator(), object(), object(),
        now=0.0)

    fresh_namespace_draw = b"\x02" * 16
    always_occupied = original.session_handle
    draws = iter([fresh_namespace_draw])

    def fake_urandom(_length):
        try:
            return next(draws)
        except StopIteration:
            return always_occupied

    monkeypatch.setattr(secure.os, "urandom", fake_urandom)

    stats_before = state.stats()
    with pytest.raises(secure.SessionIdentityExhaustedError):
        state.install_session(
            relation, "boat_099", _fresh_test_locator(), object(),
            object(), now=1.0)

    assert state._sessions[original._session_key] is original
    assert state.stats() == stats_before
    assert not registry.is_live(fresh_namespace_draw)


def test_install_session_handle_exhaustion_at_capacity_preserves_the_victim(
    monkeypatch,
):
    """F1's capacity-eviction variant: with the store already full, a
    handle-reservation failure for a genuinely new relation must not have
    evicted the LRU capacity victim first -- eviction only happens in the
    commit phase, strictly after every fallible acquisition already
    succeeded."""
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(max_sessions=1)
    registry = secure._SESSION_IDENTITY_REGISTRY
    victim = state.install_session(
        _relation_key(secure, ("192.0.2.10", 50000)),
        "boat_001", _fresh_test_locator(), object(), object(), now=0.0)

    fresh_namespace_draw = b"\x03" * 16
    always_occupied = victim.session_handle
    draws = iter([fresh_namespace_draw])

    def fake_urandom(_length):
        try:
            return next(draws)
        except StopIteration:
            return always_occupied

    monkeypatch.setattr(secure.os, "urandom", fake_urandom)

    stats_before = state.stats()
    with pytest.raises(secure.SessionIdentityExhaustedError):
        state.install_session(
            _relation_key(secure, ("192.0.2.11", 50001)),
            "boat_002", _fresh_test_locator(), object(), object(),
            now=1.0)

    assert state._sessions[victim._session_key] is victim
    assert state.stats() == stats_before
    assert not registry.is_live(fresh_namespace_draw)


def test_install_session_construction_failure_releases_all_newly_acquired(
    monkeypatch,
):
    """Direct fault injection: `LogicalSession` construction has no real
    data-dependent failure mode today, but `_prepare_candidate_session`
    must still release BOTH a freshly-reserved handle and namespace if
    construction itself raises for any reason."""
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    registry = secure._SESSION_IDENTITY_REGISTRY

    def broken_logical_session(*args, **kwargs):
        raise RuntimeError("injected construction failure")

    monkeypatch.setattr(secure, "LogicalSession", broken_logical_session)

    stats_before = state.stats()
    with pytest.raises(RuntimeError, match="injected construction failure"):
        state.install_session(
            _relation_key(secure, ("192.0.2.10", 50000)),
            "boat_001", _fresh_test_locator(), object(), object(),
            now=0.0)

    assert state.stats() == stats_before
    assert len(state._sessions) == 0
    assert registry.live_count() == 0
    assert registry.retiring_count() == 2  # released handle + namespace


def test_promote_pending_session_allocation_exhaustion_preserves_pending(
    monkeypatch,
):
    """F1 for promotion: an allocation failure (namespace or handle) must
    leave the pending candidate, its locator reservation, and its already-
    admitted confirmation-nonce accounting completely untouched -- popping
    the pending and releasing its locator only happens in the commit
    phase, after every fallible acquisition already succeeded."""
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    relation = _relation_key(secure, ("192.0.2.10", 50000))
    locator = _fresh_test_locator()
    pending = state.install_pending_session(
        relation, "boat_001", locator, object(), object(), now=0.0)
    confirmation_nonce = b"\x00" * 12
    assert state.accept_pending_data_nonce(
        pending, confirmation_nonce, now=0.0
    )

    # A value already live in the registry forces every draw (namespace
    # AND handle) to collide and exhaust deterministically.
    poison = secure._SESSION_IDENTITY_REGISTRY.reserve()
    monkeypatch.setattr(secure.os, "urandom", lambda _length: poison)

    pending_before = state._pending_sessions[relation]
    locator_owners_before = dict(state._pending_locator_owners)
    stats_before = state.stats()

    with pytest.raises(secure.SessionIdentityExhaustedError):
        state.promote_pending_session(pending, now=1.0)

    assert state._pending_sessions[relation] is pending_before
    assert state._pending_locator_owners == locator_owners_before
    assert state.stats() == stats_before
    # The already-admitted confirmation nonce is still recorded on the
    # untouched pending candidate's own ledger -- not orphaned.
    assert state.pending_data_nonce_seen(
        pending, confirmation_nonce, now=1.0
    )


def test_run_periodic_maintenance_cleans_up_idle_expired_state_without_packets(
    monkeypatch,
):
    """F3: expiry and retirement-purge housekeeping must run eventually
    even when no packet arrives on any listener to trigger it as a side
    effect -- proven here with a short real interval and a controllable
    monotonic clock, not a live production-length wait."""
    secure = load_secure_module_with_fake_keys(monkeypatch)
    clock = _FakeClock(0.0)
    state = secure.SecureState(session_ttl=10.0, clock=clock)
    registry = secure._SESSION_IDENTITY_REGISTRY
    session = state.install_session(
        _relation_key(secure, ("192.0.2.10", 50000)),
        "boat_001", _fresh_test_locator(), object(), object(), now=0.0)
    clock.now = 11.0  # already past the 10-second TTL by the time
    # maintenance ticks -- installed while the clock read 0.0 above.

    async def scenario():
        task = asyncio.create_task(
            state.run_periodic_maintenance(interval=0.01)
        )
        try:
            await asyncio.sleep(0.05)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(scenario())

    assert len(state._sessions) == 0
    assert state.stats().sessions_expired == 1
    assert not registry.is_live(session.session_handle)
    assert registry.is_retiring(session.session_handle)


def test_run_periodic_maintenance_stops_cleanly_on_cancellation(monkeypatch):
    """One owner's maintenance task can be cancelled independently -- it
    must exit via CancelledError without raising anything else or leaving
    the state lock held."""
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()

    async def scenario():
        task = asyncio.create_task(state.run_periodic_maintenance(interval=60.0))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    # The lock is usable afterward -- cancellation while parked in
    # `asyncio.sleep()` (never inside the lock) left nothing held.
    assert state.stats().current_sessions == 0


def test_two_real_threads_racing_commit_order_does_not_defeat_expiry(
    monkeypatch,
):
    """F2's exact reported reproduction, at the SecureState level: two
    real OS threads share one owner. Thread A's touch is made to COMMIT
    FIRST while reading the LARGER `now` (6); thread B's commits SECOND
    while reading the SMALLER `now` (5) -- exactly the pathological
    OrderedDict order [6, 5] the audit reported. Without heap-
    authoritative expiry, checking at `now=15` with `session_ttl=10`
    would stop at the front entry (A, 15-6=9<10, not yet due) and never
    even look at the entry behind it (B, 15-5=10>=10, genuinely due)."""
    import threading

    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(session_ttl=10.0)
    session_a = state.install_session(
        _relation_key(secure, ("192.0.2.10", 50000)),
        "boat_a", _fresh_test_locator(), object(), object(), now=0.0)
    session_b = state.install_session(
        _relation_key(secure, ("192.0.2.11", 50001)),
        "boat_b", _fresh_test_locator(), object(), object(), now=0.0)

    a_committed = threading.Event()

    def thread_a():
        state.touch_session(session_a, now=6.0)
        a_committed.set()

    def thread_b():
        assert a_committed.wait(timeout=5.0)
        state.touch_session(session_b, now=5.0)

    ta = threading.Thread(target=thread_a)
    tb = threading.Thread(target=thread_b)
    tb.start()
    ta.start()
    ta.join(timeout=5.0)
    tb.join(timeout=5.0)

    assert session_a.last_seen == 6.0
    assert session_b.last_seen == 5.0
    assert tuple(state._sessions) == (
        session_a._session_key,
        session_b._session_key,
    )

    expired = state.cleanup_expired_sessions(now=15.0)

    assert session_b._session_key in expired
    assert session_a._session_key not in expired
    assert state.is_live_session_handle(session_a, now=15.0)
    assert state._active_session_at_relation(
        _relation_key(secure, ("192.0.2.11", 50001))
    ) is None


def test_two_real_threads_racing_pending_commit_order_does_not_defeat_expiry(
    monkeypatch,
):
    """Pending equivalent, including sequence-zero confirmation: pending
    candidate B (created_at=5, genuinely overdue at now=15 under
    pending_session_ttl=10) must not survive behind pending candidate A
    (created_at=6, not yet due) merely because A's install committed
    first."""
    import threading

    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(pending_session_ttl=10.0)
    relation_a = _relation_key(secure, ("192.0.2.10", 50000))
    relation_b = _relation_key(secure, ("192.0.2.11", 50001))
    results = {}
    a_committed = threading.Event()

    def thread_a():
        results["pending_a"] = state.install_pending_session(
            relation_a, "boat_a", _fresh_test_locator(), object(),
            object(), now=6.0)
        a_committed.set()

    def thread_b():
        assert a_committed.wait(timeout=5.0)
        results["pending_b"] = state.install_pending_session(
            relation_b, "boat_b", _fresh_test_locator(), object(),
            object(), now=5.0)

    tb = threading.Thread(target=thread_b)
    ta = threading.Thread(target=thread_a)
    tb.start()
    ta.start()
    ta.join(timeout=5.0)
    tb.join(timeout=5.0)

    pending_a = results["pending_a"]
    pending_b = results["pending_b"]
    assert tuple(state._pending_sessions) == (relation_a, relation_b)

    confirmation_nonce = b"\x00" * 12
    # B is genuinely overdue at now=15 (15-5=10>=10): its confirmation
    # nonce must not be admitted, and it must not be promotable.
    assert not state.accept_pending_data_nonce(
        pending_b, confirmation_nonce, now=15.0
    )
    assert state.promote_pending_session(pending_b, now=15.0) is None
    assert state.get_pending_session(relation_b, now=15.0) is None

    # A is genuinely not yet due (15-6=9<10) and remains fully valid.
    assert state.accept_pending_data_nonce(
        pending_a, confirmation_nonce, now=15.0
    )
    assert state.promote_pending_session(pending_a, now=15.0) is not None


def test_r6_sustained_pending_replacement_churn_keeps_expiry_heap_bounded(
    monkeypatch,
):
    """R6/Blocker A: the exact Codex-reported reproduction. Before this
    fix, `_pending_expiry_heap` entries were keyed only by `relation_key`
    -- NOT fresh across a same-relation replacement, unlike an active
    session's locator-derived key -- so a stale entry for an already-
    replaced candidate, once popped, would still resolve `relation_key`
    to whatever candidate currently occupied it and recompute/reschedule
    THAT candidate's deadline on the strength of an entry that was never
    pushed for it. The result: with pending_session_ttl=3 and one
    replacement per second, cleanup after every operation, only entries
    at the very FRONT of the heap were ever due at all (the rest sat
    behind, never yet due), so the heap simply grew by one entry per
    replacement -- 1000 replacements, 1000 heap entries, despite exactly
    one live pending candidate the entire time. This is now fixed by
    carrying the exact pushed object in each heap entry and discarding
    (never recomputing/rescheduling) any entry whose object no longer
    matches what currently occupies its key."""
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(pending_session_ttl=3.0, max_pending_sessions=1)
    relation = _relation_key(secure, ("192.0.2.50", 50000))
    replacement_count = 1000

    for step in range(replacement_count):
        now = float(step)
        state.install_pending_session(
            relation, "boat_001", _fresh_test_locator(), object(), object(),
            now,
        )
        state.cleanup_expired_pending_sessions(now)

    stats = state.stats()
    assert stats.pending_sessions_created == replacement_count
    assert stats.current_pending_sessions == 1
    assert len(state._pending_sessions) == 1
    # The historical defect made this proportional to `replacement_count`
    # (1000). A correctly bounded heap never needs to retain more than a
    # small handful of entries relative to how many are due per cleanup
    # call: at most one authoritative entry per still-live candidate, plus
    # whatever stale entries have been pushed since the last cleanup but
    # have not yet reached the front of the heap.
    assert len(state._pending_expiry_heap) <= 5, (
        f"pending expiry heap retained {len(state._pending_expiry_heap)} "
        f"entries after {replacement_count} same-relation replacements "
        "with cleanup after each -- expiry metadata is growing with total "
        "historical churn, not with live/due state"
    )


def test_r6_stale_pending_heap_entry_cannot_expire_or_reschedule_a_replacement(
    monkeypatch,
):
    """Surgical proof of the mechanism, independent of the bulk-churn
    test above: candidate A's OWN heap entry, once stale, must never be
    interpreted as authority over candidate B (a same-relation
    replacement) -- neither to prematurely expire B nor to silently
    reschedule B's deadline on A's behalf."""
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(pending_session_ttl=3.0)
    relation = _relation_key(secure, ("192.0.2.51", 50000))

    pending_a = state.install_pending_session(
        relation, "boat_001", _fresh_test_locator(), object(), object(),
        now=0.0,
    )
    # A's own entry: (3.0, seq, relation, pending_a). Replace at now=1.0
    # with B, which is NOT yet due at A's original deadline (3.0).
    pending_b = state.install_pending_session(
        relation, "boat_001", _fresh_test_locator(), object(), object(),
        now=1.0,
    )
    assert state._pending_sessions[relation] is pending_b

    # At now=3.0, A's stale entry becomes due (3.0 <= 3.0) -- but A is
    # long gone. B's own real deadline is 1.0+3.0=4.0, still not due.
    state.cleanup_expired_pending_sessions(now=3.0)
    assert state._pending_sessions.get(relation) is pending_b, (
        "a stale entry belonging to a replaced candidate must never "
        "expire the candidate that replaced it"
    )
    assert state.stats().pending_sessions_expired == 0

    # B's own, correct deadline (4.0) is still honored.
    state.cleanup_expired_pending_sessions(now=4.0)
    assert relation not in state._pending_sessions
    assert state.stats().pending_sessions_expired == 1


def test_r6_stale_active_heap_entry_cannot_remove_or_reschedule_a_reused_locator(
    monkeypatch,
):
    """Active-session equivalent, using a directly-controlled (not
    randomly drawn) locator reused across two unrelated relations after
    the first session is long gone -- a deterministic stand-in for the
    2**128-scale coincidence a real random reissue would require. Once
    reused, the new incarnation must be governed only by its own object
    identity and its own `last_seen`, never by a stale entry pushed for
    the session that previously held that locator."""
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(session_ttl=5.0)
    relation_a = _relation_key(secure, ("192.0.2.52", 50000))
    relation_b = _relation_key(secure, ("192.0.2.53", 50001))
    shared_locator = _fresh_test_locator()

    session_a = state.install_session(
        relation_a, "boat_001", shared_locator, object(), object(), now=0.0,
    )
    assert state.close_session(session_a, now=0.5)
    # session_a's own heap entry (deadline 5.0) is now stale -- lazily
    # left behind, exactly like production.

    session_b = state.install_session(
        relation_b, "boat_002", shared_locator, object(), object(), now=1.0,
    )
    assert session_b._session_key == session_a._session_key
    assert session_b is not session_a

    # At now=5.0, session_a's stale entry becomes due (5.0<=5.0), but the
    # SAME KEY now names session_b, whose own real deadline is 1.0+5.0=6.0
    # -- not due yet, and must not be disturbed by an entry that was
    # never pushed for it.
    state.cleanup_expired_sessions(now=5.0)
    assert state._sessions.get(session_b._session_key) is session_b, (
        "a stale entry belonging to a removed, reused-key incarnation "
        "must never remove or reschedule the incarnation that reused it"
    )
    assert state.stats().sessions_expired == 0

    state.cleanup_expired_sessions(now=6.0)
    assert session_b._session_key not in state._sessions
    assert state.stats().sessions_expired == 1


def test_r6_high_rate_active_touches_do_not_grow_retained_expiry_metadata(
    monkeypatch,
):
    """A session touched thousands of times over a period exceeding its
    TTL must retain expiry metadata bounded by the number of LIVE
    sessions, not by the total number of historical touches: `touch_session`
    never pushes a new heap entry (only `install_session`/
    `promote_pending_session` do), so at most one authoritative entry for
    this session can ever exist at a time, occasionally recomputed/
    re-pushed in place when a stale entry predates a later touch."""
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(session_ttl=10.0)
    session = state.install_session(
        _relation_key(secure, ("192.0.2.54", 50000)),
        "boat_001", _fresh_test_locator(), object(), object(), now=0.0,
    )

    touch_count = 5000
    for step in range(1, touch_count + 1):
        now = step * 0.01  # spans 0.01 .. 50.0, well past session_ttl=10.0
        assert state.touch_session(session, now)
        state.cleanup_expired_sessions(now)

    assert state.stats().sessions_touched == touch_count
    assert state.stats().sessions_expired == 0
    assert session._session_key in state._sessions
    assert len(state._session_expiry_heap) <= 2, (
        f"active session expiry heap retained "
        f"{len(state._session_expiry_heap)} entries after {touch_count} "
        "touches -- retained metadata is growing with total historical "
        "touch count, not with live session count"
    )


def test_stale_time_touch_cannot_move_last_seen_backwards_or_revive_expiry(
    monkeypatch,
):
    """F2: a `now` sampled before this lock was acquired can arrive at
    `touch_session` after a later, larger `now` already committed (the
    same out-of-order-commit hazard as the two-thread tests above). The
    resulting stale, smaller `now` must never move `last_seen` backwards
    -- doing so would shorten the session's real remaining lifetime and
    could let a stale heap re-push (see `_push_session_expiry`'s lazy
    recompute) compute an artificially-early deadline."""
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(session_ttl=10.0)
    session = state.install_session(
        _relation_key(secure, ("192.0.2.10", 50000)),
        "boat_001", _fresh_test_locator(), object(), object(), now=0.0)

    assert state.touch_session(session, now=8.0)
    assert session.last_seen == 8.0

    # A stale, smaller `now` arrives after the later touch already
    # committed -- must not move last_seen backwards to 3.0.
    assert state.touch_session(session, now=3.0)
    assert session.last_seen == 8.0

    # Real deadline is 8.0 + 10.0 = 18.0, not 3.0 + 10.0 = 13.0: the
    # session must still be live at now=15 and only actually expire once
    # 18.0 is reached.
    assert state.is_live_session_handle(session, now=15.0)
    expired = state.cleanup_expired_sessions(now=15.0)
    assert session._session_key not in expired

    expired = state.cleanup_expired_sessions(now=18.0)
    assert session._session_key in expired


# --- R5/F2: authoritative monotonic time at state admission. The R4 heaps
# correctly find every expired entry regardless of commit/insertion order,
# but that alone does not protect against a caller supplying a STALE `now`
# value for the admission decision itself (sampled before a delay, in a
# different thread, or held on an old asynchronous reference) -- if that
# stale value has not itself passed the deadline, R4's own heap logic
# would (correctly, given that input) treat the object as still live. A
# `SecureState` configured with `clock=` floors every supplied `now` at
# that clock's own reading, so a stale value can no longer make an
# object the clock knows is already past its deadline appear live.


def test_stale_now_cannot_admit_data_nonce_past_the_authoritative_deadline(
    monkeypatch,
):
    """Active owner last_seen=0, TTL=10: a caller samples time 5 (stale),
    is delayed, then attempts admission once the authoritative clock
    already reads 11 -- past the deadline. It must be rejected, even
    though periodic maintenance has not run and even though 5 itself is
    not past the (stale) deadline the caller believes it is checking
    against."""
    secure = load_secure_module_with_fake_keys(monkeypatch)
    clock = _FakeClock(0.0)
    state = secure.SecureState(session_ttl=10.0, clock=clock)
    session = state.install_session(
        _relation_key(secure, ("192.0.2.10", 50000)),
        "boat_001", _fresh_test_locator(), object(), object(), now=0.0)

    clock.now = 11.0  # authoritative time has moved past the deadline
    stale_now = 5.0  # the caller's own (stale) sample, still < deadline

    assert state.admit_data_nonce(
        session, b"\x01" * 12, stale_now
    ) is secure._DataNonceAdmission.STALE
    assert not state.is_live_session_handle(session, stale_now)
    assert state._active_session_at_relation(
        _relation_key(secure, ("192.0.2.10", 50000))
    ) is None


def test_stale_now_cannot_admit_or_promote_pending_past_authoritative_deadline(
    monkeypatch,
):
    """Pending candidate created_at=0, TTL=10: stale time 5 cannot admit
    the sequence-zero confirmation nonce or promote once the authoritative
    clock already reads 11."""
    secure = load_secure_module_with_fake_keys(monkeypatch)
    clock = _FakeClock(0.0)
    state = secure.SecureState(pending_session_ttl=10.0, clock=clock)
    relation_key = _relation_key(secure, ("192.0.2.10", 50000))
    pending = state.install_pending_session(
        relation_key, "boat_001", _fresh_test_locator(), object(),
        object(), now=0.0)

    clock.now = 11.0
    stale_now = 5.0

    assert not state.accept_pending_data_nonce(
        pending, b"\x00" * 12, stale_now
    )
    assert state.promote_pending_session(pending, stale_now) is None
    assert state.get_pending_session(relation_key, stale_now) is None


def test_stale_touch_cannot_revive_an_already_expired_owner(monkeypatch):
    """Touch's `last_seen = max(last_seen, now)` clamp (R4) prevents
    moving last_seen BACKWARDS, but that alone does not stop a stale `now`
    from resurrecting a session the authoritative clock already knows is
    past its deadline: `touch_session` must still fail once the
    authoritative clock has moved past `last_seen + ttl`, regardless of
    what (smaller, but still >= last_seen) `now` the caller supplies."""
    secure = load_secure_module_with_fake_keys(monkeypatch)
    clock = _FakeClock(0.0)
    state = secure.SecureState(session_ttl=10.0, clock=clock)
    session = state.install_session(
        _relation_key(secure, ("192.0.2.10", 50000)),
        "boat_001", _fresh_test_locator(), object(), object(), now=0.0)

    clock.now = 11.0
    stale_now = 5.0  # >= last_seen (0), so the R4 clamp alone would allow it

    assert not state.touch_session(session, stale_now)
    assert session.last_seen == 0.0
    assert state._active_session_at_relation(
        _relation_key(secure, ("192.0.2.10", 50000))
    ) is None


def test_authoritative_clock_does_not_affect_a_caller_now_that_is_ahead(
    monkeypatch,
):
    """The clock is a FLOOR, not a hard override: a caller-supplied `now`
    already at or ahead of the configured clock's own (unmoved) reading
    is used unchanged -- ordinary deterministic testing (advancing `now`
    directly, without also advancing a configured clock) is unaffected."""
    secure = load_secure_module_with_fake_keys(monkeypatch)
    clock = _FakeClock(0.0)  # never advanced in this test
    state = secure.SecureState(session_ttl=10.0, clock=clock)
    session = state.install_session(
        _relation_key(secure, ("192.0.2.10", 50000)),
        "boat_001", _fresh_test_locator(), object(), object(), now=0.0)

    # now=9.0 is ahead of the unmoved clock (0.0); the caller's own value
    # must win, exactly as if no clock were configured at all.
    assert state.touch_session(session, now=9.0)
    assert session.last_seen == 9.0
    assert state.is_live_session_handle(session, now=9.0)

    # Still not expired at now=18.0 (9.0 + ttl(10) = 19.0 > 18.0), even
    # though that is also far ahead of the clock's own stale 0.0 reading.
    assert state.is_live_session_handle(session, now=18.0)


def test_two_real_threads_stale_now_cannot_revive_state_past_authoritative_clock(
    monkeypatch,
):
    """R5/F2 with two real OS threads: one shared SecureState is
    configured with an authoritative clock. Thread A advances that shared
    clock past the session's deadline (simulating real elapsed time
    passing); thread B, racing concurrently, attempts admission using a
    STALE `now` sampled from BEFORE that advance -- reversed relative to
    when the authoritative clock actually moved. The admission must still
    be rejected: the shared authoritative clock, not whichever thread's
    own stale sample happens to be used, governs the decision."""
    import threading

    secure = load_secure_module_with_fake_keys(monkeypatch)
    clock = _FakeClock(0.0)
    state = secure.SecureState(session_ttl=10.0, clock=clock)
    session = state.install_session(
        _relation_key(secure, ("192.0.2.10", 50000)),
        "boat_001", _fresh_test_locator(), object(), object(), now=0.0)

    a_advanced_clock = threading.Event()
    results = {}

    def thread_a():
        clock.now = 11.0  # real elapsed time moves past the deadline
        a_advanced_clock.set()

    def thread_b():
        stale_now = 5.0  # sampled as if before thread A's advance
        assert a_advanced_clock.wait(timeout=5.0)
        results["admission"] = state.admit_data_nonce(
            session, b"\x01" * 12, stale_now
        )

    ta = threading.Thread(target=thread_a)
    tb = threading.Thread(target=thread_b)
    ta.start()
    tb.start()
    ta.join(timeout=5.0)
    tb.join(timeout=5.0)

    assert results["admission"] is secure._DataNonceAdmission.STALE
    assert state._active_session_at_relation(
        _relation_key(secure, ("192.0.2.10", 50000))
    ) is None


def test_periodic_maintenance_uses_this_owners_authoritative_clock_domain(
    monkeypatch,
):
    """Maintenance must never use a clock reading behind this owner's own
    authoritative floor: even if maintenance itself observed a smaller
    value at some point, the `_authoritative_now()` floor inside
    `cleanup_expired_sessions` still applies, so a later state transition
    at this owner can never be overridden by an older maintenance
    observation. Exercised by configuring an ADVANCING clock: maintenance
    reads it once per tick, always current at the moment it ticks."""
    secure = load_secure_module_with_fake_keys(monkeypatch)
    clock = _FakeClock(0.0)
    state = secure.SecureState(session_ttl=10.0, clock=clock)
    session = state.install_session(
        _relation_key(secure, ("192.0.2.10", 50000)),
        "boat_001", _fresh_test_locator(), object(), object(), now=0.0)

    async def scenario():
        task = asyncio.create_task(
            state.run_periodic_maintenance(interval=0.01)
        )
        try:
            await asyncio.sleep(0.03)
            # Still not due: maintenance's own tick(s) so far observed
            # the clock before it advanced past the deadline.
            assert state._active_session_at_relation(
                _relation_key(secure, ("192.0.2.10", 50000))
            ) is session
            clock.now = 11.0
            await asyncio.sleep(0.03)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(scenario())

    assert len(state._sessions) == 0
    assert state.stats().sessions_expired == 1


# --- R5/F5: complete independent-owner teardown via SecureState.close().
# A DIFFERENT, WIDER operation than close_owned_sessions()/
# close_owned_pending_sessions() (R4), which discard only what ONE
# listener tracks -- close() discards EVERYTHING this exact owner holds
# and ends its lifecycle.


def test_close_discards_all_active_and_pending_state(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    session = state.install_session(
        _relation_key(secure, ("192.0.2.10", 50000)),
        "boat_001", _fresh_test_locator(), object(), object(), now=0.0)
    pending = state.install_pending_session(
        _relation_key(secure, ("192.0.2.11", 50001)),
        "boat_002", _fresh_test_locator(), object(), object(), now=0.0)

    state.close(now=1.0)

    assert len(state._sessions) == 0
    assert len(state._pending_sessions) == 0
    assert len(state._relation_index) == 0
    assert len(state._pending_locator_owners) == 0
    stats = state.stats()
    assert stats.sessions_closed == 1
    assert stats.pending_sessions_closed == 1
    assert stats.current_sessions == 0
    assert stats.current_pending_sessions == 0
    assert not state.is_live_session_handle(session, now=1.0)
    assert state.get_pending_session(pending._relation_key, now=1.0) is None


def test_close_releases_exactly_this_owners_registry_reservations(
    monkeypatch,
):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    registry = secure._SESSION_IDENTITY_REGISTRY
    session = state.install_session(
        _relation_key(secure, ("192.0.2.10", 50000)),
        "boat_001", _fresh_test_locator(), object(), object(), now=0.0)
    handle, namespace = session.session_handle, session.assembly_namespace
    assert registry.is_live(handle)
    assert registry.is_live(namespace)

    state.close(now=1.0)

    assert not registry.is_live(handle)
    assert registry.is_retiring(handle)
    assert not registry.is_live(namespace)
    assert registry.is_retiring(namespace)


def test_close_does_not_affect_a_second_owner_sharing_the_registry(
    monkeypatch,
):
    """A second `SecureState()` constructed through the SAME loaded
    module shares the process-wide identity registry (see
    `_SESSION_IDENTITY_REGISTRY`'s own sharing model) -- closing one
    owner must not touch the other owner's live sessions, its own
    registry reservations, or its own stores at all."""
    secure = load_secure_module_with_fake_keys(monkeypatch)
    registry = secure._SESSION_IDENTITY_REGISTRY
    owner_a = secure.SecureState()
    owner_b = secure.SecureState()
    session_a = owner_a.install_session(
        _relation_key(secure, ("192.0.2.10", 50000)),
        "boat_a", _fresh_test_locator(), object(), object(), now=0.0)
    session_b = owner_b.install_session(
        _relation_key(secure, ("192.0.2.11", 50001)),
        "boat_b", _fresh_test_locator(), object(), object(), now=0.0)

    owner_a.close(now=1.0)

    assert len(owner_a._sessions) == 0
    assert not registry.is_live(session_a.session_handle)
    # Owner B is completely unaffected: its store, its exact session
    # object, and its own registry reservations are all still live.
    assert owner_b._sessions[session_b._session_key] is session_b
    assert registry.is_live(session_b.session_handle)
    assert registry.is_live(session_b.assembly_namespace)
    assert owner_b.is_live_session_handle(session_b, now=1.0)


def test_close_is_idempotent_and_stale_exact_session_close_does_not_double_release(
    monkeypatch,
):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    registry = secure._SESSION_IDENTITY_REGISTRY
    session = state.install_session(
        _relation_key(secure, ("192.0.2.10", 50000)),
        "boat_001", _fresh_test_locator(), object(), object(), now=0.0)
    handle = session.session_handle

    state.close(now=1.0)
    stats_after_first_close = state.stats()

    # Calling close() again is a no-op, not an error and not a re-count.
    state.close(now=2.0)
    assert state.stats() == stats_after_first_close

    # A listener's own exact-object close (e.g. from a racing
    # close_owned_sessions() shutdown path) against a session close()
    # already removed must be a safe no-op too -- exact-object liveness
    # checking already covers this, with no double release.
    assert not state.close_session(session, now=3.0)
    assert state.stats() == stats_after_first_close
    assert registry.is_retiring(handle)
    assert not registry.is_live(handle)


def test_install_after_close_raises_and_does_not_create_state(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    state.close(now=0.0)

    with pytest.raises(secure.SecureStateClosedError):
        state.install_session(
            _relation_key(secure, ("192.0.2.10", 50000)),
            "boat_001", _fresh_test_locator(), object(), object(), now=1.0)
    with pytest.raises(secure.SecureStateClosedError):
        state.install_pending_session(
            _relation_key(secure, ("192.0.2.11", 50001)),
            "boat_002", _fresh_test_locator(), object(), object(), now=1.0)

    assert len(state._sessions) == 0
    assert len(state._pending_sessions) == 0


def test_close_then_periodic_maintenance_tick_does_not_resurrect_or_crash(
    monkeypatch,
):
    """A maintenance task still running (or ticking one more time before
    its owner cancels it) against an already-closed, now-empty owner must
    be a harmless no-op -- no crash, and certainly no resurrected
    session."""
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    state.install_session(
        _relation_key(secure, ("192.0.2.10", 50000)),
        "boat_001", _fresh_test_locator(), object(), object(), now=0.0)
    state.close(now=1.0)

    async def scenario():
        task = asyncio.create_task(
            state.run_periodic_maintenance(interval=0.01)
        )
        try:
            await asyncio.sleep(0.05)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(scenario())

    assert len(state._sessions) == 0
    assert state.stats().current_sessions == 0


def test_r6_close_discards_handshake_replay_ledger_and_expiry_heaps(
    monkeypatch,
):
    """R6/F5 residual: R5's `close()` correctly discarded active/pending
    state and released this owner's registry reservations, but left the
    handshake replay ledger and both expiry heaps behind -- dead weight
    for the heaps (harmless given the R6/Blocker-A identity check, but
    needless retained memory), and a live security-relevant defect for
    the replay ledger (see the next test)."""
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    state.install_session(
        _relation_key(secure, ("192.0.2.60", 50000)),
        "boat_001", _fresh_test_locator(), object(), object(), now=0.0)
    state.install_pending_session(
        _relation_key(secure, ("192.0.2.61", 50001)),
        "boat_002", _fresh_test_locator(), object(), object(), now=0.0)
    assert state.accept_handshake_replay(b"\x01" * 32, now=0.0)
    assert len(state._handshake_replays) == 1
    assert len(state._session_expiry_heap) == 1
    assert len(state._pending_expiry_heap) == 1

    state.close(now=1.0)

    assert len(state._handshake_replays) == 0
    assert len(state._session_expiry_heap) == 0
    assert len(state._pending_expiry_heap) == 0
    assert state.stats().current_handshake_replays == 0


def test_r6_closed_owner_refuses_new_handshake_replay_records(monkeypatch):
    """R6/F5 residual: a terminal owner must not accept new replay
    records either -- otherwise a post-close ClientHello (from a
    listener that has not yet noticed shutdown, or a lingering direct
    caller in a test) could silently repopulate a ledger `close()` just
    emptied, defeating the "no resurrection" guarantee for the one store
    that isn't keyed by an object `close()` already removed."""
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    state.close(now=0.0)

    accepted = state.accept_handshake_replay(b"\x02" * 32, now=1.0)

    assert accepted is False
    assert len(state._handshake_replays) == 0
    assert state.stats().handshake_replay_rejected == 1
    assert state.stats().handshake_replay_accepted == 0


def test_r6_secure_server_loop_rejects_handshake_after_owner_close(
    monkeypatch,
):
    """Same property, through the real `_secure_server_loop` receive
    path: a ClientHello arriving at a closed owner must be rejected as
    an ordinary handshake failure (logged, no ServerHello sent, no
    pending candidate created), not raise `SecureStateClosedError` out
    of the listener."""
    secure, client_identity_private_key = load_secure_module_with_fake_keys(
        monkeypatch, with_client_private_key=True
    )
    state = secure.SecureState()
    state.close(now=0.0)
    packet = _signed_handshake_packet(
        secure, client_identity_private_key, "boat_001", 1000,
    )

    _, fake_socket = _run_secure_server_with_packets(
        monkeypatch,
        secure,
        [(packet, ("127.0.0.1", 50333))],
        state=state,
        wall_clock=_FakeClock(1000.0),
        monotonic_clock=_FakeClock(10.0),
    )

    assert fake_socket.sent == []
    assert len(state._pending_sessions) == 0


def test_r6_close_owner_close_guarantees_final_state_regardless_of_prior_errors(
    monkeypatch,
):
    """Limited scope check: `close()` completes the FULL sweep (every
    session, every pending candidate, the replay ledger, both heaps, the
    `_closed` flag) as one sequence -- it does not stop partway and
    leave the owner in an ambiguous "partially closed" state that a
    caller could not safely retry or reason about, even though no
    individual removal step in this class is expected to raise under
    normal conditions."""
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    state.install_session(
        _relation_key(secure, ("192.0.2.62", 50000)),
        "boat_001", _fresh_test_locator(), object(), object(), now=0.0)
    state.install_session(
        _relation_key(secure, ("192.0.2.63", 50001)),
        "boat_002", _fresh_test_locator(), object(), object(), now=0.0)
    state.install_pending_session(
        _relation_key(secure, ("192.0.2.64", 50002)),
        "boat_003", _fresh_test_locator(), object(), object(), now=0.0)

    state.close(now=1.0)

    assert state._closed is True
    assert len(state._sessions) == 0
    assert len(state._pending_sessions) == 0
    assert len(state._handshake_replays) == 0
    assert len(state._session_expiry_heap) == 0
    assert len(state._pending_expiry_heap) == 0


def test_f4_delayed_old_frame_after_namespace_reuse_does_not_cross_combine(
    monkeypatch,
):
    """F4's exact previously-reported scenario, now closed: an old
    authenticated frame is admitted, deliberately delayed (simulating
    queue backpressure with no maximum residence time of its own), its
    session closes, its `assembly_namespace` retirement window elapses
    and is purged, a controlled allocator draw reuses that exact
    identifier for an unrelated new station, and both the old (delayed)
    and new fragments reach one real `AIVDMAssembler` through the real
    `PythonDataPlaneProcessor`. The old frame's `admitted_at` timestamp is
    from before the retirement window even started, so it is refused as
    stale before ever reaching the assembler -- no cross-station
    multipart completion can occur, regardless of how the registry's
    retirement window alone might otherwise interact with pipeline
    residence time."""
    from assembler import AIVDMAssembler
    from core.data_plane import DeduplicationMode, ProcessingSnapshot
    from core.ingress_frame import IngressFrame
    from core.python_data_plane import (
        MAX_INGRESS_FRAME_AGE_SECONDS,
        PythonDataPlaneProcessor,
    )
    from core.session_identity_registry import RETIREMENT_SECONDS

    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    registry = secure._SESSION_IDENTITY_REGISTRY

    session_a = state.install_session(
        _relation_key(secure, ("192.0.2.10", 50000)),
        "boat_001", _fresh_test_locator(), object(), object(), now=0.0)
    reused_namespace = session_a.assembly_namespace
    reused_assembler_key = f"udpsec-assembly:{reused_namespace.hex()}"

    old_fragment_text = "!AIVDM,2,1,9,A,delayed-old,0*00"
    old_frame = IngressFrame(
        kind="sec",
        source_id="udpsec:boat_001",
        alias_for_s="boat_001",
        remote_ip="192.0.2.10",
        assembler_key=reused_assembler_key,
        payload=old_fragment_text.encode("utf-8"),
        admitted_at=0.0,
    )

    # Session A closes; its namespace enters retirement, then the window
    # elapses and is purged via the existing monotonic cleanup pass.
    assert state.close_session(session_a, now=1.0)
    assert registry.is_retiring(reused_namespace)
    purge_time = 1.0 + RETIREMENT_SECONDS + 1.0
    state.cleanup_expired_sessions(now=purge_time)
    assert not registry.is_retiring(reused_namespace)
    assert not registry.is_live(reused_namespace)

    # Controlled allocator draw: the exact same identifier is legitimately
    # reserved again for a brand-new, unrelated station. Only the first
    # draw (assembly_namespace) needs to be forced onto the reused value;
    # the second draw (session_handle) must land on a genuinely free
    # value, not collide with the namespace it was just handed.
    draws = iter([reused_namespace])
    fresh_handle = b"\x99" * len(reused_namespace)

    def fake_urandom(_length):
        try:
            return next(draws)
        except StopIteration:
            return fresh_handle

    monkeypatch.setattr(secure.os, "urandom", fake_urandom)
    session_b = state.install_session(
        _relation_key(secure, ("192.0.2.11", 50001)),
        "boat_002", _fresh_test_locator(), object(), object(),
        now=purge_time)
    assert session_b.assembly_namespace == reused_namespace

    new_fragment_text = "!AIVDM,2,2,9,A,fresh-new,0*00"
    new_frame = IngressFrame(
        kind="sec",
        source_id="udpsec:boat_002",
        alias_for_s="boat_002",
        remote_ip="192.0.2.11",
        assembler_key=reused_assembler_key,  # same key: namespace reused
        payload=new_fragment_text.encode("utf-8"),
        admitted_at=purge_time,
    )

    # Both frames finally reach one real processor+assembler. The OLD
    # frame is delayed well past MAX_INGRESS_FRAME_AGE_SECONDS by the time
    # it is actually processed here; the NEW frame is processed
    # essentially immediately.
    processing_time = purge_time + MAX_INGRESS_FRAME_AGE_SECONDS + 5.0
    real_assembler = AIVDMAssembler(timeout=1.0)
    processor = PythonDataPlaneProcessor(
        assembler=real_assembler,
        monotonic_clock=lambda: processing_time,
    )
    snapshot = ProcessingSnapshot(
        routing_generation=0,
        deduplication_mode=DeduplicationMode.GLOBAL,
        target_ids=(),
    )

    old_outputs = processor.process(old_frame, snapshot).outputs
    assert old_outputs == ()
    assert processor.stale_frames_dropped == 1

    new_outputs = processor.process(new_frame, snapshot).outputs
    # B's own fragment 2/2 has no matching fragment 1/2 in the assembler
    # (A's was correctly refused before it could seed a group) -- the
    # assembler sees an orphaned continuation fragment, never a
    # cross-station-completed message.
    assert new_outputs == ()
    assert real_assembler.stats().completed == 0


def test_r6_inflight_frame_lease_blocks_namespace_reuse_during_a_pause(
    monkeypatch,
):
    """R6/Blocker B: the exact independently-reproduced race, closed.

    The age check alone only protects against a delay BEFORE it runs --
    it is evaluated once, at the top of `_process_impl`, and does not
    protect against a pause that happens AFTER it passes but BEFORE the
    frame actually reaches the assembler (real OS thread preemption or a
    GC pause, not merely the absence of `await`). This test simulates
    exactly that: the age check passes (age=1s, well under the 20s
    default), then execution is paused -- modeled by wrapping the real
    assembler's `feed_parsed_outcome` so the FIRST call it receives, for
    this frame, first attempts to force-reuse the frame's own
    `assembly_namespace` for an unrelated station before delegating to
    the real assembler. Because the frame carries an explicit
    `admission_lease` acquired at admission time (while its session was
    still live), that reuse attempt must fail -- not because of the age
    check (which already passed), and not because of luck avoiding a
    128-bit collision (forced deterministically here), but because the
    registry itself refuses to reissue a value with an outstanding live
    reference."""
    from assembler import AIVDMAssembler
    from core.data_plane import DeduplicationMode, ProcessingSnapshot
    from core.ingress_frame import IngressFrame
    from core.python_data_plane import PythonDataPlaneProcessor
    from core.session_identity_registry import (
        RETIREMENT_SECONDS,
        SessionIdentityExhaustedError,
    )

    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    registry = secure._SESSION_IDENTITY_REGISTRY

    session_a = state.install_session(
        _relation_key(secure, ("192.0.2.30", 50000)),
        "boat_001", _fresh_test_locator(), object(), object(), now=0.0)
    namespace = session_a.assembly_namespace
    assembler_key = f"udpsec-assembly:{namespace.hex()}"

    # Admission time: the session is provably live (it was just
    # installed), so this lease succeeds -- exactly like
    # `_secure_server_loop`'s own admission-time `.lease()` call.
    lease = registry.lease(namespace)
    old_frame = IngressFrame(
        kind="sec",
        source_id="udpsec:boat_001",
        alias_for_s="boat_001",
        remote_ip="192.0.2.30",
        assembler_key=assembler_key,
        payload=b"!AIVDM,2,1,9,A,delayed-old,0*00",
        admitted_at=0.0,
        admission_lease=lease,
    )

    # Station A's session closes well before processing actually
    # happens -- exactly the audit's own timeline (session closes at
    # t=0.5; processing does not resume until t=31).
    assert state.close_session(session_a, now=0.5)
    # The value is still LIVE: old_frame's own independent lease keeps it
    # so, even though the owning session is completely gone and its own
    # reference has already been released.
    assert registry.is_live(namespace)
    assert not registry.is_retiring(namespace)

    real_assembler = AIVDMAssembler(timeout=1.0)
    reuse_attempted = []
    original_feed = real_assembler.feed_parsed_outcome

    def feed_with_simulated_pause(parsed):
        # Models the audit's step 3-4: execution has already passed the
        # age check (evaluated once, well before this point) and is only
        # NOW -- mid-call -- actually about to touch the assembler. This
        # is the precise instant an unrelated reuse attempt would land if
        # nothing held the namespace live in the meantime. Forced onto
        # the exact same bytes to make the scenario deterministic -- a
        # controlled allocator draw, not a claim about breaking real
        # randomness.
        reuse_attempted.append(True)
        monkeypatch.setattr(secure.os, "urandom", lambda _length: namespace)
        with pytest.raises(SessionIdentityExhaustedError):
            state.install_session(
                _relation_key(secure, ("192.0.2.31", 50001)),
                "boat_002", _fresh_test_locator(), object(), object(),
                now=31.0,
            )
        return original_feed(parsed)

    monkeypatch.setattr(
        real_assembler, "feed_parsed_outcome", feed_with_simulated_pause
    )

    # The processor's own single clock sample (for the age check) reads
    # 1.0 -- well within the age budget -- then, on the SECOND read (this
    # frame's lease release in `process()`'s own `finally`), reads 31.0:
    # a genuine 30 seconds passed mid-call, exactly like the reported
    # race, even though the processor itself only ever "looked" at its
    # clock at the very start and the very end.
    clock_readings = iter([1.0, 31.0])
    processor = PythonDataPlaneProcessor(
        assembler=real_assembler,
        monotonic_clock=lambda: next(clock_readings, 31.0),
    )
    snapshot = ProcessingSnapshot(
        routing_generation=0,
        deduplication_mode=DeduplicationMode.GLOBAL,
        target_ids=(),
    )

    outputs = processor.process(old_frame, snapshot).outputs

    assert reuse_attempted == [True]
    assert outputs == ()  # a lone fragment 1/2 never completes by itself
    assert processor.stale_frames_dropped == 0  # not refused by the age check
    assert real_assembler.stats().current_groups == 1

    # The lease is released only now that processing has actually
    # finished -- not before. The namespace still cannot be reused
    # immediately: it is genuinely retiring, exactly like ordinary
    # session removal, with the retirement clock now measured from this
    # later release rather than from the session's own earlier removal.
    assert not registry.is_live(namespace)
    assert registry.is_retiring(namespace)

    # Only after the full retirement window elapses (now correctly
    # measured from the LATER of the two releases) does legitimate reuse
    # become possible again.
    purge_time = 31.0 + RETIREMENT_SECONDS + 1.0
    state.cleanup_expired_sessions(now=purge_time)
    assert not registry.is_retiring(namespace)
    # Only the FIRST draw (assembly_namespace) needs to be forced onto the
    # reused value; the second draw (session_handle) must land on a
    # genuinely free value, not collide with the namespace it was just
    # handed (matching the existing namespace-reuse test's own pattern).
    draws = iter([namespace])
    fresh_handle = b"\xaa" * len(namespace)
    monkeypatch.setattr(
        secure.os, "urandom", lambda _length: next(draws, fresh_handle)
    )
    session_b = state.install_session(
        _relation_key(secure, ("192.0.2.31", 50001)),
        "boat_002", _fresh_test_locator(), object(), object(),
        now=purge_time)
    assert session_b.assembly_namespace == namespace


def test_r6_lease_release_covers_stale_drop_completion_and_processor_exception(
    monkeypatch,
):
    """R6/Blocker B: `process()`'s single `finally` releases a frame's
    lease exactly once regardless of how `_process_impl` exits -- an
    early stale-age drop, ordinary successful completion, or a raised
    exception -- so no path can leak a live registry reference."""
    from core.data_plane import DeduplicationMode, ProcessingSnapshot
    from core.ingress_frame import IngressFrame
    from core.python_data_plane import PythonDataPlaneProcessor

    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    registry = secure._SESSION_IDENTITY_REGISTRY
    snapshot = ProcessingSnapshot(
        routing_generation=0,
        deduplication_mode=DeduplicationMode.GLOBAL,
        target_ids=(),
    )

    def make_leased_frame(namespace, payload, admitted_at, *, remote_ip):
        lease = registry.lease(namespace)
        return IngressFrame(
            kind="sec",
            source_id="udpsec:boat_001",
            alias_for_s="boat_001",
            remote_ip=remote_ip,
            assembler_key=f"udpsec-assembly:{namespace.hex()}",
            payload=payload,
            admitted_at=admitted_at,
            admission_lease=lease,
        )

    # Each scenario gets its OWN fresh session/namespace so the
    # assertions below can check the registry's exact live/retiring
    # state without one scenario's release interfering with another's.

    # 1) Stale-age drop: the age check itself refuses the frame before
    # ever touching the assembler.
    session_1 = state.install_session(
        _relation_key(secure, ("192.0.2.32", 50000)),
        "boat_001", _fresh_test_locator(), object(), object(), now=0.0)
    stale_frame = make_leased_frame(
        session_1.assembly_namespace,
        b"!AIVDM,1,1,,A,stale,0*00",
        admitted_at=0.0,
        remote_ip="192.0.2.32",
    )
    # Before processing: the session's own reference plus this frame's
    # lease means releasing just the session must not be enough to
    # retire the namespace.
    assert state.close_session(session_1, now=0.1)
    assert registry.is_live(session_1.assembly_namespace)
    processor = PythonDataPlaneProcessor(monotonic_clock=lambda: 1000.0)
    processor.process(stale_frame, snapshot)
    assert processor.stale_frames_dropped == 1
    # The lease's own release (in process()'s finally) was the LAST live
    # reference: the namespace is now retiring, not still live.
    assert not registry.is_live(session_1.assembly_namespace)
    assert registry.is_retiring(session_1.assembly_namespace)

    # 2) Ordinary successful completion.
    session_2 = state.install_session(
        _relation_key(secure, ("192.0.2.33", 50001)),
        "boat_002", _fresh_test_locator(), object(), object(), now=0.0)
    complete_frame = make_leased_frame(
        session_2.assembly_namespace,
        b"!AIVDM,1,1,,A,ok,0*00",
        admitted_at=0.0,
        remote_ip="192.0.2.33",
    )
    assert state.close_session(session_2, now=0.1)
    assert registry.is_live(session_2.assembly_namespace)
    processor2 = PythonDataPlaneProcessor(monotonic_clock=lambda: 0.5)
    processor2.process(complete_frame, snapshot)
    assert not registry.is_live(session_2.assembly_namespace)
    assert registry.is_retiring(session_2.assembly_namespace)

    # 3) A raised exception from _process_impl must still release the
    # lease via `process()`'s `finally`.
    session_3 = state.install_session(
        _relation_key(secure, ("192.0.2.34", 50002)),
        "boat_003", _fresh_test_locator(), object(), object(), now=0.0)
    boom_frame = make_leased_frame(
        session_3.assembly_namespace,
        b"!AIVDM,1,1,,A,boom,0*00",
        admitted_at=0.0,
        remote_ip="192.0.2.34",
    )
    assert state.close_session(session_3, now=0.1)
    assert registry.is_live(session_3.assembly_namespace)

    class _RaisingAssembler:
        timeout = None  # _validate_retention_compatibility skips: no numeric timeout

        def feed_parsed_outcome(self, _parsed):
            raise RuntimeError("boom")

        def reset(self):
            return ()

    processor3 = PythonDataPlaneProcessor(
        assembler=_RaisingAssembler(), monotonic_clock=lambda: 0.5
    )
    with pytest.raises(RuntimeError, match="boom"):
        processor3.process(boom_frame, snapshot)
    assert not registry.is_live(session_3.assembly_namespace)
    assert registry.is_retiring(session_3.assembly_namespace)


class _RaisingQueue:
    """R6/F4 test double: a queue whose `put()` always fails, standing in
    for a real admission failure or a task cancelled while awaiting queue
    capacity -- `_secure_server_loop` must release the just-acquired
    namespace lease in that case, since the frame never actually entered
    the pipeline and nothing else will ever release it.

    Records each offered item's `admission_lease` for the caller to
    assert on AFTER the loop returns: an exception raised from inside
    `put()` itself is caught by `_secure_server_loop`'s own broad
    `except Exception` handler and merely logged, so an assertion made
    HERE would be silently swallowed rather than failing the test."""

    def __init__(self):
        self.offered_leases = []

    async def put(self, item):
        self.offered_leases.append(item.admission_lease)
        raise RuntimeError("queue admission failed")


def test_r6_secure_server_loop_releases_lease_when_queue_admission_fails(
    monkeypatch,
):
    secure, client_identity_private_key = load_secure_module_with_fake_keys(
        monkeypatch, with_client_private_key=True
    )
    addr = ("127.0.0.1", 50321)
    station_id = "boat_001"
    client_to_server_key = b"\x01" * 32
    server_to_client_key = b"\x02" * 32
    state = secure.SecureState()
    session, _, _ = _install_test_session(
        secure, state, addr, client_to_server_key, server_to_client_key,
    )
    registry = secure._SESSION_IDENTITY_REGISTRY
    namespace = session.assembly_namespace
    assert registry._live_refcounts[namespace] == 1  # only the session's own

    packet = _encrypted_data_packet(
        secure, client_to_server_key, b"\x00" * 12,
        session._session_key.session_locator,
        source_id=station_id,
    )
    fake_socket = _FakeSecureSocket()
    fake_loop = _FakeSecureLoop([(packet, addr)])
    monkeypatch.setattr(secure, "asyncio", _FakeAsyncioModule(fake_loop))
    monotonic_clock = _FakeClock(1000.0)
    owned_sessions = dict(state._sessions)
    owned_pending_sessions = dict(state._pending_sessions)

    raising_queue = _RaisingQueue()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            secure._secure_server_loop(
                fake_socket,
                raising_queue,
                "127.0.0.1",
                9999,
                endpoint_token=_test_endpoint_token(secure),
                state=state,
                wall_clock=_FakeClock(1010.0),
                monotonic_clock=monotonic_clock,
                server_private_key=_PREPARED_SERVER_PRIVATE_KEY,
                owned_sessions=owned_sessions,
                owned_pending_sessions=owned_pending_sessions,
            )
        )

    # The offered frame actually carried a lease -- proves the admission-
    # time `.lease()` wiring ran at all, not merely that nothing crashed.
    assert len(raising_queue.offered_leases) == 1
    assert raising_queue.offered_leases[0] is not None
    # The frame never entered the pipeline (put() always raised), so the
    # lease's release must have happened right there in the loop -- back
    # to exactly the session's own single reference, not leaked at 2 and
    # not over-released to zero.
    assert registry._live_refcounts[namespace] == 1
    assert registry.is_live(namespace)
    assert not registry.is_retiring(namespace)


def test_r6_secure_server_loop_drops_data_message_when_session_dies_before_lease(
    monkeypatch,
):
    """The other admission-time release path: `lease()` itself raises
    `ValueError` because the session's own reference was released
    concurrently between `touch_session` and the lease attempt (only
    reachable with a second real listener sharing this exact
    `SecureState` -- modeled here directly by closing the session from
    under the loop via a `SecureState` subclass whose `touch_session`
    also closes it, so the real `_secure_server_loop` code path -- not a
    hand-rolled substitute -- exercises the `except ValueError` branch).
    The data message must be dropped, not raise out of the handler, and
    nothing is left half-admitted."""
    secure, client_identity_private_key = load_secure_module_with_fake_keys(
        monkeypatch, with_client_private_key=True
    )
    addr = ("127.0.0.1", 50322)
    station_id = "boat_001"
    client_to_server_key = b"\x03" * 32
    server_to_client_key = b"\x04" * 32
    state = secure.SecureState()
    session, _, _ = _install_test_session(
        secure, state, addr, client_to_server_key, server_to_client_key,
    )
    registry = secure._SESSION_IDENTITY_REGISTRY
    namespace = session.assembly_namespace

    original_touch_session = state.touch_session

    def touch_then_close(touched_session, now):
        result = original_touch_session(touched_session, now)
        # Simulate a concurrent close landing exactly between
        # touch_session (which _secure_server_loop already called) and
        # the lease attempt that follows it, in the same synchronous
        # block -- releasing the session's own reference right out from
        # under the frame construction that is about to run.
        state.close_session(touched_session, now)
        return result

    monkeypatch.setattr(state, "touch_session", touch_then_close)

    packet = _encrypted_data_packet(
        secure, client_to_server_key, b"\x00" * 12,
        session._session_key.session_locator,
        source_id=station_id,
    )
    fake_socket = _FakeSecureSocket()
    fake_loop = _FakeSecureLoop([(packet, addr)])
    monkeypatch.setattr(secure, "asyncio", _FakeAsyncioModule(fake_loop))
    fake_queue = _FakeQueue()
    owned_sessions = dict(state._sessions)
    owned_pending_sessions = dict(state._pending_sessions)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            secure._secure_server_loop(
                fake_socket,
                fake_queue,
                "127.0.0.1",
                9999,
                endpoint_token=_test_endpoint_token(secure),
                state=state,
                wall_clock=_FakeClock(1010.0),
                monotonic_clock=_FakeClock(1000.0),
                server_private_key=_PREPARED_SERVER_PRIVATE_KEY,
                owned_sessions=owned_sessions,
                owned_pending_sessions=owned_pending_sessions,
            )
        )

    assert fake_queue.items == []  # dropped, never reached the pipeline
    assert not registry.is_live(namespace)
    assert registry.is_retiring(namespace)


def test_independent_owners_share_assembler_without_cross_station_combination(
    monkeypatch,
):
    """Full encrypted-ingress reproduction of the Codex-reported defect:
    two independent SecureState owners (distinct endpoint tokens, distinct
    addresses, distinct locators/keys, distinct authenticated stations)
    each complete a real signed handshake and send a real encrypted DATA
    fragment through the production `_secure_server_loop` and frame
    parser. Both owners' fragments declare the SAME nominal AIVDM group
    (sequential id "9", channel "A", 2 total) -- exactly the shape that
    would silently cross-assemble if their assembler_key values collided,
    as they could before assembly_namespace became a process-wide
    identifier. Both are fed into one shared, real AIVDMAssembler
    instance, matching how production feeds every listener's frames
    through one downstream assembler."""
    secure, client_key_a, extra_keys = load_secure_module_with_fake_keys(
        monkeypatch,
        with_client_private_key=True,
        extra_stations=["boat_beta"],
    )
    client_key_b = extra_keys["boat_beta"]

    owner_a = secure.SecureState()
    owner_b = secure.SecureState()
    endpoint_a = secure._new_endpoint_token()
    endpoint_b = secure._new_endpoint_token()
    addr_a = ("192.0.2.160", 50050)
    addr_b = ("192.0.2.161", 50051)
    wall_clock = _FakeClock(1000.0)
    monotonic_clock = _FakeClock(0.0)

    def establish(state, endpoint_token, addr, client_private_key, station_id):
        hello = _signed_handshake_packet(
            secure, client_private_key, station_id, 1000
        )
        _run_secure_server_with_packets(
            monkeypatch,
            secure,
            [(hello, addr)],
            state=state,
            endpoint_token=endpoint_token,
            wall_clock=wall_clock,
            monotonic_clock=monotonic_clock,
        )
        pending = state._pending_sessions[
            _relation_key(secure, addr, endpoint_token)
        ]
        confirmation = _confirmation_packet_for(secure, pending, b"\x30" * 12)
        _run_secure_server_with_packets(
            monkeypatch,
            secure,
            [(confirmation, addr)],
            state=state,
            endpoint_token=endpoint_token,
            wall_clock=wall_clock,
            monotonic_clock=monotonic_clock,
        )
        return _active_session_at(secure, state, addr, endpoint_token)

    session_a = establish(owner_a, endpoint_a, addr_a, client_key_a, "boat_001")
    session_b = establish(owner_b, endpoint_b, addr_b, client_key_b, "boat_beta")

    assert session_a.session_handle != session_b.session_handle
    assert session_a.assembly_namespace != session_b.assembly_namespace

    def send_fragment(state, endpoint_token, addr, session, nonce, text, source_id):
        packet = _nmea_data_packet_with_aesgcm(
            secure,
            session.current_epoch.client_to_server_aesgcm,
            nonce,
            text,
            session._session_key.session_locator,
            source_id=source_id,
        )
        queue, _ = _run_secure_server_with_packets(
            monkeypatch,
            secure,
            [(packet, addr)],
            state=state,
            endpoint_token=endpoint_token,
            wall_clock=wall_clock,
            monotonic_clock=monotonic_clock,
        )
        return queue.items[0].assembler_key

    # Both owners declare the SAME nominal AIVDM group (2 total, channel
    # A, sequential id 9) -- the exact shape that a colliding
    # assembly_namespace would silently merge.
    a_fragment_1 = "!AIVDM,2,1,9,A,owner-a-one,0*00"
    a_fragment_2 = "!AIVDM,2,2,9,A,owner-a-two,0*00"
    b_fragment_1 = "!AIVDM,2,1,9,A,owner-b-one,0*00"
    b_fragment_2 = "!AIVDM,2,2,9,A,owner-b-two,0*00"

    key_a_1 = send_fragment(
        owner_a, endpoint_a, addr_a, session_a, b"\x31" * 12, a_fragment_1,
        "boat_001",
    )
    key_b_2 = send_fragment(
        owner_b, endpoint_b, addr_b, session_b, b"\x32" * 12, b_fragment_2,
        "boat_beta",
    )
    assert key_a_1 != key_b_2

    from assembler import AIVDMAssembler, AssemblyStatus

    shared_assembler = AIVDMAssembler(timeout=30.0)

    # Owner A's ordinal-1 fragment starts A's own group.
    outcome_a1 = shared_assembler.feed_outcome(key_a_1, a_fragment_1)
    assert outcome_a1.status is AssemblyStatus.PENDING

    # Owner B's ordinal-2 fragment, despite declaring the identical
    # nominal group (2/A/9), must NOT complete owner A's group: it must
    # start its own independent, still-pending group under B's own key.
    outcome_b2 = shared_assembler.feed_outcome(key_b_2, b_fragment_2)
    assert outcome_b2.status is AssemblyStatus.PENDING

    # Each owner's own remaining fragment completes its OWN generation,
    # with only that owner's own sentences.
    key_a_2 = send_fragment(
        owner_a, endpoint_a, addr_a, session_a, b"\x33" * 12, a_fragment_2,
        "boat_001",
    )
    key_b_1 = send_fragment(
        owner_b, endpoint_b, addr_b, session_b, b"\x34" * 12, b_fragment_1,
        "boat_beta",
    )
    assert key_a_2 == key_a_1
    assert key_b_1 == key_b_2

    outcome_a2 = shared_assembler.feed_outcome(key_a_2, a_fragment_2)
    assert outcome_a2.status is AssemblyStatus.COMPLETE
    assert outcome_a2.sentences == (a_fragment_1, a_fragment_2)

    outcome_b1 = shared_assembler.feed_outcome(key_b_1, b_fragment_1)
    assert outcome_b1.status is AssemblyStatus.COMPLETE
    assert outcome_b1.sentences == (b_fragment_1, b_fragment_2)


def test_same_relation_different_station_replacement_gets_fresh_namespace(
    monkeypatch,
):
    """Security-relevant: an authenticated replacement at the SAME relation
    but a DIFFERENT authenticated station must never inherit the old
    station's assembly namespace. Relation equality alone is insufficient
    for continuity -- authenticated identity continuity is mandatory."""
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    relation_key = _relation_key(secure, ("192.0.2.80", 50070))

    boat_001 = state.install_session(
        relation_key, "boat_001", _fresh_test_locator(), object(), object(), 0.0
    )

    pending_boat_002 = state.install_pending_session(
        relation_key, "boat_002", _fresh_test_locator(), object(), object(), 0.0
    )
    boat_002 = state.promote_pending_session(pending_boat_002, now=1.0)

    assert boat_002 is not None
    assert boat_002 is not boat_001
    assert boat_002.station_id == "boat_002"
    assert boat_002.session_handle != boat_001.session_handle
    assert boat_002.assembly_namespace != boat_001.assembly_namespace


def test_expired_same_relation_reestablishment_gets_fresh_namespace(
    monkeypatch,
):
    """No continuity survives real session death: once the old session at a
    relation has genuinely expired (removed via idle TTL, not merely
    superseded by a live replacement), a later re-establishment for the SAME
    station at the SAME relation must not resurrect the old assembly
    namespace. There is no historical namespace cache."""
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(session_ttl=10.0)
    relation_key = _relation_key(secure, ("192.0.2.81", 50071))

    original = state.install_session(
        relation_key, "boat_001", _fresh_test_locator(), object(), object(), 0.0
    )

    assert state.cleanup_expired_sessions(10.0) == [original._session_key]
    assert original._session_key not in state._sessions

    reestablished = state.install_session(
        relation_key, "boat_001", _fresh_test_locator(), object(), object(), 10.0
    )

    assert reestablished.session_handle != original.session_handle
    assert reestablished.assembly_namespace != original.assembly_namespace


def test_install_session_replacement_matches_promotion_semantics(
    monkeypatch,
):
    """`install_session()` (a supported direct/test construction path) must
    not diverge from real `promote_pending_session()` semantics: a same-
    relation, same-station replacement keeps `assembly_namespace` (with a
    fresh `session_handle`); a same-relation, different-station replacement
    gets both fresh."""
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    relation_key = _relation_key(secure, ("192.0.2.82", 50072))

    first = state.install_session(
        relation_key, "boat_001", _fresh_test_locator(), object(), object(), 0.0
    )
    same_station_replacement = state.install_session(
        relation_key, "boat_001", _fresh_test_locator(), object(), object(), 1.0
    )

    assert same_station_replacement is not first
    assert same_station_replacement.session_handle != first.session_handle
    assert (
        same_station_replacement.assembly_namespace
        == first.assembly_namespace
    )

    different_station_replacement = state.install_session(
        relation_key, "boat_002", _fresh_test_locator(), object(), object(), 2.0
    )

    assert different_station_replacement is not same_station_replacement
    assert (
        different_station_replacement.session_handle
        != same_station_replacement.session_handle
    )
    assert (
        different_station_replacement.assembly_namespace
        != same_station_replacement.assembly_namespace
    )


# Deterministic exercise of the actual production locator generator
# (Codex audit Finding E): monkeypatches only the randomness source, never
# the generation/retry logic itself, and reads production's own ownership
# indexes to confirm collision retry, bounded exhaustion, and cross-
# endpoint independence.


def test_generate_session_locator_retries_past_an_occupied_active_locator(
    monkeypatch,
):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    endpoint_token = secure._new_endpoint_token()
    occupied = _fresh_test_locator()
    free = _fresh_test_locator()
    assert occupied != free
    state.install_session(
        _relation_key(secure, ("192.0.2.110", 50060), endpoint_token),
        "boat_occupant",
        occupied,
        object(),
        object(),
        now=0.0,
    )

    draws = iter((occupied, free))
    call_count = 0

    def fake_urandom(_length):
        nonlocal call_count
        call_count += 1
        return next(draws)

    monkeypatch.setattr(secure.os, "urandom", fake_urandom)

    result = state.generate_session_locator(endpoint_token)

    assert result == free
    assert call_count == 2


def test_generate_session_locator_retries_past_an_occupied_pending_locator(
    monkeypatch,
):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    endpoint_token = secure._new_endpoint_token()
    occupied = _fresh_test_locator()
    free = _fresh_test_locator()
    assert occupied != free
    state.install_pending_session(
        _relation_key(secure, ("192.0.2.111", 50061), endpoint_token),
        "boat_pending",
        occupied,
        object(),
        object(),
        now=0.0,
    )

    draws = iter((occupied, free))
    monkeypatch.setattr(secure.os, "urandom", lambda _length: next(draws))

    result = state.generate_session_locator(endpoint_token)

    assert result == free


def test_generate_session_locator_bounded_retry_exhausts_at_exact_limit(
    monkeypatch,
):
    """Exactly `SESSION_LOCATOR_GENERATION_ATTEMPTS` draws, no more, no
    unbounded retry and no weaker fallback: exhaustion fails closed with
    the intended dedicated error."""
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    endpoint_token = secure._new_endpoint_token()
    always_occupied = _fresh_test_locator()
    state.install_session(
        _relation_key(secure, ("192.0.2.112", 50062), endpoint_token),
        "boat_occupant",
        always_occupied,
        object(),
        object(),
        now=0.0,
    )

    call_count = 0

    def fake_urandom(_length):
        nonlocal call_count
        call_count += 1
        return always_occupied

    monkeypatch.setattr(secure.os, "urandom", fake_urandom)

    with pytest.raises(secure.SessionLocatorExhaustedError):
        state.generate_session_locator(endpoint_token)

    assert call_count == secure.SESSION_LOCATOR_GENERATION_ATTEMPTS == 8


def test_generate_session_locator_same_raw_bytes_free_on_other_endpoint(
    monkeypatch,
):
    """The same raw locator bytes occupied on one endpoint_token are an
    entirely independent, immediately-free identity on another -- proving
    generation scopes collision checking per endpoint_token rather than
    globally."""
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    endpoint_a = secure._new_endpoint_token()
    endpoint_b = secure._new_endpoint_token()
    shared_bytes = _fresh_test_locator()
    state.install_session(
        _relation_key(secure, ("192.0.2.113", 50063), endpoint_a),
        "boat_a",
        shared_bytes,
        object(),
        object(),
        now=0.0,
    )

    call_count = 0

    def fake_urandom(_length):
        nonlocal call_count
        call_count += 1
        return shared_bytes

    monkeypatch.setattr(secure.os, "urandom", fake_urandom)

    result = state.generate_session_locator(endpoint_b)

    assert result == shared_bytes
    assert call_count == 1


def test_generate_session_locator_does_not_mutate_ownership_state(monkeypatch):
    """Generation is purely observational: it only reads existing
    ownership to pick a free value and never itself reserves anything --
    reservation happens only when the caller actually installs a session
    or pending candidate with the returned value."""
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    endpoint_token = secure._new_endpoint_token()
    state.install_session(
        _relation_key(secure, ("192.0.2.114", 50064), endpoint_token),
        "boat_a",
        _fresh_test_locator(),
        object(),
        object(),
        now=0.0,
    )
    sessions_before = dict(state._sessions)
    pending_before = dict(state._pending_sessions)
    owners_before = dict(state._pending_locator_owners)
    relation_index_before = dict(state._relation_index)
    stats_before = state.stats()

    result = state.generate_session_locator(endpoint_token)

    assert isinstance(result, bytes) and len(result) == secure.SESSION_LOCATOR_BYTES
    assert state._sessions == sessions_before
    assert state._pending_sessions == pending_before
    assert state._pending_locator_owners == owners_before
    assert state._relation_index == relation_index_before
    assert state.stats() == stats_before


def test_generate_session_locator_value_is_independent_of_station_or_address(
    monkeypatch,
):
    """The locator generator's output must depend only on its randomness
    source, never on station identity, network address, or ECDHE
    material -- it is a process-local lookup hint, not a credential
    derived from (and therefore entangled with) authenticated context.
    Two completely different stations/addresses/endpoint_tokens forced to
    draw the identical underlying random bytes must produce the identical
    resulting locator, proving there is no hidden derivation from the
    surrounding session context."""
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    endpoint_alpha = secure._new_endpoint_token()
    endpoint_beta = secure._new_endpoint_token()

    state.install_session(
        _relation_key(secure, ("192.0.2.120", 50070), endpoint_alpha),
        "boat_alpha",
        _fresh_test_locator(),
        object(),
        object(),
        now=0.0,
    )
    state.install_session(
        _relation_key(secure, ("2001:db8::99", 50071), endpoint_beta),
        "boat_beta_completely_different_station",
        _fresh_test_locator(),
        object(),
        object(),
        now=0.0,
    )

    forced_draw = _fresh_test_locator()
    monkeypatch.setattr(secure.os, "urandom", lambda _length: forced_draw)

    locator_for_alpha = state.generate_session_locator(endpoint_alpha)
    locator_for_beta = state.generate_session_locator(endpoint_beta)

    assert locator_for_alpha == locator_for_beta == forced_draw


# Locator uniqueness as a SecureState invariant, not merely a convention of
# generate_session_locator(): install_session()/install_pending_session()
# must fail closed if given a session_locator that already identifies a
# different live session on the same endpoint_token, rather than silently
# overwriting an existing _sessions/_pending_locator_owners entry.


def test_install_session_rejects_locator_claimed_by_other_active_session(
    monkeypatch,
):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    endpoint_token = secure._new_endpoint_token()
    shared_locator = _fresh_test_locator()
    other_addr = ("192.0.2.90", 50000)
    victim_addr = ("192.0.2.91", 50001)

    other = state.install_session(
        _relation_key(secure, other_addr, endpoint_token),
        "boat_other",
        shared_locator,
        object(),
        object(),
        now=0.0,
    )

    with pytest.raises(secure.SessionLocatorCollisionError):
        state.install_session(
            _relation_key(secure, victim_addr, endpoint_token),
            "boat_victim",
            shared_locator,
            object(),
            object(),
            now=1.0,
        )

    # The original session must survive completely intact: no orphaning,
    # no stale bookkeeping, no partial mutation from the failed attempt.
    assert _active_session_for_relation_key(
        state, _relation_key(secure, other_addr, endpoint_token)
    ) is other
    assert (
        _active_session_for_relation_key(
            state, _relation_key(secure, victim_addr, endpoint_token)
        )
        is None
    )
    assert state._sessions == {other._session_key: other}
    assert state.stats().sessions_created == 1
    assert state.stats().current_sessions == 1


def test_install_session_rejects_locator_claimed_by_live_pending(
    monkeypatch,
):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    endpoint_token = secure._new_endpoint_token()
    shared_locator = _fresh_test_locator()
    pending_addr = ("192.0.2.92", 50002)
    victim_addr = ("192.0.2.93", 50003)

    pending = state.install_pending_session(
        _relation_key(secure, pending_addr, endpoint_token),
        "boat_pending",
        shared_locator,
        object(),
        object(),
        now=0.0,
    )

    with pytest.raises(secure.SessionLocatorCollisionError):
        state.install_session(
            _relation_key(secure, victim_addr, endpoint_token),
            "boat_victim",
            shared_locator,
            object(),
            object(),
            now=1.0,
        )

    assert (
        state._pending_sessions[
            _relation_key(secure, pending_addr, endpoint_token)
        ]
        is pending
    )
    assert (
        _active_session_for_relation_key(
            state, _relation_key(secure, victim_addr, endpoint_token)
        )
        is None
    )
    assert state._sessions == {}
    assert state.stats().current_pending_sessions == 1
    assert state.stats().current_sessions == 0


def test_install_pending_session_rejects_locator_claimed_by_other_pending(
    monkeypatch,
):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    endpoint_token = secure._new_endpoint_token()
    shared_locator = _fresh_test_locator()
    first_addr = ("192.0.2.94", 50004)
    second_addr = ("192.0.2.95", 50005)

    first_pending = state.install_pending_session(
        _relation_key(secure, first_addr, endpoint_token),
        "boat_first",
        shared_locator,
        object(),
        object(),
        now=0.0,
    )

    with pytest.raises(secure.SessionLocatorCollisionError):
        state.install_pending_session(
            _relation_key(secure, second_addr, endpoint_token),
            "boat_second",
            shared_locator,
            object(),
            object(),
            now=1.0,
        )

    # Exactly one live pending object must exist, with its ownership
    # record intact -- the failed attempt must not leave two live pending
    # sessions sharing one _pending_locator_owners entry.
    assert (
        state._pending_sessions[
            _relation_key(secure, first_addr, endpoint_token)
        ]
        is first_pending
    )
    assert (
        _relation_key(secure, second_addr, endpoint_token)
        not in state._pending_sessions
    )
    assert len(state._pending_sessions) == 1
    assert state.stats().current_pending_sessions == 1


def test_install_pending_session_rejects_locator_claimed_by_active(
    monkeypatch,
):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    endpoint_token = secure._new_endpoint_token()
    shared_locator = _fresh_test_locator()
    active_addr = ("192.0.2.96", 50006)
    victim_addr = ("192.0.2.97", 50007)

    active = state.install_session(
        _relation_key(secure, active_addr, endpoint_token),
        "boat_active",
        shared_locator,
        object(),
        object(),
        now=0.0,
    )

    with pytest.raises(secure.SessionLocatorCollisionError):
        state.install_pending_session(
            _relation_key(secure, victim_addr, endpoint_token),
            "boat_victim",
            shared_locator,
            object(),
            object(),
            now=1.0,
        )

    assert (
        _active_session_for_relation_key(
            state, _relation_key(secure, active_addr, endpoint_token)
        )
        is active
    )
    assert (
        _relation_key(secure, victim_addr, endpoint_token)
        not in state._pending_sessions
    )
    assert state.stats().current_pending_sessions == 0


# Mutation-order correction: a locator-collision preflight must run BEFORE
# any destructive replacement/eviction, never after. The tests above already
# cover a collision landing at a previously-empty relation; the tests below
# specifically cover the more dangerous case where the TARGET relation (or
# pending slot) already has its own live occupant that a naive "remove, then
# check" ordering would destroy before ever discovering the collision.


def test_install_session_collision_preserves_target_relations_own_session(
    monkeypatch,
):
    """relation A -> live session SA; relation B -> live session SB,
    locator LB. install_session(relation A, locator=LB) must raise while
    preserving BOTH SA and SB exactly -- SA must never be removed merely
    because the requested locator turned out to belong to someone else."""
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    endpoint_token = secure._new_endpoint_token()
    relation_a = _relation_key(secure, ("192.0.2.100", 50020), endpoint_token)
    relation_b = _relation_key(secure, ("192.0.2.101", 50021), endpoint_token)
    locator_b = _fresh_test_locator()

    session_a = state.install_session(
        relation_a, "boat_a", _fresh_test_locator(), object(), object(), 0.0
    )
    session_b = state.install_session(
        relation_b, "boat_b", locator_b, object(), object(), 1.0
    )
    relation_index_before = dict(state._relation_index)
    stats_before = state.stats()

    with pytest.raises(secure.SessionLocatorCollisionError):
        state.install_session(
            relation_a, "boat_victim", locator_b, object(), object(), 2.0
        )

    assert _active_session_for_relation_key(state, relation_a) is session_a
    assert _active_session_for_relation_key(state, relation_b) is session_b
    assert state._sessions == {
        session_a._session_key: session_a,
        session_b._session_key: session_b,
    }
    assert (
        session_a.current_epoch.seen_data_nonces
        is session_a.current_epoch.seen_data_nonces
    )
    assert (
        session_b.current_epoch.seen_data_nonces
        is session_b.current_epoch.seen_data_nonces
    )
    assert state._relation_index == relation_index_before
    stats_after = state.stats()
    assert stats_after.sessions_created == stats_before.sessions_created
    assert stats_after.sessions_replaced == stats_before.sessions_replaced
    assert (
        stats_after.sessions_capacity_evicted
        == stats_before.sessions_capacity_evicted
    )
    assert stats_after.current_sessions == stats_before.current_sessions


def test_install_pending_session_collision_preserves_target_relations_own_pending(
    monkeypatch,
):
    """relation A -> live pending PA; relation C (elsewhere) -> live
    pending PC, locator LC. install_pending_session(relation A, locator=LC)
    must raise while preserving BOTH PA and PC exactly, with no
    replacement/creation/capacity statistic change and an untouched
    `_pending_locator_owners` index."""
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    endpoint_token = secure._new_endpoint_token()
    relation_a = _relation_key(secure, ("192.0.2.102", 50022), endpoint_token)
    relation_c = _relation_key(secure, ("192.0.2.103", 50023), endpoint_token)
    locator_c = _fresh_test_locator()

    pending_a = state.install_pending_session(
        relation_a, "boat_a", _fresh_test_locator(), object(), object(), 0.0
    )
    pending_c = state.install_pending_session(
        relation_c, "boat_c", locator_c, object(), object(), 1.0
    )
    owners_before = dict(state._pending_locator_owners)
    stats_before = state.stats()

    with pytest.raises(secure.SessionLocatorCollisionError):
        state.install_pending_session(
            relation_a, "boat_victim", locator_c, object(), object(), 2.0
        )

    assert state._pending_sessions[relation_a] is pending_a
    assert state._pending_sessions[relation_c] is pending_c
    assert state._pending_locator_owners == owners_before
    stats_after = state.stats()
    assert (
        stats_after.pending_sessions_created
        == stats_before.pending_sessions_created
    )
    assert (
        stats_after.pending_sessions_replaced
        == stats_before.pending_sessions_replaced
    )
    assert (
        stats_after.pending_sessions_capacity_evicted
        == stats_before.pending_sessions_capacity_evicted
    )
    assert (
        stats_after.current_pending_sessions
        == stats_before.current_pending_sessions
    )


def test_install_pending_session_collision_at_capacity_does_not_evict(
    monkeypatch,
):
    """When the pending store is genuinely full, a locator collision must
    be rejected BEFORE the oldest live pending entry is evicted to make
    room -- a failed installation attempt must not shrink unrelated live
    state."""
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState(max_pending_sessions=2)
    endpoint_token = secure._new_endpoint_token()
    relation_oldest = _relation_key(
        secure, ("192.0.2.104", 50024), endpoint_token
    )
    relation_newest = _relation_key(
        secure, ("192.0.2.105", 50025), endpoint_token
    )
    relation_new_request = _relation_key(
        secure, ("192.0.2.106", 50026), endpoint_token
    )
    claimed_locator = _fresh_test_locator()

    oldest = state.install_pending_session(
        relation_oldest,
        "boat_oldest",
        _fresh_test_locator(),
        object(),
        object(),
        0.0,
    )
    newest = state.install_pending_session(
        relation_newest,
        "boat_newest",
        claimed_locator,
        object(),
        object(),
        1.0,
    )
    stats_before = state.stats()

    with pytest.raises(secure.SessionLocatorCollisionError):
        state.install_pending_session(
            relation_new_request,
            "boat_victim",
            claimed_locator,
            object(),
            object(),
            2.0,
        )

    assert state._pending_sessions[relation_oldest] is oldest
    assert state._pending_sessions[relation_newest] is newest
    assert relation_new_request not in state._pending_sessions
    assert len(state._pending_sessions) == 2
    stats_after = state.stats()
    assert (
        stats_after.pending_sessions_capacity_evicted
        == stats_before.pending_sessions_capacity_evicted
    )
    assert (
        stats_after.pending_sessions_created
        == stats_before.pending_sessions_created
    )
    assert stats_after.current_pending_sessions == 2


def test_install_pending_session_same_relation_reinstall_with_own_locator_fails_closed(
    monkeypatch,
):
    """Mirrors the active-session case: a same-relation pending
    reinstallation reusing the CURRENTLY LIVE pending candidate's own
    locator must fail closed rather than being granted merely because the
    old pending entry could be removed first to make the locator look
    free. Each ServerHello mints its own fresh locator in this V2 stage."""
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    endpoint_token = secure._new_endpoint_token()
    relation_key = _relation_key(
        secure, ("192.0.2.107", 50027), endpoint_token
    )
    reused_locator = _fresh_test_locator()

    original = state.install_pending_session(
        relation_key, "boat_001", reused_locator, object(), object(), 0.0
    )

    with pytest.raises(secure.SessionLocatorCollisionError):
        state.install_pending_session(
            relation_key,
            "boat_001",
            reused_locator,
            object(),
            object(),
            1.0,
        )

    assert state._pending_sessions[relation_key] is original
    assert state._pending_locator_owners[
        (endpoint_token, reused_locator)
    ] == relation_key
    assert state.stats().pending_sessions_replaced == 0
    assert state.stats().pending_sessions_created == 1
    assert state.stats().current_pending_sessions == 1


def test_promote_pending_session_collision_is_non_destructive(monkeypatch):
    """Controlled white-box inconsistent state: an active session already
    exists (through some other internally-inconsistent path) holding the
    exact locator a still-live pending candidate legitimately reserved.
    This exercises promote_pending_session()'s defense-in-depth branch,
    which must fail BEFORE removing the pending session, releasing its
    locator reservation, incrementing pending_sessions_promoted, touching
    any active session at the relation, or changing the relation index --
    it must not temporarily delete the pending's own reservation just to
    make a free-locator check pass."""
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    endpoint_token = secure._new_endpoint_token()
    pending_relation = _relation_key(
        secure, ("192.0.2.108", 50028), endpoint_token
    )
    other_relation = _relation_key(
        secure, ("192.0.2.109", 50029), endpoint_token
    )

    pending = state.install_pending_session(
        pending_relation,
        "boat_pending",
        _fresh_test_locator(),
        object(),
        object(),
        0.0,
    )
    conflicting_locator = pending.session_locator

    # Simulate an impossible/inconsistent state: an active session already
    # exists under the pending's own reserved locator. This cannot arise
    # through the public install_session()/install_pending_session() API
    # (both now preflight-check before mutation), so it is constructed
    # directly to exercise promotion's own defense-in-depth check.
    foreign_session = state.install_session(
        other_relation, "boat_other", _fresh_test_locator(), object(), object(), 0.5
    )
    foreign_session_key = secure._EndpointSessionKey(
        endpoint_token, conflicting_locator
    )
    state._sessions[foreign_session_key] = secure.LogicalSession(
        _session_key=foreign_session_key,
        station_id="boat_impossible",
        created_at=0.5,
        last_seen=0.5,
        session_handle=state._fresh_session_handle(),
        assembly_namespace=secure._SESSION_IDENTITY_REGISTRY.reserve(),
        current_epoch=pending.current_epoch,
        path_state=secure.PathState(active_path=other_relation.peer_address),
    )
    relation_index_before = dict(state._relation_index)
    owners_before = dict(state._pending_locator_owners)
    stats_before = state.stats()

    with pytest.raises(secure.SessionLocatorCollisionError):
        state.promote_pending_session(pending, 1.0)

    assert state._pending_sessions[pending_relation] is pending
    assert (
        state._pending_locator_owners[
            (endpoint_token, conflicting_locator)
        ]
        == pending_relation
    )
    assert state._pending_locator_owners == owners_before
    assert _active_session_for_relation_key(
        state, other_relation
    ) is foreign_session
    assert state._sessions[foreign_session_key].station_id == (
        "boat_impossible"
    )
    assert state._relation_index == relation_index_before
    stats_after = state.stats()
    assert (
        stats_after.pending_sessions_promoted
        == stats_before.pending_sessions_promoted
    )
    assert stats_after.sessions_replaced == stats_before.sessions_replaced
    assert stats_after.sessions_created == stats_before.sessions_created
    assert (
        stats_after.sessions_capacity_evicted
        == stats_before.sessions_capacity_evicted
    )


def test_promote_pending_session_with_malformed_relation_key_is_non_destructive(
    monkeypatch,
):
    """Codex audit Finding D follow-up: computing the fresh-or-reused
    assembly namespace during promotion canonicalizes the pending's
    relation_key, which can now raise `MalformedSockaddrError` for a
    malformed peer address (stricter validation than before). This
    canonicalization must happen BEFORE promotion pops the pending entry
    or releases its locator reservation -- a white-box injected pending
    candidate with a malformed relation address (bypassing the public
    install_pending_session() API, which does not itself validate address
    shape, exactly like real pending installation from a raw handshake
    tuple does not) proves a validation failure here leaves the pending
    session, its locator reservation, and every lifecycle statistic
    exactly as they were."""
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    endpoint_token = secure._new_endpoint_token()
    malformed_address = ("not-an-ip-address", 50000)
    relation_key = secure._EndpointPeerKey(endpoint_token, malformed_address)
    locator = _fresh_test_locator()

    pending = secure._PendingSecureSession(
        _relation_key=relation_key,
        _address=malformed_address,
        station_id="boat_pending",
        session_locator=locator,
        created_at=0.0,
        current_epoch=secure.CryptoEpoch(
            client_to_server_aesgcm=object(),
            server_to_client_aesgcm=object(),
            seen_data_nonces=secure._BoundedNonceSet(
                secure.DATA_NONCE_MAX_PER_SESSION
            ),
            created_at=0.0,
        ),
    )
    # Constructed directly rather than via install_pending_session(): a
    # real pending candidate reaches this exact same unvalidated shape
    # from a raw handshake tuple, since pending identity is intentionally
    # exact-tuple-bound and never itself canonicalized at install time.
    state._pending_sessions[relation_key] = pending
    state._pending_locator_owners[(endpoint_token, locator)] = relation_key
    state._pending_sessions_created += 1

    pending_before = dict(state._pending_sessions)
    owners_before = dict(state._pending_locator_owners)
    stats_before = state.stats()

    with pytest.raises(secure.MalformedSockaddrError):
        state.promote_pending_session(pending, 1.0)

    assert state._pending_sessions == pending_before
    assert state._pending_sessions[relation_key] is pending
    assert state._pending_locator_owners == owners_before
    assert state.stats() == stats_before


def test_remove_session_with_malformed_active_path_is_non_destructive(
    monkeypatch,
):
    """Companion to the promotion case above: `_remove_session` (used for
    every active-session removal reason -- expiry, close, capacity,
    replacement, nonce exhaustion) also canonicalizes `active_path`, which
    can raise for a malformed value. A white-box injected active session
    with a malformed `active_path` (bypassing install_session(), which
    does validate this shape as a side effect of its own relation-index
    write -- so this state cannot arise through the public API, but
    `_remove_session` must not assume that and must still validate before
    mutating) proves a validation failure leaves `_sessions` and every
    other store untouched rather than a session already popped with
    nothing else done."""
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    endpoint_token = secure._new_endpoint_token()
    malformed_address = ("not-an-ip-address", 50000)
    locator = _fresh_test_locator()
    session_key = secure._EndpointSessionKey(endpoint_token, locator)

    session = secure.LogicalSession(
        _session_key=session_key,
        station_id="boat_malformed",
        created_at=0.0,
        last_seen=0.0,
        session_handle=state._fresh_session_handle(),
        assembly_namespace=secure._SESSION_IDENTITY_REGISTRY.reserve(),
        current_epoch=secure.CryptoEpoch(
            client_to_server_aesgcm=object(),
            server_to_client_aesgcm=object(),
            seen_data_nonces=secure._BoundedNonceSet(
                secure.DATA_NONCE_MAX_PER_SESSION
            ),
            created_at=0.0,
        ),
        path_state=secure.PathState(active_path=malformed_address),
    )
    state._sessions[session_key] = session
    state._sessions_created += 1

    sessions_before = dict(state._sessions)
    relation_index_before = dict(state._relation_index)
    stats_before = state.stats()

    with pytest.raises(secure.MalformedSockaddrError):
        state._remove_session(session_key, "closed", now=0.0)

    assert state._sessions == sessions_before
    assert state._sessions[session_key] is session
    assert state._relation_index == relation_index_before
    assert state.stats() == stats_before


def test_same_raw_locator_on_different_endpoint_token_is_independent(
    monkeypatch,
):
    """The collision check is scoped to one endpoint_token: the same raw
    locator bytes minted on a different physical listener is a genuinely
    independent identity and must install without error."""
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    endpoint_a = secure._new_endpoint_token()
    endpoint_b = secure._new_endpoint_token()
    shared_locator = _fresh_test_locator()
    peer = ("192.0.2.98", 50008)

    session_a = state.install_session(
        _relation_key(secure, peer, endpoint_a),
        "boat_a",
        shared_locator,
        object(),
        object(),
        now=0.0,
    )
    session_b = state.install_session(
        _relation_key(secure, peer, endpoint_b),
        "boat_b",
        shared_locator,
        object(),
        object(),
        now=1.0,
    )

    assert session_a is not session_b
    assert session_a._session_key.session_locator == shared_locator
    assert session_b._session_key.session_locator == shared_locator
    assert session_a._session_key != session_b._session_key
    assert state._sessions == {
        session_a._session_key: session_a,
        session_b._session_key: session_b,
    }
    assert state.stats().current_sessions == 2


def test_install_session_same_relation_reinstall_with_own_locator_fails_closed(
    monkeypatch,
):
    """V2 requires a fresh server-minted locator for every whole-
    LogicalSession replacement. A direct/internal call that reinstalls a
    session at its OWN relation reusing the CURRENTLY LIVE session's own
    locator must not be granted merely because the old entry could be
    removed first to make the locator look free -- the preflight check
    runs against unmodified state, so it still sees the locator occupied
    by the very session it would otherwise replace, and the original
    session must survive completely intact."""
    secure = load_secure_module_with_fake_keys(monkeypatch)
    state = secure.SecureState()
    endpoint_token = secure._new_endpoint_token()
    addr = ("192.0.2.89", 50010)
    relation_key = _relation_key(secure, addr, endpoint_token)
    reused_locator = _fresh_test_locator()

    original = state.install_session(
        relation_key, "boat_001", reused_locator, object(), object(), 0.0
    )

    with pytest.raises(secure.SessionLocatorCollisionError):
        state.install_session(
            relation_key, "boat_001", reused_locator, object(), object(), 1.0
        )

    assert _active_session_for_relation_key(state, relation_key) is original
    assert state._sessions == {original._session_key: original}
    assert state.stats().sessions_replaced == 0
    assert state.stats().sessions_created == 1
    assert state.stats().current_sessions == 1


# Plain UDP's assembler_key remains tuple-derived and unchanged by this
# stage; that boundary is already covered by
# tests/test_aismixer_forward_loop.py::test_handle_socket_creates_ingress_frame_with_udp_source_id
# and its neighboring `handle_socket` tests.
