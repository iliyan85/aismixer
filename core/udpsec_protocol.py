"""Canonical wire values, codecs, and control validation for UDPSEC.

UDPSEC protocol revision 2. There is no capability negotiation and no
downgrade: exactly one protocol version is ever accepted, and any other
value fails closed at parse time. Backward compatibility with revision 1 is
not provided -- both `aismixer` and `nmea_sproxy` are upgraded together.

ClientHello and ServerHello packets use pipe-delimited ASCII framing with
strict UTF-8 for the station identifier, canonical unsigned decimal for the
protocol version and timestamp, and canonical standard base64 for binary
fields. DATA packets use a fixed-offset binary framing carrying a
server-minted opaque session locator ahead of the AEAD nonce and
ciphertext. Encrypted ping and pong JSON objects use the shared structural
helpers below. This module is transport-neutral and deliberately performs
no signing, verification, ECDHE, HKDF, encryption, or elliptic-curve point
validation.

The session locator is a tuple-independent lookup hint, not a credential:
knowledge of a locator selects which session/epoch a packet might belong
to, but it is cryptographically bound into every DATA packet's AEAD
associated data (see `build_data_aad`) and can never by itself authenticate
a packet, satisfy `allow_from`, or authorize a change of active transport
path.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field


CLIENT_HELLO_PREFIX = b"NMEA-H"
SERVER_HELLO_PREFIX = b"OK"
DATA_PREFIX = b"NMEA-D2"
SESSION_CONFIRMATION_SEQUENCE = 0
SESSION_CLOSE_TYPE = "close"
SESSION_CLOSE_REASON_SHUTDOWN = "shutdown"

UDPSEC_PROTOCOL_VERSION = 2
SESSION_LOCATOR_BYTES = 16

_MAX_TIMESTAMP = (1 << 64) - 1
_MAX_TIMESTAMP_ASCII = str(_MAX_TIMESTAMP).encode("ascii")
_MAX_PROTOCOL_VERSION = 255
_MAX_PROTOCOL_VERSION_ASCII = str(_MAX_PROTOCOL_VERSION).encode("ascii")
_RANDOM_LENGTH = 32
_EPHEMERAL_PUBLIC_KEY_LENGTH = 33
_COMPRESSED_POINT_PREFIXES = (0x02, 0x03)
_DATA_NONCE_LENGTH = 12
_DATA_MIN_TAG_LENGTH = 16

__all__ = (
    "CLIENT_HELLO_PREFIX",
    "ClientHello",
    "DATA_PREFIX",
    "SERVER_HELLO_PREFIX",
    "SESSION_CLOSE_REASON_SHUTDOWN",
    "SESSION_CLOSE_TYPE",
    "SESSION_CONFIRMATION_SEQUENCE",
    "SESSION_LOCATOR_BYTES",
    "ServerHello",
    "UDPSEC_PROTOCOL_VERSION",
    "build_client_hello_packet",
    "build_data_aad",
    "build_data_packet",
    "build_ping_message",
    "build_pong_message",
    "build_session_close_message",
    "build_server_hello_packet",
    "is_matching_pong_message",
    "is_ping_message",
    "is_session_close_message",
    "parse_client_hello_packet",
    "parse_data_packet",
    "parse_server_hello_packet",
)


@dataclass(frozen=True, slots=True)
class ClientHello:
    """Immutable fields carried by one canonical UDPSEC ClientHello."""

    protocol_version: int
    station_id: str
    timestamp: int
    client_random: bytes
    client_ephemeral_public_key: bytes
    client_signature: bytes = field(repr=False)

    def __post_init__(self) -> None:
        _validate_protocol_version(self.protocol_version)
        _station_id_bytes(self.station_id)
        _validate_timestamp(self.timestamp)
        _validate_random("client_random", self.client_random)
        _validate_ephemeral_public_key(
            "client_ephemeral_public_key",
            self.client_ephemeral_public_key,
        )
        _validate_signature("client_signature", self.client_signature)


@dataclass(frozen=True, slots=True)
class ServerHello:
    """Immutable fields carried by one canonical UDPSEC ServerHello."""

    protocol_version: int
    session_locator: bytes
    server_random: bytes
    server_ephemeral_public_key: bytes
    server_signature: bytes = field(repr=False)

    def __post_init__(self) -> None:
        _validate_protocol_version(self.protocol_version)
        _validate_session_locator(self.session_locator)
        _validate_random("server_random", self.server_random)
        _validate_ephemeral_public_key(
            "server_ephemeral_public_key",
            self.server_ephemeral_public_key,
        )
        _validate_signature("server_signature", self.server_signature)


def _validate_protocol_version(version: object) -> None:
    if isinstance(version, bool) or not isinstance(version, int):
        raise TypeError("protocol_version must be an integer")
    if version != UDPSEC_PROTOCOL_VERSION:
        raise ValueError(f"unsupported UDPSEC protocol version: {version}")


def _validate_session_locator(value: object) -> None:
    normalized = _require_immutable_bytes("session_locator", value)
    if len(normalized) != SESSION_LOCATOR_BYTES:
        raise ValueError(
            f"session_locator must be exactly {SESSION_LOCATOR_BYTES} bytes"
        )


def _station_id_bytes(station_id: object) -> bytes:
    if not isinstance(station_id, str):
        raise TypeError("station_id must be a string")
    if station_id == "":
        raise ValueError("station_id must not be empty")
    if "|" in station_id:
        raise ValueError("station_id must not contain '|'")
    try:
        return station_id.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("station_id must be UTF-8 encodable") from exc


def _validate_timestamp(timestamp: object) -> None:
    if isinstance(timestamp, bool) or not isinstance(timestamp, int):
        raise TypeError("timestamp must be an integer")
    if not 0 <= timestamp <= _MAX_TIMESTAMP:
        raise ValueError("timestamp must fit in an unsigned 64-bit integer")


def _validate_liveness_timestamp(timestamp: object) -> None:
    if type(timestamp) is not int:
        raise TypeError("timestamp must be an integer")


def _validate_liveness_sequence(sequence: object) -> None:
    if type(sequence) is not int:
        raise TypeError("sequence must be an integer")
    if sequence < SESSION_CONFIRMATION_SEQUENCE:
        raise ValueError("sequence must be zero or greater")


def _build_liveness_message(
    message_type: str,
    station_id: str,
    sequence: int,
    timestamp: int,
) -> dict:
    _station_id_bytes(station_id)
    _validate_liveness_sequence(sequence)
    _validate_liveness_timestamp(timestamp)
    return {
        "type": message_type,
        "seq": sequence,
        "timestamp": timestamp,
        "source_id": station_id,
    }


def build_ping_message(
    station_id: str,
    sequence: int,
    timestamp: int,
) -> dict:
    """Build one encrypted UDPSEC liveness ping message."""

    return _build_liveness_message("ping", station_id, sequence, timestamp)


def build_pong_message(
    station_id: str,
    sequence: int,
    timestamp: int,
) -> dict:
    """Build one encrypted UDPSEC liveness pong message."""

    return _build_liveness_message("pong", station_id, sequence, timestamp)


def _is_liveness_message(
    message: object,
    station_id: str,
    *,
    message_type: str,
) -> bool:
    if not isinstance(message, dict):
        return False
    if not {"type", "seq", "timestamp", "source_id"} <= message.keys():
        return False
    return (
        message.get("type") == message_type
        and message.get("source_id") == station_id
        and type(message.get("seq")) is int
        and type(message.get("timestamp")) is int
    )


def is_ping_message(
    message: object,
    station_id: str,
    *,
    confirmation: bool = False,
) -> bool:
    """Return whether an authenticated JSON value is a valid UDPSEC ping."""

    if not _is_liveness_message(message, station_id, message_type="ping"):
        return False
    sequence = message["seq"]
    if confirmation:
        return sequence == SESSION_CONFIRMATION_SEQUENCE
    return sequence > SESSION_CONFIRMATION_SEQUENCE


def is_matching_pong_message(
    message: object,
    station_id: str,
    expected_sequence: object,
) -> bool:
    """Return whether a UDPSEC pong matches the currently outstanding ping."""

    if (
        type(expected_sequence) is not int
        or expected_sequence < SESSION_CONFIRMATION_SEQUENCE
    ):
        return False
    return (
        _is_liveness_message(message, station_id, message_type="pong")
        and message["seq"] == expected_sequence
    )


def build_session_close_message(station_id: str, timestamp: int) -> dict:
    """Build the canonical encrypted UDPSEC graceful-close message."""

    _station_id_bytes(station_id)
    _validate_timestamp(timestamp)
    return {
        "type": SESSION_CLOSE_TYPE,
        "reason": SESSION_CLOSE_REASON_SHUTDOWN,
        "timestamp": timestamp,
        "source_id": station_id,
    }


def is_session_close_message(message: object, station_id: str) -> bool:
    """Return whether an authenticated JSON value is a canonical close."""

    if not isinstance(message, dict):
        return False
    if set(message) != {"type", "reason", "timestamp", "source_id"}:
        return False
    timestamp = message.get("timestamp")
    return (
        message.get("type") == SESSION_CLOSE_TYPE
        and message.get("reason") == SESSION_CLOSE_REASON_SHUTDOWN
        and message.get("source_id") == station_id
        and isinstance(timestamp, int)
        and not isinstance(timestamp, bool)
        and 0 <= timestamp <= _MAX_TIMESTAMP
    )


def _require_immutable_bytes(name: str, value: object) -> bytes:
    if not isinstance(value, bytes):
        raise TypeError(f"{name} must be bytes")
    return value


def _validate_random(name: str, value: object) -> None:
    normalized = _require_immutable_bytes(name, value)
    if len(normalized) != _RANDOM_LENGTH:
        raise ValueError(f"{name} must be exactly 32 bytes")


def _validate_ephemeral_public_key(name: str, value: object) -> None:
    normalized = _require_immutable_bytes(name, value)
    if len(normalized) != _EPHEMERAL_PUBLIC_KEY_LENGTH:
        raise ValueError(f"{name} must be exactly 33 bytes")
    if normalized[0] not in _COMPRESSED_POINT_PREFIXES:
        raise ValueError(f"{name} must start with 0x02 or 0x03")


def _validate_signature(name: str, value: object) -> None:
    normalized = _require_immutable_bytes(name, value)
    if not normalized:
        raise ValueError(f"{name} must not be empty")


def _encode_base64(value: bytes) -> bytes:
    return base64.b64encode(value)


def _decode_base64(name: str, encoded: bytes) -> bytes:
    if not encoded:
        raise ValueError(f"{name} base64 field must not be empty")
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError(
            f"{name} must use canonical standard base64"
        ) from None
    if base64.b64encode(decoded) != encoded:
        raise ValueError(f"{name} must use canonical standard base64")
    return decoded


def _parse_canonical_ascii_decimal(
    name: str,
    encoded: bytes,
    max_ascii: bytes,
) -> int:
    if encoded == b"0":
        return 0
    if (
        not encoded
        or not 0x31 <= encoded[0] <= 0x39
        or any(not 0x30 <= value <= 0x39 for value in encoded[1:])
    ):
        raise ValueError(f"{name} must use canonical unsigned ASCII decimal")
    if len(encoded) > len(max_ascii) or (
        len(encoded) == len(max_ascii) and encoded > max_ascii
    ):
        raise ValueError(f"{name} exceeds the supported range")

    value = int(encoded)
    if str(value).encode("ascii") != encoded:
        raise ValueError(f"{name} must use canonical unsigned ASCII decimal")
    return value


def _parse_timestamp(encoded: bytes) -> int:
    return _parse_canonical_ascii_decimal(
        "timestamp", encoded, _MAX_TIMESTAMP_ASCII
    )


def _parse_protocol_version(encoded: bytes) -> int:
    version = _parse_canonical_ascii_decimal(
        "protocol version", encoded, _MAX_PROTOCOL_VERSION_ASCII
    )
    _validate_protocol_version(version)
    return version


def _split_packet(
    packet: object,
    *,
    prefix: bytes,
    field_count: int,
    packet_name: str,
) -> list[bytes]:
    if not isinstance(packet, bytes):
        raise TypeError(f"{packet_name} packet must be bytes")
    fields = packet.split(b"|")
    if len(fields) != field_count or fields[0] != prefix:
        raise ValueError(f"invalid {packet_name} packet format")
    return fields


def build_client_hello_packet(client_hello: ClientHello) -> bytes:
    """Encode one ClientHello as canonical UDPSEC handshake bytes."""

    if not isinstance(client_hello, ClientHello):
        raise TypeError("client_hello must be a ClientHello")
    return b"|".join(
        (
            CLIENT_HELLO_PREFIX,
            str(client_hello.protocol_version).encode("ascii"),
            _station_id_bytes(client_hello.station_id),
            str(client_hello.timestamp).encode("ascii"),
            _encode_base64(client_hello.client_random),
            _encode_base64(client_hello.client_ephemeral_public_key),
            _encode_base64(client_hello.client_signature),
        )
    )


def parse_client_hello_packet(packet: bytes) -> ClientHello:
    """Parse and structurally validate one canonical ClientHello packet."""

    fields = _split_packet(
        packet,
        prefix=CLIENT_HELLO_PREFIX,
        field_count=7,
        packet_name="ClientHello",
    )
    protocol_version = _parse_protocol_version(fields[1])
    try:
        station_id = fields[2].decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("station_id must use valid UTF-8") from None

    return ClientHello(
        protocol_version=protocol_version,
        station_id=station_id,
        timestamp=_parse_timestamp(fields[3]),
        client_random=_decode_base64("client_random", fields[4]),
        client_ephemeral_public_key=_decode_base64(
            "client_ephemeral_public_key",
            fields[5],
        ),
        client_signature=_decode_base64(
            "client_signature",
            fields[6],
        ),
    )


def build_server_hello_packet(server_hello: ServerHello) -> bytes:
    """Encode one ServerHello as canonical UDPSEC handshake bytes."""

    if not isinstance(server_hello, ServerHello):
        raise TypeError("server_hello must be a ServerHello")
    return b"|".join(
        (
            SERVER_HELLO_PREFIX,
            str(server_hello.protocol_version).encode("ascii"),
            _encode_base64(server_hello.session_locator),
            _encode_base64(server_hello.server_random),
            _encode_base64(server_hello.server_ephemeral_public_key),
            _encode_base64(server_hello.server_signature),
        )
    )


def parse_server_hello_packet(packet: bytes) -> ServerHello:
    """Parse and structurally validate one canonical ServerHello packet."""

    fields = _split_packet(
        packet,
        prefix=SERVER_HELLO_PREFIX,
        field_count=6,
        packet_name="ServerHello",
    )
    protocol_version = _parse_protocol_version(fields[1])
    return ServerHello(
        protocol_version=protocol_version,
        session_locator=_decode_base64("session_locator", fields[2]),
        server_random=_decode_base64("server_random", fields[3]),
        server_ephemeral_public_key=_decode_base64(
            "server_ephemeral_public_key",
            fields[4],
        ),
        server_signature=_decode_base64(
            "server_signature",
            fields[5],
        ),
    )


def build_data_aad(session_locator: bytes) -> bytes:
    """Build the locator-aware AEAD associated data for one V2 DATA packet.

    Every encrypted DATA-channel message in both directions (NMEA, ordinary
    ping/pong, confirmation ping/pong, graceful close) must use this exact
    construction, so that changing the plaintext locator on a captured
    ciphertext invalidates AEAD authentication even under the correct key.
    """

    _validate_session_locator(session_locator)
    return DATA_PREFIX + session_locator


def build_data_packet(
    session_locator: bytes,
    nonce: bytes,
    ciphertext: bytes,
) -> bytes:
    """Encode one V2 DATA packet: prefix, locator, nonce, then ciphertext."""

    _validate_session_locator(session_locator)
    normalized_nonce = _require_immutable_bytes("nonce", nonce)
    if len(normalized_nonce) != _DATA_NONCE_LENGTH:
        raise ValueError(f"nonce must be exactly {_DATA_NONCE_LENGTH} bytes")
    normalized_ciphertext = _require_immutable_bytes(
        "ciphertext", ciphertext
    )
    return (
        DATA_PREFIX + session_locator + normalized_nonce
        + normalized_ciphertext
    )


def parse_data_packet(packet: object) -> tuple[bytes, bytes, bytes]:
    """Parse one V2 DATA packet into (session_locator, nonce, ciphertext).

    Structural parsing only: this performs no AEAD work and does not know
    whether the locator is recognized or the ciphertext is authentic.
    """

    if not isinstance(packet, bytes):
        raise TypeError("DATA packet must be bytes")
    min_len = (
        len(DATA_PREFIX)
        + SESSION_LOCATOR_BYTES
        + _DATA_NONCE_LENGTH
        + _DATA_MIN_TAG_LENGTH
    )
    if not packet.startswith(DATA_PREFIX):
        raise ValueError("invalid DATA packet prefix")
    if len(packet) < min_len:
        raise ValueError("DATA packet too short")

    offset = len(DATA_PREFIX)
    session_locator = packet[offset:offset + SESSION_LOCATOR_BYTES]
    offset += SESSION_LOCATOR_BYTES
    nonce = packet[offset:offset + _DATA_NONCE_LENGTH]
    offset += _DATA_NONCE_LENGTH
    ciphertext = packet[offset:]
    return session_locator, nonce, ciphertext
