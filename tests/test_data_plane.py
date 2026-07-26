import inspect
from dataclasses import FrozenInstanceError, fields

import pytest

from core.data_plane import (
    DataPlaneProcessor,
    ProcessingSnapshot,
    ProcessorOutput,
    RoutingDisposition,
)
from core.ingress_frame import IngressFrame
from core.routing import RoutingTable
from core.routing_state import RoutingSnapshot


def make_table() -> RoutingTable:
    return RoutingTable.from_definitions(
        {"source": {"include": ["udp:source"]}},
        [
            {
                "name": "source_to_targets",
                "from_zone": "source",
                "to": ["udp:first", "udp:second"],
            }
        ],
    )


def make_frame() -> IngressFrame:
    return IngressFrame(
        kind="udp",
        source_id="udp:source",
        alias_for_s=None,
        remote_ip="192.0.2.10",
        assembler_key="192.0.2.10:17778",
        payload=b"!AIVDM,1,1,,A,payload,0*00",
    )


def test_processing_snapshot_is_frozen_slotted_and_has_only_boundary_values():
    snapshot = ProcessingSnapshot(
        routing_generation=0,
        routing_table=None,
    )

    with pytest.raises(FrozenInstanceError):
        snapshot.routing_generation = 1

    assert not hasattr(snapshot, "__dict__")
    assert tuple(field.name for field in fields(snapshot)) == (
        "routing_generation",
        "routing_table",
    )


def test_processing_snapshot_preserves_generation_and_routing_table_identity():
    table = make_table()

    snapshot = ProcessingSnapshot(
        routing_generation=7,
        routing_table=table,
    )

    assert snapshot.routing_generation == 7
    assert snapshot.routing_table is table


def test_processing_snapshot_none_table_explicitly_represents_legacy_mode():
    snapshot = ProcessingSnapshot(
        routing_generation=3,
        routing_table=None,
    )

    assert snapshot.routing_generation == 3
    assert snapshot.routing_table is None


def test_processing_snapshot_adapts_immutable_routing_snapshot():
    table = make_table()
    routing_snapshot = RoutingSnapshot(generation=11, table=table)

    snapshot = ProcessingSnapshot.from_routing_snapshot(routing_snapshot)

    assert snapshot == ProcessingSnapshot(
        routing_generation=11,
        routing_table=table,
    )
    assert snapshot.routing_table is table


@pytest.mark.parametrize(
    ("routing_generation", "routing_table", "exception"),
    [
        (True, None, TypeError),
        (1.0, None, TypeError),
        ("1", None, TypeError),
        (-1, None, ValueError),
        (0, object(), TypeError),
    ],
)
def test_processing_snapshot_rejects_invalid_values(
    routing_generation,
    routing_table,
    exception,
):
    with pytest.raises(exception):
        ProcessingSnapshot(
            routing_generation=routing_generation,
            routing_table=routing_table,
        )


def test_processing_snapshot_adapter_rejects_non_snapshot():
    with pytest.raises(TypeError, match="RoutingSnapshot"):
        ProcessingSnapshot.from_routing_snapshot(object())


@pytest.mark.parametrize(
    ("routing_snapshot", "exception"),
    [
        (RoutingSnapshot(generation=-1, table=None), ValueError),
        (RoutingSnapshot(generation=0, table=object()), TypeError),
    ],
)
def test_processing_snapshot_adapter_revalidates_snapshot_values(
    routing_snapshot,
    exception,
):
    with pytest.raises(exception):
        ProcessingSnapshot.from_routing_snapshot(routing_snapshot)


def test_processor_output_is_frozen_slotted_and_has_no_runtime_objects():
    output = ProcessorOutput(
        message="formatted message\r\n",
        disposition=RoutingDisposition.LEGACY_BROADCAST,
    )

    with pytest.raises(FrozenInstanceError):
        output.message = "changed\r\n"

    assert not hasattr(output, "__dict__")
    assert tuple(field.name for field in fields(output)) == (
        "message",
        "disposition",
        "target_ids",
    )


def test_targeted_processor_output_preserves_order_and_copies_mutable_input():
    target_ids = ["udp:second", "udp:first", "udp:second"]

    output = ProcessorOutput(
        message="formatted message\r\n",
        disposition=RoutingDisposition.TARGETED,
        target_ids=target_ids,
    )
    target_ids.append("udp:later")

    assert output.disposition is RoutingDisposition.TARGETED
    assert output.target_ids == (
        "udp:second",
        "udp:first",
        "udp:second",
    )
    assert isinstance(output.target_ids, tuple)


def test_legacy_processor_output_has_explicit_disposition_and_no_targets():
    output = ProcessorOutput(
        message="formatted message\r\n",
        disposition=RoutingDisposition.LEGACY_BROADCAST,
    )

    assert output.disposition is RoutingDisposition.LEGACY_BROADCAST
    assert output.target_ids == ()


@pytest.mark.parametrize(
    ("message", "disposition", "target_ids", "exception"),
    [
        (
            "message\r\n",
            RoutingDisposition.LEGACY_BROADCAST,
            ("udp:first",),
            ValueError,
        ),
        ("message\r\n", RoutingDisposition.TARGETED, (), ValueError),
        ("message\r\n", "targeted", ("udp:first",), TypeError),
        (b"message\r\n", RoutingDisposition.TARGETED, ("udp:first",), TypeError),
        ("message\r\n", RoutingDisposition.TARGETED, "udp:first", TypeError),
        ("message\r\n", RoutingDisposition.TARGETED, ("udp:first", object()), TypeError),
    ],
)
def test_processor_output_rejects_invalid_values(
    message,
    disposition,
    target_ids,
    exception,
):
    with pytest.raises(exception):
        ProcessorOutput(
            message=message,
            disposition=disposition,
            target_ids=target_ids,
        )


def test_minimal_synchronous_fake_satisfies_processor_protocol():
    output = ProcessorOutput(
        message="formatted message\r\n",
        disposition=RoutingDisposition.LEGACY_BROADCAST,
    )

    class FakeProcessor:
        def process(self, frame, snapshot):
            assert frame is expected_frame
            assert snapshot is expected_snapshot
            return (output,)

    expected_frame = make_frame()
    expected_snapshot = ProcessingSnapshot(
        routing_generation=0,
        routing_table=None,
    )
    processor: DataPlaneProcessor = FakeProcessor()

    assert isinstance(processor, DataPlaneProcessor)
    assert not inspect.iscoroutinefunction(processor.process)
    assert processor.process(expected_frame, expected_snapshot) == (output,)
