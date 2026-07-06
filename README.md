<a id="english"></a>

**English · [Български](#bulgarian)**

# 🛰️ AISMixer — AIS NMEA 0183 stream processor and routing engine

**Normalize · Deduplicate · Tag · Route · Forward**

AISMixer processes AIS NMEA 0183 streams with UDP/UDPSEC ingress, multipart
assembly, deduplication, logical routing, and targeted UDP forwarding.

[🌐 Website](https://aismixer.net) · [📚 Examples](examples/README.md) ·
[🔐 UDPSEC guide](nmea_sproxy/README.md) · [🗺️ Roadmap](ROADMAP.md)

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
- **`nmea_sproxy`** is the station-side UDPSEC proxy. One process forwards one
  local UDP input to one AISMixer UDPSEC input.
- **`aismixerctl.py`** is the operator CLI for the optional local routing-control
  socket.

```text
AIS receiver UDP      \
AIS receiver UDP       \        +----------------+       +----------------+
nmea_sproxy UDPSEC ---->------> |    AISMixer    | ----> | UDP targets    |
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
- `!AIVDM` and `!AIVDO` extraction.
- Multipart assembly using NMEA fragment fields and ingress assembler identity.
- NMEA TAG `s`/`c`/`g` handling.
- Global deduplication in legacy broadcast mode.
- Legacy forwarding to every configured UDP forwarder.
- Named UDP egress targets.
- Static logical routing loaded at startup.
- Logical `source_id` and `target_id` matching.
- Logical source zones with `include`, `union`, `intersection`, and
  `difference`.
- Target-scoped deduplication in routing mode.
- Immutable routing tables and process-local routing generations.
- Atomic runtime replacement of the active routing snapshot.
- Versioned JSON routing-control protocol.
- `routing.status`, `routing.replace`, and `routing.disable`.
- Unix-domain NDJSON control server and client.
- `aismixerctl` CLI.

### 🧪 Opt-in operational interface

The runtime control plane is implemented but deliberately opt-in:

- `control.unix.enabled: true` is required.
- The listener requires POSIX Unix-domain socket support.
- Filesystem ownership, group, and mode on the socket path are the current
  authorization boundary.
- There is no application-level control token.
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

The data plane receives AIS data, builds an internal `IngressEvent`, captures one
immutable routing snapshot and matches `source_id` once for that event, extracts
NMEA sentences, assembles multipart messages, applies global or target-scoped
deduplication, constructs outbound TAG metadata, and forwards accepted
sentences to UDP egress destinations.

- **Legacy mode:** global deduplication and broadcast to all forwarders.
- **Routing mode:** logical source matching, per-target deduplication, and
  targeted forwarding to named UDP egress destinations.
- One routing snapshot is captured per `IngressEvent`; a concurrent control
  update affects the next event, not the one already being processed.

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
| `nmea_sproxy/` | Station-side UDPSEC proxy: one input to one AISMixer UDPSEC input |
| `assembler.py` | Multipart AIVDM/AIVDO reassembly |
| `dedup.py` | Global or target-scoped duplicate suppression |
| `meta_writer.py` / `meta_cleaner.py` | NMEA TAG output and ingress cleanup |
| `forwarder.py` | UDP broadcast and targeted egress |

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
- The socket parent directory must already exist.
- AISMixer does not currently create `/run/aismixer` automatically.
- The current installer/systemd unit does not yet provision this directory.
- Filesystem ownership, group, and mode control access to the socket.
- There is no application-level authentication token.
- The interface is POSIX-only; Windows can run pure tests and development code,
  but not the Unix socket listener.
- Runtime routing changes are process-local and disappear after restart.

See
[`examples/config-routing-control.yaml`](examples/config-routing-control.yaml)
for an inactive complete configuration with static routing and runtime control.

---

## 🧰 `aismixerctl`

Until installer/systemd integration is updated, do not assume that
`aismixerctl` is installed globally. From a repository checkout or copied
service directory, use:

```bash
python3 aismixerctl.py --socket /run/aismixer/control.sock status
```

The shorter form works only when your local installation or `PATH` provides it:

```bash
aismixerctl --socket /run/aismixer/control.sock status
```

Replace the active process-local routing snapshot:

```bash
python3 aismixerctl.py \
  --socket /run/aismixer/control.sock \
  replace \
  --file examples/routing-update.yaml \
  --expected-generation 3
```

Disable routing and return the running process to legacy broadcast mode:

```bash
python3 aismixerctl.py \
  --socket /run/aismixer/control.sock \
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

## 🔐 UDPSEC and `nmea_sproxy`

UDPSEC is AISMixer's authenticated encrypted station-to-mixer UDP transport.
It is not an external standardized protocol. Stations authenticate with ECDSA,
while AIS data and liveness messages use authenticated AES-GCM encryption.
Authorized station public keys are configured through `authorized_keys.yaml`.
UDPSEC protects packets in transit; it does not prove that the AIS payload
itself is semantically true or physically accurate.

`nmea_sproxy` is the station-side proxy:

```text
one local UDP input → one AISMixer UDPSEC input
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

---

## 🏷️ NMEA TAG behavior

AISMixer reads ingress TAG metadata and emits a controlled `s`/`c`/`g` TAG
block according to the runtime options described below.

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

`c_preserve_ingress_c: true` preserves a valid ingress TAG `c` timestamp. When
it is disabled or no valid value is present, AISMixer emits server time.

### 🧷 TAG `g` — output group metadata

TAG `g` is ingress/output metadata for multipart messages. It is **not** the
assembler key. Multipart assembly uses NMEA fragment fields together with the
ingress assembler identity.

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

Or install the existing systemd service:

```bash
./install.sh
```

The current installer deploys the runtime files and existing service unit.
Control-plane installer/systemd integration — including automatic runtime
directory provisioning and a global `aismixerctl` command — is intentionally a
separate follow-up change.

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
- Control socket directory provisioning is currently operator-managed.
- Access control relies on Unix filesystem permissions.

---

## 📖 Further documentation

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

**[English](#english) · Български**

# 🇧🇬 AISMixer — обработка и маршрутизация на AIS NMEA 0183 потоци

**Нормализация · Дедупликация · TAG metadata · Маршрутизация · Препращане**

AISMixer обработва AIS NMEA 0183 потоци с UDP/UDPSEC входове, сглобяване на
multipart съобщения, дедупликация, логическа маршрутизация и целево UDP
препращане.

[🌐 Уебсайт](https://aismixer.net) · [📚 Примери](examples/README.md) ·
[🔐 Ръководство за UDPSEC](nmea_sproxy/README.md) ·
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
- **`nmea_sproxy`** е UDPSEC проксито при станцията. Един процес препраща един
  локален UDP вход към един UDPSEC вход на AISMixer.
- **`aismixerctl.py`** е операторският CLI клиент за допълнителния локален
  routing-control socket.

```text
AIS приемник UDP      \
AIS приемник UDP       \        +----------------+       +----------------+
nmea_sproxy UDPSEC ---->------> |    AISMixer    | ----> | UDP цели       |
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
- Извличане на `!AIVDM` и `!AIVDO`.
- Сглобяване на multipart чрез NMEA fragment полетата и ingress assembler
  identity.
- Обработка на NMEA TAG `s`/`c`/`g`.
- Глобална дедупликация в legacy broadcast режим.
- Legacy препращане към всички конфигурирани UDP forwarder-и.
- Именувани UDP egress цели.
- Статична логическа маршрутизация, зареждана при стартиране.
- Съпоставяне чрез логически `source_id` и `target_id`.
- Логически source zones с `include`, `union`, `intersection` и `difference`.
- Дедупликация по отделен target в routing режим.
- Immutable routing tables и process-local generations.
- Атомарна runtime подмяна на активния routing snapshot.
- Версиониран JSON routing-control протокол.
- `routing.status`, `routing.replace` и `routing.disable`.
- Unix-domain NDJSON control server и клиент.
- CLI инструментът `aismixerctl`.

### 🧪 Opt-in оперативен интерфейс

Runtime control plane е реализиран, но умишлено се включва само изрично:

- изисква се `control.unix.enabled: true`;
- listener-ът изисква POSIX Unix-domain socket support;
- filesystem собственикът, групата и mode на socket path са текущата граница
  за достъп;
- няма application-level control token;
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

Data plane приема AIS данните, създава вътрешен `IngressEvent`, взема един
immutable routing snapshot и съпоставя `source_id` веднъж за този event, извлича
NMEA изреченията, сглобява multipart съобщенията, прилага глобална или
target-scoped дедупликация, изгражда изходната TAG metadata и препраща приетите
изречения към UDP egress дестинациите.

- **Legacy режим:** глобална дедупликация и broadcast към всички forwarder-и.
- **Routing режим:** логическо source matching, дедупликация по target и целево
  препращане към именувани UDP egress дестинации.
- За всеки `IngressEvent` се взема един routing snapshot; паралелна control
  промяна засяга следващия event, а не вече обработвания.

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
| `nmea_sproxy/` | Station-side UDPSEC proxy: един вход към един UDPSEC вход на AISMixer |
| `assembler.py` | Сглобяване на multipart AIVDM/AIVDO |
| `dedup.py` | Глобална или target-scoped дедупликация |
| `meta_writer.py` / `meta_cleaner.py` | NMEA TAG изход и ingress cleanup |
| `forwarder.py` | UDP broadcast и targeted egress |

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
- Parent directory на socket path трябва вече да съществува.
- AISMixer все още не създава автоматично `/run/aismixer`.
- Текущият installer/systemd unit още не provision-ва тази директория.
- Filesystem собственикът, групата и mode управляват достъпа до socket-а.
- Няма application-level authentication token.
- Интерфейсът е само за POSIX. Windows може да изпълнява pure tests и
  development кода, но не и Unix socket listener-а.
- Runtime routing промените са process-local и изчезват след рестарт.

Виж
[`examples/config-routing-control.yaml`](examples/config-routing-control.yaml)
за неактивна пълна конфигурация със static routing и runtime control.

---

## 🧰 `aismixerctl`

Докато installer/systemd интеграцията не бъде обновена, не трябва да се приема,
че `aismixerctl` е инсталирана глобална команда. От repository checkout или
копирана service директория използвай:

```bash
python3 aismixerctl.py --socket /run/aismixer/control.sock status
```

Кратката форма работи само когато локалната инсталация или `PATH` я осигурява:

```bash
aismixerctl --socket /run/aismixer/control.sock status
```

Подмяна на активния process-local routing snapshot:

```bash
python3 aismixerctl.py \
  --socket /run/aismixer/control.sock \
  replace \
  --file examples/routing-update.yaml \
  --expected-generation 3
```

Изключване на routing и връщане на работещия процес към legacy broadcast режим:

```bash
python3 aismixerctl.py \
  --socket /run/aismixer/control.sock \
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

## 🔐 UDPSEC и `nmea_sproxy`

UDPSEC е автентикираният и криптиран UDP транспорт между станцията и AISMixer.
Това не е външен стандартизиран протокол. Станциите се автентикират с ECDSA, а
AIS данните и liveness съобщенията използват автентикирано AES-GCM криптиране.
Разрешените публични ключове на станциите се конфигурират чрез
`authorized_keys.yaml`. UDPSEC защитава пакетите при пренос, но не доказва, че
самият AIS payload е семантично верен или физически точен.

`nmea_sproxy` е проксито при станцията:

```text
един локален UDP вход → един криптиран UDPSEC вход на AISMixer
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

---

## 🏷️ Поведение на NMEA TAG metadata

AISMixer чете ingress TAG metadata и излъчва контролиран `s`/`c`/`g` TAG block
според описаните по-долу runtime настройки.

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

`c_preserve_ingress_c: true` запазва валиден входящ TAG `c` timestamp. Когато е
изключено или няма валидна стойност, AISMixer излъчва сървърното време.

### 🧷 TAG `g` — output group metadata

TAG `g` е ingress/output metadata за multipart съобщения. Той **не** е assembler
key. Multipart assembly използва NMEA fragment полетата заедно с ingress
assembler identity.

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

Или инсталиране на съществуващата systemd услуга:

```bash
./install.sh
```

Текущият installer разполага runtime файловете и съществуващия service unit.
Control-plane installer/systemd интеграцията — включително автоматичното
създаване на runtime директория и глобална команда `aismixerctl` — умишлено е
отделна следваща промяна.

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
- Provisioning-ът на control socket директорията все още е задача на оператора.
- Контролът на достъпа разчита на Unix filesystem permissions.

---

## 📖 Допълнителна документация

- [Примерни конфигурации](examples/README.md)
- [Операторско ръководство за `nmea_sproxy`](nmea_sproxy/README.md)
- [GitHub Wiki](https://github.com/iliyan85/aismixer/wiki)
- [Ръководство за принос](CONTRIBUTING.md)
- [Политика за сигурност](SECURITY.md)
- [План за развитие](ROADMAP.md)
- [Публичен уебсайт](https://aismixer.net)

[⬆ Към избора на език](#english)
