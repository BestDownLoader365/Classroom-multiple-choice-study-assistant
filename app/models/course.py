"""Course identity, manifests, and load definitions.

A *course* is the permanent namespace of every piece of learning content and
learning history in the application.  Its identity is a stable, URL-safe,
lowercase ASCII slug (``course_id``); everything local to a course — question
IDs, chapter IDs, source IDs, glossary term IDs — is only unique *inside* that
namespace.

Two course IDs are conceptually different and must never be conflated:

``LEGACY_COURSE_ID``
    The fixed namespace that every row of a database created before the
    multi-course refactor belongs to.  It is decided once, persisted by the
    schema migration, and can never be re-assigned afterwards.

``DEFAULT_COURSE_ID`` (a navigation *preference*, not an identity)
    The course a browser without an explicit course URL should be sent to.
    Changing it never re-owns historical data.

The module is deliberately free of filesystem and Flask imports: it only
describes the data, while ``app.repositories.course_loader`` performs the I/O.
"""

import re
from dataclasses import dataclass
from pathlib import Path

#: Namespace of all data written before the multi-course migration.
LEGACY_COURSE_ID = "legacy"

#: ``course_id`` is a stable URL-safe lowercase ASCII slug allowing ``_``/``-``.
COURSE_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:[_-][a-z0-9]+)*$")

#: Longest accepted ``course_id``; keeps URLs and directory names sane.
COURSE_ID_MAX_LENGTH = 64

#: Manifest schema version understood by this build.
SUPPORTED_COURSE_SCHEMA_VERSION = 1


class CourseIdError(ValueError):
    """Raised when a string is not a valid ``course_id``."""


class CourseLoadError(RuntimeError):
    """Raised when one course's content cannot be loaded safely.

    A course-scoped failure must never degrade into "this course has an empty
    question bank": learner reconciliation would then delete every stored
    progress record of that course.  The failure is reported as-is and the
    course is served as ``unavailable``.
    """

    def __init__(self, course_id: str, reason: str) -> None:
        self.course_id = course_id
        self.reason = reason
        super().__init__(f'Course "{course_id}" could not be loaded: {reason}')


class CourseDefinitionError(RuntimeError):
    """Raised for a global catalogue ambiguity (e.g. duplicate ``course_id``).

    These are application-assembly failures: no worker may guess which of two
    conflicting definitions is the real course.
    """


def validate_course_id(course_id: object) -> str:
    """Return ``course_id`` unchanged when valid, else raise :class:`CourseIdError`."""
    if not isinstance(course_id, str) or not course_id:
        raise CourseIdError('"course_id" must be a non-empty string.')
    if len(course_id) > COURSE_ID_MAX_LENGTH:
        raise CourseIdError(
            f'"course_id" must be at most {COURSE_ID_MAX_LENGTH} characters.'
        )
    if not COURSE_ID_PATTERN.match(course_id):
        raise CourseIdError(
            f'"{course_id}" is not a valid course_id: use lowercase ASCII '
            "letters and digits, optionally separated by '_' or '-'."
        )
    return course_id


def is_valid_course_id(course_id: object) -> bool:
    """Return whether ``course_id`` is an acceptable course slug."""
    try:
        validate_course_id(course_id)
    except CourseIdError:
        return False
    return True


@dataclass(frozen=True)
class Course:
    """Immutable course metadata accepted from one manifest.

    ``enabled`` is a *catalogue* property read from the manifest: a disabled
    course is intentionally not loaded at all.  It is not the same thing as a
    runtime *unavailable* course, which is enabled but failed to load.
    """

    course_id: str
    title: str
    title_zh: str = ""
    enabled: bool = True
    order: int = 0

    def __post_init__(self) -> None:
        validate_course_id(self.course_id)

    @property
    def display_title(self) -> str:
        """Return the bilingual label used in navigation and page headers."""
        if self.title_zh and self.title_zh != self.title:
            return f"{self.title} · {self.title_zh}"
        return self.title


@dataclass(frozen=True)
class CourseDefinition:
    """Where one course's content lives and how it was declared.

    ``layout`` is ``"manifest"`` for the ``courses/<course_id>/course.json``
    layout and ``"legacy"`` for the root ``questions.json``/``glossary.json``
    compatibility adapter.  Both layouts feed exactly the same registry,
    scoped repositories and service assembly; there is no second business
    logic path.

    Paths are already resolved and containment-checked by the loader.
    """

    course: Course
    root: Path
    questions_path: Path
    glossary_path: Path | None
    manifest_path: Path | None = None
    layout: str = "manifest"

    @property
    def course_id(self) -> str:
        return self.course.course_id

    @property
    def declares_glossary(self) -> bool:
        """Whether the course explicitly declared a glossary file."""
        return self.glossary_path is not None

    def content_paths(self) -> tuple[Path, ...]:
        """Return every content file that defines the publication identity."""
        paths = [self.questions_path]
        if self.glossary_path is not None:
            paths.append(self.glossary_path)
        if self.manifest_path is not None:
            paths.append(self.manifest_path)
        return tuple(paths)
