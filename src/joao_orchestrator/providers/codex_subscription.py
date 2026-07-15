"""Codex subscription CLI adapter.

Uses the locally installed ``codex`` executable and the user's saved ChatGPT
authentication. API-key environment variables are stripped before execution.
"""

from __future__ import annotations

import os
from pathlib import Path

from .base import ProviderAdapter, ProviderRequest, ProviderResponse, require_capability
from .subprocess_cli import CLIEngineConfig, SubprocessCLIEngine


class CodexSubscriptionProvider(ProviderAdapter):
    name = "codex-subscription"
    supported_roles = ("coder",)
    network_required = True

    def __init__(self, engine: SubprocessCLIEngine):
        self.engine = engine
        self.last_result = None

    @classmethod
    def default(cls, environment_allowlist, timeout_seconds: int = 600,
                executable: str = "codex") -> "CodexSubscriptionProvider":
        codex_home = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
        config = CLIEngineConfig(
            name=cls.name,
            executable=executable,
            base_args=[
                "exec",
                "--json",
                "--sandbox",
                "workspace-write",
            ],
            timeout_seconds=timeout_seconds,
            environment_allowlist=[*list(environment_allowlist), "CODEX_HOME"],
            required_auth_paths=[
                str(codex_home / "auth.json"),
                str(codex_home / "auth.toml"),
            ],
        )
        return cls(SubprocessCLIEngine(config))

    def is_enabled(self) -> bool:
        return self.engine.is_available() and self.engine.has_required_auth()

    def invoke(self, request: ProviderRequest) -> ProviderResponse:
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
        result = self.engine.run(Path(request.worktree_path), request.prompt)
        self.last_result = result
        return ProviderResponse(
            role=request.role,
            task_id=request.task_id,
            ok=result.ok,
            content=result.stdout,
            error=result.stderr if not result.ok else "",
            provider_name=self.name,
        )
