import re
from dataclasses import dataclass
from typing import Optional


_AIS_TALKERS = rb"(?:AI|AB|AD|AN|AR|AS|AT|AX|BS)"
_VDM_RE = re.compile(
    rb"!" + _AIS_TALKERS + rb"VDM,[^\r\n]*?\*[0-9A-Fa-f]{2}"
)
_VDMO_RE = re.compile(
    rb"!" + _AIS_TALKERS + rb"VD[MO],[^\r\n]*?\*[0-9A-Fa-f]{2}"
)
_BACKSLASH = b"\\"


@dataclass(frozen=True, slots=True)
class ByteSpan:
    start: int
    end: int

    def __post_init__(self) -> None:
        if self.start < 0 or self.end <= self.start:
            raise ValueError("byte span must satisfy 0 <= start < end")


@dataclass(frozen=True, slots=True)
class NMEAScanMatch:
    sentence_span: ByteSpan
    tag_span: Optional[ByteSpan]


def scan_nmea_sentences(
    payload: bytes,
    include_vdo: bool = False,
) -> tuple[NMEAScanMatch, ...]:
    pattern = _VDMO_RE if include_vdo else _VDM_RE
    matches: list[NMEAScanMatch] = []

    for match in pattern.finditer(payload):
        sentence_start, sentence_end = match.span()
        tag_span = _find_adjacent_tag(payload, sentence_start)
        matches.append(
            NMEAScanMatch(
                sentence_span=ByteSpan(sentence_start, sentence_end),
                tag_span=tag_span,
            )
        )

    return tuple(matches)


def _find_adjacent_tag(payload: bytes, sentence_start: int) -> Optional[ByteSpan]:
    if sentence_start == 0 or payload[sentence_start - 1] != _BACKSLASH[0]:
        return None

    tag_start = payload.rfind(_BACKSLASH, 0, sentence_start - 1)
    if tag_start == -1:
        return None

    return ByteSpan(tag_start, sentence_start)
