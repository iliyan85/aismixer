"""Canonical wire values, codecs, and control validation for UDPSEC.

UDPSEC protocol version 2 (`UDPSEC_PROTOCOL_VERSION = 2`). There is no
capability negotiation and no downgrade: exactly one protocol version is
ever accepted, and any other value fails closed at parse time. Revision 1
wire compatibility does not exist.

The V2 DATA wire format is epoch-aware: the frame carries a one-byte
plaintext epoch selector and the full 32-bit epoch generation is bound
into the AEAD associated data, so the same established `LogicalSession`
and `session_locator` can carry more than one directional traffic-key
epoch during a bounded, authenticated in-session refresh. This is an
evolution of the V2 wire format, not a new protocol version: it is NOT
compatible with pre-refresh V2 binaries (a DATA frame without the epoch
selector, or with the old locator-only associated data, fails current
authentication and admission -- there is no legacy parser, negotiation,
version fallback, or silent downgrade), so `aismixer` and `nmea_sproxy`
must be upgraded together. The selector is a lookup hint only; the
generation is the authenticated value.

ClientHello and ServerHello packets use pipe-delimited ASCII framing with
strict UTF-8 for the station identifier, canonical unsigned decimal for the
protocol version and timestamp, and canonical standard base64 for binary
fields. DATA packets use a fixed-offset binary framing carrying a
server-minted opaque session locator and the epoch selector ahead of the
AEAD nonce and ciphertext. Encrypted ping, pong, graceful-close and epoch
refresh JSON objects use the shared structural helpers below. This module is
transport-neutral and deliberately performs no signing, verification,
ECDHE, HKDF, encryption, or elliptic-curve point validation.

The session locator is a tuple-independent lookup hint, not a credential:
knowledge of a locator selects which session/epoch a packet might belong
to, but it is cryptographically bound into every DATA packet's AEAD
associated data (see `build_data_aad`) and can never by itself authenticate
a packet, satisfy `allow_from`, or authorize a change of active transport
path. The epoch selector is likewise only a hint: it narrows an O(1)
locator lookup to exactly one of the session's live epoch objects, but the
epoch generation is authenticated through the AEAD associated data and the
refresh transcript, never trusted from the plaintext byte alone.
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

REFRESH_INIT_TYPE = "refresh_init"
REFRESH_REPLY_TYPE = "refresh_reply"
REFRESH_CONFIRM_TYPE = "refresh_confirm"
REFRESH_ACK_TYPE = "refresh_ack"
REFRESH_TRANSACTION_ID_BYTES = 32
REFRESH_RANDOM_BYTES = 32

UDPSEC_PROTOCOL_VERSION = 2
SESSION_LOCATOR_BYTES = 16
EPOCH_SELECTOR_BYTES = 1
# The establishment epoch of every session; each successful refresh installs
# generation + 1. Bound as an unsigned 32-bit value in the AEAD associated
# data and in every refresh transcript digest.
ESTABLISHMENT_EPOCH_GENERATION = 0
MAX_EPOCH_GENERATION = (1 << 32) - 1

_MAX_TIMESTAMP = (1 << 64) - 1
_MAX_TIMESTAMP_ASCII = str(_MAX_TIMESTAMP).encode("ascii")
_MAX_PROTOCOL_VERSION = 255
_MAX_PROTOCOL_VERSION_ASCII = str(_MAX_PROTOCOL_VERSION).encode("ascii")
_RANDOM_LENGTH = 32
_EPHEMERAL_PUBLIC_KEY_LENGTH = 33
_COMPRESSED_POINT_PREFIXES = (0x02, 0x03)
_DATA_NONCE_LENGTH = 12
_DATA_MIN_TAG_LENGTH = 16
_EPOCH_SELECTOR_MASK = 0xFF

__all__ = (
    "CLIENT_HELLO_PREFIX",
    "ClientHello",
    "DATA_PREFIX",
    "EPOCH_SELECTOR_BYTES",
    "ESTABLISHMENT_EPOCH_GENERATION",
    "MAX_EPOCH_GENERATION",
    "REFRESH_ACK_TYPE",
    "REFRESH_CONFIRM_TYPE",
    "REFRESH_INIT_TYPE",
    "REFRESH_RANDOM_BYTES",
    "REFRESH_REPLY_TYPE",
    "REFRESH_TRANSACTION_ID_BYTES",
    "RefreshAck",
    "RefreshConfirm",
    "RefreshInit",
    "RefreshReply",
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
    "build_refresh_ack_message",
    "build_refresh_confirm_message",
    "build_refresh_init_message",
    "build_refresh_reply_message",
    "build_session_close_message",
    "build_server_hello_packet",
    "epoch_selector_for_generation",
    "is_matching_pong_message",
    "is_ping_message",
    "is_session_close_message",
    "parse_client_hello_packet",
    "parse_data_packet",
    "parse_refresh_ack_message",
    "parse_refresh_confirm_message",
    "parse_refresh_init_message",
    "parse_refresh_reply_message",
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


#
# Epoch refresh control messages (UDPSEC V2 in-session epoch refresh)
#
# The four refresh messages are carried inside the existing encrypted DATA
# channel as strict, closed-schema JSON objects. INIT/REPLY travel under the
# current (parent) epoch's directional keys; CONFIRM/ACK travel under the
# candidate epoch's directional keys. Each is additionally bound by a P-256
# ECDSA identity signature over a refresh-domain transcript digest (see
# `core.udpsec_crypto`). This module performs structural validation only.
#

_REFRESH_LONG_MEMBERS = frozenset(
    {
        "type",
        "source_id",
        "txn",
        "parent_gen",
        "next_gen",
        "epoch_random",
        "epoch_pub",
        "refresh_sig",
        "timestamp",
    }
)
_REFRESH_SHORT_MEMBERS = frozenset(
    {"type", "source_id", "txn", "next_gen", "timestamp"}
)


def _validate_wire_generation(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer epoch generation")
    if not 0 <= value <= MAX_EPOCH_GENERATION:
        raise ValueError(f"{name} must fit in an unsigned 32-bit integer")
    return value


def _validate_transaction_id(value: object) -> bytes:
    normalized = _require_immutable_bytes("transaction_id", value)
    if len(normalized) != REFRESH_TRANSACTION_ID_BYTES:
        raise ValueError(
            f"transaction_id must be exactly {REFRESH_TRANSACTION_ID_BYTES} "
            "bytes"
        )
    return normalized


def _decode_base64_text(name: str, value: object) -> bytes:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a base64 string")
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError:
        raise ValueError(f"{name} must be ASCII base64") from None
    return _decode_base64(name, encoded)


def _decode_transaction_id_text(value: object) -> bytes:
    decoded = _decode_base64_text("txn", value)
    if len(decoded) != REFRESH_TRANSACTION_ID_BYTES:
        raise ValueError(
            f"txn must decode to exactly {REFRESH_TRANSACTION_ID_BYTES} bytes"
        )
    return decoded


def _decode_refresh_random_text(name: str, value: object) -> bytes:
    decoded = _decode_base64_text(name, value)
    if len(decoded) != _RANDOM_LENGTH:
        raise ValueError(f"{name} must decode to exactly 32 bytes")
    return decoded


def _decode_refresh_public_key_text(name: str, value: object) -> bytes:
    decoded = _decode_base64_text(name, value)
    if len(decoded) != _EPHEMERAL_PUBLIC_KEY_LENGTH:
        raise ValueError(f"{name} must decode to exactly 33 bytes")
    if decoded[0] not in _COMPRESSED_POINT_PREFIXES:
        raise ValueError(f"{name} must decode to a compressed P-256 point")
    return decoded


@dataclass(frozen=True, slots=True)
class RefreshInit:
    """Structurally validated REFRESH_INIT (client -> server, parent epoch)."""

    station_id: str
    transaction_id: bytes
    parent_generation: int
    next_generation: int
    client_epoch_random: bytes
    client_epoch_public_key: bytes
    refresh_signature: bytes = field(repr=False)
    timestamp: int

    def __post_init__(self) -> None:
        _station_id_bytes(self.station_id)
        _validate_transaction_id(self.transaction_id)
        _validate_wire_generation("parent_generation", self.parent_generation)
        _validate_wire_generation("next_generation", self.next_generation)
        if self.next_generation != self.parent_generation + 1:
            raise ValueError("next_generation must be parent_generation + 1")
        _validate_random("client_epoch_random", self.client_epoch_random)
        _validate_ephemeral_public_key(
            "client_epoch_public_key", self.client_epoch_public_key
        )
        _validate_signature("refresh_signature", self.refresh_signature)
        _validate_liveness_timestamp(self.timestamp)


@dataclass(frozen=True, slots=True)
class RefreshReply:
    """Structurally validated REFRESH_REPLY (server -> client, parent epoch)."""

    station_id: str
    transaction_id: bytes
    parent_generation: int
    next_generation: int
    server_epoch_random: bytes
    server_epoch_public_key: bytes
    refresh_signature: bytes = field(repr=False)
    timestamp: int

    def __post_init__(self) -> None:
        _station_id_bytes(self.station_id)
        _validate_transaction_id(self.transaction_id)
        _validate_wire_generation("parent_generation", self.parent_generation)
        _validate_wire_generation("next_generation", self.next_generation)
        if self.next_generation != self.parent_generation + 1:
            raise ValueError("next_generation must be parent_generation + 1")
        _validate_random("server_epoch_random", self.server_epoch_random)
        _validate_ephemeral_public_key(
            "server_epoch_public_key", self.server_epoch_public_key
        )
        _validate_signature("refresh_signature", self.refresh_signature)
        _validate_liveness_timestamp(self.timestamp)


@dataclass(frozen=True, slots=True)
class RefreshConfirm:
    """Structurally validated REFRESH_CONFIRM (client -> server, new epoch)."""

    station_id: str
    transaction_id: bytes
    next_generation: int
    timestamp: int

    def __post_init__(self) -> None:
        _station_id_bytes(self.station_id)
        _validate_transaction_id(self.transaction_id)
        _validate_wire_generation("next_generation", self.next_generation)
        _validate_liveness_timestamp(self.timestamp)


@dataclass(frozen=True, slots=True)
class RefreshAck:
    """Structurally validated REFRESH_ACK (server -> client, new epoch)."""

    station_id: str
    transaction_id: bytes
    next_generation: int
    timestamp: int

    def __post_init__(self) -> None:
        _station_id_bytes(self.station_id)
        _validate_transaction_id(self.transaction_id)
        _validate_wire_generation("next_generation", self.next_generation)
        _validate_liveness_timestamp(self.timestamp)


def _build_refresh_long_message(
    message_type: str,
    *,
    station_id: str,
    transaction_id: bytes,
    parent_generation: int,
    next_generation: int,
    epoch_random: bytes,
    epoch_public_key: bytes,
    refresh_signature: bytes,
    timestamp: int,
) -> dict:
    _station_id_bytes(station_id)
    _validate_transaction_id(transaction_id)
    _validate_wire_generation("parent_generation", parent_generation)
    _validate_wire_generation("next_generation", next_generation)
    if next_generation != parent_generation + 1:
        raise ValueError("next_generation must be parent_generation + 1")
    _validate_random("epoch_random", epoch_random)
    _validate_ephemeral_public_key("epoch_public_key", epoch_public_key)
    _validate_signature("refresh_signature", refresh_signature)
    _validate_liveness_timestamp(timestamp)
    return {
        "type": message_type,
        "source_id": station_id,
        "txn": _encode_base64(transaction_id).decode("ascii"),
        "parent_gen": parent_generation,
        "next_gen": next_generation,
        "epoch_random": _encode_base64(epoch_random).decode("ascii"),
        "epoch_pub": _encode_base64(epoch_public_key).decode("ascii"),
        "refresh_sig": _encode_base64(refresh_signature).decode("ascii"),
        "timestamp": timestamp,
    }


def _build_refresh_short_message(
    message_type: str,
    *,
    station_id: str,
    transaction_id: bytes,
    next_generation: int,
    timestamp: int,
) -> dict:
    _station_id_bytes(station_id)
    _validate_transaction_id(transaction_id)
    _validate_wire_generation("next_generation", next_generation)
    _validate_liveness_timestamp(timestamp)
    return {
        "type": message_type,
        "source_id": station_id,
        "txn": _encode_base64(transaction_id).decode("ascii"),
        "next_gen": next_generation,
        "timestamp": timestamp,
    }


def build_refresh_init_message(**kwargs) -> dict:
    """Build one canonical REFRESH_INIT JSON control object."""

    return _build_refresh_long_message(REFRESH_INIT_TYPE, **kwargs)


def build_refresh_reply_message(**kwargs) -> dict:
    """Build one canonical REFRESH_REPLY JSON control object."""

    return _build_refresh_long_message(REFRESH_REPLY_TYPE, **kwargs)


def build_refresh_confirm_message(**kwargs) -> dict:
    """Build one canonical REFRESH_CONFIRM JSON control object."""

    return _build_refresh_short_message(REFRESH_CONFIRM_TYPE, **kwargs)


def build_refresh_ack_message(**kwargs) -> dict:
    """Build one canonical REFRESH_ACK JSON control object."""

    return _build_refresh_short_message(REFRESH_ACK_TYPE, **kwargs)


def _parse_refresh_long_message(
    message: object,
    *,
    message_type: str,
):
    if not isinstance(message, dict):
        raise ValueError(f"{message_type} must be a JSON object")
    if frozenset(message) != _REFRESH_LONG_MEMBERS:
        raise ValueError(f"{message_type} has an unexpected member set")
    if message["type"] != message_type:
        raise ValueError(f"{message_type} type field mismatch")
    parent_generation = _validate_wire_generation(
        "parent_gen", message["parent_gen"]
    )
    next_generation = _validate_wire_generation("next_gen", message["next_gen"])
    if next_generation != parent_generation + 1:
        raise ValueError(f"{message_type} next_gen must be parent_gen + 1")
    timestamp = message["timestamp"]
    _validate_liveness_timestamp(timestamp)
    return (
        message["source_id"],
        _decode_transaction_id_text(message["txn"]),
        parent_generation,
        next_generation,
        _decode_refresh_random_text("epoch_random", message["epoch_random"]),
        _decode_refresh_public_key_text("epoch_pub", message["epoch_pub"]),
        _decode_base64_text("refresh_sig", message["refresh_sig"]),
        timestamp,
    )


def _parse_refresh_short_message(
    message: object,
    *,
    message_type: str,
):
    if not isinstance(message, dict):
        raise ValueError(f"{message_type} must be a JSON object")
    if frozenset(message) != _REFRESH_SHORT_MEMBERS:
        raise ValueError(f"{message_type} has an unexpected member set")
    if message["type"] != message_type:
        raise ValueError(f"{message_type} type field mismatch")
    next_generation = _validate_wire_generation("next_gen", message["next_gen"])
    timestamp = message["timestamp"]
    _validate_liveness_timestamp(timestamp)
    return (
        message["source_id"],
        _decode_transaction_id_text(message["txn"]),
        next_generation,
        timestamp,
    )


def parse_refresh_init_message(message: object) -> RefreshInit:
    """Parse and structurally validate one canonical REFRESH_INIT object."""

    (
        station_id,
        transaction_id,
        parent_generation,
        next_generation,
        epoch_random,
        epoch_public_key,
        refresh_signature,
        timestamp,
    ) = _parse_refresh_long_message(message, message_type=REFRESH_INIT_TYPE)
    return RefreshInit(
        station_id=station_id,
        transaction_id=transaction_id,
        parent_generation=parent_generation,
        next_generation=next_generation,
        client_epoch_random=epoch_random,
        client_epoch_public_key=epoch_public_key,
        refresh_signature=refresh_signature,
        timestamp=timestamp,
    )


def parse_refresh_reply_message(message: object) -> RefreshReply:
    """Parse and structurally validate one canonical REFRESH_REPLY object."""

    (
        station_id,
        transaction_id,
        parent_generation,
        next_generation,
        epoch_random,
        epoch_public_key,
        refresh_signature,
        timestamp,
    ) = _parse_refresh_long_message(message, message_type=REFRESH_REPLY_TYPE)
    return RefreshReply(
        station_id=station_id,
        transaction_id=transaction_id,
        parent_generation=parent_generation,
        next_generation=next_generation,
        server_epoch_random=epoch_random,
        server_epoch_public_key=epoch_public_key,
        refresh_signature=refresh_signature,
        timestamp=timestamp,
    )


def parse_refresh_confirm_message(message: object) -> RefreshConfirm:
    """Parse and structurally validate one canonical REFRESH_CONFIRM object."""

    station_id, transaction_id, next_generation, timestamp = (
        _parse_refresh_short_message(
            message, message_type=REFRESH_CONFIRM_TYPE
        )
    )
    return RefreshConfirm(
        station_id=station_id,
        transaction_id=transaction_id,
        next_generation=next_generation,
        timestamp=timestamp,
    )


def parse_refresh_ack_message(message: object) -> RefreshAck:
    """Parse and structurally validate one canonical REFRESH_ACK object."""

    station_id, transaction_id, next_generation, timestamp = (
        _parse_refresh_short_message(message, message_type=REFRESH_ACK_TYPE)
    )
    return RefreshAck(
        station_id=station_id,
        transaction_id=transaction_id,
        next_generation=next_generation,
        timestamp=timestamp,
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


def _validate_epoch_generation(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("epoch_generation must be an integer")
    if not 0 <= value <= MAX_EPOCH_GENERATION:
        raise ValueError(
            "epoch_generation must fit in an unsigned 32-bit integer"
        )
    return value


def epoch_selector_for_generation(epoch_generation: int) -> int:
    """Return the one-byte plaintext DATA selector for an epoch generation.

    This is the low byte of the generation. It is only a lookup hint: at
    most three epoch generations (retiring G-1, current G, pending G+1) are
    ever simultaneously live for one locator, and consecutive generations
    have distinct low bytes, so the selector picks exactly one candidate.
    The authoritative generation is bound into the AEAD associated data.
    """

    return _validate_epoch_generation(epoch_generation) & _EPOCH_SELECTOR_MASK


def build_data_aad(session_locator: bytes, epoch_generation: int) -> bytes:
    """Build the locator- and epoch-aware AEAD associated data for one
    epoch-aware V2 DATA packet.

    Every encrypted DATA-channel message in both directions (NMEA, ordinary
    ping/pong, confirmation ping/pong, graceful close, and the four epoch
    refresh control messages) must use this exact construction, so that
    changing the plaintext locator OR the plaintext epoch selector on a
    captured ciphertext invalidates AEAD authentication even under the
    correct key. The full 32-bit generation -- not merely the one-byte
    selector -- is bound here, so a captured epoch-G ciphertext re-sent with
    an epoch-(G+1) selector cannot authenticate under either key.
    """

    _validate_session_locator(session_locator)
    generation = _validate_epoch_generation(epoch_generation)
    return DATA_PREFIX + session_locator + generation.to_bytes(4, "big")


def build_data_packet(
    session_locator: bytes,
    epoch_generation: int,
    nonce: bytes,
    ciphertext: bytes,
) -> bytes:
    """Encode one epoch-aware V2 DATA packet: prefix, locator, one-byte
    epoch selector, nonce, then ciphertext.

    `epoch_generation` is the full generation of the epoch the ciphertext
    was produced under; only its low byte is written to the wire (see
    `epoch_selector_for_generation`).
    """

    _validate_session_locator(session_locator)
    selector = epoch_selector_for_generation(epoch_generation)
    normalized_nonce = _require_immutable_bytes("nonce", nonce)
    if len(normalized_nonce) != _DATA_NONCE_LENGTH:
        raise ValueError(f"nonce must be exactly {_DATA_NONCE_LENGTH} bytes")
    normalized_ciphertext = _require_immutable_bytes(
        "ciphertext", ciphertext
    )
    return (
        DATA_PREFIX
        + session_locator
        + selector.to_bytes(1, "big")
        + normalized_nonce
        + normalized_ciphertext
    )


def parse_data_packet(packet: object) -> tuple[bytes, int, bytes, bytes]:
    """Parse one epoch-aware V2 DATA packet into
    (session_locator, epoch_selector, nonce, ciphertext).

    `epoch_selector` is the raw one-byte lookup hint as an ``int`` in
    ``0..255``. Structural parsing only: this performs no AEAD work and does
    not know whether the locator is recognized, which epoch the selector
    actually names, or whether the ciphertext is authentic. A pre-refresh
    V2 DATA frame (no selector byte) is not accepted through any legacy
    path: if it is long enough to parse at all its bytes are reinterpreted
    under the current layout and fail AEAD authentication, and a shorter
    one fails the length check here.
    """

    if not isinstance(packet, bytes):
        raise TypeError("DATA packet must be bytes")
    min_len = (
        len(DATA_PREFIX)
        + SESSION_LOCATOR_BYTES
        + EPOCH_SELECTOR_BYTES
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
    epoch_selector = packet[offset]
    offset += EPOCH_SELECTOR_BYTES
    nonce = packet[offset:offset + _DATA_NONCE_LENGTH]
    offset += _DATA_NONCE_LENGTH
    ciphertext = packet[offset:]
    return session_locator, epoch_selector, nonce, ciphertext
