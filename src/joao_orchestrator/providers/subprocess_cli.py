"""Reusable subprocess-backed CLI engine adapter.

This module is provider-neutral: it executes a configured local executable with
argv arrays, an explicit cwd, bounded timeout, sanitized environment, and
captured diagnostics. It never invokes a shell.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from ..domain.events import now_iso
from ..observability.redaction import redact
from ..policy.environment import build_task_env


class SubprocessCLIError(RuntimeError):
    """Raised when a CLI execution fails closed before producing a response."""


@dataclass
class CLIEngineConfig:
    name: str
    executable: str
    base_args: List[str] = field(default_factory=list)
    timeout_seconds: int = 600
    environment_allowlist: List[str] = field(default_factory=list)
    required_auth_paths: List[str] = field(default_factory=list)


@dataclass
class CLIExecutionResult:
    engine_name: str
    argv: List[str]
    cwd: str
    returncode: int
    stdout: str
    stderr: str
    started_at: str
    finished_at: str
    duration_seconds: float
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CLIProbeResult:
    """Result of a bounded probe call (--help, --version, etc.)."""
    engine_name: str
    argv: List[str]
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def to_dict(self) -> dict:
        return {
            "engine_name": self.engine_name,
            "argv": self.argv,
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_seconds": self.duration_seconds,
            "timed_out": self.timed_out,
            "ok": self.ok,
        }


# Tokens that must never leak to provider subprocesses.
_PROVIDER_TOKEN_PATTERNS = [
    re.compile(r"(?i)ZAI_"),
    re.compile(r"(?i)ZHIPU_"),
    re.compile(r"(?i)OPENAI_API_KEY"),
    re.compile(r"(?i)ANTHROPIC_API_KEY"),
    re.compile(r"(?i)CLAUDE_.*KEY"),
    re.compile(r"(?i)CLAUDE_.*TOKEN"),
    re.compile(r"(?i)GITHUB_TOKEN"),
    re.compile(r"(?i)GH_TOKEN"),
    re.compile(r"(?i)GH_PAT"),
]


def _strip_provider_tokens(env: Dict[str, str]) -> Dict[str, str]:
    """Remove Z.AI/Zhipu/OpenAI/Anthropic/GitHub token variables from env."""
    clean = {}
    keys_to_strip = set()
    for k in env:
        for pat in _PROVIDER_TOKEN_PATTERNS:
            if pat.match(k):
                keys_to_strip.add(k)
                break
    for k, v in env.items():
        if k in keys_to_strip:
            continue
        clean[k] = v
    return clean


class SubprocessCLIEngine:
    """Run a local CLI with strict subprocess defaults."""

    def __init__(self, config: CLIEngineConfig):
        if config.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.config = config

    def is_available(self) -> bool:
        return self.resolve_executable() is not None

    def resolve_executable(self) -> Optional[str]:
        exe = self.config.executable
        if os.sep in exe:
            path = Path(exe).expanduser()
            return str(path.resolve()) if path.is_file() and os.access(path, os.X_OK) else None
        return shutil.which(exe)

    def has_required_auth(self) -> bool:
        for raw in self.config.required_auth_paths:
            if Path(raw).expanduser().is_file():
                return True
        return not self.config.required_auth_paths

    def build_env(self) -> Dict[str, str]:
        env = build_task_env(self.config.environment_allowlist)
        env.pop("OPENAI_API_KEY", None)
        env.pop("CODEX_API_KEY", None)
        return env

    def probe(self, probe_args: List[str],
              timeout_seconds: Optional[int] = None) -> CLIProbeResult:
        """Run a bounded probe (e.g., --help, --version) without stdin,
        auth, or prompt.  Short timeout.  Tokens stripped from env.

        Raises SubprocessCLIError if the executable is not available.
        """
        executable = self.resolve_executable()
        if not executable:
            raise SubprocessCLIError(f"{self.config.name} executable unavailable")

        argv = [executable, *probe_args]
        effective_timeout = timeout_seconds or min(self.config.timeout_seconds, 15)
        start = time.monotonic()
        try:
            # Probe: no stdin, no prompt. Use a minimal env with tokens stripped.
            probe_env = _strip_provider_tokens(dict(os.environ))
            # Always set non-interactive Git behavior.
            probe_env["GIT_TERMINAL_PROMPT"] = "0"
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=effective_timeout,
                shell=False,
                env=probe_env,
            )
            duration = time.monotonic() - start
            return CLIProbeResult(
                engine_name=self.config.name,
                argv=argv,
                returncode=completed.returncode,
                stdout=redact(completed.stdout),
                stderr=redact(completed.stderr),
                duration_seconds=duration,
            )
        except subprocess.TimeoutExpired:
            duration = time.monotonic() - start
            return CLIProbeResult(
                engine_name=self.config.name,
                argv=argv,
                returncode=124,
                stdout="",
                stderr="timeout",
                duration_seconds=duration,
                timed_out=True,
            )

    def run_argv(
        self,
        cwd: Path,
        argv_tail: Iterable[str],
        *,
        stdin_text: Optional[str] = None,
        env_overrides: Optional[Dict[str, str]] = None,
        timeout_seconds: Optional[int] = None,
    ) -> CLIExecutionResult:
        """Run an explicitly constructed CLI argv without a shell.

        Some agents (notably ``opencode run``) accept the task as a positional
        argument rather than stdin.  This method keeps that invocation explicit
        and auditable while preserving the same bounded execution and redaction
        guarantees as :meth:`run`.
        """
        executable = self.resolve_executable()
        if not executable:
            raise SubprocessCLIError(f"{self.config.name} executable unavailable")
        if not self.has_required_auth():
            raise SubprocessCLIError(f"{self.config.name} authentication unavailable")

        resolved_cwd = Path(cwd).expanduser().resolve()
        if not resolved_cwd.is_dir():
            raise SubprocessCLIError(f"cwd is not a directory: {resolved_cwd}")

        argv = [executable, *self.config.base_args, *list(argv_tail)]
        env = self.build_env()
        if env_overrides:
            env.update({str(k): str(v) for k, v in env_overrides.items()})
        started_at = now_iso()
        start = time.monotonic()
        effective_timeout = timeout_seconds or self.config.timeout_seconds
        try:
            completed = subprocess.run(
                argv,
                input=stdin_text,
                capture_output=True,
                text=True,
                cwd=str(resolved_cwd),
                env=env,
                timeout=effective_timeout,
                shell=False,
            )
            duration = time.monotonic() - start
            return CLIExecutionResult(
                engine_name=self.config.name,
                argv=argv,
                cwd=str(resolved_cwd),
                returncode=completed.returncode,
                stdout=redact(completed.stdout),
                stderr=redact(completed.stderr),
                started_at=started_at,
                finished_at=now_iso(),
                duration_seconds=duration,
            )
        except subprocess.TimeoutExpired as exc:
            duration = time.monotonic() - start
            return CLIExecutionResult(
                engine_name=self.config.name,
                argv=argv,
                cwd=str(resolved_cwd),
                returncode=124,
                stdout=redact(exc.stdout or ""),
                stderr=redact(exc.stderr or "timeout"),
                started_at=started_at,
                finished_at=now_iso(),
                duration_seconds=duration,
                timed_out=True,
            )

    def run(self, cwd: Path, prompt: str,
            extra_args: Optional[Iterable[str]] = None) -> CLIExecutionResult:
        return self.run_argv(
            cwd,
            list(extra_args or []),
            stdin_text=prompt,
        )
