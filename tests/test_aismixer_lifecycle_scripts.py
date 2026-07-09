from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
AISMIXER_LIFECYCLE_SCRIPTS = ("install.sh", "update.sh", "uninstall.sh")
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


def read_root_file(name):
    return (ROOT / name).read_text(encoding="utf-8")


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


@pytest.mark.parametrize("name", AISMIXER_LIFECYCLE_SCRIPTS)
def test_lifecycle_scripts_use_privilege_helper(name):
    assert_privilege_helper(read_root_file(name))


@pytest.mark.parametrize("name", AISMIXER_LIFECYCLE_SCRIPTS)
def test_lifecycle_scripts_route_protected_commands_through_helper(name):
    assert_no_direct_privileged_commands(read_root_file(name))


def test_install_preflights_required_source_layout_before_privileged_writes():
    install = read_root_file("install.sh")
    aismixerctl_preflight = 'require_source_file "$SCRIPT_DIR/aismixerctl.py"'
    first_privileged_install = 'run_as_root install -d -m 0755 "$INSTALL_DIR"'

    assert 'require_source_file "$SCRIPT_DIR/aismixer.py"' in install
    assert aismixerctl_preflight in install
    assert 'require_source_file "$SCRIPT_DIR/aismixer.service"' in install
    assert 'require_source_dir "$SCRIPT_DIR/bin"' in install
    assert 'require_source_file "$SCRIPT_DIR/bin/aismixerctl"' in install
    assert 'require_source_dir "$SCRIPT_DIR/core"' in install
    assert 'require_source_dir "$SCRIPT_DIR/tools"' in install
    assert 'require_source_file "$SCRIPT_DIR/config.yaml"' in install
    assert 'require_source_file "$SCRIPT_DIR/udp_alias_map.yaml"' in install
    preflight_call = "\npreflight_source_layout\n\n"
    assert preflight_call in install
    assert install.index(aismixerctl_preflight) < install.index(preflight_call)
    assert install.index(preflight_call) < install.index(first_privileged_install)


def test_install_preserves_configs_and_keys_and_only_enables_service():
    install = read_root_file("install.sh")
    commands = shell_commands(install)

    assert 'run_as_root install -d -m 0755 "$INSTALL_DIR"' in install
    assert 'install_tree "$SCRIPT_DIR/core" "$INSTALL_DIR/core" 0644' in commands
    assert 'install_tree "$SCRIPT_DIR/tools" "$INSTALL_DIR/tools" 0755' in commands
    assert "for config_name in config.yaml udp_alias_map.yaml; do" in install
    assert 'if path_exists "$dest_path"; then' in install
    assert 'run_as_root install -m 0644 "$source_path" "$dest_path"' in install
    assert 'run_as_root install -d -m 0700 "$KEYS_DIR"' in install
    assert (
        'run_as_root python3 "$INSTALL_DIR/tools/aismixer_keys.py" server '
        '--keys-dir "$KEYS_DIR"'
    ) in install
    assert 'printf \'%s\\n\' \'authorized_clients: []\' | run_as_root tee "$AUTHORIZED_KEYS_FILE" >/dev/null' in install
    assert 'run_as_root install -m 0644 "$SCRIPT_DIR/aismixer.service" "$SYSTEMD_UNIT"' in install
    assert 'run_as_root install -m 0755 "$SCRIPT_DIR/bin/aismixerctl" "$CLI_WRAPPER"' in install
    assert 'run_as_root tee "$SYSTEMD_UNIT"' not in install
    assert "<<EOF" not in install
    assert "daemon-reexec" not in install
    assert "run_as_root systemctl enable aismixer" in commands
    assert not any(
        command.startswith("run_as_root systemctl start ") for command in commands
    )


def test_install_preserves_config_before_any_create_operation():
    install = read_root_file("install.sh")
    preserve = 'if path_exists "$dest_path"; then'
    create = 'run_as_root install -m 0644 "$source_path" "$dest_path"'

    assert preserve in install
    assert "preserving it" in install
    assert install.index(preserve) < install.index(create)


def test_install_user_facing_examples_use_root_cmd_prefix():
    install = read_root_file("install.sh")

    assert 'ROOT_CMD_PREFIX=""' in install
    assert 'ROOT_CMD_PREFIX="sudo "' in install
    assert 'echo "    Run: ${ROOT_CMD_PREFIX}apt install $pkg"' in install
    assert (
        'echo "    ${ROOT_CMD_PREFIX}python3 '
        '\\"$INSTALL_DIR/tools/aismixer_keys.py\\" server --keys-dir '
        '\\"$KEYS_DIR\\" --force" >&2'
    ) in install
    assert (
        'echo "[+] Done. You can now start the service with: '
        '${ROOT_CMD_PREFIX}systemctl start aismixer"'
    ) in install


def test_update_preflights_sources_updates_runtime_and_restarts_service():
    update = read_root_file("update.sh")
    commands = shell_commands(update)
    aismixerctl_preflight = 'require_source_file "$SCRIPT_DIR/aismixerctl.py"'
    first_privileged_update = 'run_as_root install -d -m 0755 "$INSTALL_DIR"'

    assert 'require_source_file "$SCRIPT_DIR/aismixer.py"' in update
    assert aismixerctl_preflight in update
    assert 'require_source_file "$SCRIPT_DIR/aismixer.service"' in update
    assert 'require_source_dir "$SCRIPT_DIR/bin"' in update
    assert 'require_source_file "$SCRIPT_DIR/bin/aismixerctl"' in update
    assert 'require_source_dir "$SCRIPT_DIR/core"' in update
    assert 'require_source_dir "$SCRIPT_DIR/tools"' in update
    preflight_call = "\npreflight_source_layout\n\n"
    assert preflight_call in update
    assert update.index(aismixerctl_preflight) < update.index(preflight_call)
    assert update.index(preflight_call) < update.index(first_privileged_update)
    assert 'install_tree "$SCRIPT_DIR/core" "$INSTALL_DIR/core" 0644' in commands
    assert 'install_tree "$SCRIPT_DIR/tools" "$INSTALL_DIR/tools" 0755' in commands
    assert 'run_as_root install -m 0644 "$SCRIPT_DIR/aismixer.service" "$SYSTEMD_UNIT"' in update
    assert 'run_as_root install -m 0755 "$SCRIPT_DIR/bin/aismixerctl" "$CLI_WRAPPER"' in update
    assert not any("/etc/aismixer" in command for command in commands)
    assert "run_as_root systemctl daemon-reload" in commands
    assert "run_as_root systemctl restart aismixer" in commands
    assert update.index("run_as_root systemctl daemon-reload") < update.index(
        "run_as_root systemctl restart aismixer"
    )


def test_uninstall_preserves_config_by_default_and_purges_only_when_requested():
    uninstall = read_root_file("uninstall.sh")

    assert "Usage: $SCRIPT_NAME [--purge-config]" in uninstall
    assert "run_as_root systemctl stop aismixer" in uninstall
    assert "run_as_root systemctl disable aismixer" in uninstall
    assert 'run_as_root rm -rf -- "$INSTALL_DIR"' in uninstall
    assert 'run_as_root rm -f -- "$SYSTEMD_UNIT"' in uninstall
    assert 'run_as_root rm -f -- "$CLI_WRAPPER"' in uninstall
    assert 'if [ "$PURGE_CONFIG" = true ]; then' in uninstall
    assert 'run_as_root rm -rf -- "$CONFIG_DIR"' in uninstall
    assert "Preserving operator configs and keys" in uninstall
    assert 'run_as_root systemctl daemon-reload' in uninstall


def test_repository_systemd_unit_defines_runtime_directory_and_service_behavior():
    unit = read_root_file("aismixer.service")

    assert "Description=AISMixer Service" in unit
    assert "Description=AIS Mixer Service" not in unit
    assert "After=network-online.target" in unit
    assert "Wants=network-online.target" in unit
    assert "Type=simple" in unit
    assert "WorkingDirectory=/opt/aismixer" in unit
    assert "ExecStart=/usr/bin/python3 /opt/aismixer/aismixer.py" in unit
    assert "Restart=always" in unit
    assert "SyslogIdentifier=aismixer" in unit
    assert "RuntimeDirectory=aismixer" in unit
    assert "RuntimeDirectoryMode=0755" in unit
    assert "WantedBy=multi-user.target" in unit
    assert "User=" not in unit
    assert "Group=" not in unit
    assert "DynamicUser=" not in unit
    assert "StateDirectory=" not in unit


def test_aismixerctl_wrapper_execs_installed_runtime_without_logic():
    wrapper = read_root_file("bin/aismixerctl")

    assert wrapper.startswith("#!/bin/sh\n")
    assert 'exec /usr/bin/python3 /opt/aismixer/aismixerctl.py "$@"' in wrapper
    assert "routing." not in wrapper
    assert "socket" not in wrapper


def test_control_deployment_docs_have_no_stale_runtime_or_cli_claims():
    docs = "\n".join(
        read_root_file(name)
        for name in (
            "README.md",
            "ROADMAP.md",
            "examples/README.md",
            "examples/config-routing-control.yaml",
        )
    )

    stale_phrases = [
        "AISMixer does not currently create `/run/aismixer` automatically",
        "does not create /run/aismixer automatically",
        "does not yet provision this directory",
        "installer/systemd integration is updated",
        "not assume that\n`aismixerctl` is installed globally",
        "must already exist",
        "Control socket directory provisioning is currently operator-managed",
        "Provisioning-ът на control socket директорията все още е задача",
    ]
    for phrase in stale_phrases:
        assert phrase not in docs

    assert "RuntimeDirectory=aismixer" in docs
    assert "/usr/local/bin/aismixerctl" in docs
    assert "dedicated service account" in docs
