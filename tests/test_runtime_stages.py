import asyncio
import inspect

import pytest

import aismixer
from core.data_plane import (
    DeduplicationMode,
    OutputBatch,
    ProcessingSnapshot,
    ProcessingWorkItem,
    ProcessorOutput,
)
from core.ingress_frame import IngressFrame
from core.python_data_plane import PythonDataPlaneProcessor
from core.routing import RoutingTable
from core.routing_state import RoutingSnapshot, RoutingState


def make_frame(label):
    return IngressFrame(
        kind="udp",
        source_id=f"udp:{label}",
        alias_for_s=None,
        remote_ip="192.0.2.10",
        assembler_key=f"192.0.2.10:{label}",
        payload=f"!AIVDM,1,1,,A,{label},0*00".encode("ascii"),
    )


def make_work_item(
    frame,
    *,
    generation=0,
    mode=DeduplicationMode.GLOBAL,
    target_ids=(),
):
    return ProcessingWorkItem(
        frame=frame,
        snapshot=ProcessingSnapshot(
            routing_generation=generation,
            deduplication_mode=mode,
            target_ids=target_ids,
        ),
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


class CompletingEgressQueue:
    def __init__(self):
        self.batches = []

    async def put(self, batch):
        self.batches.append(batch)
        batch.completion.set_result(None)


class RecordingEgressQueue(asyncio.Queue):
    def __init__(self):
        super().__init__()
        self.batches = []

    async def put(self, batch):
        self.batches.append(batch)
        await super().put(batch)


class RecordingGetQueue(asyncio.Queue):
    def __init__(self):
        super().__init__()
        self.get_started = asyncio.Event()
        self.getter_task = None

    async def get(self):
        self.getter_task = asyncio.current_task()
        self.get_started.set()
        return await super().get()


class RecordingRoutingState:
    def __init__(self, snapshot):
        self._snapshot = snapshot
        self.snapshot_calls = 0

    def snapshot(self):
        self.snapshot_calls += 1
        return self._snapshot


class WaitableRoutingState:
    def __init__(self, table):
        self._state = RoutingState(table)
        self.snapshot_calls = 0
        self._snapshot_events = []

    def add_snapshot_events(self, *events):
        self._snapshot_events.extend(events)

    def snapshot(self):
        snapshot = self._state.snapshot()
        call_index = self.snapshot_calls
        self.snapshot_calls += 1
        if call_index < len(self._snapshot_events):
            self._snapshot_events[call_index].set()
        return snapshot

    def replace(self, table):
        return self._state.replace(table)


class RecordingNumericRoutingTable:
    def __init__(self, target_ids):
        self.target_ids = tuple(target_ids)
        self.source_ids = []

    def match_target_ids(self, source_id):
        self.source_ids.append(source_id)
        return self.target_ids

    def match(self, _source_id):
        raise AssertionError("descriptive routing matcher was called")


class ScriptedProcessor:
    def __init__(self, *actions, effects=None):
        self._actions = list(actions)
        self._effects = effects
        self.calls = []
        self.call_events = []
        self.task = None

    def add_call_events(self, *events):
        self.call_events.extend(events)

    def process(self, frame, snapshot):
        self.task = asyncio.current_task()
        call_index = len(self.calls)
        self.calls.append((frame, snapshot))
        if call_index < len(self.call_events):
            self.call_events[call_index].set()
        if self._effects is not None:
            self._effects(call_index)

        action = self._actions[call_index]
        if isinstance(action, BaseException):
            raise action
        return action


class RecordingForwarder:
    def __init__(self):
        self.events = []

    async def send(self, _message):
        raise AssertionError("production egress called compatibility send()")

    async def send_to_ids(self, target_ids, message):
        self.events.append(("numeric", tuple(target_ids), message))

    async def send_to(self, _target_ids, _message):
        raise AssertionError("production egress called compatibility send_to()")


class GatedForwarder(RecordingForwarder):
    def __init__(self):
        super().__init__()
        self.first_send_started = asyncio.Event()
        self.release_first_send = asyncio.Event()
        self.second_send_started = asyncio.Event()
        self.task = None

    async def send_to_ids(self, target_ids, message):
        self.task = asyncio.current_task()
        self.events.append(("numeric", tuple(target_ids), message))
        if len(self.events) == 1:
            self.first_send_started.set()
            await self.release_first_send.wait()
        elif len(self.events) == 2:
            self.second_send_started.set()


class FirstSendFailingForwarder(RecordingForwarder):
    async def send_to_ids(self, target_ids, message):
        self.events.append(("numeric", tuple(target_ids), message))
        raise RuntimeError("send failed")


async def cancel_task(task):
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_runtime_module_has_no_import_time_processor_instance():
    assert "data_plane_processor" not in vars(aismixer)
    assert not any(
        isinstance(value, PythonDataPlaneProcessor)
        for value in vars(aismixer).values()
    )


def test_processor_factory_uses_current_runtime_configuration(monkeypatch):
    processor = object()
    constructor_calls = []

    def fake_processor_constructor(**kwargs):
        constructor_calls.append(kwargs)
        return processor

    monkeypatch.setattr(
        aismixer,
        "PythonDataPlaneProcessor",
        fake_processor_constructor,
    )
    monkeypatch.setattr(aismixer, "STATION_ID", "runtime-station")
    monkeypatch.setattr(aismixer, "C_PRESERVE_INGRESS_C", False)
    monkeypatch.setattr(aismixer, "G_PRESERVE_INGRESS_GID", False)
    monkeypatch.setattr(aismixer, "G_ALWAYS_TAG_SINGLE", True)
    monkeypatch.setattr(aismixer, "G_ID_DIGITS", 6)

    assert aismixer.create_data_plane_processor() is processor
    assert constructor_calls == [
        {
            "station_id": "runtime-station",
            "preserve_ingress_c": False,
            "preserve_ingress_gid": False,
            "always_tag_single": True,
            "gid_digits": 6,
        }
    ]


@pytest.mark.parametrize(
    ("operation", "required_arguments"),
    [
        (
            aismixer.ingress_fan_in_loop,
            {},
        ),
        (
            aismixer._run_runtime_stages,
            {
                "processor": object(),
                "output_forwarder": object(),
            },
        ),
    ],
)
def test_runtime_orchestration_requires_explicit_legacy_target_ids(
    operation,
    required_arguments,
):
    operation_signature = inspect.signature(operation)
    parameter = operation_signature.parameters["legacy_target_ids"]

    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty
    with pytest.raises(TypeError, match="legacy_target_ids"):
        operation(
            object(),
            object(),
            **required_arguments,
        )


@pytest.mark.parametrize(
    ("operation", "required_arguments"),
    [
        (
            aismixer.processor_stage_loop,
            {},
        ),
        (
            aismixer._run_runtime_stages,
            {
                "legacy_target_ids": (),
                "output_forwarder": object(),
            },
        ),
    ],
)
def test_runtime_orchestration_requires_explicit_processor(
    operation,
    required_arguments,
):
    operation_signature = inspect.signature(operation)
    parameter = operation_signature.parameters["processor"]

    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty
    with pytest.raises(TypeError, match="processor"):
        operation(
            object(),
            object(),
            **required_arguments,
        )


def test_processor_stage_signature_has_no_routing_dependencies():
    signature = inspect.signature(aismixer.processor_stage_loop)

    assert tuple(signature.parameters) == (
        "processing_queue",
        "egress_queue",
        "processor",
    )
    assert signature.parameters["processor"].kind is (
        inspect.Parameter.KEYWORD_ONLY
    )
    source = inspect.getsource(aismixer.processor_stage_loop)
    for forbidden_operation in (
        "coerce_ingress_frame",
        "legacy_target_ids",
        "match_target_ids",
        "routing_state",
        ".snapshot()",
    ):
        assert forbidden_operation not in source


def test_global_mode_accepts_explicit_empty_target_ids():
    frame = make_frame("empty-global-registry")

    work_item = aismixer._bind_processing_work_item(
        frame,
        legacy_target_ids=(),
    )

    assert isinstance(work_item, ProcessingWorkItem)
    assert work_item.frame is frame
    assert work_item.snapshot.routing_generation == 0
    assert (
        work_item.snapshot.deduplication_mode
        is DeduplicationMode.GLOBAL
    )
    assert work_item.snapshot.target_ids == ()


def test_fan_in_normalizes_legacy_target_ids_once_for_repeated_use():
    async def scenario():
        class SinglePassTargetIds:
            def __init__(self):
                self.iterations = 0

            def __iter__(self):
                self.iterations += 1
                if self.iterations > 1:
                    raise AssertionError("legacy target IDs were re-read")
                return iter((5, 2))

        legacy_target_ids = SinglePassTargetIds()
        ingress_queue = asyncio.Queue()
        processing_queue = aismixer._BoundedProcessingQueue(2)
        task = asyncio.create_task(
            aismixer.ingress_fan_in_loop(
                (ingress_queue,),
                processing_queue,
                legacy_target_ids=legacy_target_ids,
            )
        )
        try:
            await ingress_queue.put(make_frame("first-legacy"))
            await ingress_queue.put(make_frame("second-legacy"))
            work_items = (
                await asyncio.wait_for(processing_queue.get(), timeout=1.0),
                await asyncio.wait_for(processing_queue.get(), timeout=1.0),
            )
        finally:
            await cancel_task(task)

        assert legacy_target_ids.iterations == 1
        assert [item.snapshot.target_ids for item in work_items] == [
            (5, 2),
            (5, 2),
        ]

    asyncio.run(scenario())


def test_non_empty_global_target_ids_reach_numeric_egress_unchanged():
    async def scenario():
        payload = b"global\r\n"

        class SnapshotTargetProcessor:
            def process(self, _frame, snapshot):
                return OutputBatch(
                    (
                        ProcessorOutput(
                            payload,
                            snapshot.target_ids,
                        ),
                    )
                )

        class SignallingForwarder(RecordingForwarder):
            def __init__(self):
                super().__init__()
                self.sent = asyncio.Event()

            async def send_to_ids(self, target_ids, message):
                await super().send_to_ids(target_ids, message)
                self.sent.set()

        ingress_queue = asyncio.Queue()
        await ingress_queue.put(make_frame("global-targets"))
        forwarder = SignallingForwarder()
        task = asyncio.create_task(
            aismixer._run_runtime_stages(
                ingress_queue,
                asyncio.Queue(maxsize=1),
                processor=SnapshotTargetProcessor(),
                legacy_target_ids=(7, 2),
                output_forwarder=forwarder,
            )
        )
        try:
            await asyncio.wait_for(forwarder.sent.wait(), timeout=1.0)
        finally:
            await cancel_task(task)

        assert forwarder.events == [
            ("numeric", (7, 2), payload),
        ]
        assert forwarder.events[0][2] is payload

    asyncio.run(scenario())


def routing_table_for_sources(target_name, target_id, *source_ids):
    return RoutingTable.from_definitions(
        {"sources": {"include": list(source_ids)}},
        [
            {
                "name": f"sources_to_{target_name}",
                "from_zone": "sources",
                "to": [target_name],
            }
        ],
    ).compile_target_ids({target_name: target_id})


def test_supported_item_is_coerced_before_routing_snapshot(monkeypatch):
    async def scenario():
        raw_item = object()
        frame = make_frame("coerced")
        events = []

        def coerce(item):
            assert item is raw_item
            events.append("coerce")
            return frame

        class OrderedRoutingState:
            def snapshot(self):
                events.append("snapshot")
                return RoutingSnapshot(6, None)

        monkeypatch.setattr(aismixer, "coerce_ingress_frame", coerce)
        ingress_queue = asyncio.Queue()
        processing_queue = aismixer._BoundedProcessingQueue(1)
        task = asyncio.create_task(
            aismixer.ingress_fan_in_loop(
                (ingress_queue,),
                processing_queue,
                routing_state=OrderedRoutingState(),
                legacy_target_ids=(4,),
            )
        )
        try:
            await ingress_queue.put(raw_item)
            work_item = await asyncio.wait_for(
                processing_queue.get(),
                timeout=1.0,
            )
        finally:
            await cancel_task(task)

        assert events == ["coerce", "snapshot"]
        assert work_item.frame is frame
        assert work_item.snapshot == ProcessingSnapshot(
            routing_generation=6,
            deduplication_mode=DeduplicationMode.GLOBAL,
            target_ids=(4,),
        )

    asyncio.run(scenario())


def test_unsupported_item_is_rejected_before_routing_snapshot():
    async def scenario():
        state = RecordingRoutingState(RoutingSnapshot(7, None))
        frame = make_frame("supported-after-invalid")
        ingress_queue = asyncio.Queue()
        processing_queue = aismixer._BoundedProcessingQueue(1)
        task = asyncio.create_task(
            aismixer.ingress_fan_in_loop(
                (ingress_queue,),
                processing_queue,
                routing_state=state,
                legacy_target_ids=(),
            )
        )
        try:
            await ingress_queue.put(object())
            await ingress_queue.put(frame)
            work_item = await asyncio.wait_for(
                processing_queue.get(),
                timeout=1.0,
            )
        finally:
            await cancel_task(task)

        assert work_item.frame is frame
        assert state.snapshot_calls == 1

    asyncio.run(scenario())


def test_routed_frame_resolves_one_numeric_target_only_snapshot():
    async def scenario():
        frame = make_frame("routed")
        table = RecordingNumericRoutingTable((3, 1))
        state = RecordingRoutingState(RoutingSnapshot(8, table))
        input_queue = asyncio.Queue()
        output_queue = aismixer._BoundedProcessingQueue(1)
        task = asyncio.create_task(
            aismixer.ingress_fan_in_loop(
                (input_queue,),
                output_queue,
                routing_state=state,
                legacy_target_ids=(),
            )
        )
        await input_queue.put(frame)
        try:
            work_item = await asyncio.wait_for(
                output_queue.get(),
                timeout=1.0,
            )
        finally:
            await cancel_task(task)

        assert state.snapshot_calls == 1
        assert table.source_ids == ["udp:routed"]
        assert isinstance(work_item, ProcessingWorkItem)
        assert work_item.frame is frame
        snapshot = work_item.snapshot
        assert snapshot.routing_generation == 8
        assert snapshot.deduplication_mode is DeduplicationMode.PER_TARGET
        assert snapshot.target_ids == (3, 1)

    asyncio.run(scenario())


def test_routed_no_match_remains_per_target_with_empty_targets():
    frame = make_frame("unmatched")
    table = RecordingNumericRoutingTable(())

    work_item = aismixer._bind_processing_work_item(
        frame,
        routing_state=RecordingRoutingState(RoutingSnapshot(4, table)),
        legacy_target_ids=(9,),
    )

    assert work_item.snapshot.routing_generation == 4
    assert (
        work_item.snapshot.deduplication_mode
        is DeduplicationMode.PER_TARGET
    )
    assert work_item.snapshot.target_ids == ()


def test_legacy_mode_receives_explicit_all_numeric_forwarder_ids():
    frame = make_frame("legacy")

    work_item = aismixer._bind_processing_work_item(
        frame,
        routing_state=RecordingRoutingState(RoutingSnapshot(5, None)),
        legacy_target_ids=[0, 2, 3],
    )

    assert work_item.snapshot.routing_generation == 5
    assert work_item.snapshot.deduplication_mode is DeduplicationMode.GLOBAL
    assert work_item.snapshot.target_ids == (0, 2, 3)


def test_processor_stage_rejects_raw_internal_queue_items():
    async def scenario():
        processor = ScriptedProcessor(output_batch())

        with pytest.raises(
            TypeError,
            match="processor queue item must be a ProcessingWorkItem",
        ):
            await aismixer.processor_stage_loop(
                FiniteQueue(make_frame("raw")),
                CompletingEgressQueue(),
                processor=processor,
            )

        assert processor.calls == []

    asyncio.run(scenario())


def test_one_frame_produces_one_complete_ordered_egress_batch():
    async def scenario():
        frame = make_frame("one")
        work_item = make_work_item(
            frame,
            generation=3,
            target_ids=(4,),
        )
        processor_batch = output_batch(
            output("first", 0),
            output("second", 2, 1),
        )
        processor = ScriptedProcessor(processor_batch)
        egress_queue = CompletingEgressQueue()

        with pytest.raises(asyncio.CancelledError):
            await aismixer.processor_stage_loop(
                FiniteQueue(work_item),
                egress_queue,
                processor=processor,
            )

        assert len(processor.calls) == 1
        assert processor.calls[0][0] is work_item.frame
        assert processor.calls[0][1] is work_item.snapshot
        assert len(egress_queue.batches) == 1
        envelope = egress_queue.batches[0]
        assert envelope.output_batch is processor_batch
        assert envelope.output_batch.outputs == processor_batch.outputs
        assert aismixer._EgressBatch.__slots__ == (
            "output_batch",
            "completion",
        )

    asyncio.run(scenario())


def test_egress_dispatches_batch_sequentially_in_tuple_order():
    async def scenario():
        processor_batch = output_batch(
            output("first", 0),
            output("second", 2, 1),
        )
        completion = asyncio.get_running_loop().create_future()
        queue = asyncio.Queue()
        await queue.put(aismixer._EgressBatch(processor_batch, completion))
        forwarder = GatedForwarder()
        task = asyncio.create_task(
            aismixer.egress_stage_loop(queue, forwarder)
        )
        try:
            await forwarder.first_send_started.wait()
            assert forwarder.events == [
                ("numeric", (0,), b"first\r\n")
            ]
            assert not completion.done()

            forwarder.release_first_send.set()
            await completion
        finally:
            await cancel_task(task)

        assert forwarder.events == [
            ("numeric", (0,), b"first\r\n"),
            ("numeric", (2, 1), b"second\r\n"),
        ]

    asyncio.run(scenario())


def test_processor_does_not_run_ahead_while_first_batch_is_unacknowledged():
    async def scenario():
        first_frame = make_frame("first")
        second_frame = make_frame("second")
        processor = ScriptedProcessor(
            output_batch(output("first", 0)),
            output_batch(output("second", 0)),
        )
        ingress_queue = asyncio.Queue()
        await ingress_queue.put(first_frame)
        await ingress_queue.put(second_frame)
        egress_queue = asyncio.Queue(maxsize=1)
        forwarder = GatedForwarder()
        task = asyncio.create_task(
            aismixer._run_runtime_stages(
                ingress_queue,
                egress_queue,
                processor=processor,
                legacy_target_ids=(),
                output_forwarder=forwarder,
            )
        )
        try:
            await forwarder.first_send_started.wait()
            assert [call[0] for call in processor.calls] == [first_frame]
            # Snapshot binding is allowed to consume raw ingress ahead of the
            # processor's egress-completion barrier.
            assert ingress_queue.qsize() == 0
        finally:
            await cancel_task(task)

    asyncio.run(scenario())


def test_successful_acknowledgement_allows_the_next_frame():
    async def scenario():
        first_frame = make_frame("first")
        second_frame = make_frame("second")
        second_call = asyncio.Event()
        processor = ScriptedProcessor(
            output_batch(output("first", 0)),
            output_batch(output("second", 0)),
        )
        processor.add_call_events(asyncio.Event(), second_call)
        ingress_queue = asyncio.Queue()
        await ingress_queue.put(first_frame)
        await ingress_queue.put(second_frame)
        forwarder = GatedForwarder()
        task = asyncio.create_task(
            aismixer._run_runtime_stages(
                ingress_queue,
                asyncio.Queue(maxsize=1),
                processor=processor,
                legacy_target_ids=(),
                output_forwarder=forwarder,
            )
        )
        try:
            await forwarder.first_send_started.wait()
            assert not second_call.is_set()

            forwarder.release_first_send.set()
            await second_call.wait()
            await forwarder.second_send_started.wait()
        finally:
            await cancel_task(task)

        assert [call[0] for call in processor.calls] == [
            first_frame,
            second_frame,
        ]
        assert forwarder.events == [
            ("numeric", (0,), b"first\r\n"),
            ("numeric", (0,), b"second\r\n"),
        ]

    asyncio.run(scenario())


def test_bounded_congestion_propagates_to_ingress_and_recovers_in_order(
    monkeypatch,
):
    async def scenario():
        frames = tuple(
            make_frame(label)
            for label in ("first", "second", "third", "fourth", "fifth")
        )
        first_frame, second_frame, third_frame, fourth_frame, fifth_frame = (
            frames
        )
        state = WaitableRoutingState(None)
        first_bound = asyncio.Event()
        second_bound = asyncio.Event()
        third_bound = asyncio.Event()
        state.add_snapshot_events(first_bound, second_bound, third_bound)
        processor = ScriptedProcessor(
            *(output_batch(output(frame.source_id, 0)) for frame in frames)
        )

        class CongestionForwarder(RecordingForwarder):
            def __init__(self):
                super().__init__()
                self.first_send_started = asyncio.Event()
                self.release_first_send = asyncio.Event()
                self.all_sent = asyncio.Event()

            async def send_to_ids(self, target_ids, message):
                await super().send_to_ids(target_ids, message)
                if len(self.events) == 1:
                    self.first_send_started.set()
                    await self.release_first_send.wait()
                if len(self.events) == len(frames):
                    self.all_sent.set()

        third_removed = asyncio.Event()

        class ObservedIngressQueue(asyncio.Queue):
            async def get(self):
                item = await super().get()
                if item is third_frame:
                    third_removed.set()
                return item

        processing_queues = []
        processing_queue_type = aismixer._BoundedProcessingQueue

        def recording_processing_queue(maxsize):
            queue = processing_queue_type(maxsize)
            processing_queues.append(queue)
            return queue

        monkeypatch.setattr(
            aismixer,
            "_BoundedProcessingQueue",
            recording_processing_queue,
        )
        ingress_queue = ObservedIngressQueue(maxsize=1)
        egress_queue = asyncio.Queue(maxsize=1)
        forwarder = CongestionForwarder()
        await ingress_queue.put(first_frame)
        runtime_task = asyncio.create_task(
            aismixer._run_runtime_stages(
                ingress_queue,
                egress_queue,
                routing_state=state,
                processor=processor,
                legacy_target_ids=(),
                output_forwarder=forwarder,
                processing_queue_maxsize=1,
            )
        )
        fifth_put_attempted = asyncio.Event()
        fifth_put_completed = asyncio.Event()
        fifth_put_task = None

        async def put_fifth_frame():
            fifth_put_attempted.set()
            await ingress_queue.put(fifth_frame)
            fifth_put_completed.set()

        try:
            await asyncio.wait_for(
                forwarder.first_send_started.wait(),
                timeout=1.0,
            )
            await ingress_queue.put(second_frame)
            await asyncio.wait_for(second_bound.wait(), timeout=1.0)

            await ingress_queue.put(third_frame)
            await asyncio.wait_for(third_removed.wait(), timeout=1.0)
            await ingress_queue.put(fourth_frame)
            fifth_put_task = asyncio.create_task(put_fifth_frame())
            await asyncio.wait_for(fifth_put_attempted.wait(), timeout=1.0)

            assert first_bound.is_set()
            assert not third_bound.is_set()
            assert state.snapshot_calls == 2
            assert [call[0] for call in processor.calls] == [first_frame]
            assert len(processing_queues) == 1
            processing_queue = processing_queues[0]
            assert processing_queue.maxsize == 1
            assert processing_queue.qsize() == 1
            assert ingress_queue.maxsize == 1
            assert ingress_queue.qsize() == 1
            assert ingress_queue.full()
            assert not fifth_put_completed.is_set()
            assert not fifth_put_task.done()

            forwarder.release_first_send.set()
            await asyncio.wait_for(forwarder.all_sent.wait(), timeout=1.0)
            await asyncio.wait_for(fifth_put_completed.wait(), timeout=1.0)
        finally:
            if fifth_put_task is not None and not fifth_put_task.done():
                fifth_put_task.cancel()
            if fifth_put_task is not None:
                await asyncio.gather(fifth_put_task, return_exceptions=True)
            await cancel_task(runtime_task)

        assert [call[0] for call in processor.calls] == list(frames)
        assert [event[2] for event in forwarder.events] == [
            f"udp:{label}\r\n".encode("ascii")
            for label in ("first", "second", "third", "fourth", "fifth")
        ]
        assert state.snapshot_calls == len(frames)
        assert third_bound.is_set()
        assert processing_queue.maxsize == 1
        assert processing_queue.qsize() == 0
        assert ingress_queue.maxsize == 1
        assert ingress_queue.qsize() == 0

    asyncio.run(scenario())


def test_first_send_failure_preserves_effects_and_prevents_later_work():
    async def scenario():
        first_frame = make_frame("first")
        second_frame = make_frame("second")
        effects = []
        processor_batch = output_batch(
            output("first", 0),
            output("later", 0),
        )

        def record_effects(call_index):
            if call_index == 0:
                effects.extend(("constructed:first", "constructed:later"))
            else:
                effects.append("processed:second-frame")

        processor = ScriptedProcessor(
            processor_batch,
            output_batch(output("second-frame", 0)),
            effects=record_effects,
        )
        ingress_queue = asyncio.Queue()
        await ingress_queue.put(first_frame)
        await ingress_queue.put(second_frame)
        forwarder = FirstSendFailingForwarder()

        with pytest.raises(RuntimeError, match="send failed"):
            await aismixer._run_runtime_stages(
                ingress_queue,
                asyncio.Queue(maxsize=1),
                processor=processor,
                legacy_target_ids=(),
                output_forwarder=forwarder,
            )

        assert [call[0] for call in processor.calls] == [first_frame]
        assert effects == ["constructed:first", "constructed:later"]
        assert processor_batch.outputs[1].message == b"later\r\n"
        assert forwarder.events == [
            ("numeric", (0,), b"first\r\n")
        ]
        assert ingress_queue.qsize() == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("disable_routing", [False, True])
def test_routing_change_affects_only_later_handoffs_while_blocked(
    disable_routing,
    monkeypatch,
):
    async def scenario():
        first_frame = make_frame("first")
        second_frame = make_frame("second")
        third_frame = make_frame("third")
        source_ids = tuple(
            frame.source_id
            for frame in (first_frame, second_frame, third_frame)
        )
        first_table = routing_table_for_sources(
            "udp:first-target",
            3,
            *source_ids,
        )
        second_table = (
            None
            if disable_routing
            else routing_table_for_sources(
                "udp:second-target",
                7,
                *source_ids,
            )
        )
        state = WaitableRoutingState(first_table)
        first_bound = asyncio.Event()
        second_bound = asyncio.Event()
        third_bound = asyncio.Event()
        state.add_snapshot_events(first_bound, second_bound, third_bound)
        match_calls = []
        original_match_target_ids = RoutingTable.match_target_ids

        def record_match(table, source_id):
            match_calls.append((table, source_id))
            return original_match_target_ids(table, source_id)

        monkeypatch.setattr(
            RoutingTable,
            "match_target_ids",
            record_match,
        )
        second_call = asyncio.Event()
        third_call = asyncio.Event()
        processor = ScriptedProcessor(
            output_batch(output("first", 0)),
            output_batch(output("second", 0)),
            output_batch(output("third", 0)),
        )
        processor.add_call_events(
            asyncio.Event(),
            second_call,
            third_call,
        )
        third_removed = asyncio.Event()

        class ObservedIngressQueue(asyncio.Queue):
            async def get(self):
                item = await super().get()
                if item is third_frame:
                    third_removed.set()
                return item

        ingress_queue = ObservedIngressQueue()
        await ingress_queue.put(first_frame)
        await ingress_queue.put(second_frame)
        forwarder = GatedForwarder()
        task = asyncio.create_task(
            aismixer._run_runtime_stages(
                ingress_queue,
                asyncio.Queue(maxsize=1),
                routing_state=state,
                processor=processor,
                legacy_target_ids=(9, 4),
                output_forwarder=forwarder,
                processing_queue_maxsize=1,
            )
        )
        try:
            await forwarder.first_send_started.wait()
            await asyncio.wait_for(second_bound.wait(), timeout=1.0)
            assert first_bound.is_set()
            assert len(processor.calls) == 1
            assert ingress_queue.qsize() == 0
            assert state.snapshot_calls == 2
            assert [source_id for _, source_id in match_calls] == [
                first_frame.source_id,
                second_frame.source_id,
            ]

            await ingress_queue.put(third_frame)
            await asyncio.wait_for(third_removed.wait(), timeout=1.0)

            # The reader owns the accepted third frame, but the only queued
            # processing slot still belongs to the second frame. Routing is
            # deliberately unbound until that slot is released on dequeue.
            assert not third_bound.is_set()
            assert state.snapshot_calls == 2
            assert [source_id for _, source_id in match_calls] == [
                first_frame.source_id,
                second_frame.source_id,
            ]
            assert len(processor.calls) == 1

            state.replace(second_table)
            forwarder.release_first_send.set()
            await asyncio.wait_for(second_call.wait(), timeout=1.0)
            await asyncio.wait_for(third_bound.wait(), timeout=1.0)
            await asyncio.wait_for(third_call.wait(), timeout=1.0)
        finally:
            await cancel_task(task)

        first_snapshot = processor.calls[0][1]
        second_snapshot = processor.calls[1][1]
        third_snapshot = processor.calls[2][1]
        assert first_snapshot.routing_generation == 0
        assert first_snapshot.deduplication_mode is DeduplicationMode.PER_TARGET
        assert first_snapshot.target_ids == (3,)
        assert second_snapshot.routing_generation == 0
        assert second_snapshot.deduplication_mode is DeduplicationMode.PER_TARGET
        assert second_snapshot.target_ids == (3,)
        assert third_snapshot.routing_generation == 1
        assert third_snapshot.deduplication_mode is (
            DeduplicationMode.GLOBAL
            if disable_routing
            else DeduplicationMode.PER_TARGET
        )
        assert third_snapshot.target_ids == (
            (9, 4) if disable_routing else (7,)
        )
        assert state.snapshot_calls == 3
        assert [source_id for _, source_id in match_calls] == (
            [first_frame.source_id, second_frame.source_id]
            if disable_routing
            else [
                first_frame.source_id,
                second_frame.source_id,
                third_frame.source_id,
            ]
        )

    asyncio.run(scenario())


def test_processor_exception_propagates_and_cancels_blocked_egress():
    async def scenario():
        ingress_queue = asyncio.Queue()
        egress_queue = RecordingGetQueue()
        processor = ScriptedProcessor(RuntimeError("processor failed"))
        forwarder = RecordingForwarder()
        task = asyncio.create_task(
            aismixer._run_runtime_stages(
                ingress_queue,
                egress_queue,
                processor=processor,
                legacy_target_ids=(),
                output_forwarder=forwarder,
            )
        )

        await egress_queue.get_started.wait()
        await ingress_queue.put(make_frame("failure"))
        with pytest.raises(RuntimeError, match="processor failed"):
            await task

        assert processor.task is not None
        assert processor.task.done()
        assert egress_queue.getter_task is not None
        assert egress_queue.getter_task.done()
        assert egress_queue.getter_task.cancelled()
        assert forwarder.events == []

    asyncio.run(scenario())


def test_processor_failure_after_dequeue_releases_processing_capacity():
    async def scenario():
        processing_queue = aismixer._BoundedProcessingQueue(1)
        first_item = make_work_item(make_frame("failure-releases-slot"))
        await processing_queue.admit(lambda: first_item)
        processor = ScriptedProcessor(RuntimeError("processor failed"))

        with pytest.raises(RuntimeError, match="processor failed"):
            await aismixer.processor_stage_loop(
                processing_queue,
                CompletingEgressQueue(),
                processor=processor,
            )

        replacement = make_work_item(make_frame("replacement"))
        await asyncio.wait_for(
            processing_queue.admit(lambda: replacement),
            timeout=1.0,
        )

        assert processing_queue.maxsize == 1
        assert processing_queue.qsize() == 1
        assert await processing_queue.get() is replacement
        assert processing_queue.qsize() == 0

    asyncio.run(scenario())


def test_cancellation_resolves_active_acknowledgement_and_stage_tasks():
    async def scenario():
        ingress_queue = asyncio.Queue()
        egress_queue = RecordingEgressQueue()
        processor = ScriptedProcessor(output_batch(output("first", 0)))
        forwarder = GatedForwarder()
        task = asyncio.create_task(
            aismixer._run_runtime_stages(
                ingress_queue,
                egress_queue,
                processor=processor,
                legacy_target_ids=(),
                output_forwarder=forwarder,
            )
        )
        await ingress_queue.put(make_frame("cancel"))
        await forwarder.first_send_started.wait()

        assert len(egress_queue.batches) == 1
        batch = egress_queue.batches[0]
        assert not batch.completion.done()
        assert processor.task is not None
        assert forwarder.task is not None

        await cancel_task(task)

        assert processor.task.done()
        assert processor.task.cancelled()
        assert forwarder.task.done()
        assert forwarder.task.cancelled()
        assert batch.completion.done()
        assert batch.completion.cancelled()

    asyncio.run(scenario())


def test_main_constructs_one_processor_and_wires_runtime_stages(
    monkeypatch,
):
    async def scenario():
        state = RoutingState()
        processor = object()
        processor_factory_calls = []

        class MainForwarder:
            target_ids = ("udp:target",)
            target_id_by_name = {"udp:target": 1}
            all_target_ids = (0, 1)

            def __init__(self):
                self.close_calls = 0

            def close(self):
                self.close_calls += 1

        output_forwarder = MainForwarder()
        supervision_calls = []
        builder_calls = []

        def fake_builder(config, routing_state, target_id_by_name):
            builder_calls.append(
                (config, routing_state, target_id_by_name)
            )
            return None

        async def fake_supervise_named_tasks(task_specs):
            supervision_calls.append(tuple(task_specs))

        def fake_create_data_plane_processor():
            processor_factory_calls.append(None)
            return processor

        monkeypatch.setattr(aismixer, "SEC_INPUTS", [])
        monkeypatch.setattr(aismixer, "UDP_INPUTS", [])
        monkeypatch.setattr(aismixer, "config", {"control": None})
        monkeypatch.setattr(aismixer, "routing_state", state)
        monkeypatch.setattr(aismixer, "forwarder", output_forwarder)
        monkeypatch.setattr(aismixer, "DEBUG", False)
        monkeypatch.setattr(
            aismixer,
            "create_data_plane_processor",
            fake_create_data_plane_processor,
        )
        monkeypatch.setattr(
            aismixer,
            "build_optional_routing_control_server",
            fake_builder,
        )
        monkeypatch.setattr(
            aismixer,
            "_supervise_named_tasks",
            fake_supervise_named_tasks,
        )

        await aismixer.main()

        assert processor_factory_calls == [None]
        assert builder_calls == [
            ({"control": None}, state, {"udp:target": 1})
        ]
        assert len(supervision_calls) == 1
        specs = {spec.name: spec for spec in supervision_calls[0]}
        assert tuple(specs) == (
            "ingress-fan-in",
            "processor-stage",
            "egress-stage",
        )

        fan_in_factory = specs["ingress-fan-in"].coroutine_factory
        processor_factory = specs["processor-stage"].coroutine_factory
        egress_factory = specs["egress-stage"].coroutine_factory
        assert fan_in_factory.func is aismixer.ingress_fan_in_loop
        assert fan_in_factory.args[0] == ()
        processing_queue = fan_in_factory.args[1]
        assert processor_factory.func is aismixer.processor_stage_loop
        assert processor_factory.args[0] is processing_queue
        egress_queue = processor_factory.args[1]
        assert egress_factory.func is aismixer.egress_stage_loop
        assert egress_factory.args == (egress_queue, output_forwarder)
        assert isinstance(
            processing_queue,
            aismixer._BoundedProcessingQueue,
        )
        assert (
            processing_queue.maxsize
            == aismixer.DEFAULT_PROCESSING_QUEUE_MAXSIZE
        )
        assert isinstance(egress_queue, asyncio.Queue)
        assert egress_queue.maxsize == 1
        assert fan_in_factory.keywords == {
            "routing_state": state,
            "legacy_target_ids": (0, 1),
        }
        assert processor_factory.keywords == {"processor": processor}
        assert egress_factory.keywords == {
            "debug": False,
            "timestamp": aismixer.ts,
        }
        assert output_forwarder.close_calls == 1

    asyncio.run(scenario())


def test_outputless_frame_completes_locally_and_allows_next_frame():
    async def scenario():
        first_frame = make_frame("empty")
        second_frame = make_frame("output")
        processor = ScriptedProcessor(
            output_batch(),
            output_batch(output("second", 0)),
        )
        ingress_queue = asyncio.Queue()
        await ingress_queue.put(first_frame)
        await ingress_queue.put(second_frame)
        egress_queue = RecordingEgressQueue()

        class SignallingForwarder(RecordingForwarder):
            def __init__(self):
                super().__init__()
                self.sent = asyncio.Event()

            async def send_to_ids(self, target_ids, message):
                await super().send_to_ids(target_ids, message)
                self.sent.set()

        forwarder = SignallingForwarder()
        task = asyncio.create_task(
            aismixer._run_runtime_stages(
                ingress_queue,
                egress_queue,
                processor=processor,
                legacy_target_ids=(),
                output_forwarder=forwarder,
                processing_queue_maxsize=1,
            )
        )
        try:
            await forwarder.sent.wait()
        finally:
            await cancel_task(task)

        assert [call[0] for call in processor.calls] == [
            first_frame,
            second_frame,
        ]
        assert len(egress_queue.batches) == 1
        assert forwarder.events == [
            ("numeric", (0,), b"second\r\n")
        ]

    asyncio.run(scenario())


def test_debug_output_is_emitted_before_send_with_crlf_removed(capsys):
    async def scenario():
        completion = asyncio.get_running_loop().create_future()
        queue = asyncio.Queue()
        await queue.put(
            aismixer._EgressBatch(
                output_batch(output("message", 0)),
                completion,
            )
        )

        class DebugObservingForwarder:
            def __init__(self):
                self.output_seen_at_send = None

            async def send(self, _message):
                raise AssertionError("production egress called send()")

            async def send_to_ids(self, target_ids, _message):
                assert tuple(target_ids) == (0,)
                self.output_seen_at_send = capsys.readouterr().out

            async def send_to(self, _target_ids, _message):
                raise AssertionError("production egress called send_to()")

        forwarder = DebugObservingForwarder()
        task = asyncio.create_task(
            aismixer.egress_stage_loop(
                queue,
                forwarder,
                debug=True,
                timestamp=lambda: "STAMP",
            )
        )
        try:
            await completion
        finally:
            await cancel_task(task)

        assert (
            forwarder.output_seen_at_send
            == "STAMP OUTPUT => message\n"
        )

    asyncio.run(scenario())


def test_debug_decode_is_non_throwing_and_does_not_modify_payload(capsys):
    async def scenario():
        payload = b"invalid-\xff\r\n"
        output = ProcessorOutput(
            payload,
            (2, 1),
        )
        completion = asyncio.get_running_loop().create_future()
        queue = asyncio.Queue()
        await queue.put(
            aismixer._EgressBatch(OutputBatch((output,)), completion)
        )

        class IdentityRecordingForwarder:
            def __init__(self):
                self.message = None

            async def send(self, _message):
                raise AssertionError("production egress called send()")

            async def send_to_ids(self, target_ids, message):
                self.target_ids = tuple(target_ids)
                self.message = message

            async def send_to(self, _target_ids, _message):
                raise AssertionError("production egress called send_to()")

        forwarder = IdentityRecordingForwarder()
        task = asyncio.create_task(
            aismixer.egress_stage_loop(
                queue,
                forwarder,
                debug=True,
                timestamp=lambda: "STAMP",
            )
        )
        try:
            await completion
        finally:
            await cancel_task(task)

        assert forwarder.message is payload
        assert forwarder.target_ids == (2, 1)
        assert capsys.readouterr().out == "STAMP OUTPUT => invalid-\ufffd\n"

    asyncio.run(scenario())


def test_empty_target_ids_use_unified_numeric_dispatch_without_failure():
    async def scenario():
        payload = b"no-destinations\r\n"
        output = ProcessorOutput(payload, ())
        completion = asyncio.get_running_loop().create_future()
        queue = asyncio.Queue()
        await queue.put(
            aismixer._EgressBatch(OutputBatch((output,)), completion)
        )
        forwarder = RecordingForwarder()
        task = asyncio.create_task(aismixer.egress_stage_loop(queue, forwarder))

        try:
            await completion
        finally:
            await cancel_task(task)

        assert forwarder.events == [("numeric", (), payload)]

    asyncio.run(scenario())
