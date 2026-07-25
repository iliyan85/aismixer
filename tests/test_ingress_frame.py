from dataclasses import FrozenInstanceError

import pytest

from core.event import IngressEvent
from core.ingress_frame import (
    IngressFrame,
    PayloadTextMode,
    coerce_ingress_frame,
    decode_frame_slice,
    frame_from_ingress_event,
    frame_from_text_payload,
    frame_from_udp_datagram,
)


def make_event(raw_line="!AIVDM,1,1,,A,payload,0*00"):
    return IngressEvent(
        kind="udpsec",
        source_id="station:alpha",
        alias_for_s="alpha",
        remote_ip="192.0.2.10",
        assembler_key="udpsec:station:alpha",
        raw_line=raw_line,
    )


def test_ingress_frame_preserves_complete_metadata_and_byte_payload():
    payload = b"\\s:alpha*00\\!AIVDM,1,1,,A,payload,0*00"

    frame = IngressFrame(
        kind="udpsec",
        source_id="station:alpha",
        alias_for_s="alpha",
        remote_ip="192.0.2.10",
        assembler_key="udpsec:station:alpha",
        payload=payload,
    )

    assert frame.kind == "udpsec"
    assert frame.source_id == "station:alpha"
    assert frame.alias_for_s == "alpha"
    assert frame.remote_ip == "192.0.2.10"
    assert frame.assembler_key == "udpsec:station:alpha"
    assert frame.payload is payload
    assert frame.text_mode is PayloadTextMode.UTF8_IGNORE


def test_ingress_frame_is_frozen():
    frame = IngressFrame(
        kind="udp",
        source_id="udp:primary",
        alias_for_s=None,
        remote_ip=None,
        assembler_key="udp:primary",
        payload=b"!AIVDM,1,1,,A,payload,0*00",
    )

    with pytest.raises(FrozenInstanceError):
        frame.source_id = "udp:changed"


def test_legacy_event_conversion_preserves_identity_and_encodes_raw_line():
    event = make_event()

    frame = frame_from_ingress_event(event)

    assert frame == IngressFrame(
        kind=event.kind,
        source_id=event.source_id,
        alias_for_s=event.alias_for_s,
        remote_ip=event.remote_ip,
        assembler_key=event.assembler_key,
        payload=b"!AIVDM,1,1,,A,payload,0*00",
        text_mode=PayloadTextMode.UTF8_SURROGATEPASS,
    )


def test_text_payload_constructor_preserves_identity_and_uses_builtin_encoder():
    class Payload(str):
        def encode(self, *_args, **_kwargs):
            raise AssertionError("overridden encode must not be called")

    payload = Payload("prefix \ud800 suffix")
    frame = frame_from_text_payload(
        kind="sec",
        source_id="udpsec:boat",
        alias_for_s="listener",
        remote_ip="2001:db8::10",
        assembler_key="[2001:db8::10]:50123",
        payload=payload,
    )

    assert frame == IngressFrame(
        kind="sec",
        source_id="udpsec:boat",
        alias_for_s="listener",
        remote_ip="2001:db8::10",
        assembler_key="[2001:db8::10]:50123",
        payload=str.encode(
            payload,
            "utf-8",
            errors="surrogatepass",
        ),
        text_mode=PayloadTextMode.UTF8_SURROGATEPASS,
    )
    assert decode_frame_slice(frame, 0, len(frame.payload)) == payload


@pytest.mark.parametrize("payload", [None, b"text", 123, False, [], {}])
def test_text_payload_constructor_rejects_non_strings(payload):
    assert frame_from_text_payload(
        kind="sec",
        source_id="udpsec:boat",
        alias_for_s=None,
        remote_ip=None,
        assembler_key="secure",
        payload=payload,
    ) is None


def test_legacy_str_subclass_is_accepted():
    class RawLine(str):
        def encode(self, *args, **kwargs):
            raise AssertionError("overridden encode must not be called")

    frame = frame_from_ingress_event(make_event(RawLine("AIS data")))

    assert frame is not None
    assert frame.payload == b"AIS data"
    assert frame.text_mode is PayloadTextMode.UTF8_SURROGATEPASS


def test_legacy_non_ascii_text_uses_utf8():
    frame = frame_from_ingress_event(make_event("AIS \N{SAILBOAT}"))

    assert frame is not None
    assert frame.payload == b"AIS \xe2\x9b\xb5"
    assert decode_frame_slice(frame, 0, len(frame.payload)) == "AIS \N{SAILBOAT}"


@pytest.mark.parametrize(
    "raw_line",
    [
        "\ud800",
        "\udfff",
        "prefix \ud800 middle \udfff suffix",
    ],
    ids=["lone-high", "lone-low", "mixed"],
)
def test_legacy_surrogates_round_trip_through_declared_text_mode(raw_line):
    frame = frame_from_ingress_event(make_event(raw_line))

    assert frame is not None
    assert frame.text_mode is PayloadTextMode.UTF8_SURROGATEPASS
    assert frame.payload == str.encode(
        raw_line,
        "utf-8",
        errors="surrogatepass",
    )
    assert decode_frame_slice(frame, 0, len(frame.payload)) == raw_line


def test_bytes_native_default_mode_ignores_invalid_utf8():
    frame = IngressFrame(
        kind="udpsec",
        source_id="station:bytes",
        alias_for_s=None,
        remote_ip=None,
        assembler_key="udpsec:station:bytes",
        payload=b"before\xffafter",
    )

    assert frame.text_mode is PayloadTextMode.UTF8_IGNORE
    assert decode_frame_slice(frame, 0, len(frame.payload)) == "beforeafter"


@pytest.mark.parametrize(
    "start,end",
    [
        (-1, 0),
        (2, 1),
        (0, 4),
    ],
)
def test_decode_frame_slice_rejects_invalid_bounds(start, end):
    frame = IngressFrame(
        kind="udp",
        source_id="udp:primary",
        alias_for_s=None,
        remote_ip=None,
        assembler_key="udp:primary",
        payload=b"abc",
    )

    with pytest.raises(ValueError):
        decode_frame_slice(frame, start, end)


def test_decode_frame_slice_allows_empty_slice():
    frame = IngressFrame(
        kind="udp",
        source_id="udp:primary",
        alias_for_s=None,
        remote_ip=None,
        assembler_key="udp:primary",
        payload=b"abc",
    )

    assert decode_frame_slice(frame, 2, 2) == ""


@pytest.mark.parametrize("raw_line", [None, b"AIS data", 123, False, [], {}])
def test_non_string_legacy_payload_returns_no_frame(raw_line):
    assert frame_from_ingress_event(make_event(raw_line)) is None


def test_coercion_returns_direct_frame_unchanged():
    frame = IngressFrame(
        kind="udp",
        source_id="udp:primary",
        alias_for_s=None,
        remote_ip="192.0.2.10",
        assembler_key="192.0.2.10:17778",
        payload=b"before\xffafter",
    )

    assert coerce_ingress_frame(frame) is frame


def test_coercion_converts_valid_legacy_event():
    event = make_event("legacy \ud800 payload")

    assert coerce_ingress_frame(event) == frame_from_ingress_event(event)


@pytest.mark.parametrize(
    "item",
    [
        make_event(None),
        make_event(b"bytes"),
        None,
        b"bytes",
        "plain string",
        123,
        object(),
    ],
    ids=[
        "invalid-event-null",
        "invalid-event-bytes",
        "null",
        "bytes",
        "string",
        "number",
        "object",
    ],
)
def test_coercion_rejects_invalid_events_and_unsupported_items(item):
    assert coerce_ingress_frame(item) is None


@pytest.mark.parametrize(
    "data",
    [
        b"!AIVDM,1,1,,A,payload,0*00",
        b"  !AIVDM,1,1,,A,payload,0*00  ",
        b"\t\r\n!AIVDM,1,1,,A,payload,0*00\t\r\n",
        (
            "\u2003\u00a0!AIVDM,1,1,,A,payload,0*00"
            "\u202f\u2002"
        ).encode("utf-8"),
        "préfixe ⛵ suffixe".encode("utf-8"),
        b"\xffbefore\xfein\x80side-after\xf8",
        b"\xff \ttext \r\n\xfe",
        b"",
        b" \t\r\n",
        b"\xff\xfe\x80",
        (
            b"!AIVDM,1,1,,A,first,0*00\n"
            b"!AIVDO,1,1,,B,second,0*00"
        ),
        (
            "\u2003\\s:boat*00\\"
            "!AIVDM,1,1,,A,payload,0*00\u2002"
        ).encode("utf-8"),
    ],
    ids=[
        "ascii-nmea",
        "spaces",
        "ascii-whitespace",
        "unicode-whitespace",
        "non-ascii",
        "invalid-before-inside-after",
        "invalid-adjacent-whitespace",
        "empty",
        "whitespace-only",
        "invalid-only",
        "multiple-sentences",
        "leading-tag-after-whitespace",
    ],
)
def test_udp_datagram_constructor_matches_legacy_normalization(data):
    expected_text = data.decode("utf-8", errors="ignore").strip()

    frame = frame_from_udp_datagram(
        data=data,
        kind="udp",
        source_id="udp:dock",
        alias_for_s="dock",
        remote_ip="192.0.2.10",
        assembler_key="192.0.2.10:17778",
    )

    assert frame.kind == "udp"
    assert frame.source_id == "udp:dock"
    assert frame.alias_for_s == "dock"
    assert frame.remote_ip == "192.0.2.10"
    assert frame.assembler_key == "192.0.2.10:17778"
    assert frame.payload == expected_text.encode("utf-8")
    assert frame.text_mode is PayloadTextMode.UTF8_IGNORE
    assert decode_frame_slice(frame, 0, len(frame.payload)) == expected_text


def test_frame_is_independent_of_original_legacy_event():
    event = make_event("original")
    frame = frame_from_ingress_event(event)

    event.kind = "changed"
    event.source_id = "changed"
    event.alias_for_s = None
    event.remote_ip = None
    event.assembler_key = "changed"
    event.raw_line = "changed"

    assert frame == IngressFrame(
        kind="udpsec",
        source_id="station:alpha",
        alias_for_s="alpha",
        remote_ip="192.0.2.10",
        assembler_key="udpsec:station:alpha",
        payload=b"original",
        text_mode=PayloadTextMode.UTF8_SURROGATEPASS,
    )
