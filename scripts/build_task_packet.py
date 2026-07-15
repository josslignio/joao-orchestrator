#!/usr/bin/env python3
"""
JOÃO.AI Task Packet Builder
Builds bounded task packets from external state files
"""

import json
import sys
from pathlib import Path
from typing import Any, Dict, List


def load_run_state(state_dir: Path) -> Dict[str, Any]:
    """Load RUN_STATE.json if it exists"""
    run_state_path = state_dir / "RUN_STATE.json"
    if run_state_path.exists():
        with open(run_state_path) as f:
            return json.load(f)
    return {}


def load_final_state(state_dir: Path) -> Dict[str, Any]:
    """Load FINAL_STATE.json if it exists"""
    final_state_path = state_dir / "FINAL_STATE.json"
    if final_state_path.exists():
        with open(final_state_path) as f:
            return json.load(f)
    return {}


def build_task_packet(
    task_contract: str,
    current_commit: str,
    current_branch: str,
    relevant_files: List[str],
    max_files: int = 8
) -> Dict[str, Any]:
    """
    Build a compact task packet containing only essential information
    
    Args:
        task_contract: Description of the task to perform
        current_commit: Current git commit hash
        current_branch: Current git branch
        relevant_files: List of relevant file paths (max 8)
        max_files: Maximum number of files to include
    """
    if len(relevant_files) > max_files:
        print(f"Warning: {len(relevant_files)} files provided, limiting to {max_files}", file=sys.stderr)
        relevant_files = relevant_files[:max_files]
    
    return {
        "task_contract": task_contract,
        "git_state": {
            "commit": current_commit,
            "branch": current_branch
        },
        "relevant_files": relevant_files,
        "context_limits": {
            "max_files": max_files,
            "soft_token_limit": 25000,
            "hard_token_limit": 40000
        }
    }


def main():
    """CLI entry point"""
    if len(sys.argv) < 4:
        print("Usage: build_task_packet.py <task_contract> <commit> <branch> <files...>", file=sys.stderr)
        sys.exit(1)
    
    task_contract = sys.argv[1]
    commit = sys.argv[2]
    branch = sys.argv[3]
    files = sys.argv[4:]
    
    packet = build_task_packet(
        task_contract=task_contract,
        current_commit=commit,
        current_branch=branch,
        relevant_files=files
    )
    
    print(json.dumps(packet, indent=2))


if __name__ == "__main__":
    main()