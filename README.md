<a id="languages"></a>

**[English](#english) · [Български](#bulgarian) · [Română](#romanian)**

<a id="english"></a>

# 🛰️ AISMixer — AIS NMEA 0183 stream processing and routing

**Normalize · Deduplicate · Tag · Route · Forward**

AISMixer's source code is publicly available under [CC BY-NC 4.0](LICENSE).
Use is subject to the license terms, including its non-commercial restriction.

## 🧭 What AISMixer does

AISMixer receives AIS NMEA 0183 data from multiple receivers, extracts
supported `!AIVDM` and `!AIVDO` sentences, reassembles multipart messages,
removes near-real-time duplicates, manages NMEA 4.0 TAG metadata, and sends
clean logical output streams to configured UDP destinations.

Its current operator-facing components are:

- `aismixer` — the long-running mixer, router, and data-plane service;
- `aismixerctl` — the local routing-control and runtime-statistics CLI;
- `nmea_sproxy` — the station-side UDP/serial-to-UDPSEC or plain-UDP proxy.

Core capabilities include:

- UDP ingress over IPv4 and IPv6;
- authenticated and encrypted UDPSEC ingress;
- serial and USB virtual-serial reception through `nmea_sproxy`;
- multipart AIS assembly and group-atomic deduplication;
- controlled TAG `s`, `c`, and `g` handling;
- global fan-out or logical routing to named UDP targets;
- optional ingress allow-lists and outbound source-address binding;
- bounded queues, backpressure, and process-local operational statistics.

### ⚙️ Processing model

Each ingress datagram is scanned for supported AIS NMEA sentences. Multipart
fragments may arrive out of order; exact repeats are idempotent, while a
conflicting fragment invalidates that live group. Completed multipart messages
are deduplicated and emitted as one group so a destination does not receive a
partial duplicate.

In legacy mode, duplicate suppression is global. In routing mode, it is scoped
per target, so the same logical AIS message may legitimately reach two distinct
destinations once each. Bounded ingress, processing, and egress queues apply
backpressure rather than creating unbounded memory growth.

TAG construction, multipart assembly, deduplication, and routing operate in one
ordered processing pipeline. Exact conflict, timeout, capacity, reset, and
snapshot rules are documented in the behavioural contract.

```text
AIS receivers over UDP ──────┐
                             │
serial/UDP receiver          v
        │               +-----------+      +------------------+
        └─ nmea_sproxy → | aismixer | ───→ | UDP destinations |
           UDPSEC/UDP    +-----------+      +------------------+
                               ^
                               |
                         aismixerctl
                    optional local control
```

The [behavioural contract](BEHAVIORAL_CONTRACT.md) owns exact tested
processing, routing, runtime, and UDPSEC semantics. This README is the
project and operator overview.

## 🚀 Quick start and lifecycle

Choose conventional Linux with systemd or the versioned OpenWrt APK packages
with procd.

### 📦 Conventional Linux with systemd

The lifecycle scripts run directly as root or use `sudo` for another
administrator; they stop with an explanation if neither is available.
Examples below use `sudo`. When already root, omit it and edit privileged
files with the administrator's editor.

#### 📦 Install

On a systemd-based Debian or Raspberry Pi OS host:

```bash
git clone https://github.com/iliyan85/aismixer
cd aismixer
./install.sh
```

The installer places the runtime under `/opt/aismixer`, installs
`/usr/local/bin/aismixerctl`, seeds only missing files under
`/etc/aismixer`, preserves existing configuration and keys, and enables the
service at boot. It intentionally does **not** start the service.

#### ⚙️ Configure before the first start

Review the installed configuration and trust/network policy first:

```bash
sudoedit /etc/aismixer/config.yaml
sudoedit /etc/aismixer/authorized_keys.yaml
```

The seeded configuration contains plain UDP listeners bound broadly and
without application allow-lists. Before starting, adapt addresses, ports,
`allow_from` rules, forwarders, UDPSEC authorization, host firewall rules,
and routing policy for the deployment.

Plain UDP has no UDPSEC confidentiality, authentication, cryptographic
integrity, replay protection, or liveness checks. Application allow-lists
complement rather than replace the host firewall.

#### 🚀 Start, inspect, and follow logs

```bash
sudo systemctl start aismixer
sudo systemctl status aismixer
sudo journalctl -u aismixer -f
```

The installed unit already enables start at boot. If boot enablement was
changed later, run `sudo systemctl enable aismixer`.

#### 📦 Update

From the checkout:

```bash
git pull --ff-only
./update.sh
systemctl status aismixer
```

`update.sh` refreshes installed runtime files, the unit, and `aismixerctl`,
reloads systemd, and runs `systemctl restart aismixer`. A restart also starts
an inactive service; the updater does not preserve an intentionally stopped
state. Operator configuration and keys under `/etc/aismixer` are not directly
modified.

#### 📦 Uninstall

Normal uninstall removes the installed runtime, service unit, and CLI while
retaining configuration and keys:

```bash
./uninstall.sh
```

The following form is destructive: it also removes `/etc/aismixer`,
including operator configuration and key material.

```bash
./uninstall.sh --purge-config
```

### 📦 OpenWrt 25.12

AISMixer has versioned OpenWrt 25.12 APK builds with procd integration.
The same package recipe produces:

- `aismixer-common` — shared Python modules, installed as a dependency;
- `aismixer` — mixer/router, UDPSEC server, and `aismixerctl`;
- `nmea_sproxy` — station-side UDP/serial proxy.

The Python/shell payload declares `PKGARCH:=all` because its contents are
architecture-independent. Portability still depends on target-specific
Python, cryptographic, serial, and other runtime packages. The currently
built, published, and validated repository target architectures are
`x86_64` and `mips_24kc`; that list does not mean the source is designed
to exclude other OpenWrt targets with suitable dependencies.

| OpenWrt feed target | Signed repository index |
| --- | --- |
| `x86_64` | [`packages.adb`](https://aismixer.net/openwrt/25.12/x86_64/packages.adb) |
| `mips_24kc` | [`packages.adb`](https://aismixer.net/openwrt/25.12/mips_24kc/packages.adb) |

These are feed target paths, not separate package recipes. The local recipe
pins a versioned source revision, so a published package must not be assumed
to contain every later `main` change. The UDPSEC section below describes the
current source tree; package operators should check the package revision and
[changelog](CHANGELOG.md) before assuming that later hardening is present.

Before installation, verify writable overlay space and establish firewall or
network isolation. OpenWrt's generated package hooks enable and start the
service during `apk add`; the packaged configuration initially includes broad
plain-UDP listeners. Install as root, then stop it immediately and review its
configuration and authorization before putting it into service:

```sh
apk -U add aismixer
/etc/init.d/aismixer stop
vi /etc/aismixer/config.yaml
vi /etc/aismixer/authorized_keys.yaml
/etc/init.d/aismixer start
/etc/init.d/aismixer status
logread -e aismixer
```

The initial automatic start can precede that stop, so apply firewall or
isolation policy before `apk add`. Python and its dependencies require
materially more writable storage than a minimal router image; extroot may be
appropriate when internal overlay space is limited.

Update or remove the mixer package with the device's configured APK feed:

```sh
apk --update-cache add --upgrade aismixer
```

The update hook stops and starts the service even if it was previously stopped,
while preserving its enable/disable state. Remove the package with:

```sh
apk del aismixer
```

Removal stops and disables the service. The package has no project-specific
purge contract, so this README makes no promise about configuration or key
retention after `apk del`.

Install `nmea_sproxy` instead of or alongside the mixer when the router is
the station-side endpoint:

```sh
apk -U add nmea_sproxy
```

Its package hook also attempts to start the service, but a fresh UDPSEC
relation has no trusted mixer public key and normally cannot complete
preflight. Provision trust and restart it by following the component guide;
installation alone does not produce a ready relation.

Deployment defaults differ:

- conventional/source-systemd configuration keeps local control opt-in and
  prepares server identity only when active secure ingress requires it;
- the packaged OpenWrt configuration enables local control, and its init
  service eagerly prepares or repairs the server identity before startup.

Review the installed configuration rather than assuming one deployment's
defaults apply to the other. See the
[OpenWrt deployment guide](https://github.com/iliyan85/aismixer/wiki/OpenWrt-Deployment)
and [`nmea_sproxy` guide](nmea_sproxy/README.md) for deeper package, instance,
storage, serial, and troubleshooting guidance.

## ⚙️ Configuration and network model

The installed mixer reads `/etc/aismixer/config.yaml`. This minimal example
uses one restricted plain-UDP input and one UDP destination:

```yaml
station_id: mixstation_1

udp_inputs:
  - id: roof_receiver
    listen_ip: "0.0.0.0"
    listen_port: 17777
    allow_from:
      - 192.0.2.0/24

forwarders:
  - id: local_display
    host: 127.0.0.1
    port: 19000
```

Adapt all example addresses, ports, IDs, paths, and policy before use.
Repository examples are inactive until copied or adapted.

### 📡 Ingress and forwarders

- `udp_inputs` accepts plain UDP. An `id` gives the input a stable internal
  routing identity.
- `sec_inputs` accepts authenticated UDPSEC and derives routing identity from
  the authenticated station.
- `forwarders` defines UDP destinations. A named forwarder's canonical target
  identity is `udp:<id>`.
- `listen_ip` selects one address family. Use separate listener entries when
  explicit IPv4 and IPv6 ingress are both required.
- `allow_from` accepts literal IP addresses and CIDR networks. Omission applies
  no application ACL; an explicit empty list denies every packet on that
  listener.
- `source_ip` optionally binds a forwarder's outbound UDP socket to a literal
  local address.

Source IP addresses and UDP aliases are operational identifiers, not
cryptographic station identities.

When routing is enabled, every addressable forwarder needs a unique `id`. An
unnamed forwarder remains valid only for legacy fan-out. Sources that match no
route produce no network output in routing mode.

### 🪪 UDPSEC server identity

On conventional/source-systemd deployment, server identity preparation follows
the active `sec_inputs` configuration. A plain-only configuration creates no
server pair. When secure ingress is active, an entirely absent pair can be
created, a valid matching pair is preserved, and partial, invalid, or
mismatched material fails closed without implicit replacement.

Public-key repair is an explicit operator action:

```bash
sudo python3 /opt/aismixer/tools/aismixer_keys.py server --repair-public
```

OpenWrt differs: its init service eagerly prepares or repairs the server
identity before launching the packaged service. Neither deployment ships a
private key.

## 🗺️ Routing, zones, and TAGs

### 🔀 Legacy fan-out

With top-level `routing` absent or null, deduplication is global and every
accepted output sentence is sent to every configured UDP forwarder. Forwarders
do not need IDs in this compatibility mode.

### 🗺️ Static routing

An enabled `routing:` mapping contains both `zones` and `routes`. Named routes
select target subsets and
deduplication is scoped separately to each target. Zones are logical sets of
internal source IDs—not geographic areas, MMSI lists, vessel filters, or
emitted TAG labels.

Routes are evaluated in configuration order. When overlapping routes select
the same target, that target is retained only once for the message; selecting
two distinct targets can produce one send to each.

Zones support:

- `include` for explicit internal source identities;
- `union` for the members of named zones;
- `intersection` for members common to named zones;
- `difference` for members of the first named zone except those in the second.

Routes accept `from_zone`. They do not accept an arbitrary source ID directly;
to route one source, put that identity in a zone with `include` and route from
the zone:

```yaml
routing:
  zones:
    roof_only:
      include:
        - udp:roof_receiver
  routes:
    - name: roof_to_display
      from_zone: roof_only
      to:
        - udp:local_display
```

Typical internal identities include `udp:<input-id>`,
`udp:<mapped-alias>`, `udp:<remote-ip>`, and
`udpsec:<authenticated-station-id>`. Routing matches these internal values;
the emitted TAG `s` value is separate.

See the [static routing example](examples/config-routing.yaml) for complete
named inputs, forwarders, zones, routes, and set operations.

### 🎛️ Runtime routing

When local control is enabled, `aismixerctl replace` and
`aismixerctl disable` atomically change the process-local routing snapshot.
They do not rewrite YAML. Restart restores the routing configuration loaded
from disk.

An optional expected generation prevents a stale operator or automation writer
from overwriting a newer runtime state. Exact processing-admission and snapshot
semantics belong to the behavioural contract.

### 🏷️ NMEA TAG overview

AISMixer reads ingress TAG metadata and emits controlled `s`, `c`, and `g`
values:

- `s` identifies the configured output source label and is sanitized for NMEA;
  it is not the internal routing identity;
- `c` may preserve a valid ingress timestamp or use server time, according to
  configuration;
- `g` relates multipart output and may preserve an agreed ingress group ID or
  use a generated output ID.

TAG `g` is metadata, not the multipart assembler key. Exact priority,
multipart ownership, conflict, expiry, and compatibility rules are normative
in the behavioural contract.

## 🔐 UDPSEC and `nmea_sproxy`

### 🧩 Station-side proxy

`nmea_sproxy` represents one relation per process or service instance:

- one local input: UDP or an OS-provided serial/USB virtual-serial device;
- one network output: UDPSEC or explicitly configured plain UDP.

UDPSEC is the protected/default output model where applicable. Plain UDP must
be selected explicitly and is unauthenticated and unencrypted. UDPSEC requires
a station identity, the trusted mixer public key, and authorization of the
station public key by the mixer.

Conventional Linux deployment and OpenWrt packages are available. Detailed
installation, keys, trust, service instances, serial configuration, updates, and
troubleshooting belong to the
[`nmea_sproxy` operator guide](nmea_sproxy/README.md).

### 🔐 What UDPSEC protects

UDPSEC is AISMixer's project-specific authenticated and encrypted UDP
transport, not an external standard.

Configured long-term P-256 identities authenticate the station and mixer.
A signed ephemeral P-256 ECDHE handshake derives fresh, separate
client-to-server and server-to-client AES-256-GCM traffic keys. Encrypted
key-possession confirmation completes activation.

After confirmation:

- NMEA DATA is authenticated and encrypted;
- ping/pong liveness traffic is authenticated and encrypted;
- graceful close is authenticated, encrypted, and best effort;
- unresolved liveness and planned session refresh start a fresh signed
  handshake with new directional keys;
- same-peer sessions on separate physical secure listeners are isolated from
  one another.

Session refresh is a new authenticated handshake, not an unauthenticated reset
or an in-session plaintext key update.

### 🔢 Replay, recovery, and NAT

The receiver retains every admitted DATA nonce for its usable directional
traffic-key epoch. DATA nonce records have no independent TTL and no live-entry
eviction. If a distinct valid nonce reaches the hard epoch bound, that exact
epoch fails closed and recovery requires a fresh authenticated handshake.
Exact admission and state-transition rules remain in the
[behavioural contract](BEHAVIORAL_CONTRACT.md).

Secure session and replay state is process-local, in-memory, and non-durable.
A process restart requires fresh sessions.

UDPSEC operates through NAT or CGNAT while the server-observed UDP source
address and port mapping remains stable. Rebinding, mobility, or another source
address/port change requires a fresh handshake. An established session is not
automatically migrated to the new locator.

Recovery uses the signed handshake. There is no plaintext `NOSESSION` or reset,
no downgrade message, and no automatic fallback to plain UDP. Unknown old
session DATA is not accepted as a recovery signal.

### ⚠️ Security boundary and limitations

UDP remains lossy. UDPSEC adds no delivery acknowledgement, payload buffering,
or payload replay during recovery. Best-effort close can also be lost.

UDPSEC authenticates configured endpoints and protects transport contents. It
does not establish the semantic truth, physical origin, or accuracy of an AIS
report. Forward-secrecy properties depend on ephemeral secrets being discarded
and endpoints not being compromised while those secrets are live.

Explicit plain UDP receives none of UDPSEC's cryptographic or liveness
properties. Use network isolation, application ACLs, and firewall policy where
plain transport is deliberately enabled.

See the [security policy](SECURITY.md), [behavioural contract](BEHAVIORAL_CONTRACT.md),
and [`nmea_sproxy` guide](nmea_sproxy/README.md) for the authoritative security
and provisioning detail.

## 🧰 Operations and observability

### 🎛️ Enable local control

On conventional/source-systemd configuration, the Unix-domain control service
is opt-in:

```yaml
control:
  unix:
    enabled: true
    socket_path: /run/aismixer/control.sock
    socket_mode: "0660"
```

The installed systemd unit provisions `/run/aismixer` while running. The
packaged OpenWrt configuration currently enables control by default.

Filesystem owner, group, and mode on the Unix socket are the access-control
boundary. There is no additional application-level authentication token. The
interface requires POSIX Unix-domain socket support.

### 📊 Routing status and runtime statistics

With the default root-owned socket:

```text
sudo aismixerctl
aismixerctl> status
aismixerctl> show statistics
aismixerctl> show statistics inputs
aismixerctl> show statistics outputs
```

`status` reports routing generation, enablement, zones, routes, and targets; it
is not systemd/procd service health. Statistics are fresh process-local
snapshots, with aggregate and currently supported per-input/per-output views.

Run an unfiltered statistics view first to discover filter values. An input
filter is the exact displayed runtime input name. An output filter is an exact
canonical name such as `udp:local_display` or a displayed decimal
process-local target number. A filter with no match returns an empty view.

Use `systemctl status aismixer` or `/etc/init.d/aismixer status` for service
health on the corresponding deployment.

Use `help` in the interactive shell. Equivalent one-shot commands are
available for scripts.

### 🎛️ Replace or disable runtime routing

```bash
sudo aismixerctl replace \
  --file /etc/aismixer/routing-update.yaml \
  --expected-generation 3
sudo aismixerctl disable --expected-generation 4
```

The file shown here is a direct routing section; the CLI also accepts a full
mapping containing `routing:`. Target IDs must already exist in the running
process. The generation numbers are illustrative: use the current value from
`status`. The guard is optional, and the CLI does not retry stale updates.

The repository-checkout example is
[`examples/routing-update.yaml`](examples/routing-update.yaml).

## ⚠️ Current limitations

- UDP is the mixer's only egress adapter and remains an unreliable datagram
  transport.
- Runtime routing state, generation numbers, and statistics are process-local;
  live routing changes are not persisted.
- Secure sessions and replay records are process-local and non-durable. DATA
  nonces remain for their traffic-key epoch rather than expiring on a nonce TTL.
- Sessions do not migrate automatically after a peer address or port change.
- AISMixer does not buffer and replay NMEA payloads during UDPSEC recovery.
- Local control currently uses a POSIX Unix-domain socket and filesystem
  permissions; it has no application token.
- The service does not provide geographic or MMSI content filtering, long-term
  storage, analytics, or AIS spoof/anomaly detection.
- The current processing runtime is Python and process-local; there is no
  separate native processor, worker coordinator, IPC routing plane, or
  cross-process statistics aggregation.
- Configuration is not generally hot-reloaded. The supported live mutation is
  the process-local routing snapshot exposed through local control.

## 📚 Examples and documentation

All examples require operator adaptation. They are not loaded automatically.

- [Examples guide](examples/README.md)
- [Static routing configuration](examples/config-routing.yaml)
- [Routing with local control](examples/config-routing-control.yaml)
- [Runtime routing update](examples/routing-update.yaml)
- [`nmea_sproxy` operator guide](nmea_sproxy/README.md)
- [Behavioural contract](BEHAVIORAL_CONTRACT.md)
- [Security policy](SECURITY.md)
- [Changelog](CHANGELOG.md)
- [License](LICENSE)
- [Contributing guide](CONTRIBUTING.md)
- [Roadmap](ROADMAP.md)
- [GitHub Wiki](https://github.com/iliyan85/aismixer/wiki)
- [Public website](https://aismixer.net)

[Back to language selector](#languages)

---

<a id="bulgarian"></a>

# 🇧🇬 AISMixer — обработка и маршрутизация на AIS NMEA 0183 потоци

**Нормализиране · Дедупликация · TAG метаданни · Маршрутизация · Препращане**

Изходният код на AISMixer е публично достъпен при условията на
[CC BY-NC 4.0](LICENSE). Лицензът на хранилището разрешава използване при
спазване на условията му, включително ограничението за нетърговска употреба.

## 🧭 Какво прави AISMixer

AISMixer приема AIS NMEA 0183 данни от множество приемници, извлича
поддържаните изречения `!AIVDM` и `!AIVDO`, сглобява многосъставни съобщения,
премахва дубликати в почти реално време, управлява NMEA 4.0 TAG метаданни и
изпраща чисти логически изходни потоци към конфигурираните UDP дестинации.

Текущите компоненти, предназначени за оператори, са:

- `aismixer` — дългосрочно работещата услуга за смесване, маршрутизация и
  обработка в слоя за данни;
- `aismixerctl` — локалният CLI за управление на маршрутизацията и статистиката
  по време на работа;
- `nmea_sproxy` — проксито при станцията от UDP/сериен вход към UDPSEC или plain-UDP изход.

Основните възможности включват:

- UDP вход през IPv4 и IPv6;
- удостоверен и криптиран UDPSEC вход;
- приемане от сериен или USB виртуален сериен интерфейс чрез `nmea_sproxy`;
- сглобяване на многосъставни AIS съобщения и дедупликация, атомарна за цялата група;
- контролирано управление на TAG стойностите `s`, `c` и `g`;
- глобално разпращане или логическа маршрутизация към именувани UDP цели;
- незадължителни списъци с разрешени входни адреси и задаване на изходен адрес;
- ограничени опашки, ограничаване на подаването при запълване (backpressure) и
  локална за процеса оперативна статистика.

### ⚙️ Модел на обработка

Всеки входен дейтаграм се сканира за поддържани AIS NMEA изречения.
Фрагментите на многосъставно съобщение могат да пристигат в произволен ред;
точните повторения са идемпотентни, а противоречащ фрагмент анулира активната
група. Завършените многосъставни съобщения се дедупликират и извеждат като
една група, така че дестинацията да не получи частичен дубликат.

В legacy режим потискането на дубликати е глобално. В режим с маршрутизация
то е отделно за всяка цел, така че едно и също логическо AIS съобщение може
закономерно да достигне по веднъж до две различни дестинации. Ограничените
входни, обработващи и изходни опашки прилагат backpressure, вместо да
допускат неограничен растеж на паметта.

Изграждането на TAG метаданни, сглобяването на многосъставни съобщения,
дедупликацията и маршрутизацията работят в един подреден конвейер за
обработка. Точните правила за конфликти, изчакване, капацитет, нулиране и
моментни конфигурации са описани в Поведенческия договор.

```text
AIS приемници през UDP ────┐
                           │
сериен/UDP приемник        v
        │              +-----------+      +-----------------+
        └─ nmea_sproxy → | aismixer | ───→ | UDP дестинации |
           UDPSEC/UDP    +-----------+      +-----------------+
                               ^
                               |
                         aismixerctl
                  незадължително локално управление
```

[Поведенческият договор](BEHAVIORAL_CONTRACT.md) определя точната и тествана
семантика на обработката, маршрутизацията, поведението по време на работа и UDPSEC.
Настоящият README е обзор на проекта и ръководство за оператора.

## 🚀 Бърз старт и жизнен цикъл

Изберете стандартен Linux със systemd или версионираните OpenWrt APK пакети с procd.

### 📦 Стандартен Linux със systemd

Скриптовете за жизнения цикъл работят директно като root или използват `sudo`
за друг администратор; ако няма нито едното, спират с обяснение. Примерите
по-долу използват `sudo`. Като root го пропуснете и редактирайте защитените
файлове с предпочитания административен редактор.

#### 📦 Инсталиране

На Debian или Raspberry Pi OS система със systemd:

```bash
git clone https://github.com/iliyan85/aismixer
cd aismixer
./install.sh
```

Инсталаторът поставя файловете на приложението в `/opt/aismixer`, инсталира
`/usr/local/bin/aismixerctl`, създава начални версии само на липсващите
файлове в `/etc/aismixer`, запазва съществуващата конфигурация и ключове и
включва услугата за стартиране при зареждане. Той умишлено **не стартира** услугата.

#### ⚙️ Конфигуриране преди първото стартиране

Първо прегледайте инсталираната конфигурация и правилата за доверие и мрежов достъп:

```bash
sudoedit /etc/aismixer/config.yaml
sudoedit /etc/aismixer/authorized_keys.yaml
```

Началната конфигурация съдържа plain-UDP listener-и, свързани към широк кръг
адреси и без приложни списъци с разрешени източници. Преди стартиране
адаптирайте адресите, портовете, правилата `allow_from`, UDP целите,
UDPSEC удостоверяването, правилата на защитната стена на хоста и политиката
за маршрутизация към конкретното разгръщане.

Plain UDP не предоставя UDPSEC поверителност, удостоверяване, криптографска
цялост, защита от replay или проверки за активност. Приложните списъци с
разрешени адреси допълват, но не заменят защитната стена на хоста.

#### 🚀 Стартиране, проверка и следене на логовете

```bash
sudo systemctl start aismixer
sudo systemctl status aismixer
sudo journalctl -u aismixer -f
```

Инсталираният unit вече включва стартирането при boot. Ако впоследствие то е
изключено, изпълнете `sudo systemctl enable aismixer`.

#### 📦 Обновяване

От работното копие на хранилището:

```bash
git pull --ff-only
./update.sh
systemctl status aismixer
```

`update.sh` обновява инсталираните файлове на приложението, unit-а и `aismixerctl`,
презарежда systemd и изпълнява `systemctl restart aismixer`. Рестартирането
също стартира неактивна услуга; скриптът за обновяване не запазва състояние
на умишлено спряна услуга. Операторската конфигурация и ключовете в
`/etc/aismixer` не се променят пряко.

#### 📦 Деинсталиране

Обикновеното деинсталиране премахва инсталираните файлове на приложението,
service unit-а и CLI, но запазва конфигурацията и ключовете:

```bash
./uninstall.sh
```

Следващата форма е разрушителна: тя премахва и `/etc/aismixer`, включително
операторската конфигурация и ключовия материал.

```bash
./uninstall.sh --purge-config
```

### 📦 OpenWrt 25.12

AISMixer предоставя версионирани OpenWrt 25.12 APK пакети с procd интеграция.
Една и съща рецепта за пакетиране създава:

- `aismixer-common` — споделени Python модули, инсталирани като зависимост;
- `aismixer` — mixer/router, UDPSEC сървър и `aismixerctl`;
- `nmea_sproxy` — UDP/serial прокси при станцията.

Python/shell съдържанието декларира `PKGARCH:=all`, защото не зависи от
архитектурата. Преносимостта все пак зависи от специфичните за целевата
платформа Python, криптографски, serial и други runtime пакети. Архитектурите
на repository target-ите, за които в момента има изградени, публикувани и
валидирани пакети, са `x86_64` и `mips_24kc`; този списък не означава, че
изходният код умишлено изключва други OpenWrt платформи с подходящи зависимости.

| OpenWrt feed target | Индекс на подписаното хранилище |
| --- | --- |
| `x86_64` | [`packages.adb`](https://aismixer.net/openwrt/25.12/x86_64/packages.adb) |
| `mips_24kc` | [`packages.adb`](https://aismixer.net/openwrt/25.12/mips_24kc/packages.adb) |

Това са пътища за отделните feed target-и, а не различни рецепти за
пакетиране. Локалната рецепта фиксира конкретна версионирана ревизия на
изходния код, затова не трябва да се приема, че публикуваният пакет съдържа
всяка по-късна промяна в `main`. Секцията за UDPSEC по-долу описва текущото
дърво на изходния код; операторите трябва да проверят ревизията на пакета и
[списъка на промените](CHANGELOG.md), преди да приемат, че по-късното укрепване
на сигурността присъства.

Преди инсталиране проверете свободното записваемо място в overlay и установете
firewall или мрежова изолация. Генерираните hook скриптове на OpenWrt пакета
включват и стартират услугата по време на `apk add`; началната пакетна
конфигурация съдържа широко достъпни plain-UDP listener-и. Инсталирайте като
root, след което незабавно спрете услугата и прегледайте конфигурацията и
правилата за разрешаване, преди да я въведете в експлоатация:

```sh
apk -U add aismixer
/etc/init.d/aismixer stop
vi /etc/aismixer/config.yaml
vi /etc/aismixer/authorized_keys.yaml
/etc/init.d/aismixer start
/etc/init.d/aismixer status
logread -e aismixer
```

Първото автоматично стартиране може да предхожда това спиране, затова
приложете защитната стена или изолационната политика преди `apk add`. Python
и зависимостите му изискват значително повече записваемо място от минимален
образ на рутера; extroot може да е подходящ при ограничено overlay пространство.

Обновете или премахнете mixer пакета чрез конфигурирания на устройството APK feed:

```sh
apk --update-cache add --upgrade aismixer
```

Hook скриптът за обновяване спира и стартира услугата дори ако е била спряна,
като запазва състоянието ѝ за включване при зареждане. Премахнете пакета с:

```sh
apk del aismixer
```

Премахването спира и изключва услугата. Пакетът не определя специфично за
проекта поведение за пълно изчистване, затова този README не обещава запазване
на конфигурацията или ключовете след `apk del`.

Инсталирайте `nmea_sproxy` вместо или заедно с mixer-а, когато рутерът е
крайната точка при станцията:

```sh
apk -U add nmea_sproxy
```

Hook скриптът на пакета също опитва да стартира услугата, но нова UDPSEC връзка
няма доверен публичен ключ на mixer-а и обикновено не преминава предварителната
проверка. Осигурете доверието и рестартирайте според ръководството за компонента;
само инсталирането не създава готова връзка.

Началните настройки при двата начина на разгръщане се различават:

- стандартната конфигурация от изходния код със systemd оставя локалното
  управление изключено до изрично включване и подготвя идентичност на сървъра
  само когато активен защитен вход я изисква;
- пакетираната OpenWrt конфигурация включва локалното управление, а init
  услугата ѝ подготвя или поправя идентичността на сървъра преди стартиране.

Преглеждайте инсталираната конфигурация, вместо да приемате, че началните
настройки на единия начин за разгръщане важат и за другия. Вижте
[ръководството за разгръщане в OpenWrt](https://github.com/iliyan85/aismixer/wiki/OpenWrt-Deployment)
и [ръководството за `nmea_sproxy`](nmea_sproxy/README.md) за подробности за
пакетите, инстанциите, мястото за съхранение, серийните устройства и
отстраняването на проблеми.

## ⚙️ Конфигурация и мрежов модел

Инсталираният mixer чете `/etc/aismixer/config.yaml`. Следващият минимален
пример използва един ограничен plain-UDP вход и една UDP дестинация:

```yaml
station_id: mixstation_1

udp_inputs:
  - id: roof_receiver
    listen_ip: "0.0.0.0"
    listen_port: 17777
    allow_from:
      - 192.0.2.0/24

forwarders:
  - id: local_display
    host: 127.0.0.1
    port: 19000
```

Адаптирайте всички примерни адреси, портове, идентификатори, пътища и правила
преди употреба. Примерите не се зареждат автоматично, докато не бъдат копирани или адаптирани.

### 📡 Входове и UDP цели

- `udp_inputs` приема plain UDP. Полето `id` дава на входа стабилна вътрешна
  идентичност за маршрутизация.
- `sec_inputs` приема удостоверен UDPSEC и извежда идентичността за
  маршрутизация от удостоверената станция.
- `forwarders` определя UDP дестинациите. Каноничната идентичност на именувана UDP цел е `udp:<id>`.
- `listen_ip` избира едно адресно семейство. Използвайте отделни listener
  записи, когато са необходими едновременно изрични IPv4 и IPv6 входове.
- `allow_from` приема буквални IP адреси и CIDR мрежи. Ако липсва, не се прилага
  приложен ACL; изрично празен списък отказва всички пакети към listener-а.
- `source_ip` по желание свързва изходния UDP socket на UDP целта към конкретен
  локален адрес.

IP адресите на източниците и UDP alias-ите са оперативни идентификатори, а не
криптографски идентичности на станции.

Когато маршрутизацията е включена, всяка адресируема UDP цел трябва да има
уникално `id`. Цел без име остава валидна само при съвместимо разпращане към
всички изходи. Източници без съвпадащ маршрут не създават мрежов изход.

### 🪪 Сървърна идентичност за UDPSEC

При стандартно разгръщане от изходния код със systemd подготовката на
идентичността на сървъра следва активната конфигурация `sec_inputs`.
Конфигурация само с plain UDP не създава двойка сървърни ключове. При активен
защитен вход изцяло липсваща двойка може да бъде създадена, валидна съвпадаща
двойка се запазва, а частичен, невалиден или несъвпадащ материал води до
fail-closed отказ без неявна подмяна.

Поправянето на публичния ключ е изрично действие на оператора:

```bash
sudo python3 /opt/aismixer/tools/aismixer_keys.py server --repair-public
```

OpenWrt се различава: неговата init услуга подготвя или поправя идентичността
на сървъра преди стартиране на пакетираната услуга. Нито един от двата начина
за разгръщане не доставя частен ключ.

## 🗺️ Маршрутизация, зони и TAG метаданни

### 🔀 Съвместимо разпращане към всички изходи

Когато `routing` на най-горното ниво липсва или е null, дедупликацията е
глобална и всяко
прието изходно изречение се изпраща към всеки конфигуриран UDP forwarder.
В този режим за съвместимост forwarder-ите не се нуждаят от `id`.

### 🗺️ Статична маршрутизация

Включената `routing:` конфигурация съдържа и `zones`, и `routes`.
Именуваните маршрути избират подмножества от цели, а дедупликацията е отделна
за всяка цел. Зоните са логически множества от вътрешни идентификатори на
източници — не географски области, MMSI списъци, филтри за съдържание на
кораби или изведени TAG стойности.

Маршрутите се оценяват по реда им в конфигурацията. Когато припокриващи се
маршрути изберат една и съща цел, тя се запазва само веднъж за съобщението;
избирането на две различни цели може да доведе до по едно изпращане към всяка.

Зоните поддържат:

- `include` за изрично зададени вътрешни идентичности на източници;
- `union` за членовете на именувани зони;
- `intersection` за членовете, общи за именувани зони;
- `difference` за членовете на първата именувана зона без тези от втората.

Маршрутите приемат `from_zone`. Те не приемат директно произволен
идентификатор на източник; за да маршрутизирате един източник, поставете
идентичността му в зона чрез `include` и маршрутизирайте от тази зона:

```yaml
routing:
  zones:
    roof_only:
      include:
        - udp:roof_receiver
  routes:
    - name: roof_to_display
      from_zone: roof_only
      to:
        - udp:local_display
```

Типичните вътрешни идентичности включват `udp:<input-id>`,
`udp:<mapped-alias>`, `udp:<remote-ip>` и `udpsec:<authenticated-station-id>`.
Маршрутизацията съпоставя тези вътрешни стойности; изведената TAG стойност `s`
е отделна.

Вижте [примера за статична маршрутизация](examples/config-routing.yaml) за пълна
конфигурация с именувани входове, UDP цели, зони, маршрути и операции с множества.

### 🎛️ Маршрутизация по време на работа

Когато локалното управление е включено, `aismixerctl replace` и
`aismixerctl disable` атомарно променят локалната за процеса моментна
конфигурация на маршрутизацията. Те не пренаписват YAML. След рестартиране се
възстановява конфигурацията, заредена от диска.

Незадължително очаквано поколение предпазва от презаписване на по-ново
състояние от остарял оператор или автоматизиран процес. Точната семантика на
приемането за обработка и моментните конфигурации принадлежи на Поведенческия
договор.

### 🏷️ Обзор на NMEA TAG метаданните

AISMixer чете входните TAG метаданни и извежда контролирани стойности `s`, `c`
и `g`:

- `s` определя конфигурирания изходен етикет за източник и се пречиства за
  NMEA; това не е вътрешната идентичност за маршрутизация;
- `c` може да запази валиден входен timestamp или да използва времето на
  сървъра според конфигурацията;
- `g` свързва многосъставния изход и може да запази договорен входен group ID
  или да използва генериран изходен ID.

TAG `g` е метаданна, а не ключът на multipart assembler-а. Точните правила за
приоритет, собственост на многосъставните съобщения, конфликти, изтичане и
съвместимост са нормативно определени в Поведенческия договор.

## 🔐 UDPSEC и `nmea_sproxy`

### 🧩 Прокси при станцията

`nmea_sproxy` представя по една връзка за всеки процес или service instance:

- един локален вход: UDP или предоставено от операционната система serial/USB
  виртуално серийно устройство;
- един мрежов изход: UDPSEC или изрично конфигуриран plain UDP.

UDPSEC е защитеният и подразбиращ се изходен режим, когато е приложим.
Plain UDP трябва да бъде избран изрично и няма удостоверяване или криптиране.
UDPSEC изисква идентичност на станцията, доверения публичен ключ на mixer-а и
разрешаване на публичния ключ на станцията от mixer-а.

Налични са стандартно разгръщане под Linux и пакети за OpenWrt. Подробните
инструкции за инсталиране, ключове, доверие, service instance-и, серийна конфигурация,
обновяване и отстраняване на проблеми принадлежат на
[операторското ръководство за `nmea_sproxy`](nmea_sproxy/README.md).

### 🔐 Какво защитава UDPSEC

UDPSEC е специфичният за AISMixer удостоверен и криптиран UDP транспорт, а не
външен стандарт.

Конфигурираните дългосрочни P-256 идентичности удостоверяват станцията и
mixer-а. Подписана процедура за установяване на сесия чрез ефимерен P-256 ECDHE
обмен извежда нови, отделни AES-256-GCM ключове за трафика клиент→сървър и
сървър→клиент. Криптирано доказване на притежанието им завършва активирането.

След потвърждението:

- NMEA DATA трафикът е удостоверен и криптиран;
- ping/pong трафикът за проверка на активността е удостоверен и криптиран;
- контролираното затваряне е удостоверено, криптирано и без гаранция за доставка;
- нерешена проверка за активност или планирано опресняване на сесията стартира
  нов подписан handshake с нови отделни ключове за двете посоки;
- сесиите за един и същ peer на отделни физически UDPSEC listener-и са
  изолирани една от друга.

Опресняването на сесията е нов удостоверен handshake, а не неудостоверено
нулиране или некриптирано обновяване на ключовете вътре в сесията.

### 🔢 Защита от replay, възстановяване и NAT

Получателят запазва всеки приет DATA nonce за използваемата епоха на
еднопосочния ключ за трафик. DATA nonce записите нямат собствен TTL и не се
изхвърлят, докато съответната ключова епоха е използваема. Ако отделен валиден
nonce достигне твърдия лимит, точно тази епоха отказва в режим fail closed и
възстановяването изисква нов удостоверен handshake. Точните правила остават в
[Поведенческия договор](BEHAVIORAL_CONTRACT.md).

Защитеното сесийно и replay състояние е локално за процеса, намира се в
паметта и не е трайно. Рестартирането на процеса изисква нови сесии.

UDPSEC работи през NAT или CGNAT, докато наблюдаваната от сървъра UDP
адресно-портова двойка остава стабилна. Rebinding, преместване или друга
промяна изисква нов handshake; установената сесия не се мигрира автоматично.

Възстановяването използва подписания handshake. Няма plaintext `NOSESSION`
или reset, няма downgrade съобщение и няма автоматичен fallback към plain UDP.
DATA от неизвестна стара сесия не се приема като сигнал за възстановяване.

### ⚠️ Обхват и ограничения на защитата

UDP остава ненадежден транспорт. UDPSEC не добавя потвърждение за доставка,
буфериране или повторно изпращане на полезните данни при възстановяване.
Контролираното затваряне без гаранция за доставка също може да бъде изгубено.

UDPSEC удостоверява конфигурираните крайни точки и защитава съдържанието при
пренос. Той не установява семантичната достоверност, физическия произход или
точността на AIS съобщението. Свойствата за forward secrecy зависят от
унищожаването на ефимерните тайни и от това крайните точки да не бъдат
компрометирани, докато тези тайни са активни.

Изрично конфигурираният plain UDP не получава криптографските свойства или
проверките за активност на UDPSEC. Използвайте мрежова изолация, приложни ACL
правила и защитна стена там, където plain транспортът е разрешен умишлено.

Вижте [политиката за сигурност](SECURITY.md),
[Поведенческия договор](BEHAVIORAL_CONTRACT.md) и
[ръководството за `nmea_sproxy`](nmea_sproxy/README.md) за авторитетните
подробности относно сигурността и осигуряването на доверие.

## 🧰 Експлоатация и наблюдение

### 🎛️ Включване на локалното управление

При стандартната конфигурация от изходния код със systemd услугата за
управление през Unix-domain socket е изключена до изрично включване:

```yaml
control:
  unix:
    enabled: true
    socket_path: /run/aismixer/control.sock
    socket_mode: "0660"
```

Инсталираният systemd unit създава `/run/aismixer`, докато работи.
Пакетираната OpenWrt конфигурация в момента включва управлението по
подразбиране.

Собственикът, групата и режимът на Unix socket файла са границата за контрол
на достъпа. Няма допълнителен application-level token за удостоверяване.
Интерфейсът изисква поддръжка на POSIX Unix-domain socket-и.

### 📊 Състояние на маршрутизацията и статистика по време на работа

При подразбиращия се socket, собственост на root:

```text
sudo aismixerctl
aismixerctl> status
aismixerctl> show statistics
aismixerctl> show statistics inputs
aismixerctl> show statistics outputs
```

`status` показва поколението на маршрутизацията, дали тя е включена, зоните,
маршрутите и целите; това не е състоянието на услугата в systemd/procd.
Статистиките са моментни справки, извлечени при заявката и валидни само за
текущия процес, с общи и поддържаните в момента изгледи за вход и изход.

Първо изпълнете нефилтриран изглед на статистиката, за да откриете стойностите
за филтриране. Входният филтър е точното показано име на входа по време на
работа. Изходният филтър е точно канонично име като `udp:local_display` или
показан десетичен номер на целта, валиден само за процеса. Филтър без
съвпадение връща празен изглед.

Използвайте `systemctl status aismixer` или `/etc/init.d/aismixer status` за
състоянието на услугата при съответния начин за разгръщане.

Използвайте `help` в интерактивния shell. За скриптове са налични
еквивалентни еднократни команди.

### 🎛️ Замяна или изключване на маршрутизацията по време на работа

```bash
sudo aismixerctl replace \
  --file /etc/aismixer/routing-update.yaml \
  --expected-generation 3
sudo aismixerctl disable --expected-generation 4
```

Показаният файл е директна секция за маршрутизация; CLI приема и пълна
конфигурация, съдържаща `routing:`. Идентификаторите на целите трябва вече да
съществуват в работещия процес. Номерата на поколенията са примерни: използвайте
текущата стойност от `status`. Guard-ът е незадължителен и CLI не повтаря
автоматично остарели актуализации.

Примерът в работното копие на хранилището е
[`examples/routing-update.yaml`](examples/routing-update.yaml).

## ⚠️ Текущи ограничения

- UDP е единственият изходен адаптер на mixer-а и остава ненадежден дейтаграмен
  транспорт.
- Състоянието на маршрутизацията по време на работа, номерата на поколенията и
  статистиките са локални за процеса; текущите промени на маршрутизацията не
  се записват трайно.
- Защитените сесии и replay записите са локални за процеса и нетрайни. DATA
  nonce стойностите остават за епохата на ключа си за трафик и нямат
  независим TTL.
- Сесиите не се мигрират автоматично след промяна на адреса или порта на peer-а.
- AISMixer не буферира и не изпраща повторно NMEA payload-и при UDPSEC
  възстановяване.
- Локалното управление в момента използва POSIX Unix-domain socket и
  разрешенията на файловата система; няма application token.
- Услугата не предоставя географско или MMSI филтриране на съдържанието,
  дългосрочно съхранение, анализи или откриване на AIS spoof/anomaly.
- Текущата обработка е на Python и е локална за процеса; няма отделен native
  процесор, coordinator за worker-и, IPC routing plane или
  агрегиране на статистика между процеси.
- Конфигурацията по принцип не се презарежда в движение. Поддържаната промяна
  по време на работа е локалната за процеса моментна конфигурация на
  маршрутизацията, достъпна през локалното управление.

## 📚 Примери и документация

Всички примери изискват адаптация от оператора. Те не се зареждат автоматично.

- [Ръководство за примерите](examples/README.md)
- [Конфигурация за статична маршрутизация](examples/config-routing.yaml)
- [Маршрутизация с локално управление](examples/config-routing-control.yaml)
- [Runtime актуализация на маршрутизацията](examples/routing-update.yaml)
- [Операторско ръководство за `nmea_sproxy`](nmea_sproxy/README.md)
- [Поведенчески договор](BEHAVIORAL_CONTRACT.md)
- [Политика за сигурност](SECURITY.md)
- [Списък на промените](CHANGELOG.md)
- [Лиценз](LICENSE)
- [Ръководство за принос](CONTRIBUTING.md)
- [Пътна карта](ROADMAP.md)
- [GitHub Wiki](https://github.com/iliyan85/aismixer/wiki)
- [Публичен уебсайт](https://aismixer.net)

[Към избора на език](#languages)

---

<a id="romanian"></a>

# 🇷🇴 AISMixer — procesarea și rutarea fluxurilor AIS NMEA 0183

**Normalizare · Deduplicare · Etichetare · Rutare · Redirecționare**

Codul-sursă AISMixer este disponibil public sub licența [CC BY-NC 4.0](LICENSE).
Licența depozitului permite utilizarea în condițiile sale, inclusiv restricția
privind utilizarea necomercială.

## 🧭 Ce face AISMixer

AISMixer primește date AIS NMEA 0183 de la mai multe receptoare, extrage
propozițiile acceptate `!AIVDM` și `!AIVDO`, reasamblează mesajele multipart,
elimină duplicatele aproape în timp real, gestionează metadatele NMEA 4.0 TAG și
trimite fluxuri logice curate către destinațiile UDP configurate.

Componentele actuale destinate operatorilor sunt:

- `aismixer` — serviciul de lungă durată pentru mixare, rutare și planul de date;
- `aismixerctl` — CLI-ul local pentru controlul rutării și statisticile runtime;
- `nmea_sproxy` — proxy-ul de la stație, de la UDP/serial la UDPSEC sau UDP simplu.

Capabilitățile principale includ:

- intrare UDP prin IPv4 și IPv6;
- intrare UDPSEC autentificată și criptată;
- recepție serială și prin porturi seriale virtuale USB cu `nmea_sproxy`;
- asamblare AIS multipart și deduplicare atomică la nivel de grup;
- gestionare controlată a câmpurilor TAG `s`, `c` și `g`;
- distribuire globală sau rutare logică spre destinații UDP denumite;
- liste opționale de adrese permise la intrare și asocierea adresei-sursă la ieșire;
- cozi limitate, backpressure și statistici operaționale locale procesului.

### ⚙️ Modelul de procesare

Fiecare datagramă de intrare este scanată pentru propoziții AIS NMEA acceptate.
Fragmentele multipart pot sosi în orice ordine; repetările exacte sunt
idempotente, iar un fragment contradictoriu invalidează grupul activ. Mesajele
multipart finalizate sunt deduplicate și emise ca un singur grup, astfel încât o
destinație să nu primească un duplicat parțial.

În modul legacy, suprimarea duplicatelor este globală. În modul de rutare, ea
este separată pentru fiecare destinație, astfel încât același mesaj AIS logic
poate ajunge în mod legitim o dată la două destinații distincte. Cozile limitate
pentru intrare, procesare și ieșire aplică backpressure în loc să permită
creșterea nelimitată a memoriei.

Construirea TAG-urilor, asamblarea multipart, deduplicarea și rutarea rulează
într-un singur pipeline de procesare ordonat. Regulile exacte pentru conflicte,
timeout, capacitate, resetare și snapshot sunt documentate în contractul
comportamental.

```text
Receptoare AIS prin UDP ──────┐
                              │
receptor serial/UDP           v
        │                +-----------+      +------------------+
        └─ nmea_sproxy →  | aismixer | ───→ | Destinații UDP   |
           UDPSEC/UDP     +-----------+      +------------------+
                                ^
                                |
                          aismixerctl
                     control local opțional
```

[Contractul comportamental](BEHAVIORAL_CONTRACT.md) stabilește semantica exactă
și testată pentru procesare, rutare, runtime și UDPSEC. Acest README este
prezentarea generală a proiectului și ghidul de orientare pentru operatori.

## 🚀 Pornire rapidă și ciclu de viață

Alegeți Linux convențional cu systemd sau pachetele APK OpenWrt versionate,
integrate cu procd.

### 📦 Linux convențional cu systemd

Scripturile ciclului de viață rulează direct ca root sau folosesc `sudo` pentru
alt administrator; dacă niciuna dintre variante nu este disponibilă, se opresc
cu o explicație. Exemplele de mai jos folosesc `sudo`. Când lucrați ca root,
omiteți-l și editați fișierele privilegiate cu editorul administratorului.

#### 📦 Instalare

Pe o gazdă Debian sau Raspberry Pi OS bazată pe systemd:

```bash
git clone https://github.com/iliyan85/aismixer
cd aismixer
./install.sh
```

Instalatorul plasează runtime-ul în `/opt/aismixer`, instalează
`/usr/local/bin/aismixerctl`, creează numai fișierele lipsă din
`/etc/aismixer`, păstrează configurația și cheile existente și activează
serviciul pentru pornirea la boot. În mod intenționat, **nu** pornește serviciul.

#### ⚙️ Configurare înainte de prima pornire

Verificați mai întâi configurația instalată, încrederea și politica de rețea:

```bash
sudoedit /etc/aismixer/config.yaml
sudoedit /etc/aismixer/authorized_keys.yaml
```

Configurația inițială conține listenere UDP simple asociate unor adrese larg
accesibile și fără liste de adrese permise la nivelul aplicației. Înainte de
pornire, adaptați adresele, porturile, regulile `allow_from`, forwarderele,
autorizarea UDPSEC, regulile firewall ale gazdei și politica de rutare pentru
implementarea concretă.

UDP simplu nu oferă confidențialitatea, autentificarea, integritatea
criptografică, protecția anti-replay sau verificările de liveness ale UDPSEC.
Listele de adrese permise la nivelul aplicației completează, nu înlocuiesc,
firewall-ul gazdei.

#### 🚀 Pornire, verificare și urmărirea jurnalelor

```bash
sudo systemctl start aismixer
sudo systemctl status aismixer
sudo journalctl -u aismixer -f
```

Unitatea instalată are deja activată pornirea la boot. Dacă această activare a
fost schimbată ulterior, rulați `sudo systemctl enable aismixer`.

#### 📦 Actualizare

Din checkout:

```bash
git pull --ff-only
./update.sh
systemctl status aismixer
```

`update.sh` actualizează fișierele runtime instalate, unitatea și
`aismixerctl`, reîncarcă systemd și rulează `systemctl restart aismixer`.
O repornire pornește și un serviciu inactiv; updater-ul nu păstrează starea
intenționat oprită. Configurația operatorului și cheile din `/etc/aismixer` nu
sunt modificate direct.

#### 📦 Dezinstalare

Dezinstalarea normală elimină runtime-ul instalat, unitatea de serviciu și
CLI-ul, dar păstrează configurația și cheile:

```bash
./uninstall.sh
```

Forma următoare este distructivă: elimină și `/etc/aismixer`, inclusiv
configurația operatorului și materialul de cheie.

```bash
./uninstall.sh --purge-config
```

### 📦 OpenWrt 25.12

AISMixer oferă pachete APK versionate pentru OpenWrt 25.12, cu integrare
procd. Aceeași rețetă de pachet produce:

- `aismixer-common` — module Python comune, instalate ca dependență;
- `aismixer` — mixerul/routerul, serverul UDPSEC și `aismixerctl`;
- `nmea_sproxy` — proxy-ul UDP/serial de la stație.

Conținutul Python/shell declară `PKGARCH:=all`, deoarece este
independent de arhitectură. Portabilitatea depinde totuși de pachetele
specifice țintei pentru Python, criptografie, serial și alte componente runtime.
Arhitecturile depozitelor construite, publicate și validate în prezent
sunt `x86_64` și `mips_24kc`; lista nu înseamnă că sursa este proiectată să
excludă alte ținte OpenWrt care au dependențe adecvate.

| Țintă de feed OpenWrt | Index semnat al depozitului |
| --- | --- |
| `x86_64` | [`packages.adb`](https://aismixer.net/openwrt/25.12/x86_64/packages.adb) |
| `mips_24kc` | [`packages.adb`](https://aismixer.net/openwrt/25.12/mips_24kc/packages.adb) |

Acestea sunt căi ale țintelor de feed, nu rețete de pachet separate. Rețeta
locală fixează o revizie versionată a sursei, astfel încât nu trebuie presupus
că un pachet publicat conține toate modificările ulterioare din `main`.
Secțiunea UDPSEC de mai jos descrie arborele-sursă curent; operatorii pachetelor
trebuie să verifice revizia pachetului și
[lista de modificări](CHANGELOG.md) înainte de a presupune că măsurile ulterioare
de consolidare a securității sunt incluse.

Înainte de instalare, verificați spațiul disponibil pentru scriere în overlay și
aplicați firewall sau izolare de rețea. Hook-urile generate de OpenWrt pentru pachet
activează și pornesc serviciul în timpul `apk add`; configurația inclusă în
pachet conține inițial listenere UDP simple cu acces larg. Instalați ca root,
apoi opriți imediat serviciul și verificați configurația și autorizarea înainte
de a-l pune în funcțiune:

```sh
apk -U add aismixer
/etc/init.d/aismixer stop
vi /etc/aismixer/config.yaml
vi /etc/aismixer/authorized_keys.yaml
/etc/init.d/aismixer start
/etc/init.d/aismixer status
logread -e aismixer
```

Pornirea automată inițială poate avea loc înainte de oprire, așadar aplicați
firewall-ul sau politica de izolare înainte de `apk add`. Python și
dependențele sale necesită mult mai mult spațiu disponibil pentru scriere decât
o imagine minimală de router; extroot poate fi potrivit când spațiul intern din
overlay este limitat.

Actualizați sau eliminați pachetul mixerului folosind feed-ul APK configurat pe
dispozitiv:

```sh
apk --update-cache add --upgrade aismixer
```

Hook-ul de actualizare oprește și pornește serviciul chiar dacă acesta era oprit
anterior, păstrând însă starea sa de activare/dezactivare. Eliminați pachetul cu:

```sh
apk del aismixer
```

Eliminarea oprește și dezactivează serviciul. Pachetul nu are un contract de
purge specific proiectului, astfel încât acest README nu promite păstrarea
configurației sau a cheilor după `apk del`.

Instalați `nmea_sproxy` în locul mixerului sau împreună cu acesta atunci când
routerul este endpoint-ul de la stație:

```sh
apk -U add nmea_sproxy
```

Hook-ul pachetului său încearcă, de asemenea, să pornească serviciul, dar unei
relații UDPSEC noi îi lipsește cheia publică de încredere a mixerului și, în mod
normal, nu poate finaliza verificarea preliminară. Configurați încrederea și
reporniți serviciul urmând ghidul componentei; simpla instalare nu produce o
relație pregătită pentru utilizare.

Valorile implicite diferă între implementări:

- configurația convențională/sursă-systemd păstrează controlul local ca opțiune
  explicită și pregătește identitatea serverului numai când intrarea securizată
  activă o cere;
- configurația OpenWrt din pachet activează controlul local, iar serviciul său
  init pregătește sau repară anticipat identitatea serverului înainte de
  pornire.

Verificați configurația instalată, fără să presupuneți că valorile implicite
ale unei implementări se aplică și celeilalte. Consultați
[ghidul de implementare OpenWrt](https://github.com/iliyan85/aismixer/wiki/OpenWrt-Deployment)
și [ghidul `nmea_sproxy`](nmea_sproxy/README.md) pentru detalii despre pachete,
instanțe, stocare, conexiuni seriale și depanare.

## ⚙️ Configurație și model de rețea

Mixerul instalat citește `/etc/aismixer/config.yaml`. Acest exemplu minimal
folosește o intrare UDP simplă restricționată și o destinație UDP:

```yaml
station_id: mixstation_1

udp_inputs:
  - id: roof_receiver
    listen_ip: "0.0.0.0"
    listen_port: 17777
    allow_from:
      - 192.0.2.0/24

forwarders:
  - id: local_display
    host: 127.0.0.1
    port: 19000
```

Adaptați înainte de utilizare toate adresele, porturile, ID-urile, căile și
politicile din exemple. Exemplele din depozit sunt inactive până când sunt
copiate sau adaptate.

### 📡 Intrări și forwardere

- `udp_inputs` acceptă UDP simplu. Un `id` oferă intrării o identitate internă
  stabilă pentru rutare.
- `sec_inputs` acceptă UDPSEC autentificat și derivă identitatea de rutare din
  stația autentificată.
- `forwarders` definește destinațiile UDP. Identitatea canonică a unei
  destinații denumite este `udp:<id>`.
- `listen_ip` selectează o singură familie de adrese. Folosiți intrări listener
  separate atunci când sunt necesare explicit atât intrări IPv4, cât și IPv6.
- `allow_from` acceptă adrese IP literale și rețele CIDR. Omiterea sa nu aplică
  niciun ACL al aplicației; o listă goală explicită respinge toate pachetele pe
  listener-ul respectiv.
- `source_ip` asociază opțional socket-ul UDP de ieșire al unui forwarder cu o
  adresă locală literală.

Adresele IP sursă și aliasurile UDP sunt identificatori operaționali, nu
identități criptografice ale stațiilor.

Când rutarea este activată, fiecare forwarder adresabil trebuie să aibă un
`id` unic. Un forwarder fără nume rămâne valid numai pentru distribuirea
legacy. Sursele care nu corespund niciunei rute nu produc trafic de rețea în
modul de rutare.

### 🪪 Identitatea serverului UDPSEC

În implementarea convențională/sursă-systemd, pregătirea identității serverului
urmează configurația `sec_inputs` activă. O configurație numai cu UDP simplu
nu creează o pereche de server. Când intrarea securizată este activă, o pereche
complet absentă poate fi creată, o pereche validă și concordantă este păstrată,
iar materialul parțial, invalid sau neconcordant eșuează în mod sigur, fără
înlocuire implicită.

Repararea cheii publice este o acțiune explicită a operatorului:

```bash
sudo python3 /opt/aismixer/tools/aismixer_keys.py server --repair-public
```

OpenWrt se comportă diferit: serviciul său init pregătește sau repară anticipat
identitatea serverului înainte de lansarea serviciului din pachet. Niciuna dintre
implementări nu livrează o cheie privată.

## 🗺️ Rutare, zone și TAG-uri

### 🔀 Distribuire legacy către toate ieșirile

Când cheia top-level `routing` lipsește sau este null, deduplicarea este
globală și fiecare propoziție de ieșire acceptată este trimisă tuturor
forwarderelor UDP configurate. În acest mod de compatibilitate, forwarderele nu
au nevoie de ID-uri.

### 🗺️ Rutare statică

O mapare `routing:` activată conține atât `zones`, cât și `routes`. Rutele
denumite selectează subseturi de destinații, iar deduplicarea este separată
pentru fiecare destinație. Zonele sunt mulțimi logice de ID-uri interne ale
surselor, nu zone geografice, liste MMSI, filtre pentru nave sau etichete TAG
emise.

Rutele sunt evaluate în ordinea din configurație. Când rute suprapuse selectează
aceeași destinație, aceasta este reținută o singură dată pentru mesaj; selectarea
a două destinații distincte poate produce câte o trimitere către fiecare.

Zonele acceptă:

- `include` pentru identități interne explicite ale surselor;
- `union` pentru membrii zonelor denumite;
- `intersection` pentru membrii comuni ai zonelor denumite;
- `difference` pentru membrii primei zone denumite, cu excepția celor din a
  doua.

Rutele acceptă `from_zone`. Ele nu acceptă direct un ID de sursă arbitrar;
pentru a ruta o singură sursă, puneți identitatea într-o zonă cu `include` și
rutați din acea zonă:

```yaml
routing:
  zones:
    roof_only:
      include:
        - udp:roof_receiver
  routes:
    - name: roof_to_display
      from_zone: roof_only
      to:
        - udp:local_display
```

Identitățile interne uzuale includ `udp:<input-id>`,
`udp:<mapped-alias>`, `udp:<remote-ip>` și
`udpsec:<authenticated-station-id>`. Rutarea compară aceste valori interne;
valoarea TAG `s` emisă este separată.

Consultați [exemplul de rutare statică](examples/config-routing.yaml) pentru
intrări denumite, forwardere, zone, rute și operații pe mulțimi complete.

### 🎛️ Rutare runtime

Când controlul local este activat, `aismixerctl replace` și
`aismixerctl disable` schimbă atomic snapshot-ul de rutare local procesului.
Ele nu rescriu fișierele YAML. Repornirea restabilește configurația de rutare
încărcată de pe disc.

O generație așteptată opțională împiedică un operator sau un proces automatizat
cu stare învechită să suprascrie o stare runtime mai nouă. Semantica exactă a
admiterii pentru procesare și a snapshot-urilor aparține contractului
comportamental.

### 🏷️ Prezentare generală a metadatelor NMEA TAG

AISMixer citește metadatele TAG de intrare și emite valori controlate `s`, `c`
și `g`:

- `s` identifică eticheta configurată a sursei de ieșire și este sanitizată
  pentru NMEA; nu este identitatea internă de rutare;
- `c` poate păstra un timestamp valid de intrare sau poate folosi timpul
  serverului, în funcție de configurație;
- `g` leagă ieșirea multipart și poate păstra un ID de grup agreat la intrare
  sau poate folosi un ID de ieșire generat.

TAG `g` este metadată, nu cheia assemblerului multipart. Regulile exacte de
prioritate, proprietate multipart, conflict, expirare și compatibilitate sunt
normative în contractul comportamental.

## 🔐 UDPSEC și `nmea_sproxy`

### 🧩 Proxy-ul de la stație

`nmea_sproxy` reprezintă o relație pentru fiecare proces sau instanță de
serviciu:

- o intrare locală: UDP sau un dispozitiv serial/port serial virtual USB pus la
  dispoziție de sistemul de operare;
- o ieșire de rețea: UDPSEC sau UDP simplu configurat explicit.

UDPSEC este modelul de ieșire protejat/implicit acolo unde se aplică. UDP simplu
trebuie selectat explicit și nu este autentificat sau criptat. UDPSEC necesită o
identitate a stației, cheia publică de încredere a mixerului și autorizarea de
către mixer a cheii publice a stației.

Proiectul oferă o implementare convențională pe Linux și pachete pentru
OpenWrt. Instalarea detaliată, cheile, încrederea, instanțele de serviciu,
actualizările și depanarea aparțin
[ghidului operatorului `nmea_sproxy`](nmea_sproxy/README.md).

### 🔐 Ce protejează UDPSEC

UDPSEC este transportul UDP autentificat și criptat specific AISMixer, nu un
standard extern.

Identitățile P-256 pe termen lung configurate autentifică stația și mixerul. Un
handshake ECDHE P-256 efemer și semnat derivă chei de trafic AES-256-GCM
proaspete și separate pentru client-către-server și server-către-client.
Confirmarea criptată a posesiei cheii finalizează activarea.

După confirmare:

- mesajele NMEA de tip DATA sunt autentificate și criptate;
- traficul de liveness ping/pong este autentificat și criptat;
- închiderea grațioasă este autentificată, criptată și best-effort;
- liveness-ul nerezolvat și reîmprospătarea planificată a sesiunii pornesc un
  handshake semnat nou, cu chei direcționale noi;
- sesiunile aceluiași peer pe listenere UDPSEC fizice separate sunt izolate
  unele de altele.

Reîmprospătarea sesiunii este un nou handshake autentificat, nu o resetare
neautentificată sau o actualizare în clar a cheii în cadrul sesiunii.

### 🔢 Replay, recuperare și NAT

Receptorul păstrează fiecare nonce DATA admis pe întreaga epocă utilizabilă a
cheii de trafic direcționale. Înregistrările nonce DATA nu au TTL independent și
nu sunt eliminate prin evacuarea intrărilor active. Dacă un nonce valid distinct
atinge limita strictă a epocii, exact acea epocă este invalidată în mod sigur
(fail-closed), iar recuperarea necesită un handshake autentificat nou. Regulile exacte pentru
admitere și tranzițiile de stare rămân în
[contractul comportamental](BEHAVIORAL_CONTRACT.md).

Starea securizată a sesiunilor și a protecției anti-replay este locală
procesului, în memorie și nepersistentă. Repornirea procesului necesită sesiuni
noi.

UDPSEC funcționează prin NAT sau CGNAT atât timp cât adresa și portul sursă UDP
observate de server rămân stabile. Reasocierea, mobilitatea sau orice schimbare a
adresei/portului sursă necesită un handshake nou. O sesiune stabilită nu este
migrată automat la noul locator.

Recuperarea folosește handshake-ul semnat. Nu există `NOSESSION` sau resetare
în clar, mesaj de downgrade ori revenire automată la UDP simplu. Datele DATA
necunoscute dintr-o sesiune veche nu sunt acceptate ca semnal de recuperare.

### ⚠️ Limita de securitate și limitările

UDP rămâne un transport cu pierderi. UDPSEC nu adaugă confirmarea livrării,
buffering al payload-ului sau replay al payload-ului în timpul recuperării.
Închiderea best-effort poate fi, de asemenea, pierdută.

UDPSEC autentifică endpoint-urile configurate și protejează conținutul
transportului. Nu stabilește adevărul semantic, originea fizică sau exactitatea
unui raport AIS. Proprietățile de forward secrecy depind de eliminarea
secretelor efemere și de faptul că endpoint-urile nu sunt compromise cât timp
acele secrete sunt active.

UDP simplu configurat explicit nu primește niciuna dintre proprietățile
criptografice sau de liveness ale UDPSEC. Folosiți izolare de rețea, ACL-uri ale
aplicației și politica firewall atunci când transportul simplu este activat în
mod deliberat.

Consultați [politica de securitate](SECURITY.md),
[contractul comportamental](BEHAVIORAL_CONTRACT.md) și
[ghidul `nmea_sproxy`](nmea_sproxy/README.md) pentru detaliile oficiale de
securitate și configurare.

## 🧰 Operare și observabilitate

### 🎛️ Activarea controlului local

În configurația convențională/sursă-systemd, serviciul de control prin socket
Unix-domain este opțional:

```yaml
control:
  unix:
    enabled: true
    socket_path: /run/aismixer/control.sock
    socket_mode: "0660"
```

Unitatea systemd instalată creează `/run/aismixer` cât timp rulează.
Configurația OpenWrt din pachet activează în prezent controlul în mod implicit.

Proprietarul, grupul și modul socket-ului Unix reprezintă limita de control al
accesului. Nu există un token suplimentar de autentificare la nivelul aplicației.
Interfața necesită suport POSIX pentru socket-uri Unix-domain.

### 📊 Starea rutării și statisticile runtime

Cu socket-ul implicit deținut de root:

```text
sudo aismixerctl
aismixerctl> status
aismixerctl> show statistics
aismixerctl> show statistics inputs
aismixerctl> show statistics outputs
```

`status` raportează generația rutării, activarea, zonele, rutele și
destinațiile; nu raportează starea serviciului systemd/procd. Statisticile sunt
snapshot-uri noi, locale procesului, cu vizualizări agregate și cu
vizualizările per-intrare/per-ieșire acceptate în prezent.

Rulați mai întâi o vizualizare nefiltrată a statisticilor pentru a descoperi
valorile filtrelor. Filtrul de intrare este numele runtime exact afișat. Filtrul
de ieșire este un nume canonic exact, precum `udp:local_display`, sau un număr
zecimal afișat al destinației, local procesului. Un filtru fără potrivire
returnează o vizualizare goală.

Folosiți `systemctl status aismixer` sau `/etc/init.d/aismixer status` pentru
starea serviciului în implementarea corespunzătoare.

Folosiți `help` în shell-ul interactiv. Pentru scripturi sunt disponibile
comenzi one-shot echivalente.

### 🎛️ Înlocuirea sau dezactivarea rutării runtime

```bash
sudo aismixerctl replace \
  --file /etc/aismixer/routing-update.yaml \
  --expected-generation 3
sudo aismixerctl disable --expected-generation 4
```

Fișierul prezentat aici este o secțiune directă de rutare; CLI-ul acceptă și o
mapare completă care conține `routing:`. ID-urile destinațiilor trebuie să
existe deja în procesul activ. Numerele de generație sunt ilustrative: folosiți
valoarea curentă din `status`. Protecția este opțională, iar CLI-ul nu reîncearcă
actualizările cu stare învechită.

Exemplul din checkout-ul depozitului este
[`examples/routing-update.yaml`](examples/routing-update.yaml).

## ⚠️ Limitări actuale

- UDP este singurul adaptor de ieșire al mixerului și rămâne un transport de
  datagrame fără garanții.
- Starea rutării runtime, numerele de generație și statisticile sunt locale
  procesului; modificările live ale rutării nu sunt persistente.
- Sesiunile securizate și înregistrările anti-replay sunt locale procesului și
  nepersistente. Nonce-urile DATA rămân pe durata epocii cheii lor de trafic, în
  loc să expire pe baza unui TTL pentru nonce.
- Sesiunile nu migrează automat după schimbarea adresei sau portului corespondentului.
- AISMixer nu păstrează în buffer și nu retransmite payload-uri NMEA în timpul
  recuperării UDPSEC.
- Controlul local folosește în prezent un socket Unix-domain POSIX și
  permisiunile sistemului de fișiere; nu are token la nivelul aplicației.
- Serviciul nu oferă filtrare geografică sau după conținut MMSI, stocare pe
  termen lung, analiză ori detectarea spoofing-ului/anomaliilor AIS.
- Runtime-ul actual de procesare este Python și local procesului; nu există
  procesor nativ separat, coordonator de workeri, plan de rutare IPC sau agregare
  a statisticilor între procese.
- În general, configurația nu este reîncărcată dinamic. Mutația live acceptată
  este snapshot-ul de rutare local procesului, expus prin controlul local.

## 📚 Exemple și documentație

Toate exemplele necesită adaptare de către operator. Ele nu sunt încărcate
automat.

- [Ghid pentru exemple](examples/README.md)
- [Configurație de rutare statică](examples/config-routing.yaml)
- [Rutare cu control local](examples/config-routing-control.yaml)
- [Actualizare runtime a rutării](examples/routing-update.yaml)
- [Ghidul operatorului `nmea_sproxy`](nmea_sproxy/README.md)
- [Contract comportamental](BEHAVIORAL_CONTRACT.md)
- [Politică de securitate](SECURITY.md)
- [Listă de modificări](CHANGELOG.md)
- [Licență](LICENSE)
- [Ghid pentru contribuții](CONTRIBUTING.md)
- [Foaie de parcurs](ROADMAP.md)
- [GitHub Wiki](https://github.com/iliyan85/aismixer/wiki)
- [Site web public](https://aismixer.net)

[Înapoi la selectorul de limbă](#languages)
