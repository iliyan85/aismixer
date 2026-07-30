import inspect
from dataclasses import FrozenInstanceError, fields

import pytest

import core.data_plane as data_plane_module
from core.data_plane import (
    DataPlaneProcessor,
    DeduplicationMode,
    OutputBatch,
    ProcessingSnapshot,
    ProcessorOutput,
)
from core.ingress_frame import IngressFrame


class _BytesSubclass(bytes):
    pass


def make_frame() -> IngressFrame:
    return IngressFrame(
        kind="udp",
        source_id="udp:source",
        alias_for_s=None,
        remote_ip="192.0.2.10",
        assembler_key="192.0.2.10:17778",
        payload=b"!AIVDM,1,1,,A,payload,0*00",
    )


def test_deduplication_mode_has_exact_public_values():
    assert tuple(DeduplicationMode) == (
        DeduplicationMode.GLOBAL,
        DeduplicationMode.PER_TARGET,
    )
    assert DeduplicationMode.GLOBAL.value == "global"
    assert DeduplicationMode.PER_TARGET.value == "per-target"


def test_processing_snapshot_is_frozen_slotted_and_target_only():
    mutable_target_ids = [2, 0, 1]
    snapshot = ProcessingSnapshot(
        routing_generation=7,
        deduplication_mode=DeduplicationMode.PER_TARGET,
        target_ids=mutable_target_ids,
    )
    mutable_target_ids.append(3)

    with pytest.raises(FrozenInstanceError):
        snapshot.routing_generation = 8

    assert not hasattr(snapshot, "__dict__")
    assert not hasattr(snapshot, "routing_table")
    assert tuple(field.name for field in fields(snapshot)) == (
        "routing_generation",
        "deduplication_mode",
        "target_ids",
    )
    assert snapshot.routing_generation == 7
    assert snapshot.deduplication_mode is DeduplicationMode.PER_TARGET
    assert snapshot.target_ids == (2, 0, 1)
    assert isinstance(snapshot.target_ids, tuple)


def test_processing_snapshot_copies_a_non_string_iterable():
    snapshot = ProcessingSnapshot(
        routing_generation=0,
        deduplication_mode=DeduplicationMode.PER_TARGET,
        target_ids=(target_id for target_id in (4, 1, 3)),
    )

    assert snapshot.target_ids == (4, 1, 3)


def test_processing_snapshot_distinguishes_empty_global_and_per_target_modes():
    global_snapshot = ProcessingSnapshot(
        routing_generation=3,
        deduplication_mode=DeduplicationMode.GLOBAL,
        target_ids=(),
    )
    routed_snapshot = ProcessingSnapshot(
        routing_generation=3,
        deduplication_mode=DeduplicationMode.PER_TARGET,
        target_ids=(),
    )

    assert global_snapshot.target_ids == routed_snapshot.target_ids == ()
    assert global_snapshot != routed_snapshot
    assert global_snapshot.deduplication_mode is DeduplicationMode.GLOBAL
    assert routed_snapshot.deduplication_mode is DeduplicationMode.PER_TARGET


@pytest.mark.parametrize(
    ("routing_generation", "exception"),
    [
        (True, TypeError),
        (1.0, TypeError),
        ("1", TypeError),
        (-1, ValueError),
    ],
)
def test_processing_snapshot_rejects_invalid_generation(
    routing_generation,
    exception,
):
    with pytest.raises(exception):
        ProcessingSnapshot(
            routing_generation=routing_generation,
            deduplication_mode=DeduplicationMode.GLOBAL,
            target_ids=(),
        )


@pytest.mark.parametrize("mode", ["global", None, object()])
def test_processing_snapshot_rejects_invalid_deduplication_mode(mode):
    with pytest.raises(TypeError, match="DeduplicationMode"):
        ProcessingSnapshot(
            routing_generation=0,
            deduplication_mode=mode,
            target_ids=(),
        )


@pytest.mark.parametrize(
    "target_ids",
    [
        None,
        1,
        "1",
        b"\x01",
    ],
)
def test_processing_snapshot_rejects_invalid_target_collection(target_ids):
    with pytest.raises(TypeError, match="iterable"):
        ProcessingSnapshot(
            routing_generation=0,
            deduplication_mode=DeduplicationMode.PER_TARGET,
            target_ids=target_ids,
        )


@pytest.mark.parametrize(
    ("target_id", "exception"),
    [
        (True, TypeError),
        ("1", TypeError),
        (1.0, TypeError),
        (object(), TypeError),
        (-1, ValueError),
    ],
)
def test_processing_snapshot_rejects_invalid_numeric_target_ids(
    target_id,
    exception,
):
    with pytest.raises(exception):
        ProcessingSnapshot(
            routing_generation=0,
            deduplication_mode=DeduplicationMode.PER_TARGET,
            target_ids=(target_id,),
        )


def test_processing_snapshot_rejects_duplicate_target_ids():
    with pytest.raises(ValueError, match="unique"):
        ProcessingSnapshot(
            routing_generation=0,
            deduplication_mode=DeduplicationMode.PER_TARGET,
            target_ids=(2, 1, 2),
        )


def test_processor_output_is_frozen_slotted_and_has_no_runtime_objects():
    message = b"formatted message\r\n"
    output = ProcessorOutput(
        message=message,
        target_ids=(),
    )

    with pytest.raises(FrozenInstanceError):
        output.message = b"changed\r\n"

    assert not hasattr(output, "__dict__")
    assert output.message is message
    assert tuple(field.name for field in fields(output)) == (
        "message",
        "target_ids",
    )


def test_processor_output_preserves_target_order_repeats_and_copies_input():
    target_ids = [2, 0, 2]

    output = ProcessorOutput(
        message=b"formatted message\r\n",
        target_ids=target_ids,
    )
    target_ids.append(1)

    assert output.target_ids == (2, 0, 2)
    assert isinstance(output.target_ids, tuple)


def test_processor_output_allows_explicit_empty_target_ids():
    output = ProcessorOutput(
        message=b"formatted message\r\n",
        target_ids=(),
    )

    assert output.target_ids == ()


def test_processor_output_requires_explicit_target_ids():
    with pytest.raises(TypeError):
        ProcessorOutput(message=b"formatted message\r\n")


def test_routing_disposition_is_not_part_of_the_public_contract():
    assert not hasattr(data_plane_module, "RoutingDisposition")


@pytest.mark.parametrize(
    ("message", "target_ids", "exception"),
    [
        ("message\r\n", (0,), TypeError),
        (
            bytearray(b"message\r\n"),
            (0,),
            TypeError,
        ),
        (
            memoryview(b"message\r\n"),
            (0,),
            TypeError,
        ),
        (
            _BytesSubclass(b"message\r\n"),
            (0,),
            TypeError,
        ),
        (object(), (0,), TypeError),
        (b"message\r\n", "0", TypeError),
        (b"message\r\n", b"\x00", TypeError),
        (
            b"message\r\n",
            (target_id for target_id in (0,)),
            TypeError,
        ),
        (b"message\r\n", (True,), TypeError),
        (b"message\r\n", ("0",), TypeError),
        (b"message\r\n", (0.0,), TypeError),
        (b"message\r\n", (object(),), TypeError),
        (b"message\r\n", (-1,), ValueError),
    ],
)
def test_processor_output_rejects_invalid_values(
    message,
    target_ids,
    exception,
):
    with pytest.raises(exception):
        ProcessorOutput(
            message=message,
            target_ids=target_ids,
        )


def test_processor_output_does_not_require_crlf_framing():
    payload = b"already formatted"

    output = ProcessorOutput(
        message=payload,
        target_ids=(),
    )

    assert output.message is payload


def test_output_batch_is_frozen_slotted_empty_and_transport_agnostic():
    batch = OutputBatch(outputs=())

    with pytest.raises(FrozenInstanceError):
        batch.outputs = ()

    assert not hasattr(batch, "__dict__")
    assert tuple(field.name for field in fields(batch)) == ("outputs",)
    assert batch.outputs == ()
    source = inspect.getsource(OutputBatch)
    for forbidden_name in (
        "asyncio",
        "Future",
        "Queue",
        "transport",
        "completion",
    ):
        assert forbidden_name not in source


def test_output_batch_preserves_order_identity_and_copies_mutable_input():
    first = ProcessorOutput(
        message=b"first\r\n",
        target_ids=(2, 0),
    )
    second = ProcessorOutput(
        message=b"second\r\n",
        target_ids=(),
    )
    mutable_outputs = [first, second]

    batch = OutputBatch(outputs=mutable_outputs)
    mutable_outputs.reverse()

    assert batch.outputs == (first, second)
    assert isinstance(batch.outputs, tuple)
    assert batch.outputs[0] is first
    assert batch.outputs[1] is second


@pytest.mark.parametrize(
    "outputs",
    [
        "outputs",
        b"outputs",
        (output for output in ()),
        object(),
        None,
    ],
)
def test_output_batch_rejects_invalid_output_collection(outputs):
    with pytest.raises(TypeError, match="sequence"):
        OutputBatch(outputs=outputs)


@pytest.mark.parametrize(
    "outputs",
    [
        (object(),),
        [
            ProcessorOutput(message=b"valid\r\n", target_ids=(0,)),
            object(),
        ],
    ],
)
def test_output_batch_rejects_non_processor_output_elements(outputs):
    with pytest.raises(TypeError, match="ProcessorOutput"):
        OutputBatch(outputs=outputs)


def test_minimal_synchronous_fake_satisfies_processor_protocol():
    output = ProcessorOutput(
        message=b"formatted message\r\n",
        target_ids=(),
    )

    class FakeProcessor:
        def process(self, frame, snapshot):
            assert frame is expected_frame
            assert snapshot is expected_snapshot
            return OutputBatch(outputs=(output,))

    expected_frame = make_frame()
    expected_snapshot = ProcessingSnapshot(
        routing_generation=0,
        deduplication_mode=DeduplicationMode.GLOBAL,
        target_ids=(),
    )
    processor: DataPlaneProcessor = FakeProcessor()

    assert isinstance(processor, DataPlaneProcessor)
    assert not inspect.iscoroutinefunction(processor.process)
    batch = processor.process(expected_frame, expected_snapshot)
    assert type(batch) is OutputBatch
    assert batch.outputs == (output,)
    assert batch.outputs[0] is output
