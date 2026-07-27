"""Canonical transcript digests for the UDPSEC ECDHE handshake.

Every logical transcript field is framed as a four-byte unsigned big-endian
length followed by the field bytes. Timestamps are first encoded as exactly
eight unsigned big-endian bytes and are then framed like every other field.

Binary inputs accept ``bytes``, ``bytearray``, and ``memoryview``. Mutable
inputs are copied to immutable ``bytes`` before hashing. Public-key and
signature fields remain opaque here; later handshake work is responsible for
parsing and validating their cryptographic encodings.
"""

from __future__ import annotations

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec


ECDHE_CURVE = ec.SECP256R1()
TRANSCRIPT_HASH = hashes.SHA256()

DOMAIN_CONTEXT = b"AISMIXER-UDPSEC-ECDHE"
CLIENT_AUTH_LABEL = b"CLIENT-AUTH"
SERVER_AUTH_LABEL = b"SERVER-AUTH"
SESSION_TRANSCRIPT_LABEL = b"SESSION-TRANSCRIPT"

_MAX_FRAMED_FIELD_LENGTH = (1 << 32) - 1
_MAX_TIMESTAMP = (1 << 64) - 1

__all__ = (
    "CLIENT_AUTH_LABEL",
    "DOMAIN_CONTEXT",
    "ECDHE_CURVE",
    "SERVER_AUTH_LABEL",
    "SESSION_TRANSCRIPT_LABEL",
    "TRANSCRIPT_HASH",
    "build_client_auth_digest",
    "build_server_auth_digest",
    "build_session_transcript_hash",
)


def _station_id_bytes(station_id: object) -> bytes:
    if not isinstance(station_id, str):
        raise TypeError("station_id must be a string")
    if station_id == "":
        raise ValueError("station_id must not be empty")
    try:
        return station_id.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("station_id must be UTF-8 encodable") from exc


def _timestamp_bytes(timestamp: object) -> bytes:
    if isinstance(timestamp, bool) or not isinstance(timestamp, int):
        raise TypeError("timestamp must be an integer")
    if not 0 <= timestamp <= _MAX_TIMESTAMP:
        raise ValueError("timestamp must fit in an unsigned 64-bit integer")
    return timestamp.to_bytes(8, "big", signed=False)


def _required_bytes(name: str, value: object) -> bytes:
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise TypeError(f"{name} must be bytes, bytearray, or memoryview")
    try:
        normalized = bytes(value)
    except ValueError as exc:
        raise ValueError(f"{name} must reference readable bytes") from exc
    if not normalized:
        raise ValueError(f"{name} must not be empty")
    return normalized


def _client_hello_fields(
    station_id: object,
    timestamp: object,
    client_random: object,
    client_ephemeral_public_key: object,
) -> tuple[bytes, ...]:
    return (
        _station_id_bytes(station_id),
        _timestamp_bytes(timestamp),
        _required_bytes("client_random", client_random),
        _required_bytes(
            "client_ephemeral_public_key",
            client_ephemeral_public_key,
        ),
    )


def _server_hello_fields(
    server_random: object,
    server_ephemeral_public_key: object,
) -> tuple[bytes, ...]:
    return (
        _required_bytes("server_random", server_random),
        _required_bytes(
            "server_ephemeral_public_key",
            server_ephemeral_public_key,
        ),
    )


def _update_framed(digest: hashes.Hash, field: bytes) -> None:
    if len(field) > _MAX_FRAMED_FIELD_LENGTH:
        raise ValueError("transcript field exceeds unsigned 32-bit framing")
    digest.update(len(field).to_bytes(4, "big"))
    digest.update(field)


def _digest_fields(label: bytes, fields: tuple[bytes, ...]) -> bytes:
    digest = hashes.Hash(TRANSCRIPT_HASH)
    for field in (DOMAIN_CONTEXT, label, *fields):
        _update_framed(digest, field)
    return digest.finalize()


def build_client_auth_digest(
    *,
    station_id: str,
    timestamp: int,
    client_random: bytes | bytearray | memoryview,
    client_ephemeral_public_key: bytes | bytearray | memoryview,
) -> bytes:
    """Hash the ClientHello authentication transcript.

    Field order is domain, client-auth label, UTF-8 station ID, timestamp,
    client random, and client ephemeral public key.
    """

    client_hello = _client_hello_fields(
        station_id,
        timestamp,
        client_random,
        client_ephemeral_public_key,
    )
    return _digest_fields(CLIENT_AUTH_LABEL, client_hello)


def build_server_auth_digest(
    *,
    station_id: str,
    timestamp: int,
    client_random: bytes | bytearray | memoryview,
    client_ephemeral_public_key: bytes | bytearray | memoryview,
    client_signature: bytes | bytearray | memoryview,
    server_random: bytes | bytearray | memoryview,
    server_ephemeral_public_key: bytes | bytearray | memoryview,
) -> bytes:
    """Hash the ServerHello authentication transcript.

    Field order is domain, server-auth label, all ClientHello fields, client
    signature, server random, and server ephemeral public key.
    """

    client_hello = _client_hello_fields(
        station_id,
        timestamp,
        client_random,
        client_ephemeral_public_key,
    )
    normalized_client_signature = _required_bytes(
        "client_signature",
        client_signature,
    )
    server_hello = _server_hello_fields(
        server_random,
        server_ephemeral_public_key,
    )
    return _digest_fields(
        SERVER_AUTH_LABEL,
        (*client_hello, normalized_client_signature, *server_hello),
    )


def build_session_transcript_hash(
    *,
    station_id: str,
    timestamp: int,
    client_random: bytes | bytearray | memoryview,
    client_ephemeral_public_key: bytes | bytearray | memoryview,
    client_signature: bytes | bytearray | memoryview,
    server_random: bytes | bytearray | memoryview,
    server_ephemeral_public_key: bytes | bytearray | memoryview,
    server_signature: bytes | bytearray | memoryview,
) -> bytes:
    """Hash the final authenticated session transcript.

    Field order is domain, session-transcript label, all ClientHello fields,
    client signature, all ServerHello fields, and server signature.
    """

    client_hello = _client_hello_fields(
        station_id,
        timestamp,
        client_random,
        client_ephemeral_public_key,
    )
    normalized_client_signature = _required_bytes(
        "client_signature",
        client_signature,
    )
    server_hello = _server_hello_fields(
        server_random,
        server_ephemeral_public_key,
    )
    normalized_server_signature = _required_bytes(
        "server_signature",
        server_signature,
    )
    return _digest_fields(
        SESSION_TRANSCRIPT_LABEL,
        (
            *client_hello,
            normalized_client_signature,
            *server_hello,
            normalized_server_signature,
        ),
    )
