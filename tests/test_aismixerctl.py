import json
from io import StringIO
from pathlib import Path

import pytest

import aismixerctl
from core.routing_control_protocol import (
    ERROR_STALE_GENERATION,
    ROUTING_CONTROL_PROTOCOL_VERSION,
)
from core.routing_control_unix_client import (
    RoutingControlConnectionError,
    RoutingControlResponseError,
)


def routing_section():
    return {
        "zones": {"source": {"include": ["udp:source"]}},
        "routes": [
            {
                "name": "source_to_a",
                "from_zone": "source",
                "to": ["udp:a"],
            }
        ],
    }


def success_response(request_id="req-1"):
    return {
        "version": ROUTING_CONTROL_PROTOCOL_VERSION,
        "request_id": request_id,
        "ok": True,
        "result": {
            "generation": 0,
            "enabled": False,
            "zone_names": [],
            "route_names": [],
            "target_ids": [],
        },
    }


def server_error_response(request_id="req-1"):
    return {
        "version": ROUTING_CONTROL_PROTOCOL_VERSION,
        "request_id": request_id,
        "ok": False,
        "error": {
            "code": ERROR_STALE_GENERATION,
            "message": "Routing generation is stale.",
            "expected_generation": 3,
            "actual_generation": 4,
        },
    }


class FakeClient:
    calls = []
    response = success_response()
    exception = None

    def __init__(self, socket_path):
        self.socket_path = socket_path

    async def request(self, request):
        type(self).calls.append((self.socket_path, request))
        if type(self).exception is not None:
            raise type(self).exception
        return type(self).response


@pytest.fixture(autouse=True)
def reset_fake_client():
    FakeClient.calls = []
    FakeClient.response = success_response()
    FakeClient.exception = None


class ScriptedInput:
    def __init__(self, *events):
        self.events = list(events)
        self.prompts = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        if not self.events:
            raise AssertionError("interactive shell requested unexpected input")
        event = self.events.pop(0)
        if isinstance(event, BaseException):
            raise event
        return event


class FakeReadline:
    def __init__(self, *, fail_read=False, fail_write=False, fail_setup=False):
        self.fail_read = fail_read
        self.fail_write = fail_write
        self.fail_setup = fail_setup
        self.read_paths = []
        self.write_paths = []
        self.bindings = []
        self.installed_completers = []
        self.history = []
        self._completer = None
        self._completer_delims = " \t\n"
        self._line_buffer = ""

    def read_history_file(self, path):
        path = Path(path)
        self.read_paths.append(path)
        if self.fail_read:
            raise OSError("history read failed")
        if not path.exists():
            raise FileNotFoundError(path)
        self.history = path.read_text(encoding="utf-8").splitlines()

    def write_history_file(self, path):
        path = Path(path)
        self.write_paths.append(path)
        if self.fail_write:
            raise OSError("history write failed")
        text = "\n".join(self.history)
        if text:
            text += "\n"
        path.write_text(text, encoding="utf-8")

    def set_completer(self, completer):
        if self.fail_setup:
            raise OSError("completion setup failed")
        self._completer = completer
        self.installed_completers.append(completer)

    def get_completer(self):
        return self._completer

    def parse_and_bind(self, binding):
        if self.fail_setup:
            raise OSError("completion binding failed")
        self.bindings.append(binding)

    def get_completer_delims(self):
        return self._completer_delims

    def set_completer_delims(self, delimiters):
        if self.fail_setup:
            raise OSError("completion delimiters failed")
        self._completer_delims = delimiters

    def get_line_buffer(self):
        return self._line_buffer

    def get_current_history_length(self):
        return len(self.history)

    def get_history_item(self, index):
        if 1 <= index <= len(self.history):
            return self.history[index - 1]
        return None

    def add_history(self, line):
        self.history.append(line)

    def remove_history_item(self, index):
        del self.history[index]

    def replace_history_item(self, index, line):
        self.history[index] = line

    def clear_history(self):
        self.history.clear()

    def set_history_length(self, _length):
        pass

    def set_auto_history(self, _enabled):
        pass


def run_shell(
    tmp_path,
    events,
    *,
    argv=None,
    client_factory=FakeClient,
    generated_request_id=None,
    environ=None,
    readline_module=None,
):
    input_func = ScriptedInput(*events)
    stdout = StringIO()
    stderr = StringIO()
    rc = aismixerctl.main(
        [] if argv is None else argv,
        client_factory=client_factory,
        generated_request_id=generated_request_id,
        input_func=input_func,
        stdout=stdout,
        stderr=stderr,
        environ={} if environ is None else environ,
        home_directory=tmp_path,
        readline_module=readline_module,
    )
    return rc, input_func, stdout.getvalue(), stderr.getvalue()


def test_status_request_shape():
    assert aismixerctl.build_status_request("req-1") == {
        "version": ROUTING_CONTROL_PROTOCOL_VERSION,
        "request_id": "req-1",
        "method": "routing.status",
    }


def test_default_socket_path_is_operational_runtime_socket():
    assert aismixerctl.DEFAULT_SOCKET_PATH == "/run/aismixer/control.sock"


def test_disable_request_shape():
    assert aismixerctl.build_disable_request("req-1") == {
        "version": ROUTING_CONTROL_PROTOCOL_VERSION,
        "request_id": "req-1",
        "method": "routing.disable",
    }


def test_disable_with_expected_generation():
    assert aismixerctl.build_disable_request(
        "req-1",
        expected_generation=4,
    )["params"] == {"expected_generation": 4}


def test_replace_request_shape():
    section = routing_section()

    request = aismixerctl.build_replace_request("req-1", section)

    assert request == {
        "version": ROUTING_CONTROL_PROTOCOL_VERSION,
        "request_id": "req-1",
        "method": "routing.replace",
        "params": {"routing": section},
    }


def test_replace_with_expected_generation():
    request = aismixerctl.build_replace_request(
        "req-1",
        routing_section(),
        expected_generation=3,
    )

    assert request["params"]["expected_generation"] == 3


def test_generated_request_id_can_be_injected():
    assert (
        aismixerctl.build_request_id(None, generated_request_id=lambda: "generated")
        == "generated"
    )


def test_explicit_request_id_is_preserved():
    assert aismixerctl.build_request_id("operator-1") == "operator-1"


def test_empty_explicit_request_id_is_rejected():
    with pytest.raises(aismixerctl.AismixerCtlInputError):
        aismixerctl.build_request_id("")


def test_negative_expected_generation_is_rejected():
    with pytest.raises(aismixerctl.AismixerCtlInputError):
        aismixerctl.build_disable_request("req-1", expected_generation=-1)


def test_bool_expected_generation_is_rejected():
    with pytest.raises(aismixerctl.AismixerCtlInputError):
        aismixerctl.build_replace_request(
            "req-1",
            routing_section(),
            expected_generation=True,
        )


def test_full_config_routing_extraction():
    section = routing_section()

    assert aismixerctl.extract_routing_section({"routing": section, "udp": []}) is section


def test_direct_routing_section_extraction():
    section = routing_section()

    assert aismixerctl.extract_routing_section(section) is section


def test_routing_null_is_rejected():
    with pytest.raises(aismixerctl.AismixerCtlInputError, match="disable"):
        aismixerctl.extract_routing_section({"routing": None})


def test_malformed_yaml_is_rejected(tmp_path):
    path = tmp_path / "routing.yaml"
    path.write_text("routing: [", encoding="utf-8")

    with pytest.raises(aismixerctl.AismixerCtlInputError, match="invalid YAML"):
        aismixerctl.load_routing_section_file(path)


def test_non_mapping_yaml_root_is_rejected(tmp_path):
    path = tmp_path / "routing.yaml"
    path.write_text("- not\n- a mapping\n", encoding="utf-8")

    with pytest.raises(aismixerctl.AismixerCtlInputError, match="root"):
        aismixerctl.load_routing_section_file(path)


def test_missing_file_is_rejected(tmp_path):
    with pytest.raises(aismixerctl.AismixerCtlInputError, match="not found"):
        aismixerctl.load_routing_section_file(tmp_path / "missing.yaml")


def test_missing_usable_routing_section_is_rejected():
    with pytest.raises(aismixerctl.AismixerCtlInputError, match="usable"):
        aismixerctl.extract_routing_section({"zones": {}, "extra": True})


def test_invalid_input_prevents_client_request(tmp_path, capsys):
    missing = tmp_path / "missing.yaml"

    rc = aismixerctl.main(
        ["--socket", "control.sock", "replace", "--file", str(missing)],
        client_factory=FakeClient,
        generated_request_id=lambda: "req-1",
    )

    captured = capsys.readouterr()
    assert rc == aismixerctl.EXIT_USAGE_OR_INPUT
    assert FakeClient.calls == []
    assert "aismixerctl:" in captured.err


def test_main_status_uses_generated_request_id(capsys):
    rc = aismixerctl.main(
        ["--socket", "control.sock", "status"],
        client_factory=FakeClient,
        generated_request_id=lambda: "generated",
    )

    captured = capsys.readouterr()
    assert rc == aismixerctl.EXIT_OK
    assert FakeClient.calls == [
        (
            "control.sock",
            {
                "version": ROUTING_CONTROL_PROTOCOL_VERSION,
                "request_id": "generated",
                "method": "routing.status",
            },
        )
    ]
    assert json.loads(captured.out)["ok"] is True


def test_main_status_uses_default_socket_path_without_socket_option(capsys):
    rc = aismixerctl.main(
        ["status"],
        client_factory=FakeClient,
        generated_request_id=lambda: "generated",
    )

    captured = capsys.readouterr()
    assert rc == aismixerctl.EXIT_OK
    assert FakeClient.calls == [
        (
            aismixerctl.DEFAULT_SOCKET_PATH,
            {
                "version": ROUTING_CONTROL_PROTOCOL_VERSION,
                "request_id": "generated",
                "method": "routing.status",
            },
        )
    ]
    assert json.loads(captured.out)["ok"] is True


def test_explicit_socket_overrides_default_socket_path(capsys):
    rc = aismixerctl.main(
        ["--socket", "/custom/path.sock", "status"],
        client_factory=FakeClient,
        generated_request_id=lambda: "generated",
    )

    assert rc == aismixerctl.EXIT_OK
    assert FakeClient.calls[0][0] == "/custom/path.sock"
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_main_preserves_explicit_request_id(capsys):
    FakeClient.response = success_response("operator-1")

    rc = aismixerctl.main(
        ["--socket", "control.sock", "--request-id", "operator-1", "status"],
        client_factory=FakeClient,
    )

    assert rc == aismixerctl.EXIT_OK
    assert FakeClient.calls[0][1]["request_id"] == "operator-1"
    assert json.loads(capsys.readouterr().out)["request_id"] == "operator-1"


def test_main_empty_explicit_request_id_is_rejected(capsys):
    rc = aismixerctl.main(
        ["--socket", "control.sock", "--request-id", "", "status"],
        client_factory=FakeClient,
    )

    captured = capsys.readouterr()
    assert rc == aismixerctl.EXIT_USAGE_OR_INPUT
    assert FakeClient.calls == []
    assert "Traceback" not in captured.err


def test_main_replace_loads_file_and_sends_request(tmp_path, capsys):
    path = tmp_path / "routing.yaml"
    path.write_text(
        """
zones:
  source:
    include:
      - udp:source
routes:
  - name: source_to_a
    from_zone: source
    to:
      - udp:a
""".lstrip(),
        encoding="utf-8",
    )

    rc = aismixerctl.main(
        [
            "--socket",
            "control.sock",
            "replace",
            "--file",
            str(path),
            "--expected-generation",
            "3",
        ],
        client_factory=FakeClient,
        generated_request_id=lambda: "req-1",
    )

    assert rc == aismixerctl.EXIT_OK
    request = FakeClient.calls[0][1]
    assert request["method"] == "routing.replace"
    assert request["params"]["expected_generation"] == 3
    assert request["params"]["routing"]["zones"]["source"]["include"] == ["udp:source"]
    assert capsys.readouterr().err == ""


def test_main_disable_with_expected_generation(capsys):
    rc = aismixerctl.main(
        [
            "--socket",
            "control.sock",
            "disable",
            "--expected-generation",
            "4",
        ],
        client_factory=FakeClient,
        generated_request_id=lambda: "req-1",
    )

    assert rc == aismixerctl.EXIT_OK
    assert FakeClient.calls[0][1] == {
        "version": ROUTING_CONTROL_PROTOCOL_VERSION,
        "request_id": "req-1",
        "method": "routing.disable",
        "params": {"expected_generation": 4},
    }
    assert capsys.readouterr().err == ""


def test_argparse_negative_expected_generation_is_rejected(capsys):
    rc = aismixerctl.main(
        [
            "--socket",
            "control.sock",
            "disable",
            "--expected-generation",
            "-1",
        ],
        client_factory=FakeClient,
    )

    captured = capsys.readouterr()
    assert rc == aismixerctl.EXIT_USAGE_OR_INPUT
    assert FakeClient.calls == []
    assert "Traceback" not in captured.err


def test_compact_output(capsys):
    rc = aismixerctl.main(
        ["--socket", "control.sock", "status"],
        client_factory=FakeClient,
        generated_request_id=lambda: "req-1",
    )

    captured = capsys.readouterr()
    assert rc == aismixerctl.EXIT_OK
    assert captured.out.endswith("\n")
    assert not captured.out.endswith("\n\n")
    assert "\n" not in captured.out[:-1]
    assert json.loads(captured.out) == success_response()


def test_pretty_output(capsys):
    rc = aismixerctl.main(
        ["--socket", "control.sock", "--pretty", "status"],
        client_factory=FakeClient,
        generated_request_id=lambda: "req-1",
    )

    captured = capsys.readouterr()
    assert rc == aismixerctl.EXIT_OK
    assert captured.out.endswith("\n")
    assert "\n  " in captured.out
    assert json.loads(captured.out) == success_response()


def test_server_error_exit_code_and_stderr_output(capsys):
    FakeClient.response = server_error_response()

    rc = aismixerctl.main(
        ["--socket", "control.sock", "status"],
        client_factory=FakeClient,
        generated_request_id=lambda: "req-1",
    )

    captured = capsys.readouterr()
    assert rc == aismixerctl.EXIT_PROTOCOL_ERROR
    assert captured.out == ""
    assert json.loads(captured.err)["error"]["code"] == ERROR_STALE_GENERATION
    assert "actual_generation" in captured.err


def test_connection_error_exit_code(capsys):
    FakeClient.exception = RoutingControlConnectionError("connection failed")

    rc = aismixerctl.main(
        ["--socket", "control.sock", "status"],
        client_factory=FakeClient,
        generated_request_id=lambda: "req-1",
    )

    captured = capsys.readouterr()
    assert rc == aismixerctl.EXIT_CONNECTION_ERROR
    assert "aismixerctl: connection failed" in captured.err
    assert "Traceback" not in captured.err


def test_invalid_response_exit_code(capsys):
    FakeClient.exception = RoutingControlResponseError("bad response")

    rc = aismixerctl.main(
        ["--socket", "control.sock", "status"],
        client_factory=FakeClient,
        generated_request_id=lambda: "req-1",
    )

    captured = capsys.readouterr()
    assert rc == aismixerctl.EXIT_INVALID_RESPONSE
    assert "aismixerctl: bad response" in captured.err
    assert "Traceback" not in captured.err


def test_unexpected_cli_defect_has_no_traceback(capsys):
    class BrokenClient:
        def __init__(self, _socket_path):
            pass

        async def request(self, _request):
            raise RuntimeError("secret defect detail")

    rc = aismixerctl.main(
        ["--socket", "control.sock", "status"],
        client_factory=BrokenClient,
        generated_request_id=lambda: "req-1",
    )

    captured = capsys.readouterr()
    assert rc == aismixerctl.EXIT_INTERNAL_ERROR
    assert "aismixerctl: internal error" in captured.err
    assert "secret defect detail" not in captured.err
    assert "Traceback" not in captured.err


def test_no_command_starts_shell_without_eager_connection_or_request_id(tmp_path):
    constructed = []
    generated = []

    def client_factory(socket_path):
        constructed.append(socket_path)
        return FakeClient(socket_path)

    def request_id_generator():
        generated.append(True)
        return "unexpected"

    rc, input_func, _stdout, stderr = run_shell(
        tmp_path,
        ["exit"],
        client_factory=client_factory,
        generated_request_id=request_id_generator,
    )

    assert rc == aismixerctl.EXIT_OK
    assert aismixerctl.SHELL_PROMPT.strip() == "aismixerctl>"
    assert input_func.prompts == [aismixerctl.SHELL_PROMPT]
    assert constructed == []
    assert generated == []
    assert stderr == ""


@pytest.mark.parametrize("termination_command", ["exit", "quit"])
def test_shell_termination_commands_are_local(tmp_path, termination_command):
    def fail_request_id_generation():
        raise AssertionError("local command generated a request ID")

    rc, _input_func, _stdout, stderr = run_shell(
        tmp_path,
        [termination_command],
        generated_request_id=fail_request_id_generation,
    )

    assert rc == aismixerctl.EXIT_OK
    assert FakeClient.calls == []
    assert stderr == ""


def test_shell_help_and_empty_lines_are_local(tmp_path):
    def fail_request_id_generation():
        raise AssertionError("local input generated a request ID")

    rc, input_func, stdout, stderr = run_shell(
        tmp_path,
        ["", "   ", "help", "exit"],
        generated_request_id=fail_request_id_generation,
    )

    assert rc == aismixerctl.EXIT_OK
    assert len(input_func.prompts) == 4
    assert FakeClient.calls == []
    for command in ("status", "replace", "disable", "help", "exit", "quit"):
        assert command in stdout
    assert stderr == ""


def test_shell_inherits_global_socket_and_pretty_options(tmp_path):
    rc, _input_func, stdout, stderr = run_shell(
        tmp_path,
        ["status", "exit"],
        argv=["--socket", "/custom/control.sock", "--pretty"],
        generated_request_id=lambda: "shell-1",
    )

    assert rc == aismixerctl.EXIT_OK
    assert FakeClient.calls[0][0] == "/custom/control.sock"
    assert FakeClient.calls[0][1]["request_id"] == "shell-1"
    assert "\n  " in stdout
    assert json.loads(stdout)["ok"] is True
    assert stderr == ""


def test_shell_remote_commands_use_fresh_ids_and_parse_quoted_file_path(tmp_path):
    routing_path = tmp_path / "routing configs" / "test routing.yaml"
    routing_path.parent.mkdir()
    routing_path.write_text(
        """
zones:
  source:
    include:
      - udp:source
routes:
  - name: source_to_a
    from_zone: source
    to:
      - udp:a
""".lstrip(),
        encoding="utf-8",
    )
    request_ids = iter(["shell-1", "shell-2", "shell-3"])

    rc, _input_func, _stdout, stderr = run_shell(
        tmp_path,
        [
            "status",
            f'replace --file "{routing_path.as_posix()}" --expected-generation 3',
            "disable --expected-generation 4",
            "exit",
        ],
        generated_request_id=lambda: next(request_ids),
    )

    assert rc == aismixerctl.EXIT_OK
    assert [call[1]["request_id"] for call in FakeClient.calls] == [
        "shell-1",
        "shell-2",
        "shell-3",
    ]
    assert [call[1]["method"] for call in FakeClient.calls] == [
        "routing.status",
        "routing.replace",
        "routing.disable",
    ]
    replace_request = FakeClient.calls[1][1]
    assert replace_request["params"]["expected_generation"] == 3
    assert replace_request["params"]["routing"]["zones"]["source"]["include"] == [
        "udp:source"
    ]
    assert FakeClient.calls[2][1]["params"] == {"expected_generation": 4}
    assert stderr == ""


def test_one_shot_and_shell_commands_call_shared_dispatcher(monkeypatch, tmp_path):
    original_dispatch = aismixerctl.dispatch_command
    dispatch_calls = []

    def recording_dispatch(*args, **kwargs):
        dispatch_calls.append((args, kwargs))
        return original_dispatch(*args, **kwargs)

    monkeypatch.setattr(aismixerctl, "dispatch_command", recording_dispatch)
    stdout = StringIO()
    stderr = StringIO()

    one_shot_rc = aismixerctl.main(
        ["status"],
        client_factory=FakeClient,
        generated_request_id=lambda: "one-shot",
        stdout=stdout,
        stderr=stderr,
    )
    calls_after_one_shot = len(dispatch_calls)
    shell_rc, _input_func, _shell_stdout, shell_stderr = run_shell(
        tmp_path,
        ["disable", "exit"],
        generated_request_id=lambda: "shell",
    )

    assert one_shot_rc == aismixerctl.EXIT_OK
    assert calls_after_one_shot >= 1
    assert shell_rc == aismixerctl.EXIT_OK
    assert len(dispatch_calls) > calls_after_one_shot
    assert stderr.getvalue() == ""
    assert shell_stderr == ""


def test_shell_parse_and_argument_errors_return_to_prompt(tmp_path):
    request_ids = iter(["valid-request"])

    rc, input_func, _stdout, stderr = run_shell(
        tmp_path,
        [
            'replace --file "unterminated',
            "unknown-command",
            "replace",
            "disable --expected-generation not-a-number",
            "status unexpected-argument",
            "status",
            "exit",
        ],
        generated_request_id=lambda: next(request_ids),
    )

    assert rc == aismixerctl.EXIT_OK
    assert len(input_func.prompts) == 7
    assert len(FakeClient.calls) == 1
    assert FakeClient.calls[0][1]["method"] == "routing.status"
    assert FakeClient.calls[0][1]["request_id"] == "valid-request"
    assert stderr
    assert "Traceback" not in stderr


def test_shell_eof_prints_final_newline_and_exits_cleanly(tmp_path):
    rc, input_func, stdout, stderr = run_shell(tmp_path, [EOFError()])

    assert rc == aismixerctl.EXIT_OK
    assert input_func.prompts == [aismixerctl.SHELL_PROMPT]
    assert stdout.endswith("\n")
    assert FakeClient.calls == []
    assert "Traceback" not in stderr


def test_shell_keyboard_interrupt_while_editing_reprompts(tmp_path):
    rc, input_func, stdout, stderr = run_shell(
        tmp_path,
        [KeyboardInterrupt(), "exit"],
    )

    assert rc == aismixerctl.EXIT_OK
    assert input_func.prompts == [aismixerctl.SHELL_PROMPT, aismixerctl.SHELL_PROMPT]
    assert stdout.endswith("\n")
    assert FakeClient.calls == []
    assert "Traceback" not in stderr


def test_shell_keyboard_interrupt_while_executing_reprompts(tmp_path):
    outcomes = [KeyboardInterrupt(), success_response("shell-2")]
    calls = []

    class InterruptOnceClient:
        def __init__(self, socket_path):
            self.socket_path = socket_path

        async def request(self, request):
            calls.append((self.socket_path, request))
            outcome = outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

    request_ids = iter(["shell-1", "shell-2"])
    rc, input_func, stdout, stderr = run_shell(
        tmp_path,
        ["status", "status", "exit"],
        client_factory=InterruptOnceClient,
        generated_request_id=lambda: next(request_ids),
    )

    assert rc == aismixerctl.EXIT_OK
    assert len(input_func.prompts) == 3
    assert [call[1]["request_id"] for call in calls] == ["shell-1", "shell-2"]
    assert json.loads(stdout)["request_id"] == "shell-2"
    assert "Traceback" not in stderr


def test_one_shot_keyboard_interrupt_keeps_interrupted_exit_code():
    class InterruptingClient:
        def __init__(self, _socket_path):
            pass

        async def request(self, _request):
            raise KeyboardInterrupt

    rc = aismixerctl.main(
        ["status"],
        client_factory=InterruptingClient,
        generated_request_id=lambda: "req-1",
        stdout=StringIO(),
        stderr=StringIO(),
    )

    assert rc == aismixerctl.EXIT_INTERRUPTED


def test_shell_request_id_option_is_rejected_before_prompt(tmp_path):
    generated = []

    def request_id_generator():
        generated.append(True)
        return "unexpected"

    rc, input_func, _stdout, stderr = run_shell(
        tmp_path,
        ["exit"],
        argv=["--request-id", "fixed-for-session"],
        generated_request_id=request_id_generator,
    )

    assert rc == aismixerctl.EXIT_USAGE_OR_INPUT
    assert input_func.prompts == []
    assert FakeClient.calls == []
    assert generated == []
    assert "request-id" in stderr
    assert "interactive" in stderr.lower()
    assert "Traceback" not in stderr


def test_shell_remote_error_does_not_end_session(tmp_path):
    outcomes = [
        RoutingControlConnectionError("connection failed"),
        success_response("shell-2"),
    ]
    calls = []

    class RecoveringClient:
        def __init__(self, socket_path):
            self.socket_path = socket_path

        async def request(self, request):
            calls.append((self.socket_path, request))
            outcome = outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

    request_ids = iter(["shell-1", "shell-2"])
    rc, _input_func, stdout, stderr = run_shell(
        tmp_path,
        ["status", "status", "exit"],
        client_factory=RecoveringClient,
        generated_request_id=lambda: next(request_ids),
    )

    assert rc == aismixerctl.EXIT_OK
    assert len(calls) == 2
    assert json.loads(stdout)["request_id"] == "shell-2"
    assert "aismixerctl: connection failed" in stderr
    assert "Traceback" not in stderr


def test_xdg_history_path_resolution(tmp_path):
    state_home = tmp_path / "state"

    resolved = aismixerctl.resolve_history_path(
        environ={"XDG_STATE_HOME": str(state_home)},
        home_directory=tmp_path / "ignored-home",
    )

    assert Path(resolved) == state_home / "aismixer" / "aismixerctl_history"


@pytest.mark.parametrize("environ", [{}, {"XDG_STATE_HOME": ""}])
def test_history_path_falls_back_to_local_state_home(tmp_path, environ):
    home = tmp_path / "operator-home"

    resolved = aismixerctl.resolve_history_path(
        environ=environ,
        home_directory=home,
    )

    assert Path(resolved) == (
        home / ".local" / "state" / "aismixer" / "aismixerctl_history"
    )


def test_shell_loads_and_saves_history_and_installs_completion(tmp_path):
    state_home = tmp_path / "state"
    history_path = state_home / "aismixer" / "aismixerctl_history"
    history_path.parent.mkdir(parents=True)
    history_path.write_text("existing-command\n", encoding="utf-8")
    readline_module = FakeReadline()

    rc, _input_func, _stdout, stderr = run_shell(
        tmp_path,
        ["exit"],
        environ={"XDG_STATE_HOME": str(state_home)},
        readline_module=readline_module,
    )

    assert rc == aismixerctl.EXIT_OK
    assert readline_module.read_paths == [history_path]
    assert readline_module.write_paths == [history_path]
    assert "existing-command" in history_path.read_text(encoding="utf-8")
    assert any(callable(value) for value in readline_module.installed_completers)
    assert readline_module.bindings
    assert stderr == ""


def test_shell_history_skips_empty_and_immediate_duplicate_lines(tmp_path):
    state_home = tmp_path / "state"
    history_path = state_home / "aismixer" / "aismixerctl_history"
    readline_module = FakeReadline()

    rc, _input_func, _stdout, stderr = run_shell(
        tmp_path,
        ["status", "", "status", "exit"],
        environ={"XDG_STATE_HOME": str(state_home)},
        readline_module=readline_module,
        generated_request_id=lambda: "req-1",
    )

    assert rc == aismixerctl.EXIT_OK
    assert history_path.read_text(encoding="utf-8").splitlines() == [
        "status",
        "exit",
    ]
    assert stderr == ""


def test_shell_history_is_bounded_in_memory_and_on_disk(tmp_path):
    state_home = tmp_path / "state"
    history_path = state_home / "aismixer" / "aismixerctl_history"
    history_path.parent.mkdir(parents=True)
    history_path.write_text(
        "".join(f"entry-{index}\n" for index in range(1002)),
        encoding="utf-8",
    )
    readline_module = FakeReadline()

    rc, _input_func, _stdout, stderr = run_shell(
        tmp_path,
        ["exit"],
        environ={"XDG_STATE_HOME": str(state_home)},
        readline_module=readline_module,
    )

    persisted = history_path.read_text(encoding="utf-8").splitlines()
    assert rc == aismixerctl.EXIT_OK
    assert len(readline_module.history) == 1000
    assert len(persisted) == 1000
    assert persisted[0] == "entry-3"
    assert persisted[-1] == "exit"
    assert stderr == ""


def test_shell_creates_history_parent_directory_when_saving(tmp_path):
    state_home = tmp_path / "new-state"
    history_path = state_home / "aismixer" / "aismixerctl_history"
    readline_module = FakeReadline()

    rc, _input_func, _stdout, stderr = run_shell(
        tmp_path,
        ["quit"],
        environ={"XDG_STATE_HOME": str(state_home)},
        readline_module=readline_module,
    )

    assert rc == aismixerctl.EXIT_OK
    assert history_path.parent.is_dir()
    assert readline_module.write_paths == [history_path]
    assert stderr == ""


def test_shell_without_readline_support_still_executes_commands(tmp_path):
    rc, _input_func, stdout, stderr = run_shell(
        tmp_path,
        ["status", "exit"],
        readline_module=None,
        generated_request_id=lambda: "req-1",
    )

    assert rc == aismixerctl.EXIT_OK
    assert len(FakeClient.calls) == 1
    assert json.loads(stdout)["ok"] is True
    assert stderr == ""


def test_failing_history_and_completion_support_degrades_safely(tmp_path):
    readline_module = FakeReadline(
        fail_read=True,
        fail_write=True,
        fail_setup=True,
    )

    rc, _input_func, stdout, stderr = run_shell(
        tmp_path,
        ["status", "exit"],
        readline_module=readline_module,
        generated_request_id=lambda: "req-1",
    )

    assert rc == aismixerctl.EXIT_OK
    assert len(FakeClient.calls) == 1
    assert json.loads(stdout)["ok"] is True
    assert "Traceback" not in stderr


def test_basic_completion_candidates():
    command_candidates = {
        candidate.rstrip()
        for candidate in aismixerctl.completion_candidates("", "")
    }
    status_candidates = {
        candidate.rstrip()
        for candidate in aismixerctl.completion_candidates("st", "st")
    }
    replace_options = {
        candidate.rstrip()
        for candidate in aismixerctl.completion_candidates("replace --", "--")
    }
    disable_options = {
        candidate.rstrip()
        for candidate in aismixerctl.completion_candidates("disable --e", "--e")
    }

    assert {"status", "replace", "disable", "help", "exit", "quit"} <= (
        command_candidates
    )
    assert status_candidates == {"status"}
    assert {"--file", "--expected-generation"} <= replace_options
    assert "--expected-generation" in disable_options
