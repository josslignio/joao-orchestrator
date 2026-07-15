"""V2 GitHub CLI (gh) discovery + environment recovery (item 1).

Integrates the verified operator environment into evidence-first discovery.
This is the direct, complete fix for the historical false blockers §4 #1/#3
("gh exists outside PATH but was reported missing").

Responsibilities:

1. **discover gh outside PATH** — probe absolute-path candidate roots and
   VERIFY by running ``gh --version`` (delegates to :func:`resolve_gh` so the
   logic stays single-sourced).
2. **recover GH_CONFIG_DIR from the known-good registry** — gh stores its
   config under a directory that is NOT on PATH and NOT inferable from the
   binary location. We persist the *path* (never the token / auth-file
   contents) in the known-good registry so future runs recover it without
   re-discovery.
3. **run ``gh auth status`` with the recovered environment** and classify the
   current environment.
4. **classify** into one of:

   - ``GH_AVAILABLE_AND_AUTHENTICATED``
   - ``GH_AVAILABLE_NOT_AUTHENTICATED``
   - ``GH_MISSING``

Security invariants (item 1 requirements + §31):

- persist PATHS and CONFIG DIR only — NEVER tokens, auth-file contents, or
  the ``hosts.yml`` body;
- the classification record is redacted before persistence;
- the auth-status output is parsed for the boolean verdict only; the raw
  output (which may contain account hints) is never persisted.

Subprocess policy (§5): all command execution goes through the injectable
``CommandRunner`` callable (list argv, no shell). This module contains no
``subprocess`` usage of its own except via :func:`default_runner`.
"""

from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from .preflight import (
    CommandRunner, KnownGoodStore, KnownTool, default_runner, resolve_gh,
)

# Candidate GH_CONFIG_DIR locations, searched in order. These hold gh's
# non-secret config (hosts.yml points at the active account; the actual token
# lives in the OS keyring, not in these files).
_GH_CONFIG_CANDIDATES = (
    Path.home() / ".local" / "share" / "gh-cli",
    Path.home() / ".config" / "gh",
    Path.home() / "Library" / "Application Support" / "gh",
)

# Classification vocabulary (item 1).
GH_AVAILABLE_AND_AUTHENTICATED = "GH_AVAILABLE_AND_AUTHENTICATED"
GH_AVAILABLE_NOT_AUTHENTICATED = "GH_AVAILABLE_NOT_AUTHENTICATED"
GH_MISSING = "GH_MISSING"

# Substrings in `gh auth status` output that indicate an authenticated state.
_AUTH_OK_MARKERS = (
    re.compile(r"logged in to github\.com", re.I),
    re.compile(r"account\s+\S+\s*\(", re.I),
    re.compile(r"active account:\s*true", re.I),
)
# Substrings that indicate NOT authenticated (when gh is present).
_AUTH_FAIL_MARKERS = (
    re.compile(r"not logged in", re.I),
    re.compile(r"you are not logged", re.I),
    re.compile(r"no.*account.*found", re.I),
    re.compile(r"to log in, run", re.I),
)

# Keys we REFUSE to persist (defence in depth — item 1 + §31).
_FORBIDDEN_PERSIST_KEYS = ("token", "oauth_token", "password", "secret",
                           "passwd", "api_key", "credential", "hosts_yml_body",
                           "config_yml_body", "raw_auth_output")


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class GhDiscoveryResult:
    """Outcome of gh discovery + auth classification (item 1)."""
    classification: str               # one of the GH_* constants
    gh_path: str = ""                # absolute path, "" if missing
    gh_version: str = ""
    gh_config_dir: str = ""          # path only, never contents
    gh_config_dir_source: str = ""   # "registry" | "candidate" | "default"
    authenticated_account: str = ""  # account hint only (no token)
    auth_verified: bool = False
    false_blocker_prevented: bool = False   # True when gh found outside PATH
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # Defence in depth: never persist anything that looks like a secret.
        return _redact(d)


@dataclass
class GhEnvironmentRecord:
    """The persistable subset of the gh environment (paths only)."""
    gh_path: str = ""
    gh_config_dir: str = ""
    classification: str = GH_MISSING
    verified_at: str = ""
    gh_version: str = ""

    def to_dict(self) -> dict[str, Any]:
        return _redact(asdict(self))


def _redact(obj: Any) -> Any:
    """Recursively drop any key that looks like a secret."""
    if isinstance(obj, Mapping):
        return {k: v for k, v in (
            (k, _redact(v)) for k, v in obj.items()
        ) if not any(f in str(k).lower() for f in _FORBIDDEN_PERSIST_KEYS)}
    if isinstance(obj, (list, tuple)):
        return [_redact(v) for v in obj]
    return obj


# ---------------------------------------------------------------------------
# GH_CONFIG_DIR recovery
# ---------------------------------------------------------------------------

def recover_gh_config_dir(
    *, registry: KnownGoodStore | None = None,
    candidates: Sequence[Path] = _GH_CONFIG_CANDIDATES,
) -> tuple[Path, str]:
    """Recover the GH_CONFIG_DIR, preferring the known-good registry.

    Returns ``(config_dir, source)`` where source is one of
    ``"registry" | "candidate" | "default"``. The token/auth-file contents are
    NEVER read; we only confirm the directory exists and return its path.
    """
    # 1. registry first (recover from a prior run without re-discovery)
    if registry is not None:
        rec = registry.get_tool("gh_config_dir")
        # KnownGoodStore stores KnownTool objects; we stash the config dir in
        # its 'version' field as a lightweight side-channel (path only).
        if rec is not None and rec.status == "WORKING":
            p = Path(rec.version)
            if p.is_dir():
                return p, "registry"

    # 2. candidate locations
    for cand in candidates:
        if cand.is_dir():
            return cand, "candidate"

    # 3. default (gh's own default when GH_CONFIG_DIR is unset)
    return Path.home() / ".config" / "gh", "default"


# ---------------------------------------------------------------------------
# Auth classification
# ---------------------------------------------------------------------------

def classify_auth_status(auth_output: str, auth_rc: int) -> tuple[bool, str]:
    """Parse ``gh auth status`` output into (authenticated, account_hint).

    Returns (False, "") when the output indicates not-authenticated or when the
    command failed. We never persist the raw output — only the parsed boolean
    and a non-token account hint.
    """
    if auth_rc != 0:
        # gh auth status returns nonzero when not logged in; check markers.
        if any(p.search(auth_output) for p in _AUTH_FAIL_MARKERS):
            return False, ""
        # Ambiguous nonzero — treat as not authenticated.
        return False, ""
    if any(p.search(auth_output) for p in _AUTH_OK_MARKERS):
        # Extract a non-token account hint if present.
        m = re.search(r"account\s+(\S+)", auth_output, re.I)
        account = m.group(1).strip("()") if m else ""
        return True, account
    return False, ""


# ---------------------------------------------------------------------------
# Full discovery
# ---------------------------------------------------------------------------

def discover_gh_environment(
    *, project_id: str = "__operator__",
    runner: CommandRunner = default_runner,
    registry: KnownGoodStore | None = None,
    persist: bool = True,
) -> GhDiscoveryResult:
    """Run the full item-1 gh discovery + classification.

    Steps:
      1. resolve gh by absolute path (delegates to :func:`resolve_gh`);
      2. recover GH_CONFIG_DIR (registry → candidate → default);
      3. run ``gh auth status`` with GH_CONFIG_DIR in the environment;
      4. classify + (optionally) persist paths/config only.
    """
    result = GhDiscoveryResult(classification=GH_MISSING)

    # 1. resolve gh (false-blocker detection)
    gh = resolve_gh(runner=runner)
    result.gh_path = gh.path
    result.gh_version = gh.version
    if gh.status != "WORKING":
        result.classification = GH_MISSING
        result.notes.append("gh not resolved by absolute path or PATH")
        return result

    # Detect the false-blocker condition: gh works but is NOT on PATH.
    on_path = bool(os.environ.get("PATH", "")) and any(
        (Path(d) / "gh").exists()
        for d in os.environ.get("PATH", "").split(os.pathsep) if d
    )
    if not on_path:
        result.false_blocker_prevented = True
        result.notes.append(
            "gh resolved outside PATH via absolute-path verification "
            "(§4 #1,#3 false blocker prevented)")

    store = registry or KnownGoodStore(project_id)

    # 2. recover GH_CONFIG_DIR
    config_dir, source = recover_gh_config_dir(registry=store)
    result.gh_config_dir = str(config_dir)
    result.gh_config_dir_source = source

    # 3. run gh auth status with the recovered env. Use the RESOLVED ABSOLUTE
    # gh path (never the bare name) so auth works even when gh is off PATH —
    # this is the same false-blocker we just detected, applied consistently.
    gh_bin = result.gh_path or "gh"
    env = {**os.environ, "GH_CONFIG_DIR": str(config_dir)}
    rc, out, _ = runner([gh_bin, "auth", "status"], None, 15, env=env) \
        if _runner_supports_env(runner) else _run_with_env(
            [gh_bin, "auth", "status"], str(config_dir), runner)
    authenticated, account = classify_auth_status(out, rc)
    result.auth_verified = True
    result.authenticated_account = account

    # 4. classify
    result.classification = (
        GH_AVAILABLE_AND_AUTHENTICATED if authenticated
        else GH_AVAILABLE_NOT_AUTHENTICATED)

    # 5. persist paths/config ONLY (never tokens / file contents).
    if persist:
        _persist_environment(store, result)
    return result


# ---------------------------------------------------------------------------
# Persistence (paths/config only)
# ---------------------------------------------------------------------------

def _persist_environment(store: KnownGoodStore, result: GhDiscoveryResult) -> None:
    """Persist the gh PATH and CONFIG DIR (never tokens/file contents)."""
    # Persist gh path as a KnownTool.
    store.set_tool(KnownTool(
        name="gh", path=result.gh_path, status="WORKING",
        verified_at=result.gh_version and _now() or "",
        version=result.gh_version,
    ))
    # Persist GH_CONFIG_DIR as a KnownTool side-channel (path in 'version').
    store.set_tool(KnownTool(
        name="gh_config_dir", path=result.gh_config_dir,
        status="WORKING" if result.gh_config_dir else "MISSING",
        verified_at=_now(),
        version=result.gh_config_dir,   # path only
    ))


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Runner env-handling helpers
# ---------------------------------------------------------------------------

def _runner_supports_env(runner: CommandRunner) -> bool:
    """True if the runner signature accepts an ``env`` kwarg."""
    import inspect
    try:
        sig = inspect.signature(runner)
        return "env" in sig.parameters
    except (TypeError, ValueError):
        return False


def _run_with_env(
    argv: Sequence[str], config_dir: str, runner: CommandRunner,
) -> tuple[int, str, str]:
    """Run ``argv`` with GH_CONFIG_DIR set, for runners without an env param.

    Sets the env var in ``os.environ`` for the duration of the call (restored
    afterward). This is the bridge for the default :func:`default_runner`.
    """
    saved = os.environ.get("GH_CONFIG_DIR")
    os.environ["GH_CONFIG_DIR"] = config_dir
    try:
        return runner(argv, None, 15)
    finally:
        if saved is None:
            os.environ.pop("GH_CONFIG_DIR", None)
        else:
            os.environ["GH_CONFIG_DIR"] = saved


__all__ = [
    "GH_AVAILABLE_AND_AUTHENTICATED", "GH_AVAILABLE_NOT_AUTHENTICATED",
    "GH_MISSING", "GhDiscoveryResult", "GhEnvironmentRecord",
    "recover_gh_config_dir", "classify_auth_status",
    "discover_gh_environment",
]
