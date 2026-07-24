#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path


def materialize(template: Path, output: Path, replacements: dict[str, str]) -> None:
    data = json.loads(template.read_text(encoding="utf-8"))
    data["authorized_sha"] = replacements["core_sha"]
    data["repo_root"] = replacements["repo"]
    data["state_root"] = replacements["state"]
    data["tranche3_evidence_path"] = replacements["t3_evidence"]
    data["tranche3_closure_path"] = replacements["t3_closure"]
    data["runnight_evidence_path"] = replacements["rn_evidence"]
    data["runnight_closure_path"] = replacements["rn_closure"]
    for task in data["tasks"]:
        task["execution_root"] = replacements["repo"]
    output.write_text(json.dumps(data, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--core-sha", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--state-root", required=True)
    parser.add_argument("--tranche3-evidence", required=True)
    parser.add_argument("--tranche3-closure", required=True)
    parser.add_argument("--runnight-evidence", required=True)
    parser.add_argument("--runnight-closure", required=True)
    parser.add_argument("--output-dir", required=True)
    ns = parser.parse_args()

    if len(ns.core_sha) != 40:
        raise SystemExit("core SHA must be 40 characters")

    root = Path(__file__).resolve().parents[1]
    output_dir = Path(ns.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    replacements = {
        "core_sha": ns.core_sha,
        "repo": str(Path(ns.repo).expanduser().resolve()),
        "state": str(Path(ns.state_root).expanduser().resolve()),
        "t3_evidence": str(Path(ns.tranche3_evidence).expanduser().resolve()),
        "t3_closure": str(Path(ns.tranche3_closure).expanduser().resolve()),
        "rn_evidence": str(Path(ns.runnight_evidence).expanduser().resolve()),
        "rn_closure": str(Path(ns.runnight_closure).expanduser().resolve()),
    }
    materialize(
        root / "config/runnight/RN0_REHEARSAL.template.json",
        output_dir / "RN0_REHEARSAL.json",
        replacements,
    )
    materialize(
        root / "config/runnight/RN1_TRANCHE4_MASTER.template.json",
        output_dir / "RN1_TRANCHE4_MASTER.json",
        replacements,
    )
    print(output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
