# JOÃO execution roadmap

## Current checkpoint

Validate the Stage-A evidence repair on
`recovery/joao-full-audit-cleanup-v1`, then request an independent review of
the exact pushed commit. No self-acceptance, merge, tag, or release is allowed.

## Next five bounded checkpoints

1. **Independent Stage-A review.** Re-run the focused fail-closed test and
   inspect the exact `validation.json` hostile command entry.
2. **Extract legacy product presets.** Move V2 product profiles and benchmark
   scenarios out of the generic package behind an explicit compatibility/test
   boundary; preserve behavioural tests.
3. **Retire inactive compatibility names.** Remove ZCode terminology from
   active APIs only after consumers have migrated; preserve a documented,
   non-executable compatibility record if needed.
4. **Re-audit policy boundaries.** Re-run command-policy, subprocess, state,
   queue, and package-boundary tests; add regressions for each actual defect.
5. **Human release decision.** After an independent reviewer accepts a pinned
   remote SHA, a human may choose a merge/release procedure. This roadmap does
   not authorize it.

## Required evidence per checkpoint

- Exact branch and commit SHA, clean `git diff --check`, and collected-test
  count.
- JSON/JSONL evidence whose argv is independently inspectable.
- Explicit nonzero failure evidence for hostile validation commands.
- Reviewer identity/evidence hash before any `PASSED` verdict.
- A fetch after push proving local and remote branch SHA equality.
