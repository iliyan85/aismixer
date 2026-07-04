import os
import asyncio
import base64
import json
import socket
import time
import yaml
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.backends import default_backend
from core.event import IngressEvent
from core.source_identity import build_udpsec_source_id


HANDSHAKE_PREFIX = b"NMEA-H"
DATA_PREFIX = b"NMEA-D"
KEEPALIVE_PREFIX = b"KEEPALIVE"
NOSESSION_PREFIX = b"NOSESSION"
DATA_AAD = b"NMEA"
CONTEXT_STRING = b"NMEA-AUTH-v1"
SESSION_TTL_SECONDS = 300
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

sessions = {}
handshake_replay_cache = {}


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


def mark_handshake_replay_seen(cache, key, now, ttl, max_entries):
    expired = [cached_key for cached_key, expires_at in cache.items()
               if expires_at <= now]
    for cached_key in expired:
        cache.pop(cached_key, None)

    if key in cache:
        return False

    cache[key] = now + ttl
    while len(cache) > max_entries:
        oldest_key = min(cache, key=cache.get)
        cache.pop(oldest_key, None)
    return True


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


def create_session(station_id, aesgcm, now):
    return {
        "station_id": station_id,
        "aesgcm": aesgcm,
        "created_at": now,
        "last_seen": now,
        "seen_data_nonces": {},
    }


def cleanup_expired_data_nonces(session, now, ttl):
    seen_nonces = session["seen_data_nonces"]
    expired = [
        nonce for nonce, expires_at in seen_nonces.items()
        if expires_at <= now
    ]
    for nonce in expired:
        seen_nonces.pop(nonce, None)
    return expired


def data_nonce_seen(session, nonce, now, ttl):
    cleanup_expired_data_nonces(session, now, ttl)
    return nonce in session["seen_data_nonces"]


def mark_data_nonce_seen(session, nonce, now, ttl, max_entries):
    cleanup_expired_data_nonces(session, now, ttl)
    seen_nonces = session["seen_data_nonces"]
    if nonce in seen_nonces:
        return False

    seen_nonces[nonce] = now + ttl
    while len(seen_nonces) > max_entries:
        oldest_nonce = min(seen_nonces, key=seen_nonces.get)
        seen_nonces.pop(oldest_nonce, None)
    return True


def get_active_session(session_store, addr, now, ttl):
    session = session_store.get(addr)
    if not session:
        return None
    if now - session["last_seen"] > ttl:
        session_store.pop(addr, None)
        return None
    return session


def touch_session(session, now):
    session["last_seen"] = now


def cleanup_expired_sessions(session_store, now, ttl):
    expired = [
        addr for addr, session in session_store.items()
        if now - session["last_seen"] > ttl
    ]
    for addr in expired:
        session_store.pop(addr, None)
    return expired


def handle_keepalive_session(session_store, addr, station_id, now, ttl):
    session = get_active_session(session_store, addr, now, ttl)
    if not session:
        return False
    if session["station_id"] != station_id:
        return False
    touch_session(session, now)
    return True


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


async def secure_server(queue, ip, port, sec_input_id=None):
    sock = socket.socket(
        socket.AF_INET6 if ':' in ip else socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((ip, port))
    sock.setblocking(False)
    loop = asyncio.get_running_loop()

    print(f"[+] Secure listener started on {ip}:{port}")

    while True:
        data, addr = await loop.sock_recvfrom(sock, 8192)

        if data.startswith(HANDSHAKE_PREFIX):
            try:
                # Parse text-based handshake: NMEA-H|station_id|timestamp|base64(signature)
                parts = data[len(HANDSHAKE_PREFIX):].lstrip(b"|").split(b"|")
                if len(parts) != 3:
                    raise ValueError("Invalid handshake format")

                station_id = parts[0].decode()
                timestamp = int(parts[1].decode())
                signature = base64.b64decode(parts[2])

                if abs(time.time() - timestamp) > 30:
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
                if not mark_handshake_replay_seen(
                    handshake_replay_cache,
                    replay_key,
                    time.time(),
                    HANDSHAKE_REPLAY_TTL_SECONDS,
                    HANDSHAKE_REPLAY_MAX,
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
                sessions[addr] = create_session(
                    station_id, aesgcm, time.time())

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
                if handle_keepalive_session(
                    sessions, addr, station_id, time.time(), SESSION_TTL_SECONDS
                ):
                    if DEBUG:
                        print(f"{time.time()} [SECURE] Keepalive from {station_id} @ {addr}")
                else:
                    print(f"[!] Ignored keepalive from {addr}")
                    sock.sendto(build_no_session_hint(station_id), addr)
            except Exception as e:
                print(
                    f"[!] Keepalive error from {addr}: {type(e).__name__}: {e}")
                sock.sendto(build_no_session_hint(station_id), addr)

        elif data.startswith(DATA_PREFIX):
            try:
                session = get_active_session(
                    sessions, addr, time.time(), SESSION_TTL_SECONDS)
                if not session:
                    print(f"[!] No session for {addr}")
                    sock.sendto(build_no_session_hint(), addr)
                    continue
                station_id = session["station_id"]
                aesgcm = session["aesgcm"]

                nonce, ciphertext = parse_secure_data_packet(data)
                if data_nonce_seen(
                    session, nonce, time.time(), DATA_NONCE_TTL_SECONDS
                ):
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

                if not mark_data_nonce_seen(
                    session,
                    nonce,
                    time.time(),
                    DATA_NONCE_TTL_SECONDS,
                    DATA_NONCE_MAX_PER_SESSION,
                ):
                    print(f"[!] Duplicate secure data nonce from {addr}")
                    continue

                touch_session(session, time.time())

                if message_type == "ping":
                    response = {
                        "type": "pong",
                        "seq": msg["seq"],
                        "timestamp": int(time.time()),
                        "source_id": station_id,
                    }
                    sock.sendto(encrypt_secure_json_message(aesgcm, response), addr)
                    continue

                if DEBUG:
                    print(
                        f"{time.time()} [SECURE] From {station_id}: {msg['payload']}")

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
