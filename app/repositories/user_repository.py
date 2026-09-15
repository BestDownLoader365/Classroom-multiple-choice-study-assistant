"""Persistence and authentication for small local learner accounts."""

import sqlite3
from datetime import datetime, timezone
from uuid import uuid4

from werkzeug.security import check_password_hash, generate_password_hash

from app.models import User

from .database import Database


class UsernameAlreadyExistsError(ValueError):
    """Raised when a registration uses an existing username."""


class UserRepository:
    """Create and authenticate local username/password accounts."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self._dummy_password_hash = generate_password_hash("dummy-password-value")

    def create(self, username: str, password: str) -> User:
        user = User(
            id=str(uuid4()),
            username=username,
            password_hash=generate_password_hash(password),
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        try:
            with self.database.connect() as connection:
                connection.execute(
                    """
                    INSERT INTO users (id, username, password_hash, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (user.id, user.username, user.password_hash, user.created_at),
                )
        except sqlite3.IntegrityError as exc:
            raise UsernameAlreadyExistsError("该用户名已存在。") from exc
        return user

    def authenticate(self, username: str, password: str) -> User | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE username = ? COLLATE NOCASE",
                (username,),
            ).fetchone()
        password_hash = row["password_hash"] if row is not None else self._dummy_password_hash
        password_matches = check_password_hash(password_hash, password)
        if row is None or not password_matches:
            return None
        return self._to_model(row)

    def list_all(self) -> list[User]:
        """Return every registered account, ordered by username."""
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM users ORDER BY username COLLATE NOCASE",
            ).fetchall()
        return [self._to_model(row) for row in rows]

    def get_by_id(self, user_id: str) -> User | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        return self._to_model(row) if row else None

    @staticmethod
    def _to_model(row: sqlite3.Row) -> User:
        return User(
            id=row["id"],
            username=row["username"],
            password_hash=row["password_hash"],
            created_at=row["created_at"],
        )
