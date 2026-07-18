"""M0 — G1: SPEC_INDEX_V4.json valid, sha256 verified, load_active_specs() refuses unlisted docs.

Hermetic: builds a synthetic repo root under tmp_path (real files, real sha256, real YAML) so
this test never depends on the actual committed V4 authorities existing yet.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from joao_orchestrator.governance.spec_loader import SpecIntegrityError, load_active_specs


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _build_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "specs").mkdir(parents=True)
    constitution = "# SYSTEM CONSTITUTION V4\nC-1 Une seule autorité.\n"
    roadmap = "# ROADMAP V4\nM0 -> M1-A\n"
    spec_yaml = (
        "spec: runtime_integrity\n"
        "version: \"4.0.0\"\n"
        "requirements:\n"
        "  - id: RI-1\n"
        "    statement: baseline propre\n"
        "    severity: 3\n"
        "    verification_gate: attack test 1\n"
        "    roadmap_milestone: M1-A\n"
        "    owner: contrôleur\n"
        "    status: PLANNED\n"
        "    source_defects: []\n"
        "    derivation: verbatim\n"
    )
    traceability = json.dumps({"defect": "D-001", "requirement": "RI-1", "gate": "attack test 1", "milestone": "M1-A"}) + "\n"
    decisions = json.dumps({"decision": "test decision", "date": "2026-07-18"}) + "\n"

    (root / "CONSTITUTION.md").write_text(constitution)
    (root / "ROADMAP.md").write_text(roadmap)
    (root / "specs" / "runtime_integrity.yaml").write_text(spec_yaml)
    (root / "TRACEABILITY.jsonl").write_text(traceability)
    (root / "DECISIONS.jsonl").write_text(decisions)

    index = {
        "schema_version": 1, "version": "4.0.0", "generated_at": "2026-07-18T00:00:00Z",
        "runtime_authority": False,
        "active_documents": [
            {"path": "CONSTITUTION.md", "type": "constitution", "version": "4.0.0", "sha256": _sha256(constitution)},
            {"path": "ROADMAP.md", "type": "roadmap", "version": "4.0.0", "sha256": _sha256(roadmap)},
            {"path": "specs/runtime_integrity.yaml", "type": "spec", "version": "4.0.0", "sha256": _sha256(spec_yaml)},
            {"path": "TRACEABILITY.jsonl", "type": "traceability", "version": "4.0.0", "sha256": _sha256(traceability)},
            {"path": "DECISIONS.jsonl", "type": "decision_log", "version": "4.0.0", "sha256": _sha256(decisions)},
        ],
    }
    (root / "SPEC_INDEX_V4.json").write_text(json.dumps(index, indent=2))
    return root


def test_happy_path_loads_and_parses_everything(tmp_path):
    root = _build_repo(tmp_path)
    loaded = load_active_specs(root)
    assert "C-1" in loaded.constitution_text
    assert "M1-A" in loaded.roadmap_text
    assert "runtime_integrity" in loaded.specs
    assert loaded.requirement("RI-1")["severity"] == 3
    assert loaded.traceability[0]["defect"] == "D-001"
    assert loaded.decisions[0]["decision"] == "test decision"
    assert set(loaded.verified_paths) == {"CONSTITUTION.md", "ROADMAP.md", "specs/runtime_integrity.yaml",
                                          "TRACEABILITY.jsonl", "DECISIONS.jsonl"}


def test_missing_index_refused(tmp_path):
    root = tmp_path / "no_index"
    root.mkdir()
    with pytest.raises(SpecIntegrityError, match="no SPEC_INDEX_V4.json"):
        load_active_specs(root)


def test_tampered_file_after_indexing_is_refused(tmp_path):
    root = _build_repo(tmp_path)
    # simulate drift: the constitution changed AFTER the index was generated (or tampering)
    (root / "CONSTITUTION.md").write_text("# SYSTEM CONSTITUTION V4\nC-1 rewritten maliciously.\n")
    with pytest.raises(SpecIntegrityError, match="sha256 mismatch"):
        load_active_specs(root)


def test_doc_outside_index_is_refused_by_read_active(tmp_path):
    root = _build_repo(tmp_path)
    # a rogue markdown NOT listed in the index must never be treated as active
    (root / "ROGUE_UNLISTED_SPEC.md").write_text("# not an authority\n")
    from joao_orchestrator.governance.spec_loader import _VerifiedIndex
    index = json.loads((root / "SPEC_INDEX_V4.json").read_text())
    verified = _VerifiedIndex(root, index)
    assert verified.is_active("CONSTITUTION.md") is True
    assert verified.is_active("ROGUE_UNLISTED_SPEC.md") is False
    with pytest.raises(SpecIntegrityError, match="not listed in SPEC_INDEX_V4.json"):
        verified.read_active("ROGUE_UNLISTED_SPEC.md")


def test_missing_indexed_file_on_disk_is_refused(tmp_path):
    root = _build_repo(tmp_path)
    (root / "ROADMAP.md").unlink()
    with pytest.raises(SpecIntegrityError, match="missing on disk"):
        load_active_specs(root)


def test_index_missing_active_documents_list_refused(tmp_path):
    root = tmp_path / "bad_index"
    root.mkdir()
    (root / "SPEC_INDEX_V4.json").write_text(json.dumps({"schema_version": 1}))
    with pytest.raises(SpecIntegrityError, match="no active_documents"):
        load_active_specs(root)
