from dataclasses import dataclass
from enum import Enum
from typing import Optional

from core.event import IngressEvent, IngressKind


class PayloadTextMode(Enum):
    UTF8_IGNORE = "utf8-ignore"
    UTF8_SURROGATEPASS = "utf8-surrogatepass"


@dataclass(frozen=True, slots=True)
class IngressFrame:
    kind: IngressKind
    source_id: str
    alias_for_s: Optional[str]
    remote_ip: Optional[str]
    assembler_key: str
    payload: bytes
    text_mode: PayloadTextMode = PayloadTextMode.UTF8_IGNORE


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
        payload=str.encode(
            raw_line,
            "utf-8",
            errors="surrogatepass",
        ),
        text_mode=PayloadTextMode.UTF8_SURROGATEPASS,
    )


def decode_frame_slice(
    frame: IngressFrame,
    start: int,
    end: int,
) -> str:
    if start < 0:
        raise ValueError("frame slice start must not be negative")
    if end < start:
        raise ValueError("frame slice end must not precede start")
    if end > len(frame.payload):
        raise ValueError("frame slice end exceeds payload")

    if frame.text_mode is PayloadTextMode.UTF8_IGNORE:
        errors = "ignore"
    elif frame.text_mode is PayloadTextMode.UTF8_SURROGATEPASS:
        errors = "surrogatepass"
    else:
        raise ValueError(f"unsupported payload text mode: {frame.text_mode!r}")

    return frame.payload[start:end].decode("utf-8", errors=errors)
