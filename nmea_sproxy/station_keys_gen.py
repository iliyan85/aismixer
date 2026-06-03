#!/usr/bin/env python3
"""Compatibility wrapper for generating nmea_sproxy station keys."""

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


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
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
