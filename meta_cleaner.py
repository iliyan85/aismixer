import re
from typing import NamedTuple, List, Union

# Строг списък с AIS talker-и (вкл. BS – deprecated, но срещан)
# AI (Mobile), AB (Base), AD, AN (AtoN), AR (Receiving), AS (Limited Base),
# AT (Transmitting), AX (Repeater), BS (legacy Base)
_AIS_TALKERS = r'(?:AI|AB|AD|AN|AR|AS|AT|AX|BS)'

# Предкомпилирани регекси:
#  - VDM: приета пейлоуд рамка от "станция"
#  - VDO: "self-report" от местния трансивър (по желание)
_VDM_RE = re.compile(rf'!{_AIS_TALKERS}VDM,[^\r\n]*?\*[0-9A-Fa-f]{{2}}')
_VDMO_RE = re.compile(rf'!{_AIS_TALKERS}VD[MO],[^\r\n]*?\*[0-9A-Fa-f]{{2}}')


class NMEASlice(NamedTuple):
    start: int      # индекс в оригиналния низ, включително
    end: int        # индекс (изключително)
    tag_start: int  # начало на TAG блока '\' ... '\' (или -1 ако няма)
    tag_end: int    # край на TAG блока (позицията на '\' преди '!')


def extract_nmea_sentences(line: str,
                           want_idx: bool = False,
                           include_vdo: bool = False) -> Union[List[str], List[NMEASlice]]:
    """
    Извлича AIS NMEA изречения от комбиниран ред/буфер.
    - По подразбиране връща list[str] с VDM изречения (back-compat).
    - Ако want_idx=True → връща list[NMEASlice] (индекси към 'line' за zero-copy).
    - Ако include_vdo=True → допуска и VDO редове.
    """
    pat = _VDMO_RE if include_vdo else _VDM_RE
    if not want_idx:
        return pat.findall(line)

    out: List[NMEASlice] = []
    for m in pat.finditer(line):
        s, e = m.start(), m.end()
        # Опит да локализираме TAG блока точно преди '!'
        ts = te = -1
        if s > 0 and line[s-1] == '\\':
            te = s - 1
            # търсим предишния '\' (началото на TAG блока)
            ts = line.rfind('\\', 0, te)
            if ts == -1:
                ts = te = -1  # невалиден/недовършен TAG — игнорираме като TAG
        out.append(NMEASlice(s, e, ts, te))
    return out
