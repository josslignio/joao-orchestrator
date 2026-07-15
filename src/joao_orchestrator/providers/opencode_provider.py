"""OpenCode verified local adapter (opencode-zai provider).

Implements a generic verified external-agent adapter pattern:
  1. Resolve executable via shutil.which or explicit absolute path.
  2. Probe --help and --version through bounded local subprocess calls.
  3. Parse only explicitly supported capabilities from help output.
  4. If OpenCode is absent or its non-interactive interface cannot be proven
     from local help output: provider remains unavailable; produce a structured
     reason; do not guess commands; do not install OpenCode.

Adapted from upstream patterns in anomalyco/opencode (MIT) and
SWE-agent/mini-swe-agent (MIT). See docs/THIRD_PARTY_NOTICES.md.

Safety invariants:
  - Never copies or persists Z.AI Coding Plan keys.
  - Never falls back to paid API.
  - Redacts ZAI/ZHIPU/OPENAI/ANTHROPIC/CLAUDE/GitHub token variables
    from probe and run environments.
  - Probe results are persisted as provider_probe.json.
  - Discovered capabilities are persisted as provider_capabilities.json.
  - Run results are persisted as provider_result.json.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

from .base import (
    ProviderAdapter,
    ProviderRequest,
    ProviderResponse,
    require_capability,
)
from .subprocess_cli import (
    CLIEngineConfig,
    CLIProbeResult,
    SubprocessCLIEngine,
    SubprocessCLIError,
)

# Patterns that indicate non-interactive / headless capability in --help output.
# These are parsed from the raw help text — no guessing, no defaults.
_HEADLESS_FLAGS = [
    re.compile(r"--headless\b"),
    re.compile(r"--non-interactive\b"),
    re.compile(r"--no-tty\b"),
    re.compile(r"--batch\b"),
    re.compile(r"--json\b"),
]

# Patterns that indicate the provider supports workspace-write operations.
_WORKSPACE_WRITE_PATTERNS = [
    re.compile(r"sandbox\b"),
    re.compile(r"workspace.write\b"),
    re.compile(r"write\b.*workspace"),
]

# Patterns indicating the provider can accept a prompt via stdin or --prompt.
_PROMPT_INPUT_PATTERNS = [
    re.compile(r"--prompt\b"),
    re.compile(r"stdin"),
    re.compile(r"<prompt>"),
]


@dataclass(frozen=True)
class ProviderCapabilities:
    """Immutable set of discovered capabilities from probe output."""
    headless: bool = False
    json_output: bool = False
    workspace_write: bool = False
    prompt_via_stdin: bool = False
    raw_help_text: str = ""
    raw_version_text: str = ""
    version_string: str = ""

    def to_dict(self) -> dict:
        return {
            "headless": self.headless,
            "json_output": self.json_output,
            "workspace_write": self.workspace_write,
            "prompt_via_stdin": self.prompt_via_stdin,
            "version_string": self.version_string,
        }

    @property
    def sufficient(self) -> bool:
        """True if the provider has enough capabilities for coding tasks."""
        return self.headless and self.prompt_via_stdin


@dataclass
class ProviderProbeReport:
    """Structured report from a provider probe attempt."""
    provider_name: str
    executable_found: bool = False
    executable_path: Optional[str] = None
    help_ok: bool = False
    version_ok: bool = False
    help_returncode: int = -1
    version_returncode: int = -1
    capabilities: Optional[ProviderCapabilities] = None
    unavailable_reason: str = ""
    duration_seconds: float = 0.0

    @property
    def available(self) -> bool:
        return (self.executable_found
                and self.help_ok
                and self.capabilities is not None
                and self.capabilities.sufficient)

    def to_dict(self) -> dict:
        d = {
            "provider_name": self.provider_name,
            "executable_found": self.executable_found,
            "executable_path": self.executable_path,
            "help_ok": self.help_ok,
            "version_ok": self.version_ok,
            "help_returncode": self.help_returncode,
            "version_returncode": self.version_returncode,
            "unavailable_reason": self.unavailable_reason,
            "duration_seconds": self.duration_seconds,
            "available": self.available,
        }
        if self.capabilities is not None:
            d["capabilities"] = self.capabilities.to_dict()
        return d


def _parse_capabilities(help_text: str, version_text: str) -> ProviderCapabilities:
    """Parse capabilities from probe output text.

    Conservative: only marks capabilities as True when explicitly found
    in the output. Never guesses.
    """
    combined = f"{help_text}\n{version_text}"
    headless = False
    json_output = False
    workspace_write = False
    prompt_stdin = False

    for pat in _HEADLESS_FLAGS:
        if pat.search(combined):
            headless = True
            break
    if re.compile(r"--json\b").search(combined):
        json_output = True
    for pat in _WORKSPACE_WRITE_PATTERNS:
        if pat.search(combined):
            workspace_write = True
            break
    for pat in _PROMPT_INPUT_PATTERNS:
        if pat.search(combined):
            prompt_stdin = True
            break

    version_string = version_text.strip().splitlines()[0] if version_text.strip() else ""

    return ProviderCapabilities(
        headless=headless,
        json_output=json_output,
        workspace_write=workspace_write,
        prompt_via_stdin=prompt_stdin,
        raw_help_text=help_text,
        raw_version_text=version_text,
        version_string=version_string,
    )


class OpenCodeProvider(ProviderAdapter):
    """Verified local adapter for OpenCode (opencode-zai).

    Probes OpenCode's --help and --version to discover capabilities before
    enabling. If the provider cannot be verified locally, it remains disabled
    with a structured reason.

    This provider never:
    - Calls a paid API
    - Copies or persists Z.AI Coding Plan keys
    - Installs OpenCode
    - Guesses commands not proven by probe output
    """

    name = "opencode-zai"
    supported_roles = ("coder",)
    network_required = False  # Uses local CLI only

    def __init__(self, engine: SubprocessCLIEngine,
                 base_args: Optional[List[str]] = None):
        self.engine = engine
        self.base_args = list(base_args or [])
        self.last_result = None
        self._probe_report: Optional[ProviderProbeReport] = None

    @classmethod
    def default(
        cls,
        environment_allowlist: Optional[List[str]] = None,
        timeout_seconds: int = 600,
        executable: str = "opencode",
        base_args: Optional[List[str]] = None,
    ) -> "OpenCodeProvider":
        """Create a provider with default configuration.

        Auth detection uses OpenCode's existing auth.json (same location
        as the upstream opencode project: ~/.opencode/auth.json).
        """
        opencode_home = Path(
            os.environ.get("OPENCODE_HOME", "~/.opencode")
        ).expanduser()
        config = CLIEngineConfig(
            name=cls.name,
            executable=executable,
            base_args=list(base_args or []),
            timeout_seconds=timeout_seconds,
            environment_allowlist=list(environment_allowlist or []),
            required_auth_paths=[
                str(opencode_home / "auth.json"),
            ],
        )
        return cls(SubprocessCLIEngine(config), base_args=base_args)

    def probe(self) -> ProviderProbeReport:
        """Probe the provider executable for capabilities.

        Runs --help and --version via bounded subprocess calls. Returns a
        structured report. Tokens are stripped from the probe environment.
        """
        import time as _time

        started = _time.monotonic()
        report = ProviderProbeReport(provider_name=self.name)

        # Check executable.
        exe_path = self.engine.resolve_executable()
        if not exe_path:
            report.unavailable_reason = (
                f"executable not found: {self.engine.config.executable}"
            )
            report.duration_seconds = _time.monotonic() - started
            self._probe_report = report
            return report

        report.executable_found = True
        report.executable_path = exe_path

        # Probe --help.
        help_result = self.engine.probe(["--help"], timeout_seconds=10)
        report.help_returncode = help_result.returncode
        report.help_ok = help_result.ok

        # Probe --version.
        version_result = self.engine.probe(["--version"], timeout_seconds=10)
        report.version_returncode = version_result.returncode
        report.version_ok = version_result.ok

        # Parse capabilities only if --help succeeded.
        if help_result.ok:
            caps = _parse_capabilities(
                help_result.stdout, version_result.stdout
            )
            report.capabilities = caps

            if not caps.sufficient:
                report.unavailable_reason = (
                    "probe succeeded but provider lacks sufficient "
                    "capabilities: need headless + prompt_via_stdin"
                )
        else:
            report.unavailable_reason = (
                f"--help probe failed (exit {help_result.returncode})"
            )

        report.duration_seconds = _time.monotonic() - started
        self._probe_report = report
        return report

    def is_enabled(self) -> bool:
        """Provider is enabled only when probe confirms sufficient capabilities.

        Performs the probe on first call, then caches the result.
        """
        if self._probe_report is None:
            self.probe()
        return self._probe_report.available

    def invoke(self, request: ProviderRequest) -> ProviderResponse:
        """Execute a coding request via the OpenCode CLI.

        Requires workspace.write capability grant and verified capabilities.
        """
        require_capability(request, "workspace.write")
        if request.role not in self.supported_roles:
            return ProviderResponse(
                role=request.role,
                task_id=request.task_id,
                ok=False,
                error=f"unsupported role: {request.role}",
                provider_name=self.name,
            )
        if not request.worktree_path:
            return ProviderResponse(
                role=request.role,
                task_id=request.task_id,
                ok=False,
                error="worktree_path is required",
                provider_name=self.name,
            )

        # Require verified probe before invocation.
        if self._probe_report is None:
            self.probe()
        if not self._probe_report.available:
            return ProviderResponse(
                role=request.role,
                task_id=request.task_id,
                ok=False,
                error=self._probe_report.unavailable_reason,
                provider_name=self.name,
            )

        result = self.engine.run(
            Path(request.worktree_path), request.prompt,
            extra_args=self.base_args,
        )
        self.last_result = result
        return ProviderResponse(
            role=request.role,
            task_id=request.task_id,
            ok=result.ok,
            content=result.stdout,
            error=result.stderr if not result.ok else "",
            provider_name=self.name,
        )

    def describe(self) -> dict:
        """Structured description of this provider's status."""
        if self._probe_report is None:
            self.probe()
        return self._probe_report.to_dict()
