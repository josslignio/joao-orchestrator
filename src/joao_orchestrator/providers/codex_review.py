"""Read-only Codex subscription adapter for planning and independent review.

The existing :mod:`codex_subscription` adapter is a write-tier coder.  This
adapter is deliberately separate: it runs ``codex exec`` in read-only mode,
parses the JSONL event stream, accepts only a completed final agent message,
and rejects any filesystem mutation of the supplied execution root.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Iterable, Optional

from .base import ProviderAdapter, ProviderRequest, ProviderResponse
from .subprocess_cli import CLIEngineConfig, SubprocessCLIEngine


_PROVIDER_FAILURE_MARKERS = (
    "you've hit your weekly limit",
    "you have hit your weekly limit",
    "rate limit",
    "quota exceeded",
    "usage limit",
    "authentication required",
    "not logged in",
    "please log in",
    "please login",
)


def classify_provider_failure(text: str) -> str:
    """Return an honest provider failure reason for known non-answer content."""
    normalized = " ".join(str(text).lower().split())
    for marker in _PROVIDER_FAILURE_MARKERS:
        if marker in normalized:
            return f"provider unavailable: {marker}"
    return ""


def parse_codex_jsonl(raw: str) -> str:
    """Extract the final Codex agent message from a completed JSONL turn.

    Codex ``exec --json`` emits ``item.completed`` records whose final answer is
    an ``item.type == 'agent_message'``.  The last such message is authoritative.
    A successful process without ``turn.completed`` or without an agent message
    is rejected rather than treated as an empty/format-variant success.
    """
    messages: list[str] = []
    turn_completed = False
    fatal_errors: list[str] = []

    for line in str(raw).splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("type", ""))
        if event_type == "turn.completed":
            turn_completed = True
        elif event_type in {"turn.failed", "error"}:
            fatal_errors.append(str(event.get("message") or event.get("error") or event_type))
        elif event_type == "item.completed":
            item = event.get("item") or {}
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type", ""))
            if item_type == "agent_message":
                text = str(item.get("text", "")).strip()
                if text:
                    messages.append(text)
            elif item_type == "error" and not messages:
                fatal_errors.append(str(item.get("message", "Codex item error")))

    if not turn_completed:
        detail = "; ".join(fatal_errors[-3:])
        raise ValueError("Codex turn did not complete" + (f": {detail}" if detail else ""))
    if not messages:
        detail = "; ".join(fatal_errors[-3:])
        raise ValueError("Codex returned no final agent message" + (f": {detail}" if detail else ""))

    final = messages[-1].strip()
    failure = classify_provider_failure(final)
    if failure:
        raise ValueError(failure)
    return final


def _execution_state(root: Path) -> str:
    """Return a deterministic mutation fingerprint for Git and non-Git roots."""
    if (root / ".git").exists():
        completed = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain=v1", "--untracked-files=all"],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"git status failed: {completed.stderr.strip()}")
        return "git:" + completed.stdout

    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return "tree:" + digest.hexdigest()


class CodexReviewProvider(ProviderAdapter):
    """Subscription-backed Codex reviewer with no write-tier capability."""

    name = "codex-review"
    supported_roles = ("planner", "reviewer", "researcher", "tester")
    network_required = True

    def __init__(self, engine: SubprocessCLIEngine):
        self.engine = engine
        self.last_result = None

    @classmethod
    def default(
        cls,
        environment_allowlist: Iterable[str],
        timeout_seconds: int = 600,
        executable: str = "codex",
    ) -> "CodexReviewProvider":
        codex_home = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
        config = CLIEngineConfig(
            name=cls.name,
            executable=executable,
            base_args=[],
            timeout_seconds=timeout_seconds,
            environment_allowlist=[*list(environment_allowlist), "CODEX_HOME"],
            required_auth_paths=[
                str(codex_home / "auth.json"),
                str(codex_home / "auth.toml"),
            ],
        )
        return cls(SubprocessCLIEngine(config))

    def is_enabled(self) -> bool:
        return self.engine.is_available() and self.engine.has_required_auth()

    def invoke(self, request: ProviderRequest) -> ProviderResponse:
        if request.role not in self.supported_roles:
            return ProviderResponse(
                role=request.role,
                task_id=request.task_id,
                ok=False,
                error=f"unsupported role: {request.role}",
                provider_name=self.name,
            )
        if not request.worktree_path:
            return ProviderResponse(
                role=request.role,
                task_id=request.task_id,
                ok=False,
                error="isolated execution root is required",
                provider_name=self.name,
            )

        root = Path(request.worktree_path).expanduser().resolve()
        if not root.is_dir():
            return ProviderResponse(
                role=request.role,
                task_id=request.task_id,
                ok=False,
                error=f"execution root is not a directory: {root}",
                provider_name=self.name,
            )

        try:
            before = _execution_state(root)
            result = self.engine.run_argv(
                root,
                [
                    "exec",
                    "--json",
                    "--sandbox", "read-only",
                    "--ephemeral",
                    "--skip-git-repo-check",
                    "--color", "never",
                    request.prompt,
                ],
                env_overrides={
                    "GIT_TERMINAL_PROMPT": "0",
                    "GIT_OPTIONAL_LOCKS": "0",
                },
            )
            self.last_result = result
            after = _execution_state(root)
            if before != after:
                return ProviderResponse(
                    role=request.role,
                    task_id=request.task_id,
                    ok=False,
                    error="read-only Codex review mutated the execution root",
                    changed_paths=[after],
                    provider_name=self.name,
                )
            if not result.ok:
                return ProviderResponse(
                    role=request.role,
                    task_id=request.task_id,
                    ok=False,
                    error=result.stderr or f"Codex exited with {result.returncode}",
                    provider_name=self.name,
                )
            content = parse_codex_jsonl(result.stdout)
        except Exception as exc:
            return ProviderResponse(
                role=request.role,
                task_id=request.task_id,
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
                provider_name=self.name,
            )

        return ProviderResponse(
            role=request.role,
            task_id=request.task_id,
            ok=True,
            content=content,
            provider_name=self.name,
        )
