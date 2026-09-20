"""Web-layer authentication, CSRF, and rate-limit helpers.

These helpers are deliberately explicit about their dependencies (the
RateLimitRepository for throttling storage, and the Flask request context)
instead of capturing them in a large closure, so each can be reasoned about
and tested in isolation.
"""

from collections.abc import Callable
from functools import wraps
from typing import Any

import hmac
import secrets
from flask import current_app, g, redirect, request, session, url_for

from app.repositories import RateLimitRepository


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


def login_ip_allowed(rate_limits: RateLimitRepository, address: str | None) -> bool:
    return not rate_limits.is_limited(
        "login-ip",
        address or "unknown",
        limit=int(current_app.config["AUTH_LOGIN_IP_LIMIT"]),
    )


def login_account_allowed(rate_limits: RateLimitRepository, username: str) -> bool:
    return not rate_limits.is_limited(
        "login-account",
        username.casefold() or "<empty>",
        limit=int(current_app.config["AUTH_LOGIN_ACCOUNT_LIMIT"]),
    )


def record_login_failure(
    rate_limits: RateLimitRepository, username: str, address: str | None
) -> None:
    window = int(current_app.config["AUTH_LOGIN_WINDOW_SECONDS"])
    rate_limits.consume(
        "login-account",
        username.casefold() or "<empty>",
        limit=int(current_app.config["AUTH_LOGIN_ACCOUNT_LIMIT"]),
        window_seconds=window,
    )
    rate_limits.consume(
        "login-ip",
        address or "unknown",
        limit=int(current_app.config["AUTH_LOGIN_IP_LIMIT"]),
        window_seconds=window,
    )


def registration_allowed(rate_limits: RateLimitRepository, address: str | None) -> bool:
    return rate_limits.consume(
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


def tokens_match(submitted: object, expected: object) -> bool:
    """Compare two form tokens in constant time, tolerating any input.

    ``secrets.compare_digest`` raises ``TypeError`` as soon as one of two
    ``str`` operands contains a non-ASCII character, and every form field is
    fully caller-controlled.  A crafted ``csrf_token`` or ``answer_token``
    therefore used to turn a rejection (400) into an unhandled server error
    (500), reachable even without signing in.  Comparing UTF-8 bytes keeps the
    constant-time property and makes every byte string comparable; the tokens
    this application mints are always URL-safe ASCII, so a non-ASCII submission
    can still never match one.
    """
    if not isinstance(submitted, str) or not isinstance(expected, str):
        return False
    if not submitted or not expected:
        return False
    return hmac.compare_digest(submitted.encode("utf-8"), expected.encode("utf-8"))


def validate_csrf() -> None:
    from flask import abort

    submitted = request.form.get("csrf_token", "")
    expected = session.get("csrf_token")
    if not tokens_match(submitted, expected):
        abort(400, description="请求验证失败，请刷新页面后重试。")