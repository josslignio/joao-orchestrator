"""Fail-closed local runtime.  Product data never belongs here."""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from ..domain.models import ProjectProfile
from ..policy.paths import detect_path_violations
from ..storage.atomic import FileLock, LockAcquireError, append_line, atomic_write_json, atomic_write_text


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
    RunStatus.READY: {RunStatus.BUILDING, RunStatus.BLOCKED, RunStatus.PAUSED, RunStatus.STOPPED},
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


class CodexBuilder(BuilderAdapter):
    """Use the authenticated local Codex CLI as one bounded build engine."""
    provider = "codex-subscription"; model = "local-codex-builder"

    def __init__(self, executable: str = "codex", timeout: int = 900):
        self.executable = executable
        self.timeout = timeout

    def available(self) -> bool:
        return bool(shutil.which(self.executable))

    def build(self, mission, workspace, run_dir, allowed, correction):
        task = run_dir / ("codex-correction-task.md" if correction else "codex-builder-task.md")
        output = run_dir / ("codex-correction-last-message.txt" if correction else "codex-builder-last-message.txt")
        atomic_write_text(task, mission)
        prompt = (
            "Act as JOAO's bounded builder. Work only in this Git workspace and only "
            f"in these paths: {', '.join(allowed)}. Do not install packages, access the "
            "network, commit, push, change policy/configuration, or modify any other path. "
            "Implement the following task and run the explicitly requested tests.\n\n"
            f"{mission}"
        )
        argv = [self.executable, "exec", "--json", "--sandbox", "workspace-write", "-C", str(workspace), "--output-last-message", str(output), prompt]
        try:
            proc = subprocess.run(argv, shell=False, capture_output=True, text=True, timeout=self.timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "provider": self.provider, "model": self.model, "reason": f"Codex builder unavailable: {exc}"}
        events = run_dir / ("codex-correction.jsonl" if correction else "codex-builder.jsonl")
        atomic_write_text(events, proc.stdout)
        return {"ok": proc.returncode == 0, "provider": self.provider, "model": self.model, "returncode": proc.returncode, "stdout": proc.stdout[-4000:], "stderr": proc.stderr[-4000:], "output": str(events), "output_sha256": digest(events), "last_message": str(output), "last_message_sha256": digest(output) if output.exists() else None}


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

    def review_stage(self, run, run_dir, stage: str):
        workspace = Path(run["workspace"])
        output = run_dir / f"codex-{stage}-review.jsonl"
        prompt = (
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
    def __init__(self, state_root: Path, *, builder: BuilderAdapter, reviewer: ReviewerAdapter | None = None, builders: dict[str, BuilderAdapter] | None = None, reviewers: dict[str, ReviewerAdapter] | None = None, tests: TestRunnerAdapter | None = None, memory: MemoryAdapter | None = None, profiles: ProjectProfileAdapter | None = None):
        self.root = Path(state_root).expanduser(); self.builder = builder
        self.reviewer = reviewer or CodexEvidenceReviewer()
        self.builders = builders or {"default": builder}
        self.reviewers = reviewers or {"codex": self.reviewer}
        self.tests = tests or LocalTestRunner()
        self.memory = memory or LocalMemoryAdapter(self.root / "memory"); self.profiles = profiles or LocalProfileAdapter()
        self._run_locks: dict[str, threading.RLock] = {}
        self._run_locks_guard = threading.RLock()
        self._control_requests: dict[str, str] = {}
        self._control_lock = threading.RLock()
    def _dir(self, run_id: str) -> Path: return self.root / "runs" / run_id
    def _run_lock(self, run_id: str) -> threading.RLock:
        with self._run_locks_guard:
            return self._run_locks.setdefault(run_id, threading.RLock())
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
    def start(self, *, project_id: str, workspace: Path, mission: str, targeted_tests: list[list[str]], full_tests: list[list[str]], profile: ProjectProfile | None = None, codex_review: bool = True, builder_name: str | None = None, reviewer_names: list[str] | None = None) -> str:
        workspace = Path(workspace).resolve()
        if not workspace.is_dir() or not (workspace / ".git").exists(): raise RuntimeStateError("workspace must be a local Git worktree")
        if not mission.strip(): raise RuntimeStateError("mission cannot be empty")
        if not full_tests: raise RuntimeStateError("at least one explicit full-test command is required")
        profile = profile or self.profiles.load(project_id, workspace)
        builder_name = builder_name or next(iter(self.builders))
        if builder_name not in self.builders:
            raise RuntimeStateError("requested builder is not configured: " + builder_name)
        reviewer_names = reviewer_names if reviewer_names is not None else (["codex"] if codex_review else [])
        if not reviewer_names:
            raise RuntimeStateError("at least one independent reviewer is required")
        unknown_reviewers = sorted(set(reviewer_names) - set(self.reviewers))
        if unknown_reviewers:
            raise RuntimeStateError("requested reviewer is not configured: " + ", ".join(unknown_reviewers))
        if Path(profile.repository_root).resolve() != workspace: raise RuntimeStateError("profile workspace mismatch")
        baseline_violations = detect_path_violations(self._paths(workspace), profile)
        if baseline_violations:
            raise RuntimeStateError("workspace has forbidden or out-of-scope drift: " + "; ".join(baseline_violations))
        run_id = f"run-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{secrets.token_hex(4)}"; folder = self._dir(run_id); folder.mkdir(parents=True)
        tasks = [{"id": "plan", "status": "pending"}, {"id": "build", "status": "pending", "depends_on": ["plan"]}, {"id": "test", "status": "pending", "depends_on": ["build"]}, {"id": "review", "status": "pending", "depends_on": ["test"]}]
        try:
            workspace.relative_to((self.root / "sandboxes").resolve())
            isolated_workspace = True
        except ValueError:
            isolated_workspace = False
        run = {"schema_version": 1, "run_id": run_id, "project_id": project_id, "workspace": str(workspace), "mission": mission, "status": "pending", "created_at": now(), "updated_at": now(), "current_step": "created", "profile": profile.to_dict(), "targeted_tests": targeted_tests, "full_tests": full_tests, "builder_name": builder_name, "reviewer_names": reviewer_names, "isolated_workspace": isolated_workspace, "codex_review": "codex" in reviewer_names, "corrections_used": 0, "max_corrections": 1, "tasks": tasks}
        atomic_write_text(folder / "mission.md", mission + "\n"); atomic_write_json(folder / "project-profile.json", profile.to_dict())
        atomic_write_json(folder / "task-graph.json", {"tasks": tasks}); atomic_write_json(folder / "plan.json", {"status": "pending", "bounded": True, "max_corrections": 1}); self._write(run)
        selected_builder = self.builders[builder_name]
        self._event(run, "run_created", builder=builder_name, builder_provider=selected_builder.provider, builder_model=selected_builder.model, reviewers=reviewer_names); self._checkpoint(run)
        self._transition(run, RunStatus.PLANNING, "load profile and local memory")
        atomic_write_json(folder / "memory.json", self.memory.load(project_id)); atomic_write_json(folder / "plan.json", {"status": "ready", "mission_sha256": hashlib.sha256(mission.encode()).hexdigest(), "bounded": True, "max_corrections": 1})
        run["tasks"][0]["status"] = "completed"; self._transition(run, RunStatus.READY, "bounded plan created"); return run_id
    def get(self, run_id: str) -> dict[str, Any]:
        run = self._read(run_id)
        with self._control_lock:
            if request := self._control_requests.get(run_id):
                run["control_request"] = request
                run["current_step"] = f"{request} requested; applying at the next safe checkpoint"
        return run
    def events(self, run_id: str) -> list[dict[str, Any]]: return self._events(self._read(run_id)).read()
    def pause(self, run_id: str) -> dict[str, Any]:
        lock = self._run_lock(run_id)
        if not lock.acquire(blocking=False):
            return self._queue_control(run_id, "pause")
        try:
            run = self._read(run_id); self._pause_at_checkpoint(run, "user pause", skip_builder=run["status"] != "ready"); return run
        finally:
            lock.release()
    def resume(self, run_id: str) -> dict[str, Any]:
        with self._run_lock(run_id):
            run = self._read(run_id)
            if run["status"] != "paused": raise RuntimeStateError("only paused run can resume")
            target = RunStatus.BUILDING if run.get("resume_skip_builder") else RunStatus.READY
            self._transition(run, target, "user resume from a safe checkpoint"); return run
    def stop(self, run_id: str) -> dict[str, Any]:
        lock = self._run_lock(run_id)
        if not lock.acquire(blocking=False):
            return self._queue_control(run_id, "stop")
        try:
            run = self._read(run_id); self._transition(run, RunStatus.STOPPED, "user stop"); self._finalize(run); return run
        finally:
            lock.release()
    def approve(self, run_id: str) -> dict[str, Any]:
        with self._run_lock(run_id):
            run = self._read(run_id)
            if not run.get("review_verified"):
                self._event(run, "approval_refused", reason="independent review proof is absent or mismatched")
                raise RuntimeStateError("cannot approve without matching independent review proof")
            self._transition(run, RunStatus.ACCEPTED, "human approval"); self._finalize(run); return run
    def reject(self, run_id: str) -> dict[str, Any]: return self.stop(run_id)
    def _queue_control(self, run_id: str, request: str) -> dict[str, Any]:
        with self._control_lock:
            run = self._read(run_id)
            if run["status"] in {"needs_approval", "accepted", "stopped", "blocked", "failed"}:
                if request == "stop" and run["status"] == "needs_approval":
                    self._transition(run, RunStatus.STOPPED, "stop requested at final approval boundary")
                    self._finalize(run)
                return run
            self._control_requests[run_id] = request
        run["control_request"] = request
        run["current_step"] = f"{request} requested; applying at the next safe checkpoint"
        return run
    def _apply_control(self, run: dict[str, Any]) -> bool:
        with self._control_lock:
            request = self._control_requests.pop(run["run_id"], None)
        if request is None:
            return False
        if request == "pause":
            self._pause_at_checkpoint(run, "pause requested at safe checkpoint", skip_builder=run["status"] != "ready")
        else:
            self._transition(run, RunStatus.STOPPED, "stop requested at safe checkpoint")
            self._finalize(run)
        return True
    def _pause_at_checkpoint(self, run: dict[str, Any], reason: str, *, skip_builder: bool) -> None:
        run["resume_skip_builder"] = skip_builder
        self._write(run)
        self._transition(run, RunStatus.PAUSED, reason)
    def _paths(self, workspace: Path) -> list[str]:
        raw = subprocess.run(["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"], cwd=str(workspace), shell=False, capture_output=True, text=True, check=True).stdout
        return sorted({item[3:].replace("\\\\", "/") for item in raw.split("\0") if item})
    def _finalize(self, run: dict[str, Any]) -> None:
        folder = self._dir(run["run_id"]); atomic_write_json(folder / "final-status.json", {"status": run["status"], "last_checkpoint": run.get("last_checkpoint")})
        files = sorted(path for path in folder.rglob("*") if path.is_file() and path.name != "manifest.json")
        atomic_write_json(folder / "manifest.json", {"schema_version": 1, "run_id": run["run_id"], "files": [{"path": str(path.relative_to(folder)), "sha256": digest(path), "bytes": path.stat().st_size} for path in files]})
    def _review_gate(self, run: dict[str, Any], stage: str) -> dict[str, Any]:
        folder = self._dir(run["run_id"])
        reviews = []
        for name in run.get("reviewer_names", ["codex"]):
            reviewer = self.reviewers[name]
            try:
                if hasattr(reviewer, "review_stage"):
                    result = reviewer.review_stage(run, folder, stage)
                elif stage == "final":
                    result = reviewer.review(run, folder)
                else:
                    result = {"ok": False, "decision": "block", "stage": stage, "reason": "reviewer does not implement this required stage"}
                if not isinstance(result, dict):
                    raise TypeError("reviewer result must be a dictionary")
            except Exception as exc:
                result = {"ok": False, "decision": "block", "stage": stage, "reason": f"reviewer exception: {type(exc).__name__}: {exc}"}
            result["reviewer"] = name
            reviews.append(result)
            self._event(run, "review_completed", stage=stage, reviewer=name, provider=getattr(reviewer, "provider", "unknown"), decision=result.get("decision", "block"))
        decisions = {item.get("decision", "block") for item in reviews}
        if not decisions <= {"pass", "p1", "block"}:
            decisions.add("block")
        ok = all(item.get("ok") for item in reviews)
        decision = "block" if "block" in decisions or not ok else "p1" if "p1" in decisions else "pass"
        proofs = [item.get("proof", {}) for item in reviews]
        verified = all(isinstance(proof, dict) and proof.get("verdict") == "ACCEPT" and proof.get("reviewed_diff_sha256") == run.get("final_diff_sha256") for proof in proofs)
        review = {"ok": ok, "decision": decision, "stage": stage, "reviews": reviews, "proof": {"verdict": "ACCEPT" if verified else "MISSING", "reviewed_diff_sha256": run.get("final_diff_sha256") if verified else None}}
        atomic_write_json(folder / f"{stage}-review-evidence.json", review)
        return review

    def run_once(self, run_id: str) -> dict[str, Any]:
        with self._run_lock(run_id):
            return self._run_once(run_id)
    def _run_once(self, run_id: str) -> dict[str, Any]:
        run = self._read(run_id)
        if run["status"] == "paused" or run["status"] == "needs_approval": return run
        if run["status"] == "building" and run.get("resume_skip_builder"):
            run["resume_skip_builder"] = False; self._write(run)
            return self._execute(run, False, building=True, skip_builder=True)
        if run["status"] == "correcting":
            self._transition(run, RunStatus.BUILDING, "correction build")
            return self._execute(run, True, building=True)
        if run["status"] in {"failed", "blocked"}: return self.retry(run_id)
        if run["status"] != "ready": raise RuntimeStateError("run cannot execute from " + run["status"])
        plan_review = self._review_gate(run, "plan")
        if not plan_review.get("ok"):
            self._transition(run, RunStatus.BLOCKED, "Codex plan review blocked"); self._finalize(run); return run
        if self._apply_control(run):
            return run
        return self._execute(run, False)
    def retry(self, run_id: str) -> dict[str, Any]:
        with self._run_lock(run_id):
            run = self._read(run_id)
            if run["corrections_used"] >= run["max_corrections"]:
                if run["status"] not in {"needs_approval", "failed", "blocked"}:
                    self._transition(run, RunStatus.NEEDS_APPROVAL, "correction budget exhausted")
                else:
                    self._event(run, "repair_budget_exhausted", terminal_status=run["status"])
                return run
            self._transition(run, RunStatus.CORRECTING, "bounded repair"); run["corrections_used"] += 1; self._write(run); self._transition(run, RunStatus.BUILDING, "correction build")
            return self._execute(run, True, building=True)
    def _execute(self, run: dict[str, Any], correction: bool, building: bool = False, skip_builder: bool = False) -> dict[str, Any]:
        workspace, folder = Path(run["workspace"]), self._dir(run["run_id"])
        profile = ProjectProfile(**{key: value for key, value in run["profile"].items() if key in ProjectProfile.__dataclass_fields__})
        if not building: self._transition(run, RunStatus.BUILDING, "builder dispatch")
        if run["builder_name"] == "codex" and not run.get("isolated_workspace"):
            self._transition(run, RunStatus.BLOCKED, "Codex builder requires an isolated workspace"); self._finalize(run); return run
        if skip_builder:
            builder_path, changed_path = folder / "builder-evidence.json", folder / "changed-paths.json"
            if not builder_path.exists() or not changed_path.exists():
                self._transition(run, RunStatus.BLOCKED, "missing checkpoint evidence for resume"); self._finalize(run); return run
            builder = json.loads(builder_path.read_text())
            violations = json.loads(changed_path.read_text()).get("violations", [])
        else:
            try:
                with FileLock(folder / "builder", timeout=.01):
                    before = self._paths(workspace)
                    try:
                        builder = self.builders[run["builder_name"]].build(run["mission"], workspace, folder, profile.allowed_write_paths, correction)
                    except Exception as exc:
                        builder = {"ok": False, "provider": self.builders[run["builder_name"]].provider, "reason": f"builder exception: {type(exc).__name__}: {exc}"}
            except LockAcquireError:
                self._transition(run, RunStatus.BLOCKED, "second builder refused"); self._finalize(run); return run
            after = self._paths(workspace); changed = sorted(set(after) - set(before)); violations = detect_path_violations(changed, profile)
            atomic_write_json(folder / "changed-paths.json", {"before": before, "after": after, "changed_by_builder": changed, "violations": violations})
            atomic_write_json(folder / "builder-evidence.json", builder)
        if self._apply_control(run):
            return run
        if not builder.get("ok") or violations:
            self._transition(run, RunStatus.BLOCKED, "builder failed or outside scope"); self._finalize(run); return run
        patch = subprocess.run(["git", "diff", "--binary", "--no-ext-diff", "--no-textconv"], cwd=str(workspace), shell=False, capture_output=True, check=True).stdout
        atomic_write_text(folder / "final-diff.patch", patch.decode(errors="replace")); run["final_diff_sha256"] = digest(folder / "final-diff.patch"); run["tasks"][1]["status"] = "completed"; self._write(run)
        build_review = self._review_gate(run, "build")
        if self._apply_control(run):
            return run
        if build_review.get("decision") == "p1" and run["corrections_used"] < run["max_corrections"]:
            self._transition(run, RunStatus.CORRECTING, "Codex build review P1; one repair permitted"); return run
        if not build_review.get("ok"):
            self._transition(run, RunStatus.BLOCKED, "Codex build review blocked"); self._finalize(run); return run
        self._transition(run, RunStatus.TESTING, "targeted and full tests")
        results = [self.tests.run(argv, workspace, profile.command_timeout_seconds) for argv in run["targeted_tests"] + run["full_tests"]]; atomic_write_json(folder / "test-results.json", {"results": results, "all_passed": all(item["ok"] for item in results)})
        if self._apply_control(run):
            return run
        if not all(item["ok"] for item in results): self._transition(run, RunStatus.FAILED, "tests failed"); self._finalize(run); return run
        run["tasks"][2]["status"] = "completed"; self._transition(run, RunStatus.REVIEWING, "independent test review")
        test_review = self._review_gate(run, "test")
        if self._apply_control(run):
            return run
        if test_review.get("decision") == "p1" and run["corrections_used"] < run["max_corrections"]:
            self._transition(run, RunStatus.CORRECTING, "test review P1; one repair permitted"); return run
        if not test_review.get("ok") or test_review.get("decision") == "block":
            self._transition(run, RunStatus.BLOCKED, "test review blocked"); self._finalize(run); return run
        self._event(run, "test_review_passed")
        run["updated_at"] = now(); self._write(run)
        review = self._review_gate(run, "final"); atomic_write_json(folder / "review-evidence.json", review)
        with self._control_lock:
            if self._apply_control(run):
                return run
            proof = review.get("proof", {})
            run["review_verified"] = bool(review.get("ok") and proof.get("verdict") == "ACCEPT" and proof.get("reviewed_diff_sha256") == run["final_diff_sha256"])
            self._write(run)
            if review.get("decision") == "p1" and run["corrections_used"] < run["max_corrections"]:
                self._transition(run, RunStatus.CORRECTING, "P1; one repair permitted"); return run
            if not review.get("ok") or review.get("decision") == "block":
                self._transition(run, RunStatus.BLOCKED, "review blocked"); self._finalize(run); return run
            run["tasks"][3]["status"] = "completed"; self._transition(run, RunStatus.NEEDS_APPROVAL, "review completed; human decision"); self._finalize(run); return run
