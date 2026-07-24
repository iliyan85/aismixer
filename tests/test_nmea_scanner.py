from dataclasses import FrozenInstanceError, fields

import pytest

from core.nmea_scanner import (
    ByteSpan,
    NMEAScanMatch,
    scan_nmea_sentences,
)
from meta_cleaner import extract_nmea_sentences


TALKERS = ("AI", "AB", "AD", "AN", "AR", "AS", "AT", "AX", "BS")


def sentence(
    talker="AI",
    family="VDM",
    checksum="00",
    ais_payload="payload",
):
    return f"!{talker}{family},1,1,,A,{ais_payload},0*{checksum}"


def assert_matches_legacy(text, include_vdo=False):
    payload = text.encode("utf-8")
    legacy_slices = extract_nmea_sentences(
        text,
        want_idx=True,
        include_vdo=include_vdo,
    )
    scan_matches = scan_nmea_sentences(payload, include_vdo=include_vdo)

    assert len(scan_matches) == len(legacy_slices)
    for scan_match, legacy_slice in zip(scan_matches, legacy_slices):
        sentence_start = len(text[:legacy_slice.start].encode("utf-8"))
        sentence_end = len(text[:legacy_slice.end].encode("utf-8"))
        expected_sentence_span = ByteSpan(sentence_start, sentence_end)

        assert scan_match.sentence_span == expected_sentence_span
        assert payload[
            scan_match.sentence_span.start:scan_match.sentence_span.end
        ] == text[legacy_slice.start:legacy_slice.end].encode("utf-8")

        if legacy_slice.tag_start == -1:
            assert scan_match.tag_span is None
            continue

        tag_start = len(text[:legacy_slice.tag_start].encode("utf-8"))
        tag_end = len(text[:legacy_slice.tag_end + 1].encode("utf-8"))
        expected_tag_span = ByteSpan(tag_start, tag_end)

        assert scan_match.tag_span == expected_tag_span
        assert payload[
            scan_match.tag_span.start:scan_match.tag_span.end
        ] == text[
            legacy_slice.tag_start:legacy_slice.tag_end + 1
        ].encode("utf-8")
        assert scan_match.tag_span.end == scan_match.sentence_span.start


def test_span_and_match_are_frozen_and_slots_based():
    span = ByteSpan(1, 2)
    match = NMEAScanMatch(sentence_span=span, tag_span=None)

    assert not hasattr(span, "__dict__")
    assert not hasattr(match, "__dict__")
    with pytest.raises(FrozenInstanceError):
        span.start = 0
    with pytest.raises(FrozenInstanceError):
        match.tag_span = span


@pytest.mark.parametrize("start,end", [(-1, 1), (0, 0), (2, 1)])
def test_byte_span_rejects_invalid_boundaries(start, end):
    with pytest.raises(ValueError):
        ByteSpan(start, end)


def test_exact_half_open_sentence_and_tag_boundaries():
    tag = b"\\s:alpha*00\\"
    nmea = sentence().encode("ascii")
    payload = b"prefix" + tag + nmea + b"suffix"

    matches = scan_nmea_sentences(payload)

    assert matches == (
        NMEAScanMatch(
            sentence_span=ByteSpan(
                len(b"prefix") + len(tag),
                len(b"prefix") + len(tag) + len(nmea),
            ),
            tag_span=ByteSpan(len(b"prefix"), len(b"prefix") + len(tag)),
        ),
    )
    assert payload[
        matches[0].sentence_span.start:matches[0].sentence_span.end
    ] == nmea
    assert payload[matches[0].tag_span.start:matches[0].tag_span.end] == tag


def test_no_match_returns_empty_tuple():
    assert scan_nmea_sentences(b"noise !GPGGA,1,2,3*00") == ()


def test_multiple_matches_are_returned_in_payload_order():
    first = sentence(talker="AB").encode("ascii")
    second = sentence(talker="AX", checksum="4b").encode("ascii")
    payload = b"noise " + first + b"\xff between " + second

    matches = scan_nmea_sentences(payload)

    assert tuple(
        payload[match.sentence_span.start:match.sentence_span.end]
        for match in matches
    ) == (first, second)


@pytest.mark.parametrize("talker", TALKERS)
def test_all_supported_talkers_are_scanned(talker):
    nmea = sentence(talker=talker).encode("ascii")

    matches = scan_nmea_sentences(nmea)

    assert len(matches) == 1
    assert matches[0].sentence_span == ByteSpan(0, len(nmea))


def test_vdo_is_selected_only_when_enabled():
    vdm = sentence(family="VDM").encode("ascii")
    vdo = sentence(family="VDO").encode("ascii")
    payload = vdo + b" " + vdm

    assert tuple(
        payload[match.sentence_span.start:match.sentence_span.end]
        for match in scan_nmea_sentences(payload)
    ) == (vdm,)
    assert tuple(
        payload[match.sentence_span.start:match.sentence_span.end]
        for match in scan_nmea_sentences(payload, include_vdo=True)
    ) == (vdo, vdm)


@pytest.mark.parametrize("checksum", ["AB", "4b", "a7"])
def test_uppercase_and_lowercase_hex_checksum_syntax_is_accepted(checksum):
    nmea = sentence(checksum=checksum).encode("ascii")

    assert scan_nmea_sentences(nmea) == (
        NMEAScanMatch(ByteSpan(0, len(nmea)), None),
    )


@pytest.mark.parametrize(
    "malformed",
    [
        b"!AIVDM,1,1,,A,payload,0",
        b"!AIVDM,1,1,,A,payload,0*",
        b"!AIVDM,1,1,,A,payload,0*0",
        b"!AIVDM,1,1,,A,payload,0*GG",
        b"!AIVDM;1,1,,A,payload,0*00",
        b"!ZZVDM,1,1,,A,payload,0*00",
    ],
)
def test_malformed_sentences_are_rejected(malformed):
    assert scan_nmea_sentences(malformed) == ()


@pytest.mark.parametrize("line_break", [b"\r", b"\n"])
def test_sentence_match_does_not_cross_cr_or_lf(line_break):
    payload = b"!AIVDM,1,1,,A,payload" + line_break + b",0*00"

    assert scan_nmea_sentences(payload) == ()


def test_lazy_match_can_include_a_malformed_prefix_on_the_same_line():
    payload = b"!AIVDM,bad!AIVDM,good*00"

    matches = scan_nmea_sentences(payload)

    assert matches == (
        NMEAScanMatch(ByteSpan(0, len(payload)), None),
    )


def test_checksum_syntax_has_no_trailing_boundary():
    matched = b"!AIVDM,payload*00"
    payload = matched + b"12"

    matches = scan_nmea_sentences(payload)

    assert matches == (
        NMEAScanMatch(ByteSpan(0, len(matched)), None),
    )


def test_invalid_early_star_remains_inside_match_until_valid_checksum_syntax():
    matched = b"!AIVDM,payload*0G*ab"
    payload = matched + b" trailing"

    matches = scan_nmea_sentences(payload)

    assert matches == (
        NMEAScanMatch(ByteSpan(0, len(matched)), None),
    )


def test_adjacent_tag_uses_nearest_preceding_opening_backslash():
    nmea = sentence().encode("ascii")
    payload = b"\\old\\nearest\\tag\\" + nmea

    match = scan_nmea_sentences(payload)[0]

    assert payload[match.tag_span.start:match.tag_span.end] == b"\\tag\\"
    assert match.tag_span.end == match.sentence_span.start


@pytest.mark.parametrize("tag", [b"\\\\", b"\\open\r\narbitrary bytes\\"])
def test_tag_opening_lookup_preserves_legacy_unrestricted_search(tag):
    nmea = sentence().encode("ascii")

    match = scan_nmea_sentences(tag + nmea)[0]

    assert match.tag_span == ByteSpan(0, len(tag))


@pytest.mark.parametrize(
    "prefix",
    [
        b"\\incomplete",
        b"\\complete\\ ",
        b"single-closing\\",
    ],
)
def test_incomplete_or_non_adjacent_tag_is_not_associated(prefix):
    nmea = sentence().encode("ascii")

    match = scan_nmea_sentences(prefix + nmea)[0]

    assert match.tag_span is None


def test_invalid_utf8_outside_sentences_is_tolerated_without_decoding():
    nmea = sentence(talker="BS", checksum="aF").encode("ascii")
    payload = b"\xff\xfe\x80" + nmea + b"\xc0\xaf"

    matches = scan_nmea_sentences(payload)

    assert matches == (
        NMEAScanMatch(
            sentence_span=ByteSpan(3, 3 + len(nmea)),
            tag_span=None,
        ),
    )


def test_scan_results_store_only_spans_not_copied_payload_bytes():
    nmea = sentence().encode("ascii")
    match = scan_nmea_sentences(b"prefix" + nmea)[0]

    assert tuple(field.name for field in fields(match)) == (
        "sentence_span",
        "tag_span",
    )
    assert all(
        value is None or isinstance(value, ByteSpan)
        for value in (match.sentence_span, match.tag_span)
    )


@pytest.mark.parametrize(
    "text,include_vdo",
    [
        ("no accepted sentence", False),
        (sentence(), False),
        (sentence(family="VDO"), False),
        (sentence(family="VDO"), True),
        (f"prefix {sentence()} suffix", False),
        (f"{sentence(talker='AB')} noise {sentence(talker='AX')}", False),
        (f"\\s:alpha,c:1*00\\{sentence()}", False),
        (
            f"\\s:alpha*00\\{sentence(talker='AN')}"
            f"noise\\s:beta*00\\{sentence(talker='BS')}",
            False,
        ),
        (sentence(checksum="AF"), False),
        (sentence(checksum="4b"), False),
        ("!AIVDM,1,1,,A,payload,0", False),
        ("!AIVDM,1,1,,A,payload,0*0", False),
        ("!AIVDM,1,1,,A,payload,0*GG", False),
        (f"!AIVDM,1,1,,A,payload\r,0*00\n{sentence()}", False),
        ("!AIVDM,bad!AIVDM,good*00", False),
        ("!AIVDM,payload*0012", False),
        ("!AIVDM,payload*0G*ab", False),
        (f"\\s:incomplete{sentence()}", False),
        (f"\\s:alpha*00\\ {sentence()}", False),
        (f"\\\\{sentence()}", False),
        (f"\\open\r\narbitrary text\\{sentence()}", False),
        (f"преди {sentence()} след", False),
        (
            f"é {sentence(talker='AB')} Ω "
            f"\\s:лодка*00\\{sentence(talker='AX')}",
            False,
        ),
    ],
)
def test_scanner_agrees_with_legacy_extractor(text, include_vdo):
    assert_matches_legacy(text, include_vdo=include_vdo)


@pytest.mark.parametrize("talker", TALKERS)
def test_each_supported_talker_agrees_with_legacy_extractor(talker):
    assert_matches_legacy(sentence(talker=talker))
