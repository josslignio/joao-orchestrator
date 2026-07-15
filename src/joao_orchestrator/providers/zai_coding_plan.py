"""Native Z.AI Coding Plan provider — direct adapter.

Phase G. A direct provider adapter for Z.AI Coding Plan (the `zai` CLI).

Key properties:
- Direct: no wrappers, no OpenCode dependency, no intermediary process.
- No paid API fallback: if the `zai` CLI is unavailable, the provider is
  disabled. Never falls back to a paid OpenAI/Anthropic endpoint.
- Capability probing: resolves the executable, probes --help/--version via
  bounded local subprocess calls, parses supported capabilities from output.
- Offline-testable: the probe and invoke logic is structured so tests can
  inject a fake executable path and verify capability parsing.
- Safety: never copies or persists ZAI/ZHIPU/OPENAI/ANTHROPIC/CLAUDE/GitHub
  keys. Redacts token variables from probe/run environments.

The Z.AI Coding Plan CLI (`zai`) is the local coding-plan agent. This adapter
invokes it headless with structured prompts and captures file changes from the
worktree. It does NOT use the Z.AI OpenAI-compatible HTTP API.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

from .base import (
    ProviderAdapter,
    ProviderRequest,
    ProviderResponse,
    ProviderCapabilityError,
    require_capability,
)


# ---------------------------------------------------------------------------
# Capability patterns parsed from --help output
# ---------------------------------------------------------------------------

_HEADLESS_FLAGS = [
    re.compile(r"--headless\b"),
    re.compile(r"--non-interactive\b"),
    re.compile(r"--no-tty\b"),
    re.compile(r"--batch\b"),
    re.compile(r"--json\b"),
    re.compile(r"--print\b"),
    re.compile(r"--yes\b"),
]

_WORKSPACE_WRITE_PATTERNS = [
    re.compile(r"workspace.write\b"),
    re.compile(r"write\b.*workspace"),
    re.compile(r"file\s*edit"),
    re.compile(r"apply\s*patch"),
]

_REVIEW_PATTERNS = [
    re.compile(r"review\b"),
    re.compile(r"critique\b"),
    re.compile(r"audit\b"),
]

# Environment variables to strip from probe/run environments (never leak).
_TOKEN_ENV_PREFIXES = (
    "ZAI_", "ZHIPU_", "OPENAI_", "ANTHROPIC_", "CLAUDE_",
    "GITHUB_TOKEN", "GH_TOKEN", "GITLAB_TOKEN",
)


# ---------------------------------------------------------------------------
# Probe result
# ---------------------------------------------------------------------------

@dataclass
class ZAIProbeResult:
    """Result of probing the Z.AI CLI for capabilities."""
    available: bool = False
    executable: str = ""
    version: str = ""
    headless: bool = False
    workspace_write: bool = False
    review: bool = False
    help_text: str = ""
    error: str = ""
    schema_version: int = 1

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "available": self.available,
            "executable": self.executable,
            "version": self.version,
            "headless": self.headless,
            "workspace_write": self.workspace_write,
            "review": self.review,
            "help_text_snippet": self.help_text[:500],
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Z.AI Coding Plan provider adapter
# ---------------------------------------------------------------------------

class ZAICodingPlanProvider(ProviderAdapter):
    """Native Z.AI Coding Plan provider.

    A direct adapter that invokes the local `zai` CLI headless. Never falls
    back to a paid API. If the CLI is unavailable, the provider is disabled
    with a structured explanation.
    """

    name = "zai-coding-plan"
    supported_roles = ("coder", "planner", "reviewer")
    network_required = False  # Local CLI, not a network API.

    # Default executable name to resolve via PATH.
    DEFAULT_EXECUTABLE = "zai"

    def __init__(self, executable: Optional[str] = None,
                 probe_timeout: int = 10,
                 run_timeout: int = 300,
                 env: Optional[dict] = None):
        self._explicit_executable = executable
        self.probe_timeout = probe_timeout
        self.run_timeout = run_timeout
        self._env = env  # Optional injected environment (for tests).
        self._probe: Optional[ZAIProbeResult] = None

    # -- Environment scrubbing -------------------------------------------- #

    @staticmethod
    def _scrub_env(env: dict) -> dict:
        """Remove token-bearing variables from the environment."""
        out = {}
        for k, v in env.items():
            upper = k.upper()
            if any(upper.startswith(prefix) or upper == prefix.rstrip("_")
                   for prefix in _TOKEN_ENV_PREFIXES):
                continue
            out[k] = v
        return out

    # -- Executable resolution -------------------------------------------- #

    def _resolve_executable(self) -> Optional[str]:
        """Resolve the zai executable path."""
        exe = self._explicit_executable or self.DEFAULT_EXECUTABLE
        # If it's an absolute path and exists, use it directly.
        if os.path.isabs(exe) and os.path.isfile(exe):
            return exe
        # Otherwise resolve via PATH.
        found = shutil.which(exe)
        return found

    # -- Capability probing ----------------------------------------------- #

    def probe(self, force: bool = False) -> ZAIProbeResult:
        """Probe the Z.AI CLI for capabilities. Cached after first call."""
        if self._probe is not None and not force:
            return self._probe
        result = ZAIProbeResult()
        exe = self._resolve_executable()
        if not exe:
            result.error = (f"executable {self.DEFAULT_EXECUTABLE!r} not found "
                            "in PATH; Z.AI Coding Plan provider unavailable")
            self._probe = result
            return result
        result.executable = exe
        # Probe --version.
        version = self._run_probe(exe, ["--version"])
        if version is not None:
            result.version = version.strip()
        # Probe --help.
        help_text = self._run_probe(exe, ["--help"]) or ""
        result.help_text = help_text
        # Parse capabilities.
        result.headless = any(p.search(help_text) for p in _HEADLESS_FLAGS)
        result.workspace_write = any(
            p.search(help_text) for p in _WORKSPACE_WRITE_PATTERNS)
        result.review = any(p.search(help_text) for p in _REVIEW_PATTERNS)
        # Available if we got any help output.
        result.available = bool(help_text.strip())
        if not result.available:
            result.error = "zai CLI did not produce help output"
        self._probe = result
        return result

    def _run_probe(self, exe: str, args: List[str]) -> Optional[str]:
        """Run a probe command with bounded timeout. Returns stdout or None."""
        import subprocess
        env = self._scrub_env(self._env or os.environ.copy())
        try:
            proc = subprocess.run(
                [exe] + args,
                capture_output=True, text=True,
                timeout=self.probe_timeout, env=env,
                stdin=subprocess.DEVNULL,
            )
            if proc.returncode == 0:
                return proc.stdout
            # Some CLIs return non-zero for --help; accept stdout anyway.
            return proc.stdout or proc.stderr or None
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return None

    # -- ProviderAdapter interface ---------------------------------------- #

    def is_enabled(self) -> bool:
        """True if the Z.AI CLI is available and headless-capable."""
        probe = self.probe()
        return probe.available and probe.headless

    def describe(self) -> dict:
        """Structured description of the provider state."""
        probe = self.probe()
        return {
            "name": self.name,
            "enabled": self.is_enabled(),
            "network_required": self.network_required,
            "supported_roles": list(self.supported_roles),
            "probe": probe.to_dict(),
        }

    def invoke(self, request: ProviderRequest) -> ProviderResponse:
        """Invoke the Z.AI CLI to execute a coding/review request.

        The CLI is invoked headless with the prompt on stdin. File changes are
        captured from the worktree after execution (not from CLI output, which
        keeps the contract provider-neutral).
        """
        # Require workspace.write capability for coder role.
        if request.role == "coder":
            require_capability(request, "workspace.write")

        probe = self.probe()
        if not probe.available:
            return ProviderResponse(
                role=request.role, task_id=request.task_id,
                ok=False,
                error=f"Z.AI CLI unavailable: {probe.error}",
                provider_name=self.name,
            )
        if not probe.headless:
            return ProviderResponse(
                role=request.role, task_id=request.task_id,
                ok=False,
                error="Z.AI CLI does not support headless mode",
                provider_name=self.name,
            )

        # Build headless invocation.
        exe = probe.executable
        argv = self._build_argv(probe, request)
        env = self._scrub_env(self._env or os.environ.copy())

        import subprocess
        try:
            proc = subprocess.run(
                argv,
                input=request.prompt,
                capture_output=True, text=True,
                timeout=self.run_timeout, env=env,
                cwd=request.worktree_path or None,
            )
            ok = proc.returncode == 0
            return ProviderResponse(
                role=request.role, task_id=request.task_id,
                ok=ok,
                content=proc.stdout,
                error=proc.stderr if not ok else "",
                provider_name=self.name,
            )
        except subprocess.TimeoutExpired:
            return ProviderResponse(
                role=request.role, task_id=request.task_id,
                ok=False,
                error=f"Z.AI CLI timed out after {self.run_timeout}s",
                provider_name=self.name,
            )
        except (FileNotFoundError, OSError) as exc:
            return ProviderResponse(
                role=request.role, task_id=request.task_id,
                ok=False,
                error=f"Z.AI CLI execution error: {exc}",
                provider_name=self.name,
            )

    def _build_argv(self, probe: ZAIProbeResult,
                    request: ProviderRequest) -> List[str]:
        """Build the headless argv for the Z.AI CLI."""
        argv = [probe.executable]
        # Prefer --headless; fall back to --non-interactive / --no-tty.
        if "--headless" in probe.help_text:
            argv.append("--headless")
        elif "--non-interactive" in probe.help_text:
            argv.append("--non-interactive")
        elif "--no-tty" in probe.help_text:
            argv.append("--no-tty")
        # JSON output if supported.
        if "--json" in probe.help_text:
            argv.append("--json")
        # Role-specific flags.
        if request.role == "reviewer":
            argv.append("--review")
        # Worktree path.
        if request.worktree_path:
            argv.extend(["--worktree", request.worktree_path])
        # Prompt is passed on stdin (not as an arg, to avoid quoting issues).
        argv.append("--stdin")
        return argv


# ---------------------------------------------------------------------------
# Capability summary for routing
# ---------------------------------------------------------------------------

def zai_capability_summary(provider: ZAICodingPlanProvider) -> dict:
    """Return a routing-friendly capability summary."""
    probe = provider.probe()
    return {
        "provider": provider.name,
        "available": probe.available,
        "headless": probe.headless,
        "workspace_write": probe.workspace_write,
        "review": probe.review,
        "roles": list(provider.supported_roles),
        "paid_api_fallback": False,  # Never.
        "network_required": provider.network_required,
    }
