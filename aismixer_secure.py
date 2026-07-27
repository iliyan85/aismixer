import os
import asyncio
import base64
import binascii
import json
import socket
import time
import yaml
from collections import OrderedDict, deque
from dataclasses import dataclass
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from core.ingress_frame import frame_from_text_payload
from core.network_policy import NetworkPolicy
from core.source_identity import build_udpsec_source_id
from core.udpsec_crypto import (
    DOMAIN_CONTEXT,
    build_client_auth_digest,
    build_server_auth_digest,
    build_session_transcript_hash,
    derive_ephemeral_shared_secret,
    derive_session_key_material,
    generate_ephemeral_private_key,
    parse_ephemeral_public_key,
    serialize_ephemeral_public_key,
    sign_transcript_digest,
    verify_transcript_signature,
)
from core.udpsec_protocol import (
    CLIENT_HELLO_PREFIX,
    SESSION_CONFIRMATION_SEQUENCE,
    ServerHello,
    build_server_hello_packet,
    parse_client_hello_packet,
)


DATA_PREFIX = b"NMEA-D"
NOSESSION_PREFIX = b"NOSESSION"
DATA_AAD = b"NMEA"
SESSION_TTL_SECONDS = 300
SESSION_MAX = 100000
PENDING_SESSION_TTL_SECONDS = 30
PENDING_SESSION_MAX = SESSION_MAX
HANDSHAKE_REPLAY_TTL_SECONDS = 60
HANDSHAKE_REPLAY_MAX = 100000
DATA_NONCE_TTL_SECONDS = SESSION_TTL_SECONDS
DATA_NONCE_MAX_PER_SESSION = 100000

_HANDSHAKE_REPLAY_LABEL = b"HANDSHAKE-REPLAY"

DEBUG = True  # Set to False in production


SERVER_PRIVATE_KEY_PATHS = (
    "/etc/aismixer/keys/aismixer_private.pem",
    "/etc/aismixer/aismixer_private.key",
    "aismixer_private.pem",
    "aismixer_private.key",
)


def resolve_existing_path(candidates):
    for path in candidates:
        if os.path.exists(path):
            return path
    return candidates[-1]


def resolve_local_path(path):
    if os.path.isabs(path) or path.startswith("/"):
        return path
    return os.path.join(base_dir, path)


def _load_authorized_identity_public_key(encoded_public_key):
    if not isinstance(encoded_public_key, str):
        raise TypeError(
            "authorized station public key must be base64 text"
        )
    if not encoded_public_key:
        raise ValueError("authorized station public key must not be empty")
    try:
        encoded_ascii = encoded_public_key.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError(
            "authorized station public key must be ASCII base64"
        ) from exc
    try:
        public_key_bytes = base64.b64decode(
            encoded_ascii,
            validate=True,
        )
    except (binascii.Error, ValueError) as exc:
        raise ValueError(
            "authorized station public key must be valid base64"
        ) from exc
    if base64.b64encode(public_key_bytes) != encoded_ascii:
        raise ValueError(
            "authorized station public key must use canonical base64"
        )
    if len(public_key_bytes) != 33:
        raise ValueError(
            "authorized station public key must be a 33-byte "
            "compressed P-256 point"
        )
    if public_key_bytes[0] not in (0x02, 0x03):
        raise ValueError(
            "authorized station public key must use compressed "
            "P-256 point encoding"
        )

    try:
        public_key = ec.EllipticCurvePublicKey.from_encoded_point(
            ec.SECP256R1(),
            public_key_bytes,
        )
    except ValueError:
        raise ValueError(
            "authorized station public key is not a valid P-256 point"
        ) from None
    canonical = public_key.public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.CompressedPoint,
    )
    if canonical != public_key_bytes:
        raise ValueError(
            "authorized station public key is not canonically encoded"
        )
    return public_key


base_dir = os.path.dirname(os.path.abspath(__file__))

auth_keys_path = resolve_existing_path(
    (
        "/etc/aismixer/authorized_keys.yaml",
        os.path.join(base_dir, "authorized_keys.yaml"),
    )
)

priv_key_path = resolve_existing_path(
    tuple(resolve_local_path(path) for path in SERVER_PRIVATE_KEY_PATHS)
)

with open(auth_keys_path, 'r') as f:
    authorized_db = yaml.safe_load(f)

AUTHORIZED_KEYS = {
    entry["name"]: _load_authorized_identity_public_key(entry["pubkey"])
    for entry in authorized_db["authorized_clients"]
}

with open(priv_key_path, 'rb') as f:
    server_priv = serialization.load_pem_private_key(
        f.read(),
        password=None,
    )
if not isinstance(server_priv, ec.EllipticCurvePrivateKey):
    raise TypeError("server identity private key must be an EC private key")
if not isinstance(server_priv.curve, ec.SECP256R1):
    raise ValueError("server identity private key must use P-256")


def _validate_positive_int(name, value):
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < 1:
        raise ValueError(f"{name} must be at least 1")
    return value


def _validate_positive_ttl(name, value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be an integer or float")
    if not value > 0:
        raise ValueError(f"{name} must be greater than 0")
    return value


@dataclass(frozen=True)
class _ExpiringRecord:
    key: object
    expires_at: float


@dataclass(frozen=True)
class _ExpiringSetAdmission:
    accepted: bool
    expired: int
    capacity_evicted: int


class _BoundedExpiringSet:
    def __init__(self, ttl, max_entries):
        self._ttl = ttl
        self._max_entries = max_entries
        self._live_by_key = {}
        self._expiry_order = deque()

    def __len__(self):
        return len(self._live_by_key)

    def _cleanup_expired(self, now):
        expired = 0
        while self._expiry_order:
            record = self._expiry_order[0]
            current = self._live_by_key.get(record.key)
            if current is not record:
                self._expiry_order.popleft()
                continue
            if record.expires_at > now:
                break
            self._expiry_order.popleft()
            del self._live_by_key[record.key]
            expired += 1
        return expired

    def _evict_oldest_live(self):
        while self._expiry_order:
            record = self._expiry_order.popleft()
            if self._live_by_key.get(record.key) is not record:
                continue
            del self._live_by_key[record.key]
            return 1
        raise RuntimeError("expiring-set ordering is inconsistent")

    def contains(self, key, now):
        expired = self._cleanup_expired(now)
        return key in self._live_by_key, expired

    def accept(self, key, now):
        expired = self._cleanup_expired(now)
        if key in self._live_by_key:
            return _ExpiringSetAdmission(False, expired, 0)

        capacity_evicted = 0
        if len(self._live_by_key) >= self._max_entries:
            capacity_evicted = self._evict_oldest_live()

        record = _ExpiringRecord(key=key, expires_at=now + self._ttl)
        self._live_by_key[key] = record
        self._expiry_order.append(record)
        return _ExpiringSetAdmission(True, expired, capacity_evicted)

    def discard_all(self):
        discarded = len(self._live_by_key)
        self._live_by_key.clear()
        self._expiry_order.clear()
        return discarded


@dataclass
class _SecureSession:
    _address: object
    station_id: str
    client_to_server_aesgcm: AESGCM
    server_to_client_aesgcm: AESGCM
    created_at: float
    last_seen: float
    seen_data_nonces: _BoundedExpiringSet


@dataclass
class _PendingSecureSession:
    _address: object
    station_id: str
    client_to_server_aesgcm: AESGCM
    server_to_client_aesgcm: AESGCM
    created_at: float
    seen_data_nonces: _BoundedExpiringSet


@dataclass(frozen=True)
class SecureStateStats:
    handshake_replay_accepted: int
    handshake_replay_rejected: int
    handshake_replay_expired: int
    handshake_replay_capacity_evicted: int

    sessions_created: int
    sessions_replaced: int
    sessions_touched: int
    sessions_expired: int
    sessions_capacity_evicted: int

    pending_sessions_created: int
    pending_sessions_replaced: int
    pending_sessions_promoted: int
    pending_sessions_expired: int
    pending_sessions_capacity_evicted: int

    data_nonces_accepted: int
    data_nonce_replays: int
    data_nonces_expired: int
    data_nonces_capacity_evicted: int
    data_nonces_session_discarded: int

    current_handshake_replays: int
    peak_handshake_replays: int
    current_sessions: int
    peak_sessions: int
    current_pending_sessions: int
    peak_pending_sessions: int
    current_data_nonces: int
    peak_data_nonces: int


class SecureState:
    def __init__(
        self,
        session_ttl=SESSION_TTL_SECONDS,
        max_sessions=SESSION_MAX,
        handshake_replay_ttl=HANDSHAKE_REPLAY_TTL_SECONDS,
        handshake_replay_max=HANDSHAKE_REPLAY_MAX,
        data_nonce_ttl=DATA_NONCE_TTL_SECONDS,
        data_nonce_max_per_session=DATA_NONCE_MAX_PER_SESSION,
        pending_session_ttl=PENDING_SESSION_TTL_SECONDS,
        max_pending_sessions=PENDING_SESSION_MAX,
    ):
        self._session_ttl = _validate_positive_ttl(
            "session_ttl", session_ttl)
        self._max_sessions = _validate_positive_int(
            "max_sessions", max_sessions)
        self._pending_session_ttl = _validate_positive_ttl(
            "pending_session_ttl", pending_session_ttl)
        self._max_pending_sessions = _validate_positive_int(
            "max_pending_sessions", max_pending_sessions)
        self._handshake_replay_ttl = _validate_positive_ttl(
            "handshake_replay_ttl", handshake_replay_ttl)
        self._handshake_replay_max = _validate_positive_int(
            "handshake_replay_max", handshake_replay_max)
        self._data_nonce_ttl = _validate_positive_ttl(
            "data_nonce_ttl", data_nonce_ttl)
        self._data_nonce_max_per_session = _validate_positive_int(
            "data_nonce_max_per_session", data_nonce_max_per_session)

        self._handshake_replays = _BoundedExpiringSet(
            self._handshake_replay_ttl,
            self._handshake_replay_max,
        )
        self._sessions = OrderedDict()
        self._pending_sessions = OrderedDict()

        self._handshake_replay_accepted = 0
        self._handshake_replay_rejected = 0
        self._handshake_replay_expired = 0
        self._handshake_replay_capacity_evicted = 0

        self._sessions_created = 0
        self._sessions_replaced = 0
        self._sessions_touched = 0
        self._sessions_expired = 0
        self._sessions_capacity_evicted = 0

        self._pending_sessions_created = 0
        self._pending_sessions_replaced = 0
        self._pending_sessions_promoted = 0
        self._pending_sessions_expired = 0
        self._pending_sessions_capacity_evicted = 0

        self._data_nonces_accepted = 0
        self._data_nonce_replays = 0
        self._data_nonces_expired = 0
        self._data_nonces_capacity_evicted = 0
        self._data_nonces_session_discarded = 0

        self._current_data_nonces = 0
        self._peak_handshake_replays = 0
        self._peak_sessions = 0
        self._peak_pending_sessions = 0
        self._peak_data_nonces = 0

    def stats(self) -> SecureStateStats:
        return SecureStateStats(
            handshake_replay_accepted=self._handshake_replay_accepted,
            handshake_replay_rejected=self._handshake_replay_rejected,
            handshake_replay_expired=self._handshake_replay_expired,
            handshake_replay_capacity_evicted=(
                self._handshake_replay_capacity_evicted
            ),
            sessions_created=self._sessions_created,
            sessions_replaced=self._sessions_replaced,
            sessions_touched=self._sessions_touched,
            sessions_expired=self._sessions_expired,
            sessions_capacity_evicted=self._sessions_capacity_evicted,
            pending_sessions_created=self._pending_sessions_created,
            pending_sessions_replaced=self._pending_sessions_replaced,
            pending_sessions_promoted=self._pending_sessions_promoted,
            pending_sessions_expired=self._pending_sessions_expired,
            pending_sessions_capacity_evicted=(
                self._pending_sessions_capacity_evicted
            ),
            data_nonces_accepted=self._data_nonces_accepted,
            data_nonce_replays=self._data_nonce_replays,
            data_nonces_expired=self._data_nonces_expired,
            data_nonces_capacity_evicted=(
                self._data_nonces_capacity_evicted
            ),
            data_nonces_session_discarded=(
                self._data_nonces_session_discarded
            ),
            current_handshake_replays=len(self._handshake_replays),
            peak_handshake_replays=self._peak_handshake_replays,
            current_sessions=len(self._sessions),
            peak_sessions=self._peak_sessions,
            current_pending_sessions=len(self._pending_sessions),
            peak_pending_sessions=self._peak_pending_sessions,
            current_data_nonces=self._current_data_nonces,
            peak_data_nonces=self._peak_data_nonces,
        )

    def accept_handshake_replay(self, key, now):
        admission = self._handshake_replays.accept(key, now)
        self._handshake_replay_expired += admission.expired
        self._handshake_replay_capacity_evicted += (
            admission.capacity_evicted
        )
        if not admission.accepted:
            self._handshake_replay_rejected += 1
            return False

        self._handshake_replay_accepted += 1
        self._peak_handshake_replays = max(
            self._peak_handshake_replays,
            len(self._handshake_replays),
        )
        return True

    def _discard_session_nonces(self, session):
        discarded_nonces = session.seen_data_nonces.discard_all()
        self._current_data_nonces -= discarded_nonces
        self._data_nonces_session_discarded += discarded_nonces

    def _remove_session(self, addr, reason):
        session = self._sessions.pop(addr)
        self._discard_session_nonces(session)

        if reason == "expired":
            self._sessions_expired += 1
        elif reason == "capacity":
            self._sessions_capacity_evicted += 1
        elif reason == "replaced":
            self._sessions_replaced += 1
        else:
            raise ValueError(f"Unknown session removal reason: {reason}")
        return session

    def _remove_pending_session(self, addr, reason):
        pending = self._pending_sessions.pop(addr)
        self._discard_session_nonces(pending)

        if reason == "expired":
            self._pending_sessions_expired += 1
        elif reason == "capacity":
            self._pending_sessions_capacity_evicted += 1
        elif reason == "replaced":
            self._pending_sessions_replaced += 1
        else:
            raise ValueError(
                f"Unknown pending-session removal reason: {reason}"
            )
        return pending

    def cleanup_expired_sessions(self, now):
        expired = []
        while self._sessions:
            addr, session = next(iter(self._sessions.items()))
            if now - session.last_seen < self._session_ttl:
                break
            self._remove_session(addr, "expired")
            expired.append(addr)
        return expired

    def cleanup_expired_pending_sessions(self, now):
        expired = []
        while self._pending_sessions:
            addr, pending = next(iter(self._pending_sessions.items()))
            if now < pending.created_at + self._pending_session_ttl:
                break
            self._remove_pending_session(addr, "expired")
            expired.append(addr)
        return expired

    def install_session(
        self,
        addr,
        station_id,
        client_to_server_aesgcm,
        server_to_client_aesgcm,
        now,
    ):
        self.cleanup_expired_sessions(now)

        if addr in self._sessions:
            self._remove_session(addr, "replaced")
        elif len(self._sessions) >= self._max_sessions:
            oldest_addr = next(iter(self._sessions))
            self._remove_session(oldest_addr, "capacity")

        session = _SecureSession(
            _address=addr,
            station_id=station_id,
            client_to_server_aesgcm=client_to_server_aesgcm,
            server_to_client_aesgcm=server_to_client_aesgcm,
            created_at=now,
            last_seen=now,
            seen_data_nonces=_BoundedExpiringSet(
                self._data_nonce_ttl,
                self._data_nonce_max_per_session,
            ),
        )
        self._sessions[addr] = session
        self._sessions_created += 1
        self._peak_sessions = max(
            self._peak_sessions,
            len(self._sessions),
        )
        return session

    def install_pending_session(
        self,
        addr,
        station_id,
        client_to_server_aesgcm,
        server_to_client_aesgcm,
        now,
    ):
        self.cleanup_expired_pending_sessions(now)

        if addr in self._pending_sessions:
            self._remove_pending_session(addr, "replaced")
        elif len(self._pending_sessions) >= self._max_pending_sessions:
            oldest_addr = next(iter(self._pending_sessions))
            self._remove_pending_session(oldest_addr, "capacity")

        pending = _PendingSecureSession(
            _address=addr,
            station_id=station_id,
            client_to_server_aesgcm=client_to_server_aesgcm,
            server_to_client_aesgcm=server_to_client_aesgcm,
            created_at=now,
            seen_data_nonces=_BoundedExpiringSet(
                self._data_nonce_ttl,
                self._data_nonce_max_per_session,
            ),
        )
        self._pending_sessions[addr] = pending
        self._pending_sessions_created += 1
        self._peak_pending_sessions = max(
            self._peak_pending_sessions,
            len(self._pending_sessions),
        )
        return pending

    def get_active_session(self, addr, now):
        self.cleanup_expired_sessions(now)
        return self._sessions.get(addr)

    def get_pending_session(self, addr, now):
        self.cleanup_expired_pending_sessions(now)
        return self._pending_sessions.get(addr)

    def _get_live_session_handle(self, addr, session, now):
        if self._sessions.get(addr) is not session:
            return None

        self.cleanup_expired_sessions(now)
        if self._sessions.get(addr) is not session:
            return None

        return session

    def _get_live_pending_session_handle(self, addr, pending, now):
        if self._pending_sessions.get(addr) is not pending:
            return None

        self.cleanup_expired_pending_sessions(now)
        if self._pending_sessions.get(addr) is not pending:
            return None

        return pending

    def _touch_active_session(self, addr, session, now):
        session.last_seen = now
        self._sessions.move_to_end(addr)
        self._sessions_touched += 1

    def touch_session(self, addr, session, now):
        if self._get_live_session_handle(addr, session, now) is None:
            return False
        self._touch_active_session(addr, session, now)
        return True

    def promote_pending_session(self, addr, pending, now):
        if self._get_live_pending_session_handle(
            addr, pending, now
        ) is None:
            return None

        self.cleanup_expired_sessions(now)
        self._pending_sessions.pop(addr)
        self._pending_sessions_promoted += 1

        if addr in self._sessions:
            self._remove_session(addr, "replaced")
        elif len(self._sessions) >= self._max_sessions:
            oldest_addr = next(iter(self._sessions))
            self._remove_session(oldest_addr, "capacity")

        session = _SecureSession(
            _address=addr,
            station_id=pending.station_id,
            client_to_server_aesgcm=(
                pending.client_to_server_aesgcm
            ),
            server_to_client_aesgcm=(
                pending.server_to_client_aesgcm
            ),
            created_at=now,
            last_seen=now,
            seen_data_nonces=pending.seen_data_nonces,
        )
        self._sessions[addr] = session
        self._sessions_created += 1
        self._peak_sessions = max(
            self._peak_sessions,
            len(self._sessions),
        )
        return session

    def _account_expired_data_nonces(self, expired):
        self._data_nonces_expired += expired
        self._current_data_nonces -= expired

    def data_nonce_seen(self, session, nonce, now):
        if self._get_live_session_handle(
            session._address, session, now
        ) is None:
            return False
        seen, expired = session.seen_data_nonces.contains(nonce, now)
        self._account_expired_data_nonces(expired)
        if seen:
            self._data_nonce_replays += 1
        return seen

    def pending_data_nonce_seen(self, pending, nonce, now):
        if self._get_live_pending_session_handle(
            pending._address, pending, now
        ) is None:
            return False
        seen, expired = pending.seen_data_nonces.contains(nonce, now)
        self._account_expired_data_nonces(expired)
        return seen

    def accept_data_nonce(self, session, nonce, now):
        if self._get_live_session_handle(
            session._address, session, now
        ) is None:
            return False
        admission = session.seen_data_nonces.accept(nonce, now)
        self._account_expired_data_nonces(admission.expired)
        self._data_nonces_capacity_evicted += admission.capacity_evicted
        self._current_data_nonces -= admission.capacity_evicted

        if not admission.accepted:
            self._data_nonce_replays += 1
            return False

        self._data_nonces_accepted += 1
        self._current_data_nonces += 1
        self._peak_data_nonces = max(
            self._peak_data_nonces,
            self._current_data_nonces,
        )
        return True

    def accept_pending_data_nonce(self, pending, nonce, now):
        if self._get_live_pending_session_handle(
            pending._address, pending, now
        ) is None:
            return False
        admission = pending.seen_data_nonces.accept(nonce, now)
        self._account_expired_data_nonces(admission.expired)
        self._data_nonces_capacity_evicted += admission.capacity_evicted
        self._current_data_nonces -= admission.capacity_evicted

        if not admission.accepted:
            self._data_nonce_replays += 1
            return False

        self._data_nonces_accepted += 1
        self._current_data_nonces += 1
        self._peak_data_nonces = max(
            self._peak_data_nonces,
            self._current_data_nonces,
        )
        return True


secure_state = SecureState()


def _update_replay_digest(digest, field):
    if not isinstance(field, bytes):
        raise TypeError("handshake replay fields must be bytes")
    if len(field) > (1 << 32) - 1:
        raise ValueError(
            "handshake replay field exceeds unsigned 32-bit framing"
        )
    digest.update(len(field).to_bytes(4, "big"))
    digest.update(field)


def build_handshake_replay_key(
    client_auth_digest,
    client_signature,
):
    if not isinstance(client_auth_digest, bytes):
        raise TypeError("client_auth_digest must be bytes")
    if len(client_auth_digest) != 32:
        raise ValueError("client_auth_digest must be exactly 32 bytes")
    if not isinstance(client_signature, bytes):
        raise TypeError("client_signature must be bytes")
    if not client_signature:
        raise ValueError("client_signature must not be empty")

    digest = hashes.Hash(hashes.SHA256())
    for field in (
        DOMAIN_CONTEXT,
        _HANDSHAKE_REPLAY_LABEL,
        client_auth_digest,
        client_signature,
    ):
        _update_replay_digest(digest, field)
    return digest.finalize()


def parse_secure_data_packet(data):
    min_len = len(DATA_PREFIX) + 12 + 16
    if not data.startswith(DATA_PREFIX):
        raise ValueError("Invalid secure data packet prefix")
    if len(data) < min_len:
        raise ValueError("Secure data packet too short")
    nonce = data[len(DATA_PREFIX):len(DATA_PREFIX)+12]
    ciphertext = data[len(DATA_PREFIX)+12:]
    return nonce, ciphertext


def build_no_session_hint(station_id=None):
    if station_id:
        return NOSESSION_PREFIX + b"|" + station_id.encode()
    return NOSESSION_PREFIX


def encrypt_secure_json_message(aesgcm, message):
    nonce = os.urandom(12)
    plaintext = json.dumps(message, separators=(",", ":")).encode()
    return DATA_PREFIX + nonce + aesgcm.encrypt(nonce, plaintext, DATA_AAD)


def _is_session_confirmation_ping(message, station_id):
    if not isinstance(message, dict):
        return False
    sequence = message.get("seq")
    timestamp = message.get("timestamp")
    return (
        message.get("type") == "ping"
        and isinstance(sequence, int)
        and not isinstance(sequence, bool)
        and sequence == SESSION_CONFIRMATION_SEQUENCE
        and isinstance(timestamp, int)
        and not isinstance(timestamp, bool)
        and message.get("source_id") == station_id
    )


def _build_server_handshake(client_hello, client_ephemeral_public_key):
    """Build one authenticated ServerHello and directional session ciphers."""

    server_random = os.urandom(32)
    server_ephemeral_private_key = generate_ephemeral_private_key()
    server_ephemeral_public_bytes = serialize_ephemeral_public_key(
        server_ephemeral_private_key.public_key()
    )
    server_auth_digest = build_server_auth_digest(
        station_id=client_hello.station_id,
        timestamp=client_hello.timestamp,
        client_random=client_hello.client_random,
        client_ephemeral_public_key=(
            client_hello.client_ephemeral_public_key
        ),
        client_signature=client_hello.client_signature,
        server_random=server_random,
        server_ephemeral_public_key=server_ephemeral_public_bytes,
    )
    server_signature = sign_transcript_digest(
        server_priv,
        server_auth_digest,
    )
    shared_secret = derive_ephemeral_shared_secret(
        server_ephemeral_private_key,
        client_ephemeral_public_key,
    )
    session_transcript_hash = build_session_transcript_hash(
        station_id=client_hello.station_id,
        timestamp=client_hello.timestamp,
        client_random=client_hello.client_random,
        client_ephemeral_public_key=(
            client_hello.client_ephemeral_public_key
        ),
        client_signature=client_hello.client_signature,
        server_random=server_random,
        server_ephemeral_public_key=server_ephemeral_public_bytes,
        server_signature=server_signature,
    )
    key_material = derive_session_key_material(
        shared_secret,
        session_transcript_hash,
    )
    server_hello = ServerHello(
        server_random=server_random,
        server_ephemeral_public_key=server_ephemeral_public_bytes,
        server_signature=server_signature,
    )
    response_packet = build_server_hello_packet(server_hello)
    return (
        response_packet,
        AESGCM(key_material.client_to_server_key),
        AESGCM(key_material.server_to_client_key),
    )


async def _secure_server_loop(
    sock,
    queue,
    ip,
    port,
    sec_input_id=None,
    ingress_policy=None,
    *,
    state=None,
    wall_clock=None,
    monotonic_clock=None,
):
    sock.bind((ip, port))
    sock.setblocking(False)
    loop = asyncio.get_running_loop()
    policy = ingress_policy or NetworkPolicy.unrestricted()
    state_owner = secure_state if state is None else state
    wall_now = time.time if wall_clock is None else wall_clock
    monotonic_now = time.monotonic if monotonic_clock is None else monotonic_clock

    print(f"[+] Secure listener started on {ip}:{port}")

    while True:
        data, addr = await loop.sock_recvfrom(sock, 8192)
        source_ip = addr[0]
        if not policy.allows(source_ip):
            continue
        local_now = monotonic_now()
        state_owner.cleanup_expired_sessions(local_now)
        state_owner.cleanup_expired_pending_sessions(local_now)

        if data.startswith(CLIENT_HELLO_PREFIX):
            try:
                client_hello = parse_client_hello_packet(data)
                station_id = client_hello.station_id
                timestamp = client_hello.timestamp

                if abs(wall_now() - timestamp) > 30:
                    print(
                        f"[!] Rejected {station_id}: timestamp out of window")
                    continue

                client_identity_public_key = AUTHORIZED_KEYS.get(station_id)
                if client_identity_public_key is None:
                    print(f"[!] Rejected {station_id}: unknown client")
                    continue

                client_auth_digest = build_client_auth_digest(
                    station_id=station_id,
                    timestamp=timestamp,
                    client_random=client_hello.client_random,
                    client_ephemeral_public_key=(
                        client_hello.client_ephemeral_public_key
                    ),
                )
                if not verify_transcript_signature(
                    client_identity_public_key,
                    client_hello.client_signature,
                    client_auth_digest,
                ):
                    raise ValueError(
                        "ClientHello identity signature verification failed"
                    )
                client_ephemeral_public_key = parse_ephemeral_public_key(
                    client_hello.client_ephemeral_public_key
                )
                replay_key = build_handshake_replay_key(
                    client_auth_digest,
                    client_hello.client_signature,
                )
                if not state_owner.accept_handshake_replay(
                    replay_key, local_now
                ):
                    print(f"[!] Rejected {station_id}: handshake replay")
                    continue

                (
                    response_packet,
                    client_to_server_aesgcm,
                    server_to_client_aesgcm,
                ) = _build_server_handshake(
                    client_hello,
                    client_ephemeral_public_key,
                )
                state_owner.install_pending_session(
                    addr,
                    station_id,
                    client_to_server_aesgcm,
                    server_to_client_aesgcm,
                    local_now,
                )

                sock.sendto(response_packet, addr)
                print(
                    f"[+] Sent authenticated ServerHello "
                    f"to {station_id} @ {addr}"
                )

            except Exception as e:
                print(
                    f"[!] Handshake error from {addr}: {type(e).__name__}: {e}")

        elif data.startswith(DATA_PREFIX):
            try:
                pending = state_owner.get_pending_session(
                    addr, local_now
                )
                session = state_owner.get_active_session(addr, local_now)
                if pending is None and session is None:
                    print(f"[!] No session for {addr}")
                    sock.sendto(build_no_session_hint(), addr)
                    continue

                nonce, ciphertext = parse_secure_data_packet(data)

                if pending is not None and not (
                    state_owner.pending_data_nonce_seen(
                        pending, nonce, local_now
                    )
                ):
                    try:
                        pending_plaintext = (
                            pending.client_to_server_aesgcm.decrypt(
                                nonce,
                                ciphertext,
                                DATA_AAD,
                            )
                        )
                    except InvalidTag:
                        pass
                    else:
                        pending_message = json.loads(
                            pending_plaintext.decode()
                        )
                        if not _is_session_confirmation_ping(
                            pending_message,
                            pending.station_id,
                        ):
                            print(
                                f"[!] Invalid session confirmation "
                                f"from {addr}"
                            )
                            continue
                        if not state_owner.accept_pending_data_nonce(
                            pending, nonce, local_now
                        ):
                            print(
                                f"[!] Duplicate secure data nonce "
                                f"from {addr}"
                            )
                            continue

                        session = state_owner.promote_pending_session(
                            addr,
                            pending,
                            local_now,
                        )
                        if session is None:
                            continue
                        if not state_owner.touch_session(
                            addr, session, local_now
                        ):
                            continue

                        response = {
                            "type": "pong",
                            "seq": SESSION_CONFIRMATION_SEQUENCE,
                            "timestamp": int(wall_now()),
                            "source_id": session.station_id,
                        }
                        sock.sendto(
                            encrypt_secure_json_message(
                                session.server_to_client_aesgcm,
                                response,
                            ),
                            addr,
                        )
                        print(
                            f"[+] Confirmed secure session for "
                            f"{session.station_id} @ {addr}"
                        )
                        continue

                if session is None:
                    continue

                station_id = session.station_id
                client_to_server_aesgcm = (
                    session.client_to_server_aesgcm
                )
                if state_owner.data_nonce_seen(session, nonce, local_now):
                    print(f"[!] Duplicate secure data nonce from {addr}")
                    continue

                plaintext = client_to_server_aesgcm.decrypt(
                    nonce,
                    ciphertext,
                    DATA_AAD,
                )

                msg = json.loads(plaintext.decode())
                if msg.get("source_id") != station_id:
                    print(f"[!] source_id mismatch from {addr}")
                    continue

                message_type = msg.get("type")
                if message_type == "ping":
                    sequence = msg.get("seq")
                    if (
                        type(sequence) is not int
                        or sequence <= SESSION_CONFIRMATION_SEQUENCE
                    ):
                        print(f"[!] Invalid ping from {addr}")
                        continue
                elif message_type == "nmea":
                    if "payload" not in msg:
                        print(f"[!] Invalid NMEA data from {addr}")
                        continue
                else:
                    print(f"[!] Unknown secure message type from {addr}")
                    continue

                if not state_owner.accept_data_nonce(
                    session, nonce, local_now
                ):
                    print(f"[!] Duplicate secure data nonce from {addr}")
                    continue

                state_owner.touch_session(addr, session, local_now)

                if message_type == "ping":
                    response = {
                        "type": "pong",
                        "seq": msg["seq"],
                        "timestamp": int(wall_now()),
                        "source_id": station_id,
                    }
                    sock.sendto(
                        encrypt_secure_json_message(
                            session.server_to_client_aesgcm,
                            response,
                        ),
                        addr,
                    )
                    continue

                src_for_queue = sec_input_id or station_id or "ANONYMOUS"
                peer = addr if 'addr' in locals() else None
                remote_ip = peer[0] if isinstance(
                    peer, tuple) and peer else None
                assembler_key = f"{peer[0]}:{peer[1]}" if isinstance(
                    peer, tuple) and peer else (remote_ip or "sec")
                frame = frame_from_text_payload(
                    kind="sec",
                    source_id=build_udpsec_source_id(station_id),
                    alias_for_s=src_for_queue,
                    remote_ip=remote_ip,
                    assembler_key=assembler_key,
                    payload=msg["payload"],
                )
                if frame is not None:
                    await queue.put(frame)

                if DEBUG:
                    print(
                        f"{wall_now()} [SECURE] "
                        f"From {station_id}: {msg['payload']}")

            except Exception as e:
                print(
                    f"[!] Secure data error from {addr}: {type(e).__name__}: {e}")


async def secure_server(
    queue,
    ip,
    port,
    sec_input_id=None,
    ingress_policy=None,
    *,
    state=None,
    wall_clock=None,
    monotonic_clock=None,
):
    """Run one secure ingress producer and close its owned socket exactly once."""

    sock = socket.socket(
        socket.AF_INET6 if ':' in ip else socket.AF_INET,
        socket.SOCK_DGRAM,
    )
    try:
        await _secure_server_loop(
            sock,
            queue,
            ip,
            port,
            sec_input_id=sec_input_id,
            ingress_policy=ingress_policy,
            state=state,
            wall_clock=wall_clock,
            monotonic_clock=monotonic_clock,
        )
    finally:
        sock.close()
