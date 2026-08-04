import asyncio
from functools import partial

import pytest

import aismixer
from core.data_plane import (
    DeduplicationMode,
    ProcessingSnapshot,
    ProcessingWorkItem,
)
from core.ingress_frame import IngressFrame
from core.routing_state import RoutingSnapshot


def make_frame(label="queue"):
    return IngressFrame(
        kind="udp",
        source_id=f"udp:{label}",
        alias_for_s=None,
        remote_ip="192.0.2.10",
        assembler_key=f"192.0.2.10:{label}",
        payload=f"!AIVDM,1,1,,A,{label},0*00".encode("ascii"),
    )


def make_work_item(label="queue"):
    return ProcessingWorkItem(
        frame=make_frame(label),
        snapshot=ProcessingSnapshot(
            routing_generation=0,
            deduplication_mode=DeduplicationMode.GLOBAL,
            target_ids=(),
        ),
    )


async def assert_failure_releases_capacity(queue, factory, failure):
    with pytest.raises(type(failure)) as caught:
        await queue.admit(factory)

    assert caught.value is failure
    recovery = make_work_item("recovery")
    await queue.admit(lambda: recovery)
    assert await queue.get() is recovery


def test_production_queue_capacity_defaults_are_positive_and_bounded():
    assert aismixer.DEFAULT_INGRESS_QUEUE_MAXSIZE == 1024
    assert aismixer.DEFAULT_PROCESSING_QUEUE_MAXSIZE == 1024
    assert type(aismixer.DEFAULT_INGRESS_QUEUE_MAXSIZE) is int
    assert type(aismixer.DEFAULT_PROCESSING_QUEUE_MAXSIZE) is int


@pytest.mark.parametrize("capacity", [1, 2, 1024])
def test_queue_capacity_validator_and_processing_queue_accept_positive_ints(
    capacity,
):
    assert (
        aismixer._validate_queue_capacity(capacity, name="test_capacity")
        == capacity
    )
    assert aismixer._BoundedProcessingQueue(capacity).maxsize == capacity


@pytest.mark.parametrize("capacity", [None, "1", 1.0, True, False])
def test_queue_capacity_validator_rejects_non_integer_types(capacity):
    with pytest.raises(TypeError, match="test_capacity must be an integer"):
        aismixer._validate_queue_capacity(capacity, name="test_capacity")

    with pytest.raises(
        TypeError,
        match="processing_queue_maxsize must be an integer",
    ):
        aismixer._BoundedProcessingQueue(capacity)


@pytest.mark.parametrize("capacity", [0, -1, -100])
def test_queue_capacity_validator_rejects_values_below_one(capacity):
    with pytest.raises(ValueError, match="test_capacity must be at least 1"):
        aismixer._validate_queue_capacity(capacity, name="test_capacity")

    with pytest.raises(
        ValueError,
        match="processing_queue_maxsize must be at least 1",
    ):
        aismixer._BoundedProcessingQueue(capacity)


def test_processing_queue_blocks_at_capacity_and_releases_on_get():
    async def scenario():
        queue = aismixer._BoundedProcessingQueue(1)
        first = make_work_item("first")
        second = make_work_item("second")
        second_factory_calls = 0

        def make_second():
            nonlocal second_factory_calls
            second_factory_calls += 1
            return second

        await queue.admit(lambda: first)
        second_admission = asyncio.create_task(queue.admit(make_second))
        await asyncio.sleep(0)

        assert queue.qsize() == queue.maxsize == 1
        assert not second_admission.done()
        assert second_factory_calls == 0

        assert await queue.get() is first
        await second_admission

        assert second_factory_calls == 1
        assert queue.qsize() == 1
        assert await queue.get() is second
        assert queue.qsize() == 0

    asyncio.run(scenario())


def test_one_released_slot_admits_exactly_one_waiter_without_fairness_claim():
    async def scenario():
        queue = aismixer._BoundedProcessingQueue(1)
        occupied = make_work_item("occupied")
        waiting_items = (
            make_work_item("waiting-a"),
            make_work_item("waiting-b"),
        )
        factory_calls = []
        one_factory_called = asyncio.Event()

        def factory(item):
            factory_calls.append(item)
            one_factory_called.set()
            return item

        await queue.admit(lambda: occupied)
        waiters = tuple(
            asyncio.create_task(queue.admit(partial(factory, item)))
            for item in waiting_items
        )
        await asyncio.sleep(0)
        assert factory_calls == []

        assert await queue.get() is occupied
        await asyncio.wait_for(one_factory_called.wait(), timeout=1.0)

        assert len(factory_calls) == 1
        assert sum(task.done() for task in waiters) == 1
        assert queue.qsize() == 1

        first_admitted = await queue.get()
        await asyncio.gather(*waiters)
        second_admitted = await queue.get()
        assert {first_admitted, second_admitted} == set(waiting_items)

    asyncio.run(scenario())


def test_unsupported_ingress_does_not_reserve_processing_capacity():
    async def scenario():
        occupied = make_work_item("occupied-by-valid-work")
        recovery = make_work_item("recovery")
        processing_queue = aismixer._BoundedProcessingQueue(1)
        await processing_queue.admit(lambda: occupied)

        class UnsupportedThenBlockQueue:
            def __init__(self):
                self.calls = 0
                self.next_get_started = asyncio.Event()

            async def get(self):
                self.calls += 1
                if self.calls == 1:
                    return object()
                self.next_get_started.set()
                await asyncio.Future()

        class SnapshotGuard:
            def __init__(self):
                self.calls = 0

            def snapshot(self):
                self.calls += 1
                raise AssertionError("unsupported input captured routing")

        ingress_queue = UnsupportedThenBlockQueue()
        routing_state = SnapshotGuard()
        task = asyncio.create_task(
            aismixer.ingress_fan_in_loop(
                (ingress_queue,),
                processing_queue,
                routing_state=routing_state,
                legacy_target_ids=(),
            )
        )
        try:
            await asyncio.wait_for(
                ingress_queue.next_get_started.wait(),
                timeout=1.0,
            )
            assert ingress_queue.calls == 2
            assert routing_state.calls == 0
            assert processing_queue.qsize() == 1
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert await processing_queue.get() is occupied
        await processing_queue.admit(lambda: recovery)
        assert await processing_queue.get() is recovery

    asyncio.run(scenario())


def test_admission_does_not_suspend_between_factory_and_put_nowait(
    monkeypatch,
):
    async def scenario():
        events = []
        queue_type = asyncio.Queue

        class RecordingQueue(queue_type):
            def put_nowait(self, item):
                events.append(("put", item))
                super().put_nowait(item)

        monkeypatch.setattr(aismixer.asyncio, "Queue", RecordingQueue)
        queue = aismixer._BoundedProcessingQueue(1)
        work_item = make_work_item("synchronous")

        def factory():
            events.append(("factory", work_item))
            asyncio.get_running_loop().call_soon(
                events.append,
                ("scheduled", None),
            )
            return work_item

        await queue.admit(factory)

        assert events == [
            ("factory", work_item),
            ("put", work_item),
        ]
        await asyncio.sleep(0)
        assert events[-1] == ("scheduled", None)
        assert await queue.get() is work_item

    asyncio.run(scenario())


def test_cancellation_requested_during_binding_cannot_interrupt_enqueue():
    async def scenario():
        queue = aismixer._BoundedProcessingQueue(1)
        work_item = make_work_item("cancel-during-binding")

        def factory():
            asyncio.current_task().cancel()
            return work_item

        async def admit_then_checkpoint():
            await queue.admit(factory)
            await asyncio.sleep(0)

        task = asyncio.create_task(admit_then_checkpoint())
        with pytest.raises(asyncio.CancelledError):
            await task

        assert queue.qsize() == 1
        assert await queue.get() is work_item

    asyncio.run(scenario())


def test_routing_snapshot_capture_failure_releases_capacity():
    async def scenario():
        failure = RuntimeError("snapshot failed")

        class FailingRoutingState:
            def snapshot(self):
                raise failure

        queue = aismixer._BoundedProcessingQueue(1)
        await assert_failure_releases_capacity(
            queue,
            partial(
                aismixer._bind_processing_work_item,
                make_frame("snapshot-failure"),
                routing_state=FailingRoutingState(),
                legacy_target_ids=(),
            ),
            failure,
        )

    asyncio.run(scenario())


def test_route_matching_failure_releases_capacity():
    async def scenario():
        failure = RuntimeError("route match failed")

        class FailingRoutingTable:
            def match_target_ids(self, _source_id):
                raise failure

        class RoutedState:
            def snapshot(self):
                return RoutingSnapshot(4, FailingRoutingTable())

        queue = aismixer._BoundedProcessingQueue(1)
        await assert_failure_releases_capacity(
            queue,
            partial(
                aismixer._bind_processing_work_item,
                make_frame("match-failure"),
                routing_state=RoutedState(),
                legacy_target_ids=(),
            ),
            failure,
        )

    asyncio.run(scenario())


def test_processing_snapshot_construction_failure_releases_capacity(
    monkeypatch,
):
    async def scenario():
        failure = RuntimeError("processing snapshot failed")
        original_snapshot = aismixer.ProcessingSnapshot

        def fail_snapshot(**_kwargs):
            raise failure

        queue = aismixer._BoundedProcessingQueue(1)
        monkeypatch.setattr(aismixer, "ProcessingSnapshot", fail_snapshot)
        with pytest.raises(RuntimeError) as caught:
            await queue.admit(
                partial(
                    aismixer._bind_processing_work_item,
                    make_frame("processing-snapshot-failure"),
                    legacy_target_ids=(),
                )
            )
        assert caught.value is failure

        monkeypatch.setattr(aismixer, "ProcessingSnapshot", original_snapshot)
        recovery = make_work_item("snapshot-construction-recovery")
        await queue.admit(lambda: recovery)
        assert await queue.get() is recovery

    asyncio.run(scenario())


def test_work_item_construction_failure_releases_capacity(monkeypatch):
    async def scenario():
        failure = RuntimeError("work item failed")
        original_work_item = aismixer.ProcessingWorkItem

        def fail_work_item(**_kwargs):
            raise failure

        queue = aismixer._BoundedProcessingQueue(1)
        monkeypatch.setattr(aismixer, "ProcessingWorkItem", fail_work_item)
        with pytest.raises(RuntimeError) as caught:
            await queue.admit(
                partial(
                    aismixer._bind_processing_work_item,
                    make_frame("work-item-failure"),
                    legacy_target_ids=(),
                )
            )
        assert caught.value is failure

        monkeypatch.setattr(aismixer, "ProcessingWorkItem", original_work_item)
        recovery = make_work_item("work-item-construction-recovery")
        await queue.admit(lambda: recovery)
        assert await queue.get() is recovery

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "failure",
    [RuntimeError("put failed"), asyncio.QueueFull()],
    ids=("arbitrary-failure", "queue-full-invariant"),
)
def test_put_nowait_failure_releases_capacity_and_propagates_original_error(
    monkeypatch,
    failure,
):
    async def scenario():
        queue_type = asyncio.Queue
        fail_next_put = True

        class FailingPutQueue(queue_type):
            def put_nowait(self, item):
                nonlocal fail_next_put
                if fail_next_put:
                    fail_next_put = False
                    raise failure
                super().put_nowait(item)

        monkeypatch.setattr(aismixer.asyncio, "Queue", FailingPutQueue)
        queue = aismixer._BoundedProcessingQueue(1)
        work_item = make_work_item("put-failure")

        with pytest.raises(type(failure)) as caught:
            await queue.admit(lambda: work_item)
        assert caught.value is failure

        await queue.admit(lambda: work_item)
        assert await queue.get() is work_item

    asyncio.run(scenario())


def test_repeated_admission_failures_preserve_exact_capacity():
    async def scenario():
        queue = aismixer._BoundedProcessingQueue(2)
        failure = RuntimeError("repeatable failure")

        def fail():
            raise failure

        for _ in range(5):
            with pytest.raises(RuntimeError) as caught:
                await queue.admit(fail)
            assert caught.value is failure

        first = make_work_item("first-after-failures")
        second = make_work_item("second-after-failures")
        third = make_work_item("third-after-failures")
        third_factory_calls = 0

        def make_third():
            nonlocal third_factory_calls
            third_factory_calls += 1
            return third

        await queue.admit(lambda: first)
        await queue.admit(lambda: second)
        third_admission = asyncio.create_task(queue.admit(make_third))
        await asyncio.sleep(0)

        assert queue.qsize() == queue.maxsize == 2
        assert not third_admission.done()
        assert third_factory_calls == 0

        assert await queue.get() is first
        await third_admission
        assert third_factory_calls == 1
        assert queue.qsize() == 2
        assert await queue.get() is second
        assert await queue.get() is third

    asyncio.run(scenario())


def test_cancellation_while_waiting_does_not_call_factory_or_leak_capacity():
    async def scenario():
        queue = aismixer._BoundedProcessingQueue(1)
        first = make_work_item("occupied")
        waiting_factory_calls = 0

        def waiting_factory():
            nonlocal waiting_factory_calls
            waiting_factory_calls += 1
            return make_work_item("cancelled")

        await queue.admit(lambda: first)
        waiting_admission = asyncio.create_task(queue.admit(waiting_factory))
        await asyncio.sleep(0)
        assert not waiting_admission.done()

        waiting_admission.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting_admission

        assert waiting_factory_calls == 0
        assert queue.qsize() == 1
        assert await queue.get() is first

        recovery = make_work_item("after-cancellation")
        await queue.admit(lambda: recovery)
        assert await queue.get() is recovery
        assert queue.qsize() == 0

    asyncio.run(scenario())
