"""Immutable, transport-agnostic runtime metrics snapshots."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class QueueMetricsSnapshot:
    """Lifetime counters and current state for one bounded item queue."""

    name: str
    capacity: int
    depth: int
    peak_depth: int
    enqueued: int
    dequeued: int
    put_waits: int
    current_put_waiters: int

    def __post_init__(self) -> None:
        if not isinstance(self.name, str):
            raise TypeError("name must be a non-empty string.")
        if not self.name:
            raise ValueError("name must be a non-empty string.")

        for field_name in (
            "capacity",
            "depth",
            "peak_depth",
            "enqueued",
            "dequeued",
            "put_waits",
            "current_put_waiters",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{field_name} must be an integer.")
            if value < 0:
                raise ValueError(f"{field_name} must be non-negative.")

        if self.capacity < 1:
            raise ValueError("capacity must be at least 1.")
        if self.depth > self.capacity:
            raise ValueError("depth must not exceed capacity.")
        if self.peak_depth < self.depth:
            raise ValueError("peak_depth must not be below depth.")
        if self.peak_depth > self.capacity:
            raise ValueError("peak_depth must not exceed capacity.")
        if self.enqueued < self.dequeued:
            raise ValueError("enqueued must not be below dequeued.")
        if self.enqueued - self.dequeued != self.depth:
            raise ValueError("enqueued minus dequeued must equal depth.")
        if self.put_waits < self.current_put_waiters:
            raise ValueError(
                "put_waits must not be below current_put_waiters."
            )


@dataclass(frozen=True, slots=True)
class ProcessorMetricsSnapshot:
    """Lifetime call and output counters for one processor instance."""

    process_calls: int
    process_completed: int
    process_failed: int
    process_in_flight: int

    outputless_calls: int
    output_batches: int
    output_messages: int

    reset_calls: int
    reset_completed: int
    reset_failed: int
    reset_in_flight: int

    def __post_init__(self) -> None:
        for field_name in (
            "process_calls",
            "process_completed",
            "process_failed",
            "process_in_flight",
            "outputless_calls",
            "output_batches",
            "output_messages",
            "reset_calls",
            "reset_completed",
            "reset_failed",
            "reset_in_flight",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{field_name} must be an integer.")
            if value < 0:
                raise ValueError(f"{field_name} must be non-negative.")

        if self.process_calls != (
            self.process_completed
            + self.process_failed
            + self.process_in_flight
        ):
            raise ValueError(
                "process_calls must equal completed, failed, and in-flight "
                "process calls."
            )
        if self.process_completed != (
            self.outputless_calls + self.output_batches
        ):
            raise ValueError(
                "process_completed must equal outputless_calls plus "
                "output_batches."
            )
        if self.output_messages < self.output_batches:
            raise ValueError(
                "output_messages must not be below output_batches."
            )
        if self.reset_calls != (
            self.reset_completed + self.reset_failed + self.reset_in_flight
        ):
            raise ValueError(
                "reset_calls must equal completed, failed, and in-flight "
                "reset calls."
            )


@dataclass(frozen=True, slots=True)
class EgressMetricsSnapshot:
    """Lifetime local-operation counters for one egress stage."""

    batches_started: int
    batches_completed: int
    batches_failed: int
    batches_cancelled: int
    active_batches: int

    outputs_started: int
    outputs_completed: int
    outputs_failed: int
    outputs_cancelled: int
    active_outputs: int

    def __post_init__(self) -> None:
        for field_name in (
            "batches_started",
            "batches_completed",
            "batches_failed",
            "batches_cancelled",
            "active_batches",
            "outputs_started",
            "outputs_completed",
            "outputs_failed",
            "outputs_cancelled",
            "active_outputs",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{field_name} must be an integer.")
            if value < 0:
                raise ValueError(f"{field_name} must be non-negative.")

        if self.batches_started != (
            self.batches_completed
            + self.batches_failed
            + self.batches_cancelled
            + self.active_batches
        ):
            raise ValueError(
                "batches_started must equal completed, failed, cancelled, "
                "and active batches."
            )
        if self.outputs_started != (
            self.outputs_completed
            + self.outputs_failed
            + self.outputs_cancelled
            + self.active_outputs
        ):
            raise ValueError(
                "outputs_started must equal completed, failed, cancelled, "
                "and active outputs."
            )


@dataclass(frozen=True, slots=True)
class RuntimeStatisticsSnapshot:
    """One immutable pull of the runtime's existing metric owners."""

    ingress_queues: tuple[QueueMetricsSnapshot, ...]
    processing_queue: QueueMetricsSnapshot
    processor: ProcessorMetricsSnapshot
    egress_queue: QueueMetricsSnapshot
    egress_operations: EgressMetricsSnapshot

    def __post_init__(self) -> None:
        try:
            ingress_queues = tuple(self.ingress_queues)
        except TypeError as exc:
            raise TypeError(
                "ingress_queues must be an iterable of QueueMetricsSnapshot."
            ) from exc
        object.__setattr__(self, "ingress_queues", ingress_queues)

        for index, snapshot in enumerate(ingress_queues):
            if not isinstance(snapshot, QueueMetricsSnapshot):
                raise TypeError(
                    "ingress_queues entries must be QueueMetricsSnapshot "
                    f"instances; entry {index} is invalid."
                )

        expected_types = (
            ("processing_queue", QueueMetricsSnapshot),
            ("processor", ProcessorMetricsSnapshot),
            ("egress_queue", QueueMetricsSnapshot),
            ("egress_operations", EgressMetricsSnapshot),
        )
        for field_name, expected_type in expected_types:
            if not isinstance(getattr(self, field_name), expected_type):
                raise TypeError(
                    f"{field_name} must be a {expected_type.__name__}."
                )
