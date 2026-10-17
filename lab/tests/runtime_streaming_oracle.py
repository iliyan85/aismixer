"""Bounded, independent StreamingV2 fixture reader. Infrastructure, NOT AIS RF.

Uses only the standard library; does not import native code or retain history.
"""
import argparse
import hashlib
from pathlib import Path
import re
import runpy
import struct
import subprocess
import tempfile

_v1 = runpy.run_path(str(Path(__file__).with_name("runtime_cli_oracle.py")))
Cursor, require, fnv = _v1["Cursor"], _v1["require"], _v1["fnv"]
UNITS, SLOT, RECORDS, TXS, RXS = 9_830_400, 262_144, 97_210, 10_800, 32_400
GOLDEN_A = b"!AIVDM,1,1,,A,1Amg=5EmQso>f;LFGd5DlSoo7D<Q,0*2B\r\n"
GOLDEN_B = b"!AIVDM,1,1,,B,1Amg=5EmQso>f;LFGd5DlSoo7D<Q,0*28\r\n"
RECEIVERS = [f"receiver.R{i}" for i in (1, 2, 3)]
PHASE = {1: 0, 2: 0, 3: 0, 4: 0, 10: 4, 11: 1, 13: 2, 15: 5, 19: 6}
# Pinned after independent field/identity/causality/CRC verification, not an RF oracle.
PINNED_LOGICAL = 0x7805B698778EDACA
PINNED_RECEIVER_SHA256 = "dd9054ad8b67b02a421c7aedb4dcfdae5cecb6a6444c0e510bfcf60ec438eab9"


def table_entry(value):
    for _ in range(8):
        value = (value >> 1) ^ (0x82F63B78 if value & 1 else 0)
    return value


TABLE = tuple(table_entry(i) for i in range(256))


class Crc:
    def __init__(self):
        self.state = 0xFFFFFFFF

    def update(self, data):
        state = self.state
        for byte in data:
            state = (state >> 8) ^ TABLE[(state ^ byte) & 255]
        self.state = state

    def value(self):
        return self.state ^ 0xFFFFFFFF


def crc(data):
    result = Crc()
    result.update(data)
    return result.value()


def envelope(path, kind):
    with path.open("rb") as stream:
        header = exact(stream, 24)
        require(header[:8] == b"AISJR0F1", "envelope magic")
        version, actual, units, n = struct.unpack_from("<HHQI", header, 8)
        require((version, actual, units) == (2, kind, UNITS), "explicit streaming version/kind/timebase")
        require(n <= 262144, "bounded metadata payload before allocation")
        raw = header + exact(stream, n) + exact(stream, 4)
        require(stream.read(1) == b"" and crc(raw[:-4]) == struct.unpack_from("<I", raw, len(raw)-4)[0], "metadata framing/CRC")
    return Cursor(raw[24:-4]), fnv(raw)


def exact(stream, n):
    result = stream.read(n)
    require(len(result) == n, "truncated stream")
    return result


def identity(value, domain, producer):
    match = re.fullmatch(r"(tx|rx):([A-Za-z0-9_.-]{1,32}):([1-9][0-9]{0,19})", value)
    require(match is not None and match[1] == domain and match[2] == producer, "injective producer-bound identity")
    counter = int(match[3])
    require(counter <= (1 << 64)-1, "identity overflow")
    return counter


def read_record(raw):
    c = Cursor(raw)
    require(c.num("I") == 2, "event schema2")
    time, kind = c.num("q"), c.num("H")
    require(kind in PHASE, "known scripted kind")
    source, subject, event_id = c.text(64), c.text(64), c.event_id()
    has_cause = c.num("B")
    require(has_cause in (0, 1), "cause flag")
    cause = c.event_id() if has_cause else None
    if kind in (1, 2, 3, 4):
        payload = c.text(64)
    elif kind == 10:
        payload = [c.text(64), c.text(64), c.num("B"), c.num("q"), c.text(64)]
        n = c.num("I")
        require(n <= 512, "bounded TX coverage")
        payload += [[c.text(64) for _ in range(n)], c.num("q")]
    elif kind == 11:
        payload = c.text(64)
    elif kind == 13:
        payload = [c.text(64), c.text(64), c.text(64), c.num("q"), c.text()]
    elif kind == 15:
        payload = [c.text(64), c.text(64), c.text(64)]
        n = c.num("I")
        require(1 <= n <= 9, "NMEA fragment count")
        payload += [[c.text(82).encode("ascii") for _ in range(n)]]
    else:
        payload = [c.text(64), c.num("q")]
    c.end()
    require(event_id[0] == source and event_id[1] > 0, "event producer sequence")
    return time, kind, source, subject, event_id, cause, payload


def read_fixture(directory):
    c, chain = envelope(directory / "manifest-incomplete.bin", 1)
    require(c.num("I") == 2 and c.num("B") == 0, "declared streaming profile/status")
    require("NOT AIS RF SIMULATION" in c.text(), "honest synthetic provenance")
    require(c.num("B") == 0, "no invented randomized fixture seed")
    require((c.num("q"), c.num("q")) == (1792195199, UNITS // 2), "explicit UTC anchor")
    require(c.text(128) == "r01-streaming-infrastructure-fixture-v2", "fixture model")
    require(c.text(128) == "B3-pinned-finalized-NMEA-bytes-v1", "pinned output provenance")
    require([c.text(64) for _ in range(c.num("I"))] == RECEIVERS, "receiver catalog")
    limit, byte_limit = c.num("I"), c.num("I")
    c.end()
    terminal, _ = envelope(directory / "manifest-complete.bin", 3)
    require(terminal.num("B") == 1, "COMPLETE")
    segments, records, digest, final_chain = [terminal.num("Q") for _ in range(4)]
    terminal.end()
    require(records == RECORDS and segments == (RECORDS + limit-1)//limit, "fixture record/segment count")
    logical, count, tx_count, rx_count, closures, peak = 14695981039346656037, 0, 0, 0, 0, 0
    sources, tx_high, rx_high, live, receptions, anchors, station_on = {}, {}, {}, {}, {}, {}, {}
    stream_hashes = {rx: hashlib.sha256() for rx in RECEIVERS}
    stream_counts = {rx: 0 for rx in RECEIVERS}
    last_key = None
    for index in range(1, segments+1):
        cp, next_chain = envelope(directory / f"checkpoint-{index:010d}.bin", 2)
        ci, n, size, expected_crc = cp.num("Q"), cp.num("I"), cp.num("Q"), cp.num("I")
        total, cumulative, prior, first, last = cp.num("Q"), cp.num("Q"), cp.num("Q"), cp.num("q"), cp.num("q")
        cp.end()
        require(ci == index and prior == chain and 0 < n <= limit and size <= byte_limit, "bounded checkpoint chain")
        with (directory / f"segment-{index:010d}.bin").open("rb") as stream:
            header = exact(stream, 36)
            require(header[:8] == b"AISJR0S1" and struct.unpack_from("<HHQIIQ", header, 8) ==
                    (2, 36, index, n, size-40, UNITS), "version2 segment header")
            segment_crc, consumed = Crc(), 0
            segment_crc.update(header)
            first_seen = last_seen = None
            for _ in range(n):
                length = exact(stream, 4)
                length_value = struct.unpack("<I", length)[0]
                require(0 < length_value <= 65536 and consumed + length_value + 8 <= size-40, "record length bounds")
                raw, suffix = exact(stream, length_value), exact(stream, 4)
                require(crc(raw) == struct.unpack("<I", suffix)[0], "record CRC32C")
                for part in (length, raw, suffix):
                    segment_crc.update(part)
                logical = fnv(length + raw, logical)
                time, kind, source, subject, event_id, cause, p = read_record(raw)
                key = time, PHASE[kind], source.encode("ascii"), event_id[1], kind
                require(last_key is None or last_key < key, "canonical release order")
                require(event_id[1] > sources.get(source, 0), "source highwater across segments")
                if cause:
                    require(cause in anchors and (anchors[cause][0] < time or
                            (anchors[cause][0] == time and anchors[cause][1] < PHASE[kind])), "published typed causal anchor")
                sources[source] = event_id[1]
                if kind in (1, 2, 3, 4):
                    require(subject == p and time == (0 if kind in (1, 3) else 18000 * UNITS), "lifecycle truth")
                    anchors[event_id] = time, PHASE[kind]
                    if kind == 1:
                        station_on[subject] = event_id
                elif kind == 10:
                    tx, station, channel, end, payload, roster, horizon = p
                    counter = identity(tx, "tx", source)
                    require(counter > tx_high.get(source, 0) and tx not in live, "run-global TX identity highwater")
                    require(station in ("station.A", "station.B"), "two explicit symbolic producers")
                    interval = 10 if station == "station.A" else 2
                    require(station == source == subject and time == (counter-1)*interval*UNITS and
                            channel == (0 if interval == 10 else 1) and end == time+SLOT and horizon == end+2 and
                            payload == "fixture.b3.type1.golden" and roster == RECEIVERS and cause == station_on[station],
                            "independent illustrative TX schedule/snapshot")
                    tx_high[source] = counter
                    live[tx] = dict(start=event_id, end=None, horizon=horizon, channel=channel, outputs=set(), rxs=[])
                    anchors[event_id] = time, PHASE[kind]
                    tx_count += 1
                elif kind == 11:
                    tx = live[p]
                    require(cause == tx["start"] and tx["end"] is None, "exact TX end cause")
                    tx["end"] = event_id
                    anchors[event_id] = time, PHASE[kind]
                elif kind == 13:
                    rx, tx_id, receiver, availability, detail = p
                    tx = live[tx_id]
                    counter = identity(rx, "rx", source)
                    require(counter > rx_high.get(source, 0) and receiver == source == subject and
                            availability == time == tx["horizon"]-1 and cause == tx["end"] and
                            "NOT RF" in detail, "per-receiver scripted reception correlation")
                    rx_high[source] = counter
                    receptions[rx] = receiver, tx_id, event_id
                    tx["rxs"].append(rx)
                    anchors[event_id] = time, PHASE[kind]
                    rx_count += 1
                elif kind == 15:
                    receiver, rx_id, tx_id, sentences = p
                    tx = live[tx_id]
                    require(receptions[rx_id] == (receiver, tx_id, cause) and receiver == source == subject and
                            receiver not in tx["outputs"] and sentences == [GOLDEN_A if tx["channel"] == 0 else GOLDEN_B],
                            "exact independent per-receiver B3 bytes")
                    tx["outputs"].add(receiver)
                    stream_hashes[receiver].update(b"".join(sentences))
                    stream_counts[receiver] += 1
                elif kind == 19:
                    tx_id, horizon = p
                    tx = live[tx_id]
                    require(horizon == time == tx["horizon"] and tx["outputs"] == set(RECEIVERS) and
                            cause == tx["end"] and source == "runtime.closure", "safe final retirement")
                    anchors.pop(tx["start"])
                    anchors.pop(tx["end"])
                    for rx in tx["rxs"]:
                        anchors.pop(receptions.pop(rx)[2])
                    del live[tx_id]
                    closures += 1
                peak = max(peak, len(live) + len(receptions))
                require(len(live) <= 2 and len(receptions) <= 6 and len(sources) <= 6 and len(anchors) <= 32, "bounded independent active retention")
                last_key, last_seen = key, time
                if first_seen is None:
                    first_seen = time
                consumed += length_value + 8
                count += 1
            require(struct.unpack("<I", exact(stream, 4))[0] == expected_crc == segment_crc.value() and
                    stream.read(1) == b"" and consumed == size-40, "segment checksum/exact extent")
            require((total, cumulative, first, last) == (count, logical, first_seen, last_seen), "checkpoint stream evidence")
        chain = next_chain
    require((count, logical, chain, tx_count, rx_count, closures) ==
            (RECORDS, digest, final_chain, TXS, RXS, TXS) and not live and not receptions, "terminal bounded run evidence")
    require(all(value == TXS for value in stream_counts.values()), "three complete receiver streams")
    require(digest == PINNED_LOGICAL, "pinned StreamingV2 fixture digest")
    require(all(h.hexdigest() == PINNED_RECEIVER_SHA256 for h in stream_hashes.values()), "pinned receiver stream bytes")
    return digest, tuple(h.hexdigest() for h in stream_hashes.values()), segments, peak


def run(exe, *args):
    result = subprocess.run([str(exe), *map(str, args)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)
    require(result.returncode == 0 and result.stdout == b"" and b"NOT AIS RF SIMULATION" in result.stderr,
            f"explicit streaming fixture failed: {result.returncode}, {result.stderr!r}")


def bundle(directory):
    result = hashlib.sha256()
    terminal, _ = envelope(directory / "manifest-complete.bin", 3)
    require(terminal.num("B") == 1, "bundle requires COMPLETE")
    segments = terminal.num("Q")
    require(segments <= 1_000_000, "bounded bundle segment count")
    require(sum(1 for _ in directory.iterdir()) == segments*2+2, "fixture bundle contains only committed files")
    def names():
        for i in range(1, segments+1):
            yield f"checkpoint-{i:010d}.bin"
        yield "manifest-complete.bin"
        yield "manifest-incomplete.bin"
        for i in range(1, segments+1):
            yield f"segment-{i:010d}.bin"
    for name in names():
        path = directory / name
        result.update(path.name.encode("ascii") + b"\0")
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(65536), b""):
                result.update(block)
    return result.hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", required=True, type=Path)
    parser.add_argument("--other-exe", type=Path)
    parser.add_argument("--work-dir", required=True, type=Path)
    args = parser.parse_args()
    require(crc(b"123456789") == 0xE3069283, "table CRC32C KAT")
    args.work_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="r01-", dir=args.work_dir) as temporary:
        root = Path(temporary)
        small, large = root / "small", root / "large"
        run(args.exe, "--streaming-fixture", small, "--segment-records", 127)
        run(args.exe, "--streaming-fixture", large, "--segment-records", 4096, "--fine-steps")
        first, second = read_fixture(small), read_fixture(large)
        require(first[:2] == second[:2], "segment/step-independent canonical content and receiver bytes")
        require(len(set(first[1])) == 1, "independent receiver stream byte equivalence")
        print(f"StreamingV2 oracle PASS: NOT AIS RF; 18000 simulation seconds; TX={TXS} RX={RXS} records={RECORDS}; segments={first[2]}/{second[2]}; digest={first[0]:016x}; active_peak={first[3]}; receiver_sha256={first[1][0]}")
        if args.other_exe:
            independent = root / "independent"
            run(args.other_exe, "--streaming-fixture", independent, "--segment-records", 4096, "--fine-steps")
            require(read_fixture(independent) == second, "independent build changed StreamingV2 semantics")
            require(bundle(independent) == bundle(large), "independent build changed StreamingV2 physical files")
            print(f"StreamingV2 independent build PASS: bundle_sha256={bundle(large)}")


if __name__ == "__main__":
    main()
