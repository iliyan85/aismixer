import assembler as assembler_module
from assembler import AIVDMAssembler


class FakeClock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now


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


def test_current_behavior_orders_out_of_order_fragments_by_ordinal():
    assembler = AIVDMAssembler()
    first = "!AIVDM,2,1,7,A,payload1,0*00"
    second = "!AIVDM,2,2,7,A,payload2,0*00"

    # Characterization only: this ordering is not yet an approved contract.
    assert assembler.feed("src", second) is None
    assert assembler.feed("src", first) == [first, second]


def test_current_behavior_known_defect_candidate_repeated_ordinal_completes():
    assembler = AIVDMAssembler()
    first = "!AIVDM,2,1,7,A,payload1,0*00"

    assert assembler.feed("src", first) is None

    # Known defect candidate: stored count currently completes the group even
    # though fragment 2 was never received. This does not approve that policy.
    assert assembler.feed("src", first) == [first, first]


def test_current_behavior_timeout_equality_keeps_group_live(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(assembler_module.time, "time", clock)
    assembler = AIVDMAssembler(timeout=1.0)
    first = "!AIVDM,2,1,7,A,payload1,0*00"
    second = "!AIVDM,2,2,7,A,payload2,0*00"
    unrelated = "!AIVDM,2,1,8,A,unrelated1,0*00"

    assert assembler.feed("source-a", first) is None

    clock.now = 1.0

    # Characterization only: strict equality is not yet an approved contract.
    assert assembler.feed("source-b", unrelated) is None
    assert assembler.feed("source-a", second) == [first, second]


def test_current_behavior_unrelated_cleanup_expires_group_after_timeout(
    monkeypatch,
):
    clock = FakeClock()
    monkeypatch.setattr(assembler_module.time, "time", clock)
    assembler = AIVDMAssembler(timeout=1.0)
    first = "!AIVDM,2,1,7,A,payload1,0*00"
    second = "!AIVDM,2,2,7,A,payload2,0*00"
    unrelated = "!AIVDM,2,1,8,A,unrelated1,0*00"

    assert assembler.feed("source-a", first) is None

    clock.now = 1.001

    assert assembler.feed("source-b", unrelated) is None
    assert assembler.feed("source-a", second) is None


def test_current_behavior_accepted_fragment_refreshes_timeout_window(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(assembler_module.time, "time", clock)
    assembler = AIVDMAssembler(timeout=1.0)
    first = "!AIVDM,3,1,7,A,payload1,0*00"
    second = "!AIVDM,3,2,7,A,payload2,0*00"
    third = "!AIVDM,3,3,7,A,payload3,0*00"
    unrelated = "!AIVDM,2,1,8,A,unrelated1,0*00"

    assert assembler.feed("source-a", first) is None

    clock.now = 0.75

    assert assembler.feed("source-a", second) is None

    clock.now = 1.5

    # Characterization only: sliding refresh is not yet an approved contract.
    assert assembler.feed("source-b", unrelated) is None
    assert assembler.feed("source-a", third) == [first, second, third]


def test_current_behavior_known_defect_candidate_stale_group_revives(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(assembler_module.time, "time", clock)
    assembler = AIVDMAssembler(timeout=1.0)
    first = "!AIVDM,2,1,7,A,payload1,0*00"
    second = "!AIVDM,2,2,7,A,payload2,0*00"

    assert assembler.feed("source-a", first) is None

    clock.now = 10.0

    # Known defect candidate: feed refreshes and appends to the stale current
    # key before cleanup, so the old group completes after its nominal timeout.
    assert assembler.feed("source-a", second) == [first, second]


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
