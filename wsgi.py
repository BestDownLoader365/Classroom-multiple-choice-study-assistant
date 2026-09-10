"""Production WSGI entry point behind one trusted Nginx reverse proxy."""

import os

from werkzeug.middleware.proxy_fix import ProxyFix

from app import create_app


if not os.environ.get("MCQ_SECRET_KEY"):
    raise RuntimeError("MCQ_SECRET_KEY must be set for production.")

app = create_app(
    {
        "DEBUG": False,
        "TESTING": False,
        # Public traffic must be HTTPS (normally terminated before this WSGI app).
        "SESSION_COOKIE_SECURE": True,
        "ENABLE_CSRF": True,
    }
)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
