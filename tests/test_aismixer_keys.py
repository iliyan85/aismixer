import importlib.util
import os
import subprocess
import stat
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization


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
