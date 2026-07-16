"""Generic, profile-driven path policy.

Validation is fail-closed: every changed path must be explicitly allowed, must
not match a forbidden rule, and must resolve inside the registered repository.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, List

from ..domain.models import ProjectProfile

UNIVERSAL_FORBIDDEN_SUFFIXES = (".env",)
UNIVERSAL_FORBIDDEN_PREFIXES = (
    ".venv", "venv", ".git", "node_modules",
)


def _norm(path_str: str) -> str:
    norm = str(path_str).replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    return norm


def _matches_rule(path_str: str, rule: str) -> bool:
    norm = _norm(path_str)
    rule_norm = _norm(rule)
    name = norm.rsplit("/", 1)[-1]
    if rule_norm in {"*", "**"}:
        return True
    if rule_norm.startswith("*."):
        return name.endswith(rule_norm[1:])
    if rule_norm.endswith("/"):
        return norm == rule_norm.rstrip("/") or norm.startswith(rule_norm)
    return norm == rule_norm or norm.startswith(rule_norm + "/")


def is_forbidden(path_str: str, profile: ProjectProfile) -> str:
    if not path_str:
        return ""
    norm = _norm(path_str)
    name = norm.rsplit("/", 1)[-1]

    for forbidden in profile.forbidden_paths:
        if _matches_rule(norm, forbidden):
            return f"project-forbidden ({forbidden})"

    for suffix in UNIVERSAL_FORBIDDEN_SUFFIXES:
        if name.endswith(suffix) or norm == suffix:
            return f"forbidden suffix ({suffix})"

    for prefix in UNIVERSAL_FORBIDDEN_PREFIXES:
        if norm == prefix or norm.startswith(prefix + "/"):
            return f"off-limits area ({prefix})"
    return ""


def is_allowed_write(path_str: str, profile: ProjectProfile) -> bool:
    """True only when a changed path matches an explicit allowed rule."""
    if not path_str or not profile.allowed_write_paths:
        return False
    return any(_matches_rule(path_str, rule) for rule in profile.allowed_write_paths)


def detect_forbidden_changes(changed_paths: Iterable[str],
                             profile: ProjectProfile) -> List[str]:
    violations = []
    for path in changed_paths:
        reason = is_forbidden(path, profile)
        if reason:
            violations.append(f"{path}  ({reason})")
    return violations


def detect_disallowed_changes(changed_paths: Iterable[str],
                              profile: ProjectProfile) -> List[str]:
    violations = []
    for path in changed_paths:
        if not is_allowed_write(path, profile):
            violations.append(f"{path}  (not in allowed_write_paths)")
    return violations


def resolve_realpath(root: Path, rel: str) -> Path:
    return (Path(root).resolve() / rel).resolve()


def escapes_root(root: Path, rel: str) -> bool:
    try:
        resolve_realpath(root, rel).relative_to(Path(root).resolve())
        return False
    except ValueError:
        return True


def detect_path_violations(changed_paths: Iterable[str],
                           profile: ProjectProfile) -> List[str]:
    """Return all forbidden, non-allowlisted, and root-escape violations."""
    paths = list(dict.fromkeys(changed_paths))
    violations = detect_forbidden_changes(paths, profile)
    violations.extend(detect_disallowed_changes(paths, profile))
    root = Path(profile.repository_root).resolve()
    for path in paths:
        if escapes_root(root, path):
            violations.append(f"{path}  (resolves outside repository root)")
    return list(dict.fromkeys(violations))
