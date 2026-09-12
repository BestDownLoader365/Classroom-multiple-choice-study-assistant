"""Small, persistence-agnostic domain objects."""

from dataclasses import dataclass
from enum import Enum


class QuizMode(str, Enum):
    """The supported learning modes."""

    NORMAL = "normal"
    REVIEW = "review"
    MOCK_EXAM = "mock_exam"


class ExamStatus(str, Enum):
    """Lifecycle states of one persisted mock-exam session."""

    IN_PROGRESS = "in_progress"
    SUBMITTED = "submitted"
    EXPIRED = "expired"

    @property
    def finished(self) -> bool:
        """Return whether the exam can no longer accept answers."""
        return self is not ExamStatus.IN_PROGRESS


@dataclass(frozen=True)
class Option:
    """One selectable option belonging to a question."""

    id: str
    text: str
    text_zh: str = ""


@dataclass(frozen=True)
class SourceDocument:
    """One course document represented in the question-bank catalogue."""

    id: str
    title: str
    filename: str = ""
    lecture: str = ""


@dataclass(frozen=True)
class Chapter:
    """A stable, learner-facing topic within one source document."""

    id: str
    source_id: str
    title: str
    order: int = 0


@dataclass(frozen=True)
class Question:
    """An immutable question loaded from ``questions.json``."""

    id: str
    text: str
    question_type: str
    options: tuple[Option, ...]
    correct_answers: tuple[str, ...]
    explanation: str
    text_zh: str = ""
    explanation_zh: str = ""
    source_id: str = "legacy"
    chapter_ids: tuple[str, ...] = ("legacy",)
    section: str = ""
    pages: tuple[int, ...] = ()


@dataclass(frozen=True)
class GlossaryTerm:
    """One domain-neutral vocabulary entry loaded from ``glossary.json``."""

    id: str
    term: str
    term_zh: str
    aliases: tuple[str, ...] = ()
    definition: str | None = None
    definition_zh: str | None = None
    category: str | None = None


@dataclass(frozen=True)
class Glossary:
    """Immutable metadata and entries for the active course glossary."""

    schema_version: int
    title: str
    title_zh: str
    terms: tuple[GlossaryTerm, ...]
    description: str | None = None
    description_zh: str | None = None


@dataclass(frozen=True)
class User:
    """One local account used to isolate learning records."""

    id: str
    username: str
    password_hash: str
    created_at: str


@dataclass(frozen=True)
class Attempt:
    """A persisted answer attempt."""

    learner_id: str
    question_id: str
    mode: QuizMode
    selected_answers: tuple[str, ...]
    is_correct: bool
    answered_at: str
    id: int | None = None


@dataclass(frozen=True)
class WrongQuestion:
    """Persistent correction state for a question answered incorrectly."""

    learner_id: str
    question_id: str
    wrong_count: int
    review_streak: int
    corrected: bool
    last_wrong_at: str
    last_reviewed_at: str | None
    srs_level: int = 0
    next_review_at: str | None = None


@dataclass(frozen=True)
class WeakKnowledgePoint:
    """Chapter-level reinforcement state for one learner."""

    learner_id: str
    chapter_id: str
    active: bool
    verified_question_ids: tuple[str, ...]
    last_wrong_at: str
    updated_at: str


@dataclass(frozen=True)
class ExamSession:
    """One persisted mock exam owned by a single learner.

    Timestamps are UTC ISO strings. ``time_limit_seconds`` and
    ``deadline_at`` are ``None`` for untimed exams. ``correct_count`` and
    ``duration_seconds`` are filled when the exam is finalized.
    """

    id: str
    learner_id: str
    status: ExamStatus
    question_count: int
    time_limit_seconds: int | None
    option_seed: str
    created_at: str
    started_at: str
    deadline_at: str | None = None
    submitted_at: str | None = None
    current_position: int = 0
    correct_count: int | None = None
    duration_seconds: int | None = None


@dataclass(frozen=True)
class ExamQuestion:
    """One fixed question slot inside a mock exam."""

    exam_id: str
    position: int
    question_id: str
    selected_answers: tuple[str, ...] = ()
    is_correct: bool | None = None
    answered_at: str | None = None
