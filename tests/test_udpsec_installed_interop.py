import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap


ROOT = Path(__file__).resolve().parents[1]
DEPLOYED_PYTHON_FILES = {
    "nmea_sproxy.py": ROOT / "nmea_sproxy" / "nmea_sproxy.py",
    "input_adapters.py": ROOT / "nmea_sproxy" / "input_adapters.py",
    "output_adapters.py": ROOT / "nmea_sproxy" / "output_adapters.py",
    "meta_cleaner.py": ROOT / "nmea_sproxy" / "meta_cleaner.py",
    "core/key_material.py": ROOT / "core" / "key_material.py",
    "core/network_policy.py": ROOT / "core" / "network_policy.py",
    "core/udpsec_crypto.py": ROOT / "core" / "udpsec_crypto.py",
    "core/udpsec_protocol.py": ROOT / "core" / "udpsec_protocol.py",
}
ESSENTIAL_ENVIRONMENT_NAMES = (
    "COMSPEC",
    "LD_LIBRARY_PATH",
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "WINDIR",
)


def _stage_installed_proxy(tmp_path):
    install_dir = tmp_path / "opt" / "nmea_sproxy"
    for relative_name, source in DEPLOYED_PYTHON_FILES.items():
        destination = install_dir / relative_name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    return install_dir


def _restricted_subprocess_environment(install_dir, bytecode_dir):
    environment = {
        name: os.environ[name]
        for name in ESSENTIAL_ENVIRONMENT_NAMES
        if name in os.environ
    }
    environment.update(
        {
            "PYTHONPATH": str(install_dir),
            "UDPSEC_BYTECODE_ROOT": str(bytecode_dir),
            "UDPSEC_REPOSITORY_ROOT": str(ROOT),
            "UDPSEC_STAGED_ROOT": str(install_dir),
        }
    )
    return environment


def test_installed_layout_imports_shared_udpsec_modules_in_isolation(tmp_path):
    install_dir = _stage_installed_proxy(tmp_path)
    expected_files = set(DEPLOYED_PYTHON_FILES)
    actual_files = {
        path.relative_to(install_dir).as_posix()
        for path in install_dir.rglob("*")
        if path.is_file()
    }
    assert actual_files == expected_files

    bytecode_dir = tmp_path / "compiled"
    child_code = textwrap.dedent(
        r"""
        import json
        import os
        from pathlib import Path
        import py_compile
        import sys

        staged_root = Path(os.environ["UDPSEC_STAGED_ROOT"]).resolve()
        repository_root = Path(
            os.environ["UDPSEC_REPOSITORY_ROOT"]
        ).resolve()
        bytecode_root = Path(os.environ["UDPSEC_BYTECODE_ROOT"]).resolve()
        bytecode_root.mkdir(parents=True)
        repository_import_roots = {
            repository_root,
            (repository_root / "nmea_sproxy").resolve(),
        }

        retained_paths = []
        for raw_path in sys.path:
            if not raw_path:
                continue
            candidate = Path(raw_path)
            if candidate.resolve() not in repository_import_roots:
                retained_paths.append(raw_path)
        sys.path[:] = [str(staged_root), *retained_paths]

        assert Path.cwd().resolve() == staged_root
        assert os.environ["PYTHONPATH"] == str(staged_root)
        assert all(
            Path(raw_path).resolve() not in repository_import_roots
            for raw_path in sys.path
        )

        staged_files = sorted(staged_root.rglob("*.py"))
        compiled = []
        for index, source in enumerate(staged_files):
            py_compile.compile(
                str(source),
                cfile=str(bytecode_root / f"{index}.pyc"),
                doraise=True,
            )
            compiled.append(source.relative_to(staged_root).as_posix())

        import nmea_sproxy
        import core.key_material as key_material
        import core.udpsec_crypto as udpsec_crypto
        import core.udpsec_protocol as udpsec_protocol

        expected_origins = {
            "nmea_sproxy": staged_root / "nmea_sproxy.py",
            "key_material": staged_root / "core" / "key_material.py",
            "udpsec_crypto": staged_root / "core" / "udpsec_crypto.py",
            "udpsec_protocol": staged_root / "core" / "udpsec_protocol.py",
        }
        actual_origins = {
            "nmea_sproxy": Path(nmea_sproxy.__file__).resolve(),
            "key_material": Path(key_material.__file__).resolve(),
            "udpsec_crypto": Path(udpsec_crypto.__file__).resolve(),
            "udpsec_protocol": Path(udpsec_protocol.__file__).resolve(),
        }
        assert actual_origins == {
            name: path.resolve()
            for name, path in expected_origins.items()
        }
        assert (
            nmea_sproxy.SessionKeyMaterial
            is udpsec_crypto.SessionKeyMaterial
        )
        assert (
            nmea_sproxy.SESSION_CONFIRMATION_SEQUENCE
            == udpsec_protocol.SESSION_CONFIRMATION_SEQUENCE
            == 0
        )

        print(
            json.dumps(
                {
                    "compiled": compiled,
                    "origins": {
                        name: str(path)
                        for name, path in actual_origins.items()
                    },
                    "sys_path": sys.path,
                },
                sort_keys=True,
            )
        )
        """
    )
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", child_code],
        cwd=install_dir,
        env=_restricted_subprocess_environment(
            install_dir,
            bytecode_dir,
        ),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, (
        f"isolated installed-layout subprocess failed\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )

    report = json.loads(result.stdout)
    assert set(report["compiled"]) == expected_files
    assert Path(report["origins"]["nmea_sproxy"]) == (
        install_dir / "nmea_sproxy.py"
    ).resolve()
    assert Path(report["origins"]["key_material"]) == (
        install_dir / "core" / "key_material.py"
    ).resolve()
    assert Path(report["origins"]["udpsec_crypto"]) == (
        install_dir / "core" / "udpsec_crypto.py"
    ).resolve()
    assert Path(report["origins"]["udpsec_protocol"]) == (
        install_dir / "core" / "udpsec_protocol.py"
    ).resolve()
    repository_import_roots = {
        ROOT.resolve(),
        (ROOT / "nmea_sproxy").resolve(),
    }
    assert all(
        Path(path).resolve() not in repository_import_roots
        for path in report["sys_path"]
    )

    actual_files_after_import = {
        path.relative_to(install_dir).as_posix()
        for path in install_dir.rglob("*")
        if path.is_file()
    }
    assert actual_files_after_import == expected_files
