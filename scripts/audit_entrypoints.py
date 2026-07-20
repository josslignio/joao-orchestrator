#!/usr/bin/env python3
"""G-NO-STALE-ENTRYPOINT real entrypoint — STUB (C8-A).

`JOAO_C8_GATES_ROADMAP.md` LOT C8-A scope item (4): this script is created
now as a stub; its real AST inventory logic (walking the repo for every
callable reaching `ExecutionBackend.execute()`/`RunRuntime.promote()`/
`.approve()`/an artefact read, cross-checked against a canonical-entrypoint
allowlist, feeding `bubble.gates.gate_no_stale_entrypoint`) is C8-B scope
(`JOAO_C8_GATES_ROADMAP.md` LOT C8-B item 2).

A stub MUST NOT fabricate a real audit result — it announces itself as not
yet implemented and exits 0 (a harmless no-op in CI), never a false PASS
claiming a real inventory ran and never a false BLOCK with no actual
detection behind it. Wiring this script's exit code as the pre-close/CI gate
(`JOAO_C8_GATE_CONTRACTS.md`: "son exit code gate la clôture du lot") is
itself C8-B scope.
"""
from __future__ import annotations

import sys

STUB_NOTICE = (
    "audit_entrypoints.py: STUB (C8-A) — real AST entrypoint inventory + "
    "bubble.gates.gate_no_stale_entrypoint wiring lands in C8-B "
    "(JOAO_C8_GATES_ROADMAP.md LOT C8-B). No audit was performed; this is "
    "not a PASS or a BLOCK, only a placeholder exit."
)


def main(argv: list[str] | None = None) -> int:
    print(STUB_NOTICE)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
