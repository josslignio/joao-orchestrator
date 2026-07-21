"""joao_worker_host.proxies — `BuilderAdapter`/`ReviewerAdapter` implementations
that forward to a standalone `joao-worker-host` process instead of dispatching
in-process. `RunRuntime`/`run_c8b_mission` use these EXACTLY like `GLMBuilder`/
`ClaudeCLIReviewer` — same abstract contract, same call sites, no duplicated
orchestration (Boss architecture decision, 2026-07-20: JOAO is the sole
controller; a worker/product is launched by the external `joao-worker-host`
sibling process, never nested inside another model session).

Identity (`provider`/`model`/`provider_family`) is a CONSTRUCTOR argument here,
fixed by the controller wiring these proxies up — never read from the socket
response. `client.send_request` additionally refuses (BLOCK) a response bound
to a different `request_id`/`run_id`/`mission_id` than what was sent.
"""
from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

from ..bubble.runtime import BuilderAdapter, ReviewerAdapter
from . import client as client_mod


class RemoteBuilderProxy(BuilderAdapter):
    def __init__(self, *, worker: str, provider: str, model: str, provider_family: str,
                requires_network_transport: bool = False, socket_path: Path | None = None,
                state_dir: Path | None = None, timeout: int = 1200):
        self.worker = worker
        self.provider = provider
        self.model = model
        self.provider_family = provider_family
        self.requires_network_transport = requires_network_transport
        self.socket_path = socket_path
        self.state_dir = state_dir
        self.timeout = timeout

    def build(self, mission: str, workspace: Path, run_dir: Path, allowed: list[str],
             correction: bool) -> dict[str, Any]:
        capabilities = getattr(self, "_capabilities", None) or {}
        # `run_dir.name` IS the real `run_id` RunRuntime minted (`runs/<run_id>/`)
        # — the one genuine per-mission identifier a BuilderAdapter.build()
        # call carries; reused as `mission_id` too (this proxy has no separate
        # project/mission identifier at this call site).
        run_id = Path(run_dir).name
        request = {
            "request_id": secrets.token_hex(16), "run_id": run_id, "mission_id": run_id,
            "worker": self.worker, "role": "builder", "model": self.model,
            "workspace": str(workspace), "run_dir": str(run_dir), "timeout": self.timeout,
            "mission": mission, "allowed_paths": list(allowed), "correction": bool(correction),
            "network_capability": bool(capabilities.get("network_capability", False)),
        }
        return client_mod.send_request(request, socket_path=self.socket_path, state_dir=self.state_dir,
                                       timeout=self.timeout + 30)


class RemoteReviewerProxy(ReviewerAdapter):
    def __init__(self, *, worker: str, provider: str, model: str, provider_family: str,
                socket_path: Path | None = None, state_dir: Path | None = None, timeout: int = 900):
        self.worker = worker
        self.provider = provider
        self.model = model
        self.provider_family = provider_family
        self.socket_path = socket_path
        self.state_dir = state_dir
        self.timeout = timeout

    def available(self) -> bool:
        health = client_mod.health_check(socket_path=self.socket_path)
        return bool(health.get("ok")) and bool(health.get("pong"))

    def review_stage(self, run: dict[str, Any], run_dir: Path, stage: str, active_rules: str = "") -> dict[str, Any]:
        candidate = run.get("candidate") if stage != "plan" else None
        # mission_id == run_id, ALWAYS (Boss directive, 2026-07-21): a prior
        # bug had the builder request use run_id while this reviewer request
        # used project_id for the same mission — two different values for
        # what must be one propagated identifier end-to-end.
        run_id = run.get("run_id", Path(run_dir).name)
        request = {
            "request_id": secrets.token_hex(16), "run_id": run_id,
            "mission_id": run_id,
            "worker": self.worker, "role": "reviewer", "model": self.model,
            "workspace": run.get("workspace", ""), "run_dir": str(run_dir), "timeout": self.timeout,
            "mission": run.get("mission", ""), "stage": stage, "active_rules": active_rules,
            "candidate_tree": (candidate or {}).get("candidate_tree"),
            "candidate_readonly_copy": (candidate or {}).get("readonly_copy"),
            "candidate_commit": (candidate or {}).get("candidate_commit"),
        }
        return client_mod.send_request(request, socket_path=self.socket_path, state_dir=self.state_dir,
                                       timeout=self.timeout + 30)

    def review(self, run: dict[str, Any], run_dir: Path) -> dict[str, Any]:
        return self.review_stage(run, run_dir, "final")


def remote_glm_builder(**kwargs) -> RemoteBuilderProxy:
    from ..bubble.runtime import GLMBuilder
    return RemoteBuilderProxy(worker="zai-coding-plan", provider=GLMBuilder.provider,
                             model=GLMBuilder.model, provider_family=GLMBuilder.provider_family,
                             requires_network_transport=True, **kwargs)


def remote_claude_builder(**kwargs) -> RemoteBuilderProxy:
    from ..bubble.runtime import ClaudeCodeBuilder
    return RemoteBuilderProxy(worker="claude-cli", provider=ClaudeCodeBuilder.provider,
                             model=ClaudeCodeBuilder.model, provider_family=ClaudeCodeBuilder.provider_family,
                             requires_network_transport=True, **kwargs)


def remote_glm_reviewer(**kwargs) -> RemoteReviewerProxy:
    from ..bubble.runtime import GLMReviewer
    return RemoteReviewerProxy(worker="zai-coding-plan", provider=GLMReviewer.provider,
                              model=GLMReviewer.model, provider_family=GLMReviewer.provider_family, **kwargs)


def remote_claude_reviewer(**kwargs) -> RemoteReviewerProxy:
    from ..bubble.runtime import ClaudeCLIReviewer
    return RemoteReviewerProxy(worker="claude-cli", provider=ClaudeCLIReviewer.provider,
                              model=ClaudeCLIReviewer.model, provider_family=ClaudeCLIReviewer.provider_family,
                              **kwargs)
