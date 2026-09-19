"""Course resolution and signed form context for the web layer.

Three rules live here, and the rest of the web layer just uses them:

**The URL is authoritative.**  Every learning route carries ``<course_id>`` in
its path.  ``session["last_course_id"]`` is only ever consulted for an entry
point that has no explicit course (``/``), so two browser tabs on two courses
can never rewrite each other's request context.

**Forms are self-describing.**  Every learning form carries a server-signed
``form_context`` binding the course, the operation and the worker generation the
page was rendered from.  The transaction guard re-validates it inside the write
transaction, so a form rendered for one course/generation can never write into
another.

**Legacy URLs are navigation, not routing.**  An old ``GET`` without a course is
redirected to the explicit course URL (using the real owner for an exam ID).  An
old ``POST`` without a course is *never* guessed from the session; it is rejected
with a refresh-required response.
"""

import logging
from typing import Any

from flask import current_app, g, request, url_for
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

LOGGER = logging.getLogger(__name__)

#: Hidden form field carrying the signed context.
FORM_CONTEXT_FIELD = "form_context"

#: Query parameter carrying the context for a GET that must be bound too.
FORM_CONTEXT_QUERY = "fc"

#: How long a rendered form stays acceptable before it must be reloaded.
FORM_CONTEXT_MAX_AGE_SECONDS = 12 * 3600

_SALT = "mcq-course-form-context"

#: Endpoints that are not course-scoped.
COURSE_EXEMPT_ENDPOINTS = frozenset(
    {
        "web.index",
        "web.legacy_index",
        "web.login",
        "web.register",
        "web.logout",
        "web.courses",
        "web.health",
        "web.ready",
        "web.ready_course",
        "static",
    }
)


def build_form_serializer(secret_key: str) -> URLSafeTimedSerializer:
    """Create the signer used for form contexts (one per application)."""
    return URLSafeTimedSerializer(secret_key, salt=_SALT)


def issue_form_context(
    serializer: URLSafeTimedSerializer,
    *,
    course_id: str,
    operation: str,
    generation: int,
    **extra: Any,
) -> str:
    """Mint the signed context bound into one rendered form."""
    payload: dict[str, Any] = {
        "course_id": course_id,
        "operation": operation,
        "generation": generation,
    }
    payload.update(extra)
    return serializer.dumps(payload)


def read_form_context(
    serializer: URLSafeTimedSerializer, token: str | None
) -> dict[str, Any] | None:
    """Return the signed context, or ``None`` when missing or untrustworthy."""
    if not token:
        return None
    try:
        payload = serializer.loads(token, max_age=FORM_CONTEXT_MAX_AGE_SECONDS)
    except SignatureExpired:
        LOGGER.info("Rejected an expired form context.")
        return None
    except BadSignature:
        LOGGER.warning("Rejected a form context with an invalid signature.")
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def submitted_form_context(
    serializer: URLSafeTimedSerializer,
) -> dict[str, Any] | None:
    """Read the context a request carries, from the form or the query string."""
    token = request.form.get(FORM_CONTEXT_FIELD)
    if token is None and request.method in {"GET", "HEAD"}:
        token = request.args.get(FORM_CONTEXT_QUERY)
    return read_form_context(serializer, token)


def operation_for_endpoint(endpoint: str | None) -> str | None:
    """Return the logical operation name a route performs, if any."""
    if not endpoint:
        return None
    name = endpoint
    for prefix in ("web.", "web."):
        if name.startswith(prefix):
            name = name[len(prefix) :]
            break
    return name


def install_course_aware_url_for(app, scoped_endpoints) -> None:
    """Make template ``url_for`` calls carry the current course automatically.

    Every course-scoped endpoint requires ``course_id``.  Resolving it in one
    place means a template can never forget it (which would silently produce a
    cross-course link) while the route definitions stay explicit.  Python code
    keeps calling ``flask.url_for(..., course_id=...)`` directly.
    """
    original = url_for
    scoped = frozenset(scoped_endpoints)

    def course_url_for(endpoint: str, **values: Any):
        if endpoint in scoped and "course_id" not in values:
            course_id = _current_course_id()
            if course_id is not None:
                values["course_id"] = course_id
        elif endpoint == "web.courses":
            # Never leak a stale course default onto the selector itself.
            values.pop("course_id", None)
        return original(endpoint, **values)

    app.jinja_env.globals["url_for"] = course_url_for


def _current_course_id() -> str | None:
    """Return the course of the request being handled, if one was resolved."""
    course_id = getattr(g, "course_id", None)
    if isinstance(course_id, str) and course_id:
        return course_id
    services = current_app.extensions.get("mcq_services")
    if services is None:  # pragma: no cover - only before assembly finishes
        return None
    try:
        return services.course_registry.resolve_default_course_id()
    except Exception:  # noqa: BLE001 - navigation must never break rendering
        return None
