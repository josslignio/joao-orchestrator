"""joss_orchestrator compat shim — re-exports joao_orchestrator.v2.learning (canonical).

This shim contains NO business logic. It re-exports the canonical JOÃO.AI
implementation from ``joao_orchestrator.v2.learning`` and emits a structured deprecation event
on import. Remove once all consumers migrate to ``joao_orchestrator``.
"""

from __future__ import annotations

import warnings
from joao_orchestrator.v2.learning import *  # noqa: F401,F403  (re-export canonical)

# also expose the module object itself for attribute access
import joao_orchestrator.v2.learning as _canonical  # noqa: E402

_DEPRECATION_EVENT = {
    "event": "joss_orchestrator.deprecated_import",
    "canonical": "joao_orchestrator.v2.learning",
    "advice": "import from joao_orchestrator instead",
}

warnings.warn(
    "joss_orchestrator is a compatibility shim; import from joao_orchestrator.",
    DeprecationWarning,
    stacklevel=2,
)

# propagate __all__ if the canonical module defines it
getattr(_canonical, "__all__", None)
