"""The signed form context a page renders must match the form's own action.

Regression tests for a bug the rest of the suite could not see: the template
helper signed the *rendering page's* endpoint as the operation, while the
transaction guard compares the signed operation against the endpoint the submit
actually reached.  Every real submit was therefore answered with 409
"该页面已经过期（课程或题库版本已更新），请刷新页面后重新提交。", yet the tests
passed because their client fixture mints the *target* operation by hand.

These tests never mint a context: they read the tokens back out of the rendered
HTML and post them exactly as a browser would.
"""

import re

import pytest

from app.web.course_context import read_form_context
from tests.conftest import (
    course_bank,
    course_id_for_path,
    make_multi_app,
    operation_for_course_url,
)
from tests.test_course_web import register, services

COURSE = "course_a"

_FORM = re.compile(r"<form\b(?P<attrs>[^>]*)>(?P<body>.*?)</form>", re.S)
_ACTION = re.compile(r'action="(?P<action>[^"]+)"')
_HIDDEN = re.compile(
    r'<input[^>]*type="hidden"[^>]*name="(?P<name>[^"]+)"'
    r'[^>]*value="(?P<value>[^"]*)"[^>]*>'
)
_ANSWER_OPTION = re.compile(r'name="answers"[^>]*value="(?P<value>[^"]+)"')


def exam_bank() -> dict:
    """Return a bank with enough questions for the smallest mock exam (10)."""
    payload = course_bank()
    template = payload["questions"][0]
    payload["questions"] = [
        {
            **template,
            "id": f"q{index:03d}",
            "chapter_ids": ["chapter_1" if index % 2 else "chapter_2"],
            "text": f"Question {index}?",
        }
        for index in range(12)
    ]
    return payload


def post_forms(page_html):
    """Yield ``(action_path, hidden_fields, body)`` for every POST form."""
    for match in _FORM.finditer(page_html):
        if 'method="post"' not in match.group("attrs"):
            continue
        action = _ACTION.search(match.group("attrs"))
        fields = {
            hidden.group("name"): hidden.group("value")
            for hidden in _HIDDEN.finditer(match.group("body"))
        }
        yield (action.group("action") if action else ""), fields, match.group("body")


def form_for(page_html, suffix):
    """Return ``(action, hidden_fields, body)`` of the POST form ending in ``suffix``."""
    for action, fields, body in post_forms(page_html):
        if action.endswith(suffix):
            return action, fields, body
    raise AssertionError(f"no POST form ending with {suffix!r} on the page")


def wrong_answer(body):
    """Return an option the bank cannot mark correct.

    Every question of this test bank answers ``a``, so this is deterministic
    even though the page shuffles its options per render.
    """
    return next(value for value in _ANSWER_OPTION.findall(body) if value != "a")


def replay(client, page_html, suffix, *, answer=None, **override):
    """Submit one rendered form exactly as the browser would, then follow it.

    ``answer`` is ``None`` (no answer field), ``"first"`` (the first rendered
    option) or ``"wrong"`` (an option the bank cannot mark correct).
    """
    action, fields, body = form_for(page_html, suffix)
    if answer == "first":
        fields["answers"] = _ANSWER_OPTION.findall(body)[0]
    elif answer == "wrong":
        fields["answers"] = wrong_answer(body)
    fields.update(override)
    response = client.post(action, data=fields)
    # A stale form context answers 409 with the refresh message; a rejected
    # answer token answers 400.  Either way the rendered form was not enough.
    assert response.status_code == 302, (
        f"POST {action} -> {response.status_code}; the rendered form was rejected"
    )
    return client.get(response.headers["Location"])


def open_quiz_round(client):
    """Replay the rendered chapter form and return the practice page."""
    setup = client.get(f"/course/{COURSE}/quiz/setup")
    assert setup.status_code == 200
    quiz = replay(client, setup.text, "/quiz/start", quiz_size="10", chapter_ids="all")
    assert quiz.request.path == f"/course/{COURSE}/quiz"
    return quiz


def start_review(client):
    """Replay the rendered review form and return the review page."""
    mistakes = client.get(f"/course/{COURSE}/mistakes")
    assert mistakes.status_code == 200
    review = replay(client, mistakes.text, "/review/start")
    assert review.request.path == f"/course/{COURSE}/review"
    return review


def start_exam(client):
    """Replay the rendered exam form; return the new exam ID and its page."""
    setup = client.get(f"/course/{COURSE}/exam")
    assert setup.status_code == 200
    page = replay(
        client, setup.text, "/exam/start", question_count="10", time_limit="none"
    )
    assert page.status_code == 200
    exam_id = page.request.path.rstrip("/").rsplit("/", 1)[-1]
    return exam_id, page


def audit_form_contexts(app, snapshots):
    """Return ``{action: operation}`` for the learning forms in ``snapshots``."""
    serializer = app.extensions["mcq_form_serializer"]
    audited: dict[str, str] = {}
    for source, page_html in snapshots:
        for action, fields, _ in post_forms(page_html):
            token = fields.get("form_context")
            operation = operation_for_course_url(action)
            if operation is None:
                # Course-exempt forms (logout) carry no learning context.
                assert token is None, f"{source} -> {action}"
                continue
            assert token, f"{source} -> {action}: form carries no signed context"
            payload = read_form_context(serializer, token)
            assert payload is not None, f"{source} -> {action}: unreadable context"
            assert payload["course_id"] == course_id_for_path(action), f"{source} -> {action}"
            assert payload["operation"] == operation, (
                f"{source} -> {action}: signed {payload['operation']!r}, "
                f"the request reaches {operation!r}"
            )
            audited[action] = operation
    return audited


@pytest.fixture
def client(tmp_path):
    """A signed-in browser that sends only what the templates rendered."""
    browser = make_multi_app(tmp_path, {COURSE: exam_bank()}).test_client()
    # Nothing may be auto-signed: every token must come out of the HTML.
    browser._skip_form_context = True
    register(browser)
    return browser


def test_rendered_forms_sign_the_operation_they_perform(client):
    """Every learning form must sign the operation its own action performs."""
    snapshots = []

    for path in (f"/course/{COURSE}/quiz/setup", f"/course/{COURSE}/exam"):
        page = client.get(path)
        assert page.status_code == 200
        snapshots.append((path, page.text))

    quiz = open_quiz_round(client)
    snapshots.append((f"/course/{COURSE}/quiz", quiz.text))
    # Answer wrongly so the practice round also produces review work.
    answered_quiz = replay(client, quiz.text, "/quiz/answer", answer="wrong")
    snapshots.append((f"/course/{COURSE}/quiz (answered)", answered_quiz.text))

    mistakes = client.get(f"/course/{COURSE}/mistakes")
    assert mistakes.status_code == 200
    snapshots.append((f"/course/{COURSE}/mistakes", mistakes.text))

    review = start_review(client)
    snapshots.append((f"/course/{COURSE}/review", review.text))
    answered_review = replay(client, review.text, "/review/answer", answer="first")
    snapshots.append((f"/course/{COURSE}/review (answered)", answered_review.text))

    exam_id, exam = start_exam(client)
    snapshots.append((f"/course/{COURSE}/exam/{exam_id}", exam.text))
    answered_exam = replay(client, exam.text, f"/{exam_id}/answer", answer="first")
    snapshots.append(
        (f"/course/{COURSE}/exam/{exam_id} (answered)", answered_exam.text)
    )

    assert audit_form_contexts(client.application, snapshots) == {
        f"/course/{COURSE}/quiz/start": "start_quiz",
        f"/course/{COURSE}/quiz/answer": "answer_quiz",
        f"/course/{COURSE}/quiz/next": "next_quiz",
        f"/course/{COURSE}/review/start": "start_review",
        f"/course/{COURSE}/review/answer": "answer_review",
        f"/course/{COURSE}/review/next": "next_review",
        f"/course/{COURSE}/mistakes/reset": "reset_mistakes",
        f"/course/{COURSE}/exam/start": "start_exam",
        f"/course/{COURSE}/exam/{exam_id}/answer": "answer_exam",
        f"/course/{COURSE}/exam/{exam_id}/submit": "submit_exam",
    }


def test_a_learner_never_has_to_refresh_a_rendered_form(client):
    """The reported bug: submitting the first rendered answer answered 409."""
    attempts = services(client.application, COURSE).attempt_repository

    quiz = open_quiz_round(client)
    answered = replay(client, quiz.text, "/quiz/answer", answer="wrong")
    assert answered.status_code == 200
    assert attempts.count() == 1
    advanced = replay(client, answered.text, "/quiz/next")
    assert advanced.status_code == 200

    review = start_review(client)
    reviewed = replay(client, review.text, "/review/answer", answer="first")
    assert reviewed.status_code == 200
    assert attempts.count() == 2

    mistakes = client.get(f"/course/{COURSE}/mistakes")
    reset = replay(client, mistakes.text, "/mistakes/reset")
    assert reset.status_code == 200
    # The flash proves the reset really ran instead of being fenced off.
    assert "重置为 0" in reset.text

    exam_id, exam = start_exam(client)
    answered_exam = replay(client, exam.text, f"/{exam_id}/answer", answer="first")
    assert answered_exam.status_code == 200
    report = replay(client, answered_exam.text, f"/{exam_id}/submit")
    assert report.request.path.endswith("/report")
    assert "本场成绩" in report.text
    # Submitting the exam grades it: the third attempt is the exam one.
    assert attempts.count() == 3
