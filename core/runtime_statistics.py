"""Process-local traffic owners and pull aggregation for runtime metrics."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from core.metrics import (
    EgressMetricsSnapshot,
    InputTrafficMetricsSnapshot,
    OutputTrafficMetricsSnapshot,
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


class InputTrafficMetricsSource(Protocol):
    """Structural contract for one input traffic metric owner."""

    def input_traffic_snapshot(self) -> InputTrafficMetricsSnapshot:
        ...


class OutputTrafficMetricsSource(Protocol):
    """Structural contract for the ordered output traffic metric owner."""

    def output_traffic_snapshot(
        self,
    ) -> tuple[OutputTrafficMetricsSnapshot, ...]:
        ...


class RuntimeStatisticsSource(Protocol):
    """Structural contract consumed by the transport-neutral control layer."""

    def snapshot(self) -> RuntimeStatisticsSnapshot:
        ...

    def input_traffic_snapshot(
        self,
    ) -> tuple[InputTrafficMetricsSnapshot, ...]:
        ...

    def output_traffic_snapshot(
        self,
    ) -> tuple[OutputTrafficMetricsSnapshot, ...]:
        ...


class InputTrafficMetrics:
    """Own process-local lifetime traffic counters for one runtime input."""

    __slots__ = (
        "_name",
        "_kind",
        "_transport_packets",
        "_transport_bytes",
        "_accepted_frames",
        "_payload_bytes",
    )

    def __init__(self, name: str, kind: str) -> None:
        initial = InputTrafficMetricsSnapshot(
            name=name,
            kind=kind,
            transport_packets=0,
            transport_bytes=0,
            accepted_frames=0,
            payload_bytes=0,
        )
        self._name = initial.name
        self._kind = initial.kind
        self._transport_packets = 0
        self._transport_bytes = 0
        self._accepted_frames = 0
        self._payload_bytes = 0

    def transport_received(self, data: bytes) -> None:
        """Account one raw datagram after its socket receive completes."""

        self._transport_packets += 1
        self._transport_bytes += len(data)

    def frame_accepted(self, payload: bytes) -> None:
        """Account one frame only after bounded queue admission completes."""

        self._accepted_frames += 1
        self._payload_bytes += len(payload)

    def input_traffic_snapshot(self) -> InputTrafficMetricsSnapshot:
        """Return a fresh immutable snapshot without resetting counters."""

        return InputTrafficMetricsSnapshot(
            name=self._name,
            kind=self._kind,
            transport_packets=self._transport_packets,
            transport_bytes=self._transport_bytes,
            accepted_frames=self._accepted_frames,
            payload_bytes=self._payload_bytes,
        )


@dataclass(frozen=True, slots=True)
class RuntimeStatisticsProvider:
    """Pull fresh snapshots from the metric owners of one runtime invocation."""

    ingress_queues: tuple[QueueMetricsSource, ...]
    processing_queue: QueueMetricsSource
    processor: ProcessorMetricsSource
    egress_queue: QueueMetricsSource
    egress_operations: EgressMetricsSource
    input_traffic: tuple[InputTrafficMetricsSource, ...]
    output_traffic: OutputTrafficMetricsSource | None

    def __init__(
        self,
        ingress_queues: Iterable[QueueMetricsSource],
        processing_queue: QueueMetricsSource,
        processor: ProcessorMetricsSource,
        egress_queue: QueueMetricsSource,
        egress_operations: EgressMetricsSource,
        *,
        input_traffic: Iterable[InputTrafficMetricsSource] = (),
        output_traffic: OutputTrafficMetricsSource | None = None,
    ) -> None:
        object.__setattr__(self, "ingress_queues", tuple(ingress_queues))
        object.__setattr__(self, "processing_queue", processing_queue)
        object.__setattr__(self, "processor", processor)
        object.__setattr__(self, "egress_queue", egress_queue)
        object.__setattr__(self, "egress_operations", egress_operations)
        object.__setattr__(self, "input_traffic", tuple(input_traffic))
        object.__setattr__(self, "output_traffic", output_traffic)

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

    def input_traffic_snapshot(
        self,
    ) -> tuple[InputTrafficMetricsSnapshot, ...]:
        """Pull fresh per-input snapshots in runtime declaration order."""

        return tuple(
            source.input_traffic_snapshot()
            for source in self.input_traffic
        )

    def output_traffic_snapshot(
        self,
    ) -> tuple[OutputTrafficMetricsSnapshot, ...]:
        """Pull fresh per-target snapshots from the runtime forwarder."""

        if self.output_traffic is None:
            return ()
        return self.output_traffic.output_traffic_snapshot()
