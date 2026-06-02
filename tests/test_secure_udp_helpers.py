import base64
import importlib.util
import io
import os
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


ROOT = Path(__file__).resolve().parents[1]
NMEA_SPROXY_DIR = ROOT / "nmea_sproxy"

SERVER_PRIVATE_KEY_FILENAME = "aismixer_private.key"
SERVER_PUBLIC_KEY_FOR_PROXY_FILENAME = "aismixer_public.pem"
STATION_PRIVATE_KEY_FILENAME = "station_private.key"
STATION_PUBLIC_KEY_FILENAME = "station_public.pem"


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


def load_secure_module_with_fake_keys(monkeypatch):
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
        if name == SERVER_PRIVATE_KEY_FILENAME:
            return io.BytesIO(server_private_bytes)
        return real_open(path, mode, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os.path, "exists", lambda path: False)
        patch.setattr("builtins.open", fake_open)
        spec = importlib.util.spec_from_file_location(
            "aismixer_secure_test_helpers", ROOT / "aismixer_secure.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
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


def test_proxy_encrypt_message_aes_gcm_uses_12_byte_nonce_and_nmea_aad():
    proxy = load_proxy_module()
    key = b"\x01" * 32
    plaintext = b'{"type":"nmea","payload":"!AIVDM,1,1,,A,payload,0*00"}'

    encrypted = proxy.encrypt_message_aes_gcm(plaintext, key)
    nonce = encrypted[:12]
    ciphertext_and_tag = encrypted[12:]

    assert len(nonce) == 12
    assert AESGCM(key).decrypt(nonce, ciphertext_and_tag, b"NMEA") == plaintext


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


def test_create_session_stores_identity_crypto_and_timestamps(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    aesgcm = object()

    session = secure.create_session("boat_001", aesgcm, now=100.0)

    assert session == {
        "station_id": "boat_001",
        "aesgcm": aesgcm,
        "created_at": 100.0,
        "last_seen": 100.0,
    }


def test_session_ttl_seconds_is_300(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)

    assert secure.SESSION_TTL_SECONDS == 300


def test_get_active_session_returns_non_expired_session(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    addr = ("192.0.2.10", 50000)
    session = secure.create_session("boat_001", object(), now=100.0)
    session_store = {addr: session}

    assert secure.get_active_session(session_store, addr, now=120.0, ttl=30.0) is session
    assert addr in session_store


def test_get_active_session_returns_none_for_missing_session(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)

    assert secure.get_active_session({}, ("192.0.2.10", 50000), now=120.0, ttl=30.0) is None


def test_get_active_session_removes_expired_session(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    addr = ("192.0.2.10", 50000)
    session_store = {addr: secure.create_session("boat_001", object(), now=100.0)}

    assert secure.get_active_session(session_store, addr, now=131.0, ttl=30.0) is None
    assert addr not in session_store


def test_touch_session_updates_last_seen(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    session = secure.create_session("boat_001", object(), now=100.0)

    secure.touch_session(session, now=125.0)

    assert session["created_at"] == 100.0
    assert session["last_seen"] == 125.0


def test_cleanup_expired_sessions_removes_only_expired_sessions(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    fresh_addr = ("192.0.2.10", 50000)
    expired_addr = ("192.0.2.11", 50001)
    session_store = {
        fresh_addr: secure.create_session("fresh", object(), now=120.0),
        expired_addr: secure.create_session("expired", object(), now=90.0),
    }

    removed = secure.cleanup_expired_sessions(session_store, now=121.0, ttl=30.0)

    assert removed == [expired_addr]
    assert fresh_addr in session_store
    assert expired_addr not in session_store


def test_session_rehandshake_overwrite_uses_normal_dict_assignment(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    addr = ("192.0.2.10", 50000)
    session_store = {addr: secure.create_session("old", object(), now=100.0)}
    new_session = secure.create_session("new", object(), now=150.0)

    session_store[addr] = new_session

    assert session_store == {addr: new_session}


def test_handle_keepalive_session_updates_matching_active_session(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    addr = ("192.0.2.10", 50000)
    session = secure.create_session("boat_001", object(), now=100.0)
    session_store = {addr: session}

    assert secure.handle_keepalive_session(
        session_store, addr, "boat_001", now=120.0, ttl=30.0
    ) is True
    assert session["last_seen"] == 120.0


def test_handle_keepalive_session_returns_false_for_missing_session(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)

    assert secure.handle_keepalive_session(
        {}, ("192.0.2.10", 50000), "boat_001", now=120.0, ttl=30.0
    ) is False


def test_handle_keepalive_session_removes_expired_session(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    addr = ("192.0.2.10", 50000)
    session_store = {addr: secure.create_session("boat_001", object(), now=100.0)}

    assert secure.handle_keepalive_session(
        session_store, addr, "boat_001", now=131.0, ttl=30.0
    ) is False
    assert addr not in session_store


def test_handle_keepalive_session_returns_false_for_station_id_mismatch(monkeypatch):
    secure = load_secure_module_with_fake_keys(monkeypatch)
    addr = ("192.0.2.10", 50000)
    session = secure.create_session("boat_001", object(), now=100.0)
    session_store = {addr: session}

    assert secure.handle_keepalive_session(
        session_store, addr, "other_station", now=120.0, ttl=30.0
    ) is False
    assert session["last_seen"] == 100.0


def test_proxy_load_config_uses_remote_public_key_as_canonical(tmp_path):
    proxy = load_proxy_module()
    config_path = tmp_path / "config.yaml"
    config_path.write_text("remote_public_key: canonical.pem\n", encoding="utf-8")

    config = proxy.load_config(str(config_path))

    assert config["remote_public_key"] == "canonical.pem"


def test_proxy_load_config_supports_legacy_aismixer_public_key_as_fallback(tmp_path):
    proxy = load_proxy_module()
    config_path = tmp_path / "config.yaml"
    config_path.write_text("aismixer_public_key: legacy.pem\n", encoding="utf-8")

    config = proxy.load_config(str(config_path))

    assert config["remote_public_key"] == "legacy.pem"


def test_proxy_load_config_prefers_canonical_key_when_both_names_are_present(tmp_path):
    proxy = load_proxy_module()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "remote_public_key: canonical.pem\n"
        "aismixer_public_key: legacy.pem\n",
        encoding="utf-8",
    )

    config = proxy.load_config(str(config_path))

    assert config["remote_public_key"] == "canonical.pem"


def test_current_secure_udp_key_filename_expectations():
    proxy = load_proxy_module()

    assert SERVER_PRIVATE_KEY_FILENAME == "aismixer_private.key"
    assert SERVER_PUBLIC_KEY_FOR_PROXY_FILENAME == "aismixer_public.pem"
    assert STATION_PRIVATE_KEY_FILENAME == "station_private.key"
    assert STATION_PUBLIC_KEY_FILENAME == "station_public.pem"
    assert proxy.DEFAULT_CONFIG["remote_public_key"].endswith(
        SERVER_PUBLIC_KEY_FOR_PROXY_FILENAME
    )
    assert proxy.DEFAULT_CONFIG["station_private_key"].endswith(
        STATION_PRIVATE_KEY_FILENAME
    )
