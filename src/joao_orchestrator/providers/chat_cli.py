"""Read-only chat/review adapters backed by the existing JOÃO ChatBrain CLIs.

These adapters intentionally support only non-writing roles.  They never call
SEC-BOOT's write boundary and never mutate a worktree.  They are suitable for
planner/reviewer/researcher/tester participation in SupervisorCore.
"""
from __future__ import annotations

from typing import Optional

from ..bubble.chat import ChatBrain, available_brains
from .base import ProviderAdapter, ProviderRequest, ProviderResponse
from .codex_review import classify_provider_failure


class ChatCLIProvider(ProviderAdapter):
    supported_roles = ("planner", "reviewer", "researcher", "tester")
    network_required = True

    def __init__(self, backend: str, *, brain: Optional[ChatBrain] = None):
        backend = str(backend).strip().lower()
        if backend not in {"claude", "glm"}:
            raise ValueError("backend must be 'claude' or 'glm'")
        self.backend = backend
        self.name = f"{backend}-chat"
        self.brain = brain or ChatBrain()

    def is_enabled(self) -> bool:
        return bool(available_brains().get(self.backend, {}).get("available"))

    def invoke(self, request: ProviderRequest) -> ProviderResponse:
        if request.role not in self.supported_roles:
            return ProviderResponse(
                role=request.role,
                task_id=request.task_id,
                ok=False,
                error=f"unsupported role: {request.role}",
                provider_name=self.name,
            )
        chunks: list[str] = []
        errors: list[str] = []
        model_seen = self.backend
        for event in self.brain.reply_stream(request.prompt, model=self.backend):
            kind = event.get("event")
            if kind == "delta":
                chunks.append(str(event.get("text", "")))
            elif kind == "error":
                errors.append(str(event.get("message", "provider error")))
            elif kind in {"model", "done"} and event.get("model"):
                model_seen = str(event["model"])
        # Preserve exact provider output without stripping, so that byte-exact
        # marker checks downstream are not bypassed by transport-layer
        # whitespace normalization.  classify_provider_failure normalizes
        # internally, so it works on unstripped text.
        content = "".join(chunks)
        content_stripped = content.strip()
        semantic_error = classify_provider_failure(content_stripped) if content_stripped else ""
        if errors or not content_stripped or semantic_error:
            return ProviderResponse(
                role=request.role,
                task_id=request.task_id,
                ok=False,
                content=content,
                error="; ".join(errors) or semantic_error or "provider returned no text",
                provider_name=self.name,
            )
        return ProviderResponse(
            role=request.role,
            task_id=request.task_id,
            ok=True,
            content=content,
            provider_name=self.name,
        )
