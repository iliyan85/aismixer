"""Runtime helpers for optional routing configuration."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from core.routing import RoutingTable


class RuntimeRoutingConfigError(ValueError):
    """Raised when optional runtime routing config is invalid."""


def load_optional_routing_table(
    config: Mapping[str, object],
    available_target_ids: Iterable[str],
) -> RoutingTable | None:
    """Compile optional runtime routing config and validate route targets."""

    if "routing" not in config or config["routing"] is None:
        return None

    routing_config = config["routing"]
    if not isinstance(routing_config, Mapping):
        raise RuntimeRoutingConfigError("'routing' config must be a mapping.")

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
    _validate_available_targets(table, available_target_ids)
    return table


def _validate_available_targets(
    table: RoutingTable,
    available_target_ids: Iterable[str],
) -> None:
    available = frozenset(available_target_ids)
    unknown = sorted({
        target_id
        for route in table.route_definitions
        for target_id in route.to
        if target_id not in available
    })
    if unknown:
        joined = ", ".join(unknown)
        raise RuntimeRoutingConfigError(
            f"Routing target ID(s) are unavailable or unsupported: {joined}."
        )
