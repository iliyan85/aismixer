import pytest

from core.routing import (
    CircularZoneReferenceError,
    RouteDefinition,
    UnknownZoneError,
    ZoneDefinition,
    ZoneResolutionError,
    RoutingTable,
    load_route_definitions,
    load_zone_definitions,
    match_routes,
    resolve_zones,
    validate_routing_config,
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


def test_load_zone_definitions_from_plain_config():
    definitions = load_zone_definitions(
        {
            "balchik_fixed": {
                "include": ["udp:balchik_roof", "udpsec:rPiAIS002"],
            },
            "public_sources": {"union": ["balchik_fixed", "mobile"]},
        }
    )

    assert definitions == {
        "balchik_fixed": ZoneDefinition(
            include=("udp:balchik_roof", "udpsec:rPiAIS002")
        ),
        "public_sources": ZoneDefinition(union=("balchik_fixed", "mobile")),
    }


def test_load_route_definitions_from_plain_config_preserves_target_order():
    definitions = load_route_definitions(
        [
            {
                "name": "public_to_archive",
                "from_zone": "public_sources",
                "to": ["mongo:raw_archive", "mqtt:clean_stream"],
            }
        ]
    )

    assert definitions == [
        RouteDefinition(
            name="public_to_archive",
            from_zone="public_sources",
            to=("mongo:raw_archive", "mqtt:clean_stream"),
        )
    ]


def test_validate_complete_transport_agnostic_routing_config():
    zones = {
        "balchik_fixed": {
            "include": ["udp:balchik_roof", "udpsec:rPiAIS002"]
        },
        "mobile": {
            "include": [
                "tcp:ais_input_1",
                "http_json:receiver_api_1",
                "mqtt:ais_topic_1",
                "amqp:ais_exchange_1",
            ]
        },
        "public_sources": {"union": ["balchik_fixed", "mobile"]},
    }
    routes = [
        {
            "name": "public_to_archive",
            "from_zone": "public_sources",
            "to": ["mongo:raw_archive", "mqtt:clean_stream"],
        }
    ]

    resolved = validate_routing_config(zones, routes)

    assert resolved["public_sources"] == frozenset(
        {
            "udp:balchik_roof",
            "udpsec:rPiAIS002",
            "tcp:ais_input_1",
            "http_json:receiver_api_1",
            "mqtt:ais_topic_1",
            "amqp:ais_exchange_1",
        }
    )


def test_validate_routing_config_rejects_route_with_unknown_zone():
    zones = {"trusted": {"include": ["udpsec:rPiAIS002"]}}
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
        validate_routing_config(zones, routes)


def test_load_zone_definitions_rejects_multiple_expressions():
    zones = {
        "invalid": {
            "include": ["udp:balchik_roof"],
            "union": ["trusted"],
        }
    }

    with pytest.raises(ZoneResolutionError, match="must define exactly one"):
        load_zone_definitions(zones)


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({"invalid": {"sources": ["udp:balchik_roof"]}}, "unknown field"),
        ({"invalid": {"include": [123]}}, "must contain only strings"),
    ],
)
def test_load_zone_definitions_rejects_invalid_fields_and_source_ids(config, message):
    with pytest.raises((TypeError, ZoneResolutionError), match=message):
        load_zone_definitions(config)


@pytest.mark.parametrize(
    ("route", "message"),
    [
        (
            {
                "name": "invalid",
                "from_zone": "trusted",
                "to": ["mongo:raw_archive"],
                "targets": [],
            },
            "unknown field",
        ),
        (
            {
                "name": "invalid",
                "from_zone": "trusted",
                "to": ["mongo:raw_archive", 123],
            },
            "must contain only strings",
        ),
    ],
)
def test_load_route_definitions_rejects_invalid_fields_and_target_ids(route, message):
    with pytest.raises((TypeError, ValueError), match=message):
        load_route_definitions([route])


def test_routing_table_from_config_creates_resolved_zones_and_routes():
    table = RoutingTable.from_config(
        {
            "fixed": {"include": ["udp:balchik_roof"]},
            "mobile": {"include": ["udpsec:rPiAIS002"]},
            "trusted": {"union": ["fixed", "mobile"]},
        },
        [
            {
                "name": "trusted_to_archive",
                "from_zone": "trusted",
                "to": ["mongo:raw_archive"],
            }
        ],
    )

    assert table.resolved_zones["trusted"] == frozenset(
        {"udp:balchik_roof", "udpsec:rPiAIS002"}
    )
    assert table.route_definitions == (
        RouteDefinition(
            name="trusted_to_archive",
            from_zone="trusted",
            to=("mongo:raw_archive",),
        ),
    )


def test_routing_table_match_returns_route_names_and_target_ids():
    table = RoutingTable.from_definitions(
        {"trusted": ZoneDefinition(include=("udpsec:rPiAIS002",))},
        [
            RouteDefinition(
                name="trusted_to_platforms",
                from_zone="trusted",
                to=("mqtt:clean_stream", "mongo:raw_archive"),
            )
        ],
    )

    result = table.match("udpsec:rPiAIS002")

    assert result.route_names == ("trusted_to_platforms",)
    assert result.target_ids == ("mqtt:clean_stream", "mongo:raw_archive")


def test_routing_table_match_returns_empty_result_when_no_route_matches():
    table = RoutingTable.from_config(
        {"trusted": {"include": ["udpsec:rPiAIS002"]}},
        [
            {
                "name": "trusted_to_archive",
                "from_zone": "trusted",
                "to": ["mongo:raw_archive"],
            }
        ],
    )

    result = table.match("tcp:untrusted_input")

    assert result.route_names == ()
    assert result.target_ids == ()


def test_routing_table_from_config_rejects_unknown_route_zone():
    with pytest.raises(
        UnknownZoneError,
        match="Route 'bad_route' references unknown zone 'missing'",
    ):
        RoutingTable.from_config(
            {"trusted": {"include": ["udpsec:rPiAIS002"]}},
            [
                {
                    "name": "bad_route",
                    "from_zone": "missing",
                    "to": ["mongo:raw_archive"],
                }
            ],
        )


def test_routing_table_deduplicates_targets_and_preserves_first_order():
    table = RoutingTable.from_config(
        {"trusted": {"include": ["udpsec:rPiAIS002"]}},
        [
            {
                "name": "archive_and_mqtt",
                "from_zone": "trusted",
                "to": ["mongo:raw_archive", "mqtt:clean_stream"],
            },
            {
                "name": "archive_and_amqp",
                "from_zone": "trusted",
                "to": ["mongo:raw_archive", "amqp:clean_exchange"],
            },
        ],
    )

    result = table.match("udpsec:rPiAIS002")

    assert result.route_names == ("archive_and_mqtt", "archive_and_amqp")
    assert result.target_ids == (
        "mongo:raw_archive",
        "mqtt:clean_stream",
        "amqp:clean_exchange",
    )


def test_routing_table_supports_transport_agnostic_source_and_target_ids():
    source_ids = (
        "udpsec:rPiAIS002",
        "tcp:ais_input_1",
        "mqtt:ais_topic_1",
        "amqp:ais_exchange_1",
        "http_json:receiver_api_1",
    )
    table = RoutingTable.from_config(
        {"all_inputs": {"include": source_ids}},
        [
            {
                "name": "republish_and_archive",
                "from_zone": "all_inputs",
                "to": [
                    "mongo:raw_archive",
                    "mqtt:clean_stream",
                    "amqp:clean_exchange",
                ],
            }
        ],
    )

    for source_id in source_ids:
        assert table.match(source_id).target_ids == (
            "mongo:raw_archive",
            "mqtt:clean_stream",
            "amqp:clean_exchange",
        )


def test_routing_table_is_immutable_snapshot_of_inputs():
    resolved_zones = {"trusted": {"udpsec:rPiAIS002"}}
    routes = [
        RouteDefinition(
            name="trusted_to_archive",
            from_zone="trusted",
            to=("mongo:raw_archive",),
        )
    ]
    table = RoutingTable(
        resolved_zones=resolved_zones,
        route_definitions=routes,
    )

    resolved_zones["trusted"].add("tcp:ais_input_1")
    routes.append(
        RouteDefinition(
            name="trusted_to_mqtt",
            from_zone="trusted",
            to=("mqtt:clean_stream",),
        )
    )

    result = table.match("udpsec:rPiAIS002")

    assert result.route_names == ("trusted_to_archive",)
    assert result.target_ids == ("mongo:raw_archive",)
    unmatched = table.match("tcp:ais_input_1")
    assert unmatched.route_names == ()
    assert unmatched.target_ids == ()
    with pytest.raises(TypeError):
        table.resolved_zones["trusted"] = frozenset()
