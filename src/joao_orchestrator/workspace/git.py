"""Read-only Git inspection helpers.

Only read-only subcommands are issued.  Calls use a minimal environment,
non-interactive mode, and safe diff flags that disable external diff and
text-conversion helpers.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import List, Tuple

_SAFE_SUBCOMMANDS = {"rev-parse", "rev-list", "status", "diff", "log", "ls-files", "show"}


def _git_env() -> dict:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", str(Path.home())),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_PAGER": "cat",
        "GIT_ATTR_NOSYSTEM": "1",
    }
    return env


def _run(root: Path, argv: List[str], timeout: int = 20,
         strip: bool = True) -> Tuple[str, int]:
    """Run a read-only Git command after enforcing safe options."""
    if len(argv) < 1 or argv[0] not in _SAFE_SUBCOMMANDS:
        raise ValueError(f"unsafe git subcommand requested: {argv}")
    safe_argv = list(argv)
    if safe_argv[0] == "diff":
        # These options are harmless when repeated and prevent local config
        # from spawning external helpers.
        for flag in ("--no-ext-diff", "--no-textconv"):
            if flag not in safe_argv[1:]:
                safe_argv.insert(1, flag)
    git_bin = shutil.which("git", path=_git_env()["PATH"]) or "git"
    try:
        proc = subprocess.run(
            [git_bin, "-c", "core.fsmonitor=false", "-c", "diff.external=",
             "-c", "core.pager=cat", "-C", str(Path(root).resolve())] + safe_argv,
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_git_env(),
        )
        out = proc.stdout
        return (out.strip() if strip else out), proc.returncode
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return "", 1


def current_branch(root: Path) -> str:
    out, _ = _run(root, ["rev-parse", "--abbrev-ref", "HEAD"])
    return out or "(unknown)"


def status_short(root: Path) -> str:
    out, _ = _run(root, ["status", "--short"])
    return out


def changed_paths(root: Path) -> List[str]:
    """Return all changed paths from porcelain-v1 NUL output.

    Both sides of renames/copies are returned.  Leading status whitespace is
    preserved during parsing, so unstaged changes cannot lose the first path
    character.
    """
    out, rc = _run(
        root,
        ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
        strip=False,
    )
    if rc != 0 or not out:
        return []
    records = out.split("\0")
    paths: List[str] = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        if len(record) < 4 or record[2] != " ":
            continue
        status = record[:2]
        path = record[3:]
        if path:
            paths.append(path)
        if any(code in status for code in ("R", "C")) and index < len(records):
            old_path = records[index]
            index += 1
            if old_path:
                paths.append(old_path)
    # Stable order without duplicates.
    return list(dict.fromkeys(paths))


def diff_stat(root: Path) -> str:
    out, _ = _run(root, ["diff", "--stat"])
    return out


def diff_check(root: Path) -> str:
    out, _ = _run(root, ["diff", "--check"])
    return out


def default_branch_commit(root: Path, default_branch: str = "main") -> str:
    out, _ = _run(root, ["rev-parse", default_branch])
    return out


def commits_behind(root: Path, base: str = "main") -> int:
    out, _ = _run(root, ["rev-list", "--count", f"HEAD..{base}"])
    try:
        return int(out)
    except (TypeError, ValueError):
        return 0
