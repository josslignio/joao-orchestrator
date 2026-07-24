# JOÃO Master Specification V5 — Candidate

**Version:** 5.0.0-candidate  
**Date:** 2026-07-24  
**Runtime authority:** **NO** — `SYSTEM_CONSTITUTION_V4.md` and its signed bundle remain authoritative until an explicit Boss GO activates V5.  
**Current implementation base:** Run Night candidate `01866310a50658626a7352c6f8e5bd9ba87fa601`.

## 1. Product definition

JOÃO is a security-first control plane for coordinating multiple AI systems across software engineering, product strategy, research and repeatable business operations. It is not a chatbot wrapper. It provides identity-aware provider routing, bounded execution, exact-SHA evidence, independent review, measurable provider performance and human-controlled promotion.

## 2. Primary outcome

The Boss uses one interface to:

1. select a project and objective;
2. ask one or several AI families to reason independently;
3. run structured challenge or council workflows;
4. assign a builder and an independent reviewer;
5. execute tests and exact-SHA audits;
6. compare speed, quota use, quality and regression rates;
7. run approved milestones overnight;
8. receive candidates and evidence awaiting human approval.

## 3. Non-negotiable invariants

- GLM/Z Code or Claude Code may construct; they never self-validate.
- Codex performs the last independent exact-SHA code review before merge or promotion.
- GPT may block on architecture, security, evidence or semantic correctness.
- A provider identity is accepted only when proven by authoritative backend output.
- Every write-capable action is limited to an isolated worktree and an explicit allowlist.
- SEC-BOOT stays ON until Candidate Build receives its own signed activation.
- Raw prompts, provider transcripts, credentials and unrestricted local files are never persisted in evidence.
- No automatic merge, push, deployment, publication, application, email or irreversible external action.
- A red gate stops the affected milestone. Reports cannot convert failure into PASS.
- Signed governance bundles are immutable. Changes require a new version and activation record.

## 4. Current capability levels

### L1 — Supervised read-only cockpit

Implemented foundations:

- project-aware chat and local truthful capability answers;
- Supervisor Core;
- provider bridge with mechanical read-only enforcement;
- exact provider/model identity checks;
- challenge, council and builder/reviewer contracts;
- metadata-only evidence boundaries;
- Codex exact-SHA red-team gate;
- supervised live marker gate.

### L2 — Run Night read-only

Candidate implementation:

- authenticated, expiring, one-use activation;
- exact evidence and closure binding;
- sequential bounded tasks;
- watchdog and manual stop;
- Git working-tree/control-plane mutation detection;
- structured NightArtifact validation;
- independent reviewer requirement;
- morning report with all outputs `AWAITING_APPROVAL`.

### L3 — Provider Mesh and Control Room

Next implementation:

- certified GPT, Codex, Claude, Claude Code and GLM/Z Code adapters;
- Chrome Pilot as a browser operator, never as exact-SHA authority;
- visible provider identity, availability, quota, latency and role;
- Direct, Auto, Challenge, Council and Builder/Reviewer modes;
- runs, gates, evidence, stop/pause and budgets in one interface.

### L4 — Benchmark and routing intelligence

- identical tasks and SHA across providers;
- time-to-green, hidden-test success, regressions, review findings, repair loops, quota and cost;
- routing based on measured performance by task class;
- `UNKNOWN`, never fabricated zero, when a provider does not expose usage.

### L5 — Candidate Build

- builder writes only inside a fresh isolated worktree;
- allowlist and milestone budget are signed;
- tests run before and after the bounded change;
- at most two repair cycles per milestone;
- Codex exact-SHA ACCEPT is required to continue;
- auto-commit permitted; auto-merge/push/deploy forbidden.

### L6 — Overnight milestone factory

Target operating window:

- standard window: 8 hours;
- hard maximum: 10 hours;
- maximum six milestones per night;
- sequential active writer, optional read-only preparation worker;
- automatic continuation only after all milestone gates and Codex ACCEPT;
- stop on P0/P1, scope mutation, SEC-BOOT change, persistent regression or essential-provider outage.

## 5. Provider roles

| Provider family | Default role | May build | May independently review own build |
|---|---|---:|---:|
| GPT | architecture, arbitration, semantic audit | bounded future adapter | no |
| Codex | exact-SHA review, security, targeted correction | yes when explicitly assigned | no |
| Claude | product/strategy challenge, research | no direct repository writes | no |
| Claude Code | builder, UI/browser-linked implementation | after certification | no |
| GLM / Z Code | fast implementation, mechanical repairs | after certification | no |
| Chrome Pilot | browser operation and UI evidence | no | no |

## 6. Control Room requirements

The interface must show:

- active project, SHA and authority version;
- provider/backend/model identity;
- permission mode and execution root;
- quota/usage/latency with source and confidence;
- run state, worker, milestone, budget and stop reason;
- exact tests, diff, review verdicts and package hashes;
- unresolved P0/P1 findings;
- explicit approval queue;
- visible SEC-BOOT/write-tier state.

The interface must never display an unsupported claim such as “connected”, “reviewed” or “safe” based only on configuration.

## 7. Benchmark requirements

A benchmark task must pin:

- source SHA/tree;
- objective and acceptance contract;
- context/file budget;
- tool and network permissions;
- maximum calls and repair loops;
- test and review rubric.

Builder score:

- 40% functional quality;
- 20% regression avoidance;
- 15% speed;
- 10% quota/cost efficiency;
- 10% code/diff quality;
- 5% operational reliability.

Reviewer score prioritizes real findings, false-PASS avoidance and independence over speed.

## 8. Whole-codebase audit contract

The post-Mesh audit is split into independent tracks:

1. architecture and dependency boundaries;
2. security and supply chain;
3. performance and context efficiency;
4. correctness, tests and error handling;
5. product/UI usability;
6. public-release and operational readiness.

Findings are normalized, deduplicated, classified P0–P3 and cross-reviewed. The resulting correction plan is singular and ordered; providers do not each create competing roadmaps.

## 9. Tranche 4

After Candidate Build activation, Run Nights implement in order:

- M11A: TXT/MD/CSV/JSON attachments;
- M11B: PDF/DOCX extraction;
- M11C: Quality Lab minimal;
- M12A: controlled public web;
- M12B: Vision probe;
- M12C: image workflows;
- M13: general operator and multi-run nights.

Each item is a separate immutable checkpoint and exact-SHA review gate.

## 10. Current explicit limitations

- Run Night Master is awaiting hardening/review before merge.
- Provider Mesh and Control Room are not yet installed on the canonical branch.
- Candidate Build is not authorized.
- Full public release is blocked pending licensing, secret/path scrub and strict CI.
- The current V5 documents are descriptive candidates, not runtime authority.
