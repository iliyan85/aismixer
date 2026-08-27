# nmea_sproxy operator guide

`nmea_sproxy` is a station-side network proxy. UDPSEC is AISMixer's
authenticated encrypted UDP transport; it is not an external standardized
protocol. The shipped configuration selects UDPSEC explicitly. Plain UDP
output must also be selected explicitly and is intended only for trusted
LAN/VPN compatibility deployments.
`nmea_sproxy` does not mix inputs, assemble multipart AIS, deduplicate, rewrite
TAG metadata, route streams, or fan out to egress targets. AISMixer performs
those jobs.

Each `nmea_sproxy` process represents exactly one relation:

```text
one local input (UDP or serial) -> one network output (UDPSEC or UDP)
input.type: udp/serial -> output.type: udpsec/udp
```

Run separate processes or systemd template instances for separate relations.

## Quick Start

From a fresh checkout, install the singleton and template systemd units with:

```bash
git clone https://github.com/iliyan85/aismixer
cd aismixer
./nmea_sproxy/install.sh
```

The installer enables the singleton `nmea_sproxy.service`, but intentionally
starts no service and enables no template instance. It installs the runtime,
shared key tooling, and configuration layout while preserving all existing
operator files under `/etc/nmea_sproxy`; it does not generate, inspect, repair,
or rotate station identity or peer-trust material.

For plain UDP, configure `output.type: udp`; no station identity or trusted peer
key is needed. For canonical UDPSEC with `output.type: udpsec`, complete this
pre-start workflow:

1. Edit `/etc/nmea_sproxy/config.yaml` and set the intended `station_id` and
   UDPSEC endpoint.
2. On a fresh station where both canonical station files are absent, deliberately
   generate the canonical P-256 pair before the first service start:

   ```bash
   sudo python3 /opt/nmea_sproxy/tools/aismixer_keys.py station \
     --keys-dir /etc/nmea_sproxy/keys \
     --station-id boat_001
   ```

   Replace `boat_001` with the configured `station_id`. The command has no
   `--force` or `--repair-public`; it refuses to overwrite existing material and
   prints the public value needed by AISMixer.
3. Add that printed public value to AISMixer's `authorized_keys.yaml`, using the
   same `station_id`.
4. Manually copy the trusted AISMixer public key to the configured
   `remote_public_key` path, normally
   `/etc/nmea_sproxy/keys/aismixer_public.pem`.

Runtime can automatically generate the canonical pair if both files are still
absent when UDPSEC activates. Pre-generating it is the safer systemd workflow
because the public identity can be authorized before the first connection.
Automatic repair is never performed; repair remains an explicit operator
action described in [Keys and trust setup](#keys-and-trust-setup).

Do not start a UDPSEC unit until its peer-trust file is present, readable, and
valid. Both supplied units use `Restart=always`, so knowingly starting with
missing or unusable trust causes repeated restart attempts. If this has already
happened, stop the unit, provision trust, and then start it again.

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

The installer and updater instructions above describe conventional
Linux/systemd. Current R2.1 OpenWrt uses the same demand-driven runtime rules
through per-relation procd preflight; its named-instance workflow is documented
below. The older v0.2.0-r2 package's eager identity preparation and automatic
public-key repair are historical behavior, not current R2.1 behavior. Current
OpenWrt also accepts deprecated legacy endpoint syntax under the same runtime
compatibility rules described below.

### Guide map

- [Configuration](#configuration)
- [UDP and serial local inputs](#local-input-modes)
- [UDPSEC and plain UDP outputs](#output-modes)
- [Network endpoint controls](#network-endpoint-controls)
- [OpenWrt/procd services](#openwrtprocd-services)
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
Sequence `0` is reserved for handshake confirmation. Ordinary pings start at
sequence `1`, remain positive, and are not reused within a confirmed session.
Every ping and pong also carries a required integer timestamp, but control
timestamps have no freshness check and do not determine peer liveness.

UDPSEC has no plaintext `NOSESSION` packet or equivalent unauthenticated
session-control message. AISMixer silently drops encrypted data or pings for a
session it does not have. The proxy ignores unauthenticated datagrams, even
when they appear to come from the configured remote address.

If AISMixer loses session state, matching authenticated pongs stop arriving.
The proxy sends one encrypted ping and retains its expected pong sequence. If
that pong is still missing at the next keepalive deadline, it starts one fresh
signed ECDHE handshake immediately instead of replacing the outstanding ping.
If that handshake fails, later attempts use `reconnect_delay`.
`peer_timeout` remains the fallback. With the 30-second keepalive and 90-second
peer-timeout defaults, proactive recovery normally starts at about 60 seconds.

A fresh handshake creates only pending server state until encrypted
sequence-zero confirmation succeeds. Any old active session remains intact if
confirmation fails and is replaced only after successful confirmation. Planned
refresh uses the same safe pending-session transition. Recovery adds no reset
or probe protocol, and UDP data sent during recovery is not replayed.

On normal shutdown, each endpoint sends an encrypted authenticated `close`
control message with its established directional session key before closing
the UDP socket. The message is best-effort and unacknowledged. A validated
station close lets AISMixer remove that exact active session immediately; a
validated AISMixer close makes the proxy leave the session and wait
`reconnect_delay`. SIGTERM from systemd or procd follows this graceful path.
Crashes and lost close datagrams fall back to liveness and session TTL. A close
from an old session cannot authenticate under fresh ECDHE traffic keys.

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
input:
  type: udp
  listen_ip: "::"
  listen_port: 50000

output:
  type: udpsec
  host: 192.0.2.10
  port: 17777

station_id: boat_001

keepalive_interval: 30
peer_timeout: 90
session_refresh_interval: 0

station_private_key: station_private.pem
remote_public_key: aismixer_public.pem
```

Explicit `input:` and `output:` mappings are the canonical configuration form.

### Deprecated top-level endpoint syntax

Existing configurations that use the old top-level endpoint fields remain
functional during this compatibility period, but loading them prints concise
operator-visible deprecation messages. Migrate the endpoint fields as follows:

OLD (deprecated):

```yaml
listen_ip: "::"
listen_port: 50000
allow_from:
  - 2001:db8:42::/64

remote_host: mixer.example.net
remote_port: 19999
source_ip: 192.0.2.20
```

NEW (canonical):

```yaml
input:
  type: udp
  listen_ip: "::"
  listen_port: 50000
  allow_from:
    - 2001:db8:42::/64

output:
  type: udpsec
  host: mixer.example.net
  port: 19999
  source_ip: 192.0.2.20
```

Thus top-level `allow_from` moves to `input.allow_from`, and top-level
`source_ip` moves to `output.source_ip`. Omitting `input` still selects the old
top-level UDP input, and omitting `output` still selects UDPSEC; deprecation
does not change either transport. A fully old configuration receives at most
one input and one output notice per load, not messages from packet or session
loops.

If an explicit mapping and obsolete top-level endpoint fields coexist, the
obsolete fields still trigger the applicable notice while existing
precedence and validation rules remain in force. This deprecation applies only
to top-level `listen_ip`, `listen_port`, `allow_from`, `remote_host`,
`remote_port`, and `source_ip`. Relation settings such as `station_id`,
`station_private_key`, `remote_public_key`, `reconnect_delay`,
`keepalive_interval`, `peer_timeout`, `session_refresh_interval`, and
`log_level` remain legitimate top-level settings.

### Local input modes

UDP and serial are both first-class local input types. The canonical UDP form
is:

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
deprecated top-level compatibility form. `input.listen_ip` and
`input.listen_port` are required, and `input.allow_from` is optional.

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

Canonical UDPSEC output uses the station identity, key files, handshake,
session, and authenticated ping/pong behavior:

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

The proxy normalizes and validates the input and output configuration before
performing identity side effects. The normalized effective output type then
drives activation:

- `udp` does not inspect, generate, repair, or load local station identity and
  does not require or load `remote_public_key`, even if key fields remain in the
  configuration.
- `udpsec` ensures or validates the local station identity, then loads and
  validates the manually provisioned trusted server public key before creating
  the UDPSEC transport. The deprecated form with omitted `output` still counts
  as `udpsec`.

`output.type` must be either `udpsec` or `udp`. Explicit output mappings require
both `output.host` and `output.port`; they do not borrow missing endpoint fields
from deprecated top-level `remote_host` / `remote_port`. When both explicit
output settings and those obsolete fields are present, the explicit `output:`
endpoint is used and the obsolete fields produce a deprecation notice.
Explicit nulls, unknown output keys, blank hosts, invalid ports, invalid
`output.source_ip`, and address-family mismatches fail startup validation.

### Network endpoint controls

Two optional network controls are available for the station-side proxy:

- `input.allow_from` is an application-level ACL for the local UDP sender. When
  omitted, no application ACL is applied and the current unrestricted
  local-input behavior is preserved. `input.allow_from: []` denies all local
  UDP input packets. Entries must be literal IPv4 or IPv6 addresses, or IPv4 or
  IPv6 CIDR
  networks. Hostnames and malformed entries fail startup validation.
  `input.allow_from` applies only to UDP input and is rejected for
  `input.type: serial`. The deprecated top-level `allow_from` remains rejected
  when any explicit `input:` mapping is present, so an ACL cannot be silently
  ignored.
- `output.source_ip` binds the outbound UDPSEC or plain UDP socket to a literal
  IPv4 or IPv6 source address and an automatically selected source port. When
  omitted, the operating system chooses the outbound source address as before.
  Source binding does not select an interface, routing table, socket mark, or
  fixed source port.

When source binding is configured, it selects the outbound socket address
family. A literal destination must use the same family, and a hostname
destination is resolved only within that family. The selected destination tuple
is pinned for the process lifetime. In UDPSEC mode, handshake replies and
encrypted pongs are considered only from that tuple and must still pass their
cryptographic authentication checks before they affect session state.

The local ACL complements the host firewall; it does not replace firewall,
routing, or interface-level policy. Because the server session is bound to the
observed client source IP and port, changing the outbound source IP or source
port requires a new UDPSEC handshake.

Source binding remains valid with both UDP and serial input modes because it
controls the outbound socket, not the local receiver.

IPv4 example:

```yaml
input:
  type: udp
  listen_ip: "0.0.0.0"
  listen_port: 50000
  allow_from:
    - 192.0.2.15
    - 198.51.100.0/24

output:
  type: udpsec
  host: mixer.example.net
  port: 19999
  source_ip: 192.0.2.20
```

IPv6 example:

```yaml
input:
  type: udp
  listen_ip: "::"
  listen_port: 50000
  allow_from:
    - 2001:db8:42::15
    - 2001:db8:42::/64

output:
  type: udpsec
  host: 2001:db8:77::10
  port: 19999
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
the process exits. For UDPSEC output, relative `station_private_key`,
`remote_public_key`, and legacy `aismixer_public_key` paths are resolved from
the directory containing the selected YAML file, not from the process working
directory. Plain UDP leaves these key settings inactive and does not inspect
their files.

For compatibility, if a configured `station_private.pem` is absent, an actually
existing `station_private.key` beside it is accepted. When neither canonical
nor legacy material exists, new managed identity uses the canonical
`station_private.pem` path rather than a nonexistent legacy path. An explicitly
configured custom private-key path remains operator-owned: runtime may validate
and use the existing private key for UDPSEC, but does not invent a public-key
filename or automatically generate or repair that custom identity.

The historical legacy layout shares `station_public.pem` with the canonical
layout. When that legacy private key actually exists and `station_private.pem`
does not, runtime continues to select the legacy private key; it does not treat
the shared public file alone as a reason to replace or repair either identity.

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

## OpenWrt/procd services

R2.1 keeps the same one-process-per-relation model on OpenWrt. One init service
supervises the optional backward-compatible singleton and any named relations:

```text
/etc/init.d/nmea_sproxy
    |-- /etc/nmea_sproxy/config.yaml            -> unnamed/default procd instance
    |-- /etc/nmea_sproxy/instances/boat.yaml    -> procd instance "boat"
    `-- /etc/nmea_sproxy/instances/roof.yaml    -> procd instance "roof"
```

The singleton starts when `/etc/nmea_sproxy/config.yaml` is a regular file. A
fresh package installation seeds that canonical config as an upgrade-preserved
conffile. The package also creates `/etc/nmea_sproxy/instances/`, but ships no
active named YAML. Operator-created files in that directory are not overwritten
on package installation or update.

Only regular files named `*.yaml` are discovered. The filename stem becomes the
named procd instance; accepted names match
`[A-Za-z0-9][A-Za-z0-9_.-]*`. Unsafe names are logged and skipped rather than
renamed or interpreted. Unrelated files are ignored. Each
accepted YAML still defines exactly one local UDP or serial input and one
UDPSEC or plain-UDP output.

The matching OpenWrt 25.12 package feeds do not provide
`python3-setproctitle`. The OpenWrt package therefore does not pass
`--process-title` or promise custom OS process titles. Use the procd instance
name as the relation identity; the exact `--config` path remains visible in the
process command line. This limitation does not apply to the conventional
Linux/systemd installer, which installs `setproctitle`.

One procd name is conditionally reserved: OpenWrt internally calls a
successfully started unnamed singleton `instance1`. When that singleton starts,
`instances/instance1.yaml` is skipped to avoid an internal instance-name
collision. The same filename is valid in a named-only deployment where no
singleton starts successfully.

A named file uses the same canonical syntax as the singleton. For example,
`/etc/nmea_sproxy/instances/boat.yaml` may contain:

```yaml
input:
  type: udp
  listen_ip: "::"
  listen_port: 50001

output:
  type: udpsec
  host: mixer.example.net
  port: 19999

station_id: boat_001
station_private_key: /etc/nmea_sproxy/keys/station_private.pem
remote_public_key: /etc/nmea_sproxy/keys/aismixer_public.pem
```

Replace the example addresses, ports, and station ID. To create and inspect a
named relation, run as root:

```sh
mkdir -p /etc/nmea_sproxy/instances
cp /etc/nmea_sproxy/config.yaml /etc/nmea_sproxy/instances/boat.yaml
vi /etc/nmea_sproxy/instances/boat.yaml
/etc/init.d/nmea_sproxy restart
ubus call service list '{"name":"nmea_sproxy"}'
ps w | grep '[n]mea_sproxy'
logread -e nmea_sproxy
```

Adding, removing, or renaming an instance file requires
`/etc/init.d/nmea_sproxy restart`; there is no directory watcher. OpenWrt has no
systemd-style `systemctl enable nmea_sproxy@boat` command. The singleton and
named relations can run together when their local inputs do not contend for the
same socket or serial device.

Each config is preflighted independently before its procd process is opened:

- Plain `output.type: udp` performs no station-identity inspection or creation
  and does not require or load peer trust.
- `output.type: udpsec`, including deprecated legacy output syntax, uses the
  current runtime to ensure or validate station identity and then validate the
  manually provisioned `remote_public_key`. An entirely absent canonical pair
  may be generated sequentially during preflight; partial, invalid, or
  mismatched material fails without repair.

One invalid config or unusable peer key skips only that relation; independent
valid singleton and named relations still start. The init operation fails with
a summary only when no relation is configured successfully. The init script
does not invoke the key CLI or automatically repair, fetch, or replace identity
or peer-trust material.

Relations that use `/etc/nmea_sproxy/keys/station_private.pem` intentionally
share the canonical station pair. A named file does not acquire a new identity
from its filename. Each config may instead select its own custom station private
key and trusted `remote_public_key` path. Custom and legacy private-key paths
remain operator-owned, and relative key paths resolve from the directory
containing that relation's YAML file. Existing legacy top-level endpoint YAML
remains functional but emits the normal deprecation notices; migrate it to
explicit `input:` / `output:` mappings when practical.

Packaging note: the R2.1 development recipe intentionally pins runtime source
commit `cd68b202e362712fe0503ea2b4b8d55ef88a609a`. At final R2.1 release closure,
repin `PKG_SOURCE_VERSION` if needed and recompute `PKG_MIRROR_HASH` from that
exact final source through the normal OpenWrt download/hash workflow.

## systemd services

### Singleton service

From the `nmea_sproxy` directory:

```bash
./install.sh
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

Plain UDP output does not inspect, generate, repair, or load either station key
and does not require or load `aismixer_public.pem`. Key settings may remain in a
shared configuration, but they are inactive for that relation.

### Generation, preservation, and repair

On conventional Linux, both `install.sh` and `update.sh` preserve existing
configuration, identity, and trust files under `/etc/nmea_sproxy`. They install
the shared key-management code and tool, but never generate, validate, repair,
rotate, or otherwise modify station identity or peer-trust material. A fresh
installation with no key files therefore succeeds.

After configuration has been normalized and validated, runtime applies these
rules only when the effective output is UDPSEC:

- If neither canonical station file exists, runtime generates one P-256 key
  pair at `station_private.pem` and `station_public.pem`.
- A complete, valid, matching canonical pair is preserved byte-for-byte.
- If only one canonical member exists, activation fails clearly without
  creating, repairing, or overwriting either file.
- Complete but invalid or mismatched canonical material also fails clearly and
  remains unchanged.

Runtime never repairs canonical material automatically. The fresh-generation
case is deliberately narrow: both canonical members must be absent. To create
that pair before the first systemd start and obtain its authorization value, use
the generation command in [Quick Start](#quick-start). It refuses existing
material unless an operator explicitly adds a force-overwrite option.

An actually existing legacy `station_private.key` remains accepted where the
default-path fallback supports it. An explicitly configured custom
`station_private_key` is also supported, but both cases are operator-owned:
runtime validates and uses an existing usable private key for UDPSEC, does not
invent a corresponding public-key filename, and does not generate or repair the
custom or legacy path. A missing or unusable operator-owned private key fails
activation clearly.

The configured `remote_public_key` is peer trust, not local identity. It is
never generated, fetched, replaced, or repaired by the proxy, installer, or
updater. For UDPSEC, a missing, unreadable, invalid, or incompatible peer key
fails before transport activation. Provision it manually before starting a
systemd unit; plain UDP does not require or load it.

If the canonical private key is known to be valid and the operator deliberately
wants to recreate its missing, invalid, or mismatched public mate, repair it
explicitly without replacing the private key:

```bash
sudo python3 /opt/nmea_sproxy/tools/aismixer_keys.py station \
  --keys-dir /etc/nmea_sproxy/keys \
  --station-id boat_001 \
  --repair-public
```

This `--repair-public` command is the explicit operator repair path; runtime and
lifecycle scripts never invoke it automatically. The tool prints the compressed
public key value needed by AISMixer. Do not use force-overwrite options casually;
replacing the station private key changes its identity and requires updating
AISMixer authorization.

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
reconnect_delay: 5
```

- `keepalive_interval` schedules authenticated encrypted pings. The proxy keeps
  one expected pong outstanding rather than replacing its sequence with a new
  ping. It must be a finite number greater than zero.
- `peer_timeout` ends the session and reconnects when matching authenticated
  pongs stop arriving; it remains the fallback if proactive recovery cannot be
  selected first. It must be a finite number greater than zero.
- `session_refresh_interval` optionally schedules a planned re-handshake.
  It must be a finite number greater than or equal to zero; the default `0`
  disables planned periodic refresh.
- `reconnect_delay` controls the delay after handshake failures, socket
  failures, `peer_timeout`, and an authenticated server close. A planned
  refresh or the first proactive recovery attempt re-handshakes immediately;
  failure of that attempt returns to this delay. It must be a finite number
  greater than or equal to zero.

These fields require YAML numeric values, not booleans or quoted numeric
strings. There is no required ordering or ratio between them. Explicit plain
UDP validates and uses only `reconnect_delay`; the other three timings are
UDPSEC-only.

Sequence `0` is used only by the encrypted confirmation ping and pong. In each
confirmed session, ordinary ping/pong sequences start at `1`, must be real
built-in integers rather than booleans, stay positive, and are not reused. A
ping or pong also requires a built-in-integer `timestamp`; its value is not
checked for freshness and does not extend a deadline by itself. Additional
JSON object fields do not replace or relax these required fields.

The proxy retains exactly one outstanding ordinary ping sequence. Only a pong
from the pinned remote tuple that authenticates under the current keys, matches
the station identity, and exactly matches that sequence clears the expectation
and refreshes liveness. Duplicate or stale pongs cannot refresh liveness after
the expectation is cleared or changed. Fresh ECDHE material gives each
confirmed session fresh directional keys, so captured traffic from an earlier
session cannot authenticate in its replacement.

When one ping remains unanswered through the next keepalive deadline, the
proxy reuses the normal signed handshake and encrypted sequence-zero
confirmation. With the defaults above, a ping is normally sent around 30
seconds after the last session start or ping and proactive rekey is selected
around 60 seconds, before the 90-second `peer_timeout`. No AIS sentence is
replayed during the transition.

Deadlines use monotonic time and are due at equality. When more than one is due,
the proxy applies `peer_timeout` first, planned session refresh second, and the
keepalive action last: proactive rekey for an unanswered ping or a new ping when
none is outstanding. Due deadlines are handled before poll-ready datagrams, so
a matching pong must be authenticated and accepted before the applicable
deadline. The proxy takes fresh monotonic readings for these decisions and
recomputes its final polling timeout immediately before `select()`, after any
pending local-input forwarding.

The server keeps the old active session while the replacement is pending.
Failed confirmation leaves the old session intact; successful confirmation
atomically installs the new directional keys when the server validates the
confirmation ping, then sends the encrypted sequence-zero confirmation pong.
The proxy treats the replacement session as confirmed only after authenticating
and accepting that pong. Graceful shutdown instead uses an encrypted,
unacknowledged close under the current keys. A validated client close removes
only that active server session. A validated server close makes the proxy use
normal reconnect backoff. A crash or lost UDP close falls back to proactive
liveness recovery, `peer_timeout`, and server session TTL.

The ping traffic helps preserve a NAT mapping, but the server associates a
session with the observed client source IP and port. NAT rebinding, changing
networks, or changing the source port therefore requires a new handshake.
There is no session migration between addresses.

## Troubleshooting

### `Server signature verification failed`

The configured `aismixer_public.pem` does not verify the responding server.
Confirm that the station has the trusted public key matching the AISMixer
private key and that `output.host` / `output.port` point to the intended server.
Do not bypass this check.

### `No response from server during handshake`

Check:

- AISMixer is running and its UDPSEC input is listening on the configured port.
- Firewalls and port forwarding allow UDP traffic in both directions.
- The station `station_id` and public key are present in AISMixer's
  `authorized_keys.yaml`.
- Station and server clocks are reasonably synchronized.
- `output.host` / `output.port` are correct.

### Liveness recovery or repeated reconnects

After an AISMixer restart, server-side session expiry, or a client
source-address change, the server silently drops traffic protected by the old
session. The proxy normally detects one unanswered ping at the next keepalive
deadline and immediately tries a fresh authenticated handshake. A failed
attempt waits `reconnect_delay`; `peer_timeout` remains the fallback. No
plaintext reset hint is used.

For repeated reconnects, verify bidirectional UDP reachability, NAT timeout
behavior, the configured `keepalive_interval` / `peer_timeout`, and AISMixer
logs.

### Missing key files

Plain UDP does not use UDPSEC key files. For UDPSEC, stop a systemd unit before
correcting startup key errors so `Restart=always` does not keep retrying:

```bash
sudo systemctl stop nmea_sproxy.service
sudo ls -l /etc/nmea_sproxy/keys
```

Then distinguish local identity from peer trust:

- If both canonical station files are absent, runtime can generate the pair at
  UDPSEC activation. Prefer the [Quick Start](#quick-start) pre-generation
  command when the public value still needs to be authorized before first use.
- If one canonical member is missing, or a complete pair is invalid or
  mismatched, runtime fails without changing either file. Diagnose the material;
  use the explicit `--repair-public` command only when the private key is known
  to be the identity that should be retained.
- A configured legacy or custom private-key path must already exist and contain
  a usable P-256 private key. Runtime does not generate or repair it.
- The trusted AISMixer public key must be copied manually to the configured
  `remote_public_key` path. The installer and updater do not fetch, replace, or
  validate that trust file.

After correcting and verifying the configuration and peer trust, start the
selected unit again.

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
does not generate, repair, rotate, or otherwise modify operator identity or
trust material, and intentionally does not restart any service. For template
deployments, restart and inspect only the selected instances, for example:

```bash
sudo systemctl restart nmea_sproxy@boat.service
sudo systemctl status nmea_sproxy@boat.service
```

Uninstall from the repository root with:

```bash
./nmea_sproxy/uninstall.sh
```

`uninstall.sh` preserves `/etc/nmea_sproxy` by default; use
`./nmea_sproxy/uninstall.sh --purge-config` only when operator configs and
keys should also be removed.
