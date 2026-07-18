"""M0.1 — G1: SPEC_BUNDLE_MANIFEST_V4.json valid, sha256 verified, load_active_specs() refuses
unlisted docs. Supersedes the M0 SPEC_INDEX_V4.json test (deliverable 1: bundle/activation
split ends the activation circularity) and adds deliverable 4's external-reference attack
tests (valid ref accepted / missing file refused / wrong hash refused / path traversal or
symlink escape refused).

Hermetic: builds a synthetic repo root under tmp_path (real files, real sha256, real YAML) so
this test never depends on the actual committed V4 authorities existing yet.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from joao_orchestrator.governance.spec_loader import SpecIntegrityError, load_active_specs


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _build_repo(tmp_path: Path, *, external_ref: dict | None = None) -> Path:
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
    (root / "DECISION_LOG.jsonl").write_text(decisions)  # living log — read by fixed name, not indexed

    bundle = {
        "schema_version": 1, "bundle_version": "4.0.0", "generated_at": "2026-07-18T00:00:00Z",
        "immutable": True,
        "bundle_documents": [
            {"path": "CONSTITUTION.md", "type": "constitution", "version": "4.0.0", "sha256": _sha256(constitution)},
            {"path": "ROADMAP.md", "type": "roadmap", "version": "4.0.0", "sha256": _sha256(roadmap)},
            {"path": "specs/runtime_integrity.yaml", "type": "spec", "version": "4.0.0", "sha256": _sha256(spec_yaml)},
            {"path": "TRACEABILITY.jsonl", "type": "traceability", "version": "4.0.0", "sha256": _sha256(traceability)},
        ],
        "external_spec_references": [external_ref] if external_ref else [],
    }
    (root / "SPEC_BUNDLE_MANIFEST_V4.json").write_text(json.dumps(bundle, indent=2))
    return root


def _make_external_repo(tmp_path: Path, name: str = "external_product") -> tuple[Path, str]:
    ext_root = tmp_path / name
    (ext_root / "governance").mkdir(parents=True)
    text = "spec: product_x\nversion: \"4.0.0\"\nrequirements:\n  - id: PX-1\n    statement: x\n"
    (ext_root / "governance" / "PROJECT_SPEC_V4.yaml").write_text(text)
    return ext_root, _sha256(text)


# ---------------------------------------------------------------------------
# Baseline: bundle loads, decision log read as a living (unverified) log
# ---------------------------------------------------------------------------

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
                                          "TRACEABILITY.jsonl"}


def test_decision_log_is_not_hash_verified_and_can_change_freely(tmp_path):
    """The whole point of the bundle/activation split: appending to DECISION_LOG.jsonl never
    invalidates the bundle — it isn't in bundle_documents and carries no recorded hash."""
    root = _build_repo(tmp_path)
    loaded_before = load_active_specs(root)
    (root / "DECISION_LOG.jsonl").write_text(
        (root / "DECISION_LOG.jsonl").read_text() + json.dumps({"decision": "a new one", "date": "2026-07-19"}) + "\n")
    loaded_after = load_active_specs(root)  # must NOT raise
    assert len(loaded_after.decisions) == len(loaded_before.decisions) + 1


def test_missing_bundle_refused(tmp_path):
    root = tmp_path / "no_bundle"
    root.mkdir()
    with pytest.raises(SpecIntegrityError, match="no SPEC_BUNDLE_MANIFEST_V4.json"):
        load_active_specs(root)


def test_tampered_file_after_bundling_is_refused(tmp_path):
    root = _build_repo(tmp_path)
    # simulate drift: the constitution changed AFTER the bundle was generated (or tampering)
    (root / "CONSTITUTION.md").write_text("# SYSTEM CONSTITUTION V4\nC-1 rewritten maliciously.\n")
    with pytest.raises(SpecIntegrityError, match="sha256 mismatch"):
        load_active_specs(root)


def test_doc_outside_bundle_is_refused_by_read_active(tmp_path):
    root = _build_repo(tmp_path)
    (root / "ROGUE_UNLISTED_SPEC.md").write_text("# not an authority\n")
    from joao_orchestrator.governance.spec_loader import _VerifiedBundle
    bundle = json.loads((root / "SPEC_BUNDLE_MANIFEST_V4.json").read_text())
    verified = _VerifiedBundle(root, bundle)
    assert verified.is_active("CONSTITUTION.md") is True
    assert verified.is_active("ROGUE_UNLISTED_SPEC.md") is False
    with pytest.raises(SpecIntegrityError, match="not listed in SPEC_BUNDLE_MANIFEST_V4.json"):
        verified.read_active("ROGUE_UNLISTED_SPEC.md")


def test_missing_bundled_file_on_disk_is_refused(tmp_path):
    root = _build_repo(tmp_path)
    (root / "ROADMAP.md").unlink()
    with pytest.raises(SpecIntegrityError, match="missing on disk"):
        load_active_specs(root)


def test_bundle_missing_bundle_documents_list_refused(tmp_path):
    root = tmp_path / "bad_bundle"
    root.mkdir()
    (root / "SPEC_BUNDLE_MANIFEST_V4.json").write_text(json.dumps({"schema_version": 1}))
    with pytest.raises(SpecIntegrityError, match="no bundle_documents"):
        load_active_specs(root)


# ---------------------------------------------------------------------------
# Deliverable 4: typed external spec reference — the 4 required attack tests
# ---------------------------------------------------------------------------

def test_external_reference_valid_is_accepted(tmp_path):
    ext_root, sha = _make_external_repo(tmp_path)
    ref = {"project_id": "product-x", "repository": str(ext_root), "spec_path": "governance/PROJECT_SPEC_V4.yaml",
           "version": "4.0.0", "sha256": sha}
    root = _build_repo(tmp_path, external_ref=ref)
    loaded = load_active_specs(root)
    assert "product-x" in loaded.external_specs
    assert loaded.requirement("PX-1")["statement"] == "x"


def test_external_reference_missing_file_refused(tmp_path):
    ext_root, sha = _make_external_repo(tmp_path)
    (ext_root / "governance" / "PROJECT_SPEC_V4.yaml").unlink()
    ref = {"project_id": "product-x", "repository": str(ext_root), "spec_path": "governance/PROJECT_SPEC_V4.yaml",
           "version": "4.0.0", "sha256": sha}
    root = _build_repo(tmp_path, external_ref=ref)
    with pytest.raises(SpecIntegrityError, match="missing on disk"):
        load_active_specs(root)


def test_external_reference_wrong_hash_refused(tmp_path):
    ext_root, sha = _make_external_repo(tmp_path)
    ref = {"project_id": "product-x", "repository": str(ext_root), "spec_path": "governance/PROJECT_SPEC_V4.yaml",
           "version": "4.0.0", "sha256": "0" * 64}
    root = _build_repo(tmp_path, external_ref=ref)
    with pytest.raises(SpecIntegrityError, match="sha256 mismatch"):
        load_active_specs(root)


def test_external_reference_path_traversal_refused(tmp_path):
    ext_root, _sha = _make_external_repo(tmp_path)
    secret = tmp_path / "secret_outside_repo.yaml"
    secret.write_text("spec: leaked\nversion: \"4.0.0\"\nrequirements: []\n")
    ref = {"project_id": "product-x", "repository": str(ext_root),
           "spec_path": "../secret_outside_repo.yaml", "version": "4.0.0", "sha256": _sha256(secret.read_text())}
    root = _build_repo(tmp_path, external_ref=ref)
    with pytest.raises(SpecIntegrityError, match="path traversal or symlink escape"):
        load_active_specs(root)


def test_external_reference_symlink_escape_refused(tmp_path):
    ext_root, _sha = _make_external_repo(tmp_path)
    secret = tmp_path / "secret_target.yaml"
    secret.write_text("spec: leaked\nversion: \"4.0.0\"\nrequirements: []\n")
    link = ext_root / "governance" / "escape_link.yaml"
    try:
        os.symlink(secret, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported in this environment")
    ref = {"project_id": "product-x", "repository": str(ext_root),
           "spec_path": "governance/escape_link.yaml", "version": "4.0.0", "sha256": _sha256(secret.read_text())}
    root = _build_repo(tmp_path, external_ref=ref)
    with pytest.raises(SpecIntegrityError, match="path traversal or symlink escape"):
        load_active_specs(root)
