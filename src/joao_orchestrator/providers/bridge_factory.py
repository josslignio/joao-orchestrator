"""Default SupervisorCore provider bridge.

Every provider is registered honestly.  Unavailable CLIs remain visible in the
health inventory but are never selected.  No paid API fallback is created.
"""
from __future__ import annotations

from typing import Iterable, Optional

from .chat_cli import ChatCLIProvider
from .codex_subscription import CodexSubscriptionProvider
from .opencode_provider import OpenCodeProvider, DEFAULT_MODEL
from .zai_coding_plan import ZAICodingPlanProvider
from ..supervisor.bridge import ProviderBridge


def build_default_bridge(*, environment_allowlist: Optional[Iterable[str]] = None,
                         timeout_seconds: int = 600) -> ProviderBridge:
    env = list(environment_allowlist or ["HOME", "PATH", "TMPDIR", "LANG", "LC_ALL"])
    bridge = ProviderBridge()

    # Read-only reasoning/review participants.
    bridge.register(
        ChatCLIProvider("claude"),
        family="anthropic",
        model="claude-cli:sonnet",
        priority=100,
        enabled_by_policy=True,
    )
    bridge.register(
        ChatCLIProvider("glm"),
        family="zai",
        model="zai-coding-plan/glm-4.5-air",
        priority=90,
        enabled_by_policy=True,
    )

    # Coding providers. Their existing adapters enforce write-tier/capability
    # gates at invocation time; registration does not bypass those gates.
    bridge.register(
        CodexSubscriptionProvider.default(env, timeout_seconds=timeout_seconds),
        family="openai",
        model="codex-subscription",
        priority=100,
        enabled_by_policy=True,
    )
    bridge.register(
        OpenCodeProvider.default(env, timeout_seconds=timeout_seconds),
        family="zai",
        model=DEFAULT_MODEL,
        priority=80,
        enabled_by_policy=True,
    )
    bridge.register(
        ZAICodingPlanProvider(run_timeout=timeout_seconds),
        family="zai",
        model="zai-coding-plan",
        priority=70,
        enabled_by_policy=True,
    )
    return bridge
