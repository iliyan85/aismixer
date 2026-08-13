# nmea_sproxy operator guide

`nmea_sproxy` is a station-side network proxy. UDPSEC is AISMixer's
authenticated encrypted UDP transport; it is not an external standardized
protocol. UDPSEC remains the secure default and the legacy top-level output
behavior. Plain UDP output must be selected explicitly and is intended only
for trusted LAN/VPN compatibility deployments.
`nmea_sproxy` does not mix inputs, assemble multipart AIS, deduplicate, rewrite
TAG metadata, route streams, or fan out to egress targets. AISMixer performs
those jobs.

Each `nmea_sproxy` process represents exactly one relation:

```text
one local input (UDP or serial) -> one network output (UDPSEC or UDP)
top-level UDP fields or input.type: udp/serial -> remote_host/remote_port or output
```

Run separate processes or systemd template instances for separate relations.

## Quick Start

From a fresh checkout, install the singleton and template systemd units with:

```bash
git clone https://github.com/iliyan85/aismixer
cd aismixer
chmod +x nmea_sproxy/install.sh nmea_sproxy/update.sh
./nmea_sproxy/install.sh
```

The installer enables the singleton `nmea_sproxy.service`, but intentionally
starts no service and enables no template instance. Before starting a UDPSEC
relation, edit `/etc/nmea_sproxy/config.yaml`, copy the trusted AISMixer public
key to `/etc/nmea_sproxy/keys/aismixer_public.pem`, and authorize the station
public key in AISMixer as described in [Keys and trust
setup](#keys-and-trust-setup).

Start and inspect the singleton after configuration and trust setup:

```bash
sudo systemctl start nmea_sproxy.service
sudo systemctl status nmea_sproxy.service
```

For a separate relation, copy and edit one configuration file and start one
template instance. The instance name is an operator-chosen label:

```bash
sudo cp /etc/nmea_sproxy/config.yaml /etc/nmea_sproxy/instances/boat.yaml
sudo systemctl enable --now nmea_sproxy@boat.service
sudo systemctl status nmea_sproxy@boat.service
```

Each singleton or template instance still represents exactly one local input
and one network output. A template-only deployment can disable the
installer-enabled singleton after confirming that it is not needed:

```bash
sudo systemctl disable --now nmea_sproxy.service
```

The lifecycle scripts can run directly as root. From a non-root account they
use `sudo` for privileged operations when it is available; root users can omit
`sudo` from the `systemctl` examples. The updater installs new runtime and unit
files but intentionally restarts nothing, so the operator must restart the
singleton or each selected template instance. See [Update and
uninstall](#update-and-uninstall) for the compact update flow.

### Guide map

- [Configuration](#configuration)
- [UDP and serial local inputs](#local-input-modes)
- [UDPSEC and plain UDP outputs](#output-modes)
- [Network endpoint controls](#network-endpoint-controls)
- [Singleton and template services](#systemd-services)
- [Keys and trust setup](#keys-and-trust-setup)
- [UDPSEC session lifecycle](#udpsec-session-lifecycle)
- [Troubleshooting](#troubleshooting)
- [Update and uninstall](#update-and-uninstall)

## UDPSEC behavior and limits

The station authenticates to AISMixer with its ECDSA identity key. The station
also verifies the AISMixer server key. After the handshake, AIS data is sent in
AES-GCM authenticated encrypted packets.

UDPSEC authenticates the station and protects packets in transit. It does not
prove that the AIS payload itself is semantically true or physically accurate.

The proxy sends authenticated encrypted pings and accepts matching
authenticated encrypted pongs from the configured remote peer. These messages
provide liveness and help keep NAT, CGNAT, and mobile-client UDP mappings alive.

AISMixer may send `NOSESSION` when it receives traffic for a session it no
longer has. `NOSESSION` is unauthenticated and is only a reconnect hint; the
proxy accepts it only from the configured remote address, ends the local
session, and attempts a new handshake after `reconnect_delay`.

UDPSEC session recovery does not make UDP reliable:

- UDP packet loss is still possible.
- Recovery does not guarantee delivery of every AIS sentence.
- A changed client source IP or source port requires a new handshake.
- No session migration is implemented.

The design assumes that the station client behind NAT, CGNAT, or a mobile
network is the active side: it initiates the handshake and sends keepalive
traffic to the reachable AISMixer UDPSEC input.

There is no UDPSEC-to-plain-UDP fallback. A relation uses plain UDP only when
`output.type: udp` is explicitly configured.

## Plain UDP behavior and limits

Plain UDP output sends each extracted `!AIVDM` or `!AIVDO` sentence as one UDP
datagram. It sends the NMEA sentence text itself: no JSON envelope, no UDPSEC
prefix, no encryption, no `station_id`, and no TAG metadata rewrite.

Plain UDP provides no confidentiality, station authentication, integrity
protection, replay protection, or liveness protocol. Use it only where those
properties are supplied by a controlled LAN, VPN, or other external network
boundary. In plain UDP mode AISMixer must derive source identity from its own
UDP ingress configuration, listener ID, allow-list policy, and observed source
address; `nmea_sproxy` does not authenticate `station_id` in plain UDP mode.

## Configuration

A minimal relation looks like this:

```yaml
listen_ip: "::"
listen_port: 50000
remote_host: 192.0.2.10
remote_port: 17777
station_id: boat_001

keepalive_interval: 30
peer_timeout: 90
session_refresh_interval: 0

station_private_key: station_private.pem
remote_public_key: aismixer_public.pem
```

When `input:` is omitted, `listen_ip` / `listen_port` select UDP input through
the backward-compatible top-level configuration form.
When `output:` is omitted, `remote_host` / `remote_port` select the legacy
AISMixer UDPSEC output and behavior is unchanged.

### Local input modes

UDP and serial are both first-class local input types. For compatibility, UDP
remains the default when `input:` is omitted and uses the top-level listener
fields:

```yaml
listen_ip: "::"
listen_port: 50000
```

The canonical explicit UDP form is:

```yaml
input:
  type: udp
  listen_ip: "::"
  listen_port: 50000
  allow_from:
    - 2001:db8:42::15
    - 2001:db8:42::/64
```

The explicit UDP form does not borrow missing listener fields from the
top-level compatibility form. `input.listen_ip` and `input.listen_port` are
required, and `input.allow_from` is optional.

Serial input also uses an explicit `input:` mapping. In serial mode no local
UDP listener is created, and the configured `port` string is passed unchanged
to pySerial.

Linux example:

```yaml
input:
  type: serial
  port: /dev/serial/by-id/usb-SRT_Marine_Technology_Ltd._AIS_Virtual_COM_Port_<device-id>-if00
  baudrate: 38400
  bytesize: 8
  parity: N
  stopbits: 1
  read_timeout: 1.0
  reconnect_delay: 5
  max_line_bytes: 4096
```

Windows example:

```yaml
input:
  type: serial
  port: COM4
  baudrate: 38400
  bytesize: 8
  parity: N
  stopbits: 1
  read_timeout: 1.0
  reconnect_delay: 5
  max_line_bytes: 4096
```

The serial defaults are `baudrate: 38400`, `bytesize: 8`, `parity: N`,
`stopbits: 1`, `read_timeout: 1.0`, `reconnect_delay: 5`, and
`max_line_bytes: 4096`. Explicit null values, invalid numeric ranges,
unsupported parity or stop-bit values, missing/blank `input.port`, unknown
input options, and `input.type` values other than `udp` or `serial` fail startup
validation. UDP listener and ACL options are rejected in a serial mapping.

The serial reader uses a daemon thread and a bounded queue, so it works on both
Linux and Windows without putting the serial port in `select.select()`. If the
device is absent or disconnects, the proxy logs a concise message, closes the
current port object, discards unsafe partial line-framing state, and retries the
same configured port after `input.reconnect_delay`. If the internal queue fills,
the oldest queued line is dropped so fresher real-time AIS traffic can continue.

### Output modes

The legacy UDPSEC output remains the default and uses the top-level endpoint
fields:

```yaml
remote_host: mixer.example.net
remote_port: 19999
source_ip: 192.0.2.20
```

Explicit UDPSEC output uses the same station identity, key files, handshake,
session, ping/pong, and `NOSESSION` behavior:

```yaml
output:
  type: udpsec
  host: mixer.example.net
  port: 19999
  source_ip: 192.0.2.20
```

Explicit plain UDP output disables UDPSEC for that relation:

```yaml
output:
  type: udp
  host: 192.168.10.20
  port: 17777
  source_ip: 192.168.10.15
```

`output.type` must be either `udpsec` or `udp`. Explicit output mappings require
both `output.host` and `output.port`; they do not borrow missing endpoint fields
from legacy `remote_host` / `remote_port`. When both explicit output settings
and legacy endpoint fields are present, the explicit `output:` endpoint is used.
Explicit nulls, unknown output keys, blank hosts, invalid ports, invalid
`output.source_ip`, and address-family mismatches fail startup validation.

### Network endpoint controls

Two optional network controls are available for the station-side proxy:

- `allow_from` is an application-level ACL for the local UDP sender. Use the
  top-level key with the backward-compatible top-level UDP form, or
  `input.allow_from` with explicit `input.type: udp`. When the key is omitted,
  no application ACL is applied and the current unrestricted local-input
  behavior is preserved. `allow_from: []` denies all local UDP input packets.
  Entries must be literal IPv4 or IPv6 addresses, or IPv4 or IPv6 CIDR
  networks. Hostnames and malformed entries fail startup validation.
  `allow_from` applies only to UDP input and is rejected for
  `input.type: serial`. A top-level `allow_from` is rejected when any explicit
  `input:` mapping is present, so an ACL cannot be silently ignored.
- `source_ip` binds the legacy outbound UDPSEC socket to a literal IPv4 or IPv6
  source address and an automatically selected source port. In explicit output
  mode, use `output.source_ip` for either UDPSEC or plain UDP output. When
  omitted, the operating system chooses the outbound source address as before.
  Source binding does not select an interface, routing table, socket mark, or
  fixed source port.

When source binding is configured, it selects the outbound socket address
family. A literal destination must use the same family, and a hostname
destination is resolved only within that family. The selected destination tuple
is pinned for the process lifetime. In UDPSEC mode, handshake replies, pongs,
and `NOSESSION` hints are accepted only from that tuple.

The local ACL complements the host firewall; it does not replace firewall,
routing, or interface-level policy. Because the server session is bound to the
observed client source IP and port, changing the outbound source IP or source
port requires a new UDPSEC handshake.

Source binding remains valid with both UDP and serial input modes because it
controls the outbound socket, not the local receiver.

IPv4 example:

```yaml
listen_ip: "0.0.0.0"
listen_port: 50000
allow_from:
  - 192.0.2.15
  - 198.51.100.0/24

remote_host: mixer.example.net
remote_port: 19999
source_ip: 192.0.2.20
```

IPv6 example:

```yaml
listen_ip: "::"
listen_port: 50000
allow_from:
  - 2001:db8:42::15
  - 2001:db8:42::/64

remote_host: 2001:db8:77::10
remote_port: 19999
source_ip: 2001:db8:42::20
```

### Config resolution order

The proxy selects configuration in this order:

1. `--config PATH`
2. `NMEA_SPROXY_CONFIG`
3. `/etc/nmea_sproxy/config.yaml`
4. `config.yaml` next to `nmea_sproxy.py`
5. built-in defaults

An explicitly selected `--config` or `NMEA_SPROXY_CONFIG` path must exist or
the process exits. Relative `station_private_key`, `remote_public_key`, and
legacy `aismixer_public_key` paths are resolved from the directory containing
the selected YAML file, not from the process working directory. For
compatibility, if a configured `station_private.pem` is absent, an existing
`station_private.key` beside it is accepted.

The repository files have separate purposes:

- `config.yaml` is the local/manual-use template and uses relative key paths.
- `config.system.yaml` is the source template installed as
  `/etc/nmea_sproxy/config.yaml` and uses system key paths.

The installer copies `config.system.yaml` only when the system config does not
already exist.

## Manual mode

From the repository:

```bash
cd nmea_sproxy
python3 nmea_sproxy.py
```

Select a specific config with the CLI or environment:

```bash
python3 nmea_sproxy.py --config /path/to/udpsec-proxy.yaml
NMEA_SPROXY_CONFIG=/path/to/udpsec-proxy.yaml python3 nmea_sproxy.py
```

Use `--process-title TEXT` to choose the name shown by process tools when
`setproctitle` is installed:

```bash
python3 nmea_sproxy.py --process-title nmea_sproxy@boat
```

The no-argument workflow remains supported and follows the config resolution
order above.

On Windows, install the manual-mode dependencies with:

```powershell
py -m pip install pyserial pyyaml cryptography setproctitle
```

If `setproctitle` is unavailable on Windows, `nmea_sproxy` continues without
changing the process title. pySerial can list visible serial ports:

```powershell
py -m serial.tools.list_ports
```

Use the shown COM name, such as `COM4`, as `input.port`. No Windows Service
integration is provided by this installer; the systemd scripts are Linux-only.

## systemd services

### Singleton service

From the `nmea_sproxy` directory:

```bash
bash ./install.sh
sudo systemctl start nmea_sproxy
```

The installer installs and enables `nmea_sproxy.service`, using:

```text
/etc/nmea_sproxy/config.yaml
```

It does not start the service automatically.
On Debian-family systems the installer requires `python3-serial`,
`python3-yaml`, `python3-cryptography`, and `python3-setproctitle`. Manual mode
can continue without changing the process title when `setproctitle` is absent.
Future non-root service users would also need operating-system permission to
open the serial device, for example through the appropriate device group.

### Template services

Template services use one YAML file per relation:

```text
/etc/nmea_sproxy/instances/<operator-name>.yaml
```

For example:

```bash
sudo cp /etc/nmea_sproxy/config.yaml /etc/nmea_sproxy/instances/boat.yaml
sudo cp /etc/nmea_sproxy/config.yaml /etc/nmea_sproxy/instances/yacht.yaml
sudo cp /etc/nmea_sproxy/config.yaml /etc/nmea_sproxy/instances/balchik_roof.yaml

sudo systemctl start nmea_sproxy@boat
sudo systemctl start nmea_sproxy@yacht
sudo systemctl start nmea_sproxy@balchik_roof
```

`boat`, `yacht`, and names such as `balchik_roof` are operator-chosen labels.
They are not predefined or numbered instance names. Each instance config must
define one local UDP or serial input and one network output using UDPSEC to
AISMixer or explicitly configured plain UDP to a compatible trusted-network
consumer.

The installer creates `/etc/nmea_sproxy/instances/` but does not create
instance configs or enable template instances.

## Keys and trust setup

The standard system key files are:

```text
/etc/nmea_sproxy/keys/station_private.pem
/etc/nmea_sproxy/keys/station_public.pem
/etc/nmea_sproxy/keys/aismixer_public.pem
```

- `station_private.pem` is the station identity private key for UDPSEC mode.
  Keep it private and never copy it to AISMixer.
- `station_public.pem` is derived from the station private key and is used to
  create the station entry in AISMixer's `authorized_keys.yaml`.
- `aismixer_public.pem` is the trusted AISMixer server public key copied to the
  station. It lets the station verify the server handshake.

Plain UDP output does not load or require `station_private.pem` or
`aismixer_public.pem`.

### Generation, preservation, and repair

During installation:

- If both station key files are absent, a new station key pair is generated.
- If `station_private.pem` exists, it is preserved and
  `station_public.pem` is checked and repaired from it when needed.
- If only `station_public.pem` exists, installation stops rather than
  generating or overwriting private-key material.
- Existing configuration, station private key, and trusted AISMixer public key
  are preserved. The derived station public key may be repaired from the
  preserved private key.
- A missing `aismixer_public.pem` produces a warning; copy the trusted server
  public key before starting the proxy.

To repair the public key manually without replacing the private key:

```bash
sudo python3 /opt/nmea_sproxy/tools/aismixer_keys.py station \
  --keys-dir /etc/nmea_sproxy/keys \
  --station-id boat_001 \
  --repair-public
```

The key tool prints the compressed public key value needed by AISMixer.
Do not use force-overwrite options casually; replacing the station private key
changes its identity and requires updating AISMixer authorization.

### Authorize the station in AISMixer

Add the printed station public-key value to AISMixer's `authorized_keys.yaml`
(normally `/etc/aismixer/authorized_keys.yaml`). The `name` must match the
proxy's configured `station_id`:

```yaml
authorized_clients:
  - name: boat_001
    pubkey: <compressed-public-key-base64>
```

Restart AISMixer after changing its authorization file. Trust material is not
exchanged automatically: copy the AISMixer public key to the station as
`aismixer_public.pem`, and add the station public-key value to AISMixer.

## UDPSEC session lifecycle

The defaults are:

```yaml
keepalive_interval: 30
peer_timeout: 90
session_refresh_interval: 0
```

- `keepalive_interval` is the interval between authenticated encrypted pings.
- `peer_timeout` ends the session and reconnects when matching authenticated
  pongs stop arriving.
- `session_refresh_interval` optionally schedules a planned re-handshake.
  The default `0` disables planned periodic refresh.
- `reconnect_delay` controls the delay after handshake failures, socket
  failures, `peer_timeout`, and `NOSESSION`. A planned refresh re-handshakes
  immediately.

The ping traffic helps preserve a NAT mapping, but the server associates a
session with the observed client source IP and port. NAT rebinding, changing
networks, or changing the source port therefore requires a new handshake.
There is no session migration between addresses.

## Troubleshooting

### `Server signature verification failed`

The configured `aismixer_public.pem` does not verify the responding server.
Confirm that the station has the trusted public key matching the AISMixer
private key and that `remote_host` / `remote_port` point to the intended
server. Do not bypass this check.

### `No response from server during handshake`

Check:

- AISMixer is running and its UDPSEC input is listening on the configured port.
- Firewalls and port forwarding allow UDP traffic in both directions.
- The station `station_id` and public key are present in AISMixer's
  `authorized_keys.yaml`.
- Station and server clocks are reasonably synchronized.
- `remote_host` / `remote_port` are correct.

### `NOSESSION` or repeated reconnects

An occasional `NOSESSION` can follow an AISMixer restart, server-side session
expiry, or a client source-address change. The proxy treats it as a reconnect
hint and performs a new handshake after `reconnect_delay`.

For repeated reconnects, verify bidirectional UDP reachability, NAT timeout
behavior, the configured `keepalive_interval` / `peer_timeout`, and AISMixer
logs. Remember that `NOSESSION` itself is not authenticated.

### Missing key files

For UDPSEC mode, check that all three standard key files exist and that the
service user can read the station private key and AISMixer public key:

```bash
sudo ls -l /etc/nmea_sproxy/keys
```

From the `nmea_sproxy` directory, re-run `bash ./install.sh` to generate a
missing station key pair or repair a station public key while preserving an
existing private key. Copy the trusted AISMixer public key separately; the
installer does not fetch it.

### systemd status and logs

```bash
sudo systemctl status nmea_sproxy
sudo journalctl -u nmea_sproxy -f

sudo systemctl status nmea_sproxy@boat
sudo journalctl -u nmea_sproxy@boat -f
```

## Update and uninstall

From the repository root, update the checkout and installed files, then restart
the singleton when ready:

```bash
git pull --ff-only
./nmea_sproxy/update.sh
sudo systemctl restart nmea_sproxy.service
sudo systemctl status nmea_sproxy.service
```

`update.sh` does not modify `/etc/nmea_sproxy` configs or keys and intentionally
does not restart any service. For template deployments, restart and inspect
only the selected instances, for example:

```bash
sudo systemctl restart nmea_sproxy@boat.service
sudo systemctl status nmea_sproxy@boat.service
```

If this checkout predates the Quick Start permission step, run
`chmod +x nmea_sproxy/update.sh` once before invoking the updater.

Uninstall from the repository root with:

```bash
bash ./nmea_sproxy/uninstall.sh
```

`uninstall.sh` preserves `/etc/nmea_sproxy` by default; use
`bash ./nmea_sproxy/uninstall.sh --purge-config` only when operator configs and
keys should also be removed.
