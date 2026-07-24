"""Controller-owned supervisor evidence store."""
from __future__ import annotations

import json
from pathlib import Path

from ..storage.atomic import atomic_write_json, append_line
from ..observability.redaction import redact
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
        # Errors/reasons are HASHED (not just secret-pattern-redacted) because
        # a provider can echo raw prompts or other non-secret content into
        # error fields.  Hashing ensures no raw provider text crosses the
        # persistence boundary.
        data = result.to_dict(include_content=False)
        reason_text = str(data.get("reason", ""))
        data["reason"] = redact(reason_text)
        data["reason_sha256"] = hashlib.sha256(reason_text.encode("utf-8")).hexdigest()[:16]
        for call in data.get("calls", []):
            error_text = str(call.get("error", ""))
            call["error"] = redact(error_text)
            call["error_sha256"] = hashlib.sha256(error_text.encode("utf-8")).hexdigest()[:16]
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
