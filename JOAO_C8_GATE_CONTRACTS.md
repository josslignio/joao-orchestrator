# JOÃO C-8 GATE CONTRACTS

Phase 0 — spec only, no implementation. Base: `feat/joao-a0-integrite-clean` @
`cc796b55808b95210adf9d219ecd91a240b2676c` (A0.2, `PROVISIONALLY_CLOSED`, frozen —
not touched by this document or this branch).

This file is the machine-checkable contract layer: for each of the exact seven C-8
gates, its inputs, outputs, stable reason codes, and the real runtime entrypoint that
must enforce it (not a helper function tested in isolation — this is G-AUTH-IO's own
requirement applied reflexively to every other gate's own test suite).

Convention: every gate returns a dict shaped like JOÃO's existing reviewer/builder
adapters already do (`{"ok": bool, "decision": "pass"|"block", "reason_code": str,
"reason": str, ...gate-specific fields}`) — this reuses the vocabulary
`execution_backend.py`'s `PREFLIGHT_UNAVAILABLE` and `reviewer_contract.py`'s
`"decision": "block"` already established in A0/A0.1/A0.2, not a new response shape.

---

## G-DBL-AUDIT — risk-tiered reviewer requirement

**Claim:** a `critical`/security/release run cannot reach `approve()` with only one
reviewer's verdict; a normal bounded run needs exactly one; the builder identity can
never equal either reviewer identity.

**Inputs:** `run.risk_tier` (`normal` | `critical`), the list of verdicts collected for
the run's terminal ("final") review stage, each verdict's `reviewer.provider` (the
controller-computed value per `reviewer_contract.py`'s existing "never trust the
reviewer's own claimed identity" rule — unchanged, reused), `run.builder_provider`.

**Output / reason codes:**
- `ok=true` only if: `risk_tier == "normal"` and exactly 1 distinct-from-builder
  reviewer verdict is `ACCEPT`; OR `risk_tier == "critical"` and 2 verdicts from 2
  *distinct* `reviewer.provider` values are both `ACCEPT`.
- `G_DBL_AUDIT_INSUFFICIENT_REVIEWERS` — critical run has fewer than 2 accepted
  verdicts.
- `G_DBL_AUDIT_SAME_PROVIDER` — critical run's two verdicts share a `reviewer.provider`
  (this is the exact defect the current session corrected by hand: a Claude subagent
  standing in for GLM was rejected after the fact by written governance correction —
  G-DBL-AUDIT makes that rejection automatic and pre-emptive instead of a manual Boss
  catch).
- `G_DBL_AUDIT_BUILDER_SELF_REVIEW` — any reviewer's `provider` equals
  `run.builder_provider`.

**Real entrypoint:** `RunRuntime.approve()` (`bubble/runtime.py`) — the one place a run
transitions from `needs_approval` to `accepted`. Not a standalone validator called by a
test; `approve()` itself must call it before flipping status, mirroring how
`promotion.py`'s `_verify_acceptance_for_promotion` already gates `promote()`.

---

## G-HERMETIC — no live/external dependency in default tests

**Claim:** the default test suite (`pytest`, no special markers) resolves zero real
paths outside `tmp_path`/an injected root: no real `memory/lessons.jsonl`, no sibling
repo (`~/job-opportunity-radar*`), no `~/Claude-HQ/DEFECTS_LEDGER.md`, no real
`~/.joao-profile.json`-bearing user workspace.

**Inputs:** the resolved value of every environment-overridable root at test-session
start (`JOAO_MEMORY_DIR` — already exists per A0.2 correctif 7 — plus, if this
milestone adds them, equivalents for any future ledger/spec path); a static/dynamic
inventory of file paths actually opened during a test run.

**Output / reason codes:**
- `ok=true` only if every opened path resolves under an injected/temp root.
- `G_HERMETIC_REAL_MEMORY_TOUCHED` — a test read or wrote outside the isolated
  `JOAO_MEMORY_DIR` copy.
- `G_HERMETIC_EXTERNAL_LEDGER_DEPENDENCY` — a test's outcome depends on a file outside
  this repo and outside `tmp_path` (this is precisely the root cause behind D-044:
  `scripts/build_traceability.py`'s `DEFAULT_LEDGER = Path.home() / "Claude-HQ" /
  "DEFECTS_LEDGER.md"`, and the still-open second finding from this session's own
  history — `test_real_repo_produces_zero_unmapped_with_cv_bot_specs` reading
  `~/job-opportunity-radar/governance/*.yaml` — both would be caught here by
  construction, not discovered by chance during a candidate's closeout).
- `G_HERMETIC_UNINJECTED_ROOT` — a test imported a module whose root constant resolved
  to a real (non-temp, non-explicitly-injected) path.

**Real entrypoint:** a session-scoped `pytest` fixture (autouse), analogous to the
existing `_isolated_joao_memory_dir` fixture in `tests/conftest.py` — but this gate is
about *detecting and failing* an escape, not merely providing an isolated copy; it must
wrap/patch path resolution (or post-hoc audit `strace`-equivalent file-open records) so
a NEW test that forgets to use the injected root fails loudly instead of silently
reading real data, which is exactly the gap `test_real_repo_produces_zero_unmapped_
with_cv_bot_specs` currently falls through (it has its own `pytest.skip()` escape
hatch instead of a systemic guard).

---

## G-AUTH-IO — real entrypoint, not helper-only tests

**Claim:** every gate/correctif's own test suite calls the actual production
entrypoint a real run would exercise (an `Adapter.review()`/`.build()` method, a
`RunRuntime` method, a CLI subprocess boundary) — never only an internal helper
(`validate_reviewer_verdict()` called directly, bypassing `CodexEvidenceReviewer.
review()`) with no test proving the helper is actually wired to the entrypoint in
production.

**Inputs:** for each gate's test file, a static list of the call sites its assertions
depend on.

**Output / reason codes:**
- `ok=true` only if at least one test per gate calls the entrypoint class/function
  identified in that gate's "Real entrypoint" line in this document, not only the
  underlying helper.
- `G_AUTH_IO_HELPER_ONLY_COVERAGE` — a gate has attack tests, but none of them call
  through the real entrypoint (this is exactly A0.2 correctif 5's original defect,
  which GLM's round-1 review caught and which this milestone's own G-AUTH-IO gate is
  named for).
- `G_AUTH_IO_ENTRYPOINT_UNREACHABLE` — the identified real entrypoint cannot be
  constructed/called from a test at all (signals the contract doc is stale or the
  entrypoint was refactored away).

**Real entrypoint:** this gate is enforced by a repo-level *test-suite auditor* (a
script under `scripts/`, run in CI/pre-close, not a runtime gate inside `RunRuntime`
itself) that statically maps each gate's test file to the entrypoint symbols it must
reference — the audit target IS the test suite, so "the real entrypoint" here means the
auditor itself must be invoked as part of the close-out sequence (M1/M5 in the
roadmap), not as a helper a human remembers to run.

---

## G-NO-STALE-ENTRYPOINT — inventory and block legacy/duplicate/unprotected entrypoints

**Claim:** every code path capable of triggering a build, a review, a promotion, or an
artifact read is enumerated exactly once, is protected by the same gate set as its
"canonical" sibling, and no second, older, or forgotten path exists that reaches the
same effect ungated.

**Inputs:** a static inventory (AST-walked, same technique as A0.2's existing
`test_a02_single_dispatch_point_ast_guard_no_direct_subprocess_in_adapters`) of every
callable that can (a) invoke `ExecutionBackend.execute()`, (b) call
`RunRuntime.promote()`/`.approve()`, (c) read `PKG_ROOT`/artifact paths, cross-checked
against a maintained allowlist of the canonical entrypoints this spec names.

**Output / reason codes:**
- `ok=true` only if the inventory's set of dispatch-capable callables exactly equals
  the allowlist.
- `G_NO_STALE_UNLISTED_DISPATCH` — a new callable reaches `ExecutionBackend.execute()`
  (or promotion/artifact-read) without being added to the allowlist — this is the
  generalized, permanent form of A0.2's one-off "single dispatch point" AST guard,
  extended from "adapters only" to "the whole repo."
- `G_NO_STALE_DUPLICATE_PATH` — two distinct callables reach an equivalent effect
  (e.g. two different promotion functions), one of which is not the allowlisted
  canonical path.
- `G_NO_STALE_LEGACY_UNPROTECTED` — an inventoried entrypoint exists that predates a
  gate (e.g. a hypothetical old promote-without-approval-record function surviving
  alongside the new one) and does not itself call the current gate chain.

**Real entrypoint:** a static AST-audit script (`scripts/audit_entrypoints.py`, new —
this milestone's own deliverable), run as a required pre-close/CI step; it does not
live inside `RunRuntime` at request time (there is no "request" for a static-analysis
gate) but its exit code gates M1's own closeout, the same way `git diff --check` gates
this session's candidate closeouts.

---

## G-SHA-BOUND-PROOF — every proof binds to the exact candidate commit/tree

**Claim:** no test-pass record, attack-test output, canary result, or promotion record
is accepted as evidence for a candidate unless it carries that exact candidate's
`candidate_tree` (and, where a promotion has occurred, `candidate_commit`) — inherited
directly from RI-3/RI-4/A0-1's existing `candidate_tree` binding
(`recompute_candidate_tree`, already used by `promotion.py` and both reviewer
adapters), extended to cover EVERY evidence artifact this milestone introduces
(canary results, gate-run logs), not only reviewer verdicts.

**Inputs:** every evidence artifact this milestone produces (gate run logs, canary
result records) plus the `candidate_tree`/`candidate_commit` the run's `RunRuntime`
state currently holds.

**Output / reason codes:**
- `ok=true` only if every evidence artifact's own recorded `candidate_tree` equals the
  independently recomputed tree at the moment that artifact is consumed (same
  "recompute, don't trust the stored value" discipline `CodexCLIReviewer.review_stage`
  already applies to reviewer verdicts).
- `G_SHA_BOUND_MISSING` — an evidence artifact has no `candidate_tree` field at all.
- `G_SHA_BOUND_MISMATCH` — an evidence artifact's recorded tree disagrees with the
  independently recomputed one at consumption time (candidate mutated between
  production and consumption of the proof).
- `G_SHA_BOUND_CROSS_CANDIDATE` — an evidence artifact from one candidate is presented
  as proof for a different candidate (this is precisely the mistake this session's own
  human-in-the-loop process caught by hand three times over: the premature Claude-review
  tag, the memory-contaminated candidate, both discovered only because a human diffed
  SHAs manually — G-SHA-BOUND-PROOF makes that check automatic).

**Real entrypoint:** `RunRuntime`'s evidence-write path (wherever gate/canary results
are persisted to `run_dir`, analogous to `build-review-evidence.json`/
`final-review-evidence.json` today) and the consumption path (whatever reads those
files to decide `approve()`/`promote()`) — both ends, not just one.

---

## G-CANARY-FIRST — promotion blocked until a bounded canary succeeds on the exact candidate

**Claim:** for any run whose policy requires a canary (mirrors this session's own
CV-SEC-CORE and A0.2 canary practice — synthetic-data dry runs before real-data use),
`promote()` refuses if no canary result exists for the exact `candidate_tree` being
promoted, or if that canary's own recorded result is not a pass.

**Inputs:** `run.canary_required` (bool, set by risk tier/mission policy), a canary
result record (same G-SHA-BOUND-PROOF-compliant shape: `candidate_tree`, pass/fail,
raw evidence path).

**Output / reason codes:**
- `ok=true` if `canary_required == false`, OR a matching, passing canary result exists
  for this exact `candidate_tree`.
- `G_CANARY_FIRST_MISSING` — `canary_required == true` and no canary result exists at
  all for this candidate.
- `G_CANARY_FIRST_FAILED` — a canary result exists for this candidate but recorded
  fail.
- `G_CANARY_FIRST_STALE_CANDIDATE` — a canary result exists but for a DIFFERENT
  `candidate_tree` than the one now being promoted (same failure mode
  G-SHA-BOUND-PROOF's `G_SHA_BOUND_CROSS_CANDIDATE` names, applied specifically at the
  promotion boundary).

**Real entrypoint:** `RunRuntime.promote()` / `bubble/promotion.py`'s
`_verify_acceptance_for_promotion` — the exact function A0.2 correctif 8 already hardens
against a missing/tampered approval record; this gate is one more precondition in that
same function, not a new code path.

---

## G-FROZEN-FINISH-LINE — the approved scope/acceptance criteria are frozen

**Claim:** once a mission's scope and acceptance criteria are Boss-approved (the state
this milestone's own `JOAO_C8_GATES_SPEC.md` + `JOAO_C8_GATES_ROADMAP.md` will be, once
approved), no new finding — however real — can silently widen that scope mid-run; a new
finding is either (a) within the frozen acceptance criteria already, and must be fixed
within them, or (b) is logged as an out-of-claim finding for a FUTURE, distinctly
authorized run, exactly as this session repeatedly did by hand (GLM's correctif-5
finding was fixed *because* it was in-scope per the frozen run card's own EVIDENCE_
REQUIRED wording; the memory-contamination finding was explicitly kept OUT of the
correctif-5 fix and disentangled into its own separate concern).

**Inputs:** the frozen scope document's own explicit boundary (this milestone: the
seven named gates, nothing else — "NO EIGHTH GATE" is this gate's own first
self-application), a proposed change/finding under consideration mid-run.

**Output / reason codes:**
- `ok=true` if the proposed change maps to an already-frozen acceptance-criterion line.
- `G_FROZEN_FINISH_LINE_SCOPE_CREEP` — the proposed change does not map to any frozen
  criterion (e.g. an eighth gate, an OS-security claim, a roadmap change proposed
  mid-run).
- `G_FROZEN_FINISH_LINE_REQUIRES_NEW_AUTHORITY` — the change is real and worth doing,
  but requires a new, distinct Boss-authorized run/document — never silent inclusion in
  the current one.

**Real entrypoint:** this is the one gate with no single runtime call site — it is
enforced by discipline at the SPEC/ROADMAP layer plus, mechanically, by
G-NO-STALE-ENTRYPOINT and G-DBL-AUDIT's own allowlists refusing anything not already
named in them. Any implementation milestone (M1+) that finds itself needing to touch a
file outside this document's own "files/modules expected to change" list for that
milestone IS this gate firing, in the same way this exact planning document is
mechanically confined to five specific filenames and nothing else.
