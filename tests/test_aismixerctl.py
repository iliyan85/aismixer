import json

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
