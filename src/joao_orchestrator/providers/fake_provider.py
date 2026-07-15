"""FakeProvider — test-only provider that writes files in the worktree.

Deterministic and side-effectful: writes a file in the worktree directory to
simulate provider changes. Used by worktree validation loop tests.

Never call real APIs. Never install packages.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from .base import ProviderAdapter, ProviderRequest, ProviderResponse, require_capability
from ..policy.capabilities import CapabilitySet


class FakeProvider(ProviderAdapter):
    """Test-only provider that writes files into a worktree directory."""

    name = "fake"
    supported_roles = ("coder",)
    network_required = False

    def __init__(
        self,
        files_to_create: Optional[dict] = None,
        fail: bool = False,
        error_message: str = "",
    ):
        """Create a FakeProvider.

        Args:
            files_to_create: dict mapping relative paths to content to write.
            fail: If True, simulate a provider failure.
            error_message: Error message when fail=True.
        """
        self.files_to_create = dict(files_to_create or {})
        self.fail = fail
        self.error_message = error_message
        self.last_request: Optional[ProviderRequest] = None

    def is_enabled(self) -> bool:
        return True

    def invoke(self, request: ProviderRequest) -> ProviderResponse:
        require_capability(request, "workspace.write")
        self.last_request = request
        if request.role not in self.supported_roles:
            return ProviderResponse(
                role=request.role,
                task_id=request.task_id,
                ok=False,
                error=f"unsupported role: {request.role}",
                provider_name=self.name,
            )
        if self.fail:
            return ProviderResponse(
                role=request.role,
                task_id=request.task_id,
                ok=False,
                error=self.error_message or "fake provider failure",
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

        # Write files into the worktree.
        wt = Path(request.worktree_path)
        created = []
        for rel_path, content in self.files_to_create.items():
            target = wt / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            created.append(rel_path)

        return ProviderResponse(
            role=request.role,
            task_id=request.task_id,
            ok=True,
            content=f"fake provider created {len(created)} file(s)",
            changed_paths=created,
            provider_name=self.name,
        )
