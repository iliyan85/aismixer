import argparse
from dataclasses import dataclass, field
from pathlib import Path
import socket
import yaml
import os
import math
import signal
import time
import sys
import json
import select
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from input_adapters import (
    InputConfigError,
    SERIAL_INPUT_TYPE,
    UDP_INPUT_TYPE,
    SerialInputAdapter,
    UdpInputAdapter,
    normalize_local_input_config,
)
from meta_cleaner import extract_nmea_sentences
from output_adapters import (
    OutputConfigError,
    PlainUdpOutputAdapter,
    UDPSEC_OUTPUT_TYPE,
    UDP_OUTPUT_TYPE,
    create_output_socket,
    create_outbound_socket as adapter_create_outbound_socket,
    normalize_output_config,
    parse_source_ip as adapter_parse_source_ip,
    resolve_output_endpoint,
    resolve_remote_addr as adapter_resolve_remote_addr,
)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
_SHARED_CORE_MODULES = (
    "key_material.py",
    "network_policy.py",
    "udpsec_crypto.py",
    "udpsec_protocol.py",
)


def add_shared_module_path():
    for base in (SCRIPT_DIR, REPO_ROOT):
        core_dir = os.path.join(base, "core")
        if all(
            os.path.exists(os.path.join(core_dir, module))
            for module in _SHARED_CORE_MODULES
        ):
            if base not in sys.path:
                sys.path.insert(0, base)
            return


add_shared_module_path()

from core.key_material import (  # noqa: E402
    KeyFileExistsError,
    generate_key_pair,
)
from core.network_policy import (  # noqa: E402
    NetworkPolicy,
    NetworkPolicyConfigError,
    compile_ingress_policy,
)
from core.udpsec_crypto import (  # noqa: E402
    SessionKeyMaterial,
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
from core.udpsec_protocol import (  # noqa: E402
    ClientHello,
    SESSION_CONFIRMATION_SEQUENCE,
    build_client_hello_packet,
    build_ping_message,
    build_session_close_message,
    is_matching_pong_message,
    is_session_close_message,
    parse_server_hello_packet,
)


# Константи
DATA_PREFIX = b"NMEA-D"
DATA_AAD = b"NMEA"
SERVER_PACKET_IGNORED = "ignored"
SERVER_PACKET_AUTHENTICATED = "authenticated"
SERVER_PACKET_PEER_CLOSE = "peer_close"
SESSION_END_PLANNED_REFRESH = "planned_refresh"
SESSION_END_PROACTIVE_REKEY = "proactive_rekey"
SESSION_END_PEER_GRACEFUL_CLOSE = "peer_graceful_close"
SESSION_END_PEER_TIMEOUT = "peer_timeout"
SESSION_END_SOCKET_ERROR = "socket_error"
SESSION_ACTION_SEND_PING = "send_ping"
HANDSHAKE_FAILURE = "handshake_failure"
CONFIG_ENV_VAR = "NMEA_SPROXY_CONFIG"
DEFAULT_PROCESS_TITLE = "nmea_sproxy"
SYSTEM_CONFIG_PATH = "/etc/nmea_sproxy/config.yaml"
LOCAL_CONFIG_PATH = os.path.join(
    SCRIPT_DIR,
    "config.yaml",
)
CANONICAL_STATION_PRIVATE_KEY_PATH = "/etc/nmea_sproxy/keys/station_private.pem"
CANONICAL_STATION_PUBLIC_KEY_PATH = "/etc/nmea_sproxy/keys/station_public.pem"
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

LEGACY_INPUT_CONFIG_FIELDS = frozenset({"listen_ip", "listen_port", "allow_from"})
LEGACY_OUTPUT_CONFIG_FIELDS = frozenset({"remote_host", "remote_port", "source_ip"})
LEGACY_INPUT_DEPRECATION_MESSAGE = (
    "DEPRECATION: legacy nmea_sproxy input configuration (omitted input or "
    "top-level listen_ip/listen_port/allow_from) is deprecated; use explicit "
    "input.type with input.listen_ip/input.listen_port/input.allow_from."
)
LEGACY_OUTPUT_DEPRECATION_MESSAGE = (
    "DEPRECATION: legacy nmea_sproxy UDPSEC output configuration (omitted output "
    "or top-level remote_host/remote_port/source_ip) is deprecated; use explicit "
    "output.type with output.host/output.port/output.source_ip."
)


class ProxyConfigError(ValueError):
    """Raised for operator-facing proxy configuration errors."""


class StationIdentityError(RuntimeError):
    """Raised when a required local UDPSEC station identity is unusable."""


class PeerTrustError(RuntimeError):
    """Raised when configured UDPSEC peer trust cannot be used safely."""


@dataclass(frozen=True)
class StationIdentity:
    private_path: Path
    public_path: Path | None
    generated: bool
    private_key: ec.EllipticCurvePrivateKey = field(
        repr=False,
        compare=False,
    )


def resolve_existing_path(candidates):
    for path in candidates:
        if os.path.exists(path):
            return path
    return candidates[-1]


def _path_present(path):
    return os.path.lexists(os.fspath(path))


def _paths_match(first, second):
    return os.path.normcase(os.path.abspath(os.fspath(first))) == os.path.normcase(
        os.path.abspath(os.fspath(second))
    )


def resolve_default_station_private_key_path():
    if _path_present(CANONICAL_STATION_PRIVATE_KEY_PATH):
        return CANONICAL_STATION_PRIVATE_KEY_PATH
    if _path_present(LEGACY_STATION_PRIVATE_KEY_PATH):
        return LEGACY_STATION_PRIVATE_KEY_PATH
    return CANONICAL_STATION_PRIVATE_KEY_PATH


def apply_default_key_paths(config, user_config=None):
    user_config = user_config or {}
    if "station_private_key" not in user_config:
        config["station_private_key"] = resolve_default_station_private_key_path()

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


def _configured_key_path(config, key):
    try:
        path = os.fspath(config[key])
    except (KeyError, TypeError) as exc:
        raise ProxyConfigError(f"{key}: must be a non-empty path string") from exc
    if not isinstance(path, str) or not path.strip():
        raise ProxyConfigError(f"{key}: must be a non-empty path string")
    return path


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
        path = _configured_key_path(config, key)
        if not os.path.isabs(path) and not path.startswith("/"):
            config[key] = os.path.normpath(os.path.join(config_dir, path))

    station_path = _configured_key_path(config, "station_private_key")
    if (
        os.path.basename(station_path) == "station_private.pem"
        and not _path_present(station_path)
    ):
        legacy_path = os.path.join(
            os.path.dirname(station_path), LEGACY_STATION_PRIVATE_KEY_PATH
        )
        if _path_present(legacy_path):
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


def report_legacy_config_deprecations(user_config):
    if not isinstance(user_config, dict):
        return

    if (
        "input" not in user_config
        or LEGACY_INPUT_CONFIG_FIELDS.intersection(user_config)
    ):
        print(LEGACY_INPUT_DEPRECATION_MESSAGE, file=sys.stderr)

    if (
        "output" not in user_config
        or LEGACY_OUTPUT_CONFIG_FIELDS.intersection(user_config)
    ):
        print(LEGACY_OUTPUT_DEPRECATION_MESSAGE, file=sys.stderr)


def load_config(path=None):
    config = dict(DEFAULT_CONFIG)
    user_config = None
    selected_path = os.fspath(path) if path is not None else resolve_config_path()
    if selected_path and os.path.exists(selected_path):
        with open(selected_path, 'r') as f:
            user_config = yaml.safe_load(f)
            if user_config is None:
                user_config = {}
            report_legacy_config_deprecations(user_config)
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
    validate_local_input_config(config)
    output_config = validate_output_config(config)
    if output_config["type"] == UDPSEC_OUTPUT_TYPE:
        validate_udpsec_lifecycle_config(config)
        apply_default_key_paths(config, user_config)
        resolve_configured_key_paths(config, user_config, selected_path)
    else:
        validate_reconnect_delay_config(config)
    return config


def _validate_timing_value(config, name, *, strictly_positive):
    value = config.get(name)
    comparison = (
        "greater than 0"
        if strictly_positive
        else "greater than or equal to 0"
    )
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProxyConfigError(f"{name} must be a finite number {comparison}")
    try:
        normalized = float(value)
    except (OverflowError, TypeError, ValueError):
        normalized = math.nan
    valid_range = normalized > 0 if strictly_positive else normalized >= 0
    if not math.isfinite(normalized) or not valid_range:
        raise ProxyConfigError(f"{name} must be a finite number {comparison}")


def validate_reconnect_delay_config(config):
    _validate_timing_value(
        config,
        "reconnect_delay",
        strictly_positive=False,
    )


def validate_udpsec_lifecycle_config(config):
    _validate_timing_value(
        config,
        "keepalive_interval",
        strictly_positive=True,
    )
    _validate_timing_value(
        config,
        "peer_timeout",
        strictly_positive=True,
    )
    _validate_timing_value(
        config,
        "session_refresh_interval",
        strictly_positive=False,
    )
    validate_reconnect_delay_config(config)


def validate_local_input_config(config):
    had_explicit_input = "input" in config
    try:
        input_config = normalize_local_input_config(config)
    except InputConfigError as exc:
        raise ProxyConfigError(str(exc)) from exc
    if had_explicit_input:
        config["input"] = input_config
    return input_config


def create_local_input_adapter(config, ingress_policy=None):
    input_config = validate_local_input_config(config)
    if input_config["type"] == SERIAL_INPUT_TYPE:
        try:
            return SerialInputAdapter(input_config)
        except InputConfigError as exc:
            raise ProxyConfigError(str(exc)) from exc
    return UdpInputAdapter.bind(input_config, ingress_policy)


def validate_output_config(config):
    try:
        output_config = normalize_output_config(config)
    except OutputConfigError as exc:
        raise ProxyConfigError(str(exc)) from exc
    config["output"] = output_config
    return output_config


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


def _require_key_file(path, *, description, error_type):
    if not _path_present(path):
        raise error_type(f"{description} is missing: {path}")
    if not path.exists() or not path.is_file():
        raise error_type(
            f"{description} path exists but is not a usable file: {path}. "
            "Refusing to replace operator key material."
        )


def _load_station_private_key(path):
    path = Path(path)
    _require_key_file(
        path,
        description="UDPSEC station private key",
        error_type=StationIdentityError,
    )
    try:
        private_key = load_private_key(path)
    except (OSError, TypeError, ValueError, UnsupportedAlgorithm) as exc:
        raise StationIdentityError(
            f"Unable to load UDPSEC station private key {path}: {exc}. "
            "Refusing to generate, repair, or replace operator key material."
        ) from exc
    if not isinstance(private_key, ec.EllipticCurvePrivateKey) or not isinstance(
        private_key.curve, ec.SECP256R1
    ):
        raise StationIdentityError(
            f"UDPSEC station private key must be an EC P-256 key: {path}. "
            "Refusing to generate, repair, or replace operator key material."
        )
    return private_key


def _load_station_public_key(path):
    path = Path(path)
    _require_key_file(
        path,
        description="UDPSEC station public key",
        error_type=StationIdentityError,
    )
    try:
        public_key = load_public_key(path)
    except (OSError, TypeError, ValueError, UnsupportedAlgorithm) as exc:
        raise StationIdentityError(
            f"Unable to load UDPSEC station public key {path}: {exc}. "
            "Refusing to repair or replace operator key material."
        ) from exc
    if not isinstance(public_key, ec.EllipticCurvePublicKey) or not isinstance(
        public_key.curve, ec.SECP256R1
    ):
        raise StationIdentityError(
            f"UDPSEC station public key must be an EC P-256 key: {path}. "
            "Refusing to repair or replace operator key material."
        )
    return public_key


def _load_canonical_station_identity(private_path, public_path, *, generated):
    private_key = _load_station_private_key(private_path)
    public_key = _load_station_public_key(public_path)
    if private_key.public_key().public_numbers() != public_key.public_numbers():
        raise StationIdentityError(
            "UDPSEC station public key does not match its private key: "
            f"{public_path}. Refusing to repair or replace operator key material."
        )
    return StationIdentity(
        private_path=private_path,
        public_path=public_path,
        generated=generated,
        private_key=private_key,
    )


def _incomplete_station_identity_error(existing_path, missing_path):
    return StationIdentityError(
        "UDPSEC station identity is incomplete: "
        f"found {existing_path}, but {missing_path} is missing. "
        "Refusing to generate, repair, or overwrite operator key material."
    )


def _inspect_canonical_station_identity(private_path, public_path):
    private_present = _path_present(private_path)
    public_present = _path_present(public_path)
    if private_present and not public_present:
        raise _incomplete_station_identity_error(private_path, public_path)
    if public_present and not private_present:
        raise _incomplete_station_identity_error(public_path, private_path)
    return private_present and public_present


def _ensure_canonical_station_identity():
    private_path = Path(CANONICAL_STATION_PRIVATE_KEY_PATH)
    public_path = Path(CANONICAL_STATION_PUBLIC_KEY_PATH)
    if _inspect_canonical_station_identity(private_path, public_path):
        return _load_canonical_station_identity(
            private_path,
            public_path,
            generated=False,
        )

    try:
        private_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        generate_key_pair(
            private_path.parent,
            private_path.name,
            public_path.name,
        )
    except (KeyFileExistsError, OSError) as exc:
        try:
            pair_exists = _inspect_canonical_station_identity(
                private_path,
                public_path,
            )
        except StationIdentityError:
            raise
        if not pair_exists:
            raise StationIdentityError(
                "Unable to generate the required UDPSEC station identity at "
                f"{private_path.parent}: {exc}"
            ) from exc
        return _load_canonical_station_identity(
            private_path,
            public_path,
            generated=False,
        )

    return _load_canonical_station_identity(
        private_path,
        public_path,
        generated=True,
    )


def ensure_station_identity(config):
    """Ensure or validate the identity required by one UDPSEC relation."""

    output_config = config.get("output")
    if not isinstance(output_config, dict) or output_config.get("type") not in {
        UDPSEC_OUTPUT_TYPE,
        UDP_OUTPUT_TYPE,
    }:
        raise ProxyConfigError(
            "station identity requires a normalized output configuration"
        )
    if output_config["type"] == UDP_OUTPUT_TYPE:
        return None

    station_private_path = Path(
        _configured_key_path(config, "station_private_key")
    )
    if _paths_match(
        station_private_path,
        CANONICAL_STATION_PRIVATE_KEY_PATH,
    ):
        return _ensure_canonical_station_identity()

    private_key = _load_station_private_key(station_private_path)
    return StationIdentity(
        private_path=station_private_path,
        public_path=None,
        generated=False,
        private_key=private_key,
    )


def load_peer_public_key(path):
    """Load configured aismixer trust without provisioning or mutation."""

    peer_path = Path(path)
    _require_key_file(
        peer_path,
        description="trusted aismixer public key",
        error_type=PeerTrustError,
    )
    try:
        public_key = load_public_key(peer_path)
    except (OSError, TypeError, ValueError, UnsupportedAlgorithm) as exc:
        raise PeerTrustError(
            f"Unable to load trusted aismixer public key {peer_path}: {exc}. "
            "Provision the correct peer trust explicitly; it is never generated "
            "or repaired automatically."
        ) from exc
    if not isinstance(public_key, ec.EllipticCurvePublicKey) or not isinstance(
        public_key.curve, ec.SECP256R1
    ):
        raise PeerTrustError(
            f"Trusted aismixer public key must be an EC P-256 key: {peer_path}. "
            "Provision the correct peer trust explicitly."
        )
    return public_key


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


def address_family_name(family):
    from output_adapters import address_family_name as _address_family_name
    return _address_family_name(family)


def family_for_ip_address(address):
    from output_adapters import family_for_ip_address as _family_for_ip_address
    return _family_for_ip_address(address)


def default_remote_family(host):
    from output_adapters import default_remote_family as _default_remote_family
    return _default_remote_family(host)


def parse_source_ip(config):
    try:
        return adapter_parse_source_ip(config, context="source_ip")
    except OutputConfigError as exc:
        raise ProxyConfigError(str(exc)) from exc


def compile_local_ingress_policy(config):
    return compile_ingress_policy(config, context="nmea_sproxy")


def resolve_remote_endpoint(config, source_address=None):
    output_config = {
        "type": UDPSEC_OUTPUT_TYPE,
        "host": config["remote_host"],
        "port": config["remote_port"],
        "legacy": True,
    }
    if source_address is not None:
        output_config["source_ip"] = str(source_address)
    elif "source_ip" in config:
        output_config["source_ip"] = config["source_ip"]
    try:
        return resolve_output_endpoint(output_config)
    except OutputConfigError as exc:
        raise ProxyConfigError(str(exc)) from exc


def resolve_remote_addr(host, port, family):
    try:
        return adapter_resolve_remote_addr(
            host,
            port,
            family,
            context="remote_host",
        )
    except OutputConfigError as exc:
        raise ProxyConfigError(str(exc)) from exc


def create_outbound_socket(family, source_address=None):
    try:
        return adapter_create_outbound_socket(
            family,
            source_address,
            context="source_ip",
        )
    except OutputConfigError as exc:
        raise ProxyConfigError(str(exc)) from exc


def handle_server_packet(
    data,
    addr,
    remote_addr,
    server_to_client_key,
    station_id,
    expected_ping_seq,
):
    if not remote_addresses_match(addr, remote_addr):
        return SERVER_PACKET_IGNORED

    try:
        message = decrypt_secure_json_message(data, server_to_client_key)
    except Exception:
        return SERVER_PACKET_IGNORED

    if not isinstance(message, dict):
        return SERVER_PACKET_IGNORED
    if is_session_close_message(message, station_id):
        return SERVER_PACKET_PEER_CLOSE
    if is_matching_pong_message(
        message,
        station_id,
        expected_ping_seq,
    ):
        return SERVER_PACKET_AUTHENTICATED
    return SERVER_PACKET_IGNORED


def session_expiration_reason(
    now,
    session_started_at,
    last_authenticated_peer,
    config,
):
    if now >= last_authenticated_peer + float(config["peer_timeout"]):
        return SESSION_END_PEER_TIMEOUT
    refresh_interval = float(config["session_refresh_interval"])
    if refresh_interval > 0 and now >= session_started_at + refresh_interval:
        return SESSION_END_PLANNED_REFRESH
    return None


def session_deadline_action(
    now,
    session_started_at,
    last_authenticated_peer,
    last_ping_at,
    expected_ping_seq,
    config,
):
    expiration_reason = session_expiration_reason(
        now,
        session_started_at,
        last_authenticated_peer,
        config,
    )
    if expiration_reason is not None:
        return expiration_reason
    if now >= last_ping_at + float(config["keepalive_interval"]):
        if expected_ping_seq is not None:
            return SESSION_END_PROACTIVE_REKEY
        return SESSION_ACTION_SEND_PING
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
    if reason in (
        SESSION_END_PLANNED_REFRESH,
        SESSION_END_PROACTIVE_REKEY,
    ):
        return None
    return config["reconnect_delay"]


def _coerce_input_adapter(local_input, ingress_policy=None):
    adapter_methods = (
        "selectable_sockets",
        "poll_interval",
        "read_ready",
        "read_pending",
    )
    if all(hasattr(local_input, method) for method in adapter_methods):
        return local_input
    return UdpInputAdapter(local_input, ingress_policy)


HEARTBEAT_INTERVAL_SECONDS = 60.0


class ForwardingStats:
    """Own relation-lifetime forwarding counters and heartbeat scheduling.

    Both the cumulative counters and the heartbeat deadline belong to the
    running relation/process, not to one secure session or one plain-output
    socket incarnation, so they must persist unchanged across successive
    forward_loop()/plain_udp_forward_loop() invocations (UDPSEC
    reconnect/rekey, plain-UDP output-socket recreation).
    """

    __slots__ = ("_messages", "_bytes", "_next_heartbeat_at")

    def __init__(self):
        self._messages = 0
        self._bytes = 0
        self._next_heartbeat_at = None

    def record_forwarded(self, sentence):
        self._messages += 1
        self._bytes += len(sentence.encode("utf-8"))

    @property
    def messages(self):
        return self._messages

    @property
    def bytes(self):
        return self._bytes

    def is_heartbeat_due(self, now):
        """Return whether the relation-level heartbeat deadline has passed.

        The first call for a fresh instance only establishes the initial
        deadline and never fires immediately.
        """
        if self._next_heartbeat_at is None:
            self._next_heartbeat_at = now + HEARTBEAT_INTERVAL_SECONDS
            return False
        return now >= self._next_heartbeat_at

    def reschedule_heartbeat(self, now):
        self._next_heartbeat_at = now + HEARTBEAT_INTERVAL_SECONDS

    def heartbeat_remaining(self, now):
        """Return the non-negative seconds until the next heartbeat."""
        if self._next_heartbeat_at is None:
            return HEARTBEAT_INTERVAL_SECONDS
        return max(0.0, self._next_heartbeat_at - now)


def _format_bytes_compact(byte_count):
    """Render a byte count compactly for the sparse runtime heartbeat."""

    value = float(byte_count)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024.0:
            return f"{byte_count}B" if unit == "B" else f"{value:.2f}{unit}"
        value /= 1024.0
    return f"{value:.2f}TiB"


def print_forwarding_heartbeat(input_adapter, output_label, stats, *, session_up=None):
    """Print a sparse, traffic-rate-independent forwarding summary.

    Sourced entirely from existing adapter/stats state; performs no
    separate accounting of its own.
    """
    input_label = "serial" if isinstance(input_adapter, SerialInputAdapter) else "udp"
    line = (
        f"Runtime: input={input_label} output={output_label} "
        f"forwarded={stats.messages} messages / "
        f"{_format_bytes_compact(stats.bytes)}"
    )
    if session_up is not None:
        line += f" session={'up' if session_up else 'down'}"
    print(line)


def iter_forwardable_nmea_sentences(data):
    return extract_nmea_sentences(data.decode(errors="replace").strip())


def send_udpsec_nmea_sentence(
    clean_line,
    out_sock,
    config,
    client_to_server_key,
    remote_addr,
):
    json_obj = {
        "type": "nmea",
        "payload": clean_line,
        "timestamp": int(time.time()),
        "source_id": config["station_id"],
    }
    out_sock.sendto(
        encrypt_secure_json_message(json_obj, client_to_server_key),
        remote_addr,
    )


def forward_input_payload(data, send_sentence, stats=None):
    for clean_line in iter_forwardable_nmea_sentences(data):
        if not clean_line:
            continue
        send_sentence(clean_line)
        if stats is not None:
            stats.record_forwarded(clean_line)


def forward_pending_input(input_adapter, send_sentence, stats=None):
    for data in input_adapter.read_pending():
        forward_input_payload(data, send_sentence, stats)


def send_ping(sock, remote_addr, client_to_server_key, station_id, seq):
    message = build_ping_message(station_id, seq, int(time.time()))
    sock.sendto(
        encrypt_secure_json_message(message, client_to_server_key),
        remote_addr,
    )


def send_session_close(
    sock,
    remote_addr,
    client_to_server_key,
    station_id,
):
    message = build_session_close_message(
        station_id,
        int(time.time()),
    )
    sock.sendto(
        encrypt_secure_json_message(message, client_to_server_key),
        remote_addr,
    )


def perform_handshake(
    sock,
    config,
    station_identity_private_key,
    server_identity_public_key,
    remote_addr,
):
    station_id = config["station_id"]
    timestamp = int(time.time())
    client_random = os.urandom(32)
    client_ephemeral_private_key = generate_ephemeral_private_key()
    client_ephemeral_public_key = serialize_ephemeral_public_key(
        client_ephemeral_private_key.public_key()
    )
    client_digest = build_client_auth_digest(
        station_id=station_id,
        timestamp=timestamp,
        client_random=client_random,
        client_ephemeral_public_key=client_ephemeral_public_key,
    )
    client_signature = sign_transcript_digest(
        station_identity_private_key,
        client_digest,
    )
    client_hello = ClientHello(
        station_id=station_id,
        timestamp=timestamp,
        client_random=client_random,
        client_ephemeral_public_key=client_ephemeral_public_key,
        client_signature=client_signature,
    )
    packet = build_client_hello_packet(client_hello)
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

            try:
                server_hello = parse_server_hello_packet(response)
                server_ephemeral_public_key = parse_ephemeral_public_key(
                    server_hello.server_ephemeral_public_key
                )
            except (TypeError, ValueError) as e:
                print(f"⚠️ Invalid handshake response format: {e}")
                continue

            server_digest = build_server_auth_digest(
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
            if not verify_transcript_signature(
                server_identity_public_key,
                server_hello.server_signature,
                server_digest,
            ):
                print("❌ Server signature verification failed.")
                continue

            shared_secret = derive_ephemeral_shared_secret(
                client_ephemeral_private_key,
                server_ephemeral_public_key,
            )
            session_transcript_hash = build_session_transcript_hash(
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
            session_key_material = derive_session_key_material(
                shared_secret,
                session_transcript_hash,
            )
            try:
                send_ping(
                    sock,
                    remote_addr,
                    session_key_material.client_to_server_key,
                    station_id,
                    SESSION_CONFIRMATION_SEQUENCE,
                )
            except OSError as e:
                print(f"❌ Session confirmation send error: {e}")
                return None

            confirmation_deadline = time.monotonic() + handshake_timeout
            while True:
                remaining = confirmation_deadline - time.monotonic()
                if remaining <= 0:
                    print("⚠️ No session confirmation from server.")
                    return None
                if settimeout:
                    settimeout(remaining)

                try:
                    confirmation, confirmation_addr = sock.recvfrom(8192)
                except socket.timeout:
                    print("⚠️ No session confirmation from server.")
                    return None
                except ConnectionResetError as e:
                    print(
                        "❌ Connection reset during session confirmation: "
                        f"{e}"
                    )
                    return None
                except OSError as e:
                    print(f"❌ Session confirmation receive error: {e}")
                    return None

                if not remote_addresses_match(
                    confirmation_addr,
                    remote_addr,
                ):
                    continue

                confirmation_result = handle_server_packet(
                    confirmation,
                    confirmation_addr,
                    remote_addr,
                    session_key_material.server_to_client_key,
                    station_id,
                    SESSION_CONFIRMATION_SEQUENCE,
                )
                if confirmation_result == SERVER_PACKET_IGNORED:
                    continue
                if confirmation_result != SERVER_PACKET_AUTHENTICATED:
                    print("❌ Invalid secure session confirmation.")
                    return None

                print("Mutual ECDHE session confirmed.")
                return session_key_material
    finally:
        if settimeout:
            settimeout(original_timeout)


def forward_loop(
    local_input,
    out_sock,
    config,
    session_key_material,
    remote_addr,
    ingress_policy=None,
    stats=None,
):
    """Run one authenticated forwarding session.

    ``stats`` should be the caller-owned ForwardingStats for the running
    relation so cumulative counters survive reconnect/rekey across
    successive calls; a fresh instance is created only when none is given.
    """
    input_adapter = _coerce_input_adapter(local_input, ingress_policy)
    client_to_server_key = session_key_material.client_to_server_key
    server_to_client_key = session_key_material.server_to_client_key
    session_started_at = time.monotonic()
    last_authenticated_peer = session_started_at
    last_ping_at = session_started_at
    expected_ping_seq = None
    next_ping_seq = 1
    stats = ForwardingStats() if stats is None else stats
    send_sentence = lambda clean_line: send_udpsec_nmea_sentence(
        clean_line,
        out_sock,
        config,
        client_to_server_key,
        remote_addr,
    )

    def apply_due_deadline(now):
        nonlocal expected_ping_seq, last_ping_at, next_ping_seq

        action = session_deadline_action(
            now,
            session_started_at,
            last_authenticated_peer,
            last_ping_at,
            expected_ping_seq,
            config,
        )
        if action == SESSION_ACTION_SEND_PING:
            try:
                send_ping(
                    out_sock,
                    remote_addr,
                    client_to_server_key,
                    config["station_id"],
                    next_ping_seq,
                )
            except Exception as e:
                print(f"❌ Secure ping error: {e}")
                return SESSION_END_SOCKET_ERROR
            expected_ping_seq = next_ping_seq
            next_ping_seq += 1
            last_ping_at = now
            return None
        if action == SESSION_END_PROACTIVE_REKEY:
            print(
                "Secure session liveness unresolved; "
                "starting authenticated re-handshake."
            )
            return action
        if action == SESSION_END_PLANNED_REFRESH:
            print("Secure session planned refresh due.")
            return action
        if action is not None:
            print(f"Secure session invalidated: {action}")
        return action

    while True:
        now = time.monotonic()
        deadline_reason = apply_due_deadline(now)
        if deadline_reason is not None:
            return deadline_reason

        if stats.is_heartbeat_due(now):
            print_forwarding_heartbeat(
                input_adapter,
                UDPSEC_OUTPUT_TYPE,
                stats,
                session_up=(
                    now < last_authenticated_peer + float(config["peer_timeout"])
                ),
            )
            stats.reschedule_heartbeat(now)

        try:
            forward_pending_input(
                input_adapter,
                send_sentence,
                stats,
            )
        except Exception as e:
            print(f"❌ Forwarding error: {e}")
            return SESSION_END_SOCKET_ERROR

        input_sockets = input_adapter.selectable_sockets()
        input_poll_interval = input_adapter.poll_interval()
        readable_sockets = input_sockets + [out_sock]

        now = time.monotonic()
        deadline_reason = apply_due_deadline(now)
        if deadline_reason is not None:
            return deadline_reason
        poll_timeout = session_poll_timeout(
            now,
            session_started_at,
            last_authenticated_peer,
            last_ping_at,
            config,
        )
        if input_poll_interval is not None:
            poll_timeout = min(poll_timeout, input_poll_interval)
        # Bound the wait so the heartbeat cadence is explicit rather than
        # merely incidental to keepalive/session wakeups.
        poll_timeout = min(poll_timeout, stats.heartbeat_remaining(now))

        try:
            readable, _, _ = select.select(
                readable_sockets, [], [], poll_timeout
            )
        except Exception as e:
            print(f"❌ Forwarding error: {e}")
            return SESSION_END_SOCKET_ERROR

        now = time.monotonic()
        deadline_reason = apply_due_deadline(now)
        if deadline_reason is not None:
            return deadline_reason

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
                server_to_client_key,
                config["station_id"],
                expected_ping_seq,
            )
            now = time.monotonic()
            deadline_reason = apply_due_deadline(now)
            if deadline_reason is not None:
                return deadline_reason
            if result == SERVER_PACKET_AUTHENTICATED:
                last_authenticated_peer = now
                expected_ping_seq = None
            elif result == SERVER_PACKET_PEER_CLOSE:
                print("Secure peer closed the current session gracefully.")
                return SESSION_END_PEER_GRACEFUL_CLOSE

        for ready_socket in input_sockets:
            if ready_socket not in readable:
                continue
            try:
                for data in input_adapter.read_ready(ready_socket):
                    forward_input_payload(
                        data,
                        send_sentence,
                        stats,
                    )
            except Exception as e:
                print(f"❌ Forwarding error: {e}")
                return SESSION_END_SOCKET_ERROR

def plain_udp_forward_loop(local_input, output_adapter, ingress_policy=None, stats=None):
    """Run one plain UDP forwarding pass over the current output socket.

    ``stats`` should be the caller-owned ForwardingStats for the running
    relation so cumulative counters survive output-socket recreation across
    successive calls; a fresh instance is created only when none is given.
    """
    input_adapter = _coerce_input_adapter(local_input, ingress_policy)
    stats = ForwardingStats() if stats is None else stats

    while True:
        now = time.monotonic()
        if stats.is_heartbeat_due(now):
            print_forwarding_heartbeat(input_adapter, UDP_OUTPUT_TYPE, stats)
            stats.reschedule_heartbeat(now)

        send_sentence = output_adapter.send_sentence
        try:
            forward_pending_input(input_adapter, send_sentence, stats)
        except Exception as e:
            print(f"❌ Plain UDP forwarding error: {e}")
            return SESSION_END_SOCKET_ERROR

        # Recompute monotonic time here rather than reusing the value
        # captured before forward_pending_input(), which may itself take a
        # non-trivial amount of time.
        now = time.monotonic()
        input_sockets = input_adapter.selectable_sockets()
        poll_timeout = input_adapter.poll_interval()
        # Bound an unset (None) or overlong adapter poll interval so a
        # quiet relation still wakes up in time for its own heartbeat,
        # rather than blocking indefinitely on select()/sleep().
        heartbeat_remaining = stats.heartbeat_remaining(now)
        bounded_timeout = (
            heartbeat_remaining
            if poll_timeout is None
            else min(poll_timeout, heartbeat_remaining)
        )

        if not input_sockets:
            time.sleep(bounded_timeout)
            continue

        try:
            readable, _, _ = select.select(
                input_sockets, [], [], bounded_timeout
            )
        except Exception as e:
            print(f"❌ Plain UDP forwarding error: {e}")
            return SESSION_END_SOCKET_ERROR

        for ready_socket in input_sockets:
            if ready_socket not in readable:
                continue
            try:
                for data in input_adapter.read_ready(ready_socket):
                    forward_input_payload(data, send_sentence, stats)
            except Exception as e:
                print(f"❌ Plain UDP forwarding error: {e}")
                return SESSION_END_SOCKET_ERROR


def create_plain_udp_output_adapter(output_config):
    try:
        return PlainUdpOutputAdapter.from_config(output_config)
    except OutputConfigError as exc:
        raise ProxyConfigError(str(exc)) from exc


def run_plain_udp_relation(
    local_input,
    output_config,
    config,
    ingress_policy=None,
    output_adapter=None,
    stats=None,
):
    """Run the plain UDP relation for the process lifetime, recreating the
    output socket as needed.

    One ForwardingStats instance is owned here for the whole relation, so
    its cumulative counters survive output-socket recreation across
    successive plain_udp_forward_loop() calls.
    """
    if output_adapter is None:
        try:
            output_adapter = create_plain_udp_output_adapter(output_config)
        except ProxyConfigError as exc:
            print(f"Configuration error: {exc}", file=sys.stderr)
            return 1
    stats = ForwardingStats() if stats is None else stats
    try:
        while True:
            reason = plain_udp_forward_loop(
                local_input,
                output_adapter,
                ingress_policy,
                stats,
            )
            output_adapter.close()

            retry_delay = retry_delay_for_reason(reason, config)
            print(f"🔁 Recreating plain UDP socket in {retry_delay} seconds...")
            time.sleep(retry_delay)

            try:
                output_adapter.recreate_socket()
            except OutputConfigError as exc:
                print(f"Configuration error: {exc}", file=sys.stderr)
                return 1
    finally:
        output_adapter.close()


def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Forward one local AIS input (UDP or serial) to one network "
            "output (UDPSEC or UDP)."
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

    try:
        config = load_config(config_path)
        input_config = validate_local_input_config(config)
        output_config = config["output"]
        if input_config["type"] == UDP_INPUT_TYPE:
            ingress_policy = compile_local_ingress_policy(input_config)
        else:
            ingress_policy = NetworkPolicy.unrestricted()
    except (NetworkPolicyConfigError, ProxyConfigError) as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1

    if output_config["type"] == UDPSEC_OUTPUT_TYPE:
        try:
            remote_addr, out_family = resolve_output_endpoint(output_config)
        except OutputConfigError as e:
            print(f"Configuration error: {e}", file=sys.stderr)
            return 1

        try:
            station_identity = ensure_station_identity(config)
            if station_identity.generated:
                print(
                    "[+] Generated UDPSEC station identity: "
                    f"{station_identity.public_path}"
                )
            server_identity_public_key = load_peer_public_key(
                config["remote_public_key"]
            )
        except (StationIdentityError, PeerTrustError) as e:
            print(f"Configuration error: {e}", file=sys.stderr)
            return 1
        station_identity_private_key = station_identity.private_key
        try:
            out_sock = create_output_socket(output_config, out_family)
        except OutputConfigError as e:
            print(f"Configuration error: {e}", file=sys.stderr)
            return 1
        out_sock.settimeout(5.0)
    else:
        try:
            plain_output = create_plain_udp_output_adapter(output_config)
        except ProxyConfigError as e:
            print(f"Configuration error: {e}", file=sys.stderr)
            return 1

    try:
        local_input = create_local_input_adapter(config, ingress_policy)
    except (OSError, ProxyConfigError) as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        if output_config["type"] == UDPSEC_OUTPUT_TYPE:
            out_sock.close()
        else:
            plain_output.close()
        return 1

    if input_config["type"] == SERIAL_INPUT_TYPE:
        print(
            f"📡 Reading serial input from {input_config['port']} "
            f"at {input_config['baudrate']} baud"
        )
    else:
        print(
            f"📡 Listening on UDP "
            f"{input_config['listen_ip']}:{input_config['listen_port']}"
        )

    if output_config["type"] == UDP_OUTPUT_TYPE:
        print(
            f"📤 Forwarding plain UDP packets to "
            f"{output_config['host']}:{output_config['port']}"
        )
    else:
        print(
            f"📤 Forwarding encrypted packets to "
            f"{output_config['host']}:{output_config['port']}"
        )

    active_session_key_material = None
    # One ForwardingStats instance is owned for the whole process/relation
    # lifetime here, so cumulative counters survive UDPSEC reconnect/rekey
    # and plain-UDP output-socket recreation across successive forwarding
    # loop invocations.
    stats = ForwardingStats()
    local_input.start()
    try:
        if output_config["type"] == UDP_OUTPUT_TYPE:
            return run_plain_udp_relation(
                local_input,
                output_config,
                config,
                ingress_policy,
                output_adapter=plain_output,
                stats=stats,
            )

        while True:
            session_key_material = perform_handshake(
                out_sock,
                config,
                station_identity_private_key,
                server_identity_public_key,
                remote_addr,
            )
            if session_key_material:
                active_session_key_material = session_key_material
                reason = forward_loop(
                    local_input,
                    out_sock,
                    config,
                    session_key_material,
                    remote_addr,
                    ingress_policy,
                    stats,
                )
                if reason == SESSION_END_PEER_GRACEFUL_CLOSE:
                    active_session_key_material = None
            else:
                reason = HANDSHAKE_FAILURE

            retry_delay = retry_delay_for_reason(reason, config)
            if retry_delay is None:
                print("Refreshing secure session immediately.")
                continue

            print(f"🔁 Retrying in {retry_delay} seconds...")
            time.sleep(retry_delay)
    finally:
        try:
            if (
                output_config["type"] == UDPSEC_OUTPUT_TYPE
                and active_session_key_material is not None
            ):
                try:
                    send_session_close(
                        out_sock,
                        remote_addr,
                        active_session_key_material.client_to_server_key,
                        config["station_id"],
                    )
                except Exception as exc:
                    print(
                        "Best-effort secure close failed: "
                        f"{type(exc).__name__}: {exc}"
                    )
        finally:
            try:
                local_input.close()
            finally:
                if output_config["type"] == UDPSEC_OUTPUT_TYPE:
                    out_sock.close()


def _raise_keyboard_interrupt_for_sigterm(_signum, _frame):
    raise KeyboardInterrupt


def run_service(argv=None):
    previous_sigterm = signal.signal(
        signal.SIGTERM,
        _raise_keyboard_interrupt_for_sigterm,
    )
    try:
        return main(argv)
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)


if __name__ == "__main__":
    try:
        raise SystemExit(run_service())
    except KeyboardInterrupt:
        print("👋 Exit by user.")
