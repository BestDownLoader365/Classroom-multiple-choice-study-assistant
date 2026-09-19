"""Repositories and data-loading utilities."""

from .attempt_repository import AttemptRepository
from .course_loader import (
    LAYOUT_LEGACY,
    LAYOUT_MANIFEST,
    MANIFEST_NAME,
    CourseBundle,
    CourseLoader,
)
from .course_repository import CourseRecord, CourseRepository, CourseStateError
from .course_scope import require_course_id
from .database import CrossCourseQueries, Database
from .exam_repository import ExamRepository
from .glossary_loader import GlossaryError, GlossaryLoader, normalize_glossary_label
from .glossary_repository import GlossaryRepository
from .progress_repository import ProgressRepository
from .question_bank_state_repository import QuestionBankStateRepository
from .question_loader import QuestionBankError, QuestionLoader
from .question_registry_repository import QuestionRegistryRepository
from .question_repository import QuestionRepository
from .rate_limit_repository import RateLimitRepository
from .schema_migrations import (
    LEGACY_SCHEMA_VERSION,
    SCHEMA_VERSION,
    SchemaInfo,
    SchemaMigrationError,
    ensure_schema,
    read_meta,
)
from .user_repository import UserRepository, UsernameAlreadyExistsError
from .weak_knowledge_point_repository import WeakKnowledgePointRepository
from .wrong_question_repository import WrongQuestionRepository

__all__ = [
    "AttemptRepository",
    "CourseBundle",
    "CourseLoader",
    "CourseRecord",
    "CourseRepository",
    "CourseStateError",
    "CrossCourseQueries",
    "Database",
    "ExamRepository",
    "GlossaryError",
    "GlossaryLoader",
    "GlossaryRepository",
    "LAYOUT_LEGACY",
    "LAYOUT_MANIFEST",
    "LEGACY_SCHEMA_VERSION",
    "MANIFEST_NAME",
    "ProgressRepository",
    "QuestionBankError",
    "QuestionBankStateRepository",
    "QuestionLoader",
    "QuestionRegistryRepository",
    "QuestionRepository",
    "RateLimitRepository",
    "SCHEMA_VERSION",
    "SchemaInfo",
    "SchemaMigrationError",
    "UserRepository",
    "UsernameAlreadyExistsError",
    "WeakKnowledgePointRepository",
    "WrongQuestionRepository",
    "ensure_schema",
    "normalize_glossary_label",
    "read_meta",
    "require_course_id",
]

