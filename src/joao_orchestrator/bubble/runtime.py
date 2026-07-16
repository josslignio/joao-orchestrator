"""Fail-closed local runtime.  Product data never belongs here."""
from __future__ import annotations

import hashlib
import inspect
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


def resolve_executable(configured: str | Path, *, fallback: Path | None = None) -> str | None:
    """Resolve provider CLIs even when a macOS app has a minimal PATH."""
    candidate = Path(configured).expanduser()
    if candidate.is_absolute():
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    else:
        found = shutil.which(str(configured))
        if found:
            return found
    if fallback is not None:
        fallback = fallback.expanduser()
        if fallback.is_file() and os.access(fallback, os.X_OK):
            return str(fallback)
    return None


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
    @abstractmethod
    def preflight(self) -> dict[str, Any]: ...


class ReviewerAdapter(ABC):
    provider = "unknown"; model = "unknown"
    @abstractmethod
    def review(self, run: dict[str, Any], run_dir: Path) -> dict[str, Any]: ...
    @abstractmethod
    def preflight(self) -> dict[str, Any]: ...


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
    provider = "sandbox"; model = "deterministic-fixture"; real_or_mock = "mock"
    def __init__(self, callback): self.callback = callback
    def build(self, mission, workspace, run_dir, allowed, correction):
        parameters = inspect.signature(self.callback).parameters.values()
        positional = [item for item in parameters if item.kind in {item.POSITIONAL_ONLY, item.POSITIONAL_OR_KEYWORD}]
        has_varargs = any(item.kind == item.VAR_POSITIONAL for item in parameters)
        if has_varargs or len(positional) >= 5:
            return self.callback(mission, workspace, run_dir, allowed, correction)
        return self.callback(mission, workspace, correction)
    def preflight(self): return {"available": True, "executable": "sandbox://fixture", "model": self.model, "provider": self.provider, "real_or_mock": "mock", "reason": "Sandbox builder is for testing only"}


class GLMBuilder(BuilderAdapter):
    provider = "zai-coding-plan"; model = "zai-coding-plan/glm-4.5-air"
    def __init__(self, executable: Path = Path("~/.local/bin/joao-glm").expanduser()): self.executable = executable
    def preflight(self):
        found = resolve_executable(self.executable, fallback=Path("~/.local/bin/joao-glm"))
        if not found:
            return {"available": False, "executable": str(self.executable), "model": self.model, "provider": self.provider, "real_or_mock": "real", "reason": f"GLM executable not found at {self.executable}", "last_error": "Executable not found or not executable", "auth_status": "unknown", "config_status": "not_configured"}
        return {"available": True, "executable": found, "model": self.model, "provider": self.provider, "real_or_mock": "real", "reason": "GLM is available", "last_error": None, "auth_status": "adapter_managed", "config_status": "configured"}
    def build(self, mission, workspace, run_dir, allowed, correction):
        start_time = now()
        found = resolve_executable(self.executable, fallback=Path("~/.local/bin/joao-glm"))
        if not found:
            return {"ok": False, "provider": self.provider, "model": self.model, "reason": f"GLM executable not found at {self.executable}", "real_or_mock": "real", "timestamp_start": start_time, "timestamp_end": now(), "returncode": -1}
        task = run_dir / ("correction.md" if correction else "builder-task.md"); atomic_write_text(task, mission)
        output = run_dir / ("glm-correction.jsonl" if correction else "glm-builder.jsonl")
        argv = [found, "--workspace", str(workspace), "--task-file", str(task), "--output", str(output), "--mode", "workspace-write", "--budget", "small"]
        for item in allowed: argv.extend(["--allowed-path", item])
        try:
            proc = subprocess.run(argv, shell=False, capture_output=True, text=True, timeout=900)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "provider": self.provider, "model": self.model, "executable": found, "real_or_mock": "real", "reason": f"GLM execution failed: {exc}", "timestamp_start": start_time, "timestamp_end": now(), "returncode": -1, "last_error": str(exc)}
        return {"ok": proc.returncode == 0, "provider": self.provider, "model": self.model, "executable": found, "adapter_command": argv[:-1] if allowed else argv, "real_or_mock": "real", "returncode": proc.returncode, "stdout": proc.stdout[-4000:], "stderr": proc.stderr[-4000:], "output": str(output), "output_sha256": digest(output) if output.exists() else None, "timestamp_start": start_time, "timestamp_end": now(), "evidence_paths": [str(output), str(task)]}


class CodexBuilder(BuilderAdapter):
    """Use the authenticated local Codex CLI as one bounded build engine."""
    provider = "openai-codex-subscription"; model = "gpt-5.6-terra"

    def __init__(self, executable: str = "codex", timeout: int = 900, model: str = "gpt-5.6-terra"):
        self.executable = executable
        self.timeout = timeout
        self.model = model

    def preflight(self):
        found = resolve_executable(self.executable, fallback=Path("~/.local/bin/codex"))
        if not found:
            return {"available": False, "executable": self.executable, "model": self.model, "provider": self.provider, "real_or_mock": "real", "reason": f"Codex executable not found at {self.executable}", "last_error": "Executable not found or not executable", "auth_status": "unknown", "config_status": "not_configured"}
        try:
            login_check = subprocess.run([found, "login", "status"], shell=False, capture_output=True, text=True, timeout=3)
            authenticated = login_check.returncode == 0 and "Logged in" in (login_check.stdout + login_check.stderr)
            if not authenticated:
                return {"available": False, "executable": found, "model": self.model, "provider": self.provider, "real_or_mock": "real", "reason": "Codex is not authenticated (run 'codex login')", "last_error": "Authentication failed", "auth_status": "not_logged_in", "config_status": "configured"}
            return {"available": True, "executable": found, "model": self.model, "provider": self.provider, "real_or_mock": "real", "reason": "Codex is available and authenticated", "auth_status": "logged_in", "config_status": "configured", "last_error": None}
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"available": False, "executable": self.executable, "model": self.model, "provider": self.provider, "real_or_mock": "real", "reason": f"Codex preflight failed: {exc}", "last_error": str(exc), "auth_status": "error", "config_status": "error"}

    def available(self) -> bool:
        return self.preflight()["available"]

    def build(self, mission, workspace, run_dir, allowed, correction):
        start_time = now()
        found = resolve_executable(self.executable, fallback=Path("~/.local/bin/codex"))
        if not found:
            return {"ok": False, "provider": self.provider, "model": self.model, "executable": self.executable, "real_or_mock": "real", "reason": f"Codex executable not found at {self.executable}", "timestamp_start": start_time, "timestamp_end": now(), "returncode": -1, "last_error": "Executable not found"}
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
        argv = [found, "exec", "--json", "--ephemeral", "--model", self.model,
                "--sandbox", "workspace-write", "-C", str(workspace),
                "--output-last-message", str(output), prompt]
        try:
            proc = subprocess.run(argv, shell=False, capture_output=True, text=True, timeout=self.timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "provider": self.provider, "model": self.model, "executable": found, "real_or_mock": "real", "reason": f"Codex builder unavailable: {exc}", "timestamp_start": start_time, "timestamp_end": now(), "returncode": -1, "last_error": str(exc)}
        events = run_dir / ("codex-correction.jsonl" if correction else "codex-builder.jsonl")
        atomic_write_text(events, proc.stdout)
        return {"ok": proc.returncode == 0, "provider": self.provider, "model": self.model, "executable": found, "adapter_command": argv[:-1], "real_or_mock": "real", "returncode": proc.returncode, "stdout": proc.stdout[-4000:], "stderr": proc.stderr[-4000:], "output": str(events), "output_sha256": digest(events), "last_message": str(output), "last_message_sha256": digest(output) if output.exists() else None, "timestamp_start": start_time, "timestamp_end": now(), "evidence_paths": [str(events), str(output), str(task)]}


class CodexEvidenceReviewer(ReviewerAdapter):
    provider = "codex"; model = "independent-exact-sha"
    def preflight(self):
        return {"available": True, "executable": "evidence-import", "model": self.model, "provider": self.provider, "real_or_mock": "real", "reason": "Evidence-based reviewer always available"}
    def review(self, run, run_dir):
        proof = run_dir / "review-import.json"
        if not proof.exists(): return {"ok": True, "decision": "approval", "required": True, "expected_diff_sha256": run.get("final_diff_sha256"), "provider": self.provider, "model": self.model, "real_or_mock": "real"}
        data = json.loads(proof.read_text())
        if data.get("reviewed_diff_sha256") != run.get("final_diff_sha256"): return {"ok": False, "decision": "block", "reason": "review proof diff mismatch", "provider": self.provider, "model": self.model, "real_or_mock": "real"}
        return {"ok": data.get("verdict") == "ACCEPT", "decision": data.get("decision", "pass"), "proof": data, "provider": self.provider, "model": self.model, "real_or_mock": "real"}


class CodexCLIReviewer(ReviewerAdapter):
    """Run a real local Codex review, fail-closed on an ambiguous result."""
    provider = "openai-codex-subscription"; model = "gpt-5.6-terra"

    def __init__(self, executable: str = "codex", timeout: int = 900, model: str = "gpt-5.6-terra"):
        self.executable = executable
        self.timeout = timeout
        self.model = model

    def preflight(self):
        found = resolve_executable(self.executable, fallback=Path("~/.local/bin/codex"))
        if not found:
            return {"available": False, "executable": self.executable, "model": self.model, "provider": self.provider, "real_or_mock": "real", "reason": f"Codex executable not found at {self.executable}", "last_error": "Executable not found", "auth_status": "unknown", "config_status": "not_configured"}
        try:
            login_check = subprocess.run([found, "login", "status"], shell=False, capture_output=True, text=True, timeout=3)
            authenticated = login_check.returncode == 0 and "Logged in" in (login_check.stdout + login_check.stderr)
            if not authenticated:
                return {"available": False, "executable": found, "model": self.model, "provider": self.provider, "real_or_mock": "real", "reason": "Codex is not authenticated (run 'codex login')", "last_error": "Authentication failed", "auth_status": "not_logged_in", "config_status": "configured"}
            return {"available": True, "executable": found, "model": self.model, "provider": self.provider, "real_or_mock": "real", "reason": "Codex reviewer is available and authenticated", "auth_status": "logged_in", "config_status": "configured", "last_error": None}
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"available": False, "executable": self.executable, "model": self.model, "provider": self.provider, "real_or_mock": "real", "reason": f"Codex reviewer preflight failed: {exc}", "last_error": str(exc), "auth_status": "error", "config_status": "error"}

    def available(self) -> bool:
        return self.preflight()["available"]

    def review_stage(self, run, run_dir, stage: str):
        start_time = now()
        workspace = Path(run["workspace"])
        output = run_dir / f"codex-{stage}-review.jsonl"
        found = resolve_executable(self.executable, fallback=Path("~/.local/bin/codex"))
        if not found:
            return {"ok": False, "decision": "block", "stage": stage, "provider": self.provider, "model": self.model, "real_or_mock": "real", "reason": f"Codex executable not found at {self.executable}", "timestamp_start": start_time, "timestamp_end": now(), "returncode": -1, "last_error": "Executable not found"}
        prompt = (
            "You are the independent JOAO reviewer. Work read-only. Review the "
            f"{stage} gate for this bounded mission:\\n\\n{run['mission']}\\n\\n"
            f"The immutable local evidence directory is {run_dir}. "
            "Inspect only the current worktree, that evidence directory, and git diff. Do not "
            "edit, commit, push, install packages or call external services. At the "
            "end, print exactly one final line: JOAO_REVIEW: ACCEPT, JOAO_REVIEW: P1, "
            "or JOAO_REVIEW: BLOCK. A P1 must name the concrete repair."
        )
        argv = [found, "exec", "--json", "--ephemeral", "--model", self.model,
                "--sandbox", "read-only", "-C", str(workspace), prompt]
        try:
            proc = subprocess.run(argv, cwd=str(workspace), shell=False, capture_output=True,
                                  text=True, timeout=self.timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "decision": "block", "stage": stage,
                    "provider": self.provider, "model": self.model, "real_or_mock": "real",
                    "reason": f"Codex reviewer unavailable: {exc}",
                    "timestamp_start": start_time, "timestamp_end": now(), "returncode": -1, "last_error": str(exc)}
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
            "provider": self.provider, "model": self.model, "executable": found, "real_or_mock": "real",
            "adapter_command": argv[:-1],
            "timestamp_start": start_time, "timestamp_end": now(), "evidence_paths": [str(output)],
        }

    def review(self, run, run_dir):
        return self.review_stage(run, run_dir, "final")


class ClaudeCLIReviewer(ReviewerAdapter):
    """Optional real Claude CLI reviewer; absence is an explicit preflight refusal."""

    provider = "anthropic-claude-cli"; model = "claude-cli-configured-default"

    def __init__(self, executable: str = "claude", timeout: int = 900):
        self.executable = executable
        self.timeout = timeout

    def preflight(self) -> dict[str, Any]:
        found = resolve_executable(self.executable, fallback=Path("~/.local/bin/claude"))
        base = {
            "provider": self.provider, "model": self.model,
            "real_or_mock": "real", "mode": "optional_secondary_review",
            "executable": found,
        }
        if not found:
            return {**base, "available": False, "config_status": "not_configured",
                    "auth_status": "unknown", "last_error": "Executable not found",
                    "reason": "Claude CLI is not installed or not available to JOAO"}
        try:
            result = subprocess.run([found, "--version"], shell=False, capture_output=True,
                                    text=True, timeout=3)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {**base, "available": False, "config_status": "error",
                    "auth_status": "unknown", "last_error": str(exc),
                    "reason": f"Claude CLI preflight failed: {exc}"}
        if result.returncode != 0:
            error = (result.stderr or result.stdout)[-500:]
            return {**base, "available": False, "config_status": "error",
                    "auth_status": "unknown", "last_error": error,
                    "reason": "Claude CLI version check failed"}
        return {**base, "available": True, "config_status": "configured",
                "auth_status": "cli_available", "last_error": None,
                "reason": "Claude CLI is available; authentication is verified on invocation"}

    def review_stage(self, run, run_dir, stage: str) -> dict[str, Any]:
        started = now()
        found = resolve_executable(self.executable, fallback=Path("~/.local/bin/claude"))
        output = run_dir / f"claude-{stage}-review.json"
        if not found:
            return {"ok": False, "decision": "block", "stage": stage,
                    "provider": self.provider, "model": self.model,
                    "real_or_mock": "real", "returncode": -1,
                    "timestamp_start": started, "timestamp_end": now(),
                    "reason": "Claude CLI is unavailable", "last_error": "Executable not found"}
        prompt = (
            "You are a read-only JOAO reviewer. Review the " + stage + " gate for the mission below. "
            f"Inspect the current Git diff and local evidence in {run_dir}. Do not edit files, install packages, "
            "commit, push, or use the network. End with exactly CLAUDE_REVIEW: ACCEPT, "
            "CLAUDE_REVIEW: P1, or CLAUDE_REVIEW: BLOCK.\n\n" + run["mission"]
        )
        argv = [found, "--print", "--output-format", "json", prompt]
        try:
            result = subprocess.run(argv, cwd=run["workspace"], shell=False,
                                    capture_output=True, text=True, timeout=self.timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "decision": "block", "stage": stage,
                    "provider": self.provider, "model": self.model,
                    "real_or_mock": "real", "returncode": -1,
                    "timestamp_start": started, "timestamp_end": now(),
                    "reason": f"Claude reviewer failed: {exc}", "last_error": str(exc)}
        atomic_write_text(output, result.stdout)
        text = result.stdout + "\n" + result.stderr
        matches = re.findall(r"CLAUDE_REVIEW:\s*(ACCEPT|P1|BLOCK)", text, flags=re.I)
        verdict = matches[-1].upper() if matches else "MISSING"
        decision = {"ACCEPT": "pass", "P1": "p1", "BLOCK": "block"}.get(verdict, "block")
        return {
            "ok": result.returncode == 0 and verdict == "ACCEPT", "decision": decision,
            "stage": stage, "verdict": verdict, "returncode": result.returncode,
            "provider": self.provider, "model": self.model, "executable": found,
            "real_or_mock": "real", "timestamp_start": started, "timestamp_end": now(),
            "output": str(output), "output_sha256": digest(output),
            "proof": {"verdict": verdict, "reviewed_diff_sha256": run.get("final_diff_sha256")},
            "stderr": result.stderr[-4000:], "evidence_paths": [str(output)],
        }

    def review(self, run, run_dir):
        return self.review_stage(run, run_dir, "final")


def claude_capability() -> dict[str, Any]:
    return ClaudeCLIReviewer().preflight()


class RunRuntime:
    """Persistent CP2 state machine, evidence writer and bounded repair loop."""
    def __init__(self, state_root: Path, *, builder: BuilderAdapter, reviewer: ReviewerAdapter | None = None, builders: dict[str, BuilderAdapter] | None = None, reviewers: dict[str, ReviewerAdapter] | None = None, tests: TestRunnerAdapter | None = None, memory: MemoryAdapter | None = None, profiles: ProjectProfileAdapter | None = None, allow_test_adapters: bool = False):
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
        self.allow_test_adapters = allow_test_adapters
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
    def start(self, *, project_id: str, workspace: Path, mission: str, targeted_tests: list[list[str]], full_tests: list[list[str]], profile: ProjectProfile | None = None, codex_review: bool = True, builder_name: str | None = None, reviewer_names: list[str] | None = None, review_policy: str | None = None) -> str:
        workspace = Path(workspace).resolve()
        if not workspace.is_dir() or not (workspace / ".git").exists(): raise RuntimeStateError("workspace must be a local Git worktree")
        if not mission.strip(): raise RuntimeStateError("mission cannot be empty")
        if not full_tests: raise RuntimeStateError("at least one explicit full-test command is required")
        profile = profile or self.profiles.load(project_id, workspace)
        builder_name = builder_name or next(iter(self.builders))
        if builder_name not in self.builders:
            raise RuntimeStateError("requested builder is not configured: " + builder_name)

        aliases = {"independent": None, "self-review": "codex"}
        if review_policy in aliases:
            review_policy = aliases[review_policy]
        if review_policy == "none":
            reviewer_names = []
        elif reviewer_names is None:
            reviewer_names = ["codex"] if codex_review else []
        reviewer_names = list(reviewer_names or [])
        if review_policy is None:
            if reviewer_names == ["codex"]:
                review_policy = "codex"
            elif reviewer_names == ["claude"]:
                review_policy = "claude"
            elif reviewer_names == ["codex", "claude"]:
                review_policy = "codex_and_claude"
            elif not reviewer_names:
                review_policy = "none"
        expected_reviewers = {
            "none": [], "codex": ["codex"], "claude": ["claude"],
            "codex_and_claude": ["codex", "claude"],
        }
        if review_policy not in expected_reviewers or reviewer_names != expected_reviewers[review_policy]:
            raise RuntimeStateError("review policy and selected reviewers do not match")

        builder_instance = self.builders[builder_name]
        builder_preflight = builder_instance.preflight()
        if not builder_preflight.get("available"):
            raise RuntimeStateError(
                f"selected builder {builder_name} is unavailable: "
                f"{builder_preflight.get('reason', 'preflight failed')}"
            )
        if not self.allow_test_adapters and builder_preflight.get("real_or_mock") != "real":
            raise RuntimeStateError(f"Mock adapter {builder_name} is not allowed in production Bubble")

        reviewer_preflights: list[dict[str, Any]] = []
        for name in reviewer_names:
            if name not in self.reviewers:
                raise RuntimeStateError("requested reviewer is not configured: " + name)
            preflight = self.reviewers[name].preflight() if hasattr(self.reviewers[name], "preflight") else {
                "available": self.allow_test_adapters,
                "real_or_mock": "mock" if self.allow_test_adapters else "unknown",
                "reason": "reviewer does not expose preflight",
            }
            reviewer_preflights.append({name: preflight})
            if not preflight.get("available"):
                raise RuntimeStateError(
                    f"selected reviewer {name} is unavailable: {preflight.get('reason', 'preflight failed')}"
                )
            if not self.allow_test_adapters and preflight.get("real_or_mock") != "real":
                raise RuntimeStateError(f"Mock reviewer adapter {name} is not allowed in production Bubble")

        is_self_review = builder_name == "codex" and "codex" in reviewer_names
        review_semantics = "self-review" if is_self_review else (
            "stacked-independent" if len(reviewer_names) > 1 else
            "independent" if reviewer_names else "none"
        )

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

        # Persist the exact UI routing decision and its provider preflight.
        builder_provider = getattr(builder_instance, 'provider', 'unknown')
        builder_model = getattr(builder_instance, 'model', 'unknown')
        reviewer_providers = []
        reviewer_models = []
        for name in reviewer_names:
            reviewer_instance = self.reviewers[name]
            reviewer_providers.append(getattr(reviewer_instance, 'provider', 'unknown'))
            reviewer_models.append(getattr(reviewer_instance, 'model', 'unknown'))

        run = {"schema_version": 1, "run_id": run_id, "project_id": project_id, "workspace": str(workspace), "mission": mission, "status": "pending", "created_at": now(), "updated_at": now(), "current_step": "created", "profile": profile.to_dict(), "targeted_tests": targeted_tests, "full_tests": full_tests, "builder_name": builder_name, "builder_provider": builder_provider, "builder_model": builder_model, "builder_preflight": builder_preflight, "reviewer_names": reviewer_names, "reviewer_providers": reviewer_providers, "reviewer_models": reviewer_models, "reviewer_preflights": reviewer_preflights, "review_policy": review_policy, "review_semantics": review_semantics, "isolated_workspace": isolated_workspace, "codex_review": "codex" in reviewer_names, "corrections_used": 0, "max_corrections": 1, "tasks": tasks, "is_self_review": is_self_review, "no_review_label": review_policy == "none", "review_verified": False}
        atomic_write_text(folder / "mission.md", mission + "\n"); atomic_write_json(folder / "project-profile.json", profile.to_dict())
        atomic_write_json(folder / "task-graph.json", {"tasks": tasks}); plan_data = {"status": "pending", "bounded": True, "max_corrections": 1, "builder_name": builder_name, "builder_provider": builder_provider, "builder_model": builder_model, "builder_preflight": builder_preflight, "reviewer_names": reviewer_names, "reviewer_preflights": reviewer_preflights, "review_policy": review_policy, "review_semantics": review_semantics, "is_self_review": is_self_review}; atomic_write_json(folder / "plan.json", plan_data); self._write(run)
        self._event(run, "run_created", builder=builder_name, builder_provider=builder_provider, builder_model=builder_model, reviewers=reviewer_names, review_policy=review_policy, review_semantics=review_semantics, is_self_review=is_self_review); self._checkpoint(run)
        self._transition(run, RunStatus.PLANNING, "load profile and local memory")
        atomic_write_json(folder / "memory.json", self.memory.load(project_id)); plan_data["status"] = "ready"; plan_data["mission_sha256"] = hashlib.sha256(mission.encode()).hexdigest(); atomic_write_json(folder / "plan.json", plan_data)
        run["tasks"][0]["status"] = "completed"; self._transition(run, RunStatus.READY, "bounded plan created"); return run_id
    def get(self, run_id: str) -> dict[str, Any]:
        run = self._read(run_id)
        folder = self._dir(run_id)
        created = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
        run["elapsed_seconds"] = max(0, int((datetime.now(timezone.utc) - created).total_seconds()))
        run["progress"] = {
            "completed": sum(item.get("status") == "completed" for item in run.get("tasks", [])),
            "total": len(run.get("tasks", [])),
            "tasks": run.get("tasks", []),
        }
        run["evidence_directory"] = str(folder)
        for filename, field in (("changed-paths.json", "changed_files"),
                                ("test-results.json", "test_results"),
                                ("review-evidence.json", "review_findings"),
                                ("builder-evidence.json", "builder_invocation")):
            path = folder / filename
            if path.is_file():
                try:
                    run[field] = json.loads(path.read_text())
                except (OSError, json.JSONDecodeError):
                    run[field] = {"error": f"cannot read {filename}"}
        with self._control_lock:
            if request := self._control_requests.get(run_id):
                run["control_request"] = request
                run["current_step"] = f"{request} requested; applying at the next safe checkpoint"
        return run
    def events(self, run_id: str) -> list[dict[str, Any]]: return self._events(self._read(run_id)).read()
    def get_evidence_metadata(self, run_id: str) -> dict[str, Any]:
        """Safely read evidence metadata for a run."""
        run = self._read(run_id)
        folder = self._dir(run_id)
        metadata = {
            "run_id": run_id,
            "status": run.get("status"),
            "builder_name": run.get("builder_name"),
            "builder_provider": run.get("builder_provider"),
            "builder_model": run.get("builder_model"),
            "reviewer_names": run.get("reviewer_names", []),
            "review_policy": run.get("review_policy"),
            "is_self_review": run.get("is_self_review", False),
            "evidence_files": {},
        }
        # List evidence files
        for path in folder.rglob("*"):
            if path.is_file() and path.name != "manifest.json":
                rel_path = str(path.relative_to(folder))
                metadata["evidence_files"][rel_path] = {
                    "sha256": digest(path),
                    "bytes": path.stat().st_size,
                }
        return metadata
    def get_final_diff(self, run_id: str) -> dict[str, Any]:
        """Safely read the final diff for a run."""
        run = self._read(run_id)
        folder = self._dir(run_id)
        diff_path = folder / "final-diff.patch"
        if not diff_path.exists():
            return {"error": "final diff not found", "run_id": run_id}
        return {
            "run_id": run_id,
            "diff_sha256": run.get("final_diff_sha256"),
            "diff_content": diff_path.read_text(errors="replace"),
            "diff_size": diff_path.stat().st_size,
        }
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
            if run["status"] != "needs_approval":
                raise RuntimeStateError("only a run awaiting approval can be approved")
            human_only = run.get("review_policy") == "none"
            if not human_only and not run.get("review_verified"):
                self._event(run, "approval_refused", reason="independent review proof is absent or mismatched")
                raise RuntimeStateError("cannot approve without matching independent review proof")
            self._event(run, "human_approval", human_only=human_only)
            self._transition(run, RunStatus.ACCEPTED, "explicit human approval"); self._finalize(run); return run
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
    def _git_diff(self, workspace: Path) -> bytes:
        tracked = subprocess.run(
            ["git", "diff", "--binary", "--no-ext-diff", "--no-textconv"],
            cwd=str(workspace), shell=False, capture_output=True, check=True,
        ).stdout
        raw = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
            cwd=str(workspace), shell=False, capture_output=True, check=True,
        ).stdout
        parts = [tracked]
        for encoded in raw.split(b"\0"):
            if not encoded:
                continue
            path = encoded.decode(errors="surrogateescape")
            result = subprocess.run(
                ["git", "diff", "--binary", "--no-index", "--", "/dev/null", path],
                cwd=str(workspace), shell=False, capture_output=True,
            )
            if result.returncode not in {0, 1}:
                raise RuntimeStateError(f"cannot capture untracked diff for {path}")
            parts.append(result.stdout)
        return b"".join(parts)
    def _finalize(self, run: dict[str, Any]) -> None:
        folder = self._dir(run["run_id"]); atomic_write_json(folder / "final-status.json", {
            "status": run["status"], "last_checkpoint": run.get("last_checkpoint"),
            "builder_name": run.get("builder_name"), "builder_provider": run.get("builder_provider"),
            "builder_model": run.get("builder_model"), "reviewer_names": run.get("reviewer_names", []),
            "review_policy": run.get("review_policy"), "review_semantics": run.get("review_semantics"),
            "review_verified": run.get("review_verified", False),
            "final_diff_sha256": run.get("final_diff_sha256"),
        })
        files = sorted(path for path in folder.rglob("*") if path.is_file() and path.name != "manifest.json")
        atomic_write_json(folder / "manifest.json", {"schema_version": 1, "run_id": run["run_id"], "files": [{"path": str(path.relative_to(folder)), "sha256": digest(path), "bytes": path.stat().st_size} for path in files]})
    def _review_gate(self, run: dict[str, Any], stage: str) -> dict[str, Any]:
        folder = self._dir(run["run_id"])
        # Skip reviews if no-review policy
        if run.get("review_policy") == "none" and not run.get("reviewer_names"):
            review = {"ok": True, "decision": "pass", "stage": stage, "no_review": True,
                      "provider": None, "model": None, "reviews": [],
                      "proof": {"verdict": "NONE", "reviewed_diff_sha256": None,
                                "reason": "No-review policy: independent review skipped"}}
            atomic_write_json(folder / f"{stage}-review-evidence.json", review)
            return review
        reviews = []
        for name in run.get("reviewer_names", ["codex"]):
            reviewer = self.reviewers[name]
            try:
                if hasattr(reviewer, "review_stage"):
                    result = reviewer.review_stage(run, folder, stage)
                elif stage == "final":
                    result = reviewer.review(run, folder)
                else:
                    result = {"ok": False, "decision": "block", "stage": stage,
                              "reason": "reviewer does not implement this required stage"}
                if not isinstance(result, dict):
                    raise TypeError("reviewer result must be a dictionary")
            except Exception as exc:
                result = {"ok": False, "decision": "block", "stage": stage, "reason": f"reviewer exception: {type(exc).__name__}: {exc}"}
            result["reviewer"] = name
            expected_provider = getattr(reviewer, "provider", "unknown")
            reported_provider = result.get("provider", expected_provider)
            if reported_provider != expected_provider:
                result.update({"ok": False, "decision": "block",
                               "reason": f"review provider mismatch: expected {expected_provider}, got {reported_provider}"})
            result["provider"] = reported_provider
            result.setdefault("model", getattr(reviewer, "model", "unknown"))
            result.setdefault("real_or_mock", "unknown")
            if not self.allow_test_adapters and result.get("real_or_mock") != "real":
                result.update({"ok": False, "decision": "block",
                               "reason": "production review lacks real-provider evidence"})
            reviews.append(result)
            self._event(run, "review_completed", stage=stage, reviewer=name, provider=getattr(reviewer, "provider", "unknown"), decision=result.get("decision", "block"))
        decisions = {item.get("decision", "block") for item in reviews}
        if not decisions <= {"pass", "p1", "block"}:
            decisions.add("block")
        ok = all(item.get("ok") for item in reviews)
        decision = "block" if "block" in decisions or not ok else "p1" if "p1" in decisions else "pass"
        proofs = [item.get("proof", {}) for item in reviews]
        verified = stage == "final" and all(
            isinstance(proof, dict) and proof.get("verdict") == "ACCEPT"
            and proof.get("reviewed_diff_sha256") == run.get("final_diff_sha256")
            for proof in proofs
        )
        review = {"ok": ok, "decision": decision, "stage": stage,
                  "provider": "+".join(run.get("reviewer_providers", [])) or None,
                  "model": "+".join(run.get("reviewer_models", [])) or None,
                  "review_policy": run.get("review_policy"),
                  "review_semantics": run.get("review_semantics"),
                  "reviews": reviews, "proof": {"verdict": "ACCEPT" if verified else "MISSING", "reviewed_diff_sha256": run.get("final_diff_sha256") if verified else None}, "is_self_review": run.get("is_self_review", False), "no_review": run.get("review_policy") == "none"}
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
            builder.setdefault("selected_builder", run["builder_name"])
            builder.setdefault("selected_provider", run["builder_provider"])
            builder.setdefault("selected_model", run["builder_model"])
            builder.setdefault("review_policy", run["review_policy"])
            atomic_write_json(folder / "builder-evidence.json", builder)
        if self._apply_control(run):
            return run
        evidence_missing = []
        if not self.allow_test_adapters:
            required = {"provider", "model", "executable", "real_or_mock", "timestamp_start",
                        "timestamp_end", "returncode", "evidence_paths"}
            evidence_missing = sorted(required - set(builder))
            if builder.get("provider") != run["builder_provider"]:
                evidence_missing.append("provider_mismatch")
            if builder.get("real_or_mock") != "real":
                evidence_missing.append("real_provider_evidence")
        if not builder.get("ok") or violations or evidence_missing:
            if evidence_missing:
                self._event(run, "builder_evidence_invalid", missing=evidence_missing)
            self._transition(run, RunStatus.BLOCKED, "builder failed or outside scope"); self._finalize(run); return run
        patch = self._git_diff(workspace)
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
            # For no-review policy, never auto-accept and set review_verified to false
            if run.get("review_policy") == "none":
                run["review_verified"] = False
                run["no_review_label"] = True
            else:
                run["review_verified"] = bool(review.get("ok") and proof.get("verdict") == "ACCEPT" and proof.get("reviewed_diff_sha256") == run["final_diff_sha256"])
            self._write(run)
            if review.get("decision") == "p1" and run["corrections_used"] < run["max_corrections"]:
                self._transition(run, RunStatus.CORRECTING, "P1; one repair permitted"); return run
            if not review.get("ok") or review.get("decision") == "block":
                self._transition(run, RunStatus.BLOCKED, "review blocked"); self._finalize(run); return run
            run["tasks"][3]["status"] = "completed"; self._transition(run, RunStatus.NEEDS_APPROVAL, "review completed; human decision"); self._finalize(run); return run
