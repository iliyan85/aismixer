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
    before_failure = queue.metrics_snapshot()
    with pytest.raises(type(failure)) as caught:
        await queue.admit(factory)

    assert caught.value is failure
    assert queue.metrics_snapshot() == before_failure
    recovery = make_work_item("recovery")
    await queue.admit(lambda: recovery)
    assert await queue.get() is recovery
    recovered = queue.metrics_snapshot()
    assert recovered.enqueued == 1
    assert recovered.dequeued == 1
    assert recovered.depth == 0


async def wait_for_put_waiters(queue, expected):
    async def observe_public_metrics():
        while queue.metrics_snapshot().current_put_waiters != expected:
            await asyncio.sleep(0)

    await asyncio.wait_for(observe_public_metrics(), timeout=1.0)


def test_production_queue_capacity_defaults_are_positive_and_bounded():
    assert aismixer.DEFAULT_INGRESS_QUEUE_MAXSIZE == 1024
    assert aismixer.DEFAULT_PROCESSING_QUEUE_MAXSIZE == 1024
    assert type(aismixer.DEFAULT_INGRESS_QUEUE_MAXSIZE) is int
    assert type(aismixer.DEFAULT_PROCESSING_QUEUE_MAXSIZE) is int


def test_observed_queue_tracks_fifo_identity_and_each_public_operation_once():
    async def scenario():
        queue = aismixer._ObservedQueue(name="test-ingress", maxsize=3)
        first = object()
        second = object()

        fresh = queue.metrics_snapshot()
        repeated_fresh = queue.metrics_snapshot()
        assert fresh == repeated_fresh
        assert fresh is not repeated_fresh
        assert fresh.name == "test-ingress"
        assert fresh.capacity == 3
        assert fresh.depth == 0
        assert fresh.peak_depth == 0
        assert fresh.enqueued == 0
        assert fresh.dequeued == 0
        assert fresh.put_waits == 0
        assert fresh.current_put_waiters == 0

        queue.put_nowait(first)
        await queue.put(second)

        filled = queue.metrics_snapshot()
        assert filled.depth == 2
        assert filled.peak_depth == 2
        assert filled.enqueued == 2
        assert filled.dequeued == 0
        assert filled.put_waits == 0
        assert filled.current_put_waiters == 0

        # Pulling a snapshot must not consume, reorder, or replace items.
        assert queue.metrics_snapshot() == filled
        assert queue.get_nowait() is first
        assert await queue.get() is second

        drained = queue.metrics_snapshot()
        assert drained.depth == 0
        assert drained.peak_depth == 2
        assert drained.enqueued == 2
        assert drained.dequeued == 2

    asyncio.run(scenario())


def test_observed_queue_reports_one_full_wait_and_clears_it_after_capacity():
    async def scenario():
        queue = aismixer._ObservedQueue(name="blocked-ingress", maxsize=1)
        first = object()
        second = object()
        await queue.put(first)

        blocked_put = asyncio.create_task(queue.put(second))
        await wait_for_put_waiters(queue, 1)

        blocked = queue.metrics_snapshot()
        assert blocked.depth == 1
        assert blocked.enqueued == 1
        assert blocked.put_waits == 1
        assert blocked.current_put_waiters == 1
        assert not blocked_put.done()

        assert queue.get_nowait() is first
        await blocked_put

        admitted = queue.metrics_snapshot()
        assert admitted.depth == 1
        assert admitted.peak_depth == 1
        assert admitted.enqueued == 2
        assert admitted.dequeued == 1
        assert admitted.put_waits == 1
        assert admitted.current_put_waiters == 0
        assert await queue.get() is second

    asyncio.run(scenario())


def test_observed_queue_cancelled_waiters_are_historical_and_do_not_enqueue():
    async def scenario():
        queue = aismixer._ObservedQueue(name="cancelled-ingress", maxsize=1)
        occupied = object()
        await queue.put(occupied)
        blocked_puts = tuple(
            asyncio.create_task(queue.put(object()))
            for _ in range(2)
        )
        await wait_for_put_waiters(queue, 2)

        waiting = queue.metrics_snapshot()
        assert waiting.put_waits == 2
        assert waiting.current_put_waiters == 2
        assert waiting.enqueued == 1

        for task in blocked_puts:
            task.cancel()
        results = await asyncio.gather(*blocked_puts, return_exceptions=True)
        assert all(
            isinstance(result, asyncio.CancelledError)
            for result in results
        )

        cancelled = queue.metrics_snapshot()
        assert cancelled.put_waits == 2
        assert cancelled.current_put_waiters == 0
        assert cancelled.enqueued == 1
        assert cancelled.dequeued == 0
        assert cancelled.depth == 1
        assert queue.get_nowait() is occupied
        with pytest.raises(asyncio.QueueEmpty):
            queue.get_nowait()

    asyncio.run(scenario())


def test_observed_queue_metrics_are_isolated_per_instance():
    async def scenario():
        first = aismixer._ObservedQueue(name="first-ingress", maxsize=2)
        second = aismixer._ObservedQueue(name="second-ingress", maxsize=2)

        await first.put(object())

        first_snapshot = first.metrics_snapshot()
        second_snapshot = second.metrics_snapshot()
        assert first_snapshot.name == "first-ingress"
        assert first_snapshot.enqueued == 1
        assert first_snapshot.depth == 1
        assert second_snapshot.name == "second-ingress"
        assert second_snapshot.enqueued == 0
        assert second_snapshot.depth == 0

    asyncio.run(scenario())


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


def test_processing_queue_metrics_track_free_admission_dequeue_and_snapshots():
    async def scenario():
        queue = aismixer._BoundedProcessingQueue(2)
        work_item = make_work_item("metrics")

        fresh = queue.metrics_snapshot()
        repeated_fresh = queue.metrics_snapshot()
        assert fresh == repeated_fresh
        assert fresh is not repeated_fresh
        assert fresh.name == "processing"
        assert fresh.capacity == 2
        assert fresh.depth == 0
        assert fresh.peak_depth == 0
        assert fresh.enqueued == 0
        assert fresh.dequeued == 0
        assert fresh.put_waits == 0
        assert fresh.current_put_waiters == 0

        await queue.admit(lambda: work_item)
        admitted = queue.metrics_snapshot()
        assert admitted.depth == 1
        assert admitted.peak_depth == 1
        assert admitted.enqueued == 1
        assert admitted.dequeued == 0
        assert admitted.put_waits == 0
        assert admitted.current_put_waiters == 0
        assert queue.metrics_snapshot() == admitted

        assert await queue.get() is work_item
        drained = queue.metrics_snapshot()
        assert drained.depth == 0
        assert drained.peak_depth == 1
        assert drained.enqueued == 1
        assert drained.dequeued == 1

    asyncio.run(scenario())


def test_processing_queue_metrics_report_full_wait_and_capacity_release():
    async def scenario():
        queue = aismixer._BoundedProcessingQueue(1)
        first = make_work_item("metrics-first")
        second = make_work_item("metrics-second")
        second_factory_called = asyncio.Event()

        def make_second():
            second_factory_called.set()
            return second

        await queue.admit(lambda: first)
        blocked_admission = asyncio.create_task(queue.admit(make_second))
        await wait_for_put_waiters(queue, 1)

        blocked = queue.metrics_snapshot()
        assert blocked.depth == 1
        assert blocked.put_waits == 1
        assert blocked.current_put_waiters == 1
        assert not second_factory_called.is_set()
        assert not blocked_admission.done()

        assert await queue.get() is first
        await asyncio.wait_for(second_factory_called.wait(), timeout=1.0)
        await blocked_admission

        admitted = queue.metrics_snapshot()
        assert admitted.depth == 1
        assert admitted.peak_depth == 1
        assert admitted.enqueued == 2
        assert admitted.dequeued == 1
        assert admitted.put_waits == 1
        assert admitted.current_put_waiters == 0
        assert await queue.get() is second

    asyncio.run(scenario())


def test_processing_queue_counts_wait_for_released_reserved_permit():
    async def scenario():
        queue = aismixer._BoundedProcessingQueue(1)
        initial = make_work_item("initial")
        waiting_a = make_work_item("waiting-a")
        waiting_b = make_work_item("waiting-b")
        a_started = asyncio.Event()
        drain_a = asyncio.Event()
        drain_ready = asyncio.Event()
        intermediate = None
        events = []

        def make_a():
            nonlocal intermediate
            intermediate = queue.metrics_snapshot()
            events.append("a-factory")
            drain_a.set()
            return waiting_a

        def make_b():
            events.append("b-factory")
            return waiting_b

        async def admit_a():
            a_started.set()
            await queue.admit(make_a)

        async def drain_waiting_a():
            drain_ready.set()
            await drain_a.wait()
            work_item = await queue.get()
            events.append("drain-a")
            return work_item

        await queue.admit(lambda: initial)
        admission_a = asyncio.create_task(admit_a())
        await asyncio.wait_for(a_started.wait(), timeout=1.0)
        assert queue.metrics_snapshot().current_put_waiters == 1

        drain_task = asyncio.create_task(drain_waiting_a())
        await asyncio.wait_for(drain_ready.wait(), timeout=1.0)

        # The known non-empty get completes without suspending this task. It
        # assigns the released permit to A, after which B immediately enters
        # admit() while the work queue is empty and blocks on capacity.
        assert await queue.get() is initial
        assert queue.qsize() == 0
        await queue.admit(make_b)

        assert intermediate is not None
        assert intermediate.capacity == 1
        assert intermediate.depth == 0
        assert intermediate.peak_depth == 1
        assert intermediate.enqueued == 1
        assert intermediate.dequeued == 1
        assert intermediate.put_waits == 2
        assert intermediate.current_put_waiters == 1

        await admission_a
        assert await drain_task is waiting_a
        assert await queue.get() is waiting_b
        assert events == ["a-factory", "drain-a", "b-factory"]

        final = queue.metrics_snapshot()
        assert final.current_put_waiters == 0
        assert final.enqueued == final.dequeued == 3
        assert final.depth == 0
        assert final.peak_depth == final.capacity == 1
        assert final.put_waits == 2

    asyncio.run(scenario())


def test_processing_queue_metrics_are_isolated_per_instance():
    async def scenario():
        first = aismixer._BoundedProcessingQueue(1)
        second = aismixer._BoundedProcessingQueue(1)

        await first.admit(lambda: make_work_item("isolated"))

        assert first.metrics_snapshot().enqueued == 1
        assert first.metrics_snapshot().depth == 1
        assert second.metrics_snapshot().enqueued == 0
        assert second.metrics_snapshot().depth == 0
        assert second.metrics_snapshot().put_waits == 0

    asyncio.run(scenario())


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
        before_failure = queue.metrics_snapshot()
        with pytest.raises(RuntimeError) as caught:
            await queue.admit(
                partial(
                    aismixer._bind_processing_work_item,
                    make_frame("processing-snapshot-failure"),
                    legacy_target_ids=(),
                )
            )
        assert caught.value is failure
        assert queue.metrics_snapshot() == before_failure

        monkeypatch.setattr(aismixer, "ProcessingSnapshot", original_snapshot)
        recovery = make_work_item("snapshot-construction-recovery")
        await queue.admit(lambda: recovery)
        assert await queue.get() is recovery
        assert queue.metrics_snapshot().enqueued == 1
        assert queue.metrics_snapshot().dequeued == 1

    asyncio.run(scenario())


def test_work_item_construction_failure_releases_capacity(monkeypatch):
    async def scenario():
        failure = RuntimeError("work item failed")
        original_work_item = aismixer.ProcessingWorkItem

        def fail_work_item(**_kwargs):
            raise failure

        queue = aismixer._BoundedProcessingQueue(1)
        monkeypatch.setattr(aismixer, "ProcessingWorkItem", fail_work_item)
        before_failure = queue.metrics_snapshot()
        with pytest.raises(RuntimeError) as caught:
            await queue.admit(
                partial(
                    aismixer._bind_processing_work_item,
                    make_frame("work-item-failure"),
                    legacy_target_ids=(),
                )
            )
        assert caught.value is failure
        assert queue.metrics_snapshot() == before_failure

        monkeypatch.setattr(aismixer, "ProcessingWorkItem", original_work_item)
        recovery = make_work_item("work-item-construction-recovery")
        await queue.admit(lambda: recovery)
        assert await queue.get() is recovery
        assert queue.metrics_snapshot().enqueued == 1
        assert queue.metrics_snapshot().dequeued == 1

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
        before_failure = queue.metrics_snapshot()

        with pytest.raises(type(failure)) as caught:
            await queue.admit(lambda: work_item)
        assert caught.value is failure
        assert queue.metrics_snapshot() == before_failure

        await queue.admit(lambda: work_item)
        assert await queue.get() is work_item
        assert queue.metrics_snapshot().enqueued == 1
        assert queue.metrics_snapshot().dequeued == 1

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

        after_failures = queue.metrics_snapshot()
        assert after_failures.enqueued == 0
        assert after_failures.dequeued == 0
        assert after_failures.depth == 0
        assert after_failures.put_waits == 0

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
        await wait_for_put_waiters(queue, 1)
        assert not waiting_admission.done()
        waiting = queue.metrics_snapshot()
        assert waiting.put_waits == 1
        assert waiting.current_put_waiters == 1
        assert waiting.enqueued == 1

        waiting_admission.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting_admission

        assert waiting_factory_calls == 0
        assert queue.qsize() == 1
        cancelled = queue.metrics_snapshot()
        assert cancelled.put_waits == 1
        assert cancelled.current_put_waiters == 0
        assert cancelled.enqueued == 1
        assert cancelled.dequeued == 0
        assert cancelled.depth == 1
        assert await queue.get() is first

        recovery = make_work_item("after-cancellation")
        await queue.admit(lambda: recovery)
        assert await queue.get() is recovery
        assert queue.qsize() == 0
        final = queue.metrics_snapshot()
        assert final.put_waits == 1
        assert final.current_put_waiters == 0
        assert final.enqueued == 2
        assert final.dequeued == 2
        assert final.depth == 0

    asyncio.run(scenario())
