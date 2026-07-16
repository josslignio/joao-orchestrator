from __future__ import annotations

import json
from pathlib import Path

from joao_orchestrator.providers.subprocess_cli import CLIEngineConfig, SubprocessCLIEngine


def test_run_argv_preserves_explicit_argv_and_stdin(tmp_path: Path) -> None:
    executable = tmp_path / "echo_args.py"
    executable.write_text(
        "#!/usr/bin/env python3\nimport json,sys\nprint(json.dumps({'argv':sys.argv[1:],'stdin':sys.stdin.read()}))\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    engine = SubprocessCLIEngine(CLIEngineConfig(name="fixture", executable=str(executable), environment_allowlist=["PATH"]))
    result = engine.run_argv(tmp_path, ["run", "hello"], stdin_text="body")
    assert result.ok
    payload = json.loads(result.stdout)
    assert payload == {"argv": ["run", "hello"], "stdin": "body"}
