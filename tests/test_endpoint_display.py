from core.endpoint_display import format_endpoint, format_endpoint_tuple


def test_format_endpoint_renders_ipv4_with_colon():
    assert format_endpoint("192.0.2.10", 17778) == "192.0.2.10:17778"


def test_format_endpoint_renders_ipv6_tcpdump_style_with_dot():
    assert format_endpoint("2001:db8::10", 17778) == "2001:db8::10.17778"


def test_format_endpoint_ipv6_loopback():
    assert format_endpoint("::1", 50000) == "::1.50000"


def test_format_endpoint_does_not_use_uri_bracket_notation():
    rendered = format_endpoint("2001:db8::10", 17778)
    assert "[" not in rendered
    assert "]" not in rendered


def test_format_endpoint_tuple_uses_only_ip_and_port():
    assert format_endpoint_tuple(("192.0.2.10", 17778)) == "192.0.2.10:17778"
    assert (
        format_endpoint_tuple(("2001:db8::10", 17778))
        == "2001:db8::10.17778"
    )


def test_format_endpoint_tuple_ignores_ipv6_flowinfo_and_scope_id():
    # Display only: a raw 4-tuple's trailing flowinfo/scope_id must not
    # appear in, or otherwise affect, the rendered string.
    assert format_endpoint_tuple(("fe80::1", 12345, 0, 3)) == "fe80::1.12345"


def test_formatted_string_loses_information_the_wire_format_may_need():
    """Two structurally different IPv6 tuples (differing only in flowinfo or
    scope_id) can render to the identical display string. This is exactly
    why the formatted string must never be used as session/path identity,
    a dict key, or anything parsed back into an endpoint -- structured
    (ip, port, ...) data is the only safe source of truth for that."""

    rendered_a = format_endpoint_tuple(("2001:db8::10", 17778, 0, 0))
    rendered_b = format_endpoint_tuple(("2001:db8::10", 17778, 5, 7))

    assert rendered_a == rendered_b == "2001:db8::10.17778"
