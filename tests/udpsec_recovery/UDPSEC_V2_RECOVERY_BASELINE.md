# UDPSEC V2 Recovery Baseline and Acceptance Contract (MP0, updated by MP1)

| | |
|---|---|
| Status | MP0 baseline (tests and documentation) plus MP1 client liveness recovery. Astra Gate A: NEEDS CORRECTION BEFORE MP2 (three LOW findings, no BLOCKER/HIGH; the core design accepted). F1-F3 corrected in one corrective iteration (section 11.2). The Astra corrective recheck accepted F2, F3 and the `retransmit_ping` part of F1. The remaining `start_ping` part of F1 is corrected in a final micro-correction, implementer-verified and awaiting the final Astra micro-recheck. MP1 changed one production file, `nmea_sproxy/nmea_sproxy.py`. |
| Branch / commits | `main`. Pre-MP1 production baseline `de513674fabf2ee594d0f08f10ddbdf87267a352` (2026-09-25, "feat(udpsec): add authenticated field diagnostics for mobile path migration"), unchanged by MP0. MP0 committed as `71a47894264105531b0d873b09d0520ba90e15eb`. MP1 is the working-tree delta on top of `71a47894`. |
| Written | MP0 2026-09-29; MP1 update 2026-09-29; Gate A corrective iteration 2026-09-30 |
| Serves the next instruction | Final Astra micro-recheck of the `start_ping` anchor only (section 11.2); then MP1 road validation; then MP2. |
| Normative behaviour | `BEHAVIORAL_CONTRACT.md` section 11, rewritten by MP1 (see its 11.5). This file adds no guarantee beyond it. |

Companion files in this directory:

| File | Role |
|---|---|
| `SCENARIO_MATRIX.md` | One row per scenario: class, network conditions, current result, future target, provenance. |
| `EVIDENCE_LEDGER.md` | Every important claim with its evidence class (code, project test, Fable, Astra, field, implementer-only, not verified). |
| `harness/lab.py`, `harness/scenarios.py`, `harness/expect.py` | Deterministic lab: repository port of the Fable harness. |
| `test_recovery_invariants.py` | Ordinary tests that must keep passing through MP1 and MP2. They passed unchanged through MP1. |
| `test_mp1_liveness_acceptance.py` | MP1 acceptance tests MP1-L1..L12. MP0 wrote L1-L8 as `xfail(strict=True, raises=AssertionError)`; MP1 removed the markers without changing an assertion and added L4d, L7b, L8b, L11a, L11b and L12. |
| `test_mp1_policy_pins.py` | Exact pins of the MP1 policy (constants, cadence, every scenario MP0 pinned) plus the unchanged pre-MP2 pins. Replaces MP0's `test_pre_mp1_policy_pins.py`. MP2 replaces the pre-MP2 pins. |
| `test_mp1_liveness_units.py` | Unit tests for MP1 mechanisms that no lab scenario isolates (pure helpers, drain bound, evidence drained after a stall, errno classification, input pause, counting). |
| `test_baseline_consistency.py` | Keeps the matrix, the harness catalogue and the acceptance tests consistent; forbids xfail markers in the acceptance module. |

Run: `python -m pytest tests/udpsec_recovery` (from the repository root, as for the whole suite).

---

## 1. Scope and provenance

MP0 turns evidence that already existed into one repository-local recovery
baseline. It defines what must survive before any production change. It does
not redesign UDPSEC V2, change keepalive, migration, timers, crypto, the
transcript, the wire format or the protocol version. MP1 later changed the
client's keepalive/liveness policy only (sections 7.1, 9.1 and 11.1).

Inputs (ingested read-only from the operator's machine; not committed):

| Input | Identity | Used for |
|---|---|---|
| Fable report `UDPSEC_V2_FABLE_AUDIT.md` (2026-09-28, audit of `de513674`) | sha256 `4b48f1ac…eaf41300` | findings F1-F12, traces, designs L1-L3/M1-M3 |
| Fable `UDPSEC_V2_REPAIR_BACKLOG.md` | sha256 `71dd17c6…795934bd` | phase ideas P0-P4 (superseded by section 10 here) |
| Fable `UDPSEC_V2_TEST_MATRIX.md` | sha256 `075001eb…fd4c3198` | scenario IDs T01-T16 and observed values |
| Fable assignment `AISMixer_Fable_UDPSECv2_Deep_Independent_Audit_20260927.txt` | sha256 `708371bf…ee0dbdc7` | audit questions, prior-audit provenance |
| Fable harness `udpsec_lab.py`, `scenarios.py` (+ `pytest.py` stub, `show.py`, 26 logs) | sha256 `8285768f…ee070794`, `5eb227ed…58a5a24b` | ported to `harness/` (section 12) |
| Field screenshot `photo_2026-09-27_11-47-51.jpg` (IPv6 run) | sha256 `4ad08422…0ac925b6` | section 4.1 |
| Astra resume note `aismixer_astra_RESUME_budget_capped_20260925.txt` | sha256 `a739122c…ef132f20` | prior Astra evidence (ledger) |
| IPv4/CGNAT road test and Wi-Fi-loss experiment | operator report in the MP0 instruction; no logs supplied | sections 4.2, 4.3 |

Full hashes are in `EVIDENCE_LEDGER.md`.

**Location.** `AGENTS.md` forbids reintroducing `docs/` on `main`, and root
Markdown files are reserved for public/operator documents. The baseline lives
next to the executable tests that enforce it, so one path
(`tests/udpsec_recovery/`) carries the contract, the matrix, the ledger and
the proof.

## 2. Pre-MP1 liveness behaviour (baseline)

These are the facts of the pre-MP1 client, kept as the record MP1 was measured
against. MP1 replaced L2, L3, L6 and L7 and the arithmetic of L5. L1, L4 and
L8 still hold. L9 still holds too: input pauses only after a transient local
send failure, never because a ping is unresolved. L10-L12 (server) are
untouched. Section 7.1 and
`BEHAVIORAL_CONTRACT.md` section 11 state the MP1 rules.

Source line numbers refer to the baseline commit. MP0 changes no production
file, so they hold for the MP0 tree too. `BEHAVIORAL_CONTRACT.md` line
numbers refer to the MP0 tree: MP0 inserted two lines after line 1039 and a
new subsection 11.5. `nmea_sproxy.py` means `nmea_sproxy/nmea_sproxy.py`.

| # | Fact | Where |
|---|---|---|
| L1 | At a keepalive deadline with nothing outstanding the client sends ONE encrypted ping (`seq` = next integer) and records `expected_ping_seq`. | `nmea_sproxy.py:2634-2651`, `2702-2718` |
| L2 | That ping is never retransmitted and never replaced by a later ping. | `session_deadline_action` `1975-1995`; contract `1029-1031` |
| L3 | If `expected_ping_seq` is still set at the next keepalive deadline (`last_ping_at + keepalive_interval`, equality included), `forward_loop` prints "Secure session liveness unresolved; starting authenticated re-handshake." and returns `proactive_rekey`. | `1991-1994`, `2062-2066`, `2652-2657` |
| L4 | `retry_delay_for_reason(proactive_rekey)` is `None`: `main()` starts a fresh signed ECDHE handshake at once. A failed attempt then waits `reconnect_delay`. | `2104-2110`, `3258-3264` |
| L5 | Defaults are `keepalive_interval: 30`, `peer_timeout: 90`, `session_refresh_interval: 0`, `reconnect_delay: 5`. An unanswered ping forces a rekey 30 s after it was sent, at most 60 s after the last accepted PONG, so `peer_timeout` (90 s) is normally pre-empted. Ignoring RTT, `peer_timeout` fires first only when `keepalive_interval >= peer_timeout / 2`. | `DEFAULT_CONFIG` `195-208`; both shipped `config*.yaml` |
| L6 | Each loop iteration evaluates deadlines at four points (`2726`, `2768`, `2798`, `2878`). The check at `2798` runs after `select()` returns and before `recvfrom` (`2806`). The check at `2878` runs after a datagram is decoded but before its liveness effect is applied (`2883`). A matching PONG that is readable when a deadline is already due, or whose processing crosses a deadline, is therefore discarded. The next handshake skips it as old-session DATA. | `2791-2891`; contract `1043-1051` |
| L7 | Any exception from a local `sendto` inside `forward_loop` (NMEA, ping) or from `select`/`recvfrom` ends the session with `socket_error`, followed by `reconnect_delay`. A transient `ENETUNREACH` therefore destroys the session immediately (scenario A11). The sentence whose send failed is not retried. | `2645-2647`, `2713-2715`, `2760-2762`, `2794-2796`, `2807-2809` |
| L8 | `main()` creates ONE UDP socket per relation (`3162`, timeout 5 s `3166`) and reuses it for every handshake. A fresh session therefore normally keeps the same local port and, behind stable NAT, the same server-observed tuple. | `3161-3166`, `3229-3264` |
| L9 | NMEA forwarding never pauses while a ping is unresolved. NMEA is client-to-server only; the client cannot observe its delivery. | loop structure `2725-2982` |
| L10 | Server: every authenticated, replay-admitted ping from the session's active path is answered with a PONG echoing its `seq`, sent to `active_path` under the ping's epoch. There is no sequence-monotonicity check. A ping from an unproved or retired path gets no PONG. | `aismixer_secure.py:5085-5131` |
| L11 | Server: an unknown locator (including after a server restart) is a silent drop: no reply, no counter. | `aismixer_secure.py:4772-4783` |
| L12 | Server: an active session expires after `SESSION_TTL_SECONDS = 300` idle. A fresh session promoted at the same relation replaces the old one (`sessions_replaced`); at a different relation the old one lingers until the TTL. | `aismixer_secure.py:87`, `promote_pending_session` `2803` |

### 2.1 Authenticated liveness evidence inventory (for MP1 failure accounting)

What the pre-MP1 code accepts as peer-liveness evidence, and what it does
with it. MP1 failure accounting must not contradict this table (section 7.1,
R5), and must never add a row for unauthenticated input. MP1 kept every row;
it credits each advancing row as of the instant the datagram is read (the
PATH_ACK row no longer waits for a two-phase final admission) and added the
last row.

| Evidence (client side) | Advances `last_authenticated_peer` | Clears the outstanding ping | Where |
|---|---|---|---|
| Authenticated PONG from the pinned tuple, current locator, allowed epoch, `seq == expected_ping_seq` | yes | yes | `handle_server_packet` `1719-1761`; `2883-2891` |
| First verified REFRESH_REPLY (`REFRESH_PROGRESS`) | yes | **no** | `2832-2834`; contract `1383-1389` |
| Genuine REFRESH_ACK commit (`REFRESH_COMMITTED`) | yes | **no** | `2823-2831` |
| PATH_ACK matched against a live, unexpired, same-epoch client proof | yes, after the two-phase final admission | only if that proof captured this exact `seq` | `2892-2965`; contract `1714-1828` |
| Duplicate REFRESH_REPLY (`REFRESH_BENIGN`) | no | no | `2835-2839` |
| PATH_CHALLENGE, unmatched/expired/replayed PATH_ACK | no | no | `_ClientPathMigration` `1292-1406` |
| PONG with no outstanding ping, wrong/stale `seq`, duplicate after clear | no | no | `protocol.is_matching_pong_message` |
| Anything plaintext, wrong key, wrong address, wrong locator, malformed; server-to-client NMEA-typed messages | no | no | contract `1014-1027` |
| Client-to-server NMEA delivery | not observable by the client | — | L9 |
| ICMP-derived receive error (`ECONNRESET`/`ECONNREFUSED` on `recvfrom`; MP1) | no | no | MP1 `receive_datagram`; scenario A11b |

## 3. Current migration behaviour (pre-MP2)

| # | Fact | Where |
|---|---|---|
| M1 | An authenticated, replay-admitted `nmea` or `ping` from an off-path source under the CURRENT epoch opens or replaces the session's single candidate. | `_process_candidate_path_observation` `4424-4477`; `open_or_replace_candidate_path` `3552` |
| M2 | Exactly ONE PATH_CHALLENGE per candidate incarnation (`outcome == "installed"`). Duplicate traffic on a live candidate sends nothing. | `4459-4460`; contract 11.3 item 5 |
| M3 | `PATH_CANDIDATE_TTL_SECONDS = 10.0`, never extended. After expiry the next authenticated off-path packet opens a new incarnation (new `path_generation`, new challenge). | `aismixer_secure.py:127` |
| M4 | A matching PATH_RESPONSE from the candidate's exact address commits atomically. The PATH_ACK goes to the new active path. The old path is retired for `RETIRED_PATH_GRACE_SECONDS = 5.0` (late nmea/ping admitted, no reverse migration). | `commit_candidate_path` `3700-3869`; `:134` |
| M5 | A commit preserves the `LogicalSession` object, the `CryptoEpoch` object and generation, the per-epoch replay ledger object and the `assembly_namespace`. `path_generation` is per session; `active_path_generation` is the generation that last won a commit. | contract 11.3 items 1-4; T08 |
| M6 | Pings from an unproved path get no PONG (L10). A ping that overtakes a PATH_RESPONSE which captured no ping stays unresolved. The later PATH_ACK advances liveness but cannot clear it, so L3 fires (T09d). Since MP1 the ping is retransmitted from the now-active path and answered (A10). | `5085-5106`; contract 11.3 item 8 |
| M7 | A lost CHALLENGE, RESPONSE or ACK is recovered without re-handshake, through candidate expiry and re-open (about 10 s plus the next authenticated packet) or through the next PONG (lost ACK). | T09a/b/c; e2e `test_06/07/08` |

## 4. Field observations

These are operator observations. Their status in the evidence ledger is FIELD
OBSERVATION. None of them identifies which packet was lost in any event.

### 4.1 IPv6 road run (Raspberry Pi, serial input, Vivacom direct IPv6)

Screenshot of the client console, 2026-09-27; IPv6 address redacted here:

```
Runtime: … forwarded=640 … session=8fb44452 epoch=0 peer=alive observed=<IPv6>.46770 path_gen=0 age=28s
Secure session liveness unresolved; starting authenticated re-handshake.
Refreshing secure session immediately.
Mutual ECDHE session confirmed.
Runtime: … forwarded=650 … session=a949cda4 epoch=0 peer=alive observed=<same IPv6>.46770 path_gen=0 age=28s
```

Supported:
- The session was re-established after "liveness unresolved" (L3).
- The old and new sessions have different labels; the new one is `epoch=0`, `path_gen=0`.
- The same externally observed IPv6 address and UDP port appear before and after.
- The first handshake attempt succeeded, so the path was usable at the deadline instant.
- Visible session failure does not require a tuple change.

Not proven:
- Whether the cause was a lost PING, a lost PONG, a short blackhole, a client stall, or a transient A→B→A flap between two PONGs.
- The outage duration. `age=28s` is heartbeat phase, not outage length.
- Whether any NMEA was lost server-side.

### 4.2 IPv4 / CGNAT road run (Vivacom IPv4, Balchik → Kavarna → Balchik → Kranevo)

- The public observed tuple stayed stable for long road intervals; a captured tuple was `90.154.211.195:54654`.
- Session re-establishments still occurred.
- `path_gen` stayed `0` in the captured ordinary-driving examples.

Conclusion supported: cellular handover and ordinary mobility do not
necessarily change the public IP or port, so re-establishments on this run
cannot be attributed to migration. No client or server logs were supplied, so
the event causes are not verified.

### 4.3 Deliberate long Wi-Fi loss (phone hotspot moved away; about 5-10 min)

- During the outage: `Handshake send error: [Errno 101] Network is unreachable` / `Retrying in 5 seconds...`.
- After recovery: a fresh mutual ECDHE session with the same public IPv4 `90.154.211.195`, but the observed CGNAT UDP port changed from `54654` to `54104`. The new session had `path_gen=0`.

Classification: **terminal recovery (class C), not a failed migration.** The
old logical session was already gone:
- the client abandoned it at the first local send error (L7) or at the keepalive deadline;
- the server expires it after 300 s idle (L12).

No old-session traffic ever arrived from the new tuple, so no candidate could
exist. The server-observed CGNAT port is the NAT's mapping. It is not
necessarily the Raspberry Pi's local UDP source port (L8 keeps the local port;
the NAT chose a new public one). Reproduced as C1/C2.

### 4.4 Consequence

The assumption "MOBILE NETWORK CHANGE == PUBLIC IP/PORT CHANGE" is falsified by
4.1 and 4.2. The dominant observed failure class is same-tuple (class A).
Hence the ordering: **packet-loss tolerance first, path migration second,
full re-establishment last.**

## 5. Recovery classes

| Class | Definition | Examples | Future target |
|---|---|---|---|
| **A** | Temporary reachability loss, SAME tuple | isolated PING or PONG loss; short two-way blackhole; asymmetric loss; bounded client stall; PONG at or near a deadline | Preserve the `LogicalSession` (and its epoch, replay ledger, path, `path_gen`) when bounded authenticated recovery proves the peer is still viable. |
| **B** | Short reachability loss WITH a real tuple change | NAT rebinding or public port change while the logical session is still legitimately alive | Existing migration validates the new path in time; preserve the `LogicalSession`; no liveness decision may pre-empt a migration that is completing. |
| **C** | Terminal or long outage | no route or Wi-Fi gone for minutes; server restart (unknown locator); recovery bounds exhausted | Fresh authenticated establishment remains correct, within documented bounds and with bounded backoff. |

Architecture ordering: **LIVENESS RECOVERY FIRST → PATH MIGRATION SECOND →
FRESH ESTABLISHMENT LAST.**

Telling them apart:
- **A:** a re-establishment with unchanged `observed=`, no server candidate OPENED line or `path_candidates_opened` delta, and a first-try handshake.
- **B:** a server candidate OPENED (and COMMITTED or EXPIRED), with `observed`/`path_gen` changing.
- **C:** handshake failures ("No response" or local send errors) before the new session.

## 6. Reproduced pre-MP1 failures and their MP1 outcomes

Every pre-MP1 outcome below was reproduced in-repo by the MP0 lab on the
baseline commit (real client, real server, fake clock) and pinned by MP0's
`test_pre_mp1_policy_pins.py`. The rows marked (MP1) are scenarios MP1 added;
their pre-MP1 outcome comes from one implementer run of the MP1 lab against the
pre-MP1 `nmea_sproxy.py`. The MP1 outcomes are implementer-run, pinned
exactly in `test_mp1_policy_pins.py`, and not yet independently verified
(Gate A). Times are fake-clock seconds; the session starts at 1000.2 and
ping#2 leaves at 1060.2.

| ID (Fable) | Fault | Pre-MP1 outcome, reproduced | MP1 outcome | MP1 test |
|---|---|---|---|---|
| A2 (T02) | ping#2 lost | `proactive_rekey` at 1090.2; new session at 1090.4 (`epoch 0`, `path_gen 0`, same tuple); 19/19 NMEA delivered; server `sessions_replaced=1` | ping#2 retransmitted at 1090.2 (same seq, fresh nonce), answered 1090.3; same session; 19/19 NMEA prompt | MP1-L1 |
| A3 (T03) | pong#2 lost | identical; the server received the ping and never noticed | as A2 | MP1-L2 |
| A4 (T04) | two-way blackhole 1058-1063 | rekey at 1090.2, 27 s after recovery; 1 NMEA lost inside the hole | retransmission at 1090.2 answered; same session; the same 1 line lost | MP1-L3 |
| A4b (T04b) | two-way blackhole 1058-1095 | rekey 1090.2, then ClientHello lost, failure at 1095.2, 5 s backoff, new session 1100.4 (+5.4 s after the path returned) | retransmission 1090.2 lost, 1095.2 answered at 1095.3; same session; the 4 in-hole lines lost | pin (7.3 decided) |
| A5b (T05b) | pong#2 readable exactly at its deadline | rekey; the PONG is consumed by the next handshake, never by `forward_loop` | the PONG is read and credited at 1090.2 before the deadline is serviced; no retransmission | MP1-L4a |
| A5c (T05c) | pong#2 10 ms after its deadline | rekey | retransmission at 1090.2; the original PONG (1090.21) resolves the ping | MP1-L4c |
| A5d (MP0) | pong#1 readable exactly at the `peer_timeout` boundary (`peer_timeout: 45`) | `peer_timeout` wins; the PONG is discarded; 5 s backoff | the PONG is credited at the 1045.2 boundary; same session | MP1-L4b |
| A5e (MP1) | pong#1 readable 10 ms after that boundary | `peer_timeout` at 1045.2; 5 s backoff; new session 1050.4 | `proactive_rekey` at 1045.2 (a probe is outstanding): immediate new session at 1045.4; the late PONG stays late | MP1-L4d |
| A6 (T07) | client stall 1060.26→1095 with pong#2 buffered | rekey on resume at 1095.0; the buffered PONG is discarded | the buffered PONG is credited on resume (1095.0); same session | MP1-L5 |
| A7 (T06a) | server→client lost 1055-1155, client→server healthy | rekey 1090.2; 7 failed handshakes; new session 1160.4; a 70.2 s forward-data outage while the forward path was healthy | forwarding continues; ping#2 retransmitted 7 times (1090.2-1120.2); `proactive_rekey` at 1120.3 (pong#1 + 90); new session 1160.5; 40.2 s outage; no line lost | MP1-L6, MP1-L12 |
| A9 (T14) | pong#2 lost; refresh committed at 1065.4 | rekey 1090.2 despite the authenticated refresh evidence | retransmission at 1090.2 answered; same session | MP1-L7 |
| A9b (MP1) | every PONG after pong#1 lost; refresh every 65 s | re-handshakes at 1090.2, 1180.4 and 1270.6 despite refresh evidence | same session for the whole window on refresh evidence alone; ping#2 retransmitted every 5 s | MP1-L7b |
| A10/B5 (T09d) | A→B at 1059.9; ping#2 overtakes the PATH_RESPONSE | migration commits (1060.55), ACK accepted; rekey 1090.2 anyway; the new session starts at `path_gen 0` | the retransmission at 1090.2 leaves from B and is answered; the migrated session (`path_gen` 1) is kept | MP1-L8 |
| A10b (MP1) | as A10, every PONG after pong#1 lost | re-handshakes at 1090.2 and 1180.4 | the ACK (read 1060.6) is the last evidence: `proactive_rekey` at 1150.6, not at pong#1 + 90 = 1120.3 | MP1-L8b |
| A11 (MP0) | local `ENETUNREACH` 1059.9-1062.0 | `socket_error` at 1060.0; 5 s backoff; new session 1065.2; the failed sentence is lost | the failed sentence is dropped and input pauses; the ping at 1060.2 fails; the probe at 1065.2 succeeds and is answered; same session; only the failed sentence lost | MP1-L11a |
| A11b (MP1) | one ICMP-derived `ECONNRESET` on `recvfrom` at 1070 | `socket_error` at 1070.0; 5 s backoff; new session 1075.2 | reported once, ignored; same session | MP1-L11b |
| C3/C3b | 242 s two-way / 100 s forward-path blackhole | rekey 1090.2; 4 lines lost | `proactive_rekey` at 1120.3; 7 lines lost (trade-off, 7.3) | pin; MP1-L9 guard |
| C5 (T16) | server restart at 1045 | 5 NMEA silently dropped (unknown locator); rekey at 1090.2 | 8 NMEA silently dropped; `proactive_rekey` at 1120.3; new session 1120.5 (trade-off, 7.3) | pin; guard |

## 7. Acceptance semantics

MP0 defined these constraints without implementing them or choosing any state
names, retry counts or timing constants. MP1 implemented 7.1; each
requirement below ends with an **MP1** note on how it is met and tested.

### 7.1 MP1 — client liveness recovery (contract; implemented)

- **R1 Same logical ping, fresh nonce.** A retry may reuse the logical ping
  `seq`. Every transmission is a new AEAD encryption with a fresh 96-bit
  nonce: no nonce or ciphertext reuse. Retries are bounded, and state is O(1)
  per session (no history-scaled state).
  - Enforced by: the D9 sweep (all scenarios, no nonce reuse per direction/locator/epoch).
  - Wire compatibility is proven by L10: the current server answers a same-`seq` ping sent under a fresh nonce (and a lower `seq`), and refuses a byte-identical replay.
  - **MP1:** `retransmit_ping` re-encrypts the outstanding ping under the current epoch for every transmission; nothing is cached. The state is `ping_retry_at` and `ping_transmissions`. Tests: MP1-L12 (A7: 8 transmissions of ping#2, distinct nonces and ciphertexts), the D9 sweep, and `test_proxy_outstanding_ping_is_retransmitted_not_overwritten_until_peer_timeout`.
- **R2 Staged liveness.** One missed keepalive deadline must not by itself end
  the logical or cryptographic session. There must be a bounded suspect/retry
  phase, or equivalent, before any terminal verdict.
  - Acceptance: MP1-L1, L2, L3, L4c, L5, L6, L7, L8.
  - **MP1:** an unanswered ping is retransmitted at its keepalive deadline and then every `min(5 s, keepalive_interval)` (`KEEPALIVE_RETRY_INTERVAL_SECONDS = 5.0`). The first retransmission logs "liveness suspect"; an answer to a retransmitted ping logs "liveness recovered".
- **R3 `peer_timeout` becomes meaningful.** The configured `peer_timeout`
  must be a real upper fallback. It must not normally be made unreachable by
  one missed probe. MP1 documents its terminal bound in terms of
  `peer_timeout` and any new constants.
  - **MP1:** the only terminal liveness bound is last authenticated evidence + `peer_timeout` (equality due). It ends with `proactive_rekey` (immediate re-handshake) when a probe is outstanding, else `peer_timeout` (`reconnect_delay` first). With the defaults a silent peer is declared dead 90 s after the last evidence. The retransmission count of that episode depends on the case (corrected after Gate A F2):
    - at most ceil((`peer_timeout` - `keepalive_interval`) / retry interval) = 12 in an ordinary keepalive-only episode, and 7 in the steady state;
    - more when an A11 reprobe brings retries forward (17 in Astra's reproduction);
    - no finite count while other qualifying evidence renews liveness (A9b).

    State and rate stay bounded in every case. Each retry is at least one retry interval after the previous attempt (Gate A F1).
- **R4 Evidence before verdict (ordering).** When a terminal verdict is due
  at instant `t`:
  1. The client first performs a bounded, non-blocking drain of datagrams
     already readable at `t` (delivered at or before `t`). Each goes through
     the normal authentication and dispatch path. The drain count is finite
     and independent of attacker input rate.
  2. Authenticated liveness evidence found in that drain (section 2.1 rows
     that advance liveness) is admitted with its effect evaluated as of `t`,
     before the terminal classification for `t`.
  3. Only then is the terminal classification for `t` taken, on the updated
     state. The MP5 property stays: no potentially blocking work between the
     final terminal classification and the mutation of liveness state.
  4. Evidence that becomes readable after `t` has no effect on the verdict
     at `t`.
  5. The drain can never extend a session beyond the terminal bound without
     authenticated evidence.
  - Acceptance: MP1-L4a (keepalive boundary), MP1-L4b (`peer_timeout` boundary, independent of any retry policy), MP1-L5.
  - **Deliberate conflict, to be resolved by MP1, not ignored.** R4 supersedes `BEHAVIORAL_CONTRACT.md` `1043-1051` ("a due deadline is resolved before poll-ready packets"). It also changes the equality outcome of the MP5 two-phase PATH_ACK admission tests (section 9, category b; e.g. `test_e2e_terminal_deadline_wins_when_ack_receive_reaches_equality`), which today let a deadline due at the admission instant win over an ACK readable at that instant. MP1 must re-derive those outcomes under R4 and state the new rule in the contract. This is the highest-risk semantic change in MP1 and belongs in the Gate A scope.
  - R4 concerns datagrams not yet read when the verdict falls due. It does not relax the MP5 rule that an already-received datagram whose own processing or maintenance work crosses a deadline gains no effect (`test_e2e_final_admission_exact_equality_rejects_ack_effects`, `test_e2e_final_maintenance_*_peer_timeout`), unless MP1 explicitly re-derives and documents that too.
  - **MP1 (conflict resolved; Gate A focus).** Every datagram is credited as of the monotonic instant it is read. The loop receives a readable datagram before it services the deadlines due at that instant. Before a terminal liveness verdict it drains at most `EVIDENCE_DRAIN_MAX_DATAGRAMS = 16` already-readable datagrams (`select` with a zero timeout), then re-samples the clock and classifies again. MP1 explicitly re-derived the MP5 rule: an already-received ACK keeps its effect even when its own processing, logging or later maintenance crosses a deadline. The ACK counts from its read instant, so later work can neither cancel nor extend it (MP1-L4d and `test_e2e_ack_credit_is_anchored_at_its_receipt_not_at_later_work`). The two-phase final admission is removed. Its surviving property -- a supported planned refresh never masks a simultaneously due liveness verdict -- is kept (`test_e2e_supported_planned_refresh_never_masks_a_simultaneous_liveness_verdict`). The contract's section 11 paragraphs were rewritten, not appended to. Tests: MP1-L4a, L4b, L4d, L5, the section 9.1 category-C tests, and the stall and flood unit tests.
- **R5 Failure accounting agrees with evidence.** A terminal verdict must not
  contradict authenticated evidence the client has already accepted
  (section 2.1): refresh REPLY/ACK and matched PATH_ACK included. No
  unauthenticated datagram, and no client-to-server NMEA delivery, may ever
  become evidence.
  - Acceptance: MP1-L7, MP1-L8.
  - Invariants: existing D10 tests (matrix).
  - **MP1:** the verdict is always last evidence + `peer_timeout`, where the evidence includes refresh REPLY/ACK and matched PATH_ACK (credited at read time). No evidence type was added; an ICMP-derived receive error is explicitly not evidence (A11b). Strong variants added: MP1-L7b (refresh-only evidence carries the session, A9b) and MP1-L8b (a PATH_ACK that cannot clear the ping still moves the bound, A10b).
- **R6 Long outages still terminate.** No immortal sessions. A session with
  no authenticated evidence ends no later than `peer_timeout` after its last
  evidence. Re-establishment attempts stay bounded by `reconnect_delay`
  (at most one immediate attempt per terminal verdict). A fresh session
  follows within one handshake timeout plus one `reconnect_delay` of the path
  returning.
  - Guards (pass today, must keep passing): C1, C2, C3, C3b, C5, and the retry-cadence sweep.
  - **MP1:** all guards pass unchanged. A transient local network error stays inside the same bound (A11, C1): the failed sentence is dropped and input pauses for one retry interval, and a keepalive probe runs before input resumes. Other socket errors stay terminal.
- **R7 No wire change by default.** MP1 is client-only (`nmea_sproxy`). No
  wire format, message schema or protocol version change is needed (L10). If
  MP1 finds one unavoidable, it stops and reports before changing anything.
  - **MP1:** none was needed. The production diff touches only `nmea_sproxy/nmea_sproxy.py`. There is no new message type, version, AAD, crypto, transcript, server, migration or configuration-schema change. `_ClientPathMigration`, `_ClientEpochRefresh`, `_ClientObservedEndpoint` and `perform_handshake` are unchanged.

Regression boundary for MP1: `_ClientPathMigration`, `_ClientEpochRefresh`,
`_ClientObservedEndpoint`, `perform_handshake` and all server code stay
unchanged unless a failing acceptance test proves otherwise.

### 7.2 MP2 — bounded path-migration recovery (contract)

- Bounded PATH_CHALLENGE retransmission for an existing candidate incarnation (same token and generation), with a per-incarnation cap.
- No unbounded candidate history: still one candidate and one retired record per session.
- No reply or amplification toward unknown or unproved traffic beyond the capped challenges. Replays are still refused before any candidate is created.
- The candidate TTL stays finite. A retry never extends a candidate's authority or TTL.
- No epoch change, no session identity change, no replay-ledger reset, no wire change by default. The current client already answers duplicate challenges (`1292-1370`).
- Migration and liveness retry never race into contradictory terminal decisions.
- Pre-MP2 pins to replace: `test_pre_mp2_one_path_challenge_per_candidate_incarnation`, `test_pre_mp2_lost_challenge_or_response_waits_for_candidate_expiry`.
- Guards to keep: `test_b_tuple_change_keeps_one_logical_session`, `test_b1_…`, `test_b8_…`, `test_b11_…`.

### 7.3 Open decisions (explicitly NOT decided by MP0) and MP1's decisions

| Topic | Evidence | Who decides | MP1 decision |
|---|---|---|---|
| Should a transient local send error (`ENETUNREACH`, A11) be non-terminal for a bounded time, and should the failed sentence be retried? | L7; A11 pin | MP1, documented | Non-terminal within the liveness bound for the errnos listed in contract section 11. The failed sentence is dropped, not retried (no NMEA replay or buffering). Input pauses for one retry interval (backpressure into the adapter's own bounded queue), and a keepalive probe runs before input resumes. A11: same session, 1 line lost (as pre-MP1). |
| Quantitative recovery bound (retry cadence, count, relation to `peer_timeout`); hence the expected outcomes of A4b (37 s blackhole) and the A7 outage length | R2/R3 | MP1; then MP1 converts A4b to a test | Retransmission at the keepalive deadline and then every `min(5 s, keepalive_interval)`; terminal at last evidence + `peer_timeout`. A4b now recovers in session (pinned). The A7 forward-data outage fell from 70.2 s to 40.2 s. |
| Server-restart detection time (C5): staged retries delay the terminal verdict for a genuinely dead session, a trade-off against today's 30 s single shot. The guard caps it at `peer_timeout` + `reconnect_delay` after the last evidence. | Fable F4; C5 guard | MP1, documented | Detection at last evidence + `peer_timeout` (1120.3 vs 1090.2 pre-MP1), followed by an immediate re-handshake, inside the guard. Cost: lines forwarded into a dead path until then are lost (C5 8 vs 5; C3/C3b 7 vs 4). Forwarding cannot pause while liveness is only suspect (A8 invariant, MP1-L6). An operator can shorten the window by lowering `peer_timeout`. |
| Whether delivered client-to-server NMEA should ever count as liveness evidence (A8) | L9 | not MP1 unless the owner decides | Not decided; unchanged (NMEA is not evidence). |
| Strong isolation variants: refresh-only evidence with every PONG lost (L7-strong); PATH_ACK-only evidence (L8-strong) | R5 | MP1 adds once its constants exist | Added: MP1-L7b (A9b) and MP1-L8b (A10b). |
| Candidate-path PONG (Fable M3) | T09d | MP3, only with field data | Not MP1 (A10 recovers through the retransmission from the committed path). |

## 8. Must-not-regress security invariants

No MP1-MP4 change may weaken any of these. The tests that enforce each row
are listed in `SCENARIO_MATRIX.md` (D1-D10) and were not cloned.

1. Transcript binding: version, locator, both ECDHE keys, both randoms and both signatures are bound into the transcript and HKDF salt.
2. Per-epoch replay ownership; migration never resets a ledger (D8).
3. Path-authority gating: unproved and retired paths carry inbound data only; commits require exact token, generation, address, live candidate, current epoch and a fresh nonce.
4. No reply and no amplification for an unknown locator (D1); anti-probing silence stays.
5. Replay rejection (D2), forgery rejection (D5), wrong-station rejection (D3), wrong-selector/epoch rejection (D4).
6. Cross-listener isolation (D6); old-session ciphertext rejection (D7, C4).
7. Exactly one epoch lookup and one AEAD attempt per datagram.
8. AEAD nonces are never reused (D9, including any future retry).
9. Unauthenticated traffic never becomes liveness evidence (D10).
10. Diagnostics (`observed_endpoint`, `path_gen`, `age`, event lines) stay observational only.

MP1 re-verified rows 8 and 9 with its own tests: the D9 sweep over every
scenario, MP1-L12 for retransmissions, the D10 tests and A11b for ICMP-derived
errors. Row 10 covers MP1's new log lines, which are observational only.

Known LOW residuals, carried forward and not repaired:
- An unauthenticated ClientHello costs one P-256 verification before any per-source budget (Fable F7; `aismixer_secure.py:4548-4600`).
- The ±30 s wall-clock handshake window can look like an outage on an unsynchronised Raspberry Pi (Fable F6; `:4554`).

## 9. MP1 impact inventory (existing tests that encode the single-shot policy)

MP1 must update these deliberately; each update is part of the Gate A delta
(section 11). Categories:
- **(a)** pins the single-shot policy itself: change or invert it;
- **(b)** uses proactive recovery as the terminal boundary under test for the MP5 two-phase PATH_ACK admission. Re-parameterise it to MP1's terminal boundary and keep the choreography assertion;
- **(c)** uses proactive rekey only as a scripted teardown. Only the expected end reason or time changes;
- **(d)** checks `main()`'s retry mapping. Unaffected unless MP1 changes the reason's retry class.

| File | Tests | Cat. |
|---|---|---|
| `tests/test_secure_udp_helpers.py` | `test_proxy_outstanding_ping_is_not_overwritten_and_proactively_rekeys`, `test_proxy_exact_keepalive_deadline_rekeys_before_ready_matching_pong` (`recv_calls == 0`), `test_proxy_forward_loop_ignores_forged_no_session_until_proactive_rekey` (one ping, end at 60) | a |
| `tests/test_secure_udp_helpers.py` | `test_proxy_deadline_action_has_deterministic_exact_boundary_priority` | a (pure classification) |
| `tests/test_secure_udp_helpers.py` | `test_proxy_planned_or_proactive_rekey_does_not_wait_reconnect_delay` | d |
| `tests/test_udpsec_security_validation.py` | `test_real_client_proactively_recovers_after_server_restart`, `test_real_nonce_exhaustion_recovers_with_fresh_replay_epoch` (both assert `proactive_rekey` within `peer_timeout` on real sockets) | a |
| `tests/test_udpsec_client_path_migration.py` | `test_e2e_superseded_ack_after_response_send_failure_cannot_delay_recovery`, `test_e2e_ack_crossing_keepalive_deadline_cannot_erase_new_ping`, `test_e2e_ack_cannot_clear_ping_after_captured_ping_was_resolved`, `test_e2e_terminal_deadline_wins_when_ack_receive_reaches_equality` (all three parametrizations: R4 changes the equality rule), `test_e2e_refresh_retransmit_crossing_proactive_recovery_deadline_wins`, `test_e2e_ack_logging_crossing_proactive_recovery_deadline_wins`, `test_e2e_control_ack_still_clears_ping_and_reanchors_liveness`, `test_e2e_final_maintenance_refresh_start_crosses_proactive_recovery`, `test_e2e_simultaneous_planned_refresh_and_proactive_recovery_at_final_admission`, `test_e2e_final_maintenance_ping_send_without_terminal_crossing`, `test_e2e_final_admission_no_maintenance_due_control` | b |
| `tests/test_udpsec_mobile_path_e2e.py` | `test_01_…`, `test_03_…`, `test_10_ack_with_captured_none_cannot_clear_maintenance_ping` | c (test_10 also H2 semantics: keep) |
| `tests/test_udpsec_field_diagnostics.py` | `test_e2e_production_path_diagnostics_survive_real_migration`, `test_f3_forward_loop_real_migration_then_stale_unversioned_pong`, `test_pg6_real_migration_heartbeats_show_each_endpoint_with_its_generation` | c |
| `tests/test_nmea_sproxy_output_adapters.py` | `test_udpsec_main_proactive_rekey_is_immediate_then_failure_backs_off` (real `main()` with a fake `forward_loop`) | d |
| this directory | every `test_pre_mp1_*` pin, and every MP1 xfail marker (strict: an XPASS fails the run) | a |

`BEHAVIORAL_CONTRACT.md` paragraphs that MP1 must rewrite, not append to:
- section 11 lines `1029-1041` (single-shot rule and defaults arithmetic);
- `1043-1051` (deadline-before-readable ordering, superseded by R4);
- `1067-1083` (proactive recovery wording);
- `1383-1389` (refresh evidence vs. the outstanding ping, per R5);
- the "terminal/proactive recovery" phrases in the client path-migration subsection (`1714-1828`).

MP1 rewrote each of them in place. It also added the transient-network-error
paragraph, updated the refresh subsection's proactive-rekey sentence and the
11.4 display-floor wording, and replaced 11.5.

### 9.1 MP1 disposition of the existing tests

Categories (MP1 instruction):
- **A**: the normative rule changed and the test now pins the MP1 rule;
- **B**: an invariant the test guards is kept, and only a label or timing around it changed;
- **C**: R4 timing -- evidence before verdict changed an equality or crossing outcome;
- **D**: unrelated to the rule, or needed no change.

Every assertion change is listed. Tests not listed here are unchanged.

| File :: test (old name → new name) | Cat. | What changed and why |
|---|---|---|
| `test_secure_udp_helpers.py` :: `test_proxy_deadline_action_has_deterministic_exact_boundary_priority` | A | Keepalive deadline with a ping outstanding → `RETRY_PING` (was `PROACTIVE_REKEY`); the `peer_timeout` bound with a probe outstanding → `PROACTIVE_REKEY` (was `PEER_TIMEOUT`); retry-instant boundaries added. The priority order is unchanged. |
| :: `test_proxy_outstanding_ping_is_not_overwritten_and_proactively_rekeys` → `…_retransmitted_not_overwritten_until_peer_timeout` | A | Was one ping and a rekey at 60. Now seq 1 at 30, retransmitted at 60, 65, …, 85 with distinct nonces, `PROACTIVE_REKEY` at 90. Never overwritten (kept). |
| :: `test_proxy_exact_keepalive_deadline_rekeys_before_ready_matching_pong` → `test_proxy_matching_pong_ready_at_the_keepalive_deadline_is_credited_first` | C | Was `recv_calls == 0` and a rekey at 60. Now the readable PONG is read and credited first (`recv_calls == 1`); the session goes on to `PROACTIVE_REKEY` at 150. |
| :: `test_proxy_duplicate_pong_after_acceptance_does_not_refresh_liveness` | B | Invariant kept: the duplicate PONG gives no credit, and the end is still 25.5. Only the reason label changed: `PEER_TIMEOUT` → `PROACTIVE_REKEY`, because ping #2 is outstanding at the bound. |
| :: `test_proxy_forward_loop_ignores_forged_no_session_until_proactive_rekey` → `…_until_liveness_bound` | A | Invariant kept: forged plaintext is never evidence. The end moved from 60 to 90 and sends from 1 to 7 (the retransmissions). |
| `test_udpsec_security_validation.py` :: `test_real_client_proactively_recovers_after_server_restart`, `test_real_nonce_exhaustion_recovers_with_fresh_replay_epoch` | A | Real sockets. Recovery now comes at the `peer_timeout` bound: `peer_timeout <= elapsed < 5 s` (was `< peer_timeout`). `PROACTIVE_REKEY` and the immediate retry are kept. |
| `test_udpsec_mobile_path_e2e.py` :: `test_08_lost_path_ack` | B | Invariant kept: the lost ACK grants no extension, and the end is still 1008.0. Reason label → `PROACTIVE_REKEY` (ping #1 outstanding). |
| :: `test_10_ack_with_captured_none_cannot_clear_maintenance_ping` | A/B | H2 kept: the captured-None ACK never clears ping #1, and no second logical ping is started. It is now retransmitted, so the sequences are all 1 and there is more than one. |
| `test_udpsec_client_path_migration.py` :: `test_e2e_superseded_ack_after_response_send_failure_cannot_delay_recovery` | A | Invariant kept: the superseded ACK gives no credit. With `peer_timeout: 20` the session ends at start + 20 (not ACK + 20), after retransmitting ping #1. |
| :: `test_e2e_ack_crossing_keepalive_deadline_cannot_erase_new_ping` | C | The ACK is credited at 5.01 before the due keepalive; the ping sent after it is never resolved by it, so it is retransmitted until ACK + 20 = 25.01. |
| :: `test_e2e_ack_cannot_clear_ping_after_captured_ping_was_resolved` | A | Invariant kept: the ACK's captured ping #1 is already resolved, so it cannot clear ping #2. Ping #2 is retransmitted until ACK + 20 = 31.0. |
| :: `test_e2e_terminal_deadline_wins_when_ack_receive_reaches_equality` (3 cases) → `test_e2e_ack_receive_reaching_a_deadline_is_credited_before_the_verdict` | C | The MP0 "deliberate conflict". The ACK received exactly at a deadline is credited first: the `peer_timeout` case ends at ACK + 5 = 10.0 (was 5.0), and the keepalive case ends at 30.0. The planned refresh without refresh support still ends at 5.0: evidence never postpones it. |
| :: F1-F4 `test_e2e_refresh_retransmit_crossing_proactive_recovery_deadline_wins`, `…_crossing_peer_timeout_wins`, `test_e2e_ack_logging_crossing_proactive_recovery_deadline_wins`, `test_e2e_final_admission_exact_equality_rejects_ack_effects` → `test_e2e_ack_credit_survives_refresh_retransmit_crossing_the_ping_deadline`, `…_refresh_retransmit_crossing_peer_timeout`, `…_its_logging_crossing_the_ping_deadline`, `…_work_landing_exactly_on_peer_timeout` | C | Same crossing timelines; the ACK's credit now survives work that crosses a deadline after its receipt. The ends move to ACK read + `peer_timeout`, and pings follow their retransmission schedule. |
| :: F5 `test_e2e_control_ack_still_clears_ping_and_reanchors_liveness` | B | Control kept: the ACK clears its captured ping and reanchors liveness. Ping #2 is now retransmitted, so the end is 18.5. |
| :: G1-G4 `test_e2e_final_maintenance_ping_send_crosses_peer_timeout`, `…_lands_exactly_on_peer_timeout`, `test_e2e_final_maintenance_refresh_start_crosses_peer_timeout`, `…_crosses_proactive_recovery` → `test_e2e_ack_credit_survives_maintenance_ping_send_crossing_peer_timeout`, `…_landing_on_peer_timeout`, `…_maintenance_refresh_start_crossing_peer_timeout`, `…_crossing_the_ping_deadline` | C | Maintenance after the ACK's receipt can no longer void the ACK's credit. The ends move to ACK read + `peer_timeout`. |
| :: G5 `test_e2e_simultaneous_planned_refresh_and_proactive_recovery_at_final_admission` → `test_e2e_supported_planned_refresh_never_masks_a_simultaneous_liveness_verdict` | B | The MP5 property, restated for MP1's only terminal liveness deadline: when the verdict and a supported refresh fall due together, the verdict wins and no REFRESH_INIT is sent. The script has no ACK now, because under R4 an ACK would be credited first. |
| :: Controls A-D `test_e2e_final_maintenance_ping_send_without_terminal_crossing`, `…_refresh_start_without_terminal_crossing`, `test_e2e_final_maintenance_reanchors_to_post_not_pre_maintenance_sample` → `test_e2e_ack_credit_is_anchored_at_its_receipt_not_at_later_work`, `test_e2e_final_admission_no_maintenance_due_control` | C (A for Control C) | The credit is anchored at the ACK's read instant (8.9), not at a post-maintenance sample (9.0/9.4): the ends are 19.9, 18.4 and 18.9. Control C: ping #2 is retransmitted at 18.0 and the end is 20.4 (was 18.0). |
| :: `test_e2e_migration_reanchors_peer_timeout_but_not_keepalive_or_refresh` | D | Harness correction only: its fake `select` consumed 0.25 s even for the zero-timeout drain poll. It now advances by `min(0.25, timeout)`. The assertion (end exactly 6.5 = ACK 0.5 + 6) is unchanged. |
| `test_proxy_planned_or_proactive_rekey_does_not_wait_reconnect_delay`, `test_udpsec_main_proactive_rekey_is_immediate_then_failure_backs_off`, e2e `test_01_…`, `test_03_…`, `tests/test_udpsec_field_diagnostics.py` (the three section 9 tests), `test_e2e_matched_ack_without_ping_clear_authority_still_reanchors_liveness` | D | Unchanged and passing. The retry mapping is unchanged, and the teardowns and the evidence rules they rely on still hold. |
| this directory: `test_pre_mp1_policy_pins.py` (deleted), `test_mp1_liveness_acceptance.py`, `test_baseline_consistency.py`, `harness/` | A | Each pre-MP1 pin is replaced or inverted by an exact MP1 pin in `test_mp1_policy_pins.py`; the pre-MP2 pins moved there unchanged. The 10 strict-xfail markers were removed with their assertions unchanged, and 6 acceptance tests were added. The consistency test now forbids xfail markers. Lab: a due-but-unread serial line is no longer a `select` wake event (needed once MP1 pauses input), plus the `receive_error_at` fault and scenarios A5e, A9b, A10b, A11b. |

## 10. Roadmap

| Phase | Scope | Constraints | Exit |
|---|---|---|---|
| **MP1 — Client Liveness Recovery** | Staged, bounded client recovery (R1-R7); make `peer_timeout` meaningful; evidence-before-verdict. | `nmea_sproxy` only; no wire change; no server change; O(1) state. | All 10 MP1 xfail markers removed and passing; pre-MP1 pins deleted or inverted; section 9 tests updated; all invariants still green; contract rewritten per section 9. Then **Gate A**. |
| **MP2 — Bounded Path-Migration Recovery** | Bounded server PATH_CHALLENGE retransmission (7.2). | Server only; no history-scaled state; no wire change by default. | Pre-MP2 pins replaced by bounded-retransmission and anti-amplification tests; B-guards green. |
| **MP3 — Liveness × Migration × Epoch Integration Closure** | Only the cross-mechanism races and interactions not already closed by MP1/MP2 (e.g. T09d class under MP2 timing, refresh × retry, shutdown while recovering). | No new architecture. | Closure tests. Then **Gate B**. |
| **MP4 — Mobile Recovery Closure** | Final field matrix (IPv6 and IPv4/CGNAT with the Fable §11 worksheet), operator docs, release and rollback notes. Includes Fable F8: `CHANGELOG.md` `[Unreleased]` (line 14) says migration works "without dropping the connection or requiring a new authenticated handshake". That is true for a validated tuple change and false for same-tuple keepalive loss before MP1; reword it for the release. | No new architecture. | Field evidence recorded; no deep audit unless substantial production code changed after Gate B. |

Status: the MP1 exit criteria were met, implementer-run (section 9.1; the
test results are in the MP1 report). Gate A then ran and returned NEEDS
CORRECTION BEFORE MP2 with three LOW findings, corrected in section 11.2.
Next is the **Astra corrective recheck** of F1-F3 only, then **MP2**.

One finding does not automatically create one new major prompt. Minor findings
are batched into the current corrective round of the phase that owns them.

## 11. Independent-audit (Astra) resource-control policy

We do not "dig the same vineyard several times".

1. **Carry-forward evidence.** Every future Astra gate starts from:
   - the exact baseline commit;
   - the exact feature or corrective delta (commit range);
   - the exact changed production files and functions;
   - the exact changed tests (section 9 lists the expected ones for MP1);
   - `EVIDENCE_LEDGER.md`;
   - the list of independently proven invariants that were NOT touched.

   Unchanged, independently proven areas are carried forward, not re-audited.
2. **Gate placement.**
   - **Gate A**, after MP1 is complete and implementer tests are green. It audits only:
     - the liveness-state delta;
     - that the MP1 scenarios now pass;
     - the directly adjacent replay, session and security invariants.
   - **Gate B**, after MP2/MP3. It audits only:
     - the liveness × migration × epoch composition;
     - bounded retry;
     - authority, session, replay and identity non-regression.
   - **MP0 gets no Astra gate.** MP4 gets no deep audit unless substantial production code changed after Gate B.
3. **Never request "Audit UDPSECv2".** The request is always "audit THIS EXACT DELTA against THIS EXACT carried-forward baseline". The following are not re-run from scratch unless relevant production code changed or a concrete contradiction requires it:
   - the handshake transcript review;
   - the whole crypto review;
   - the whole replay subsystem;
   - the unrelated diagnostics worker;
   - the unrelated SIGTERM lifecycle;
   - complete multi-OS full suites.
4. **One agent, bounded work.**
   - One audit agent; no parallel sub-agents unless explicitly approved.
   - Targeted independent reproductions first.
   - Implementer full-suite results may remain implementer-reported.
   - Astra reruns only the critical acceptance slices (for Gate A: this directory plus the section 9 files), unless a concrete failure justifies more.
5. **Mandatory final verdict**, even when the budget runs out: `BLOCKER`, `READY FOR NEXT GATE`, or `UNVERIFIED — <exact remaining items>`. Never consume the budget without leaving a report.
6. **Corrective continuation.** If Astra finds a defect, the implementer fixes the bounded defect or batch. The audit resumes in the SAME context where possible and inspects only the corrective delta and the affected acceptance tests. The gate is not restarted.
7. **Minor finding ≠ new major prompt.** Minor findings are batched into the current corrective round.

### 11.1 Gate A handoff (MP1)

Audit THIS EXACT DELTA against THIS EXACT carried-forward baseline:
- **Baseline:** `71a47894264105531b0d873b09d0520ba90e15eb` (MP0; its production code is `de513674`'s).
- **Delta:** the MP1 working tree, or the commit that records it.
- **Production file changed:** `nmea_sproxy/nmea_sproxy.py` only.
  - New constants: `SESSION_ACTION_RETRY_PING`, `KEEPALIVE_RETRY_INTERVAL_SECONDS`, `EVIDENCE_DRAIN_MAX_DATAGRAMS`, `_TRANSIENT_NETWORK_ERRNOS`.
  - New helpers: `liveness_verdict`, `keepalive_retry_interval`, `keepalive_action`, `is_transient_network_error`.
  - Changed helpers: `session_deadline_action`, `session_poll_timeout`, `terminal_deadline_reason` (its `last_ping_at` parameter was removed), `nonterminal_due_action`, `forward_input_payload`.
  - `forward_loop`: the liveness state, `receive_datagram`, `drain_available_datagrams`, `service_deadlines`, the transient-error pause and probe, and the main loop order. The MP5 `apply_due_deadline` / two-phase PATH_ACK gate were removed.
- **Unchanged and carried forward:** the server, the wire and version, AAD, crypto, the handshake transcript, `perform_handshake`, `_ClientPathMigration`, `_ClientEpochRefresh`, `_ClientObservedEndpoint`, `main()`, the config schema and defaults. The evidence ledger (section 7) lists what carries forward.
- **Focus, in priority order:**
  1. R4: the evidence-first order, the drain bound and its re-classification, and "late evidence stays late".
  2. The re-derived MP5 PATH_ACK rule: credit anchored at the read instant; ping-clear authority only for the captured sequence.
  3. Nonce freshness of every retransmission.
  4. No unauthenticated input as evidence, including ICMP-derived errors.
  5. Boundedness: at most one keepalive transmission per retry interval; O(1) state; drain ≤ 16; a pause of one retry interval.
  6. Errno classification: which errors stay terminal.
  7. The trade-offs C3/C3b/C5 and the rate-bounded but not count-bounded retransmission while other evidence renews liveness (A9b).
- **Critical slices to rerun:** `python -m pytest tests/udpsec_recovery`, plus the section 9.1 files: `tests/test_secure_udp_helpers.py`, `tests/test_udpsec_client_path_migration.py`, `tests/test_udpsec_mobile_path_e2e.py`, `tests/test_udpsec_security_validation.py`, `tests/test_udpsec_field_diagnostics.py`, `tests/test_nmea_sproxy_output_adapters.py`.
- **Not to re-audit** unless the delta touches it: the transcript, the crypto, the replay subsystem, server migration, the diagnostics worker, SIGTERM lifecycle.

### 11.2 Gate A result and the F1-F3 corrective iteration (MP1)

**Astra Gate A** (independent; report outside the repository) audited the MP1 worktree above.
- **Verdict:** NEEDS CORRECTION BEFORE MP2. No BLOCKER or HIGH security defect.
- **Delta scope confirmed:** production `nmea_sproxy/nmea_sproxy.py` only; no server, wire/version, AAD, crypto, transcript or configuration change.
- **Accepted:** the core MP1 liveness design, R4, PATH_ACK authority, epoch-refresh composition and terminal recovery.
- **Accepted as designed:** A9b.
- **Accepted as an explicit bounded trade-off:** the 16-datagram drain. A valid PONG queued as datagram #17 behind 16 junk datagrams can miss the drain, and the terminal recovery then follows; the junk gains no authority.

Three LOW findings, each independently reproduced by Astra, and their corrections:

| ID | Finding (Astra) | Correction (this iteration) | Regression |
|---|---|---|---|
| F1 | Retry spacing: with the first "liveness suspect" line delayed 4 s, keepalive transmissions left at about 64 s and 65 s. The next retry was scheduled from `now`, sampled before the slow log line, so the documented minimum spacing was false (no busy loop, no liveness extension). | `retransmit_ping` schedules the next retry, and `last_ping_at`, from a fresh monotonic sample taken after the transmission attempt, never from the pre-log/pre-send `now`. The `now` parameter was removed. **Final micro-correction:** the corrective recheck found `start_ping` still anchored at its pre-attempt `now`. With `keepalive_interval` 2 s and a 1.5 s slow initial encryption/send, the ping due at 2.0 left at 3.5 and the first retry followed at 4.0, only 0.5 s later. `start_ping` now anchors `last_ping_at` and `ping_retry_at` the same way, at a fresh sample after the initial attempt, with the first-retry delay still `keepalive_interval`; its `now` parameter was removed. | `test_mp1_liveness_units.py::test_slow_suspect_log_cannot_compress_the_retry_spacing`: 30, 64, 69, 74, …; verdict still at 90. It fails against the pre-correction code with 64 then 65. `::test_slow_initial_ping_send_cannot_compress_first_retry_spacing`: 3.5, 5.5, 7.5, …, 19.5; verdict still at 20. It fails against the pre-micro-correction code with 3.5 then 4.0. With the micro-correction, G1/G2 in `test_udpsec_client_path_migration.py` (a mocked initial ping send crossing, or landing on, the original `peer_timeout`) no longer retransmit at 18.0: the first retry would fall at 19.5 or 19.0, after the unchanged 18.9 verdict. |
| F2 | Accounting: "never more than 12 retransmissions" is false. Last PONG and ping #2 at 60, transient NMEA send failure at 60.1: ping #2 is retransmitted at 65.1 … 145.1 (17 times), and the verdict is correctly at 150. | The contract, this file, the ledger and the code comment now separate the three bounds (section 7.1 R3): the state bound; the rate bound; and an episode-count bound that is finite for an ordinary keepalive-only episode, larger with an A11 reprobe, and absent under A9b. No cap was added. | `test_mp1_liveness_units.py::test_a11_reprobe_can_exceed_the_ordinary_retransmission_count` pins Astra's schedule (17 > 12), cadence ≥ one retry interval, the verdict at PONG + `peer_timeout`, and no resend of the failed line. |
| F3 | The operator guide said "every 5 seconds", a 5-second pause, and "any authenticated answer". | `nmea_sproxy/README.md` now says the retry interval is 5 s or `keepalive_interval`, whichever is shorter; the input pause lasts one retry interval with a keepalive reprobe before input resumes; and a re-handshake follows only the absence of *qualifying authenticated evidence*, which it lists. | documentation only |

No other production hunk changed after Gate A. The corrective delta is limited to:
- `retransmit_ping`, its two call sites and the constant's comment;
- two unit tests and one pin docstring;
- the documentation and accounting texts named above.

**Astra corrective recheck scope** (it was then run: F2, F3 and the `retransmit_ping` part of F1 accepted; the `start_ping` part corrected by the final micro-correction above, whose micro-recheck covers only the `start_ping` anchor and its regression): F1, F2 and F3 only, plus the sanity statement that no other production hunk changed.
- F1: the retry anchor after slow log or work; no compressed interval; no busy loop; `peer_timeout` unchanged.
- F2: the ordinary, A11 and A9b accounting; no unconditional 12-retry ceiling left.
- F3: the README wording matches the implementation.
- Tests to run: the two regressions above, `python -m pytest tests/udpsec_recovery`, and `git diff --check`.

## 12. NOT VERIFIED boundaries, harness provenance and fidelity limits

Not verified by MP0 (and not claimed):
- Which packet was lost, or for how long, in any field event.
- OpenWrt or Raspberry Pi behaviour of the MP0 harness. On Linux, MP0 ran only its own suite and only under WSL2, with cryptography 41.0.7 (below the declared `>=42.0` floor).
- The real `main()` config/key/socket/SIGTERM path, `SecureState` maintenance, and the diagnostics output worker.
- A real IPv4 CGNAT mapping lifetime.
- Hello-flood CPU cost, nonce exhaustion under load, multi-listener collisions beyond the existing tests.
- Anything about MP1/MP2 implementations (the xfails prove only that current code fails them; a scratch satisfiability probe is in the ledger).

Not verified by MP1 (and not claimed):
- Any independent review of the MP1 delta: every MP1 result is implementer-run until Gate A.
- MP1 on real networks, a Raspberry Pi or OpenWrt; no field run of the MP1 client yet (MP4).
- The pause against the real serial adapter's 256-line drop-oldest queue. The lab's serial input has no queue. A feed above about 50 lines/s can overflow the queue during one 5 s pause, which is bounded and the same exposure as a pre-MP1 reconnect.
- Real OS behaviour of every errno in `_TRANSIENT_NETWORK_ERRNOS`. The classification is unit-tested with the platform's `errno` constants. On this Windows CPython those are the `WSAE*` codes (for example `ENETUNREACH == 10051`). The lab raises `ENETUNREACH` and `ECONNRESET` only.

Harness provenance: `harness/lab.py` and `harness/scenarios.py` are
repository ports of Fable's `udpsec_lab.py` and `scenarios.py`. Scenario
fault injections, timings and IDs are kept.

Changes versus Fable's harness:
- The Fable-local `pytest.py` stub is dropped; real pytest `monkeypatch` is used.
- The hard-coded `REPO` path is dropped; the repository test import convention is used.
- Global `builtins.print` patching is replaced by per-module `print` capture.
- Exact float timestamps are kept (Fable rounded them), so boundary scenarios are exact; `deliver_s2c_at` places a datagram exactly on a client deadline.
- `HarnessError`/`_HarnessAbort` make harness faults visible. A Fable-harness poll-limit or hook error raised inside `select()` would have been swallowed by `forward_loop` as `socket_error`.
- Each consumed server datagram records whether the handshake or `forward_loop` read it.
- Local send errors, identity snapshots and injection were added, and so were scenarios A5d, A11, C1, C2 and C3.
- MP1 added ICMP-derived receive errors (`receive_error_at`) and scenarios A5e, A9b, A10b and A11b. A local-input line that is due but unread is no longer a fake-`select` wake event: a real serial adapter has no descriptor, so the loop sleeps its poll interval.
- A restarted server gets a new endpoint token.

Not copied:
- Fable's 26 logs, `_results.json` and `show.py`. Set `UDPSEC_RECOVERY_TRACE_DIR` to regenerate traces.
- T13 negatives a-i (existing project tests cover them, see D1-D6); T13-j became C4.

Fidelity limits:
- `main()` is replicated; a drift guard checks its call order, and `test_udpsec_main_proactive_rekey_is_immediate_then_failure_backs_off` runs the real `main()`.
- One datagram per `asyncio.run()`; no maintenance task.
- Constant 50 ms one-way delay; zero server processing time.
- Serial-like input without the 256-line drop-oldest queue.
- `time.monotonic`/`time.time`/`select.select` are patched process-wide during a test.

## 13. Fable scenario → phase mapping

| Fable | MP0 ID | Class | Phase that changes the outcome | MP0 artefact | MP1 artefact |
|---|---|---|---|---|---|
| T01 | A1 | A | none (guard) | invariant | invariant (unchanged) |
| T02, T03, T04 | A2, A3, A4 | A | MP1 | pin + xfail L1, L2, L3 | acceptance L1, L2, L3 + MP1 pin |
| T04b | A4b | A/C | MP1 (bound-dependent) | pin; PLANNED | MP1 pin (recovers in session) |
| T05a | A5a | A | none (guard) | invariant | invariant (unchanged) |
| T05b, T05c | A5b, A5c | A | MP1 | pin + xfail L4a, L4c | acceptance L4a, L4c + MP1 pins |
| — | A5d | A | MP1 (R4) | pin + xfail L4b | acceptance L4b + MP1 pin |
| — | A5e | A/C | MP1 (R4 item 4) | — | acceptance L4d + MP1 pin |
| T06a | A7 | A | MP1 | pin + xfail L6 | acceptance L6, L12 + MP1 pin |
| T06b | C3b | C | none (guard) | invariant | invariant + MP1 pin (trade-off) |
| T07 | A6 | A | MP1 | pin + xfail L5 | acceptance L5 + MP1 pin |
| T08 | B1 | B | none (guard) | invariant | invariant (unchanged) |
| T09a, T09a7, T09b | B2, B2b, B3 | B | MP2 (latency) | invariant + pre-MP2 pin | unchanged |
| T09c | B4 | B | none (guard) | invariant | unchanged |
| T09d | A10 / B5 | A/B | MP1 (outcome), MP3 (composition under MP2 timing) | pin + xfail L8 | acceptance L8 + MP1 pin; A10b: acceptance L8b |
| T10a, T10b | B10, B8 | B | none (guard); MP2 counts | invariant (+ pre-MP2 pin for T10a) | unchanged |
| T11a, T11b, T11c | B7, B6a, B6b | B | none (guard); MP3 composition | invariant | unchanged |
| T12 | B11 | B | none (guard) | invariant | unchanged |
| T13 a-j | D1-D7, C4 | D | none (guard) | existing tests + C4 invariant | unchanged |
| T14 | A9 | A | MP1 | pin + xfail L7 | acceptance L7 + MP1 pin; A9b: acceptance L7b |
| T16 | C5 | C | MP1 (detection-time trade-off) | pin + guard | guard + MP1 pin (trade-off) |
| — | A11, A11b | A/C | MP1 (7.3 decision) | pin (A11) | acceptance L11a, L11b + MP1 pins |
