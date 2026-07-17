#!/usr/bin/env python3
"""Install the Finder-launchable JOÃO.AI app bundle for THIS worktree.

Fixes over the original installer: the launcher exports a provider-capable
PATH (Finder apps start with a minimal one), logs to a stable file, uses the
installing interpreter's absolute path, and always points at the repository
this script is run from — never a stale worktree.
"""
from __future__ import annotations

import argparse
import os
import plistlib
import shlex
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
APP_NAME = "JOÃO.AI"
APP = Path(f"/Applications/{APP_NAME}.app")

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
    executable_name = app.stem
    contents = app / "Contents"
    info = {
        "CFBundleDisplayName": APP_NAME,
        "CFBundleExecutable": executable_name,
        "CFBundleIdentifier": "ai.joao.command-center",
        "CFBundleName": APP_NAME,
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "1.2",
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
    write(contents / "MacOS" / executable_name, launcher.encode("utf-8"), 0o755)
    print(app)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
