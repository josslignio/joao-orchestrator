"""Generic benchmark protocol for explicitly supplied fixture providers."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from .fixtures import Fixture, run_fixtures


@dataclass
class FixtureBenchmark:
    fixture_id: str
    category: str
    kind: str
    wall_seconds: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BenchmarkSummary:
    fixtures: list[FixtureBenchmark] = field(default_factory=list)
    fixture_results: dict[str, dict[str, str]] = field(default_factory=dict)
    comparison_kind: str = "EXECUTION_TIMING_ONLY"

    def to_dict(self) -> dict[str, Any]:
        return {"fixtures": [f.to_dict() for f in self.fixtures],
                "fixture_results": self.fixture_results,
                "comparison_kind": self.comparison_kind}


def run_benchmark(fixtures: Iterable[Fixture]) -> BenchmarkSummary:
    """Time explicitly supplied fixtures; no product fixture set is implicit."""
    items = list(fixtures)
    timings: list[FixtureBenchmark] = []
    for fixture in items:
        started = time.perf_counter()
        try:
            fixture.expect(fixture.build())
        except Exception:
            pass
        timings.append(FixtureBenchmark(
            fixture_id=fixture.fixture_id, category=fixture.category,
            kind=fixture.kind, wall_seconds=round(time.perf_counter() - started, 6)))
    return BenchmarkSummary(fixtures=timings, fixture_results=run_fixtures(items))


__all__ = ["FixtureBenchmark", "BenchmarkSummary", "run_benchmark"]
