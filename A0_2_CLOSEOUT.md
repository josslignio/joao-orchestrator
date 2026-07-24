# A0.2 CLOSEOUT — clean branch, 2026-07-20

BOSS GO — Option A: disentangle A0.2 from lessons integration. This branch
(`feat/joao-a0-integrite-clean`) exists specifically to correct a scope leak in the
prior candidate.

## STATUS

```
STATUS=PROVISIONALLY_CLOSED
PROVISIONAL_TAG=joao-a0.2-provisionally-closed-clean-20260720
PROVISIONAL_TAG_TARGET=cc796b55808b95210adf9d219ecd91a240b2676c
PROVISIONAL_REVIEWER=GLM
GLM_INDEPENDENT_REVIEW=PASS (DECISION=GO)
CODEX_FINAL_REVIEW=PENDING_2026-07-23
FINAL_CODEX_VALIDATION=false
FINAL_TAG_CREATED=false
```

## Why this branch exists

Candidate `d305f570f3d88858465e2c5f6797bf4743aefa33` (on `feat/joao-a0-integrite`)
closed correctif 5 correctly, and an earlier GLM review of it returned GO — but it was
built on a branch tip that also carried an unrelated commit,
`639ec01954fdd5eb53b86cd465a922e217016e0d` ("integrate CV-SEC-CORE lessons L-048..L-059
+ C-8 doctrine"), which turned out to cause a real, second full-suite regression
(`test_visual_docx_surfaces_authority_chain`, a memory/lessons-selection test broken by
that commit's changes to `memory/lessons.jsonl`). That candidate is **superseded**, not
because correctif 5's own fix was wrong, but because its base was contaminated with
out-of-scope changes.

`d305f570f3d88858465e2c5f6797bf4743aefa33` is marked `SUPERSEDED_SCOPE_LEAK`. Its
provisional tag (`joao-a0.2-provisionally-closed-20260720`) has been deleted (not
merely revoked-in-writing — it was created in the same session as this correction, not
pushed, not yet referenced by Codex, so clean removal is safe; full details preserved
below for traceability).

## This candidate

- **Base:** `114229b79a09108dba166dbace0821d190438b3b` (the original, uncontaminated
  A0.2 code candidate).
- **Candidate:** `cc796b55808b95210adf9d219ecd91a240b2676c`, tree
  `7ae68454a64df9063dc3c4f78b14cc888ba6fddf` — exactly one cherry-picked commit
  (correctif 5's fix + tests) on top of the clean base.
- **Verified:** `git diff --name-only 114229b79a09108dba166dbace0821d190438b3b` shows
  EXACTLY two files: `src/joao_orchestrator/bubble/reviewer_contract.py`,
  `tests/test_a0_2_corrections.py`. No `memory/lessons.jsonl`, no
  `SYSTEM_CONSTITUTION_V4.md`, no C-8 integration, no governance/evidence/closeout
  documentation of any kind is in this candidate's diff from the base.

## Evidence (raw, `evidence/A0_2_CORRECTIF5_CLEAN_20260720/`)

- Correctif 5 focused tests: 7/7 passed.
- All A0.2 attack tests: 23/23 passed.
- E2E (isolated `JOAO_MEMORY_DIR`): 7/7 green, real `memory/lessons.jsonl` and the live
  workspace proven byte-identical before/after.
- Full deterministic suite: **329 passed, 1 failed** — the failure is
  `tests/test_m0_traceability.py::test_real_repo_produces_zero_unmapped_with_cv_bot_specs`
  (the same external `~/Claude-HQ/DEFECTS_LEDGER.md` dependency already documented for
  the original candidate — 9 unrelated CV-bot defects). **This is now the SOLE
  failure** — the second, memory-contamination-caused failure is gone, confirming the
  disentanglement worked.
- `git diff --check` between base and candidate: clean, no whitespace errors.

## GLM review

A real, independent GLM review (`opencode` + `zai-coding-plan/glm-4.5-air` via
`~/.local/bin/joao-glm --mode read-only`, genuine inference, zero session context, this
is round 3 — round 1 was NO-GO against the original candidate before correctif 5 was
written, round 2 was GO against the now-superseded contaminated candidate) reviewed
this exact clean candidate independently and returned **GO**. Full transcript:
`evidence/A0_2_CORRECTIF5_CLEAN_20260720/GLM_REVIEW/`.

## Codex handoff (updated)

```
CODEX_REVIEW_TARGET_SHA=cc796b55808b95210adf9d219ecd91a240b2676c
CODEX_REVIEW_TARGET_TREE=7ae68454a64df9063dc3c4f78b14cc888ba6fddf
CODEX_REVIEW_TARGET_BRANCH=feat/joao-a0-integrite-clean
CODEX_FINAL_REVIEW=PENDING_2026-07-23
```

Do not review `d305f570f3d88858465e2c5f6797bf4743aefa33` or the
`feat/joao-a0-integrite` branch tip for A0.2 purposes — that candidate is superseded.
This branch and this SHA are the current provisional candidate.

## Forbidden / not done in this run

No C-8 gates implemented. No lessons integrated. No roadmap change. No SOURCE-FRESH. No
merge (this branch was never merged into `feat/joao-a0-integrite` or `main`). No push.
No promotion. `D-044` was not skipped, xfailed, or modified.
