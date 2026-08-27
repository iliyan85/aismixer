import importlib.util
import sys
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
NMEA_SPROXY_DIR = ROOT / "nmea_sproxy"
TEMPLATE_NAMES = ("config.yaml", "config.system.yaml")
EXPECTED_INPUT_NOTICE = (
    "DEPRECATION: legacy nmea_sproxy input configuration (omitted input or "
    "top-level listen_ip/listen_port/allow_from) is deprecated; use explicit "
    "input.type with input.listen_ip/input.listen_port/input.allow_from."
)
EXPECTED_OUTPUT_NOTICE = (
    "DEPRECATION: legacy nmea_sproxy UDPSEC output configuration (omitted output "
    "or top-level remote_host/remote_port/source_ip) is deprecated; use explicit "
    "output.type with output.host/output.port/output.source_ip."
)


def load_proxy_module():
    previous_meta_cleaner = sys.modules.pop("meta_cleaner", None)
    sys.path.insert(0, str(NMEA_SPROXY_DIR))
    try:
        spec = importlib.util.spec_from_file_location(
            "nmea_sproxy_config_deprecation_tests",
            NMEA_SPROXY_DIR / "nmea_sproxy.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(NMEA_SPROXY_DIR))
        sys.modules.pop("meta_cleaner", None)
        if previous_meta_cleaner is not None:
            sys.modules["meta_cleaner"] = previous_meta_cleaner


def write_config(path, config):
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return path


def canonical_input(**overrides):
    config = {
        "type": "udp",
        "listen_ip": "::",
        "listen_port": 50000,
    }
    config.update(overrides)
    return config


def canonical_output(output_type="udpsec", **overrides):
    config = {
        "type": output_type,
        "host": "mixer.example.net",
        "port": 19999 if output_type == "udpsec" else 17777,
    }
    config.update(overrides)
    return config


@pytest.mark.parametrize(
    "keepalive_interval",
    (0, -1, True, float("inf"), float("nan"), "not-a-number"),
)
def test_udpsec_config_rejects_unsafe_keepalive_interval(
    keepalive_interval,
):
    proxy = load_proxy_module()

    with pytest.raises(
        proxy.ProxyConfigError,
        match="keepalive_interval must be a finite number greater than 0",
    ):
        proxy.validate_udpsec_lifecycle_config(
            {"keepalive_interval": keepalive_interval}
        )


@pytest.mark.parametrize("output_type", ["udpsec", "udp"])
def test_canonical_config_emits_no_deprecation_notice(
    tmp_path,
    capsys,
    output_type,
):
    proxy = load_proxy_module()
    config_path = write_config(
        tmp_path / "canonical.yaml",
        {
            "input": canonical_input(),
            "output": canonical_output(output_type),
            "station_id": "boat_001",
            "station_private_key": "station.pem",
            "remote_public_key": "server.pem",
            "reconnect_delay": 5,
            "keepalive_interval": 30,
            "peer_timeout": 90,
            "session_refresh_interval": 0,
            "log_level": "INFO",
        },
    )

    config = proxy.load_config(config_path)

    assert config["input"]["type"] == "udp"
    assert config["output"]["type"] == output_type
    assert "DEPRECATION:" not in capsys.readouterr().err


def test_empty_operator_config_warns_for_both_omitted_mappings(tmp_path, capsys):
    proxy = load_proxy_module()
    config_path = tmp_path / "empty.yaml"
    config_path.write_text("", encoding="utf-8")

    config = proxy.load_config(config_path)

    assert proxy.validate_local_input_config(config)["type"] == "udp"
    assert config["output"]["type"] == "udpsec"
    assert capsys.readouterr().err.splitlines() == [
        EXPECTED_INPUT_NOTICE,
        EXPECTED_OUTPUT_NOTICE,
    ]


def test_legacy_input_keeps_behavior_and_emits_one_input_notice(tmp_path, capsys):
    proxy = load_proxy_module()
    allow_from = ["2001:db8:42::15", "2001:db8:42::/64"]
    config_path = write_config(
        tmp_path / "legacy-input.yaml",
        {
            "listen_ip": "2001:db8::10",
            "listen_port": 50123,
            "allow_from": allow_from,
            "output": canonical_output("udp"),
        },
    )

    config = proxy.load_config(config_path)
    input_config = proxy.validate_local_input_config(config)

    assert input_config == {
        "type": "udp",
        "listen_ip": "2001:db8::10",
        "listen_port": 50123,
        "allow_from": allow_from,
    }
    assert capsys.readouterr().err.splitlines() == [
        EXPECTED_INPUT_NOTICE
    ]


def test_legacy_output_stays_udpsec_and_emits_one_output_notice(tmp_path, capsys):
    proxy = load_proxy_module()
    config_path = write_config(
        tmp_path / "legacy-output.yaml",
        {
            "input": canonical_input(),
            "remote_host": "legacy.example.net",
            "remote_port": 20000,
            "source_ip": "192.0.2.20",
        },
    )

    config = proxy.load_config(config_path)

    assert config["output"] == {
        "type": "udpsec",
        "host": "legacy.example.net",
        "port": 20000,
        "source_ip": "192.0.2.20",
        "legacy": True,
    }
    assert capsys.readouterr().err.splitlines() == [
        EXPECTED_OUTPUT_NOTICE
    ]


def test_fully_legacy_config_emits_one_notice_per_side_without_value_changes(
    tmp_path,
    capsys,
):
    proxy = load_proxy_module()
    config_path = write_config(
        tmp_path / "legacy.yaml",
        {
            "listen_ip": "127.0.0.1",
            "listen_port": 50123,
            "allow_from": ["127.0.0.1"],
            "remote_host": "legacy.example.net",
            "remote_port": 20000,
            "source_ip": "192.0.2.20",
        },
    )

    config = proxy.load_config(config_path)

    assert proxy.validate_local_input_config(config) == {
        "type": "udp",
        "listen_ip": "127.0.0.1",
        "listen_port": 50123,
        "allow_from": ["127.0.0.1"],
    }
    assert config["output"] == {
        "type": "udpsec",
        "host": "legacy.example.net",
        "port": 20000,
        "source_ip": "192.0.2.20",
        "legacy": True,
    }
    assert capsys.readouterr().err.splitlines() == [
        EXPECTED_INPUT_NOTICE,
        EXPECTED_OUTPUT_NOTICE,
    ]


def test_explicit_config_keeps_precedence_and_warns_about_obsolete_top_level_fields(
    tmp_path,
    capsys,
):
    proxy = load_proxy_module()
    config_path = write_config(
        tmp_path / "mixed.yaml",
        {
            "input": canonical_input(
                listen_ip="2001:db8::10",
                listen_port=50123,
            ),
            "listen_ip": "127.0.0.1",
            "listen_port": 50000,
            "output": canonical_output(
                "udp",
                host="192.0.2.30",
                port=17777,
                source_ip="192.0.2.31",
            ),
            "remote_host": "legacy.example.net",
            "remote_port": 19999,
            "source_ip": "192.0.2.20",
        },
    )

    config = proxy.load_config(config_path)

    assert config["input"]["listen_ip"] == "2001:db8::10"
    assert config["input"]["listen_port"] == 50123
    assert config["output"]["host"] == "192.0.2.30"
    assert config["output"]["port"] == 17777
    assert config["output"]["source_ip"] == "192.0.2.31"
    assert capsys.readouterr().err.splitlines() == [
        EXPECTED_INPUT_NOTICE,
        EXPECTED_OUTPUT_NOTICE,
    ]


def test_obsolete_top_level_allow_from_keeps_existing_explicit_input_error(
    tmp_path,
    capsys,
):
    proxy = load_proxy_module()
    config_path = write_config(
        tmp_path / "invalid-mixed.yaml",
        {
            "input": canonical_input(),
            "allow_from": ["192.0.2.0/24"],
            "output": canonical_output("udp"),
        },
    )

    with pytest.raises(proxy.ProxyConfigError, match="use input.allow_from"):
        proxy.load_config(config_path)

    assert capsys.readouterr().err.splitlines() == [
        EXPECTED_INPUT_NOTICE
    ]


@pytest.mark.parametrize("template_name", TEMPLATE_NAMES)
def test_shipped_template_uses_active_explicit_input_and_output(template_name):
    template_path = NMEA_SPROXY_DIR / template_name
    config = yaml.safe_load(template_path.read_text(encoding="utf-8"))

    assert config["input"] == {
        "type": "udp",
        "listen_ip": "::",
        "listen_port": 50000,
    }
    assert config["output"] == {
        "type": "udpsec",
        "host": "192.0.2.10",
        "port": 17777,
    }
    assert not set(config).intersection(
        {
            "listen_ip",
            "listen_port",
            "allow_from",
            "remote_host",
            "remote_port",
            "source_ip",
        }
    )


def test_fresh_system_template_emits_no_deprecation_notice(capsys):
    proxy = load_proxy_module()

    config = proxy.load_config(NMEA_SPROXY_DIR / "config.system.yaml")

    assert config["input"]["type"] == "udp"
    assert config["output"]["type"] == "udpsec"
    assert "DEPRECATION:" not in capsys.readouterr().err
