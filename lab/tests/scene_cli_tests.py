"""Subprocess acceptance tests for the native runner; this is not a runner wrapper.

Uses only Python's standard library. Each invocation retains its own artifacts
under the supplied ignored build directory. The GPU modes require real CUDA.
"""

import argparse
import csv
import io
import os
from pathlib import Path
import struct
import subprocess
import tempfile


HEADER = ["absolute_tick", "actor_id", "category", "x_mm", "y_mm", "random_u32"]
DEMO_DIGEST = "7aad5e9c764012e3"
GPU_ERRORS = ("cudaget", "cudaset", "cudamalloc", "no cuda device", "cuda failure")


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def fixture(*, seed="42", start="0", count="8", seconds="2", actors=None):
    if actors is None:
        actors = [{}]
    lines = ["schema_version: 1", "name: test-scene", "simulation:",
             f"  seed: {seed}", f"  start_tick: {start}",
             f"  tick_count: {count}", f"  tick_seconds: {seconds}"]
    if not actors:
        return "\n".join(lines + ["actors: []", ""])
    lines.append("actors:")
    for index, overrides in enumerate(actors):
        actor = dict(actor_id=str(index + 1), category="ClassA", x_mm="1000", y_mm="-2000",
                     vx_mm_per_s="250", vy_mm_per_s="-100", report_interval_ticks="2",
                     phase_ticks="0")
        actor.update(overrides)
        for field_index, (key, value) in enumerate(actor.items()):
            lines.append(f"  {'- ' if field_index == 0 else '  '}{key}: {value}")
    return "\n".join(lines) + "\n"


def fnv_digest(rows):
    value = 14695981039346656037
    for row in rows:
        # Independent explicit 36-byte LE encoding; no C++ object layout.
        for byte in struct.pack("<QIIqqI", *row):
            value = ((value ^ byte) * 1099511628211) & ((1 << 64) - 1)
    return f"{value:016x}"


def csv_rows(result):
    parsed = list(csv.reader(io.StringIO(result.stdout)))
    require(parsed and parsed[0] == HEADER, "stdout is not the canonical CSV header")
    require(all(len(row) == 6 for row in parsed[1:]), "stdout contains prompts or diagnostics")
    rows = [tuple(int(value) for value in row) for row in parsed[1:]]
    require(all((a[0], a[1]) < (b[0], b[1]) for a, b in zip(rows, rows[1:])),
            "CSV records are not strictly canonical")
    require(all(0 <= row[2] <= 5 and 0 <= row[5] <= 4294967295 for row in rows),
            "CSV contains invalid category or PRNG words")
    require(fnv_digest(rows) in result.stderr.lower(), "stderr digest does not describe stdout CSV")
    return rows


class Harness:
    def __init__(self, args):
        self.runner = args.runner.resolve()
        self.source = args.source_dir.resolve()
        require(self.runner.is_file(), f"native runner is not built: {self.runner}")
        require((self.source / "scenes/demo.yaml").is_file(), "demo.yaml is missing")
        args.work_dir.mkdir(parents=True, exist_ok=True)
        self.work = Path(tempfile.mkdtemp(prefix=f"{args.mode}-", dir=args.work_dir.resolve()))
        self.checked = 0
        self.originals = {path: path.read_bytes() for path in (self.source / "scenes").glob("*.y*ml")}

    def invoke(self, arguments, *, stdin="", no_gpu=False):
        environment = os.environ.copy()
        if no_gpu:
            environment["CUDA_VISIBLE_DEVICES"] = "-1"
        result = subprocess.run([str(self.runner), *map(str, arguments)], input=stdin.encode("utf-8"),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=False, timeout=30,
                                cwd=self.source.parent, env=environment, check=False)
        self.checked += 1
        # Decode explicitly: text=True would normalize CRLF and conceal a
        # byte-level mismatch between canonical stdout and the exported CSV.
        return subprocess.CompletedProcess(result.args, result.returncode,
                                           stdout=result.stdout.decode("utf-8"),
                                           stderr=result.stderr.decode("utf-8"))

    def good(self, arguments, **kwargs):
        result = self.invoke(arguments, **kwargs)
        require(result.returncode == 0,
                f"expected success for {arguments}: {result.returncode}: {result.stderr}")
        return result

    def bad(self, arguments, field, *, code=None, no_gpu=True):
        result = self.invoke(arguments, no_gpu=no_gpu)
        require(result.returncode != 0, f"invalid input accepted: {arguments}")
        if code is not None:
            require(result.returncode == code, f"wrong error class for {arguments}: {result.returncode}")
        text = (result.stdout + result.stderr).lower()
        require(field.lower() in text, f"diagnostic omitted {field}: {text}")
        require(not any(token in text for token in GPU_ERRORS), "host error attempted CUDA")
        require("export complete" not in text, "failed operation claimed export success")
        return result

    def scene_file(self, name, content):
        path = self.work / name
        path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
        return path

    def unchanged(self):
        for path, original in self.originals.items():
            require(path.read_bytes() == original, f"runner changed YAML on disk: {path}")


def host_tests(h):
    demo = h.source / "scenes/demo.yaml"
    example = h.source / "scenes/example.yaml"
    h.good(["--help"], no_gpu=True)
    no_args = h.invoke([], no_gpu=True)
    require(no_args.returncode != 0, "missing noninteractive input should fail explicitly")
    require("interactive" in (no_args.stdout + no_args.stderr).lower(),
            "noninteractive no-args did not explain interactive usage")
    for path in (demo, example):
        h.good(["--scene", path, "--validate"], no_gpu=True)
    overridden = h.good(["--scene", demo, "--validate", "--seed", "18446744073709551615",
                         "--start-tick", "1000000000", "--tick-count", "1", "--tick-seconds", "60"],
                        no_gpu=True)
    require("18446744073709551615" in overridden.stdout + overridden.stderr,
            "validate did not show effective full-width seed override")
    unicode_input = h.scene_file("сцена-валидна.yaml", demo.read_bytes())
    h.good(["--scene", unicode_input, "--validate"], no_gpu=True)
    h.good(["--scene", h.scene_file("empty.yaml", fixture(actors=[])), "--validate"], no_gpu=True)
    h.good(["--scene", h.scene_file("zero.yaml", fixture(count="0")), "--validate"], no_gpu=True)

    for value in ("-1", "+1", "1.5", "NaN", "1e2", "0x10", "", " 1", "1 ",
                  "18446744073709551616"):
        h.bad(["--scene", demo, "--validate", "--seed", value], "seed", code=2)
    for arguments, field in [(["--scene"], "scene"), (["--wat"], "wat"),
                              (["--scene", demo, "--format", "xml"], "format"),
                              (["--scene", demo, "--seed", "1", "--seed", "2"], "seed"),
                              (["--scene", demo, "--validate", "--tick-seconds", "4294967296"], "tick-seconds"),
                              (["--scene", demo, "--validate", "--tick-count", "3601"], "tick-count"),
                              (["--scene", demo, "--validate", "--start-tick", "1000000000",
                                "--tick-count", "2"], "tick")]:
        h.bad(arguments, field, code=2)

    invalid_yaml = [
        ("syntax", "schema_version: [\n", "yaml"),
        ("top-sequence", "[]\n", "mapping"),
        ("version", fixture().replace("schema_version: 1", "schema_version: 2"), "schema_version"),
        ("missing-seed", fixture().replace("  seed: 42\n", ""), "seed"),
        ("missing-category", fixture().replace("    category: ClassA\n", ""), "category"),
        ("dup-top", fixture() + "name: again\n", "duplicate"),
        ("dup-sim", fixture().replace("  seed: 42\n", "  seed: 42\n  seed: 43\n"), "duplicate"),
        ("dup-actor", fixture().replace("    category: ClassA\n", "    actor_id: 2\n    category: ClassA\n"), "duplicate"),
        ("dup-id", fixture(actors=[{"actor_id": "10"}, {"actor_id": "10"}]), "actor_id"),
        ("unknown-top", fixture() + "hidden: true\n", "hidden"),
        ("unknown-sim", fixture().replace("  seed: 42\n", "  seed: 42\n  hidden: 1\n"), "hidden"),
        ("unknown-actor", fixture(actors=[{"hidden": "1"}]), "hidden"),
        ("category", fixture(actors=[{"category": "classa"}]), "category"),
        ("tag", fixture().replace("name: test-scene", "name: !custom test-scene"), "tag"),
        ("name", fixture().replace("name: test-scene", "name: \"   \""), "name"),
        ("seed-width", fixture(seed="18446744073709551616"), "seed"),
        ("negative-seed", fixture(seed="-1"), "seed"),
        ("fraction", fixture(count="1.5"), "tick_count"),
        ("hex", fixture(seed="0x10"), "seed"),
        ("nan", fixture(seed=".nan"), "seed"),
        ("quoted-space", fixture(seed='" 42"'), "seed"),
        ("id-width", fixture(actors=[{"actor_id": "4294967296"}]), "actor_id"),
        ("position-width", fixture(actors=[{"x_mm": "-9223372036854775809"}]), "x_mm"),
        ("velocity-width", fixture(actors=[{"vx_mm_per_s": "2147483648"}]), "vx_mm_per_s"),
        ("position-bound", fixture(actors=[{"y_mm": "1000000000001"}]), "y_mm"),
        ("velocity-bound", fixture(actors=[{"vy_mm_per_s": "-100001"}]), "vy_mm_per_s"),
        ("interval", fixture(actors=[{"report_interval_ticks": "0"}]), "report_interval_ticks"),
        ("phase", fixture(actors=[{"phase_ticks": "2"}]), "phase_ticks"),
        ("stationary", fixture(actors=[{"category": "BaseStation"}]), "vx_mm_per_s"),
        ("aton", fixture(actors=[{"category": "AtoN", "vx_mm_per_s": "0"}]), "vy_mm_per_s"),
        ("seconds", fixture(seconds="0"), "tick_seconds"),
        ("range", fixture(start="1000000000", count="2"), "tick"),
        ("ticks", fixture(count="18446744073709551615"), "tick_count"),
        ("product", fixture(count="1001", actors=[{} for _ in range(1000)]), "tick_count"),
    ]
    for name, content, field in invalid_yaml:
        path = h.scene_file(f"invalid-{name}.yaml", content)
        result = h.bad(["--scene", path, "--validate"], field, code=2)
        require(path.name in result.stderr, "YAML diagnostic omitted its input filename")
    h.bad(["--scene", h.work / "missing.yaml", "--validate"], "missing.yaml", code=3)
    h.bad(["--scene", h.scene_file("too-large.yaml", b"#" * (1024 * 1024 + 1)), "--validate"],
          "size", code=2)
    h.bad(["--scene", h.scene_file("invalid-utf8.yaml", b"name: \xff\n"), "--validate"], "UTF-8", code=2)
    print("PASS no-GPU help/validation, strict CLI/YAML failures, overrides and Unicode input")


def gpu_tests(h):
    demo = h.source / "scenes/demo.yaml"
    base = ["--scene", demo, "--format", "csv"]
    first = h.good(base)
    rows = csv_rows(first)
    require(len(rows) == 23 and fnv_digest(rows) == DEMO_DIGEST, "reference YAML demo changed")
    require(rows[:3] == [(0, 10, 0, 1000, -2000, 1037580057),
                         (0, 40, 3, -9000, 10000, 2619888965),
                         (0, 60, 5, -100, 200, 1764872081)], "reference event oracle changed")
    repeated = csv_rows(h.good(base))
    require(repeated == rows, "repeated run changed records")
    changed = csv_rows(h.good(base + ["--seed", "43"]))
    require([row[:-1] for row in changed] == [row[:-1] for row in rows],
            "seed changed deterministic fields")
    require(any(a[-1] != b[-1] for a, b in zip(rows, changed)), "seed did not change PRNG")
    for ranges in (((0, 3), (3, 5)), ((0, 2), (2, 3), (5, 3))):
        joined = []
        for start, count in ranges:
            joined.extend(csv_rows(h.good(base + ["--start-tick", start, "--tick-count", count])))
        require(sorted(joined, key=lambda row: row[:2]) == rows, "split range changed records")
    summary = h.good(["--scene", demo])
    require(DEMO_DIGEST in summary.stdout.lower(), "default summary lacks digest")
    require("preview_count=10" in summary.stdout, "default preview does not contain ten events")
    require(len(summary.stdout.splitlines()) < 50, "default preview is not bounded")
    many = h.scene_file("summary-bounded.yaml", fixture(count="3600", actors=[{"report_interval_ticks": "1"}]))
    bounded = h.good(["--scene", many])
    require(len(bounded.stdout.splitlines()) < 50, "3600-event summary dumped the full stream")
    output = h.work / "new-parent" / "events.csv"
    exported = h.good(base + ["--output", output])
    require(csv_rows(exported) == rows, "output option changed stdout CSV")
    require(output.read_bytes() == first.stdout.encode("utf-8"), "export differs from stdout CSV bytes")
    require("export complete" in exported.stderr.lower(), "export lacks completion after write/close")
    original = output.read_bytes()
    h.bad(base + ["--output", output], "exist", code=3, no_gpu=False)
    require(output.read_bytes() == original, "overwrite refusal changed the existing output")
    blocker = h.scene_file("parent-is-file", "preserve")
    h.bad(base + ["--output", blocker / "events.csv"], "parent", code=3, no_gpu=False)
    require(blocker.read_bytes() == b"preserve", "file error modified parent file")
    unicode_scene = h.scene_file("сцена-GPU.yaml", demo.read_bytes())
    unicode_output = h.work / "нови-данни" / "резултат.csv"
    unicode_run = h.good(["--scene", unicode_scene, "--format", "csv", "--output", unicode_output])
    require(csv_rows(unicode_run) == rows and unicode_output.read_bytes() == first.stdout.encode("utf-8"),
            "Unicode input/output changed CSV")
    unavailable = h.invoke(base, no_gpu=True)
    require(unavailable.returncode == 4, "unavailable GPU did not use the GPU error exit class")
    require(not unavailable.stdout and "export complete" not in unavailable.stderr.lower(),
            "unavailable GPU claimed CSV output success")
    print(f"PASS actual GPU YAML demo: events=23 digest={DEMO_DIGEST}; repeat/seed/split/summary/export")


def interactive_tests(h):
    demo = h.source / "scenes/demo.yaml"
    output = h.work / "interactive" / "edited.csv"
    script = "\n".join(["1", str(h.work / "missing.yaml"), "1", str(demo), "2", "3",
                        "bad-seed", "43", "", "", "", "4", "5", "6", str(output), "0", ""])
    result = h.good(["--interactive"], stdin=script)
    text = (result.stdout + result.stderr).lower()
    require("invalid seed" in text, "interactive invalid-number retry missing")
    require("43" in text and "digest" in text, "interactive edited run missing")
    require("export complete" in text and output.is_file(), "interactive export did not complete")
    direct = h.good(["--scene", demo, "--seed", "43", "--format", "csv"])
    require(output.read_bytes() == direct.stdout.encode("utf-8"), "interactive seed edit was not effective")
    failed_edit = h.good(["--interactive", "--scene", demo, "--seed", "43"],
                         stdin="3\nbad\nbad\nbad\n\n\n\n4\n0\n", no_gpu=True)
    failed_text = (failed_edit.stdout + failed_edit.stderr).lower()
    require("previous seed kept" in failed_text and "seed: 43" in failed_text,
            "failed edits changed a prior valid seed")
    h.good(["--interactive"], stdin="") # EOF without a command must exit cleanly.
    h.good(["--interactive", "--scene", demo], stdin="9\n\n0\n")
    print("PASS redirected interactive load/recovery/edit/validate/GPU/export/EOF/exit")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runner", required=True, type=Path)
    parser.add_argument("--source-dir", required=True, type=Path)
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--mode", required=True, choices=("host", "gpu", "interactive"))
    args = parser.parse_args()
    harness = Harness(args)
    try:
        {"host": host_tests, "gpu": gpu_tests, "interactive": interactive_tests}[args.mode](harness)
        harness.unchanged()
        print(f"Scene CLI {args.mode} tests: PASS; subprocesses={harness.checked}; artifacts={harness.work}")
    finally:
        harness.unchanged()


if __name__ == "__main__":
    main()
