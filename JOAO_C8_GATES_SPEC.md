# JOÃO C-8 GATES + WORKER INTEGRATION — PRODUCT/TECHNICAL SPEC

**Phase 0 — SPEC + ROADMAP ONLY. No implementation in this branch.**
Branch: `feat/joao-c8-gates-spec`. Base:
`feat/joao-a0-integrite-clean` @ `10156a2311dd4018944d1b10000f262fbaac06f3`.
A0.2 is frozen at `cc796b55808b95210adf9d219ecd91a240b2676c`
(`PROVISIONALLY_CLOSED`, `CODEX_FINAL_REVIEW=PENDING_2026-07-23`) and is not modified,
referenced-for-editing, or reopened by this document — it is read-only context.

Companion documents: `JOAO_C8_GATE_CONTRACTS.md` (per-gate inputs/outputs/reason
codes/entrypoints), `JOAO_WORKER_INTEGRATION_SPEC.md` (builder/reviewer/orchestration
detail), `JOAO_C8_GATES_ROADMAP.md` (milestones), `JOAO_C8_OPEN_DECISIONS.md`
(Boss-level decisions this spec cannot make for itself).

---

## 1. Goals

1. Convert the seven C-8 lessons (already integrated into `memory/lessons.jsonl` on
   `feat/joao-a0-integrite` at commit `639ec01`, **not this branch** — see §4) from
   prose lessons into executable, fail-closed gates with stable reason codes, wired
   into the real run pipeline.
2. Give JOÃO a real `GLMReviewer` adapter (the missing half of what this session
   already proved works manually — see `JOAO_WORKER_INTEGRATION_SPEC.md` §1) so GLM
   can be dispatched as an independent reviewer BY THE SYSTEM, not only by a human
   copy-pasting a task file.
3. Give JOÃO an orchestration controller that walks Boss GO → approved SPEC/ROADMAP →
   build → gates → freeze → review → canary/promotion → Boss approval, end to end,
   without a human manually sequencing `RunRuntime` calls (which is how every run in
   this repo's history, including this session's own A0.2 work, has actually happened
   so far).
4. Make the discipline this session repeatedly enforced BY HAND — reviewer
   independence, hermetic tests, real-entrypoint proof, SHA-binding, canary-before-
   promotion, frozen scope — a property of the SYSTEM instead of a property of
   whichever human/agent happens to be careful that day.

## 2. Explicit non-goals

- **Not** an eighth gate, ever, in this milestone (G-FROZEN-FINISH-LINE applies to this
  spec's own scope first).
- **Not** OS-level security hardening or a new isolation boundary — unchanged claim,
  see `JOAO_WORKER_INTEGRATION_SPEC.md` §14.
- **Not** a rebuild of `RunRuntime`, `promotion.py`, or `execution_backend.py` — the
  orchestration controller calls existing primitives; it does not replace them.
- **Not** SOURCE-FRESH implementation — SOURCE-FRESH is the intended FIRST mission to
  run through this milestone's output, not part of building it.
- **Not** memory/lessons.jsonl changes, roadmap changes, or Competitor/OSS Intelligence
  — all explicitly forbidden for this Phase-0 run.
- **Not** a retroactive reopening of A0.2 — any A0.2-scoped finding this planning work
  surfaces is logged in `JOAO_C8_OPEN_DECISIONS.md` as a candidate for a SEPARATE,
  future, distinctly-authorized run, never folded back into A0.2.
- **Not** a new authentication/signature mechanism for "Boss GO" (see
  `JOAO_WORKER_INTEGRATION_SPEC.md` §3) — reuses the existing named-instruction
  convention.

## 3. Current-state architecture

See `JOAO_WORKER_INTEGRATION_SPEC.md` §1 for the full, source-verified table. Summary:
`RunRuntime` (`bubble/runtime.py`) is a real, tested state machine
(`ready → building → needs_approval → accepted → promoted`, plus `blocked`/`paused`)
already exercised end-to-end by `scripts/a0_toy_mission_e2e.py`. Builders
(`SandboxBuilder`, `GLMBuilder`) and reviewers (`CodexEvidenceReviewer`,
`CodexCLIReviewer`) are real `Adapter` implementations dispatched through one
`ExecutionBackend.execute()` choke point (A0.2 correctif 2/12.2). What does not exist:
a `GLMReviewer` adapter, and any code that sequences a full mission unattended — both
gaps this milestone closes.

## 4. Target architecture

Additive only, per G-FROZEN-FINISH-LINE and G-NO-STALE-ENTRYPOINT:
1. `GLMReviewer(ReviewerAdapter)` — new class, `bubble/runtime.py` (detail:
   `JOAO_WORKER_INTEGRATION_SPEC.md` §2.1).
2. Seven gate checks wired into `RunRuntime.approve()` / `.promote()` / the pytest
   session / a new static-audit script — never a new parallel state machine (detail:
   `JOAO_C8_GATE_CONTRACTS.md`, one section per gate, "Real entrypoint" line each).
3. A thin orchestration controller module (name/location TBD at implementation time;
   `bubble/orchestrator.py` is the current working name) that sequences existing
   `RunRuntime` calls per §9's state-machine diagram — implemented in M3/M4, not this
   Phase-0 branch.

**Note on the memory-integration precedent:** this milestone's own C-8 lessons already
live in `memory/lessons.jsonl` via commit `639ec01954fdd5eb53b86cd465a922e217016e0d` on
`feat/joao-a0-integrite` — the branch this Phase-0 work deliberately does NOT build on
(it builds on `feat/joao-a0-integrite-clean`, which excludes that commit, per the
immediately-preceding Boss GO). This spec does not re-litigate that decision; it simply
notes that "the seven C-8 lessons" as source material already exist in the OTHER
branch's memory, and converting them to gates does not require re-reading that specific
commit — the lesson CONTENT (the seven gate names and one-line claims) was given
verbatim in this instruction and is restated in full in
`JOAO_C8_GATE_CONTRACTS.md`.

## 5. Trust boundaries

Unchanged from A0/A0.2 (`JOAO_WORKER_INTEGRATION_SPEC.md` §14): the trust boundary is
the OS user account running JOÃO, not a process/container boundary. Within that
boundary, this milestone adds one NEW trust distinction: a "risk tier" boundary between
`normal` and `critical` runs (§6), which changes how much independent verification a
run needs before promotion — not a new technical isolation boundary, a POLICY boundary.

## 6. Risk tiers

- **`normal`** (default): bounded, non-security-relevant, single-project missions.
  Requires one independent reviewer (G-DBL-AUDIT).
- **`critical`**: security-relevant, release-relevant, or explicitly Boss-flagged
  missions (mirrors this session's own real practice — CV-SEC-CORE-V2's promotion, and
  A0.2's own closeout, both explicitly treated as higher-scrutiny than an ordinary
  commit). Requires two reviewers from distinct providers (G-DBL-AUDIT), and, where
  mission policy says so, a passing canary before promotion (G-CANARY-FIRST).

Tier assignment is a mission-definition field, set by the Boss-approved SPEC/ROADMAP
reference at GO time (`JOAO_WORKER_INTEGRATION_SPEC.md` §3) — not inferred
automatically by JOÃO. Whether tier assignment should EVER be automatic is a real,
open, Boss-level question — see `JOAO_C8_OPEN_DECISIONS.md`.

## 7. Gate inputs, outputs, and stable reason codes

Fully specified per-gate in `JOAO_C8_GATE_CONTRACTS.md` — not duplicated here.

## 8. Real runtime entrypoint per gate

Summarized (full detail in `JOAO_C8_GATE_CONTRACTS.md`, "Real entrypoint" per gate):

| Gate | Entrypoint |
|---|---|
| G-DBL-AUDIT | `RunRuntime.approve()` |
| G-HERMETIC | autouse pytest session fixture (extends `tests/conftest.py`'s existing pattern) |
| G-AUTH-IO | static test-suite auditor script, run pre-close/CI |
| G-NO-STALE-ENTRYPOINT | static AST-audit script, run pre-close/CI |
| G-SHA-BOUND-PROOF | evidence write path + evidence consumption path (both ends) |
| G-CANARY-FIRST | `RunRuntime.promote()` / `promotion.py`'s acceptance-verification function |
| G-FROZEN-FINISH-LINE | SPEC/ROADMAP layer discipline, mechanically reinforced by G-NO-STALE-ENTRYPOINT + G-DBL-AUDIT's allowlists |

## 9. Builder/reviewer provider identity model

Unchanged mechanism, reused: class-level `provider`/`model` constants on each
`Adapter`, always overwritten into accepted evidence server-side (never trusting a
subprocess's self-reported identity) — see `JOAO_WORKER_INTEGRATION_SPEC.md` §5 and
`reviewer_contract.py`'s existing, A0.2-correctif-5-hardened `validate_reviewer_verdict`.

## 10. GLM/ZCode launch contract

`JOAO_WORKER_INTEGRATION_SPEC.md` §4 (builder, already real) and §2.1 (reviewer, new).

## 11. Codex/GLM reviewer launch contract

`JOAO_WORKER_INTEGRATION_SPEC.md` §5.

## 12. Exact SHA/tree evidence model

`JOAO_WORKER_INTEGRATION_SPEC.md` §6; generalized by G-SHA-BOUND-PROOF
(`JOAO_C8_GATE_CONTRACTS.md`) to cover every evidence artifact this milestone adds, not
only reviewer verdicts.

## 13. State machine transitions

```
ready → building → needs_approval → [G-DBL-AUDIT, G-SHA-BOUND-PROOF] → accepted
      → [G-CANARY-FIRST if required] → promoted
(blocked / paused are existing off-ramps at any point a gate or adapter returns
 ok=false — unchanged from today's RunRuntime)
```
Full orchestration sequence: `JOAO_WORKER_INTEGRATION_SPEC.md` §2.2.

## 14. Correction-loop maximum

2 (`MAX_REPAIR_LOOPS: 2`), reused from every existing run card's own convention — not a
new number.

## 15. Boss approval points

Exactly three: initial GO, mid-run risk-tier escalation, promotion. Detail:
`JOAO_WORKER_INTEGRATION_SPEC.md` §9.

## 16. Failure and recovery behavior

Any gate `ok=false` → `blocked`, never a silent downgrade; recovery is always a NEW run
(new `candidate_tree`), never a patched resume of a blocked one. Detail:
`JOAO_WORKER_INTEGRATION_SPEC.md` §10.

## 17. Immutable candidate/tag/rollback model

Reuses `promotion.py`'s existing atomic promote/rollback plus this session's own
now-established annotated-tag convention. Detail: `JOAO_WORKER_INTEGRATION_SPEC.md`
§11.

## 18. Test strategy, including red→green adversarial tests

Each milestone (see `JOAO_C8_GATES_ROADMAP.md`) ships its own gate's adversarial test
set, following the EXACT pattern this session used for A0.2 correctif 5 and validated
through three independent GLM review rounds: one red→green test per failure mode named
in that gate's "Output / reason codes" list in `JOAO_C8_GATE_CONTRACTS.md`, each
exercising the gate's real entrypoint (per G-AUTH-IO, applied to this milestone's own
development — the same standard this milestone imposes on future work applies to
building it), plus one positive test proving the fully-compliant case is accepted.
Every new test proven, per G-HERMETIC, to depend on nothing outside `tmp_path`/an
injected root — checked by literally running the new hermeticity auditor (M2) against
the new tests introduced in M1 and every milestone after it.

## 19. Acceptance criteria (for this Phase-0 spec itself)

- Exactly the five named files exist on `feat/joao-c8-gates-spec`, nothing else
  changed.
- Exactly seven gates specified, by name, matching the instruction's list exactly — no
  eighth.
- Every required SPEC section (this list, verbatim from the instruction) is present.
- Roadmap is milestone-based with the required per-milestone fields.
- Open decisions contains only genuine Boss-level product questions, not mechanically
  resolvable ones.
- A0.2 (`feat/joao-a0-integrite-clean` @ `cc796b5...`, its tag, its evidence, its
  tests) is provably untouched — `git diff --stat feat/joao-a0-integrite-clean
  feat/joao-c8-gates-spec` shows only the five new files as additions, zero
  modifications to any pre-existing file.

## 20. Migration plan from current A0.2

No migration in the code sense — A0.2's `RunRuntime`/adapters/promotion machinery is
the FOUNDATION this milestone extends, not something replaced. Migration is additive:
M1-M2 add gate checks and hermeticity enforcement around the existing engine; M3 adds
`GLMReviewer`; M4 wires risk-tier-aware reviewer orchestration into `approve()`; M5
adds SHA-bound canary/promotion enforcement into `promote()`; M6 proves the whole chain
on one synthetic end-to-end mission (the direct successor to
`scripts/a0_toy_mission_e2e.py`, extended to exercise gates and both reviewers, not a
replacement of it); M7 is a readiness checklist, not new code, confirming SOURCE-FRESH
can launch through the resulting controller.

## 21. Compatibility with SOURCE-FRESH as the first real product mission

`JOAO_WORKER_INTEGRATION_SPEC.md` §12; expanded in `JOAO_C8_GATES_ROADMAP.md` M7.

## 22. Cost policy: zero incremental paid API cost

`JOAO_WORKER_INTEGRATION_SPEC.md` §13 — reuses existing flat-rate GLM (Z.AI Coding
Plan) and Codex (subscription) access; no new metered provider.

## 23. No recreation of an OS-security boundary

`JOAO_WORKER_INTEGRATION_SPEC.md` §14 — unchanged claim from A0/A0.2.
