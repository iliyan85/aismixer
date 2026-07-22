<a id="english"></a>

**English · [Български](#bulgarian) · [Română](#romanian)**

# 🛰️ AISMixer — AIS NMEA 0183 stream processor and routing engine

**Normalize · Deduplicate · Tag · Route · Forward**

AISMixer processes AIS NMEA 0183 streams with UDP/UDPSEC ingress, multipart
assembly, deduplication, logical routing, and targeted UDP forwarding.

[🌐 Website](https://aismixer.net) · [📚 Examples](examples/README.md) ·
[📐 Behavioural contract](BEHAVIORAL_CONTRACT.md) ·
[🔐 `nmea_sproxy` guide](nmea_sproxy/README.md) · [🗺️ Roadmap](ROADMAP.md)

**Keywords:** AIS software, Automatic Identification System, NMEA 0183, AIVDM,
AIVDO, multiplexer, deduplication, NMEA TAG block, `s`/`c`/`g`, routing, UDP,
UDPSEC, ECDSA, AES-GCM, Raspberry Pi.

> ### ⚡ TL;DR
> AISMixer receives AIS feeds from multiple receivers, extracts `!AIVDM` and
> `!AIVDO`, reassembles multipart messages, removes near-real-time duplicates,
> manages NMEA TAG metadata, and forwards one clean logical stream. Optional
> logical routing can direct each ingress source to selected named UDP targets,
> while `aismixerctl` can atomically replace or disable the active routing
> snapshot through a local Unix-domain control socket.

---

## 🌿 Branches and website

The `main` branch is the primary runtime and development branch. It contains the
Python service, secure proxy helpers, configuration examples, control-plane
components, and the test suite under `tests/`.

The public website lives on the long-lived `website` branch. GitHub Pages
deploys from that branch using `/docs` as its site root, so `docs/` is
intentionally not present on `main`.

---

## 🧭 What is AISMixer?

**AISMixer** is a Python service for receiving, normalizing, deduplicating,
tagging, routing, and forwarding AIS NMEA 0183 streams.

- **`aismixer.py`** is the long-running mixer and data-plane service.
- **`nmea_sproxy`** is the station-side network proxy. One process forwards one
  local UDP or physical serial input to one AISMixer UDPSEC or UDP input.
- **`aismixerctl.py`** is the operator CLI for the optional local routing-control
  socket.

```text
AIS receiver UDP      \
AIS receiver UDP       \        +----------------+       +----------------+
nmea_sproxy UDPSEC/UDP ------> |    AISMixer    | ----> | UDP targets    |
                                |   data plane   |       +----------------+
                                +----------------+
                                         ^
                                         |
                                optional Unix control plane
                                         |
                                   aismixerctl
```

---

## ✅ Current capabilities

### ✅ Implemented

- UDP ingress over IPv4 and IPv6.
- Authenticated encrypted UDPSEC ingress through `nmea_sproxy`.
- Physical serial or USB virtual COM receiver input through `nmea_sproxy`.
- Explicit trusted-network plain UDP output through `nmea_sproxy`.
- `!AIVDM` and `!AIVDO` extraction.
- Fully out-of-order multipart assembly using NMEA fragment fields and ingress
  assembler identity.
- Lifecycle-aware, deterministic NMEA TAG `s`/`c`/`g` handling.
- Global deduplication in legacy broadcast mode, group-atomic for multipart
  messages.
- Legacy forwarding to every configured UDP forwarder.
- Named UDP egress targets.
- Outbound UDP source-address binding for AISMixer forwarders.
- Application-level ingress allow-lists for AISMixer UDP and UDPSEC listeners.
- `nmea_sproxy` local UDP ingress allow-lists.
- `nmea_sproxy` outbound UDPSEC/plain UDP source-address binding.
- Static logical routing loaded at startup.
- Logical `source_id` and `target_id` matching.
- Logical source zones with `include`, `union`, `intersection`, and
  `difference`.
- Target-scoped deduplication in routing mode, group-atomic for multipart
  messages.
- Immutable routing tables and process-local routing generations.
- Atomic runtime replacement of the active routing snapshot.
- Versioned JSON routing-control protocol.
- `routing.status`, `routing.replace`, and `routing.disable`.
- Unix-domain NDJSON control server and client.
- `aismixerctl` CLI.
- Repository-managed systemd service with `RuntimeDirectory=aismixer`.
- Global `/usr/local/bin/aismixerctl` wrapper installed by lifecycle scripts.

### 🧪 Opt-in operational interface

The runtime control plane is implemented but deliberately opt-in:

- `control.unix.enabled: true` is required.
- The listener requires POSIX Unix-domain socket support.
- Filesystem ownership, group, and mode on the socket path are the current
  authorization boundary.
- There is no application-level control token.
- The installed systemd unit creates `/run/aismixer` only while the service is
  running; the runtime directory is not persistent state.
- Runtime routing updates are process-local and are not persisted across
  service restart.

### 🧭 Planned or not implemented

- Persistence of runtime routing state.
- Automatic config-file watching or reload.
- Dynamic creation/removal of ingress or egress adapters.
- Multiprocessing coordinator and IPC synchronization.
- P2P routing exchange.
- HTTP or TCP control APIs.
- MQTT, AMQP, MongoDB, or HTTP egress adapters.
- Geographic, vessel, or MMSI filtering.
- Spoof detection.
- Long-term AIS storage and analytics.

---

## 🔀 Architecture

AISMixer keeps the **data plane** and **control plane** separate.

### 📡 Data plane

The data plane receives AIS data and builds an internal `IngressEvent`. The
forwarding boundary ignores a non-string `raw_line` before routing and
extraction. For each accepted string event, the data plane captures one
immutable routing snapshot, matches `source_id` once, extracts NMEA sentences,
assembles multipart messages, applies global or target-scoped deduplication,
constructs outbound TAG metadata, and forwards accepted sentences to UDP
egress destinations.

- **Legacy mode:** global deduplication and broadcast to all forwarders.
- **Routing mode:** logical source matching, per-target deduplication, and
  targeted forwarding to named UDP egress destinations.
- One routing snapshot is captured per accepted string `IngressEvent`; a
  concurrent control update affects the next event, not the one already being
  processed.

### 🎛️ Control plane

When enabled, the local Unix-domain socket accepts newline-delimited JSON
requests. The control service validates a candidate routing section against the
currently available target IDs, compiles a new immutable table, and atomically
replaces the process-local routing state.

```text
aismixerctl
    ↓ Unix-domain NDJSON
RoutingControlProtocol
    ↓
RoutingControlService
    ↓
RoutingState (generation + immutable snapshot)
    ↓
next IngressEvent
```

### 🧩 Main components

| Component | Role |
|---|---|
| `aismixer.py` | Main runtime, ingress tasks, mixer loop, forwarding loop, optional control lifecycle |
| `core/routing.py` | Logical zones, set operations, routes, immutable routing table |
| `core/routing_state.py` | Thread-safe process-local generation and snapshot replacement |
| `core/routing_control.py` | Transport-neutral status/replace/disable service |
| `core/routing_control_protocol.py` | Versioned JSON request/response contract |
| `core/routing_control_unix.py` | Async Unix-domain NDJSON server |
| `core/routing_control_unix_client.py` | One-request Unix-domain client |
| `aismixerctl.py` | Operator CLI for runtime routing control |
| `aismixer_secure.py` | UDPSEC handshake, authentication, and decryption |
| `nmea_sproxy/` | Station-side network proxy: one input to one AISMixer UDPSEC or UDP input |
| `assembler.py` | Multipart AIVDM/AIVDO reassembly |
| `dedup.py` | Global or target-scoped duplicate suppression |
| `meta_writer.py` / `meta_cleaner.py` | NMEA TAG output and ingress cleanup |
| `forwarder.py` | UDP broadcast and targeted egress |

---

## 📐 Processing contract

[BEHAVIORAL_CONTRACT.md](BEHAVIORAL_CONTRACT.md) is the normative, tested
contract for the Python reference implementation and the basis for future
differential testing of a native processor. This README remains an operational
overview, not a duplicate normative specification.

The forwarding boundary accepts `str` instances, including subclasses, and
ignores non-string payloads before routing and extraction. Multipart assembly
supports fully out-of-order arrivals: an exact repeat at an occupied ordinal is
idempotent, while a different sentence at that ordinal invalidates the live
assembler generation. Multipart deduplication is group-atomic, and multipart
TAG `s`, `c`, and `g` state follows assembler lifecycle boundaries. Each
accepted ingress event uses one immutable routing snapshot.

---

## 🚀 Quick start: legacy broadcast mode

When no top-level `routing:` section is configured, AISMixer keeps its original
broadcast behavior:

- deduplication is global;
- every accepted output sentence is sent to every configured forwarder;
- unnamed forwarders remain valid;
- routing-control generations may exist, but the active routing table is
  disabled.

Minimal example:

```yaml
station_id: mixstation_1

udp_inputs:
  - id: roof_receiver
    listen_ip: "0.0.0.0"
    listen_port: 17777

forwarders:
  - host: 203.0.113.10
    port: 5000
  - host: 127.0.0.1
    port: 19000
```

Run from the repository:

```bash
python3 aismixer.py
```

### Network endpoint controls

Two optional network-boundary controls are available in AISMixer
configuration:

- `forwarders[].source_ip` binds an outbound UDP forwarder socket to a literal
  IPv4 or IPv6 source address. When omitted, the operating system chooses the
  source address as before. Hostnames in `forwarders[].host` are resolved only
  within the address family selected by `source_ip`.
- `udp_inputs[].allow_from` and `sec_inputs[].allow_from` are
  application-level ingress allow-lists. When the key is omitted, AISMixer does
  not apply an application ACL. An explicitly empty list denies all packets for
  that listener. Entries must be literal IP addresses or CIDR networks; hostnames
  are rejected during startup.

The ingress ACL complements the host firewall; it does not replace firewall,
routing, or interface-level policy.

```yaml
udp_inputs:
  - id: roof_receiver
    listen_ip: "0.0.0.0"
    listen_port: 17777
    allow_from:
      - 192.0.2.15
      - 198.51.100.0/24

sec_inputs:
  - id: secure_stations
    listen_ip: "::"
    listen_port: 19999
    allow_from:
      - 2001:db8:42::/64
      - 203.0.113.44

forwarders:
  - id: aishub
    host: feed.example.net
    port: 10110
    source_ip: 192.0.2.15
```

---

## 🗺️ Static logical routing

Routing mode is enabled by adding a valid top-level `routing:` section.

In routing mode:

- matching uses the internal `IngressEvent.source_id`;
- matching does **not** use the emitted NMEA TAG `s` value;
- route targets must reference named forwarders;
- unknown or unsupported target IDs fail startup validation;
- zones are logical source-ID sets, not geographic AIS areas;
- deduplication is scoped per logical `target_id`.

### 🪪 Canonical source and target IDs

- `udp:<input-id>` when `udp_inputs[].id` is configured.
- `udp:<mapped-alias>` when a UDP alias map supplies identity.
- `udp:<remote-ip>` when no UDP ID or alias is available.
- `udpsec:<authenticated-station-id>` for an authenticated UDPSEC station.
- `udp:<forwarder-id>` for a named UDP forwarder.

`sec_inputs[].id` may affect the emitted TAG `s` alias when the global
`station_id` is empty, but it does not replace the authenticated UDPSEC routing
source ID.

### 🧮 Logical zone operations

```yaml
routing:
  zones:
    fixed_receivers:
      include:
        - udp:roof
        - udp:dock

    mobile_receivers:
      include:
        - udpsec:boat_ais

    trusted_sources:
      union:
        - fixed_receivers
        - mobile_receivers

    trusted_fixed_sources:
      intersection:
        - trusted_sources
        - fixed_receivers

    public_without_boat:
      difference:
        - trusted_sources
        - mobile_receivers
```

Operands for `union`, `intersection`, and `difference` are names of other
logical zones. They are not coordinates, geographic regions, MMSI lists, or
vessel filters.

See [`examples/config-routing.yaml`](examples/config-routing.yaml) for an
inactive full static-routing example.

---

## 🎛️ Runtime routing control

The Unix control server remains disabled until explicitly enabled:

```yaml
control:
  unix:
    enabled: true
    socket_path: /run/aismixer/control.sock
    socket_mode: "0660"
    max_request_bytes: 1048576
```

### ⚠️ Operational notes

- Adding `control:` or `control.unix:` alone does not enable the server.
- The installed systemd unit uses `RuntimeDirectory=aismixer` to create
  `/run/aismixer` before AISMixer starts. systemd removes that runtime directory
  after the service stops; it is not persistent state.
- If AISMixer is run outside the installed systemd unit, provide an equivalent
  parent directory for the configured socket path.
- Filesystem ownership, group, and mode control access to the socket.
- The service continues to run under the same identity as before. This change
  does not add `User=`, `Group=`, `DynamicUser=`, or a dedicated service
  account.
- With the current root-run service and `socket_mode: "0660"`, access may
  effectively be root-only unless the operator deliberately configures
  ownership or group policy for the socket.
- There is no application-level authentication token.
- The interface is POSIX-only; Windows can run pure tests and development code,
  but not the Unix socket listener.
- Runtime routing changes are process-local and disappear after restart.

See
[`examples/config-routing-control.yaml`](examples/config-routing-control.yaml)
for an inactive complete configuration with static routing and runtime control.

---

## 🧰 `aismixerctl`

The installer deploys a small POSIX wrapper at `/usr/local/bin/aismixerctl`.
The wrapper executes `/usr/bin/python3 /opt/aismixer/aismixerctl.py "$@"` and
contains no routing or protocol logic.

The default socket path is `/run/aismixer/control.sock`, so an installed system
can query status with:

```bash
aismixerctl status
```

From a repository checkout or copied service directory, use:

```bash
python3 aismixerctl.py status
```

Use `--socket` to override the default path:

```bash
aismixerctl --socket /custom/path.sock status
```

Replace the active process-local routing snapshot:

```bash
aismixerctl \
  replace \
  --file examples/routing-update.yaml \
  --expected-generation 3
```

Disable routing and return the running process to legacy broadcast mode:

```bash
aismixerctl \
  disable \
  --expected-generation 4
```

### 🔢 Generation semantics

- `status` returns the current generation.
- `replace` and `disable` may carry an expected generation.
- A stale update is rejected instead of overwriting a newer snapshot.
- The CLI does not retry automatically.

`replace --file` accepts either:

1. a full configuration containing a top-level `routing:` mapping; or
2. a direct routing section containing only `zones:` and `routes:`.

`routing: null` is not a replacement request; use `disable`.

See [`examples/routing-update.yaml`](examples/routing-update.yaml) for a direct
routing-section update file.

---

## 🔐 `nmea_sproxy` Outputs

UDPSEC is AISMixer's authenticated encrypted station-to-mixer UDP transport.
It is not an external standardized protocol. Stations authenticate with ECDSA,
while AIS data and liveness messages use authenticated AES-GCM encryption.
Authorized station public keys are configured through `authorized_keys.yaml`.
UDPSEC protects packets in transit; it does not prove that the AIS payload
itself is semantically true or physically accurate.

`nmea_sproxy` is the station-side proxy:

```text
one local input (UDP or serial) → one AISMixer UDPSEC or UDP input
```

Example commands:

```bash
cd nmea_sproxy
python3 nmea_sproxy.py
sudo systemctl start nmea_sproxy
sudo systemctl start nmea_sproxy@boat
```

Template names such as `boat`, `yacht`, or `balchik_roof` are operator-chosen
labels. See [`nmea_sproxy/README.md`](nmea_sproxy/README.md) for the detailed
station-side guide.

`nmea_sproxy` has its own station-side endpoint controls: top-level
`allow_from` limits which local/LAN UDP senders may be forwarded under the
station identity. Top-level `source_ip` is the legacy UDPSEC source binding;
for explicit `output:` mappings, `output.source_ip` binds either UDPSEC or plain
UDP output sockets to a literal source address. These are configured in
`nmea_sproxy` relation files; they are separate from AISMixer's `udp_inputs[]`,
`sec_inputs[]`, and `forwarders[]` controls.

For stations with a physical AIS receiver, `nmea_sproxy` can also read directly
from a serial or USB virtual COM port and forward the resulting NMEA sentences
through the configured UDPSEC or UDP output. It can also explicitly forward
plain UDP for trusted LAN/VPN environments; plain UDP provides no UDPSEC
authentication, encryption, replay protection, or liveness protocol.

---

## 🏷️ NMEA TAG behavior

AISMixer reads ingress TAG metadata and emits a controlled `s`/`c`/`g` TAG
block according to the runtime options described below.

For multipart groups, TAG `s`, `c`, and `g` context follows assembler conflict,
expiry, and completion boundaries. The exact ownership and selection rules are
defined in [BEHAVIORAL_CONTRACT.md](BEHAVIORAL_CONTRACT.md).

### 🪪 TAG `s` — source label

The emitted TAG `s` value is selected separately from routing `source_id`.

Priority:

1. non-empty global `station_id`;
2. per-input ID, UDP alias, or authorized UDPSEC station/client name;
3. incoming TAG `s` when present;
4. remote IP fallback.

The emitted value is sanitized to `[A-Za-z0-9_]` and limited to 15 characters.
Routing source IDs are opaque internal identifiers and are not sanitized or
truncated as TAG `s` values.

### 🕒 TAG `c` — timestamp

For multipart groups, `c_preserve_ingress_c: true` selects the minimum valid
numeric ingress TAG `c` value observed during the live assembler generation,
independently of arrival order. When preservation is disabled or no valid value
is present, AISMixer emits server time. The behavioural contract records the
intentional single-sentence `c:0` compatibility exception.

### 🧷 TAG `g` — output group metadata

TAG `g` is ingress/output metadata for multipart messages. It is **not** the
assembler key. Multipart assembly uses NMEA fragment fields together with the
ingress assembler identity. Preserved group IDs use exact string agreement;
missing or disagreeing observations produce one generated ID for the completed
logical group.

Relevant options:

```yaml
g_preserve_ingress_gid: true
g_id_digits: 18
g_always_tag_single: false
c_preserve_ingress_c: true
```

---

## 📦 Installation

Run directly from the repository:

```bash
python3 aismixer.py
```

Or install the repository-managed systemd service and global CLI wrapper:

```bash
./install.sh
```

The installer deploys runtime files to `/opt/aismixer`, installs
`aismixer.service`, installs `/usr/local/bin/aismixerctl`, reloads systemd, and
enables the service. It does not start AISMixer automatically. The unit uses
`RuntimeDirectory=aismixer`, so systemd creates `/run/aismixer` while the
service is running and removes it after the service stops.

The installed service reads `/etc/aismixer/config.yaml`. On first installation,
`install.sh` seeds `/etc/aismixer` from the repository configuration while
preserving any existing operator configuration and keys.

Update installed runtime files and restart the service with:

```bash
./update.sh
```

`update.sh` leaves operator configuration and keys under `/etc/aismixer`
untouched.

Uninstall the service and installed runtime files with:

```bash
./uninstall.sh
```

By default, uninstall preserves `/etc/aismixer`; the explicit
`./uninstall.sh --purge-config` option also removes operator configuration and
keys.

---

## 📚 Examples

The examples are inactive until copied or adapted by an operator:

- [`examples/config-routing.yaml`](examples/config-routing.yaml) — full static
  routing configuration.
- [`examples/config-routing-control.yaml`](examples/config-routing-control.yaml)
  — full routing configuration with `control.unix` enabled.
- [`examples/routing-update.yaml`](examples/routing-update.yaml) — direct routing
  section for `aismixerctl replace --file`.
- [`examples/README.md`](examples/README.md) — short guide to the example files.

All addresses, IDs, ports, paths, and keys in example files must be adapted to
the deployment.

---

## 🧪 Testing

The test suite covers multipart assembly, TAG handling, metadata processing,
UDPSEC helpers, routing, snapshot replacement, control protocol and transports,
`aismixerctl`, and forwarding behavior.

```bash
python -m pytest
```

Real Unix-domain listener tests require Linux, WSL, Raspberry Pi OS, or another
POSIX environment with asyncio Unix-socket support.

---

## ⚠️ Current limitations

- UDP is the currently implemented egress adapter.
- Routing state and generations are process-local.
- Runtime control changes are not persistent.
- There is no multiprocessing coordinator or cross-process synchronization.
- There is no automatic config reload/watch.
- There is no geographic, MMSI, or vessel filtering.
- Unix control requires POSIX Unix-domain socket support.
- Access control relies on Unix filesystem permissions.
- A dedicated service user/group policy is not yet introduced.

---

## 📖 Further documentation

- [Behavioural contract](BEHAVIORAL_CONTRACT.md)
- [Examples](examples/README.md)
- [`nmea_sproxy` operator guide](nmea_sproxy/README.md)
- [GitHub Wiki](https://github.com/iliyan85/aismixer/wiki)
- [Contributing guide](CONTRIBUTING.md)
- [Security policy](SECURITY.md)
- [Project roadmap](ROADMAP.md)
- [Public website](https://aismixer.net)

[⬆ Back to language selector](#english)

---

<a id="bulgarian"></a>

**[English](#english) · Български · [Română](#romanian)**

# 🇧🇬 AISMixer — обработка и маршрутизация на AIS NMEA 0183 потоци

**Нормализация · Дедупликация · TAG metadata · Маршрутизация · Препращане**

AISMixer обработва AIS NMEA 0183 потоци с UDP/UDPSEC входове, сглобяване на
multipart съобщения, дедупликация, логическа маршрутизация и целево UDP
препращане.

[🌐 Уебсайт](https://aismixer.net) · [📚 Примери](examples/README.md) ·
[📐 Договор за поведение](BEHAVIORAL_CONTRACT.md) ·
[🔐 Ръководство за `nmea_sproxy`](nmea_sproxy/README.md) ·
[🗺️ План за развитие](ROADMAP.md)

**Ключови думи:** AIS софтуер, Automatic Identification System, NMEA 0183,
AIVDM, AIVDO, multiplexer, дедупликация, NMEA TAG block, `s`/`c`/`g`, routing,
UDP, UDPSEC, ECDSA, AES-GCM, Raspberry Pi.

> ### ⚡ Накратко
> AISMixer приема AIS потоци от няколко приемника, извлича `!AIVDM` и `!AIVDO`,
> сглобява multipart съобщения, премахва близки във времето дубликати, управлява
> NMEA TAG metadata и излъчва един чист логически поток. По желание логическата
> маршрутизация насочва всеки ingress източник към избрани именувани UDP цели, а
> `aismixerctl` може атомарно да замени или изключи активния routing snapshot
> през локален Unix-domain control socket.

---

## 🌿 Клонове и уебсайт

Клонът `main` е основният runtime и development клон. В него са Python услугата,
secure proxy компонентите, конфигурационните примери, control-plane модулите и
тестовете в `tests/`.

Публичният сайт е в дългоживеещия клон `website`. GitHub Pages се публикува от
него с `/docs` като site root, затова `docs/` умишлено не присъства в `main`.

---

## 🧭 Какво е AISMixer?

**AISMixer** е Python услуга за приемане, нормализиране, дедупликация, TAG
обработка, маршрутизация и препращане на AIS NMEA 0183 потоци.

- **`aismixer.py`** е дългоживеещият mixer и data-plane процес.
- **`nmea_sproxy`** е мрежовото прокси при станцията. Един процес препраща един
  локален UDP или физически serial вход към един UDPSEC или UDP вход на AISMixer.
- **`aismixerctl.py`** е операторският CLI клиент за допълнителния локален
  routing-control socket.

```text
AIS приемник UDP      \
AIS приемник UDP       \        +----------------+       +----------------+
nmea_sproxy UDPSEC/UDP ------> |    AISMixer    | ----> | UDP цели       |
                                |   data plane   |       +----------------+
                                +----------------+
                                         ^
                                         |
                                opt-in Unix control plane
                                         |
                                   aismixerctl
```

---

## ✅ Текущи възможности

### ✅ Реализирано

- UDP ingress по IPv4 и IPv6.
- Автентикиран и криптиран UDPSEC ingress чрез `nmea_sproxy`.
- Физически serial или USB virtual COM receiver вход чрез `nmea_sproxy`.
- Изричен trusted-network plain UDP изход чрез `nmea_sproxy`.
- Извличане на `!AIVDM` и `!AIVDO`.
- Напълно out-of-order сглобяване на multipart чрез NMEA fragment полетата и
  ingress assembler identity.
- Lifecycle-aware и детерминирана обработка на NMEA TAG `s`/`c`/`g`.
- Глобална дедупликация в legacy broadcast режим, атомарна за цялата multipart
  група.
- Legacy препращане към всички конфигурирани UDP forwarder-и.
- Именувани UDP egress цели.
- Изходно UDP source-address binding за AISMixer forwarder-и.
- Application-level ingress allow-lists за AISMixer UDP и UDPSEC listeners.
- Локални UDP ingress allow-lists в `nmea_sproxy`.
- Изходно UDPSEC/plain UDP source-address binding в `nmea_sproxy`.
- Статична логическа маршрутизация, зареждана при стартиране.
- Съпоставяне чрез логически `source_id` и `target_id`.
- Логически source zones с `include`, `union`, `intersection` и `difference`.
- Дедупликация по отделен target в routing режим, атомарна за цялата multipart
  група.
- Immutable routing tables и process-local generations.
- Атомарна runtime подмяна на активния routing snapshot.
- Версиониран JSON routing-control протокол.
- `routing.status`, `routing.replace` и `routing.disable`.
- Unix-domain NDJSON control server и клиент.
- CLI инструментът `aismixerctl`.
- Repository-managed systemd service с `RuntimeDirectory=aismixer`.
- Глобален `/usr/local/bin/aismixerctl` wrapper, инсталиран от lifecycle scripts.

### 🧪 Opt-in оперативен интерфейс

Runtime control plane е реализиран, но умишлено се включва само изрично:

- изисква се `control.unix.enabled: true`;
- listener-ът изисква POSIX Unix-domain socket support;
- filesystem собственикът, групата и mode на socket path са текущата граница
  за достъп;
- няма application-level control token;
- инсталираният systemd unit създава `/run/aismixer` само докато услугата
  работи; runtime директорията не е persistent state;
- runtime routing промените са process-local и не се запазват след рестарт.

### 🧭 Планирано или нереализирано

- Запазване на runtime routing state.
- Автоматично следене или reload на конфигурационния файл.
- Динамично създаване и премахване на ingress/egress adapters.
- Multiprocessing coordinator и IPC синхронизация.
- P2P обмен на routing информация.
- HTTP или TCP control API.
- MQTT, AMQP, MongoDB или HTTP egress adapters.
- Географско, vessel или MMSI филтриране.
- Spoof detection.
- Дългосрочно AIS съхранение и анализи.

---

## 🔀 Архитектура

AISMixer разделя **data plane** и **control plane**.

### 📡 Data plane

Data plane приема AIS данните и създава вътрешен `IngressEvent`. Forwarding
границата игнорира non-string `raw_line` преди routing и extraction. За всеки
приет string event data plane взема един immutable routing snapshot, съпоставя
`source_id` веднъж, извлича NMEA изреченията, сглобява multipart съобщенията,
прилага глобална или target-scoped дедупликация, изгражда изходната TAG metadata
и препраща приетите изречения към UDP egress дестинациите.

- **Legacy режим:** глобална дедупликация и broadcast към всички forwarder-и.
- **Routing режим:** логическо source matching, дедупликация по target и целево
  препращане към именувани UDP egress дестинации.
- За всеки приет string `IngressEvent` се взема един routing snapshot;
  паралелна control промяна засяга следващия event, а не вече обработвания.

### 🎛️ Control plane

При включване локалният Unix-domain socket приема newline-delimited JSON заявки.
Control service валидира кандидат routing секцията спрямо наличните target IDs,
компилира нова immutable таблица и атомарно заменя process-local routing state.

```text
aismixerctl
    ↓ Unix-domain NDJSON
RoutingControlProtocol
    ↓
RoutingControlService
    ↓
RoutingState (generation + immutable snapshot)
    ↓
следващият IngressEvent
```

### 🧩 Основни компоненти

| Компонент | Роля |
|---|---|
| `aismixer.py` | Основен runtime, ingress tasks, mixer loop, forwarding loop и control lifecycle |
| `core/routing.py` | Логически zones, set operations, routes и immutable routing table |
| `core/routing_state.py` | Thread-safe process-local generation и snapshot replacement |
| `core/routing_control.py` | Transport-neutral service за status/replace/disable |
| `core/routing_control_protocol.py` | Версиониран JSON request/response contract |
| `core/routing_control_unix.py` | Async Unix-domain NDJSON server |
| `core/routing_control_unix_client.py` | Unix-domain клиент с една заявка на връзка |
| `aismixerctl.py` | Операторски CLI за runtime routing control |
| `aismixer_secure.py` | UDPSEC handshake, автентикация и декриптиране |
| `nmea_sproxy/` | Station-side network proxy: един вход към един UDPSEC или UDP вход на AISMixer |
| `assembler.py` | Сглобяване на multipart AIVDM/AIVDO |
| `dedup.py` | Глобална или target-scoped дедупликация |
| `meta_writer.py` / `meta_cleaner.py` | NMEA TAG изход и ingress cleanup |
| `forwarder.py` | UDP broadcast и targeted egress |

---

## 📐 Договор за обработка

[BEHAVIORAL_CONTRACT.md](BEHAVIORAL_CONTRACT.md) е нормативният, проверен с
тестове договор за поведението на референтната Python реализация и основата за
бъдещо диференциално тестване на native processor. Този README остава
оперативен преглед, а не дублирана нормативна спецификация.

Границата на forwarding обработката приема екземпляри на `str`, включително
негови subclasses, и игнорира non-string payload-и преди routing и extraction.
Multipart сглобяването поддържа фрагменти в напълно произволен ред: точно
повторение на изречението за вече зает ordinal е идемпотентно, а различно
изречение на същия ordinal обезсилва активната assembler generation. Multipart
дедупликацията се решава атомарно за цялата група, а състоянието на TAG `s`, `c`
и `g` следва lifecycle границите на assembler-а. Всеки приет ingress event се
обработва с един immutable routing snapshot.

---

## 🚀 Бърз старт: legacy broadcast режим

Когато няма top-level `routing:` секция, AISMixer запазва първоначалното
broadcast поведение:

- дедупликацията е глобална;
- всяко прието изходно изречение се изпраща към всички forwarder-и;
- forwarder-и без `id` остават валидни;
- routing-control generations може да съществуват, но активната routing table е
  изключена.

Минимален пример:

```yaml
station_id: mixstation_1

udp_inputs:
  - id: roof_receiver
    listen_ip: "0.0.0.0"
    listen_port: 17777

forwarders:
  - host: 203.0.113.10
    port: 5000
  - host: 127.0.0.1
    port: 19000
```

Стартиране от repository checkout:

```bash
python3 aismixer.py
```

### Контрол на network endpoints

В AISMixer конфигурацията са налични два допълнителни network-boundary
контрола:

- `forwarders[].source_ip` обвързва изходния UDP forwarder socket към literal
  IPv4 или IPv6 source address. Когато е пропуснат, операционната система избира
  source address както досега. Hostnames в `forwarders[].host` се resolve-ват
  само в address family, избрана от `source_ip`.
- `udp_inputs[].allow_from` и `sec_inputs[].allow_from` са application-level
  ingress allow-lists. Когато ключът е пропуснат, AISMixer не прилага
  application ACL. Явно празен списък отказва всички пакети за този listener.
  Entries трябва да са literal IP addresses или CIDR networks; hostnames се
  отхвърлят при startup.

Ingress ACL допълва host firewall-а; не заменя firewall, routing или
interface-level policy.

```yaml
udp_inputs:
  - id: roof_receiver
    listen_ip: "0.0.0.0"
    listen_port: 17777
    allow_from:
      - 192.0.2.15
      - 198.51.100.0/24

sec_inputs:
  - id: secure_stations
    listen_ip: "::"
    listen_port: 19999
    allow_from:
      - 2001:db8:42::/64
      - 203.0.113.44

forwarders:
  - id: aishub
    host: feed.example.net
    port: 10110
    source_ip: 192.0.2.15
```

---

## 🗺️ Статична логическа маршрутизация

Routing режимът се включва с валидна top-level `routing:` секция.

В routing режим:

- matching използва вътрешния `IngressEvent.source_id`;
- matching **не** използва излъчения NMEA TAG `s`;
- route targets трябва да сочат към именувани forwarder-и;
- неизвестни или неподдържани target IDs прекратяват startup validation;
- zones са логически множества от source IDs, а не географски AIS области;
- дедупликацията се изпълнява по отделен логически `target_id`.

### 🪪 Канонични source и target IDs

- `udp:<input-id>` при конфигуриран `udp_inputs[].id`.
- `udp:<mapped-alias>` при identity от UDP alias map.
- `udp:<remote-ip>` когато няма UDP ID или alias.
- `udpsec:<authenticated-station-id>` за автентикирана UDPSEC станция.
- `udp:<forwarder-id>` за именуван UDP forwarder.

`sec_inputs[].id` може да влияе на излъчения TAG `s` alias, когато глобалният
`station_id` е празен, но не заменя автентикирания UDPSEC routing source ID.

### 🧮 Операции върху логически zones

```yaml
routing:
  zones:
    fixed_receivers:
      include:
        - udp:roof
        - udp:dock

    mobile_receivers:
      include:
        - udpsec:boat_ais

    trusted_sources:
      union:
        - fixed_receivers
        - mobile_receivers

    trusted_fixed_sources:
      intersection:
        - trusted_sources
        - fixed_receivers

    public_without_boat:
      difference:
        - trusted_sources
        - mobile_receivers
```

Операндите на `union`, `intersection` и `difference` са имена на други логически
zones. Те не са координати, географски области, MMSI списъци или vessel filters.

Виж [`examples/config-routing.yaml`](examples/config-routing.yaml) за неактивен
пълен пример със статична маршрутизация.

---

## 🎛️ Runtime routing control

Unix control server остава изключен, докато не бъде включен изрично:

```yaml
control:
  unix:
    enabled: true
    socket_path: /run/aismixer/control.sock
    socket_mode: "0660"
    max_request_bytes: 1048576
```

### ⚠️ Оперативни бележки

- Самото добавяне на `control:` или `control.unix:` не включва server-а.
- Инсталираният systemd unit използва `RuntimeDirectory=aismixer`, за да създаде
  `/run/aismixer` преди старта на AISMixer. systemd премахва тази runtime
  директория след спиране на услугата; тя не е persistent state.
- Ако AISMixer се стартира извън инсталирания systemd unit, осигури
  еквивалентна parent directory за конфигурирания socket path.
- Filesystem собственикът, групата и mode управляват достъпа до socket-а.
- Услугата продължава да работи със същата identity както досега. Тази промяна
  не добавя `User=`, `Group=`, `DynamicUser=` или dedicated service account.
- При текущата root-run услуга и `socket_mode: "0660"` достъпът може на практика
  да е само за root, освен ако операторът умишлено не конфигурира ownership или
  group policy за socket-а.
- Няма application-level authentication token.
- Интерфейсът е само за POSIX. Windows може да изпълнява pure tests и
  development кода, но не и Unix socket listener-а.
- Runtime routing промените са process-local и изчезват след рестарт.

Виж
[`examples/config-routing-control.yaml`](examples/config-routing-control.yaml)
за неактивна пълна конфигурация със static routing и runtime control.

---

## 🧰 `aismixerctl`

Installer-ът разполага малък POSIX wrapper в `/usr/local/bin/aismixerctl`.
Wrapper-ът изпълнява `/usr/bin/python3 /opt/aismixer/aismixerctl.py "$@"` и не
съдържа routing или protocol логика.

Default socket path е `/run/aismixer/control.sock`, така че инсталирана система
може да провери status с:

```bash
aismixerctl status
```

От repository checkout или копирана service директория използвай:

```bash
python3 aismixerctl.py status
```

Използвай `--socket`, за да override-неш default path:

```bash
aismixerctl --socket /custom/path.sock status
```

Подмяна на активния process-local routing snapshot:

```bash
aismixerctl \
  replace \
  --file examples/routing-update.yaml \
  --expected-generation 3
```

Изключване на routing и връщане на работещия процес към legacy broadcast режим:

```bash
aismixerctl \
  disable \
  --expected-generation 4
```

### 🔢 Generation semantics

- `status` връща текущата generation.
- `replace` и `disable` могат да носят expected generation.
- Stale update се отхвърля, вместо да презапише по-нов snapshot.
- CLI не прави автоматични повторни опити.

`replace --file` приема:

1. пълна конфигурация с top-level `routing:` mapping; или
2. директна routing секция само с `zones:` и `routes:`.

`routing: null` не е replace заявка; използвай `disable`.

Виж [`examples/routing-update.yaml`](examples/routing-update.yaml) за директен
routing-section update файл.

---

## 🔐 Изходи на `nmea_sproxy`

UDPSEC е автентикираният и криптиран UDP транспорт между станцията и AISMixer.
Това не е външен стандартизиран протокол. Станциите се автентикират с ECDSA, а
AIS данните и liveness съобщенията използват автентикирано AES-GCM криптиране.
Разрешените публични ключове на станциите се конфигурират чрез
`authorized_keys.yaml`. UDPSEC защитава пакетите при пренос, но не доказва, че
самият AIS payload е семантично верен или физически точен.

`nmea_sproxy` е проксито при станцията:

```text
един локален вход (UDP или serial) → един UDPSEC или UDP вход на AISMixer
```

Примерни команди:

```bash
cd nmea_sproxy
python3 nmea_sproxy.py
sudo systemctl start nmea_sproxy
sudo systemctl start nmea_sproxy@boat
```

Template имена като `boat`, `yacht` или `balchik_roof` са етикети, избрани от
оператора. Подробното ръководство е в
[`nmea_sproxy/README.md`](nmea_sproxy/README.md).

`nmea_sproxy` има отделни station-side endpoint controls: top-level
`allow_from` ограничава кои локални/LAN UDP податели могат да бъдат препратени
под station identity. Top-level `source_ip` е legacy UDPSEC source binding;
при explicit `output:` mappings `output.source_ip` обвързва UDPSEC или plain UDP
изходните sockets към literal source address. Те се конфигурират в relation
файловете на `nmea_sproxy` и са отделни от AISMixer контролите `udp_inputs[]`,
`sec_inputs[]` и `forwarders[]`.

За станции с физически AIS приемник `nmea_sproxy` може да чете директно от
serial или USB virtual COM port и да препраща получените NMEA изречения през
конфигурирания UDPSEC или UDP output. Може също изрично да препраща plain UDP
за trusted LAN/VPN среди; plain UDP не предоставя UDPSEC authentication,
encryption, replay protection или liveness protocol.

---

## 🏷️ Поведение на NMEA TAG metadata

AISMixer чете ingress TAG metadata и излъчва контролиран `s`/`c`/`g` TAG block
според описаните по-долу runtime настройки.

За multipart групите контекстът на TAG `s`, `c` и `g` следва assembler
границите за conflict, expiry и completion. Точните правила за ownership и
selection са определени в
[BEHAVIORAL_CONTRACT.md](BEHAVIORAL_CONTRACT.md).

### 🪪 TAG `s` — source label

Излъченият TAG `s` се избира отделно от routing `source_id`.

Приоритет:

1. непразен глобален `station_id`;
2. ID на входа, UDP alias или име на разрешената UDPSEC станция/клиент;
3. входящ TAG `s`, когато е наличен;
4. remote IP като fallback.

Излъчената стойност се sanitize-ва до `[A-Za-z0-9_]` и се ограничава до 15
символа. Routing source IDs са opaque вътрешни идентификатори и не се sanitize-ват
или съкращават като TAG `s`.

### 🕒 TAG `c` — timestamp

За multipart групите `c_preserve_ingress_c: true` избира минималната валидна
числова стойност на входящ TAG `c`, наблюдавана през активната assembler
generation, независимо от реда на пристигане. Когато запазването е изключено
или няма валидна стойност, AISMixer излъчва сървърното време. Договорът за
поведение описва умишленото compatibility изключение за single-sentence `c:0`.

### 🧷 TAG `g` — output group metadata

TAG `g` е ingress/output metadata за multipart съобщения. Той **не** е assembler
key. Multipart assembly използва NMEA fragment полетата заедно с ingress
assembler identity. Запазените group IDs се сравняват като точни strings;
липсващи или несъгласувани наблюдения водят до един генериран ID за завършената
логическа група.

Свързани настройки:

```yaml
g_preserve_ingress_gid: true
g_id_digits: 18
g_always_tag_single: false
c_preserve_ingress_c: true
```

---

## 📦 Инсталация

Директно стартиране от repository checkout:

```bash
python3 aismixer.py
```

Или инсталиране на repository-managed systemd услугата и глобалния CLI wrapper:

```bash
./install.sh
```

Installer-ът разполага runtime файловете в `/opt/aismixer`, инсталира
`aismixer.service`, инсталира `/usr/local/bin/aismixerctl`, reload-ва systemd и
enable-ва услугата. Той не стартира AISMixer автоматично. Unit-ът използва
`RuntimeDirectory=aismixer`, така че systemd създава `/run/aismixer`, докато
услугата работи, и я премахва след спиране.

Инсталираната услуга чете `/etc/aismixer/config.yaml`. При първа инсталация
`install.sh` попълва `/etc/aismixer` от repository конфигурацията и запазва
съществуващите операторски конфигурации и ключове.

Обновяване на инсталираните runtime файлове и рестартиране на услугата:

```bash
./update.sh
```

`update.sh` не променя операторските конфигурации и ключове в `/etc/aismixer`.

Премахване на услугата и инсталираните runtime файлове:

```bash
./uninstall.sh
```

По подразбиране деинсталирането запазва `/etc/aismixer`; изричната опция
`./uninstall.sh --purge-config` премахва и операторските конфигурации и ключове.

---

## 📚 Примери

Примерите са неактивни, докато операторът не ги копира или адаптира:

- [`examples/config-routing.yaml`](examples/config-routing.yaml) — пълна static
  routing конфигурация.
- [`examples/config-routing-control.yaml`](examples/config-routing-control.yaml)
  — пълна routing конфигурация с включен `control.unix`.
- [`examples/routing-update.yaml`](examples/routing-update.yaml) — директна
  routing секция за `aismixerctl replace --file`.
- [`examples/README.md`](examples/README.md) — кратко описание на примерните
  файлове.

Всички адреси, IDs, портове, пътища и ключове в примерните файлове трябва да се
адаптират към конкретната инсталация.

---

## 🧪 Тестове

Тестовете покриват multipart assembly, TAG обработката, metadata processing,
UDPSEC helper-ите, routing, snapshot replacement, control protocol и
transport-ите, `aismixerctl` и forwarding поведението.

```bash
python -m pytest
```

Реалните Unix-domain listener тестове изискват Linux, WSL, Raspberry Pi OS или
друга POSIX среда с asyncio Unix-socket support.

---

## ⚠️ Текущи ограничения

- UDP е текущо реализираният egress adapter.
- Routing state и generations са process-local.
- Runtime control промените не се запазват.
- Няма multiprocessing coordinator или cross-process синхронизация.
- Няма автоматичен config reload/watch.
- Няма географско, MMSI или vessel филтриране.
- Unix control изисква POSIX Unix-domain socket support.
- Контролът на достъпа разчита на Unix filesystem permissions.
- Dedicated service user/group policy все още не е въведена.

---

## 📖 Допълнителна документация

- [Договор за поведение](BEHAVIORAL_CONTRACT.md)
- [Примерни конфигурации](examples/README.md)
- [Операторско ръководство за `nmea_sproxy`](nmea_sproxy/README.md)
- [GitHub Wiki](https://github.com/iliyan85/aismixer/wiki)
- [Ръководство за принос](CONTRIBUTING.md)
- [Политика за сигурност](SECURITY.md)
- [План за развитие](ROADMAP.md)
- [Публичен уебсайт](https://aismixer.net)

[⬆ Към избора на език](#english)

---

<a id="romanian"></a>

**[English](#english) · [Български](#bulgarian) · Română**

# 🇷🇴 AISMixer — procesarea și rutarea fluxurilor AIS NMEA 0183

**Normalizează · Deduplică · Etichetează · Rutează · Redirecționează**

AISMixer procesează fluxuri AIS NMEA 0183 cu intrări UDP/UDPSEC, asamblare
multipart, deduplicare, rutare logică și redirecționare UDP către destinații
specifice.

[🌐 Site web](https://aismixer.net) · [📚 Exemple](examples/README.md) ·
[📐 Contract comportamental](BEHAVIORAL_CONTRACT.md) ·
[🔐 Ghid `nmea_sproxy`](nmea_sproxy/README.md) · [🗺️ Foaie de parcurs](ROADMAP.md)

**Cuvinte-cheie:** software AIS, Sistem de identificare automată, NMEA 0183,
AIVDM, AIVDO, multiplexor, deduplicare, bloc NMEA TAG, `s`/`c`/`g`, rutare,
UDP, UDPSEC, ECDSA, AES-GCM, Raspberry Pi.

> ### ⚡ Pe scurt
> AISMixer primește fluxuri AIS de la mai multe receptoare, extrage `!AIVDM` și
> `!AIVDO`, reasamblează mesajele multipart, elimină duplicatele aproape în timp
> real, gestionează metadatele NMEA TAG și redirecționează un flux logic curat.
> Rutarea logică opțională poate direcționa fiecare sursă de ingress către
> anumite destinații UDP denumite, iar `aismixerctl` poate înlocui atomic sau
> dezactiva snapshot-ul activ de rutare printr-un socket local din domeniul Unix.

---

## 🌿 Ramuri și site web

Ramura `main` este ramura principală pentru runtime și dezvoltare. Aceasta
conține serviciul Python, componentele proxy securizate, exemplele de
configurare, componentele planului de control și suita de teste din `tests/`.

Site-ul public se află pe ramura de lungă durată `website`. GitHub Pages publică
din această ramură, folosind `/docs` drept rădăcină a site-ului; de aceea,
directorul `docs/` nu este prezent în mod intenționat pe `main`.

---

## 🧭 Ce este AISMixer?

**AISMixer** este un serviciu Python pentru recepționarea, normalizarea,
deduplicarea, etichetarea, rutarea și redirecționarea fluxurilor AIS NMEA 0183.

- **`aismixer.py`** este serviciul de mixare și de plan de date care rulează
  continuu.
- **`nmea_sproxy`** este proxy-ul de rețea de la stație. Un proces
  redirecționează o intrare UDP locală sau o intrare serială fizică spre o
  intrare AISMixer UDPSEC sau UDP.
- **`aismixerctl.py`** este CLI-ul operatorului pentru socket-ul local opțional
  de control al rutării.

```text
Receptor AIS UDP      \
Receptor AIS UDP       \        +----------------+       +----------------+
nmea_sproxy UDPSEC/UDP ------> |    AISMixer    | ----> | Destinații UDP |
                                |  plan de date  |       +----------------+
                                +----------------+
                                         ^
                                         |
                                plan de control Unix opțional
                                         |
                                   aismixerctl
```

---

## ✅ Capabilități actuale

### ✅ Implementat

- Ingress UDP prin IPv4 și IPv6.
- Ingress UDPSEC autentificat și criptat prin `nmea_sproxy`.
- Intrare de la un receptor fizic serial sau USB virtual COM prin
  `nmea_sproxy`.
- Ieșire UDP simplă, explicită, pentru rețele de încredere prin `nmea_sproxy`.
- Extragerea mesajelor `!AIVDM` și `!AIVDO`.
- Asamblare multipart complet independentă de ordinea sosirii, folosind
  câmpurile fragmentelor NMEA și identitatea assemblerului de ingress.
- Gestionare deterministă a NMEA TAG `s`/`c`/`g`, care respectă ciclul de viață.
- Deduplicare globală în modul legacy broadcast, atomică la nivel de grup pentru
  mesajele multipart.
- Redirecționare legacy către fiecare forwarder UDP configurat.
- Destinații UDP egress denumite.
- Asocierea adresei-sursă UDP de ieșire pentru forwarderele AISMixer.
- Liste de adrese ingress permise la nivelul aplicației pentru listener-ele UDP
  și UDPSEC AISMixer.
- Liste de adrese permise pentru ingress-ul UDP local al `nmea_sproxy`.
- Asocierea adresei-sursă de ieșire UDPSEC/UDP simplu pentru `nmea_sproxy`.
- Rutare logică statică încărcată la pornire.
- Potrivire logică după `source_id` și `target_id`.
- Zone logice de surse cu `include`, `union`, `intersection` și `difference`.
- Deduplicare separată pentru fiecare destinație în modul de rutare, atomică la
  nivel de grup pentru mesajele multipart.
- Tabele de rutare imuabile și generații de rutare locale procesului.
- Înlocuirea atomică, la runtime, a snapshot-ului activ de rutare.
- Protocol JSON versionat pentru controlul rutării.
- `routing.status`, `routing.replace` și `routing.disable`.
- Server și client NDJSON prin socket din domeniul Unix.
- CLI `aismixerctl`.
- Serviciu systemd administrat de repository cu `RuntimeDirectory=aismixer`.
- Wrapper global `/usr/local/bin/aismixerctl` instalat de scripturile pentru
  ciclul de viață.

### 🧪 Interfață operațională cu activare explicită

Planul de control la runtime este implementat, dar necesită activare explicită:

- Este obligatoriu `control.unix.enabled: true`.
- Listener-ul necesită suport POSIX pentru socket-uri din domeniul Unix.
- Proprietarul, grupul și modul de acces al socket-ului reprezintă limita
  actuală de autorizare.
- Nu există token de control la nivelul aplicației.
- Unitatea systemd instalată creează `/run/aismixer` numai cât timp serviciul
  rulează; directorul runtime nu reprezintă stare persistentă.
- Actualizările rutării la runtime sunt locale procesului și nu sunt păstrate
  după repornirea serviciului.

### 🧭 Planificat sau neimplementat

- Persistența stării de rutare la runtime.
- Urmărirea sau reîncărcarea automată a fișierului de configurare.
- Crearea sau eliminarea dinamică a adaptoarelor ingress ori egress.
- Coordonator multiprocessing și sincronizare IPC.
- Schimb de rutare P2P.
- API-uri de control HTTP sau TCP.
- Adaptoare egress MQTT, AMQP, MongoDB sau HTTP.
- Filtrare geografică, după navă sau MMSI.
- Detectarea spoofing-ului.
- Stocare și analiză AIS pe termen lung.

---

## 🔀 Arhitectură

AISMixer păstrează separate **planul de date** și **planul de control**.

### 📡 Planul de date

Planul de date primește date AIS și construiește un `IngressEvent` intern.
Limita de redirecționare ignoră un `raw_line` care nu este șir înainte de rutare
și extragere. Pentru fiecare eveniment acceptat al cărui `raw_line` este un șir,
planul de date preia un singur snapshot imuabil de rutare, potrivește `source_id`
o singură dată, extrage propozițiile NMEA, asamblează mesajele multipart, aplică
deduplicarea globală sau separată pe destinații, construiește metadatele TAG de
ieșire și redirecționează propozițiile acceptate spre destinațiile UDP egress.

- **Modul legacy:** deduplicare globală și broadcast către toate forwarderele.
- **Modul de rutare:** potrivirea sursei logice, deduplicare pentru fiecare
  destinație și redirecționare direcționată către destinații UDP egress denumite.
- Se preia un singur snapshot de rutare pentru fiecare `IngressEvent` acceptat
  al cărui `raw_line` este un șir; o actualizare de control concurentă afectează
  evenimentul următor, nu pe cel deja în curs de procesare.

### 🎛️ Planul de control

Când este activat, socket-ul local din domeniul Unix acceptă cereri JSON
delimitate prin linii noi. Serviciul de control validează o secțiune de rutare
candidată în raport cu ID-urile de destinație disponibile, compilează un tabel
imuabil nou și înlocuiește atomic starea de rutare locală procesului.

```text
aismixerctl
    ↓ NDJSON prin socket din domeniul Unix
RoutingControlProtocol
    ↓
RoutingControlService
    ↓
RoutingState (generație + snapshot imuabil)
    ↓
următorul IngressEvent
```

### 🧩 Componente principale

| Componentă | Rol |
|---|---|
| `aismixer.py` | Runtime principal, task-uri ingress, bucla mixerului, bucla de forwarding și ciclul de viață opțional al controlului |
| `core/routing.py` | Zone logice, operații pe mulțimi, rute și tabel imuabil de rutare |
| `core/routing_state.py` | Generație locală procesului și înlocuire thread-safe a snapshot-ului |
| `core/routing_control.py` | Serviciu independent de transport pentru status/replace/disable |
| `core/routing_control_protocol.py` | Contract JSON versionat pentru cereri și răspunsuri |
| `core/routing_control_unix.py` | Server asincron NDJSON prin socket din domeniul Unix |
| `core/routing_control_unix_client.py` | Client cu o cerere prin socket din domeniul Unix |
| `aismixerctl.py` | CLI pentru controlul rutării la runtime de către operator |
| `aismixer_secure.py` | Handshake UDPSEC, autentificare și decriptare |
| `nmea_sproxy/` | Proxy de rețea la stație: o intrare spre o intrare AISMixer UDPSEC sau UDP |
| `assembler.py` | Reasamblarea mesajelor multipart AIVDM/AIVDO |
| `dedup.py` | Suprimarea duplicatelor global sau separat pentru fiecare destinație |
| `meta_writer.py` / `meta_cleaner.py` | Ieșire NMEA TAG și curățarea ingress-ului |
| `forwarder.py` | Broadcast UDP și egress direcționat |

---

## 📐 Contractul de procesare

[BEHAVIORAL_CONTRACT.md](BEHAVIORAL_CONTRACT.md) este contractul normativ,
verificat prin teste, pentru implementarea Python de referință și baza pentru
viitoarea testare diferențială a unui procesor nativ. Acest README rămâne o
prezentare generală operațională, nu o copie a specificației normative.

Limita de redirecționare acceptă instanțe `str`, inclusiv subclase, și ignoră
payload-urile care nu sunt șiruri înainte de rutare și extragere. Asamblarea
multipart acceptă sosiri într-o ordine complet arbitrară: repetarea exactă a
unei propoziții la un ordinal deja ocupat este idempotentă, iar o propoziție
diferită la același ordinal invalidează generația activă a assemblerului.
Deduplicarea multipart este atomică la nivel de grup, iar starea TAG `s`, `c`
și `g` urmează limitele ciclului de viață al assemblerului. Fiecare eveniment
ingress acceptat folosește un singur snapshot imuabil de rutare.

---

## 🚀 Pornire rapidă: modul legacy broadcast

Când nu este configurată nicio secțiune top-level `routing:`, AISMixer își
păstrează comportamentul broadcast inițial:

- deduplicarea este globală;
- fiecare propoziție de ieșire acceptată este trimisă către fiecare forwarder
  configurat;
- forwarderele fără nume rămân valide;
- generațiile pentru controlul rutării pot exista, însă tabelul activ de rutare
  este dezactivat.

Exemplu minimal:

```yaml
station_id: mixstation_1

udp_inputs:
  - id: roof_receiver
    listen_ip: "0.0.0.0"
    listen_port: 17777

forwarders:
  - host: 203.0.113.10
    port: 5000
  - host: 127.0.0.1
    port: 19000
```

Rulare din repository:

```bash
python3 aismixer.py
```

### Controale pentru endpoint-urile de rețea

În configurația AISMixer sunt disponibile două controale opționale pentru
limita de rețea:

- `forwarders[].source_ip` asociază socket-ul unui forwarder UDP de ieșire cu o
  adresă-sursă IPv4 sau IPv6 literală. Când opțiunea este omisă, sistemul de
  operare alege adresa-sursă la fel ca înainte. Numele de host din
  `forwarders[].host` sunt rezolvate numai în familia de adrese selectată prin
  `source_ip`.
- `udp_inputs[].allow_from` și `sec_inputs[].allow_from` sunt liste de adrese
  ingress permise la nivelul aplicației. Când cheia este omisă, AISMixer nu
  aplică un ACL la nivelul aplicației. O listă explicit goală respinge toate
  pachetele pentru listener-ul respectiv. Intrările trebuie să fie adrese IP
  literale sau rețele CIDR; numele de host sunt respinse la pornire.

ACL-ul de ingress completează firewall-ul gazdei; nu înlocuiește firewall-ul,
rutarea sau politica la nivel de interfață.

```yaml
udp_inputs:
  - id: roof_receiver
    listen_ip: "0.0.0.0"
    listen_port: 17777
    allow_from:
      - 192.0.2.15
      - 198.51.100.0/24

sec_inputs:
  - id: secure_stations
    listen_ip: "::"
    listen_port: 19999
    allow_from:
      - 2001:db8:42::/64
      - 203.0.113.44

forwarders:
  - id: aishub
    host: feed.example.net
    port: 10110
    source_ip: 192.0.2.15
```

---

## 🗺️ Rutare logică statică

Modul de rutare este activat prin adăugarea unei secțiuni top-level `routing:`
valide.

În modul de rutare:

- potrivirea folosește `IngressEvent.source_id` intern;
- potrivirea **nu** folosește valoarea NMEA TAG `s` emisă;
- destinațiile rutelor trebuie să facă referire la forwardere denumite;
- ID-urile de destinație necunoscute sau nesuportate determină eșecul validării
  la pornire;
- zonele sunt mulțimi logice de ID-uri de sursă, nu zone AIS geografice;
- deduplicarea este separată pentru fiecare `target_id` logic.

### 🪪 ID-uri canonice de sursă și destinație

- `udp:<input-id>` când este configurat `udp_inputs[].id`.
- `udp:<mapped-alias>` când o hartă de aliasuri UDP furnizează identitatea.
- `udp:<remote-ip>` când nu este disponibil niciun ID UDP sau alias.
- `udpsec:<authenticated-station-id>` pentru o stație UDPSEC autentificată.
- `udp:<forwarder-id>` pentru un forwarder UDP denumit.

`sec_inputs[].id` poate influența aliasul TAG `s` emis când `station_id` global
este gol, dar nu înlocuiește ID-ul sursei de rutare pentru stația UDPSEC
autentificată.

### 🧮 Operații cu zone logice

```yaml
routing:
  zones:
    fixed_receivers:
      include:
        - udp:roof
        - udp:dock

    mobile_receivers:
      include:
        - udpsec:boat_ais

    trusted_sources:
      union:
        - fixed_receivers
        - mobile_receivers

    trusted_fixed_sources:
      intersection:
        - trusted_sources
        - fixed_receivers

    public_without_boat:
      difference:
        - trusted_sources
        - mobile_receivers
```

Operanzii pentru `union`, `intersection` și `difference` sunt numele altor zone
logice. Nu sunt coordonate, regiuni geografice, liste MMSI sau filtre pentru
nave.

Consultați [`examples/config-routing.yaml`](examples/config-routing.yaml) pentru
un exemplu complet, inactiv, de rutare statică.

---

## 🎛️ Controlul rutării la runtime

Serverul de control Unix rămâne dezactivat până când este activat explicit:

```yaml
control:
  unix:
    enabled: true
    socket_path: /run/aismixer/control.sock
    socket_mode: "0660"
    max_request_bytes: 1048576
```

### ⚠️ Note operaționale

- Simpla adăugare a `control:` sau `control.unix:` nu activează serverul.
- Unitatea systemd instalată folosește `RuntimeDirectory=aismixer` pentru a crea
  `/run/aismixer` înainte de pornirea AISMixer. systemd elimină acest director
  runtime după oprirea serviciului; el nu reprezintă stare persistentă.
- Dacă AISMixer rulează în afara unității systemd instalate, trebuie furnizat un
  director-părinte echivalent pentru calea configurată a socket-ului.
- Proprietarul, grupul și modul fișierului controlează accesul la socket.
- Serviciul continuă să ruleze cu aceeași identitate ca înainte. Această
  schimbare nu adaugă `User=`, `Group=`, `DynamicUser=` sau un cont de serviciu
  dedicat.
- Cu serviciul actual rulat ca root și `socket_mode: "0660"`, accesul poate fi
  efectiv limitat la root, dacă operatorul nu configurează în mod deliberat
  proprietarul sau politica de grup pentru socket.
- Nu există token de autentificare la nivelul aplicației.
- Interfața este numai pentru POSIX; Windows poate rula testele pure și codul de
  dezvoltare, dar nu listener-ul pentru socket-ul Unix.
- Modificările de rutare la runtime sunt locale procesului și dispar după
  repornire.

Consultați
[`examples/config-routing-control.yaml`](examples/config-routing-control.yaml)
pentru o configurație completă, inactivă, cu rutare statică și control la
runtime.

---

## 🧰 `aismixerctl`

Programul de instalare amplasează un mic wrapper POSIX în
`/usr/local/bin/aismixerctl`. Wrapper-ul execută
`/usr/bin/python3 /opt/aismixer/aismixerctl.py "$@"` și nu conține logică de
rutare sau de protocol.

Calea implicită a socket-ului este `/run/aismixer/control.sock`, astfel încât
un sistem instalat poate interoga starea cu:

```bash
aismixerctl status
```

Dintr-un checkout al repository-ului sau dintr-un director copiat al
serviciului, utilizați:

```bash
python3 aismixerctl.py status
```

Utilizați `--socket` pentru a suprascrie calea implicită:

```bash
aismixerctl --socket /custom/path.sock status
```

Înlocuiți snapshot-ul activ de rutare, local procesului:

```bash
aismixerctl \
  replace \
  --file examples/routing-update.yaml \
  --expected-generation 3
```

Dezactivați rutarea și readuceți procesul care rulează în modul legacy
broadcast:

```bash
aismixerctl \
  disable \
  --expected-generation 4
```

### 🔢 Semantica generațiilor

- `status` returnează generația curentă.
- `replace` și `disable` pot include o generație așteptată.
- O actualizare învechită este respinsă în loc să suprascrie un snapshot mai
  nou.
- CLI-ul nu reîncearcă automat.

`replace --file` acceptă fie:

1. o configurație completă care conține un mapping top-level `routing:`; fie
2. o secțiune directă de rutare care conține numai `zones:` și `routes:`.

`routing: null` nu reprezintă o cerere de înlocuire; utilizați `disable`.

Consultați [`examples/routing-update.yaml`](examples/routing-update.yaml) pentru
un fișier de actualizare care conține direct secțiunea de rutare.

---

## 🔐 Ieșirile `nmea_sproxy`

UDPSEC este transportul UDP autentificat și criptat folosit de AISMixer între
stație și mixer. Nu este un protocol extern standardizat. Stațiile se
autentifică prin ECDSA, iar datele AIS și mesajele de liveness folosesc criptare
AES-GCM autentificată. Cheile publice ale stațiilor autorizate sunt configurate
prin `authorized_keys.yaml`. UDPSEC protejează pachetele în tranzit; nu dovedește că
payload-ul AIS este veridic din punct de vedere semantic sau exact din punct de
vedere fizic.

`nmea_sproxy` este proxy-ul de la stație:

```text
o intrare locală (UDP sau serială) → o intrare AISMixer UDPSEC sau UDP
```

Exemple de comenzi:

```bash
cd nmea_sproxy
python3 nmea_sproxy.py
sudo systemctl start nmea_sproxy
sudo systemctl start nmea_sproxy@boat
```

Numele de template precum `boat`, `yacht` sau `balchik_roof` sunt etichete
alese de operator. Consultați
[`nmea_sproxy/README.md`](nmea_sproxy/README.md) pentru ghidul detaliat destinat
stației.

`nmea_sproxy` are propriile controale pentru endpoint-urile de la stație:
`allow_from` top-level limitează expeditorii UDP locali sau LAN care pot fi
redirecționați sub identitatea stației. `source_ip` top-level este asocierea
legacy a sursei UDPSEC; pentru mapări explicite în `output:`,
`output.source_ip` asociază cu o adresă-sursă literală socket-urile de ieșire
UDPSEC sau UDP simplu. Acestea sunt configurate în fișierele de relații
`nmea_sproxy`; sunt separate de controalele AISMixer `udp_inputs[]`,
`sec_inputs[]` și `forwarders[]`.

Pentru stațiile cu receptor AIS fizic, `nmea_sproxy` poate citi direct de la
un port serial sau USB virtual COM și poate redirecționa propozițiile NMEA
rezultate prin ieșirea UDPSEC sau UDP configurată. De asemenea, poate
redirecționa explicit UDP simplu pentru medii LAN/VPN de încredere; UDP simplu
nu oferă autentificare UDPSEC, criptare, protecție împotriva replay-ului sau
protocol de liveness.

---

## 🏷️ Comportamentul metadatelor NMEA TAG

AISMixer citește metadatele TAG de ingress și emite un bloc TAG `s`/`c`/`g`
controlat, conform opțiunilor runtime descrise mai jos.

Pentru grupurile multipart, contextul TAG `s`, `c` și `g` urmează limitele de
conflict, expirare și finalizare ale assemblerului. Regulile exacte de
proprietate și selecție sunt definite în
[BEHAVIORAL_CONTRACT.md](BEHAVIORAL_CONTRACT.md).

### 🪪 TAG `s` — eticheta sursei

Valoarea TAG `s` emisă este aleasă separat de `source_id` folosit pentru rutare.

Ordinea de prioritate:

1. `station_id` global nevid;
2. ID-ul intrării, aliasul UDP sau numele stației/clientului UDPSEC autorizat;
3. TAG `s` de ingress, când este prezent;
4. adresa IP remote ca fallback.

Valoarea emisă este sanitizată la `[A-Za-z0-9_]` și limitată la 15 caractere.
ID-urile surselor de rutare sunt identificatori interni opaci și nu sunt
sanitizate sau trunchiate precum valorile TAG `s`.

### 🕒 TAG `c` — marcaj temporal

Pentru grupurile multipart, `c_preserve_ingress_c: true` selectează valoarea
numerică minimă validă a TAG `c` de ingress observată în timpul generației
active a assemblerului, indiferent de ordinea sosirii. Când păstrarea este
dezactivată sau nu există o valoare validă, AISMixer emite timpul serverului.
Contractul comportamental consemnează excepția intenționată de compatibilitate
pentru `c:0` într-o propoziție individuală.

### 🧷 TAG `g` — metadatele grupului de ieșire

TAG `g` reprezintă metadate de ingress/ieșire pentru mesajele multipart. **Nu**
este cheia assemblerului. Asamblarea multipart folosește câmpurile fragmentelor
NMEA împreună cu identitatea assemblerului de ingress. ID-urile de grup păstrate
folosesc egalitatea exactă a șirurilor; observațiile absente sau discordante
produc un singur ID generat pentru grupul logic finalizat.

Opțiuni relevante:

```yaml
g_preserve_ingress_gid: true
g_id_digits: 18
g_always_tag_single: false
c_preserve_ingress_c: true
```

---

## 📦 Instalare

Rulare directă din repository:

```bash
python3 aismixer.py
```

Sau instalați serviciul systemd administrat de repository și wrapper-ul CLI
global:

```bash
./install.sh
```

Programul de instalare amplasează fișierele runtime în `/opt/aismixer`,
instalează `aismixer.service`, instalează `/usr/local/bin/aismixerctl`,
reîncarcă systemd și activează serviciul. Nu pornește automat AISMixer. Unitatea
folosește `RuntimeDirectory=aismixer`, astfel încât systemd creează
`/run/aismixer` cât timp serviciul rulează și îl elimină după oprirea
serviciului.

Serviciul instalat citește `/etc/aismixer/config.yaml`. La prima instalare,
`install.sh` inițializează `/etc/aismixer` din configurația repository-ului,
păstrând configurația și cheile existente ale operatorului.

Actualizați fișierele runtime instalate și reporniți serviciul cu:

```bash
./update.sh
```

`update.sh` nu modifică configurația și cheile operatorului din
`/etc/aismixer`.

Dezinstalați serviciul și fișierele runtime instalate cu:

```bash
./uninstall.sh
```

În mod implicit, dezinstalarea păstrează `/etc/aismixer`; opțiunea explicită
`./uninstall.sh --purge-config` elimină și configurația, și cheile operatorului.

---

## 📚 Exemple

Fișierele de exemplu nu produc efecte până când sunt copiate sau adaptate de un
operator:

- [`examples/config-routing.yaml`](examples/config-routing.yaml) — configurație
  completă pentru rutare statică.
- [`examples/config-routing-control.yaml`](examples/config-routing-control.yaml)
  — configurație completă de rutare cu `control.unix` activat.
- [`examples/routing-update.yaml`](examples/routing-update.yaml) — secțiune
  directă de rutare pentru `aismixerctl replace --file`.
- [`examples/README.md`](examples/README.md) — ghid scurt pentru fișierele de
  exemplu.

Toate adresele, ID-urile, porturile, căile și cheile din fișierele de exemplu
trebuie adaptate la mediul de instalare.

---

## 🧪 Testare

Suita de teste acoperă asamblarea multipart, gestionarea TAG, procesarea
metadatelor, componentele auxiliare UDPSEC, rutarea, înlocuirea snapshot-ului,
protocolul și transporturile de control, `aismixerctl` și comportamentul de
redirecționare.

```bash
python -m pytest
```

Testele reale pentru listener-ul din domeniul Unix necesită Linux, WSL,
Raspberry Pi OS sau alt mediu POSIX cu suport asyncio pentru socket-uri Unix.

---

## ⚠️ Limitări actuale

- UDP este adaptorul egress implementat în prezent.
- Starea și generațiile de rutare sunt locale procesului.
- Modificările de rutare efectuate la runtime nu sunt persistente.
- Nu există coordonator multiprocessing sau sincronizare între procese.
- Nu există reîncărcare sau urmărire automată a configurației.
- Nu există filtrare geografică, după MMSI sau după navă.
- Controlul Unix necesită suport POSIX pentru socket-uri din domeniul Unix.
- Controlul accesului se bazează pe permisiunile sistemului de fișiere Unix.
- Nu a fost introdusă încă o politică pentru un utilizator/grup de serviciu
  dedicat.

---

## 📖 Documentație suplimentară

- [Contract comportamental](BEHAVIORAL_CONTRACT.md)
- [Exemple](examples/README.md)
- [Ghidul operatorului pentru `nmea_sproxy`](nmea_sproxy/README.md)
- [GitHub Wiki](https://github.com/iliyan85/aismixer/wiki)
- [Ghid de contribuție](CONTRIBUTING.md)
- [Politica de securitate](SECURITY.md)
- [Foaia de parcurs a proiectului](ROADMAP.md)
- [Site public](https://aismixer.net)

[⬆ Înapoi la selectorul de limbă](#english)
