"""Discover and load per-course content from one course manifest per course.

Layout
------

```text
courses/
├── digital_ic/
│   ├── course.json
│   ├── questions.json
│   └── glossary.json
└── physical_design/
    ├── course.json
    ├── questions.json
    └── glossary.json
```

The root ``questions.json``/``glossary.json`` files stay supported through a
small *legacy adapter*: they are presented as one virtual
:class:`~app.models.course.CourseDefinition` with
:data:`~app.models.course.LEGACY_COURSE_ID`, and then flow through exactly the
same registry, repositories and services as a manifest course.  There is no
second business-logic path.

Safety rules enforced here:

* ``course_id`` must be a stable URL-safe lowercase ASCII slug;
* every declared content path is resolved relative to the manifest directory
  and must stay inside it (path traversal is rejected);
* request parameters are never concatenated into filesystem paths;
* a duplicate ``course_id`` is a *global* application-assembly error, while a
  single broken course is reported as a course-scoped
  :class:`~app.models.course.CourseLoadError`;
* ``glossary: null`` explicitly means "this course has no glossary"; a
  manifest that *declares* a glossary file which is missing or invalid is a
  course load failure, never a silent "no glossary".
"""

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.models import (
    LEGACY_COURSE_ID,
    SUPPORTED_COURSE_SCHEMA_VERSION,
    Course,
    CourseDefinition,
    CourseDefinitionError,
    CourseLoadError,
    Glossary,
    Question,
    validate_course_id,
)
from app.models.course import CourseIdError

from .glossary_loader import GlossaryError, GlossaryLoader
from .glossary_repository import GlossaryRepository
from .question_loader import QuestionBankError, QuestionLoader
from .question_repository import QuestionRepository

LOGGER = logging.getLogger(__name__)

MANIFEST_NAME = "course.json"
DEFAULT_QUESTIONS_NAME = "questions.json"
DEFAULT_GLOSSARY_NAME = "glossary.json"

LAYOUT_MANIFEST = "manifest"
LAYOUT_LEGACY = "legacy"


@dataclass(frozen=True)
class CourseBundle:
    """One fully loaded, immutable course publication.

    The repositories are read-only snapshots of the content files; the rest of
    the runtime (scoped persistence repositories, services) is assembled in
    ``app.course_runtime`` on top of this object.
    """

    definition: CourseDefinition
    question_repository: QuestionRepository
    glossary_repository: GlossaryRepository | None
    bank_version: str
    publication_identity: str

    @property
    def course(self) -> Course:
        return self.definition.course

    @property
    def course_id(self) -> str:
        return self.definition.course.course_id

    @property
    def questions(self) -> tuple[Question, ...]:
        return tuple(self.question_repository.get_all())

    def glossary(self) -> Glossary | None:
        """Return the parsed glossary, or ``None`` when the course has none."""
        if self.glossary_repository is None:
            return None
        return self.glossary_repository.glossary



class CourseLoader:
    """Discover course definitions and load their immutable content."""

    def __init__(
        self,
        courses_root: Path,
        *,
        legacy_directory: Path | None = None,
        legacy_questions_name: str = DEFAULT_QUESTIONS_NAME,
        legacy_glossary_name: str = DEFAULT_GLOSSARY_NAME,
    ) -> None:
        self.courses_root = Path(courses_root)
        self.legacy_directory = Path(legacy_directory) if legacy_directory else None
        self.legacy_questions_name = legacy_questions_name
        self.legacy_glossary_name = legacy_glossary_name

    # ------------------------------------------------------------------ discovery

    def discover_definitions(self) -> tuple[CourseDefinition, ...]:
        """Return every declared course definition, enabled or not.

        Raises :class:`CourseDefinitionError` on a global ambiguity such as two
        definitions claiming the same ``course_id``: application assembly must
        fail rather than pick one silently.
        """
        definitions: list[CourseDefinition] = []
        by_id: dict[str, CourseDefinition] = {}

        def register(definition: CourseDefinition) -> None:
            existing = by_id.get(definition.course_id)
            if existing is not None:
                raise CourseDefinitionError(
                    f'Duplicate course_id "{definition.course_id}" declared by '
                    f"{existing.manifest_path or existing.questions_path} and "
                    f"{definition.manifest_path or definition.questions_path}. "
                    "Every course_id must be unique."
                )
            by_id[definition.course_id] = definition
            definitions.append(definition)

        for definition in self._discover_manifest_definitions():
            register(definition)
        legacy = self._legacy_definition()
        if legacy is not None:
            register(legacy)
        return tuple(sorted(definitions, key=_definition_order))

    def enabled_definitions(self) -> tuple[CourseDefinition, ...]:
        """Return the definitions that should be loaded and served."""
        return tuple(
            definition
            for definition in self.discover_definitions()
            if definition.course.enabled
        )

    def find_definition(self, course_id: str) -> CourseDefinition | None:
        """Return one definition by ``course_id`` (still including disabled)."""
        try:
            validate_course_id(course_id)
        except CourseIdError:
            return None
        for definition in self.discover_definitions():
            if definition.course_id == course_id:
                return definition
        return None

    def _discover_manifest_definitions(self) -> list[CourseDefinition]:
        if not self.courses_root.is_dir():
            return []
        definitions: list[CourseDefinition] = []
        for entry in sorted(self.courses_root.iterdir(), key=lambda item: item.name):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            manifest_path = entry / MANIFEST_NAME
            if not manifest_path.is_file():
                LOGGER.warning(
                    "Ignoring course directory %s: no %s found.", entry, MANIFEST_NAME
                )
                continue
            definitions.append(self._parse_manifest(manifest_path))
        return definitions

    def _parse_manifest(self, manifest_path: Path) -> CourseDefinition:
        try:
            raw = manifest_path.read_bytes()
        except OSError as exc:
            raise CourseDefinitionError(
                f"Could not read course manifest {manifest_path}: {exc}"
            ) from exc
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise CourseDefinitionError(
                f"Invalid JSON in course manifest {manifest_path}: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise CourseDefinitionError(
                f"Course manifest root must be a JSON object: {manifest_path}"
            )

        schema_version = payload.get("schema_version")
        if (
            not isinstance(schema_version, int)
            or isinstance(schema_version, bool)
            or schema_version != SUPPORTED_COURSE_SCHEMA_VERSION
        ):
            raise CourseDefinitionError(
                'Course manifest "schema_version" must be '
                f"{SUPPORTED_COURSE_SCHEMA_VERSION}: {manifest_path}"
            )
        try:
            course_id = validate_course_id(payload.get("course_id"))
        except CourseIdError as exc:
            raise CourseDefinitionError(f"{manifest_path}: {exc}") from exc

        title = payload.get("title")
        if not isinstance(title, str) or not title.strip():
            raise CourseDefinitionError(
                f'Course manifest "title" must be a non-empty string: {manifest_path}'
            )
        title_zh = payload.get("title_zh", "")
        if not isinstance(title_zh, str):
            raise CourseDefinitionError(
                f'Course manifest "title_zh" must be a string: {manifest_path}'
            )
        enabled = payload.get("enabled")
        if not isinstance(enabled, bool):
            raise CourseDefinitionError(
                f'Course manifest "enabled" must be a boolean: {manifest_path}'
            )
        order = payload.get("order", 0)
        if not isinstance(order, int) or isinstance(order, bool):
            raise CourseDefinitionError(
                f'Course manifest "order" must be an integer: {manifest_path}'
            )

        manifest_dir = manifest_path.parent.resolve()
        questions_path = self._resolve_content_path(
            payload, "questions", manifest_dir, manifest_path, required=True
        )
        glossary_path = self._resolve_content_path(
            payload, "glossary", manifest_dir, manifest_path, required=False
        )
        assert questions_path is not None  # required=True
        return CourseDefinition(
            course=Course(
                course_id=course_id,
                title=title.strip(),
                title_zh=title_zh.strip(),
                enabled=enabled,
                order=order,
            ),
            root=manifest_dir,
            questions_path=questions_path,
            glossary_path=glossary_path,
            manifest_path=manifest_path.resolve(),
            layout=LAYOUT_MANIFEST,
        )

    @staticmethod
    def _resolve_content_path(
        payload: dict[str, Any],
        field: str,
        manifest_dir: Path,
        manifest_path: Path,
        *,
        required: bool,
    ) -> Path | None:
        """Resolve and containment-check one declared content path.

        ``None`` is only valid when the key is explicitly present and null
        (``glossary: null``).  A missing ``glossary`` key is a manifest error
        so "this course has no glossary" is always an explicit, reviewable
        declaration.  Declared paths are relative and must stay inside the
        course directory, which rejects ``..`` traversal and symlink escapes.
        """
        if field not in payload:
            if field == "glossary":
                raise CourseDefinitionError(
                    'Course manifest must declare "glossary" explicitly '
                    f"(a filename or null): {manifest_path}"
                )
            raise CourseDefinitionError(
                f'Course manifest is missing required "{field}": {manifest_path}'
            )
        raw_value = payload[field]
        if raw_value is None:
            if required:
                raise CourseDefinitionError(
                    f'Course manifest "{field}" must name a file: {manifest_path}'
                )
            return None
        if not isinstance(raw_value, str) or not raw_value.strip():
            raise CourseDefinitionError(
                f'Course manifest "{field}" must be a non-empty string or null: '
                f"{manifest_path}"
            )
        declared = raw_value.strip().replace("\\", "/")
        relative = Path(declared)
        if relative.is_absolute() or ".." in relative.parts:
            raise CourseDefinitionError(
                f'Course manifest "{field}" must be a relative path inside the '
                f"course directory: {manifest_path}"
            )
        candidate = manifest_dir / relative
        # ``resolve()`` collapses symlinks before the containment check, so a
        # symlink pointing outside the course directory is rejected too.
        try:
            resolved = candidate.resolve()
        except OSError as exc:
            raise CourseDefinitionError(
                f'Course manifest "{field}" cannot be resolved: {manifest_path} ({exc})'
            ) from exc
        if not _is_contained(resolved, manifest_dir):
            raise CourseDefinitionError(
                f'Course manifest "{field}" escapes the course directory: '
                f"{manifest_path}"
            )
        return resolved

    def _legacy_definition(self) -> CourseDefinition | None:
        """Adapt the root ``questions.json`` files into one virtual course."""
        if self.legacy_directory is None:
            return None
        legacy_dir = self.legacy_directory.resolve()
        questions_path = legacy_dir / self.legacy_questions_name
        if not questions_path.is_file():
            return None
        glossary_candidate = legacy_dir / self.legacy_glossary_name
        glossary_path = (
            glossary_candidate.resolve() if glossary_candidate.is_file() else None
        )
        title, title_zh = self._peek_legacy_titles(questions_path)
        return CourseDefinition(
            course=Course(
                course_id=LEGACY_COURSE_ID,
                title=title,
                title_zh=title_zh,
                enabled=True,
                order=-1_000_000,
            ),
            root=legacy_dir,
            questions_path=questions_path.resolve(),
            glossary_path=glossary_path,
            manifest_path=None,
            layout=LAYOUT_LEGACY,
        )

    @staticmethod
    def _peek_legacy_titles(questions_path: Path) -> tuple[str, str]:
        """Best-effort titles for a legacy bank used only for navigation.

        Failures are ignored on purpose: a broken legacy bank must surface as a
        course-scoped load error from :meth:`load_bundle`, not as a crash while
        the course list is being built.
        """
        try:
            payload = json.loads(questions_path.read_bytes())
            title = payload.get("title")
            title_zh = payload.get("title_zh", "")
        except Exception:  # noqa: BLE001 - navigation metadata is best effort
            return "Legacy question bank", ""
        if not isinstance(title, str) or not title.strip():
            return "Legacy question bank", ""
        return title.strip(), title_zh.strip() if isinstance(title_zh, str) else ""

    # --------------------------------------------------------------------- loading

    def load_bundle(self, definition: CourseDefinition) -> CourseBundle:
        """Load one course's immutable content.

        Raises :class:`CourseLoadError` when the course's content is broken.
        The caller must treat that as "this course is unavailable" and never as
        "this course has no questions": an empty bank would make learner
        reconciliation delete that course's stored history.
        """
        if not definition.questions_path.is_file():
            raise CourseLoadError(
                definition.course_id,
                f"question bank not found at {definition.questions_path}"
                + (
                    " (declared by course.json)"
                    if definition.manifest_path is not None
                    else ""
                ),
            )
        loader = QuestionLoader(definition.questions_path)
        try:
            questions = loader.load()
        except QuestionBankError as exc:
            raise CourseLoadError(definition.course_id, str(exc)) from exc
        if loader.source_fingerprint is None:
            raise CourseLoadError(
                definition.course_id, "question bank fingerprint was not generated"
            )

        glossary_repository: GlossaryRepository | None = None
        if definition.glossary_path is not None:
            if not definition.glossary_path.is_file():
                # A *declared* glossary that vanished is a failure: silently
                # serving no glossary would hide a broken publication.
                raise CourseLoadError(
                    definition.course_id,
                    f"declared glossary not found at {definition.glossary_path}",
                )
            try:
                glossary = GlossaryLoader(definition.glossary_path).load()
            except GlossaryError as exc:
                raise CourseLoadError(definition.course_id, str(exc)) from exc
            glossary_repository = GlossaryRepository(glossary)

        question_repository = QuestionRepository(
            questions,
            title=loader.title,
            title_zh=loader.title_zh,
            sources=loader.sources,
            chapters=loader.chapters,
        )
        # The manifest is authoritative when it exists, so a course's displayed
        # name cannot change depending on whether it happened to load: a disabled
        # course shows the manifest title and so does an enabled one.  The legacy
        # adapter has no manifest, so its question bank names the course.
        if definition.manifest_path is None:
            course = Course(
                course_id=definition.course.course_id,
                title=loader.title or definition.course.title,
                title_zh=loader.title_zh or definition.course.title_zh,
                enabled=definition.course.enabled,
                order=definition.course.order,
            )
        else:
            course = definition.course
        resolved_definition = CourseDefinition(
            course=course,
            root=definition.root,
            questions_path=definition.questions_path,
            glossary_path=definition.glossary_path,
            manifest_path=definition.manifest_path,
            layout=definition.layout,
        )
        return CourseBundle(
            definition=resolved_definition,
            question_repository=question_repository,
            glossary_repository=glossary_repository,
            bank_version=loader.source_fingerprint,
            publication_identity=self.publication_identity(resolved_definition),
        )

    def publication_identity(self, definition: CourseDefinition) -> str:
        """Hash the on-disk publication a definition currently points at.

        Recomputing this inside the question-bank sync transaction detects the
        "preloaded old files, then a newer publication landed" race: if the
        identity changed, the caller must discard the preloaded snapshot and
        reload instead of syncing stale content into the database.
        """
        digest = hashlib.sha256()
        for path in definition.content_paths():
            digest.update(path.name.encode("utf-8"))
            digest.update(b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
        return digest.hexdigest()


def _is_contained(candidate: Path, parent: Path) -> bool:
    """Return whether ``candidate`` lives inside ``parent``."""
    try:
        return candidate == parent or candidate.is_relative_to(parent)
    except ValueError:  # pragma: no cover - defensive for exotic paths
        return False


def _definition_order(definition: CourseDefinition) -> tuple[int, str]:
    return (definition.course.order, definition.course.course_id)

