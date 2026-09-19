"""Flask-independent guards for every learner write.

Two races motivated this module, and both are closed *inside* the write
transaction rather than by a pre-check:

``stale worker``
    A worker's outer pre-check can pass, a sibling worker can then commit a
    structural publication for the same course, and only afterwards can the old
    worker begin its learner transaction.  Re-reading the course's database
    generation after ``BEGIN IMMEDIATE`` makes that ordering harmless: the
    transaction is rejected with zero learner writes.

``stale form``
    A browser may submit a form rendered by a worker that has since been
    superseded, or a form for a different course.  The signed form context is
    re-validated in the same transaction, so such a submit is rejected before
    any business logic runs.

The helpers return plain exceptions; the web layer owns the HTTP mapping
(``503`` for stale/unavailable, ``409`` for a stale form, ``404`` for an
unknown course).
"""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from app.repositories import CourseRepository, Database, QuestionBankStateRepository

FORM_CONTEXT_COURSE = "course_id"
FORM_CONTEXT_OPERATION = "operation"
FORM_CONTEXT_GENERATION = "generation"


class CourseConsistencyError(RuntimeError):
    """Base class for a rejected learner transaction."""


class CourseServabilityChangedError(CourseConsistencyError):
    """The course stopped being servable between rendering and writing (503)."""


class StaleWorkerError(CourseConsistencyError):
    """This worker's loaded generation is no longer the live one (503)."""

    def __init__(self, course_id: str, worker: int, database: int) -> None:
        self.course_id = course_id
        self.worker_generation = worker
        self.database_generation = database
        super().__init__(
            f'Course "{course_id}" moved from generation {worker} to '
            f"{database}; this worker is stale."
        )


class StaleFormError(CourseConsistencyError):
    """The submitted form no longer matches the served course/state (409)."""

    def __init__(self, course_id: str, reason: str) -> None:
        self.course_id = course_id
        self.reason = reason
        super().__init__(f'Stale form for course "{course_id}": {reason}')


@contextmanager
def guarded_learner_transaction(
    *,
    database: Database,
    course_repository: CourseRepository,
    state_repository: QuestionBankStateRepository,
    course_id: str,
    worker_generation: int,
    operation: str | None = None,
    form_context: Mapping[str, Any] | None = None,
) -> Iterator[None]:
    """Open the learner write transaction and fence it, or raise.

    Order inside the lock, exactly as designed::

        BEGIN IMMEDIATE
          check course enabled
          check worker loaded generation == DB generation
          validate form/progress identity
          (business operation)
          (learner writes)
        COMMIT
    """
    with database.transaction():
        if not course_repository.is_enabled(course_id):
            raise CourseServabilityChangedError(
                f'Course "{course_id}" is not currently servable.'
            )
        database_generation = state_repository.get_generation()
        if database_generation != worker_generation:
            raise StaleWorkerError(course_id, worker_generation, database_generation)
        if form_context is not None:
            _validate_form_context(
                course_id, operation, worker_generation, form_context
            )
        yield


def _validate_form_context(
    course_id: str,
    operation: str | None,
    worker_generation: int,
    form_context: Mapping[str, Any],
) -> None:
    """Re-confirm the signed form identity inside the transaction."""
    bound_course = form_context.get(FORM_CONTEXT_COURSE)
    if bound_course != course_id:
        raise StaleFormError(
            course_id, f"submitted for course {bound_course!r}"
        )
    if operation is not None and form_context.get(FORM_CONTEXT_OPERATION) != operation:
        raise StaleFormError(course_id, "operation no longer matches the page")
    bound_generation = form_context.get(FORM_CONTEXT_GENERATION)
    if bound_generation != worker_generation:
        raise StaleFormError(
            course_id,
            f"page was rendered at generation {bound_generation}, "
            f"worker serves {worker_generation}",
        )
