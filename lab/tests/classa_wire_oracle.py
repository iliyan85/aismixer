"""Independent Class A payload/state oracle using only Python's standard library.

Field offsets were separately transcribed from ITU-R M.1371-6 (02/2026),
Annex 7 Tables 46/47 (printed105-107), 50/51 (109-114), 77/78 (140-142),
and Annex 2 Tables 18/19/20 (48-50). Fixtures are original synthetic examples,
not captured traffic, operational identities or copied decoder/test code.
No production headers, field arrays, BitWriter or state implementation is read
or imported. The executable is invoked only to obtain actual bytes to check.
Full Class A compliance, physical framing/FCS and scheduling are not tested.
"""

import argparse
from collections import Counter
from contextlib import redirect_stdout
import hashlib
import io
import os
import re
import subprocess


# Literal binary goldens were assembled independently before production code.
GOLDENS = {
    "type3": (
        "000011010000000000000000000000000000010101110101100001111011110111001110101110001011011101101001101000010011111010110100110100100011110111110111000111010000000000011101",
        "e1d82bd5f3dfd772fdd2cde083398238d8a96576d460b5fecf5c82140dddd622",
        "3@0000EmQso>f;Ma\x60CrllSoo7@0M",
        [b"!AIVDM,1,1,,A,3@0000EmQso>f;Ma\x60CrllSoo7@0M,0*77\r\n"],
    ),
    "type5": (
        "0001011000011101011011110011010001010111000000000100101101011010000111001100000001000010110001110010110011110100000001001001010011001101001001011000000101010010100000001100000001000010100000110000110001000000000000000000000000000000010001100011001000000110010001110010011111001011101101111110110111101101011000000101001000111000000110000000001000011100000000000000000000000000000000000000000000000000000000000000000000000000",
        "b2b56a31de1246f08c2e3291d98d379739a1c4d3e9662c2b3ebddf3d7cf30f0c",
        "5Qmg=5L0Bm\x60Lh4;7;?@4U<lUPE:0h4:334000016<PI79tfosNmPDSPH0Qh000000000000",
        [b"!AIVDM,2,1,7,B,5Qmg=5L0Bm\x60Lh4;7;?@4U<lUPE:0h4:334000016<PI79tfosNmPDSPH0Qh0,0*52\r\n",
         b"!AIVDM,2,2,7,B,00000000000,2*20\r\n"],
    ),
    "classa24b": (
        "011000010000000000000111100010010000000100000000000001000010000011001101010100001100100001000000000000000000000000000000000000000000000000000000000000000000000000000000",
        "96603cba1f3aba5f9d3ff5a7f7a0172dbc3171f90477918bf05dca9e29db7ec8",
        "H@07R@40123=D<Q0000000000000",
        [b"!AIVDM,1,1,,A,H@07R@40123=D<Q0000000000000,0*2B\r\n"],
    ),
}


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def number(value, width):
    require(-(1 << (width - 1)) <= value < (1 << width), "reference number width")
    return f"{value % (1 << width):0{width}b}"


def six_text(value, width):
    require(len(value) <= width and all(32 <= ord(ch) <= 95 and ch != "@" for ch in value),
            "invalid reference AIS text")
    return "".join(number(ord(ch) & 63, 6) for ch in value.ljust(width, "@"))


def assemble(name):
    """Separate literal offset maps, not encoder layout arrays or C++ output."""
    if name == "type3":
        size = 168
        fields = [
            (0, 6, 3), (6, 2, 1), (8, 30, 1), (38, 4, 5), (42, 8, -42),
            (50, 10, 123), (60, 1, 1), (61, 28, -73567890),
            (89, 27, -23456789), (116, 12, 1234), (128, 9, 123),
            (137, 6, 59), (143, 2, 2), (145, 2, 0), (147, 1, 1), (148, 1, 1),
            # ITDMA independently expanded into all four wire subfields.
            (149, 2, 2), (151, 13, 4097), (164, 3, 6), (167, 1, 1),
        ]
        text_fields = []
    elif name == "type5":
        size = 424
        fields = [
            (0, 6, 5), (6, 2, 2), (8, 30, 123456789), (38, 2, 3),
            (40, 30, 1234567), (232, 8, 70), (240, 9, 100), (249, 9, 25),
            (258, 6, 7), (264, 6, 9), (270, 4, 15), (274, 4, 2),
            (278, 5, 29), (283, 5, 23), (288, 6, 59), (294, 8, 123),
            (422, 1, 0), (423, 1, 0),
        ]
        text_fields = [(70, 7, "LAB1234"), (112, 20, "AISMIXER LAB 01"),
                       (302, 20, "VARNA BG")]
    elif name == "classa24b":
        size = 168
        fields = [
            (0, 6, 24), (6, 2, 1), (8, 30, 123456), (38, 2, 1),
            (40, 8, 0), (66, 4, 3), (70, 20, 0x54321),
            (90, 42, 0), (132, 9, 0), (141, 9, 0), (150, 6, 0),
            (156, 6, 0), (162, 4, 0), (166, 2, 0),
        ]
        text_fields = [(48, 3, "ABC")]
    else:
        raise AssertionError("unknown independent fixture")
    bits = [None] * size
    for offset, width, value in fields:
        for index, bit in enumerate(number(value, width)):
            require(bits[offset + index] is None, "overlapping independent fields")
            bits[offset + index] = bit
    for offset, width, value in text_fields:
        for index, bit in enumerate(six_text(value, width)):
            require(bits[offset + index] is None, "overlapping independent text")
            bits[offset + index] = bit
    require(None not in bits, "independent layout omitted a field")
    return "".join(bits), fields, text_fields


def armor(bits):
    fill = -len(bits) % 6
    padded = bits + "0" * fill
    return "".join(chr(value + 48 + (8 if value >= 40 else 0))
                   for value in (int(padded[i:i + 6], 2)
                                 for i in range(0, len(padded), 6))), fill


def unarmor(payload, fill):
    require(0 <= fill <= 5, "fill outside domain")
    require(payload, "empty payload")
    bits = ""
    for char in payload:
        require(48 <= char <= 87 or 96 <= char <= 119, "bad armor alphabet")
        bits += f"{char - 48 if char <= 87 else char - 56:06b}"
    if fill:
        require(bits[-fill:] == "0" * fill, "nonzero armoring fill")
        bits = bits[:-fill]
    return bits


def sentence(body):
    result = 0
    for value in body:
        result ^= value
    return b"!" + body + f"*{result:02X}\r\n".encode("ascii")


def parse_sentence(data):
    require(data.endswith(b"\r\n") and len(data) <= 82, "NMEA length/CRLF")
    require(data.startswith(b"!AIVDM,") and data.count(b"*") == 1,
            "NMEA sentence identity")
    body, check = data[1:-2].split(b"*")
    require(re.fullmatch(rb"[0-9A-F]{2}", check), "canonical checksum")
    require(sentence(body) == data, "independent XOR mismatch")
    parts = body.split(b",")
    require(len(parts) == 7, "field count")
    require(parts[0] == b"AIVDM", "NMEA type")
    require(re.fullmatch(rb"[1-9]", parts[1]) and re.fullmatch(rb"[1-9]", parts[2]),
            "noncanonical fragment index/count")
    total, index = int(parts[1]), int(parts[2])
    require(1 <= index <= total <= 9, "fragment domain")
    require(parts[4] in (b"A", b"B"), "channel domain")
    require((total == 1 and parts[3] == b"") or
            (total > 1 and re.fullmatch(rb"[0-9]", parts[3])), "sequence domain")
    require(re.fullmatch(rb"[0-5]", parts[6]), "fill domain")
    fill = int(parts[6])
    require(index == total or fill == 0, "nonfinal fill")
    unarmor(parts[5], fill)  # Validate each alphabet and final padding separately.
    return total, index, parts[3], parts[4], parts[5], fill


def reconstruct(sentences):
    records = [parse_sentence(value) for value in sentences]
    require(records, "empty sequence")
    total = records[0][0]
    require(len(records) == total, "incomplete/extra fragment sequence")
    require([record[1] for record in records] == list(range(1, total + 1)),
            "unordered or repeated fragments")
    require(all((record[0], record[2], record[3]) ==
                (total, records[0][2], records[0][3]) for record in records),
            "inconsistent sequence/channel/count")
    return unarmor(b"".join(record[4] for record in records), records[-1][5])


def corruptions(actual_output):
    """Check that the independent oracle detects corruption, not just goldens."""
    good = GOLDENS["type5"][3]
    fields = [value[1:-5].split(b",") for value in good]
    cases = [[], [good[0]], [good[1]], [good[1], good[0]], [good[0], good[0]],
             good + [good[1]], [good[0][:-1], good[1]],
             [good[0].replace(b"*52", b"*53"), good[1]],
             [good[0].replace(b"*52", b"*5a"), good[1]],
             [good[0].replace(b"\r\n", b"\n"), good[1]]]
    for index, column, value in [(1, 3, b"8"), (1, 4, b"A"), (1, 1, b"3"),
                                  (0, 3, b""), (0, 2, b"0"), (0, 1, b"10"),
                                  (0, 6, b"1"), (1, 6, b"6"), (1, 5, b"["),
                                  (1, 5, b"00000000001"), (1, 5, b""),
                                  (0, 5, b"0" * 61)]:
        changed = list(good)
        modified = list(fields[index])
        modified[column] = value
        changed[index] = sentence(b",".join(modified))
        cases.append(changed)
    # Explicit payload/type/length corruptions exercise the actual independent
    # validator, not a comparison of a modified literal with its original.
    counts = Counter()
    for data in cases:
        try:
            reconstruct(data)
        except (AssertionError, ValueError):
            counts["nmea_corruption"] += 1
        else:
            raise AssertionError("independent oracle accepted corrupt fragment fixture")
    lines = actual_output.decode("ascii").splitlines()
    for name in GOLDENS:
        index = next(index for index, line in enumerate(lines)
                     if line.startswith(f"VECTOR|{name}|"))
        parts = lines[index].split("|")
        literal = parts[5]
        changed_bits = [
            ("0" if literal[0] == "1" else "1") + literal[1:],
            literal[:-1] + ("0" if literal[-1] == "1" else "1"),
            literal[:-1],
        ]
        for bits in changed_bits:
            changed = list(lines)
            modified = list(parts)
            modified[5] = bits
            modified[2] = str(len(bits))
            changed[index] = "|".join(modified)
            try:
                with redirect_stdout(io.StringIO()):
                    check_output(("\n".join(changed) + "\n").encode("ascii"))
            except AssertionError:
                counts["payload_corruption"] += 1
            else:
                raise AssertionError("independent validator accepted corrupt actual payload row")
    return counts


def check_output(data):
    vectors, sentences, states = {}, {}, {}
    for line in data.decode("ascii").splitlines():
        parts = line.split("|")
        if parts[0] == "VECTOR":
            require(len(parts) == 6 and parts[1] not in vectors, "vector row format/duplicate")
            vectors[parts[1]] = (int(parts[2]), int(parts[3]), parts[4], parts[5])
        elif parts[0] == "NMEA":
            require(len(parts) == 3 and re.fullmatch("[0-9A-F]+", parts[2]), "NMEA row format")
            sentences.setdefault(parts[1], []).append(bytes.fromhex(parts[2]))
        elif parts[0] == "STATE":
            require(len(parts) == 3 and parts[1] not in states, "state row format/duplicate")
            states[parts[1]] = int(parts[2])
        else:
            raise AssertionError("unexpected fixture output")
    require(set(vectors) == set(sentences) == set(GOLDENS), "missing/extra fixtures")
    fields_checked = 0
    for name, (literal, fingerprint, armored, wire) in GOLDENS.items():
        expected, fields, texts = assemble(name)
        require(literal == expected, f"{name} independent literal versus reference layout")
        require(hashlib.sha256(literal.encode("ascii")).hexdigest() == fingerprint,
                f"{name} independent literal fingerprint")
        size, fill, actual_armor, bits = vectors[name]
        require(bits == literal and size == len(expected), f"{name} exact literal bits/length")
        expected_armor, expected_fill = armor(expected)
        require(actual_armor == armored == expected_armor and fill == expected_fill,
                f"{name} six-bit armoring/fill")
        require(sentences[name] == wire, f"{name} exact NMEA bytes")
        require(reconstruct(sentences[name]) == expected, f"{name} reconstructed AIS bits")
        for offset, width, value in fields:
            actual = int(bits[offset:offset + width], 2)
            require(actual == value % (1 << width), f"{name} field {offset}/{width}")
            # Reverse read of negative two's-complement fields is independent.
            if value < 0:
                require(actual - (1 << width) == value, "signed field interpretation")
            fields_checked += 1
        for offset, width, value in texts:
            decoded = "".join(chr(code + 64 if code < 32 else code)
                              for code in (int(bits[i:i + 6], 2)
                                           for i in range(offset, offset + width * 6, 6)))
            require(decoded == value.ljust(width, "@"), f"{name} text mapping/padding")
            fields_checked += 1
        print(f"PASS independent {name}: bits={size} fill={fill} "
              f"sentence_bytes={','.join(str(len(value)) for value in wire)} sha256={fingerprint}")
    require(len(states) == 16, "missing state modes")
    for code in range(8):
        expected = (3 << 17) | (8191 << 4) | (code << 1) | 1
        require(states[f"itdma{code}"] == expected, "independent ITDMA state bits")
        effective = 8191 + (8192 if code >= 5 else 0)
        slots = code + 1 if code < 5 else code - 4
        require(1 <= slots <= 5 and effective in (8191, 16383), "ITDMA semantic oracle")
    for timeout in range(8):
        submessage = 4499 if timeout == 0 else 16383 if timeout % 2 else 2249
        if timeout == 1:
            submessage = (23 << 9) | (59 << 2)
        require(states[f"sotdma{timeout}"] == ((2 << 17) | (timeout << 14) | submessage),
                "independent SOTDMA timeout-dependent bits")
    return fields_checked


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", required=True)
    args = parser.parse_args()
    env = dict(os.environ, CUDA_VISIBLE_DEVICES="-1")
    first = subprocess.run([args.exe, "--vectors"], check=True, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, env=env).stdout
    second = subprocess.run([args.exe, "--vectors"], check=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env).stdout
    require(first == second, "fixture output changed between fresh processes")
    fields = check_output(first)
    counts = corruptions(first)
    for category, count in sorted(counts.items()):
        print(f"NEGATIVE {category}={count}")
    print(f"PASS independent Class A oracle: payloads=3 exact_fields={fields} "
          f"state_modes=16 rejected={sum(counts.values())} deterministic_processes=2")


if __name__ == "__main__":
    main()
