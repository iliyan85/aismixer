from core.output_builder import build_output_bytes
from meta_writer import wrap_with_meta


SINGLE = "!AIVDM,1,1,,A,payload,0*00"
FIRST = "!AIVDM,2,1,7,A,payload1,0*00"
SECOND = "!AIVDM,2,2,7,A,payload2,0*00"


def test_build_output_bytes_returns_exact_single_sentence_bytes():
    payload = build_output_bytes(SINGLE, "boat", timestamp=123)

    assert type(payload) is bytes
    assert payload == (
        "\\c:123,s:boat*14\\" + SINGLE + "\r\n"
    ).encode("utf-8")


def test_build_output_bytes_appends_exactly_one_final_crlf():
    payload = build_output_bytes(SINGLE, "boat", timestamp=123)

    assert payload.endswith(b"\r\n")
    assert not payload.endswith(b"\r\n\r\n")
    assert payload.count(b"\r\n") == 1


def test_build_output_bytes_produces_exact_first_multipart_output():
    payload = build_output_bytes(
        FIRST,
        "boat",
        timestamp=123,
        is_first=True,
        g_triplet="1-2-99",
    )

    assert payload == (
        "\\c:123,s:boat,g:1-2-99*66\\" + FIRST + "\r\n"
    ).encode("utf-8")


def test_build_output_bytes_produces_exact_later_multipart_output():
    payload = build_output_bytes(
        SECOND,
        "boat",
        timestamp=123,
        is_first=False,
        g_triplet="2-2-99",
    )

    assert payload == (
        "\\g:2-2-99*5D\\" + SECOND + "\r\n"
    ).encode("utf-8")


def test_build_output_bytes_preserves_explicit_timestamp():
    payload = build_output_bytes(
        SINGLE,
        "boat",
        timestamp="987",
        clock=lambda: 456.9,
    )

    assert payload.startswith(b"\\c:987,s:boat*")


def test_build_output_bytes_uses_injected_fallback_clock():
    payload = build_output_bytes(
        SINGLE,
        "boat",
        clock=lambda: 456.9,
    )

    assert payload == (
        "\\c:456,s:boat*13\\" + SINGLE + "\r\n"
    ).encode("utf-8")


def test_build_output_bytes_does_not_observe_clock_for_truthy_timestamp():
    def fail_clock():
        raise AssertionError("clock must not be observed")

    payload = build_output_bytes(
        SINGLE,
        "boat",
        timestamp=123,
        clock=fail_clock,
    )

    assert payload.startswith(b"\\c:123,s:boat*")


def test_build_output_bytes_preserves_malformed_line_fallback_and_crlf():
    assert (
        build_output_bytes("malformed", "boat", timestamp=123)
        == b"malformed\r\n"
    )


def test_build_output_bytes_uses_utf8_for_complete_framed_text():
    payload = build_output_bytes(
        SINGLE,
        "b\u00e5t \u26f5",
        timestamp=123,
    )

    expected_text = wrap_with_meta(
        SINGLE,
        "b\u00e5t \u26f5",
        timestamp=123,
    ) + "\r\n"
    assert payload == expected_text.encode("utf-8")
    assert b"\xc3\xa5" in payload
    assert b"\xe2\x9b\xb5" in payload


def test_build_output_bytes_matches_canonical_string_formatter():
    expected_text = wrap_with_meta(
        FIRST,
        "boat",
        timestamp=123,
        is_first=True,
        g_triplet="1-2-99",
    )

    assert build_output_bytes(
        FIRST,
        "boat",
        timestamp=123,
        is_first=True,
        g_triplet="1-2-99",
    ) == (expected_text + "\r\n").encode("utf-8")
