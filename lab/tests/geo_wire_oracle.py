"""Independent stdlib acceptance oracle for GPU geography -> offline AIS.

No CUDA, YAML, geospatial, or production AIS Python packages are imported.
Integer AIS rounding, reverse armoring, wire fields, checksums, canonical FNV
records and the historical B2 digest are independently computed here.
"""

import argparse
import csv
import ctypes
import hashlib
import io
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile


# Qualified default-profile vectors, not claims for every user-supplied domain.
QUALIFIED_PROFILE_HASH = "9ab63fc6b9e76951"
QUALIFIED_DEMO_DIGEST = "48dec953d3129a2d"
# Independent pin of all shipped UTF-8 profile bytes, including metadata and
# coordinates. Normalize CRLF only so checkout EOL conversion is immaterial.
# Updating this pin is a deliberate profile/golden review, never an inference
# from a digest reported by the executable under test.
QUALIFIED_PROFILE_SHA256 = "0b880083e2147310b55decb3c35e9d58f5a660c0ff7828c76bc5e0b8f1c92bc2"


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def fnv(data):
    value = 14695981039346656037
    for byte in data:
        value = ((value ^ byte) * 1099511628211) & ((1 << 64) - 1)
    return f"{value:016x}"


def shipped_profile(domain, source_dir):
    reference = (source_dir / "geo" / "profiles" / "black_sea_demo.yaml").resolve()
    if domain.resolve() != reference:
        return False
    normalized = domain.read_bytes().replace(b"\r\n", b"\n")
    require(hashlib.sha256(normalized).hexdigest() == QUALIFIED_PROFILE_SHA256,
            "repository-shipped profile pin changed: review coordinates/metadata and expected golden explicitly")
    return True


def compiler_evidence(executable):
    """Read CMake's configured compiler facts independently of CLI metadata."""
    for directory in executable.resolve().parents:
        if not (directory / "CMakeCache.txt").is_file():
            continue
        cuda_files = sorted(directory.glob("CMakeFiles/*/CMakeCUDACompiler.cmake"))
        cxx_files = sorted(directory.glob("CMakeFiles/*/CMakeCXXCompiler.cmake"))
        if not cuda_files or not cxx_files:
            return None, "configured CMake compiler evidence unavailable"

        def setting(path, name):
            match = re.search(r"^set\(" + re.escape(name) + r' "([^"\r\n]*)"\)',
                              path.read_text(encoding="utf-8"), re.MULTILINE)
            return match.group(1) if match else None

        cuda_versions = {setting(path, "CMAKE_CUDA_COMPILER_VERSION") for path in cuda_files}
        cxx_versions = {setting(path, "CMAKE_CXX_COMPILER_VERSION") for path in cxx_files}
        cxx_ids = {setting(path, "CMAKE_CXX_COMPILER_ID") for path in cxx_files}
        if (None in cuda_versions | cxx_versions | cxx_ids or
                len(cuda_versions) != 1 or len(cxx_versions) != 1 or len(cxx_ids) != 1):
            return None, "CMake compiler evidence is incomplete or ambiguous"
        project = directory / "aismixer_lab_geo_engine.vcxproj"
        fmad_disabled = project.is_file() and "--fmad=false" in project.read_text(encoding="utf-8-sig")
        return (cuda_versions.pop(), cxx_ids.pop(), cxx_versions.pop(), fmad_disabled), ""
    return None, "executable has no adjacent configured CMake build evidence"


def cuda_driver_evidence():
    """Query the installed CUDA driver directly, not the program's own claims."""
    if os.name != "nt":
        return None, "Windows qualification is unavailable on this host"
    try:
        driver = ctypes.WinDLL("nvcuda.dll")
        integer_pointer = ctypes.POINTER(ctypes.c_int)
        signatures = {
            "cuInit": [ctypes.c_uint],
            "cuDeviceGet": [integer_pointer, ctypes.c_int],
            "cuDeviceComputeCapability": [integer_pointer, integer_pointer, ctypes.c_int],
            "cuDeviceGetName": [ctypes.POINTER(ctypes.c_char), ctypes.c_int, ctypes.c_int],
            "cuDriverGetVersion": [integer_pointer],
        }
        for name, arguments in signatures.items():
            function = getattr(driver, name)
            function.argtypes = arguments
            function.restype = ctypes.c_int

        def checked(name, *arguments):
            status = getattr(driver, name)(*arguments)
            if status != 0:
                raise RuntimeError(f"{name} returned CUDA driver status {status}")

        checked("cuInit", 0)
        device, major, minor, version = (ctypes.c_int() for _ in range(4))
        checked("cuDeviceGet", ctypes.byref(device), 0)
        checked("cuDeviceComputeCapability", ctypes.byref(major), ctypes.byref(minor), device.value)
        name = ctypes.create_string_buffer(256)
        checked("cuDeviceGetName", name, len(name), device.value)
        checked("cuDriverGetVersion", ctypes.byref(version))
        return (f"{major.value}.{minor.value}", name.value.decode("utf-8"), str(version.value)), ""
    except (OSError, AttributeError, RuntimeError, UnicodeError) as error:
        return None, f"independent CUDA driver evidence unavailable: {error}"


def qualified_platform(executable, values):
    compilers, compiler_reason = compiler_evidence(executable)
    device, device_reason = cuda_driver_evidence()
    if compilers:
        cuda, cxx_id, cxx, fmad_disabled = compilers
        require(cuda in values["build_toolchain"] and cxx in values["build_toolchain"],
                "native build-toolchain metadata disagrees with independently configured compiler versions")
    if device:
        require((values["compute_capability"], values["gpu"], values["driver_version"]) == device,
                "native device metadata disagrees with independent CUDA driver query")
    if not compilers or not device:
        return False, "; ".join(reason for reason in (compiler_reason, device_reason) if reason)
    if compilers != ("12.9.86", "MSVC", "19.44.35229.0", True) or device[0] != "6.1":
        return False, (f"non-reference platform: CUDA={cuda}, {cxx_id}={cxx}, "
                       f"sm={device[0]}, --fmad=false={fmad_disabled}")
    require(values["runtime_version"] == "12090", "qualified CUDA 12.9 build reports another runtime")
    return True, "independent CMake CUDA12.9.86/MSVC19.44.35229.0/--fmad=false and driver sm6.1"


def wire_coordinate(ndeg):
    numerator = abs(ndeg) * 3
    whole, remainder = divmod(numerator, 5000)
    rounded = whole + (remainder >= 2500)
    return -rounded if ndeg < 0 else rounded


def metadata(stderr):
    values = {}
    events = []
    metrics = []
    for line in stderr.decode("utf-8").splitlines():
        if line.startswith("event="):
            event = tuple(map(int, line[6:].split(",")))
            require(len(event) == 7, "event metadata field count")
            events.append(event)
        elif line.startswith("case="):
            metrics.append(dict(part.split("=", 1) for part in line.split()))
        elif "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values, events, metrics


def decode_sentence(line):
    require(line.endswith(b"\r\n") and len(line) <= 82, "NMEA CRLF/82-byte bound")
    require(line.startswith(b"!AIVDM,"), "offline stream contains diagnostic prose or another sentence type")
    body, check = line[1:-2].split(b"*")
    require(re.fullmatch(rb"[0-9A-F]{2}", check), "checksum is not canonical uppercase hex")
    checksum = 0
    for byte in body:
        checksum ^= byte
    require(checksum == int(check, 16), "independent XOR checksum mismatch")
    fields = body.split(b",")
    require(len(fields) == 7 and fields[:5] == [b"AIVDM", b"1", b"1", b"", b"A"],
            "offline canonical single-fragment channel-A framing")
    require(fields[6] == b"0", "168-bit Type1/18 unexpectedly has fill")
    bits = ""
    for byte in fields[5]:
        require(48 <= byte <= 87 or 96 <= byte <= 119, "invalid AIS armor character")
        value = byte - 48 if byte <= 87 else byte - 56
        bits += format(value, "06b")
    require(len(bits) == 168, "offline Type1/18 exact bit count")
    return bits


def check_field(bits, offset, width, expected, name, signed=False):
    value = int(bits[offset:offset + width], 2)
    if signed and bits[offset] == "1":
        value -= 1 << width
    require(value == expected, f"{name}: {value} != {expected}")


def check_wire(stdout, events):
    lines = stdout.splitlines(keepends=True)
    require(len(lines) == len(events), "accepted event/NMEA cardinality differs")
    lengths = []
    for line, event in zip(lines, events):
        tick, seconds, actor_id, category, longitude, latitude, mmsi = event
        require(category in (0, 1), "unsupported generic actor category reached AIS output")
        require(actor_id != mmsi and 1 <= mmsi <= 999999999, "actor identity silently reinterpreted as MMSI")
        require(-180_000_000_000 <= longitude <= 180_000_000_000 and
                -70_000_000_000 <= latitude <= 70_000_000_000, "impossible/unsupported geographic position")
        bits = decode_sentence(line)
        check_field(bits, 0, 6, 1 if category == 0 else 18, "explicit family")
        check_field(bits, 6, 2, 0, "repeat")
        check_field(bits, 8, 30, mmsi, "explicit synthetic MMSI")
        if category == 0:
            check_field(bits, 38, 4, 15, "navigation unavailable")
            check_field(bits, 42, 8, -128, "ROT unavailable", True)
            check_field(bits, 50, 10, 1023, "SOG unavailable")
            check_field(bits, 60, 1, 0, "no inferred GNSS accuracy")
            check_field(bits, 61, 28, wire_coordinate(longitude), "longitude wire integer", True)
            check_field(bits, 89, 27, wire_coordinate(latitude), "latitude wire integer", True)
            check_field(bits, 116, 12, 3600, "COG unavailable")
            check_field(bits, 128, 9, 511, "true heading unavailable")
            check_field(bits, 137, 6, 60, "UTC unavailable, distinct from simulated seconds")
            check_field(bits, 143, 25, 0, "manoeuvre/spare/power/RAIM/explicit raw test state")
        else:
            check_field(bits, 38, 8, 0, "spare")
            check_field(bits, 46, 10, 1023, "SOG unavailable")
            check_field(bits, 56, 1, 0, "no inferred GNSS accuracy")
            check_field(bits, 57, 28, wire_coordinate(longitude), "longitude wire integer", True)
            check_field(bits, 85, 27, wire_coordinate(latitude), "latitude wire integer", True)
            check_field(bits, 112, 12, 3600, "COG unavailable")
            check_field(bits, 124, 9, 511, "heading unavailable")
            check_field(bits, 133, 6, 60, "UTC unavailable")
            check_field(bits, 139, 29, 0, "power/spare/CS/capabilities/RAIM/explicit raw test state")
        lengths.append(len(line))
    return lengths


def check_demo(result, expected_count=None):
    require(result.returncode == 0, f"native CUDA demo failed: {result.stderr.decode(errors='replace')}")
    values, events, metrics = metadata(result.stderr)
    pairs = [(e[0], e[2]) for e in events]
    possible = [(tick, actor) for tick in range(4) for actor in (701, 902)]
    require(pairs == sorted(set(pairs)) and all(pair in possible for pair in pairs) and
            (0, 701) in pairs and (0, 902) in pairs, "demo canonical accepted order/placement")
    if expected_count is not None:
        require(len(events) == expected_count and pairs == possible, "qualified demo accepted counts")
    require(all(event[1] == event[0] for event in events), "demo simulation time must be tick*1sec")
    require(all(event[6] == (100000001 if event[2] == 701 else 100000002) for event in events),
            "synthetic catalog differs from documented sorted-rank policy")
    for key in ("seed_mode", "seed", "profile", "rng", "rng_domain_version", "projection",
                "coordinate_units", "build_toolchain", "qualification_reference", "gpu",
                "compute_capability", "runtime_version", "driver_version", "profile_fnv1a64",
                "overlay_fnv1a64", "scenario_fnv1a64", "result_fnv1a64"):
        require(key in values, f"missing replay/qualification metadata {key}")
    require(values["rng"] == "cuRAND_Philox4_32_10" and values["rng_domain_version"] == "1" and
            values["projection"] == "wgs84_tangent_linear_v1" and
            values["coordinate_units"] == "nanodegrees", "unversioned simulation metadata")
    require(len(metrics) == 1 and metrics[0]["candidates"] == "8" and
            metrics[0]["scheduled"] == "8" and int(metrics[0]["accepted"]) == len(events) and
            int(metrics[0]["rejected"]) == 8 - len(events), "demo device diagnostics cardinality")
    require(int(metrics[0]["d2h_record_bytes"]) == len(events) * 64,
            "demo downloaded unaccepted candidates or a nonqualified record layout")
    require(int(metrics[0]["device_bytes"]) > 0 and float(metrics[0]["placement_ms"]) > 0,
            "demo does not report an actual allocated CUDA placement stage")
    lengths = check_wire(result.stdout, events)
    return values, events, lengths


def csv_scene(result, expected_count, expected_b2=None):
    require(result.returncode == 0, f"native geo scene failed: {result.stderr.decode(errors='replace')}")
    values, _, metrics = metadata(result.stderr)
    reader = csv.reader(io.StringIO(result.stdout.decode("utf-8")))
    require(next(reader) == ["absolute_tick", "simulation_seconds", "actor_id", "category", "x_mm", "y_mm",
                             "longitude_ndeg", "latitude_ndeg", "random_u32"], "geographic CSV schema")
    rows = [tuple(map(int, row)) for row in reader]
    require(len(rows) == expected_count, "scene accepted count")
    require([(row[0], row[2]) for row in rows] == sorted((row[0], row[2]) for row in rows), "scene canonical ordering")
    require(fnv(b"".join(struct.pack("<QQIIqqqqI", *row) for row in rows)) == values["result_fnv1a64"],
            "independent raw60-byte GeoEvent FNV differs from native result digest")
    if expected_b2 is not None:
        data = b"".join(struct.pack("<QIIqqI", row[0], row[2], row[3], row[4], row[5], row[8]) for row in rows)
        require(fnv(data) == expected_b2, "historical B2 trajectory/Philox digest changed")
    require(len(metrics) == 1 and int(metrics[0]["accepted"]) == expected_count and
            int(metrics[0]["d2h_record_bytes"]) == expected_count * 64, "scene compact transfer diagnostics")
    return rows, values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--domain", type=Path, required=True)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--skip-gate-self-tests", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    reference = shipped_profile(args.domain, args.source_dir)
    args.work_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="geo-wire-", dir=args.work_dir))

    def run(*options, hidden=False):
        environment = dict(os.environ)
        if hidden:
            environment["CUDA_VISIBLE_DEVICES"] = "-1"
        return subprocess.run([str(args.exe.resolve()), *map(str, options)], env=environment,
                              capture_output=True, check=False, timeout=60)

    base = ("--demo", "--domain", args.domain, "--seed", "42")
    first = run(*base)
    values, events, lengths = check_demo(first, expected_count=8 if reference else None)
    if reference:
        require(values["profile_fnv1a64"] == QUALIFIED_PROFILE_HASH,
                "native profile FNV differs from independently pinned repository profile")
        qualified, reason = qualified_platform(args.exe, values)
    else:
        qualified, reason = False, "non-reference profile"
    if qualified:
        require(values["result_fnv1a64"] == QUALIFIED_DEMO_DIGEST, "qualified sm61/CUDA12.9.86 default-domain demo golden digest")
        print(f"PASS qualified golden {QUALIFIED_DEMO_DIGEST}: {reason}")
    else:
        print(f"qualified golden SKIPPED: {reason}")
    for block in (64, 128, 256):
        repeated = run(*base, "--block", block)
        repeated_values, repeated_events, _ = check_demo(repeated, expected_count=8 if reference else None)
        require(repeated.stdout == first.stdout and repeated_events == events and
                repeated_values["result_fnv1a64"] == values["result_fnv1a64"],
                "fixed-seed NMEA/event bytes vary by launch shape/replay")
    different = run("--demo", "--domain", args.domain, "--seed", "43")
    _, different_events, _ = check_demo(different, expected_count=None)
    if reference:
        require([(e[4], e[5]) for e in different_events] != [(e[4], e[5]) for e in events],
                "different seed did not change actual geographic placement")
    fresh = run("--demo", "--domain", args.domain, "--fresh-seed")
    fresh_values, fresh_events, _ = check_demo(fresh, expected_count=None)
    require(fresh_values["seed_mode"] == "fresh", "fresh seed mode not recorded")
    replay = run("--demo", "--domain", args.domain, "--seed", fresh_values["seed"])
    replay_values, replay_events, _ = check_demo(replay, expected_count=None)
    require(replay_values["seed_mode"] == "fixed" and fresh.stdout == replay.stdout and
            fresh_events == replay_events and fresh_values["result_fnv1a64"] == replay_values["result_fnv1a64"],
            "OS-selected fresh seed cannot replay exact CUDA/AIS bytes")
    unavailable = run(*base, hidden=True)
    require(unavailable.returncode != 0 and not unavailable.stdout and b"error" in unavailable.stderr.lower(),
            "GPU unavailable silently passed or used a CPU fallback")
    help_result = run("--help")
    require(help_result.returncode == 0 and b"--fresh-seed" in help_result.stdout, "native geo help")
    for options in [("--demo",), ("--unknown",), ("--demo", "--domain"),
                    ("--demo", "--domain", args.domain, "--seed", "-1"),
                    ("--demo", "--domain", args.domain, "--seed", "18446744073709551616"),
                    ("--demo", "--domain", args.domain, "--seed", "42", "--fresh-seed"),
                    ("--demo", "--domain", args.domain, "--block", "32")]:
        bad = run(*options)
        require(bad.returncode != 0 and not bad.stdout, f"invalid native CLI partially succeeded: {options}")

    if reference:
        original_scene = args.source_dir / "scenes" / "demo.yaml"
        original_overlay = args.source_dir / "geo" / "profiles" / "scene_demo_overlay.yaml"
        old = run("--scene", original_scene, "--overlay", original_overlay, "--domain", args.domain)
        csv_scene(old, 23, "7aad5e9c764012e3")

    # Replay actual CUDA-generated integer anchors through a separate explicit
    # overlay. CSV exposes all nine record fields, so the full demo digest can
    # be recomputed independently without borrowing a cuRAND implementation.
    placement_replay_scene = work / "placed-demo-scene.yaml"
    placement_replay_overlay = work / "placed-demo-anchors.yaml"
    placement_replay_scene.write_text("""schema_version: 1
name: explicit-replay-of-cuda-placement
simulation: {seed: 42, start_tick: 0, tick_count: 4, tick_seconds: 1}
actors:
  - {actor_id: 902, category: ClassB, x_mm: 12345, y_mm: -67890, vx_mm_per_s: -50, vy_mm_per_s: 25, report_interval_ticks: 1, phase_ticks: 0}
  - {actor_id: 701, category: ClassA, x_mm: -999, y_mm: 888, vx_mm_per_s: 250, vy_mm_per_s: -100, report_interval_ticks: 1, phase_ticks: 0}
""", encoding="utf-8", newline="\n")
    anchor_text = """schema_version: 1
coordinate_units: nanodegrees
coordinate_order: longitude_latitude
datum: WGS84
projection: wgs84_tangent_linear_v1
anchor_reference: actor_tick_zero
anchors:
"""
    for event in events:
        if event[0] == 0:
            anchor_text += (f"  - {{actor_id: {event[2]}, longitude_ndeg: {event[4]}, "
                            f"latitude_ndeg: {event[5]}}}\n")
    placement_replay_overlay.write_text(anchor_text, encoding="utf-8", newline="\n")
    placement_replay = run("--scene", placement_replay_scene, "--overlay", placement_replay_overlay,
                           "--domain", args.domain, "--seed", "42")
    placed_rows, placed_values = csv_scene(placement_replay, len(events))
    require([(row[0], row[1], row[2], row[3], row[6], row[7]) for row in placed_rows] ==
            [(event[0], event[1], event[2], event[3], event[4], event[5]) for event in events] and
            placed_values["result_fnv1a64"] == values["result_fnv1a64"],
            "CUDA placement demo differs from exact integer-anchor replay/full independent FNV")

    if reference:
        reference_scene_checks(run, args.domain, work)
    else:
        print("SKIPPED repository-specific scene-anchor fixtures: non-reference profile")
    if not args.skip_gate_self_tests:
        profile_gate_self_tests(args, work)
    print(f"PASS actual CUDA -> independent Type1/18 decoder:{len(events)} canonical records, NMEA byte lengths={lengths}")
    print(f"PASS fixed/fresh replay, different-seed wire validation and launch shapes; result digest={values['result_fnv1a64']}")
    print("PASS GPU-unavailable rejection, seven invalid CLI cases, independent raw60-byte demo/scene digests")
    if reference:
        print("PASS historical B2 digest7aad5e9c764012e3 /23events; overlay CSV -> controlled offline NMEA /6events; existing-file refusal")
    print(f"Small inspectable artifacts: {work}")


def reference_scene_checks(run, domain, work):
    """These explicit coordinates/counts belong only to the shipped domain."""
    scene = work / "two-actors.yaml"
    overlay = work / "two-anchors.yaml"
    nmea = work / "accepted.nmea"
    scene.write_text("""schema_version: 1
name: geo-wire-test
simulation: {seed: 42, start_tick: 0, tick_count: 4, tick_seconds: 2}
actors:
  - {actor_id: 10, category: ClassA, x_mm: 12345, y_mm: 67890, vx_mm_per_s: -1000, vy_mm_per_s: 500, report_interval_ticks: 2, phase_ticks: 0}
  - {actor_id: 20, category: ClassB, x_mm: -3000, y_mm: 4000, vx_mm_per_s: 500, vy_mm_per_s: -1000, report_interval_ticks: 1, phase_ticks: 0}
""", encoding="utf-8", newline="\n")
    overlay.write_text("""schema_version: 1
coordinate_units: nanodegrees
coordinate_order: longitude_latitude
datum: WGS84
projection: wgs84_tangent_linear_v1
anchor_reference: actor_tick_zero
anchors:
  - {actor_id: 20, longitude_ndeg: 32001000000, latitude_ndeg: 43001000000}
  - {actor_id: 10, longitude_ndeg: 32000000000, latitude_ndeg: 43000000000}
""", encoding="utf-8", newline="\n")
    scene_options = ("--scene", scene, "--overlay", overlay, "--domain", domain)
    csv_result = run(*scene_options)
    rows, _ = csv_scene(csv_result, 6)
    require(rows[0][4:8] == (12345, 67890, 32000000000, 43000000000), "tick-zero anchor/local-offset association")
    file_result = run(*scene_options, "--nmea", nmea)
    require(file_result.returncode == 0 and not file_result.stdout, "controlled NMEA file mode failed")
    _, file_events, _ = metadata(file_result.stderr)
    require([(e[0], e[1], e[2], e[3], e[4], e[5]) for e in file_events] ==
            [(row[0], row[1], row[2], row[3], row[6], row[7]) for row in rows],
            "CSV and offline AIS describe different accepted events")
    check_wire(nmea.read_bytes(), file_events)
    preserved = nmea.read_bytes()
    refused = run(*scene_options, "--nmea", nmea)
    require(refused.returncode != 0 and not refused.stdout and nmea.read_bytes() == preserved,
            "existing controlled NMEA output was overwritten or partially succeeded")



def profile_gate_self_tests(args, work):
    """Permanent gate coverage; children suppress only further child spawning."""
    altered_source = work / "altered-shipped-source"
    altered_domain = altered_source / "geo" / "profiles" / "black_sea_demo.yaml"
    altered_domain.parent.mkdir(parents=True)
    original = (args.source_dir / "geo" / "profiles" / "black_sea_demo.yaml").read_bytes()
    require(b"longitude_ndeg: 28530000000" in original, "shipped tamper fixture coordinate was not found")
    altered_domain.write_bytes(original.replace(b"longitude_ndeg: 28530000000",
                                                 b"longitude_ndeg: 28530000001", 1))

    def child(domain, source, output):
        return subprocess.run([sys.executable, str(Path(__file__).resolve()),
                               "--exe", str(args.exe.resolve()), "--domain", str(domain.resolve()),
                               "--source-dir", str(source.resolve()), "--work-dir", str(output.resolve()),
                               "--skip-gate-self-tests"], capture_output=True, check=False, timeout=120)

    tampered = child(altered_domain, altered_source, work / "tampered-work")
    require(tampered.returncode != 0 and b"repository-shipped profile pin changed" in tampered.stderr,
            "coordinate-tampered shipped profile did not fail the independent profile pin")
    alternate = work / "alternate-small-rectangle.yaml"
    alternate.write_text("""schema_version: 1
profile_id: independent_nonreference_small_rectangle_v1
provenance: Synthetic tiny rectangle for generic-profile exits and golden-gate testing.
coordinate_units: nanodegrees
coordinate_order: longitude_latitude
datum: WGS84
ring_closure: explicit
boundary: inclusive
ring:
  - {label: southwest, longitude_ndeg: 28000000000, latitude_ndeg: 42000000000}
  - {label: southeast, longitude_ndeg: 28000001000, latitude_ndeg: 42000000000}
  - {label: northeast, longitude_ndeg: 28000001000, latitude_ndeg: 42000001000}
  - {label: northwest, longitude_ndeg: 28000000000, latitude_ndeg: 42000001000}
  - {label: southwest, longitude_ndeg: 28000000000, latitude_ndeg: 42000000000}
""", encoding="utf-8", newline="\n")
    legitimate = child(alternate, args.source_dir, work / "alternate-work")
    require(legitimate.returncode == 0,
            f"legitimate alternate profile failed generic checks: {legitimate.stderr.decode(errors='replace')}")
    require(b"qualified golden SKIPPED: non-reference profile" in legitimate.stdout and
            b"PASS qualified golden" not in legitimate.stdout and
            b"decoder:8 canonical records" not in legitimate.stdout,
            "alternate profile was mislabeled qualified or retained fixed eight-event assumptions")
    print("PASS permanent profile-gate regressions: shipped-coordinate tamper rejected; alternate profile generic checks passed with explicit golden SKIPPED")


if __name__ == "__main__":
    main()
