"""C3 Roadmap Compiler package.

Transforms a short project objective into an executable, bounded, auditable
roadmap. Two-stage compilation: deterministic skeleton + optional bounded model
call for domain intent. The validator owns the final accepted roadmap.

Modules:
  models.py     — Roadmap, TaskSpec, RoadmapState data models
  compiler.py   — two-stage compiler
  templates.py  — deterministic roadmap templates
  validator.py  — human-independent deterministic validator
  store.py      — persistence outside repos
"""
