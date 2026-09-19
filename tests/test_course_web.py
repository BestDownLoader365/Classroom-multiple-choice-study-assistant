"""Multi-course web behaviour: switching, forms, fencing, readiness, workers."""

import copy
import re

import pytest

from tests.conftest import (
    course_bank,
    make_multi_app,
    two_course_app,
    write_course,
    write_json,
)

A = "course_a"
B = "course_b"


def register(client, username="learner"):
    return client.post(
        "/register",
        data={
            "username": username,
            "password": "secret1",
            "password_confirmation": "secret1",
        },
        follow_redirects=True,
    )


def services(app, course_id):
    return app.extensions["mcq_services"].course(course_id)


def progress_state(app, course_id, learner, mode="normal"):
    row = services(app, course_id).progress_repository.get(learner, __import__(
        "app.models", fromlist=["QuizMode"]
    ).QuizMode(mode))
    return row[1] if row else None


def learner_of(client):
    with client.session_transaction() as browser_session:
        return browser_session["user_id"]


def any_valid_answer(app, course_id, state):
    """Return one valid option ID for the question the round is showing."""
    question_id = state["question_ids"][state["current_index"]]
    question = services(app, course_id).question_repository.get_by_id(question_id)
    return question.options[0].id


def start_quiz(client, course_id, size="all"):
    return client.post(
        f"/course/{course_id}/quiz/start",
        data={"quiz_size": size},
        follow_redirects=False,
    )


@pytest.fixture
def app(tmp_path):
    return two_course_app(tmp_path)


def test_switching_a_b_a_keeps_independent_progress(app):
    client = app.test_client()
    register(client)
    learner = learner_of(client)

    start_quiz(client, A)
    token_a = progress_state(app, A, learner)["answer_token"]
    assert client.get(f"/course/{A}/").status_code == 200

    start_quiz(client, B)
    token_b = progress_state(app, B, learner)["answer_token"]
    assert token_b != token_a

    # Back to A: the original round is intact and the session preference moved.
    page = client.get(f"/course/{A}/quiz")
    assert page.status_code == 200
    assert token_a in page.text
    with client.session_transaction() as browser_session:
        assert browser_session["last_course_id"] == A

    # Both rounds exist side by side; neither overwrote the other.
    assert progress_state(app, A, learner)["answer_token"] == token_a
    assert progress_state(app, B, learner)["answer_token"] == token_b


def test_two_tabs_keep_their_own_course(app):
    """Opening A, switching another tab to B, then submitting A still submits A."""
    client = app.test_client()
    register(client)
    learner = learner_of(client)

    start_quiz(client, A)
    token_a = progress_state(app, A, learner)["answer_token"]
    # Tab 2 opens B, which rewrites the session preference.
    client.get(f"/course/{B}/")
    with client.session_transaction() as browser_session:
        assert browser_session["last_course_id"] == B

    # Tab 1 submits its A form, whose action URL names A explicitly.
    state_a = progress_state(app, A, learner)
    response = client.post(
        f"/course/{A}/quiz/answer",
        data={
            "answer_token": token_a,
            "answers": [any_valid_answer(app, A, state_a)],
        },
    )

    assert response.status_code == 302
    assert services(app, A).attempt_repository.count() == 1
    assert services(app, B).attempt_repository.count() == 0


def test_a_form_submitted_to_b_url_is_rejected_with_zero_writes(app):
    client = app.test_client()
    register(client)
    learner = learner_of(client)
    start_quiz(client, A)
    start_quiz(client, B)
    state_a = progress_state(app, A, learner)
    token_a = state_a["answer_token"]

    response = client.post(
        f"/course/{B}/quiz/answer",
        data={"answer_token": token_a, "answers": ["b"]},
    )

    assert response.status_code == 400
    assert services(app, A).attempt_repository.count() == 0
    assert services(app, B).attempt_repository.count() == 0


def test_legacy_get_redirects_and_legacy_post_is_refused(app):
    client = app.test_client()
    register(client)

    redirected = client.get("/quiz")
    assert redirected.status_code == 302
    assert redirected.headers["Location"] == f"/course/{A}/quiz"

    refused = client.post("/quiz/start", data={"quiz_size": "all"})
    assert refused.status_code == 409
    assert services(app, A).attempt_repository.count() == 0
    assert progress_state(app, A, learner_of(client)) is None


def test_cross_course_exam_id_is_rejected(app):
    client = app.test_client()
    register(client)
    a = services(app, A)
    from app.models import ExamSession, ExamStatus

    a.exam_repository.create(
        ExamSession(
            id="exam-a",
            learner_id=learner_of(client),
            status=ExamStatus.IN_PROGRESS,
            question_count=2,
            time_limit_seconds=None,
            option_seed="seed",
            created_at="2026-01-01T00:00:00+00:00",
            started_at="2026-01-01T00:00:00+00:00",
        ),
        ("q001", "q002"),
    )

    assert client.get(f"/course/{B}/exam/exam-a").status_code == 409
    assert client.get(f"/course/{A}/exam/exam-a").status_code == 200
    # The old course-less URL resolves the exam's real owner from the database.
    legacy = client.get("/exam/exam-a/report")
    assert legacy.status_code == 302
    assert legacy.headers["Location"].startswith(f"/course/{A}/exam/exam-a")


def test_health_ready_and_per_course_readiness(app):
    client = app.test_client()
    assert client.get("/health").get_json() == {"status": "ok"}

    ready = client.get("/ready")
    assert ready.status_code == 200
    payload = ready.get_json()
    assert payload["status"] == "ready"
    assert sorted(payload["courses"]) == [A, B]
    assert payload["ready_course_count"] == 2

    per_course = client.get(f"/ready/{A}")
    assert per_course.status_code == 200
    assert per_course.get_json()["status"] == "ready"
    assert client.get("/ready/nope").status_code == 404


def _add_question(course_id: str, option_id: str) -> dict:
    changed = course_bank((option_id, course_id))
    changed["questions"].append(
        {
            "id": "q003",
            "source_id": "source_1",
            "chapter_ids": ["chapter_1"],
            "text": f"Third question for {course_id}",
            "type": "single",
            "options": [{"id": "a", "text": "First"}, {"id": "b", "text": "Second"}],
            "correct_answers": ["a"],
            "explanation": "A.",
        }
    )
    return changed


def test_publishing_a_does_not_change_b(tmp_path):
    """A's structural update bumps only A's generation and fences only A."""
    app = two_course_app(tmp_path)
    client = app.test_client()
    register(client)
    learner = learner_of(client)
    start_quiz(client, A)
    start_quiz(client, B)
    state_a = progress_state(app, A, learner)
    state_b = progress_state(app, B, learner)

    write_json(tmp_path / "courses" / A / "questions.json", _add_question(A, "a"))
    from app import create_app

    republished = create_app(dict(app.config))
    registry = republished.extensions["mcq_services"].course_registry

    assert registry.state(A).generation == 1
    assert registry.state(B).generation == 0
    assert set(
        services(republished, B).question_registry_repository.get_all()
    ) == {"q001", "q002"}
    assert registry.state(A).status.value == "ready"
    assert registry.state(B).status.value == "ready"
    assert progress_state(republished, B, learner) == state_b
    assert progress_state(republished, A, learner) == state_a



def test_stale_a_does_not_block_b_routes(tmp_path):
    """A stale course must not make B fail merely because /ready is 503."""
    app = two_course_app(tmp_path)
    client = app.test_client()
    register(client)
    assert client.get(f"/course/{B}/").status_code == 200

    write_json(tmp_path / "courses" / A / "questions.json", _add_question(A, "a"))
    from app import create_app

    # A new worker advances A's generation; the first worker becomes stale for A.
    create_app(dict(app.config))

    registry = app.extensions["mcq_services"].course_registry
    assert registry.state(A).status.value == "stale"
    assert registry.state(B).status.value == "ready"
    # Aggregate readiness degrades, but B's own readiness and routes do not.
    assert client.get("/ready").status_code == 503
    assert client.get(f"/ready/{A}").status_code == 503
    assert client.get(f"/ready/{B}").status_code == 200
    assert client.get(f"/course/{A}/").status_code == 503
    assert client.get(f"/course/{B}/").status_code == 200
    assert start_quiz(client, B).status_code == 302


def test_stale_form_is_rejected_and_progress_survives(tmp_path):
    """A page rendered before a publication is refused with a refresh request."""
    app = two_course_app(tmp_path)
    client = app.test_client()
    register(client)
    learner = learner_of(client)
    start_quiz(client, A)
    state_before = progress_state(app, A, learner)

    write_json(tmp_path / "courses" / A / "questions.json", _add_question(A, "a"))
    from app import create_app
    from app.web.course_context import issue_form_context

    # The new worker activates the publication; only then is the old one stale.
    create_app(dict(app.config))
    stale_token = issue_form_context(
        app.extensions["mcq_form_serializer"],
        course_id=A,
        operation="answer_quiz",
        generation=0,
    )
    assert (
        app.extensions["mcq_services"].course_registry.state(A).status.value == "stale"
    )
    response = client.post(
        f"/course/{A}/quiz/answer",
        data={
            "answer_token": state_before["answer_token"],
            "answers": ["a"],
            "form_context": stale_token,
        },
    )
    assert response.status_code == 503
    assert services(app, A).attempt_repository.count() == 0

    # A healthy worker keeps the same round: the stale form never cleared it.
    fresh = create_app(dict(app.config))
    fresh_client = fresh.test_client()
    fresh_client.post("/login", data={"username": "learner", "password": "secret1"})
    state_after = progress_state(fresh, A, learner)
    assert state_after == state_before
    accepted = fresh_client.post(
        f"/course/{A}/quiz/answer",
        data={
            "answer_token": state_after["answer_token"],
            "answers": [any_valid_answer(fresh, A, state_after)],
        },
    )
    assert accepted.status_code == 302
    assert services(fresh, A).attempt_repository.count() == 1


def test_wrong_form_context_is_rejected(tmp_path):
    """A form context for another course or operation never writes anything."""
    app = two_course_app(tmp_path)
    client = app.test_client()
    register(client)
    learner = learner_of(client)
    start_quiz(client, A)
    state = progress_state(app, A, learner)
    from app.web.course_context import issue_form_context

    for context in (
        issue_form_context(
            app.extensions["mcq_form_serializer"],
            course_id=B,
            operation="answer_quiz",
            generation=0,
        ),
        issue_form_context(
            app.extensions["mcq_form_serializer"],
            course_id=A,
            operation="start_quiz",
            generation=0,
        ),
        "not-a-signed-token",
    ):
        response = client.post(
            f"/course/{A}/quiz/answer",
            data={
                "answer_token": state["answer_token"],
                "answers": ["a"],
                "form_context": context,
            },
        )
        assert response.status_code == 409
        assert services(app, A).attempt_repository.count() == 0



def test_transaction_guard_closes_the_precheck_race(tmp_path):
    """The "outer pre-check passed, sibling published, then we write" ordering."""
    from app.services.course_consistency import (
        StaleWorkerError,
        guarded_learner_transaction,
    )

    app = two_course_app(tmp_path)
    a = services(app, A)
    # The worker snapshot says generation 0, the database has moved to 1.
    a.question_bank_state_repository.save_state("bank-a2", 1, None)

    with pytest.raises(StaleWorkerError) as excinfo:
        with guarded_learner_transaction(
            database=a.progress_repository.database,
            course_repository=app.extensions["mcq_services"].course_repository,
            state_repository=a.question_bank_state_repository,
            course_id=A,
            worker_generation=0,
            operation="answer_quiz",
            form_context={
                "course_id": A,
                "operation": "answer_quiz",
                "generation": 0,
            },
        ):
            raise AssertionError("the body must never run")
    assert excinfo.value.worker_generation == 0
    assert excinfo.value.database_generation == 1

    # The same transaction for B still runs: fencing is per course.
    b = services(app, B)
    with guarded_learner_transaction(
        database=b.progress_repository.database,
        course_repository=app.extensions["mcq_services"].course_repository,
        state_repository=b.question_bank_state_repository,
        course_id=B,
        worker_generation=0,
    ):
        b.attempt_repository.count()
    assert b.attempt_repository.count() == 0


def test_csrf_enabled_error_page_renders_a_working_logout_form(tmp_path):
    """The error page must be usable with a real CSRF-enabled fixture."""
    app = two_course_app(tmp_path, ENABLE_CSRF=True)
    client = app.test_client()
    register_page = client.get("/register")
    register_token = re.search(
        r'name="csrf_token" value="([^"]+)"', register_page.text
    ).group(1)
    client.post(
        "/register",
        data={
            "username": "learner",
            "password": "secret1",
            "password_confirmation": "secret1",
            "csrf_token": register_token,
        },
    )

    page = client.get("/course/nope/")
    assert page.status_code == 404
    assert "没有找到你要访问的页面" in page.text
    assert 'action="/logout"' in page.text
    logout_token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    logged_out = client.post(
        "/logout", data={"csrf_token": logout_token}, follow_redirects=True
    )
    assert logged_out.status_code == 200
    assert "你已安全退出" in logged_out.text


def test_stale_error_page_is_usable_with_csrf_enabled(tmp_path):
    """The stale-course 503 must render a working page, not a broken form."""
    app = two_course_app(tmp_path, ENABLE_CSRF=True)
    client = app.test_client()
    register_page = client.get("/register")
    token = re.search(r'name="csrf_token" value="([^"]+)"', register_page.text).group(1)
    client.post(
        "/register",
        data={
            "username": "learner",
            "password": "secret1",
            "password_confirmation": "secret1",
            "csrf_token": token,
        },
    )

    write_json(tmp_path / "courses" / A / "questions.json", _add_question(A, "a"))
    from app import create_app

    create_app(dict(app.config))

    page = client.get(f"/course/{A}/")
    assert page.status_code == 503
    assert "题库正在更新" in page.text
    assert "course_a" in page.text
    assert 'href="/courses"' in page.text
    assert page.headers["Retry-After"] == "5"
    # The page's logout form is a real, CSRF-signed form.
    logout_token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    assert (
        client.post(
            "/logout", data={"csrf_token": logout_token}, follow_redirects=True
        ).status_code
        == 200
    )



def test_home_selector_lists_declared_courses_and_needs_no_javascript(tmp_path):
    app = two_course_app(tmp_path)
    client = app.test_client()
    register(client)

    page = client.get(f"/course/{A}/")
    assert f'href="/course/{A}/"' in page.text
    assert f'href="/course/{B}/"' in page.text
    assert 'href="/courses"' in page.text

    selector = client.get("/courses")
    assert selector.status_code == 200
    assert A in selector.text and B in selector.text


def test_unavailable_course_does_not_hide_the_healthy_one(tmp_path):
    """A broken B must leave A serving; B must never borrow A's content."""
    app = two_course_app(tmp_path)
    client = app.test_client()
    register(client)
    start_quiz(client, A)
    before = services(app, A).attempt_repository.count()

    write_json(tmp_path / "courses" / B / "questions.json", {"questions": "broken"})
    from app import create_app

    degraded = create_app(dict(app.config))
    registry = degraded.extensions["mcq_services"].course_registry

    assert registry.state(B).status.value == "unavailable"
    assert registry.state(A).status.value == "ready"
    degraded_client = degraded.test_client()
    degraded_client.post(
        "/login", data={"username": "learner", "password": "secret1"}
    )
    assert degraded_client.get("/ready").status_code == 503
    assert degraded_client.get(f"/ready/{A}").status_code == 200
    assert degraded_client.get(f"/ready/{B}").status_code == 503
    assert degraded_client.get(f"/course/{A}/").status_code == 200
    assert degraded_client.get(f"/course/{B}/").status_code == 503
    # A keeps serving and B's failure never touched A's history.
    assert start_quiz(degraded_client, A).status_code == 302
    assert services(degraded, A).attempt_repository.count() == before

