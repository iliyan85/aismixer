import pytest

from core.routing import RoutingTable
from core.target_identity import freeze_target_id_by_name
from core.runtime_routing import (
    RuntimeRoutingConfigError,
    compile_routing_section,
    load_optional_routing_table,
)


TARGET_ID_BY_NAME = {
    "udp:aishub": 0,
    "udp:local_debug": 1,
}
AVAILABLE_TARGETS = tuple(TARGET_ID_BY_NAME)


def routing_config(targets=None):
    return {
        "routing": {
            "zones": {
                "balchik_fixed": {
                    "include": ["udp:balchik_roof", "udpsec:rPiAIS002"],
                },
                "mobile": {
                    "include": ["udpsec:vitara_mobile"],
                },
                "trusted": {
                    "union": ["balchik_fixed", "mobile"],
                },
            },
            "routes": [
                {
                    "name": "trusted_to_public",
                    "from_zone": "trusted",
                    "to": list(targets or AVAILABLE_TARGETS),
                }
            ],
        }
    }


def test_missing_routing_section_returns_none():
    assert load_optional_routing_table({}, TARGET_ID_BY_NAME) is None


def test_null_routing_section_returns_none():
    assert (
        load_optional_routing_table({"routing": None}, TARGET_ID_BY_NAME)
        is None
    )


def test_valid_routing_section_creates_routing_table():
    table = load_optional_routing_table(routing_config(), TARGET_ID_BY_NAME)

    assert isinstance(table, RoutingTable)
    assert table.match("udp:balchik_roof").target_ids == AVAILABLE_TARGETS
    assert table.match_target_ids("udp:balchik_roof") == (0, 1)


def test_compile_routing_section_matches_optional_loader_output():
    config = routing_config()

    direct = compile_routing_section(config["routing"], TARGET_ID_BY_NAME)
    optional = load_optional_routing_table(config, TARGET_ID_BY_NAME)

    assert direct.resolved_zones == optional.resolved_zones
    assert direct.route_definitions == optional.route_definitions
    assert direct.match("udp:balchik_roof") == optional.match("udp:balchik_roof")


def test_invalid_routing_section_type_is_rejected():
    with pytest.raises(RuntimeRoutingConfigError, match="must be a mapping"):
        load_optional_routing_table({"routing": []}, TARGET_ID_BY_NAME)


def test_compile_routing_section_rejects_invalid_section_type():
    with pytest.raises(RuntimeRoutingConfigError, match="must be a mapping"):
        compile_routing_section([], TARGET_ID_BY_NAME)


def test_compile_routing_section_reuses_optional_loader_validation_errors():
    config = routing_config()
    config["routing"]["enabled"] = True

    with pytest.raises(RuntimeRoutingConfigError) as direct_exc:
        compile_routing_section(config["routing"], TARGET_ID_BY_NAME)

    with pytest.raises(RuntimeRoutingConfigError) as optional_exc:
        load_optional_routing_table(config, TARGET_ID_BY_NAME)

    assert str(direct_exc.value) == str(optional_exc.value)


def test_unknown_routing_fields_are_rejected():
    config = routing_config()
    config["routing"]["enabled"] = True

    with pytest.raises(RuntimeRoutingConfigError, match="unknown field.*enabled"):
        load_optional_routing_table(config, TARGET_ID_BY_NAME)


@pytest.mark.parametrize("missing_field", ["zones", "routes"])
def test_missing_zones_or_routes_are_rejected(missing_field):
    config = routing_config()
    del config["routing"][missing_field]

    with pytest.raises(RuntimeRoutingConfigError, match=missing_field):
        load_optional_routing_table(config, TARGET_ID_BY_NAME)


def test_route_referencing_unavailable_udp_target_is_rejected():
    config = routing_config(targets=("udp:missing_target",))

    with pytest.raises(RuntimeRoutingConfigError, match="udp:missing_target"):
        load_optional_routing_table(config, TARGET_ID_BY_NAME)


def test_multiple_unknown_targets_are_reported_deterministically():
    config = routing_config(
        targets=("mongo:raw_archive", "udp:missing_target", "mqtt:clean_stream")
    )

    with pytest.raises(RuntimeRoutingConfigError) as exc_info:
        load_optional_routing_table(config, TARGET_ID_BY_NAME)

    assert str(exc_info.value).endswith(
        "mongo:raw_archive, mqtt:clean_stream, udp:missing_target."
    )


def test_compile_routing_section_target_errors_are_deterministic():
    config = routing_config(
        targets=("mongo:raw_archive", "udp:missing_target", "mqtt:clean_stream")
    )

    with pytest.raises(RuntimeRoutingConfigError) as exc_info:
        compile_routing_section(config["routing"], TARGET_ID_BY_NAME)

    assert str(exc_info.value).endswith(
        "mongo:raw_archive, mqtt:clean_stream, udp:missing_target."
    )


def test_transport_targets_without_installed_adapters_are_rejected():
    config = routing_config(targets=("mqtt:clean_stream",))

    with pytest.raises(RuntimeRoutingConfigError, match="mqtt:clean_stream"):
        load_optional_routing_table(config, TARGET_ID_BY_NAME)


@pytest.mark.parametrize(
    ("mapping", "exception"),
    [
        ("udp:aishub", TypeError),
        ([("udp:aishub", 0)], TypeError),
        ({1: 0}, TypeError),
        ({"": 0}, ValueError),
        ({"udp:aishub": True}, TypeError),
        ({"udp:aishub": 1.5}, TypeError),
        ({"udp:aishub": -1}, ValueError),
        ({"udp:aishub": 0, "udp:local_debug": 0}, ValueError),
    ],
)
def test_target_name_mapping_rejects_invalid_values_at_public_boundaries(
    mapping,
    exception,
):
    with pytest.raises(exception, match="target_id_by_name"):
        freeze_target_id_by_name(mapping)
    with pytest.raises(exception, match="target_id_by_name"):
        compile_routing_section(routing_config()["routing"], mapping)


def test_target_name_mapping_is_an_immutable_copy():
    original = {"udp:aishub": 4}

    frozen = freeze_target_id_by_name(original)
    original["udp:aishub"] = 9
    original["udp:later"] = 10

    assert dict(frozen) == {"udp:aishub": 4}
    with pytest.raises(TypeError):
        frozen["udp:aishub"] = 9


def test_runtime_compilation_copies_target_mapping():
    mapping = {
        "udp:aishub": 7,
        "udp:local_debug": 3,
    }
    table = load_optional_routing_table(routing_config(), mapping)

    mapping["udp:aishub"] = 99
    mapping["udp:local_debug"] = 98

    assert table.match_target_ids("udp:balchik_roof") == (7, 3)
