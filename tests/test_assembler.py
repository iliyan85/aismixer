from dataclasses import FrozenInstanceError

import pytest

import assembler as assembler_module
from assembler import AIVDMAssembler, AssemblyOutcome, AssemblyStatus


class FakeClock:
    def __init__(self, now=0.0):
        self.now = now
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.now


def _multipart_state_snapshot(assembler):
    return (
        {
            key: dict(group)
            for key, group in assembler.fragments.items()
        },
        dict(assembler.timestamps),
    )


@pytest.mark.parametrize(
    "line",
    [
        "!AIVDM,1,1,,A,payload",
        "!AIVDM,x,1,,A,payload,0*00",
        "!AIVDM,1,x,,A,payload,0*00",
        "!AIVDM,0,1,,A,payload,0*00",
        "!AIVDM,1,0,,A,payload,0*00",
        "!AIVDM,1,2,,A,payload,0*00",
    ],
    ids=[
        "too-few-fields",
        "non-integer-total",
        "non-integer-current",
        "total-below-one",
        "single-current-below-one",
        "single-current-above-total",
    ],
)
def test_feed_outcome_invalid_input_is_state_and_clock_free(line):
    clock = FakeClock()
    assembler = AIVDMAssembler(clock=clock)
    pending = "!AIVDM,2,1,7,A,pending,0*00"

    assert (
        assembler.feed_outcome("pending-src", pending).status
        is AssemblyStatus.PENDING
    )
    clock.now = assembler.timeout
    state_before = _multipart_state_snapshot(assembler)
    calls_before = clock.calls

    outcome = assembler.feed_outcome("src", line)

    assert outcome.status is AssemblyStatus.INVALID
    assert outcome.group_key is None
    assert outcome.sentences == ()
    assert outcome.discarded_keys == ()
    assert clock.calls == calls_before
    assert _multipart_state_snapshot(assembler) == state_before


def test_feed_outcome_reports_single_sentence():
    clock = FakeClock()
    assembler = AIVDMAssembler(clock=clock)
    sentence = "!AIVDM,1,1,,A,payload,0*00"

    first = assembler.feed_outcome("src", sentence)
    second = assembler.feed_outcome("src", sentence)

    for outcome in (first, second):
        assert outcome.status is AssemblyStatus.SINGLE
        assert outcome.group_key is None
        assert outcome.sentences == (sentence,)
        assert outcome.sentences[0] is sentence
        assert outcome.discarded_keys == ()
    assert clock.calls == 0
    assert _multipart_state_snapshot(assembler) == ({}, {})


def test_single_sentence_does_not_expire_or_mutate_pending_group():
    clock = FakeClock()
    assembler = AIVDMAssembler(timeout=1.0, clock=clock)
    old_first = "!AIVDM,2,1,7,A,OLD,0*00"
    fresh_first = "!AIVDM,2,1,7,A,FRESH,0*00"
    fresh_second = "!AIVDM,2,2,7,A,FRESH-SECOND,0*00"
    single = "!AIVDM,1,1,,A,single,0*00"
    key = ("src", "7", "A", 2)

    pending = assembler.feed_outcome("src", old_first)

    assert pending.status is AssemblyStatus.PENDING
    assert clock.calls == 1

    state_before = _multipart_state_snapshot(assembler)
    calls_before = clock.calls
    clock.now = 1.1

    single_outcome = assembler.feed_outcome("other-src", single)

    assert single_outcome.status is AssemblyStatus.SINGLE
    assert single_outcome.group_key is None
    assert single_outcome.sentences == (single,)
    assert single_outcome.sentences[0] is single
    assert single_outcome.discarded_keys == ()
    assert clock.calls == calls_before
    assert _multipart_state_snapshot(assembler) == state_before

    fresh_pending = assembler.feed_outcome("src", fresh_second)

    assert fresh_pending.status is AssemblyStatus.PENDING
    assert fresh_pending.group_key == key
    assert fresh_pending.discarded_keys == (key,)
    assert clock.calls == calls_before + 1

    complete = assembler.feed_outcome("src", fresh_first)

    assert complete.status is AssemblyStatus.COMPLETE
    assert complete.sentences == (fresh_first, fresh_second)


def test_feed_outcome_reports_pending_then_complete():
    assembler = AIVDMAssembler()
    first = "!AIVDM,2,1,7,A,payload1,0*00"
    second = "!AIVDM,2,2,7,A,payload2,0*00"
    key = ("src", "7", "A", 2)

    pending = assembler.feed_outcome("src", first)
    complete = assembler.feed_outcome("src", second)

    assert pending.status is AssemblyStatus.PENDING
    assert pending.group_key == key
    assert pending.sentences == ()
    assert pending.discarded_keys == ()
    assert complete.status is AssemblyStatus.COMPLETE
    assert complete.group_key == key
    assert complete.sentences == (first, second)
    assert complete.discarded_keys == ()


def test_feed_outcome_preserves_out_of_order_completion():
    assembler = AIVDMAssembler()
    first = "!AIVDM,2,1,7,A,payload1,0*00"
    second = "!AIVDM,2,2,7,A,payload2,0*00"
    key = ("src", "7", "A", 2)

    pending = assembler.feed_outcome("src", second)
    complete = assembler.feed_outcome("src", first)

    assert pending.status is AssemblyStatus.PENDING
    assert pending.group_key == key
    assert complete.status is AssemblyStatus.COMPLETE
    assert complete.group_key == key
    assert complete.sentences == (first, second)
    assert complete.discarded_keys == ()


def test_feed_outcome_reports_exact_duplicate():
    assembler = AIVDMAssembler()
    first = "!AIVDM,2,1,7,A,payload1,0*00"
    second = "!AIVDM,2,2,7,A,payload2,0*00"
    key = ("src", "7", "A", 2)

    pending = assembler.feed_outcome("src", first)
    duplicate = assembler.feed_outcome("src", first)
    complete = assembler.feed_outcome("src", second)

    assert pending.status is AssemblyStatus.PENDING
    assert duplicate.status is AssemblyStatus.DUPLICATE
    assert duplicate.group_key == key
    assert duplicate.sentences == ()
    assert duplicate.discarded_keys == ()
    assert complete.status is AssemblyStatus.COMPLETE
    assert complete.sentences == (first, second)


def test_feed_outcome_reports_conflict_and_discarded_key():
    assembler = AIVDMAssembler()
    first_a = "!AIVDM,2,1,7,A,AAAAAA,0*00"
    first_b = "!AIVDM,2,1,7,A,BBBBBB,0*00"
    second = "!AIVDM,2,2,7,A,CCCCCC,0*00"
    key = ("src", "7", "A", 2)

    pending = assembler.feed_outcome("src", first_a)
    conflict = assembler.feed_outcome("src", first_b)

    assert pending.status is AssemblyStatus.PENDING
    assert conflict.status is AssemblyStatus.CONFLICT
    assert conflict.group_key == key
    assert conflict.sentences == ()
    assert conflict.discarded_keys == (key,)

    fresh_pending = assembler.feed_outcome("src", second)
    complete = assembler.feed_outcome("src", first_a)

    assert fresh_pending.status is AssemblyStatus.PENDING
    assert complete.status is AssemblyStatus.COMPLETE
    assert complete.sentences == (first_a, second)


def test_feed_outcome_reports_expired_generation_before_fresh_state():
    clock = FakeClock()
    assembler = AIVDMAssembler(timeout=1.0, clock=clock)
    old_first = "!AIVDM,2,1,7,A,OLD,0*00"
    fresh_first = "!AIVDM,2,1,7,A,FRESH,0*00"
    fresh_second = "!AIVDM,2,2,7,A,payload2,0*00"
    key = ("src", "7", "A", 2)

    old_pending = assembler.feed_outcome("src", old_first)
    clock.now = 1.0
    fresh_pending = assembler.feed_outcome("src", fresh_second)

    assert old_pending.status is AssemblyStatus.PENDING
    assert fresh_pending.status is AssemblyStatus.PENDING
    assert fresh_pending.group_key == key
    assert fresh_pending.sentences == ()
    assert fresh_pending.discarded_keys == (key,)

    complete = assembler.feed_outcome("src", fresh_first)

    assert complete.status is AssemblyStatus.COMPLETE
    assert complete.sentences == (fresh_first, fresh_second)
    assert old_first not in complete.sentences


def test_feed_outcome_reports_unrelated_expired_keys():
    clock = FakeClock()
    assembler = AIVDMAssembler(timeout=1.0, clock=clock)
    source_b_first = "!AIVDM,2,1,2,B,source-b,0*00"
    source_a_first = "!AIVDM,2,1,1,A,source-a,0*00"
    source_c_first = "!AIVDM,2,1,3,A,source-c,0*00"
    key_a = ("source-a", "1", "A", 2)
    key_b = ("source-b", "2", "B", 2)
    key_c = ("source-c", "3", "A", 2)

    assert (
        assembler.feed_outcome("source-b", source_b_first).status
        is AssemblyStatus.PENDING
    )
    assert (
        assembler.feed_outcome("source-a", source_a_first).status
        is AssemblyStatus.PENDING
    )

    clock.now = 1.0
    outcome = assembler.feed_outcome("source-c", source_c_first)

    assert outcome.status is AssemblyStatus.PENDING
    assert outcome.group_key == key_c
    assert outcome.sentences == ()
    assert outcome.discarded_keys == (key_a, key_b)


def test_feed_remains_compatible_with_structured_outcomes():
    assembler = AIVDMAssembler()
    first = "!AIVDM,2,1,7,A,payload1,0*00"
    second = "!AIVDM,2,2,7,A,payload2,0*00"

    assert assembler.feed("src", first) is None

    complete = assembler.feed("src", second)

    assert complete == [first, second]
    assert isinstance(complete, list)
    assert not isinstance(complete, AssemblyOutcome)


def test_assembly_outcome_is_immutable():
    outcome = AIVDMAssembler().feed_outcome(
        "src",
        "!AIVDM,2,1,7,A,payload1,0*00",
    )

    with pytest.raises(FrozenInstanceError):
        outcome.status = AssemblyStatus.COMPLETE


def test_default_clock_uses_time_monotonic(monkeypatch):
    calls = []

    def fake_monotonic():
        calls.append(True)
        return 123.0

    monkeypatch.setattr(assembler_module.time, "monotonic", fake_monotonic)
    assembler = AIVDMAssembler()
    first = "!AIVDM,2,1,7,A,payload1,0*00"

    assert assembler.feed("src", first) is None
    assert calls


def test_feed_returns_none_for_non_numeric_total():
    assembler = AIVDMAssembler()

    assert assembler.feed("src", "!AIVDM,x,1,,A,payload,0*00") is None


def test_feed_returns_none_for_non_numeric_current():
    assembler = AIVDMAssembler()

    assert assembler.feed("src", "!AIVDM,2,x,,A,payload,0*00") is None


def test_feed_returns_none_for_too_few_fields():
    assembler = AIVDMAssembler()

    assert assembler.feed("src", "!AIVDM,1,1,,A,payload") is None


def test_feed_returns_none_for_zero_total():
    assembler = AIVDMAssembler()

    assert assembler.feed("src", "!AIVDM,0,1,,A,payload,0*00") is None


def test_feed_returns_none_for_zero_current():
    assembler = AIVDMAssembler()

    assert assembler.feed("src", "!AIVDM,2,0,,A,payload,0*00") is None


def test_feed_returns_none_when_current_exceeds_total():
    assembler = AIVDMAssembler()

    assert assembler.feed("src", "!AIVDM,2,3,,A,payload,0*00") is None


def test_feed_returns_single_part_sentence():
    clock = FakeClock()
    assembler = AIVDMAssembler(clock=clock)
    sentence = "!AIVDM,1,1,,A,payload,0*00"

    first = assembler.feed("src", sentence)
    second = assembler.feed("src", sentence)

    assert first == [sentence]
    assert second == [sentence]
    assert first[0] is sentence
    assert second[0] is sentence
    assert clock.calls == 0


def test_feed_assembles_valid_multipart_sentences():
    assembler = AIVDMAssembler()
    first = "!AIVDM,2,1,7,A,payload1,0*00"
    second = "!AIVDM,2,2,7,A,payload2,0*00"

    assert assembler.feed("src", first) is None
    assert assembler.feed("src", second) == [first, second]


def test_out_of_order_fragments_complete_in_ordinal_order():
    assembler = AIVDMAssembler()
    first = "!AIVDM,2,1,7,A,payload1,0*00"
    second = "!AIVDM,2,2,7,A,payload2,0*00"

    assert assembler.feed("src", second) is None
    assert assembler.feed("src", first) == [first, second]


def test_blank_sequence_compatibility_supports_out_of_order_assembly():
    assembler = AIVDMAssembler(clock=FakeClock())
    first = "!AIVDM,2,1,,A,SINGLE_MESSAGE_PART_1,0*00"
    second = "!AIVDM,2,2,,A,SINGLE_MESSAGE_PART_2,0*00"

    # Compatibility contract: a coherent blank-ID message may start
    # with any valid ordinal and still complete in ordinal order.
    assert assembler.feed("src", second) is None
    assert assembler.feed("src", first) == [first, second]


def test_blank_sequence_compatibility_can_ambiguously_combine_distinct_messages():
    assembler = AIVDMAssembler(clock=FakeClock())
    message_a = (
        "!AIVDM,2,1,,A,MESSAGE_A_PART_1,0*00",
        "!AIVDM,2,2,,A,MESSAGE_A_PART_2,0*00",
    )
    message_b = (
        "!AIVDM,2,1,,A,MESSAGE_B_PART_1,0*00",
        "!AIVDM,2,2,,A,MESSAGE_B_PART_2,0*00",
    )

    assert assembler.feed("src", message_a[0]) is None

    # Compatibility limitation: A1 plus B2 forms a synthetic
    # logical combination, not proof of common transmission origin. There is
    # no duplicate-ordinal conflict, and the available NMEA identity cannot
    # distinguish the cases without rejecting valid blank-ID traffic or valid
    # out-of-order fragments. Future native implementations must preserve this
    # reference behavior unless the public contract is deliberately revised.
    assert assembler.feed("src", message_b[1]) == [
        message_a[0],
        message_b[1],
    ]


def test_blank_sequence_compatibility_does_not_correlate_with_expired_state():
    clock = FakeClock()
    assembler = AIVDMAssembler(timeout=1.0, clock=clock)
    old_a1 = "!AIVDM,2,1,,A,OLD_A1,0*00"
    new_b1 = "!AIVDM,2,1,,A,NEW_B1,0*00"
    new_b2 = "!AIVDM,2,2,,A,NEW_B2,0*00"

    assert assembler.feed("src", old_a1) is None

    clock.now = 1.0

    assert assembler.feed("src", new_b2) is None

    clock.now = 1.1

    result = assembler.feed("src", new_b1)

    # TTL bounds the ambiguity window; it does not prove fresh common origin.
    assert result == [new_b1, new_b2]
    assert old_a1 not in result


def test_exact_duplicate_fragment_is_idempotent():
    assembler = AIVDMAssembler()
    first = "!AIVDM,2,1,7,A,payload1,0*00"
    second = "!AIVDM,2,2,7,A,payload2,0*00"

    assert assembler.feed("src", first) is None
    assert assembler.feed("src", first) is None

    result = assembler.feed("src", second)

    assert result == [first, second]
    assert result.count(first) == 1


def test_conflicting_duplicate_ordinal_invalidates_live_group():
    assembler = AIVDMAssembler()
    first_a = "!AIVDM,2,1,7,A,AAAAAA,0*00"
    first_b = "!AIVDM,2,1,7,A,BBBBBB,0*00"
    second = "!AIVDM,2,2,7,A,CCCCCC,0*00"

    assert assembler.feed("src", first_a) is None
    assert assembler.feed("src", first_b) is None
    assert assembler.feed("src", second) is None

    result = assembler.feed("src", first_a)

    assert result == [first_a, second]
    assert first_b not in result


def test_completion_requires_every_unique_ordinal():
    assembler = AIVDMAssembler()
    first = "!AIVDM,3,1,7,A,payload1,0*00"
    second = "!AIVDM,3,2,7,A,payload2,0*00"
    third = "!AIVDM,3,3,7,A,payload3,0*00"

    assert assembler.feed("src", first) is None
    assert assembler.feed("src", first) is None
    assert assembler.feed("src", third) is None
    assert assembler.feed("src", second) == [first, second, third]


def test_group_expires_exactly_at_timeout():
    clock = FakeClock()
    assembler = AIVDMAssembler(timeout=1.0, clock=clock)
    old_first = "!AIVDM,2,1,7,A,OLD,0*00"
    new_first = "!AIVDM,2,1,7,A,NEW,0*00"
    second = "!AIVDM,2,2,7,A,payload2,0*00"

    assert assembler.feed("source-a", old_first) is None

    clock.now = 1.0

    assert assembler.feed("source-a", second) is None

    clock.now = 1.1

    result = assembler.feed("source-a", new_first)

    assert result == [new_first, second]
    assert old_first not in result


def test_group_remains_live_immediately_before_timeout():
    clock = FakeClock()
    assembler = AIVDMAssembler(timeout=1.0, clock=clock)
    first = "!AIVDM,2,1,7,A,payload1,0*00"
    second = "!AIVDM,2,2,7,A,payload2,0*00"

    assert assembler.feed("source-a", first) is None

    clock.now = 0.999

    assert assembler.feed("source-a", second) == [first, second]


def test_unrelated_cleanup_expires_group_at_timeout_boundary():
    clock = FakeClock()
    assembler = AIVDMAssembler(timeout=1.0, clock=clock)
    first = "!AIVDM,2,1,7,A,payload1,0*00"
    second = "!AIVDM,2,2,7,A,payload2,0*00"
    unrelated = "!AIVDM,2,1,8,A,unrelated1,0*00"

    assert assembler.feed("source-a", first) is None

    clock.now = 1.0

    assert assembler.feed("source-b", unrelated) is None
    assert assembler.feed("source-a", second) is None
    assert assembler.feed("source-a", first) == [first, second]


def test_cleanup_expired_uses_injected_clock_at_timeout_boundary():
    clock = FakeClock()
    assembler = AIVDMAssembler(timeout=1.0, clock=clock)
    first = "!AIVDM,2,1,7,A,payload1,0*00"
    second = "!AIVDM,2,2,7,A,payload2,0*00"

    assert assembler.feed("source-a", first) is None

    clock.now = 1.0

    assert assembler.cleanup_expired() is None
    assert assembler.feed("source-a", second) is None
    assert assembler.feed("source-a", first) == [first, second]


def test_unique_fragment_refreshes_timeout_window():
    clock = FakeClock()
    assembler = AIVDMAssembler(timeout=1.0, clock=clock)
    first = "!AIVDM,3,1,7,A,payload1,0*00"
    second = "!AIVDM,3,2,7,A,payload2,0*00"
    third = "!AIVDM,3,3,7,A,payload3,0*00"

    assert assembler.feed("source-a", first) is None

    clock.now = 0.75

    assert assembler.feed("source-a", second) is None

    clock.now = 1.5

    assert assembler.feed("source-a", third) == [first, second, third]


def test_exact_duplicate_does_not_refresh_timeout_window():
    clock = FakeClock()
    assembler = AIVDMAssembler(timeout=1.0, clock=clock)
    old_first = "!AIVDM,2,1,7,A,OLD,0*00"
    new_first = "!AIVDM,2,1,7,A,NEW,0*00"
    second = "!AIVDM,2,2,7,A,payload2,0*00"

    assert assembler.feed("source-a", old_first) is None

    clock.now = 0.75

    assert assembler.feed("source-a", old_first) is None

    clock.now = 1.0

    assert assembler.feed("source-a", second) is None

    clock.now = 1.1

    result = assembler.feed("source-a", new_first)

    assert result == [new_first, second]
    assert old_first not in result


def test_fragment_after_timeout_starts_fresh_out_of_order_group():
    clock = FakeClock()
    assembler = AIVDMAssembler(timeout=1.0, clock=clock)
    old_first = "!AIVDM,2,1,7,A,OLD,0*00"
    new_first = "!AIVDM,2,1,7,A,NEW,0*00"
    second = "!AIVDM,2,2,7,A,payload2,0*00"

    assert assembler.feed("source-a", old_first) is None

    clock.now = 10.0

    assert assembler.feed("source-a", second) is None

    clock.now = 10.1

    result = assembler.feed("source-a", new_first)

    assert result == [new_first, second]
    assert old_first not in result


def test_reset_clears_all_pending_groups():
    clock = FakeClock()
    assembler = AIVDMAssembler(timeout=1.0, clock=clock)
    first_a = "!AIVDM,2,1,7,A,group-a-1,0*00"
    second_a = "!AIVDM,2,2,7,A,group-a-2,0*00"
    first_b = "!AIVDM,2,1,8,A,group-b-1,0*00"
    second_b = "!AIVDM,2,2,8,A,group-b-2,0*00"

    assert assembler.feed("source-a", first_a) is None
    assert assembler.feed("source-b", first_b) is None

    assert assembler.reset() is None

    assert assembler.feed("source-a", second_a) is None
    assert assembler.feed("source-b", second_b) is None
    assert assembler.feed("source-a", first_a) == [first_a, second_a]
    assert assembler.feed("source-b", first_b) == [first_b, second_b]


def test_reset_is_safe_when_empty():
    clock = FakeClock()
    assembler = AIVDMAssembler(timeout=1.0, clock=clock)
    first = "!AIVDM,2,1,7,A,payload1,0*00"
    second = "!AIVDM,2,2,7,A,payload2,0*00"

    assert assembler.reset() is None
    assert assembler.reset() is None
    assert assembler.feed("src", first) is None
    assert assembler.feed("src", second) == [first, second]


def test_feed_groups_multipart_by_same_source_seq_channel_and_total():
    assembler = AIVDMAssembler()
    first = "!AIVDM,2,1,7,A,payload1,0*00"
    second = "!AIVDM,2,2,7,A,payload2,0*00"

    assert assembler.feed("source-a", first) is None
    assert assembler.feed("source-a", second) == [first, second]


def test_feed_does_not_assemble_fragments_from_different_sources():
    assembler = AIVDMAssembler()
    first = "!AIVDM,2,1,7,A,payload1,0*00"
    second = "!AIVDM,2,2,7,A,payload2,0*00"

    assert assembler.feed("source-a", first) is None
    assert assembler.feed("source-b", second) is None


def test_feed_does_not_assemble_fragments_with_different_seq_id():
    assembler = AIVDMAssembler()
    first = "!AIVDM,2,1,7,A,payload1,0*00"
    second = "!AIVDM,2,2,8,A,payload2,0*00"

    assert assembler.feed("source-a", first) is None
    assert assembler.feed("source-a", second) is None


def test_feed_does_not_assemble_fragments_with_different_channel():
    assembler = AIVDMAssembler()
    first = "!AIVDM,2,1,7,A,payload1,0*00"
    second = "!AIVDM,2,2,7,B,payload2,0*00"

    assert assembler.feed("source-a", first) is None
    assert assembler.feed("source-a", second) is None


def test_feed_does_not_assemble_fragments_with_different_total_count():
    assembler = AIVDMAssembler()
    first = "!AIVDM,2,1,7,A,payload1,0*00"
    second = "!AIVDM,3,2,7,A,payload2,0*00"

    assert assembler.feed("source-a", first) is None
    assert assembler.feed("source-a", second) is None
