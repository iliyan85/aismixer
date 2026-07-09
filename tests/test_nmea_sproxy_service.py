from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PROXY_DIR = ROOT / "nmea_sproxy"
PROXY_LIFECYCLE_SCRIPTS = ("install.sh", "update.sh", "uninstall.sh")
PRIVILEGE_ERROR = (
    '[!] This script must be run as root or by a user with sudo installed.'
)
RUN_AS_ROOT_FUNCTION = """run_as_root() {
\tif ((${#AS_ROOT[@]})); then
\t\t"${AS_ROOT[@]}" "$@"
\telse
\t\t"$@"
\tfi
}"""


def read_proxy_file(name):
    return (PROXY_DIR / name).read_text(encoding="utf-8")


def shell_commands(text):
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith(("echo ", "#"))
    ]


def assert_privilege_helper(script):
    assert "if (( EUID == 0 )); then\n\tAS_ROOT=()" in script
    assert "elif command -v sudo >/dev/null 2>&1; then\n\tAS_ROOT=(sudo)" in script
    assert f'else\n\techo "{PRIVILEGE_ERROR}" >&2\n\texit 1\nfi' in script
    assert RUN_AS_ROOT_FUNCTION in script


def assert_no_direct_privileged_commands(script):
    for raw_line in script.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("echo ", "#")):
            continue

        assert not line.startswith("sudo ")
        assert not line.startswith("if sudo ")
        assert not line.startswith(
            ("install ", "systemctl ", "rm ", "tee ", "chmod ", "python3 ", "test ")
        )
        assert not line.startswith(("if systemctl ", "if test "))
        assert " | sudo " not in line


def test_systemd_units_select_singleton_and_instance_configs():
    singleton = read_proxy_file("nmea_sproxy.service")
    template = read_proxy_file("nmea_sproxy@.service")

    assert "--config /etc/nmea_sproxy/config.yaml" in singleton
    assert "--config /etc/nmea_sproxy/instances/%i.yaml" in template
    assert "/etc/nmea_sproxy/instances/" not in singleton


def test_systemd_units_align_process_titles_and_syslog_identifiers():
    singleton = read_proxy_file("nmea_sproxy.service")
    template = read_proxy_file("nmea_sproxy@.service")

    assert "--process-title nmea_sproxy" in singleton
    assert "SyslogIdentifier=nmea_sproxy" in singleton
    assert "--process-title nmea_sproxy@%i" in template
    assert "SyslogIdentifier=nmea_sproxy@%i" in template


def test_install_creates_layout_repairs_keys_and_only_enables_singleton():
    install = read_proxy_file("install.sh")
    commands = shell_commands(install)

    assert 'CORE_DIR="$INSTALL_DIR/core"' in install
    assert (
        'run_as_root install -d -m 0755 "$INSTALL_DIR" "$TOOLS_DIR" '
        '"$CORE_DIR"'
    ) in install
    assert (
        'run_as_root install -m 0644 "$REPO_ROOT/core/network_policy.py" '
        '"$CORE_DIR/network_policy.py"'
    ) in install
    assert (
        'run_as_root install -m 0644 "$SCRIPT_DIR/input_adapters.py" '
        '"$INSTALL_DIR/input_adapters.py"'
    ) in install
    assert (
        'run_as_root install -m 0644 "$SCRIPT_DIR/output_adapters.py" '
        '"$INSTALL_DIR/output_adapters.py"'
    ) in install
    assert 'run_as_root install -d -m 0755 "$CONFIG_DIR" "$INSTANCES_DIR"' in install
    assert 'run_as_root install -d -m 0700 "$KEYS_DIR"' in install
    assert (
        'run_as_root python3 "$KEY_TOOL" station --keys-dir "$KEYS_DIR" '
        "--repair-public"
    ) in commands
    assert 'run_as_root python3 "$KEY_TOOL" station --keys-dir "$KEYS_DIR"' in commands
    assert "python3-setproctitle" in install
    assert "python3-serial" in install
    assert "run_as_root systemctl enable nmea_sproxy.service" in commands
    assert not any(
        command.startswith("run_as_root systemctl start ") for command in commands
    )
    assert not any("enable nmea_sproxy@" in command for command in commands)


def test_install_creates_absolute_system_config_when_missing():
    install = read_proxy_file("install.sh")
    system_config = read_proxy_file("config.system.yaml")

    assert 'run_as_root install -m 0644 "$SCRIPT_DIR/config.system.yaml" "$CONFIG_FILE"' in install
    assert (
        "station_private_key: /etc/nmea_sproxy/keys/station_private.pem"
        in system_config
    )
    assert (
        "remote_public_key: /etc/nmea_sproxy/keys/aismixer_public.pem"
        in system_config
    )


def test_manual_config_uses_local_relative_key_paths():
    manual_config = read_proxy_file("config.yaml")

    assert "station_private_key: station_private.pem" in manual_config
    assert "remote_public_key: aismixer_public.pem" in manual_config
    assert "/etc/nmea_sproxy/keys/" not in manual_config


def test_configs_include_inactive_serial_examples_with_placeholder_device_id():
    combined = "\n".join(
        read_proxy_file(name)
        for name in ("config.yaml", "config.system.yaml")
    )

    assert "# input:" in combined
    assert "#   type: serial" in combined
    assert "#   port: COM4" in combined
    assert "# output:" in combined
    assert "#   type: udpsec" in combined
    assert "#   type: udp" in combined
    assert "Virtual_COM_Port_<device-id>-if00" in combined


def test_install_preserves_existing_system_config():
    install = read_proxy_file("install.sh")
    preserve = 'if path_exists "$CONFIG_FILE"; then'
    create = 'run_as_root install -m 0644 "$SCRIPT_DIR/config.system.yaml" "$CONFIG_FILE"'

    assert preserve in install
    assert "Preserving existing singleton config" in install
    assert install.index(preserve) < install.index(create)


@pytest.mark.parametrize("name", PROXY_LIFECYCLE_SCRIPTS)
def test_lifecycle_scripts_use_privilege_helper(name):
    assert_privilege_helper(read_proxy_file(name))


@pytest.mark.parametrize("name", PROXY_LIFECYCLE_SCRIPTS)
def test_lifecycle_scripts_route_protected_commands_through_helper(name):
    script = read_proxy_file(name)

    assert_no_direct_privileged_commands(script)


def test_install_user_facing_examples_use_root_cmd_prefix():
    install = read_proxy_file("install.sh")

    assert 'ROOT_CMD_PREFIX=""' in install
    assert 'ROOT_CMD_PREFIX="sudo "' in install
    assert 'echo "    Run: ${ROOT_CMD_PREFIX}apt install $pkg" >&2' in install
    assert (
        'echo "    Singleton: ${ROOT_CMD_PREFIX}systemctl start '
        'nmea_sproxy.service"'
    ) in install
    assert (
        'echo "    Template example: ${ROOT_CMD_PREFIX}systemctl enable --now '
        'nmea_sproxy@boat.service"'
    ) in install


def test_update_routes_runtime_and_unit_updates_through_helper():
    update = read_proxy_file("update.sh")

    assert 'CORE_DIR="$INSTALL_DIR/core"' in update
    assert (
        'run_as_root install -d -m 0755 "$INSTALL_DIR" "$TOOLS_DIR" '
        '"$CORE_DIR"'
    ) in update
    assert (
        'run_as_root install -m 0755 "$SCRIPT_DIR/nmea_sproxy.py" '
        '"$INSTALL_DIR/nmea_sproxy.py"'
    ) in update
    assert (
        'run_as_root install -m 0644 "$SCRIPT_DIR/input_adapters.py" '
        '"$INSTALL_DIR/input_adapters.py"'
    ) in update
    assert (
        'run_as_root install -m 0644 "$SCRIPT_DIR/output_adapters.py" '
        '"$INSTALL_DIR/output_adapters.py"'
    ) in update
    assert (
        'run_as_root install -m 0644 "$REPO_ROOT/core/network_policy.py" '
        '"$CORE_DIR/network_policy.py"'
    ) in update
    assert (
        'run_as_root install -m 0644 "$SCRIPT_DIR/nmea_sproxy.service" '
        '"$SYSTEMD_DIR/nmea_sproxy.service"'
    ) in update
    assert 'run_as_root systemctl daemon-reload' in update


def test_uninstall_routes_systemd_and_filesystem_operations_through_helper():
    uninstall = read_proxy_file("uninstall.sh")

    assert 'run_as_root systemctl stop nmea_sproxy.service' in uninstall
    assert "run_as_root systemctl stop 'nmea_sproxy@*.service'" in uninstall
    assert "run_as_root systemctl list-unit-files 'nmea_sproxy@*.service'" in uninstall
    assert 'run_as_root rm -rf -- "$INSTALL_DIR"' in uninstall
    assert (
        'run_as_root rm -f -- "$SYSTEMD_DIR/nmea_sproxy.service" '
        '"$SYSTEMD_DIR/nmea_sproxy@.service"'
    ) in uninstall
    assert 'run_as_root rm -rf -- "$CONFIG_DIR"' in uninstall
    assert 'run_as_root systemctl daemon-reload' in uninstall


def test_update_does_not_write_proxy_configs_or_keys():
    commands = shell_commands(read_proxy_file("update.sh"))

    assert not any("/etc/nmea_sproxy" in command for command in commands)


def test_uninstall_removes_both_units_and_preserves_config_by_default():
    uninstall = read_proxy_file("uninstall.sh")

    assert '"$SYSTEMD_DIR/nmea_sproxy.service"' in uninstall
    assert '"$SYSTEMD_DIR/nmea_sproxy@.service"' in uninstall
    assert "--purge-config" in uninstall
    assert "Preserving operator configs and keys" in uninstall


def test_operator_chosen_instance_examples_do_not_use_numbered_placeholders():
    names = (
        "README.md",
        "config.yaml",
        "config.system.yaml",
        "install.sh",
        "update.sh",
        "uninstall.sh",
        "nmea_sproxy.service",
        "nmea_sproxy@.service",
    )
    combined = "\n".join(read_proxy_file(name) for name in names)

    assert "nmea_sproxy@boat" in combined
    assert "nmea_sproxy@yacht" in combined
    assert "nmea_sproxy@balchik_roof" in combined
    assert "station1" not in combined
    assert "station2" not in combined
