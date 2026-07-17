"""B-25 — the JOÃO evidence-pack convention, codified.

Rules (constitutional, non-negotiable):

1. A **certified pack** is an evidence directory sealed by a ``SHA256SUMS.txt``
   manifest. From that moment it is **immutable**: no file inside it may be
   added, changed, or removed.
2. Anything produced later that belongs with a certified pack goes in a
   **sibling supplement directory** named ``<pack>-supplement-vN`` (N = 1, 2, …),
   sealed by its **own** ``SHA256SUMS.txt``. The original pack stays
   byte-identical.
3. An artifact that was **rebuilt after the fact** (not captured during the
   original run) must carry the ``RECONSTRUCTED`` label in its file name so it
   can never be mistaken for primary evidence.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

MANIFEST_NAME = "SHA256SUMS.txt"
RECONSTRUCTED_LABEL = "RECONSTRUCTED"
SUPPLEMENT_RE = re.compile(r"^(?P<base>.+)-supplement-v(?P<version>[1-9]\d*)$")


def _sha256(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1048576), b""):
            value.update(block)
    return value.hexdigest()


def is_certified(pack: Path) -> bool:
    """A pack is certified (and therefore immutable) once its manifest exists."""
    return (Path(pack) / MANIFEST_NAME).is_file()


def is_supplement(name: str) -> bool:
    return bool(SUPPLEMENT_RE.match(name))


def supplement_base(name: str) -> str | None:
    match = SUPPLEMENT_RE.match(name)
    return match.group("base") if match else None


def is_reconstructed(name: str) -> bool:
    """Reconstructed artifacts must be labelled in the file name itself."""
    return RECONSTRUCTED_LABEL in Path(name).name


def write_manifest(pack: Path) -> Path:
    """Seal a pack: hash every file into SHA256SUMS.txt (sha256sum format)."""
    pack = Path(pack)
    lines = []
    for path in sorted(pack.rglob("*")):
        if path.is_file() and path.name != MANIFEST_NAME:
            lines.append(f"{_sha256(path)}  {path.relative_to(pack)}")
    manifest = pack / MANIFEST_NAME
    manifest.write_text("\n".join(lines) + "\n")
    return manifest


def verify_pack(pack: Path) -> list[str]:
    """Return every integrity violation of a certified pack (empty = intact)."""
    pack = Path(pack)
    manifest = pack / MANIFEST_NAME
    if not manifest.is_file():
        return [f"not a certified pack: {MANIFEST_NAME} is missing"]
    problems: list[str] = []
    recorded: dict[str, str] = {}
    for line in manifest.read_text().splitlines():
        if not line.strip():
            continue
        try:
            checksum, relative = line.split(None, 1)
        except ValueError:
            problems.append(f"malformed manifest line: {line!r}")
            continue
        recorded[relative.strip()] = checksum
    for relative, checksum in recorded.items():
        path = pack / relative
        if not path.is_file():
            problems.append(f"missing file: {relative}")
        elif _sha256(path) != checksum:
            problems.append(f"content changed: {relative}")
    for path in sorted(pack.rglob("*")):
        if path.is_file() and path.name != MANIFEST_NAME:
            relative = str(path.relative_to(pack))
            if relative not in recorded:
                problems.append(f"file added after certification: {relative}")
    return problems


def next_supplement_dir(pack: Path) -> Path:
    """Where a later addition must go: the next sibling <pack>-supplement-vN."""
    pack = Path(pack)
    versions = [0]
    for sibling in pack.parent.glob(pack.name + "-supplement-v*"):
        match = SUPPLEMENT_RE.match(sibling.name)
        if match and match.group("base") == pack.name:
            versions.append(int(match.group("version")))
    return pack.parent / f"{pack.name}-supplement-v{max(versions) + 1}"


def plan_addition(pack: Path) -> Path:
    """Refuse any write into a certified pack; give the supplement dir instead."""
    pack = Path(pack)
    if not is_certified(pack):
        return pack
    return next_supplement_dir(pack)
