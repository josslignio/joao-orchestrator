I'll read all the materials to form an independent verdict. Let me start by examining the files mentioned in the task.```
REVIEWER_PROVIDER=GLM
REVIEWER_INDEPENDENT_FROM_BUILDER=true
EXACT_SHA_REVIEWED=cc796b55808b95210adf9d219ecd91a240b2676c
EXACT_TREE_REVIEWED=7ae68454a64df9063dc3c4f78b14cc888ba6fddf
DECISION=GO
P0_P1_IN_SCOPE=none
CLAIM_ACCURACY=pass
EVIDENCE_SUFFICIENT=true
D_044_ACCEPTED_KNOWN_LIMITATION=true
KNOWN_LIMITATIONS=external-ledger dependency (test_real_repo_produces_zero_unmapped_with_cv_bot_specs)
CODE_CHANGES_MADE=false
```

**Justification:**

1. **SCOPED_DIFF_correctif5_clean.patch** modifies exactly two files: `src/joao_orchestrator/bubble/reviewer_contract.py` and `tests/test_a0_2_corrections.py`. No memory/lessons/roadmap/governance content present.

2. **Validator now fails closed** on all six required keys: `candidate_tree`, `verdict`, `findings`, `reviewer.provider`, `reviewer.model`. A complete valid verdict is accepted.

3. **7 new tests** exercise the real `CodexEvidenceReviewer.review()` path (not isolated helpers), reading actual `review-import.json` files and asserting stable reason strings including "correctif-5" tag.

4. **ALL 23 A02 attack tests pass** (ALL_23_A02_ATTACK_TESTS_RAW.txt:32 shows 23 passed, EXIT=0).

5. **FULL_SUITE_RAW_OUTPUT.txt** shows EXACTLY ONE failure: `test_real_repo_produces_zero_unmapped_with_cv_bot_specs` (external-ledger dependency), NOT the memory-selection failure from the contaminated candidate.

6. **Clean base**: Built on 114229b with no memory integration contamination (CANDIDATE_IDENTITY.txt:8 confirms no memory/lessons.jsonl changes, no C-8 integration).

7. **E2E_TRACE.json** shows successful end-to-end promotion/rollback with hash consistency verified.

8. **No scope expansion**: No OS security, gates/C-8/roadmap content introduced - correctif 5 strictly bounded to JSON schema validation.