"""Web tests for the cross-account global statistics page."""

from tests.test_dashboard_web import record
from tests.test_exam_web import answer, exam_payload, start_exam
from tests.test_web import learner_id, make_app, register


def test_stats_requires_login(tmp_path):
    client = make_app(tmp_path, exam_payload()).test_client()
    response = client.get("/stats")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/login")


def test_home_links_to_the_global_stats_page(tmp_path):
    app = make_app(tmp_path, exam_payload())
    client = app.test_client()
    register(client)

    page = client.get("/")

    assert "全员统计" in page.text
    assert 'href="/stats"' in page.text
    assert "仅供参考" in page.text


def test_stats_empty_state_for_a_fresh_site(tmp_path):
    app = make_app(tmp_path, exam_payload())
    client = app.test_client()
    register(client)

    page = client.get("/stats")

    assert page.status_code == 200
    assert "暂无全员答题记录" in page.text
    assert "章节难度榜" in page.text
    assert "无人作答" in page.text
    assert "Chapter A" in page.text and "Chapter B" in page.text
    assert "NaN" not in page.text


def test_stats_aggregates_every_account_without_exposing_one(tmp_path):
    app = make_app(tmp_path, exam_payload())
    alice = app.test_client()
    register(alice, "alice")
    record(app, learner_id(alice), "q0", True)
    record(app, learner_id(alice), "q1", False)

    bob = app.test_client()
    register(bob, "bob")
    record(app, learner_id(bob), "q2", True)

    page = alice.get("/stats")

    assert "注册账号" in page.text
    assert "有作答的账号" in page.text
    assert "全员累计答题" in page.text
    assert "全员总正确率" in page.text
    assert "67%" in page.text  # 2 of 3 attempts correct
    assert "最近 7 天" in page.text
    assert "最近 30 天" in page.text
    assert "全员学习趋势" in page.text
    assert "trend-chart" in page.text
    assert "较难" in page.text  # chapter-b: 1/3 ≈ 33% across accounts
    assert "作答人数" in page.text
    # The aggregate page never names another account.
    assert "bob" not in page.text

    overview = app.extensions[
        "mcq_services"
    ].global_statistics_service.build_overview()
    assert overview.account_count == 2
    assert overview.active_account_count == 2
    assert overview.total_attempts == 3
    assert overview.correct_attempts == 2


def test_stats_reflects_mock_exam_attempts(tmp_path):
    app = make_app(tmp_path, exam_payload())
    client = app.test_client()
    register(client)
    exam_id = start_exam(client)
    answer(client, exam_id, 0, ["b"])
    answer(client, exam_id, 1, ["a"])
    client.post(f"/exam/{exam_id}/submit")

    page = client.get("/stats")

    assert "暂无全员答题记录" not in page.text
    overview = app.extensions[
        "mcq_services"
    ].global_statistics_service.build_overview()
    assert overview.total_attempts == 2
    assert overview.correct_attempts == 1


def test_stats_page_links_back_to_personal_dashboard(tmp_path):
    app = make_app(tmp_path, exam_payload())
    client = app.test_client()
    register(client)

    page = client.get("/stats")

    assert 'href="/dashboard"' in page.text
    assert "我的学习数据" in page.text
    assert "全体学习数据 · 仅供参考" in page.text
