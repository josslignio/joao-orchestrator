"""Provider layer: typed role interfaces + local/offline adapters.

V1.4.1 ships Mock + ManualFile (offline, usable), an opt-in local Codex CLI
subscription adapter, a verified OpenCode local adapter (opencode-zai), and
Disabled* stubs for unavailable providers. No paid API fallback is available.

Role interface (base.ProviderAdapter) is provider-neutral; provider-specific
logic must NEVER be embedded in the task engine.
"""
