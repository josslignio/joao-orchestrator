"""C8-B: `scripts/audit_entrypoints.py` (G-NO-STALE-ENTRYPOINT) and
`scripts/audit_test_entrypoints.py` (G-AUTH-IO, with dynamic invocation
proof) — real logic, no second dispatch path (`JOAO_C8_GATES_ROADMAP.md`
LOT C8-B item 2/2b).
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import audit_entrypoints  # noqa: E402
import audit_test_entrypoints  # noqa: E402


def test_current_repo_inventory_is_clean():
    result = audit_entrypoints.main([])
    assert result == 0


def test_detection_logic_flags_a_direct_subprocess_call_in_an_adapter_subclass():
    src = """
class RogueBuilder(BuilderAdapter):
    def build(self, mission, workspace, run_dir, allowed, correction):
        import subprocess
        subprocess.run(["echo", "hi"])
        return {"ok": True}
"""
    tree = ast.parse(src)
    class_node = tree.body[0]
    assert audit_entrypoints._base_names(class_node) == {"BuilderAdapter"}
    method_node = class_node.body[0]
    assert audit_entrypoints._calls_dispatch_primitive_directly(method_node) is True


def test_detection_logic_ignores_a_method_with_no_dispatch_call():
    src = """
class HarmlessBuilder(BuilderAdapter):
    def build(self, mission, workspace, run_dir, allowed, correction):
        return {"ok": True}
"""
    tree = ast.parse(src)
    method_node = tree.body[0].body[0]
    assert audit_entrypoints._calls_dispatch_primitive_directly(method_node) is False
    assert audit_entrypoints._calls_execute_method(method_node) is False


def test_detection_logic_recognizes_indirect_execute_call_as_protected_pattern():
    src = """
class GoodBuilder(BuilderAdapter):
    def build(self, mission, workspace, run_dir, allowed, correction):
        return self.backend.execute(["x"])
"""
    tree = ast.parse(src)
    method_node = tree.body[0].body[0]
    assert audit_entrypoints._calls_dispatch_primitive_directly(method_node) is False
    assert audit_entrypoints._calls_execute_method(method_node) is True


def test_a_stray_unlisted_dispatch_entrypoint_is_caught_by_the_gate():
    from src.joao_orchestrator.bubble import gates
    discovered = [
        {"name": "LocalUntrustedBackend.execute", "effect": "dispatch", "canonical": True,
         "protected": True, "predates_gates": False},
        {"name": "RogueBuilder.build", "effect": "dispatch", "canonical": False,
         "protected": False, "predates_gates": False},
    ]
    result = gates.gate_no_stale_entrypoint(
        discovered_callables=discovered, canonical_entrypoints=["LocalUntrustedBackend.execute"])
    assert result["ok"] is False
    assert result["reason_code"] == "G_NO_STALE_DUPLICATE_PATH"


def test_referenced_symbols_extraction_matches_real_test_file():
    referenced = audit_test_entrypoints._referenced_symbols(
        REPO_ROOT / "tests" / "test_c8_glm_reviewer.py")
    assert "GLMReviewer" in referenced
    assert "review_stage" in referenced


def test_resolvable_symbols_resolve_against_real_c8b_modules():
    for symbol in ("GLMReviewer.review_stage", "GPTFormalEvidenceReviewer.review",
                   "consume_import", "run_c8b_mission", "gate_dbl_audit"):
        assert audit_test_entrypoints._resolvable(symbol) is True
    assert audit_test_entrypoints._resolvable("NoSuchSymbol.nope") is False


def test_full_dynamic_and_static_audit_passes_for_the_current_repo():
    result = audit_test_entrypoints.main([])
    assert result == 0


def test_audit_entrypoints_script_runs_as_a_real_subprocess():
    proc = subprocess.run([sys.executable, str(REPO_ROOT / "scripts" / "audit_entrypoints.py")],
                          cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0
    assert "G_NO_STALE_OK" in proc.stdout
