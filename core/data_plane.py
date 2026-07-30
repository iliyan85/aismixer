"""Immutable, transport-agnostic contracts for data-plane processing."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from core.ingress_frame import IngressFrame
from core.target_identity import EgressTargetId


class DeduplicationMode(Enum):
    """Deduplication scope selected for one accepted ingress frame."""

    GLOBAL = "global"
    PER_TARGET = "per-target"


@dataclass(frozen=True, slots=True)
class ProcessingSnapshot:
    """Immutable target-only processing view for one accepted ingress frame."""

    routing_generation: int
    deduplication_mode: DeduplicationMode
    target_ids: tuple[EgressTargetId, ...]

    def __post_init__(self) -> None:
        if isinstance(self.routing_generation, bool) or not isinstance(
            self.routing_generation,
            int,
        ):
            raise TypeError("routing_generation must be a non-negative integer.")
        if self.routing_generation < 0:
            raise ValueError("routing_generation must be a non-negative integer.")
        if not isinstance(self.deduplication_mode, DeduplicationMode):
            raise TypeError(
                "deduplication_mode must be a DeduplicationMode."
            )
        if isinstance(self.target_ids, (str, bytes)):
            raise TypeError(
                "target_ids must be a non-string iterable of integers."
            )
        try:
            target_ids = tuple(self.target_ids)
        except TypeError:
            raise TypeError(
                "target_ids must be a non-string iterable of integers."
            ) from None

        _validate_numeric_target_ids(target_ids)
        if len(set(target_ids)) != len(target_ids):
            raise ValueError("ProcessingSnapshot target_ids must be unique.")
        object.__setattr__(
            self,
            "target_ids",
            target_ids,
        )


class RoutingDisposition(Enum):
    """How orchestration should deliver one processor output."""

    LEGACY_BROADCAST = "legacy-broadcast"
    TARGETED = "targeted"


@dataclass(frozen=True, slots=True)
class ProcessorOutput:
    """One fully formatted message and its transport-independent routing.

    ``message`` is a completely formatted, normally CRLF-terminated immutable
    ``bytes`` payload.
    """

    message: bytes
    disposition: RoutingDisposition
    target_ids: tuple[EgressTargetId, ...] = ()

    def __post_init__(self) -> None:
        if type(self.message) is not bytes:
            raise TypeError("message must be immutable bytes.")
        if not isinstance(self.disposition, RoutingDisposition):
            raise TypeError("disposition must be a RoutingDisposition.")
        if isinstance(self.target_ids, (str, bytes)) or not isinstance(
            self.target_ids,
            Sequence,
        ):
            raise TypeError("target_ids must be a sequence of integers.")

        target_ids = tuple(self.target_ids)
        _validate_numeric_target_ids(target_ids)
        object.__setattr__(self, "target_ids", target_ids)

        if self.disposition is RoutingDisposition.LEGACY_BROADCAST:
            if target_ids:
                raise ValueError(
                    "legacy broadcast output must not contain target IDs."
                )
        elif not target_ids:
            raise ValueError("targeted output must contain at least one target ID.")


def _validate_numeric_target_ids(
    target_ids: Iterable[EgressTargetId],
) -> None:
    for target_id in target_ids:
        if isinstance(target_id, bool) or not isinstance(target_id, int):
            raise TypeError(
                "target_ids must contain only non-negative integers."
            )
        if target_id < 0:
            raise ValueError(
                "target_ids must contain only non-negative integers."
            )


@runtime_checkable
class DataPlaneProcessor(Protocol):
    """Synchronous processing boundary, independent of forwarding transports."""

    def process(
        self,
        frame: IngressFrame,
        snapshot: ProcessingSnapshot,
    ) -> tuple[ProcessorOutput, ...]:
        ...
