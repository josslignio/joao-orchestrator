"""M0.1 — THE single loader authority for the V4 documentary bundle (C-1).

`SPEC_BUNDLE_MANIFEST_V4.json` is the ONLY list of active bundle documents. This module is
the ONE place that reads them: every path it returns is verified present in the bundle AND
its sha256 verified to match the file currently on disk — a document outside the bundle, or
one whose content drifted since the bundle was generated, is REFUSED, never silently served
(G1). This supersedes the M0 `SPEC_INDEX_V4.json` / `load_active_specs` (M0.1 patch,
deliverable 1 — ends the activation circularity, contre-review verdict FAIL on it).

Two things are deliberately verified OUTSIDE the bundle's own hash set:

  - `DECISION_LOG.jsonl` — a living journal, read directly and NOT sha256-verified against
    the bundle. It keeps accepting new entries after the bundle is signed; folding it into
    the hashed set (as M0 did) meant a routine decision-log append silently invalidated the
    Constitution's own signed hash — that circularity is exactly what this patch kills.
  - Typed EXTERNAL spec references (`external_spec_references` in the manifest — each entry
    names a sibling product repository, never hardcoded here; see the manifest itself and
    `scripts/build_spec_bundle.py` for which product repos are currently referenced) —
    verified by `_verify_external_reference`, which refuses: a missing file, a sha256
    mismatch, AND a `spec_path` that resolves (after following symlinks) outside the
    referenced repository root (path traversal / symlink escape). This is precisely how
    JOÃO stays isolated from product repos (`tests/test_project_isolation.py`,
    `tests/test_package_boundaries.py`) while still governing them: it reads their spec
    files by hash, at a path given entirely by manifest data, never by an identifier
    embedded in JOÃO's own canonical package.

`ACTIVATION_RECORD_V4.json`, if present, is loaded as `LoadedSpecs.activation` — it is NEVER
required for the bundle itself to load (a bundle can be read, inspected, and hash-verified
before any Boss GO exists), but `runtime_authority` should only ever be trusted from THIS
record, never inferred from the bundle's own content (C-2: silence ≠ GO, D-038).

This is a READ gate, not yet a runtime enforcement gate: no mission-launch path calls this
today (that wiring is `RunRuntime`'s job under A0/M1-A, out of scope for M0/M0.1).

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
    """The verified, parsed V4 authority set. Every field was read through the bundle."""

    bundle: dict[str, Any]
    activation: dict[str, Any] | None
    constitution_text: str
    roadmap_text: str
    specs: dict[str, dict[str, Any]]        # spec name (e.g. "runtime_integrity") -> parsed YAML
    traceability: list[dict[str, Any]]      # parsed TRACEABILITY_V4.jsonl rows
    decisions: list[dict[str, Any]]         # parsed DECISION_LOG.jsonl rows — NOT hash-verified (living log)
    external_specs: dict[str, dict[str, Any]]  # project_id -> parsed external spec YAML
    verified_paths: list[str] = field(default_factory=list)

    @property
    def runtime_authority(self) -> bool:
        """Never inferred from the bundle itself — only ACTIVATION_RECORD_V4.json can say true."""
        if not self.activation:
            return False
        return bool(self.activation.get("runtime_authority")) and bool(
            self.activation.get("boss_go", {}).get("given"))

    def requirement(self, requirement_id: str) -> dict[str, Any] | None:
        """Look up a single requirement by its immutable id across every loaded spec."""
        for spec in self.specs.values():
            for req in spec.get("requirements", []):
                if req.get("id") == requirement_id:
                    return req
        for spec in self.external_specs.values():
            for req in spec.get("requirements", []):
                if req.get("id") == requirement_id:
                    return req
        return None

    def all_requirements(self) -> list[dict[str, Any]]:
        local = [req for spec in self.specs.values() for req in spec.get("requirements", [])]
        external = [req for spec in self.external_specs.values() for req in spec.get("requirements", [])]
        return local + external


class _VerifiedBundle:
    """Internal: the bundle plus a helper that refuses anything not listed + hash-matched."""

    def __init__(self, repo_root: Path, bundle: dict[str, Any]):
        self.repo_root = repo_root
        self.bundle = bundle
        self._by_path = {doc["path"]: doc for doc in bundle.get("bundle_documents", [])}

    def is_active(self, rel_path: str) -> bool:
        return rel_path in self._by_path

    def read_active(self, rel_path: str) -> str:
        """Return the text of a bundle, hash-verified document — refuse anything else."""
        entry = self._by_path.get(rel_path)
        if entry is None:
            raise SpecIntegrityError(
                f"refused: {rel_path!r} is not listed in SPEC_BUNDLE_MANIFEST_V4.json "
                f"bundle_documents — only bundled documents are an active authority (C-1)")
        path = self.repo_root / rel_path
        if not path.is_file():
            raise SpecIntegrityError(f"refused: bundled document missing on disk: {rel_path}")
        actual = _sha256_of(path)
        if actual != entry.get("sha256"):
            raise SpecIntegrityError(
                f"refused: {rel_path!r} sha256 mismatch — bundle has {entry.get('sha256', '')[:16]}…, "
                f"disk has {actual[:16]}… (content drifted since SPEC_BUNDLE_MANIFEST_V4.json was "
                f"generated; a signed bundle must never be regenerated in place — this is either "
                f"tampering or a v4.0.1+ bundle that was never re-signed)")
        return path.read_text(encoding="utf-8")


def _verify_external_reference(ref: dict[str, Any]) -> str:
    """Resolve, hash-verify, and return the text of one external_spec_references entry.

    Refuses: missing file, sha256 mismatch, and a spec_path that (after resolving symlinks)
    lands outside the referenced repository root — path traversal / symlink escape.
    """
    repo_root = Path(ref["repository"]).expanduser().resolve()
    candidate = (repo_root / ref["spec_path"]).resolve()
    if candidate != repo_root and repo_root not in candidate.parents:
        raise SpecIntegrityError(
            f"refused: external reference {ref.get('project_id')!r} spec_path "
            f"{ref.get('spec_path')!r} resolves outside its repository {repo_root} "
            f"(path traversal or symlink escape) — got {candidate}")
    if not candidate.is_file():
        raise SpecIntegrityError(
            f"refused: external reference {ref.get('project_id')!r} spec_path missing on disk: {candidate}")
    actual = _sha256_of(candidate)
    if actual != ref.get("sha256"):
        raise SpecIntegrityError(
            f"refused: external reference {ref.get('project_id')!r} sha256 mismatch — "
            f"manifest has {ref.get('sha256', '')[:16]}…, disk has {actual[:16]}… "
            f"(content drifted or wrong file referenced)")
    return candidate.read_text(encoding="utf-8")


def _find_repo_root(start: Path) -> Path:
    for candidate in (start, *start.parents):
        if (candidate / "SPEC_BUNDLE_MANIFEST_V4.json").is_file():
            return candidate
    return start


def load_active_specs(repo_root: Path | str | None = None) -> LoadedSpecs:
    """Load, hash-verify, and parse the full V4 bundle. Refuses anything not bundled.

    Raises SpecIntegrityError if SPEC_BUNDLE_MANIFEST_V4.json is absent, malformed,
    references a missing file, any bundle document's content no longer matches its
    recorded sha256, or an external_spec_references entry fails its own checks (missing /
    hash mismatch / path escape).
    """
    root = Path(repo_root).expanduser().resolve() if repo_root else _find_repo_root(Path.cwd())
    bundle_path = root / "SPEC_BUNDLE_MANIFEST_V4.json"
    if not bundle_path.is_file():
        raise SpecIntegrityError(f"refused: no SPEC_BUNDLE_MANIFEST_V4.json at {root} — no active bundle")
    try:
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SpecIntegrityError(f"refused: SPEC_BUNDLE_MANIFEST_V4.json is not valid JSON: {exc}") from exc
    if not isinstance(bundle.get("bundle_documents"), list) or not bundle["bundle_documents"]:
        raise SpecIntegrityError("refused: SPEC_BUNDLE_MANIFEST_V4.json has no bundle_documents list")

    verified = _VerifiedBundle(root, bundle)

    constitution_path = next((d["path"] for d in bundle["bundle_documents"] if d["type"] == "constitution"), None)
    roadmap_path = next((d["path"] for d in bundle["bundle_documents"] if d["type"] == "roadmap"), None)
    if not constitution_path or not roadmap_path:
        raise SpecIntegrityError("refused: bundle is missing a constitution or roadmap entry")

    constitution_text = verified.read_active(constitution_path)
    roadmap_text = verified.read_active(roadmap_path)

    if yaml is None:
        raise SpecIntegrityError(f"refused: PyYAML is required to parse specs/*.yaml: {_YAML_IMPORT_ERROR}")

    specs: dict[str, dict[str, Any]] = {}
    for doc in bundle["bundle_documents"]:
        if doc["type"] != "spec":
            continue
        text = verified.read_active(doc["path"])
        parsed = yaml.safe_load(text)
        if not isinstance(parsed, dict) or "spec" not in parsed:
            raise SpecIntegrityError(f"refused: {doc['path']} does not parse to a valid spec document")
        specs[parsed["spec"]] = parsed

    external_specs: dict[str, dict[str, Any]] = {}
    for ref in bundle.get("external_spec_references", []):
        text = _verify_external_reference(ref)
        parsed = yaml.safe_load(text)
        if not isinstance(parsed, dict) or "spec" not in parsed:
            raise SpecIntegrityError(f"refused: external reference {ref.get('project_id')} does not parse to a valid spec document")
        external_specs[ref["project_id"]] = parsed

    traceability = []
    trace_path = next((d["path"] for d in bundle["bundle_documents"] if d["type"] == "traceability"), None)
    if trace_path:
        text = verified.read_active(trace_path)
        for line in text.splitlines():
            line = line.strip()
            if line:
                traceability.append(json.loads(line))

    # DECISION_LOG.jsonl is a living journal OUTSIDE the bundle — read directly, unverified.
    decisions = []
    decisions_path = root / "DECISION_LOG.jsonl"
    if decisions_path.is_file():
        for line in decisions_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                decisions.append(json.loads(line))

    activation = None
    activation_path = root / "ACTIVATION_RECORD_V4.json"
    if activation_path.is_file():
        activation = json.loads(activation_path.read_text(encoding="utf-8"))

    return LoadedSpecs(
        bundle=bundle, activation=activation,
        constitution_text=constitution_text, roadmap_text=roadmap_text,
        specs=specs, traceability=traceability, decisions=decisions,
        external_specs=external_specs,
        verified_paths=sorted(verified._by_path),
    )
