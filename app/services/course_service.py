"""Assemble every service of one course from that course's own bundle.

This is the single composition root for course-scoped dependencies.  Every
repository is constructed with the *same* ``course_id``, every service receives
those repositories, and the resulting :class:`CourseServices` is immutable.

Because the whole graph is built per course, a wiring mistake cannot silently
mix namespaces.  ``QuestionBankSyncService`` re-checks the scope of everything
it is handed, so a mis-assembled graph fails at startup instead of writing one
course's reconciliation into another course's history.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import tzinfo

from app.models import Course
from app.repositories import (
    AttemptRepository,
    CourseBundle,
    Database,
    ExamRepository,
    GlossaryRepository,
    ProgressRepository,
    QuestionBankStateRepository,
    QuestionRegistryRepository,
    QuestionRepository,
    UserRepository,
    WeakKnowledgePointRepository,
    WrongQuestionRepository,
)

from .exam_service import ExamService
from .global_statistics_service import GlobalStatisticsService
from .grading_service import GradingService
from .question_bank_sync_service import QuestionBankSyncService
from .quiz_service import QuizService
from .statistics_service import StatisticsService
from .weak_knowledge_point_service import WeakKnowledgePointService
from .wrong_question_service import WrongQuestionService

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class CourseServices:
    """Everything one course needs, all bound to the same namespace.

    The repositories here are *persistence* repositories; the content
    repositories come straight from the immutable :class:`CourseBundle`.
    Nothing in this object can reach outside ``course.course_id``.
    """

    course: Course
    bank_version: str
    #: Generation this worker loaded and holds for its whole lifetime.
    generation: int
    question_repository: QuestionRepository
    glossary_repository: GlossaryRepository | None
    attempt_repository: AttemptRepository
    progress_repository: ProgressRepository
    wrong_question_repository: WrongQuestionRepository
    weak_knowledge_point_repository: WeakKnowledgePointRepository
    exam_repository: ExamRepository
    question_registry_repository: QuestionRegistryRepository
    question_bank_state_repository: QuestionBankStateRepository
    grading_service: GradingService
    weak_knowledge_point_service: WeakKnowledgePointService
    wrong_question_service: WrongQuestionService
    quiz_service: QuizService
    exam_service: ExamService
    statistics_service: StatisticsService
    global_statistics_service: GlobalStatisticsService
    question_bank_sync_service: QuestionBankSyncService
    #: The publication this worker loaded, for the startup/publication race.
    publication_identity: str = ""

    @property
    def course_id(self) -> str:
        return self.course.course_id

    @property
    def title(self) -> str:
        return self.course.title

    @property
    def title_zh(self) -> str:
        return self.course.title_zh

    def has_glossary(self) -> bool:
        return self.glossary_repository is not None


def assemble_course_services(
    bundle: CourseBundle,
    *,
    database: Database,
    knowledge_verification_target: int,
    display_timezone: tzinfo,
    user_repository: UserRepository,
) -> CourseServices:
    """Build the full, course-scoped dependency graph of one loaded bundle."""
    course_id = bundle.course_id
    questions = bundle.question_repository

    attempt_repository = AttemptRepository(database, course_id=course_id)
    progress_repository = ProgressRepository(database, course_id=course_id)
    wrong_question_repository = WrongQuestionRepository(database, course_id=course_id)
    weak_knowledge_point_repository = WeakKnowledgePointRepository(
        database, course_id=course_id
    )
    exam_repository = ExamRepository(database, course_id=course_id)
    registry_repository = QuestionRegistryRepository(database, course_id=course_id)
    state_repository = QuestionBankStateRepository(database, course_id=course_id)

    grading_service = GradingService()
    weak_knowledge_point_service = WeakKnowledgePointService(
        repository=weak_knowledge_point_repository,
        question_repository=questions,
        wrong_question_repository=wrong_question_repository,
        verification_target=knowledge_verification_target,
    )
    wrong_question_service = WrongQuestionService(
        attempt_repository=attempt_repository,
        wrong_question_repository=wrong_question_repository,
        question_repository=questions,
        weak_knowledge_point_service=weak_knowledge_point_service,
    )
    quiz_service = QuizService(
        question_repository=questions,
        grading_service=grading_service,
        wrong_question_service=wrong_question_service,
        weak_knowledge_point_service=weak_knowledge_point_service,
    )
    exam_service = ExamService(
        exam_repository=exam_repository,
        question_repository=questions,
        grading_service=grading_service,
        wrong_question_service=wrong_question_service,
    )
    sync_service = QuestionBankSyncService(
        database=database,
        course_id=course_id,
        state_repository=state_repository,
        registry_repository=registry_repository,
        question_repository=questions,
        attempt_repository=attempt_repository,
        wrong_question_repository=wrong_question_repository,
        weak_knowledge_point_service=weak_knowledge_point_service,
        progress_repository=progress_repository,
        exam_service=exam_service,
    )
    return CourseServices(
        course=bundle.course,
        bank_version=bundle.bank_version,
        generation=0,
        question_repository=questions,
        glossary_repository=bundle.glossary_repository,
        attempt_repository=attempt_repository,
        progress_repository=progress_repository,
        wrong_question_repository=wrong_question_repository,
        weak_knowledge_point_repository=weak_knowledge_point_repository,
        exam_repository=exam_repository,
        question_registry_repository=registry_repository,
        question_bank_state_repository=state_repository,
        grading_service=grading_service,
        weak_knowledge_point_service=weak_knowledge_point_service,
        wrong_question_service=wrong_question_service,
        quiz_service=quiz_service,
        exam_service=exam_service,
        statistics_service=StatisticsService(
            attempt_repository=attempt_repository,
            question_repository=questions,
            wrong_question_service=wrong_question_service,
            display_tz=display_timezone,
        ),
        global_statistics_service=GlobalStatisticsService(
            attempt_repository=attempt_repository,
            user_repository=user_repository,
            question_repository=questions,
            display_tz=display_timezone,
        ),
        question_bank_sync_service=sync_service,
        publication_identity=bundle.publication_identity,
    )


def with_generation(services: CourseServices, generation: int) -> CourseServices:
    """Return a copy of ``services`` that records its loaded generation."""
    return replace(services, generation=generation)


def synchronize_course(
    services: CourseServices,
    *,
    publication_identity: Callable[[], str],
) -> CourseServices:
    """Reconcile one course's bank against its data and record the generation.

    The publication identity is re-checked *inside* the sync transaction, so a
    publication that landed during startup is detected and the caller can reload
    instead of writing a stale snapshot back into the database.
    """
    generation = services.question_bank_sync_service.synchronize(
        services.bank_version,
        expected_publication=services.publication_identity,
        publication_identity=publication_identity,
    )
    services.weak_knowledge_point_service.backfill_existing_wrong_questions()
    return with_generation(services, generation)

