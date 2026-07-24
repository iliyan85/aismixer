import pytest

from core.s_policy import (
    choose_s_value,
    choose_s_value_from_candidates,
    extract_g_tuple,
    extract_incoming_s,
    ip_to_s,
    parse_tag_pairs_before_index,
    sanitize_s,
)
from meta_cleaner import extract_nmea_sentences


def _tag_checksum(body):
    checksum = 0
    for character in body:
        checksum ^= ord(character)
    return f"{checksum:02X}"


def test_sanitize_s_replaces_unsafe_chars_and_truncates():
    assert sanitize_s(" Station-Name.With Spaces ") == "Station_Name_Wi"


def test_ip_to_s_converts_ip_punctuation_and_handles_missing_ip():
    assert ip_to_s("192.0.2.10") == "192_0_2_10"
    assert ip_to_s("2001:db8::1234") == "2001_db8__1234"
    assert ip_to_s(None) == "ANONYMOUS"


def test_extract_incoming_s_reads_leading_tag_only():
    assert extract_incoming_s(r"\c:123,s:boat-1*00\!AIVDM,1,1,,A,payload,0*00") == "boat-1"
    assert extract_incoming_s(r"prefix \s:boat-1*00\!AIVDM,1,1,,A,payload,0*00") is None
    assert extract_incoming_s("!AIVDM,1,1,,A,payload,0*00") is None


def test_parse_tag_pairs_before_index_uses_tag_immediately_before_sentence():
    first_sentence = "!AIVDM,1,1,,A,payload1,0*00"
    second_sentence = "!AIVDM,1,1,,B,payload2,0*00"
    raw = rf"\s:first,c:111*00\{first_sentence}\s:second,g:1-1-99*00\{second_sentence}"

    assert parse_tag_pairs_before_index(raw, raw.index(first_sentence)) == {
        "s": "first",
        "c": "111",
    }
    assert parse_tag_pairs_before_index(raw, raw.index(second_sentence)) == {
        "s": "second",
        "g": "1-1-99",
    }


def test_current_behavior_tag_without_checksum_is_associated_and_parsed():
    tag = "\\s:boat,c:123,g:1-1-9\\"
    sentence = "!AIVDM,1,1,,A,15Muq?002>G?svP00<:O?vN60<0,0*31"
    raw = tag + sentence

    slices = extract_nmea_sentences(raw, want_idx=True)

    assert len(slices) == 1
    assert slices[0].tag_end == slices[0].start - 1
    # Characterization only: checksum omission is not yet an approved policy.
    assert parse_tag_pairs_before_index(raw, slices[0].start) == {
        "s": "boat",
        "c": "123",
        "g": "1-1-9",
    }


def test_current_behavior_incorrect_tag_checksum_is_ignored_when_parsing():
    tag_body = "s:boat,c:123,g:1-1-9"
    supplied_checksum = "00"
    sentence = "!AIVDM,1,1,,A,15Muq?002>G?svP00<:O?vN60<0,0*31"
    tag = f"\\{tag_body}*{supplied_checksum}\\"
    raw = tag + sentence

    assert _tag_checksum(tag_body) == "5C"
    assert _tag_checksum(tag_body) != supplied_checksum

    slices = extract_nmea_sentences(raw, want_idx=True)

    assert len(slices) == 1
    assert raw[slices[0].tag_start:slices[0].tag_end + 1] == tag
    # Characterization only: acceptance does not approve the final checksum policy.
    assert parse_tag_pairs_before_index(raw, slices[0].start) == {
        "s": "boat",
        "c": "123",
        "g": "1-1-9",
    }


def test_correct_tag_checksum_is_associated_and_parsed():
    tag_body = "s:boat,c:123,g:1-1-9"
    supplied_checksum = "5C"
    sentence = "!AIVDM,1,1,,A,15Muq?002>G?svP00<:O?vN60<0,0*31"
    tag = f"\\{tag_body}*{supplied_checksum}\\"
    raw = tag + sentence

    assert _tag_checksum(tag_body) == supplied_checksum

    slices = extract_nmea_sentences(raw, want_idx=True)

    assert len(slices) == 1
    assert raw[slices[0].tag_start:slices[0].tag_end + 1] == tag
    assert parse_tag_pairs_before_index(raw, slices[0].start) == {
        "s": "boat",
        "c": "123",
        "g": "1-1-9",
    }


def test_extract_g_tuple_parses_valid_g_and_rejects_invalid_values():
    assert extract_g_tuple({"g": "1-2-788872464"}) == (1, 2, "788872464")
    assert extract_g_tuple({}) is None
    assert extract_g_tuple({"g": "not-a-group"}) is None
    assert extract_g_tuple({"g": "1-onlytwo"}) is None


def test_choose_s_value_uses_station_id_first():
    assert choose_s_value("global station", "input", r"\s:incoming*00\!AIVDM", "192.0.2.10") == "global_station"


def test_choose_s_value_uses_source_before_incoming_tag():
    assert choose_s_value(None, "input id", r"\s:incoming*00\!AIVDM", "192.0.2.10") == "input_id"


def test_choose_s_value_uses_incoming_tag_when_source_is_anonymous():
    assert choose_s_value(None, "ANONYMOUS", r"\s:incoming-id*00\!AIVDM", "192.0.2.10") == "incoming_id"


def test_choose_s_value_falls_back_to_ip():
    assert choose_s_value(None, None, "!AIVDM,1,1,,A,payload,0*00", "192.0.2.10") == "192_0_2_10"


@pytest.mark.parametrize(
    "global_station_id,source_name_or_id,expected",
    [
        ("global station", None, "global_station"),
        (None, "input id", "input_id"),
    ],
)
def test_higher_priority_source_candidates_do_not_inspect_raw_input(
    global_station_id,
    source_name_or_id,
    expected,
):
    class ExplodingRaw(str):
        def startswith(self, *_args, **_kwargs):
            raise AssertionError("incoming raw input must not be inspected")

    assert choose_s_value(
        global_station_id,
        source_name_or_id,
        ExplodingRaw("ignored"),
        "192.0.2.10",
    ) == expected


@pytest.mark.parametrize(
    "global_station_id,source_name_or_id,incoming_raw,remote_ip",
    [
        (
            " global station ",
            "input",
            r"\s:incoming*00\ignored",
            "192.0.2.10",
        ),
        (
            None,
            "input id",
            r"\s:incoming*00\ignored",
            "192.0.2.10",
        ),
        (
            "",
            "ANONYMOUS",
            r"\s:incoming-id*00\ignored",
            "192.0.2.10",
        ),
        (
            None,
            "",
            r"\s:*00\ignored",
            "192.0.2.10",
        ),
        (None, None, "not a leading tag", "2001:db8::1234"),
    ],
    ids=[
        "global",
        "source",
        "incoming",
        "empty-incoming",
        "remote-ip",
    ],
)
def test_explicit_candidate_helper_matches_legacy_source_policy(
    global_station_id,
    source_name_or_id,
    incoming_raw,
    remote_ip,
):
    incoming_s = extract_incoming_s(incoming_raw)

    assert choose_s_value_from_candidates(
        global_station_id,
        source_name_or_id,
        incoming_s,
        remote_ip,
    ) == choose_s_value(
        global_station_id,
        source_name_or_id,
        incoming_raw,
        remote_ip,
    )
