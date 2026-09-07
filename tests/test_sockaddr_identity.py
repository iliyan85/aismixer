import socket

import pytest

from core.sockaddr_identity import (
    MalformedSockaddrError,
    SockaddrIdentity,
    normalize_sockaddr,
    sockaddrs_match,
)


# IPv4 identity


def test_ipv4_two_tuples_with_same_ip_and_port_match():
    assert sockaddrs_match(("192.0.2.10", 17778), ("192.0.2.10", 17778))


def test_ipv4_different_port_does_not_match():
    assert not sockaddrs_match(("192.0.2.10", 1), ("192.0.2.10", 2))


def test_ipv4_different_ip_does_not_match():
    assert not sockaddrs_match(("192.0.2.10", 1), ("192.0.2.11", 1))


def test_ipv4_four_tuple_shape_is_rejected():
    with pytest.raises(MalformedSockaddrError):
        normalize_sockaddr(("192.0.2.10", 17778, 0, 0))


# IPv6 identity: flowinfo excluded, scope_id included


def test_ipv6_four_tuples_differing_only_in_flowinfo_match():
    assert sockaddrs_match(
        ("2001:db8::1", 5000, 1, 2),
        ("2001:db8::1", 5000, 99, 2),
    )


def test_ipv6_four_tuples_differing_in_scope_id_do_not_match():
    assert not sockaddrs_match(
        ("fe80::1", 5000, 0, 2),
        ("fe80::1", 5000, 0, 3),
    )


def test_ipv6_missing_scope_defaults_to_zero_explicitly():
    """A bare 2-tuple IPv6 address is treated as scope_id 0 -- not as a
    wildcard matching any scope. This is the exact defect the audit
    reported: missing scope must never behave as an arbitrary-scope
    wildcard."""
    assert sockaddrs_match(("fe80::1", 5000), ("fe80::1", 5000, 0, 0))
    assert sockaddrs_match(("fe80::1", 5000), ("fe80::1", 5000, 12345, 0))
    assert not sockaddrs_match(("fe80::1", 5000), ("fe80::1", 5000, 0, 1))
    assert not sockaddrs_match(("fe80::1", 5000), ("fe80::1", 5000, 0, 3))


def test_ipv6_two_tuple_and_native_four_tuple_scope_zero_are_the_same_path():
    """Native mixed shapes: a 2-tuple and a native 4-tuple with scope_id 0
    are intentionally the same identity."""
    assert normalize_sockaddr(("2001:db8::1", 5000)) == normalize_sockaddr(
        ("2001:db8::1", 5000, 0, 0)
    )


# Family mismatch and mapped addresses


def test_ipv4_and_ipv6_never_match_even_with_equal_port():
    assert not sockaddrs_match(("192.0.2.1", 100), ("::1", 100))


def test_ipv4_mapped_ipv6_is_ipv6_identity_not_collapsed_to_ipv4():
    mapped = normalize_sockaddr(("::ffff:192.0.2.1", 100))
    assert mapped.family == socket.AF_INET6
    assert not sockaddrs_match(("::ffff:192.0.2.1", 100), ("192.0.2.1", 100))


# Equivalent spellings normalize to the same identity


def test_equivalent_ipv6_spellings_normalize_identically():
    assert normalize_sockaddr(("2001:DB8::0:1", 100)) == normalize_sockaddr(
        ("2001:db8::1", 100)
    )


def test_equivalent_ipv6_spellings_match_via_sockaddrs_match():
    assert sockaddrs_match(("2001:DB8::0:1", 100), ("2001:db8::1", 100))


# Canonical value type and re-normalization safety


def test_normalize_sockaddr_returns_a_typed_value_not_a_plain_tuple():
    result = normalize_sockaddr(("192.0.2.1", 100))
    assert isinstance(result, SockaddrIdentity)
    assert not isinstance(result, tuple)


def test_canonical_value_is_rejected_if_fed_back_in():
    """The canonical IPv6 output and a native raw IPv6 4-tuple are both 4
    elements long; without a distinct type, a canonical value fed back in
    would be silently misparsed (its own `family` int reinterpreted as an
    IP string, etc). Explicit rejection makes that mistake impossible."""
    canonical = normalize_sockaddr(("2001:db8::1", 5000, 0, 3))
    with pytest.raises(MalformedSockaddrError):
        normalize_sockaddr(canonical)


def test_sockaddrs_match_rejects_a_canonical_value_argument():
    canonical = normalize_sockaddr(("192.0.2.1", 100))
    with pytest.raises(MalformedSockaddrError):
        sockaddrs_match(canonical, ("192.0.2.1", 100))


# Port validation


@pytest.mark.parametrize("bad_port", [-1, 65536, True, False, "17778", 1.5, None])
def test_invalid_port_is_rejected(bad_port):
    with pytest.raises(MalformedSockaddrError):
        normalize_sockaddr(("192.0.2.1", bad_port))


@pytest.mark.parametrize("good_port", [0, 1, 17778, 65535])
def test_boundary_and_typical_ports_are_accepted(good_port):
    result = normalize_sockaddr(("192.0.2.1", good_port))
    assert result.port == good_port


# Flowinfo / scope_id validation on the native IPv6 4-tuple


_MAX_UINT32 = (1 << 32) - 1


@pytest.mark.parametrize(
    "bad_flowinfo",
    [-1, True, False, "0", 1.5, None, _MAX_UINT32 + 1, 1 << 40],
)
def test_invalid_flowinfo_is_rejected(bad_flowinfo):
    with pytest.raises(MalformedSockaddrError):
        normalize_sockaddr(("fe80::1", 5000, bad_flowinfo, 0))


@pytest.mark.parametrize(
    "bad_scope_id",
    [-1, True, False, "0", 1.5, None, _MAX_UINT32 + 1, 1 << 40],
)
def test_invalid_scope_id_is_rejected(bad_scope_id):
    with pytest.raises(MalformedSockaddrError):
        normalize_sockaddr(("fe80::1", 5000, 0, bad_scope_id))


@pytest.mark.parametrize("good_flowinfo", [0, 1, 12345, _MAX_UINT32])
def test_boundary_flowinfo_values_are_accepted(good_flowinfo):
    """The bound is the real 32-bit `sin6_flowinfo` field width, not the
    narrower 20-bit semantic flow-label -- a value using the high-order
    bits a real OS could still legitimately hand back must not be
    rejected, since this module discards flowinfo rather than
    interpreting it."""
    result = normalize_sockaddr(("fe80::1", 5000, good_flowinfo, 0))
    assert result.port == 5000


@pytest.mark.parametrize("good_scope_id", [0, 1, 12345, _MAX_UINT32])
def test_boundary_scope_id_values_are_accepted(good_scope_id):
    result = normalize_sockaddr(("fe80::1", 5000, 0, good_scope_id))
    assert result.scope_id == good_scope_id


# Malformed / ambiguous shapes fail closed rather than matching or
# safely-not-matching by accident


@pytest.mark.parametrize(
    "malformed",
    [
        ("192.0.2.1", 100, 0),  # 3-tuple: not a supported shape
        ("a", "b", "c"),
        5,
        "192.0.2.1:100",
        ("192.0.2.1",),
        (123, 100),  # IP is not a string
        (b"192.0.2.1", 100),  # IP is bytes, not str
        ("not-an-ip", 100),
        ("fe80::1%eth0", 100),  # zone-qualified string, not a 4-tuple
        ("fe80::1%3", 100, 0, 3),  # zone in string AND a 4-tuple scope
        [192, 0, 2, 1],
        None,
    ],
)
def test_malformed_or_ambiguous_sockaddrs_fail_closed(malformed):
    with pytest.raises(MalformedSockaddrError):
        normalize_sockaddr(malformed)


def test_malformed_argument_never_silently_matches_a_valid_one():
    with pytest.raises(MalformedSockaddrError):
        sockaddrs_match(("192.0.2.1", 100, 0), ("192.0.2.1", 100))


# Index/hash equality: SockaddrIdentity (or a key built from it) must be
# usable as a dict/set key with equal-spelling and equal-shape inputs
# hashing and comparing identically -- this is exactly the property
# SecureState's relation index relies on.


def test_equal_canonical_values_hash_and_index_identically():
    a = normalize_sockaddr(("2001:DB8::0:1", 100, 0, 7))
    b = normalize_sockaddr(("2001:db8::1", 100, 5, 7))
    assert a == b
    assert hash(a) == hash(b)
    index = {a: "first"}
    index[b] = "second"
    assert index == {a: "second"}
    assert len(index) == 1


def test_distinct_scope_id_values_index_separately():
    a = normalize_sockaddr(("fe80::1", 100, 0, 1))
    b = normalize_sockaddr(("fe80::1", 100, 0, 2))
    assert a != b
    index = {a: "scope-1", b: "scope-2"}
    assert len(index) == 2
    assert index[a] == "scope-1"
    assert index[b] == "scope-2"


# Mixed native shapes: a 2-tuple and native 4-tuple inputs of both
# families normalize consistently regardless of which shape is used for
# which operand.


def test_mixed_two_tuple_and_four_tuple_operands_compare_consistently():
    two_tuple = ("2001:db8::5", 4000)
    four_tuple = ("2001:db8::5", 4000, 42, 0)
    assert sockaddrs_match(two_tuple, four_tuple)
    assert sockaddrs_match(four_tuple, two_tuple)
    assert normalize_sockaddr(two_tuple) == normalize_sockaddr(four_tuple)


def test_ipv4_two_tuple_is_not_conflated_with_ipv6_four_tuple_length():
    """Family is decided by parsing the IP string, never by tuple length
    alone -- an IPv4 2-tuple and an IPv6 4-tuple never compare equal
    regardless of any numeric coincidence."""
    assert not sockaddrs_match(("192.0.2.1", 100), ("::1", 100, 0, 0))


# Construction boundary: SockaddrIdentity is internal-trusted, constructed
# only inside this module.


def test_sockaddr_identity_is_not_constructed_directly_outside_its_module():
    """Static guard for the documented construction-boundary decision:
    `SockaddrIdentity(...)` must appear only inside
    `core/sockaddr_identity.py` itself. Direct construction elsewhere would
    bypass every invariant `normalize_sockaddr()` enforces (family/port/
    flowinfo/scope_id bounds, canonical IP spelling, zone-string
    rejection), silently creating an implicitly-trusted-but-unvalidated
    value."""
    import pathlib
    import re

    repo_root = pathlib.Path(__file__).resolve().parents[1]
    pattern = re.compile(r"\bSockaddrIdentity\s*\(")
    offending = []
    for path in repo_root.rglob("*.py"):
        parts = path.relative_to(repo_root).parts
        if parts[0] in (".git", "nmea_sproxy_dist", "dist", "build"):
            continue
        if ".pytest-tmp" in parts or "__pycache__" in parts:
            continue
        if path == repo_root / "core" / "sockaddr_identity.py":
            continue
        if path == repo_root / "tests" / "test_sockaddr_identity.py":
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if pattern.search(text):
            offending.append(str(path.relative_to(repo_root)))
    assert offending == []
