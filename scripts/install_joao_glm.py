#!/usr/bin/env python3
"""Install the stable JOÃO GLM adapter into ~/.local/bin/joao-glm."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from pathlib import Path


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--destination', default='~/.local/bin/joao-glm')
    parser.add_argument('--evidence-root', default='~/.local/share/joao/capabilities/glm')
    args = parser.parse_args()

    source = Path(__file__).with_name('joao_glm_cli.py').resolve()
    destination = Path(args.destination).expanduser().resolve()
    evidence_root = Path(args.evidence_root).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    evidence_root.mkdir(parents=True, exist_ok=True)

    tmp = destination.with_name(destination.name + f'.tmp-{os.getpid()}')
    shutil.copy2(source, tmp)
    tmp.chmod(0o755)
    os.replace(tmp, destination)

    payload = {
        'schema_version': 1,
        'source': str(source),
        'destination': str(destination),
        'source_sha256': sha256(source),
        'installed_sha256': sha256(destination),
        'matches': sha256(source) == sha256(destination),
    }
    manifest = evidence_root / 'INSTALLATION.json'
    manifest.write_text(json.dumps(payload, sort_keys=True, separators=(',', ':')) + '\n', encoding='utf-8')
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload['matches'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
