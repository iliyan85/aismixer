# B3.1 qualified acceptance record

Initial acceptance was recorded **2026-10-14**, branch `lab`, starting HEAD
`91eb429` (`lab: polish root README and documentation references`), with a clean
starting tree. Corrective closure on **2026-10-15** preserves that uncommitted
B3.1 work. No commit, push, merge, rebase, cherry-pick or branch synchronization
was performed. Historical measurements below remain attributed to their run.

## Environment and build

Windows 11, NVIDIA GeForce GTX 1050 Ti, compute capability 6.1, 4 GiB VRAM,
driver 582.78 (driver API 13000), CUDA Toolkit/nvcc **12.9.86** (runtime API
12090), VS2022/MSVC **19.44.35229.0**, MSBuild 17.14.60, Windows SDK 10.0.26100.0.
Python 3.14 and local yaml-cpp were already installed; no dependency or global
toolchain was installed or changed. The original Codex run did not record its
yaml-cpp version. The independent Claude recheck recorded **0.9.0**, and the
Codex corrective closure separately verified **0.9.0** from the installed CMake
package-version file. Minimum supported yaml-cpp remains **>=0.8**.

An external fresh build tree preserved the ignored private preset and existing
repository build artifacts. CMake/CTest were invoked from VS2022's embedded
`Common7/IDE/CommonExtensions/Microsoft/CMake/CMake/bin` directory. Equivalent
commands with that directory on PATH:

```powershell
$b31Build = 'C:/Users/iliya/AppData/Local/Temp/aismixer-b31-20261014'
cmake -S lab -B $b31Build -G 'Visual Studio 17 2022' -A x64 -T 'cuda=C:/Program Files/NVIDIA GPU Computing Toolkit/CUDA/v12.9' -DCMAKE_CUDA_ARCHITECTURES=61 -DCMAKE_TOOLCHAIN_FILE=C:/Users/iliya/Tools/vcpkg/scripts/buildsystems/vcpkg.cmake -DVCPKG_TARGET_TRIPLET=x64-windows -DAISMIXER_LAB_BUILD_SCENE_RUNNER=ON -DAISMIXER_LAB_BUILD_GEO_RUNNER=ON -DBUILD_TESTING=ON
cmake --build $b31Build --config Release --parallel 4
ctest --test-dir $b31Build -C Release --output-on-failure -V
& "$b31Build/Release/aismixer_lab_geo.exe" --benchmark --domain lab/geo/profiles/black_sea_demo.yaml --seed 42
```

Initial acceptance passed **14/14 Release CTests**: the historical nine plus
five geography tests. CUDA tests executed actual device kernels. The original
GPU-hidden test required a sufficiently descriptive runtime error; its observed
message was `cudaGetDeviceCount: cudaErrorNoDevice`, but it did not exclusively
assert that error category. Corrective closure strengthens that test to the
explicit no-device signatures. The test is labeled host and never represents
successful simulation without a GPU. Current suite results are recorded in the
corrective-closure section below.

Build warnings were CUDA's Pascal deprecation notice and MSBuild MSB8029 about
temporary-directory incremental builds. Fresh and incremental Release builds
succeeded. A future persistent operator build should use a dedicated external
non-temporary directory. Linux, alternative compilers/GPUs and arbitrary
cross-platform bit-identical trigonometry are **NOT RUN / not qualified**.

## Independent correctness and regressions

| Evidence | Result |
| --- | --- |
| B1 seed 42 / count 1025 | `d3901485badba404`, unchanged |
| Original B2 fixed demo | 23 events, `7aad5e9c764012e3`, unchanged |
| B2.1 YAML CLI and interactive acceptance | Existing host, GPU and interactive tests passed |
| B3 native and independent Python wire oracle | All five golden Types 1/4/9/18/21, GPSD fixture, framing and 330 invalid cases passed unchanged |
| Geo host tests | 96 invalid cases; pure validation, fresh seed, units, identity/time/forbidden-category bridge checks |
| Geo YAML tests | 37 invalid cases plus profile/overlay/receiver parsing, UTF-8, named axes, schema/size/alias/tag checks |
| Geo CUDA tests | 16 invalid/unsupported cases, including actual bounded placement exhaustion counted once |
| Independent geographic oracle | ECEF/ENU inversion comparison; maximum observed horizontal model discrepancy plus rounding **0.245858 m**, within 0.30 m test budget |
| Device scheduling/selection versus original B2 | 48 candidates / 23 accepted; every original field and diagnostic random word matched |
| Reproducibility | Actor/anchor reorder, 64/128/256-thread shapes, contiguous/split ranges, smaller reusable capacity, fixed/different/fresh replay |
| Geometry | Independent winding classification, inclusive vertices/edges, diagonal +/-1 nanodegree cases, expected exits, all accepted/all rejected |
| Maximum CUDA test | 1000000 accepted events; 64000000 record bytes D2H; 129943347 owned device-buffer bytes |
| Device absence | Explicit error, including no CPU fallback |
| Geo independent wire oracle | GPU placement -> explicit-anchor CSV replay -> independent 60-byte FNV; every Type 1/18 field, XOR, 49-byte CRLF framing, identity/time metadata and offline file behavior |

The ECEF oracle tests 120 compass-direction cases across equator, both
hemispheres and high latitudes, plus inward/outward cases at exactly +/-70
degrees and longitude limits. This is measured qualification over tested cases,
not exact-geodesic or navigation certification. The 0.245858 m discrepancy is
predominantly first-order linear-tangent versus ECEF/ENU model difference, not
observed GPU numerical error; nanodegree rounding contributes at most roughly
0.112 mm per coordinate. Geographic builds use CUDA `--fmad=false`, a relevant
part of the qualified floating-point and digest-replay contract. Other compiler
flags/toolchains require renewed qualification. Polygon source quality remains
separate: [PROVENANCE.md](../geo/profiles/PROVENANCE.md) documents representative
Wikidata settlement points and varying source precision.

`dumpbin /DEPENDENTS` confirmed the new host test and original AIS test have no
CUDA or yaml-cpp imports. The core geographic path adds no YAML dependency to
B1/B2; the optional configuration/operator targets use local yaml-cpp. All
original B1/B2/B2.1/B3 source files and fixture-license contents remain unchanged.

## Final profile and offline example

Profile `black_sea_illustrative_wikidata_v1`, FNV **`9ab63fc6b9e76951`**, has the
required 16-landmark order plus explicit Durankulak closure. Values are rounded
to 0.01 degree from pinned P625 records retrieved 2026-10-14. This is a coarse
illustrative straight-edged domain, not a coastline or water-only mask.

Fixed seed 42, CUDA placement, two actors (701 Class A and 902 Class B), ticks
0..3, tick seconds 1 produced 8 accepted records and 392 NMEA bytes. Qualified
result FNV: **`48dec953d3129a2d`**. The first tick's records are:

```text
!AIVDM,1,1,,A,11OGQ0OP?w2qhF6GsOLf4?wp0000,0*1E
!AIVDM,1,1,,A,B1OGQ0P3whW39OVEOHSQ3wv00000,0*46
```

The displayed lines omit the literal terminal CRLF; emitted records include it.
MMSIs are explicitly cataloged synthetic `100000001` and `100000002`, not actor
IDs and not an officially allocated testing prefix. Radio states are deliberate
raw zero test values. Speed/course/heading/ROT/navigation/UTC unavailable codes
and false accuracy/RAIM are preserved. BaseStation/AtoN, generic SAR and generic
Emergency fail AIS bridge requests; geography can still emit their CSV events.
Receiver catalogs perform no propagation. No network/RF transmission occurs.
The CLI generates the rank-based catalog for the current population each run;
it does not persist or reload it. Changing population membership can change
actor ranks/MMSIs. Explicit catalogs are supported by the library bridge.

## Measured GPU benchmark

Same final profile, seed 42, block size 256, tick seconds 1. Each population is
half Class A / half Class B, intervals 2/4 with ID-associated phases, velocity
+100/-50 mm/s east/north. This illustrates compaction of scheduled reports;
these particular benchmark samples have zero polygon exits. Separate geometry
tests exercise partial/all rejection. Placement uses uniform discrete lon/lat
bounding-box sampling, not uniform Earth-surface area.

One kernel warmup precedes three identical steady runs. The table reports the
median selected by **steady run wall**, not setup-plus-run wall. Setup is
measured once per session; the first includes process-first CUDA initialization,
unlike later sessions. Times are milliseconds. **Setup wall** covers Session
construction; **steady wall** covers one reused-buffer run, including host
validation and accepted-vector allocation. **Setup + selected steady wall** is
their arithmetic sum, after configuration load; it excludes warmup, discarded
repetitions, report/digest formatting and the separately timed AIS sample.
CUDA events time device stage/transfer intervals. H2D/D2H intervals use pageable
host memory and include staging/enqueue gaps, not isolated PCIe DMA.
Synchronization makes results/timings ready; its CPU wait belongs to wall time.
Shared desktop/device load can vary.

| Measurement | Small | Medium | Million |
| --- | ---: | ---: | ---: |
| Population P (A/B) | 100 (50/50) | 1000 (500/500) | 10000 (5000/5000) |
| Absolute ticks | 0..9 | 0..99 | 0..99 |
| Duration (simulation seconds) | 10 | 100 | 100 |
| Actor-tick candidates | 1000 | 100000 | 1000000 |
| Scheduled / accepted / rejected | 375 / 375 / 0 | 37500 / 37500 / 0 | 375000 / 375000 / 0 |
| Unscheduled | 625 | 62500 | 625000 |
| Setup wall | 126.9601 | 1.9975 | 11.8420 |
| Host-to-device (setup) | 0.1300 | 0.1466 | 1.0977 |
| Device placement (setup) | 0.3109 | 0.1773 | 0.4628 |
| Setup diagnostic D2H | 0.0931 | 0.0492 | 0.0947 |
| Motion / schedule / transform | 0.0806 | 0.3667 | 3.5691 |
| Device polygon predicate | 0.0205 | 0.3635 | 3.4673 |
| Stable CUB selection | 0.0543 | 0.1423 | 1.2902 |
| Steady D2H (records + counters) | 0.2799 | 3.1402 | 29.8660 |
| Steady end-to-end wall | 0.7043 | 5.1156 | 51.8214 |
| Setup + selected steady wall | 127.6644 | 7.1131 | 63.6634 |
| Effective candidates/second | 1419849 | 19548049 | 19297047 |
| H2D bytes (once) | 5872 | 56272 | 560272 |
| Potential uncompacted record bytes | 64000 | 6400000 | 64000000 |
| Actual accepted D2H record bytes | 24000 | 2400000 | 24000000 |
| Small diagnostics D2H bytes (setup + run) | 68 | 68 | 68 |
| Requested device-buffer bytes including CUB scratch | 138875 | 12995219 | 129943539 |
| CPU AIS sample events | 375 | 1000 | 1000 |
| CPU AIS sample encoding wall | 1.4620 | 4.5233 | 7.3159 |
| Result FNV | `6333b11261924021` | `bb481b4d26d7253d` | `fe596df262ce14fc` |

The million case requests approximately 123.9 MiB of device buffers, before
allocator rounding and CUDA context/event/library allocations. D2H transfers 24 MB of accepted records
instead of 64 MB of all candidate records, plus 68 diagnostic bytes. Both byte
units are decimal. Large-run D2H and host work materially exceed device motion
time; they are reported separately. CPU AIS sample time includes identity-catalog
validation and encoding, not all 375000 accepted messages. No CPU simulation
baseline, GPU speedup ratio or pass/fail performance threshold is claimed.

Raw local diagnostic artifacts are in the external build directory:
`b31-benchmark.txt`, `b31-demo.nmea`, `b31-demo-metadata.txt`, and CTest's
`Testing/Temporary/LastTest.log`. They are not committed test fixtures and the
private preset/existing repository build tree were not used for output.

## Corrective closure: 2026-10-15

Closure uses the fresh ignored `lab/build/codex-b31-closure` Release tree with
both YAML runners and `BUILD_TESTING=ON`, the existing VS2022 x64/CUDA12.9
toolchain, `sm_61` and installed yaml-cpp **0.9.0**. The prior external Codex
build, ignored Claude audit, private preset and original B1/B2/B2.1/B3 sources
are preserved. No GPU algorithm or qualified codec/RNG behavior is changed.
The original 14 test names are retained; native output and CLI acceptance
registrations extend the configured suite to 16 tests.

The fresh Release build passed; the full configured suite passed **16/16
CTests in 12.33 seconds**, with actual GTX1050Ti/sm6.1, runtime12090/driver13000
reported by device execution. Both runners and Python acceptance were enabled.
Historical B1 digest `d3901485badba404`, B2's 23 events/digest
`7aad5e9c764012e3`, all five B3 golden vectors and the B3.1 eight-record/
392-byte demo digest `48dec953d3129a2d` matched. CTest label inventories were
actually checked: `-N -L AIS` lists six tests; adding `-LE GPU` lists four host
tests (B3 codec, smoke, wire oracle and new geographic output). The corrected
GPU-hidden host recipe passed **4/4**:

```powershell
$closureBuild = 'lab/build/codex-b31-closure'
cmake --build $closureBuild --config Release --parallel 4
ctest --test-dir $closureBuild -C Release --output-on-failure
ctest --test-dir $closureBuild -C Release -N -L AIS
ctest --test-dir $closureBuild -C Release -N -L AIS -LE GPU
$env:CUDA_VISIBLE_DEVICES = '-1'
ctest --test-dir $closureBuild -C Release -L AIS -LE GPU --output-on-failure
Remove-Item Env:CUDA_VISIBLE_DEVICES
```

`GPU;geo;AIS` wire/CLI tests are excluded from that host recipe because they
execute genuine CUDA placement and motion. The wire oracle reported
`PASS qualified golden 48dec953d3129a2d` using independently read CMake
CUDA12.9.86/MSVC19.44.35229.0/`--fmad=false` evidence and a direct driver sm6.1
query. It also permanently verified that changed coordinates at the shipped
profile path fail its LF-normalized SHA-256 pin and that a legitimate alternate
domain passes generic checks with `qualified golden SKIPPED: non-reference
profile`. The alternate tiny rectangle deliberately rejects later demo events,
so the generic path also verifies that eight accepted events are not assumed.
The shipped profile FNV remains `9ab63fc6b9e76951`; fixed demo bytes remain
392 bytes/eight 49-byte CRLF sentences.

| Permanent closure evidence | Observed result |
| --- | --- |
| Host validation | 100 invalid cases, including actual closure/T-junction/repeated-vertex/overlap diagnostics; three portable Philox block known answers and five qualified scalar vectors |
| CUDA mixed compaction | 5000 actors x 200 ticks = 1000000 candidates; 520834 scheduled, 330521 accepted, 190313 rejected, 479166 unscheduled; 2812 boundary crossers |
| Mixed ordering/replay | Independent full-record and polygon checks, canonical tick/actor order, repeat and 73/127 split-range equality; digest `ac9b635e46cefd1a` |
| Mixed compact transfer | 21153344 accepted-record bytes instead of 64000000 candidate-record bytes, plus small diagnostics |
| Independent CUDA Philox comparison | 18 pinned placement anchors, five rejected placement proposals, 108 B2 diagnostic words and 25 semantic-purpose words matched the integer-only reference |
| CUDA invalid/unsupported cases | 16 total, including bounded placement exhaustion once; explicit no-device signatures required |
| YAML validation | Existing 37 invalid cases passed; no parser rewrite |
| Native publication and CLI | Both new CTests passed; original 14 names retained |

The permanent mixed fixture is independently constructed and differs from the
separate CLI performance comparison below; their accepted counts/digests must
not be interchanged. The integer-only Philox reference documents key/counter/
subsequence/lane assumptions in `lab/tests/philox_reference.h`. Portable block
algorithm facts are distinguished from the qualified CUDA12.9 cuRAND adapter;
no test reads ignored audit files or network data.

Permanent CLI/native-output tests cover Cyrillic domain directory/output names,
UTF-8 error diagnostics, exact successful bytes, CSV reader closure after 2048
bytes, existing/intervening destination preservation and write/flush/close/
publication failure cleanup via explicit test hooks. No disk exhaustion,
desktop GPU reset or other dangerous verification was attempted. A many-actor
test checks 2048 reversed actors and 6144 canonical metadata/NMEA records.
The CLI uses Windows wide argv conversion, checked CSV/NMEA flushes and unique
same-directory exclusive temporary output with no-replace publication.
Forced process termination may leave a unique temporary file; this is not a
claim of atomic stdout or crash-proof cleanup.

### Measured CLI correction on one identical workload

The original and corrected executables each ran the same 5000-actor x 200-tick
fixture once, under the same qualified hardware. The ignored audit input files
were read only for this comparison; permanent tracked tests construct their
own fixtures and never depend on that directory. This is process wall time,
not a GPU-versus-CPU comparison or a brittle performance gate.

| Path | Before (seconds) | After (seconds) |
| --- | ---: | ---: |
| CSV-only complete process | 2.7582744 | 2.1500363 |
| NMEA export plus event diagnostics, complete process | 75.9252245 | 3.6987797 |

The workload had 1000000 candidates, 520834 scheduled reports, 333441 accepted,
187393 outside-domain rejections and 479166 unscheduled candidates. All 333441
`event=` metadata rows matched byte for byte. CSV SHA-256 remained
`51a6492371da5670d8b309c75f44dc68002671f78159a7560f3dce44730ae38d`;
NMEA SHA-256 remained
`e6e91b511f14e5333e04c754fb6e2ab2a52a887cbc614731feabbdc6acbbfedd`
over 16338609 bytes. An independent pass checked all 333441 XOR/CRLF records.

Diagnostic identity lookup changed from O(E x P) repeated linear searches to
O(E log P) binary lookup over the validated sorted catalog. Each metadata row
is buffered into one bounded write instead of repeated unit-buffered stderr
insertions. The observed process-time difference includes both improvements;
it must not be attributed solely to lookup or CUDA work. The corrected run
reported CPU AIS encoding **1238.0620 ms**, GPU Session setup wall **105.8835
ms**, steady run wall **42.2054 ms**, motion **2.8533 ms**, polygon predicate
**3.6465 ms**, CUB selection **1.5954 ms**, and D2H interval **26.4298 ms**.
These measurements cover distinct scopes and are not interchangeable totals.

## Scope, limitations and next milestone

Modified files: root `README.md` and `lab/CMakeLists.txt` only.

Added files:

- `lab/geo/geo_api.h`, `geo_host.cpp`, `geo_engine.cu`;
- `lab/geo/geo_config.h`, `geo_config.cpp`, `geo_cli_main.cpp`;
- `lab/geo/geo_ais.h`, `geo_ais.cpp`, `geo_output.h`, `geo_output.cpp`;
- `lab/geo/profiles/black_sea_demo.yaml`, `scene_demo_overlay.yaml`,
  `receivers_demo.yaml`, `PROVENANCE.md`;
- `lab/tests/geo_host_tests.cpp`, `geo_gpu_tests.cpp`, `geo_config_tests.cpp`,
  `geo_wire_oracle.py`, `geo_output_tests.cpp`, `geo_cli_closure_tests.py`,
  `philox_reference.h`;
- `lab/docs/GPU_FIRST_ARCHITECTURE.md` and this acceptance record.

Coordinate limits on scheduled reports and radial displacement limits on all
requested trajectory ticks, simple polygons, bounded sampling failure,
source accuracy, platform-qualified replay, synchronous session calls and
population-rank MMSI continuity are explicit limits. NMEA export now uses
no-clobber temporary/publication operations as well as early preflight. No
global toolchain/dependency/production or other-branch file changed.

[GPU_FIRST_ARCHITECTURE.md](GPU_FIRST_ARCHITECTURE.md) defines separate simulation,
execution and future delivery clocks and P/A/D axes. Physical RF constraints and
explicit synthetic ingress stress remain distinct. Correlated atmosphere/duct
fields, conservative long-range candidate screening, one immutable transmission
with per-receiver copies, independent dispatcher and independent collector are
interface contracts only. Recommend a separate RF milestone for conservative
transmission-receiver screening and an independently validated baseline path-loss
model, preserving correlated-field and height-datum interfaces. Survey-quality
water masks, exact/long-range geodesics, TDMA, live playback/networking, GUI,
Python control and Linux qualification are deliberately deferred.
Multi-GPU, page-locked D2H optimization and performance redesign remain deferred.
The yaml-cpp trailing null-document boundary edge is also deferred to a focused
parser task; existing loader behavior is preserved rather than broadly rewritten.
