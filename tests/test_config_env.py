"""Deployment configuration: environment detection, strict booleans, cookies."""

from __future__ import annotations

import pytest

from app import create_app
from app.config import (
    COOKIE_SECURE_VARIABLE,
    ENVIRONMENT_VARIABLE,
    ConfigurationError,
    InvalidEnvironmentError,
    McqEnvironment,
    default_cookie_secure,
    parse_strict_bool,
    resolve_environment,
    resolve_session_cookie_secure,
)
from tests.conftest import course_bank, write_course

#: Every environment variable the factory reads for these decisions.
_MANAGED = (ENVIRONMENT_VARIABLE, COOKIE_SECURE_VARIABLE, "MCQ_SECRET_KEY")


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch):
    """No test result may depend on the developer machine's environment."""
    for name in _MANAGED:
        monkeypatch.delenv(name, raising=False)
    yield


def _app(tmp_path, **overrides):
    """A real app over one generated course, with nothing configured unless asked."""
    courses_dir = tmp_path / "courses"
    write_course(courses_dir, "course_a", course_bank())
    config = {
        "SECRET_KEY": "test-secret",
        "COURSES_DIR": courses_dir,
        "QUESTION_FILE": tmp_path / "absent" / "questions.json",
        "GLOSSARY_FILE": tmp_path / "absent" / "glossary.json",
        "DATABASE": tmp_path / "mcq.db",
    }
    config.update(overrides)
    return create_app(config)


# ------------------------------------------------------------- strict booleans


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1", True),
        ("0", False),
        ("true", True),
        ("TRUE", True),
        ("  True  ", True),
        ("false", False),
        ("False", False),
        ("yes", True),
        ("no", False),
        ("on", True),
        ("off", False),
    ],
)
def test_parse_strict_bool_accepts_the_documented_spellings(raw, expected):
    assert parse_strict_bool("X", raw, default=not expected) is expected


@pytest.mark.parametrize("raw", ["maybe", "2", "trueish", "yes please", "-"])
def test_parse_strict_bool_rejects_anything_ambiguous(raw):
    """``bool("false")`` is True, so guessing here would be a security bug."""
    with pytest.raises(ConfigurationError) as excinfo:
        parse_strict_bool("X", raw, default=True)
    assert "X" in str(excinfo.value) and raw in str(excinfo.value)


def test_parse_strict_bool_treats_blank_and_missing_as_not_configured():
    assert parse_strict_bool("X", None, default=True) is True
    assert parse_strict_bool("X", "   ", default=False) is False


# ---------------------------------------------------------------- environment


@pytest.mark.parametrize(
    "value,expected",
    [
        ("production", McqEnvironment.PRODUCTION),
        ("PRODUCTION", McqEnvironment.PRODUCTION),
        (" development ", McqEnvironment.DEVELOPMENT),
        ("testing", McqEnvironment.TESTING),
    ],
)
def test_resolve_environment_reads_mcq_env(value, expected):
    assert resolve_environment({ENVIRONMENT_VARIABLE: value}) is expected


def test_unset_environment_is_unknown_not_development():
    """Unknown must behave conservatively; it is not a synonym for development."""
    assert resolve_environment({}) is McqEnvironment.UNKNOWN
    assert resolve_environment({ENVIRONMENT_VARIABLE: "  "}) is McqEnvironment.UNKNOWN
    assert default_cookie_secure(McqEnvironment.UNKNOWN) is True


def test_unknown_environment_value_is_an_error():
    with pytest.raises(InvalidEnvironmentError) as excinfo:
        resolve_environment({ENVIRONMENT_VARIABLE: "staging"})
    message = str(excinfo.value)
    assert "staging" in message
    for allowed in ("production", "development", "testing"):
        assert allowed in message


def test_testing_flag_wins_over_an_exported_environment():
    assert (
        resolve_environment({ENVIRONMENT_VARIABLE: "production"}, testing=True)
        is McqEnvironment.TESTING
    )


# -------------------------------------------------------------------- cookies


def _csrf(client) -> str:
    """Return the CSRF token the login form rendered (the app enables CSRF)."""
    import re

    page = client.get("/login")
    match = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert match, "the login form must render a CSRF token"
    return match.group(1)


def _login(app, username: str, password: str = "secret1"):
    """Register ``username``, then log in with a *fresh* client.

    A fresh client matters: the register response may already carry a session, and
    an authenticated ``GET /login`` redirects instead of rendering the form, so the
    login POST would have no token to send.  Returns ``(client, login_response)``
    so the caller can inspect the cookie the login actually set.
    """
    registrar = app.test_client()
    registrar.post(
        "/register",
        data={
            "username": username,
            "password": password,
            "password_confirmation": password,
            "csrf_token": _csrf(registrar),
        },
    )
    client = app.test_client()
    response = client.post(
        "/login",
        data={"username": username, "password": password, "csrf_token": _csrf(client)},
    )
    return client, response


def test_development_default_allows_a_plain_http_session(tmp_path, monkeypatch):
    """``python run.py`` over HTTP must be able to log in and stay logged in."""
    monkeypatch.setenv(ENVIRONMENT_VARIABLE, "development")
    app = _app(tmp_path, DEBUG=True)
    assert app.config["SESSION_COOKIE_SECURE"] is False
    assert app.config["MCQ_ENVIRONMENT"] == "development"

    client, login = _login(app, "dev")
    assert login.status_code in (200, 302)
    # The cookie is not marked Secure, so a plain-HTTP browser sends it back.
    cookie = login.headers.get("Set-Cookie", "")
    assert cookie and "Secure" not in cookie
    # The session survives: a protected page renders instead of redirecting out.
    assert client.get("/courses").status_code == 200


def test_production_default_sets_a_secure_cookie(tmp_path, monkeypatch):
    monkeypatch.setenv(ENVIRONMENT_VARIABLE, "production")
    app = _app(tmp_path, TESTING=False)
    assert app.config["SESSION_COOKIE_SECURE"] is True
    assert app.config["MCQ_ENVIRONMENT"] == "production"

    _client, login = _login(app, "produser")
    assert "Secure" in login.headers.get("Set-Cookie", "")


def test_unknown_environment_keeps_the_safe_default(tmp_path):
    app = _app(tmp_path)
    assert app.config["MCQ_ENVIRONMENT"] == "unknown"
    assert app.config["SESSION_COOKIE_SECURE"] is True


@pytest.mark.parametrize("raw,expected", [("true", True), ("false", False)])
def test_environment_variable_overrides_the_environment_default(
    tmp_path, monkeypatch, raw, expected
):
    monkeypatch.setenv(ENVIRONMENT_VARIABLE, "development")
    monkeypatch.setenv(COOKIE_SECURE_VARIABLE, raw)
    app = _app(tmp_path)
    assert app.config["SESSION_COOKIE_SECURE"] is expected


def test_production_cannot_be_downgraded_by_an_environment_variable(
    tmp_path, monkeypatch
):
    """A stray ``false`` in a systemd env file must not silently disable it."""
    monkeypatch.setenv(ENVIRONMENT_VARIABLE, "production")
    monkeypatch.setenv(COOKIE_SECURE_VARIABLE, "false")
    with pytest.raises(ConfigurationError) as excinfo:
        _app(tmp_path, TESTING=False)
    message = str(excinfo.value)
    assert COOKIE_SECURE_VARIABLE in message and "HTTPS" in message


def test_production_allows_a_code_level_downgrade_with_a_warning(
    tmp_path, monkeypatch, caplog
):
    """The entry point may decide it in code; that is a reviewable decision."""
    monkeypatch.setenv(ENVIRONMENT_VARIABLE, "production")
    with caplog.at_level("WARNING"):
        app = _app(tmp_path, TESTING=False, SESSION_COOKIE_SECURE=False)
    assert app.config["SESSION_COOKIE_SECURE"] is False
    assert any("明文" in record.getMessage() for record in caplog.records)


def test_entry_point_value_beats_the_environment_variable(tmp_path, monkeypatch):
    monkeypatch.setenv(ENVIRONMENT_VARIABLE, "development")
    monkeypatch.setenv(COOKIE_SECURE_VARIABLE, "false")
    app = _app(tmp_path, SESSION_COOKIE_SECURE=True)
    assert app.config["SESSION_COOKIE_SECURE"] is True


def test_illegal_boolean_stops_the_startup(tmp_path, monkeypatch):
    monkeypatch.setenv(COOKIE_SECURE_VARIABLE, "maybe")
    with pytest.raises(ConfigurationError):
        _app(tmp_path)


def test_testing_is_stable_regardless_of_the_machine_environment(tmp_path, monkeypatch):
    """A TESTING app must not inherit a developer's exported cookie setting."""
    monkeypatch.setenv(COOKIE_SECURE_VARIABLE, "true")
    app = _app(tmp_path / "plain", TESTING=True)
    assert app.config["SESSION_COOKIE_SECURE"] is False
    # ... while an explicit request to exercise the Secure cookie still wins.
    other = _app(tmp_path / "explicit", TESTING=True, SESSION_COOKIE_SECURE=True)
    assert other.config["SESSION_COOKIE_SECURE"] is True


def test_resolver_precedence_is_documented_by_behaviour():
    """The three layers, in order: explicit, environment variable, environment."""
    assert (
        resolve_session_cookie_secure(
            environment=McqEnvironment.PRODUCTION,
            environ={COOKIE_SECURE_VARIABLE: "false"},
            explicit=True,
        )
        is True
    )
    assert (
        resolve_session_cookie_secure(
            environment=McqEnvironment.DEVELOPMENT,
            environ={COOKIE_SECURE_VARIABLE: "true"},
        )
        is True
    )
    assert (
        resolve_session_cookie_secure(environment=McqEnvironment.DEVELOPMENT) is False
    )
    assert resolve_session_cookie_secure(environment=McqEnvironment.UNKNOWN) is True
