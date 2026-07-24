from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "scripts/joao_glm_cli.py"


def _fake_opencode(tmp_path: Path) -> Path:
    exe = tmp_path / "opencode"
    exe.write_text(
        """#!/usr/bin/env python3
import json, pathlib, sys
args=sys.argv[1:]
if args == ['--version']:
 print('1.17.20'); raise SystemExit(0)
if args == ['run','--help']:
 print('Usage: opencode run [message..] --model --agent --format --dir --auto --title'); raise SystemExit(0)
if args == ['auth','list']:
 print('zai-coding-plan'); raise SystemExit(0)
if args[:2] == ['models','zai-coding-plan']:
 print('zai-coding-plan/glm-4.5-air'); raise SystemExit(0)
if args and args[0] == 'run':
 work=pathlib.Path(args[args.index('--dir')+1])
 agent=args[args.index('--agent')+1]
 if agent == 'build':
  (work/'result.txt').write_text('JOAO_GLM_WORKSPACE_WRITE_OK\\n')
 print(json.dumps({'provider':'zai-coding-plan','model':'zai-coding-plan/glm-4.5-air','ok':True}))
 raise SystemExit(0)
print('bad args', args, file=sys.stderr); raise SystemExit(2)
""",
        encoding="utf-8",
    )
    exe.chmod(0o755)
    return exe


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["git", "init", "-q"], ["git", "config", "user.email", "x@example.invalid"], ["git", "config", "user.name", "x"]):
        subprocess.run(args, cwd=repo, check=True)
    (repo / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)
    return repo


def _env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    auth = home / ".local/share/opencode/auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text("{}\n", encoding="utf-8")
    env = dict(os.environ)
    env["HOME"] = str(home)
    return env


def test_read_only_probe_and_run(tmp_path: Path) -> None:
    fake = _fake_opencode(tmp_path)
    repo = _repo(tmp_path)
    task = tmp_path / "task.md"
    task.write_text("Read only.\n", encoding="utf-8")
    output = tmp_path / "out.jsonl"
    proc = subprocess.run(
        [sys.executable, str(ADAPTER), "--workspace", str(repo), "--task-file", str(task),
         "--output", str(output), "--mode", "read-only", "--opencode", str(fake)],
        env=_env(tmp_path), capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert output.is_file()
    assert subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True).stdout == ""
    evidence = json.loads(output.with_suffix(output.suffix + ".evidence.json").read_text())
    assert evidence["ok"] is True
    assert evidence["fallback_used"] is False


def test_workspace_write_allowed_path(tmp_path: Path) -> None:
    fake = _fake_opencode(tmp_path)
    repo = _repo(tmp_path)
    task = tmp_path / "task.md"
    task.write_text("Create result.txt.\n", encoding="utf-8")
    output = tmp_path / "out.jsonl"
    proc = subprocess.run(
        [sys.executable, str(ADAPTER), "--workspace", str(repo), "--task-file", str(task),
         "--output", str(output), "--mode", "workspace-write", "--opencode", str(fake),
         "--allowed-path", "result.txt"],
        env=_env(tmp_path), capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert (repo / "result.txt").read_text() == "JOAO_GLM_WORKSPACE_WRITE_OK\n"


def test_workspace_write_rejects_unauthorized_path(tmp_path: Path) -> None:
    fake = _fake_opencode(tmp_path)
    repo = _repo(tmp_path)
    task = tmp_path / "task.md"
    task.write_text("Create result.txt.\n", encoding="utf-8")
    output = tmp_path / "out.jsonl"
    proc = subprocess.run(
        [sys.executable, str(ADAPTER), "--workspace", str(repo), "--task-file", str(task),
         "--output", str(output), "--mode", "workspace-write", "--opencode", str(fake),
         "--allowed-path", "other.txt"],
        env=_env(tmp_path), capture_output=True, text=True,
    )
    assert proc.returncode != 0
    evidence = json.loads(output.with_suffix(output.suffix + ".evidence.json").read_text())
    assert evidence["unauthorized_paths"] == ["result.txt"]


def test_convergence_invokes_real_glm_adapter_path(tmp_path: Path, allow_test_write_tier) -> None:
    from types import SimpleNamespace
    from joao_orchestrator.runtime.convergence import ConvergenceConfig, _invoke_fixer

    repo = _repo(tmp_path)
    adapter = tmp_path / "joao-glm"
    adapter.write_text(
        """#!/usr/bin/env python3
import pathlib,sys
args=sys.argv[1:]
work=pathlib.Path(args[args.index('--workspace')+1])
out=pathlib.Path(args[args.index('--output')+1])
(work/'result.txt').write_text('fixed\\n')
out.write_text('{"ok":true}\\n')
""",
        encoding="utf-8",
    )
    adapter.chmod(0o755)
    profile = SimpleNamespace(allowed_write_paths=["result.txt"])
    config = ConvergenceConfig(fix_executable=None, glm_adapter=str(adapter), fix_timeout=30)
    stdout, returncode, stderr = _invoke_fixer(
        {"selected_provider": "opencode-zai"}, config, {"task_id": "t1"}, repo, profile
    )
    assert returncode == 0, stderr
    assert json.loads(stdout)["ok"] is True
    assert (repo / "result.txt").read_text() == "fixed\n"
