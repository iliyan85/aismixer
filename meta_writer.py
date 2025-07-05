import time


def nmea_checksum(data: str) -> str:
    checksum = 0
    for ch in data:
        checksum ^= ord(ch)
    return f"{checksum:02X}"


def format_header(content: str) -> str:
    chk = nmea_checksum(content)
    return "\\" + content + "*" + chk + "\\"


def wrap_with_meta(nmea_line: str, station_id: str, timestamp=None, is_first=True) -> str:
    if not timestamp:
        timestamp = int(time.time())

    parts = nmea_line.split(",")
    if len(parts) < 4:
        return nmea_line  # safety fallback

    header = ""
    if parts[1] == "2":
        seq_id = parts[3] or "0"
        frag_index = parts[2]
        frag_total = parts[1]
        group_id = f"{frag_index}-{frag_total}-{seq_id}"

        if is_first:
            header = format_header(
                f"c:{timestamp},s:{station_id},g:{group_id}")
        else:
            header = format_header(f"g:{group_id}")
    else:
        header = format_header(f"c:{timestamp},s:{station_id}")

    return header + nmea_line
