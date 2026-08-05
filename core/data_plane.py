"""Immutable, transport-agnostic contracts for data-plane processing."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from core.ingress_frame import IngressFrame
from core.metrics import ProcessorMetricsSnapshot
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


@dataclass(frozen=True, slots=True)
class ProcessingWorkItem:
    """Immutable ingress-to-processor handoff with routing resolved."""

    frame: IngressFrame
    snapshot: ProcessingSnapshot

    def __post_init__(self) -> None:
        if not isinstance(self.frame, IngressFrame):
            raise TypeError("frame must be an IngressFrame.")
        if not isinstance(self.snapshot, ProcessingSnapshot):
            raise TypeError("snapshot must be a ProcessingSnapshot.")


@dataclass(frozen=True, slots=True)
class ProcessorOutput:
    """One fully formatted message and its explicit numeric targets.

    ``message`` is a completely formatted, normally CRLF-terminated immutable
    ``bytes`` payload.
    """

    message: bytes
    target_ids: tuple[EgressTargetId, ...]

    def __post_init__(self) -> None:
        if type(self.message) is not bytes:
            raise TypeError("message must be immutable bytes.")
        if isinstance(self.target_ids, (str, bytes)) or not isinstance(
            self.target_ids,
            Sequence,
        ):
            raise TypeError("target_ids must be a sequence of integers.")

        target_ids = tuple(self.target_ids)
        _validate_numeric_target_ids(target_ids)
        object.__setattr__(self, "target_ids", target_ids)


@dataclass(frozen=True, slots=True)
class OutputBatch:
    """Immutable ordered processor outputs for one accepted ingress frame."""

    outputs: tuple[ProcessorOutput, ...]

    def __post_init__(self) -> None:
        if isinstance(self.outputs, (str, bytes)) or not isinstance(
            self.outputs,
            Sequence,
        ):
            raise TypeError(
                "outputs must be a non-string sequence of ProcessorOutput values."
            )

        outputs = tuple(self.outputs)
        if not all(isinstance(output, ProcessorOutput) for output in outputs):
            raise TypeError(
                "outputs must contain only ProcessorOutput values."
            )
        object.__setattr__(self, "outputs", outputs)


@dataclass(frozen=True, slots=True)
class ProcessorResetReport:
    """Counts of processor-owned live state discarded by one reset call."""

    assembler_groups_discarded: int
    dedup_entries_discarded: int
    source_entries_discarded: int
    multipart_s_contexts_discarded: int
    multipart_c_contexts_discarded: int
    multipart_gid_contexts_discarded: int

    def __post_init__(self) -> None:
        for field_name in (
            "assembler_groups_discarded",
            "dedup_entries_discarded",
            "source_entries_discarded",
            "multipart_s_contexts_discarded",
            "multipart_c_contexts_discarded",
            "multipart_gid_contexts_discarded",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(
                    f"{field_name} must be a non-negative integer."
                )
            if value < 0:
                raise ValueError(
                    f"{field_name} must be a non-negative integer."
                )


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
    """Synchronous processing lifecycle independent of forwarding transports.

    Instances are usable immediately and require no asynchronous start, stop,
    or close operation. The owner must serialize calls to ``process()`` and
    ``reset()``; implementations are not required to make concurrent calls
    safe.
    """

    def process(
        self,
        frame: IngressFrame,
        snapshot: ProcessingSnapshot,
    ) -> OutputBatch:
        ...

    def reset(self) -> ProcessorResetReport:
        """Synchronously discard live state while retaining configuration."""

        ...

    def metrics_snapshot(self) -> ProcessorMetricsSnapshot:
        """Return immutable lifetime metrics without changing processor state."""

        ...
