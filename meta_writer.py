import time


def nmea_checksum(data: str) -> str:
    checksum = 0
    for ch in data:
        checksum ^= ord(ch)
    return f"{checksum:02X}"


def format_header(content: str) -> str:
    chk = nmea_checksum(content)
    return "\\" + content + "*" + chk + "\\"


def wrap_with_meta(
        nmea_line: str,
        station_id: str,
        timestamp: int | None = None,
        is_first: bool = True,
        g_triplet: str | None = None) -> str:

    if not timestamp:
        timestamp = int(time.time())

    parts = nmea_line.split(",")
    if len(parts) < 4:
        return nmea_line  # safety fallback

    # g се подава отвън (ако е нужно) като triplet "<part>-<total>-<gid>".
    tag_fields = [f"c:{timestamp}", f"s:{station_id}"]
    if g_triplet:
        # при първата част добавяме c,s,g; при следващите може да се подават само g (ако желаеш)
        if is_first:
            header = format_header(",".join(tag_fields + [f"g:{g_triplet}"]))
        else:
            header = format_header(f"g:{g_triplet}")
    else:
        header = format_header(",".join(tag_fields))

    return header + nmea_line
