"""MockProvider — deterministic, offline, for tests.

Returns a canned response. Honors capability grants (denies if the request
needs a capability not granted). No network, no filesystem writes.
"""

from __future__ import annotations

from typing import Optional

from .base import ProviderAdapter, ProviderRequest, ProviderResponse


class MockProvider(ProviderAdapter):
    name = "mock"
    supported_roles = ("planner", "coder", "reviewer", "researcher",
                       "tester", "skill_worker")
    network_required = False

    def __init__(self, response_content: str = "mock response",
                 response_ok: bool = True,
                 changed_paths: Optional[list] = None):
        self.response_content = response_content
        self.response_ok = response_ok
        self.changed_paths = list(changed_paths or [])

    def is_enabled(self) -> bool:
        return True

    def invoke(self, request: ProviderRequest) -> ProviderResponse:
        return ProviderResponse(
            role=request.role,
            task_id=request.task_id,
            ok=self.response_ok,
            content=self.response_content,
            changed_paths=list(self.changed_paths),
            provider_name=self.name,
        )
