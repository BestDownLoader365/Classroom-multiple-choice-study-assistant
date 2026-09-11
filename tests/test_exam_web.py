"""End-to-end web tests for the mock-exam pages and flows."""

import re
from datetime import datetime, timedelta, timezone

from app.models import ExamStatus, QuizMode
from tests.test_web import learner_id, make_app, register

NOW = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)


def exam_payload():
    """Build a 12-question, two-chapter bank large enough for mock exams."""
    questions = []
    for index in range(12):
        questions.append(
            {
                "id": f"q{index}",
                "source_id": "pd",
                "chapter_ids": ["chapter-a" if index % 2 == 0 else "chapter-b"],
                "text": f"Question {index}",
                "type": "single",
                "options": [
                    {"id": "a", "text": "Alpha"},
                    {"id": "b", "text": "Beta"},
                ],
                "correct_answers": ["b"],
                "explanation": f"Because {index}.",
            }
        )
    return {
        "title": "Test Bank",
        "sources": [{"id": "pd", "title": "Physical Design", "lecture": "L1"}],
        "chapters": [
            {"id": "chapter-a", "source_id": "pd", "title": "Chapter A", "order": 1},
            {"id": "chapter-b", "source_id": "pd", "title": "Chapter B", "order": 2},
        ],
        "questions": questions,
    }


def services(app):
    return app.extensions["mcq_services"]


def start_exam(client, count="10", limit="none"):
    response = client.post(
        "/exam/start", data={"question_count": count, "time_limit": limit}
    )
    assert response.status_code == 302
    location = response.headers["Location"]
    return location.split("/")[2]


def answer(client, exam_id, position, answers, goto="next"):
    return client.post(
        f"/exam/{exam_id}/answer",
        data={"position": str(position), "answers": list(answers), "goto": goto},
    )


def question_text_on(page_html):
    match = re.search(
        r'<span class="question-en" data-glossary-highlight>(.*?)</span>',
        page_html,
    )
    return match.group(1) if match else None


def expire_exam(app, exam_id):
    """Push the deadline into the past without waiting for real time."""
    database = services(app).progress_repository.database
    past = (NOW - timedelta(hours=1)).isoformat()
    with database.connect() as connection:
        connection.execute(
            "UPDATE exam_sessions SET deadline_at = ? WHERE id = ?",
            (past, exam_id),
        )


def test_exam_setup_lists_options_and_rejects_invalid_config(tmp_path):
    app = make_app(tmp_path, exam_payload())
    client = app.test_client()
    register(client)

    page = client.get("/exam")
    assert page.status_code == 200
    assert "模拟考试" in page.text
    assert "题目数量" in page.text
    assert "时间限制" in page.text
    assert "不限时" in page.text
    assert "还没有模拟考试记录" in page.text

    for bad in ({"question_count": "15", "time_limit": "none"},
                {"question_count": "999", "time_limit": "none"},
                {"question_count": "10", "time_limit": "45"},
                {"question_count": "abc", "time_limit": "none"}):
        response = client.post("/exam/start", data=bad, follow_redirects=True)
        assert "考试配置无效" in response.text or "不支持" in response.text
    assert services(app).exam_repository.count() == 0


def test_exam_flow_keeps_fixed_questions_without_feedback(tmp_path):
    app = make_app(tmp_path, exam_payload())
    client = app.test_client()
    register(client)
    exam_id = start_exam(client)

    first_page = client.get(f"/exam/{exam_id}")
    assert first_page.status_code == 200
    assert "第 1 / 10 题" in first_page.text
    assert "不限时" in first_page.text
    # No immediate grading during the exam.
    assert "提交答案" not in first_page.text
    assert "回答正确" not in first_page.text
    assert "正确答案" not in first_page.text

    # A refresh shows the same fixed question and restores the saved answer.
    first_question = question_text_on(first_page.text)
    answer(client, exam_id, 0, ["a"])
    refreshed = client.get(f"/exam/{exam_id}?q=0")
    assert question_text_on(refreshed.text) == first_question
    assert "已保存 1 项选择" in refreshed.text
    assert 'value="a"' in refreshed.text and "checked" in refreshed.text
    assert "已答 1" in refreshed.text

    # Navigation moves between fixed slots and persists across requests.
    answer(client, exam_id, 0, ["b"], goto="prev")
    after_prev = client.get(f"/exam/{exam_id}?q=0")
    assert "已保存 1 项选择" in after_prev.text
    second_page = client.get(f"/exam/{exam_id}?q=1")
    assert "第 2 / 10 题" in second_page.text

    # The normal practice cycle is untouched by the exam.
    user_id = learner_id(client)
    assert services(app).progress_repository.get(user_id, QuizMode.NORMAL) is None


def test_submit_produces_report_attempts_and_mistakes(tmp_path):
    app = make_app(tmp_path, exam_payload())
    client = app.test_client()
    register(client)
    exam_id = start_exam(client)
    service = services(app).exam_service
    user_id = learner_id(client)
    slots = service.get_questions(service.get_session(user_id, exam_id))

    # Answer the first four slots: even positions correct, odd positions wrong.
    for position in range(4):
        answer(client, exam_id, position, ["b"] if position % 2 == 0 else ["a"])

    submit = client.post(f"/exam/{exam_id}/submit")
    assert submit.status_code == 302
    report = client.get(f"/exam/{exam_id}/report")
    assert report.status_code == 200
    assert "本场成绩" in report.text
    assert "章节表现" in report.text
    assert "错题解析" in report.text
    assert "6 题未作答" in report.text
    assert "20%" in report.text  # 2 correct of 10
    assert "Chapter A" in report.text and "Chapter B" in report.text
    assert "Because" in report.text  # explanations of wrong questions

    session = service.get_session(user_id, exam_id)
    assert session.status is ExamStatus.SUBMITTED
    assert session.correct_count == 2
    wrong_question = services(app).wrong_question_repository.get_by_id(
        user_id, slots[1].question_id
    )
    assert wrong_question is not None and wrong_question.corrected is False
    attempts = services(app).attempt_repository.list_for_learner(user_id)
    assert len(attempts) == 4
    assert all(attempt.mode is QuizMode.MOCK_EXAM for attempt in attempts)

    # Submitting again is a no-op and the exam page is read-only afterwards.
    client.post(f"/exam/{exam_id}/submit")
    assert len(services(app).attempt_repository.list_for_learner(user_id)) == 4
    reopened = client.get(f"/exam/{exam_id}")
    assert reopened.status_code == 302
    assert reopened.headers["Location"].endswith("/report")
    locked = answer(client, exam_id, 4, ["b"])
    assert locked.headers["Location"].endswith("/report")
    assert "已经交卷" in client.get(locked.headers["Location"]).text

    # The exam history links back to the immutable report.
    setup_page = client.get("/exam")
    assert "已交卷" in setup_page.text
    assert "查看报告" in setup_page.text
    assert "过往模拟考试" in setup_page.text


def test_timed_exam_auto_submits_after_the_server_deadline(tmp_path):
    app = make_app(tmp_path, exam_payload())
    client = app.test_client()
    register(client)
    exam_id = start_exam(client, limit="600")

    page = client.get(f"/exam/{exam_id}")
    assert "data-exam-timer" in page.text
    assert "data-remaining-seconds" in page.text
    answer(client, exam_id, 0, ["b"])

    expire_exam(app, exam_id)

    # Loading the exam after the deadline finalizes it as expired.
    redirected = client.get(f"/exam/{exam_id}", follow_redirects=True)
    assert "自动交卷" in redirected.text
    assert "本场成绩" in redirected.text
    user_id = learner_id(client)
    session = services(app).exam_service.get_session(user_id, exam_id)
    assert session.status is ExamStatus.EXPIRED
    assert session.correct_count == 1

    # A late answer POST is rejected instead of changing the result.
    response = answer(client, exam_id, 1, ["a"])
    assert response.headers["Location"].endswith("/report")
    slots = services(app).exam_service.get_questions(session)
    assert slots[1].selected_answers == ()

    report = client.get(f"/exam/{exam_id}/report")
    assert "时间结束，自动交卷" in report.text


def test_unfinished_exam_has_no_report_and_owner_only_access(tmp_path):
    app = make_app(tmp_path, exam_payload())
    owner = app.test_client()
    register(owner, "alice")
    exam_id = start_exam(owner)

    # An in-progress exam redirects the report route back to the exam page.
    early = owner.get(f"/exam/{exam_id}/report")
    assert early.status_code == 302
    assert early.headers["Location"].endswith(f"/exam/{exam_id}")

    other = app.test_client()
    register(other, "bob")
    assert other.get(f"/exam/{exam_id}").status_code == 404
    assert other.get(f"/exam/{exam_id}/report").status_code == 404
    assert other.post(f"/exam/{exam_id}/submit").status_code == 404
    forged = answer(other, exam_id, 0, ["a"])
    assert forged.status_code == 404
    assert services(app).exam_service.get_session(
        learner_id(owner), exam_id
    ).status is ExamStatus.IN_PROGRESS


def test_home_and_setup_offer_resume_for_active_exam(tmp_path):
    app = make_app(tmp_path, exam_payload())
    client = app.test_client()
    register(client)
    exam_id = start_exam(client)
    answer(client, exam_id, 2, ["b"])  # last visited position becomes 2

    home = client.get("/")
    assert "继续模拟考试" in home.text
    assert "第 3 / 10 题" in home.text
    assert "学习数据" in home.text

    setup_page = client.get("/exam")
    assert "进行中的考试" in setup_page.text
    assert "继续考试" in setup_page.text

    # Reopening the exam without a page parameter lands on the saved spot.
    resumed = client.get(f"/exam/{exam_id}")
    assert "第 3 / 10 题" in resumed.text


def test_legacy_attempts_table_is_migrated_in_place(tmp_path):
    """A pre-mock-exam database keeps its rows and learns the new mode."""
    import sqlite3

    from app.models import Attempt
    from app.repositories import AttemptRepository, Database

    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            learner_id TEXT NOT NULL,
            question_id TEXT NOT NULL,
            mode TEXT NOT NULL CHECK (mode IN ('normal', 'review')),
            selected_answers TEXT NOT NULL,
            is_correct INTEGER NOT NULL CHECK (is_correct IN (0, 1)),
            answered_at TEXT NOT NULL
        );
        INSERT INTO attempts (
            learner_id, question_id, mode, selected_answers, is_correct,
            answered_at
        ) VALUES ('learner', 'q0', 'normal', '["b"]', 1, '2026-01-01T00:00:00+00:00');
        """
    )
    connection.commit()
    connection.close()

    database = Database(path)
    database.initialize()
    database.initialize()  # the migration stays idempotent on repeat startup

    attempts = AttemptRepository(database)
    kept = attempts.list_for_learner("learner")
    assert len(kept) == 1
    assert kept[0].question_id == "q0"
    # The widened CHECK constraint now accepts mock-exam attempts.
    attempts.add(
        Attempt(
            learner_id="learner",
            question_id="q1",
            mode=QuizMode.MOCK_EXAM,
            selected_answers=("a",),
            is_correct=False,
            answered_at="2026-01-02T00:00:00+00:00",
        )
    )
    assert len(attempts.list_for_learner("learner")) == 2


def test_expired_exam_settles_when_visiting_home(tmp_path):
    app = make_app(tmp_path, exam_payload())
    client = app.test_client()
    register(client)
    exam_id = start_exam(client, limit="600")
    answer(client, exam_id, 0, ["a"])  # wrong answer
    expire_exam(app, exam_id)

    home = client.get("/")

    assert "继续模拟考试" not in home.text
    user_id = learner_id(client)
    session = services(app).exam_service.get_session(user_id, exam_id)
    assert session.status is ExamStatus.EXPIRED
    # The settlement also synced the wrong answer into the mistake flow.
    slots = services(app).exam_service.get_questions(session)
    record = services(app).wrong_question_repository.get_by_id(
        user_id, slots[0].question_id
    )
    assert record is not None and record.corrected is False
    # The history and the report are immediately consistent.
    setup_page = client.get("/exam")
    assert "已超时" in setup_page.text
    assert "进行中的考试" not in setup_page.text
    report = client.get(f"/exam/{exam_id}/report")
    assert report.status_code == 200
    assert "时间结束，自动交卷" in report.text


def test_dashboard_visit_settles_expired_exam_into_statistics(tmp_path):
    app = make_app(tmp_path, exam_payload())
    client = app.test_client()
    register(client)
    exam_id = start_exam(client, limit="600")
    answer(client, exam_id, 0, ["b"])  # correct
    answer(client, exam_id, 1, ["a"])  # wrong
    expire_exam(app, exam_id)

    page = client.get("/dashboard")

    assert "暂无答题记录" not in page.text
    user_id = learner_id(client)
    dashboard = services(app).statistics_service.build_dashboard(user_id)
    assert dashboard.total_attempts == 2
    assert dashboard.correct_attempts == 1
    assert dashboard.pending_wrong == 1
