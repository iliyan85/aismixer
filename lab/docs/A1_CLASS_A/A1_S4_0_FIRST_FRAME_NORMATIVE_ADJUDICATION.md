# A1-S4.0: first-frame normative adjudication (A0-ISS-003)

Date: 2026-10-23. Documentation-only campaign on branch `lab`, baseline HEAD
`b9558b97eb674d1b7aea37615b9d582d81f99337` (`lab: implement A1-S3
station-local AIS maps and semantic Philox`), clean worktree and empty index
before and after writing. No code, test, profile, golden, A0/A0.1 or S1-S3
file is modified. Nothing here authorizes a transmission, claims radio
reception, five-hour pilot success or full Class A conformance.

Outcome in one sentence: the current source (ITU-R M.1371-6, 02/2026) fixes
the first-frame ANCHOR (the first nominal transmission slot) and the
first-frame DURATION (one frame of 2 250 slots) but does not state whether the
first-frame chain and its terminal zero increment are per channel or per
transceiver, and it gives no entry/first-frame procedure at all for report
rates below two per minute; the adjudication outcome is therefore **B**
(reviewed versioned simulation policy `CLASSA-FIRST-FRAME-v1` for the
Table 1 rates with Rr in {6, 10, 18, 30}) with the low-rate domain kept
**NO-GO** until a separate, operator-approved policy extends LCP-P01.
A0-ISS-003 stays `UNSPECIFIED_OR_CONFLICT` as a source interpretation.

Companion documents: [causal event and interface contract](A1_S4_0_CAUSAL_EVENT_AND_INTERFACE_CONTRACT.md),
[review and gate](A1_S4_0_REVIEW_AND_GATE.md).

## 1. Source manifest, verification and reading log

| Item | Value | Status |
| --- | --- | --- |
| ITU record | https://www.itu.int/rec/R-REC-M.1371-6-202602-I/en | Retrieved 2026-10-23; page states "Approved in 2026-02-19" and "In force" |
| English PDF | https://www.itu.int/dms_pubrec/itu-r/rec/m/R-REC-M.1371-6-202602-I!!PDF-E.pdf | 3 464 843 bytes, 156 pages; SHA-256 `126025db40f545454bbfc192c85353bd24bded48016f853ec184ef46f704a1dd` = A0 reference |
| English Word | https://www.itu.int/dms_pubrec/itu-r/rec/m/R-REC-M.1371-6-202602-I!!MSW-E.docx | 2 132 560 bytes; SHA-256 `f75379e3a0725b1c096ba06a049fc4fcb00ddd81355ba97883a35382933b2d42` = A0 reference |
| Local copies | ignored `lab/build/a1-s4-0-research/source/` (not tracked, ITU copyright retained, no excerpt longer than a sentence imported) | present |

Page convention (A0): printed page p is PDF page index p+1 (0-based) and
URL `#page=p+2`. All printed pages below were checked against the text layer
(`pdftotext -layout`, xpdf 4.06): A2-3.3.3 (printed 30), A2-3.3.4.1 and
Table 13 (30-31), A2-3.3.4.2.1 (31), Table 16 (35-36), Figure 11 (37),
A2-3.3.5.1 and Figure 12 (38), A2-3.3.5.2 and Figure 13 (38-39),
A2-3.3.5.3 and Figure 14 (40-41), A2-3.3.5.4 and Figure 15 (41-42),
Table 20 and A2-3.3.7.3.2 (49-50), A2-4.1.2 (51), A2-4.3 (53), Table 1 (9).

Figure reading log. No PDF rasterizer exists on this machine (`pdftoppm`,
`mutool`, `gs` absent; installation prohibited), so the PDF figure pixels
were NOT rendered. The figures were read from the Word file's embedded PNG
images (`word/media`), each of which carries the ITU figure label inside the
bitmap, which is the identification evidence:

| Word image | Label inside the bitmap | Content read |
| --- | --- | --- |
| image13.png | M.1371-11 | Two slot-map strips (Channel A, Channel B) with NSS, NS, NTS, SI, 2 x NI spacing; legend; "Channel synchronization equation (note that channels are not considered synchronized while the reporting rates are different): NSS_B = NSS_A + NI (change effective at next B-channel NTS)"; Note 1 "This occurs once during network entry phase or as needed inside the change report rate phase"; Note 2 on NSS_CC = NS_CC in the change report rate phase |
| image14.png | M.1371-12 | Initialization phase: "Monitor VHF data link" -> decision "1 min?" (No loops back, Yes) -> "Network entry phase" |
| image15.png | M.1371-13 | Network entry phase: one box "Select NSS_A for channel A and NSS_B for channel B, NSS_B = NSS_A + NI" -> "Select NTS" -> "Success?" (No returns to NSS selection) -> "Wait for NTS" -> "At NTS?" (No waits) -> "First frame phase" |
| image16.png | M.1371-14 | First frame phase: loop head decision "One frame?"; Yes -> "Set increment to zero" -> joins before "Add increment to NTS to this transmission"; No -> "Select next NS and NTS" -> "Add increment to NTS to this transmission" -> "Transmit" -> "Increment is zero?"; Yes -> "Continuous operation phase"; No -> "Wait for NTS" -> back to "One frame?" |
| image17.png | M.1371-15 | Continuous operation phase: "Wait for NTS" -> "Decrement slot time-out" -> "Slot time-out is zero?" (Yes: calculate new NTS, offset, insert offset; No: slot offset zero) -> "Insert new slot time-out into message" -> "Transmit" -> loop |

The image-to-figure order was also confirmed by the document-order list of
embedded images (image12 = Figure 10 and image17 = Figure 15 as recorded by
the A0 audit; the four intermediate images are Figures 11-14). Both the PDF
text layer and the Word text were used for prose; no figure arrow or glyph
was taken from flattened text. Because the PDF figures themselves were not
rasterized, any claim that depends on a PDF-only rendering difference is
marked UNVERIFIED below; none of the adjudication conclusions depends on one.

Readings reused from A0/A0.1 without re-derivation: printed page convention;
Table 16 NI maximum typo (1 225 vs 1 125); LCP-P01 integer selection
intervals; LCP-P02 one decrement per visit; A0-MAC-017 extended ITDMA codes.

## 2. RQ1: four temporal concepts

| Concept | What the source says | Classification | Open point |
| --- | --- | --- | --- |
| (i) ON epoch and 60 s observation | A2-3.3.3 (printed 30): "At power on, a station should monitor the TDMA channels for one (1) min ... After one (1) min has elapsed, the station should enter the operational mode"; A2-3.3.5.1.1/.2 (38) and Figure 12 repeat it ("1 min?" decision). Elapsed time from power on, not a UTC minute. | NORMATIVE_EXPLICIT (A0-MAC-011) | Whether "power on" is the electrical ON or the instant the receiver is actually able to decode is not stated; the lab uses the W1 `EquipmentOn` transition instant = `OwnPowerNotice.time` (A0-ISS-006/LCP-P07 policy). |
| (ii) Entry selection and first unannounced Message 3 | A2-3.3.5.2 (38): first transmission is Message 3 (Figure 13); A2-3.3.5.2.1 (39): NSS "randomly selected between current slot and nominal increment (NI) slots forward", first NS = NSS; A2-3.3.5.2.2: NTS randomly among SI candidates, marked internally allocated with a random time-out TMO_MIN..TMO_MAX, restart NSS selection on failure; A2-3.3.4.1.1 (30-31): the first entry slot is allocated with RATDMA and becomes the first ITDMA slot; A2-3.3.4.2.1 (31): the p-persistent algorithm does not apply to network entry; Figure 13 selects NSS_A and NSS_B = NSS_A + NI in one step. | NORMATIVE_EXPLICIT (A0-MAC-018/025) | "Current slot" endpoint inclusivity and whether NSS may equal the current slot are not stated (LCP-P01 excludes entry endpoints): SIMULATION_POLICY below. Channel B's first slot cannot be announced on channel B by channel A's ITDMA state (Table 20, printed 50: the state applies only to the channel of the transmission), so B's first transmission is also an unannounced (RATDMA-label) ITDMA slot: NORMATIVE_DERIVED. |
| (iii) First frame phase and its anchor | A2-3.3.5.2.4 (39): "When the frame map indicates that the NTS is approaching, the station should enter the first frame phase"; A2-3.3.5.3 (40): "During the first frame phase which is equal to a 1 min interval, the station should continuously allocate its transmission slots and transmit special position reports (Message 3) using ITDMA"; A2-3.3.5.3.1 (41): "When a 1 min interval has elapsed, the initial transmissions should have been allocated"; A2-3.3.5.3.2: "When all allocations have been made after one frame, the increment should be set to zero in the last transmission"; A2-3.3.5.3.3: next NS by "keeping track of the number of transmissions performed so far on the channel (from n to Rr - 1)" with Table 16; A2-3.3.5.3.4: every first-frame transmission carries the increment to the next due slot and sets the keep flag. | Anchor = first NTS: NORMATIVE_EXPLICIT (phase entry at NTS). Duration = one frame = 2 250 slots after the anchor: NORMATIVE_DERIVED (Section 4, F2 counterexample rejects the UTC-minute and ON+60 readings). | Whether "the first frame phase" and "the last transmission" are per channel or per transceiver is not stated: see Section 5. |
| (iv) Transition to continuous SOTDMA or to a low-rate ITDMA mode | A2-3.3.5.3.6 (41): increment zero ends the first frame phase and enters continuous operation (Figure 15, SOTDMA time-outs). Low rate: A2-3.3.4.1 (30) "periodic transmission of the mobile station with a report rate of less than two reports per min" uses ITDMA; Table 16 note 2 (36) same; A2-4.3 (53) "less than one report per frame ... ITDMA"; Table 16 gives Rr minimum 2. | Continuous SOTDMA after zero: NORMATIVE_EXPLICIT (A0-MAC-026/027). Low-rate entry and first frame: UNRESOLVED (no procedure; Figure 14 presupposes at least one further same-channel slot within one frame). | The two low-rate thresholds remain A0-MAC-038/A0-RF-016; Rr = 1/3 is below both. |

Answers to the RQ1 questions:

- The first-frame minute is a rolling interval of one frame measured from the
  channel's first NTS; it is neither a UTC-aligned minute nor ON+60 s. The
  UTC-aligned reading fails F2 (a channel whose first NTS lies at slot 2249
  would end its first frame after one transmission with n = 1..Rr/2-1 never
  announced, contradicting A2-3.3.5.3.1/.3.2). The ON+60 reading fails
  because Figure 13 places a network entry phase of non-zero duration (NSS up
  to NI forward, NTS within SI, waiting) between the end of observation and
  phase entry, so a timer started at ON+60 would expire before the last
  first-frame NTS is reached (F1: ON+60 = 67.0 s, first NTS 70.99 s, last
  first-frame NTS 110.99 s, A's kept repeat 130.99 s).
- Per-channel origins: Figure 13 selects NSS_A and NSS_B together and
  enters one "First frame phase"; Figure 14 draws one loop; but A2-3.3.5.3.3
  counts transmissions per channel, Table 20 scopes the ITDMA state to its
  channel, A2-4.1.2 (printed 51) states "Channel access is performed
  independently on each of the two parallel channels", and A2-3.3.4.1.1 ends
  every ITDMA chain with a zero increment "in the last ITDMA slot". The
  figures do not settle it; the text favours per-channel chains (Section 5).
- Earliest NTS: NSS in [current slot, current slot + NI] (endpoints policy),
  NTS in SI = NS +/- floor(0.1 NI) (LCP-P01), first NS = NSS. Ordinary
  reports in the first frame: NS_n = NSS + n * 2 NI per channel for two
  channels (Table 16), each a Message 3 with increment to the next
  same-channel NTS and keep = TRUE. Terminal zero: the first same-channel
  transmission at or after anchor + 2 250 slots (Section 5). Keep flags:
  TRUE during the first frame (A2-3.3.5.3.4), which is exactly what makes the
  first-frame slots the continuous SOTDMA slots of the next frame. First
  continuous transmissions: the kept repeats, now Message 1 with SOTDMA
  state, time-out as drawn at NTS selection and decremented per LCP-P02.

## 3. Issue-by-issue claim, evidence and status matrix

| ID | Claim | Evidence (printed page) | Status |
| --- | --- | --- | --- |
| FF-01 | Observation lasts 60 s elapsed from ON, both channels, zero TX. | A2-3.3.3 (30); A2-3.3.5.1.1-.2 (38); Figure 12 | NORMATIVE_EXPLICIT |
| FF-02 | First transmission is Message 3 with ITDMA state; generic RATDMA probability gate is not used. | A2-3.3.5.2 (38), Figure 13; A2-3.3.4.1.1 (30-31); A2-3.3.4.2.1 (31); A2-3.3.7.4.1 (50) | NORMATIVE_EXPLICIT |
| FF-03 | NSS in [current slot, current + NI]; first NS = NSS; NTS in SI; time-out 3..7 at NTS selection; NSS reselection on failure. | A2-3.3.5.2.1-.2 (39); Table 16 (35-36) | NORMATIVE_EXPLICIT; endpoint inclusivity SIMULATION_POLICY |
| FF-04 | NSS_B = NSS_A + NI at entry (equal rates). | Figure 13; Table 16 "NSSB = NSSA + NI"; Figure 11 equation and Note 1 | NORMATIVE_EXPLICIT |
| FF-05 | First-frame anchor is the first NTS (phase entry), not ON+60 and not a UTC boundary. | A2-3.3.5.2.4 (39); Figure 13 "At NTS? -> First frame phase" | NORMATIVE_EXPLICIT |
| FF-06 | First-frame duration is one frame = 2 250 slots from the anchor. | A2-3.3.5.3 "equal to a 1 min interval" (40); A2-3.3.5.3.1 (41); F2 counterexample | NORMATIVE_DERIVED |
| FF-07 | Every first-frame transmission is Message 3 with increment to the next same-channel due slot and keep = TRUE. | A2-3.3.5.3.4-.5 (41); A2-3.3.4.1 (30) entry slots reserved one additional frame; Table 20 channel scope (50) | NORMATIVE_EXPLICIT |
| FF-08 | The chain (and the zero) is per channel, each channel anchored at its own first NTS. | For: A2-3.3.5.3.3 per-channel counting (41), A2-4.1.2 independent channel access (51), A2-3.3.4.1.1 zero in the last ITDMA slot (31), Table 20 channel scope. Against: single boxes in Figures 13/14, singular "the last transmission" (41). | UNRESOLVED at source level; adopted as SIMULATION_POLICY with the derived reading |
| FF-09 | Terminal zero slot for Rr >= 2 is the kept repeat of the channel's first NTS, i.e. first NTS + 2 250 exactly. | Figure 14 "One frame? -> Set increment to zero -> Add increment -> Transmit"; keep = TRUE on the first NTS (FF-07); no new NS selection on the Yes branch | NORMATIVE_DERIVED (given FF-06/FF-08) |
| FF-10 | Subsequent first-frame NTSs carry a SOTDMA time-out drawn 3..7. | A2-3.3.5.3.3 "SOTDMA algorithm" with Table 16 TMO; A2-3.3.5.2.2 explicit only for the first NTS | NORMATIVE_DERIVED; draw purpose LCP-P08 |
| FF-11 | Continuous operation after the zero uses Message 1/SOTDMA with one decrement per visit. | A2-3.3.5.3.6 (41); A2-3.3.5.4.2-.4 (42), Figure 15; LCP-P02 | NORMATIVE_EXPLICIT + A0-MAC-037 derived decrement |
| FF-12 | A UTC minute rollover inside the first frame changes nothing. | FF-06; slot numbers repeat modulo 2 250 so first NTS + 2 250 is the same frame slot in the next UTC minute | NORMATIVE_DERIVED |
| FF-13 | OFF at exactly a boundary dominates the boundary action. | A0-SIM-004; runtime phase 0 precedes phases 2/3 (contract document) | SIMULATION_POLICY (existing) |
| FF-14 | Rates below two reports per minute use ITDMA for periodic transmission; Table 16 Rr minimum is 2. | A2-3.3.4.1 (30); Table 16 note 2 (36); A2-4.3 (53) | NORMATIVE_EXPLICIT; thresholds conflict A0-MAC-038/RF-016 retained |
| FF-15 | Figure 14's first-frame procedure is inapplicable at Rr < 2: its "One frame?" test would demand a zero at the next NTS, which at Rr < 2 is the next periodic slot itself, and a zero would end the only allocation mechanism available at that rate. | Figure 14; Table 13 (31); Table 20 (50) "Use of 5 to 7 removes the need for RATDMA broadcast for scheduled transmissions up to 6 min intervals" | UNRESOLVED (source silent) |
| FF-16 | Low-rate selection window: Table 16 SI = NS +/- 0.1 NI would be +/- 675 slots at NI 6 750; A2-3.3.4.1.1 says only "randomly selects the next following ITDMA slot". | Table 16 (35); A2-3.3.4.1.1 (30); LCP-P01 scope restricted to NI 75/125/225/375 | UNRESOLVED; outside LCP-P01 |
| FF-17 | Low-rate keep flag: A2-3.3.4.1 reads "reserved for one additional frame or a given increment afterward" for entry and sub-frame periodic transmission; keep = TRUE at RI 180 s would reserve a slot the station never uses. | A2-3.3.4.1 (30); Table 13 keep description (31) | NORMATIVE_DERIVED reading "given increment afterward" for Rr < 2; retained as policy because the sentence is textually damaged |
| FF-18 | ITDMA increment zero vs extended code: wire increment 0 with slot-count code 5..7 means effective 8 192, not termination; termination requires effective increment 0 (code 0..4). | Table 20 (49-50); S1 codec `ItdmaState19::no_further_allocation` = effective increment == 0 | NORMATIVE_EXPLICIT; S1 verified by inspection |
| FF-19 | Periodic transmissions alternate AIS 1/AIS 2 transmission by transmission, including the initial link access. | A2-4.1.2 (51) | NORMATIVE_EXPLICIT (A0-RF-013) |
| FF-20 | PDF figure pixels identical to the Word figures. | Not rasterized | UNVERIFIED (no conclusion depends on it) |

## 4. Rejected readings and their smallest counterexamples

- UTC-aligned first frame: F2, first NTS_A at index 4499 (frame M0+1 slot
  2249); the UTC minute rolls over at index 4500, one slot later; a
  UTC-aligned "One frame?" would become true at NS_1 = 5249 and force the
  zero there, leaving n = 2 (index 5999) unannounced and no kept repeat
  pattern, contradicting A2-3.3.5.3.1 ("the initial transmissions should
  have been allocated"). The rolling reading places the zero at 6749.
- ON+60 anchor: F1, ON+60 = 67.000 s (index 4012), first NTS at 70.987 s
  (index 4162); a timer from 4012 expires at 6262, before A's third
  first-frame transmission at 5662 is followed by its kept repeat at 6412;
  the zero would be forced at 6412 anyway only by coincidence of ordering,
  while for a late NSS (up to 4387) the last first-frame NTS (5887) and its
  kept repeat (6637) both fall after 6262, so the station would have to emit
  the zero at 5887 before announcing the n = 2 slot: contradiction with
  A2-3.3.5.3.2.
- Single transceiver chain with no zero on channel B (Interpretation I in
  Section 5): channel B's last ITDMA transmission (F1: 6037) carries a
  non-zero increment to 6787 and no B transmission ever carries increment
  zero, contradicting A2-3.3.4.1.1 "In the last ITDMA slot, the relative
  increment is set to zero" read with Table 20's channel scope.
- Terminal zero before all allocations (zero at the n = Rr/2 - 1 transmission
  instead of the kept repeat): the n = Rr/2 - 1 transmission must carry the
  increment to the kept repeat (A2-3.3.5.3.4 "the next slot in which a
  transmission is due"), so a zero there would leave the kept repeat
  unannounced by increment (still reserved by keep) and would end the phase
  before "one frame" (Figure 14 tests "One frame?" before each
  transmission); rejected by the figure order.
- Type 1 first, or generic p-persistence at entry: explicit contrary text
  (FF-02).

## 5. RQ3: terminal zero and multi-channel operation

Distinctions required by the mandate:

1. "No subsequent ITDMA allocation" (increment zero) versus "a nominally due
   position report": the zero ends ITDMA allocation only; position reports
   continue in continuous SOTDMA on the kept slots (A2-3.3.5.3.6, Figure 15).
2. Per-channel end of the first frame versus the transceiver's first phase:
   see the two interpretations below.
3. ITDMA increment zero versus extended increment with wire zero: FF-18.
4. Type 3 startup: every first-frame transmission is Message 3 (FF-02/07);
   entry never uses the generic RATDMA probability gate (A2-3.3.4.2.1).

Interpretation I (literal single flowchart): one first frame phase for the
transceiver, anchored at the first NTS overall (channel A); the zero is set
in the first transmission at or after anchor + one frame, which for equal
rates is the kept repeat of NTS_A0; channel B's chain stops without a zero
and B's kept repeat is already a Message 1. Consistent with Figure 14 drawn
once and with "the last transmission" in the singular. Inconsistent with
A2-3.3.4.1.1 and Table 20 channel scope (Section 4, third item).

Interpretation II (per-channel chains): each channel runs Figure 14 with its
own anchor (its first NTS), its own transmission counter n and its own zero
at its kept repeat (first NTS + 2 250). For equal rates the two terminal
zeros occur at NTS_A0 + 2 250 and NTS_B0 + 2 250 = NTS_A0 + 2 250 + NI
(F1: 6412 and 6787). Consistent with A2-3.3.5.3.3, A2-4.1.2, A2-3.3.4.1.1
and Table 20; consistent with Figure 14 if the flowchart is instantiated per
channel; inconsistent only with the singular wording.

Interpretation III (transceiver timer, per-channel termination): one timer
from NTS_A0, but after it elapses every channel still emits one terminal
zero at its next due slot. For equal rates III produces exactly II's slots
because NTS_B0 + 2 250 is B's first due slot after NTS_A0 + 2 250 whenever
NI < 2 250. III differs from II only for unequal channel rates, which the
pilot profile does not use (Figure 11: channels are not considered
synchronized while rates differ).

On-air difference between I and II/III: channel B's kept repeat (F1 index
6787) is Message 3 with increment 0 and keep = TRUE under II/III, Message 1
with SOTDMA time-out under I. Receivers' maps converge either way one frame
later; the difference is observable in the message ID and communication
state of exactly one transmission per channel other than A.

Low rate (Rr < 2): neither interpretation applies, because no same-channel
transmission is due within one frame; Figure 14 would demand a zero on the
second periodic transmission, which would terminate the only allocation
mechanism (ITDMA pre-announcement). A valid "first-frame completion" cannot
be established from the source without a zero-carrying transmission, and a
zero cannot be emitted without stopping the periodic schedule. No timer in
the source is violated by never emitting a zero at low rate (Figure 14's
timer belongs to a procedure that presupposes Rr >= 2, Table 16). Only a
bounded policy can make the anchored path executable (Section 7.3); it is
not adopted here.

## 6. RQ2: annotated two-channel traces

Common fixture arithmetic (independently computed with the standard-library
script `lab/build/a1-s4-0-research/arith_check.py`, log `arith_check.log`,
no production code): 9 830 400 units/s, 262 144 units/slot, 2 250 slots and
589 824 000 units per UTC minute. Scenario zero is POSIX second
1 900 000 000, which is 40 s after the start of UTC minute M0 = 31 666 666,
i.e. slot 1 500 of M0. Absolute index = (UTC minute - M0) * 2 250 + slot;
scenario instant of a slot start = (index - 1 500) * 262 144 units. Owner:
synthetic station A (vessel 11, equipment 21, radio 31, MMSI 000000001),
power generation 1, ideal direct UTC valid, EPFS from own W1 truth, every
peer observation a UNIT_FIXTURE token. No selected NTS below is a grant:
each is a hand-selected legal fixture under the stated candidate proof
(all SI slots free on both local maps unless stated).

### 6.1 F1: ON at 7.0 s (mid-slot), RI 10 s, NI 375, staggered A/B

| Event | Units | Scenario t | UTC minute / slot | Index | Channel | Rule | State before -> after | RF request legal? | Message / comm state | Classification |
| --- | ---: | ---: | --- | ---: | --- | --- | --- | --- | --- | --- |
| ON (W1 EquipmentOn, OwnPowerNotice gen 1) | 68 812 800 | 7.000 s | M0+0 / 1762 (+131 072 units) | 1762 | both | A2-3.3.3 | OFF -> OBSERVE_60_SECONDS; maps/directory empty, epoch start | no | none | NORMATIVE_EXPLICIT |
| Observation complete | 658 636 800 | 67.000 s | M0+1 / 1762 (+131 072) | 4012 | both | A2-3.3.5.1.2 | OBSERVE -> ENTRY_SELECT; current slot 4012 | no | none | NORMATIVE_EXPLICIT |
| NSS_A drawn (fixture +150) | 658 636 800 | 67.000 s | M0+1 / 1762 | 4162 | A | A2-3.3.5.2.1 "between current slot and NI slots forward" (4012 .. 4387); policy E2 draws from [4013, 4387] | ENTRY_SELECT: NSS_A = 4162, SI_A = [4125, 4199] | no | none | endpoint policy |
| NSS_B = NSS_A + NI | same | same | M0+2 / 37 | 4537 | B | Figure 13, Table 16 | SI_B = [4500, 4574] (starts at the frame boundary) | no | none | NORMATIVE_EXPLICIT |
| NTS_A0 selected (fixture = NSS_A), TMO_A0 = 5 drawn | same | same | - | 4162 | A | A2-3.3.5.2.2; LCP-P01/P08 | own map A: internal 4162 retained 4 further frames (LCP-P02 drawn 5); ENTRY_WAIT | no | none | NORMATIVE_EXPLICIT; draw SIMULATION_POLICY |
| NTS_B0 selected (fixture = NSS_B), TMO_B0 = 4 | same | same | - | 4537 | B | same | own map B: internal 4537 retained 3 further frames | no | none | derived (unannounced B entry slot) |
| A n=0 (first NTS, phase entry) | 697 827 328 | 70.987 s (ON+63.99) | M0+1 / 1912 | 4162 | A | A2-3.3.5.2.4/.3.4-.5 | FIRST_FRAME_A anchored at 4162; next NS_A1 = 4912, SI [4875, 4949], NTS 4912 (fixture), TMO 3 drawn; increment 750, keep TRUE | yes (intent) | Message 3; ITDMA19 = sync 0, incr 750, code 0, keep 1 = 12001 (0x02EE1) | NORMATIVE_EXPLICIT |
| B n=0 (first NTS on B) | 796 131 328 | 80.987 s (ON+73.99) | M0+2 / 37 | 4537 | B | same; A2-4.1.2 alternation | FIRST_FRAME_B anchored at 4537; next NS_B1 = 5287, NTS 5287, TMO 6; increment 750, keep TRUE | yes | Message 3; ITDMA19 12001 | NORMATIVE_EXPLICIT (unannounced B slot: derived) |
| A n=1 | 894 435 328 | 90.987 s | M0+2 / 412 | 4912 | A | A2-3.3.5.3.3-.5 | next NS_A2 = 5662, NTS 5662, TMO 7; increment 750, keep | yes | Message 3; 12001 | NORMATIVE_EXPLICIT |
| B n=1 | 992 739 328 | 100.987 s | M0+2 / 787 | 5287 | B | same | next NS_B2 = 6037, NTS 6037, TMO 3; increment 750, keep | yes | Message 3; 12001 | NORMATIVE_EXPLICIT |
| A n=2 | 1 091 043 328 | 110.987 s | M0+2 / 1162 | 5662 | A | same; n = Rr/2 - 1 = 2 | next due slot = kept repeat 6412; increment 750, keep | yes | Message 3; 12001 | NORMATIVE_EXPLICIT |
| B n=2 | 1 189 347 328 | 120.987 s | M0+2 / 1537 | 6037 | B | same | next due slot = kept repeat 6787; increment 750, keep | yes | Message 3; 12001 | NORMATIVE_EXPLICIT |
| A kept repeat, one frame after 4162 | 1 287 651 328 | 130.987 s | M0+2 / 1912 | 6412 | A | Figure 14 "One frame? Yes"; A2-3.3.5.3.2/.3.6 | FIRST_FRAME_A -> CONTINUOUS_A; increment 0, keep TRUE | yes | Message 3; ITDMA19 = 1 (0x00001) | derived FF-09 (both interpretations) |
| B kept repeat, one frame after 4537 | 1 385 955 328 | 140.987 s | M0+3 / 37 | 6787 | B | Interpretation II/III: zero; Interpretation I: continuous | II/III: FIRST_FRAME_B -> CONTINUOUS_B, Message 3 incr 0 keep; I: Message 1, SOTDMA time-out 4-1 = 3 | yes | II/III: Message 3, ITDMA19 = 1; I: Message 1, SOTDMA19 time-out 3 with received-stations submessage | UNRESOLVED -> policy |
| A continuous, next frame | 1 484 259 328 | 150.987 s | M0+3 / 412 | 7162 | A | A2-3.3.5.4.2, LCP-P02 | SOTDMA at kept 4912 + 2 250; time-out 3-1 = 2 advertised | yes | Message 1; SOTDMA19 time-out 2, current-slot submessage 412 | NORMATIVE_EXPLICIT + derived decrement |

Uncommitted-decision consequences: every row above is an INTENT; until RF1
exists no on-air completion, no FCS and no receiver decode exist. If the
A n=0 intent is not physically completed (future RF1 outcome), the local
map still holds the own reservation; the source has no retraction mechanism
and the pilot must not pretend one. Reservation knowledge at 130.987 s under
II/III: own A map internal 4162(+kept 6412), 4912, 5662 with time-outs;
own B map internal 4537, 5287, 6037 and the pending 6787.

### 6.2 F2: first opportunity at slot 2249, UTC rollover one slot later

NSS_A = NTS_A0 = index 4499 (M0+1 slot 2249, scenario 79.973 s), SI_A =
[4462, 4536] crossing the frame boundary 4500; NSS_B = 4874 (M0+2 slot 374).
A chain: 4499 -> 5249 (M0+2 slot 749, 99.973 s) -> 5999 (slot 1499,
119.973 s) -> kept repeat 6749 (M0+2 slot 2249, 139.973 s) with the zero.
The UTC minute rolls over at 4500, one slot after the anchor; under FF-06
nothing changes: the increments 750, 750, 750 cross the boundary as
absolute offsets, the kept repeat is slot 2249 of the next UTC minute, and
the zero is emitted there. A UTC-aligned reading would force the zero at
5249 (Section 4). Separately: OFF at exactly ON+60 s (F4-b) and OFF at
exactly NTS_A0 (F4-c) are phase-0 events that precede any phase-3 decision
at the same instant, so no entry decision and no TX intent exist at that
instant (contract document, Section 4).

### 6.3 F3: anchored or moored, RI 180 s (Rr = 1/3, NI = 6 750)

Table 1 (printed 9): "at anchor or moored and not moving faster than 3
knots: 3 min". Rr = 1/3 is below both disputed thresholds (A0-MAC-038,
A0-RF-016), so ITDMA periodic transmission applies (FF-14).

| Event | Units | Scenario t | UTC minute / slot | Index | Channel | What the source fixes | What it does not fix |
| --- | ---: | ---: | --- | ---: | --- | --- | --- |
| ON, observation as F1 | 68 812 800 / 658 636 800 | 7.0 s / 67.0 s | M0+0 1762 / M0+1 1762 | 1762 / 4012 | both | 60 s observation, no TX | - |
| NSS_A (fixture current + 150) | 658 636 800 | 67.0 s | M0+1 / 1912 | 4162 | A | NSS in [current, current + NI] = [4012, 10762]: the first transmission may lie up to three minutes after entry | window endpoint; whether "NI forward" is really intended for NI > 2 250 |
| NSS_B = NSS_A + NI | same | same | M0+4 / 1912 | 10912 | B | Figure 13 equation | nothing beyond the equation |
| NTS_A0 | 697 827 328 | 70.987 s | M0+1 / 1912 | 4162 | A | Message 3 (FF-02); increment to the next same-channel periodic slot 4162 + 2 NI = 17662 (13 500 slots, extended code 5, wire 5 308, block 1, keep FALSE under FF-17): ITDMA19 = 84938 (0x14BCA) | selection window (SI would be +/- 675, FF-16); keep semantics (FF-17); absence of a zero (FF-15) |
| NTS_B0 (unannounced on B) | 2 467 299 328 | 250.987 s | M0+4 / 1912 | 10912 | B | Message 3; increment 13 500 to 24412; keep FALSE | same as A |
| A second periodic slot | 4 236 771 328 | 430.987 s | M0+7 / 1912 | 17662 | A | Message 3 with the next increment (Table 20 up to 6 min) | Figure 14 would demand a zero here ("One frame?" true) which would end the schedule: UNRESOLVED |
| B second periodic slot | 6 006 243 328 | 610.987 s | M0+10 / 1912 | 24412 | B | same | same |

What MUST be transmitted at low rate (source): Message 3 with a valid ITDMA
state at every periodic slot, each announcing the next same-channel slot
(extended code when the offset is 8 192 or more). What cannot be proven from
the source: the random selection window of the next slot, the keep value,
whether a terminal zero is ever required, and whether the entry NSS domain
"NI forward" (three minutes) is intended. Deadline interaction (RQ9): with
NSS_A at the worst case current + NI the first A transmission is at 246.99 s
and B's at 426.99 s, so the LCP-P07 Type 5 due (ON+360 s = 367.0 s) and the
Class A 24B due (ON+720 s = 727.0 s) can precede the first B transmission;
the obligations stay due and are recorded `blocked_no_legal_opportunity`
or `missed_due` by policy, never shifted (A0-GAP-014, LCP-P07).

### 6.4 F4: short ON intervals

| Case | Instant (F1 fixture) | Phase-0 lifecycle vs decision | Expected result | Classification |
| --- | --- | --- | --- | --- |
| OFF at ON+59 s | 648 806 400 units, 66.000 s, M0+1 slot 1725 | OFF before observation completes | zero TX, epoch cancelled, no pending intent; re-ON starts a fresh 60 s (A0-T02) | NORMATIVE_EXPLICIT + A0-SIM-004 |
| OFF at exactly ON+60 s | 658 636 800 units, 67.000 s, M0+1 slot 1762 | phase 0 precedes the phase-3 observation-complete decision at the same instant | OFF wins: no ENTRY_SELECT, no NSS draw, no intent | SIMULATION_POLICY (A0-T02, S3-TIE-ORDER-v1) |
| OFF after 60 s, before NTS_A0 (e.g. 69.0 s) | 678 297 600 units | pending entry intent exists | intent withdrawn (`cancelled_by_off`), own reservation cleared by S3 OFF reset, no TX | SIMULATION_POLICY (S3 OFF semantics) |
| OFF at exactly NTS_A0 start | 697 827 328 units, index 4162 | phase 0 precedes phase 4 TX start | no TransmissionStart may be enqueued at that instant; intent withdrawn | SIMULATION_POLICY |
| OFF between A n=0 and B n=0 (75.987 s) | 746 979 328 units, M0+1 slot 2099 | A transmitted once (increment 750, keep) | B never transmits; receivers that decoded A hold a one-shot reservation at 4912 and a kept reservation at 6412 that simply age out; the source has no retraction; re-ON after OFF starts a new epoch with a new 60 s observation and new NSS draws | NORMATIVE_EXPLICIT (no retraction) + policy (fresh epoch) |

## 7. RQ4: closure decision

### 7.1 Decision: outcome B

The source uniquely determines FF-01..FF-07, FF-11, FF-14, FF-18, FF-19 and,
by derivation with retained counterexamples, FF-06, FF-09, FF-10, FF-12.
It does not determine FF-08 (per-channel versus transceiver chain) and it is
silent on the low-rate domain (FF-15..FF-17). A defensible bounded policy
exists for the Table 1 rates with Rr >= 2 (RI 2, 10/3, 6, 10 s). The
anchored low-rate path has no defensible policy inside the frozen LCP-P01
domain, so it is NOT closed here. Domains are therefore classified
separately:

| Domain | Outcome | Basis |
| --- | --- | --- |
| Entry (observation, NSS/NTS, first Message 3) at Rr in {6, 10, 18, 30} | A for the explicit rules, B for the endpoint policy | FF-01..FF-05 |
| First-frame chain and terminal zero at Rr in {6, 10, 18, 30} | B: `CLASSA-FIRST-FRAME-v1` | FF-08 unresolved, FF-06/FF-09 derived |
| Continuous SOTDMA after the zero | A (with LCP-P02 already frozen) | FF-11 |
| Anchored low rate (Rr = 1/3) entry and periodic ITDMA | C for now: NO-GO pending an operator-approved `CLASSA-LOWRATE-ITDMA-v1` and an LCP-P01 version extension | FF-15..FF-17 |
| Assigned rates, [1, 2) reports/min, Message 23 | excluded (A0-MAC-038/RF-016, LCP) | unchanged |

### 7.2 Policy record CLASSA-FIRST-FRAME-v1 (SIMULATION_POLICY, proposed, not frozen in A0)

- Atom: proposed `A0-SIM-017` (project policy); links A0-ISS-003
  (`UNSPECIFIED_OR_CONFLICT`, unchanged), A0-MAC-011/018/025/026/033,
  A0-RF-013, LCP-P01/P02/P05/P08.
- Scope and preconditions: profile `LCP-CLASSA-AUTO-TERR-v1`; autonomous
  mode; two terrestrial channels alternating transmission by transmission;
  Rr in {6, 10, 18, 30} (NI 375/225/125/75); valid ideal direct UTC and
  qualified own inputs at every decision instant; own maps/directory from
  S3; no assignment, no base-station interaction, no low rate.
- Rule E1 (observation): the station is entry-eligible at
  `OwnPowerNotice.time + 60 s` exactly (elapsed units, 589 824 000), on
  both channels, never earlier; the eligibility instant belongs to a decision
  phase, so an OFF notice at the same instant dominates.
- Rule E2 (NSS/NTS): `current` is the absolute index of the slot containing
  the eligibility instant; NSS_A is drawn equiprobably from the inclusive
  integer range [current + 1, current + NI] (the current slot itself cannot
  start a transmission that has already begun; CLASSA-PHILOX-v1 purpose
  `EntryNominalStartSlot`); NSS_B = NSS_A + NI; first NS = NSS on each
  channel; NTS from the LCP-P01 interval around NS using the S3 candidate
  hierarchy and `CandidateChoice`; a channel with no candidate restarts at
  E2 with a new NSS_A draw (both channels re-derived, A2-3.3.5.2.2); the NTS
  is reserved in the own map with a `ContinuousTimeout` draw T in 3..7
  stored as retained frames T-1 (LCP-P02 reading).
- Rule F1 (per-channel chains): each channel runs the first-frame procedure
  independently, anchored at its own first NTS (slot start instant of that
  transmission's intent); transmission n on a channel (n = 0..Rr/2-1)
  selects NS_(n+1) = NSS + (n+1) * 2 NI with its NTS and time-out before
  transmitting, carries the increment to that NTS and keep = TRUE; the
  transmission at anchor + 2 250 (the kept repeat of the first NTS) carries
  increment 0 and keep = TRUE and ends that channel's first frame; the
  channel then operates continuous SOTDMA (Message 1) on its kept slots
  with the time-outs drawn at their selection.
- Rule F2 (increments): the increment is the absolute slot difference to the
  next same-channel due slot (750 slots at Rr = 6, generally 2 NI); values
  from 8 192 use Table 20 extended codes; a block length of one slot.
- Rule F3 (UTC independence): first-frame bookkeeping uses absolute indices;
  UTC minute boundaries have no effect; the kept repeat lands on the same
  frame slot of the next UTC minute by construction.
- Rule F4 (OFF): OFF revokes every pending intent and every own reservation
  (S3 OFF reset); already transmitted intents are historical facts; a later
  ON repeats E1/E2 with a new power generation and a fresh key domain.
- Rule F5 (RF outcome): no rule above depends on an RF success; intents are
  proposals; the local map keeps own reservations regardless of on-air
  outcome until RF1/I1 define abort/failure evidence.
- Exceptions and non-coverage: unequal channel rates, assigned mode,
  reporting-interval changes during the first frame, Rr < 2, semaphore
  operation, Message 16/23 commands, no-UTC time-out 1 (A0-ISS-004).
- Negative tests (policy oracles, see the contract document, Section 8):
  no TX before ON+60 s; OFF at ON+60 s wins; NSS outside
  [current + 1, current + NI] rejected; B chain shorter or longer than one
  frame rejected; zero at any slot other than anchor + 2 250 rejected;
  Message 1 before the channel's zero rejected; Type 1 first rejected;
  generic RATDMA draw during entry rejected; extended-code wire-zero treated
  as termination rejected.
- What it does not prove: that ITU intends per-channel chains (FF-08),
  that the NSS range endpoints match any type-approval oracle, that a real
  receiver decodes anything, or that the five-hour pilot is achievable.
- Change condition: any authoritative clarification of A2-3.3.5.3 or a
  profile change in rates or channel use requires a new policy version.

### 7.3 Low-rate candidate policy (DRAFT only, not adopted, operator decision required)

A `CLASSA-LOWRATE-ITDMA-v1` would need: an LCP-P01 extension defining the
candidate window for NI 6 750 (Table 16 formula gives +/- 675 slots, an
integer; the S3 `selection_interval` currently rejects NI outside
{75, 125, 225, 375} by design); keep = FALSE and increment = 2 NI (13 500,
extended code 5..7) for every periodic Message 3 (FF-17); no terminal zero
while Rr < 2 (FF-15); B's first slot at NSS_A + NI unannounced; NSS domain
[current + 1, current + NI] with NI = 6 750 accepted or replaced by an
explicit shorter entry window (operator choice: the source formula allows a
first transmission up to three minutes after entry); explicit handling of
LCP-P07 duties that fall due before the first B transmission. Until the
operator approves such a policy and A0 records the LCP-P01 version change,
S4 must reject or defer any Rr < 2 configuration explicitly
(`blocked_low_rate_policy_absent`), never fall back to SOTDMA, Type 1 or a
synthetic zero.

### 7.4 Proposed treatment of A0-ISS-003 (review-only patch plan; no A0 file edited)

1. Keep the `A0-ISS-003` CSV row status `UNSPECIFIED_OR_CONFLICT`; append to
   `expected_behavior`: "Adjudicated 2026-10-23 in A1_S4_0_FIRST_FRAME_NORMATIVE_ADJUDICATION.md:
   anchor = first NTS, duration = one frame (derived); per-channel chain and
   terminal zero adopted as SIMULATION_POLICY A0-SIM-017 for Rr >= 2; low
   rate split into A0-ISS-007."
2. Add row `A0-SIM-017` (`SIMULATION_POLICY`, applicability "entry and
   first frame at Rr in {6,10,18,30} under LCP-CLASSA-AUTO-TERR-v1",
   paraphrase "CLASSA-FIRST-FRAME-v1 per-channel first-frame chains anchored
   at each channel's first NTS; terminal zero at anchor + 2 250; keep TRUE",
   test_ids `A0-T03;A0-T10;A0-T19`, phase `A1;I1`, observable evidence
   "two Message 3 chains, two zeros, kept repeats become SOTDMA slots").
3. Add row `A0-ISS-007` (`UNSPECIFIED_OR_CONFLICT`, "Low-rate (Rr < 2)
   entry, selection window, keep and absence of terminal zero", section
   A2-3.3.4.1/A2-3.3.5.3/Table 16 note 2/Table 20, printed 30, 36, 40-41,
   50, test_ids `A0-T10;A0-T19;A0-T29`).
4. Test matrix: A0-T03 gains the F1 per-channel trace reference and the
   policy label; A0-T10 gains F3 with "no PASS until A0-ISS-007 is
   adjudicated".
5. Gap register: A0-GAP-019 lists A0-ISS-007; the A1-S4 row becomes
   "S4-A entry/first frame CONDITIONAL GO under A0-SIM-017; low rate NO-GO".
6. Review document: the LCP table gains the A0-SIM-017 row with its
   non-conformance statement.

These edits are proposals for an operator-authorized A0.2 revision; this
campaign changes no tracked file other than creating the three A1-S4.0
documents.

## 8. Independent checks performed and not performed

Performed (standard library only, logged in `arith_check.log`): unit
constants (9 830 400 / 9 600 = 1 024; 60 * 9 830 400 = 589 824 000;
589 824 000 / 2 250 = 262 144); every instant, UTC minute, slot and index in
Sections 6.1-6.4; the 19-bit ITDMA words 12001, 1, 0, 84938 and 108000
(sync 2 + increment 13 + code 3 + keep 1); the extended-code arithmetic
13 500 - 8 192 = 5 308; the LCP-P07 due instants 367.0 s and 727.0 s;
frame-boundary units 589 561 856 / 589 824 000 / 589 823 999.

Not performed: no C++ build, no CTest, no RF, FCS, UDP or NMEA activity, no
PDF rasterization, no execution of any S1-S3 test (documentation-only
campaign). No test is declared PASS in this document.
