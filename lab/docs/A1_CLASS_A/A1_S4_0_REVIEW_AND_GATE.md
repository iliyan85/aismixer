# A1-S4.0: review and acceptance gate

Date: 2026-10-23. Companion documents:
[first-frame adjudication](A1_S4_0_FIRST_FRAME_NORMATIVE_ADJUDICATION.md),
[causal event and interface contract](A1_S4_0_CAUSAL_EVENT_AND_INTERFACE_CONTRACT.md).

## (a) Baseline and exact change inventory

Branch `lab` (orphan; never merged with `main` or `website`), HEAD
`b9558b97eb674d1b7aea37615b9d582d81f99337`, subject `lab: implement A1-S3
station-local AIS maps and semantic Philox`, identical to the commit the
mandate expected. Worktree and index were clean before writing; no fetch,
pull, checkout, reset, stash, rebase, merge, cherry-pick, tag, stage,
commit or push was performed. Scratch material (downloaded source files,
extracted Word media, the arithmetic script and its log) lives only in the
ignored directory `lab/build/a1-s4-0-research/` (`lab/.gitignore` line 1
`/build/`, verified with `git check-ignore`).

Created (the only changes):

| Path | Role |
| --- | --- |
| `lab/docs/A1_CLASS_A/A1_S4_0_FIRST_FRAME_NORMATIVE_ADJUDICATION.md` | Deliverable 1 |
| `lab/docs/A1_CLASS_A/A1_S4_0_CAUSAL_EVENT_AND_INTERFACE_CONTRACT.md` | Deliverable 2 |
| `lab/docs/A1_CLASS_A/A1_S4_0_REVIEW_AND_GATE.md` | Deliverable 3 (this file) |

No existing tracked file was edited (A0/A0.1, S1/S2/S3 reports, README,
CMake, code, tests, profiles, goldens); the post-write `git status` lists
exactly the three untracked files above and nothing modified.

## (b) Source verification and limits

| Check | Result |
| --- | --- |
| ITU record page | retrieved; "Approved in 2026-02-19", "In force" |
| PDF SHA-256 | `126025db40f545454bbfc192c85353bd24bded48016f853ec184ef46f704a1dd` (matches A0) |
| Word SHA-256 | `f75379e3a0725b1c096ba06a049fc4fcb00ddd81355ba97883a35382933b2d42` (matches A0) |
| Page convention | printed p = PDF index p+1 (0-based); verified on printed 30-42 and 49-53 headers |
| Prose | read from the PDF text layer (`pdftotext -layout`) and cross-checked in the Word text |
| Figures 11-15 | read as the Word embedded PNGs image13-image17, each labelled inside the bitmap (M.1371-11 .. M.1371-15); arrows, decision labels and the Figure 11 equation/notes were read from the images, not from flattened text |
| Limit | no PDF rasterizer is available (`pdftoppm`, `mutool`, `gs` absent; installs prohibited): the PDF figure pixels were not rendered. Claim FF-20 (PDF and Word figures identical) is UNVERIFIED; no adjudication conclusion depends on it |
| Edition | 02/2026 only; the 2014 edition was not consulted |
| Copyright | no table, figure or long excerpt imported; quotations limited to short sentences with printed-page anchors |

## (c) Traceability

| Reference | Where it is addressed |
| --- | --- |
| A0-ISS-003 (staggered channels, low rate, first-frame timer origin) | Adjudication Sections 2-7; proposed treatment Section 7.4 (no CSV edit) |
| A0-T03 (empty-network entry to continuous operation) | Adjudication F1 (Section 6.1), policy rules E1-F5; contract tests T4-01..T4-05 |
| A0-T10 (ITDMA and first frame, anchored 1/3 report/min) | Adjudication F3 (Section 6.3), FF-14..FF-18; test T4-07 (explicit block, no PASS) |
| A0-T19 (source conflicts) | FF-08, FF-15..FF-17 kept unresolved; counterexamples Section 4 |
| A0-T01/T02 (one-minute initialization, short ON) | F4 (Section 6.4); tests T4-08..T4-11 |
| A0-T05/T20 (frame, wrap, integer time) | F2 (Section 6.2); test T4-06, T4-15 |
| A0-T09 (RATDMA variants) | entry exemption FF-02; S4-C in the staged plan |
| A0-T15/T17/T29 | tests T4-18..T4-20; message/state expectations in F1; low-rate thresholds in FF-14 |
| A0-MAC-011/015/018/023/025/026/027/038, A0-RF-016 | FF-01..FF-14 with printed pages |
| LCP-P01 | NSS/NTS windows in E2; low-rate domain explicitly outside (FF-16) |
| LCP-P02 | time-out drawn per NTS, one decrement at the first continuous visit (T4-05) |
| LCP-P03/P04 | not used at entry (FF-02); S4-C |
| LCP-P05 | one transmitter, no simultaneous own starts (contract Section 3 step 5) |
| LCP-P06 | reporting-rate inputs listed as engine inputs; hysteresis deferred to S4-D |
| LCP-P07 | due instants 367.0 s / 727.0 s anchored at ON; statuses explicit; low-rate deadline interaction noted (F3) |
| LCP-P08 | `EntryNominalStartSlot`, `CandidateChoice`, `ContinuousTimeout` purposes used by E2/F1 |
| S3-TIE-ORDER-v1 | mapped onto runtime phases 0/2/3 (contract Section 3); late evidence and OFF dominance tests T4-09..T4-12 |

## (d) Checks performed versus not run

Performed:

- Source retrieval and hash verification of PDF and Word (two files), record
  page status, text-layer page mapping, figure reading from the Word
  images (five images).
- Independent arithmetic (`lab/build/a1-s4-0-research/arith_check.py`,
  standard library, log `arith_check.log`): unit constants, every F1/F2/F3/F4
  instant, UTC minute, slot and absolute index; ITDMA 19-bit words; extended
  code arithmetic; LCP-P07 due instants; frame-boundary units; INT64 bound.
- Code inspection at this HEAD: `station_knowledge.cpp` call order
  (`synchronize` at line 160 before the token checks at lines 162-191;
  mutations at lines 389-403), `station_inputs.h/.cpp` freshness
  (`max_*_age_units` default 0), `scheduler.cpp` enqueue/dispatch rules,
  `event.cpp` `causal_phase`, `communication_state.cpp`
  `no_further_allocation` (effective increment == 0), `selection_interval`
  domain.
- Static checks on the three new files: UTF-8 without BOM, LF, final
  newline, no trailing whitespace, `git diff --check`.

NOT run: any C++ build, any CTest (S1/S2/S3 suites, Python oracles), any RF,
FCS, UDP, NMEA or playback activity, any PDF rasterization, any five-hour
pilot. No test is declared PASS in this campaign; the S3 witness table in
the contract document is derived from the code path, not executed.

## (e) Gate statuses

| Gate | Question | Status | Basis |
| --- | --- | --- | --- |
| G0 | Official current source accessible and verified? | PASS (with the rasterization limit recorded) | (b) |
| G1 | First-frame outcome | B: reviewed versioned policy `CLASSA-FIRST-FRAME-v1` for Rr in {6, 10, 18, 30}; anchor and duration derived from the source; per-channel chain is policy; A0-ISS-003 remains unresolved as a source interpretation | Adjudication Sections 2-7 |
| G2 | Low-rate outcome | NO-GO: no source procedure, LCP-P01 does not cover NI 6 750; a draft policy is proposed for operator decision; S4 must block Rr < 2 explicitly | Adjudication Sections 5, 6.3, 7.3 |
| G3 | S3 time/rejection contract | CONDITIONAL: the implicit time advance on rejected tokens is a real implementation consequence (witness table); S3.1 reordering is required before S4-A integration acceptance; S4 must synchronize explicitly | Contract Section 4 |
| G4 | Scheduler causality contract | PASS: phases 0-6 and the enqueue rules of the existing scheduler already enforce evidence-before-decision and forbid same-time earlier-phase effects; the S4 ordering and timer rules are specified | Contract Section 3 |
| G5 | Readiness for initial code | CONDITIONAL GO for S4-A only (entry and first frame at Rr in {6, 10, 18, 30}, intents only), with S3.1 and the T4 table as its acceptance criteria; S4-B..S4-E remain gated | Contract Section 7 |

## (f) Decisions

- D0 documents: PASS. The three documents are standalone: a subsequent
  S4-A Code agent task can be written from the policy record (adjudication
  Section 7.2), the interface sketch (contract Section 5), the phase
  sequence (contract Section 3), the S3.1 change list (contract Section 4)
  and the T4 test table (contract Section 8) without repeating the
  normative research.
- D1 conditional S4-A implementation: GO, limited to `Off`,
  `Observe60Seconds`, `EntrySelect`, `EntryWait`, per-channel
  `FirstFrameChain` with terminal zero, explicit `Blocked`, intents only,
  Rr in {6, 10, 18, 30}, under `CLASSA-FIRST-FRAME-v1`, after S3.1.
  Continuous operation, duties, rate changes and the low-rate path are
  separate gated slices; the full S4 FSM freeze/acceptance stays NO-GO.
- D2 five-hour RF/UDP pilot: NO-GO (unchanged). Nothing in this campaign
  creates RF reception, FCS, a transmitter, NMEA or UDP, and no transmission
  or reception has occurred.

Open gates unchanged: A0-ISS-003 (source interpretation), A0-ISS-004,
A0-MAC-038/A0-RF-016, A0-MAC-041, S1-NOGO-IMO-ZERO,
S1-NOGO-UTC-UNAVAILABLE, S2-GAP-SIDECAR, S5 production evidence minter, RF1
(physical tail, FCS, datum), I1 (abort, journal, playback), P1.

## (g) Operator review checklist and suggested commit

Review checklist:

1. Confirm the figure readings in the adjudication Section 1 against your
   own rendering of the PDF pages 39-44 (1-based), since this machine could
   not rasterize the PDF.
2. Decide A0-ISS-003 treatment: accept the review-only patch plan
   (adjudication Section 7.4) for a future A0.2 revision, or keep the row
   untouched and reference the adjudication from the gap register only.
3. Approve or reject `CLASSA-FIRST-FRAME-v1` as the S4-A policy; in
   particular the NSS range [current + 1, current + NI], per-channel chains,
   terminal zero at anchor + 2 250 with keep TRUE, and time-outs drawn per
   first-frame NTS.
4. Decide the low-rate path: approve the direction of the draft
   `CLASSA-LOWRATE-ITDMA-v1` (and the required LCP-P01 version extension)
   or keep anchored operation blocked for the pilot.
5. Approve S3.1 (side-effect-free rejection) as a prerequisite of S4-A
   acceptance, or accept the documented side effect with the explicit
   `synchronize` discipline only.
6. Confirm the S2 freshness policy for the pilot (`max_*_age_units` 0 means
   contemporaneous sampling at every decision instant).

Suggested operator-only commit subject:
`lab: adjudicate A1-S4.0 first-frame rules and freeze the S4 causal contract`.
