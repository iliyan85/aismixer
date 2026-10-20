# A1-S1 payload and communication-state qualification

S1.0 layout reviewed on 2026-10-20, before production implementation. This
report qualifies a host payload/state adapter, not a Class A station or radio.
Pilot profile: `LCP-CLASSA-AUTO-TERR-v1`. Frozen A0/A0.1 documents remain unchanged.

## Baseline and authority

- Branch `lab`; HEAD `ef0db3357d93cc26947e799b275cfa27e0920b9b`,
  `lab: reconcile A0 Class A rules and freeze autonomous terrestrial pilot profile`.
- Initial index, worktree and untracked inventory: clean. SHA256 inventory of
  all 114 tracked files retained in ignored research output for scope checking.
- Read authority: all five A0 specification/traceability/test/interface/review
  documents, existing B3 model/encoder/bits/NMEA/CLI/tests, CMake and root README.
- Existing native baseline: Windows, VS2022 MSVC x64, CMake 3.31.6-msvc6,
  CUDA 12.9.86, sm_61, GTX 1050 Ti, driver 582.78. Existing private
  `cuda129-pascal` preset inspected, not changed. Existing full configuration
  exposes 33 CTests; optional scene/geography/runtime/world runners enabled.
- Current [official record](https://www.itu.int/rec/R-REC-M.1371-6-202602-I/en):
  ITU-R M.1371-6 (02/2026), approved 2026-02-19, in force. Original English
  [PDF](https://www.itu.int/dms_pubrec/itu-r/rec/m/R-REC-M.1371-6-202602-I!!PDF-E.pdf)
  and [Word](https://www.itu.int/dms_pubrec/itu-r/rec/m/R-REC-M.1371-6-202602-I!!MSW-E.docx)
  verified against A0 hashes. PDF: 3,464,843 bytes,
  SHA256 `126025db40f545454bbfc192c85353bd24bded48016f853ec184ef46f704a1dd`;
  Word: 2,132,560 bytes,
  SHA256 `f75379e3a0725b1c096ba06a049fc4fcb00ddd81355ba97883a35382933b2d42`.
  Original table images inspected, including continuation pages; no older
  edition or third-party codec used as normative authority. Source copies,
  renderings and logs stay under ignored `lab/build/`.

Offsets below are zero-based from the payload MSB. U means unsigned integer,
S signed two's complement, B Boolean; text consists of AIS six-bit characters.
Numeric values are wire units, not convenience conversions. Width checks precede
packing. MMSI uses the existing explicit nine-decimal-digit, nonzero `Mmsi`;
leading zeroes are preserved. Identity allocation is outside this codec.

## S1.0: Type 3, exactly 168 bits, fill 0

`NORMATIVE_EXPLICIT`: Annex 7 A7-3.1, Table 46 printed pp.105-106 and Table 47
p.107. Type 3 selects ITDMA, unlike Type 1's SOTDMA meaning.

| Field | Offset | Width/type | Unit and qualified domain |
|---|---:|---|---|
| Message ID | 0 | 6 U | Fixed 3 |
| Repeat | 6 | 2 U | 0..3 |
| MMSI | 8 | 30 U | Explicit nonzero identity |
| Navigational status | 38 | 4 U | 0..15 except reserved 9,10,13; 15 undefined/default |
| ROT | 42 | 8 S | AIS coded rate: -128 unavailable, -127/+127 extreme turn indicators; -126..126 coded values |
| SOG | 50 | 10 U | 0.1 knot; 0..1021, 1022 saturation, 1023 unavailable |
| Position accuracy | 60 | 1 B | Explicit caller indication |
| Longitude | 61 | 28 S | 1/10000 minute; -108000000..108000000, +108600000 unavailable |
| Latitude | 89 | 27 S | 1/10000 minute; -54000000..54000000, +54600000 unavailable |
| COG | 116 | 12 U | 0.1 degree; 0..3599, 3600 unavailable |
| True heading | 128 | 9 U | Degrees 0..359, 511 unavailable; 360..510 invalid |
| Timestamp | 137 | 6 U | UTC second 0..59; 60 unavailable, 61 manual, 62 estimated, 63 inoperative |
| Special manoeuvre | 143 | 2 U | 0 unavailable, 1 not engaged, 2 engaged, 3 regional-use code |
| Spare | 145 | 2 U | Fixed zero |
| Low-power | 147 | 1 B | Current-edition explicit indication |
| RAIM | 148 | 1 B | Explicit caller indication |
| Communication state | 149 | 19 U | Required validated `ItdmaState19`, layout below |

No world, navigation, clock, channel or RAIM state is inferred. Default
unavailable markers match the existing Type 1 model. Regional code acceptance
does not assert regional assignment authority.

## S1.0: Type 5, exactly 424 bits, fill 2

`NORMATIVE_EXPLICIT`: Annex 7 A7-3.3, Table 50 printed pp.109-110;
ship/cargo details Table 51 pp.111-114; dimensions Figure 38 p.115.

| Field | Offset | Width/type | Unit and qualified domain |
|---|---:|---|---|
| Message ID | 0 | 6 U | Fixed 5 |
| Repeat | 6 | 2 U | 0..3 |
| MMSI | 8 | 30 U | Explicit nonzero identity |
| AIS edition | 38 | 2 U | Fixed 3: current M.1371-6 convention |
| IMO / flag-state number | 40 | 30 U | Required 1000000..1073741823; see limitation below |
| Call sign | 70 | 42 text | At most 7 characters, padded to 7 |
| Name | 112 | 120 text | At most 20 characters, padded to 20 |
| Ship/cargo type | 232 | 8 U | 0 unavailable; legal Table 51 codes; 100..199 regional use; 200..255 reserved |
| Dimension A | 240 | 9 U | Metres 0..511, 511 saturation |
| Dimension B | 249 | 9 U | Metres 0..511, 511 saturation |
| Dimension C | 258 | 6 U | Metres 0..63, 63 saturation |
| Dimension D | 264 | 6 U | Metres 0..63, 63 saturation |
| Position-fixing device | 270 | 4 U | 0 unavailable; 1..9,12..15 permitted; 10,11 reserved |
| ETA month | 274 | 4 U | 1..12; 0 unavailable |
| ETA day | 278 | 5 U | 1..31; 0 unavailable |
| ETA hour | 283 | 5 U | 0..23 UTC; 24 unavailable |
| ETA minute | 288 | 6 U | 0..59 UTC; 60 unavailable |
| Draught | 294 | 8 U | 0.1 metre; 0 unavailable, 1..254, 255 saturation |
| Destination | 302 | 120 text | At most 20 characters, padded to 20 |
| DTE | 422 | 1 B | 0 ready, 1 not ready/unavailable default |
| Spare | 423 | 1 U | Fixed zero |

Reserved Table 51 values rejected within 0..99:
`8,10,15,16,25,26,27,28,47,48,68,87,88,95,96,97,98`.
Codes 56/57 are local assignments, not future-reserved. Regional/local
allocation validity is external. All-zero dimensions indicate unavailable;
zero and saturation are retained per field. ETA components can independently
be unavailable; impossible known month/day combinations fail. Without a year,
February 29 remains possible. No invented year or UTC source is attached.

`SIMULATION_POLICY` (canonical API representation): configured text accepts
uppercase ASCII 32..95 except literal `@`; rejects lowercase, Unicode,
overlength and embedded terminators; pads remaining positions with `@` six-bit
zero. Empty text is unavailable. The source alphabet permits `@` as terminator;
the API generates it rather than accepting ambiguous embedded terminators.
No truncation, transliteration or implicit uppercasing occurs.

`UNSPECIFIED_OR_CONFLICT`, **S1-NOGO-IMO-ZERO**: current Table 50 lists
1..999999 as not used, 1000000..9999999 as IMO, and 10000000..1073741823 as
flag-state number, with a footnote requiring a flag-state number when no IMO
exists. Zero has no stated interpretation in this edition. This implementation
qualifies only explicit numbers >=1000000 and rejects zero. It does not claim
qualification for an unknown IMO/flag identity, verify registry allocation or
invent a legacy unavailable default. No frozen A0 field rule contradicts this
narrower qualified domain. Older-edition Type 5 version values are outside the
current-edition model.

## S1.0: Class A Type 24 Part B, exactly 168 bits, fill 0

`NORMATIVE_EXPLICIT`: Annex 7 A7-3.22 printed p.140 specifies Class A
manufacturer-identification-only content; Table 77 pp.140-141 and Table 78
p.142 specify the layout, unavailable markers and manufacturer subfields.
This is a dedicated Class A model, not a configurable Class B Part B model.

| Field | Offset | Width/type | Class A qualified value |
|---|---:|---|---|
| Message ID | 0 | 6 U | Fixed 24 |
| Repeat | 6 | 2 U | Explicit 0..3 |
| MMSI | 8 | 30 U | Explicit nonzero identity |
| Part | 38 | 2 U | Fixed 1 (Part B) |
| Ship/cargo type | 40 | 8 U | Fixed 0, unavailable |
| Manufacturer identification | 48 | 42 | Required explicit metadata, decomposed below |
| Manufacturer mnemonic | 48 | 18 text | Exactly 3 AIS six-bit characters |
| Manufacturer model | 66 | 4 U | 1..15; 0 explicitly not used |
| Manufacturer serial | 70 | 20 U | 0..1048575 |
| Call sign | 90 | 42 text | Fixed seven `@`, unavailable |
| Dimensions / reference point | 132 | 30 U | Fixed zero, unavailable |
| EPFS | 162 | 4 U | Fixed 0, unavailable |
| VDES capabilities | 166 | 2 U | Fixed 0, AIS-only capability profile |

The last six bits are **EPFS plus VDES, not six spare bits**. Manufacturer
mnemonic uses the canonical text syntax above with exactly three characters;
the external NMEA manufacturer registry is not checked. Required metadata has
no fabricated vendor/model/serial default. Vessel particulars, auxiliary
mothership substitution, non-AIS VDES capability and Part A are unrepresentable.

`NORMATIVE_DERIVED`: the manufacturer-only restriction combines with source
unavailable values for the other vessel fields. `SIMULATION_POLICY`: selecting
the explicit AIS-only VDES value 0 limits the model to this pilot's capability;
it is not a spare or an assertion about all Class A devices. No current-source
mandatory field/default contradiction remains for this restricted model.

## S1.0: semantic communication-state adapter v1

Annex 2 Tables 18/19 printed p.48 (SOTDMA), Table 20 pp.49-50 (ITDMA), with
the increment-zero context in Table 13. Both are 19-bit states. Offsets here
are relative to the state, not the complete message. Sync values 0 UTC direct,
1 UTC indirect, 2 base direct, and 3 synchronization via a highest-received-
station-count mobile source or a mobile directly synchronized to base are
explicit caller choices.

| State field | Offset | Width | Domain / meaning |
|---|---:|---:|---|
| ITDMA sync | 0 | 2 | 0..3 |
| ITDMA wire increment | 2 | 13 | 0..8191 slot offsets |
| ITDMA slot-count code | 15 | 3 | 0..7, mapping below |
| ITDMA keep | 18 | 1 | Explicit Boolean |
| SOTDMA sync | 0 | 2 | 0..3 |
| SOTDMA timeout | 2 | 3 | 0..7 frames |
| SOTDMA submessage | 5 | 14 | Strict timeout-dependent variant below |

ITDMA codes 0..4 mean block lengths 1..5 and effective increment equal to the
wire increment. Codes 5..7 mean block lengths 1..3 and effective increment
`wire + 8192` (8192..16383). Ordinary wire increment 0 indicates no further
allocation, not an inferred future transmission. Extended wire 0 means 8192,
not termination. Keep remains explicit. Effective factories reject impossible
length/increment pairs; no masking, scheduling horizon or random slot choice.

| SOTDMA timeout | Submessage type | Checked scalar domain |
|---|---|---|
| 0 | Slot offset / release | 0 releases; positive offsets 1..4499, next-frame relation needs current-slot context |
| 1 | UTC hour/minute | Hour 0..23, minute 0..59, packed `(hour << 9) | (minute << 2)` |
| 2,4,6 | Current slot | 0..2249 |
| 3,5,7 | Received-station count | 0..16383 other stations, excluding own station |

UTC uses five hour bits and seven minute bits, followed by two unused bits.
`SIMULATION_POLICY`: adapter v1 emits and requires zero in the unused low two
bits; this canonical decode restriction is stricter than merely calling them
unused. **S1-NOGO-UTC-UNAVAILABLE**: invalid/absent UTC fails closed; A0-ISS-004
is not guessed. `NORMATIVE_DERIVED`: Table 19 requires a positive offset to
target the next frame. With current slot j and next-frame target k both
0..2249, the offset is `2250 + k - j`, so its possible positive range is
1..4499. Semantic build/decode reject 4500..16383 despite the 14-bit width;
the legacy raw API remains unrestricted. The explicit `offset_in_next_frame`
builder checks both caller-supplied slot numbers and computes that relation,
without choosing a target. A context-free offset build/decode can check the
necessary scalar bound but cannot certify the particular current-slot relation;
later scheduling still owns that context. This refinement followed an original
Table 19 spot-check during implementation, before qualification; it does not
change the frozen A0 requirements or assert a source ambiguity.

Distinct semantic variants reject mismatched timeout/meaning. Raw decode checks
the selected variant's scalar domain and UTC canonical bits; range-valid raw
integers do not automatically become semantically validated. Syntactic UTC
validity cannot attest real clock provenance. Semantic types are immutable,
expose raw `RadioState19` explicitly, and do not implicitly convert between
SOTDMA and ITDMA. Existing raw 19/20-bit APIs retain their old trust level and
all existing values. New Type 3 requires ITDMA; SOTDMA can explicitly feed
existing Type 1 via `.raw()` without changing its raw compatibility surface.

## Qualification plan and integration boundary

Implementation is additive under `lab/ais`, two additive independent tests,
minimal codec/test CMake additions and this report. Existing variant indices
0..4 and CLI demo bytes are preserved; new alternatives append. Original
tests, goldens, A0 documents, README, YAML, private presets, world/runtime/CUDA
and journals are not edited.

Independent test layouts are transcribed from the above tables, not imported
from production helpers. C++ exercises every field interval, defaults, limits,
one-bit mutations and semantic modes; Python uses standard-library literal
offset assembly/extraction, literal vectors and SHA256 of ASCII logical bits.
NMEA validation independently checks XOR, CRLF, 82-byte cap, exact order/fill,
channel and explicit multipart sequence, and rejects incomplete/corrupt sets.
Type 5's 424 bits armor with fill 2; the current renderer yields two fragments
for the fixture. This is unrelated to the physical two-slot rule. AIS payload
bits contain no FCS, HDLC framing, training, NRZI or GMSK.

Execution results: **NOT RUN yet at the S1.0 pre-implementation checkpoint**.
Final results, literal vectors, negative category counts, reproduction commands
and scope audit will be recorded below after implementation.

A1-S2 can later consume these pure payload/state adapters. Entry FSM, channel
selection, timeout decrements, slot allocation, A1-S2..S5, A1-S4 clock/UTC,
RF1 physical frames/two-slot constraints, I1 closed-loop integration and P1
five-hour UDP pilot remain separate gates. **A0-ISS-003 remains unresolved**;
the ability to encode Message 3 does not solve it. A0 tests T03/T10/T17/T33/T34
and relevant T19 are supported only at this payload/state scope, not promoted
wholesale to PASS or ITU/IEC compliance.

## Implemented public surface and file scope

- [ais_message.h](../../ais/ais_message.h): `Type3(Mmsi,ItdmaState19)`,
  `Type5(Mmsi,int64_t imo_or_flag_state_number)`,
  `ManufacturerIdentification(vendor_id,model_code,serial_number)` and
  `ClassAType24B(Mmsi,ManufacturerIdentification)`. `Message` appends these
  payload alternatives at indices 5,6,7; old indices 0..4 are unchanged.
- [ais_encoder.h](../../ais/ais_encoder.h) and
  [ais_encoder.cpp](../../ais/ais_encoder.cpp): three additive overloads,
  checked validation before serialization; existing five serializer bodies
  unchanged. Even moved-from manufacturer metadata is rechecked before writing.
- [communication_state.h](../../ais/communication_state.h) and
  [.cpp](../../ais/communication_state.cpp): `kCommunicationStateCodecVersion=1`,
  checked signed-wide `SyncState`, immutable `ItdmaState19` factories/inspectors,
  immutable `SotdmaState19`, and distinct `SotdmaSlotOffset`, `SotdmaUtc`,
  `SotdmaCurrentSlot`, `SotdmaReceivedStations` variants. `decode` is strict;
  `raw()` is explicit. ITDMA exposes wire/effective increment, code/block
  length, keep and no-further-allocation separately.
- [classa_payload_tests.cpp](../../tests/classa_payload_tests.cpp) and
  [classa_wire_oracle.py](../../tests/classa_wire_oracle.py): original synthetic
  fixtures; no copied decoder, captured traffic or third-party fixture license
  added. Existing GPSD fixture/license paths remain unchanged.
- [CMakeLists.txt](../../CMakeLists.txt): one codec source addition, one C++
  test target and one Python CTest; both new tests labelled `host;AIS;classa`,
  C++17, `/utf-8`, `/W4`, `CUDA_VISIBLE_DEVICES=-1`.

Exactly four pre-existing tracked files changed (CMake and the three model/
encoder files); five new delivery files (two state files, two tests, this
report). Initial 114-file SHA256 inventory confirms all other 110 tracked
files remain byte-identical, including README/A0/old tests/world/runtime/CUDA/
profiles. No staging, commit, push, branch switch or remote write performed.
`EncodedPayload` retains its existing public value format; a caller can retain
a const value for a future immutable handoff. No physical-frame ABI is added.

## Qualified literal vectors

These are explicit synthetic metadata, not allocated identities or real sensor
observations. Type 3 uses MMSI `000000001`, negative longitude/latitude
`-73567890/-23456789`, ROT -42, SOG 123, COG 1234, heading 123, second 59,
status 5, repeat 1, manoeuvre 2, PA/low-power/RAIM true, ITDMA sync 2,
wire increment 4097, code 6, keep true (raw 327709). Type 5 uses MMSI
`123456789`, IMO 1234567, repeat 2, `LAB1234`, `AISMIXER LAB 01`, ship type
70, dimensions 100/25/7/9, EPFS 15, ETA February 29 23:59, draught 123,
destination `VARNA BG`, DTE ready. Class A 24B uses MMSI `000123456`, repeat
1, vendor `ABC`, model 3, serial `0x54321`, and the fixed fields above.

| Payload | Data bits / fill | SHA256 of ASCII `0`/`1` logical bits |
|---|---|---|
| Type 3 | 168 / 0 | `e1d82bd5f3dfd772fdd2cde083398238d8a96576d460b5fecf5c82140dddd622` |
| Type 5 | 424 / 2 | `b2b56a31de1246f08c2e3291d98d379739a1c4d3e9662c2b3ebddf3d7cf30f0c` |
| Class A 24B | 168 / 0 | `96603cba1f3aba5f9d3ff5a7f7a0172dbc3171f90477918bf05dca9e29db7ec8` |

Literal complete NMEA records (each line ends with bytes `0D 0A`; line breaks
in this Markdown block are not a substitute for that CRLF contract):

```text
!AIVDM,1,1,,A,3@0000EmQso>f;Ma`CrllSoo7@0M,0*77
!AIVDM,2,1,7,B,5Qmg=5L0Bm`Lh4;7;?@4U<lUPE:0h4:334000016<PI79tfosNmPDSPH0Qh0,0*52
!AIVDM,2,2,7,B,00000000000,2*20
!AIVDM,1,1,,A,H@07R@40123=D<Q0000000000000,0*2B
```

Complete byte lengths are 49; 82+33; 49 respectively. Sequence 7 and channel B
are explicit Type 5 fixture inputs. Full literal logical bit strings are pinned
in both new test files; Python independently assembles and extracts every
field, checks each literal/fingerprint/armor and reconstructs all 424 Type 5
data bits. Each new test executable's opt-in `--vectors` emits ASCII fields,
hex NMEA bytes and state vectors; legacy CLI output is unchanged.

## Executed qualification evidence

Final execution date: 2026-10-20. MSVC 19.44.35229.0 / x64; Python 3.14.7.
Independent CXX-only Release configuration/build and its five CTests: **PASS**,
0 failures, 0.88 s CTest wall time. It has no CUDA language/toolset, package
discovery, CUDA import or yaml-cpp link. PE dependency inspection of
`classa_host_tests.exe` shows only Windows and MSVC/UCRT libraries. The main
project retains its historical CUDA configuration prerequisite; this separate
compilation proves the new codec/test sources do not depend on it.

C++ reports **77 counted field checks**, **524288 exhaustive ITDMA wire cases**,
and **383 rejected invalid fixtures**. The 77 counter is endpoint/toggle
instrumentation, not the total number of assertions. Independent Python reports
**3 payloads, 56 exact fields, 16 state modes, 2 deterministic fresh processes**,
and **31 actual oracle rejections** (22 NMEA, 9 payload). Payload corruptions
mutate real fixture-protocol rows and exercise the verifier's rejection path,
including wrong message ID and truncated data. No production decoder or encoder
layout is imported into the reference implementation.

| C++ negative category | Rejected |
|---|---:|
| identity | 12 |
| ITDMA domain | 20 |
| manufacturer fields | 24 |
| moved-from manufacturer | 1 |
| NMEA | 12 |
| SOTDMA scalar domain | 16 |
| SOTDMA explicit slot context | 8 |
| SOTDMA absent/invalid UTC | 11 |
| SOTDMA untrusted raw decode | 10 |
| SOTDMA variant/timeout mismatch | 40 |
| sync state, including signed-wide malformed casts | 15 |
| Type 3 numeric | 24 |
| Type 3 position | 12 |
| Type 3 reserved/status | 13 |
| Type 5 ETA/calendar | 21 |
| Type 5 IMO/flag-state domain | 7 |
| Type 5 numeric | 24 |
| Type 5 reserved ship/EPFS | 83 |
| Type 5 text | 30 |
| **Total** | **383** |

Both native compilations emit identical new fixture-protocol bytes: 1783 bytes,
SHA256 `5ca21cbd05980375d06f6c150c5dac00015928963aa96062cff44a38f9b94cd2`.
The historical demo matches its pre-edit binary capture in both builds:
583 bytes, SHA256
`bb2838bded8b32ad4d8167109c23c1f4ee837c265c2eddea1f5c4d9f5a8f1314`.
Unchanged legacy C++ tests reject their original 330 invalid cases; legacy
Python still validates all five original complete NMEA vectors and GPSD fixture.

Full native Release: **35/35 PASS**, zero failed/skipped tests, 118.40 s CTest
wall time; labels include 10 GPU tests, 25 host tests, 8 AIS tests and 2 new
classa tests. All historical B3/B3.1/R0/W1 tests run unchanged, including
independent wire/CLI/journal and GPU checks. The separate complete CUDA-hidden
host suite with `CUDA_VISIBLE_DEVICES=-1`: **25/25 PASS**, zero failed/skipped
tests, 98.90 s CTest wall time. This includes the existing explicit
GPU-unavailable contract test and both new tests. There are no skips replacing
GPU tests in the native suite.

Cross-compiler build: **NOT RUN** (the established native MSVC toolchain was
used; no alternate compiler installed). G8 is satisfied by the separate
CUDA-free native compilation and five passing tests. Memory benchmark, physical
RF, network traffic, five-hour run and IEC certification: **NOT RUN**, outside
this slice. No corresponding success or conformity is claimed.

## Reproduction

From repository root, use the existing local VS2022 CMake/CTest binaries. The
following PowerShell names resolve the installed toolchain and read the
operator-owned preset without editing it; no local absolute path is checked
into this report. Configuration used the existing vcpkg toolchain and packages,
without installing anything or changing the preset's binary directory/options.

```powershell
$taskCmakeBin = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio/2022/BuildTools/Common7/IDE/CommonExtensions/Microsoft/CMake/CMake/bin'
$taskCmake = Join-Path $taskCmakeBin 'cmake.exe'
$taskCtest = Join-Path $taskCmakeBin 'ctest.exe'
$taskPreset = (Get-Content lab/CMakeUserPresets.json -Raw | ConvertFrom-Json).configurePresets | Where-Object name -eq 'cuda129-pascal'
$taskToolchain = $taskPreset.cacheVariables.CMAKE_TOOLCHAIN_FILE
$taskCuda = Join-Path $env:ProgramFiles 'NVIDIA GPU Computing Toolkit/CUDA/v12.9'
& $taskCmake -S lab -B lab/build/a1-s1-release -G 'Visual Studio 17 2022' -A x64 -T "cuda=$taskCuda" -DCMAKE_CUDA_ARCHITECTURES=61 -DBUILD_TESTING=ON -DAISMIXER_LAB_BUILD_SCENE_RUNNER=ON -DAISMIXER_LAB_BUILD_GEO_RUNNER=ON -DAISMIXER_LAB_BUILD_RUNTIME_RUNNER=ON -DAISMIXER_LAB_BUILD_WORLD=ON "-DCMAKE_TOOLCHAIN_FILE=$taskToolchain" -DVCPKG_TARGET_TRIPLET=x64-windows
& $taskCmake --build lab/build/a1-s1-release --config Release --parallel 4
& $taskCtest --test-dir lab/build/a1-s1-release -C Release --output-on-failure
$taskOldCudaVisible = $env:CUDA_VISIBLE_DEVICES
try {
    $env:CUDA_VISIBLE_DEVICES = '-1'
    & $taskCtest --test-dir lab/build/a1-s1-release -C Release -L host --output-on-failure
} finally { $env:CUDA_VISIBLE_DEVICES = $taskOldCudaVisible }
python lab/tests/classa_wire_oracle.py --exe lab/build/a1-s1-release/Release/aismixer_lab_classa_payload_tests.exe
git diff --check
```

The independent project used an ignored temporary CMake wrapper at
`lab/build/a1-s1-host-source/CMakeLists.txt`. For independent reproduction,
create a CXX-only project that compiles `lab/ais/{ais_bits,ais_encoder,nmea0183,
communication_state}.cpp`, exposes `lab/ais` as its include directory, and
links only that static library into `lab/tests/classa_payload_tests.cpp`,
unchanged `lab/tests/ais_codec_tests.cpp` and `lab/ais/ais_smoke_main.cpp`.
All four targets use C++17, `/utf-8`, `/W4`. Register those three executable
tests (smoke with `--self-test`) plus the new and original Python wire oracles
with `--exe` pointing at the corresponding executable, and set
`CUDA_VISIBLE_DEVICES=-1` for all five. No `enable_language(CUDA)`, CUDA/yaml
package lookup or toolchain file belongs in this wrapper.

Actual independent commands:

```powershell
& $taskCmake -S lab/build/a1-s1-host-source -B lab/build/a1-s1-host-only -G 'Visual Studio 17 2022' -A x64
& $taskCmake --build lab/build/a1-s1-host-only --config Release --parallel 3
& $taskCtest --test-dir lab/build/a1-s1-host-only -C Release --output-on-failure -V
```

The first scratch-wrapper configure attempt had a relative source-root typo;
it was corrected only in the ignored wrapper before building. No production
compile/test failure or baseline relaxation resulted. The full fresh CUDA
build retains toolkit deprecation warnings for sm_61 and CUB alignment warnings
in unchanged historical CUDA targets; the new host sources/tests compile
without warnings. Full command output, dependency inspection, pre/post demo
capture and scope inventory are retained under ignored
`lab/build/a1-s1-research/`. They are evidence artifacts, not delivery files.

## Final gates and handoff

| Gate | Actual disposition |
|---|---|
| G0 baseline | PASS: authorized branch/HEAD, initially clean index/worktree/untracked scope; no branch/history changes |
| G1 source/layout | PASS within the explicit qualified domains; original current tables/hash manifest verified; IMO-zero omission named |
| G2 Type 3 | PASS: 168 bits, ID 3, required ITDMA, negative positions and all field/default intervals; Type 1 unchanged |
| G3 Type 5 | PASS for explicit valid IMO/flag identity: 424 bits, edition 3, fill 2, complete independently reconstructed multipart NMEA; S1-NOGO-IMO-ZERO remains |
| G4 Class A 24B | PASS: 168 bits, Part B, explicit manufacturer-only model, unavailable particulars, EPFS/VDES separately verified |
| G5 semantic state | PASS for adapter v1 domains, all eight ITDMA codes/all SOTDMA modes, strict decode and context-aware next-frame builder; S1-NOGO-UTC-UNAVAILABLE remains |
| G6 independent qualification | PASS: independent layouts/literals/fingerprints, exhaustive ITDMA, field toggles, typed API separation and actual corruption rejection |
| G7 regression/hidden host | PASS: native Release 35/35; separate CUDA-hidden host 25/25; original goldens/tests unchanged |
| G8 independent compilation | PASS: separate CXX-only Release project, no CUDA/yaml dependency, 5/5 tests, identical new fixture output and legacy demo |
| G9 scope/integrity | PASS: four modified tracked files/five authorized additions, 110 other tracked files byte-identical, empty index, git diff --check, UTF-8/no BOM/LF/final-newline/trailing-whitespace checks |

Qualified **A1-S1 GO** for these payload/state domains. Unsupported IMO-zero,
absent UTC and contextless operational reservation claims are explicit limits,
not normative success. The codec can now supply exact logical payload bits and
semantic announcements to later station decisions. A1-S2..S5 still need their
own independent acceptance; no clock/entry/maps/RNG/cadence engine was built.
**A0-ISS-003 remains unresolved** and the affected A1-S4 first-frame gate stays
NO-GO. RF1 frame/FCS/two-slot validation, I1 ownership/causal/power-abort/catalog
integration and P1 receiver-specific UDP/five-hour pilot remain NO-GO until
their separate work is accepted. Full Class A compliance is not claimed.

Operator-only suggested commit (not performed):
`lab: implement A1-S1 Class A AIS payload and communication-state codecs`.
