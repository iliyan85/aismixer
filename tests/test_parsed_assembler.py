from dataclasses import dataclass, replace

from assembler import (
    AIVDMAssembler,
    AssemblyOutcome,
    AssemblyStatus,
)
from core.event import IngressEvent
from core.ingress_frame import (
    IngressFrame,
    PayloadTextMode,
    frame_from_ingress_event,
)
from core.parsed_sentence import (
    ParsedFragment,
    ParsedSentence,
    parse_frame_sentences,
)


class FakeClock:
    def __init__(self, now=0.0):
        self.now = now
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.now


@dataclass(frozen=True)
class ParsedCase:
    parsed: ParsedSentence
    sentence_text: str


class DifferentialHarness:
    def __init__(self, **config):
        self.legacy_clock = FakeClock()
        self.parsed_clock = FakeClock()
        self.legacy = AIVDMAssembler(
            clock=self.legacy_clock,
            **config,
        )
        self.parsed = AIVDMAssembler(
            clock=self.parsed_clock,
            **config,
        )

    def at(self, now):
        self.legacy_clock.now = now
        self.parsed_clock.now = now

    def feed(self, case):
        legacy_outcome = self.legacy.feed_outcome(
            case.parsed.frame.assembler_key,
            case.sentence_text,
        )
        parsed_outcome = self.parsed.feed_parsed_outcome(case.parsed)

        assert parsed_outcome == legacy_outcome
        assert self.parsed.stats() == self.legacy.stats()
        assert self.parsed_clock.calls == self.legacy_clock.calls
        return parsed_outcome

    def reset(self):
        legacy_discarded = self.legacy.reset()
        parsed_discarded = self.parsed.reset()

        assert parsed_discarded == legacy_discarded
        assert self.parsed.stats() == self.legacy.stats()
        assert self.parsed_clock.calls == self.legacy_clock.calls
        return parsed_discarded


def sentence(
    total,
    ordinal,
    sequential_id,
    payload,
    channel="A",
):
    return (
        f"!AIVDM,{total},{ordinal},{sequential_id},"
        f"{channel},{payload},0*00"
    )


def make_case(
    sentence_text,
    *,
    assembler_key="source",
    source_id="input",
    alias_for_s=None,
    remote_ip="192.0.2.10",
    tag="",
    prefix="",
    suffix="",
):
    event = IngressEvent(
        kind="udp",
        source_id=source_id,
        alias_for_s=alias_for_s,
        remote_ip=remote_ip,
        assembler_key=assembler_key,
        raw_line=prefix + tag + sentence_text + suffix,
    )
    frame = frame_from_ingress_event(event)
    assert frame is not None
    parsed = parse_frame_sentences(frame, include_vdo=True)
    assert len(parsed) == 1
    return ParsedCase(parsed=parsed[0], sentence_text=sentence_text)


def make_bytes_case(
    sentence_bytes,
    *,
    assembler_key="source",
    prefix=b"",
    suffix=b"",
):
    frame = IngressFrame(
        kind="udpsec",
        source_id="bytes-input",
        alias_for_s=None,
        remote_ip=None,
        assembler_key=assembler_key,
        payload=prefix + sentence_bytes + suffix,
    )
    parsed = parse_frame_sentences(frame)
    assert len(parsed) == 1
    return ParsedCase(
        parsed=parsed[0],
        sentence_text=sentence_bytes.decode("utf-8", errors="ignore"),
    )


def test_parsed_fast_paths_match_legacy_without_clock_or_state_cleanup():
    harness = DifferentialHarness(
        timeout=1.0,
        max_fragments_per_group=2,
    )
    retained = make_case(
        sentence(2, 1, "retained", "first"),
        assembler_key="retained-source",
    )

    assert harness.feed(retained).status is AssemblyStatus.PENDING
    harness.at(1.0)

    invalid = make_case(
        "!AIVDM,x,1,,A,invalid,0*00",
        assembler_key="other-source",
    )
    single = make_case(
        sentence(1, 1, "", "single"),
        assembler_key="other-source",
    )
    limited = make_case(
        sentence(3, 1, "limited", "first"),
        assembler_key="other-source",
    )

    assert invalid.parsed.fragment is None
    assert harness.feed(invalid) == AssemblyOutcome(AssemblyStatus.INVALID)
    assert harness.feed(single) == AssemblyOutcome(
        AssemblyStatus.SINGLE,
        sentences=(single.sentence_text,),
    )
    assert harness.feed(limited) == AssemblyOutcome(
        AssemblyStatus.LIMIT_EXCEEDED,
    )
    assert harness.parsed_clock.calls == 1
    assert harness.parsed.stats().current_groups == 1
    assert harness.parsed.stats().expired == 0


def test_parsed_blank_id_completes_fully_out_of_order_with_progress_ttl():
    harness = DifferentialHarness(timeout=1.0)
    third = make_case(sentence(3, 3, "", "third"))
    first = make_case(sentence(3, 1, "", "first"))
    second = make_case(sentence(3, 2, "", "second"))

    assert harness.feed(third).status is AssemblyStatus.PENDING
    harness.at(0.75)
    assert harness.feed(first).status is AssemblyStatus.PENDING
    harness.at(1.5)
    complete = harness.feed(second)

    assert complete.status is AssemblyStatus.COMPLETE
    assert complete.group_key == ("source", "", "A", 3)
    assert complete.sentences == (
        first.sentence_text,
        second.sentence_text,
        third.sentence_text,
    )


def test_parsed_duplicate_does_not_refresh_matching_key_expiry():
    harness = DifferentialHarness(timeout=1.0)
    first = make_case(sentence(2, 1, "7", "first"))
    second = make_case(sentence(2, 2, "7", "second"))
    key = ("source", "7", "A", 2)

    assert harness.feed(first).status is AssemblyStatus.PENDING
    harness.at(0.75)
    assert harness.feed(first).status is AssemblyStatus.DUPLICATE
    harness.at(1.0)
    expired = harness.feed(second)

    assert expired.status is AssemblyStatus.PENDING
    assert expired.discarded_keys == (key,)

    harness.at(1.1)
    complete = harness.feed(first)
    assert complete.status is AssemblyStatus.COMPLETE
    assert complete.sentences == (first.sentence_text, second.sentence_text)


def test_parsed_conflict_opportunistically_expires_sorted_unrelated_keys():
    harness = DifferentialHarness(timeout=1.0)
    source_b = make_case(
        sentence(2, 1, "2", "source-b"),
        assembler_key="source-b",
    )
    source_a = make_case(
        sentence(2, 1, "1", "source-a"),
        assembler_key="source-a",
    )
    target = make_case(
        sentence(2, 1, "9", "target"),
        assembler_key="target",
    )
    conflict = make_case(
        sentence(2, 1, "9", "conflict"),
        assembler_key="target",
    )
    expected_keys = tuple(sorted((
        ("source-a", "1", "A", 2),
        ("source-b", "2", "A", 2),
        ("target", "9", "A", 2),
    )))

    harness.feed(source_b)
    harness.feed(source_a)
    harness.at(0.5)
    harness.feed(target)
    harness.at(1.0)
    outcome = harness.feed(conflict)

    assert outcome.status is AssemblyStatus.CONFLICT
    assert outcome.discarded_keys == expected_keys
    assert harness.parsed.stats().expired == 2
    assert harness.parsed.stats().conflicts == 1


def test_parsed_distinct_channels_keep_independent_groups():
    harness = DifferentialHarness()
    a_first = make_case(sentence(2, 1, "7", "a-first", channel="A"))
    b_second = make_case(sentence(2, 2, "7", "b-second", channel="B"))
    a_second = make_case(sentence(2, 2, "7", "a-second", channel="A"))
    b_first = make_case(sentence(2, 1, "7", "b-first", channel="B"))

    assert harness.feed(a_first).status is AssemblyStatus.PENDING
    assert harness.feed(b_second).status is AssemblyStatus.PENDING

    complete_a = harness.feed(a_second)
    complete_b = harness.feed(b_first)

    assert complete_a.group_key == ("source", "7", "A", 2)
    assert complete_a.sentences == (
        a_first.sentence_text,
        a_second.sentence_text,
    )
    assert complete_b.group_key == ("source", "7", "B", 2)
    assert complete_b.sentences == (
        b_first.sentence_text,
        b_second.sentence_text,
    )


def test_parsed_capacity_eviction_uses_deterministic_key_tiebreak():
    harness = DifferentialHarness(
        timeout=100.0,
        max_pending_groups=2,
    )
    harness.at(5.0)
    source_b = make_case(
        sentence(2, 1, "1", "source-b"),
        assembler_key="source-b",
    )
    source_a = make_case(
        sentence(2, 1, "2", "source-a"),
        assembler_key="source-a",
    )
    source_c = make_case(
        sentence(2, 1, "3", "source-c"),
        assembler_key="source-c",
    )
    key_a = ("source-a", "2", "A", 2)

    harness.feed(source_b)
    harness.feed(source_a)
    admitted = harness.feed(source_c)

    assert admitted.status is AssemblyStatus.PENDING
    assert admitted.discarded_keys == (key_a,)
    assert harness.parsed.stats().capacity_evicted == 1


def test_reset_after_parsed_input_matches_legacy_state_and_statistics():
    harness = DifferentialHarness()
    source_b = make_case(
        sentence(2, 1, "2", "source-b"),
        assembler_key="source-b",
    )
    source_a = make_case(
        sentence(3, 1, "1", "source-a"),
        assembler_key="source-a",
    )
    expected = (
        ("source-a", "1", "A", 3),
        ("source-b", "2", "A", 2),
    )

    harness.feed(source_b)
    harness.feed(source_a)

    assert harness.reset() == expected
    stats = harness.parsed.stats()
    assert stats.resets == 1
    assert stats.reset_discarded == 2
    assert stats.current_groups == 0
    assert stats.current_fragments == 0


def test_equivalent_mixed_sequences_produce_exact_statistics():
    harness = DifferentialHarness(max_fragments_per_group=2)
    invalid = make_case("!AIVDM,x,1,,A,invalid,0*00")
    single = make_case(sentence(1, 1, "", "single"))
    limited = make_case(sentence(3, 1, "limit", "limited"))
    first = make_case(sentence(2, 1, "7", "first"))
    conflict = make_case(sentence(2, 1, "7", "conflict"))
    second = make_case(sentence(2, 2, "7", "second"))

    assert harness.feed(invalid).status is AssemblyStatus.INVALID
    assert harness.feed(single).status is AssemblyStatus.SINGLE
    assert harness.feed(limited).status is AssemblyStatus.LIMIT_EXCEEDED
    assert harness.feed(first).status is AssemblyStatus.PENDING
    assert harness.feed(first).status is AssemblyStatus.DUPLICATE
    assert harness.feed(conflict).status is AssemblyStatus.CONFLICT
    assert harness.feed(first).status is AssemblyStatus.PENDING
    assert harness.feed(second).status is AssemblyStatus.COMPLETE

    stats = harness.parsed.stats()
    assert stats.invalid == 1
    assert stats.single == 1
    assert stats.limit_exceeded == 1
    assert stats.pending == 2
    assert stats.duplicates == 1
    assert stats.conflicts == 1
    assert stats.completed == 1


def test_assembler_key_excludes_ingress_and_tag_metadata():
    harness = DifferentialHarness()
    first_sentence = sentence(2, 1, "7", "first")
    second_sentence = sentence(2, 2, "7", "second")
    first = make_case(
        first_sentence,
        assembler_key="shared-key",
        source_id="input-a",
        alias_for_s="alias-a",
        remote_ip="192.0.2.1",
        tag="\\s:first,c:1,g:1-2-001\\",
    )
    duplicate = make_case(
        first_sentence,
        assembler_key="shared-key",
        source_id="input-b",
        alias_for_s="alias-b",
        remote_ip="198.51.100.2",
        tag="\\s:second,c:2,g:9-9-other\\",
    )
    second = make_case(
        second_sentence,
        assembler_key="shared-key",
        source_id="input-c",
        alias_for_s=None,
        remote_ip=None,
        tag="\\s:third,c:3,g:2-2-003\\",
    )

    assert first.parsed.tag != duplicate.parsed.tag
    pending = harness.feed(first)
    exact_duplicate = harness.feed(duplicate)
    complete = harness.feed(second)

    assert pending.group_key == ("shared-key", "7", "A", 2)
    assert exact_duplicate.status is AssemblyStatus.DUPLICATE
    assert complete.status is AssemblyStatus.COMPLETE
    assert complete.sentences == (first_sentence, second_sentence)


def test_frame_from_event_materializes_only_the_legacy_sentence():
    harness = DifferentialHarness()
    single = sentence(1, 1, "", "single")
    case = make_case(
        single,
        tag="\\s:station,c:123,g:1-1-007\\",
        prefix="vendor prefix ",
        suffix=" trailing text",
    )

    outcome = harness.feed(case)

    assert outcome == AssemblyOutcome(
        AssemblyStatus.SINGLE,
        sentences=(single,),
    )


def test_legacy_single_sentence_materialization_preserves_surrogate():
    harness = DifferentialHarness()
    single = sentence(1, 1, "", "payload\ud800")
    case = make_case(single)

    outcome = harness.feed(case)

    assert case.parsed.frame.text_mode is PayloadTextMode.UTF8_SURROGATEPASS
    assert outcome == AssemblyOutcome(
        AssemblyStatus.SINGLE,
        sentences=(single,),
    )


def test_legacy_multipart_storage_and_completion_preserve_surrogates():
    harness = DifferentialHarness()
    first_text = sentence(2, 1, "7", "first\ud800")
    second_text = sentence(2, 2, "7", "second\udfff")
    first = make_case(first_text)
    second = make_case(second_text)

    pending = harness.feed(first)
    complete = harness.feed(second)

    assert pending.status is AssemblyStatus.PENDING
    assert harness.legacy.stats() == harness.parsed.stats()
    assert complete.status is AssemblyStatus.COMPLETE
    assert complete.sentences == (first_text, second_text)


def test_invalid_utf8_materialization_ignores_only_bytes_in_sentence_span():
    sentence_bytes = b"!AIVDM,2\xff,1,7,A,pay\xffload,0*00"
    case = make_bytes_case(
        sentence_bytes,
        prefix=b"\xff\xfe outside ",
        suffix=b" outside \x80",
    )
    harness = DifferentialHarness()

    outcome = harness.feed(case)

    expected_sentence = "!AIVDM,2,1,7,A,payload,0*00"
    assert case.sentence_text == expected_sentence
    assert case.parsed.frame.text_mode is PayloadTextMode.UTF8_IGNORE
    assert outcome.status is AssemblyStatus.PENDING
    assert outcome.group_key == ("source", "7", "A", 2)
    group = harness.parsed._groups[outcome.group_key]
    assert group.fragments_by_ordinal == {1: expected_sentence}


def test_parsed_fragment_is_authoritative_over_sentence_comma_fields():
    first_text = "!AIVDM,9,8,text-seq,T,first,0*00"
    second_text = "!AIVDM,1,1,other,U,second,0*00"
    first_case = make_case(first_text, assembler_key="source")
    second_case = make_case(second_text, assembler_key="source")
    explicit_first = replace(
        first_case.parsed,
        fragment=ParsedFragment(
            declared_total=2,
            ordinal=1,
            sequential_id="meta-seq",
            channel="M",
        ),
    )
    explicit_second = replace(
        second_case.parsed,
        fragment=ParsedFragment(
            declared_total=2,
            ordinal=2,
            sequential_id="meta-seq",
            channel="M",
        ),
    )
    assembler = AIVDMAssembler(clock=FakeClock())

    pending = assembler.feed_parsed_outcome(explicit_first)
    complete = assembler.feed_parsed_outcome(explicit_second)

    assert pending.status is AssemblyStatus.PENDING
    assert pending.group_key == ("source", "meta-seq", "M", 2)
    assert complete.status is AssemblyStatus.COMPLETE
    assert complete.group_key == ("source", "meta-seq", "M", 2)
    assert complete.sentences == (first_text, second_text)


def test_legacy_feed_apis_keep_original_string_and_list_or_none_contract():
    class Sentence(str):
        pass

    single = Sentence(sentence(1, 1, "", "single"))
    outcome_assembler = AIVDMAssembler(clock=FakeClock())
    outcome = outcome_assembler.feed_outcome("source", single)

    assert outcome.status is AssemblyStatus.SINGLE
    assert outcome.sentences[0] is single
    assert outcome_assembler.stats().single == 1

    feed_assembler = AIVDMAssembler(clock=FakeClock())
    first = Sentence(sentence(2, 1, "7", "first"))
    second = Sentence(sentence(2, 2, "7", "second"))

    assert feed_assembler.feed("source", first) is None
    completed = feed_assembler.feed("source", second)

    assert isinstance(completed, list)
    assert completed == [first, second]
    assert completed[0] is first
    assert completed[1] is second
    assert feed_assembler.stats().pending == 1
    assert feed_assembler.stats().completed == 1
