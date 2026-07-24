#!/usr/bin/env python3
"""
import_from_source.py — deterministic import of the generic orchestrator core
from weekly-trading-radar into joao-orchestrator (canonical rename).

Per C7 §10.2-10.3:
  * imports the GENERIC orchestrator source (src/joss_orchestrator/**) as the
    CANONICAL package `joao_orchestrator`;
  * does NOT import product files (data/, raw/, radar scripts);
  * records full provenance: source repo, source commit, source path, content
    hash, import commit, classification for every file;
  * produces the four required manifests.

The source is NOT rewritten or deleted (§10.2: no rewrite of accepted history).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

SRC = Path.home() / "twitter-scrape-test"
SRC_REF = "autopilot/joao/corrective-foundation"
DST = Path.home() / "joao-orchestrator"


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def git(args: list[str], cwd: Path = SRC) -> str:
    import subprocess
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True).stdout.strip()


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def export_source_files() -> dict[str, bytes]:
    """Export every file under src/joss_orchestrator/ at SRC_REF as {path: bytes}."""
    import subprocess
    listing = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", SRC_REF, "--", "src/joss_orchestrator/"],
        cwd=str(SRC), capture_output=True, text=True).stdout.strip().splitlines()
    files: dict[str, bytes] = {}
    for rel in listing:
        blob = subprocess.run(
            ["git", "show", f"{SRC_REF}:{rel}"],
            cwd=str(SRC), capture_output=True).stdout
        files[rel] = blob
    return files


def classify(rel: str) -> str:
    if "/storage/" in rel:
        return "generic-infrastructure"
    if "/domain/" in rel or "/policy/" in rel:
        return "generic-core"
    if "/v2/" in rel:
        return "generic-v2-governance"
    if "/providers/" in rel:
        return "generic-providers"
    if "/evaluation/" in rel or "/optimization/" in rel:
        return "generic-evaluation"
    if "/capabilities/" in rel or "/observability/" in rel or "/memory/" in rel:
        return "generic-subsystem"
    if rel.endswith("__init__.py"):
        return "generic-package-init"
    return "generic-other"


def rename_package(content: bytes, rel: str) -> tuple[bytes, str]:
    """Rename joss_orchestrator -> joao_orchestrator inside source files.

    Returns (rewritten_bytes, canonical_path). For .py files we do a textual
    rename of the package identifier. Binary/non-py files pass through.
    """
    if not rel.endswith(".py"):
        return content, rel.replace("src/joss_orchestrator/", "src/joao_orchestrator/")
    text = content.decode("utf-8")
    new = text.replace("joss_orchestrator", "joao_orchestrator")
    canon = rel.replace("src/joss_orchestrator/", "src/joao_orchestrator/")
    return new.encode("utf-8"), canon


def main() -> None:
    src_commit = git(["rev-parse", SRC_REF])
    src_remote = git(["config", "--get", "remote.origin.url"])
    files = export_source_files()

    source_manifest: list[dict] = []
    import_manifest: list[dict] = []

    # write canonical package
    canon_pkg_dir = DST / "src" / "joao_orchestrator"
    if canon_pkg_dir.exists():
        shutil.rmtree(canon_pkg_dir)
    canon_pkg_dir.mkdir(parents=True, exist_ok=True)

    for rel, blob in sorted(files.items()):
        src_hash = sha256_bytes(blob)
        renamed, canon_rel = rename_package(blob, rel)
        canon_hash = sha256_bytes(renamed)
        out_path = DST / canon_rel
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(renamed)
        source_manifest.append({
            "source_path": rel,
            "source_commit": src_commit,
            "source_repo": src_remote,
            "source_content_sha256": src_hash,
            "classification": classify(rel),
        })
        import_manifest.append({
            "source_path": rel,
            "import_path": canon_rel,
            "source_commit": src_commit,
            "source_content_sha256": src_hash,
            "import_content_sha256": canon_hash,
            "renamed": src_hash != canon_hash,
            "classification": classify(rel),
        })

    # MANIFEST 1: PROVENANCE.json
    provenance = {
        "schema_version": "1.0",
        "product": "JOÃO.AI",
        "generated_at": utcnow(),
        "repository": "josslignio/joao-orchestrator",
        "visibility": "PRIVATE",
        "origin": {
            "source_repository": src_remote,
            "source_commit": src_commit,
            "source_ref": SRC_REF,
            "option": "C — new repository with selected history import and PROVENANCE.json",
        },
        "canonical_package": "joao_orchestrator",
        "compat_shim_package": "joss_orchestrator",
        "canonical_cli": "joao",
        "legacy_cli_aliases": ["joss", "joss_v2"],
        "import_policy": "source history NOT rewritten; source repo NOT deleted",
        "files_imported": len(files),
    }
    (DST / "PROVENANCE.json").write_text(json.dumps(provenance, indent=2))

    # MANIFEST 2: SOURCE_MANIFEST.json
    (DST / "SOURCE_MANIFEST.json").write_text(json.dumps({
        "schema_version": "1.0", "generated_at": utcnow(),
        "source_repository": src_remote, "source_commit": src_commit,
        "files": source_manifest,
    }, indent=2))

    # MANIFEST 3: IMPORT_MANIFEST.json
    (DST / "IMPORT_MANIFEST.json").write_text(json.dumps({
        "schema_version": "1.0", "generated_at": utcnow(),
        "files": import_manifest,
        "rename_rule": "joss_orchestrator -> joao_orchestrator (textual, .py only)",
    }, indent=2))

    # MANIFEST 4: PROTECTED_REFS.json
    (DST / "PROTECTED_REFS.json").write_text(json.dumps({
        "schema_version": "1.0", "generated_at": utcnow(),
        "protected_refs": [
            {"ref": "main", "protection": "accepted stable; never modified during run"},
            {"ref": "autopilot/joao/corrective-foundation", "protection": "integration branch"},
        ],
        "immutable_history": [
            "accepted tags", "accepted snapshots", "accepted releases",
            "accepted hashes", "prior commit messages", "prior PR references",
            "append-only historical logs",
        ],
        "source_immutable": f"{src_remote}@{src_commit[:12]} (NOT rewritten, NOT deleted)",
    }, indent=2))

    print(f"imported {len(files)} files; canonical package joao_orchestrator created")
    print(f"4 manifests written; source commit {src_commit[:12]}")


if __name__ == "__main__":
    main()
