"""Strict validated deliverables for Run Night.

Raw provider transcripts are never persisted.  A provider response must be one
exact JSON object matching this schema; only the validated artifact is stored.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from ..evaluation.models import sha256_json, validate_safe_id
from .models import ArtifactKind

ALLOWED_KEYS = {
    "schema_version", "artifact_id", "task_id", "kind", "title", "summary",
    "decisions", "steps", "tests", "risks", "dependencies", "open_questions",
    "source_sha", "reviewer_verdict",
}
SECRET_PATTERNS = (
    re.compile(r"(?i)\b(api[_-]?key|secret|password|private[_-]?key|access[_-]?token)\b\s*[:=]\s*\S+"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)


class ArtifactError(ValueError):
    pass


def _clean_list(value: Any, name: str, *, max_items: int = 40, max_chars: int = 2000) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ArtifactError(f"{name} must be a list")
    if len(value) > max_items:
        raise ArtifactError(f"{name} exceeds max items")
    result = []
    for item in value:
        text = str(item)
        if not text.strip() or len(text) > max_chars:
            raise ArtifactError(f"{name} contains invalid text")
        result.append(text)
    return tuple(result)


def _reject_secrets(value: str) -> None:
    for pattern in SECRET_PATTERNS:
        if pattern.search(value):
            raise ArtifactError("artifact contains secret-like material")


@dataclass(frozen=True)
class NightArtifact:
    artifact_id: str
    task_id: str
    kind: str
    title: str
    summary: str
    decisions: tuple[str, ...]
    steps: tuple[str, ...]
    tests: tuple[str, ...]
    risks: tuple[str, ...]
    dependencies: tuple[str, ...]
    open_questions: tuple[str, ...]
    source_sha: str
    reviewer_verdict: str
    schema_version: int = 1

    def validate(self) -> None:
        validate_safe_id(self.artifact_id, "artifact_id")
        validate_safe_id(self.task_id, "task_id")
        ArtifactKind(self.kind)
        if not self.title.strip() or len(self.title) > 200:
            raise ArtifactError("invalid title")
        if not self.summary.strip() or len(self.summary) > 4000:
            raise ArtifactError("invalid summary")
        if len(self.source_sha) != 40 or any(c not in "0123456789abcdef" for c in self.source_sha):
            raise ArtifactError("invalid source_sha")
        if self.reviewer_verdict != "ACCEPT":
            raise ArtifactError("reviewer_verdict must be ACCEPT")
        rendered = json.dumps(self.unsigned_dict(), sort_keys=True, ensure_ascii=False)
        _reject_secrets(rendered)

    def unsigned_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "artifact_id": self.artifact_id,
            "task_id": self.task_id,
            "kind": self.kind,
            "title": self.title,
            "summary": self.summary,
            "decisions": list(self.decisions),
            "steps": list(self.steps),
            "tests": list(self.tests),
            "risks": list(self.risks),
            "dependencies": list(self.dependencies),
            "open_questions": list(self.open_questions),
            "source_sha": self.source_sha,
            "reviewer_verdict": self.reviewer_verdict,
        }

    @property
    def artifact_sha256(self) -> str:
        return sha256_json(self.unsigned_dict())

    def to_dict(self) -> dict[str, Any]:
        return {**self.unsigned_dict(), "artifact_sha256": self.artifact_sha256}


def parse_artifact_exact(
    raw: str, *, expected_task_id: str, expected_kind: str, expected_sha: str
) -> NightArtifact:
    if not isinstance(raw, str) or not raw or raw[0] != "{" or raw[-1] != "}":
        raise ArtifactError("provider output must be exactly one JSON object")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ArtifactError(f"invalid artifact JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ArtifactError("artifact must be a JSON object")
    extra = sorted(set(data) - ALLOWED_KEYS)
    if extra:
        raise ArtifactError(f"unexpected artifact keys: {extra}")
    artifact = NightArtifact(
        artifact_id=str(data.get("artifact_id", "")),
        task_id=str(data.get("task_id", "")),
        kind=str(data.get("kind", "")),
        title=str(data.get("title", "")),
        summary=str(data.get("summary", "")),
        decisions=_clean_list(data.get("decisions", []), "decisions"),
        steps=_clean_list(data.get("steps", []), "steps"),
        tests=_clean_list(data.get("tests", []), "tests"),
        risks=_clean_list(data.get("risks", []), "risks"),
        dependencies=_clean_list(data.get("dependencies", []), "dependencies"),
        open_questions=_clean_list(data.get("open_questions", []), "open_questions"),
        source_sha=str(data.get("source_sha", "")),
        reviewer_verdict=str(data.get("reviewer_verdict", "")).upper(),
        schema_version=int(data.get("schema_version", 1)),
    )
    artifact.validate()
    if artifact.task_id != expected_task_id:
        raise ArtifactError("artifact task_id mismatch")
    if artifact.kind != expected_kind:
        raise ArtifactError("artifact kind mismatch")
    if artifact.source_sha != expected_sha:
        raise ArtifactError("artifact source SHA mismatch")
    return artifact
