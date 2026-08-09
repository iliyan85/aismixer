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

The current codebase includes the following completed Python/native-ready
foundation. "Native-ready" describes explicit contracts and separable
boundaries; it does not mean that a native implementation or bindings exist:

- A consolidated behavioural contract with explicit state ownership, ordering,
  failure, and delivery guarantees and limitations.
- Immutable `IngressFrame` values and a bytes-native parsed ingress
  representation.
- An explicit synchronous `DataPlaneProcessor` boundary with immutable
  snapshot and output values, with `PythonDataPlaneProcessor` as the sole
  production reference implementation.
- Processor-instance ownership of mutable assembler, deduplication, source,
  and multipart metadata state, plus a synchronous ordered reset boundary.
- Explicit single-process ingress fan-in, processor, and egress stages with
  process-local fail-fast supervision.
- Private bounded ingress queues, bounded processing admission, and bounded
  egress handoff with explicit process-local backpressure.
- Admission-time binding of each `IngressFrame` and `ProcessingSnapshot` into
  one immutable work item after processing capacity becomes available.
- An ordered completion barrier that prevents processor work on a later frame
  from running ahead of the current non-empty batch's egress dispatch.
- Fresh immutable, pull-based snapshots for queue, processor, egress-operation,
  input-traffic, and output-target metrics, with process-local ownership.
- An immutable dense numeric egress target registry covering every configured
  destination, including unnamed legacy destinations, while configuration and
  control target names remain string-facing.
- Immutable routing snapshots with process-local generations and compiled
  numeric target-only matching in the production path.
- Exact immutable bytes at the processor-output boundary, with TAG formatting
  delegated to the canonical writer and one UTF-8 encoding per emitted
  sentence.
- Ordered `OutputBatch` processor results, explicit numeric targets on every
  `ProcessorOutput`, and unified numeric production egress through
  `send_to_ids()`.

The following service and operational capabilities are also implemented:

- Plain UDP ingress.
- Authenticated encrypted UDPSEC ingress through `nmea_sproxy`.
- `!AIVDM` and `!AIVDO` extraction from realistic input.
- Multipart AIVDM/AIVDO assembly.
- NMEA TAG `s`/`c`/`g` handling.
- Legacy global deduplication and broadcast forwarding.
- Named UDP egress targets.
- Logical zones with `include`, `union`, `intersection`, and `difference`.
- Static routing loaded from configuration.
- Target-scoped deduplication in routing mode.
- Runtime routing status, replacement, and disable operations.
- Versioned JSON routing-control protocol with read-only aggregate, per-input,
  and per-output runtime statistics.
- Opt-in POSIX Unix-domain control server.
- `aismixerctl` one-shot CLI and interactive operator shell for routing and
  runtime statistics.
- Repository-managed systemd unit with `RuntimeDirectory=aismixer`.
- Globally installed `/usr/local/bin/aismixerctl` wrapper in lifecycle scripts.
- Install and update flows preserve existing operator configuration and keys.

These items should not be described as planned functionality in repository
metadata or user-facing documentation.

## Priority Development Track

### 1. Operational Deployment Hardening

Completed deployment baseline:

- systemd `RuntimeDirectory=aismixer` provisions `/run/aismixer` while the
  installed service is running.
- Installation and update deploy the global `aismixerctl` wrapper.
- Installation and update preserve existing operator configuration and keys.

Remaining deployment hardening:

- Define explicit service ownership and group access for the control socket.
- Verify Linux and Raspberry Pi operational behavior for installer, service,
  UDPSEC, and control-socket deployments.
- Aggregate resource-closer failures. A failing closer can currently mask the
  primary runtime exception or prevent a later closer from running; correcting
  that is cleanup hardening, not a Campaign D processor-boundary guarantee.

### 2. Campaign F — Worker Readiness (Completed)

The current single-process Python runtime now has the Worker Readiness
foundation: bounded stage queues and backpressure, processor-instance state
ownership and reset semantics, capacity-safe processing snapshot handoff,
immutable pull-based metrics, runtime input/output traffic accounting, and
read-only operator statistics through the control protocol and `aismixerctl`.

This closes the in-process boundaries needed for staged process separation. It
does not mean that ingress or egress worker processes, a coordinator, IPC,
cross-process supervision, or distributed metrics already exist.

### 3. Later Process Architecture And Native Implementation

- Introduce a coordinator process and actual ingress worker and egress worker
  processes.
- Add IPC and cross-process routing-snapshot distribution.
- Define cross-process lifecycle supervision and failure handling.
- Define worker restart and recovery policy.
- Add cross-process or distributed metrics aggregation where required.
- Implement a native processor and its bindings behind the established
  processor contract.
- Migrate in stages rather than as a single large rewrite.

### 4. Routing-State Operations

- Consider optional persistence or controlled restoration of runtime routing
  state.
- Add safe configuration reload or watch behavior.
- Keep rollback history for recent routing snapshots.
- Improve operational observability around active routes, targets,
  generations, and control operations.

### 5. Maritime Security And Data-Quality Research

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
- Exported, persistent, or distributed monitoring and health reporting beyond
  the current process-local runtime statistics.

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
