# AISMixer Roadmap

This roadmap describes the current implemented baseline and the next practical
development tracks for AISMixer. It is status-oriented, not a release log, and
does not promise dates.

## Purpose And Scope

AISMixer is a production-oriented Python service for receiving, normalizing,
deduplicating, tagging, routing, and forwarding AIS NMEA 0183 streams.

The current scope is a reliable mixer with UDP and UDPSEC ingress, deterministic
NMEA/TAG handling, logical routing, and a local operator control plane. Future
work should keep the data plane and control plane separate, preserve legacy
broadcast compatibility where intended, and avoid large rewrites when staged
migration is practical.

## Implemented Baseline

The following capabilities are implemented in the current codebase:

- Plain UDP ingress.
- Authenticated encrypted UDPSEC ingress through `nmea_sproxy`.
- `!AIVDM` and `!AIVDO` extraction from realistic input.
- Multipart AIVDM/AIVDO assembly.
- NMEA TAG `s`/`c`/`g` handling.
- Legacy global deduplication and broadcast forwarding.
- Internal `source_id` and `target_id` routing identities.
- Named UDP egress targets.
- Logical zones with `include`, `union`, `intersection`, and `difference`.
- Static routing loaded from configuration.
- Target-scoped deduplication in routing mode.
- Immutable routing snapshots.
- Process-local routing generations.
- Runtime routing status, replacement, and disable operations.
- Versioned JSON routing-control protocol.
- Opt-in POSIX Unix-domain control server.
- `aismixerctl` local operator CLI.

These items should not be described as planned functionality in repository
metadata or user-facing documentation.

## Priority Development Track

### 1. Operational Deployment Hardening

- Add systemd `RuntimeDirectory` or equivalent control-socket directory
  provisioning.
- Define explicit service ownership and group access for the control socket.
- Provide a globally installed `aismixerctl` command during installation.
- Preserve an existing operator configuration during install and update flows.
- Verify Linux and Raspberry Pi operational behavior for installer, service,
  UDPSEC, and control-socket deployments.

### 2. Process Architecture

- Introduce a coordinator process and dedicated ingress and egress workers.
- Define process lifecycle supervision and failure handling.
- Add IPC for routing snapshot distribution between processes.
- Use explicit egress-worker terminology for forwarding workers.
- Migrate in stages rather than as a single large rewrite.

### 3. Routing-State Operations

- Consider optional persistence or controlled restoration of runtime routing
  state.
- Add safe configuration reload or watch behavior.
- Keep rollback history for recent routing snapshots.
- Improve operational observability around active routes, targets,
  generations, and control operations.

### 4. Maritime Security And Data-Quality Research

- Research AIS spoof and anomaly detection.
- Surface receiver and feed quality signals.
- Explore deduplication feedback from edge nodes.
- Support maritime-domain-awareness data pipelines.

AIS spoof detection is a priority planned capability. It is not implemented in
the current AISMixer data plane or control plane.

## Later Expansion

The following ideas are not currently implemented and should remain clearly
marked as future work:

- Additional egress adapters such as MQTT, AMQP, HTTP, or database sinks.
- Remote authenticated control transports.
- Peer-to-peer routing exchange.
- Dynamic ingress and egress adapter lifecycle management.
- Geographic, MMSI, vessel, or payload-aware filtering.
- Richer monitoring, metrics, and health reporting.

## Non-Goals For The Current Phase

- Do not replace the existing runtime with a full multi-process architecture in
  one step.
- Do not make runtime routing persistent without an explicit operator model.
- Do not imply that UDP source IP or TAG metadata is cryptographic identity.
- Do not treat `source_id` as the emitted NMEA TAG `s` value.
- Do not add remote control transports before the local POSIX control plane is
  hardened.
- Do not describe spoof detection, geographic filtering, or non-UDP egress as
  available features until they are implemented and tested.
