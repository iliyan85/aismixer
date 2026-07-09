import ipaddress
import socket


UDPSEC_OUTPUT_TYPE = "udpsec"
UDP_OUTPUT_TYPE = "udp"
SUPPORTED_OUTPUT_TYPES = {UDPSEC_OUTPUT_TYPE, UDP_OUTPUT_TYPE}


class OutputConfigError(ValueError):
    """Raised for operator-facing output configuration errors."""


def address_family_name(family):
    if family == socket.AF_INET:
        return "IPv4"
    if family == socket.AF_INET6:
        return "IPv6"
    return str(family)


def family_for_ip_address(address):
    return socket.AF_INET6 if address.version == 6 else socket.AF_INET


def default_remote_family(host):
    return socket.AF_INET6 if ":" in str(host) else socket.AF_INET


def _explicit_value(mapping, key, context, required=False):
    if key not in mapping:
        if required:
            raise OutputConfigError(f"{context}: required")
        return None
    value = mapping[key]
    if value is None:
        raise OutputConfigError(f"{context}: explicit null is not supported")
    return value


def _validate_host(value, context):
    if not isinstance(value, str) or not value.strip():
        raise OutputConfigError(f"{context}: must be a non-empty string")
    return value


def _validate_port(value, context):
    if isinstance(value, bool) or not isinstance(value, int):
        raise OutputConfigError(f"{context}: must be an integer UDP port")
    if value < 1 or value > 65535:
        raise OutputConfigError(f"{context}: must be in the range 1-65535")
    return value


def parse_source_ip(config, context="source_ip"):
    if "source_ip" not in config:
        return None

    value = config["source_ip"]
    if value is None:
        raise OutputConfigError(
            f"{context}: explicit null is not supported"
        )
    if not isinstance(value, str) or not value.strip():
        raise OutputConfigError(
            f"{context}: invalid literal IP address {value!r}"
        )

    value = value.strip()
    try:
        return ipaddress.ip_address(value)
    except ValueError as exc:
        raise OutputConfigError(
            f"{context}: invalid literal IP address {value!r}"
        ) from exc


def normalize_output_config(config):
    if "output" not in config:
        if "source_ip" in config:
            parse_source_ip(config, context="source_ip")
        return {
            "type": UDPSEC_OUTPUT_TYPE,
            "host": _validate_host(config["remote_host"], "remote_host"),
            "port": _validate_port(config["remote_port"], "remote_port"),
            **(
                {"source_ip": config["source_ip"]}
                if "source_ip" in config else {}
            ),
            "legacy": True,
        }

    raw_output = config["output"]
    if raw_output is None:
        raise OutputConfigError("output: explicit null is not supported")
    if not isinstance(raw_output, dict):
        raise OutputConfigError("output: must be a mapping")

    allowed_keys = {"type", "host", "port", "source_ip"}
    unknown_keys = sorted(set(raw_output) - allowed_keys)
    if unknown_keys:
        names = ", ".join(f"output.{key}" for key in unknown_keys)
        raise OutputConfigError(f"{names}: unknown output option")

    output_type = _explicit_value(
        raw_output,
        "type",
        "output.type",
        required=True,
    )
    if not isinstance(output_type, str):
        raise OutputConfigError("output.type: must be 'udpsec' or 'udp'")
    if output_type not in SUPPORTED_OUTPUT_TYPES:
        raise OutputConfigError(
            "output.type: supported values are 'udpsec' and 'udp'"
        )

    output_config = {
        "type": output_type,
        "host": _validate_host(
            _explicit_value(
                raw_output,
                "host",
                "output.host",
                required=True,
            ),
            "output.host",
        ),
        "port": _validate_port(
            _explicit_value(
                raw_output,
                "port",
                "output.port",
                required=True,
            ),
            "output.port",
        ),
        "legacy": False,
    }
    if "source_ip" in raw_output:
        _explicit_value(raw_output, "source_ip", "output.source_ip")
        parse_source_ip(
            {"source_ip": raw_output["source_ip"]},
            context="output.source_ip",
        )
        output_config["source_ip"] = raw_output["source_ip"]
    return output_config


def resolve_remote_addr(host, port, family, context="remote_host"):
    try:
        addresses = socket.getaddrinfo(host, port, family, socket.SOCK_DGRAM)
    except socket.gaierror as exc:
        raise OutputConfigError(
            f"{context}: no {address_family_name(family)} address for "
            f"{host!r}:{port}"
        ) from exc
    if not addresses:
        raise OutputConfigError(
            f"{context}: no {address_family_name(family)} address for "
            f"{host!r}:{port}"
        )
    return addresses[0][4]


def resolve_output_endpoint(output_config):
    source_context = (
        "source_ip"
        if output_config.get("legacy")
        else "output.source_ip"
    )
    host_context = (
        "remote_host"
        if output_config.get("legacy")
        else "output.host"
    )
    source_address = parse_source_ip(output_config, context=source_context)
    host = output_config["host"]
    port = output_config["port"]
    if source_address is None:
        family = default_remote_family(host)
    else:
        family = family_for_ip_address(source_address)
        try:
            remote_literal = ipaddress.ip_address(str(host))
        except ValueError:
            remote_literal = None
        if (
            remote_literal is not None
            and remote_literal.version != source_address.version
        ):
            raise OutputConfigError(
                f"{host_context}: literal address family "
                f"{address_family_name(family_for_ip_address(remote_literal))} "
                f"does not match {source_context} {source_address}"
            )

    return resolve_remote_addr(host, port, family, context=host_context), family


def create_outbound_socket(family, source_address=None, context="source_ip"):
    sock = socket.socket(family, socket.SOCK_DGRAM)
    if source_address is None:
        return sock

    source_ip = str(source_address)
    try:
        sock.bind((source_ip, 0))
    except OSError as exc:
        close = getattr(sock, "close", None)
        if close:
            close()
        raise OutputConfigError(
            f"{context} {source_ip!r}: bind failed: {exc}"
        ) from exc
    return sock


def create_output_socket(output_config, family):
    source_context = (
        "source_ip"
        if output_config.get("legacy")
        else "output.source_ip"
    )
    source_address = parse_source_ip(output_config, context=source_context)
    return create_outbound_socket(family, source_address, context=source_context)


class PlainUdpOutputAdapter:
    def __init__(self, sock, remote_addr, output_config=None, family=None):
        self.sock = sock
        self.remote_addr = remote_addr
        self.output_config = (
            dict(output_config) if output_config is not None else None
        )
        self.family = family

    @classmethod
    def from_config(cls, output_config):
        remote_addr, family = resolve_output_endpoint(output_config)
        sock = create_output_socket(output_config, family)
        return cls(
            sock,
            remote_addr,
            output_config=output_config,
            family=family,
        )

    def recreate_socket(self):
        if self.output_config is None or self.family is None:
            raise OutputConfigError(
                "plain UDP output cannot recreate socket without a pinned "
                "startup endpoint"
            )
        self.close()
        self.sock = create_output_socket(self.output_config, self.family)

    def send_sentence(self, sentence):
        self.sock.sendto(sentence.encode(), self.remote_addr)

    def close(self):
        close = getattr(self.sock, "close", None)
        if close:
            close()
