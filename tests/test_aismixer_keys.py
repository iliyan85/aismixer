import importlib.util
import os
import shutil
import subprocess
import stat
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

import core.key_material as key_material


ROOT = Path(__file__).resolve().parents[1]
KEY_TOOL_PATH = ROOT / "tools" / "aismixer_keys.py"
NMEA_SPROXY_DIR = ROOT / "nmea_sproxy"
STATION_WRAPPER_PATH = NMEA_SPROXY_DIR / "station_keys_gen.py"
LEGACY_STATION_PRIVATE_NAME = "station_private.key"
LEGACY_STATION_PUBLIC_NAME = "station_public.pem"


def load_key_tool():
    spec = importlib.util.spec_from_file_location("aismixer_keys", KEY_TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_private_key(path):
    return serialization.load_pem_private_key(path.read_bytes(), password=None)


def load_public_key(path):
    return serialization.load_pem_public_key(path.read_bytes())


def run_station_wrapper(cwd, script, keys_dir, *extra_args):
    return subprocess.run(
        [
            sys.executable,
            str(script),
            "--keys-dir",
            str(keys_dir),
            *extra_args,
        ],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )


def run_key_tool(cwd, script, keys_dir, *extra_args):
    return subprocess.run(
        [
            sys.executable,
            str(script),
            "server",
            "--keys-dir",
            str(keys_dir),
            *extra_args,
        ],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )


def test_server_key_files_are_created(tmp_path):
    tool = load_key_tool()

    result = tool.generate_key_pair(
        tmp_path,
        tool.SERVER_PRIVATE_NAME,
        tool.SERVER_PUBLIC_NAME,
    )

    assert result.private_path == tmp_path / tool.SERVER_PRIVATE_NAME
    assert result.public_path == tmp_path / tool.SERVER_PUBLIC_NAME
    assert result.private_path.exists()
    assert result.public_path.exists()


def test_server_cli_runs_outside_repository_after_runtime_extraction(tmp_path):
    installed_root = tmp_path / "installed"
    installed_tools = installed_root / "tools"
    installed_core = installed_root / "core"
    installed_tools.mkdir(parents=True)
    installed_core.mkdir()
    installed_tool = installed_tools / "aismixer_keys.py"
    shutil.copy2(KEY_TOOL_PATH, installed_tool)
    shutil.copy2(ROOT / "core" / "key_material.py", installed_core)

    unrelated_cwd = tmp_path / "unrelated-working-directory"
    unrelated_cwd.mkdir()
    keys_dir = tmp_path / "server-keys"

    result = run_key_tool(unrelated_cwd, installed_tool, keys_dir)

    assert result.returncode == 0, result.stderr
    private_path = keys_dir / "aismixer_private.pem"
    public_path = keys_dir / "aismixer_public.pem"
    assert private_path.exists()
    assert public_path.exists()
    assert (
        load_private_key(private_path).public_key().public_numbers()
        == load_public_key(public_path).public_numbers()
    )
    assert "Generated AISMixer server key pair" in result.stdout
    assert f"Private key: {private_path}" in result.stdout
    assert f"Public key:  {public_path}" in result.stdout


def test_station_cli_runs_outside_repository_after_runtime_extraction(tmp_path):
    installed_root = tmp_path / "installed"
    installed_tools = installed_root / "tools"
    installed_core = installed_root / "core"
    installed_tools.mkdir(parents=True)
    installed_core.mkdir()
    installed_tool = installed_tools / "aismixer_keys.py"
    shutil.copy2(KEY_TOOL_PATH, installed_tool)
    shutil.copy2(ROOT / "core" / "key_material.py", installed_core)

    unrelated_cwd = tmp_path / "unrelated-working-directory"
    unrelated_cwd.mkdir()
    keys_dir = tmp_path / "station-keys"
    result = subprocess.run(
        [
            sys.executable,
            str(installed_tool),
            "station",
            "--keys-dir",
            str(keys_dir),
            "--station-id",
            "dock_001",
        ],
        cwd=unrelated_cwd,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    private_path = keys_dir / "station_private.pem"
    public_path = keys_dir / "station_public.pem"
    loaded_private = load_private_key(private_path)
    loaded_public = load_public_key(public_path)
    assert isinstance(loaded_private.curve, ec.SECP256R1)
    assert isinstance(loaded_public.curve, ec.SECP256R1)
    assert (
        loaded_private.public_key().public_numbers()
        == loaded_public.public_numbers()
    )
    assert "Generated nmea_sproxy station key pair" in result.stdout
    assert "name: dock_001" in result.stdout


def test_generated_public_key_matches_private_key(tmp_path):
    tool = load_key_tool()
    result = tool.generate_key_pair(
        tmp_path,
        tool.STATION_PRIVATE_NAME,
        tool.STATION_PUBLIC_NAME,
    )

    private_key = load_private_key(result.private_path)
    public_key = load_public_key(result.public_path)

    assert private_key.public_key().public_numbers() == public_key.public_numbers()


def test_generated_key_format_and_curve_remain_udpsec_compatible(tmp_path):
    tool = load_key_tool()
    result = tool.generate_key_pair(
        tmp_path,
        tool.SERVER_PRIVATE_NAME,
        tool.SERVER_PUBLIC_NAME,
    )

    assert result.private_path.read_bytes().startswith(
        b"-----BEGIN EC PRIVATE KEY-----\n"
    )
    assert result.public_path.read_bytes().startswith(
        b"-----BEGIN PUBLIC KEY-----\n"
    )
    assert isinstance(load_private_key(result.private_path).curve, ec.SECP256R1)
    assert isinstance(load_public_key(result.public_path).curve, ec.SECP256R1)


def test_existing_files_are_not_overwritten_without_force(tmp_path):
    tool = load_key_tool()
    private_path = tmp_path / tool.SERVER_PRIVATE_NAME
    public_path = tmp_path / tool.SERVER_PUBLIC_NAME
    private_path.write_bytes(b"existing private")
    public_path.write_bytes(b"existing public")

    with pytest.raises(tool.KeyFileExistsError):
        tool.generate_key_pair(
            tmp_path,
            tool.SERVER_PRIVATE_NAME,
            tool.SERVER_PUBLIC_NAME,
        )

    assert private_path.read_bytes() == b"existing private"
    assert public_path.read_bytes() == b"existing public"


def _residual_temp_files(directory):
    return sorted(p.name for p in Path(directory).iterdir() if p.name.endswith(".tmp"))


def _seed_valid_pair(tmp_path, tool):
    """Create a real, valid old key pair at the default server names."""

    seed_dir = tmp_path / "seed"
    tool.generate_key_pair(seed_dir, tool.SERVER_PRIVATE_NAME, tool.SERVER_PUBLIC_NAME)
    private_path = tmp_path / tool.SERVER_PRIVATE_NAME
    public_path = tmp_path / tool.SERVER_PUBLIC_NAME
    old_private = (seed_dir / tool.SERVER_PRIVATE_NAME).read_bytes()
    old_public = (seed_dir / tool.SERVER_PUBLIC_NAME).read_bytes()
    private_path.write_bytes(old_private)
    public_path.write_bytes(old_public)
    return private_path, public_path, old_private, old_public


def test_force_overwrites_existing_files(tmp_path):
    tool = load_key_tool()
    private_path = tmp_path / tool.SERVER_PRIVATE_NAME
    public_path = tmp_path / tool.SERVER_PUBLIC_NAME
    private_path.write_bytes(b"existing private")
    public_path.write_bytes(b"existing public")

    tool.generate_key_pair(
        tmp_path,
        tool.SERVER_PRIVATE_NAME,
        tool.SERVER_PUBLIC_NAME,
        force=True,
    )

    assert private_path.read_bytes() != b"existing private"
    assert public_path.read_bytes() != b"existing public"
    load_private_key(private_path)
    load_public_key(public_path)


def test_force_success_replaces_pair_and_leaves_no_temp_files(tmp_path):
    tool = load_key_tool()
    private_path, public_path, old_private, old_public = _seed_valid_pair(tmp_path, tool)

    tool.generate_key_pair(
        tmp_path,
        tool.SERVER_PRIVATE_NAME,
        tool.SERVER_PUBLIC_NAME,
        force=True,
    )

    new_private = load_private_key(private_path)
    new_public = load_public_key(public_path)
    assert private_path.read_bytes() != old_private
    assert public_path.read_bytes() != old_public
    assert new_private.public_key().public_numbers() == new_public.public_numbers()
    assert _residual_temp_files(tmp_path) == []
    if os.name == "posix":
        assert stat.S_IMODE(private_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(public_path.stat().st_mode) == 0o644


def test_force_failure_before_replacement_preserves_old_pair(tmp_path, monkeypatch):
    tool = load_key_tool()
    private_path, public_path, old_private, old_public = _seed_valid_pair(tmp_path, tool)

    real_stage = key_material._stage_key_file
    calls = {"n": 0}

    def failing_stage(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("no space left while staging the public key")
        return real_stage(*args, **kwargs)

    monkeypatch.setattr(key_material, "_stage_key_file", failing_stage)

    with pytest.raises(OSError, match="no space left"):
        tool.generate_key_pair(
            tmp_path,
            tool.SERVER_PRIVATE_NAME,
            tool.SERVER_PUBLIC_NAME,
            force=True,
        )

    assert private_path.read_bytes() == old_private
    assert public_path.read_bytes() == old_public
    assert _residual_temp_files(tmp_path) == []


def test_force_failure_on_first_replace_preserves_old_pair(tmp_path, monkeypatch):
    tool = load_key_tool()
    private_path, public_path, old_private, old_public = _seed_valid_pair(tmp_path, tool)

    real_replace = key_material.os.replace

    def failing_replace(src, dst):
        if os.path.basename(os.fspath(dst)) == tool.SERVER_PRIVATE_NAME:
            raise OSError("cannot replace private path")
        return real_replace(src, dst)

    monkeypatch.setattr(key_material.os, "replace", failing_replace)

    with pytest.raises(OSError, match="cannot replace private path"):
        tool.generate_key_pair(
            tmp_path,
            tool.SERVER_PRIVATE_NAME,
            tool.SERVER_PUBLIC_NAME,
            force=True,
        )

    assert private_path.read_bytes() == old_private
    assert public_path.read_bytes() == old_public
    assert _residual_temp_files(tmp_path) == []


def test_force_failure_on_second_replace_leaves_recoverable_residual(tmp_path, monkeypatch):
    tool = load_key_tool()
    private_path, public_path, old_private, old_public = _seed_valid_pair(tmp_path, tool)

    real_replace = key_material.os.replace

    def failing_replace(src, dst):
        if os.path.basename(os.fspath(dst)) == tool.SERVER_PUBLIC_NAME:
            raise OSError("cannot replace public path")
        return real_replace(src, dst)

    monkeypatch.setattr(key_material.os, "replace", failing_replace)

    with pytest.raises(OSError, match="cannot replace public path"):
        tool.generate_key_pair(
            tmp_path,
            tool.SERVER_PRIVATE_NAME,
            tool.SERVER_PUBLIC_NAME,
            force=True,
        )

    # Residual: the new private is in place, the old public is untouched, and
    # the pair is intentionally NOT claimed to be atomic.
    assert private_path.read_bytes() != old_private
    assert public_path.read_bytes() == old_public
    assert _residual_temp_files(tmp_path) == []
    new_private = load_private_key(private_path)  # parseable

    # The public is recoverable from the new private via existing repair.
    result = tool.repair_public_key(
        tmp_path,
        tool.SERVER_PRIVATE_NAME,
        tool.SERVER_PUBLIC_NAME,
    )
    assert result.repaired is True
    assert (
        new_private.public_key().public_numbers()
        == load_public_key(public_path).public_numbers()
    )


def test_non_force_write_requests_kernel_exclusive_create(tmp_path, monkeypatch):
    tool = load_key_tool()
    key_names = {tool.SERVER_PRIVATE_NAME, tool.SERVER_PUBLIC_NAME}
    seen = []
    real_open = key_material.os.open

    def spy_open(path, flags, *rest):
        if os.path.basename(os.fspath(path)) in key_names:
            seen.append(flags)
        return real_open(path, flags, *rest)

    monkeypatch.setattr(key_material.os, "open", spy_open)

    def forbidden_replace(*_args, **_kwargs):
        raise AssertionError("os.replace must not run on the non-force path")

    monkeypatch.setattr(key_material.os, "replace", forbidden_replace)

    tool.generate_key_pair(tmp_path, tool.SERVER_PRIVATE_NAME, tool.SERVER_PUBLIC_NAME)

    assert len(seen) == 2, "expected exclusive-create opens for both key files"
    assert all(flags & os.O_EXCL for flags in seen)
    assert all(not (flags & os.O_TRUNC) for flags in seen)


def test_non_force_exclusive_create_still_refuses_a_raced_in_file(tmp_path, monkeypatch):
    tool = load_key_tool()
    key_names = {tool.SERVER_PRIVATE_NAME, tool.SERVER_PUBLIC_NAME}
    private_path = tmp_path / tool.SERVER_PRIVATE_NAME
    real_exists = key_material.Path.exists

    def selective_exists(self):
        if self.name in key_names:
            return False  # hide the raced-in file from the Python pre-check
        return real_exists(self)

    monkeypatch.setattr(key_material.Path, "exists", selective_exists, raising=True)
    private_path.write_bytes(b"raced-in private")

    with pytest.raises(tool.KeyFileExistsError):
        tool.generate_key_pair(
            tmp_path,
            tool.SERVER_PRIVATE_NAME,
            tool.SERVER_PUBLIC_NAME,
        )

    # Enforcement fell to O_CREAT | O_EXCL, not the pre-check; file untouched.
    assert private_path.read_bytes() == b"raced-in private"


def test_key_file_permissions_are_set_on_posix(tmp_path):
    if os.name != "posix":
        pytest.skip("POSIX file modes are not portable on this platform")

    tool = load_key_tool()
    result = tool.generate_key_pair(
        tmp_path,
        tool.SERVER_PRIVATE_NAME,
        tool.SERVER_PUBLIC_NAME,
    )

    assert stat.S_IMODE(result.private_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(result.public_path.stat().st_mode) == 0o644


def test_repair_public_creates_missing_public_without_replacing_private(tmp_path):
    tool = load_key_tool()
    generated = tool.generate_key_pair(
        tmp_path,
        tool.SERVER_PRIVATE_NAME,
        tool.SERVER_PUBLIC_NAME,
    )
    original_private = generated.private_path.read_bytes()
    generated.public_path.unlink()

    result = tool.repair_public_key(
        tmp_path,
        tool.SERVER_PRIVATE_NAME,
        tool.SERVER_PUBLIC_NAME,
    )

    assert result.repaired is True
    assert result.private_path.read_bytes() == original_private
    assert (
        load_private_key(result.private_path).public_key().public_numbers()
        == load_public_key(result.public_path).public_numbers()
    )


def test_repair_public_overwrites_only_mismatched_public(tmp_path):
    tool = load_key_tool()
    generated = tool.generate_key_pair(
        tmp_path,
        tool.SERVER_PRIVATE_NAME,
        tool.SERVER_PUBLIC_NAME,
    )
    original_private = generated.private_path.read_bytes()
    other_keys = tmp_path / "other"
    other = tool.generate_key_pair(
        other_keys,
        tool.SERVER_PRIVATE_NAME,
        tool.SERVER_PUBLIC_NAME,
    )
    generated.public_path.write_bytes(other.public_path.read_bytes())

    result = tool.repair_public_key(
        tmp_path,
        tool.SERVER_PRIVATE_NAME,
        tool.SERVER_PUBLIC_NAME,
    )

    assert result.repaired is True
    assert result.private_path.read_bytes() == original_private
    assert (
        load_private_key(result.private_path).public_key().public_numbers()
        == load_public_key(result.public_path).public_numbers()
    )


def test_repair_public_reports_noop_when_public_matches(tmp_path, capsys):
    tool = load_key_tool()
    generated = tool.generate_key_pair(
        tmp_path,
        tool.SERVER_PRIVATE_NAME,
        tool.SERVER_PUBLIC_NAME,
    )
    original_private = generated.private_path.read_bytes()
    original_public = generated.public_path.read_bytes()

    rc = tool.main(["server", "--keys-dir", str(tmp_path), "--repair-public"])

    captured = capsys.readouterr()
    assert rc == 0
    assert "already matches private key; no repair needed" in captured.out
    assert generated.private_path.read_bytes() == original_private
    assert generated.public_path.read_bytes() == original_public


def test_repair_public_does_not_generate_missing_private_key(tmp_path, capsys):
    tool = load_key_tool()

    rc = tool.main(["server", "--keys-dir", str(tmp_path), "--repair-public"])

    captured = capsys.readouterr()
    assert rc == 1
    assert "Unable to repair public key" in captured.err
    assert not (tmp_path / tool.SERVER_PRIVATE_NAME).exists()
    assert not (tmp_path / tool.SERVER_PUBLIC_NAME).exists()


def test_repair_public_key_permissions_are_set_on_posix(tmp_path):
    if os.name != "posix":
        pytest.skip("POSIX file modes are not portable on this platform")

    tool = load_key_tool()
    generated = tool.generate_key_pair(
        tmp_path,
        tool.SERVER_PRIVATE_NAME,
        tool.SERVER_PUBLIC_NAME,
    )
    generated.private_path.chmod(0o644)
    generated.public_path.chmod(0o600)

    tool.repair_public_key(
        tmp_path,
        tool.SERVER_PRIVATE_NAME,
        tool.SERVER_PUBLIC_NAME,
    )

    assert stat.S_IMODE(generated.private_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(generated.public_path.stat().st_mode) == 0o644


def test_cli_returns_nonzero_on_unsafe_overwrite(tmp_path, capsys):
    tool = load_key_tool()
    tool.generate_key_pair(
        tmp_path,
        tool.SERVER_PRIVATE_NAME,
        tool.SERVER_PUBLIC_NAME,
    )

    rc = tool.main(["server", "--keys-dir", str(tmp_path)])

    captured = capsys.readouterr()
    assert rc == 1
    assert "Refusing to overwrite" in captured.err


def test_station_cli_prints_authorized_keys_guidance(tmp_path, capsys):
    tool = load_key_tool()

    rc = tool.main(
        [
            "station",
            "--keys-dir",
            str(tmp_path),
            "--station-id",
            "dock_001",
        ]
    )

    captured = capsys.readouterr()
    assert rc == 0
    assert "authorized_clients:" in captured.out
    assert "name: dock_001" in captured.out
    assert "pubkey:" in captured.out


def test_station_repair_prints_authorized_keys_guidance(tmp_path, capsys):
    tool = load_key_tool()
    generated = tool.generate_key_pair(
        tmp_path,
        tool.STATION_PRIVATE_NAME,
        tool.STATION_PUBLIC_NAME,
    )
    generated.public_path.unlink()
    private_key = load_private_key(generated.private_path)
    expected_b64 = tool._compressed_public_b64(private_key.public_key())

    rc = tool.main(
        [
            "station",
            "--keys-dir",
            str(tmp_path),
            "--station-id",
            "dock_001",
            "--repair-public",
        ]
    )

    captured = capsys.readouterr()
    assert rc == 0
    assert "Repaired nmea_sproxy station public key" in captured.out
    assert "authorized_clients:" in captured.out
    assert "name: dock_001" in captured.out
    assert f"pubkey: {expected_b64}" in captured.out


def test_station_keys_gen_runs_from_repo_root(tmp_path):
    result = run_station_wrapper(ROOT, STATION_WRAPPER_PATH, tmp_path)

    assert result.returncode == 0, result.stderr
    assert (tmp_path / LEGACY_STATION_PRIVATE_NAME).exists()
    assert (tmp_path / LEGACY_STATION_PUBLIC_NAME).exists()
    assert "authorized_clients:" in result.stdout


def test_station_keys_gen_runs_from_nmea_sproxy_dir(tmp_path):
    result = run_station_wrapper(NMEA_SPROXY_DIR, "station_keys_gen.py", tmp_path)

    assert result.returncode == 0, result.stderr
    assert (tmp_path / LEGACY_STATION_PRIVATE_NAME).exists()
    assert (tmp_path / LEGACY_STATION_PUBLIC_NAME).exists()
    assert "authorized_clients:" in result.stdout


def test_station_keys_gen_does_not_overwrite_existing_keys_by_default(tmp_path):
    first = run_station_wrapper(ROOT, STATION_WRAPPER_PATH, tmp_path)
    assert first.returncode == 0, first.stderr

    private_path = tmp_path / LEGACY_STATION_PRIVATE_NAME
    public_path = tmp_path / LEGACY_STATION_PUBLIC_NAME
    original_private = private_path.read_bytes()
    original_public = public_path.read_bytes()

    second = run_station_wrapper(ROOT, STATION_WRAPPER_PATH, tmp_path)

    assert second.returncode == 1
    assert "Refusing to overwrite" in second.stderr
    assert private_path.read_bytes() == original_private
    assert public_path.read_bytes() == original_public


def test_station_keys_gen_force_overwrites_existing_keys(tmp_path):
    first = run_station_wrapper(ROOT, STATION_WRAPPER_PATH, tmp_path)
    assert first.returncode == 0, first.stderr

    private_path = tmp_path / LEGACY_STATION_PRIVATE_NAME
    public_path = tmp_path / LEGACY_STATION_PUBLIC_NAME
    original_private = private_path.read_bytes()
    original_public = public_path.read_bytes()

    second = run_station_wrapper(ROOT, STATION_WRAPPER_PATH, tmp_path, "--force")

    assert second.returncode == 0, second.stderr
    assert private_path.read_bytes() != original_private
    assert public_path.read_bytes() != original_public
    load_private_key(private_path)
    load_public_key(public_path)


def load_station_wrapper():
    spec = importlib.util.spec_from_file_location(
        "station_keys_gen", STATION_WRAPPER_PATH
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_station_keys_gen_emits_deprecation_notice_and_still_works(tmp_path):
    result = run_station_wrapper(ROOT, STATION_WRAPPER_PATH, tmp_path)

    assert result.returncode == 0, result.stderr
    assert "DEPRECATION" in result.stderr
    assert "tools/aismixer_keys.py station" in result.stderr
    assert str(tmp_path) in result.stderr
    # No secret material is echoed by the notice.
    assert "PRIVATE KEY" not in result.stderr
    # Delegation is unchanged: keys are still produced with the legacy names.
    assert (tmp_path / LEGACY_STATION_PRIVATE_NAME).exists()
    assert (tmp_path / LEGACY_STATION_PUBLIC_NAME).exists()
    assert "authorized_clients:" in result.stdout


def test_station_keys_gen_notice_reports_resolved_output_directory():
    wrapper = load_station_wrapper()

    assert wrapper._resolved_keys_dir([]) == str(wrapper.SCRIPT_DIR)
    assert wrapper._resolved_keys_dir(["--keys-dir", "/tmp/custom"]) == "/tmp/custom"
    assert wrapper._resolved_keys_dir(["--keys-dir=/tmp/eq"]) == "/tmp/eq"
    assert wrapper.SCRIPT_DIR.name == "nmea_sproxy"
    assert "deprecated" in (wrapper.__doc__ or "").lower()
