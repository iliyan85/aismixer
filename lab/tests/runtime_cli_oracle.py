"""Independent Journal v1 byte reader and native R0 CLI acceptance (stdlib only).

The pinned sentence is the qualified synthetic B3 Type 1 vector, not RF truth.
This reader does not import, call, or generate references with the C++ codec.
"""
import argparse
import hashlib
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile

UNITS = 9_830_400
GOLDEN = b"!AIVDM,1,1,,A,1Amg=5EmQso>f;LFGd5DlSoo7D<Q,0*2B\r\n"
PHASE = {1: 0, 2: 0, 3: 0, 4: 0, 5: 3, 6: 3, 7: 3, 8: 3,
         9: 3, 10: 4, 11: 1, 12: 1, 13: 2, 14: 2, 15: 5, 16: 5}


def require(value, message):
    if not value:
        raise AssertionError(message)


def crc32c(data):
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0x82F63B78 if crc & 1 else 0)
    return crc ^ 0xFFFFFFFF


def fnv(data, state=14695981039346656037):
    for byte in data:
        state = ((state ^ byte) * 1099511628211) & ((1 << 64) - 1)
    return state


class Cursor:
    def __init__(self, data):
        self.data, self.pos = data, 0

    def take(self, n):
        require(0 <= n <= len(self.data) - self.pos, "truncated field")
        result = self.data[self.pos:self.pos + n]
        self.pos += n
        return result

    def num(self, fmt):
        return struct.unpack("<" + fmt, self.take(struct.calcsize("<" + fmt)))[0]

    def text(self, limit=4096):
        n = self.num("I")
        require(n <= limit, "oversize text")
        return self.take(n).decode("utf-8", errors="strict")

    def event_id(self):
        return self.text(64), self.num("Q")

    def end(self):
        require(self.pos == len(self.data), "trailing fields")


def envelope(path, kind):
    raw = path.read_bytes()
    require(28 <= len(raw) <= 262144 + 28, "metadata size")
    require(raw[:8] == b"AISJR0F1", "metadata magic")
    version, actual, units, length = struct.unpack_from("<HHQI", raw, 8)
    require((version, actual, units) == (1, kind, UNITS), "metadata schema/timebase")
    require(len(raw) == 28 + length, "metadata framing")
    require(crc32c(raw[:-4]) == struct.unpack_from("<I", raw, len(raw)-4)[0], "metadata CRC")
    return Cursor(raw[24:-4]), fnv(raw)


def event(raw):
    c = Cursor(raw)
    require(c.num("I") == 1, "event schema")
    time, kind = c.num("q"), c.num("H")
    require(kind in PHASE, "event kind")
    source, subject, identity = c.text(64), c.text(64), c.event_id()
    has_cause = c.num("B")
    require(has_cause in (0, 1), "cause boolean")
    cause = c.event_id() if has_cause else None
    require(identity[0] == source and identity[1] > 0, "stable source-local identity")
    receiver, payload = None, None
    if kind in (1, 2, 3, 4):
        payload = c.text(64)
        if kind in (3, 4):
            receiver = payload
    elif kind in (5, 6, 7, 8):
        payload = (c.text(64), c.num("B"), c.num("H"), c.text())
    elif kind == 9:
        payload = (c.text(64), c.text())
    elif kind == 10:
        payload = (c.text(64), c.text(64), c.num("B"), c.num("q"), c.text())
    elif kind == 11:
        payload = c.text(64)
    elif kind == 12:
        tx, receiver, availability = c.text(64), c.text(64), c.num("q")
        carrier, has_station = c.num("B"), c.num("B")
        require(carrier in (0, 1) and has_station in (0, 1), "RF booleans")
        payload = (tx, receiver, availability, carrier, c.text(64) if has_station else None, c.text())
    elif kind in (13, 14):
        rx, tx, receiver, availability = c.text(64), c.text(64), c.text(64), c.num("q")
        require(availability == time, "delivered reception availability")
        payload = (rx, tx, receiver, availability, c.text())
    elif kind == 15:
        receiver, rx, tx = c.text(64), c.text(64), c.text(64)
        count = c.num("I")
        require(1 <= count <= 9, "fragment count")
        payload = (receiver, rx, tx, tuple(c.text(82).encode("ascii") for _ in range(count)))
    elif kind == 16:
        receiver = c.text(64)
        payload = (receiver, c.text(), c.text())
    c.end()
    return dict(time=time, kind=kind, source=source, subject=subject, id=identity,
                cause=cause, receiver=receiver, payload=payload,
                key=(time, PHASE[kind], source.encode("ascii"), identity[1], kind))


def read_fixture(directory):
    c, chain = envelope(directory / "manifest-incomplete.bin", 1)
    require(c.num("B") == 0, "initial status")
    provenance = c.text()
    require("NOT AIS RADIO SIMULATION" in provenance, "fixture provenance")
    seeded = c.num("B")
    require(seeded == 0, "fixture has no randomized activation seed")
    require((c.num("q"), c.num("q")) == (1792195199, UNITS // 2), "explicit UTC anchor")
    require(c.text(128) == "r0-scripted-infrastructure-fixture-v1", "fixture model")
    require(c.text(128) == "B3-pinned-finalized-NMEA-bytes-v1", "codec provenance")
    receivers = [c.text(64) for _ in range(c.num("I"))]
    require(receivers == ["receiver.R1", "receiver.R2", "receiver.R3"], "receiver catalog")
    record_limit, byte_limit = c.num("I"), c.num("I")
    c.end()
    terminal, _ = envelope(directory / "manifest-complete.bin", 3)
    require(terminal.num("B") == 1, "COMPLETE status")
    segments, records, digest, final_chain = [terminal.num("Q") for _ in range(4)]
    terminal.end()
    require(records == 17 and 1 <= segments <= 17, "fixture record/segment counts")
    logical, rolling, output, sources, ids = [], 14695981039346656037, {}, {}, {}
    for index in range(1, segments + 1):
        cp, next_chain = envelope(directory / f"checkpoint-{index:010d}.bin", 2)
        ci, nr, size, expected_crc = cp.num("Q"), cp.num("I"), cp.num("Q"), cp.num("I")
        total, expected_digest, prior = cp.num("Q"), cp.num("Q"), cp.num("Q")
        first, last = cp.num("q"), cp.num("q")
        cp.end()
        require(ci == index and prior == chain and 1 <= nr <= record_limit, "checkpoint chain/bounds")
        raw = (directory / f"segment-{index:010d}.bin").read_bytes()
        require(len(raw) == size <= byte_limit, "segment length limit")
        require(raw[:8] == b"AISJR0S1", "segment magic")
        require(struct.unpack_from("<HHQIIQ", raw, 8) == (1, 36, index, nr, size-40, UNITS), "segment header")
        require(crc32c(raw[:-4]) == expected_crc == struct.unpack_from("<I", raw, size-4)[0], "segment CRC")
        body = Cursor(raw[36:-4])
        times = []
        for _ in range(nr):
            n = body.num("I")
            require(0 < n <= 65536, "record bound")
            encoded = body.take(n)
            require(body.num("I") == crc32c(encoded), "record CRC")
            rolling = fnv(struct.pack("<I", n) + encoded, rolling)
            row = event(encoded)
            if logical:
                require(row["key"] > logical[-1]["key"], "canonical total order")
            require(row["id"][1] > sources.get(row["source"], 0), "source sequence monotonicity")
            if row["cause"]:
                require(row["cause"] in ids, "proven earlier cause")
                cause = ids[row["cause"]]
                require(cause["time"] < row["time"] or
                        (cause["time"] == row["time"] and PHASE[cause["kind"]] < PHASE[row["kind"]]), "causal phase")
            sources[row["source"]] = row["id"][1]
            ids[row["id"]] = row
            logical.append(row)
            times.append(row["time"])
            if row["kind"] == 15:
                output.setdefault(row["receiver"], bytearray()).extend(b"".join(row["payload"][3]))
        body.end()
        require((total, expected_digest, first, last) == (len(logical), rolling, times[0], times[-1]), "checkpoint content")
        chain = next_chain
    require(len(logical) == records and rolling == digest and chain == final_chain, "terminal content digest")
    end = 3 * UNITS + 262144
    expected_keys = [(0, 3, f"receiver.R{i}", 1) for i in (1, 2, 3)] + [
        (0, 1, "station.A", 1), (UNITS, 4, "receiver.R3", 2),
        (2 * UNITS, 3, "receiver.R3", 3), (3 * UNITS, 10, "station.A", 2),
        (end, 11, "station.A", 3), (end, 13, "receiver.R1", 2),
        (end, 13, "receiver.R2", 2), (end, 14, "receiver.R3", 4),
        (end, 15, "receiver.R1", 3), (end, 15, "receiver.R2", 3),
        (4 * UNITS, 4, "receiver.R1", 4), (4 * UNITS, 4, "receiver.R2", 4),
        (4 * UNITS, 4, "receiver.R3", 5), (4 * UNITS, 2, "station.A", 4)]
    require([(r["time"], r["kind"], r["source"], r["id"][1]) for r in logical] == expected_keys,
            "pinned stable fixture identities/lifecycle chronology")
    require(logical[6]["payload"] == ("fixture.tx.1", "station.A", 0, end, "fixture.b3.type1.golden"),
            "immutable logical TX association")
    for index, row in enumerate(logical[8:11], 1):
        require(row["payload"][:4] == (f"fixture.rx.{index}", "fixture.tx.1", f"receiver.R{index}", end),
                "independent reception correlation")
    require(sum(row["kind"] == 10 for row in logical) == 1, "one logical TX")
    require(sum(row["kind"] == 13 for row in logical) == 2, "two scripted successes")
    require(sum(row["kind"] == 14 for row in logical) == 1, "one scripted failure")
    require(output == {"receiver.R1": GOLDEN, "receiver.R2": GOLDEN}, "independent fixed NMEA")
    require(any(row["kind"] == 4 and row["receiver"] == "receiver.R3" and row["time"] == UNITS for row in logical), "OFF fixture interval")
    return logical, digest, segments


def run(exe, *args, success=True):
    result = subprocess.run([str(exe), *map(str, args)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
    require((result.returncode == 0) == success, f"CLI exit {args}: {result.returncode}, {result.stderr!r}")
    return result


def file_hashes(directory):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.iterdir() if p.is_file()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--other-exe", type=Path, help="Optional independently built CLI for byte/digest comparison")
    parser.add_argument("--work-dir", type=Path, required=True)
    args = parser.parse_args()
    require(crc32c(b"123456789") == 0xE3069283, "CRC32C independent KAT")
    require(len(GOLDEN) == 49 and GOLDEN.endswith(b"\r\n"), "independent NMEA framing")
    args.work_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="r0-", dir=args.work_dir) as root:
        root = Path(root)
        tiny, huge, repeated = root / "запис 海 1", root / "huge", root / "repeated"
        for directory, limit in ((tiny, 1), (huge, 65536), (repeated, 1)):
            result = run(args.exe, "--fixture", directory, "--segment-records", limit)
            require(result.stdout == b"" and b"SYNTHETIC INFRASTRUCTURE TEST (NOT AIS RADIO SIMULATION)" in result.stderr, "fixture stderr label/stdout purity")
        a, digest, count = read_fixture(tiny)
        b, other, huge_count = read_fixture(huge)
        require(a == b and digest == other and count == 17 and huge_count == 1, "segment-size independent logical history")
        require(file_hashes(tiny) == file_hashes(repeated), "repeat physical bytes")
        baseline = file_hashes(tiny)
        run(args.exe, "--fixture", tiny, success=False)
        require(file_hashes(tiny) == baseline, "existing run no-clobber")
        for receiver in ("receiver.R1", "receiver.R2", "receiver.R3"):
            expected = GOLDEN if receiver != "receiver.R3" else b""
            for events, view in (("disabled", "all"), ("all", "station.A"), ("reception,output,metrics", "receiver.R1")):
                result = run(args.exe, "--play", tiny, "--receiver", receiver, "--speed", "fast", "--events", events, "--view", view)
                require(result.stdout == expected, "pure per-receiver stored CRLF bytes independent of view")
                if events == "disabled":
                    require(result.stderr == b"", "disabled diagnostics")
                else:
                    require(b"[r0 " in result.stderr, "selected stderr event diagnostics")
        require(file_hashes(tiny) == baseline, "playback/filter cannot mutate journal")
        run(args.exe, "--play", tiny, "--speed", "fast", success=False)
        for options in (("--receiver", "receiver.unknown"), ("--speed", "nan"), ("--events", "bogus")):
            require(run(args.exe, "--play", tiny, *options, success=False).stdout == b"", "invalid input emits no NMEA")
        destination = root / "изход 海.nmea"
        result = run(args.exe, "--play", tiny, "--receiver", "receiver.R1", "--speed", "fast", "--output", destination)
        require(result.stdout == b"" and destination.read_bytes() == GOLDEN, "Unicode no-clobber named output")
        run(args.exe, "--play", tiny, "--receiver", "receiver.R1", "--speed", "fast", "--output", destination, success=False)
        require(destination.read_bytes() == GOLDEN, "existing NMEA preserved")
        # The pipe has no reader BEFORE process creation: no scheduler race or sleep.
        read_fd, write_fd = os.pipe()
        os.close(read_fd)
        try:
            process = subprocess.Popen([str(args.exe), "--play", str(tiny), "--receiver", "receiver.R1", "--speed", "fast"], stdout=write_fd, stderr=subprocess.PIPE)
        finally:
            os.close(write_fd)
        try:
            _, diagnostics = process.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise
        require(process.returncode != 0 and diagnostics, "closed stdout must fail nonzero")
        for mode in ("corrupt", "truncate", "version", "incomplete", "missing"):
            damaged = root / mode
            shutil.copytree(tiny, damaged)
            first = damaged / "segment-0000000001.bin"
            data = bytearray(first.read_bytes())
            if mode == "corrupt":
                data[45] ^= 0x40
                first.write_bytes(data)
            elif mode == "truncate":
                first.write_bytes(data[:-1])
            elif mode == "version":
                data[8] = 2
                first.write_bytes(data)
            elif mode == "incomplete":
                (damaged / "manifest-complete.bin").unlink()
            else:
                first.unlink()
            result = run(args.exe, "--play", damaged, "--receiver", "receiver.R1", "--speed", "fast", success=False)
            require(result.stdout == b"", "damaged/incomplete recording rejected before output")
        (tiny / "uncommitted.aismixer-tmp-orphan").write_bytes(b"interrupted write")
        require(run(args.exe, "--play", tiny, "--receiver", "receiver.R1", "--speed", "fast").stdout == GOLDEN, "stray temp ignored")
        if args.other_exe:
            cross_a, cross_b = root / "build-a", root / "build-b"
            run(args.exe, "--fixture", cross_a, "--segment-records", 2)
            run(args.other_exe, "--fixture", cross_b, "--segment-records", 2)
            require(file_hashes(cross_a) == file_hashes(cross_b), "independent build changed physical journal bytes")
            require(read_fixture(cross_a) == read_fixture(cross_b), "independent build changed canonical events")
            combined = hashlib.sha256()
            for file in sorted(cross_a.iterdir()):
                combined.update(file.name.encode("ascii") + b"\0" + file.read_bytes())
            print(f"independent builds PASS: logical_digest={digest:016x}; physical_bundle_sha256={combined.hexdigest()}")
        print(f"runtime CLI/oracle PASS: 17 events; segments=17/1; logical_digest={digest:016x}; NMEA=49/49/0; Unicode, corruption, closed stdout, no-clobber")


if __name__ == "__main__":
    main()
