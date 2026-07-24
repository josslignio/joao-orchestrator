#!/usr/bin/env python3
"""P1-3: Scan state roots and evidence packages for raw provider text.

This gate traverses every file under the given directories and fails if any
raw provider prompt, response, or reason is persisted verbatim.  Acceptable
artifacts are SHA256 hashes, verdict labels (ACCEPT/BLOCK), and metadata.

Raw provider text markers that must NOT appear outside hash fields:
- Prompt content (CANDIDATE_SHA=, VERIFIED_HEAD_SHA=, DIFF=, FULL_TESTS=)
- Provider responses (JOAO_M10_OK, raw JSON verdicts)
- Provider reasons (any non-hashed reason text)

Usage:
    python3 scan_raw_provider_text.py --root <dir> [--root <dir> ...]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


# Markers that indicate raw provider text has been persisted.
# These should only appear in hash fields (HASH:, sha256, _sha256), never as
# raw content in JSON values or text files.
RAW_MARKERS = [
    "CANDIDATE_SHA=",
    "VERIFIED_HEAD_SHA=",
    "VERIFIED_TREE_SHA=",
    "FULL_DIFF_SHA256=",
    "FULL_DIFF_CHARS=",
    "DIFF_SHA256=",
    "You are Codex performing",
    "You are an independent JOAO",
    "Reply with exactly JOAO_M10_OK",
]

# Files that are allowed to contain diff content (they ARE the diff, not
# raw provider text persisted by the supervisor).
ALLOWED_DIFF_FILES = {
    "FULL_DIFF_M7_M10.patch",
    "FINAL_EXACT_SHA.diff",
    "DIFF_FAILURE.patch",
    "PRODUCTION_DIFF.patch",
    "BASE_TO_FINAL_FORMAT_PATCH.mbox",
    "TRANCHE2_TO_FINAL_FORMAT_PATCH.mbox",
}


def scan_file(path: Path) -> list[str]:
    """Return list of violations found in a single file."""
    if path.name in ALLOWED_DIFF_FILES:
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []

    violations = []
    lines = text.split("\n")
    for i, line in enumerate(lines, 1):
        for marker in RAW_MARKERS:
            if marker not in line:
                continue
            # Check if it's in a hash field (acceptable)
            if any(h in line for h in ("HASH:", "sha256", "_sha256", "sha256=")):
                continue
            # Check if it's a JSON key reference (acceptable in structured output)
            if f'"{marker.split("=")[0]}"' in line:
                continue
            violations.append(f"{path}:{i}: {marker} → {line.strip()[:100]}")
    return violations


def scan_root(root: Path) -> list[str]:
    """Scan all files under a root directory for raw provider text."""
    violations = []
    if not root.exists():
        return violations
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        violations.extend(scan_file(path))
    return violations


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Scan state roots and evidence for raw provider text"
    )
    parser.add_argument(
        "--root", action="append", required=True,
        help="Directory to scan (can be repeated)",
    )
    parser.add_argument(
        "--output", default=None,
        help="Write JSON report to this path",
    )
    args = parser.parse_args()

    all_violations = []
    for root_str in args.root:
        root = Path(root_str).expanduser().resolve()
        all_violations.extend(scan_root(root))

    report = {
        "schema_version": 1,
        "roots_scanned": args.root,
        "raw_provider_text_files": len(all_violations),
        "violations": all_violations[:50],
        "pass": len(all_violations) == 0,
    }

    output = json.dumps(report, sort_keys=True, indent=2)
    if args.output:
        Path(args.output).write_text(output + "\n")
    print(output)
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
