import copy

from app import create_app
from app.models import QuizMode
from tests.conftest import write_json
from tests.test_web import learner_id, make_app, register


def test_glossary_requires_login_and_renders_repository_metadata(
    tmp_path, valid_payload, statistics_glossary
):
    glossary_file = write_json(tmp_path / "glossary.json", statistics_glossary)
    app = make_app(tmp_path, valid_payload, GLOSSARY_FILE=glossary_file)
    client = app.test_client()

    assert client.get("/glossary").status_code == 302
    register(client)
    page = client.get("/glossary")

    assert page.status_code == 200
    assert "统计学专业词汇" in page.text
    assert "Statistics Glossary" in page.text
    assert "Descriptive Statistics" in page.text
    assert "Hypothesis Testing" in page.text
    assert "Standard Deviation" in page.text
    assert "data-glossary-search" in page.text
    assert "data-picker-option" in page.text
    assert "<select" not in page.text


def test_home_and_learning_content_expose_generic_glossary_hooks(
    tmp_path, valid_payload, statistics_glossary
):
    valid_payload["questions"][0]["text"] = "Which standard deviation is larger?"
    valid_payload["questions"][0]["options"][0]["text"] = "A standard deviation"
    glossary_file = write_json(tmp_path / "glossary.json", statistics_glossary)
    app = make_app(tmp_path, valid_payload, GLOSSARY_FILE=glossary_file)
    client = app.test_client()
    home = register(client)

    assert "专业词汇" in home.text
    assert "统计学专业词汇" in home.text
    assert 'js/glossary.js' in home.text
    client.post("/quiz/start", data={"quiz_size": "all"})
    quiz = client.get("/quiz")
    assert "data-glossary-highlight" in quiz.text
    assert '"standard-deviation"' in quiz.text


def test_glossary_change_does_not_reset_any_learning_state(
    tmp_path, valid_payload, statistics_glossary
):
    glossary_file = write_json(tmp_path / "glossary.json", statistics_glossary)
    first_app = make_app(tmp_path, valid_payload, GLOSSARY_FILE=glossary_file)
    first_client = first_app.test_client()
    register(first_client, "persistent")
    user_id = learner_id(first_client)
    first_client.post("/quiz/start", data={"quiz_size": "all"})
    first_app.extensions["mcq_services"].wrong_question_service.record_attempt(
        learner_id=user_id,
        question_id="q1",
        mode=QuizMode.NORMAL,
        selected_answers=("1",),
        is_correct=False,
    )

    changed = copy.deepcopy(statistics_glossary)
    changed["terms"][0]["term_zh"] = "标准偏差"
    write_json(glossary_file, changed)
    restarted = make_app(tmp_path, valid_payload, GLOSSARY_FILE=glossary_file)
    services = restarted.extensions["mcq_services"]

    assert services.attempt_repository.count() == 1
    assert services.wrong_question_repository.get_by_id(user_id, "q1") is not None
    assert services.progress_repository.get(user_id, QuizMode.NORMAL)[1] is not None


def test_highlight_hooks_cover_feedback_explanation_mistakes_and_review(
    tmp_path, valid_payload, statistics_glossary
):
    for question in valid_payload["questions"]:
        question["text"] = "Compare the standard deviation."
        question["explanation"] = "Standard deviation measures dispersion."
        question["options"][0]["text"] = "A standard deviation"
    glossary_file = write_json(tmp_path / "glossary.json", statistics_glossary)
    app = make_app(tmp_path, valid_payload, GLOSSARY_FILE=glossary_file)
    client = app.test_client()
    register(client)
    user_id = learner_id(client)
    services = app.extensions["mcq_services"]

    client.post("/quiz/start", data={"quiz_size": "all"})
    _, state = services.progress_repository.get(user_id, QuizMode.NORMAL)
    question = services.question_repository.get_by_id(state["question_ids"][0])
    client.post(
        "/quiz/answer",
        data={
            "answer_token": state["answer_token"],
            "answers": list(question.correct_answers),
        },
    )
    feedback = client.get("/quiz")

    services.wrong_question_service.record_attempt(
        user_id, "q1", QuizMode.NORMAL, ("1",), False
    )
    mistakes = client.get("/mistakes")
    client.post("/review/start")
    review = client.get("/review")

    assert "data-feedback" in feedback.text
    assert feedback.text.count("data-glossary-highlight") >= 4
    assert "data-glossary-highlight" in mistakes.text
    assert "data-glossary-highlight" in review.text


def test_statistics_course_replacement_needs_only_two_json_files(
    tmp_path, valid_payload, statistics_glossary
):
    valid_payload.update(
        title="Statistics Practice",
        title_zh="统计学选择题练习",
    )
    for question in valid_payload["questions"]:
        question.update(
            text="Which standard deviation is larger?",
            explanation="Standard deviation measures dispersion.",
        )
    glossary_file = write_json(tmp_path / "glossary.json", statistics_glossary)
    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "QUESTION_FILE": write_json(tmp_path / "questions.json", valid_payload),
            "GLOSSARY_FILE": glossary_file,
            "DATABASE": tmp_path / "mcq.db",
        }
    )
    client = app.test_client()

    home = register(client)
    setup = client.get("/quiz/setup")
    client.post("/quiz/start", data={"quiz_size": "all"})
    quiz = client.get("/quiz")
    glossary = client.get("/glossary")

    assert all(page.status_code == 200 for page in (home, setup, quiz, glossary))
    assert "统计学选择题练习" in home.text
    assert "Which standard deviation is larger?" in quiz.text
    assert "统计学专业词汇" in glossary.text
    assert "Hypothesis Testing" in glossary.text
