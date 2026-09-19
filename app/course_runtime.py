"""Worker-side course runtime: registry, states, and app-wide services.

Everything in this module is **read-only after startup**.  A worker loads each
enabled course once, reconciles it once, and then holds a fixed generation for
its whole lifetime.  Serving a request never reloads global configuration,
overwrites a file, restarts a process or looks up a process-wide "current
course": the course always comes from the request URL.

Failure isolation is per course:

* one broken course becomes ``unavailable``, keeps its historical state
  untouched, and is never served or synced;
* every other course keeps working;
* a *global* ambiguity — a duplicate ``course_id``, an unreadable manifest
  directory — fails application assembly instead, because no worker may guess
  which definition is real.

``AppServices`` deliberately holds only deployment-wide resources plus the
course access entry point.  It does not expose a single global
``question_repository``: content is only reachable through a course.
"""

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import tzinfo
from enum import Enum

from app.models import LEGACY_COURSE_ID, Course, CourseLoadError
from app.repositories import (
    CourseBundle,
    CourseLoader,
    CourseRepository,
    Database,
    QuestionBankError,
    RateLimitRepository,
    UserRepository,
)

from .services.course_service import (
    CourseServices,
    assemble_course_services,
    synchronize_course,
)
from .services.question_bank_sync_service import CoursePublicationChangedError

LOGGER = logging.getLogger(__name__)


class CourseStatus(str, Enum):
    """How one course is doing on *this* worker."""

    #: Loaded, and its generation matches the database.
    READY = "ready"
    #: Loaded, but the database has moved on: learning traffic is fenced.
    STALE = "stale"
    #: Enabled and declared here, but its content could not be loaded.
    UNAVAILABLE = "unavailable"
    #: Declared with ``enabled: false``; intentionally not served.
    DISABLED = "disabled"
    #: Known to the database but not declared in this worker's catalogue.
    UNDEPLOYED = "undeployed"

    @property
    def servable(self) -> bool:
        return self is CourseStatus.READY


class CourseNotFoundError(LookupError):
    """Raised when no course definition matches the requested ``course_id``."""

    def __init__(self, course_id: str) -> None:
        self.course_id = course_id
        super().__init__(f'Unknown course "{course_id}".')


class CourseUnavailableError(RuntimeError):
    """Raised when a known course cannot be served on this worker."""

    def __init__(self, course_id: str, reason: str) -> None:
        self.course_id = course_id
        self.reason = reason
        super().__init__(f'Course "{course_id}" is unavailable: {reason}')


class CourseDisabledError(CourseUnavailableError):
    """Raised when a course is switched off in the catalogue."""


@dataclass(frozen=True)
class CourseState:
    """One course as this worker sees it."""

    course: Course
    status: CourseStatus
    generation: int | None = None
    database_generation: int | None = None
    reason: str = ""

    @property
    def course_id(self) -> str:
        return self.course.course_id

    @property
    def servable(self) -> bool:
        return self.status.servable and self.generation is not None

    @property
    def declared(self) -> bool:
        """Whether this worker's catalogue declares the course at all."""
        return self.status is not CourseStatus.UNDEPLOYED


class CourseRegistry:
    """Read-only ``course_id -> CourseServices`` lookup for one worker."""

    def __init__(
        self,
        *,
        services: dict[str, CourseServices],
        states: dict[str, CourseState],
        default_course_id: str | None,
    ) -> None:
        self._services = dict(services)
        self._states = dict(states)
        self.default_course_id = default_course_id

    # ------------------------------------------------------------------ lookup

    def get(self, course_id: str) -> CourseServices:
        """Return the services of one servable course, or raise.

        ``CourseNotFoundError`` means "no such course" (404); a
        :class:`CourseUnavailableError` means "known but not servable here"
        (503).  The distinction matters: a 404 must never be softened into a
        fallback to another course's content.
        """
        state = self._states.get(course_id)
        if state is None:
            raise CourseNotFoundError(course_id)
        if state.status is CourseStatus.DISABLED:
            raise CourseDisabledError(course_id, "the course is disabled")
        services = self._services.get(course_id)
        if services is None:
            raise CourseUnavailableError(
                course_id, state.reason or f"status is {state.status.value}"
            )
        if state.status is not CourseStatus.READY:
            raise CourseUnavailableError(
                course_id, f"status is {state.status.value}"
            )
        return services

    def find(self, course_id: str) -> CourseServices | None:
        """Return the services of a servable course, or ``None``."""
        try:
            return self.get(course_id)
        except (CourseNotFoundError, CourseUnavailableError):
            return None

    def loaded_services(self, course_id: str) -> CourseServices | None:
        """Return the loaded services even when the course is fenced.

        Only for the deliberately stale-exempt read-only pages (the reference
        glossary): those must stay reachable on a superseded worker, and they
        must read *that course's* snapshot rather than another course's.
        """
        return self._services.get(course_id)

    def state(self, course_id: str) -> CourseState | None:
        """Return one course's state, recomputed against the live database."""
        state = self._states.get(course_id)
        if state is None:
            return None
        return self._with_live_status(state)

    def has(self, course_id: str) -> bool:
        return course_id in self._states

    def states(self) -> list[CourseState]:
        """Return every known course in catalogue order, with live staleness.

        A loaded course's generation is compared with the database *now*, so a
        worker that was READY at startup and has since been superseded by a
        sibling's structural publication reports ``stale`` without any request
        handling being involved.
        """
        return sorted(
            (self._with_live_status(state) for state in self._states.values()),
            key=lambda state: (state.course.order, state.course_id),
        )

    def _with_live_status(self, state: CourseState) -> CourseState:
        if state.status is not CourseStatus.READY:
            return state
        services = self._services.get(state.course_id)
        if services is None:
            return state
        try:
            live_database_generation = (
                services.question_bank_state_repository.get_generation()
            )
        except Exception:  # noqa: BLE001 - diagnostics must never break routing
            return state
        if live_database_generation == state.generation:
            return state
        return CourseState(
            course=state.course,
            status=CourseStatus.STALE,
            generation=state.generation,
            database_generation=live_database_generation,
            reason=(
                f"the database generation is {live_database_generation} but this "
                f"worker loaded {state.generation}"
            ),
        )

    def declared_states(self) -> list[CourseState]:
        """Return the courses this worker's catalogue declares."""
        return [state for state in self.states() if state.declared]

    def ready_course_ids(self) -> list[str]:
        """Return the IDs of the courses this worker can serve right now."""
        return [
            state.course_id
            for state in self.states()
            if state.servable and state.status is CourseStatus.READY
        ]

    def enabled_declared_course_ids(self) -> list[str]:
        """Return declared, enabled course IDs, including unavailable ones.

        Readiness is judged against exactly this set: a course may not be
        missing merely because its content is broken — that is precisely the
        condition that must make the aggregate ``/ready`` report 503.  A course
        that only exists in the database (``undeployed``) is not part of this
        worker's contract and never affects its readiness.
        """
        return [
            state.course_id
            for state in self.declared_states()
            if state.course.enabled
        ]

    def resolve_default_course_id(self, preference: str | None = None) -> str | None:
        """Pick the navigation target for a URL without an explicit course.

        Order: the caller's valid preference (e.g. this browser's last course),
        then the configured default, then the first servable course, then the
        first declared enabled course.  A course that is unavailable is still
        returned so the learner gets an honest 503 rather than silently landing
        in a different course.
        """
        for candidate in (preference, self.default_course_id):
            if candidate and self.has(candidate):
                return candidate
        for state in self.states():
            if state.servable:
                return state.course_id
        for state in self.declared_states():
            if state.course.enabled:
                return state.course_id
        return None


    # ------------------------------------------------------------------ readiness

    def readiness(self) -> tuple[bool, dict[str, object]]:
        """Return this worker's aggregate readiness and its diagnostics.

        Aggregate readiness covers *this worker's* declared, enabled courses.
        A stale course A makes the aggregate report 503 without making course
        B's routes fail: the aggregate is a monitoring signal, not a routing
        decision.
        """
        declared = self.enabled_declared_course_ids()
        courses: dict[str, dict[str, object]] = {}
        for state in self.states():
            if not state.declared or not state.course.enabled:
                continue
            courses[state.course_id] = {
                "status": state.status.value,
                "worker_generation": state.generation,
                "database_generation": state.database_generation,
                "reason": state.reason,
            }
        ready = bool(declared) and all(
            entry["status"] == CourseStatus.READY.value for entry in courses.values()
        )
        payload: dict[str, object] = {
            "status": "ready" if ready else "degraded",
            "courses": courses,
            "enabled_course_count": len(declared),
            "ready_course_count": len(self.ready_course_ids()),
        }
        if not declared:
            payload["reason"] = "no enabled course is declared on this worker"
        return ready, payload

    def course_readiness(self, course_id: str) -> tuple[bool, dict[str, object]]:
        """Return one course's readiness and diagnostics."""
        state = self.state(course_id)
        if state is None:
            return False, {
                "status": "unknown",
                "reason": "no course definition with this id",
            }
        return state.status is CourseStatus.READY, {
            "status": state.status.value,
            "course_id": state.course_id,
            "worker_generation": state.generation,
            "database_generation": state.database_generation,
            "reason": state.reason,
        }


@dataclass(frozen=True)
class AppServices:
    """Deployment-wide resources plus the course access entry point.

    Only genuinely global things live here: the shared SQLite database, the
    account and rate-limit repositories, the permanent course identity table,
    and the read-only course registry.  There is deliberately no global
    ``question_repository`` (or learner repository): content and learner state
    are only reachable through a course, so a request cannot accidentally act
    on "whatever bank happens to be loaded".
    """

    database: Database
    user_repository: UserRepository
    rate_limit_repository: RateLimitRepository
    course_repository: CourseRepository
    course_registry: CourseRegistry
    display_timezone: tzinfo
    legacy_course_id: str = LEGACY_COURSE_ID

    @property
    def default_services(self) -> CourseServices:
        """Return the services of the resolved default course.

        Convenience for tooling and integration tests that operate on the single
        default course; request handling always resolves the course from the URL
        instead.
        """
        course_id = self.course_registry.resolve_default_course_id()
        if course_id is None:
            raise CourseUnavailableError(
                "<default>", "no enabled course is declared on this worker"
            )
        return self.course_registry.get(course_id)

    def course(self, course_id: str) -> CourseServices:
        return self.course_registry.get(course_id)




def build_course_registry(
    *,
    loader: CourseLoader,
    database: Database,
    course_repository: CourseRepository,
    knowledge_verification_target: int,
    display_timezone: tzinfo,
    user_repository: UserRepository,
    default_course_id: str | None = None,
) -> CourseRegistry:
    """Load every enabled course and build this worker's registry.

    A duplicate ``course_id`` or an unreadable manifest directory propagates as
    a global assembly failure.  A single broken course is recorded as
    ``unavailable`` and left completely alone: its content is not served, its
    question bank is not synced, and its learner state is not reconciled.
    """
    definitions = loader.discover_definitions()
    services: dict[str, CourseServices] = {}
    states: dict[str, CourseState] = {}

    for definition in definitions:
        course = definition.course
        if not course.enabled:
            states[course.course_id] = CourseState(course, CourseStatus.DISABLED)
            continue
        course_repository.accept(course)
        loaded = _load_and_synchronize(
            definition=definition,
            loader=loader,
            database=database,
            course_repository=course_repository,
            knowledge_verification_target=knowledge_verification_target,
            display_timezone=display_timezone,
            user_repository=user_repository,
        )
        if isinstance(loaded, CourseState):
            states[course.course_id] = loaded
            continue
        services[course.course_id] = loaded
        states[course.course_id] = CourseState(
            course=loaded.course,
            status=CourseStatus.READY,
            generation=loaded.generation,
            database_generation=(
                loaded.question_bank_state_repository.get_generation()
            ),
        )

    _record_undeployed_courses(course_repository, states)
    persisted_default = course_repository.default_course_id()
    return CourseRegistry(
        services=services,
        states=states,
        default_course_id=default_course_id or persisted_default,
    )


def _load_and_synchronize(
    *,
    definition,
    loader: CourseLoader,
    database: Database,
    course_repository: CourseRepository,
    knowledge_verification_target: int,
    display_timezone: tzinfo,
    user_repository: UserRepository,
) -> CourseServices | CourseState:
    """Load, assemble and reconcile one course, retrying once on republish.

    Returns the ready services, or a non-ready :class:`CourseState` describing
    the course-scoped failure.  The retry covers the startup/publication race:
    if the publication changed while this worker waited for the write lock, the
    preloaded snapshot is discarded and the course is loaded again.
    """
    attempts = 2
    last_reason = "unknown error"
    for attempt in range(attempts):
        try:
            bundle: CourseBundle = loader.load_bundle(definition)
        except CourseLoadError as exc:
            LOGGER.error(
                "Course %s is unavailable: %s", definition.course_id, exc.reason
            )
            return CourseState(
                course=definition.course,
                status=CourseStatus.UNAVAILABLE,
                reason=exc.reason,
            )
        course_services = assemble_course_services(
            bundle,
            database=database,
            knowledge_verification_target=knowledge_verification_target,
            display_timezone=display_timezone,
            user_repository=user_repository,
        )
        try:
            reconciled = synchronize_course(
                course_services,
                publication_identity=lambda: loader.publication_identity(definition),
            )
        except CoursePublicationChangedError:
            if attempt + 1 < attempts:
                LOGGER.warning(
                    "Course %s was republished during startup; reloading.",
                    definition.course_id,
                )
                continue
            last_reason = "the course was republished repeatedly during startup"
            break
        except CourseLoadError as exc:
            last_reason = exc.reason
            break
        except QuestionBankError as exc:
            last_reason = str(exc)
            break
        except ValueError:
            # A mis-wired service graph must never be tolerated silently.
            raise
        LOGGER.info(
            'Course "%s" ready at generation %d (%d questions).',
            reconciled.course_id,
            reconciled.generation,
            len(reconciled.question_repository.get_all()),
        )
        return reconciled
    LOGGER.error('Course "%s" is unavailable: %s', definition.course_id, last_reason)
    return CourseState(
        course=definition.course,
        status=CourseStatus.UNAVAILABLE,
        reason=last_reason,
    )


def _record_undeployed_courses(
    course_repository: CourseRepository, states: dict[str, CourseState]
) -> None:
    """Record database-known courses that this worker does not declare.

    They keep their historical learner state exactly as it is, but they are not
    part of this worker's serving contract: a course that is not declared here
    can never be reinterpreted as another course's content, and it does not
    affect aggregate readiness.
    """
    for record in course_repository.list_accepted():
        course_id = record.course.course_id
        if course_id in states:
            continue
        states[course_id] = CourseState(
            course=record.course,
            status=CourseStatus.UNDEPLOYED,
            reason="not present in this worker's course catalogue",
        )
