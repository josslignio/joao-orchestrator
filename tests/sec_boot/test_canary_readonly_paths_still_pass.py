from __future__ import annotations

from joao_orchestrator.bubble.runtime import GLMReviewer
from joao_orchestrator.bubble.write_tier_policy import WriteTierDisabled


def test_readonly_reviewer_is_not_write_gated():
    try:
        GLMReviewer()
    except WriteTierDisabled as exc:
        raise AssertionError("read-only reviewer was over-gated") from exc


def main() -> int:
    try:
        GLMReviewer()
    except WriteTierDisabled:
        print("FAIL reviewer over-gated")
        return 1
    except Exception as exc:  # construction may require local runtime config
        print("OK reviewer not write-gated:", type(exc).__name__)
        return 0
    print("OK reviewer constructs (no over-gating)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
