"""Flask application factory for the local MCQ practice tool."""

import os
import secrets
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from flask import Flask, request

from app.repositories import (
    AttemptRepository,
    Database,
    ExamRepository,
    GlossaryLoader,
    GlossaryRepository,
    ProgressRepository,
    QuestionBankStateRepository,
    QuestionLoader,
    QuestionRepository,
    RateLimitRepository,
    UserRepository,
    WeakKnowledgePointRepository,
    WrongQuestionRepository,
)
from app.routes import create_web_blueprint
from app.services import (
    ExamService,
    GradingService,
    QuizService,
    StatisticsService,
    WeakKnowledgePointService,
    WrongQuestionService,
    resolve_display_timezone,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
QUESTION_FILE = PROJECT_ROOT / "questions.json"
GLOSSARY_FILE = PROJECT_ROOT / "glossary.json"
DATABASE_FILE = PROJECT_ROOT / "instance" / "mcq.db"
KNOWLEDGE_VERIFICATION_TARGET = 2


@dataclass(frozen=True)
class AppServices:
    """Visible dependency container useful for integration and diagnostics."""

    question_repository: QuestionRepository
    glossary_repository: GlossaryRepository
    user_repository: UserRepository
    attempt_repository: AttemptRepository
    wrong_question_repository: WrongQuestionRepository
    weak_knowledge_point_repository: WeakKnowledgePointRepository
    grading_service: GradingService
    weak_knowledge_point_service: WeakKnowledgePointService
    wrong_question_service: WrongQuestionService
    progress_repository: ProgressRepository
    quiz_service: QuizService
    exam_repository: ExamRepository
    exam_service: ExamService
    statistics_service: StatisticsService


def create_app(test_config: dict[str, Any] | None = None) -> Flask:
    """Load course content, initialize SQLite, and assemble the web app."""
    instance_path = str(PROJECT_ROOT / "instance")
    app = Flask(__name__, instance_path=instance_path, instance_relative_config=False)
    configured_secret = os.environ.get("MCQ_SECRET_KEY")
    app.config.from_mapping(
        SECRET_KEY=configured_secret or secrets.token_hex(32),
        QUESTION_FILE=QUESTION_FILE,
        GLOSSARY_FILE=GLOSSARY_FILE,
        DATABASE=DATABASE_FILE,
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

    question_file = Path(app.config["QUESTION_FILE"]).resolve()
    question_loader = QuestionLoader(question_file)
    questions = question_loader.load()
    question_bank_version = question_loader.source_fingerprint
    if question_bank_version is None:
        raise RuntimeError("Question bank fingerprint was not generated.")
    display_timezone = resolve_display_timezone(app.config["DISPLAY_TIMEZONE"])
    question_repository = QuestionRepository(
        questions,
        title=question_loader.title,
        title_zh=question_loader.title_zh,
        sources=question_loader.sources,
        chapters=question_loader.chapters,
    )

    glossary = GlossaryLoader(Path(app.config["GLOSSARY_FILE"]).resolve()).load()
    glossary_repository = GlossaryRepository(glossary)

    database = Database(Path(app.config["DATABASE"]).resolve())
    database.initialize()
    question_bank_state_repository = QuestionBankStateRepository(database)
    bank_generation = question_bank_state_repository.synchronize(
        question_bank_version
    )
    rate_limit_repository = RateLimitRepository(database)
    progress_repository = ProgressRepository(database)
    user_repository = UserRepository(database)
    attempt_repository = AttemptRepository(database)
    wrong_question_repository = WrongQuestionRepository(database)
    weak_knowledge_point_repository = WeakKnowledgePointRepository(database)
    grading_service = GradingService()
    weak_knowledge_point_service = WeakKnowledgePointService(
        repository=weak_knowledge_point_repository,
        question_repository=question_repository,
        wrong_question_repository=wrong_question_repository,
        verification_target=int(app.config["KNOWLEDGE_VERIFICATION_TARGET"]),
    )
    weak_knowledge_point_service.backfill_existing_wrong_questions()
    wrong_question_service = WrongQuestionService(
        attempt_repository=attempt_repository,
        wrong_question_repository=wrong_question_repository,
        question_repository=question_repository,
        weak_knowledge_point_service=weak_knowledge_point_service,
    )
    quiz_service = QuizService(
        question_repository=question_repository,
        grading_service=grading_service,
        wrong_question_service=wrong_question_service,
        weak_knowledge_point_service=weak_knowledge_point_service,
    )
    exam_repository = ExamRepository(database)
    exam_service = ExamService(
        exam_repository=exam_repository,
        question_repository=question_repository,
        grading_service=grading_service,
        wrong_question_service=wrong_question_service,
    )
    statistics_service = StatisticsService(
        attempt_repository=attempt_repository,
        question_repository=question_repository,
        wrong_question_service=wrong_question_service,
        display_tz=display_timezone,
    )

    services = AppServices(
        question_repository=question_repository,
        glossary_repository=glossary_repository,
        user_repository=user_repository,
        attempt_repository=attempt_repository,
        wrong_question_repository=wrong_question_repository,
        weak_knowledge_point_repository=weak_knowledge_point_repository,
        grading_service=grading_service,
        weak_knowledge_point_service=weak_knowledge_point_service,
        wrong_question_service=wrong_question_service,
        quiz_service=quiz_service,
        progress_repository=progress_repository,
        exam_repository=exam_repository,
        exam_service=exam_service,
        statistics_service=statistics_service,
    )
    app.extensions["mcq_services"] = services

    @app.get("/health")
    def health() -> tuple[dict[str, str], int]:
        """Report that the fully assembled application is ready to serve."""
        return {"status": "ok"}, 200

    @app.after_request
    def add_security_headers(response):
        """Apply browser protections to public and authenticated responses alike."""
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if request.is_secure:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; "
            "script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "form-action 'self'",
        )
        return response

    app.register_blueprint(
        create_web_blueprint(
            question_repository=question_repository,
            glossary_repository=glossary_repository,
            user_repository=user_repository,
            quiz_service=quiz_service,
            progress_repository=progress_repository,
            rate_limit_repository=rate_limit_repository,
            wrong_question_service=wrong_question_service,
            weak_knowledge_point_service=weak_knowledge_point_service,
            exam_service=exam_service,
            statistics_service=statistics_service,
            display_timezone=display_timezone,
            question_bank_version=question_bank_version,
            bank_generation=bank_generation,
        )
    )
    return app
