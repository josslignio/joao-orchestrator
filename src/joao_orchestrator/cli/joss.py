#!/usr/bin/env python3
"""joss — legacy alias; calls the canonical joao CLI + emits deprecation."""
from __future__ import annotations

import sys
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

warnings.warn("'joss' is a deprecated alias; use 'joao'.", DeprecationWarning, stacklevel=2)
print("DEPRECATED: 'joss' -> 'joao' (emitting deprecation)", file=sys.stderr)

from joao_orchestrator.cli.joao import main  # noqa: E402

raise SystemExit(main())
