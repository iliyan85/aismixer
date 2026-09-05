import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
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
CANONICAL_PROXY_UDP_OUTPUT = (
    "output:\n"
    "  type: udp\n"
    "  host: 192.0.2.20\n"
    "  port: 17777\n"
)
EXPECTED_OPENWRT_VERSION = "0.2.1"
EXPECTED_OPENWRT_RELEASE = "3"
EXPECTED_OPENWRT_SOURCE_DATE = "2026-09-04"
EXPECTED_OPENWRT_SOURCE_REVISION = "d8d8c500f5bcfcb451db92b899b2eba5a1626e48"
EXPECTED_OPENWRT_MIRROR_HASH = (
    "8f95da56763df989923f7d1ebe283aa2f5ed173a3b37ff4399022ec9bacee9cb"
)


@dataclass(frozen=True)
class InitHarnessResult:
    service_status: int
    fixture_root: str
    events: tuple[tuple[str, ...], ...]
    stdout: str
    stderr: str

    @property
    def singleton_config(self):
        return f"{self.fixture_root}/etc/nmea_sproxy/config.yaml"

    def instance_config(self, filename):
        return f"{self.fixture_root}/etc/nmea_sproxy/instances/{filename}"


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
        r'^\s*/usr/bin/python3 - "\$config" "\$RUNTIME_DIR" 2>&1 <<\'PY\'\n'
        r"(.*?)^PY$",
        init,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert match is not None, "missing nmea_sproxy runtime preflight"
    return match.group(1)


def run_nmea_sproxy_preflight(config_path, *, runtime_dir=None, extra_env=None):
    runtime_dir = ROOT / "nmea_sproxy" if runtime_dir is None else runtime_dir
    return subprocess.run(
        [
            sys.executable,
            "-c",
            nmea_sproxy_preflight_source(),
            str(config_path),
            str(runtime_dir),
        ],
        check=False,
        capture_output=True,
        env={
            **os.environ,
            "PYTHONDONTWRITEBYTECODE": "1",
            **({} if extra_env is None else extra_env),
        },
        stdin=subprocess.DEVNULL,
        text=True,
    )


def write_private_key(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    private_key = ec.generate_private_key(ec.SECP256R1())
    path.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return private_key


def write_public_key(path, private_key=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    if private_key is None:
        private_key = ec.generate_private_key(ec.SECP256R1())
    public_key = private_key.public_key()
    path.write_bytes(
        public_key.public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )


_POSIX_SHELL = None


def posix_shell_command():
    global _POSIX_SHELL
    if _POSIX_SHELL is not None:
        if not _POSIX_SHELL:
            pytest.skip("a POSIX sh implementation is required for init-script tests")
        return _POSIX_SHELL

    candidates = []
    if os.name == "nt":
        wsl = shutil.which("wsl.exe")
        if wsl:
            candidates.append((wsl, "sh"))
    shell = shutil.which("sh")
    if shell:
        candidates.append((shell,))

    for candidate in candidates:
        try:
            probe = subprocess.run(
                [*candidate, "-c", "exit 0"],
                check=False,
                capture_output=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if probe.returncode == 0:
            _POSIX_SHELL = candidate
            return candidate

    _POSIX_SHELL = ()
    pytest.skip("a POSIX sh implementation is required for init-script tests")


def shell_path(path, command):
    path = Path(path).resolve()
    if os.name == "nt" and Path(command[0]).name.lower() == "wsl.exe":
        translated = subprocess.run(
            [command[0], "wslpath", "-a", str(path)],
            check=True,
            capture_output=True,
            text=True,
        )
        return translated.stdout.strip()
    return path.as_posix()


def _fixture_setup_lines(instances, singleton):
    lines = []
    if singleton == "file":
        lines.append(': > "$CONFIG"')
    elif singleton == "directory":
        lines.append('mkdir -p "$CONFIG"')
    elif singleton != "absent":
        raise ValueError(f"unsupported singleton fixture type: {singleton}")

    for specification in instances:
        if isinstance(specification, str):
            filename, file_type = specification, "file"
        else:
            filename, file_type = specification
        target = f'"$INSTANCES_DIR"/{shlex.quote(filename)}'
        if file_type == "file":
            lines.append(f": > {target}")
        elif file_type == "directory":
            lines.append(f"mkdir -p {target}")
        else:
            raise ValueError(f"unsupported instance fixture type: {file_type}")
    return lines


def _preflight_failure_lines(failed_preflights):
    lines = []
    for name in failed_preflights:
        if name == "singleton":
            target = '"$CONFIG"'
        else:
            target = f'"$INSTANCES_DIR"/{shlex.quote(name)}'
        lines.extend(
            (
                f"\tif [ \"$config_arg\" = {target} ]; then",
                "\t\treturn 1",
                "\tfi",
            )
        )
    return lines


def run_init_harness(
    tmp_path,
    *,
    singleton="absent",
    instances=(),
    failed_preflights=(),
):
    command = posix_shell_command()
    fixture = tmp_path / "openwrt-init-harness"
    fixture.mkdir()
    fixture_root = shell_path(fixture, command)
    init_path = shell_path(PACKAGE_FILES / "nmea_sproxy.init", command)
    setup = "\n".join(_fixture_setup_lines(instances, singleton))
    failures = "\n".join(_preflight_failure_lines(failed_preflights))
    script = f"""
set -u
LC_ALL=C
export LC_ALL
. {shlex.quote(init_path)}

fixture_root={shlex.quote(fixture_root)}
CONFIG="$fixture_root/etc/nmea_sproxy/config.yaml"
INSTANCES_DIR="$fixture_root/etc/nmea_sproxy/instances"
PROG=/mock/usr/bin/nmea_sproxy
events_file="$fixture_root/events.tsv"
mkdir -p "${{CONFIG%/*}}" "$INSTANCES_DIR"
: > "$events_file"
{setup}

record() {{
\tevent_name="$1"
\tshift
\t{{
\t\tprintf '%s' "$event_name"
\t\tfor argument in "$@"; do
\t\t\tprintf '\\t%s' "$argument"
\t\tdone
\t\tprintf '\\n'
\t}} >> "$events_file"
}}

check_runtime_readiness() {{
\tconfig_arg="$1"
\trecord preflight "$config_arg"
{failures}
\treturn 0
}}

procd_open_instance() {{ record open "$@"; }}
procd_set_param() {{ record param "$@"; }}
procd_close_instance() {{ record close "$@"; }}
logger() {{ record logger "$@"; }}

start_service
service_status=$?
record status "$service_status"
cat "$events_file"
exit 0
"""
    result = subprocess.run(
        [*command, "-s"],
        input=script,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr

    parsed = tuple(
        tuple(line.split("\t"))
        for line in result.stdout.splitlines()
        if line.strip()
    )
    status_events = [event for event in parsed if event[0] == "status"]
    assert status_events and len(status_events) == 1, result.stdout
    events = tuple(event for event in parsed if event[0] != "status")
    return InitHarnessResult(
        service_status=int(status_events[0][1]),
        fixture_root=fixture_root,
        events=events,
        stdout=result.stdout,
        stderr=result.stderr,
    )


def relation_events(config, *, instance_name=None):
    open_event = (
        ("open",)
        if instance_name is None
        else ("open", instance_name)
    )
    return (
        ("preflight", config),
        open_event,
        (
            "param",
            "command",
            "/mock/usr/bin/nmea_sproxy",
            "--config",
            config,
        ),
        ("param", "stdout", "1"),
        ("param", "stderr", "1"),
        ("param", "respawn", "3600", "5", "5"),
        ("param", "file", config),
        ("close",),
    )


def operational_events(result):
    return tuple(
        event
        for event in result.events
        if event[0] in {"preflight", "open", "param", "close"}
    )


def logger_messages(result):
    messages = []
    for event in result.events:
        if event[0] != "logger":
            continue
        assert event[1:3] == ("-t", "nmea_sproxy")
        messages.append(event[3])
    return messages


def write_preflight_spy_runtime(runtime_dir):
    runtime_dir.mkdir()
    (runtime_dir / "nmea_sproxy.py").write_text(
        """import os

UDPSEC_OUTPUT_TYPE = "udpsec"


def record(*parts):
    with open(os.environ["NMEA_SPROXY_PREFLIGHT_CALLS"], "a", encoding="utf-8") as log:
        log.write("|".join(str(part) for part in parts) + "\\n")


def load_config(path):
    record("load_config", path)
    return {
        "output": {"type": os.environ["NMEA_SPROXY_PREFLIGHT_OUTPUT"]},
        "remote_public_key": os.environ.get(
            "NMEA_SPROXY_PREFLIGHT_PEER", "unused-peer.pem"
        ),
    }


def ensure_station_identity(config):
    record("ensure_station_identity")
    error = os.environ.get("NMEA_SPROXY_PREFLIGHT_IDENTITY_ERROR")
    if error:
        raise RuntimeError(error)


def load_peer_public_key(path):
    record("load_peer_public_key", path)
    error = os.environ.get("NMEA_SPROXY_PREFLIGHT_PEER_ERROR")
    if error:
        raise RuntimeError(error)
""",
        encoding="utf-8",
    )


def preflight_spy_calls(path):
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()


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


def test_openwrt_pkgarch_all_is_declared_only_on_shared_package_default():
    """Regression: a top-level PKGARCH:=all before
    include $(INCLUDE_DIR)/package.mk is ineffective for generated package
    metadata -- real mips_24kc APK inspection showed all three packages
    still reporting arch: mips_24kc despite that top-level line. PKGARCH
    must instead be set inside Package/aismixer/Default, the shared block
    every package definition calls, so it actually reaches the generated
    metadata for all three packages without being duplicated per package."""
    recipe = read_text(PACKAGE_DIR / "Makefile")
    include_index = recipe.index("include $(INCLUDE_DIR)/package.mk")
    preamble = recipe[:include_index]
    default_block = makefile_block(recipe, "Package/aismixer/Default")

    # 1. The shared default carries the architecture declaration.
    assert re.search(r"^\s*PKGARCH:=all\s*$", default_block, re.MULTILINE)

    # 2. No top-level PKGARCH:=all before include $(INCLUDE_DIR)/package.mk,
    #    where it has no effect on generated package metadata.
    assert "PKGARCH" not in preamble

    # 3. All three packages still call the shared default that now carries
    #    PKGARCH, and none of them duplicates the declaration itself.
    for package_name in ("aismixer-common", "aismixer", "nmea_sproxy"):
        package = makefile_block(recipe, f"Package/{package_name}")
        assert "$(call Package/aismixer/Default)" in package
        assert "PKGARCH" not in package


def test_openwrt_packages_pin_stable_source_metadata():
    recipe = read_text(PACKAGE_DIR / "Makefile")

    for package_name in ("aismixer-common", "aismixer", "nmea_sproxy"):
        package = makefile_block(recipe, f"Package/{package_name}")
        sources = re.findall(r"^\s*SOURCE:=(\S+)\s*$", package, re.MULTILINE)

        assert sources == ["github.com/iliyan85/aismixer"]


def test_openwrt_packages_pin_expected_release_source_metadata():
    recipe = read_text(PACKAGE_DIR / "Makefile")

    assert re.findall(r"^PKG_VERSION:=(\S+)$", recipe, re.MULTILINE) == [
        EXPECTED_OPENWRT_VERSION
    ]
    assert re.findall(r"^PKG_RELEASE:=(\S+)$", recipe, re.MULTILINE) == [
        EXPECTED_OPENWRT_RELEASE
    ]
    assert re.findall(r"^PKG_SOURCE_DATE:=(\S+)$", recipe, re.MULTILINE) == [
        EXPECTED_OPENWRT_SOURCE_DATE
    ]
    assert re.findall(
        r"^PKG_SOURCE_VERSION:=(\S+)$", recipe, re.MULTILINE
    ) == [EXPECTED_OPENWRT_SOURCE_REVISION]


def test_openwrt_package_mirror_hash_matches_current_source_pin():
    """PKG_MIRROR_HASH must verify the exact PKG_SOURCE_VERSION pinned above.

    Kept as its own test, separate from
    test_openwrt_packages_pin_expected_release_source_metadata, so a source
    repin that updates PKG_SOURCE_VERSION without regenerating the matching
    SDK-produced PKG_MIRROR_HASH fails here specifically, rather than being
    masked by (or conflated with) the other pin fields. This is the release
    pin's content-integrity check: the archive OpenWrt actually downloads
    and builds from must be the exact byte content of the pinned release
    commit, not merely a commit SHA that resolves at fetch time.
    """
    recipe = read_text(PACKAGE_DIR / "Makefile")
    (mirror_hash,) = re.findall(
        r"^PKG_MIRROR_HASH:=(\S+)$", recipe, re.MULTILINE
    )

    assert mirror_hash == EXPECTED_OPENWRT_MIRROR_HASH, (
        "PKG_MIRROR_HASH does not match the SDK-generated hash for the "
        f"pinned PKG_SOURCE_VERSION; the Makefile holds {mirror_hash!r}. "
        "Regenerate it with the OpenWrt SDK whenever PKG_SOURCE_VERSION "
        "changes, and update EXPECTED_OPENWRT_MIRROR_HASH to match."
    )


def test_openwrt_nmea_sproxy_uses_procd_names_without_unavailable_title_dependency():
    recipe = read_text(PACKAGE_DIR / "Makefile")
    package = makefile_block(recipe, "Package/nmea_sproxy")
    init = read_text(PACKAGE_FILES / "nmea_sproxy.init")

    assert "python3-setproctitle" not in package
    assert "OpenWrt 25.12 does not package Python setproctitle" in recipe
    assert "--process-title" not in init


def test_openwrt_common_package_copies_complete_core_tree():
    recipe = read_text(PACKAGE_DIR / "Makefile")
    install = makefile_block(recipe, "Package/aismixer-common/install")

    assert "$(PKG_BUILD_DIR)/core/." in install
    assert "$(1)/usr/lib/aismixer/core/" in install
    assert "$(PKG_BUILD_DIR)/core/*.py" not in install


def test_openwrt_common_package_declares_cryptography_dependency():
    """Regression for Point 12A.2: aismixer-common ships core/key_material.py,
    core/udpsec_crypto.py, core/udpsec_identity.py, and the canonical
    tools/aismixer_keys.py, all of which import `cryptography` directly.
    That dependency must be declared on aismixer-common itself rather than
    relying on every downstream consumer to bring it in transitively."""
    recipe = read_text(PACKAGE_DIR / "Makefile")
    common = makefile_block(recipe, "Package/aismixer-common")

    assert "DEPENDS:=+python3 +python3-cryptography" in common


def test_openwrt_consumers_pin_exact_matching_aismixer_common_revision():
    """Regression: on a real OpenWrt 25.12.5 x86_64 router, `apk upgrade
    aismixer-common aismixer nmea_sproxy` from 0.2.1-r1 transiently ran the
    new aismixer/nmea_sproxy Python source against the still-r1
    aismixer-common core/ tree mid-transaction, producing
    `ImportError: cannot import name 'SESSION_CLOSE_TYPE' from
    'core.udpsec_protocol'` (aismixer) and `cannot import name
    'build_ping_message' from 'core.udpsec_protocol'` (nmea_sproxy) --
    both symbols genuinely absent from the r1-pinned source. A plain,
    unversioned `+aismixer-common` DEPENDS lets apk consider any installed
    aismixer-common revision satisfying, including a stale one mid-upgrade.

    Fix: keep the ordinary, unversioned `+aismixer-common` in DEPENDS for
    normal build/select package-presence handling, and add a
    version-constrained EXTRA_DEPENDS carrying the exact runtime pin. The
    OpenWrt/APK package version string is $(PKG_VERSION)-r$(PKG_RELEASE)
    with a literal "r" -- matching the published 0.2.1-r1/0.2.1-r2
    artifacts and real router `apk policy` output -- not the bare
    $(PKG_VERSION)-$(PKG_RELEASE) form.
    """
    recipe = read_text(PACKAGE_DIR / "Makefile")
    exact_extra_depends = re.compile(
        r"^\s*EXTRA_DEPENDS:=aismixer-common "
        r"\(=\$\(PKG_VERSION\)-r\$\(PKG_RELEASE\)\)\s*$",
        re.MULTILINE,
    )
    missing_r_form = re.compile(r"\(=\$\(PKG_VERSION\)-\$\(PKG_RELEASE\)\)")

    for package_name in ("aismixer", "nmea_sproxy"):
        package = makefile_block(recipe, f"Package/{package_name}")

        # 1. Ordinary, unversioned +aismixer-common remains in DEPENDS for
        #    normal build/select package-presence handling -- no version
        #    constraint embedded here; that lives in EXTRA_DEPENDS only.
        (depends_line,) = re.findall(
            r"^\s*DEPENDS:=(.*)$", package, re.MULTILINE
        )
        assert "+aismixer-common" in depends_line.split(), (
            f"Package/{package_name} DEPENDS must still list the ordinary "
            f"'+aismixer-common' selection dependency. Got: {depends_line!r}"
        )
        assert "(" not in depends_line, (
            f"Package/{package_name} DEPENDS must stay unversioned; the "
            f"exact pin belongs in EXTRA_DEPENDS. Got: {depends_line!r}"
        )

        # 2 & 3. Exactly one EXTRA_DEPENDS line, carrying the
        #    variable-derived "-r$(PKG_RELEASE)" exact pin (literal "r").
        extra_depends = re.findall(
            r"^\s*EXTRA_DEPENDS:=(.*)$", package, re.MULTILINE
        )
        assert len(extra_depends) == 1, (
            "expected exactly one EXTRA_DEPENDS line in "
            f"Package/{package_name}"
        )
        assert exact_extra_depends.search(package), (
            f"Package/{package_name} must declare exactly "
            "'EXTRA_DEPENDS:=aismixer-common "
            "(=$(PKG_VERSION)-r$(PKG_RELEASE))'. "
            f"Got: {extra_depends[0]!r}"
        )

        # 4. The "r"-less form must not appear anywhere in this package's
        #    block: it does not match the real OpenWrt/APK package version
        #    string and would never resolve against the published 0.2.1-rN
        #    artifacts.
        assert not missing_r_form.search(package), (
            f"Package/{package_name} must not use "
            "$(PKG_VERSION)-$(PKG_RELEASE) (missing the 'r'); the actual "
            "package version string is $(PKG_VERSION)-r$(PKG_RELEASE)."
        )

        # Package-variable-derived, not hard-coded release text: this must
        # keep matching automatically on the next release bump.
        assert "0.2.1-3" not in extra_depends[0]
        assert "0.2.1-r3" not in extra_depends[0]

    # 5. aismixer-common must not gain a circular/self dependency, in
    #    either DEPENDS or EXTRA_DEPENDS.
    common = makefile_block(recipe, "Package/aismixer-common")
    assert "EXTRA_DEPENDS" not in common
    (common_depends,) = re.findall(
        r"^\s*DEPENDS:=(.*)$", common, re.MULTILINE
    )
    assert "aismixer-common" not in common_depends

    # 6. Packaging revision bump.
    assert re.findall(r"^PKG_RELEASE:=(\S+)$", recipe, re.MULTILINE) == [
        EXPECTED_OPENWRT_RELEASE
    ]

    # 7. Upstream source pin/date/hash remain exactly the published 0.2.1
    #    values; only the packaging revision changed for this fix.
    assert re.findall(r"^PKG_SOURCE_DATE:=(\S+)$", recipe, re.MULTILINE) == [
        EXPECTED_OPENWRT_SOURCE_DATE
    ]
    assert re.findall(
        r"^PKG_SOURCE_VERSION:=(\S+)$", recipe, re.MULTILINE
    ) == [EXPECTED_OPENWRT_SOURCE_REVISION]
    assert re.findall(r"^PKG_MIRROR_HASH:=(\S+)$", recipe, re.MULTILINE) == [
        EXPECTED_OPENWRT_MIRROR_HASH
    ]


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
    # Regression for Point 7: shipped deployment seeds must not silently
    # revert to high-frequency, traffic-proportional debug logging.
    assert generic["debug"] is False
    assert "$(INSTALL_CONF) ./files/config.yaml" in install
    assert "$(PKG_BUILD_DIR)/config.yaml" not in install
    assert "/etc/aismixer/config.yaml" in conffiles.splitlines()


def test_openwrt_aismixer_udp_alias_map_uses_sanitized_packaged_default():
    """Regression for Point 12A.2: the packaged alias map must come from the
    sanitized ./files/ template, not the repository-root developer/lab copy,
    and must ship structurally empty."""
    recipe = read_text(PACKAGE_DIR / "Makefile")
    install = makefile_block(recipe, "Package/aismixer/install")
    conffiles = makefile_block(recipe, "Package/aismixer/conffiles")
    alias_map = yaml.safe_load(read_text(PACKAGE_FILES / "udp_alias_map.yaml"))

    assert "$(INSTALL_CONF) ./files/udp_alias_map.yaml" in install
    assert "$(1)/etc/aismixer/udp_alias_map.yaml" in install
    assert "$(PKG_BUILD_DIR)/udp_alias_map.yaml" not in install
    assert "/etc/aismixer/udp_alias_map.yaml" in conffiles.splitlines()
    assert alias_map == {"udp_alias_map": []}


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


def test_openwrt_nmea_sproxy_package_installs_multi_instance_layout_only():
    recipe = read_text(PACKAGE_DIR / "Makefile")
    install = makefile_block(recipe, "Package/nmea_sproxy/install")
    conffiles = makefile_block(recipe, "Package/nmea_sproxy/conffiles")

    assert "$(INSTALL_CONF) ./files/nmea_sproxy.config.yaml" in install
    assert "$(1)/etc/nmea_sproxy/config.yaml" in install
    assert "$(INSTALL_DIR) $(1)/etc/nmea_sproxy/instances" in install
    assert "$(INSTALL_DIR) $(1)/etc/nmea_sproxy/keys" in install
    assert "chmod 0700 $(1)/etc/nmea_sproxy/keys" in install
    assert conffiles.splitlines() == ["/etc/nmea_sproxy/config.yaml"]
    assert "/etc/nmea_sproxy/instances" not in conffiles
    assert "/etc/nmea_sproxy/keys" not in conffiles
    assert not [
        path
        for path in PACKAGE_FILES.rglob("*")
        if path.is_file() and path.suffix.lower() in {".pem", ".key"}
    ]


def test_openwrt_nmea_sproxy_packaged_config_matches_current_canonical_template():
    packaged = PACKAGE_FILES / "nmea_sproxy.config.yaml"
    current = ROOT / "nmea_sproxy" / "config.system.yaml"

    assert read_text(packaged) == read_text(current)
    config = yaml.safe_load(read_text(packaged))
    assert config["input"]["type"] == "udp"
    assert config["output"]["type"] == "udpsec"


def test_openwrt_nmea_sproxy_init_defines_multi_instance_contract():
    init = read_text(PACKAGE_FILES / "nmea_sproxy.init")

    assert "CONFIG=/etc/nmea_sproxy/config.yaml" in init
    assert "INSTANCES_DIR=/etc/nmea_sproxy/instances" in init
    assert "LC_ALL=C\nexport LC_ALL" in init
    assert "start_relation()" in init
    assert "valid_instance_name()" in init
    assert 'for config in "$INSTANCES_DIR"/*.yaml; do' in init
    assert 'instance_name="${filename%.yaml}"' in init
    assert "[!A-Za-z0-9]*|*[!A-Za-z0-9_.-]*" in init
    assert "config_load" not in init
    assert "config_foreach" not in init
    assert "/sbin/uci" not in init


def test_openwrt_nmea_sproxy_relation_preflights_before_opening_procd():
    init = read_text(PACKAGE_FILES / "nmea_sproxy.init")
    match = re.search(
        r"^start_relation\(\) \{\n(.*?)^\}$",
        init,
        flags=re.MULTILINE | re.DOTALL,
    )

    assert match is not None
    body = match.group(1)
    assert body.index('if [ ! -f "$config" ]') < body.index(
        'if ! check_runtime_readiness "$config"'
    )
    assert body.index('if ! check_runtime_readiness "$config"') < body.index(
        'procd_open_instance "$instance_name"'
    )
    assert body.index('procd_open_instance "$instance_name"') < body.index(
        "procd_set_param command"
    )
    assert body.index("procd_set_param command") < body.index(
        "procd_close_instance"
    )
    assert 'procd_set_param stdout 1' in body
    assert 'procd_set_param stderr 1' in body
    assert 'procd_set_param respawn 3600 5 5' in body
    assert 'procd_set_param file "$config"' in body


def test_openwrt_nmea_sproxy_singleton_uses_unnamed_instance_and_exact_params(
    tmp_path,
):
    result = run_init_harness(tmp_path, singleton="file")

    assert result.service_status == 0
    assert operational_events(result) == relation_events(result.singleton_config)
    assert logger_messages(result) == []


def test_openwrt_nmea_sproxy_named_relation_uses_stem_as_procd_name(tmp_path):
    result = run_init_harness(tmp_path, instances=("boat.yaml",))
    config = result.instance_config("boat.yaml")

    assert result.service_status == 0
    assert operational_events(result) == relation_events(
        config,
        instance_name="boat",
    )
    assert logger_messages(result) == []


def test_openwrt_nmea_sproxy_multiple_named_relations_use_glob_order(tmp_path):
    filenames = (
        "zulu.yaml",
        "station_3.yaml",
        "roof.v2.yaml",
        "boat-2.yaml",
        "alpha.yaml",
    )
    result = run_init_harness(tmp_path, instances=filenames)
    expected_names = ("alpha", "boat-2", "roof.v2", "station_3", "zulu")
    expected = tuple(
        event
        for name in expected_names
        for event in relation_events(
            result.instance_config(f"{name}.yaml"),
            instance_name=name,
        )
    )

    assert result.service_status == 0
    assert operational_events(result) == expected
    assert logger_messages(result) == []


def test_openwrt_nmea_sproxy_singleton_precedes_named_relations(tmp_path):
    result = run_init_harness(
        tmp_path,
        singleton="file",
        instances=("yacht.yaml", "balchik_roof.yaml"),
    )
    expected = (
        *relation_events(result.singleton_config),
        *relation_events(
            result.instance_config("balchik_roof.yaml"),
            instance_name="balchik_roof",
        ),
        *relation_events(
            result.instance_config("yacht.yaml"),
            instance_name="yacht",
        ),
    )

    assert result.service_status == 0
    assert operational_events(result) == expected
    assert logger_messages(result) == []


def test_openwrt_nmea_sproxy_rejects_internal_default_name_with_singleton(
    tmp_path,
):
    result = run_init_harness(
        tmp_path,
        singleton="file",
        instances=("instance1.yaml", "boat.yaml"),
    )
    expected = (
        *relation_events(result.singleton_config),
        *relation_events(
            result.instance_config("boat.yaml"),
            instance_name="boat",
        ),
    )

    assert result.service_status == 0
    assert operational_events(result) == expected
    assert logger_messages(result) == [
        "Skipping named relation 'instance1'; its name conflicts with the "
        "unnamed singleton's internal procd instance name: "
        + result.instance_config("instance1.yaml"),
        "Started 2 of 3 configured nmea_sproxy relations; invalid relations "
        "were skipped",
    ]


def test_openwrt_nmea_sproxy_allows_instance1_without_started_singleton(
    tmp_path,
):
    result = run_init_harness(tmp_path, instances=("instance1.yaml",))

    assert result.service_status == 0
    assert operational_events(result) == relation_events(
        result.instance_config("instance1.yaml"),
        instance_name="instance1",
    )
    assert logger_messages(result) == []


def test_openwrt_nmea_sproxy_allows_instance1_after_singleton_preflight_failure(
    tmp_path,
):
    result = run_init_harness(
        tmp_path,
        singleton="file",
        instances=("instance1.yaml",),
        failed_preflights=("singleton",),
    )

    assert result.service_status == 0
    assert operational_events(result) == (
        ("preflight", result.singleton_config),
        *relation_events(
            result.instance_config("instance1.yaml"),
            instance_name="instance1",
        ),
    )
    assert logger_messages(result) == [
        "Skipping singleton relation after failed preflight: "
        + result.singleton_config,
        "Started 1 of 2 configured nmea_sproxy relations; invalid relations "
        "were skipped",
    ]


def test_openwrt_nmea_sproxy_unsafe_names_are_logged_and_skipped(tmp_path):
    unsafe = ("-leading.yaml", "_leading.yaml", "bad name.yaml")
    result = run_init_harness(
        tmp_path,
        instances=(*unsafe, "safe.yaml"),
    )

    assert result.service_status == 0
    assert operational_events(result) == relation_events(
        result.instance_config("safe.yaml"),
        instance_name="safe",
    )
    assert logger_messages(result) == [
        "Skipping named relation with unsafe instance name '-leading': "
        + result.instance_config("-leading.yaml"),
        "Skipping named relation with unsafe instance name '_leading': "
        + result.instance_config("_leading.yaml"),
        "Skipping named relation with unsafe instance name 'bad name': "
        + result.instance_config("bad name.yaml"),
        "Started 1 of 4 configured nmea_sproxy relations; invalid relations "
        "were skipped",
    ]


def test_openwrt_nmea_sproxy_ignores_other_suffixes_hidden_and_nonregular_files(
    tmp_path,
):
    result = run_init_harness(
        tmp_path,
        instances=(
            ".hidden.yaml",
            "relation.yml",
            "relation.yaml.bak",
            ("directory.yaml", "directory"),
        ),
    )

    assert result.service_status == 1
    assert operational_events(result) == ()
    assert logger_messages(result) == [
        "No nmea_sproxy relations configured; expected "
        f"{result.singleton_config} or regular "
        f"{result.fixture_root}/etc/nmea_sproxy/instances/*.yaml files"
    ]


def test_openwrt_nmea_sproxy_empty_layout_fails_clearly(tmp_path):
    result = run_init_harness(tmp_path)

    assert result.service_status == 1
    assert operational_events(result) == ()
    assert logger_messages(result) == [
        "No nmea_sproxy relations configured; expected "
        f"{result.singleton_config} or regular "
        f"{result.fixture_root}/etc/nmea_sproxy/instances/*.yaml files"
    ]


def test_openwrt_nmea_sproxy_peer_failures_are_isolated_per_relation(tmp_path):
    result = run_init_harness(
        tmp_path,
        instances=(
            "plain.yaml",
            "missing-peer.yaml",
            "invalid-peer.yaml",
        ),
        failed_preflights=("missing-peer.yaml", "invalid-peer.yaml"),
    )
    preflights = [event[1] for event in result.events if event[0] == "preflight"]

    assert result.service_status == 0
    assert preflights == [
        result.instance_config("invalid-peer.yaml"),
        result.instance_config("missing-peer.yaml"),
        result.instance_config("plain.yaml"),
    ]
    assert [event for event in result.events if event[0] == "open"] == [
        ("open", "plain")
    ]
    assert logger_messages(result) == [
        "Skipping named 'invalid-peer' relation after failed preflight: "
        + result.instance_config("invalid-peer.yaml"),
        "Skipping named 'missing-peer' relation after failed preflight: "
        + result.instance_config("missing-peer.yaml"),
        "Started 1 of 3 configured nmea_sproxy relations; invalid relations "
        "were skipped",
    ]


def test_openwrt_nmea_sproxy_all_invalid_relations_fail_after_all_preflights(
    tmp_path,
):
    result = run_init_harness(
        tmp_path,
        singleton="file",
        instances=("broken.yaml",),
        failed_preflights=("singleton", "broken.yaml"),
    )

    assert result.service_status == 1
    assert operational_events(result) == (
        ("preflight", result.singleton_config),
        ("preflight", result.instance_config("broken.yaml")),
    )
    assert logger_messages(result) == [
        "Skipping singleton relation after failed preflight: "
        + result.singleton_config,
        "Skipping named 'broken' relation after failed preflight: "
        + result.instance_config("broken.yaml"),
        "No nmea_sproxy relations started; all 2 configured relations were "
        "invalid",
    ]


def test_openwrt_nmea_sproxy_preflight_uses_demand_driven_runtime_api():
    init = read_text(PACKAGE_FILES / "nmea_sproxy.init")
    preflight = nmea_sproxy_preflight_source()

    assert "RUNTIME_DIR=/usr/lib/aismixer/nmea_sproxy" in init
    assert "runtime.load_config(config_path)" in preflight
    assert 'config["output"]["type"]' in preflight
    assert "runtime.ensure_station_identity(config)" in preflight
    assert (
        'runtime.load_peer_public_key(config["remote_public_key"])'
        in preflight
    )
    assert preflight.index("runtime.ensure_station_identity(config)") < (
        preflight.index("runtime.load_peer_public_key")
    )
    assert "runtime.load_public_key" not in preflight
    assert "os.access" not in preflight
    assert "prepare_station_identity" not in init
    assert "aismixer_keys.py" not in init
    assert "--repair-public" not in init


@pytest.mark.parametrize(
    ("output_type", "expected_calls"),
    (
        (
            "udp",
            ("load_config",),
        ),
        (
            "udpsec",
            (
                "load_config",
                "ensure_station_identity",
                "load_peer_public_key",
            ),
        ),
    ),
)
def test_openwrt_nmea_sproxy_preflight_runtime_call_order(
    tmp_path,
    output_type,
    expected_calls,
):
    runtime_dir = tmp_path / "runtime"
    write_preflight_spy_runtime(runtime_dir)
    config_path = tmp_path / "config.yaml"
    config_path.write_text("fixture: true\n", encoding="utf-8")
    call_log = tmp_path / "calls.log"
    peer_path = tmp_path / "trust" / "peer.pem"

    result = run_nmea_sproxy_preflight(
        config_path,
        runtime_dir=runtime_dir,
        extra_env={
            "NMEA_SPROXY_PREFLIGHT_CALLS": str(call_log),
            "NMEA_SPROXY_PREFLIGHT_OUTPUT": output_type,
            "NMEA_SPROXY_PREFLIGHT_PEER": str(peer_path),
        },
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""
    calls = preflight_spy_calls(call_log)
    assert tuple(call.split("|", 1)[0] for call in calls) == expected_calls
    assert calls[0] == f"load_config|{config_path}"
    if output_type == "udpsec":
        assert calls[-1] == f"load_peer_public_key|{peer_path}"


def test_openwrt_nmea_sproxy_preflight_plain_udp_needs_no_crypto_material(
    tmp_path,
):
    missing_private = tmp_path / "keys" / "missing-station.pem"
    missing_peer = tmp_path / "trust" / "missing-peer.pem"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        CANONICAL_PROXY_UDP_INPUT
        + CANONICAL_PROXY_UDP_OUTPUT
        + f"station_private_key: {missing_private}\n"
        + f"remote_public_key: {missing_peer}\n",
        encoding="utf-8",
    )

    result = run_nmea_sproxy_preflight(config_path)

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""
    assert not missing_private.exists()
    assert not missing_peer.exists()


def test_openwrt_nmea_sproxy_preflight_accepts_valid_udpsec_relation(tmp_path):
    station_path = tmp_path / "keys" / "station.pem"
    peer_path = tmp_path / "trust" / "mixer.pem"
    write_private_key(station_path)
    write_public_key(peer_path)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        CANONICAL_PROXY_UDP_INPUT
        + CANONICAL_PROXY_UDPSEC_OUTPUT
        + "station_private_key: keys/station.pem\n"
        + "remote_public_key: trust/mixer.pem\n",
        encoding="utf-8",
    )

    result = run_nmea_sproxy_preflight(config_path)

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_openwrt_nmea_sproxy_preflight_rejects_missing_udpsec_peer_key(
    tmp_path,
):
    station_path = tmp_path / "keys" / "station.pem"
    write_private_key(station_path)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        CANONICAL_PROXY_UDP_INPUT
        + CANONICAL_PROXY_UDPSEC_OUTPUT
        + "station_private_key: keys/station.pem\n"
        + "remote_public_key: trust/custom-peer.pem\n",
        encoding="utf-8",
    )
    peer_path = tmp_path / "trust" / "custom-peer.pem"

    result = run_nmea_sproxy_preflight(config_path)

    assert result.returncode == 1
    assert result.stderr == ""
    assert result.stdout.startswith(
        "Configuration preflight failed; service not started: "
    )
    assert "trusted aismixer public key is missing" in result.stdout
    assert str(peer_path) in result.stdout
    assert not peer_path.exists()


def test_openwrt_nmea_sproxy_preflight_rejects_invalid_peer_without_mutation(
    tmp_path,
):
    station_path = tmp_path / "keys" / "station.pem"
    peer_path = tmp_path / "trust" / "mixer.pem"
    write_private_key(station_path)
    peer_path.parent.mkdir()
    peer_path.write_text("not a PEM public key\n", encoding="utf-8")
    original_peer = peer_path.read_bytes()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        CANONICAL_PROXY_UDP_INPUT
        + CANONICAL_PROXY_UDPSEC_OUTPUT
        + "station_private_key: keys/station.pem\n"
        + "remote_public_key: trust/mixer.pem\n",
        encoding="utf-8",
    )

    result = run_nmea_sproxy_preflight(config_path)

    assert result.returncode == 1
    assert result.stderr == ""
    assert result.stdout.startswith(
        "Configuration preflight failed; service not started: "
    )
    assert "Unable to load trusted aismixer public key" in result.stdout
    assert str(peer_path) in result.stdout
    assert peer_path.read_bytes() == original_peer


def test_openwrt_nmea_sproxy_preflight_rejects_invalid_config_before_crypto(
    tmp_path,
):
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


def test_openwrt_nmea_sproxy_legacy_config_is_accepted_with_deprecation(
    tmp_path,
):
    station_path = tmp_path / "station.pem"
    peer_path = tmp_path / "peer.pem"
    write_private_key(station_path)
    write_public_key(peer_path)
    config_path = tmp_path / "legacy.yaml"
    config_path.write_text(
        "listen_ip: '::'\n"
        "listen_port: 50000\n"
        "remote_host: 192.0.2.10\n"
        "remote_port: 19999\n"
        "station_private_key: station.pem\n"
        "remote_public_key: peer.pem\n",
        encoding="utf-8",
    )

    result = run_nmea_sproxy_preflight(config_path)

    assert result.returncode == 0
    assert result.stdout == ""
    assert "DEPRECATION: legacy nmea_sproxy input configuration" in result.stderr
    assert "DEPRECATION: legacy nmea_sproxy UDPSEC output configuration" in (
        result.stderr
    )


def test_openwrt_nmea_sproxy_canonical_config_emits_no_deprecation(tmp_path):
    config_path = tmp_path / "canonical.yaml"
    config_path.write_text(
        CANONICAL_PROXY_UDP_INPUT + CANONICAL_PROXY_UDP_OUTPUT,
        encoding="utf-8",
    )

    result = run_nmea_sproxy_preflight(config_path)

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


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
