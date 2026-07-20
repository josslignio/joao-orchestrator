# JOÃO A0.2 CORRECTIF 5 — GLM PROVISIONAL INDEPENDENT REVIEW (round 3, clean candidate)

Read `PRIOR_GLM_VERDICTS.txt` and `WHY_THIS_CANDIDATE_EXISTS.txt` first. You are
reviewing a freshly re-derived candidate, built on a clean base after an earlier
candidate was found to be contaminated by an unrelated commit. Form your own verdict —
do not assume any earlier round's result carries over.

Read-only, no edit, no bash, no network. Materials:

- `FROZEN_AUTHORITY_JOAO_RUN_CARD_A0_2_BYPASSES_1.md` — your only scope authority.
- `CANDIDATE_IDENTITY.txt` — exact base/candidate SHA/tree.
- `SCOPED_DIFF_correctif5_clean.patch` — the exact, only diff vs the clean base.
- `ALL_23_A02_ATTACK_TESTS_RAW.txt`, `E2E_RAW_OUTPUT.txt`, `E2E_TRACE.json`,
  `FULL_SUITE_RAW_OUTPUT.txt` — raw evidence for this exact candidate.

## Your task

1. Read the frozen run card's correctif 5 text.
2. Read `SCOPED_DIFF_correctif5_clean.patch` in full — confirm it is EXACTLY two files
   (`src/joao_orchestrator/bubble/reviewer_contract.py`,
   `tests/test_a0_2_corrections.py`) and contains no memory/lessons/roadmap/governance
   content whatsoever.
3. Confirm the validator now fails closed on all six required keys missing
   (candidate_tree, verdict, findings, reviewer, reviewer.provider, reviewer.model), and
   that a complete valid verdict is still accepted.
4. Confirm the 7 new tests exercise the real `CodexEvidenceReviewer.review()` path, not
   an isolated helper, and assert stable reason strings.
5. Check `ALL_23_A02_ATTACK_TESTS_RAW.txt`: all 23 pass?
6. Check `FULL_SUITE_RAW_OUTPUT.txt`: is there now EXACTLY ONE failure, and is it the
   `test_real_repo_produces_zero_unmapped_with_cv_bot_specs` / external-ledger one (not
   the memory-selection one from the prior contaminated candidate)?
7. Do NOT propose a 9th correctif, do NOT expand into OS security, do NOT
   review/reference gates/C-8/roadmap.
8. Form your own independent verdict.

## Required output — return EXACTLY this block, filled in, as your final answer

```
REVIEWER_PROVIDER=GLM
REVIEWER_INDEPENDENT_FROM_BUILDER=true
EXACT_SHA_REVIEWED=cc796b55808b95210adf9d219ecd91a240b2676c
EXACT_TREE_REVIEWED=7ae68454a64df9063dc3c4f78b14cc888ba6fddf
DECISION=<GO|NO-GO|HARD_BOUNDARY>
P0_P1_IN_SCOPE=<none|list>
CLAIM_ACCURACY=<pass|fail>
EVIDENCE_SUFFICIENT=<true|false>
D_044_ACCEPTED_KNOWN_LIMITATION=<true|false>
KNOWN_LIMITATIONS=<list>
CODE_CHANGES_MADE=false
```

After the block, add a short justification a third party could check against the diff
and raw outputs you were given.
