import os
import re
import subprocess
import sys
from pathlib import Path

import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = ROOT / "packaging" / "openwrt" / "aismixer"
PACKAGE_FILES = PACKAGE_DIR / "files"
CANONICAL_PROXY_UDP_INPUT = (
    "input:\n"
    "  type: udp\n"
    "  listen_ip: '::'\n"
    "  listen_port: 50000\n"
)
CANONICAL_PROXY_UDPSEC_OUTPUT = (
    "output:\n"
    "  type: udpsec\n"
    "  host: 192.168.190.53\n"
    "  port: 19999\n"
)


def read_text(path):
    return path.read_text(encoding="utf-8")


def makefile_block(recipe, name):
    match = re.search(
        rf"^define {re.escape(name)}\n(.*?)^endef$",
        recipe,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert match is not None, f"missing Makefile block: {name}"
    return match.group(1)


def nmea_sproxy_preflight_source():
    init = read_text(PACKAGE_FILES / "nmea_sproxy.init")
    match = re.search(
        r'^\s*/usr/bin/python3 - "\$CONFIG" "\$RUNTIME_DIR" 2>&1 <<\'PY\'\n'
        r"(.*?)^PY$",
        init,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert match is not None, "missing nmea_sproxy runtime preflight"
    return match.group(1)


def run_nmea_sproxy_preflight(config_path):
    return subprocess.run(
        [
            sys.executable,
            "-c",
            nmea_sproxy_preflight_source(),
            str(config_path),
            str(ROOT / "nmea_sproxy"),
        ],
        check=False,
        capture_output=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        stdin=subprocess.DEVNULL,
        text=True,
    )


def write_public_key(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    public_key = ec.generate_private_key(ec.SECP256R1()).public_key()
    path.write_bytes(
        public_key.public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )


def test_openwrt_recipe_uses_canonical_three_package_split():
    recipe = read_text(PACKAGE_DIR / "Makefile")

    package_names = re.findall(r"^define Package/([^/\s]+)$", recipe, re.MULTILINE)
    built_packages = re.findall(
        r"^\$\(eval \$\(call BuildPackage,([^)]+)\)\)$",
        recipe,
        re.MULTILINE,
    )

    assert package_names == ["aismixer-common", "aismixer", "nmea_sproxy"]
    assert built_packages == ["aismixer-common", "aismixer", "nmea_sproxy"]
    for block in (
        "Package/nmea_sproxy",
        "Package/nmea_sproxy/description",
        "Package/nmea_sproxy/conffiles",
        "Package/nmea_sproxy/install",
    ):
        assert f"define {block}\n" in recipe
    assert "-".join(("nmea", "sproxy")) not in recipe


def test_openwrt_packages_pin_stable_source_metadata():
    recipe = read_text(PACKAGE_DIR / "Makefile")

    for package_name in ("aismixer-common", "aismixer", "nmea_sproxy"):
        package = makefile_block(recipe, f"Package/{package_name}")
        sources = re.findall(r"^\s*SOURCE:=(\S+)\s*$", package, re.MULTILINE)

        assert sources == ["github.com/iliyan85/aismixer"]


def test_openwrt_package_revision_only_advances_local_release():
    recipe = read_text(PACKAGE_DIR / "Makefile")

    assert re.findall(r"^PKG_VERSION:=(\S+)$", recipe, re.MULTILINE) == [
        "0.2.0"
    ]
    assert re.findall(r"^PKG_RELEASE:=(\S+)$", recipe, re.MULTILINE) == ["2"]
    assert re.findall(
        r"^PKG_SOURCE_VERSION:=(\S+)$", recipe, re.MULTILINE
    ) == ["10df1265b4226debe06815202ed88a4010a567fc"]
    assert re.findall(r"^PKG_MIRROR_HASH:=(\S+)$", recipe, re.MULTILINE) == [
        "f4cf3d8fa68b338b58db38d4919d6f01c4027bef2b7db3108d48ad9c30db10c1"
    ]


def test_openwrt_common_package_copies_complete_core_tree():
    recipe = read_text(PACKAGE_DIR / "Makefile")
    install = makefile_block(recipe, "Package/aismixer-common/install")

    assert "$(PKG_BUILD_DIR)/core/." in install
    assert "$(1)/usr/lib/aismixer/core/" in install
    assert "$(PKG_BUILD_DIR)/core/*.py" not in install


def test_openwrt_aismixer_config_only_enables_packaged_unix_control():
    recipe = read_text(PACKAGE_DIR / "Makefile")
    install = makefile_block(recipe, "Package/aismixer/install")
    conffiles = makefile_block(recipe, "Package/aismixer/conffiles")
    generic = yaml.safe_load(read_text(ROOT / "config.yaml"))
    packaged = yaml.safe_load(read_text(PACKAGE_FILES / "config.yaml"))
    control = {
        "unix": {
            "enabled": True,
            "socket_path": "/run/aismixer/control.sock",
            "socket_mode": "0660",
        }
    }

    assert packaged == {**generic, "control": control}
    assert "$(INSTALL_CONF) ./files/config.yaml" in install
    assert "$(PKG_BUILD_DIR)/config.yaml" not in install
    assert "/etc/aismixer/config.yaml" in conffiles.splitlines()


def test_openwrt_package_has_safe_authorization_default_and_no_identity_payload():
    recipe = read_text(PACKAGE_DIR / "Makefile")
    authorization = yaml.safe_load(
        read_text(PACKAGE_FILES / "authorized_keys.yaml")
    )
    payload_files = [path for path in PACKAGE_FILES.rglob("*") if path.is_file()]

    assert authorization == {"authorized_clients": []}
    assert "$(INSTALL_CONF) ./files/authorized_keys.yaml" in recipe
    assert not [
        path
        for path in payload_files
        if path.suffix.lower() in {".key", ".pem"}
    ]
    for private_name in (
        "aismixer_private.key",
        "aismixer_private.pem",
        "station_private.key",
        "station_private.pem",
    ):
        assert private_name not in recipe


def test_openwrt_nmea_sproxy_init_remains_singleton_without_process_title():
    init = read_text(PACKAGE_FILES / "nmea_sproxy.init")
    open_instance_lines = [
        line.strip()
        for line in init.splitlines()
        if line.strip().startswith("procd_open_instance")
    ]

    assert open_instance_lines == ["procd_open_instance"]
    assert init.count("procd_close_instance") == 1
    assert '"$PROG" \\\n\t\t--config "$CONFIG"' in init
    assert "--process-title" not in init
    assert "nmea_sproxy@" not in init
    assert "config_load" not in init
    assert "config_foreach" not in init
    assert "/sbin/uci" not in init


def test_openwrt_nmea_sproxy_preflight_precedes_procd_instance():
    init = read_text(PACKAGE_FILES / "nmea_sproxy.init")
    start_service = re.search(
        r"^start_service\(\) \{\n(.*?)^\}$",
        init,
        flags=re.MULTILINE | re.DOTALL,
    )

    assert start_service is not None
    body = start_service.group(1)
    assert body.index('if [ ! -f "$CONFIG" ]') < body.index(
        "prepare_station_identity || return 1"
    )
    assert body.index("prepare_station_identity || return 1") < body.index(
        "check_runtime_readiness || return 1"
    )
    assert body.index("check_runtime_readiness || return 1") < body.index(
        "procd_open_instance"
    )
    assert 'procd_set_param respawn 3600 5 5' in body
    assert 'logger -t nmea_sproxy "$readiness_error"' in init


def test_openwrt_nmea_sproxy_preflight_uses_installed_runtime_semantics():
    init = read_text(PACKAGE_FILES / "nmea_sproxy.init")
    preflight = nmea_sproxy_preflight_source()

    assert "RUNTIME_DIR=/usr/lib/aismixer/nmea_sproxy" in init
    assert "runtime.load_config(config_path)" in preflight
    assert 'config["output"]["type"]' in preflight
    assert "runtime.load_public_key(peer_key_path)" in preflight
    assert "os.access(peer_key_path, os.R_OK)" in preflight
    assert "/etc/nmea_sproxy/keys/aismixer_public.pem" not in init


def test_openwrt_nmea_sproxy_preflight_rejects_missing_udpsec_peer_key(
    tmp_path,
):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        CANONICAL_PROXY_UDP_INPUT
        + CANONICAL_PROXY_UDPSEC_OUTPUT
        + "remote_public_key: trust/custom-peer.pem\n",
        encoding="utf-8",
    )

    result = run_nmea_sproxy_preflight(config_path)
    resolved_path = tmp_path / "trust" / "custom-peer.pem"

    assert result.returncode == 1
    assert result.stderr == ""
    assert result.stdout.strip() == (
        "UDPSEC peer public key not provisioned; service not started: "
        f"{resolved_path}"
    )


def test_openwrt_nmea_sproxy_preflight_does_not_require_peer_key_for_udp(
    tmp_path,
):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        CANONICAL_PROXY_UDP_INPUT
        + "remote_public_key: trust/missing-peer.pem\n"
        "output:\n"
        "  type: udp\n"
        "  host: 192.0.2.20\n"
        "  port: 17777\n",
        encoding="utf-8",
    )

    result = run_nmea_sproxy_preflight(config_path)

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_openwrt_nmea_sproxy_preflight_accepts_parseable_custom_peer_key(
    tmp_path,
):
    peer_key_path = tmp_path / "trust" / "mixer.pem"
    write_public_key(peer_key_path)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        CANONICAL_PROXY_UDP_INPUT
        + CANONICAL_PROXY_UDPSEC_OUTPUT
        + "remote_public_key: trust/mixer.pem\n",
        encoding="utf-8",
    )

    result = run_nmea_sproxy_preflight(config_path)

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_openwrt_nmea_sproxy_preflight_rejects_invalid_peer_key(tmp_path):
    peer_key_path = tmp_path / "trust" / "mixer.pem"
    peer_key_path.parent.mkdir()
    peer_key_path.write_text("not a PEM public key\n", encoding="utf-8")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        CANONICAL_PROXY_UDP_INPUT
        + CANONICAL_PROXY_UDPSEC_OUTPUT
        + "remote_public_key: trust/mixer.pem\n",
        encoding="utf-8",
    )

    result = run_nmea_sproxy_preflight(config_path)

    assert result.returncode == 1
    assert result.stderr == ""
    assert result.stdout.strip() == (
        "UDPSEC peer public key invalid or unreadable; service not started: "
        f"{peer_key_path}"
    )


def test_openwrt_nmea_sproxy_preflight_rejects_invalid_config(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        CANONICAL_PROXY_UDP_INPUT
        + "output:\n"
        "  type: tcp\n"
        "  host: 192.0.2.20\n"
        "  port: 17777\n",
        encoding="utf-8",
    )

    result = run_nmea_sproxy_preflight(config_path)

    assert result.returncode == 1
    assert result.stderr == ""
    assert result.stdout.strip() == (
        "Configuration preflight failed; service not started: "
        "output.type: supported values are 'udpsec' and 'udp'"
    )


def test_systemd_process_title_and_template_instance_remain_unchanged():
    singleton = read_text(ROOT / "nmea_sproxy" / "nmea_sproxy.service")
    template = read_text(ROOT / "nmea_sproxy" / "nmea_sproxy@.service")

    assert "--process-title nmea_sproxy" in singleton
    assert "--config /etc/nmea_sproxy/config.yaml" in singleton
    assert "--process-title nmea_sproxy@%i" in template
    assert "--config /etc/nmea_sproxy/instances/%i.yaml" in template


def test_openwrt_sources_are_explicitly_forced_to_lf():
    attributes = read_text(ROOT / ".gitattributes").splitlines()

    assert "packaging/openwrt/aismixer/files/* text eol=lf" in attributes
    assert "packaging/openwrt/aismixer/Makefile text eol=lf" in attributes
