# Changelog

Notable AISMixer changes are documented in this file. The project uses
Semantic Versioning during active pre-1.0 development; pre-1.0 releases may
still change public APIs and configuration behavior as the service matures.

## [Unreleased]

## [0.2.1] - 2026-09-04

### Highlights

- Closes a security and correctness hardening pass across UDPSEC session
  lifecycle, ingress error containment, processing/state bookkeeping, runtime
  observability, configuration validation, the local control-socket
  lifecycle, and key-material handling. This remains a pre-1.0 release
  without a stable public API or configuration compatibility guarantee.

### UDPSEC hardening

- Builds on the 0.2.0 authenticated ephemeral P-256 ECDHE handshake — signed
  transcript-bound digests, HKDF-SHA256-derived directional AES-256-GCM
  traffic keys, and an encrypted key-possession confirmation — with a
  hardened session lifecycle.
- Isolates pending and active sessions by physical listener socket
  incarnation plus raw peer address, so same-peer state can no longer be
  selected or replaced across listeners sharing one secure-state owner.
- Retains every admitted receiver-side DATA nonce for its full traffic-key
  epoch instead of expiring or evicting live nonce records. At the hard
  per-epoch bound, the next distinct valid nonce fails closed by
  invalidating only that one epoch and dropping the packet; recovery is
  through the existing authenticated ECDHE re-handshake, with no
  wire-protocol change. Adds `data_nonce_exhaustions` secure-state
  accounting; the legacy `data_nonces_expired` and
  `data_nonces_capacity_evicted` snapshot fields remain for compatibility
  and stay zero.
- Adds authenticated, encrypted keepalive liveness: sequence `0` is reserved
  for the encrypted confirmation ping/pong, and every later ping/pong
  requires an exact matching sequence from the pinned remote tuple, under
  the live session key; no pong is accepted with no ping outstanding. An
  unanswered ping at the next keepalive deadline ends the local forwarding
  loop with a proactive-rekey reason and immediately starts one fresh signed
  ECDHE handshake.
- Adds an authenticated, encrypted best-effort graceful close sent under the
  session's own key, replacing the previous unauthenticated plaintext
  `NOSESSION` notice. UDPSEC now has no plaintext session-reset, downgrade,
  or fallback path of any kind: a plaintext, malformed, or otherwise
  unauthenticated datagram can never touch, promote, or delete a live
  session, and every recovery path is fail-closed through a fresh
  authenticated handshake.

### Ingress robustness

- Contains recoverable per-peer `ConnectionResetError` /
  `ConnectionRefusedError` receive conditions (for example a delayed ICMP
  port-unreachable response surfacing on a later receive) on both the plain
  UDP and UDPSEC listeners: the condition is logged, produces no
  `IngressFrame`, and the listener keeps awaiting the next datagram instead
  of terminating the runtime. Any other `OSError` still propagates to
  runtime supervision, and cancellation still propagates unchanged.
- Plain UDP ingress now reads one full datagram per receive at a buffer size
  that does not truncate ordinary IPv4/IPv6 UDP payloads at the application
  boundary, replacing the previous 8192-byte application receive bound.

### Processing and state correctness

- Redesigns `TTLMap` expiry bookkeeping onto one ordered structure that also
  serves as the expiry queue, removing the separate expiry-record queue that
  previously grew by one stale record on every refresh of an existing key.
- Expands `nmea_sproxy`'s accepted AIS talker whitelist from `AI`-only to
  the same closed set AISMixer core supports: `AI`, `AB`, `AD`, `AN`, `AR`,
  `AS`, `AT`, `AX`, and `BS`.
- Closes regression coverage confirming multipart TAG `s` handling: an
  exact-duplicate fragment arrival may update the cached `s`, a completing
  arrival that carries no `s` of its own falls back to that cached value,
  and the cached value does not leak into a later, unrelated completion
  that reuses the same assembly key.

### Runtime and operator behavior

- The shipped example and packaged configuration now default `debug: false`
  instead of `true`, so a fresh deployment no longer defaults to
  high-frequency, traffic-proportional debug logging.
- Adds a sparse, debug-independent runtime statistics heartbeat to both
  AISMixer and `nmea_sproxy`, supervised as an essential task alongside the
  existing ingress/processing/egress stages.
- Hardens configuration validation: `g_id_digits` must now be a plain
  integer in `1..32`, checked before any step with a persistent side effect
  (including UDPSEC server key generation); `control.unix.socket_mode` now
  requires an unambiguous canonical four-character octal string
  (`"0000"`-`"0777"`) instead of a looser, radix-ambiguous form; and a
  boolean `listen_port` is rejected instead of being silently coerced.
- Hardens the local Unix-domain control-socket lifecycle: the socket node
  is created under a mode-derived umask so it is never briefly more
  permissive than its configured mode; a pre-existing path at the socket
  location is now actively probed and only removed once confirmed stale —
  a live socket refuses replacement instead of being displaced — with an
  identity re-check immediately before removal to close the
  replace-on-startup race; and a partial request frame left at connection
  EOF is discarded instead of being dispatched.

### Forwarding and key tooling

- `Forwarder` now detects a cached UDP transport that has started closing
  and transparently recreates it, instead of silently dropping sends
  through a closing transport.
- Hardens `--force` PEM key-pair replacement used by the canonical
  `tools/aismixer_keys.py` utility: both the private and public PEM are
  staged as complete temporary files beside their destinations, then
  replaced individually with `os.replace`; an existing key file is never
  truncated in place. Replacement across the pair is sequential, not one
  atomic operation: a failure between the two individual replacements can
  leave a new private key beside an old public key, a mismatch that
  identity validation rejects and that `--repair-public` resolves.
- `nmea_sproxy/station_keys_gen.py` now prints an explicit deprecation
  notice naming its replacement on every invocation. The canonical key tool
  for both server and station identities remains `tools/aismixer_keys.py`.
- AISMixer's UDPSEC server identity is now prepared through a shared
  identity service and only when `sec_inputs` actually configures secure
  ingress, instead of unconditionally. `nmea_sproxy` station identity
  preparation is now demand-driven at runtime instead of eager at
  systemd-installer time; the installer no longer generates or repairs
  station keys itself.

### Compatibility and operator impact

- No configuration keys were removed or renamed. The only shipped default
  that changed is `debug` (`true` → `false`) in the example and packaged
  configuration; an operator's existing explicit `debug: true` is
  unaffected.
- The legacy top-level `nmea_sproxy` configuration form (predating the
  `input:`/`output:` mapping already canonical since 0.2.0) remains
  accepted, now emits an explicit runtime deprecation notice, and the
  shipped/example templates switched to the explicit mapping as their
  primary form.
- The UDPSEC control plane no longer sends or expects the plaintext
  `NOSESSION` notice; graceful close is now an authenticated encrypted
  message under an established session key. This is additive to the 0.2.0
  ECDHE handshake, not a handshake-format change.

## [0.2.0] - 2026-08-09

### Highlights

- Delivers a native-ready Python data plane, worker-readiness foundations, and
  process-local runtime observability while retaining a single-process runtime.
- Adds an authenticated ephemeral-ECDH UDPSEC handshake and expands
  `nmea_sproxy` with physical serial input and explicit plain-UDP output.
- Hardens network endpoint controls and systemd deployment for production
  operation. This remains a pre-1.0 release without a stable public API or
  configuration compatibility guarantee.

### Data Plane and Processing

- Introduces a normalized, immutable bytes-based ingress representation, a
  byte-span NMEA scanner, and frozen parsed-sentence values that carry
  parse-once fragment and TAG metadata into downstream processing.
- Establishes immutable `ProcessingSnapshot` / work-item handoff and a
  synchronous `DataPlaneProcessor` boundary. `PythonDataPlaneProcessor` is the
  sole production and reference implementation; no native processor exists
  yet.
- Separates ingress adaptation, processing, and egress orchestration while
  retaining one long-lived processor instance as the owner of assembler,
  deduplication, source, multipart-metadata, and processor-metric state.
- Improves multipart processing with a single-sentence assembler fast path that
  allocates no multipart group, indexed fully out-of-order assembly,
  deterministic duplicate/conflict and expiry handling, group-atomic
  deduplication, and corrected `!AIVDO` forwarding.
- Replaces full deduplication expiry sweeps with incremental expiry and adds
  optional deduplication and assembler capacities, reset boundaries, and
  lifecycle statistics. The production processor leaves those optional
  capacities unset by default.

### Routing and Egress

- Compiles string-named destinations and routes to dense, zero-based numeric
  egress target IDs for the internal hot path. Routing configuration and
  status/mutation remain string-name-facing; output statistics also expose the
  process-local numeric IDs.
- Builds each emitted sentence as exact immutable bytes once, then reuses that
  payload across destinations through immutable `ProcessorOutput` values and
  ordered `OutputBatch` results.
- Dispatches production egress through numeric targets with an ordered local
  completion barrier. Completion means that the local send returned, not that
  a remote UDP consumer received or processed the message.

### Worker Readiness and Runtime Observability

- Adds bounded process-local queues for each ingress, shared processing
  admission, and egress handoff. Full stages wait and apply backpressure;
  AISMixer has no stage-level drop-on-full branch, but UDP itself remains
  lossy and queued work is not durable.
- Binds one immutable processing/routing snapshot after processing capacity is
  obtained, so admitted work retains its generation and target tuple while
  still-waiting frames may observe a later routing replacement.
- Adds a synchronous, ordered processor reset contract that retains
  configuration and cumulative metrics. It is a lifecycle boundary, not a
  current control-protocol or `aismixerctl` command.
- Adds fail-fast supervision for UDP/UDPSEC producers, fan-in, processing, and
  egress tasks. These are stages in one process, not coordinator-managed worker
  processes.
- Adds immutable pull-based statistics for ingress, processing, and egress
  queues; processor calls and outputs; local egress operations; per-input
  traffic; and per-target output traffic.
- Separates raw input transport packets/bytes from frames and payload bytes
  accepted after queue admission. Per-target completion/messages/bytes count
  successful local dispatch calls, not acknowledged UDP delivery.
- Statistics are fresh sequential process-local snapshots of current gauges
  and lifetime counters. They are not a globally atomic view, persistent
  history, time-series export, or distributed aggregation, and restart resets
  them.

### Runtime Control and `aismixerctl`

- Extends control protocol v1 with the read-only `runtime.statistics`,
  `runtime.statistics.inputs`, and `runtime.statistics.outputs` methods; the
  aggregate method covers stage/processor/egress-operation data, while the
  detailed methods expose input and output traffic.
- Makes no-command `aismixerctl` an interactive shell while retaining one-shot
  JSON operation. Routing `status`, `replace`, and `disable`, plus aggregate,
  per-input, and per-output `show statistics` views, share command behavior.
- Adds shell help, `exit` / `quit`, quoting, clean EOF and Ctrl+C handling, and
  optional command history, line editing, and basic completion; interactive
  statistics are rendered as tables.
- Keeps routing mutation process-local and non-persistent: restart restores
  routing from the active configuration file.

### UDPSEC and `nmea_sproxy`

- Replaces static identity-key ECDH with authenticated ephemeral P-256 ECDHE.
  Long-term P-256 ECDSA identity keys authenticate transcript-bound digests;
  HKDF-SHA256 derives separate client-to-server and server-to-client
  AES-256-GCM traffic keys from the ephemeral shared secret and authenticated
  transcript.
- Adds encrypted key-possession confirmation and strict handshake/control
  validation. Handshake-replay, pending-session, and active-session state is
  explicitly owned, bounded, and TTL-managed; per-session DATA nonce state is
  separately bounded, with lifecycle statistics internal to the secure-state
  owner.
- This provides forward-secrecy properties against later identity-key
  compromise only when past ephemeral secrets have been discarded and neither
  endpoint was compromised while those secrets were live. The protocol has not
  been formally verified and does not make UDP reliable or AIS data
  semantically authentic.
- Extends `nmea_sproxy` with explicit physical serial / USB virtual-COM input
  and explicit plain-UDP output for trusted LAN/VPN use. Plain UDP has no
  encryption, identity authentication, integrity, replay, or liveness
  guarantees, and there is no automatic UDPSEC-to-UDP fallback.
- Retains one input-to-one output relation per proxy process/systemd instance,
  singleton and template-instance deployment, CLI/environment/system config
  resolution, relative key-path handling, identity-key preservation/repair,
  and legacy key-path aliases. Omitting `input:` retains the backward-compatible
  top-level UDP input form; omitting `output:` retains the legacy UDPSEC output.

### Networking and Deployment

- Adds literal-IP/CIDR application-level ingress ACLs and outbound
  source-address binding to AISMixer, with corresponding local-UDP ACL and
  UDPSEC/plain-UDP source-binding controls in `nmea_sproxy`. These controls
  complement rather than replace firewall and routing policy.
- Makes AISMixer IPv4 and IPv6 listeners explicitly single-family; dual-stack
  operation uses separate IPv4 and IPv6 listener entries, which may share a
  port.
- Makes both lifecycle suites privilege-aware for direct-root or `sudo`
  operation. AISMixer install/update now preflight their required source
  layouts, and installation preserves existing config files rather than
  overwriting them; existing key-preservation and incomplete-keypair safeguards
  remain.
- Adds systemd-managed `/run/aismixer` through `RuntimeDirectory=aismixer` and
  installs `aismixerctl` globally as `/usr/local/bin/aismixerctl`.
- Current service semantics are deliberate: the AISMixer installer enables but
  does not start the service, and its updater reloads systemd and restarts the
  service. The proxy installer enables only the singleton and starts nothing;
  its updater reloads systemd but does not restart any proxy instance.
- Uninstallers preserve configuration and keys by default; their explicit
  `--purge-config` option removes that retained operator state.

### Compatibility and Operator Impact

- Without top-level `routing:`, legacy global deduplication and broadcast to
  every forwarder remain active, and unnamed forwarders remain valid. Enabled
  routing still requires named string targets.
- Numeric target IDs are internal declaration-order positions, not durable
  configuration identities; runtime routing remains process-local,
  non-persistent, and restored from configuration on restart.
- Install/update workflows preserve existing AISMixer and `nmea_sproxy`
  configuration, identity keys, trust files, and authorization entries.
- The v0.2.0 UDPSEC ECDHE wire handshake is not compatible with the v0.1.0
  handshake and has no downgrade path. Upgrade AISMixer and `nmea_sproxy`
  together in one maintenance window, then restart every running proxy process
  (the singleton, selected template instances, or a manual process) because the
  proxy updater intentionally does not restart them. Existing P-256 identity
  keys and supported legacy configuration/key-path aliases remain usable.
- Pre-1.0 internal bytes-facing processor and forwarder contracts have evolved;
  this release does not promise absolute API or configuration compatibility.

### Documentation

- Consolidates the root README into a concise operator overview in English,
  Bulgarian, and Romanian, and expands the dedicated `nmea_sproxy` operator
  guide.
- Adds the normative behavioural contract through the completed worker-
  readiness foundation and updates the roadmap and focused examples for
  current routing, control, statistics, endpoint, and deployment behavior.
- GitHub Wiki and public-website updates are accompanying release work outside
  the `main`-branch tag; this entry does not claim that work is complete.

### Known Limitations

- AISMixer egress is UDP-only and provides no delivery guarantee; lost payloads
  are not replayed. UDPSEC sessions are process-local, non-durable, and do not
  migrate across client address/port changes.
- There is no coordinator, separate ingress/egress worker process,
  multiprocessing, IPC, cross-process routing synchronization, distributed
  metrics aggregation, or automatic worker recovery/replay.
- Runtime routing is process-local and non-persistent, and configuration is not
  reloaded automatically.
- Local control uses a POSIX Unix-domain socket with filesystem permissions as
  its authorization boundary; there is no application-level token or remote
  HTTP/TCP control transport.
- There is no native processor or binding, Prometheus exporter, persistent
  metrics history, geographic/MMSI/vessel-content filtering, spoof detection,
  or long-term storage/analytics.
- Optional deduplication and multipart-assembly capacity limits remain unset in
  the production processor by default.

## [0.1.0] - 2026-07-06

### Highlights

- First versioned AISMixer baseline.
- First release that formally documents the routing and runtime-control
  architecture.
- Pre-1.0 release without a stable public API or configuration compatibility
  guarantee.

### Data Plane

- Supports plain UDP ingress over IPv4 and IPv6.
- Supports authenticated encrypted UDPSEC ingress.
- Extracts `!AIVDM` and `!AIVDO` sentences from incoming data.
- Assembles multipart AIS messages using ingress assembler identity and NMEA
  fragment fields.
- Handles NMEA TAG `s`/`c`/`g` metadata according to runtime configuration.
- Preserves legacy global deduplication behavior when routing is disabled.
- Preserves legacy broadcast UDP forwarding to all configured forwarders.
- Provides UDP-only egress in this baseline.

### Logical Routing

- Introduces internal `source_id` and `target_id` identities for routing.
- Supports named UDP egress targets.
- Supports logical zones using `include`, `union`, `intersection`, and
  `difference`.
- Loads static routing from configuration at startup.
- Applies target-scoped deduplication in routing mode.
- Captures one immutable routing snapshot per `IngressEvent`.
- Treats logical zones as source-ID sets, not geographic regions.

### Runtime Control Plane

- Adds process-local `RoutingState` generations.
- Supports atomic routing snapshot replacement.
- Implements `routing.status`, `routing.replace`, and `routing.disable`.
- Defines versioned JSON routing-control protocol v1.
- Provides an opt-in POSIX Unix-domain NDJSON control transport.
- Adds the `aismixerctl` local operator CLI.
- Uses `expected_generation` to reject stale updates.
- Keeps `control.unix` disabled unless explicitly enabled.
- Keeps runtime routing changes non-persistent; restart restores routing from
  the active configuration file.
- Treats `expected_generation` as concurrency control, not authorization.

### UDPSEC and nmea_sproxy

- Documents UDPSEC as AISMixer's authenticated encrypted
  station-to-mixer UDP transport.
- Documents `nmea_sproxy` as one local UDP input mapped to one AISMixer UDPSEC
  input.
- Uses ECDSA station/server authentication.
- Protects session traffic with AES-GCM.
- Supports encrypted ping/pong liveness traffic.
- Handles NAT, CGNAT, and mobile-client recovery cases with reconnect and
  session recovery behavior.
- Preserves the legacy `aismixer_public_key` compatibility alias.
- Preserves the legacy `station_private.key` fallback where currently
  supported.
- UDP remains lossy, and UDPSEC does not prove the semantic truth of AIS
  payloads.

### Compatibility

- Without a `routing:` section, existing global-deduplication and broadcast
  behavior remains active.
- Unnamed UDP forwarders remain valid in legacy mode.
- Routing targets require named forwarders.
- `control.unix` remains disabled unless explicitly enabled.
- Runtime control does not modify `config.yaml`.
- This release does not provide an absolute backward-compatibility guarantee.

### Security and Trust Boundaries

- Plain UDP is unauthenticated and unencrypted.
- UDPSEC authenticates configured station identities and encrypts transport.
- Emitted TAG `s` is not the internal routing identity.
- Unix socket filesystem ownership, group, and mode are the current
  authorization boundary for runtime control.
- No application-level control token exists.
- Spoof or anomaly detection is not implemented.
- See [SECURITY.md](SECURITY.md) for the full security policy.

### Operations and Deployment

- Supports direct repository execution and existing systemd installation paths.
- Leaves control socket parent-directory provisioning operator-managed.
- Does not yet automatically provision `/run/aismixer` through installer or
  systemd integration.
- Does not yet install `aismixerctl` as a global command.
- Requires Linux, WSL, Raspberry Pi OS, or another compatible POSIX environment
  for real Unix-domain control operation.

### Documentation

- Establishes a coordinated documentation baseline across the bilingual
  [README](README.md), routing and runtime-control
  [examples](examples/README.md), [roadmap](ROADMAP.md),
  [security policy](SECURITY.md), and [contribution guide](CONTRIBUTING.md).
- Notes accompanying documentation updates in the comprehensive
  [GitHub Wiki](https://github.com/iliyan85/aismixer/wiki) and bilingual
  [public website](https://aismixer.net/).
- Website and Wiki updates are accompanying documentation; they are not commits
  contained in the main-branch tag.

### Known Limitations

- No formally stable API or configuration compatibility guarantee.
- UDP-only egress.
- Process-local, non-persistent runtime routing.
- POSIX-only Unix control transport.
- No automatic config reload or watch behavior.
- No multiprocessing coordinator or IPC.
- No dynamic adapter lifecycle.
- No remote HTTP or TCP control.
- No application-level control authentication.
- No automatic control `RuntimeDirectory` provisioning.
- No globally installed `aismixerctl`.
- No CI workflow.
- No package distribution.
- No geographic, MMSI, vessel, or payload filtering.
- No spoof detection.
- No long-term storage or analytics.
- No operational maritime-picture generation.

[Unreleased]: https://github.com/iliyan85/aismixer/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/iliyan85/aismixer/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/iliyan85/aismixer/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/iliyan85/aismixer/releases/tag/v0.1.0
