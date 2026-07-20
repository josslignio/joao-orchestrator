# JOÃO C-8 GATES + WORKER INTEGRATION — OPEN DECISIONS (Boss-level only)

Phase 0. Every item below is a genuine product/policy choice this spec cannot resolve
by engineering judgment alone — each has real, defensible arguments on more than one
side, and the "wrong" choice isn't a bug, it's a different product. Anything
mechanically resolvable (naming, exact function signatures, which file a class lives
in) is decided directly in `JOAO_C8_GATES_SPEC.md` / `JOAO_C8_GATE_CONTRACTS.md` /
`JOAO_WORKER_INTEGRATION_SPEC.md` and is NOT repeated here.

---

## D1 — Default risk tier when a mission omits it

`JOAO_C8_GATES_ROADMAP.md` M4 flags this without resolving it. Options:
- **Fail-safe-permissive:** unset defaults to `normal` (one reviewer). Matches how most
  of this session's own work actually ran (a human decided criticality by judgment,
  case by case, not by a mandatory field).
- **Fail-closed:** JOÃO refuses to start any run with no explicit `risk_tier` at all,
  forcing the Boss (or the approved SPEC/ROADMAP reference) to always declare it.

This is a real usability-vs-safety tradeoff, not a technical question — both are
implementable identically cheaply.

## D2 — Should risk-tier assignment ever be inferred automatically?

E.g., a mission whose scope touches `bubble/promotion.py` or credential-adjacent paths
auto-escalates to `critical` even if the mission definition said `normal`. Pro: catches
a human's under-declaration by mistake (the same class of gap G-DBL-AUDIT itself exists
to catch for reviewer identity). Con: an inference heuristic can itself be wrong or
gamed, and this milestone's own stated principle (`JOAO_COURSE_CORRECTION_20260719_1.md`
D-046: "JOÃO est un CONTROL PLANE ... PAS un moteur de sécurité OS") leans toward NOT
building heuristic security judgment into JOÃO itself. Needs a Boss call on which
principle wins here.

## D3 — Canary definition and observation requirement for REAL (non-toy) missions

`JOAO_C8_GATES_ROADMAP.md` M5's own STOP condition flags this. This session's actual
canary practice (CV-SEC-CORE-V2, A0.2's own E2E) was always synthetic-data, one-shot,
and effectively human-observed in real time. For a REAL product mission's canary: must
a human/agent session be actively watching (as every canary this session ran was), or
can G-CANARY-FIRST accept an unattended, automated canary run as sufficient proof? This
determines whether "canary" in the gate sense is a technical check or also an
organizational/observation requirement — a product decision, not an engineering one.

## D4 — Reviewer disagreement resolution on a critical run

If `GLMReviewer` and `CodexCLIReviewer` disagree (one ACCEPT, one BLOCK/P1) on a
`critical` run, G-DBL-AUDIT as currently specified simply refuses (both must accept).
Is that the permanent policy, or should disagreement trigger an escalation path (e.g.
automatic Boss notification with both verdicts, a designated third reviewer, or a
defined tie-break rule)? This session's own history has no precedent for genuine
reviewer disagreement (every GLM/Claude verdict this session obtained either agreed or
was treated as authoritative on its own) — this is a real gap in lived experience to
design against, not something inferable from precedent.

## D5 — Permanence of the "Boss GO = named instruction text" authority model

`JOAO_WORKER_INTEGRATION_SPEC.md` §3 explicitly chooses zero-new-mechanism (reusing the
existing named-instruction convention) for THIS milestone, citing the cost policy. Is
that acceptable as the PERMANENT authority model once the orchestration controller runs
with less continuous human presence than this session had, or is it explicitly an MVP
shortcut that a later milestone must replace with something more binding (a signed
approval record, a separate approval store)? Needs a Boss decision on how long the
current model is trusted to scale.

## D6 — Should the orchestration controller ever run unattended (no active human/agent
   session) between Boss GO and the next Boss approval point?

`JOAO_COURSE_CORRECTION_20260719_1.md`'s own decision 6 explicitly defers "autonomie
nocturne" (nightly/unattended autonomy) until after M4 (10 real missions, measured).
This milestone's M3-M6 build the machinery that WOULD make unattended operation
possible between the three Boss-approval points named in
`JOAO_WORKER_INTEGRATION_SPEC.md` §9 — but whether it is actually ALLOWED to run that
way during THIS milestone's own life (vs. requiring a human/agent to stay attached
throughout, as every run in this repo's history so far has had) is a policy choice this
spec deliberately leaves to the Boss, since the course-correction document's own
existing stance on nightly autonomy is ambiguous about whether it covers this narrower
case.

## D7 — Retention/lifecycle of superseded candidates and provisional tags

This session alone produced two provisional tags for A0.2 (one superseded, one
current) and one fully-deleted premature tag, all within a few hours, all requiring a
human to notice the contamination and correct it by hand. At the scale seven gates +
real worker orchestration will produce, should JOÃO maintain an automatic
retention/cleanup policy for superseded candidates and their tags/evidence (e.g.
auto-archive after N days, or after a successor is promoted), or does every such
cleanup remain a manual, Boss-reviewed action indefinitely (matching this session's own
practice throughout)? A genuine operational-policy tradeoff, not resolvable by
engineering default.
