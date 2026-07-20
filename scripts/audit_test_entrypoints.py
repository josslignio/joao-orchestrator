#!/usr/bin/env python3
"""G-AUTH-IO real entrypoint — STUB (C8-A).

`JOAO_C8_GATES_ROADMAP.md` LOT C8-A scope item (4): this script is created
now as a stub; its real static suite audit (mapping each gate's test file to
the real production entrypoint symbols it must reference, feeding
`bubble.gates.gate_auth_io`) plus the dynamic invocation-proof reinforcement
(spy/monkeypatch asserting the entrypoint was actually CALLED, not just
referenced) are both C8-B scope (`JOAO_C8_GATES_ROADMAP.md` LOT C8-B items
2/2b, `JOAO_C8_GATE_CONTRACTS.md` G-AUTH-IO "Renforcement dynamique").

A stub MUST NOT fabricate a real audit result — it announces itself as not
yet implemented and exits 0 (a harmless no-op in CI), never a false PASS
claiming a real static scan ran and never a false BLOCK with no actual
detection behind it.
"""
from __future__ import annotations

import sys

STUB_NOTICE = (
    "audit_test_entrypoints.py: STUB (C8-A) — real static test-suite entrypoint "
    "audit + dynamic invocation proof + bubble.gates.gate_auth_io wiring land "
    "in C8-B (JOAO_C8_GATES_ROADMAP.md LOT C8-B). No audit was performed; this "
    "is not a PASS or a BLOCK, only a placeholder exit."
)


def main(argv: list[str] | None = None) -> int:
    print(STUB_NOTICE)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
