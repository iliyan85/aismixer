from dataclasses import FrozenInstanceError

import pytest

from core.event import IngressEvent
from core.ingress_frame import IngressFrame, frame_from_ingress_event


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
    )


def test_legacy_str_subclass_is_accepted():
    class RawLine(str):
        pass

    frame = frame_from_ingress_event(make_event(RawLine("AIS data")))

    assert frame is not None
    assert frame.payload == b"AIS data"


def test_legacy_non_ascii_text_uses_utf8():
    frame = frame_from_ingress_event(make_event("AIS \N{SAILBOAT}"))

    assert frame is not None
    assert frame.payload == b"AIS \xe2\x9b\xb5"


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
    )
