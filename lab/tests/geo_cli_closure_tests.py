"""Windows offline geo CLI regressions. No network or optional Python packages."""

import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
from geo_wire_oracle import check_wire, csv_scene, metadata, require


def fixtures(work, count, ticks):
    scene, overlay = work / f"actors-{count}-{ticks}.yaml", work / f"anchors-{count}.yaml"
    scene_text = ("schema_version: 1\nname: closure-cli\n"
                  f"simulation: {{seed: 42, start_tick: 0, tick_count: {ticks}, tick_seconds: 1}}\nactors:\n")
    overlay_text = ("schema_version: 1\ncoordinate_units: nanodegrees\n"
                    "coordinate_order: longitude_latitude\ndatum: WGS84\n"
                    "projection: wgs84_tangent_linear_v1\nanchor_reference: actor_tick_zero\nanchors:\n")
    # Reverse input order and distinct anchors independently exercise ID/rank association.
    for index in reversed(range(count)):
        actor_id = 100000 + 7 * index
        category = "ClassB" if index % 2 else "ClassA"
        scene_text += (f"  - {{actor_id: {actor_id}, category: {category}, x_mm: {index}, y_mm: {-index}, "
                       "vx_mm_per_s: 0, vy_mm_per_s: 0, report_interval_ticks: 1, phase_ticks: 0}\n")
        overlay_text += (f"  - {{actor_id: {actor_id}, longitude_ndeg: {32000000000 + index * 1000}, "
                         f"latitude_ndeg: {43000000000 + index * 1000}}}\n")
    scene.write_text(scene_text, encoding="utf-8", newline="\n")
    overlay.write_text(overlay_text, encoding="utf-8", newline="\n")
    return scene, overlay


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    args = parser.parse_args()
    args.work_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="cli-closure-", dir=args.work_dir))
    exe = args.exe.resolve()
    domain = args.source_dir / "geo/profiles/black_sea_demo.yaml"

    def run(*options, timeout=90):
        return subprocess.run([str(exe), *map(str, options)], capture_output=True, timeout=timeout)

    reference = run("--demo", "--domain", domain, "--seed", "42")
    require(reference.returncode == 0 and len(reference.stdout) == 392, "reference demo failed")
    ref_values, ref_events, _ = metadata(reference.stderr)
    check_wire(reference.stdout, ref_events)

    unicode_dir = work / "\u043f\u0440\u043e\u0444\u0438\u043b\u0438"
    unicode_dir.mkdir()
    unicode_domain = unicode_dir / "\u043c\u043e\u0440\u0435.yaml"
    shutil.copyfile(domain, unicode_domain)
    unicode_output = unicode_dir / "\u0441\u044a\u043e\u0431\u0449\u0435\u043d\u0438\u044f.nmea"
    unicode_scene = run("--demo", "--domain", unicode_domain, "--seed", "42", "--nmea", unicode_output)
    require(unicode_scene.returncode == 0 and not unicode_scene.stdout and
            unicode_output.read_bytes() == reference.stdout, "Unicode input/output changed NMEA bytes")
    values, events, _ = metadata(unicode_scene.stderr)
    require(values["result_fnv1a64"] == ref_values["result_fnv1a64"] and events == ref_events,
            "Unicode paths changed ordered event metadata/digest")
    missing = unicode_dir / "\u043d\u044f\u043c\u0430.yaml"
    failed = run("--demo", "--domain", missing)
    require(failed.returncode != 0 and not failed.stdout and str(missing).encode("utf-8") in failed.stderr,
            "Unicode error diagnostic did not preserve UTF-8 path")
    before = hashlib.sha256(unicode_output.read_bytes()).digest()
    refused = run("--demo", "--domain", unicode_domain, "--nmea", unicode_output)
    require(refused.returncode != 0 and not refused.stdout and
            hashlib.sha256(unicode_output.read_bytes()).digest() == before, "existing Unicode NMEA overwritten")
    require(not list(unicode_dir.glob("*.aismixer-tmp-*")), "successful/refused export left temporary files")
    print("PASS Cyrillic domain directory/output name, exact392-byte NMEA, UTF-8 diagnostic and no-clobber")

    receivers = args.source_dir / "geo/profiles/receivers_demo.yaml"
    catalog = run("--demo", "--domain", domain, "--receivers", receivers, "--seed", "42")
    require(catalog.returncode == 0 and catalog.stdout == reference.stdout and
            b"passive_receiver_count=3 (catalog_only_no_RF)" in catalog.stderr,
            "passive receiver catalog changed payloads or role diagnostic")
    benchmark = run("--benchmark", "--domain", domain, "--seed", "42")
    require(benchmark.returncode == 0 and not benchmark.stdout, "benchmark failed")
    cases = [dict(part.split("=", 1) for part in line.split())
             for line in benchmark.stderr.decode("utf-8").splitlines() if line.startswith("case=")]
    require([c["candidates"] for c in cases] == ["1000", "100000", "1000000"], "benchmark sizes changed")
    for case in cases:
        require(int(case["candidates"]) == int(case["scheduled"]) + int(case["unscheduled"]) and
                int(case["scheduled"]) == int(case["accepted"]) + int(case["rejected"]) and
                int(case["d2h_record_bytes"]) == 64 * int(case["accepted"]), "benchmark conservation/compact bytes")
        for key in ("setup_wall_ms", "steady_wall_ms", "h2d_ms", "placement_ms", "motion_ms",
                    "predicate_ms", "compaction_ms", "d2h_ms"):
            require(float(case[key]) >= 0, f"invalid benchmark measurement {key}")
    print("PASS --receivers catalog and all3 actual CUDA benchmark paths without timing thresholds")

    many_scene, many_overlay = fixtures(work, 2048, 3)
    many_args = ("--scene", many_scene, "--overlay", many_overlay, "--domain", domain)
    csv_result = run(*many_args)
    rows, csv_values = csv_scene(csv_result, 6144)
    require(csv_result.stdout.endswith(b"\r\n") and b"\r\r\n" not in csv_result.stdout,
            "successful Windows CSV framing changed")
    nmea_output = work / "many.nmea"
    nmea_result = run(*many_args, "--nmea", nmea_output)
    require(nmea_result.returncode == 0 and not nmea_result.stdout, "many-actor NMEA publication failed")
    many_values, many_events, _ = metadata(nmea_result.stderr)
    require(many_values["result_fnv1a64"] == csv_values["result_fnv1a64"], "CSV/NMEA simulation digest differs")
    expected = []
    for tick in range(3):
        for index in range(2048):
            expected.append((tick, tick, 100000 + 7 * index, index % 2,
                             32000000000 + index * 1000, 43000000000 + index * 1000,
                             100000001 + index))
    require(many_events == expected, "many-actor identity/rank/location metadata association changed")
    check_wire(nmea_output.read_bytes(), many_events)
    require([(r[0], r[1], r[2], r[3], r[6], r[7]) for r in rows] == [e[:6] for e in expected],
            "many-actor CSV field association changed")
    print("PASS2048 reversed actors /6144 canonical identity metadata rows and independent NMEA checksums/fields")

    pipe_scene, pipe_overlay = fixtures(work, 512, 64)
    with (work / "closed-pipe.stderr").open("wb") as error_stream:
        process = subprocess.Popen([str(exe), "--scene", str(pipe_scene), "--overlay", str(pipe_overlay),
                                    "--domain", str(domain)], stdout=subprocess.PIPE, stderr=error_stream)
        require(process.stdout is not None, "pipe creation failed")
        try:
            prefix = process.stdout.read(2048)
            process.stdout.close()
            require(len(prefix) == 2048, "CSV fixture too short to test early-close")
            exit_code = process.wait(timeout=60)
        except BaseException:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            raise
    pipe_error = (work / "closed-pipe.stderr").read_bytes().lower()
    require(exit_code != 0 and b"csv" in pipe_error and (b"write" in pipe_error or b"flush" in pipe_error),
            "early-closing CSV pipe reported success or omitted output-failure context")
    print("PASS CSV reader closes after2048bytes: checked nonzero completion, no giant fixture")

    for options in [("--demo", "--domain", domain, "--nmea", ""),
                    ("--demo", "--domain", "--seed", "42"),
                    ("--demo", "--domain", domain, "--nmea", "--fresh-seed")]:
        bad = run(*options)
        require(bad.returncode != 0 and not bad.stdout, "empty/option-like path value silently accepted")
    print(f"PASS adjacent CLI value errors; controlled artifacts: {work}")


if __name__ == "__main__":
    main()
