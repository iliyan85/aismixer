import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = ROOT / "packaging" / "openwrt" / "aismixer"
PACKAGE_FILES = PACKAGE_DIR / "files"


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
