# JOÃO current architecture

`src/joao_orchestrator` is the canonical implementation namespace.
`src/joss_orchestrator` is compatibility-only: each module re-exports its
canonical counterpart and must not gain independent business logic.

The bounded execution path is:

1. Queue scheduler creates an isolated Git worktree at a resolved revision.
2. Provider dispatch is fail-closed; a non-fake engine without an adapter
   returns an error rather than a synthetic success.
3. Structured validation profiles resolve approved Python/Git executables and
   execute list argv with `shell=False`, timeouts, output caps, redaction, and
   a policy allowlist.
4. `validation.json` records exact declared argv, return code, and outcome.
   Any nonzero return code makes validation and the pipeline fail.
5. Review packets are evidence only. They remain `REVIEW_NOT_RUN` until
   independent reviewer evidence is available. Approval, commit, push, merge,
   and release remain human-controlled.

State, queue, artifacts, and worktrees are project-scoped. Runtime state is
outside managed repositories. Legacy MCP/control-plane names are not active
runtime dependencies; historical branches and worktrees remain forensic-only.

Concrete product defaults, forbidden-pattern data, and historical regression
scenarios live under `project_profiles/`, outside `src/joao_orchestrator`.
The generic V2 profile loader accepts an explicitly selected JSON file; it does
not select or import a project. Generic fixture and benchmark interfaces accept
explicitly supplied fixtures only.
