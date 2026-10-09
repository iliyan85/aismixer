<a id="languages"></a>

**[English](#english) · [Български](#bulgarian) · [Română](#romanian)**

<a id="english"></a>

# 🛰️ AISMixer — AIS NMEA 0183 stream processing and routing

**Normalize · Deduplicate · Tag · Route · Forward**

AISMixer's source code is publicly available under [CC BY-NC 4.0](LICENSE).
Use is subject to the license terms, including its non-commercial restriction.

## 🧭 What AISMixer is

AISMixer combines AIS NMEA 0183 data from many receivers into clean logical
streams. Its long-running service, `aismixer`, receives supported AIS
sentences such as `!AIVDM` and `!AIVDO` over plain UDP and over UDPSEC, the
project's authenticated and encrypted UDP transport. It reassembles multipart
messages, removes near-real-time duplicates, manages NMEA 4.0 TAG metadata,
and forwards the result to configured UDP destinations.

A network of shore, harbour, and mobile receivers produces overlapping copies
of the same AIS traffic with inconsistent metadata, often over lossy, shared,
or untrusted links. Chart plotters, aggregators, and analysis tools need one
deduplicated, consistently tagged stream per destination, and remote stations
need a transport that authenticates them and copes with ordinary mobile-network
behaviour. AISMixer provides both.

### 🧩 Components

- `aismixer` — the mixer, router, data-plane service, and UDPSEC server;
- `aismixerctl` — the local CLI for routing control and runtime statistics;
- `nmea_sproxy` — the station-side proxy from one local UDP or serial/USB
  input to one UDPSEC or explicitly configured plain-UDP output.

### ⚙️ Processing model

Each ingress datagram is scanned for supported AIS NMEA sentences. Multipart
fragments may arrive out of order; exact repeats are idempotent, while a
conflicting fragment invalidates that live group. Completed multipart messages
are deduplicated and emitted as one group so a destination does not receive a
partial duplicate.

In legacy mode, duplicate suppression is global. In routing mode, it is scoped
per target, so the same logical AIS message may legitimately reach two distinct
destinations once each. Bounded ingress, processing, and egress queues apply
backpressure rather than creating unbounded memory growth. TAG construction,
multipart assembly, deduplication, and routing run in one ordered pipeline.

Main capabilities:

- UDP ingress over IPv4 and IPv6, with optional per-listener allow-lists;
- authenticated and encrypted UDPSEC ingress from `nmea_sproxy` stations;
- serial and USB virtual-serial reception through `nmea_sproxy`;
- multipart AIS assembly and group-atomic deduplication;
- controlled TAG `s`, `c`, and `g` handling;
- global fan-out or logical routing to named UDP targets;
- optional outbound source-address binding;
- bounded queues, backpressure, and process-local operational statistics.

```text
 AIS receiver ── UDP (LAN) ─────────────────┐
                                            v
 serial/USB or UDP receiver           +----------+      +------------------+
   └─ nmea_sproxy ── UDPSEC / UDP ──→ | aismixer | ───→ | UDP destinations |
                                      +----------+      +------------------+
                                            ^
                                            │
                                      aismixerctl (optional local control)
```

The [behavioural contract](BEHAVIORAL_CONTRACT.md) owns the exact, tested
processing, routing, runtime, and UDPSEC semantics. This README is the project
and operator overview.

## 🔐 UDPSECv2 secure transport

UDPSEC is AISMixer's authenticated and encrypted UDP transport between
`nmea_sproxy` stations and `aismixer`. It is a project-specific protocol, not
an external standard; the current version is UDPSECv2.

- **Identity:** long-term P-256 ECDSA key pairs identify each station and the
  mixer. A station trusts one configured mixer public key; the mixer accepts a
  station only if its public key is authorized under its `station_id`.
- **Establishment:** a signed ephemeral P-256 ECDHE handshake derives separate
  AES-256-GCM traffic keys for each direction, and an encrypted key-possession
  confirmation completes it. There is no version negotiation or downgrade.
- **Encrypted DATA:** NMEA payloads, keepalive pings and pongs, and all
  in-session control messages travel in one encrypted DATA channel; every
  packet is bound to its session and to its traffic-key epoch.
- **Replay protection:** the mixer keeps every nonce it admits from a station
  for the life of that key epoch and rejects repeats. If the current epoch's
  bounded ledger fills, the mixer ends the session and the station performs a
  fresh handshake at its `peer_timeout` bound; NMEA sent in between is lost,
  and a nonzero `session_refresh_interval` gives each refreshed epoch a fresh
  ledger. The station keeps no nonce ledger; it acts on a mixer answer only
  when it matches a request still outstanding, and only once.
- **Authenticated liveness:** only authenticated, matching answers count as
  evidence that the peer is still reachable.
- **Key epoch refresh (optional):** with `session_refresh_interval` above zero,
  the station renews the traffic keys inside the same session through a fresh
  signed ECDHE exchange. This is neither a new session nor a path change.
- **Authenticated path migration:** a live session can move to a new public
  address or port only after the mixer has verified return routability.

A session is bound to the authenticated station and identified by a
mixer-issued session locator, never by the source IP address and port.
UDPSEC has no plaintext reset, no downgrade message, and no automatic fallback
to plain UDP; unauthenticated datagrams cannot change session state. Session
and replay state is in memory and process-local, so restarting either end
requires a fresh session.

The current UDPSECv2 wire format does not interoperate with earlier builds:
upgrade `aismixer` and `nmea_sproxy` together.

### 📶 Mobile continuity

Mobile stations lose packets, and their public IP address or UDP port can
change. Field runs showed that cellular handovers often keep the same public
address and port, so UDPSECv2 treats a temporary loss differently from a real
address change:

| Situation | Recovery |
| --- | --- |
| Short loss on the same path | **Liveness recovery.** The station keeps resending its one outstanding keepalive ping at a bounded rate, with the same sequence number and a fresh encryption each time, until an authenticated answer arrives. The session continues; the only terminal bound is `peer_timeout` (default 90 s) after the last authenticated evidence. |
| New public address or port while the session is alive | **Authenticated path migration.** Packets from the new address must authenticate under the session's current keys. The mixer sends a challenge there, at most four times in total, at least 2 s apart, within a fixed 10 s window, and moves its replies to the new address only after the station's authenticated response arrives from that address. Session, keys, and replay state are kept. |
| Long or terminal outage | **Fresh authenticated establishment.** Once the liveness bound is reached, the station performs a new signed handshake; the mixer expires the old idle session on its own. |

Forwarding continues while liveness is in doubt, but UDPSEC never buffers or
resends NMEA: sentences sent into an outage can be lost even when the session
survives, and no session survives every outage. Field validation so far covers
same-path recovery; path migration is covered by end-to-end tests. The
[`nmea_sproxy` guide](nmea_sproxy/README.md) explains the related log lines and
status fields.

## 🗺️ Deployment patterns

| Pattern | Typical setup |
| --- | --- |
| Receiver on a trusted LAN | receiver → plain UDP → `aismixer`, restricted by `allow_from` and firewall rules |
| Remote fixed station | serial/USB or UDP receiver → `nmea_sproxy` → UDPSEC → `aismixer` |
| Mobile station on a vessel or vehicle | as above, over cellular or CGNAT links, with mobile continuity |
| Router-based edge node | OpenWrt running `nmea_sproxy` at the station, or `aismixer` as a local mixer |
| Several consumers | `aismixer` fan-out or named routes to separate UDP destinations |

`aismixer` and `nmea_sproxy` run on systemd-based Debian and Raspberry Pi OS
hosts and as OpenWrt 25.12 packages. `nmea_sproxy` can also run manually,
including on Windows, without service integration.

## 🚀 Quick start on Linux with systemd

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

The shipped `aismixer.service` sets no `User=`/`Group=`, so it runs with the
privileges of whichever account starts it -- root by default. Operators
wanting privilege isolation should create a dedicated service account
themselves and add matching `User=`/`Group=` directives to the unit.

#### ⚙️ Configure before the first start

```bash
sudoedit /etc/aismixer/config.yaml
sudoedit /etc/aismixer/authorized_keys.yaml
```

The seeded configuration contains listeners bound broadly and without
application allow-lists. Before starting, adapt addresses, ports,
`allow_from` rules, forwarders, UDPSEC authorization, host firewall rules,
and routing policy for the deployment. Plain UDP has none of UDPSEC's
confidentiality, authentication, integrity, replay, or liveness protection,
and application allow-lists complement rather than replace the host firewall.

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
modified. Update UDPSEC stations together with the mixer.

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

To connect remote stations, add a UDPSEC listener and authorize each station
(see "UDPSEC ingress and station authorization" below), then follow the
[`nmea_sproxy` operator guide](nmea_sproxy/README.md) on the station.

## 📦 OpenWrt 25.12

OpenWrt is a full edge-deployment target: a router can run `nmea_sproxy` next
to a receiver, or `aismixer` as a local mixer. Versioned OpenWrt 25.12 APK
packages with procd integration come from one package recipe:

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

**Package status.** The recipe builds `0.2.1-r4` from the pinned v0.2.1
release source. These packages predate the UDPSECv2 session continuity
described above (key epoch refresh, path migration, and liveness recovery),
and their UDPSEC wire format does not interoperate with the current source
tree. Use one release line on both ends of a UDPSEC relation. Packages with
this work need a later release, at which the recipe is repinned; check the
package revision and the [changelog](CHANGELOG.md).

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

Update every installed AISMixer package in one `apk` command from the
device's configured feed, because `aismixer` and `nmea_sproxy` each require
the identical `aismixer-common` revision. With both installed, run
`apk --update-cache add --upgrade aismixer-common aismixer nmea_sproxy`;
with only one, name `aismixer-common` and that package:
`apk --update-cache add --upgrade aismixer-common aismixer` on a mixer, or
`apk --update-cache add --upgrade aismixer-common nmea_sproxy` on a station.
Never name a package the device does not have: `apk add` would install it,
and its hook would enable and start it. The update hooks stop and start each
service even if it was previously stopped, while preserving its
enable/disable state.
`apk del aismixer` stops, disables, and removes it. The package has no
project-specific purge contract, so this README makes no promise about
configuration or key retention after removal.

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
    listen_port: 17778
    allow_from:
      - 192.0.2.0/24

forwarders:
  - id: local_display
    host: 127.0.0.1
    port: 19000
```

Adapt all example addresses, ports, IDs, paths, and policy before use.
Repository examples are inactive until copied or adapted. The examples use
port 17778 for plain UDP and 17779 for UDPSEC; both are project-chosen example
values and remain configurable. The seeded and packaged configurations in the
current source also use 17779 for UDPSEC; already published packages keep the
configuration of the release they were built from.

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

### 🔐 UDPSEC ingress and station authorization

Add a secure listener, and authorize each station's public key under its
`station_id` in `/etc/aismixer/authorized_keys.yaml`:

```yaml
sec_inputs:
  - listen_ip: "0.0.0.0"
    listen_port: 17779
```

```yaml
authorized_clients:
  - name: boat_001
    pubkey: <compressed-public-key-base64>
```

Each station's `output.host` and `output.port` must name this listener.
Restart `aismixer` after authorization changes. Give each station the mixer
public key, `/etc/aismixer/keys/aismixer_public.pem` on conventional
deployments, through a trusted channel; the station guide covers both sides.

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
select target subsets, and deduplication is scoped separately to each target.
Zones are logical sets of internal source IDs—not geographic areas, MMSI
lists, vessel filters, or emitted TAG labels.

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
filter (`show statistics inputs <SELECTOR>`) is the exact SELECTOR column
value, a stable, address-independent identity such as `udp-ingress:0`; the
INPUT column is a human-readable label and is not matched. An output filter is
an exact canonical name such as `udp:local_display` or a displayed decimal
process-local target number. A filter with no match returns an empty view.

Use `systemctl status aismixer` or `/etc/init.d/aismixer status` for service
health on the corresponding deployment. Use `help` in the interactive shell;
equivalent one-shot commands are available for scripts.

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

## ⚠️ Security notes and current limitations

UDPSEC authenticates configured endpoints and protects transport contents. It
does not establish the semantic truth, physical origin, or accuracy of an AIS
report. Forward-secrecy properties depend on ephemeral secrets being discarded
and endpoints not being compromised while those secrets are live. Protect
station and mixer private keys, and never copy a station private key to the
mixer. Explicit plain UDP receives none of UDPSEC's cryptographic or liveness
properties; use network isolation, application ACLs, and firewall policy where
plain transport is deliberately enabled.

Current limitations:

- UDP is the mixer's only egress adapter and remains an unreliable datagram
  transport; UDPSEC adds no delivery acknowledgement or payload replay.
- Runtime routing state, generation numbers, and statistics are process-local;
  live routing changes are not persisted.
- Secure sessions and replay records are process-local and non-durable. DATA
  nonces remain for their traffic-key epoch rather than expiring on a nonce TTL.
- A session moves to a new address only while it is alive and the new path is
  proven; after a terminal outage, a fresh handshake is required.
- Local control currently uses a POSIX Unix-domain socket and filesystem
  permissions; it has no application token.
- The service does not provide geographic or MMSI content filtering, long-term
  storage, analytics, or AIS spoof/anomaly detection.
- The current processing runtime is Python and process-local; there is no
  separate native processor, worker coordinator, IPC routing plane, or
  cross-process statistics aggregation.
- Configuration is not generally hot-reloaded. The supported live mutation is
  the process-local routing snapshot exposed through local control.

See the [security policy](SECURITY.md) for vulnerability reporting.

## 🧭 Project status and roadmap

AISMixer is in active pre-1.0 development; pre-1.0 releases may still change
configuration and interfaces. The latest tagged release is v0.2.1. The `main`
branch also contains unreleased work, including the UDPSECv2 session
continuity described above; see the [changelog](CHANGELOG.md).

Planned directions, none of which is implemented yet (see the
[roadmap](ROADMAP.md)):

- further operational deployment hardening;
- a later multi-process architecture and a native processor behind the
  existing processor contract;
- routing-state operations such as optional persistence and safe reload;
- maritime security and data-quality research, including AIS spoof and
  anomaly detection;
- additional egress adapters and remote authenticated control.

## 📚 Documentation, license, and contributing

All examples require operator adaptation. They are not loaded automatically.

- [Examples guide](examples/README.md)
- [Static routing configuration](examples/config-routing.yaml)
- [Routing with local control](examples/config-routing-control.yaml)
- [Runtime routing update](examples/routing-update.yaml)
- [`nmea_sproxy` operator guide](nmea_sproxy/README.md)
- [Behavioural contract](BEHAVIORAL_CONTRACT.md)
- [Security policy](SECURITY.md)
- [Changelog](CHANGELOG.md)
- [Roadmap](ROADMAP.md)
- [GitHub Wiki](https://github.com/iliyan85/aismixer/wiki) for deeper
  architecture and deployment guides
- [Public website](https://aismixer.net)

AISMixer is licensed under [CC BY-NC 4.0](LICENSE). See the
[contributing guide](CONTRIBUTING.md) and the
[code of conduct](CODE_OF_CONDUCT.md) before opening issues or pull requests.

[Back to language selector](#languages)

---

<a id="bulgarian"></a>

# 🇧🇬 AISMixer — обработка и маршрутизация на AIS NMEA 0183 потоци

**Нормализиране · Дедупликация · TAG метаданни · Маршрутизация · Препращане**

Изходният код на AISMixer е публично достъпен при условията на
[CC BY-NC 4.0](LICENSE). Лицензът на хранилището разрешава използване при
спазване на условията му, включително ограничението за нетърговска употреба.

## 🧭 Какво е AISMixer

AISMixer обединява AIS NMEA 0183 данни от множество приемници в чисти
логически потоци. Дългосрочно работещата му услуга `aismixer` приема
поддържани AIS изречения като `!AIVDM` и `!AIVDO` през plain UDP и през
UDPSEC — удостоверения и криптиран UDP транспорт на проекта. Тя сглобява
многосъставни съобщения, премахва дубликати в почти реално време, управлява
NMEA 4.0 TAG метаданни и препраща резултата към конфигурираните UDP дестинации.

Мрежа от брегови, пристанищни и мобилни приемници създава припокриващи се
копия на един и същ AIS трафик с несъгласувани метаданни, често през
ненадеждни, споделени или недоверени връзки. Картографските плотери,
агрегаторите и инструментите за анализ се нуждаят от един дедупликиран и
последователно маркиран поток за всяка дестинация, а отдалечените станции — от
транспорт, който ги удостоверява и се справя с обичайното поведение на
мобилните мрежи. AISMixer осигурява и двете.

### 🧩 Компоненти

- `aismixer` — услугата за смесване, маршрутизация и обработка в слоя за
  данни, както и UDPSEC сървърът;
- `aismixerctl` — локалният CLI за управление на маршрутизацията и
  статистиката по време на работа;
- `nmea_sproxy` — проксито при станцията от един локален UDP или сериен/USB
  вход към един UDPSEC или изрично конфигуриран plain-UDP изход.

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
допускат неограничен растеж на паметта. Изграждането на TAG метаданни,
сглобяването на многосъставни съобщения, дедупликацията и маршрутизацията
работят в един подреден конвейер.

Основни възможности:

- UDP вход през IPv4 и IPv6 с незадължителни списъци с разрешени адреси за
  всеки listener;
- удостоверен и криптиран UDPSEC вход от станции с `nmea_sproxy`;
- приемане от сериен или USB виртуален сериен интерфейс чрез `nmea_sproxy`;
- сглобяване на многосъставни AIS съобщения и дедупликация, атомарна за цялата
  група;
- контролирано управление на TAG стойностите `s`, `c` и `g`;
- глобално разпращане или логическа маршрутизация към именувани UDP цели;
- незадължително задаване на изходен адрес;
- ограничени опашки, backpressure и локална за процеса оперативна статистика.

```text
 AIS приемник ── UDP (LAN) ─────────────────┐
                                            v
 сериен/USB или UDP приемник          +----------+      +------------------+
   └─ nmea_sproxy ── UDPSEC / UDP ──→ | aismixer | ───→ | UDP дестинации   |
                                      +----------+      +------------------+
                                            ^
                                            │
                                      aismixerctl (незадължително локално управление)
```

[Поведенческият договор](BEHAVIORAL_CONTRACT.md) определя точната и тествана
семантика на обработката, маршрутизацията, поведението по време на работа и
UDPSEC. Настоящият README е обзор на проекта и ръководство за оператора.

## 🔐 Защитен транспорт UDPSECv2

UDPSEC е удостовереният и криптиран UDP транспорт на AISMixer между станции с
`nmea_sproxy` и `aismixer`. Това е специфичен за проекта протокол, а не
външен стандарт; текущата версия е UDPSECv2.

- **Идентичност:** дългосрочни P-256 ECDSA двойки ключове идентифицират всяка
  станция и mixer-а. Станцията се доверява на един конфигуриран публичен ключ
  на mixer-а; mixer-ът приема станция само ако публичният ѝ ключ е разрешен
  под нейния `station_id`.
- **Установяване:** подписан ефимерен P-256 ECDHE handshake извежда отделни
  AES-256-GCM ключове за трафика във всяка посока, а криптирано доказване на
  притежанието на ключовете го завършва. Няма договаряне на версия и няма
  downgrade.
- **Криптирани DATA пакети:** NMEA данните, keepalive ping/pong съобщенията и
  всички контролни съобщения в сесията се пренасят в един криптиран DATA
  канал; всеки пакет е обвързан със своята сесия и със своята епоха на
  ключовете за трафик.
- **Защита от replay:** mixer-ът пази всеки nonce, който приеме от станция,
  за целия живот на съответната ключова епоха и отхвърля повторенията. Ако
  ограниченият регистър на nonce стойностите за текущата епоха се запълни,
  mixer-ът прекратява сесията, а станцията извършва нов handshake при
  достигане на границата `peer_timeout`; NMEA данните, изпратени междувременно,
  се губят, а ненулев `session_refresh_interval` дава нов регистър на всяка
  опреснена епоха. Станцията не води регистър на nonce стойностите; тя приема
  отговор от mixer-а само ако той съответства на нейна заявка, която все още
  чака отговор, и само веднъж.
- **Удостоверена проверка за активност:** само удостоверени и съвпадащи
  отговори се приемат като доказателство, че отсрещната страна е достижима.
- **Опресняване на ключовата епоха (по избор):** при `session_refresh_interval`
  над нула станцията обновява ключовете за трафик в рамките на същата сесия
  чрез нов подписан ECDHE обмен. Това не е нито нова сесия, нито смяна на пътя.
- **Удостоверена миграция на пътя:** активна сесия може да премине към нов
  публичен адрес или порт само след като mixer-ът е проверил обратната
  достижимост (return routability).

Сесията е обвързана с удостоверената станция и се идентифицира чрез издаден
от mixer-а локатор на сесията, никога чрез IP адреса и порта на източника.
UDPSEC няма plaintext нулиране, downgrade съобщение или автоматичен fallback
към plain UDP; неудостоверени дейтаграми не могат да променят състоянието на
сесията. Състоянието на сесиите и на защитата от replay е в паметта и е
локално за процеса, затова рестартирането на която и да е от двете страни
изисква нова сесия.

Текущият формат на пакетите на UDPSECv2 не е съвместим с по-ранни версии:
обновявайте `aismixer` и `nmea_sproxy` заедно.

### 📶 Мобилна непрекъснатост

Мобилните станции губят пакети, а публичният им IP адрес или UDP порт може да
се смени. Полевите тестове показаха, че при смяна на клетката в мобилната
мрежа публичният адрес и портът често остават същите, затова UDPSECv2
третира временната загуба различно от реалната смяна на адреса:

| Ситуация | Възстановяване |
| --- | --- |
| Кратка загуба по същия път | **Възстановяване на активността.** Станцията повтаря с ограничена честота единствения си неотговорен keepalive ping, със същия пореден номер и с ново криптиране всеки път, докато пристигне удостоверен отговор. Сесията продължава; единствената крайна граница е `peer_timeout` (по подразбиране 90 s) след последното удостоверено доказателство. |
| Нов публичен адрес или порт при активна сесия | **Удостоверена миграция на пътя.** Пакетите от новия адрес трябва да бъдат удостоверени с текущите ключове на сесията. Mixer-ът изпраща проверка (challenge) към този адрес най-много четири пъти общо, през поне 2 s, в рамките на фиксиран 10-секунден прозорец, и пренасочва отговорите си към новия адрес едва след като удостовереният отговор на станцията пристигне от същия адрес. Сесията, ключовете и replay състоянието се запазват. |
| Дълго или окончателно прекъсване | **Ново удостоверено установяване.** Щом бъде достигната границата за активност, станцията извършва нов подписан handshake; mixer-ът сам премахва старата неактивна сесия. |

Препращането продължава, докато активността е под съмнение, но UDPSEC никога
не буферира и не изпраща повторно NMEA данни: изречения, изпратени по време на
прекъсване, може да се загубят дори когато сесията оцелее, и никоя сесия не
оцелява при всяко прекъсване. Полевото валидиране досега обхваща
възстановяването по същия път; миграцията на пътя е покрита от end-to-end
тестове. [Ръководството за `nmea_sproxy`](nmea_sproxy/README.md) описва
свързаните редове в логовете и полетата за състояние.

## 🗺️ Типични схеми на разгръщане

| Схема | Типична конфигурация |
| --- | --- |
| Приемник в доверена локална мрежа | приемник → plain UDP → `aismixer`, ограничен чрез `allow_from` и правила на защитната стена |
| Отдалечена стационарна станция | сериен/USB или UDP приемник → `nmea_sproxy` → UDPSEC → `aismixer` |
| Мобилна станция на плавателен съд или превозно средство | както по-горе, през мобилни или CGNAT връзки, с мобилна непрекъснатост |
| Периферен възел на рутер | OpenWrt с `nmea_sproxy` при станцията или с `aismixer` като локален mixer |
| Няколко консуматора | разпращане от `aismixer` или именувани маршрути към отделни UDP дестинации |

`aismixer` и `nmea_sproxy` работят на Debian и Raspberry Pi OS системи със
systemd и като OpenWrt 25.12 пакети. `nmea_sproxy` може да се стартира и
ръчно, включително под Windows, без интеграция като услуга.

## 🚀 Бърз старт в Linux със systemd

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
включва услугата за стартиране при зареждане. Той умишлено **не стартира**
услугата.

Доставеният `aismixer.service` не задава `User=`/`Group=`, затова работи с
правата на акаунта, който го стартира — по подразбиране root. Операторите,
които искат изолация на правата, трябва сами да създадат отделен служебен
акаунт и да добавят съответните директиви `User=`/`Group=` в юнита.

#### ⚙️ Конфигуриране преди първото стартиране

```bash
sudoedit /etc/aismixer/config.yaml
sudoedit /etc/aismixer/authorized_keys.yaml
```

Началната конфигурация съдържа listener-и, свързани към широк кръг адреси и
без приложни списъци с разрешени източници. Преди стартиране адаптирайте
адресите, портовете, правилата `allow_from`, UDP целите, разрешенията за
UDPSEC, правилата на защитната стена на хоста и политиката за маршрутизация
към конкретното разгръщане. Plain UDP няма нито една от защитите на UDPSEC —
поверителност, удостоверяване, цялост, защита от replay или проверки за
активност, а приложните списъци с разрешени адреси допълват, но не заменят
защитната стена на хоста.

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

`update.sh` обновява инсталираните файлове на приложението, unit-а и
`aismixerctl`, презарежда systemd и изпълнява `systemctl restart aismixer`.
Рестартирането също стартира неактивна услуга; скриптът за обновяване не
запазва състояние на умишлено спряна услуга. Операторската конфигурация и
ключовете в `/etc/aismixer` не се променят пряко. Обновявайте UDPSEC
станциите заедно с mixer-а.

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

За да свържете отдалечени станции, добавете UDPSEC listener и разрешете всяка
станция (вижте „UDPSEC вход и разрешаване на станции“ по-долу), след което
следвайте [операторското ръководство за `nmea_sproxy`](nmea_sproxy/README.md)
на станцията.

## 📦 OpenWrt 25.12

OpenWrt е пълноценна платформа за периферно разгръщане: рутер може да изпълнява
`nmea_sproxy` до приемника или `aismixer` като локален mixer. Версионираните
OpenWrt 25.12 APK пакети с procd интеграция се създават от една и съща рецепта
за пакетиране:

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

**Състояние на пакетите.** Рецептата изгражда `0.2.1-r4` от фиксирания
изходен код на изданието v0.2.1. Тези пакети предхождат описаната по-горе
непрекъснатост на сесиите в UDPSECv2 (опресняване на ключовата епоха, миграция
на пътя и възстановяване на активността), а техният UDPSEC формат на пакетите
не е съвместим с текущото дърво на изходния код. Използвайте една и съща
версия от двете страни на UDPSEC връзката. Пакети с тази функционалност
изискват по-късно издание, при което рецептата се фиксира наново; проверявайте
ревизията на пакета и [списъка на промените](CHANGELOG.md).

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

Обновете с една команда `apk` всички инсталирани пакети на AISMixer от
конфигурирания на устройството feed, защото `aismixer` и `nmea_sproxy`
изискват точно същата ревизия на пакета `aismixer-common`. Ако на
устройството са инсталирани и `aismixer`, и `nmea_sproxy`, изпълнете
`apk --update-cache add --upgrade aismixer-common aismixer nmea_sproxy`; ако
е инсталиран само единият, посочете `aismixer-common` и този пакет:
`apk --update-cache add --upgrade aismixer-common aismixer` на mixer или
`apk --update-cache add --upgrade aismixer-common nmea_sproxy` на станция.
Никога не посочвайте пакет, който устройството няма: `apk add` ще го
инсталира, а hook скриптът му ще включи и стартира услугата.
Hook скриптовете за обновяване спират и стартират всяка услуга дори
ако е била спряна, като запазват състоянието ѝ за включване при зареждане.
`apk del aismixer` спира, изключва и премахва пакета. Пакетът не определя
специфично за проекта поведение за пълно изчистване, затова този README не
обещава запазване на конфигурацията или ключовете след премахването.

Инсталирайте `nmea_sproxy` вместо или заедно с mixer-а, когато рутерът е
крайната точка при станцията:

```sh
apk -U add nmea_sproxy
```

Hook скриптът на пакета също опитва да стартира услугата, но нова UDPSEC връзка
няма доверен публичен ключ на mixer-а и обикновено не преминава предварителната
проверка. Осигурете доверието и рестартирайте според ръководството за
компонента; само инсталирането не създава готова връзка.

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
    listen_port: 17778
    allow_from:
      - 192.0.2.0/24

forwarders:
  - id: local_display
    host: 127.0.0.1
    port: 19000
```

Адаптирайте всички примерни адреси, портове, идентификатори, пътища и правила
преди употреба. Примерите в хранилището са неактивни, докато не бъдат копирани
или адаптирани. Примерите използват порт 17778 за plain UDP и 17779 за UDPSEC;
това са избрани от проекта примерни стойности, които остават конфигурируеми.
Началните и пакетираните конфигурации в текущия изходен код също използват
17779 за UDPSEC; вече публикуваните пакети запазват конфигурацията на
изданието, от което са изградени.

### 📡 Входове и UDP цели

- `udp_inputs` приема plain UDP. Полето `id` дава на входа стабилна вътрешна
  идентичност за маршрутизация.
- `sec_inputs` приема удостоверен UDPSEC и извежда идентичността за
  маршрутизация от удостоверената станция.
- `forwarders` определя UDP дестинациите. Каноничната идентичност на
  именувана UDP цел е `udp:<id>`.
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

### 🔐 UDPSEC вход и разрешаване на станции

Добавете защитен listener и разрешете публичния ключ на всяка станция под
нейния `station_id` в `/etc/aismixer/authorized_keys.yaml`:

```yaml
sec_inputs:
  - listen_ip: "0.0.0.0"
    listen_port: 17779
```

```yaml
authorized_clients:
  - name: boat_001
    pubkey: <compressed-public-key-base64>
```

`output.host` и `output.port` на всяка станция трябва да сочат към този
listener. Рестартирайте `aismixer` след промени в разрешенията. Предайте на
всяка станция публичния ключ на mixer-а — при стандартно разгръщане
`/etc/aismixer/keys/aismixer_public.pem` — по доверен канал; ръководството за
станцията описва и двете страни.

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
глобална и всяко прието изходно изречение се изпраща към всеки конфигуриран
UDP forwarder. В този режим за съвместимост forwarder-ите не се нуждаят от
идентификатори.

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
за филтриране. Входният филтър (`show statistics inputs <SELECTOR>`) е точната
стойност от колоната SELECTOR — стабилна идентичност, независима от адреса,
например `udp-ingress:0`; колоната INPUT е етикет за хора и не се съпоставя.
Изходният филтър е точно канонично име като `udp:local_display` или показан
десетичен номер на целта, валиден само за процеса. Филтър без съвпадение
връща празен изглед.

Използвайте `systemctl status aismixer` или `/etc/init.d/aismixer status` за
състоянието на услугата при съответния начин за разгръщане. Използвайте
`help` в интерактивния shell; за скриптове са налични еквивалентни
еднократни команди.

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

## ⚠️ Бележки за сигурността и текущи ограничения

UDPSEC удостоверява конфигурираните крайни точки и защитава съдържанието при
пренос. Той не установява семантичната достоверност, физическия произход или
точността на AIS съобщението. Свойствата за forward secrecy зависят от
унищожаването на ефимерните тайни и от това крайните точки да не бъдат
компрометирани, докато тези тайни са активни. Пазете частните ключове на
станциите и на mixer-а и никога не копирайте частен ключ на станция в mixer-а.
Изрично конфигурираният plain UDP не получава криптографските свойства или
проверките за активност на UDPSEC; използвайте мрежова изолация, приложни ACL
правила и защитна стена там, където plain транспортът е разрешен умишлено.

Текущи ограничения:

- UDP е единственият изходен адаптер на mixer-а и остава ненадежден
  дейтаграмен транспорт; UDPSEC не добавя потвърждение за доставка или
  повторно изпращане на полезните данни.
- Състоянието на маршрутизацията по време на работа, номерата на поколенията и
  статистиките са локални за процеса; текущите промени на маршрутизацията не
  се записват трайно.
- Защитените сесии и replay записите са локални за процеса и нетрайни. DATA
  nonce стойностите остават за епохата на ключа си за трафик и нямат
  независим TTL.
- Сесията преминава към нов адрес само докато е активна и новият път е
  доказан; след окончателно прекъсване е необходим нов handshake.
- Локалното управление в момента използва POSIX Unix-domain socket и
  разрешенията на файловата система; няма application token.
- Услугата не предоставя географско или MMSI филтриране на съдържанието,
  дългосрочно съхранение, анализи или откриване на AIS spoof/anomaly.
- Текущата обработка е на Python и е локална за процеса; няма отделен native
  процесор, coordinator за worker-и, IPC routing plane или агрегиране на
  статистика между процеси.
- Конфигурацията по принцип не се презарежда в движение. Поддържаната промяна
  по време на работа е локалната за процеса моментна конфигурация на
  маршрутизацията, достъпна през локалното управление.

Вижте [политиката за сигурност](SECURITY.md) за докладване на уязвимости.

## 🧭 Състояние на проекта и пътна карта

AISMixer е в активна разработка преди версия 1.0; изданията преди 1.0 все
още могат да променят конфигурацията и интерфейсите. Последното издание с
етикет е v0.2.1. Клонът `main` съдържа и неиздадена работа, включително
описаната по-горе непрекъснатост на сесиите в UDPSECv2; вижте
[списъка на промените](CHANGELOG.md).

Планирани посоки, нито една от които все още не е реализирана (вижте
[пътната карта](ROADMAP.md)):

- допълнително укрепване на оперативното разгръщане;
- бъдеща многопроцесна архитектура и native процесор зад съществуващия
  договор на процесора;
- операции със състоянието на маршрутизацията, например незадължително
  трайно съхранение и безопасно презареждане;
- изследвания в областта на морската сигурност и качеството на данните,
  включително откриване на AIS spoofing и аномалии;
- допълнителни изходни адаптери и отдалечено удостоверено управление.

## 📚 Документация, лиценз и принос

Всички примери изискват адаптация от оператора. Те не се зареждат автоматично.

- [Ръководство за примерите](examples/README.md)
- [Конфигурация за статична маршрутизация](examples/config-routing.yaml)
- [Маршрутизация с локално управление](examples/config-routing-control.yaml)
- [Runtime актуализация на маршрутизацията](examples/routing-update.yaml)
- [Операторско ръководство за `nmea_sproxy`](nmea_sproxy/README.md)
- [Поведенчески договор](BEHAVIORAL_CONTRACT.md)
- [Политика за сигурност](SECURITY.md)
- [Списък на промените](CHANGELOG.md)
- [Пътна карта](ROADMAP.md)
- [GitHub Wiki](https://github.com/iliyan85/aismixer/wiki) с по-подробни
  ръководства за архитектурата и разгръщането
- [Публичен уебсайт](https://aismixer.net)

AISMixer се разпространява под лиценза [CC BY-NC 4.0](LICENSE). Преди да
отворите issue или pull request, прочетете
[ръководството за принос](CONTRIBUTING.md) и
[кодекса за поведение](CODE_OF_CONDUCT.md).

[Към избора на език](#languages)

---

<a id="romanian"></a>

# 🇷🇴 AISMixer — procesarea și rutarea fluxurilor AIS NMEA 0183

**Normalizare · Deduplicare · Etichetare · Rutare · Redirecționare**

Codul-sursă AISMixer este disponibil public sub licența [CC BY-NC 4.0](LICENSE).
Licența depozitului permite utilizarea în condițiile sale, inclusiv restricția
privind utilizarea necomercială.

## 🧭 Ce este AISMixer

AISMixer combină datele AIS NMEA 0183 de la mai multe receptoare în fluxuri
logice curate. Serviciul său de lungă durată, `aismixer`, primește propoziții
AIS acceptate, precum `!AIVDM` și `!AIVDO`, prin UDP simplu și prin UDPSEC,
transportul UDP autentificat și criptat al proiectului. Acesta reasamblează
mesajele multipart, elimină duplicatele aproape în timp real, gestionează
metadatele NMEA 4.0 TAG și redirecționează rezultatul către destinațiile UDP
configurate.

O rețea de receptoare de coastă, din porturi și mobile produce copii
suprapuse ale aceluiași trafic AIS, cu metadate inconsecvente, adesea prin
legături cu pierderi, partajate sau nesigure. Plotterele de hărți,
agregatoarele și instrumentele de analiză au nevoie de un singur flux
deduplicat și etichetat consecvent pentru fiecare destinație, iar stațiile
aflate la distanță au nevoie de un transport care să le autentifice și să
facă față comportamentului obișnuit al rețelelor mobile. AISMixer le oferă pe
amândouă.

### 🧩 Componente

- `aismixer` — serviciul de mixare, rutare și plan de date, precum și
  serverul UDPSEC;
- `aismixerctl` — CLI-ul local pentru controlul rutării și statisticile
  runtime;
- `nmea_sproxy` — proxy-ul de la stație, de la o intrare locală UDP sau
  serial/USB la o ieșire UDPSEC sau UDP simplu configurată explicit.

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
creșterea nelimitată a memoriei. Construirea TAG-urilor, asamblarea multipart,
deduplicarea și rutarea rulează într-un singur pipeline ordonat.

Capabilități principale:

- intrare UDP prin IPv4 și IPv6, cu liste opționale de adrese permise pentru
  fiecare listener;
- intrare UDPSEC autentificată și criptată de la stații cu `nmea_sproxy`;
- recepție serială și prin porturi seriale virtuale USB cu `nmea_sproxy`;
- asamblare AIS multipart și deduplicare atomică la nivel de grup;
- gestionare controlată a câmpurilor TAG `s`, `c` și `g`;
- distribuire globală sau rutare logică spre destinații UDP denumite;
- asocierea opțională a adresei-sursă la ieșire;
- cozi limitate, backpressure și statistici operaționale locale procesului.

```text
 Receptor AIS ── UDP (LAN) ─────────────────┐
                                            v
 receptor serial/USB sau UDP          +----------+      +------------------+
   └─ nmea_sproxy ── UDPSEC / UDP ──→ | aismixer | ───→ | Destinații UDP   |
                                      +----------+      +------------------+
                                            ^
                                            │
                                      aismixerctl (control local opțional)
```

[Contractul comportamental](BEHAVIORAL_CONTRACT.md) stabilește semantica exactă
și testată pentru procesare, rutare, runtime și UDPSEC. Acest README este
prezentarea generală a proiectului și ghidul de orientare pentru operatori.

## 🔐 Transportul securizat UDPSECv2

UDPSEC este transportul UDP autentificat și criptat al AISMixer între stațiile
cu `nmea_sproxy` și `aismixer`. Este un protocol specific proiectului, nu un
standard extern; versiunea actuală este UDPSECv2.

- **Identitate:** perechi de chei P-256 ECDSA pe termen lung identifică fiecare
  stație și mixerul. O stație are încredere într-o singură cheie publică
  configurată a mixerului; mixerul acceptă o stație numai dacă cheia ei
  publică este autorizată sub `station_id`-ul ei.
- **Stabilire:** un handshake P-256 ECDHE efemer și semnat derivă chei de trafic
  AES-256-GCM separate pentru fiecare direcție, iar o confirmare criptată a
  posesiei cheilor îl finalizează. Nu există negociere de versiune sau
  downgrade.
- **DATA criptat:** payload-urile NMEA, ping-urile și pong-urile keepalive și
  toate mesajele de control din sesiune circulă printr-un singur canal DATA
  criptat; fiecare pachet este legat de sesiunea sa și de epoca sa de chei de
  trafic.
- **Protecție anti-replay:** mixerul păstrează fiecare nonce admis de la o
  stație pe toată durata epocii de chei respective și respinge repetările.
  Dacă registrul limitat de nonce-uri al epocii curente se umple, mixerul
  încheie sesiunea, iar stația efectuează un handshake nou la limita
  `peer_timeout`; datele NMEA trimise între timp se pierd, iar un
  `session_refresh_interval` nenul oferă fiecărei epoci reîmprospătate un
  registru nou. Stația nu ține un registru de nonce-uri; acceptă un răspuns
  de la mixer numai dacă acesta corespunde unei cereri proprii aflate încă în
  așteptare și numai o singură dată.
- **Liveness autentificat:** numai răspunsurile autentificate și corespunzătoare
  contează ca dovadă că partenerul este încă accesibil.
- **Reîmprospătarea epocii de chei (opțională):** cu `session_refresh_interval`
  peste zero, stația reînnoiește cheile de trafic în cadrul aceleiași sesiuni,
  printr-un schimb ECDHE semnat nou. Nu este nici o sesiune nouă, nici o
  schimbare de cale.
- **Migrare autentificată a căii:** o sesiune activă poate trece la o adresă
  sau la un port public nou numai după ce mixerul a verificat rutabilitatea de
  retur (return routability).

O sesiune este legată de stația autentificată și identificată printr-un
locator de sesiune emis de mixer, niciodată prin adresa IP și portul sursă.
UDPSEC nu are resetare în clar, mesaj de downgrade sau revenire automată la UDP
simplu; datagramele neautentificate nu pot schimba starea sesiunii. Starea
sesiunilor și a protecției anti-replay este în memorie și locală procesului,
astfel încât repornirea oricăruia dintre capete necesită o sesiune nouă.

Formatul actual al pachetelor UDPSECv2 nu este compatibil cu versiunile
anterioare: actualizați împreună `aismixer` și `nmea_sproxy`.

### 📶 Continuitate mobilă

Stațiile mobile pierd pachete, iar adresa lor IP publică sau portul UDP se pot
schimba. Testele de teren au arătat că un handover celular păstrează adesea
aceeași adresă publică și același port, astfel încât UDPSECv2 tratează o
pierdere temporară diferit de o schimbare reală de adresă:

| Situație | Recuperare |
| --- | --- |
| Pierdere scurtă pe aceeași cale | **Recuperarea liveness.** Stația retrimite, cu o rată limitată, singurul său ping keepalive fără răspuns, cu același număr de secvență și cu o criptare nouă de fiecare dată, până când sosește un răspuns autentificat. Sesiunea continuă; singura limită terminală este `peer_timeout` (implicit 90 s) după ultima dovadă autentificată. |
| Adresă sau port public nou cât timp sesiunea este activă | **Migrare autentificată a căii.** Pachetele de la noua adresă trebuie să se autentifice cu cheile curente ale sesiunii. Mixerul trimite o verificare (challenge) către acea adresă, de cel mult patru ori în total, la cel puțin 2 s distanță, într-o fereastră fixă de 10 s, și își mută răspunsurile la noua adresă numai după ce răspunsul autentificat al stației sosește de la aceeași adresă. Sesiunea, cheile și starea anti-replay sunt păstrate. |
| Întrerupere lungă sau terminală | **Stabilire autentificată nouă.** Odată atinsă limita de liveness, stația efectuează un handshake semnat nou; mixerul elimină singur vechea sesiune inactivă. |

Redirecționarea continuă cât timp liveness-ul este incert, dar UDPSEC nu
păstrează în buffer și nu retransmite niciodată date NMEA: propozițiile
trimise în timpul unei întreruperi se pot pierde chiar și atunci când sesiunea
supraviețuiește, iar nicio sesiune nu supraviețuiește oricărei întreruperi.
Validarea pe teren acoperă până acum recuperarea pe aceeași cale; migrarea
căii este acoperită de teste end-to-end. [Ghidul `nmea_sproxy`](nmea_sproxy/README.md)
explică liniile de jurnal și câmpurile de stare asociate.

## 🗺️ Modele de implementare

| Model | Configurație tipică |
| --- | --- |
| Receptor într-o rețea locală de încredere | receptor → UDP simplu → `aismixer`, restricționat prin `allow_from` și reguli firewall |
| Stație fixă la distanță | receptor serial/USB sau UDP → `nmea_sproxy` → UDPSEC → `aismixer` |
| Stație mobilă pe o navă sau un vehicul | ca mai sus, prin legături celulare sau CGNAT, cu continuitate mobilă |
| Nod de margine pe router | OpenWrt cu `nmea_sproxy` la stație sau cu `aismixer` ca mixer local |
| Mai mulți consumatori | distribuire din `aismixer` sau rute denumite către destinații UDP separate |

`aismixer` și `nmea_sproxy` rulează pe gazde Debian și Raspberry Pi OS cu
systemd și ca pachete OpenWrt 25.12. `nmea_sproxy` poate rula și manual,
inclusiv pe Windows, fără integrare ca serviciu.

## 🚀 Pornire rapidă pe Linux cu systemd

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

Unitatea `aismixer.service` livrată nu setează `User=`/`Group=`, deci rulează
cu privilegiile contului care o pornește -- root, implicit. Operatorii care
doresc izolarea privilegiilor trebuie să creeze ei înșiși un cont de serviciu
dedicat și să adauge directivele `User=`/`Group=` corespunzătoare în unitate.

#### ⚙️ Configurare înainte de prima pornire

```bash
sudoedit /etc/aismixer/config.yaml
sudoedit /etc/aismixer/authorized_keys.yaml
```

Configurația inițială conține listenere asociate unor adrese larg accesibile
și fără liste de adrese permise la nivelul aplicației. Înainte de pornire,
adaptați adresele, porturile, regulile `allow_from`, forwarderele, autorizarea
UDPSEC, regulile firewall ale gazdei și politica de rutare pentru implementarea
concretă. UDP simplu nu oferă niciuna dintre protecțiile UDPSEC —
confidențialitate, autentificare, integritate, anti-replay sau verificări de
liveness —, iar listele de adrese permise la nivelul aplicației completează, nu
înlocuiesc, firewall-ul gazdei.

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
sunt modificate direct. Actualizați stațiile UDPSEC împreună cu mixerul.

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

Pentru a conecta stații aflate la distanță, adăugați un listener UDPSEC și
autorizați fiecare stație (vedeți „Intrare UDPSEC și autorizarea stațiilor”
mai jos), apoi urmați [ghidul operatorului `nmea_sproxy`](nmea_sproxy/README.md)
pe stație.

## 📦 OpenWrt 25.12

OpenWrt este o țintă completă pentru implementări de margine: un router poate
rula `nmea_sproxy` lângă un receptor sau `aismixer` ca mixer local. Pachetele
APK versionate pentru OpenWrt 25.12, integrate cu procd, provin din aceeași
rețetă de pachet:

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

**Starea pachetelor.** Rețeta construiește `0.2.1-r4` din sursa fixată a
versiunii v0.2.1. Aceste pachete sunt anterioare continuității sesiunilor
UDPSECv2 descrise mai sus (reîmprospătarea epocii de chei, migrarea căii și
recuperarea liveness), iar formatul lor de pachete UDPSEC nu este compatibil
cu arborele-sursă actual. Folosiți aceeași linie de versiune la ambele capete
ale unei relații UDPSEC. Pachetele care includ această funcționalitate necesită
o versiune ulterioară, la care rețeta este refixată; verificați revizia
pachetului și [lista de modificări](CHANGELOG.md).

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

Actualizați cu o singură comandă `apk` toate pachetele AISMixer instalate,
din feed-ul configurat pe dispozitiv, deoarece `aismixer` și `nmea_sproxy`
necesită exact aceeași revizie a pachetului `aismixer-common`. Dacă pe
dispozitiv sunt instalate atât `aismixer`, cât și `nmea_sproxy`, rulați
`apk --update-cache add --upgrade aismixer-common aismixer nmea_sproxy`; dacă
este instalat doar unul, specificați `aismixer-common` și pachetul respectiv:
`apk --update-cache add --upgrade aismixer-common aismixer` pe un mixer sau
`apk --update-cache add --upgrade aismixer-common nmea_sproxy` pe o stație.
Nu specificați niciodată un pachet pe care dispozitivul nu îl are: `apk add`
l-ar instala, iar hook-ul său ar activa și ar porni serviciul.
Hook-urile de actualizare opresc și pornesc fiecare serviciu chiar dacă
acesta era oprit anterior, păstrând însă starea sa de
activare/dezactivare. `apk del aismixer` oprește, dezactivează și elimină
pachetul. Pachetul nu are un contract de purge specific proiectului, astfel
încât acest README nu promite păstrarea configurației sau a cheilor după
eliminare.

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
    listen_port: 17778
    allow_from:
      - 192.0.2.0/24

forwarders:
  - id: local_display
    host: 127.0.0.1
    port: 19000
```

Adaptați înainte de utilizare toate adresele, porturile, ID-urile, căile și
politicile din exemple. Exemplele din depozit sunt inactive până când sunt
copiate sau adaptate. Exemplele folosesc portul 17778 pentru UDP simplu și
17779 pentru UDPSEC; acestea sunt valori de exemplu alese de proiect și rămân
configurabile. În sursa actuală, configurațiile inițiale și cele incluse în
pachete folosesc, de asemenea, 17779 pentru UDPSEC; pachetele deja publicate
păstrează configurația versiunii din care au fost construite.

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

### 🔐 Intrare UDPSEC și autorizarea stațiilor

Adăugați un listener securizat și autorizați cheia publică a fiecărei stații
sub `station_id`-ul ei în `/etc/aismixer/authorized_keys.yaml`:

```yaml
sec_inputs:
  - listen_ip: "0.0.0.0"
    listen_port: 17779
```

```yaml
authorized_clients:
  - name: boat_001
    pubkey: <compressed-public-key-base64>
```

`output.host` și `output.port` ale fiecărei stații trebuie să indice acest
listener. Reporniți `aismixer` după modificarea autorizărilor. Transmiteți
fiecărei stații cheia publică a mixerului — în implementările convenționale,
`/etc/aismixer/keys/aismixer_public.pem` — printr-un canal de încredere; ghidul
stației acoperă ambele părți.

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
valorile filtrelor. Filtrul de intrare (`show statistics inputs <SELECTOR>`)
este valoarea exactă din coloana SELECTOR, o identitate stabilă, independentă
de adresă, precum `udp-ingress:0`; coloana INPUT este o etichetă pentru
oameni și nu este folosită la potrivire. Filtrul de ieșire este un nume canonic
exact, precum `udp:local_display`, sau un număr zecimal afișat al destinației,
local procesului. Un filtru fără potrivire returnează o vizualizare goală.

Folosiți `systemctl status aismixer` sau `/etc/init.d/aismixer status` pentru
starea serviciului în implementarea corespunzătoare. Folosiți `help` în
shell-ul interactiv; pentru scripturi sunt disponibile comenzi one-shot
echivalente.

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

## ⚠️ Note de securitate și limitări actuale

UDPSEC autentifică endpoint-urile configurate și protejează conținutul
transportului. Nu stabilește adevărul semantic, originea fizică sau exactitatea
unui raport AIS. Proprietățile de forward secrecy depind de eliminarea
secretelor efemere și de faptul că endpoint-urile nu sunt compromise cât timp
acele secrete sunt active. Protejați cheile private ale stațiilor și ale
mixerului și nu copiați niciodată cheia privată a unei stații pe mixer. UDP
simplu configurat explicit nu primește niciuna dintre proprietățile
criptografice sau de liveness ale UDPSEC; folosiți izolare de rețea, ACL-uri
ale aplicației și politica firewall atunci când transportul simplu este activat
în mod deliberat.

Limitări actuale:

- UDP este singurul adaptor de ieșire al mixerului și rămâne un transport de
  datagrame fără garanții; UDPSEC nu adaugă confirmarea livrării sau
  retransmiterea payload-ului.
- Starea rutării runtime, numerele de generație și statisticile sunt locale
  procesului; modificările live ale rutării nu sunt persistente.
- Sesiunile securizate și înregistrările anti-replay sunt locale procesului și
  nepersistente. Nonce-urile DATA rămân pe durata epocii cheii lor de trafic, în
  loc să expire pe baza unui TTL pentru nonce.
- O sesiune trece la o adresă nouă numai cât timp este activă și noua cale este
  dovedită; după o întrerupere terminală este necesar un handshake nou.
- Controlul local folosește în prezent un socket Unix-domain POSIX și
  permisiunile sistemului de fișiere; nu are token la nivelul aplicației.
- Serviciul nu oferă filtrare geografică sau după conținut MMSI, stocare pe
  termen lung, analiză ori detectarea spoofing-ului/anomaliilor AIS.
- Runtime-ul actual de procesare este Python și local procesului; nu există
  procesor nativ separat, coordonator de workeri, plan de rutare IPC sau agregare
  a statisticilor între procese.
- În general, configurația nu este reîncărcată dinamic. Mutația live acceptată
  este snapshot-ul de rutare local procesului, expus prin controlul local.

Consultați [politica de securitate](SECURITY.md) pentru raportarea
vulnerabilităților.

## 🧭 Starea proiectului și foaia de parcurs

AISMixer este în dezvoltare activă înainte de versiunea 1.0; versiunile
anterioare lui 1.0 pot încă schimba configurația și interfețele. Ultima
versiune etichetată este v0.2.1. Ramura `main` conține și lucrări nelansate,
inclusiv continuitatea sesiunilor UDPSECv2 descrisă mai sus; consultați
[lista de modificări](CHANGELOG.md).

Direcții planificate, dintre care niciuna nu este încă implementată (consultați
[foaia de parcurs](ROADMAP.md)):

- consolidarea suplimentară a implementării operaționale;
- o viitoare arhitectură cu mai multe procese și un procesor nativ în spatele
  contractului de procesor existent;
- operații asupra stării rutării, precum persistența opțională și
  reîncărcarea sigură;
- cercetare privind securitatea maritimă și calitatea datelor, inclusiv
  detectarea spoofing-ului și a anomaliilor AIS;
- adaptoare de ieșire suplimentare și control la distanță autentificat.

## 📚 Documentație, licență și contribuții

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
- [Foaie de parcurs](ROADMAP.md)
- [GitHub Wiki](https://github.com/iliyan85/aismixer/wiki), cu ghiduri
  detaliate de arhitectură și implementare
- [Site web public](https://aismixer.net)

AISMixer este licențiat sub [CC BY-NC 4.0](LICENSE). Consultați
[ghidul pentru contribuții](CONTRIBUTING.md) și
[codul de conduită](CODE_OF_CONDUCT.md) înainte de a deschide issue-uri sau
pull request-uri.

[Înapoi la selectorul de limbă](#languages)
