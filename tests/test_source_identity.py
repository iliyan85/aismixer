from core.event import IngressEvent
from core.source_identity import build_udp_source_id, build_udpsec_source_id


def test_udp_source_id_uses_configured_input_id_first():
    assert (
        build_udp_source_id("balchik_roof", "dock_gate", "192.0.2.10")
        == "udp:balchik_roof"
    )


def test_udp_source_id_uses_mapped_alias_without_configured_id():
    assert build_udp_source_id(None, "dock_gate", "192.0.2.10") == "udp:dock_gate"


def test_udp_source_id_falls_back_to_remote_ipv4_address():
    assert build_udp_source_id(None, None, "192.0.2.10") == "udp:192.0.2.10"


def test_udp_source_id_accepts_ipv6_address_as_opaque_identity():
    assert build_udp_source_id(None, None, "2001:db8::10") == "udp:2001:db8::10"


def test_udpsec_source_id_uses_authenticated_station_id():
    assert build_udpsec_source_id("rPiAIS002") == "udpsec:rPiAIS002"


def test_routing_source_ids_are_not_tag_s_sanitized_or_truncated():
    identity = "station/name with spaces and far more than fifteen characters"

    assert build_udp_source_id(identity, None, "192.0.2.10") == f"udp:{identity}"


def test_ingress_event_accepts_future_adapter_kind():
    event = IngressEvent(
        kind="mqtt",
        source_id="mqtt:ais/topic",
        alias_for_s=None,
        remote_ip=None,
        assembler_key="mqtt:ais/topic",
        raw_line="!AIVDM,1,1,,A,payload,0*00",
    )

    assert event.kind == "mqtt"
    assert event.source_id == "mqtt:ais/topic"
