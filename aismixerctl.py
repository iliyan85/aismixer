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
import os
import shlex
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO

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
DEFAULT_SOCKET_PATH = "/run/aismixer/control.sock"
SHELL_PROMPT = "aismixerctl> "
_HISTORY_DIRECTORY = "aismixer"
_HISTORY_FILENAME = "aismixerctl_history"
_HISTORY_LENGTH = 1000
_AUTO_READLINE = object()


class AismixerCtlInputError(ValueError):
    """Raised for local CLI input that should fail before connecting."""


class _InteractiveArgumentParser(argparse.ArgumentParser):
    """Argument parser that reports shell errors without terminating it."""

    def error(self, message: str) -> None:
        raise AismixerCtlInputError(message)


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
    input_func: Callable[[str], str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    environ: Mapping[str, str] | None = None,
    home_directory: str | Path | None = None,
    readline_module: object = _AUTO_READLINE,
) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE_OR_INPUT

    output_stream = sys.stdout if stdout is None else stdout
    error_stream = sys.stderr if stderr is None else stderr

    if args.command is None:
        if args.request_id is not None:
            _print_cli_error(
                "--request-id cannot be used with interactive mode.",
                file=error_stream,
            )
            return EXIT_USAGE_OR_INPUT
        return run_interactive_shell(
            socket_path=args.socket_path,
            pretty=args.pretty,
            client_factory=client_factory,
            generated_request_id=generated_request_id,
            input_func=input_func,
            stdout=output_stream,
            stderr=error_stream,
            environ=environ,
            home_directory=home_directory,
            readline_module=readline_module,
        )

    return dispatch_command(
        args,
        socket_path=args.socket_path,
        pretty=args.pretty,
        explicit_request_id=args.request_id,
        client_factory=client_factory,
        generated_request_id=generated_request_id,
        stdout=output_stream,
        stderr=error_stream,
    )


def dispatch_command(
    args: argparse.Namespace,
    *,
    socket_path: str,
    pretty: bool,
    explicit_request_id: str | None,
    client_factory: Callable[[str], object] = RoutingControlUnixClient,
    generated_request_id: Callable[[], str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Build, send, and render one remote command for either interface."""

    output_stream = sys.stdout if stdout is None else stdout
    error_stream = sys.stderr if stderr is None else stderr

    try:
        request_id = build_request_id(
            explicit_request_id,
            generated_request_id=generated_request_id,
        )
        request = build_request_from_args(args, request_id)
        response = asyncio.run(
            _send_request(
                client_factory,
                socket_path,
                request,
            )
        )
        output = format_response(response, pretty=pretty)
        if response["ok"] is True:
            output_stream.write(output)
            return EXIT_OK

        error_stream.write(output)
        return EXIT_PROTOCOL_ERROR
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED
    except AismixerCtlInputError as exc:
        _print_cli_error(str(exc), file=error_stream)
        return EXIT_USAGE_OR_INPUT
    except RoutingControlConnectionError as exc:
        _print_cli_error(str(exc), file=error_stream)
        return EXIT_CONNECTION_ERROR
    except RoutingControlResponseError as exc:
        _print_cli_error(str(exc), file=error_stream)
        return EXIT_INVALID_RESPONSE
    except RoutingControlClientError as exc:
        _print_cli_error(str(exc), file=error_stream)
        return EXIT_INVALID_RESPONSE
    except Exception:
        _print_cli_error("internal error", file=error_stream)
        return EXIT_INTERNAL_ERROR


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aismixerctl")
    parser.add_argument(
        "--socket",
        dest="socket_path",
        default=DEFAULT_SOCKET_PATH,
        help=f"control socket path (default: {DEFAULT_SOCKET_PATH})",
    )
    parser.add_argument("--request-id")
    parser.add_argument("--pretty", action="store_true")

    _add_remote_command_parsers(parser, required=False)
    return parser


def build_shell_parser() -> argparse.ArgumentParser:
    parser = _InteractiveArgumentParser(prog="aismixerctl")
    subparsers = _add_remote_command_parsers(parser, required=True)
    subparsers.add_parser("help")
    subparsers.add_parser("exit")
    subparsers.add_parser("quit")
    return parser


def _add_remote_command_parsers(
    parser: argparse.ArgumentParser,
    *,
    required: bool,
) -> Any:
    subparsers = parser.add_subparsers(dest="command", required=required)
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

    return subparsers


def run_interactive_shell(
    *,
    socket_path: str = DEFAULT_SOCKET_PATH,
    pretty: bool = False,
    client_factory: Callable[[str], object] = RoutingControlUnixClient,
    generated_request_id: Callable[[], str] | None = None,
    input_func: Callable[[str], str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    environ: Mapping[str, str] | None = None,
    home_directory: str | Path | None = None,
    readline_module: object = _AUTO_READLINE,
) -> int:
    """Run the interactive operational shell until a local exit command or EOF."""

    read_input = input if input_func is None else input_func
    output_stream = sys.stdout if stdout is None else stdout
    error_stream = sys.stderr if stderr is None else stderr
    command_parser = build_shell_parser()
    line_editor = (
        _load_readline() if readline_module is _AUTO_READLINE else readline_module
    )

    try:
        history_path = resolve_history_path(
            environ=environ,
            home_directory=home_directory,
        )
    except Exception:
        history_path = None

    _configure_readline(line_editor, history_path)

    try:
        while True:
            try:
                line = read_input(SHELL_PROMPT)
            except EOFError:
                _write_shell_newline(output_stream)
                return EXIT_OK
            except KeyboardInterrupt:
                _write_shell_newline(output_stream)
                continue

            if not line.strip():
                continue

            _remember_history(line_editor, line)

            try:
                tokens = shlex.split(line, comments=False, posix=True)
            except ValueError as exc:
                _print_cli_error(f"could not parse command: {exc}", file=error_stream)
                continue
            except KeyboardInterrupt:
                _write_shell_newline(output_stream)
                continue

            try:
                args = command_parser.parse_args(tokens)
            except AismixerCtlInputError as exc:
                _print_cli_error(str(exc), file=error_stream)
                continue
            except SystemExit:
                continue
            except KeyboardInterrupt:
                _write_shell_newline(output_stream)
                continue

            if args.command == "help":
                output_stream.write(command_parser.format_help())
                continue
            if args.command in {"exit", "quit"}:
                return EXIT_OK

            try:
                result = dispatch_command(
                    args,
                    socket_path=socket_path,
                    pretty=pretty,
                    explicit_request_id=None,
                    client_factory=client_factory,
                    generated_request_id=generated_request_id,
                    stdout=output_stream,
                    stderr=error_stream,
                )
            except KeyboardInterrupt:
                _write_shell_newline(output_stream)
                continue
            if result == EXIT_INTERRUPTED:
                _write_shell_newline(output_stream)
    finally:
        _save_history(line_editor, history_path)


def resolve_history_path(
    *,
    environ: Mapping[str, str] | None = None,
    home_directory: str | Path | None = None,
) -> Path:
    """Return the XDG state path used for persistent shell history."""

    environment = os.environ if environ is None else environ
    state_home = environment.get("XDG_STATE_HOME")
    if state_home:
        base = Path(state_home).expanduser()
    elif home_directory is None:
        base = Path.home() / ".local" / "state"
    else:
        base = Path(home_directory) / ".local" / "state"
    return base / _HISTORY_DIRECTORY / _HISTORY_FILENAME


def completion_candidates(line: str, text: str) -> tuple[str, ...]:
    """Return basic command or option completions for the current shell line."""

    commands = ("status", "replace", "disable", "help", "exit", "quit")
    stripped = line.lstrip()
    words = stripped.split()
    completing_command = not words or (
        len(words) == 1 and not stripped[-1:].isspace()
    )
    if completing_command:
        candidates = commands
    elif words[0] == "replace":
        candidates = ("--file", "--expected-generation")
    elif words[0] == "disable":
        candidates = ("--expected-generation",)
    else:
        candidates = ()
    return tuple(candidate for candidate in candidates if candidate.startswith(text))


def _load_readline() -> object | None:
    try:
        import readline
    except Exception:
        return None
    return readline


def _configure_readline(line_editor: object, history_path: Path | None) -> None:
    if line_editor is None:
        return

    _call_line_editor(line_editor, "set_auto_history", False)
    _call_line_editor(line_editor, "set_history_length", _HISTORY_LENGTH)
    if history_path is not None:
        _call_line_editor(line_editor, "read_history_file", str(history_path))
        _trim_history(line_editor)

    completer = _build_completer(line_editor)
    _call_line_editor(line_editor, "set_completer", completer)
    _call_line_editor(line_editor, "set_completer_delims", " \t\n")

    try:
        documentation = getattr(line_editor, "__doc__", "") or ""
    except Exception:
        documentation = ""
    binding = "bind ^I rl_complete" if "libedit" in documentation else "tab: complete"
    _call_line_editor(line_editor, "parse_and_bind", binding)


def _build_completer(line_editor: object) -> Callable[[str, int], str | None]:
    def complete(text: str, state: int) -> str | None:
        line = text
        try:
            get_line_buffer = getattr(line_editor, "get_line_buffer")
            line = get_line_buffer()
        except Exception:
            pass

        candidates = completion_candidates(line, text)
        if 0 <= state < len(candidates):
            return candidates[state]
        return None

    return complete


def _remember_history(line_editor: object, line: str) -> None:
    if line_editor is None:
        return

    command = line.rstrip("\r\n")
    if not command.strip():
        return

    try:
        get_length = getattr(line_editor, "get_current_history_length")
        get_item = getattr(line_editor, "get_history_item")
        length = get_length()
        if length > 0 and get_item(length) == command:
            return
    except Exception:
        pass

    if _call_line_editor(line_editor, "add_history", command):
        _trim_history(line_editor)


def _trim_history(line_editor: object) -> None:
    try:
        get_length = getattr(line_editor, "get_current_history_length")
        remove_item = getattr(line_editor, "remove_history_item")
        excess = get_length() - _HISTORY_LENGTH
        for _ in range(max(0, excess)):
            remove_item(0)
    except Exception:
        pass


def _save_history(line_editor: object, history_path: Path | None) -> None:
    if line_editor is None or history_path is None:
        return

    try:
        history_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        return
    _call_line_editor(line_editor, "write_history_file", str(history_path))


def _call_line_editor(line_editor: object, method_name: str, *args: object) -> bool:
    try:
        method: Any = getattr(line_editor, method_name)
        method(*args)
    except Exception:
        return False
    return True


def _write_shell_newline(output_stream: TextIO) -> None:
    output_stream.write("\n")
    output_stream.flush()


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


def _print_cli_error(message: str, *, file: TextIO | None = None) -> None:
    error_stream = sys.stderr if file is None else file
    print(f"aismixerctl: {message}", file=error_stream)


if __name__ == "__main__":
    raise SystemExit(main())
