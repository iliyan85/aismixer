from dataclasses import FrozenInstanceError, fields

import pytest

from assembler import AIVDMAssembler, AssemblyStatus
from core.event import IngressEvent
from core.ingress_frame import IngressFrame, frame_from_ingress_event
from core.nmea_scanner import ByteSpan, NMEAScanMatch, scan_nmea_sentences
from core.parsed_sentence import (
    ParsedFragment,
    ParsedGroupTag,
    ParsedSentence,
    ParsedTagMetadata,
    parse_frame_sentences,
    parse_scanned_sentence,
)
from core.s_policy import extract_g_tuple, parse_tag_pairs_before_index
from meta_cleaner import extract_nmea_sentences


SENTENCE = "!AIVDM,1,1,,A,payload,0*00"


def make_frame(payload):
    return IngressFrame(
        kind="udpsec",
        source_id="station:alpha",
        alias_for_s="alpha",
        remote_ip="192.0.2.10",
        assembler_key="udpsec:station:alpha",
        payload=payload,
    )


def frame_from_legacy(raw):
    event = IngressEvent(
        kind="udp",
        source_id="udp:primary",
        alias_for_s=None,
        remote_ip="192.0.2.20",
        assembler_key="udp:primary",
        raw_line=raw,
    )
    frame = frame_from_ingress_event(event)
    assert frame is not None
    return frame


def parse_one_legacy(raw):
    parsed = parse_frame_sentences(frame_from_legacy(raw))
    assert len(parsed) == 1
    return parsed[0]


def expected_tag_from_legacy(raw):
    legacy_slices = extract_nmea_sentences(raw, want_idx=True)
    assert len(legacy_slices) == 1
    legacy_slice = legacy_slices[0]
    pairs = (
        parse_tag_pairs_before_index(raw, legacy_slice.start)
        if legacy_slice.tag_end >= 0
        else {}
    )

    c_text = pairs.get("c")
    c_value = None
    if c_text and c_text.isdigit():
        try:
            c_value = int(c_text)
        except ValueError:
            c_value = None

    g_tuple = extract_g_tuple(pairs)
    g_value = None
    if g_tuple is not None:
        part, total, group_id = g_tuple
        g_value = ParsedGroupTag(
            part=part,
            total=total,
            group_id=group_id,
            preservable_group_id=(
                group_id
                if group_id and group_id.isdigit()
                else None
            ),
        )

    return ParsedTagMetadata(
        s_value=pairs.get("s"),
        c_text=c_text,
        c_value=c_value,
        g_value=g_value,
    )


def test_parsed_representations_are_frozen_and_slots_based():
    parsed = parse_one_legacy(
        "\\s:alpha,c:1,g:1-1-007*00\\" + SENTENCE
    )
    values_and_fields = (
        (parsed.fragment, "ordinal", 2),
        (parsed.tag.g_value, "part", 2),
        (parsed.tag, "s_value", "changed"),
        (parsed, "fragment", None),
    )

    for value, field_name, replacement in values_and_fields:
        assert value is not None
        assert not hasattr(value, "__dict__")
        with pytest.raises(FrozenInstanceError):
            setattr(value, field_name, replacement)
    assert tuple(field.name for field in fields(ParsedTagMetadata)) == (
        "s_value",
        "c_text",
        "c_value",
        "g_value",
    )


def test_one_match_parser_retains_original_frame_match_and_spans():
    raw = ("prefix " + SENTENCE).encode("ascii")
    frame = make_frame(raw)
    match = scan_nmea_sentences(frame.payload)[0]

    parsed = parse_scanned_sentence(frame, match)

    assert parsed.frame is frame
    assert parsed.match is match
    assert frame.payload[
        parsed.match.sentence_span.start:parsed.match.sentence_span.end
    ] == SENTENCE.encode("ascii")
    assert tuple(field.name for field in fields(ParsedSentence)) == (
        "frame",
        "match",
        "fragment",
        "tag",
    )


def test_frame_parser_returns_tuple_and_controls_vdo_selection():
    vdo = b"!AIVDO,1,1,,A,self,0*00"
    vdm = SENTENCE.encode("ascii")
    frame = make_frame(vdo + b" " + vdm)

    default_results = parse_frame_sentences(frame)
    vdo_results = parse_frame_sentences(frame, include_vdo=True)

    assert isinstance(default_results, tuple)
    assert tuple(
        frame.payload[
            parsed.match.sentence_span.start:parsed.match.sentence_span.end
        ]
        for parsed in default_results
    ) == (vdm,)
    assert tuple(
        frame.payload[
            parsed.match.sentence_span.start:parsed.match.sentence_span.end
        ]
        for parsed in vdo_results
    ) == (vdo, vdm)


@pytest.mark.parametrize(
    "sentence,expected",
    [
        (
            "!AIVDM,1,1,,A,payload,0*00",
            ParsedFragment(1, 1, "", "A"),
        ),
        (
            "!AIVDM,2,1,7,B,payload,0*00",
            ParsedFragment(2, 1, "7", "B"),
        ),
        (
            "!AIVDM,2,2,,A,payload,0*00",
            ParsedFragment(2, 2, "", "A"),
        ),
        (
            "!AIVDM,2,1, 07 , B ,payload,0*00",
            ParsedFragment(2, 1, " 07 ", " B "),
        ),
        (
            "!AIVDM,\t+2\u2003,\u2003+1\t,seq,C,payload,0*00",
            ParsedFragment(2, 1, "seq", "C"),
        ),
        (
            "!AIVDM,٢,١,seq,К,payload,0*00",
            ParsedFragment(2, 1, "seq", "К"),
        ),
        (
            "!AIVDM,2_0,1,seq,A,payload,0*00",
            ParsedFragment(20, 1, "seq", "A"),
        ),
        (
            "!AIVDM,1,1,seq,A,payload,0,extra*00",
            ParsedFragment(1, 1, "seq", "A"),
        ),
        (
            "!AIVDM,1000000,1,seq,A,payload,0*00",
            ParsedFragment(1000000, 1, "seq", "A"),
        ),
        ("!AIVDM,1,1,,A,payload*00", None),
        ("!AIVDM,x,1,,A,payload,0*00", None),
        ("!AIVDM,1,x,,A,payload,0*00", None),
        ("!AIVDM,,1,,A,payload,0*00", None),
        ("!AIVDM,1,,,A,payload,0*00", None),
        ("!AIVDM,0,1,,A,payload,0*00", None),
        ("!AIVDM,2,0,,A,payload,0*00", None),
        ("!AIVDM,2,3,,A,payload,0*00", None),
        ("!AIVDM,-1,1,,A,payload,0*00", None),
        ("!AIVDM,2,-1,,A,payload,0*00", None),
        ("!AIVDM,1.0,1,,A,payload,0*00", None),
        ("!AIVDM,0x2,1,,A,payload,0*00", None),
        ("!AIVDM,²,1,,A,payload,0*00", None),
    ],
)
def test_fragment_parsing_matches_assembler_structure(sentence, expected):
    parsed = parse_one_legacy(sentence)
    legacy_outcome = AIVDMAssembler().feed_outcome("source", sentence)

    assert parsed.fragment == expected
    assert (legacy_outcome.status is AssemblyStatus.INVALID) is (
        expected is None
    )
    if expected is not None and expected.declared_total > 1:
        assert legacy_outcome.group_key == (
            "source",
            expected.sequential_id,
            expected.channel,
            expected.declared_total,
        )


def test_valid_multipart_fragments_are_parsed_independently():
    first = "!AIVDM,2,1,7,A,first,0*00"
    second = "!AIVDM,2,2,7,A,second,0*00"
    frame = frame_from_legacy(first + "\n" + second)

    parsed = parse_frame_sentences(frame)

    assert tuple(item.fragment for item in parsed) == (
        ParsedFragment(2, 1, "7", "A"),
        ParsedFragment(2, 2, "7", "A"),
    )


def test_scanner_match_with_invalid_fragment_still_produces_parsed_sentence():
    raw = "!AIVDM,1,x,,A,payload,0*00"

    parsed = parse_one_legacy(raw)

    assert isinstance(parsed, ParsedSentence)
    assert parsed.fragment is None
    assert parsed.tag == ParsedTagMetadata(
        s_value=None,
        c_text=None,
        c_value=None,
        g_value=None,
    )


@pytest.mark.parametrize(
    "tag,expected_s",
    [
        ("", None),
        ("\\\\", None),
        ("\\x:y,no-colon,,q:z*00\\", None),
        ("\\s:first,s:second\\", "second"),
        ("\\s:first,s:\\", ""),
        ("\\s:first,s\\", "first"),
        ("\\s:a:b\\", "a:b"),
        ("\\s:before*00,s:after\\", "before"),
    ],
)
def test_tag_pair_and_s_semantics_match_legacy(tag, expected_s):
    raw = tag + SENTENCE

    parsed = parse_one_legacy(raw)

    assert parsed.tag == expected_tag_from_legacy(raw)
    assert parsed.tag.s_value == expected_s


@pytest.mark.parametrize(
    "tag,expected_c_text,expected_c_value",
    [
        ("", None, None),
        ("\\c:\\", "", None),
        ("\\c:bad\\", "bad", None),
        ("\\c:0\\", "0", 0),
        ("\\c:00012\\", "00012", 12),
        ("\\c:+1\\", "+1", None),
        ("\\c:-1\\", "-1", None),
        ("\\c: 1 \\", " 1 ", None),
        ("\\c:1_0\\", "1_0", None),
        ("\\c:٠٠١\\", "٠٠١", 1),
        ("\\c:²\\", "²", None),
        ("\\c:1,c:\\", "", None),
        ("\\c:1,c:bad\\", "bad", None),
        ("\\c:bad,c:2\\", "2", 2),
    ],
)
def test_tag_c_semantics_match_legacy_candidate_rule(
    tag,
    expected_c_text,
    expected_c_value,
):
    raw = tag + SENTENCE

    parsed = parse_one_legacy(raw)

    assert parsed.tag == expected_tag_from_legacy(raw)
    assert parsed.tag.c_text == expected_c_text
    assert parsed.tag.c_value == expected_c_value


@pytest.mark.parametrize(
    "tag,expected_g",
    [
        (
            "\\g:1-2-007\\",
            ParsedGroupTag(1, 2, "007", "007"),
        ),
        (
            "\\g:1-2-name\\",
            ParsedGroupTag(1, 2, "name", None),
        ),
        (
            "\\g:1-2-\\",
            ParsedGroupTag(1, 2, "", None),
        ),
        ("\\g:bad\\", None),
        ("\\g:1-onlytwo\\", None),
        (
            "\\g:0-0-0\\",
            ParsedGroupTag(0, 0, "0", "0"),
        ),
        (
            "\\g:+1- 2 -001\\",
            ParsedGroupTag(1, 2, "001", "001"),
        ),
        (
            "\\g:١-٢-٠٠٣\\",
            ParsedGroupTag(1, 2, "٠٠٣", "٠٠٣"),
        ),
        (
            "\\g:1-2-a-b\\",
            ParsedGroupTag(1, 2, "a-b", None),
        ),
        (
            "\\g:1-2-²\\",
            ParsedGroupTag(1, 2, "²", "²"),
        ),
        ("\\g:1-2-7,g:bad\\", None),
        (
            "\\g:bad,g:2-3-9\\",
            ParsedGroupTag(2, 3, "9", "9"),
        ),
    ],
)
def test_tag_g_semantics_match_legacy_structure(tag, expected_g):
    raw = tag + SENTENCE

    parsed = parse_one_legacy(raw)

    assert parsed.tag == expected_tag_from_legacy(raw)
    assert parsed.tag.g_value == expected_g


def test_tag_g_and_fragment_disagreement_is_preserved():
    raw = "\\g:9-8-0007\\" + "!AIVDM,2,1,seq,A,payload,0*00"

    parsed = parse_one_legacy(raw)

    assert parsed.fragment == ParsedFragment(2, 1, "seq", "A")
    assert parsed.tag.g_value == ParsedGroupTag(9, 8, "0007", "0007")


def test_non_adjacent_tag_is_not_parsed():
    raw = "\\s:stale,c:1,g:1-1-9\\ separated " + SENTENCE

    parsed = parse_one_legacy(raw)

    assert parsed.match.tag_span is None
    assert parsed.tag == ParsedTagMetadata(
        s_value=None,
        c_text=None,
        c_value=None,
        g_value=None,
    )


def test_utf8_ignore_policy_applies_only_to_matched_parsing_views():
    tag = b"\\s:bo\xffat,c:0\xff01,g:1-2-0\xff07\\"
    sentence = b"!AIVDM,2\xff,1,seq\xffid, B\xff ,payload,0*00"
    payload = b"\xfe outside " + tag + sentence + b" trailing \x80"
    frame = make_frame(payload)

    parsed = parse_frame_sentences(frame)

    assert len(parsed) == 1
    assert parsed[0].fragment == ParsedFragment(2, 1, "seqid", " B ")
    assert parsed[0].tag == ParsedTagMetadata(
        s_value="boat",
        c_text="001",
        c_value=1,
        g_value=ParsedGroupTag(1, 2, "007", "007"),
    )


def test_span_validation_accepts_scanner_output():
    frame = frame_from_legacy("\\s:alpha\\" + SENTENCE)
    match = scan_nmea_sentences(frame.payload)[0]

    assert parse_scanned_sentence(frame, match).match is match


def test_span_validation_rejects_sentence_outside_payload():
    frame = make_frame(SENTENCE.encode("ascii"))
    match = NMEAScanMatch(
        sentence_span=ByteSpan(0, len(frame.payload) + 1),
        tag_span=None,
    )

    with pytest.raises(ValueError, match="sentence span"):
        parse_scanned_sentence(frame, match)


def test_span_validation_rejects_sentence_not_beginning_with_bang():
    frame = make_frame(b"x" + SENTENCE.encode("ascii"))
    match = NMEAScanMatch(
        sentence_span=ByteSpan(0, len(frame.payload)),
        tag_span=None,
    )

    with pytest.raises(ValueError, match="begin"):
        parse_scanned_sentence(frame, match)


def test_span_validation_rejects_tag_outside_payload():
    payload = ("\\s:a\\" + SENTENCE).encode("ascii")
    frame = make_frame(payload)
    scanner_match = scan_nmea_sentences(payload)[0]
    match = NMEAScanMatch(
        sentence_span=scanner_match.sentence_span,
        tag_span=ByteSpan(0, len(payload) + 1),
    )

    with pytest.raises(ValueError, match="TAG span"):
        parse_scanned_sentence(frame, match)


@pytest.mark.parametrize("tag_end_delta", [-1, 1])
def test_span_validation_rejects_tag_gap_or_sentence_overlap(tag_end_delta):
    payload = ("\\s:a\\" + SENTENCE).encode("ascii")
    frame = make_frame(payload)
    scanner_match = scan_nmea_sentences(payload)[0]
    sentence_start = scanner_match.sentence_span.start
    match = NMEAScanMatch(
        sentence_span=scanner_match.sentence_span,
        tag_span=ByteSpan(0, sentence_start + tag_end_delta),
    )

    with pytest.raises(ValueError, match="end at sentence start"):
        parse_scanned_sentence(frame, match)


def test_span_validation_rejects_missing_tag_delimiters():
    payload = ("xs:a\\" + SENTENCE).encode("ascii")
    frame = make_frame(payload)
    sentence_start = payload.index(b"!")
    match = NMEAScanMatch(
        sentence_span=ByteSpan(sentence_start, len(payload)),
        tag_span=ByteSpan(0, sentence_start),
    )

    with pytest.raises(ValueError, match="delimiters"):
        parse_scanned_sentence(frame, match)
