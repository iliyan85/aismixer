import inspect

import core.python_data_plane as python_data_plane_module
from assembler import AIVDMAssembler
from core.data_plane import (
    DataPlaneProcessor,
    DeduplicationMode,
    ProcessingSnapshot,
    ProcessorOutput,
    RoutingDisposition,
)
from core.ingress_frame import IngressFrame
from core.python_data_plane import PythonDataPlaneProcessor
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
        "touch_s_operation": lambda _s_value: None,
    }
    arguments.update(overrides)
    return PythonDataPlaneProcessor(**arguments)


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


def test_processor_satisfies_synchronous_protocol_without_asyncio_dependency():
    processor = make_processor()

    assert isinstance(processor, DataPlaneProcessor)
    assert not inspect.iscoroutinefunction(processor.process)
    assert "asyncio" not in vars(python_data_plane_module)
    assert "RoutingTable" not in vars(python_data_plane_module)
    process_source = inspect.getsource(PythonDataPlaneProcessor.process)
    assert "routing_table" not in process_source
    assert ".match(" not in process_source


def test_exact_single_sentence_legacy_output():
    processor = make_processor()

    outputs = processor.process(make_frame(SENTENCE), make_snapshot())

    expected_message = (
        tag_block(f"c:{WALL_TIME},s:test_station")
        + SENTENCE
        + "\r\n"
    ).encode("utf-8")
    assert outputs == (
        ProcessorOutput(
            message=expected_message,
            disposition=RoutingDisposition.LEGACY_BROADCAST,
            target_ids=(),
        ),
    )


def test_processor_builds_once_per_emitted_sentence(monkeypatch):
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
    first = make_multipart_sentence(1, "first")
    second = make_multipart_sentence(2, "second")
    processor = make_processor()

    assert processor.process(make_frame(first), make_snapshot()) == ()
    outputs = processor.process(make_frame(second), make_snapshot())

    assert len(outputs) == 2
    assert len(calls) == 2


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

    assert len(processor.process(frame, make_snapshot())) == 1
    calls.clear()
    assert processor.process(frame, make_snapshot()) == ()
    assert calls == []


def test_routed_no_match_does_not_invoke_builder(monkeypatch):
    def fail_builder(*_args, **_kwargs):
        raise AssertionError("builder must not be invoked")

    monkeypatch.setattr(
        python_data_plane_module,
        "build_output_bytes",
        fail_builder,
    )

    assert make_processor().process(
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
    outputs = make_processor().process(
        make_frame(SENTENCE),
        make_snapshot(
            mode=DeduplicationMode.PER_TARGET,
            target_ids=(2, 0, 1),
        ),
    )

    assert len(outputs) == 1
    assert outputs[0].disposition is RoutingDisposition.TARGETED
    assert outputs[0].target_ids == (2, 0, 1)


def test_routed_frame_without_matching_route_returns_no_output():
    outputs = make_processor().process(
        make_frame(SENTENCE),
        make_snapshot(mode=DeduplicationMode.PER_TARGET),
    )

    assert outputs == ()


def test_routed_deduplication_uses_numeric_target_ids_as_scopes():
    deduplicator = RecordingDeduplicator()
    outputs = make_processor(deduplicator=deduplicator).process(
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

    first_outputs = processor.process(
        frame,
        make_snapshot(
            mode=DeduplicationMode.PER_TARGET,
            target_ids=(5,),
        ),
    )
    second_outputs = processor.process(
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

    assert processor.process(
        frame,
        make_snapshot(mode=DeduplicationMode.PER_TARGET),
    ) == ()
    global_outputs = processor.process(frame, make_snapshot())

    assert len(global_outputs) == 1
    assert global_outputs[0].disposition is RoutingDisposition.LEGACY_BROADCAST
    assert deduplicator.stats().accepted == 1
    assert deduplicator.stats().duplicates == 0


def test_global_mode_ignores_snapshot_targets_and_uses_global_scope():
    deduplicator = RecordingDeduplicator()
    outputs = make_processor(deduplicator=deduplicator).process(
        make_frame(SENTENCE),
        make_snapshot(
            mode=DeduplicationMode.GLOBAL,
            target_ids=(0, 1),
        ),
    )

    assert len(outputs) == 1
    assert outputs[0].disposition is RoutingDisposition.LEGACY_BROADCAST
    assert outputs[0].target_ids == ()
    assert [scope for _message, scope in deduplicator.calls] == [None]


def test_multiple_sentences_reuse_the_snapshot_numeric_targets():
    frame = make_frame(SENTENCE + SECOND_SENTENCE)

    outputs = make_processor().process(
        frame,
        make_snapshot(
            mode=DeduplicationMode.PER_TARGET,
            target_ids=(3, 1),
        ),
    )

    assert len(outputs) == 2
    assert all(output.target_ids == (3, 1) for output in outputs)


def test_target_only_snapshot_with_no_sentence_returns_no_output():
    outputs = make_processor().process(
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

    first_outputs = processor.process(frame, make_snapshot())
    duplicate_outputs = processor.process(frame, make_snapshot())

    assert len(first_outputs) == 1
    assert duplicate_outputs == ()
    assert deduplicator.stats().accepted == 1
    assert deduplicator.stats().duplicates == 1


def test_assembler_state_is_retained_across_process_calls():
    assembler = AIVDMAssembler(clock=lambda: 0.0)
    processor = make_processor(assembler=assembler)
    first = make_multipart_sentence(1, "first")
    second = make_multipart_sentence(2, "second")

    assert processor.process(make_frame(first), make_snapshot()) == ()
    assert assembler.stats().current_groups == 1

    outputs = processor.process(make_frame(second), make_snapshot())

    assert len(outputs) == 2
    assert assembler.stats().current_groups == 0
    assert assembler.stats().completed == 1


def test_completed_multipart_group_is_deduplicated_atomically():
    deduplicator = Deduplicator(clock=lambda: 0.0)
    processor = make_processor(deduplicator=deduplicator)
    first = make_multipart_sentence(1, "first")
    second = make_multipart_sentence(2, "second")
    snapshot = make_snapshot()

    assert processor.process(make_frame(first), snapshot) == ()
    assert len(processor.process(make_frame(second), snapshot)) == 2
    assert processor.process(make_frame(first), snapshot) == ()
    assert processor.process(make_frame(second), snapshot) == ()
    assert deduplicator.stats().accepted == 1
    assert deduplicator.stats().duplicates == 1


def test_conflict_discards_all_metadata_for_invalidated_generation():
    processor = make_processor(station_id="")
    old_first = make_multipart_sentence(1, "old-first")
    conflicting_first = make_multipart_sentence(1, "conflict")
    fresh_second = make_multipart_sentence(2, "fresh-second")
    fresh_first = make_multipart_sentence(1, "fresh-first")
    snapshot = make_snapshot()

    assert processor.process(
        make_frame(f"\\s:stale,c:111,g:1-2-111*00\\{old_first}"),
        snapshot,
    ) == ()
    assert processor.process(make_frame(conflicting_first), snapshot) == ()
    assert processor.process(make_frame(fresh_second), snapshot) == ()
    outputs = processor.process(make_frame(fresh_first), snapshot)

    assert_fresh_multipart_output(outputs, fresh_first, fresh_second)


def test_expiry_discards_old_metadata_before_fresh_arrival_is_observed():
    assembler_clock = MutableClock()
    assembler = AIVDMAssembler(timeout=1.0, clock=assembler_clock)
    processor = make_processor(assembler=assembler, station_id="")
    old_first = make_multipart_sentence(1, "old-first")
    fresh_second = make_multipart_sentence(2, "fresh-second")
    fresh_first = make_multipart_sentence(1, "fresh-first")
    snapshot = make_snapshot()

    assert processor.process(
        make_frame(f"\\s:stale,c:111,g:1-2-111*00\\{old_first}"),
        snapshot,
    ) == ()
    assembler_clock.now = 1.0
    assert processor.process(make_frame(fresh_second), snapshot) == ()
    outputs = processor.process(make_frame(fresh_first), snapshot)

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

    assert processor.process(
        make_frame(
            f"\\s:stale,c:111,g:1-2-111*00\\{a_old_first}"
        ),
        snapshot,
    ) == ()
    assert processor.process(make_frame(b_first), snapshot) == ()
    assert len(processor.process(make_frame(b_second), snapshot)) == 2
    assert processor.process(make_frame(a_fresh_second), snapshot) == ()
    outputs = processor.process(make_frame(a_fresh_first), snapshot)

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

    assert processor.process(
        make_frame(f"\\s:stale,c:111,g:1-2-111*00\\{old_first}"),
        snapshot,
    ) == ()
    assert len(processor.process(make_frame(old_second), snapshot)) == 2
    assert processor.process(make_frame(fresh_second), snapshot) == ()
    outputs = processor.process(make_frame(fresh_first), snapshot)

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

    assert processor.process(
        make_frame(f"\\s:stale,c:111,g:1-2-111*00\\{old_first}"),
        no_route_snapshot,
    ) == ()
    assert (
        processor.process(make_frame(old_second), no_route_snapshot)
        == ()
    )
    assert processor.process(make_frame(fresh_second), routed_snapshot) == ()
    outputs = processor.process(make_frame(fresh_first), routed_snapshot)

    assert all(
        output.disposition is RoutingDisposition.TARGETED
        for output in outputs
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

    assert processor.process(make_frame(first), snapshot) == ()
    assert len(processor.process(make_frame(second), snapshot)) == 2

    assert processor.process(
        make_frame(f"\\s:stale,c:111,g:1-2-111*00\\{first}"),
        snapshot,
    ) == ()
    assert processor.process(make_frame(second), snapshot) == ()

    assert processor.process(make_frame(fresh_second), snapshot) == ()
    outputs = processor.process(make_frame(fresh_first), snapshot)

    assert_fresh_multipart_output(outputs, fresh_first, fresh_second)


def test_injected_wall_clock_preserves_single_and_multipart_c_zero_asymmetry():
    single_clock = SequenceClock(1234)
    single_processor = make_processor(wall_clock=single_clock)

    single_outputs = single_processor.process(
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

    assert multipart_processor.process(
        make_frame(f"\\c:0*00\\{first}"),
        make_snapshot(),
    ) == ()
    multipart_outputs = multipart_processor.process(
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

    assert processor.process(make_frame(first), make_snapshot()) == ()
    outputs = processor.process(make_frame(second), make_snapshot())

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

    assert processor.process(make_frame(first), make_snapshot()) == ()
    outputs = processor.process(make_frame(second), make_snapshot())

    assert generator_calls == [6]
    assert leading_tag_content(outputs[0].message).endswith(
        "g:1-2-654321"
    )
    assert leading_tag_content(outputs[1].message) == "g:2-2-654321"


def test_injected_touch_s_operation_observes_each_emitted_sentence_only():
    touched_s_values = []
    processor = make_processor(
        touch_s_operation=touched_s_values.append,
    )
    first = make_multipart_sentence(1, "first")
    second = make_multipart_sentence(2, "second")
    snapshot = make_snapshot()

    assert processor.process(make_frame(first), snapshot) == ()
    outputs = processor.process(make_frame(second), snapshot)
    assert len(outputs) == 2
    assert touched_s_values == ["test_station", "test_station"]

    assert processor.process(make_frame(first), snapshot) == ()
    assert processor.process(make_frame(second), snapshot) == ()
    assert touched_s_values == ["test_station", "test_station"]


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

    assert len(processor.process(frame, first_snapshot)) == 1
    assert processor.process(frame, second_snapshot) == ()

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

    assert processor.process(make_frame(tagged_first), start_snapshot) == ()
    outputs = processor.process(make_frame(second), completion_snapshot)

    assert len(outputs) == 2
    assert leading_tag_content(outputs[0].message) == (
        "c:123,s:gen_source,g:1-2-444"
    )
    assert leading_tag_content(outputs[1].message) == "g:2-2-444"
    assert all(
        output.disposition is RoutingDisposition.TARGETED
        for output in outputs
    )
    assert all(
        output.target_ids == (8, 2)
        for output in outputs
    )
    assert assembler.stats().completed == 1
    assert assembler.stats().resets == 0
