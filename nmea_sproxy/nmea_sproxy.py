import argparse
import socket
import yaml
import os
import time
import base64
import sys
import json
import select
from meta_cleaner import extract_nmea_sentences
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidSignature


# Константи
HANDSHAKE_PREFIX = b"NMEA-H"
DATA_PREFIX = b"NMEA-D"
NOSESSION_PREFIX = b"NOSESSION"
DATA_AAD = b"NMEA"
SERVER_PACKET_IGNORED = "ignored"
SERVER_PACKET_AUTHENTICATED = "authenticated"
SESSION_END_PLANNED_REFRESH = "planned_refresh"
SESSION_END_PEER_TIMEOUT = "peer_timeout"
SESSION_END_NOSESSION = "nosession"
SESSION_END_SOCKET_ERROR = "socket_error"
HANDSHAKE_FAILURE = "handshake_failure"
SERVER_PACKET_NO_SESSION = SESSION_END_NOSESSION
CONFIG_ENV_VAR = "NMEA_SPROXY_CONFIG"
DEFAULT_PROCESS_TITLE = "nmea_sproxy"
SYSTEM_CONFIG_PATH = "/etc/nmea_sproxy/config.yaml"
LOCAL_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "config.yaml",
)
CANONICAL_STATION_PRIVATE_KEY_PATH = "/etc/nmea_sproxy/keys/station_private.pem"
LEGACY_STATION_PRIVATE_KEY_PATH = "station_private.key"
CANONICAL_REMOTE_PUBLIC_KEY_PATH = "/etc/nmea_sproxy/keys/aismixer_public.pem"
LEGACY_REMOTE_PUBLIC_KEY_PATH = "aismixer_public.pem"

DEFAULT_CONFIG = {
    "listen_ip": "::",
    "listen_port": 50000,
    "remote_host": "192.168.190.53",
    "remote_port": 19999,
    "station_id": "boat_001",
    "remote_public_key": CANONICAL_REMOTE_PUBLIC_KEY_PATH,
    "station_private_key": CANONICAL_STATION_PRIVATE_KEY_PATH,
    "reconnect_delay": 5,
    "keepalive_interval": 30,
    "peer_timeout": 90,
    "session_refresh_interval": 0,
    "log_level": "INFO",
}


def resolve_existing_path(candidates):
    for path in candidates:
        if os.path.exists(path):
            return path
    return candidates[-1]


def apply_default_key_paths(config, user_config=None):
    user_config = user_config or {}
    if "station_private_key" not in user_config:
        config["station_private_key"] = resolve_existing_path(
            (
                CANONICAL_STATION_PRIVATE_KEY_PATH,
                LEGACY_STATION_PRIVATE_KEY_PATH,
            )
        )

    remote_key_configured = (
        "remote_public_key" in user_config
        or "aismixer_public_key" in user_config
    )
    if not remote_key_configured:
        config["remote_public_key"] = resolve_existing_path(
            (
                CANONICAL_REMOTE_PUBLIC_KEY_PATH,
                LEGACY_REMOTE_PUBLIC_KEY_PATH,
            )
        )
    return config


def resolve_configured_key_paths(config, user_config, config_path):
    if not user_config or not config_path:
        return config

    config_dir = os.path.dirname(os.path.abspath(config_path))
    configured_keys = []
    if "station_private_key" in user_config:
        configured_keys.append("station_private_key")
    if "remote_public_key" in user_config or "aismixer_public_key" in user_config:
        configured_keys.append("remote_public_key")

    for key in configured_keys:
        path = os.fspath(config[key])
        if not os.path.isabs(path) and not path.startswith("/"):
            config[key] = os.path.normpath(os.path.join(config_dir, path))

    station_path = os.fspath(config["station_private_key"])
    if (
        os.path.basename(station_path) == "station_private.pem"
        and not os.path.exists(station_path)
    ):
        legacy_path = os.path.join(
            os.path.dirname(station_path), LEGACY_STATION_PRIVATE_KEY_PATH
        )
        if os.path.exists(legacy_path):
            config["station_private_key"] = legacy_path
    return config


def resolve_config_path(cli_path=None, environ=None):
    if cli_path:
        return os.fspath(cli_path)

    environ = os.environ if environ is None else environ
    env_path = environ.get(CONFIG_ENV_VAR)
    if env_path:
        return env_path

    for path in (SYSTEM_CONFIG_PATH, LOCAL_CONFIG_PATH):
        if os.path.exists(path):
            return path
    return None


def load_config(path=None):
    config = dict(DEFAULT_CONFIG)
    user_config = None
    selected_path = os.fspath(path) if path is not None else resolve_config_path()
    if selected_path and os.path.exists(selected_path):
        with open(selected_path, 'r') as f:
            user_config = yaml.safe_load(f)
            if user_config:
                config.update(user_config)
                if (
                    "aismixer_public_key" in user_config
                    and "remote_public_key" not in user_config
                ):
                    config["remote_public_key"] = user_config["aismixer_public_key"]
    elif selected_path:
        print(f"⚠️ Config file not found: {selected_path}. Using defaults.")
    else:
        print("⚠️ No config file found. Using built-in defaults.")
    apply_default_key_paths(config, user_config)
    resolve_configured_key_paths(config, user_config, selected_path)
    return config


def set_process_title(title):
    try:
        from setproctitle import setproctitle
        setproctitle(title)
    except ImportError:
        pass


def load_private_key(path):
    with open(path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def load_public_key(path):
    with open(path, "rb") as f:
        return serialization.load_pem_public_key(f.read())


def sign_message(message, private_key):
    return private_key.sign(message, ec.ECDSA(hashes.SHA256()))


def verify_signature(message, signature, public_key):
    try:
        public_key.verify(signature, message, ec.ECDSA(hashes.SHA256()))
        return True
    except InvalidSignature:
        return False

# Нов метод: derive_session_key


def derive_session_key(shared_secret, client_signature, server_signature):
    digest = hashes.Hash(hashes.SHA256())
    digest.update(b"NMEA-SESSION")
    digest.update(shared_secret)
    digest.update(client_signature)
    digest.update(server_signature)
    return digest.finalize()

# Стар метод за съвместимост (не се използва)


def compute_session_hash(station_id, timestamp, signature, server_signature):
    digest = hashes.Hash(hashes.SHA256())
    digest.update(HANDSHAKE_PREFIX)
    digest.update(station_id.encode())
    digest.update(str(timestamp).encode())
    digest.update(signature)
    digest.update(server_signature)
    return digest.finalize()


def encrypt_message_aes_gcm(plaintext, key):
    iv = os.urandom(12)
    encryptor = Cipher(
        algorithms.AES(key),
        modes.GCM(iv)
    ).encryptor()
    encryptor.authenticate_additional_data(DATA_AAD)
    ciphertext = encryptor.update(plaintext) + encryptor.finalize()
    return iv + ciphertext + encryptor.tag


def decrypt_secure_json_message(data, key):
    min_len = len(DATA_PREFIX) + 12 + 16
    if not data.startswith(DATA_PREFIX) or len(data) < min_len:
        raise ValueError("Invalid secure server packet")
    nonce = data[len(DATA_PREFIX):len(DATA_PREFIX)+12]
    ciphertext = data[len(DATA_PREFIX)+12:]
    plaintext = AESGCM(key).decrypt(nonce, ciphertext, DATA_AAD)
    return json.loads(plaintext.decode())


def encrypt_secure_json_message(message, key):
    plaintext = json.dumps(message, separators=(",", ":")).encode()
    return DATA_PREFIX + encrypt_message_aes_gcm(plaintext, key)


def remote_addresses_match(addr, remote_addr):
    return (
        isinstance(addr, tuple)
        and isinstance(remote_addr, tuple)
        and len(addr) >= 2
        and len(remote_addr) >= 2
        and addr[0] == remote_addr[0]
        and addr[1] == remote_addr[1]
    )


def resolve_remote_addr(host, port, family):
    addresses = socket.getaddrinfo(host, port, family, socket.SOCK_DGRAM)
    if not addresses:
        raise OSError(f"Could not resolve remote address: {host}:{port}")
    return addresses[0][4]


def is_no_session_hint(data):
    return data == NOSESSION_PREFIX or data.startswith(NOSESSION_PREFIX + b"|")


def handle_server_packet(
    data,
    addr,
    remote_addr,
    session_key,
    station_id,
    expected_ping_seq,
):
    if not remote_addresses_match(addr, remote_addr):
        return SERVER_PACKET_IGNORED

    if is_no_session_hint(data):
        return SERVER_PACKET_NO_SESSION

    try:
        message = decrypt_secure_json_message(data, session_key)
    except Exception:
        return SERVER_PACKET_IGNORED

    if (
        message.get("type") == "pong"
        and message.get("source_id") == station_id
        and message.get("seq") == expected_ping_seq
    ):
        return SERVER_PACKET_AUTHENTICATED
    return SERVER_PACKET_IGNORED


def session_expiration_reason(
    now,
    session_started_at,
    last_authenticated_peer,
    config,
):
    if now - last_authenticated_peer >= float(config["peer_timeout"]):
        return SESSION_END_PEER_TIMEOUT
    refresh_interval = float(config["session_refresh_interval"])
    if refresh_interval > 0 and now - session_started_at >= refresh_interval:
        return SESSION_END_PLANNED_REFRESH
    return None


def session_poll_timeout(
    now,
    session_started_at,
    last_authenticated_peer,
    last_ping_at,
    config,
):
    deadlines = [
        last_ping_at + float(config["keepalive_interval"]),
        last_authenticated_peer + float(config["peer_timeout"]),
    ]
    refresh_interval = float(config["session_refresh_interval"])
    if refresh_interval > 0:
        deadlines.append(session_started_at + refresh_interval)
    return max(0.0, min(deadlines) - now)


def retry_delay_for_reason(reason, config):
    if reason == SESSION_END_PLANNED_REFRESH:
        return None
    return config["reconnect_delay"]


def send_ping(sock, remote_addr, session_key, station_id, seq):
    message = {
        "type": "ping",
        "seq": seq,
        "timestamp": int(time.time()),
        "source_id": station_id,
    }
    sock.sendto(encrypt_secure_json_message(message, session_key), remote_addr)


def perform_handshake(sock, config, private_key, server_pubkey, remote_addr):
    timestamp = int(time.time())
    payload = HANDSHAKE_PREFIX + \
        config["station_id"].encode() + timestamp.to_bytes(8, "big")
    signature = sign_message(payload, private_key)
    packet = b"|".join([
        HANDSHAKE_PREFIX,
        config["station_id"].encode(),
        str(timestamp).encode(),
        base64.b64encode(signature),
    ])
    try:
        sock.sendto(packet, remote_addr)
    except OSError as e:
        print(f"❌ Handshake send error: {e}")
        return None

    gettimeout = getattr(sock, "gettimeout", None)
    settimeout = getattr(sock, "settimeout", None)
    original_timeout = gettimeout() if gettimeout else 5.0
    handshake_timeout = (
        5.0 if original_timeout is None else max(float(original_timeout), 0.1)
    )
    deadline = time.monotonic() + handshake_timeout

    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                print("⚠️ No response from server during handshake.")
                return None
            if settimeout:
                settimeout(remaining)

            try:
                response, addr = sock.recvfrom(2048)
            except socket.timeout:
                print("⚠️ No response from server during handshake.")
                return None
            except ConnectionResetError as e:
                print(f"❌ Connection reset by peer (likely no listener yet): {e}")
                return None
            except OSError as e:
                print(f"❌ Handshake receive error: {e}")
                return None

            if not remote_addresses_match(addr, remote_addr):
                continue
            if not response.startswith(b"OK|"):
                print(
                    f"⚠️ Ignored handshake response: "
                    f"{response.decode(errors='ignore')}"
                )
                continue

            try:
                _, server_sig_b64 = response.split(b"|", 1)
                server_signature = base64.b64decode(server_sig_b64)
            except Exception as e:
                print(f"⚠️ Invalid handshake response format: {e}")
                continue

            if not verify_signature(payload, server_signature, server_pubkey):
                print("❌ Server signature verification failed.")
                continue

            shared_secret = private_key.exchange(ec.ECDH(), server_pubkey)
            session_key = derive_session_key(
                shared_secret, signature, server_signature)
            print(
                f"✅ Mutual handshake OK. "
                f"Session hash: {session_key.hex()[:16]}..."
            )
            return session_key
    finally:
        if settimeout:
            settimeout(original_timeout)


def forward_loop(udp_sock, out_sock, config, session_key, remote_addr):
    session_started_at = time.monotonic()
    last_authenticated_peer = session_started_at
    last_ping_at = session_started_at
    expected_ping_seq = None
    next_ping_seq = 1

    while True:
        now = time.monotonic()
        expiration_reason = session_expiration_reason(
            now, session_started_at, last_authenticated_peer, config
        )
        if expiration_reason:
            if expiration_reason == SESSION_END_PLANNED_REFRESH:
                print("Secure session planned refresh due.")
            else:
                print(f"Secure session invalidated: {expiration_reason}")
            return expiration_reason

        keepalive_interval = float(config["keepalive_interval"])
        poll_timeout = session_poll_timeout(
            now,
            session_started_at,
            last_authenticated_peer,
            last_ping_at,
            config,
        )

        try:
            readable, _, _ = select.select(
                [udp_sock, out_sock], [], [], poll_timeout
            )
        except Exception as e:
            print(f"❌ Forwarding error: {e}")
            return SESSION_END_SOCKET_ERROR

        if out_sock in readable:
            try:
                response, addr = out_sock.recvfrom(8192)
            except Exception as e:
                print(f"❌ Secure peer receive error: {e}")
                return SESSION_END_SOCKET_ERROR

            result = handle_server_packet(
                response,
                addr,
                remote_addr,
                session_key,
                config["station_id"],
                expected_ping_seq,
            )
            if result == SERVER_PACKET_NO_SESSION:
                print("Secure server reported NOSESSION.")
                return SERVER_PACKET_NO_SESSION
            if result == SERVER_PACKET_AUTHENTICATED:
                last_authenticated_peer = time.monotonic()
                expected_ping_seq = None

        if udp_sock in readable:
            try:
                data, _ = udp_sock.recvfrom(4096)
                clean_lines = extract_nmea_sentences(
                    data.decode(errors="replace").strip()
                )
                for clean_line in clean_lines:
                    if not clean_line:
                        continue

                    json_obj = {
                        "type": "nmea",
                        "payload": clean_line,
                        "timestamp": int(time.time()),
                        "source_id": config["station_id"],
                    }
                    print(clean_line)
                    out_sock.sendto(
                        encrypt_secure_json_message(json_obj, session_key),
                        remote_addr,
                    )
            except Exception as e:
                print(f"❌ Forwarding error: {e}")
                return SESSION_END_SOCKET_ERROR

        now = time.monotonic()
        if now - last_ping_at >= keepalive_interval:
            try:
                send_ping(
                    out_sock,
                    remote_addr,
                    session_key,
                    config["station_id"],
                    next_ping_seq,
                )
            except Exception as e:
                print(f"❌ Secure ping error: {e}")
                return SESSION_END_SOCKET_ERROR
            expected_ping_seq = next_ping_seq
            next_ping_seq += 1
            last_ping_at = now


def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Forward one local AIS UDP input to one encrypted remote secure output."
        )
    )
    parser.add_argument(
        "--config",
        help=(
            "config file path; overrides NMEA_SPROXY_CONFIG and automatic "
            "system/local config discovery"
        ),
    )
    parser.add_argument(
        "--process-title",
        default=DEFAULT_PROCESS_TITLE,
        help=f"process title shown by system tools (default: {DEFAULT_PROCESS_TITLE})",
    )
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    set_process_title(args.process_title)
    config_path = resolve_config_path(args.config)
    if config_path and not os.path.exists(config_path):
        print(f"Config file not found: {config_path}", file=sys.stderr)
        return 1

    config = load_config(config_path)
    private_key = load_private_key(config["station_private_key"])
    server_pubkey = load_public_key(config["remote_public_key"])

    udp_family = socket.AF_INET6 if ':' in config["listen_ip"] else socket.AF_INET
    out_family = socket.AF_INET6 if ':' in config["remote_host"] else socket.AF_INET
    remote_addr = resolve_remote_addr(
        config["remote_host"], config["remote_port"], out_family
    )

    udp_sock = socket.socket(udp_family, socket.SOCK_DGRAM)
    udp_sock.bind((config["listen_ip"], config["listen_port"]))

    out_sock = socket.socket(out_family, socket.SOCK_DGRAM)
    out_sock.settimeout(5.0)

    print(f"📡 Listening on UDP {config['listen_ip']}:{config['listen_port']}")
    print(
        f"📤 Forwarding encrypted packets to {config['remote_host']}:{config['remote_port']}")

    while True:
        session_key = perform_handshake(
            out_sock, config, private_key, server_pubkey, remote_addr)
        if session_key:
            reason = forward_loop(
                udp_sock, out_sock, config, session_key, remote_addr
            )
        else:
            reason = HANDSHAKE_FAILURE

        retry_delay = retry_delay_for_reason(reason, config)
        if retry_delay is None:
            print("Refreshing secure session immediately.")
            continue

        print(f"🔁 Retrying in {retry_delay} seconds...")
        time.sleep(retry_delay)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("👋 Exit by user.")
