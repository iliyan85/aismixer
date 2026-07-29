"""Pure helpers for egress routing target identities."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import TypeAlias


EgressTargetId: TypeAlias = int


def freeze_target_id_by_name(
    target_id_by_name: Mapping[str, EgressTargetId],
) -> Mapping[str, EgressTargetId]:
    """Validate and copy an external-name to process-local target-ID mapping."""

    if not isinstance(target_id_by_name, Mapping):
        raise TypeError("target_id_by_name must be a mapping.")

    copied: dict[str, EgressTargetId] = {}
    name_by_target_id: dict[EgressTargetId, str] = {}
    for target_name, target_id in target_id_by_name.items():
        if not isinstance(target_name, str):
            raise TypeError(
                "target_id_by_name keys must be non-empty strings."
            )
        if not target_name:
            raise ValueError(
                "target_id_by_name keys must be non-empty strings."
            )
        if isinstance(target_id, bool) or not isinstance(target_id, int):
            raise TypeError(
                "target_id_by_name values must be non-negative integers."
            )
        if target_id < 0:
            raise ValueError(
                "target_id_by_name values must be non-negative integers."
            )

        previous_name = name_by_target_id.get(target_id)
        if previous_name is not None:
            raise ValueError(
                "target_id_by_name must assign each numeric target ID to only "
                f"one name; {target_id} is assigned to both "
                f"{previous_name!r} and {target_name!r}."
            )

        copied[target_name] = target_id
        name_by_target_id[target_id] = target_name

    return MappingProxyType(copied)


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
