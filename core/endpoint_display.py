"""Human/operator-facing network endpoint rendering.

This module is display-only. The strings it returns must never become
session identity, path identity, assembler identity, a dict key, a wire
value, or a value later parsed back into an endpoint -- structured
``(ip, port, ...)`` data remains authoritative for all of that.

AISMixer's operator-facing convention:

    IPv4: 192.0.2.10:17778
    IPv6: 2001:db8::10.17778

The IPv6 form is deliberately tcpdump-style (``ipv6.port``) rather than
``ipv6:port`` (ambiguous: ':' already separates IPv6 address components) or
the URI/web convention ``[ipv6]:port`` (correct but not this project's
chosen operator display style). Where a standards-compliant URI is actually
required, render one directly instead of using this helper.
"""

from __future__ import annotations


def format_endpoint(ip: str, port: int) -> str:
    """Render one ``(ip, port)`` endpoint for logs/diagnostics/status text."""

    if ":" in ip:
        return f"{ip}.{port}"
    return f"{ip}:{port}"


def format_endpoint_tuple(address) -> str:
    """Render a raw socket address tuple (2- or 4-tuple) for display only.

    Only the address and port are used; IPv6 ``flowinfo``/``scope_id`` (the
    3rd/4th tuple elements some platforms return) are not part of this
    display convention.
    """

    return format_endpoint(address[0], address[1])
