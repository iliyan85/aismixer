# nmea_sproxy operator guide

`nmea_sproxy` is a client-side UDPSEC proxy. UDPSEC is AISMixer's
authenticated encrypted UDP transport; it is not an external standardized
protocol. `nmea_sproxy` does not mix inputs, assemble multipart AIS,
deduplicate, rewrite TAG metadata, route streams, or fan out to egress targets.
AISMixer performs those jobs.

Each `nmea_sproxy` process represents exactly one UDPSEC relation:

```text
one local UDP input -> one AISMixer UDPSEC input
listen_ip/listen_port -> remote_host/remote_port
```

Run separate processes or systemd template instances for separate relations.

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

`listen_ip` / `listen_port` select the one local UDP input.
`remote_host` / `remote_port` select the configured remote AISMixer UDPSEC
input.

### Network endpoint controls

Two optional top-level controls are available for the station-side proxy:

- `allow_from` is an application-level ACL for the local UDP sender. When the
  key is omitted, no application ACL is applied and the current unrestricted
  local-input behavior is preserved. `allow_from: []` denies all local UDP
  input packets. Entries must be literal IPv4 or IPv6 addresses, or IPv4 or
  IPv6 CIDR networks. Hostnames and malformed entries fail startup validation.
- `source_ip` binds the outbound UDPSEC socket to a literal IPv4 or IPv6 source
  address and an automatically selected source port. When omitted, the
  operating system chooses the outbound source address as before. `source_ip`
  does not select an interface, routing table, socket mark, or source port.

When `source_ip` is configured, it selects the outbound socket address family.
A literal `remote_host` must use the same family, and a hostname `remote_host`
is resolved only within that family. The selected remote address and
`remote_port` are pinned for the process lifetime; handshake replies, pongs,
and `NOSESSION` hints are accepted only from that tuple.

The local ACL complements the host firewall; it does not replace firewall,
routing, or interface-level policy. Because the server session is bound to the
observed client source IP and port, changing the outbound source IP or source
port requires a new UDPSEC handshake.

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

## systemd services

### Singleton service

From the `nmea_sproxy` directory:

```bash
sudo ./install.sh
sudo systemctl start nmea_sproxy
```

The installer installs and enables `nmea_sproxy.service`, using:

```text
/etc/nmea_sproxy/config.yaml
```

It does not start the service automatically.

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
define its own 1:1 `listen_ip` / `listen_port` to `remote_host` /
`remote_port` relation.

The installer creates `/etc/nmea_sproxy/instances/` but does not create
instance configs or enable template instances.

## Keys and trust setup

The standard system key files are:

```text
/etc/nmea_sproxy/keys/station_private.pem
/etc/nmea_sproxy/keys/station_public.pem
/etc/nmea_sproxy/keys/aismixer_public.pem
```

- `station_private.pem` is the station identity private key. Keep it private
  and never copy it to AISMixer.
- `station_public.pem` is derived from the station private key and is used to
  create the station entry in AISMixer's `authorized_keys.yaml`.
- `aismixer_public.pem` is the trusted AISMixer server public key copied to the
  station. It lets the station verify the server handshake.

### Generation, preservation, and repair

During `sudo ./install.sh`:

- If both station key files are absent, a new station key pair is generated.
- If `station_private.pem` exists, it is preserved and
  `station_public.pem` is checked and repaired from it when needed.
- If only `station_public.pem` exists, installation stops rather than
  generating or overwriting private-key material.
- Existing `/etc/nmea_sproxy` configs and keys are preserved.
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

## Session lifecycle

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

Check that all three standard key files exist and that the service user can
read the station private key and AISMixer public key:

```bash
sudo ls -l /etc/nmea_sproxy/keys
```

From the `nmea_sproxy` directory, re-run `sudo ./install.sh` to generate a
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

From the repository:

```bash
sudo ./nmea_sproxy/update.sh
sudo ./nmea_sproxy/uninstall.sh
```

`update.sh` does not modify `/etc/nmea_sproxy` configs or keys.
`uninstall.sh` preserves `/etc/nmea_sproxy` by default; use
`uninstall.sh --purge-config` only when operator configs and keys should also
be removed.
