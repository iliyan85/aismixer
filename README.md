# AISMixer Lab — GPU-Accelerated AIS Simulation & Testing

AISMixer Lab is a standalone research and development laboratory within the
AISMixer ecosystem. It provides deterministic CUDA scene/event generation,
native YAML scenario execution, and standards-oriented host-side AIS payload
and NMEA 0183 encoding. It is independent of the production `aismixer` daemon
and related services.

The original scene runner uses the B2 GPU engine; the B3 AIS codec accepts
explicit message inputs. B3.1 adds a separate GPU geospatial path and an offline
Class A/B AIS bridge with explicit synthetic identities. UDP traffic generation,
traffic collection, RF modeling and production integration remain future work.
Logical event intents and `!AIVDM` framing do not establish radio
transmission, reception or real TDMA scheduling.

The `lab` branch has independent, orphan-rooted history and is not intended to
be merged or synchronized with `main` or `website`. `main` contains production
`aismixer` and related services; `website` contains the website. Each branch has
its own branch-relative `README.md`; this landing page belongs to `lab`.

## Overview and status

| Stage or capability | Current scope | Status |
| --- | --- | --- |
| [B1](#b1-cuda-foundation) | GPU cuRAND Philox and native CUDA foundation. | Implemented |
| [B2](#b2-deterministic-scenes-and-event-intents) | GPU scene engine and deterministic event intents. | Implemented |
| [B2.1](#b21-native-console-runner-and-yaml-scenarios) | Native YAML scene runner; requires local yaml-cpp ≥ 0.8. | Implemented |
| [B3](#b3-explicit-ais-model-and-host-only-nmea-encoder) | Host-only AIS model and NMEA framing for Types 1, 4, 9, 18 and base-only 21. | Implemented |
| [B3.1](#b31-gpu-first-geospatial-foundation) | CUDA placement, bounded geographic motion, polygon filtering, stable compaction and offline Type 1/18 bridge. | Implemented; qualified acceptance below |
| Broader scene-to-AIS mapping | Additional identity/category mappings and larger-distance geography. | Future |
| UDP traffic generation, collection and production integration | Separate traffic delivery and validation components. | Future |
| RF model and real TDMA scheduling | Radio propagation, reception and collision behavior. | Future |
| Full Linux build/CUDA qualification | Qualification of the complete laboratory outside the current Windows build. | Not qualified |

The data boundaries are explicit:

- `actor_id` is a scene identity, not an MMSI.
- `x_mm/y_mm` are local planar millimetres, not WGS84 longitude/latitude.
- `random_u32` is diagnostic PRNG output, not an AIS field.

B3 accepts explicit wire-compatible AIS fields, not B2 events. The detailed
stage sections below retain the contracts, limits, examples and caveats.

## Repository layout

Paths in this overview are relative to the repository root:

| Path | Contents |
| --- | --- |
| [lab/cuda/](lab/cuda/) | B1/B2 GPU engines, public APIs and smoke utilities. |
| [lab/runner/](lab/runner/) | Native console runner, YAML validation and output helpers. |
| [lab/scenes/](lab/scenes/) | Version 1 YAML scenario examples. |
| [lab/ais/](lab/ais/) | Host-only AIS model, binary encoder, NMEA framing and diagnostic CLI. |
| [lab/geo/](lab/geo/) | B3.1 GPU geographic engine, optional YAML operator and replaceable domain/overlay/receiver profiles. |
| [lab/docs/](lab/docs/) | GPU-first architecture, precision limits and recorded acceptance measurements. |
| [lab/tests/](lab/tests/) | Host/GPU checks, independent wire oracles and fixture provenance. |
| [lab/CMakeLists.txt](lab/CMakeLists.txt) | Windows build targets, optional runner and CTest registration. |

Short directory names in the detailed references, such as `cuda/` or `ais/`,
refer to subdirectories of `lab/`. CLI examples run from the repository root
unless stated otherwise.

## Getting started

The current top-level CMake project requires native Windows, VS2022 x64 and
CUDA Toolkit 12.9; its default architecture is Pascal `sm_61`. Begin with the
[B1 configure/build instructions](#configure-build-and-run). The ignored,
machine-local `cuda129-pascal` user preset is an alternative for an existing
setup, not a preset guaranteed to be present in a fresh checkout.

To use the YAML operator application, follow the [B2.1 dependency and opt-in
build instructions](#isolated-dependency-and-opt-in-build). A local yaml-cpp
package ≥ 0.8 is required, and `AISMIXER_LAB_BUILD_SCENE_RUNNER=ON` enables the
runner. The standalone B3 codec has no CUDA/yaml-cpp dependency, but configuring
the encompassing lab project still requires the Windows/CUDA toolchain.

B1/B2 GPU generation and the runner's GPU/interactive acceptance need a
compatible NVIDIA device. Host-only AIS codec tests run with CUDA hidden;
they do not qualify the complete Linux build. Build and test commands must use
the same configured directory, as shown in the detailed instructions.

Recorded B3 acceptance on Windows 11 / VS2022 / CUDA 12.9 / Pascal `sm_61` was
**9/9 Release CTests**, with the YAML runner and Python acceptance harness
enabled. This is the historical **pre-B3.1** acceptance, not the current full
suite. Initial B3.1 acceptance had 14 tests; the corrective closure passed
**16/16 Release CTests**, plus **4/4** GPU-hidden host AIS tests. See
[B31_ACCEPTANCE.md](lab/docs/B31_ACCEPTANCE.md) for evidence. Full runner/operator coverage requires local yaml-cpp,
both opt-in runner options and Python 3; disabled options reduce coverage.

## B1: CUDA foundation

B1 establishes a native Windows CUDA C++17 engine and a GPU smoke executable.
It does not generate AIS/NMEA traffic or send network packets.

### Toolchain and Pascal compatibility

The B1 baseline is Windows 11, Visual Studio Build Tools 2022 with the C++ desktop
workload, MSVC v143, a Windows SDK, CMake 3.24 or newer, and **CUDA Toolkit 12.9**.
The GTX 1050 Ti has compute capability 6.1; the default CUDA architecture is `61`.
CUDA 13.x no longer compiles for Pascal and must not be selected for this lab.

`nvidia-smi` reports the maximum CUDA version supported by the installed driver.
For example, driver 582.78 can report CUDA 13.0 while the installed compiler is
`nvcc V12.9.86`. Use `nvcc --version` to identify the toolkit. A newer compatible
driver does not require installing a newer toolkit.

### Configure, build and run

Run these PowerShell commands from the repository root, with CMake, CTest and
CUDA 12.9's `bin` directory available on `PATH`:

```powershell
nvcc --version
nvidia-smi
cmake --version
cmake -S lab -B lab/build -G "Visual Studio 17 2022" -A x64 -DCMAKE_CUDA_ARCHITECTURES=61
cmake --build lab/build --config Release
.\lab\build\Release\aismixer_lab_gpu_smoke.exe --probe
ctest --test-dir lab/build -C Release --output-on-failure
```

The standard configure command works when Visual Studio selects its installed
CUDA 12.9 integration. CMake pins toolkit discovery to CUDA 12.9 and checks the
Visual Studio selection **before enabling the CUDA language**. If several CUDA
integrations are installed, select 12.9 explicitly on the first configure:

```powershell
cmake -S lab -B lab/build -G "Visual Studio 17 2022" -A x64 -T cuda=12.9 -DCMAKE_CUDA_ARCHITECTURES=61
```

If the toolkit exists but Visual Studio's global CUDA build customizations are
missing, CMake also supports using the integration files in the toolkit itself.
Use this alternative for the first configure of the build tree:

```powershell
cmake -S lab -B lab/build -G "Visual Studio 17 2022" -A x64 -T "cuda=C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.9" -DCMAKE_CUDA_ARCHITECTURES=61
cmake --build lab/build --config Release
.\lab\build\Release\aismixer_lab_gpu_smoke.exe --probe
ctest --test-dir lab/build -C Release --output-on-failure
```

The custom toolkit root must contain `bin/nvcc.exe` and
`extras/visual_studio_integration/MSBuildExtensions` with CUDA 12.9's `.props`,
`.targets` and build-task DLL. This selects existing files; it does not copy files
into Visual Studio or change the machine. To use a nonstandard toolkit location,
pass that root with `-T "cuda=<root>"`; optionally set `-DCUDAToolkit_ROOT=<root>`.

A generator or toolset cannot be changed in an initialized CMake build tree.
Before retrying with different `-T` arguments, preserve or move the old
`lab/build` directory and configure a fresh `lab/build`. All build artifacts in
that directory are ignored by Git. Debug builds may use `--config Debug` and
the executable under `lab/build/Debug`; the validation baseline is Release.

If `cmake` or `ctest` is not on `PATH`, the Visual Studio Build Tools installation
may provide both in
`Common7/IDE/CommonExtensions/Microsoft/CMake/CMake/bin` under its installation
root. Invoke those executables by their full paths or add that directory to the
current shell's `PATH`.

For the default VS2022 Build Tools location, add its existing tools for this
PowerShell session, then run the commands above:

```powershell
$labCmakeBin = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\2022\BuildTools\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin"
$env:PATH = "$labCmakeBin;$env:PATH"
```

For future hardware, override `-DCMAKE_CUDA_ARCHITECTURES=61` with its supported
architecture, or a quoted list such as `-DCMAKE_CUDA_ARCHITECTURES="61;75"`.
The selected architectures must be supported by CUDA 12.9 and the actual GPU.
B1 does not automatically upgrade the toolkit.

### Smoke interface and deterministic output

The Release executable is `lab/build/Release/aismixer_lab_gpu_smoke.exe`.

```powershell
.\lab\build\Release\aismixer_lab_gpu_smoke.exe --help
.\lab\build\Release\aismixer_lab_gpu_smoke.exe --probe
.\lab\build\Release\aismixer_lab_gpu_smoke.exe --seed 42 --count 1025
.\lab\build\Release\aismixer_lab_gpu_smoke.exe --seed 42 --count 1025
.\lab\build\Release\aismixer_lab_gpu_smoke.exe --seed 43 --count 1025
.\lab\build\Release\aismixer_lab_gpu_smoke.exe --self-test
```

`--probe` reports device 0's actual name, compute capability, CUDA runtime and
driver API versions, and total/free GPU memory. Used alone it only probes;
combining it with `--seed` or `--count` also generates a batch. Generation defaults
to seed `42`, count `1025`, and a preview of up to eight samples. Seeds are unsigned
64-bit decimal integers. Counts range from `0` to `1,000,000` inclusive. Invalid
arguments, CUDA errors and failed validation return a nonzero exit status.
`--self-test` cannot be combined with `--seed` or `--count`; `--help` displays
usage without accessing CUDA.

Each record contains a stable 32-bit sample index and 32-bit random value. The
kernel initializes `curandStatePhilox4_32_10_t` with
`curand_init(seed, sample_index, 0, ...)`, then writes that sample's value into its
indexed output slot. Samples do not depend on thread scheduling. Count zero
still validates device availability, then returns an empty batch without sample
allocation or a kernel launch; nonzero batches run on the GPU. There is no CPU
random fallback or clock-derived seed.

The digest is FNV-1a 64-bit over the entire ordered output. For every sample,
the index followed by the random value is encoded as four bytes each, least
significant byte first. Start from `14695981039346656037`; XOR each byte and
multiply by `1099511628211` modulo 2^64. No struct padding or `std::hash` enters
the digest. An empty batch has the FNV offset-basis digest.

The two seed-42 commands should produce identical samples and digest; seed 43
should change generated values. Reproducibility is verified within the same
build, toolkit and device/runtime environment. B1 does not promise golden
digests across future cuRAND/toolkit versions or different environments.

`--self-test` executes actual GPU kernels. It compares repeated batches, checks
that another seed changes values, verifies indices and ordering, covers counts
`0`, `1`, `257`, `1025` and `1,000,000`, and rejects `1,000,001`. CTest runs this
same self-test with the `GPU` label and a 120-second timeout. It also checks
that shared sample indices retain their values across batch sizes, exercises
seeds `0` and `18446744073709551615`, and verifies that upper seed bits affect
the output. A successful
configure/build alone does not demonstrate GPU execution; the probe and
self-test must run on an available compatible NVIDIA GPU.

### Engine boundary and limitations

`cuda/engine_api.h` exposes CUDA-free C++ types and host-callable device probing
and bounded batch generation. `cuda/engine.cu` owns device operations and CUDA
error handling; `cuda/smoke_main.cpp` owns CLI parsing, digest and validation.
Device-side cuRAND Philox is provided by toolkit headers, so no host cuRAND
library or additional third-party framework is linked. The engine uses device
0 and synchronous generation; B1 has no throughput or multi-GPU objective.

When configuration fails, check the selected Visual Studio instance, installed
C++ desktop workload/Windows SDK and CUDA 12.9 integration. If nvcc rejects the
host compiler, select a supported installed MSVC toolset and a fresh build tree.
Do not suppress nvcc checks with `--allow-unsupported-compiler`. No automatic
installation or repair is performed by this lab. Inspect the CMake configure
output for compiler diagnostics; recent CMake versions also write
`lab/build/CMakeFiles/CMakeConfigureLog.yaml`.

VS Code's CMake Tools extension may detect `lab/CMakeLists.txt` automatically.
Point its source directory at `lab`, its build directory at `lab/build`, and
select the VS2022 **x64** configuration. Its automatic defaults can otherwise
create a root `build/` directory or attempt the unsupported Win32 platform.

If the runtime DLL is not found, make CUDA 12.9's `bin` directory available to
the process. If probing or generation fails, check `nvidia-smi`, GPU availability,
driver compatibility and the configured architecture. The reported failure is
an error, not a skipped or passed GPU test. Free memory varies with other GPU
users; the bounded batch is deliberately conservative for a 4 GB GTX 1050 Ti.

### Future architecture — documentation only

Later stages may simulate Class A/B vessels, base stations, AtoN, SAR and
emergency devices; channels, TDMA slots, reception and collisions; and network
delay, loss, duplication and possibly UDPSEC. The GPU will compute simulation
and generate events. A host CPU UDP dispatcher will handle networking. A Python
scenario controller, collector, validator and reporting layer will coordinate
tests against local, Raspberry Pi and OpenWrt targets. Those targets must not
require CUDA. B1 implements none of these later components.

## B2: deterministic scenes and event intents

B2 adds an immutable scene input and GPU-generated logical event intents. An
intent means that the scenario schedules an actor's report at a simulated tick.
It does not establish that a radio transmission occurred or was received. B1's
API, executable, commands and GPU self-test remain unchanged.

`cuda/scene_api.h` contains CUDA-free host types and
`generate_event_intents(actors, seed, start_tick, tick_count, tick_seconds)`.
The call accepts actors by const reference, does not mutate them, and returns
only active intents. There is no global scene state or implicit simulation clock.

Each actor has these explicit fields:

| Field | Type and meaning |
| --- | --- |
| `actor_id` | Unique `uint32`, stable across scene input order; independent of MMSI and TAG identifiers. |
| `category` | `ActorCategory`: `ClassA=0`, `ClassB=1`, `BaseStation=2`, `AtoN=3`, `SAR=4`, `Emergency=5`. |
| `x_mm`, `y_mm` | Initial signed `int64` positions in a local planar coordinate system, in millimetres. |
| `vx_mm_per_s`, `vy_mm_per_s` | Signed `int32` constant velocities, in millimetres per second. |
| `report_interval_ticks` | Positive `uint32` scenario-defined report interval. |
| `phase_ticks` | `uint32` phase, strictly smaller than the interval. |

Categories are scenario classifications. B2 does not apply regulatory Class A/B
reporting rates. `BaseStation` and `AtoN` require both velocity components to be
zero; the other categories may move. There is no geographic coordinate
projection, latitude/longitude, heading, turning, acceleration or interpolation.

Ticks are absolute `uint64` integers with a uniform positive integer duration.
Tick zero is the initial scene state. For absolute tick `t`:

```text
x_mm(t) = x_mm + vx_mm_per_s * t * tick_seconds
y_mm(t) = y_mm + vy_mm_per_s * t * tick_seconds
scheduled(t) = (t % report_interval_ticks) == phase_ticks
```

All arithmetic uses integer units; a call starting at a nonzero tick evaluates
motion from tick zero, not from the beginning of that call. The requested ticks
are `[start_tick, start_tick + tick_count)`. Returned intents contain
`absolute_tick`, `actor_id`, `category`, `x_mm`, `y_mm` and `random_u32`, ordered
by absolute tick ascending, then actor ID ascending.

### Bounds and execution ownership

Resource and arithmetic bounds and actor fields are checked before sorting or
GPU allocation. Duplicate IDs are checked on the bounded sorted copy:

| Input | Admitted range |
| --- | --- |
| Actors | 0 through 10,000; IDs must be unique and categories valid. |
| Ticks per call | 0 through 3,600. |
| Actor-tick candidates | `actors.size() * tick_count` at most 1,000,000. |
| Tick duration | 1 through 60 seconds. |
| Start and last included absolute tick | At most 1,000,000,000, including a validated start for empty requests. |
| Initial position component | -1,000,000,000,000 through +1,000,000,000,000 mm. |
| Velocity component | -100,000 through +100,000 mm/s. |

Actor count and tick count limits apply independently, even when their product
is zero. Division checks the candidate bound before multiplication; subtraction
checks the final tick before addition. The greatest admitted position magnitude
is `6,001,000,000,000,000` mm, safely within signed `int64`. Invalid scenarios
throw `std::invalid_argument`; CUDA failures throw `std::runtime_error`.

The CPU validates the scene, sorts a copy by actor ID and uploads those actors.
The GPU derives a bounded actor-tick grid, evaluates schedules and computes each
active event's position and Philox word. The CPU downloads candidates and compacts the active
ones while preserving canonical order. It does not compute a fallback scene or
PRNG. The explicit device representations have asserted layouts; no C++ vector
or string object is copied to the device.

This correctness baseline uses a 40-byte candidate slot, so at most 40 MB of
candidate GPU storage plus 0.4 MB of actor storage is required. Host candidate
and result vectors can together use about 80 MB. Inactive candidates are also
downloaded; this deliberate tradeoff avoids a GPU compaction framework. There
are no worker threads, custom streams, multi-GPU operations or IPC. Device 0 is
selected and its B2 kernel compatibility is checked even for empty scenes or
zero ticks; those calls allocate no sample buffers and launch no kernel.

### Deterministic event key and digest

For an active event, the GPU initializes `curandStatePhilox4_32_10_t` with:

```text
curand_init(seed, actor_id, absolute_tick, &state)
random_u32 = curand(&state)
```

The full `uint64` seed is explicit, the subsequence is the stable actor ID, and
the offset is the absolute tick measured in 32-bit PRNG words. B2 draws one word
for each event. Neither array index, request start, batch size nor launch shape
enters this key. Actor reordering and splitting adjacent ranges therefore retain
the same per-event word. This diagnostic word is not an AIS field, secret or
collision decision. Reproducibility has the same toolkit/environment limitation
as B1; there is no CPU PRNG or wall-clock seeding.

B2's FNV-1a-64 digest uses B1's offset basis and prime and these **36 serialized
bytes per event**, in canonical order:

```text
absolute_tick: 8 bytes
actor_id:      4 bytes
category:      4 bytes (numeric enum value)
x_mm:          8 bytes
y_mm:          8 bytes
random_u32:    4 bytes
```

Every field is serialized least-significant byte first. Signed positions are
converted to `uint64` modulo 2^64 before serialization. Object padding is never
hashed or compared. The digest of an empty event vector is `cbf29ce484222325`.

### Build, demo and validation

Use the B1 configure command above, including its explicit CUDA 12.9 toolset
alternative when needed. Then run from the repository root:

```powershell
cmake --build lab/build --config Release
.\lab\build\Release\aismixer_lab_scene_smoke.exe --help
.\lab\build\Release\aismixer_lab_scene_smoke.exe --demo
.\lab\build\Release\aismixer_lab_scene_smoke.exe --self-test
ctest --test-dir lab/build -C Release --output-on-failure
ctest --test-dir lab/build -C Release -R '^aismixer_lab_scene_self_test$' --output-on-failure
.\lab\build\Release\aismixer_lab_gpu_smoke.exe --seed 42 --count 1025
```

The separate B2 executable defaults to `--demo` when no arguments are supplied.
`--demo`, `--self-test` and `--help` are mutually exclusive; unknown, repeated or
conflicting options fail with exit status 2. CUDA or validation failures return
1. Help works without CUDA access. There are no scene-file or launch-size CLI
options in B2.

Demo stdout is CSV only, with header
`absolute_tick,actor_id,category,x_mm,y_mm,random_u32`. Categories use the numeric
codes above. Seed, range, event count and the whole-output digest are written to
stderr, so a CSV redirect remains machine-readable. The fixed demo uses seed 42,
six actors covering all categories, ticks 0 through 7, and two seconds per tick.
It emits 23 intents. Actor 10 starts at `(1000,-2000)` with velocity `(250,-100)`
and interval 2/phase 0; its tick-2 position is `(2000,-2400)`.

The B2 self-test checks an explicit small schedule/position oracle, repeated
records and digests, changed-seed invariants, reordered actors, two- and
three-part requests, stationary and moving actors, tick/phase boundaries, empty
requests, maximal admitted work and invalid scenarios. Both B1 and B2 CTests
must pass on an available GPU.

With `BUILD_TESTING=ON` (the default), `scene_test_api.h` enables a test-only
64/128/256-thread launch-size hook, and the B2 self-test compares its outputs.
The macro and hook are private to the B2 validation build; the public scene API
uses 256-thread blocks. With testing disabled, the hook is absent and the smoke
self-test reports that launch-shape validation was not run.

B2 generates no AIS/NMEA encoding, MMSI validation, radio message types, standards
based schedules, TDMA slots, VHF propagation or reception, RF collision outcomes,
network traffic, UDPSEC integration or Python control layer. Its scene/event
boundary can support later message abstractions and schedule fidelity. The
standalone B3 codec below does not adapt scene events to AIS messages.

## B2.1: native console runner and YAML scenarios

`aismixer_lab_scene` is the operator application, separate from
`aismixer_lab_scene_smoke`. It loads a scene, validates it on the CPU, and calls
the unchanged B2 `generate_event_intents(...)` API for GPU execution. CLI parsing,
YAML parsing, interaction, digest/CSV formatting and file I/O are host work.
Event intents still do not represent AIS frames, RF transmissions, reception or
standards-based AIS reporting schedules. The standalone B3 codec is separate;
the scene runner has no AIS output mode or geographic/identity adapter.

### Isolated dependency and opt-in build

The runner requires a **local yaml-cpp CMake package, version 0.8 or newer**;
0.8.0 is the minimum documented source version. B3.1 corrective closure uses
the already-installed 0.9.0 package. This is a C++ YAML library, separate
from the VS/CUDA toolchain and not supplied by CUDA. Only the runner links it.
There is no FetchContent download, package installation or global tool change.

`AISMIXER_LAB_BUILD_SCENE_RUNNER` defaults to `OFF`, preserving B1/B2 builds even
without yaml-cpp. The host validation/output library and its native tests need
neither yaml-cpp nor the CUDA runtime. Explicitly requesting the runner with
`ON` and no package produces an actionable configure error; no requested target
is silently omitted.

Use an already provisioned x64/Release yaml-cpp package and set its prefix or
`yaml-cpp_DIR`. From the repository root, with the tools on `PATH` as described
above:

```powershell
# Replace this with the local yaml-cpp installation prefix.
$labYamlPrefix = 'C:/path/to/local/yaml-cpp-prefix'
cmake -S lab -B lab/build -G "Visual Studio 17 2022" -A x64 -T "cuda=C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.9" -DCMAKE_CUDA_ARCHITECTURES=61 -DAISMIXER_LAB_BUILD_SCENE_RUNNER=ON "-DCMAKE_PREFIX_PATH=$labYamlPrefix"
cmake --build lab/build --config Release
ctest --test-dir lab/build -C Release --output-on-failure
.\lab\build\Release\aismixer_lab_scene.exe --help
```

Alternatively set `-Dyaml-cpp_DIR=<prefix>/lib/cmake/yaml-cpp`. An existing vcpkg
installation with yaml-cpp already installed can supply
`-DCMAKE_TOOLCHAIN_FILE=<vcpkg>/scripts/buildsystems/vcpkg.cmake` and
`-DVCPKG_TARGET_TRIPLET=x64-windows` on the first configure of a fresh build tree.
This task does not invoke vcpkg or provision packages automatically. Match the
package architecture, configuration and MSVC runtime to the application. For a
shared yaml-cpp package, its DLL must also be available beside the executable or
on `PATH`; CUDA 12.9's runtime DLL remains a separate requirement.

If you already have a trusted **yaml-cpp 0.8.0 source checkout**, this optional
operator-run sequence builds and installs it only under ignored `lab/build/`.
It does not acquire sources or install tools:

```powershell
$labYamlPrefix = Join-Path (Get-Location) 'lab/build/deps/yaml-cpp-install'
cmake -S '<local yaml-cpp 0.8.0 source>' -B lab/build/deps/yaml-cpp-build -G "Visual Studio 17 2022" -A x64 -DYAML_BUILD_SHARED_LIBS=OFF -DYAML_CPP_BUILD_TESTS=OFF -DYAML_CPP_BUILD_TOOLS=OFF -DYAML_CPP_INSTALL=ON "-DCMAKE_INSTALL_PREFIX=$labYamlPrefix"
cmake --build lab/build/deps/yaml-cpp-build --config Release
cmake --install lab/build/deps/yaml-cpp-build --config Release
# Now enable the runner using the prefix in the earlier configure command.
```

The user's ignored `lab/CMakeUserPresets.json` is retained without modification.
For the existing `cuda129-pascal` VS Code preset, the runner can use its separate
`lab/build/vscode-cuda129-preset` directory:

```powershell
cmake --preset cuda129-pascal -S lab -DAISMIXER_LAB_BUILD_SCENE_RUNNER=ON "-DCMAKE_PREFIX_PATH=$labYamlPrefix"
cmake --build lab/build/vscode-cuda129-preset --config Release
ctest --test-dir lab/build/vscode-cuda129-preset -C Release --output-on-failure
.\lab\build\vscode-cuda129-preset\Release\aismixer_lab_scene.exe --interactive
```

These preset commands require that local preset to exist; the explicit configure
command works without it. To continue B1/B2 and host tests while the dependency
is unavailable, configure with `-DAISMIXER_LAB_BUILD_SCENE_RUNNER=OFF` and use the
existing build/test commands. Do not substitute another CUDA toolkit to resolve
a YAML package problem.

### Noninteractive commands and output

All examples below run from the repository root:

```powershell
.\lab\build\Release\aismixer_lab_scene.exe --help
.\lab\build\Release\aismixer_lab_scene.exe --interactive
.\lab\build\Release\aismixer_lab_scene.exe --scene lab/scenes/demo.yaml
.\lab\build\Release\aismixer_lab_scene.exe --scene lab/scenes/demo.yaml --seed 42 --start-tick 0 --tick-count 8 --tick-seconds 2
.\lab\build\Release\aismixer_lab_scene.exe --scene lab/scenes/demo.yaml --validate
.\lab\build\Release\aismixer_lab_scene.exe --scene lab/scenes/demo.yaml --output lab/build/output/events.csv
.\lab\build\Release\aismixer_lab_scene.exe --scene lab/scenes/demo.yaml --format csv
```

`--scene PATH` loads required YAML defaults. `--seed`, `--start-tick`,
`--tick-count` and `--tick-seconds` override only the current run's simulation
fields; actor fields and the file on disk are never changed. YAML defaults must
themselves be valid before overrides are applied. `--validate` checks YAML/schema,
actor identity/category/schedule rules and all B2 arithmetic/resource bounds
without calling CUDA or allocating GPU buffers. It does not check GPU health.

`--format summary` is the default: scene name, actor/category counts, effective
parameters, event count, FNV digest and at most ten event rows. `--format csv`
emits only the canonical CSV header/rows on stdout; path/status/count/digest
diagnostics go to stderr. Categories retain B2 numeric codes 0 through 5. Native
CSV is UTF-8 with LF line endings, independent of stream locale or number flags.
Windows CSV stdout uses binary mode so it matches exported CSV bytes. Prefer
`--output` for a native file export when shell redirection would re-encode text.

`--output PATH` writes the complete CSV and creates missing parent directories.
Existing destinations are refused before GPU execution and again at publication.
An exclusive same-directory temporary file is fully written, flushed and closed,
then published without replacement. Completion is printed only after success.
Failures attempt temporary-file cleanup; forced process termination may leave
a temporary file but never publishes a partial destination. No full CSV string
or second event-sized vector is allocated for formatting. Exports are opt-in;
files under `lab/build/output` remain local and Git-ignored.

User paths resolve relative to the current working directory and are reported
as resolved paths. Windows command-line arguments and filesystem operations
preserve Unicode filenames; YAML and redirected interactive input use UTF-8.
Unknown/repeated options, missing values, invalid integer spelling/width and
incompatible modes fail explicitly. `--help` is used alone. `--validate` rejects
`--output` and `--format csv`; `--interactive` rejects `--validate`, `--format`
and `--output`, but accepts a startup scene and simulation overrides.

| Exit | Meaning |
| --- | --- |
| 0 | Success, help or clean interactive exit/EOF. |
| 2 | CLI syntax, YAML/schema or scenario validation error. |
| 3 | File/stream I/O or other host failure. |
| 4 | GPU execution/availability failure. |

### Interactive workflow

With terminal stdin, no arguments enter the native menu. With redirected stdin,
no arguments fail promptly; use explicit `--interactive` for scripted sessions.
Startup looks for `lab/scenes/demo.yaml` from the repository root, then
`scenes/demo.yaml` from a lab working directory. If no usable default is present,
the menu asks the operator to load a path; no developer absolute path is baked in.

The menu loads YAML, shows actor IDs/categories/positions/velocities/intervals and
phases, edits run parameters, validates without GPU, runs with a bounded preview,
or runs and exports to a selected path. Inspection previews 100 actors and offers
an explicit full listing for larger scenes. Actors are edited in YAML externally.
Loading another file adopts its simulation defaults. Enter retains a parameter;
each field gets at most three invalid attempts before retaining its prior value.
Every edit must keep the complete range/candidate bounds valid. Load/action
failures retain the current scene/settings, while explicit invalid startup
arguments fail with a nonzero exit. EOF exits cleanly; Ctrl+C retains ordinary
console process-termination behavior. Runs are synchronous, with no sleeps or
real-time pacing.

### Version 1 YAML schema and reference scenarios

Files are UTF-8, at most 1 MiB, and contain exactly one YAML mapping document.
Required top-level fields are `schema_version: 1`, `name`, `simulation` and
`actors`. Name is 1 through 200 UTF-8 bytes, contains a non-space character and
has no control characters. Simulation requires all four fields shown below;
every actor requires all eight B2 fields. Empty `actors: []` is allowed.

```yaml
schema_version: 1
name: small-local-scene
simulation:
  seed: 42
  start_tick: 0
  tick_count: 8
  tick_seconds: 2
actors:
  - actor_id: 10
    category: ClassA
    x_mm: 1000
    y_mm: -2000
    vx_mm_per_s: 250
    vy_mm_per_s: -100
    report_interval_ticks: 2
    phase_ticks: 0
```

Integer scalars use ASCII base-10 digits: unsigned fields have no sign, signed
position/velocity fields permit a leading minus. Plain or quoted decimal scalars
are accepted; leading zeros are decimal. Empty values, whitespace, plus signs,
fractions, exponents, NaN, hexadecimal and values outside the required integer
width are rejected, then the narrower B2 bounds apply. Seed supports the complete
`uint64` range. Categories are exactly `ClassA`, `ClassB`, `BaseStation`, `AtoN`,
`SAR` or `Emergency`. Duplicate IDs/keys, unknown/missing fields, unsupported
schema versions, aliases and explicit/custom tags are rejected with filename,
field and available line/column. There are no includes, environment expansion,
URL fetching or execution hooks.

Coordinates are local millimetres, velocities are mm/s, and tick zero is the
initial state. Motion and scheduling retain the B2 integer rules above,
particularly `t % report_interval_ticks == phase_ticks`. No standards-based
intervals are inferred and no bounds are silently clamped.

`lab/scenes/demo.yaml` mirrors all six actors and parameters in the B2 fixed
demo. With the validated B2 environment it must yield **23 events** and
`7aad5e9c764012e3`. `lab/scenes/example.yaml` uses a nonzero starting tick,
different phases, moving Class B/SAR actors and a stationary AtoN. Compare the
reference digests after building the runner:

```powershell
.\lab\build\Release\aismixer_lab_scene.exe --scene lab/scenes/demo.yaml
.\lab\build\Release\aismixer_lab_scene_smoke.exe --demo
.\lab\build\Release\aismixer_lab_gpu_smoke.exe --seed 42 --count 1025
```

Both scene commands must report `7aad5e9c764012e3`; B1 must retain
`d3901485badba404`. The runner digest reuses B2's exact 36-byte little-endian FNV
encoding, without object padding or a changed PRNG key.

### B2.1 checks

The native `aismixer_lab_scene_host_test` CTest covers strict integer/bounds
validation, independent digest oracles, bounded summaries, locale-independent
CSV and exclusive Unicode file export. It runs without YAML or CUDA. With the
runner enabled and a local Python 3 interpreter available, additional CTests
exercise no-GPU CLI/YAML validation, actual GPU CSV/seed/range/export behavior,
and scripted interactive load/edit/run/export/EOF. Python is only the standard
library acceptance harness; the application does not launch Python or a shell.
Test artifacts stay in unique ignored build subdirectories.

```powershell
ctest --test-dir lab/build -C Release -L host --output-on-failure
ctest --test-dir lab/build -C Release -R 'aismixer_lab_scene_cli_' --output-on-failure
ctest --test-dir lab/build -C Release --output-on-failure
```

If yaml-cpp is absent, runner compilation and all runner functional/interactive
checks are **NOT RUN**, even when the independent host and B1/B2 GPU tests pass.
A successful helper build or static review does not demonstrate YAML parsing or
operator-app GPU execution. Report each category of check separately.

## B3: explicit AIS model and host-only NMEA encoder

The codec constructs AIS payloads from explicit message objects and encapsulates
them as `!AIVDM`. It has no dependency on CUDA, yaml-cpp or the scene engine.
`actor_id` is not MMSI, local `x_mm/y_mm` are not geographic coordinates, and
`random_u32` supplies no AIS field. The B2/B2.1 API, menus, schema version 1 and
six-category scenes are unchanged. There is no `--format nmea` scene-runner mode.

### API boundary and supported families

All codec headers live in `ais/` and use `aismixer::lab::ais`:

| Component | Responsibility |
| --- | --- |
| `ais_message.h` | Explicit `Mmsi`, `RadioState19/20`, typed `Type1/4/9/18/21` and the closed `Message` variant. |
| `ais_bits.h/.cpp` | Bounded MSB-first unsigned/signed field serialization, widths 1 through 64. |
| `ais_encoder.h/.cpp` | Complete input validation, exact payload bits and six-bit armoring. |
| `nmea0183.h/.cpp` | Channel/sequence validation, checksums, fragmentation and stream writing. |
| `ais_smoke_main.cpp` | CPU diagnostic application with explicit synthetic message objects. |

`encode(const Message&)` and the typed overloads return `EncodedPayload` with
the unpadded 0/1 bit vector, armored payload and fill count. `encode_aivdm` or
`format_aivdm` returns every ordered sentence for one message. `write_sentences`
validates the complete vector before writing to a caller-selected `std::ostream`.
Invalid fields throw `std::invalid_argument` naming the field. No output is
written by encoding itself and no input is clamped or automatically populated.

| Type | Explicit input | Payload bits |
| --- | --- | --- |
| 1 | Class A position report, without Type 2/3 scheduling variations. | 168 |
| 18 | Standard Class B report, with explicit SO/CS flags and state. | 168 |
| 4 | Base station geographic position and supplied/unavailable UTC. | 168 |
| 9 | SAR **aircraft** report, not a generic SAR actor mapping. | 168 |
| 21 | Aids-to-navigation base report; no optional name extension. | 272 |

No generic Emergency actor is encoded automatically. AIS-SART, MOB and EPIRB
profiles/identities require separate explicit future specifications. Type 1
status 14 can be supplied as a legal wire value but does not make this codec an
AIS-SART simulator. Types 2/3, 5/6/8/12/14/19/24 and binary application protocols
are not implemented.

### Wire units, markers and validation

MMSI must be exactly nine ASCII decimal digits. Leading zeros are retained by
the identity object; the numeric value is checked against the 30-bit field.
`000000000` is rejected by this codec's identity policy. Nine digits can be at
most `999999999`, already below the 30-bit ceiling; larger representations are
rejected before narrowing. No prefix/allocation validity is inferred.

Model numeric fields use `int64_t` so negative or excessive unsigned-field
inputs remain visible to validation. Boolean fields represent only their two
wire states. The core wire path needs no floating-point conversion:

| Field | Units, admitted values and unavailable/default marker |
| --- | --- |
| Longitude / latitude | Signed 1/10000 minute; limits ±108000000 / ±54000000. Only positive 108600000 / 54600000 are unavailable markers. |
| Speed, Types 1/18 | Tenths of a knot: 0..1022; 1022 is the saturation code, 1023 unavailable. |
| Speed, Type 9 | **Whole knots**: 0..1022, 1022 saturated, 1023 unavailable. |
| Course | Tenths of a degree, 0..3599; 3600 unavailable; 3601..4095 rejected. |
| True heading | Whole degrees, 0..359; 511 unavailable; 360..510 rejected. |
| Type 1 ROT | Signed eight-bit AIS nonlinear code -128..127; -128 unavailable; ±127 special turn indicators. No deg/min converter. |
| Type 9 altitude | Metres, 0..4094; 4094 saturated, 4095 unavailable; explicit GNSS/barometric flag. |
| Position-report second | 0..59 UTC; 60 unavailable, 61 manual input, 62 estimated position, 63 positioning system inoperative. CS Type 18 allows only 0..60. |
| Type 4 UTC | Year 0..9999, month 0..12, day 0..31; zero means unavailable. Hour 24, minute/second 60 unavailable; impossible known dates rejected, with Gregorian leap-year checks. |
| Repeat / navigation | Repeat 0..3. Type 1 navigation 0..8,11,12,14,15; 9/10/13 reserved and rejected. Status 15 is undefined. |
| Position fixing | Explicit EPFS code 0..9 or 12..15; 10/11 reserved. Default 0 undefined; 15 internal GNSS. |
| Type 21 dimensions/status | A/B 0..511 m; C/D 0..63 m; regional/application status raw 0..255. |

Defaults use unavailable position/motion/time markers, repeat 0, undefined
navigation/fixing codes and zero flags; Type 9 DTE defaults to not ready. Date
and communication state never come from the wall clock. Constructors for Types
1/4 require explicit `RadioState19`, and Types 9/18 require `RadioState20`;
these classes have no default state. The latter's high bit is the selector and
low 19 bits are the state. Raw states are width-checked; slot/time interpretation
and operational TDMA validity remain the caller's responsibility. CS Type 18
additionally requires repeat 0 and combined state `0xE0006` (selector 1 plus the
standard fixed 19-bit pattern). No live communication state is manufactured.

Type 21 name is 0..20 characters from ASCII 0x20..0x5F, with literal `@`
rejected because it is the text terminator. There is no case conversion or
Unicode normalization. Short names are padded with `@` codes to exactly 120
bits, independently of NMEA armoring. Empty name is permitted. Virtual aids and
reference points require all dimensions zero. Physical dimension orientation,
floating-aid geometry and regional/IALA status profiles are not synthesized or
qualified. The explicit off-position flag is preserved; a receiver considers
it meaningful only when the timestamp is 0..59.

### Wire framing, bounds and deterministic examples

Every spare/reserved bit in supported layouts is written as zero. Signed values
use defined two's-complement bit serialization; object layout, endian order and
padding are never dumped. Armoring maps six-bit values 0..39 to ASCII 48..87
and 40..63 to ASCII 96..119. Final zero fill is `(6 - bit_count % 6) % 6`, not
AIS radio byte padding. Fixed reports have fill 0; the 272-bit Type 21 has fill 4.

Channel A/B is explicit and asserts no RF transmission. Single-fragment output
has total/part 1 and a blank sequence field. Fragment capacity is derived from
the actual header, fill and checksum/CRLF overhead, with a maximum of **82 bytes
including CRLF**. Multipart output requires a caller-supplied decimal ID 0..9,
uses at most nine fragments, and places nonzero fill only on the final fragment.
There are no global sequence counters. Bit/armor helpers cap input at 4096 bits;
NMEA rejects inputs requiring more than nine fragments. Under these headers the
single capacity is 61 armor characters and multipart capacity is 60; these are
derived results, not fixed slicing constants. No TAG blocks or AIVDO are added.

The five diagnostic vectors are synthetic, independently checked examples with
explicit identities, coordinates, flags, time and communication-state values:

```text
Type 1:  !AIVDM,1,1,,A,1Amg=5EmQso>f;LFGd5DlSoo7D<Q,0*2B
Type 4:  !AIVDM,1,1,,A,4P1;FQivPfosrk81`0kOqP?P2SAF,0*26
Type 9:  !AIVDM,1,1,,A,9ib4N?DlU1dovH1<P6P>3w`2>ckN,0*43
Type 18: !AIVDM,1,1,,A,B>eq`d@3wk?8mP=18D3Q3wvCuD<Q,0*20
Type 21: !AIVDM,1,1,,A,ENk`A<S60Q@64ST:@HHP0000000Owwwwwwwwh91AISuJFP,4*59
```

The displayed lines omit their literal CRLF. Wire lengths are 49 bytes for
Types 1/4/9/18 and 67 for Type 21. The AtoN example is a physical light with
sectors, and the aircraft example explicitly selects barometric altitude.
None of the example metadata is borrowed from scene actors.

### Existing preset build and CPU checks

From the repository root, use the existing ignored preset without editing it:

```powershell
cmake --preset cuda129-pascal -S lab
cmake --build lab/build/vscode-cuda129-preset --config Release
.\lab\build\vscode-cuda129-preset\Release\aismixer_lab_ais_smoke.exe --help
.\lab\build\vscode-cuda129-preset\Release\aismixer_lab_ais_smoke.exe --demo
.\lab\build\vscode-cuda129-preset\Release\aismixer_lab_ais_smoke.exe --self-test
ctest --test-dir lab/build/vscode-cuda129-preset -C Release --output-on-failure
$env:CUDA_VISIBLE_DEVICES = '-1'
ctest --test-dir lab/build/vscode-cuda129-preset -C Release -L AIS -LE GPU --output-on-failure
Remove-Item Env:CUDA_VISIBLE_DEVICES
```

The diagnostic defaults to `--demo`; options are mutually exclusive. Exit 2
means invalid CLI; exit 1 means codec/validation/I/O failure. Each demo header
reports type, exact bit count, armor and fill, then the complete sentence bytes.
Stdout uses binary mode on Windows to preserve CRLF. Library callers must also
use binary streams on translating hosts, e.g. `std::ofstream(path,
std::ios::binary)`; arbitrary ostream failures cannot roll back bytes already
written. There is no socket, file export or YAML input in this diagnostic.

`aismixer_lab_ais_codec_test` checks independent full bit vectors/field offsets,
individual flags, all sentinels, field limits, dates/text, every armor value and
fill count, fragmentation/reconstruction and prevalidation before output.
`aismixer_lab_ais_wire_oracle_test` uses only Python's standard library to decode
actual smoke bytes independently, recompute XOR, verify exact lengths/CRLF,
and check repeat/execution-order stability. The B3 host AIS CTests set the
GPU-hidden environment themselves. The B3.1 host output test also has the `AIS`
label. Its geographic wire oracle and CLI acceptance tests require actual CUDA
execution (`GPU;geo;AIS`); `-LE GPU` excludes both from the GPU-hidden host
recipe. The native B3 host targets link only the standard C++ runtime
and this codec. The encompassing existing project still requires Windows
VS2022/CUDA 12.9 configuration; Linux portability is not qualified by B3.

### References, provenance and limits of the claims

Payload definitions were checked against [ITU-R M.1371-6 (02/2026)](https://www.itu.int/dms_pubrec/itu-r/rec/m/R-REC-M.1371-6-202602-I!!PDF-E.pdf),
Annex 7 Tables 46 (Type 1), 49 (Type 4), 57 (Type 9), 68 (Type 18) and 71/72
(Type 21). The diagnostic vectors/oracles were synthesized independently from
those facts. M.1371-6 assigns transmit-power bits at Type 1 offset 147 and Type
18 offset 139, and updates EPFS/aid codes. These definitions take precedence
over older summaries that label those bits spare or regional reserved.

[GPSD AIVDM/AIVDO protocol decoding v1.58, 24 June 2023](https://gpsd.gitlab.io/gpsd/AIVDM.html)
was crosschecked for armoring, sentence framing and a published Type 1 data
fixture. It is an informative implementation reference, not a normative AIS
standard. Its armoring table is followed; prose ASCII-number typos are not.
Only the short fixture sentence is reused, with provenance and upstream BSD
notice in [AIS_FIXTURE_LICENSE.txt](lab/tests/AIS_FIXTURE_LICENSE.txt); no GPSD or
GPL decoder code is copied. [IEC 61162-1:2024 edition 6 catalog](https://webstore.iec.ch/en/publication/72729)
was reviewed for scope/version; full paid VDM clauses were not available for
inspection. This work is not a claim of formal ITU/IEC equipment certification.

Regional manoeuvre code 3 and raw AtoN status are permitted explicit values
without regional profile interpretation. Radio state internals, physical aid
geometry/orientation, identity allocation and regulatory report scheduling need
separate profiles/qualification. Older downstream decoders may not interpret
new M.1371-6 flag/code meanings. The standalone B3 codec has no scene adapter;
B3.1's separate geographic path below adds an explicit Class A/B adapter.
Existing B2 local coordinates and scene schema version 1 retain their meaning.
RF/TDMA, network dispatch, collection and production-process orchestration
remain future work.

## B3.1: GPU-first geospatial foundation

B3.1 provides a separate CUDA-native geographic path; the original B2 API and
the standalone B3 codec described above retain their contracts. CUDA performs
Philox placement, absolute-tick motion/report scheduling, exact polygon tests
and stable CUB selection. Only accepted records and small counters cross back
to the host. The offline bridge explicitly maps Class A/B to Types 1/18 with a
deterministically generated synthetic MMSI catalog and deliberately supplied
test radio states. The CLI regenerates population-rank identities per run; it
does not persist a catalog. Changing population membership can change ranks.

The replaceable [Black Sea demonstration profile](lab/geo/profiles/black_sea_demo.yaml)
uses coarse Wikidata settlement positions with [recorded provenance and precision
limits](lab/geo/profiles/PROVENANCE.md), not a coastline/water mask or navigational
chart. Geographic anchors refer to actor tick zero, independently
of initial B2 local x/y. The WGS84 tangent approximation is limited to 1 km
radial displacement and +/-70-degree latitude. Boundaries are inclusive;
outside scheduled reports are counted as rejected. Passive receiver catalogs
are configuration only. No RF, TDMA, live playback or network dispatch exists.

The YAML operator is opt-in with a locally provisioned yaml-cpp >=0.8. The
core geographic libraries/tests add no yaml-cpp requirement to B1/B2:

```powershell
cmake --preset cuda129-pascal -S lab -DAISMIXER_LAB_BUILD_GEO_RUNNER=ON
cmake --build lab/build/vscode-cuda129-preset --config Release
ctest --test-dir lab/build/vscode-cuda129-preset -C Release --output-on-failure
```

The ignored local preset is optional. For a fresh external build directory,
use the same documented VS2022 x64/CUDA 12.9 setup and add
`-DAISMIXER_LAB_BUILD_GEO_RUNNER=ON`; supply the existing local yaml-cpp package
or toolchain. Use that same directory for build, tests and executable paths.

```powershell
# Binary CRLF AIVDM to an explicitly named offline file; replay metadata on stderr.
$geoOutputDir = Join-Path (Resolve-Path lab/build/vscode-cuda129-preset).Path 'offline-output'
New-Item -ItemType Directory -Path $geoOutputDir -Force | Out-Null
$geoDemoNmea = Join-Path $geoOutputDir ("demo-{0}.nmea" -f [guid]::NewGuid().ToString('N'))
lab/build/vscode-cuda129-preset/Release/aismixer_lab_geo.exe --demo --domain lab/geo/profiles/black_sea_demo.yaml --seed 42 --nmea $geoDemoNmea
# Fresh OS seed is printed; replay it later using --seed <recorded-value>.
$geoFreshNmea = Join-Path $geoOutputDir ("fresh-{0}.nmea" -f [guid]::NewGuid().ToString('N'))
lab/build/vscode-cuda129-preset/Release/aismixer_lab_geo.exe --demo --domain lab/geo/profiles/black_sea_demo.yaml --fresh-seed --nmea $geoFreshNmea
# Existing schema-version-1 scene, separate tick-zero overlay, geographic CSV.
lab/build/vscode-cuda129-preset/Release/aismixer_lab_geo.exe --scene lab/scenes/demo.yaml --overlay lab/geo/profiles/scene_demo_overlay.yaml --domain lab/geo/profiles/black_sea_demo.yaml --receivers lab/geo/profiles/receivers_demo.yaml
# 1000 / 100000 / 1000000 candidates, CUDA stage and transfer measurements.
lab/build/vscode-cuda129-preset/Release/aismixer_lab_geo.exe --benchmark --domain lab/geo/profiles/black_sea_demo.yaml --seed 42
```

The output directory stays under ignored build output. Generate a new filename
for each export: existing destinations are refused. Publication uses a unique
same-directory temporary file and a no-replace final operation; handled failures
clean up the temporary file. Forced termination can leave a uniquely named
temporary file, and arbitrary stdout writes cannot be rolled back.

Simulation ticks determine physical motion, execution time measures calculation,
and future playback acceleration A will control delivery only. Future population
P and receiver multiplicity D define separate workload axes. Synthetic ingress
stress is distinct from physically constrained RF experiments. Seed/replay,
precision, memory/batch and future RF/network contracts are in
[GPU_FIRST_ARCHITECTURE.md](lab/docs/GPU_FIRST_ARCHITECTURE.md); actual qualification
and benchmark results are recorded in [B31_ACCEPTANCE.md](lab/docs/B31_ACCEPTANCE.md).

Recorded B3.1 acceptance on Windows / VS2022 / CUDA 12.9.86 / GTX 1050 Ti
`sm_61` initially passed **14/14 Release CTests**, including the historical nine,
with both YAML operators and Python enabled. Corrective closure passed
**16/16** with the same options, including permanent Unicode paths, checked
CSV output, no-clobber NMEA publication, mixed compaction, independent Philox
and profile-pin/alternate-profile checks. The independent ECEF/ENU oracle observed 0.245858 m maximum
discrepancy against the 0.30 m tested-envelope budget. This is predominantly
linear-tangent versus ECEF/ENU model discrepancy, plus rounding, rather than
observed GPU numerical error. Qualified builds use CUDA `--fmad=false`; floating
point results/digests are not promised across arbitrary flags or toolchains.
The million-candidate
benchmark accepted 375000 reports, transferred 24 MB of compact records and
measured 51.8214 ms median steady wall time (three runs after warmup). These
are measurements for that configuration, without a CPU speedup claim.

## R0: recorded runtime foundation (opt-in)

R0 adds exact simulation time, causal event ordering, a bounded portable journal,
per-receiver recorded NMEA playback and output contracts. Its separate operator
is enabled with `-DAISMIXER_LAB_BUILD_RUNTIME_RUNNER=ON`. Only an explicitly
labeled synthetic infrastructure fixture is generated; AIS radio/TDMA,
cylinder placement and UDP remain future work. The test-only CUDA bridge checks
real transfer/compaction without calculating RF outcomes. Build commands,
journal wire format, CLI examples and resource limits are in
[R0_RUNTIME_FOUNDATION.md](lab/docs/R0_RUNTIME_FOUNDATION.md).
