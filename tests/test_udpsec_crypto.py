import hashlib
import hmac
import itertools
from dataclasses import FrozenInstanceError, fields, is_dataclass

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils

import core.udpsec_crypto as udpsec_crypto
from core.udpsec_crypto import (
    CLIENT_AUTH_LABEL,
    DOMAIN_CONTEXT,
    ECDHE_CURVE,
    SERVER_AUTH_LABEL,
    SessionKeyMaterial,
    SESSION_TRANSCRIPT_LABEL,
    TRANSCRIPT_HASH,
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


CLIENT_ARGS = {
    "station_id": "boat_001",
    "timestamp": 1234567890,
    "client_random": b"client-random",
    "client_ephemeral_public_key": b"client-ephemeral",
}
SERVER_ARGS = {
    **CLIENT_ARGS,
    "client_signature": b"client-signature",
    "server_random": b"server-random",
    "server_ephemeral_public_key": b"server-ephemeral",
}
SESSION_ARGS = {
    **SERVER_ARGS,
    "server_signature": b"server-signature",
}

BUILDERS = (
    ("client", build_client_auth_digest, CLIENT_ARGS),
    ("server", build_server_auth_digest, SERVER_ARGS),
    ("session", build_session_transcript_hash, SESSION_ARGS),
)

P256_PUBLIC_KEY_SCALAR_1 = bytes.fromhex(
    "036b17d1f2e12c4247f8bce6e563a440"
    "f277037d812deb33a0f4a13945d898c296"
)
P256_SHARED_SECRET_SCALARS_1_AND_2 = bytes.fromhex(
    "7cf27b188d034f7e8a52380304b51ac3"
    "c08969e277f21b35a60b48fc47669978"
)
P256_SHARED_SECRET_SCALARS_1_AND_379 = bytes.fromhex(
    "005543894af3d00ed7d740abdbd75c96"
    "b06877b787db5f70eea78b90a8d7c00a"
)
P256_ORDER = int(
    "FFFFFFFF00000000FFFFFFFFFFFFFFFF"
    "BCE6FAADA7179E84F3B9CAC2FC632551",
    16,
)
TEST_TRANSCRIPT_DIGEST = bytes(range(32))
EXPECTED_CLIENT_TO_SERVER_INFO = bytes.fromhex(
    "000000154149534d495845522d5544505345432d4543444845"
    "0000001453455353494f4e2d4b45592d5343484544554c45"
    "00000010434c49454e542d544f2d534552564552"
    "0000000f4145532d3235362d47434d2d4b4559"
)
EXPECTED_SERVER_TO_CLIENT_INFO = bytes.fromhex(
    "000000154149534d495845522d5544505345432d4543444845"
    "0000001453455353494f4e2d4b45592d5343484544554c45"
    "000000105345525645522d544f2d434c49454e54"
    "0000000f4145532d3235362d47434d2d4b4559"
)
EXPECTED_CLIENT_TO_SERVER_KEY = bytes.fromhex(
    "df76ce2324cd1f051b79ffece9d776e9"
    "e9d53326cfa807bfcd43b18605cd25ea"
)
EXPECTED_SERVER_TO_CLIENT_KEY = bytes.fromhex(
    "a29be2b5bce14069ae3c189126bf48cf"
    "8d101e2fd726e881c21c1f6fc7c0ba6d"
)


class _BytesConvertible:
    def __bytes__(self):
        return b"silently-converted"


def _field_cases(replacement):
    return [
        pytest.param(
            builder,
            arguments,
            field,
            replacement(field),
            id=f"{name}-{field}",
        )
        for name, builder, arguments in BUILDERS
        for field in arguments
    ]


def _binary_field_cases(replacement):
    return [
        pytest.param(
            builder,
            arguments,
            field,
            replacement,
            id=f"{name}-{field}",
        )
        for name, builder, arguments in BUILDERS
        for field in arguments
        if field not in ("station_id", "timestamp")
    ]


def _p256_private_key(private_value):
    return ec.derive_private_key(private_value, ec.SECP256R1())


def _p384_private_key(private_value=1):
    return ec.derive_private_key(private_value, ec.SECP384R1())


def _to_low_s(signature):
    r, s = utils.decode_dss_signature(signature)
    return utils.encode_dss_signature(r, min(s, P256_ORDER - s))


def _reference_frame(value):
    return len(value).to_bytes(4, "big") + value


def _reference_session_key_info(direction_label):
    return b"".join(
        _reference_frame(value)
        for value in (
            b"AISMIXER-UDPSEC-ECDHE",
            b"SESSION-KEY-SCHEDULE",
            direction_label,
            b"AES-256-GCM-KEY",
        )
    )


def _reference_hkdf_sha256(ikm, salt, info, length=32):
    digest_size = hashlib.sha256().digest_size
    if length > 255 * digest_size:
        raise ValueError("reference HKDF output is too long")

    pseudorandom_key = hmac.new(
        salt,
        ikm,
        hashlib.sha256,
    ).digest()
    output = bytearray()
    previous = b""
    for counter in range(1, (length + digest_size - 1) // digest_size + 1):
        previous = hmac.new(
            pseudorandom_key,
            previous + info + bytes((counter,)),
            hashlib.sha256,
        ).digest()
        output.extend(previous)
    return bytes(output[:length])


def _build_pure_handshake():
    client_identity_private_key = _p256_private_key(101)
    server_identity_private_key = _p256_private_key(202)
    client_ephemeral_private_key = _p256_private_key(1)
    server_ephemeral_private_key = _p256_private_key(2)

    client_ephemeral_public_bytes = serialize_ephemeral_public_key(
        client_ephemeral_private_key.public_key()
    )
    server_ephemeral_public_bytes = serialize_ephemeral_public_key(
        server_ephemeral_private_key.public_key()
    )
    parsed_client_ephemeral_public_key = parse_ephemeral_public_key(
        client_ephemeral_public_bytes
    )
    parsed_server_ephemeral_public_key = parse_ephemeral_public_key(
        server_ephemeral_public_bytes
    )

    client_arguments = {
        "station_id": "boat_001",
        "timestamp": 0x0102030405060708,
        "client_random": bytes(range(16)),
        "client_ephemeral_public_key": client_ephemeral_public_bytes,
    }
    client_digest = build_client_auth_digest(**client_arguments)
    client_signature = sign_transcript_digest(
        client_identity_private_key,
        client_digest,
    )

    server_arguments = {
        **client_arguments,
        "client_signature": client_signature,
        "server_random": bytes(range(16, 32)),
        "server_ephemeral_public_key": server_ephemeral_public_bytes,
    }
    server_digest = build_server_auth_digest(**server_arguments)
    server_signature = sign_transcript_digest(
        server_identity_private_key,
        server_digest,
    )

    session_arguments = {
        **server_arguments,
        "server_signature": server_signature,
    }
    session_transcript_hash = build_session_transcript_hash(
        **session_arguments
    )
    client_shared_secret = derive_ephemeral_shared_secret(
        client_ephemeral_private_key,
        parsed_server_ephemeral_public_key,
    )
    server_shared_secret = derive_ephemeral_shared_secret(
        server_ephemeral_private_key,
        parsed_client_ephemeral_public_key,
    )

    return {
        "client_identity_private_key": client_identity_private_key,
        "server_identity_private_key": server_identity_private_key,
        "client_ephemeral_private_key": client_ephemeral_private_key,
        "server_ephemeral_private_key": server_ephemeral_private_key,
        "parsed_client_ephemeral_public_key": (
            parsed_client_ephemeral_public_key
        ),
        "parsed_server_ephemeral_public_key": (
            parsed_server_ephemeral_public_key
        ),
        "client_ephemeral_public_bytes": client_ephemeral_public_bytes,
        "server_ephemeral_public_bytes": server_ephemeral_public_bytes,
        "client_arguments": client_arguments,
        "client_digest": client_digest,
        "client_signature": client_signature,
        "server_arguments": server_arguments,
        "server_digest": server_digest,
        "server_signature": server_signature,
        "session_arguments": session_arguments,
        "session_transcript_hash": session_transcript_hash,
        "client_shared_secret": client_shared_secret,
        "server_shared_secret": server_shared_secret,
    }


def test_protocol_constants_fix_curve_hash_domain_and_roles():
    assert ECDHE_CURVE.name == "secp256r1"
    assert ECDHE_CURVE.key_size == 256
    assert TRANSCRIPT_HASH.name == "sha256"
    assert TRANSCRIPT_HASH.digest_size == 32
    assert DOMAIN_CONTEXT == b"AISMIXER-UDPSEC-ECDHE"
    assert CLIENT_AUTH_LABEL == b"CLIENT-AUTH"
    assert SERVER_AUTH_LABEL == b"SERVER-AUTH"
    assert SESSION_TRANSCRIPT_LABEL == b"SESSION-TRANSCRIPT"
    assert len({
        CLIENT_AUTH_LABEL,
        SERVER_AUTH_LABEL,
        SESSION_TRANSCRIPT_LABEL,
    }) == 3


@pytest.mark.parametrize(
    ("builder", "arguments", "expected_hex"),
    (
        pytest.param(
            build_client_auth_digest,
            {
                "station_id": "лодка_⚓",
                "timestamp": 0x0102030405060708,
                "client_random": b"client-random",
                "client_ephemeral_public_key": b"client-ephemeral",
            },
            "71048db17aba3853082ee8ebccfb495a"
            "79a8bcdaf9c4a84a503dc154557dc1b7",
            id="client",
        ),
        pytest.param(
            build_server_auth_digest,
            {
                "station_id": "лодка_⚓",
                "timestamp": 0x0102030405060708,
                "client_random": b"client-random",
                "client_ephemeral_public_key": b"client-ephemeral",
                "client_signature": b"client-signature",
                "server_random": b"server-random",
                "server_ephemeral_public_key": b"server-ephemeral",
            },
            "a6ba92ec6a327665b777549b591efe1d"
            "949784d409d782e7de801b2896f2ff53",
            id="server",
        ),
        pytest.param(
            build_session_transcript_hash,
            {
                "station_id": "лодка_⚓",
                "timestamp": 0x0102030405060708,
                "client_random": b"client-random",
                "client_ephemeral_public_key": b"client-ephemeral",
                "client_signature": b"client-signature",
                "server_random": b"server-random",
                "server_ephemeral_public_key": b"server-ephemeral",
                "server_signature": b"server-signature",
            },
            "baa61ee499efc953d4a3c7cb30988d17"
            "ed02ddbb7f6797c08e20788fae8a9d28",
            id="session",
        ),
    ),
)
def test_canonical_digest_vectors(builder, arguments, expected_hex):
    assert builder(**arguments).hex() == expected_hex


@pytest.mark.parametrize(
    ("builder", "arguments"),
    [
        pytest.param(builder, arguments, id=name)
        for name, builder, arguments in BUILDERS
    ],
)
def test_digests_are_deterministic_bytes_of_sha256_length(builder, arguments):
    first = builder(**arguments)
    second = builder(**arguments)

    assert first == second
    assert type(first) is bytes
    assert len(first) == 32


@pytest.mark.parametrize(
    ("builder", "arguments", "field", "replacement"),
    _field_cases(
        lambda field: {
            "station_id": "boat_002",
            "timestamp": 1234567891,
            "client_random": b"Client-random",
            "client_ephemeral_public_key": b"Client-ephemeral",
            "client_signature": b"Client-signature",
            "server_random": b"Server-random",
            "server_ephemeral_public_key": b"Server-ephemeral",
            "server_signature": b"Server-signature",
        }[field]
    ),
)
def test_changing_each_transcript_field_changes_digest(
    builder,
    arguments,
    field,
    replacement,
):
    changed = dict(arguments)
    changed[field] = replacement

    assert builder(**changed) != builder(**arguments)


def test_client_server_and_session_roles_produce_distinct_digests():
    digests = {
        build_client_auth_digest(**CLIENT_ARGS),
        build_server_auth_digest(**SERVER_ARGS),
        build_session_transcript_hash(**SESSION_ARGS),
    }

    assert len(digests) == 3


def test_length_framing_prevents_ambiguous_field_concatenation():
    split_after_two = {
        **CLIENT_ARGS,
        "client_random": b"ab",
        "client_ephemeral_public_key": b"c",
    }
    split_after_one = {
        **CLIENT_ARGS,
        "client_random": b"a",
        "client_ephemeral_public_key": b"bc",
    }

    assert b"ab" + b"c" == b"a" + b"bc"
    assert (
        build_client_auth_digest(**split_after_two)
        != build_client_auth_digest(**split_after_one)
    )


def test_binary_field_exceeding_framing_limit_is_rejected(monkeypatch):
    framing_limit = 32
    assert max(
        len(DOMAIN_CONTEXT),
        len(CLIENT_AUTH_LABEL),
        len(CLIENT_ARGS["station_id"].encode("utf-8")),
        8,
        len(CLIENT_ARGS["client_ephemeral_public_key"]),
    ) <= framing_limit
    monkeypatch.setattr(
        udpsec_crypto,
        "_MAX_FRAMED_FIELD_LENGTH",
        framing_limit,
    )
    arguments = {
        **CLIENT_ARGS,
        "client_random": b"x" * (framing_limit + 1),
    }

    with pytest.raises(
        ValueError,
        match="transcript field exceeds unsigned 32-bit framing",
    ):
        build_client_auth_digest(**arguments)


def test_utf8_station_ids_are_deterministic_without_normalization():
    precomposed = {**CLIENT_ARGS, "station_id": "bååt_⚓"}
    decomposed = {**CLIENT_ARGS, "station_id": "ba\u030aat_⚓"}

    assert (
        build_client_auth_digest(**precomposed)
        == build_client_auth_digest(**precomposed)
    )
    assert precomposed["station_id"].encode("utf-8") != decomposed[
        "station_id"
    ].encode("utf-8")
    assert (
        build_client_auth_digest(**precomposed)
        != build_client_auth_digest(**decomposed)
    )


@pytest.mark.parametrize("timestamp", (0, (1 << 64) - 1))
@pytest.mark.parametrize(
    ("builder", "arguments"),
    [
        pytest.param(builder, arguments, id=name)
        for name, builder, arguments in BUILDERS
    ],
)
def test_unsigned_64_bit_timestamp_boundaries_are_supported(
    builder,
    arguments,
    timestamp,
):
    changed = {**arguments, "timestamp": timestamp}

    assert len(builder(**changed)) == 32


@pytest.mark.parametrize("timestamp", (-1, 1 << 64))
def test_out_of_range_timestamps_are_rejected(timestamp):
    arguments = {**CLIENT_ARGS, "timestamp": timestamp}

    with pytest.raises(ValueError, match="unsigned 64-bit"):
        build_client_auth_digest(**arguments)


@pytest.mark.parametrize("timestamp", (False, True))
@pytest.mark.parametrize(
    ("builder", "arguments"),
    [
        pytest.param(builder, arguments, id=name)
        for name, builder, arguments in BUILDERS
    ],
)
def test_bool_timestamps_are_rejected(builder, arguments, timestamp):
    changed = {**arguments, "timestamp": timestamp}

    with pytest.raises(TypeError, match="timestamp must be an integer"):
        builder(**changed)


@pytest.mark.parametrize(
    ("builder", "arguments", "field", "replacement"),
    _field_cases(
        lambda field: {
            "station_id": b"boat_001",
            "timestamp": 1234.5,
            "client_random": "client-random",
            "client_ephemeral_public_key": "client-ephemeral",
            "client_signature": "client-signature",
            "server_random": "server-random",
            "server_ephemeral_public_key": "server-ephemeral",
            "server_signature": "server-signature",
        }[field]
    ),
)
def test_unsupported_input_types_are_rejected(
    builder,
    arguments,
    field,
    replacement,
):
    changed = dict(arguments)
    changed[field] = replacement

    with pytest.raises(TypeError):
        builder(**changed)


@pytest.mark.parametrize(
    "unsupported",
    (
        13,
        True,
        [99, 108, 105, 101, 110, 116],
        _BytesConvertible(),
    ),
)
def test_conversion_capable_non_byte_inputs_are_rejected(unsupported):
    changed = {**CLIENT_ARGS, "client_random": unsupported}

    with pytest.raises(TypeError, match="client_random must be bytes"):
        build_client_auth_digest(**changed)


@pytest.mark.parametrize(
    ("builder", "arguments", "field", "replacement"),
    _binary_field_cases(b""),
)
def test_empty_binary_fields_are_rejected(
    builder,
    arguments,
    field,
    replacement,
):
    changed = dict(arguments)
    changed[field] = replacement

    with pytest.raises(ValueError, match=f"{field} must not be empty"):
        builder(**changed)


@pytest.mark.parametrize("empty", (bytearray(), memoryview(b"")))
def test_empty_mutable_bytes_like_inputs_are_rejected(empty):
    changed = {**CLIENT_ARGS, "client_random": empty}

    with pytest.raises(ValueError, match="client_random must not be empty"):
        build_client_auth_digest(**changed)


@pytest.mark.parametrize(
    ("builder", "arguments"),
    [
        pytest.param(builder, arguments, id=name)
        for name, builder, arguments in BUILDERS
    ],
)
def test_empty_station_ids_are_rejected(builder, arguments):
    changed = {**arguments, "station_id": ""}

    with pytest.raises(ValueError, match="station_id must not be empty"):
        builder(**changed)


def test_bytearray_and_memoryview_fields_are_normalized_to_bytes():
    expected = build_session_transcript_hash(**SESSION_ARGS)
    binary_fields = {
        field
        for field in SESSION_ARGS
        if field not in ("station_id", "timestamp")
    }
    bytearrays = {
        field: bytearray(value) if field in binary_fields else value
        for field, value in SESSION_ARGS.items()
    }
    memoryviews = {
        field: memoryview(value) if field in binary_fields else value
        for field, value in SESSION_ARGS.items()
    }

    assert build_session_transcript_hash(**bytearrays) == expected
    assert build_session_transcript_hash(**memoryviews) == expected


def test_mutable_binary_inputs_are_snapshotted_before_hashing():
    client_random = bytearray(b"client-random")
    arguments = {**CLIENT_ARGS, "client_random": client_random}
    before_mutation = build_client_auth_digest(**arguments)

    client_random[0] = ord("C")

    assert before_mutation == build_client_auth_digest(**CLIENT_ARGS)
    assert before_mutation != build_client_auth_digest(**arguments)


def test_ephemeral_key_helpers_are_public_exports():
    assert {
        "derive_ephemeral_shared_secret",
        "generate_ephemeral_private_key",
        "parse_ephemeral_public_key",
        "serialize_ephemeral_public_key",
    } <= set(udpsec_crypto.__all__)


def test_generated_ephemeral_private_keys_are_fresh_p256_keys():
    first = generate_ephemeral_private_key()
    second = generate_ephemeral_private_key()

    for private_key in (first, second):
        assert isinstance(private_key, ec.EllipticCurvePrivateKey)
        assert isinstance(private_key.curve, ec.SECP256R1)
        assert private_key.curve.key_size == 256

    assert first is not second
    assert (
        serialize_ephemeral_public_key(first.public_key())
        != serialize_ephemeral_public_key(second.public_key())
    )


def test_ephemeral_generation_calls_the_backend_for_each_invocation(
    monkeypatch,
):
    expected = [_p256_private_key(1), _p256_private_key(2)]
    seen_curves = []

    def fake_generate_private_key(curve):
        seen_curves.append(curve)
        return expected[len(seen_curves) - 1]

    monkeypatch.setattr(
        udpsec_crypto.ec,
        "generate_private_key",
        fake_generate_private_key,
    )

    assert generate_ephemeral_private_key() is expected[0]
    assert generate_ephemeral_private_key() is expected[1]
    assert len(seen_curves) == 2
    assert all(
        isinstance(curve, ec.SECP256R1)
        for curve in seen_curves
    )


def test_generated_private_keys_are_not_cached_in_module_state():
    generate_ephemeral_private_key()

    assert not any(
        isinstance(value, ec.EllipticCurvePrivateKey)
        for value in vars(udpsec_crypto).values()
    )


def test_generated_public_key_uses_strict_compressed_encoding():
    private_key = generate_ephemeral_private_key()
    encoded = serialize_ephemeral_public_key(private_key.public_key())

    assert type(encoded) is bytes
    assert len(encoded) == 33
    assert encoded[0] in (0x02, 0x03)
    assert (
        serialize_ephemeral_public_key(private_key.public_key())
        == encoded
    )


def test_deterministic_public_key_serialization_vector():
    public_key = _p256_private_key(1).public_key()

    assert (
        serialize_ephemeral_public_key(public_key)
        == P256_PUBLIC_KEY_SCALAR_1
    )


@pytest.mark.parametrize("wrong_type", (None, b"encoded", object()))
def test_public_key_serialization_rejects_wrong_types(wrong_type):
    with pytest.raises(
        TypeError,
        match="public_key must be an EllipticCurvePublicKey",
    ):
        serialize_ephemeral_public_key(wrong_type)


def test_public_key_serialization_does_not_accept_private_keys():
    with pytest.raises(
        TypeError,
        match="public_key must be an EllipticCurvePublicKey",
    ):
        serialize_ephemeral_public_key(_p256_private_key(1))


def test_public_key_serialization_rejects_other_curves():
    with pytest.raises(
        ValueError,
        match="public_key must use SECP256R1/P-256",
    ):
        serialize_ephemeral_public_key(
            _p384_private_key().public_key()
        )


def test_generated_compressed_public_key_round_trips():
    encoded = serialize_ephemeral_public_key(
        generate_ephemeral_private_key().public_key()
    )
    parsed = parse_ephemeral_public_key(encoded)

    assert isinstance(parsed, ec.EllipticCurvePublicKey)
    assert isinstance(parsed.curve, ec.SECP256R1)
    assert parsed.curve.key_size == 256
    assert serialize_ephemeral_public_key(parsed) == encoded


@pytest.mark.parametrize(
    "container",
    (bytes, bytearray, memoryview),
)
def test_public_key_parser_accepts_supported_bytes_like_types(container):
    parsed = parse_ephemeral_public_key(
        container(P256_PUBLIC_KEY_SCALAR_1)
    )

    assert (
        serialize_ephemeral_public_key(parsed)
        == P256_PUBLIC_KEY_SCALAR_1
    )


@pytest.mark.parametrize(
    "use_memoryview",
    (
        pytest.param(False, id="bytearray"),
        pytest.param(True, id="memoryview"),
    ),
)
def test_public_key_parser_snapshots_mutable_input(use_memoryview):
    storage = bytearray(P256_PUBLIC_KEY_SCALAR_1)
    encoded = memoryview(storage) if use_memoryview else storage
    parsed = parse_ephemeral_public_key(encoded)

    storage[-1] ^= 0xFF

    assert (
        serialize_ephemeral_public_key(parsed)
        == P256_PUBLIC_KEY_SCALAR_1
    )


@pytest.mark.parametrize(
    "empty",
    (b"", bytearray(), memoryview(b"")),
)
def test_public_key_parser_rejects_empty_values(empty):
    with pytest.raises(
        ValueError,
        match="encoded ephemeral public key must not be empty",
    ):
        parse_ephemeral_public_key(empty)


@pytest.mark.parametrize(
    "encoded",
    (
        pytest.param(b"\x02" + b"\x00" * 31, id="32-bytes"),
        pytest.param(b"\x02" + b"\x00" * 33, id="34-bytes"),
        pytest.param(
            _p256_private_key(1).public_key().public_bytes(
                encoding=serialization.Encoding.X962,
                format=serialization.PublicFormat.UncompressedPoint,
            ),
            id="65-byte-uncompressed-point",
        ),
    ),
)
def test_public_key_parser_rejects_wrong_lengths(encoded):
    with pytest.raises(
        ValueError,
        match="encoded ephemeral public key must be exactly 33 bytes",
    ):
        parse_ephemeral_public_key(encoded)


@pytest.mark.parametrize("prefix", (0x00, 0x04, 0x06, 0x07))
def test_public_key_parser_rejects_non_compressed_prefixes(prefix):
    encoded = bytes((prefix,)) + b"\x00" * 32

    with pytest.raises(
        ValueError,
        match="must start with 0x02 or 0x03",
    ):
        parse_ephemeral_public_key(encoded)


def test_public_key_parser_normalizes_malformed_point_errors():
    malformed = b"\x02" + b"\xff" * 32

    with pytest.raises(ValueError) as caught:
        parse_ephemeral_public_key(malformed)

    assert str(caught.value) == (
        "encoded ephemeral public key is not a valid canonical compressed "
        "P-256 point"
    )
    assert caught.value.__cause__ is None


@pytest.mark.parametrize(
    "unsupported",
    (
        None,
        "encoded",
        33,
        [0x02] + [0x00] * 32,
        _BytesConvertible(),
    ),
)
def test_public_key_parser_rejects_unsupported_types(unsupported):
    with pytest.raises(
        TypeError,
        match="encoded ephemeral public key must be bytes",
    ):
        parse_ephemeral_public_key(unsupported)


def test_independently_generated_peers_derive_the_same_secret():
    first = generate_ephemeral_private_key()
    second = generate_ephemeral_private_key()

    first_secret = derive_ephemeral_shared_secret(
        first,
        second.public_key(),
    )
    second_secret = derive_ephemeral_shared_secret(
        second,
        first.public_key(),
    )

    assert first_secret == second_secret
    assert type(first_secret) is bytes
    assert len(first_secret) == 32


def test_deterministic_ephemeral_shared_secret_vector():
    first = _p256_private_key(1)
    second = _p256_private_key(2)

    first_secret = derive_ephemeral_shared_secret(
        first,
        second.public_key(),
    )
    second_secret = derive_ephemeral_shared_secret(
        second,
        first.public_key(),
    )

    assert first_secret == P256_SHARED_SECRET_SCALARS_1_AND_2
    assert second_secret == first_secret
    assert type(first_secret) is bytes
    assert len(first_secret) == 32


def test_raw_shared_secret_preserves_leading_zero_padding():
    first = _p256_private_key(1)
    peer = _p256_private_key(379)

    shared_secret = derive_ephemeral_shared_secret(
        first,
        peer.public_key(),
    )

    assert shared_secret == P256_SHARED_SECRET_SCALARS_1_AND_379
    assert len(shared_secret) == 32
    assert shared_secret[0] == 0


def test_changing_either_ephemeral_key_changes_the_shared_secret():
    first = _p256_private_key(1)
    second = _p256_private_key(2)
    third = _p256_private_key(3)

    baseline = derive_ephemeral_shared_secret(
        first,
        second.public_key(),
    )
    changed_private = derive_ephemeral_shared_secret(
        third,
        second.public_key(),
    )
    changed_peer = derive_ephemeral_shared_secret(
        first,
        third.public_key(),
    )

    assert baseline != changed_private
    assert baseline != changed_peer


def test_ephemeral_shared_secret_rejects_wrong_key_types():
    private_key = _p256_private_key(1)
    public_key = private_key.public_key()

    with pytest.raises(
        TypeError,
        match="private_key must be an EllipticCurvePrivateKey",
    ):
        derive_ephemeral_shared_secret(public_key, public_key)

    with pytest.raises(
        TypeError,
        match="peer_public_key must be an EllipticCurvePublicKey",
    ):
        derive_ephemeral_shared_secret(private_key, private_key)

    with pytest.raises(
        TypeError,
        match="peer_public_key must be an EllipticCurvePublicKey",
    ):
        derive_ephemeral_shared_secret(
            private_key,
            P256_PUBLIC_KEY_SCALAR_1,
        )


def test_ephemeral_shared_secret_rejects_wrong_private_curve():
    with pytest.raises(
        ValueError,
        match="private_key must use SECP256R1/P-256",
    ):
        derive_ephemeral_shared_secret(
            _p384_private_key(),
            _p256_private_key(1).public_key(),
        )


def test_ephemeral_shared_secret_rejects_wrong_public_curve():
    with pytest.raises(
        ValueError,
        match="peer_public_key must use SECP256R1/P-256",
    ):
        derive_ephemeral_shared_secret(
            _p256_private_key(1),
            _p384_private_key().public_key(),
        )


def test_transcript_signature_helpers_are_public_exports():
    assert {
        "sign_transcript_digest",
        "verify_transcript_signature",
    } <= set(udpsec_crypto.__all__)


def test_transcript_signing_returns_strict_low_s_der_bytes():
    private_key = _p256_private_key(7)
    signature = sign_transcript_digest(
        private_key,
        TEST_TRANSCRIPT_DIGEST,
    )
    r, s = utils.decode_dss_signature(signature)

    assert type(signature) is bytes
    assert signature
    assert 1 <= r < P256_ORDER
    assert 1 <= s < P256_ORDER
    assert s <= P256_ORDER // 2
    assert utils.encode_dss_signature(r, s) == signature


def test_repeated_transcript_signatures_are_low_s_and_verify():
    private_key = _p256_private_key(7)
    public_key = private_key.public_key()

    for _ in range(12):
        signature = sign_transcript_digest(
            private_key,
            TEST_TRANSCRIPT_DIGEST,
        )
        _, s = utils.decode_dss_signature(signature)

        assert s <= P256_ORDER // 2
        assert verify_transcript_signature(
            public_key,
            signature,
            TEST_TRANSCRIPT_DIGEST,
        ) is True


@pytest.mark.parametrize("container", (bytearray, memoryview))
def test_transcript_signing_accepts_mutable_digest_types(container):
    private_key = _p256_private_key(7)
    signature = sign_transcript_digest(
        private_key,
        container(TEST_TRANSCRIPT_DIGEST),
    )

    assert verify_transcript_signature(
        private_key.public_key(),
        signature,
        TEST_TRANSCRIPT_DIGEST,
    ) is True


@pytest.mark.parametrize(
    "digest",
    (
        None,
        "digest",
        32,
        list(TEST_TRANSCRIPT_DIGEST),
        _BytesConvertible(),
    ),
)
def test_transcript_signing_rejects_wrong_digest_types(digest):
    with pytest.raises(
        TypeError,
        match="digest must be bytes, bytearray, or memoryview",
    ):
        sign_transcript_digest(_p256_private_key(7), digest)


@pytest.mark.parametrize(
    "digest",
    (
        pytest.param(b"", id="zero"),
        pytest.param(b"x" * 31, id="31"),
        pytest.param(b"x" * 33, id="33"),
    ),
)
def test_transcript_signing_rejects_wrong_digest_lengths(digest):
    with pytest.raises(
        ValueError,
        match="digest must be exactly 32 bytes",
    ):
        sign_transcript_digest(_p256_private_key(7), digest)


@pytest.mark.parametrize("private_key", (None, b"key", object()))
def test_transcript_signing_rejects_wrong_private_key_types(private_key):
    with pytest.raises(
        TypeError,
        match="private_key must be an EllipticCurvePrivateKey",
    ):
        sign_transcript_digest(private_key, TEST_TRANSCRIPT_DIGEST)


def test_transcript_signing_does_not_accept_public_keys():
    private_key = _p256_private_key(7)

    with pytest.raises(
        TypeError,
        match="private_key must be an EllipticCurvePrivateKey",
    ):
        sign_transcript_digest(
            private_key.public_key(),
            TEST_TRANSCRIPT_DIGEST,
        )


def test_transcript_signing_rejects_other_private_key_curves():
    with pytest.raises(
        ValueError,
        match="private_key must use SECP256R1/P-256",
    ):
        sign_transcript_digest(
            _p384_private_key(),
            TEST_TRANSCRIPT_DIGEST,
        )


def test_transcript_signature_verification_returns_exact_bool():
    private_key = _p256_private_key(7)
    signature = sign_transcript_digest(
        private_key,
        TEST_TRANSCRIPT_DIGEST,
    )
    changed_digest = bytearray(TEST_TRANSCRIPT_DIGEST)
    changed_digest[-1] ^= 0x01

    assert verify_transcript_signature(
        private_key.public_key(),
        signature,
        TEST_TRANSCRIPT_DIGEST,
    ) is True
    assert verify_transcript_signature(
        private_key.public_key(),
        signature,
        changed_digest,
    ) is False
    assert verify_transcript_signature(
        _p256_private_key(8).public_key(),
        signature,
        TEST_TRANSCRIPT_DIGEST,
    ) is False


def test_client_signature_does_not_verify_for_server_or_session_digest():
    identity_key = _p256_private_key(7)
    client_digest = build_client_auth_digest(**CLIENT_ARGS)
    client_signature = sign_transcript_digest(
        identity_key,
        client_digest,
    )
    server_arguments = {
        **SERVER_ARGS,
        "client_signature": client_signature,
    }
    server_digest = build_server_auth_digest(**server_arguments)
    server_signature = sign_transcript_digest(
        identity_key,
        server_digest,
    )
    session_digest = build_session_transcript_hash(
        **server_arguments,
        server_signature=server_signature,
    )

    assert verify_transcript_signature(
        identity_key.public_key(),
        client_signature,
        server_digest,
    ) is False
    assert verify_transcript_signature(
        identity_key.public_key(),
        client_signature,
        session_digest,
    ) is False


@pytest.mark.parametrize(
    ("builder", "arguments", "field", "replacement"),
    _field_cases(
        lambda field: {
            "station_id": "boat_002",
            "timestamp": 1234567891,
            "client_random": b"Client-random",
            "client_ephemeral_public_key": b"Client-ephemeral",
            "client_signature": b"Client-signature",
            "server_random": b"Server-random",
            "server_ephemeral_public_key": b"Server-ephemeral",
            "server_signature": b"Server-signature",
        }[field]
    ),
)
def test_signature_fails_when_each_authenticated_field_changes(
    builder,
    arguments,
    field,
    replacement,
):
    identity_key = _p256_private_key(7)
    digest = builder(**arguments)
    signature = sign_transcript_digest(identity_key, digest)
    changed = {**arguments, field: replacement}

    assert verify_transcript_signature(
        identity_key.public_key(),
        signature,
        builder(**changed),
    ) is False


@pytest.mark.parametrize(
    "signature_container",
    (bytes, bytearray, memoryview),
)
@pytest.mark.parametrize(
    "digest_container",
    (bytes, bytearray, memoryview),
)
def test_signature_verification_accepts_supported_bytes_like_types(
    signature_container,
    digest_container,
):
    private_key = _p256_private_key(7)
    signature = sign_transcript_digest(
        private_key,
        TEST_TRANSCRIPT_DIGEST,
    )

    assert verify_transcript_signature(
        private_key.public_key(),
        signature_container(signature),
        digest_container(TEST_TRANSCRIPT_DIGEST),
    ) is True


def test_transcript_signing_snapshots_mutable_digest_input():
    private_key = _p256_private_key(7)
    digest_storage = bytearray(TEST_TRANSCRIPT_DIGEST)
    signature = sign_transcript_digest(
        private_key,
        memoryview(digest_storage),
    )
    original_digest = bytes(digest_storage)

    digest_storage[0] ^= 0xFF

    assert verify_transcript_signature(
        private_key.public_key(),
        signature,
        original_digest,
    ) is True
    assert verify_transcript_signature(
        private_key.public_key(),
        signature,
        digest_storage,
    ) is False


def test_signature_verification_snapshots_mutable_inputs():
    private_key = _p256_private_key(7)
    signature_storage = bytearray(
        sign_transcript_digest(private_key, TEST_TRANSCRIPT_DIGEST)
    )
    digest_storage = bytearray(TEST_TRANSCRIPT_DIGEST)

    result = verify_transcript_signature(
        private_key.public_key(),
        memoryview(signature_storage),
        memoryview(digest_storage),
    )
    signature_storage[-1] ^= 0x01
    digest_storage[0] ^= 0xFF

    assert result is True
    assert verify_transcript_signature(
        private_key.public_key(),
        signature_storage,
        digest_storage,
    ) is False


@pytest.mark.parametrize("public_key", (None, b"key", object()))
def test_signature_verification_rejects_wrong_public_key_types(public_key):
    with pytest.raises(
        TypeError,
        match="public_key must be an EllipticCurvePublicKey",
    ):
        verify_transcript_signature(
            public_key,
            b"signature",
            TEST_TRANSCRIPT_DIGEST,
        )


def test_signature_verification_does_not_accept_private_keys():
    private_key = _p256_private_key(7)

    with pytest.raises(
        TypeError,
        match="public_key must be an EllipticCurvePublicKey",
    ):
        verify_transcript_signature(
            private_key,
            b"signature",
            TEST_TRANSCRIPT_DIGEST,
        )


def test_signature_verification_rejects_other_public_key_curves():
    with pytest.raises(
        ValueError,
        match="public_key must use SECP256R1/P-256",
    ):
        verify_transcript_signature(
            _p384_private_key().public_key(),
            b"signature",
            TEST_TRANSCRIPT_DIGEST,
        )


@pytest.mark.parametrize(
    "signature",
    (
        None,
        "signature",
        70,
        [0x30, 0x00],
        _BytesConvertible(),
    ),
)
def test_signature_verification_rejects_unsupported_signature_types(
    signature,
):
    with pytest.raises(
        TypeError,
        match="signature must be bytes, bytearray, or memoryview",
    ):
        verify_transcript_signature(
            _p256_private_key(7).public_key(),
            signature,
            TEST_TRANSCRIPT_DIGEST,
        )


@pytest.mark.parametrize(
    "signature",
    (b"", bytearray(), memoryview(b"")),
)
def test_signature_verification_rejects_empty_signatures(signature):
    assert verify_transcript_signature(
        _p256_private_key(7).public_key(),
        signature,
        TEST_TRANSCRIPT_DIGEST,
    ) is False


@pytest.mark.parametrize(
    "digest",
    (
        None,
        "digest",
        32,
        list(TEST_TRANSCRIPT_DIGEST),
        _BytesConvertible(),
    ),
)
def test_signature_verification_rejects_wrong_digest_types(digest):
    with pytest.raises(
        TypeError,
        match="digest must be bytes, bytearray, or memoryview",
    ):
        verify_transcript_signature(
            _p256_private_key(7).public_key(),
            b"signature",
            digest,
        )


@pytest.mark.parametrize(
    "digest",
    (
        pytest.param(b"", id="zero"),
        pytest.param(b"x" * 31, id="31"),
        pytest.param(b"x" * 33, id="33"),
    ),
)
def test_signature_verification_rejects_wrong_digest_lengths(digest):
    with pytest.raises(
        ValueError,
        match="digest must be exactly 32 bytes",
    ):
        verify_transcript_signature(
            _p256_private_key(7).public_key(),
            b"signature",
            digest,
        )


@pytest.mark.parametrize(
    "signature",
    (
        pytest.param(
            bytes.fromhex("30060201010201"),
            id="truncated",
        ),
        pytest.param(
            bytes.fromhex("3106020101020101"),
            id="wrong-sequence-tag",
        ),
        pytest.param(
            bytes.fromhex("300702020001020101"),
            id="non-minimal-integer",
        ),
        pytest.param(
            bytes.fromhex("308106020101020101"),
            id="non-minimal-length",
        ),
    ),
)
def test_signature_verification_rejects_malformed_or_noncanonical_der(
    signature,
):
    assert verify_transcript_signature(
        _p256_private_key(7).public_key(),
        signature,
        TEST_TRANSCRIPT_DIGEST,
    ) is False


def test_signature_verification_rejects_der_with_trailing_bytes():
    private_key = _p256_private_key(7)
    signature = sign_transcript_digest(
        private_key,
        TEST_TRANSCRIPT_DIGEST,
    )

    assert verify_transcript_signature(
        private_key.public_key(),
        signature + b"\x00",
        TEST_TRANSCRIPT_DIGEST,
    ) is False


@pytest.mark.parametrize(
    ("r", "s"),
    (
        pytest.param(0, 1, id="zero-r"),
        pytest.param(1, 0, id="zero-s"),
        pytest.param(P256_ORDER, 1, id="order-r"),
        pytest.param(1, P256_ORDER, id="order-s"),
        pytest.param(P256_ORDER + 1, 1, id="above-order-r"),
        pytest.param(1, P256_ORDER + 1, id="above-order-s"),
    ),
)
def test_signature_verification_rejects_out_of_range_scalars(r, s):
    signature = utils.encode_dss_signature(r, s)

    assert verify_transcript_signature(
        _p256_private_key(7).public_key(),
        signature,
        TEST_TRANSCRIPT_DIGEST,
    ) is False


@pytest.mark.parametrize(
    "signature",
    (
        pytest.param(
            bytes.fromhex("30060201ff020101"),
            id="negative-r",
        ),
        pytest.param(
            bytes.fromhex("30060201010201ff"),
            id="negative-s",
        ),
    ),
)
def test_signature_verification_rejects_negative_der_scalars(signature):
    assert verify_transcript_signature(
        _p256_private_key(7).public_key(),
        signature,
        TEST_TRANSCRIPT_DIGEST,
    ) is False


def test_signature_verification_rejects_in_range_invalid_signature():
    signature = utils.encode_dss_signature(1, 1)

    assert verify_transcript_signature(
        _p256_private_key(7).public_key(),
        signature,
        TEST_TRANSCRIPT_DIGEST,
    ) is False


def test_signature_verification_rejects_high_s_twin():
    private_key = _p256_private_key(7)
    public_key = private_key.public_key()
    low_signature = sign_transcript_digest(
        private_key,
        TEST_TRANSCRIPT_DIGEST,
    )
    r, low_s = utils.decode_dss_signature(low_signature)
    high_s = P256_ORDER - low_s
    high_signature = utils.encode_dss_signature(r, high_s)

    assert low_s <= P256_ORDER // 2
    assert high_s > P256_ORDER // 2
    assert utils.encode_dss_signature(r, high_s) == high_signature

    try:
        public_key.verify(
            high_signature,
            TEST_TRANSCRIPT_DIGEST,
            ec.ECDSA(utils.Prehashed(hashes.SHA256())),
        )
    except InvalidSignature:
        pass

    assert verify_transcript_signature(
        public_key,
        low_signature,
        TEST_TRANSCRIPT_DIGEST,
    ) is True
    assert verify_transcript_signature(
        public_key,
        high_signature,
        TEST_TRANSCRIPT_DIGEST,
    ) is False


def test_helper_signature_uses_prehashed_sha256_without_rehashing():
    private_key = _p256_private_key(7)
    public_key = private_key.public_key()
    signature = sign_transcript_digest(
        private_key,
        TEST_TRANSCRIPT_DIGEST,
    )

    public_key.verify(
        signature,
        TEST_TRANSCRIPT_DIGEST,
        ec.ECDSA(utils.Prehashed(hashes.SHA256())),
    )
    with pytest.raises(InvalidSignature):
        public_key.verify(
            signature,
            TEST_TRANSCRIPT_DIGEST,
            ec.ECDSA(hashes.SHA256()),
        )


def test_helper_verification_uses_prehashed_sha256_without_rehashing():
    private_key = _p256_private_key(7)
    public_key = private_key.public_key()
    prehashed_signature = _to_low_s(
        private_key.sign(
            TEST_TRANSCRIPT_DIGEST,
            ec.ECDSA(utils.Prehashed(hashes.SHA256())),
        )
    )
    ordinary_signature = _to_low_s(
        private_key.sign(
            TEST_TRANSCRIPT_DIGEST,
            ec.ECDSA(hashes.SHA256()),
        )
    )

    assert verify_transcript_signature(
        public_key,
        prehashed_signature,
        TEST_TRANSCRIPT_DIGEST,
    ) is True
    assert verify_transcript_signature(
        public_key,
        ordinary_signature,
        TEST_TRANSCRIPT_DIGEST,
    ) is False


def test_session_key_api_is_public_frozen_slot_based_and_secret_safe():
    assert "SessionKeyMaterial" in udpsec_crypto.__all__
    assert "derive_session_key_material" in udpsec_crypto.__all__
    assert is_dataclass(SessionKeyMaterial)
    assert SessionKeyMaterial.__dataclass_params__.frozen is True
    assert SessionKeyMaterial.__slots__ == (
        "client_to_server_key",
        "server_to_client_key",
    )
    assert tuple(item.name for item in fields(SessionKeyMaterial)) == (
        "client_to_server_key",
        "server_to_client_key",
    )

    material = SessionKeyMaterial(
        client_to_server_key=b"\x01" * 32,
        server_to_client_key=b"\x02" * 32,
    )

    assert isinstance(material.client_to_server_key, bytes)
    assert isinstance(material.server_to_client_key, bytes)
    assert len(material.client_to_server_key) == 32
    assert len(material.server_to_client_key) == 32
    assert not hasattr(material, "__dict__")
    assert not hasattr(material, "shared_secret")
    assert not hasattr(material, "session_transcript_hash")
    assert repr(material) == "SessionKeyMaterial()"
    assert material.client_to_server_key.hex() not in repr(material)
    assert material.server_to_client_key.hex() not in repr(material)


@pytest.mark.parametrize(
    "field_name",
    ("client_to_server_key", "server_to_client_key"),
)
def test_session_key_material_rejects_assignment(field_name):
    material = SessionKeyMaterial(
        client_to_server_key=b"\x01" * 32,
        server_to_client_key=b"\x02" * 32,
    )

    with pytest.raises(FrozenInstanceError):
        setattr(material, field_name, b"\x03" * 32)


@pytest.mark.parametrize(
    "field_name",
    ("client_to_server_key", "server_to_client_key"),
)
@pytest.mark.parametrize(
    "value",
    (
        pytest.param(bytearray(32), id="bytearray"),
        pytest.param(memoryview(bytes(32)), id="memoryview"),
        pytest.param(None, id="none"),
        pytest.param("not-bytes", id="string"),
    ),
)
def test_session_key_material_rejects_non_bytes_fields(
    field_name,
    value,
):
    arguments = {
        "client_to_server_key": b"\x01" * 32,
        "server_to_client_key": b"\x02" * 32,
    }
    arguments[field_name] = value

    with pytest.raises(TypeError, match=f"{field_name} must be bytes"):
        SessionKeyMaterial(**arguments)


@pytest.mark.parametrize(
    "field_name",
    ("client_to_server_key", "server_to_client_key"),
)
@pytest.mark.parametrize("length", (0, 31, 33))
def test_session_key_material_rejects_wrong_key_lengths(
    field_name,
    length,
):
    arguments = {
        "client_to_server_key": b"\x01" * 32,
        "server_to_client_key": b"\x02" * 32,
    }
    arguments[field_name] = b"\x03" * length

    with pytest.raises(
        ValueError,
        match=f"{field_name} must be exactly 32 bytes",
    ):
        SessionKeyMaterial(**arguments)


def test_session_key_derivation_matches_fixed_hkdf_sha256_vectors():
    material = derive_session_key_material(
        P256_SHARED_SECRET_SCALARS_1_AND_2,
        TEST_TRANSCRIPT_DIGEST,
    )

    assert material.client_to_server_key == EXPECTED_CLIENT_TO_SERVER_KEY
    assert material.server_to_client_key == EXPECTED_SERVER_TO_CLIENT_KEY
    assert material.client_to_server_key != material.server_to_client_key


def test_session_key_vectors_match_independent_rfc5869_reference():
    assert _reference_session_key_info(
        b"CLIENT-TO-SERVER"
    ) == EXPECTED_CLIENT_TO_SERVER_INFO
    assert _reference_session_key_info(
        b"SERVER-TO-CLIENT"
    ) == EXPECTED_SERVER_TO_CLIENT_INFO
    assert _reference_hkdf_sha256(
        P256_SHARED_SECRET_SCALARS_1_AND_2,
        TEST_TRANSCRIPT_DIGEST,
        EXPECTED_CLIENT_TO_SERVER_INFO,
    ) == EXPECTED_CLIENT_TO_SERVER_KEY
    assert _reference_hkdf_sha256(
        P256_SHARED_SECRET_SCALARS_1_AND_2,
        TEST_TRANSCRIPT_DIGEST,
        EXPECTED_SERVER_TO_CLIENT_INFO,
    ) == EXPECTED_SERVER_TO_CLIENT_KEY


def test_session_key_derivation_is_deterministic_and_direction_separated():
    first = derive_session_key_material(
        P256_SHARED_SECRET_SCALARS_1_AND_2,
        TEST_TRANSCRIPT_DIGEST,
    )
    second = derive_session_key_material(
        P256_SHARED_SECRET_SCALARS_1_AND_2,
        TEST_TRANSCRIPT_DIGEST,
    )

    assert first == second
    assert first.client_to_server_key != first.server_to_client_key


def test_changing_each_hkdf_input_changes_both_directional_keys():
    baseline = derive_session_key_material(
        P256_SHARED_SECRET_SCALARS_1_AND_2,
        TEST_TRANSCRIPT_DIGEST,
    )
    changed_secret = bytearray(P256_SHARED_SECRET_SCALARS_1_AND_2)
    changed_secret[0] ^= 0x01
    changed_transcript_hash = bytearray(TEST_TRANSCRIPT_DIGEST)
    changed_transcript_hash[-1] ^= 0x01

    secret_changed = derive_session_key_material(
        changed_secret,
        TEST_TRANSCRIPT_DIGEST,
    )
    transcript_changed = derive_session_key_material(
        P256_SHARED_SECRET_SCALARS_1_AND_2,
        changed_transcript_hash,
    )

    assert (
        secret_changed.client_to_server_key
        != baseline.client_to_server_key
    )
    assert (
        secret_changed.server_to_client_key
        != baseline.server_to_client_key
    )
    assert (
        transcript_changed.client_to_server_key
        != baseline.client_to_server_key
    )
    assert (
        transcript_changed.server_to_client_key
        != baseline.server_to_client_key
    )


@pytest.mark.parametrize(
    "secret_container",
    (bytes, bytearray, memoryview),
)
@pytest.mark.parametrize(
    "transcript_container",
    (bytes, bytearray, memoryview),
)
def test_session_key_derivation_accepts_supported_bytes_like_inputs(
    secret_container,
    transcript_container,
):
    material = derive_session_key_material(
        secret_container(P256_SHARED_SECRET_SCALARS_1_AND_2),
        transcript_container(TEST_TRANSCRIPT_DIGEST),
    )

    assert material.client_to_server_key == EXPECTED_CLIENT_TO_SERVER_KEY
    assert material.server_to_client_key == EXPECTED_SERVER_TO_CLIENT_KEY
    assert type(material.client_to_server_key) is bytes
    assert type(material.server_to_client_key) is bytes


def test_session_key_derivation_snapshots_mutable_inputs_without_retaining_them():
    shared_secret = bytearray(P256_SHARED_SECRET_SCALARS_1_AND_2)
    transcript_hash = bytearray(TEST_TRANSCRIPT_DIGEST)
    material = derive_session_key_material(
        memoryview(shared_secret),
        transcript_hash,
    )

    shared_secret[0] ^= 0xFF
    transcript_hash[0] ^= 0xFF

    assert material == SessionKeyMaterial(
        client_to_server_key=EXPECTED_CLIENT_TO_SERVER_KEY,
        server_to_client_key=EXPECTED_SERVER_TO_CLIENT_KEY,
    )
    assert SessionKeyMaterial.__slots__ == (
        "client_to_server_key",
        "server_to_client_key",
    )


@pytest.mark.parametrize(
    "argument_name",
    ("shared_secret", "session_transcript_hash"),
)
@pytest.mark.parametrize(
    "value",
    (
        pytest.param(None, id="none"),
        pytest.param("not-bytes", id="string"),
        pytest.param(32, id="integer"),
        pytest.param(_BytesConvertible(), id="bytes-convertible"),
    ),
)
def test_session_key_derivation_rejects_unsupported_input_types(
    argument_name,
    value,
):
    arguments = {
        "shared_secret": P256_SHARED_SECRET_SCALARS_1_AND_2,
        "session_transcript_hash": TEST_TRANSCRIPT_DIGEST,
    }
    arguments[argument_name] = value

    with pytest.raises(
        TypeError,
        match=(
            f"{argument_name} must be bytes, bytearray, or memoryview"
        ),
    ):
        derive_session_key_material(**arguments)


@pytest.mark.parametrize(
    "argument_name",
    ("shared_secret", "session_transcript_hash"),
)
@pytest.mark.parametrize("length", (0, 31, 33))
def test_session_key_derivation_rejects_wrong_input_lengths(
    argument_name,
    length,
):
    arguments = {
        "shared_secret": P256_SHARED_SECRET_SCALARS_1_AND_2,
        "session_transcript_hash": TEST_TRANSCRIPT_DIGEST,
    }
    arguments[argument_name] = b"\x00" * length

    with pytest.raises(
        ValueError,
        match=f"{argument_name} must be exactly 32 bytes",
    ):
        derive_session_key_material(**arguments)


@pytest.mark.parametrize(
    "argument_name",
    ("shared_secret", "session_transcript_hash"),
)
def test_session_key_derivation_rejects_released_memoryviews(argument_name):
    released_view = memoryview(bytearray(32))
    released_view.release()
    arguments = {
        "shared_secret": P256_SHARED_SECRET_SCALARS_1_AND_2,
        "session_transcript_hash": TEST_TRANSCRIPT_DIGEST,
    }
    arguments[argument_name] = released_view

    with pytest.raises(
        ValueError,
        match=f"{argument_name} must reference readable bytes",
    ):
        derive_session_key_material(**arguments)


def test_session_key_derivation_uses_two_independent_exact_hkdf_operations(
    monkeypatch,
):
    instances = []

    class _RecordingHKDF:
        def __init__(self, *, algorithm, length, salt, info):
            self.algorithm = algorithm
            self.length = length
            self.salt = salt
            self.info = info
            self.output_byte = len(instances) + 1
            self.key_material = None
            instances.append(self)

        def derive(self, key_material):
            self.key_material = key_material
            return bytes((self.output_byte,)) * self.length

    monkeypatch.setattr(udpsec_crypto, "HKDF", _RecordingHKDF)
    shared_secret = bytearray(P256_SHARED_SECRET_SCALARS_1_AND_2)
    transcript_hash = bytearray(TEST_TRANSCRIPT_DIGEST)

    material = derive_session_key_material(
        shared_secret,
        memoryview(transcript_hash),
    )

    assert len(instances) == 2
    assert instances[0] is not instances[1]
    assert [item.algorithm.name for item in instances] == [
        "sha256",
        "sha256",
    ]
    assert [item.length for item in instances] == [32, 32]
    assert [item.salt for item in instances] == [
        TEST_TRANSCRIPT_DIGEST,
        TEST_TRANSCRIPT_DIGEST,
    ]
    assert [item.info for item in instances] == [
        EXPECTED_CLIENT_TO_SERVER_INFO,
        EXPECTED_SERVER_TO_CLIENT_INFO,
    ]
    assert [item.key_material for item in instances] == [
        P256_SHARED_SECRET_SCALARS_1_AND_2,
        P256_SHARED_SECRET_SCALARS_1_AND_2,
    ]
    assert all(type(item.salt) is bytes for item in instances)
    assert all(type(item.key_material) is bytes for item in instances)
    assert material == SessionKeyMaterial(
        client_to_server_key=b"\x01" * 32,
        server_to_client_key=b"\x02" * 32,
    )


@pytest.mark.parametrize(
    "backend_output",
    (
        pytest.param(b"\x01" * 31, id="wrong-length"),
        pytest.param(bytearray(b"\x01" * 32), id="mutable"),
    ),
)
def test_session_key_derivation_rejects_invalid_backend_output(
    monkeypatch,
    backend_output,
):
    class _InvalidHKDF:
        def __init__(self, **_arguments):
            pass

        def derive(self, _key_material):
            return backend_output

    monkeypatch.setattr(udpsec_crypto, "HKDF", _InvalidHKDF)

    with pytest.raises(
        RuntimeError,
        match="HKDF-SHA256 did not produce a 32-byte session key",
    ):
        derive_session_key_material(
            P256_SHARED_SECRET_SCALARS_1_AND_2,
            TEST_TRANSCRIPT_DIGEST,
        )


def test_session_key_derivation_rejects_equal_directional_backend_outputs(
    monkeypatch,
):
    class _EqualHKDF:
        def __init__(self, **_arguments):
            pass

        def derive(self, _key_material):
            return b"\x01" * 32

    monkeypatch.setattr(udpsec_crypto, "HKDF", _EqualHKDF)

    with pytest.raises(
        RuntimeError,
        match="directional HKDF session keys must differ",
    ):
        derive_session_key_material(
            P256_SHARED_SECRET_SCALARS_1_AND_2,
            TEST_TRANSCRIPT_DIGEST,
        )


def test_directional_hkdf_info_is_exactly_framed_and_separated():
    assert len(EXPECTED_CLIENT_TO_SERVER_INFO) == 88
    assert len(EXPECTED_SERVER_TO_CLIENT_INFO) == 88
    assert (
        EXPECTED_CLIENT_TO_SERVER_INFO
        != EXPECTED_SERVER_TO_CLIENT_INFO
    )
    assert _reference_session_key_info(
        b"CLIENT-TO-SERVER"
    ) == EXPECTED_CLIENT_TO_SERVER_INFO
    assert _reference_session_key_info(
        b"SERVER-TO-CLIENT"
    ) == EXPECTED_SERVER_TO_CLIENT_INFO


@pytest.mark.parametrize(
    ("direction_label", "expected_info", "expected_key"),
    (
        pytest.param(
            b"CLIENT-TO-SERVER",
            EXPECTED_CLIENT_TO_SERVER_INFO,
            EXPECTED_CLIENT_TO_SERVER_KEY,
            id="client-to-server",
        ),
        pytest.param(
            b"SERVER-TO-CLIENT",
            EXPECTED_SERVER_TO_CLIENT_INFO,
            EXPECTED_SERVER_TO_CLIENT_KEY,
            id="server-to-client",
        ),
    ),
)
def test_replacing_or_reordering_any_hkdf_info_field_changes_the_key(
    direction_label,
    expected_info,
    expected_key,
):
    fields_in_order = (
        b"AISMIXER-UDPSEC-ECDHE",
        b"SESSION-KEY-SCHEDULE",
        direction_label,
        b"AES-256-GCM-KEY",
    )
    assert b"".join(
        _reference_frame(value) for value in fields_in_order
    ) == expected_info

    for index in range(len(fields_in_order)):
        changed_fields = list(fields_in_order)
        changed_fields[index] += b"!"
        changed_info = b"".join(
            _reference_frame(value) for value in changed_fields
        )
        assert changed_info != expected_info
        assert _reference_hkdf_sha256(
            P256_SHARED_SECRET_SCALARS_1_AND_2,
            TEST_TRANSCRIPT_DIGEST,
            changed_info,
        ) != expected_key

    for reordered_fields in itertools.permutations(fields_in_order):
        if reordered_fields == fields_in_order:
            continue
        reordered_info = b"".join(
            _reference_frame(value) for value in reordered_fields
        )
        assert reordered_info != expected_info
        assert _reference_hkdf_sha256(
            P256_SHARED_SECRET_SCALARS_1_AND_2,
            TEST_TRANSCRIPT_DIGEST,
            reordered_info,
        ) != expected_key


def test_pure_full_handshake_composition_derives_matching_directional_keys():
    handshake = _build_pure_handshake()

    assert serialize_ephemeral_public_key(
        handshake["parsed_client_ephemeral_public_key"]
    ) == handshake["client_ephemeral_public_bytes"]
    assert serialize_ephemeral_public_key(
        handshake["parsed_server_ephemeral_public_key"]
    ) == handshake["server_ephemeral_public_bytes"]
    assert verify_transcript_signature(
        handshake["client_identity_private_key"].public_key(),
        handshake["client_signature"],
        handshake["client_digest"],
    ) is True
    assert verify_transcript_signature(
        handshake["server_identity_private_key"].public_key(),
        handshake["server_signature"],
        handshake["server_digest"],
    ) is True
    assert (
        handshake["client_shared_secret"]
        == handshake["server_shared_secret"]
    )

    client_material = derive_session_key_material(
        handshake["client_shared_secret"],
        handshake["session_transcript_hash"],
    )
    server_material = derive_session_key_material(
        handshake["server_shared_secret"],
        handshake["session_transcript_hash"],
    )

    assert client_material.client_to_server_key == (
        server_material.client_to_server_key
    )
    assert client_material.server_to_client_key == (
        server_material.server_to_client_key
    )
    assert (
        client_material.client_to_server_key
        != client_material.server_to_client_key
    )


def test_changing_authenticated_field_changes_composed_session_keys():
    handshake = _build_pure_handshake()
    baseline_material = derive_session_key_material(
        handshake["client_shared_secret"],
        handshake["session_transcript_hash"],
    )
    changed_client_arguments = {
        **handshake["client_arguments"],
        "timestamp": handshake["client_arguments"]["timestamp"] + 1,
    }
    changed_client_digest = build_client_auth_digest(
        **changed_client_arguments
    )
    changed_session_arguments = {
        **handshake["session_arguments"],
        "timestamp": changed_client_arguments["timestamp"],
    }
    changed_transcript_hash = build_session_transcript_hash(
        **changed_session_arguments
    )
    changed_material = derive_session_key_material(
        handshake["client_shared_secret"],
        changed_transcript_hash,
    )

    assert verify_transcript_signature(
        handshake["client_identity_private_key"].public_key(),
        handshake["client_signature"],
        changed_client_digest,
    ) is False
    assert changed_transcript_hash != handshake["session_transcript_hash"]
    assert (
        changed_material.client_to_server_key
        != baseline_material.client_to_server_key
    )
    assert (
        changed_material.server_to_client_key
        != baseline_material.server_to_client_key
    )


def test_changing_client_ephemeral_key_changes_composed_session_keys():
    handshake = _build_pure_handshake()
    baseline_material = derive_session_key_material(
        handshake["client_shared_secret"],
        handshake["session_transcript_hash"],
    )
    changed_client_private_key = _p256_private_key(3)
    changed_client_public_bytes = serialize_ephemeral_public_key(
        changed_client_private_key.public_key()
    )
    changed_client_public_key = parse_ephemeral_public_key(
        changed_client_public_bytes
    )
    changed_client_secret = derive_ephemeral_shared_secret(
        changed_client_private_key,
        handshake["parsed_server_ephemeral_public_key"],
    )
    changed_server_secret = derive_ephemeral_shared_secret(
        handshake["server_ephemeral_private_key"],
        changed_client_public_key,
    )
    changed_client_arguments = {
        **handshake["client_arguments"],
        "client_ephemeral_public_key": changed_client_public_bytes,
    }
    changed_session_arguments = {
        **handshake["session_arguments"],
        "client_ephemeral_public_key": changed_client_public_bytes,
    }
    changed_transcript_hash = build_session_transcript_hash(
        **changed_session_arguments
    )
    changed_client_material = derive_session_key_material(
        changed_client_secret,
        changed_transcript_hash,
    )
    changed_server_material = derive_session_key_material(
        changed_server_secret,
        changed_transcript_hash,
    )

    assert changed_client_secret == changed_server_secret
    assert changed_client_secret != handshake["client_shared_secret"]
    assert verify_transcript_signature(
        handshake["client_identity_private_key"].public_key(),
        handshake["client_signature"],
        build_client_auth_digest(**changed_client_arguments),
    ) is False
    assert changed_client_material == changed_server_material
    assert (
        changed_client_material.client_to_server_key
        != baseline_material.client_to_server_key
    )
    assert (
        changed_client_material.server_to_client_key
        != baseline_material.server_to_client_key
    )


def test_changing_server_ephemeral_key_changes_composed_session_keys():
    handshake = _build_pure_handshake()
    baseline_material = derive_session_key_material(
        handshake["client_shared_secret"],
        handshake["session_transcript_hash"],
    )
    changed_server_private_key = _p256_private_key(4)
    changed_server_public_bytes = serialize_ephemeral_public_key(
        changed_server_private_key.public_key()
    )
    changed_server_public_key = parse_ephemeral_public_key(
        changed_server_public_bytes
    )
    changed_client_secret = derive_ephemeral_shared_secret(
        handshake["client_ephemeral_private_key"],
        changed_server_public_key,
    )
    changed_server_secret = derive_ephemeral_shared_secret(
        changed_server_private_key,
        handshake["parsed_client_ephemeral_public_key"],
    )
    changed_server_arguments = {
        **handshake["server_arguments"],
        "server_ephemeral_public_key": changed_server_public_bytes,
    }
    changed_session_arguments = {
        **handshake["session_arguments"],
        "server_ephemeral_public_key": changed_server_public_bytes,
    }
    changed_transcript_hash = build_session_transcript_hash(
        **changed_session_arguments
    )
    changed_client_material = derive_session_key_material(
        changed_client_secret,
        changed_transcript_hash,
    )
    changed_server_material = derive_session_key_material(
        changed_server_secret,
        changed_transcript_hash,
    )

    assert changed_client_secret == changed_server_secret
    assert changed_client_secret != handshake["client_shared_secret"]
    assert verify_transcript_signature(
        handshake["server_identity_private_key"].public_key(),
        handshake["server_signature"],
        build_server_auth_digest(**changed_server_arguments),
    ) is False
    assert changed_client_material == changed_server_material
    assert (
        changed_client_material.client_to_server_key
        != baseline_material.client_to_server_key
    )
    assert (
        changed_client_material.server_to_client_key
        != baseline_material.server_to_client_key
    )


def test_changing_identity_signature_changes_composed_session_keys():
    handshake = _build_pure_handshake()
    baseline_material = derive_session_key_material(
        handshake["client_shared_secret"],
        handshake["session_transcript_hash"],
    )
    changed_server_signature = handshake["server_signature"] + b"\x00"
    changed_session_arguments = {
        **handshake["session_arguments"],
        "server_signature": changed_server_signature,
    }
    changed_transcript_hash = build_session_transcript_hash(
        **changed_session_arguments
    )
    changed_material = derive_session_key_material(
        handshake["client_shared_secret"],
        changed_transcript_hash,
    )

    assert verify_transcript_signature(
        handshake["server_identity_private_key"].public_key(),
        changed_server_signature,
        handshake["server_digest"],
    ) is False
    assert changed_transcript_hash != handshake["session_transcript_hash"]
    assert (
        changed_material.client_to_server_key
        != baseline_material.client_to_server_key
    )
    assert (
        changed_material.server_to_client_key
        != baseline_material.server_to_client_key
    )
