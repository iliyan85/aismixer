# 🧩 nmea_sproxy — operator guide

`nmea_sproxy` is the station-side bridge between one local AIS/NMEA input and
one network output. It can read UDP or serial input and send either protected
UDPSEC traffic or explicitly selected plain UDP.

This guide covers normal installation, configuration, identity and trust,
service operation, OpenWrt deployment, and troubleshooting. For the project
overview, see the [root README](../README.md).

## 🧭 Purpose and relation model

One `nmea_sproxy` process represents one runtime relation:

`one local UDP or serial input → one UDPSEC or plain-UDP network output`

It does not mix inputs, perform AISMixer routing or fan-out, deduplicate, or
assemble multipart AIS. Run another process for every independent relation.
A systemd template name or procd instance name identifies a supervised process;
it is service-manager configuration, not a UDPSEC concept.

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
4. Authorize the station public value under the same `station_id` in AISMixer.
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
  port: 17777
  # source_ip: 192.0.2.20

station_id: boat_001
reconnect_delay: 5
keepalive_interval: 30
peer_timeout: 90
session_refresh_interval: 0

station_private_key: /etc/nmea_sproxy/keys/station_private.pem
remote_public_key: /etc/nmea_sproxy/keys/aismixer_public.pem
```

Replace all documentation addresses. The `output.source_ip` line is optional;
when omitted, the operating system selects the outbound source address.

The shown timing values are the defaults; zero `session_refresh_interval`
disables planned refresh. [`config.yaml`](config.yaml) is for checkout/manual
use, while [`config.system.yaml`](config.system.yaml) seeds the system config.

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
and omitting `output` selects UDPSEC. Old endpoint, ACL, and source-address
fields remain accepted with deprecation notices.

Do not use legacy syntax for new deployments. Explicit mappings do not borrow
missing deprecated values. `log_level` is not a functional runtime logging
filter and is not documented as a supported control.

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

Defaults are 38400 baud, 8-N-1, a one-second read timeout, five-second reconnect,
and a 4096-byte line limit; CR, LF, and CRLF terminate lines. Device/read failure
clears partial framing and retries the same path. Overlong input is discarded
through its next delimiter.

The reader queue holds 256 complete lines. When full, it drops the oldest line,
keeps fresher traffic, and logs the drop; serial input is not lossless.
Manual use needs device permission, OpenWrt needs the correct USB-serial driver,
and two relations must not compete for one device. Prefer stable device names.

### NMEA extraction

Each datagram or completed serial line is scanned for uppercase `!AIVDM` or
`!AIVDO` substrings ending in an uppercase `*HH` checksum-shaped suffix, where
`HH` represents two uppercase hexadecimal characters. The proxy checks that
syntax only: it does not calculate the checksum value or accept the mixer's
broader talker set.

Every match is forwarded independently. Multipart fragments are not assembled;
nonmatching material is silently discarded. The bare match is forwarded, so
ingress TAG blocks, prefixes, surrounding bytes, and terminators are removed.

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
encryption, replay protection, and liveness. The bare NMEA match becomes its
protected DATA payload. Invalid identity or trust prevents activation.

### Explicit plain UDP

> **Warning:** Plain UDP is unauthenticated and unencrypted. Use it only inside
> a controlled LAN, VPN, or another boundary that supplies the required
> protection.

```yaml
output:
  type: udp
  host: 192.168.10.20
  port: 17777
  # source_ip: 192.168.10.15
```

Each bare match becomes one datagram; removed TAG/prefix material is not
restored, and no JSON envelope or authenticated `station_id` is sent.
AISMixer must derive source identity from its own plain-UDP listener policy.

## 🪪 Identity and trust

Keep station identity (`station_private.pem` plus `station_public.pem`), trust
in the mixer (`aismixer_public.pem`), and AISMixer's authorization mapping from
`station_id` to station public value separate. Repository example public keys
are not deployment trust material. Protect `station_private.pem`; never copy it
to AISMixer.

### Station identity

For the canonical system key directory, the key tool creates a P-256 pair and
prints the compressed public value used by AISMixer:

```bash
# Debian/systemd
sudo python3 /opt/nmea_sproxy/tools/aismixer_keys.py station \
  --keys-dir /etc/nmea_sproxy/keys --station-id boat_001

# OpenWrt
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
it to `remote_public_key`. Proxy tooling and lifecycle scripts never generate,
download, exchange, replace, or repair this trust key.

Add the station public value printed by the key tool to AISMixer's
`authorized_keys.yaml`, normally `/etc/aismixer/authorized_keys.yaml`:

```yaml
authorized_clients:
  - name: boat_001
    pubkey: <compressed-public-key-base64>
```

The name must match `station_id`. Restart AISMixer after authorization changes,
then start the proxy. Plain UDP skips identity/trust handling.

## 🔐 UDPSEC behavior and recovery

UDPSEC authenticates the configured long-term P-256 station identity and
trusted mixer identity, binds them to fresh signed ephemeral P-256 ECDHE
material, derives separate client-to-server and server-to-client AES-256-GCM
keys, and confirms key possession over the encrypted channel.

DATA, ping, pong, and best-effort close messages are authenticated and
encrypted. Close is unacknowledged. Keepalive ping/pong traffic provides
liveness and helps retain NAT, CGNAT, and mobile-network UDP mappings.

An unresolved liveness failure starts a fresh signed handshake. Optional
`session_refresh_interval` uses the same authenticated refresh path; zero
disables planned refresh. Session and replay state are in-memory and
non-durable, so process restart establishes a fresh session.

A receiver remembers every admitted DATA nonce for the full usable directional
traffic-key epoch. Those nonce records have no independent TTL and are not
evicted while that epoch remains usable. The bounded replay ledger fails closed
for the affected epoch when exhausted; recovery uses a fresh authenticated
handshake and fresh directional keys.

Exact confirmation, nonce admission, exhaustion, and session-transition rules
are normative in the [Behavioural Contract](../BEHAVIORAL_CONTRACT.md).

NAT works while the server-observed source address and port remain usable and
stable. Rebinding, changing networks, or changing the source port requires a
fresh handshake. There is no automatic session migration.

UDPSEC has no plaintext `NOSESSION`, plaintext reset, downgrade control, or
automatic fallback to plain UDP. Unauthenticated control-looking datagrams do
not alter session state.

UDP remains lossy. AIS payloads are not buffered, retransmitted, or replayed
during handshake or recovery, so UDPSEC does not guarantee delivery.

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

Names are operator labels, not protocol identities. Instances may coexist only
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

## 📦 OpenWrt

The OpenWrt recipe produces `aismixer-common`, `aismixer`, and `nmea_sproxy`.
Its Python/shell payload declares `PKGARCH:=all`, but portability still depends
on target Python, cryptography, serial, and related packages.

The currently built, published, and validated repository feed targets include
`x86_64` and `mips_24kc`; this does not intentionally exclude other targets with
suitable dependencies. The package recipe pins a source revision, so packaged
behavior may lag current `main`. Check the [root README](../README.md) and
[changelog](../CHANGELOG.md) for the applicable package revision.

### Installation and first configuration

After configuring a supported AISMixer package feed, verify writable overlay
space and establish firewall or network isolation before installation. Generated
package hooks enable and attempt to start the service during `apk add`, so an
initial start can precede operator review.

Install, stop the automatically started service, configure it, provision
station identity and mixer trust, authorize the station at AISMixer, then start:

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
fails when no configured relation is valid.

Preflight validates configuration and required UDPSEC identity/trust. It does
not prove that a UDP address can be bound or that a serial device is exclusively
available.

### Serial and storage prerequisites

Install the USB-serial kernel package appropriate for the receiver chipset and
confirm the configured device path exists. Prefer a stable path when OpenWrt
provides one, and do not assign one device to multiple relations.

Python, cryptography, pySerial, and their dependencies need materially more
writable space than a minimal router image. Verify overlay capacity before
installation; extroot may be appropriate on constrained devices. Detailed feed,
hardware, and storage procedures belong in the
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

Restart only the singleton or named instances chosen by the operator.

`./nmea_sproxy/uninstall.sh` stops and disables proxy units, removes the
installed runtime and unit files, and preserves `/etc/nmea_sproxy`.

`./nmea_sproxy/uninstall.sh --purge-config` additionally deletes that entire
directory, including operator configurations and keys. Use it only when that
destructive result is intended.

OpenWrt package lifecycle is separate. Update with
`apk --update-cache add --upgrade nmea_sproxy`. Its generated upgrade hook stops
and starts the service even if it had been manually stopped, while preserving
the boot enable/disable state.

Remove the OpenWrt package with `apk del nmea_sproxy`. Removal stops and
disables the service. There is no project-specific purge contract, so do not
assume configurations, named relations, or keys will be retained after removal.

## 🩺 Status, logs, and troubleshooting

For systemd, use `sudo systemctl status nmea_sproxy.service` and
`sudo journalctl -u nmea_sproxy.service -f`; substitute
`nmea_sproxy@boat.service` for a named relation. On OpenWrt, use
`/etc/init.d/nmea_sproxy status`, `ubus call service list
'{"name":"nmea_sproxy"}'`, and `logread -e nmea_sproxy`.

Common symptoms:

| Symptom | Checks |
| --- | --- |
| Server signature verification failed | Confirm `remote_public_key` contains the intended mixer's P-256 public key and the output endpoint is correct. Never bypass verification. |
| No handshake response | Check the mixer UDPSEC listener, bidirectional UDP firewall/NAT rules, endpoint, address family, station authorization, and clocks. |
| Unauthorized station | Match `station_id` exactly and install the station public value in AISMixer's `authorized_keys.yaml`; restart AISMixer after changes. |
| Repeated reconnects | Inspect bidirectional reachability, NAT timeout/rebinding, keepalive and peer-timeout values, and both endpoint logs. |
| Identity startup failure | Stop a restarting unit; inspect both canonical station files. Repair only when the retained private key is known to be correct. |
| Bind or address error | Check address-family agreement, local address ownership, duplicate listeners, and port availability. |
| Serial device unavailable | Check the configured path, OS permission, USB-serial driver, physical connection, and competing processes. |
| No network output | Confirm matching NMEA input, output type, pinned destination, routing, firewall, and plain-UDP consumer or UDPSEC session state. |
| Restart loop | Stop the unit, run the process manually with its exact config if safe, correct the persistent error, then start it again. |

The current runtime prints every forwarded matching sentence to standard output.
`log_level` does not currently filter this output, so journal or logread volume
can follow the forwarded traffic rate.

## ⚠️ Limitations and security boundary

Current operator-visible limits:

- one input-to-output relation per process;
- no mixing, AISMixer routing, fan-out, deduplication, or multipart assembly;
- proxy scanner support narrower than the mixer scanner;
- checksum-shaped syntax checking without checksum arithmetic verification;
- ingress TAG blocks, prefixes, and surrounding material stripped;
- lossy UDP delivery without payload buffering or retransmission;
- one destination resolution pinned until process restart;
- automatically allocated outbound source port;
- bounded serial queue that discards the oldest entry when full;
- process-local, non-durable UDPSEC session and replay state;
- fresh handshake required after a source-address or source-port tuple change;
- no automatic UDPSEC session migration;
- unconditional per-sentence standard-output logging.

UDPSEC provides confidentiality, cryptographic integrity, and peer
authentication for transport between configured and authenticated endpoints.
It does not establish the semantic truth of AIS reports, the physical truth of
vessel positions, or the accuracy of transmitted AIS content.

`input.allow_from` is an application filter, not peer authentication. Plain UDP
has no confidentiality, peer authentication, cryptographic integrity, replay
protection, or UDPSEC liveness behavior.

## 📚 Further documentation

- [AISMixer project and deployment overview](../README.md)
- [Normative Behavioural Contract](../BEHAVIORAL_CONTRACT.md)
- [Security policy and vulnerability reporting](../SECURITY.md)
- [Release history and compatibility notes](../CHANGELOG.md)
- [OpenWrt deployment guide](https://github.com/iliyan85/aismixer/wiki/OpenWrt-Deployment)
- [Project Wiki](https://github.com/iliyan85/aismixer/wiki)
