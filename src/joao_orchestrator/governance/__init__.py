"""M0 — governance: the single loader authority for the V4 documentary constitution (C-1)."""

from .spec_loader import LoadedSpecs, SpecIntegrityError, load_active_specs

__all__ = ["LoadedSpecs", "SpecIntegrityError", "load_active_specs"]
