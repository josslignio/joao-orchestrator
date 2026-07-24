"""Evidence store that separates validated artifacts from raw transcripts."""
from __future__ import annotations
import json, re
from pathlib import Path
from typing import Any

from ..storage.atomic import append_line, atomic_write_json, atomic_write_text
from .artifacts import NightArtifact

FORBIDDEN_LEDGER_KEYS = {
    "prompt", "objective", "content", "response", "provider_response",
    "raw_output", "raw_reason", "transcript", "conversation", "message",
}
FORBIDDEN_NAMES = re.compile(
    r"(prompt|response|transcript|conversation|provider_output|block_reason)",
    re.IGNORECASE,
)


class EvidenceError(RuntimeError):
    pass


def assert_metadata_only(value: Any, path: str = "root") -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if str(key).lower() in FORBIDDEN_LEDGER_KEYS:
                raise EvidenceError(f"forbidden raw field at {path}.{key}")
            assert_metadata_only(nested, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            assert_metadata_only(nested, f"{path}[{index}]")


class EvidenceStore:
    def __init__(self, state_root: Path, run_id: str):
        self.root = Path(state_root).expanduser().resolve() / "run_night" / "runs" / run_id
        self.root.mkdir(parents=True, exist_ok=False)
        (self.root / "artifacts").mkdir()

    def write_manifest(self, value: dict[str, Any]) -> Path:
        assert_metadata_only(value)
        path = self.root / "run_manifest.json"
        atomic_write_json(path, value)
        return path

    def append_event(self, value: dict[str, Any]) -> None:
        assert_metadata_only(value)
        append_line(self.root / "task_ledger.jsonl", json.dumps(value, sort_keys=True))

    def write_budget(self, value: dict[str, Any]) -> Path:
        assert_metadata_only(value)
        path = self.root / "budget_ledger.json"
        atomic_write_json(path, value)
        return path

    def write_artifact(self, artifact: NightArtifact) -> Path:
        artifact.validate()
        path = self.root / "artifacts" / f"{artifact.task_id}.json"
        atomic_write_json(path, artifact.to_dict())
        return path

    def write_report(self, value: dict[str, Any]) -> Path:
        assert_metadata_only(value)
        path = self.root / "morning_report.json"
        atomic_write_json(path, value)
        return path

    def write_heartbeat(self, value: dict[str, Any]) -> Path:
        assert_metadata_only(value)
        path = self.root / "heartbeat.json"
        atomic_write_json(path, value)
        return path

    def write_stop_reason(self, code: str) -> Path:
        if not re.fullmatch(r"[A-Z0-9_-]{1,96}", code):
            raise EvidenceError("invalid stop code")
        path = self.root / "STOP_REASON.txt"
        atomic_write_text(path, code + "\n")
        return path

    def final_scan(self) -> None:
        findings: list[str] = []
        for path in self.root.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(self.root)
            if FORBIDDEN_NAMES.search(path.name):
                findings.append(f"forbidden filename: {rel}")
                continue
            if rel.parts and rel.parts[0] == "artifacts":
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                    artifact = NightArtifact(
                        artifact_id=value["artifact_id"], task_id=value["task_id"],
                        kind=value["kind"], title=value["title"], summary=value["summary"],
                        decisions=tuple(value["decisions"]), steps=tuple(value["steps"]),
                        tests=tuple(value["tests"]), risks=tuple(value["risks"]),
                        dependencies=tuple(value["dependencies"]),
                        open_questions=tuple(value["open_questions"]),
                        source_sha=value["source_sha"],
                        reviewer_verdict=value["reviewer_verdict"],
                        schema_version=int(value.get("schema_version", 1)),
                    )
                    artifact.validate()
                    if value.get("artifact_sha256") != artifact.artifact_sha256:
                        findings.append(f"artifact hash mismatch: {rel}")
                except Exception as exc:
                    findings.append(f"invalid artifact {rel}: {exc}")
            elif path.suffix == ".json":
                try:
                    assert_metadata_only(json.loads(path.read_text(encoding="utf-8")), str(rel))
                except Exception as exc:
                    findings.append(f"{rel}: {exc}")
            elif path.suffix == ".jsonl":
                try:
                    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                        if line:
                            assert_metadata_only(json.loads(line), f"{rel}:{number}")
                except Exception as exc:
                    findings.append(f"{rel}: {exc}")
        if findings:
            raise EvidenceError("; ".join(findings))
