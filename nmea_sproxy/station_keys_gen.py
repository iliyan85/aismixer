#!/usr/bin/env python3
"""Deprecated compatibility wrapper for generating nmea_sproxy station keys.

This wrapper is retained only for backward compatibility. Use the canonical
tool instead:

    python3 tools/aismixer_keys.py station --keys-dir <dir>

It still delegates to that tool with the legacy ``station_private.key`` /
``station_public.pem`` filenames and, unless ``--keys-dir`` is given, writes
into this wrapper's own directory.
"""

from __future__ import annotations

import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
LEGACY_PRIVATE_NAME = "station_private.key"
LEGACY_PUBLIC_NAME = "station_public.pem"


def _load_key_tool():
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from tools import aismixer_keys

    return aismixer_keys


def _resolved_keys_dir(argv) -> str:
    """Return the directory this invocation will write to.

    The wrapper always forwards ``--keys-dir <SCRIPT_DIR>``; a later
    user-supplied ``--keys-dir`` overrides it because argparse takes the last
    value.
    """

    resolved = str(SCRIPT_DIR)
    tokens = iter(argv)
    for token in tokens:
        if token == "--keys-dir":
            resolved = next(tokens, resolved)
        elif token.startswith("--keys-dir="):
            resolved = token.split("=", 1)[1]
    return resolved


def _emit_deprecation_notice(argv) -> None:
    sys.stderr.write(
        "DEPRECATION: station_keys_gen.py is a deprecated compatibility "
        "wrapper.\n"
        "  Use the canonical tool: python3 tools/aismixer_keys.py station\n"
        f"  This invocation writes station keys to: {_resolved_keys_dir(argv)}\n"
    )


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    _emit_deprecation_notice(argv)
    key_tool = _load_key_tool()
    return key_tool.main(
        [
            "station",
            "--keys-dir",
            str(SCRIPT_DIR),
            "--private-name",
            LEGACY_PRIVATE_NAME,
            "--public-name",
            LEGACY_PUBLIC_NAME,
            *argv,
        ]
    )


if __name__ == "__main__":
    raise SystemExit(main())
