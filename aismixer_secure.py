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


HANDSHAKE_PREFIX = b"NMEA-H"
DATA_PREFIX = b"NMEA-D"
KEEPALIVE_PREFIX = b"KEEPALIVE"
CONTEXT_STRING = b"NMEA-AUTH-v1"
SESSION_TTL_SECONDS = 300

DEBUG = True  # Set to False in production


def resolve_path(primary, fallback):
    return primary if os.path.exists(primary) else fallback


base_dir = os.path.dirname(os.path.abspath(__file__))

auth_keys_path = resolve_path(
    "/etc/aismixer/authorized_keys.yaml",
    os.path.join(base_dir, "authorized_keys.yaml")
)

priv_key_path = resolve_path(
    "/etc/aismixer/aismixer_private.key",
    os.path.join(base_dir, "aismixer_private.key")
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


def create_session(station_id, aesgcm, now):
    return {
        "station_id": station_id,
        "aesgcm": aesgcm,
        "created_at": now,
        "last_seen": now,
    }


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
                digest.update(
                    HANDSHAKE_PREFIX + station_id.encode() + timestamp.to_bytes(8, "big"))
                to_verify = digest.finalize()

                verify_signature(client_pub_bytes, signature, to_verify)

                # build response
                digest_s = hashes.Hash(
                    hashes.SHA256(), backend=default_backend())
                digest_s.update(
                    HANDSHAKE_PREFIX + station_id.encode() + timestamp.to_bytes(8, "big"))
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

        elif data.startswith(KEEPALIVE_PREFIX + b"|"):
            try:
                station_id, _ = parse_keepalive_packet(data)
                if handle_keepalive_session(
                    sessions, addr, station_id, time.time(), SESSION_TTL_SECONDS
                ):
                    if DEBUG:
                        print(f"{time.time()} [SECURE] Keepalive from {station_id} @ {addr}")
                else:
                    print(f"[!] Ignored keepalive from {addr}")
            except Exception as e:
                print(
                    f"[!] Keepalive error from {addr}: {type(e).__name__}: {e}")

        elif data.startswith(DATA_PREFIX):
            try:
                session = get_active_session(
                    sessions, addr, time.time(), SESSION_TTL_SECONDS)
                if not session:
                    print(f"[!] No session for {addr}")
                    continue
                station_id = session["station_id"]
                aesgcm = session["aesgcm"]

                nonce, ciphertext = parse_secure_data_packet(data)
                plaintext = aesgcm.decrypt(nonce, ciphertext, b"NMEA")

                msg = json.loads(plaintext.decode())
                if msg["source_id"] != station_id:
                    print(f"[!] source_id mismatch from {addr}")
                    continue

                touch_session(session, time.time())

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
                                             alias_for_s=src_for_queue,
                                             remote_ip=remote_ip,
                                             assembler_key=assembler_key,
                                             raw_line=msg["payload"]))

            except Exception as e:
                print(
                    f"[!] Secure data error from {addr}: {type(e).__name__}: {e}")
