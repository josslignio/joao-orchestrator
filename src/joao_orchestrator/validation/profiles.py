"""Validation profile loader.

Profiles live at <repo>/.agent/validation.toml and declare structured offline
commands.  Definitions are policy-validated when loaded; a malicious profile
cannot bless a wrapper, package installer, inline Python, or arbitrary Git
arguments simply by naming them.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path
from typing import List

from ..domain.models import ProjectProfile
from ..policy.commands import CommandDefinition, validate_argv


class ValidationProfileError(ValueError):
    """Raised when a validation profile is absent, empty, or unsafe."""


def _resolve_python(profile: ProjectProfile) -> str:
    root = Path(profile.repository_root)
    strat = profile.python_strategy
    if strat == ".venv/bin/python":
        venv_py = root / ".venv" / "bin" / "python"
        if venv_py.is_file():
            return str(venv_py)
    if sys.executable:
        return sys.executable
    return "python3"


def _resolve_git() -> str:
    for candidate in ("/usr/bin/git", "/opt/homebrew/bin/git"):
        if Path(candidate).is_file():
            return candidate
    return "git"


def load_profile_commands(validation_toml: Path,
                           project_profile: ProjectProfile) -> List[CommandDefinition]:
    """Load and policy-check structured command definitions."""
    validation_toml = Path(validation_toml)
    if not validation_toml.is_file():
        raise ValidationProfileError(f"validation profile not found: {validation_toml}")
    with open(validation_toml, "rb") as fh:
        data = tomllib.load(fh)

    raw_commands = data.get("commands", [])
    if not isinstance(raw_commands, list):
        raise ValidationProfileError("validation.toml commands must be an array")

    py = _resolve_python(project_profile)
    git = _resolve_git()
    resolver = {"python": py, "git": git}
    commands = [
        CommandDefinition.from_dict(entry, resolve=lambda role: resolver[role])
        for entry in raw_commands
    ]

    approved, signatures = signatures_for(commands)
    for index, command in enumerate(commands, start=1):
        if command.timeout <= 0:
            raise ValidationProfileError(f"command {index}: timeout must be positive")
        argv = [command.executable] + list(command.args) + list(command.args_extra)
        ok, reason = validate_argv(argv, approved, signatures)
        if not ok:
            raise ValidationProfileError(f"command {index} rejected: {reason}")
    return commands


def commands_for_profile(defs: List[CommandDefinition], profile_name: str) -> List[CommandDefinition]:
    selected = [d for d in defs if d.profile == profile_name]
    if not selected:
        raise ValidationProfileError(
            f"validation profile {profile_name!r} contains no commands"
        )
    return selected


def signatures_for(defs: List[CommandDefinition]):
    approved = {d.executable for d in defs}
    signatures = [
        (d.executable, list(d.args), list(d.extra_args_allowed))
        for d in defs
    ]
    return approved, signatures
