"""Pure helpers for ingress routing source identities."""

from __future__ import annotations


def build_udp_source_id(
    configured_id: str | None,
    mapped_alias: str | None,
    remote_ip: str | None,
) -> str:
    """Build an opaque source ID for a plain UDP packet."""

    identity = _first_non_empty(configured_id, mapped_alias, remote_ip)
    if identity is None:
        raise ValueError(
            "UDP source identity requires configured_id, mapped_alias, or remote_ip."
        )
    return f"udp:{identity}"


def build_udpsec_source_id(authenticated_station_id: str | None) -> str:
    """Build an opaque source ID for an authenticated secure UDP peer."""

    if authenticated_station_id is None or authenticated_station_id == "":
        raise ValueError("UDPSEC source identity requires authenticated_station_id.")
    return f"udpsec:{authenticated_station_id}"


def _first_non_empty(*values: str | None) -> str | None:
    for value in values:
        if value is not None and value != "":
            return value
    return None
