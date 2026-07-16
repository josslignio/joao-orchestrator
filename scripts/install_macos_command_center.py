#!/usr/bin/env python3
"""Install a local Finder-launchable JOAO Command Center app bundle."""
from __future__ import annotations

import os
import plistlib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
APP = Path("/Applications/JOAO Command Center.app")

def write(path: Path, value: bytes, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)
    if mode is not None:
        os.chmod(path, mode)

def main() -> int:
    contents = APP / "Contents"
    info = {
        "CFBundleDisplayName": "JOAO Command Center",
        "CFBundleExecutable": "JOAO Command Center",
        "CFBundleIdentifier": "local.joss.joao-command-center",
        "CFBundleName": "JOAO Command Center",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "0.1",
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
    }
    launcher = "#!/bin/zsh\\n" + f"exec /usr/bin/env python3 {REPO / 'src/joao_orchestrator/cli/joao.py'} ui\\n"
    write(contents / "Info.plist", plistlib.dumps(info))
    write(contents / "MacOS" / "JOAO Command Center", launcher.encode("utf-8"), 0o755)
    print(APP)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
