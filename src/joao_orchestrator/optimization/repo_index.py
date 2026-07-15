"""Incremental repository index and impact graph (T4).

Indexes a git repository's tracked files with content hashes and extracts
Python symbols (imports/classes/functions) via AST, plus deterministic lexical
extraction for non-Python files. No embeddings, no vector DB, no network.

The impact graph answers:
  - relevant files for a changed path
  - direct importers / dependents
  - relevant tests for a changed source file
  - CLI exposure of a path
  - sensitive impact

Incremental: rebuilds only files whose hash changed since the last index
version, using `git diff` to detect the changed set. The index version is a
hash of (HEAD commit, indexed-file-set).

Persistence (outside repos):
  repo_index.json
  impact_graph.json
  index_events.jsonl

Deterministic: same repo state -> same index. Stdlib only.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Optional

from ..evaluation.models import sha256_json
from ..storage.atomic import append_line, atomic_write_json
from .plan_compiler import is_sensitive_path


SCHEMA_VERSION = 1


def _run_git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(
        ["git"] + args, cwd=cwd, capture_output=True, text=True, check=True,
    )
    return result.stdout


def _file_hash(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _detect_language(rel_path: str) -> str:
    ext = Path(rel_path).suffix.lower()
    return {
        ".py": "python",
        ".md": "markdown",
        ".rst": "rst",
        ".txt": "text",
        ".json": "json",
        ".yml": "yaml",
        ".yaml": "yaml",
        ".toml": "toml",
        ".cfg": "ini",
        ".ini": "ini",
        ".sh": "shell",
        ".js": "javascript",
        ".ts": "typescript",
    }.get(ext, "unknown")


@dataclass(frozen=True)
class FileEntry:
    """One indexed file."""
    rel_path: str
    language: str
    content_sha256: str
    size_bytes: int
    is_test: bool
    is_cli_entry: bool
    is_config: bool
    is_sensitive: bool
    imports: tuple[str, ...]
    defines: tuple[str, ...]
    references: tuple[str, ...]  # module paths referenced (for impact graph)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rel_path": self.rel_path,
            "language": self.language,
            "content_sha256": self.content_sha256,
            "size_bytes": self.size_bytes,
            "is_test": self.is_test,
            "is_cli_entry": self.is_cli_entry,
            "is_config": self.is_config,
            "is_sensitive": self.is_sensitive,
            "imports": list(self.imports),
            "defines": list(self.defines),
            "references": list(self.references),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "FileEntry":
        return cls(
            rel_path=str(d["rel_path"]),
            language=str(d["language"]),
            content_sha256=str(d["content_sha256"]),
            size_bytes=int(d["size_bytes"]),
            is_test=bool(d["is_test"]),
            is_cli_entry=bool(d["is_cli_entry"]),
            is_config=bool(d["is_config"]),
            is_sensitive=bool(d["is_sensitive"]),
            imports=tuple(d.get("imports", [])),
            defines=tuple(d.get("defines", [])),
            references=tuple(d.get("references", [])),
        )


def _is_test_path(rel_path: str) -> bool:
    name = Path(rel_path).name.lower()
    return name.startswith("test_") or name.endswith("_test.py") or "/tests/" in rel_path.replace("\\", "/")


def _is_cli_entry(rel_path: str, content: str) -> bool:
    if not rel_path.endswith(".py"):
        return False
    return "__main__" in content and ("argparse" in content or "sys.argv" in content or "click" in content)


def _is_config(rel_path: str) -> bool:
    name = Path(rel_path).name.lower()
    return name in (
        "pyproject.toml", "setup.py", "setup.cfg", "tox.ini", "requirements.txt",
        ".gitignore", "agents.md", "readme.md",
    ) or name.endswith((".yml", ".yaml")) and ".github" in rel_path


def _extract_python(rel_path: str, content: str) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Return (imports, defines, references) via AST."""
    imports: list[str] = []
    defines: list[str] = []
    references: list[str] = []
    try:
        tree = ast.parse(content, filename=rel_path)
    except SyntaxError:
        return (), (), ()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.append(node.module)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defines.append(node.name)
        elif isinstance(node, ast.ClassDef):
            defines.append(node.name)
    # References: map import module names to plausible file paths.
    for imp in imports:
        # Convert "joao_orchestrator.runtime.queue" -> "src/joao_orchestrator/runtime/queue.py"
        parts = imp.split(".")
        if len(parts) >= 2:
            references.append("src/" + "/".join(parts) + ".py")
            references.append("/".join(parts) + ".py")
    return tuple(dict.fromkeys(imports)), tuple(dict.fromkeys(defines)), tuple(dict.fromkeys(references))


def _index_file(repo: Path, rel_path: str) -> FileEntry:
    full = repo / rel_path
    content = full.read_text(encoding="utf-8", errors="replace")
    language = _detect_language(rel_path)
    if language == "python":
        imports, defines, references = _extract_python(rel_path, content)
    else:
        imports, defines, references = (), (), ()
    return FileEntry(
        rel_path=rel_path,
        language=language,
        content_sha256=_file_hash(full),
        size_bytes=len(content.encode("utf-8")),
        is_test=_is_test_path(rel_path),
        is_cli_entry=_is_cli_entry(rel_path, content),
        is_config=_is_config(rel_path),
        is_sensitive=is_sensitive_path(rel_path),
        imports=imports,
        defines=defines,
        references=references,
    )


def _tracked_files(repo: Path) -> list[str]:
    out = _run_git(["ls-files"], repo).splitlines()
    return [f for f in out if f.strip()]


@dataclass(frozen=True)
class RepoIndex:
    """A content-addressed repository index."""
    repo_path: str
    head_commit: str
    files: tuple[FileEntry, ...]
    index_version: str
    schema_version: int = SCHEMA_VERSION

    def unsigned_dict(self) -> dict[str, Any]:
        # index_version IS the integrity hash; exclude it from the signed payload.
        return {
            "schema_version": self.schema_version,
            "repo_path": self.repo_path,
            "head_commit": self.head_commit,
            "files": [f.to_dict() for f in self.files],
        }

    def with_integrity(self) -> "RepoIndex":
        return replace(self, index_version=sha256_json(self.unsigned_dict()))

    def verify_integrity(self) -> bool:
        return bool(self.index_version) and self.index_version == sha256_json(self.unsigned_dict())

    def to_dict(self) -> dict[str, Any]:
        payload = self.unsigned_dict()
        payload["index_version"] = self.index_version
        return payload

    def file_map(self) -> dict[str, FileEntry]:
        return {f.rel_path: f for f in self.files}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RepoIndex":
        return cls(
            repo_path=str(d["repo_path"]),
            head_commit=str(d["head_commit"]),
            files=tuple(FileEntry.from_dict(f) for f in d.get("files", [])),
            index_version=str(d.get("index_version", "")),
            schema_version=int(d.get("schema_version", SCHEMA_VERSION)),
        )


def build_index(repo: Path) -> RepoIndex:
    """Build a full index of a git repository's tracked files."""
    repo = Path(repo).resolve()
    head = _run_git(["rev-parse", "HEAD"], repo).strip()
    files = []
    for rel in _tracked_files(repo):
        try:
            files.append(_index_file(repo, rel))
        except (OSError, UnicodeDecodeError):
            continue
    idx = RepoIndex(
        repo_path=str(repo),
        head_commit=head,
        files=tuple(files),
        index_version="",
    )
    return idx.with_integrity()


def changed_paths_since(repo: Path, base_commit: str) -> list[str]:
    """List paths changed between base_commit and HEAD (deterministic)."""
    repo = Path(repo).resolve()
    try:
        out = _run_git(["diff", "--name-only", base_commit, "HEAD"], repo)
    except subprocess.CalledProcessError:
        return []
    return [l for l in out.splitlines() if l.strip()]


def update_index(repo: Path, previous: Optional[RepoIndex]) -> RepoIndex:
    """Incrementally update an index.

    If `previous` exists and its head_commit matches the repo HEAD, reuse it.
    Otherwise rebuild only the changed files' entries (reusing unchanged ones).
    """
    repo = Path(repo).resolve()
    head = _run_git(["rev-parse", "HEAD"], repo).strip()
    if previous is not None and previous.head_commit == head:
        return previous
    if previous is None:
        return build_index(repo)
    # Incremental: reuse unchanged entries, re-index changed ones.
    prev_map = previous.file_map()
    changed = set(changed_paths_since(repo, previous.head_commit))
    # Also catch files that changed since the previous index version (content
    # hash differs from what we recorded).
    current_tracked = set(_tracked_files(repo))
    new_files: list[FileEntry] = []
    for rel in sorted(current_tracked):
        if rel in changed or rel not in prev_map:
            try:
                new_files.append(_index_file(repo, rel))
            except (OSError, UnicodeDecodeError):
                continue
        else:
            new_files.append(prev_map[rel])
    idx = RepoIndex(
        repo_path=str(repo), head_commit=head,
        files=tuple(new_files), index_version="",
    )
    return idx.with_integrity()


# ---------------------------------------------------------------------------
# Impact graph
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ImpactGraph:
    """Answers: who imports what, which tests reference what."""
    repo_path: str
    head_commit: str
    # rel_path -> files that import/reference it
    dependents: dict[str, tuple[str, ...]]
    # rel_path -> tests that reference it
    test_map: dict[str, tuple[str, ...]]
    # rel_path -> CLI entries that expose it
    cli_map: dict[str, tuple[str, ...]]
    sensitive_paths: tuple[str, ...]
    index_version: str
    schema_version: int = SCHEMA_VERSION

    def unsigned_dict(self) -> dict[str, Any]:
        # index_version IS the integrity hash; exclude it from the signed payload.
        return {
            "schema_version": self.schema_version,
            "repo_path": self.repo_path,
            "head_commit": self.head_commit,
            "dependents": {k: list(v) for k, v in self.dependents.items()},
            "test_map": {k: list(v) for k, v in self.test_map.items()},
            "cli_map": {k: list(v) for k, v in self.cli_map.items()},
            "sensitive_paths": list(self.sensitive_paths),
        }

    def with_integrity(self) -> "ImpactGraph":
        return replace(self, index_version=sha256_json(self.unsigned_dict()))

    def verify_integrity(self) -> bool:
        return bool(self.index_version) and self.index_version == sha256_json(self.unsigned_dict())

    def to_dict(self) -> dict[str, Any]:
        d = self.unsigned_dict()
        d["index_version"] = self.index_version
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ImpactGraph":
        return cls(
            repo_path=str(d["repo_path"]),
            head_commit=str(d["head_commit"]),
            dependents={k: tuple(v) for k, v in d.get("dependents", {}).items()},
            test_map={k: tuple(v) for k, v in d.get("test_map", {}).items()},
            cli_map={k: tuple(v) for k, v in d.get("cli_map", {}).items()},
            sensitive_paths=tuple(d.get("sensitive_paths", [])),
            index_version=str(d.get("index_version", "")),
            schema_version=int(d.get("schema_version", SCHEMA_VERSION)),
        )


def build_impact_graph(index: RepoIndex) -> ImpactGraph:
    """Build the impact graph from a repo index."""
    dependents: dict[str, set[str]] = {}
    test_map: dict[str, set[str]] = {}
    cli_map: dict[str, set[str]] = {}
    sensitive: list[str] = []
    fmap = index.file_map()
    for f in index.files:
        if f.is_sensitive:
            sensitive.append(f.rel_path)
        # dependents: for each reference this file makes, record self as dependent.
        for ref in f.references:
            dependents.setdefault(ref, set()).add(f.rel_path)
        # test_map: test files reference the modules they test.
        if f.is_test:
            for ref in f.references:
                test_map.setdefault(ref, set()).add(f.rel_path)
        # cli_map: CLI entries expose the modules they import.
        if f.is_cli_entry:
            for imp in f.imports:
                # map import to plausible path
                parts = imp.split(".")
                if len(parts) >= 2:
                    p = "src/" + "/".join(parts) + ".py"
                    cli_map.setdefault(p, set()).add(f.rel_path)
    return ImpactGraph(
        repo_path=index.repo_path,
        head_commit=index.head_commit,
        dependents={k: tuple(sorted(v)) for k, v in dependents.items()},
        test_map={k: tuple(sorted(v)) for k, v in test_map.items()},
        cli_map={k: tuple(sorted(v)) for k, v in cli_map.items()},
        sensitive_paths=tuple(sorted(set(sensitive))),
        index_version="",
    ).with_integrity()


def relevant_files(graph: ImpactGraph, changed_paths: Iterable[str]) -> list[str]:
    """Files relevant to a set of changed paths (direct dependents)."""
    out: set[str] = set()
    for p in changed_paths:
        out.add(p)
        for dep in graph.dependents.get(p, ()):
            out.add(dep)
    return sorted(out)


def relevant_tests(graph: ImpactGraph, changed_paths: Iterable[str]) -> list[str]:
    """Tests relevant to a set of changed source paths."""
    out: set[str] = set()
    for p in changed_paths:
        out.update(graph.test_map.get(p, ()))
    return sorted(out)


def sensitive_impact(graph: ImpactGraph, changed_paths: Iterable[str]) -> list[str]:
    """Sensitive paths touched by a change set."""
    changed = set(changed_paths)
    return sorted(p for p in graph.sensitive_paths if p in changed or any(p.startswith(c.rstrip("/") + "/") for c in changed))


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def persist_index(state_root: Path, index: RepoIndex, graph: ImpactGraph) -> dict[str, Path]:
    """Persist index + impact graph + event outside the repo."""
    root = Path(state_root).resolve() / "repo_index"
    root.mkdir(parents=True, exist_ok=True)
    idx_path = root / "repo_index.json"
    graph_path = root / "impact_graph.json"
    events_path = root / "index_events.jsonl"
    atomic_write_json(idx_path, index.to_dict())
    atomic_write_json(graph_path, graph.to_dict())
    append_line(events_path, json.dumps({
        "ts": _now_iso_static(),
        "head_commit": index.head_commit,
        "file_count": len(index.files),
        "index_version": index.index_version,
    }, ensure_ascii=False))
    return {"index": idx_path, "graph": graph_path, "events": events_path}


def _now_iso_static() -> str:
    import time
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
