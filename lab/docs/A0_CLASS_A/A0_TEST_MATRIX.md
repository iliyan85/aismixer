# A0 Class A test design

Status: **DESIGN ONLY / NOT RUN**. These are future A1 unit, RF1 unit, I1 closed-loop and P1 playback cases. No packet error rate, propagation result, throughput or Class A conformance result is claimed. Requirements and precise M.1371-6 references are in [the CSV](A0_REQUIREMENTS_TRACEABILITY.csv); its `test_ids` column is the authoritative requirement-to-test relation. The final section here supplies the inverse relation.

## Fixture and oracle contract

All times below are simulation seconds unless expressed in integer units. Use 9,830,400 units/s; a nominal slot is 262,144 units. A default fixture has two independently owned Class A station instances A/B, separately configured MMSIs, AIS1/2 receive processes, one transmitter per equipment, valid independent direct UTC, and an explicit UTC anchor. Own navigation is valid W1 input at 8 kn, status underway, no course change; heading, ROT, accuracy and RAIM remain explicitly unavailable unless a case supplies them. This is an input assumption, not an inference from W1. Static/voyage/manufacturer fields are explicit configuration. A third passive receiver has its own power history.

Local protocol unit fixtures may inject hand-authored **owned, time-valid mock observation tokens** at the A1 input boundary; mark their provenance `UNIT_FIXTURE`, never emit these as RF1 results or claim reception. RF1/I1 cases require the actual physical pipeline and complete frame/FCS evidence. An input token lists owner, receiver, channel, power epoch, physical interval, disclosure instant, decoded bytes/reference or failure disposition, and causal receipt. Test harness access to two independent states is observational; station logic never receives the other state.

Use independent hand calculations, literal source-table fixtures and a separately reviewed packet decoder as oracles. Implementation re-encoding or self-comparison alone is insufficient. Random cases use a fixture-controlled per-station draw ledger, then a seeded sweep against unbiased candidate selection and allowed outcomes; no statistically universal fairness claim follows from a finite sweep. Byte/FCS oracles require the external standard review before acceptance. Any source conflict has an unresolved expectation and cannot produce a normative PASS until adjudicated.

## Mandatory scenarios

### A0-T01 - One-minute initialization

**A1 unit / I1; normative + epoch policy.** ON at t=7, observations belonging to A disclosed at t=10 and 66. At 67 seconds initialization is complete, subject to clock/config eligibility; before 67, assert zero TX requests, including at 67 minus one unit. At 67 assert entry eligibility, not an unconditional emission. Directory contains only delivered local information. Repeat with non-minute-aligned UTC anchor and both channels. Oracle: A2-3.3.5.1 and exact elapsed 60 seconds.

### A0-T02 - Short ON episode and reactivation

**A1 unit / I1; normative + simulation policy.** ON 7, OFF 66, ON 100. First episode has zero TX; second has no TX before160. At159.999... inspect a fresh directory/epoch and no pending old reservation. OFF at exactly the 60-second boundary wins lifecycle applicability for a new start; do not emit. Repeat OFF at boundary minus/plus one unit and stale old-epoch token delivery. Oracle: observation rule plus explicit exact-OFF/phase policy; persistent own configuration is retained.

### A0-T03 - Empty-network entry to continuous operation

**A1 unit, then I1; normative.** ON at0, no remote traffic, direct UTC valid. At60 begin network entry. With RI10 s, derive Rr6 and NI375; fixture draws NSS150 and an eligible NTS150 in the first channel. A possible first TX time is64 s, **Message3 with ITDMA state**, not Type1. Record separate entry-access and first-frame states; entry must not call generic p-persistence. Feed back only actual local results. For a full elapsed minute from first-frame entry, each first-frame emission uses Message3, next same-channel target agrees with its increment/keep, and terminal increment is zero before continuous SOTDMA. Thereafter scheduled autonomous messages use1. Repeat channel-order swap and no candidate/new-NSS retry. The example fixes the first emission only: later slots require an independent two-channel table/figure oracle, not a hardcoded globally granted schedule. First-frame end/rounding ambiguities are reviewed under T19, never concealed by passing the codec test.

### A0-T04 - Asymmetric two-station knowledge

**A1 unit / I1; normative + isolation policy.** A ON0, B ON37; deliver a valid A report only to B after its physical completion; initially deliver no B report to A. At every decision inspect owner-specific map/provenance. B knows its own reservation while A still considers that B-used slot available because it has not decoded B. B also knows A's announced allocation; A knows its own allocation independently. Changing B's undisclosed physical position must not directly change A's FSM. Independent passive receiver results are not ship observations. Invalid owner, future disclosure and global-journal inputs are rejected. Oracle: local evidence, not a globally reconciled map.

### A0-T05 - Frame, wrap and channels

**A1 unit / I1; normative + time policy.** Check slots0,2249 and next frame0 at units0,589561856,589824000 for a minute-aligned anchor. At one unit before/after each boundary inspect quotient/remainder. Use another anchor at UTC59 s and negative POSIX minute. A reservation on AIS1 alone must not appear on AIS2. Equal-rate schedules obey Table16 two-channel offsets and Fig11 effective-next-B-NTS rule. Exercise own cross-channel switching exclusions, periodic alternation, same-channel announced follow-up/reply and channel-22 override. No own simultaneous transmission is possible with one TX. Separate unrelated stations may overlap.

### A0-T06 - Announcement, expiry and reuse

**A1 unit / I1; normative.** Deliver at own RX a valid source report in slot100 on AIS1 with hand-decoded reservation/timeout; check only the advertised future slots/channel. Exercise timeout0 offset0 (no future allocation), positive timeout, ITDMA keep true/false, extended increment, three-frame unused SOTDMA aging, repeated refresh and exactly120 NM versus beyond120 NM. For base reservation require matching Message4+20 MMSI; unpaired20 is ignored. Compare each transition to Tables18-20/A2-3.1.6, not the sender's internal map. Timeout decrement conflict blocks the disputed continuous trace under T19.

### A0-T07 - Garble, bad FCS and absent decode

**A1 unit / RF1 unit / I1; normative + validity policy.** Supply undecodable energy just below/equal/above the A2-3.1.6 threshold, bad FCS, missing end flag, absent frame and a foreign-owner token. None creates decoded MMSI, remote reservation or synchronization state. Class A's garbled/free slot treatment remains distinct from a repeater's garble allocation. Valid complete control frame is the positive comparison. RF1 decides energy/FCS; A1 cannot fabricate them. No-decode does not mean no interfering energy.

### A0-T08 - Candidate hierarchy and timeout replacement

**A1 unit; normative.** Build an SI with more than four free slots on both channels, then only three, then none. Introduce own allocation, opposite-channel own switching exclusion, valid distant mobile allocation, missing-position mobile, protected nearby base and a multi-slot block with occupied middle slot. Verify the ordered A2-3.3.1.2/A2-4.4.1 candidate reasons, at least-four objective subject to restrictions, current-slot fallback only when available, and explicit failure otherwise. Across independent reference draws every permitted candidate is selectable with equal probability. Test timeouts3 and7 and replacement offset into next frame. Source-conflicting decrement/adjacency/rounding expectations stay unresolved under T19.

### A0-T09 - RATDMA variants and exhaustion

**A1 unit; normative.** Queue non-entry traffic with150-slot deadline, candidates at fixture offsets10,40,80,120, RTCSC4, RTA0, RTP2=25. Compare attempt draws below/equal/above RTP2; an inhibit increments RTA, decrements RTCSC and applies the Table14 probability recurrence. A received reservation can remove a candidate without incrementing attempts. Inspect RTES wrap across2249. Do not reset the4s deadline at each inhibit. Separately run network entry and assert no probability gate. Exhaustion/replenishment comparator, numeric extrema and probability representation are T19 unresolved or explicitly approved policy fixtures; never choose Fig10 or prose silently.

### A0-T10 - ITDMA and first frame

**A1 unit; normative.** Encode/decode all wire slot-count codes0..7: semantic lengths1..5 for0..4, lengths1..3 plus8192 increment extension for5..7. Check increment0 termination,13-bit max8191, keep0/1, minute wrap and a3min low-rate schedule. Follow first-frame announced same-channel targets and Message3 transition from T03. Exercise <2 reports/min and exactly1/min; the latter cannot PASS until the conflicting threshold texts are adjudicated. Do not confuse semantic block length with encoded slot-count value.

### A0-T11 - Reporting inputs and interval change

**A1 unit / I1; normative + atomic-input policy.** Supply own SOG milli-knots7999/8000,13999/14000/14001,22999/23000/23001; status underway/anchor/moored; explicit course-change history. Compare row/rationale with Table1. Exercise3kn and0.2kn edges, full10s anchor window and anchor-motion exception; missing heading must not be silently synthesized. Test change expected <2frames versus >=2frames: temporary ITDMA additions versus persistent rescheduling, old-slot release, simultaneous speed/status sample, reduced/increased RI and semaphore2s hysteresis. At14kn or unresolved angular boundaries record competing expectations under T19/T29. Assigned-slower-than-autonomous cannot force a slower report.

### A0-T12 - Independent overlapping choices

**A1 unit / RF1 unit / I1; normative + decentralized policy.** Give A/B blind maps and independent reference draws choosing the same AIS1 slot300. Both requests survive causal scheduling; there is no global grant, slot repair or preselected winner. RF1 evaluates per receiver with geometry and complete intervals; outcomes may differ and are learned only after disclosure. Repeat same time on different channels and with one station locally aware of prior traffic. No numerical RF success expectation is specified before RF1's independently qualified model.

### A0-T13 - Power cut during a physical frame

**RF1 unit / I1 / P1; simulation policy and frame validity.** Begin TX at slot300; OFF at its physical midpoint T. RF energy exists only up to actual cutoff; the incomplete packet never produces successful decode. Repeat receiver OFF during training/data/FCS, OFF exactly at physical end, +/-one unit, and before start. Current R0/StreamingV2 cannot represent every case: expected outcome today is explicit integration NO-GO, not a forged normal TransmissionEnd. Future version must resolve phase0 OFF versus phase1 TX end. A complete earlier reception remains playable after later OFF in wall time.

### A0-T14 - Missing or degraded clock/navigation

**A1 unit / I1; normative + input policy.** Remove direct UTC, then supply eligible direct-peer, base and semaphore reports in priority order. Test one-hop indirect limits, two reports/40s window, equal counts with lower-MMSI tie, nine-frame count, qualification Tables10/11, source expiry and3min semaphore hysteresis. No eligible source must not masquerade as UTC-direct. Check mobile source-relative timing error103/104/105microseconds and accumulated312microseconds; base52/104microsecond limits are separate conditional fixtures. Test nominal bit rate at +/-50ppm and just outside, preserving rounded microsecond versus exact bit-time distinction. Supply valid world position but no EPFS validity, heading, ROT, RAIM or correction evidence; preserve the individual unavailable/default fields and A2-4.1.6 loss behavior. Direct ideal-clock pilot assumption is explicit; degraded/holdover behavior remains separate coverage.

### A0-T15 - Causal and scheduling determinism

**A1 unit / I1; simulation policy.** Run the same semantic inputs/seed with1,2 and4 workers and RF batch cutpoints before/after a disclosure instant and minute boundary. Compare canonical event/state/draw traces, not wall-clock order. No observation becomes usable before its availability instant; reject feedback below committed watermark. Include equal-time lifecycle/RF/map/decision/TX events and producer barriers. Repeated execution comparison is complemented by independently specified expected phase order and decision provenance.

### A0-T16 - Stable station random streams

**A1 unit; simulation policy.** Run A/B with seed S, then add unrelated C and permute station container order. Keep each existing station's semantic purpose/episode draws identical when its inputs are unchanged. Alter A's local observations and allow only its causally affected decisions/draw counts to change. New protocol namespace must not consume W1 Purpose1..16 or a global cursor. Oracle: independently reproduced keyed tuples and per-purpose draw ledger.

### A0-T17 - Payload, communication state and rendering

**A1 unit / RF1 unit / P1; normative + representation policy.** Independently decode first Message3's168 bits and ITDMA19 state; continuous Message1 uses SOTDMA19, assignment Message2 uses SOTDMA19. Include timeout0/1/2..7 submessage cases and channel scope. Type5 is424 AIS data bits in a two-slot physical packet; later receiver NMEA can have multiple fragments with fill bits excluded from RF. Check WGS84/sentinels, EPFS generation timestamp, low-power bit and edition indicator3. Existing Type1/4/9/18/21 cannot stand in for3/5. Exact wire FCS remains gated by external reference review.

### A0-T18 - Conditional base/assignment/poll/silent modes

**A1 unit / I1; normative, future capability.** Deliver owned Message15/16/20/22/23 fixtures with own/wrong address, matching/mismatching base identity, inside/outside120 NM and relevant geographic selectors. Check poll same channel, Message3 response and interrogated Message5 within4s with priority3, preferred ITDMA/preannouncement and specified RATDMA fallback. Check assigned Message2, shorter-autonomous override, assignment expiry4..8min and Table65 report-count rounding/clamp (21 requested per10min becomes40; above600 clamps600); increment7 is ignored and1..6 have their distinct reporting-rate meanings. Validate complete Table70 reservation parameter units and the prose five-slot bound despite the wider field. Check Message23 quiet1/15min and unchanged report interval on the remaining TX channel, regional settings and silent receive/noTX. Use Tables9-11,44,63-65,70,73-75 as independent decision fixtures. A limited pilot declares unsupported controls and cannot claim those cases passed; absence of a base in the topology does not erase their requirements.

## Additional review and integration cases

### A0-T19 - Source conflicts and external references

**A1 design review; UNSPECIFIED_OR_CONFLICT.** Present paired literal locations for Table16 NI maximum versus formula; Fig10 comparator versus prose; timeout double-decrement versus Fig15; low-rate ITDMA thresholds; NI/SI rounding; cross-channel adjacency; Table14 numeric extrema. Include14kn, exactly2kn-without-HDG and angle-wrap issues. For staggered first channels (for example first A64/B74s), adjudicate first-frame timer origins and terminal-zero times rather than presuming one global phase cutoff; include RI>60s. Resolve timeout1 submessage when UTC is unavailable without borrowing Message4 codes. Expected evidence is an authoritative clarification or an explicitly scoped, reviewed simulation policy with a version and regression fixture. A regression asserting a convenient chosen answer is not a normative oracle. External ISO/IEC13239:2002, M.585/RR19, applicable IEC61162/61993-2 and IMO/IALA dependencies remain `EXTERNAL_REFERENCE_REQUIRED` until their actual editions/content are reviewed.

### A0-T20 - Integer time and tie policy

**A1 unit / I1; derived + simulation policy.** Independently calculate9830400/9600=1024,60*9830400=589824000 and frame/2250=262144. Check compose/decompose inverse on negative and positive anchors, units_into_slot0..262143, wrap, and overflow rejection. Source RF tail1ms is9830.4units, so reject an unexplained truncation. Reservation expiry/observation/TX ties use a reviewed ordered policy; unknown fractional phase/leap-second behavior stays declared outside the pilot, never implied by POSIX time.

### A0-T26 - Physical wire and frame envelope

**RF1 unit; normative + unresolved external details.** Start with hand-authored AIS data bytes, including five consecutive ones crossing data/FCS octets; exclude HDLC flags from stuffing. Check preamble/start/end flags, byte order,16-bit FCS, NRZI and recovered data using an independent reviewed oracle. For168 bits with zero stuffing, nominal modulated end is232 bit-times after slot T0; modulation starts after eight ramp bit-times, so its duration is224+S bit-times. Compare the one-stuff-bit Table6 -50dB envelope separately from Fig8 zero-power tail. Exercise maximal stuffing and two-/five-slot packets: no restarting preamble at each slot, no treating all slots as full RF power. RF1 resolves tail threshold/rounding and failure of truncated/bad-FCS packets; this design has not computed FCS or receive success.

### A0-T27 - Channel region, precedence and priority

**A1 unit; normative.** Use eight stored dated regional settings, enter/leave a WGS84 rectangular region, restart with own valid position inside/outside it, and lose position. Check source precedence, refresh/expiry and default/restart against A2-4.1.3-.6 and A7-3.20/Table73. Compare exactly500NM versus farther, exactly24h versus older, overlapping presentation input before/after2h base-message protection, touching-region retention and ninth-entry eviction. Check explicit1/12.5W selection. Current Table73 fixes transitional-zone size to zero; do not import historical transitional-zone machinery. Queue messages at priorities1..4, equal-priority FIFO, then a higher-priority arrival; contrast transmission priority order with received presentation-interface order. No geographical datum is guessed.

### A0-T28 - Conditional transport and repeater behavior

**A1 unit / I1; normative, future capability.** Addressed packet source/destination/type/sequence tuple gets only matching VDL ACK; missing ACK triggers bounded retries and PI success/failure as Annex5 figures23..31. Test4s retry delay, default3 retries, configured0..3 and8min configuration expiry. Broadcast creates no ACK. Distinguish own ClassA from repeater: repeated bits/communication state, repeat count, garbled slot rules and range policy. Test applicable variable binary/safety3-slot default/5-slot FATDMA limits and19/20/21 RATDMA slots per frame against the20-slot budget. These variable-message limits do not replace fixed Message5 rules. Do not implement the repeater in A1 merely because it is inventoried.

### A0-T29 - Heading and rate edge cases

**A1 design/unit; normative + unresolved.** Missing HDG with SOG1999,2000,2001 milli-knots: below2kn suppress heading-based increase; above2kn apply permitted COG fallback for cadence only, never ROT. Exactly2kn needs adjudication. Mean HDG samples359/1 degrees, differential angle near +/-180 and10s anchor history require an approved circular/wrap convention. Check heading deviation below/equal/above5degrees, recovery at exactly20s versus greater, and lower-SOG hysteresis immediately before/at180s. Compare1/min and below1/min ITDMA/SOTDMA texts. No guessed boundary is called normative.

### A0-T31 - TX-to-RF interface validation

**RF1 unit / I1; simulation policy.** Reject request with wrong power epoch, wrong owner, missing immutable payload/physical frame, unresolved AMSL/ellipsoid geometry, start below watermark or incompatible own-transmitter overlap. Positive request includes exact channel and validated bit-count/envelope descriptor; RF result contains per-receiver completeness/FCS/evidence. Radius-only synthetic success and no-frame allocation learning are rejected. Future additive journal schema is required before physical acceptance.

### A0-T32 - Catalog and playback evidence

**I1 / P1; simulation policy.** Persist station/equipment/TX/RX ownership, catalog versions and simulation power epochs. Decode at t100, OFF at101, replay at later wall time: output original valid frame/NMEA with original simulation identity. Reject token catalog-version mismatch and stale-epoch influence on a new live station. Existing journal-v1/StreamingV2 golden bytes remain unchanged; new schema has separate versioned fixtures.

### A0-T33 - Own metadata and sensor authority

**A1 unit; normative + configuration policy.** Explicit nine-digit MMSI still needs allocation validation; zero/missing/wrong-vessel identity suppresses TX. Give W1 position/SOG/COG/status but omit HDG/ROT/integrity/static metadata; assert no fabrication. Add valid TI/RAIM/correction inputs for positive cases. Check Table48 independently, Type5 dimensions/reference point/DTE and voyage unavailable codes. Antenna mounting is not ship dimensions or absolute ellipsoid height. Review actual external identity/presentation editions before accepting conformance.

### A0-T34 - Class A Message24B deadline

**A1 unit / I1; normative.** ON0 and remain active through720s with explicit manufacturer metadata; observe at least one applicable24B within12min and24h recurrence in an extended fixture. Repeat own Message24 interrogation: Manufacturer-ID-only PartB response. A startup-only fixture shorter than the deadline may isolate access; a five-hour pilot cannot silently omit24B. No ClassB six-minute A/B cadence substitution.

### A0-T35 - Capability limits and additional message duties

**A1 unit / I1; normative + capability policy.** Declare supported autonomous/assigned/polled/safety/binary/satellite features. For full satellite support, Message27 alternates75/76 at nominal3min, selects MSSA from local terrestrial maps, respects paired4+23 control and3min expiry. Validate conditional message IDs/priority/access/channel against Table44 and Annex7, including mobile11 versus base4. In an AIS1/2-only pilot record each deferred duty; reject a full-ClassA claim. Adding a base or crossing an applicable time limit triggers the corresponding gate, not silent behavior.

## Requirement coverage index

The generated inverse relation below is checked against the CSV. All listed tests remain NOT RUN.

<!-- REQUIREMENT_INDEX -->

| Test | Traced requirement IDs |
|---|---|
| A0-T01 | A0-GEN-001, A0-SIM-001, A0-MAC-011, A0-MAC-025, A0-RF-005, A0-INT-003 |
| A0-T02 | A0-SIM-001, A0-SIM-004, A0-MAC-011, A0-RF-014, A0-INT-003 |
| A0-T03 | A0-GEN-001, A0-GEN-009, A0-SIM-005, A0-ISS-003, A0-MAC-011, A0-MAC-015, A0-MAC-018, A0-MAC-023, A0-MAC-025, A0-MAC-026, A0-MAC-027, A0-MSG-001, A0-INT-012 |
| A0-T04 | A0-SIM-001, A0-SIM-003, A0-MAC-004, A0-RF-024, A0-INT-001, A0-INT-002, A0-INT-010, A0-INT-011 |
| A0-T05 | A0-GEN-007, A0-TIM-001, A0-TIM-002, A0-MAC-006, A0-MAC-007, A0-MAC-009, A0-MAC-014, A0-MAC-023, A0-MAC-024, A0-MAC-032, A0-MAC-033, A0-MAC-039, A0-MAC-040, A0-RF-001, A0-RF-002, A0-RF-005, A0-RF-009, A0-RF-013, A0-RF-023, A0-MSG-009, A0-MSG-014 |
| A0-T06 | A0-MAC-008, A0-MAC-012, A0-MAC-016, A0-MAC-021, A0-MAC-022, A0-MAC-027, A0-MAC-029, A0-MAC-031, A0-MAC-032, A0-RF-005, A0-RF-008, A0-RF-022, A0-RF-024, A0-MSG-013, A0-INT-005, A0-INT-010 |
| A0-T07 | A0-SIM-003, A0-SIM-007, A0-MAC-007, A0-MAC-008, A0-RF-007, A0-RF-008, A0-RF-024, A0-INT-001, A0-INT-005, A0-INT-011 |
| A0-T08 | A0-SIM-002, A0-MAC-008, A0-MAC-012, A0-MAC-013, A0-MAC-014, A0-MAC-022, A0-MAC-024, A0-MAC-027, A0-MAC-037, A0-MAC-039, A0-MAC-040, A0-RF-010, A0-RF-022, A0-RF-023, A0-MSG-013, A0-INT-006 |
| A0-T09 | A0-GEN-009, A0-SIM-002, A0-MAC-012, A0-MAC-015, A0-MAC-018, A0-MAC-019, A0-MAC-020, A0-MAC-021, A0-MAC-025, A0-MAC-036, A0-MAC-041, A0-INT-006 |
| A0-T10 | A0-ISS-003, A0-MAC-015, A0-MAC-016, A0-MAC-017, A0-MAC-026, A0-MAC-031, A0-MAC-033, A0-MAC-038, A0-RF-013, A0-RF-016, A0-RF-021, A0-MSG-001 |
| A0-T11 | A0-GEN-003, A0-GEN-004, A0-GEN-005, A0-SIM-006, A0-ISS-001, A0-MAC-005, A0-MAC-015, A0-MAC-023, A0-MAC-028, A0-MAC-029, A0-MAC-030, A0-RF-017, A0-RF-018, A0-RF-019, A0-RF-020, A0-RF-021, A0-MSG-004, A0-MSG-005, A0-MSG-012, A0-MSG-014, A0-INT-007, A0-INT-010 |
| A0-T12 | A0-SIM-003, A0-SIM-007, A0-MAC-014, A0-INT-002, A0-INT-011 |
| A0-T13 | A0-TIM-003, A0-SIM-004, A0-SIM-007, A0-MAC-009, A0-RF-011, A0-INT-003, A0-INT-004, A0-INT-005, A0-INT-009, A0-INT-011 |
| A0-T14 | A0-GEN-003, A0-GEN-004, A0-TIM-002, A0-SIM-005, A0-RF-031, A0-ISS-004, A0-MAC-001, A0-MAC-002, A0-MAC-003, A0-MAC-004, A0-MAC-005, A0-MAC-006, A0-MAC-007, A0-RF-017, A0-RF-018, A0-RF-019, A0-RF-021, A0-RF-022, A0-MSG-003, A0-MSG-004, A0-MSG-005, A0-MSG-006, A0-MSG-016, A0-INT-001, A0-INT-005, A0-INT-007 |
| A0-T15 | A0-SIM-002, A0-SIM-003, A0-SIM-004, A0-SIM-006, A0-MAC-019, A0-INT-001, A0-INT-002, A0-INT-005, A0-INT-006, A0-INT-009, A0-INT-010 |
| A0-T16 | A0-SIM-002, A0-INT-006 |
| A0-T17 | A0-GEN-006, A0-SIM-008, A0-ISS-004, A0-MAC-009, A0-MAC-013, A0-MAC-016, A0-MAC-017, A0-MAC-025, A0-MAC-026, A0-MAC-032, A0-MAC-033, A0-MAC-034, A0-RF-003, A0-RF-006, A0-RF-007, A0-RF-009, A0-RF-010, A0-RF-011, A0-RF-026, A0-RF-029, A0-RF-030, A0-MSG-001, A0-MSG-002, A0-MSG-003, A0-MSG-004, A0-MSG-005, A0-MSG-006, A0-MSG-007, A0-MSG-008, A0-MSG-009, A0-MSG-011, A0-MSG-017, A0-INT-007, A0-INT-008, A0-INT-012 |
| A0-T18 | A0-GEN-001, A0-GEN-005, A0-SIM-008, A0-MAC-001, A0-MAC-002, A0-MAC-003, A0-MAC-004, A0-MAC-005, A0-MAC-010, A0-MAC-022, A0-MAC-024, A0-MAC-030, A0-MAC-031, A0-RF-001, A0-RF-004, A0-RF-012, A0-RF-013, A0-RF-014, A0-RF-015, A0-RF-020, A0-RF-022, A0-RF-024, A0-RF-025, A0-RF-028, A0-MSG-001, A0-MSG-009, A0-MSG-010, A0-MSG-011, A0-MSG-012, A0-MSG-013, A0-MSG-014, A0-MSG-015, A0-MSG-016, A0-MSG-017, A0-MSG-018, A0-INT-012 |
| A0-T19 | A0-TIM-004, A0-ISS-001, A0-ISS-002, A0-ISS-003, A0-ISS-004, A0-MAC-021, A0-MAC-024, A0-MAC-027, A0-MAC-035, A0-MAC-036, A0-MAC-037, A0-MAC-038, A0-MAC-039, A0-MAC-040, A0-MAC-041 |
| A0-T20 | A0-TIM-001, A0-TIM-002, A0-TIM-003, A0-SIM-004, A0-RF-031 |
| A0-T26 | A0-TIM-004, A0-ISS-002, A0-RF-031, A0-RF-002, A0-RF-003, A0-RF-006, A0-RF-007, A0-RF-008, A0-RF-009, A0-RF-010, A0-RF-011, A0-RF-027 |
| A0-T27 | A0-GEN-007, A0-RF-004, A0-RF-012, A0-RF-013, A0-RF-014, A0-RF-015, A0-RF-025 |
| A0-T28 | A0-GEN-010, A0-RF-026, A0-RF-027, A0-RF-028, A0-RF-029, A0-RF-030 |
| A0-T29 | A0-ISS-001, A0-RF-016, A0-RF-018, A0-RF-019 |
| A0-T31 | A0-TIM-004, A0-SIM-007, A0-MSG-007, A0-MSG-018, A0-INT-004, A0-INT-005, A0-INT-008, A0-INT-011 |
| A0-T32 | A0-TIM-003, A0-INT-003, A0-INT-004, A0-INT-008, A0-INT-009 |
| A0-T33 | A0-GEN-002, A0-GEN-006, A0-ISS-002, A0-MSG-003, A0-MSG-005, A0-MSG-006, A0-MSG-007, A0-MSG-008, A0-INT-007 |
| A0-T34 | A0-SIM-008, A0-MSG-015, A0-INT-012 |
| A0-T35 | A0-GEN-007, A0-GEN-008, A0-GEN-009, A0-SIM-008, A0-MSG-009, A0-MSG-010, A0-MSG-011, A0-MSG-012, A0-MSG-013, A0-MSG-014, A0-MSG-015, A0-MSG-016, A0-MSG-017, A0-MSG-018, A0-INT-012 |
