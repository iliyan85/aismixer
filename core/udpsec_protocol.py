"""Canonical wire values and codecs for the UDPSEC ECDHE handshake.

ClientHello and ServerHello packets use pipe-delimited ASCII framing with
strict UTF-8 for the station identifier, canonical unsigned decimal for the
timestamp, and canonical standard base64 for binary fields.  This module is
transport-neutral and deliberately performs no signing, verification, ECDHE,
HKDF, or elliptic-curve point validation.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field


CLIENT_HELLO_PREFIX = b"NMEA-H"
SERVER_HELLO_PREFIX = b"OK"
SESSION_CONFIRMATION_SEQUENCE = 0
SESSION_CLOSE_TYPE = "close"
SESSION_CLOSE_REASON_SHUTDOWN = "shutdown"

_MAX_TIMESTAMP = (1 << 64) - 1
_MAX_TIMESTAMP_ASCII = str(_MAX_TIMESTAMP).encode("ascii")
_RANDOM_LENGTH = 32
_EPHEMERAL_PUBLIC_KEY_LENGTH = 33
_COMPRESSED_POINT_PREFIXES = (0x02, 0x03)

__all__ = (
    "CLIENT_HELLO_PREFIX",
    "ClientHello",
    "SERVER_HELLO_PREFIX",
    "SESSION_CLOSE_REASON_SHUTDOWN",
    "SESSION_CLOSE_TYPE",
    "SESSION_CONFIRMATION_SEQUENCE",
    "ServerHello",
    "build_client_hello_packet",
    "build_session_close_message",
    "build_server_hello_packet",
    "is_session_close_message",
    "parse_client_hello_packet",
    "parse_server_hello_packet",
)


@dataclass(frozen=True, slots=True)
class ClientHello:
    """Immutable fields carried by one canonical UDPSEC ClientHello."""

    station_id: str
    timestamp: int
    client_random: bytes
    client_ephemeral_public_key: bytes
    client_signature: bytes = field(repr=False)

    def __post_init__(self) -> None:
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

    server_random: bytes
    server_ephemeral_public_key: bytes
    server_signature: bytes = field(repr=False)

    def __post_init__(self) -> None:
        _validate_random("server_random", self.server_random)
        _validate_ephemeral_public_key(
            "server_ephemeral_public_key",
            self.server_ephemeral_public_key,
        )
        _validate_signature("server_signature", self.server_signature)


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


def _parse_timestamp(encoded: bytes) -> int:
    if encoded == b"0":
        return 0
    if (
        not encoded
        or not 0x31 <= encoded[0] <= 0x39
        or any(not 0x30 <= value <= 0x39 for value in encoded[1:])
    ):
        raise ValueError(
            "timestamp must use canonical unsigned ASCII decimal"
        )
    if (
        len(encoded) > len(_MAX_TIMESTAMP_ASCII)
        or (
            len(encoded) == len(_MAX_TIMESTAMP_ASCII)
            and encoded > _MAX_TIMESTAMP_ASCII
        )
    ):
        raise ValueError("timestamp must fit in an unsigned 64-bit integer")

    timestamp = int(encoded)
    if str(timestamp).encode("ascii") != encoded:
        raise ValueError(
            "timestamp must use canonical unsigned ASCII decimal"
        )
    return timestamp


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
        field_count=6,
        packet_name="ClientHello",
    )
    try:
        station_id = fields[1].decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("station_id must use valid UTF-8") from None

    return ClientHello(
        station_id=station_id,
        timestamp=_parse_timestamp(fields[2]),
        client_random=_decode_base64("client_random", fields[3]),
        client_ephemeral_public_key=_decode_base64(
            "client_ephemeral_public_key",
            fields[4],
        ),
        client_signature=_decode_base64(
            "client_signature",
            fields[5],
        ),
    )


def build_server_hello_packet(server_hello: ServerHello) -> bytes:
    """Encode one ServerHello as canonical UDPSEC handshake bytes."""

    if not isinstance(server_hello, ServerHello):
        raise TypeError("server_hello must be a ServerHello")
    return b"|".join(
        (
            SERVER_HELLO_PREFIX,
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
        field_count=4,
        packet_name="ServerHello",
    )
    return ServerHello(
        server_random=_decode_base64("server_random", fields[1]),
        server_ephemeral_public_key=_decode_base64(
            "server_ephemeral_public_key",
            fields[2],
        ),
        server_signature=_decode_base64(
            "server_signature",
            fields[3],
        ),
    )
