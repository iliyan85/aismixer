from dataclasses import dataclass
from typing import Optional

from core.event import IngressEvent, IngressKind


@dataclass(frozen=True, slots=True)
class IngressFrame:
    kind: IngressKind
    source_id: str
    alias_for_s: Optional[str]
    remote_ip: Optional[str]
    assembler_key: str
    payload: bytes


def frame_from_ingress_event(event: IngressEvent) -> Optional[IngressFrame]:
    raw_line = event.raw_line
    if not isinstance(raw_line, str):
        return None

    return IngressFrame(
        kind=event.kind,
        source_id=event.source_id,
        alias_for_s=event.alias_for_s,
        remote_ip=event.remote_ip,
        assembler_key=event.assembler_key,
        payload=str.encode(raw_line, "utf-8"),
    )
