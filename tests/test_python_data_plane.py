import inspect

import pytest

import core.python_data_plane as python_data_plane_module
from assembler import AIVDMAssembler
from core.data_plane import (
    DataPlaneProcessor,
    DeduplicationMode,
    OutputBatch,
    ProcessingSnapshot,
    ProcessorOutput,
    ProcessorResetReport,
)
from core.ingress_frame import IngressFrame
from core.python_data_plane import PythonDataPlaneProcessor
from core.state.s_cache import SourceState
from dedup import Deduplicator


SOURCE_ID = "udp:source"
REMOTE_IP = "192.0.2.10"
ASSEMBLER_KEY = "192.0.2.10:17778"
WALL_TIME = 1_700_000_000


def make_nmea_sentence(body):
    checksum = 0
    for character in body:
        checksum ^= ord(character)
    return f"!{body}*{checksum:02X}"


SENTENCE = make_nmea_sentence(
    "AIVDM,1,1,,A,15Muq?002>G?svP00<:O?vN60<0,0"
)
SECOND_SENTENCE = make_nmea_sentence(
    "AIVDM,1,1,,B,25Muq?002>G?svP00<:O?vN60<0,0"
)


def make_multipart_sentence(part, payload, *, sequence="7", total=2):
    return make_nmea_sentence(
        f"AIVDM,{total},{part},{sequence},A,{payload},0"
    )


def make_frame(
    payload,
    *,
    source_id=SOURCE_ID,
    alias_for_s=None,
    remote_ip=REMOTE_IP,
    assembler_key=ASSEMBLER_KEY,
):
    return IngressFrame(
        kind="udp",
        source_id=source_id,
        alias_for_s=alias_for_s,
        remote_ip=remote_ip,
        assembler_key=assembler_key,
        payload=payload.encode("utf-8"),
    )


def make_snapshot(
    generation=0,
    mode=DeduplicationMode.GLOBAL,
    target_ids=(),
):
    return ProcessingSnapshot(
        routing_generation=generation,
        deduplication_mode=mode,
        target_ids=target_ids,
    )


def make_processor(**overrides):
    arguments = {
        "station_id": "test_station",
        "wall_clock": lambda: WALL_TIME,
        "gid_generator": lambda _digits: "999999",
    }
    arguments.update(overrides)
    return PythonDataPlaneProcessor(**arguments)


def process_batch(processor, frame, snapshot):
    batch = processor.process(frame, snapshot)
    assert type(batch) is OutputBatch
    return batch


def process_outputs(processor, frame, snapshot):
    return process_batch(processor, frame, snapshot).outputs


def tag_block(content):
    checksum = 0
    for character in content:
        checksum ^= ord(character)
    return f"\\{content}*{checksum:02X}\\"


def leading_tag_content(message):
    if isinstance(message, bytes):
        message = message.decode("utf-8")
    end = message.find("\\", 1)
    return message[1:end].split("*", 1)[0]


def assert_fresh_multipart_output(
    outputs,
    first,
    second,
    *,
    gid="999999",
    timestamp=WALL_TIME,
):
    assert len(outputs) == 2
    assert outputs[0].message.endswith((first + "\r\n").encode("utf-8"))
    assert outputs[1].message.endswith((second + "\r\n").encode("utf-8"))
    assert leading_tag_content(outputs[0].message) == (
        f"c:{timestamp},s:192_0_2_10,g:1-2-{gid}"
    )
    assert leading_tag_content(outputs[1].message) == f"g:2-2-{gid}"


class MutableClock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now


class SequenceClock:
    def __init__(self, *values):
        self._values = iter(values)
        self.observations = []

    def __call__(self):
        value = next(self._values)
        self.observations.append(value)
        return value


class RecordingDeduplicator:
    def __init__(self):
        self.calls = []

    def is_unique(self, message, scope=None):
        self.calls.append((message, scope))
        return True


class RecordingSourceState(SourceState):
    def __init__(self):
        super().__init__()
        self.touched_s_values = []
        self.reset_calls = 0

    def touch_s(self, s_value):
        self.touched_s_values.append(s_value)
        super().touch_s(s_value)

    def reset(self):
        self.reset_calls += 1
        return super().reset()


def test_processor_satisfies_synchronous_protocol_without_asyncio_dependency():
    processor = make_processor()

    assert isinstance(processor, DataPlaneProcessor)
    assert not inspect.iscoroutinefunction(processor.process)
    assert not inspect.iscoroutinefunction(processor.reset)
    assert "asyncio" not in vars(python_data_plane_module)
    assert "RoutingTable" not in vars(python_data_plane_module)
    process_source = inspect.getsource(PythonDataPlaneProcessor.process)
    assert "routing_table" not in process_source
    assert ".match(" not in process_source


def test_exact_single_sentence_global_output_uses_snapshot_targets():
    processor = make_processor()
    snapshot = make_snapshot(target_ids=(3, 1))

    batch = process_batch(processor, make_frame(SENTENCE), snapshot)

    expected_message = (
        tag_block(f"c:{WALL_TIME},s:test_station")
        + SENTENCE
        + "\r\n"
    ).encode("utf-8")
    assert batch == OutputBatch(
        outputs=(
            ProcessorOutput(
                message=expected_message,
                target_ids=(3, 1),
            ),
        ),
    )


def test_processor_builds_once_per_emitted_sentence(monkeypatch):
    calls = []
    built_messages = []
    original_builder = python_data_plane_module.build_output_bytes

    def recording_builder(*args, **kwargs):
        calls.append((args, kwargs))
        message = original_builder(*args, **kwargs)
        built_messages.append(message)
        return message

    monkeypatch.setattr(
        python_data_plane_module,
        "build_output_bytes",
        recording_builder,
    )
    first = make_multipart_sentence(1, "first")
    second = make_multipart_sentence(2, "second")
    processor = make_processor()

    assert process_outputs(processor, make_frame(first), make_snapshot()) == ()
    outputs = process_outputs(processor, make_frame(second), make_snapshot())

    assert len(outputs) == 2
    assert len(calls) == 2
    assert outputs[0].message is built_messages[0]
    assert outputs[1].message is built_messages[1]
    assert outputs[0].message is not outputs[1].message


def test_dedup_suppressed_output_does_not_invoke_builder(monkeypatch):
    calls = []
    original_builder = python_data_plane_module.build_output_bytes

    def recording_builder(*args, **kwargs):
        calls.append((args, kwargs))
        return original_builder(*args, **kwargs)

    monkeypatch.setattr(
        python_data_plane_module,
        "build_output_bytes",
        recording_builder,
    )
    processor = make_processor(deduplicator=Deduplicator(clock=lambda: 0.0))
    frame = make_frame(SENTENCE)

    assert len(process_outputs(processor, frame, make_snapshot())) == 1
    calls.clear()
    assert process_outputs(processor, frame, make_snapshot()) == ()
    assert calls == []


def test_routed_no_match_does_not_invoke_builder(monkeypatch):
    def fail_builder(*_args, **_kwargs):
        raise AssertionError("builder must not be invoked")

    monkeypatch.setattr(
        python_data_plane_module,
        "build_output_bytes",
        fail_builder,
    )

    assert process_outputs(
        make_processor(),
        make_frame(SENTENCE),
        make_snapshot(mode=DeduplicationMode.PER_TARGET),
    ) == ()


def test_processor_delegates_framing_and_encoding_to_output_builder():
    process_source = inspect.getsource(PythonDataPlaneProcessor.process)

    assert "build_output_bytes(" in process_source
    assert "wrap_with_meta" not in process_source
    assert "\\r\\n" not in process_source
    assert ".encode(" not in process_source


def test_routed_output_preserves_numeric_target_order():
    outputs = process_outputs(
        make_processor(),
        make_frame(SENTENCE),
        make_snapshot(
            mode=DeduplicationMode.PER_TARGET,
            target_ids=(2, 0, 1),
        ),
    )

    assert len(outputs) == 1
    assert outputs[0].target_ids == (2, 0, 1)


def test_routed_frame_without_matching_route_returns_no_output():
    outputs = process_outputs(
        make_processor(),
        make_frame(SENTENCE),
        make_snapshot(mode=DeduplicationMode.PER_TARGET),
    )

    assert outputs == ()


def test_routed_deduplication_uses_numeric_target_ids_as_scopes():
    deduplicator = RecordingDeduplicator()
    outputs = process_outputs(
        make_processor(deduplicator=deduplicator),
        make_frame(SENTENCE),
        make_snapshot(
            mode=DeduplicationMode.PER_TARGET,
            target_ids=(7, 2),
        ),
    )

    assert len(outputs) == 1
    assert outputs[0].target_ids == (7, 2)
    assert [scope for _message, scope in deduplicator.calls] == [7, 2]


def test_routed_targets_are_independent_and_filter_in_snapshot_order():
    deduplicator = Deduplicator(clock=lambda: 0.0)
    processor = make_processor(deduplicator=deduplicator)
    frame = make_frame(SENTENCE)

    first_outputs = process_outputs(
        processor,
        frame,
        make_snapshot(
            mode=DeduplicationMode.PER_TARGET,
            target_ids=(5,),
        ),
    )
    second_outputs = process_outputs(
        processor,
        frame,
        make_snapshot(
            mode=DeduplicationMode.PER_TARGET,
            target_ids=(2, 5, 3),
        ),
    )

    assert first_outputs[0].target_ids == (5,)
    assert second_outputs[0].target_ids == (2, 3)
    assert deduplicator.stats().accepted == 3
    assert deduplicator.stats().duplicates == 1


def test_routed_no_target_message_is_not_admitted_to_global_deduplication():
    deduplicator = Deduplicator(clock=lambda: 0.0)
    processor = make_processor(deduplicator=deduplicator)
    frame = make_frame(SENTENCE)

    assert process_outputs(
        processor,
        frame,
        make_snapshot(mode=DeduplicationMode.PER_TARGET),
    ) == ()
    global_outputs = process_outputs(processor, frame, make_snapshot())

    assert len(global_outputs) == 1
    assert deduplicator.stats().accepted == 1
    assert deduplicator.stats().duplicates == 0


def test_global_mode_carries_snapshot_targets_and_uses_global_scope():
    deduplicator = RecordingDeduplicator()
    outputs = process_outputs(
        make_processor(deduplicator=deduplicator),
        make_frame(SENTENCE),
        make_snapshot(
            mode=DeduplicationMode.GLOBAL,
            target_ids=(0, 1),
        ),
    )

    assert len(outputs) == 1
    assert outputs[0].target_ids == (0, 1)
    assert [scope for _message, scope in deduplicator.calls] == [None]


def test_global_mode_with_empty_targets_still_builds_output(monkeypatch):
    calls = []
    original_builder = python_data_plane_module.build_output_bytes

    def recording_builder(*args, **kwargs):
        calls.append((args, kwargs))
        return original_builder(*args, **kwargs)

    monkeypatch.setattr(
        python_data_plane_module,
        "build_output_bytes",
        recording_builder,
    )

    outputs = process_outputs(
        make_processor(),
        make_frame(SENTENCE),
        make_snapshot(
            mode=DeduplicationMode.GLOBAL,
            target_ids=(),
        ),
    )

    assert len(outputs) == 1
    assert outputs[0].target_ids == ()
    assert len(calls) == 1


def test_multiple_sentences_reuse_the_snapshot_numeric_targets():
    frame = make_frame(SENTENCE + SECOND_SENTENCE)

    outputs = process_outputs(
        make_processor(),
        frame,
        make_snapshot(
            mode=DeduplicationMode.PER_TARGET,
            target_ids=(3, 1),
        ),
    )

    assert len(outputs) == 2
    assert all(output.target_ids == (3, 1) for output in outputs)


def test_target_only_snapshot_with_no_sentence_returns_no_output():
    outputs = process_outputs(
        make_processor(),
        make_frame("not an AIS sentence"),
        make_snapshot(
            mode=DeduplicationMode.PER_TARGET,
            target_ids=(4,),
        ),
    )

    assert outputs == ()


def test_single_sentence_deduplication_is_retained_across_calls():
    deduplicator = Deduplicator(clock=lambda: 0.0)
    processor = make_processor(deduplicator=deduplicator)
    frame = make_frame(SENTENCE)

    first_outputs = process_outputs(processor, frame, make_snapshot())
    duplicate_outputs = process_outputs(processor, frame, make_snapshot())

    assert len(first_outputs) == 1
    assert duplicate_outputs == ()
    assert deduplicator.stats().accepted == 1
    assert deduplicator.stats().duplicates == 1


def test_default_processors_do_not_share_deduplication_state():
    first_processor = PythonDataPlaneProcessor()
    second_processor = PythonDataPlaneProcessor()
    frame = make_frame(SENTENCE)
    snapshot = make_snapshot()

    assert len(process_outputs(first_processor, frame, snapshot)) == 1
    assert len(process_outputs(second_processor, frame, snapshot)) == 1
    assert process_outputs(first_processor, frame, snapshot) == ()
    assert process_outputs(second_processor, frame, snapshot) == ()
    assert first_processor._deduplicator is not second_processor._deduplicator


def test_default_processors_cannot_complete_each_others_multipart_groups():
    first_processor = PythonDataPlaneProcessor()
    second_processor = PythonDataPlaneProcessor()
    first = make_multipart_sentence(1, "first")
    second = make_multipart_sentence(2, "second")
    snapshot = make_snapshot()

    assert process_outputs(
        first_processor,
        make_frame(first),
        snapshot,
    ) == ()
    assert process_outputs(
        second_processor,
        make_frame(second),
        snapshot,
    ) == ()
    assert first_processor._assembler.stats().current_fragments == 1
    assert second_processor._assembler.stats().current_fragments == 1

    assert len(
        process_outputs(
            first_processor,
            make_frame(second),
            snapshot,
        )
    ) == 2
    assert len(
        process_outputs(
            second_processor,
            make_frame(first),
            snapshot,
        )
    ) == 2


def test_default_processors_do_not_share_source_state_activity():
    first_processor = PythonDataPlaneProcessor()
    second_processor = PythonDataPlaneProcessor()

    assert len(
        process_outputs(
            first_processor,
            make_frame(SENTENCE),
            make_snapshot(),
        )
    ) == 1

    first_source_state = first_processor._source_state
    second_source_state = second_processor._source_state
    assert isinstance(first_source_state, SourceState)
    assert isinstance(second_source_state, SourceState)
    assert first_source_state is not second_source_state
    assert first_source_state._s_cache.contains("mixstation_1")
    assert "mixstation_1" in first_source_state._per_s_state
    assert not second_source_state._s_cache.contains("mixstation_1")
    assert "mixstation_1" not in second_source_state._per_s_state


def test_assembler_state_is_retained_across_process_calls():
    assembler = AIVDMAssembler(clock=lambda: 0.0)
    processor = make_processor(assembler=assembler)
    first = make_multipart_sentence(1, "first")
    second = make_multipart_sentence(2, "second")

    assert process_outputs(processor, make_frame(first), make_snapshot()) == ()
    assert assembler.stats().current_groups == 1

    outputs = process_outputs(processor, make_frame(second), make_snapshot())

    assert len(outputs) == 2
    assert assembler.stats().current_groups == 0
    assert assembler.stats().completed == 1


def test_completed_multipart_group_is_deduplicated_atomically():
    deduplicator = Deduplicator(clock=lambda: 0.0)
    processor = make_processor(deduplicator=deduplicator)
    first = make_multipart_sentence(1, "first")
    second = make_multipart_sentence(2, "second")
    snapshot = make_snapshot()

    assert process_outputs(processor, make_frame(first), snapshot) == ()
    assert len(process_outputs(processor, make_frame(second), snapshot)) == 2
    assert process_outputs(processor, make_frame(first), snapshot) == ()
    assert process_outputs(processor, make_frame(second), snapshot) == ()
    assert deduplicator.stats().accepted == 1
    assert deduplicator.stats().duplicates == 1


def test_conflict_discards_all_metadata_for_invalidated_generation():
    processor = make_processor(station_id="")
    old_first = make_multipart_sentence(1, "old-first")
    conflicting_first = make_multipart_sentence(1, "conflict")
    fresh_second = make_multipart_sentence(2, "fresh-second")
    fresh_first = make_multipart_sentence(1, "fresh-first")
    snapshot = make_snapshot()

    assert process_outputs(
        processor,
        make_frame(f"\\s:stale,c:111,g:1-2-111*00\\{old_first}"),
        snapshot,
    ) == ()
    assert process_outputs(
        processor,
        make_frame(conflicting_first),
        snapshot,
    ) == ()
    assert process_outputs(processor, make_frame(fresh_second), snapshot) == ()
    outputs = process_outputs(processor, make_frame(fresh_first), snapshot)

    assert_fresh_multipart_output(outputs, fresh_first, fresh_second)


def test_expiry_discards_old_metadata_before_fresh_arrival_is_observed():
    assembler_clock = MutableClock()
    assembler = AIVDMAssembler(timeout=1.0, clock=assembler_clock)
    processor = make_processor(assembler=assembler, station_id="")
    old_first = make_multipart_sentence(1, "old-first")
    fresh_second = make_multipart_sentence(2, "fresh-second")
    fresh_first = make_multipart_sentence(1, "fresh-first")
    snapshot = make_snapshot()

    assert process_outputs(
        processor,
        make_frame(f"\\s:stale,c:111,g:1-2-111*00\\{old_first}"),
        snapshot,
    ) == ()
    assembler_clock.now = 1.0
    assert process_outputs(processor, make_frame(fresh_second), snapshot) == ()
    outputs = process_outputs(processor, make_frame(fresh_first), snapshot)

    assert assembler.stats().expired == 1
    assert_fresh_multipart_output(outputs, fresh_first, fresh_second)


def test_capacity_eviction_discards_victim_metadata():
    assembler = AIVDMAssembler(
        clock=lambda: 0.0,
        max_pending_groups=1,
    )
    processor = make_processor(assembler=assembler, station_id="")
    a_old_first = make_multipart_sentence(
        1,
        "a-old-first",
        sequence="1",
    )
    b_first = make_multipart_sentence(1, "b-first", sequence="2")
    b_second = make_multipart_sentence(2, "b-second", sequence="2")
    a_fresh_second = make_multipart_sentence(
        2,
        "a-fresh-second",
        sequence="1",
    )
    a_fresh_first = make_multipart_sentence(
        1,
        "a-fresh-first",
        sequence="1",
    )
    snapshot = make_snapshot()

    assert process_outputs(
        processor,
        make_frame(
            f"\\s:stale,c:111,g:1-2-111*00\\{a_old_first}"
        ),
        snapshot,
    ) == ()
    assert process_outputs(processor, make_frame(b_first), snapshot) == ()
    assert len(
        process_outputs(processor, make_frame(b_second), snapshot)
    ) == 2
    assert process_outputs(
        processor,
        make_frame(a_fresh_second),
        snapshot,
    ) == ()
    outputs = process_outputs(processor, make_frame(a_fresh_first), snapshot)

    assert assembler.stats().capacity_evicted == 1
    assert_fresh_multipart_output(
        outputs,
        a_fresh_first,
        a_fresh_second,
    )


def test_normal_completion_consumes_multipart_metadata():
    processor = make_processor(station_id="")
    old_first = make_multipart_sentence(1, "old-first")
    old_second = make_multipart_sentence(2, "old-second")
    fresh_second = make_multipart_sentence(2, "fresh-second")
    fresh_first = make_multipart_sentence(1, "fresh-first")
    snapshot = make_snapshot()

    assert process_outputs(
        processor,
        make_frame(f"\\s:stale,c:111,g:1-2-111*00\\{old_first}"),
        snapshot,
    ) == ()
    assert len(
        process_outputs(processor, make_frame(old_second), snapshot)
    ) == 2
    assert process_outputs(processor, make_frame(fresh_second), snapshot) == ()
    outputs = process_outputs(processor, make_frame(fresh_first), snapshot)

    assert_fresh_multipart_output(outputs, fresh_first, fresh_second)


def test_no_route_completion_consumes_multipart_metadata():
    processor = make_processor(station_id="")
    no_route_snapshot = make_snapshot(
        mode=DeduplicationMode.PER_TARGET,
        target_ids=(),
    )
    routed_snapshot = make_snapshot(
        mode=DeduplicationMode.PER_TARGET,
        target_ids=(6,),
    )
    old_first = make_multipart_sentence(1, "old-first")
    old_second = make_multipart_sentence(2, "old-second")
    fresh_second = make_multipart_sentence(2, "fresh-second")
    fresh_first = make_multipart_sentence(1, "fresh-first")

    assert process_outputs(
        processor,
        make_frame(f"\\s:stale,c:111,g:1-2-111*00\\{old_first}"),
        no_route_snapshot,
    ) == ()
    assert (
        process_outputs(
            processor,
            make_frame(old_second),
            no_route_snapshot,
        )
        == ()
    )
    assert process_outputs(
        processor,
        make_frame(fresh_second),
        routed_snapshot,
    ) == ()
    outputs = process_outputs(
        processor,
        make_frame(fresh_first),
        routed_snapshot,
    )

    assert all(output.target_ids == (6,) for output in outputs)
    assert_fresh_multipart_output(outputs, fresh_first, fresh_second)


def test_dedup_suppressed_completion_consumes_multipart_metadata():
    processor = make_processor(station_id="")
    first = make_multipart_sentence(1, "duplicate-first")
    second = make_multipart_sentence(2, "duplicate-second")
    fresh_second = make_multipart_sentence(2, "fresh-second")
    fresh_first = make_multipart_sentence(1, "fresh-first")
    snapshot = make_snapshot()

    assert process_outputs(processor, make_frame(first), snapshot) == ()
    assert len(process_outputs(processor, make_frame(second), snapshot)) == 2

    assert process_outputs(
        processor,
        make_frame(f"\\s:stale,c:111,g:1-2-111*00\\{first}"),
        snapshot,
    ) == ()
    assert process_outputs(processor, make_frame(second), snapshot) == ()

    assert process_outputs(processor, make_frame(fresh_second), snapshot) == ()
    outputs = process_outputs(processor, make_frame(fresh_first), snapshot)

    assert_fresh_multipart_output(outputs, fresh_first, fresh_second)


def test_injected_wall_clock_preserves_single_and_multipart_c_zero_asymmetry():
    single_clock = SequenceClock(1234)
    single_processor = make_processor(wall_clock=single_clock)

    single_outputs = process_outputs(
        single_processor,
        make_frame(f"\\c:0*00\\{SENTENCE}"),
        make_snapshot(),
    )

    assert leading_tag_content(single_outputs[0].message) == (
        "c:1234,s:test_station"
    )
    assert single_clock.observations == [1234]

    multipart_clock = SequenceClock(9999)
    multipart_processor = make_processor(wall_clock=multipart_clock)
    first = make_multipart_sentence(1, "zero-first")
    second = make_multipart_sentence(2, "zero-second")

    assert process_outputs(
        multipart_processor,
        make_frame(f"\\c:0*00\\{first}"),
        make_snapshot(),
    ) == ()
    multipart_outputs = process_outputs(
        multipart_processor,
        make_frame(f"\\c:111*00\\{second}"),
        make_snapshot(),
    )

    assert leading_tag_content(multipart_outputs[0].message).startswith(
        "c:0,s:test_station,g:1-2-"
    )
    assert multipart_clock.observations == []


def test_multipart_continuation_retains_wall_clock_observation():
    wall_clock = SequenceClock(1234, 5678)
    processor = make_processor(wall_clock=wall_clock)
    first = make_multipart_sentence(1, "first")
    second = make_multipart_sentence(2, "second")

    assert process_outputs(processor, make_frame(first), make_snapshot()) == ()
    outputs = process_outputs(processor, make_frame(second), make_snapshot())

    assert leading_tag_content(outputs[0].message).startswith(
        "c:1234,s:test_station,g:1-2-"
    )
    assert leading_tag_content(outputs[1].message).startswith("g:2-2-")
    assert wall_clock.observations == [1234, 5678]


def test_injected_gid_generator_receives_digits_once_and_reuses_gid():
    generator_calls = []

    def generate_gid(digits):
        generator_calls.append(digits)
        return "654321"

    processor = make_processor(
        gid_digits=6,
        gid_generator=generate_gid,
    )
    first = make_multipart_sentence(1, "first")
    second = make_multipart_sentence(2, "second")

    assert process_outputs(processor, make_frame(first), make_snapshot()) == ()
    outputs = process_outputs(processor, make_frame(second), make_snapshot())

    assert generator_calls == [6]
    assert leading_tag_content(outputs[0].message).endswith(
        "g:1-2-654321"
    )
    assert leading_tag_content(outputs[1].message) == "g:2-2-654321"


def test_injected_source_state_receives_each_emitted_sentence_only():
    source_state = RecordingSourceState()
    processor = make_processor(source_state=source_state)
    first = make_multipart_sentence(1, "first")
    second = make_multipart_sentence(2, "second")
    snapshot = make_snapshot()

    assert process_outputs(processor, make_frame(first), snapshot) == ()
    outputs = process_outputs(processor, make_frame(second), snapshot)

    assert len(outputs) == 2
    assert processor._source_state is source_state
    assert source_state.touched_s_values == [
        "test_station",
        "test_station",
    ]
    report = processor.reset()
    assert report.source_entries_discarded == 1
    assert source_state.reset_calls == 1
    assert source_state._per_s_state == {}


def test_callback_only_source_state_compatibility_is_removed():
    parameters = inspect.signature(PythonDataPlaneProcessor).parameters

    assert "touch_s_operation" not in parameters
    assert "_TouchSOperationSourceState" not in vars(
        python_data_plane_module
    )


def test_reset_orders_owned_state_and_reports_orphan_contexts():
    events = []

    class ResetAssembler:
        def reset(self):
            events.append("assembler")
            return ("group-a", "group-b")

    class ResetDeduplicator:
        def reset(self):
            events.append("deduplicator")
            return 3

    class ResetSourceState:
        def reset(self):
            events.append("source-state")
            return 4

    class ResetContext(dict):
        def __init__(self, name, count):
            super().__init__((index, index) for index in range(count))
            self._name = name

        def clear(self):
            events.append(self._name)
            super().clear()

    processor = make_processor(
        assembler=ResetAssembler(),
        deduplicator=ResetDeduplicator(),
        source_state=ResetSourceState(),
    )
    processor._multipart_s_ctx = ResetContext("multipart-s", 5)
    processor._multipart_c_ctx = ResetContext("multipart-c", 6)
    processor._multipart_gid_ctx = ResetContext("multipart-gid", 7)

    report = processor.reset()

    assert report == ProcessorResetReport(
        assembler_groups_discarded=2,
        dedup_entries_discarded=3,
        source_entries_discarded=4,
        multipart_s_contexts_discarded=5,
        multipart_c_contexts_discarded=6,
        multipart_gid_contexts_discarded=7,
    )
    assert events == [
        "assembler",
        "deduplicator",
        "source-state",
        "multipart-s",
        "multipart-c",
        "multipart-gid",
    ]
    assert processor._multipart_s_ctx == {}
    assert processor._multipart_c_ctx == {}
    assert processor._multipart_gid_ctx == {}


def test_reset_preserves_owned_component_identity_config_and_counters():
    assembler_clock = MutableClock()
    dedup_clock = MutableClock()
    assembler = AIVDMAssembler(
        timeout=7.5,
        clock=assembler_clock,
        max_fragments_per_group=4,
        max_pending_groups=3,
    )
    deduplicator = Deduplicator(
        ttl=30,
        clock=dedup_clock,
        max_entries=4,
    )
    source_state = SourceState(
        ttl_seconds=60,
        max_entries=5,
        sweep_every_seconds=2,
        ops_per_sweep=7,
    )
    wall_clock_observations = []
    gid_digits_observed = []

    def wall_clock():
        wall_clock_observations.append(WALL_TIME)
        return WALL_TIME

    def gid_generator(digits):
        gid_digits_observed.append(digits)
        return "777777"

    processor = make_processor(
        station_id="",
        always_tag_single=True,
        gid_digits=6,
        assembler=assembler,
        deduplicator=deduplicator,
        source_state=source_state,
        wall_clock=wall_clock,
        gid_generator=gid_generator,
    )
    processor_identity = id(processor)
    processing_config = processor._config
    snapshot = make_snapshot()
    pending_first = make_multipart_sentence(
        1,
        "pending-first",
        sequence="4",
    )
    pending_second = make_multipart_sentence(
        2,
        "pending-second",
        sequence="4",
    )
    tagged_pending_first = (
        "\\s:pending,c:123,g:1-2-444*00\\" + pending_first
    )

    assert len(
        process_outputs(
            processor,
            make_frame(SENTENCE, alias_for_s="source-one"),
            snapshot,
        )
    ) == 1
    assert len(
        process_outputs(
            processor,
            make_frame(SECOND_SENTENCE, alias_for_s="source-two"),
            snapshot,
        )
    ) == 1
    assert process_outputs(
        processor,
        make_frame(SENTENCE, alias_for_s="source-one"),
        snapshot,
    ) == ()
    assert process_outputs(
        processor,
        make_frame(tagged_pending_first),
        snapshot,
    ) == ()

    report = processor.reset()

    assert report == ProcessorResetReport(
        assembler_groups_discarded=1,
        dedup_entries_discarded=2,
        source_entries_discarded=2,
        multipart_s_contexts_discarded=1,
        multipart_c_contexts_discarded=1,
        multipart_gid_contexts_discarded=1,
    )
    assert id(processor) == processor_identity
    assert processor._config is processing_config
    assert processor._assembler is assembler
    assert processor._deduplicator is deduplicator
    assert processor._source_state is source_state
    assert processor._wall_clock is wall_clock
    assert processor._gid_generator is gid_generator
    assert assembler.timeout == 7.5
    assert assembler._clock is assembler_clock
    assert assembler.max_fragments_per_group == 4
    assert assembler.max_pending_groups == 3
    assembler_stats = assembler.stats()
    assert assembler_stats.pending == 1
    assert assembler_stats.reset_discarded == 1
    assert assembler_stats.resets == 1
    assert assembler_stats.current_groups == 0
    assert assembler_stats.current_fragments == 0
    assert assembler_stats.peak_groups == 1
    assert assembler_stats.peak_fragments == 1
    assert deduplicator.ttl == 30
    assert deduplicator._clock is dedup_clock
    assert deduplicator.max_entries == 4
    dedup_stats = deduplicator.stats()
    assert dedup_stats.accepted == 2
    assert dedup_stats.duplicates == 1
    assert dedup_stats.resets == 1
    assert dedup_stats.current_entries == 0
    assert dedup_stats.peak_entries == 2
    assert source_state._s_cache._ttl_ns == 60_000_000_000
    assert source_state._s_cache._max_entries == 5
    assert source_state._s_cache._sweep_every_ns == 2_000_000_000
    assert source_state._s_cache._ops_per_sweep == 7
    assert source_state._per_s_state == {}
    assert processor._multipart_s_ctx == {}
    assert processor._multipart_c_ctx == {}
    assert processor._multipart_gid_ctx == {}

    # Deduplication admits the pre-reset sentence again using preserved
    # station/TAG configuration and deterministic helpers.
    wall_clock_calls_before_reuse = len(wall_clock_observations)
    gid_calls_before_reuse = len(gid_digits_observed)
    reused_single_outputs = process_outputs(
        processor,
        make_frame(SENTENCE, alias_for_s="source-one"),
        snapshot,
    )
    assert len(reused_single_outputs) == 1
    assert leading_tag_content(reused_single_outputs[0].message) == (
        f"c:{WALL_TIME},s:source_one,g:1-1-777777"
    )
    assert len(wall_clock_observations) == wall_clock_calls_before_reuse + 1
    assert len(gid_digits_observed) == gid_calls_before_reuse + 1
    assert gid_digits_observed[-1] == 6
    # A pre-reset first fragment cannot combine with this continuation.
    assert process_outputs(
        processor,
        make_frame(pending_second),
        snapshot,
    ) == ()
    assert len(
        process_outputs(
            processor,
            make_frame(tagged_pending_first),
            snapshot,
        )
    ) == 2


def test_repeated_empty_reset_reports_zero_and_advances_owner_counters():
    processor = make_processor()
    empty_report = ProcessorResetReport(
        assembler_groups_discarded=0,
        dedup_entries_discarded=0,
        source_entries_discarded=0,
        multipart_s_contexts_discarded=0,
        multipart_c_contexts_discarded=0,
        multipart_gid_contexts_discarded=0,
    )

    assert processor.reset() == empty_report
    assert processor.reset() == empty_report
    assert processor._assembler.stats().resets == 2
    assert processor._assembler.stats().reset_discarded == 0
    assert processor._deduplicator.stats().resets == 2


@pytest.mark.parametrize(
    ("failing_owner", "expected_calls"),
    [
        ("assembler", ["assembler"]),
        ("deduplicator", ["assembler", "deduplicator"]),
        (
            "source-state",
            ["assembler", "deduplicator", "source-state"],
        ),
    ],
)
def test_reset_owner_failure_propagates_without_running_later_owners(
    failing_owner,
    expected_calls,
):
    calls = []

    class FailingAssembler:
        def reset(self):
            calls.append("assembler")
            if failing_owner == "assembler":
                raise RuntimeError("assembler reset failed")
            return ()

    class FailingDeduplicator:
        def reset(self):
            calls.append("deduplicator")
            if failing_owner == "deduplicator":
                raise RuntimeError("deduplicator reset failed")
            return 0

    class FailingSourceState:
        def reset(self):
            calls.append("source-state")
            if failing_owner == "source-state":
                raise RuntimeError("source-state reset failed")
            return 0

    processor = make_processor(
        assembler=FailingAssembler(),
        deduplicator=FailingDeduplicator(),
        source_state=FailingSourceState(),
    )
    processor._multipart_s_ctx["orphan-s"] = "value"
    processor._multipart_c_ctx["orphan-c"] = 1
    processor._multipart_gid_ctx["orphan-gid"] = frozenset(("1",))

    with pytest.raises(RuntimeError, match=f"{failing_owner} reset failed"):
        processor.reset()

    assert calls == expected_calls
    assert processor._multipart_s_ctx == {"orphan-s": "value"}
    assert processor._multipart_c_ctx == {"orphan-c": 1}
    assert processor._multipart_gid_ctx == {
        "orphan-gid": frozenset(("1",))
    }


def test_routing_generation_change_does_not_reset_deduplication():
    deduplicator = Deduplicator(clock=lambda: 0.0)
    processor = make_processor(deduplicator=deduplicator)
    frame = make_frame(SENTENCE)

    first_snapshot = make_snapshot(
        generation=1,
        mode=DeduplicationMode.PER_TARGET,
        target_ids=(3,),
    )
    second_snapshot = make_snapshot(
        generation=2,
        mode=DeduplicationMode.PER_TARGET,
        target_ids=(3,),
    )

    assert len(process_outputs(processor, frame, first_snapshot)) == 1
    assert process_outputs(processor, frame, second_snapshot) == ()

    stats = deduplicator.stats()
    assert stats.duplicates == 1
    assert stats.resets == 0


def test_multipart_crosses_generations_and_uses_completion_numeric_targets():
    assembler = AIVDMAssembler(clock=lambda: 0.0)
    processor = make_processor(
        assembler=assembler,
        station_id="",
    )
    first = make_multipart_sentence(1, "first")
    second = make_multipart_sentence(2, "second")
    tagged_first = (
        "\\c:123,s:gen_source,g:1-2-444*00\\"
        + first
    )
    start_snapshot = make_snapshot(
        generation=1,
        mode=DeduplicationMode.PER_TARGET,
        target_ids=(4,),
    )
    completion_snapshot = make_snapshot(
        generation=2,
        mode=DeduplicationMode.PER_TARGET,
        target_ids=(8, 2),
    )

    assert process_outputs(
        processor,
        make_frame(tagged_first),
        start_snapshot,
    ) == ()
    outputs = process_outputs(
        processor,
        make_frame(second),
        completion_snapshot,
    )

    assert len(outputs) == 2
    assert leading_tag_content(outputs[0].message) == (
        "c:123,s:gen_source,g:1-2-444"
    )
    assert leading_tag_content(outputs[1].message) == "g:2-2-444"
    assert all(
        output.target_ids == (8, 2)
        for output in outputs
    )
    assert assembler.stats().completed == 1
    assert assembler.stats().resets == 0
