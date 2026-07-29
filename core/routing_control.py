"""Transport-neutral process-local routing control service.

Future control transports such as CLIs, sockets, HTTP handlers, or peer links
should delegate routing updates here instead of compiling or installing tables
themselves. Candidate configs are fully compiled and validated before atomic
installation, and RoutingState remains the single owner of generation numbers.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from core.routing import RoutingTable, ZoneResolutionError
from core.routing_state import RoutingSnapshot, RoutingState
from core.runtime_routing import RuntimeRoutingConfigError, compile_routing_section
from core.target_identity import EgressTargetId, freeze_target_id_by_name


class RoutingCandidateConfigError(ValueError):
    """Raised when a candidate routing section cannot be compiled or validated."""


@dataclass(frozen=True, slots=True)
class RoutingControlStatus:
    """Immutable status view for process-local routing control adapters."""

    generation: int
    enabled: bool
    zone_names: tuple[str, ...]
    route_names: tuple[str, ...]
    target_ids: tuple[str, ...]


class RoutingControlService:
    """Validate and atomically install process-local routing snapshots.

    This service is intentionally transport-neutral. It does not know about
    sockets, CLIs, HTTP, or runtime globals; adapters pass plain candidate
    routing sections here and RoutingState owns all generation semantics.
    """

    def __init__(
        self,
        routing_state: RoutingState,
        target_id_by_name: Mapping[str, EgressTargetId],
    ):
        if not isinstance(routing_state, RoutingState):
            raise TypeError("routing_state must be a RoutingState.")

        self._routing_state = routing_state
        self._target_id_by_name = freeze_target_id_by_name(target_id_by_name)

    def status(self) -> RoutingControlStatus:
        """Return a stable immutable view of the current routing snapshot."""

        return _status_from_snapshot(self._routing_state.snapshot())

    def replace_from_config(
        self,
        routing_config: Mapping[str, object],
        expected_generation: int | None = None,
    ) -> RoutingControlStatus:
        """Compile, validate, and atomically install a candidate config."""

        try:
            candidate_table = compile_routing_section(
                routing_config,
                self._target_id_by_name,
            )
        except (
            RuntimeRoutingConfigError,
            ZoneResolutionError,
            TypeError,
            ValueError,
        ) as exc:
            raise RoutingCandidateConfigError(str(exc)) from exc

        snapshot = self._routing_state.replace(
            candidate_table,
            expected_generation=expected_generation,
        )
        return _status_from_snapshot(snapshot)

    def disable(
        self,
        expected_generation: int | None = None,
    ) -> RoutingControlStatus:
        """Disable routing by atomically installing a disabled snapshot."""

        snapshot = self._routing_state.replace(
            None,
            expected_generation=expected_generation,
        )
        return _status_from_snapshot(snapshot)


def _status_from_snapshot(snapshot: RoutingSnapshot) -> RoutingControlStatus:
    table = snapshot.table
    if table is None:
        return RoutingControlStatus(
            generation=snapshot.generation,
            enabled=False,
            zone_names=(),
            route_names=(),
            target_ids=(),
        )

    return RoutingControlStatus(
        generation=snapshot.generation,
        enabled=True,
        zone_names=tuple(table.resolved_zones),
        route_names=tuple(route.name for route in table.route_definitions),
        target_ids=_target_ids_from_routes(table),
    )


def _target_ids_from_routes(table: RoutingTable) -> tuple[str, ...]:
    target_ids: list[str] = []
    seen: set[str] = set()
    for route in table.route_definitions:
        for target_id in route.to:
            if target_id not in seen:
                seen.add(target_id)
                target_ids.append(target_id)
    return tuple(target_ids)
