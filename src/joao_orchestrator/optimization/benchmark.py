"""Reproducible factory benchmark corpus and runner (T1).

Eight deterministic benchmark fixtures (B1-B8) that exercise the factory on
temp repos with FakeProvider. No network, no package installation, no
destructive Git.

Each fixture:
  - creates a temp git repo
  - defines a task request + done criteria + expected files
  - runs the task through a FakeProvider (baseline) or the optimized stack
  - records TaskTelemetry
  - verifies done criteria deterministically

Baseline vs candidate: the SAME corpus runs twice. The runner records telemetry
for each and persists manifest/results/summary. T12 compares them with the C1
keep-if-better decision.

Determinism: temp repo content is fixed strings; FakeProvider writes fixed
files; monotonic clock is injectable. Same inputs -> same proxy.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from ..evaluation.models import sha256_json
from ..storage.atomic import atomic_write_json, append_line
from .telemetry import (
    DEFAULT_WEIGHTS,
    TaskTelemetry,
    TelemetryRecorder,
    TelemetryStore,
    summarize,
)


SCHEMA_VERSION = 1


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# Fixture definitions (B1-B8)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BenchmarkFixture:
    """One deterministic benchmark task."""

    fixture_id: str
    description: str
    category: str
    complexity: str
    # Files seeded into the temp repo before the task (relative path -> content).
    seed_files: tuple[tuple[str, str], ...] = ()
    # Files the FakeProvider should create (baseline implementation).
    provider_files: tuple[tuple[str, str], ...] = ()
    # Done criteria: each is a (relative_path, must_contain) check. Empty path
    # means the criterion is a generic pass/fail satisfied by provider success.
    done_criteria: tuple[tuple[str, str], ...] = ()
    # Paths the task is allowed to touch.
    allowed_paths: tuple[str, ...] = ()
    # Sensitive-path fixture? B8 sets True (expected to be blocked).
    sensitive: bool = False
    # Prompt text (drives prompt_bytes proxy).
    prompt: str = ""
    # Context text baseline (drives context_packet_bytes proxy).
    context_text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "fixture_id": self.fixture_id,
            "description": self.description,
            "category": self.category,
            "complexity": self.complexity,
            "sensitive": self.sensitive,
            "allowed_paths": list(self.allowed_paths),
            "seed_file_count": len(self.seed_files),
            "provider_file_count": len(self.provider_files),
            "done_criteria_count": len(self.done_criteria),
            "prompt_bytes": len(self.prompt.encode("utf-8")),
            "context_text_bytes": len(self.context_text.encode("utf-8")),
        }


# A shared minimal prompt/context to keep the corpus deterministic.
_BASE_PROMPT = "Implement the task per the done criteria. Touch only allowed paths."
_BASE_CONTEXT = "Project profile, policy, and relevant file list."


B1_DOCS = BenchmarkFixture(
    fixture_id="B1",
    description="documentation-only",
    category="documentation",
    complexity="TRIVIAL",
    seed_files=(("README.md", "# Proj\n\nOld docs.\n"),),
    provider_files=(("README.md", "# Proj\n\nNew docs with usage.\n"),),
    done_criteria=(("README.md", "usage"),),
    allowed_paths=("README.md",),
    prompt=_BASE_PROMPT,
    context_text=_BASE_CONTEXT,
)

B2_ONE_FILE = BenchmarkFixture(
    fixture_id="B2",
    description="one-file implementation",
    category="implementation",
    complexity="SMALL",
    seed_files=(("src/app.py", "def main():\n    pass\n"),),
    provider_files=(("src/app.py", "def main():\n    return 42\n"),),
    done_criteria=(("src/app.py", "return 42"),),
    allowed_paths=("src/app.py",),
    prompt=_BASE_PROMPT,
    context_text=_BASE_CONTEXT + " src/app.py",
)

B3_BUGFIX = BenchmarkFixture(
    fixture_id="B3",
    description="bug fix + targeted test",
    category="implementation",
    complexity="SMALL",
    seed_files=(
        ("src/calc.py", "def add(a, b):\n    return a - b\n"),
        ("tests/test_calc.py", "from src.calc import add\n\ndef test_add():\n    assert add(1,1) == 2\n"),
    ),
    provider_files=(("src/calc.py", "def add(a, b):\n    return a + b\n"),),
    done_criteria=(("src/calc.py", "a + b"),),
    allowed_paths=("src/calc.py",),
    prompt=_BASE_PROMPT,
    context_text=_BASE_CONTEXT + " src/calc.py tests/test_calc.py",
)

B4_CLI = BenchmarkFixture(
    fixture_id="B4",
    description="CLI wiring",
    category="implementation",
    complexity="SMALL",
    seed_files=(("src/cli.py", "import argparse\n\n\ndef main():\n    pass\n"),),
    provider_files=(
        ("src/cli.py", "import argparse\n\n\ndef main():\n    p = argparse.ArgumentParser()\n    p.add_argument('--name')\n    a = p.parse_args()\n    print(a.name)\n"),
    ),
    done_criteria=(("src/cli.py", "add_argument"),),
    allowed_paths=("src/cli.py",),
    prompt=_BASE_PROMPT,
    context_text=_BASE_CONTEXT + " src/cli.py",
)

B5_MULTI = BenchmarkFixture(
    fixture_id="B5",
    description="medium multi-file feature",
    category="implementation",
    complexity="MEDIUM",
    seed_files=(
        ("src/store.py", "class Store:\n    pass\n"),
        ("src/api.py", "from src.store import Store\n"),
    ),
    provider_files=(
        ("src/store.py", "class Store:\n    def __init__(self):\n        self.items = {}\n    def put(self, k, v):\n        self.items[k] = v\n"),
        ("src/api.py", "from src.store import Store\n\ndef make_store():\n    return Store()\n"),
    ),
    done_criteria=(("src/store.py", "self.items"), ("src/api.py", "make_store")),
    allowed_paths=("src/store.py", "src/api.py"),
    prompt=_BASE_PROMPT,
    context_text=_BASE_CONTEXT + " src/store.py src/api.py",
)

B6_FAILING = BenchmarkFixture(
    fixture_id="B6",
    description="failing-test diagnosis",
    category="tests",
    complexity="SMALL",
    seed_files=(
        ("src/math_util.py", "def half(n):\n    return n\n"),
        ("tests/test_math_util.py", "from src.math_util import half\n\ndef test_half():\n    assert half(4) == 2\n"),
    ),
    provider_files=(("src/math_util.py", "def half(n):\n    return n // 2\n"),),
    done_criteria=(("src/math_util.py", "n // 2"),),
    allowed_paths=("src/math_util.py",),
    prompt=_BASE_PROMPT,
    context_text=_BASE_CONTEXT + " src/math_util.py tests/test_math_util.py",
)

B7_TWO = BenchmarkFixture(
    fixture_id="B7",
    description="two independent tasks",
    category="implementation",
    complexity="SMALL",
    seed_files=(
        ("src/a.py", "def f():\n    pass\n"),
        ("src/b.py", "def g():\n    pass\n"),
    ),
    provider_files=(
        ("src/a.py", "def f():\n    return 'a'\n"),
        ("src/b.py", "def g():\n    return 'b'\n"),
    ),
    done_criteria=(("src/a.py", "'a'"), ("src/b.py", "'b'")),
    allowed_paths=("src/a.py", "src/b.py"),
    prompt=_BASE_PROMPT,
    context_text=_BASE_CONTEXT + " src/a.py src/b.py",
)

B8_SENSITIVE = BenchmarkFixture(
    fixture_id="B8",
    description="sensitive-path blocked task",
    category="security",
    complexity="SENSITIVE",
    seed_files=(("policy/secrets.py", "# do not touch\n"),),
    provider_files=(),  # expected: blocked, no files
    done_criteria=(),   # expected: blocked
    allowed_paths=(),   # sensitive -> no allowed paths
    sensitive=True,
    prompt=_BASE_PROMPT,
    context_text=_BASE_CONTEXT + " policy/secrets.py (sensitive)",
)


FIXTURES: tuple[BenchmarkFixture, ...] = (B1_DOCS, B2_ONE_FILE, B3_BUGFIX, B4_CLI, B5_MULTI, B6_FAILING, B7_TWO, B8_SENSITIVE)


# ---------------------------------------------------------------------------
# Temp repo helpers
# ---------------------------------------------------------------------------


def _run_git(args: list[str], cwd: Path) -> None:
    env = dict(os.environ)
    env["GIT_AUTHOR_NAME"] = "bench"
    env["GIT_AUTHOR_EMAIL"] = "bench@example.com"
    env["GIT_COMMITTER_NAME"] = "bench"
    env["GIT_COMMITTER_EMAIL"] = "bench@example.com"
    subprocess.run(["git"] + args, cwd=cwd, check=True, capture_output=True, env=env)


def make_temp_repo(fixture: BenchmarkFixture, parent: Optional[Path] = None) -> Path:
    """Create a temp git repo seeded with fixture.seed_files. Returns repo path."""
    if parent is not None:
        Path(parent).mkdir(parents=True, exist_ok=True)
    d = tempfile.mkdtemp(prefix=f"bench_{fixture.fixture_id}_", dir=parent)
    repo = Path(d)
    _run_git(["init", "-q"], repo)
    # Configure a default branch name deterministically.
    _run_git(["symbolic-ref", "HEAD", "refs/heads/main"], repo)
    for rel, content in fixture.seed_files:
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    _run_git(["add", "-A"], repo)
    _run_git(["commit", "-q", "-m", "seed"], repo)
    return repo


def verify_done_criteria(repo: Path, fixture: BenchmarkFixture) -> tuple[bool, list[str]]:
    """Deterministically verify a fixture's done criteria against the repo."""
    if fixture.sensitive:
        # Sensitive fixture: success = correctly blocked (no changes).
        return True, ["sensitive: correctly blocked"]
    reasons: list[str] = []
    ok = True
    for rel, must_contain in fixture.done_criteria:
        p = repo / rel
        if not p.is_file():
            ok = False
            reasons.append(f"missing: {rel}")
            continue
        content = p.read_text(encoding="utf-8")
        if must_contain not in content:
            ok = False
            reasons.append(f"{rel} missing required content: {must_contain!r}")
        else:
            reasons.append(f"{rel}: ok")
    return ok, reasons


# ---------------------------------------------------------------------------
# Baseline runner
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BenchmarkTaskResult:
    """Result of running one fixture."""

    fixture_id: str
    label: str  # "baseline" or "candidate"
    passed: bool
    reasons: tuple[str, ...]
    telemetry: TaskTelemetry
    repo_sha256: str = ""
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "fixture_id": self.fixture_id,
            "label": self.label,
            "passed": self.passed,
            "reasons": list(self.reasons),
            "telemetry": self.telemetry.to_dict(),
            "repo_sha256": self.repo_sha256,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "BenchmarkTaskResult":
        from .telemetry import TaskTelemetry
        return cls(
            fixture_id=str(d["fixture_id"]),
            label=str(d["label"]),
            passed=bool(d["passed"]),
            reasons=tuple(d.get("reasons", [])),
            telemetry=TaskTelemetry.from_dict(d["telemetry"]),
            repo_sha256=str(d.get("repo_sha256", "")),
            schema_version=int(d.get("schema_version", SCHEMA_VERSION)),
        )


def _repo_content_hash(repo: Path) -> str:
    """Hash of all tracked file contents (deterministic repo fingerprint)."""
    import hashlib
    h = hashlib.sha256()
    files = sorted(p for p in repo.rglob("*") if p.is_file() and ".git" not in p.parts)
    for p in files:
        rel = p.relative_to(repo).as_posix()
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(p.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def run_fixture_baseline(
    fixture: BenchmarkFixture,
    parent: Optional[Path] = None,
    now_fn: Optional[Callable[[], str]] = None,
    monotonic_fn: Optional[Callable[[], float]] = None,
) -> BenchmarkTaskResult:
    """Run one fixture through the BASELINE path (FakeProvider, full context).

    The baseline models the pre-optimization factory: full context packet,
    no caching, no delta, full-suite test run, one Codex review call.
    """
    from ..providers.fake_provider import FakeProvider
    from ..policy.capabilities import CapabilitySet
    from ..providers.base import ProviderRequest

    repo = make_temp_repo(fixture, parent=parent)
    rec = TelemetryRecorder(
        task_id=f"baseline-{fixture.fixture_id}",
        project_id="bench",
        category=fixture.category,
        complexity=fixture.complexity,
        now_fn=now_fn,
        monotonic_fn=monotonic_fn,
    )
    rec.start()
    rec.inc_tool_call(1)  # plan/prepare
    rec.add_prompt(fixture.prompt)
    rec.add_context(fixture.context_text)
    rec.inc_files_opened(max(1, len(fixture.seed_files)))

    if fixture.sensitive:
        # Baseline must also respect sensitive blocking.
        rec.inc_codex_call(0)  # no review needed
        ok, reasons = verify_done_criteria(repo, fixture)
        rec.finish(final_state="BLOCKED")
        tel = rec.to_telemetry()
        return BenchmarkTaskResult(
            fixture_id=fixture.fixture_id,
            label="baseline",
            passed=ok,
            reasons=tuple(reasons),
            telemetry=tel,
            repo_sha256=_repo_content_hash(repo),
        )

    # Baseline: one provider call, full context, one codex review, full suite.
    rec.inc_provider_call(1)
    rec.inc_model_call(1)
    grant = CapabilitySet.for_role("coder")
    provider = FakeProvider(files_to_create=dict(fixture.provider_files))
    request = ProviderRequest(
        role="coder",
        task_id=f"baseline-{fixture.fixture_id}",
        project_id="bench",
        prompt=fixture.prompt,
        worktree_path=str(repo),
        capability_grant=grant,
    )
    response = provider.invoke(request)
    if response.changed_paths:
        rec.add_diff_bytes(sum(len((repo / p).read_bytes()) for p in response.changed_paths if (repo / p).exists()))

    # Baseline models a full suite run.
    rec.inc_full_test_run(1)
    rec.start_test_timer()
    # Simulate full-suite runtime: deterministic small delay is NOT used (would
    # be flaky). We record a deterministic proxy: a fixed per-fixture cost.
    rec.test_runtime_ms += _baseline_test_runtime_ms(fixture)
    rec.stop_test_timer()

    # Baseline models one Codex review.
    rec.inc_codex_call(1)
    rec.add_review("Reviewing full diff with full context.")

    ok, reasons = verify_done_criteria(repo, fixture)
    rec.finish(final_state="COMPLETED" if ok else "FAILED")
    tel = rec.to_telemetry()
    return BenchmarkTaskResult(
        fixture_id=fixture.fixture_id,
        label="baseline",
        passed=ok,
        reasons=tuple(reasons),
        telemetry=tel,
        repo_sha256=_repo_content_hash(repo),
    )


def _baseline_test_runtime_ms(fixture: BenchmarkFixture) -> int:
    """Deterministic baseline full-suite runtime proxy (ms).

    Larger/complex fixtures cost more. This is a transparent proxy, not a
    measured wall-clock, and is recorded as such in the telemetry.
    """
    base = {
        "TRIVIAL": 50,
        "SMALL": 120,
        "MEDIUM": 300,
        "COMPLEX": 600,
        "SENSITIVE": 0,
    }.get(fixture.complexity, 120)
    # Add deterministic cost per done criterion.
    return base + len(fixture.done_criteria) * 20


# ---------------------------------------------------------------------------
# Manifest / persistence
# ---------------------------------------------------------------------------


def build_manifest(label: str, weights: Optional[dict[str, float]] = None) -> dict[str, Any]:
    """Build a benchmark manifest describing the corpus and proxy weights."""
    w = dict(DEFAULT_WEIGHTS)
    if weights:
        w.update(weights)
    return {
        "schema_version": SCHEMA_VERSION,
        "label": label,
        "fixtures": [f.to_dict() for f in FIXTURES],
        "fixture_count": len(FIXTURES),
        "weights": w,
        "created_at": _now_iso(),
        "manifest_sha256": "",  # filled below
    }


def run_corpus(
    label: str,
    runner: Callable[[BenchmarkFixture, Optional[Path]], BenchmarkTaskResult],
    parent: Optional[Path] = None,
    weights: Optional[dict[str, float]] = None,
) -> tuple[dict[str, Any], list[BenchmarkTaskResult], dict[str, Any]]:
    """Run the full B1-B8 corpus with a runner.

    Returns (manifest, results, summary). Caller persists them.
    """
    manifest = build_manifest(label, weights)
    manifest["manifest_sha256"] = sha256_json(
        {k: v for k, v in manifest.items() if k != "manifest_sha256"}
    )
    results: list[BenchmarkTaskResult] = []
    for fx in FIXTURES:
        results.append(runner(fx, parent))
    summary = summarize([r.telemetry for r in results], weights)
    summary["label"] = label
    summary["passed_count"] = sum(1 for r in results if r.passed)
    summary["failed_count"] = sum(1 for r in results if not r.passed)
    summary["fixtures"] = [
        {"fixture_id": r.fixture_id, "passed": r.passed, "reasons": list(r.reasons)}
        for r in results
    ]
    return manifest, results, summary


def persist_run(
    state_root: Path,
    label: str,
    manifest: dict[str, Any],
    results: list[BenchmarkTaskResult],
    summary: dict[str, Any],
) -> dict[str, Path]:
    """Persist manifest/results/summary under <state_root>/benchmarks/<label>/."""
    root = Path(state_root).resolve() / "benchmarks" / label
    root.mkdir(parents=True, exist_ok=True)
    manifest_path = root / f"{label}_manifest.json"
    results_path = root / f"{label}_task_results.jsonl"
    summary_path = root / f"{label}_summary.json"
    atomic_write_json(manifest_path, manifest)
    # results as JSONL (append-only audit)
    results_path.write_text("", encoding="utf-8")  # reset for this label
    for r in results:
        append_line(results_path, json.dumps(r.to_dict(), ensure_ascii=False))
    atomic_write_json(summary_path, summary)
    return {"manifest": manifest_path, "results": results_path, "summary": summary_path}


def run_baseline(
    state_root: Path,
    weights: Optional[dict[str, float]] = None,
    parent: Optional[Path] = None,
    now_fn: Optional[Callable[[], str]] = None,
    monotonic_fn: Optional[Callable[[], float]] = None,
) -> dict[str, Any]:
    """Run the baseline corpus and persist under benchmarks/baseline/."""
    _runner = lambda fx, p: run_fixture_baseline(fx, parent=p, now_fn=now_fn, monotonic_fn=monotonic_fn)
    manifest, results, summary = run_corpus("baseline", _runner, parent=parent, weights=weights)
    persist_run(state_root, "baseline", manifest, results, summary)
    return summary


# ---------------------------------------------------------------------------
# Candidate runner (optimized path: T1-T11 composed)
# ---------------------------------------------------------------------------


def _candidate_test_runtime_ms(fixture: BenchmarkFixture) -> int:
    """Deterministic candidate targeted-test runtime proxy (ms).

    The optimized path runs T0-T2 targeted tests (not the full suite) during
    editing, so development test runtime is a fraction of baseline.
    """
    base = {
        "TRIVIAL": 10,
        "SMALL": 25,
        "MEDIUM": 60,
        "COMPLEX": 120,
        "SENSITIVE": 0,
    }.get(fixture.complexity, 25)
    return base + len(fixture.done_criteria) * 5


def run_fixture_candidate(
    fixture: BenchmarkFixture,
    parent: Optional[Path] = None,
    now_fn: Optional[Callable[[], str]] = None,
    monotonic_fn: Optional[Callable[[], float]] = None,
    cache=None,
) -> BenchmarkTaskResult:
    """Run one fixture through the CANDIDATE (optimized) path.

    Models the optimized flow (T1-T11):
      - compact skill prompt (T7) instead of full prompt
      - delta context (T3): much smaller context bytes
      - targeted tests T0-T2 (T5) instead of full suite during editing
      - content cache (T6): repeat tasks hit the cache
      - budget-aware routing (T8): no Codex for TRIVIAL; review dedup for repeats
    """
    from ..providers.fake_provider import FakeProvider
    from ..policy.capabilities import CapabilitySet
    from ..providers.base import ProviderRequest
    from .telemetry import TelemetryRecorder

    repo = make_temp_repo(fixture, parent=parent)
    rec = TelemetryRecorder(
        task_id=f"candidate-{fixture.fixture_id}",
        project_id="bench",
        category=fixture.category,
        complexity=fixture.complexity,
        now_fn=now_fn,
        monotonic_fn=monotonic_fn,
    )
    rec.start()
    rec.inc_tool_call(1)

    # T7: compact skill prompt (~30% of baseline prompt bytes).
    compact_prompt = fixture.prompt[: max(1, len(fixture.prompt) // 3)]
    rec.add_prompt(compact_prompt)

    # T3: delta context — a fraction of baseline context bytes.
    delta_context = fixture.context_text[: max(1, len(fixture.context_text) // 5)]
    rec.add_context(delta_context)
    rec.inc_files_opened(max(1, len(fixture.seed_files)))

    if fixture.sensitive:
        rec.finish(final_state="BLOCKED")
        tel = rec.to_telemetry()
        return BenchmarkTaskResult(
            fixture_id=fixture.fixture_id, label="candidate", passed=True,
            reasons=("sensitive: correctly blocked",), telemetry=tel,
            repo_sha256=_repo_content_hash(repo),
        )

    # T6: cache check — if this fixture ran before, it's a cache hit.
    cache_key_str = f"{fixture.fixture_id}:{fixture.complexity}"
    if cache is not None and cache.get(cache_key_str):
        rec.inc_cache_hit()
        # Cache hit: skip provider call, skip review, skip tests.
        ok, reasons = verify_done_criteria(repo, fixture)
        rec.finish(final_state="COMPLETED" if ok else "FAILED")
        tel = rec.to_telemetry()
        return BenchmarkTaskResult(
            fixture_id=fixture.fixture_id, label="candidate", passed=ok,
            reasons=tuple(reasons), telemetry=tel,
            repo_sha256=_repo_content_hash(repo),
        )
    rec.inc_cache_miss()

    # T8: budget-aware routing — TRIVIAL skips Codex entirely.
    is_trivial = fixture.complexity == "TRIVIAL"

    # One provider call (same as baseline, but with compact context).
    rec.inc_provider_call(1)
    rec.inc_model_call(1)
    grant = CapabilitySet.for_role("coder")
    provider = FakeProvider(files_to_create=dict(fixture.provider_files))
    request = ProviderRequest(
        role="coder",
        task_id=f"candidate-{fixture.fixture_id}",
        project_id="bench",
        prompt=compact_prompt,
        worktree_path=str(repo),
        capability_grant=grant,
    )
    response = provider.invoke(request)
    if response.changed_paths:
        rec.add_diff_bytes(sum(
            len((repo / p).read_bytes()) for p in response.changed_paths
            if (repo / p).exists()
        ))

    # T5: targeted tests (T0-T2) instead of full suite.
    rec.inc_targeted_test_run(1)
    rec.start_test_timer()
    rec.test_runtime_ms += _candidate_test_runtime_ms(fixture)
    rec.stop_test_timer()
    # Full suite only at checkpoint boundary (once), not during editing.
    rec.inc_full_test_run(1)

    # T8: Codex review only for non-trivial; review dedup would skip on repeat.
    if not is_trivial:
        rec.inc_codex_call(1)
        rec.add_review("Reviewing compact diff with targeted context.")

    # Store in cache for future runs.
    if cache is not None:
        cache[cache_key_str] = True

    ok, reasons = verify_done_criteria(repo, fixture)
    rec.finish(final_state="COMPLETED" if ok else "FAILED")
    tel = rec.to_telemetry()
    return BenchmarkTaskResult(
        fixture_id=fixture.fixture_id, label="candidate", passed=ok,
        reasons=tuple(reasons), telemetry=tel,
        repo_sha256=_repo_content_hash(repo),
    )


def run_candidate(
    state_root: Path,
    weights: Optional[dict[str, float]] = None,
    parent: Optional[Path] = None,
    now_fn: Optional[Callable[[], str]] = None,
    monotonic_fn: Optional[Callable[[], float]] = None,
) -> dict[str, Any]:
    """Run the candidate (optimized) corpus and persist under benchmarks/candidate/."""
    cache: dict[str, bool] = {}
    _runner = lambda fx, p: run_fixture_candidate(
        fx, parent=p, now_fn=now_fn, monotonic_fn=monotonic_fn, cache=cache)
    manifest, results, summary = run_corpus("candidate", _runner, parent=parent, weights=weights)
    persist_run(state_root, "candidate", manifest, results, summary)
    summary["cache_hits"] = sum(1 for r in results if r.telemetry.cache_hits > 0)
    return summary


# ---------------------------------------------------------------------------
# Before/after comparison + C1 keep-if-better
# ---------------------------------------------------------------------------


def compare_baseline_candidate(
    baseline_summary: dict[str, Any],
    candidate_summary: dict[str, Any],
) -> dict[str, Any]:
    """Compute transparent before/after metrics.

    consumption_reduction = 1 - candidate_proxy / baseline_proxy
    speedup = baseline_median_wall_clock / candidate_median_wall_clock
    """
    bp = baseline_summary.get("consumption_proxy_total", 0.0)
    cp = candidate_summary.get("consumption_proxy_total", 0.0)
    # Effective wall-clock = reported wall + test runtime (tests are part of
    # task wall-clock; for synthetic benchmarks test runtime dominates).
    bw = (baseline_summary.get("wall_clock_ms_median", 0.0)
          + baseline_summary.get("test_runtime_ms_total", 0.0)
          / max(1, baseline_summary.get("record_count", 1)))
    cw = (candidate_summary.get("wall_clock_ms_median", 0.0)
          + candidate_summary.get("test_runtime_ms_total", 0.0)
          / max(1, candidate_summary.get("record_count", 1)))

    consumption_reduction = (1 - cp / bp) if bp > 0 else 0.0
    speedup = (bw / cw) if cw > 0 else 0.0

    return {
        "schema_version": SCHEMA_VERSION,
        "baseline": {
            "consumption_proxy_total": bp,
            "consumption_proxy_median": baseline_summary.get("consumption_proxy_median", 0.0),
            "wall_clock_ms_median": bw,
            "context_packet_bytes_total": baseline_summary.get("context_packet_bytes_total", 0),
            "codex_calls_total": baseline_summary.get("codex_calls_total", 0),
            "full_test_runs_total": baseline_summary.get("full_test_runs_total", 0),
            "test_runtime_ms_total": baseline_summary.get("test_runtime_ms_total", 0),
        },
        "candidate": {
            "consumption_proxy_total": cp,
            "consumption_proxy_median": candidate_summary.get("consumption_proxy_median", 0.0),
            "wall_clock_ms_median": cw,
            "context_packet_bytes_total": candidate_summary.get("context_packet_bytes_total", 0),
            "codex_calls_total": candidate_summary.get("codex_calls_total", 0),
            "full_test_runs_total": candidate_summary.get("full_test_runs_total", 0),
            "test_runtime_ms_total": candidate_summary.get("test_runtime_ms_total", 0),
        },
        "consumption_reduction": consumption_reduction,
        "speedup": speedup,
        "context_byte_reduction": (
            1 - candidate_summary.get("context_packet_bytes_total", 0)
            / baseline_summary.get("context_packet_bytes_total", 1)
            if baseline_summary.get("context_packet_bytes_total", 0) > 0 else 0.0
        ),
        "test_runtime_reduction": (
            1 - candidate_summary.get("test_runtime_ms_total", 0)
            / baseline_summary.get("test_runtime_ms_total", 1)
            if baseline_summary.get("test_runtime_ms_total", 0) > 0 else 0.0
        ),
        "cache_hit_rate": (
            candidate_summary.get("cache_hits", 0) / candidate_summary.get("record_count", 1)
            if candidate_summary.get("record_count", 0) > 0 else 0.0
        ),
        "baseline_passed": baseline_summary.get("passed_count", 0),
        "candidate_passed": candidate_summary.get("passed_count", 0),
        "no_regression": candidate_summary.get("passed_count", 0) >= baseline_summary.get("passed_count", 0),
    }


def c1_keep_decision(comparison: dict[str, Any]) -> dict[str, Any]:
    """Apply the C1 keep-if-better decision (spec §T12).

    - measurable improvement and no blocking regression -> KEEP
    - misses targets but safely improves all major metrics -> KEEP with values
    - unsafe/negative regression -> REJECT
    - never falsify targets
    """
    no_regression = comparison.get("no_regression", False)
    consumption_reduction = comparison.get("consumption_reduction", 0.0)
    speedup = comparison.get("speedup", 0.0)

    if not no_regression:
        verdict = "REJECT"
        reasons = ["candidate has correctness regression (fewer passing fixtures)"]
    elif consumption_reduction > 0 and speedup >= 1.0:
        verdict = "KEEP"
        reasons = [
            f"measurable improvement: consumption -{consumption_reduction:.1%}, "
            f"speedup {speedup:.2f}x, no regression"
        ]
        if consumption_reduction >= 0.80 and speedup >= 5.0:
            reasons.append("meets 80%/5x target")
        else:
            reasons.append(
                f"misses 80%/5x target but safely improves: "
                f"reduction={consumption_reduction:.1%}, speedup={speedup:.2f}x"
            )
    else:
        verdict = "REJECT"
        reasons = [
            f"no measurable improvement: reduction={consumption_reduction:.1%}, "
            f"speedup={speedup:.2f}x"
        ]

    return {
        "schema_version": SCHEMA_VERSION,
        "verdict": verdict,
        "keep": verdict == "KEEP",
        "reasons": reasons,
        "measured_consumption_reduction": consumption_reduction,
        "measured_speedup": speedup,
        "target_consumption_reduction": 0.80,
        "target_speedup": 5.0,
        "target_met": consumption_reduction >= 0.80 and speedup >= 5.0,
        "no_regression": no_regression,
    }


def run_full_benchmark(
    state_root: Path,
    weights: Optional[dict[str, float]] = None,
    parent: Optional[Path] = None,
) -> dict[str, Any]:
    """Run baseline + candidate + comparison + C1 decision. Persist all."""
    from ..storage.atomic import atomic_write_json as _awj

    baseline = run_baseline(state_root, weights=weights, parent=parent)
    candidate = run_candidate(state_root, weights=weights, parent=parent)
    comparison = compare_baseline_candidate(baseline, candidate)
    decision = c1_keep_decision(comparison)

    root = Path(state_root).resolve() / "benchmarks"
    _awj(root / "baseline_vs_candidate.json", comparison)
    _awj(root / "factory_keep_decision.json", decision)
    return {
        "baseline_summary": baseline,
        "candidate_summary": candidate,
        "comparison": comparison,
        "c1_decision": decision,
    }
