# JOÃO full recovery audit

## Scope and baseline

- Canonical checkout preserved: `/Users/jocelyngrosjean/joao-orchestrator` at
  `26c1f1c8f9eb31ad56c99a9922d8086593055204`.
- Recovery branch starts from reviewed Stage-A candidate
  `d37f6354647309ebf8fde0c2770062b1aabc282c` in an isolated worktree.
- This audit does not merge, tag, release, mutate the canonical checkout, or
  automate destructive Git operations.

## Findings and disposition

| Severity | Finding | Disposition |
| --- | --- | --- |
| P1 | `validation.json` rendered the executable basename and omitted `args_extra`, so an evidence record could not prove the command actually declared. | Repaired. Evidence now stores exact `argv` and a `shlex.join` rendering. |
| P1 | The former hostile-command regression did not require a unique complete argv match through `args_extra`. | Repaired with a real failing Git command, resolved Git executable, nonempty `args_extra`, unique-match assertion, and fail-closed assertions. |
| P1 | V2 contained product presets, fixture predicates, and benchmark data inside the generic package. | Repaired. Concrete definitions now live under repository-level `project_profiles/`; the canonical package exposes only generic profile, fixture, and benchmark interfaces. |
| P2 | Historical ZCode terminology remains in compatibility models and disabled capability declarations. | Inactive compatibility material; it must not be selected as a runtime implementation path. |

## Safety checks

- No executable `shell=True` was found in source or tests; occurrences are
  policy/documentation text.
- No active source reference was found for `joao-mcp-toy`,
  `joao-control-plane`, `joao-mcp-goal-smoke`, or `/joao-run`.
- `joss_orchestrator` files are compatibility re-exports to the canonical
  `joao_orchestrator` namespace; canonical implementation ownership stays in
  `src/joao_orchestrator`.

## Independent-review commands

```sh
TMPDIR=/private/tmp/joao-recovery-tmp PYTHONDONTWRITEBYTECODE=1 \
  python3 -m pytest -p no:cacheprovider tests/test_fail_closed_runtime.py -q
TMPDIR=/private/tmp/joao-recovery-tmp PYTHONDONTWRITEBYTECODE=1 \
  python3 -m pytest -p no:cacheprovider --collect-only -q
TMPDIR=/private/tmp/joao-recovery-tmp PYTHONDONTWRITEBYTECODE=1 \
  python3 -m pytest -p no:cacheprovider -q
git diff --check
```

Remote verification remains blocked until `origin` has usable GitHub
credentials; no remote SHA is asserted without a successful fetch after push.
