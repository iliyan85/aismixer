# B3.1 GPU-first geospatial foundation

This document separates the implemented offline foundation from future RF and
network work. The qualified target is Windows, VS2022/MSVC, CUDA 12.9 and Pascal
`sm_61`. No Linux qualification, RF fidelity, navigational accuracy or live AIS
operation is claimed.

## Implemented boundary

Host configuration/validation and stable actor-ID sorting -> CUDA placement and
per-actor geographic state -> CUDA absolute-tick motion/report scheduling ->
CUDA polygon predicate -> stable CUB selection -> compact records and small
counters on the host -> existing B3 host codec -> controlled offline NMEA.

`geo_api.h` is CUDA-free. `geo_host.cpp` validates inputs and selects a seed;
`geo_engine.cu` owns device execution and persistent buffers. Optional
`geo_config.cpp` loads YAML with the already-provisioned yaml-cpp dependency.
`geo_ais.cpp` constructs explicit Type 1/18 inputs. Host code never filters a
downloaded population of candidate events. The old B2 implementation and API
remain unchanged, including their original correctness-first download behavior.
There is no CPU simulation/placement fallback. Missing CUDA is an error, even
for an empty actor population or zero-tick request.

## Three clocks and workload axes

Simulation time is `absolute_tick * tick_seconds`; checked B2 limits keep this
integer product safe. Motion is evaluated from tick zero, reporting uses
`tick % interval == phase`, and events retain absolute tick and simulation
seconds. Execution time measures how long the CPU/GPU calculate that scenario.
Playback/ingress time will measure delivery to an external target. Hardware
speed changes execution time only: it never changes physical velocity, phase,
report interval, tick duration, identity order or seed-derived outcomes.

Future workload controls are:

| Axis | Meaning | Interpretation |
| --- | --- | --- |
| P | Population count and composition | Identity count, categories and reporting patterns matter independently of sentence rate. |
| A | Simulated time / real delivery time | A=1 is real-time delivery; A>1 compresses delivery. It does not multiply velocity or `tick_seconds`. |
| D | Receiver multiplicity per original transmission | An outcome in physical RF mode; an independently controlled duplication parameter only in explicitly synthetic ingress stress. |

Physically constrained experiments will retain physical populations/time and
RF reception limits. Synthetic network stress may use absurd P, accelerated A
or controlled D; it is not proof of feasible AIS VHF traffic. Equal input
sentences/second can hide different identities, timing and duplication. Keep
scenario/profile hashes, actor-to-MMSI catalog, logical transmission identity,
tick/time, receiver outcomes and intended delivery schedule as ground truth.
Even enormous P may not increase delivered radio traffic once realistic
channel/reception constraints cap it.

Ticks are discrete motion steps, not GPU clocks, frame rates or AIS TDMA slots.
A future `SimulationInstant {absolute_tick, offset_ns_within_tick}` can refine
transmission timestamps with explicit resolution and range checks, preserving
the original B2 tick semantics. B3.1 records integer simulation seconds only;
it implements no `playback_speed`, real-time wait, socket, TDMA or RF model.

## Geographic data versus computational correctness

`geo/profiles/black_sea_demo.yaml` is a replaceable version-1 illustrative ring
in the required landmark order. Representative settlement positions come from
Wikidata P625 records retrieved on 2026-10-14, rounded to 0.01 degree. The
[provenance table](../geo/profiles/PROVENANCE.md) records original values, pinned
revisions, claim-selection policy and varying reported precision. Rounding does
not improve source accuracy; these community-maintained points have no survey
qualification. Labels are organizational anchors, not coastline vertices.
Straight edges can include land and exclude real water: this is not a water-only
mask, coastline chart, navigational dataset, bathymetry or Danube fairway map.

Geometry correctness is tested independently of that profile's data quality.
New profiles load without recompiling geography-specific kernels. Version 1
accepts a single explicitly closed simple ring, 3..1024 nonclosing vertices,
inclusive boundary, WGS84 coordinates in integer nanodegrees, explicitly ordered
longitude then latitude. This application order is not a claim about normative
EPSG axis order. Longitude must be within +/-180 degrees, latitude within +/-70
degrees; longitude extent must be <=60 degrees. Antimeridian crossings, poles,
holes, multiple rings, repeated vertices, degenerate edges, adjacent collinear
turns and self-intersection/self-touch are unsupported and rejected.

Host geometry validation uses exact signed wide-integer orientation; the GPU
point-in-polygon predicate independently uses exact integer orientation and ray
crossing. A point on any supported segment or vertex is accepted. There is no
floating epsilon or stochastic boundary test. Resource limits bound validation
and per-candidate edge traversal. Dense coastlines/water masks can later use
indexed tiles or hierarchy screening with explicit versioned contracts, rather
than silently raising the current bound or introducing a GIS framework.

## Explicit georeferencing and precision

An overlay is opt-in and separate from B2.1 `schema_version: 1` scenes. Every
actor has exactly one ID-associated WGS84 tick-zero anchor. `x_mm/y_mm` retain
their B2 local meaning. Geographic East/North displacement is
`vx/vy * absolute_tick * tick_seconds`, relative to tick zero; the initial local
offset is never added to the anchor again. East is positive x and north positive
y, with height zero relative to the WGS84 ellipsoid for the transform.

`wgs84_tangent_linear_v1` uses per-anchor WGS84 meridional radius M and prime
vertical radius N: delta-lat=north/M, delta-lon=east/(N*cos(anchor-lat)). It is a
bounded first-order tangent approximation, not an exact ellipsoid geodesic and
not a single equirectangular map across the Black Sea. Radial displacement is
limited to 1000 m at every requested absolute tick, latitude to +/-70 degrees,
and longitude to the valid nonwrapped range for evaluated scheduled reports.
Host extent checks conservatively
reject a request beyond that displacement even if some ticks are unscheduled;
device checks reject transformed unsupported coordinates on scheduled reports.
Unscheduled ticks are counted without evaluating geographic coordinates; their
radial displacement still undergoes the host extent check. Valid movement out
of the polygon is expected: scheduled-but-outside records increment `rejected`.
Invalid transforms fail the complete run before event download.

Double precision is used once per anchor for curvature and per scheduled event
for the transform. Pascal double throughput is limited; exact integer geometry
and B2 motion prevent replacing precision with uncontrolled float arithmetic.
Output is rounded to the nearest nanodegree, ties away from zero. One nanodegree
is at most approximately 0.112 mm on WGS84. An independent ECEF/ENU inversion
oracle checks horizontal discrepancy <=0.30 m over the tested <=1 km,
+/-70-degree envelope. The observed 0.245858 m maximum is predominantly the
difference between this first-order linear tangent model and the ECEF/ENU
model, plus coordinate rounding; it is not an observed GPU numerical error.
Qualification covers those tested cases and this toolchain, not a proof over
all possible geodesics or arbitrary platforms.

AIS conversion is integer `nanodegrees * 3 / 5000`, rounded nearest with exact
ties away from zero (1 degree = 600000 AIS units). It rejects coordinates
outside physical longitude/latitude ranges; it never clamps, wraps or invents
unavailable sentinels. AIS wire quantization itself has an additional maximum
horizontal error of approximately 0.132 m (half a unit per axis). That encoding
quantization is separate from projection and profile accuracy.

## Randomness and replay

Fixed mode accepts an explicit uint64 seed. Fresh mode calls Windows
[`BCryptGenRandom`](https://learn.microsoft.com/en-us/windows/win32/api/bcrypt/nf-bcrypt-bcryptgenrandom)
once for 8 bytes using the system-preferred RNG; failure is an explicit error.
The selected seed is printed and can be replayed in fixed mode. No clock seed,
`rand`, assumed-secure `random_device`, GPU entropy or per-event OS random call
is used. High-volume placement uses cuRAND Philox, which is pseudorandom and
does not supply cryptographic material or a physical TRNG.

Stream-domain version 1 uses subsequence
`(version << 56) | (purpose << 48) | actor_id`, master seed, and absolute-tick
offset where relevant. Placement, future motion, atmosphere, receiver fading
and failure purposes are distinct; only placement is implemented as a model.
Future receiver streams must also include stable receiver/spatial-cell keys
using a new documented key version, never thread indices or unordered append
indices. The B2 diagnostic word intentionally retains the original
`curand_init(seed, actor_id, absolute_tick)` stream; it remains a diagnostic and
is never a behavior rule, MMSI, navigation field or encryption key.

The integer-only test [Philox reference](../tests/philox_reference.h) implements
ten Philox4x32 rounds independently of CUDA/cuRAND and production RNG helpers.
Portable block known-answer tests are separate from the CUDA12.9-qualified
adapter: key `(seed_low32, seed_high32)`, counter
`((offset/4)_low32, (offset/4)_high32, subsequence_low32, subsequence_high32)`,
then scalar lane `offset % 4`. Offset counts 32-bit words; a subsequence skips
2^66 such words. Tracked tests compare small pinned scalar/anchor vectors,
bounded rejection proposals and actual GPU diagnostic words with this
independent calculation. This is a correctness oracle, never a simulation
fallback or a guarantee about future cuRAND initialization conventions.

Placement uses bounded rejection sampling of an unbiased discrete uniform
longitude/latitude bounding rectangle, followed by the device polygon test.
Uniform lon/lat grid density is not uniform surface-area density. Each actor
has its own Philox stream and at most 4096 attempts by default (configurable
1..65536). Extremely low area-to-bounding-box ratios can fail; all failures are
reported, with no partial population, ID collision, runaway loop or CPU retry.
Bounded iteration is not a deadline guarantee: extreme attempt counts combined
with dense/sliver polygons can exceed a desktop WDDM watchdog. Such resets were
not provoked in acceptance; recovery after a device timeout can require process
restart. This closure does not redesign placement launches.

Replay metadata includes seed mode/effective seed, profile/scenario/overlay FNV
hashes, RNG algorithm/domain version, projection version, integer coordinate
units, tick parameters, actor composition, toolchain qualification, device and
result digest. The result digest serializes members little endian in 60 bytes
per GeoEvent, excluding object padding. FNV is a diagnostic fingerprint, not a
security hash. Placement-mode overlay hash is the empty explicit-anchor list;
the effective seed, profile and RNG version define generated anchors. Fixed
input replay, actor reorder, alternative launch shape and split absolute
ranges are tested on the qualified platform. No bit-identical trigonometry or
cuRAND compatibility across arbitrary compiler/GPU versions is promised.
The qualified geographic target compiles with CUDA `--fmad=false`; changing
floating-point contraction settings can change coordinate rounding and result
digests and requires renewed qualification.

The permanent wire oracle pins every LF-normalized UTF-8 byte of the shipped
Black Sea profile independently with SHA-256, then checks its known native FNV
`9ab63fc6b9e76951`. Changing coordinates/metadata at the shipped path fails
before GPU execution unless the pin is deliberately reviewed and updated.
For that profile, CMake's compiler records and a direct CUDA-driver query
independently determine whether the CUDA12.9.86/MSVC19.44.35229.0/sm6.1 golden
`48dec953d3129a2d` applies; native claims alone cannot enable or disable it.
Other platforms print an explicit platform-golden skip reason. Other domain
paths print `qualified golden SKIPPED: non-reference profile`, while generic
wire, metadata, deterministic replay and independent record-digest checks
continue without assuming all eight demo reports remain inside the polygon.
Tracked regressions exercise both shipped-profile tampering and a legitimate
alternate small domain; no ignored audit directory is a test dependency.

## Buffer reuse, ordering and measurement

A `Session` sorts a host copy of actors by ID, then uploads actors, polygon and
optional explicit anchors once. Generated anchors and per-anchor scales remain
on-device. Candidate/selected record buffers, byte flags, counters and CUB
scratch are allocated once for the configured capacity. Every subsequent run
reuses them. Calls on one session are synchronous and must be serialized;
separate sessions have independent buffers. Device 0 is selected explicitly.
Capacity is a hard per-run bound, not automatic internal chunking;
callers split the absolute tick range on the same session. B2 maxima remain
10000 actors, 3600 requested ticks, 1000000 actor-tick candidates, absolute tick
<=1000000000 and tick seconds 1..60. Allocation sizes and available memory are
checked, with explicit errors on unavailable resources.

Tick-major candidate indexing over sorted actors defines canonical
absolute-tick/actor-ID order before selection. The
[`CUB DeviceSelect::Flagged`](https://github.com/NVIDIA/cccl/blob/main/cub/cub/device/device_select.cuh)
operation preserves relative order. Atomic increments only count diagnostics;
there is no unordered atomic append. Device motion, predicate and selection
finish before the host reads small counters. Only accepted `GeoEvent` records
are then downloaded. `sizeof(GeoEvent)` is 64 on this target, while digest
serialization is 60 bytes. Candidate plus selected buffers therefore cost
128 MB at 1M candidates, plus flags, actors/anchors, ring and CUB scratch. The
benchmark prints the sum of requested owned-buffer bytes, including queried
CUB scratch. Allocator rounding, CUDA context/events/library allocations are
outside this estimate and shared device use changes available VRAM.

The operator's benchmark runs 1000, 100000 and 1000000 actor-tick candidates
with mixed Class A/B categories and intervals, one warmup and three equal
steady runs, reporting the median by end-to-end run wall time. CUDA events
separately time H2D, placement, motion, predicate, CUB selection and D2H with
proper synchronization; CPU wall timers cover setup and steady execution.
H2D/D2H event intervals bracket synchronous copies using pageable host memory,
so they include staging/enqueue gaps as well as transfer; they are not isolated
PCIe DMA measurements. Synchronization makes results/timings ready; CPU wait
time belongs to the wall measurement, not an extra CUDA-event stage.
Allocation/setup is separate from reused-buffer execution. Transfer counters
distinguish uncompacted potential bytes, actual accepted records and small
diagnostics. CPU AIS encoding is timed separately on at most 1000 accepted
events and explicitly labeled as a sample. No brittle speed threshold or
unmeasured GPU-over-CPU speedup is claimed. See the acceptance record for
actual hardware, numbers and limits.

## Offline identity and AIS bridge

Actor IDs remain scene identities, distinct from MMSIs. The demo builds a
sorted explicit actor catalog: `100000001 + population_rank`. These synthetic
values have no claimed official testing-range allocation. Reordering input
does not change the mapping; changing population membership can change ranks,
and the CLI regenerates this catalog per run without persisting/reloading it.
Library callers can preserve an explicit catalog for comparisons requiring
identity continuity; a persistent CLI catalog is deferred. The bridge accepts
that explicit catalog and checks both actor-ID and MMSI
uniqueness. Logical transmission identity is `(absolute_tick << 32) | actor_id`
within the existing B2 tick bound, stable across split ranges.

Only Class A -> Type 1 and Class B -> Type 18 are supported. BaseStation/AtoN
mapping is deferred; generic SAR never becomes SAR-aircraft Type 9, and generic
Emergency never becomes AIS-SART/MOB/EPIRB. Unsupported categories fail an
offline encoding request; they can still appear in geographic CSV simulation.
Raw radio state zero is supplied deliberately as an offline test value, not
live TDMA scheduling. B3's unavailable speed/course/heading/ROT/navigation/UTC
markers and false accuracy/RAIM flags remain explicit. No GNSS fix or UTC
synchronization is inferred from RNG or simulation seconds.

The host codec retains exact checksum, fragmentation, binary CRLF and 82-byte
sentence limits. NMEA goes only to stdout or an explicitly named binary file;
diagnostics and replay/event metadata go to stderr. There is no socket,
multicast, gateway or RF transmission. The existing B3 codec remains host-only
and independently usable without CUDA/yaml-cpp at the library level.
The operator uses Windows wide arguments converted to valid UTF-8, checks CSV
and NMEA stdout flushes, and publishes named NMEA output from an exclusive,
unique same-directory temporary file with a no-replace final operation.
Existing/intervening destinations are preserved, and handled failures clean
up temporary output. Forced termination can leave a unique temporary file;
stdout cannot provide an atomic file-publication guarantee. Diagnostic MMSI
lookup is binary search over the validated sorted catalog, O(E log P), with
one bounded buffered metadata row at a time.

## Passive receivers and future RF contracts

The host-only receiver catalog validates unique receiver IDs, WGS84 locations
and antenna heights in millimetres with explicit `WGS84_ellipsoid` datum.
Negative heights can be valid against an ellipsoid. These are passive receiver
sites, not B2 BaseStation AIS transmitters and not AtoN transmitters. B3.1
validates/records the catalog and implements no propagation/reception.

A future GPU RF engine should consume immutable logical transmissions with
simulation instant, transmitter position/height, channel and encoded-payload
reference, plus receiver catalog and a versioned model. It returns sparse
`Reception {logical_transmission_id, receiver_id, arrival_time, power,
path_loss, model_flags, accepted_or_reason}` records. Geometry, antenna heights,
terrain/visibility, refractivity/ducting, correlated fading, interference and
collisions belong in that model. Treat missing model data as explicit unknowns,
not physical facts.

Atmosphere/duct fields should be correlated in space and simulation time,
sampled from stable field/cell keys with documented interpolation and resolution.
Independent per-packet Bernoulli ducting is not a physical model. Candidate
screening must retain plausible duct-enabled long-range paths; an ordinary
radio-horizon cutoff alone can incorrectly remove them. Conservative candidate
bounds and approximation metadata must accompany any screening acceleration.

One logical transmission is encoded once. Multiple receiving stations produce
copies of that same immutable payload with per-receiver metadata, never
independently randomized AIS messages. A future independent CPU dispatcher
controls ingress A and receiver duplication D, transports those immutable
records, and records intended/actual delivery times. A separate black-box
collector compares external `aismixer` output against ground truth without
sharing its internal decoder or inferred acceptance state. Physical RF and
explicit synthetic ingress stress use distinct mode labels and manifests.

## Deferred decisions

Survey-qualified geographic profiles, larger-distance/geodesic motion, richer polygons
and water masks, stable identity catalogs across changing populations, sub-tick
timestamps, receiver RF fields and calibrated correlated propagation are future
milestones. The next RF milestone should implement conservative transmission-
receiver candidate screening and a validated baseline path-loss model with
explicit antenna datum and correlated-field interfaces, followed by independent
receiver-outcome oracles. Live network dispatch/collector and synthetic ingress
stress follow as separate components. No RF, GIS, navigation, TDMA, UDPSEC,
socket, GUI, Python controller or Linux port is hidden in this foundation.
Multi-GPU support, page-locked transfer optimization and performance redesign
are also deferred. The adjacent yaml-cpp trailing null-document boundary edge
is recorded for a separate parser task: this closure preserves current
`LoadAll`/validation behavior rather than undertaking a parser rewrite.
