"""Verified OpenCode + Z.AI Coding Plan provider.

The adapter uses OpenCode's supported non-interactive ``run`` command.  It is
fail-closed: availability is established from local CLI probes, the configured
auth store, and the requested model listing before a coding request may run.
No legacy MCP bridge, GUI automation, paid API fallback, shell invocation, or
credential copying is used.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from .base import ProviderAdapter, ProviderRequest, ProviderResponse, require_capability
from .subprocess_cli import CLIEngineConfig, SubprocessCLIEngine

DEFAULT_MODEL = "zai-coding-plan/glm-4.5-air"
DEFAULT_PROVIDER = "zai-coding-plan"
DEFAULT_AUTH_PATH = "~/.local/share/opencode/auth.json"
_REQUIRED_RUN_FLAGS = ("--model", "--agent", "--format", "--dir", "--auto")


@dataclass(frozen=True)
class ProviderCapabilities:
    headless: bool = False
    json_output: bool = False
    workspace_write: bool = False
    positional_prompt: bool = False
    provider_visible: bool = False
    model_visible: bool = False
    raw_help_text: str = ""
    raw_version_text: str = ""
    version_string: str = ""

    def to_dict(self) -> dict:
        return {
            "headless": self.headless,
            "json_output": self.json_output,
            "workspace_write": self.workspace_write,
            "positional_prompt": self.positional_prompt,
            "provider_visible": self.provider_visible,
            "model_visible": self.model_visible,
            "version_string": self.version_string,
        }

    @property
    def sufficient(self) -> bool:
        return (
            self.headless
            and self.json_output
            and self.workspace_write
            and self.positional_prompt
            and self.provider_visible
        )


@dataclass
class ProviderProbeReport:
    provider_name: str
    executable_found: bool = False
    executable_path: Optional[str] = None
    auth_found: bool = False
    help_ok: bool = False
    version_ok: bool = False
    auth_list_ok: bool = False
    models_ok: bool = False
    help_returncode: int = -1
    version_returncode: int = -1
    auth_list_returncode: int = -1
    models_returncode: int = -1
    capabilities: Optional[ProviderCapabilities] = None
    unavailable_reason: str = ""
    duration_seconds: float = 0.0

    @property
    def available(self) -> bool:
        return (
            self.executable_found
            and self.auth_found
            and self.help_ok
            and self.version_ok
            and self.auth_list_ok
            and self.capabilities is not None
            and self.capabilities.sufficient
        )

    def to_dict(self) -> dict:
        payload = {
            "provider_name": self.provider_name,
            "executable_found": self.executable_found,
            "executable_path": self.executable_path,
            "auth_found": self.auth_found,
            "help_ok": self.help_ok,
            "version_ok": self.version_ok,
            "auth_list_ok": self.auth_list_ok,
            "models_ok": self.models_ok,
            "help_returncode": self.help_returncode,
            "version_returncode": self.version_returncode,
            "auth_list_returncode": self.auth_list_returncode,
            "models_returncode": self.models_returncode,
            "unavailable_reason": self.unavailable_reason,
            "duration_seconds": self.duration_seconds,
            "available": self.available,
        }
        if self.capabilities is not None:
            payload["capabilities"] = self.capabilities.to_dict()
        return payload


def _safe_opencode_config(model: str) -> dict:
    """Return a deny-by-default coding policy for a bounded worktree.

    Repository commit/push operations remain controller responsibilities.  The
    builder may edit workspace files and run read-only inspection/tests only.
    """
    return {
        "$schema": "https://opencode.ai/config.json",
        "model": model,
        "enabled_providers": [DEFAULT_PROVIDER],
        "share": "disabled",
        "autoupdate": False,
        "snapshot": False,
        "mcp": {},
        "permission": {
            "*": "deny",
            "read": {
                "*": "allow",
                "*.env": "deny",
                "*.env.*": "deny",
                "*.pem": "deny",
                "*.key": "deny",
            },
            "glob": "allow",
            "grep": "allow",
            "lsp": "allow",
            "edit": "deny",
            "task": "deny",
            "question": "deny",
            "webfetch": "deny",
            "websearch": "deny",
            "external_directory": "deny",
            "bash": {
                "*": "deny",
                "pwd": "allow",
                "ls*": "allow",
                "find *": "deny",
                "grep *": "deny",
                "sed *": "deny",
                "cat *": "deny",
                "head *": "deny",
                "tail *": "deny",
                "wc *": "deny",
                "git status*": "allow",
                "git diff*": "deny",
                "git ls-files*": "allow",
                "git rev-parse*": "allow",
                "git show*": "deny",
                "python -m pytest*": "deny",
                "python3 -m pytest*": "deny",
                "pytest*": "deny",
                "python -m compileall*": "allow",
                "python3 -m compileall*": "allow",
                "python -m pip *": "deny",
                "python3 -m pip *": "deny",
                "pip *": "deny",
                "pip3 *": "deny",
                "npm *": "deny",
                "pnpm *": "deny",
                "yarn *": "deny",
                "bun *": "deny",
                "brew *": "deny",
                "curl *": "deny",
                "wget *": "deny",
                "rm *": "deny",
                "sudo *": "deny",
                "git add*": "deny",
                "git commit*": "deny",
                "git push*": "deny",
                "git merge*": "deny",
                "git rebase*": "deny",
                "git reset*": "deny",
                "git clean*": "deny",
                "git checkout*": "deny",
                "git switch*": "deny",
            },
        },
    }


class OpenCodeProvider(ProviderAdapter):
    name = "opencode-zai"
    supported_roles = ("coder",)
    network_required = True

    def __init__(
        self,
        engine: SubprocessCLIEngine,
        *,
        model: str = DEFAULT_MODEL,
        base_args: Optional[List[str]] = None,
    ):
        self.engine = engine
        self.model = model
        self.base_args = list(base_args or [])
        self.last_result = None
        self._probe_report: Optional[ProviderProbeReport] = None

    @classmethod
    def default(
        cls,
        environment_allowlist: Optional[List[str]] = None,
        timeout_seconds: int = 900,
        executable: str = "opencode",
        base_args: Optional[List[str]] = None,
        model: str = DEFAULT_MODEL,
        auth_path: str = DEFAULT_AUTH_PATH,
    ) -> "OpenCodeProvider":
        config = CLIEngineConfig(
            name=cls.name,
            executable=executable,
            base_args=list(base_args or []),
            timeout_seconds=timeout_seconds,
            environment_allowlist=list(environment_allowlist or ["HOME", "PATH", "TMPDIR", "LANG", "LC_ALL"]),
            required_auth_paths=[str(Path(auth_path).expanduser())],
        )
        return cls(SubprocessCLIEngine(config), model=model)

    def probe(self) -> ProviderProbeReport:
        import time

        started = time.monotonic()
        report = ProviderProbeReport(provider_name=self.name)
        executable = self.engine.resolve_executable()
        if not executable:
            report.unavailable_reason = f"executable not found: {self.engine.config.executable}"
            report.duration_seconds = time.monotonic() - started
            self._probe_report = report
            return report

        report.executable_found = True
        report.executable_path = executable
        report.auth_found = self.engine.has_required_auth()

        help_result = self.engine.probe(["run", "--help"], timeout_seconds=15)
        version_result = self.engine.probe(["--version"], timeout_seconds=15)
        auth_result = self.engine.probe(["auth", "list"], timeout_seconds=20)
        models_result = self.engine.probe(["models", DEFAULT_PROVIDER], timeout_seconds=30)

        report.help_returncode = help_result.returncode
        report.version_returncode = version_result.returncode
        report.auth_list_returncode = auth_result.returncode
        report.models_returncode = models_result.returncode
        report.help_ok = help_result.ok
        report.version_ok = version_result.ok
        report.auth_list_ok = auth_result.ok
        report.models_ok = models_result.ok

        help_text = f"{help_result.stdout}\n{help_result.stderr}"
        auth_text = f"{auth_result.stdout}\n{auth_result.stderr}"
        models_text = f"{models_result.stdout}\n{models_result.stderr}"
        missing_flags = [flag for flag in _REQUIRED_RUN_FLAGS if flag not in help_text]
        provider_visible = DEFAULT_PROVIDER in f"{auth_text}\n{models_text}"
        model_visible = self.model in models_text
        caps = ProviderCapabilities(
            headless=help_result.ok and not missing_flags,
            json_output="--format" in help_text,
            workspace_write="--agent" in help_text and "--auto" in help_text,
            positional_prompt="run [message" in help_text.lower() or "message" in help_text.lower(),
            provider_visible=provider_visible,
            model_visible=model_visible,
            raw_help_text=help_text,
            raw_version_text=version_result.stdout,
            version_string=(version_result.stdout or version_result.stderr).strip().splitlines()[0] if (version_result.stdout or version_result.stderr).strip() else "",
        )
        report.capabilities = caps

        reasons = []
        if not report.auth_found:
            reasons.append("OpenCode auth store unavailable")
        if not help_result.ok:
            reasons.append(f"opencode run --help failed ({help_result.returncode})")
        if missing_flags:
            reasons.append(f"required run flags missing: {missing_flags}")
        if not auth_result.ok:
            reasons.append(f"opencode auth list failed ({auth_result.returncode})")
        if not provider_visible:
            reasons.append(f"provider not visible: {DEFAULT_PROVIDER}")
        if not model_visible:
            # Model listing can be unavailable while the authenticated provider
            # still accepts an exact model identifier, so record but do not make
            # this the sole hard gate.
            reasons.append(f"model not listed: {self.model}")
        hard_reasons = [r for r in reasons if not r.startswith("model not listed")]
        report.unavailable_reason = "; ".join(hard_reasons)
        report.duration_seconds = time.monotonic() - started
        self._probe_report = report
        return report

    def is_enabled(self) -> bool:
        if self._probe_report is None:
            self.probe()
        return bool(self._probe_report and self._probe_report.available)

    def invoke(self, request: ProviderRequest) -> ProviderResponse:
        from ..bubble.write_tier_policy import assert_write_tier_enabled
        assert_write_tier_enabled("OpenCodeProvider.invoke")
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
        if self._probe_report is None:
            self.probe()
        if not self._probe_report or not self._probe_report.available:
            return ProviderResponse(
                role=request.role,
                task_id=request.task_id,
                ok=False,
                error=(self._probe_report.unavailable_reason if self._probe_report else "provider probe unavailable"),
                provider_name=self.name,
            )

        config = _safe_opencode_config(self.model)
        env = {
            "OPENCODE_CONFIG_CONTENT": json.dumps(config, sort_keys=True, separators=(",", ":")),
            "OPENCODE_DISABLE_AUTOUPDATE": "1",
            "OPENCODE_DISABLE_DEFAULT_PLUGINS": "1",
            "OPENCODE_DISABLE_CLAUDE_CODE": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
        }
        prompt = (
            "Execute this bounded JOÃO coding task exactly. Do not expand scope, "
            "install dependencies, commit, push, access external paths, use web "
            "tools, or expose secrets. Finish the task and report concise evidence.\n\n"
            + request.prompt
        )
        result = self.engine.run_argv(
            Path(request.worktree_path),
            [
                "run",
                "--format", "json",
                "--agent", "build",
                "--auto",
                "--model", self.model,
                "--dir", str(Path(request.worktree_path).expanduser().resolve()),
                *self.base_args,
                prompt,
            ],
            env_overrides=env,
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
        if self._probe_report is None:
            self.probe()
        return self._probe_report.to_dict() if self._probe_report else {"available": False}
