# AISMixer Examples

These files are examples only. They are not automatically installed and are not
loaded unless an operator copies or adapts them.

## Files

- `config-routing.yaml` is a full inactive AISMixer configuration showing
  static logical routing with named UDP ingress, UDPSEC ingress, named UDP
  forwarders, logical zones, and target-scoped deduplication.
- `config-routing-control.yaml` is a full inactive AISMixer configuration
  showing static routing plus `control.unix` enabled for the Unix-domain
  routing-control socket.
- `routing-update.yaml` is not a full config. It is a direct routing section
  with top-level `zones:` and `routes:` and can be used with
  `aismixerctl replace --file`.

## Runtime Control Example

From a repository checkout or copied service directory:

```bash
python3 aismixerctl.py --socket /run/aismixer/control.sock status
python3 aismixerctl.py --socket /run/aismixer/control.sock replace --file examples/routing-update.yaml
```

The shorter `aismixerctl` command works only if your local installation or
`PATH` provides it.

## Operator Notes

Adapt all IDs, ports, hosts, and paths before using these examples. The control
socket parent directory must already exist, and socket filesystem permissions
are the authorization boundary. Runtime routing changes are process-local and
are not persisted after restart.
