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


def frame_from_text_payload(
    *,
    kind: IngressKind,
    source_id: str,
    alias_for_s: Optional[str],
    remote_ip: Optional[str],
    assembler_key: str,
    payload: object,
) -> Optional[IngressFrame]:
    if not isinstance(payload, str):
        return None

    return IngressFrame(
        kind=kind,
        source_id=source_id,
        alias_for_s=alias_for_s,
        remote_ip=remote_ip,
        assembler_key=assembler_key,
        payload=str.encode(
            payload,
            "utf-8",
            errors="surrogatepass",
        ),
        text_mode=PayloadTextMode.UTF8_SURROGATEPASS,
    )


def frame_from_udp_datagram(
    *,
    data: bytes,
    kind: IngressKind,
    source_id: str,
    alias_for_s: Optional[str],
    remote_ip: Optional[str],
    assembler_key: str,
) -> IngressFrame:
    normalized_text = data.decode("utf-8", errors="ignore").strip()
    return IngressFrame(
        kind=kind,
        source_id=source_id,
        alias_for_s=alias_for_s,
        remote_ip=remote_ip,
        assembler_key=assembler_key,
        payload=normalized_text.encode("utf-8"),
        text_mode=PayloadTextMode.UTF8_IGNORE,
    )


def frame_from_ingress_event(event: IngressEvent) -> Optional[IngressFrame]:
    return frame_from_text_payload(
        kind=event.kind,
        source_id=event.source_id,
        alias_for_s=event.alias_for_s,
        remote_ip=event.remote_ip,
        assembler_key=event.assembler_key,
        payload=event.raw_line,
    )


def coerce_ingress_frame(item: object) -> Optional[IngressFrame]:
    if isinstance(item, IngressFrame):
        return item
    if isinstance(item, IngressEvent):
        return frame_from_ingress_event(item)
    return None


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
