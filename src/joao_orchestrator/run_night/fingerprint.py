"""Deterministic source fingerprints for read-only nights."""
from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

EXCLUDED = frozenset({"__pycache__", ".pytest_cache", ".mypy_cache"})


def _hash_entry(digest: "hashlib._Hash", base: Path, path: Path) -> None:
    try:
        stat = path.lstat()
        rel = path.relative_to(base).as_posix()
    except (FileNotFoundError, ValueError):
        return
    digest.update(rel.encode("utf-8", errors="surrogateescape"))
    digest.update(b"\0")
    digest.update(str(stat.st_mode).encode("ascii"))
    digest.update(b"\0")
    if path.is_symlink():
        digest.update(os.readlink(path).encode("utf-8", errors="surrogateescape"))
    elif path.is_file():
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    digest.update(b"\n")


def _git_output(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        raise ValueError(result.stderr.strip() or result.stdout.strip())
    return result.stdout


def fingerprint_tree(root: Path) -> str:
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"missing fingerprint root: {root}")
    digest = hashlib.sha256()

    if (root / ".git").exists():
        digest.update(b"git-status\0")
        digest.update(
            _git_output(root, "status", "--porcelain=v1", "--untracked-files=all")
            .encode("utf-8", errors="replace")
        )
        digest.update(b"\0")
        git_dir = Path(_git_output(root, "rev-parse", "--git-dir").strip())
        if not git_dir.is_absolute():
            git_dir = (root / git_dir).resolve()
        common_dir = Path(_git_output(root, "rev-parse", "--git-common-dir").strip())
        if not common_dir.is_absolute():
            common_dir = (root / common_dir).resolve()

        control = {
            git_dir / "HEAD",
            git_dir / "index",
            common_dir / "config",
            common_dir / "packed-refs",
        }
        for directory in (
            git_dir / "refs",
            git_dir / "logs",
            common_dir / "refs",
            common_dir / "logs",
            common_dir / "hooks",
        ):
            if directory.exists():
                control.update(
                    path for path in directory.rglob("*")
                    if path.is_file() or path.is_symlink()
                )
        for path in sorted(control, key=lambda value: str(value)):
            base = git_dir if git_dir in path.parents else common_dir
            _hash_entry(digest, base, path)
        return "git:" + digest.hexdigest()

    for current, dirs, names in os.walk(root):
        dirs[:] = sorted(name for name in dirs if name not in EXCLUDED)
        current_path = Path(current)
        for name in sorted(names):
            _hash_entry(digest, root, current_path / name)
    return "tree:" + digest.hexdigest()
