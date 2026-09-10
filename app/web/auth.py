"""Web-layer authentication, CSRF, and rate-limit helpers.

These helpers are deliberately explicit about their dependencies (the
Database for rate-limit storage, and the Flask request context) instead of
capturing them in a large closure, so each can be reasoned about and tested
in isolation.
"""

from collections.abc import Callable
from functools import wraps
from typing import Any

import secrets
from flask import current_app, g, redirect, request, session, url_for

from app.repositories import Database


def registration_error(username: str, password: str, confirmation: str) -> str | None:
    """Validate registration form input; return an error message or ``None``."""
    if not 2 <= len(username) <= 30:
        return "用户名长度需要为 2～30 个字符。"
    if any(character.isspace() for character in username):
        return "用户名中不能包含空格。"
    if not 6 <= len(password) <= 128:
        return "密码长度需要为 6～128 个字符。"
    if password != confirmation:
        return "两次输入的密码不一致。"
    return None


def require_account(view: Callable[..., Any]) -> Callable[..., Any]:
    """Redirect anonymous visitors to the login page."""

    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        if getattr(g, "learner_id", None):
            return view(*args, **kwargs)
        return redirect(url_for("web.login", next=request.path))

    return wrapped


def login_ip_allowed(database: Database, address: str | None) -> bool:
    return not database.is_rate_limited(
        "login-ip",
        address or "unknown",
        limit=int(current_app.config["AUTH_LOGIN_IP_LIMIT"]),
    )


def login_account_allowed(database: Database, username: str) -> bool:
    return not database.is_rate_limited(
        "login-account",
        username.casefold() or "<empty>",
        limit=int(current_app.config["AUTH_LOGIN_ACCOUNT_LIMIT"]),
    )


def record_login_failure(
    database: Database, username: str, address: str | None
) -> None:
    window = int(current_app.config["AUTH_LOGIN_WINDOW_SECONDS"])
    database.consume_rate_limit(
        "login-account",
        username.casefold() or "<empty>",
        limit=int(current_app.config["AUTH_LOGIN_ACCOUNT_LIMIT"]),
        window_seconds=window,
    )
    database.consume_rate_limit(
        "login-ip",
        address or "unknown",
        limit=int(current_app.config["AUTH_LOGIN_IP_LIMIT"]),
        window_seconds=window,
    )


def registration_allowed(database: Database, address: str | None) -> bool:
    return database.consume_rate_limit(
        "register-ip",
        address or "unknown",
        limit=int(current_app.config["AUTH_REGISTER_IP_LIMIT"]),
        window_seconds=int(current_app.config["AUTH_REGISTER_WINDOW_SECONDS"]),
    )


def csrf_token() -> str:
    if not current_app.config["ENABLE_CSRF"]:
        return ""
    token = session.get("csrf_token")
    if not isinstance(token, str) or len(token) < 32:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def validate_csrf() -> None:
    from flask import abort

    submitted = request.form.get("csrf_token", "")
    expected = session.get("csrf_token")
    if not isinstance(expected, str) or not submitted or not secrets.compare_digest(
        submitted, expected
    ):
        abort(400, description="请求验证失败，请刷新页面后重试。")