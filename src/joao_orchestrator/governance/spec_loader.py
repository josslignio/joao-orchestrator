"""M0 — THE single loader authority for the V4 documentary constitution (C-1).

`SPEC_INDEX_V4.json` is the ONLY list of active documents. This module is the ONE place
that reads them: every path it returns is verified present in the index AND its sha256
verified to match the file currently on disk — a document outside the index, or one whose
content drifted since the index was generated, is REFUSED, never silently served (G1).

This is a READ gate, not yet a runtime enforcement gate: no mission-launch path calls this
today (that wiring is `RunRuntime`'s job under A0/M1-A, out of scope for M0 — see
SYSTEM_CONSTITUTION_V4.md §8). It exists so the constitution's C-1 has a real, testable
implementation from day one, and so A0 has something to wire into rather than starting cold.

Standard library only, except `yaml` (PyYAML — see requirements.txt) for the specs/*.yaml
parse. Deterministic: no clock, no randomness, in the verification path itself.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as exc:  # fail loud, never silently skip spec parsing (D-043: no invisible gaps)
    yaml = None
    _YAML_IMPORT_ERROR = exc
else:
    _YAML_IMPORT_ERROR = None


class SpecIntegrityError(RuntimeError):
    """Raised whenever a document is missing, unlisted, or tampered — never a silent skip."""


def _sha256_of(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1048576), b""):
            value.update(block)
    return value.hexdigest()


@dataclass
class LoadedSpecs:
    """The verified, parsed V4 authority set. Every field was read through the index."""

    index: dict[str, Any]
    constitution_text: str
    roadmap_text: str
    specs: dict[str, dict[str, Any]]        # spec name (e.g. "runtime_integrity") -> parsed YAML
    traceability: list[dict[str, Any]]      # parsed TRACEABILITY_V4.jsonl rows
    decisions: list[dict[str, Any]]         # parsed DECISION_LOG.jsonl rows
    verified_paths: list[str] = field(default_factory=list)

    def requirement(self, requirement_id: str) -> dict[str, Any] | None:
        """Look up a single requirement by its immutable id across every loaded spec."""
        for spec in self.specs.values():
            for req in spec.get("requirements", []):
                if req.get("id") == requirement_id:
                    return req
        return None

    def all_requirements(self) -> list[dict[str, Any]]:
        return [req for spec in self.specs.values() for req in spec.get("requirements", [])]


class _VerifiedIndex:
    """Internal: the index plus a helper that refuses anything not listed + hash-matched."""

    def __init__(self, repo_root: Path, index: dict[str, Any]):
        self.repo_root = repo_root
        self.index = index
        self._by_path = {doc["path"]: doc for doc in index.get("active_documents", [])}

    def is_active(self, rel_path: str) -> bool:
        return rel_path in self._by_path

    def read_active(self, rel_path: str) -> str:
        """Return the text of an active, hash-verified document — refuse anything else."""
        entry = self._by_path.get(rel_path)
        if entry is None:
            raise SpecIntegrityError(
                f"refused: {rel_path!r} is not listed in SPEC_INDEX_V4.json active_documents "
                f"— only indexed documents are an active authority (C-1)")
        path = self.repo_root / rel_path
        if not path.is_file():
            raise SpecIntegrityError(f"refused: indexed document missing on disk: {rel_path}")
        actual = _sha256_of(path)
        if actual != entry.get("sha256"):
            raise SpecIntegrityError(
                f"refused: {rel_path!r} sha256 mismatch — index has {entry.get('sha256', '')[:16]}…, "
                f"disk has {actual[:16]}… (content drifted since SPEC_INDEX_V4.json was generated; "
                f"re-run scripts/build_spec_index.py, or this is tampering)")
        return path.read_text(encoding="utf-8")


def _find_repo_root(start: Path) -> Path:
    for candidate in (start, *start.parents):
        if (candidate / "SPEC_INDEX_V4.json").is_file():
            return candidate
    return start


def load_active_specs(repo_root: Path | str | None = None) -> LoadedSpecs:
    """Load, hash-verify, and parse the full V4 authority set. Refuses anything not indexed.

    Raises SpecIntegrityError if SPEC_INDEX_V4.json is absent, malformed, references a
    missing file, or any file's content no longer matches its recorded sha256.
    """
    root = Path(repo_root).expanduser().resolve() if repo_root else _find_repo_root(Path.cwd())
    index_path = root / "SPEC_INDEX_V4.json"
    if not index_path.is_file():
        raise SpecIntegrityError(f"refused: no SPEC_INDEX_V4.json at {root} — no active authority")
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SpecIntegrityError(f"refused: SPEC_INDEX_V4.json is not valid JSON: {exc}") from exc
    if not isinstance(index.get("active_documents"), list) or not index["active_documents"]:
        raise SpecIntegrityError("refused: SPEC_INDEX_V4.json has no active_documents list")

    verified = _VerifiedIndex(root, index)

    constitution_path = next((d["path"] for d in index["active_documents"] if d["type"] == "constitution"), None)
    roadmap_path = next((d["path"] for d in index["active_documents"] if d["type"] == "roadmap"), None)
    if not constitution_path or not roadmap_path:
        raise SpecIntegrityError("refused: index is missing a constitution or roadmap entry")

    constitution_text = verified.read_active(constitution_path)
    roadmap_text = verified.read_active(roadmap_path)

    if yaml is None:
        raise SpecIntegrityError(f"refused: PyYAML is required to parse specs/*.yaml: {_YAML_IMPORT_ERROR}")

    specs: dict[str, dict[str, Any]] = {}
    for doc in index["active_documents"]:
        if doc["type"] != "spec":
            continue
        text = verified.read_active(doc["path"])
        parsed = yaml.safe_load(text)
        if not isinstance(parsed, dict) or "spec" not in parsed:
            raise SpecIntegrityError(f"refused: {doc['path']} does not parse to a valid spec document")
        specs[parsed["spec"]] = parsed

    traceability = []
    trace_path = next((d["path"] for d in index["active_documents"] if d["type"] == "traceability"), None)
    if trace_path:
        text = verified.read_active(trace_path)
        for line in text.splitlines():
            line = line.strip()
            if line:
                traceability.append(json.loads(line))

    decisions = []
    decisions_path = next((d["path"] for d in index["active_documents"] if d["type"] == "decision_log"), None)
    if decisions_path:
        text = verified.read_active(decisions_path)
        for line in text.splitlines():
            line = line.strip()
            if line:
                decisions.append(json.loads(line))

    return LoadedSpecs(
        index=index, constitution_text=constitution_text, roadmap_text=roadmap_text,
        specs=specs, traceability=traceability, decisions=decisions,
        verified_paths=sorted(verified._by_path),
    )
