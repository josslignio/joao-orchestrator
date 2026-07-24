"""Generic fixture protocol and runner.

Concrete regression scenarios live outside the canonical package in an
explicit project-profile boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable


REAL_MECHANISM = "REAL_MECHANISM"
SCENARIO_DOCUMENTATION = "SCENARIO_DOCUMENTATION"


@dataclass
class Fixture:
    """One externally supplied regression fixture."""

    fixture_id: str
    category: str
    old_behavior: str
    v2_behavior: str
    build: Callable[[], dict[str, Any]] = field(repr=False)
    expect: Callable[[dict[str, Any]], None] = field(repr=False)
    kind: str = REAL_MECHANISM

    def to_meta(self) -> dict[str, str]:
        return {"fixture_id": self.fixture_id, "category": self.category,
                "kind": self.kind, "old_behavior": self.old_behavior,
                "v2_behavior": self.v2_behavior}


def run_fixtures(fixtures: Iterable[Fixture]) -> dict[str, dict[str, str]]:
    """Run explicitly supplied fixtures without selecting any project."""
    results: dict[str, dict[str, str]] = {}
    for fixture in fixtures:
        try:
            fixture.expect(fixture.build())
            results[fixture.fixture_id] = {"status": "PASS", "kind": fixture.kind}
        except Exception as exc:
            results[fixture.fixture_id] = {
                "status": "FAIL", "kind": fixture.kind,
                "error": f"{type(exc).__name__}: {exc}",
            }
    return results


__all__ = ["REAL_MECHANISM", "SCENARIO_DOCUMENTATION", "Fixture", "run_fixtures"]
