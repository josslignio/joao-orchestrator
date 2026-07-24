"""RI-2: complete change capture — tracked, staged, untracked, deleted,
renamed, permissions, and symlinks. `git diff` alone is forbidden as proof.

The completeness guarantee comes from cross-validation, not from any single
git command: an independently captured `git status` path list (already used
elsewhere in this runtime for scope enforcement) is compared against the set
of paths the diff text actually mentions. A path present in one but not the
other is a completeness violation and is reported rather than silently
dropped — that is the actual attack surface behind ATTACK_TESTS 1 and 2
(an untracked file, or a staged-but-unstaged-looking file, invisible to a
naive `git diff` with no ref).
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

EMPTY_DIFF_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def _git(argv: list[str], cwd: Path, check: bool = True):
    return subprocess.run(["git"] + argv, cwd=str(cwd), shell=False,
                          capture_output=True, text=True, check=check)


def _status_codes(workspace: Path) -> dict[str, str]:
    """path -> 2-char porcelain status code (e.g. '??', ' M', 'A ', 'R ')."""
    raw = _git(["status", "--porcelain=v1", "-z", "--untracked-files=all"], workspace).stdout
    codes: dict[str, str] = {}
    records = raw.split("\0")
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record or len(record) < 4:
            continue
        status, path = record[:2], record[3:]
        if path:
            codes[path] = status
        if any(c in status for c in ("R", "C")) and index < len(records):
            old_path = records[index]
            index += 1
            if old_path:
                codes.setdefault(old_path, status)
    return codes


_DIFF_PATH_RE = re.compile(r"^diff --git a/(.+) b/(.+)$", re.MULTILINE)


def _paths_mentioned_in_diff(patch_text: str) -> set[str]:
    mentioned: set[str] = set()
    for a, b in _DIFF_PATH_RE.findall(patch_text):
        mentioned.add(a)
        mentioned.add(b)
    return mentioned


def capture_full_diff(workspace: Path, base_ref: str = "HEAD") -> dict[str, Any]:
    """Capture a complete diff (tracked+staged+untracked+deleted+renamed,
    with permission and symlink changes intact) against `base_ref`, then
    cross-validate it against an independently captured status listing.

    `base_ref` defaults to "HEAD" (unchanged default behavior for every
    existing caller). A0-3: passing a frozen baseline commit here instead of
    "HEAD" isolates a builder's own diff from whatever was already dirty in
    the workspace before the run started — the completeness cross-check
    below is still computed against the live `git status` (i.e. against the
    real HEAD, which never moves mid-run), so `missing_from_diff` when
    `base_ref != HEAD` legitimately includes anything not yet re-diffed
    against the alternate base; callers using a non-HEAD `base_ref` treat
    this call as a supplementary, informational capture, not the RI-2 gate
    itself (that gate always runs with the default `base_ref="HEAD"`).

    Returns: {"patch": bytes, "changed_paths": [...], "completeness_ok": bool,
              "missing_from_diff": [...], "extra_in_diff": [...]}
    """
    workspace = Path(workspace)
    codes = _status_codes(workspace)
    untracked = [p for p, code in codes.items() if code == "??"]
    added_intent = []
    for path in untracked:
        result = _git(["add", "-N", "--", path], workspace, check=False)
        if result.returncode == 0:
            added_intent.append(path)
    try:
        patch = _git(["diff", base_ref, "--binary", "--no-ext-diff", "--no-textconv", "-M"], workspace).stdout
    finally:
        if added_intent:
            _git(["reset", "--", *added_intent], workspace, check=False)

    mentioned = _paths_mentioned_in_diff(patch)
    status_paths = set(codes.keys())
    missing_from_diff = sorted(status_paths - mentioned)
    extra_in_diff = sorted(mentioned - status_paths)
    return {
        "patch": patch.encode(errors="replace"),
        "changed_paths": sorted(status_paths),
        "status_codes": codes,
        "completeness_ok": not missing_from_diff,
        "missing_from_diff": missing_from_diff,
        "extra_in_diff": extra_in_diff,
    }


def ignored_files_inventory(workspace: Path) -> list[str]:
    """A0-4: an explicit inventory of every gitignored file actually present
    on disk, via `git ls-files --others --ignored --exclude-standard` — the
    canonical plumbing command for "untracked AND ignored". `git add -A` /
    `git status` never see these paths at all (that is the whole point of
    `.gitignore`), so without this dedicated inventory a file like
    `payload.secret` next to a matching `*.secret` `.gitignore` rule is
    invisible to every other check in this module and would silently ride
    along in the workspace through build, review, and promotion.
    """
    workspace = Path(workspace)
    raw = _git(["ls-files", "--others", "--ignored", "--exclude-standard", "-z"], workspace, check=False).stdout
    return sorted(p for p in raw.split("\0") if p)
