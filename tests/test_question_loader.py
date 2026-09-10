import copy

import pytest

from app.repositories import QuestionBankError, QuestionLoader
from tests.conftest import write_json


def test_loads_valid_question_bank(tmp_path, valid_payload):
    questions = QuestionLoader(write_json(tmp_path / "questions.json", valid_payload)).load()

    assert [question.id for question in questions] == ["q1", "q2"]
    assert questions[1].question_type == "multiple"
    assert [option.id for option in questions[1].options] == ["a", "b", "c"]


def test_loads_optional_chinese_learning_text(tmp_path, valid_payload):
    valid_payload["title_zh"] = "测试题库"
    valid_payload["questions"][0]["text_zh"] = "选择一个"
    valid_payload["questions"][0]["options"][0]["text_zh"] = "一"
    valid_payload["questions"][0]["explanation_zh"] = "二是正确答案。"
    loader = QuestionLoader(write_json(tmp_path / "questions.json", valid_payload))

    questions = loader.load()

    assert loader.title_zh == "测试题库"
    assert questions[0].text_zh == "选择一个"
    assert questions[0].options[0].text_zh == "一"
    assert questions[0].explanation_zh == "二是正确答案。"


def test_omitted_chinese_fields_remain_empty(tmp_path, valid_payload):
    loader = QuestionLoader(write_json(tmp_path / "questions.json", valid_payload))

    questions = loader.load()

    assert loader.title_zh == ""
    assert questions[0].text_zh == ""
    assert questions[0].options[0].text_zh == ""
    assert questions[0].explanation_zh == ""


def test_legacy_bank_without_catalogue_uses_compatible_defaults(
    tmp_path, valid_payload
):
    loader = QuestionLoader(write_json(tmp_path / "questions.json", valid_payload))

    questions = loader.load()

    assert questions[0].source_id == "legacy"
    assert questions[0].chapter_ids == ("legacy",)
    assert loader.sources[0].id == "legacy"
    assert loader.chapters[0].title == "Uncategorized"


def test_loads_and_validates_structured_course_metadata(tmp_path, valid_payload):
    valid_payload["sources"] = [
        {"id": "pd", "title": "Physical Design", "filename": "pd.pdf"}
    ]
    valid_payload["chapters"] = [
        {"id": "floorplanning", "source_id": "pd", "title": "Floorplanning", "order": 1}
    ]
    for question in valid_payload["questions"]:
        question.update(
            source_id="pd",
            chapter_ids=["floorplanning"],
            section="Macro placement",
            pages=[12],
        )
    loader = QuestionLoader(write_json(tmp_path / "questions.json", valid_payload))

    questions = loader.load()

    assert loader.sources[0].filename == "pd.pdf"
    assert loader.chapters[0].title == "Floorplanning"
    assert questions[0].pages == (12,)


def test_question_can_belong_to_multiple_chapters(tmp_path, valid_payload):
    valid_payload["sources"] = [{"id": "course", "title": "Course"}]
    valid_payload["chapters"] = [
        {"id": "algebra", "source_id": "course", "title": "Algebra"},
        {"id": "geometry", "source_id": "course", "title": "Geometry"},
    ]
    for question in valid_payload["questions"]:
        question.update(
            source_id="course",
            chapter_ids=["algebra", "geometry"],
        )

    questions = QuestionLoader(
        write_json(tmp_path / "questions.json", valid_payload)
    ).load()

    assert questions[0].chapter_ids == ("algebra", "geometry")


def test_legacy_singular_chapter_id_remains_supported(tmp_path, valid_payload):
    valid_payload["sources"] = [{"id": "course", "title": "Course"}]
    valid_payload["chapters"] = [
        {"id": "topic", "source_id": "course", "title": "Topic"}
    ]
    for question in valid_payload["questions"]:
        question.update(source_id="course", chapter_id="topic")

    questions = QuestionLoader(
        write_json(tmp_path / "questions.json", valid_payload)
    ).load()

    assert questions[0].chapter_ids == ("topic",)


def test_singular_and_plural_chapter_fields_cannot_be_combined(
    tmp_path, valid_payload
):
    valid_payload["sources"] = [{"id": "course", "title": "Course"}]
    valid_payload["chapters"] = [
        {"id": "topic", "source_id": "course", "title": "Topic"}
    ]
    for question in valid_payload["questions"]:
        question.update(
            source_id="course",
            chapter_id="topic",
            chapter_ids=["topic"],
        )

    with pytest.raises(QuestionBankError, match="not both"):
        QuestionLoader(write_json(tmp_path / "questions.json", valid_payload)).load()


def test_unknown_chapter_in_plural_field_is_rejected(tmp_path, valid_payload):
    valid_payload["sources"] = [{"id": "course", "title": "Course"}]
    valid_payload["chapters"] = [
        {"id": "topic", "source_id": "course", "title": "Topic"}
    ]
    for question in valid_payload["questions"]:
        question.update(source_id="course", chapter_ids=["topic", "missing"])

    with pytest.raises(QuestionBankError, match='chapter_id "missing"'):
        QuestionLoader(write_json(tmp_path / "questions.json", valid_payload)).load()


@pytest.mark.parametrize(
    ("chapter_ids", "message"),
    [
        ([], "non-empty array"),
        (["topic", "topic"], "contains duplicates"),
        (["topic", 2], "non-empty string"),
    ],
)
def test_invalid_plural_chapter_ids_are_rejected(
    tmp_path, valid_payload, chapter_ids, message
):
    valid_payload["sources"] = [{"id": "course", "title": "Course"}]
    valid_payload["chapters"] = [
        {"id": "topic", "source_id": "course", "title": "Topic"}
    ]
    for question in valid_payload["questions"]:
        question.update(source_id="course", chapter_ids=chapter_ids)

    with pytest.raises(QuestionBankError, match=message):
        QuestionLoader(write_json(tmp_path / "questions.json", valid_payload)).load()


def test_question_chapters_must_share_the_question_source(tmp_path, valid_payload):
    valid_payload["sources"] = [
        {"id": "first", "title": "First"},
        {"id": "second", "title": "Second"},
    ]
    valid_payload["chapters"] = [
        {"id": "first-topic", "source_id": "first", "title": "First topic"},
        {"id": "second-topic", "source_id": "second", "title": "Second topic"},
    ]
    for question in valid_payload["questions"]:
        question.update(
            source_id="first",
            chapter_ids=["first-topic", "second-topic"],
        )

    with pytest.raises(QuestionBankError, match="belongs to a different source"):
        QuestionLoader(write_json(tmp_path / "questions.json", valid_payload)).load()


def test_catalogued_bank_rejects_question_without_chapter(tmp_path, valid_payload):
    valid_payload["sources"] = [{"id": "pd", "title": "Physical Design"}]
    valid_payload["chapters"] = [
        {"id": "floorplanning", "source_id": "pd", "title": "Floorplanning"}
    ]

    with pytest.raises(QuestionBankError, match='"source_id"'):
        QuestionLoader(write_json(tmp_path / "questions.json", valid_payload)).load()


def test_missing_file_has_clear_error(tmp_path):
    question_file = tmp_path / "questions.json"

    with pytest.raises(QuestionBankError, match="Question bank not found") as error:
        QuestionLoader(question_file).load()

    assert str(question_file) in str(error.value)
    assert "next to run.py" in str(error.value)


def test_invalid_json_has_location(tmp_path):
    question_file = tmp_path / "questions.json"
    question_file.write_text('{"questions": [}', encoding="utf-8")

    with pytest.raises(QuestionBankError, match="Invalid JSON"):
        QuestionLoader(question_file).load()


def test_invalid_schema_version_is_rejected(tmp_path, valid_payload):
    valid_payload["schema_version"] = 0

    with pytest.raises(QuestionBankError, match="schema_version"):
        QuestionLoader(write_json(tmp_path / "questions.json", valid_payload)).load()


def test_invalid_utf8_is_reported_as_question_bank_error(tmp_path):
    question_file = tmp_path / "questions.json"
    question_file.write_bytes(b'{"title": "\xff", "questions": []}')

    with pytest.raises(QuestionBankError, match="UTF-8"):
        QuestionLoader(question_file).load()


def test_duplicate_question_id_is_rejected(tmp_path, valid_payload):
    payload = copy.deepcopy(valid_payload)
    payload["questions"][1]["id"] = "q1"

    with pytest.raises(QuestionBankError, match="Duplicate question id"):
        QuestionLoader(write_json(tmp_path / "questions.json", payload)).load()


def test_duplicate_option_id_is_rejected(tmp_path, valid_payload):
    payload = copy.deepcopy(valid_payload)
    payload["questions"][0]["options"][1]["id"] = "1"

    with pytest.raises(QuestionBankError, match="duplicate option id"):
        QuestionLoader(write_json(tmp_path / "questions.json", payload)).load()


def test_single_question_requires_one_correct_answer(tmp_path, valid_payload):
    payload = copy.deepcopy(valid_payload)
    payload["questions"][0]["correct_answers"] = ["1", "2"]

    with pytest.raises(QuestionBankError, match="exactly one correct answer"):
        QuestionLoader(write_json(tmp_path / "questions.json", payload)).load()


def test_nonexistent_correct_answer_is_rejected(tmp_path, valid_payload):
    payload = copy.deepcopy(valid_payload)
    payload["questions"][0]["correct_answers"] = ["missing"]

    with pytest.raises(QuestionBankError, match='correct answer "missing"'):
        QuestionLoader(write_json(tmp_path / "questions.json", payload)).load()
