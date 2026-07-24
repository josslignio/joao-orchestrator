#!/usr/bin/env python3
"""G-AUTH-IO real entrypoint (C8-B, `JOAO_C8_GATES_ROADMAP.md` LOT C8-B
items 2/2b).

For each C8-B gate this repo now wires, (a) statically scans that gate's own
test file for AST references to the real production entrypoint symbol(s) it
must exercise — feeding `bubble.gates.gate_auth_io` — and (b) actually RUNS
that test file's dedicated `test_dynamic_invocation_proof_*` test(s), which
assert via monkeypatch/spy that the real entrypoint was CALLED, not merely
imported/referenced. A dead reference alone would satisfy (a); C8-A's own
G-AUTH-IO contract already names that insufficiency (`JOAO_C8_GATE_
CONTRACTS.md`, "Renforcement dynamique"). Both (a) and (b) must pass —
static reference alone is never treated as sufficient proof here.
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for candidate in (REPO_ROOT, REPO_ROOT / "src"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from src.joao_orchestrator.bubble import gates  # noqa: E402

GATES = {
    "G-AUTH-IO-GLM-REVIEWER": {
        "required": ["GLMReviewer.review_stage"],
        "test_file": REPO_ROOT / "tests" / "test_c8_glm_reviewer.py",
    },
    "G-AUTH-IO-GPT-SECURE-IMPORT": {
        "required": ["GPTFormalEvidenceReviewer.review", "consume_import"],
        "test_file": REPO_ROOT / "tests" / "test_c8_secure_import.py",
    },
    "G-AUTH-IO-ORCHESTRATOR": {
        "required": ["run_c8b_mission"],
        "test_file": REPO_ROOT / "tests" / "test_c8_orchestration.py",
    },
    "G-AUTH-IO-DBL-AUDIT-WIRING": {
        "required": ["gate_dbl_audit"],
        "test_file": REPO_ROOT / "tests" / "test_c8_dbl_audit_wiring.py",
    },
}


def _referenced_symbols(test_file: Path) -> list[str]:
    if not test_file.is_file():
        return []
    tree = ast.parse(test_file.read_text(), filename=str(test_file))
    referenced = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            referenced.add(node.attr)
            if isinstance(node.value, ast.Name):
                referenced.add(f"{node.value.id}.{node.attr}")
        elif isinstance(node, ast.Name):
            referenced.add(node.id)
    return sorted(referenced)


def _resolvable(symbol: str) -> bool:
    from src.joao_orchestrator.bubble import runtime, secure_import, orchestrator, gates as gates_mod
    table = {
        "GLMReviewer.review_stage": hasattr(runtime.GLMReviewer, "review_stage"),
        "GPTFormalEvidenceReviewer.review": hasattr(runtime.GPTFormalEvidenceReviewer, "review"),
        "consume_import": hasattr(secure_import, "consume_import"),
        "run_c8b_mission": hasattr(orchestrator, "run_c8b_mission"),
        "gate_dbl_audit": hasattr(gates_mod, "gate_dbl_audit"),
    }
    return bool(table.get(symbol, False))


def _dynamic_proof_passed(test_file: Path) -> tuple[bool, str]:
    if not test_file.is_file():
        return False, "test file does not exist"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", str(test_file), "-k", "dynamic_invocation_proof", "-q"],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=180)
    output = proc.stdout + proc.stderr
    collected_none = "no tests ran" in output or "collected 0 items" in output
    return (proc.returncode == 0 and not collected_none), output[-2000:]


def main(argv: list[str] | None = None) -> int:
    overall_ok = True
    for gate_name, spec in GATES.items():
        required = spec["required"]
        referenced = _referenced_symbols(spec["test_file"])
        resolvable = [sym for sym in required if _resolvable(sym)]
        static = gates.gate_auth_io(gate_name=gate_name, required_entrypoint_symbols=required,
                                    referenced_symbols=referenced, resolvable_symbols=resolvable)
        dynamic_ok, dynamic_output = _dynamic_proof_passed(spec["test_file"])
        ok = static["ok"] and dynamic_ok
        overall_ok = overall_ok and ok
        print(f"{gate_name}: static={static['reason_code']} "
              f"dynamic_invocation_proof={'PASS' if dynamic_ok else 'FAIL'} -> {'OK' if ok else 'BLOCK'}")
        if not ok:
            print(f"  static reason: {static['reason']}")
            if not dynamic_ok:
                print(f"  dynamic proof output tail:\n{dynamic_output}")
    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
