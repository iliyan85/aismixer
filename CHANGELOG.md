# Changelog

Notable AISMixer changes are documented in this file. The project uses
Semantic Versioning during active pre-1.0 development; pre-1.0 releases may
still change public APIs and configuration behavior as the service matures.

## [Unreleased]

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

[Unreleased]: https://github.com/iliyan85/aismixer/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/iliyan85/aismixer/releases/tag/v0.1.0
