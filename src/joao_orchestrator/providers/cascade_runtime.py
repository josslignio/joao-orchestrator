"""B-29.1 (suite) — WIRE THE COST CASCADE TO REAL WORKERS.

`providers/cascade.py` is a pure, deterministic router: it decides WHO builds and proves
it objectively, but its workers are injected callables. This module closes limit L9.3 of
the CERVEAU report by binding those callables to the REAL local workers:

    (a) DETERMINISTIC — an optional in-process tool (cost ~0)
    (b)/(c) GLM       — a real `joao-glm` subprocess (same pattern as `GLMBuilder`), one per
                        best-of-N angle, each in a git-isolated pass over the worktree
    (d) CLAUDE        — a real `claude` CLI subprocess, last resort only

`CascadeBuilder` is a normal `BuilderAdapter`: `RunRuntime` drives it exactly like
`GLMBuilder`, so the winning artifact still flows through the unchanged independent review
downstream. The MEMORY block that `RunRuntime._inject` prepends to the mission reaches EVERY
best-of-N candidate (not just the winner): each candidate's archived `builder-task.md` is
built from that block-prefixed mission, so the injection is provable per worker.

FAIL-CLOSED (D-018 / 1.2): a worker that dies, times out or fails its objective test is an
EXPLICIT logged escalation, never a silent pass; if no tier verifies — Claude included — the
build result is `ok=False` and `RunRuntime` blocks the run. The worktree is always returned
to its pre-build state before the winner (or nothing, on BLOCK) is applied, so cross-candidate
flux cannot leak between variants.

The GLM/Claude/test runners are injectable seams (`glm_runner`, `claude_runner`,
`test_runner`) so the whole isolation/capture/judge/cost machinery is unit-testable with no
real model call; the defaults are the real subprocesses.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

from ..storage.atomic import atomic_write_json, atomic_write_text
from .cascade import (COST_CLAUDE, COST_GLM, Cascade, MissionCost, RouteDecision,
                      TaskSpec, Tier, WorkerResult, DEFAULT_ANGLES)

GLM_MODEL = "zai-coding-plan/glm-4.5-air"
CLAUDE_MODEL = "claude-cli"
DETERMINISTIC_MODEL = "deterministic-tool"
GIT_TIMEOUT = 120  # no git call may wedge the build forever (P2-B)


def _run_bounded(argv: list[str], *, cwd: Optional[Path] = None, timeout: int) -> tuple[int, str, str, bool]:
    """Run a child in its OWN session and, on timeout, SIGKILL the whole process GROUP.

    `subprocess.run(timeout=…)` reaps only the direct child; a model/network grandchild spawned
    by joao-glm/claude would be orphaned (P2-C). Starting a new session lets us kill the group.
    Returns (returncode, stdout, stderr, timed_out).
    """
    proc = subprocess.Popen(argv, cwd=str(cwd) if cwd else None, shell=False,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            start_new_session=True)
    try:
        out, err = proc.communicate(timeout=timeout)
        return proc.returncode, out, err, False
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            out, err = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            out, err = "", ""
        return 124, out, err, True


# ─────────────────────────── worktree isolation ───────────────────────────
class WorkspaceGit:
    """Snapshot / reset / capture / re-apply the worktree so each candidate builds clean.

    `git` is shelled out (never a library) to match the rest of the runtime. `capture_changes`
    copies the produced files OUT of the worktree (into a dir under the run's evidence, never
    inside the worktree) so a subsequent `git clean` cannot touch them.
    """

    def __init__(self, workspace: Path):
        self.workspace = Path(workspace)
        self._untracked_snap: Optional[Path] = None
        self._untracked_files: list[str] = []

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        # bounded (P2-B): a hung filter/lock can never wedge the build forever
        return subprocess.run(["git", *args], cwd=str(self.workspace), shell=False,
                              capture_output=True, text=True, check=check, timeout=GIT_TIMEOUT)

    def snapshot_base(self, untracked_snapshot: Optional[Path] = None) -> str:
        """Capture the CURRENT tree so every candidate can be reset to it.

        `git stash create` records tracked WIP but NOT untracked files, and `git clean -fd`
        would then delete a user's pre-existing untracked WIP forever (P2-A). So we ALSO copy
        every untracked-non-ignored file into a snapshot dir and restore it on each reset.
        """
        sha = self._git("stash", "create").stdout.strip()
        base = sha or self._git("rev-parse", "HEAD").stdout.strip()
        if untracked_snapshot is not None:
            raw = self._git("ls-files", "--others", "--exclude-standard", "-z").stdout
            self._untracked_files = [p for p in raw.split("\0") if p]
            self._untracked_snap = Path(untracked_snapshot)
            for rel in self._untracked_files:
                src = self.workspace / rel
                if src.is_file():
                    dst = self._untracked_snap / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
        return base

    def reset_to_base(self, base: str) -> None:
        # best-effort restoration; git failures must not raise out of a finally (P2-B / P1-A)
        self._git("checkout", "-q", "--force", base, "--", ".", check=False)
        self._git("clean", "-fdq", check=False)
        # restore pre-existing untracked WIP that `clean` just removed (P2-A)
        if self._untracked_snap is not None:
            for rel in self._untracked_files:
                src = self._untracked_snap / rel
                if src.is_file():
                    dst = self.workspace / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
        self._git("reset", "-q", check=False)  # index → HEAD; working tree stays at `base`

    def _changed_paths(self) -> list[str]:
        raw = self._git("status", "--porcelain=v1", "-z", "--untracked-files=all").stdout
        return sorted({item[3:] for item in raw.split("\0") if item})

    def capture_changes(self, dest: Path) -> dict:
        """Copy every changed file into `dest`; return {files, deletions} vs base."""
        dest.mkdir(parents=True, exist_ok=True)
        files, deletions = [], []
        for rel in self._changed_paths():
            src = self.workspace / rel
            if src.is_file():
                target = dest / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, target)
                files.append(rel)
            else:
                deletions.append(rel)
        return {"files": files, "deletions": deletions}

    def apply_capture(self, src: Path, capture: dict) -> None:
        for rel in capture.get("files", []):
            source = src / rel
            if source.is_file():
                target = self.workspace / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        for rel in capture.get("deletions", []):
            (self.workspace / rel).unlink(missing_ok=True)


# ─────────────────────────── real subprocess runners ───────────────────────────
def real_glm_runner(executable: Path, timeout: int = 900) -> Callable:
    """A `joao-glm` subprocess runner (same contract as GLMBuilder). Writes into the worktree.

    Returns {ok, returncode, output, output_sha256, real_cost, duration_s, stderr}. `real_cost`
    is the token/cost figure joao-glm reports if any (else 0.0 — never fabricated).
    """
    from ..bubble.runtime import digest  # local import avoids a cycle

    def _run(workspace: Path, task_file: Path, output: Path, allowed: list[str], angle: str) -> dict:
        from ..bubble.write_tier_policy import assert_write_tier_enabled
        assert_write_tier_enabled("cascade.runner")
        argv = [str(executable), "--workspace", str(workspace), "--task-file", str(task_file),
                "--output", str(output), "--mode", "workspace-write", "--budget", "small"]
        for item in allowed:
            argv.extend(["--allowed-path", item])
        started = time.monotonic()
        try:
            rc, _out, stderr, timed_out = _run_bounded(argv, timeout=timeout)
        except OSError as exc:  # e.g. joao-glm missing/unexecutable — fail-closed, never raise
            rc, stderr, timed_out = 127, f"joao-glm not launchable: {exc}", False
        duration = round(time.monotonic() - started, 2)
        real_cost = _extract_real_cost(output)
        return {"ok": rc == 0 and not timed_out, "returncode": rc, "output": str(output),
                "output_sha256": digest(output) if output.exists() else None,
                "real_cost": real_cost, "duration_s": duration, "stderr": stderr[-2000:],
                "timed_out": timed_out}

    return _run


def real_claude_runner(executable: str = "claude", timeout: int = 1200,
                       claude_model: str = "sonnet") -> Callable:
    """A last-resort `claude` CLI builder. Real subprocess; only ever reached on full escalation.

    B-24 tiering: `claude_model` selects the tier — "haiku" for a cheap smoke build, "sonnet"
    for a real build (never Opus, per the quota doctrine). The model actually used is honest:
    it is exactly the `--model` we pass.
    """
    from ..bubble.runtime import digest

    def _run(workspace: Path, task_file: Path, output: Path, allowed: list[str], angle: str) -> dict:
        from ..bubble.write_tier_policy import assert_write_tier_enabled
        assert_write_tier_enabled("cascade.runner")
        prompt = task_file.read_text()
        argv = [executable, "-p", prompt, "--model", claude_model, "--permission-mode", "acceptEdits",
                "--add-dir", str(workspace)]
        started = time.monotonic()
        try:
            rc, stdout, stderr, timed_out = _run_bounded(argv, cwd=workspace, timeout=timeout)
        except OSError as exc:  # claude missing/unexecutable — fail-closed, never raise
            rc, stdout, stderr, timed_out = 127, "", f"claude not launchable: {exc}", False
        duration = round(time.monotonic() - started, 2)
        atomic_write_text(output, stdout)
        return {"ok": rc == 0 and not timed_out, "returncode": rc, "output": str(output),
                "output_sha256": digest(output) if output.exists() else None,
                "real_cost": 0.0, "duration_s": duration, "stderr": stderr[-2000:],
                "timed_out": timed_out}

    return _run


def _extract_real_cost(output: Path) -> float:
    """Best-effort: read a token/cost figure joao-glm may have written. Never invented."""
    if not output.exists():
        return 0.0
    try:
        for line in output.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            data = json.loads(line)
            for key in ("cost", "total_cost", "tokens", "total_tokens"):
                if isinstance(data.get(key), (int, float)):
                    return float(data[key])
    except (json.JSONDecodeError, OSError):
        return 0.0
    return 0.0


# ─────────────────────────── the wired builder ───────────────────────────
class CascadeBuilder:
    """A `BuilderAdapter` that routes each mission through the real cost cascade.

    Injectable seams (`glm_runner`, `claude_runner`, `test_runner`, `deterministic`, `judge`)
    default to the real subprocesses; tests pass fakes to exercise the isolation/judge/cost
    logic hermetically.
    """
    provider = "cascade"
    model = "glm-cascade+claude-fallback"

    def __init__(self, *, glm_executable: Path = Path("~/.local/bin/joao-glm").expanduser(),
                 claude_executable: str = "claude", n: int = 3,
                 angles: tuple[str, ...] = DEFAULT_ANGLES, timeout: int = 900,
                 claude_model: str = "sonnet",
                 deterministic: Optional[Callable] = None, judge: Optional[Callable] = None,
                 glm_runner: Optional[Callable] = None, claude_runner: Optional[Callable] = None,
                 test_runner: Optional[Callable] = None):
        self.glm_executable = Path(glm_executable)
        self.claude_executable = claude_executable
        self.claude_model = claude_model  # B-24: "sonnet" build / "haiku" smoke (never Opus)
        self.n = n
        self.angles = angles
        self.timeout = timeout
        self.deterministic = deterministic
        self.judge = judge or _readability_tiebreak_judge
        self.glm_runner = glm_runner or real_glm_runner(self.glm_executable, timeout)
        self._claude_runner_injected = claude_runner
        self.claude_runner = claude_runner or real_claude_runner(claude_executable, max(timeout, 1200), claude_model)
        self.test_runner = test_runner or _default_test_runner

    # BuilderAdapter API — RunRuntime calls this exactly like GLMBuilder.build
    def build(self, mission: str, workspace: Path, run_dir: Path, allowed: list[str],
              correction: bool) -> dict:
        workspace, run_dir = Path(workspace), Path(run_dir)
        run = json.loads((run_dir / "run.json").read_text())
        test_cmds = [list(cmd) for cmd in (run.get("targeted_tests") or []) + (run.get("full_tests") or [])]
        task = TaskSpec(task_id=run.get("run_id", "task"), prompt=mission,
                        critical=bool(run.get("critical")), recurrence=bool(run.get("recurrence")),
                        gate_failed=bool(correction), tags=list(run.get("tags") or []))
        git = WorkspaceGit(workspace)
        from ..bubble.write_tier_policy import assert_write_tier_enabled
        assert_write_tier_enabled("CascadeBuilder.build")
        cascade_dir = run_dir / "cascade"
        cascade_dir.mkdir(parents=True, exist_ok=True)
        base = git.snapshot_base(cascade_dir / "_base-untracked")  # preserves pre-existing WIP (P2-A)
        captures: list[dict] = []

        def _worker(provider: str, angle: str, runner: Callable, unit_cost: float) -> WorkerResult:
            git.reset_to_base(base)
            index = len(captures)
            label = provider + (f"-{angle}" if angle else "")
            cdir = cascade_dir / f"cand-{index:02d}-{label}"
            cdir.mkdir(parents=True, exist_ok=True)
            angle_hint = ("" if not angle else
                          f"\n\n## ANGLE PRIORITAIRE POUR CETTE VARIANTE : {angle}\n"
                          f"Produis une solution correcte d'abord ; à correction égale, optimise « {angle} ».\n")
            # `mission` already carries the RÈGLES ACTIVES block from RunRuntime._inject → this
            # archived task file IS the per-worker proof that memory reached THIS candidate.
            atomic_write_text(cdir / "builder-task.md", mission + angle_hint)
            output = cdir / "worker-output.jsonl"
            report = runner(workspace, cdir / "builder-task.md", output, allowed, angle)
            capture = git.capture_changes(cdir / "changes")
            captures.append({"index": index, "dir": cdir, "capture": capture, "angle": angle,
                             "provider": provider, "report": report})
            atomic_write_json(cdir / "worker-report.json",
                              {"provider": provider, "angle": angle, "ok": bool(report.get("ok")),
                               "returncode": report.get("returncode"),
                               "duration_s": report.get("duration_s"),
                               "real_cost": report.get("real_cost", 0.0),
                               "changed_files": capture["files"], "deletions": capture["deletions"]})
            result = WorkerResult(provider=label, angle=angle, cost=unit_cost,
                                  ok=bool(report.get("ok")), text=f"{label} rc={report.get('returncode')}")
            result._cap_index = index  # type: ignore[attr-defined]
            return result

        # B-24: a smoke mission uses the cheap Claude tier (haiku); a real build uses sonnet.
        claude_runner = self.claude_runner
        if self._claude_runner_injected is None and run.get("smoke"):
            claude_runner = real_claude_runner(self.claude_executable, max(self.timeout, 1200), "haiku")

        def glm(_task: TaskSpec, angle: str) -> WorkerResult:
            return _worker("glm", angle, self.glm_runner, COST_GLM)

        def claude(_task: TaskSpec) -> WorkerResult:
            return _worker("claude", "", claude_runner, COST_CLAUDE)

        def verify(_task: TaskSpec, _result: WorkerResult) -> tuple[bool, float]:
            # objective tests FIRST, on the candidate that is currently applied to the worktree
            return self._verify(workspace, test_cmds, captures[-1] if captures else None)

        deterministic = None
        if self.deterministic is not None:
            def deterministic(_task: TaskSpec) -> Optional[WorkerResult]:  # noqa: E306
                git.reset_to_base(base)
                index = len(captures)
                cdir = cascade_dir / f"cand-{index:02d}-deterministic"
                cdir.mkdir(parents=True, exist_ok=True)
                produced = self.deterministic(_task, workspace, allowed)
                if produced is None:
                    return None
                capture = git.capture_changes(cdir / "changes")
                captures.append({"index": index, "dir": cdir, "capture": capture, "angle": "",
                                 "provider": "deterministic", "report": {"ok": True, "real_cost": 0.0}})
                result = WorkerResult(provider="deterministic", cost=0.0, ok=True,
                                      text="deterministic tool")
                result._cap_index = index  # type: ignore[attr-defined]
                return result

        cascade = Cascade(glm=glm, claude=claude, verify=verify, judge=self._judge_adapter,
                          deterministic=deterministic, n=self.n, angles=self.angles)
        try:
            result = cascade.route(task)
        except Exception as exc:  # any worker/git crash → fail-closed BLOCK, worktree restored
            git.reset_to_base(base)
            atomic_write_json(run_dir / "cascade-error.json",
                              {"error": f"{type(exc).__name__}: {exc}", "captures": len(captures)})
            return {"ok": False, "provider": self.provider, "model": self.model, "blocked": True,
                    "tier": Tier.BLOCKED, "cascade_tier": Tier.BLOCKED,
                    "reason": f"cascade crashed → fail-closed BLOCK (worktree restored): "
                              f"{type(exc).__name__}: {exc}"}

        mission_cost = MissionCost()
        mission_cost.record(result)
        real_total = round(sum(c["report"].get("real_cost", 0.0) for c in captures), 4)
        atomic_write_json(run_dir / "cascade-decision.json", result.to_dict())
        atomic_write_json(run_dir / "cascade-cost.json", {
            **mission_cost.report(),
            "real_cost_signal": real_total,
            "worker_durations_s": [c["report"].get("duration_s") for c in captures],
            "direct_claude_proxy": COST_CLAUDE,
            "cascade_proxy_cost": result.total_cost,
            "savings_vs_direct_claude": round(COST_CLAUDE - result.total_cost, 3),
            "note": "proxy cost units (GLM=1, Claude=20); real_cost_signal = tokens/cost joao-glm "
                    "reported (0.0 if it reported none). $ not instrumented (L9).",
        })
        atomic_write_json(run_dir / "cascade-injection.json", {
            "funnel": "RunRuntime._inject('builder') → prefixed to mission → EACH candidate task",
            "block_present_in_mission": mission.startswith("🧠 RÈGLES ACTIVES") or "RÈGLES ACTIVES" in mission[:400],
            "candidates": [{"index": c["index"], "provider": c["provider"], "angle": c["angle"],
                            "task_file": str((c["dir"] / "builder-task.md"))} for c in captures],
        })

        if result.blocked or result.winner is None:
            git.reset_to_base(base)  # never leave a failed candidate applied
            return {"ok": False, "provider": self.provider, "model": self.model, "blocked": True,
                    "tier": result.tier, "cascade_tier": result.tier,
                    "reason": "cascade fail-closed: no tier could be objectively verified → BLOCKED",
                    "cascade": result.to_dict(), "cost": result.total_cost, "real_cost_signal": real_total}

        # a custom judge could return a non-candidate object → guard, never crash (P3-A)
        win_i = getattr(result.winner, "_cap_index", None)
        if win_i is None or not (0 <= win_i < len(captures)):
            git.reset_to_base(base)
            return {"ok": False, "provider": self.provider, "model": self.model, "blocked": True,
                    "tier": Tier.BLOCKED, "cascade_tier": Tier.BLOCKED,
                    "reason": "cascade fail-closed: winner is not a tracked candidate (bad judge)",
                    "cascade": result.to_dict()}
        git.reset_to_base(base)
        winner_capture = captures[win_i]
        git.apply_capture(winner_capture["dir"] / "changes", winner_capture["capture"])
        return {"ok": True, "provider": self._winner_provider(result), "model": self._winner_model(result),
                "tier": result.tier, "cascade_tier": result.tier, "cost": result.total_cost,
                "real_cost_signal": winner_capture["report"].get("real_cost", 0.0),
                "output": str(winner_capture["dir"] / "worker-output.jsonl"),
                "winner_angle": winner_capture["angle"], "cascade": result.to_dict()}

    # ── verification: objective tests first ──
    def _verify(self, workspace: Path, test_cmds: list[list[str]], capture: Optional[dict]) -> tuple[bool, float]:
        if not test_cmds:
            # no objective test to trust → cannot verify → fail-closed (escalate)
            return False, 0.0
        results = [self.test_runner(list(cmd), workspace, self.timeout) for cmd in test_cmds]
        passed = all(r.get("ok") for r in results)
        fraction = sum(1 for r in results if r.get("ok")) / len(results)
        # deterministic secondary score breaks near-ties WITHOUT an LLM (smaller change = tidier)
        secondary = _tidiness_score(capture) if capture else 0.0
        return passed, round(fraction + secondary, 6)

    def _judge_adapter(self, task: TaskSpec, candidates: list[WorkerResult]):
        return self.judge(task, candidates)

    @staticmethod
    def _winner_provider(result) -> str:
        return {Tier.DETERMINISTIC: "deterministic-tool", Tier.GLM_SOLO: "zai-coding-plan",
                Tier.BEST_OF_N: "zai-coding-plan", Tier.CLAUDE: "claude-cli"}.get(result.tier, "cascade")

    @staticmethod
    def _winner_model(result) -> str:
        return {Tier.DETERMINISTIC: DETERMINISTIC_MODEL, Tier.GLM_SOLO: GLM_MODEL,
                Tier.BEST_OF_N: GLM_MODEL, Tier.CLAUDE: CLAUDE_MODEL}.get(result.tier, "cascade")


def _default_test_runner(argv: list[str], cwd: Path, timeout: int) -> dict:
    from ..bubble.runtime import LocalTestRunner
    return LocalTestRunner().run(argv, Path(cwd), timeout)


def _tidiness_score(capture: dict) -> float:
    """Tiny deterministic tie-break in [0, 0.01): fewer changed files ranks marginally higher.

    Keeps the LLM judge for GENUINE ties only (identical tidiness) — builder ≠ judge, and no
    clock/LLM in the scoring path.
    """
    changed = len(capture.get("files", [])) + len(capture.get("deletions", []))
    return 0.0 if changed == 0 else round(0.01 / (1 + changed), 6)


def _readability_tiebreak_judge(task: TaskSpec, candidates: list[WorkerResult]):
    """Default judge for a genuine tie the objective tests could not break.

    Deterministic and independent of the builders: pick the tied candidate whose angle sorts
    first (stable), and say so. A real LLM judge can be injected via `CascadeBuilder(judge=...)`
    — it is only ever consulted on a true tie (never on the common unique-winner path).
    """
    winner = sorted(candidates, key=lambda c: (c.provider, c.angle))[0]
    return winner, {"reason": f"tie broken deterministically on angle {winner.angle!r}",
                    "judge": "deterministic-independent", "builder_is_judge": False}
