# Codex Global Audit Order

## Authority

Audit the exact candidate produced by applying this overlay to:

```text
01866310a50658626a7352c6f8e5bd9ba87fa601
```

Do not review a summary, working tree with uncommitted files, or a different SHA.

## Required inputs

- complete exact candidate source;
- complete Git bundle/history;
- root-to-head history patch;
- source and package manifests;
- raw outputs for compile, collection, Run Night/M7–M10, M3–M10 compatibility and full repository suite;
- changed-file list and exact diff from base;
- SEC-BOOT base/final hashes;
- secret scan;
- clean-worktree proof.

## Review tracks

1. Run Night execution-root containment and symlink/path behavior.
2. NightArtifact secret detection, false negatives and false positives.
3. Watchdog signal, timeout and process-group cleanup.
4. SEC-BOOT canaries under pytest and standalone execution.
5. Audit-package completeness and reproducibility from a clean clone.
6. CI correctness, no swallowed failures and no stale model assumptions.
7. V4 authority immutability and accuracy of V5 candidate documents.
8. Public-release privacy, provenance and dependency risks.
9. Full codebase architecture, security, performance and correctness regressions.

## Mandatory negative tests

- task execution root outside repository;
- symlinked child path escaping repository;
- GitHub/OpenAI/Anthropic/Slack token embedded in every persisted artifact field;
- SIGTERM/SIGHUP to watchdog while a grandchild process is alive;
- full `pytest --collect-only tests`;
- missing tracked file from source archive;
- audit builder invoked with failed or skipped full suite;
- dirty worktree and output path inside repository.

## Verdict format

Return one exact JSON object:

```json
{
  "status": "ACCEPT|BLOCK",
  "candidate_sha": "<40 lowercase hex>",
  "candidate_tree": "<40 lowercase hex>",
  "reviewed_package_sha256": "<64 lowercase hex>",
  "open_p0": 0,
  "open_p1": 0,
  "findings": [],
  "limits": [],
  "next_action": "GPT_COUNTER_AUDIT|REPAIR"
}
```

Any real P0/P1, missing raw gate, incomplete source/history package or SHA mismatch requires `BLOCK`.
