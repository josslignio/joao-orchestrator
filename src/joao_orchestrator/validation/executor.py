"""Restricted validation executor (migrated + generalized from V1.4.0).

Runs ONLY structured commands declared in the project's validation profile.
shell=False; argv arrays only; secret redaction; output caps; timeouts.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from ..domain.models import CommandResult, ProjectProfile
from ..observability.redaction import scrub_env_for_subprocess, redact
from ..policy.commands import CommandDefinition, validate_argv
from ..policy.environment import build_task_env
from .profiles import _resolve_python, _resolve_git, commands_for_profile


class RestrictedExecutor:
    """Allowlist-driven, shell=False command executor."""

    def __init__(self, profile: ProjectProfile,
                 commands: List[CommandDefinition],
                 timeout_default: Optional[int] = None,
                 max_output_bytes: Optional[int] = None,
                 redaction_enabled: bool = True):
        self.profile = profile
        self.commands = commands
        self.timeout_default = int(timeout_default or profile.command_timeout_seconds)
        self.max_output_bytes = int(max_output_bytes or profile.max_output_bytes)
        self.redaction_enabled = bool(redaction_enabled)
        self.venv_python = _resolve_python(profile)
        self.git_bin = _resolve_git()
        self._approved = {self.venv_python, self.git_bin}
        self._signatures = self._build_signatures()

    def _build_signatures(self):
        approved = set(self._approved)
        sigs = []
        for d in self.commands:
            approved.add(d.executable)
            sigs.append((d.executable, list(d.args), list(d.extra_args_allowed)))
        return approved, sigs

    # ------------------------------------------------------------------ #
    # Validation
    # ------------------------------------------------------------------ #
    def validate_command(self, argv: List[str]):
        approved, sigs = self._signatures
        return validate_argv(argv, approved, sigs)

    # ------------------------------------------------------------------ #
    # Execution
    # ------------------------------------------------------------------ #
    def run(self, argv: List[str], timeout: Optional[int] = None,
            cwd_override: Optional[str] = None) -> CommandResult:
        allowed, reason = self.validate_command(argv)
        if not allowed:
            return CommandResult(argv=argv, returncode=126, stdout="", stderr="",
                                 ok=False, reason=reason)
        to = int(timeout if timeout is not None else self.timeout_default)
        env = build_task_env(self.profile.environment_allowlist)
        run_argv = list(argv)
        if Path(argv[0]).name == "git":
            # Override repository-local configuration that could execute an
            # fsmonitor or external diff helper during an otherwise read-only
            # validation command.
            run_argv = [argv[0], "-c", "core.fsmonitor=false",
                        "-c", "diff.external=", *argv[1:]]
            env["GIT_ATTR_NOSYSTEM"] = "1"
        effective_cwd = cwd_override or self.profile.repository_root
        try:
            proc = subprocess.run(
                run_argv, shell=False, cwd=effective_cwd,
                capture_output=True, text=True, timeout=to, env=env,
            )
        except subprocess.TimeoutExpired as exc:
            out = self._redact(self._cap(exc.stdout or ""))
            err = self._redact(self._cap(exc.stderr or ""))
            return CommandResult(argv=argv, returncode=124, stdout=out, stderr=err,
                                 ok=False, reason="timeout", timed_out=True)
        except FileNotFoundError as exc:
            return CommandResult(argv=argv, returncode=127, stdout="", stderr=str(exc),
                                 ok=False, reason="executable not found")
        out = self._redact(self._cap(proc.stdout or ""))
        err = self._redact(self._cap(proc.stderr or ""))
        return CommandResult(argv=argv, returncode=proc.returncode, stdout=out,
                             stderr=err, ok=(proc.returncode == 0))

    def run_profile(self, profile_name: str = "default") -> List[CommandResult]:
        selected = commands_for_profile(self.commands, profile_name)
        results = []
        for d in selected:
            argv = [d.executable] + list(d.args) + list(d.args_extra)
            results.append(self.run(argv, timeout=d.timeout))
        return results

    # ------------------------------------------------------------------ #
    # Output post-processing
    # ------------------------------------------------------------------ #
    def _cap(self, text: str) -> str:
        if len(text) <= self.max_output_bytes:
            return text
        keep = self.max_output_bytes // 2
        return (text[:keep] +
                f"\n...[truncated: {len(text)} bytes total]...\n" +
                text[-keep:])

    def _redact(self, text: str) -> str:
        return redact(text) if self.redaction_enabled else text
