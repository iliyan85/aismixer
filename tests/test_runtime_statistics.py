from dataclasses import FrozenInstanceError, fields, replace

import pytest

import core.runtime_statistics as runtime_statistics_module
from core.metrics import (
    EgressMetricsSnapshot,
    ProcessorMetricsSnapshot,
    QueueMetricsSnapshot,
    RuntimeStatisticsSnapshot,
)
from core.runtime_statistics import RuntimeStatisticsProvider


PROVIDER_SOURCE_FIELDS = (
    "ingress_queues",
    "processing_queue",
    "processor",
    "egress_queue",
    "egress_operations",
)


def queue_snapshot(
    name,
    *,
    capacity=8,
    depth=0,
    peak_depth=0,
    enqueued=0,
    dequeued=0,
    put_waits=0,
    current_put_waiters=0,
):
    return QueueMetricsSnapshot(
        name=name,
        capacity=capacity,
        depth=depth,
        peak_depth=peak_depth,
        enqueued=enqueued,
        dequeued=dequeued,
        put_waits=put_waits,
        current_put_waiters=current_put_waiters,
    )


def processor_snapshot(*, completed=0):
    return ProcessorMetricsSnapshot(
        process_calls=completed,
        process_completed=completed,
        process_failed=0,
        process_in_flight=0,
        outputless_calls=completed,
        output_batches=0,
        output_messages=0,
        reset_calls=0,
        reset_completed=0,
        reset_failed=0,
        reset_in_flight=0,
    )


def egress_snapshot(*, completed=0):
    return EgressMetricsSnapshot(
        batches_started=completed,
        batches_completed=completed,
        batches_failed=0,
        batches_cancelled=0,
        active_batches=0,
        outputs_started=completed,
        outputs_completed=completed,
        outputs_failed=0,
        outputs_cancelled=0,
        active_outputs=0,
    )


class FakeMetricsSource:
    def __init__(self, name, current_snapshot, call_log=None):
        self.name = name
        self.current_snapshot = current_snapshot
        self.call_log = call_log
        self.snapshot_calls = 0
        self.reset_calls = 0

    def metrics_snapshot(self):
        self.snapshot_calls += 1
        if self.call_log is not None:
            self.call_log.append(self.name)
        return replace(self.current_snapshot)

    def reset(self):
        self.reset_calls += 1


def make_sources(call_log=None):
    return {
        "first_ingress": FakeMetricsSource(
            "first-ingress",
            queue_snapshot("udpsec-ingress:0:secure-a"),
            call_log,
        ),
        "second_ingress": FakeMetricsSource(
            "second-ingress",
            queue_snapshot("udp-ingress:0:station-a"),
            call_log,
        ),
        "processing_queue": FakeMetricsSource(
            "processing",
            queue_snapshot("processing"),
            call_log,
        ),
        "processor": FakeMetricsSource(
            "processor",
            processor_snapshot(),
            call_log,
        ),
        "egress_queue": FakeMetricsSource(
            "egress-queue",
            queue_snapshot("egress"),
            call_log,
        ),
        "egress_operations": FakeMetricsSource(
            "egress-operations",
            egress_snapshot(),
            call_log,
        ),
    }


def make_provider(sources, ingress_queues=None):
    if ingress_queues is None:
        ingress_queues = (
            sources["first_ingress"],
            sources["second_ingress"],
        )
    return RuntimeStatisticsProvider(
        ingress_queues,
        sources["processing_queue"],
        sources["processor"],
        sources["egress_queue"],
        sources["egress_operations"],
    )


def test_provider_normalizes_ingress_sources_and_preserves_order():
    sources = make_sources()
    ordered_sources = (
        sources["second_ingress"],
        sources["first_ingress"],
    )
    provider = make_provider(
        sources,
        ingress_queues=(source for source in ordered_sources),
    )

    snapshot = provider.snapshot()

    assert isinstance(provider.ingress_queues, tuple)
    assert provider.ingress_queues == ordered_sources
    assert tuple(queue.name for queue in snapshot.ingress_queues) == (
        "udp-ingress:0:station-a",
        "udpsec-ingress:0:secure-a",
    )


def test_provider_accepts_empty_ingress_collection():
    sources = make_sources()
    provider = make_provider(sources, ingress_queues=[])

    snapshot = provider.snapshot()

    assert isinstance(snapshot, RuntimeStatisticsSnapshot)
    assert snapshot.ingress_queues == ()


def test_provider_pulls_each_source_once_per_fresh_snapshot_in_field_order():
    call_log = []
    sources = make_sources(call_log)
    provider = make_provider(sources)

    first = provider.snapshot()
    second = provider.snapshot()

    expected_order = [
        "first-ingress",
        "second-ingress",
        "processing",
        "processor",
        "egress-queue",
        "egress-operations",
    ]
    assert call_log == expected_order * 2
    assert all(source.snapshot_calls == 2 for source in sources.values())
    assert first == second
    assert first is not second
    assert first.ingress_queues is not second.ingress_queues
    assert all(
        first_snapshot is not second_snapshot
        for first_snapshot, second_snapshot in zip(
            first.ingress_queues,
            second.ingress_queues,
        )
    )
    assert first.processing_queue is not second.processing_queue
    assert first.processor is not second.processor
    assert first.egress_queue is not second.egress_queue
    assert first.egress_operations is not second.egress_operations


def test_repeated_pulls_reflect_live_sources_without_resetting_them():
    sources = make_sources()
    provider = make_provider(sources)
    original_source_snapshots = {
        name: source.current_snapshot
        for name, source in sources.items()
    }

    initial = provider.snapshot()

    assert initial.processing_queue.enqueued == 0
    assert initial.processor.process_calls == 0
    assert initial.egress_operations.outputs_completed == 0
    assert {
        name: source.current_snapshot
        for name, source in sources.items()
    } == original_source_snapshots
    assert all(source.reset_calls == 0 for source in sources.values())

    sources["first_ingress"].current_snapshot = queue_snapshot(
        "udpsec-ingress:0:secure-a",
        depth=1,
        peak_depth=2,
        enqueued=2,
        dequeued=1,
        put_waits=1,
    )
    sources["processing_queue"].current_snapshot = queue_snapshot(
        "processing",
        peak_depth=1,
        enqueued=1,
        dequeued=1,
    )
    sources["processor"].current_snapshot = processor_snapshot(completed=1)
    sources["egress_queue"].current_snapshot = queue_snapshot(
        "egress",
        peak_depth=1,
        enqueued=1,
        dequeued=1,
    )
    sources["egress_operations"].current_snapshot = egress_snapshot(
        completed=1,
    )

    updated = provider.snapshot()

    assert updated.ingress_queues[0].enqueued == 2
    assert updated.processing_queue.enqueued == 1
    assert updated.processor.process_calls == 1
    assert updated.egress_queue.dequeued == 1
    assert updated.egress_operations.outputs_completed == 1
    assert all(source.reset_calls == 0 for source in sources.values())


def test_provider_contains_only_metric_source_references_not_counter_state():
    sources = make_sources()
    provider = make_provider(sources)

    assert (
        tuple(field.name for field in fields(provider))
        == PROVIDER_SOURCE_FIELDS
    )
    assert not hasattr(provider, "__dict__")
    assert provider.processing_queue is sources["processing_queue"]
    assert provider.processor is sources["processor"]
    assert provider.egress_queue is sources["egress_queue"]
    assert provider.egress_operations is sources["egress_operations"]

    with pytest.raises(FrozenInstanceError):
        provider.processor = sources["processor"]


def test_runtime_statistics_module_has_no_global_provider_instance():
    assert not any(
        isinstance(value, RuntimeStatisticsProvider)
        for value in vars(runtime_statistics_module).values()
    )
