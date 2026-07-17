#!/usr/bin/env python3
"""Stable, fail-closed JOÃO adapter for OpenCode + Z.AI Coding Plan.

This executable is intentionally standalone (stdlib only) so it can be copied to
``~/.local/bin/joao-glm`` without importing the repository package.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
DEFAULT_MODEL = "zai-coding-plan/glm-4.5-air"
DEFAULT_PROVIDER = "zai-coding-plan"
AUTH_PATH = Path("~/.local/share/opencode/auth.json").expanduser()
SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|bearer)"
    r"\s*[:=]\s*([^\s\",}]+)"
)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
    with tmp.open("wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def redact(text: str) -> str:
    if not text:
        return ""
    text = SECRET_RE.sub(lambda m: f"{m.group(1)}=<redacted>", text)
    # Avoid accidentally persisting the complete auth file path contents in an error.
    return text[:1_000_000]


def run_cmd(argv: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None,
            timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=str(cwd) if cwd else None,
        env=env,
        capture_output=True,
        text=True,
        shell=False,
        timeout=timeout,
    )


def git_changed_paths(workspace: Path) -> list[str]:
    proc = run_cmd(["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"], cwd=workspace)
    if proc.returncode != 0:
        raise RuntimeError(f"workspace is not a readable Git worktree: {redact(proc.stderr)}")
    changed: list[str] = []
    for entry in proc.stdout.split("\0"):
        if not entry:
            continue
        # XY + space + path. Rename records may include an extra path entry; both are safe to record.
        raw = entry[3:] if len(entry) >= 4 else entry
        if " -> " in raw:
            raw = raw.split(" -> ", 1)[1]
        changed.append(raw.replace("\\", "/"))
    return sorted(set(changed))


def path_allowed(path: str, rules: list[str]) -> bool:
    norm = path.lstrip("./").replace("\\", "/")
    for raw in rules:
        rule = raw.lstrip("./").replace("\\", "/")
        if rule.endswith("/**"):
            prefix = rule[:-3].rstrip("/")
            if norm == prefix or norm.startswith(prefix + "/"):
                return True
        elif rule.endswith("/"):
            prefix = rule.rstrip("/")
            if norm == prefix or norm.startswith(prefix + "/"):
                return True
        elif norm == rule:
            return True
    return False


def safe_opencode_config(mode: str, model: str) -> dict[str, Any]:
    common: dict[str, Any] = {
        "$schema": "https://opencode.ai/config.json",
        "model": model,
        "enabled_providers": [DEFAULT_PROVIDER],
        "share": "disabled",
        "autoupdate": False,
        "snapshot": False,
        "mcp": {},
        "permission": {
            "*": "deny",
            "read": {"*": "allow", "*.env": "deny", "*.env.*": "deny", "*.pem": "deny", "*.key": "deny"},
            "glob": "allow",
            "grep": "allow",
            "lsp": "allow",
            "task": "deny",
            "skill": "deny",
            "question": "deny",
            "webfetch": "deny",
            "websearch": "deny",
            "external_directory": "deny",
        },
    }
    if mode == "read-only":
        common["permission"].update({"edit": "deny", "bash": "deny"})
        return common

    common["permission"].update({
        "edit": "allow",
        "bash": {
            "*": "deny",
            "pwd": "allow",
            "ls*": "allow",
            "find *": "allow",
            "grep *": "allow",
            "sed *": "allow",
            "cat *": "allow",
            "head *": "allow",
            "tail *": "allow",
            "wc *": "allow",
            "git status*": "allow",
            "git diff*": "allow",
            "git ls-files*": "allow",
            "git rev-parse*": "allow",
            "git show*": "allow",
            "python -m pytest*": "allow",
            "python3 -m pytest*": "allow",
            "pytest*": "allow",
            "python -m compileall*": "allow",
            "python3 -m compileall*": "allow",
            "python -m pip *": "deny",
            "python3 -m pip *": "deny",
            "pip *": "deny",
            "pip3 *": "deny",
            "npm *": "deny",
            "pnpm *": "deny",
            "yarn *": "deny",
            "bun *": "deny",
            "brew *": "deny",
            "curl *": "deny",
            "wget *": "deny",
            "rm *": "deny",
            "sudo *": "deny",
            "git add*": "deny",
            "git commit*": "deny",
            "git push*": "deny",
            "git merge*": "deny",
            "git rebase*": "deny",
            "git reset*": "deny",
            "git clean*": "deny",
            "git checkout*": "deny",
            "git switch*": "deny",
        },
    })
    return common


def clean_env(config: dict[str, Any]) -> dict[str, str]:
    env = dict(os.environ)
    for key in list(env):
        upper = key.upper()
        if any(token in upper for token in (
            "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ZAI_API_KEY", "ZHIPU_API_KEY",
            "GITHUB_TOKEN", "GH_TOKEN", "CODEX_API_KEY",
        )):
            env.pop(key, None)
    env.update({
        "OPENCODE_CONFIG_CONTENT": json.dumps(config, sort_keys=True, separators=(",", ":")),
        "OPENCODE_DISABLE_AUTOUPDATE": "1",
        "OPENCODE_DISABLE_DEFAULT_PLUGINS": "1",
        "OPENCODE_DISABLE_CLAUDE_CODE": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_ADDOPTS": "-p no:cacheprovider",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    })
    return env


TRANSIENT_CACHE_RE = re.compile(r"(^|/)(__pycache__(/|$)|[^/]+\.py[co]$|\.pytest_cache(/|$))")


def remove_transient_caches(workspace: Path, before: list[str]) -> list[str]:
    """Delete interpreter caches the task run introduced; they are never deliverables."""
    workspace = workspace.resolve()
    baseline = set(before)
    removed: list[str] = []
    for path in git_changed_paths(workspace):
        if path in baseline or not TRANSIENT_CACHE_RE.search(path):
            continue
        target = (workspace / path).resolve()
        try:
            target.relative_to(workspace)
        except ValueError:
            continue
        if target.is_file() or target.is_symlink():
            target.unlink(missing_ok=True)
            removed.append(path)
            parent = target.parent
            while parent != workspace and parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
                parent = parent.parent
    return sorted(removed)


def probe(opencode: str, model: str) -> dict[str, Any]:
    exe = shutil.which(opencode) if os.sep not in opencode else str(Path(opencode).expanduser().resolve())
    if not exe or not Path(exe).is_file():
        raise RuntimeError(f"OpenCode executable unavailable: {opencode}")
    version = run_cmd([exe, "--version"], timeout=15)
    run_help = run_cmd([exe, "run", "--help"], timeout=15)
    auth_list = run_cmd([exe, "auth", "list"], timeout=20)
    model_list = run_cmd([exe, "models", DEFAULT_PROVIDER], timeout=30)
    help_text = run_help.stdout + "\n" + run_help.stderr
    required_flags = ["--model", "--agent", "--format", "--dir", "--auto"]
    missing_flags = [flag for flag in required_flags if flag not in help_text]
    provider_visible = DEFAULT_PROVIDER in (auth_list.stdout + auth_list.stderr + model_list.stdout + model_list.stderr)
    model_visible = model in (model_list.stdout + model_list.stderr)
    available = (
        version.returncode == 0 and run_help.returncode == 0 and auth_list.returncode == 0
        and AUTH_PATH.is_file() and not missing_flags and provider_visible
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "available": available,
        "executable": exe,
        "version": redact(version.stdout.strip() or version.stderr.strip()),
        "auth_file_present": AUTH_PATH.is_file(),
        "provider": DEFAULT_PROVIDER,
        "provider_visible": provider_visible,
        "model": model,
        "model_visible": model_visible,
        "required_flags": required_flags,
        "missing_flags": missing_flags,
        "returncodes": {
            "version": version.returncode,
            "run_help": run_help.returncode,
            "auth_list": auth_list.returncode,
            "models": model_list.returncode,
        },
        "checked_at": now_iso(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Fail-closed JOÃO GLM adapter")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--task-file")
    parser.add_argument("--output", required=True)
    parser.add_argument("--mode", choices=("read-only", "workspace-write"), default="read-only")
    parser.add_argument("--budget", choices=("small", "normal", "large"), default="normal")
    parser.add_argument("--model", default=os.environ.get("JOAO_GLM_MODEL", DEFAULT_MODEL))
    parser.add_argument("--opencode", default=os.environ.get("JOAO_OPENCODE", "opencode"))
    parser.add_argument("--allowed-path", action="append", default=[])
    parser.add_argument("--baseline-path", action="append", default=[])
    parser.add_argument("--probe-only", action="store_true")
    args = parser.parse_args()

    started = now_iso()
    start = time.monotonic()
    workspace = Path(args.workspace).expanduser().resolve()
    output = Path(args.output).expanduser().resolve()
    evidence_path = output.with_suffix(output.suffix + ".evidence.json")

    evidence: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "started_at": started,
        "workspace": str(workspace),
        "mode": args.mode,
        "budget": args.budget,
        "provider": DEFAULT_PROVIDER,
        "model": args.model,
        "allowed_paths": sorted(set(args.allowed_path)),
        "baseline_paths": sorted(set(args.baseline_path)),
        "fallback_used": False,
        "legacy_mcp_used": False,
    }

    try:
        if not workspace.is_dir():
            raise RuntimeError(f"workspace is not a directory: {workspace}")
        try:
            output.relative_to(workspace)
        except ValueError:
            pass
        else:
            raise RuntimeError("--output must be outside the workspace to preserve change attribution")

        capability = probe(args.opencode, args.model)
        evidence["capability"] = capability
        if not capability["available"]:
            raise RuntimeError(f"OpenCode/Z.AI capability probe failed: {capability}")

        if args.probe_only:
            atomic_write(output, canonical_json_bytes(capability))
            evidence.update({"ok": True, "returncode": 0, "finished_at": now_iso()})
            evidence["duration_seconds"] = round(time.monotonic() - start, 3)
            atomic_write(evidence_path, canonical_json_bytes(evidence))
            print(json.dumps({"ok": True, "probe": capability}, sort_keys=True))
            return 0

        if not args.task_file:
            raise RuntimeError("--task-file is required unless --probe-only is used")
        task_file = Path(args.task_file).expanduser().resolve()
        if not task_file.is_file():
            raise RuntimeError(f"task file unavailable: {task_file}")
        task_bytes = task_file.read_bytes()
        limits = {"small": 40_000, "normal": 120_000, "large": 220_000}
        if len(task_bytes) > limits[args.budget]:
            raise RuntimeError(f"task file exceeds {args.budget} budget ({len(task_bytes)} bytes)")
        task_text = task_bytes.decode("utf-8")
        evidence["task_file"] = str(task_file)
        evidence["task_sha256"] = sha256_bytes(task_bytes)
        evidence["task_bytes"] = len(task_bytes)

        before = git_changed_paths(workspace)
        evidence["changed_paths_before"] = before
        if args.mode == "workspace-write" and before != sorted(set(args.baseline_path)):
            raise RuntimeError("workspace-write dirty baseline differs from explicit baseline contract")

        config = safe_opencode_config(args.mode, args.model)
        env = clean_env(config)
        exe = capability["executable"]
        agent = "plan" if args.mode == "read-only" else "build"
        prompt = (
            "Execute the attached JOÃO task contract exactly. Do not expand scope. "
            "Do not install dependencies, commit, push, access external paths, use web tools, "
            "or expose secrets. Finish the task and report concise evidence.\n\n"
            + task_text
        )
        argv = [
            exe, "run", "--format", "json", "--agent", agent, "--auto",
            "--model", args.model, "--dir", str(workspace), prompt,
        ]
        timeouts = {"small": 300, "normal": 900, "large": 1800}
        proc = run_cmd(argv, cwd=workspace, env=env, timeout=timeouts[args.budget])
        evidence["transient_cache_paths_removed"] = remove_transient_caches(workspace, before)
        after = git_changed_paths(workspace)
        evidence["changed_paths_after"] = after
        changed_by_task = sorted(set(after) - set(before))
        evidence["changed_paths_by_task"] = changed_by_task
        unauthorized = [path for path in changed_by_task if not path_allowed(path, args.allowed_path)]
        if args.mode == "read-only":
            # Read-only means the task itself changed nothing; pre-existing
            # uncommitted work (e.g. a builder diff under review) is legitimate.
            unauthorized = changed_by_task
        evidence["unauthorized_paths"] = unauthorized
        evidence["argv"] = argv[:-1] + [f"<prompt sha256={sha256_bytes(prompt.encode('utf-8'))}>"]
        evidence["config_sha256"] = sha256_bytes(canonical_json_bytes(config))
        evidence["returncode"] = proc.returncode
        evidence["stdout_sha256"] = sha256_bytes(proc.stdout.encode("utf-8"))
        evidence["stderr_sha256"] = sha256_bytes(proc.stderr.encode("utf-8"))
        evidence["stderr_excerpt"] = redact(proc.stderr)[-4000:]

        raw_output = proc.stdout.encode("utf-8")
        atomic_write(output, raw_output)
        evidence["output"] = str(output)
        evidence["output_sha256"] = sha256_file(output)
        evidence["output_bytes"] = output.stat().st_size

        if proc.returncode != 0:
            raise RuntimeError(f"OpenCode returned {proc.returncode}: {redact(proc.stderr)[-2000:]}")
        if not proc.stdout.strip():
            raise RuntimeError("OpenCode returned empty output")
        if unauthorized:
            raise RuntimeError(f"unauthorized workspace changes: {unauthorized}")

        evidence.update({"ok": True, "finished_at": now_iso()})
        evidence["duration_seconds"] = round(time.monotonic() - start, 3)
        atomic_write(evidence_path, canonical_json_bytes(evidence))
        print(json.dumps({
            "ok": True,
            "provider": DEFAULT_PROVIDER,
            "model": args.model,
            "mode": args.mode,
            "changed_paths": after,
            "output": str(output),
            "evidence": str(evidence_path),
        }, sort_keys=True))
        return 0
    except subprocess.TimeoutExpired as exc:
        evidence.update({"ok": False, "returncode": 124, "error": f"timeout: {exc}", "finished_at": now_iso()})
    except Exception as exc:  # fail closed with structured evidence
        evidence.update({"ok": False, "returncode": 1, "error": str(exc), "finished_at": now_iso()})

    evidence["duration_seconds"] = round(time.monotonic() - start, 3)
    try:
        atomic_write(evidence_path, canonical_json_bytes(evidence))
    except Exception:
        pass
    print(json.dumps({"ok": False, "error": evidence.get("error", "unknown"), "evidence": str(evidence_path)}, sort_keys=True), file=sys.stderr)
    return int(evidence.get("returncode", 1) or 1)


if __name__ == "__main__":
    raise SystemExit(main())
