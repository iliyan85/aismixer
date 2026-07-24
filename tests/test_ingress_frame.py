from dataclasses import FrozenInstanceError

import pytest

from core.event import IngressEvent
from core.ingress_frame import (
    IngressFrame,
    PayloadTextMode,
    decode_frame_slice,
    frame_from_ingress_event,
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
