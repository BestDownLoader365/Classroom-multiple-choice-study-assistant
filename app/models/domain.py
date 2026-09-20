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


#: The synthetic catalogue of a question bank that declares no sources or
#: chapters (the legacy root ``questions.json`` layout and any hand-written
#: bank).  Both the loader (which materializes it) and the in-memory
#: repository (which falls back to it) use *this* definition, so a legacy
#: question is always filed under the same course material and chapter.
LEGACY_SOURCE = SourceDocument(
    id="legacy", title="Uncategorized course material", lecture="Legacy question bank"
)
LEGACY_CHAPTER = Chapter(
    id="legacy", source_id="legacy", title="Uncategorized", order=1
)


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
    #: Owning course namespace.  ``exam_id`` stays globally unique, but a
    #: session always belongs to exactly one course; repositories fill this in
    #: from their own binding, so callers never have to know the namespace.
    course_id: str = ""


@dataclass(frozen=True)
class ExamQuestion:
    """One fixed question slot inside a mock exam.

    ``grading_fingerprint`` records the question's grading identity at exam
    creation so historical reports can detect slots whose grading rule has
    since changed.  It is ``None`` for slots created before tracking existed.
    """

    exam_id: str
    position: int
    question_id: str
    selected_answers: tuple[str, ...] = ()
    is_correct: bool | None = None
    answered_at: str | None = None
    grading_fingerprint: str | None = None


class QuestionRegistryStatus(str, Enum):
    """Lifecycle states of one tracked question identity."""

    ACTIVE = "active"
    RETIRED = "retired"


@dataclass(frozen=True)
class QuestionRegistryEntry:
    """Persistent per-question identity record used for bank reconciliation.

    ``question_type``, ``option_ids`` and ``correct_answers`` together form
    the grading identity: historical answers stay meaningful exactly while
    all three stay compatible.  ``content_fingerprint`` covers every other
    validated field and only distinguishes cosmetic edits from no-ops.
    ``placement_fingerprint`` covers the ``source_id``/``chapter_ids`` filing
    identity: changing it keeps learner history but is still a structural bank
    change, because chapter membership drives filtering, review selection and
    chapter progress.  Rows written before placement tracking store an empty
    placement fingerprint and are adopted (backfilled) on the next startup
    without a generation bump.
    A retired row is a tombstone: it is never deleted, so a deleted
    question's ID can never be silently recycled for a different question.
    """

    question_id: str
    status: QuestionRegistryStatus
    question_type: str
    option_ids: tuple[str, ...]
    correct_answers: tuple[str, ...]
    content_fingerprint: str
    first_seen_at: str
    last_seen_at: str
    retired_at: str | None = None
    placement_fingerprint: str = ""
