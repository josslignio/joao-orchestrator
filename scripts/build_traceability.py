#!/usr/bin/env python3
"""M0 — build TRACEABILITY_V4.jsonl: defect → requirement → gate → milestone → status.

G2 (mandatory): a requirement declared in specs/*.yaml WITHOUT a verification_gate AND a
roadmap_milestone is INVALID — this generator refuses to emit anything and raises
TraceabilityError rather than silently skip it (D-043: no invisible gap).

Every defect in DEFECTS_LEDGER.md is accounted for: either linked to a requirement (one row
per defect×requirement pair — a requirement's `source_defects: []` still emits one row with
`defect: null` so the requirement itself stays traceable to its gate+milestone), or, if no
requirement in any of the 8 specs covers it, emitted as an explicit `UNMAPPED_PENDING_SPEC`
row with a reason — never silently dropped.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "memory"))
import import_ledger  # noqa: E402  (reuse the existing, tested ledger parser)

DEFAULT_LEDGER = Path.home() / "Claude-HQ" / "DEFECTS_LEDGER.md"

# M0.1 patch (deliverable 6): specs/cv_bot.yaml moved out of this repo (test_project_isolation
# fix, deliverable 4) to ~/job-opportunity-radar/governance/PROJECT_SPEC_V4.yaml, referenced by
# JOÃO only as a typed external reference (SPEC_BUNDLE_MANIFEST_V4.json). Its requirements (and
# the new cv_bot_business_rules.yaml covering the 11 previously-UNMAPPED defects) still need to
# appear in TRACEABILITY_V4.jsonl, so this generator reads them directly, same as any local spec.
DEFAULT_EXTERNAL_SPECS = [
    Path.home() / "job-opportunity-radar" / "governance" / "PROJECT_SPEC_V4.yaml",
    Path.home() / "job-opportunity-radar" / "governance" / "cv_bot_business_rules.yaml",
]


class TraceabilityError(RuntimeError):
    """Raised when a requirement has no gate+milestone — an invalid, un-emittable exigence."""


def all_ledger_defect_ids(ledger_path: Path = DEFAULT_LEDGER) -> list[str]:
    if not ledger_path.is_file():
        return []
    rows = import_ledger.parse_ledger(ledger_path.read_text())
    ids = {r["source_defect"] for r in rows if str(r.get("source_defect", "")).startswith("D-")}
    return sorted(ids, key=lambda d: int(d.split("-")[1]))


def build_traceability(specs_dir: Path = REPO / "specs",
                       ledger_path: Path = DEFAULT_LEDGER,
                       external_specs: list[Path] | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    mapped_defects: set[str] = set()

    spec_files = sorted(specs_dir.glob("*.yaml"))
    for ext in (DEFAULT_EXTERNAL_SPECS if external_specs is None else external_specs):
        if ext.is_file():
            spec_files.append(ext)

    for spec_file in spec_files:
        spec = yaml.safe_load(spec_file.read_text(encoding="utf-8"))
        spec_name = spec.get("spec", spec_file.stem)
        for req in spec.get("requirements", []):
            req_id = req.get("id", "<no-id>")
            gate = (req.get("verification_gate") or "").strip()
            milestone = (req.get("roadmap_milestone") or "").strip()
            if not gate or not milestone:
                raise TraceabilityError(
                    f"orphan requirement {spec_name}/{req_id}: missing "
                    f"{'verification_gate' if not gate else ''}{' and ' if not gate and not milestone else ''}"
                    f"{'roadmap_milestone' if not milestone else ''} — refusing to emit traceability (G2)")
            defects = req.get("source_defects") or [None]
            for defect in defects:
                rows.append({
                    "defect": defect, "requirement": req_id, "spec": spec_name,
                    "gate": gate, "milestone": milestone,
                    "status": req.get("status", "PLANNED"), "severity": req.get("severity"),
                })
                if defect:
                    mapped_defects.add(defect)

    for defect in all_ledger_defect_ids(ledger_path):
        if defect not in mapped_defects:
            rows.append({
                "defect": defect, "requirement": None, "spec": None, "gate": None,
                "milestone": None, "status": "UNMAPPED_PENDING_SPEC",
                "note": ("non couvert par les 8 specs V4 — règle métier (sourcing/scoring/"
                        "salaire) ou incompatibilité d'environnement, pas une exigence de "
                        "sécurité/intégrité d'artefact ; voir M0 report NON VÉRIFIÉ."),
            })
    return rows


def main() -> int:
    rows = build_traceability()
    out = REPO / "TRACEABILITY_V4.jsonl"
    out.write_text("\n".join(json.dumps(r, ensure_ascii=False, sort_keys=True) for r in rows) + "\n", encoding="utf-8")
    mapped = sum(1 for r in rows if r["status"] != "UNMAPPED_PENDING_SPEC")
    unmapped = sum(1 for r in rows if r["status"] == "UNMAPPED_PENDING_SPEC")
    print(f"wrote {out} — {len(rows)} rows ({mapped} mapped, {unmapped} UNMAPPED_PENDING_SPEC)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except TraceabilityError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        sys.exit(1)
