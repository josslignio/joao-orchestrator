"""Controller-owned supervisor evidence store."""
from __future__ import annotations

import json
from pathlib import Path

from ..storage.atomic import atomic_write_json, append_line
from .models import SupervisorResult


class SupervisorStore:
    def __init__(self, state_root: Path):
        self.root = Path(state_root).expanduser().resolve() / "supervisor"

    def persist(self, result: SupervisorResult) -> Path:
        import hashlib
        runs = self.root / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        path = runs / f"{result.run_id}.json"
        # Raw prompts and provider answer text are intentionally not persisted.
        # Errors/reasons are REPLACED with their SHA256 hash (not just
        # secret-pattern-redacted) because a provider can echo raw prompts or
        # other non-secret content into error fields.  The original text never
        # crosses the persistence boundary — only the hash is stored.
        data = result.to_dict(include_content=False)
        reason_text = str(data.get("reason", ""))
        data["reason"] = "HASH:" + hashlib.sha256(reason_text.encode("utf-8")).hexdigest()[:16]
        for call in data.get("calls", []):
            error_text = str(call.get("error", ""))
            call["error"] = "HASH:" + hashlib.sha256(error_text.encode("utf-8")).hexdigest()[:16]
        atomic_write_json(path, data)
        self.root.mkdir(parents=True, exist_ok=True)
        append_line(
            self.root / "events.jsonl",
            json.dumps({
                "schema_version": 1,
                "run_id": result.run_id,
                "task_id": result.task_id,
                "project_id": result.project_id,
                "mode": result.mode,
                "status": result.status,
                "selected_provider": result.selected_provider,
                "verdict": result.verdict,
                "started_at": result.started_at,
                "finished_at": result.finished_at,
                "prompt_sha256": result.prompt_sha256,
            }, sort_keys=True),
        )
        return path
