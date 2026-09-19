import copy
import logging

from app import create_app

LEGACY_HOME = "/course/legacy/"
from app.models import QuizMode
from tests.test_web import learner_id, make_app, register


def current_state(app, user_id, mode):
    return app.extensions["mcq_services"].default_services.progress_repository.get(
        user_id, mode
    )[1]


def test_transfer_wrong_becomes_real_wrong_and_resets_verification(
    tmp_path, valid_payload
):
    app = make_app(tmp_path, valid_payload)
    client = app.test_client()
    register(client)
    user_id = learner_id(client)
    services = app.extensions["mcq_services"].default_services
    services.wrong_question_service.record_attempt(
        user_id, "q1", QuizMode.NORMAL, ("1",), False
    )

    client.post("/course/legacy/review/start")
    original = current_state(app, user_id, QuizMode.REVIEW)
    client.post(
        "/course/legacy/review/answer",
        data={"answer_token": original["answer_token"], "answers": "2"},
    )
    client.post(
        "/course/legacy/review/next", data={"answer_token": original["answer_token"]}
    )
    transfer = current_state(app, user_id, QuizMode.REVIEW)
    assert transfer["question_ids"][transfer["current_index"]] == "q2"
    assert transfer["review_items"][transfer["current_index"]]["role"] == (
        "transfer_verification"
    )
    assert services.wrong_question_repository.get_by_id(user_id, "q2") is None

    page = client.post(
        "/course/legacy/review/answer",
        data={"answer_token": transfer["answer_token"], "answers": "b"},
        follow_redirects=True,
    )

    record = services.wrong_question_repository.get_by_id(user_id, "q2")
    point = services.weak_knowledge_point_repository.get_by_id(user_id, "legacy")
    assert record is not None
    assert record.corrected is False
    assert point.active is True
    assert point.verified_question_ids == ()
    assert "这道题已加入错题" in page.text
    assert "强化验证已重新开始" in page.text


def test_review_transfer_never_changes_normal_fairness_state(
    tmp_path, valid_payload
):
    app = make_app(tmp_path, valid_payload)
    client = app.test_client()
    register(client)
    user_id = learner_id(client)
    services = app.extensions["mcq_services"].default_services
    client.post("/course/legacy/quiz/start", data={"quiz_size": "10"})
    normal_before = copy.deepcopy(current_state(app, user_id, QuizMode.NORMAL))
    services.wrong_question_service.record_attempt(
        user_id, "q1", QuizMode.NORMAL, ("1",), False
    )

    client.post("/course/legacy/review/start")
    review = current_state(app, user_id, QuizMode.REVIEW)
    client.post(
        "/course/legacy/review/answer",
        data={"answer_token": review["answer_token"], "answers": "2"},
    )
    client.post(
        "/course/legacy/review/next", data={"answer_token": review["answer_token"]}
    )

    normal_after = current_state(app, user_id, QuizMode.NORMAL)
    assert normal_after["fairness_scope"] == normal_before["fairness_scope"]
    assert normal_after["fairness_remaining_ids"] == (
        normal_before["fairness_remaining_ids"]
    )
    assert normal_after["question_ids"] == normal_before["question_ids"]


def test_multi_chapter_question_updates_each_chapter_independently(
    tmp_path, valid_payload
):
    valid_payload["sources"] = [{"id": "course", "title": "Course"}]
    valid_payload["chapters"] = [
        {"id": "a", "source_id": "course", "title": "A"},
        {"id": "b", "source_id": "course", "title": "B"},
    ]
    for question in valid_payload["questions"]:
        question.update(source_id="course", chapter_ids=["a", "b"])
    app = make_app(tmp_path, valid_payload)
    client = app.test_client()
    register(client)
    user_id = learner_id(client)
    services = app.extensions["mcq_services"].default_services
    learning = services.wrong_question_service

    learning.record_attempt(user_id, "q1", QuizMode.NORMAL, ("1",), False)
    assert all(
        services.weak_knowledge_point_repository.get_by_id(
            user_id, chapter_id
        ).active
        for chapter_id in ("a", "b")
    )

    learning.record_attempt(user_id, "q1", QuizMode.REVIEW, ("2",), True)
    learning.record_attempt(user_id, "q1", QuizMode.REVIEW, ("2",), True)
    for chapter_id in ("a", "b"):
        point = services.weak_knowledge_point_repository.get_by_id(
            user_id, chapter_id
        )
        assert point.verified_question_ids == ("q1",)
        assert point.active is True

    learning.record_attempt(
        user_id, "q2", QuizMode.REVIEW, ("a", "c"), True
    )
    for chapter_id in ("a", "b"):
        point = services.weak_knowledge_point_repository.get_by_id(
            user_id, chapter_id
        )
        assert set(point.verified_question_ids) == {"q1", "q2"}
        assert point.active is False


def test_completed_chapter_reopens_after_later_wrong_answer(
    tmp_path, valid_payload
):
    app = make_app(tmp_path, valid_payload)
    client = app.test_client()
    register(client)
    user_id = learner_id(client)
    services = app.extensions["mcq_services"].default_services
    learning = services.wrong_question_service
    learning.record_attempt(user_id, "q1", QuizMode.NORMAL, ("1",), False)
    learning.record_attempt(user_id, "q1", QuizMode.REVIEW, ("2",), True)
    learning.record_attempt(
        user_id, "q2", QuizMode.REVIEW, ("a", "c"), True
    )
    assert not services.weak_knowledge_point_repository.get_by_id(
        user_id, "legacy"
    ).active

    learning.record_attempt(user_id, "q2", QuizMode.NORMAL, ("b",), False)

    point = services.weak_knowledge_point_repository.get_by_id(user_id, "legacy")
    assert point.active is True
    assert point.verified_question_ids == ()
    assert not services.wrong_question_repository.get_by_id(
        user_id, "q2"
    ).corrected


def test_mistakes_page_shows_weak_point_progress_and_completed_state(
    tmp_path, valid_payload
):
    app = make_app(tmp_path, valid_payload)
    client = app.test_client()
    register(client)
    user_id = learner_id(client)
    services = app.extensions["mcq_services"].default_services
    learning = services.wrong_question_service
    learning.record_attempt(user_id, "q1", QuizMode.NORMAL, ("1",), False)

    pending = client.get("/course/legacy/mistakes")
    assert "薄弱知识点" in pending.text
    assert "0 / 2 道不同题目" in pending.text
    assert "待强化" in pending.text
    assert 'class="table-card"' in pending.text
    assert 'class="mistake-table"' in pending.text

    learning.record_attempt(user_id, "q1", QuizMode.REVIEW, ("2",), True)
    learning.record_attempt(
        user_id, "q2", QuizMode.REVIEW, ("a", "c"), True
    )
    completed = client.get("/course/legacy/mistakes")

    assert "2 / 2 道不同题目" in completed.text
    assert "强化完成" in completed.text


def test_generated_transfer_is_stable_across_refresh_and_devices(
    tmp_path, valid_payload
):
    app = make_app(tmp_path, valid_payload)
    first = app.test_client()
    register(first)
    user_id = learner_id(first)
    services = app.extensions["mcq_services"].default_services
    services.wrong_question_service.record_attempt(
        user_id, "q1", QuizMode.NORMAL, ("1",), False
    )
    first.post("/course/legacy/review/start")
    original = current_state(app, user_id, QuizMode.REVIEW)
    first.post(
        "/course/legacy/review/answer",
        data={"answer_token": original["answer_token"], "answers": "2"},
    )
    first.post(
        "/course/legacy/review/next", data={"answer_token": original["answer_token"]}
    )
    generated = copy.deepcopy(current_state(app, user_id, QuizMode.REVIEW))

    other_app = create_app(dict(app.config))
    second = other_app.test_client()
    second.post(
        "/login", data={"username": "learner", "password": "secret1"}
    )
    second.get(LEGACY_HOME)

    assert first.get("/course/legacy/review").data == second.get("/course/legacy/review").data
    assert current_state(other_app, user_id, QuizMode.REVIEW) == generated
    stale = second.post(
        "/course/legacy/review/answer",
        data={"answer_token": original["answer_token"], "answers": ["a", "c"]},
    )
    assert stale.status_code == 400
    assert current_state(app, user_id, QuizMode.REVIEW) == generated


def test_old_database_adds_weak_table_and_preserves_learning_data(
    tmp_path, valid_payload
):
    app = make_app(tmp_path, valid_payload)
    client = app.test_client()
    register(client)
    user_id = learner_id(client)
    services = app.extensions["mcq_services"].default_services
    services.wrong_question_service.record_attempt(
        user_id, "q1", QuizMode.NORMAL, ("1",), False
    )
    client.post("/course/legacy/quiz/start", data={"quiz_size": "10"})
    database = services.progress_repository.database
    with database.connect() as connection:
        connection.execute("DROP TABLE weak_knowledge_points")
        user_count = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    restarted = create_app(dict(app.config))
    restarted_services = restarted.extensions["mcq_services"].default_services

    assert user_count == 1
    assert restarted_services.attempt_repository.count() == 1
    assert restarted_services.wrong_question_repository.get_by_id(
        user_id, "q1"
    ) is not None
    assert restarted_services.progress_repository.get(
        user_id, QuizMode.NORMAL
    )[1] is not None
    assert restarted_services.weak_knowledge_point_repository.get_by_id(
        user_id, "legacy"
    ) is not None


def test_old_review_queue_is_cleared_without_losing_other_learning_state(
    tmp_path, valid_payload
):
    app = make_app(tmp_path, valid_payload)
    client = app.test_client()
    register(client)
    user_id = learner_id(client)
    services = app.extensions["mcq_services"].default_services
    services.wrong_question_service.record_attempt(
        user_id, "q1", QuizMode.NORMAL, ("1",), False
    )
    client.post("/course/legacy/quiz/start", data={"quiz_size": "10"})
    normal = copy.deepcopy(current_state(app, user_id, QuizMode.NORMAL))
    client.post("/course/legacy/review/start")
    version, old_review = services.progress_repository.get(
        user_id, QuizMode.REVIEW
    )
    old_review.pop("review_items")
    services.progress_repository.save(
        user_id, QuizMode.REVIEW, version, old_review
    )

    response = client.get("/course/legacy/review", follow_redirects=True)

    assert "当前没有可继续的练习" in response.text
    assert services.progress_repository.get(user_id, QuizMode.REVIEW)[1] is None
    assert current_state(app, user_id, QuizMode.NORMAL) == normal
    assert services.wrong_question_repository.get_by_id(
        user_id, "q1"
    ) is not None


def test_single_question_chapter_stops_with_recoverable_shortage(
    tmp_path, valid_payload, caplog
):
    valid_payload["questions"] = [valid_payload["questions"][0]]
    app = make_app(tmp_path, valid_payload)
    client = app.test_client()
    register(client)
    user_id = learner_id(client)
    services = app.extensions["mcq_services"].default_services
    services.wrong_question_service.record_attempt(
        user_id, "q1", QuizMode.NORMAL, ("1",), False
    )
    client.post("/course/legacy/review/start")
    review = current_state(app, user_id, QuizMode.REVIEW)
    client.post(
        "/course/legacy/review/answer",
        data={"answer_token": review["answer_token"], "answers": "2"},
    )

    with caplog.at_level(logging.WARNING):
        page = client.post(
            "/course/legacy/review/next",
            data={"answer_token": review["answer_token"]},
            follow_redirects=True,
        )

    point = services.weak_knowledge_point_repository.get_by_id(user_id, "legacy")
    assert page.status_code == 200
    assert "缺少足够的不同题目" in page.text
    assert "已验证 1 / 2 道不同题目" in page.text
    assert point.active is True
    assert point.verified_question_ids == ("q1",)
    assert 'Chapter "legacy" has only 1 distinct questions' in caplog.text
