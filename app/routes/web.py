"""Web routes for accounts and per-course learning.

Every learning route carries ``<course_id>`` in its URL, and that URL is the
only authority for which course a request acts on.  ``session["last_course_id"]``
is a navigation *preference* used by ``/`` and by the legacy redirect shims —
it never overrides an explicit course URL, so two tabs on two courses cannot
rewrite each other.

The old course-less URLs are still registered:

* ``GET`` redirects to the explicit course URL (resolving the real owner from the
  database for an exam ID, after checking learner ownership);
* ``POST`` is **never** guessed from the session and answers 409 with a
  refresh-required message.

Every learner write runs inside
:func:`app.services.course_consistency.guarded_learner_transaction`, which
re-checks the course's servability, the worker's generation and the signed form
context after taking the write lock.
"""

import logging
import secrets
from datetime import tzinfo
from functools import partial, wraps
from typing import Any

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    g,
    make_response,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.exceptions import HTTPException

from app.course_runtime import (
    CourseNotFoundError,
    CourseRegistry,
    CourseStatus,
    CourseUnavailableError,
)
from app.models import Chapter, ExamSession, ExamStatus, Question, QuizMode
from app.repositories import (
    CourseRepository,
    CrossCourseQueries,
    RateLimitRepository,
    UserRepository,
    UsernameAlreadyExistsError,
)
from app.services import (
    AnswerResult,
    AnswerValidationError,
    ExamConfigError,
    ExamNotFoundError,
    ExamStateError,
)
from app.services import course_consistency as _consistency
from app.services import local_time as _local_time
from app.services import progress_state as _progress_state
from app.services import srs_service as _srs
from app.web import auth as _web_auth
from app.web import course_context as _course_ctx
from app.web import view_helpers as _view

LOGGER = logging.getLogger(__name__)

# Learner-facing wording for the stale-worker 503.  Restarting the service is
# an operator action, so the message never asks a learner to do it.
STALE_BANK_MESSAGE = "题库正在更新，请稍后刷新页面；如果长时间未恢复，请联系管理员。"
# Shown as a flash when an exempt page would otherwise hand a learner to a
# learning page that can only answer 503.
STALE_BANK_NOTICE = (
    "题库正在更新，暂时只能浏览课程列表与术语表；如果长时间未恢复，请联系管理员。"
)
STALE_BANK_RETRY_AFTER_SECONDS = 5
#: Old course-less POSTs are not guessed; the learner must reload the page.
REFRESH_REQUIRED_MESSAGE = (
    "该页面已经过期（课程或题库版本已更新），请刷新页面后重新提交。"
)
COURSE_NOT_FOUND_MESSAGE = "没有找到该课程。"
COURSE_DISABLED_MESSAGE = "该课程当前已停用。"
COURSE_UNAVAILABLE_MESSAGE = "该课程暂时不可用，请稍后再试或选择其他课程。"
NO_COURSE_MESSAGE = "当前没有可用课程。"
CROSS_COURSE_EXAM_MESSAGE = "该考试不属于当前课程，请回到对应课程查看。"

#: Endpoints reachable without signing in.
PUBLIC_ENDPOINTS = frozenset({"web.login", "web.register"})

#: Course pages that stay available on a stale worker: learners must always be
#: able to read reference material and leave a shared device.
STALE_EXEMPT_ENDPOINTS = frozenset(
    {"web.glossary", "web.legacy_glossary", "web.logout"}
)

#: Prefix used for the course-less compatibility aliases.
LEGACY_PREFIX = "legacy_"

#: Payload injected when the current course has no glossary at all.
NO_GLOSSARY_PAYLOAD: dict[str, Any] = {
    "schema_version": 1,
    "title": "",
    "title_zh": "",
    "description": None,
    "description_zh": None,
    "terms": [],
}


def create_web_blueprint(
    *,
    user_repository: UserRepository,
    rate_limit_repository: RateLimitRepository,
    course_repository: CourseRepository,
    course_registry: CourseRegistry,
    cross_course: CrossCourseQueries,
    display_timezone: tzinfo,
    form_serializer,
) -> Blueprint:
    """Build the learner-facing web blueprint."""
    blueprint = Blueprint("web", __name__)

    _ip_login_allowed = partial(_web_auth.login_ip_allowed, rate_limit_repository)
    _account_login_allowed = partial(
        _web_auth.login_account_allowed, rate_limit_repository
    )
    _record_login_failure = partial(
        _web_auth.record_login_failure, rate_limit_repository
    )
    _registration_allowed = partial(
        _web_auth.registration_allowed, rate_limit_repository
    )
    scoped_endpoints: set[str] = set()

    def course_route(rule: str, methods: tuple[str, ...] = ("GET",)):
        """Register one rule both course-scoped and as a legacy alias.

        The ``course_id`` URL variable is consumed here, not by the view: the
        course context was already resolved in ``before_request`` and handed to
        the view through ``g.course``.
        """

        def decorator(view):
            name = view.__name__

            @wraps(view)
            def dispatch(*args, course_id: str | None = None, **kwargs):
                return view(*args, **kwargs)

            scoped_endpoints.add(f"web.{name}")
            blueprint.add_url_rule(
                f"/course/<course_id>{rule}",
                endpoint=name,
                view_func=dispatch,
                methods=list(methods),
            )
            blueprint.add_url_rule(
                rule,
                endpoint=f"{LEGACY_PREFIX}{name}",
                view_func=dispatch,
                methods=list(methods),
                defaults={"course_id": None},
            )
            return view

        return decorator

    def operation_name() -> str | None:
        endpoint = request.endpoint or ""
        if endpoint.startswith(f"web.{LEGACY_PREFIX}"):
            return endpoint[len(f"web.{LEGACY_PREFIX}") :]
        return _course_ctx.operation_for_endpoint(endpoint)

    def issue_form_context(target_endpoint: str, **extra: Any) -> str:
        """Return the signed form context of one form.

        ``target_endpoint`` is the route the form **posts to** — the same value
        the template hands to ``url_for`` for the form's ``action`` — and never
        the page that rendered it.  The transaction guard compares the signed
        operation against the endpoint the request actually reached, so signing
        the rendering page's endpoint would reject every submit with 409.
        A bare operation name (``"start_quiz"``) is accepted and kept as-is.
        """
        course = getattr(g, "course", None)
        if course is None:
            return ""
        return _course_ctx.issue_form_context(
            form_serializer,
            course_id=course.course_id,
            operation=_course_ctx.operation_for_endpoint(target_endpoint) or "",
            generation=course.generation,
            **extra,
        )

    # ------------------------------------------------------------ page context

    @blueprint.app_context_processor
    def inject_global_page_data() -> dict[str, Any]:
        course = getattr(g, "course", None)
        glossary_repository = getattr(course, "glossary_repository", None)
        return {
            "bank_title": course.title if course else "",
            "bank_title_zh": course.title_zh if course else "",
            "current_course": course.course if course else None,
            "course_states": course_registry.states(),
            "course_summaries": [
                {
                    "course_id": state.course_id,
                    "title": state.course.title_zh or state.course.title,
                    "servable": state.servable,
                }
                for state in course_registry.states()
                if state.declared and state.course.enabled
            ],
            "glossary_data": (
                glossary_repository.to_dict()
                if glossary_repository is not None
                else NO_GLOSSARY_PAYLOAD
            ),
            "has_glossary": glossary_repository is not None,
            "form_context": issue_form_context,
            "option_label": _view.option_label,
            "format_stem": _view.format_stem,
            "csrf_token": _web_auth.csrf_token,
            "format_duration": _view.format_duration,
            "format_datetime": partial(_view.format_datetime, zone=display_timezone),
            "display_tz_label": _local_time.timezone_label(display_timezone),
        }


    # ------------------------------------------------------------ course context

    @blueprint.before_request
    def require_account() -> Any:
        """Validate the request envelope and load the signed-in user."""
        if request.method == "POST":
            if request.content_length and request.content_length > 64 * 1024:
                abort(413)
            if current_app.config["ENABLE_CSRF"]:
                _web_auth.validate_csrf()
        if request.endpoint in PUBLIC_ENDPOINTS:
            return None
        user_id = session.get("user_id")
        user = user_repository.get_by_id(user_id) if isinstance(user_id, str) else None
        if user is None:
            session.clear()
            flash("请先登录后再开始学习。", "info")
            return redirect(url_for("web.login"))
        g.user = user
        g.learner_id = user.id
        return None

    @blueprint.before_request
    def resolve_course_context() -> Any:
        """Bind the request to exactly one course, or redirect/reject.

        The URL is authoritative.  A course-less legacy URL is redirected for a
        ``GET`` and refused for anything else, so a stale form can never be
        guessed into a course from the session.
        """
        endpoint = request.endpoint
        if endpoint is None or endpoint in _course_ctx.COURSE_EXEMPT_ENDPOINTS:
            return None
        if endpoint.startswith(f"web.{LEGACY_PREFIX}"):
            return _redirect_legacy_request(endpoint)
        course_id = (request.view_args or {}).get("course_id")
        if not isinstance(course_id, str) or not course_id:
            abort(404, description=COURSE_NOT_FOUND_MESSAGE)
        return _bind_course(course_id)

    def _bind_course(course_id: str) -> Any:
        state = course_registry.state(course_id)
        if state is None:
            abort(404, description=COURSE_NOT_FOUND_MESSAGE)
        # Recorded before any abort so error pages can name the course.
        g.course_id = course_id
        if state.status is CourseStatus.DISABLED:
            abort(503, description=COURSE_DISABLED_MESSAGE)
        if state.status is CourseStatus.UNAVAILABLE:
            abort(503, description=f"{COURSE_UNAVAILABLE_MESSAGE}（{state.reason}）")
        exempt_from_fence = request.endpoint in STALE_EXEMPT_ENDPOINTS
        if state.status is CourseStatus.STALE and not exempt_from_fence:
            LOGGER.warning(
                'Course "%s" is stale on this worker (worker=%s db=%s) path=%s',
                course_id,
                state.generation,
                state.database_generation,
                request.path,
            )
            abort(503, description=STALE_BANK_MESSAGE)
        if exempt_from_fence:
            # Read-only reference pages keep working on a superseded worker,
            # reading *this* course's own snapshot.
            services = course_registry.loaded_services(course_id)
        else:
            try:
                services = course_registry.get(course_id)
            except CourseNotFoundError:
                abort(404, description=COURSE_NOT_FOUND_MESSAGE)
            except CourseUnavailableError:
                abort(503, description=COURSE_UNAVAILABLE_MESSAGE)
        if services is None:
            abort(503, description=COURSE_UNAVAILABLE_MESSAGE)
        g.course = services
        g.course_id = course_id
        g.learner_course_preference = course_id
        session["last_course_id"] = course_id
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            context = _course_ctx.submitted_form_context(form_serializer)
            if context is None:
                abort(409, description=REFRESH_REQUIRED_MESSAGE)
            g.form_context = context
        else:
            g.form_context = _course_ctx.submitted_form_context(form_serializer)
        return None

    def course_is_live(services) -> bool:
        """Return whether the database still serves this worker's generation."""
        return (
            services.question_bank_state_repository.get_generation()
            == services.generation
        )

    def _redirect_legacy_request(endpoint: str) -> Any:
        """Send an old course-less URL to the explicit course URL.

        A ``GET`` is a navigation and may be redirected.  A ``POST`` is a state
        change and is never guessed: it answers 409 so the browser reloads the
        page at its real, course-scoped URL without any learner write.
        """
        base_endpoint = f"web.{endpoint[len(f'web.{LEGACY_PREFIX}'):]}"
        if request.method not in {"GET", "HEAD"}:
            LOGGER.info("Refused a course-less POST to %s.", request.path)
            abort(409, description=REFRESH_REQUIRED_MESSAGE)
        view_args = {
            key: value
            for key, value in (request.view_args or {}).items()
            if key != "course_id" and value is not None
        }
        course_id = _legacy_course_for(view_args)
        if course_id is None:
            return redirect(url_for("web.courses"))
        query = {**view_args, **request.args.to_dict(flat=True)}
        return redirect(url_for(base_endpoint, course_id=course_id, **query))

    def _legacy_course_for(view_args: dict[str, Any]) -> str | None:
        """Resolve the course a course-less URL refers to.

        An exam ID resolves to the course that actually owns it (the namespace
        comes from the database, never from the session), which keeps old
        report bookmarks working across course switches.  Everything else uses
        the browser's last-course preference and then the configured default.
        """
        exam_id = view_args.get("exam_id")
        if isinstance(exam_id, str) and exam_id:
            learner_id = getattr(g, "learner_id", None)
            if not learner_id:
                abort(404, description=COURSE_NOT_FOUND_MESSAGE)
            course_id = cross_course.find_course_for_exam_of_learner(exam_id, learner_id)
            if course_id is None:
                abort(404, description=COURSE_NOT_FOUND_MESSAGE)
            return course_id
        preference = session.get("last_course_id")
        return course_registry.resolve_default_course_id(
            preference if isinstance(preference, str) else None
        )


    # ------------------------------------------------------------ learner writes

    def _latest_correctness(learner_id: str, mode: QuizMode):
        """Bind the legacy counter fallback for progress reconciliation."""

        def lookup(question_id: str) -> bool | None:
            attempt = (
                g.course.attempt_repository.get_latest_attempt_for(
                    learner_id, question_id, mode
                )
            )
            return attempt.is_correct if attempt is not None else None

        return lookup

    def shared_progress(view):
        """Run one view inside this course's fenced learner transaction.

        The transaction re-checks the course's servability, this worker's
        generation and the signed form context *after* taking the write lock, so
        the "outer pre-check passed, sibling worker published, then the old
        worker starts writing" race ends with zero learner writes.
        """

        @wraps(view)
        def wrapped(*args, **kwargs):
            services = g.course
            try:
                with _consistency.guarded_learner_transaction(
                    database=services.progress_repository.database,
                    course_repository=course_repository,
                    state_repository=services.question_bank_state_repository,
                    course_id=services.course_id,
                    worker_generation=services.generation,
                    operation=operation_name(),
                    form_context=getattr(g, "form_context", None),
                ):
                    return _run_with_progress(view, args, kwargs)
            except _consistency.StaleWorkerError as exc:
                LOGGER.warning(
                    'Rejected a learner write for course "%s": worker=%s db=%s',
                    services.course_id,
                    exc.worker_generation,
                    exc.database_generation,
                )
                abort(503, description=STALE_BANK_MESSAGE)
            except _consistency.CourseServabilityChangedError:
                abort(503, description=COURSE_DISABLED_MESSAGE)
            except _consistency.StaleFormError as exc:
                LOGGER.info("Rejected a stale form: %s", exc.reason)
                abort(409, description=REFRESH_REQUIRED_MESSAGE)

        return wrapped

    def _run_with_progress(view, args, kwargs) -> Any:
        """Load, expose and persist the course's resumable rounds around a view."""
        _load_practice_progress()
        response = view(*args, **kwargs)
        _persist_practice_progress()
        return response

    def _load_practice_progress() -> None:
        """Expose this learner's stored rounds of this course on ``g``.

        Each mode's row is validated and reconciled against the live bank
        before a view can read it, so a round written against an older or
        invalid state can never be resumed as-is.  Any ancient cookie copy is
        purged without being read: the server-side row is the only source of
        practice progress.
        """
        progress_repository = g.course.progress_repository
        question_repository = g.course.question_repository
        g.quiz_progress = {}
        for mode in _progress_state.PRACTICE_MODES:
            session.pop(_progress_state.session_key(mode), None)
            stored = progress_repository.get(g.learner_id, mode)
            state = stored[1] if stored is not None else None
            if not _progress_state.is_valid_progress_state(state, mode):
                state = None
            elif state is not None:
                # Defensive sweep: startup reconciliation already removed
                # unusable questions, so this normally no-ops.
                state, _reconciled = _progress_state.reconcile_state(
                    state,
                    mode,
                    is_usable=question_repository.has,
                    lookup_correct=_latest_correctness(g.learner_id, mode),
                    is_live_chapter=lambda chapter_id: (
                        question_repository.get_chapter(chapter_id) is not None
                    ),
                )
            g.quiz_progress[_progress_state.session_key(mode)] = state
        session.pop("quiz_progress", None)
        session.pop("question_bank_version", None)

    def _persist_practice_progress() -> None:
        """Write back only the modes whose stored practice state really changed.

        ``bank_version`` is diagnostic, so two workers running banks that
        differ only in wording must not rewrite this row back and forth.
        """
        services = g.course
        progress_repository = services.progress_repository
        for mode in _progress_state.PRACTICE_MODES:
            state = g.quiz_progress.get(_progress_state.session_key(mode))
            stored = progress_repository.get(g.learner_id, mode)
            stored_state = stored[1] if stored is not None else None
            if (stored is not None or state is not None) and (
                stored is None or stored_state != state
            ):
                progress_repository.save(
                    g.learner_id, mode, services.bank_version, state
                )

    @blueprint.after_request
    def prevent_stale_progress_cache(response):
        if hasattr(g, "learner_id"):
            response.headers["Cache-Control"] = "no-store"
        return response


    # ------------------------------------------------------------ navigation

    @blueprint.get("/")
    def index() -> Any:
        """Send a browser without a course URL to its preferred course."""
        preference = session.get("last_course_id")
        course_id = course_registry.resolve_default_course_id(
            preference if isinstance(preference, str) else None
        )
        if course_id is None:
            return redirect(url_for("web.courses"))
        services = course_registry.find(course_id)
        if services is not None and not course_is_live(services):
            # The preference points at a course this worker can no longer serve:
            # do not silently land the learner in a different course.
            abort(503, description=STALE_BANK_MESSAGE)
        return redirect(url_for("web.home", course_id=course_id))

    @blueprint.get("/courses")
    def courses() -> str:
        """Show every course this worker knows and its current status."""
        return render_template("courses.html", states=course_registry.states())

    @blueprint.route("/login", methods=["GET", "POST"])
    def login() -> Any:
        existing_id = session.get("user_id")
        if request.method == "GET" and isinstance(existing_id, str):
            if user_repository.get_by_id(existing_id) is not None:
                return _redirect_after_sign_in()
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            if not _ip_login_allowed(request.remote_addr):
                abort(429, description="登录尝试过于频繁，请稍后再试。")
            user = user_repository.authenticate(username, password)
            if user is None:
                if not _account_login_allowed(username):
                    abort(429, description="该账号的登录尝试过于频繁，请稍后再试。")
                _record_login_failure(username, request.remote_addr)
                flash("用户名或密码不正确。", "error")
            else:
                session.clear()
                session["user_id"] = user.id
                session.permanent = True
                flash(f"欢迎回来，{user.username}。", "success")
                return _redirect_after_sign_in()
        return render_template("auth.html", page="login")

    @blueprint.route("/register", methods=["GET", "POST"])
    def register() -> Any:
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            confirmation = request.form.get("password_confirmation", "")
            error = _web_auth.registration_error(username, password, confirmation)
            if not _registration_allowed(request.remote_addr):
                abort(429, description="请求过于频繁，请稍后再试。")
            if error:
                flash(error, "error")
            else:
                try:
                    user = user_repository.create(username, password)
                except UsernameAlreadyExistsError as exc:
                    flash(str(exc), "error")
                else:
                    session.clear()
                    session["user_id"] = user.id
                    session.permanent = True
                    flash("账号创建成功，学习记录将只对你可见。", "success")
                    return _redirect_after_sign_in()
        return render_template("auth.html", page="register")

    @blueprint.post("/logout")
    def logout() -> Any:
        """Clear the session and return to the login page.

        Deliberately outside the course transaction: a worker whose content was
        superseded must still let a learner sign out of a shared device instead
        of answering 503.
        """
        session.clear()
        flash("你已安全退出。", "success")
        return redirect(url_for("web.login"))

    def _redirect_after_sign_in() -> Any:
        """Send a just-authenticated learner to the best page this worker has.

        The account pages are exempt from the per-course fence, so they must not
        hand the learner to a course that can only answer 503: a degraded worker
        explains the situation and offers the course selector instead.
        """
        course_id = course_registry.resolve_default_course_id()
        if course_id is None:
            flash(NO_COURSE_MESSAGE, "info")
            return redirect(url_for("web.courses"))
        services = course_registry.find(course_id)
        if services is None or not course_is_live(services):
            flash(STALE_BANK_NOTICE, "info")
            return redirect(url_for("web.courses"))
        return redirect(url_for("web.home", course_id=course_id))


    # ------------------------------------------------------------ practice

    @course_route("/")
    @shared_progress
    def home() -> str:
        """Show the personal landing page of the current course.

        Expired settlement is course-scoped: opening course A's home settles
        only A's expired exams, never B's.
        """
        services = g.course
        wrong_question_service = services.wrong_question_service
        exam_service = services.exam_service
        exam_service.finalize_expired_for_learner(g.learner_id)
        stats = wrong_question_service.get_stats(g.learner_id)
        return render_template(
            "home.html",
            question_count=len(services.question_repository.get_all()),
            stats=stats,
            srs_due_count=wrong_question_service.get_due_srs_count(g.learner_id),
            normal_progress=_active_progress(QuizMode.NORMAL),
            review_progress=_active_progress(QuizMode.REVIEW),
            active_exam=exam_service.get_active_session(g.learner_id),
        )

    @course_route("/dashboard")
    @shared_progress
    def dashboard() -> str:
        """Show the learner's aggregated learning statistics for this course."""
        g.course.exam_service.finalize_expired_for_learner(g.learner_id)
        return render_template(
            "dashboard.html",
            dashboard=g.course.statistics_service.build_dashboard(g.learner_id),
        )

    @course_route("/stats")
    def stats() -> str:
        """Show this course's statistics aggregated across every account.

        Reference-only counterpart of the personal dashboard: the page renders
        group totals and chapter difficulty, never single-account details.  The
        course is the URL's course, so it is still a learning page and is fenced
        per course.
        """
        return render_template(
            "stats.html",
            overview=g.course.global_statistics_service.build_overview(),
        )

    @course_route("/quiz/setup")
    @shared_progress
    def quiz_setup() -> str:
        """Show the dynamic course-material and chapter selection screen."""
        return render_template(
            "quiz_setup.html",
            curriculum=_curriculum(),
            question_count=len(g.course.question_repository.get_all()),
            normal_progress=_active_progress(QuizMode.NORMAL),
        )

    @course_route("/glossary")
    def glossary() -> Any:
        """Show this course's data-driven terminology page.

        A course without a glossary renders an explicit "no glossary" state and
        never falls back to another course's payload.
        """
        repository = g.course.glossary_repository
        if repository is None:
            return render_template(
                "glossary.html",
                glossary=None,
                terms=(),
                categories=(),
            )
        return render_template(
            "glossary.html",
            glossary=repository,
            terms=repository.all_terms(),
            categories=repository.categories(),
        )

    @course_route("/quiz/start", methods=("POST",))
    @shared_progress
    def start_quiz() -> Any:
        raw_size = request.form.get("quiz_size", "20")
        limit = _progress_state.quiz_limit(raw_size)
        chapter_ids = _view.catalogue_filter(
            request.form.getlist("chapter_ids"),
            {chapter.id for chapter in g.course.question_repository.get_chapters()},
            "章节",
        )
        question_ids = _begin(
            QuizMode.NORMAL,
            limit=limit,
            requested_size=raw_size,
            chapter_ids=chapter_ids,
        )
        if not question_ids:
            g.quiz_progress.pop(_progress_state.session_key(QuizMode.NORMAL), None)
            flash("所选章节暂时没有可练习题目。", "info")
            return redirect(url_for("web.quiz_setup", course_id=g.course_id))
        return redirect(url_for("web.quiz", course_id=g.course_id))

    @course_route("/quiz")
    @shared_progress
    def quiz() -> Any:
        return _render_quiz(QuizMode.NORMAL)

    @course_route("/quiz/answer", methods=("POST",))
    @shared_progress
    def answer_quiz() -> Any:
        return _answer(QuizMode.NORMAL)

    @course_route("/quiz/next", methods=("POST",))
    @shared_progress
    def next_quiz() -> Any:
        return _next(QuizMode.NORMAL)


    # ------------------------------------------------------------ mistakes / review

    @course_route("/mistakes")
    @shared_progress
    def mistakes() -> str:
        services = g.course
        question_repository = services.question_repository
        wrong_question_service = services.wrong_question_service
        weak_knowledge_point_service = services.weak_knowledge_point_service
        selected_source = request.args.get("source", "").strip()
        selected_chapter = request.args.get("chapter", "").strip()
        source_ids = _view.single_catalogue_filter(
            selected_source,
            {source.id for source in question_repository.get_sources()},
            "课件",
        )
        chapter_ids = _view.single_catalogue_filter(
            selected_chapter,
            {chapter.id for chapter in question_repository.get_chapters()},
            "章节",
        )
        items = wrong_question_service.get_items(
            g.learner_id, chapter_ids=chapter_ids, source_ids=source_ids
        )
        stats = {
            "pending": sum(not item.record.corrected for item in items),
            "corrected": sum(item.record.corrected for item in items),
        }
        due_srs_count = len(
            wrong_question_service.get_filtered_due_srs_question_ids(
                g.learner_id, chapter_ids=chapter_ids, source_ids=source_ids
            )
        )
        total_stats = wrong_question_service.get_stats(g.learner_id)
        weak_points = weak_knowledge_point_service.get_summaries(
            g.learner_id, chapter_ids=chapter_ids, source_ids=source_ids
        )
        all_weak_points = weak_knowledge_point_service.get_summaries(g.learner_id)
        return render_template(
            "mistakes.html",
            items=items,
            stats=stats,
            srs_due_count=due_srs_count,
            weak_points=weak_points,
            has_review_work=(
                stats["pending"] > 0
                or due_srs_count > 0
                or any(summary.point.active for summary in weak_points)
            ),
            has_mistakes=(
                total_stats["pending"] + total_stats["corrected"] > 0
                or bool(all_weak_points)
            ),
            review_progress=_active_progress(QuizMode.REVIEW),
            verification_target=weak_knowledge_point_service.verification_target,
            curriculum=_curriculum(),
            selected_source=selected_source,
            selected_chapter=selected_chapter,
            sources_by_id={
                source.id: source for source in question_repository.get_sources()
            },
            chapters_by_id={
                chapter.id: chapter for chapter in question_repository.get_chapters()
            },
        )

    @course_route("/mistakes/reset", methods=("POST",))
    @shared_progress
    def reset_mistakes() -> Any:
        """Reset *this learner's* mistake state *in this course* only."""
        services = g.course
        deleted_count = services.wrong_question_service.reset(g.learner_id)
        services.progress_repository.save(
            g.learner_id, QuizMode.REVIEW, services.bank_version, None
        )
        g.quiz_progress.pop(_progress_state.session_key(QuizMode.REVIEW), None)
        if deleted_count:
            flash(f"已将 {deleted_count} 道错题重置为 0。", "success")
        else:
            flash("错题数已经是 0。", "info")
        return redirect(url_for("web.mistakes", course_id=g.course_id))

    @course_route("/review/start", methods=("POST",))
    @shared_progress
    def start_review() -> Any:
        services = g.course
        question_repository = services.question_repository
        source_ids = _view.single_catalogue_filter(
            request.form.get("source_id", "").strip(),
            {source.id for source in question_repository.get_sources()},
            "课件",
        )
        chapter_ids = _view.single_catalogue_filter(
            request.form.get("chapter_id", "").strip(),
            {chapter.id for chapter in question_repository.get_chapters()},
            "章节",
        )
        question_ids = _begin(
            QuizMode.REVIEW, chapter_ids=chapter_ids, source_ids=source_ids
        )
        if not question_ids:
            state = g.quiz_progress.get(_progress_state.session_key(QuizMode.REVIEW))
            if state and state.get("review_shortages"):
                return redirect(url_for("web.review", course_id=g.course_id))
            flash("当前筛选范围内没有待纠正或待强化内容。", "info")
            g.quiz_progress.pop(_progress_state.session_key(QuizMode.REVIEW), None)
            return redirect(
                url_for(
                    "web.mistakes",
                    course_id=g.course_id,
                    source=next(iter(source_ids), "") if source_ids else "",
                    chapter=next(iter(chapter_ids), "") if chapter_ids else "",
                )
            )
        return redirect(url_for("web.review", course_id=g.course_id))

    @course_route("/review")
    @shared_progress
    def review() -> Any:
        return _render_quiz(QuizMode.REVIEW)

    @course_route("/review/answer", methods=("POST",))
    @shared_progress
    def answer_review() -> Any:
        return _answer(QuizMode.REVIEW)

    @course_route("/review/next", methods=("POST",))
    @shared_progress
    def next_review() -> Any:
        return _next(QuizMode.REVIEW)


    # ------------------------------------------------------------ mock exam

    @course_route("/exam")
    @shared_progress
    def exam_setup() -> str:
        """Show this course's exam configuration, resume prompt, and history."""
        services = g.course
        exam_service = services.exam_service
        exam_service.finalize_expired_for_learner(g.learner_id)
        return render_template(
            "exam_setup.html",
            active_exam=exam_service.get_active_session(g.learner_id),
            history=exam_service.list_sessions(g.learner_id),
            count_options=exam_service.question_count_options(
                len(services.question_repository.get_all())
            ),
            time_options=exam_service.time_limit_options(),
        )

    @course_route("/exam/start", methods=("POST",))
    @shared_progress
    def start_exam() -> Any:
        raw_count = request.form.get("question_count", "")
        raw_limit = request.form.get("time_limit", "")
        try:
            question_count = int(raw_count)
            time_limit = None if raw_limit == "none" else int(raw_limit)
        except ValueError:
            flash("考试配置无效，请重新选择。", "error")
            return redirect(url_for("web.exam_setup", course_id=g.course_id))
        try:
            exam_session = g.course.exam_service.create_exam(
                g.learner_id,
                question_count=question_count,
                time_limit_seconds=time_limit,
            )
        except ExamConfigError as exc:
            flash(str(exc), "error")
            return redirect(url_for("web.exam_setup", course_id=g.course_id))
        return _to_exam(exam_session.id)

    @course_route("/exam/<exam_id>")
    @shared_progress
    def exam(exam_id: str) -> Any:
        """Render one question of an unfinished exam without feedback.

        An exam ID that belongs to another course is rejected instead of being
        reinterpreted against this course's content.
        """
        services = g.course
        exam_service = services.exam_service
        _require_exam_course(exam_id)
        try:
            exam_session = exam_service.get_session(g.learner_id, exam_id)
        except ExamNotFoundError:
            abort(404)
        if exam_session.status.finished:
            return _to_exam_report(exam_id)
        if exam_service.finalize_if_expired(exam_session):
            flash("考试时间已结束，系统已自动交卷。", "info")
            return _to_exam_report(exam_id)
        if exam_service.reconcile_session(exam_session):
            # Slots were dropped silently; the exam may even have finished.
            exam_session = exam_service.get_session(g.learner_id, exam_id)
            if exam_session.status.finished:
                return _to_exam_report(exam_id)
        position = _exam_position(request.args.get("q"), exam_session)
        slots = exam_service.get_questions(exam_session)
        slot = slots[position]
        question = services.question_repository.get_by_id(slot.question_id)
        if question is None:
            # Unreachable: reconciliation above guarantees every slot's
            # question exists with an unchanged grading identity.
            abort(410, description="题库已经更新，当前题目不再存在。")
        return render_template(
            "exam.html",
            exam_session=exam_session,
            position=position,
            total=len(slots),
            answered_count=sum(bool(item.selected_answers) for item in slots),
            question=question,
            ordered_options=services.quiz_service.order_options(
                question, exam_session.option_seed, position
            ),
            saved_answers=slot.selected_answers,
            remaining_seconds=exam_service.remaining_seconds(
                exam_session, _srs.utc_now()
            ),
            source=services.question_repository.get_source(question.source_id),
            chapters=_chapters_of(question),
            slot_question_id=slot.question_id,
        )


    @course_route("/exam/<exam_id>/answer", methods=("POST",))
    @shared_progress
    def answer_exam(exam_id: str) -> Any:
        """Persist one exam answer without revealing correctness.

        The posted ``position`` is re-confirmed against the slot's recorded
        ``question_id`` inside the transaction, so a page rendered before a slot
        reorder can never write the answer onto a different question.
        """
        exam_service = g.course.exam_service
        _require_exam_course(exam_id)
        try:
            exam_session = exam_service.get_session(g.learner_id, exam_id)
        except ExamNotFoundError:
            abort(404)
        if exam_service.reconcile_session(exam_session):
            # Slots shifted or the exam finished, so the posted position no
            # longer refers to the same question; drop this submission and
            # let the form reload at the reconciled layout.
            return _to_exam(exam_id)
        try:
            position = int(request.form.get("position", "-1"))
        except ValueError:
            abort(400, description="提交的数据无效，请返回后重试。")
        posted_question_id = request.form.get("question_id", "")
        slot = next(
            (
                item
                for item in exam_service.get_questions(exam_session)
                if item.position == position
            ),
            None,
        )
        if slot is None or (
            posted_question_id and posted_question_id != slot.question_id
        ):
            flash("页面已经过期，请刷新后重新作答。", "info")
            return _to_exam(exam_id)
        try:
            exam_service.save_answer(
                g.learner_id,
                exam_id,
                position,
                request.form.getlist("answers"),
            )
        except ExamStateError as exc:
            flash(str(exc), "info")
            return _to_exam_report(exam_id)
        except ExamConfigError as exc:
            abort(400, description=str(exc))
        except AnswerValidationError:
            abort(400, description="提交的答案不属于当前题目，请重新作答。")
        except ExamNotFoundError:
            abort(404)
        goto = request.form.get("goto", "next")
        if goto == "prev":
            target = max(0, position - 1)
        elif goto == "stay":
            target = position
        else:
            target = min(position + 1, exam_session.question_count - 1)
        return _to_exam(exam_id, position=target)

    @course_route("/exam/<exam_id>/submit", methods=("POST",))
    @shared_progress
    def submit_exam(exam_id: str) -> Any:
        """Finalize an exam; repeated submits keep the first result."""
        _require_exam_course(exam_id)
        exam_service = g.course.exam_service
        try:
            previous = exam_service.get_session(g.learner_id, exam_id)
            exam_session = exam_service.submit(g.learner_id, exam_id)
        except ExamNotFoundError:
            abort(404)
        if not previous.status.finished:
            if exam_session.status is ExamStatus.EXPIRED:
                flash("考试时间已到，已自动交卷。", "info")
            else:
                flash("交卷成功，已生成成绩报告。", "success")
        return _to_exam_report(exam_id)

    @course_route("/exam/<exam_id>/report")
    @shared_progress
    def exam_report(exam_id: str) -> Any:
        """Show the finalized score report with chapter breakdown.

        The report is rendered against the *exam's own course* content: the
        course here always comes from the URL, which ``_require_exam_course``
        has just confirmed is the exam's real owner.  Keeping this a live-content
        read is deliberate — see ``docs/ARCHITECTURE.md``: an old exam does not
        retroactively gain a full snapshot of the old question content.
        """
        services = g.course
        _require_exam_course(exam_id)
        try:
            report = services.exam_service.get_report(g.learner_id, exam_id)
        except ExamNotFoundError:
            abort(404)
        except ExamStateError:
            return _to_exam(exam_id)
        wrong_details = [
            {
                "item": item,
                "ordered_options": services.quiz_service.order_options(
                    item.question, report.session.option_seed, item.position
                ),
                "source": services.question_repository.get_source(
                    item.question.source_id
                ),
                "chapters": _chapters_of(item.question),
            }
            for item in report.wrong_items
        ]
        return render_template(
            "exam_report.html",
            report=report,
            wrong_details=wrong_details,
        )

    def _require_exam_course(exam_id: str) -> None:
        """Reject an exam ID that this course does not own."""
        owner = cross_course.find_course_for_exam(exam_id)
        if owner is not None and owner != g.course_id:
            abort(409, description=CROSS_COURSE_EXAM_MESSAGE)

    def _to_exam(exam_id: str, *, position: int | None = None) -> Any:
        """Send the learner to one page of a still-unfinished exam."""

        query = {} if position is None else {"q": position}
        return redirect(
            url_for("web.exam", course_id=g.course_id, exam_id=exam_id, **query)
        )

    def _to_exam_report(exam_id: str) -> Any:
        """Send the learner to the score report of a finished exam."""

        return redirect(
            url_for("web.exam_report", course_id=g.course_id, exam_id=exam_id)
        )


    # ------------------------------------------------------------ practice helpers

    def _exam_position(raw: str | None, exam_session: ExamSession) -> int:
        """Resolve the requested exam page, defaulting to the saved spot."""
        if raw is None or raw == "":
            return min(
                exam_session.current_position, exam_session.question_count - 1
            )
        try:
            position = int(raw)
        except ValueError:
            abort(400, description="考试页码无效。")
        if not 0 <= position < exam_session.question_count:
            abort(400, description="考试页码超出本场考试范围。")
        return position

    def _begin(
        mode: QuizMode,
        limit: int | None = None,
        requested_size: str = "all",
        chapter_ids: set[str] | None = None,
        source_ids: set[str] | None = None,
    ) -> list[str]:
        quiz_service = g.course.quiz_service
        previous = g.quiz_progress.get(_progress_state.session_key(mode))
        review_items: list[dict[str, str | None]] = []
        shortages: list[dict[str, object]] = []
        selection = None
        if mode is QuizMode.NORMAL:
            selection = quiz_service.start_normal(
                limit,
                chapter_ids=chapter_ids,
                source_ids=source_ids,
                fairness_scope=(
                    previous.get("fairness_scope")
                    if isinstance(previous, dict)
                    else None
                ),
                fairness_remaining_ids=(
                    previous.get("fairness_remaining_ids")
                    if isinstance(previous, dict)
                    else None
                ),
            )
            question_ids = list(selection.question_ids)
        else:
            review_selection = quiz_service.start_review(
                g.learner_id,
                chapter_ids=chapter_ids,
                source_ids=source_ids,
            )
            review_items = [item.to_dict() for item in review_selection.items]
            question_ids = [item["question_id"] for item in review_items]
            shortages = list(review_selection.shortages)

        state = {
            "mode": mode.value,
            "question_ids": question_ids,
            "question_results": [None] * len(question_ids),
            "current_index": 0,
            "correct_count": 0,
            "incorrect_count": 0,
            "status": "pending",
            "answer_token": secrets.token_urlsafe(24),
            "option_seed": secrets.token_hex(16),
            "requested_size": requested_size,
            "initial_question_count": len(question_ids),
            "chapter_ids": sorted(chapter_ids) if chapter_ids is not None else [],
            "source_ids": sorted(source_ids) if source_ids is not None else [],
        }
        if mode is QuizMode.NORMAL and selection is not None:
            state.update(
                {
                    "fairness_scope": selection.fairness_scope,
                    "fairness_remaining_ids": list(selection.fairness_remaining_ids),
                }
            )
        else:
            state.update(
                {
                    "review_items": review_items,
                    "corrected_count": 0,
                    "knowledge_completed_count": 0,
                    "review_shortages": shortages,
                }
            )
        g.quiz_progress[_progress_state.session_key(mode)] = state
        return question_ids

    def _render_quiz(mode: QuizMode) -> Any:
        services = g.course
        question_repository = services.question_repository
        state = _state_for(mode)
        if state is None:
            flash("当前没有可继续的练习，请重新开始。", "info")
            destination = "web.home" if mode is QuizMode.NORMAL else "web.mistakes"
            return redirect(url_for(destination, course_id=g.course_id))

        question_ids = state["question_ids"]
        current_index = state["current_index"]
        is_complete = current_index >= len(question_ids)
        question = None
        review_item = None
        ordered_options = []
        if not is_complete:
            question = question_repository.get_by_id(question_ids[current_index])
            if question is None:
                # Defensive: reconciliation removes unusable questions from
                # every round, so a missing question can only come from a
                # state written before startup reconciliation ran.
                state, _reconciled = _progress_state.reconcile_state(
                    state, mode, is_usable=question_repository.has
                )
                g.quiz_progress[_progress_state.session_key(mode)] = state
                return redirect(
                    url_for(_mode_endpoints(mode)["page"], course_id=g.course_id)
                )
            ordered_options = services.quiz_service.order_options(
                question, state["option_seed"], current_index
            )
            if mode is QuizMode.REVIEW:
                review_item = state["review_items"][current_index]

        endpoints = _mode_endpoints(mode)
        attempts = state["correct_count"] + state["incorrect_count"]
        accuracy = round(state["correct_count"] / attempts * 100) if attempts else 0
        return render_template(
            "quiz.html",
            mode=mode,
            state=state,
            question=question,
            ordered_options=ordered_options,
            is_complete=is_complete,
            total=len(question_ids),
            accuracy=accuracy,
            answer_endpoint=endpoints["answer"],
            next_endpoint=endpoints["next"],
            review_item=review_item,
            source=(
                question_repository.get_source(question.source_id)
                if question
                else None
            ),
            chapters=_chapters_of(question) if question else [],
        )


    def _answer(mode: QuizMode) -> Any:
        services = g.course
        question_repository = services.question_repository
        state = _state_for(mode)
        if state is None:
            abort(409, description="本轮练习已经失效，请重新开始。")
        endpoint = _mode_endpoints(mode)["page"]

        if state["status"] == "answered":
            return redirect(url_for(endpoint, course_id=g.course_id))
        if state["status"] != "pending":
            abort(409, description="当前题目暂时不能提交，请重新进入练习。")

        _require_current_answer_token(state)

        current_index = state["current_index"]
        question_ids = state["question_ids"]
        if current_index >= len(question_ids):
            return redirect(url_for(endpoint, course_id=g.course_id))
        selected_answers = request.form.getlist("answers")
        if not selected_answers:
            flash("请至少选择一个答案后再提交。", "error")
            return redirect(url_for(endpoint, course_id=g.course_id))

        question_id = question_ids[current_index]
        review_item = (
            state["review_items"][current_index]
            if mode is QuizMode.REVIEW
            else None
        )
        try:
            result = services.quiz_service.answer(
                learner_id=g.learner_id,
                question_id=question_id,
                mode=mode,
                selected_answers=selected_answers,
            )
        except AnswerValidationError:
            abort(400, description="提交的答案不属于当前题目，请重新作答。")
        except LookupError:
            # Defensive: reconciliation should have removed this question
            # from the queue already; drop it silently and carry on.
            state, _reconciled = _progress_state.reconcile_state(
                state, mode, is_usable=question_repository.has
            )
            g.quiz_progress[_progress_state.session_key(mode)] = state
            return redirect(url_for(endpoint, course_id=g.course_id))

        _record_answer_outcome(state, mode, result, review_item)
        return redirect(url_for(endpoint, course_id=g.course_id))

    def _record_answer_outcome(
        state: dict[str, Any],
        mode: QuizMode,
        result: AnswerResult,
        review_item: dict[str, Any] | None,
    ) -> None:
        """Store one graded answer's outcome in the resumable round state.

        The counters, the per-slot entry and the temporary ``feedback``
        payload are exactly what a refreshed page renders, so they are written
        here and nowhere else.
        """
        question_ids = state["question_ids"]
        state["status"] = "answered"
        counter = "correct_count" if result.is_correct else "incorrect_count"
        state[counter] += 1
        feedback = {
            "is_correct": result.is_correct,
            "selected_answers": list(result.selected_answers),
        }
        updates: list[dict[str, Any]] = []
        if mode is QuizMode.REVIEW:
            updates = _knowledge_update_feedback(result)
            feedback.update(
                {
                    "review_role": review_item["role"],
                    "target_chapter_id": review_item.get("chapter_id"),
                    "became_wrong_question": (
                        result.learning_update.became_wrong_question
                    ),
                    "corrected_now": result.learning_update.corrected_now,
                    "knowledge_updates": updates,
                }
            )
            if result.learning_update.corrected_now:
                state["corrected_count"] += 1
            state["knowledge_completed_count"] += sum(
                update["newly_completed"] for update in updates
            )
        entry = {"correct": bool(result.is_correct)}
        if mode is QuizMode.REVIEW:
            entry["corrected"] = bool(result.learning_update.corrected_now)
            entry["knowledge"] = sum(update["newly_completed"] for update in updates)
        results = state.get("question_results")
        if not isinstance(results, list) or len(results) != len(question_ids):
            results = [None] * len(question_ids)
        results[state["current_index"]] = entry
        state["question_results"] = results
        state["feedback"] = feedback
        g.quiz_progress[_progress_state.session_key(mode)] = state


    def _next(mode: QuizMode) -> Any:
        state = _state_for(mode)
        if state is None:
            abort(409, description="本轮练习已经失效，请重新开始。")
        endpoint = _mode_endpoints(mode)["page"]
        if state["status"] != "answered":
            return redirect(url_for(endpoint, course_id=g.course_id))

        _require_current_answer_token(state)

        state["current_index"] += 1
        if (
            mode is QuizMode.REVIEW
            and state["current_index"] >= len(state["question_ids"])
        ):
            previous_question_id = state["question_ids"][state["current_index"] - 1]
            selection = g.course.quiz_service.next_review_item(
                g.learner_id,
                chapter_ids=set(state["chapter_ids"]) or None,
                source_ids=set(state["source_ids"]) or None,
                avoid_question_ids={previous_question_id},
            )
            for item in selection.items:
                state["question_ids"].append(item.question_id)
                state["review_items"].append(item.to_dict())
                if isinstance(state.get("question_results"), list):
                    state["question_results"].append(None)
            state["review_shortages"] = list(selection.shortages)
        state["status"] = "pending"
        state["answer_token"] = secrets.token_urlsafe(24)
        state.pop("feedback", None)
        g.quiz_progress[_progress_state.session_key(mode)] = state
        return redirect(url_for(endpoint, course_id=g.course_id))

    def _state_for(mode: QuizMode) -> dict[str, Any] | None:
        return _progress_state.valid_state_for(g.quiz_progress, mode)

    def _require_current_answer_token(state: dict[str, Any]) -> None:
        """Reject a form that does not carry this question's current token.

        The token is rotated on every advance, so a stale tab or a replayed
        form can neither answer nor advance a later question.
        """
        submitted = request.form.get("answer_token", "")
        if not _web_auth.tokens_match(submitted, state.get("answer_token", "")):
            abort(400, description="答题页面已经过期，请返回后重新进入。")

    def _active_progress(mode: QuizMode) -> dict[str, Any] | None:
        """Return a small resume summary for an unfinished mode."""
        return _progress_state.active_summary(g.quiz_progress, mode)

    def _curriculum() -> list[dict[str, Any]]:
        """Build one dynamic catalogue for selection and filtering templates."""
        return _view.build_curriculum(g.course.question_repository)

    def _chapters_of(question: Question) -> list[Chapter | None]:
        """Return the chapter records one question belongs to, in bank order."""
        question_repository = g.course.question_repository
        return [
            question_repository.get_chapter(chapter_id)
            for chapter_id in question.chapter_ids
        ]

    def _knowledge_update_feedback(result: AnswerResult) -> list[dict[str, Any]]:
        """Render chapter reinforcement updates for the review feedback card."""
        services = g.course
        updates = []
        for update in result.learning_update.knowledge_updates:
            chapter = services.question_repository.get_chapter(update.point.chapter_id)
            updates.append(
                {
                    "chapter_id": update.point.chapter_id,
                    "chapter_title": (
                        chapter.title if chapter else update.point.chapter_id
                    ),
                    "verified_count": len(update.point.verified_question_ids),
                    "verification_target": (
                        services.weak_knowledge_point_service.verification_target
                    ),
                    "active": update.point.active,
                    "newly_completed": update.newly_completed,
                }
            )
        return updates

    def _mode_endpoints(mode: QuizMode) -> dict[str, str]:
        if mode is QuizMode.NORMAL:
            return {
                "page": "web.quiz",
                "answer": "web.answer_quiz",
                "next": "web.next_quiz",
            }
        return {
            "page": "web.review",
            "answer": "web.answer_review",
            "next": "web.next_review",
        }

    @blueprint.app_errorhandler(HTTPException)
    def friendly_http_error(error: HTTPException) -> Any:
        messages = {
            400: "提交的数据无效，请返回后重试。",
            404: "没有找到你要访问的页面。",
            405: "当前页面不支持这种操作方式。",
            409: "当前练习状态已经失效，请刷新页面后重新开始。",
            410: "这道题已经不在当前题库中。",
            500: "服务暂时出现问题，请返回首页后重试。",
            503: STALE_BANK_MESSAGE,
        }
        if error.code in {404, 405, 500}:
            description = messages[error.code]
        else:
            description = (
                str(error.description)
                if error.description
                else messages.get(error.code)
            )
        course = getattr(g, "course", None)
        response = make_response(
            render_template(
                "error.html",
                error_code=error.code or 500,
                error_message=(
                    description or messages.get(error.code, "页面暂时无法访问。")
                ),
                error_course_id=getattr(g, "course_id", None),
                error_course=course.course if course is not None else None,
                course_states=course_registry.states(),
                retry_after_seconds=(
                    STALE_BANK_RETRY_AFTER_SECONDS if error.code == 503 else None
                ),
            ),
            error.code or 500,
        )
        if error.code == 503:
            # Ask well-behaved clients to back off while the service is being
            # restarted, without promising when that restart finishes.
            response.headers["Retry-After"] = str(STALE_BANK_RETRY_AFTER_SECONDS)
        return response

    def _install_course_aware_url_for(state) -> None:
        """Install the course-aware template ``url_for`` on the real app."""
        _course_ctx.install_course_aware_url_for(state.app, scoped_endpoints)

    blueprint.record_once(_install_course_aware_url_for)
    return blueprint

