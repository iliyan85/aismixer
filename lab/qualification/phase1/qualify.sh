#!/usr/bin/env bash
# Run as a regular user with Bash. Only Docker commands use sudo.
set -Eeuo pipefail

die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd -P)
expected_branch=task/lab-linux-base
image_tag=aismixer-lab-cuda12.9:phase1

check_checkout() {
    local git_root branch
    git_root=$(git -C "$source_dir" rev-parse --show-toplevel)
    branch=$(git -C "$source_dir" branch --show-current)
    [[ "$git_root" == "$source_dir" && -f "$source_dir/lab/CMakeLists.txt" ]] ||
        die "Run the script from its checked-in location in the lab checkout."
    [[ "$branch" == "$expected_branch" ]] ||
        die "Expected branch $expected_branch in $source_dir (found ${branch:-detached HEAD})."
    git -C "$source_dir" diff --check HEAD
}

new_directory() {
    local parent
    parent=$(realpath -m -- "$1")
    case "$parent/" in
        "$source_dir/"*) die "Build and log directories must be outside the checkout: $parent" ;;
    esac
    [[ "$parent" != *:* ]] || die "Directory paths must not contain a colon: $parent"
    mkdir -p -- "$parent"
    mktemp -d -- "$parent/$2.XXXXXX"
}

patch_digest() {
    git -C "$source_dir" diff --binary --no-ext-diff --no-textconv HEAD | sha256sum
}

check_checkout
(( EUID != 0 )) || die "Run as a regular user. The script invokes sudo for Docker."
[[ "$source_dir" != *:* ]] || die "The checkout path must not contain a colon."
source_head=$(git -C "$source_dir" rev-parse HEAD)
source_diff=$(patch_digest)
build_dir=$(new_directory "${AISMIXER_PHASE1_BUILD_PARENT:-${TMPDIR:-/tmp}}" aismixer-phase1-build)
log_dir=$(new_directory "${AISMIXER_PHASE1_LOG_PARENT:-${TMPDIR:-/tmp}}" aismixer-phase1-logs)
printf 'Build directory: %s\nLog directory: %s\n' "$build_dir" "$log_dir"

common=(
    --rm --pull=never --network=none --runtime=nvidia
    --user "$(id -u):$(id -g)"
    --security-opt=no-new-privileges --cap-drop=ALL
    -e NVIDIA_DRIVER_CAPABILITIES=compute,utility
    -e NVIDIA_VISIBLE_DEVICES=all
    -v "$source_dir:/src:ro" -v "$build_dir:/build:rw"
    -w /build
)

preflight() {
    git -C "$source_dir" status -sb
    printf 'Git HEAD: %s\nTracked diff against HEAD (SHA-256): %s\n' "$source_head" "${source_diff%% *}"
    printf 'Image tag: %s\n' "$image_tag"
    sudo docker info --format 'Docker: {{.ServerVersion}}'
    sudo docker image inspect "$image_tag" --format '{{.Id}}' > "$log_dir/image-id"
    local image_id
    image_id=$(<"$log_dir/image-id")
    [[ "$image_id" =~ ^sha256:[0-9a-f]{64}$ ]] || die "Expected one local image ID for $image_tag."
    sudo docker image inspect "$image_id" \
        --format 'Image ID: {{.Id}} / RepoDigests: {{json .RepoDigests}} / Platform: {{.Os}}/{{.Architecture}}'
    [[ "$(sudo docker image inspect "$image_id" --format '{{.Os}}/{{.Architecture}}')" == linux/amd64 ]] ||
        die "Expected a linux/amd64 image for $image_tag."
    sudo docker run "${common[@]}" -e CUDA_VISIBLE_DEVICES=-1 "$image_id" bash -euo pipefail -c '
        for tool in /usr/bin/gcc-13 /usr/bin/g++-13 cmake ninja python3 ctest \
            /usr/local/cuda-12.9/bin/nvcc /usr/local/cuda-12.9/bin/cuobjdump nvidia-smi; do
            if ! tool_path=$(command -v "$tool") || [[ ! -x "$tool_path" ]]; then
                printf "Missing required tool: %s\n" "$tool" >&2
                exit 10
            fi
            printf "\n%s\n" "$tool_path"
            "$tool_path" --version
        done
        if ! dpkg-query -W -f "\${Package} \${Version}\n" libyaml-cpp-dev; then
            echo "libyaml-cpp-dev package revision unavailable"
        fi
    '
}

preflight 2>&1 | tee "$log_dir/preflight.log"
image_id=$(<"$log_dir/image-id")

sudo docker run "${common[@]}" -e CUDA_VISIBLE_DEVICES=-1 "$image_id" bash -euo pipefail -c '
    configure=(
        env -u CUDAHOSTCXX cmake -S /src/lab -B /build -G Ninja
        -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=ON
        -DCMAKE_CXX_COMPILER=/usr/bin/g++-13
        -DCMAKE_CUDA_HOST_COMPILER=/usr/bin/g++-13
        -DCMAKE_CUDA_COMPILER=/usr/local/cuda-12.9/bin/nvcc
        -DCUDAToolkit_ROOT=/usr/local/cuda-12.9
        -DCMAKE_CUDA_ARCHITECTURES=61
        -DAISMIXER_LAB_BUILD_SCENE_RUNNER=OFF
        -DAISMIXER_LAB_BUILD_GEO_RUNNER=OFF
        -DAISMIXER_LAB_BUILD_RUNTIME_RUNNER=OFF
        -DAISMIXER_LAB_BUILD_WORLD=OFF
    )
    printf "Configure:"
    printf " %q" "${configure[@]}"
    printf "\n"
    "${configure[@]}"
    cmake --build /build --parallel 2
    ctest --test-dir /build -N
    ctest --test-dir /build --show-only=json-v1 > /build/phase1-test-inventory.json
    python3 - /build/phase1-test-inventory.json <<\PYTHON
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    inventory = json.load(stream)

prefix = "aismixer_lab_"
host_suffixes = [
    "runtime_core", "runtime_journal", "runtime_playback", "runtime_rf_contract",
    "runtime_journal_v2", "runtime_rf_boundary", "runtime_streaming", "runtime_contract",
    "scene_host", "ais_codec", "ais_smoke", "classa_payload", "classa_station",
    "classa_knowledge", "classa_access", "ais_wire_oracle", "classa_wire_oracle",
    "classa_station_oracle", "classa_knowledge_oracle", "classa_access_oracle",
    "geo_host", "geo_output", "geo_unavailable",
]
gpu_suffixes = ["runtime_cuda_bridge", "gpu_self", "scene_self", "geo_gpu"]
expected = {
    prefix + suffix + "_test": label
    for label, suffixes in (("host", host_suffixes), ("GPU", gpu_suffixes))
    for suffix in suffixes
}

actual = {}
for test in inventory["tests"]:
    name = test["name"]
    if name in actual:
        sys.exit(f"Duplicate CTest entry {name}. A reviewed inventory update is required.")
    labels = {
        label
        for prop in test.get("properties", [])
        if prop["name"] == "LABELS"
        for label in prop["value"]
    }
    actual[name] = labels & {"host", "GPU"}

expected_labels = {name: {label} for name, label in expected.items()}
if actual != expected_labels:
    for name in sorted(actual.keys() | expected_labels.keys()):
        if actual.get(name) != expected_labels.get(name):
            print(f"{name}: expected {expected_labels.get(name)}, found {actual.get(name)}",
                  file=sys.stderr)
    sys.exit("Expected exactly 27 base tests (23 host, 4 GPU). Check Python and the base "
             "profile. If tests changed, request a reviewed inventory update.")
print("Verified 27 registered tests (23 host, 4 GPU).")
PYTHON
    /usr/local/cuda-12.9/bin/cuobjdump --list-elf /build/aismixer_lab_gpu_smoke |
        grep -E "ELF file.*sm_61[.]cubin([[:space:]]|$)"
    ctest --test-dir /build -L "^host$" --output-on-failure
' 2>&1 | tee "$log_dir/build-host.log"

sudo docker run "${common[@]}" "$image_id" bash -euo pipefail -c '
    unset CUDA_VISIBLE_DEVICES
    nvidia-smi --query-gpu=name,compute_cap,driver_version --format=csv,noheader
    ctest --test-dir /build -L "^GPU$" --output-on-failure
' 2>&1 | tee "$log_dir/gpu.log"

check_checkout
[[ "$(git -C "$source_dir" rev-parse HEAD)" == "$source_head" && "$(patch_digest)" == "$source_diff" ]] ||
    die "Git HEAD or the tracked diff changed during qualification. Review and rerun."
git -C "$source_dir" status -sb
printf 'Qualification finished. Logs: %s\nBuild: %s\n' "$log_dir" "$build_dir"
