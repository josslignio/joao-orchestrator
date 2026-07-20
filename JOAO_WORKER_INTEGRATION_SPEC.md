# JOÃO WORKER INTEGRATION SPEC — builder + reviewer orchestration

Phase 0 — spec only. Base: `feat/joao-a0-integrite-clean` @
`cc796b55808b95210adf9d219ecd91a240b2676c` (frozen, untouched).

## 1. Current-state architecture (verified against the real source this session
   already read in full, not assumed)

| Role | Class | File | Real dispatch mechanism | Status |
|---|---|---|---|---|
| Builder (sandbox/test fixture) | `SandboxBuilder` | `bubble/runtime.py` | direct Python callable, no subprocess | done, used by every existing test |
| Builder (real GLM) | `GLMBuilder` | `bubble/runtime.py:274` | `~/.local/bin/joao-glm --mode workspace-write` → `opencode run --agent build` → Z.AI `zai-coding-plan/glm-4.5-air` | **done and REAL** — this session used the read-only sibling of this exact adapter live, twice, for the A0.2 GLM reviews |
| Reviewer (imported proof) | `CodexEvidenceReviewer` | `bubble/runtime.py:318` | reads `run_dir/review-import.json` off disk, calls `reviewer_contract.validate_reviewer_verdict` | done, is the DEFAULT reviewer when none is injected |
| Reviewer (real Codex, live dispatch) | `CodexCLIReviewer` | `bubble/runtime.py:335` | `codex exec --json --sandbox read-only -C <candidate readonly_copy> <prompt>` via `ExecutionBackend.execute()` | **done and REAL** — not a stub |
| Reviewer (real GLM, live dispatch) | *(none)* | — | — | **MISSING — this is the actual integration gap.** This session's own A0.2 correctif-5 GLM reviews (rounds 1-3) called `~/.local/bin/joao-glm --mode read-only` directly, by hand, entirely OUTSIDE `RunRuntime`/`ReviewerAdapter`. That is the exact ad-hoc pattern this milestone formalizes into a real adapter class. |
| Orchestration entrypoint (Boss GO → promotion) | *(none)* | — | — | **MISSING.** `RunRuntime.start()`/`.run_once()`/`.approve()`/`.promote()` all exist and are individually real, but nothing today reads an approved SPEC/ROADMAP, decides risk tier, launches the right builder/reviewer combination, and walks a run end-to-end unattended. Every run in this repo's history (A0/A0.1/A0.2, this session's own canaries) has been driven by a human (Boss, via a run card + an agent session) calling these pieces by hand. |

**What this spec does NOT claim:** that GLM-as-builder or Codex-as-reviewer are
theoretical/future work — they are real, running code, proven by this session's own
use of the read-only sibling of `GLMBuilder`'s exact dispatch path. The gap is
narrower than "connect the workers" — it is specifically: (a) a `GLMReviewer` adapter
class, and (b) an orchestration layer that currently only exists as a human following a
run card by hand.

## 2. Target architecture

### 2.1 `GLMReviewer(ReviewerAdapter)` — new class, `bubble/runtime.py`

Mirrors `CodexCLIReviewer` exactly, substituting the dispatch target:

- `provider = "zai-coding-plan"`, `model = os.environ.get("JOAO_GLM_MODEL",
  "zai-coding-plan/glm-4.5-air")` (reuses `GLMBuilder`'s own default, not a new
  constant).
- `executable: Path = Path("~/.local/bin/joao-glm").expanduser()` (the SAME binary
  `GLMBuilder` already calls, in `--mode read-only` instead of `--mode
  workspace-write`).
- `review_stage(run, run_dir, stage, active_rules="")`: same candidate-binding
  discipline as `CodexCLIReviewer.review_stage` (recompute `candidate_tree` before AND
  after dispatch for `build`/`final` stages; `plan` stage reviews the live workspace,
  no candidate yet) — this is not new design, it is copying an already-audited pattern
  verbatim, per G-AUTH-IO (the real entrypoint for THIS gate's own tests must be
  `GLMReviewer.review_stage`, not a bare call to the `joao-glm` CLI from a test).
  Builds the task-file contract this session's own `task.md` files already
  demonstrated works reliably (frozen authority doc + candidate identity + scoped diff
  + raw evidence + explicit "prior verdict does not count" framing + an exact required
  output block) — that pattern is reusable machinery, not bespoke prose to reinvent
  per run.
- Dispatch: `self.backend.execute([str(self.executable), "--workspace", ..., "--mode",
  "read-only", ...], ...)` through `ExecutionBackend.execute()` — same single-dispatch-
  point discipline A0.2 correctif 2/12.2 already enforces for `GLMBuilder`/
  `CodexCLIReviewer`; `GLMReviewer` must be added to the AST guard's `_GUARDED_METHODS`
  allowlist (`test_a02_single_dispatch_point_ast_guard_...`), which is itself an
  instance of G-NO-STALE-ENTRYPOINT's inventory requirement.
- Parses `joao-glm`'s own JSON-lines stdout (the same shape this session captured in
  `GLM_RAW_STREAM.jsonl`/`GLM_RUN_EVIDENCE.json`) to extract the final text block, then
  runs it through `reviewer_contract.validate_reviewer_verdict` exactly like
  `CodexCLIReviewer` does — no second, divergent parser.

### 2.2 Orchestration controller — new module, `bubble/orchestrator.py` (name TBD at
  implementation time; not created by this Phase-0 branch)

A thin state-machine driver, NOT a rewrite of `RunRuntime` (G-FROZEN-FINISH-LINE: this
spec's scope is orchestrating existing primitives, not rebuilding the engine — the same
boundary A0.2's own run card already drew: "ne pas reconstruire le moteur"):

```
Boss GO (a signed/approved SPEC+ROADMAP reference, see §3)
  → load_approved_spec_and_roadmap(ref) -> mission, risk_tier, canary_required
  → RunRuntime.start(...)                                   [existing]
  → RunRuntime.run_once(...)  [build + targeted/full tests]  [existing]
  → G-HERMETIC / G-NO-STALE-ENTRYPOINT checked as part of the test run itself
    (pytest session, not a separate controller step)
  → candidate_tree frozen (RunRuntime already does this — RI-3)
  → launch reviewer(s) per G-DBL-AUDIT's risk-tier rule:
      normal   -> one of {GLMReviewer, CodexCLIReviewer}
      critical -> BOTH, distinct providers required
  → RunRuntime.approve(...)  [existing, gains G-DBL-AUDIT + G-SHA-BOUND-PROOF checks]
  → if canary_required: run canary against this exact candidate_tree,
    record G-CANARY-FIRST-compliant result
  → RunRuntime.promote(...)  [existing, gains G-CANARY-FIRST precondition]
  → Boss approval requested ONLY at: initial GO, any risk-tier escalation
    mid-run, and promotion itself — never at intermediate mechanical steps
```

The controller's own job is narrow: read a mission definition, pick the reviewer
combination, and call `RunRuntime` methods in the right order with the right gate
checks activated — it does not reimplement anything `RunRuntime`/`promotion.py`/
`execution_backend.py` already do correctly.

## 3. Boss GO / approved SPEC+ROADMAP contract

"Boss GO" is not a new authentication mechanism — it is the same pattern already used
throughout this session and its predecessors: an explicit, dated, human-authored
instruction naming an exact scope document. This milestone's controller formalizes it
as: a mission definition file (path + git SHA it was approved at) that the controller
refuses to act on unless a human has referenced it by name in the invoking instruction
— mirroring exactly how this session only executed A0.2 correctif-5 work after an
explicit "BOSS GO — A0.2 CORRECTIF #5 ONLY" naming the base SHA, and refused to
implement C-8 gates or touch A0.2 until this exact instruction explicitly authorized
Phase 0 planning. No cryptographic signature scheme is introduced (out of scope, zero
incremental cost per this spec's own cost policy in `JOAO_C8_GATES_SPEC.md`) — the
"signature" is the human instruction text itself, logged in the run's evidence the same
way this session's own `A0_2_GOVERNANCE_CORRECTION_20260720.md` preserved the Boss's
exact words as the authority for its own actions.

## 4. GLM/ZCode launch contract (builder)

Already real (`GLMBuilder`, §1) — this milestone's only change is making its dispatch
observable to the orchestration controller's gate checks (capabilities/network
already flow from frozen mission scope per A0.2 correctif 1/2, unchanged) and ensuring
`set_capabilities()` is called by the controller at the same point `RunRuntime._execute`
already calls it today — no change to `GLMBuilder` itself.

## 5. Codex/GLM reviewer launch contract

- **Codex:** already real (`CodexCLIReviewer`, §1) — unchanged.
- **GLM:** new (`GLMReviewer`, §2.1) — built on the exact, already-proven-live
  `~/.local/bin/joao-glm --mode read-only` path this session used three times with
  zero unauthorized file changes each time (`changed_paths_by_task=[]`,
  `unauthorized_paths=[]`, verified via the adapter's own evidence.json every time).
- **Provider identity:** both adapters' `provider`/`model` are class-level constants
  overwritten into the accepted `proof.reviewer` field the same way A0.2 correctif 5
  now enforces structurally (never trust the subprocess's own claimed identity) — no
  new identity model, direct reuse.

## 6. Exact SHA/tree evidence model

Direct reuse of RI-3 (`recompute_candidate_tree`) and A0.2 correctif 7/8's existing
patterns — see `JOAO_C8_GATE_CONTRACTS.md`'s G-SHA-BOUND-PROOF for the generalization
to canary/gate-run evidence specifically. No new hashing scheme.

## 7. State machine transitions

`RunRuntime`'s existing states are unchanged: `ready → building → needs_approval →
accepted → promoted` (and `blocked`/`paused` off-ramps, per existing tests in
`test_bubble_runtime.py`). This milestone adds gate CHECKS at existing transition
points (`approve()` gains G-DBL-AUDIT + G-SHA-BOUND-PROOF; `promote()` gains
G-CANARY-FIRST), not new states.

## 8. Correction-loop maximum

Reuses the existing repo-wide convention already stated in every run card this session
touched (`MAX_REPAIR_LOOPS: 2`) — not a new number invented for this milestone.

## 9. Boss approval points

Exactly three, per §2.2's flow: (a) initial GO naming the approved SPEC/ROADMAP
reference, (b) any risk-tier escalation discovered mid-run (e.g. a normal run's builder
touches a path the frozen scope marks `critical`), (c) promotion itself. No approval
gate is inserted at intermediate mechanical steps (build complete, tests green,
individual reviewer verdict) — those remain autonomous, matching how this session's own
canary/promotion work never asked for Boss confirmation between, say, "tests passed"
and "starting the E2E."

## 10. Failure and recovery behavior

Any gate returning `ok=false` halts the run at `blocked`, exactly as
`CodexEvidenceReviewer`'s existing "no proof imported" path already does today (see
`test_dirty_secret_and_unreviewed_approval_are_refused`) — never a silent downgrade to
a passing state. Recovery is always: fix the underlying cause, start a NEW run (new
`candidate_tree`) — never resume/repair a blocked run's own frozen candidate, matching
A0.2 correctif 6's "no partial package ever becomes downloadable"-equivalent discipline
already proven in the CV-SEC-CORE-V2 lineage this session also worked on.

## 11. Immutable candidate / tag / rollback model

Direct reuse: `promotion.py`'s existing atomic promote/rollback
(`scripts/a0_toy_mission_e2e.py`'s own E2E already proves this end-to-end, 7/7 green,
every time this session ran it) plus this session's own now-established tagging
convention (`joao-a0.2-provisionally-closed-clean-20260720`-style annotated tags,
pointing at an exact commit, never a moving branch tip, created only after an
independent reviewer's GO). No new mechanism — codifying what this session already did
by hand three times as the milestone's own acceptance bar.

## 12. Compatibility with SOURCE-FRESH as the first real product mission

SOURCE-FRESH is not started or designed by this document (forbidden, explicitly). This
spec's only SOURCE-FRESH-relevant claim: once M1-M7 (see roadmap) land, SOURCE-FRESH
becomes launchable as an ordinary `normal`-risk-tier mission through the orchestration
controller — no SOURCE-FRESH-specific code path is anticipated or required in this
milestone's own scope.

## 13. Cost policy

Zero incremental paid API cost: `GLMReviewer` reuses the existing Z.AI Coding Plan
subscription `GLMBuilder` already uses (flat-rate, not metered-per-call, per
`~/.local/bin/joao-glm`'s own `zai-coding-plan` provider naming), `CodexCLIReviewer`
reuses the existing Codex CLI subscription already authenticated via `~/.codex`. No new
provider account, API key, or metered endpoint is introduced.

## 14. No OS-security boundary recreation

Unchanged from A0/A0.2's own bounded claim (`JOAO_RUN_CARD_A0_2_BYPASSES_1.md`'s "LA
CLAIM A0"): this milestone's gates prevent accidental errors, known bypasses, fake
PASS, candidate mutation, unauthorized promotion, and evident secret leaks — not a
boundary against a hostile process with the same OS user's rights. Strong isolation
remains deferred to a `container`/`vm` `ExecutionBackend`, unimplemented, exactly as
today (`PREFLIGHT_UNAVAILABLE` fail-closed, unchanged).
