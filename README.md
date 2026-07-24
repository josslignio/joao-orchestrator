# JOÃO.AI

**A security-first multi-agent engineering control plane for building, reviewing and operating software with GPT, Codex, Claude and GLM/Z Code.**

JOÃO coordinates AI systems without letting a model silently validate its own work. It binds tasks to exact Git SHAs, enforces provider identity and permissions, runs deterministic gates, requires independent review and leaves promotion under human control.

> Current stage: **private alpha / audited candidate**. Tranche 3 is closed. Run Night Master is in final global hardening. Candidate Build, automatic merge and deployment are not enabled.

## What JOÃO does

- routes work by provider capability, identity, permissions and measured performance;
- supports Direct, Auto, Challenge, Council and Builder/Reviewer workflows;
- separates builder, reviewer, GPT counter-audit and Boss approval;
- controls subprocesses, working directories, network/capability boundaries and timeouts;
- detects working-tree and Git control-plane mutations;
- creates exact-SHA evidence packages with raw gate outputs;
- runs authenticated, bounded, read-only overnight analysis;
- preserves structured artifacts without raw provider transcripts or credentials;
- maintains compatibility with the legacy `joss_orchestrator` package while `joao_orchestrator` is canonical.

## Current verified milestones

| Milestone | State | Canonical evidence |
|---|---|---|
| Tranche 1 — interaction foundation | closed | repository history |
| Tranche 2 — execution/security closure | closed | SHA `76de966a8a5cfae29c4093a9c5e822bda5191ce6` |
| M7 — Supervisor Core | closed | exact-SHA review chain |
| M8 — Provider Bridge/read-only enforcement | closed | mutation and identity tests |
| M9 — Codex exact-SHA red-team | closed | `ACCEPT` |
| M10 — supervised live smoke | closed | exact `JOAO_M10_OK` |
| Tranche 3 | closed | SHA `1bfa76bb53d3b158f53b54c5b1a18fcf47091fb2` |
| Run Night Master | final global hardening | candidate `01866310a50658626a7352c6f8e5bd9ba87fa601` |

## Architecture

```text
Boss authority
    ↓
JOÃO Supervisor Core
    ├── GPT / Claude strategy and challenge
    ├── GLM / Claude Code construction
    └── Codex exact-SHA independent review
    ↓
Deterministic tests, security gates and evidence
    ↓
Human approval queue
```

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the trust boundaries and execution planes.

## Run Night

The current Run Night design is read-only and fail-closed:

- HMAC-signed, expiring, one-use activation;
- exact SHA/evidence/closure/SEC-BOOT binding;
- bounded tasks, calls, context, artifacts and duration;
- repository-bounded execution roots;
- watchdog and manual stop;
- independent reviewer requirement;
- outputs remain `AWAITING_APPROVAL`;
- no merge, push, deployment, publication or write-tier activation.

The future Candidate Build mode is a separate signed milestone and will write only inside isolated worktrees.

## Local verification

```bash
python -m pip install PyYAML==6.0.3 pytest==9.0.2

# Prove the complete suite can be collected.
python -m pytest --collect-only -q tests

# SEC-BOOT and Run Night security matrix.
python -m pytest -q \
  tests/sec_boot \
  tests/test_run_night_master.py \
  tests/test_run_night_watchdog.py \
  tests/test_m7_supervisor_core.py \
  tests/test_m8_provider_bridge.py \
  tests/test_m8_readonly_enforcement.py \
  tests/test_m8_final_security_closure.py \
  tests/test_m9_supervisor_redteam.py \
  tests/test_m9_m10_strict_live_gates.py

# Full repository gate.
python -m pytest -q tests
```

Build a reproducible audit package from a clean exact-SHA worktree:

```bash
python scripts/build_full_global_audit.py \
  --output "$HOME/JOAO_EVIDENCE/FULL_GLOBAL_AUDIT.zip"
```

The builder includes the complete tracked source, Git history bundle, commit chain, raw gate outputs and hash manifest. It cannot report PASS when the full suite was skipped or failed.

## Governance

The signed V4 bundle remains runtime authority. [`JOAO_MASTER_SPEC_V5.md`](JOAO_MASTER_SPEC_V5.md), [`ROADMAP_V5.md`](ROADMAP_V5.md) and [`JOAO_PRODUCT_BACKLOG_V5.md`](JOAO_PRODUCT_BACKLOG_V5.md) are candidates describing the actual implementation state and next sequence; they become authoritative only through a new explicit activation record.

## Next sequence

1. close global audit hardening with Codex and GPT on the exact SHA;
2. merge and push the approved private canonical repository;
3. install Provider Mesh + Control Room V1;
4. certify GPT, Codex, Claude, Claude Code, GLM/Z Code and Chrome Pilot;
5. run Benchmark Lab and the whole-codebase multi-AI audit;
6. activate Candidate Build in isolated worktrees;
7. implement Tranche 4 through bounded overnight milestones.

## Honest limitations

- Provider Mesh and the complete Control Room are not on the canonical branch yet.
- Candidate Build and write-tier are OFF.
- No automatic merge, push or deployment is authorized.
- Public release is blocked until [`PUBLIC_RELEASE_GATE.md`](PUBLIC_RELEASE_GATE.md) is complete, including a deliberate license decision and history-wide privacy scan.
- Test counts alone never establish production readiness.

## Why this project matters

JOÃO explores a practical question in AI-assisted engineering: **how do we obtain the speed of multiple AI builders without losing provenance, security, independent judgment and human control?** The project combines product architecture, developer tooling, security engineering, evaluation design and operational automation.
