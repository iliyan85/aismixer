"""Parse metadata from scanner spans without retaining decoded payload views.

Only the matched sentence and associated TAG slices are decoded temporarily.
They use UTF-8 with ``errors="ignore"`` to preserve the current plain-UDP
conversion policy while allowing arbitrary bytes-native frames to be parsed.
"""

from dataclasses import dataclass
from typing import Optional

from core.ingress_frame import IngressFrame
from core.nmea_scanner import NMEAScanMatch, scan_nmea_sentences


@dataclass(frozen=True, slots=True)
class ParsedFragment:
    declared_total: int
    ordinal: int
    sequential_id: str
    channel: str


@dataclass(frozen=True, slots=True)
class ParsedGroupTag:
    part: int
    total: int
    group_id: str
    preservable_group_id: Optional[str]


@dataclass(frozen=True, slots=True)
class ParsedTagMetadata:
    s_value: Optional[str]
    c_text: Optional[str]
    c_value: Optional[int]
    g_value: Optional[ParsedGroupTag]


@dataclass(frozen=True, slots=True)
class ParsedSentence:
    frame: IngressFrame
    match: NMEAScanMatch
    fragment: Optional[ParsedFragment]
    tag: ParsedTagMetadata


def parse_scanned_sentence(
    frame: IngressFrame,
    match: NMEAScanMatch,
) -> ParsedSentence:
    _validate_match(frame, match)

    sentence_span = match.sentence_span
    sentence_text = frame.payload[
        sentence_span.start:sentence_span.end
    ].decode("utf-8", errors="ignore")

    tag = _parse_tag_metadata(frame, match)
    fragment = _parse_fragment(sentence_text)
    return ParsedSentence(
        frame=frame,
        match=match,
        fragment=fragment,
        tag=tag,
    )


def parse_frame_sentences(
    frame: IngressFrame,
    include_vdo: bool = False,
) -> tuple[ParsedSentence, ...]:
    return tuple(
        parse_scanned_sentence(frame, match)
        for match in scan_nmea_sentences(
            frame.payload,
            include_vdo=include_vdo,
        )
    )


def _validate_match(frame: IngressFrame, match: NMEAScanMatch) -> None:
    payload = frame.payload
    sentence_span = match.sentence_span
    if sentence_span.end > len(payload):
        raise ValueError("sentence span exceeds frame payload")
    if payload[sentence_span.start] != ord("!"):
        raise ValueError("sentence span must begin with '!'")

    tag_span = match.tag_span
    if tag_span is None:
        return
    if tag_span.end > len(payload):
        raise ValueError("TAG span exceeds frame payload")
    if tag_span.end != sentence_span.start:
        raise ValueError("TAG span must end at sentence start")
    if (
        payload[tag_span.start] != ord("\\")
        or payload[tag_span.end - 1] != ord("\\")
    ):
        raise ValueError("TAG span must include both backslash delimiters")


def _parse_fragment(sentence_text: str) -> Optional[ParsedFragment]:
    fields = sentence_text.split(",")
    if len(fields) < 7:
        return None

    try:
        declared_total = int(fields[1])
        ordinal = int(fields[2])
    except ValueError:
        return None

    if declared_total < 1 or ordinal < 1 or ordinal > declared_total:
        return None

    return ParsedFragment(
        declared_total=declared_total,
        ordinal=ordinal,
        sequential_id=fields[3],
        channel=fields[4],
    )


def _parse_tag_metadata(
    frame: IngressFrame,
    match: NMEAScanMatch,
) -> ParsedTagMetadata:
    tag_span = match.tag_span
    if tag_span is None:
        return ParsedTagMetadata(
            s_value=None,
            c_text=None,
            c_value=None,
            g_value=None,
        )

    tag_text = frame.payload[tag_span.start:tag_span.end].decode(
        "utf-8",
        errors="ignore",
    )
    body = tag_text[1:-1].split("*", 1)[0]

    s_value: Optional[str] = None
    c_text: Optional[str] = None
    g_text: Optional[str] = None
    for pair in body.split(","):
        if not pair:
            continue
        key, separator, value = pair.partition(":")
        if not separator:
            continue
        if key == "s":
            s_value = value
        elif key == "c":
            c_text = value
        elif key == "g":
            g_text = value

    return ParsedTagMetadata(
        s_value=s_value,
        c_text=c_text,
        c_value=_parse_c_value(c_text),
        g_value=_parse_g_value(g_text),
    )


def _parse_c_value(value: Optional[str]) -> Optional[int]:
    if not value or not value.isdigit():
        return None
    try:
        return int(value)
    except ValueError:
        # Some Unicode characters satisfy isdigit() but are rejected by int().
        # Bytes-native parsing must remain non-raising for those inputs.
        return None


def _parse_g_value(value: Optional[str]) -> Optional[ParsedGroupTag]:
    if value is None:
        return None
    try:
        part_text, total_text, group_id = value.split("-", 2)
        part = int(part_text)
        total = int(total_text)
    except ValueError:
        return None

    preservable_group_id = (
        group_id
        if group_id and group_id.isdigit()
        else None
    )
    return ParsedGroupTag(
        part=part,
        total=total,
        group_id=group_id,
        preservable_group_id=preservable_group_id,
    )
