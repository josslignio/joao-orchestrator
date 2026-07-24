#!/usr/bin/env python3
"""Fail-closed scan for raw provider material in supervisor/state evidence.

The previous marker scan could miss arbitrary provider prose and even allowed
M9_BLOCK_REASON.txt.  This version validates filenames and structured fields,
and treats state-root text files as compact-code/hash only.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

RAW_KEYS = {
    "prompt", "content", "final_content", "response", "provider_response",
    "raw_output", "raw_reason", "transcript", "conversation", "message",
}
SAFE_REASON_VALUES = {
    "", "direct provider completed", "auto route completed",
}
HASH_RE = re.compile(r"^(?:HASH|REDACTED):[0-9a-f]{16,64}$")
COMPACT_CODE_RE = re.compile(r"^[A-Z0-9_-]{1,96}$")
FORBIDDEN_NAME_RE = re.compile(
    r"(?:prompt|response|content|transcript|conversation|message|block_reason)",
    re.IGNORECASE,
)
STATE_PART_RE = re.compile(r"(?:^|[-_])(m9|m10|supervisor|state)(?:$|[-_])", re.IGNORECASE)


def _is_state_path(path: Path, root: Path) -> bool:
    rel_parts = path.relative_to(root).parts
    return any(
        part == "supervisor" or part.endswith("-state") or part.endswith("_state")
        for part in rel_parts
    )


def _validate_value(value: Any, location: str, violations: list[str]) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            lowered = str(key).lower()
            if lowered in RAW_KEYS or lowered.startswith("raw_"):
                if nested not in ("", None, [], {}):
                    violations.append(f"{location}.{key}: raw provider field")
                continue
            if lowered in {"reason", "error"} and isinstance(nested, str):
                if (
                    nested not in SAFE_REASON_VALUES
                    and not HASH_RE.fullmatch(nested)
                    and not COMPACT_CODE_RE.fullmatch(nested)
                ):
                    violations.append(f"{location}.{key}: unhashed provider text")
            _validate_value(nested, f"{location}.{key}", violations)
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _validate_value(nested, f"{location}[{index}]", violations)


def scan_file(path: Path, root: Path) -> list[str]:
    violations: list[str] = []
    state_path = _is_state_path(path, root)
    name = path.name.lower()

    if state_path and FORBIDDEN_NAME_RE.search(name):
        violations.append(f"{path}: forbidden raw-provider filename")

    suffix = path.suffix.lower()
    if suffix in {".json", ".jsonl"}:
        try:
            if suffix == ".json":
                _validate_value(json.loads(path.read_text(encoding="utf-8")), str(path), violations)
            else:
                for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                    if line.strip():
                        _validate_value(json.loads(line), f"{path}:{number}", violations)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            violations.append(f"{path}: invalid structured evidence: {exc}")
        return violations

    if state_path and suffix in {".txt", ".md", ".log"}:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return [f"{path}: unreadable state text: {exc}"]
        lines = [line for line in text.splitlines() if line]
        for number, line in enumerate(lines, 1):
            if not (HASH_RE.fullmatch(line) or COMPACT_CODE_RE.fullmatch(line)):
                violations.append(f"{path}:{number}: non-compact state text")
    return violations


def scan_root(root: Path) -> list[str]:
    root = Path(root).expanduser().resolve()
    violations: list[str] = []
    if not root.exists():
        return violations
    for path in sorted(root.rglob("*")):
        if path.is_file():
            violations.extend(scan_file(path, root))
    return violations


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", action="append", required=True)
    parser.add_argument("--output")
    args = parser.parse_args()
    violations: list[str] = []
    for root in args.root:
        violations.extend(scan_root(Path(root)))
    report = {
        "schema_version": 2,
        "roots_scanned": args.root,
        "raw_provider_text_files": len({item.split(":", 1)[0] for item in violations}),
        "violations": violations[:100],
        "pass": not violations,
    }
    rendered = json.dumps(report, sort_keys=True, indent=2)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
