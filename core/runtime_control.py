"""Runtime integration helpers for optional routing control.

The Unix routing-control server is explicit opt-in runtime infrastructure. This
module parses the optional ``control.unix`` configuration and can build the
process-local control stack without starting the listener or touching the
filesystem.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
import re
from typing import TypeAlias

from core.routing_control import RoutingControlService
from core.routing_control_protocol import RoutingControlProtocol
from core.routing_control_unix import RoutingControlUnixServer
from core.routing_state import RoutingState


DEFAULT_CONTROL_MAX_REQUEST_BYTES = 1_048_576
DEFAULT_CONTROL_SOCKET_MODE = 0o660


class RuntimeControlConfigError(ValueError):
    """Raised when optional runtime control configuration is invalid."""


@dataclass(frozen=True, slots=True)
class RoutingControlUnixSettings:
    socket_path: str
    max_request_bytes: int = DEFAULT_CONTROL_MAX_REQUEST_BYTES
    socket_mode: int = DEFAULT_CONTROL_SOCKET_MODE


_ServiceFactory: TypeAlias = Callable[[RoutingState, Iterable[str]], object]
_ProtocolFactory: TypeAlias = Callable[[object], object]
_ServerFactory: TypeAlias = Callable[..., object]
_OCTAL_MODE_RE = re.compile(r"^[0-7]{3,4}$")


def load_optional_routing_control_unix_settings(
    config: Mapping[str, object],
) -> RoutingControlUnixSettings | None:
    """Load strict optional ``control.unix`` settings.

    The server is enabled only by ``control.unix.enabled: true``. Missing,
    null, or explicitly disabled configuration returns ``None``.
    """

    if not isinstance(config, Mapping):
        raise RuntimeControlConfigError("Config must be a mapping.")

    if "control" not in config or config["control"] is None:
        return None

    control = config["control"]
    if not isinstance(control, Mapping):
        raise RuntimeControlConfigError("'control' config must be a mapping.")

    _reject_unknown_fields(control, {"unix"}, "'control' config")
    if "unix" not in control or control["unix"] is None:
        return None

    unix = control["unix"]
    if not isinstance(unix, Mapping):
        raise RuntimeControlConfigError("'control.unix' config must be a mapping.")

    _reject_unknown_fields(
        unix,
        {"enabled", "socket_path", "socket_mode", "max_request_bytes"},
        "'control.unix' config",
    )

    if "enabled" not in unix:
        raise RuntimeControlConfigError(
            "'control.unix.enabled' is required when 'control.unix' is present."
        )
    enabled = unix["enabled"]
    if not isinstance(enabled, bool):
        raise RuntimeControlConfigError("'control.unix.enabled' must be a boolean.")
    if enabled is False:
        return None

    socket_path = _load_socket_path(unix)
    max_request_bytes = _load_max_request_bytes(unix)
    socket_mode = _load_socket_mode(unix)
    return RoutingControlUnixSettings(
        socket_path=socket_path,
        max_request_bytes=max_request_bytes,
        socket_mode=socket_mode,
    )


def build_optional_routing_control_server(
    config: Mapping[str, object],
    routing_state: RoutingState,
    available_target_ids: Iterable[str],
    *,
    service_factory: _ServiceFactory = RoutingControlService,
    protocol_factory: _ProtocolFactory = RoutingControlProtocol,
    server_factory: _ServerFactory = RoutingControlUnixServer,
) -> object | None:
    """Build, but do not start, the optional routing-control Unix server."""

    settings = load_optional_routing_control_unix_settings(config)
    if settings is None:
        return None

    service = service_factory(routing_state, available_target_ids)
    protocol = protocol_factory(service)
    return server_factory(
        protocol,
        settings.socket_path,
        max_request_bytes=settings.max_request_bytes,
        socket_mode=settings.socket_mode,
    )


def _reject_unknown_fields(
    mapping: Mapping[str, object],
    allowed_fields: set[str],
    description: str,
) -> None:
    unknown_fields = set(mapping) - allowed_fields
    if unknown_fields:
        unknown = ", ".join(sorted(str(field) for field in unknown_fields))
        raise RuntimeControlConfigError(
            f"{description} has unknown field(s): {unknown}."
        )


def _load_socket_path(unix: Mapping[str, object]) -> str:
    if "socket_path" not in unix:
        raise RuntimeControlConfigError(
            "'control.unix.socket_path' is required when enabled is true."
        )
    socket_path = unix["socket_path"]
    if not isinstance(socket_path, str) or not socket_path:
        raise RuntimeControlConfigError(
            "'control.unix.socket_path' must be a non-empty string."
        )
    return socket_path


def _load_max_request_bytes(unix: Mapping[str, object]) -> int:
    if "max_request_bytes" not in unix:
        return DEFAULT_CONTROL_MAX_REQUEST_BYTES
    max_request_bytes = unix["max_request_bytes"]
    if isinstance(max_request_bytes, bool) or not isinstance(max_request_bytes, int):
        raise RuntimeControlConfigError(
            "'control.unix.max_request_bytes' must be a positive integer."
        )
    if max_request_bytes <= 0:
        raise RuntimeControlConfigError(
            "'control.unix.max_request_bytes' must be a positive integer."
        )
    return max_request_bytes


def _load_socket_mode(unix: Mapping[str, object]) -> int:
    if "socket_mode" not in unix:
        return DEFAULT_CONTROL_SOCKET_MODE

    socket_mode = unix["socket_mode"]
    if isinstance(socket_mode, bool):
        raise RuntimeControlConfigError(
            "'control.unix.socket_mode' must be an octal permission mode."
        )

    if isinstance(socket_mode, int):
        mode = socket_mode
    elif isinstance(socket_mode, str) and _OCTAL_MODE_RE.fullmatch(socket_mode):
        mode = int(socket_mode, 8)
    else:
        raise RuntimeControlConfigError(
            "'control.unix.socket_mode' must be an octal permission mode."
        )

    if mode < 0 or mode > 0o777:
        raise RuntimeControlConfigError(
            "'control.unix.socket_mode' must be an octal permission mode."
        )
    return mode
