"""Web routes for accounts, normal practice, and personal mistake review."""

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
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.exceptions import HTTPException

from app.models import Chapter, ExamSession, ExamStatus, Question, QuizMode
from app.repositories import (
    GlossaryRepository,
    QuestionRepository,
    ProgressRepository,
    RateLimitRepository,
    UserRepository,
    UsernameAlreadyExistsError,
)
from app.services import (
    AnswerResult,
    AnswerValidationError,
    ExamConfigError,
    ExamNotFoundError,
    ExamService,
    ExamStateError,
    QuizService,
    StatisticsService,
    WeakKnowledgePointService,
    WrongQuestionService,
)
from app.services import local_time as _local_time
from app.services import progress_state as _progress_state
from app.services import srs_service as _srs
from app.web import auth as _web_auth
from app.web import view_helpers as _view


def create_web_blueprint(
    question_repository: QuestionRepository,
    glossary_repository: GlossaryRepository,
    user_repository: UserRepository,
    quiz_service: QuizService,
    wrong_question_service: WrongQuestionService,
    question_bank_version: str,
    progress_repository: ProgressRepository,
    rate_limit_repository: RateLimitRepository,
    weak_knowledge_point_service: WeakKnowledgePointService,
    exam_service: ExamService,
    statistics_service: StatisticsService,
    display_timezone: tzinfo,
    bank_generation: int = 0,
) -> Blueprint:
    """Build the learner-facing web blueprint."""
    blueprint = Blueprint("web", __name__)
    public_endpoints = {"web.login", "web.register"}
    glossary_data = glossary_repository.to_dict()

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

    @blueprint.app_context_processor
    def inject_global_page_data() -> dict[str, Any]:
        return {
            "bank_title": question_repository.title,
            "bank_title_zh": question_repository.title_zh,
            "glossary_data": glossary_data,
            "option_label": _view.option_label,
            "csrf_token": _web_auth.csrf_token,
            "format_duration": _view.format_duration,
            "format_datetime": partial(
                _view.format_datetime, zone=display_timezone
            ),
            "display_tz_label": _local_time.timezone_label(display_timezone),
        }

    @blueprint.before_request
    def require_account() -> Any:
        """Load the signed-in user or send the browser to the login page."""
        if request.method == "POST":
            if request.content_length and request.content_length > 64 * 1024:
                abort(413)
            if current_app.config["ENABLE_CSRF"]:
                _web_auth.validate_csrf()
        if request.endpoint in public_endpoints:
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

    def shared_progress(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            # One transaction includes token checks, attempts, learning state and progress.
            # SQLite coordinates concurrent requests even across Gunicorn workers.
            with progress_repository.database.transaction() as connection:
                active_bank = connection.execute(
                    "SELECT bank_version, generation FROM question_bank_state WHERE id = 1"
                ).fetchone()
                if (active_bank["bank_version"], active_bank["generation"]) != (
                    question_bank_version, bank_generation
                ):
                    abort(503, description="题库已更新，请重启服务后刷新页面。")
                if bank_generation and session.get("bank_generation") != bank_generation:
                    flash("检测到题库更新，所有课程学习记录已清空，账号已保留。", "info")
                session["bank_generation"] = bank_generation
                g.quiz_progress = {}
                changed = False
                for mode in _progress_state.PRACTICE_MODES:
                    stored = progress_repository.get(g.learner_id, mode)
                    legacy = session.pop(_progress_state.session_key(mode), None)
                    if stored is None:
                        state = (
                            legacy
                            if bank_generation == 0 and session.get("question_bank_version") == question_bank_version
                            else None
                        )
                    else:
                        version, state = stored
                        if version != question_bank_version:
                            changed = changed or state is not None
                            state = None
                    if not _progress_state.is_valid_progress_state(state, mode):
                        state = None
                    g.quiz_progress[_progress_state.session_key(mode)] = state
                session.pop("quiz_progress", None)
                session.pop("question_bank_version", None)
                if changed:
                    flash("检测到题库更新，未完成的练习进度已重置。", "info")
                response = view(*args, **kwargs)
                for mode in _progress_state.PRACTICE_MODES:
                    state = g.quiz_progress.get(_progress_state.session_key(mode))
                    stored = progress_repository.get(g.learner_id, mode)
                    if (stored is not None or state is not None) and stored != (
                        question_bank_version, state
                    ):
                        progress_repository.save(
                            g.learner_id, mode, question_bank_version, state
                        )
                return response
        return wrapped

    @blueprint.after_request
    def prevent_stale_progress_cache(response):
        if hasattr(g, "learner_id"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @blueprint.route("/login", methods=["GET", "POST"])
    def login() -> Any:
        existing_id = session.get("user_id")
        if request.method == "GET" and isinstance(existing_id, str):
            if user_repository.get_by_id(existing_id) is not None:
                return redirect(url_for("web.home"))
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
                return redirect(url_for("web.home"))
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
                    return redirect(url_for("web.home"))
        return render_template("auth.html", page="register")

    @blueprint.post("/logout")
    @shared_progress
    def logout() -> Any:
        session.clear()
        flash("你已安全退出。", "success")
        return redirect(url_for("web.login"))

    @blueprint.get("/")
    @shared_progress
    def home() -> str:
        exam_service.finalize_expired_for_learner(g.learner_id)
        stats = wrong_question_service.get_stats(g.learner_id)
        return render_template(
            "home.html",
            question_count=len(question_repository.get_all()),
            stats=stats,
            srs_due_count=wrong_question_service.get_due_srs_count(g.learner_id),
            normal_progress=_active_progress(QuizMode.NORMAL),
            review_progress=_active_progress(QuizMode.REVIEW),
            active_exam=exam_service.get_active_session(g.learner_id),
        )

    @blueprint.get("/dashboard")
    @shared_progress
    def dashboard() -> str:
        """Show the learner's aggregated learning statistics."""
        exam_service.finalize_expired_for_learner(g.learner_id)
        return render_template(
            "dashboard.html",
            dashboard=statistics_service.build_dashboard(g.learner_id),
        )

    @blueprint.get("/quiz/setup")
    @shared_progress
    def quiz_setup() -> str:
        """Show the dynamic course-material and chapter selection screen."""
        return render_template(
            "quiz_setup.html",
            curriculum=_curriculum(),
            question_count=len(question_repository.get_all()),
            normal_progress=_active_progress(QuizMode.NORMAL),
        )

    @blueprint.get("/glossary")
    def glossary() -> str:
        """Show the active course's data-driven terminology learning page."""
        return render_template(
            "glossary.html",
            glossary=glossary_repository,
            terms=glossary_repository.all_terms(),
            categories=glossary_repository.categories(),
        )

    @blueprint.post("/quiz/start")
    @shared_progress
    def start_quiz() -> Any:
        raw_size = request.form.get("quiz_size", "20")
        limit = _progress_state.quiz_limit(raw_size)
        chapter_ids = _view.catalogue_filter(
            request.form.getlist("chapter_ids"),
            {chapter.id for chapter in question_repository.get_chapters()},
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
            return redirect(url_for("web.quiz_setup"))
        return redirect(url_for("web.quiz"))

    @blueprint.get("/quiz")
    @shared_progress
    def quiz() -> Any:
        return _render_quiz(QuizMode.NORMAL)

    @blueprint.post("/quiz/answer")
    @shared_progress
    def answer_quiz() -> Any:
        return _answer(QuizMode.NORMAL)

    @blueprint.post("/quiz/next")
    @shared_progress
    def next_quiz() -> Any:
        return _next(QuizMode.NORMAL)

    @blueprint.get("/mistakes")
    @shared_progress
    def mistakes() -> str:
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

    @blueprint.post("/mistakes/reset")
    @shared_progress
    def reset_mistakes() -> Any:
        deleted_count = wrong_question_service.reset(g.learner_id)
        progress_repository.save(g.learner_id, QuizMode.REVIEW, question_bank_version, None)
        g.quiz_progress.pop(_progress_state.session_key(QuizMode.REVIEW), None)
        if deleted_count:
            flash(f"已将 {deleted_count} 道错题重置为 0。", "success")
        else:
            flash("错题数已经是 0。", "info")
        return redirect(url_for("web.mistakes"))

    @blueprint.post("/review/start")
    @shared_progress
    def start_review() -> Any:
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
                return redirect(url_for("web.review"))
            else:
                flash("当前筛选范围内没有待纠正或待强化内容。", "info")
                g.quiz_progress.pop(_progress_state.session_key(QuizMode.REVIEW), None)
                return redirect(
                    url_for(
                        "web.mistakes",
                        source=next(iter(source_ids), "") if source_ids else "",
                        chapter=next(iter(chapter_ids), "") if chapter_ids else "",
                    )
                )
        return redirect(url_for("web.review"))

    @blueprint.get("/review")
    @shared_progress
    def review() -> Any:
        return _render_quiz(QuizMode.REVIEW)

    @blueprint.post("/review/answer")
    @shared_progress
    def answer_review() -> Any:
        return _answer(QuizMode.REVIEW)

    @blueprint.post("/review/next")
    @shared_progress
    def next_review() -> Any:
        return _next(QuizMode.REVIEW)

    @blueprint.get("/exam")
    @shared_progress
    def exam_setup() -> str:
        """Show the mock-exam configuration, resume prompt, and history."""
        exam_service.finalize_expired_for_learner(g.learner_id)
        return render_template(
            "exam_setup.html",
            active_exam=exam_service.get_active_session(g.learner_id),
            history=exam_service.list_sessions(g.learner_id),
            count_options=exam_service.question_count_options(
                len(question_repository.get_all())
            ),
            time_options=exam_service.time_limit_options(),
        )

    @blueprint.post("/exam/start")
    @shared_progress
    def start_exam() -> Any:
        raw_count = request.form.get("question_count", "")
        raw_limit = request.form.get("time_limit", "")
        try:
            question_count = int(raw_count)
            time_limit = None if raw_limit == "none" else int(raw_limit)
        except ValueError:
            flash("考试配置无效，请重新选择。", "error")
            return redirect(url_for("web.exam_setup"))
        try:
            exam_session = exam_service.create_exam(
                g.learner_id,
                question_count=question_count,
                time_limit_seconds=time_limit,
            )
        except ExamConfigError as exc:
            flash(str(exc), "error")
            return redirect(url_for("web.exam_setup"))
        return redirect(url_for("web.exam", exam_id=exam_session.id))

    @blueprint.get("/exam/<exam_id>")
    @shared_progress
    def exam(exam_id: str) -> Any:
        """Render one question of an unfinished exam without feedback."""
        try:
            exam_session = exam_service.get_session(g.learner_id, exam_id)
        except ExamNotFoundError:
            abort(404)
        if exam_session.status.finished:
            return redirect(url_for("web.exam_report", exam_id=exam_id))
        if exam_service.finalize_if_expired(exam_session):
            flash("考试时间已结束，系统已自动交卷。", "info")
            return redirect(url_for("web.exam_report", exam_id=exam_id))
        position = _exam_position(request.args.get("q"), exam_session)
        slots = exam_service.get_questions(exam_session)
        slot = slots[position]
        question = question_repository.get_by_id(slot.question_id)
        if question is None:
            abort(410, description="题库已经更新，当前题目不再存在。")
        return render_template(
            "exam.html",
            exam_session=exam_session,
            position=position,
            total=len(slots),
            answered_count=sum(bool(item.selected_answers) for item in slots),
            question=question,
            ordered_options=quiz_service.order_options(
                question, exam_session.option_seed, position
            ),
            saved_answers=slot.selected_answers,
            remaining_seconds=exam_service.remaining_seconds(
                exam_session, _srs.utc_now()
            ),
            source=question_repository.get_source(question.source_id),
            chapters=_chapters_of(question),
        )

    @blueprint.post("/exam/<exam_id>/answer")
    @shared_progress
    def answer_exam(exam_id: str) -> Any:
        """Persist one exam answer without revealing correctness."""
        try:
            exam_session = exam_service.get_session(g.learner_id, exam_id)
        except ExamNotFoundError:
            abort(404)
        try:
            position = int(request.form.get("position", "-1"))
        except ValueError:
            abort(400, description="提交的数据无效，请返回后重试。")
        try:
            exam_service.save_answer(
                g.learner_id,
                exam_id,
                position,
                request.form.getlist("answers"),
            )
        except ExamStateError as exc:
            flash(str(exc), "info")
            return redirect(url_for("web.exam_report", exam_id=exam_id))
        except ExamConfigError as exc:
            abort(400, description=str(exc))
        except AnswerValidationError:
            abort(400, description="提交的答案不属于当前题目，请重新作答。")
        goto = request.form.get("goto", "next")
        if goto == "prev":
            target = max(0, position - 1)
        elif goto == "stay":
            target = position
        else:
            target = min(position + 1, exam_session.question_count - 1)
        return redirect(url_for("web.exam", exam_id=exam_id, q=target))

    @blueprint.post("/exam/<exam_id>/submit")
    @shared_progress
    def submit_exam(exam_id: str) -> Any:
        """Finalize an exam; repeated submits keep the first result."""
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
        return redirect(url_for("web.exam_report", exam_id=exam_id))

    @blueprint.get("/exam/<exam_id>/report")
    @shared_progress
    def exam_report(exam_id: str) -> Any:
        """Show the finalized score report with chapter breakdown."""
        try:
            report = exam_service.get_report(g.learner_id, exam_id)
        except ExamNotFoundError:
            abort(404)
        except ExamStateError:
            return redirect(url_for("web.exam", exam_id=exam_id))
        wrong_details = [
            {
                "item": item,
                "ordered_options": quiz_service.order_options(
                    item.question, report.session.option_seed, item.position
                ),
                "source": question_repository.get_source(item.question.source_id),
                "chapters": _chapters_of(item.question),
            }
            for item in report.wrong_items
        ]
        return render_template(
            "exam_report.html",
            report=report,
            wrong_details=wrong_details,
        )

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
        previous = g.quiz_progress.get(_progress_state.session_key(mode))
        review_items: list[dict[str, str | None]] = []
        shortages: list[dict[str, object]] = []
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
        if mode is QuizMode.NORMAL:
            state.update(
                {
                    "fairness_scope": selection.fairness_scope,
                    "fairness_remaining_ids": list(
                        selection.fairness_remaining_ids
                    ),
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
        state = _state_for(mode)
        if state is None:
            flash("当前没有可继续的练习，请重新开始。", "info")
            destination = "web.home" if mode is QuizMode.NORMAL else "web.mistakes"
            return redirect(url_for(destination))

        question_ids = state["question_ids"]
        current_index = state["current_index"]
        is_complete = current_index >= len(question_ids)
        question = None
        review_item = None
        ordered_options = []
        if not is_complete:
            question = question_repository.get_by_id(question_ids[current_index])
            if question is None:
                abort(410, description="题库已经更新，当前题目不再存在。")
            ordered_options = quiz_service.order_options(
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
            source=(question_repository.get_source(question.source_id) if question else None),
            chapters=_chapters_of(question) if question else [],
        )

    def _answer(mode: QuizMode) -> Any:
        state = _state_for(mode)
        if state is None:
            abort(409, description="本轮练习已经失效，请重新开始。")
        endpoint = _mode_endpoints(mode)["page"]

        if state["status"] == "answered":
            return redirect(url_for(endpoint))
        if state["status"] != "pending":
            abort(409, description="当前题目暂时不能提交，请重新进入练习。")

        submitted_token = request.form.get("answer_token", "")
        expected_token = state.get("answer_token", "")
        if not submitted_token or not secrets.compare_digest(
            submitted_token, expected_token
        ):
            abort(400, description="答题页面已经过期，请返回后重新进入。")

        current_index = state["current_index"]
        question_ids = state["question_ids"]
        if current_index >= len(question_ids):
            return redirect(url_for(endpoint))
        selected_answers = request.form.getlist("answers")
        if not selected_answers:
            flash("请至少选择一个答案后再提交。", "error")
            return redirect(url_for(endpoint))

        question_id = question_ids[current_index]
        review_item = (
            state["review_items"][current_index]
            if mode is QuizMode.REVIEW
            else None
        )
        try:
            result = quiz_service.answer(
                learner_id=g.learner_id,
                question_id=question_id,
                mode=mode,
                selected_answers=selected_answers,
            )
        except AnswerValidationError:
            abort(400, description="提交的答案不属于当前题目，请重新作答。")

        state["status"] = "answered"
        counter = "correct_count" if result.is_correct else "incorrect_count"
        state[counter] += 1
        feedback = {
            "is_correct": result.is_correct,
            "selected_answers": list(result.selected_answers),
        }
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
        state["feedback"] = feedback
        g.quiz_progress[_progress_state.session_key(mode)] = state
        return redirect(url_for(endpoint))

    def _next(mode: QuizMode) -> Any:
        state = _state_for(mode)
        if state is None:
            abort(409, description="本轮练习已经失效，请重新开始。")
        endpoint = _mode_endpoints(mode)["page"]
        if state["status"] != "answered":
            return redirect(url_for(endpoint))

        submitted_token = request.form.get("answer_token", "")
        if not submitted_token or not secrets.compare_digest(
            submitted_token, state["answer_token"]
        ):
            abort(400, description="答题页面已经过期，请返回后重新进入。")

        state["current_index"] += 1
        if (
            mode is QuizMode.REVIEW
            and state["current_index"] >= len(state["question_ids"])
        ):
            previous_question_id = state["question_ids"][
                state["current_index"] - 1
            ]
            selection = quiz_service.next_review_item(
                g.learner_id,
                chapter_ids=set(state["chapter_ids"]) or None,
                source_ids=set(state["source_ids"]) or None,
                avoid_question_ids={previous_question_id},
            )
            for item in selection.items:
                state["question_ids"].append(item.question_id)
                state["review_items"].append(item.to_dict())
            state["review_shortages"] = list(selection.shortages)
        state["status"] = "pending"
        state["answer_token"] = secrets.token_urlsafe(24)
        state.pop("feedback", None)
        g.quiz_progress[_progress_state.session_key(mode)] = state
        return redirect(url_for(endpoint))

    def _state_for(mode: QuizMode) -> dict[str, Any] | None:
        return _progress_state.valid_state_for(g.quiz_progress, mode)

    def _active_progress(mode: QuizMode) -> dict[str, Any] | None:
        """Return a small resume summary for an unfinished mode."""
        return _progress_state.active_summary(g.quiz_progress, mode)

    def _curriculum() -> list[dict[str, Any]]:
        """Build one dynamic catalogue for selection and filtering templates."""
        return _view.build_curriculum(question_repository)

    def _chapters_of(question: Question) -> list[Chapter | None]:
        """Return the chapter records one question belongs to, in bank order."""
        return [
            question_repository.get_chapter(chapter_id)
            for chapter_id in question.chapter_ids
        ]

    def _knowledge_update_feedback(result: AnswerResult) -> list[dict[str, Any]]:
        """Render chapter reinforcement updates for the review feedback card."""
        updates = []
        for update in result.learning_update.knowledge_updates:
            chapter = question_repository.get_chapter(update.point.chapter_id)
            updates.append(
                {
                    "chapter_id": update.point.chapter_id,
                    "chapter_title": (
                        chapter.title if chapter else update.point.chapter_id
                    ),
                    "verified_count": len(update.point.verified_question_ids),
                    "verification_target": (
                        weak_knowledge_point_service.verification_target
                    ),
                    "active": update.point.active,
                    "newly_completed": update.newly_completed,
                }
            )
        return updates

    @blueprint.app_errorhandler(HTTPException)
    def friendly_http_error(error: HTTPException) -> tuple[str, int]:
        messages = {
            400: "提交的数据无效，请返回后重试。",
            404: "没有找到你要访问的页面。",
            405: "当前页面不支持这种操作方式。",
            409: "当前练习状态已经失效，请重新开始。",
            410: "这道题已经不在当前题库中。",
            500: "服务暂时出现问题，请返回首页后重试。",
        }
        if error.code in {404, 405, 500}:
            description = messages[error.code]
        else:
            description = str(error.description) if error.description else messages.get(error.code)
        return (
            render_template(
                "error.html",
                error_code=error.code or 500,
                error_message=description or messages.get(error.code, "页面暂时无法访问。"),
            ),
            error.code or 500,
        )

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

    return blueprint
