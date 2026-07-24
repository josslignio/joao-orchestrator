#!/usr/bin/env python3
"""Build a reproducible, exact-SHA global audit package.

The package contains the complete tracked source at HEAD, a Git bundle with
history/refs, raw gate outputs, hashes and a deterministic manifest. It never
labels a package PASS when a required command failed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXED_ZIP_TIME = (2026, 7, 24, 0, 0, 0)

MASTER_TESTS = [
    "tests/test_run_night_master.py",
    "tests/test_run_night_watchdog.py",
    "tests/test_m7_supervisor_core.py",
    "tests/test_m8_provider_bridge.py",
    "tests/test_m8_readonly_enforcement.py",
    "tests/test_m8_final_security_closure.py",
    "tests/test_m9_supervisor_redteam.py",
    "tests/test_m9_m10_strict_live_gates.py",
    "tests/sec_boot",
]
COMPAT_TESTS = [
    "tests/test_m3_memory_watchdog.py", "tests/test_m3_network_isolation.py",
    "tests/test_m3_timeout_cleanup.py", "tests/test_m3_token_cleanup.py",
    "tests/test_m4_lease_authority.py", "tests/test_m4_multiprocessing.py",
    "tests/test_m4_public_paths.py", "tests/test_m5_git_operations.py",
    "tests/test_m5_integrity.py", "tests/test_m5_public_paths.py",
    *MASTER_TESTS,
]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(command: list[str], *, cwd: Path, output: Path, env: dict[str, str] | None = None) -> int:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    proc = subprocess.run(command, cwd=cwd, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, env=merged)
    output.write_text("$ " + " ".join(command) + "\n" + proc.stdout +
                      f"\nEXIT_CODE={proc.returncode}\n", encoding="utf-8")
    return proc.returncode


def git_text(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def tracked_files() -> list[Path]:
    raw = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT)
    return [ROOT / item.decode("utf-8") for item in raw.split(b"\0") if item]


def write_source_manifest(destination: Path) -> None:
    lines = []
    for path in tracked_files():
        if not path.is_file():
            raise RuntimeError(f"tracked path is not a file: {path.relative_to(ROOT)}")
        lines.append(f"{sha256_file(path)}  {path.relative_to(ROOT)}")
    destination.write_text("\n".join(sorted(lines)) + "\n", encoding="utf-8")




def scan_tracked_secrets(destination: Path) -> int:
    sys.path.insert(0, str(ROOT / "src"))
    from joao_orchestrator.observability.secrets_engine import SecretsEngine

    engine = SecretsEngine()
    findings = []
    low_confidence_rules = {"zai_api_key_hex64", "generic_key_assignment", "bearer_standalone"}
    for path in tracked_files():
        data = path.read_bytes()
        if b"\0" in data:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        for finding in engine.scan(text):
            if finding["rule_id"] in low_confidence_rules:
                continue
            findings.append({
                "path": str(path.relative_to(ROOT)),
                "rule_id": finding["rule_id"],
                "provider": finding["provider"],
                "severity": finding["severity"],
            })
    destination.write_text(json.dumps({
        "pass": not findings,
        "finding_count": len(findings),
        "findings": findings,
        "excluded_low_confidence_rule_ids": sorted(low_confidence_rules),
    }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0 if not findings else 1


def deterministic_zip(source: Path, destination: Path) -> None:
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source.rglob("*")):
            if not path.is_file():
                continue
            info = zipfile.ZipInfo(str(path.relative_to(source)), FIXED_ZIP_TIME)
            info.external_attr = (0o755 if os.access(path, os.X_OK) else 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True,
                        help="Output ZIP path; must be outside the repository")
    parser.add_argument("--skip-full-tests", action="store_true",
                        help="Prevalidation only; resulting status is INCOMPLETE")
    args = parser.parse_args()
    output = args.output.expanduser().resolve()
    try:
        output.relative_to(ROOT.resolve())
    except ValueError:
        pass
    else:
        raise SystemExit("output must be outside the repository")

    if git_text("status", "--porcelain=v1"):
        raise SystemExit("worktree must be clean before building audit package")

    head = git_text("rev-parse", "HEAD")
    tree = git_text("rev-parse", "HEAD^{tree}")
    root_commit = git_text("rev-list", "--max-parents=0", "HEAD").splitlines()[0]

    with tempfile.TemporaryDirectory(prefix="joao-global-audit-") as tmp:
        stage = Path(tmp) / "FULL_GLOBAL_AUDIT"
        stage.mkdir()
        (stage / "HEAD_SHA.txt").write_text(head + "\n", encoding="utf-8")
        (stage / "HEAD_TREE.txt").write_text(tree + "\n", encoding="utf-8")
        (stage / "ROOT_COMMIT.txt").write_text(root_commit + "\n", encoding="utf-8")
        (stage / "GIT_STATUS.txt").write_text("", encoding="utf-8")
        (stage / "GENERATED_AT_UTC.txt").write_text(
            datetime.now(timezone.utc).isoformat() + "\n", encoding="utf-8")

        with (stage / "FULL_TRACKED_SOURCE.tar.gz").open("wb") as handle:
            subprocess.run(["git", "archive", "--format=tar.gz", "HEAD"],
                           cwd=ROOT, check=True, stdout=handle)
        subprocess.run(["git", "bundle", "create", str(stage / "FULL_REPOSITORY.bundle"),
                        "HEAD", "--branches", "--tags"], cwd=ROOT, check=True)
        subprocess.run(["git", "log", "--all", "--date=iso-strict", "--decorate=full",
                        "--pretty=fuller", "--stat"], cwd=ROOT, check=True,
                       stdout=(stage / "FULL_COMMIT_CHAIN.txt").open("wb"))
        subprocess.run(["git", "format-patch", "--stdout", "--root", "HEAD"],
                       cwd=ROOT, check=True,
                       stdout=(stage / "FULL_HISTORY.patch").open("wb"))
        write_source_manifest(stage / "SOURCE_MANIFEST.sha256")
        (stage / "TRACKED_FILES.txt").write_text(
            "\n".join(str(path.relative_to(ROOT)) for path in tracked_files()) + "\n",
            encoding="utf-8")

        pycache = Path(tmp) / "pycache"
        results: dict[str, int | str | bool] = {}
        results["compile"] = run(
            [sys.executable, "-m", "compileall", "-q", "src", "scripts", "tests", "memory", "project_profiles"],
            cwd=ROOT, output=stage / "GATE_COMPILE.txt",
            env={"PYTHONPYCACHEPREFIX": str(pycache)},
        )
        results["collect"] = run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", "tests"],
            cwd=ROOT, output=stage / "GATE_PYTEST_COLLECTION.txt",
        )
        results["master"] = run(
            [sys.executable, "-m", "pytest", "-q", *MASTER_TESTS],
            cwd=ROOT, output=stage / "GATE_RUNNIGHT_MASTER.txt",
        )
        results["compatibility"] = run(
            [sys.executable, "-m", "pytest", "-q", *COMPAT_TESTS],
            cwd=ROOT, output=stage / "GATE_M3_M10_COMPATIBILITY.txt",
        )
        if args.skip_full_tests:
            results["full_repo"] = "SKIPPED"
            (stage / "GATE_FULL_REPOSITORY.txt").write_text(
                "SKIPPED: --skip-full-tests was supplied\n", encoding="utf-8")
        else:
            results["full_repo"] = run(
                [sys.executable, "-m", "pytest", "-q", "tests"],
                cwd=ROOT, output=stage / "GATE_FULL_REPOSITORY.txt",
            )
        results["diff_check"] = run(
            ["git", "diff", "--check", "HEAD"], cwd=ROOT,
            output=stage / "GATE_DIFF_CHECK.txt",
        )
        results["secret_scan"] = scan_tracked_secrets(stage / "GATE_SECRET_SCAN.json")

        required_codes = [value for value in results.values() if isinstance(value, int)]
        passed = not args.skip_full_tests and all(code == 0 for code in required_codes)
        status = {
            "schema_version": 1,
            "head_sha": head,
            "head_tree": tree,
            "complete_tracked_source": True,
            "git_history_bundle": True,
            "full_tests_skipped": args.skip_full_tests,
            "gates": results,
            "verdict": "PASS" if passed else ("INCOMPLETE" if args.skip_full_tests else "BLOCK"),
        }
        (stage / "AUDIT_STATUS.json").write_text(
            json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        checksums = []
        for path in sorted(stage.iterdir()):
            if path.is_file() and path.name != "PACKAGE_MANIFEST.sha256":
                checksums.append(f"{sha256_file(path)}  {path.name}")
        (stage / "PACKAGE_MANIFEST.sha256").write_text(
            "\n".join(checksums) + "\n", encoding="utf-8")

        output.parent.mkdir(parents=True, exist_ok=True)
        deterministic_zip(stage, output)
        output.with_suffix(output.suffix + ".sha256").write_text(
            f"{sha256_file(output)}  {output.name}\n", encoding="utf-8")

    print(f"PACKAGE={output}")
    print(f"SHA256={sha256_file(output)}")
    print(f"VERDICT={'PASS' if passed else ('INCOMPLETE' if args.skip_full_tests else 'BLOCK')}")
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
