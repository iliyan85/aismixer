#!/usr/bin/env bash
# Operator entry point. Only Docker commands use sudo.
set -euo pipefail
export GIT_OPTIONAL_LOCKS=0

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
src=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd -P)
cd -- "$src"
check_checkout() {
    [[ "$src" == /home/dev/src/aismixer-lab-linux-full && "$(git rev-parse --show-toplevel)" == "$src" ]] ||
        die "Expected the Phase 2 worktree at /home/dev/src/aismixer-lab-linux-full."
    [[ "$(git branch --show-current)" == task/lab-linux-full ]] || die "Expected task/lab-linux-full."
    git diff --check HEAD
}
source_identity() {
    printf 'branch=%s\nrevision=%s\n' "$(git branch --show-current)" "$(git rev-parse HEAD)"
    git diff --binary --no-ext-diff --no-textconv HEAD | sha256sum | awk '{print "tracked_diff_sha256=" $1}'
    # Include new, uncommitted Phase 2 files in the run identity.
    git ls-files --others --exclude-standard -z | xargs -0 -r sha256sum --zero -- |
        sha256sum | awk '{print "untracked_manifest_sha256=" $1}'
}

check_checkout
(( EUID != 0 )) || die "Run as a regular user with sudo access for Docker."
run_dir=$(mktemp -d -- /var/tmp/aismixer-phase2.XXXXXXXX)
build_dir=$run_dir/build
logs=$run_dir/logs
mkdir -- "$build_dir" "$logs"
printf 'Build directory: %s\nLog directory: %s\n' "$build_dir" "$logs"
trap 'rc=$?; printf "qualification\t%d\n" "$rc" >> "$logs/exit-codes.tsv"; printf "Qualification exit %d. Logs: %s\n" "$rc" "$logs"' EXIT

stage() {
    local name=$1
    shift
    # The subshell retains errexit. Its EXIT status and the tee status both
    # matter, including when the operator gets only a partial run.
    ( trap 'printf "%s\t%d\n" "$name" "$?" >> "$logs/exit-codes.tsv"' EXIT
      "$@"
    ) 2>&1 | tee "$logs/$name.log"
}

image_tag=aismixer-lab-cuda12.9:phase1
provenance() {
    check_checkout
    git status -sb --untracked-files=all
    source_identity | tee "$logs/source-identity"
    printf 'Image tag: %s\n' "$image_tag"
    sudo docker info --format 'Docker server: {{.ServerVersion}}'
    sudo docker image inspect "$image_tag" --format '{{.Id}}' > "$logs/image-id"
    local id
    id=$(<"$logs/image-id")
    [[ "$id" =~ ^sha256:[0-9a-f]{64}$ ]] || die "Expected a local immutable image ID."
    sudo docker image inspect "$id" --format 'Image {{.Id}} RepoDigests={{json .RepoDigests}} {{.Os}}/{{.Architecture}}'
    [[ "$(sudo docker image inspect "$id" --format '{{.Os}}/{{.Architecture}}')" == linux/amd64 ]] ||
        die "Expected a linux/amd64 image."
}
stage provenance provenance
image_id=$(<"$logs/image-id")

common=(--rm --pull=never --network=none --runtime=nvidia
    --user "$(id -u):$(id -g)" --security-opt no-new-privileges --cap-drop ALL
    -e NVIDIA_VISIBLE_DEVICES=all -e NVIDIA_DRIVER_CAPABILITIES=compute,utility
    -e PYTHONDONTWRITEBYTECODE=1
    -v "$src:/src:ro" -v "$build_dir:/build:rw" -w /build)

host_container() {
    sudo docker run -i "${common[@]}" -e CUDA_VISIBLE_DEVICES=-1 "$image_id" "$@"
}

stage toolchain host_container bash -eu -o pipefail -s <<'CONTAINER'
source /etc/os-release
echo "$PRETTY_NAME"
[[ "$ID/$VERSION_ID" == ubuntu/24.04 ]]
for tool in /usr/bin/gcc-13 /usr/bin/g++-13 /usr/local/cuda-12.9/bin/nvcc \
    /usr/local/cuda-12.9/bin/cuobjdump cmake ctest ninja python3; do
    command -v "$tool"
    "$tool" --version
done
dpkg-query -W -f='${Package} ${Version}\n' libyaml-cpp-dev
CONTAINER

stage configure host_container env -u CUDAHOSTCXX cmake -S /src/lab -B /build -G Ninja \
    -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=ON \
    -DCMAKE_CXX_COMPILER=/usr/bin/g++-13 -DCMAKE_CUDA_HOST_COMPILER=/usr/bin/g++-13 \
    -DCMAKE_CUDA_COMPILER=/usr/local/cuda-12.9/bin/nvcc -DCUDAToolkit_ROOT=/usr/local/cuda-12.9 \
    -DCMAKE_CUDA_ARCHITECTURES=61 -DAISMIXER_LAB_BUILD_SCENE_RUNNER=ON \
    -DAISMIXER_LAB_BUILD_GEO_RUNNER=ON -DAISMIXER_LAB_BUILD_RUNTIME_RUNNER=ON -DAISMIXER_LAB_BUILD_WORLD=ON
stage build host_container cmake --build /build -j2
stage list host_container bash -eu -o pipefail -s <<'CONTAINER'
ctest --test-dir /build -N
ctest --test-dir /build --show-only=json-v1 > /build/inventory.json
for target in aismixer_lab_scene aismixer_lab_geo aismixer_lab_runtime_runner aismixer_lab_world; do
    test -f "/build/$target" && test -x "/build/$target" || { echo "Missing executable $target"; exit 1; }
done
/usr/local/cuda-12.9/bin/cuobjdump --list-elf /build/aismixer_lab_world |
    grep -E 'ELF file.*sm_61[.]cubin([[:space:]]|$)'
CONTAINER
stage inventory host_container python3 - <<'PYTHON'
import json
from pathlib import Path

# Each row assigns the complete CMake label set to the named tests.
groups = {
    "host runtime": "runtime_core runtime_journal runtime_playback runtime_rf_contract runtime_journal_v2 "
                    "runtime_rf_boundary runtime_contract runtime_cli_oracle",
    "host runtime stress": "runtime_streaming runtime_streaming_oracle",
    "host": "scene_host scene_cli_host",
    "host AIS": "ais_codec ais_smoke ais_wire_oracle",
    "host AIS classa": "classa_payload classa_wire_oracle",
    "host AIS classa station": "classa_station classa_station_oracle",
    "host AIS classa knowledge": "classa_knowledge classa_knowledge_oracle",
    "host AIS classa access": "classa_access classa_access_oracle",
    "host geo": "geo_host geo_unavailable geo_config",
    "host geo AIS": "geo_output",
    "host world": "world_host world_geodesy world_config world_cli_host",
    "GPU": "gpu_self scene_self scene_cli_gpu scene_cli_interactive",
    "GPU runtime": "runtime_cuda_bridge",
    "GPU geo": "geo_gpu",
    "GPU geo AIS": "geo_wire_oracle geo_cli_closure",
    "GPU world": "world_gpu",
    "GPU world stress": "world_cli_gpu",
}
expected = {f"aismixer_lab_{name}_test": set(labels.split())
            for labels, names in groups.items() for name in names.split()}
actual = {}
for test in json.loads(Path("/build/inventory.json").read_text())["tests"]:
    name = test["name"]
    if name in actual:
        raise SystemExit(f"Duplicate CTest registration: {name}")
    props = {prop["name"]: prop["value"] for prop in test["properties"]}
    if props.get("DISABLED") or not test.get("command"):
        raise SystemExit(f"Disabled or unrunnable test: {name}")
    actual[name] = set(props.get("LABELS", []))

if actual != expected:
    for name in sorted(actual.keys() | expected.keys()):
        if actual.get(name) != expected.get(name):
            print(f"{name}: expected {expected.get(name)}, found {actual.get(name)}")
    raise SystemExit("FULL inventory differs from the reviewed registration.")
print("Verified 41 FULL test registrations and labels (31 host, 10 GPU).")
PYTHON

stage host host_container ctest --test-dir /build -L '^host$' --no-tests=error \
    --output-on-failure -V --output-junit /build/host.xml

stage gpu sudo docker run -i "${common[@]}" "$image_id" bash -eu -o pipefail -s <<'CONTAINER'
unset CUDA_VISIBLE_DEVICES
devices=$(nvidia-smi --query-gpu=uuid,name,compute_cap,driver_version --format=csv,noheader)
printf '%s\n' "$devices"
# CUDA tests use ordinal zero. Select a matching physical device by UUID even
# on a mixed-GPU host. Never run this profile on a substitute device.
uuid=$(awk -F ', *' '$2 ~ /^(NVIDIA )?Quadro P2000$/ && $3 == "6.1" {print $1; exit}' <<< "$devices")
[[ "$uuid" == GPU-* ]] || { echo "Required Quadro P2000 sm_61 unavailable" >&2; exit 1; }
export CUDA_VISIBLE_DEVICES="$uuid"
echo "GPU pass CUDA_VISIBLE_DEVICES=$uuid"
ctest --test-dir /build -L '^GPU$' --no-tests=error --output-on-failure -V --output-junit /build/gpu.xml
CONTAINER
# Corroborate the NVML selection with W1's report from its actual CUDA run.
stage world-device grep -E 'gpu=(NVIDIA )?Quadro P2000 sm=6\.1 runtime=12090 driver=[0-9]+ build=GNU_13\.[0-9.]+_CUDA_12\.9\.[0-9]+_sm61_fmad_false([[:space:]]|$)' "$logs/gpu.log"

stage results host_container python3 - <<'PYTHON'
import json
from pathlib import Path
import xml.etree.ElementTree as ET

tests = json.loads(Path("/build/inventory.json").read_text())["tests"]
for label in ("host", "GPU"):
    names = {t["name"] for t in tests for p in t["properties"] if p["name"] == "LABELS" and label in p["value"]}
    cases = ET.parse(f"/build/{label.lower()}.xml").findall(".//testcase")
    if (len(cases) != len(names) or {case.get("name") for case in cases} != names or
            any(case.get("status") != "run" or case.find("skipped") is not None or
                case.find("failure") is not None or case.find("error") is not None for case in cases)):
        raise SystemExit(f"Incomplete {label} execution or non-passing result. Inspect the JUnit report.")
    print(f"{label}: {len(cases)}/{len(names)} passed")
PYTHON

verify_source() {
    check_checkout
    source_identity > "$logs/source-identity-final"
    diff -u "$logs/source-identity" "$logs/source-identity-final"
    git status -sb --untracked-files=all
}
stage final-source verify_source
printf 'FULL Linux/P2000 qualification passed. Build and JUnit reports: %s\nLogs: %s\n' "$build_dir" "$logs"
