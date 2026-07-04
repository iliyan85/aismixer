"""Pure helpers for egress routing target identities."""

from __future__ import annotations


def build_udp_target_id(configured_id: str) -> str:
    """Build the canonical opaque target ID for a configured UDP forwarder."""

    if not isinstance(configured_id, str):
        raise TypeError("UDP target identity requires a string configured_id.")
    if configured_id.strip() == "":
        raise ValueError("UDP target identity requires a non-empty configured_id.")
    if ":" in configured_id:
        raise ValueError(
            "UDP target configured_id must be unnamespaced; use values like 'aishub'."
        )
    return f"udp:{configured_id}"
