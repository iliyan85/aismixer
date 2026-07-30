import asyncio
import gc

import pytest

import aismixer
from core.data_plane import ProcessorOutput, RoutingDisposition
from core.ingress_frame import IngressFrame


WATCHDOG_SECONDS = 1.0


def make_frame(label="frame"):
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
        f"{message}\r\n".encode("utf-8"),
        RoutingDisposition.LEGACY_BROADCAST,
    )


async def wait_for_event(event):
    await asyncio.wait_for(event.wait(), timeout=WATCHDOG_SECONDS)


async def wait_for_probes(*probes):
    await asyncio.wait_for(
        asyncio.gather(*(probe.started.wait() for probe in probes)),
        timeout=WATCHDOG_SECONDS,
    )


class LifecycleProbe:
    def __init__(self, failure=None):
        self.failure = failure
        self.release = asyncio.Event()
        self.started = asyncio.Event()
        self.finished = asyncio.Event()
        self.task = None
        self.task_name = None
        self.call_count = 0
        self.cancel_count = 0
        self.finish_count = 0

    async def run(self):
        self.call_count += 1
        self.task = asyncio.current_task()
        self.task_name = self.task.get_name()
        self.started.set()
        try:
            await self.release.wait()
            if self.failure is not None:
                raise self.failure
        except asyncio.CancelledError:
            self.cancel_count += 1
            raise
        finally:
            self.finish_count += 1
            self.finished.set()


class ObservingQueue(asyncio.Queue):
    def __init__(self, *, maxsize=0):
        super().__init__(maxsize=maxsize)
        self.get_started = asyncio.Event()
        self.get_task = None
        self.put_items = []

    async def get(self):
        self.get_task = asyncio.current_task()
        self.get_started.set()
        return await super().get()

    async def put(self, item):
        self.put_items.append(item)
        await super().put(item)


class ScriptedProcessor:
    def __init__(self, outputs):
        self.outputs = outputs
        self.calls = []
        self.task = None

    def process(self, frame, snapshot):
        self.task = asyncio.current_task()
        self.calls.append((frame, snapshot))
        return self.outputs


class FailingForwarder:
    def __init__(self, failure):
        self.failure = failure
        self.started = asyncio.Event()
        self.task = None
        self.messages = []

    async def send(self, message):
        self.task = asyncio.current_task()
        self.messages.append(message)
        self.started.set()
        raise self.failure

    async def send_to(self, _target_ids, _message):
        raise AssertionError("expected legacy broadcast output")


class GatedForwarder:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.task = None
        self.messages = []

    async def send(self, message):
        self.task = asyncio.current_task()
        self.messages.append(message)
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise

    async def send_to(self, _target_ids, _message):
        raise AssertionError("expected legacy broadcast output")


class GetProbeQueue:
    def __init__(self, *, failure=None, cancellation_failure=None):
        self.failure = failure
        self.cancellation_failure = cancellation_failure
        self.release = asyncio.Event()
        self.started = asyncio.Event()
        self.finished = asyncio.Event()
        self.task = None
        self.call_count = 0
        self.cancel_count = 0
        self.finish_count = 0

    async def get(self):
        self.call_count += 1
        self.task = asyncio.current_task()
        self.started.set()
        try:
            await self.release.wait()
            if self.failure is not None:
                raise self.failure
            raise AssertionError("released get probe has no configured result")
        except asyncio.CancelledError:
            self.cancel_count += 1
            if self.cancellation_failure is not None:
                raise self.cancellation_failure
            raise
        finally:
            self.finish_count += 1
            self.finished.set()


def make_spec(name, coroutine_factory):
    return aismixer._RuntimeTaskSpec(name, coroutine_factory)


def assert_probe_finished(role, probe, *, cancelled):
    assert probe.call_count == 1
    assert probe.finish_count == 1
    assert probe.task_name == role
    assert probe.task is not None
    assert probe.task.done()
    assert probe.task.cancelled() is cancelled
    assert probe.cancel_count == int(cancelled)
    assert probe.task not in asyncio.all_tasks()


def test_ingress_task_name_escapes_and_bounds_configured_diagnostic_label():
    name = aismixer._ingress_task_name(
        "udp",
        2,
        {"id": "unsafe\nidentity" + ("x" * 200)},
        "127.0.0.1",
        10110,
    )

    prefix = "udp-ingress:2:"
    assert name.startswith(prefix + r"unsafe\nidentity")
    assert "\n" not in name
    assert len(name.removeprefix(prefix)) <= 80


async def run_named_failure_case(failing_role):
    failure = RuntimeError(f"{failing_role} failed")
    roles = (
        "udp-ingress:192.0.2.10:17778",
        "udpsec-ingress:192.0.2.20:17779",
        "ingress-fan-in",
        "processor-stage",
        "egress-stage",
    )
    probes = {
        role: LifecycleProbe(failure if role == failing_role else None)
        for role in roles
    }
    supervisor = asyncio.create_task(
        aismixer._supervise_named_tasks(
            tuple(make_spec(role, probes[role].run) for role in roles)
        ),
        name="test-runtime-supervisor",
    )

    await wait_for_probes(*(probes[role] for role in roles))
    probes[failing_role].release.set()

    with pytest.raises(RuntimeError) as exc_info:
        await supervisor

    assert exc_info.value is failure
    assert supervisor.done()
    assert supervisor not in asyncio.all_tasks()
    for role, probe in probes.items():
        assert_probe_finished(
            role,
            probe,
            cancelled=role != failing_role,
        )


def test_udp_producer_failure_cancels_and_awaits_all_runtime_siblings():
    asyncio.run(
        run_named_failure_case("udp-ingress:192.0.2.10:17778")
    )


def test_udpsec_producer_failure_cancels_and_awaits_all_runtime_siblings():
    asyncio.run(
        run_named_failure_case("udpsec-ingress:192.0.2.20:17779")
    )


def test_fan_in_failure_cancels_producers_processor_and_egress():
    asyncio.run(run_named_failure_case("ingress-fan-in"))


def test_processor_failure_cancels_ingress_fan_in_and_egress():
    asyncio.run(run_named_failure_case("processor-stage"))


def test_egress_failure_propagates_through_ack_and_cancels_ingress_side():
    async def scenario():
        failure = RuntimeError("egress failed")
        upstream = {
            "udp-ingress:192.0.2.10:17778": LifecycleProbe(),
            "udpsec-ingress:192.0.2.20:17779": LifecycleProbe(),
            "ingress-fan-in": LifecycleProbe(),
        }
        ingress_queue = ObservingQueue()
        egress_queue = ObservingQueue(maxsize=1)
        processor = ScriptedProcessor((broadcast("one"),))
        output_forwarder = FailingForwarder(failure)
        specs = [
            make_spec(role, probe.run)
            for role, probe in upstream.items()
        ]
        specs.extend(
            (
                make_spec(
                    "processor-stage",
                    lambda: aismixer.processor_stage_loop(
                        ingress_queue,
                        egress_queue,
                        processor=processor,
                    ),
                ),
                make_spec(
                    "egress-stage",
                    lambda: aismixer.egress_stage_loop(
                        egress_queue,
                        output_forwarder,
                    ),
                ),
            )
        )
        supervisor = asyncio.create_task(
            aismixer._supervise_named_tasks(tuple(specs)),
            name="test-runtime-supervisor",
        )

        await wait_for_probes(*upstream.values())
        await wait_for_event(ingress_queue.get_started)
        await wait_for_event(egress_queue.get_started)
        frame = make_frame("egress-failure")
        await ingress_queue.put(frame)

        with pytest.raises(RuntimeError) as exc_info:
            await supervisor

        assert exc_info.value is failure
        assert processor.calls[0][0] is frame
        assert len(processor.calls) == 1
        assert output_forwarder.messages == [b"one\r\n"]
        assert len(egress_queue.put_items) == 1
        batch = egress_queue.put_items[0]
        assert batch.outputs is processor.outputs
        assert batch.completion.done()
        assert not batch.completion.cancelled()
        assert processor.task is not None
        assert processor.task.get_name() == "processor-stage"
        assert processor.task.done()
        assert output_forwarder.task is not None
        assert output_forwarder.task.get_name() == "egress-stage"
        assert output_forwarder.task.done()
        assert supervisor.done()
        for role, probe in upstream.items():
            assert_probe_finished(role, probe, cancelled=True)

    asyncio.run(scenario())


def test_unexpected_essential_task_return_names_role_and_cancels_siblings():
    async def scenario():
        returning_role = "udp-ingress:192.0.2.10:17778"
        roles = (
            returning_role,
            "udpsec-ingress:192.0.2.20:17779",
            "ingress-fan-in",
            "processor-stage",
            "egress-stage",
        )
        probes = {role: LifecycleProbe() for role in roles}
        supervisor = asyncio.create_task(
            aismixer._supervise_named_tasks(
                tuple(make_spec(role, probes[role].run) for role in roles)
            ),
            name="test-runtime-supervisor",
        )

        await wait_for_probes(*(probes[role] for role in roles))
        probes[returning_role].release.set()

        with pytest.raises(RuntimeError) as exc_info:
            await supervisor

        assert returning_role in str(exc_info.value)
        assert supervisor.done()
        for role, probe in probes.items():
            assert_probe_finished(
                role,
                probe,
                cancelled=role != returning_role,
            )

    asyncio.run(scenario())


def test_unexpected_essential_task_cancellation_names_role_and_cleans_siblings():
    async def scenario():
        cancelled_role = "udpsec-ingress:unexpected-cancel"
        cancelled_probe = LifecycleProbe(asyncio.CancelledError())
        sibling_probe = LifecycleProbe()
        supervisor = asyncio.create_task(
            aismixer._supervise_named_tasks(
                (
                    make_spec(cancelled_role, cancelled_probe.run),
                    make_spec("processor-stage", sibling_probe.run),
                )
            )
        )

        await wait_for_probes(cancelled_probe, sibling_probe)
        cancelled_probe.release.set()

        with pytest.raises(
            RuntimeError,
            match="udpsec-ingress:unexpected-cancel.*cancelled unexpectedly",
        ):
            await supervisor

        assert_probe_finished(
            cancelled_role,
            cancelled_probe,
            cancelled=True,
        )
        assert_probe_finished(
            "processor-stage",
            sibling_probe,
            cancelled=True,
        )

    asyncio.run(scenario())


def test_simultaneous_real_failure_wins_over_normal_completion():
    async def scenario():
        failure = RuntimeError("causal failure")
        release = asyncio.Event()
        started = (asyncio.Event(), asyncio.Event())

        async def return_normally():
            started[0].set()
            await release.wait()

        async def fail():
            started[1].set()
            await release.wait()
            raise failure

        supervisor = asyncio.create_task(
            aismixer._supervise_named_tasks(
                (
                    make_spec("udp-ingress:returning", return_normally),
                    make_spec("udpsec-ingress:failing", fail),
                )
            )
        )
        for event in started:
            await wait_for_event(event)
        release.set()

        with pytest.raises(RuntimeError) as excinfo:
            await supervisor

        assert excinfo.value is failure

    asyncio.run(scenario())


def test_external_cancellation_cleans_all_tasks_and_active_acknowledgement():
    async def scenario():
        upstream = {
            "udp-ingress:192.0.2.10:17778": LifecycleProbe(),
            "udpsec-ingress:192.0.2.20:17779": LifecycleProbe(),
            "ingress-fan-in": LifecycleProbe(),
        }
        ingress_queue = ObservingQueue()
        egress_queue = ObservingQueue(maxsize=1)
        processor = ScriptedProcessor((broadcast("one"),))
        output_forwarder = GatedForwarder()
        specs = [
            make_spec(role, probe.run)
            for role, probe in upstream.items()
        ]
        specs.extend(
            (
                make_spec(
                    "processor-stage",
                    lambda: aismixer.processor_stage_loop(
                        ingress_queue,
                        egress_queue,
                        processor=processor,
                    ),
                ),
                make_spec(
                    "egress-stage",
                    lambda: aismixer.egress_stage_loop(
                        egress_queue,
                        output_forwarder,
                    ),
                ),
            )
        )
        supervisor = asyncio.create_task(
            aismixer._supervise_named_tasks(tuple(specs)),
            name="test-runtime-supervisor",
        )

        await wait_for_probes(*upstream.values())
        await wait_for_event(ingress_queue.get_started)
        await wait_for_event(egress_queue.get_started)
        await ingress_queue.put(make_frame("cancel"))
        await wait_for_event(output_forwarder.started)

        assert len(egress_queue.put_items) == 1
        batch = egress_queue.put_items[0]
        assert not batch.completion.done()

        supervisor.cancel()
        with pytest.raises(asyncio.CancelledError):
            await supervisor

        assert supervisor.done()
        assert supervisor.cancelled()
        assert batch.completion.done()
        assert batch.completion.cancelled()
        assert processor.task is not None
        assert processor.task.done()
        assert processor.task.cancelled()
        assert output_forwarder.task is not None
        assert output_forwarder.task.done()
        assert output_forwarder.task.cancelled()
        assert output_forwarder.cancelled.is_set()
        for role, probe in upstream.items():
            assert_probe_finished(role, probe, cancelled=True)
        owned_tasks = {
            *(probe.task for probe in upstream.values()),
            processor.task,
            output_forwarder.task,
        }
        assert owned_tasks.isdisjoint(asyncio.all_tasks())

    asyncio.run(scenario())


def test_fan_in_reader_failure_cancels_and_awaits_sibling_readers():
    async def scenario():
        failure = RuntimeError("reader failed")
        failing_queue = GetProbeQueue(failure=failure)
        sibling_queue = GetProbeQueue()
        output_queue = asyncio.Queue()
        parent = asyncio.create_task(
            aismixer.ingress_fan_in_loop(
                (failing_queue, sibling_queue),
                output_queue,
            ),
            name="test-fan-in-parent",
        )

        await wait_for_event(failing_queue.started)
        await wait_for_event(sibling_queue.started)
        failing_queue.release.set()

        with pytest.raises(RuntimeError) as exc_info:
            await parent

        assert exc_info.value is failure
        assert parent.done()
        assert failing_queue.task is not None
        assert failing_queue.task.done()
        assert not failing_queue.task.cancelled()
        assert failing_queue.call_count == 1
        assert failing_queue.finish_count == 1
        assert sibling_queue.task is not None
        assert sibling_queue.task.done()
        assert sibling_queue.task.cancelled()
        assert sibling_queue.call_count == 1
        assert sibling_queue.cancel_count == 1
        assert sibling_queue.finish_count == 1
        assert {
            failing_queue.task,
            sibling_queue.task,
        }.isdisjoint(asyncio.all_tasks())

    asyncio.run(scenario())


def test_empty_fan_in_remains_pending_and_cancels_cleanly():
    async def scenario():
        task = asyncio.create_task(
            aismixer.ingress_fan_in_loop((), asyncio.Queue()),
            name="test-empty-fan-in",
        )
        try:
            checkpoint = asyncio.get_running_loop().create_future()
            asyncio.get_running_loop().call_soon(
                checkpoint.set_result,
                None,
            )
            await checkpoint

            assert not task.done()

            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            assert task.done()
            assert task.cancelled()
            assert task not in asyncio.all_tasks()
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_supervisor_with_empty_fan_in_stays_active_and_cancels_cleanly():
    async def scenario():
        loop = asyncio.get_running_loop()
        exception_contexts = []
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(
            lambda _loop, context: exception_contexts.append(context)
        )

        fan_in_started = asyncio.Event()
        fan_in_task = None
        producer = LifecycleProbe()
        processor = LifecycleProbe()
        egress = LifecycleProbe()

        async def empty_fan_in():
            nonlocal fan_in_task
            fan_in_task = asyncio.current_task()
            fan_in_started.set()
            await aismixer.ingress_fan_in_loop((), asyncio.Queue())

        supervisor = asyncio.create_task(
            aismixer._supervise_named_tasks(
                (
                    make_spec("udp-ingress:test", producer.run),
                    make_spec("ingress-fan-in", empty_fan_in),
                    make_spec("processor-stage", processor.run),
                    make_spec("egress-stage", egress.run),
                )
            ),
            name="test-runtime-supervisor",
        )
        try:
            await wait_for_event(fan_in_started)
            await wait_for_probes(producer, processor, egress)
            checkpoint = loop.create_future()
            loop.call_soon(checkpoint.set_result, None)
            await checkpoint

            assert not supervisor.done()
            assert fan_in_task is not None
            assert not fan_in_task.done()

            supervisor.cancel()
            with pytest.raises(asyncio.CancelledError):
                await supervisor

            assert supervisor.done()
            assert supervisor.cancelled()
            assert fan_in_task.done()
            assert fan_in_task.cancelled()
            assert_probe_finished(
                "udp-ingress:test",
                producer,
                cancelled=True,
            )
            assert_probe_finished(
                "processor-stage",
                processor,
                cancelled=True,
            )
            assert_probe_finished(
                "egress-stage",
                egress,
                cancelled=True,
            )
            assert {
                fan_in_task,
                producer.task,
                processor.task,
                egress.task,
            }.isdisjoint(asyncio.all_tasks())

            checkpoint = loop.create_future()
            loop.call_soon(checkpoint.set_result, None)
            await checkpoint
            gc.collect()
            assert exception_contexts == []
        finally:
            if not supervisor.done():
                supervisor.cancel()
            await asyncio.gather(supervisor, return_exceptions=True)
            loop.set_exception_handler(previous_handler)

    asyncio.run(scenario(), debug=True)


def test_fan_in_parent_cancellation_cancels_and_awaits_all_readers():
    async def scenario():
        queues = (GetProbeQueue(), GetProbeQueue(), GetProbeQueue())
        parent = asyncio.create_task(
            aismixer.ingress_fan_in_loop(queues, asyncio.Queue()),
            name="test-fan-in-parent",
        )

        for queue in queues:
            await wait_for_event(queue.started)

        parent.cancel()
        with pytest.raises(asyncio.CancelledError):
            await parent

        assert parent.done()
        assert parent.cancelled()
        for queue in queues:
            assert queue.task is not None
            assert queue.task.done()
            assert queue.task.cancelled()
            assert queue.call_count == 1
            assert queue.cancel_count == 1
            assert queue.finish_count == 1
            assert queue.task not in asyncio.all_tasks()

    asyncio.run(scenario())


def test_fan_in_cleanup_retrieves_secondary_reader_exception():
    async def scenario():
        loop = asyncio.get_running_loop()
        loop.set_debug(True)
        exception_contexts = []
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(
            lambda _loop, context: exception_contexts.append(context)
        )

        primary_error = RuntimeError("primary reader failed")
        secondary_error = RuntimeError("reader cleanup failed")
        primary_queue = GetProbeQueue(failure=primary_error)
        secondary_queue = GetProbeQueue(
            cancellation_failure=secondary_error
        )
        parent = asyncio.create_task(
            aismixer.ingress_fan_in_loop(
                (primary_queue, secondary_queue),
                asyncio.Queue(),
            ),
            name="test-fan-in-parent",
        )
        try:
            await wait_for_event(primary_queue.started)
            await wait_for_event(secondary_queue.started)
            primary_queue.release.set()

            caught = None
            try:
                await parent
            except RuntimeError as exc:
                caught = exc

            assert caught is primary_error
            assert primary_queue.task is not None
            assert primary_queue.task.done()
            assert secondary_queue.task is not None
            assert secondary_queue.task.done()
            assert not secondary_queue.task.cancelled()
            assert secondary_queue.cancel_count == 1
            assert secondary_queue.finish_count == 1

            primary_task = primary_queue.task
            secondary_task = secondary_queue.task
            primary_queue.task = None
            secondary_queue.task = None
            primary_queue.failure = None
            secondary_queue.cancellation_failure = None
            primary_error.__traceback__ = None
            secondary_error.__traceback__ = None
            caught = None
            del primary_task
            del secondary_task
            del parent
            gc.collect()
            checkpoint = loop.create_future()
            loop.call_soon(checkpoint.set_result, None)
            await checkpoint
            gc.collect()

            assert exception_contexts == []
        finally:
            loop.set_exception_handler(previous_handler)

    asyncio.run(scenario(), debug=True)


def test_task_creation_failure_closes_coroutine_and_cleans_created_task(
    monkeypatch,
):
    async def scenario():
        failure = RuntimeError("task creation failed")
        created_tasks = []
        rejected_coroutines = []
        create_calls = 0
        real_create_task = asyncio.create_task

        async def block_forever():
            await asyncio.Future()

        def first_factory():
            return block_forever()

        def rejected_factory():
            coroutine = block_forever()
            rejected_coroutines.append(coroutine)
            return coroutine

        def controlled_create_task(coroutine, *, name=None):
            nonlocal create_calls
            create_calls += 1
            if create_calls == 2:
                raise failure
            task = real_create_task(coroutine, name=name)
            created_tasks.append(task)
            return task

        monkeypatch.setattr(
            aismixer.asyncio,
            "create_task",
            controlled_create_task,
        )
        try:
            with pytest.raises(RuntimeError) as exc_info:
                await aismixer._supervise_named_tasks(
                    (
                        make_spec("udp-ingress:first", first_factory),
                        make_spec(
                            "udpsec-ingress:rejected",
                            rejected_factory,
                        ),
                    )
                )
        finally:
            monkeypatch.setattr(
                aismixer.asyncio,
                "create_task",
                real_create_task,
            )

        assert exc_info.value is failure
        assert len(created_tasks) == 1
        assert created_tasks[0].get_name() == "udp-ingress:first"
        assert created_tasks[0].done()
        assert created_tasks[0].cancelled()
        assert created_tasks[0] not in asyncio.all_tasks()
        assert len(rejected_coroutines) == 1
        assert rejected_coroutines[0].cr_frame is None

    asyncio.run(scenario())
