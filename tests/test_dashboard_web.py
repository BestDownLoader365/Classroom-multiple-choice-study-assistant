"""Web tests for the learning dashboard page."""

from app.models import QuizMode
from tests.test_exam_web import answer, start_exam, exam_payload
from tests.test_web import learner_id, make_app, register


def record(app, user_id, question_id, correct, *, mode=QuizMode.NORMAL):
    """Record one attempt with the real current time (dashboard windows)."""
    app.extensions["mcq_services"].default_services.wrong_question_service.record_attempt(
        learner_id=user_id,
        question_id=question_id,
        mode=mode,
        selected_answers=("b",) if correct else ("a",),
        is_correct=correct,
    )


def test_dashboard_requires_login(tmp_path):
    client = make_app(tmp_path, exam_payload()).test_client()
    response = client.get("/course/legacy/dashboard")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/login")


def test_dashboard_empty_state_for_new_learner(tmp_path):
    app = make_app(tmp_path, exam_payload())
    client = app.test_client()
    register(client)

    page = client.get("/course/legacy/dashboard")

    assert page.status_code == 200
    assert "暂无答题记录" in page.text
    assert "章节掌握度" in page.text
    assert "未开始" in page.text
    assert "Chapter A" in page.text and "Chapter B" in page.text
    assert "NaN" not in page.text


def test_dashboard_shows_metrics_mastery_and_trend(tmp_path):
    app = make_app(tmp_path, exam_payload())
    client = app.test_client()
    register(client)
    user_id = learner_id(client)
    record(app, user_id, "q0", True)
    record(app, user_id, "q1", False)
    record(app, user_id, "q2", True)

    page = client.get("/course/legacy/dashboard")

    assert "累计答题" in page.text
    assert "总正确率" in page.text
    assert "67%" in page.text  # 2 of 3 correct
    assert "最近 7 天" in page.text
    assert "最近 30 天" in page.text
    assert "待纠正错题" in page.text
    assert "已纠正错题" in page.text
    assert "学习趋势" in page.text
    assert "trend-chart" in page.text
    assert "进行中" in page.text  # chapter-a: 2/3 ≈ 67%
    assert "薄弱" in page.text  # chapter-b: 0/1 = 0%
    assert "数据较少" in page.text  # low-sample chapter
    assert "mastery-bar" in page.text


def test_dashboard_reflects_mock_exam_attempts(tmp_path):
    app = make_app(tmp_path, exam_payload())
    client = app.test_client()
    register(client)
    exam_id = start_exam(client)
    answer(client, exam_id, 0, ["b"])
    answer(client, exam_id, 1, ["a"])
    client.post(f"/course/legacy/exam/{exam_id}/submit")

    page = client.get("/course/legacy/dashboard")

    assert "暂无答题记录" not in page.text
    assert "待纠正错题" in page.text
    dashboard = app.extensions["mcq_services"].default_services.statistics_service.build_dashboard(
        learner_id(client)
    )
    assert dashboard.total_attempts == 2
    assert dashboard.correct_attempts == 1


def test_dashboard_is_scoped_to_the_signed_in_user(tmp_path):
    app = make_app(tmp_path, exam_payload())
    alice = app.test_client()
    register(alice, "alice")
    record(app, learner_id(alice), "q0", True)

    bob = app.test_client()
    register(bob, "bob")
    page = bob.get("/course/legacy/dashboard")

    assert "暂无答题记录" in page.text


def test_displayed_dates_follow_the_configured_timezone(tmp_path):
    from datetime import timezone
    from zoneinfo import ZoneInfo

    from app.services import srs_service as srs

    app = make_app(tmp_path, exam_payload(), DISPLAY_TIMEZONE="Asia/Shanghai")
    client = app.test_client()
    register(client)
    exam_id = start_exam(client)
    answer(client, exam_id, 0, ["b"])
    client.post(f"/course/legacy/exam/{exam_id}/submit")

    services = app.extensions["mcq_services"].default_services
    session = services.exam_service.get_session(learner_id(client), exam_id)
    expected = (
        srs.parse_timestamp(session.created_at)
        .astimezone(ZoneInfo("Asia/Shanghai"))
        .strftime("%Y-%m-%d %H:%M")
    )

    setup_page = client.get("/course/legacy/exam")
    assert expected in setup_page.text
    assert "UTC+08:00" in setup_page.text
    report = client.get(f"/course/legacy/exam/{exam_id}/report")
    assert expected in report.text
    dashboard = client.get("/course/legacy/dashboard")
    assert "UTC+08:00" in dashboard.text
