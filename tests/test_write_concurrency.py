"""Concurrency regressions: simultaneous writes must never duplicate state.

The write fence exists for exactly this case.  Two rapid clicks, two browser
tabs or two Gunicorn workers submitting the same page at the same time must
grade the answer once, so these tests drive real threads through the Flask test
client over one shared SQLite database — a shape the single-threaded request
tests in ``test_web.py`` cannot cover.

``Database.transaction`` opens ``BEGIN IMMEDIATE``, so the losing requests must
observe the winner's committed state and turn into plain redirects instead of
grading a second time.
"""

import re
import threading

from app.models import QuizMode
from tests.conftest import make_multi_app


def bank(count=12):
    """A bank whose every question accepts the same option IDs."""
    return {
        "title": "Parallel bank",
        "sources": [{"id": "s1", "title": "Source 1", "lecture": "L1"}],
        "chapters": [{"id": "c1", "source_id": "s1", "title": "C1", "order": 1}],
        "questions": [
            {
                "id": f"q{index:03d}",
                "source_id": "s1",
                "chapter_ids": ["c1"],
                "text": f"Question {index}",
                "type": "single",
                "options": [{"id": "a", "text": "A"}, {"id": "b", "text": "B"}],
                "correct_answers": ["a"],
                "explanation": "A.",
            }
            for index in range(count)
        ],
    }


def app_and_client(tmp_path):
    app = make_multi_app(tmp_path, {"course_a": bank()})
    client = app.test_client()
    client.post(
        "/register",
        data={
            "username": "parallel-user",
            "password": "secret1",
            "password_confirmation": "secret1",
        },
    )
    return app, client


def learner_client(app, user_id):
    """A second browser that is already signed in as the same account."""
    client = app.test_client()
    with client.session_transaction() as browser_session:
        browser_session["user_id"] = user_id
    return client


def learner_id(client):
    with client.session_transaction() as browser_session:
        return browser_session["user_id"]


def run_together(callables):
    """Run every callable in its own thread and return their results."""
    results = []
    lock = threading.Lock()

    def wrapper(callable_):
        outcome = callable_()
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=wrapper, args=(item,)) for item in callables]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results


def start_round(client, base="/course/course_a", mode="quiz"):
    page = client.get(f"{base}/{mode}").get_data(as_text=True)
    return re.search(r'name="answer_token" value="([^"]+)"', page).group(1)

def answer_submitter(app, user_id, base, token, answer_id):
    """Build a callable that submits one answer from its own browser."""

    def submit():
        browser = learner_client(app, user_id)
        return browser.post(
            f"{base}/quiz/answer",
            data={"answer_token": token, "answers": answer_id},
        ).status_code

    return submit


def test_simultaneous_identical_answers_are_graded_exactly_once(tmp_path):
    """Eight parallel submits of one page produce one attempt and one count."""
    app, client = app_and_client(tmp_path)
    base = "/course/course_a"
    client.post(f"{base}/quiz/start", data={"quiz_size": "all"})
    token = start_round(client)
    user_id = learner_id(client)
    services = app.extensions["mcq_services"].course("course_a")

    statuses = run_together(
        [answer_submitter(app, user_id, base, token, "a") for _ in range(8)]
    )

    attempts = services.attempt_repository.list_for_learner(user_id)
    state = services.progress_repository.get(user_id, QuizMode.NORMAL)[1]
    assert statuses == [302] * 8
    assert len(attempts) == 1
    assert attempts[0].is_correct is True
    assert (state["correct_count"], state["incorrect_count"]) == (1, 0)
    assert state["status"] == "answered"


def test_a_concurrent_replay_cannot_change_the_verdict(tmp_path):
    """A losing concurrent submit must not open a second, differing grading."""
    app, client = app_and_client(tmp_path)
    base = "/course/course_a"
    client.post(f"{base}/quiz/start", data={"quiz_size": "all"})
    token = start_round(client)
    user_id = learner_id(client)
    services = app.extensions["mcq_services"].course("course_a")

    run_together(
        [answer_submitter(app, user_id, base, token, answer) for answer in "abab"]
    )

    attempts = services.attempt_repository.list_for_learner(user_id)
    state = services.progress_repository.get(user_id, QuizMode.NORMAL)[1]
    assert len(attempts) == 1
    assert state["correct_count"] + state["incorrect_count"] == 1
    assert state["status"] == "answered"


def test_simultaneous_exam_submits_finalize_once(tmp_path):
    """Six parallel submits of one exam produce one score and one attempt."""
    app, client = app_and_client(tmp_path)
    base = "/course/course_a"
    start = client.post(
        f"{base}/exam/start", data={"question_count": "10", "time_limit": "none"}
    )
    exam_id = start.headers["Location"].rsplit("/", 1)[-1]
    user_id = learner_id(client)
    services = app.extensions["mcq_services"].course("course_a")
    exam = services.exam_service.get_session(user_id, exam_id)
    first_slot = services.exam_service.get_questions(exam)[0]
    answered = client.post(
        f"{base}/exam/{exam_id}/answer",
        data={
            "position": str(first_slot.position),
            "question_id": first_slot.question_id,
            "answers": "a",
        },
    )
    assert answered.status_code == 302
    attempts_before = services.attempt_repository.count()

    def submit():
        browser = learner_client(app, user_id)
        return browser.post(f"{base}/exam/{exam_id}/submit", data={}).status_code

    statuses = run_together([submit for _ in range(6)])

    session_after = services.exam_service.get_session(user_id, exam_id)
    report = services.exam_service.get_report(user_id, exam_id)
    assert statuses == [302] * 6
    assert session_after.status.finished
    assert session_after.correct_count == 1
    assert report.correct_count == 1
    assert report.question_count == 10
    # Only the one answered slot created an attempt: the replay graded nothing.
    assert services.attempt_repository.count() == attempts_before + 1
