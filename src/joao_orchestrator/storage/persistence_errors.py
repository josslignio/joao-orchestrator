"""Typed persistence errors for SQLite operations.

All SQLite, file I/O, and inter-process lock failures raise these specific errors
to ensure fail-closed behavior and proper error handling.
"""
from __future__ import annotations


class PersistenceError(Exception):
    """Base class for all persistence-related errors."""
    pass


class SQLiteInitializationError(PersistenceError):
    """Raised when SQLite database cannot be initialized or schema migration fails."""
    pass


class SQLiteTransactionError(PersistenceError):
    """Raised when a SQLite transaction fails (BEGIN, COMMIT, etc.)."""
    pass


class SQLiteCorruptionError(PersistenceError):
    """Raised when SQLite database is corrupted or unreadable."""
    pass


class SQLiteLockTimeoutError(PersistenceError):
    """Raised when SQLite database is locked and timeout expires."""
    pass


class SQLiteConstraintError(PersistenceError):
    """Raised when a SQLite constraint is violated (unique, foreign key, etc.)."""
    pass


class LeaseAuthorityError(PersistenceError):
    """Raised when lease authority operations fail (claim, renew, release, recovery)."""
    pass


class StaleLeaseError(LeaseAuthorityError):
    """Raised when a stale owner attempts operations on a successor lease."""
    pass


class InvalidLeaseTokenError(LeaseAuthorityError):
    """Raised when lease token or generation does not match expected values."""
    pass


class MigrationError(PersistenceError):
    """Raised when legacy JSON migration fails."""
    pass


class InterProcessLockError(PersistenceError):
    """Raised when inter-process lock acquisition fails."""
    pass


class AtomicWriteError(PersistenceError):
    """Raised when atomic file write operations fail."""
    pass


class CheckpointCorruptionError(PersistenceError):
    """Raised when checkpoint file is corrupted or unsigned."""
    pass
