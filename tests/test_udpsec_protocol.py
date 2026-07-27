import base64
from dataclasses import FrozenInstanceError, fields, is_dataclass

import pytest

import core.udpsec_protocol as udpsec_protocol
from core.udpsec_protocol import (
    CLIENT_HELLO_PREFIX,
    ClientHello,
    SESSION_CONFIRMATION_SEQUENCE,
    SERVER_HELLO_PREFIX,
    ServerHello,
    build_client_hello_packet,
    build_server_hello_packet,
    parse_client_hello_packet,
    parse_server_hello_packet,
)


MAX_TIMESTAMP = (1 << 64) - 1
CLIENT_RANDOM = bytes(range(32))
SERVER_RANDOM = bytes(range(32, 64))
CLIENT_EPHEMERAL_PUBLIC_KEY = bytes.fromhex(
    "036b17d1f2e12c4247f8bce6e563a440"
    "f277037d812deb33a0f4a13945d898c296"
)
SERVER_EPHEMERAL_PUBLIC_KEY = bytes.fromhex(
    "037cf27b188d034f7e8a52380304b51ac"
    "3c08969e277f21b35a60b48fc47669978"
)
CLIENT_SIGNATURE = bytes.fromhex("3006020101020101")
SERVER_SIGNATURE = bytes.fromhex("3006020102020103")

CLIENT_RANDOM_B64 = (
    b"AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="
)
CLIENT_EPHEMERAL_PUBLIC_KEY_B64 = (
    b"A2sX0fLhLEJH+Lzm5WOkQPJ3A32BLeszoPShOUXYmMKW"
)
CLIENT_SIGNATURE_B64 = b"MAYCAQECAQE="
SERVER_RANDOM_B64 = (
    b"ICEiIyQlJicoKSorLC0uLzAxMjM0NTY3ODk6Ozw9Pj8="
)
SERVER_EPHEMERAL_PUBLIC_KEY_B64 = (
    b"A3zyexiNA09+ilI4AwS1GsPAiWnid/IbNaYLSPxHZpl4"
)
SERVER_SIGNATURE_B64 = b"MAYCAQICAQM="

CLIENT_PACKET = (
    b"NMEA-H|boat_001|18446744073709551615|"
    b"AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=|"
    b"A2sX0fLhLEJH+Lzm5WOkQPJ3A32BLeszoPShOUXYmMKW|"
    b"MAYCAQECAQE="
)
SERVER_PACKET = (
    b"OK|ICEiIyQlJicoKSorLC0uLzAxMjM0NTY3ODk6Ozw9Pj8=|"
    b"A3zyexiNA09+ilI4AwS1GsPAiWnid/IbNaYLSPxHZpl4|"
    b"MAYCAQICAQM="
)


class _BytesConvertible:
    def __bytes__(self):
        return CLIENT_PACKET


def _client_hello(**changes):
    values = {
        "station_id": "boat_001",
        "timestamp": MAX_TIMESTAMP,
        "client_random": CLIENT_RANDOM,
        "client_ephemeral_public_key": CLIENT_EPHEMERAL_PUBLIC_KEY,
        "client_signature": CLIENT_SIGNATURE,
    }
    values.update(changes)
    return ClientHello(**values)


def _server_hello(**changes):
    values = {
        "server_random": SERVER_RANDOM,
        "server_ephemeral_public_key": SERVER_EPHEMERAL_PUBLIC_KEY,
        "server_signature": SERVER_SIGNATURE,
    }
    values.update(changes)
    return ServerHello(**values)


def _replace_wire_field(packet, index, replacement):
    packet_fields = packet.split(b"|")
    packet_fields[index] = replacement
    return b"|".join(packet_fields)


def _empty_base64(_encoded):
    return b""


def _prepend_non_alphabet(encoded):
    return b"*" + encoded


def _insert_space(encoded):
    return encoded[:2] + b" " + encoded[2:]


def _insert_newline(encoded):
    return encoded[:2] + b"\n" + encoded[2:]


def _remove_final_character(encoded):
    return encoded[:-1]


def _add_padding(encoded):
    return encoded + b"="


def _use_urlsafe_character(encoded):
    return b"_" + encoded[1:]


BASE64_MUTATIONS = (
    pytest.param(_empty_base64, id="empty"),
    pytest.param(_prepend_non_alphabet, id="non-alphabet"),
    pytest.param(_insert_space, id="space"),
    pytest.param(_insert_newline, id="newline"),
    pytest.param(_remove_final_character, id="malformed-padding"),
    pytest.param(_add_padding, id="extra-padding"),
    pytest.param(_use_urlsafe_character, id="urlsafe-character"),
)

BINARY_WIRE_FIELDS = (
    pytest.param(
        parse_client_hello_packet,
        CLIENT_PACKET,
        3,
        id="client-random",
    ),
    pytest.param(
        parse_client_hello_packet,
        CLIENT_PACKET,
        4,
        id="client-ephemeral-public-key",
    ),
    pytest.param(
        parse_client_hello_packet,
        CLIENT_PACKET,
        5,
        id="client-signature",
    ),
    pytest.param(
        parse_server_hello_packet,
        SERVER_PACKET,
        1,
        id="server-random",
    ),
    pytest.param(
        parse_server_hello_packet,
        SERVER_PACKET,
        2,
        id="server-ephemeral-public-key",
    ),
    pytest.param(
        parse_server_hello_packet,
        SERVER_PACKET,
        3,
        id="server-signature",
    ),
)


def test_protocol_api_and_wire_prefixes_are_public():
    assert {
        "CLIENT_HELLO_PREFIX",
        "ClientHello",
        "SESSION_CONFIRMATION_SEQUENCE",
        "SERVER_HELLO_PREFIX",
        "ServerHello",
        "build_client_hello_packet",
        "build_server_hello_packet",
        "parse_client_hello_packet",
        "parse_server_hello_packet",
    } <= set(udpsec_protocol.__all__)
    assert CLIENT_HELLO_PREFIX == b"NMEA-H"
    assert SESSION_CONFIRMATION_SEQUENCE == 0
    assert SERVER_HELLO_PREFIX == b"OK"


def test_exact_client_hello_wire_vector():
    client_hello = _client_hello()

    packet = build_client_hello_packet(client_hello)

    assert packet == CLIENT_PACKET
    assert len(packet) == 139
    assert type(packet) is bytes
    assert packet.split(b"|") == [
        b"NMEA-H",
        b"boat_001",
        b"18446744073709551615",
        CLIENT_RANDOM_B64,
        CLIENT_EPHEMERAL_PUBLIC_KEY_B64,
        CLIENT_SIGNATURE_B64,
    ]
    assert parse_client_hello_packet(packet) == client_hello


def test_exact_server_hello_wire_vector():
    server_hello = _server_hello()

    packet = build_server_hello_packet(server_hello)

    assert packet == SERVER_PACKET
    assert len(packet) == 105
    assert type(packet) is bytes
    assert packet.split(b"|") == [
        b"OK",
        SERVER_RANDOM_B64,
        SERVER_EPHEMERAL_PUBLIC_KEY_B64,
        SERVER_SIGNATURE_B64,
    ]
    assert parse_server_hello_packet(packet) == server_hello


@pytest.mark.parametrize(
    ("value", "builder", "parser"),
    (
        pytest.param(
            _client_hello(timestamp=1234567890),
            build_client_hello_packet,
            parse_client_hello_packet,
            id="client",
        ),
        pytest.param(
            _server_hello(),
            build_server_hello_packet,
            parse_server_hello_packet,
            id="server",
        ),
    ),
)
def test_build_parse_round_trip_is_deterministic(value, builder, parser):
    first_packet = builder(value)
    parsed = parser(first_packet)

    assert parsed == value
    assert builder(parsed) == first_packet


@pytest.mark.parametrize(
    ("value", "expected_fields", "signature_name", "signature"),
    (
        pytest.param(
            _client_hello(),
            (
                "station_id",
                "timestamp",
                "client_random",
                "client_ephemeral_public_key",
                "client_signature",
            ),
            "client_signature",
            CLIENT_SIGNATURE,
            id="client",
        ),
        pytest.param(
            _server_hello(),
            (
                "server_random",
                "server_ephemeral_public_key",
                "server_signature",
            ),
            "server_signature",
            SERVER_SIGNATURE,
            id="server",
        ),
    ),
)
def test_value_objects_are_frozen_slotted_and_signature_safe(
    value,
    expected_fields,
    signature_name,
    signature,
):
    assert is_dataclass(value)
    assert type(value).__dataclass_params__.frozen is True
    assert tuple(item.name for item in fields(value)) == expected_fields
    assert tuple(type(value).__slots__) == expected_fields
    assert not hasattr(value, "__dict__")

    with pytest.raises(FrozenInstanceError):
        setattr(value, expected_fields[0], getattr(value, expected_fields[0]))

    representation = repr(value)
    assert signature_name not in representation
    assert repr(signature) not in representation
    assert signature.hex() not in representation


def test_parsed_binary_fields_are_immutable_bytes():
    client_hello = parse_client_hello_packet(CLIENT_PACKET)
    server_hello = parse_server_hello_packet(SERVER_PACKET)

    assert type(client_hello.client_random) is bytes
    assert type(client_hello.client_ephemeral_public_key) is bytes
    assert type(client_hello.client_signature) is bytes
    assert type(server_hello.server_random) is bytes
    assert type(server_hello.server_ephemeral_public_key) is bytes
    assert type(server_hello.server_signature) is bytes


def test_unicode_station_id_round_trips_as_strict_utf8():
    station_id = "\u043b\u043e\u0434\u043a\u0430_\u2693"
    client_hello = _client_hello(station_id=station_id)

    packet = build_client_hello_packet(client_hello)

    assert packet.split(b"|")[1] == station_id.encode("utf-8")
    assert parse_client_hello_packet(packet).station_id == station_id


def test_station_id_is_not_normalized_or_trimmed():
    composed = _client_hello(station_id="\u00e9")
    decomposed = _client_hello(station_id="e\u0301")
    spaced = _client_hello(station_id=" boat ")

    composed_packet = build_client_hello_packet(composed)
    decomposed_packet = build_client_hello_packet(decomposed)

    assert composed_packet != decomposed_packet
    assert parse_client_hello_packet(composed_packet).station_id == "\u00e9"
    assert parse_client_hello_packet(decomposed_packet).station_id == "e\u0301"
    assert (
        parse_client_hello_packet(build_client_hello_packet(spaced)).station_id
        == " boat "
    )


@pytest.mark.parametrize("station_id", (None, b"boat_001", 1, object()))
def test_station_id_rejects_non_string_types(station_id):
    with pytest.raises(TypeError, match="station_id must be a string"):
        _client_hello(station_id=station_id)


@pytest.mark.parametrize(
    ("station_id", "message"),
    (
        pytest.param("", "must not be empty", id="empty"),
        pytest.param("boat|001", "must not contain", id="delimiter"),
        pytest.param("\ud800", "must be UTF-8 encodable", id="surrogate"),
    ),
)
def test_station_id_rejects_invalid_values(station_id, message):
    with pytest.raises(ValueError, match=message):
        _client_hello(station_id=station_id)


def test_parser_rejects_invalid_station_id_utf8():
    packet = _replace_wire_field(CLIENT_PACKET, 1, b"\xff")

    with pytest.raises(ValueError, match="station_id must use valid UTF-8"):
        parse_client_hello_packet(packet)


@pytest.mark.parametrize("timestamp", (0, MAX_TIMESTAMP))
def test_timestamp_unsigned_64_bit_boundaries_round_trip(timestamp):
    client_hello = _client_hello(timestamp=timestamp)
    packet = build_client_hello_packet(client_hello)

    assert packet.split(b"|")[2] == str(timestamp).encode("ascii")
    assert parse_client_hello_packet(packet).timestamp == timestamp


@pytest.mark.parametrize("timestamp", (True, False, 1.0, "1", b"1", None))
def test_timestamp_rejects_non_integer_types_and_bool(timestamp):
    with pytest.raises(TypeError, match="timestamp must be an integer"):
        _client_hello(timestamp=timestamp)


@pytest.mark.parametrize("timestamp", (-1, MAX_TIMESTAMP + 1))
def test_timestamp_rejects_out_of_range_values(timestamp):
    with pytest.raises(ValueError, match="unsigned 64-bit"):
        _client_hello(timestamp=timestamp)


@pytest.mark.parametrize(
    "encoded",
    (
        b"",
        b"+1",
        b"-0",
        b"-1",
        b"00",
        b"01",
        b"000",
        b" 1",
        b"1 ",
        b"\t1",
        b"1\n",
        b"1.0",
        b"one",
        b"\xff",
    ),
)
def test_parser_rejects_noncanonical_timestamp_wire_forms(encoded):
    packet = _replace_wire_field(CLIENT_PACKET, 2, encoded)

    with pytest.raises(ValueError, match="canonical unsigned ASCII decimal"):
        parse_client_hello_packet(packet)


@pytest.mark.parametrize(
    "encoded",
    (
        b"18446744073709551616",
        b"9" * 5000,
    ),
)
def test_parser_rejects_out_of_range_timestamp_wire_forms(encoded):
    packet = _replace_wire_field(CLIENT_PACKET, 2, encoded)

    with pytest.raises(ValueError, match="unsigned 64-bit"):
        parse_client_hello_packet(packet)


@pytest.mark.parametrize(
    ("factory", "field_name"),
    (
        pytest.param(_client_hello, "client_random", id="client"),
        pytest.param(_server_hello, "server_random", id="server"),
    ),
)
@pytest.mark.parametrize(
    "value",
    (
        None,
        "random",
        bytearray(b"x" * 32),
        memoryview(b"x" * 32),
        _BytesConvertible(),
    ),
)
def test_random_fields_reject_non_bytes_types(factory, field_name, value):
    with pytest.raises(TypeError, match=f"{field_name} must be bytes"):
        factory(**{field_name: value})


@pytest.mark.parametrize(
    ("factory", "field_name"),
    (
        pytest.param(_client_hello, "client_random", id="client"),
        pytest.param(_server_hello, "server_random", id="server"),
    ),
)
@pytest.mark.parametrize("length", (0, 31, 33))
def test_random_fields_require_exactly_32_bytes(
    factory,
    field_name,
    length,
):
    with pytest.raises(ValueError, match="must be exactly 32 bytes"):
        factory(**{field_name: b"x" * length})


@pytest.mark.parametrize(
    ("factory", "field_name"),
    (
        pytest.param(
            _client_hello,
            "client_ephemeral_public_key",
            id="client",
        ),
        pytest.param(
            _server_hello,
            "server_ephemeral_public_key",
            id="server",
        ),
    ),
)
@pytest.mark.parametrize(
    "value",
    (
        None,
        "public-key",
        bytearray(b"\x02" + b"x" * 32),
        memoryview(b"\x02" + b"x" * 32),
        _BytesConvertible(),
    ),
)
def test_ephemeral_public_key_fields_reject_non_bytes_types(
    factory,
    field_name,
    value,
):
    with pytest.raises(TypeError, match=f"{field_name} must be bytes"):
        factory(**{field_name: value})


@pytest.mark.parametrize(
    ("factory", "field_name"),
    (
        pytest.param(
            _client_hello,
            "client_ephemeral_public_key",
            id="client",
        ),
        pytest.param(
            _server_hello,
            "server_ephemeral_public_key",
            id="server",
        ),
    ),
)
@pytest.mark.parametrize("length", (0, 32, 34))
def test_ephemeral_public_key_fields_require_exactly_33_bytes(
    factory,
    field_name,
    length,
):
    with pytest.raises(ValueError, match="must be exactly 33 bytes"):
        factory(**{field_name: b"\x02" * length})


@pytest.mark.parametrize(
    ("factory", "field_name"),
    (
        pytest.param(
            _client_hello,
            "client_ephemeral_public_key",
            id="client",
        ),
        pytest.param(
            _server_hello,
            "server_ephemeral_public_key",
            id="server",
        ),
    ),
)
@pytest.mark.parametrize("prefix", (0x00, 0x04, 0x06, 0x07, 0xFF))
def test_ephemeral_public_key_fields_reject_invalid_prefixes(
    factory,
    field_name,
    prefix,
):
    encoded = bytes((prefix,)) + b"x" * 32

    with pytest.raises(ValueError, match="must start with 0x02 or 0x03"):
        factory(**{field_name: encoded})


@pytest.mark.parametrize(
    ("factory", "field_name"),
    (
        pytest.param(_client_hello, "client_signature", id="client"),
        pytest.param(_server_hello, "server_signature", id="server"),
    ),
)
@pytest.mark.parametrize(
    "value",
    (
        None,
        "signature",
        bytearray(b"signature"),
        memoryview(b"signature"),
        _BytesConvertible(),
    ),
)
def test_signature_fields_reject_non_bytes_types(
    factory,
    field_name,
    value,
):
    with pytest.raises(TypeError, match=f"{field_name} must be bytes"):
        factory(**{field_name: value})


@pytest.mark.parametrize(
    ("factory", "field_name"),
    (
        pytest.param(_client_hello, "client_signature", id="client"),
        pytest.param(_server_hello, "server_signature", id="server"),
    ),
)
def test_signature_fields_reject_empty_bytes(factory, field_name):
    with pytest.raises(ValueError, match=f"{field_name} must not be empty"):
        factory(**{field_name: b""})


def test_protocol_layer_does_not_validate_curve_points_or_der_signatures():
    structurally_valid_off_curve_point = b"\x02" + b"\xff" * 32
    opaque_non_der_signature = b"not-DER"
    client_hello = _client_hello(
        client_ephemeral_public_key=structurally_valid_off_curve_point,
        client_signature=opaque_non_der_signature,
    )
    server_hello = _server_hello(
        server_ephemeral_public_key=structurally_valid_off_curve_point,
        server_signature=opaque_non_der_signature,
    )

    assert (
        parse_client_hello_packet(build_client_hello_packet(client_hello))
        == client_hello
    )
    assert (
        parse_server_hello_packet(build_server_hello_packet(server_hello))
        == server_hello
    )


@pytest.mark.parametrize(
    ("builder", "value", "message"),
    (
        pytest.param(
            build_client_hello_packet,
            None,
            "client_hello must be a ClientHello",
            id="client-none",
        ),
        pytest.param(
            build_client_hello_packet,
            _server_hello(),
            "client_hello must be a ClientHello",
            id="client-wrong-value-object",
        ),
        pytest.param(
            build_server_hello_packet,
            None,
            "server_hello must be a ServerHello",
            id="server-none",
        ),
        pytest.param(
            build_server_hello_packet,
            _client_hello(),
            "server_hello must be a ServerHello",
            id="server-wrong-value-object",
        ),
    ),
)
def test_builders_reject_unsupported_value_objects(builder, value, message):
    with pytest.raises(TypeError, match=message):
        builder(value)


@pytest.mark.parametrize(
    "parser",
    (parse_client_hello_packet, parse_server_hello_packet),
)
@pytest.mark.parametrize(
    "packet",
    (
        None,
        "packet",
        bytearray(CLIENT_PACKET),
        memoryview(CLIENT_PACKET),
        _BytesConvertible(),
    ),
)
def test_parsers_accept_only_bytes_packets(parser, packet):
    with pytest.raises(TypeError, match="packet must be bytes"):
        parser(packet)


@pytest.mark.parametrize(
    ("parser", "packet"),
    (
        pytest.param(
            parse_client_hello_packet,
            b"",
            id="client-empty",
        ),
        pytest.param(
            parse_client_hello_packet,
            b"NMEA-H",
            id="client-prefix-only",
        ),
        pytest.param(
            parse_client_hello_packet,
            b"NMEA-H|boat_001|123|MAYCAQECAQE=",
            id="client-old-format",
        ),
        pytest.param(
            parse_client_hello_packet,
            CLIENT_PACKET + b"|",
            id="client-trailing-delimiter",
        ),
        pytest.param(
            parse_client_hello_packet,
            CLIENT_PACKET + b"|extra",
            id="client-extra-field",
        ),
        pytest.param(
            parse_client_hello_packet,
            b"|".join(CLIENT_PACKET.split(b"|")[:-1]),
            id="client-missing-field",
        ),
        pytest.param(
            parse_client_hello_packet,
            _replace_wire_field(CLIENT_PACKET, 0, b"NMEA-X"),
            id="client-wrong-prefix",
        ),
        pytest.param(
            parse_client_hello_packet,
            _replace_wire_field(CLIENT_PACKET, 1, b"boat|001"),
            id="client-delimiter-in-station",
        ),
        pytest.param(
            parse_client_hello_packet,
            SERVER_PACKET,
            id="client-given-server",
        ),
        pytest.param(
            parse_server_hello_packet,
            b"",
            id="server-empty",
        ),
        pytest.param(
            parse_server_hello_packet,
            b"OK",
            id="server-prefix-only",
        ),
        pytest.param(
            parse_server_hello_packet,
            b"OK|MAYCAQICAQM=",
            id="server-old-format",
        ),
        pytest.param(
            parse_server_hello_packet,
            SERVER_PACKET + b"|",
            id="server-trailing-delimiter",
        ),
        pytest.param(
            parse_server_hello_packet,
            SERVER_PACKET + b"|extra",
            id="server-extra-field",
        ),
        pytest.param(
            parse_server_hello_packet,
            b"|".join(SERVER_PACKET.split(b"|")[:-1]),
            id="server-missing-field",
        ),
        pytest.param(
            parse_server_hello_packet,
            _replace_wire_field(SERVER_PACKET, 0, b"NO"),
            id="server-wrong-prefix",
        ),
        pytest.param(
            parse_server_hello_packet,
            CLIENT_PACKET,
            id="server-given-client",
        ),
    ),
)
def test_parsers_reject_wrong_prefixes_and_field_counts(parser, packet):
    with pytest.raises(ValueError, match="invalid .* packet format"):
        parser(packet)


@pytest.mark.parametrize(
    ("parser", "packet", "field_index"),
    BINARY_WIRE_FIELDS,
)
@pytest.mark.parametrize("mutation", BASE64_MUTATIONS)
def test_all_binary_wire_fields_require_strict_standard_base64(
    parser,
    packet,
    field_index,
    mutation,
):
    encoded = packet.split(b"|")[field_index]
    malformed_packet = _replace_wire_field(
        packet,
        field_index,
        mutation(encoded),
    )

    with pytest.raises(ValueError):
        parser(malformed_packet)


def test_parser_rejects_noncanonical_base64_with_nonzero_pad_bits():
    noncanonical = CLIENT_RANDOM_B64[:-2] + b"9="
    assert base64.b64decode(
        noncanonical,
        validate=True,
    ) == CLIENT_RANDOM
    packet = _replace_wire_field(CLIENT_PACKET, 3, noncanonical)

    with pytest.raises(ValueError, match="canonical standard base64"):
        parse_client_hello_packet(packet)


@pytest.mark.parametrize(
    ("parser", "packet", "field_index", "decoded", "message"),
    (
        pytest.param(
            parse_client_hello_packet,
            CLIENT_PACKET,
            3,
            b"x" * 31,
            "client_random must be exactly 32 bytes",
            id="client-random-short",
        ),
        pytest.param(
            parse_client_hello_packet,
            CLIENT_PACKET,
            3,
            b"x" * 33,
            "client_random must be exactly 32 bytes",
            id="client-random-long",
        ),
        pytest.param(
            parse_server_hello_packet,
            SERVER_PACKET,
            1,
            b"x" * 31,
            "server_random must be exactly 32 bytes",
            id="server-random-short",
        ),
        pytest.param(
            parse_server_hello_packet,
            SERVER_PACKET,
            1,
            b"x" * 33,
            "server_random must be exactly 32 bytes",
            id="server-random-long",
        ),
        pytest.param(
            parse_client_hello_packet,
            CLIENT_PACKET,
            4,
            b"\x02" + b"x" * 31,
            "client_ephemeral_public_key must be exactly 33 bytes",
            id="client-key-short",
        ),
        pytest.param(
            parse_client_hello_packet,
            CLIENT_PACKET,
            4,
            b"\x02" + b"x" * 33,
            "client_ephemeral_public_key must be exactly 33 bytes",
            id="client-key-long",
        ),
        pytest.param(
            parse_server_hello_packet,
            SERVER_PACKET,
            2,
            b"\x02" + b"x" * 31,
            "server_ephemeral_public_key must be exactly 33 bytes",
            id="server-key-short",
        ),
        pytest.param(
            parse_server_hello_packet,
            SERVER_PACKET,
            2,
            b"\x02" + b"x" * 33,
            "server_ephemeral_public_key must be exactly 33 bytes",
            id="server-key-long",
        ),
        pytest.param(
            parse_client_hello_packet,
            CLIENT_PACKET,
            4,
            b"\x04" + b"x" * 32,
            "client_ephemeral_public_key must start with 0x02 or 0x03",
            id="client-key-prefix",
        ),
        pytest.param(
            parse_server_hello_packet,
            SERVER_PACKET,
            2,
            b"\x04" + b"x" * 32,
            "server_ephemeral_public_key must start with 0x02 or 0x03",
            id="server-key-prefix",
        ),
    ),
)
def test_parsers_enforce_decoded_binary_field_shapes(
    parser,
    packet,
    field_index,
    decoded,
    message,
):
    malformed_packet = _replace_wire_field(
        packet,
        field_index,
        base64.b64encode(decoded),
    )

    with pytest.raises(ValueError, match=message):
        parser(malformed_packet)


@pytest.mark.parametrize(
    ("parser", "packet", "field_index"),
    (
        pytest.param(
            parse_client_hello_packet,
            CLIENT_PACKET,
            5,
            id="client",
        ),
        pytest.param(
            parse_server_hello_packet,
            SERVER_PACKET,
            3,
            id="server",
        ),
    ),
)
def test_parsers_reject_empty_signature_fields(parser, packet, field_index):
    malformed_packet = _replace_wire_field(packet, field_index, b"")

    with pytest.raises(ValueError, match="base64 field must not be empty"):
        parser(malformed_packet)


@pytest.mark.parametrize(
    ("parser", "packet", "field_index"),
    BINARY_WIRE_FIELDS,
)
def test_builders_emit_canonical_base64_for_every_binary_field(
    parser,
    packet,
    field_index,
):
    parsed = parser(packet)
    encoded = packet.split(b"|")[field_index]

    assert base64.b64encode(base64.b64decode(encoded, validate=True)) == encoded
    if isinstance(parsed, ClientHello):
        assert build_client_hello_packet(parsed) == packet
    else:
        assert build_server_hello_packet(parsed) == packet
