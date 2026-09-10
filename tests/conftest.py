import json
from pathlib import Path

import pytest

from app.models import Option, Question


@pytest.fixture
def sample_questions() -> list[Question]:
    return [
        Question(
            id="q1",
            text="Pick one",
            question_type="single",
            options=(Option("1", "One"), Option("2", "Two")),
            correct_answers=("2",),
            explanation="Two is correct.",
            source_id="source-a",
            chapter_ids=("chapter-a",),
        ),
        Question(
            id="q2",
            text="Pick two",
            question_type="multiple",
            options=(Option("a", "A"), Option("b", "B"), Option("c", "C")),
            correct_answers=("a", "c"),
            explanation="A and C are correct.",
            source_id="source-a",
            chapter_ids=("chapter-b",),
        ),
        Question(
            id="q3",
            text="Another question",
            question_type="single",
            options=(Option("yes", "Yes"), Option("no", "No")),
            correct_answers=("yes",),
            explanation="Yes.",
            source_id="source-b",
            chapter_ids=("chapter-c",),
        ),
    ]


@pytest.fixture
def valid_payload() -> dict:
    return {
        "title": "Test",
        "questions": [
            {
                "id": "q1",
                "text": "Pick one",
                "type": "single",
                "options": [
                    {"id": "1", "text": "One"},
                    {"id": "2", "text": "Two"},
                ],
                "correct_answers": ["2"],
                "explanation": "Two.",
            },
            {
                "id": "q2",
                "text": "Pick several",
                "type": "multiple",
                "options": [
                    {"id": "a", "text": "A"},
                    {"id": "b", "text": "B"},
                    {"id": "c", "text": "C"},
                ],
                "correct_answers": ["a", "c"],
                "explanation": "A and C.",
            },
        ],
    }


def write_json(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.fixture
def statistics_glossary() -> dict:
    return {
        "schema_version": 1,
        "title": "Statistics Glossary",
        "title_zh": "统计学专业词汇",
        "description": "Terms used in this course.",
        "terms": [
            {
                "id": "standard-deviation",
                "term": "Standard Deviation",
                "term_zh": "标准差",
                "aliases": ["SD"],
                "definition_zh": "衡量数据相对于均值离散程度的统计量。",
                "category": "Descriptive Statistics",
            },
            {
                "id": "null-hypothesis",
                "term": "Null Hypothesis",
                "term_zh": "原假设",
                "aliases": ["H0"],
                "definition": "A hypothesis tested for possible rejection.",
                "category": "Hypothesis Testing",
            },
        ],
    }
