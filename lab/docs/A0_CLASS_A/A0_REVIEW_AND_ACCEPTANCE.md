# A0 research, review and acceptance record

> Historical A0 account: the original counts, HEAD/status, execution results and readiness statements below record A0 before commit `964c2a149d1de2fae128c1c6d950cce3622b3d20`. They are not current A0.1 counts or worktree results. The distinct A0.1 addendum at the end controls the present limited-profile decisions. The explicit false source-sign claim is corrected in place with its cause identified.

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
| Source inconsistencies | Actual PDF Fig10/Table14/Table16/Fig15 plus corresponding prose | Comparator, numeric and timeout issues verified visually; A0.1 correction: both formats print minus25, while flattened Word text omitted Symbol F02D. The contradiction is internal to the table/formula, not between PDF and DOCX |
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

## A0.1 addendum - 2026-10-19

This addendum is the **current** documentation correction and limited-profile record. Its starting and ending baseline is branch `lab`, HEAD `964c2a149d1de2fae128c1c6d950cce3622b3d20`, subject `lab: specify normative Class A AIS protocol for A1`. Starting worktree/index were clean. The historical A0 counts/status/results above remain historical, except the explicit false PDF-versus-Word sign statement corrected in place. No older executed result is changed or relabelled as an A0.1 execution.

**D0 documentation: GO**, subject to the final integrity result recorded below. **D1: CONDITIONAL GO for isolated A1-S1 codec/state units only. D2 five-hour RF-closed-loop UDP: NO-GO. Full ITU/IEC Class A conformance: NOT CLAIMED.** The frozen feature boundary and eight decisions are `LCP-CLASSA-AUTO-TERR-v1`, not a certified or fully frozen end-to-end FSM. A0-ISS-003 still blocks the affected A1-S4 first-frame interpretation/acceptance, including the anchored startup path. It is not silently waived to obtain D1.

### Input review versus independently checked evidence

The operator supplied independent Claude findings CL-A0-001..007 and a reported conditional-GO recommendation. Those are **reported review input**, not Codex-performed inspection or an official ITU erratum. Codex performed the focused verification below before applying them. A0.1 did not repeat a broad reading campaign or claim a fresh complete edition redline.

The official record was freshly opened and the current English PDF and Word were freshly downloaded from the exact official links in the source manifest. Both downloaded byte counts and SHA-256 hashes match the historical manifest and hash-matched cached copies exactly: PDF3464843/`126025db40f545454bbfc192c85353bd24bded48016f853ec184ef46f704a1dd`; Word2132560/`f75379e3a0725b1c096ba06a049fc4fcb00ddd81355ba97883a35382933b2d42`. **No network fallback or source-access failure occurred.** External IEC/ISO/identity authorities were not reacquired and remain gated for their corresponding claims; no substituted source edition is used.

Focused source inspection used original Word XML (including `w:sym`, not only `w:t`), source figure images and Windows.Data.Pdf rendering. The source reviewer visually inspected zero-based PDF indices9,10,28,30,32-37,39-44,49,53-58,109-111,125-126,141-142; other navigational context was not claimed as new conformance coverage. The primary additionally inspected current PDF indices10,34,49,57,110,111,141 and the original Word Symbol node. Printed page remains index minus one. The checked scope was Table1/A1-4, A2-3.3.1.2/Figures9/20, Tables14-16/Figures10/11, A2-3.3.5.4/Figure15/Tables18-19, A2-4.3.1, Message5 and ClassA24B. This is focused evidence, distinct from the historical complete-source log.

New full-source copies, symbol proof, rendered images, focused notes and scratch scripts are only in ignored `lab/build/a01-research/`; old `a0-research/` material was read and preserved. No full source, figure, source-extraction transcript or helper script is tracked. Source paraphrases/short numeric derivations and precise references are used, not long copyrighted passages.

| Finding | Independently checked evidence and A0.1 adjudication | Current atom / oracle |
|---|---|---|
| CL-A0-001: hidden minus | Table14 printed33 has minus25 in both formats. Original Word has Symbol `w:sym char=F02D` immediately before25; flattened `w:t` dropped it. The maximum column25 and100/4 disagree with that note, while priority1/0 conflicts with the four levels in A2-4.2.3 printed53. RTPI's printed bounds also do not prescribe rational rounding. Delete the nonexistent inter-format discrepancy; retain internal source defects. | A0-MAC-041 stays unresolved; P04/A0-SIM-012 supplies explicit representation. T09/T19: equality,26/101, fractions and no source-bound clamp. |
| CL-A0-002: own adjacency | Figures9/20, printed29/56, support j-1,j,j+1 exclusion. In Fig20 own B10 excludes A9/10/11; A8 is still a lower-priority candidate due to base B8, while A12 has its own-channel base exclusion. | A0-MAC-040 becomes derived; P05/A0-SIM-013. T05/T08/T19 check j-2..j+2 and wrap with unrelated reservations separated. |
| CL-A0-003: timeout | Figure15 printed42 has one decrement before zero; prose printed42-43 mentions it twice. Table18 printed48 defines remaining frames/zero last use. The reviewed continuous-domain reading advertises3->2,1,0 and7->6..0; new reservation timeout does not replace the expiring old-slot zero. | A0-MAC-037 becomes derived; P02/A0-SIM-010. T06/T08/T17/T19 independent countdown/offset evidence; no official correction claimed. |
| CL-A0-004: RATDMA comparator | Prose printed31 says sum<4; Fig10 printed32 draws>4. The latter can fail peer-loss replenishment or loop without progress. Adopt the bounded prose reading, retaining both printed locations. | A0-MAC-036 becomes derived; P03/A0-SIM-011. T09/T19 sums3/4/5, peer loss without attempt, real candidates only and unchanged absolute RTES. |
| CL-A0-005: SI | Table16/Fig11 printed35-37 give exact rational half-width NI/10. For allowed autonomous NI75/125/225/375, integer centers give floor windows of15/25/45/75. NI375/NS1000 includes+37 but excludes+38. | A0-MAC-039 becomes derived only in that domain; P01/A0-SIM-009. T05/T08/T19/T20 include signed wrap and deferred assigned37.5. |
| CL-A0-006: NI maximum | Table16 printed35 retains1225, while2250/Rr withRr>=2 gives1125. Table15 printed34 and Message16/Table65 printed125 corroborate1125 in their distinct FATDMA/assignment contexts; they are not assumed identical to SOTDMA. LCP autonomous maximum is375. | A0-MAC-035 becomes formula-derived with literal discrepancy retained; T05/T19. No ITU erratum or unrestricted assigned-mode solution claimed. |
| CL-A0-007: low rates | A2-3.3.4.1/Table16 note2 and A2-4.3 disagree for[1,2)reports/min. Ordinary anchored1/3/min is below both thresholds. | A0-MAC-038/A0-RF-016 stay unresolved; disputed assignment/Message23 excluded, anchor retained. T10/T19/T29 positive anchor and negative out-of-profile cases. |

The additional source checks confirm that Table1 overlaps exactly14kn, A2-4.3.1.2 distinguishes faster/slower than2kn without equality, and the circular mean/undefined resultant is not prescribed. A1-4 establishes ordinary Type5 six-minute/amendment/request duties without an explicit startup first-due origin; A7-3.22 explicitly establishes ClassA24B within12min and every24h. These limits are not erased by project decisions.

### Versioned decisions and remaining gates

The full decision records (scope, preconditions, rule, reason, exclusions, independent oracle and change condition) are in [the specification](A0_NORMATIVE_CLASS_A_SPEC.md). All eight new policy atoms are `SIMULATION_POLICY`; where a policy adopts a reviewed derivation, that derivation remains a separate MAC atom.

| Decision | Atom | Source versus project status / frozen scope |
|---|---|---|
| LCP-P01 | A0-SIM-009 | Applies derived MAC039 integer autonomous floor windows; assigned/fractional/general endpoints and first-frame origins are not solved. |
| LCP-P02 | A0-SIM-010 | Applies derived MAC037 single pre-check continuous decrement; preserves repeated-prose conflict and excludes other countdown modes. |
| LCP-P03 | A0-SIM-011 | Applies derived MAC036 prose<4; finite actual-candidate scans, pending/exhausted decisions and immutable RTES are explicit pilot policy. |
| LCP-P04 | A0-SIM-012 | Exact pre-update rational recurrence, uniform integer0..100, inclusive comparison and no bound-clamping are numerical policy. At25,26 of101 values pass. Printed table defects remain unresolved source facts. |
| LCP-P05 | A0-SIM-013 | Applies derived MAC040 j-1/j/j+1 in both directions/wrap; shared TX and independent reservations remain. |
| LCP-P06 | A0-SIM-014 | Policy at exactly14kn/lower band and exactly2kn/no new heading-driven increase; unit-vector COG mean, rho<=2^-40 undefined and signed-angle convention. No fabricated HDG/ROT or override of source-valid<2kn default reversion. |
| LCP-P07 | A0-SIM-015 | Policy ON-epoch first Type5<=ON+360s; source first24B<=ON+720s; subsequent confirmed-physical-TX anchors and short-ON handling explicit. Queue after actual first-frame without moving deadlines. Blocked/missed is not successful TX;24h is beyond five hours. |
| LCP-P08 | A0-SIM-016 | Separately versioned semantic Philox4x32-10, unbiased bounded rejection and independent station/purpose/epoch ledger are project choices; no W1-purpose reuse, actual RNG code or seed-serialization acceptance delivered. |

Two minimal new issue atoms preserve missing distinctions: **A0-ISS-005** for Type5's unspecified first-due origin and **A0-ISS-006** for24B relevant epoch/short-ON/recurrence applicability. Existing A0-ISS-001 and A0-RF-019 already cover the14kn,2kn and circular-angle source gaps, so no duplicate issue IDs were added for those topics. Those issues remain source-unspecified despite policy choices. All map to meaningful existing tests and the exact reverse index.

The limited profile is explicitly non-conformant as a full-ClassA capability claim: two one-TX/two-RX stations on AIS1/2, own-only W1/clock/EPFS inputs with provenance, unique synthetic syntax-valid MMSIs, one designated independent passive acceptance receiver, local knowledge and independent overlapping requests. It excludes satellite/commands/assignment/polling/base/degraded clocks/repeater/silent controls/transport/full IEC integration. Type2 and24A are optional later, not A1-S1 gates; existing4/9/18/21 are not substituted ClassA duties. A later UDP endpoint must be operator-approved loopback/isolated and synthetic-labelled. No traffic is generated here.

The historical W1 YAML and digest are untouched. A future separate versioned long-ON configuration recommends guaranteed>=30min ON spans with varied OFF, plus an optional independently controlled continuously-ON five-simulation-hour fixture. Its seed, interval bounds, applicability and datum manifest are explicit; extended ON ensures deadlines are reached, not that legal candidates/physical success exist. Future RF1 must use declared synthetic ellipsoid heights or a justified geoid/tide conversion, never treat antenna offset/AMSL as surveyed WGS84 height. Five hours reaches Type5 recurrence and initial24B, not24h recurrence.

All23 gap IDs remain. A0-GAP-023 now records reviewed restricted readings, not a blanket bar to isolated coding. Entries001-012 are **12 implementation dependencies**, not a prohibition on engineering;013-018 remain six capability/duration/output entries (014 and018 apply to this pilot);019-022 are four open/external/review entries;023 is one reviewed-profile entry with residual conflicts. A0-ISS-003 remains pilot-critical for S4. Other unresolved assigned-rate/fractional/no-UTC and external claims stay excluded/gated. RF1/FCS, I1 live ownership/epochs/abort/journal and P1 playback gates remain separate.

| Gate | Current result / boundary |
|---|---|
| D0 documentation | **GO** after the actual checks below; only the limited feature boundary and reviewed decisions are frozen, not an invented complete FSM. |
| D1 A1-S1 isolated coding | **CONDITIONAL GO** for separately staged Type3/5/ClassA24B and semantic ITDMA/SOTDMA state with independent bit oracles, existingType1 retained. No Type2/24A/RF/FCS requirement is smuggled into this slice. |
| A1-S4 first-frame freeze/acceptance | **NO-GO** until ISS003 gets an annotated staggered-channel/anchored low-rate trace and operator/reviewer adjudication. S1 codec work does not resolve this rule. |
| D2 five-hour RF-closed-loop UDP | **NO-GO** until actual codecs/FSM/RF/FCS, datum-valid geometry, live ownership, interrupted-frame/additive journal semantics, causal feedback, playback and receiver-specific isolated UDP tests pass. Existing StationOff/open-TX validation is not loosened. |
| Full ITU/IEC conformity | **NOT CLAIMED**; excluded capabilities and external authorities remain necessary for any later full claim. |

### Current counts and document validation

The current CSV has **138** unique requirements: **89 explicit + 7 derived + 31 policies + 11 unresolved = 138**; source-backed count is96. Historical A0 was128=89+2+23+14. Five original unresolved MAC atoms035/036/037/039/040 become derived:2+5=7 and14-5=9. Adding eight policy atoms and two unresolved issue atoms gives23+8=31,9+2=11,128+10=138. No other original status changed.

All128 original requirement IDs, all29 original test headings (including mandatory01-18), all23 original gap IDs and all15 CSV columns are preserved. Original source URLs are unchanged; applicable table/page/derivation evidence is extended. The21 amended original atoms are SIM002/008; ISS001/003/004; MAC014/019/021/024/027/035/036/037/038/039/040/041; RF016/019; MSG015; INT012. Their changes correct findings or downstream profile/trace wording;107 original records are otherwise unchanged. Exactly10 IDs are added: ISS005/006 and SIM009..016. No new test heading or gap row is padded into the count.

Documentation-only checks actually executed on2026-10-19:

- `python lab/build/a01-research/check_a01.py`: **PASS**. It strictly parses/byte-roundtrips RFC4180 CSV; compares the original15-column order,128 IDs, unchanged rows/source URLs and status math; checks29 original tests/all18 mandatory,23 gap rows, no dangling requirement/test/gap refs, every requirement-to-test relation and exact inverse index; checks local Markdown links, official PDF URL/page anchors, UTF-8/no BOM, per-file newline conventions and trailing whitespace/final newline.
- Focused source acquisition/hash/XML/render checks: **PASS**, as recorded above. Source URLs were actually fetched, not merely pattern-checked; row page fragments were range-checked and altered printed references visually/source-checked. External sources remain unreviewed, as stated.
- `python lab/build/a01-research/reference_checks.py`: **PASS** for independent SI set widths/wrap/1037-versus1038, remaining-count sequences, exact rational thresholds,101-value probability cardinality,32-bit rejection threshold, circular-resultant examples and the original Word Symbol node. The historical2014 source URLs also returned HTTP200 to HEAD requests; no new2014 source reading is claimed. A normalized exact26-word source-window scan found no matches in the five outputs, alongside manual review for short paraphrases/no copied figures or long quotations. These are document-oracle/source checks, not execution of any future A0-T/A1/RF test or protocol engine.
- `git diff --check`: **PASS**. CSV is deliberately CRLF exactly as the committed working-copy baseline; Markdown remains LF. Git may report its existing CRLF-to-LF normalization notice; no Git configuration was changed and the working-copy CSV convention is preserved.
- Scope/hash checks: **PASS**. Exactly the five authorized tracked documents differ, no new tracked/untracked deliverable, no staged/index diff, branch/HEAD unchanged. All109 other tracked files hash-identical to preflight, including W1 YAML/README/runtime/codecs/tests/build/goldens; old source PDF/Word hashes still match. No implementation or binary/package/configuration change, staging, commit, push or output traffic occurred.

<!-- A01_FINAL_SCOPE -->

Final `git diff --stat` is restricted to **five files changed**. Exact ending `git status --short --untracked-files=all`:

```text
 M lab/docs/A0_CLASS_A/A0_A1_INTERFACE_AND_GAP_REGISTER.md
 M lab/docs/A0_CLASS_A/A0_NORMATIVE_CLASS_A_SPEC.md
 M lab/docs/A0_CLASS_A/A0_REQUIREMENTS_TRACEABILITY.csv
 M lab/docs/A0_CLASS_A/A0_REVIEW_AND_ACCEPTANCE.md
 M lab/docs/A0_CLASS_A/A0_TEST_MATRIX.md
```

`git diff --cached --name-only` is empty. Branch is still `lab`; ending HEAD is still `964c2a149d1de2fae128c1c6d950cce3622b3d20`. No sixth tracked document or untracked deliverable was added; research/check material is ignored under `lab/build/a01-research/`. D0's final scope/trace checks pass against this state.

**Build/CTest: NOT RUN. Historical regression suites: NOT RUN. A1/RF1/I1/P1 tests: NOT RUN.** No physical transmission, decoded RF/FCS result, NMEA/UDP delivery or performance measurement was produced. The test matrix remains future design, with separate SOURCE-DERIVED, POLICY and unresolved/out-of-profile expectations.

Remaining operator/reviewer work before later slices includes ISS003's first-frame trace, concrete future synthetic metadata/clock/duration/datum choices, A1-S3 RNG serialization, external RF/FCS authorities and I1 power/abort/version design. These do not waive independent acceptance evidence. Recommended next engineering scope is **A1-S1 only**, under D1; no A1 code is part of this patch. Suggested **operator-only** commit message: `lab: reconcile A0 Class A rules and freeze autonomous terrestrial pilot profile`.

## A0.2 addendum - 2026-10-23

This addendum is the **current** documentation record for the operator-approved first-frame policy. The historical A0 and A0.1 sections above (counts, HEADs, source downloads, tool output, executed checks and NO-GO decisions) are unchanged. Staged history: A0 (128 requirements) -> A0.1 (138 requirements, D1 conditional for isolated A1-S1 only) -> A1-S1/S2/S3 isolated host-unit acceptance -> A1-S4.0 documentation adjudication -> A0.2 (140 requirements; a bounded policy enables S4-A engineering after S3.1).

### A. Baseline and change scope

Starting and ending branch `lab`, HEAD `caa43f05027bb4ce6839e944656fbc6e52b11442`, subject `lab: adjudicate A1-S4.0 first-frame rules and freeze the S4 causal contract`, exactly the expected baseline. Starting worktree and index were clean. Exactly five tracked documents are edited: [A0_NORMATIVE_CLASS_A_SPEC.md](A0_NORMATIVE_CLASS_A_SPEC.md), [A0_REQUIREMENTS_TRACEABILITY.csv](A0_REQUIREMENTS_TRACEABILITY.csv), [A0_TEST_MATRIX.md](A0_TEST_MATRIX.md), [A0_A1_INTERFACE_AND_GAP_REGISTER.md](A0_A1_INTERFACE_AND_GAP_REGISTER.md) and this file. No sixth tracked file, no change to the three A1-S4.0 documents, no code, test, CMake, profile, golden, README or branch-history change. Private scripts, the pre-edit baseline copies of the five documents and the tracked-file SHA-256 inventory live only in ignored `lab/build/a0-2-research/`.

### B. Source lineage

Authority for A0.2 is the reviewed A1-S4.0 research: [first-frame normative adjudication](../A1_CLASS_A/A1_S4_0_FIRST_FRAME_NORMATIVE_ADJUDICATION.md), [causal event and interface contract](../A1_CLASS_A/A1_S4_0_CAUSAL_EVENT_AND_INTERFACE_CONTRACT.md) and [review and gate](../A1_CLASS_A/A1_S4_0_REVIEW_AND_GATE.md). A0.2 inherits their recorded source evidence for ITU-R M.1371-6 (02/2026): PDF SHA-256 `126025db40f545454bbfc192c85353bd24bded48016f853ec184ef46f704a1dd` and Word SHA-256 `f75379e3a0725b1c096ba06a049fc4fcb00ddd81355ba97883a35382933b2d42`, the record page status "Approved 2026-02-19, in force", figures read from the Word embedded images and the explicit limit that the PDF figure pixels were not rasterized (A1-S4.0 FF-20 UNVERIFIED). No official file was re-downloaded for A0.2; this is a transcription and cross-index campaign, not a fresh source verification. No older edition was consulted and no figure or long passage was reproduced.

### C. Reviewed choice and preserved uncertainty

A1-S4.0 Interpretation II (per-channel first-frame ITDMA chains, each anchored at that channel's first NTS, with a per-channel terminal Message 3 of effective increment zero at NTS_first + 2 250, keep TRUE) is recorded as SIMULATION_POLICY `CLASSA-FIRST-FRAME-v1`, new atom A0-SIM-017, limited to two-channel autonomous Rr in {6, 10, 18, 30}. The source-explicit parts (60 s elapsed observation, Message 3 first, channel-scoped ITDMA state, entry exempt from the probability gate, keep during the first frame, continuous SOTDMA after the zero) and the derived parts (anchor at the first NTS, one-frame duration of 2 250 slots) keep their own markings in the specification addendum. A0-ISS-003 remains `UNSPECIFIED_OR_CONFLICT` with its test mapping intact and a minimal cross-reference added; the Rr < 2 execution question is split into the new unresolved atom A0-ISS-007. The policy is not LCP-P09, not an ITU addendum and no conformance claim; LCP-P01..P08 are unchanged.

### D. Why low rate remains a substantive blocker

The anchored/moored 3 min interval is a Table 1 source obligation and periodic ITDMA below two reports per minute is a normative classification, so the obligation is not waived. The source gives no low-rate entry/first-frame execution: the selection window lies outside the frozen LCP-P01 integer domain (the Table 16 formula would give +/- 675 slots at NI 6 750, which the S3 `selection_interval` rejects by design), the NSS/NTS forward reach exceeds one frame, keep semantics over 3 min same-channel intervals are undefined, Figure 14's terminal zero presupposes a same-channel NTS inside one frame, the [1,2)/min threshold texts conflict (A0-MAC-038/A0-RF-016), and LCP-P07 duties can fall due before a late first opportunity. The limited implementation must therefore return `blocked_low_rate_policy_absent` and no intent at Rr < 2 until a separately operator-approved, versioned `CLASSA-LOWRATE-ITDMA-v1` and an LCP-P01 revision exist; the A1-S4.0 Section 7.3 draft values are a non-adopted research proposal.

### E. Integration gates

S3.1 (side-effect-free rejection in `StationKnowledge::accept_evidence`, explicit station-local `synchronize` at the phase-2/phase-3 boundaries, tests for post-horizon expiry/capacity and post-synchronize throws) is GO TO IMPLEMENT in a separate campaign and is not PASS. S4-A (intent-only entry and per-channel first frame at Rr in {6, 10, 18, 30}) is CONDITIONAL GO TO IMPLEMENT only after S3.1 and this policy cross-index; it is not S4-A acceptance and not a real sender. Full A1-S4, RF1, I1, S5, P1 and the five-hour RF-closed-loop UDP pilot (D2) stay NO-GO. Full ITU/IEC Class A conformance is not claimed.

### F. Counts and cross-file integrity actually run

The CSV now has **140** unique requirements: **89 explicit + 7 derived + 32 policies + 12 unresolved = 140** (A0.1 had 138 = 89 + 7 + 31 + 11). Exactly two IDs were added, A0-SIM-017 and A0-ISS-007; no original ID was removed, renumbered, duplicated or reclassified; among the 138 originals only the `expected_behavior` field of A0-ISS-003 changed (cross-reference), and its status and test mapping are intact. All 29 test headings (18 mandatory), all 23 gap rows and all 15 CSV columns are preserved; the `## Requirement coverage index` was regenerated from the final CSV in CSV row order and the two new IDs appear in the A0-T03/T10/T19 and A0-T10/T19/T29 rows respectively. Working-copy line endings: the CSV is written LF to match its committed (index) convention `text=auto eol=lf`; Markdown remains LF.

Documentation-only checker executed on 2026-10-23 from the repository root (ignored, not a tracked test): `python lab/build/a0-2-research/check_a02.py`. It parses the CSV with `csv.DictReader` (UTF-8, `newline=''`), verifies the 15-column order, unique IDs, the 140-atom and 89/7/32/12 counts, the RFC4180 fully-quoted byte roundtrip, non-empty mandatory fields, `test_ids` syntax, the exact two-ID addition against the pre-edit baseline copy, unchanged original statuses and fields, the A0-ISS-003/A0-ISS-007/A0-SIM-017 statuses; the 29 unchanged test headings and the two-way equality of the coverage index with the CSV inverse; the 23 gap rows and the A0-GAP-019 cross-reference; the A0.2 markers in A0-T03/T10/T19/T29, the policy statements in the specification and the preserved historical A0/A0.1 sentences; every relative link in the four Markdown documents; UTF-8 without BOM, LF only, final newline, no trailing whitespace and no NUL in all five files; `git diff --check`; branch, HEAD, empty index, exactly five modified files and byte-identical SHA-256 for every other tracked file against the preflight inventory. Its final output is recorded below (section I).

### G. Decisions

- **D0 A0.2 documentation: PASS** on the basis of the checker result in section I; a failing check would have made D0 NOT PASS without editing away the check.
- **D1 S3.1: GO TO IMPLEMENT** in a separate campaign; not already PASS.
- **D1 S4-A: CONDITIONAL GO TO IMPLEMENT** only for isolated intent-only engineering at Rr in {6, 10, 18, 30} once the S3.1 prerequisite and the policy cross-index are satisfied; not a real sender and not S4-A acceptance.
- **Rr < 2: NO-GO** until a separately reviewed and adopted low-rate policy and an LCP-P01 extension exist; the standard's low-rate ITDMA obligation is not waived.
- **D2 five-hour RF-closed-loop UDP pilot: NO-GO. Full ITU/IEC Class A conformance: NOT CLAIMED.**

### H. What was and was not run

Run: the documentation checker above, `git diff --check`, `git status`, `git diff --cached`, `git ls-files`/SHA-256 inventory comparison and the index regeneration script. NOT run: any build, CTest, CUDA, S1/S2/S3 suites or oracles, RF1, I1, P1, NMEA/UDP activity, PDF rasterization or any five-hour radio pilot. No compiler, test or source-execution PASS is claimed for A0.2; the A0 test designs A0-T01..T35 and the A1-S4.0 cases T4-01..T4-20 remain DESIGN ONLY.

### I. Final Git scope and checker output

Command (repository root, 2026-10-23): `python lab/build/a0-2-research/check_a02.py`, exit code 0, 87 PASS lines and 0 FAIL lines. This is the third run. Run 1 reported one failure because the specification sentence had bold markers inside the phrase `not retroactively LCP-P09`; the sentence was re-bolded with its meaning unchanged. Run 2 crashed inside the checker itself: its inventory reader expected the two-space `sha256sum` text format while the preflight inventory uses the binary-mode `digest *path` format; the checker was corrected and no document was changed for it. Recorded output of run 3:

```text
PASS CSV has exactly the 15 original columns in order
PASS every CSV row has exactly 15 fields
PASS CSV requirement IDs are unique
PASS CSV has 140 atoms (found 140)
PASS status counts 89/7/32/12 (found {'NORMATIVE_EXPLICIT': 89, 'SIMULATION_POLICY': 32, 'NORMATIVE_DERIVED': 7, 'UNSPECIFIED_OR_CONFLICT': 12})
PASS RFC4180 fully-quoted LF byte roundtrip of the CSV
PASS no empty mandatory CSV field
PASS every test_ids field parses
PASS baseline CSV had 138 atoms
PASS exactly A0-SIM-017 and A0-ISS-007 were added (found ['A0-SIM-017', 'A0-ISS-007'])
PASS all 138 original IDs retained in order, none renamed
PASS only A0-ISS-003.expected_behavior changed among originals (found [('A0-ISS-003', ['expected_behavior'])])
PASS original 138 statuses unchanged
PASS A0-ISS-003 stays UNSPECIFIED_OR_CONFLICT
PASS A0-ISS-007 is UNSPECIFIED_OR_CONFLICT
PASS A0-SIM-017 is SIMULATION_POLICY with a blank source_url
PASS A0-ISS-003 test mapping intact
PASS A0-ISS-003 cross-references A0-SIM-017 and A0-ISS-007
PASS 29 unique test headings (found 29)
PASS test headings unchanged from baseline
PASS all 18 mandatory scenarios present
PASS every CSV test ID is a matrix heading and vice versa
PASS coverage index equals the CSV inverse relation exactly (both directions, CSV order)
PASS A0-SIM-017 appears in T03/T10/T19 index rows
PASS A0-ISS-007 appears in T10/T19/T29 index rows
PASS A0-T03 carries an A0.2-labelled addition
PASS A0-T10 carries an A0.2-labelled addition
PASS A0-T19 carries an A0.2-labelled addition
PASS A0-T29 carries an A0.2-labelled addition
PASS matrix labels the A1-S4.0 T4 cases DESIGN ONLY
PASS exactly the 23 original gap rows in order (found 23)
PASS A0-GAP-019 mentions A0-ISS-007
PASS gap register names the explicit low-rate block status
PASS gap register describes the S3.1 prerequisite and the intents-only seam
PASS spec records the policy and both new atoms
PASS spec states the policy is not LCP-P09
PASS review has an A0.2 addendum
PASS spec keeps the historical A0.1 D1 sentence
PASS review keeps the historical A0.1 138-count sentence
PASS review keeps the historical A0 128-count sentence
PASS review keeps the historical A0.1 check record
PASS spec keeps the eight LCP-P01..P08 decision headings
PASS matrix index has mandatory rows
PASS spec: all relative links resolve ([])
PASS spec: references checked
PASS matrix: all relative links resolve ([])
PASS matrix: references checked
PASS gaps: all relative links resolve ([])
PASS gaps: references checked
PASS review: all relative links resolve ([])
PASS review: references checked
PASS spec links an A1-S4.0 document
PASS matrix links an A1-S4.0 document
PASS gaps links an A1-S4.0 document
PASS review links an A1-S4.0 document
PASS spec: no BOM
PASS spec: LF only
PASS spec: final newline
PASS spec: no NUL
PASS spec: no trailing whitespace (lines [])
PASS csv: no BOM
PASS csv: LF only
PASS csv: final newline
PASS csv: no NUL
PASS csv: no trailing whitespace (lines [])
PASS matrix: no BOM
PASS matrix: LF only
PASS matrix: final newline
PASS matrix: no NUL
PASS matrix: no trailing whitespace (lines [])
PASS gaps: no BOM
PASS gaps: LF only
PASS gaps: final newline
PASS gaps: no NUL
PASS gaps: no trailing whitespace (lines [])
PASS review: no BOM
PASS review: LF only
PASS review: final newline
PASS review: no NUL
PASS review: no trailing whitespace (lines [])
PASS HEAD unchanged
PASS branch lab
PASS git status lists exactly the five documents as modified ([' M lab/docs/A0_CLASS_A/A0_A1_INTERFACE_AND_GAP_REGISTER.md', ' M lab/docs/A0_CLASS_A/A0_NORMATIVE_CLASS_A_SPEC.md', ' M lab/docs/A0_CLASS_A/A0_REQUIREMENTS_TRACEABILITY.csv', ' M lab/docs/A0_CLASS_A/A0_REVIEW_AND_ACCEPTANCE.md', ' M lab/docs/A0_CLASS_A/A0_TEST_MATRIX.md'])
PASS index empty
PASS git diff --check passes
PASS tracked file set unchanged (150 files)
PASS all 145 other tracked files byte-identical to the preflight inventory ([])

RESULT: ALL CHECKS PASS
```

After this output was inserted into this file the checker was run a fourth time and produced byte-identical output.

Ending `git status --short --untracked-files=all`:

```text
 M lab/docs/A0_CLASS_A/A0_A1_INTERFACE_AND_GAP_REGISTER.md
 M lab/docs/A0_CLASS_A/A0_NORMATIVE_CLASS_A_SPEC.md
 M lab/docs/A0_CLASS_A/A0_REQUIREMENTS_TRACEABILITY.csv
 M lab/docs/A0_CLASS_A/A0_REVIEW_AND_ACCEPTANCE.md
 M lab/docs/A0_CLASS_A/A0_TEST_MATRIX.md
```

`git diff --cached --name-only` is empty; branch `lab`; ending HEAD `caa43f05027bb4ce6839e944656fbc6e52b11442`. Nothing was staged, committed, tagged, pushed, fetched, merged or reset. Suggested **operator-only** commit subject: `lab: record A0.2 first-frame policy and low-rate Class A gap`.
