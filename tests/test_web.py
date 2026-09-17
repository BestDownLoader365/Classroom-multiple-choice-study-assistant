import copy
import re
from contextlib import contextmanager

from app import create_app
from app.models import QuizMode
from tests.conftest import write_json


def make_app(tmp_path, valid_payload, **overrides):
    config = {
        "TESTING": True,
        "SECRET_KEY": "test-secret",
        "QUESTION_FILE": write_json(tmp_path / "questions.json", valid_payload),
        "DATABASE": tmp_path / "mcq.db",
    }
    config.update(overrides)
    return create_app(config)


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


def learner_id(client) -> str:
    with client.session_transaction() as browser_session:
        return browser_session["user_id"]


@contextmanager
def stored_learner_state(client):
    """Inspect or seed server progress without depending on browser cookies."""
    repository = client.application.extensions["mcq_services"].progress_repository
    user_id = learner_id(client)
    states = {"user_id": user_id}
    originals = {}
    for mode in QuizMode:
        row = repository.get(user_id, mode)
        originals[mode] = row
        if row and row[1] is not None:
            states[f"quiz_progress_{mode.value}"] = copy.deepcopy(row[1])
    yield states
    for mode, row in originals.items():
        state = states.get(f"quiz_progress_{mode.value}")
        if row and state != row[1]:
            repository.save(user_id, mode, row[0], state)


def add_curriculum(payload):
    payload["sources"] = [
        {"id": "pd", "title": "Physical Design 2", "lecture": "Lecture 3"}
    ]
    payload["chapters"] = [
        {"id": "floorplanning", "source_id": "pd", "title": "Floorplanning", "order": 1},
        {"id": "routing", "source_id": "pd", "title": "Routing", "order": 2},
    ]
    payload["questions"][0].update(
        source_id="pd", chapter_id="floorplanning", section="Macro Placement", pages=[27]
    )
    payload["questions"][1].update(
        source_id="pd", chapter_id="routing", section="Detailed Routing", pages=[30]
    )
    return payload


def test_health_is_public(tmp_path, valid_payload):
    client = make_app(tmp_path, valid_payload).test_client()
    response = client.get("/health")

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_login_is_required_and_registration_opens_personal_home(
    tmp_path, valid_payload
):
    app = make_app(tmp_path, valid_payload)
    client = app.test_client()

    redirect_response = client.get("/")
    registered_home = register(client)

    assert redirect_response.status_code == 302
    assert redirect_response.headers["Location"].endswith("/login")
    assert registered_home.status_code == 200
    assert "当前题库" in registered_home.text
    assert "learner" in registered_home.text


def test_login_logout_and_duplicate_username(tmp_path, valid_payload):
    app = make_app(tmp_path, valid_payload)
    first = app.test_client()
    register(first, "alice")
    logout = first.post("/logout", follow_redirects=True)
    login = first.post(
        "/login",
        data={"username": "ALICE", "password": "secret1"},
        follow_redirects=True,
    )
    second = app.test_client()
    duplicate = register(second, "Alice")

    assert "你已安全退出" in logout.text
    assert "欢迎回来，alice" in login.text
    assert "该用户名已存在" in duplicate.text


def test_home_has_practice_sizes_and_two_core_entries(tmp_path, valid_payload):
    app = make_app(tmp_path, valid_payload)
    client = app.test_client()
    response = register(client)

    assert "开始正常练习" in response.text
    assert "我的错题" in response.text
    assert "快速练习 10 题" in response.text
    assert "标准练习 20 题" in response.text
    assert "其他学习者" not in response.text


def test_chapter_setup_is_dynamic_and_selected_chapter_reaches_session(
    tmp_path, valid_payload
):
    app = make_app(tmp_path, add_curriculum(valid_payload))
    client = app.test_client()
    register(client)

    setup = client.get("/quiz/setup")
    started = client.post(
        "/quiz/start",
        data={"quiz_size": "all", "chapter_ids": "floorplanning"},
    )

    assert "All Chapters" in setup.text
    assert "Floorplanning" in setup.text
    assert "Physical Design 2" in setup.text
    assert "全选本课件章节" in setup.text
    assert 'data-chapter-group-all' in setup.text
    assert 'data-chapter-group' in setup.text
    assert "2 个章节" in setup.text
    assert "1 份课程资料" in setup.text
    assert "四份课程课件" not in setup.text
    assert started.status_code == 302
    with stored_learner_state(client) as browser_session:
        state = browser_session["quiz_progress_normal"]
        assert state["question_ids"] == ["q1"]
        assert state["chapter_ids"] == ["floorplanning"]


def test_quiz_displays_all_chapters_for_a_multi_chapter_question(
    tmp_path, valid_payload
):
    payload = add_curriculum(valid_payload)
    payload["questions"][0].pop("chapter_id")
    payload["questions"][0]["chapter_ids"] = ["floorplanning", "routing"]
    app = make_app(tmp_path, payload)
    client = app.test_client()
    register(client)
    client.post(
        "/quiz/start",
        data={"quiz_size": "all", "chapter_ids": "floorplanning"},
    )

    page = client.get("/quiz")

    assert "Floorplanning" in page.text
    assert "Routing" in page.text


def test_option_labels_continue_after_z(tmp_path, valid_payload):
    valid_payload["questions"][0]["options"] = [
        {"id": f"option-{index}", "text": f"Option {index}"}
        for index in range(27)
    ]
    valid_payload["questions"][0]["correct_answers"] = ["option-0"]
    app = make_app(tmp_path, valid_payload)
    client = app.test_client()
    register(client)
    client.post("/quiz/start", data={"quiz_size": "all"})
    with stored_learner_state(client) as browser_session:
        state = browser_session["quiz_progress_normal"]
        state["question_ids"] = ["q1"]
        state["current_index"] = 0
        browser_session["quiz_progress_normal"] = state

    page = client.get("/quiz")

    assert '<span class="option-index" id="option-label-25">Z</span>' in page.text
    assert '<span class="option-index" id="option-label-26">AA</span>' in page.text


def test_all_chapters_start_preserves_original_behavior(tmp_path, valid_payload):
    app = make_app(tmp_path, add_curriculum(valid_payload))
    client = app.test_client()
    register(client)

    client.post(
        "/quiz/start", data={"quiz_size": "all", "chapter_ids": "all"}
    )

    with stored_learner_state(client) as browser_session:
        assert set(browser_session["quiz_progress_normal"]["question_ids"]) == {
            "q1",
            "q2",
        }
        assert browser_session["quiz_progress_normal"]["chapter_ids"] == []


def test_multiple_selected_chapters_are_combined_server_side(
    tmp_path, valid_payload
):
    app = make_app(tmp_path, add_curriculum(valid_payload))
    client = app.test_client()
    register(client)

    client.post(
        "/quiz/start",
        data={
            "quiz_size": "all",
            "chapter_ids": ["floorplanning", "routing"],
        },
    )

    with stored_learner_state(client) as browser_session:
        state = browser_session["quiz_progress_normal"]
        assert set(state["question_ids"]) == {"q1", "q2"}
        assert state["chapter_ids"] == ["floorplanning", "routing"]


def test_bank_without_chinese_title_uses_english_title(tmp_path, valid_payload):
    app = make_app(tmp_path, valid_payload)
    client = app.test_client()

    response = register(client)

    assert "<title>首页 · Test</title>" in response.text
    assert "<h1>Test</h1>" in response.text
    assert "选择题练习" not in response.text


def test_home_offers_continue_and_confirmable_restart(tmp_path, valid_payload):
    app = make_app(tmp_path, valid_payload)
    client = app.test_client()
    register(client)
    client.post("/quiz/start", data={"quiz_size": "10"})

    response = client.get("/")

    assert "继续正常练习" in response.text
    assert "重新开始正常练习" in response.text
    assert "data-confirm-restart" in response.text
    assert 'method="get" action="/quiz/setup"' in response.text


def test_restart_entry_returns_to_chapter_selection_before_replacing_progress(
    tmp_path, valid_payload
):
    app = make_app(tmp_path, add_curriculum(valid_payload))
    client = app.test_client()
    register(client)
    client.post(
        "/quiz/start",
        data={"quiz_size": "all", "chapter_ids": "floorplanning"},
    )
    with stored_learner_state(client) as browser_session:
        original_token = browser_session["quiz_progress_normal"]["answer_token"]

    setup = client.get("/quiz/setup")

    assert setup.status_code == 200
    assert "选择练习章节" in setup.text
    assert "Floorplanning" in setup.text
    assert "Routing" in setup.text
    with stored_learner_state(client) as browser_session:
        assert browser_session["quiz_progress_normal"]["answer_token"] == original_token

    client.post(
        "/quiz/start", data={"quiz_size": "all", "chapter_ids": "routing"}
    )
    with stored_learner_state(client) as browser_session:
        restarted = browser_session["quiz_progress_normal"]
        assert restarted["question_ids"] == ["q2"]
        assert restarted["chapter_ids"] == ["routing"]
        assert restarted["answer_token"] != original_token


def test_restart_replaces_only_that_modes_progress(tmp_path, valid_payload):
    app = make_app(tmp_path, valid_payload)
    client = app.test_client()
    register(client)
    client.post("/quiz/start", data={"quiz_size": "all"})
    with stored_learner_state(client) as browser_session:
        old_token = browser_session["quiz_progress_normal"]["answer_token"]
        browser_session["quiz_progress_normal"]["current_index"] = 1

    client.post("/quiz/start", data={"quiz_size": "all"})

    with stored_learner_state(client) as browser_session:
        restarted = browser_session["quiz_progress_normal"]
        assert restarted["current_index"] == 0
        assert restarted["answer_token"] != old_token


def test_normal_and_review_progress_are_kept_independently(
    tmp_path, valid_payload
):
    app = make_app(tmp_path, valid_payload)
    client = app.test_client()
    register(client)
    services = app.extensions["mcq_services"]
    user_id = learner_id(client)
    services.wrong_question_service.record_attempt(
        learner_id=user_id,
        question_id="q1",
        mode=QuizMode.NORMAL,
        selected_answers=("1",),
        is_correct=False,
    )

    client.post("/quiz/start", data={"quiz_size": "all"})
    client.post("/review/start")

    with stored_learner_state(client) as browser_session:
        assert "quiz_progress_normal" in browser_session
        assert "quiz_progress_review" in browser_session
        review_token = browser_session["quiz_progress_review"]["answer_token"]
    home = client.get("/")
    assert "继续正常练习" in home.text
    assert "继续错题巩固" in home.text

    client.post("/quiz/start", data={"quiz_size": "all"})
    with stored_learner_state(client) as browser_session:
        assert browser_session["quiz_progress_review"]["answer_token"] == review_token


def test_quiz_and_mistakes_pages_have_return_home_action(
    tmp_path, valid_payload
):
    app = make_app(tmp_path, valid_payload)
    client = app.test_client()
    register(client)
    client.post("/quiz/start", data={"quiz_size": "all"})

    quiz_page = client.get("/quiz")
    mistakes_page = client.get("/mistakes")

    assert "返回首页" in quiz_page.text
    assert "返回首页" in mistakes_page.text


def test_refresh_and_repeated_post_do_not_duplicate_attempt(
    tmp_path, valid_payload
):
    app = make_app(tmp_path, valid_payload)
    client = app.test_client()
    register(client)
    client.post("/quiz/start", data={"quiz_size": "all"})
    with stored_learner_state(client) as browser_session:
        state = browser_session["quiz_progress_normal"]
        user_id = browser_session["user_id"]
        question_id = state["question_ids"][0]
        token = state["answer_token"]

    services = app.extensions["mcq_services"]
    question = services.question_repository.get_by_id(question_id)
    wrong_answer = next(
        option.id
        for option in question.options
        if option.id not in question.correct_answers
    )
    form = {"answer_token": token, "answers": wrong_answer}

    client.post("/quiz/answer", data=form)
    refresh = client.get("/quiz")
    duplicate = client.post("/quiz/answer", data=form)

    assert refresh.status_code == 200
    assert duplicate.status_code == 302
    assert services.attempt_repository.count() == 1
    record = services.wrong_question_repository.get_by_id(user_id, question_id)
    assert record.wrong_count == 1


def test_normal_refresh_preserves_round_and_does_not_consume_fairness(
    tmp_path, valid_payload
):
    app = make_app(tmp_path, valid_payload)
    client = app.test_client()
    register(client)
    client.post("/quiz/start", data={"quiz_size": "10"})
    user_id = learner_id(client)
    repository = app.extensions["mcq_services"].progress_repository
    before = copy.deepcopy(repository.get(user_id, QuizMode.NORMAL)[1])

    first_page = client.get("/quiz")
    second_page = client.get("/quiz")
    after = repository.get(user_id, QuizMode.NORMAL)[1]

    assert first_page.data == second_page.data
    assert after == before
    assert after["question_ids"] == before["question_ids"]
    assert after["option_seed"] == before["option_seed"]
    assert after["fairness_remaining_ids"] == before["fairness_remaining_ids"]
    assert "fairness_remaining_ids" not in first_page.text
    assert "coverage cycle" not in first_page.text


def test_legacy_normal_progress_without_fairness_fields_is_compatible(
    tmp_path, valid_payload
):
    app = make_app(tmp_path, valid_payload)
    client = app.test_client()
    register(client)
    client.post("/quiz/start", data={"quiz_size": "10"})
    user_id = learner_id(client)
    repository = app.extensions["mcq_services"].progress_repository
    version, old_state = repository.get(user_id, QuizMode.NORMAL)
    old_state.pop("fairness_scope")
    old_state.pop("fairness_remaining_ids")
    repository.save(user_id, QuizMode.NORMAL, version, old_state)

    assert client.get("/quiz").status_code == 200
    client.post("/quiz/start", data={"quiz_size": "10"})
    upgraded = repository.get(user_id, QuizMode.NORMAL)[1]

    assert isinstance(upgraded["fairness_scope"], str)
    assert isinstance(upgraded["fairness_remaining_ids"], list)


def test_unknown_answer_is_rejected_without_recording(tmp_path, valid_payload):
    app = make_app(tmp_path, valid_payload)
    client = app.test_client()
    register(client)
    client.post("/quiz/start", data={"quiz_size": "all"})
    with stored_learner_state(client) as browser_session:
        token = browser_session["quiz_progress_normal"]["answer_token"]

    response = client.post(
        "/quiz/answer",
        data={"answer_token": token, "answers": "not-an-option"},
    )

    assert response.status_code == 400
    assert "提交的答案不属于当前题目" in response.text
    assert app.extensions["mcq_services"].attempt_repository.count() == 0


def test_next_before_answer_does_not_advance_progress(tmp_path, valid_payload):
    app = make_app(tmp_path, valid_payload)
    client = app.test_client()
    register(client)
    client.post("/quiz/start", data={"quiz_size": "all"})

    response = client.post("/quiz/next")

    assert response.status_code == 302
    with stored_learner_state(client) as browser_session:
        state = browser_session["quiz_progress_normal"]
        assert state["current_index"] == 0
        assert state["status"] == "pending"


def test_negative_saved_progress_is_discarded_instead_of_resumed(
    tmp_path, valid_payload
):
    app = make_app(tmp_path, valid_payload)
    client = app.test_client()
    register(client)
    client.post("/quiz/start", data={"quiz_size": "all"})
    with stored_learner_state(client) as browser_session:
        state = browser_session["quiz_progress_normal"]
        state["current_index"] = -1
        browser_session["quiz_progress_normal"] = state

    response = client.get("/quiz", follow_redirects=True)

    assert response.status_code == 200
    assert "当前没有可继续的练习" in response.text
    assert "继续正常练习" not in response.text
    with stored_learner_state(client) as browser_session:
        assert "quiz_progress_normal" not in browser_session


def test_empty_multiple_answer_is_not_recorded(tmp_path, valid_payload):
    app = make_app(tmp_path, valid_payload)
    client = app.test_client()
    register(client)
    client.post("/quiz/start", data={"quiz_size": "all"})
    with stored_learner_state(client) as browser_session:
        state = browser_session["quiz_progress_normal"]
        state["question_ids"] = ["q2"]
        state["current_index"] = 0
        browser_session["quiz_progress_normal"] = state
        token = state["answer_token"]

    response = client.post(
        "/quiz/answer",
        data={"answer_token": token},
        follow_redirects=True,
    )

    assert "请至少选择一个答案" in response.text
    assert app.extensions["mcq_services"].attempt_repository.count() == 0


def test_feedback_distinguishes_missed_and_wrong_options(tmp_path, valid_payload):
    app = make_app(tmp_path, valid_payload)
    client = app.test_client()
    register(client)
    client.post("/quiz/start", data={"quiz_size": "all"})
    with stored_learner_state(client) as browser_session:
        state = browser_session["quiz_progress_normal"]
        state["question_ids"] = ["q2"]
        state["current_index"] = 0
        browser_session["quiz_progress_normal"] = state
        token = state["answer_token"]

    response = client.post(
        "/quiz/answer",
        data={"answer_token": token, "answers": ["a", "b"]},
        follow_redirects=True,
    )

    assert "正确选择" in response.text
    assert "漏选" in response.text
    assert "误选" in response.text
    assert "英文解析" in response.text
    # The answer-summary paragraphs must render without template newlines:
    # white-space: pre-line would turn them into blank lines above the answer.
    assert not re.search(r"<p data-glossary-highlight>\s", response.text)
    assert "<p data-glossary-highlight>A and C.</p>" in response.text


def test_review_corrects_original_then_uses_transfer_question(tmp_path, valid_payload):
    app = make_app(tmp_path, valid_payload)
    client = app.test_client()
    register(client)
    services = app.extensions["mcq_services"]
    user_id = learner_id(client)
    services.wrong_question_service.record_attempt(
        user_id, "q1", QuizMode.NORMAL, ("1",), False
    )

    client.post("/review/start")
    with stored_learner_state(client) as browser_session:
        first = browser_session["quiz_progress_review"]
        first_token = first["answer_token"]
    first_answer = client.post(
        "/review/answer",
        data={"answer_token": first_token, "answers": "2"},
        follow_redirects=True,
    )
    assert "原错题已纠正" in first_answer.text
    assert "本知识点已验证 1 / 2 道不同题目" in first_answer.text
    assert "连续答对" not in first_answer.text
    assert services.wrong_question_repository.get_by_id(user_id, "q2") is None

    client.post("/review/next", data={"answer_token": first_token})
    with stored_learner_state(client) as browser_session:
        second = browser_session["quiz_progress_review"]
        second_token = second["answer_token"]
        assert second["question_ids"][1] == "q2"
        assert second["review_items"][1]["role"] == "transfer_verification"
    second_answer = client.post(
        "/review/answer",
        data={"answer_token": second_token, "answers": ["a", "c"]},
        follow_redirects=True,
    )

    assert "同知识点强化题回答正确" in second_answer.text
    assert "本知识点强化完成（2 / 2 道不同题目）" in second_answer.text
    record = services.wrong_question_repository.get_by_id(user_id, "q1")
    assert record.corrected is True
    assert services.wrong_question_repository.get_by_id(user_id, "q2") is None


def test_accounts_only_see_their_own_mistakes(tmp_path, valid_payload):
    app = make_app(tmp_path, valid_payload)
    alice = app.test_client()
    bob = app.test_client()
    register(alice, "alice")
    register(bob, "bob")
    services = app.extensions["mcq_services"]
    services.wrong_question_service.record_attempt(
        learner_id(alice), "q1", QuizMode.NORMAL, ("1",), False
    )
    services.wrong_question_service.record_attempt(
        learner_id(bob), "q2", QuizMode.NORMAL, ("b",), False
    )

    alice_page = alice.get("/mistakes")

    assert "Pick one" in alice_page.text
    assert "Pick several" not in alice_page.text
    assert "其他学习者" not in alice_page.text
    assert "bob" not in alice_page.text


def test_reset_mistakes_clears_only_current_account_and_review_progress(
    tmp_path, valid_payload
):
    app = make_app(tmp_path, valid_payload)
    alice = app.test_client()
    bob = app.test_client()
    register(alice, "alice")
    register(bob, "bob")
    services = app.extensions["mcq_services"]
    alice_id = learner_id(alice)
    bob_id = learner_id(bob)
    services.wrong_question_service.record_attempt(
        alice_id, "q1", QuizMode.NORMAL, ("1",), False
    )
    services.wrong_question_service.record_attempt(
        bob_id, "q2", QuizMode.NORMAL, ("b",), False
    )
    alice.post("/review/start")

    page = alice.get("/mistakes")
    response = alice.post("/mistakes/reset", follow_redirects=True)

    assert "重置全部错题" in page.text
    assert "data-confirm-reset" in page.text
    assert "已将 1 道错题重置为 0" in response.text
    assert "Pick one" not in response.text
    assert services.wrong_question_repository.get_all(alice_id) == []
    assert len(services.wrong_question_repository.get_all(bob_id)) == 1
    assert services.attempt_repository.count() == 2
    with stored_learner_state(alice) as browser_session:
        assert "quiz_progress_review" not in browser_session


def test_reset_mistakes_requires_login_and_post(tmp_path, valid_payload):
    client = make_app(tmp_path, valid_payload).test_client()

    unauthenticated = client.post("/mistakes/reset")
    register(client)
    wrong_method = client.get("/mistakes/reset")

    assert unauthenticated.status_code == 302
    assert unauthenticated.headers["Location"].endswith("/login")
    assert wrong_method.status_code == 405


def test_mistakes_page_uses_configured_verification_target(
    tmp_path, valid_payload
):
    app = make_app(tmp_path, valid_payload, KNOWLEDGE_VERIFICATION_TARGET=3)
    client = app.test_client()
    register(client)
    services = app.extensions["mcq_services"]
    services.wrong_question_service.record_attempt(
        learner_id(client), "q1", QuizMode.NORMAL, ("1",), False
    )

    response = client.get("/mistakes")

    assert "所属知识点还需答对 3 道不同题目" in response.text
    assert "0 / 3 道不同题目" in response.text


def test_mistakes_show_answers_after_correction(
    tmp_path, valid_payload
):
    app = make_app(tmp_path, valid_payload)
    client = app.test_client()
    register(client)
    services = app.extensions["mcq_services"]
    user_id = learner_id(client)

    services.wrong_question_service.record_attempt(
        user_id, "q1", QuizMode.NORMAL, ("1",), False
    )
    services.wrong_question_service.record_attempt(
        user_id, "q2", QuizMode.NORMAL, ("b",), False
    )
    services.wrong_question_service.record_attempt(
        user_id, "q2", QuizMode.REVIEW, ("a", "c"), True
    )
    services.wrong_question_service.record_attempt(
        user_id, "q2", QuizMode.REVIEW, ("a", "c"), True
    )

    page = client.get("/mistakes")

    assert 'data-corrected-question="q2"' in page.text
    assert 'data-correct-option="a"' in page.text
    assert 'data-correct-option="c"' in page.text
    assert 'data-correct-option="b"' not in page.text
    assert "你的答案" in page.text
    assert 'data-selected-option="1"' in page.text
    assert 'data-selected-option="b"' in page.text
    assert "A and C." in page.text
    assert 'data-corrected-question="q1"' not in page.text
    assert 'data-correct-option="2"' not in page.text
    assert "Two." not in page.text


def test_mistakes_can_be_filtered_by_chapter_and_show_course_context(
    tmp_path, valid_payload
):
    app = make_app(tmp_path, add_curriculum(valid_payload))
    client = app.test_client()
    register(client)
    services = app.extensions["mcq_services"]
    user_id = learner_id(client)
    services.wrong_question_service.record_attempt(
        user_id, "q1", QuizMode.NORMAL, ("1",), False
    )
    services.wrong_question_service.record_attempt(
        user_id, "q2", QuizMode.NORMAL, ("b",), False
    )

    page = client.get("/mistakes?chapter=floorplanning")

    assert "Pick one" in page.text
    assert "Pick several" not in page.text
    assert "Floorplanning" in page.text
    assert "Physical Design 2" in page.text
    assert 'id="mistake-chapter" name="chapter" value="floorplanning"' in page.text
    assert 'data-value="floorplanning" aria-selected="true"' in page.text
    assert 'data-select-picker="mistake-source"' in page.text
    assert 'data-select-picker="mistake-chapter"' in page.text
    assert "data-picker-submit-on-change" in page.text
    assert 'class="size-picker-trigger"' in page.text
    assert "筛选错题" not in page.text


def test_account_and_wrong_question_survive_app_restart(tmp_path, valid_payload):
    config = {
        "TESTING": True,
        "SECRET_KEY": "test-secret",
        "QUESTION_FILE": write_json(tmp_path / "questions.json", valid_payload),
        "DATABASE": tmp_path / "mcq.db",
    }
    first_app = create_app(config)
    first_client = first_app.test_client()
    register(first_client, "persistent")
    user_id = learner_id(first_client)
    first_app.extensions["mcq_services"].wrong_question_service.record_attempt(
        learner_id=user_id,
        question_id="q1",
        mode=QuizMode.NORMAL,
        selected_answers=("1",),
        is_correct=False,
    )

    restarted_app = create_app(config)
    restarted_client = restarted_app.test_client()
    restarted_client.post(
        "/login",
        data={"username": "persistent", "password": "secret1"},
    )

    record = restarted_app.extensions[
        "mcq_services"
    ].wrong_question_repository.get_by_id(user_id, "q1")
    assert record is not None
    assert record.wrong_count == 1


def test_changed_question_bank_preserves_progress_silently(tmp_path, valid_payload):
    first_app = make_app(tmp_path, valid_payload)
    first_client = first_app.test_client()
    register(first_client, "learner")
    first_client.post("/quiz/start", data={"quiz_size": "all"})
    with first_client.session_transaction() as browser_session:
        saved_session = dict(browser_session)

    changed_payload = copy.deepcopy(valid_payload)
    changed_payload["questions"][0]["text"] = "Updated question text"
    restarted_app = make_app(tmp_path, changed_payload)
    restarted_client = restarted_app.test_client()
    with restarted_client.session_transaction() as browser_session:
        browser_session.update(saved_session)

    home = restarted_client.get("/")

    # A wording edit is content-only: the round resumes in place and no
    # bank-update banner is ever shown.
    assert "继续正常练习" in home.text
    assert "检测到题库更新" not in home.text


def test_expired_answer_token_uses_chinese_error_page(tmp_path, valid_payload):
    app = make_app(tmp_path, valid_payload)
    client = app.test_client()
    register(client)
    client.post("/quiz/start", data={"quiz_size": "all"})

    response = client.post(
        "/quiz/answer",
        data={"answer_token": "expired", "answers": "1"},
    )

    assert response.status_code == 400
    assert "页面出现问题" in response.text
    assert "答题页面已经过期" in response.text
