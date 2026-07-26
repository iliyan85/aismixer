"""Immutable, transport-agnostic contracts for data-plane processing."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from core.ingress_frame import IngressFrame
from core.routing import RoutingTable, TargetId

if TYPE_CHECKING:
    from core.routing_state import RoutingSnapshot


@dataclass(frozen=True, slots=True)
class ProcessingSnapshot:
    """Immutable routing view used to process one accepted ingress frame.

    ``routing_table is None`` denotes legacy broadcast/global-dedup mode. A
    table denotes routed/target-scoped-dedup mode.
    """

    routing_generation: int
    routing_table: RoutingTable | None

    def __post_init__(self) -> None:
        if isinstance(self.routing_generation, bool) or not isinstance(
            self.routing_generation,
            int,
        ):
            raise TypeError("routing_generation must be a non-negative integer.")
        if self.routing_generation < 0:
            raise ValueError("routing_generation must be a non-negative integer.")
        if self.routing_table is not None and not isinstance(
            self.routing_table,
            RoutingTable,
        ):
            raise TypeError("routing_table must be a RoutingTable or None.")

    @classmethod
    def from_routing_snapshot(
        cls,
        snapshot: RoutingSnapshot,
    ) -> ProcessingSnapshot:
        """Adapt an acquired immutable routing snapshot without retaining state."""

        from core.routing_state import RoutingSnapshot

        if not isinstance(snapshot, RoutingSnapshot):
            raise TypeError("snapshot must be a RoutingSnapshot.")
        return cls(
            routing_generation=snapshot.generation,
            routing_table=snapshot.table,
        )


class RoutingDisposition(Enum):
    """How orchestration should deliver one processor output."""

    LEGACY_BROADCAST = "legacy-broadcast"
    TARGETED = "targeted"


@dataclass(frozen=True, slots=True)
class ProcessorOutput:
    """One fully formatted message and its transport-independent routing.

    ``message`` retains the current CRLF-terminated ``str`` representation.
    """

    message: str
    disposition: RoutingDisposition
    target_ids: tuple[TargetId, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.message, str):
            raise TypeError("message must be a string.")
        if not isinstance(self.disposition, RoutingDisposition):
            raise TypeError("disposition must be a RoutingDisposition.")
        if isinstance(self.target_ids, (str, bytes)) or not isinstance(
            self.target_ids,
            Sequence,
        ):
            raise TypeError("target_ids must be a sequence of strings.")

        target_ids = tuple(self.target_ids)
        if not all(isinstance(target_id, str) for target_id in target_ids):
            raise TypeError("target_ids must contain only strings.")
        object.__setattr__(self, "target_ids", target_ids)

        if self.disposition is RoutingDisposition.LEGACY_BROADCAST:
            if target_ids:
                raise ValueError(
                    "legacy broadcast output must not contain target IDs."
                )
        elif not target_ids:
            raise ValueError("targeted output must contain at least one target ID.")


@runtime_checkable
class DataPlaneProcessor(Protocol):
    """Synchronous processing boundary, independent of forwarding transports."""

    def process(
        self,
        frame: IngressFrame,
        snapshot: ProcessingSnapshot,
    ) -> tuple[ProcessorOutput, ...]:
        ...
