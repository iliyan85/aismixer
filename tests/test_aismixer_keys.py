import importlib.util
import os
import stat
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization


ROOT = Path(__file__).resolve().parents[1]
KEY_TOOL_PATH = ROOT / "tools" / "aismixer_keys.py"


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
