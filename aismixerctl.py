"""Thin command-line client for the local control Unix socket.

aismixerctl constructs versioned routing-control protocol requests, sends one
request per Unix-domain NDJSON connection, and preserves structured responses
in one-shot mode. The interactive shell renders successful runtime statistics
as tables. Routing semantics remain server-side.
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
    METHOD_RUNTIME_STATISTICS,
    METHOD_RUNTIME_STATISTICS_INPUTS,
    METHOD_RUNTIME_STATISTICS_OUTPUTS,
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
_STATISTICS_HELP = (
    "Statistics commands:\n"
    "  show statistics\n"
    "  show statistics inputs [INPUT]\n"
    "  show statistics outputs [OUTPUT]"
)


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


def build_runtime_statistics_request(request_id: str) -> dict[str, object]:
    _validate_request_id(request_id)
    return {
        "version": ROUTING_CONTROL_PROTOCOL_VERSION,
        "request_id": request_id,
        "method": METHOD_RUNTIME_STATISTICS,
    }


def build_runtime_statistics_inputs_request(
    request_id: str,
    input_name: str | None = None,
) -> dict[str, object]:
    _validate_request_id(request_id)
    request: dict[str, object] = {
        "version": ROUTING_CONTROL_PROTOCOL_VERSION,
        "request_id": request_id,
        "method": METHOD_RUNTIME_STATISTICS_INPUTS,
    }
    if input_name is not None:
        if not isinstance(input_name, str) or not input_name:
            raise AismixerCtlInputError(
                "Input statistics filter must be a non-empty string."
            )
        request["params"] = {"input": input_name}
    return request


def build_runtime_statistics_outputs_request(
    request_id: str,
    output: str | None = None,
) -> dict[str, object]:
    _validate_request_id(request_id)
    request: dict[str, object] = {
        "version": ROUTING_CONTROL_PROTOCOL_VERSION,
        "request_id": request_id,
        "method": METHOD_RUNTIME_STATISTICS_OUTPUTS,
    }
    if output is not None:
        if not isinstance(output, str) or not output:
            raise AismixerCtlInputError(
                "Output statistics filter must be a non-empty string."
            )
        if output.isdecimal():
            request["params"] = {"target_id": int(output)}
        else:
            request["params"] = {"name": output}
    return request


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
    interactive: bool = False,
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
        if response["ok"] is True:
            if interactive and _is_statistics_command(args):
                output = _format_interactive_statistics(
                    args,
                    response["result"],
                )
            else:
                output = format_response(response, pretty=pretty)
            output_stream.write(output)
            return EXIT_OK

        output = format_response(response, pretty=pretty)
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
    parser = argparse.ArgumentParser(
        prog="aismixerctl",
        epilog=_STATISTICS_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
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
    parser = _InteractiveArgumentParser(
        prog="aismixerctl",
        epilog=_STATISTICS_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
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

    show_parser = subparsers.add_parser(
        "show",
        help="show runtime information (for example: show statistics)",
    )
    show_subparsers = show_parser.add_subparsers(
        dest="show_command",
        required=True,
    )
    statistics_parser = show_subparsers.add_parser(
        "statistics",
        help="show runtime statistics",
    )
    statistics_subparsers = statistics_parser.add_subparsers(
        dest="statistics_command",
    )
    inputs_parser = statistics_subparsers.add_parser(
        "inputs",
        help="show per-input transport and accepted-frame traffic",
    )
    inputs_parser.add_argument(
        "input_filter",
        nargs="?",
        metavar="INPUT",
    )
    outputs_parser = statistics_subparsers.add_parser(
        "outputs",
        help="show per-output local dispatch traffic",
    )
    outputs_parser.add_argument(
        "output_filter",
        nargs="?",
        metavar="OUTPUT",
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
                    interactive=True,
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

    commands = (
        "status",
        "replace",
        "disable",
        "show",
        "help",
        "exit",
        "quit",
    )
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
    elif words[0] == "show" and (
        len(words) == 1
        or (len(words) == 2 and not stripped[-1:].isspace())
    ):
        candidates = ("statistics",)
    elif words[:2] == ["show", "statistics"] and (
        len(words) == 2
        or (len(words) == 3 and not stripped[-1:].isspace())
    ):
        candidates = ("inputs", "outputs")
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
    if args.command == "show" and args.show_command == "statistics":
        statistics_command = getattr(args, "statistics_command", None)
        if statistics_command == "inputs":
            return build_runtime_statistics_inputs_request(
                request_id,
                args.input_filter,
            )
        if statistics_command == "outputs":
            return build_runtime_statistics_outputs_request(
                request_id,
                args.output_filter,
            )
        return build_runtime_statistics_request(request_id)
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


def _is_statistics_command(args: argparse.Namespace) -> bool:
    return (
        args.command == "show"
        and getattr(args, "show_command", None) == "statistics"
    )


def _format_interactive_statistics(
    args: argparse.Namespace,
    result: object,
) -> str:
    statistics_command = getattr(args, "statistics_command", None)
    if statistics_command == "inputs":
        return format_runtime_statistics_inputs(result)
    if statistics_command == "outputs":
        return format_runtime_statistics_outputs(result)
    return format_runtime_statistics(result)


_QUEUE_RESULT_FIELDS = (
    "name",
    "capacity",
    "depth",
    "peak_depth",
    "enqueued",
    "dequeued",
    "put_waits",
    "current_put_waiters",
)
_QUEUE_HEADERS = (
    "NAME",
    "CAPACITY",
    "DEPTH",
    "PEAK",
    "ENQUEUED",
    "DEQUEUED",
    "PUT WAITS",
    "WAITERS",
)
_PROCESSOR_RESULT_FIELDS = (
    "process_calls",
    "process_completed",
    "process_failed",
    "process_in_flight",
    "outputless_calls",
    "output_batches",
    "output_messages",
    "reset_calls",
    "reset_completed",
    "reset_failed",
    "reset_in_flight",
)
_EGRESS_RESULT_FIELDS = (
    "batches_started",
    "batches_completed",
    "batches_failed",
    "batches_cancelled",
    "active_batches",
    "outputs_started",
    "outputs_completed",
    "outputs_failed",
    "outputs_cancelled",
    "active_outputs",
)
_STATISTICS_RESULT_FIELDS = (
    "ingress_queues",
    "processing_queue",
    "processor",
    "egress_queue",
    "egress_operations",
)
_INPUT_TRAFFIC_RESULT_FIELDS = (
    "name",
    "kind",
    "transport_packets",
    "transport_bytes",
    "accepted_frames",
    "payload_bytes",
)
_INPUT_TRAFFIC_HEADERS = (
    "INPUT",
    "KIND",
    "TRANSPORT PACKETS",
    "TRANSPORT BYTES",
    "ACCEPTED FRAMES",
    "PAYLOAD BYTES",
)
_OUTPUT_TRAFFIC_RESULT_FIELDS = (
    "target_id",
    "name",
    "dispatch_attempts",
    "dispatch_completed",
    "dispatch_failed",
    "messages",
    "bytes",
)
_OUTPUT_TRAFFIC_HEADERS = (
    "TARGET ID",
    "NAME",
    "ATTEMPTS",
    "COMPLETED",
    "FAILED",
    "MESSAGES",
    "BYTES",
)


def format_runtime_statistics(result: object) -> str:
    """Render one successful statistics result as deterministic ASCII tables."""

    statistics = _require_exact_statistics_mapping(
        result,
        _STATISTICS_RESULT_FIELDS,
        "runtime statistics result",
    )
    ingress_value = statistics["ingress_queues"]
    if (
        not isinstance(ingress_value, Sequence)
        or isinstance(ingress_value, (str, bytes, bytearray))
    ):
        raise RoutingControlResponseError(
            "Runtime statistics ingress_queues field is invalid."
        )

    ingress_rows = tuple(
        _queue_table_row(queue, f"ingress_queues[{index}]")
        for index, queue in enumerate(ingress_value)
    )
    processing_row = _queue_table_row(
        statistics["processing_queue"],
        "processing_queue",
    )
    egress_row = _queue_table_row(
        statistics["egress_queue"],
        "egress_queue",
    )

    processor = _require_counter_mapping(
        statistics["processor"],
        _PROCESSOR_RESULT_FIELDS,
        "processor",
    )
    processor_rows = tuple(
        (field_name, str(processor[field_name]))
        for field_name in _PROCESSOR_RESULT_FIELDS
    )

    egress = _require_counter_mapping(
        statistics["egress_operations"],
        _EGRESS_RESULT_FIELDS,
        "egress_operations",
    )
    egress_rows = (
        (
            "BATCHES",
            str(egress["batches_started"]),
            str(egress["batches_completed"]),
            str(egress["batches_failed"]),
            str(egress["batches_cancelled"]),
            str(egress["active_batches"]),
        ),
        (
            "OUTPUTS",
            str(egress["outputs_started"]),
            str(egress["outputs_completed"]),
            str(egress["outputs_failed"]),
            str(egress["outputs_cancelled"]),
            str(egress["active_outputs"]),
        ),
    )

    sections = (
        "Ingress queues\n" + _format_ascii_table(_QUEUE_HEADERS, ingress_rows),
        "Processing queue\n"
        + _format_ascii_table(_QUEUE_HEADERS, (processing_row,)),
        "Processor\n"
        + _format_ascii_table(("METRIC", "VALUE"), processor_rows),
        "Egress queue\n" + _format_ascii_table(_QUEUE_HEADERS, (egress_row,)),
        "Local egress operations\n"
        + _format_ascii_table(
            ("TYPE", "STARTED", "COMPLETED", "FAILED", "CANCELLED", "ACTIVE"),
            egress_rows,
        ),
    )
    return "\n\n".join(sections) + "\n"


def format_runtime_statistics_inputs(result: object) -> str:
    """Render detailed per-input traffic as one deterministic ASCII table."""

    statistics = _require_exact_statistics_mapping(
        result,
        ("inputs",),
        "input traffic result",
    )
    inputs = _require_statistics_sequence(
        statistics["inputs"],
        "input traffic inputs",
    )
    rows = tuple(
        _input_traffic_table_row(value, f"inputs[{index}]")
        for index, value in enumerate(inputs)
    )
    return _format_ascii_table(_INPUT_TRAFFIC_HEADERS, rows) + "\n"


def _input_traffic_table_row(
    value: object,
    description: str,
) -> tuple[str, ...]:
    row = _require_exact_statistics_mapping(
        value,
        _INPUT_TRAFFIC_RESULT_FIELDS,
        description,
    )
    name = row["name"]
    if not isinstance(name, str) or not name:
        raise RoutingControlResponseError(
            f"Runtime statistics {description} name is invalid."
        )
    kind = row["kind"]
    if not isinstance(kind, str) or kind not in {"udp", "udpsec"}:
        raise RoutingControlResponseError(
            f"Runtime statistics {description} kind is invalid."
        )
    for field_name in _INPUT_TRAFFIC_RESULT_FIELDS[2:]:
        _require_counter(row[field_name], f"{description}.{field_name}")
    return tuple(str(row[field_name]) for field_name in _INPUT_TRAFFIC_RESULT_FIELDS)


def format_runtime_statistics_outputs(result: object) -> str:
    """Render detailed per-output local traffic as one ASCII table."""

    statistics = _require_exact_statistics_mapping(
        result,
        ("outputs",),
        "output traffic result",
    )
    outputs = _require_statistics_sequence(
        statistics["outputs"],
        "output traffic outputs",
    )
    rows = tuple(
        _output_traffic_table_row(value, f"outputs[{index}]")
        for index, value in enumerate(outputs)
    )
    return _format_ascii_table(_OUTPUT_TRAFFIC_HEADERS, rows) + "\n"


def _output_traffic_table_row(
    value: object,
    description: str,
) -> tuple[str, ...]:
    row = _require_exact_statistics_mapping(
        value,
        _OUTPUT_TRAFFIC_RESULT_FIELDS,
        description,
    )
    _require_counter(row["target_id"], f"{description}.target_id")
    name = row["name"]
    if name is not None and (not isinstance(name, str) or not name):
        raise RoutingControlResponseError(
            f"Runtime statistics {description} name is invalid."
        )
    for field_name in _OUTPUT_TRAFFIC_RESULT_FIELDS[2:]:
        _require_counter(row[field_name], f"{description}.{field_name}")
    return (
        str(row["target_id"]),
        "-" if name is None else name,
        *(str(row[field_name]) for field_name in _OUTPUT_TRAFFIC_RESULT_FIELDS[2:]),
    )


def _require_statistics_sequence(
    value: object,
    description: str,
) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(
        value,
        (str, bytes, bytearray),
    ):
        raise RoutingControlResponseError(
            f"Runtime statistics {description} field is invalid."
        )
    return value


def _queue_table_row(value: object, description: str) -> tuple[str, ...]:
    queue = _require_exact_statistics_mapping(
        value,
        _QUEUE_RESULT_FIELDS,
        description,
    )
    name = queue["name"]
    if not isinstance(name, str) or not name:
        raise RoutingControlResponseError(
            f"Runtime statistics {description} name is invalid."
        )
    for field_name in _QUEUE_RESULT_FIELDS[1:]:
        _require_counter(queue[field_name], f"{description}.{field_name}")
    return tuple(str(queue[field_name]) for field_name in _QUEUE_RESULT_FIELDS)


def _require_counter_mapping(
    value: object,
    fields: tuple[str, ...],
    description: str,
) -> Mapping[str, object]:
    mapping = _require_exact_statistics_mapping(value, fields, description)
    for field_name in fields:
        _require_counter(mapping[field_name], f"{description}.{field_name}")
    return mapping


def _require_exact_statistics_mapping(
    value: object,
    fields: tuple[str, ...],
    description: str,
) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != set(fields):
        raise RoutingControlResponseError(
            f"Runtime statistics {description} fields are invalid."
        )
    return value


def _require_counter(value: object, description: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RoutingControlResponseError(
            f"Runtime statistics {description} is invalid."
        )


def _format_ascii_table(
    headers: tuple[str, ...],
    rows: Sequence[tuple[str, ...]],
) -> str:
    widths = [len(header) for header in headers]
    for row in rows:
        if len(row) != len(headers):
            raise AssertionError("table row width does not match its headers")
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))

    header_line = "  ".join(
        header.ljust(widths[index])
        for index, header in enumerate(headers)
    ).rstrip()
    separator = "  ".join("-" * width for width in widths)
    data_lines = tuple(
        "  ".join(
            value.ljust(widths[index]) if index == 0 else value.rjust(widths[index])
            for index, value in enumerate(row)
        ).rstrip()
        for row in rows
    )
    return "\n".join((header_line, separator, *data_lines))


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
