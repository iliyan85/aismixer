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
    # Monotonic timestamp of admission into the pipeline (NOT the
    # transport's own arrival time, and NOT wall-clock), or `None` when
    # the source does not need one. Currently populated only by UDPSEC
    # (`aismixer_secure`), whose `assembler_key` is derived from a
    # process-wide `assembly_namespace` identifier that can legitimately
    # be reserved for an unrelated station once its own retirement window
    # elapses (see `core.session_identity_registry`); plain UDP/serial
    # assembler keys are address-derived and stable for that peer, so they
    # have no analogous reuse hazard and never set this. See
    # `core.python_data_plane` for where and why this is enforced.
    admitted_at: Optional[float] = None
    # R6/F4: an optional, narrowly-scoped hold on this exact frame's own
    # right to use `assembler_key`'s underlying process-wide reservation
    # (see `core.session_identity_registry.SessionIdentityRegistry.lease`),
    # taken out at admission time -- while the owning session is provably
    # still live -- and released exactly once via
    # `release_admission_lease()`, by whichever pipeline stage is the
    # last to touch this frame, regardless of outcome (successful
    # processing, a stale-age drop, a queue-admission failure, or
    # cancellation). `None` for every source with no analogous reuse
    # hazard (every non-UDPSEC frame, and a UDPSEC frame whose lease
    # could not be acquired because its session had already died by
    # admission time). Deliberately untyped here (structural: anything
    # exposing `release(now)`) so this module has no dependency on
    # `core.session_identity_registry` or `aismixer_secure` -- a plain
    # UDP/serial frame never sets this and pays no such cost.
    admission_lease: Optional[object] = None

    def release_admission_lease(self, now: float) -> None:
        """Idempotently release `admission_lease`, if this frame carries
        one (a no-op otherwise). Safe to call more than once: the
        lease's own `release()` is itself idempotent."""
        if self.admission_lease is not None:
            self.admission_lease.release(now)


def frame_from_text_payload(
    *,
    kind: IngressKind,
    source_id: str,
    alias_for_s: Optional[str],
    remote_ip: Optional[str],
    assembler_key: str,
    payload: object,
    admitted_at: Optional[float] = None,
    admission_lease: Optional[object] = None,
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
        admitted_at=admitted_at,
        admission_lease=admission_lease,
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
