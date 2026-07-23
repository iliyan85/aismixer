from dataclasses import FrozenInstanceError

import pytest

import assembler as assembler_module
from assembler import (
    AIVDMAssembler,
    AssemblerStats,
    AssemblyOutcome,
    AssemblyStatus,
)


class FakeClock:
    def __init__(self, now=0.0):
        self.now = now
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.now


def _multipart_state_snapshot(assembler):
    return {
        key: (
            dict(group.fragments_by_ordinal),
            group.received_count,
            group.last_progress_at,
        )
        for key, group in assembler._groups.items()
    }


class MembershipBudgetDict(dict):
    def __init__(self, values, allowed_checks):
        super().__init__(values)
        self.allowed_checks = allowed_checks
        self.membership_checks = 0

    def __contains__(self, key):
        self.membership_checks += 1
        if self.membership_checks > self.allowed_checks:
            raise AssertionError("unexpected ordinal membership scan")
        return super().__contains__(key)


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
    assert _multipart_state_snapshot(assembler) == {}


def test_group_state_indexes_unique_progress_out_of_order():
    clock = FakeClock(now=10.0)
    assembler = AIVDMAssembler(clock=clock)
    first = "!AIVDM,3,1,7,A,payload1,0*00"
    third = "!AIVDM,3,3,7,A,payload3,0*00"
    key = ("src", "7", "A", 3)

    assert not hasattr(assembler, "fragments")
    assert not hasattr(assembler, "timestamps")

    first_pending = assembler.feed_outcome("src", third)

    assert first_pending.status is AssemblyStatus.PENDING
    assert set(assembler._groups) == {key}
    group = assembler._groups[key]
    assert group.fragments_by_ordinal == {3: third}
    assert group.fragments_by_ordinal[3] is third
    assert group.received_count == 1
    assert group.last_progress_at == 10.0

    clock.now = 10.5
    second_pending = assembler.feed_outcome("src", first)

    assert second_pending.status is AssemblyStatus.PENDING
    assert assembler._groups[key] is group
    assert group.fragments_by_ordinal == {1: first, 3: third}
    assert group.fragments_by_ordinal[1] is first
    assert group.fragments_by_ordinal[3] is third
    assert group.received_count == 2
    assert group.last_progress_at == 10.5


def test_incomplete_admission_does_not_scan_declared_ordinal_range():
    assembler = AIVDMAssembler(clock=FakeClock())
    first = "!AIVDM,1000000,1,7,A,payload1,0*00"
    second = "!AIVDM,1000000,2,7,A,payload2,0*00"
    key = ("src", "7", "A", 1_000_000)

    assert (
        assembler.feed_outcome("src", first).status
        is AssemblyStatus.PENDING
    )
    group = assembler._groups[key]
    fragments = MembershipBudgetDict(
        group.fragments_by_ordinal,
        allowed_checks=1,
    )
    group.fragments_by_ordinal = fragments

    outcome = assembler.feed_outcome("src", second)

    assert outcome.status is AssemblyStatus.PENDING
    assert fragments.membership_checks <= 1
    assert group.received_count == 2


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
    assembler = AIVDMAssembler(clock=FakeClock())
    first = "!AIVDM,2,1,7,A,payload1,0*00"
    second = "!AIVDM,2,2,7,A,payload2,0*00"
    key = ("src", "7", "A", 2)

    pending = assembler.feed_outcome("src", second)
    completed_group = assembler._groups[key]
    complete = assembler.feed_outcome("src", first)

    assert pending.status is AssemblyStatus.PENDING
    assert pending.group_key == key
    assert complete.status is AssemblyStatus.COMPLETE
    assert complete.group_key == key
    assert complete.sentences == (first, second)
    assert complete.sentences[0] is first
    assert complete.sentences[1] is second
    assert complete.discarded_keys == ()
    assert key not in assembler._groups

    fresh_pending = assembler.feed_outcome("src", first)

    assert fresh_pending.status is AssemblyStatus.PENDING
    assert assembler._groups[key] is not completed_group
    assert assembler._groups[key].fragments_by_ordinal == {1: first}


def test_feed_outcome_reports_exact_duplicate():
    clock = FakeClock()
    assembler = AIVDMAssembler(clock=clock)
    first = "!AIVDM,2,1,7,A,payload1,0*00"
    second = "!AIVDM,2,2,7,A,payload2,0*00"
    key = ("src", "7", "A", 2)

    pending = assembler.feed_outcome("src", first)
    group = assembler._groups[key]
    state_before = _multipart_state_snapshot(assembler)
    clock.now = 0.5
    duplicate = assembler.feed_outcome("src", first)

    assert pending.status is AssemblyStatus.PENDING
    assert duplicate.status is AssemblyStatus.DUPLICATE
    assert duplicate.group_key == key
    assert duplicate.sentences == ()
    assert duplicate.discarded_keys == ()
    assert assembler._groups[key] is group
    assert _multipart_state_snapshot(assembler) == state_before
    assert group.fragments_by_ordinal == {1: first}
    assert group.fragments_by_ordinal[1] is first
    assert group.received_count == 1
    assert group.last_progress_at == 0.0

    complete = assembler.feed_outcome("src", second)

    assert complete.status is AssemblyStatus.COMPLETE
    assert complete.sentences == (first, second)


def test_feed_outcome_reports_conflict_and_discarded_key():
    assembler = AIVDMAssembler()
    first_a = "!AIVDM,2,1,7,A,AAAAAA,0*00"
    first_b = "!AIVDM,2,1,7,A,BBBBBB,0*00"
    second = "!AIVDM,2,2,7,A,CCCCCC,0*00"
    key = ("src", "7", "A", 2)

    pending = assembler.feed_outcome("src", first_a)
    invalidated_group = assembler._groups[key]
    conflict = assembler.feed_outcome("src", first_b)

    assert pending.status is AssemblyStatus.PENDING
    assert conflict.status is AssemblyStatus.CONFLICT
    assert conflict.group_key == key
    assert conflict.sentences == ()
    assert conflict.discarded_keys == (key,)
    assert key not in assembler._groups
    assert first_b not in invalidated_group.fragments_by_ordinal.values()

    fresh_pending = assembler.feed_outcome("src", second)
    replacement_group = assembler._groups[key]

    assert replacement_group is not invalidated_group
    assert replacement_group.fragments_by_ordinal == {2: second}
    assert replacement_group.received_count == 1

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
    expired_group = assembler._groups[key]
    clock.now = 1.0
    fresh_pending = assembler.feed_outcome("src", fresh_second)

    assert old_pending.status is AssemblyStatus.PENDING
    assert fresh_pending.status is AssemblyStatus.PENDING
    assert fresh_pending.group_key == key
    assert fresh_pending.sentences == ()
    assert fresh_pending.discarded_keys == (key,)
    fresh_group = assembler._groups[key]
    assert fresh_group is not expired_group
    assert fresh_group.fragments_by_ordinal == {2: fresh_second}
    assert fresh_group.fragments_by_ordinal[2] is fresh_second
    assert fresh_group.received_count == 1
    assert fresh_group.last_progress_at == 1.0
    assert old_first not in fresh_group.fragments_by_ordinal.values()

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
    assert set(assembler._groups) == {key_c}


def test_duplicate_reports_unrelated_expired_keys_without_refresh():
    clock = FakeClock()
    assembler = AIVDMAssembler(timeout=1.0, clock=clock)
    source_b_first = "!AIVDM,2,1,2,B,source-b,0*00"
    source_a_first = "!AIVDM,2,1,1,A,source-a,0*00"
    target_first = "!AIVDM,2,1,3,A,target,0*00"
    key_a = ("source-a", "1", "A", 2)
    key_b = ("source-b", "2", "B", 2)
    target_key = ("target", "3", "A", 2)

    assembler.feed_outcome("source-b", source_b_first)
    assembler.feed_outcome("source-a", source_a_first)
    clock.now = 0.5
    assembler.feed_outcome("target", target_first)
    target_group = assembler._groups[target_key]

    clock.now = 1.0
    duplicate = assembler.feed_outcome("target", target_first)

    assert duplicate.status is AssemblyStatus.DUPLICATE
    assert duplicate.discarded_keys == (key_a, key_b)
    assert set(assembler._groups) == {target_key}
    assert assembler._groups[target_key] is target_group
    assert target_group.received_count == 1
    assert target_group.last_progress_at == 0.5


def test_conflict_reports_unrelated_expired_keys_in_sorted_order():
    clock = FakeClock()
    assembler = AIVDMAssembler(timeout=1.0, clock=clock)
    source_b_first = "!AIVDM,2,1,2,B,source-b,0*00"
    source_a_first = "!AIVDM,2,1,1,A,source-a,0*00"
    target_first = "!AIVDM,2,1,3,A,target-a,0*00"
    target_conflict = "!AIVDM,2,1,3,A,target-b,0*00"
    key_a = ("source-a", "1", "A", 2)
    key_b = ("source-b", "2", "B", 2)
    target_key = ("target", "3", "A", 2)

    assembler.feed_outcome("source-b", source_b_first)
    assembler.feed_outcome("source-a", source_a_first)
    clock.now = 0.5
    assembler.feed_outcome("target", target_first)

    clock.now = 1.0
    conflict = assembler.feed_outcome("target", target_conflict)

    assert conflict.status is AssemblyStatus.CONFLICT
    assert conflict.group_key == target_key
    assert conflict.discarded_keys == (key_a, key_b, target_key)
    assert conflict.discarded_keys.count(target_key) == 1
    assert assembler._groups == {}


def test_completion_does_not_sweep_unrelated_expired_group():
    clock = FakeClock()
    assembler = AIVDMAssembler(timeout=1.0, clock=clock)
    unrelated_first = "!AIVDM,2,1,1,A,unrelated,0*00"
    target_first = "!AIVDM,2,1,3,A,target-1,0*00"
    target_second = "!AIVDM,2,2,3,A,target-2,0*00"
    unrelated_key = ("unrelated", "1", "A", 2)
    target_key = ("target", "3", "A", 2)

    assembler.feed_outcome("unrelated", unrelated_first)
    unrelated_group = assembler._groups[unrelated_key]
    clock.now = 0.5
    assembler.feed_outcome("target", target_first)

    clock.now = 1.0
    complete = assembler.feed_outcome("target", target_second)

    assert complete.status is AssemblyStatus.COMPLETE
    assert complete.sentences == (target_first, target_second)
    assert complete.discarded_keys == ()
    assert target_key not in assembler._groups
    assert assembler._groups[unrelated_key] is unrelated_group


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
    key = ("source-a", "7", "A", 2)

    assert assembler.feed("source-a", first) is None

    clock.now = 1.0

    assert assembler.cleanup_expired() == (key,)
    assert key not in assembler._groups
    assert assembler.feed("source-a", second) is None
    assert assembler.feed("source-a", first) == [first, second]


def test_cleanup_expired_removes_only_expired_group_objects():
    clock = FakeClock()
    assembler = AIVDMAssembler(timeout=1.0, clock=clock)
    expired_first = "!AIVDM,2,1,1,A,expired,0*00"
    live_first = "!AIVDM,2,1,2,A,live,0*00"
    expired_key = ("expired", "1", "A", 2)
    live_key = ("live", "2", "A", 2)

    assembler.feed_outcome("expired", expired_first)
    expired_group = assembler._groups[expired_key]
    clock.now = 0.5
    assembler.feed_outcome("live", live_first)
    live_group = assembler._groups[live_key]
    calls_before = clock.calls

    assert assembler.cleanup_expired(now=1.0) == (expired_key,)

    assert clock.calls == calls_before
    assert expired_key not in assembler._groups
    assert expired_group is not live_group
    assert assembler._groups[live_key] is live_group
    assert live_group.fragments_by_ordinal == {1: live_first}
    assert live_group.last_progress_at == 0.5


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
    assert set(assembler._groups) == {
        ("source-a", "7", "A", 2),
        ("source-b", "8", "A", 2),
    }

    assert assembler.reset() == (
        ("source-a", "7", "A", 2),
        ("source-b", "8", "A", 2),
    )
    assert assembler._groups == {}
    assert not hasattr(assembler, "fragments")
    assert not hasattr(assembler, "timestamps")

    assert assembler.feed("source-a", second_a) is None
    assert assembler.feed("source-b", second_b) is None
    assert assembler.feed("source-a", first_a) == [first_a, second_a]
    assert assembler.feed("source-b", first_b) == [first_b, second_b]


def test_reset_is_safe_when_empty():
    clock = FakeClock()
    assembler = AIVDMAssembler(timeout=1.0, clock=clock)
    first = "!AIVDM,2,1,7,A,payload1,0*00"
    second = "!AIVDM,2,2,7,A,payload2,0*00"

    assert assembler.reset() == ()
    assert assembler.reset() == ()
    assert assembler._groups == {}
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


def _b5_sentence(
    total,
    current,
    seq_id,
    payload,
    channel="A",
):
    return (
        f"!AIVDM,{total},{current},{seq_id},{channel},{payload},0*00"
    )


@pytest.mark.parametrize(
    "parameter",
    ["max_fragments_per_group", "max_pending_groups"],
)
@pytest.mark.parametrize("value", [None, 1, 7])
def test_assembler_limits_accept_none_and_positive_integers(
    parameter,
    value,
):
    assembler = AIVDMAssembler(**{parameter: value})

    assert getattr(assembler, parameter) == value


@pytest.mark.parametrize(
    "parameter",
    ["max_fragments_per_group", "max_pending_groups"],
)
@pytest.mark.parametrize("value", [0, -1, -99])
def test_assembler_limits_reject_integers_below_one(parameter, value):
    with pytest.raises(ValueError):
        AIVDMAssembler(**{parameter: value})


@pytest.mark.parametrize(
    "parameter",
    ["max_fragments_per_group", "max_pending_groups"],
)
@pytest.mark.parametrize(
    "value",
    [True, False, "1", 1.0, object()],
    ids=["true", "false", "string", "float", "object"],
)
def test_assembler_limits_reject_non_integer_types(parameter, value):
    with pytest.raises(TypeError):
        AIVDMAssembler(**{parameter: value})


def test_default_fragment_limit_is_unbounded():
    assembler = AIVDMAssembler(clock=FakeClock())
    sentence = _b5_sentence(1_000_000, 1, "7", "payload")

    outcome = assembler.feed_outcome("source", sentence)

    assert assembler.max_fragments_per_group is None
    assert outcome.status is AssemblyStatus.PENDING
    assert outcome.group_key == ("source", "7", "A", 1_000_000)


def test_fragment_limit_accepts_declaration_equal_to_limit():
    assembler = AIVDMAssembler(
        clock=FakeClock(),
        max_fragments_per_group=3,
    )

    outcome = assembler.feed_outcome(
        "source",
        _b5_sentence(3, 1, "7", "payload"),
    )

    assert outcome.status is AssemblyStatus.PENDING
    assert outcome.group_key == ("source", "7", "A", 3)


def test_fragment_limit_rejection_is_clock_cleanup_and_state_free():
    clock = FakeClock()
    assembler = AIVDMAssembler(
        timeout=1.0,
        clock=clock,
        max_fragments_per_group=2,
    )
    retained = _b5_sentence(2, 1, "1", "retained")
    rejected = _b5_sentence(3, 1, "2", "rejected")
    retained_key = ("retained-source", "1", "A", 2)

    assembler.feed_outcome("retained-source", retained)
    clock.now = 1.0
    state_before = _multipart_state_snapshot(assembler)
    calls_before = clock.calls
    stats_before = assembler.stats()

    first = assembler.feed_outcome("rejected-source", rejected)
    second = assembler.feed_outcome("rejected-source", rejected)

    for outcome in (first, second):
        assert outcome == AssemblyOutcome(
            AssemblyStatus.LIMIT_EXCEEDED,
        )
    assert assembler.feed("rejected-source", rejected) is None
    assert clock.calls == calls_before
    assert _multipart_state_snapshot(assembler) == state_before
    assert retained_key in assembler._groups

    stats = assembler.stats()
    assert stats.limit_exceeded == stats_before.limit_exceeded + 3
    assert stats.expired == stats_before.expired


def test_fragment_limit_one_preserves_single_and_invalid_fast_paths():
    clock = FakeClock()
    assembler = AIVDMAssembler(
        clock=clock,
        max_fragments_per_group=1,
    )
    single = _b5_sentence(1, 1, "", "single")

    single_outcome = assembler.feed_outcome("source", single)
    invalid_outcome = assembler.feed_outcome(
        "source",
        _b5_sentence(0, 1, "7", "invalid"),
    )
    limited_outcome = assembler.feed_outcome(
        "source",
        _b5_sentence(2, 1, "7", "limited"),
    )

    assert single_outcome.status is AssemblyStatus.SINGLE
    assert single_outcome.sentences == (single,)
    assert invalid_outcome.status is AssemblyStatus.INVALID
    assert limited_outcome.status is AssemblyStatus.LIMIT_EXCEEDED
    assert clock.calls == 0
    assert assembler._groups == {}


def test_capacity_evicts_one_victim_and_evicted_key_reopens_fresh():
    clock = FakeClock()
    assembler = AIVDMAssembler(
        timeout=100.0,
        clock=clock,
        max_pending_groups=2,
    )
    old_a_first = _b5_sentence(2, 1, "1", "old-a")
    fresh_a_first = _b5_sentence(2, 1, "1", "fresh-a")
    a_second = _b5_sentence(2, 2, "1", "a-second")
    b_first = _b5_sentence(2, 1, "2", "b-first")
    b_second = _b5_sentence(2, 2, "2", "b-second")
    c_first = _b5_sentence(2, 1, "3", "c-first")
    key_a = ("source-a", "1", "A", 2)
    key_b = ("source-b", "2", "A", 2)
    key_c = ("source-c", "3", "A", 2)

    assembler.feed_outcome("source-a", old_a_first)
    evicted_group = assembler._groups[key_a]
    clock.now = 1.0
    assembler.feed_outcome("source-b", b_first)
    clock.now = 2.0

    admitted = assembler.feed_outcome("source-c", c_first)

    assert admitted.status is AssemblyStatus.PENDING
    assert admitted.discarded_keys == (key_a,)
    assert set(assembler._groups) == {key_b, key_c}
    assert key_a not in assembler._groups
    assert evicted_group.fragments_by_ordinal == {1: old_a_first}
    assert assembler.stats().capacity_evicted == 1

    clock.now = 3.0
    assert (
        assembler.feed_outcome("source-b", b_second).status
        is AssemblyStatus.COMPLETE
    )
    clock.now = 4.0
    reopened = assembler.feed_outcome("source-a", a_second)

    assert reopened.status is AssemblyStatus.PENDING
    assert assembler._groups[key_a].fragments_by_ordinal == {2: a_second}

    completed = assembler.feed_outcome("source-a", fresh_a_first)

    assert completed.status is AssemblyStatus.COMPLETE
    assert completed.sentences == (fresh_a_first, a_second)
    assert old_a_first not in completed.sentences


def test_capacity_victim_uses_least_recent_unique_progress():
    clock = FakeClock()
    assembler = AIVDMAssembler(
        timeout=100.0,
        clock=clock,
        max_pending_groups=2,
    )
    a_first = _b5_sentence(3, 1, "1", "a-first")
    a_second = _b5_sentence(3, 2, "1", "a-second")
    b_first = _b5_sentence(2, 1, "2", "b-first")
    c_first = _b5_sentence(2, 1, "3", "c-first")
    key_a = ("source-a", "1", "A", 3)
    key_b = ("source-b", "2", "A", 2)
    key_c = ("source-c", "3", "A", 2)

    assembler.feed_outcome("source-a", a_first)
    clock.now = 1.0
    assembler.feed_outcome("source-b", b_first)
    clock.now = 2.0
    progress = assembler.feed_outcome("source-a", a_second)
    clock.now = 3.0
    admitted = assembler.feed_outcome("source-c", c_first)

    assert progress.status is AssemblyStatus.PENDING
    assert progress.discarded_keys == ()
    assert admitted.discarded_keys == (key_b,)
    assert set(assembler._groups) == {key_a, key_c}


def test_capacity_victim_ties_break_by_assembly_key():
    clock = FakeClock(now=5.0)
    assembler = AIVDMAssembler(
        timeout=100.0,
        clock=clock,
        max_pending_groups=2,
    )
    first = _b5_sentence(2, 1, "1", "first")
    second = _b5_sentence(2, 1, "2", "second")
    third = _b5_sentence(2, 1, "3", "third")
    key_a = ("source-a", "2", "A", 2)
    key_b = ("source-b", "1", "A", 2)
    key_c = ("source-c", "3", "A", 2)

    assembler.feed_outcome("source-b", first)
    assembler.feed_outcome("source-a", second)
    admitted = assembler.feed_outcome("source-c", third)

    assert admitted.discarded_keys == (key_a,)
    assert set(assembler._groups) == {key_b, key_c}


def test_full_capacity_does_not_evict_for_existing_group_lifecycle():
    clock = FakeClock()
    assembler = AIVDMAssembler(
        timeout=100.0,
        clock=clock,
        max_pending_groups=2,
    )
    a_first = _b5_sentence(3, 1, "1", "a-first")
    a_second = _b5_sentence(3, 2, "1", "a-second")
    b_first = _b5_sentence(2, 1, "2", "b-first")
    b_second = _b5_sentence(2, 2, "2", "b-second")
    key_a = ("source-a", "1", "A", 3)
    key_b = ("source-b", "2", "A", 2)

    assembler.feed_outcome("source-a", a_first)
    assembler.feed_outcome("source-b", b_first)

    duplicate = assembler.feed_outcome("source-a", a_first)
    progress = assembler.feed_outcome("source-a", a_second)
    complete = assembler.feed_outcome("source-b", b_second)

    assert duplicate.status is AssemblyStatus.DUPLICATE
    assert duplicate.discarded_keys == ()
    assert progress.status is AssemblyStatus.PENDING
    assert progress.discarded_keys == ()
    assert complete.status is AssemblyStatus.COMPLETE
    assert complete.discarded_keys == ()
    assert set(assembler._groups) == {key_a}
    assert key_b not in assembler._groups
    assert assembler.stats().capacity_evicted == 0


def test_full_capacity_conflict_invalidates_only_matching_group():
    assembler = AIVDMAssembler(
        clock=FakeClock(),
        max_pending_groups=2,
    )
    a_first = _b5_sentence(2, 1, "1", "a-first")
    a_conflict = _b5_sentence(2, 1, "1", "a-conflict")
    b_first = _b5_sentence(2, 1, "2", "b-first")
    key_a = ("source-a", "1", "A", 2)
    key_b = ("source-b", "2", "A", 2)

    assembler.feed_outcome("source-a", a_first)
    assembler.feed_outcome("source-b", b_first)
    conflict = assembler.feed_outcome("source-a", a_conflict)

    assert conflict.status is AssemblyStatus.CONFLICT
    assert conflict.discarded_keys == (key_a,)
    assert set(assembler._groups) == {key_b}
    assert assembler.stats().capacity_evicted == 0


def test_full_capacity_isolated_paths_do_not_evict_or_expire():
    clock = FakeClock()
    assembler = AIVDMAssembler(
        timeout=1.0,
        clock=clock,
        max_fragments_per_group=2,
        max_pending_groups=1,
    )
    retained = _b5_sentence(2, 1, "1", "retained")
    retained_key = ("retained-source", "1", "A", 2)

    assembler.feed_outcome("retained-source", retained)
    clock.now = 1.0
    state_before = _multipart_state_snapshot(assembler)
    calls_before = clock.calls

    invalid = assembler.feed_outcome(
        "other-source",
        _b5_sentence(0, 1, "2", "invalid"),
    )
    single = assembler.feed_outcome(
        "other-source",
        _b5_sentence(1, 1, "", "single"),
    )
    limited = assembler.feed_outcome(
        "other-source",
        _b5_sentence(3, 1, "3", "limited"),
    )

    assert invalid.status is AssemblyStatus.INVALID
    assert single.status is AssemblyStatus.SINGLE
    assert limited.status is AssemblyStatus.LIMIT_EXCEEDED
    assert all(
        outcome.discarded_keys == ()
        for outcome in (invalid, single, limited)
    )
    assert clock.calls == calls_before
    assert _multipart_state_snapshot(assembler) == state_before
    assert retained_key in assembler._groups
    assert assembler.stats().capacity_evicted == 0
    assert assembler.stats().expired == 0


def test_capacity_expires_dead_groups_before_selecting_live_victim():
    clock = FakeClock()
    assembler = AIVDMAssembler(
        timeout=1.0,
        clock=clock,
        max_pending_groups=2,
    )
    a_first = _b5_sentence(2, 1, "1", "a-first")
    b_first = _b5_sentence(2, 1, "2", "b-first")
    c_first = _b5_sentence(2, 1, "3", "c-first")
    key_a = ("source-a", "1", "A", 2)
    key_b = ("source-b", "2", "A", 2)
    key_c = ("source-c", "3", "A", 2)

    assembler.feed_outcome("source-a", a_first)
    clock.now = 0.5
    assembler.feed_outcome("source-b", b_first)
    clock.now = 1.0
    admitted = assembler.feed_outcome("source-c", c_first)

    assert admitted.discarded_keys == (key_a,)
    assert set(assembler._groups) == {key_b, key_c}
    stats = assembler.stats()
    assert stats.expired == 1
    assert stats.capacity_evicted == 0


def test_capacity_evicts_live_group_immediately_before_timeout():
    clock = FakeClock()
    assembler = AIVDMAssembler(
        timeout=1.0,
        clock=clock,
        max_pending_groups=1,
    )
    a_first = _b5_sentence(2, 1, "1", "a-first")
    b_first = _b5_sentence(2, 1, "2", "b-first")
    key_a = ("source-a", "1", "A", 2)
    key_b = ("source-b", "2", "A", 2)

    assembler.feed_outcome("source-a", a_first)
    clock.now = 0.999
    admitted = assembler.feed_outcome("source-b", b_first)

    assert admitted.discarded_keys == (key_a,)
    assert set(assembler._groups) == {key_b}
    stats = assembler.stats()
    assert stats.expired == 0
    assert stats.capacity_evicted == 1


def test_cleanup_expired_returns_sorted_keys_and_retains_live_groups():
    clock = FakeClock()
    assembler = AIVDMAssembler(timeout=1.0, clock=clock)
    b_first = _b5_sentence(2, 1, "2", "b-first")
    a_first = _b5_sentence(2, 1, "1", "a-first")
    live_first = _b5_sentence(2, 1, "3", "live-first")
    key_a = ("source-a", "1", "A", 2)
    key_b = ("source-b", "2", "A", 2)
    live_key = ("source-live", "3", "A", 2)

    assembler.feed_outcome("source-b", b_first)
    assembler.feed_outcome("source-a", a_first)
    clock.now = 0.5
    assembler.feed_outcome("source-live", live_first)
    calls_before = clock.calls

    discarded = assembler.cleanup_expired(now=1.0)

    assert discarded == (key_a, key_b)
    assert clock.calls == calls_before
    assert set(assembler._groups) == {live_key}
    assert assembler.stats().expired == 2
    assert assembler.cleanup_expired(now=1.0) == ()
    assert assembler.stats().expired == 2


def test_cleanup_expired_without_now_invokes_clock_once():
    clock = FakeClock()
    assembler = AIVDMAssembler(timeout=1.0, clock=clock)
    first = _b5_sentence(2, 1, "1", "first")
    key = ("source", "1", "A", 2)

    assembler.feed_outcome("source", first)
    clock.now = 1.0
    calls_before = clock.calls

    assert assembler.cleanup_expired() == (key,)
    assert clock.calls == calls_before + 1
    assert assembler.cleanup_expired(now=1.0) == ()
    assert clock.calls == calls_before + 1


def test_reset_returns_sorted_keys_and_preserves_configuration_and_stats():
    clock = FakeClock()
    assembler = AIVDMAssembler(
        timeout=7.5,
        clock=clock,
        max_fragments_per_group=4,
        max_pending_groups=3,
    )
    b_first = _b5_sentence(2, 1, "2", "b-first")
    a_first = _b5_sentence(3, 1, "1", "a-first")
    key_a = ("source-a", "1", "A", 3)
    key_b = ("source-b", "2", "A", 2)

    assembler.feed_outcome("source-b", b_first)
    assembler.feed_outcome("source-a", a_first)
    before = assembler.stats()

    discarded = assembler.reset()

    assert discarded == (key_a, key_b)
    assert assembler._groups == {}
    assert assembler.timeout == 7.5
    assert assembler._clock is clock
    assert assembler.max_fragments_per_group == 4
    assert assembler.max_pending_groups == 3

    after = assembler.stats()
    assert after.pending == before.pending
    assert after.peak_groups == before.peak_groups
    assert after.peak_fragments == before.peak_fragments
    assert after.reset_discarded == 2
    assert after.resets == 1
    assert after.expired == 0
    assert after.capacity_evicted == 0
    assert after.current_groups == 0
    assert after.current_fragments == 0

    assert assembler.reset() == ()
    empty_reset_stats = assembler.stats()
    assert empty_reset_stats.resets == 2
    assert empty_reset_stats.reset_discarded == 2


def test_new_assembler_statistics_are_zero():
    assembler = AIVDMAssembler()

    assert assembler.stats() == AssemblerStats(
        invalid=0,
        single=0,
        limit_exceeded=0,
        pending=0,
        duplicates=0,
        conflicts=0,
        completed=0,
        expired=0,
        capacity_evicted=0,
        reset_discarded=0,
        resets=0,
        current_groups=0,
        peak_groups=0,
        current_fragments=0,
        peak_fragments=0,
    )


def test_statistics_count_exactly_one_outcome_per_feed_call():
    assembler = AIVDMAssembler(
        clock=FakeClock(),
        max_fragments_per_group=2,
    )
    single = _b5_sentence(1, 1, "", "single")
    conflict_first = _b5_sentence(2, 1, "1", "first")
    conflict_other = _b5_sentence(2, 1, "1", "other")
    complete_first = _b5_sentence(2, 1, "2", "complete-first")
    complete_second = _b5_sentence(2, 2, "2", "complete-second")

    assert (
        assembler.feed_outcome("source", "!AIVDM,broken").status
        is AssemblyStatus.INVALID
    )
    assert assembler.feed("source", single) == [single]
    assert (
        assembler.feed_outcome(
            "source",
            _b5_sentence(3, 1, "3", "limited"),
        ).status
        is AssemblyStatus.LIMIT_EXCEEDED
    )
    assert (
        assembler.feed_outcome("source", conflict_first).status
        is AssemblyStatus.PENDING
    )
    assert (
        assembler.feed_outcome("source", conflict_first).status
        is AssemblyStatus.DUPLICATE
    )
    assert (
        assembler.feed_outcome("source", conflict_other).status
        is AssemblyStatus.CONFLICT
    )
    assert (
        assembler.feed_outcome("source", complete_first).status
        is AssemblyStatus.PENDING
    )
    assert (
        assembler.feed_outcome("source", complete_second).status
        is AssemblyStatus.COMPLETE
    )

    stats = assembler.stats()
    assert stats.invalid == 1
    assert stats.single == 1
    assert stats.limit_exceeded == 1
    assert stats.pending == 2
    assert stats.duplicates == 1
    assert stats.conflicts == 1
    assert stats.completed == 1
    assert sum(
        (
            stats.invalid,
            stats.single,
            stats.limit_exceeded,
            stats.pending,
            stats.duplicates,
            stats.conflicts,
            stats.completed,
        )
    ) == 8


def test_statistics_track_final_retained_state_and_peaks():
    clock = FakeClock()
    assembler = AIVDMAssembler(
        timeout=100.0,
        clock=clock,
        max_pending_groups=2,
    )
    a_first = _b5_sentence(3, 1, "1", "a-first")
    a_second = _b5_sentence(3, 2, "1", "a-second")
    a_third = _b5_sentence(3, 3, "1", "a-third")
    b_first = _b5_sentence(2, 1, "2", "b-first")
    b_second = _b5_sentence(2, 2, "2", "b-second")

    assembler.feed_outcome("source-a", a_first)
    assembler.feed_outcome("source-b", b_first)
    assembler.feed_outcome("source-a", a_second)
    before_completion = assembler.stats()

    assert before_completion.current_groups == len(assembler._groups) == 2
    assert before_completion.current_fragments == 3
    assert before_completion.peak_groups == 2
    assert before_completion.peak_fragments == 3
    assert before_completion.peak_groups <= assembler.max_pending_groups

    assembler.feed_outcome("source-b", b_second)
    after_b_completion = assembler.stats()

    assert after_b_completion.current_groups == 1
    assert after_b_completion.current_fragments == 2
    assert after_b_completion.peak_groups == 2
    assert after_b_completion.peak_fragments == 3

    assembler.feed_outcome("source-a", a_third)
    after_all_completion = assembler.stats()

    assert after_all_completion.current_groups == 0
    assert after_all_completion.current_fragments == 0
    assert after_all_completion.peak_groups == 2
    assert after_all_completion.peak_fragments == 3
    assert before_completion.current_groups == 2
    assert before_completion.current_fragments == 3

    with pytest.raises(FrozenInstanceError):
        before_completion.pending = 99


def test_stats_does_not_call_clock_or_clean_expired_state():
    clock = FakeClock()
    assembler = AIVDMAssembler(timeout=1.0, clock=clock)
    first = _b5_sentence(2, 1, "1", "first")
    key = ("source", "1", "A", 2)

    assembler.feed_outcome("source", first)
    original_snapshot = assembler.stats()
    clock.now = 1.0
    calls_before = clock.calls

    current_snapshot = assembler.stats()

    assert clock.calls == calls_before
    assert key in assembler._groups
    assert current_snapshot.expired == 0
    assert current_snapshot.current_groups == 1
    assert original_snapshot == current_snapshot

    assert assembler.cleanup_expired(now=1.0) == (key,)
    assert original_snapshot.expired == 0
    assert original_snapshot.current_groups == 1
    assert assembler.stats().expired == 1


def test_lifecycle_statistics_keep_removal_reasons_distinct():
    clock = FakeClock()
    assembler = AIVDMAssembler(
        timeout=1.0,
        clock=clock,
        max_pending_groups=1,
    )
    a_first = _b5_sentence(2, 1, "1", "a-first")
    b_first = _b5_sentence(2, 1, "2", "b-first")
    c_first = _b5_sentence(2, 1, "3", "c-first")
    c_conflict = _b5_sentence(2, 1, "3", "c-conflict")
    d_first = _b5_sentence(2, 1, "4", "d-first")

    assembler.feed_outcome("source-a", a_first)
    clock.now = 0.5
    assembler.feed_outcome("source-b", b_first)
    clock.now = 1.5
    assembler.cleanup_expired()
    assembler.feed_outcome("source-c", c_first)
    assembler.feed_outcome("source-c", c_conflict)
    assembler.feed_outcome("source-d", d_first)
    assembler.reset()

    stats = assembler.stats()
    assert stats.capacity_evicted == 1
    assert stats.expired == 1
    assert stats.conflicts == 1
    assert stats.reset_discarded == 1
    assert stats.resets == 1
