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

On an installed system:

```bash
aismixerctl status
aismixerctl replace --file examples/routing-update.yaml
```

From a repository checkout or copied service directory:

```bash
python3 aismixerctl.py status
python3 aismixerctl.py replace --file examples/routing-update.yaml
```

## Operator Notes

Adapt all IDs, ports, hosts, and paths before using these examples. The control
socket parent directory is provisioned by the installed systemd unit while the
service is running; provide an equivalent directory when running outside that
unit. Socket filesystem permissions are the authorization boundary. Runtime
routing changes are process-local and are not persisted after restart.
