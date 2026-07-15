"""V2 impact-based test selection + cache adapter (§18).

Thin adapter over the EXISTING engine:

* :func:`joao_orchestrator.optimization.test_selector.select_tests`
* :class:`joao_orchestrator.optimization.repo_index.ImpactGraph`
* :func:`joao_orchestrator.optimization.warm_cache.make_test_cache_key`

V2 policy (§18):

* focused tests first, affected second, fresh acceptance always;
* full suite ONLY for cross-cutting core or the final gate;
* cache may GUIDE selection but never REPLACE fresh acceptance.

The cache key binds HEAD + file hashes + command + environment fingerprint so
a stale green result is never trusted across a changed environment.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

# Reuse existing engine surfaces.
from ..optimization.test_selector import select_tests as _select_tests  # noqa
from ..optimization.repo_index import build_impact_graph  # noqa
from ..optimization.warm_cache import make_test_cache_key  # noqa


@dataclass
class TestSelection:
    """§18 selection output."""
    focused_tests: list[str] = field(default_factory=list)
    affected_tests: list[str] = field(default_factory=list)
    acceptance_tests: list[str] = field(default_factory=list)
    full_regression_required: bool = False
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {**self.__dict__}


class V2TestSelector:
    """Wraps the existing selector with the §18 cache-guidance rule."""

    def __init__(self, repo_root: Path):
        self.repo_root = Path(repo_root)

    def select(
        self, *, changed_files: Sequence[str], acceptance_tests: Sequence[str],
        is_cross_cutting_core: bool = False, is_final_gate: bool = False,
        cache_key_inputs: dict[str, Any] | None = None,
    ) -> TestSelection:
        # §18: full suite ONLY for cross-cutting core or the final gate.
        full = bool(is_cross_cutting_core or is_final_gate)
        reason_bits: list[str] = []
        if full:
            reason_bits.append(
                "full suite: cross-cutting core" if is_cross_cutting_core
                else "full suite: final gate")

        focused: list[str] = []
        affected: list[str] = []
        try:
            sel = _select_tests(
                self.repo_root, list(changed_files))
            focused = list(getattr(sel, "focused", []) or [])
            affected = list(getattr(sel, "affected", []) or [])
        except Exception as e:  # selector is best-effort; never block on it
            reason_bits.append(f"selector unavailable ({type(e).__name__}); "
                               "falling back to acceptance-only")

        # Cache GUIDES but never replaces fresh acceptance (§18).
        cache_note = ""
        if cache_key_inputs:
            try:
                _key = make_test_cache_key(self.repo_root,
                                           list(changed_files))
                cache_note = (f"cache_key={_key[:16]}… "
                              "(guidance only; acceptance always fresh)")
            except Exception:
                pass

        return TestSelection(
            focused_tests=focused,
            affected_tests=affected,
            acceptance_tests=list(acceptance_tests),
            full_regression_required=full,
            reason=" | ".join(reason_bits + ([cache_note] if cache_note else [])
                              ) or "focused-first selection",
        )


__all__ = ["TestSelection", "V2TestSelector"]
