"""Process-local pull aggregation for existing runtime metric owners."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from core.metrics import (
    EgressMetricsSnapshot,
    ProcessorMetricsSnapshot,
    QueueMetricsSnapshot,
    RuntimeStatisticsSnapshot,
)


class QueueMetricsSource(Protocol):
    """Structural contract for one queue metric owner."""

    def metrics_snapshot(self) -> QueueMetricsSnapshot:
        ...


class ProcessorMetricsSource(Protocol):
    """Structural contract for one processor metric owner."""

    def metrics_snapshot(self) -> ProcessorMetricsSnapshot:
        ...


class EgressMetricsSource(Protocol):
    """Structural contract for the local egress-operation metric owner."""

    def metrics_snapshot(self) -> EgressMetricsSnapshot:
        ...


class RuntimeStatisticsSource(Protocol):
    """Structural contract consumed by the transport-neutral control layer."""

    def snapshot(self) -> RuntimeStatisticsSnapshot:
        ...


@dataclass(frozen=True, slots=True)
class RuntimeStatisticsProvider:
    """Pull fresh snapshots from the metric owners of one runtime invocation."""

    ingress_queues: tuple[QueueMetricsSource, ...]
    processing_queue: QueueMetricsSource
    processor: ProcessorMetricsSource
    egress_queue: QueueMetricsSource
    egress_operations: EgressMetricsSource

    def __init__(
        self,
        ingress_queues: Iterable[QueueMetricsSource],
        processing_queue: QueueMetricsSource,
        processor: ProcessorMetricsSource,
        egress_queue: QueueMetricsSource,
        egress_operations: EgressMetricsSource,
    ) -> None:
        object.__setattr__(self, "ingress_queues", tuple(ingress_queues))
        object.__setattr__(self, "processing_queue", processing_queue)
        object.__setattr__(self, "processor", processor)
        object.__setattr__(self, "egress_queue", egress_queue)
        object.__setattr__(self, "egress_operations", egress_operations)

    def snapshot(self) -> RuntimeStatisticsSnapshot:
        """Return one fresh aggregate without caching or mutating its sources."""

        return RuntimeStatisticsSnapshot(
            ingress_queues=tuple(
                source.metrics_snapshot() for source in self.ingress_queues
            ),
            processing_queue=self.processing_queue.metrics_snapshot(),
            processor=self.processor.metrics_snapshot(),
            egress_queue=self.egress_queue.metrics_snapshot(),
            egress_operations=self.egress_operations.metrics_snapshot(),
        )
