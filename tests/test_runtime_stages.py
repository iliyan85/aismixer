import asyncio
import inspect

import pytest

import aismixer
from core.data_plane import (
    DeduplicationMode,
    OutputBatch,
    ProcessorOutput,
)
from core.ingress_frame import IngressFrame
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


@pytest.mark.parametrize(
    ("operation", "required_arguments"),
    [
        (aismixer.processor_stage_loop, {}),
        (
            aismixer._run_runtime_stages,
            {"output_forwarder": object()},
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


def test_global_mode_accepts_explicit_empty_target_ids():
    async def scenario():
        frame = make_frame("empty-global-registry")
        processor = ScriptedProcessor(output_batch())

        with pytest.raises(asyncio.CancelledError):
            await aismixer.processor_stage_loop(
                FiniteQueue(frame),
                CompletingEgressQueue(),
                processor=processor,
                legacy_target_ids=(),
            )

        snapshot = processor.calls[0][1]
        assert snapshot.deduplication_mode is DeduplicationMode.GLOBAL
        assert snapshot.target_ids == ()

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


def empty_routing_table():
    return RoutingTable.from_definitions({}, []).compile_target_ids({})


def test_unsupported_item_is_rejected_before_snapshot_and_processor():
    async def scenario():
        frame = make_frame("accepted")
        state = RecordingRoutingState(RoutingSnapshot(7, None))
        processor = ScriptedProcessor(output_batch())
        egress_queue = CompletingEgressQueue()

        with pytest.raises(asyncio.CancelledError):
            await aismixer.processor_stage_loop(
                FiniteQueue(object(), frame),
                egress_queue,
                routing_state=state,
                processor=processor,
                legacy_target_ids=(),
            )

        assert state.snapshot_calls == 1
        assert len(processor.calls) == 1
        assert processor.calls[0][0] is frame
        assert processor.calls[0][1].routing_generation == 7
        assert (
            processor.calls[0][1].deduplication_mode
            is DeduplicationMode.GLOBAL
        )
        assert egress_queue.batches == []

    asyncio.run(scenario())


def test_routed_frame_resolves_one_numeric_target_only_snapshot():
    async def scenario():
        frame = make_frame("routed")
        table = RecordingNumericRoutingTable((3, 1))
        state = RecordingRoutingState(RoutingSnapshot(8, table))
        processor = ScriptedProcessor(output_batch())

        with pytest.raises(asyncio.CancelledError):
            await aismixer.processor_stage_loop(
                FiniteQueue(frame),
                CompletingEgressQueue(),
                routing_state=state,
                processor=processor,
                legacy_target_ids=(),
            )

        assert state.snapshot_calls == 1
        assert table.source_ids == ["udp:routed"]
        snapshot = processor.calls[0][1]
        assert snapshot.routing_generation == 8
        assert snapshot.deduplication_mode is DeduplicationMode.PER_TARGET
        assert snapshot.target_ids == (3, 1)

    asyncio.run(scenario())


def test_routed_no_match_remains_per_target_with_empty_targets():
    async def scenario():
        frame = make_frame("unmatched")
        table = RecordingNumericRoutingTable(())
        processor = ScriptedProcessor(output_batch())

        with pytest.raises(asyncio.CancelledError):
            await aismixer.processor_stage_loop(
                FiniteQueue(frame),
                CompletingEgressQueue(),
                routing_state=RecordingRoutingState(
                    RoutingSnapshot(4, table)
                ),
                processor=processor,
                legacy_target_ids=(),
            )

        snapshot = processor.calls[0][1]
        assert snapshot.deduplication_mode is DeduplicationMode.PER_TARGET
        assert snapshot.target_ids == ()

    asyncio.run(scenario())


def test_legacy_mode_receives_explicit_all_numeric_forwarder_ids():
    async def scenario():
        frame = make_frame("legacy")
        processor = ScriptedProcessor(output_batch())

        with pytest.raises(asyncio.CancelledError):
            await aismixer.processor_stage_loop(
                FiniteQueue(frame),
                CompletingEgressQueue(),
                routing_state=RecordingRoutingState(
                    RoutingSnapshot(5, None)
                ),
                processor=processor,
                legacy_target_ids=(0, 2, 3),
            )

        snapshot = processor.calls[0][1]
        assert snapshot.routing_generation == 5
        assert snapshot.deduplication_mode is DeduplicationMode.GLOBAL
        assert snapshot.target_ids == (0, 2, 3)

    asyncio.run(scenario())


def test_one_frame_produces_one_complete_ordered_egress_batch():
    async def scenario():
        frame = make_frame("one")
        processor_batch = output_batch(
            output("first", 0),
            output("second", 2, 1),
        )
        state = RecordingRoutingState(RoutingSnapshot(3, None))
        processor = ScriptedProcessor(processor_batch)
        egress_queue = CompletingEgressQueue()

        with pytest.raises(asyncio.CancelledError):
            await aismixer.processor_stage_loop(
                FiniteQueue(frame),
                egress_queue,
                routing_state=state,
                processor=processor,
                legacy_target_ids=(),
            )

        assert state.snapshot_calls == 1
        assert len(processor.calls) == 1
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
            assert ingress_queue.qsize() == 1
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
        assert ingress_queue.qsize() == 1

    asyncio.run(scenario())


def test_routing_replacement_while_blocked_affects_only_the_next_frame():
    async def scenario():
        first_table = empty_routing_table()
        second_table = empty_routing_table()
        state = RoutingState(first_table)
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
                routing_state=state,
                processor=processor,
                legacy_target_ids=(),
                output_forwarder=forwarder,
            )
        )
        try:
            await forwarder.first_send_started.wait()
            assert len(processor.calls) == 1
            state.replace(second_table)
            assert len(processor.calls) == 1

            forwarder.release_first_send.set()
            await second_call.wait()
        finally:
            await cancel_task(task)

        first_snapshot = processor.calls[0][1]
        second_snapshot = processor.calls[1][1]
        assert first_snapshot.routing_generation == 0
        assert first_snapshot.deduplication_mode is DeduplicationMode.PER_TARGET
        assert first_snapshot.target_ids == ()
        assert second_snapshot.routing_generation == 1
        assert second_snapshot.deduplication_mode is DeduplicationMode.PER_TARGET
        assert second_snapshot.target_ids == ()

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


def test_main_wires_module_processor_and_forwarder_into_runtime_stages(
    monkeypatch,
):
    async def scenario():
        state = RoutingState()
        processor = object()

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

        monkeypatch.setattr(aismixer, "SEC_INPUTS", [])
        monkeypatch.setattr(aismixer, "UDP_INPUTS", [])
        monkeypatch.setattr(aismixer, "config", {"control": None})
        monkeypatch.setattr(aismixer, "routing_state", state)
        monkeypatch.setattr(aismixer, "data_plane_processor", processor)
        monkeypatch.setattr(aismixer, "forwarder", output_forwarder)
        monkeypatch.setattr(aismixer, "DEBUG", False)
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
        ingress_queue = fan_in_factory.args[1]
        assert processor_factory.func is aismixer.processor_stage_loop
        assert processor_factory.args[0] is ingress_queue
        egress_queue = processor_factory.args[1]
        assert egress_factory.func is aismixer.egress_stage_loop
        assert egress_factory.args == (egress_queue, output_forwarder)
        assert isinstance(ingress_queue, asyncio.Queue)
        assert isinstance(egress_queue, asyncio.Queue)
        assert processor_factory.keywords == {
            "routing_state": state,
            "processor": processor,
            "legacy_target_ids": (0, 1),
        }
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
