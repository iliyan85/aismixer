from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROXY_DIR = ROOT / "nmea_sproxy"


def read_proxy_file(name):
    return (PROXY_DIR / name).read_text(encoding="utf-8")


def shell_commands(text):
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith(("echo ", "#"))
    ]


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

    assert 'sudo install -d -m 0755 "$CONFIG_DIR" "$INSTANCES_DIR"' in install
    assert 'sudo python3 "$KEY_TOOL" station --keys-dir "$KEYS_DIR" --repair-public' in install
    assert 'sudo python3 "$KEY_TOOL" station --keys-dir "$KEYS_DIR"' in install
    assert "python3-setproctitle" in install
    assert "sudo systemctl enable nmea_sproxy.service" in commands
    assert not any(command.startswith("sudo systemctl start ") for command in commands)
    assert not any("enable nmea_sproxy@" in command for command in commands)


def test_install_creates_absolute_system_config_when_missing():
    install = read_proxy_file("install.sh")
    system_config = read_proxy_file("config.system.yaml")

    assert (
        'sudo install -m 0644 "$SCRIPT_DIR/config.system.yaml" "$CONFIG_FILE"'
        in install
    )
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


def test_install_preserves_existing_system_config():
    install = read_proxy_file("install.sh")
    preserve = 'if path_exists "$CONFIG_FILE"; then'
    create = 'sudo install -m 0644 "$SCRIPT_DIR/config.system.yaml" "$CONFIG_FILE"'

    assert preserve in install
    assert "Preserving existing singleton config" in install
    assert install.index(preserve) < install.index(create)


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
