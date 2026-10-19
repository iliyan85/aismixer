# A0 research, review and acceptance record

Review date: **2026-10-19**. Milestone: **analysis/documentation only**. Starting branch `lab`; HEAD `2f4bdfc0a0407916ddbb173dfd8f23c12d55ecb0`, subject `lab: add W1 maritime world and lifecycle engine`. Starting working tree/index clean; no untracked files and no existing A0 directory. No branch synchronization, staging, commit or push is authorized or performed.

**Outcome: documentation review package, with explicit open interpretations. A1 design freeze and a sustained closed-loop pilot remain NO-GO.** This is not an AIS simulation validation or completed SOTDMA implementation. Source availability is verified; unresolved current-source questions are retained rather than replaced by guessed algorithms.

## Source manifest and retrieval evidence

All sources were retrieved/reviewed on2026-10-19. The [official M.1371-6 record](https://www.itu.int/rec/R-REC-M.1371-6-202602-I/en) identifies **02/2026**, approval **2026-02-19**, **In force (Main)** and **Free Download**. Its English files were posted2026-05-26. The downloaded cover/body identifies the same recommendation and date. Source confidence is high for those observations; no inferred publication date is substituted for approval/retrieval.

| Source actually retrieved | Bytes | SHA-256 | Use |
|---|---:|---|---|
| [M.1371-6 English PDF](https://www.itu.int/dms_pubrec/itu-r/rec/m/R-REC-M.1371-6-202602-I!!PDF-E.pdf) | 3464843 | `126025db40f545454bbfc192c85353bd24bded48016f853ec184ef46f704a1dd` | Primary printed-page/figure authority;156 PDF pages, printed body1-154 |
| [M.1371-6 linked English Word](https://www.itu.int/dms_pubrec/itu-r/rec/m/R-REC-M.1371-6-202602-I!!MSW-E.docx) | 2132560 | `f75379e3a0725b1c096ba06a049fc4fcb00ddd81355ba97883a35382933b2d42` | Full paragraph/table reading, original embedded figures and footnotes; cross-checked against PDF |
| [M.1371-5 linked English Word](https://www.itu.int/dms_pubrec/itu-r/rec/m/R-REC-M.1371-5-201402-S!!MSW-E.docx) | 2641730 | `566aea1d261c6c6bd88df71a3fac03d332f46ca62942618e3940d2fced6fb7bb` | Targeted historical comparison only; not the normative basis |

The [official 2014 record](https://www.itu.int/rec/R-REC-M.1371-5-201402-S/en) was separately checked:02/2014, approved2014-02-18, superseded. It is not presented as the2026 source. The optional e-publication was not used; no claim of reading it is made.

Retrieval used direct HTTPS (web record/PDF text and Python standard-library `urllib.request` downloads). Word reading used ZIP/XML extraction of `word/document.xml` (5989 nonempty paragraphs including table cells), original embedded figure PNGs and `word/footnotes.xml` (24 substantive entries including the unnumbered asterisk). Tables were not trusted solely through flattened extraction: high-risk formulas/notes were checked in rendered PDF pages and original figure images. Existing Python lacked PDF parser/Poppler tools; no package or toolchain was installed. Local PDF rendering used Windows.Data.Pdf and source inspection used image viewing. Web screenshot calls returned text handles, sometimes timeouts, and were **not** counted as visual inspection.

Full source files, extracted full text, images, research notes and assembly/check helpers reside only under ignored `lab/build/a0-research/`. They are research cache, **not Git deliverables**. The ITU copyright is retained; no full document or source figure enters the change set. The five output documents use short paraphrases, source locations and project analysis rather than long quotations or reproduced normative tables.

## Actual reading log

This records distributed source reading and subsequent integration, not a claim that every PDF page was visually rendered. The complete current source text/tables/notes were read across the following non-overlapping assignments. All42 numbered figures were inspected as original source images or rendered pages; high-risk normative tables additionally received PDF spot checks. Reading all source material does not imply resolving every interpretation or importing every out-of-scope device requirement into the pilot.

| Reader / source coverage actually read | Printed pages / tables / figures | Result carried into package |
|---|---|---|
| Primary: cover, foreword, scope/glossary/recommends and complete Annex1 | preliminary;1-10; Tables1-2 and notes | Entity classes, MMSI authority, reporting schedule,2026 anchor condition, channels; no ClassB reporting substitution |
| Access reviewer: complete A2-3.1 and A2-3.3, every subsection/table/note; cross-read A2-4.3 | 17-23;27-50; Tables8-11,13-20; Figures3-5,9-19 | Clock/source hierarchy, local map/candidates, one-minute startup, Message3, entry exception, ITDMA/SOTDMA and source conflicts |
| Framing/network reviewer: complete A2-1, A2-2, A2-3.2, A2-4, A2-5 | 10-17;23-27;50-63; Tables3-7,12,21; Figures1-2,6-8,20-22 | OneTX/twoRX, physical timing/FCS boundary, regional/current-channel rules, reporting hysteresis, reuse, priority and transport |
| Primary: complete Annex3 | 63-64; Table22 and note | Message27, MSSA,75/76 alternation, base4+23 control/expiry; explicit limited pilot coverage |
| Primary: complete Annex4, including all IFM tables and notes | 65-76; Tables23-33 | Application identifiers and binary-function obligations inventoried as conditional; not mistaken for ClassA access control |
| Primary: complete Annex5 | 76-80; Figures23-31 | VDL transaction/ACK versus PI/application acknowledgement; addressed retries and unacknowledged broadcast distinction |
| Primary: complete Annex6, all physical/access/message-use tables and footnotes | 80-99; Tables34-43; Figures32-37 | ClassB CS carrier sensing/timing/initial Message18 explicitly not imported into ClassA entry |
| Message/interface reviewer: complete Annex7, all message schemas/notes and character table | 100-150; Tables44-85; Figures38-39 inspected by primary | Roles1/2/3, state width,5 metadata/two-slot structure,24B deadline, conditional commands/transport and additional device roles |
| Primary: complete Annex8 | 150-154; Tables86-89; Figures40-42 | Safety-device burst/identity rules inventoried, not used as ClassA autonomous access |
| Access reviewer and primary: all substantive Word footnotes | Including source references, unit conversions, silence authority, retry/assignment/device qualifications | Numbering differs by one after the initial asterisk; PDF printed labels used in requirements |

Access reviewer actually viewed rendered zero-based PDF indices18-23,30,32-34,36-51 (except35), including Figures9-16 and Tables14/16/18-20. Primary additionally viewed indices10,16,24-28,33,36,43,64-65,78-81,106-108,110-111,141. These are **viewed**, not merely generated. For example index10 is printed9/Table1, index33 printed32/Fig10, index36 printed35/Table16, index43 printed42/Fig15, index108 printed107/Tables47-48 and index141 printed140/24B deadline. Original Word images supplied Figures1-42 where not included in that rendered-page list. Analog certification parameters and other-class schemas were read/inventoried, not claimed simulated or independently certified.

## Claims spot-checked and historical comparison

| Claim / issue | Exact current-source check | Review result |
|---|---|---|
| One-minute passive startup | A2-3.3.5.1, printed38, Fig12; A0-MAC-011 | Confirmed; electrical ON does not mean TX-ready; short ON may emit zero |
| First ClassA TX and first-frame state | A2-3.3.5.2-.3, printed38-41, Figures13-14; A0-MAC-025/026 | Message3/ITDMA required; no Type1 shortcut; per-channel first-frame timing remains explicit review issue |
| RATDMA exception | A2-3.3.4.2.1, printed31; A0-MAC-018 | Network entry exempt from generic p-persistence; generic150-slot probability procedure remains separate |
| Both channels and shared TX | A2-2.1.4, A2-4.1.2; Table16/Fig11; A0-RF-001/013 | Independent local maps with one shared TX; satellite channels separately inventoried |
| Reporting | A1-4/Table1, printed9 and A2-4.3.1, printed54-55 | Current anchor exception/averaging and transition hysteresis retained;14kn/angle/missing-HDG boundaries not silently filled |
| State and Message5 | Tables18-20,46-48,50; printed48-50,105-110 |19-bit semantic state;3 usesITDMA;5 is424 data bits/two slots; NMEA separate |
| ClassA24B | A7-3.22, printed140 | Within12min startup/every24h; missing codec/metadata is duration-dependent blocker |
| Wire envelope | A2-3.2.2, printed23-27, Table12/Fig8; Table6, printed14 | Nominal slot, modulation and RF tail differ; non-integral1ms timing/profile remains open |
| Source inconsistencies | Actual PDF Fig10/Table14/Table16/Fig15 plus corresponding prose | Comparator, numeric and timeout issues verified visually; PDF/DOCX sign discrepancy retained |
| Exact OFF / normal end | Baseline runtime event.cpp phases and validator; W1 lifecycle declarations | Current schema cannot encode all required cuts or exact-end tie; no fake end/validator relaxation proposed |

The targeted2014 comparison read old Annex1/Table1, Annex2 startup/access and Table20, old Annex8 position Table48, Message5 Table52 and Message24 section3.22. It was **not a complete edition redline**. Current Table1 adds no-speed and anchor-motion conditions absent from the old table. Current position layout uses two spare bits plus transmit-power bit where old Table48 had three spare bits; existing B3 already models current bit147 explicitly. Current Message5 indicator3 identifies M.1371-6+, whereas the old table reserved it for future editions. Current ClassA24B startup/24h duty is explicit; the old section inspected had the interrogation response but not that duty. Extended ITDMA slot-count codes5..7/+8192 already existed in2014; they are not called a2026 addition. Both editions inspected retain the observation/ITDMA startup sequence; no historical B3 code or acceptance baseline is changed.

## Limits, unfinished decisions and independent review

Source confidence does not erase ambiguities. The CSV has14 unresolved requirement records, including overlapping contexts. Seven access issues are grouped in A0-GAP-023/A0-MAC-035..041; further questions cover14kn and angular/missing-heading edges, per-channel/low-rate first-frame timing, no-UTC timeout1 encoding, and RF-tail/rational-time conventions. A1 must not certify a guessed resolution as normative. Limited simulation policies require their own explicit version/scope and cannot close a disputed source PASS.

External sources **not read in full**: RR Article19 and the actual applicable M.585 edition; ISO/IEC13239:2002 complete CRC/serialization details; applicable IEC61162 sensor/presentation and IEC61993-2 conformance/testing editions; IMO identity/operational authority, CIRM manufacturer allocations, IALA/IMO application guidance and M.823 correction details. These remain `EXTERNAL_REFERENCE_REQUIRED` before corresponding conformance claims. Their edition or rules are not invented. The current recommendation's cross-references alone are not evidence of reviewing those texts. The official current source was accessible for this research; there was no need to substitute an older edition or request an operator copy.

Independent source reviewers checked the integrated draft for MAC, framing/network and message/interface claims. Corrections incorporated: current regional management does not revive historical nonzero transitional-zone machinery; slot-end232 is measured from T0, not a232-bit modulation duration; overlapping energy does not itself prove failed decode or remote allocation; timeout1 UTC fields remain availability-qualified; asymmetric knowledge distinguishes own reservation from unheard peer reservation; conditional assignment/poll tests now have concrete boundary oracles. No review constitutes IEC certification or a third-party ITU interpretation.

## Deliverables, counts and validation

Exactly five authorized files:

1. [A0_NORMATIVE_CLASS_A_SPEC.md](A0_NORMATIVE_CLASS_A_SPEC.md)
2. [A0_REQUIREMENTS_TRACEABILITY.csv](A0_REQUIREMENTS_TRACEABILITY.csv)
3. [A0_TEST_MATRIX.md](A0_TEST_MATRIX.md)
4. [A0_A1_INTERFACE_AND_GAP_REGISTER.md](A0_A1_INTERFACE_AND_GAP_REGISTER.md)
5. [A0_REVIEW_AND_ACCEPTANCE.md](A0_REVIEW_AND_ACCEPTANCE.md)

The CSV contains **128** unique requirement records: **89 NORMATIVE_EXPLICIT**, **2 NORMATIVE_DERIVED** (91 source-backed), **23 SIMULATION_POLICY**, **14 UNSPECIFIED_OR_CONFLICT**. There are **29** future test designs, covering all18 mandatory scenarios; ID gaps are intentional reserved numbers, not missing tests. Every record maps to at least one named test. The test matrix contains the machine-checked inverse requirement index.

There are **23 gap entries**: **12 unconditional implementation blockers**, **1 blocking source-adjudication group**, **6 conditional capability blockers**, **4 open review/external entries**. Thus13 entries are unconditionally BLOCKING for their stated acceptance claims, but none calls for implementation in A0. A five-hour pilot additionally reaches the24B deadline. A1/RF1/I1/P1 acceptance remains NO-GO/NOT IMPLEMENTED as specified in the register.

Validation record is completed by the final documentation-integrity check below. It checks document structure and scope, not AIS behavior.

<!-- FINAL_VALIDATION -->

- Ending branch/HEAD verified unchanged: `lab` / `2f4bdfc0a0407916ddbb173dfd8f23c12d55ecb0`.
- All109 pre-existing tracked files were compared by SHA-256 with the captured starting inventory: **zero content changes**. This includes README, runtime, world, codecs, tests, YAML, presets, dependencies and historical baselines. No pre-existing tracked file was added/removed. `lab/lab.zip` remains absent and untouched.
- `git diff --check`: **PASS**. Since ordinary diff excludes untracked documents, raw-file `git diff --no-index --check -- NUL <file>` was additionally run for each of the five new documents with command-local `core.autocrlf=false` and `core.whitespace=trailing-space,space-before-tab,cr-at-eol`: **PASS**, zero diagnostics. This recognizes the CSV's required CRLF; exit1 denotes a new-file difference. No repository Git configuration changed. Direct UTF-8/trailing-whitespace/final-newline checks also passed.
- CSV strict parsing and RFC4180 parse/write byte roundtrip: **PASS**; all15 columns,128 unique IDs, required source fields/official URLs and valid PDF page anchors checked. Policies explicitly identify project authority; unresolved records are not counted as settled normative requirements.
- Cross-file requirement/test references,29 test headings, all18 mandatory scenarios, exact inverse coverage,23 gap entries and local Markdown links: **PASS**. No undefined requirement/test ID or missing mapping was found.
- `git diff --name-only` and `git diff --cached --name-only`: empty. Index untouched. Exact `git status --short --untracked-files=all`:

```text
?? lab/docs/A0_CLASS_A/A0_A1_INTERFACE_AND_GAP_REGISTER.md
?? lab/docs/A0_CLASS_A/A0_NORMATIVE_CLASS_A_SPEC.md
?? lab/docs/A0_CLASS_A/A0_REQUIREMENTS_TRACEABILITY.csv
?? lab/docs/A0_CLASS_A/A0_REVIEW_AND_ACCEPTANCE.md
?? lab/docs/A0_CLASS_A/A0_TEST_MATRIX.md
```

`git check-ignore` confirms all downloaded source copies are ignored under `lab/build/a0-research/`. No source PDF/Word/image, binary, package, config or implementation appears in the deliverable status. No staging, commit or push was performed. These checks validate documentation integrity and preservation of the baseline; they do not execute the future cases.

**Build: NOT RUN. CTest: NOT RUN. Historical runtime/world/codec/CUDA acceptance: NOT RUN. All A1/RF1/I1/P1 cases: NOT RUN.** This text-only milestone does not require rerunning historical suites. No radio outcomes, FCS vectors or performance measurements are fabricated.

## Release gate self-audit

| Gate | Outcome and practical limit |
|---|---|
| G0 baseline/source | PASS: required branch/HEAD/clean baseline and official current source verified; no reset or sync |
| G1 actual source reading | PASS as research coverage: all current annex text/tables/notes and figures read through distributed review; PDF spot checks and limits recorded above |
| G2 mandatory source facts | PASS as documentation check: observation, first3, access phases, channels, cadence/state and partial-OFF dependency explicit; disputed interpretations remain open |
| G3 local knowledge | PASS as specified contract/test design; not executed: no omniscient map, future/foreign token, synthetic success or global slot allocator |
| G4 end-to-end route | PASS as interface/dependency specification: own W1 -> explicit clock/input -> local FSM -> physical TX request -> future owned RF feedback; missing versions/functions visible |
| G5 genuine first emission | A1 NO-GO explicitly enforced by missing Message3, frame/input/FSM dependencies; A0 does not waive it |
| G6 requirements/oracles | Traced with unresolved atoms; **NO normative A1 freeze/PASS** for disputed source rules |
| G7 cross-file/CSV | See actual documentation validation below; no fabricated execution or long source passages |
| G8 change control | Only five new authorized documents; existing files/hash inventory, unstaged/index scope and whitespace checked; no commit/push |

## Operator review checklist

- Review the seven grouped access questions and additional first-frame/degraded/angle/tail questions before approving a normative A1 oracle.
- Choose an explicit pilot capability/duration/input profile, including independent direct UTC assumptions and metadata; no Type1 startup shortcut or silent static/24B omission.
- Review versioned physical-frame/abort/catalog/token contracts and exact-end/same-phase causality before I1 work.
- Acquire/review actual external editions for identity, CRC, presentation and any claimed conformance scope.
- Review requirement/test coverage and preserve the distinction between unit fixtures, physical RF evidence and later playback.
- Retain operator control of staging/commit/push. Suggested commit message only: `lab: specify normative Class A AIS protocol for A1`.
