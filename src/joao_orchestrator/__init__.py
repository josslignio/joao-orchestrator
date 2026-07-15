"""joss-orchestrator — generic, multi-project orchestration engine.

V1.4.1: multi-project foundation. Provider-agnostic, profile-driven, SQLite
indexed. Standard library only. No network by default; all provider adapters
beyond Mock/ManualFile are disabled.

This package is intended to be extractable into a standalone repository later
(see docs/MULTIPROJECT_MIGRATION.md). It must NOT contain references to any
specific managed project or domain — no social-media scraping tools, no
financial-instrument names, no project-specific data files. All such rules
belong in each project's .agent/ profile.
"""

__version__ = "1.4.1"
