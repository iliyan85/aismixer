import os
import asyncio
import base64
import json
import socket
import time
import yaml
from collections import OrderedDict, deque
from dataclasses import dataclass
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.backends import default_backend
from core.event import IngressEvent
from core.network_policy import NetworkPolicy
from core.source_identity import build_udpsec_source_id


HANDSHAKE_PREFIX = b"NMEA-H"
DATA_PREFIX = b"NMEA-D"
KEEPALIVE_PREFIX = b"KEEPALIVE"
NOSESSION_PREFIX = b"NOSESSION"
DATA_AAD = b"NMEA"
CONTEXT_STRING = b"NMEA-AUTH-v1"
SESSION_TTL_SECONDS = 300
SESSION_MAX = 100000
HANDSHAKE_REPLAY_TTL_SECONDS = 60
HANDSHAKE_REPLAY_MAX = 100000
DATA_NONCE_TTL_SECONDS = SESSION_TTL_SECONDS
DATA_NONCE_MAX_PER_SESSION = 100000

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
    entry["name"]: base64.b64decode(entry["pubkey"])
    for entry in authorized_db["authorized_clients"]
}

with open(priv_key_path, 'rb') as f:
    server_priv = serialization.load_pem_private_key(
        f.read(), password=None, backend=default_backend())

server_pub = server_priv.public_key()
server_pub_bytes = server_pub.public_bytes(
    encoding=serialization.Encoding.X962,
    format=serialization.PublicFormat.CompressedPoint
)


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
    aesgcm: AESGCM
    created_at: float
    last_seen: float
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

    data_nonces_accepted: int
    data_nonce_replays: int
    data_nonces_expired: int
    data_nonces_capacity_evicted: int
    data_nonces_session_discarded: int

    current_handshake_replays: int
    peak_handshake_replays: int
    current_sessions: int
    peak_sessions: int
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
    ):
        self._session_ttl = _validate_positive_ttl(
            "session_ttl", session_ttl)
        self._max_sessions = _validate_positive_int(
            "max_sessions", max_sessions)
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

        self._handshake_replay_accepted = 0
        self._handshake_replay_rejected = 0
        self._handshake_replay_expired = 0
        self._handshake_replay_capacity_evicted = 0

        self._sessions_created = 0
        self._sessions_replaced = 0
        self._sessions_touched = 0
        self._sessions_expired = 0
        self._sessions_capacity_evicted = 0

        self._data_nonces_accepted = 0
        self._data_nonce_replays = 0
        self._data_nonces_expired = 0
        self._data_nonces_capacity_evicted = 0
        self._data_nonces_session_discarded = 0

        self._current_data_nonces = 0
        self._peak_handshake_replays = 0
        self._peak_sessions = 0
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

    def _remove_session(self, addr, reason):
        session = self._sessions.pop(addr)
        discarded_nonces = session.seen_data_nonces.discard_all()
        self._current_data_nonces -= discarded_nonces
        self._data_nonces_session_discarded += discarded_nonces

        if reason == "expired":
            self._sessions_expired += 1
        elif reason == "capacity":
            self._sessions_capacity_evicted += 1
        elif reason == "replaced":
            self._sessions_replaced += 1
        else:
            raise ValueError(f"Unknown session removal reason: {reason}")
        return session

    def cleanup_expired_sessions(self, now):
        expired = []
        while self._sessions:
            addr, session = next(iter(self._sessions.items()))
            if now - session.last_seen < self._session_ttl:
                break
            self._remove_session(addr, "expired")
            expired.append(addr)
        return expired

    def install_session(self, addr, station_id, aesgcm, now):
        self.cleanup_expired_sessions(now)

        if addr in self._sessions:
            self._remove_session(addr, "replaced")
        elif len(self._sessions) >= self._max_sessions:
            oldest_addr = next(iter(self._sessions))
            self._remove_session(oldest_addr, "capacity")

        session = _SecureSession(
            _address=addr,
            station_id=station_id,
            aesgcm=aesgcm,
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

    def get_active_session(self, addr, now):
        self.cleanup_expired_sessions(now)
        return self._sessions.get(addr)

    def _get_live_session_handle(self, addr, session, now):
        if self._sessions.get(addr) is not session:
            return None

        self.cleanup_expired_sessions(now)
        if self._sessions.get(addr) is not session:
            return None

        return session

    def _touch_active_session(self, addr, session, now):
        session.last_seen = now
        self._sessions.move_to_end(addr)
        self._sessions_touched += 1

    def touch_session(self, addr, session, now):
        if self._get_live_session_handle(addr, session, now) is None:
            return False
        self._touch_active_session(addr, session, now)
        return True

    def handle_keepalive(self, addr, station_id, now):
        session = self.get_active_session(addr, now)
        if session is None or session.station_id != station_id:
            return False
        self._touch_active_session(addr, session, now)
        return True

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


secure_state = SecureState()


def _field_bytes(value):
    if isinstance(value, str):
        return value.encode()
    return value


def _update_framed(digest, value):
    data = _field_bytes(value)
    digest.update(len(data).to_bytes(4, "big"))
    digest.update(data)


def build_current_handshake_payload(station_id, timestamp):
    return HANDSHAKE_PREFIX + station_id.encode() + timestamp.to_bytes(8, "big")


def build_handshake_context_v1(
    station_id,
    timestamp,
    client_pub_bytes,
    server_pub_bytes,
    context_string=CONTEXT_STRING,
):
    digest = hashes.Hash(hashes.SHA256(), backend=default_backend())
    for value in (
        context_string,
        station_id,
        timestamp.to_bytes(8, "big"),
        client_pub_bytes,
        server_pub_bytes,
    ):
        _update_framed(digest, value)
    return digest.finalize()


def build_session_transcript_v1(handshake_context, client_signature, server_signature):
    digest = hashes.Hash(hashes.SHA256(), backend=default_backend())
    for value in (handshake_context, client_signature, server_signature):
        _update_framed(digest, value)
    return digest.finalize()


def build_handshake_replay_key(station_id, timestamp, signature):
    digest = hashes.Hash(hashes.SHA256(), backend=default_backend())
    for value in (
        b"NMEA-H-REPLAY",
        station_id,
        timestamp.to_bytes(8, "big"),
        signature,
    ):
        _update_framed(digest, value)
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


def parse_keepalive_packet(data):
    parts = data.split(b"|")
    if len(parts) != 3 or parts[0] != KEEPALIVE_PREFIX:
        raise ValueError("Invalid keepalive packet format")
    if not parts[1]:
        raise ValueError("Missing keepalive station_id")
    if not parts[2]:
        raise ValueError("Missing keepalive timestamp")
    try:
        timestamp = int(parts[2].decode())
    except ValueError as e:
        raise ValueError("Invalid keepalive timestamp") from e
    return parts[1].decode(), timestamp


def parse_keepalive_station_id(data):
    parts = data.split(b"|", 2)
    if len(parts) < 2 or parts[0] != KEEPALIVE_PREFIX or not parts[1]:
        return None
    try:
        return parts[1].decode()
    except UnicodeDecodeError:
        return None


def build_no_session_hint(station_id=None):
    if station_id:
        return NOSESSION_PREFIX + b"|" + station_id.encode()
    return NOSESSION_PREFIX


def encrypt_secure_json_message(aesgcm, message):
    nonce = os.urandom(12)
    plaintext = json.dumps(message, separators=(",", ":")).encode()
    return DATA_PREFIX + nonce + aesgcm.encrypt(nonce, plaintext, DATA_AAD)


def verify_signature(pub_bytes, signature, message_digest):
    pubkey = ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP256R1(), pub_bytes)
    try:
        pubkey.verify(signature, message_digest, ec.ECDSA(
            utils.Prehashed(hashes.SHA256())))
    except Exception as e:
        raise ValueError(f"Signature verification failed: {e}")


def derive_session_key(shared_secret, combined_sig):
    h = hashes.Hash(hashes.SHA256(), backend=default_backend())
    h.update(b"NMEA-SESSION" + shared_secret + combined_sig)
    return h.finalize()


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
    sock = socket.socket(
        socket.AF_INET6 if ':' in ip else socket.AF_INET, socket.SOCK_DGRAM)
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

        if data.startswith(HANDSHAKE_PREFIX):
            try:
                # Parse text-based handshake: NMEA-H|station_id|timestamp|base64(signature)
                parts = data[len(HANDSHAKE_PREFIX):].lstrip(b"|").split(b"|")
                if len(parts) != 3:
                    raise ValueError("Invalid handshake format")

                station_id = parts[0].decode()
                timestamp = int(parts[1].decode())
                signature = base64.b64decode(parts[2])

                if abs(wall_now() - timestamp) > 30:
                    print(
                        f"[!] Rejected {station_id}: timestamp out of window")
                    continue

                client_pub_bytes = AUTHORIZED_KEYS.get(station_id)
                if not client_pub_bytes:
                    print(f"[!] Rejected {station_id}: unknown client")
                    continue

                digest = hashes.Hash(
                    hashes.SHA256(), backend=default_backend())
                digest.update(build_current_handshake_payload(station_id, timestamp))
                to_verify = digest.finalize()

                verify_signature(client_pub_bytes, signature, to_verify)

                replay_key = build_handshake_replay_key(
                    station_id, timestamp, signature)
                if not state_owner.accept_handshake_replay(
                    replay_key, local_now
                ):
                    print(f"[!] Rejected {station_id}: handshake replay")
                    continue

                # build response
                digest_s = hashes.Hash(
                    hashes.SHA256(), backend=default_backend())
                digest_s.update(build_current_handshake_payload(station_id, timestamp))
                to_sign = digest_s.finalize()
                sig_server = server_priv.sign(
                    to_sign, ec.ECDSA(utils.Prehashed(hashes.SHA256())))

                # build session key
                client_pubkey = ec.EllipticCurvePublicKey.from_encoded_point(
                    ec.SECP256R1(), client_pub_bytes)
                shared_secret = server_priv.exchange(ec.ECDH(), client_pubkey)
                session_key = derive_session_key(
                    shared_secret, signature + sig_server)
                aesgcm = AESGCM(session_key)
                state_owner.install_session(
                    addr, station_id, aesgcm, local_now)

                response = b"OK|" + base64.b64encode(sig_server)
                sock.sendto(response, addr)
                print(f"[+] Accepted handshake from {station_id} @ {addr}")

            except Exception as e:
                print(
                    f"[!] Handshake error from {addr}: {type(e).__name__}: {e}")

        elif data == KEEPALIVE_PREFIX or data.startswith(KEEPALIVE_PREFIX + b"|"):
            station_id = parse_keepalive_station_id(data)
            try:
                station_id, _ = parse_keepalive_packet(data)
                if state_owner.handle_keepalive(
                    addr, station_id, local_now
                ):
                    if DEBUG:
                        print(
                            f"{wall_now()} [SECURE] Keepalive "
                            f"from {station_id} @ {addr}"
                        )
                else:
                    print(f"[!] Ignored keepalive from {addr}")
                    sock.sendto(build_no_session_hint(station_id), addr)
            except Exception as e:
                print(
                    f"[!] Keepalive error from {addr}: {type(e).__name__}: {e}")
                sock.sendto(build_no_session_hint(station_id), addr)

        elif data.startswith(DATA_PREFIX):
            try:
                session = state_owner.get_active_session(addr, local_now)
                if session is None:
                    print(f"[!] No session for {addr}")
                    sock.sendto(build_no_session_hint(), addr)
                    continue
                station_id = session.station_id
                aesgcm = session.aesgcm

                nonce, ciphertext = parse_secure_data_packet(data)
                if state_owner.data_nonce_seen(session, nonce, local_now):
                    print(f"[!] Duplicate secure data nonce from {addr}")
                    continue

                plaintext = aesgcm.decrypt(nonce, ciphertext, DATA_AAD)

                msg = json.loads(plaintext.decode())
                if msg.get("source_id") != station_id:
                    print(f"[!] source_id mismatch from {addr}")
                    continue

                message_type = msg.get("type")
                if message_type == "ping":
                    if "seq" not in msg:
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
                    sock.sendto(encrypt_secure_json_message(aesgcm, response), addr)
                    continue

                if DEBUG:
                    print(
                        f"{wall_now()} [SECURE] "
                        f"From {station_id}: {msg['payload']}")

                src_for_queue = sec_input_id or station_id or "ANONYMOUS"
                peer = addr if 'addr' in locals() else None
                remote_ip = peer[0] if isinstance(
                    peer, tuple) and peer else None
                assembler_key = f"{peer[0]}:{peer[1]}" if isinstance(
                    peer, tuple) and peer else (remote_ip or "sec")
                await queue.put(IngressEvent(kind="sec",
                                             source_id=build_udpsec_source_id(station_id),
                                             alias_for_s=src_for_queue,
                                             remote_ip=remote_ip,
                                             assembler_key=assembler_key,
                                             raw_line=msg["payload"]))

            except Exception as e:
                print(
                    f"[!] Secure data error from {addr}: {type(e).__name__}: {e}")
