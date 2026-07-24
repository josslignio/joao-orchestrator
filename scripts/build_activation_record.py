#!/usr/bin/env python3
"""M0.1 — build ACTIVATION_RECORD_V4.json, the Boss GO record SEPARATE from the bundle.

Deliverable 1 of the M0.1 patch (contre-review verdict: activation circularity FAIL).
Before this patch, `SPEC_INDEX_V4.json` carried both the documents AND (implicitly, via
DECISION_LOG.jsonl being one of its hashed entries) the record of decisions about them —
a document that grows to record its own validation is circular. Now:

  - `SPEC_BUNDLE_MANIFEST_V4.json` is the immutable, hash-verified bundle (never records
    anything about ITS OWN validation).
  - THIS file is where a Boss GO is recorded — it references the bundle's hash, it never
    lives inside it. Regenerating this file never changes `bundle_sha256`.

Until an explicit Boss GO is given (identity + hash + version — Constitution C-2), this
file is emitted with `boss_go.given: false` and `runtime_authority: false`. It is NEVER
self-signed by the control tower (D-038, LOI 1). Re-run this script to refresh
`bundle_sha256_at_generation` if the bundle is regenerated (e.g. before a real GO).
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def build_activation_record(repo: Path = REPO, *, at: str | None = None) -> dict:
    bundle_path = repo / "SPEC_BUNDLE_MANIFEST_V4.json"
    if not bundle_path.is_file():
        raise FileNotFoundError(
            "ACTIVATION_RECORD_V4 build refused: SPEC_BUNDLE_MANIFEST_V4.json does not exist yet "
            "— run scripts/build_spec_bundle.py first")
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    return {
        "schema_version": 1,
        "activation_id": "ACT-V4-PENDING",
        "bundle_version": bundle["bundle_version"],
        "bundle_sha256_at_generation": bundle["bundle_sha256"],
        "generated_at": at or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "boss_go": {
            "given": False,
            "identity": None,
            "date": None,
            "hash_confirmed": None,
            "version_confirmed": None,
        },
        "runtime_authority": False,
        "note": (
            "Enregistrement SÉPARÉ du bundle (fin de la circularité d'activation, patch M0.1 "
            "deliverable 1). Le bundle ne s'auto-déclare jamais signé (C-1/C-2, D-038) — silence "
            "≠ GO. `runtime_authority` ne passe à `true` que si un GO Boss explicite (identité + "
            "hash + version) remplit `boss_go` ci-dessus ET que `boss_go.hash_confirmed` == "
            "`bundle_sha256_at_generation` du bundle SIGNÉ (pas d'un bundle regénéré depuis). "
            "DECISION_LOG.jsonl reste hors bundle et hors de ce record : c'est un journal vivant, "
            "jamais une autorité machine (Constitution §1.6)."),
    }


def main() -> int:
    record = build_activation_record()
    out = REPO / "ACTIVATION_RECORD_V4.json"
    out.write_text(json.dumps(record, indent=2, sort_keys=False, ensure_ascii=False) + "\n")
    signed = "SIGNED" if record["boss_go"]["given"] else "NOT SIGNED (runtime_authority: false)"
    print(f"wrote {out} — bundle_sha256_at_generation={record['bundle_sha256_at_generation'][:16]}… — {signed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
