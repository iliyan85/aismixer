import pytest

from core.routing import RoutingTable
from core.runtime_routing import (
    RuntimeRoutingConfigError,
    compile_routing_section,
    load_optional_routing_table,
)


AVAILABLE_TARGETS = ("udp:aishub", "udp:local_debug")


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
    assert load_optional_routing_table({}, AVAILABLE_TARGETS) is None


def test_null_routing_section_returns_none():
    assert load_optional_routing_table({"routing": None}, AVAILABLE_TARGETS) is None


def test_valid_routing_section_creates_routing_table():
    table = load_optional_routing_table(routing_config(), AVAILABLE_TARGETS)

    assert isinstance(table, RoutingTable)
    assert table.match("udp:balchik_roof").target_ids == AVAILABLE_TARGETS


def test_compile_routing_section_matches_optional_loader_output():
    config = routing_config()

    direct = compile_routing_section(config["routing"], AVAILABLE_TARGETS)
    optional = load_optional_routing_table(config, AVAILABLE_TARGETS)

    assert direct.resolved_zones == optional.resolved_zones
    assert direct.route_definitions == optional.route_definitions
    assert direct.match("udp:balchik_roof") == optional.match("udp:balchik_roof")


def test_invalid_routing_section_type_is_rejected():
    with pytest.raises(RuntimeRoutingConfigError, match="must be a mapping"):
        load_optional_routing_table({"routing": []}, AVAILABLE_TARGETS)


def test_compile_routing_section_rejects_invalid_section_type():
    with pytest.raises(RuntimeRoutingConfigError, match="must be a mapping"):
        compile_routing_section([], AVAILABLE_TARGETS)


def test_compile_routing_section_reuses_optional_loader_validation_errors():
    config = routing_config()
    config["routing"]["enabled"] = True

    with pytest.raises(RuntimeRoutingConfigError) as direct_exc:
        compile_routing_section(config["routing"], AVAILABLE_TARGETS)

    with pytest.raises(RuntimeRoutingConfigError) as optional_exc:
        load_optional_routing_table(config, AVAILABLE_TARGETS)

    assert str(direct_exc.value) == str(optional_exc.value)


def test_unknown_routing_fields_are_rejected():
    config = routing_config()
    config["routing"]["enabled"] = True

    with pytest.raises(RuntimeRoutingConfigError, match="unknown field.*enabled"):
        load_optional_routing_table(config, AVAILABLE_TARGETS)


@pytest.mark.parametrize("missing_field", ["zones", "routes"])
def test_missing_zones_or_routes_are_rejected(missing_field):
    config = routing_config()
    del config["routing"][missing_field]

    with pytest.raises(RuntimeRoutingConfigError, match=missing_field):
        load_optional_routing_table(config, AVAILABLE_TARGETS)


def test_route_referencing_unavailable_udp_target_is_rejected():
    config = routing_config(targets=("udp:missing_target",))

    with pytest.raises(RuntimeRoutingConfigError, match="udp:missing_target"):
        load_optional_routing_table(config, AVAILABLE_TARGETS)


def test_multiple_unknown_targets_are_reported_deterministically():
    config = routing_config(
        targets=("mongo:raw_archive", "udp:missing_target", "mqtt:clean_stream")
    )

    with pytest.raises(RuntimeRoutingConfigError) as exc_info:
        load_optional_routing_table(config, AVAILABLE_TARGETS)

    assert str(exc_info.value).endswith(
        "mongo:raw_archive, mqtt:clean_stream, udp:missing_target."
    )


def test_compile_routing_section_target_errors_are_deterministic():
    config = routing_config(
        targets=("mongo:raw_archive", "udp:missing_target", "mqtt:clean_stream")
    )

    with pytest.raises(RuntimeRoutingConfigError) as exc_info:
        compile_routing_section(config["routing"], AVAILABLE_TARGETS)

    assert str(exc_info.value).endswith(
        "mongo:raw_archive, mqtt:clean_stream, udp:missing_target."
    )


def test_transport_targets_without_installed_adapters_are_rejected():
    config = routing_config(targets=("mqtt:clean_stream",))

    with pytest.raises(RuntimeRoutingConfigError, match="mqtt:clean_stream"):
        load_optional_routing_table(config, AVAILABLE_TARGETS)
