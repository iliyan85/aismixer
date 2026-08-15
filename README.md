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
> logical routing directs ingress sources to named UDP targets, while the local
> `aismixerctl` control interface exposes runtime status, statistics, and atomic
> routing updates.

## 🚀 Quick start

Choose the deployment path for the host: conventional Linux with systemd, or
the published OpenWrt APK packages with procd.

### Conventional Linux with systemd

The lifecycle scripts can run directly as root. When invoked by a non-root user,
they elevate privileged operations with `sudo`; they stop with an explanation if
neither condition is available.

Standalone `systemctl` commands and commands that write under `/etc` assume a
root shell; non-root operators should prefix them with `sudo` when required.

#### Fresh installation

On a systemd-based Debian or Raspberry Pi OS host:

```bash
git clone https://github.com/iliyan85/aismixer
cd aismixer
chmod +x install.sh
./install.sh
systemctl start aismixer
systemctl status aismixer
```

`install.sh` installs the runtime under `/opt/aismixer`, seeds missing files
under `/etc/aismixer` while preserving existing configuration and keys, installs
`/usr/local/bin/aismixerctl`, and enables AISMixer at boot. It intentionally does
**not** start the service. Review `/etc/aismixer/config.yaml` before exposing the
service outside a trusted network.

#### Update

The repository tracks lifecycle scripts without an executable bit. Before the
first update, run `chmod +x update.sh` once. Then update from the checkout:

```bash
git pull --ff-only
./update.sh
systemctl status aismixer
```

`update.sh` refreshes installed runtime files, the systemd unit, and
`aismixerctl`, reloads systemd, and **restarts AISMixer**. It leaves operator
configuration and keys under `/etc/aismixer` untouched. `uninstall.sh` preserves
that directory unless `--purge-config` is explicitly requested.

### OpenWrt 25.12 (x86_64 only)

For OpenWrt 25.12 on x86_64, a signed APK v3 repository publishes the current
Python implementation with procd integration. This is the only currently
published OpenWrt target. Run as root:

```sh
wget -O /etc/apk/keys/aismixer-openwrt.pem \
  https://aismixer.net/openwrt/keys/aismixer-openwrt.pem

chmod 0644 /etc/apk/keys/aismixer-openwrt.pem

REPO_FILE=/etc/apk/repositories.d/customfeeds.list
REPO_URL='https://aismixer.net/openwrt/25.12/x86_64/packages.adb'

grep -qxF "$REPO_URL" "$REPO_FILE" 2>/dev/null || {
  printf '\n# AISMixer OpenWrt 25.12 x86_64 repository\n%s\n' \
    "$REPO_URL" >> "$REPO_FILE"
}

apk update
apk add aismixer
```

Repository public-key SHA-256:
`170d30219e0e05d59898cd8ccd5ec9804e915df7882ab56b8e869ef6e99c8f9c`.
The `aismixer` package resolves `aismixer-common` automatically as its shared
dependency; normally, do not install it manually. To install only the
station-side package instead:

```sh
apk add nmea_sproxy
```

On first service start, AISMixer automatically generates or repairs its local
server identity when required, uses the OpenWrt configuration and identity
layout under `/etc/aismixer`, and runs through procd. `nmea_sproxy` likewise
generates or repairs its local station identity.

For UDPSEC, peer trust is never provisioned automatically. Copy
`/etc/aismixer/keys/aismixer_public.pem` from the mixer to the proxy's configured
`remote_public_key` path (by default
`/etc/nmea_sproxy/keys/aismixer_public.pem`), then authorize the station public
identity key on AISMixer as described in the
[`nmea_sproxy` guide](nmea_sproxy/README.md#authorize-the-station-in-aismixer).
If the configured peer key is missing, unreadable, or invalid, preflight
intentionally keeps the service stopped instead of entering a respawn loop.
This is expected safe behavior, not a failed package installation. Explicit
plain UDP does not require a peer key.

## 🧭 What is AISMixer?

**AISMixer** is a production-oriented Python service for receiving,
normalizing, deduplicating, tagging, routing, and forwarding AIS NMEA 0183
streams.

- **`aismixer.py`** is the long-running mixer and data-plane service.
- **`aismixerctl`** is the installed operator command for the optional local
  control socket.
- **`nmea_sproxy`** is the station-side proxy for one local UDP or serial input
  and one UDPSEC or explicitly configured plain-UDP output.

```text
AIS receiver UDP      \
AIS receiver UDP       \        +----------------+       +----------------+
nmea_sproxy UDPSEC/UDP ------> |    AISMixer    | ----> | UDP targets    |
                                |   data plane   |       +----------------+
                                +----------------+
                                         ^
                                         |
                                optional local control
                                         |
                                   aismixerctl
```

## ⚙️ Basic configuration

The installed service reads `/etc/aismixer/config.yaml`. With no top-level
`routing:` section, AISMixer uses legacy broadcast mode: deduplication is global
and every accepted output sentence goes to every configured forwarder.

```yaml
station_id: mixstation_1

udp_inputs:
  - id: roof_receiver
    listen_ip: "0.0.0.0"
    listen_port: 17777

forwarders:
  - id: local_display
    host: 127.0.0.1
    port: 19000
```

The repository examples are inactive until copied or adapted. See
[`examples/config-routing.yaml`](examples/config-routing.yaml) for static
routing and
[`examples/config-routing-control.yaml`](examples/config-routing-control.yaml)
for routing with local control enabled.

### Network endpoint controls

- `listen_ip` selects one address family. Use separate entries for IPv4 and
  IPv6 dual-stack ingress.
- Optional `udp_inputs[].allow_from` and `sec_inputs[].allow_from` values are
  application-level literal-IP/CIDR allow-lists. Omission applies no application
  ACL; an explicitly empty list denies all packets for that listener.
- Optional `forwarders[].source_ip` binds an outbound UDP socket to a literal
  local source address.

These controls complement, rather than replace, host firewall and routing
policy.

## 🧰 `aismixerctl`

On the conventional Linux/systemd path, `install.sh` installs
`/usr/local/bin/aismixerctl`. The OpenWrt `aismixer` package installs the same
operator command as `/usr/bin/aismixerctl`. The command talks to the optional
local Unix-domain control socket; it does not modify configuration files or
persist runtime routing across a restart.

### Enable local control

The control service is disabled until explicitly enabled in AISMixer
configuration:

```yaml
control:
  unix:
    enabled: true
    socket_path: /run/aismixer/control.sock
    socket_mode: "0660"
```

The installed systemd unit creates `/run/aismixer` while the service is running.
Socket ownership and mode determine access; there is no application-level
authentication token. The interface requires POSIX Unix-domain socket support.

### Interactive workflow

The interactive shell is the quickest way to discover current state and inspect
process-local counters. With the default root-owned socket:

```text
sudo aismixerctl
aismixerctl> status
aismixerctl> show statistics
aismixerctl> show statistics inputs
aismixerctl> show statistics outputs
```

Use `help` inside the shell. Input and output statistics commands also accept an
optional input or output filter. One-shot commands remain available for scripts.

### Routing updates

Replace or disable the active process-local routing snapshot atomically:

```bash
sudo aismixerctl replace --file examples/routing-update.yaml --expected-generation 3
sudo aismixerctl disable --expected-generation 4
```

The expected generation is optional; when supplied, it prevents a stale update
from overwriting newer state. The CLI does not retry automatically.

## 🔐 `nmea_sproxy`

`nmea_sproxy` is the station-side network proxy. One process or service instance
represents one relation: one local UDP or physical serial/USB input to one
network output. The install, update, and systemd template-instance instructions
below describe the conventional Linux path; OpenWrt uses the singleton APK/procd
path in Quick start. UDPSEC is the secure/default transport to AISMixer; plain
UDP must be selected explicitly and is intended only for trusted LAN/VPN paths.

### Install and start

From the AISMixer checkout:

```bash
cd nmea_sproxy
chmod +x install.sh
./install.sh
```

The proxy installer prepares the configuration layout and station key pair while
preserving existing operator material, installs singleton and template units,
enables only the singleton, and starts no service. Complete the relation
configuration and, for the default UDPSEC mode, the trusted AISMixer public-key
setup described in the [operator guide](nmea_sproxy/README.md), then start it:

```bash
systemctl start nmea_sproxy.service
systemctl status nmea_sproxy.service
```

### Singleton and template instances

The singleton reads `/etc/nmea_sproxy/config.yaml`. Template instances such as
`boat`, `yacht`, or `roof` use operator-chosen relation names and configuration
under `/etc/nmea_sproxy/instances/`. Copy and edit a relation file before
starting its instance:

```bash
cp /etc/nmea_sproxy/config.yaml /etc/nmea_sproxy/instances/boat.yaml
systemctl enable --now nmea_sproxy@boat.service
systemctl status nmea_sproxy@boat.service
```

Run another template instance when another independent relation is required. A
template-only deployment should disable and stop the unused singleton with
`systemctl disable --now nmea_sproxy.service`.

### Update

The proxy updater is also tracked without an executable bit. From the repository
root, make it executable once and run it after pulling changes:

```bash
git pull --ff-only
cd nmea_sproxy
chmod +x update.sh
./update.sh
```

Unlike AISMixer's updater, `nmea_sproxy/update.sh` updates installed files and
reloads systemd but intentionally **does not restart** the singleton or any
template instance. Restart only the relations you choose, when ready:

```bash
systemctl restart nmea_sproxy.service
systemctl restart nmea_sproxy@boat.service
```

### Security boundary

UDPSEC is AISMixer's project-specific authenticated and encrypted UDP transport,
not an external standard. It mutually authenticates configured endpoint
identities, protects packet confidentiality and integrity in transit, and
provides replay and liveness checks. Its forward-secrecy properties depend on
ephemeral secrets being discarded and endpoints not being compromised while
those secrets are live; the design has engineering validation, not formal
cryptographic verification. UDPSEC does not prove that AIS payloads are
semantically true or physically accurate. Explicit plain UDP provides no
confidentiality, station authentication, cryptographic integrity, replay
protection, or liveness protocol. See the
[`nmea_sproxy` guide](nmea_sproxy/README.md), [security policy](SECURITY.md),
[behavioural contract](BEHAVIORAL_CONTRACT.md), and
[Wiki](https://github.com/iliyan85/aismixer/wiki) for keys, trust, configuration,
session behavior, endpoint policy, and troubleshooting.

## ✅ Current capabilities

### 📡 Ingress and egress

- UDP ingress over IPv4 and IPv6, with optional application-level allow-lists.
- Authenticated encrypted UDPSEC ingress compatible with `nmea_sproxy`.
- Physical serial and USB virtual COM input through `nmea_sproxy`, plus explicit
  plain UDP for trusted networks.
- Optional outbound source-address binding for AISMixer and proxy outputs.
- UDP broadcast egress in legacy mode and named UDP targets in routing mode.

### ⚙️ Processing

- Extraction of supported `!AIVDM`, `!AIVDO`, and compatible AIS sentences.
- Fully out-of-order multipart assembly; exact repeats are idempotent and
  conflicting fragments invalidate the live group.
- Deterministic, lifecycle-aware NMEA TAG `s`/`c`/`g` handling.
- Group-atomic multipart deduplication: global in legacy mode and scoped to each
  `target_id` in routing mode.
- Bounded process-local ingress, processing, and egress queues apply
  backpressure; mutable data-plane state has an explicit instance-owned
  processor lifecycle and reset boundary.
- Explicit process-local TTL lifecycles, optional reference-state capacities,
  and fresh immutable, pull-based runtime statistics snapshots.

### 🔀 Routing and operation

- Legacy broadcast mode or static logical routing loaded at startup.
- Named ingress `source_id` and egress `target_id` identities.
- Logical source zones with `include`, `union`, `intersection`, and `difference`.
- One immutable `ProcessingSnapshot`, bound only when an ingress frame is
  admitted; routing mode performs one source match at that point.
- Optional atomic runtime routing replacement through `aismixerctl`.
- Supervised process-local ingress, processing, and egress tasks; the optional
  control listener is lifecycle-managed separately.
- Conventional Linux deployment uses lifecycle scripts, systemd, and
  `/usr/local/bin/aismixerctl`; the signed OpenWrt 25.12 x86_64 APK repository
  provides Python packages with procd integration.

## 🔀 Architecture

AISMixer keeps the data plane and optional control plane separate.

### 📡 Data plane

UDP and UDPSEC producers create immutable ingress frames in private bounded
queues. Fan-in admits them to bounded processing before the one instance-owned
processor scans NMEA data, assembles multipart messages, applies global or
target-scoped deduplication, and builds controlled TAG metadata. Full AISMixer
stage queues wait with backpressure instead of dropping their queued items.

Each admitted frame has one bound `ProcessingSnapshot`; routing mode adds one
source match. Non-empty `OutputBatch` values cross a bounded egress handoff in
order, and processing waits for local egress completion before advancing. There
is no automatic delivery replay if a supervised runtime task fails.

### 🎛️ Control plane

When enabled, the local NDJSON Unix-domain service validates requests against
available target IDs and atomically swaps immutable process-local routing state.
It also exposes runtime status plus read-only aggregate, per-input, and
per-output runtime statistics to `aismixerctl`.

### 🧩 Main components

| Component | Role |
|---|---|
| `aismixer.py` | Runtime lifecycle, ingress fan-in, processing, egress, and optional control |
| `core/routing*.py` | Logical routing and immutable snapshots |
| `core/routing_control*.py` / `core/runtime_control.py` | Control protocol and Unix-domain service |
| `core/runtime_statistics.py` | Process-local runtime counters and immutable views |
| `aismixerctl.py` | Operator control and statistics CLI |
| `aismixer_secure.py` | UDPSEC ingress |
| `nmea_sproxy/` | Station-side UDP/serial-to-UDPSEC/UDP proxy |
| `assembler.py` / `dedup.py` | Multipart assembly and duplicate suppression |
| `meta_writer.py` / `meta_cleaner.py` | NMEA TAG output and ingress cleanup |
| `forwarder.py` | UDP broadcast and targeted egress |

The normative processing and runtime semantics are in
[BEHAVIORAL_CONTRACT.md](BEHAVIORAL_CONTRACT.md). This README is an operator
overview, not a second specification.

## 🗺️ Logical routing

### Legacy broadcast

Without a top-level `routing:` section, deduplication is global and every
accepted sentence goes to every forwarder. Unnamed forwarders remain valid.

### Static routing

A valid top-level `routing:` section enables ordered routes from source IDs or
logical zones to named forwarders. Zones are source-ID sets, not geographic
areas, MMSI lists, or vessel filters. Deduplication is scoped per target.

Canonical identities include `udp:<input-id>`, `udp:<mapped-alias>`,
`udp:<remote-ip>`, `udpsec:<authenticated-station-id>`, and
`udp:<forwarder-id>` for named UDP targets. Routing matches internal source IDs,
not emitted TAG `s` labels. See the [static example](examples/config-routing.yaml).

### Runtime routing

When local control is enabled, `routing.replace` and `routing.disable` affect
frames not yet admitted to bounded processing through one atomic snapshot
change; admitted work keeps its bound snapshot. Updates are process-local and
disappear at restart. See the
[control example](examples/config-routing-control.yaml) and
[update payload](examples/routing-update.yaml).

## 🏷️ NMEA TAG behavior

AISMixer reads ingress TAG metadata and emits controlled `s`/`c`/`g` metadata.
The emitted `s` label is selected separately from the internal routing source ID
and is sanitized for NMEA output. Depending on configuration, `c` can preserve
valid ingress time or use server time, and multipart `g` can preserve an agreed
ingress group ID or generate one output ID. TAG `g` is metadata, not the
multipart assembler key.

```yaml
g_preserve_ingress_gid: true
g_id_digits: 18
g_always_tag_single: false
c_preserve_ingress_c: true
```

Exact priority, multipart ownership, conflict, expiry, and compatibility rules
belong to the [behavioural contract](BEHAVIORAL_CONTRACT.md).

## 📚 Examples and testing

- [`examples/config-routing.yaml`](examples/config-routing.yaml) — static routing.
- [`examples/config-routing-control.yaml`](examples/config-routing-control.yaml)
  — static routing with local control.
- [`examples/routing-update.yaml`](examples/routing-update.yaml) — direct routing
  update for `aismixerctl`.
- [`examples/README.md`](examples/README.md) — example-file guide.

All example addresses, IDs, ports, paths, and keys require operator adaptation.
Run the repository test suite with:

```bash
python -m pytest
```

Real Unix-domain listener tests require Linux, WSL, Raspberry Pi OS, or another
POSIX environment with asyncio Unix-socket support.

## ⚠️ Current limitations

- UDP is the only implemented AISMixer egress adapter.
- Routing state, generations, and runtime statistics are process-local; runtime
  routing changes are not persisted.
- Secure replay, session, and nonce state is process-local and non-durable;
  expiry cleanup is driven by allowed traffic.
- Current service wiring leaves optional deduplication and assembly capacities
  at `None`.
- There is no coordinator process or separate ingress/egress worker processes,
  IPC, cross-process routing or metrics aggregation, automatic worker restart,
  or configuration reload.
- There is no geographic, MMSI, vessel-content, long-term storage, analytics, or
  spoof-detection feature.
- Local control requires POSIX Unix sockets and relies on filesystem permissions;
  there is no application token or dedicated service account policy.

## 📖 Documentation map

- [Behavioural contract](BEHAVIORAL_CONTRACT.md) — normative processing and
  runtime semantics.
- [`nmea_sproxy` operator guide](nmea_sproxy/README.md) — complete station-side
  installation, configuration, keys, serial, output, service, and troubleshooting
  guidance.
- [GitHub Wiki](https://github.com/iliyan85/aismixer/wiki) — deeper architecture
  and explanatory material.
- [Examples](examples/README.md) · [Security policy](SECURITY.md) ·
  [Contributing guide](CONTRIBUTING.md) · [Roadmap](ROADMAP.md) ·
  [Public website](https://aismixer.net)

## 🌿 Branches and website

`main` contains the service, proxy, examples, control components, and `tests/`.
The public website lives on `website`; GitHub Pages uses `/docs` from that branch,
so `docs/` is intentionally absent from `main`.

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
> логическата маршрутизация насочва входните източници към именувани UDP цели, а
> локалният интерфейс `aismixerctl` предоставя статус и статистика по време на
> работа и атомарни промени на маршрутизацията.

## 🚀 Бърз старт

Изберете пътя за инсталиране според хоста: стандартен Linux със systemd или
публикуваните APK пакети за OpenWrt с procd.

### Стандартен Linux със systemd

Скриптовете за жизнения цикъл могат да се изпълняват директно като root. Когато
ги стартира потребител без root права, те повишават правата на привилегированите
операции чрез `sudo`; ако нито едно от двете не е налично, спират с обяснение.

Самостоятелните команди `systemctl` и командите, които пишат в `/etc`,
предполагат root shell; при нужда потребител без root права трябва да добави
`sudo`.

#### Нова инсталация

На Debian или Raspberry Pi OS система със systemd:

```bash
git clone https://github.com/iliyan85/aismixer
cd aismixer
chmod +x install.sh
./install.sh
systemctl start aismixer
systemctl status aismixer
```

`install.sh` разполага runtime файловете в `/opt/aismixer`, създава липсващите
файлове в `/etc/aismixer`, без да презаписва съществуващите конфигурации и
ключове, инсталира `/usr/local/bin/aismixerctl` и включва AISMixer за автоматично
стартиране при boot. Умишлено **не** стартира услугата. Прегледайте
`/etc/aismixer/config.yaml`, преди да изложите услугата извън доверена мрежа.

#### Обновяване

Хранилището проследява скриптовете за жизнения цикъл без executable bit. Преди
първото обновяване изпълнете еднократно `chmod +x update.sh`. След това обновете
от локалното копие:

```bash
git pull --ff-only
./update.sh
systemctl status aismixer
```

`update.sh` обновява инсталираните runtime файлове, systemd unit-а и
`aismixerctl`, презарежда systemd и **рестартира AISMixer**. Операторските
конфигурации и ключове в `/etc/aismixer` остават непроменени. `uninstall.sh`
запазва тази директория, освен ако изрично не е зададено `--purge-config`.

### OpenWrt 25.12 (само x86_64)

За OpenWrt 25.12 на x86_64 е публикувано подписано APK v3 хранилище. Пакетите
съдържат текущата Python реализация и се интегрират с procd. В момента това е
единствената публикувана цел за OpenWrt. Изпълнете като root:

```sh
wget -O /etc/apk/keys/aismixer-openwrt.pem \
  https://aismixer.net/openwrt/keys/aismixer-openwrt.pem

chmod 0644 /etc/apk/keys/aismixer-openwrt.pem

REPO_FILE=/etc/apk/repositories.d/customfeeds.list
REPO_URL='https://aismixer.net/openwrt/25.12/x86_64/packages.adb'

grep -qxF "$REPO_URL" "$REPO_FILE" 2>/dev/null || {
  printf '\n# AISMixer OpenWrt 25.12 x86_64 repository\n%s\n' \
    "$REPO_URL" >> "$REPO_FILE"
}

apk update
apk add aismixer
```

SHA-256 отпечатъкът на публичния ключ на хранилището е
`170d30219e0e05d59898cd8ccd5ec9804e915df7882ab56b8e869ef6e99c8f9c`.
Пакетът `aismixer` инсталира автоматично общата зависимост `aismixer-common`;
обикновено не е нужно да я инсталирате ръчно. За да инсталирате вместо това само
пакета при станцията:

```sh
apk add nmea_sproxy
```

При първото стартиране AISMixer автоматично генерира или поправя при
необходимост локалната си сървърна идентичност, използва OpenWrt структурата за
конфигурация и идентичност в `/etc/aismixer` и се стартира чрез procd.
`nmea_sproxy` прави същото за локалната идентичност на станцията.

При UDPSEC доверието към отсрещната страна никога не се настройва автоматично.
Копирайте `/etc/aismixer/keys/aismixer_public.pem` от хоста с AISMixer на
зададения в конфигурацията на проксито път `remote_public_key` (по подразбиране
`/etc/nmea_sproxy/keys/aismixer_public.pem`), след което разрешете публичния
ключ за идентичност на станцията в AISMixer според [ръководството за
`nmea_sproxy`](nmea_sproxy/README.md#authorize-the-station-in-aismixer). Ако
публичният ключ на отсрещната страна, зададен в конфигурацията, липсва, не може
да бъде прочетен или е невалиден, предварителната проверка умишлено оставя
услугата спряна, вместо да допусне respawn loop. Това е очаквано безопасно
поведение, а не неуспешна инсталация на пакета. Изрично конфигурираният plain UDP
не изисква публичен ключ на отсрещната страна.

## 🧭 Какво е AISMixer?

**AISMixer** е ориентирана към реална експлоатация Python услуга за приемане,
нормализиране, дедупликация, обработка на TAG метаданни, маршрутизация и
препращане на AIS NMEA 0183 потоци.

- **`aismixer.py`** е дългосрочно работещата mixer услуга от слоя за данни.
- **`aismixerctl`** е инсталираната операторска команда за незадължителния локален
  сокет за управление.
- **`nmea_sproxy`** е проксито при станцията за един локален UDP или сериен вход
  и един UDPSEC или изрично конфигуриран plain-UDP изход.

```text
AIS приемник UDP      \
AIS приемник UDP       \        +----------------+       +----------------+
nmea_sproxy UDPSEC/UDP ------> |    AISMixer    | ----> | UDP цели       |
                                | слой за данни  |       +----------------+
                                +----------------+
                                         ^
                                         |
                               незадължително локално
                                     управление
                                         |
                                   aismixerctl
```

## ⚙️ Основна конфигурация

Инсталираната услуга чете `/etc/aismixer/config.yaml`. Ако няма секция
`routing:` на най-горното ниво, AISMixer използва legacy broadcast режим:
дедупликацията е глобална и всяко прието изходно изречение отива към всеки
конфигуриран forwarder.

```yaml
station_id: mixstation_1

udp_inputs:
  - id: roof_receiver
    listen_ip: "0.0.0.0"
    listen_port: 17777

forwarders:
  - id: local_display
    host: 127.0.0.1
    port: 19000
```

Примерите в хранилището са неактивни, докато не бъдат копирани или адаптирани.
Вижте [`examples/config-routing.yaml`](examples/config-routing.yaml) за статична
маршрутизация и
[`examples/config-routing-control.yaml`](examples/config-routing-control.yaml)
за маршрутизация с включено локално управление.

### Контрол на мрежовите крайни точки

- `listen_ip` избира едно адресно семейство. За вход едновременно по IPv4 и IPv6
  използвайте отделни записи.
- Незадължителните `udp_inputs[].allow_from` и `sec_inputs[].allow_from` са
  списъци с буквални IP адреси/CIDR мрежи, разрешени на ниво приложение. При
  липсващ ключ няма приложен ACL; изрично празен списък отказва всички пакети за
  съответния listener.
- Незадължителният `forwarders[].source_ip` обвързва изходен UDP сокет с буквално
  зададен локален адрес на източника.

Тези механизми допълват, а не заменят firewall-а и routing политиката на хоста.

## 🧰 `aismixerctl`

При стандартната Linux/systemd инсталация `install.sh` инсталира
`/usr/local/bin/aismixerctl`. Пакетът `aismixer` за OpenWrt инсталира същата
операторска команда в `/usr/bin/aismixerctl`. Командата комуникира с
незадължителния локален Unix-domain сокет за управление; тя не променя
конфигурационни файлове и не запазва runtime маршрутизацията след рестарт.

### Включване на локалното управление

Услугата за управление е изключена, докато не бъде включена изрично в
конфигурацията на AISMixer:

```yaml
control:
  unix:
    enabled: true
    socket_path: /run/aismixer/control.sock
    socket_mode: "0660"
```

Инсталираният systemd unit създава `/run/aismixer`, докато услугата работи.
Собственикът и режимът на сокета определят достъпа; няма token за автентикация на
ниво приложение. Интерфейсът изисква POSIX поддръжка за Unix-domain sockets.

### Интерактивен режим

Интерактивната обвивка е най-бързият начин за откриване на текущото състояние и
преглед на локалните за процеса броячи. При сокет, собственост на root:

```text
sudo aismixerctl
aismixerctl> status
aismixerctl> show statistics
aismixerctl> show statistics inputs
aismixerctl> show statistics outputs
```

В самата обвивка използвайте `help`. Командите за входна и изходна статистика
приемат и незадължителен филтър за вход или изход. Еднократните команди остават
налични за скриптове.

### Промени на маршрутизацията

Атомарна подмяна или изключване на активната, локална за процеса моментна снимка:

```bash
sudo aismixerctl replace --file examples/routing-update.yaml --expected-generation 3
sudo aismixerctl disable --expected-generation 4
```

Очакваното поколение е незадължително; когато е зададено, то не допуска остаряла
промяна да презапише по-ново състояние. CLI не повтаря заявката автоматично.

## 🔐 `nmea_sproxy`

`nmea_sproxy` е мрежовото прокси при станцията. Един процес или инстанция на
услугата представлява една връзка: един локален UDP или физически сериен/USB
вход към един мрежов изход. Инструкциите по-долу за инсталиране, обновяване и
systemd template instances описват стандартния Linux път; OpenWrt използва
описания в „Бърз старт“ singleton модел с APK/procd. UDPSEC е защитеният
транспорт по подразбиране към AISMixer; plain UDP трябва да се избере изрично и
е предназначен само за доверени LAN/VPN връзки.

### Инсталиране и стартиране

От локалното копие на AISMixer:

```bash
cd nmea_sproxy
chmod +x install.sh
./install.sh
```

Инсталаторът на проксито подготвя структурата за конфигурация и ключовата двойка
на станцията, като запазва съществуващите операторски данни, инсталира singleton
и template unit-и, включва само singleton услугата и не стартира услуга.
Завършете конфигурацията на връзката и, за UDPSEC режима по подразбиране,
настройката на доверения публичен ключ на AISMixer, описани в [операторското
ръководство](nmea_sproxy/README.md), след което я стартирайте:

```bash
systemctl start nmea_sproxy.service
systemctl status nmea_sproxy.service
```

### Singleton и template instances

Singleton услугата чете `/etc/nmea_sproxy/config.yaml`. Template instances като
`boat`, `yacht` или `roof` използват избрани от оператора имена на връзки и
конфигурация в `/etc/nmea_sproxy/instances/`. Копирайте и редактирайте файла на
връзката, преди да стартирате instance-а:

```bash
cp /etc/nmea_sproxy/config.yaml /etc/nmea_sproxy/instances/boat.yaml
systemctl enable --now nmea_sproxy@boat.service
systemctl status nmea_sproxy@boat.service
```

За всяка допълнителна независима връзка стартирайте друг template instance.
Инсталация само с template instances трябва да изключи и спре неизползваната
singleton услуга с `systemctl disable --now nmea_sproxy.service`.

### Обновяване

Updater-ът на проксито също се проследява без executable bit. От корена на
хранилището го направете изпълним еднократно и го стартирайте след изтегляне на
промените:

```bash
git pull --ff-only
cd nmea_sproxy
chmod +x update.sh
./update.sh
```

За разлика от updater-а на AISMixer, `nmea_sproxy/update.sh` обновява
инсталираните файлове и презарежда systemd, но умишлено **не рестартира**
singleton услугата или template instances. Когато сте готови, рестартирайте
само избраните връзки:

```bash
systemctl restart nmea_sproxy.service
systemctl restart nmea_sproxy@boat.service
```

### Граница на сигурността

UDPSEC е специфичният за AISMixer автентикиран и криптиран UDP транспорт, а не
външен стандарт. Той взаимно удостоверява конфигурираните идентичности на
крайните точки, защитава поверителността и целостта на пакетите при пренос и
проверява за replay и liveness. Свойствата му за forward secrecy зависят от
унищожаването на ефемерните тайни и от това крайните точки да не са
компрометирани, докато тези тайни са активни; дизайнът има инженерна, но не и
формална криптографска проверка. UDPSEC не доказва, че AIS payload-ът е
семантично верен или физически точен. Изрично конфигурираният plain UDP не
предоставя поверителност, автентикация на станцията, криптографска цялост,
защита от replay или liveness протокол. За ключове, доверие, конфигурация,
поведение на сесиите, endpoint policy и диагностика вижте [ръководството за
`nmea_sproxy`](nmea_sproxy/README.md), [политиката за сигурност](SECURITY.md),
[договора за поведение](BEHAVIORAL_CONTRACT.md) и
[Wiki](https://github.com/iliyan85/aismixer/wiki).

## ✅ Текущи възможности

### 📡 Входове и изходи

- UDP входове по IPv4 и IPv6 с незадължителни списъци с разрешени адреси.
- Автентикиран и криптиран UDPSEC вход, съвместим с `nmea_sproxy`.
- Физически сериен порт и USB virtual COM вход чрез `nmea_sproxy`, както и
  изрично зададен plain UDP за доверени мрежи.
- Незадължително обвързване на изходния адрес за AISMixer и proxy изходите.
- Broadcast UDP изход в legacy режим и именувани UDP цели в routing режим.

### ⚙️ Обработка

- Извличане на поддържаните `!AIVDM`, `!AIVDO` и съвместими AIS изречения.
- Multipart сглобяване независимо от реда на пристигане; точните повторения са
  идемпотентни, а конфликтните фрагменти обезсилват активната група.
- Детерминистична обработка на NMEA TAG `s`/`c`/`g`, съобразена с жизнения цикъл.
- Решения за multipart дедупликация, атомарни за групата: глобални в legacy
  режим и отделни за всеки `target_id` в routing режим.
- Ограничените локални за процеса опашки за ingress, обработка и egress прилагат
  backpressure; изменяемото състояние на слоя за данни има изрична граница на
  жизнен цикъл и reset, притежавана от процесорната инстанция.
- Изрични локални за процеса TTL жизнени цикли, незадължителни ограничения на
  референтното състояние и свежи неизменяеми pull-based снимки на runtime
  статистиката.

### 🔀 Маршрутизация и експлоатация

- Legacy broadcast режим или статична логическа маршрутизация при стартиране.
- Именувани входни `source_id` и изходни `target_id` идентификатори.
- Логически зони на източници с `include`, `union`, `intersection` и `difference`.
- Един неизменяем `ProcessingSnapshot`, обвързан едва когато входният frame бъде
  допуснат; routing режимът изпълнява едно съпоставяне на източника в този момент.
- Незадължителна атомарна подмяна на маршрутизацията чрез `aismixerctl`.
- Наблюдавани в един процес задачи за ingress, обработка и egress;
  незадължителният control listener има отделно управление на жизнения цикъл.
- Стандартното разполагане под Linux използва скриптове за жизнения цикъл,
  systemd и `/usr/local/bin/aismixerctl`; подписаното APK хранилище за OpenWrt
  25.12 x86_64 предоставя Python пакети, интегрирани с procd.

## 🔀 Архитектура

AISMixer държи разделени слоя за данни и незадължителния слой за управление.

### 📡 Слой за данни

UDP и UDPSEC входовете създават неизменяеми ingress frames в отделни ограничени
опашки. Fan-in ги допуска до ограничената обработка, преди единственият
притежаващ състоянието процесор да сканира NMEA данните, да сглобява multipart
съобщения, да прилага глобална или отделна за всяка цел дедупликация и да
изгражда контролирани TAG метаданни. Пълните опашки между етапите на AISMixer
изчакват с backpressure, вместо да отхвърлят елементите си.

Всеки допуснат frame има един обвързан `ProcessingSnapshot`; routing режимът
добавя едно съпоставяне на източника. Непразните `OutputBatch` стойности
преминават подредено през ограничено предаване към egress и обработката изчаква
локалното му завършване, преди да продължи. При грешка в наблюдавана runtime
задача няма автоматично повторно изпращане.

### 🎛️ Слой за управление

Когато е включена, локалната NDJSON услуга през Unix-domain socket проверява
заявките спрямо наличните идентификатори на цели и атомарно заменя неизменяемото,
локално за процеса състояние на маршрутизацията. Тя предоставя също runtime
статус и достъпна само за четене агрегирана runtime статистика, както и
статистика по входове и изходи, чрез `aismixerctl`.

### 🧩 Основни компоненти

| Компонент | Роля |
|---|---|
| `aismixer.py` | Runtime жизнен цикъл, ingress fan-in, обработка, изход и незадължително управление |
| `core/routing*.py` | Логическа маршрутизация и неизменяеми моментни снимки |
| `core/routing_control*.py` / `core/runtime_control.py` | Протокол за управление и Unix-domain услуга |
| `core/runtime_statistics.py` | Локални за процеса runtime броячи и неизменяеми изгледи |
| `aismixerctl.py` | Операторски CLI за управление и статистика |
| `aismixer_secure.py` | UDPSEC вход |
| `nmea_sproxy/` | Прокси при станцията от UDP/serial към UDPSEC/UDP |
| `assembler.py` / `dedup.py` | Multipart сглобяване и премахване на дубликати |
| `meta_writer.py` / `meta_cleaner.py` | NMEA TAG изход и почистване на входа |
| `forwarder.py` | UDP broadcast и целеви изход |

Нормативната семантика на обработката и runtime поведението е в
[BEHAVIORAL_CONTRACT.md](BEHAVIORAL_CONTRACT.md). Този README е операторски
преглед, а не втора спецификация.

## 🗺️ Логическа маршрутизация

### Legacy broadcast

Без секция `routing:` на най-горното ниво дедупликацията е глобална и всяко
прието изречение отива към всеки forwarder. Forwarder-и без име остават валидни.

### Статична маршрутизация

Валидна секция `routing:` на най-горното ниво включва подредени маршрути от
идентификатори или логически зони на източници към именувани forwarder-и. Зоните
са множества от `source_id`, а не географски области, MMSI списъци или филтри по
плавателен съд. Дедупликацията се изпълнява отделно за всяка цел.

Каноничните идентификатори включват `udp:<input-id>`, `udp:<mapped-alias>`,
`udp:<remote-ip>`, `udpsec:<authenticated-station-id>` и
`udp:<forwarder-id>` за именуваните UDP цели. Маршрутизацията използва вътрешните
идентификатори на източниците, а не излъчения TAG `s`. Вижте [примера за статична
маршрутизация](examples/config-routing.yaml).

### Маршрутизация по време на работа

Когато локалното управление е включено, `routing.replace` и `routing.disable`
засягат чрез една атомарна промяна frame-овете, които още не са допуснати до
ограничената обработка; вече допуснатата работа запазва обвързаната си снимка.
Промените са локални за процеса и изчезват при рестарт. Вижте [примера за
управление](examples/config-routing-control.yaml) и [update
payload-а](examples/routing-update.yaml).

## 🏷️ Поведение на NMEA TAG метаданните

AISMixer чете входните TAG метаданни и излъчва контролирани `s`/`c`/`g`
метаданни. Излъченият `s` се избира отделно от вътрешния идентификатор за
маршрутизация и се филтрира за NMEA изход. Според конфигурацията `c` може да
запази валидно входно време или да използва сървърното време, а multipart `g`
може да запази съгласуван входен групов идентификатор или да генерира един
изходен идентификатор. TAG `g` е метаданна, а не ключ за multipart сглобяване.

```yaml
g_preserve_ingress_gid: true
g_id_digits: 18
g_always_tag_single: false
c_preserve_ingress_c: true
```

Точните правила за приоритет, притежание на multipart състоянието, конфликт,
изтичане и съвместимост са в [договора за поведение](BEHAVIORAL_CONTRACT.md).

## 📚 Примери и тестове

- [`examples/config-routing.yaml`](examples/config-routing.yaml) — статична
  маршрутизация.
- [`examples/config-routing-control.yaml`](examples/config-routing-control.yaml)
  — статична маршрутизация с локално управление.
- [`examples/routing-update.yaml`](examples/routing-update.yaml) — директна
  промяна на маршрутизацията за `aismixerctl`.
- [`examples/README.md`](examples/README.md) — ръководство за примерните файлове.

Всички примерни адреси, идентификатори, портове, пътища и ключове трябва да се
адаптират от оператора. Стартирайте тестовете в хранилището с:

```bash
python -m pytest
```

Тестовете с действителен Unix-domain listener изискват Linux, WSL, Raspberry Pi
OS или друга POSIX среда с поддръжка на asyncio Unix sockets.

## ⚠️ Текущи ограничения

- UDP е единственият реализиран изходен адаптер на AISMixer.
- Състоянието, поколенията и runtime статистиката на маршрутизацията са локални
  за процеса; промените по време на работа не се запазват.
- Защитеното състояние за replay, сесии и nonce е локално за процеса и нетрайно;
  почистването при изтичане се задейства от разрешен трафик.
- Текущата интеграция оставя незадължителните ограничения за дедупликация и
  сглобяване на `None`.
- Няма координаторен процес или отделни ingress/egress worker процеси, IPC,
  междупроцесна маршрутизация или агрегиране на метрики, автоматичен рестарт на
  worker процеси или презареждане на конфигурацията.
- Няма географско филтриране, филтриране по MMSI или съдържание на плавателния
  съд, дългосрочно съхранение, анализи или откриване на spoofing.
- Локалното управление изисква POSIX Unix sockets и разчита на разрешенията на
  файловата система; няма приложен token или политика с отделен служебен акаунт.

## 📖 Карта на документацията

- [Договор за поведение](BEHAVIORAL_CONTRACT.md) — нормативна семантика на
  обработката и runtime поведението.
- [Операторско ръководство за `nmea_sproxy`](nmea_sproxy/README.md) — пълни
  указания за инсталация, конфигурация, ключове, serial input, изход, услуги и
  диагностика при станцията.
- [GitHub Wiki](https://github.com/iliyan85/aismixer/wiki) — по-задълбочена
  архитектура и обяснителни материали.
- [Примери](examples/README.md) · [Политика за сигурност](SECURITY.md) ·
  [Ръководство за принос](CONTRIBUTING.md) · [План за развитие](ROADMAP.md) ·
  [Публичен уебсайт](https://aismixer.net)

## 🌿 Клонове и уебсайт

`main` съдържа услугата, проксито, примерите, компонентите за управление и
`tests/`. Публичният уебсайт е в `website`; GitHub Pages използва `/docs` от този
клон, затова `docs/` умишлено отсъства от `main`.

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
> Rutarea logică opțională direcționează sursele de ingress către destinații UDP
> denumite, iar interfața locală `aismixerctl` oferă stare și statistici runtime,
> precum și actualizări atomice ale rutării.

## 🚀 Pornire rapidă

Alegeți calea de instalare potrivită gazdei: Linux convențional cu systemd sau
pachetele APK publicate pentru OpenWrt, integrate cu procd.

### Linux convențional cu systemd

Scripturile ciclului de viață pot rula direct ca root. Când sunt invocate de un
utilizator non-root, ele ridică privilegiile operațiilor necesare prin `sudo`; se
opresc cu o explicație dacă niciuna dintre variante nu este disponibilă.

Comenzile `systemctl` independente și comenzile care scriu sub `/etc` presupun
un shell root; operatorii non-root trebuie să le prefixeze cu `sudo` când este
necesar.

#### Instalare nouă

Pe o gazdă Debian sau Raspberry Pi OS bazată pe systemd:

```bash
git clone https://github.com/iliyan85/aismixer
cd aismixer
chmod +x install.sh
./install.sh
systemctl start aismixer
systemctl status aismixer
```

`install.sh` instalează runtime-ul în `/opt/aismixer`, creează fișierele lipsă
din `/etc/aismixer` păstrând configurația și cheile existente, instalează
`/usr/local/bin/aismixerctl` și activează AISMixer pentru pornire la boot. În mod
intenționat, **nu** pornește serviciul. Verificați
`/etc/aismixer/config.yaml` înainte de a expune serviciul în afara unei rețele de
încredere.

#### Actualizare

Repository-ul urmărește scripturile ciclului de viață fără bitul executabil.
Înainte de prima actualizare, rulați o singură dată `chmod +x update.sh`. Apoi
actualizați din checkout:

```bash
git pull --ff-only
./update.sh
systemctl status aismixer
```

`update.sh` actualizează fișierele runtime instalate, unitatea systemd și
`aismixerctl`, reîncarcă systemd și **repornește AISMixer**. Configurația și
cheile operatorului din `/etc/aismixer` rămân nemodificate. `uninstall.sh`
păstrează acel director dacă `--purge-config` nu este cerut explicit.

### OpenWrt 25.12 (numai x86_64)

Pentru OpenWrt 25.12 pe x86_64 este publicat un repository APK v3 semnat.
Pachetele conțin implementarea Python actuală și se integrează cu procd. Aceasta
este singura țintă OpenWrt publicată în prezent. Rulați ca root:

```sh
wget -O /etc/apk/keys/aismixer-openwrt.pem \
  https://aismixer.net/openwrt/keys/aismixer-openwrt.pem

chmod 0644 /etc/apk/keys/aismixer-openwrt.pem

REPO_FILE=/etc/apk/repositories.d/customfeeds.list
REPO_URL='https://aismixer.net/openwrt/25.12/x86_64/packages.adb'

grep -qxF "$REPO_URL" "$REPO_FILE" 2>/dev/null || {
  printf '\n# AISMixer OpenWrt 25.12 x86_64 repository\n%s\n' \
    "$REPO_URL" >> "$REPO_FILE"
}

apk update
apk add aismixer
```

Amprenta SHA-256 a cheii publice a repository-ului este
`170d30219e0e05d59898cd8ccd5ec9804e915df7882ab56b8e869ef6e99c8f9c`.
Pachetul `aismixer` instalează automat dependența comună `aismixer-common`; în
mod normal nu trebuie să o instalați manual. Pentru a instala în schimb doar
pachetul de la stație:

```sh
apk add nmea_sproxy
```

La prima pornire a serviciului, AISMixer generează sau repară automat, după caz,
identitatea locală a serverului, folosește structura OpenWrt pentru configurare
și identitate din `/etc/aismixer` și pornește prin procd. `nmea_sproxy` procedează
la fel pentru identitatea locală a stației.

Pentru UDPSEC, încrederea în peer nu este configurată niciodată automat. Copiați
`/etc/aismixer/keys/aismixer_public.pem` de pe gazda AISMixer la calea
`remote_public_key` configurată pe proxy (implicit
`/etc/nmea_sproxy/keys/aismixer_public.pem`), apoi autorizați cheia publică de
identitate a stației în AISMixer conform [ghidului
`nmea_sproxy`](nmea_sproxy/README.md#authorize-the-station-in-aismixer). Dacă acea
cheie publică configurată pentru peer lipsește, nu poate fi citită sau este
invalidă, verificarea preliminară menține intenționat serviciul oprit în loc să
permită o buclă de respawn. Acesta este un comportament sigur așteptat, nu un
eșec al instalării pachetului. UDP simplu configurat explicit nu necesită o
cheie publică pentru peer.

## 🧭 Ce este AISMixer?

**AISMixer** este un serviciu Python orientat spre exploatare reală pentru
recepționarea, normalizarea, deduplicarea, etichetarea, rutarea și
redirecționarea fluxurilor AIS NMEA 0183.

- **`aismixer.py`** este serviciul de mixare și plan de date care rulează
  continuu.
- **`aismixerctl`** este comanda instalată pentru operatori, destinată socket-ului
  local opțional de control.
- **`nmea_sproxy`** este proxy-ul de la stație pentru o intrare UDP locală sau
  serială și o ieșire UDPSEC sau UDP simplu configurată explicit.

```text
Receptor AIS UDP      \
Receptor AIS UDP       \        +----------------+       +----------------+
nmea_sproxy UDPSEC/UDP ------> |    AISMixer    | ----> | Destinații UDP |
                                |  plan de date  |       +----------------+
                                +----------------+
                                         ^
                                         |
                                  control local opțional
                                         |
                                   aismixerctl
```

## ⚙️ Configurație de bază

Serviciul instalat citește `/etc/aismixer/config.yaml`. Fără o secțiune
top-level `routing:`, AISMixer folosește modul legacy broadcast: deduplicarea
este globală, iar fiecare propoziție acceptată este trimisă către fiecare
forwarder configurat.

```yaml
station_id: mixstation_1

udp_inputs:
  - id: roof_receiver
    listen_ip: "0.0.0.0"
    listen_port: 17777

forwarders:
  - id: local_display
    host: 127.0.0.1
    port: 19000
```

Exemplele din repository sunt inactive până când sunt copiate sau adaptate.
Consultați [`examples/config-routing.yaml`](examples/config-routing.yaml) pentru
rutare statică și
[`examples/config-routing-control.yaml`](examples/config-routing-control.yaml)
pentru rutare cu controlul local activat.

### Controale pentru endpoint-urile de rețea

- `listen_ip` selectează o singură familie de adrese. Folosiți intrări separate
  pentru ingress dual-stack IPv4 și IPv6.
- Valorile opționale `udp_inputs[].allow_from` și `sec_inputs[].allow_from` sunt
  liste de adrese IP/rețele CIDR literale permise la nivelul aplicației. Omiterea
  nu aplică un ACL al aplicației; o listă explicit goală respinge toate pachetele
  listener-ului respectiv.
- `forwarders[].source_ip` opțional asociază socket-ul UDP de ieșire cu o adresă
  locală sursă literală.

Aceste controale completează, nu înlocuiesc, firewall-ul și politica de rutare a
gazdei.

## 🧰 `aismixerctl`

În instalarea Linux/systemd convențională, `install.sh` instalează
`/usr/local/bin/aismixerctl`. Pachetul `aismixer` pentru OpenWrt instalează
aceeași comandă pentru operatori la calea `/usr/bin/aismixerctl`. Comanda
comunică prin socket-ul local opțional de control din domeniul Unix; nu modifică
fișierele de configurație și nu persistă rutarea runtime după repornire.

### Activarea controlului local

Serviciul de control rămâne dezactivat până când este activat explicit în
configurația AISMixer:

```yaml
control:
  unix:
    enabled: true
    socket_path: /run/aismixer/control.sock
    socket_mode: "0660"
```

Unitatea systemd instalată creează `/run/aismixer` cât timp serviciul rulează.
Proprietarul și modul socket-ului determină accesul; nu există token de
autentificare la nivelul aplicației. Interfața necesită suport POSIX pentru
socket-uri din domeniul Unix.

### Flux interactiv

Shell-ul interactiv este cea mai rapidă cale de a descoperi starea curentă și de
a inspecta contoarele locale procesului. Cu socket-ul implicit deținut de root:

```text
sudo aismixerctl
aismixerctl> status
aismixerctl> show statistics
aismixerctl> show statistics inputs
aismixerctl> show statistics outputs
```

Folosiți `help` în shell. Comenzile pentru statisticile intrărilor și ieșirilor
acceptă și un filtru opțional. Comenzile one-shot rămân disponibile pentru
scripturi.

### Actualizări de rutare

Înlocuiți sau dezactivați atomic snapshot-ul activ de rutare, local procesului:

```bash
sudo aismixerctl replace --file examples/routing-update.yaml --expected-generation 3
sudo aismixerctl disable --expected-generation 4
```

Generația așteptată este opțională; atunci când este furnizată, împiedică o
actualizare învechită să suprascrie o stare mai nouă. CLI-ul nu reîncearcă
automat.

## 🔐 `nmea_sproxy`

`nmea_sproxy` este proxy-ul de rețea de la stație. Un proces sau o instanță de
serviciu reprezintă o relație: o intrare UDP locală sau serială/USB fizică spre
o ieșire de rețea. Instrucțiunile de mai jos pentru instalare, actualizare și
instanțe template systemd descriu calea Linux convențională; OpenWrt folosește
modelul singleton APK/procd descris în secțiunea Pornire rapidă. UDPSEC este
transportul securizat/implicit spre AISMixer; UDP simplu trebuie selectat
explicit și este destinat numai conexiunilor LAN/VPN de încredere.

### Instalare și pornire

Din checkout-ul AISMixer:

```bash
cd nmea_sproxy
chmod +x install.sh
./install.sh
```

Programul de instalare al proxy-ului pregătește structura de configurare și
perechea de chei a stației, păstrând materialul existent al operatorului,
instalează unitățile singleton și template, activează numai singleton-ul și nu
pornește niciun serviciu. Finalizați configurarea relației și, pentru modul
UDPSEC implicit, configurarea cheii publice AISMixer de încredere descrisă în [ghidul
operatorului](nmea_sproxy/README.md), apoi porniți serviciul:

```bash
systemctl start nmea_sproxy.service
systemctl status nmea_sproxy.service
```

### Instanțe singleton și template

Singleton-ul citește `/etc/nmea_sproxy/config.yaml`. Instanțele template precum
`boat`, `yacht` sau `roof` folosesc nume de relații alese de operator și
configurații din `/etc/nmea_sproxy/instances/`. Copiați și editați fișierul
relației înainte de a-i porni instanța:

```bash
cp /etc/nmea_sproxy/config.yaml /etc/nmea_sproxy/instances/boat.yaml
systemctl enable --now nmea_sproxy@boat.service
systemctl status nmea_sproxy@boat.service
```

Rulați o altă instanță template atunci când este necesară o altă relație
independentă. O instalare bazată numai pe instanțe template trebuie să
dezactiveze și să oprească singleton-ul nefolosit cu
`systemctl disable --now nmea_sproxy.service`.

### Actualizare

Updater-ul proxy-ului este, de asemenea, urmărit fără bit executabil. Din
rădăcina repository-ului, faceți-l executabil o singură dată și rulați-l după
preluarea modificărilor:

```bash
git pull --ff-only
cd nmea_sproxy
chmod +x update.sh
./update.sh
```

Spre deosebire de updater-ul AISMixer, `nmea_sproxy/update.sh` actualizează
fișierele instalate și reîncarcă systemd, dar intenționat **nu repornește**
singleton-ul sau nicio instanță template. Reporniți numai relațiile alese, când
sunteți pregătit:

```bash
systemctl restart nmea_sproxy.service
systemctl restart nmea_sproxy@boat.service
```

### Limită de securitate

UDPSEC este transportul UDP autentificat și criptat specific AISMixer, nu un
standard extern. Autentifică reciproc identitățile configurate ale
endpoint-urilor, protejează confidențialitatea și integritatea pachetelor în
tranzit și verifică replay-ul și liveness-ul. Proprietățile sale de forward
secrecy depind de eliminarea secretelor efemere și de faptul că endpoint-urile nu
sunt compromise cât timp acele secrete sunt active; designul are validare
inginerească, nu verificare criptografică formală. UDPSEC nu dovedește că
payload-urile AIS sunt adevărate semantic sau exacte fizic. UDP simplu explicit
nu oferă confidențialitate, autentificarea stației, integritate criptografică,
protecție anti-replay sau protocol de liveness. Pentru chei,
încredere, configurare, comportamentul sesiunilor, politica endpoint-urilor și
depanare, consultați [ghidul `nmea_sproxy`](nmea_sproxy/README.md), [politica de
securitate](SECURITY.md), [contractul comportamental](BEHAVIORAL_CONTRACT.md) și
[Wiki](https://github.com/iliyan85/aismixer/wiki).

## ✅ Capabilități actuale

### 📡 Intrări și ieșiri

- Ingress UDP prin IPv4 și IPv6, cu liste opționale de adrese permise la nivelul
  aplicației.
- Ingress UDPSEC autentificat și criptat, compatibil cu `nmea_sproxy`.
- Intrare serială fizică și USB virtual COM prin `nmea_sproxy`, plus UDP simplu
  explicit pentru rețele de încredere.
- Asocierea opțională a adresei-sursă pentru ieșirile AISMixer și proxy.
- Egress UDP broadcast în modul legacy și destinații UDP denumite în modul de
  rutare.

### ⚙️ Procesare

- Extragerea propozițiilor AIS `!AIVDM`, `!AIVDO` și compatibile acceptate.
- Asamblare multipart complet independentă de ordinea sosirii; repetările exacte
  sunt idempotente, iar fragmentele conflictuale invalidează grupul activ.
- Gestionare deterministă a NMEA TAG `s`/`c`/`g`, care respectă ciclul de viață.
- Decizii de deduplicare multipart atomice la nivel de grup: globale în modul
  legacy și separate pentru fiecare `target_id` în modul de rutare.
- Cozile limitate, locale procesului, pentru ingress, procesare și egress aplică
  backpressure; starea mutabilă a planului de date are o limită explicită de
  ciclu de viață și reset deținută de instanța procesorului.
- Cicluri de viață TTL explicite, locale procesului, capacități opționale ale
  stării de referință și snapshot-uri runtime proaspete, imuabile și pull-based.

### 🔀 Rutare și operare

- Mod legacy broadcast sau rutare logică statică încărcată la pornire.
- Identități denumite `source_id` pentru ingress și `target_id` pentru egress.
- Zone logice de surse cu `include`, `union`, `intersection` și `difference`.
- Un `ProcessingSnapshot` imuabil, asociat numai când un cadru de ingress este
  admis; modul de rutare efectuează atunci o singură potrivire a sursei.
- Înlocuirea atomică opțională a rutării prin `aismixerctl`.
- Task-uri supravegheate local procesului pentru ingress, procesare și egress;
  listener-ul de control opțional are un ciclu de viață gestionat separat.
- Instalarea convențională pe Linux folosește scripturi pentru ciclul de viață,
  systemd și `/usr/local/bin/aismixerctl`; repository-ul APK semnat pentru
  OpenWrt 25.12 x86_64 furnizează pachete Python integrate cu procd.

## 🔀 Arhitectură

AISMixer păstrează separate planul de date și planul de control opțional.

### 📡 Planul de date

Producătorii UDP și UDPSEC creează cadre de ingress imuabile în cozi private
limitate. Fan-in le admite în procesarea limitată înainte ca procesorul unic,
care deține starea, să scaneze datele NMEA, să asambleze mesajele multipart, să
aplice deduplicarea globală sau separată pe destinații și să construiască
metadate TAG controlate. Cozile pline ale etapelor AISMixer așteaptă cu
backpressure în loc să elimine elementele aflate în coadă.

Fiecare cadru admis are un `ProcessingSnapshot` asociat; modul de rutare adaugă
o singură potrivire a sursei. Valorile `OutputBatch` ne-goale traversează
ordonat un handoff egress limitat, iar procesarea așteaptă finalizarea locală a
egress-ului înainte de a continua. Dacă un task runtime supravegheat eșuează,
nu există reluarea automată a livrării.

### 🎛️ Planul de control

Când este activat, serviciul local NDJSON prin socket din domeniul Unix validează
cererile în raport cu ID-urile de destinație disponibile și înlocuiește atomic
starea de rutare imuabilă, locală procesului. De asemenea, expune starea și
statistici runtime read-only agregate, per-input și per-output prin
`aismixerctl`.

### 🧩 Componente principale

| Componentă | Rol |
|---|---|
| `aismixer.py` | Ciclul de viață runtime, fan-in ingress, procesare, egress și control opțional |
| `core/routing*.py` | Rutare logică și snapshot-uri imuabile |
| `core/routing_control*.py` / `core/runtime_control.py` | Protocol de control și serviciu prin socket Unix |
| `core/runtime_statistics.py` | Contoare runtime locale procesului și vizualizări imuabile |
| `aismixerctl.py` | CLI pentru control și statistici destinat operatorului |
| `aismixer_secure.py` | Ingress UDPSEC |
| `nmea_sproxy/` | Proxy la stație de la UDP/serial la UDPSEC/UDP |
| `assembler.py` / `dedup.py` | Asamblare multipart și suprimarea duplicatelor |
| `meta_writer.py` / `meta_cleaner.py` | Ieșire NMEA TAG și curățarea ingress-ului |
| `forwarder.py` | Broadcast UDP și egress direcționat |

Semantica normativă de procesare și runtime se află în
[BEHAVIORAL_CONTRACT.md](BEHAVIORAL_CONTRACT.md). Acest README este o prezentare
pentru operatori, nu o a doua specificație.

## 🗺️ Rutare logică

### Legacy broadcast

Fără o secțiune top-level `routing:`, deduplicarea este globală și fiecare
propoziție acceptată ajunge la fiecare forwarder. Forwarderele fără nume rămân
valide.

### Rutare statică

O secțiune top-level `routing:` validă activează rute ordonate de la ID-uri de
sursă sau zone logice către forwardere denumite. Zonele sunt mulțimi de ID-uri
de sursă, nu regiuni geografice, liste MMSI sau filtre de nave. Deduplicarea este
separată pentru fiecare destinație.

Identitățile canonice includ `udp:<input-id>`, `udp:<mapped-alias>`,
`udp:<remote-ip>`, `udpsec:<authenticated-station-id>` și
`udp:<forwarder-id>` pentru destinații UDP denumite. Rutarea potrivește ID-urile
interne ale surselor, nu etichetele TAG `s` emise. Consultați [exemplul
static](examples/config-routing.yaml).

### Rutare la runtime

Când controlul local este activat, `routing.replace` și `routing.disable`
afectează printr-o schimbare atomică de snapshot cadrele care nu au fost încă
admise în procesarea limitată; lucrul deja admis își păstrează snapshot-ul
asociat. Actualizările sunt locale procesului și dispar la repornire. Consultați [exemplul
de control](examples/config-routing-control.yaml) și [payload-ul de
actualizare](examples/routing-update.yaml).

## 🏷️ Comportamentul metadatelor NMEA TAG

AISMixer citește metadatele TAG de ingress și emite metadate `s`/`c`/`g`
controlate. Eticheta `s` emisă este aleasă separat de ID-ul intern al sursei de
rutare și este sanitizată pentru ieșirea NMEA. În funcție de configurație, `c`
poate păstra un timp de ingress valid sau poate folosi timpul serverului, iar
`g` multipart poate păstra un ID de grup ingress agreat sau poate genera un ID
de ieșire. TAG `g` este metadată, nu cheia assemblerului multipart.

```yaml
g_preserve_ingress_gid: true
g_id_digits: 18
g_always_tag_single: false
c_preserve_ingress_c: true
```

Regulile exacte pentru prioritate, proprietatea stării multipart, conflict,
expirare și compatibilitate aparțin [contractului
comportamental](BEHAVIORAL_CONTRACT.md).

## 📚 Exemple și testare

- [`examples/config-routing.yaml`](examples/config-routing.yaml) — rutare statică.
- [`examples/config-routing-control.yaml`](examples/config-routing-control.yaml)
  — rutare statică cu control local.
- [`examples/routing-update.yaml`](examples/routing-update.yaml) — actualizare
  directă de rutare pentru `aismixerctl`.
- [`examples/README.md`](examples/README.md) — ghidul fișierelor de exemplu.

Toate adresele, ID-urile, porturile, căile și cheile de exemplu necesită
adaptarea operatorului. Rulați suita de teste a repository-ului cu:

```bash
python -m pytest
```

Testele reale pentru listener-ul din domeniul Unix necesită Linux, WSL,
Raspberry Pi OS sau alt mediu POSIX cu suport asyncio pentru socket-uri Unix.

## ⚠️ Limitări actuale

- UDP este singurul adaptor egress AISMixer implementat.
- Starea, generațiile și statisticile runtime de rutare sunt locale procesului;
  modificările runtime nu sunt persistente.
- Starea securizată pentru replay, sesiuni și nonce-uri este locală procesului și
  nepersistentă; curățarea la expirare este determinată de traficul permis.
- Integrarea curentă a serviciului lasă capacitățile opționale de deduplicare și
  asamblare la `None`.
- Nu există procese coordonator sau worker ingress/egress, IPC, rutare ori
  agregare de metrici între procese, restart automat al workerilor sau
  reîncărcare automată a configurației.
- Nu există filtrare geografică, după MMSI ori conținutul navei, stocare pe termen
  lung, analiză sau detectare a spoofing-ului.
- Controlul local necesită socket-uri Unix POSIX și se bazează pe permisiunile
  sistemului de fișiere; nu există token al aplicației sau politică pentru un
  cont de serviciu dedicat.

## 📖 Harta documentației

- [Contract comportamental](BEHAVIORAL_CONTRACT.md) — semantica normativă de
  procesare și runtime.
- [Ghidul operatorului pentru `nmea_sproxy`](nmea_sproxy/README.md) — ghid complet
  la stație pentru instalare, configurare, chei, serial, ieșire, servicii și
  depanare.
- [GitHub Wiki](https://github.com/iliyan85/aismixer/wiki) — arhitectură mai
  aprofundată și material explicativ.
- [Exemple](examples/README.md) · [Politica de securitate](SECURITY.md) ·
  [Ghid de contribuție](CONTRIBUTING.md) · [Foaie de parcurs](ROADMAP.md) ·
  [Site public](https://aismixer.net)

## 🌿 Ramuri și site web

`main` conține serviciul, proxy-ul, exemplele, componentele de control și
`tests/`. Site-ul public se află pe `website`; GitHub Pages folosește `/docs` din
acea ramură, astfel că `docs/` lipsește intenționat din `main`.

[⬆ Înapoi la selectorul de limbă](#english)
