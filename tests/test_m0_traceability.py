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
    rows = build_traceability(specs_dir=specs, ledger_path=tmp_path / "no-ledger.md", external_specs=[])
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
        build_traceability(specs_dir=specs, ledger_path=tmp_path / "no-ledger.md", external_specs=[])


def test_requirement_missing_milestone_is_refused(tmp_path):
    specs = tmp_path / "specs"; specs.mkdir()
    _write_spec(specs / "fixture.yaml", [{
        "id": "FX-ORPHAN2", "statement": "planted orphan", "severity": 2,
        "verification_gate": "some gate", "roadmap_milestone": "",
        "owner": "contrôleur", "status": "PLANNED", "source_defects": [], "derivation": "verbatim",
    }])
    with pytest.raises(TraceabilityError, match="FX-ORPHAN2.*roadmap_milestone"):
        build_traceability(specs_dir=specs, ledger_path=tmp_path / "no-ledger.md", external_specs=[])


def test_requirement_missing_both_is_refused(tmp_path):
    specs = tmp_path / "specs"; specs.mkdir()
    _write_spec(specs / "fixture.yaml", [{
        "id": "FX-ORPHAN3", "statement": "planted orphan", "severity": 1,
        "verification_gate": "", "roadmap_milestone": "",
        "owner": "contrôleur", "status": "PLANNED", "source_defects": [], "derivation": "verbatim",
    }])
    with pytest.raises(TraceabilityError, match="FX-ORPHAN3"):
        build_traceability(specs_dir=specs, ledger_path=tmp_path / "no-ledger.md", external_specs=[])


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
        build_traceability(specs_dir=specs, ledger_path=tmp_path / "no-ledger.md", external_specs=[])


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
    rows = build_traceability(specs_dir=specs, ledger_path=ledger, external_specs=[])
    unmapped = [r for r in rows if r["status"] == "UNMAPPED_PENDING_SPEC"]
    assert any(r["defect"] == "D-999" for r in unmapped)


def test_real_repo_specs_produce_no_orphans(tmp_path):
    """Integration proof: the ACTUAL specs/*.yaml this run generated pass G2 for real.

    C8-A / G-HERMETIC: `external_specs=[]` and a nonexistent `ledger_path`
    under `tmp_path` pin this to repo-source specs only (legitimate,
    always-allowed repo content) — this test's own stated intent ("the
    ACTUAL specs/*.yaml this run generated") never depended on the sibling
    `~/job-opportunity-radar` repo or the real `~/Claude-HQ` ledger; both
    were only ever incidental (`build_traceability`'s default arguments),
    not this test's purpose. The 0-UNMAPPED-with-external-specs claim
    belongs solely to `test_d044_hermetic_frozen_fixture_produces_zero_unmapped`
    below, which proves it deterministically against a frozen fixture instead.
    """
    repo = Path(__file__).resolve().parents[1]
    rows = build_traceability(specs_dir=repo / "specs", ledger_path=tmp_path / "no-ledger.md", external_specs=[])
    assert len(rows) > 0
    # every requirement-linked row has both gate and milestone (enforced by build_traceability
    # not raising) — this assertion is redundant with "it didn't raise" but documents intent
    assert all(r.get("gate") and r.get("milestone") for r in rows if r["status"] != "UNMAPPED_PENDING_SPEC")


def _write_frozen_d044_fixture(tmp_path: Path) -> tuple[Path, Path, list[Path]]:
    """C8-A / G-HERMETIC (`JOAO_C8_GATE_CONTRACTS.md`, finding GPT v2 #2): the
    frozen fixture that determinizes D-044 — a fully self-contained, frozen
    `DEFECTS_LEDGER` + frozen copies of the cv_bot-style external specs, all
    under `tmp_path`. Never resolves to `~/Claude-HQ` or
    `~/job-opportunity-radar` — the suite's dependency on those live external
    roots is what this fixture removes (D-044's ORIGINAL bug — a real,
    still-open traceability gap in the product backlog — is untouched by this
    fixture and stays open there; only the TEST SUITE's dependency on live
    content disappears). The `D-044` row below is an explicit SYNTHETIC
    demo stand-in (roadmap: "incluant un D-044 de démonstration"), never the
    real product defect — it is fully mapped here on purpose, to prove the
    mechanism, and this in no way marks the real backlog D-044 as resolved.
    """
    specs_dir = tmp_path / "specs"
    specs_dir.mkdir()
    _write_spec(specs_dir / "fixture.yaml", [{
        "id": "FX-DEMO-1", "statement": "frozen demo requirement", "severity": 2,
        "verification_gate": "attack test demo", "roadmap_milestone": "M-DEMO",
        "owner": "contrôleur", "status": "PLANNED", "source_defects": ["D-901"],
        "derivation": "verbatim",
    }])

    external_dir = tmp_path / "external"
    external_dir.mkdir()
    cv_bot_a = external_dir / "PROJECT_SPEC_V4.yaml"
    _write_spec(cv_bot_a, [{
        "id": "CVB-DEMO-1", "statement": "frozen cv_bot demo requirement A", "severity": 2,
        "verification_gate": "attack test demo cv_bot A", "roadmap_milestone": "M-DEMO-CVB",
        "owner": "contrôleur", "status": "PLANNED", "source_defects": ["D-902", "D-044"],
        "derivation": "verbatim",
    }])
    cv_bot_b = external_dir / "cv_bot_business_rules.yaml"
    _write_spec(cv_bot_b, [{
        "id": "CVB-DEMO-2", "statement": "frozen cv_bot demo requirement B", "severity": 2,
        "verification_gate": "attack test demo cv_bot B", "roadmap_milestone": "M-DEMO-CVB",
        "owner": "contrôleur", "status": "PLANNED", "source_defects": ["D-903"],
        "derivation": "verbatim",
    }])

    ledger = tmp_path / "DEFECTS_LEDGER.md"
    ledger.write_text(
        "| ID | Produit | Symptôme | Cause racine | Leçon | Statut |\n|---|---|---|---|---|---|\n"
        "| D-901 | demo | frozen demo defect 1 | **CODE** | fixed | FIXÉ |\n"
        "| D-902 | demo | frozen demo defect 2 | **CODE** | fixed | FIXÉ |\n"
        "| D-903 | demo | frozen demo defect 3 | **CODE** | fixed | FIXÉ |\n"
        "| D-044 | demo | SYNTHETIC demo stand-in for D-044 — NOT the real product defect | "
        "**CODE** | fixed | FIXÉ |\n"
    )
    return specs_dir, ledger, [cv_bot_a, cv_bot_b]


def test_d044_hermetic_frozen_fixture_produces_zero_unmapped(tmp_path):
    """C8-A / G-HERMETIC: D-044 determinized by a fully frozen, self-contained
    fixture (`JOAO_C8_GATE_CONTRACTS.md` G-HERMETIC red->green,
    `G_HERMETIC_EXTERNAL_LEDGER_DEPENDENCY`). Replaces the prior
    `test_real_repo_produces_zero_unmapped_with_cv_bot_specs`, which either
    silently skipped (sibling repo absent) or asserted against live,
    machine-dependent content (sibling repo present) — neither is hermetic
    or deterministic. This test always runs (no skip, no xfail) and asserts
    a fixed, known mapping — the suite no longer depends on the live content
    of `~/Claude-HQ` or `~/job-opportunity-radar` at all.
    """
    specs_dir, ledger, external_specs = _write_frozen_d044_fixture(tmp_path)
    rows = build_traceability(specs_dir=specs_dir, ledger_path=ledger, external_specs=external_specs)
    unmapped = [r for r in rows if r["status"] == "UNMAPPED_PENDING_SPEC"]
    assert unmapped == [], f"frozen fixture must map every defect deterministically, got: {unmapped}"
    mapped_defect_ids = {r["defect"] for r in rows if r["status"] != "UNMAPPED_PENDING_SPEC"}
    assert mapped_defect_ids == {"D-901", "D-902", "D-903", "D-044"}
