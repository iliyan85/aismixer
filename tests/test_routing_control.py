import pytest

from core.routing_control import (
    RoutingCandidateConfigError,
    RoutingControlService,
    RoutingControlStatus,
)
from core.routing_state import RoutingState, StaleRoutingGenerationError
from core.runtime_routing import (
    compile_routing_section,
)


AVAILABLE_TARGETS = ("udp:a", "udp:b", "udp:c")


def routing_section(routes=None, zones=None):
    return {
        "zones": zones
        or {
            "source": {"include": ["udp:source"]},
            "backup": {"include": ["udp:backup"]},
        },
        "routes": routes
        or [
            {
                "name": "source_to_a",
                "from_zone": "source",
                "to": ["udp:a"],
            }
        ],
    }


def compiled_table(section=None):
    return compile_routing_section(section or routing_section(), AVAILABLE_TARGETS)


def test_constructor_copies_available_target_ids():
    available_target_ids = ["udp:a"]
    service = RoutingControlService(RoutingState(), available_target_ids)
    available_target_ids.append("udp:b")

    with pytest.raises(RoutingCandidateConfigError, match="udp:b"):
        service.replace_from_config(
            routing_section(
                routes=[
                    {
                        "name": "source_to_b",
                        "from_zone": "source",
                        "to": ["udp:b"],
                    }
                ]
            )
        )


def test_constructor_rejects_invalid_routing_state():
    with pytest.raises(TypeError, match="routing_state"):
        RoutingControlService(object(), AVAILABLE_TARGETS)


@pytest.mark.parametrize(
    "available_target_ids",
    ["udp:a", object(), ["udp:a", 1]],
)
def test_constructor_rejects_invalid_available_target_ids(available_target_ids):
    with pytest.raises(TypeError, match="available_target_ids"):
        RoutingControlService(RoutingState(), available_target_ids)


def test_status_reports_disabled_generation_zero_state():
    service = RoutingControlService(RoutingState(), AVAILABLE_TARGETS)

    assert service.status() == RoutingControlStatus(
        generation=0,
        enabled=False,
        zone_names=(),
        route_names=(),
        target_ids=(),
    )


def test_status_reports_enabled_table_details():
    routes = [
        {
            "name": "source_primary",
            "from_zone": "source",
            "to": ["udp:a", "udp:b"],
        },
        {
            "name": "backup_secondary",
            "from_zone": "backup",
            "to": ["udp:b", "udp:c"],
        },
    ]
    state = RoutingState(compiled_table(routing_section(routes=routes)))
    service = RoutingControlService(state, AVAILABLE_TARGETS)

    assert service.status() == RoutingControlStatus(
        generation=0,
        enabled=True,
        zone_names=("backup", "source"),
        route_names=("source_primary", "backup_secondary"),
        target_ids=("udp:a", "udp:b", "udp:c"),
    )


def test_status_does_not_increment_generation():
    service = RoutingControlService(RoutingState(), AVAILABLE_TARGETS)

    assert service.status().generation == 0
    assert service.status().generation == 0


def test_replace_from_config_installs_valid_candidate():
    state = RoutingState()
    service = RoutingControlService(state, AVAILABLE_TARGETS)

    status = service.replace_from_config(routing_section())

    assert status.enabled is True
    assert state.snapshot().table.match("udp:source").target_ids == ("udp:a",)


def test_successful_replacement_increments_generation_once():
    service = RoutingControlService(RoutingState(), AVAILABLE_TARGETS)

    status = service.replace_from_config(routing_section())

    assert status.generation == 1
    assert service.status().generation == 1


def test_replacement_preserves_route_declaration_order_in_status():
    routes = [
        {"name": "first_route", "from_zone": "source", "to": ["udp:a"]},
        {"name": "second_route", "from_zone": "backup", "to": ["udp:b"]},
    ]
    service = RoutingControlService(RoutingState(), AVAILABLE_TARGETS)

    status = service.replace_from_config(routing_section(routes=routes))

    assert status.route_names == ("first_route", "second_route")


def test_status_target_ids_are_deduplicated_by_first_occurrence():
    routes = [
        {
            "name": "first_route",
            "from_zone": "source",
            "to": ["udp:b", "udp:a"],
        },
        {
            "name": "second_route",
            "from_zone": "backup",
            "to": ["udp:b", "udp:c"],
        },
    ]
    service = RoutingControlService(RoutingState(), AVAILABLE_TARGETS)

    status = service.replace_from_config(routing_section(routes=routes))

    assert status.target_ids == ("udp:b", "udp:a", "udp:c")


def test_invalid_routing_config_leaves_state_unchanged():
    state = RoutingState(compiled_table())
    service = RoutingControlService(state, AVAILABLE_TARGETS)
    before = state.snapshot()

    with pytest.raises(RoutingCandidateConfigError, match="missing required"):
        service.replace_from_config({"zones": {"source": {"include": ["udp:source"]}}})

    assert state.snapshot() is before


def test_malformed_candidate_config_raises_candidate_config_error():
    service = RoutingControlService(RoutingState(), AVAILABLE_TARGETS)

    with pytest.raises(RoutingCandidateConfigError, match="missing required"):
        service.replace_from_config({"zones": {"source": {"include": ["udp:source"]}}})


def test_unknown_target_leaves_state_unchanged():
    state = RoutingState(compiled_table())
    service = RoutingControlService(state, AVAILABLE_TARGETS)
    before = state.snapshot()

    with pytest.raises(RoutingCandidateConfigError, match="udp:missing"):
        service.replace_from_config(
            routing_section(
                routes=[
                    {
                        "name": "source_to_missing",
                        "from_zone": "source",
                        "to": ["udp:missing"],
                    }
                ]
            )
        )

    assert state.snapshot() is before


def test_unavailable_target_raises_candidate_config_error():
    service = RoutingControlService(RoutingState(), AVAILABLE_TARGETS)

    with pytest.raises(RoutingCandidateConfigError, match="udp:missing"):
        service.replace_from_config(
            routing_section(
                routes=[
                    {
                        "name": "source_to_missing",
                        "from_zone": "source",
                        "to": ["udp:missing"],
                    }
                ]
            )
        )


def test_candidate_config_error_retains_original_message():
    service = RoutingControlService(RoutingState(), AVAILABLE_TARGETS)

    with pytest.raises(RoutingCandidateConfigError) as exc_info:
        service.replace_from_config(
            routing_section(
                routes=[
                    {
                        "name": "source_to_missing",
                        "from_zone": "source",
                        "to": ["udp:missing"],
                    }
                ]
            )
        )

    assert str(exc_info.value) == (
        "Routing target ID(s) are unavailable or unsupported: udp:missing."
    )


def test_stale_expected_generation_raises_stale_error():
    service = RoutingControlService(RoutingState(), AVAILABLE_TARGETS)
    service.replace_from_config(routing_section())

    with pytest.raises(StaleRoutingGenerationError):
        service.replace_from_config(routing_section(), expected_generation=0)


def test_stale_update_leaves_state_unchanged():
    state = RoutingState()
    service = RoutingControlService(state, AVAILABLE_TARGETS)
    service.replace_from_config(routing_section())
    before = state.snapshot()

    with pytest.raises(StaleRoutingGenerationError):
        service.replace_from_config(
            routing_section(
                routes=[
                    {
                        "name": "source_to_b",
                        "from_zone": "source",
                        "to": ["udp:b"],
                    }
                ]
            ),
            expected_generation=0,
        )

    assert state.snapshot() is before
    assert state.snapshot().table.match("udp:source").target_ids == ("udp:a",)


def test_stale_generation_is_not_wrapped_as_candidate_config_error():
    service = RoutingControlService(RoutingState(), AVAILABLE_TARGETS)
    service.replace_from_config(routing_section())

    with pytest.raises(StaleRoutingGenerationError):
        service.replace_from_config(routing_section(), expected_generation=0)


def test_routing_state_replace_failure_is_not_wrapped(monkeypatch):
    state = RoutingState()
    service = RoutingControlService(state, AVAILABLE_TARGETS)

    def replace_raises(_table, expected_generation=None):
        raise RuntimeError("replace failed")

    monkeypatch.setattr(state, "replace", replace_raises)

    with pytest.raises(RuntimeError, match="replace failed"):
        service.replace_from_config(routing_section())


def test_matching_expected_generation_succeeds():
    service = RoutingControlService(RoutingState(), AVAILABLE_TARGETS)
    initial = service.status()

    status = service.replace_from_config(
        routing_section(),
        expected_generation=initial.generation,
    )

    assert status.generation == initial.generation + 1
    assert status.enabled is True


def test_disable_changes_enabled_to_false():
    service = RoutingControlService(
        RoutingState(compiled_table()),
        AVAILABLE_TARGETS,
    )

    status = service.disable()

    assert status.enabled is False
    assert status.zone_names == ()
    assert status.route_names == ()
    assert status.target_ids == ()


def test_disable_increments_generation_once():
    service = RoutingControlService(
        RoutingState(compiled_table()),
        AVAILABLE_TARGETS,
    )

    status = service.disable()

    assert status.generation == 1
    assert service.status().generation == 1


def test_stale_disable_leaves_state_unchanged():
    state = RoutingState(compiled_table())
    service = RoutingControlService(state, AVAILABLE_TARGETS)
    before = state.snapshot()

    with pytest.raises(StaleRoutingGenerationError):
        service.disable(expected_generation=99)

    assert state.snapshot() is before
    assert state.snapshot().table is before.table


def test_replacement_after_disable_reenables_routing():
    service = RoutingControlService(
        RoutingState(compiled_table()),
        AVAILABLE_TARGETS,
    )
    service.disable()

    status = service.replace_from_config(routing_section())

    assert status.enabled is True
    assert status.generation == 2


def test_old_snapshots_remain_valid_after_control_service_updates():
    state = RoutingState(compiled_table())
    service = RoutingControlService(state, AVAILABLE_TARGETS)
    old_snapshot = state.snapshot()

    service.replace_from_config(
        routing_section(
            routes=[
                {
                    "name": "source_to_b",
                    "from_zone": "source",
                    "to": ["udp:b"],
                }
            ]
        )
    )

    assert old_snapshot.generation == 0
    assert old_snapshot.table.match("udp:source").target_ids == ("udp:a",)
    assert state.snapshot().table.match("udp:source").target_ids == ("udp:b",)


def test_two_services_sharing_one_state_observe_same_generations():
    state = RoutingState()
    first = RoutingControlService(state, AVAILABLE_TARGETS)
    second = RoutingControlService(state, AVAILABLE_TARGETS)

    assert first.replace_from_config(routing_section()).generation == 1
    assert second.status().generation == 1
    assert second.disable(expected_generation=1).generation == 2
    assert first.status().generation == 2


def test_independent_routing_states_remain_independent():
    first = RoutingControlService(RoutingState(), AVAILABLE_TARGETS)
    second = RoutingControlService(RoutingState(), AVAILABLE_TARGETS)

    assert first.replace_from_config(routing_section()).generation == 1
    assert second.status().generation == 0
