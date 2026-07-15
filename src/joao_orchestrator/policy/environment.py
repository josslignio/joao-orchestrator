"""Environment-variable allowlist policy.

Task subprocesses receive only explicitly allowed names.  Variables capable of
injecting code/configuration into Git, Python, dynamic loaders, or proxies are
always removed even if mistakenly listed by a project profile.
"""

from __future__ import annotations

import os
from typing import Iterable

from ..observability.redaction import scrub_env_for_subprocess, _looks_secret

_BLOCKED_EXACT = {
    "GIT_EXTERNAL_DIFF", "GIT_DIFF_OPTS", "GIT_SSH", "GIT_SSH_COMMAND",
    "GIT_ASKPASS", "GIT_CONFIG", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM",
    "GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_COUNT", "GIT_PAGER",
    "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONINSPECT",
    "PYTHONBREAKPOINT", "BASH_ENV", "ENV", "CDPATH", "PROMPT_COMMAND",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
}
_BLOCKED_PREFIXES = (
    "GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_", "LD_", "DYLD_",
)


def _is_injection_variable(name: str) -> bool:
    upper = name.upper()
    return upper in _BLOCKED_EXACT or any(upper.startswith(p) for p in _BLOCKED_PREFIXES)


def build_task_env(profile_allowlist: Iterable[str]) -> dict:
    """Build a strict, sanitized task environment from ``os.environ``."""
    allowlist = tuple(
        name for name in profile_allowlist
        if not _is_injection_variable(str(name))
    )
    env = scrub_env_for_subprocess(dict(os.environ), allowlist=allowlist)
    # Deterministic non-interactive behavior for tools that honor these vars.
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


def is_allowed_env_var(name: str, allowlist: Iterable[str]) -> bool:
    if _looks_secret(name, "") or _is_injection_variable(name):
        return False
    return name in set(allowlist)
