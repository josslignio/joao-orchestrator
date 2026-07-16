#!/usr/bin/env python3
"""joao — the canonical JOÃO.AI CLI.

This is the canonical entry point. The legacy aliases ``joss`` and ``joss_v2``
call into this implementation and emit deprecation output.
"""
from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]  # repo root (src/joao_orchestrator/cli/joao.py -> root)
sys.path.insert(0, str(ROOT / "src"))

from joao_orchestrator.v2.autonomy import classify_current_state  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="joao", description="JOÃO.AI canonical CLI")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("autonomy", help="print the current autonomy classification")
    sub.add_parser("version", help="print the product version + identity")
    ui = sub.add_parser("ui", help="start the local-only JOAO bubble")
    ui.add_argument("--state-root", default="~/.local/share/joao")
    ui.add_argument("--no-open", action="store_true", help="do not open the local browser automatically")

    args = parser.parse_args(argv)
    if args.cmd == "autonomy":
        c = classify_current_state()
        print(json.dumps(c.to_dict(), indent=2))
        return 0
    if args.cmd == "version":
        print("JOÃO.AI joao-orchestrator (canonical); technical_id=joao")
        return 0
    if args.cmd == "ui":
        from joao_orchestrator.bubble.api import LocalAPIServer
        from joao_orchestrator.bubble.runtime import ClaudeCLIReviewer, CodexBuilder, CodexCLIReviewer, GLMBuilder, RunRuntime
        glm = GLMBuilder()
        codex_builder = CodexBuilder()
        codex_reviewer = CodexCLIReviewer()
        claude_reviewer = ClaudeCLIReviewer()
        server = LocalAPIServer(RunRuntime(
            Path(args.state_root).expanduser(),
            builder=glm,
            builders={"glm": glm, "codex": codex_builder},
            reviewer=codex_reviewer,
            reviewers={"codex": codex_reviewer, "claude": claude_reviewer},
            allow_test_adapters=False,
        ))
        print(server.url)
        if not args.no_open:
            webbrowser.open(server.url, new=2)
        try: server.server.serve_forever()
        except KeyboardInterrupt: server.close()
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
