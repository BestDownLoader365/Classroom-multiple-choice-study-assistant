"""Flask application factory for the multi-course MCQ practice tool.

The factory does four things and nothing else:

1. read deployment configuration;
2. create/migrate the shared SQLite database (course namespace migration is a
   separate, transactional step inside ``Database.initialize``);
3. build the read-only :class:`~app.course_runtime.CourseRegistry` by loading
   and reconciling every *enabled* course once;
4. register the web blueprint and the ``/health``, ``/ready`` endpoints.

Nothing here reloads configuration while serving, writes a content file, or
keeps a process-wide "current course": a request's course always comes from its
URL.  Switching courses at runtime is plain navigation.

Failure isolation is per course.  A broken course becomes ``unavailable`` and
keeps its historical learner state untouched, while the other courses keep
serving.  Only *global* ambiguities (a duplicate ``course_id``, an unreadable
manifest) abort assembly.
"""

import os
import secrets
from datetime import timedelta
from pathlib import Path
from typing import Any

from flask import Flask, request

from app.course_runtime import (
    AppServices,
    CourseRegistry,
    CourseStatus,
    build_course_registry,
)
from app.repositories import (
    CourseLoader,
    CourseRepository,
    CrossCourseQueries,
    Database,
    RateLimitRepository,
    UserRepository,
)
from app.routes import create_web_blueprint
from app.services import resolve_display_timezone
from app.web.course_context import build_form_serializer

PROJECT_ROOT = Path(__file__).resolve().parent.parent
QUESTION_FILE = PROJECT_ROOT / "questions.json"
GLOSSARY_FILE = PROJECT_ROOT / "glossary.json"
COURSES_DIR = PROJECT_ROOT / "courses"
DATABASE_FILE = PROJECT_ROOT / "instance" / "mcq.db"
KNOWLEDGE_VERIFICATION_TARGET = 2

__all__ = [
    "AppServices",
    "COURSES_DIR",
    "DATABASE_FILE",
    "GLOSSARY_FILE",
    "QUESTION_FILE",
    "create_app",
]


def create_app(test_config: dict[str, Any] | None = None) -> Flask:
    """Load every course, initialize SQLite, and assemble the web app."""
    instance_path = str(PROJECT_ROOT / "instance")
    app = Flask(__name__, instance_path=instance_path, instance_relative_config=False)
    configured_secret = os.environ.get("MCQ_SECRET_KEY")
    app.config.from_mapping(
        SECRET_KEY=configured_secret or secrets.token_hex(32),
        QUESTION_FILE=QUESTION_FILE,
        GLOSSARY_FILE=GLOSSARY_FILE,
        COURSES_DIR=COURSES_DIR,
        DATABASE=DATABASE_FILE,
        # A navigation *preference* only: it decides where a browser without an
        # explicit course URL lands, and never re-owns historical data.
        DEFAULT_COURSE_ID=os.environ.get("MCQ_DEFAULT_COURSE"),
        KNOWLEDGE_VERIFICATION_TARGET=KNOWLEDGE_VERIFICATION_TARGET,
        DISPLAY_TIMEZONE=os.environ.get("MCQ_DISPLAY_TIMEZONE"),
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=True,
        MAX_CONTENT_LENGTH=64 * 1024,
        ENABLE_CSRF=True,
        AUTH_LOGIN_ACCOUNT_LIMIT=10,
        AUTH_LOGIN_IP_LIMIT=100,
        AUTH_LOGIN_WINDOW_SECONDS=300,
        AUTH_REGISTER_IP_LIMIT=10,
        AUTH_REGISTER_WINDOW_SECONDS=3600,
    )
    if test_config:
        app.config.update(test_config)
        if app.config.get("TESTING") and "SESSION_COOKIE_SECURE" not in test_config:
            app.config["SESSION_COOKIE_SECURE"] = False
        if app.config.get("TESTING") and "ENABLE_CSRF" not in test_config:
            app.config["ENABLE_CSRF"] = False
    elif configured_secret is None:
        app.logger.warning(
            "MCQ_SECRET_KEY is not set; using an ephemeral development secret."
        )

    display_timezone = resolve_display_timezone(app.config["DISPLAY_TIMEZONE"])
    question_file = Path(app.config["QUESTION_FILE"]).resolve()
    glossary_file = Path(app.config["GLOSSARY_FILE"]).resolve()
    courses_dir = Path(app.config["COURSES_DIR"]).resolve()

    loader = CourseLoader(
        courses_dir,
        legacy_directory=question_file.parent,
        legacy_questions_name=question_file.name,
        legacy_glossary_name=glossary_file.name,
    )

    database = Database(Path(app.config["DATABASE"]).resolve())
    database.initialize()
    user_repository = UserRepository(database)
    rate_limit_repository = RateLimitRepository(database)
    course_repository = CourseRepository(database)
    cross_course = CrossCourseQueries(database)

    course_registry = build_course_registry(
        loader=loader,
        database=database,
        course_repository=course_repository,
        knowledge_verification_target=int(app.config["KNOWLEDGE_VERIFICATION_TARGET"]),
        display_timezone=display_timezone,
        user_repository=user_repository,
        default_course_id=app.config.get("DEFAULT_COURSE_ID"),
    )

    form_serializer = build_form_serializer(app.config["SECRET_KEY"])
    services = AppServices(
        database=database,
        user_repository=user_repository,
        rate_limit_repository=rate_limit_repository,
        course_repository=course_repository,
        course_registry=course_registry,
        display_timezone=display_timezone,
        legacy_course_id=course_repository.legacy_course_id(),
    )
    app.extensions["mcq_services"] = services
    app.extensions["mcq_form_serializer"] = form_serializer
    app.extensions["mcq_course_loader"] = loader
    app.extensions["mcq_cross_course"] = cross_course

    _register_readiness_endpoints(app, course_registry)

    @app.after_request
    def add_security_headers(response):
        """Apply browser protections to public and authenticated responses alike."""
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault(
            "Referrer-Policy", "strict-origin-when-cross-origin"
        )
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        if request.is_secure:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; base-uri 'self'; object-src 'none'; "
            "frame-ancestors 'none'; script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "form-action 'self'",
        )
        return response

    app.register_blueprint(
        create_web_blueprint(
            user_repository=user_repository,
            rate_limit_repository=rate_limit_repository,
            course_repository=course_repository,
            course_registry=course_registry,
            cross_course=cross_course,
            display_timezone=display_timezone,
            form_serializer=form_serializer,
        )
    )
    return app


def _register_readiness_endpoints(
    app: Flask, course_registry: CourseRegistry
) -> None:
    """Register ``/health``, ``/ready`` and ``/ready/<course_id>``.

    ``/health`` is liveness only: a stale worker is still alive and must still
    let a learner sign out.  ``/ready`` is this worker's aggregate signal over
    its declared, enabled courses, and ``/ready/<course_id>`` reports one course.
    Aggregate failure is a *monitoring* signal: it never makes a healthy
    course's route answer 503.
    """

    @app.get("/health")
    def health() -> tuple[dict[str, str], int]:
        return {"status": "ok"}, 200

    @app.get("/ready")
    def ready() -> tuple[dict[str, Any], int]:
        ok, payload = course_registry.readiness()
        return payload, 200 if ok else 503

    @app.get("/ready/<course_id>")
    def ready_course(course_id: str) -> tuple[dict[str, Any], int]:
        ok, payload = course_registry.course_readiness(course_id)
        if payload.get("status") == "unknown":
            payload = {
                **payload,
                "course_id": course_id,
                "known_courses": [
                    state.course_id
                    for state in course_registry.states()
                    if state.declared
                ],
            }
            return payload, 404
        state = course_registry.state(course_id)
        if state is not None and state.status is CourseStatus.DISABLED:
            return payload, 503
        return payload, 200 if ok else 503

