"""JOSS V2 — Ultra-Sharp Multi-Project Delivery Engine.

V2 is an *additive* layer over the existing canonical engine
(``src/joao_orchestrator/``). It reuses — never duplicates — the already-built
subsystems (atomic storage, roadmap DAG, telemetry, circuit breaker, model
router, test selector, context broker, convergence/review, lessons store) and
fills only the gaps mandated by the V2 master prompt:

* canonical external project state with immutable passed gates (§8, §9)
* a real gate ledger with contradiction-based invalidation (§9)
* objective contracts with backward planning (§10)
* evidence-first preflight + known-good absolute-path capability registry (§11, §12)
* a ``joss_v2`` CLI (§28)

Design invariants (unchanged from the existing engine):

* Standard library only (no package install required).
* State lives OUTSIDE the repo under ``~/.local/share/joss-orchestrator/``.
* Generic core never contains project literals (trading KOLs, job boards, CVs).
* Fail-closed integrity; no secrets in state/telemetry/logs.

Public submodules:

* :mod:`joao_orchestrator.v2.state` — external atomic project state.
* :mod:`joao_orchestrator.v2.gate_ledger` — immutable gate ledger.
* :mod:`joao_orchestrator.v2.objective` — objective contract + backward planner.
* :mod:`joao_orchestrator.v2.preflight` — evidence-first preflight + known-good.
* :mod:`joao_orchestrator.v2.profiles` — trading/job profile presets.
* :mod:`joao_orchestrator.v2.review` — independent review gate.
* :mod:`joao_orchestrator.v2.genesis` — Product Genesis Engine (V2.4 PR-A):
  gates production coding behind a human-accepted Product Blueprint.
* :mod:`joao_orchestrator.v2.control_plane` — Control Plane + Reviewer Bridge
  (V2.4 PR-A2): classifies decisions D0/D1/H1/H2 and routes them without
  operator clicks inside the accepted scope.
* :mod:`joao_orchestrator.v2.deploy_controller` — Auto-Merge / Progressive
  Deploy / Rollback (V2.4 PR-A3): the authorization envelope + zero-click
  pipeline for accepted milestones.
* :mod:`joao_orchestrator.v2.autonomy` — V3 C0 autonomy guard: classifies the
  autonomy level (L0–L4) from REAL evidence only; SIMULATED evidence can never
  promote the level.
* :mod:`joao_orchestrator.v2.pr_gates` — V3 C2 PR gates: budget (lines/files/
  concern), scope (allowed/forbidden paths), evidence-provenance (hardened
  executor: allowlist, no shell, stripped env, timeout, path confinement).
* :mod:`joao_orchestrator.v2.governance` — V3 C2.1 governance bootstrap guard:
  the one-time gate-bootstrap exception is closed permanently; ordinary PRs
  cannot modify GATES.lock or weaken a gate.
* :mod:`joao_orchestrator.v2.cli` — the ``joss_v2`` entry point.
"""

from __future__ import annotations

__all__ = ["__version__"]
__version__ = "2.0.0"

# V2 canonical schema version for all newly written external state artifacts.
V2_SCHEMA_VERSION = "2.0"

# Canonical external runtime root (never tracked by Git).
RUNTIME_ROOT_NAME = "joss-orchestrator"
