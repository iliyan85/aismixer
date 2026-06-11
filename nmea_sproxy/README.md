# nmea_sproxy secure proxy

`nmea_sproxy` is a secure UDP proxy, or shovel. It is not a mixer.

Each process represents exactly one relation:

```text
one local UDP input -> one encrypted remote secure output
```

The relation is configured with `listen_ip` / `listen_port` and
`remote_host` / `remote_port`.

## Manual use

From this directory, the existing no-argument workflow remains supported:

```bash
cd nmea_sproxy
python3 nmea_sproxy.py
```

An explicit config can be selected with:

```bash
python3 nmea_sproxy.py --config /path/to/shovel.yaml
```

The optional `--process-title TEXT` argument controls the name shown by
process tools when `setproctitle` is installed. Its default is `nmea_sproxy`.

Config resolution order is:

1. `--config PATH`
2. `NMEA_SPROXY_CONFIG`
3. `/etc/nmea_sproxy/config.yaml`
4. `config.yaml` next to `nmea_sproxy.py`
5. built-in defaults

An explicitly selected CLI or environment config must exist.
Relative `station_private_key`, `remote_public_key`, and legacy
`aismixer_public_key` values are resolved relative to the selected config
file, not the process working directory. When a configured canonical
`station_private.pem` is absent, an existing `station_private.key` beside it
is still accepted for compatibility.

After a successful handshake, the proxy sends encrypted pings and requires
authenticated encrypted pongs from the configured remote address. The
lifecycle settings are:

```yaml
keepalive_interval: 30
peer_timeout: 90
session_refresh_interval: 0
```

`keepalive_interval` controls encrypted ping frequency, `peer_timeout`
reconnects when authenticated replies stop, and `session_refresh_interval`
optionally triggers an immediate planned re-handshake without waiting for
`reconnect_delay`. The default value `0` disables proactive refresh, leaving
authenticated ping/pong plus `peer_timeout` as the primary recovery mechanism
for server restarts, NAT rebinding, and long outages.

## systemd services

`install.sh` installs:

- `nmea_sproxy.service`, using `/etc/nmea_sproxy/config.yaml`
- `nmea_sproxy@.service`, using `/etc/nmea_sproxy/instances/%i.yaml`

The singleton service is enabled during install but is not started
automatically.

Template instance labels are chosen by the operator. For example:

```bash
sudo cp /etc/nmea_sproxy/config.yaml /etc/nmea_sproxy/instances/boat.yaml
sudo systemctl enable --now nmea_sproxy@boat.service

sudo cp /etc/nmea_sproxy/config.yaml /etc/nmea_sproxy/instances/yacht.yaml
sudo systemctl enable --now nmea_sproxy@yacht.service

sudo cp /etc/nmea_sproxy/config.yaml /etc/nmea_sproxy/instances/balchik_roof.yaml
sudo systemctl enable --now nmea_sproxy@balchik_roof.service
```

Each instance config must define its own 1:1 input/output relation. The
installer creates `/etc/nmea_sproxy/instances/` but does not create instance
configs.

Default key paths are:

```text
/etc/nmea_sproxy/keys/station_private.pem
/etc/nmea_sproxy/keys/aismixer_public.pem
```

Instance configs may override key paths when needed.

The repository's `config.yaml` is the manual-use template and uses local
relative key paths. During installation, `config.system.yaml` is copied to
`/etc/nmea_sproxy/config.yaml` only when that system config does not already
exist.

The installer preserves `/etc/nmea_sproxy/keys`. It generates station keys
only when both `station_private.pem` and `station_public.pem` are absent. When
the private key exists, the installer derives and repairs the public key
without replacing the private key. It also warns when the trusted
`aismixer_public.pem` is missing.

## Lifecycle scripts

Run these scripts from the repository:

```bash
sudo bash ./nmea_sproxy/install.sh
sudo bash ./nmea_sproxy/update.sh
sudo bash ./nmea_sproxy/uninstall.sh
```

`update.sh` does not modify `/etc/nmea_sproxy` configs or keys.
`uninstall.sh` preserves `/etc/nmea_sproxy` by default; use
`uninstall.sh --purge-config` only when operator configs and keys should also
be removed.
