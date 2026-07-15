"""joss_orchestrator compat shim — re-exports joao_orchestrator.registry (canonical).

This shim contains NO business logic. It re-exports the canonical JOÃO.AI
implementation from ``joao_orchestrator.registry`` and emits a structured deprecation event
on import. Remove once all consumers migrate to ``joao_orchestrator``.
"""

from __future__ import annotations

import warnings
from joao_orchestrator.registry import *  # noqa: F401,F403  (re-export canonical)

# also expose the module object itself for attribute access
import joao_orchestrator.registry as _canonical  # noqa: E402

_DEPRECATION_EVENT = {
    "event": "joss_orchestrator.deprecated_import",
    "canonical": "joao_orchestrator.registry",
    "advice": "import from joao_orchestrator instead",
}

warnings.warn(
    "joss_orchestrator is a compatibility shim; import from joao_orchestrator.",
    DeprecationWarning,
    stacklevel=2,
)

# propagate __all__ if the canonical module defines it
getattr(_canonical, "__all__", None)
