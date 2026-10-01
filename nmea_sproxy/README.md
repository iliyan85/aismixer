# 🧩 nmea_sproxy — operator guide

`nmea_sproxy` is AISMixer's lightweight station-side proxy. It runs next to an
AIS receiver, reads one local UDP or serial input, and sends the AIS sentences
it finds to a remote `aismixer` service: over UDPSEC, or over plain UDP when
explicitly selected. Deduplication, multipart assembly, TAG handling, and
routing happen at the mixer, not here. For the project overview, see the
[root README](../README.md).

## 🧭 Purpose and relation model

One `nmea_sproxy` process represents one runtime relation:

`one local UDP or serial input → one UDPSEC or plain-UDP network output`

It does not mix inputs, perform aismixer routing or fan-out, deduplicate, or
assemble multipart AIS. Run another process for every independent relation.

## 🚀 Debian/systemd quick start

The conventional installer targets Debian-family systems such as Debian and
Raspberry Pi OS with systemd. It checks Debian packages, installs under
`/opt/nmea_sproxy`, and prepares `/etc/nmea_sproxy/config.yaml`,
`/etc/nmea_sproxy/instances/`, `/etc/nmea_sproxy/keys/`, and both systemd units.

Install dependencies and the proxy from a fresh checkout:

```bash
git clone https://github.com/iliyan85/aismixer
cd aismixer
sudo apt install python3-setproctitle python3-yaml python3-cryptography python3-serial
./nmea_sproxy/install.sh
```

The script runs as root or uses `sudo`, preserves an existing system config,
enables `nmea_sproxy.service`, and starts nothing. It neither generates station
identity nor provisions trust in the mixer.

Complete the following workflow before starting UDPSEC:

1. Review `/etc/nmea_sproxy/config.yaml`, replace example addresses, and set
   `station_id`.
2. Generate the station identity and retain its printed public value.
3. Install the trusted mixer key as
   `/etc/nmea_sproxy/keys/aismixer_public.pem`.
4. Authorize the station public value under the same `station_id` in aismixer.
5. Start the proxy, then inspect status and logs.

Generate a new canonical station identity only where both station identity files
are absent:

```bash
sudo python3 /opt/nmea_sproxy/tools/aismixer_keys.py station \
  --keys-dir /etc/nmea_sproxy/keys \
  --station-id boat_001
```

Replace `boat_001` with the configured ID. The command refuses to overwrite
existing material unless an explicit destructive option is supplied.

After configuration, station identity, mixer trust, and mixer authorization
have all been reviewed:

```bash
sudo systemctl start nmea_sproxy.service
sudo systemctl status nmea_sproxy.service
sudo journalctl -u nmea_sproxy.service -f
```

Do not start incomplete UDPSEC configuration: `Restart=always` can turn a
persistent configuration or trust failure into a restart loop.

## ⚙️ Configuration

### Canonical relation

Use explicit `input:` and `output:` mappings for every new configuration. This
Debian/systemd example accepts one documented station address and sends UDPSEC
to one mixer:

```yaml
input:
  type: udp
  listen_ip: "192.0.2.20"
  listen_port: 50000
  allow_from:
    - 192.0.2.15

output:
  type: udpsec
  host: mixer.example.net
  port: 17779
  # source_ip: 192.0.2.20

station_id: boat_001
reconnect_delay: 5
keepalive_interval: 30
peer_timeout: 90
session_refresh_interval: 0

station_private_key: /etc/nmea_sproxy/keys/station_private.pem
remote_public_key: /etc/nmea_sproxy/keys/aismixer_public.pem
```

Replace all documentation addresses; `output.port` must match the mixer's
`sec_inputs` listener. The examples use mixer port 17779 for UDPSEC and 17778
for plain UDP; both are project-chosen example values and remain configurable.
[`config.yaml`](config.yaml) is for checkout/manual use, while
[`config.system.yaml`](config.system.yaml) seeds the system config; until the
next release cleanup, both templates may still use an older example port.

### Settings and defaults

| Setting | Default | Meaning |
| --- | --- | --- |
| `input.type` | required | `udp` or `serial` (see "Input modes") |
| `output.type` | required | `udpsec` or `udp` |
| `output.host`, `output.port` | required | destination, resolved once at startup |
| `output.source_ip` | OS choice | optional literal local address; fixes the address family |
| `station_id` | `boat_001` | station name; must match the mixer's authorization entry |
| `station_private_key` | `/etc/nmea_sproxy/keys/station_private.pem` | station identity (UDPSEC) |
| `remote_public_key` | `/etc/nmea_sproxy/keys/aismixer_public.pem` | trusted mixer key (UDPSEC) |
| `keepalive_interval` | `30` | seconds between keepalive pings (UDPSEC) |
| `peer_timeout` | `90` | seconds without authenticated evidence before a fresh handshake (UDPSEC) |
| `session_refresh_interval` | `0` | seconds between in-session key epoch refreshes; `0` disables them (UDPSEC) |
| `reconnect_delay` | `5` | seconds to wait after a failed handshake, a socket error, or a peer close before the next attempt |

Timing values are finite numbers of seconds, never booleans or numeric
strings. `keepalive_interval` and `peer_timeout` must be above zero;
`session_refresh_interval` and `reconnect_delay` may be zero. No ratio between
them is enforced. Plain UDP uses only `reconnect_delay`.

### Configuration selection and legacy migration

Configuration is selected in this order:

1. `--config PATH`;
2. `NMEA_SPROXY_CONFIG`;
3. `/etc/nmea_sproxy/config.yaml`;
4. `config.yaml` beside `nmea_sproxy.py`;
5. built-in defaults.

An explicitly selected CLI or environment path must exist. Relative
`station_private_key`, `remote_public_key`, and legacy
`aismixer_public_key` paths resolve from the directory containing the selected
YAML file, not the current working directory.

For compatibility, omitting `input` selects the legacy top-level UDP-input form
and omitting `output` selects UDPSEC; old endpoint, ACL, and source-address
fields remain accepted with deprecation notices. Do not use legacy syntax for
new deployments: explicit mappings do not borrow missing deprecated values.
`log_level` is not a functional runtime logging filter.

## 📡 Input modes

The input adapters are `udp` and `serial`. USB virtual serial uses the operating
system's serial-device abstraction, not a separate proxy USB stack.

### UDP

UDP input requires `listen_ip` and `listen_port`. Optional `allow_from` entries
are literal IP addresses or CIDR networks; hostnames are rejected. Omission
permits every application-level source, while `[]` denies every datagram.
This filtering is not authentication and does not replace firewall policy.

Receives are bounded to 4096 bytes. Each datagram is scanned for every substring
matching the proxy's current NMEA syntax.

### Serial and USB virtual serial

A serial mapping passes `port` to pySerial unchanged. Examples are
`/dev/ttyUSB0`, `/dev/ttyACM0`, `/dev/serial/by-id/...`, and Windows `COM3`.

```yaml
input:
  type: serial
  port: /dev/ttyUSB0
  baudrate: 38400
  bytesize: 8
  parity: N
  stopbits: 1
  read_timeout: 1.0
  reconnect_delay: 5
  max_line_bytes: 4096
```

The values shown are the defaults; CR, LF, and CRLF terminate lines.
Device/read failure clears partial framing and retries the same path. Overlong
input is discarded through its next delimiter.

The reader queue holds 256 complete lines. When full, it drops the oldest line,
keeps fresher traffic, and logs the drop; serial input is not lossless.
Manual use needs device permission, OpenWrt needs the correct USB-serial driver,
and two relations must not compete for one device. Prefer stable device names.

### NMEA extraction and payload semantics

Each datagram or completed serial line is scanned for `!<talker>VDM` or
`!<talker>VDO` substrings ending in an uppercase `*HH` checksum-shaped suffix,
where `HH` represents two uppercase hexadecimal characters. The accepted
talker IDs are the same AIS whitelist aismixer core supports: `AI`, `AB`,
`AD`, `AN`, `AR`, `AS`, `AT`, `AX`, and `BS`. The proxy checks sentence syntax
only: it does not calculate the checksum value.

Every match is forwarded independently. Multipart fragments are not assembled;
nonmatching material is silently discarded. The bare match is forwarded, so
ingress TAG blocks, prefixes, surrounding bytes, and terminators are removed;
the mixer applies its own TAG policy.

## 📤 Output modes and endpoint controls

The only output types are protected `udpsec` and explicit plain `udp`.

Modern output mappings require `host` and `port`. Optional `source_ip` must be a
literal address and fixes the family. Without it, an IPv6 literal selects IPv6;
all other destinations, including hostnames, resolve as IPv4.

One address is selected at startup and pinned for the process lifetime. Socket
recreation reuses it, so DNS changes require restart. The OS allocates the
source port; fixed source ports and interface/routing selection are unsupported.

### UDPSEC

`output.type: udpsec` activates identity, trust, authenticated handshake,
encryption, replay protection, and liveness. Each bare NMEA match travels as
one authenticated, encrypted DATA message of the station's session. Invalid
identity or trust prevents activation.

### Explicit plain UDP

> **Warning:** Plain UDP is unauthenticated and unencrypted. Use it only inside
> a controlled LAN, VPN, or another boundary that supplies the required
> protection.

```yaml
output:
  type: udp
  host: 192.168.10.20
  port: 17778
  # source_ip: 192.168.10.15
```

Each bare match becomes one datagram; removed TAG/prefix material is not
restored, and no JSON envelope or authenticated `station_id` is sent.
aismixer must derive source identity from its own plain-UDP listener policy.

## 🪪 Identity and trust

Keep station identity (`station_private.pem` plus `station_public.pem`), trust
in the mixer (`aismixer_public.pem`), and aismixer's authorization mapping from
`station_id` to station public value separate. Repository example public keys
are not deployment trust material. Protect `station_private.pem`; never copy it
to aismixer.

### Station identity

For the canonical system key directory, the key tool creates a P-256 pair and
prints the compressed public value used by aismixer (Debian/systemd command in
the quick start above). On OpenWrt:

```sh
python3 /usr/lib/aismixer/tools/aismixer_keys.py station \
  --keys-dir /etc/nmea_sproxy/keys --station-id boat_001
```

Canonical UDPSEC activation may create the identity only when both canonical
files are absent; pre-generation permits authorization before service start.
A valid matching pair is preserved. A missing member, malformed/non-P-256
material, or mismatch fails closed without mutation or automatic repair.

To derive the public mate from a known-good intended private identity:

```bash
sudo python3 /opt/nmea_sproxy/tools/aismixer_keys.py station \
  --keys-dir /etc/nmea_sproxy/keys \
  --station-id boat_001 \
  --repair-public
```

Replacing the private key changes identity and requires new authorization.
A selected custom or legacy private path must already hold a usable key;
runtime neither invents its public filename nor generates or repairs it.

### Trust the mixer and authorize the station

Obtain the intended mixer public key through an authenticated channel and copy
it to `remote_public_key`. Every handshake reply must carry a valid signature
by that key; otherwise the proxy rejects it and never falls back. Proxy
tooling and lifecycle scripts never generate, download, exchange, replace, or
repair this trust key.

Add the station public value printed by the key tool to aismixer's
`authorized_keys.yaml`, normally `/etc/aismixer/authorized_keys.yaml`:

```yaml
authorized_clients:
  - name: boat_001
    pubkey: <compressed-public-key-base64>
```

The name must match `station_id`. Restart aismixer after authorization changes,
then start the proxy. Plain UDP skips identity/trust handling.

## 🔐 UDPSECv2 sessions

### Establishment

The proxy sends one signed ClientHello with its `station_id`, a timestamp,
and a fresh ephemeral P-256 key, then waits up to 5 seconds for the mixer's
signed reply. It verifies that reply against `remote_public_key`, derives
separate AES-256-GCM traffic keys for each direction, and confirms them with
an encrypted ping that the mixer answers (`Mutual ECDHE session confirmed.`).
The mixer, in turn, accepts only an authorized station public key. Handshake
timestamps must be within 30 seconds of the mixer's clock. A failed attempt
waits `reconnect_delay` before the next one.

Every DATA packet is bound to its session, identified by a mixer-issued
session locator, and to its traffic-key epoch. Each receiver keeps every
admitted nonce for the epoch's lifetime and rejects replays; if the current
epoch's bounded nonce ledger fills, that epoch fails closed and a fresh
handshake follows. State is in memory only: restarting either end requires a
fresh session.

### Liveness recovery on the same path

After confirmation the proxy sends an encrypted keepalive ping whenever
`keepalive_interval` passes with no ping outstanding, and the mixer answers
each with a pong. At most one logical ping is outstanding. If it is still
unanswered at its keepalive deadline, the proxy retransmits the same ping --
same sequence number, fresh encryption with a fresh nonce -- and then again
once per retry interval, which is 5 seconds or `keepalive_interval`, whichever
is shorter, until it is answered. NMEA forwarding continues meanwhile.

Only when no qualifying authenticated evidence -- a matching pong, the first
verified reply or the commit of an in-session epoch refresh, or a matched
path-migration acknowledgement -- has arrived for `peer_timeout` does the proxy
end the session and start a fresh signed handshake. With the defaults, one lost
ping or pong costs one retransmission about 60 seconds after the last
evidence, answered in the same session; a peer silent for 90 seconds triggers
fresh establishment. Plaintext, forged, mismatched, and ICMP-derived signals
are never evidence.

A momentary local network error while an interface switches -- "Network is
unreachable" or another missing route, a network or host down, a vanished
source address, buffer pressure, or an ICMP-derived refusal or reset -- does
not end the session by itself. A sentence whose send failed is dropped, not
resent; reading local input then pauses for one retry interval, and a
keepalive transmission re-probes the path before input resumes. A long outage
still ends at the `peer_timeout` bound. Any other socket error ends the session
and waits `reconnect_delay`.

### Path migration

When the station's public address or port changes while the session is alive
-- for example after a NAT or CGNAT rebinding -- the mixer decides whether to
migrate; the proxy never moves the mixer's path itself. Authenticated
current-epoch packets from the new address make it a candidate, and the mixer
sends an encrypted path challenge there: at most four times in total, at least
2 seconds apart, within a fixed 10-second window. The proxy answers each valid
challenge with an encrypted path response. Only when a response arrives from
the candidate address does the mixer make it the active path and confirm with
a path acknowledgement. Session, keys, and replay state are unchanged.

A challenge is not liveness evidence. When the proxy answers one, it records a
short-lived proof that includes the keepalive ping outstanding at that moment,
if any. A matching acknowledgement counts as fresh liveness evidence as of the
instant it is read, exactly like an accepted pong; it clears only the ping
captured in that proof, and only if it is still outstanding. It never resets
the session age, postpones a planned refresh, or restarts the keepalive
schedule. Until the mixer commits, pings sent from the new address get no
pong and stay outstanding; after the commit, pongs reach the new address.

Migration needs a live session on both ends. If the proxy has already reached
its `peer_timeout` bound, or the mixer has expired the idle session, the next
step is a fresh handshake.

### Key epoch refresh

With `session_refresh_interval` above zero, the proxy periodically renews the
traffic keys inside the same session: a signed exchange with fresh ephemeral
ECDHE keys (REFRESH_INIT, REPLY, CONFIRM, ACK) derives a new key epoch while
forwarding continues under the current one. The proxy switches only after an
acknowledgement authenticated under the new epoch, and abandons a refresh that
has not completed within 15 seconds, keeping the current epoch. Each control
retransmission re-encrypts the same signed message under a fresh nonce, at
most 6 attempts per phase and at least 2 seconds apart, and every send is
checked against the transaction deadline immediately before it leaves.

A refresh is neither a new session nor a path change: the session locator,
the active path, and the forwarding loop are unchanged. Zero disables planned
refresh; a liveness failure still leads to a fresh full handshake.

### Mobile behaviour

| Event | What happens | Session |
| --- | --- | --- |
| Lost ping or pong, a short two-way outage, or an interface switch that keeps the public address | the outstanding ping is retransmitted; sentences whose send failed are dropped | kept if authenticated evidence returns within `peer_timeout` |
| New public address or port while the session is alive | forwarding continues; the proxy answers the mixer's path challenges | kept; the mixer moves its active path after proof |
| Outage longer than `peer_timeout`, a mixer restart, or an expired session | a fresh signed handshake, with `reconnect_delay` between failed attempts | new session |

### No fallback and compatibility

UDPSEC has no plaintext `NOSESSION`, plaintext reset, downgrade control, or
automatic fallback to plain UDP. Unauthenticated control-looking datagrams do
not alter session state. UDP remains lossy: AIS payloads are not buffered,
retransmitted, or replayed during handshake or recovery, so UDPSEC does not
guarantee delivery.

The current UDPSECv2 wire format does not interoperate with earlier builds,
including the 0.2.1 OpenWrt packages: upgrade `nmea_sproxy` and `aismixer`
together. Exact confirmation, admission, migration, refresh, and recovery
rules are normative in the [Behavioural Contract](../BEHAVIORAL_CONTRACT.md).

## 🎛️ Services and instances

Every supervised or manual process owns one runtime relation.

### systemd singleton

`nmea_sproxy.service` uses `/etc/nmea_sproxy/config.yaml`; installation enables
but does not start it. In a named-only deployment, run
`sudo systemctl disable --now nmea_sproxy.service`.
The root-running unit uses `Restart=always` with a five-second delay; stop it
before repairing persistent configuration, identity, or trust failures.

### systemd named instances

`nmea_sproxy@<name>.service` reads:

`/etc/nmea_sproxy/instances/<name>.yaml`

Choose an unused instance name and edit its listener/device before starting so
it cannot collide with another relation:

```bash
sudo cp /etc/nmea_sproxy/config.yaml /etc/nmea_sproxy/instances/boat.yaml
sudoedit /etc/nmea_sproxy/instances/boat.yaml
sudo systemctl enable --now nmea_sproxy@boat.service
sudo systemctl status nmea_sproxy@boat.service
sudo journalctl -u nmea_sproxy@boat.service -f
```

Names are operator labels, not protocol identities: any label works, not a
numbered or sequential scheme. `nmea_sproxy@yacht.service` (a second boat) and
`nmea_sproxy@balchik_roof.service` (a fixed shore station) are equally valid
alongside `nmea_sproxy@boat.service` above. Instances may coexist only
with distinct UDP listeners or serial devices. systemd does not centrally
preflight collisions; a conflict remains a runtime failure and may restart-loop.

### Manual and Windows operation

From the component directory, select the local template or another explicit
configuration:

```bash
cd nmea_sproxy
python3 nmea_sproxy.py --config config.yaml
```

For Windows, install dependencies, inspect ports, and pass the intended config:

```powershell
py -m pip install pyserial pyyaml cryptography
py -m serial.tools.list_ports
py nmea_sproxy.py --config config.yaml
```

Use a reported name such as `COM3`; no Windows Service integration is supplied.

## 🍓 Raspberry Pi and edge hosts

The Debian/systemd installer suits Raspberry Pi OS. On small edge hosts:

- Synchronise the clock (NTP) before UDPSEC starts. A board without a
  real-time clock can boot far from real time, and a handshake outside the
  30-second window then fails as if the mixer were unreachable.
- Prefer `/dev/serial/by-id/...` paths, and attach each receiver to exactly
  one relation; run a named instance for each additional receiver.
- The 256-line serial queue drops the oldest lines when full, so a fast feed
  can lose lines during an input pause after a local send error.
- Cellular hotspots and CGNAT work through keepalive and mobile continuity;
  keep plain UDP for a trusted LAN or VPN.

## 📦 OpenWrt

The OpenWrt recipe produces `aismixer-common`, `aismixer`, and `nmea_sproxy`.
Its Python/shell payload declares `PKGARCH:=all`, but portability still depends
on target Python, cryptography, serial, and related packages.

The currently built, published, and validated repository feed targets include
`x86_64` and `mips_24kc`; this does not intentionally exclude other targets with
suitable dependencies. The package recipe pins a source revision, so packaged
behavior may lag current `main`: it currently builds `0.2.1-r4` from the
v0.2.1 release, which predates the UDPSECv2 session behaviour described above
and does not interoperate with current source-tree builds. Pair packages only
with a mixer from the same release line, and check the
[root README](../README.md) and [changelog](../CHANGELOG.md).

### Installation and first configuration

After configuring a supported AISMixer package feed, verify writable overlay
space and establish firewall or network isolation: generated package hooks
enable and attempt to start the service during `apk add`, before operator
review. Install, stop the service, configure it, provision station identity and
mixer trust, authorize the station at aismixer, then start:

```sh
apk -U add nmea_sproxy
/etc/init.d/nmea_sproxy stop
vi /etc/nmea_sproxy/config.yaml
# Provision /etc/nmea_sproxy/keys before the next start.
/etc/init.d/nmea_sproxy start
/etc/init.d/nmea_sproxy status
logread -e nmea_sproxy
```

The seeded UDP listener is broad. The singleton uses UDPSEC, and a fresh install
normally lacks mixer trust and cannot pass preflight until it is provisioned.
Installation alone does not create a ready relation.

Only `/etc/nmea_sproxy/config.yaml` receives package conffile treatment.
Operator-created named configurations and keys are not declared package
conffiles.

### Named procd relations

The singleton reads `/etc/nmea_sproxy/config.yaml`. Every regular
`/etc/nmea_sproxy/instances/*.yaml` file whose stem matches
`[A-Za-z0-9][A-Za-z0-9_.-]*` becomes a named procd relation. Choose an unused
name and unique listener/device before copying:

```sh
mkdir -p /etc/nmea_sproxy/instances
cp /etc/nmea_sproxy/config.yaml /etc/nmea_sproxy/instances/boat.yaml
vi /etc/nmea_sproxy/instances/boat.yaml
/etc/init.d/nmea_sproxy restart
ubus call service list '{"name":"nmea_sproxy"}'
logread -e nmea_sproxy
```

Adding, deleting, or renaming a named file requires an init-service restart;
there is no systemd-style template enable command. Avoid the name `instance1`
while a singleton relation is active because procd uses that internal name for
the unnamed singleton.

Each relation is preflighted independently. Valid relations can start while
invalid configurations or unusable trust skip only their relation. Startup
fails when no configured relation is valid. Preflight validates configuration
and required UDPSEC identity/trust; it does not prove that a UDP address can be
bound or that a serial device is exclusively available.

### Serial and storage prerequisites

Install the USB-serial kernel package for the receiver chipset and confirm the
configured device path exists. Python, cryptography, pySerial, and their
dependencies need materially more writable space than a minimal router image;
extroot may be appropriate on constrained devices. Detailed feed, hardware, and
storage procedures belong in the
[OpenWrt deployment guide](https://github.com/iliyan85/aismixer/wiki/OpenWrt-Deployment).

## 🔄 Update and uninstall

On Debian/systemd, `update.sh` refreshes the installed runtime, key helper, and
unit files and reloads systemd. It does not change `/etc/nmea_sproxy`
configuration or keys, restart any unit, or start an inactive relation.

```bash
git pull --ff-only
./nmea_sproxy/update.sh
sudo systemctl restart nmea_sproxy.service
sudo systemctl status nmea_sproxy.service
```

Restart only the singleton or named instances chosen by the operator, and
update the mixer in the same maintenance window.

`./nmea_sproxy/uninstall.sh` stops and disables proxy units, removes the
installed runtime and unit files, and preserves `/etc/nmea_sproxy`.

`./nmea_sproxy/uninstall.sh --purge-config` additionally deletes that entire
directory, including operator configurations and keys. Use it only when that
destructive result is intended.

OpenWrt package lifecycle is separate. `apk --update-cache add --upgrade
nmea_sproxy` updates it; the generated upgrade hook stops and starts the service
even if it had been manually stopped, while preserving the boot enable/disable
state. `apk del nmea_sproxy` stops, disables, and removes it. There is no
project-specific purge contract, so do not assume configurations, named
relations, or keys will be retained after removal.

## 🩺 Logs and diagnostics

Read logs with `journalctl -u nmea_sproxy.service` (or the named unit) on
systemd, and `logread -e nmea_sproxy` on OpenWrt. The proxy does not trace
every forwarded sentence. Roughly every 60 seconds it prints one `Runtime:`
line with the input and output modes and cumulative forwarded counts,
independent of `log_level` and traffic volume. For UDPSEC it adds the session
state:

```text
Runtime: input=serial output=udpsec forwarded=640 messages / 51.20KiB session=8fb44452 epoch=0 peer=alive observed=192.0.2.10:41000 path_gen=0 age=28s
```

`session` is a short label of the session locator, and `epoch` the
traffic-key epoch, which rises with each in-session refresh. `peer` is the
local liveness state, not a confirmation from the mixer. `observed` is the
public address and port the mixer saw on the last answered ping (`ipv4:port`
or `ipv6.port`, or `unknown`), and `age` the seconds since that observation.
`path_gen` is the mixer's committed path-migration generation for the session:
`0` before any migration, `unknown` when not reported, and marked `last-known`
when older than an acknowledged migration. A fresh handshake shows a new
`session` label with `epoch=0 path_gen=0`; a migration keeps the label and
raises `path_gen`. These fields are observational only: nothing displayed
controls liveness, migration, or keys.

| Log line | Meaning |
| --- | --- |
| `Mutual ECDHE session confirmed.` | a new session is established |
| `Secure session liveness suspect: keepalive ping #N ...` | first retransmission of an unanswered ping |
| `Secure session liveness recovered: keepalive ping #N answered ...` | answered in the same session |
| `Secure ... failed transiently (...); keeping the session ...` | local network error inside the liveness bound |
| `Secure session network path usable again after N transient failure(s).` | first successful send after it |
| `Secure session path migration acknowledged by peer.` | the mixer committed a path migration |
| `Starting in-session authenticated epoch refresh.` | a key epoch refresh began |
| `Secure epoch refresh committed; continuing on the new epoch ...` | the refresh completed in the same session |
| `Secure session liveness unresolved; starting authenticated re-handshake.` | `peer_timeout` reached with a ping outstanding |
| `⚠️ No response from server during handshake.` | no signed reply within 5 seconds |

On the mixer, migration appears as `[+] Path candidate OPENED`,
`[+] Path challenge RETRY n/4`, `[+] Path migration COMMITTED ... old=... new=...`,
and `[+] Path candidate EXPIRED` lines, which never contain keys, nonces, or
challenge tokens.

## 🛠️ Troubleshooting

| Symptom | Checks |
| --- | --- |
| No handshake response | Check the mixer's `sec_inputs` listener and port, bidirectional UDP firewall/NAT rules, the output endpoint and address family, station authorization, and both clocks (30-second window). |
| Server signature verification failed | Confirm `remote_public_key` contains the intended mixer's P-256 public key and the output endpoint is correct. Never bypass verification. |
| Unauthorized station | Match `station_id` exactly and install the station public value in aismixer's `authorized_keys.yaml`; restart aismixer after changes. |
| Identity startup failure | Stop a restarting unit; inspect both canonical station files. Repair only when the retained private key is known to be correct. |
| "Network is unreachable" or `failed transiently` | The local interface or route is missing, for example during a Wi-Fi or cellular switch. Short episodes keep the session; a persistent one ends at `peer_timeout`, and `Handshake send error` then repeats each reconnect cycle until the network returns. |
| `liveness suspect` without `recovered` | Pongs are not arriving: check return-path firewall and NAT state and the mixer's reachability. Without qualifying authenticated evidence the session ends at `peer_timeout` with a fresh handshake. |
| Repeated re-handshakes | Inspect bidirectional reachability, NAT timeout/rebinding, keepalive and peer-timeout values, and both endpoint logs. A re-handshake follows only `peer_timeout` without qualifying authenticated evidence. |
| `observed` changed | A change with a raised `path_gen` and the same `session` label is a completed migration; a change with a new label and `path_gen=0` followed a fresh handshake. |
| Mixer logs candidate `OPENED` then `EXPIRED` without `COMMITTED` | Challenges or responses for the new address are lost: check that return traffic reaches the station's new address. Later traffic opens a new candidate; if no proof succeeds, the session ends at `peer_timeout`. |
| Bind or address error | Check address-family agreement, local address ownership, duplicate listeners, and port availability. |
| Serial device unavailable | Check the configured path, OS permission, USB-serial driver, physical connection, and competing processes. |
| No network output | Confirm matching NMEA input, output type, pinned destination, routing, firewall, and plain-UDP consumer or UDPSEC session state. |
| Restart loop | Stop the unit, run the process manually with its exact config if safe, correct the persistent error, then start it again. |

## ⚠️ Limitations and security notes

Current operator-visible limits:

- one input-to-output relation per process, with no mixing, routing, fan-out,
  deduplication, or multipart assembly;
- checksum-shaped syntax checking without checksum arithmetic verification;
- ingress TAG blocks, prefixes, and surrounding material stripped;
- lossy UDP delivery without payload buffering or retransmission;
- one destination resolution pinned until process restart, and an
  automatically allocated outbound source port;
- bounded serial queue that discards the oldest entry when full;
- process-local, non-durable UDPSEC session and replay state;
- a public address or port change keeps the session only while it is alive
  and the mixer has proven the new path; otherwise a fresh handshake follows.

Security notes:

- Protect `station_private.pem` and its key directory; never copy it to the
  mixer. Replacing it changes the station's identity.
- The public IP address and port are not identity: a new address gains nothing
  without authentication and the mixer's return-routability proof.
- UDPSEC provides confidentiality, cryptographic integrity, and peer
  authentication between configured endpoints. It does not establish the
  semantic truth of AIS reports, the physical truth of vessel positions, or
  the accuracy of transmitted AIS content.
- `input.allow_from` is an application filter, not peer authentication.
- Plain UDP is an explicit non-secure mode without UDPSEC's protections or
  liveness; UDPSEC never falls back to it automatically.

## 📚 Further documentation

- [AISMixer project and deployment overview](../README.md)
- [Normative Behavioural Contract](../BEHAVIORAL_CONTRACT.md)
- [Security policy and vulnerability reporting](../SECURITY.md)
- [Release history and compatibility notes](../CHANGELOG.md)
- [OpenWrt deployment guide](https://github.com/iliyan85/aismixer/wiki/OpenWrt-Deployment)
- [Project Wiki](https://github.com/iliyan85/aismixer/wiki)
