"""Read-only chat/review adapters backed by the existing JOÃO ChatBrain CLIs.

These adapters intentionally support only non-writing roles.  They never call
SEC-BOOT's write boundary and never mutate a worktree.  They are suitable for
planner/reviewer/researcher/tester participation in SupervisorCore.

P1-1 (mechanical read-only): the adapter uses a controller-owned execution
root as ``cwd``, computes a full fingerprint before/after invocation, and
rejects the result if any mutation is detected.  Claude is forced into
``--permission-mode plan`` with Bash/Edit/Write/NotebookEdit blocked.  GLM
receives a deny-by-default ``OPENCODE_PERMISSION`` policy.

P1-2 (real GLM identity): the adapter no longer trusts ``self.glm_model``.
The authoritative model is extracted from the OpenCode event stream.  If no
authoritative model record is found, the response is rejected.
"""
from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path
from typing import Optional

from ..bubble.chat import ChatBrain, available_brains
from .base import ProviderAdapter, ProviderRequest, ProviderResponse
from .codex_review import classify_provider_failure


def _execution_fingerprint(root: Path) -> str:
    """Return a deterministic mutation fingerprint for an execution root."""
    root = Path(root)
    digest = hashlib.sha256()
    if (root / ".git").exists():
        import subprocess
        result = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain=v1", "--untracked-files=all"],
            capture_output=True, text=True, check=False,
        )
        return "git:" + result.stdout
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return "tree:" + digest.hexdigest()


class ChatCLIProvider(ProviderAdapter):
    supported_roles = ("planner", "reviewer", "researcher", "tester")
    network_required = True

    # Expected model prefixes for each backend family.  A backend-reported
    # model that does not match its family indicates a silent fallback or
    # misrouted response, which must be rejected to prevent identity confusion.
    _FAMILY_MODELS = {
        "claude": ("claude",),
        "glm": ("glm", "zai", "opencode"),
    }

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

        # P1-1: Use a controller-owned execution root as cwd.  If the request
        # provides a worktree_path, use it; otherwise create an isolated temp dir.
        if request.worktree_path:
            execution_root = Path(request.worktree_path).expanduser().resolve()
        else:
            execution_root = Path(tempfile.mkdtemp(prefix="joao-chat-readonly-"))
        execution_root.mkdir(parents=True, exist_ok=True)

        # Compute fingerprint BEFORE invocation to detect any mutation.
        before = _execution_fingerprint(execution_root)

        chunks: list[str] = []
        errors: list[str] = []
        model_seen = None  # fail-closed: must receive an explicit model event
        for event in self.brain.reply_stream(
            request.prompt, model=self.backend,
            execution_root=str(execution_root), read_only=True,
        ):
            kind = event.get("event")
            if kind == "delta":
                chunks.append(str(event.get("text", "")))
            elif kind == "error":
                errors.append(str(event.get("message", "provider error")))
            elif kind in {"model", "done"} and event.get("model"):
                model_seen = str(event["model"])

        # P1-1: Compute fingerprint AFTER invocation — reject on any mutation.
        after = _execution_fingerprint(execution_root)
        if before != after:
            return ProviderResponse(
                role=request.role,
                task_id=request.task_id,
                ok=False,
                content="".join(chunks),
                error="read-only enforcement violated: execution root was mutated",
                changed_paths=[after],
                provider_name=self.name,
            )

        # Fail-closed: if no model event was received, we cannot verify identity.
        # But surface provider errors first (they explain why no model arrived).
        if model_seen is None:
            if errors:
                return ProviderResponse(
                    role=request.role,
                    task_id=request.task_id,
                    ok=False,
                    content="".join(chunks),
                    error="; ".join(errors),
                    provider_name=self.name,
                )
            return ProviderResponse(
                role=request.role,
                task_id=request.task_id,
                ok=False,
                content="".join(chunks),
                error=f"identity verification failed: no model event received from backend={self.backend}",
                provider_name=self.name,
            )
        # P1-2: Validate that the backend-reported model belongs to the expected
        # family.  A mismatch indicates a silent fallback or misrouted response,
        # which would break identity separation in independent-review modes.
        expected_prefixes = self._FAMILY_MODELS.get(self.backend, ())
        model_lower = model_seen.lower()
        if expected_prefixes and not any(
            prefix in model_lower for prefix in expected_prefixes
        ):
            return ProviderResponse(
                role=request.role,
                task_id=request.task_id,
                ok=False,
                error=f"identity confusion: backend={self.backend} but model={model_seen!r} "
                      f"does not match expected family prefixes {expected_prefixes}",
                provider_name=self.name,
            )
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
