# W1 world and lifecycle engine

Design frozen before implementation, 2026-10-18. Initial branch `lab`, clean
HEAD `1c1c5fff4d12a55e7cd25386ca1d0989d8451c11`; existing Release CTest
baseline **27/27 PASS** (117.28 s). This is a separate world-state schema,
not an extension of the runtime v1/StreamingV2 journal or a radio simulator.

## Source of truth and boundaries

The version-1 world configuration owns stable numeric typed vessel, equipment,
radio, waypoint and region identities, exact integer simulation time, and
independent navigation, power and environment schedules. An immutable validated
configuration creates a bounded monotone `Timeline`; snapshots are values, not
borrowed views. Sparse host transitions integrate rational piecewise-constant
distance. Production CUDA evaluates WGS84 geodesic positions in batches and
samples receiver circular footprints and elevation. No CPU production fallback
or test-only transfer kernel substitutes for that calculation.

| Subsystem | W1 policy |
| --- | --- |
| Clock | R0.1 SimulationInstant, 9830400 units/s; scenario starts at zero; all validity intervals [start,end) |
| Vessel | Exists independently of equipment power; current leg, speed/status and antenna mounting persist through OFF |
| Class A equipment | One coupled TX/RX power state; ON creates a new initialization requirement, never a slot map |
| Passive receiver | Fixed unqualified synthetic candidate; independent power; no transmitter or reach radius |
| Reach | Per-transmitter independent environment schedule continuing during OFF; radius is not power or reception proof |
| R0.1 | Reuse time/ownership contracts; no EventKind/wire change and no invented ReceptionSuccess or complete radio journal |
| Interruption | OFF effective exactly at T; any future burst spanning T requires an aborted-frame disposition in a future versioned radio integration; never forge TransmissionEnd |
| Heights | Preserve AMSL/ellipsoid/unresolved datum. No geoid data means no conversion; RF geometry query fails on unresolved height |

## Numerical and randomness contract

W1 uses a separately versioned semantic Philox4x32-10 adapter. The key is
`seed XOR entity_u64`; counter words are episode low/high, draw-block index,
and `(version << 24) | purpose`. All purpose values are explicit and disjoint.
Equipment IDs for vessels use positive u32 values; passive equipment uses
`(region_id << 32) | ordinal`, with positive region/ordinal, so these domains
cannot collide. Region position/elevation draws use region ID plus ordinal
episode. Vessel selection/speed/status/reach use vessel or equipment identity
and their own episode counters. No global random iterator exists. Inclusive
integer draws use bounded 64-bit rejection; area samples use independent
midpoint uniforms `(word+0.5)/2^32`, `r=R*sqrt(U)` and bearing `360*V`.

The new bounded WGS84 Vincenty inverse/direct implementation must fail closed
on unsupported points, distance or nonconvergence; no spherical fallback.
World legs are limited to 1 m..2000 km and |latitude| <=80 degrees, independently
qualified against ellipsoid geodesic ODE reference calculations. This is not
B3.1's <=1 km tangent approximation. Each query starts at its fixed leg origin
and absolute accumulated distance; repeated short displacements are forbidden.
Distances for leg bookkeeping round to millimetres; rational submillimetre
progress is retained between queries and carried across leg boundaries.
Leg-length rounding contributes <=0.5 mm per leg; every new leg starts at the
exact catalogue endpoint rather than accumulating coordinate drift.
Coordinates round to nearest nanodegree, ties away from zero. Numerical
tolerances and actual host/device reference errors will be recorded below.

Nominal speed is positive integer milli-knots, with 1 mkn =463/900 mm/s.
Progress uses denominator `900*9830400`, checked u64 arithmetic and retained
remainder, so changing query/advance partitions cannot change travel distance.
Anchoring pauses progress without moving to a waypoint; resume retains the same
destination. Restricted motion uses a positive explicit cap. COG is unavailable
at SOG=0. Waypoint arrival is the first representable instant reaching the
quantized leg length; sub-unit overshoot carries into the next leg.

Same-time precedence: integrate old state up to T; process waypoint arrival,
then nominal speed, navigation status, reach, and equipment power transitions;
snapshot consumers see the resulting state effective at T. Equipment OFF
therefore applies at T even during a hypothetical burst. All repeating
durations are positive; only initial equipment delay may be zero. Independent
draw domains prevent this precedence from coupling random streams.

## Configuration and execution limits

Separate schema-1 world, waypoint catalogue and receiver-region YAML files
use strict bounded UTF-8 parsing, exact decimal units, explicit metadata and
unknown/duplicate-key rejection. Waypoints preserve catalogue identities;
only the exact duplicate closing vertex is removed. Catalogue points are not
harbors, water masks or safe routes. Region samples are not land-verified sites.
The engine bounds catalogues, work buffers, scenario duration and sparse
transition work; exceeding a bound is an error, never dropped state.

The CUDA session owns reusable bounded input/output buffers and region
descriptors; host state holds only current navigation/episode state. Snapshot
inputs are copied into batch buffers, outputs are values. No caller may mutate
an in-flight descriptor. Calls on a session/timeline are serialized. Future
resident vessel/antenna registries may upload versioned deltas without changing
the immutable TX-time snapshot contract. The current implementation must
report actual allocations, launches, transfer bytes and timing separately from
canonical data; diagnostics must never influence sampling or traces.

Implementation and measured acceptance evidence follow after qualification.

## API and independent equipment state

[world.h](../world/world.h) is the host source of truth; [rng.h](../world/rng.h)
freezes purpose values 1..16. The same Philox equations execute on CUDA for
receiver placement. Equipment online/offline episodes each count from zero;
initial delay is episode zero of its own purpose. Initial speed, status and
reach use episode zero. The first cycling destination uses the new leg index
one. Positive passive ordinals are 1..count and are also their placement draw
episode. Catalogue ordering never supplies a random key.

`canonical_config` returns a sorted validated copy. `Timeline` retains only
current leg/progress, independent episode counters/deadlines, power state and
fixed receiver samples. It advances monotonically within the configured
horizon and emits notices in time/kind/stable-ID order. A callback failure
poisons that timeline, since a consumer may already have observed a prefix;
there is no automatic replay or hidden retry. Invalid regressed queries fail
before advancement. `prepare()` returns a value snapshot of exact descriptors.
State intervals are half-open; queries and notices exactly at the configured
scenario endpoint are supported. The pending-transition query hides later
out-of-horizon deadlines.
The explicitly named `reference_snapshot` is for CUDA-free contract/reference
use; the production `WorldSession::snapshot` always calls the real CUDA engine.

```cpp
auto config = world::load_world(profile_path);
world::WorldSession session(config, {65536, 256});
session.advance(runtime::from_seconds(1000), consumer_notice_callback);
const auto state = session.snapshot();
const auto owners = world::receiver_ownership(state);
```

The session chunks vessel queries at its capacity. Its region descriptor count
and total sampled passive population must also fit that capacity, since region
sampling is one bounded call; smaller capacities do not imply automatic
receiver batching. The operator uses capacity 65536 and separately bounds each
position batch with `--batch-size`.

`resolve_seed` consumes a fresh-seed request exactly once; the CLI and production
session call it automatically. Lower-level Timeline, region-count and burst
inspection APIs reject an unresolved fresh request rather than using its zero
placeholder. The actual seed remains in the session's configuration.
The CLI records it in the trace; using that number with
`--seed` reproduces the data. No world clock uses wall time. Names such as
`world.station.101`, `world.radio.301` and `world.passive.11.1` preserve typed
ownership when constructing an immutable R0.1 `ReceiverRegistry` view. Its
enable bits describe that snapshot instant. An integration must establish
new power/registry epochs and handle interrupted frames before applying later
snapshots; existing R0.1 ownership tokens are not silently mutated or made live.
The aggregate Class A radio plus maximum passive population is <=4096, and
Class A stations <=1024, matching the current R0.1 catalog bounds.
Vessel and receiver state values carry their snapshot instant; RF-geometry and
ownership queries reject stale/mixed-time views. The snapshot/VesselId overload
binds geometry time directly to the containing snapshot.

With zero initial delay, ON is effective at t=0. A five-second ON interval is
[0,5); an OFF transition at 5 makes both Class A interfaces unavailable at 5.
`inspect_burst(config,equipment,4s,6s)` reports a permitted start and a power cut
at 5 requiring future abort handling. A burst ending exactly at 5 has no
interior power cut; a start at 5 is disallowed. These are state constraints,
not `runtime::Event` records. No `TransmissionEnd`, reception or NMEA is made.
Every Class A ON notice requests fresh protocol initialization; passive ON
notices never request AIS network entry. Position, nominal speed, status,
antenna offset and reach schedules are independent of those notices.

## YAML schema and operator usage

The dedicated sample is
[black_sea_world_v1.yaml](../world/profiles/black_sea_world_v1.yaml), with
[black_sea_waypoints_v1.yaml](../world/profiles/black_sea_waypoints_v1.yaml)
and [black_sea_regions_v1.yaml](../world/profiles/black_sea_regions_v1.yaml).
Their [provenance](../world/profiles/PROVENANCE.md) links the unchanged legacy
settlement catalogue. No new site measurements or geographic masks are claimed.

All three files require `schema_version: 1` and an explicit `kind`. The world
requires `profile_id`, `provenance`, `duration_seconds`, `seed`,
`waypoint_catalog`/`receiver_regions` references and `vessels`. References may
be absolute or resolved relative to the scenario. Each vessel
provides distinct `id`, `equipment_id`, `radio_id`, name, route, speed,
navigation, restricted-speed cap, power, antenna and reach models. Unknown or
duplicate keys fail. YAML anchors, aliases, explicit/custom typed tags, extra
documents, NUL and invalid UTF-8 fail before graph construction; decoded
scalar UTF-8 is also checked. Files are <=1 MiB, depth <=32, <=100000 nodes and
scalar length <=4096 bytes. There are no recursive includes or dependency
downloads; the operator uses the existing local yaml-cpp package.

| Configuration group | Exact units / rules |
| --- | --- |
| `seed` | `{mode: fixed, value: uint64}` or `{mode: fresh}` |
| `route.initial`, `first_destination` | `{mode: fixed, waypoint_id: ID}`, `{mode: all}`, or `{mode: subset, waypoint_ids: [...]}` |
| `route.subsequent_destination` | all/subset, >=2 distinct candidates; current endpoint excluded |
| `speed.knots`, restricted cap | Exact decimals, <=3 fractional digits -> integer milli-knots, 0.001..100 kn |
| Speed/status/reach hold durations | Inclusive whole-second intervals, strictly positive |
| Power | Inclusive `initial_delay_seconds` >=0; ON/OFF duration intervals >0 |
| Navigation | Unique enabled engine/anchor/restricted modes, positive integer weights and per-mode duration intervals |
| `antenna.height_above_waterline_m` | Fixed nonnegative offset, <=3 decimals -> mm; waterline separately unresolved or explicit ellipsoid assumption/provenance |
| Reach `radius_km` | Inclusive nonnegative range, <=6 decimals -> mm, <=2000 km; independent hold interval |
| Region center | WGS84 longitude/latitude integer nanodegrees |
| Region footprint | `radius_km` 0..100 km; sampled geodesic surface distance, no polygon clipping |
| Region count | Inclusive integer 1..4096, with aggregate radio cap |
| Region elevation | Exact metre decimals -> mm, 0..10000 m; explicit AMSL or WGS84_ellipsoid datum/provenance |

Exponents, NaN/Inf, excess precision, silent rounding and ambiguous unit
conversions are rejected. If the first destination is fixed, its ID is excluded
from the configured initial Fixed/All/Subset candidate pool before sampling.
An empty effective initial pool is rejected. Otherwise the existing per-vessel
`Purpose::InitialWaypoint`, episode0, selects uniformly from its sorted eligible
IDs, without retries, replacement or rewriting the configured selector. A
nonfixed first destination preserves the original initial pool and excludes
only the actually selected start from its own destination draw. All configured
IDs and duplicates are validated before filtering, and every eligible route
pair retains the existing geodesic qualification. Closing
waypoint ID/name/coordinates must repeat the first exactly; only that final
entry is removed. Remaining IDs, names and positions are distinct. Geodesics
for all eligible route pairs must pass the bounded solve before execution.

From the existing native VS2022/CUDA12.9 preset, with local yaml-cpp >=0.8:

```powershell
cmake --preset cuda129-pascal -S lab -B lab/build/codex-w1-world -DAISMIXER_LAB_BUILD_GEO_RUNNER=ON -DAISMIXER_LAB_BUILD_RUNTIME_RUNNER=ON -DAISMIXER_LAB_BUILD_WORLD=ON -DBUILD_TESTING=ON
cmake --build lab/build/codex-w1-world --config Release --parallel 4
lab/build/codex-w1-world/Release/aismixer_lab_world.exe --world lab/world/profiles/black_sea_world_v1.yaml --validate
lab/build/codex-w1-world/Release/aismixer_lab_world.exe --world lab/world/profiles/black_sea_world_v1.yaml --seed 42 --duration-seconds 18000 --step-seconds 60 --chunk-seconds 7 --batch-size 37 --block-size 64 --output lab/build/codex-w1-world/NEW-world.ndjson
```

Use the VS2022 embedded CMake/CTest executables if they are not on PATH; the
private preset/toolchain was not changed. A named trace uses an exclusive
same-directory temporary and no-replace publication only after successful
completion. Existing destinations/intervening competitors are preserved.
Forced termination may leave an owned temporary. Without `--output`, stdout
is binary UTF-8 NDJSON with checked writes/flush; stderr carries the mandatory
scope banner, device/build/seed details and measured metrics. No NMEA or sockets
exist in W1. `--validate` and help run without a visible CUDA device; production
evaluation fails clearly if the GPU is unavailable. Optional `--verbose` changes
diagnostics only.

Trace schema 1 has a manifest, sparse transition rows, sampled vessel/receiver
rows, and a mandatory `COMPLETE_WORLD_ONLY` final row. The manifest records
world/RNG/geodesic/model versions, actual seed, duration/sample interval,
LF-normalized source FNV hashes and provenance. Every numeric state value is
integer; unavailable COG/ellipsoid height is JSON null. Logical FNV-1a64 covers
every byte including LF before the final row; that final row carries the digest
and covered-record count. A missing final row is incomplete, not a radio journal.
FNV is a reproducibility fingerprint, not authentication. Source byte hashes
will change under YAML reorder, while existing entity-state rows remain stable.
`--step-seconds` chooses trace samples; `--chunk-seconds`, block shape, batch
capacity and verbosity must not affect data at a fixed sample interval.

Limits include 2..256 waypoints, 1..1024 vessels, <=64 regions, <=7 scenario
days, <=2000000 sparse transitions and <=5000000 sampled snapshot rows. The
CLI retains <=8192 pending trace rows and <=65536 query descriptors; each CUDA
session owns reusable bounded buffers. Active descriptor counts and owned
buffer bytes differ from process RSS: the operator also reports Windows peak
working set/pagefile use, which includes runtime/driver/library allocations.
CUDA events time H2D, position, sampling and D2H stages separately; pageable
copies/event intervals include staging and host enqueue gaps, not pure PCIe
bandwidth. Wall time includes host work, synchronization and trace I/O. No
performance threshold or GPU-over-CPU speedup is asserted.

## Qualified W1 semantics and measured acceptance (2026-10-18)

The production and independent W1 builds used Windows 11, VS2022 x64,
MSVC 19.44.35229.0, CUDA 12.9.86 (`--fmad=false`, no fast math), the actual
NVIDIA GeForce GTX 1050 Ti sm_61, 4 GiB VRAM, runtime API12090/driver API13000
and the existing local yaml-cpp. All new C++ targets keep `/utf-8` and `/W4`.
No packages, dependencies, private presets or toolchain files were changed.
Both build trees were newly configured for W1.

The source of the numerical method is Vincenty's full nested ellipsoidal
equations ([original paper hosted by NOAA/NGS](https://www.ngs.noaa.gov/PUBS_LIB/inverse.pdf)),
with WGS84 a/f from [NGA](https://earth-info.nga.mil/?action=wgs84&dir=wgs84).
The shared host/device solve is bounded to 128 iterations and rejects
nonconvergence/unsupported distances or points. There is no spherical fallback.
The independent test reference integrates the ellipsoid's geodesic differential
equations using meridional/prime-vertical curvature, RK4 and refinement. It does
not reuse Vincenty equations. Equator/meridian checks are separate. Tests and
reference vectors are local and require no network or external geodesy package.

| Numerical evidence | Observed result |
| --- | --- |
| Independent host cases | 119, including short/coincident/corner, dateline, high-latitude, long routes and explicit failures |
| Maximum host direct error | 0.000070304 m |
| Maximum inverse distance error | 0.000062000 m |
| Maximum independent reference refinement difference | 0.000001130 m |
| Declared reference tolerance | 0.002 m, including integer coordinate quantization |
| Actual CUDA reference position error | 0.000064226 m |
| Independent five-hour operator position error | 0.000064902 m |

For the independent multi-hour case, Varna (27.91E,43.21N) to illustrative
Istanbul (28.96E,41.01N) has reference distance **259332.189502357 m** and
initial course **160.081995740407 degrees**. Constant20kn for five hours travels
exactly **185200 m** despite equipment OFF periods; the reference endpoint is
28.667187719559E,41.639944356435N, course160.592865015522 degrees. The host gate
checks that long-voyage position, exact rational distance, power boundaries,
navigation continuity, anchor holds/resumption and cap-limited restricted motion.
These computational error results qualify the tested cases/toolchain, not a
navigation certificate or a proof of every possible ellipsoid input. Catalogue
precision and site/route suitability remain entirely separate from arithmetic.

The four host/native contract gates and two operator gates are:

| Exact CTest name | Labels | Executed coverage |
| --- | --- | --- |
| `aismixer_lab_world_host_test` | host;world | Independent Philox/inclusive draws, five-hour exact navigation/power/speed/status/reach, chunk/population independence, waypoint cycling, stale-time/ownership/fresh-seed guards; 53 invalid contracts |
| `aismixer_lab_world_geodesy_test` | host;world | Independent ellipsoid reference, 119 numerical cases and explicit supported-domain/nonconvergence checks |
| `aismixer_lab_world_config_test` | host;world | Strict YAML, exact units, closures/selections, Unicode, source hashes, datums, catalogue limits; 84 invalid cases |
| `aismixer_lab_world_gpu_test` | GPU;world | Actual production CUDA, 64/256 blocks, reordered/batched queries/regions, area/elevation oracle, wrapper power/ownership/fresh-seed replay and capacity-one vessel evaluation |
| `aismixer_lab_world_cli_host_test` | host;world | Help/strict validation without visible GPU, rejected invalid options and explicit unavailable-GPU production failure |
| `aismixer_lab_world_cli_gpu_test` | GPU;world;stress | Five-hour independent schedule/geodesic/PRNG/FNV oracle, chunk7/300, batch1/1024, block64/256, verbosity, fresh replay, Unicode, no-clobber and closed stdout |

The independent GPU unit workload evaluated **8195 position queries** and
**6146 candidate receiver samples** (a device-unit workload, not a world radio
catalogue). Its canonical field digest was **`dda107efc8701139`**. The observed
mean squared normalized sample radius was **0.49900343354**, against0.5 for the
declared area-sampling rule. Reordering/adding another region preserved the
existing region's samples and semantic elevation draws. These tests establish
the sampling calculation, not land suitability or reception.

### Reproducible five-hour operator demonstration

The shipped fixed-seed42 world contains two coupled Class A units and sampled
passive regions: **8 Varna-region candidates and 3 Balchik-region candidates**.
Every candidate remains explicitly unqualified and AMSL. Source FNVs:
world `c6208d82cfe68d40`, waypoint catalogue `cece58237503ab93`, regions
`809b2625bb9d7cb5`. The independent CLI oracle checks these shipped fingerprints
before applying the qualified GPU/toolchain golden; other hardware/toolchains
must still pass semantic oracles and explicitly skip the numeric pin.

| Canonical trace evidence | Result |
| --- | --- |
| Simulation time | 18000 seconds; no real-time waiting |
| Physical vessels / passive equipment | 2 /11 |
| Sparse transitions | 1405 |
| Samples at60s including0/end | 301 frames,602 vessel queries,3311 receiver rows |
| Covered records / final record | 5319 /1 |
| File count / exact bytes | 1 NDJSON trace /2218030 bytes,5320 LF lines |
| Logical FNV64 | **`6328099c89f9a685`** |
| Entire trace SHA256 | `69112ee58d276730ec838cf2509e7cdf7ea3b8079f7b1adb52b7dea45f3bc978` |
| Independent fresh build comparison | Identical every trace byte, including manifest and final status |

Files actually generated for operator review:
`lab/build/codex-w1-world/acceptance-world-v1.ndjson` and
`lab/build/codex-w1-world-independent/acceptance-world-v1.ndjson`, with separate
`.metrics.txt` diagnostics beside each and an independent-build comparison JSON.
These are ignored build artifacts. The trace is a world trace, never a COMPLETE
radio journal. Its last line is:

```json
{"type":"complete","status":"COMPLETE_WORLD_ONLY","records":5319,"logical_fnv1a64":"6328099c89f9a685"}
```

Selected real trace fields illustrate that equipment power and navigation are
independent. At0s, vessel101 has OFF coupled TX/RX, position27.91E/43.21N,
SOG10.673kn, fixed antenna offset18m and reach36046.271m. At60s, it is ON at
27.912786675E/43.212153734N, with unchanged nominal speed and total travel
329439mm plus8257536000/8847360000mm. Radius is a world environment input;
neither row says that a packet was received. Later Class A OFF notices carry
`abort_if_overlapping_burst:true`; passive OFF notices carry no protocol-init or
transmit-abort requirement. All three enabled navigation modes occur in the
independently checked schedules.

Two observed operator runs used the same data but different batching:

| Measured resource/stage | Primary: chunk7,batch37,block64 | Independent: chunk300,batch1024,block256 |
| --- | --- | --- |
| Host integration intervals | 3827 | 1604 |
| Current-state descriptor owned bytes (not total heap/RSS) | 4168 | 4168 |
| Peak pending notices / trace rows | 19 /341 | 19 /5318 |
| Position queries / fixed candidate samples | 602 /11 | 602 /11 |
| CUDA launches / largest batch | 18 /37 | 2 /602 |
| Persistent owned device buffers | 10223624 bytes | 10223624 bytes |
| H2D / record D2H / diagnostic D2H | 19536 /14800 /144 bytes | 19408 /14800 /16 bytes |
| Setup wall ms | 144.0086 | 141.5675 |
| H2D event ms | 2.4680 | 0.2655 |
| Position / sampling kernel event ms | 1.6323 /1.3619 | 0.1833 /1.3230 |
| D2H event ms | 2.7360 | 0.1946 |
| Summed CUDA-call wall ms | 10.9251 | 2.3572 |
| Operator world-stage wall ms | 260.5968 | 225.8077 |
| Windows process peak working set | 89776128 bytes | 92450816 bytes |
| Windows peak pagefile usage | 160296960 bytes | 161480704 bytes |

These are individual observed runs, without warmup/median or speed claims.
`total_wall_ms` starts after YAML parsing/validation and includes device setup,
host orchestration and trace publication. An external Python timer measured
the independent entire process at **289.3037 ms**, including configuration and
process startup/exit. Owned device bytes exclude allocator rounding, CUDA
context/events and driver allocations. Process peak memory includes those
libraries/runtime effects; no precise total-heap or universal RSS bound is
inferred from the descriptor counter. Batch size changes launches, memory
staging and timings, while data remains exactly equal.

### Final regression gates and exact commands

| Executed gate | Final result |
| --- | --- |
| Initial historical Release baseline | PASS27/27,117.28s, before edits |
| Primary fresh native Release build | PASS |
| Independent fresh W1 native Release build | PASS |
| Entire final primary Release CTest | **PASS33/33,120.70s** |
| Entire host label, CUDA_VISIBLE_DEVICES=-1 | **PASS23/23,106.21s** |
| Independent build W1 label | **PASS6/6,8.46s** |
| Cross-build Python oracle, final source | PASS, every five-hour trace byte identical |
| Independently generated named trace files | PASS, identical SHA256 and bytes |
| Native host-test import inspection | PASS, no CUDA or yaml-cpp imports |
| Real five-hour wall wait / real AIS radio network | **NOT RUN**; W1 is simulation-time world state only |

Full Release test-label totals (labels overlap) were host23, GPU10, world6,
runtime11, geo7, AIS6 and stress3, across33 tests. All27 historical tests ran;
their exact names remain in the [R0/R0.1 acceptance inventory](R0_RUNTIME_FOUNDATION.md)
and the final detailed log. New W1 names/coverage appear above. The host label
includes all four W1 host/config/geodesy/operator gates; the explicit GPU
operator's unavailable-device error is tested, not counted as successful world
evaluation. Actual CUDA W1 and historical CUDA gates ran with the device visible.

Historical outputs passed unchanged: B1 **`d3901485badba404`**; B2
**`7aad5e9c764012e3`**,23events; all five B3 Type1/4/9/18/21 bit/NMEA vectors;
B3.1 **`48dec953d3129a2d`** with exact392-byte NMEA; R0 v1
**`728b400ac805d49b`**; R0.1 StreamingV2 **`7805b698778edaca`**. The existing
real CUDA bridge still qualified blocks64/256. No old source, validator,
journal format, codec, profile, fixture license or golden test was modified.

The earlier expanded GPU-wrapper test expected the fresh request flag to
survive selection. The final test instead requires the consumed request and
directly replays the retained effective configuration; both builds were rebuilt
and the complete final suite rerun. No historical assertion or golden was relaxed.

These commands were executed from repository root using the embedded VS tools:

```powershell
$w1CMake = 'C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe'
$w1CTest = 'C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\ctest.exe'
& $w1CMake --preset cuda129-pascal -S lab -B lab/build/codex-w1-world -DAISMIXER_LAB_BUILD_GEO_RUNNER=ON -DAISMIXER_LAB_BUILD_RUNTIME_RUNNER=ON -DAISMIXER_LAB_BUILD_WORLD=ON -DBUILD_TESTING=ON
& $w1CMake --build lab/build/codex-w1-world --config Release --parallel 4
& $w1CMake --preset cuda129-pascal -S lab -B lab/build/codex-w1-world-independent -DAISMIXER_LAB_BUILD_GEO_RUNNER=ON -DAISMIXER_LAB_BUILD_RUNTIME_RUNNER=ON -DAISMIXER_LAB_BUILD_WORLD=ON -DBUILD_TESTING=ON
& $w1CMake --build lab/build/codex-w1-world-independent --config Release --parallel 4 --target aismixer_lab_world aismixer_lab_world_host_tests aismixer_lab_world_geodesy_tests aismixer_lab_world_config_tests aismixer_lab_world_gpu_tests
& $w1CTest --test-dir lab/build/codex-w1-world -C Release --output-on-failure --output-log lab/build/codex-w1-world/w1-full-final-ctest.log
& $w1CTest --test-dir lab/build/codex-w1-world-independent -C Release -L world --output-on-failure --output-log lab/build/codex-w1-world-independent/w1-world-final-ctest.log
$env:CUDA_VISIBLE_DEVICES = '-1'
& $w1CTest --test-dir lab/build/codex-w1-world -C Release -L host --output-on-failure --output-log lab/build/codex-w1-world/w1-host-final-ctest.log
# The following oracle was run in a fresh shell with the GPU visible:
python lab/tests/world_cli_tests.py --exe lab/build/codex-w1-world/Release/aismixer_lab_world.exe --other-exe lab/build/codex-w1-world-independent/Release/aismixer_lab_world.exe --mode gpu --source-dir lab --work-dir lab/build/codex-w1-world/cross-build-final-tests
```

The named operator commands above were also executed in both trees, with
subprocess-captured raw stderr/stdout and exact file comparison; named trace
destinations must be new. Use the saved examples or new ignored filenames to
repeat. Full native output was saved before the host rerun as
`lab/build/codex-w1-world/w1-full-final-detail.log`; the three final suite
summaries are at the paths above. The independent comparison JSON records the
actual process wall timer and SHA256. New-file whitespace/UTF-8/BOM/line-end
checks, Python syntax, final scope and `git diff --check` passed.

### Final source scope and deferred integration

Initial and final branch are `lab`; initial and final HEAD are
`1c1c5fff4d12a55e7cd25386ca1d0989d8451c11`
(`lab: close R0 runtime identity and causal contracts`). Initial worktree and
index were clean, with no untracked files. Final changes are exactly two
modified tracked files and21 new files, all within W1's authorized scope:

```text
modified README.md
modified lab/CMakeLists.txt
new lab/docs/W1_WORLD_LIFECYCLE.md
new lab/tests/world_cli_tests.py
new lab/tests/world_config_tests.cpp
new lab/tests/world_geodesic_reference.h
new lab/tests/world_geodesy_tests.cpp
new lab/tests/world_gpu_tests.cpp
new lab/tests/world_host_tests.cpp
new lab/world/cli_main.cpp
new lab/world/config.cpp
new lab/world/config.h
new lab/world/cuda_engine.cu
new lab/world/cuda_engine.h
new lab/world/geodesy.h
new lab/world/profiles/PROVENANCE.md
new lab/world/profiles/black_sea_regions_v1.yaml
new lab/world/profiles/black_sea_waypoints_v1.yaml
new lab/world/profiles/black_sea_world_v1.yaml
new lab/world/rng.h
new lab/world/world.cpp
new lab/world/world.h
new lab/world/world_session.cpp
```

The README adds the W1 entry point/document link; CMake adds only the opt-in
W1 targets and their named tests. SHA256 comparison against the pre-edit
baseline covers89 tracked files plus two private config files: only README
and CMake changed; the other89 baseline files remain byte-identical. No
staging, commit, push, branch switch, merge/rebase/cherry-pick or branch
synchronization was performed. Private config, `main`, `website`, operational
software and unrelated files were not modified. Generated artifacts remain
under ignored `lab/build/`; the21 untracked additions above are source/docs.

W1 is **COMPLETE within the documented bounded world model**. The following
remain explicit limits for subsequent milestones, in order of integration impact:

1. **No radio/protocol outcome exists.** Class A network entry/TDMA/GNSS
   behavior, actual RF reception, partial-frame decode/capture/interference,
   GMSK, link budget, path loss, SINR, antenna horizon, AIS/NMEA generation and
   UDP are deferred. Reach is an asymmetric environment parameter, never
   conducted power, reciprocity or proof of reception. Future RF snapshots may
   add power/EIRP, antenna pattern/polarization, channels, receiver noise and
   sensitivity independently of this world contract.
2. **Power-cut integration needs a future versioned radio contract.** W1
   publishes exact OFF boundaries and abort requirements. A future durable
   `TransmissionAborted`/partial-frame disposition must update serialization,
   causal validation and RF obligations together. Do not map an OFF directly
   to a legacy StationOff while its TX is open, fabricate TransmissionEnd or
   weaken R0.1's active-TX checks. Every reactivation requires fresh normative
   protocol initialization, which W1 does not perform or restore.
3. **Geographic/datum suitability is unqualified.** Coarse settlement points
   and geodesic routes may cross land. Candidate circles have no coast, terrain,
   chart, port or land-only qualification. AMSL/AGL/ellipsoid are distinct; no
   geoid/tide/sea-surface conversion is available. An explicit ellipsoid
   waterline assumption may resolve a vessel height, but no implicit sea-level
   zero is used. Heel, trim, heave and waves are ignored; mounting offset stays
   fixed relative to modeled waterline.
4. **Computational scope is bounded.** World legs are1m..2000km, solve
   endpoints within +/-80degrees, scenario<=7days and catalog/work limits above.
   Unsupported/nonconvergent queries fail instead of using an approximate
   fallback. Qualification covers the tested cases and fixed native toolchain.
   Other GPUs, compilers, platforms, universal bitwise math and a real five-hour
   wall-time playback are **NOT RUN / not qualified**.
5. **GPU residency is a bounded staging design.** Production performs real
   vectorized ellipsoid/placement computation with persistent reusable buffers.
   Current immutable leg descriptors upload per batch; regions upload per
   sampling call. A continuously evolving full vessel/antenna device registry
   with versioned delta uploads is a future optimization/integration, not a
   feature claimed by the buffer metrics. Small-capacity receiver sampling
   fails explicitly rather than silently splitting identities or using CPU.

Recommendation: continue to separately scoped Class A and frame-level RF
milestones using these immutable world snapshots, power epochs and R0.1 causal
boundaries. Those milestones need their own physical/protocol qualification;
W1 does not establish an operating AIS network.

Suggested operator commit message **only** (no commit was made):
`lab: add W1 deterministic maritime world and lifecycle engine`.

## W1.1 initial-waypoint correction (2026-10-19)

This narrow correction starts from the existing uncommitted W1 implementation
on `lab`, HEAD `1c1c5fff4d12a55e7cd25386ca1d0989d8451c11`. The existing README/
CMake edits and21 untracked W1 files were inputs. A pre-existing deletion of
`lab/lab.zip` was also present and is preserved; it is not part of W1.1.
The historical W1 measurements above retain their original attribution.

The defect was universal validation of the unfiltered initial pool: an All or
Subset selector containing the fixed first destination was rejected even when
other valid starts existed. W1.1 instead removes that one destination ID before
the initial draw. Empty effective pools still fail, including Fixed X -> Fixed X
and Subset [X] -> Fixed X. Full selector validation precedes filtering, so an
excluded duplicate or invalid ID cannot escape validation. Every remaining
initial-to-first route retains the existing geodesic qualification.

Only four input files change in this correction:

- [world.cpp](../world/world.cpp): internal candidate filtering is shared by
  validation and Timeline initialization. The exclusion is zero for a nonfixed
  first selector. Initial sampling still uses the sorted eligible IDs, the
  effective seed, stable vessel ID, `Purpose::InitialWaypoint` and episode0.
  Config selectors remain intact; there are no retries, replacement draws or
  new RNG domains. First-destination and later-cycle behavior are unchanged.
- [world_host_tests.cpp](../tests/world_host_tests.cpp):64 seeds each for All
  and overlapping Subset initial pools, independent existing Philox reference
  indices and both eligible starts observed. Another128 cases retain full-pool
  initial/first draws for nonfixed-first All/Subset combinations. Reordered
  catalogues/subsets and an unrelated vessel preserve the subject's startup
  and first-arrival state. The next leg checks the unchanged destination RNG
  episode and positional continuity. Fixed A -> different Fixed B has explicit
  startup,120-second transition-trace and exact-distance regression coverage.
- [world_config_tests.cpp](../tests/world_config_tests.cpp): the old All+fixed
  rejection becomes positive coverage, alongside an overlapping Subset
  positive. Returned selectors preserve the requested mode and subset IDs/
  order, including the excluded destination. New negatives cover an empty
  effective subset, invalid ID, duplicate excluded ID, sub-1m eligible leg and
  over-2000km eligible leg; geographic cases isolate the initial leg from the
  later cycle. Existing malformed YAML and route/catalogue checks remain.
- This document corrects the selection policy and records W1.1 evidence without
  replacing the historical W1 acceptance measurements.

The two targeted CTest names are `aismixer_lab_world_host_test` and
`aismixer_lab_world_config_test`. Both passed: **2/2**, with **57 host invalid
contracts** (was53) and **88 YAML invalid cases** (was84). The host's former
now-valid negative was replaced with an impossible singleton subset; four
additional host negatives raise53 to57. YAML removes one old negative and adds
five:84-1+5=88. No CTest names or test-label counts were added.

The independently configured build's W1 suite passed **6/6**. Actual CUDA
production and CLI gates ran on the GTX1050Ti; no GPU outcome was simulated.
The cross-build independent CLI oracle passed with exact bytes under blocks
64/256, batches1/1024 and chunks7/300. The shipped five-hour profile remains
**5319 covered records**, logical FNV **`6328099c89f9a685`**. A newly generated
trace was compared directly to the original W1 acceptance file and was identical
in all **2218030 bytes**, SHA256
`69112ee58d276730ec838cf2509e7cdf7ea3b8079f7b1adb52b7dea45f3bc978`.
The demo YAML files, schema v1, public API, RNG code, CUDA code, receiver/antenna
models and runtime/journal/codec code are unchanged.

W1.1 reused the existing validated native build trees and private configuration.
Both Release builds succeeded. The full primary suite passed **33/33** in
129.79s; the independently configured W1 suite passed **6/6** in9.02s.
The separate CUDA-hidden host suite passed **23/23** in108.76s; there were
zero failed tests in these final gates. `git diff --check`, untracked-file
whitespace checks and strict UTF-8/no-BOM/LF/final-newline checks all passed.
Full-suite label counts remain host23/GPU10/world6/runtime11/geo7/AIS6/stress3
(labels overlap). The actual CUDA bridge and W1 production GPU/CLI gates passed.
Historical B1 `d3901485badba404`, B2 `7aad5e9c764012e3`, all five B3 vectors,
B3.1 `48dec953d3129a2d`/392-byte NMEA, R0 `728b400ac805d49b`, and R0.1
`7805b698778edaca` all passed unchanged. W1's separate device-unit digest is
still `dda107efc8701139`.

Exact W1.1 acceptance commands (VS tool variables are defined in the W1
acceptance block above; commands run from repository root):

```powershell
& $w1CMake --build lab/build/codex-w1-world --config Release --parallel 4
& $w1CMake --build lab/build/codex-w1-world-independent --config Release --parallel 4 --target aismixer_lab_world aismixer_lab_world_host_tests aismixer_lab_world_config_tests aismixer_lab_world_geodesy_tests aismixer_lab_world_gpu_tests
& $w1CTest --test-dir lab/build/codex-w1-world -C Release -R '^aismixer_lab_world_(host|config)_test$' -V --output-on-failure --output-log lab/build/codex-w1-world/w11-targeted-ctest.log
& $w1CTest --test-dir lab/build/codex-w1-world -C Release --output-on-failure --output-log lab/build/codex-w1-world/w11-full-ctest.log
& $w1CTest --test-dir lab/build/codex-w1-world-independent -C Release -L world --output-on-failure --output-log lab/build/codex-w1-world-independent/w11-world-ctest.log
# Cross-build oracle executed with the GPU visible:
python lab/tests/world_cli_tests.py --exe lab/build/codex-w1-world/Release/aismixer_lab_world.exe --other-exe lab/build/codex-w1-world-independent/Release/aismixer_lab_world.exe --mode gpu --source-dir lab --work-dir lab/build/codex-w1-world/w11-cross-build-tests
$env:CUDA_VISIBLE_DEVICES = '-1'
& $w1CTest --test-dir lab/build/codex-w1-world -C Release -L host --output-on-failure --output-log lab/build/codex-w1-world/w11-host-ctest.log
```

The full detailed output was preserved before the separate host run at
`lab/build/codex-w1-world/w11-full-detail.log`. The old-recording byte-comparison
result is saved as `lab/build/codex-w1-world/w11-previous-trace-comparison.json`.
The shipped profile was evaluated anew; its data and old acceptance recording
were not edited. No actual five-hour wall wait, RF reception or TDMA experiment
was run. Remaining numerical/model limits are the unchanged W1 limits above.

The worktree remains on the same branch/HEAD, with no staging, commit or push.
The pre-existing README/CMake modifications and archive deletion remain intact.
The21 pre-existing untracked W1 files remain untracked; W1.1 adds no source
files. Hash checks against the W1.1 input identify only the four paths listed
above as changed, with107 other present files byte-identical (including private
configuration and demo profiles), and `lab/lab.zip` still absent as it was at
entry. No main/website, public API, dependency, schema, RNG or CUDA change was
made. Generated evidence is confined to ignored build paths.

Current Git status relative to HEAD (the untracked files and tracked changes
were already present at W1.1 entry; four of those W1 input files were corrected):

```text
 M README.md
 M lab/CMakeLists.txt
 D lab/lab.zip
?? lab/docs/W1_WORLD_LIFECYCLE.md
?? lab/tests/world_cli_tests.py
?? lab/tests/world_config_tests.cpp
?? lab/tests/world_geodesic_reference.h
?? lab/tests/world_geodesy_tests.cpp
?? lab/tests/world_gpu_tests.cpp
?? lab/tests/world_host_tests.cpp
?? lab/world/
```

Suggested operator commit **only**, not executed:
`lab: correct W1 initial waypoint selection against fixed destination`.
