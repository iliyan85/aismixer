"""Structured socket-address identity, shared by both server and client.

This module is the single source of truth for "do these two raw socket
address tuples identify the same path" -- used consistently by UDPSEC's
server-side active-path comparison and canonical active-relation indexing
(`aismixer_secure.py`) and by the client's pinned-remote-address check
(`nmea_sproxy/nmea_sproxy.py`). Two independent copies of this logic
previously existed and could drift; consolidating them here keeps both
sides of the protocol identically strict.

Identity semantics:

    IPv4:  (family, ip, port)
    IPv6:  (family, ip, port, scope_id)

IPv6 ``flowinfo`` is never part of identity: two addresses differing only
in flowinfo are the same path. ``scope_id`` IS part of identity: it
disambiguates link-local addresses, and a MISSING scope_id (a bare 2-tuple
IPv6 address) is treated as an explicit scope_id of 0 -- never as a
wildcard that matches any scope. Family is determined by parsing the IP
string with `ipaddress.ip_address`, never by tuple length, so a
coincidental tuple length can never misclassify an address. IPv4-mapped
IPv6 literals (``::ffff:192.0.2.1``) parse as `ipaddress.IPv6Address` and
are therefore IPv6 identity, never silently collapsed to IPv4.

The canonical result is `SockaddrIdentity`, a small immutable typed value,
deliberately NOT a plain tuple: a raw native IPv6 4-tuple
``(ip, port, flowinfo, scope_id)`` and this module's own canonical IPv6
output ``(family, ip, port, scope_id)`` are both 4 elements long, so a
plain-tuple canonical form could be fed back into `normalize_sockaddr` and
silently misparsed as a *different* raw address (its own `family` int
member reinterpreted as an IP string, and so on). Giving the canonical
form a distinct type makes that impossible: `normalize_sockaddr` accepts
only a raw address tuple and raises `MalformedSockaddrError` if handed a
`SockaddrIdentity` instead, rather than reparsing it.

Any other tuple shape, a non-string IP, an IP string `ipaddress` cannot
parse, an out-of-range port, a non-integer/negative/too-large flowinfo or
scope_id, or a zone-qualified IP string (``"fe80::1%eth0"`` -- native OS
sockaddrs convey scope only as the separate 4th tuple element, never
embedded in the address string, so a string-embedded zone is an ambiguous
shape this module does not attempt to reconcile with that field) is
malformed or ambiguous and raises `MalformedSockaddrError` rather than
being silently treated as equal to (or safely distinct from) a well-formed
address. `flowinfo` and `scope_id` are both bounded to `0 ..
2**32 - 1`: this is the width of the underlying `sin6_flowinfo` and
`sin6_scope_id` C struct fields a real OS `recvfrom()` fills in (Python's
socket module documents both as the raw C `unsigned int` values, not a
narrower type), so it is the actual representational bound for a value
that could legitimately arrive from the OS -- not the narrower 20-bit IPv6
flow-label field RFC 8200 assigns semantic meaning to within the low-order
bits of `sin6_flowinfo`. This module already never uses flowinfo for
identity (see above) and does not otherwise interpret its bits, so
enforcing the narrower semantic bound here would only risk rejecting a
value the OS handed us in the reserved/unused high-order bits, for a field
this module immediately discards; the wide bound still rejects negative
values, non-integers, and clearly-impossible magnitudes. In production,
every address reaching this module originates from a real OS `recvfrom()`
(always a well-formed 2- or 4-tuple for its actual family, with in-range
fields) or from configuration, so this exception is primarily a defensive
invariant check and an operator-configuration validator, not a reachable
attacker-controlled failure mode.

`SockaddrIdentity` is an internal-trusted value type, not a validated
public constructor: its `__init__` performs no invariant checking, and
`normalize_sockaddr()` is the only place in this codebase that constructs
one (see the static source-scan test in `tests/test_sockaddr_identity.py`
that enforces this as a checked invariant, not merely a convention). Code
outside this module must obtain a `SockaddrIdentity` by calling
`normalize_sockaddr()` (or by reading one already produced that way),
never by constructing the dataclass directly with unvalidated field
values.

This module has no opinion on formatting for humans -- see
`core.endpoint_display` for that, entirely separate concern.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass

_MIN_PORT = 0
_MAX_PORT = 65535

# The width of the C `unsigned int` fields sin6_flowinfo/sin6_scope_id --
# see the module docstring for why this is the bound used, rather than the
# narrower 20-bit IPv6 flow-label field.
_MAX_UINT32 = (1 << 32) - 1


class MalformedSockaddrError(ValueError):
    """A socket address tuple has an unsupported or ambiguous shape for
    structured path/relation identity.

    Raised instead of silently treating an unrecognized shape as equal to,
    or safely distinguishable from, a well-formed address -- an ambiguous
    sockaddr must fail closed, never act as an implicit wildcard.
    """


@dataclass(frozen=True, slots=True)
class SockaddrIdentity:
    """Canonical structured identity for one socket address.

    Deliberately a distinct type, not a tuple -- see the module docstring
    for why that matters. `family` is `socket.AF_INET` or
    `socket.AF_INET6`; `ip` is the canonical string form from
    `ipaddress.ip_address` (so equivalent spellings, e.g. differing case
    or zero-compression, compare equal); `scope_id` is always `0` for
    IPv4 and for an IPv6 address with no scope.
    """

    family: int
    ip: str
    port: int
    scope_id: int


def _require_protocol_int(value: object, description: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MalformedSockaddrError(f"{description} must be an integer: {value!r}")
    return value


def normalize_sockaddr(address: object) -> SockaddrIdentity:
    """Canonical structured identity for one raw socket address tuple.

    IPv4 requires an exact 2-tuple ``(ip, port)``.

    IPv6 accepts a 2-tuple ``(ip, port)`` (no scope information available;
    scope_id defaults to 0 explicitly, never as a wildcard) or the native
    4-tuple ``(ip, port, flowinfo, scope_id)`` (flowinfo is validated, then
    dropped -- it never distinguishes a path).

    Raises `MalformedSockaddrError` for a `SockaddrIdentity` passed back in
    (see module docstring), any other unsupported shape, a non-string IP,
    an unparsable or zone-qualified IP string, an out-of-range port, or a
    non-integer, negative, or out-of-range (see `_MAX_UINT32`) flowinfo or
    scope_id.
    """

    if isinstance(address, SockaddrIdentity):
        raise MalformedSockaddrError(
            "normalize_sockaddr() does not re-normalize an already-"
            "canonical SockaddrIdentity; use the value directly"
        )
    if not isinstance(address, tuple) or len(address) not in (2, 4):
        raise MalformedSockaddrError(
            f"unsupported sockaddr shape: {address!r}"
        )

    ip, port = address[0], address[1]
    if not isinstance(ip, str):
        raise MalformedSockaddrError(
            f"sockaddr IP must be a string: {address!r}"
        )
    port = _require_protocol_int(port, "sockaddr port")
    if not (_MIN_PORT <= port <= _MAX_PORT):
        raise MalformedSockaddrError(
            f"sockaddr port out of range: {port!r}"
        )

    try:
        parsed = ipaddress.ip_address(ip)
    except (ValueError, TypeError) as exc:
        raise MalformedSockaddrError(
            f"unparsable sockaddr address: {address!r}"
        ) from exc
    if getattr(parsed, "scope_id", None) is not None:
        raise MalformedSockaddrError(
            "sockaddr IP must not carry a zone-qualified ('%zone') "
            f"scope in the address string: {address!r}"
        )

    if isinstance(parsed, ipaddress.IPv4Address):
        if len(address) != 2:
            raise MalformedSockaddrError(
                f"IPv4 sockaddr must be a 2-tuple: {address!r}"
            )
        return SockaddrIdentity(
            family=socket.AF_INET,
            ip=str(parsed),
            port=port,
            scope_id=0,
        )

    if len(address) == 2:
        scope_id = 0
    else:
        flowinfo = _require_protocol_int(address[2], "sockaddr flowinfo")
        if not (0 <= flowinfo <= _MAX_UINT32):
            raise MalformedSockaddrError(
                f"sockaddr flowinfo out of range: {flowinfo!r}"
            )
        scope_id = _require_protocol_int(address[3], "sockaddr scope_id")
        if not (0 <= scope_id <= _MAX_UINT32):
            raise MalformedSockaddrError(
                f"sockaddr scope_id out of range: {scope_id!r}"
            )
    return SockaddrIdentity(
        family=socket.AF_INET6,
        ip=str(parsed),
        port=port,
        scope_id=scope_id,
    )


def sockaddrs_match(a: object, b: object) -> bool:
    """True iff two raw socket address tuples identify the same
    structured path (see module docstring for the exact semantics).

    Raises `MalformedSockaddrError` if either address has an unsupported
    or ambiguous shape.
    """

    return normalize_sockaddr(a) == normalize_sockaddr(b)
