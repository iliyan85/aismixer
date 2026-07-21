import assembler as assembler_module
from assembler import AIVDMAssembler


class FakeClock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now


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
    assembler = AIVDMAssembler()
    sentence = "!AIVDM,1,1,,A,payload,0*00"

    assert assembler.feed("src", sentence) == [sentence]


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
