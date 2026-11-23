# AISMixer Lab FULL Linux / CUDA qualification

**Phase 2 status — pending operator execution on the Quadro P2000.** This
Release profile builds the B2.1 Scene, B3.1 Geo, R0 Runtime and W1 World
operators and runs their acceptance tests alongside the Phase 1 suite. The
expected result from the current CMake source is **31 host and 10 GPU tests**.

## Run the qualification

Use the regular operator account in the Linux checkout on branch
`task/lab-linux-full`. Docker must have its NVIDIA runtime configured and the
local linux/amd64 image `aismixer-lab-cuda12.9:phase1` available. The host
supplies the driver. The image supplies CUDA, GCC, CMake, Ninja, Python and
yaml-cpp. If the tag is absent, use the image-build command in the Phase 1
recipe from the Phase 2 checkout. Only Docker commands require sudo.
[Phase 1 image recipe](../phase1/README.md) · [Dockerfile](../phase1/Dockerfile)

Run this command from any working directory. Keep the checkout stable until
the script exits.

```bash
bash /home/dev/src/aismixer-lab-linux-full/lab/qualification/phase2/qualify.sh
```

Each invocation prints its fresh `/var/tmp/aismixer-phase2.XXXXXXXX` path.
Keep both `logs/` and `build/`, including on failure. Return
`logs/exit-codes.tsv`, the failed stage log on error, or `host.log`, `gpu.log`
and `results.log` with the provenance and toolchain logs on success. A rerun
uses a new directory. Git finalization belongs to the operator after review.

## Source and toolchain evidence

The Phase 1 checkpoint is `a5f0aaadbe8f373f6e40604bd66c356b2fe4f33d`.
The operator observed 89/89 Ninja steps, an sm_61 cubin, 23/23 host tests and
4/4 GPU tests for the base profile. The observed environment was an Ubuntu
26.04 host with driver 580.178.04 and an Ubuntu 24.04 CUDA 12.9.2 container
with nvcc 12.9.86, GCC/G++ 13.3.0, CMake 3.28.3, Ninja 1.11.1, Python 3.12.3
and libyaml-cpp-dev 0.8.0+dfsg-6build1. The recorded local image ID was
`sha256:6d6730b4c520e0f63c521f93fa301917bbf5cc43203835545267a75d8b51ae6f`.

For the FULL run, the script records the actual branch, HEAD, status, SHA-256
of the tracked binary diff against HEAD, and a digest covering untracked,
non-ignored filenames and contents. It rechecks these identities at completion.
It resolves the local image tag once, records the image ID and available
RepoDigests, and runs every stage by ID with pulling and networking disabled.
Rebuilding the Phase 1 Dockerfile can resolve different apt package revisions.
The run records the installed versions. Pinning apt snapshots is future work.

All containers use the operator's UID:GID, `--runtime=nvidia`, dropped
capabilities and `no-new-privileges`. The checkout is mounted read-only at
`/src` for native fixtures and Python oracles. The same writable build is
mounted at `/build` for each stage.

CMake configures `/src/lab` with Ninja, Release, testing enabled, and
`AISMIXER_LAB_BUILD_{SCENE_RUNNER,GEO_RUNNER,RUNTIME_RUNNER,WORLD}=ON`.
Both C++ and nvcc's host compiler use `/usr/bin/g++-13`. First configuration
unsets `CUDAHOSTCXX` in an empty build directory. CMake checks the resolved
host compiler paths and leaves nvcc's compatibility checks enabled.
`CUDAToolkit_ROOT=/usr/local/cuda-12.9`, its `bin/nvcc` and architecture
`61` select the Pascal toolchain. The YAML targets require the local
`find_package(yaml-cpp 0.8 CONFIG ...)` package.

## Acceptance gates

1. `cmake --build /build -j2` must succeed. Check the four executable targets,
   `ctest --test-dir /build -N`, the JSON test inventory and the World
   executable's sm_61 ELF cubin.
2. Run CTest with `-L '^host$'`, `NVIDIA_VISIBLE_DEVICES=all`,
   `NVIDIA_DRIVER_CAPABILITIES=compute,utility` and `CUDA_VISIBLE_DEVICES=-1`.
   This supplies the driver libraries while hiding devices at the CUDA API.
   Scene and World validate on the host. Geo and World must diagnose real
   CUDA unavailability.
3. Run CTest with `-L '^GPU$'` and the same driver access. The script clears
   any CUDA mask and selects an enumerated Quadro P2000 with compute capability
   6.1 by UUID, so that it is the device at ordinal zero. The World CLI oracle
   logs the device, runtime, driver and compiled identity from the five-hour run.
   The script requires that record to confirm the P2000 and the GNU/sm_61 build.
4. Both passes use the registered timeouts, verbose output and
   `--output-on-failure`. Check each JUnit report against the registered names
   and require every case to run and pass. Each stage and the overall run
   record an exit code.

| CMake registration | Host | GPU | Added test suffixes (each name is `aismixer_lab_<suffix>_test`) |
| --- | ---: | ---: | --- |
| Phase 1 base | 23 | 4 | Includes the five independent AIS/Class A Python oracles |
| Scene | 1 | 2 | `scene_cli_host`, `scene_cli_gpu`, `scene_cli_interactive` |
| Geo | 1 | 2 | `geo_config`, `geo_wire_oracle`, `geo_cli_closure` |
| Runtime | 2 | 0 | `runtime_cli_oracle`, `runtime_streaming_oracle` |
| World | 4 | 2 | `world_host`, `world_geodesy`, `world_config`, `world_cli_host`, `world_gpu`, `world_cli_gpu` |
| **FULL** | **31** | **10** | **41** |

The earlier estimate of 38 does not account for all 14 optional registrations
in this checkout. The inventory stage specifies every expected name and its
complete label set. Missing Python oracles, duplicates, disabled tests and
changed labels fail qualification. [Full inventory and procedure](qualify.sh)

During implementation on 2026-11-23, source-level evaluation confirmed all
41 names and complete label sets. The inventory checker rejected missing,
duplicate and mislabelled entries. Bash syntax, Python syntax and whitespace
checks passed. With host G++ 15.2, the standalone W1 host and geodesy tests
passed, including 57 invalid contracts and 119 geodesy cases. An isolated W1
writer check preserved a competing destination created after temporary-file
creation, cleaned the temporary file and diagnosed EPIPE. The R0 CLI oracle
passed with logical digest `728b400ac805d49b`. The Docker qualification and
FULL CTest results remain pending.

## W1 output and numeric reference

Linux requires UTF-8 arguments and filenames. The W1 writer emits LF-framed
NDJSON, retrying interrupted writes and advancing on partial writes. A closed
pipe produces a nonzero exit with a stdout diagnostic. For a named output,
use a stable, operator-controlled destination directory on a filesystem that
supports Linux `renameat2(RENAME_NOREPLACE)` (for example ext4). W1 opens an
exclusive temporary file there with mode 0600, fsyncs and closes it, then
atomically publishes it only if the destination is still absent. Publication
errors fail the command. Exception cleanup removes the owned temporary file.
Forced termination can leave a temporary file. Directory-entry survival
across power loss is outside this acceptance contract.

Linux reports process peak resident bytes from `getrusage(RUSAGE_SELF)`,
converting Linux `ru_maxrss` from KiB, or reports memory unavailable.
The suite checks Unicode paths, CRLF/LF source normalization, competing
publications, failed stdout, fresh OS seed acquisition and explicit-seed
replay. Fault injection of `getrandom` short reads and EINTR is unverified.

The five-hour W1 numeric golden remains **UNQUALIFIED / SKIPPED on Linux**.
Its 5,319 records and FNV-1a64 `6328099c89f9a685` are pinned to the shipped
profiles on the Windows GTX 1050 Ti, CUDA runtime 12090 and
`MSVC_19.44.35229.0_CUDA_12.9.86_sm61_fmad_false`. Linux still checks the
three pinned source fingerprints, independent Philox schedules, geodesy
tolerances, causal order, trace digest and exact bytes across launch shapes.
Geo likewise retains its reference gate, wire checks and fixed-seed replay.
These simulations retain their existing scope, including W1's world and
lifecycle output only. [W1 acceptance](../../docs/W1_WORLD_LIFECYCLE.md) ·
[Geo acceptance](../../docs/B31_ACCEPTANCE.md)

Native Windows VS2022/CUDA 12.9 regression is **unverified** for these Phase 2
changes. The FULL Linux milestone requires the operator's successful P2000 run.
