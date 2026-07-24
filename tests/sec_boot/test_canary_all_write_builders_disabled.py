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


# ---------------------------------------------------------------------------
# SEC-BOOT micro-A proofs (Codex-placed here, real SEC-BOOT, no bypass).
# Via le VRAI builder GLMBuilder.build (l'ecriture du task-file suit le gate) :
#   - le blocage precede le callback builder (backend.execute jamais appele) ;
#   - aucune evidence candidate/review/approval/promotion emise (run_dir vide) ;
#   - isolation : une ecriture opted-in reussie n'autorise pas l'ecriture du
#     test non-opted-in suivant.
# Les deux tests sont INDEPENDANTS (chacun passe seul, dans n'importe quel ordre).
# La preuve d'isolation CAUSALE se fait en executant explicitement, dans l'ordre :
#   pytest tests/sec_boot/test_canary_all_write_builders_disabled.py::test_secboot_opt_in_reaches_builder_and_writes \
#          tests/sec_boot/test_canary_all_write_builders_disabled.py::test_secboot_block_precedes_callback_no_evidence_and_no_leak
#   -> test 1 opte-in (ecriture autorisee) -> teardown monkeypatch function-scoped
#      -> test 2 sous SEC-BOOT reel bloque et n'ecrit rien (pas de fuite).
# Seul le test opted-in demande allow_test_write_tier.
# ---------------------------------------------------------------------------
class _SpyBackend:
    """Backend d'execution espion : n'execute rien, enregistre les appels."""

    def __init__(self):
        self.calls = []

    def execute(self, argv, **kwargs):
        self.calls.append(argv)
        return {"ok": True, "returncode": 0, "stdout": "{}", "stderr": "", "enforcement": None}


def test_secboot_opt_in_reaches_builder_and_writes(allow_test_write_tier, tmp_path):
    """opted-in (independant) : avec la fixture explicite, GLMBuilder.build franchit
    SEC-BOOT, atteint le callback builder (backend.execute) ET ecrit le task-file."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    spy = _SpyBackend()
    result = GLMBuilder(backend=spy).build("mission", run_dir, run_dir, [], False)
    assert spy.calls, "opted-in build must reach the builder callback"
    assert (run_dir / "builder-task.md").exists(), "opted-in build must actually write"
    assert result["provider"] == "zai-coding-plan"


def test_secboot_block_precedes_callback_no_evidence_and_no_leak(tmp_path):
    """no bypass (independant) : SEC-BOOT bloque AVANT le callback builder, et aucune
    evidence candidate/review/approval/promotion n'est emise. Passe seul ET juste apres
    le test opted-in (preuve d'isolation via l'ordre d'invocation explicite ci-dessus)."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    spy = _SpyBackend()
    with pytest.raises(WriteTierDisabled, match="SEC-BOOT"):
        GLMBuilder(backend=spy).build("mission", run_dir, run_dir, [], False)
    assert spy.calls == [], "builder callback must NOT run under SEC-BOOT block"
    assert list(run_dir.rglob("*")) == [], "SEC-BOOT block must emit no evidence"


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
