#!/usr/bin/env python3
"""G-NO-STALE-ENTRYPOINT real entrypoint (C8-B, `JOAO_C8_GATES_ROADMAP.md`
LOT C8-B item 2).

Scope: every `BuilderAdapter`/`ReviewerAdapter` subclass (the builder/
reviewer dispatch surface `JOAO_WORKER_INTEGRATION_SPEC.md` governs) plus
`ExecutionBackend`'s one real implementation, `LocalUntrustedBackend` — the
sole legitimate direct caller of the raw dispatch primitives
(`subprocess.run`/`subprocess.Popen`/`run_sandboxed`). This generalizes
`tests/test_a0_2_corrections.py`'s hand-maintained `_GUARDED_METHODS` AST
guard (previously scoped to two named classes) into a real scan across every
adapter class in `bubble/runtime.py`, feeding `bubble.gates.
gate_no_stale_entrypoint` instead of a hand-curated dict.

Bounded, honest scope (documented, not hidden): a single-hop AST scan of each
adapter method's own body — not a full transitive call-graph resolver, and
deliberately NOT scanning `TestRunnerAdapter` (test execution is a distinct
concern from builder/reviewer/promotion dispatch; `LocalTestRunner`'s
intentional `sandboxed=False` raw-baseline branch, used only by attack tests
per its own docstring, is out of this script's scope by design, not missed).

A callable this script finds reaching a dispatch primitive that is NOT on the
canonical allowlist below is exactly the "unlisted dispatch"/"duplicate
path"/"unprotected entrypoint" shape G-NO-STALE-ENTRYPOINT exists to catch.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for candidate in (REPO_ROOT, REPO_ROOT / "src"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from src.joao_orchestrator.bubble import gates  # noqa: E402

RUNTIME_PATH = REPO_ROOT / "src" / "joao_orchestrator" / "bubble" / "runtime.py"
EXECUTION_BACKEND_PATH = REPO_ROOT / "src" / "joao_orchestrator" / "bubble" / "execution_backend.py"

_ADAPTER_BASES = {"BuilderAdapter", "ReviewerAdapter"}
_DISPATCH_PRIMITIVES_NAME = {"run_sandboxed"}
_DISPATCH_PRIMITIVES_ATTR = {("subprocess", "run"), ("subprocess", "Popen")}

# Hand-curated, already-reviewed dispatch entrypoints (mirrors A0.2's
# `_GUARDED_METHODS` + the sink `ExecutionBackend.execute()` itself, which
# `tests/test_c8_gates.py`'s own C8-A fixtures already treat as canonical AND
# protected — the "already vetted, sanctioned dispatch point" convention this
# script reuses).
CANONICAL_DISPATCH_ENTRYPOINTS = (
    "LocalUntrustedBackend.execute",
    "GLMBuilder.build",
    "CodexCLIReviewer.review_stage",
    "GLMReviewer.review_stage",
)


def _base_names(node: ast.ClassDef) -> set[str]:
    names = set()
    for base in node.bases:
        if isinstance(base, ast.Name):
            names.add(base.id)
        elif isinstance(base, ast.Attribute):
            names.add(base.attr)
    return names


def _calls_dispatch_primitive_directly(node: ast.AST) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            func = sub.func
            if isinstance(func, ast.Name) and func.id in _DISPATCH_PRIMITIVES_NAME:
                return True
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                if (func.value.id, func.attr) in _DISPATCH_PRIMITIVES_ATTR:
                    return True
    return False


def _calls_execute_method(node: ast.AST) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) and sub.func.attr == "execute":
            return True
    return False


def inventory_bubble_dispatch() -> list[dict]:
    """Real AST scan (not a placeholder). Returns the discovered_callables
    list for `bubble.gates.gate_no_stale_entrypoint`, plus internal `_`-
    prefixed diagnostic fields for the human-readable report below."""
    discovered: list[dict] = []

    # (1) The one legitimate sink: LocalUntrustedBackend.execute — the only
    # class.method in this repo allowed to call run_sandboxed directly.
    sink_tree = ast.parse(EXECUTION_BACKEND_PATH.read_text(), filename=str(EXECUTION_BACKEND_PATH))
    for node in ast.walk(sink_tree):
        if isinstance(node, ast.ClassDef) and node.name == "LocalUntrustedBackend":
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "execute":
                    direct = _calls_dispatch_primitive_directly(item)
                    discovered.append({
                        "name": "LocalUntrustedBackend.execute", "effect": "dispatch",
                        "canonical": True, "protected": True, "predates_gates": False,
                        "_file": str(EXECUTION_BACKEND_PATH.relative_to(REPO_ROOT)), "_direct": direct,
                        "_note": "the designated sink — direct run_sandboxed() call here is by design",
                    })

    # (2) Every BuilderAdapter/ReviewerAdapter subclass's method reaching a
    # dispatch primitive (directly) or ExecutionBackend.execute() (indirectly).
    runtime_tree = ast.parse(RUNTIME_PATH.read_text(), filename=str(RUNTIME_PATH))
    for node in ast.walk(runtime_tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if not (_base_names(node) & _ADAPTER_BASES):
            continue
        for item in node.body:
            if not isinstance(item, ast.FunctionDef):
                continue
            direct = _calls_dispatch_primitive_directly(item)
            indirect = _calls_execute_method(item)
            if not direct and not indirect:
                continue  # this adapter method never reaches dispatch at all — not in scope
            name = f"{node.name}.{item.name}"
            canonical = name in CANONICAL_DISPATCH_ENTRYPOINTS
            discovered.append({
                "name": name, "effect": "dispatch", "canonical": canonical,
                # protected = reaches dispatch ONLY through ExecutionBackend.execute()
                # (indirect), never a raw primitive directly — the single-dispatch-point
                # invariant A0.2/§12.2 established.
                "protected": bool(indirect and not direct),
                "predates_gates": False,
                "_file": str(RUNTIME_PATH.relative_to(REPO_ROOT)), "_direct": direct, "_indirect": indirect,
            })
    return discovered


def main(argv: list[str] | None = None) -> int:
    discovered = inventory_bubble_dispatch()
    clean = [{k: v for k, v in item.items() if not k.startswith("_")} for item in discovered]
    result = gates.gate_no_stale_entrypoint(
        discovered_callables=clean, canonical_entrypoints=list(CANONICAL_DISPATCH_ENTRYPOINTS))
    print(f"G-NO-STALE-ENTRYPOINT: ok={result['ok']} {result['reason_code']} — {result['reason']}")
    for item in discovered:
        print(f"  {item['name']:38s} file={item['_file']:45s} canonical={str(item['canonical']):5s} "
              f"protected={str(item['protected']):5s} direct={str(item['_direct']):5s}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
