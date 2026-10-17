# R0 runtime foundation

R0 supplies a host event core, bounded recorder, portable Canonical Event
Journal v1, recorded playback and output contracts. The separate CLI generates
only a **SYNTHETIC INFRASTRUCTURE TEST (NOT AIS RADIO SIMULATION)** fixture.
Its reception outcomes are explicitly supplied data. It does not calculate RF,
collisions, carrier sensing, protocol decisions or station activation.

Historic B1 through B3.1 APIs and kernels are unchanged. R0 time is independent
of B2/B3.1 motion ticks. The B3.1 polygon is **not a landmask**. Its <=1 km
tangent displacement and qualified CUDA/toolchain restrictions still apply.
GIS/DEM constraints, horizontal cylinder sampling, independent randomized
activation, Class A SOTDMA/ITDMA/RATDMA, RF propagation and UDP are deferred.

## Build and operator commands

From the repository root, with the existing local VS2022/CUDA 12.9 preset and
locally installed yaml-cpp (no downloads or preset edits):

```powershell
cmake --preset cuda129-pascal -S lab -B lab/build/codex-r0-runtime -DAISMIXER_LAB_BUILD_GEO_RUNNER=ON -DAISMIXER_LAB_BUILD_RUNTIME_RUNNER=ON -DBUILD_TESTING=ON
cmake --build lab/build/codex-r0-runtime --config Release --parallel 2
ctest --test-dir lab/build/codex-r0-runtime -C Release --output-on-failure
ctest --test-dir lab/build/codex-r0-runtime -C Release -L host --output-on-failure
```

The existing preset enables the scene YAML operator. A fresh build can use the
same explicitly provisioned toolchain without a preset. The runtime library
and its native host tests link no CUDA or YAML library; the encompassing lab
project retains its existing Windows/VS2022/CUDA configuration requirement.
`AISMIXER_LAB_BUILD_RUNTIME_RUNNER` defaults OFF. The synthetic CUDA bridge is
compiled only under `BUILD_TESTING`; its kernel is absent from the runtime.
Python is used only by the independent acceptance harness, never by the app.

Choose a **new** run directory and a **new** output filename on every recording
or export. The parent directory must already exist. For example, once each:

```powershell
lab/build/codex-r0-runtime/Release/aismixer_lab_runtime_runner.exe --fixture lab/build/codex-r0-runtime/example-run --segment-records 2
lab/build/codex-r0-runtime/Release/aismixer_lab_runtime_runner.exe --play lab/build/codex-r0-runtime/example-run --receiver receiver.R1 --speed fast --events all --view all --output lab/build/codex-r0-runtime/example-R1.nmea
lab/build/codex-r0-runtime/Release/aismixer_lab_runtime_runner.exe --play lab/build/codex-r0-runtime/example-run --receiver receiver.R2 --speed 10 --events reception,output,metrics --view receiver.R2
lab/build/codex-r0-runtime/Release/aismixer_lab_runtime_runner.exe --play lab/build/codex-r0-runtime/example-run --receiver receiver.R3 --speed fast --events disabled
```

Generation labels stderr and leaves stdout empty. Playback defaults to x1 and
disabled diagnostics. `--speed` accepts `0.5`, `1`, `2`, `10`, `100`, `fast`
(also an `x` prefix). Events/categories/view affect stderr only. The last two
commands write pure binary CRLF NMEA to stdout; use a binary-safe consumer.
R1/R2 each emit the same pinned 49-byte B3 Type 1 vector, R3 emits zero bytes.
The multi-receiver fixture requires `--receiver`; omission is an error before
output. `--view all|ID` is an observational identity filter. It creates no
global TDMA cluster. Categories are `lifecycle,slotmap,tdma,rf,reception,output,metrics`;
`all` and `disabled` are standalone choices. Invalid/duplicate options fail.
Exit codes: 0 success, 2 invalid input, 3 runtime/I/O failure. `--help` alone is
operator help, not an NMEA stream.

## Exact time and clocks

`SimulationInstant` is checked signed int64 with exactly **9,830,400 units/s**,
**262,144 units/slot**, **589,824,000 units/60 s frame**, **2250 slots/frame**.
One unit is 1/1024 of a nominal 9600-bit/s AIS bit. This is a nominal timebase,
not a claim of oscillator, RF or astronomical accuracy. `simulation_time.h`
offers checked addition/difference, integer seconds/nanoseconds conversions,
`decompose_utc` and `compose_utc`. External nanosecond conversions round to
nearest, ties away from zero; exact units remain authoritative. Unrepresentable
intermediate/results fail, never saturate. Negative minutes use floor division.

`UtcAnchor{posix_seconds,subsecond_units}` explicitly maps simulation zero to
recorded UTC. Subseconds must be normalized to [0,9830400). AIS minute/slot
decomposition follows that anchor, not simulation start. POSIX seconds exclude
leap seconds; a leap-second-aware conversion/traceability policy is future work.
Neither scenario time nor the effective seed is inferred from wall time.

Simulation time is event data; execution wall time measures computation;
playback wall time schedules delivery. `MonotonicClock` separates the last two
and permits a fake clock/sleeper. Playback maps the first event to one captured
steady origin and computes every absolute deadline independently:

```text
deadline_ns = origin_ns + floor((event_units - first_units) * 1e9 / (9830400 * speed))
```

Rational presets use checked quotient/remainder arithmetic, not overflowing
intermediate multiplication or accumulated relative sleeps. Whole-nanosecond
flooring is <1 ns early; even x100 keeps adjacent simulation units distinct.
`sleep_until` handles future deadlines, late output proceeds in original order
and increments lateness counters. Slow consumers extend wall duration; no
real-time delivery guarantee is claimed. Nominal five hours at x1/x10 means
five hours/30 minutes; fake-clock tests verify this instantly. `fast` skips
waiting. Extreme spans outside signed nanosecond deadlines are rejected.

## Typed events, causality and bounded scheduling

All headers in `lab/runtime/` are CUDA-free C++17. `event.h` defines strong
`EventId{source_id,sequence}`, `TransmissionId`, `ReceptionId`, `ReceiverId`,
typed payloads and schema 1. IDs are 1..64 printable, non-space ASCII bytes.
Sequences are immutable positive producer-local values, strictly increasing
in canonical chronology. They are never CUDA indices, batch positions, launch
shape, population rank or execution counters. UTF-8 human text is <=4096 bytes
with C0/DEL controls rejected. Event envelope source must equal ID source;
typed subject/receiver references are validated.

Canonical key is `(signed time, phase, bytewise source_id, unsigned sequence,
kind_code)`. A repeated source/sequence is invalid even if kind differs.
Lexical ordering creates a reproducible display, never a causal relationship.

| Phase | Kinds (stable wire codes) |
| --- | --- |
| 0 | station_on/off (1/2), receiver_on/off (3/4) |
| 1 | transmission_end (11), rf_observation (12) |
| 2 | reception_success/failure (13/14) |
| 3 | slot_map_init/reset/reserve/release (5/6/7/8), tdma_decision (9) |
| 4 | transmission_start (10) |
| 5 | receiver_nmea (15), output_error (16) |

Every supplied causal reference must name an already validated predecessor.
At equal time its phase must be strictly smaller. Same-phase lexical neighbors
cannot cause one another. Reception and RF availability equals envelope time;
receptions cannot precede declared TX completion. TX end requires its start;
reception requires matching TX end or matching delivered RF observation; NMEA
requires its matching successful reception. There is at most one final result
per TX/receiver and one NMEA record per reception. A single physical TX can
serve several receivers. OFF receivers cannot observe/receive/emit; on/off
fixtures are supplied truth, not an RF continuity or carrier model. NMEA is
validated finalized AIVDM with checksum, coherent fragment order and exact CRLF;
playback never invokes the AIS encoder.

Station-local slot maps/TDMA payloads identify the station whose knowledge they
represent. The runtime creates no occupancy map or decisions. Undecoded carrier
observations contain no decoded payload or implied successful reception.

`Scheduler` owns a bounded pending queue. Its watermark is monotonic and
half-open: **all events with time < watermark are final**. Equality remains
eligible. Late events, regressions, duplicate/regressing source sequences,
invalid causes and changes to an already dispatched prefix fail. During
`advance_watermark_batches`, callbacks may enqueue later time/phase work inside
the requested interval; the target is published only after that feedback drains.
Callbacks receive a complete simultaneous phase, including **all TX starts at
one time together**. Future RF must use this batch interface; per-record
callbacks exist for serial recording, not sequential RF decisions. Recursive
advance is prohibited. Callback/validation failure seals the scheduler.
`finish_batches`/`finish` drains the terminal time including INT64_MAX and seals
without inventing a `last+1` timestamp. It rejects unfinished transmissions.

Causal batches, GPU batches and output/storage batches have separate meanings.
An interval cannot become final until every observation needed for a decision
inside it is delivered. Advancing a minute merely because a CUDA batch or
segment spans a minute is invalid. Recorders consume only finalized canonical
events; they neither synthesize missing causes nor schedule station behavior.

Explicit limits keep memory bounded: scheduler pending <=65536; 1024 event
sources; retained causal history 4096; station/receiver catalogs 4096 each;
**4096 distinct TX IDs and 4096 distinct reception IDs per run**. Older causal
references and exhausted catalogs fail explicitly. Completed identities are
retained to reject duplicates. A long high-traffic scenario must currently
partition runs; a future indexed identity/retention policy is needed before
unrestricted five-hour traffic recording. Long playback timing is supported
independently of traffic count. This is a documented R0 resource limitation.

## Journal v1 wire specification

All integers below are explicit **little-endian**, unsigned unless `i64`.
Signed values encode two's complement. There is no native struct dump,
alignment, pointer, implicit string terminator or platform-dependent field.
`str` = u32 byte length then exact UTF-8 bytes (ASCII subset for IDs/NMEA).
`id` = source `str` then sequence u64. `optional<T>` = u8 0/1 then T if 1.
Unknown versions, kinds, timebases, booleans, trailing fields and oversized
lengths are rejected. These rules plus the tables suffice for an independent
reader; `tests/runtime_cli_oracle.py` implements one without importing C++ code.

CRC32C is reflected Castagnoli polynomial `0x82F63B78`, initial/final XOR
`0xffffffff`; pinned `CRC32C("123456789") = 0xe3069283`. Logical digest is
FNV-1a64 offset `14695981039346656037`, prime `1099511628211`, modulo 2^64,
over each record's u32 length followed by its event bytes. It excludes CRCs,
segment framing, paths and execution time, so segmentation cannot change it.
Neither checksum provides authentication or cryptographic tamper resistance.

| File | Layout |
| --- | --- |
| `manifest-incomplete.bin` | envelope kind 1 with initial metadata |
| `segment-0000000001.bin` etc. | segment layout below, numbering starts at 1 |
| `checkpoint-0000000001.bin` etc. | envelope kind 2 with fixed 64-byte segment metadata |
| `manifest-complete.bin` | envelope kind 3 with terminal metadata, published last |

Envelope = magic8 `AISJR0F1`, version u16=1, kind u16, units/s u64=9830400,
payload_bytes u32, payload, CRC32C u32 over every preceding byte. Payload limit
256 KiB, total overhead 28 bytes.

Initial payload = status u8=0, provenance str, optional seed u64,
UTC anchor POSIX seconds i64 and subsecond units i64, model-version str,
codec-version str, receiver-count u32, sorted unique receiver-ID strs,
max-records-per-segment u32, max-segment-bytes u32. Version strings <=128 bytes,
provenance <=4096, receiver catalog <=1024. Every receiver-bearing event must
belong to this catalog. No randomized seed is claimed for the fixed fixture.

Segment header (36 bytes) = magic8 `AISJR0S1`, version u16=1, header-size u16=36,
index u64, record-count u32, body-bytes u32, units/s u64. Body repeats
`event-length u32, event-bytes, CRC32C(event-bytes) u32`. Footer is CRC32C u32
over header plus entire body. Event length is 1..65536, segment record count
1..65536, total segment file <=8 MiB; configured lower limits are enforced.
No empty segments. Maximum one million segments per run; no in-memory list
of all segments is accumulated.

Checkpoint payload (64 bytes) = index u64, records u32, file-bytes u64,
segment-CRC u32, cumulative-records u64, cumulative-logical-digest u64,
prior-chain u64, first-time i64, last-time i64. Chain starts as FNV of the
entire initial manifest including its CRC; each checkpoint chain value is FNV
of that entire checkpoint including its CRC. The prior-chain field binds
successive immutable publications. Terminal payload (33 bytes) = status u8=1,
segments u64, records u64, logical-digest u64, last-checkpoint-chain u64.

Event bytes = schema u32=1, time i64, kind u16, source str, subject str,
event ID, optional causal ID, followed by the corresponding payload below.
No additional variant tag is encoded.

| Kind | Payload field order |
| --- | --- |
| 1/2 | station str |
| 3/4 | receiver str |
| 5..8 | station str, channel u8 (A=0/B=1), slot u16 (0..2249), reason str |
| 9 | station str, decision str |
| 10 | TX str, station str, channel u8, end-time i64, immutable payload-reference str |
| 11 | TX str |
| 12 | TX str, receiver str, availability i64, carrier-detected u8, optional local-station str, detail str |
| 13/14 | reception str, TX str, receiver str, availability i64, detail str |
| 15 | receiver str, reception str, TX str, sentence-count u32 (1..9), exact sentence strs (<=82 bytes each) |
| 16 | receiver str, error-code str, detail str |

## Publication, corruption and interruption

The writer exclusively creates a new run directory. Immutable files use
exclusive same-directory temporary creation, checked writes, Windows
`FlushFileBuffers`, checked close and no-replace `MoveFileExW` publication.
A segment is committed only once its checkpoint is published. COMPLETE is a
separate terminal publication; the initial manifest is never rewritten.
This is process-interruption handling, not a promise of power-loss atomicity
for all filesystems/storage devices. No existing target is overwritten.

A forced termination can leave uniquely named temporary files and a published
segment without its checkpoint. Both are ignored as uncommitted. The reader
never repairs or deletes evidence. Missing/corrupt committed segments or
checkpoints, gaps, unknown versions, out-of-order/duplicate events, altered
metadata, bad CRCs/chains and terminal digest mismatch are errors.
`JournalReader(path)` requires COMPLETE. Explicit API-only
`JournalReader(path,true)` inspects a validated INCOMPLETE committed prefix;
playback still refuses it. This snapshot is not a live tailing interface.

Writer RAM holds one bounded segment plus one event, bounded metadata and
bounded validation state; reader RAM holds one event plus bounded metadata and
validation state. `peak_buffer_bytes` measures tracked wire buffers, excluding
metadata, validation state and allocator/stream overhead; it is not process RSS.
The reader constructor validates the entire committed recording. `for_each`
validates it again before callbacks, then streams another checked pass. Files
are assumed immutable during playback; concurrent tampering after
prevalidation may still yield partial external output before a later error.
Raw stdout cannot roll back successful earlier writes. Named NMEA export
streams to an exclusive same-directory temporary and publishes only after
complete successful playback. Handled failures remove owned output temps;
forced termination can leave them. Write/flush/closed-pipe failures are nonzero.

## Independent receiver output and future RF boundary

`OutputSink` accepts finalized `ReceiverNmea` records. `StreamOutputSink` and
`FileOutputSink` preserve bytes, fragment order and receiver identity.
`ReceiverQueues` provides independent bounded record/byte queues with explicit
backpressure, accepted byte/record/error counters and serialized drain calls.
Queue-full is visible; no silent discard or hidden worker exists. Sink failure
retains the front record and poisons only that receiver queue; automatic retry
after a potentially partial external write would be unsafe. These counters
measure local acceptance, not remote reception or durable delivery.
The raw stream flushes/checks after every complete logical record, before any
later playback wait; sparse records do not remain buffered until end-of-run.

`EndpointBindings` validates **one receiver -> one stream -> one UDP
destination**, rejecting duplicate/conflicting destinations. It currently
accepts canonical dotted-decimal IPv4 literals and ports 1..65535 only.
Hostnames, IPv6, duplicate-destination overrides and actual socket I/O are
future work. No R0 endpoint operation contacts a network.

`rf_contract.h` defines bounded submissions and completed observations, without
implementing a backend. Submissions identify TX/channel/start/end/payload,
commit watermark, receiver registry and receiver change sets with geographic
position and explicit WGS84 ellipsoid antenna-height datum. Unchanged receiver
state remains resident; the active host catalog is validation metadata, not
a requirement to copy the entire catalog H2D every interval. Current limits:
1024 TX, 4096 receivers/change entries, 65536 compact observations/submission.
Journal and output catalogs currently cap end-to-end operation at 1024
receivers; an adapter must respect the minimum downstream capacity.
The contract validates intervals, identities, capacities and causal availability.
It calculates no propagation or decode outcome.

A future backend uploads CPU change sets, retains spatial/antenna state on the
device, evaluates candidate TX/RX work and stably compacts observations. D2H
returns compact tuples/counts, never a mandatory dense TX x RX matrix.
The 64-byte CUDA-free `CompactRfObservation` POD uses explicit stable numeric
registry keys; registry binding to strong ASCII IDs must be collision-checked.
Keys are producer identities, never compacted indices; reception key zero is
absent for undecoded observations. This transfer DTO is **not the journal ABI**.
Successful decoding requires a separate reception identity and immutable
payload reference; an undecoded observation cannot become an RX_OK oracle.

Before a dependent station decision, the host must receive and validate all
relevant RF observations through their availability time and feed complete
causal phases back into the scheduler. Host canonical sorting and serialization
follow computation. Final intervals may then be journaled asynchronously with
backpressure; asynchronous recording cannot justify early causal advancement.
Future independently randomized activation is keyed by effective seed, stable
station ID, model version and purpose (`FutureActivationKey`), not population
rank or activation order. R0 samples no activation or cylinder position.

The test-only CUDA bridge uploads 513 synthetic candidate tuples and explicit
fixture keep flags, executes a copy/flag kernel plus real stable CUB selection,
and downloads only a count and 342 compact tuples. Reversed/rotated input and
64/256-thread launches must produce identical host-sorted tuples. The flags
are supplied infrastructure test data, not RF, collision or decoding results.
There is no CPU fallback and no production entry point to this kernel.

## Permanent acceptance and limits

Six new CTests cover core time/causality/bounds, journal integrity and fault
injection, fake-clock playback/output, host RF contracts, actual CUDA transfer,
and independent CLI/journal-byte acceptance. Host tests hide CUDA explicitly;
the bridge has `GPU;runtime` labels. Tests cover all slots/UTC boundaries,
INT64 endpoints, dynamic feedback, coherent TX batches, off-receiver guards,
tiny/large segments, exact B3 bytes, views, no-clobber races, Unicode paths,
interrupted write/flush/close/publication, bad versions/lengths/CRCs,
24000-record bounded recording, queues, closed stdout and five-hour fake time.
The CLI oracle opens a pipe with its reader already closed before child
creation, avoiding timing-dependent broken-pipe tests. No long real sleep,
disk exhaustion, OS kill or watchdog-scale GPU load is used.

The 16 historical tests and their qualified goldens remain mandatory. R0's
new tests do not qualify actual radio or TDMA behavior.

## Observed acceptance, 2026-10-17

Native Release builds succeeded in two fresh ignored directories:
`lab/build/codex-r0-runtime` (all targets) and
`lab/build/codex-r0-runtime-independent` (runtime targets). Environment:
VS2022/MSVC 19.44.35229.0, CUDA 12.9.86, architecture 61, actual NVIDIA
GeForce GTX 1050 Ti, both installed-YAML operators enabled, BUILD_TESTING=ON.
Host sources use /utf-8 /W4; the bridge explicitly selects CUDA VS host /W4
metadata so the inherited /W1 cannot override it. Toolkit deprecation notices
for Pascal remain visible; architecture/toolchain validation is unchanged.

The existing configured closure tree contained 16 tests. The final full suite
passed **22/22** (33.00 s in this run); the independent runtime suite passed
**6/6**, and the explicit CUDA-hidden host run passed **14/14** (11.14 s).
The source tree and private preset/settings hashes were checked against
the clean starting HEAD `e51692aa22b9e41dc1ebb7769687c445bc72eff9`.
Only root README and lab CMake changed among pre-existing tracked files.
No staging, commit, branch/history or remote operation was performed.

| CTest name | Label class | Result |
| --- | --- | --- |
| aismixer_lab_runtime_core_test | host;runtime | PASS |
| aismixer_lab_runtime_journal_test | host;runtime | PASS |
| aismixer_lab_runtime_playback_test | host;runtime | PASS |
| aismixer_lab_runtime_rf_contract_test | host;runtime | PASS |
| aismixer_lab_runtime_cuda_bridge_test | GPU;runtime | PASS |
| aismixer_lab_gpu_self_test | GPU | PASS |
| aismixer_lab_scene_self_test | GPU | PASS |
| aismixer_lab_scene_host_test | host | PASS |
| aismixer_lab_scene_cli_host_test | host | PASS |
| aismixer_lab_scene_cli_gpu_test | GPU | PASS |
| aismixer_lab_scene_cli_interactive_test | GPU | PASS |
| aismixer_lab_ais_codec_test | host;AIS | PASS |
| aismixer_lab_ais_smoke_test | host;AIS | PASS |
| aismixer_lab_runtime_cli_oracle_test | host;runtime | PASS |
| aismixer_lab_ais_wire_oracle_test | host;AIS | PASS |
| aismixer_lab_geo_host_test | host;geo | PASS |
| aismixer_lab_geo_output_test | host;geo;AIS | PASS |
| aismixer_lab_geo_gpu_test | GPU;geo | PASS |
| aismixer_lab_geo_unavailable_test | host;geo | PASS |
| aismixer_lab_geo_config_test | host;geo | PASS |
| aismixer_lab_geo_wire_oracle_test | GPU;geo;AIS | PASS |
| aismixer_lab_geo_cli_closure_test | GPU;geo;AIS | PASS |

Core tests reported 64 branch-specific rejections; journal tests reported 94,
including 24000-record streaming with bounded tracked wire buffers. Five-hour
x1/x10 timing and observable per-record flush were tested with the fake clock;
an actual five-hour wall-clock wait was **NOT RUN**. All required GPU/YAML gates
ran. Historic results: B1 seed 42/count 1025 `d3901485badba404`; B2 demo 23 events
`7aad5e9c764012e3`; all five independent B3 vectors preserved; qualified B3.1
demo `48dec953d3129a2d` and exact **392-byte** NMEA preserved.

Cross-build byte comparison command (also runs the independent CLI oracle):

```powershell
python lab/tests/runtime_cli_oracle.py --exe lab/build/codex-r0-runtime/Release/aismixer_lab_runtime_runner.exe --other-exe lab/build/codex-r0-runtime-independent/Release/aismixer_lab_runtime_runner.exe --work-dir lab/build/codex-r0-runtime/cross-build-tests
```

Both builds produced logical FNV **`728b400ac805d49b`** and identical physical
files with segment-records=2. Bundle SHA256 (sorted ASCII filename + NUL + file
bytes, concatenated) was
`a7b1cec81abca2a99be19268f955d9e19cf0dd11636bb3056d377f827e9692f3`.
Segment-records=1 versus 65536 independently produced the same 17 canonical
events and 49/49/0 receiver bytes, with 17 versus 1 segment files.

Observed operator evidence, saved under the primary ignored build tree:

```powershell
lab/build/codex-r0-runtime/Release/aismixer_lab_runtime_runner.exe --fixture lab/build/codex-r0-runtime/r0-acceptance-fixture --segment-records 2
lab/build/codex-r0-runtime/Release/aismixer_lab_runtime_runner.exe --play lab/build/codex-r0-runtime/r0-acceptance-fixture --receiver receiver.R1 --speed fast --events rf,reception,output,metrics --view all --output lab/build/codex-r0-runtime/r0-acceptance-R1.nmea
```

These paths now exist; commands refuse another recording/export to them.
Generation stderr reported `SYNTHETIC INFRASTRUCTURE TEST (NOT AIS RADIO
SIMULATION)` and `status=COMPLETE records=17 segments=9
logical_digest=728b400ac805d49b`. Playback stderr reported:

```text
time_units=29491200 phase=4 kind=transmission_start id=station.A:2
time_units=29753344 phase=1 kind=transmission_end id=station.A:3
time_units=29753344 phase=2 kind=reception_success id=receiver.R1:2
time_units=29753344 phase=2 kind=reception_success id=receiver.R2:2
time_units=29753344 phase=2 kind=reception_failure id=receiver.R3:4
events=17 displayed=7 receiver=receiver.R1 output_records=1 output_bytes=49
```

The trace is abbreviated; its outcomes are scripted fixture data. Both commands
left stdout empty and the named NMEA file contains the exact 49-byte vector.
The run contains 9 segments (123..456 bytes, 2547 bytes total), 9 checkpoints
(92 bytes each), initial manifest 294 bytes, terminal manifest 61 bytes: 3730
bytes total. No temporary files were treated as committed.

Both actual CUDA launches reported H2D 36936 bytes, compact D2H 21888 bytes plus
4-byte count, 513 candidates/342 selected, block64 and block256. Stable
identity, full tuple fields and canonical host sorting matched. This measures
only a synthetic transfer/compaction contract; it establishes no RF accuracy
or CPU/GPU speedup. Remaining radio/protocol/geography/network behavior and
the explicit bounded-identity limits above require subsequent milestones.

## R0.1 contract closure: decisions frozen before implementation

Initial branch `lab`, clean HEAD `01288cfac2e4cff14a44e7c4dbde8e69f980daf5`.
The existing Release baseline passed 22/22 before edits (27.68 s). This addendum
supersedes R0's requirement to partition long traffic histories **only for the
explicit opt-in StreamingV2 profile**. LegacyV1 remains the default.

| Decision | Implemented in this closure | Deferred / reason |
| --- | --- | --- |
| D1 identity | StreamingV2 requires injective `tx:<producer>:<counter>` / `rx:<producer>:<counter>` IDs; producer equals creation-event source, uses <=32 ASCII letters/digits/underscore/dot/hyphen, counter is canonical positive u64 and strictly increases separately for TX/RX. At most 1024 stable producers retain highwaters forever; only active correlations retire. | Arbitrary-string v1 retains its original finite identity catalog; bounded RAM cannot prove arbitrary historical string uniqueness without an index. No ID recycling or probabilistic hash proof. |
| D2 watermark | Half-open event commit W remains irreversible. A separate stateful RF input/result horizon tracks registered in-flight context, finite coverage obligations and materialized feedback. Explicit resolution closes correlations only after TX end, coverage completion and output disposition. | RF physics and choosing scientifically valid resolution horizons. |
| D3 local knowledge | Typed receiver role/owner registry and router admit observations only to an enabled transceiver radio's own station. Passive terrestrial interfaces never produce station-decision input. Journal events are not protocol-input tokens. | Durable ownership proof/catalog is not claimed by v1 or the narrow StreamingV2 identity profile; a future separately versioned ownership profile is required. |
| D4 producers | Bounded producer hold/release in availability/phase/semantic-key order assigns event sequences at release. Scheduler rejects enqueue phase inversions and offers pending-time inspection; RF barriers reject unresolved/unmaterialized feedback. | Actual decentralized station machines and TDMA decisions. |
| D5 RF geometry | Document immutable TX-time snapshots, registry lifetimes/deltas, position time, units, power and height datums; keep existing 64-byte CUDA bridge compatible. | Physical parameter schema and real computation await RF implementation; no ellipsoid/AMSL equivalence or 20 km B3.1 extension. |
| D6 persistence | Existing v1 bytes, framing and fixture stay unchanged. Opt-in envelope/segment version 2 with manifest profile tag 2 selects event schema 2 and shared streaming validation. Checkpoint/CRC/FNV algorithms remain unchanged; reader accepts both and rejects mixed/future versions. | General format redesign, database index and resumable writer are unnecessary for this bounded correction. |

Streaming schema 2 extends TransmissionStart with an explicit bounded coverage
roster and a finite latest-availability horizon. New typed records represent
no-reception dispositions, successful reception with explicitly omitted output,
and final TX resolution. Resolution is bookkeeping phase 6 after existing phases
0..5, never a radio decision. Active TX/RX, pair obligations and typed anchors
have explicit capacities; live lifecycle/TX/reception/reservation anchors survive
the generic 4096-event recent window. Arbitrary old unanchored causes still fail.
Retirement never substitutes an elapsed TTL or a count threshold for completion.

NMEA hardening binds the first observed canonical AIS payload bits per active
TX and compares subsequent receiver outputs, allowing receiver-local fragment
presentation differences. It verifies TX channel and cross-receiver content;
it does **not** claim equality with unseen external `payload_reference` bits.
No actual RF, TDMA, UDP, cylinder, GIS or new geographic model is authorized.

### R0.1 executable invariants and source map

D1: [event.h](../runtime/event.h), [event.cpp](../runtime/event.cpp) define both
profiles. The domain prefix plus colon-free producer and canonical decimal u64
is an injective encoding. A creation event's source must equal its ID producer;
its TX or RX counter must exceed that producer's retained highwater. Separate
TX/RX counters and event source highwaters persist for the entire run and are
never evicted. At most 1024 stable producers are permitted. Reusing an old ID
after active retirement therefore fails without an all-history identity set.
The independent reader reconstructs the same proof from the beginning across
every segment. Writer resume/append to an existing run remains unsupported.

Streaming active limits are 1024 TX, 4096 receptions, 65536 TX/receiver pairs,
512 sorted unique receiver IDs per TX coverage, 4096 lifecycle/map/reservation
entries per catalog, and 4096 generic recent events. Typed anchors retain
current lifecycle, open correlation, map/reset and reservation evidence beyond
that recent window. The latest carrier anchor for a TX/receiver supersedes its
older carrier anchor; older arbitrary references then use the recent-window
policy. A typed anchor is created only from a validated published event, never
from a caller-supplied dangling ID. TX end removes the per-station open-TX
index, not the remaining reception obligations. Final resolution removes active
TX, results, pair evidence and their typed anchors together only after every
candidate pair has a final disposition and every successful RX has NMEA or an
explicit `ReceptionNoOutput`. Zero decoded receptions is a valid closed result.
Coverage horizons bound RF availability; they are not elapsed-time retirement.
Metrics report active/anchor counts and recent history separately, not RSS.

D2/D4: [scheduler.h](../runtime/scheduler.h),
[scheduler.cpp](../runtime/scheduler.cpp),
[rf_contract.h](../runtime/rf_contract.h),
[rf_contract.cpp](../runtime/rf_contract.cpp). The source enqueue guard compares
the same time/phase/source/sequence order as canonical drain, so a phase
inversion fails before insertion and leaves earlier valid queued work usable.
`next_pending_time()` is observational. `accepted_event(id)` returns an exact
bounded pending/retained snapshot copy; pending acceptance is not proof of
publication. `has_dispatched(id)` supplies a bounded source progress query for
private known-enqueued producer tokens; because sequence gaps are legal, it is
not an arbitrary EventId membership oracle.

`RfCausalBoundary::attach` installs a state-backed producer barrier before the
first advance and before registering RF work. Fresh TX must start at/after
event W and RF H. Its ID/start snapshot, interval, channel, payload reference,
coverage and horizon must match the Scheduler's actual accepted TX start;
the end anchor must match its accepted TX end. A registered context can then
span multiple RF slices with start < W. `context(id)` supplies that same
immutable work; it never republishes a TransmissionStart. Context retirement
preserves producer highwaters and requires end, closed obligations, materialized
discoveries and H strictly beyond its coverage deadline (or explicit terminal).

H is a monotonic half-open **no more discoveries with availability < H**
producer assertion, distinct from event W. `resolve_before(H)` rejects overdue
missing final dispositions; all pending discoveries below a proposed W must
be materialized before Scheduler can advance. Late discoveries fail. Final
dispositions are Decoded, Failed, NotAudible or NotScheduled; missing evaluation
is an open obligation, never an implicit failure/success. Pending discoveries
are bounded at 65536 and sorted by availability, phase, receiver, TX,
disposition and stable semantic key before event/RX sequences are assigned.
The public barrier checks before and after phase feedback; failure after any
published prefix seals the scheduler. Terminal handling includes INT64_MAX.

For example, the synthetic contract test registers one TX [0,100] with known
coverage, carries the same context across W=20/50 and later slices, delivers an
own-radio carrier at 25, and enqueues its explicitly injected phase-3 decision
at the same instant after phase 1. It then closes final pair dispositions at
110 and resolves at 120 before W=121. Reversed discovery order and alternate
event-watermark boundaries produce the same canonical bytes. These are symbolic units,
not propagation or AIS scheduling predictions. An RF producer must stop at
each feedback frontier before extending H if that feedback may create a new
TX. An arbitrary whole-minute RF assertion is not a valid substitute.

D3: `ReceiverRegistry`, `ReleasedRfObservation`, `LocalObservationRouter` and
`LocalRfObservation` in [rf_contract.h](../runtime/rf_contract.h). The bounded
immutable registry requires a registered owner for TransceiverRadio and no
owner for PassiveTerrestrial. The router accepts only a privately minted
producer token bound to that exact catalog and Scheduler, after semantic
dispatch, at/after availability, for the enabled radio's exact owner. Passive,
disabled, cross-owner, pending and no-evaluation injections fail. Its local
result exposes only owner, RX interface, availability, disposition, an opaque
causal receipt and (only on decode) the immutable decoded payload reference.
It exposes no global Event, TX identity or ground-truth journal interface.
The attached Scheduler must stay alive and unmoved for all token/router calls. This is an
in-memory ownership boundary, not durable owner proof in either current format.

D5: the future RF physical schema must specify `tx_position_time_units` and an
immutable registry revision/snapshot retained until final RF resolution;
WGS84 longitude/latitude in nanodegrees; station altitude in ellipsoidal mm;
antenna offset and explicit ellipsoid/AMSL/AGL datum; transmit power/EIRP with
declared units (proposed milli-dBm), antenna gain/pattern/polarization/reference
frame, losses, receiver sensitivity/noise bandwidth/channel and enabled
timeline. Conducted power plus gains/losses must not be counted twice as EIRP.
Motion interpolation endpoints and position timestamps must be explicit.
Propagation/processing delays need a versioned deterministic quantization to
9830400 units/s, with a strictly positive representable carrier delay. The
one-unit minimum here asserts ordering only, not a physical signal speed.
Persistent GPU registry state and changed H2D snapshots are the intended
boundary. None of these physical fields/calculations is implemented by current
`RfTransmissionWork` schema 1 or the additive schema-2 causal boundary. B3.1's
<=1 km approximation is not a 10-20 km placement method.

D6: [journal.h](../runtime/journal.h), [journal.cpp](../runtime/journal.cpp).
LegacyV1 is still the default; its fixture source and wire bytes are unchanged.
The four correctness fixes apply to legacy validation too, including channel
and cross-receiver AIS-bit consistency. Previously accepted contradictory
records are errors; no valid historical golden is redefined. First observed
finalized NMEA binds observed TX bits, not unseen external payload bytes.

StreamingV2 explicitly uses envelope and segment version u16=2, unchanged
magic/names/LE framing/timebase, and initial manifest **profile tag u32=2 before
the old status byte**. Remaining metadata, checkpoint64 and terminal33 payloads
are unchanged. Every event must have schema u32=2. Kind10 appends roster-count
u32, receiver strs and resolution-horizon i64 after the existing payload
reference; there is no optional flag on wire. Kind17 is TX str, receiver str,
NoReceptionReason u8 (1 NotAudible / 2 NotScheduled), detail str. Kind18 is
receiver str, reception str, TX str, reason str. Kind19 is TX str, horizon i64.
Kinds17/18 use phases2/5; kind19 uses bookkeeping phase6. V1 rejects these
extensions. Reader rejects mixed schemas/versions, unknown profile tags and
future versions. CRC32C, logical FNV, chain and publication remain unchanged.
`finish()` now publishes valid buffered records/checkpoint before terminal
validation; unfinished RF/TX/output leaves an inspectable INCOMPLETE prefix,
never a false COMPLETE.

Future durable ownership sketch (unsupported new profile tag, not a v2
extension accepted today): receiver ID, role u8, optional owner station ID,
catalog revision, station catalog, effective simulation time and versioned
lifecycle/ownership deltas, bound to the model/registry snapshot. Such metadata
must be versioned and validated on replay before claiming durable local-owner
proof. Current v1 `local_station_id` strings do not provide that proof.

Multi-receiver playback remains one logical stream per receiver/endpoint. A
future synchronized dispatcher must scan one journal using **one shared clock
origin/deadline mapping for all receivers**, with independent bounded sinks.
Running separate single-receiver CLI processes is not synchronized fan-out.
The full multi-sink player and UDP remain deferred. Five-hour playback checks
use an injected clock; no five-hour actual wall wait is claimed.

### R0.1 acceptance evidence (2026-10-17)

The final environment was native Windows 11, Visual Studio 2022 x64,
MSVC 19.44.35229.0, CUDA 12.9.86, Pascal sm_61 and an actual NVIDIA GeForce
GTX 1050 Ti with 4 GB VRAM. Existing `/utf-8`, `/W4`, toolchain, local YAML
dependency and private presets were retained. Both build directories were
freshly configured for this closure. No packages were installed.

| Executed gate | Result |
| --- | --- |
| Initial existing R0 Release suite | PASS 22/22, 27.68 s, before edits |
| Primary fresh Release build and full CTest | PASS 27/27, 126.30 s |
| Primary host label with `CUDA_VISIBLE_DEVICES=-1` | PASS 19/19, 99.16 s |
| Independent fresh build, runtime label | PASS 11/11, 109.98 s |
| Legacy CLI oracle across both fresh executables | PASS logical history and identical physical v1 bundle |
| StreamingV2 independent Python oracle across both fresh executables | PASS logical history, receiver SHA256 and identical physical v2 bundle |
| Short actual `SteadyPlaybackClock` playback of pre-existing R0 recording at x100 | PASS exact 49-byte R1 stdout, empty stderr, successful exit |
| Five-hour actual wall-clock playback | NOT RUN; full five-hour x1/x10 tests use injected clocks |
| Actual RF/TDMA/UDP or scientific radio validation | NOT RUN; outside this infrastructure closure |

The full Release suite executed the following exact CTest names. H means also
executed in the CUDA-hidden host gate; R means also executed in the independent
runtime gate. Every listed test passed; a dash means outside that selected gate.

| Exact CTest name | H | R |
| --- | --- | --- |
| `aismixer_lab_runtime_core_test` | PASS | PASS |
| `aismixer_lab_runtime_journal_test` | PASS | PASS |
| `aismixer_lab_runtime_playback_test` | PASS | PASS |
| `aismixer_lab_runtime_rf_contract_test` | PASS | PASS |
| `aismixer_lab_runtime_journal_v2_test` | PASS | PASS |
| `aismixer_lab_runtime_rf_boundary_test` | PASS | PASS |
| `aismixer_lab_runtime_streaming_test` | PASS | PASS |
| `aismixer_lab_runtime_contract_test` | PASS | PASS |
| `aismixer_lab_runtime_cuda_bridge_test` | - | PASS |
| `aismixer_lab_gpu_self_test` | - | - |
| `aismixer_lab_scene_self_test` | - | - |
| `aismixer_lab_scene_host_test` | PASS | - |
| `aismixer_lab_scene_cli_host_test` | PASS | - |
| `aismixer_lab_scene_cli_gpu_test` | - | - |
| `aismixer_lab_scene_cli_interactive_test` | - | - |
| `aismixer_lab_ais_codec_test` | PASS | - |
| `aismixer_lab_ais_smoke_test` | PASS | - |
| `aismixer_lab_runtime_cli_oracle_test` | PASS | PASS |
| `aismixer_lab_runtime_streaming_oracle_test` | PASS | PASS |
| `aismixer_lab_ais_wire_oracle_test` | PASS | - |
| `aismixer_lab_geo_host_test` | PASS | - |
| `aismixer_lab_geo_output_test` | PASS | - |
| `aismixer_lab_geo_gpu_test` | - | - |
| `aismixer_lab_geo_unavailable_test` | PASS | - |
| `aismixer_lab_geo_config_test` | PASS | - |
| `aismixer_lab_geo_wire_oracle_test` | - | - |
| `aismixer_lab_geo_cli_closure_test` | - | - |

Targeted acceptance mapping (test file names map directly to CTest names above):

| Requirement | Executed coverage |
| --- | --- |
| T1 enqueue phase inversion | `runtime_contract_tests.cpp`: immediate rejection, legal phase progression and subsequent usable queue; existing core phase-group regressions retained |
| T2 accepted incomplete prefix | `runtime_journal_tests.cpp` and `runtime_journal_v2_tests.cpp`: open TX, multiple accepted records, segment limits 1/default, no COMPLETE, verified incomplete reader and no-clobber |
| T3 NMEA/TX consistency | `runtime_contract_tests.cpp`: wrong channel, changed payload/fill, fragment reassembly/order and valid receiver presentation differences; old B3/v1 independent oracles |
| T4 positive carrier availability | `runtime_rf_contract_tests.cpp` and `runtime_rf_boundary_tests.cpp`: equality rejection, +1 success, decode-before-end, negative/MAX arithmetic |
| T5 in-flight context | `runtime_rf_boundary_tests.cpp`: registered TX spanning slices, one start, later result, retroactive/unregistered/snapshot mismatch rejection |
| T6 simultaneous TX | `runtime_rf_boundary_tests.cpp`: two same-channel TX, independent explicit failed dispositions, reversed discovery order; ordering only |
| T7 causal feedback/release | `runtime_rf_boundary_tests.cpp`: own-radio carrier causes an injected same-instant phase-3 decision, late feedback/barrier bypass rejection, alternate event-watermark steps give equal bytes |
| T8 continuous five hours | `runtime_streaming_tests.cpp` and `runtime_streaming_oracle.py`: one validator/writer per run, 10800 TX/32400 RX, bounded streaming, x1/x10 injected-clock playback |
| T9 retired identity reuse | `runtime_contract_tests.cpp` and `runtime_journal_v2_tests.cpp`: TX/RX reuse after retirement/segment boundaries, independent reader rejects valid-CRC semantic forgeries and reorder |
| T10 local ownership | `runtime_rf_boundary_tests.cpp`: exact owner/registry binding, passive/cross-owner/disabled/pending/global injection rejection, filtered local result type |
| T11 legacy compatibility | `runtime_cli_oracle.py`, old journal tests and manual playback of pre-existing v1 recording; v1 bytes/digest unchanged |
| T12 full/host/GPU | All 27 tests above; CUDA-hidden host 19; actual bridge block64/block256 |
| T13 segments/builds | Native 127/coarse, 4096/fine, 127/fine recordings; independent Python reads and cross-build bundle comparison |
| T14 timeline/terminal/output | Existing core/playback/CLI tests plus signed-endpoint RF tests: negative/MAX, terminal drain, simultaneous phases and closed stdout |

The five-hour fixture in [streaming_fixture.cpp](../runtime/streaming_fixture.cpp)
is loudly labeled **INFRASTRUCTURE TEST -- NOT AIS RF SIMULATION**. Symbolic
station A emits every 10 simulation seconds and B every 2 seconds, solely as
load examples. Every TX has three scripted successful receptions and outputs;
these are neither AIS reporting rules nor RF predictions. The generator holds
at most two TX/eighteen draft events in each two-second window, assigns source
sequences after canonical sorting, and never resets a run to evade capacity.

| Observed quantity | Result |
| --- | --- |
| Continuous simulation duration | 18000 s (5 h) |
| Run-global unique TX / reception IDs | 10800 / 32400 |
| Canonical records | 97210 |
| Segment-record limit 127 | 766 segments, 1534 total files, 19067015 bytes |
| Segment-record limit 4096 | 24 segments, 50 total files, 18969071 bytes |
| Peak validator active TX / RX / receiver pairs | 2 / 6 / 6 |
| Peak validator typed anchors | 15 |
| Retained generic recent-event cache | 4096 |
| Output per each of three passive receivers | 10800 NMEA records, 529200 bytes |
| Logical FNV64, new StreamingV2 golden | `7805b698778edaca` |
| Per-receiver NMEA FNV64 | `c022d14ec8edd1d5` |

These metrics instrument validator objects, not process RSS or device memory.
Both Scheduler and journal validation are exercised; the reader reconstructs
the same bounded identity/causal rules. The independent Python oracle uses
standard-library parsing, its own CRC32C and structural checks, literal NMEA
goldens and bounded live correlation state. Its `active_peak=8` counts TX+RX,
so it is a different metric from native typed-anchor count. It does not call
the native decoder or encoder to compute expected bits/bytes.

Changing valid watermark steps left all physical files identical at the same
segment limit. Changing segmentation left canonical event bytes/digest and
receiver output unchanged. Two independently built executables also produced
identical physical bundles at limit 4096. Bundle hash construction remains
sorted ASCII filename + NUL + file bytes, concatenated:

```text
StreamingV2 logical FNV64: 7805b698778edaca
StreamingV2 bundle SHA256: bffbd8ece5c0086923c09916fa989a4ad89817cee4677cf1c30acec33f9dca1d
Each receiver SHA256:     dd9054ad8b67b02a421c7aedb4dcfdae5cecb6a6444c0e510bfcf60ec438eab9
LegacyV1 logical FNV64:   728b400ac805d49b
LegacyV1 bundle SHA256:   a7b1cec81abca2a99be19268f955d9e19cf0dd11636bb3056d377f827e9692f3
```

The v1 reference source `fixture.cpp`, its old CLI oracle and its existing
recording were not edited. The new CLI still reads that old recording. B1
`d3901485badba404`, B2 `7aad5e9c764012e3` (23 events), all five B3 complete
bit/NMEA goldens (Types 1/4/9/18/21), and qualified B3.1
`48dec953d3129a2d` with exact 392-byte NMEA passed unchanged. The real CUDA
bridge again matched blocks 64/256: H2D 36936 bytes, compact D2H 21888 bytes
plus 4-byte count, 513 candidates/342 selected. This measures transfer and
compaction only; no physical RF behavior or speedup claim follows.

Commands used for configuration and acceptance (from repository root; the
existing private preset was used without modification):

```powershell
$r01CMake = 'C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe'
$r01CTest = 'C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\ctest.exe'
& $r01CMake --preset cuda129-pascal -S lab -B lab/build/codex-r01-runtime -DAISMIXER_LAB_BUILD_GEO_RUNNER=ON -DAISMIXER_LAB_BUILD_RUNTIME_RUNNER=ON -DBUILD_TESTING=ON
& $r01CMake --build lab/build/codex-r01-runtime --config Release --parallel 4
& $r01CMake --preset cuda129-pascal -S lab -B lab/build/codex-r01-runtime-independent -DAISMIXER_LAB_BUILD_GEO_RUNNER=ON -DAISMIXER_LAB_BUILD_RUNTIME_RUNNER=ON -DBUILD_TESTING=ON
& $r01CMake --build lab/build/codex-r01-runtime-independent --config Release --parallel 4 --target aismixer_lab_runtime_runner aismixer_lab_runtime_core_tests aismixer_lab_runtime_journal_tests aismixer_lab_runtime_playback_tests aismixer_lab_runtime_rf_contract_tests aismixer_lab_runtime_journal_v2_tests aismixer_lab_runtime_rf_boundary_tests aismixer_lab_runtime_streaming_tests aismixer_lab_runtime_contract_tests aismixer_lab_runtime_cuda_bridge_tests
& $r01CTest --test-dir lab/build/codex-r01-runtime -C Release --output-on-failure --output-log lab/build/codex-r01-runtime/r01-full-ctest.log
& $r01CTest --test-dir lab/build/codex-r01-runtime-independent -C Release -L runtime --output-on-failure --output-log lab/build/codex-r01-runtime-independent/r01-runtime-ctest.log
$env:CUDA_VISIBLE_DEVICES = '-1'
& $r01CTest --test-dir lab/build/codex-r01-runtime -C Release -L host --output-on-failure --output-log lab/build/codex-r01-runtime/r01-host-ctest.log
python lab/tests/runtime_cli_oracle.py --exe lab/build/codex-r01-runtime/Release/aismixer_lab_runtime_runner.exe --other-exe lab/build/codex-r01-runtime-independent/Release/aismixer_lab_runtime_runner.exe --work-dir lab/build/codex-r01-runtime/cross-v1-build-tests
python lab/tests/runtime_streaming_oracle.py --exe lab/build/codex-r01-runtime/Release/aismixer_lab_runtime_runner.exe --other-exe lab/build/codex-r01-runtime-independent/Release/aismixer_lab_runtime_runner.exe --work-dir lab/build/codex-r01-runtime/cross-v2-build-tests
lab/build/codex-r01-runtime/Release/aismixer_lab_runtime_runner.exe --play lab/build/codex-r0-runtime/r0-acceptance-fixture --receiver receiver.R1 --speed 100
```

The last command was captured by a Python subprocess assertion, including exact
stdout bytes, empty stderr and exit status. No duration threshold was imposed.
Recordings refuse existing destinations; use a new ignored directory to rerun
either oracle. Full detailed output is preserved at
`lab/build/codex-r01-runtime/r01-full-detail.log`; suite summaries are the
three `r01-*-ctest.log` files named above. All generated artifacts remain under
ignored build directories.

### R0.1 final worktree and continuation decision

Initial inspection found branch `lab` at
`01288cfac2e4cff14a44e7c4dbde8e69f980daf5`, with empty status, empty diff and
no untracked files. Final branch/HEAD are unchanged. No staging, commit, push,
merge, branch switch, synchronization or remote operation was performed.
There are exactly 16 modified tracked files and 7 new untracked source files:

```text
 M README.md
 M lab/CMakeLists.txt
 M lab/docs/R0_RUNTIME_FOUNDATION.md
 M lab/runtime/cli_main.cpp
 M lab/runtime/event.cpp
 M lab/runtime/event.h
 M lab/runtime/journal.cpp
 M lab/runtime/journal.h
 M lab/runtime/playback.cpp
 M lab/runtime/rf_contract.cpp
 M lab/runtime/rf_contract.h
 M lab/runtime/scheduler.cpp
 M lab/runtime/scheduler.h
 M lab/tests/runtime_core_tests.cpp
 M lab/tests/runtime_journal_tests.cpp
 M lab/tests/runtime_rf_contract_tests.cpp
?? lab/runtime/streaming_fixture.cpp
?? lab/runtime/streaming_fixture.h
?? lab/tests/runtime_contract_tests.cpp
?? lab/tests/runtime_journal_v2_tests.cpp
?? lab/tests/runtime_rf_boundary_tests.cpp
?? lab/tests/runtime_streaming_oracle.py
?? lab/tests/runtime_streaming_tests.cpp
```

The README change is two lines in the R0 section linking this addendum. CMake
adds the fixture source and named acceptance gates. Existing tests keep their
qualification assertions: the core test's formerly inconsistent B-channel TX
with A-channel NMEA now uses A; its unsupported-schema test now uses 3 because
2 is supported. No historical golden was changed. Baseline SHA256 comparison
of all 81 tracked files plus both private configuration files found only the
16 intended existing paths changed, no deletions, and 67 files byte-identical.
Production, website, B1-B3.1 code/data, fixture license, old fixture, old CLI
oracle, old CUDA bridge test and private config remain untouched.
Final `git diff --check`, separate new-file whitespace checks, UTF-8/no-BOM/LF
checks for the seven new files, and generated-file/scope checks passed. Nothing
is staged. No build artifacts exist among the seven untracked source additions.

Remaining limitations, ordered by impact on the next milestone:

1. **Real radio/protocol correctness is still unimplemented.** No Class A
   SOTDMA/ITDMA/RATDMA, RF interference/capture/path loss, cylinder placement,
   extended geography, GIS, Inland AIS/ADN or UDP was built. Synthetic successes
   cannot qualify an operating AIS network. The next milestone must independently
   validate physical models and decentralized station behavior.
2. **Future RF producers must honor the causal horizon assertion.** Current
   barriers reject missing/unmaterialized known obligations and late feedback;
   they cannot prove an external backend honestly supplied every relevant
   interferer. Advance H at feedback frontiers, retain registry snapshots and
   declare finite coverage before publication. The current CUDA bridge is not
   that RF backend.
3. **Ownership and RF geometry are not durable proofs yet.** The immutable
   host registry/router is enforced now; either journal profile lacks a durable
   owner/physical-state catalog. A future explicitly versioned profile must bind
   those catalogs/deltas before replay can claim ownership/geometry validation.
   The attached Scheduler must remain alive and unmoved for local token/router use.
4. **Capacity and recovery limits remain explicit.** Streaming requires bounded
   stable producers and the active limits above; it rejects overflow rather
   than dropping observations. LegacyV1 retains its original finite arbitrary-ID
   catalogs. Writer restart/resume, exhaustive power-loss behavior, authentication
   and process RSS bounds are not established by these tests.
5. **Live fan-out and timing qualification remain future work.** The one-clock
   multi-receiver contract is specified, but the full multi-sink live player and
   networking are deferred. Five-hour wall-clock operation and cross-GPU physical
   bitwise equivalence were not tested.

Recommendation: **CONTINUE** to a separately scoped real Class A/RF milestone
using the bounded identity, ownership and causal boundaries here. Require that
milestone's physical/TDMA/registry acceptance before describing the product as
an AIS network simulator. No remaining R0.1 blocker was observed in the executed
gates; operator review and any commit remain separate.

Suggested concise commit message, **OPERATOR USE ONLY**:
`lab: close R0 runtime identity and causal contracts`.
