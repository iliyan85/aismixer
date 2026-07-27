"""Canonical transcript and key helpers for the UDPSEC ECDHE handshake.

Every logical transcript field is framed as a four-byte unsigned big-endian
length followed by the field bytes. Timestamps are first encoded as exactly
eight unsigned big-endian bytes and are then framed like every other field.

Binary inputs accept ``bytes``, ``bytearray``, and ``memoryview``. Mutable
inputs are copied to immutable ``bytes``. UDPSEC ephemeral public points use
only canonical 33-byte SEC1/X9.62 compressed-point encoding on P-256.
Transcript authentication uses P-256 ECDSA over a precomputed SHA-256 digest,
with signatures encoded as strict canonical low-S ASN.1 DER. The transcript
helpers continue to treat their public-key and signature fields as opaque
bytes.
"""

from __future__ import annotations

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils


ECDHE_CURVE = ec.SECP256R1()
TRANSCRIPT_HASH = hashes.SHA256()

DOMAIN_CONTEXT = b"AISMIXER-UDPSEC-ECDHE"
CLIENT_AUTH_LABEL = b"CLIENT-AUTH"
SERVER_AUTH_LABEL = b"SERVER-AUTH"
SESSION_TRANSCRIPT_LABEL = b"SESSION-TRANSCRIPT"

_MAX_FRAMED_FIELD_LENGTH = (1 << 32) - 1
_MAX_TIMESTAMP = (1 << 64) - 1
_COMPRESSED_PUBLIC_KEY_LENGTH = 33
_COMPRESSED_POINT_PREFIXES = (0x02, 0x03)
_P256_SHARED_SECRET_LENGTH = ECDHE_CURVE.key_size // 8
_TRANSCRIPT_DIGEST_LENGTH = TRANSCRIPT_HASH.digest_size
_P256_ORDER = int(
    "FFFFFFFF00000000FFFFFFFFFFFFFFFF"
    "BCE6FAADA7179E84F3B9CAC2FC632551",
    16,
)
_P256_HALF_ORDER = _P256_ORDER // 2
_INVALID_EPHEMERAL_PUBLIC_KEY = (
    "encoded ephemeral public key is not a valid canonical compressed "
    "P-256 point"
)

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
    "derive_ephemeral_shared_secret",
    "generate_ephemeral_private_key",
    "parse_ephemeral_public_key",
    "serialize_ephemeral_public_key",
    "sign_transcript_digest",
    "verify_transcript_signature",
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


def _require_p256_curve(name: str, curve: ec.EllipticCurve) -> None:
    if not isinstance(curve, ec.SECP256R1):
        raise ValueError(f"{name} must use SECP256R1/P-256")


def _transcript_digest_bytes(
    digest: bytes | bytearray | memoryview,
) -> bytes:
    if not isinstance(digest, (bytes, bytearray, memoryview)):
        raise TypeError("digest must be bytes, bytearray, or memoryview")
    try:
        normalized = bytes(digest)
    except ValueError as exc:
        raise ValueError("digest must reference readable bytes") from exc
    if len(normalized) != _TRANSCRIPT_DIGEST_LENGTH:
        raise ValueError("digest must be exactly 32 bytes")
    return normalized


def _signature_bytes(
    signature: bytes | bytearray | memoryview,
) -> bytes | None:
    if not isinstance(signature, (bytes, bytearray, memoryview)):
        raise TypeError(
            "signature must be bytes, bytearray, or memoryview"
        )
    try:
        return bytes(signature)
    except ValueError:
        return None


def _transcript_signature_algorithm() -> ec.ECDSA:
    return ec.ECDSA(utils.Prehashed(hashes.SHA256()))


def _valid_p256_scalar(value: int) -> bool:
    return 1 <= value < _P256_ORDER


def generate_ephemeral_private_key() -> ec.EllipticCurvePrivateKey:
    """Generate a fresh, in-memory P-256 private key for one handshake."""

    return ec.generate_private_key(ECDHE_CURVE)


def serialize_ephemeral_public_key(
    public_key: ec.EllipticCurvePublicKey,
) -> bytes:
    """Serialize a P-256 public key as one canonical compressed SEC1 point."""

    if not isinstance(public_key, ec.EllipticCurvePublicKey):
        raise TypeError("public_key must be an EllipticCurvePublicKey")
    _require_p256_curve("public_key", public_key.curve)

    encoded = public_key.public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.CompressedPoint,
    )
    if (
        not isinstance(encoded, bytes)
        or len(encoded) != _COMPRESSED_PUBLIC_KEY_LENGTH
        or encoded[0] not in _COMPRESSED_POINT_PREFIXES
    ):
        raise RuntimeError(
            "public_key did not produce a canonical compressed P-256 point"
        )
    return encoded


def parse_ephemeral_public_key(
    encoded: bytes | bytearray | memoryview,
) -> ec.EllipticCurvePublicKey:
    """Parse one canonical compressed SEC1 P-256 public point."""

    normalized = _required_bytes("encoded ephemeral public key", encoded)
    if len(normalized) != _COMPRESSED_PUBLIC_KEY_LENGTH:
        raise ValueError(
            "encoded ephemeral public key must be exactly 33 bytes"
        )
    if normalized[0] not in _COMPRESSED_POINT_PREFIXES:
        raise ValueError(
            "encoded ephemeral public key must start with 0x02 or 0x03"
        )

    try:
        public_key = ec.EllipticCurvePublicKey.from_encoded_point(
            ECDHE_CURVE,
            normalized,
        )
    except ValueError:
        raise ValueError(_INVALID_EPHEMERAL_PUBLIC_KEY) from None

    canonical = serialize_ephemeral_public_key(public_key)
    if canonical != normalized:
        raise ValueError(_INVALID_EPHEMERAL_PUBLIC_KEY)
    return public_key


def derive_ephemeral_shared_secret(
    private_key: ec.EllipticCurvePrivateKey,
    peer_public_key: ec.EllipticCurvePublicKey,
) -> bytes:
    """Return the raw 32-byte P-256 ECDHE shared secret without a KDF."""

    if not isinstance(private_key, ec.EllipticCurvePrivateKey):
        raise TypeError("private_key must be an EllipticCurvePrivateKey")
    if not isinstance(peer_public_key, ec.EllipticCurvePublicKey):
        raise TypeError(
            "peer_public_key must be an EllipticCurvePublicKey"
        )
    _require_p256_curve("private_key", private_key.curve)
    _require_p256_curve("peer_public_key", peer_public_key.curve)

    shared_secret = private_key.exchange(ec.ECDH(), peer_public_key)
    if (
        not isinstance(shared_secret, bytes)
        or len(shared_secret) != _P256_SHARED_SECRET_LENGTH
    ):
        raise RuntimeError(
            "P-256 ECDHE exchange did not produce a 32-byte secret"
        )
    return shared_secret


def sign_transcript_digest(
    private_key: ec.EllipticCurvePrivateKey,
    digest: bytes | bytearray | memoryview,
) -> bytes:
    """Sign one precomputed SHA-256 transcript digest as low-S ECDSA DER."""

    if not isinstance(private_key, ec.EllipticCurvePrivateKey):
        raise TypeError("private_key must be an EllipticCurvePrivateKey")
    _require_p256_curve("private_key", private_key.curve)
    normalized_digest = _transcript_digest_bytes(digest)

    signature = private_key.sign(
        normalized_digest,
        _transcript_signature_algorithm(),
    )
    if not isinstance(signature, bytes) or not signature:
        raise RuntimeError("ECDSA backend did not produce a DER signature")

    try:
        r, s = utils.decode_dss_signature(signature)
    except ValueError as exc:
        raise RuntimeError(
            "ECDSA backend produced an invalid DER signature"
        ) from exc
    if not _valid_p256_scalar(r) or not _valid_p256_scalar(s):
        raise RuntimeError(
            "ECDSA backend produced signature scalars outside P-256"
        )
    if utils.encode_dss_signature(r, s) != signature:
        raise RuntimeError(
            "ECDSA backend produced a non-canonical DER signature"
        )

    if s > _P256_HALF_ORDER:
        s = _P256_ORDER - s
    canonical = utils.encode_dss_signature(r, s)
    if not isinstance(canonical, bytes) or not canonical:
        raise RuntimeError(
            "ECDSA signature canonicalization did not produce DER bytes"
        )
    return canonical


def verify_transcript_signature(
    public_key: ec.EllipticCurvePublicKey,
    signature: bytes | bytearray | memoryview,
    digest: bytes | bytearray | memoryview,
) -> bool:
    """Verify strict canonical low-S DER over a precomputed SHA-256 digest."""

    if not isinstance(public_key, ec.EllipticCurvePublicKey):
        raise TypeError("public_key must be an EllipticCurvePublicKey")
    _require_p256_curve("public_key", public_key.curve)
    normalized_digest = _transcript_digest_bytes(digest)
    normalized_signature = _signature_bytes(signature)
    if not normalized_signature:
        return False

    try:
        r, s = utils.decode_dss_signature(normalized_signature)
    except ValueError:
        return False
    if not _valid_p256_scalar(r) or not _valid_p256_scalar(s):
        return False
    if s > _P256_HALF_ORDER:
        return False
    if utils.encode_dss_signature(r, s) != normalized_signature:
        return False

    try:
        public_key.verify(
            normalized_signature,
            normalized_digest,
            _transcript_signature_algorithm(),
        )
    except InvalidSignature:
        return False
    return True


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
