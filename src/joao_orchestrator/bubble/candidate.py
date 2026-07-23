"""Immutable candidate and baseline creation with isolated Git indexes."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

from ..integrity.records import CandidateIdentityV2, IntegrityKeyManager, sha256_hex


class CandidateError(RuntimeError):
    pass


def _git(
    argv: list[str], cwd: Path, check: bool = True, *, env: Mapping[str, str] | None = None,
    text: bool = True,
) -> subprocess.CompletedProcess:
    clean_env = dict(os.environ)
    clean_env.pop("GIT_INDEX_FILE", None)
    clean_env.setdefault("LC_ALL", "C")
    clean_env.setdefault("LANG", "C")
    if env:
        clean_env.update(env)
    return subprocess.run(
        ["git"] + argv, cwd=str(cwd), shell=False, capture_output=True,
        text=text, check=check, env=clean_env,
    )


def _git_text(argv: list[str], cwd: Path, *, env: Mapping[str, str] | None = None) -> str:
    return _git(argv, cwd, env=env).stdout.strip()


def _real_index_path(workspace: Path) -> Path:
    raw = _git_text(["rev-parse", "--git-path", "index"], workspace)
    path = Path(raw)
    if not path.is_absolute():
        path = workspace / path
    return path.resolve(strict=False)


def _snapshot_index(workspace: Path) -> tuple[Path, bool, bytes, int | None]:
    path = _real_index_path(workspace)
    if not path.exists():
        return path, False, b"", None
    return path, True, path.read_bytes(), stat.S_IMODE(path.stat().st_mode)


def _restore_and_raise_if_index_changed(
    snapshot: tuple[Path, bool, bytes, int | None], *, operation: str,
) -> None:
    path, existed, original, mode = snapshot
    current_exists = path.exists()
    current = path.read_bytes() if current_exists else b""
    changed = current_exists != existed or (existed and current != original)
    if not changed:
        return
    try:
        if existed:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_name(path.name + ".joao-restore")
            temp.write_bytes(original)
            if mode is not None:
                os.chmod(temp, mode)
            os.replace(temp, path)
        elif current_exists:
            path.unlink()
    finally:
        raise CandidateError(
            f"{operation}: real Git index changed unexpectedly; original bytes were restored"
        )


@contextmanager
def _temporary_index(workspace: Path, base_commit: str) -> Iterator[dict[str, str]]:
    snapshot = _snapshot_index(workspace)
    fd, name = tempfile.mkstemp(prefix="joao-index-", suffix=".tmp")
    os.close(fd)
    temp_index = Path(name)
    # Git requires a nonexistent or valid index; the empty mkstemp file is not
    # a valid index, so remove it before read-tree creates it.
    temp_index.unlink(missing_ok=True)
    env = {"GIT_INDEX_FILE": str(temp_index)}
    try:
        _git(["read-tree", base_commit], workspace, env=env)
        yield env
    finally:
        temp_index.unlink(missing_ok=True)
        _restore_and_raise_if_index_changed(snapshot, operation="temporary-index operation")


def _tree_from_worktree(workspace: Path, base_commit: str) -> str:
    with _temporary_index(workspace, base_commit) as env:
        _git(["add", "-A", "--"], workspace, env=env)
        return _git_text(["write-tree"], workspace, env=env)


def _canonical_diff(workspace: Path, parent_commit: str, candidate_commit: str) -> bytes:
    result = _git(
        ["diff", "--binary", "--full-index", "--no-ext-diff", "--no-color",
         parent_commit, candidate_commit, "--"],
        workspace, text=False,
    )
    return bytes(result.stdout)


def _changed_path_manifest(workspace: Path, parent_commit: str, candidate_commit: str) -> str:
    result = _git(
        ["diff", "--name-status", "-z", "--no-renames", parent_commit, candidate_commit, "--"],
        workspace, text=False,
    )
    # Hex is deterministic and preserves NUL-delimited bytes without encoding
    # ambiguity. The digest is over this exact canonical representation.
    return bytes(result.stdout).hex()


def _source_worktree_identity(workspace: Path) -> str:
    root = Path(_git_text(["rev-parse", "--show-toplevel"], workspace)).resolve()
    common = _git_text(["rev-parse", "--git-common-dir"], workspace)
    common_path = Path(common)
    if not common_path.is_absolute():
        common_path = (workspace / common_path).resolve()
    st = root.stat()
    raw = json.dumps(
        {"root": str(root), "git_common_dir": str(common_path),
         "device": int(st.st_dev), "inode": int(st.st_ino)},
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _resolve_key(run_dir: Path, hmac_key: bytes | None) -> bytes:
    if hmac_key is not None:
        return hmac_key
    return IntegrityKeyManager(Path(run_dir).parent / "integrity").get_key()


def build_candidate_identity(
    workspace: Path, *, run_id: str, attempt: int, base_commit: str,
    parent_commit: str, candidate_commit: str, candidate_tree: str,
    hmac_key: bytes,
) -> CandidateIdentityV2:
    actual_commit = _git_text(["rev-parse", candidate_commit + "^{commit}"], workspace)
    actual_parent = _git_text(["rev-parse", candidate_commit + "^"], workspace)
    actual_tree = _git_text(["rev-parse", candidate_commit + "^{tree}"], workspace)
    if actual_commit != candidate_commit or actual_parent != parent_commit or actual_tree != candidate_tree:
        raise CandidateError("controller Git identity recomputation mismatch")
    diff = _canonical_diff(workspace, parent_commit, candidate_commit)
    manifest = _changed_path_manifest(workspace, parent_commit, candidate_commit)
    identity = CandidateIdentityV2(
        run_id=run_id, attempt=attempt, base_commit=base_commit,
        parent_commit=parent_commit, candidate_commit=candidate_commit,
        candidate_tree=candidate_tree,
        canonical_diff_sha256=sha256_hex(diff),
        changed_path_manifest=manifest,
        manifest_sha256=sha256_hex(manifest),
        source_worktree_identity=_source_worktree_identity(workspace),
        creation_timestamp=time.time(),
    )
    identity.validate()
    identity.controller_signature = identity.compute_signature(hmac_key)
    return identity


def _chmod_read_only(root: Path) -> None:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name != ".git"]
        for name in filenames:
            path = Path(dirpath) / name
            try:
                mode = path.lstat().st_mode
                if not stat.S_ISLNK(mode):
                    path.chmod(mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
            except OSError:
                pass
        try:
            directory = Path(dirpath)
            directory.chmod(
                directory.stat().st_mode
                & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
                | stat.S_IXUSR
            )
        except OSError:
            pass


def _chmod_writable(root: Path) -> None:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name != ".git"]
        try:
            Path(dirpath).chmod(0o755)
        except OSError:
            pass
        for name in filenames:
            path = Path(dirpath) / name
            try:
                if not path.is_symlink():
                    path.chmod(0o644)
            except OSError:
                pass


def freeze_candidate(
    workspace: Path, run_dir: Path, run_id: str, attempt: int,
    *, base_commit: str | None = None, hmac_key: bytes | None = None,
) -> dict[str, Any]:
    """Freeze the worktree without ever touching the caller's real index."""
    workspace = Path(workspace)
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    parent = _git_text(["rev-parse", "HEAD"], workspace)
    base = base_commit or parent
    tree = _tree_from_worktree(workspace, parent)
    commit = _git_text(
        ["commit-tree", tree, "-p", parent, "-m",
         f"JOAO candidate {run_id} attempt {attempt}"], workspace,
    )
    ref = f"refs/joao/candidates/{run_id}-{attempt:03d}"
    _git(["update-ref", ref, commit], workspace)

    copy_dir = run_dir / f"candidate-readonly-{attempt:03d}"
    if copy_dir.exists():
        _chmod_writable(copy_dir)
        _git(["worktree", "remove", "--force", str(copy_dir)], workspace, check=False)
        shutil.rmtree(copy_dir, ignore_errors=True)
    result = _git(["worktree", "add", "--detach", str(copy_dir), commit], workspace, check=False)
    if result.returncode != 0:
        raise CandidateError(f"could not create read-only candidate copy: {result.stderr}")
    _chmod_read_only(copy_dir)

    key = _resolve_key(run_dir, hmac_key)
    identity = build_candidate_identity(
        workspace, run_id=run_id, attempt=attempt, base_commit=base,
        parent_commit=parent, candidate_commit=commit, candidate_tree=tree,
        hmac_key=key,
    )
    return {
        "run_id": run_id, "attempt": attempt, "candidate_ref": ref,
        "candidate_commit": commit, "candidate_tree": tree,
        "readonly_copy": str(copy_dir), "parent_commit": parent,
        "base_commit": base,
        "identity_v2": asdict_identity(identity),
        "identity_digest": identity.digest(),
        "identity_signature": identity.controller_signature,
    }


def asdict_identity(identity: CandidateIdentityV2) -> dict[str, Any]:
    # Avoid importing dataclasses.asdict at every caller and keep one canonical
    # serialization boundary.
    return json.loads(json.dumps(identity.__dict__, sort_keys=True))


def freeze_baseline(
    workspace: Path, run_id: str, *, run_dir: Path | None = None,
    hmac_key: bytes | None = None,
) -> dict[str, Any]:
    workspace = Path(workspace)
    true_head = _git_text(["rev-parse", "HEAD"], workspace)
    tree = _tree_from_worktree(workspace, true_head)
    commit = _git_text(
        ["commit-tree", tree, "-p", true_head, "-m", f"JOAO frozen baseline {run_id}"],
        workspace,
    )
    ref = f"refs/joao/baselines/{run_id}"
    _git(["update-ref", ref, commit], workspace)
    if _git(["cat-file", "-e", commit + "^{commit}"], workspace, check=False).returncode != 0:
        raise CandidateError(f"baseline freeze did not produce a resolvable commit ({commit})")
    record = {
        "run_id": run_id, "baseline_ref": ref, "baseline_commit": commit,
        "baseline_tree": tree, "true_head": true_head,
    }
    if run_dir is not None:
        key = _resolve_key(Path(run_dir), hmac_key)
        # A baseline is represented as a zero-diff identity rooted at true_head.
        identity = build_candidate_identity(
            workspace, run_id=run_id + ":baseline", attempt=1,
            base_commit=true_head, parent_commit=true_head,
            candidate_commit=commit, candidate_tree=tree, hmac_key=key,
        )
        record.update({
            "identity_v2": asdict_identity(identity),
            "identity_digest": identity.digest(),
            "identity_signature": identity.controller_signature,
        })
    return record


def recompute_candidate_tree(readonly_copy: Path) -> str:
    copy_dir = Path(readonly_copy)
    head = _git_text(["rev-parse", "HEAD"], copy_dir)
    return _tree_from_worktree(copy_dir, head)


def recompute_candidate_identity(
    candidate: Mapping[str, Any], workspace: Path, hmac_key: bytes,
) -> CandidateIdentityV2:
    stored = CandidateIdentityV2.from_mapping(candidate["identity_v2"])
    current = build_candidate_identity(
        Path(workspace), run_id=stored.run_id, attempt=stored.attempt,
        base_commit=stored.base_commit, parent_commit=candidate["parent_commit"],
        candidate_commit=candidate["candidate_commit"],
        candidate_tree=candidate["candidate_tree"], hmac_key=hmac_key,
    )
    # Creation time is evidence metadata, not a recomputable Git property.
    current.creation_timestamp = stored.creation_timestamp
    current.controller_signature = current.compute_signature(hmac_key)
    return current


def verify_candidate_identity(
    candidate: Mapping[str, Any], workspace: Path, hmac_key: bytes,
) -> CandidateIdentityV2:
    if not isinstance(candidate.get("identity_v2"), Mapping):
        raise CandidateError("candidate is missing CandidateIdentityV2")
    stored = CandidateIdentityV2.from_mapping(candidate["identity_v2"])
    stored.validate()
    signature = candidate.get("identity_signature") or stored.controller_signature
    if not signature or not stored.verify_signature(signature, hmac_key):
        raise CandidateError("candidate identity signature verification failed")
    if candidate.get("identity_digest") != stored.digest():
        raise CandidateError("candidate identity digest mismatch")
    recomputed = recompute_candidate_identity(candidate, workspace, hmac_key)
    comparable_stored = stored._unsigned_dict()
    comparable_current = recomputed._unsigned_dict()
    if comparable_stored != comparable_current:
        raise CandidateError("candidate Git identity changed since freeze")
    readonly = candidate.get("readonly_copy")
    if readonly and recompute_candidate_tree(Path(readonly)) != stored.candidate_tree:
        raise CandidateError("candidate read-only worktree mutated after freeze")
    return stored


def release_candidate(candidate: Mapping[str, Any], workspace: Path) -> None:
    copy_dir = Path(candidate["readonly_copy"])
    if copy_dir.exists():
        _chmod_writable(copy_dir)
        _git(["worktree", "remove", "--force", str(copy_dir)], Path(workspace), check=False)
        shutil.rmtree(copy_dir, ignore_errors=True)
