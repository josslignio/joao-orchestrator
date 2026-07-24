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

# A0-4: gitignored files are, by construction, invisible to every git-based
# capture in this runtime (`git add -A`, `git status`, `git diff`) — that is
# exactly why `.gitignore` exists. A gitignored file whose NAME matches one
# of these patterns is treated as landing in a "sensitive runtime path" and
# is refused outright wherever it is found during a run, rather than being
# silently allowed to ride along untracked. This is a deliberately narrow,
# name-pattern policy (documented here, not inferred): credential/keypair/
# secret-shaped filenames. It does not attempt to inspect file CONTENT for
# secrets — only the name.
SENSITIVE_IGNORED_PATTERNS = (
    "*.secret", "*.pem", "*.key", "*.p12", "*.pfx", "*.keystore", "*.jks",
    "*.env", ".env", ".env.*", "id_rsa", "id_rsa.*", "id_ed25519", "id_ed25519.*",
    "credentials", "credentials.*", "*_credentials.*", "secrets.json", "secrets.yaml", "secrets.yml",
)


def _matches_sensitive_pattern(name: str) -> str:
    for pattern in SENSITIVE_IGNORED_PATTERNS:
        if pattern.startswith("*."):
            if name.endswith(pattern[1:]):
                return pattern
        elif pattern.endswith(".*"):
            if name == pattern[:-2] or name.startswith(pattern[:-1]):
                return pattern
        elif name == pattern:
            return pattern
    return ""


def detect_sensitive_ignored_files(ignored_paths: Iterable[str]) -> List[str]:
    """A0-4: of the paths git reports as ignored-and-present-on-disk (see
    `change_capture.ignored_files_inventory`), return the ones whose
    filename matches `SENSITIVE_IGNORED_PATTERNS` — these are refused
    outright rather than silently tolerated as "just build noise"."""
    violations = []
    for path in ignored_paths:
        name = _norm(path).rsplit("/", 1)[-1]
        pattern = _matches_sensitive_pattern(name)
        if pattern:
            violations.append(f"{path}  (gitignored, matches sensitive pattern {pattern})")
    return violations


def _norm(path_str: str) -> str:
    norm = str(path_str).replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    return norm


def _matches_rule(path_str: str, rule: str) -> bool:
    norm = _norm(path_str)
    rule_norm = _norm(rule)
    name = norm.rsplit("/", 1)[-1]
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
