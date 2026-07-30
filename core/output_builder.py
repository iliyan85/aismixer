"""Pure construction of final immutable egress payloads."""

from __future__ import annotations

from collections.abc import Callable

from meta_writer import wrap_with_meta


def build_output_bytes(
    nmea_line: str,
    station_id: str,
    timestamp: int | str | None = None,
    is_first: bool = True,
    g_triplet: str | None = None,
    *,
    clock: Callable[[], float] | None = None,
) -> bytes:
    """Format, frame, and UTF-8 encode one output NMEA sentence."""

    wrapped_text = wrap_with_meta(
        nmea_line,
        station_id,
        timestamp,
        is_first=is_first,
        g_triplet=g_triplet,
        clock=clock,
    )
    return (wrapped_text + "\r\n").encode("utf-8")
