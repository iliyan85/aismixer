import pytest

from core.routing import (
    CircularZoneReferenceError,
    RouteDefinition,
    UnknownZoneError,
    ZoneDefinition,
    match_routes,
    resolve_zones,
)


def test_resolve_simple_include_zone():
    zones = {
        "balchik_fixed": {
            "include": ["udp:balchik_roof", "udpsec:rPiAIS002"],
        }
    }

    assert resolve_zones(zones) == {
        "balchik_fixed": frozenset({"udp:balchik_roof", "udpsec:rPiAIS002"})
    }


def test_resolve_union_zone():
    zones = {
        "fixed": ZoneDefinition(include=("udp:balchik_roof",)),
        "portable": ZoneDefinition(include=("udpsec:rPiAIS002",)),
        "trusted": ZoneDefinition(union=("fixed", "portable")),
    }

    assert resolve_zones(zones)["trusted"] == frozenset(
        {"udp:balchik_roof", "udpsec:rPiAIS002"}
    )


def test_resolve_intersection_zone():
    zones = {
        "trusted": {"include": ["tcp:ais_input_1", "mqtt:ais_topic_1"]},
        "streaming": {"include": ["tcp:ais_input_1", "http_json:receiver_api_1"]},
        "trusted_streaming": {"intersection": ["trusted", "streaming"]},
    }

    assert resolve_zones(zones)["trusted_streaming"] == frozenset({"tcp:ais_input_1"})


def test_resolve_difference_zone():
    zones = {
        "all_receivers": {
            "include": ["udp:balchik_roof", "mqtt:ais_topic_1", "amqp:ais_exchange_1"]
        },
        "quarantined": {"include": ["mqtt:ais_topic_1"]},
        "active": {"difference": ["all_receivers", "quarantined"]},
    }

    assert resolve_zones(zones)["active"] == frozenset(
        {"udp:balchik_roof", "amqp:ais_exchange_1"}
    )


def test_unknown_zone_reference_raises_clear_exception():
    with pytest.raises(UnknownZoneError, match="references unknown zone 'missing'"):
        resolve_zones({"trusted": {"union": ["missing"]}})


def test_circular_zone_reference_raises_clear_exception():
    zones = {
        "one": {"union": ["two"]},
        "two": {"difference": ["one"]},
    }

    with pytest.raises(CircularZoneReferenceError, match=r"one -> two -> one"):
        resolve_zones(zones)


def test_route_matching_by_from_zone_returns_names_and_targets():
    resolved = resolve_zones(
        {"trusted": {"include": ["udpsec:rPiAIS002", "http_json:receiver_api_1"]}}
    )
    routes = [
        RouteDefinition(
            name="trusted_to_public_platforms",
            from_zone="trusted",
            to=("udp:aishub", "udp:marinetraffic", "mongo:raw_archive"),
        )
    ]

    result = match_routes("udpsec:rPiAIS002", resolved, routes)

    assert result.route_names == ("trusted_to_public_platforms",)
    assert result.target_ids == (
        "udp:aishub",
        "udp:marinetraffic",
        "mongo:raw_archive",
    )


def test_route_matching_returns_empty_result_when_no_route_matches():
    resolved = resolve_zones({"trusted": {"include": ["udpsec:rPiAIS002"]}})
    routes = [
        {
            "name": "trusted_to_archive",
            "from_zone": "trusted",
            "to": ["mongo:raw_archive"],
        }
    ]

    result = match_routes("tcp:untrusted_input", resolved, routes)

    assert result.route_names == ()
    assert result.target_ids == ()


def test_route_matching_unknown_zone_raises_clear_exception():
    resolved = resolve_zones({"trusted": {"include": ["udpsec:rPiAIS002"]}})
    routes = [
        {
            "name": "bad_route",
            "from_zone": "missing",
            "to": ["mongo:raw_archive"],
        }
    ]

    with pytest.raises(
        UnknownZoneError,
        match="Route 'bad_route' references unknown zone 'missing'",
    ):
        match_routes("udpsec:rPiAIS002", resolved, routes)


def test_route_matching_deduplicates_targets_but_keeps_first_order():
    resolved = resolve_zones({"trusted": {"include": ["udpsec:rPiAIS002"]}})
    routes = [
        {
            "name": "trusted_to_archive_and_mqtt",
            "from_zone": "trusted",
            "to": ["mongo:raw_archive", "mqtt:clean_stream"],
        },
        {
            "name": "trusted_to_archive_and_amqp",
            "from_zone": "trusted",
            "to": ["mongo:raw_archive", "amqp:clean_exchange"],
        },
    ]

    result = match_routes("udpsec:rPiAIS002", resolved, routes)

    assert result.route_names == (
        "trusted_to_archive_and_mqtt",
        "trusted_to_archive_and_amqp",
    )
    assert result.target_ids == (
        "mongo:raw_archive",
        "mqtt:clean_stream",
        "amqp:clean_exchange",
    )


def test_transport_namespaces_are_opaque_and_targets_keep_stable_order():
    source_ids = [
        "mqtt:ais_topic_1",
        "amqp:ais_exchange_1",
        "tcp:ais_input_1",
        "http_json:receiver_api_1",
    ]
    resolved = resolve_zones({"multi_transport": {"include": source_ids}})
    routes = [
        {
            "name": "archive_and_republish",
            "from_zone": "multi_transport",
            "to": ["mongo:raw_archive", "mqtt:clean_stream", "amqp:clean_exchange"],
        }
    ]

    for source_id in source_ids:
        result = match_routes(source_id, resolved, routes)
        assert result.route_names == ("archive_and_republish",)
        assert result.target_ids == (
            "mongo:raw_archive",
            "mqtt:clean_stream",
            "amqp:clean_exchange",
        )
