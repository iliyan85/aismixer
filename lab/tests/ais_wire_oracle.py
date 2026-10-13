"""Independent standard-library-only acceptance oracle for the host AIS demo.

No production code, field tables, bit writer, GPU, or third-party decoder is
imported. Offsets were separately checked against ITU-R M.1371-6 (02/2026),
Annex 7 Tables 46, 49, 57, 68 and 71. The synthetic vectors below were computed
from explicit numeric examples independently of the C++ encoder. They are not
captured RF data, assigned operational identities, or copied GPSD test data.
"""

import argparse
import os
from pathlib import Path
import re
import subprocess


GOLDEN = {
    1: b"!AIVDM,1,1,,A,1Amg=5EmQso>f;LFGd5DlSoo7D<Q,0*2B\r\n",
    4: b"!AIVDM,1,1,,A,4P1;FQivPfosrk81`0kOqP?P2SAF,0*26\r\n",
    9: b"!AIVDM,1,1,,A,9ib4N?DlU1dovH1<P6P>3w`2>ckN,0*43\r\n",
    18: b"!AIVDM,1,1,,A,B>eq`d@3wk?8mP=18D3Q3wvCuD<Q,0*20\r\n",
    21: b"!AIVDM,1,1,,A,ENk`A<S60Q@64ST:@HHP0000000Owwwwwwwwh91AISuJFP,4*59\r\n",
}

# Short factual telemetry example, not copied decoder code. Published by
# Eric S. Raymond / GPSD project in AIVDM/AIVDO protocol decoding v1.58,
# sentence-layer section. Project BSD-2-Clause provenance/notice: README.md (repository root).
# https://gpsd.gitlab.io/gpsd/AIVDM.html#_aivdm_aivdo_sentence_layer
# https://gitlab.com/gpsd/gpsd/-/raw/master/COPYING
PUBLISHED_TYPE1 = b"!AIVDM,1,1,,B,177KQJ5000G?tO`K>RA1wUbN0TKH,0*5C\r\n"


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def unarmor(payload, fill):
    require(0 <= fill <= 5, "fill outside 0..5")
    raw = ""
    for char in payload:
        require(48 <= char <= 87 or 96 <= char <= 119,
                "invalid AIS armoring character")
        value = char - 48 if char <= 87 else char - 56
        raw += f"{value:06b}"
    require(raw, "empty armored payload")
    if fill:
        require(raw[-fill:] == "0" * fill, "nonzero fill bits")
        raw = raw[:-fill]
    return raw


def sentence_fields(sentence):
    require(sentence.endswith(b"\r\n"), "wire sentence lacks exact CRLF")
    require(len(sentence) <= 82, "wire sentence exceeds 82 bytes with CRLF")
    require(sentence.startswith(b"!AIVDM,"), "wire sentence is not AIVDM")
    body, checksum = sentence[1:-2].split(b"*")
    require(re.fullmatch(rb"[0-9A-F]{2}", checksum), "noncanonical checksum")
    value = 0
    for byte in body:
        value ^= byte
    require(value == int(checksum, 16), "incorrect independent XOR checksum")
    fields = body.split(b",")
    require(len(fields) == 7, "incorrect comma/field count")
    require(fields[0] == b"AIVDM", "incorrect talker/message")
    require(fields[4] in (b"A", b"B"), "invalid channel")
    total, part, fill = map(int, (fields[1], fields[2], fields[6]))
    require(1 <= part <= total <= 9, "invalid bounded fragment numbers")
    if total == 1:
        require(part == 1 and fields[3] == b"", "single fragment has sequence ID")
    else:
        require(re.fullmatch(rb"[0-9]", fields[3]), "fragment sequence not explicit digit")
    require(0 <= fill <= 5 and (part == total or fill == 0), "fragment fill rule")
    return total, part, fields[3], fields[4], fields[5], fill


def reconstruct(sentences):
    records = [sentence_fields(sentence) for sentence in sentences]
    require(records, "no NMEA sentences")
    total = records[0][0]
    require(len(records) == total, "missing/extra fragments")
    require([record[1] for record in records] == list(range(1, total + 1)),
            "fragments not in canonical order")
    require(all(record[0] == total and record[2:4] == records[0][2:4]
                for record in records), "fragment headers inconsistent")
    armored = b"".join(record[4] for record in records)
    return unarmor(armored, records[-1][5]), armored, records[-1][5]


def decoded_fields(bits, message_type):
    def field(offset, width, expected, name, signed=False):
        value = int(bits[offset:offset + width], 2)
        if signed and bits[offset] == "1":
            value -= 1 << width
        require(value == expected, f"type {message_type} {name}: {value} != {expected}")

    require(len(bits) == (272 if message_type == 21 else 168), "incorrect payload bit count")
    field(0, 6, message_type, "message type")
    if message_type == 1:
        field(6, 2, 1, "repeat")
        field(8, 30, 123456789, "MMSI")
        field(38, 4, 5, "navigation status")
        field(42, 8, -42, "ROT", True)
        field(50, 10, 123, "speed deciknots")
        field(60, 1, 1, "accuracy")
        field(61, 28, -73567890, "longitude", True)
        field(89, 27, 23456789, "latitude", True)
        field(116, 12, 1234, "course dec degrees")
        field(128, 9, 123, "heading")
        field(137, 6, 59, "UTC second")
        field(143, 2, 2, "manoeuvre")
        field(145, 2, 0, "spare")
        field(147, 1, 1, "low power (M.1371-6)")
        field(148, 1, 1, "RAIM")
        field(149, 19, 0x54321, "SOTDMA raw state")
    elif message_type == 4:
        field(6, 2, 2, "repeat")
        field(8, 30, 1234567, "leading-zero MMSI")
        field(38, 14, 2024, "UTC year")
        field(52, 4, 2, "UTC month")
        field(56, 5, 29, "UTC leap day")
        field(61, 5, 23, "UTC hour")
        field(66, 6, 59, "UTC minute")
        field(72, 6, 58, "UTC second")
        field(78, 1, 1, "accuracy")
        field(79, 28, -108000000, "longitude -180 deg", True)
        field(107, 27, 54000000, "latitude +90 deg", True)
        field(134, 4, 15, "internal GNSS EPFS")
        field(138, 1, 1, "satellite transmission control")
        field(139, 9, 0, "spare")
        field(148, 1, 1, "RAIM")
        field(149, 19, 0x23456, "SOTDMA raw state")
    elif message_type == 9:
        field(6, 2, 3, "repeat")
        field(8, 30, 111222333, "MMSI")
        field(38, 12, 1234, "altitude metres")
        field(50, 10, 321, "speed WHOLE knots")
        field(60, 1, 1, "accuracy")
        field(61, 28, 108000000, "longitude +180 deg", True)
        field(89, 27, -54000000, "latitude -90 deg", True)
        field(116, 12, 3599, "course maximum")
        field(128, 6, 62, "UTC estimated position")
        field(134, 1, 1, "barometric altitude")
        field(135, 7, 0, "spare")
        field(142, 1, 1, "DTE not ready")
        field(143, 3, 0, "spare")
        field(146, 1, 1, "assigned mode")
        field(147, 1, 1, "RAIM")
        field(148, 1, 1, "ITDMA selector")
        field(149, 19, 0x2BCDE, "raw radio state")
    elif message_type == 18:
        field(6, 2, 0, "repeat")
        field(8, 30, 987654321, "MMSI")
        field(38, 8, 0, "spare")
        field(46, 10, 1023, "speed unavailable")
        field(56, 1, 0, "accuracy")
        field(57, 28, 108600000, "longitude unavailable", True)
        field(85, 27, 54600000, "latitude unavailable", True)
        field(112, 12, 3600, "course unavailable")
        field(124, 9, 511, "heading unavailable")
        field(133, 6, 60, "UTC unavailable")
        field(139, 1, 1, "low power (M.1371-6)")
        field(140, 1, 0, "spare")
        field(141, 1, 0, "Class B SO")
        for offset, name in [(142, "display"), (143, "DSC"), (144, "band"),
                             (145, "Message22"), (146, "assigned"), (147, "RAIM")]:
            field(offset, 1, 1, name)
        field(148, 1, 0, "SOTDMA selector")
        field(149, 19, 0x54321, "raw radio state")
    elif message_type == 21:
        field(6, 2, 1, "repeat")
        field(8, 30, 993661234, "MMSI")
        field(38, 5, 6, "light with sectors aid type")
        name = ""
        for offset in range(43, 163, 6):
            value = int(bits[offset:offset + 6], 2)
            name += chr(value + 64 if value < 32 else value)
        require(name == "LAB LIGHT 01@@@@@@@@", "six-bit name/padding")
        field(163, 1, 1, "accuracy")
        field(164, 28, -1, "longitude negative LSB", True)
        field(192, 27, -2, "latitude negative", True)
        field(219, 9, 9, "bow dimension")
        field(228, 9, 10, "stern dimension")
        field(237, 6, 11, "port dimension")
        field(243, 6, 12, "starboard dimension")
        field(249, 4, 7, "surveyed EPFS")
        field(253, 6, 58, "UTC second")
        field(259, 1, 1, "off-position")
        field(260, 8, 0xA5, "AtoN raw status")
        field(268, 1, 1, "RAIM")
        field(269, 1, 0, "physical aid")
        field(270, 1, 1, "assigned")
        field(271, 1, 0, "spare")
    else:
        raise AssertionError("unsupported demo type")


def check_demo(data):
    records = []
    current = None
    for line in data.splitlines(keepends=True):
        metadata = re.fullmatch(rb"type=(\d+) bits=(\d+) armored=([^ ]+) fill=(\d+)\r?\n", line)
        if metadata:
            if current is not None:
                records.append(current)
            current = (tuple(metadata.groups()), [])
        elif line.startswith(b"!AIVDM,"):
            require(current is not None, "NMEA emitted before message metadata")
            current[1].append(line)
    if current is not None:
        records.append(current)
    require([int(record[0][0]) for record in records] == [1, 4, 9, 18, 21],
            "demo must explicitly show each supported type in canonical order")
    lengths = []
    for metadata, sentences in records:
        message_type, advertised_bits, advertised_payload, advertised_fill = metadata
        message_type = int(message_type)
        require(b"".join(sentences) == GOLDEN[message_type], f"type {message_type} golden bytes")
        bits, armored, fill = reconstruct(sentences)
        require(int(advertised_bits) == len(bits) and advertised_payload == armored and
                int(advertised_fill) == fill, "diagnostic metadata differs from wire")
        decoded_fields(bits, message_type)
        lengths.extend(map(len, sentences))
    return lengths


def check_published_fixture():
    bits, _, fill = reconstruct([PUBLISHED_TYPE1])
    require(len(bits) == 168 and fill == 0, "published fixture bit count")
    # Independent decoded field facts, in wire units; unused/spare fields and
    # the M.1371-6 transmit-power bit must also match the legacy example's zero.
    for offset, width, expected, signed in [
            (0, 6, 1, False), (6, 2, 0, False), (8, 30, 477553000, False),
            (38, 4, 5, False), (42, 8, 0, True), (50, 10, 0, False),
            (60, 1, 0, False), (61, 28, -73407500, True),
            (89, 27, 28549700, True), (116, 12, 510, False),
            (128, 9, 181, False), (137, 6, 15, False),
            (143, 2, 0, False), (145, 2, 0, False), (147, 1, 0, False),
            (148, 1, 0, False), (149, 19, 149208, False)]:
        value = int(bits[offset:offset + width], 2)
        if signed and bits[offset] == "1":
            value -= 1 << width
        require(value == expected, f"published fixture offset {offset}: {value} != {expected}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path, required=True)
    # Accepted for consistency with the existing CLI acceptance tests. No capture
    # files are needed: these deliberately small records stay in memory.
    parser.add_argument("--work-dir", type=Path)
    args = parser.parse_args()
    environment = dict(os.environ, CUDA_VISIBLE_DEVICES="-1")

    def run(*options):
        return subprocess.run([str(args.exe.resolve()), *options], capture_output=True,
                              env=environment, timeout=30, check=False)

    demo = run("--demo")
    require(demo.returncode == 0, f"demo failed: {demo.stderr.decode(errors='replace')}")
    require(not demo.stderr, "successful demo wrote stderr")
    lengths = check_demo(demo.stdout)
    check_published_fixture()
    repeat = run("--demo")
    require(repeat.returncode == 0 and repeat.stdout == demo.stdout, "demo bytes changed on repeat")
    self_test = run("--self-test")
    require(self_test.returncode == 0, "native self-test failed with GPU hidden")
    after_test = run("--demo")
    require(after_test.returncode == 0 and after_test.stdout == demo.stdout,
            "self-test/execution order changed demo bytes")
    help_result = run("--help")
    require(help_result.returncode == 0 and b"--demo" in help_result.stdout and
            b"--self-test" in help_result.stdout, "help omitted supported options")
    for options in [("--unknown",), ("--demo", "--self-test"), ("--demo", "--demo"),
                    ("--demo", "unexpected"), ("--format", "nmea")]:
        result = run(*options)
        require(result.returncode != 0 and b"!AIVDM," not in result.stdout,
                f"invalid CLI partially succeeded: {options}")
    print("PASS independent per-field ITU-R M.1371-6 decoder, five golden CRLF vectors")
    print("PASS published GPSD v1.58 Type1 fixture: all decoded fields and independent XOR")
    print(f"PASS XOR, fill, exact lengths; sentence byte counts={lengths}; CUDA_VISIBLE_DEVICES=-1")
    print("PASS repeated/order-independent demo, native self-test, help, five invalid CLI cases")


if __name__ == "__main__":
    main()
