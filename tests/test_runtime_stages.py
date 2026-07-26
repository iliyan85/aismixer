import asyncio

import pytest

import aismixer
from core.data_plane import ProcessorOutput, RoutingDisposition
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


def broadcast(message):
    return ProcessorOutput(
        f"{message}\r\n",
        RoutingDisposition.LEGACY_BROADCAST,
    )


def targeted(message, *target_ids):
    return ProcessorOutput(
        f"{message}\r\n",
        RoutingDisposition.TARGETED,
        target_ids,
    )


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

    async def send(self, message):
        self.events.append(("broadcast", message))

    async def send_to(self, target_ids, message):
        self.events.append(("targeted", tuple(target_ids), message))


class GatedForwarder(RecordingForwarder):
    def __init__(self):
        super().__init__()
        self.first_send_started = asyncio.Event()
        self.release_first_send = asyncio.Event()
        self.second_send_started = asyncio.Event()
        self.task = None

    async def send(self, message):
        self.task = asyncio.current_task()
        self.events.append(("broadcast", message))
        if len(self.events) == 1:
            self.first_send_started.set()
            await self.release_first_send.wait()
        elif len(self.events) == 2:
            self.second_send_started.set()


class FirstSendFailingForwarder(RecordingForwarder):
    async def send(self, message):
        self.events.append(("broadcast", message))
        raise RuntimeError("send failed")


async def cancel_task(task):
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def empty_routing_table():
    return RoutingTable.from_definitions({}, [])


def test_unsupported_item_is_rejected_before_snapshot_and_processor():
    async def scenario():
        frame = make_frame("accepted")
        state = RecordingRoutingState(RoutingSnapshot(7, None))
        processor = ScriptedProcessor(())
        egress_queue = CompletingEgressQueue()

        with pytest.raises(asyncio.CancelledError):
            await aismixer.processor_stage_loop(
                FiniteQueue(object(), frame),
                egress_queue,
                routing_state=state,
                processor=processor,
            )

        assert state.snapshot_calls == 1
        assert len(processor.calls) == 1
        assert processor.calls[0][0] is frame
        assert processor.calls[0][1].routing_generation == 7
        assert egress_queue.batches == []

    asyncio.run(scenario())


def test_one_frame_produces_one_complete_ordered_egress_batch():
    async def scenario():
        frame = make_frame("one")
        outputs = (
            broadcast("first"),
            targeted("second", "udp:b", "udp:a"),
        )
        state = RecordingRoutingState(RoutingSnapshot(3, None))
        processor = ScriptedProcessor(outputs)
        egress_queue = CompletingEgressQueue()

        with pytest.raises(asyncio.CancelledError):
            await aismixer.processor_stage_loop(
                FiniteQueue(frame),
                egress_queue,
                routing_state=state,
                processor=processor,
            )

        assert state.snapshot_calls == 1
        assert len(processor.calls) == 1
        assert len(egress_queue.batches) == 1
        assert egress_queue.batches[0].outputs is outputs
        assert egress_queue.batches[0].outputs == outputs

    asyncio.run(scenario())


def test_egress_dispatches_batch_sequentially_in_tuple_order():
    async def scenario():
        outputs = (
            broadcast("first"),
            targeted("second", "udp:b", "udp:a"),
        )
        completion = asyncio.get_running_loop().create_future()
        queue = asyncio.Queue()
        await queue.put(aismixer._EgressBatch(outputs, completion))
        forwarder = GatedForwarder()
        task = asyncio.create_task(
            aismixer.egress_stage_loop(queue, forwarder)
        )
        try:
            await forwarder.first_send_started.wait()
            assert forwarder.events == [("broadcast", "first\r\n")]
            assert not completion.done()

            forwarder.release_first_send.set()
            await completion
        finally:
            await cancel_task(task)

        assert forwarder.events == [
            ("broadcast", "first\r\n"),
            ("targeted", ("udp:b", "udp:a"), "second\r\n"),
        ]

    asyncio.run(scenario())


def test_processor_does_not_run_ahead_while_first_batch_is_unacknowledged():
    async def scenario():
        first_frame = make_frame("first")
        second_frame = make_frame("second")
        processor = ScriptedProcessor(
            (broadcast("first"),),
            (broadcast("second"),),
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
            (broadcast("first"),),
            (broadcast("second"),),
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
            ("broadcast", "first\r\n"),
            ("broadcast", "second\r\n"),
        ]

    asyncio.run(scenario())


def test_first_send_failure_preserves_effects_and_prevents_later_work():
    async def scenario():
        first_frame = make_frame("first")
        second_frame = make_frame("second")
        effects = []
        outputs = (broadcast("first"), broadcast("later"))

        def record_effects(call_index):
            if call_index == 0:
                effects.extend(("constructed:first", "constructed:later"))
            else:
                effects.append("processed:second-frame")

        processor = ScriptedProcessor(
            outputs,
            (broadcast("second-frame"),),
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
                output_forwarder=forwarder,
            )

        assert [call[0] for call in processor.calls] == [first_frame]
        assert effects == ["constructed:first", "constructed:later"]
        assert outputs[1].message == "later\r\n"
        assert forwarder.events == [("broadcast", "first\r\n")]
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
            (broadcast("first"),),
            (broadcast("second"),),
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
        assert first_snapshot.routing_table is first_table
        assert second_snapshot.routing_generation == 1
        assert second_snapshot.routing_table is second_table

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
        processor = ScriptedProcessor((broadcast("first"),))
        forwarder = GatedForwarder()
        task = asyncio.create_task(
            aismixer._run_runtime_stages(
                ingress_queue,
                egress_queue,
                processor=processor,
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

            def __init__(self):
                self.close_calls = 0

            def close(self):
                self.close_calls += 1

        output_forwarder = MainForwarder()
        supervision_calls = []
        builder_calls = []

        def fake_builder(config, routing_state, target_ids):
            builder_calls.append((config, routing_state, target_ids))
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
            ({"control": None}, state, ("udp:target",))
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
        processor = ScriptedProcessor((), (broadcast("second"),))
        ingress_queue = asyncio.Queue()
        await ingress_queue.put(first_frame)
        await ingress_queue.put(second_frame)
        egress_queue = RecordingEgressQueue()

        class SignallingForwarder(RecordingForwarder):
            def __init__(self):
                super().__init__()
                self.sent = asyncio.Event()

            async def send(self, message):
                await super().send(message)
                self.sent.set()

        forwarder = SignallingForwarder()
        task = asyncio.create_task(
            aismixer._run_runtime_stages(
                ingress_queue,
                egress_queue,
                processor=processor,
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
        assert forwarder.events == [("broadcast", "second\r\n")]

    asyncio.run(scenario())


def test_debug_output_is_emitted_before_send_with_crlf_removed(capsys):
    async def scenario():
        completion = asyncio.get_running_loop().create_future()
        queue = asyncio.Queue()
        await queue.put(
            aismixer._EgressBatch((broadcast("message"),), completion)
        )

        class DebugObservingForwarder:
            def __init__(self):
                self.output_seen_at_send = None

            async def send(self, _message):
                self.output_seen_at_send = capsys.readouterr().out

            async def send_to(self, _target_ids, _message):
                raise AssertionError("unexpected targeted send")

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


def test_unsupported_disposition_is_a_programming_error():
    async def scenario():
        class InvalidOutput:
            message = "invalid\r\n"
            disposition = object()
            target_ids = ()

        completion = asyncio.get_running_loop().create_future()
        queue = asyncio.Queue()
        await queue.put(
            aismixer._EgressBatch((InvalidOutput(),), completion)
        )
        task = asyncio.create_task(
            aismixer.egress_stage_loop(queue, RecordingForwarder())
        )

        with pytest.raises(
            AssertionError,
            match="Unsupported routing disposition",
        ):
            await task

        assert isinstance(completion.exception(), AssertionError)

    asyncio.run(scenario())
