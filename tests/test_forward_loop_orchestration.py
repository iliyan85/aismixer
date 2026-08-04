import asyncio

import pytest

import aismixer
from core.data_plane import (
    DeduplicationMode,
    OutputBatch,
    ProcessorOutput,
)
from core.ingress_frame import IngressFrame
from core.python_data_plane import PythonDataPlaneProcessor
from core.routing_state import RoutingSnapshot
from core.state.s_cache import SourceState
from dedup import Deduplicator


FIRST_SENTENCE = "!AIVDM,1,1,,A,first-payload,0*00"
SECOND_SENTENCE = "!AIVDM,1,1,,B,second-payload,0*00"


def make_frame(payload=b"!AIVDM,1,1,,A,payload,0*00"):
    return IngressFrame(
        kind="udp",
        source_id="udp:source",
        alias_for_s=None,
        remote_ip="192.0.2.10",
        assembler_key="192.0.2.10:17778",
        payload=payload,
    )


def output(message, *target_ids):
    return ProcessorOutput(
        f"{message}\r\n".encode("utf-8"),
        target_ids,
    )


def output_batch(*outputs):
    return OutputBatch(outputs)


class FiniteQueue:
    def __init__(self, *items):
        self._items = list(items)

    async def get(self):
        if self._items:
            return self._items.pop(0)
        raise asyncio.CancelledError()


class RecordingRoutingState:
    def __init__(self, snapshot):
        self._snapshot = snapshot
        self.snapshot_calls = 0

    def snapshot(self):
        self.snapshot_calls += 1
        return self._snapshot


class ScriptedProcessor:
    def __init__(self, *batches):
        self._batches = list(batches)
        self.calls = []

    def process(self, frame, snapshot):
        self.calls.append((frame, snapshot))
        return (
            self._batches.pop(0)
            if self._batches
            else OutputBatch(outputs=())
        )


class CompletionRecordingProcessor:
    def __init__(self, delegate):
        self._delegate = delegate
        self.completed = False
        self.outputs = None

    def process(self, frame, snapshot):
        outputs = self._delegate.process(frame, snapshot)
        self.outputs = outputs
        self.completed = True
        return outputs


class RecordingSourceState(SourceState):
    def __init__(self, touched_s_values):
        super().__init__()
        self._touched_s_values = touched_s_values

    def touch_s(self, s_value):
        self._touched_s_values.append(s_value)
        super().touch_s(s_value)


class RecordingForwarder:
    def __init__(self, fail_at=None):
        self.events = []
        self._fail_at = fail_at

    async def send(self, _message):
        raise AssertionError("production egress called compatibility send()")

    async def send_to_ids(self, target_ids, message):
        self.events.append(("numeric", tuple(target_ids), message))
        self._raise_if_requested()

    async def send_to(self, _target_ids, _message):
        raise AssertionError("production egress called compatibility send_to()")

    def _raise_if_requested(self):
        if self._fail_at == len(self.events):
            raise RuntimeError("send failed")


class BoundaryObservingForwarder:
    def __init__(
        self,
        processor,
        deduplicator,
        touched_s_values,
        clock_observations,
        gid_observations,
        *,
        fail_first=False,
    ):
        self._processor = processor
        self._deduplicator = deduplicator
        self._touched_s_values = touched_s_values
        self._clock_observations = clock_observations
        self._gid_observations = gid_observations
        self._fail_first = fail_first
        self.observations = []
        self.attempted_messages = []

    async def send(self, _message):
        raise AssertionError("production egress called compatibility send()")

    async def send_to_ids(self, target_ids, message):
        self.observations.append(
            {
                "processor_completed": self._processor.completed,
                "outputs": self._processor.outputs,
                "target_ids": tuple(target_ids),
                "dedup_stats": self._deduplicator.stats(),
                "touched_s_values": tuple(self._touched_s_values),
                "clock_observations": tuple(self._clock_observations),
                "gid_observations": tuple(self._gid_observations),
            }
        )
        self.attempted_messages.append(message)
        if self._fail_first and len(self.attempted_messages) == 1:
            raise RuntimeError("send failed")

    async def send_to(self, _target_ids, _message):
        raise AssertionError("production egress called compatibility send_to()")


async def run_runtime_stages(
    ingress_queue,
    processor,
    output_forwarder,
    *,
    routing_state=None,
):
    await aismixer._run_runtime_stages(
        ingress_queue,
        asyncio.Queue(),
        routing_state=routing_state,
        processor=processor,
        legacy_target_ids=(),
        output_forwarder=output_forwarder,
        debug=False,
    )


def make_boundary_processor():
    touched_s_values = []
    clock_observations = []
    clock_values = iter((1001.9, 1002.9))
    gid_observations = []
    gid_values = iter(("111111", "222222"))
    deduplicator = Deduplicator(clock=lambda: 0.0)
    source_state = RecordingSourceState(touched_s_values)

    def wall_clock():
        value = next(clock_values)
        clock_observations.append(value)
        return value

    def generate_gid(digits):
        value = next(gid_values)
        gid_observations.append((digits, value))
        return value

    processor = CompletionRecordingProcessor(
        PythonDataPlaneProcessor(
            station_id="boundary",
            always_tag_single=True,
            gid_digits=6,
            deduplicator=deduplicator,
            wall_clock=wall_clock,
            gid_generator=generate_gid,
            source_state=source_state,
        )
    )
    return (
        processor,
        deduplicator,
        touched_s_values,
        clock_observations,
        gid_observations,
    )


def test_invalid_item_is_ignored_before_snapshot_and_processing_then_loop_continues():
    frame = make_frame()
    state = RecordingRoutingState(
        RoutingSnapshot(generation=7, table=None)
    )
    processor = ScriptedProcessor(output_batch())
    output_forwarder = RecordingForwarder()

    with pytest.raises(
        RuntimeError,
        match="processor-stage.*cancelled unexpectedly",
    ):
        asyncio.run(
            run_runtime_stages(
                FiniteQueue(object(), frame),
                processor=processor,
                output_forwarder=output_forwarder,
                routing_state=state,
            )
        )

    assert state.snapshot_calls == 1
    assert len(processor.calls) == 1
    processed_frame, snapshot = processor.calls[0]
    assert processed_frame is frame
    assert snapshot.routing_generation == 7
    assert snapshot.deduplication_mode is DeduplicationMode.GLOBAL
    assert snapshot.target_ids == ()


def test_processor_is_called_once_and_outputs_are_dispatched_sequentially():
    frame = make_frame()
    state = RecordingRoutingState(
        RoutingSnapshot(generation=3, table=None)
    )
    processor = ScriptedProcessor(
        output_batch(
            output("first", 0),
            output("second", 2, 1),
        )
    )
    forwarder = RecordingForwarder()

    with pytest.raises(
        RuntimeError,
        match="processor-stage.*cancelled unexpectedly",
    ):
        asyncio.run(
            run_runtime_stages(
                FiniteQueue(frame),
                processor=processor,
                output_forwarder=forwarder,
                routing_state=state,
            )
        )

    assert state.snapshot_calls == 1
    assert len(processor.calls) == 1
    assert forwarder.events == [
        ("numeric", (0,), b"first\r\n"),
        (
            "numeric",
            (2, 1),
            b"second\r\n",
        ),
    ]


def test_dispatch_failure_stops_before_later_output_dispatch():
    processor = ScriptedProcessor(
        output_batch(
            output("first", 0),
            output("second", 0),
        )
    )
    forwarder = RecordingForwarder(fail_at=1)

    with pytest.raises(RuntimeError, match="send failed"):
        asyncio.run(
            run_runtime_stages(
                FiniteQueue(make_frame()),
                processor=processor,
                output_forwarder=forwarder,
            )
        )

    assert len(processor.calls) == 1
    assert forwarder.events == [("numeric", (0,), b"first\r\n")]


def test_whole_frame_processing_and_effects_complete_before_ordered_egress():
    (
        processor,
        deduplicator,
        touched_s_values,
        clock_observations,
        gid_observations,
    ) = make_boundary_processor()
    forwarder = BoundaryObservingForwarder(
        processor,
        deduplicator,
        touched_s_values,
        clock_observations,
        gid_observations,
    )
    frame = make_frame(
        (FIRST_SENTENCE + "\n" + SECOND_SENTENCE).encode("ascii")
    )

    with pytest.raises(
        RuntimeError,
        match="processor-stage.*cancelled unexpectedly",
    ):
        asyncio.run(
            run_runtime_stages(
                FiniteQueue(frame),
                processor=processor,
                output_forwarder=forwarder,
            )
        )

    output_batch_result = processor.outputs
    assert processor.completed is True
    assert isinstance(output_batch_result, OutputBatch)
    assert len(output_batch_result.outputs) == 2
    assert output_batch_result.outputs[0].message.endswith(
        (FIRST_SENTENCE + "\r\n").encode("ascii")
    )
    assert output_batch_result.outputs[1].message.endswith(
        (SECOND_SENTENCE + "\r\n").encode("ascii")
    )

    first_send = forwarder.observations[0]
    assert first_send == {
        "processor_completed": True,
        "outputs": output_batch_result,
        "target_ids": (),
        "dedup_stats": deduplicator.stats(),
        "touched_s_values": ("boundary", "boundary"),
        "clock_observations": (1001.9, 1002.9),
        "gid_observations": (
            (6, "111111"),
            (6, "222222"),
        ),
    }
    assert first_send["dedup_stats"].accepted == 2
    assert forwarder.attempted_messages == [
        output.message for output in output_batch_result.outputs
    ]


def test_first_send_failure_keeps_completed_effects_and_later_output():
    (
        processor,
        deduplicator,
        touched_s_values,
        clock_observations,
        gid_observations,
    ) = make_boundary_processor()
    forwarder = BoundaryObservingForwarder(
        processor,
        deduplicator,
        touched_s_values,
        clock_observations,
        gid_observations,
        fail_first=True,
    )
    frame = make_frame(
        (FIRST_SENTENCE + "\n" + SECOND_SENTENCE).encode("ascii")
    )

    with pytest.raises(RuntimeError, match="send failed"):
        asyncio.run(
            run_runtime_stages(
                FiniteQueue(frame),
                processor=processor,
                output_forwarder=forwarder,
            )
        )

    output_batch_result = processor.outputs
    assert processor.completed is True
    assert isinstance(output_batch_result, OutputBatch)
    assert len(output_batch_result.outputs) == 2
    assert output_batch_result.outputs[0].message.endswith(
        (FIRST_SENTENCE + "\r\n").encode("ascii")
    )
    assert output_batch_result.outputs[1].message.endswith(
        (SECOND_SENTENCE + "\r\n").encode("ascii")
    )
    assert forwarder.attempted_messages == [
        output_batch_result.outputs[0].message
    ]

    assert touched_s_values == ["boundary", "boundary"]
    assert clock_observations == [1001.9, 1002.9]
    assert gid_observations == [
        (6, "111111"),
        (6, "222222"),
    ]
    assert deduplicator.stats().accepted == 2
    assert (
        forwarder.observations[0]["outputs"]
        is output_batch_result
    )
    assert forwarder.observations[0]["target_ids"] == ()
