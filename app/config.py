"""Deployment configuration resolution: environment, booleans, safe defaults.

The application factory reads *deployment* settings once, at assembly time, and
fails fast on anything it cannot interpret.  This module owns that resolution so
the rules live in one testable place instead of inside ``create_app``:

``MCQ_ENV``
    Declares which kind of deployment this process is: ``production``,
    ``development`` or ``testing``.  Anything else is an error.  Leaving it unset
    is allowed and means **unknown**, which is resolved the conservative way (see
    :func:`resolve_session_cookie_secure` and
    :func:`resolve_startup_migration_policy`): a process that cannot say what it
    is must not be assumed to be a developer's laptop.

``MCQ_SESSION_COOKIE_SECURE``
    Strict boolean override for the session cookie's ``Secure`` flag.  Only
    ``1/0``, ``true/false``, ``yes/no`` and ``on/off`` (case-insensitive) are
    accepted; anything else raises :class:`ConfigurationError` instead of being
    coerced, because Python's ``bool("false")`` is ``True`` and a silently
    mis-read security switch is worse than a failed start.

Resolution order for the cookie, highest priority first:

1. the value the entry point passes *in code* (``wsgi.create_app({...})``, a
   test's ``make_app(TESTING=..., SESSION_COOKIE_SECURE=...)``);
2. the ``MCQ_SESSION_COOKIE_SECURE`` environment variable;
3. the environment default: only ``development`` defaults to a non-``Secure``
   cookie (a plain-HTTP ``python run.py`` session must be able to log in and stay
   logged in); ``production``, ``testing`` and *unknown* keep the safe value.

Production extra guard: an environment variable may turn the cookie *on* in
production, but not *off* — ``Secure=False`` there must be a code-level decision
(the entry point passing ``False`` explicitly), never an accidentally exported
variable.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from enum import Enum


class ConfigurationError(ValueError):
    """Raised when a deployment setting cannot be interpreted safely."""


class InvalidEnvironmentError(ConfigurationError):
    """Raised when ``MCQ_ENV`` names something this build does not know."""


#: Which kind of deployment this process is.
ENVIRONMENT_VARIABLE = "MCQ_ENV"

#: Strict boolean override for the session cookie's ``Secure`` flag.
COOKIE_SECURE_VARIABLE = "MCQ_SESSION_COOKIE_SECURE"

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


class McqEnvironment(str, Enum):
    """The deployment kinds this build understands.

    ``UNKNOWN`` is not a configured value: it is what an unset ``MCQ_ENV``
    resolves to, and it deliberately behaves like the safest of the real
    environments rather than like development.
    """

    PRODUCTION = "production"
    DEVELOPMENT = "development"
    TESTING = "testing"
    UNKNOWN = "unknown"


def parse_strict_bool(variable: str, raw: str | None, *, default: bool) -> bool:
    """Parse a boolean environment variable, refusing anything ambiguous.

    ``None``/blank means "not configured" and yields ``default``; every other
    value must be one of the documented spellings, otherwise the caller gets a
    :class:`ConfigurationError` naming the variable, the offending value and the
    accepted set.
    """
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise ConfigurationError(
        f'{variable} 的值无法解析为布尔值："{raw}"。'
        "支持 1/0、true/false、yes/no、on/off（大小写不敏感）。"
    )


def resolve_environment(
    environ: Mapping[str, str] | None = None, *, testing: bool = False
) -> McqEnvironment:
    """Return the declared environment, or :attr:`McqEnvironment.UNKNOWN`.

    The entry point's own ``TESTING`` flag wins: a test process is testing no
    matter what the machine exports.  Otherwise ``MCQ_ENV`` decides, and an
    unset variable means *unknown* rather than "development".
    """
    if testing:
        return McqEnvironment.TESTING
    raw = (environ or os.environ).get(ENVIRONMENT_VARIABLE)
    if raw is None or not raw.strip():
        return McqEnvironment.UNKNOWN
    normalized = raw.strip().lower()
    try:
        return McqEnvironment(normalized)
    except ValueError as exc:
        allowed = ", ".join(
            environment.value
            for environment in McqEnvironment
            if environment is not McqEnvironment.UNKNOWN
        )
        raise InvalidEnvironmentError(
            f'{ENVIRONMENT_VARIABLE} 的值无法识别："{raw}"。可选值：{allowed}。'
        ) from exc


def default_cookie_secure(environment: McqEnvironment) -> bool:
    """Return the environment default without applying any override.

    Exposed for documentation and tests: only ``development`` (a plain-HTTP
    ``python run.py`` session that must be able to log in and stay logged in)
    defaults to a non-``Secure`` cookie; *unknown* keeps the safe value.
    """
    return environment is not McqEnvironment.DEVELOPMENT


def resolve_session_cookie_secure(
    *,
    environment: McqEnvironment,
    environ: Mapping[str, str] | None = None,
    explicit: bool | None = None,
    testing: bool = False,
) -> bool:
    """Decide the session cookie's ``Secure`` flag: see the module docstring.

    ``explicit`` is the value the entry point passed in code, or ``None`` when it
    passed nothing.  It is *not* read back from ``app.config``: Flask installs its
    own ``SESSION_COOKIE_SECURE = False`` default, so a lookup there would be
    indistinguishable from a deliberate choice.
    """
    if explicit is not None:
        return bool(explicit)
    if testing:
        # Tests must not depend on whatever the developer's shell exports, and a
        # test client speaks plain HTTP.  A test that *does* want to exercise the
        # Secure cookie passes the key explicitly, which is handled above.
        return False
    value = parse_strict_bool(
        COOKIE_SECURE_VARIABLE,
        (environ or os.environ).get(COOKIE_SECURE_VARIABLE),
        default=default_cookie_secure(environment),
    )
    if environment is McqEnvironment.PRODUCTION and not value:
        raise ConfigurationError(
            f"{ENVIRONMENT_VARIABLE}={environment.value} 下不允许通过 "
            f"{COOKIE_SECURE_VARIABLE} 关闭 Secure Session Cookie："
            "生产环境的登录凭据只能在 HTTPS 上传送。"
            "若确实要关闭，必须在入口代码里显式传入 "
            "SESSION_COOKIE_SECURE=False（并承担明文传输的风险）。"
        )
    return value
