# UDPSEC V2 Recovery Baseline and Acceptance Contract (MP0)

| | |
|---|---|
| Status | MP0 baseline. Documentation and tests only; no production runtime file changed. |
| Branch / baseline commit | `main` @ `de513674fabf2ee594d0f08f10ddbdf87267a352` (2026-09-25, "feat(udpsec): add authenticated field diagnostics for mobile path migration") |
| Written | 2026-09-29 |
| Serves the next instruction | "Implement MP1 against this exact recovery baseline." |
| Normative current behaviour | `BEHAVIORAL_CONTRACT.md` section 11 (this file adds no current guarantee; see its 11.5) |

Companion files in this directory:

| File | Role |
|---|---|
| `SCENARIO_MATRIX.md` | One row per scenario: class, network conditions, current result, future target, provenance. |
| `EVIDENCE_LEDGER.md` | Every important claim with its evidence class (code, project test, Fable, Astra, field, implementer-only, not verified). |
| `harness/lab.py`, `harness/scenarios.py`, `harness/expect.py` | Deterministic lab: repository port of the Fable harness. |
| `test_recovery_invariants.py` | Ordinary tests that must keep passing through MP1 and MP2. |
| `test_pre_mp1_policy_pins.py` | Exact pins of the current (pre-MP1/pre-MP2) policy. MP1/MP2 delete or invert them. |
| `test_mp1_liveness_acceptance.py` | MP1 acceptance tests, `xfail(strict=True, raises=AssertionError)`. |

Run: `python -m pytest tests/udpsec_recovery` (from the repository root, as for the whole suite).

---

## 1. Scope and provenance

MP0 turns evidence that already existed into one repository-local recovery
baseline. It defines what must survive before any production change. It does
not redesign UDPSEC V2, change keepalive, migration, timers, crypto, the
transcript, the wire format or the protocol version.

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

## 2. Current liveness behaviour (pre-MP1)

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

What current code accepts as peer-liveness evidence, and what it does with it.
MP1 failure accounting must not contradict this table (section 7.1, R5), and
must never add a row for unauthenticated input.

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

## 3. Current migration behaviour (pre-MP2)

| # | Fact | Where |
|---|---|---|
| M1 | An authenticated, replay-admitted `nmea` or `ping` from an off-path source under the CURRENT epoch opens or replaces the session's single candidate. | `_process_candidate_path_observation` `4424-4477`; `open_or_replace_candidate_path` `3552` |
| M2 | Exactly ONE PATH_CHALLENGE per candidate incarnation (`outcome == "installed"`). Duplicate traffic on a live candidate sends nothing. | `4459-4460`; contract 11.3 item 5 |
| M3 | `PATH_CANDIDATE_TTL_SECONDS = 10.0`, never extended. After expiry the next authenticated off-path packet opens a new incarnation (new `path_generation`, new challenge). | `aismixer_secure.py:127` |
| M4 | A matching PATH_RESPONSE from the candidate's exact address commits atomically. The PATH_ACK goes to the new active path. The old path is retired for `RETIRED_PATH_GRACE_SECONDS = 5.0` (late nmea/ping admitted, no reverse migration). | `commit_candidate_path` `3700-3869`; `:134` |
| M5 | A commit preserves the `LogicalSession` object, the `CryptoEpoch` object and generation, the per-epoch replay ledger object and the `assembly_namespace`. `path_generation` is per session; `active_path_generation` is the generation that last won a commit. | contract 11.3 items 1-4; T08 |
| M6 | Pings from an unproved path get no PONG (L10). A ping that overtakes a PATH_RESPONSE which captured no ping stays unresolved. The later PATH_ACK advances liveness but cannot clear it, so L3 fires (T09d). | `5085-5106`; contract 11.3 item 8 |
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

## 6. Reproduced current failures

Every row below is reproduced in-repo by the MP0 lab on the baseline commit
(real client, real server, fake clock). Times are fake-clock seconds; the
session starts at 1000.2 and ping#2 leaves at 1060.2.

| ID (Fable) | Fault | Pre-MP1 outcome, reproduced | Pinned by | MP1 acceptance |
|---|---|---|---|---|
| A2 (T02) | ping#2 lost | `proactive_rekey` at 1090.2; new session at 1090.4 (`epoch 0`, `path_gen 0`, same tuple); 19/19 NMEA delivered; server `sessions_replaced=1` | `test_pre_mp1_one_unresolved_keepalive_…[A2]` | MP1-L1 |
| A3 (T03) | pong#2 lost | identical; the server received the ping and never noticed | `…[A3]` | MP1-L2 |
| A4 (T04) | two-way blackhole 1058-1063 | rekey at 1090.2, 27 s after recovery; 1 NMEA lost inside the hole | `…[A4]` | MP1-L3 |
| A4b (T04b) | two-way blackhole 1058-1095 | rekey 1090.2, then ClientHello lost, failure at 1095.2, 5 s backoff, new session 1100.4 (+5.4 s after the path returned) | `test_pre_mp1_outage_past_the_deadline_…` | PLANNED (7.3) |
| A5b (T05b) | pong#2 readable exactly at its deadline | rekey; the PONG is consumed by the next handshake, never by `forward_loop` | `…[A5b]`, `test_pre_mp1_readable_matching_pong_…[A5b-2]` | MP1-L4a |
| A5c (T05c) | pong#2 10 ms after its deadline | rekey | `…[A5c]` | MP1-L4c |
| A5d (MP0) | pong#1 readable exactly at the `peer_timeout` boundary (`peer_timeout: 45`) | `peer_timeout` wins; the PONG is discarded; 5 s backoff | `test_pre_mp1_peer_timeout_verdict_…` | MP1-L4b |
| A6 (T07) | client stall 1060.26→1095 with pong#2 buffered | rekey on resume at 1095.0; the buffered PONG is discarded | `test_pre_mp1_client_stall_…` | MP1-L5 |
| A7 (T06a) | server→client lost 1055-1155, client→server healthy | rekey 1090.2; 7 failed handshakes; new session 1160.4; a 70.2 s forward-data outage while the forward path was healthy | `test_pre_mp1_reverse_path_loss_…` | MP1-L6 |
| A9 (T14) | pong#2 lost; refresh committed at 1065.4 | rekey 1090.2 despite the authenticated refresh evidence | `…[A9]` | MP1-L7 |
| A10/B5 (T09d) | A→B at 1059.9; ping#2 overtakes the PATH_RESPONSE | migration commits (1060.55), ACK accepted; rekey 1090.2 anyway; the new session starts at `path_gen 0` | `…[A10]` | MP1-L8 |
| A11 (MP0) | local `ENETUNREACH` 1059.9-1062.0 | `socket_error` at 1060.0; 5 s backoff; new session 1065.2; the failed sentence is lost | `test_pre_mp1_short_local_send_error_…` | OPEN (7.3) |
| C5 (T16) | server restart at 1045 | 5 NMEA silently dropped (unknown locator); rekey at 1090.2 | `test_pre_mp1_server_restart_…` | guard only (7.3) |

## 7. Future acceptance semantics

MP0 defines these constraints. It does not implement them, and it chooses no
state names, retry counts or timing constants.

### 7.1 MP1 — client liveness recovery (contract)

- **R1 Same logical ping, fresh nonce.** A retry may reuse the logical ping
  `seq`. Every transmission is a new AEAD encryption with a fresh 96-bit
  nonce: no nonce or ciphertext reuse. Retries are bounded, and state is O(1)
  per session (no history-scaled state).
  - Enforced by: the D9 sweep (all scenarios, no nonce reuse per direction/locator/epoch).
  - Wire compatibility is proven by L10: the current server answers a same-`seq` ping sent under a fresh nonce (and a lower `seq`), and refuses a byte-identical replay.
- **R2 Staged liveness.** One missed keepalive deadline must not by itself end
  the logical or cryptographic session. There must be a bounded suspect/retry
  phase, or equivalent, before any terminal verdict.
  - Acceptance: MP1-L1, L2, L3, L4c, L5, L6, L7, L8.
- **R3 `peer_timeout` becomes meaningful.** The configured `peer_timeout`
  must be a real upper fallback. It must not normally be made unreachable by
  one missed probe. MP1 documents its terminal bound in terms of
  `peer_timeout` and any new constants.
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
- **R5 Failure accounting agrees with evidence.** A terminal verdict must not
  contradict authenticated evidence the client has already accepted
  (section 2.1): refresh REPLY/ACK and matched PATH_ACK included. No
  unauthenticated datagram, and no client-to-server NMEA delivery, may ever
  become evidence.
  - Acceptance: MP1-L7, MP1-L8.
  - Invariants: existing D10 tests (matrix).
- **R6 Long outages still terminate.** No immortal sessions. A session with
  no authenticated evidence ends no later than `peer_timeout` after its last
  evidence. Re-establishment attempts stay bounded by `reconnect_delay`
  (at most one immediate attempt per terminal verdict). A fresh session
  follows within one handshake timeout plus one `reconnect_delay` of the path
  returning.
  - Guards (pass today, must keep passing): C1, C2, C3, C3b, C5, and the retry-cadence sweep.
- **R7 No wire change by default.** MP1 is client-only (`nmea_sproxy`). No
  wire format, message schema or protocol version change is needed (L10). If
  MP1 finds one unavoidable, it stops and reports before changing anything.

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

### 7.3 Open decisions (explicitly NOT decided by MP0)

| Topic | Evidence | Who decides |
|---|---|---|
| Should a transient local send error (`ENETUNREACH`, A11) be non-terminal for a bounded time, and should the failed sentence be retried? | L7; A11 pin | MP1, documented |
| Quantitative recovery bound (retry cadence, count, relation to `peer_timeout`); hence the expected outcomes of A4b (37 s blackhole) and the A7 outage length | R2/R3 | MP1; then MP1 converts A4b to a test |
| Server-restart detection time (C5): staged retries delay the terminal verdict for a genuinely dead session, a trade-off against today's 30 s single shot. The guard caps it at `peer_timeout` + `reconnect_delay` after the last evidence. | Fable F4; C5 guard | MP1, documented |
| Whether delivered client-to-server NMEA should ever count as liveness evidence (A8) | L9 | not MP1 unless the owner decides |
| Strong isolation variants: refresh-only evidence with every PONG lost (L7-strong); PATH_ACK-only evidence (L8-strong) | R5 | MP1 adds once its constants exist |
| Candidate-path PONG (Fable M3) | T09d | MP3, only with field data |

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

## 10. Roadmap

| Phase | Scope | Constraints | Exit |
|---|---|---|---|
| **MP1 — Client Liveness Recovery** | Staged, bounded client recovery (R1-R7); make `peer_timeout` meaningful; evidence-before-verdict. | `nmea_sproxy` only; no wire change; no server change; O(1) state. | All 10 MP1 xfail markers removed and passing; pre-MP1 pins deleted or inverted; section 9 tests updated; all invariants still green; contract rewritten per section 9. Then **Gate A**. |
| **MP2 — Bounded Path-Migration Recovery** | Bounded server PATH_CHALLENGE retransmission (7.2). | Server only; no history-scaled state; no wire change by default. | Pre-MP2 pins replaced by bounded-retransmission and anti-amplification tests; B-guards green. |
| **MP3 — Liveness × Migration × Epoch Integration Closure** | Only the cross-mechanism races and interactions not already closed by MP1/MP2 (e.g. T09d class under MP2 timing, refresh × retry, shutdown while recovering). | No new architecture. | Closure tests. Then **Gate B**. |
| **MP4 — Mobile Recovery Closure** | Final field matrix (IPv6 and IPv4/CGNAT with the Fable §11 worksheet), operator docs, release and rollback notes. Includes Fable F8: `CHANGELOG.md` `[Unreleased]` (line 14) says migration works "without dropping the connection or requiring a new authenticated handshake". That is true for a validated tuple change and false for same-tuple keepalive loss before MP1; reword it for the release. | No new architecture. | Field evidence recorded; no deep audit unless substantial production code changed after Gate B. |

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

## 12. NOT VERIFIED boundaries, harness provenance and fidelity limits

Not verified by MP0 (and not claimed):
- Which packet was lost, or for how long, in any field event.
- OpenWrt or Raspberry Pi behaviour of the MP0 harness. On Linux, MP0 ran only its own suite and only under WSL2, with cryptography 41.0.7 (below the declared `>=42.0` floor).
- The real `main()` config/key/socket/SIGTERM path, `SecureState` maintenance, and the diagnostics output worker.
- A real IPv4 CGNAT mapping lifetime.
- Hello-flood CPU cost, nonce exhaustion under load, multi-listener collisions beyond the existing tests.
- Anything about MP1/MP2 implementations (the xfails prove only that current code fails them; a scratch satisfiability probe is in the ledger).

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

| Fable | MP0 ID | Class | Phase that changes the outcome | MP0 artefact |
|---|---|---|---|---|
| T01 | A1 | A | none (guard) | invariant |
| T02, T03, T04 | A2, A3, A4 | A | MP1 | pin + xfail L1, L2, L3 |
| T04b | A4b | A/C | MP1 (bound-dependent) | pin; PLANNED |
| T05a | A5a | A | none (guard) | invariant |
| T05b, T05c | A5b, A5c | A | MP1 | pin + xfail L4a, L4c |
| — | A5d | A | MP1 (R4) | pin + xfail L4b |
| T06a | A7 | A | MP1 | pin + xfail L6 |
| T06b | C3b | C | none (guard) | invariant |
| T07 | A6 | A | MP1 | pin + xfail L5 |
| T08 | B1 | B | none (guard) | invariant |
| T09a, T09a7, T09b | B2, B2b, B3 | B | MP2 (latency) | invariant + pre-MP2 pin |
| T09c | B4 | B | none (guard) | invariant |
| T09d | A10 / B5 | A/B | MP1 (outcome), MP3 (composition under MP2 timing) | pin + xfail L8 |
| T10a, T10b | B10, B8 | B | none (guard); MP2 counts | invariant (+ pre-MP2 pin for T10a) |
| T11a, T11b, T11c | B7, B6a, B6b | B | none (guard); MP3 composition | invariant |
| T12 | B11 | B | none (guard) | invariant |
| T13 a-j | D1-D7, C4 | D | none (guard) | existing tests + C4 invariant |
| T14 | A9 | A | MP1 | pin + xfail L7 |
| T16 | C5 | C | MP1 (detection-time trade-off) | pin + guard |
