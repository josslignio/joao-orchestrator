from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
DANGEROUS = [
    "find *", "grep *", "sed *", "cat *", "head *", "tail *", "wc *",
    "git diff*", "git show*", "python -m pytest*", "python3 -m pytest*", "pytest*",
]


def _violations() -> tuple[list[str], bool]:
    source = (ROOT / "src/joao_orchestrator/providers/opencode_provider.py").read_text(encoding="utf-8")
    still_allowed = [
        command for command in DANGEROUS
        if re.search(r'"%s":\s*"allow"' % re.escape(command), source)
    ]
    edit_allowed = bool(re.search(r'"edit":\s*"allow"', source))
    return still_allowed, edit_allowed


def test_generic_commands_and_edit_are_denied():
    still_allowed, edit_allowed = _violations()
    assert not still_allowed
    assert not edit_allowed


def main() -> int:
    still_allowed, edit_allowed = _violations()
    if still_allowed or edit_allowed:
        print("FAIL still allowed:", still_allowed, "edit:", edit_allowed)
        return 1
    print("OK generic commands + edit denied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
