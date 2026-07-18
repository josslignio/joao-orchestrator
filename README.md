# JOÃO.AI — `joao-orchestrator`

**The canonical JOÃO.AI orchestration core.** Human-facing brand: **JOÃO.AI**. Technical identifier: `joao`.

This repository is the **Option C** split (C7) of the generic orchestration core out of `josslignio/weekly-trading-radar`, created with **selected history import + full provenance** (no rewrite of accepted source history).

## Status

| Field | Value |
|---|---|
| Release stage | **ALPHA** — see `SYSTEM_CONSTITUTION_V4.md` §4 (proof levels P0→P7) and `ROADMAP_V4.md` (12-item GA checklist, no date). Never GA/stable/production-ready on the basis of a test count alone (D-043). |
| Visibility | PRIVATE (intended) |
| Autonomy | L0-SHADOW |
| Canonical package | `joao_orchestrator` |
| Compat shim | `joss_orchestrator` (re-exports canonical, emits deprecation) |
| Canonical CLI | `joao` |
| Legacy CLI aliases | `joss`, `joss_v2` (call canonical + emit deprecation) |
| Source repo | `josslignio/weekly-trading-radar` (NOT rewritten, NOT deleted) |

## Provenance

Four manifests record the exact, hash-verified import:

- `PROVENANCE.json` — origin, source commit, canonical/shim/CLI identities, import policy.
- `SOURCE_MANIFEST.json` — every source file: path, commit, content hash, classification.
- `IMPORT_MANIFEST.json` — every file: source path, import path, source hash, import hash, rename flag.
- `PROTECTED_REFS.json` — protected refs + immutable history.

## Packages

- **`joao_orchestrator`** (canonical) — the real implementation, renamed from `joss_orchestrator`.
- **`joss_orchestrator`** (compat shim) — contains **no business logic**; re-exports the canonical package and emits a structured deprecation event on import. Covered by parity tests.

## CLI

```bash
python3 src/joao_orchestrator/cli/joao.py version     # canonical
python3 src/joao_orchestrator/cli/joss.py version      # legacy alias (deprecation)
python3 src/joao_orchestrator/cli/joss_v2.py version   # legacy alias (deprecation)
```

## Tests

```bash
python3 scripts/test_parity.py    # canonical-vs-shim parity + manifest integrity
```

## Honest constraints

- This repository was created **locally** (the GitHub repo `josslignio/joao-orchestrator` does not yet exist because `gh` CLI is unauthenticated). It is a real git repository with real provenance, pushable to GitHub when authentication is available.
- The source repository is **not deleted or rewritten**. Source-repo generic files remain until parity is accepted and the pinned consumer path works (§10.5).
- No merge to `main` of either repo during this run.
