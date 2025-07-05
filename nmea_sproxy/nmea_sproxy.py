import socket
import yaml
import os
import time
import base64
import sys
import json
import threading
from meta_cleaner import extract_nmea_sentences
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.exceptions import InvalidSignature


# Константи
HANDSHAKE_PREFIX = b"NMEA-H"
DATA_PREFIX = b"NMEA-D"
KEEPALIVE_INTERVAL = 30  # секунди

DEFAULT_CONFIG = {
    "listen_ip": "192.168.190.214",
    "listen_port": 17778,
    "remote_host": "127.0.0.1",
    "remote_port": 19999,
    "station_id": "boat_001",
    "remote_public_key": "nmea_sproxy/aismixer_public.pem",
    "station_private_key": "nmea_sproxy/station_private.key",
    "reconnect_delay": 5,
    "log_level": "INFO",
}


def load_config(path="/etc/nmea_sproxy/config.yaml"):
    config = DEFAULT_CONFIG
    if os.path.exists(path):
        with open(path, 'r') as f:
            user_config = yaml.safe_load(f)
            if user_config:
                config.update(user_config)
    else:
        print(f"⚠️ Config file not found: {path}. Using defaults.")
    return config


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
    encryptor.authenticate_additional_data(b"NMEA")
    ciphertext = encryptor.update(plaintext) + encryptor.finalize()
    return iv + ciphertext + encryptor.tag


def send_keepalive_loop(sock, remote_addr, station_id):
    while True:
        time.sleep(KEEPALIVE_INTERVAL)
        timestamp = int(time.time())
        message = f"KEEPALIVE|{station_id}|{timestamp}".encode()
        sock.sendto(message, remote_addr)


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
    sock.sendto(packet, remote_addr)

    try:
        response, _ = sock.recvfrom(2048)
    except socket.timeout:
        print("⚠️ No response from server during handshake.")
        return None
    except ConnectionResetError as e:
        print(f"❌ Connection reset by peer (likely no listener yet): {e}")
        return None

    if not response.startswith(b"OK|"):
        print(f"⚠️ Handshake failed: {response.decode(errors='ignore')}")
        return None

    try:
        _, server_sig_b64 = response.split(b"|", 1)
        server_signature = base64.b64decode(server_sig_b64)
    except Exception as e:
        print(f"⚠️ Invalid handshake response format: {e}")
        return None

    if not verify_signature(payload, server_signature, server_pubkey):
        print("❌ Server signature verification failed.")
        return None

    shared_secret = private_key.exchange(ec.ECDH(), server_pubkey)
    session_key = derive_session_key(
        shared_secret, signature, server_signature)
    print(f"✅ Mutual handshake OK. Session hash: {session_key.hex()[:16]}...")
    return session_key


def forward_loop(udp_sock, out_sock, config, session_key, remote_addr):
    while True:
        try:
            data, _ = udp_sock.recvfrom(4096)
            if not data.startswith(b"!"):
                #continue
                for clean_line in extract_nmea_sentences(data.decode(errors="replace").strip()):
                    if not clean_line:
                        continue
                

                    json_obj = {
                        "type": "nmea",
                        #"payload": data.decode(errors="replace").strip(),
                        "payload": clean_line,
                        "timestamp": int(time.time()),
                        "source_id": config["station_id"]
                    }
                    plaintext = json.dumps(json_obj).encode()
                    encrypted = encrypt_message_aes_gcm(plaintext, session_key)
                    print(clean_line)
                    out_sock.sendto(DATA_PREFIX + encrypted, remote_addr)

        except Exception as e:
            print(f"❌ Forwarding error: {e}")
            break


def main():
    config = load_config()
    private_key = load_private_key(config["station_private_key"])
    server_pubkey = load_public_key(config["remote_public_key"])
    remote_addr = (config["remote_host"], config["remote_port"])

    udp_family = socket.AF_INET6 if ':' in config["listen_ip"] else socket.AF_INET
    out_family = socket.AF_INET6 if ':' in config["remote_host"] else socket.AF_INET

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
            threading.Thread(target=send_keepalive_loop, args=(
                out_sock, remote_addr, config["station_id"]), daemon=True).start()
            forward_loop(udp_sock, out_sock, config, session_key, remote_addr)
        print(f"🔁 Retrying in {config['reconnect_delay']} seconds...")
        time.sleep(config["reconnect_delay"])


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("👋 Exit by user.")
        sys.exit(0)
