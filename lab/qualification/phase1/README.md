# AISMixer Lab Linux Phase 1 qualification

This procedure qualifies the Linux/amd64 base build and its host and GPU tests
with CUDA 12.9 and GCC 13. The optional Scene, Geo, Runtime, and World operators
are Phase 2 work.

## Operator recipe

Use a Linux host with Docker, sudo access for Docker, and NVIDIA Container
Toolkit configured for the `nvidia` runtime. The NVIDIA driver stays on the
host and the runtime supplies its libraries to the containers. The CUDA Toolkit
comes from the image. Run the script as a regular user with Bash and use sudo for
Docker without changing Docker group membership or socket permissions.

From the repository root, build the local image.

```bash
sudo docker build --platform linux/amd64 --pull=false \
  -t aismixer-lab-cuda12.9:phase1 \
  -f lab/qualification/phase1/Dockerfile lab/qualification/phase1
```

The base image is pinned by digest. The build resolves apt packages from live
repositories. Full bitwise reproducibility requires a pinned package snapshot
and a recorded final built-image digest. GCC 13 and G++ 13 came from the
operator's base image and the script checks for them before configuring.

Invoke the script from any working directory using the path to the Phase 1
checkout.

```bash
bash /path/to/checkout/lab/qualification/phase1/qualify.sh
```

The script resolves the checkout from its location and requires the
`task/lab-linux-base` branch. Adapt that guard in a separately reviewed change
before using the script on a post-merge branch. Keep the checkout stable during
a run. The script records HEAD and the SHA-256 of the tracked binary diff
against HEAD, then resolves the local image tag to an image ID and logs the ID
and any RepoDigests. Qualification runs use that ID with `--pull=never` and
network access disabled.

The script creates fresh build and log directories under `${TMPDIR:-/tmp}`.
Set `AISMIXER_PHASE1_BUILD_PARENT` and `AISMIXER_PHASE1_LOG_PARENT` to choose
writable parents outside the checkout. Each container mounts the checkout as
`/src:ro` and the same fresh build directory as `/build:rw`. CTest uses
absolute paths into `/src` for the five Python oracles. The build mount retains
the executables, CTest registrations, and test outputs across containers. A fresh
CMake build avoids stale compiler settings. Configure unsets `CUDAHOSTCXX`
because CMake can give that environment variable precedence over the specified
CUDA host compiler on first configure.

The following checks must pass.

- Preflight finds the toolchain and logs its paths and versions, the installed
  yaml-cpp package revision when available, and the Git and image provenance.
- Ninja builds the Release base profile with testing enabled, all four optional
  operators off, and CUDA architecture `61`. `cuobjdump` must list an
  `sm_61` ELF cubin in `aismixer_lab_gpu_smoke`.
- CTest registers exactly 27 expected tests, with 23 labelled `host` and four
  labelled `GPU`. Missing Python oracles, renamed tests, and changed labels
  fail the inventory check.
- The host pass uses `NVIDIA_VISIBLE_DEVICES=all` with driver capabilities
  `compute,utility` to supply the NVIDIA driver libraries. It sets
  `CUDA_VISIBLE_DEVICES=-1` so CUDA reports no devices. This setting hides
  devices at the CUDA API level only. Device nodes may still be accessible.
  `ctest --test-dir /build -L '^host$' --output-on-failure` must pass.
- The GPU pass uses the NVIDIA runtime with `NVIDIA_VISIBLE_DEVICES=all` and
  unsets `CUDA_VISIBLE_DEVICES`. It records the GPU, compute capability, and
  driver before running
  `ctest --test-dir /build -L '^GPU$' --output-on-failure`.

The script preserves the directories and writes separate `preflight.log`,
`build-host.log`, and `gpu.log` files. A failed command in a logged pipeline
fails the script.

## Operator-observed evidence

- Source base and HEAD were `ddb8ed339809d9f9e3ad71fdc08b256cf6217834` with
  five uncommitted Phase 1 source changes. The observed patch digest and final
  commit remain unknown.
- The base was NVIDIA CUDA 12.9.2 devel on Ubuntu 24.04 at
  `sha256:16656a1ef115bca9e1f820c6349876f1486d2b3c9a0e615773799fe402960dc5`.
- The derived image was `aismixer-lab-cuda12.9:phase1` with local image ID
  `sha256:6d6730b4c520e0f63c521f93fa301917bbf5cc43203835545267a75d8b51ae6f`.
  A portable registry digest was not recorded.
- Tools were nvcc 12.9.86, GCC/G++ 13.3.0, CMake 3.28.3, Ninja 1.11.1,
  Python 3.12.3, and `libyaml-cpp-dev` 0.8.0+dfsg-6build1. The GPU was a
  Quadro P2000 (`sm_61`) with NVIDIA driver 580.178.04.
- Ninja completed 89/89 build steps and `cuobjdump` found an `sm_61` cubin.
  The corrected host invocation passed 23/23 tests with both mounts and the
  driver libraries present. The GPU invocation passed 4/4. The 27/27 total
  combines two CTest executions. An operator rerun of the checked-in recipe is
  pending.

The stock CUDA devel image lacked CMake. An earlier host pass hid devices at
the NVIDIA runtime level and the Geo negative test received an unexpected
runtime error. Another earlier invocation omitted `/src`, so five Python
oracle scripts could not open.

## Outstanding verification

Windows VS2022/CUDA 12.9 regression was not run, and cross-platform parity
remains unverified. The optional operators were off. W1 compiler metadata is
Phase 2 work. Alternate toolchain paths and `CUDAHOSTCXX` configurations were
not comprehensively validated, World entropy fault injection was not tested,
and a pinned apt snapshot remains outstanding.
