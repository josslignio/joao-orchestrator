"""ManualFileProvider — the offline human handoff adapter.

This preserves the V1.4.0 manual ZCode workflow. It does NOT invoke any CLI or
API. It writes a provider-neutral handoff file to the configured inbox and
returns a response instructing the user to apply changes manually.

This is the only "real" non-mock provider enabled in V1.4.1.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..domain.identifiers import validate_identifier
from .base import (ProviderAdapter, ProviderRequest, ProviderResponse,
                   require_capability)


class ManualFileProvider(ProviderAdapter):
    name = "manual_file"
    supported_roles = ("coder", "reviewer")
    network_required = False

    def __init__(self, inbox_dir: Path):
        self.inbox_dir = Path(inbox_dir)

    def is_enabled(self) -> bool:
        return True

    def invoke(self, request: ProviderRequest) -> ProviderResponse:
        """Write the prompt to the inbox; return instructions for manual application."""
        require_capability(request, "artifact.write")
        task_id = validate_identifier(request.task_id, "task_id")
        role = validate_identifier(request.role, "provider role")
        self.inbox_dir.mkdir(parents=True, exist_ok=True)
        out_path = self.inbox_dir / f"{task_id}_{role}_handoff.md"
        out_path.write_text(request.prompt, encoding="utf-8")
        return ProviderResponse(
            role=request.role,
            task_id=request.task_id,
            ok=True,
            content=(f"Handoff written to {out_path}. Apply the requested changes "
                     f"manually, then run validate."),
            changed_paths=[str(out_path)],
            provider_name=self.name,
        )
