# UDPSEC V2 Recovery Evidence Ledger (MP0)

Purpose: let every future independent gate (Astra Gate A and Gate B,
baseline section 11) start from classified evidence instead of re-auditing
the protocol from scratch. Baseline commit `de513674fabf2ee594d0f08f10ddbdf87267a352`.

Evidence classes (as required by MP0):

| Class | Meaning |
|---|---|
| CURRENT CODE INSPECTION | Read in production source at the baseline commit during MP0 (line references given). |
| EXISTING PROJECT TEST | A pre-MP0 test in `tests/` asserts it; it passes at the baseline commit (MP0 run, section 3). |
| FABLE INDEPENDENT HARNESS | Verified by the independent Fable audit, 2026-09-28: code reading (Fable "[A]") and/or its harness run. Fable did NOT run the project suite. |
| PRIOR INDEPENDENT ASTRA EVIDENCE | Reported by the earlier independent Astra review; carried forward, not re-run. |
| FIELD OBSERVATION | Operator-reported road/field data. |
| IMPLEMENTER-REPORTED ONLY | Produced by the implementer (including every MP0 test run), not independently re-run. |
| NOT YET VERIFIED | Nobody has established it. |

A claim can hold several classes. "Re-verify when" names the change that
voids the carried-forward evidence.

## 1. Current liveness behaviour

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

## 2. Current migration behaviour

| ID | Claim | Classes | Pointer | Re-verify when |
|---|---|---|---|---|
| E-M1 | A commit preserves the `LogicalSession`, `CryptoEpoch`, replay ledger and `assembly_namespace` objects. | CODE; PROJECT TEST (e2e `test_01`, `test_03`); FABLE (T08: object ids); IMPL (MP0 B1 via weak references) | `commit_candidate_path` `3700-3869`; contract 11.3 items 1-4 | server migration code changes |
| E-M2 | Exactly one PATH_CHALLENGE per candidate incarnation; a 10 s TTL, never extended. | CODE; PROJECT TEST (e2e `test_15` counters); FABLE (T09a/b); IMPL (MP0 pre-MP2 pins) | `4424-4477`, `:127` | MP2 (expected to change) |
| E-M3 | A lost CHALLENGE or RESPONSE is recovered by candidate expiry and re-open (+10.1 s); a lost ACK by the next PONG; no rekey in any case. | PROJECT TEST (e2e `test_06/07/08`); FABLE (T09a/a7/b/c); IMPL (MP0 B2-B4) | contract 11.3 item 5 | MP2 |
| E-M4 | A ping from an unproved or retired path gets no PONG. | CODE; PROJECT TEST (`test_candidate_nmea_shares_assembly_namespace_and_gets_no_pong`); FABLE | `aismixer_secure.py:5085-5106` | server ping branch changes |
| E-M5 | Flap, rapid A→B→C, stale-epoch candidate, refresh × migration ordering and canonical sockaddr all keep one session with no rekey. | PROJECT TEST (matrix B6-B11); FABLE (T10a/b, T11a/b/c, T12); IMPL (MP0 B-invariants) | matrix B rows | server migration or refresh changes |

## 3. Security invariants (must not regress)

| ID | Claim | Classes | Pointer | Re-verify when |
|---|---|---|---|---|
| E-S1 | Transcript binding (version, locator, both ECDHE keys, both randoms, both signatures into the HKDF salt). | CODE (Fable-cited lines); PROJECT TEST (`tests/test_udpsec_crypto.py`); FABLE [A]. Not re-read line-by-line in MP0. | `core/udpsec_crypto.py:474-857` | any crypto or transcript change |
| E-S2 | Per-epoch replay ownership; migration never resets a ledger; nonce admission is authoritative and locked. | PROJECT TEST (D2, D8); FABLE (T08, T13-b/c); IMPL (MP0 B1, D9 sweep) | matrix D2, D8 | ledger or admission changes |
| E-S3 | Path-authority gating (unproved and retired paths carry inbound data only; commit needs exact proof). | PROJECT TEST (hardening a2/a5/a7, races); FABLE (T13-d/e/f, T10a/b, T11c) | matrix B, D5 | migration changes |
| E-S4 | No reply or amplification for an unknown locator; replays refused before any candidate. | PROJECT TEST (D1, D2); FABLE (T13-a/c); IMPL (MP0 C4, C5) | matrix D1, D2 | server lookup changes |
| E-S5 | Wrong-station, wrong-selector/epoch, forged, cross-listener and old-session ciphertext are all rejected. | PROJECT TEST (D3-D7); FABLE (T13-g/i/e/f/h/j); IMPL (MP0 C4) | matrix D3-D7 | admission or epoch changes |
| E-S6 | No AEAD nonce reuse per (direction, locator, epoch) in any MP0 scenario; random 96-bit nonce per encryption. | PROJECT TEST (`test_proxy_encrypt_message_aes_gcm_uses_12_byte_nonce_and_locator_aad`); IMPL (MP0 D9 sweep) | `harness/lab.py` `nonce_reuse()` | every MP1 retry change (Gate A) |
| E-S7 | Unauthenticated input never becomes liveness evidence. | PROJECT TEST (D10); FABLE [A] | matrix D10 | MP1 (Gate A) |
| E-S8 | No BLOCKER or HIGH security finding in the reviewed UDPSEC V2. LOW residuals: a ClientHello costs one ECDSA verify before any per-source budget (F7); the ±30 s wall-clock window is a bring-up hazard (F6). | FABLE (sections 6-7). Not repaired. | `aismixer_secure.py:4548-4600, 4554` | handshake path changes |
| E-S9 | Diagnostics are observational only. | CODE; PROJECT TEST (`tests/test_udpsec_field_diagnostics.py`); FABLE (F9); PRIOR INDEPENDENT ASTRA EVIDENCE (diagnostics delta: no confirmed new blocker) | contract 11.4 | diagnostics changes |

## 4. Field observations

| ID | Claim | Classes | Pointer |
|---|---|---|---|
| E-F1 | IPv6 run: re-establishment after "liveness unresolved"; the old and new labels differ; the new session is `epoch 0`, `path_gen 0`; the same IPv6 address and port 46770 are seen before and after. | FIELD OBSERVATION (screenshot sha256 `4ad084221bf93a08bd106a4761303b2127af86d06e940344028c6d3a0ac925b6`); interpretation FABLE (section 4) | baseline 4.1 |
| E-F2 | IPv4/CGNAT run: the public tuple was stable over long intervals (`90.154.211.195:54654`); re-establishments still occurred; `path_gen` stayed 0. | FIELD OBSERVATION (operator report in the MP0 instruction; no logs supplied) | baseline 4.2 |
| E-F3 | Wi-Fi-loss experiment: `Errno 101` handshake send errors every 5 s; afterwards a fresh session with the same public IPv4 and CGNAT port 54654→54104. | FIELD OBSERVATION; mechanism reproduced as IMPL (MP0 C1/C2) | baseline 4.3 |
| E-F4 | Which packet was lost, and for how long, in any field event; server-side NMEA loss in the field. | NOT YET VERIFIED (not identifiable from the data) | Fable 5.2, worksheet section 11 |

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

If the MP1 delta touches only `nmea_sproxy/nmea_sproxy.py` (the liveness
state and the deadline helpers in `forward_loop`), its config validation, and
the section 9 tests, then E-M1..E-M5, E-S1, E-S3, E-S4, E-S5 (server side),
E-S8 and E-S9 carry forward without re-audit. Gate A re-verifies E-L*,
E-S2, E-S6 and E-S7 against the delta. It also re-verifies the R4 × MP5
equality change (baseline 7.1) and reruns this directory plus the section 9
test files.
