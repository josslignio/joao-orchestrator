#!/usr/bin/env python3
"""Prove joao-glm workspace-write on an isolated non-product fixture.

The proof artifact is fail-safe: it is created before any mutable operation and
atomically finalized from ``finally`` on every exit path.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

EXPECTED = "JOAO_GLM_WORKSPACE_WRITE_OK"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def unique_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return f"{stamp}-pid{os.getpid()}-{secrets.token_hex(4)}"


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{secrets.token_hex(3)}")
    with tmp.open("wb") as fh:
        fh.write(canonical_bytes(value))
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def run(argv: list[str], cwd: Path | None = None, timeout: int = 1200) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        shell=False,
        timeout=timeout,
    )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def git_text(fixture: Path, *args: str) -> str:
    proc = run(["git", *args], fixture, timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr[-2000:]}")
    return proc.stdout


def changed_paths(fixture: Path) -> list[str]:
    raw = git_text(fixture, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    paths: list[str] = []
    for entry in raw.split("\0"):
        if not entry:
            continue
        path = entry[3:] if len(entry) >= 4 else entry
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        paths.append(path.replace("\\", "/"))
    return sorted(set(paths))


def parse_adapter_stdout(stdout: str) -> dict[str, Any] | None:
    for line in reversed([line.strip() for line in stdout.splitlines() if line.strip()]):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--adapter", default="~/.local/bin/joao-glm")
    parser.add_argument("--evidence-root", default="~/.local/share/joao/capabilities/glm")
    args = parser.parse_args()

    started = now_iso()
    monotonic_start = time.monotonic()
    adapter = Path(args.adapter).expanduser().resolve()
    run_dir = Path(args.evidence_root).expanduser().resolve() / f"workspace-write-{unique_id()}"
    run_dir.mkdir(parents=True, exist_ok=False)
    fixture = run_dir / "fixture-repo"
    proof_path = run_dir / "WORKSPACE_WRITE_PROOF.json"
    task = run_dir / "TASK.md"
    output = run_dir / "OPENCODE_OUTPUT.jsonl"
    adapter_evidence = output.with_suffix(output.suffix + ".evidence.json")

    proof: dict[str, Any] = {
        "schema_version": 2,
        "status": "running",
        "passed": False,
        "started_at": started,
        "finished_at": None,
        "duration_seconds": None,
        "proof_path": str(proof_path),
        "evidence_directory": str(run_dir),
        "fixture": str(fixture),
        "adapter": str(adapter),
        "adapter_sha256": sha256(adapter) if adapter.is_file() else None,
        "provider": "zai-coding-plan",
        "model": "zai-coding-plan/glm-4.5-air",
        "expected_text": EXPECTED,
        "expected_normalized_sha256": sha256_bytes(EXPECTED.encode("utf-8")),
        "returncode": None,
        "exception_type": None,
        "exception_message": None,
        "checks": {},
    }
    atomic_json(proof_path, proof)

    exit_code = 1
    proc: subprocess.CompletedProcess[str] | None = None
    try:
        if not adapter.is_file():
            raise RuntimeError(f"adapter unavailable: {adapter}")

        fixture.mkdir(parents=True, exist_ok=False)
        for argv in (
            ["git", "init", "-q"],
            ["git", "config", "user.email", "joao-fixture@example.invalid"],
            ["git", "config", "user.name", "JOAO Fixture"],
        ):
            init = run(argv, fixture, timeout=30)
            if init.returncode != 0:
                raise RuntimeError(f"{' '.join(argv)} failed: {init.stderr[-2000:]}")

        readme = fixture / "README.md"
        readme.write_text("# JOAO GLM isolated fixture\n", encoding="utf-8")
        add = run(["git", "add", "README.md"], fixture, timeout=30)
        commit = run(["git", "commit", "-q", "-m", "fixture baseline"], fixture, timeout=30)
        if add.returncode != 0 or commit.returncode != 0:
            raise RuntimeError(f"fixture baseline failed: {add.stderr[-1000:]} {commit.stderr[-1000:]}")

        head_before = git_text(fixture, "rev-parse", "HEAD").strip()
        readme_before = sha256(readme)
        proof["fixture_head_before"] = head_before
        proof["readme_sha256_before"] = readme_before

        task.write_text(
            "# Bounded fixture task\n\n"
            "Create exactly one file named `result.txt` in the workspace root. "
            "Its complete UTF-8 text, ignoring at most one optional final newline, must be:\n\n"
            "```text\nJOAO_GLM_WORKSPACE_WRITE_OK\n```\n\n"
            "Do not modify README.md. Do not create another file. Do not commit. "
            "Do not run package managers.\n",
            encoding="utf-8",
        )
        proof["task_sha256"] = sha256(task)
        proof["task_bytes"] = task.stat().st_size

        argv = [
            str(adapter),
            "--workspace", str(fixture),
            "--task-file", str(task),
            "--output", str(output),
            "--mode", "workspace-write",
            "--budget", "small",
            "--allowed-path", "result.txt",
        ]
        proof["command"] = argv
        proc = run(argv, timeout=1200)
        proof["returncode"] = proc.returncode
        proof["stdout_sha256"] = sha256_bytes(proc.stdout.encode("utf-8", errors="replace"))
        proof["stderr_sha256"] = sha256_bytes(proc.stderr.encode("utf-8", errors="replace"))
        proof["stdout_excerpt"] = proc.stdout[-8000:]
        proof["stderr_excerpt"] = proc.stderr[-8000:]
        proof["adapter_result"] = parse_adapter_stdout(proc.stdout)

        result = fixture / "result.txt"
        head_after = git_text(fixture, "rev-parse", "HEAD").strip()
        readme_after = sha256(readme)
        changed = changed_paths(fixture)
        result_bytes = result.read_bytes() if result.is_file() else b""
        try:
            result_text = result_bytes.decode("utf-8")
            utf8_valid = True
        except UnicodeDecodeError:
            result_text = ""
            utf8_valid = False
        normalized = result_text[:-1] if result_text.endswith("\n") else result_text

        adapter_payload: dict[str, Any] | None = None
        if adapter_evidence.is_file():
            try:
                loaded = json.loads(adapter_evidence.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    adapter_payload = loaded
            except Exception as exc:
                proof["adapter_evidence_parse_error"] = f"{type(exc).__name__}: {exc}"

        checks = {
            "adapter_returncode_zero": proc.returncode == 0,
            "result_exists": result.is_file(),
            "result_utf8_valid": utf8_valid,
            "result_text_matches": normalized == EXPECTED,
            "only_result_changed": changed == ["result.txt"],
            "readme_unchanged": readme_before == readme_after,
            "head_unchanged": head_before == head_after,
            "adapter_evidence_exists": adapter_evidence.is_file(),
            "adapter_evidence_ok": bool(adapter_payload and adapter_payload.get("ok") is True),
            "adapter_evidence_mode_workspace_write": bool(adapter_payload and adapter_payload.get("mode") == "workspace-write"),
            "adapter_evidence_paths_bounded": bool(adapter_payload and adapter_payload.get("unauthorized_paths") == []),
        }
        proof.update({
            "fixture_head_after": head_after,
            "readme_sha256_after": readme_after,
            "changed_paths": changed,
            "result_bytes": len(result_bytes),
            "result_sha256": sha256_bytes(result_bytes) if result.is_file() else None,
            "result_normalized_sha256": sha256_bytes(normalized.encode("utf-8")) if utf8_valid else None,
            "output_path": str(output),
            "output_sha256": sha256(output) if output.is_file() else None,
            "adapter_evidence_path": str(adapter_evidence),
            "adapter_evidence_sha256": sha256(adapter_evidence) if adapter_evidence.is_file() else None,
            "adapter_evidence": adapter_payload,
            "checks": checks,
        })
        proof["passed"] = all(checks.values())
        proof["status"] = "passed" if proof["passed"] else "failed"
        exit_code = 0 if proof["passed"] else 1
    except subprocess.TimeoutExpired as exc:
        proof["status"] = "failed"
        proof["returncode"] = 124
        proof["exception_type"] = type(exc).__name__
        proof["exception_message"] = str(exc)
        exit_code = 124
    except Exception as exc:
        proof["status"] = "failed"
        proof["exception_type"] = type(exc).__name__
        proof["exception_message"] = str(exc)
        if proof.get("returncode") is None:
            proof["returncode"] = 1
        exit_code = 1
    finally:
        proof["finished_at"] = now_iso()
        proof["duration_seconds"] = round(time.monotonic() - monotonic_start, 3)
        try:
            atomic_json(proof_path, proof)
        except Exception as write_exc:
            fallback = {
                "schema_version": 2,
                "status": "proof_write_failed",
                "passed": False,
                "proof_path": str(proof_path),
                "exception_type": type(write_exc).__name__,
                "exception_message": str(write_exc),
            }
            print(json.dumps(fallback, sort_keys=True), file=sys.stderr)
            return 2

    print(json.dumps({
        "passed": bool(proof.get("passed")),
        "proof_path": str(proof_path),
        "evidence_directory": str(run_dir),
        "returncode": proof.get("returncode"),
        "provider": proof.get("provider"),
        "model": proof.get("model"),
    }, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
