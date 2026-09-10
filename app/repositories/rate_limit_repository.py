"""Authentication rate-limit storage backed by the shared database.

Rate limiting is an authentication/security concern, not a connection
management concern, so it lives in its own repository rather than on the
Database connection manager. All identifiers are hashed before storage so
raw usernames and IP addresses are never persisted.
"""

import hashlib
import time

from .database import Database


class RateLimitRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def consume(
        self,
        scope: str,
        identifier: str,
        *,
        limit: int,
        window_seconds: int,
        now: int | None = None,
    ) -> bool:
        """Atomically consume one fixed-window allowance shared by all workers."""
        if limit < 1 or window_seconds < 1:
            raise ValueError("rate limit and window must be positive")
        timestamp = int(time.time()) if now is None else now
        identifier_hash = hashlib.sha256(identifier.encode("utf-8")).hexdigest()
        expires_at = timestamp + window_seconds
        with self.database.transaction() as connection:
            connection.execute(
                "DELETE FROM auth_rate_limits WHERE expires_at <= ?",
                (timestamp,),
            )
            connection.execute(
                """
                INSERT INTO auth_rate_limits (
                    scope, identifier_hash, window_started_at, expires_at,
                    attempt_count
                ) VALUES (?, ?, ?, ?, 1)
                ON CONFLICT(scope, identifier_hash) DO UPDATE SET
                    attempt_count = auth_rate_limits.attempt_count + 1
                """,
                (scope, identifier_hash, timestamp, expires_at),
            )
            row = connection.execute(
                """
                SELECT attempt_count FROM auth_rate_limits
                WHERE scope = ? AND identifier_hash = ?
                """,
                (scope, identifier_hash),
            ).fetchone()
        return int(row["attempt_count"]) <= limit

    def is_limited(
        self,
        scope: str,
        identifier: str,
        *,
        limit: int,
        now: int | None = None,
    ) -> bool:
        """Check an allowance without consuming it."""
        timestamp = int(time.time()) if now is None else now
        identifier_hash = hashlib.sha256(identifier.encode("utf-8")).hexdigest()
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT attempt_count FROM auth_rate_limits
                WHERE scope = ? AND identifier_hash = ? AND expires_at > ?
                """,
                (scope, identifier_hash, timestamp),
            ).fetchone()
        return row is not None and int(row["attempt_count"]) >= limit