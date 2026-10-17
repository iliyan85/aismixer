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
