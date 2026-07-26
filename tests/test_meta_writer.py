from meta_writer import format_header, nmea_checksum, wrap_with_meta


def test_nmea_checksum_uses_nmea_xor():
    assert nmea_checksum("c:123,s:boat") == "14"


def test_format_header_wraps_content_with_checksum():
    assert format_header("c:123,s:boat") == "\\c:123,s:boat*14\\"


def test_wrap_with_meta_adds_c_and_s_with_explicit_timestamp():
    line = "!AIVDM,1,1,,A,payload,0*00"

    assert wrap_with_meta(line, "boat", timestamp=123) == "\\c:123,s:boat*14\\" + line


def test_wrap_with_meta_first_multipart_adds_c_s_and_g():
    line = "!AIVDM,2,1,7,A,payload1,0*00"

    assert (
        wrap_with_meta(line, "boat", timestamp=123, is_first=True, g_triplet="1-2-99")
        == "\\c:123,s:boat,g:1-2-99*66\\" + line
    )


def test_wrap_with_meta_later_multipart_adds_only_g():
    line = "!AIVDM,2,2,7,A,payload2,0*00"

    assert (
        wrap_with_meta(line, "boat", timestamp=123, is_first=False, g_triplet="2-2-99")
        == "\\g:2-2-99*5D\\" + line
    )


def test_wrap_with_meta_uses_injected_clock_for_fallback_timestamp():
    line = "!AIVDM,1,1,,A,payload,0*00"

    wrapped = wrap_with_meta(
        line,
        "boat",
        clock=lambda: 456.9,
    )

    assert wrapped == "\\c:456,s:boat*13\\" + line


def test_wrap_with_meta_does_not_observe_clock_for_truthy_timestamp():
    line = "!AIVDM,1,1,,A,payload,0*00"

    def fail_clock():
        raise AssertionError("clock must not be observed")

    assert (
        wrap_with_meta(
            line,
            "boat",
            timestamp=123,
            clock=fail_clock,
        )
        == "\\c:123,s:boat*14\\" + line
    )
