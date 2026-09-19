import re

from tests.test_web import make_app


def _csrf(client):
    with client.session_transaction() as browser_session:
        token = browser_session.get("csrf_token")
    if isinstance(token, str):
        return token
    for path in ("/login", "/courses"):
        response = client.get(path, base_url="https://localhost", follow_redirects=True)
        match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
        if match:
            return match.group(1)
    raise AssertionError("CSRF token was not rendered")


def test_production_forms_require_csrf_and_set_security_headers(tmp_path, valid_payload):
    app = make_app(
        tmp_path,
        valid_payload,
        TESTING=False,
        ENABLE_CSRF=True,
        SESSION_COOKIE_SECURE=True,
    )
    client = app.test_client()

    page = client.get("/login")
    assert page.status_code == 200
    assert page.headers["X-Content-Type-Options"] == "nosniff"
    assert page.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in page.headers["Content-Security-Policy"]

    rejected = client.post(
        "/register",
        data={
            "username": "csrf-user",
            "password": "secret1",
            "password_confirmation": "secret1",
        },
    )
    assert rejected.status_code == 400

    accepted = client.post(
        "/register",
        data={
            "username": "csrf-user",
            "password": "secret1",
            "password_confirmation": "secret1",
            "csrf_token": _csrf(client),
        },
    )
    assert accepted.status_code == 302
    assert "Secure" in accepted.headers["Set-Cookie"]
    assert "HttpOnly" in accepted.headers["Set-Cookie"]
    assert "SameSite=Lax" in accepted.headers["Set-Cookie"]

    health = client.get("/health", base_url="https://localhost")
    assert health.status_code == 200
    assert health.headers["X-Content-Type-Options"] == "nosniff"
    assert health.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in health.headers["Content-Security-Policy"]


def test_failed_authentication_is_rate_limited(tmp_path, valid_payload):
    app = make_app(
        tmp_path,
        valid_payload,
        TESTING=False,
        ENABLE_CSRF=True,
        SESSION_COOKIE_SECURE=True,
    )
    client = app.test_client()
    for _ in range(10):
        response = client.post(
            "/login",
            data={"username": "unknown", "password": "wrong", "csrf_token": _csrf(client)},
        )
        assert response.status_code == 200
    response = client.post(
        "/login",
        data={"username": "unknown", "password": "wrong", "csrf_token": _csrf(client)},
    )
    assert response.status_code == 429


def test_successful_login_cannot_reset_failed_login_budget(tmp_path, valid_payload):
    app = make_app(
        tmp_path,
        valid_payload,
        TESTING=False,
        ENABLE_CSRF=True,
        SESSION_COOKIE_SECURE=True,
        AUTH_LOGIN_ACCOUNT_LIMIT=3,
        AUTH_LOGIN_IP_LIMIT=100,
    )
    client = app.test_client()
    token = _csrf(client)
    assert client.post(
        "/register",
        base_url="https://localhost",
        data={
            "username": "known-user",
            "password": "secret1",
            "password_confirmation": "secret1",
            "csrf_token": token,
        },
    ).status_code == 302
    client.post("/logout", base_url="https://localhost", data={"csrf_token": _csrf(client)})

    for _ in range(3):
        assert client.post(
            "/login",
            base_url="https://localhost",
            data={"username": "known-user", "password": "wrong", "csrf_token": _csrf(client)},
        ).status_code == 200
    assert client.post(
        "/login",
        base_url="https://localhost",
        data={"username": "known-user", "password": "secret1", "csrf_token": _csrf(client)},
    ).status_code == 302
    client.post("/logout", base_url="https://localhost", data={"csrf_token": _csrf(client)})
    assert client.post(
        "/login",
        base_url="https://localhost",
        data={"username": "known-user", "password": "wrong", "csrf_token": _csrf(client)},
    ).status_code == 429


def test_registration_is_rate_limited(tmp_path, valid_payload):
    app = make_app(
        tmp_path,
        valid_payload,
        TESTING=False,
        ENABLE_CSRF=True,
        SESSION_COOKIE_SECURE=True,
        AUTH_REGISTER_IP_LIMIT=2,
        AUTH_REGISTER_WINDOW_SECONDS=3600,
    )
    client = app.test_client()
    for index in range(2):
        assert client.post(
            "/register",
            base_url="https://localhost",
            data={
                "username": f"new-user-{index}",
                "password": "secret1",
                "password_confirmation": "secret1",
                "csrf_token": _csrf(client),
            },
        ).status_code == 302
        client.post("/logout", base_url="https://localhost", data={"csrf_token": _csrf(client)})
    assert client.post(
        "/register",
        base_url="https://localhost",
        data={
            "username": "new-user-final",
            "password": "secret1",
            "password_confirmation": "secret1",
            "csrf_token": _csrf(client),
        },
    ).status_code == 429