"""Production WSGI entry point behind one trusted Nginx reverse proxy."""

import os

from werkzeug.middleware.proxy_fix import ProxyFix

from app import create_app


if not os.environ.get("MCQ_SECRET_KEY"):
    raise RuntimeError("MCQ_SECRET_KEY must be set for production.")

# Declare the environment explicitly: it selects the conservative production
# defaults (a Secure session cookie, and a verified timestamped backup before any
# startup schema migration).  Without it the environment is *unknown*, which is
# even stricter — see app/config.py.
os.environ.setdefault("MCQ_ENV", "production")

app = create_app(
    {
        "DEBUG": False,
        "TESTING": False,
        # Public traffic must be HTTPS (normally terminated before this WSGI app).
        # Passed in code on purpose: an environment variable may not downgrade
        # this in production (app/config.py refuses that).
        "SESSION_COOKIE_SECURE": True,
        "ENABLE_CSRF": True,
    }
)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
