from __future__ import annotations

import pathlib

import pytest

from joao_orchestrator.bubble.write_tier_policy import WriteTierDisabled
from joao_orchestrator.bubble.runtime import ClaudeCodeBuilder, GLMBuilder
from joao_orchestrator.providers.cascade_runtime import real_claude_runner, real_glm_runner
from joao_orchestrator.providers.codex_subscription import CodexSubscriptionProvider
from joao_orchestrator.providers.opencode_provider import OpenCodeProvider
from joao_orchestrator.runtime.convergence import _invoke_fixer


def _write_entrypoints():
    path = pathlib.Path("/tmp")
    opencode = OpenCodeProvider.__new__(OpenCodeProvider)
    codex = CodexSubscriptionProvider.__new__(CodexSubscriptionProvider)
    return {
        "GLMBuilder.build": lambda: GLMBuilder().build("m", path, path, [], False),
        "ClaudeCodeBuilder.build": lambda: ClaudeCodeBuilder().build("m", path, path, [], False),
        "cascade.real_glm_runner": lambda: real_glm_runner(path / "x")(path, path / "t", path / "o", [], "a"),
        "cascade.real_claude_runner": lambda: real_claude_runner()(path, path / "t", path / "o", [], "a"),
        "convergence._invoke_fixer": lambda: _invoke_fixer(None, None, None, None, None),
        "OpenCodeProvider.invoke": lambda: OpenCodeProvider.invoke(opencode, None),
        "CodexSubscriptionProvider.invoke": lambda: CodexSubscriptionProvider.invoke(codex, None),
    }


@pytest.mark.parametrize("name,entrypoint", list(_write_entrypoints().items()))
def test_all_write_builders_disabled(name, entrypoint):
    with pytest.raises(WriteTierDisabled, match="SEC-BOOT"):
        entrypoint()


def main() -> int:
    failed = False
    for name, entrypoint in _write_entrypoints().items():
        try:
            entrypoint()
        except WriteTierDisabled:
            print("OK", name)
        except Exception as exc:  # pragma: no cover - standalone diagnostic
            print("FAIL", name, "->", type(exc).__name__)
            failed = True
        else:
            print("FAIL", name, "did not raise")
            failed = True
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
