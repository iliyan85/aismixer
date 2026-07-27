import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

import core.udpsec_crypto as udpsec_crypto
from core.udpsec_crypto import (
    CLIENT_AUTH_LABEL,
    DOMAIN_CONTEXT,
    ECDHE_CURVE,
    SERVER_AUTH_LABEL,
    SESSION_TRANSCRIPT_LABEL,
    TRANSCRIPT_HASH,
    build_client_auth_digest,
    build_server_auth_digest,
    build_session_transcript_hash,
    derive_ephemeral_shared_secret,
    generate_ephemeral_private_key,
    parse_ephemeral_public_key,
    serialize_ephemeral_public_key,
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
