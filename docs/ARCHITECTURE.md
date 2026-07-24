# JOÃO Architecture

## Purpose

JOÃO is a control plane that separates **planning**, **construction**, **review**, **authority** and **promotion**. The separation is deliberate: no AI family can construct a change and independently authorize that same change.

## Main planes

### Authority plane

- signed governance bundle and activation record;
- project specifications and exact hashes;
- SEC-BOOT/write-tier policy;
- Boss approvals and closure certificates.

### Supervisor plane

- typed task and provider contracts;
- Direct, Auto, Challenge, Council and Builder/Reviewer modes;
- provider-family independence;
- budgets and stop conditions;
- metadata-only task persistence.

### Provider plane

- GPT/Codex, Claude/Claude Code and GLM/Z Code adapters;
- backend-proven model identity;
- controlled working directory and environment;
- read-only permissions unless separately activated;
- no trust in user/global provider configuration.

### Execution plane

- exact repository/worktree root;
- subprocess isolation, timeout and process-group cleanup;
- network and capability policies;
- before/after working-tree and Git control-plane fingerprints;
- immutable candidate commits and rollback.

### Evidence plane

- exact source SHA/tree and package hashes;
- raw test outputs and exit codes;
- structured findings and verdicts;
- no raw provider transcripts or credentials;
- Codex exact-SHA review and GPT counter-audit.

### Product plane

- local bubble and future Control Room;
- projects, providers, runs, quotas, gates and approval queue;
- Benchmark Lab and provider routing;
- morning reports from bounded Run Nights.

## Current trust boundary

```text
Boss
  -> signed authority / explicit approval
JOÃO Supervisor
  -> typed request + bounded context + permission policy
Builder provider
  -> isolated worktree or read-only root
Tests and deterministic gates
  -> exact candidate SHA
Independent reviewer (Codex)
  -> ACCEPT/BLOCK
GPT counter-audit
  -> PASS/REQUEST_CHANGES
Boss
  -> merge/push/promotion decision
```

## Current state

Tranche 3 is closed at `1bfa76bb53d3b158f53b54c5b1a18fcf47091fb2`. Run Night Master candidate `01866310a50658626a7352c6f8e5bd9ba87fa601` provides authenticated read-only overnight operation but is undergoing final global hardening before merge. Provider Mesh, Control Room and Candidate Build follow as separate gated milestones.
