#!/usr/bin/env python3
"""M0.1 — build SPEC_BUNDLE_MANIFEST_V4.json, the IMMUTABLE bundle of active V4 authorities.

Supersedes `build_spec_index.py` / `SPEC_INDEX_V4.json` (M0.1 patch — ends the activation
circularity, deliverable 1). Two changes from the retired index:

1. `DECISION_LOG.jsonl` is NOT a bundle document. It is a living journal that keeps
   accepting new entries after the bundle is signed — including it in the hashed set meant
   a routine decision-log append would silently invalidate the Constitution's own signed
   hash. It is read directly by the loader, unverified (see spec_loader.py docstring).
2. `specs/cv_bot.yaml` is NOT a local document — the CV bot business spec now lives in its
   own product repo (`~/job-opportunity-radar/governance/PROJECT_SPEC_V4.yaml`, moved by
   this same patch to restore `test_project_isolation.py`). It is referenced here as a
   TYPED EXTERNAL REFERENCE (project_id/repository/spec_path/version/sha256), verified by
   `spec_loader.py::_verify_external_reference` at load time (missing file / wrong hash /
   path escaping the referenced repo are all refused, never silently skipped).

The bundle, once written and signed (ACTIVATION_RECORD_V4.json), NEVER changes in place.
Any future correction to a bundle document is a new bundle version (v4.0.1, v4.0.2, ...)
with a fresh ACTIVATION_RECORD_V4.json — never an edit-in-place of a signed manifest.

Run: `python3 scripts/build_spec_bundle.py` from the repo root.
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BUNDLE_VERSION = "4.0.0"

# The CLOSED list of LOCAL bundle documents (C-1) — DECISION_LOG.jsonl deliberately excluded.
BUNDLE_DOCUMENTS = [
    ("SYSTEM_CONSTITUTION_V4.md", "constitution"),
    ("specs/runtime_integrity.yaml", "spec"),
    ("specs/security.yaml", "spec"),
    ("specs/memory.yaml", "spec"),
    ("specs/phase0.yaml", "spec"),
    ("specs/provider_cascade.yaml", "spec"),
    ("specs/product_ui.yaml", "spec"),
    ("specs/operations.yaml", "spec"),
    ("ROADMAP_V4.md", "roadmap"),
    ("TRACEABILITY_V4.jsonl", "traceability"),
]

# Typed external references — a spec that lives in a sibling product repo, not copied in.
EXTERNAL_SPEC_REFERENCES = [
    {
        "project_id": "cv-bot",
        "repository": "~/job-opportunity-radar",
        "spec_path": "governance/PROJECT_SPEC_V4.yaml",
        "version": "4.0.0",
    },
]


def sha256_of(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1048576), b""):
            value.update(block)
    return value.hexdigest()


def _resolve_external(ref: dict) -> Path:
    repo_root = Path(ref["repository"]).expanduser().resolve()
    candidate = (repo_root / ref["spec_path"]).resolve()
    if repo_root not in candidate.parents and candidate != repo_root:
        raise FileNotFoundError(
            f"external reference {ref['project_id']!r} spec_path escapes its repository: {candidate}")
    return candidate


def build_bundle(repo: Path = REPO, *, at: str | None = None) -> dict:
    documents = []
    missing = []
    for rel, doc_type in BUNDLE_DOCUMENTS:
        path = repo / rel
        if not path.is_file():
            missing.append(rel)
            continue
        documents.append({
            "path": rel,
            "type": doc_type,
            "version": BUNDLE_VERSION,
            "sha256": sha256_of(path),
            "bytes": path.stat().st_size,
        })
    if missing:
        raise FileNotFoundError(f"SPEC_BUNDLE_MANIFEST_V4 build refused: missing bundle documents: {missing}")

    external_refs = []
    for ref in EXTERNAL_SPEC_REFERENCES:
        path = _resolve_external(ref)
        if not path.is_file():
            raise FileNotFoundError(
                f"external reference {ref['project_id']!r} missing on disk: {path}")
        external_refs.append({**ref, "sha256": sha256_of(path)})

    canonical = json.dumps(
        {"bundle_documents": documents, "external_spec_references": external_refs},
        sort_keys=True, ensure_ascii=False)
    bundle_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    return {
        "schema_version": 1,
        "bundle_version": BUNDLE_VERSION,
        "generated_at": at or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "immutable": True,
        "immutability_note": (
            "Ce manifest ne change JAMAIS après signature (ACTIVATION_RECORD_V4.json). "
            "DECISION_LOG.jsonl est un journal vivant HORS bundle (voir docstring) — il "
            "peut recevoir de nouvelles entrées sans jamais invalider bundle_sha256."),
        "bundle_documents": documents,
        "external_spec_references": external_refs,
        "bundle_sha256": bundle_sha256,
    }


def main() -> int:
    bundle = build_bundle()
    out = REPO / "SPEC_BUNDLE_MANIFEST_V4.json"
    out.write_text(json.dumps(bundle, indent=2, sort_keys=False, ensure_ascii=False) + "\n")
    print(f"wrote {out} — {len(bundle['bundle_documents'])} bundle documents "
          f"+ {len(bundle['external_spec_references'])} external reference(s)")
    for doc in bundle["bundle_documents"]:
        print(f"  {doc['path']:40s} {doc['sha256'][:16]}… ({doc['bytes']} bytes)")
    for ref in bundle["external_spec_references"]:
        print(f"  [external] {ref['repository']}/{ref['spec_path']:30s} {ref['sha256'][:16]}…")
    print(f"bundle_sha256 = {bundle['bundle_sha256']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
