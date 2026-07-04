"""Process-local routing snapshot state.

This module stores the active immutable RoutingTable snapshot for one running
process. Dynamic updates must build and validate a candidate RoutingTable first,
then replace the whole snapshot atomically. Existing RoutingTable instances are
never mutated. Future multiprocessing deployments will need a coordinator or
IPC mechanism; this state does not synchronize across processes.
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import Lock

from core.routing import RoutingTable


@dataclass(frozen=True, slots=True)
class RoutingSnapshot:
    generation: int
    table: RoutingTable | None


class StaleRoutingGenerationError(RuntimeError):
    """Raised when an optimistic routing-state update uses a stale generation."""

    def __init__(self, expected_generation: int, actual_generation: int):
        self.expected_generation = expected_generation
        self.actual_generation = actual_generation
        super().__init__(
            "Stale routing generation: "
            f"expected {expected_generation}, actual {actual_generation}."
        )


class RoutingState:
    """Thread-safe process-local holder for immutable routing snapshots."""

    def __init__(self, initial_table: RoutingTable | None = None):
        _validate_table(initial_table)
        self._lock = Lock()
        self._snapshot = RoutingSnapshot(generation=0, table=initial_table)

    def snapshot(self) -> RoutingSnapshot:
        with self._lock:
            return self._snapshot

    def replace(
        self,
        table: RoutingTable | None,
        expected_generation: int | None = None,
    ) -> RoutingSnapshot:
        _validate_table(table)
        with self._lock:
            current = self._snapshot
            if (
                expected_generation is not None
                and expected_generation != current.generation
            ):
                raise StaleRoutingGenerationError(
                    expected_generation=expected_generation,
                    actual_generation=current.generation,
                )

            next_snapshot = RoutingSnapshot(
                generation=current.generation + 1,
                table=table,
            )
            self._snapshot = next_snapshot
            return next_snapshot


def _validate_table(table: RoutingTable | None) -> None:
    if table is not None and not isinstance(table, RoutingTable):
        raise TypeError("RoutingState table must be a RoutingTable or None.")
