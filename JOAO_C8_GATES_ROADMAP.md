# JOÃO C-8 GATES + WORKER INTEGRATION — ROADMAP

Phase 0 — spec only, this document plans; it implements nothing. Every milestone below
is future work, not started by this branch. Base:
`feat/joao-a0-integrite-clean` @ `cc796b55808b95210adf9d219ecd91a240b2676c` (frozen).

---

## M1 — Gate contracts and reason codes

**Scope:** implement the pure-logic core of all seven gates as importable functions
returning the exact `{"ok", "decision", "reason_code", "reason"}` shape
`JOAO_C8_GATE_CONTRACTS.md` defines — NOT yet wired into `RunRuntime`/`promote()`
(that's M4/M5). This milestone proves the gate logic is correct in isolation before
anything depends on it.

**Files/modules expected to change:** new `bubble/gates.py` (one function per gate);
new `scripts/audit_entrypoints.py` (G-NO-STALE-ENTRYPOINT's static auditor, stub
version — full inventory logic can land in M3 once real entrypoints exist to audit);
new `scripts/audit_test_entrypoints.py` (G-AUTH-IO's static auditor).

**Tests:** one test file, `tests/test_c8_gates.py`, unit-level, per-gate, using
hand-constructed input dicts (no live `RunRuntime` yet).

**Adversarial tests:** one red→green test per reason code in
`JOAO_C8_GATE_CONTRACTS.md` (e.g. `G_DBL_AUDIT_SAME_PROVIDER`,
`G_SHA_BOUND_MISMATCH`, ...) — every reason code in the contract doc must have exactly
one test proving it fires, and one proving the compliant case does not.

**Reviewer requirement:** `normal` tier — one independent reviewer (Codex or GLM, per
whichever adapter M1 itself doesn't touch, to keep the reviewer genuinely independent
of what's being reviewed).

**Acceptance criteria:** all seven gate functions exist, match
`JOAO_C8_GATE_CONTRACTS.md`'s inputs/outputs exactly, every reason code has a passing
red→green test, `pytest tests/test_c8_gates.py` is 100% hermetic (no real
paths touched — self-checked, since G-HERMETIC's own enforcement fixture doesn't exist
until M2).

**Rollback:** delete `bubble/gates.py` + its test file + the two new script stubs;
zero interaction with any existing file, so rollback is a pure file removal, no
`RunRuntime`/`promotion.py` state to unwind.

**Dependencies:** none (first milestone).

**STOP condition:** if any gate's logic cannot be expressed as a pure function of the
inputs `JOAO_C8_GATE_CONTRACTS.md` names (e.g. a gate turns out to need live process
state that can't be captured as a plain input) — stop, do not silently redefine the
gate's contract; that is scope creep requiring the same Boss-level
correction process this session's own A0.2 work went through, not a unilateral spec
change.

---

## M2 — Hermetic execution and frozen fixtures

**Scope:** implement G-HERMETIC for real: an autouse pytest fixture (extending
`tests/conftest.py`'s existing `_isolated_joao_memory_dir` pattern to cover EVERY
external root, not only memory) plus a path-open auditor that fails a test loudly if it
resolves a real path outside `tmp_path`/an injected root. Applied first to the
EXISTING full suite (`pytest`, no gates yet) to retroactively catch and document
`test_real_repo_produces_zero_unmapped_with_cv_bot_specs`'s known external-ledger
dependency (D-044) as a NAMED, gate-flagged case — not fixed (D-044 stays open, per
every prior instruction in this session forbidding masking it), but now caught by a
systemic gate instead of only by a human reading full-suite output line by line.

**Files/modules expected to change:** `tests/conftest.py` (extend the isolation
fixture); new `tests/test_g_hermetic_self_check.py` (the gate auditing the suite
itself); no production code changes — this milestone is test-infrastructure only.

**Tests:** `test_g_hermetic_self_check.py` — asserts the fixture's path-audit catches a
deliberately-planted "reads a real path" test double, and separately reports (not
fails the build on) the currently-known D-044 external dependency by name.

**Adversarial tests:** a fixture-scoped test that plants a fake test reading
`Path.home() / "something"` and asserts G-HERMETIC's auditor flags it with
`G_HERMETIC_EXTERNAL_LEDGER_DEPENDENCY`-shaped output before that fake test is removed
again (must not leak into the real suite).

**Reviewer requirement:** `normal` tier.

**Acceptance criteria:** running `pytest` with the new fixture active produces the SAME
pass/fail counts as before (329 passed / 1 failed, per the clean A0.2 candidate's own
last full-suite run) PLUS a new, separate, non-failing report naming D-044 as a known
G-HERMETIC exception — the gate must not turn a currently-accepted, disclosed
limitation into a new hard failure; it documents it.

**Rollback:** revert `tests/conftest.py`'s extension; delete the new self-check test
file. No promotion/runtime state involved.

**Dependencies:** M1 (reuses its gate-result shape for the report).

**STOP condition:** if enforcing G-HERMETIC strictly would require deleting or
skipping D-044's own test rather than reporting it — stop; that is "modifying D-044,"
explicitly forbidden by every instruction in this session so far, and requires Boss
adjudication, not a silent test change.

---

## M3 — Real worker launch integration

**Scope:** implement `GLMReviewer(ReviewerAdapter)` per
`JOAO_WORKER_INTEGRATION_SPEC.md` §2.1. Fill in `scripts/audit_entrypoints.py`'s real
inventory logic (G-NO-STALE-ENTRYPOINT) now that `GLMReviewer` exists as a second
dispatch-capable class to audit alongside `GLMBuilder`/`CodexCLIReviewer`.

**Files/modules expected to change:** `bubble/runtime.py` (add `GLMReviewer` class);
`tests/test_a0_2_corrections.py`'s AST-guard allowlist (`_GUARDED_METHODS`) — add
`"GLMReviewer": {"review_stage"}` (this is the one place M3 touches a file also touched
by A0.2's own tests; it is an ADDITION to an allowlist dict, not a modification of any
A0.2 assertion or expected count — full diff must be reviewed against
G-FROZEN-FINISH-LINE before landing); new `tests/test_c8_glm_reviewer.py`;
`scripts/audit_entrypoints.py` (fill in real logic).

**Tests:** `test_c8_glm_reviewer.py` — using a stubbed/fake `~/.local/bin/joao-glm`
executable (per the existing `_RecordingBackend`/fake-executable patterns already used
in `test_a0_2_corrections.py`), proving `GLMReviewer.review_stage` binds to
`candidate_tree` before/after dispatch exactly like `CodexCLIReviewer` does, and a
SEPARATE, explicitly-marked `live_canary`-style test (not run by default `pytest`,
matching this repo's own `addopts`-equivalent convention from the CV-SEC-CORE-V2
sibling project) that exercises the REAL `~/.local/bin/joao-glm` binary once, manually
invoked, the same way this session did three times by hand.

**Adversarial tests:** candidate-tampered-mid-review (reuses `CodexCLIReviewer`'s own
proven tamper-detection test shape, applied to `GLMReviewer`); dispatch-failure
fail-closed (GLM CLI missing/erroring → `block`, never a silent pass).

**Reviewer requirement:** `critical` tier — this milestone adds a NEW live-dispatch
capability, itself needs Codex to review it since GLM cannot yet review its own new
integration path with the required independence.

**Acceptance criteria:** `GLMReviewer` passes the same tamper/fail-closed test shapes
`CodexCLIReviewer` already has; `scripts/audit_entrypoints.py` run against the repo
reports zero unlisted dispatch-capable callables; one real (non-mocked) manual GLM
reviewer dispatch succeeds against a toy candidate, evidenced the same way this
session's own `GLM_RUN_EVIDENCE.json` captures were.

**Rollback:** remove the `GLMReviewer` class and its allowlist entry; delete its test
file; `scripts/audit_entrypoints.py` reverts to M1's stub. No `RunRuntime` default
behavior changes (no reviewer is auto-selected until M4), so nothing else can regress.

**Dependencies:** M1 (gate shapes), M2 (hermetic test fixture must cover the new test
file).

**STOP condition:** if the real `joao-glm` binary's CLI contract has drifted from what
`JOAO_WORKER_INTEGRATION_SPEC.md` §2.1 assumes (verified this session, but time may
pass before M3 implementation) — stop and re-verify against the live binary before
writing `GLMReviewer`, do not implement against a stale assumption.

---

## M4 — Independent reviewer orchestration

**Scope:** wire G-DBL-AUDIT into `RunRuntime.approve()`: risk-tier-aware reviewer
selection (one of {GLM, Codex} for `normal`, both distinct providers for `critical`),
builder-identity-cannot-equal-reviewer-identity check.

**Files/modules expected to change:** `bubble/runtime.py` (`approve()` gains the
G-DBL-AUDIT check, reusing `bubble/gates.py`'s M1 function — not reimplementing the
logic inline); `tests/test_bubble_runtime.py` (new tests only — ADDING test functions,
never editing an existing assertion, per G-FROZEN-FINISH-LINE applied to this
milestone's own development discipline).

**Tests:** normal-tier single-reviewer accept/reject; critical-tier two-distinct-
providers accept; critical-tier same-provider-twice reject
(`G_DBL_AUDIT_SAME_PROVIDER`); builder-equals-reviewer reject
(`G_DBL_AUDIT_BUILDER_SELF_REVIEW`).

**Adversarial tests:** a critical run where BOTH reviewer slots are filled by
`GLMReviewer` instances (misconfiguration, not malice) must still refuse via
`G_DBL_AUDIT_SAME_PROVIDER` — proves the check is on `provider` identity, not merely
"two calls happened."

**Reviewer requirement:** `critical` tier (this milestone IS G-DBL-AUDIT's own
enforcement point — dogfood the rule being added).

**Acceptance criteria:** `approve()`'s existing passing tests
(`test_nominal_run_evidence_and_approval` etc.) still pass unmodified; new tests above
all green; a run whose `risk_tier` is unset defaults to `normal` (fail-safe default,
never silently `critical`-strength-required-but-unenforced, and never silently
`normal` when the mission scope says `critical` — the DEFAULT when genuinely unset is
the open question in `JOAO_C8_OPEN_DECISIONS.md`).

**Rollback:** the G-DBL-AUDIT check in `approve()` is one additional guarded block;
revert it, `approve()`'s behavior returns to today's (single implicit reviewer,
unchanged pre-M4 tests still pass since they were never edited).

**Dependencies:** M1, M3 (needs `GLMReviewer` to exist for the two-distinct-providers
case to be testable with a real second adapter, not only Codex).

**STOP condition:** if any EXISTING test in `test_bubble_runtime.py` needs to change
(not just gain new siblings) to pass after G-DBL-AUDIT lands — stop; that signals
G-DBL-AUDIT's default risk tier assumption is wrong and needs Boss adjudication before
proceeding, not a silent edit to a previously-green test.

---

## M5 — SHA-bound evidence and canary/promotion enforcement

**Scope:** implement G-SHA-BOUND-PROOF generally (evidence write/consumption
recompute-and-compare) and G-CANARY-FIRST in `promotion.py`'s
`_verify_acceptance_for_promotion`.

**Files/modules expected to change:** `bubble/promotion.py` (new precondition
functions, called from the existing verification chain — not a rewrite of it);
`bubble/gates.py` (G-SHA-BOUND-PROOF, G-CANARY-FIRST implementations, filling in what
M1 stubbed); new `tests/test_c8_canary_promotion.py`.

**Tests:** promote refuses with no canary result when `canary_required=true`
(`G_CANARY_FIRST_MISSING`); promote refuses a stale-candidate canary
(`G_CANARY_FIRST_STALE_CANDIDATE`); promote succeeds with a matching passing canary;
an evidence artifact with a mismatched tree is refused
(`G_SHA_BOUND_MISMATCH`) at both a build-review and a canary-result consumption point.

**Adversarial tests:** a canary result for candidate A presented at promotion time for
candidate B (the exact cross-candidate-evidence mistake this session's own history
made twice by hand and caught only through manual SHA diffing) must be refused
mechanically.

**Reviewer requirement:** `critical` tier (promotion-path changes are inherently
higher-risk).

**Acceptance criteria:** `promotion.py`'s existing passing tests
(`test_a02_7_promote_refuses_...`) unmodified and still green; all new tests above
green; `scripts/a0_toy_mission_e2e.py` (or its M6 successor) still passes end-to-end
with the new preconditions active for a NON-canary-required toy mission (canary
requirement itself is a `critical`-tier-only default, so the existing toy E2E, which is
`normal`, must be unaffected).

**Rollback:** the two new preconditions in `_verify_acceptance_for_promotion` are
additive checks; revert them and `promote()` returns to pre-M5 behavior. No candidate
data model changes, so no migration needed either direction.

**Dependencies:** M1, M4 (canary requirement is itself a risk-tier concept M4
introduces).

**STOP condition:** if implementing G-CANARY-FIRST requires defining what a "canary" IS
in code (a new run type? a flag on an existing run?) in a way not already implied by
this session's own actual canary practice (synthetic-data dry run, human-observed,
against the exact candidate) — stop and raise it as an open decision rather than
inventing a canary data model unilaterally.

---

## M6 — End-to-end synthetic product mission

**Scope:** one full, real, end-to-end run through the ENTIRE assembled chain (Boss GO →
orchestration controller §2.2 of `JOAO_WORKER_INTEGRATION_SPEC.md` → build → gates →
freeze → both reviewers (critical tier, to exercise G-DBL-AUDIT for real) → canary →
promotion) on a synthetic, harmless toy mission — the direct successor to
`scripts/a0_toy_mission_e2e.py`, extended rather than replaced.

**Files/modules expected to change:** new `scripts/c8_e2e_synthetic_mission.py`
(extends the existing toy-mission pattern); the orchestration controller module named
in `JOAO_C8_GATES_SPEC.md` §4 (first real implementation, M3/M4/M5's pieces assembled).

**Tests:** the script itself IS the test, run manually and its raw trace captured as
evidence — same evidentiary standard as every canary this session ran (raw commands,
raw output, before/after hashes, exit codes).

**Adversarial tests:** the same E2E run repeated with one gate deliberately misconfigured
per run (missing second reviewer on a critical mission, missing canary result before
promotion, tampered candidate mid-review) — each must halt at the correct gate with the
correct reason code, proving the assembled chain, not just each gate in isolation.

**Reviewer requirement:** `critical` tier (dogfoods G-DBL-AUDIT on the milestone that
makes G-DBL-AUDIT real end-to-end for the first time).

**Acceptance criteria:** the happy-path run succeeds fully (build → both reviewers
GO → canary pass → promotion), matching `scripts/a0_toy_mission_e2e.py`'s existing
"SAME HASH TRACED END TO END" proof, extended to include both reviewers' verdicts and
the canary result in that trace; each of the deliberately-misconfigured adversarial
runs halts at its expected gate with its expected reason code; the live workspace and
real `memory/lessons.jsonl` are proven byte-identical before/after, exactly as every
prior E2E run in this session's own evidence already demonstrates is achievable.

**Rollback:** delete the new script; the orchestration controller module reverts to
whatever state M3-M5 individually left it in (each of those milestones is independently
rollback-able per their own rollback sections).

**Dependencies:** M1-M5 (this milestone assembles all of them).

**STOP condition:** if the happy-path run cannot achieve the same "SAME HASH TRACED END
TO END: True" proof `scripts/a0_toy_mission_e2e.py` already achieves today — stop; a
regression in that specific, already-working guarantee is not acceptable collateral
damage from adding gates around it.

---

## M7 — SOURCE-FRESH readiness gate

**Scope:** NOT SOURCE-FRESH implementation (forbidden). A readiness checklist/report
confirming the M1-M6 chain can launch a `normal`-tier mission end-to-end with no
SOURCE-FRESH-specific code required — i.e., proving compatibility, not building the
feature.

**Files/modules expected to change:** one new document,
`JOAO_C8_SOURCE_FRESH_READINESS_REPORT.md` (report only — no `.py` files; if writing
this report reveals a genuine gap requiring new code, that code is OUT of M7's own
scope and becomes a new, separately-authorized milestone, per G-FROZEN-FINISH-LINE).

**Tests:** none new — M7 re-runs M6's synthetic E2E once more with SOURCE-FRESH's
KNOWN mission shape (a `normal`-tier, non-critical, no-canary-required mission, per
this session's own understanding of SOURCE-FRESH's place in the roadmap) substituted
for the toy greeting mission, to prove the SAME chain handles a differently-shaped
mission without code changes.

**Adversarial tests:** none new — this milestone is a proof, not a new capability.

**Reviewer requirement:** `normal` tier (a report, not new dispatch-capable code).

**Acceptance criteria:** the readiness report explicitly answers, with evidence: can
SOURCE-FRESH launch through the M6 controller today, unmodified? If yes: cite the
passing run. If no: name exactly what's missing, and stop there — M7 does not build
the missing piece itself.

**Rollback:** delete the report. Zero code involved.

**Dependencies:** M1-M6.

**STOP condition:** if answering M7's own readiness question requires writing ANY
SOURCE-FRESH-specific code to find out — stop immediately; that is SOURCE-FRESH
implementation happening under M7's name, exactly what this milestone and this entire
Phase-0 spec forbid.
