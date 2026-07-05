"""Thin command-line client for the local routing-control Unix socket.

aismixerctl constructs versioned routing-control protocol requests, sends one
request per Unix-domain NDJSON connection, and prints the structured protocol
response. It does not compile, validate, or install routing tables; all routing
semantics remain server-side.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

from core.routing_control_protocol import (
    METHOD_DISABLE,
    METHOD_REPLACE,
    METHOD_STATUS,
    ROUTING_CONTROL_PROTOCOL_VERSION,
)
from core.routing_control_unix_client import (
    RoutingControlClientError,
    RoutingControlConnectionError,
    RoutingControlResponseError,
    RoutingControlUnixClient,
)


EXIT_OK = 0
EXIT_USAGE_OR_INPUT = 2
EXIT_PROTOCOL_ERROR = 3
EXIT_CONNECTION_ERROR = 4
EXIT_INVALID_RESPONSE = 5
EXIT_INTERNAL_ERROR = 6
EXIT_INTERRUPTED = 130


class AismixerCtlInputError(ValueError):
    """Raised for local CLI input that should fail before connecting."""


def build_request_id(
    explicit_request_id: str | None,
    *,
    generated_request_id: Callable[[], str] | None = None,
) -> str:
    """Return an explicit non-empty request ID or generate an opaque one."""

    if explicit_request_id is not None:
        if not isinstance(explicit_request_id, str) or not explicit_request_id:
            raise AismixerCtlInputError("--request-id must be a non-empty string.")
        return explicit_request_id

    generator = generated_request_id or _uuid_request_id
    request_id = generator()
    if not isinstance(request_id, str) or not request_id:
        raise AismixerCtlInputError("Generated request ID is invalid.")
    return request_id


def build_status_request(request_id: str) -> dict[str, object]:
    _validate_request_id(request_id)
    return {
        "version": ROUTING_CONTROL_PROTOCOL_VERSION,
        "request_id": request_id,
        "method": METHOD_STATUS,
    }


def build_disable_request(
    request_id: str,
    *,
    expected_generation: int | None = None,
) -> dict[str, object]:
    _validate_request_id(request_id)
    request: dict[str, object] = {
        "version": ROUTING_CONTROL_PROTOCOL_VERSION,
        "request_id": request_id,
        "method": METHOD_DISABLE,
    }
    if expected_generation is not None:
        request["params"] = {
            "expected_generation": _validate_expected_generation(expected_generation)
        }
    return request


def build_replace_request(
    request_id: str,
    routing: Mapping[str, object],
    *,
    expected_generation: int | None = None,
) -> dict[str, object]:
    _validate_request_id(request_id)
    if not isinstance(routing, Mapping):
        raise AismixerCtlInputError("Routing section must be a mapping.")

    params: dict[str, object] = {"routing": routing}
    if expected_generation is not None:
        params["expected_generation"] = _validate_expected_generation(expected_generation)

    return {
        "version": ROUTING_CONTROL_PROTOCOL_VERSION,
        "request_id": request_id,
        "method": METHOD_REPLACE,
        "params": params,
    }


def load_routing_section_file(path: str | Path) -> Mapping[str, object]:
    """Load YAML and extract a candidate routing section without compiling it."""

    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle)
    except FileNotFoundError as exc:
        raise AismixerCtlInputError(f"Routing file not found: {path}") from exc
    except PermissionError as exc:
        raise AismixerCtlInputError(f"Routing file permission denied: {path}") from exc
    except yaml.YAMLError as exc:
        raise AismixerCtlInputError("Routing file contains invalid YAML.") from exc
    except OSError as exc:
        raise AismixerCtlInputError(f"Routing file could not be read: {path}") from exc

    return extract_routing_section(loaded)


def extract_routing_section(loaded: object) -> Mapping[str, object]:
    """Return either top-level routing: {...} or a direct routing section."""

    if not isinstance(loaded, Mapping):
        raise AismixerCtlInputError("Routing file root must be a mapping.")

    if "routing" in loaded:
        routing = loaded["routing"]
        if routing is None:
            raise AismixerCtlInputError(
                "Routing file has routing: null; use disable instead."
            )
        if not isinstance(routing, Mapping):
            raise AismixerCtlInputError("Top-level routing value must be a mapping.")
        return routing

    if set(loaded) == {"zones", "routes"}:
        return loaded

    raise AismixerCtlInputError("Routing file does not contain a usable routing section.")


def main(
    argv: Sequence[str] | None = None,
    *,
    client_factory: Callable[[str], object] = RoutingControlUnixClient,
    generated_request_id: Callable[[], str] | None = None,
) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE_OR_INPUT

    try:
        request_id = build_request_id(
            args.request_id,
            generated_request_id=generated_request_id,
        )
        request = build_request_from_args(args, request_id)
        response = asyncio.run(
            _send_request(
                client_factory,
                args.socket_path,
                request,
            )
        )
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED
    except AismixerCtlInputError as exc:
        _print_cli_error(str(exc))
        return EXIT_USAGE_OR_INPUT
    except RoutingControlConnectionError as exc:
        _print_cli_error(str(exc))
        return EXIT_CONNECTION_ERROR
    except RoutingControlResponseError as exc:
        _print_cli_error(str(exc))
        return EXIT_INVALID_RESPONSE
    except RoutingControlClientError as exc:
        _print_cli_error(str(exc))
        return EXIT_INVALID_RESPONSE
    except Exception:
        _print_cli_error("internal error")
        return EXIT_INTERNAL_ERROR

    output = format_response(response, pretty=args.pretty)
    if response["ok"] is True:
        sys.stdout.write(output)
        return EXIT_OK

    sys.stderr.write(output)
    return EXIT_PROTOCOL_ERROR


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aismixerctl")
    parser.add_argument("--socket", dest="socket_path", required=True)
    parser.add_argument("--request-id")
    parser.add_argument("--pretty", action="store_true")

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status")

    replace_parser = subparsers.add_parser("replace")
    replace_parser.add_argument("--file", required=True, dest="routing_file")
    replace_parser.add_argument(
        "--expected-generation",
        type=_parse_expected_generation,
        dest="expected_generation",
    )

    disable_parser = subparsers.add_parser("disable")
    disable_parser.add_argument(
        "--expected-generation",
        type=_parse_expected_generation,
        dest="expected_generation",
    )

    return parser


def build_request_from_args(args: argparse.Namespace, request_id: str) -> dict[str, object]:
    if args.command == "status":
        return build_status_request(request_id)
    if args.command == "disable":
        return build_disable_request(
            request_id,
            expected_generation=args.expected_generation,
        )
    if args.command == "replace":
        routing = load_routing_section_file(args.routing_file)
        return build_replace_request(
            request_id,
            routing,
            expected_generation=args.expected_generation,
        )
    raise AssertionError(f"Unsupported aismixerctl command: {args.command}")


def format_response(response: Mapping[str, object], *, pretty: bool = False) -> str:
    if pretty:
        return json.dumps(response, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    return (
        json.dumps(
            response,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    )


async def _send_request(
    client_factory: Callable[[str], object],
    socket_path: str,
    request: Mapping[str, object],
) -> Mapping[str, object]:
    client = client_factory(socket_path)
    request_method: Any = getattr(client, "request")
    return await request_method(request)


def _validate_request_id(request_id: str) -> None:
    if not isinstance(request_id, str) or not request_id:
        raise AismixerCtlInputError("request_id must be a non-empty string.")


def _validate_expected_generation(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AismixerCtlInputError("expected_generation must be a non-negative integer.")
    return value


def _parse_expected_generation(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "expected_generation must be a non-negative integer."
        ) from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError(
            "expected_generation must be a non-negative integer."
        )
    return parsed


def _uuid_request_id() -> str:
    return uuid.uuid4().hex


def _print_cli_error(message: str) -> None:
    print(f"aismixerctl: {message}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
