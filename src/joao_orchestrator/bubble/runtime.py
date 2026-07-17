"""Fail-closed local runtime.  Product data never belongs here."""
from __future__ import annotations

import hashlib
import inspect
import io
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import zipfile
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


def bounded_provider_env() -> dict[str, str]:
    """Prevent provider-invoked test tools from leaving interpreter/cache drift."""
    env = dict(os.environ)
    env.update({
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_ADDOPTS": "-p no:cacheprovider",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    })
    return env


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
        return None
    else:
        found = shutil.which(str(configured))
        if found:
            return found
    if fallback is not None:
        fallback = fallback.expanduser()
        if fallback.is_file() and os.access(fallback, os.X_OK):
            return str(fallback)
    return None


def resolve_claude_executable(configured: str | Path = "claude") -> str | None:
    """Resolve Claude for both Terminal and minimal-PATH macOS app launches."""
    configured_path = Path(configured).expanduser()
    candidates: list[str | Path] = [configured]
    if not configured_path.is_absolute():
        candidates.append(shutil.which("claude") or "")
    candidates.extend([
        Path("~/.local/bin/claude").expanduser(),
        Path("/opt/homebrew/bin/claude"),
        Path("/usr/local/bin/claude"),
    ])
    seen: set[str] = set()
    for value in candidates:
        if not value:
            continue
        text = str(value)
        if text in seen:
            continue
        seen.add(text)
        path = Path(text).expanduser()
        if path.is_absolute() and path.is_file() and os.access(path, os.X_OK):
            return str(path)
        if not path.is_absolute() and (found := shutil.which(text)):
            return found
    return None


def probe_claude_cli(executable: str | Path, model: str) -> dict[str, Any]:
    """Perform the bounded subscription-backed Claude CLI proof required by JOAO."""
    started = now()
    found = resolve_claude_executable(executable)
    base = {
        "provider": "anthropic-claude-code-subscription", "model": model,
        "requested_model": model, "actual_model": None, "real_or_mock": "real",
        "mode": "subscription_cli", "executable": found or str(executable),
        "timestamp_start": started,
    }
    if not found:
        return {**base, "available": False, "config_status": "not_configured",
                "auth_status": "unknown", "returncode": -1,
                "timestamp_end": now(), "last_error": "Executable not found",
                "reason": "Claude Code CLI is not installed or visible to the macOS Bubble"}
    try:
        version_result = subprocess.run([found, "--version"], shell=False,
                                        capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {**base, "available": False, "config_status": "error",
                "auth_status": "unknown", "returncode": -1,
                "timestamp_end": now(), "last_error": str(exc),
                "reason": "Claude Code version preflight failed"}
    version = (version_result.stdout or version_result.stderr).strip()[:200]
    if version_result.returncode != 0:
        return {**base, "available": False, "version": version,
                "config_status": "error", "auth_status": "unknown",
                "returncode": version_result.returncode, "timestamp_end": now(),
                "last_error": version or "version command failed",
                "reason": "Claude Code version preflight failed"}
    prompt = "Reply with exactly: CLAUDE_JOAO_OK"
    argv = [found, "-p", prompt, "--max-turns", "1", "--output-format", "json"]
    try:
        result = subprocess.run(argv, shell=False, capture_output=True, text=True, timeout=90)
        payload = json.loads(result.stdout) if result.stdout.strip() else {}
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        return {**base, "available": False, "version": version,
                "config_status": "configured", "auth_status": "error",
                "adapter_command": argv, "returncode": -1,
                "timestamp_end": now(), "last_error": str(exc),
                "reason": "Claude Code bounded authentication preflight could not be verified"}
    model_usage = payload.get("modelUsage") if isinstance(payload, dict) else None
    actual_model = payload.get("model") if isinstance(payload, dict) else None
    if not actual_model and isinstance(model_usage, dict) and model_usage:
        actual_model = next(iter(model_usage))
    response = payload.get("result") if isinstance(payload, dict) else None
    available = (
        result.returncode == 0 and payload.get("is_error") is not True
        and response == "CLAUDE_JOAO_OK"
    )
    error = None if available else str(response or result.stderr or "bounded preflight reply mismatch")[-500:]
    return {**base, "available": available, "version": version,
            "actual_model": actual_model, "config_status": "configured",
            "auth_status": "logged_in" if available else "not_logged_in",
            "adapter_command": argv, "returncode": result.returncode,
            "timestamp_end": now(), "last_error": error,
            "reason": ("Claude Code CLI is available and authenticated through its subscription"
                       if available else "Claude Code CLI is installed but its bounded subscription preflight failed")}


def parse_review_verdict(text: str, marker: str) -> str:
    matches = re.findall(rf"{re.escape(marker)}:\s*(ACCEPT|P1|BLOCK)", text, flags=re.I)
    return matches[-1].upper() if matches else ""


# SHA-256 of the empty byte string: a final-diff.patch of exactly 0 bytes.
# A run whose diff hashes to this produced literally nothing — fail-closed (A1).
EMPTY_DIFF_SHA256 = hashlib.sha256(b"").hexdigest()


def parse_test_cases(results: list[dict[str, Any]]) -> dict[str, int]:
    """Count real test CASES (not commands) from unittest/pytest output (A2/F9).

    A green run must report the cases that actually executed, not the number of
    commands. Zero cases collected is reported as such — never as a green count.
    """
    collected = passed = 0
    for item in results:
        blob = (item.get("stdout") or "") + "\n" + (item.get("stderr") or "")
        unittest_ran = re.search(r"Ran (\d+) tests? in", blob)
        if unittest_ran:
            count = int(unittest_ran.group(1))
            collected += count
            # unittest prints a trailing "OK" only when every case passed.
            if item.get("ok") and re.search(r"^OK", blob, re.M):
                passed += count
            else:
                failures = re.search(r"failures=(\d+)", blob)
                errors = re.search(r"errors=(\d+)", blob)
                bad = (int(failures.group(1)) if failures else 0) + (int(errors.group(1)) if errors else 0)
                passed += max(0, count - bad)
            continue
        p = re.search(r"(\d+) passed", blob)
        f = re.search(r"(\d+) failed", blob)
        e = re.search(r"(\d+) error", blob)
        n_pass = int(p.group(1)) if p else 0
        n_fail = int(f.group(1)) if f else 0
        n_err = int(e.group(1)) if e else 0
        collected += n_pass + n_fail + n_err
        passed += n_pass
    return {"cases_collected": collected, "cases_passed": passed}


TEXT_FILE_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]*\.(?:py|md|txt|json|html|htm|css|js|csv|ya?ml|toml|ini|rst)\b")
CODE_INTENT_RE = re.compile(r"\.py\b|fonction|function|classe|\bclass\b|script|parser|parseur|algorithm|algorithme|"
                            r"\bAPI\b|\bCLI\b|endpoint|regex|tests?\b|unittest|pytest", re.I)


def mission_allowed_paths(mission: str) -> list[str]:
    """Derive the sandbox write scope from the mission itself (A5b / V13-F14).

    Never inherit a stale static profile (todo.py…): the allowed paths are the
    files the mission actually names, plus the conventional src/ and tests/
    directories and their atomic-write staging siblings.
    """
    paths: set[str] = {"src/", "tests/", "todo.json.tmp", "test_tasks.json.tmp"}
    for match in TEXT_FILE_RE.findall(mission):
        relative = match.lstrip("./")
        if ".." in relative or relative.startswith("/") or "\\" in relative:
            continue
        paths.add(relative)
    return sorted(paths)


def extract_agent_finding(jsonl: str, marker: str) -> str:
    messages: list[str] = []
    for line in jsonl.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = event.get("item", {})
        if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
            messages.append(item["text"])
    for message in reversed(messages):
        if marker in message:
            return message[-4000:]
    return messages[-1][-4000:] if messages else ""


def extract_opencode_text(jsonl: str) -> str:
    """Concatenate the assistant text events of an OpenCode --format json stream."""
    texts: list[str] = []
    for line in jsonl.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        part = event.get("part", {})
        if event.get("type") == "text" and isinstance(part.get("text"), str):
            texts.append(part["text"])
    return "\n".join(texts)


def git_status_paths(workspace: Path) -> list[str]:
    result = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=str(workspace), shell=False, capture_output=True, text=True, check=True,
    )
    return sorted({item[3:].replace("\\", "/") for item in result.stdout.split("\0") if item})


def git_worktree_fingerprint(workspace: Path) -> str:
    value = hashlib.sha256()
    for relative in git_status_paths(workspace):
        value.update(relative.encode(errors="surrogateescape"))
        path = workspace / relative
        if path.is_file():
            value.update(digest(path).encode())
        elif path.is_symlink():
            value.update(os.readlink(path).encode(errors="surrogateescape"))
        else:
            value.update(b"<missing-or-directory>")
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
    RunStatus.READY: {RunStatus.BUILDING, RunStatus.NEEDS_APPROVAL, RunStatus.BLOCKED, RunStatus.PAUSED, RunStatus.STOPPED},
    RunStatus.BUILDING: {RunStatus.TESTING, RunStatus.NEEDS_APPROVAL, RunStatus.CORRECTING, RunStatus.BLOCKED, RunStatus.FAILED, RunStatus.PAUSED, RunStatus.STOPPED},
    RunStatus.TESTING: {RunStatus.REVIEWING, RunStatus.CORRECTING, RunStatus.FAILED, RunStatus.BLOCKED, RunStatus.PAUSED, RunStatus.STOPPED},
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


# Deterministic reviewer ordering for review policies and their combinations.
CANONICAL_REVIEWERS = ["codex", "claude", "glm"]


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
            proc = subprocess.run(argv, cwd=str(cwd), shell=False, capture_output=True, text=True, timeout=timeout, env=bounded_provider_env())
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
    def __init__(self, executable: Path = Path("~/.local/bin/joao-glm").expanduser(), timeout: int = 1200): self.executable = executable; self.timeout = timeout
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
        bounded_mission = (
            "Act as JOAO's bounded builder. Work only in this Git workspace and only "
            f"in these paths: {', '.join(allowed)}. Do not create scratch, runner, or "
            "verification files outside those paths; interpreter caches such as __pycache__ "
            "are forbidden deliverables. Do not install packages, access the network, commit, "
            "push, or change policy/configuration. Implement the following task and run the "
            "explicitly requested tests.\n\n" + mission
        )
        task = run_dir / ("correction.md" if correction else "builder-task.md"); atomic_write_text(task, bounded_mission)
        output = run_dir / ("glm-correction.jsonl" if correction else "glm-builder.jsonl")
        argv = [found, "--workspace", str(workspace), "--task-file", str(task), "--output", str(output), "--mode", "workspace-write", "--budget", "normal"]
        for item in allowed: argv.extend(["--allowed-path", item])
        if correction:
            for item in git_status_paths(workspace):
                argv.extend(["--baseline-path", item])
        try:
            proc = subprocess.run(argv, shell=False, capture_output=True, text=True,
                                  timeout=self.timeout, env=bounded_provider_env())
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "provider": self.provider, "model": self.model, "executable": found, "real_or_mock": "real", "reason": f"GLM execution failed: {exc}", "timestamp_start": start_time, "timestamp_end": now(), "returncode": -1, "last_error": str(exc)}
        return {"ok": proc.returncode == 0, "provider": self.provider, "model": self.model, "executable": found, "adapter_command": argv, "real_or_mock": "real", "returncode": proc.returncode, "stdout": proc.stdout[-4000:], "stderr": proc.stderr[-4000:], "output": str(output), "output_sha256": digest(output) if output.exists() else None, "timestamp_start": start_time, "timestamp_end": now(), "evidence_paths": [str(output), str(task)]}


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
        argv = [found, "exec", "--json", "--ephemeral", "--ignore-user-config", "--model", self.model,
                "--sandbox", "workspace-write", "-C", str(workspace),
                "--output-last-message", str(output), prompt]
        try:
            proc = subprocess.run(argv, shell=False, capture_output=True, text=True,
                                  timeout=self.timeout, env=bounded_provider_env())
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "provider": self.provider, "model": self.model, "executable": found, "real_or_mock": "real", "reason": f"Codex builder unavailable: {exc}", "timestamp_start": start_time, "timestamp_end": now(), "returncode": -1, "last_error": str(exc)}
        events = run_dir / ("codex-correction.jsonl" if correction else "codex-builder.jsonl")
        atomic_write_text(events, proc.stdout)
        return {"ok": proc.returncode == 0, "provider": self.provider, "model": self.model, "executable": found, "adapter_command": argv[:-1], "real_or_mock": "real", "returncode": proc.returncode, "stdout": proc.stdout[-4000:], "stderr": proc.stderr[-4000:], "output": str(events), "output_sha256": digest(events), "last_message": str(output), "last_message_sha256": digest(output) if output.exists() else None, "timestamp_start": start_time, "timestamp_end": now(), "evidence_paths": [str(events), str(output), str(task)]}


class ClaudeBuilder(BuilderAdapter):
    """Use one bounded Claude Code subscription process as the selected builder."""

    provider = "anthropic-claude-code-subscription"
    model = "claude-cli-configured-default"

    def __init__(self, executable: str = "claude", timeout: int = 900,
                 model: str = "claude-cli-configured-default"):
        self.executable = executable
        self.timeout = timeout
        self.model = model
        self._preflight_cache: dict[str, Any] | None = None

    def preflight(self) -> dict[str, Any]:
        if self._preflight_cache is None:
            self._preflight_cache = probe_claude_cli(self.executable, self.model)
        return dict(self._preflight_cache)

    def build(self, mission, workspace, run_dir, allowed, correction):
        started = now()
        capability = self.preflight()
        found = capability.get("executable")
        task = run_dir / ("claude-correction-task.md" if correction else "claude-builder-task.md")
        output = run_dir / ("claude-correction.json" if correction else "claude-builder.json")
        atomic_write_text(task, mission)
        if not capability.get("available") or not found:
            atomic_write_json(output, {"preflight": capability})
            return {
                "ok": False, "provider": self.provider, "model": self.model,
                "actual_model": capability.get("actual_model"), "executable": found,
                "real_or_mock": "real", "reason": capability.get("reason"),
                "last_error": capability.get("last_error"), "returncode": -1,
                "timestamp_start": started, "timestamp_end": now(),
                "evidence_paths": [str(output), str(task)],
            }
        prompt = (
            "Act as JOAO's bounded builder. Work only in the current disposable Git "
            "workspace. You may use only Read, Edit, Write, Glob and Grep; shell, network, "
            "package installation, commit, push and deployment are forbidden. Modify only "
            f"these allowed paths: {', '.join(allowed)}. Implement the mission fully. "
            "JOAO will execute the tests after you finish.\n\n" + mission
        )
        argv = [
            found, "-p", prompt, "--max-turns", "12", "--output-format", "json",
            "--permission-mode", "acceptEdits", "--tools", "Read,Edit,Write,Glob,Grep",
        ]
        try:
            result = subprocess.run(argv, cwd=str(workspace), shell=False,
                                    capture_output=True, text=True, timeout=self.timeout,
                                    env=bounded_provider_env())
            payload = json.loads(result.stdout) if result.stdout.strip() else {}
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            atomic_write_json(output, {"error": str(exc)})
            return {
                "ok": False, "provider": self.provider, "model": self.model,
                "actual_model": capability.get("actual_model"), "executable": found,
                "adapter_command": argv[:-9] + argv[-8:], "real_or_mock": "real",
                "reason": f"Claude builder failed: {exc}", "last_error": str(exc),
                "returncode": -1, "timestamp_start": started, "timestamp_end": now(),
                "evidence_paths": [str(output), str(task)],
            }
        atomic_write_text(output, result.stdout)
        model_usage = payload.get("modelUsage") if isinstance(payload, dict) else None
        actual_model = payload.get("model") if isinstance(payload, dict) else None
        if not actual_model and isinstance(model_usage, dict) and model_usage:
            actual_model = next(iter(model_usage))
        provider_error = payload.get("is_error") is True
        return {
            "ok": result.returncode == 0 and not provider_error,
            "provider": self.provider, "model": self.model,
            "actual_model": actual_model or capability.get("actual_model"),
            "executable": found, "adapter_command": argv[:2] + argv[3:],
            "real_or_mock": "real", "returncode": result.returncode,
            "stderr": result.stderr[-4000:],
            "last_error": str(payload.get("result"))[-500:] if provider_error else None,
            "output": str(output), "output_sha256": digest(output),
            "timestamp_start": started, "timestamp_end": now(),
            "evidence_paths": [str(output), str(task)],
        }


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
        repair_suffix = f"-repair-{run.get('corrections_used')}" if run.get("corrections_used") else ""
        output = run_dir / f"codex-{stage}-review{repair_suffix}.jsonl"
        found = resolve_executable(self.executable, fallback=Path("~/.local/bin/codex"))
        if not found:
            return {"ok": False, "decision": "block", "stage": stage, "provider": self.provider, "model": self.model, "real_or_mock": "real", "reason": f"Codex executable not found at {self.executable}", "timestamp_start": start_time, "timestamp_end": now(), "returncode": -1, "last_error": "Executable not found"}
        prompt = (
            "You are the independent JOAO reviewer. Work read-only. Review the "
            f"{stage} gate for this bounded mission:\\n\\n{run['mission']}\\n\\n"
            f"The immutable local evidence directory is {run_dir}. "
            "Inspect only the current worktree, that evidence directory, and git diff. Do not "
            "edit, commit, push, install packages or call external services. At the "
            "plan gate, assess only whether the bounded plan and safety contract are sound; do not "
            "require implementation or run tests. For the plan gate, read only mission.md, "
            "plan.json, and project-profile.json in the evidence directory; do not scan checkpoints "
            "or propose preferences already answered by objective_verbatim. A P1 requires a concrete "
            "correctness or safety defect, not a speculative enhancement. At the build gate, inspect the produced diff. "
            "At the test gate, inspect or rerun the recorded tests. At the final gate, recheck the "
            "complete diff, tests, scope, and evidence. "
            "For P1 or BLOCK, print JOAO_FINDING: followed by one concrete repair line. "
            "Then end with exactly one final line: JOAO_REVIEW: ACCEPT, JOAO_REVIEW: P1, "
            "or JOAO_REVIEW: BLOCK."
        )
        argv = [found, "exec", "--json", "--ephemeral", "--ignore-user-config", "--model", self.model,
                "--sandbox", "workspace-write", "-C", str(workspace), prompt]
        before_paths = git_status_paths(workspace)
        before_fingerprint = git_worktree_fingerprint(workspace)
        env = bounded_provider_env()
        try:
            proc = subprocess.run(argv, cwd=str(workspace), shell=False, capture_output=True,
                                  text=True, timeout=self.timeout, env=env)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "decision": "block", "stage": stage,
                    "provider": self.provider, "model": self.model, "real_or_mock": "real",
                    "reason": f"Codex reviewer unavailable: {exc}",
                    "timestamp_start": start_time, "timestamp_end": now(), "returncode": -1, "last_error": str(exc)}
        atomic_write_text(output, proc.stdout)
        text = proc.stdout + "\n" + proc.stderr
        verdict = parse_review_verdict(text, "JOAO_REVIEW")
        finding = extract_agent_finding(proc.stdout, "JOAO_REVIEW")
        decision = {"ACCEPT": "pass", "P1": "p1", "BLOCK": "block"}.get(verdict, "block")
        concrete = re.sub(r"JOAO_REVIEW:\s*(ACCEPT|P1|BLOCK)", "", finding, flags=re.I).strip(" \n—:-")
        malformed_finding = verdict in {"P1", "BLOCK"} and len(concrete) < 12
        if malformed_finding:
            decision = "block"
        after_paths = git_status_paths(workspace)
        reviewer_drift = sorted(set(after_paths) ^ set(before_paths))
        if git_worktree_fingerprint(workspace) != before_fingerprint and not reviewer_drift:
            reviewer_drift = ["<content changed in existing worktree path>"]
        if reviewer_drift:
            verdict, decision = "BLOCK", "block"
        return {
            "ok": proc.returncode == 0 and verdict == "ACCEPT" and not reviewer_drift,
            "decision": decision, "stage": stage, "verdict": verdict or "MISSING",
            "finding": finding,
            "malformed_finding": malformed_finding,
            "returncode": proc.returncode, "output": str(output),
            "output_sha256": digest(output), "stderr": proc.stderr[-4000:],
            "stdout_tail": proc.stdout[-2000:],
            "proof": {"verdict": verdict, "reviewed_diff_sha256": run.get("final_diff_sha256")},
            "provider": self.provider, "model": self.model, "executable": found, "real_or_mock": "real",
            "adapter_command": argv[:-1],
            "reviewer_workspace_drift": reviewer_drift,
            "timestamp_start": start_time, "timestamp_end": now(), "evidence_paths": [str(output)],
        }

    def review(self, run, run_dir):
        return self.review_stage(run, run_dir, "final")


class ClaudeCLIReviewer(ReviewerAdapter):
    """Optional real Claude CLI reviewer; absence is an explicit preflight refusal."""

    provider = "anthropic-claude-code-subscription"; model = "claude-cli-configured-default"

    def __init__(self, executable: str = "claude", timeout: int = 900):
        self.executable = executable
        self.timeout = timeout
        self._preflight_cache: dict[str, Any] | None = None

    def preflight(self) -> dict[str, Any]:
        if self._preflight_cache is None:
            self._preflight_cache = probe_claude_cli(self.executable, self.model)
            self._preflight_cache["mode"] = "read_only_review"
        return dict(self._preflight_cache)

    def review_stage(self, run, run_dir, stage: str) -> dict[str, Any]:
        started = now()
        capability = self.preflight()
        found = capability.get("executable")
        output = run_dir / f"claude-{stage}-review.json"
        if not capability.get("available") or not found:
            return {"ok": False, "decision": "block", "stage": stage,
                    "provider": self.provider, "model": self.model,
                    "real_or_mock": "real", "returncode": -1,
                    "timestamp_start": started, "timestamp_end": now(),
                    "reason": capability.get("reason", "Claude CLI is unavailable"),
                    "last_error": capability.get("last_error")}
        prompt = (
            "You are a read-only JOAO reviewer. Review the " + stage + " gate for the mission below. "
            f"Inspect the current Git diff and local evidence in {run_dir}. Do not edit files, install packages, "
            "commit, push, or use the network. Judge only this gate: at the plan gate, assess only whether "
            "the bounded plan and safety contract are sound. At the build gate, inspect only the produced "
            "diff; JOAO itself executes the recorded test commands at the dedicated test gate, so never "
            "demand test execution or test output at the build gate. At the test gate, inspect the recorded "
            "test results. At the final gate, recheck the complete diff, tests, scope, and evidence. "
            "A P1 requires a concrete correctness or safety defect, not a speculative enhancement. "
            "For P1 or BLOCK, print CLAUDE_FINDING: followed by one "
            "concrete repair line. End with exactly CLAUDE_REVIEW: ACCEPT, "
            "CLAUDE_REVIEW: P1, or CLAUDE_REVIEW: BLOCK.\n\n" + run["mission"]
        )
        argv = [found, "-p", prompt, "--max-turns", "24", "--output-format", "json",
                "--permission-mode", "plan", "--tools", "Read,Grep,Glob"]
        workspace = Path(run["workspace"])
        before_paths = git_status_paths(workspace)
        before_fingerprint = git_worktree_fingerprint(workspace)
        try:
            result = subprocess.run(argv, cwd=workspace, shell=False,
                                    capture_output=True, text=True, timeout=self.timeout,
                                    env=bounded_provider_env())
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "decision": "block", "stage": stage,
                    "provider": self.provider, "model": self.model,
                    "real_or_mock": "real", "returncode": -1,
                    "timestamp_start": started, "timestamp_end": now(),
                    "reason": f"Claude reviewer failed: {exc}", "last_error": str(exc)}
        atomic_write_text(output, result.stdout)
        text = result.stdout + "\n" + result.stderr
        try:
            response = json.loads(result.stdout)
            if isinstance(response, dict) and isinstance(response.get("result"), str):
                text = response["result"] + "\n" + result.stderr
        except json.JSONDecodeError:
            pass
        verdict = parse_review_verdict(text, "CLAUDE_REVIEW") or "MISSING"
        decision = {"ACCEPT": "pass", "P1": "p1", "BLOCK": "block"}.get(verdict, "block")
        finding_match = re.findall(r"CLAUDE_FINDING:\s*(.+)", text, flags=re.I)
        finding = finding_match[-1].strip()[-4000:] if finding_match else ""
        malformed_finding = verdict in {"P1", "BLOCK"} and len(finding) < 12
        if malformed_finding:
            decision = "block"
        after_paths = git_status_paths(workspace)
        reviewer_drift = sorted(set(after_paths) ^ set(before_paths))
        if git_worktree_fingerprint(workspace) != before_fingerprint and not reviewer_drift:
            reviewer_drift = ["<content changed in existing worktree path>"]
        if reviewer_drift:
            verdict, decision = "BLOCK", "block"
        return {
            "ok": result.returncode == 0 and verdict == "ACCEPT" and not reviewer_drift,
            "decision": decision,
            "stage": stage, "verdict": verdict, "finding": finding,
            "malformed_finding": malformed_finding, "returncode": result.returncode,
            "provider": self.provider, "model": self.model,
            "actual_model": capability.get("actual_model"), "executable": found,
            "real_or_mock": "real", "timestamp_start": started, "timestamp_end": now(),
            "output": str(output), "output_sha256": digest(output),
            "proof": {"verdict": verdict, "reviewed_diff_sha256": run.get("final_diff_sha256")},
            "adapter_command": argv[:2] + argv[3:],
            "reviewer_workspace_drift": reviewer_drift,
            "stderr": result.stderr[-4000:], "evidence_paths": [str(output)],
        }

    def review(self, run, run_dir):
        return self.review_stage(run, run_dir, "final")


class GLMCLIReviewer(ReviewerAdapter):
    """Read-only GLM reviewer through the fail-closed joao-glm wrapper (free plan)."""

    provider = "zai-coding-plan"; model = "zai-coding-plan/glm-4.5-air"

    def __init__(self, executable: Path = Path("~/.local/bin/joao-glm").expanduser(), timeout: int = 1200):
        self.executable = executable
        self.timeout = timeout

    def preflight(self) -> dict[str, Any]:
        found = resolve_executable(self.executable, fallback=Path("~/.local/bin/joao-glm"))
        if not found:
            return {"available": False, "executable": str(self.executable), "model": self.model,
                    "provider": self.provider, "real_or_mock": "real", "mode": "read_only_review",
                    "reason": f"GLM executable not found at {self.executable}",
                    "last_error": "Executable not found or not executable",
                    "auth_status": "unknown", "config_status": "not_configured"}
        return {"available": True, "executable": found, "model": self.model,
                "provider": self.provider, "real_or_mock": "real", "mode": "read_only_review",
                "reason": "GLM reviewer is available", "last_error": None,
                "auth_status": "adapter_managed", "config_status": "configured"}

    def review_stage(self, run, run_dir, stage: str) -> dict[str, Any]:
        started = now()
        workspace = Path(run["workspace"])
        found = resolve_executable(self.executable, fallback=Path("~/.local/bin/joao-glm"))
        if not found:
            return {"ok": False, "decision": "block", "stage": stage,
                    "provider": self.provider, "model": self.model, "real_or_mock": "real",
                    "returncode": -1, "timestamp_start": started, "timestamp_end": now(),
                    "reason": f"GLM executable not found at {self.executable}",
                    "last_error": "Executable not found"}
        repair_suffix = f"-repair-{run.get('corrections_used')}" if run.get("corrections_used") else ""
        task = run_dir / f"glm-{stage}-review-task{repair_suffix}.md"
        output = run_dir / f"glm-{stage}-review{repair_suffix}.jsonl"
        # OpenCode's read-only agent cannot leave the workspace, so every gate's
        # evidence must travel inline inside the task itself.
        stage_evidence = {
            "plan": ["plan.json", "project-profile.json"],
            "build": ["final-diff.patch"],
            "test": ["test-results.json"],
            "final": ["final-diff.patch", "test-results.json"],
        }
        def neutralize(text: str) -> str:
            # Quoted mission or evidence must never satisfy the verdict or
            # finding regexes, which match case-insensitively.
            return re.sub(r"(?i)GLM_(REVIEW|FINDING)", r"GLM-\1-QUOTED", text)

        def truncate_utf8(text: str, limit: int) -> str:
            raw = text.encode("utf-8", errors="replace")
            return text if len(raw) <= limit else raw[:limit].decode("utf-8", errors="replace")

        head = (
            "You are a read-only JOAO reviewer. Review the " + stage + " gate for the mission below. "
            "All the gate evidence you need is included verbatim at the end of this task; never claim "
            "evidence is inaccessible, and do not try to read outside the workspace. Do not edit files, "
            "install packages, commit, push, or use the network. Judge only this gate: at the plan gate, "
            "assess only whether the bounded plan and safety contract are sound; the mission may "
            "legitimately create files that do not exist yet, so a missing deliverable file is never a "
            "plan defect. At the build gate, inspect only the produced diff; JOAO itself executes the "
            "recorded test commands at the dedicated test gate, so never demand test execution or test "
            "output at the build gate. At the test gate, inspect the recorded test results. At the final "
            "gate, recheck the complete diff, tests, and scope. A P1 or BLOCK requires a concrete "
            "correctness or safety defect, not a speculative enhancement or a preference. "
            "For P1 or BLOCK, print GLM_FINDING: followed by one concrete repair line. "
            "You need no tool at the plan gate — answer directly from the mission and the inline "
            "evidence; keep tool use minimal at every gate, and ALWAYS finish by printing the final "
            "verdict line. "
            "End with exactly GLM_REVIEW: ACCEPT, GLM_REVIEW: P1, or GLM_REVIEW: BLOCK.\n\nMISSION:\n"
            + neutralize(run["mission"]) + "\n\nGATE EVIDENCE:"
        )
        # The wrapper caps a normal-budget task at 120000 bytes; budget the
        # inline evidence in encoded bytes with headroom, never in characters.
        names = [name for name in stage_evidence.get(stage, []) if (run_dir / name).is_file()]
        available = max(2000, 110_000 - len(head.encode("utf-8")))
        sections = []
        for name in names:
            body = truncate_utf8(neutralize((run_dir / name).read_text(errors="replace")),
                                 available // len(names))
            sections.append(f"\n--- {name} (verbatim, may be truncated) ---\n{body}")
        evidence_inline = "".join(sections) or "\n--- no recorded evidence file for this gate ---"
        prompt = head + evidence_inline
        atomic_write_text(task, prompt)
        before_paths = git_status_paths(workspace)
        before_fingerprint = git_worktree_fingerprint(workspace)
        # A weak model sometimes wanders and never prints its verdict; one
        # bounded retry is permitted for that exact case, both attempts kept.
        attempts = 0
        first_attempt_verdict = None
        while True:
            attempts += 1
            attempt_output = output if attempts == 1 else output.with_name(output.stem + "-retry.jsonl")
            argv = [found, "--workspace", str(workspace), "--task-file", str(task),
                    "--output", str(attempt_output), "--mode", "read-only", "--budget", "normal"]
            try:
                proc = subprocess.run(argv, shell=False, capture_output=True, text=True,
                                      timeout=self.timeout, env=bounded_provider_env())
            except (OSError, subprocess.TimeoutExpired) as exc:
                return {"ok": False, "decision": "block", "stage": stage,
                        "provider": self.provider, "model": self.model, "real_or_mock": "real",
                        "returncode": -1, "timestamp_start": started, "timestamp_end": now(),
                        "reason": f"GLM reviewer failed: {exc}", "last_error": str(exc)}
            review_text = ""
            if attempt_output.is_file():
                review_text = extract_opencode_text(attempt_output.read_text(errors="replace"))
            text = review_text or (proc.stdout + "\n" + proc.stderr)
            verdict = parse_review_verdict(text, "GLM_REVIEW") or "MISSING"
            if verdict != "MISSING" or attempts >= 2:
                output = attempt_output
                break
            first_attempt_verdict = "MISSING"
        decision = {"ACCEPT": "pass", "P1": "p1", "BLOCK": "block"}.get(verdict, "block")
        finding_match = re.findall(r"GLM_FINDING:\s*(.+)", text, flags=re.I)
        finding = finding_match[-1].strip()[-4000:] if finding_match else ""
        malformed_finding = verdict in {"P1", "BLOCK"} and len(finding) < 12
        if malformed_finding:
            decision = "block"
        after_paths = git_status_paths(workspace)
        reviewer_drift = sorted(set(after_paths) ^ set(before_paths))
        if git_worktree_fingerprint(workspace) != before_fingerprint and not reviewer_drift:
            reviewer_drift = ["<content changed in existing worktree path>"]
        if reviewer_drift:
            verdict, decision = "BLOCK", "block"
        return {
            "ok": proc.returncode == 0 and verdict == "ACCEPT" and not reviewer_drift,
            "decision": decision, "stage": stage, "verdict": verdict, "finding": finding,
            "malformed_finding": malformed_finding, "returncode": proc.returncode,
            "provider": self.provider, "model": self.model, "executable": found,
            "real_or_mock": "real", "timestamp_start": started, "timestamp_end": now(),
            "output": str(output), "output_sha256": digest(output) if output.is_file() else None,
            "stdout_tail": proc.stdout[-2000:], "stderr": proc.stderr[-4000:],
            "attempts": attempts, "first_attempt_verdict": first_attempt_verdict,
            "proof": {"verdict": verdict, "reviewed_diff_sha256": run.get("final_diff_sha256")},
            "adapter_command": argv, "reviewer_workspace_drift": reviewer_drift,
            "evidence_paths": [str(output), str(task)],
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
            if review_policy:
                reviewer_names = review_policy.split("_and_")
            else:
                reviewer_names = ["codex"] if codex_review else []
        reviewer_names = list(reviewer_names or [])
        if review_policy is None:
            review_policy = "_and_".join(reviewer_names) or "none"
        # Any duplicate-free combination of known reviewers is a valid policy;
        # the policy string is the canonical-order join of the selected names.
        canonical = [name for name in CANONICAL_REVIEWERS if name in reviewer_names]
        if (len(set(reviewer_names)) != len(reviewer_names)
                or any(name not in CANONICAL_REVIEWERS for name in reviewer_names)
                or reviewer_names != canonical
                or review_policy != ("_and_".join(reviewer_names) or "none")):
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

        is_self_review = builder_name in reviewer_names
        review_semantics = "stacked-with-self-review" if is_self_review and len(reviewer_names) > 1 else (
            "self-review" if is_self_review else (
            "stacked-independent" if len(reviewer_names) > 1 else
            "independent" if reviewer_names else "none"
        ))

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

        run = {"schema_version": 1, "run_id": run_id, "project_id": project_id, "workspace": str(workspace), "mission": mission, "status": "pending", "created_at": now(), "updated_at": now(), "current_step": "created", "profile": profile.to_dict(), "targeted_tests": targeted_tests, "full_tests": full_tests, "builder_name": builder_name, "builder_provider": builder_provider, "builder_model": builder_model, "builder_preflight": builder_preflight, "reviewer_names": reviewer_names, "reviewer_providers": reviewer_providers, "reviewer_models": reviewer_models, "reviewer_preflights": reviewer_preflights, "review_policy": review_policy, "review_semantics": review_semantics, "isolated_workspace": isolated_workspace, "codex_review": "codex" in reviewer_names, "corrections_used": 0, "max_corrections": 1, "tasks": tasks, "is_self_review": is_self_review, "no_review_label": review_policy == "none", "review_verified": False, "plan_review_completed": False}
        atomic_write_text(folder / "mission.md", mission + "\n"); atomic_write_json(folder / "project-profile.json", profile.to_dict())
        atomic_write_json(folder / "task-graph.json", {"tasks": tasks})
        plan_data = {
            "status": "pending", "bounded": True, "max_corrections": 1,
            "objective_verbatim": mission,
            "implementation_scope": {
                "workspace": str(workspace),
                "allowed_write_paths": profile.allowed_write_paths,
                "forbidden_paths": profile.forbidden_paths,
                "external_dependencies": "forbidden unless the mission and human policy explicitly allow them",
                "network": "forbidden for quick sandbox builds",
                "delivery_excluded_generated_paths": profile.generated_paths,
            },
            "validation_contract": {
                "targeted_test_commands": targeted_tests,
                "full_test_commands": full_tests,
                "all_commands_must_pass": True,
                "final_state": "needs_approval",
                "requirements_source": "objective_verbatim and mission.md",
            },
            "execution_steps": [
                {"gate": "plan", "action": "validate objective, scope, provider routing, and tests"},
                {"gate": "build", "action": "implement every objective requirement only inside allowed paths"},
                {"gate": "test", "action": "run every recorded targeted and full-test command"},
                {"gate": "cleanup", "action": "remove every delivery_excluded_generated_path before diff review"},
                {"gate": "review", "action": "bind reviewer verdicts to the final diff hash"},
                {"gate": "delivery", "action": "stop for explicit human approval"},
            ],
            "builder_name": builder_name, "builder_provider": builder_provider,
            "builder_model": builder_model, "builder_preflight": builder_preflight,
            "reviewer_names": reviewer_names, "reviewer_preflights": reviewer_preflights,
            "review_policy": review_policy, "review_semantics": review_semantics,
            "is_self_review": is_self_review,
        }
        atomic_write_json(folder / "plan.json", plan_data); self._write(run)
        self._event(run, "run_created", builder=builder_name, builder_provider=builder_provider, builder_model=builder_model, reviewers=reviewer_names, review_policy=review_policy, review_semantics=review_semantics, is_self_review=is_self_review); self._checkpoint(run)
        self._transition(run, RunStatus.PLANNING, "load profile and local memory")
        atomic_write_json(folder / "memory.json", self.memory.load(project_id)); plan_data["status"] = "ready"; plan_data["mission_sha256"] = hashlib.sha256(mission.encode()).hexdigest(); atomic_write_json(folder / "plan.json", plan_data)
        run["tasks"][0]["status"] = "completed"; self._transition(run, RunStatus.READY, "bounded plan created"); return run_id
    def get(self, run_id: str) -> dict[str, Any]:
        run = self._read(run_id)
        folder = self._dir(run_id)
        created = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
        if run["status"] in {"accepted", "stopped", "blocked", "failed"}:
            # Terminal card: the clock must freeze at the last transition.
            end = datetime.fromisoformat(run["updated_at"].replace("Z", "+00:00"))
        else:
            end = datetime.now(timezone.utc)
        run["elapsed_seconds"] = max(0, int((end - created).total_seconds()))
        run["progress"] = {
            "completed": sum(item.get("status") == "completed" for item in run.get("tasks", [])),
            "total": len(run.get("tasks", [])),
            "tasks": run.get("tasks", []),
        }
        run["evidence_directory"] = str(folder)
        run["result_available"] = (folder / "final-diff.patch").is_file()
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
        if run["status"] in {"blocked", "failed"}:
            run.update(self.explain_block(run_id))
        try:
            updated = datetime.fromisoformat(run["updated_at"].replace("Z", "+00:00"))
            active = run["status"] not in {"accepted", "stopped", "blocked", "failed"}
            run["step_elapsed_seconds"] = (
                max(0, int((datetime.now(timezone.utc) - updated).total_seconds())) if active else 0)
        except (KeyError, ValueError):
            run["step_elapsed_seconds"] = 0
        run["mission_display"] = self.user_mission(run.get("mission"))
        run["narration"] = self.narrate(run)
        run["phase_label"] = self.phase_label(run)
        attach_path = folder / "attachments.json"
        if attach_path.is_file():
            try:
                run["attachments"] = json.loads(attach_path.read_text()).get("attachments", [])
            except (OSError, json.JSONDecodeError):
                run["attachments"] = []
        if run["status"] not in {"accepted", "stopped", "blocked", "failed"}:
            run["eta"] = self.estimate_eta(run.get("builder_name"), run.get("review_policy"),
                                           len(run["mission_display"]))
        with self._control_lock:
            if request := self._control_requests.get(run_id):
                run["control_request"] = request
                run["current_step"] = f"{request} requested; applying at the next safe checkpoint"
        return run
    def events(self, run_id: str) -> list[dict[str, Any]]: return self._events(self._read(run_id)).read()
    PROVIDER_LABELS = {"glm": "GLM", "codex": "Codex", "claude": "Claude"}
    @staticmethod
    def user_mission(mission: str) -> str:
        """The human's own words, without the quick-sandbox contract preamble."""
        return (mission or "").split("User task:\n", 1)[-1].strip()
    def _gate_position(self, run: dict[str, Any]) -> str:
        completed = sum(item.get("status") == "completed" for item in run.get("tasks", []))
        return f"gate {min(completed + 1, len(run.get('tasks', [])) or 4)}/{len(run.get('tasks', [])) or 4}"
    def narrate(self, run: dict[str, Any]) -> str:
        """F3/F6 — one human-language sentence describing what is happening now."""
        builder = self.PROVIDER_LABELS.get(run.get("builder_name"), run.get("builder_name") or "?")
        reviewers = [self.PROVIDER_LABELS.get(name, name) for name in run.get("reviewer_names") or []]
        reviewer = " + ".join(reviewers) if reviewers else None
        gate = self._gate_position(run)
        diff_note = ""
        try:
            manifest = json.loads((self._dir(run["run_id"]) / "deliverables-manifest.json").read_text())
            count = len(manifest.get("files", []))
            if count:
                diff_note = f" ({count} fichier{'s' if count > 1 else ''})"
        except (OSError, json.JSONDecodeError, KeyError):
            pass
        status = run.get("status")
        step = str(run.get("current_step") or "")
        if status in {"pending", "planning"}:
            return "JOÃO prépare le plan borné de la mission…"
        if status == "ready":
            return (f"{reviewer} review le plan — gate 1/4" if reviewer
                    else "Plan prêt — démarrage du build…")
        if status == "building":
            if run.get("corrections_used"):
                return f"{builder} corrige le code après la review — re-build en cours…"
            return f"{builder} écrit le code…"
        if status == "testing":
            return "Tests en cours…"
        if status == "reviewing":
            return (f"{reviewer} relit le diff{diff_note} produit par {builder} — {gate}"
                    if reviewer else f"Vérification finale du diff{diff_note} — {gate}")
        if status == "correcting":
            return f"Correction demandée par la review — {builder} va reprendre le diff…"
        if status == "needs_approval":
            verdict = ""
            review = run.get("review_findings") or {}
            if isinstance(review, dict) and review.get("reviews"):
                verdicts = [f"{item.get('reviewer')}: {(item.get('proof') or {}).get('verdict') or item.get('decision', '?')}"
                            for item in review["reviews"]]
                verdict = " · review " + ", ".join(verdicts)
            elif run.get("review_policy") == "none":
                verdict = " · sans review (approbation humaine seule)"
            return f"Résultat prêt{diff_note}{verdict} — à toi de décider."
        if status == "accepted":
            return "Accepté — résultat conservé dans l'évidence."
        if status == "stopped":
            return "Arrêté — run archivé."
        if run.get("nothing_produced"):
            return "🔴 Rien n'a été produit — diff vide, aucun fichier livré."
        if status == "failed":
            return "Échec — " + (run.get("block_cause") or step or "les tests ont échoué")
        if status == "blocked":
            return "Bloqué — " + (run.get("block_cause") or step or "cause inconnue")
        if status == "paused":
            return "En pause (reprise interne possible)."
        return step or status
    # A5/F7: clear human states — no more raw 'ready' while a run executes.
    PHASE_LABELS = {
        "pending": "queued", "planning": "running", "ready": "running",
        "building": "running", "testing": "running", "reviewing": "running",
        "correcting": "running", "needs_approval": "done", "accepted": "done",
        "stopped": "stopped", "blocked": "failed", "failed": "failed", "paused": "paused",
    }
    def phase_label(self, run: dict[str, Any]) -> str:
        status = run.get("status")
        base = self.PHASE_LABELS.get(status, status or "?")
        if base == "running":
            return f"running · {self._gate_position(run)}"
        return base
    def estimate_eta(self, builder_name: str, review_policy: str, mission_length: int) -> dict[str, Any]:
        """Rough ETA from past terminal runs of the same config and mission size."""
        bucket = "small" if mission_length < 200 else "medium" if mission_length < 600 else "large"
        durations: list[int] = []
        folder = self.root / "runs"
        if folder.is_dir():
            for path in folder.glob("run-*/run.json"):
                try:
                    past = json.loads(path.read_text())
                except (OSError, json.JSONDecodeError):
                    continue
                if past.get("builder_name") != builder_name: continue
                if past.get("review_policy") != review_policy: continue
                if past.get("status") not in {"needs_approval", "accepted"}: continue
                length = len(self.user_mission(past.get("mission")))
                past_bucket = "small" if length < 200 else "medium" if length < 600 else "large"
                if past_bucket != bucket: continue
                try:
                    created = datetime.fromisoformat(past["created_at"].replace("Z", "+00:00"))
                    updated = datetime.fromisoformat(past["updated_at"].replace("Z", "+00:00"))
                except (KeyError, ValueError):
                    continue
                seconds = int((updated - created).total_seconds())
                if seconds > 0:
                    durations.append(seconds)
        if not durations:
            return {"eta_seconds": None, "based_on_runs": 0, "bucket": bucket}
        durations.sort()
        return {"eta_seconds": durations[len(durations) // 2],
                "based_on_runs": len(durations), "bucket": bucket}
    STEP_LABELS = {
        "planning": "Plan borné", "ready": "Plan validé", "building": "Écriture du code",
        "testing": "Tests", "reviewing": "Review du diff", "correcting": "Correction demandée",
        "needs_approval": "En attente de ta décision", "accepted": "Accepté",
        "stopped": "Arrêté", "blocked": "Bloqué", "failed": "Échec des tests", "paused": "Pause",
    }
    def get_timeline(self, run_id: str) -> list[dict[str, Any]]:
        """F9 — 'Ce qui s'est passé': readable steps with durations, from events.jsonl."""
        events = self.events(run_id)
        run = self._read(run_id)
        steps: list[dict[str, Any]] = []
        def seconds_between(start: str | None, end: str | None) -> int | None:
            if not start or not end:
                return None
            try:
                a = datetime.fromisoformat(start.replace("Z", "+00:00"))
                b = datetime.fromisoformat(end.replace("Z", "+00:00"))
                return max(0, int((b - a).total_seconds()))
            except ValueError:
                return None
        last_state: dict[str, Any] | None = None
        for event in events:
            kind = event.get("kind")
            at = event.get("at")
            if kind == "state_changed":
                to_status = event.get("to_status", "")
                label = self.STEP_LABELS.get(to_status, to_status)
                if to_status == "building" and event.get("reason") == "correction build":
                    label = "Re-build après correction"
                # Durations belong to state steps; review rows are point events
                # and must never absorb the elapsed time of the state they end.
                if last_state is not None and last_state["duration_seconds"] is None:
                    last_state["duration_seconds"] = seconds_between(last_state["at"], at)
                step = {"at": at, "label": label, "status": to_status,
                        "duration_seconds": None}
                steps.append(step)
                last_state = step
            elif kind == "review_completed":
                decision = str(event.get("decision", "")).upper()
                reviewer = self.PROVIDER_LABELS.get(event.get("reviewer"), event.get("reviewer"))
                steps.append({"at": at, "label": f"Review {event.get('stage')} par {reviewer}: "
                              + ("ACCEPT" if decision == "PASS" else decision),
                              "status": "review", "duration_seconds": None, "point": True})
            elif kind == "human_approval":
                steps.append({"at": at, "label": "Approbation humaine", "status": "human",
                              "duration_seconds": None, "point": True})
        if last_state is not None and last_state["duration_seconds"] is None:
            last_state["duration_seconds"] = seconds_between(last_state["at"], run.get("updated_at"))
        return steps
    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        """Persisted run summaries, newest first, for UI restoration."""
        folder = self.root / "runs"
        if not folder.is_dir():
            return []
        summaries: list[dict[str, Any]] = []
        for path in folder.glob("run-*/run.json"):
            try:
                run = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            summary = {key: run.get(key) for key in (
                "run_id", "status", "created_at", "updated_at", "current_step",
                "builder_name", "builder_provider", "builder_model",
                "reviewer_names", "review_policy", "review_semantics",
                "is_self_review", "no_review_label")}
            summary["mission_excerpt"] = self.user_mission(run.get("mission"))[:240]
            summaries.append(summary)
        summaries.sort(key=lambda item: item.get("created_at") or "", reverse=True)
        return summaries[:limit]
    QUOTA_RE = re.compile(r"quota|rate.?limit|usage.?limit", re.I)
    def quota_trace(self, run_id: str) -> dict[str, Any]:
        """Detect Codex quota evidence in a run regardless of its terminal state.

        Only evidence produced by a Codex adapter is considered, so mission text
        or findings about rate limiters can never raise a false quota alarm.
        """
        run = self._read(run_id)
        codex_involved = ("codex" in str(run.get("builder_provider", ""))
                          or "codex" in (run.get("reviewer_names") or []))
        if not codex_involved:
            return {"quota_blocked": False, "reset_hint": None}
        combined = "\n".join(self._evidence_fragments(run_id, run, provider_filter="codex"))
        quota = bool(self.QUOTA_RE.search(combined))
        reset = re.search(r"try again at\s+([^.\"\n]+)", combined, re.I)
        return {"quota_blocked": quota,
                "reset_hint": reset.group(1).strip() if reset else None}
    def _evidence_fragments(self, run_id: str, run: dict[str, Any], provider_filter: str | None = None) -> list[str]:
        folder = self._dir(run_id)
        fragments: list[str] = [] if provider_filter else [run.get("current_step") or ""]
        for path in sorted(folder.glob("*review-evidence*.json")) + [folder / "builder-evidence.json"]:
            if not path.is_file():
                continue
            try:
                data = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            reviews = data.get("reviews", [data] if isinstance(data, dict) else [])
            for item in reviews:
                if not isinstance(item, dict):
                    continue
                if provider_filter and provider_filter not in str(item.get("provider", "")) \
                        and provider_filter not in str(item.get("reviewer", "")):
                    continue
                for key in ("reason", "last_error", "stderr", "stdout_tail", "finding"):
                    value = item.get(key)
                    if isinstance(value, str) and value.strip():
                        fragments.append(value)
        return fragments
    def reviewer_verdicts(self, run_id: str) -> list[dict[str, Any]]:
        """Structured reviewer findings from every stage's review evidence (A6).

        Returns the real, intelligent verdicts (falsification detected, 9 is not
        prime, forbidden library…) so the UI can show them instead of a generic
        'plan review blocked'.
        """
        folder = self._dir(run_id)
        out: list[dict[str, Any]] = []
        for path in sorted(folder.glob("*-review-evidence*.json")):
            try:
                data = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            stage = data.get("stage") or path.name.split("-review", 1)[0]
            for item in data.get("reviews", []):
                if not isinstance(item, dict):
                    continue
                finding = (item.get("finding") or item.get("reason") or "").strip()
                verdict = item.get("verdict") or (item.get("decision") or "").upper()
                if finding and len(finding) >= 12 and verdict not in {"ACCEPT", "PASS"}:
                    out.append({"stage": stage, "reviewer": item.get("reviewer"),
                                "provider": item.get("provider"), "verdict": verdict,
                                "finding": finding[:2000]})
        return out
    def explain_block(self, run_id: str) -> dict[str, Any]:
        """Human-readable cause and unblock condition for a blocked/failed run."""
        run = self._read(run_id)
        if run["status"] not in {"blocked", "failed"}:
            return {"block_cause": None, "unblock_hint": None, "quota_blocked": False}
        # A1: an honest 'nothing produced' failure reads as such, in red.
        if run.get("nothing_produced"):
            return {"block_cause": "🔴 Rien n'a été produit — diff vide, aucun fichier livré. "
                                   "Aucun compteur vert, aucune approbation possible.",
                    "unblock_hint": "Reformule la demande ou relance : le builder n'a créé aucun fichier.",
                    "quota_blocked": False, "nothing_produced": True, "block_verdicts": []}
        # Quota attribution must stay codex-scoped, like quota_trace.
        trace = self.quota_trace(run_id)
        quota = trace["quota_blocked"]
        if quota:
            cause = "Quota fournisseur épuisé (Codex usage limit)"
            if trace.get("reset_hint"):
                cause += f" — reset annoncé: {trace['reset_hint']}"
            return {"block_cause": cause, "block_verdicts": [], "quota_blocked": True,
                    "unblock_hint": ("Retry relancera la même configuration après le retour du quota; "
                                     "Reject termine ce run immédiatement et libère l'interface.")}
        # A4/A6: surface the reviewer's real verdict, not the generic transition reason.
        verdicts = self.reviewer_verdicts(run_id)
        if verdicts:
            best = verdicts[-1]
            who = self.PROVIDER_LABELS.get(best.get("reviewer"), best.get("reviewer") or "reviewer")
            cause = f"{who} a bloqué la gate {best['stage']} ({best['verdict'] or 'BLOCK'}) : {best['finding'][:280]}"
            return {"block_cause": cause, "block_verdict_full": best["finding"],
                    "block_verdicts": verdicts, "quota_blocked": False,
                    "unblock_hint": "Le reviewer a refusé pour la raison ci-dessus. "
                                    "Corrige la mission ou relance ; consulte l'évidence pour le détail complet."}
        fragments = self._evidence_fragments(run_id, run)
        detail = next((frag.strip() for frag in fragments if frag.strip()), "cause inconnue")
        return {"block_cause": detail[:300], "block_verdicts": [], "quota_blocked": False,
                "unblock_hint": ("Retry relance une réparation bornée; Reject termine ce run. "
                                 "Consulte le dossier d'évidence pour le détail complet.")}
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
    def _result_file_target(self, run: dict[str, Any], relative: str) -> Path:
        """Resolve a deliverable path and refuse anything outside the run workspace."""
        workspace = Path(run["workspace"]).resolve()
        target = (workspace / relative).resolve()
        if target != workspace and workspace not in target.parents:
            raise RuntimeStateError("result path escapes the run workspace: " + relative)
        if target == workspace:
            raise RuntimeStateError("result path must name a file, not the workspace root")
        return target
    def _deliverable_copy(self, run_id: str, relative: str) -> Path:
        """Resolve a persistent evidence copy, confined to the deliverables dir."""
        root = (self._dir(run_id) / "deliverables").resolve()
        target = (root / relative).resolve()
        if target != root and root not in target.parents:
            raise RuntimeStateError("result path escapes the evidence deliverables: " + relative)
        if target == root:
            raise RuntimeStateError("result path must name a file, not the deliverables root")
        return target
    def _persist_deliverables(self, run: dict[str, Any], workspace: Path, folder: Path) -> None:
        """F4: results are served from the run's evidence directory forever,
        never from the disposable sandbox. Called at every final-diff capture,
        so repair loops refresh the copy."""
        target_root = folder / "deliverables"
        if target_root.exists():
            shutil.rmtree(target_root)
        manifest: list[dict[str, Any]] = []
        workspace_root = workspace.resolve()
        for relative in self._paths(workspace):
            source = workspace / relative
            # A symlink would be dereferenced by the copy and served forever:
            # refuse it, and confine the resolved path to the workspace.
            if source.is_symlink():
                manifest.append({"path": relative, "bytes": None, "sha256": None,
                                 "is_text": False, "skipped": "symlink refusé"})
                continue
            if not source.is_file():
                continue
            resolved = source.resolve()
            if resolved != workspace_root and workspace_root not in resolved.parents:
                manifest.append({"path": relative, "bytes": None, "sha256": None,
                                 "is_text": False, "skipped": "hors workspace"})
                continue
            # A hardlink resolves in-workspace yet shares content with an
            # outside file — same smuggling channel as a symlink.
            if source.stat().st_nlink > 1:
                manifest.append({"path": relative, "bytes": None, "sha256": None,
                                 "is_text": False, "skipped": "lien matériel refusé"})
                continue
            target = target_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            with source.open("rb") as handle:
                head = handle.read(8192)
            manifest.append({"path": relative, "bytes": target.stat().st_size,
                             "sha256": digest(target), "is_text": b"\0" not in head})
        atomic_write_json(folder / "deliverables-manifest.json",
                          {"schema_version": 1, "captured_at": now(), "files": manifest})
    def record_attachments(self, run_id: str, manifest: list[dict[str, Any]]) -> None:
        """Persist the composer attachments manifest as run evidence (BLOC E)."""
        atomic_write_json(self._dir(run_id) / "attachments.json",
                          {"schema_version": 1, "recorded_at": now(), "attachments": manifest})
    def _delivered_count(self, folder: Path) -> int:
        """Number of real deliverable files persisted for this run (A1 gate)."""
        manifest_path = folder / "deliverables-manifest.json"
        if not manifest_path.is_file():
            return 0
        try:
            manifest = json.loads(manifest_path.read_text())
        except (OSError, json.JSONDecodeError):
            return 0
        return sum(1 for entry in manifest.get("files", []) if entry.get("sha256"))
    def list_result_files(self, run_id: str) -> list[dict[str, Any]]:
        """Deliverable files, from the persistent evidence copy when it exists."""
        run = self._read(run_id)
        folder = self._dir(run_id)
        manifest_path = folder / "deliverables-manifest.json"
        if manifest_path.is_file():
            try:
                manifest = json.loads(manifest_path.read_text())
            except (OSError, json.JSONDecodeError):
                manifest = {"files": []}
            files = []
            for entry in manifest.get("files", []):
                copy = folder / "deliverables" / entry.get("path", "")
                files.append({**entry, "exists": copy.is_file(), "source": "evidence"})
            return files
        # Legacy runs recorded before deliverable persistence: fall back to the sandbox.
        changed_path = folder / "changed-paths.json"
        if not changed_path.is_file():
            return []
        try:
            changed = json.loads(changed_path.read_text())
        except (OSError, json.JSONDecodeError):
            return []
        files = []
        for relative in changed.get("after", []):
            entry: dict[str, Any] = {"path": relative, "exists": False, "bytes": None,
                                     "sha256": None, "is_text": False, "source": "workspace"}
            try:
                target = self._result_file_target(run, relative)
            except RuntimeStateError as exc:
                entry["error"] = str(exc)
                files.append(entry)
                continue
            if target.is_file():
                with target.open("rb") as handle:
                    head = handle.read(8192)
                entry.update({"exists": True, "bytes": target.stat().st_size,
                              "sha256": digest(target), "is_text": b"\0" not in head})
            files.append(entry)
        return files
    def get_result_summary(self, run_id: str) -> dict[str, Any]:
        """'What was built' digest a human can consult before deciding."""
        run = self._read(run_id)
        folder = self._dir(run_id)
        summary: dict[str, Any] = {
            "run_id": run_id, "status": run.get("status"),
            "result_available": (folder / "final-diff.patch").is_file(),
            "builder_name": run.get("builder_name"),
            "builder_provider": run.get("builder_provider"),
            "builder_model": run.get("builder_model"),
            "review_policy": run.get("review_policy"),
            "review_semantics": run.get("review_semantics"),
            "final_diff_sha256": run.get("final_diff_sha256"),
            "files": [], "tests": None,
        }
        if not summary["result_available"]:
            summary["reason"] = "aucun diff final enregistré pour ce run"
            return summary
        summary["files"] = self.list_result_files(run_id)
        tests_path = folder / "test-results.json"
        if tests_path.is_file():
            try:
                data = json.loads(tests_path.read_text())
                results = data.get("results", [])
                summary["tests"] = {
                    "commands": len(results),
                    "passed": sum(1 for item in results if item.get("ok")),
                    "all_passed": bool(data.get("all_passed")),
                    # A2/F9: real test CASES, not command count. 0 collected → grey, never green.
                    "cases_collected": data.get("cases_collected"),
                    "cases_passed": data.get("cases_passed"),
                    "real_tests_ran": bool(data.get("cases_collected")),
                    "argv": [item.get("argv") for item in results],
                }
            except (OSError, json.JSONDecodeError):
                summary["tests"] = {"error": "cannot read test-results.json"}
        return summary
    def read_result_file(self, run_id: str, relative: str) -> dict[str, Any]:
        """Raw bytes of one deliverable — evidence copy first, sandbox fallback."""
        run = self._read(run_id)
        copy = self._deliverable_copy(run_id, relative)
        if copy.is_file():
            raw = copy.read_bytes()
        else:
            target = self._result_file_target(run, relative)
            if not target.is_file():
                raise RuntimeStateError(
                    "résultat introuvable: " + relative
                    + " (ni copie d'évidence, ni fichier de sandbox)")
            raw = target.read_bytes()
        return {"path": relative, "bytes": len(raw), "is_text": b"\0" not in raw[:8192],
                "sha256": hashlib.sha256(raw).hexdigest(), "content": raw}
    def build_result_zip(self, run_id: str) -> dict[str, Any]:
        """Bundle the deliverables, final diff and summary for one-click download."""
        folder = self._dir(run_id)
        if not (folder / "final-diff.patch").is_file():
            raise RuntimeStateError("aucun diff final enregistré pour ce run")
        summary = self.get_result_summary(run_id)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
            for entry in summary["files"]:
                if not entry.get("exists"):
                    continue
                record = self.read_result_file(run_id, entry["path"])
                bundle.writestr("deliverables/" + entry["path"], record["content"])
            bundle.write(folder / "final-diff.patch", "final-diff.patch")
            bundle.writestr("result-summary.json",
                            json.dumps(summary, indent=2, sort_keys=True))
        raw = buffer.getvalue()
        return {"run_id": run_id, "filename": f"{run_id}-result.zip",
                "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
                "content": raw}
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
        return git_status_paths(workspace)
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
        disagreement = len(decisions) > 1
        decision = ("disagreement" if disagreement else
                    "block" if "block" in decisions else
                    "p1" if "p1" in decisions else "pass" if ok else "block")
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
                  "reviewer_disagreement": disagreement,
                  "reviews": reviews, "proof": {"verdict": "ACCEPT" if verified else "MISSING", "reviewed_diff_sha256": run.get("final_diff_sha256") if verified else None}, "is_self_review": run.get("is_self_review", False), "no_review": run.get("review_policy") == "none"}
        atomic_write_json(folder / f"{stage}-review-evidence.json", review)
        if run.get("corrections_used"):
            atomic_write_json(folder / f"{stage}-review-evidence-repair-{run['corrections_used']}.json", review)
        return review

    def _request_repair(self, run: dict[str, Any], review: dict[str, Any], stage: str) -> None:
        findings = [
            {"reviewer": item.get("reviewer"), "provider": item.get("provider"),
             "decision": item.get("decision"), "finding": item.get("finding") or item.get("reason")}
            for item in review.get("reviews", []) if item.get("decision") == "p1"
        ]
        run["repair_request"] = {"stage": stage, "findings": findings,
                                 "final_diff_sha256": run.get("final_diff_sha256")}
        self._write(run)

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
            if run["corrections_used"] >= run["max_corrections"]:
                self._transition(run, RunStatus.NEEDS_APPROVAL, "correction budget exhausted")
                return run
            run["corrections_used"] += 1
            self._write(run)
            self._transition(run, RunStatus.BUILDING, "correction build")
            return self._execute(run, True, building=True)
        if run["status"] in {"failed", "blocked"}: return self.retry(run_id)
        if run["status"] != "ready": raise RuntimeStateError("run cannot execute from " + run["status"])
        if not run.get("plan_review_completed"):
            plan_review = self._review_gate(run, "plan")
            if plan_review.get("decision") == "disagreement":
                self._transition(run, RunStatus.NEEDS_APPROVAL, "reviewer disagreement at plan gate")
                self._finalize(run); return run
            if not plan_review.get("ok"):
                self._transition(run, RunStatus.BLOCKED, "plan review blocked"); self._finalize(run); return run
            run["plan_review_completed"] = True
            self._write(run)
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
        if run["builder_name"] in {"codex", "claude"} and not run.get("isolated_workspace"):
            self._transition(run, RunStatus.BLOCKED, f"{run['builder_name'].title()} builder requires an isolated workspace"); self._finalize(run); return run
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
                        builder_mission = run["mission"]
                        if correction:
                            builder_mission += (
                                "\n\nJOAO BOUNDED REPAIR — address only this verified P1 and rerun tests:\n"
                                + json.dumps(run.get("repair_request", {}), indent=2, sort_keys=True)
                            )
                        builder = self.builders[run["builder_name"]].build(builder_mission, workspace, folder, profile.allowed_write_paths, correction)
                    except Exception as exc:
                        builder = {"ok": False, "provider": self.builders[run["builder_name"]].provider, "reason": f"builder exception: {type(exc).__name__}: {exc}"}
            except LockAcquireError:
                self._transition(run, RunStatus.BLOCKED, "second builder refused"); self._finalize(run); return run
            after = self._paths(workspace); changed = after; violations = detect_path_violations(changed, profile)
            atomic_write_json(folder / "changed-paths.json", {"before": before, "after": after, "changed_by_builder": changed, "violations": violations})
            builder.setdefault("selected_builder", run["builder_name"])
            builder.setdefault("selected_provider", run["builder_provider"])
            builder.setdefault("selected_model", run["builder_model"])
            builder.setdefault("review_policy", run["review_policy"])
            evidence_path = folder / "builder-evidence.json"
            if correction and evidence_path.exists() and not (folder / "builder-initial-evidence.json").exists():
                atomic_write_json(folder / "builder-initial-evidence.json", json.loads(evidence_path.read_text()))
            atomic_write_json(evidence_path, builder)
            if correction:
                atomic_write_json(folder / f"builder-repair-{run['corrections_used']}-evidence.json", builder)
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
        self._persist_deliverables(run, workspace, folder)
        # A1 fail-closed: an empty diff (0 bytes → EMPTY_DIFF_SHA256) or zero
        # delivered files means the builder produced nothing. Never let that
        # reach a green counter or needs_approval — fail honestly.
        delivered = self._delivered_count(folder)
        if run["final_diff_sha256"] == EMPTY_DIFF_SHA256 or delivered == 0:
            run["nothing_produced"] = True; self._write(run)
            self._event(run, "nothing_produced",
                        final_diff_sha256=run["final_diff_sha256"], delivered=delivered)
            self._transition(run, RunStatus.FAILED, "nothing_produced: le builder n'a produit aucun livrable")
            self._finalize(run); return run
        build_review = self._review_gate(run, "build")
        if self._apply_control(run):
            return run
        if build_review.get("decision") == "p1" and run["corrections_used"] < run["max_corrections"]:
            self._request_repair(run, build_review, "build")
            self._transition(run, RunStatus.CORRECTING, "build review P1; one repair permitted"); return run
        if build_review.get("decision") == "disagreement":
            self._transition(run, RunStatus.NEEDS_APPROVAL, "reviewer disagreement at build gate")
            self._finalize(run); return run
        if not build_review.get("ok"):
            self._transition(run, RunStatus.BLOCKED, "build review blocked"); self._finalize(run); return run
        self._transition(run, RunStatus.TESTING, "targeted and full tests")
        results = [self.tests.run(argv, workspace, profile.command_timeout_seconds) for argv in run["targeted_tests"] + run["full_tests"]]; atomic_write_json(folder / "test-results.json", {"results": results, "all_passed": all(item["ok"] for item in results), **parse_test_cases(results)})
        if self._apply_control(run):
            return run
        if not all(item["ok"] for item in results): self._transition(run, RunStatus.FAILED, "tests failed"); self._finalize(run); return run
        remaining_generated = sorted(set(self._paths(workspace)) & set(profile.generated_paths))
        if remaining_generated:
            cleanup_review = {"reviews": [{
                "reviewer": "deterministic-cleanup-gate", "provider": "joao-local",
                "decision": "p1", "finding": "Remove generated delivery artifacts: " + ", ".join(remaining_generated),
            }]}
            if run["corrections_used"] < run["max_corrections"]:
                self._request_repair(run, cleanup_review, "cleanup")
                self._transition(run, RunStatus.CORRECTING, "generated artifacts remain; one cleanup repair permitted")
                return run
            self._transition(run, RunStatus.BLOCKED, "generated artifacts remain after repair")
            self._finalize(run)
            return run
        run["tasks"][2]["status"] = "completed"; self._transition(run, RunStatus.REVIEWING, "independent test review")
        test_review = self._review_gate(run, "test")
        if self._apply_control(run):
            return run
        if test_review.get("decision") == "p1" and run["corrections_used"] < run["max_corrections"]:
            self._request_repair(run, test_review, "test")
            self._transition(run, RunStatus.CORRECTING, "test review P1; one repair permitted"); return run
        if test_review.get("decision") == "disagreement":
            self._transition(run, RunStatus.NEEDS_APPROVAL, "reviewer disagreement at test gate")
            self._finalize(run); return run
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
                self._request_repair(run, review, "final")
                self._transition(run, RunStatus.CORRECTING, "P1; one repair permitted"); return run
            if review.get("decision") == "disagreement":
                self._transition(run, RunStatus.NEEDS_APPROVAL, "reviewer disagreement at final gate")
                self._finalize(run); return run
            if not review.get("ok") or review.get("decision") == "block":
                self._transition(run, RunStatus.BLOCKED, "review blocked"); self._finalize(run); return run
            run["tasks"][3]["status"] = "completed"; self._transition(run, RunStatus.NEEDS_APPROVAL, "review completed; human decision"); self._finalize(run); return run
