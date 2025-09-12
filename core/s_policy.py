import re
from typing import Optional

# Строг сет: само [A-Za-z0-9_]
_SAFE = re.compile(r'[^A-Za-z0-9_]')


def sanitize_s(val: Optional[str]) -> str:
    v = (val or "").strip()
    v = _SAFE.sub('_', v)
    return v[:15]  # твърд лимит 15


def extract_incoming_s(raw: Optional[str]) -> Optional[str]:
    """ Взима s: от водещ TAG блок \k:v,...*CS\..., ако има. Връща стойността без checksum. """
    if not raw or not raw.startswith('\\'):
        return None
    try:
        end = raw.find('\\', 1)
        if end == -1:
            return None
        body = raw[1:end]             # "k1:v1,k2:v2*CS"
        body = body.split('*', 1)[0]  # "k1:v1,k2:v2"
        for pair in body.split(','):
            if ':' not in pair:
                continue
            k, v = pair.split(':', 1)
            if k == 's':
                return v
    except Exception:
        return None
    return None


def ip_to_s(ip: Optional[str]) -> str:
    """ IPv4 -> 1_2_3_4; IPv6 -> колони в '_' и отрязване до 15. """
    if not ip:
        return "ANONYMOUS"
    return sanitize_s(ip.replace('.', '_').replace(':', '_'))


def choose_s_value(
    global_station_id: Optional[str],
    source_name_or_id: Optional[str],
    incoming_raw: Optional[str],
    remote_ip: Optional[str],
) -> str:
    """
    Приоритет:
      1) глобално station_id (ако е непразно/не-None)
      2) input.id / alias / SEC client name (ако е непразно и != 'ANONYMOUS')
      3) s от входящия TAG (ако има)
      4) по IP (fallback)
    Всичко минава през sanitize + лимит до 15.
    """
    if global_station_id:
        return sanitize_s(global_station_id)
    if source_name_or_id and source_name_or_id != "ANONYMOUS":
        return sanitize_s(source_name_or_id)
    inc = extract_incoming_s(incoming_raw)
    if inc:
        return sanitize_s(inc)
    return ip_to_s(remote_ip)
