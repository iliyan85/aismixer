"""Runtime helpers for optional routing configuration."""

from __future__ import annotations

from collections.abc import Mapping

from core.routing import RoutingTable
from core.target_identity import (
    EgressTargetId,
    freeze_target_id_by_name,
)


class RuntimeRoutingConfigError(ValueError):
    """Raised when optional runtime routing config is invalid."""


def compile_routing_section(
    routing_config: Mapping[str, object],
    target_id_by_name: Mapping[str, EgressTargetId],
) -> RoutingTable:
    """Compile routing config with immutable process-local numeric targets."""

    if not isinstance(routing_config, Mapping):
        raise RuntimeRoutingConfigError("'routing' config must be a mapping.")
    frozen_target_ids = freeze_target_id_by_name(target_id_by_name)

    valid_fields = {"zones", "routes"}
    unknown_fields = set(routing_config) - valid_fields
    if unknown_fields:
        unknown = ", ".join(sorted(str(field) for field in unknown_fields))
        raise RuntimeRoutingConfigError(
            f"'routing' config has unknown field(s): {unknown}."
        )

    missing_fields = valid_fields - set(routing_config)
    if missing_fields:
        missing = ", ".join(sorted(missing_fields))
        raise RuntimeRoutingConfigError(
            f"'routing' config is missing required field(s): {missing}."
        )

    table = RoutingTable.from_config(
        routing_config["zones"],
        routing_config["routes"],
    )
    _validate_available_targets(table, frozen_target_ids)
    return table.compile_target_ids(frozen_target_ids)


def load_optional_routing_table(
    config: Mapping[str, object],
    target_id_by_name: Mapping[str, EgressTargetId],
) -> RoutingTable | None:
    """Compile optional runtime routing config and validate route targets."""

    frozen_target_ids = freeze_target_id_by_name(target_id_by_name)
    if "routing" not in config or config["routing"] is None:
        return None

    return compile_routing_section(config["routing"], frozen_target_ids)


def _validate_available_targets(
    table: RoutingTable,
    target_id_by_name: Mapping[str, EgressTargetId],
) -> None:
    unknown = sorted({
        target_id
        for route in table.route_definitions
        for target_id in route.to
        if target_id not in target_id_by_name
    })
    if unknown:
        joined = ", ".join(unknown)
        raise RuntimeRoutingConfigError(
            f"Routing target ID(s) are unavailable or unsupported: {joined}."
        )
