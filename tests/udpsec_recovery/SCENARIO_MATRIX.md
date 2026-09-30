# UDPSEC V2 Recovery Scenario Matrix (MP0, updated by MP1)

Companion to `UDPSEC_V2_RECOVERY_BASELINE.md`. Pre-MP1 baseline commit
`de513674` (MP0 itself was committed as `71a47894`); MP1 is the client-only
delta on top of `71a47894`. One row per scenario; `test_baseline_consistency.py`
checks this table against the harness catalogue and the MP1 acceptance tests,
so it cannot silently drift.

Legend:
- **Times** are fake-clock seconds in the lab: the session is confirmed at 1000.2, ping#1 leaves at 1030.2, ping#2 at 1060.2, and NMEA is produced every 10 s. Production defaults apply: keepalive 30, peer_timeout 90, reconnect_delay 5. MP1 retransmits an unanswered ping at its keepalive deadline and then every 5 s.
- **Tuple**: `stable`, or the server-observed change (A = `2001:db8:a::1.46770`, B = `2001:db8:b::7.46770`, C = IPv4 `203.0.113.30:43000`).
- **C→S / S→C**: availability of client→server and server→client delivery.
- **Pre-MP1 result**: observed on the pre-MP1 client by the Fable harness and/or the MP0 lab. For A5e, A9b, A10b and A11b (scenarios added by MP1) it comes from one implementer run of the MP1 lab against the pre-MP1 `nmea_sproxy.py`.
- **MP1 result**: observed on the MP1 client by the same lab (implementer-run).
- **Target**: the requirement. `MP1-Lx` IDs map 1:1 to `test_mp1_liveness_acceptance.py`; MP1-L9 and MP1-L10 are guards in `test_recovery_invariants.py`.
- **Session / Epoch / path_gen / Forward data**: expectation after MP1 (after MP2 where noted).
- **Components** (production code exercised):
  - `C.hs` `perform_handshake`
  - `C.fl` `forward_loop` and its deadline helpers
  - `C.main*` `main()` relation loop (replicated) with the real `retry_delay_for_reason`
  - `C.pm` `_ClientPathMigration`
  - `C.rf` `_ClientEpochRefresh`
  - `S.hs` server handshake and pending promotion
  - `S.loop` `_secure_server_loop` + `SecureState` admission, replay ledger, PONG
  - `S.pm` server path migration
  - `S.rf` server epoch refresh
- **Provenance** (what exists; a PLANNED entry proves nothing):
  - `FABLE`: executed by the independent Fable harness, 2026-09-28, on the pre-MP1 client (logs outside the repo);
  - `MP0`: executed by the MP0 repository lab (implementer-run);
  - `MP1`: an MP1 acceptance test or MP1 policy pin runs this scenario on the MP1 client (implementer-run; independent verification belongs to Gate A);
  - `PYTEST`: covered by pre-existing project tests (listed below);
  - `FIELD`: operator field observation;
  - `PLANNED`: future test (MP2).
  - MP0's `XFAIL` tag is retired: MP1 removed every strict xfail marker without changing an assertion.

<!-- matrix:start -->
| ID | Fable | Class | Scenario | Tuple | C→S | S→C | Outstanding ping | Candidate | Refresh | Pre-MP1 result | MP1 result | Target | Session | Epoch | path_gen | Forward data | Components | Provenance |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| A1 | T01 | A | healthy baseline | stable | ok | ok | each ping answered | — | — | 1 session; 6 pings / 6 pongs; 19/19 NMEA prompt | unchanged; no retransmission | unchanged; no extra probes while healthy | preserve | 0 unchanged | 0 | all prompt | C.hs C.fl C.main* S.hs S.loop | FABLE MP0 PYTEST |
| A2 | T02 | A | lost PING | stable | ping#2 lost | ok | #2 unresolved | — | — | proactive_rekey 1090.2; new session 1090.4 (epoch 0, path_gen 0, same tuple); 19/19 NMEA | ping#2 retransmitted 1090.2 (same seq, fresh nonce), answered 1090.3; same session; 19/19 NMEA prompt | MP1-L1 same LogicalSession | preserve | unchanged | unchanged | all prompt | C.hs C.fl C.main* S.hs S.loop | FABLE MP0 MP1 |
| A3 | T03 | A | lost PONG | stable | ok | pong#2 lost | #2 unresolved (server answered) | — | — | as A2; server never noticed | as A2: retransmission answered 1090.3; same session | MP1-L2 same LogicalSession | preserve | unchanged | unchanged | all prompt | C.hs C.fl C.main* S.hs S.loop | FABLE MP0 MP1 |
| A4 | T04 | A | 5 s two-way blackhole | stable | lost 1058-1063 | lost 1058-1063 | #2 lost in hole | — | — | proactive_rekey 1090.2, 27 s after recovery; 1 NMEA lost in hole | retransmission 1090.2 answered 1090.3; same session; 1 NMEA lost in hole | MP1-L3 same LogicalSession | preserve | unchanged | unchanged | only in-hole line lost | C.hs C.fl C.main* S.hs S.loop | FABLE MP0 MP1 |
| A4b | T04b | A/C | 37 s two-way blackhole past deadline | stable | lost 1058-1095 | lost 1058-1095 | #2 lost | — | — | proactive_rekey 1090.2; hello lost; failure 1095.2; +5 s; new session 1100.4 | retransmissions 1090.2 (lost) and 1095.2 (answered 1095.3); same session; 4 in-hole lines lost | MP1 bound (baseline 7.3): recover in session when the path returns before last evidence + peer_timeout | preserve | unchanged | unchanged | in-hole lines lost | C.hs C.fl C.main* S.hs S.loop | FABLE MP0 MP1 |
| A5a | T05a | A | PONG 10 ms before deadline | stable | ok | pong#2 delayed | #2 answered late | — | — | accepted; 1 session | unchanged | unchanged | preserve | unchanged | unchanged | all prompt | C.fl S.loop | FABLE MP0 PYTEST |
| A5b | T05b | A | PONG readable exactly at deadline | stable | ok | pong#2 held to deadline | #2 answer readable at deadline | — | — | proactive_rekey; PONG consumed by next handshake | PONG read and credited at 1090.2 before the deadline is serviced; no retransmission; same session | MP1-L4a processed before verdict | preserve | unchanged | unchanged | all prompt | C.hs C.fl C.main* S.loop | FABLE MP0 PYTEST MP1 |
| A5c | T05c | A | PONG 10 ms after deadline | stable | ok | pong#2 +10 ms | #2 answered after deadline | — | — | proactive_rekey | retransmission 1090.2; original PONG at 1090.21 resolves ping#2; the retransmission's PONG is an ignored duplicate; same session | MP1-L4c one miss not terminal | preserve | unchanged | unchanged | all prompt | C.hs C.fl C.main* S.loop | FABLE MP0 MP1 |
| A5d | — | A | PONG readable at peer_timeout boundary (peer_timeout 45) | stable | ok | pong#1 held to boundary | #1 answer readable at boundary | — | — | peer_timeout; PONG discarded; +5 s; new session | PONG credited at the 1045.2 boundary; same session | MP1-L4b R4 ordering | preserve | unchanged | unchanged | all prompt | C.hs C.fl C.main* S.loop | MP0 MP1 |
| A5e | — | A/C | PONG 10 ms after peer_timeout boundary (peer_timeout 45) | stable | ok | pong#1 held to boundary + 10 ms | #1 answered after boundary | — | — | peer_timeout 1045.2; +5 s; new session 1050.4 | proactive_rekey 1045.2 (probe outstanding); immediate new session 1045.4; the late PONG is skipped by the handshake | MP1-L4d late evidence stays late (R4 item 4) | fresh | new session 0 | new session 0 | all prompt | C.hs C.fl C.main* S.loop | MP1 |
| A6 | T07 | A | 35 s client stall, PONG buffered | stable | ok | ok (buffered) | #2 answered, unread | — | — | proactive_rekey on resume 1095.0; PONG discarded | buffered PONG credited on resume 1095.0; ping#3 at 1095.0; same session | MP1-L5 use buffered evidence | preserve | unchanged | unchanged | stall-delayed lines only | C.hs C.fl C.main* S.loop | FABLE MP0 MP1 |
| A7 | T06a | A | reverse-path-only loss 100 s | stable | ok | lost 1055-1155 | #2 and later unanswered | — | — | proactive_rekey 1090.2; 7 failed handshakes; new session 1160.4; 70.2 s forward outage with C→S healthy | ping#2 + 7 retransmissions 1090.2-1120.2 (same seq, fresh nonces) while forwarding continues; proactive_rekey 1120.3 (pong#1 + 90); 4 failed handshakes; new session 1160.5; 40.2 s forward outage; 0 lines lost | MP1-L6 forward past first miss; end ≤ last evidence + peer_timeout; MP1-L12 same seq, fresh nonce per retransmission | may end, never at first miss | unchanged while alive | 0 if re-established | continuous while alive | C.hs C.fl C.main* S.hs S.loop | FABLE MP0 MP1 |
| A8 | (A3 run) | A | NMEA while PONG unresolved | stable | ok | pong#2 lost | #2 unresolved | — | — | NMEA in the unresolved window delivered promptly; not client-visible evidence | unchanged: forwarding never pauses while liveness is suspect | unchanged; NMEA-as-evidence OPEN | n/a | n/a | n/a | continuous | C.fl S.loop | MP0 PYTEST |
| A9 | T14 | A | refresh evidence + lost PONG | stable | ok | pong#2 lost | #2 unresolved | — | E0→E1 committed 1065.4 | proactive_rekey 1090.2 despite refresh evidence | retransmission 1090.2 answered 1090.3; same session; refreshes keep committing | MP1-L7 agree with refresh evidence | preserve | refresh only (≥ E1) | unchanged | all prompt | C.fl C.rf S.loop S.rf | FABLE MP0 MP1 |
| A9b | — | A | refresh-only evidence, every PONG lost (L7-strong) | stable | ok | pong#2 and later lost | #2 never answered | — | commits 1065.4, 1130.6, 1195.8, 1261.0 | 3 re-handshakes (1090.2, 1180.4, 1270.6) despite refresh evidence | same session for the whole window on refresh evidence alone; ping#2 retransmitted every 5 s from 1090.2 (rate-bounded, not count-bounded) | MP1-L7b no verdict contradicts refresh evidence | preserve | refresh only | unchanged | all prompt | C.fl C.rf S.loop S.rf | MP1 |
| A10 | T09d | A/B | ping overtakes PATH_RESPONSE (= B5) | A→B 1059.9 | ok | ok | #2 from unproved B, no PONG | B opened 1060.05, committed 1060.55 | — | ACK accepted 1060.6; proactive_rekey 1090.2; new session at B, path_gen 0 | ACK accepted 1060.6 (liveness, no ping clear); retransmission 1090.2 from B answered 1090.3; migrated session kept | MP1-L8 keep migrated session | preserve | unchanged | 1 | all prompt | C.fl C.pm S.pm S.loop | FABLE MP0 PYTEST MP1 |
| A10b | — | A/B | PATH_ACK-only evidence, every PONG lost (L8-strong) | A→B 1059.9 | ok | pong#2 and later lost | #2 never answered | committed 1060.55 | — | proactive_rekey 1090.2 and 1180.4 | ACK read at 1060.6 is the last evidence; proactive_rekey 1150.6 (ACK + 90, not pong#1 + 90 = 1120.3); new session at B | MP1-L8b ACK evidence counts though it cannot clear the ping | ends at ACK + peer_timeout | new session 0 | new session 0 | all prompt | C.hs C.fl C.main* C.pm S.pm S.loop | MP1 |
| A11 | — | A/C | 2.1 s local ENETUNREACH | stable | local send error 1059.9-1062 | nothing arrives | — | — | — | socket_error 1060.0; +5 s; new session 1065.2; failed line lost | 1060.0 line dropped and input paused; ping#2 fails at 1060.2; the 1065.2 probe succeeds, answered 1065.3; same session; only the failed line lost | MP1-L11a transient send error keeps the session | preserve | unchanged | unchanged | 1 line lost, no replay | C.fl C.hs C.main* | MP0 MP1 |
| A11b | — | A | ICMP-derived receive error (ECONNRESET) at 1070 | stable | ok | one reset on recvfrom | — | — | — | socket_error 1070.0; +5 s; new session 1075.2 | reported once and ignored; same session; keepalive cadence unchanged | MP1-L11b transient receive error keeps the session | preserve | unchanged | unchanged | all prompt | C.fl | MP1 |
| B1 | T08 | B | healthy A→B migration | A→B 1045 | ok | ok | answered | opened 1050.05, committed 1050.15 | — | same session, epoch, ledger, namespace objects; 1 challenge | unchanged | unchanged | preserve | 0 unchanged | 0→1 | all prompt | C.fl C.pm S.pm S.loop | FABLE MP0 PYTEST |
| B2 | T09a | B | lost PATH_CHALLENGE | A→B 1045 | ok | 1st challenge lost | answered | 2 incarnations; commit 1060.15 (+10.1 s) | — | no rekey; recovery waits candidate TTL | unchanged | MP2 faster commit via bounded challenge retransmission; no rekey | preserve | unchanged | 0→2 | all prompt | C.fl C.pm S.pm S.loop | FABLE MP0 PYTEST PLANNED |
| B2b | T09a7 | B | lost PATH_CHALLENGE, 7 s NMEA | A→B 1045 | ok | 1st challenge lost | answered | 2 incarnations; ping#2 re-opens; commit 1060.35 | — | no rekey | unchanged | MP2 as B2 | preserve | unchanged | 0→2 | all prompt | C.fl C.pm S.pm S.loop | FABLE MP0 PLANNED |
| B3 | T09b | B | lost PATH_RESPONSE | A→B 1045 | 1st response lost | ok | answered | 2 incarnations; commit 1060.15 (+10.1 s) | — | no rekey; recovery waits candidate TTL | unchanged | MP2 as B2 | preserve | unchanged | 0→2 | all prompt | C.fl C.pm S.pm S.loop | FABLE MP0 PYTEST PLANNED |
| B4 | T09c | B | lost PATH_ACK | A→B 1045 | ok | 1st ACK lost | answered | committed 1050.15 | — | no rekey; client learns via next PONG | unchanged | unchanged | preserve | unchanged | 0→1 | all prompt | C.fl C.pm S.pm S.loop | FABLE MP0 PYTEST |
| B6a | T11b | B | refresh starts while candidate unproved | A→B 1045 | ok | challenge held 1.5 s | answered | committed 1051.65 | INIT from unproved path refused; E1 after commit | migration first, then refresh; no rekey | unchanged | unchanged (MP3 composition) | preserve | refresh only | 0→1 | all prompt | C.fl C.pm C.rf S.pm S.rf S.loop | FABLE MP0 PYTEST |
| B6b | T11c | B | stale E0 candidate after refresh | A→B→A→B 1049.9-1052 | ok | 1st challenge unreachable | answered | E0 candidate expired; E1 incarnation committed 1060.15 | E0→E1 1050.4 | stale candidate replaced, never revived; no rekey | unchanged | unchanged (MP3 composition) | preserve | refresh only | 0→2 | all prompt | C.fl C.pm C.rf S.pm S.rf S.loop | FABLE MP0 PYTEST |
| B7 | T11a | B | refresh then migration | A→B 1045 | ok | ok | answered | committed 1050.15 under E1 | E0→E1 1040.4 | migration under E1; no rekey | unchanged | unchanged | preserve | refresh only | 0→1 | all prompt | C.fl C.pm C.rf S.pm S.rf S.loop | FABLE MP0 PYTEST |
| B8 | T10b | B | A→B→C with late old-path packet | A→B 1045, B→C 1052 | ok | ok | answered | 2 commits (1050.15, 1060.15) | — | late A packet admitted on retired path; no reverse migration | unchanged | unchanged | preserve | unchanged | 0→2 | all + late line | C.fl C.pm S.pm S.loop | FABLE MP0 PYTEST |
| B9 | — | B | return to former path after retired grace | A→B→A | ok | ok | — | fresh candidate + proof required | — | covered by existing unit/integration tests | unchanged | unchanged | preserve | unchanged | increments | n/a | S.pm | PYTEST |
| B10 | T10a | B | flap A→B→A during challenge | A→B 1045, →A 1052 | ok | challenge held 3 s | answered | 1 candidate, expired, no commit | — | response from A ignored; no rekey | unchanged | unchanged | preserve | unchanged | 0 (candidate gen 1) | all prompt | C.fl C.pm S.pm S.loop | FABLE MP0 PYTEST |
| B11 | T12 | B | flowinfo-only change vs family/port change | A→A2 1045, →C 1075 | ok | ok | answered | no candidate for A2; C committed 1080.15 | — | canonical sockaddr semantics hold | unchanged | unchanged | preserve | unchanged | 0→1 | all prompt | C.fl C.pm S.pm S.loop | FABLE MP0 PYTEST |
| C1 | — | C | 6 min local Wi-Fi loss, same tuple | stable (IPv4 CGNAT) | local send error 1058-1418 | nothing arrives | — | — | — | socket_error 1060.0; handshake send errors every 5 s; new session 1420.2; old server session expired (TTL); 1 line lost | 1060.0 line dropped, input paused; ping#2 probes fail every 5 s (1060.2-1120.2); proactive_rekey 1120.3; handshake send errors every 5 s; new session 1420.5; 1 line lost | unchanged: terminal recovery is correct | fresh | new session 0 | new session 0 | queued, burst after recovery | C.fl C.hs C.main* S.hs S.loop | FIELD MP0 MP1 |
| C2 | — | C | 6 min loss, CGNAT port 54654→54104 | port change during outage | local send error 1058-1418 | nothing arrives | — | none (no old-session traffic from new tuple) | — | new session 1420.2 observed at new port; no candidate; old session expired | as C1; new session 1420.5 at the new port | unchanged: terminal recovery, not migration | fresh | new session 0 | new session 0 | queued, burst after recovery | C.fl C.hs C.main* S.hs S.loop | FIELD MP0 |
| C3 | — | C | 242 s silent blackhole | stable | lost 1058-1300 | lost 1058-1300 | unanswered | — | — | proactive_rekey 1090.2; attempts every 10 s; new session 1300.4; 4 lines lost | retransmissions 1090.2-1120.2; proactive_rekey 1120.3; attempts every 10 s; new session 1300.5; 7 lines lost (trade-off, baseline 7.3) | MP1-L9 guard: end ≤ last evidence + peer_timeout; bounded backoff | fresh | new session 0 | new session 0 | lost until the verdict, then queued | C.fl C.hs C.main* S.hs S.loop | MP0 MP1 |
| C3b | T06b | C | forward-path-only loss 100 s | stable | lost 1055-1155 | ok | unanswered | — | — | proactive_rekey 1090.2; 7 failed handshakes; new session 1160.4; 4 lines lost | proactive_rekey 1120.3; 4 failed handshakes; new session 1160.5; 7 lines lost (trade-off, baseline 7.3) | MP1-L9 guard as C3 | fresh | new session 0 | new session 0 | lost until the verdict, then queued | C.fl C.hs C.main* S.hs S.loop | FABLE MP0 MP1 |
| C4 | T13-j | C | old-session packets after fresh establishment (C3 run) | stable | ok | ok | — | — | — | replayed and freshly encrypted old-session DATA: no reply, no ingress, no counter change | unchanged | unchanged | n/a | n/a | n/a | n/a | S.loop | FABLE MP0 PYTEST |
| C5 | T16 | C | server restart (unknown locator) | stable | ok (silently dropped) | ok | unanswered | — | — | 5 NMEA silently dropped; proactive_rekey 1090.2; new session 1090.4 | 8 NMEA silently dropped; retransmissions 1090.2-1120.2; proactive_rekey 1120.3; new session 1120.5 (detection-time trade-off, baseline 7.3) | guard: fresh session ≤ last evidence + peer_timeout + reconnect_delay; MP1 detects at last evidence + peer_timeout | fresh | new session 0 | new session 0 | lost until re-establishment | C.fl C.hs C.main* S.hs S.loop | FABLE MP0 PYTEST MP1 |
| D1 | T13-a | D | unknown locator → no reply | any | — | — | — | — | — | silent drop, no state | unchanged | unchanged | — | — | — | — | S.loop | FABLE PYTEST |
| D2 | T13-b,c | D | replay → no fresh authority | any | — | — | — | no candidate from replay | — | REPLAY; no reply, no candidate | unchanged | unchanged | — | — | — | — | S.loop S.pm | FABLE PYTEST |
| D3 | T13-g | D | wrong station → reject | any | — | — | — | — | — | dropped before nonce admission | unchanged | unchanged | — | — | — | — | S.hs S.loop C.pm | FABLE PYTEST |
| D4 | T13-i | D | wrong selector / epoch → reject | any | — | — | — | — | any | dropped; one exact-epoch lookup | unchanged | unchanged | — | — | — | — | S.loop S.rf C.rf C.pm | FABLE PYTEST |
| D5 | T13-e,f | D | forged / invalid ciphertext → reject | any | — | — | — | no commit | — | no state change | unchanged | unchanged | — | — | — | — | S.loop S.pm C.fl | FABLE PYTEST |
| D6 | T13-h | D | cross-listener confusion → reject | any | — | — | — | — | — | inert | unchanged | unchanged | — | — | — | — | S.loop S.pm | FABLE PYTEST |
| D7 | T13-j | D | old-session ciphertext → reject | any | — | — | — | — | — | unknown locator after replacement | unchanged | unchanged | — | — | — | — | S.hs S.loop | FABLE MP0 PYTEST |
| D8 | T08 | D | migration does not reset replay ledger | A→B | — | — | — | committed | — | same ledger object rejects pre-migration nonce | unchanged | unchanged | — | same ledger | — | — | S.pm S.loop | FABLE MP0 PYTEST |
| D9 | — | D | liveness retry never reuses an AEAD nonce | any | — | — | — | — | any | no nonce reuse in any MP0 scenario; same-seq fresh-nonce ping answered, byte replay refused | every retransmission is a fresh encryption (A7: 8 transmissions of ping#2, all nonces and ciphertexts distinct); sweep clean over every scenario | must hold for every MP1 retry (MP1-L12); the server answers same-seq retransmissions (MP1-L10 guard) | — | — | — | — | C.fl S.loop | MP0 PYTEST MP1 |
| D10 | — | D | unauthenticated traffic never liveness evidence | any | — | — | — | — | — | forged, plaintext, wrong-key, wrong-seq, unmatched ACK: no effect | unchanged; an ICMP-derived receive error is not evidence either way (A11b) | unchanged | — | — | — | — | C.fl C.pm | PYTEST |
<!-- matrix:end -->

## Existing project tests cited (PYTEST provenance)

Node IDs are relative to the repository root. They are cited, not cloned.

| ID | Tests |
|---|---|
| A1 | `tests/test_secure_udp_helpers.py::test_proxy_healthy_ping_pong_runs_past_old_refresh_interval`; `::test_proxy_matching_pong_before_deadline_schedules_next_normal_ping` |
| A5a | `tests/test_secure_udp_helpers.py::test_proxy_matching_pong_before_deadline_schedules_next_normal_ping` |
| A5b | `tests/test_secure_udp_helpers.py::test_proxy_matching_pong_ready_at_the_keepalive_deadline_is_credited_first` (MP1 evidence before verdict; before MP1 this test pinned the opposite ordering) |
| A8 | `tests/test_secure_udp_helpers.py::test_proxy_authenticated_nmea_is_not_server_liveness` (a server→client NMEA-typed message is not liveness) |
| A10 | `tests/test_udpsec_mobile_path_e2e.py::test_10_ack_with_captured_none_cannot_clear_maintenance_ping`; `tests/test_udpsec_client_path_migration.py::test_e2e_matched_ack_without_ping_clear_authority_still_reanchors_liveness` |
| B1 | `tests/test_udpsec_mobile_path_e2e.py::test_01_canonical_same_session_migration_a_to_b`; `tests/test_udpsec_path_migration.py::test_happy_path_migration_commits_and_preserves_session` |
| B2 | `tests/test_udpsec_mobile_path_e2e.py::test_06_lost_path_challenge` |
| B3 | `tests/test_udpsec_mobile_path_e2e.py::test_07_lost_path_response` |
| B4 | `tests/test_udpsec_mobile_path_e2e.py::test_08_lost_path_ack` |
| B6a | `tests/test_udpsec_mobile_path_e2e.py::test_12_migration_during_active_refresh_transaction`; `tests/test_udpsec_migration_lifecycle_races.py::test_01_candidate_created_then_refresh_begins_stays_e1_bound` |
| B6b | `tests/test_udpsec_mobile_path_e2e.py::test_13_stale_e1_candidate_then_fresh_e2_migration`; `tests/test_udpsec_path_migration.py::test_stale_e1_same_address_candidate_replaced_by_fresh_e2_incarnation`; `tests/test_udpsec_migration_lifecycle_races.py::test_03_epoch_swap_strands_outstanding_candidate_under_both_encodings` |
| B7 | `tests/test_udpsec_migration_lifecycle_races.py::test_04_fresh_e2_traffic_establishes_new_candidate_only_it_may_migrate`; `tests/test_udpsec_mobile_path_e2e.py::test_11_migration_completes_before_planned_refresh` |
| B8 | `tests/test_udpsec_mobile_path_e2e.py::test_05_rapid_a_to_b_to_c_mobility` (candidate replaced while unproved); `tests/test_udpsec_path_migration.py::test_retired_path_admits_late_nmea_without_reactivating`, `::test_candidate_churn_retains_at_most_one_candidate` |
| B9 | `tests/test_udpsec_path_migration.py::test_retired_path_expires_at_exact_grace_boundary_then_needs_fresh_cycle`, `::test_C_current_e2_crossing_retired_deadline_opens_fresh_reverse_candidate`; `tests/test_udpsec_migration_lifecycle_races.py::test_07_retired_grace_exact_equality_expires_needs_fresh_cycle` |
| B10 | `tests/test_udpsec_path_migration.py::test_response_from_wrong_source_address_does_not_commit`, `::test_newer_candidate_replaces_older_and_old_response_is_stale` |
| B11 | `tests/test_udpsec_security_validation.py::test_ipv6_remote_comparison_uses_ip_port_and_scope_from_four_tuple`; `tests/test_sockaddr_identity.py` |
| C4 | `tests/test_udpsec_security_validation.py::test_real_confirmed_same_address_rekey_replaces_traffic_keys`; `tests/test_secure_udp_helpers.py::test_d66_same_address_confirmed_rekey_preserves_then_replaces_active` |
| C5 | `tests/test_udpsec_security_validation.py::test_real_client_proactively_recovers_after_server_restart` (real sockets; MP1: recovery within peer_timeout plus a handshake, not before it) |
| D1 | `tests/test_secure_udp_helpers.py::test_unknown_locator_never_reaches_decrypt_regardless_of_correct_path`; `tests/test_udpsec_migration_security_hardening.py::test_a1_many_guessed_locators_create_no_retained_state`; `tests/test_udpsec_path_migration.py::test_guessed_locator_from_a_new_path_triggers_no_candidate` |
| D2 | `tests/test_secure_udp_helpers.py::test_secure_server_rejects_duplicate_data_nonce_after_first_valid_packet`, `::test_secure_server_rejects_verified_duplicate_handshake_replay`; `tests/test_udpsec_migration_security_hardening.py::test_a3_admitted_replay_fails_from_every_path_role`; `tests/test_udpsec_path_migration.py::test_replayed_nonce_from_a_new_path_creates_no_candidate`, `::test_byte_replay_of_path_response_does_not_recommit`; `tests/test_udpsec_migration_lifecycle_races.py::test_14_replay_across_active_candidate_retired_roles_gains_no_admission` |
| D3 | `tests/test_secure_udp_helpers.py::test_secure_server_source_mismatch_does_not_record_data_nonce_or_touch`; `tests/test_udpsec_path_migration.py::test_response_with_wrong_station_does_not_commit`; `tests/test_udpsec_client_path_migration.py::test_path_ack_with_wrong_station_id_is_ignored` |
| D4 | `tests/test_secure_udp_helpers.py::test_d66_data_dispatch_by_locator_has_no_cross_epoch_trial_decrypt`; `tests/test_udpsec_path_migration.py::test_retiring_epoch_traffic_from_a_new_path_cannot_open_a_candidate`; `tests/test_udpsec_client_path_migration.py::test_path_ack_under_pending_epoch_is_ignored`, `::test_path_ack_under_a_real_retiring_epoch_is_ignored` |
| D5 | `tests/test_secure_udp_helpers.py::test_secure_server_failed_decrypt_does_not_record_data_nonce`; `tests/test_udpsec_security_validation.py::test_real_listener_rejects_data_corpus_without_state_mutation`; `tests/test_udpsec_client_path_migration.py::test_path_ack_forged_under_wrong_key_fails_authentication`; `tests/test_udpsec_migration_security_hardening.py::test_a7_stale_token_generation_permutations_never_commit` |
| D6 | `tests/test_udpsec_migration_security_hardening.py::test_a9_cross_listener_locator_confusion_isolated`; `tests/test_secure_udp_helpers.py::test_endpoint_namespace_cross_listener_active_data_is_inert`; `tests/test_udpsec_security_validation.py::test_real_same_peer_is_isolated_across_physical_listeners`; `tests/test_udpsec_path_migration.py::test_response_for_another_listener_endpoint_token_does_not_commit` |
| D7 | `tests/test_udpsec_security_validation.py::test_real_confirmed_same_address_rekey_replaces_traffic_keys` |
| D8 | `tests/test_udpsec_mobile_path_e2e.py::test_03_replay_ledger_identity_and_continuity_across_migration` |
| D9 | `tests/test_secure_udp_helpers.py::test_proxy_encrypt_message_aes_gcm_uses_12_byte_nonce_and_locator_aad` (fresh random nonce per encryption); `::test_proxy_outstanding_ping_is_retransmitted_not_overwritten_until_peer_timeout` (MP1: same seq, distinct nonces) |
| D10 | `tests/test_secure_udp_helpers.py::test_proxy_accepts_only_authenticated_matching_pong_as_liveness`, `::test_proxy_ignores_forged_plaintext_no_session_from_configured_remote`, `::test_proxy_rejects_stale_future_reserved_or_negative_pong_sequence`, `::test_proxy_cleared_expectation_rejects_duplicate_matching_pong`, `::test_proxy_forward_loop_ignores_forged_no_session_until_liveness_bound`; `tests/test_udpsec_client_path_migration.py::test_challenge_alone_gives_no_liveness_credit`; `tests/test_udpsec_migration_security_hardening.py::test_j_adversarial_ack_cannot_grant_liveness_or_clear_unrelated_ping` |

Wire version and DATA prefix (MP1-L10, R7) are pinned by
`tests/test_udpsec_protocol.py::test_protocol_api_and_wire_prefixes_are_public`,
`::test_client_hello_and_server_hello_default_to_protocol_version_2` and
`::test_data_prefix_is_the_v2_prefix_and_distinct_from_v1`.

MP1 mechanisms that no lab scenario isolates are unit-tested in
`tests/udpsec_recovery/test_mp1_liveness_units.py`: the pure liveness
helpers, the pre-verdict drain bound under a datagram flood, evidence that
arrived while the process was held up outside `select()`, the transient
errno classification and input pause, and forwarded-sentence counting.

## Counts

| Class | Rows | Lab scenarios | MP1 acceptance tests | MP1 policy pins | PLANNED | Cited project tests only |
|---|---|---|---|---|---|---|
| A (incl. A/B, A/C) | 19 (A1-A11 incl. A4b, A5a-e, A9b, A10b, A11b) | 18 (+ A8 via A3) | 16 (L1-L8b, L11a, L11b, L12) | A2-A7, A9-A11b | 0 | 0 |
| B | 12 (B1-B11 incl. B2b, B6a/b) | 11 | 0 | pre-MP2 pins only | 3 (B2, B2b, B3: MP2 latency) | 1 (B9) |
| C | 6 | 5 (+ C4 via C3) | 0 (C3, C3b are MP1-L9 guards) | C1, C3, C3b, C5 | 0 | 0 |
| D | 10 | 0 (D7 via C3, D8 via B1, D9 sweep) | 1 (L12 via A7) | — | 0 | 7 |

Field observations map to A2-A4 and A7 (IPv6 and IPv4 same-tuple
re-establishments, cause not identifiable) and to C1/C2 (the Wi-Fi-loss
experiment).
