import json
import re
from pathlib import Path

import pytest
from flask.testing import FlaskClient

from app.models import Option, Question

#: Ordered (suffix, operation) pairs for the POST routes the web layer serves.
POST_OPERATION_SUFFIXES = (
    ("/quiz/start", "start_quiz"),
    ("/quiz/answer", "answer_quiz"),
    ("/quiz/next", "next_quiz"),
    ("/review/start", "start_review"),
    ("/review/answer", "answer_review"),
    ("/review/next", "next_review"),
    ("/mistakes/reset", "reset_mistakes"),
    ("/exam/start", "start_exam"),
)

_EXAM_SLOT_ROUTE = re.compile(r"/exam/(?P<exam_id>[^/]+)/(?P<action>answer|submit)$")


def operation_for_course_url(path: str) -> str | None:
    """Return the operation a course-scoped POST path performs."""
    match = _EXAM_SLOT_ROUTE.search(path)
    if match:
        return f"{match.group('action')}_exam"
    for suffix, operation in POST_OPERATION_SUFFIXES:
        if path.endswith(suffix):
            return operation
    return None


#: ``form_context`` hidden field of a rendered learning form.
_SIGNED_FORM_CONTEXT = re.compile(rb'(name="form_context" value=")[^"]*(")')


def mask_signed_form_context(body: bytes) -> bytes:
    """Blank the time-stamped signed ``form_context`` values of a page body.

    ``form_context`` is minted by an ``itsdangerous`` ``URLSafeTimedSerializer``,
    whose signature carries the *second* it was signed: two renders a second
    apart differ in that field even when the page is otherwise identical.  Tests
    that compare two independent renders byte for byte must ignore exactly that
    field, otherwise they fail whenever the pair straddles a second boundary.
    Every other byte still has to match.
    """
    return _SIGNED_FORM_CONTEXT.sub(rb"\1<context>\2", body)


def course_id_for_path(path: str) -> str | None:
    """Return the ``course_id`` encoded in a course-scoped path, if any."""
    match = re.match(r"^/course/(?P<course_id>[^/]+)(?:/|$)", path)
    return match.group("course_id") if match else None


@pytest.fixture(autouse=True)
def _inject_signed_form_context(monkeypatch):
    """Mint the same signed form context the real templates render.

    Every learning form carries a server-signed ``form_context`` (course,
    operation, worker generation) that the transaction guard re-validates.  The
    Flask test client would otherwise have to hand-build it at ~200 call sites,
    so this fixture signs one automatically for a POST whose URL names a course
    and whose body does not already carry one.

    Tests that exercise the guard itself set ``client._skip_form_context = True``
    to send a request with no context (or with a deliberately wrong one).
    """
    original_open = FlaskClient.open

    def open_with_context(self, *args, **kwargs):
        if getattr(self, "_skip_form_context", False):
            return original_open(self, *args, **kwargs)
        path = args[0] if args else kwargs.get("path")
        method = str(kwargs.get("method", "GET")).upper()
        data = kwargs.get("data")
        if (
            isinstance(path, str)
            and method in {"POST", "PUT", "PATCH", "DELETE"}
            and (data is None or isinstance(data, dict))
        ):
            course_id = course_id_for_path(path)
            operation = operation_for_course_url(path)
            payload = dict(data or {})
            if course_id and operation and "form_context" not in payload:
                services = self.application.extensions["mcq_services"]
                serializer = self.application.extensions["mcq_form_serializer"]
                state = services.course_registry.state(course_id)
                if state is not None and state.generation is not None:
                    from app.web.course_context import issue_form_context

                    payload["form_context"] = issue_form_context(
                        serializer,
                        course_id=course_id,
                        operation=operation,
                        generation=state.generation,
                    )
            if payload != (data or {}):
                kwargs = dict(kwargs)
                kwargs["data"] = payload
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(FlaskClient, "open", open_with_context)
    yield


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


def bundled_loader():
    """Build the app's course loader over the *deployed* catalogue.

    These helpers let the bundled-content tests follow wherever the content
    actually lives (``courses/<course_id>/…`` today, root files historically)
    instead of hard-coding a path that a layout change would break.
    """
    from app import COURSES_DIR, GLOSSARY_FILE, QUESTION_FILE
    from app.repositories import CourseLoader

    return CourseLoader(
        COURSES_DIR,
        legacy_directory=QUESTION_FILE.parent,
        legacy_questions_name=QUESTION_FILE.name,
        legacy_glossary_name=GLOSSARY_FILE.name,
    )


def bundled_question_file() -> Path:
    """Return the question bank of the first enabled deployed course."""
    definitions = bundled_loader().enabled_definitions()
    assert definitions, "no enabled course is declared in the deployed catalogue"
    return definitions[0].questions_path


def bundled_glossary_file() -> Path:
    """Return the glossary of the first enabled deployed course that declares one."""
    for definition in bundled_loader().enabled_definitions():
        if definition.glossary_path is not None:
            return definition.glossary_path
    raise AssertionError("no enabled deployed course declares a glossary")


def write_json(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# --------------------------------------------------------------------- courses


def course_bank(correct: tuple[str, str] = ("a", "alpha")) -> dict:
    """Build a small bank whose local IDs are deliberately reusable across courses.

    Two courses built from this payload share ``source_1``, ``chapter_1`` and
    ``q001`` while having completely different content and correct answers, which
    is exactly the collision the multi-course refactor must isolate.  ``correct``
    is ``(option_id, tag)``: the option ID must exist in the question, while the
    tag only makes the two banks' text different.
    """
    option_id, tag = correct
    return {
        "title": f"Bank {tag}",
        "sources": [{"id": "source_1", "title": "Source 1", "lecture": "L1"}],
        "chapters": [
            {"id": "chapter_1", "source_id": "source_1", "title": "Chapter 1", "order": 1},
            {"id": "chapter_2", "source_id": "source_1", "title": "Chapter 2", "order": 2},
        ],
        "questions": [
            {
                "id": "q001",
                "source_id": "source_1",
                "chapter_ids": ["chapter_1"],
                "text": f"Which option is correct for {tag}?",
                "type": "single",
                "options": [
                    {"id": "a", "text": "First"},
                    {"id": "b", "text": "Second"},
                    {"id": "c", "text": "Third"},
                ],
                # The correct answer differs per course on purpose.
                "correct_answers": [option_id],
                "explanation": f"Answer is {option_id}.",
            },
            {
                "id": "q002",
                "source_id": "source_1",
                "chapter_ids": ["chapter_2"],
                "text": f"Second question for {tag}?",
                "type": "single",
                "options": [
                    {"id": "x", "text": "X"},
                    {"id": "y", "text": "Y"},
                ],
                "correct_answers": ["y"],
                "explanation": "Y.",
            },
        ],
    }


def write_course(
    courses_dir: Path,
    course_id: str,
    payload: dict,
    *,
    glossary: dict | None = None,
    enabled: bool = True,
    order: int = 0,
    title: str | None = None,
    questions_name: str = "questions.json",
    glossary_name: str = "glossary.json",
    manifest: dict | None = None,
):
    """Materialise one ``courses/<course_id>/`` directory."""
    root = courses_dir / course_id
    root.mkdir(parents=True, exist_ok=True)
    write_json(root / questions_name, payload)
    glossary_value: str | None = None
    if glossary is not None:
        write_json(root / glossary_name, glossary)
        glossary_value = glossary_name
    document = manifest if manifest is not None else {
        "schema_version": 1,
        "course_id": course_id,
        "title": title or course_id.title(),
        "title_zh": "",
        "enabled": enabled,
        "questions": questions_name,
        "glossary": glossary_value,
        "order": order,
    }
    write_json(root / "course.json", document)
    return root


def course_glossary(prefix: str) -> dict:
    """Build a two-term glossary unique to one course."""
    return {
        "schema_version": 1,
        "title": f"{prefix.title()} Glossary",
        "title_zh": f"{prefix} 术语表",
        "terms": [
            {
                "id": f"{prefix}-term-1",
                "term": f"{prefix} term one",
                "term_zh": "一",
                "aliases": [f"{prefix} alias one"],
            },
            {
                "id": f"{prefix}-term-2",
                "term": f"{prefix} term two",
                "term_zh": "二",
            },
        ],
    }


def make_multi_app(
    tmp_path: Path,
    payloads: dict[str, dict],
    glossaries: dict[str, dict] | None = None,
    **overrides,
):
    """Build an app over a generated multi-course tree on one database."""
    from app import create_app

    courses_dir = tmp_path / "courses"
    for course_id, payload in payloads.items():
        write_course(
            courses_dir,
            course_id,
            payload,
            glossary=(glossaries or {}).get(course_id),
        )
    config = {
        "TESTING": True,
        "SECRET_KEY": "test-secret",
        "COURSES_DIR": courses_dir,
        # No root bank: this deployment is manifest-only.
        "QUESTION_FILE": tmp_path / "absent" / "questions.json",
        "GLOSSARY_FILE": tmp_path / "absent" / "glossary.json",
        "DATABASE": tmp_path / "mcq.db",
    }
    config.update(overrides)
    return create_app(config)


def services_for(app, course_id: str):
    """Return one course's service graph from a built app."""
    return app.extensions["mcq_services"].course(course_id)


def two_course_app(tmp_path, **overrides):
    """A two-course app whose courses share every local ID but not their content."""
    return make_multi_app(
        tmp_path,
        {
            "course_a": course_bank(("a", "alpha")),
            "course_b": course_bank(("b", "beta")),
        },
        glossaries={"course_a": course_glossary("a"), "course_b": course_glossary("b")},
        **overrides,
    )


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
                "category": "Hypothesis Testing",
            },
        ],
    }
