from core.s_policy import (
    choose_s_value,
    extract_g_tuple,
    extract_incoming_s,
    ip_to_s,
    parse_tag_pairs_before_index,
    sanitize_s,
)


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
