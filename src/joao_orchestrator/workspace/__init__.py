"""Workspace layer: read-only git helpers + isolated worktree manager.

Read-only inspection (branch, status, diff) is used by validation and evidence
generation.  The worktree manager creates per-task Git worktrees outside the
managed repository for provider dispatch.
"""
