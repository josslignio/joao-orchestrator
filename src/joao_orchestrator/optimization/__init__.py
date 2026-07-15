"""Factory optimization layer (JOSS Factory Turbo).

Deterministic, stdlib-only optimization primitives that *extend* the existing
orchestrator runtime — they do not duplicate the broker, router, queue,
convergence, or evaluation harness.

Modules:
  telemetry  — per-task consumption proxy and wall-clock measurement
  benchmark  — reproducible B1-B8 fixture corpus and baseline/candidate runner

Design invariants (inherited from evaluation/models.py):
  * Determinism: every persisted artifact is serialized via canonical JSON.
  * Integrity: records carry an integrity_sha256 over their unsigned payload.
  * No NaN; no network; no package installation; no destructive Git.
  * Transparent proxies only: never invent token counts.
"""
