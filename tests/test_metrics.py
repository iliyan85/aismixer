from dataclasses import FrozenInstanceError, fields

import pytest

from core.metrics import (
    EgressMetricsSnapshot,
    ProcessorMetricsSnapshot,
    QueueMetricsSnapshot,
    RuntimeStatisticsSnapshot,
)


QUEUE_FIELDS = (
    "name",
    "capacity",
    "depth",
    "peak_depth",
    "enqueued",
    "dequeued",
    "put_waits",
    "current_put_waiters",
)
QUEUE_NUMERIC_FIELDS = QUEUE_FIELDS[1:]
PROCESSOR_FIELDS = (
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
)
EGRESS_FIELDS = (
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
)
RUNTIME_STATISTICS_FIELDS = (
    "ingress_queues",
    "processing_queue",
    "processor",
    "egress_queue",
    "egress_operations",
)
INVALID_NUMERIC_VALUES = (
    pytest.param(True, id="true-bool"),
    pytest.param(False, id="false-bool"),
    pytest.param(1.0, id="float"),
    pytest.param("1", id="string"),
    pytest.param(None, id="none"),
    pytest.param(object(), id="object"),
)


def queue_snapshot(**overrides):
    values = {
        "name": "udp-ingress:0:station-a",
        "capacity": 5,
        "depth": 2,
        "peak_depth": 4,
        "enqueued": 7,
        "dequeued": 5,
        "put_waits": 3,
        "current_put_waiters": 1,
    }
    values.update(overrides)
    return QueueMetricsSnapshot(**values)


def processor_snapshot(**overrides):
    values = {
        "process_calls": 6,
        "process_completed": 3,
        "process_failed": 2,
        "process_in_flight": 1,
        "outputless_calls": 1,
        "output_batches": 2,
        "output_messages": 5,
        "reset_calls": 4,
        "reset_completed": 2,
        "reset_failed": 1,
        "reset_in_flight": 1,
    }
    values.update(overrides)
    return ProcessorMetricsSnapshot(**values)


def egress_snapshot(**overrides):
    values = {
        "batches_started": 7,
        "batches_completed": 3,
        "batches_failed": 1,
        "batches_cancelled": 1,
        "active_batches": 2,
        "outputs_started": 10,
        "outputs_completed": 5,
        "outputs_failed": 2,
        "outputs_cancelled": 1,
        "active_outputs": 2,
    }
    values.update(overrides)
    return EgressMetricsSnapshot(**values)


def runtime_statistics_snapshot(**overrides):
    values = {
        "ingress_queues": (queue_snapshot(),),
        "processing_queue": queue_snapshot(name="processing"),
        "processor": processor_snapshot(),
        "egress_queue": queue_snapshot(name="egress"),
        "egress_operations": egress_snapshot(),
    }
    values.update(overrides)
    return RuntimeStatisticsSnapshot(**values)


def assert_frozen_slotted(snapshot, expected_fields, field_name):
    assert tuple(field.name for field in fields(snapshot)) == expected_fields
    assert not hasattr(snapshot, "__dict__")

    with pytest.raises(FrozenInstanceError):
        setattr(snapshot, field_name, getattr(snapshot, field_name) + 1)


def test_queue_metrics_snapshot_is_frozen_slotted_and_preserves_values():
    snapshot = queue_snapshot()

    assert_frozen_slotted(snapshot, QUEUE_FIELDS, "depth")
    assert tuple(getattr(snapshot, name) for name in QUEUE_FIELDS) == (
        "udp-ingress:0:station-a",
        5,
        2,
        4,
        7,
        5,
        3,
        1,
    )


@pytest.mark.parametrize(
    ("name", "exception"),
    [
        ("", ValueError),
        (None, TypeError),
        (1, TypeError),
        (True, TypeError),
        (b"queue", TypeError),
        ([], TypeError),
        (object(), TypeError),
    ],
)
def test_queue_metrics_snapshot_rejects_invalid_names(name, exception):
    with pytest.raises(exception, match="name"):
        queue_snapshot(name=name)


@pytest.mark.parametrize("field_name", QUEUE_NUMERIC_FIELDS)
@pytest.mark.parametrize("value", INVALID_NUMERIC_VALUES)
def test_queue_metrics_snapshot_rejects_non_integer_numeric_fields(
    field_name,
    value,
):
    with pytest.raises(TypeError, match=field_name):
        queue_snapshot(**{field_name: value})


@pytest.mark.parametrize("field_name", QUEUE_NUMERIC_FIELDS)
def test_queue_metrics_snapshot_rejects_negative_numeric_fields(field_name):
    with pytest.raises(ValueError, match=field_name):
        queue_snapshot(**{field_name: -1})


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"capacity": 0}, "capacity"),
        ({"depth": 6}, "depth"),
        ({"peak_depth": 1}, "peak_depth"),
        ({"peak_depth": 6}, "peak_depth"),
        ({"enqueued": 4}, "enqueued"),
        ({"enqueued": 8}, "enqueued minus dequeued"),
        ({"put_waits": 0}, "put_waits"),
    ],
)
def test_queue_metrics_snapshot_enforces_structural_invariants(
    overrides,
    message,
):
    with pytest.raises(ValueError, match=message):
        queue_snapshot(**overrides)


def test_queue_metrics_snapshot_accepts_empty_and_full_boundaries():
    assert queue_snapshot(
        capacity=1,
        depth=0,
        peak_depth=0,
        enqueued=0,
        dequeued=0,
        put_waits=0,
        current_put_waiters=0,
    ).depth == 0
    assert queue_snapshot(
        capacity=1,
        depth=1,
        peak_depth=1,
        enqueued=1,
        dequeued=0,
        put_waits=1,
        current_put_waiters=1,
    ).depth == 1


def test_processor_metrics_snapshot_is_frozen_slotted_and_preserves_values():
    snapshot = processor_snapshot()

    assert_frozen_slotted(snapshot, PROCESSOR_FIELDS, "process_calls")
    assert tuple(getattr(snapshot, name) for name in PROCESSOR_FIELDS) == (
        6,
        3,
        2,
        1,
        1,
        2,
        5,
        4,
        2,
        1,
        1,
    )


@pytest.mark.parametrize("field_name", PROCESSOR_FIELDS)
@pytest.mark.parametrize("value", INVALID_NUMERIC_VALUES)
def test_processor_metrics_snapshot_rejects_non_integer_fields(
    field_name,
    value,
):
    with pytest.raises(TypeError, match=field_name):
        processor_snapshot(**{field_name: value})


@pytest.mark.parametrize("field_name", PROCESSOR_FIELDS)
def test_processor_metrics_snapshot_rejects_negative_fields(field_name):
    with pytest.raises(ValueError, match=field_name):
        processor_snapshot(**{field_name: -1})


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"process_calls": 7}, "process_calls"),
        ({"outputless_calls": 0}, "process_completed"),
        ({"output_messages": 1}, "output_messages"),
        ({"reset_calls": 5}, "reset_calls"),
    ],
)
def test_processor_metrics_snapshot_enforces_call_and_output_invariants(
    overrides,
    message,
):
    with pytest.raises(ValueError, match=message):
        processor_snapshot(**overrides)


def test_processor_metrics_snapshot_accepts_all_zero_lifetime_state():
    snapshot = ProcessorMetricsSnapshot(
        **{field_name: 0 for field_name in PROCESSOR_FIELDS}
    )

    assert all(getattr(snapshot, name) == 0 for name in PROCESSOR_FIELDS)


def test_egress_metrics_snapshot_is_frozen_slotted_and_preserves_values():
    snapshot = egress_snapshot()

    assert_frozen_slotted(snapshot, EGRESS_FIELDS, "batches_started")
    assert tuple(getattr(snapshot, name) for name in EGRESS_FIELDS) == (
        7,
        3,
        1,
        1,
        2,
        10,
        5,
        2,
        1,
        2,
    )


@pytest.mark.parametrize("field_name", EGRESS_FIELDS)
@pytest.mark.parametrize("value", INVALID_NUMERIC_VALUES)
def test_egress_metrics_snapshot_rejects_non_integer_fields(
    field_name,
    value,
):
    with pytest.raises(TypeError, match=field_name):
        egress_snapshot(**{field_name: value})


@pytest.mark.parametrize("field_name", EGRESS_FIELDS)
def test_egress_metrics_snapshot_rejects_negative_fields(field_name):
    with pytest.raises(ValueError, match=field_name):
        egress_snapshot(**{field_name: -1})


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"batches_started": 8}, "batches_started"),
        ({"outputs_started": 11}, "outputs_started"),
    ],
)
def test_egress_metrics_snapshot_enforces_lifecycle_invariants(
    overrides,
    message,
):
    with pytest.raises(ValueError, match=message):
        egress_snapshot(**overrides)


def test_egress_metrics_snapshot_accepts_all_zero_lifetime_state():
    snapshot = EgressMetricsSnapshot(
        **{field_name: 0 for field_name in EGRESS_FIELDS}
    )

    assert all(getattr(snapshot, name) == 0 for name in EGRESS_FIELDS)


def test_runtime_statistics_snapshot_is_frozen_slotted_and_preserves_fields():
    first_ingress = queue_snapshot(name="udpsec-ingress:0:secure-a")
    second_ingress = queue_snapshot(name="udp-ingress:0:station-a")
    processing = queue_snapshot(name="processing")
    processor = processor_snapshot()
    egress_queue = queue_snapshot(name="egress")
    egress_operations = egress_snapshot()

    snapshot = RuntimeStatisticsSnapshot(
        ingress_queues=(first_ingress, second_ingress),
        processing_queue=processing,
        processor=processor,
        egress_queue=egress_queue,
        egress_operations=egress_operations,
    )

    assert (
        tuple(field.name for field in fields(snapshot))
        == RUNTIME_STATISTICS_FIELDS
    )
    assert not hasattr(snapshot, "__dict__")
    assert snapshot.ingress_queues == (first_ingress, second_ingress)
    assert snapshot.processing_queue is processing
    assert snapshot.processor is processor
    assert snapshot.egress_queue is egress_queue
    assert snapshot.egress_operations is egress_operations

    with pytest.raises(FrozenInstanceError):
        snapshot.processor = processor_snapshot()


def test_runtime_statistics_snapshot_normalizes_ingress_iterable_to_tuple():
    ordered = (
        queue_snapshot(name="udpsec-ingress:0:secure-a"),
        queue_snapshot(name="udp-ingress:0:station-a"),
    )

    snapshot = runtime_statistics_snapshot(
        ingress_queues=(queue for queue in ordered),
    )

    assert isinstance(snapshot.ingress_queues, tuple)
    assert snapshot.ingress_queues == ordered
    assert snapshot.ingress_queues[0] is ordered[0]
    assert snapshot.ingress_queues[1] is ordered[1]


def test_runtime_statistics_snapshot_accepts_empty_ingress_collection():
    snapshot = runtime_statistics_snapshot(ingress_queues=[])

    assert snapshot.ingress_queues == ()


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"ingress_queues": object()}, "ingress_queues"),
        (
            {"ingress_queues": (queue_snapshot(), object())},
            "ingress_queues",
        ),
        ({"processing_queue": object()}, "processing_queue"),
        ({"processor": object()}, "processor"),
        ({"egress_queue": object()}, "egress_queue"),
        ({"egress_operations": object()}, "egress_operations"),
    ],
)
def test_runtime_statistics_snapshot_rejects_invalid_component_types(
    overrides,
    message,
):
    with pytest.raises(TypeError, match=message):
        runtime_statistics_snapshot(**overrides)


def test_metric_contract_fields_do_not_claim_downstream_delivery():
    field_names = {
        field_name
        for snapshot_type in (
            QueueMetricsSnapshot,
            ProcessorMetricsSnapshot,
            EgressMetricsSnapshot,
            RuntimeStatisticsSnapshot,
        )
        for field_name in (
            field.name for field in fields(snapshot_type)
        )
    }

    assert field_names.isdisjoint(
        {
            "messages_delivered",
            "records_persisted",
            "remote_acknowledged",
            "database_committed",
        }
    )
