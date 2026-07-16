#!/usr/bin/env python3
"""Install a local Finder-launchable JOAO Command Center app bundle."""
from __future__ import annotations

import argparse
import os
import plistlib
import shlex
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
APP = Path("/Applications/JOAO Command Center.app")

def write(path: Path, value: bytes, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)
    if mode is not None:
        os.chmod(path, mode)

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", type=Path, default=APP)
    parser.add_argument("--repo", type=Path, default=REPO)
    args = parser.parse_args()
    app = args.app.expanduser().resolve()
    repo = args.repo.expanduser().resolve()
    cli = repo / "src/joao_orchestrator/cli/joao.py"
    if not cli.is_file():
        parser.error(f"JOAO CLI not found: {cli}")
    contents = app / "Contents"
    info = {
        "CFBundleDisplayName": "JOAO Command Center",
        "CFBundleExecutable": "JOAO Command Center",
        "CFBundleIdentifier": "local.joss.joao-command-center",
        "CFBundleName": "JOAO Command Center",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "1.0",
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
    }
    log = Path("~/.local/share/joao/command-center.log").expanduser()
    launcher = (
        "#!/bin/zsh\n"
        "export PATH=\"$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin\"\n"
        f"mkdir -p {shlex.quote(str(log.parent))}\n"
        f"exec {shlex.quote(sys.executable)} {shlex.quote(str(cli))} ui "
        f">>{shlex.quote(str(log))} 2>&1\n"
    )
    write(contents / "Info.plist", plistlib.dumps(info))
    write(contents / "MacOS" / "JOAO Command Center", launcher.encode("utf-8"), 0o755)
    print(app)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
