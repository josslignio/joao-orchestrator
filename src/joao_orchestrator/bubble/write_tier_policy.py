"""SEC-BOOT central write-tier kill-switch. Containment = this function raises
UNCONDITIONALLY. Re-enabling write-tier later means replacing THIS file via a signed
step, not flipping a runtime flag or env var (no configuration can reactivate it)."""


class WriteTierDisabled(RuntimeError):
    pass


SEC_BOOT_DISABLED = "SEC_BOOT_DISABLED"


def assert_write_tier_enabled(actor: str, mode: str = "workspace-write") -> None:
    raise WriteTierDisabled(f"{SEC_BOOT_DISABLED}: builder write/exec disabled by SEC-BOOT (actor={actor}, mode={mode})")
