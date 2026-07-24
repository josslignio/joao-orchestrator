# JOÃO Product Backlog V5

**Status:** candidate, ordered by execution dependency.  
**P0/P1 must be closed before Candidate Build or public release.**

| ID | Priority | Item | Completion evidence |
|---|---:|---|---|
| GA-001 | P1 | Bind every Run Night execution root to the repository | negative outside-root test + exact source review |
| GA-002 | P1 | Reuse comprehensive secret detection for NightArtifacts | GitHub/OpenAI/Anthropic/Slack token regressions |
| GA-003 | P1 | Kill child process groups on watchdog signals | real child PID disappears after SIGTERM test |
| GA-004 | P1 | Make all SEC-BOOT canaries pytest-compatible | `pytest --collect-only tests` and canary suite pass |
| GA-005 | P1 | Replace incomplete global audit archive | complete `git archive`, Git bundle, raw gates, manifest |
| GA-006 | P1 | Strict CI on complete history | portable matrix + full macOS suite from clean clone |
| GA-007 | P1 | Remove tracked bytecode/pytest caches and prove clean compile | generated files absent from Git tree and ignored |
| GA-008 | P1 | Codex exact-SHA audit of global hardening | machine-readable ACCEPT bound to candidate SHA/tree |
| GA-009 | P1 | GPT counter-audit and merge authorization | PASS certificate bound to evidence ZIP hash |
| GH-001 | P1 | Establish canonical GitHub remote and protected main | remote SHA equals approved local SHA; branch protection |
| GH-002 | P1 | Decide repository license/visibility | recorded owner decision; LICENSE present before public |
| GH-003 | P1 | Sanitize public repository | zero secrets, private evidence, customer data and unsafe paths |
| GH-004 | P2 | Publish recruiter-facing repository README | accurate features, architecture, status and limits |
| GH-005 | P2 | Update GitHub profile README/About/topics/pins | profile reflects AI systems + growth/operator positioning |
| MESH-001 | P1 | Install provider registry and certification contracts | identity, permission, cwd and mutation tests |
| MESH-002 | P1 | Connect GPT and Codex subscription/token paths | authoritative identity + quota/availability status |
| MESH-003 | P1 | Connect Claude and Claude Code | backend identity, plan/read-only and builder role tests |
| MESH-004 | P1 | Connect GLM/Z Code High | backend identity, strict OpenCode permissions, role tests |
| MESH-005 | P1 | Add Chrome Pilot adapter | browser-only scope; no exact-SHA authority |
| UI-001 | P1 | Control Room Command Center | project/SHA/mode/provider visible; no unsupported claims |
| UI-002 | P1 | Provider Matrix | identity, role, quota, latency, availability and source |
| UI-003 | P1 | Runs/Evidence/Approval views | gates, budgets, stop, packages, P0/P1 and approvals |
| BENCH-001 | P1 | Frozen benchmark schema and workload | reproducible manifests and identical task constraints |
| BENCH-002 | P1 | Usage/time/quality instrumentation | unknown handling, hash-chained events, no raw transcript |
| BENCH-003 | P1 | Builder and reviewer scorecards | rankings by task class with confidence/sample size |
| AUDIT-001 | P1 | Multi-family architecture audit | independent reports + normalized findings |
| AUDIT-002 | P1 | Security/supply-chain audit | attack matrix, secrets, subprocess, network, provenance |
| AUDIT-003 | P1 | Performance/context audit | latency, I/O, memory, redundant calls, context cost |
| AUDIT-004 | P1 | Product/UI audit | real browser E2E and Boss recovery workflows |
| CB-001 | P1 | Candidate Build signed activation | separate authority, exact scope and expiry |
| CB-002 | P1 | Isolated builder worktrees | one writer, immutable checkpoints, rollback |
| CB-003 | P1 | Bounded repair and exact-SHA review loop | max two repairs, stop on persistent red gate |
| RN-001 | P1 | RN0 read-only rehearsal | watchdog, mutation, activation, evidence and stop tests |
| RN-002 | P1 | RN-CB0 candidate-build rehearsal | deliberate failing build, rollback, Codex block/accept |
| T4-001 | P1 | M11A TXT/MD/CSV/JSON attachments | contracts, limits, security, API/UI and tests |
| T4-002 | P1 | M11B PDF/DOCX | bounded extraction, malformed-file tests, provenance |
| T4-003 | P1 | M11C Quality Lab | fixtures, scoring, regression comparisons |
| T4-004 | P2 | M12A controlled public web | SSRF/network policy, sources and browser evidence |
| T4-005 | P2 | M12B vision probe | capability/quality/security benchmarks |
| T4-006 | P2 | M12C image workflows | generation/edit contracts and evidence |
| OPS-001 | P2 | Eight-hour dynamic overnight scheduler | maximum six milestones, no artificial one-lot stop |
| OPS-002 | P2 | Read-only preparation worker | may prepare next lot while reviewer runs; no writes |
| OPS-003 | P2 | Morning decision dashboard | candidates, verdicts, blockers, quotas and next action |
