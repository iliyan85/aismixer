<a id="english"></a>

**English · [Български](#bulgarian) · [Română](#romanian)**

# 🛰️ AISMixer — AIS NMEA 0183 stream processor and routing engine

**Normalize · Deduplicate · Tag · Route · Forward**

[🌐 Website](https://aismixer.net) · [📚 Examples](examples/README.md) ·
[📐 Behavioural contract](BEHAVIORAL_CONTRACT.md) ·
[🔐 `nmea_sproxy` guide](nmea_sproxy/README.md) · [🗺️ Roadmap](ROADMAP.md)

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

### 📡 Ingress and egress

- UDP ingress over IPv4 and IPv6, with optional application-level allow-lists
  for AISMixer UDP/UDPSEC listeners and `nmea_sproxy` local UDP input.
- Authenticated encrypted UDPSEC ingress compatible with `nmea_sproxy`.
- Physical serial and USB virtual COM input through `nmea_sproxy`, plus an
  explicit plain UDP mode for trusted LAN/VPN environments.
- Optional outbound source-address binding for AISMixer UDP forwarders and
  `nmea_sproxy` UDPSEC/plain UDP outputs.
- UDP broadcast egress in legacy mode and named UDP targets in routing mode.

### ⚙️ Processing

- Extraction of supported `!AIVDM` and `!AIVDO` sentences.
- Fully out-of-order multipart assembly, with exact repeats treated
  idempotently for assembly and conflicting fragments invalidating the live
  group.
- Deterministic, lifecycle-aware NMEA TAG `s`/`c`/`g` handling.
- Group-atomic multipart deduplication decisions: global in legacy mode and
  scoped to each `target_id` in routing mode.
- Explicit, tested process-local TTL lifecycles for deduplication, assembly,
  forwarding-owned multipart metadata, and secure ingress state.
- Optional constructor-level process-local limits in the Python reference state
  objects, plus immutable statistics snapshots for deduplication, assembly, and
  secure state.

### 🔀 Routing and operation

- Legacy broadcast mode or static logical routing loaded at startup.
- Named ingress `source_id` and egress `target_id` identities.
- Logical source zones with `include`, `union`, `intersection`, and
  `difference`, consumed by ordered routes.
- One immutable routing snapshot per accepted string `IngressEvent`.
- Optional atomic runtime routing replacement through the Unix-domain NDJSON
  control plane and `aismixerctl`.
- `routing.status`, `routing.replace`, and `routing.disable`.
- Repository-managed systemd service with `RuntimeDirectory=aismixer` and a
  global `/usr/local/bin/aismixerctl` wrapper installed by lifecycle scripts.

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

### 🧩 Main components

| Component | Role |
|---|---|
| `aismixer.py` | Main runtime, ingress tasks, mixer loop, forwarding loop, optional control lifecycle |
| `core/routing.py` / `core/routing_state.py` | Logical routing and process-local immutable snapshots |
| `core/routing_control*.py` | Versioned control protocol, service, and Unix-domain transport |
| `aismixerctl.py` | Operator CLI for runtime routing control |
| `aismixer_secure.py` | UDPSEC handshake, authentication, and decryption |
| `nmea_sproxy/` | Station-side network proxy: one input to one AISMixer UDPSEC or UDP input |
| `assembler.py` | Multipart `!AIVDM`/`!AIVDO` reassembly |
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
idempotent for assembly, while a different sentence at that ordinal invalidates
the live assembler generation. Multipart deduplication decisions are
group-atomic, and multipart TAG `s`, `c`, and `g` state follows assembler
lifecycle boundaries. Each accepted ingress event uses one immutable routing
snapshot.

Deduplication state, multipart assembly state, forwarding-owned multipart
metadata, and secure replay/session/nonce state have explicit, deterministic,
process-local TTL lifecycles. The Python reference state objects accept an
optional deduplication `max_entries` capacity and optional assembly
`max_fragments_per_group` and `max_pending_groups` limits. Current service
wiring uses their `None` defaults, leaving those dimensions unbounded. When
capacity admission applies, expired state is handled before deterministic
eviction of live state. The deduplication, assembly, and secure state objects
expose immutable statistics snapshots for inspection and testing. Exact
lifecycle boundaries and edge cases remain in the behavioural contract.

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
- The installed unit sets no `User=`, `Group=`, or `DynamicUser=` and has no
  dedicated service account.
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

Run the CLI as a socket-authorized user. With the default root-owned installed
socket, this normally means:

```bash
sudo aismixerctl status
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
sudo aismixerctl \
  replace \
  --file examples/routing-update.yaml \
  --expected-generation 3
```

Disable routing and return the running process to legacy broadcast mode:

```bash
sudo aismixerctl \
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
and ECDH-derived session keys let the current `nmea_sproxy` protect AIS and
ping/pong traffic with authenticated AES-GCM encryption. Authorized station
public keys are configured through `authorized_keys.yaml`. Handshake-replay
records, active sessions, and per-session data-nonce records use bounded,
TTL-managed process-local state. Expiry cleanup is driven by allowed traffic
rather than a background timer, and secure replay/session/nonce state is lost
when the service restarts. UDPSEC protects packets in transit; it does not prove
that the AIS payload itself is semantically true or physically accurate.

See the [`nmea_sproxy` guide](nmea_sproxy/README.md), [security
policy](SECURITY.md), [behavioural contract](BEHAVIORAL_CONTRACT.md), and
[Wiki](https://github.com/iliyan85/aismixer/wiki) for protocol operation,
security scope, exact state semantics, and deeper background.

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
expiry, capacity-eviction, and completion boundaries. The exact ownership and
selection rules are defined in
[BEHAVIORAL_CONTRACT.md](BEHAVIORAL_CONTRACT.md).

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

Install the repository-managed systemd service and global CLI wrapper:

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

- UDP is the only currently implemented AISMixer egress adapter.
- Routing state and generations are process-local; runtime changes are not
  persisted.
- Secure replay, session, and nonce state is process-local and non-durable;
  expiry cleanup is traffic-driven rather than background-timer-driven.
- Current service wiring leaves the optional deduplication and assembly limits
  at `None`, so those dimensions have no capacity bound.
- There is no multiprocessing coordinator or cross-process synchronization.
- There is no automatic config reload/watch.
- There is no geographic, MMSI, or vessel-content filtering.
- There is no long-term storage, analytics, or spoof detection.
- Unix control requires POSIX Unix-domain socket support.
- Control access relies on Unix filesystem permissions, with no
  application-level token.
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

**Нормализация · Дедупликация · TAG метаданни · Маршрутизация · Препращане**

[🌐 Уебсайт](https://aismixer.net) · [📚 Примери](examples/README.md) ·
[📐 Договор за поведение](BEHAVIORAL_CONTRACT.md) ·
[🔐 Ръководство за `nmea_sproxy`](nmea_sproxy/README.md) ·
[🗺️ План за развитие](ROADMAP.md)

> ### ⚡ Накратко
> AISMixer приема AIS потоци от няколко приемника, извлича `!AIVDM` и `!AIVDO`,
> сглобява multipart съобщения, премахва близки във времето дубликати, управлява
> NMEA TAG метаданните и препраща един чист логически поток. По желание
> логическата маршрутизация насочва всеки входен източник към избрани именувани
> UDP цели, а `aismixerctl` може атомарно да замени или изключи активната
> моментна снимка на маршрутизацията през локален Unix-domain socket за
> управление.

---

## 🌿 Клонове и уебсайт

Клонът `main` е основният клон за работа и разработка. В него са Python
услугата, компонентите на защитеното мрежово прокси, конфигурационните примери,
модулите на слоя за управление и тестовете в `tests/`.

Публичният сайт е в дългоживеещия клон `website`. GitHub Pages се публикува от
него с `/docs` като основна директория на сайта, затова `docs/` умишлено не
присъства в `main`.

---

## 🧭 Какво е AISMixer?

**AISMixer** е Python услуга за приемане, нормализиране, дедупликация, обработка
на TAG метаданни, маршрутизация и препращане на AIS NMEA 0183 потоци.

- **`aismixer.py`** е дългосрочно работещата смесваща услуга от слоя за данни.
- **`nmea_sproxy`** е мрежовото прокси при станцията. Един процес препраща един
  локален UDP или физически сериен вход към един UDPSEC или UDP вход на
  AISMixer.
- **`aismixerctl.py`** е операторският инструмент за команден ред към
  незадължителния локален сокет за управление на маршрутизацията.

```text
AIS приемник UDP      \
AIS приемник UDP       \        +----------------+       +----------------+
nmea_sproxy UDPSEC/UDP ------> |    AISMixer    | ----> | UDP цели       |
                                | слой за данни  |       +----------------+
                                +----------------+
                                         ^
                                         |
                            незадължителен Unix слой
                                  за управление
                                         |
                                   aismixerctl
```

---

## ✅ Текущи възможности

### 📡 Входове и изходи

- UDP входове по IPv4 и IPv6 с незадължителни списъци с разрешени адреси за
  UDP/UDPSEC входовете на AISMixer и локалния UDP вход на `nmea_sproxy`.
- Автентикиран и криптиран UDPSEC вход, съвместим с `nmea_sproxy`.
- Физически сериен порт и USB virtual COM вход чрез `nmea_sproxy`, както и
  изричен режим с plain UDP за доверени LAN/VPN среди.
- Незадължително обвързване с изходен адрес за UDP forwarder-ите на AISMixer и
  за UDPSEC и plain UDP изходите на `nmea_sproxy`.
- Broadcast UDP изход в legacy режим и именувани UDP цели в routing режим.

### ⚙️ Обработка

- Извличане на поддържаните `!AIVDM` и `!AIVDO` изречения.
- Сглобяване на multipart съобщения независимо от реда на пристигане; точните
  повторения са идемпотентни за сглобяването, а конфликтните фрагменти
  обезсилват активната група.
- Детерминистична обработка на NMEA TAG `s`/`c`/`g`, съобразена с жизнения цикъл.
- Решения за дедупликация, атомарни за цялата multipart група: глобални в legacy
  режим и отделни за всеки `target_id` в routing режим.
- Изрични, локални за процеса TTL жизнени цикли, проверени с тестове, за
  дедупликацията, сглобяването, multipart метаданните, управлявани от слоя за
  препращане, и състоянието на защитения вход.
- Незадължителни ограничения, задавани чрез конструктора на обектите за локално
  състояние в референтната Python реализация, както и неизменяеми моментни
  снимки на статистиката за дедупликация, сглобяване и защитено състояние.

### 🔀 Маршрутизация и експлоатация

- Legacy broadcast режим или статична логическа маршрутизация, заредена при
  стартиране.
- Именувани входни `source_id` и изходни `target_id` идентификатори.
- Логически зони на източници с `include`, `union`, `intersection` и
  `difference`, използвани от подредени маршрути.
- Една неизменяема моментна снимка на маршрутизацията за всеки приет
  `IngressEvent`, чийто `raw_line` е `str`.
- Незадължителна атомарна подмяна на маршрутизацията по време на работа чрез
  Unix-domain socket с NDJSON протокол и `aismixerctl`.
- `routing.status`, `routing.replace` и `routing.disable`.
- Поддържана в хранилището systemd услуга с `RuntimeDirectory=aismixer` и
  глобален `/usr/local/bin/aismixerctl` wrapper, инсталиран от скриптовете за
  жизнения цикъл.

---

## 🔀 Архитектура

AISMixer разделя **слоя за данни** (`data plane`) и **слоя за управление**
(`control plane`).

### 📡 Слой за данни

Слоят за данни приема AIS данните и създава вътрешен `IngressEvent`. Границата
за препращане пренебрегва `raw_line`, който не е `str`, преди маршрутизирането и
извличането. За всяко прието събитие слоят взема една неизменяема моментна
снимка на маршрутизацията, съпоставя `source_id` веднъж, извлича NMEA
изреченията, сглобява multipart съобщенията, прилага глобална или отделна за
всяка цел дедупликация, изгражда изходните TAG метаданни и препраща приетите
изречения към UDP дестинациите.

- **Legacy режим:** глобална дедупликация и broadcast към всички forwarder-и.
- **Routing режим:** логическо съпоставяне на източника, дедупликация по цел и
  целево препращане към именувани UDP дестинации.
- За всеки приет `IngressEvent`, чийто `raw_line` е `str`, се взема една моментна
  снимка на маршрутизацията; паралелна промяна в слоя за управление засяга
  следващото събитие, а не вече обработваното.

### 🎛️ Слой за управление

При включване локалният Unix-domain socket приема JSON заявки, разделени с нов
ред. Услугата за управление проверява предложената секция за маршрутизация спрямо
наличните `target_id`, компилира нова неизменяема таблица и атомарно заменя
локалното за процеса състояние на маршрутизацията.

### 🧩 Основни компоненти

| Компонент | Роля |
|---|---|
| `aismixer.py` | Основен процес, задачи за входовете, цикли за смесване и препращане, жизнен цикъл на незадължителния слой за управление |
| `core/routing.py` / `core/routing_state.py` | Логическа маршрутизация и неизменяеми моментни снимки, локални за процеса |
| `core/routing_control*.py` | Версиониран протокол, услуга за управление и Unix-domain транспорт |
| `aismixerctl.py` | Операторски CLI за управление на маршрутизацията по време на работа |
| `aismixer_secure.py` | UDPSEC handshake, автентикация и декриптиране |
| `nmea_sproxy/` | Мрежово прокси при станцията: един вход към един UDPSEC или UDP вход на AISMixer |
| `assembler.py` | Сглобяване на multipart `!AIVDM`/`!AIVDO` |
| `dedup.py` | Глобална дедупликация или дедупликация по цел |
| `meta_writer.py` / `meta_cleaner.py` | NMEA TAG изход и почистване на входа |
| `forwarder.py` | UDP broadcast и целево препращане |

---

## 📐 Договор за обработка

[BEHAVIORAL_CONTRACT.md](BEHAVIORAL_CONTRACT.md) е нормативният, проверен с
тестове договор за поведението на референтната Python реализация и основата за
бъдещо диференциално тестване на нативен процесор. Този README остава
оперативен преглед, а не дублирана нормативна спецификация.

Границата за препращане приема екземпляри на `str`, включително негови
подкласове, и пренебрегва останалите стойности преди маршрутизирането и
извличането. Multipart сглобяването поддържа фрагменти в напълно произволен ред:
точно повторение на изречението на вече заета поредна позиция е идемпотентно за
сглобяването, а различно изречение на същата позиция обезсилва активното
поколение на assembler-а. Решенията за multipart дедупликация са атомарни за
цялата група, а състоянието на TAG `s`, `c` и `g` следва границите на жизнения
цикъл на assembler-а. Всеки приет `IngressEvent` се обработва с една
неизменяема моментна снимка на маршрутизацията.

Състоянието за дедупликация, състоянието за multipart сглобяване, multipart
метаданните, управлявани от слоя за препращане, и защитеното състояние за replay,
сесии и nonce имат изрични, детерминирани TTL жизнени цикли, локални за процеса.
Обектите за състояние на референтната Python реализация приемат незадължителен
лимит `max_entries` за дедупликацията и незадължителни ограничения
`max_fragments_per_group` и `max_pending_groups` за сглобяването. Текущата
интеграция на услугата използва стойностите им по подразбиране `None`, което
оставя тези измерения без горна граница. Когато се прилага ограничение на
капацитета, първо се обработва изтеклото състояние и едва след това
детерминистично се премахва активен запис. Обектите за състояние на
дедупликацията, сглобяването и защитения вход предоставят неизменяеми моментни
снимки на статистиката за инспекция и тестване. Точните граници на жизнения
цикъл и граничните случаи остават в договора за поведение.

---

## 🚀 Бърз старт: legacy broadcast режим

Когато на най-горното ниво няма секция `routing:`, AISMixer запазва
първоначалното broadcast поведение:

- дедупликацията е глобална;
- всяко прието изходно изречение се изпраща към всички forwarder-и;
- forwarder-и без `id` остават валидни;
- може да има поколения за управление на маршрутизацията, но активната таблица
  за маршрутизация е изключена.

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

Стартиране от локално копие на хранилището:

```bash
python3 aismixer.py
```

### Контрол на мрежовите крайни точки

В конфигурацията на AISMixer има два незадължителни механизма за контрол на
мрежовата граница:

- `forwarders[].source_ip` обвързва сокета на изходния UDP препращач с конкретно
  зададен IPv4 или IPv6 адрес на източника. Когато ключът е пропуснат,
  операционната система избира адреса както досега. Имената на хостове във
  `forwarders[].host` се преобразуват само до адреси от семейството, избрано от
  `source_ip`.
- `udp_inputs[].allow_from` и `sec_inputs[].allow_from` са списъци с разрешени
  входни адреси на ниво приложение. Когато ключът е пропуснат, AISMixer не
  прилага такъв ACL. Явно празен списък отказва всички пакети за съответния
  вход. Записите трябва да са буквални IP адреси или CIDR мрежи; имена на
  хостове се отхвърлят при стартиране.

Входният ACL допълва firewall-а на хоста; не заменя правилата на firewall-а,
маршрутизацията или политиката на мрежовия интерфейс.

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

Режимът за маршрутизация се включва с валидна секция `routing:` на най-горното
ниво.

В този режим:

- съпоставянето използва вътрешния `IngressEvent.source_id`;
- съпоставянето **не** използва излъчения NMEA TAG `s`;
- целите на маршрутите трябва да сочат към именувани forwarder-и;
- при неизвестен или неподдържан `target_id` проверката при стартиране завършва
  с грешка;
- зоните са логически множества от `source_id`, а не географски AIS области;
- дедупликацията се изпълнява по отделен логически `target_id`.

### 🪪 Канонични `source_id` и `target_id`

- `udp:<input-id>` при конфигуриран `udp_inputs[].id`.
- `udp:<mapped-alias>` при идентификатор от картата с UDP псевдоними.
- `udp:<remote-ip>` когато няма UDP ID или псевдоним.
- `udpsec:<authenticated-station-id>` за автентикирана UDPSEC станция.
- `udp:<forwarder-id>` за именуван UDP forwarder.

`sec_inputs[].id` може да влияе на псевдонима в излъчения TAG `s`, когато
глобалният `station_id` е празен, но не заменя автентикирания UDPSEC
идентификатор за маршрутизация.

### 🧮 Операции върху логически зони

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
зони. Те не са координати, географски области, MMSI списъци или филтри по
плавателен съд.

Вижте [`examples/config-routing.yaml`](examples/config-routing.yaml) за неактивен
пълен пример със статична маршрутизация.

---

## 🎛️ Управление на маршрутизацията по време на работа

Unix сървърът за управление остава изключен, докато не бъде включен изрично:

```yaml
control:
  unix:
    enabled: true
    socket_path: /run/aismixer/control.sock
    socket_mode: "0660"
    max_request_bytes: 1048576
```

### ⚠️ Оперативни бележки

- Самото добавяне на `control:` или `control.unix:` не включва сървъра.
- Инсталираният systemd unit използва `RuntimeDirectory=aismixer`, за да създаде
  `/run/aismixer` преди старта на AISMixer. systemd премахва тази работна
  директория след спиране на услугата; тя не е постоянно състояние.
- Ако AISMixer се стартира извън инсталирания systemd unit, осигурете
  еквивалентна родителска директория за конфигурирания път до сокета.
- Собственикът, групата и режимът на файловата система управляват достъпа до
  сокета.
- Unit-ът не задава `User=`, `Group=` или `DynamicUser=` и услугата няма
  отделен служебен акаунт.
- При текущата услуга, работеща като root, и `socket_mode: "0660"` достъпът може
  на практика да е само за root, освен ако операторът умишлено не настрои
  собствеността или груповата политика за сокета.
- Няма маркер за автентикация на ниво приложение.
- Интерфейсът е само за POSIX. Windows може да изпълнява чистите тестове и кода
  за разработка, но не и слушателя на Unix-domain сокета.
- Промените в маршрутизацията по време на работа са локални за процеса и
  изчезват след рестарт.

Вижте
[`examples/config-routing-control.yaml`](examples/config-routing-control.yaml)
за неактивна пълна конфигурация със статична маршрутизация и управление по време
на работа.

---

## 🧰 `aismixerctl`

Инсталаторът поставя малък обвиващ POSIX скрипт в
`/usr/local/bin/aismixerctl`. Скриптът изпълнява
`/usr/bin/python3 /opt/aismixer/aismixerctl.py "$@"` и не съдържа логика за
маршрутизация или протокол.

CLI трябва да се изпълнява от потребител с достъп до сокета. При инсталирания
по подразбиране сокет, който е собственост на root, това обикновено означава:

```bash
sudo aismixerctl status
```

От локално копие на хранилището или копирана директория на услугата използвайте:

```bash
python3 aismixerctl.py status
```

Използвайте `--socket`, за да зададете друг път:

```bash
aismixerctl --socket /custom/path.sock status
```

Подмяна на активната, локална за процеса моментна снимка на маршрутизацията:

```bash
sudo aismixerctl \
  replace \
  --file examples/routing-update.yaml \
  --expected-generation 3
```

Изключване на маршрутизацията и връщане на работещия процес към legacy
broadcast режим:

```bash
sudo aismixerctl \
  disable \
  --expected-generation 4
```

### 🔢 Семантика на поколенията

- `status` връща текущото поколение.
- `replace` и `disable` могат да съдържат очаквано поколение.
- Остаряла актуализация се отхвърля, вместо да презапише по-нова моментна
  снимка.
- CLI не прави автоматични повторни опити.

`replace --file` приема:

1. пълна конфигурация с `routing:` mapping на най-горното ниво; или
2. директна секция за маршрутизация само с `zones:` и `routes:`.

`routing: null` не е заявка за подмяна; използвайте `disable`.

Вижте [`examples/routing-update.yaml`](examples/routing-update.yaml) за директен
файл за актуализиране на секцията за маршрутизация.

---

## 🔐 Изходи на `nmea_sproxy`

UDPSEC е автентикираният и криптиран UDP транспорт между станцията и AISMixer.
Това не е външен стандартизиран протокол. Станциите се автентикират с ECDSA, а
ключовете за сесия, изведени чрез ECDH, позволяват на текущия `nmea_sproxy` да
защитава AIS и ping/pong трафика с автентикирано AES-GCM криптиране.
Разрешените публични ключове на станциите се конфигурират чрез
`authorized_keys.yaml`.
Записите за защита от повторение на handshake съобщения, записите за активни
сесии и nonce стойностите за данни във всяка сесия са ограничени по брой,
управляват се чрез TTL и са локални за процеса. Почистването на изтеклите записи
се задейства от разрешен трафик, а не от фонов таймер; записите и nonce
стойностите се губят при рестарт на услугата. UDPSEC защитава
пакетите при пренос, но не доказва, че самото AIS съдържание е семантично вярно
или физически точно.

За работата на протокола, обхвата на сигурността, точната семантика на
състоянието и по-подробен контекст вижте [ръководството за
`nmea_sproxy`](nmea_sproxy/README.md), [политиката за сигурност](SECURITY.md),
[договора за поведение](BEHAVIORAL_CONTRACT.md) и
[Wiki](https://github.com/iliyan85/aismixer/wiki).

`nmea_sproxy` е мрежовото прокси при станцията:

```text
един локален вход (UDP или сериен) → един UDPSEC или UDP вход на AISMixer
```

Примерни команди:

```bash
cd nmea_sproxy
python3 nmea_sproxy.py
sudo systemctl start nmea_sproxy
sudo systemctl start nmea_sproxy@boat
```

Имената на шаблони като `boat`, `yacht` или `balchik_roof` са етикети, избрани
от оператора. Подробното ръководство е в
[`nmea_sproxy/README.md`](nmea_sproxy/README.md).

`nmea_sproxy` има собствени механизми за крайните точки при станцията:
`allow_from` на най-горното ниво ограничава локалните/LAN UDP податели, които
могат да бъдат препратени под идентичността на станцията. `source_ip` на
най-горното ниво е legacy обвързването на UDPSEC източника; при изрично зададен
`output:` mapping, `output.source_ip` обвързва UDPSEC или plain UDP изходните
sockets към буквално зададен адрес на източника. Тези настройки са във
файловете за връзки на `nmea_sproxy` и са отделни от AISMixer настройките
`udp_inputs[]`, `sec_inputs[]` и `forwarders[]`.

За станции с физически AIS приемник `nmea_sproxy` може да чете директно от
сериен порт или USB virtual COM и да препраща получените NMEA изречения през
конфигурирания UDPSEC или UDP изход. Може също изрично да препраща plain UDP за
доверени LAN/VPN среди; plain UDP не предоставя UDPSEC автентикация, криптиране,
защита от replay или протокол за liveness.

---

## 🏷️ Поведение на NMEA TAG метаданните

AISMixer чете входните TAG метаданни и излъчва контролиран `s`/`c`/`g` TAG block
според описаните по-долу настройки.

За multipart групите контекстът на TAG `s`, `c` и `g` следва границите на
сглобяването при конфликт, изтичане, премахване поради ограничение на капацитета
и завършване. Точните правила за притежание и избор са определени в
[BEHAVIORAL_CONTRACT.md](BEHAVIORAL_CONTRACT.md).

### 🪪 TAG `s` — етикет на източника

Излъченият TAG `s` се избира отделно от `source_id` за маршрутизация.

Приоритет:

1. непразен глобален `station_id`;
2. ID на входа, UDP псевдоним или име на разрешената UDPSEC станция/клиент;
3. входящ TAG `s`, когато е наличен;
4. отдалеченият IP като резервна стойност.

Излъчената стойност се филтрира до `[A-Za-z0-9_]` и се ограничава до 15 символа.
Идентификаторите `source_id` за маршрутизация са непрозрачни вътрешни стойности
и не се филтрират или съкращават като TAG `s`.

### 🕒 TAG `c` — времева отметка

За multipart групите `c_preserve_ingress_c: true` избира минималната валидна
числова стойност на входящ TAG `c`, наблюдавана през активното поколение на
сглобяването, независимо от реда на пристигане. Когато запазването е изключено
или няма валидна стойност, AISMixer излъчва сървърното време. Договорът за
поведение описва умишленото изключение за съвместимост при единично изречение
с `c:0`.

### 🧷 TAG `g` — метаданни за изходната група

TAG `g` представлява входни/изходни метаданни за multipart съобщения. Той
**не** е ключът за сглобяване. Multipart сглобяването използва полетата на NMEA
фрагментите заедно с идентичността на входния контекст за сглобяване. Запазените
идентификатори на групи се сравняват като точни низове; липсващи или
несъгласувани стойности водят до един генериран идентификатор за завършената
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

Инсталиране на поддържаната в хранилището systemd услуга и глобалния CLI
wrapper:

```bash
./install.sh
```

Инсталаторът разполага файловете за изпълнение в `/opt/aismixer`, инсталира
`aismixer.service` и `/usr/local/bin/aismixerctl`, презарежда systemd и активира
услугата. Той не стартира AISMixer автоматично. Unit-ът използва
`RuntimeDirectory=aismixer`, така че systemd създава `/run/aismixer`, докато
услугата работи, и премахва директорията след спиране.

Инсталираната услуга чете `/etc/aismixer/config.yaml`. При първа инсталация
`install.sh` попълва `/etc/aismixer` от конфигурацията в хранилището и запазва
съществуващите операторски конфигурации и ключове.

Обновяване на инсталираните файлове за изпълнение и рестартиране на услугата:

```bash
./update.sh
```

`update.sh` не променя операторските конфигурации и ключове в `/etc/aismixer`.

Премахване на услугата и инсталираните файлове за изпълнение:

```bash
./uninstall.sh
```

По подразбиране деинсталирането запазва `/etc/aismixer`; изричната опция
`./uninstall.sh --purge-config` премахва и операторските конфигурации и ключове.

---

## 📚 Примери

Примерите са неактивни, докато операторът не ги копира или адаптира:

- [`examples/config-routing.yaml`](examples/config-routing.yaml) — пълна
  конфигурация за статична маршрутизация.
- [`examples/config-routing-control.yaml`](examples/config-routing-control.yaml)
  — пълна конфигурация за маршрутизация с включен `control.unix`.
- [`examples/routing-update.yaml`](examples/routing-update.yaml) — директна
  секция за маршрутизация за `aismixerctl replace --file`.
- [`examples/README.md`](examples/README.md) — кратко описание на примерните
  файлове.

Всички адреси, идентификатори, портове, пътища и ключове в примерните файлове
трябва да се адаптират към конкретната инсталация.

---

## 🧪 Тестове

Тестовете покриват multipart сглобяването, обработката на TAG и метаданни,
помощните UDPSEC компоненти, маршрутизацията, подмяната на моментни снимки,
протокола и транспортите за управление, `aismixerctl` и поведението при
препращане.

```bash
python -m pytest
```

Тестовете с действителен Unix-domain слушател изискват Linux, WSL, Raspberry Pi
OS или друга POSIX среда с поддръжка на asyncio Unix сокети.

---

## ⚠️ Текущи ограничения

- UDP е единственият реализиран в момента изходен адаптер на AISMixer.
- Състоянието и поколенията на маршрутизацията са локални за процеса; промените
  по време на работа не се запазват.
- Защитеното състояние за replay, сесии и nonce е локално за процеса и не е
  трайно; почистването при изтичане се задейства от трафик, а не от фонов таймер.
- Текущата интеграция на услугата оставя незадължителните ограничения за
  дедупликация и сглобяване на `None`, затова тези измерения нямат ограничение
  на капацитета.
- Няма многопроцесен координатор или синхронизация между процеси.
- Няма автоматично следене или презареждане на конфигурацията.
- Няма географско филтриране, филтриране по MMSI или по съдържание за
  плавателен съд.
- Няма дългосрочно съхранение, анализи или откриване на spoofing.
- Unix управлението изисква POSIX поддръжка за Unix-domain socket.
- Контролът на достъпа разчита на Unix разрешенията на файловата система и няма
  token на ниво приложение.
- Все още няма политика с отделни потребител и група за услугата.

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

[🌐 Site web](https://aismixer.net) · [📚 Exemple](examples/README.md) ·
[📐 Contract comportamental](BEHAVIORAL_CONTRACT.md) ·
[🔐 Ghid `nmea_sproxy`](nmea_sproxy/README.md) · [🗺️ Foaie de parcurs](ROADMAP.md)

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

### 📡 Intrări și ieșiri

- Ingress UDP prin IPv4 și IPv6, cu liste opționale de adrese permise pentru
  intrările UDP/UDPSEC AISMixer și intrarea UDP locală a `nmea_sproxy`.
- Ingress UDPSEC autentificat și criptat, compatibil cu `nmea_sproxy`.
- Intrare serială fizică și USB virtual COM prin `nmea_sproxy`, plus un mod UDP
  simplu, explicit, pentru medii LAN/VPN de încredere.
- Asocierea opțională a adresei-sursă pentru forwarderele UDP AISMixer și
  ieșirile UDPSEC/UDP simplu ale `nmea_sproxy`.
- Egress UDP broadcast în modul legacy și destinații UDP denumite în modul de
  rutare.

### ⚙️ Procesare

- Extragerea propozițiilor `!AIVDM` și `!AIVDO` acceptate.
- Asamblare multipart complet independentă de ordinea sosirii; repetările
  identice sunt idempotente pentru asamblare, iar fragmentele conflictuale
  invalidează grupul activ.
- Gestionare deterministă a NMEA TAG `s`/`c`/`g`, care respectă ciclul de viață.
- Decizii de deduplicare atomice la nivelul întregului grup multipart: globale
  în modul legacy și separate pentru fiecare `target_id` în modul de rutare.
- Cicluri de viață TTL locale procesului, explicite și verificate prin teste,
  pentru deduplicare, asamblare, metadatele multipart gestionate de componenta
  de forwarding și starea de ingress securizat.
- Limite locale procesului, opționale la nivel de constructor, în obiectele de
  stare Python de referință, plus snapshot-uri statistice imuabile pentru
  deduplicare, asamblare și starea securizată.

### 🔀 Rutare și operare

- Mod legacy broadcast sau rutare logică statică încărcată la pornire.
- Identități denumite `source_id` pentru ingress și `target_id` pentru egress.
- Zone logice de surse cu `include`, `union`, `intersection` și `difference`,
  consumate de rute ordonate.
- Un singur snapshot imuabil de rutare pentru fiecare `IngressEvent` acceptat
  al cărui `raw_line` este un șir.
- Înlocuirea atomică opțională a rutării la runtime prin planul de control
  NDJSON pe socket din domeniul Unix și `aismixerctl`.
- `routing.status`, `routing.replace` și `routing.disable`.
- Serviciu systemd administrat de repository cu `RuntimeDirectory=aismixer` și
  wrapper global `/usr/local/bin/aismixerctl` instalat de scripturile ciclului
  de viață.

---

## 🔀 Arhitectură

AISMixer păstrează separate **planul de date** și **planul de control**.

### 📡 Planul de date

Planul de date primește date AIS și construiește un `IngressEvent` intern.
Limita de redirecționare ignoră un `raw_line` care nu este un șir înainte de
rutare și extragere. Pentru fiecare eveniment acceptat al cărui `raw_line` este
un șir, planul de date preia un singur snapshot imuabil de rutare, potrivește
`source_id` o singură dată, extrage propozițiile NMEA, asamblează mesajele multipart, aplică
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

### 🧩 Componente principale

| Componentă | Rol |
|---|---|
| `aismixer.py` | Runtime principal, task-uri ingress, bucla mixerului, bucla de forwarding și ciclul de viață opțional al controlului |
| `core/routing.py` / `core/routing_state.py` | Rutare logică și snapshot-uri imuabile locale procesului |
| `core/routing_control*.py` | Protocol de control versionat, serviciu și transport prin socket din domeniul Unix |
| `aismixerctl.py` | CLI pentru controlul rutării la runtime de către operator |
| `aismixer_secure.py` | Handshake UDPSEC, autentificare și decriptare |
| `nmea_sproxy/` | Proxy de rețea la stație: o intrare spre o intrare AISMixer UDPSEC sau UDP |
| `assembler.py` | Reasamblarea mesajelor multipart `!AIVDM`/`!AIVDO` |
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
unei propoziții la un ordinal deja ocupat este idempotentă pentru asamblare, iar
o propoziție diferită la același ordinal invalidează generația activă a
assemblerului. Deciziile de deduplicare multipart sunt atomice la nivel de grup,
iar starea TAG `s`, `c` și `g` urmează limitele ciclului de viață al
assemblerului. Fiecare eveniment ingress acceptat folosește un singur snapshot
imuabil de rutare.

Starea de deduplicare, starea asamblării multipart, metadatele multipart
gestionate de componenta de forwarding și starea securizată pentru replay,
sesiuni și nonce-uri au cicluri de viață TTL explicite, deterministe și locale
procesului. Obiectele de stare Python de referință acceptă pentru deduplicare
capacitatea opțională `max_entries`, iar pentru asamblare limitele opționale
`max_fragments_per_group` și `max_pending_groups`. Integrarea actuală a
serviciului folosește valorile implicite `None`, lăsând aceste dimensiuni fără
limită. Când se aplică o limită de capacitate, starea expirată este tratată
înaintea evacuării deterministe a stării active. Obiectele de deduplicare,
asamblare și stare securizată expun snapshot-uri statistice imuabile pentru
inspecție și testare. Limitele exacte ale ciclurilor de viață și cazurile de
margine rămân în contractul comportamental.

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
perimetrul de rețea:

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
- Unitatea instalată nu setează `User=`, `Group=` sau `DynamicUser=` și nu are
  un cont de serviciu dedicat.
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

Rulați CLI-ul ca utilizator autorizat pentru socket. Cu socket-ul instalat
implicit, deținut de root, aceasta înseamnă de obicei:

```bash
sudo aismixerctl status
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
sudo aismixerctl \
  replace \
  --file examples/routing-update.yaml \
  --expected-generation 3
```

Dezactivați rutarea și readuceți procesul care rulează în modul legacy
broadcast:

```bash
sudo aismixerctl \
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
autentifică prin ECDSA, iar cheile de sesiune derivate prin ECDH permit
versiunii actuale a `nmea_sproxy` să protejeze traficul AIS și ping/pong prin
criptare AES-GCM autentificată. Cheile publice ale stațiilor autorizate sunt
configurate prin `authorized_keys.yaml`. Înregistrările de replay ale
handshake-ului, sesiunile active și înregistrările nonce-urilor de date pentru
fiecare sesiune folosesc o stare locală procesului, limitată și gestionată prin
TTL.
Curățarea la expirare este declanșată de traficul permis, nu de un timer în
fundal, iar starea securizată pentru replay, sesiuni și nonce-uri se pierde la
repornirea serviciului. UDPSEC protejează pachetele în tranzit; nu dovedește că
payload-ul AIS este veridic din punct de vedere semantic sau exact din punct de
vedere fizic.

Pentru funcționarea protocolului, domeniul de securitate, semantica exactă a
stării și context suplimentar, consultați [ghidul
`nmea_sproxy`](nmea_sproxy/README.md), [politica de securitate](SECURITY.md),
[contractul comportamental](BEHAVIORAL_CONTRACT.md) și
[Wiki](https://github.com/iliyan85/aismixer/wiki).

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
conflict, expirare, evacuare la atingerea capacității și finalizare ale
assemblerului. Regulile exacte de proprietate și selecție sunt definite în
[BEHAVIORAL_CONTRACT.md](BEHAVIORAL_CONTRACT.md).

### 🪪 TAG `s` — eticheta sursei

Valoarea TAG `s` emisă este aleasă separat de `source_id` folosit pentru rutare.

Ordinea de prioritate:

1. `station_id` global nevid;
2. ID-ul intrării, aliasul UDP sau numele stației/clientului UDPSEC autorizat;
3. TAG `s` de ingress, când este prezent;
4. adresa IP a expeditorului, ca valoare de rezervă.

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

Instalați serviciul systemd administrat de repository și wrapper-ul CLI global:

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

- UDP este singurul adaptor egress AISMixer implementat în prezent.
- Starea și generațiile de rutare sunt locale procesului; modificările efectuate
  la runtime nu sunt persistente.
- Starea securizată pentru replay, sesiuni și nonce-uri este locală procesului
  și nepersistentă; curățarea la expirare este declanșată de trafic, nu de un
  timer în fundal.
- Integrarea actuală a serviciului lasă limitele opționale de deduplicare și
  asamblare la `None`, astfel că aceste dimensiuni nu au limită de capacitate.
- Nu există coordonator multiprocessing sau sincronizare între procese.
- Nu există reîncărcare sau urmărire automată a configurației.
- Nu există filtrare geografică, după MMSI sau după conținutul datelor navei.
- Nu există stocare pe termen lung, analiză sau detectare a spoofing-ului.
- Controlul Unix necesită suport POSIX pentru socket-uri din domeniul Unix.
- Controlul accesului se bazează pe permisiunile sistemului de fișiere Unix,
  fără token la nivelul aplicației.
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
