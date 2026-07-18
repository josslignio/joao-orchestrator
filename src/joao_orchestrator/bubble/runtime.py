"""Fail-closed local runtime.  Product data never belongs here."""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from ..domain.models import ProjectProfile
from ..policy.paths import detect_path_violations
from ..storage.atomic import FileLock, LockAcquireError, append_line, atomic_write_json, atomic_write_text


def _memory_dir() -> Path:
    """Locate the B-28 brain (repo-root `memory/`), honouring an explicit override."""
    override = os.environ.get("JOAO_MEMORY_DIR")
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parents[3] / "memory"


_INJECTOR = None
_INJECTOR_LOADED = False


def _injector():
    """Lazily import the single injection authority (memory/inject.py). None if absent."""
    global _INJECTOR, _INJECTOR_LOADED
    if _INJECTOR_LOADED:
        return _INJECTOR
    _INJECTOR_LOADED = True
    mem = _memory_dir()
    if (mem / "inject.py").exists():
        if str(mem) not in sys.path:
            sys.path.insert(0, str(mem))
        import inject as _inject_mod  # noqa: PLC0415
        _INJECTOR = _inject_mod
    return _INJECTOR


_RETRO = None
_RETRO_LOADED = False


def _retro():
    """Lazily import the Phase-4 retro/loop module (memory/retro.py). None if absent."""
    global _RETRO, _RETRO_LOADED
    if _RETRO_LOADED:
        return _RETRO
    _RETRO_LOADED = True
    mem = _memory_dir()
    if (mem / "retro.py").exists():
        if str(mem) not in sys.path:
            sys.path.insert(0, str(mem))
        import retro as _retro_mod  # noqa: PLC0415
        _RETRO = _retro_mod
    return _RETRO


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1048576), b""):
            value.update(block)
    return value.hexdigest()


class RunStatus(str, Enum):
    PENDING = "pending"; PLANNING = "planning"; READY = "ready"
    BUILDING = "building"; TESTING = "testing"; REVIEWING = "reviewing"
    NEEDS_APPROVAL = "needs_approval"; CORRECTING = "correcting"
    PAUSED = "paused"; BLOCKED = "blocked"; FAILED = "failed"
    ACCEPTED = "accepted"; STOPPED = "stopped"


NEXT = {
    RunStatus.PENDING: {RunStatus.PLANNING, RunStatus.STOPPED},
    RunStatus.PLANNING: {RunStatus.READY, RunStatus.FAILED, RunStatus.STOPPED},
    RunStatus.READY: {RunStatus.BUILDING, RunStatus.PAUSED, RunStatus.STOPPED},
    RunStatus.BUILDING: {RunStatus.TESTING, RunStatus.BLOCKED, RunStatus.FAILED, RunStatus.PAUSED, RunStatus.STOPPED},
    RunStatus.TESTING: {RunStatus.REVIEWING, RunStatus.FAILED, RunStatus.BLOCKED, RunStatus.PAUSED, RunStatus.STOPPED},
    RunStatus.REVIEWING: {RunStatus.NEEDS_APPROVAL, RunStatus.CORRECTING, RunStatus.BLOCKED, RunStatus.FAILED, RunStatus.PAUSED, RunStatus.STOPPED},
    RunStatus.CORRECTING: {RunStatus.BUILDING, RunStatus.NEEDS_APPROVAL, RunStatus.BLOCKED, RunStatus.FAILED, RunStatus.STOPPED},
    RunStatus.NEEDS_APPROVAL: {RunStatus.ACCEPTED, RunStatus.CORRECTING, RunStatus.STOPPED},
    RunStatus.PAUSED: {RunStatus.READY, RunStatus.BUILDING, RunStatus.TESTING, RunStatus.REVIEWING, RunStatus.STOPPED},
    RunStatus.BLOCKED: {RunStatus.CORRECTING, RunStatus.NEEDS_APPROVAL, RunStatus.STOPPED},
    RunStatus.FAILED: {RunStatus.CORRECTING, RunStatus.STOPPED},
    RunStatus.ACCEPTED: set(), RunStatus.STOPPED: set(),
}


class RuntimeStateError(RuntimeError):
    pass


class BuilderAdapter(ABC):
    provider = "unknown"; model = "unknown"
    @abstractmethod
    def build(self, mission: str, workspace: Path, run_dir: Path, allowed: list[str], correction: bool) -> dict[str, Any]: ...


class ReviewerAdapter(ABC):
    provider = "unknown"; model = "unknown"
    @abstractmethod
    def review(self, run: dict[str, Any], run_dir: Path) -> dict[str, Any]: ...


class TestRunnerAdapter(ABC):
    @abstractmethod
    def run(self, argv: list[str], cwd: Path, timeout: int) -> dict[str, Any]: ...


class MemoryAdapter(ABC):
    @abstractmethod
    def load(self, project_id: str) -> dict[str, Any]: ...


class EventStoreAdapter(ABC):
    @abstractmethod
    def append(self, event: dict[str, Any]) -> None: ...
    @abstractmethod
    def read(self) -> list[dict[str, Any]]: ...


class ProjectProfileAdapter(ABC):
    @abstractmethod
    def load(self, project_id: str, workspace: Path) -> ProjectProfile: ...


class JsonlEvents(EventStoreAdapter):
    def __init__(self, path: Path): self.path = path
    def append(self, event: dict[str, Any]) -> None: append_line(self.path, json.dumps(event, sort_keys=True))
    def read(self) -> list[dict[str, Any]]:
        return [] if not self.path.exists() else [json.loads(x) for x in self.path.read_text().splitlines() if x.strip()]


class LocalMemoryAdapter(MemoryAdapter):
    def __init__(self, root: Path): self.root = root
    def load(self, project_id: str) -> dict[str, Any]:
        path = self.root / project_id / "memory.json"
        if not path.exists(): return {"schema_version": 1, "project_id": project_id, "entries": []}
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or data.get("schema_version") != 1: raise RuntimeStateError("unsupported memory schema")
        return data


class LocalProfileAdapter(ProjectProfileAdapter):
    def __init__(self, path: Path | None = None): self.path = path
    def load(self, project_id: str, workspace: Path) -> ProjectProfile:
        path = self.path or workspace / ".joao-profile.json"
        if not path.exists():
            return ProjectProfile(project_id=project_id, display_name=project_id, repository_root=str(workspace), allowed_write_paths=[], forbidden_paths=[])
        data = json.loads(path.read_text()); data.setdefault("project_id", project_id); data.setdefault("repository_root", str(workspace))
        allowed = set(ProjectProfile.__dataclass_fields__)
        return ProjectProfile(**{key: value for key, value in data.items() if key in allowed})


class LocalTestRunner(TestRunnerAdapter):
    def run(self, argv: list[str], cwd: Path, timeout: int) -> dict[str, Any]:
        try:
            proc = subprocess.run(argv, cwd=str(cwd), shell=False, capture_output=True, text=True, timeout=timeout)
            return {"argv": argv, "returncode": proc.returncode, "ok": proc.returncode == 0, "stdout": proc.stdout[-16000:], "stderr": proc.stderr[-16000:]}
        except subprocess.TimeoutExpired as exc:
            return {"argv": argv, "returncode": 124, "ok": False, "stdout": (exc.stdout or "")[-16000:], "stderr": (exc.stderr or "")[-16000:], "timed_out": True}


class SandboxBuilder(BuilderAdapter):
    provider = "sandbox"; model = "deterministic-fixture"
    def __init__(self, callback): self.callback = callback
    def build(self, mission, workspace, run_dir, allowed, correction): return self.callback(mission, workspace, correction)


class GLMBuilder(BuilderAdapter):
    provider = "zai-coding-plan"; model = "zai-coding-plan/glm-4.5-air"
    def __init__(self, executable: Path = Path("~/.local/bin/joao-glm").expanduser()): self.executable = executable
    def build(self, mission, workspace, run_dir, allowed, correction):
        task = run_dir / ("correction.md" if correction else "builder-task.md"); atomic_write_text(task, mission)
        output = run_dir / ("glm-correction.jsonl" if correction else "glm-builder.jsonl")
        argv = [str(self.executable), "--workspace", str(workspace), "--task-file", str(task), "--output", str(output), "--mode", "workspace-write", "--budget", "small"]
        for item in allowed: argv.extend(["--allowed-path", item])
        proc = subprocess.run(argv, shell=False, capture_output=True, text=True, timeout=900)
        return {"ok": proc.returncode == 0, "provider": self.provider, "model": self.model, "returncode": proc.returncode, "stdout": proc.stdout[-4000:], "stderr": proc.stderr[-4000:], "output": str(output), "output_sha256": digest(output) if output.exists() else None}


class CodexEvidenceReviewer(ReviewerAdapter):
    provider = "codex"; model = "independent-exact-sha"
    def review(self, run, run_dir):
        proof = run_dir / "review-import.json"
        if not proof.exists(): return {"ok": True, "decision": "approval", "required": True, "expected_diff_sha256": run.get("final_diff_sha256")}
        data = json.loads(proof.read_text())
        if data.get("reviewed_diff_sha256") != run.get("final_diff_sha256"): return {"ok": False, "decision": "block", "reason": "review proof diff mismatch"}
        return {"ok": data.get("verdict") == "ACCEPT", "decision": data.get("decision", "pass"), "proof": data}


class CodexCLIReviewer(ReviewerAdapter):
    """Run a real local Codex review, fail-closed on an ambiguous result."""
    provider = "codex-subscription"; model = "local-codex-review"

    def __init__(self, executable: str = "codex", timeout: int = 900):
        self.executable = executable
        self.timeout = timeout

    def available(self) -> bool:
        return bool(shutil.which(self.executable) and Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser().exists())

    def review_stage(self, run, run_dir, stage: str, active_rules: str = ""):
        workspace = Path(run["workspace"])
        output = run_dir / f"codex-{stage}-review.jsonl"
        rules_prefix = (active_rules + "\\n\\n") if active_rules else ""
        prompt = (
            f"{rules_prefix}"
            "You are the independent JOAO reviewer. Work read-only. Review the "
            f"{stage} gate for this bounded mission:\\n\\n{run['mission']}\\n\\n"
            "Inspect only the current worktree, task evidence and git diff. Do not "
            "edit, commit, push, install packages or call external services. At the "
            "end, print exactly one final line: JOAO_REVIEW: ACCEPT, JOAO_REVIEW: P1, "
            "or JOAO_REVIEW: BLOCK. A P1 must name the concrete repair."
        )
        argv = [self.executable, "exec", "--json", "--sandbox", "read-only",
                "-C", str(workspace), prompt]
        try:
            proc = subprocess.run(argv, cwd=str(workspace), shell=False, capture_output=True,
                                  text=True, timeout=self.timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "decision": "block", "stage": stage,
                    "reason": f"Codex reviewer unavailable: {exc}"}
        atomic_write_text(output, proc.stdout)
        text = proc.stdout + "\\n" + proc.stderr
        match = re.findall(r"JOAO_REVIEW:\\s*(ACCEPT|P1|BLOCK)", text, flags=re.I)
        verdict = match[-1].upper() if match else ""
        decision = {"ACCEPT": "pass", "P1": "p1", "BLOCK": "block"}.get(verdict, "block")
        return {
            "ok": proc.returncode == 0 and verdict == "ACCEPT",
            "decision": decision, "stage": stage, "verdict": verdict or "MISSING",
            "returncode": proc.returncode, "output": str(output),
            "output_sha256": digest(output), "stderr": proc.stderr[-4000:],
            "proof": {"verdict": verdict, "reviewed_diff_sha256": run.get("final_diff_sha256")},
        }

    def review(self, run, run_dir):
        return self.review_stage(run, run_dir, "final")


def claude_capability() -> dict[str, Any]:
    """Report only a real local Claude CLI; never pretend it is connected."""
    executable = shutil.which("claude")
    return {
        "available": bool(executable),
        "provider": "claude-cli",
        "mode": "optional_secondary_review",
        "reason": "" if executable else "Claude CLI is not installed or not on PATH",
    }


class RunRuntime:
    """Persistent CP2 state machine, evidence writer and bounded repair loop."""
    def __init__(self, state_root: Path, *, builder: BuilderAdapter, reviewer: ReviewerAdapter | None = None, tests: TestRunnerAdapter | None = None, memory: MemoryAdapter | None = None, profiles: ProjectProfileAdapter | None = None):
        self.root = Path(state_root).expanduser(); self.builder = builder
        self.reviewer = reviewer or CodexEvidenceReviewer(); self.tests = tests or LocalTestRunner()
        self.memory = memory or LocalMemoryAdapter(self.root / "memory"); self.profiles = profiles or LocalProfileAdapter()
    def _dir(self, run_id: str) -> Path: return self.root / "runs" / run_id
    def _inject(self, run: dict[str, Any], role: str, *, files_touched: list[str] | None = None, stage: str | None = None):
        """Compose the role's RÈGLES ACTIVES block, persist it as evidence, log the ids.

        This is the ONE place memory reaches a role prompt — every launch path funnels here.
        If the memory subsystem is genuinely absent the gap is recorded loudly (no silent
        bypass) and an empty block is returned so a mis-installed package still runs.
        """
        folder = self._dir(run["run_id"])
        mod = _injector()
        if mod is None:
            self._event(run, "memory_injection_unavailable", role=role, stage=stage or "")
            from types import SimpleNamespace
            return SimpleNamespace(role=role, ids=[], block="", token_estimate=0)
        result = mod.build_injection(role, project=run.get("project_id", ""),
                                     mission_type=run.get("mission", ""),
                                     files_touched=files_touched)
        name = f"active-rules-{role}" + (f"-{stage}" if stage else "") + ".md"
        atomic_write_text(folder / name, result.block or "(no lessons matched)\n")
        self._event(run, "memory_injected", role=role, stage=stage or "",
                    injected_ids=result.ids, count=len(result.ids),
                    token_estimate=result.token_estimate)
        return result
    def _read(self, run_id: str) -> dict[str, Any]:
        path = self._dir(run_id) / "run.json"
        if not path.exists(): raise RuntimeStateError("unknown run: " + run_id)
        return json.loads(path.read_text())
    def _write(self, run: dict[str, Any]) -> None: atomic_write_json(self._dir(run["run_id"]) / "run.json", run)
    def _events(self, run: dict[str, Any]) -> JsonlEvents: return JsonlEvents(self._dir(run["run_id"]) / "events.jsonl")
    def _event(self, run: dict[str, Any], kind: str, **data) -> None:
        self._events(run).append({"event_id": secrets.token_hex(12), "at": now(), "kind": kind, "run_id": run["run_id"], "status": run["status"], **data})
    def _checkpoint(self, run: dict[str, Any]) -> None:
        folder = self._dir(run["run_id"]) / "checkpoints"; folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{len(list(folder.glob('*.json'))):04d}-{run['status']}.json"; atomic_write_json(path, run)
        run["last_checkpoint"] = str(path); self._write(run)
    def _transition(self, run: dict[str, Any], target: RunStatus, reason: str) -> None:
        old = RunStatus(run["status"])
        if target not in NEXT[old]:
            self._event(run, "invalid_transition", requested=target.value, reason=reason)
            raise RuntimeStateError(f"invalid transition {old.value} -> {target.value}")
        run.update({"status": target.value, "updated_at": now(), "current_step": reason}); self._write(run)
        self._event(run, "state_changed", from_status=old.value, to_status=target.value, reason=reason); self._checkpoint(run)
    def start(self, *, project_id: str, workspace: Path, mission: str, targeted_tests: list[list[str]], full_tests: list[list[str]], profile: ProjectProfile | None = None) -> str:
        workspace = Path(workspace).resolve()
        if not workspace.is_dir() or not (workspace / ".git").exists(): raise RuntimeStateError("workspace must be a local Git worktree")
        if not mission.strip(): raise RuntimeStateError("mission cannot be empty")
        if not full_tests: raise RuntimeStateError("at least one explicit full-test command is required")
        profile = profile or self.profiles.load(project_id, workspace)
        if Path(profile.repository_root).resolve() != workspace: raise RuntimeStateError("profile workspace mismatch")
        baseline_violations = detect_path_violations(self._paths(workspace), profile)
        if baseline_violations:
            raise RuntimeStateError("workspace has forbidden or out-of-scope drift: " + "; ".join(baseline_violations))
        run_id = f"run-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{secrets.token_hex(4)}"; folder = self._dir(run_id); folder.mkdir(parents=True)
        tasks = [{"id": "plan", "status": "pending"}, {"id": "build", "status": "pending", "depends_on": ["plan"]}, {"id": "test", "status": "pending", "depends_on": ["build"]}, {"id": "review", "status": "pending", "depends_on": ["test"]}]
        run = {"schema_version": 1, "run_id": run_id, "project_id": project_id, "workspace": str(workspace), "mission": mission, "status": "pending", "created_at": now(), "updated_at": now(), "current_step": "created", "profile": profile.to_dict(), "targeted_tests": targeted_tests, "full_tests": full_tests, "corrections_used": 0, "max_corrections": 1, "tasks": tasks}
        atomic_write_text(folder / "mission.md", mission + "\n"); atomic_write_json(folder / "project-profile.json", profile.to_dict())
        atomic_write_json(folder / "task-graph.json", {"tasks": tasks}); atomic_write_json(folder / "plan.json", {"status": "pending", "bounded": True, "max_corrections": 1}); self._write(run)
        self._event(run, "run_created", builder_provider=self.builder.provider, builder_model=self.builder.model); self._checkpoint(run)
        self._transition(run, RunStatus.PLANNING, "load profile and local memory")
        planner_rules = self._inject(run, "planner")
        atomic_write_json(folder / "memory.json", self.memory.load(project_id))
        atomic_write_json(folder / "plan.json", {"status": "ready", "mission_sha256": hashlib.sha256(mission.encode()).hexdigest(), "bounded": True, "max_corrections": 1, "injected_lesson_ids": planner_rules.ids})
        run["tasks"][0]["status"] = "completed"; self._transition(run, RunStatus.READY, "bounded plan created"); return run_id
    def get(self, run_id: str) -> dict[str, Any]: return self._read(run_id)
    def events(self, run_id: str) -> list[dict[str, Any]]: return self._events(self._read(run_id)).read()
    def pause(self, run_id: str) -> dict[str, Any]:
        run = self._read(run_id); self._transition(run, RunStatus.PAUSED, "user pause"); return run
    def resume(self, run_id: str) -> dict[str, Any]:
        run = self._read(run_id)
        if run["status"] != "paused": raise RuntimeStateError("only paused run can resume")
        prior = [event.get("from_status") for event in self.events(run_id) if event.get("to_status") == "paused"]
        self._transition(run, RunStatus(prior[-1] if prior else "ready"), "user resume"); return run
    def stop(self, run_id: str) -> dict[str, Any]:
        run = self._read(run_id); self._transition(run, RunStatus.STOPPED, "user stop"); self._finalize(run); return run
    def approve(self, run_id: str) -> dict[str, Any]:
        run = self._read(run_id)
        if not run.get("review_verified"):
            self._event(run, "approval_refused", reason="independent review proof is absent or mismatched")
            raise RuntimeStateError("cannot approve without matching independent review proof")
        self._transition(run, RunStatus.ACCEPTED, "human approval"); self._finalize(run); return run
    def reject(self, run_id: str) -> dict[str, Any]: return self.stop(run_id)
    def _paths(self, workspace: Path) -> list[str]:
        raw = subprocess.run(["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"], cwd=str(workspace), shell=False, capture_output=True, text=True, check=True).stdout
        return sorted({item[3:].replace("\\\\", "/") for item in raw.split("\0") if item})
    def _write_retro(self, run: dict[str, Any]) -> None:
        """Phase-4 hook: emit the retro template and record the run metric (state-local).

        Deliberately light — it never fabricates lessons (D-029: candidate ingestion is a
        separate, considered step). Brain runtime-state lives under this runtime's state_root.
        """
        mod = _retro()
        if mod is None:
            return
        mod.set_state_dir(self.root / "memory")
        folder = self._dir(run["run_id"]); status = run["status"]; project = run.get("project_id", "")
        template = mod.render_retro_template(project, run["run_id"], (run.get("mission", "")[:80] or "mission"),
                                             spec=run.get("mission", ""), result=f"status={status}")
        atomic_write_text(folder / "retro-template.md", template)
        if status in {"accepted", "stopped", "blocked", "failed"}:
            perfect = status == "accepted" and int(run.get("corrections_used", 0)) == 0 and bool(run.get("review_verified"))
            mod.record_run_metric(project, run["run_id"], perfect=perfect, at=now())
            self._event(run, "retro_recorded", perfect=perfect, runs_until_perfect=mod.runs_until_perfect(project))

    def _finalize(self, run: dict[str, Any]) -> None:
        folder = self._dir(run["run_id"]); atomic_write_json(folder / "final-status.json", {"status": run["status"], "last_checkpoint": run.get("last_checkpoint")})
        self._write_retro(run)
        files = sorted(path for path in folder.rglob("*") if path.is_file() and path.name != "manifest.json")
        atomic_write_json(folder / "manifest.json", {"schema_version": 1, "run_id": run["run_id"], "files": [{"path": str(path.relative_to(folder)), "sha256": digest(path), "bytes": path.stat().st_size} for path in files]})
    def _review_gate(self, run: dict[str, Any], stage: str) -> dict[str, Any]:
        folder = self._dir(run["run_id"])
        changed = []
        changed_path = folder / "changed-paths.json"
        if changed_path.exists():
            changed = json.loads(changed_path.read_text()).get("changed_by_builder", [])
        r_rules = self._inject(run, "reviewer", files_touched=changed, stage=stage)
        if hasattr(self.reviewer, "review_stage"):
            review = self.reviewer.review_stage(run, folder, stage, active_rules=r_rules.block)
        elif stage == "final":
            review = self.reviewer.review(run, folder)
        else:
            review = {"ok": True, "decision": "pass", "stage": stage, "skipped": "legacy reviewer"}
        atomic_write_json(folder / f"{stage}-review-evidence.json", review)
        self._event(run, "review_completed", stage=stage, provider=self.reviewer.provider,
                    decision=review.get("decision", "block"))
        return review

    def run_once(self, run_id: str) -> dict[str, Any]:
        run = self._read(run_id)
        if run["status"] == "paused" or run["status"] == "needs_approval": return run
        if run["status"] == "correcting":
            self._transition(run, RunStatus.BUILDING, "correction build")
            return self._execute(run, True, building=True)
        if run["status"] in {"failed", "blocked"}: return self.retry(run_id)
        if run["status"] != "ready": raise RuntimeStateError("run cannot execute from " + run["status"])
        plan_review = self._review_gate(run, "plan")
        if not plan_review.get("ok"):
            self._transition(run, RunStatus.BLOCKED, "Codex plan review blocked"); self._finalize(run); return run
        return self._execute(run, False)
    def retry(self, run_id: str) -> dict[str, Any]:
        run = self._read(run_id)
        if run["corrections_used"] >= run["max_corrections"]:
            if run["status"] != "needs_approval": self._transition(run, RunStatus.NEEDS_APPROVAL, "correction budget exhausted")
            return run
        self._transition(run, RunStatus.CORRECTING, "bounded repair"); run["corrections_used"] += 1; self._write(run); self._transition(run, RunStatus.BUILDING, "correction build")
        return self._execute(run, True, building=True)
    def _execute(self, run: dict[str, Any], correction: bool, building: bool = False) -> dict[str, Any]:
        workspace, folder = Path(run["workspace"]), self._dir(run["run_id"])
        profile = ProjectProfile(**{key: value for key, value in run["profile"].items() if key in ProjectProfile.__dataclass_fields__})
        if not building: self._transition(run, RunStatus.BUILDING, "builder dispatch")
        try:
            with FileLock(folder / "builder", timeout=.01):
                b_rules = self._inject(run, "builder", files_touched=profile.allowed_write_paths)
                mission_for_builder = f"{b_rules.block}\n\n---\n\n{run['mission']}" if b_rules.block else run["mission"]
                before = self._paths(workspace); builder = self.builder.build(mission_for_builder, workspace, folder, profile.allowed_write_paths, correction)
        except LockAcquireError:
            self._transition(run, RunStatus.BLOCKED, "second builder refused"); self._finalize(run); return run
        after = self._paths(workspace); changed = sorted(set(after) - set(before)); violations = detect_path_violations(changed, profile)
        atomic_write_json(folder / "changed-paths.json", {"before": before, "after": after, "changed_by_builder": changed, "violations": violations})
        atomic_write_json(folder / "builder-evidence.json", builder)
        if not builder.get("ok") or violations:
            self._transition(run, RunStatus.BLOCKED, "builder failed or outside scope"); self._finalize(run); return run
        patch = subprocess.run(["git", "diff", "--binary", "--no-ext-diff", "--no-textconv"], cwd=str(workspace), shell=False, capture_output=True, check=True).stdout
        atomic_write_text(folder / "final-diff.patch", patch.decode(errors="replace")); run["final_diff_sha256"] = digest(folder / "final-diff.patch"); run["tasks"][1]["status"] = "completed"; self._write(run)
        build_review = self._review_gate(run, "build")
        if build_review.get("decision") == "p1" and run["corrections_used"] < run["max_corrections"]:
            self._transition(run, RunStatus.CORRECTING, "Codex build review P1; one repair permitted"); return run
        if not build_review.get("ok"):
            self._transition(run, RunStatus.BLOCKED, "Codex build review blocked"); self._finalize(run); return run
        self._transition(run, RunStatus.TESTING, "targeted and full tests")
        results = [self.tests.run(argv, workspace, profile.command_timeout_seconds) for argv in run["targeted_tests"] + run["full_tests"]]; atomic_write_json(folder / "test-results.json", {"results": results, "all_passed": all(item["ok"] for item in results)})
        if not all(item["ok"] for item in results): self._transition(run, RunStatus.FAILED, "tests failed"); self._finalize(run); return run
        run["tasks"][2]["status"] = "completed"; self._transition(run, RunStatus.REVIEWING, "independent review")
        review = self._review_gate(run, "final"); atomic_write_json(folder / "review-evidence.json", review)
        proof = review.get("proof", {})
        run["review_verified"] = bool(review.get("ok") and proof.get("verdict") == "ACCEPT" and proof.get("reviewed_diff_sha256") == run["final_diff_sha256"])
        self._write(run)
        if review.get("decision") == "p1" and run["corrections_used"] < run["max_corrections"]: self._transition(run, RunStatus.CORRECTING, "P1; one repair permitted"); return run
        if not review.get("ok") or review.get("decision") == "block": self._transition(run, RunStatus.BLOCKED, "review blocked"); self._finalize(run); return run
        run["tasks"][3]["status"] = "completed"; self._transition(run, RunStatus.NEEDS_APPROVAL, "review completed; human decision"); self._finalize(run); return run
