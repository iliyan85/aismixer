# UDPSEC V2 Recovery Evidence Ledger (MP0, updated by MP1, MP2 and MP3)

Purpose: let every future independent gate (Astra Gate A and Gate B,
baseline section 11) start from classified evidence instead of re-auditing
the protocol from scratch. Pre-MP1 baseline commit
`de513674fabf2ee594d0f08f10ddbdf87267a352`; MP0 committed as `71a47894`; MP1
is the client-only delta on top of `71a47894`. Section 1.1 is the pre-MP1
record and keeps its Fable/MP0 evidence. Section 1.3 is MP1's own claims.
Astra Gate A has since audited them (section 8): the audited design is
accepted, and the F1-F3 corrections are IMPLEMENTER-REPORTED ONLY until the
Astra corrective recheck. MP1 was then committed as `aa41b782`. MP2 is the
server-side delta on top of `aa41b782`: its claims (section 2.2) are
IMPLEMENTER-REPORTED ONLY; no independent gate has audited them. MP3 is the
integration closure in the same working tree: its claims (section 2.3),
including one lifecycle correction to `aismixer_secure.py`, are
IMPLEMENTER-REPORTED ONLY until Astra Gate B.

Evidence classes (as required by MP0):

| Class | Meaning |
|---|---|
| CURRENT CODE INSPECTION | Read in production source at the baseline commit during MP0 (line references given). |
| EXISTING PROJECT TEST | A pre-MP0 test in `tests/` asserts it; it passes at the baseline commit (MP0 run, section 3). |
| FABLE INDEPENDENT HARNESS | Verified by the independent Fable audit, 2026-09-28: code reading (Fable "[A]") and/or its harness run. Fable did NOT run the project suite. |
| PRIOR INDEPENDENT ASTRA EVIDENCE | Reported by the earlier independent Astra review; carried forward, not re-run. |
| ASTRA GATE A | Established by the independent Astra Gate A audit of the MP1 worktree (2026-09-30; report outside the repository), including its own reproductions. |
| FIELD OBSERVATION | Operator-reported road/field data. |
| IMPLEMENTER-REPORTED ONLY | Produced by the implementer (including every MP0 test run), not independently re-run. |
| NOT YET VERIFIED | Nobody has established it. |

A claim can hold several classes. "Re-verify when" names the change that
voids the carried-forward evidence.

## 1. Liveness behaviour

### 1.1 Pre-MP1 claims (line numbers are the baseline's)

| ID | Claim | Classes | Pointer | Re-verify when |
|---|---|---|---|---|
| E-L1 | One outstanding keepalive ping; never retransmitted or replaced. | CODE; PROJECT TEST; FABLE; IMPL (MP0 A1-A3) | `nmea_sproxy.py:1975-1995, 2634-2651, 2702-2718`; `test_proxy_outstanding_ping_is_not_overwritten_and_proactively_rekeys` | any change to the deadline helpers or `forward_loop` |
| E-L2 | An unresolved ping at the next keepalive deadline ends the session with `proactive_rekey` and triggers an immediate fresh handshake; with the defaults `peer_timeout` is pre-empted. | CODE; PROJECT TEST; FABLE (T02/T03/T04/T05b/T05c/T07/T14/T09d); IMPL (MP0 pins) | `1991-1994, 2652-2657, 2104-2110, 3258-3261`; `test_pre_mp1_one_unresolved_keepalive_…` | MP1 (expected to change) |
| E-L3 | A deadline due at the moment a matching PONG is readable wins; the PONG is later skipped by the next handshake. | CODE; PROJECT TEST; FABLE (T05b, T07); IMPL (MP0 A5b, A5d, A6: consumer recorded) | `2798-2806, 2878-2891`; `test_proxy_exact_keepalive_deadline_rekeys_before_ready_matching_pong`; `test_pre_mp1_readable_matching_pong_…` | MP1 (R4) |
| E-L4 | Liveness evidence inventory (baseline section 2.1): PONG clears and advances; first REFRESH_REPLY and REFRESH_ACK advance only; a matched PATH_ACK advances and clears only its captured `seq`; nothing unauthenticated counts. | CODE; PROJECT TEST (D10 list in the matrix); FABLE [A] | `1719-1761, 2823-2839, 2883-2965`; contract `1014-1027, 1383-1389` | MP1 (R5 may add resets, never new evidence types) |
| E-L5 | Refresh evidence does not prevent the rekey caused by one lost PONG. | CODE; FABLE (T14); IMPL (MP0 A9 pin) | contract `1383-1389` | MP1 |
| E-L6 | A PATH_ACK whose proof captured no ping cannot clear a ping that overtook the PATH_RESPONSE; the rekey follows. | CODE; PROJECT TEST (`test_10_ack_with_captured_none_cannot_clear_maintenance_ping`); FABLE (T09d); IMPL (MP0 A10) | `2961-2965`, `aismixer_secure.py:5085-5106` | MP1 |
| E-L7 | Forwarding never pauses while a ping is unresolved. | CODE; FABLE; IMPL (MP0 A8 invariant) | loop `2725-2982` | any `forward_loop` change |
| E-L8 | Any local send, select or recv exception inside `forward_loop` ends the session with `socket_error`, followed by `reconnect_delay`; a 2 s `ENETUNREACH` destroys the session and loses the failed sentence. | CODE; PROJECT TEST (`test_proxy_forward_loop_reports_socket_error`, `test_proxy_handshake_socket_error_returns_to_retry_loop`); IMPL (MP0 A11). **Not in the Fable report.** | `2645-2647, 2713-2715, 2760-2762, 2794-2796, 2807-2809` | MP1 (open decision) |
| E-L9 | One UDP socket per relation, reused for every handshake, so the same local port is kept across re-establishment. | CODE; FABLE [A]; IMPL (MP0 drift guard; same observed tuple in pins) | `3161-3166, 3229-3264` | `main()` changes |
| E-L10 | The server answers every admitted active-path ping with its `seq`, including a same-`seq` fresh-nonce retransmission and a lower `seq`; a byte-identical replay is refused. Client-only MP1 therefore needs no wire change. | CODE; PROJECT TEST (`test_secure_server_replies_with_encrypted_pong_for_valid_ping`, `test_secure_server_rejects_duplicate_data_nonce_after_first_valid_packet`); FABLE [A] (M3 discussion); IMPL (MP0 L10) | `aismixer_secure.py:4830-4837, 5026-5037, 5107-5131` | server ping branch changes |
| E-L11 | An unknown locator (e.g. after a server restart) is a silent drop; the client notices only at its keepalive deadline. | CODE; PROJECT TEST (`test_unknown_locator_never_reaches_decrypt_regardless_of_correct_path`, `test_real_client_proactively_recovers_after_server_restart`); FABLE (T16); IMPL (MP0 C5) | `aismixer_secure.py:4772-4783` | server lookup changes |

### 1.2 Status of the pre-MP1 claims after MP1

The Fable and MP0 evidence in 1.1 remains valid evidence about the pre-MP1
client. It is not evidence about MP1.

| ID | Status after MP1 |
|---|---|
| E-L1 | SUPERSEDED by E-P1: still one outstanding ping, never replaced, but now retransmitted. |
| E-L2 | SUPERSEDED by E-P2: the unresolved ping no longer ends the session at its keepalive deadline. |
| E-L3 | SUPERSEDED by E-P3 (R4 evidence before verdict). |
| E-L4 | HOLDS: the inventory is unchanged, credited as of the read instant; MP1 adds no evidence type, and an ICMP-derived error is not evidence (E-P6). |
| E-L5 | SUPERSEDED by E-P4: refresh evidence now postpones the only terminal bound. |
| E-L6 | PARTLY HOLDS: the ACK still cannot clear a ping it did not capture; the rekey no longer follows (A10: recovered by the retransmission). |
| E-L7 | HOLDS: forwarding never pauses while a ping is unresolved; input pauses only after a transient local send failure (E-P6). |
| E-L8 | SUPERSEDED by E-P6 for the transient errnos; still true for every other socket error. |
| E-L9 | HOLDS: `main()` is unchanged. |
| E-L10 | HOLDS: the server is unchanged. It is the basis of the client-only MP1. |
| E-L11 | HOLDS: the silent drop is unchanged, but the client now notices at last evidence + `peer_timeout` (E-P7). |

### 1.3 MP1 claims

Pointers name functions in the MP1 `nmea_sproxy/nmea_sproxy.py`.

| ID | Claim | Classes | Pointer | Re-verify when |
|---|---|---|---|---|
| E-P1 | One logical ping at a time. If unanswered at its keepalive deadline, it is retransmitted then and every `min(5 s, keepalive_interval)`: same `seq`, a fresh encryption under the current epoch (fresh AEAD nonce), never replaced by a later `seq`. Each next retry is scheduled from a fresh monotonic sample taken after the previous attempt, never earlier than one retry interval after it. This was corrected after Gate A F1: the retry had been anchored at the pre-log `now`. | CODE (MP1); ASTRA GATE A (design accepted; F1 reproduced); IMPL (acceptance L1-L3, L4c, L12; `test_mp1_policy_pins.py`; `test_proxy_outstanding_ping_is_retransmitted_not_overwritten_until_peer_timeout`; F1 correction: `test_slow_suspect_log_cannot_compress_the_retry_spacing`) | `keepalive_action`, `keepalive_retry_interval`; `start_ping`, `retransmit_ping`, `transmit_ping` in `forward_loop` | any keepalive helper or `forward_loop` change |
| E-P2 | The only terminal liveness bound is last authenticated evidence + `peer_timeout`, equality due. It ends with `proactive_rekey` (immediate) when a probe is outstanding, else `peer_timeout` (after `reconnect_delay`). | CODE; IMPL (acceptance L6, L8b; guards L9; pins A5e, A7, C1, C3, C3b, C5; `test_proxy_deadline_action_has_deterministic_exact_boundary_priority`) | `liveness_verdict`, `terminal_deadline_reason`, unchanged `retry_delay_for_reason` | same |
| E-P3 | Evidence before verdict. A readable datagram is received and credited at its read instant, before the deadlines due at that instant. Before a terminal liveness verdict, at most 16 already-readable datagrams are drained (zero-timeout `select`) and the clock is re-sampled. Evidence readable only later has no effect. | CODE; IMPL (acceptance L4a, L4b, L4d, L5; units: the stall drain and the flood bound; mutation: with the drain disabled, the stall unit test fails) | `receive_datagram`, `drain_available_datagrams`, `service_deadlines`, main loop | same (Gate A focus) |
| E-P4 | Failure accounting agrees with evidence. The first REFRESH_REPLY, a REFRESH_ACK commit and a matched PATH_ACK advance liveness as of their read instant. None clears a ping it did not capture, and no verdict comes before last evidence + `peer_timeout`. The MP5 two-phase PATH_ACK admission is replaced by this rule. | CODE; IMPL (acceptance L7, L7b, L8, L8b; `test_udpsec_client_path_migration.py` sections E-G) | `receive_datagram` | same |
| E-P5 | Bounds, stated separately (corrected after Gate A F2, which showed the former unconditional "at most 12 retransmissions" false). STATE: per session, O(1) extra state (`ping_retry_at`, `ping_transmissions`, `send_paused_until`, `transient_failures`). RATE: at most one keepalive transmission per retry interval, which needs the F1 correction. EPISODE COUNT: at most ceil((`peer_timeout` - `keepalive_interval`) / retry interval) in an ordinary keepalive-only episode, 12 with the defaults and 7 in the steady state (A7, C5). With an A11 reprobe that brings retries forward, more fit before the same bound (17 in Astra's reproduction). While other qualifying evidence keeps renewing liveness there is no finite count, only the rate bound (A9b, accepted as designed). DRAIN: ≤ 16 datagrams per verdict (accepted bounded trade-off). | CODE; ASTRA GATE A (A9b and drain accepted; F2 reproduced); IMPL (pins A7, A9b, C1; units: flood; F2 correction: `test_a11_reprobe_can_exceed_the_ordinary_retransmission_count`) | same | same |
| E-P6 | Transient network errors on the session socket (`_TRANSIENT_NETWORK_ERRNOS`, plus `ConnectionRefusedError`/`ConnectionResetError`) do not end the session. The failed NMEA sentence is dropped: not counted, never retried or replayed. Input pauses one retry interval, with input sockets out of `select`, and one keepalive probe runs before input resumes. A transient receive error is ignored and is not evidence. Every other error stays terminal. | CODE; IMPL (acceptance L11a, L11b; pins A11, A11b, C1; units: classification, pause, EBADF) | `is_transient_network_error`, `note_transient_failure`, `note_send_success`, `send_sentence`, main-loop pause and probe, `forward_input_payload` | any errno-list or pause change |
| E-P7 | Trade-offs. (a) Server-restart detection moves from ~60 s to `peer_timeout` after the last evidence (C5: 1120.3 vs 1090.2; 8 vs 5 lines dropped). (b) With a dead forward path, lines forwarded until the verdict are lost (C3, C3b: 7 vs 4), because forwarding continues while liveness is only suspect (A8, MP1-L6). (c) A reverse-path-only loss costs less (A7: 40.2 s vs 70.2 s outage; 0 lines lost). | IMPL (pins; pre-MP1 numbers from one run of the MP1 lab against the pre-MP1 `nmea_sproxy.py`) | pins C3, C3b, C5, A7 | policy change |
| E-P8 | No wire, server, configuration-schema, crypto, AAD, transcript or migration change, and no new message type or version. | CODE (production diff: `nmea_sproxy/nmea_sproxy.py` only); PROJECT TEST (`tests/test_udpsec_protocol.py` wire pins; L10 guard) | `git diff 71a47894 --stat` | any production diff outside that file |
| E-P9 | The new log lines (liveness suspect, liveness recovered, transient failure, path usable again) are observational only. Nothing logged feeds back into liveness, admission or timing, and the lines carry errno text only (ASCII-escaped), never key material. | CODE | `note_transient_failure`, `note_send_success`, `retransmit_ping`, `resolve_ping` | log text changes |

## 2. Migration behaviour

### 2.0 Pre-MP2 claims (line numbers are the baseline's)

| ID | Claim | Classes | Pointer | Re-verify when |
|---|---|---|---|---|
| E-M1 | A commit preserves the `LogicalSession`, `CryptoEpoch`, replay ledger and `assembly_namespace` objects. | CODE; PROJECT TEST (e2e `test_01`, `test_03`); FABLE (T08: object ids); IMPL (MP0 B1 via weak references) | `commit_candidate_path` `3700-3869`; contract 11.3 items 1-4 | server migration code changes |
| E-M2 | Exactly one PATH_CHALLENGE per candidate incarnation; a 10 s TTL, never extended. | CODE; PROJECT TEST (e2e `test_15` counters); FABLE (T09a/b); IMPL (MP0 pre-MP2 pins) | `4424-4477`, `:127` | MP2 (expected to change) |
| E-M3 | A lost CHALLENGE or RESPONSE is recovered by candidate expiry and re-open (+10.1 s); a lost ACK by the next PONG; no rekey in any case. | PROJECT TEST (e2e `test_06/07/08`); FABLE (T09a/a7/b/c); IMPL (MP0 B2-B4) | contract 11.3 item 5 | MP2 |
| E-M4 | A ping from an unproved or retired path gets no PONG. | CODE; PROJECT TEST (`test_candidate_nmea_shares_assembly_namespace_and_gets_no_pong`); FABLE | `aismixer_secure.py:5085-5106` | server ping branch changes |
| E-M5 | Flap, rapid A→B→C, stale-epoch candidate, refresh × migration ordering and canonical sockaddr all keep one session with no rekey. | PROJECT TEST (matrix B6-B11); FABLE (T10a/b, T11a/b/c, T12); IMPL (MP0 B-invariants) | matrix B rows | server migration or refresh changes |

### 2.1 Status of the pre-MP2 claims after MP2

| ID | Status after MP2 |
|---|---|
| E-M1 | HOLDS: commit preservation is untouched. |
| E-M2 | PARTLY SUPERSEDED by E-N1: an incarnation now sends up to 4 attempts of its one challenge. The 10 s TTL, never extended, still holds. |
| E-M3 | PARTLY SUPERSEDED by E-N1: a lost challenge or response is now recovered by a same-incarnation retry about 2 s later (lab B2, B2b, B3). Candidate expiry and re-open remain the fallback when every attempt is lost (lab B12). A lost ACK is unchanged. |
| E-M4 | HOLDS: server ping handling is untouched. |
| E-M5 | HOLDS: the B invariants are green, unchanged. |

### 2.2 MP2 claims

Pointers name code in the MP2 `aismixer_secure.py`.

| ID | Claim | Classes | Pointer | Re-verify when |
|---|---|---|---|---|
| E-N1 | One live candidate incarnation sends its SAME challenge -- same token, `path_generation`, address and epoch, a fresh encryption each time -- at most `PATH_CHALLENGE_MAX_SENDS` = 4 times in total, the initial send included. Each retry comes at least `PATH_CHALLENGE_RETRY_INTERVAL_SECONDS` = 2 s after the previous attempt ended and only while `now < deadline`; the deadline is never moved. A failed send consumes its attempt and schedules the next one interval later. | CODE; IMPL (MP2-B2, B3, B4, B6, B8, B9, B11; `test_mp2_policy_pins.py`; mutations M1-M4, M6) | `claim_due_path_challenge_retries`, `finish_path_challenge_attempt`, `_build_path_challenge_packet`, `_send_due_path_challenge_retries` | any change to the retry helpers or candidate lifecycle |
| E-N2 | Commit, replacement, expiry, an epoch refresh that strands the candidate, and session removal each cancel pending retries at once. A retry claim reads only the session's live candidate, and a final revalidation right before `sendto` refuses a candidate replaced in between. | CODE; IMPL (MP2-B5, B7, B10; `test_mp2_b7_a_replacement_between_claim_and_send_is_never_sent`; `test_retry_never_crosses_an_epoch_refresh`; lab B6b; mutation M5) | `commit_candidate_path`, `open_or_replace_candidate_path`, `_expire_session_path_state`, `_remove_session`, `path_challenge_is_current` | same |
| E-N3 | Anti-amplification. Retry state exists only for a candidate installed by the unchanged authenticated, replay-admitted, current-epoch gate. Unknown-locator, forged, wrong-epoch and replayed traffic cannot create, reset or extend it. Challenges go only to the candidate address: at most 4 per incarnation, at most one per 2 s. Duplicate traffic never resets the budget. | CODE; IMPL (`test_unknown_locator_forged_or_replayed_traffic_schedules_no_retry`, MP2-B8, `test_retry_passes_create_no_candidates_and_state_stays_bounded`, `test_a_retry_pass_serves_only_its_own_listener`) | candidate gate (unchanged) + retry index | gate or retry changes |
| E-N4 | Bounds. STATE: two O(1) fields per candidate, plus a per-owner index of session keys with a scheduled retry, at most one entry per live session. RATE: at most one challenge per 2 s per candidate. COUNT: at most 4 per incarnation. No history, no heap, no per-candidate task or timer; one retry driver per listener. | CODE; IMPL | `CandidatePath`, `_path_challenge_retry_keys`, `_run_path_challenge_retries` | same |
| E-N5 | Driver lifecycle. `secure_server` runs one `_run_path_challenge_retries` per listener. It waits exactly until the earliest due retry, or until a new candidate schedules one (no polling), and is cancelled before the listener's sessions and socket are closed. An unexpected error stops only the driver, after one log line; the listener falls back to single challenges. | CODE; IMPL (`test_retry_driver_waits_exactly_until_the_next_due_retry`, `test_retry_driver_stops_quietly_on_an_unexpected_error`, `test_secure_server_owns_and_cancels_its_retry_driver`) | `secure_server`, `_run_path_challenge_retries` | listener lifecycle changes |
| E-N6 | No wire, version, AAD, crypto, transcript, client or configuration change. The unchanged client answers a duplicate challenge with a fresh response and never extends or revives its proof for it; a challenge is never client liveness evidence. | CODE (production diff: `aismixer_secure.py` only); IMPL (MP2-B3 with the real `_ClientPathMigration`; the lab B2, B2b, B3 pins; the MP1 recovery suite) | git diff | any client or wire change |
| E-N7 | Observability. `migration_challenges_sent` counts every challenge handed to `sendto`, initial or retry; `migration_challenge_retries_sent` counts the retries among them. Each retry attempt emits one `Path challenge RETRY n/4` diagnostic, or `... send FAILED`, which never contains the token. | CODE; IMPL (MP2-B1, B9; `test_retry_diagnostic_line_carries_no_challenge_token`; pins) | `finish_path_challenge_attempt`, `_format_migration_diagnostic` | stats or diagnostics changes |

### 2.3 MP3 claims

Tests are in `tests/test_udpsec_mp3_integration.py` (`test_mp3_c<N>…`);
outcomes per candidate are in baseline section 11.4.

| ID | Claim | Classes | Pointer | Re-verify when |
|---|---|---|---|---|
| E-I1 | Retry × epoch refresh (C1). A refresh commit that makes a candidate's epoch non-current stops its retries wherever it lands: before the claim, between claim and build, between build and the final revalidation, or right after a completed send (then one empty wake-up drops the stale schedule). A claimed retry is sealed only under its own epoch and never re-encrypted under the new one. The refresh changes no candidate, token, generation, deadline, old-epoch replay ledger or `LogicalSession` identity. Held on the MP2 code without change. | CODE; IMPL (C1A, C1B ×2, C1C, C1D ×3; mutation M1) | `claim_due_path_challenge_retries`, `path_challenge_is_current`, `admit_refresh_confirm` | retry, revalidation or refresh-commit changes |
| E-I2 | Retry/expiry × terminal liveness (C2). Expiry wins at equality for a late retry pass, and a retry that would fall on the deadline is never scheduled. Against the real client: a proof-matched PATH_ACK readable 10 ms before, or exactly at, the MP1 verdict keeps the session; 10 ms after, it is ignored by the fresh handshake and revives nothing. The terminated session sends nothing more, and old-session challenges reaching the new session draw no response. A late PATH_RESPONSE commits the old server session only while its candidate is live and the path is unowned; the fresh establishment then replaces it, and once the fresh session owns the path the commit is refused. No client change. Held on the MP2 code without change. | CODE; IMPL (C2A ×3; lab C2B, C2C ×2, C2D ×3; mutations M3a, M3b) | `_expire_session_path_state`, `finish_path_challenge_attempt`, `commit_candidate_path`, client `liveness_verdict`, `drain_available_datagrams`, `_ClientPathMigration` | liveness, expiry or commit changes |
| E-I3 | Shutdown with an admitted retry (C3). A retry pass (claim, build, final revalidation, `sendto`, report) is one synchronous call, and the driver's only await is its wait, so a cancellation never lands inside a pass. Listener teardown forced into any window of an admitted retry sends no challenge (final revalidation, or a closed socket). DEFECT FOUND AND CORRECTED: on the MP2 code `secure_server` only requested the driver's cancellation, so the driver was still running when the listener's sessions and socket closed (E-N5's "cancelled before" held only as a request). Under pre-3.12 `asyncio.wait_for`, a cancellation racing a wakeup was lost: the driver ran one more pass after the socket closed and then waited forever. Now `secure_server` awaits the driver's end before any cleanup, and the driver waits on its Event with a loop timer instead of `asyncio.wait_for`. The listener's exception or cancellation still propagates, and the socket closes once. | CODE; IMPL (C3A-C3D, C3C structure, C3E ×4 with native and pre-3.12 `wait_for` semantics, the wait-helper race and timer tests, the driver-failure policy test; mutations M2, M2b) | `secure_server`, `_wait_for_path_challenge_retry`, `_run_path_challenge_retries` | listener lifecycle or driver wait changes |
| E-I4 | Driver under load (C4). A retry runs late, never early; lateness only reduces how many attempts fit before the fixed deadline, and one that would run at or after it is omitted. Under a real busy event loop and a second, heavily churning listener on the same `SecureState`: no starvation, no busy loop, constant task count, retry index ≤ live sessions, and each driver claims and sends only its own endpoint token's retries through its own socket. Held on the MP2 code without change. | CODE; IMPL (C4A, C4B, the lateness model ×4; mutation M4) | `_run_path_challenge_retries`, `claim_due_path_challenge_retries` | driver or index changes |
| E-I5 | MP3 changes no wire, message, version, AAD, crypto, client, configuration, TTL, retry interval or send cap. | CODE (production diff: `aismixer_secure.py` only; the MP3 hunks are in `secure_server` and `_wait_for_path_challenge_retry`) | git diff | any further production change |

## 3. Security invariants (must not regress)

| ID | Claim | Classes | Pointer | Re-verify when |
|---|---|---|---|---|
| E-S1 | Transcript binding (version, locator, both ECDHE keys, both randoms, both signatures into the HKDF salt). | CODE (Fable-cited lines); PROJECT TEST (`tests/test_udpsec_crypto.py`); FABLE [A]. Not re-read line-by-line in MP0. | `core/udpsec_crypto.py:474-857` | any crypto or transcript change |
| E-S2 | Per-epoch replay ownership; migration never resets a ledger; nonce admission is authoritative and locked. | PROJECT TEST (D2, D8); FABLE (T08, T13-b/c); IMPL (MP0 B1, D9 sweep) | matrix D2, D8 | ledger or admission changes |
| E-S3 | Path-authority gating (unproved and retired paths carry inbound data only; commit needs exact proof). | PROJECT TEST (hardening a2/a5/a7, races); FABLE (T13-d/e/f, T10a/b, T11c) | matrix B, D5 | migration changes |
| E-S4 | No reply or amplification for an unknown locator; replays refused before any candidate. | PROJECT TEST (D1, D2); FABLE (T13-a/c); IMPL (MP0 C4, C5) | matrix D1, D2 | server lookup changes |
| E-S5 | Wrong-station, wrong-selector/epoch, forged, cross-listener and old-session ciphertext are all rejected. | PROJECT TEST (D3-D7); FABLE (T13-g/i/e/f/h/j); IMPL (MP0 C4) | matrix D3-D7 | admission or epoch changes |
| E-S6 | No AEAD nonce reuse per (direction, locator, epoch) in any scenario; random 96-bit nonce per encryption, including every MP1 retransmission. | PROJECT TEST (`test_proxy_encrypt_message_aes_gcm_uses_12_byte_nonce_and_locator_aad`); IMPL (MP0 D9 sweep; MP1: the sweep over all 34 scenarios, MP1-L12 with 8 distinct transmissions of ping#2 in A7, `test_proxy_outstanding_ping_is_retransmitted_not_overwritten_until_peer_timeout`) | `harness/lab.py` `nonce_reuse()` | every MP1 retry change (Gate A) |
| E-S7 | Unauthenticated input never becomes liveness evidence. | PROJECT TEST (D10); FABLE [A]; IMPL (MP1: the D10 tests unchanged and passing; A11b: an ICMP-derived receive error is not evidence; the flood unit test: junk datagrams in the drain are not evidence) | matrix D10 | MP1 (Gate A) |
| E-S8 | No BLOCKER or HIGH security finding in the reviewed UDPSEC V2. LOW residuals: a ClientHello costs one ECDSA verify before any per-source budget (F7); the ±30 s wall-clock window is a bring-up hazard (F6). | FABLE (sections 6-7). Not repaired. | `aismixer_secure.py:4548-4600, 4554` | handshake path changes |
| E-S9 | Diagnostics are observational only. | CODE; PROJECT TEST (`tests/test_udpsec_field_diagnostics.py`); FABLE (F9); PRIOR INDEPENDENT ASTRA EVIDENCE (diagnostics delta: no confirmed new blocker) | contract 11.4 | diagnostics changes |

## 4. Field observations

| ID | Claim | Classes | Pointer |
|---|---|---|---|
| E-F1 | IPv6 run: re-establishment after "liveness unresolved"; the old and new labels differ; the new session is `epoch 0`, `path_gen 0`; the same IPv6 address and port 46770 are seen before and after. | FIELD OBSERVATION (screenshot sha256 `4ad084221bf93a08bd106a4761303b2127af86d06e940344028c6d3a0ac925b6`); interpretation FABLE (section 4) | baseline 4.1 |
| E-F2 | IPv4/CGNAT run: the public tuple was stable over long intervals (`90.154.211.195:54654`); re-establishments still occurred; `path_gen` stayed 0. | FIELD OBSERVATION (operator report in the MP0 instruction; no logs supplied) | baseline 4.2 |
| E-F3 | Wi-Fi-loss experiment: `Errno 101` handshake send errors every 5 s; afterwards a fresh session with the same public IPv4 and CGNAT port 54654→54104. | FIELD OBSERVATION; mechanism reproduced as IMPL (MP0 C1/C2) | baseline 4.3 |
| E-F4 | Which packet was lost, and for how long, in any field event; server-side NMEA loss in the field. | NOT YET VERIFIED (not identifiable from the data) | Fable 5.2, worksheet section 11 |
| E-F5 | MP1 road run, after `aa41b782`: one logical session survived the whole run; one keepalive exchange needed one retransmission and recovered in-session; the server-observed public IP and port were unchanged and `path_gen` stayed 0. A same-tuple liveness event (class A), not a migration. Which packet was lost is unknown. | FIELD OBSERVATION (operator report in the MP2 instruction; no logs supplied) | baseline 4.5 |

## 5. Test and environment provenance

| ID | Claim | Classes |
|---|---|---|
| E-T1 | The Fable harness ran on Linux 7.0.0-34, CPython 3.12.3, cryptography 41.0.7 (below the declared `>=42.0` floor), without pytest. It did not exercise `main()`, maintenance, SIGTERM lifecycle or the diagnostics output worker. | FABLE (self-reported) |
| E-T2 | The MP0 port reproduces Fable's observed timings for every ported scenario on Windows 11 Pro 10.0.26200, CPython 3.14.7, cryptography 50.0.0, PyYAML 6.0.3, pytest 9.1.1. | IMPLEMENTER-REPORTED ONLY |
| E-T3 | MP0 test results are listed in the MP0 report: on Windows, `tests/udpsec_recovery`, the related existing UDPSEC suites and one full suite; on Linux (WSL2 Ubuntu, kernel 5.15, CPython 3.12.3, pytest 9.1.1, cryptography 41.0.7, below the declared floor), `tests/udpsec_recovery` only. | IMPLEMENTER-REPORTED ONLY |
| E-T4 | All ten MP1 xfails fail today at their first acceptance assertion, which states the documented reason (checked with `--runxfail`). A scratch in-memory stand-in (not committed) made the unresolved ping re-probe instead of rekeying. Under it, nine turned into strict XPASS, and all invariant tests still passed. MP1-L4b XPASSed only with an added boundary-ordering stand-in, and MP1-L6 is exact at the `peer_timeout` bound. | IMPLEMENTER-REPORTED ONLY |
| E-T5 | MP0 harness behaviour on OpenWrt or a Raspberry Pi, and on Linux with cryptography `>=42.0`. | NOT YET VERIFIED |
| E-T6 | Earlier independent Astra targeted runs at `c0a945d`: S6/S7 selections Windows 26 passed / 8 skipped and Linux 34 passed; a later targeted client-diagnostics selection passed 28 on each platform. Astra identified two observability gaps (heartbeat `path_gen`, the contract 11.4 section), since addressed. It found no confirmed new blocker for the diagnostics changes, and did not establish trip readiness. Residuals: repeated SIGTERM in a no-running-loop teardown gap (server); serial-client immediate-SIGTERM fake-hardware lock. | PRIOR INDEPENDENT ASTRA EVIDENCE (carried forward; the selections' exact content is defined in the prior assignment, not reproduced here) |
| E-T7 | Earlier full-suite results: Windows 3753 passed / 37 skipped; Linux 3789 passed / 1 failed (a known OpenWrt-packaging WSL/POSIX baseline failure, reproduced on clean HEAD). | IMPLEMENTER-REPORTED ONLY (per the Astra resume note) |
| E-T8 | MP1 acceptance tests discriminate. On the MP1 client all 16 pass. With the MP1 test tree run against the pre-MP1 `nmea_sproxy.py` (a scratch extraction of `71a47894` outside the repository), 15 fail at an acceptance assertion (`AssertionError`). The 16th, MP1-L4d, passes; it is a guard that holds under both policies. | IMPLEMENTER-REPORTED ONLY |
| E-T9 | Mutation check: with `EVIDENCE_DRAIN_MAX_DATAGRAMS = 0`, exactly one test in the acceptance and unit modules fails, the stall-drain unit test. The lab boundary scenarios are carried by the evidence-first receive order. | IMPLEMENTER-REPORTED ONLY |
| E-T10 | MP1 results on Windows 11 Pro 10.0.26200 (CPython 3.14.7, pytest 9.1.1, cryptography 50.0.0, PyYAML 6.0.3): MP1 acceptance, pin and unit modules 72 passed; the four changed existing test files 550 passed; `tests/udpsec_recovery` 142 passed; related UDPSEC/proxy slice 1900 passed, 8 skipped (POSIX-only SIGTERM tests); one full suite 3895 passed, 37 skipped (platform skips only). | IMPLEMENTER-REPORTED ONLY |
| E-T11 | MP1 on Linux (WSL2 Ubuntu, kernel 5.15.153.1, CPython 3.12.3, pytest 9.1.1, cryptography 41.0.7, below the declared floor): `tests/udpsec_recovery` 142 passed. No Linux full suite was run for MP1. | IMPLEMENTER-REPORTED ONLY |
| E-T12 | The pre-MP1 comparison numbers for scenarios MP1 added or re-measured (A4, A4b, A5e, A7, A9b, A10b, A11, A11b, C1, C3, C3b, C5) come from one run of the MP1 lab against the pre-MP1 `nmea_sproxy.py` in that scratch extraction. | IMPLEMENTER-REPORTED ONLY |
| E-T13 | F1-F3 corrective tests: the two new regressions pass on the corrected code. Against the pre-correction `nmea_sproxy.py` (a scratch copy), the F1 regression fails exactly at Astra's reproduction (second retry at 65, one second after the delayed first retry at 64). The F2 accounting regression passes on both, because it pins documented behaviour, not a code change. | IMPLEMENTER-REPORTED ONLY (until the Astra corrective recheck) |
| E-T14 | Final micro-correction (`start_ping` post-attempt anchor). `test_slow_initial_ping_send_cannot_compress_first_retry_spacing` passes: 3.5, 5.5, 7.5, …, 19.5, and `proactive_rekey` at 20.0. Against the pre-micro-correction `nmea_sproxy.py` (a scratch copy) it fails at Astra's reproduction: initial attempt 3.5, first retry 4.0. `tests/udpsec_recovery` 145 passed. The proxy `forward_loop` test files passed 837 with 8 skipped, after updating the pinned first-retry expectation of G1/G2 in `test_udpsec_client_path_migration.py` (baseline 11.2). | IMPLEMENTER-REPORTED ONLY (until the final Astra micro-recheck) |
| E-T15 | MP2 results, Windows 11 Pro 10.0.26200 (CPython 3.14.7, pytest 9.1.1, cryptography 50.0.1, PyYAML 6.0.3). Before any production edit, the pre-MP2 characterization passed: the 8 pre-MP2 pin cases and 187 migration and invariant tests. With MP2: the MP2 unit, pin and consistency tests 45 passed; the migration suites 205 passed; `tests/udpsec_recovery` 154 passed; the UDPSEC/server slice 1633 passed with 8 skipped (POSIX-only SIGTERM tests); one full suite 3930 passed with 37 skipped (platform skips only). No second OS. | IMPLEMENTER-REPORTED ONLY |
| E-T16 | MP2 mutations, each run on a scratch copy outside the repository and restored afterwards, each killed. M1 (a retry extends the expiry) and M4 (the send cap is ignored): `test_mp2_b4_all_attempts_lost_…`. M2 (a retry mints a new token) and M3 (a retry mints a new generation): `test_mp2_b2_lost_first_challenge_…`. M5 (the pre-send revalidation is removed, so a candidate superseded between claim and send is retried): `test_mp2_b7_a_replacement_between_claim_and_send_is_never_sent`. M6 (cached ciphertext resent): `test_mp2_b11_every_attempt_is_a_fresh_encryption`. | IMPLEMENTER-REPORTED ONLY |
| E-T17 | MP3 results, Windows 11 Pro 10.0.26300 (CPython 3.14.7, pytest 9.1.1, cryptography 50.0.1, PyYAML 6.0.3). Before any production edit, on the MP2 code: the MP3 module ran 31 passed and 5 failed, all C3E (the 4 listener-shutdown cases: driver still running at session and socket close, and never ending under pre-3.12 `wait_for` semantics; plus the pre-3.12 wait-helper race). With the MP3 correction: the MP3 module 36 passed; with the MP2 retry module 59 passed; the migration suites (retry, client migration, mobile e2e) 96 passed; `tests/udpsec_recovery` 154 passed; the refresh and lifecycle slice (refresh, refresh protocol, secure helpers, `test_aismixer_secure.py`, migration lifecycle races, path migration, runtime supervision) 672 passed; the broad UDPSEC slice 1823 passed with 8 skipped (POSIX-only SIGTERM tests); one full suite 3966 passed with 37 skipped (platform skips only). No second OS; no Python 3.11 interpreter was available. | IMPLEMENTER-REPORTED ONLY |
| E-T18 | MP3 mutations, each applied to the working `aismixer_secure.py` by a scratch script, run, and restored byte-exact (sha256 checked before and after), each killed. M1 (final revalidation removed): C1B ×2 and C3D ×2. M2 (the termination wait removed): C3E ×4. M2b (the wait kept, the pre-MP3 `wait_for` helper restored): the pre-3.12 C3E cases, where a PATH_CHALLENGE leaves after shutdown began and shutdown never finishes, and the pre-3.12 wait-helper race. M3a (a candidate expires only after its deadline): C2A ×2 on expiry state; the wire stayed clean because the final revalidation is strict. M3b (M3a, and the final revalidation admits equality): C2A ×2, with a challenge sent exactly at the deadline. M4 (the claim ignores the endpoint token): C4B. | IMPLEMENTER-REPORTED ONLY |

## 6. Ingested artifacts (full hashes)

| Artifact | sha256 |
|---|---|
| `UDPSEC_V2_FABLE_AUDIT.md` | `4b48f1ac01eb96f8803106a0e519619207a599871e860774b5efb38deaf41300` |
| `UDPSEC_V2_REPAIR_BACKLOG.md` | `71dd17c612076d4fecbe01a4ed34f1f2353f4840c5f9448eb0864b16795934bd` |
| `UDPSEC_V2_TEST_MATRIX.md` | `075001eb5fe223f9c18ca97c42e38898e3ae394c4ca476c6b00a7648fd4c3198` |
| `AISMixer_Fable_UDPSECv2_Deep_Independent_Audit_20260927.txt` | `708371bfd0f0abe57d74f34572ace27eae3832ed98b2ecee443de5fdee0dbdc7` |
| `harness/udpsec_lab.py` | `8285768fafdc41e155686981df7d7e6d7ffc4e91407c4b073b588230ee070794` |
| `harness/scenarios.py` | `5eb227ed21f9632fd37ead2344d75742cd33ad8d246a34d5a93248c858a5a24b` |
| `harness/pytest.py` (stub, not ported) | `42d8ac62f4be7161a88acbcab4b51c43b9cc3362b4833b76338beb2aed32aed6` |
| `harness/show.py` (not ported) | `7b4846556c10b40e75bc1c20ae09e0f74512afafac49f8cf4b68049ea2c30dd7` |
| `harness/logs/_results.json` (not copied) | `3b21555838edffbc76a7c44a00e14857aaf9eacd206321381e51e62b7c6513e2` |
| `photo_2026-09-27_11-47-51.jpg` | `4ad084221bf93a08bd106a4761303b2127af86d06e940344028c6d3a0ac925b6` |
| `aismixer_astra_RESUME_budget_capped_20260925.txt` | `a739122c85b66ee492005175fce5674a0558c67f5d36080da7628779ef132f20` |

## 7. What a Gate A reviewer may carry forward unchanged

The MP0 condition was: if the MP1 delta touches only
`nmea_sproxy/nmea_sproxy.py` (the liveness state and the deadline helpers in
`forward_loop`), its config validation, and the section 9 tests, then
E-M1..E-M5, E-S1, E-S3, E-S4, E-S5 (server side), E-S8 and E-S9 carry forward
without re-audit.

It holds for the MP1 delta. The production change is limited to
`nmea_sproxy/nmea_sproxy.py`: its liveness state, deadline helpers,
`forward_loop` receive/drain/pause logic and `forward_input_payload`
counting. Config validation and defaults are unchanged. The test changes are
the ones listed in baseline section 9.1.

Gate A therefore re-verifies:
- E-P1..E-P9;
- E-L4, E-L6 and E-L7 as restated in 1.2;
- E-S6 and E-S7 against the delta.

E-S2 (server-side ledger) is untouched, but retransmissions add admitted
DATA traffic: one ping per retry interval at most. Gate A also re-verifies
the R4 × MP5 change (baseline 7.1, "MP1 (conflict resolved)") and reruns the
slices named in baseline 11.1.

## 8. Astra Gate A (MP1) and the F1-F3 corrective iteration

| ID | Claim | Classes |
|---|---|---|
| E-G1 | Gate A verdict on the MP1 worktree: NEEDS CORRECTION BEFORE MP2. No BLOCKER or HIGH security defect. | ASTRA GATE A |
| E-G2 | Delta scope: production `nmea_sproxy/nmea_sproxy.py` only; no server, wire/version, AAD, crypto, transcript or configuration change (E-P8). | ASTRA GATE A; IMPL |
| E-G3 | Accepted: the core liveness design (E-P1, E-P2), R4 (E-P3), PATH_ACK authority and epoch-refresh composition (E-P4), terminal recovery (E-P2, E-P7). | ASTRA GATE A |
| E-G4 | A9b accepted as designed: the state and rate are bounded and the episode count is not. A finite retry-count cap could end a session despite fresh qualifying evidence. | ASTRA GATE A |
| E-G5 | The 16-datagram drain is accepted as an explicit bounded trade-off. A valid PONG queued as datagram #17 behind 16 junk datagrams can miss the drain, and the terminal recovery then follows. The junk gains no authority. | ASTRA GATE A |
| E-G6 | F1 (LOW): after a 4 s delay of the first "liveness suspect" line, keepalive transmissions left at about 64 s and 65 s; the next retry was anchored at a stale pre-log sample. Corrected in `retransmit_ping` (baseline 11.2); the Astra corrective recheck accepted that part. The recheck also found `start_ping` still anchored before its attempt (keepalive 2 s with a 1.5 s slow initial send gave a 0.5 s first-retry gap), corrected by the final micro-correction. | ASTRA GATE A (reproduced); Astra corrective recheck (`retransmit_ping` part accepted; `start_ping` part reproduced); corrections IMPL (E-T13, E-T14) |
| E-G7 | F2 (LOW): the unconditional "never more than 12 retransmissions" claim is false. The A11 interaction gives 17 retransmissions before the correct verdict at 150. The accounting in E-P5, the contract and the code comment is corrected; no cap was added. | ASTRA GATE A (reproduced); correction IMPL (E-T13) |
| E-G8 | F3 (LOW): the operator guide said a fixed 5 s interval and pause and "any authenticated answer". It now gives `min(5 s, keepalive_interval)`, a pause of one retry interval, and qualifying authenticated evidence. | ASTRA GATE A; correction documentation only |
