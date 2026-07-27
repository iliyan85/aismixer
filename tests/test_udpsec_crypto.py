import pytest

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
