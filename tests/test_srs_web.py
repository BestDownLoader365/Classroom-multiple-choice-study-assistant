"""End-to-end SRS behavior through the Flask review flow and home entry."""

from datetime import datetime, timedelta, timezone

from app.models import QuizMode
from app.services import srs_service as srs
from tests.test_web import learner_id, make_app, register

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _services(app):
    return app.extensions["mcq_services"]


def _review_state(app, user_id):
    return _services(app).progress_repository.get(user_id, QuizMode.REVIEW)[1]


def _make_due(app, user_id, question_id, when=None):
    due_at = (when or (NOW - timedelta(days=1))).isoformat()
    database = _services(app).progress_repository.database
    with database.connect() as connection:
        connection.execute(
            "UPDATE wrong_questions SET next_review_at = ? "
            "WHERE learner_id = ? AND question_id = ?",
            (due_at, user_id, question_id),
        )


def _seed_corrected_schedule(app, user_id, question_id="q1"):
    """Normal 答错 → Review 纠正，生成 1 天后的首次 SRS 排期。"""
    services = _services(app)
    services.wrong_question_service.record_attempt(
        user_id, question_id, QuizMode.NORMAL, ("1",), False, now=NOW
    )
    services.wrong_question_service.record_attempt(
        user_id, question_id, QuizMode.REVIEW, ("2",), True, now=NOW
    )


def test_corrected_question_returns_for_scheduled_srs_reviews(
    tmp_path, valid_payload
):
    app = make_app(tmp_path, valid_payload)
    client = app.test_client()
    register(client)
    user_id = learner_id(client)
    services = _services(app)

    # Normal 模式答错 → 进入错题本。
    services.wrong_question_service.record_attempt(
        user_id, "q1", QuizMode.NORMAL, ("1",), False, now=NOW
    )

    # Review 中完成原错题纠正。
    client.post("/review/start")
    state = _review_state(app, user_id)
    assert [
        (item["question_id"], item["role"]) for item in state["review_items"]
    ] == [("q1", "original_correction")]
    client.post(
        "/review/answer",
        data={"answer_token": state["answer_token"], "answers": "2"},
    )

    # 纠正完成 → 安排 1 天后的 SRS，但尚未到期。
    record = services.wrong_question_repository.get_by_id(user_id, "q1")
    assert record.corrected is True
    assert record.srs_level == 0
    assert srs.parse_timestamp(record.next_review_at) > datetime.now(timezone.utc)
    home = client.get("/")
    assert "今日暂无到期复习" in home.text
    assert "今日待复习" not in home.text

    # 时间推进到到期 → 首页出现“今日待复习 1 题”。
    _make_due(app, user_id, "q1")
    home = client.get("/")
    assert "今日待复习 1 题" in home.text

    # 再次进入 Review：以“间隔复习”角色抽到该题。
    client.post("/review/start")
    state = _review_state(app, user_id)
    assert [
        (item["question_id"], item["role"]) for item in state["review_items"]
    ] == [("q1", "srs_review")]
    page = client.get("/review")
    assert "间隔复习" in page.text

    # 到期答对 → 升级到 Level 1，3 天后再次复习。
    before = datetime.now(timezone.utc)
    client.post(
        "/review/answer",
        data={"answer_token": state["answer_token"], "answers": "2"},
    )
    record = services.wrong_question_repository.get_by_id(user_id, "q1")
    assert record.srs_level == 1
    next_due = srs.parse_timestamp(record.next_review_at)
    assert before + timedelta(days=3) <= next_due
    assert next_due <= datetime.now(timezone.utc) + timedelta(days=3)
    assert services.wrong_question_service.get_due_srs_count(user_id) == 0


def test_failed_srs_review_restarts_the_correction_cycle(tmp_path, valid_payload):
    app = make_app(tmp_path, valid_payload)
    client = app.test_client()
    register(client)
    user_id = learner_id(client)
    services = _services(app)
    _seed_corrected_schedule(app, user_id)
    _make_due(app, user_id, "q1")

    # 到期后答错 → 回到纠错状态，SRS 排期被清除。
    client.post("/review/start")
    state = _review_state(app, user_id)
    assert state["review_items"][0]["role"] == "srs_review"
    page = client.post(
        "/review/answer",
        data={"answer_token": state["answer_token"], "answers": "1"},
        follow_redirects=True,
    )

    record = services.wrong_question_repository.get_by_id(user_id, "q1")
    assert record.corrected is False
    assert record.srs_level == 0
    assert record.next_review_at is None
    assert "已重新进入待纠正状态" in page.text

    # 重新开始 Review：它作为待纠正的原错题出现，而不是重复的 SRS 任务。
    client.post("/review/start")
    state = _review_state(app, user_id)
    assert [
        (item["question_id"], item["role"]) for item in state["review_items"]
    ] == [("q1", "original_correction")]

    # 重新纠正成功 → 重新从 1 天周期开始。
    before = datetime.now(timezone.utc)
    client.post(
        "/review/answer",
        data={"answer_token": state["answer_token"], "answers": "2"},
    )
    record = services.wrong_question_repository.get_by_id(user_id, "q1")
    assert record.corrected is True
    assert record.srs_level == 0
    next_due = srs.parse_timestamp(record.next_review_at)
    assert before + timedelta(days=1) <= next_due
    assert next_due <= datetime.now(timezone.utc) + timedelta(days=1)


def test_home_due_count_is_isolated_per_user(tmp_path, valid_payload):
    app = make_app(tmp_path, valid_payload)
    alice = app.test_client()
    register(alice, "alice")
    alice_id = learner_id(alice)
    _seed_corrected_schedule(app, alice_id)
    _make_due(app, alice_id, "q1")

    bob = app.test_client()
    bob_home = register(bob, "bob")
    alice_home = alice.get("/")

    assert "今日暂无到期复习" in bob_home.text
    assert "今日待复习 1 题" not in bob_home.text
    assert "今日待复习 1 题" in alice_home.text


def test_srs_review_session_survives_interruption(tmp_path, valid_payload):
    app = make_app(tmp_path, valid_payload)
    client = app.test_client()
    register(client)
    user_id = learner_id(client)
    _seed_corrected_schedule(app, user_id)
    _make_due(app, user_id, "q1")

    client.post("/review/start")
    client.get("/")  # 中途退出复习页面

    page = client.get("/review")

    assert page.status_code == 200
    assert "Pick one" in page.text
    assert "间隔复习" in page.text


def test_mistakes_page_offers_review_when_only_srs_is_due(tmp_path, valid_payload):
    app = make_app(tmp_path, valid_payload)
    client = app.test_client()
    register(client)
    user_id = learner_id(client)
    services = _services(app)
    _seed_corrected_schedule(app, user_id)
    # 让 legacy 章节的 2 道不同题强化也完成，排除其他 Review 工作来源。
    services.wrong_question_service.record_attempt(
        user_id, "q2", QuizMode.REVIEW, ("a", "c"), True, now=NOW
    )
    _make_due(app, user_id, "q1")

    page = client.get("/mistakes")

    assert "今日待复习" in page.text
    assert "disabled" not in page.text
    client.post("/review/start")
    state = _review_state(app, user_id)
    assert [
        (item["question_id"], item["role"]) for item in state["review_items"]
    ] == [("q1", "srs_review")]

