"""M0 — G2: the traceability generator FAILS on an orphan requirement (no gate/milestone)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from build_traceability import TraceabilityError, build_traceability  # noqa: E402


def _write_spec(path: Path, requirements: list[dict]) -> None:
    path.write_text(yaml.safe_dump({"spec": "fixture", "requirements": requirements}, sort_keys=False))


def test_valid_requirement_produces_rows(tmp_path):
    specs = tmp_path / "specs"; specs.mkdir()
    _write_spec(specs / "fixture.yaml", [{
        "id": "FX-1", "statement": "x", "severity": 3, "verification_gate": "attack test 1",
        "roadmap_milestone": "M1-A", "owner": "contrôleur", "status": "PLANNED",
        "source_defects": ["D-001"], "derivation": "verbatim",
    }])
    rows = build_traceability(specs_dir=specs, ledger_path=tmp_path / "no-ledger.md")
    assert rows == [{"defect": "D-001", "requirement": "FX-1", "spec": "fixture",
                     "gate": "attack test 1", "milestone": "M1-A", "status": "PLANNED", "severity": 3}]


def test_requirement_missing_gate_is_refused(tmp_path):
    specs = tmp_path / "specs"; specs.mkdir()
    _write_spec(specs / "fixture.yaml", [{
        "id": "FX-ORPHAN", "statement": "planted orphan", "severity": 2, "verification_gate": "",
        "roadmap_milestone": "M1-A", "owner": "contrôleur", "status": "PLANNED",
        "source_defects": [], "derivation": "verbatim",
    }])
    with pytest.raises(TraceabilityError, match="FX-ORPHAN.*verification_gate"):
        build_traceability(specs_dir=specs, ledger_path=tmp_path / "no-ledger.md")


def test_requirement_missing_milestone_is_refused(tmp_path):
    specs = tmp_path / "specs"; specs.mkdir()
    _write_spec(specs / "fixture.yaml", [{
        "id": "FX-ORPHAN2", "statement": "planted orphan", "severity": 2,
        "verification_gate": "some gate", "roadmap_milestone": "",
        "owner": "contrôleur", "status": "PLANNED", "source_defects": [], "derivation": "verbatim",
    }])
    with pytest.raises(TraceabilityError, match="FX-ORPHAN2.*roadmap_milestone"):
        build_traceability(specs_dir=specs, ledger_path=tmp_path / "no-ledger.md")


def test_requirement_missing_both_is_refused(tmp_path):
    specs = tmp_path / "specs"; specs.mkdir()
    _write_spec(specs / "fixture.yaml", [{
        "id": "FX-ORPHAN3", "statement": "planted orphan", "severity": 1,
        "verification_gate": "", "roadmap_milestone": "",
        "owner": "contrôleur", "status": "PLANNED", "source_defects": [], "derivation": "verbatim",
    }])
    with pytest.raises(TraceabilityError, match="FX-ORPHAN3"):
        build_traceability(specs_dir=specs, ledger_path=tmp_path / "no-ledger.md")


def test_one_orphan_among_many_still_fails_the_whole_batch(tmp_path):
    # G2 requires the generator to refuse, not silently drop the bad one and emit the rest
    specs = tmp_path / "specs"; specs.mkdir()
    _write_spec(specs / "fixture.yaml", [
        {"id": "FX-OK", "statement": "fine", "severity": 3, "verification_gate": "gate",
         "roadmap_milestone": "M1-A", "owner": "c", "status": "PLANNED",
         "source_defects": [], "derivation": "verbatim"},
        {"id": "FX-BAD", "statement": "orphan", "severity": 3, "verification_gate": "",
         "roadmap_milestone": "M1-A", "owner": "c", "status": "PLANNED",
         "source_defects": [], "derivation": "verbatim"},
    ])
    with pytest.raises(TraceabilityError, match="FX-BAD"):
        build_traceability(specs_dir=specs, ledger_path=tmp_path / "no-ledger.md")


def test_unmapped_ledger_defects_are_explicit_not_dropped(tmp_path):
    specs = tmp_path / "specs"; specs.mkdir()
    _write_spec(specs / "fixture.yaml", [{
        "id": "FX-1", "statement": "x", "severity": 3, "verification_gate": "g",
        "roadmap_milestone": "M1-A", "owner": "c", "status": "PLANNED",
        "source_defects": ["D-001"], "derivation": "verbatim",
    }])
    ledger = tmp_path / "ledger.md"
    ledger.write_text(
        "| ID | Produit | Symptôme | Cause racine | Leçon | Statut |\n|---|---|---|---|---|---|\n"
        "| D-001 | x | mapped one | **CODE** | fixed | FIXÉ |\n"
        "| D-999 | x | orphan defect not covered by any spec | **CODE** | tbd | OUVERT |\n"
    )
    rows = build_traceability(specs_dir=specs, ledger_path=ledger)
    unmapped = [r for r in rows if r["status"] == "UNMAPPED_PENDING_SPEC"]
    assert any(r["defect"] == "D-999" for r in unmapped)


def test_real_repo_specs_produce_no_orphans():
    """Integration proof: the ACTUAL specs/*.yaml this run generated pass G2 for real."""
    repo = Path(__file__).resolve().parents[1]
    rows = build_traceability(specs_dir=repo / "specs")
    assert len(rows) > 0
    # every requirement-linked row has both gate and milestone (enforced by build_traceability
    # not raising) — this assertion is redundant with "it didn't raise" but documents intent
    assert all(r.get("gate") and r.get("milestone") for r in rows if r["status"] != "UNMAPPED_PENDING_SPEC")
