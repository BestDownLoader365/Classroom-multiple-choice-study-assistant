"""Shared, thin tooling layer for the course-level CLI scripts.

The scripts in this directory are *adapters*: they resolve a course, call the
application's own loader/diff/sync code, and perform the filesystem side of a
publication.  No business rule is duplicated here — a rule change belongs in
``app/`` and is picked up automatically.

Publication model
-----------------

A publish is two separate, individually-atomic steps, and the tooling never
pretends otherwise:

1. **filesystem publish** — the frozen candidate bytes are written to an
   immutable, content-addressed location and the course manifest is switched
   over with ``os.replace`` (a single rename on the same filesystem), then the
   directory is fsynced;
2. **database activation** — a worker restart loads the new publication and the
   startup reconciliation moves the course's generation.

Filesystem publication is *not* a joint transaction with SQLite.  The tooling
therefore reports "published, pending worker activation" rather than claiming
the deployment is complete, and it never bumps the generation itself.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.models import (  # noqa: E402
    CourseDefinition,
    CourseDefinitionError,
)
from app.repositories import CourseLoader  # noqa: E402

VERSIONS_DIRECTORY = "versions"
LOCK_FILE_NAME = ".publish.lock"


class ToolingError(RuntimeError):
    """Raised when a course operation cannot be performed safely."""


def build_loader(
    courses_dir: Path, question_file: Path, glossary_file: Path
) -> CourseLoader:
    """Build the application's own course loader for the CLI."""
    return CourseLoader(
        courses_dir,
        legacy_directory=question_file.parent,
        legacy_questions_name=question_file.name,
        legacy_glossary_name=glossary_file.name,
    )


def resolve_definition(
    loader: CourseLoader, course_id: str | None, *, require_explicit: bool = False
) -> CourseDefinition:
    """Resolve the one course a command applies to."""
    definitions = loader.enabled_definitions()
    if course_id:
        definition = loader.find_definition(course_id)
        if definition is None:
            declared = ", ".join(
                item.course_id for item in loader.discover_definitions()
            )
            raise ToolingError(
                f'Unknown course "{course_id}". Declared courses: '
                f"{declared or '(none)'}."
            )
        return definition
    if require_explicit:
        raise ToolingError(
            "--course <course_id> is required: a formal multi-course publish must "
            "name the target course explicitly."
        )
    if len(definitions) == 1:
        return definitions[0]
    if not definitions:
        raise ToolingError(
            "No enabled course is declared. Add courses/<course_id>/course.json or "
            "place a root questions.json."
        )
    raise ToolingError(
        "Several courses are declared, so the target is ambiguous. Pass "
        "--course <course_id>."
    )


def freeze_candidate(path: Path) -> tuple[bytes, str]:
    """Read a candidate file exactly once and fingerprint those bytes.

    Validation and publication both work on the returned bytes, so a file that
    changes between the two steps can never publish content that was not the
    content that was validated.
    """
    if not path.is_file():
        raise ToolingError(f"Candidate file not found: {path}")
    payload = path.read_bytes()
    return payload, hashlib.sha256(payload).hexdigest()


def validate_bytes(payload: bytes, *, suffix: str = "questions.json") -> list:
    """Validate frozen question-bank bytes by loading them from a private copy."""
    from app.repositories import QuestionBankError, QuestionLoader

    with tempfile.TemporaryDirectory() as directory:
        scratch = Path(directory) / suffix
        scratch.write_bytes(payload)
        loader = QuestionLoader(scratch)
        try:
            questions = loader.load()
        except QuestionBankError as exc:
            raise ToolingError(
                f"Candidate question bank failed validation:\n{exc}"
            ) from exc
        if loader.source_fingerprint is None:  # pragma: no cover - defensive
            raise ToolingError("Candidate question bank fingerprint was not generated.")
        return questions


def validate_glossary_bytes(payload: bytes):
    """Validate frozen glossary bytes by loading them from a private copy."""
    from app.repositories import GlossaryError, GlossaryLoader

    with tempfile.TemporaryDirectory() as directory:
        scratch = Path(directory) / "glossary.json"
        scratch.write_bytes(payload)
        try:
            return GlossaryLoader(scratch).load()
        except GlossaryError as exc:
            raise ToolingError(f"Candidate glossary failed validation:\n{exc}") from exc


def read_manifest(definition: CourseDefinition) -> dict:
    """Return the parsed manifest, or a synthesized one for the legacy layout."""
    if definition.manifest_path is None:
        return {
            "schema_version": 1,
            "course_id": definition.course_id,
            "title": definition.course.title,
            "title_zh": definition.course.title_zh,
            "enabled": definition.course.enabled,
            "questions": definition.questions_path.name,
            "glossary": (
                definition.glossary_path.name
                if definition.glossary_path is not None
                else None
            ),
            "order": definition.course.order,
        }
    return json.loads(definition.manifest_path.read_text(encoding="utf-8"))


# ------------------------------------------------------------------- filesystem


def fsync_directory(directory: Path) -> None:
    """Persist a rename itself, where the filesystem supports it."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(directory, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def write_file_atomically(target: Path, payload: bytes, *, mode: int = 0o644) -> None:
    """Write ``payload`` next to ``target`` and swap it in with one rename."""
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, target)
        fsync_directory(target.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


@contextmanager
def publication_lock(course_root: Path):
    """Hold the course's publication lock while a baseline is re-validated.

    The lock file lives inside the course directory so every publisher of that
    course serialises on the same inode.  It is advisory: a publisher that does
    not take it can still corrupt a publication, which is exactly why the
    baseline is re-validated *inside* the lock as well.
    """
    import fcntl

    course_root.mkdir(parents=True, exist_ok=True)
    lock_path = course_root / LOCK_FILE_NAME
    handle = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield lock_path
    finally:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            os.close(handle)


def file_digest(path: Path) -> str:
    """Return the SHA-256 of a file, or an empty string when it is missing."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def publication_digest(definition: CourseDefinition) -> str:
    """Hash everything a publication consists of for one course.

    The baseline a publish re-validates under its lock must cover the manifest
    *and* the content it points at: a concurrent publisher may have changed
    either, and switching over on top of a half-changed publication would leave
    the course pointing at content nobody validated.
    """
    digest = hashlib.sha256()
    for path in definition.content_paths():
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _revalidate_baseline(
    definition: CourseDefinition, baseline: tuple[str, str] | None
) -> None:
    """Refuse to switch over when the publication moved under us."""
    if baseline is None:
        return
    _label, baseline_digest = baseline
    current = publication_digest(definition)
    if current != baseline_digest:
        raise ToolingError(
            "The publication changed while this command was running "
            f"(baseline was {baseline_digest[:12]}..., now "
            f"{current[:12] or 'missing'}...). Nothing was switched over; re-run "
            "the preflight."
        )



def publish_questions(
    definition: CourseDefinition,
    payload: bytes,
    digest: str,
    *,
    baseline: tuple[str, str] | None = None,
) -> tuple[Path, bool]:
    """Publish frozen question-bank bytes for one course.

    Writes an immutable, content-addressed copy under ``versions/<digest>/`` and
    then switches the course manifest over to it with one atomic rename.  The
    legacy (manifest-less) layout falls back to an atomic replace of its single
    questions file, which is the only thing it has.

    ``baseline`` is the ``(path, digest)`` pair recorded during preflight; it is
    re-validated inside the publication lock so a concurrent publisher cannot be
    silently overwritten.  Returns ``(published_path, legacy_layout)``.
    """
    course_root = definition.root
    versioned_questions = course_root / VERSIONS_DIRECTORY / digest / "questions.json"
    with publication_lock(course_root):
        _revalidate_baseline(definition, baseline)
        if file_digest(versioned_questions) != digest:
            write_file_atomically(versioned_questions, payload)
        if definition.manifest_path is None:
            # Legacy layout: one file and no manifest to switch over.
            write_file_atomically(definition.questions_path, payload)
            return definition.questions_path, True
        manifest = read_manifest(definition)
        manifest["questions"] = f"{VERSIONS_DIRECTORY}/{digest}/questions.json"
        if definition.glossary_path is not None:
            manifest["glossary"] = definition.glossary_path.relative_to(
                course_root
            ).as_posix()
        write_file_atomically(
            definition.manifest_path,
            json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8") + b"\n",
        )
        return versioned_questions, False


def publish_glossary(
    definition: CourseDefinition,
    payload: bytes,
    digest: str,
    *,
    baseline: tuple[str, str] | None = None,
) -> Path:
    """Publish frozen glossary bytes for one course, atomically.

    The glossary never participates in the question-bank generation, so this is
    a pure content switch: learners keep their state and no worker is fenced.
    """
    course_root = definition.root
    versioned_glossary = course_root / VERSIONS_DIRECTORY / digest / "glossary.json"
    with publication_lock(course_root):
        _revalidate_baseline(definition, baseline)
        if file_digest(versioned_glossary) != digest:
            write_file_atomically(versioned_glossary, payload)
        if definition.manifest_path is None:
            target = course_root / definition.glossary_path.name
            write_file_atomically(target, payload)
            return target
        manifest = read_manifest(definition)
        manifest["glossary"] = f"{VERSIONS_DIRECTORY}/{digest}/glossary.json"
        write_file_atomically(
            definition.manifest_path,
            json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8") + b"\n",
        )
        return versioned_glossary


def preflight_baseline(definition: CourseDefinition) -> tuple[str, str]:
    """Record the ``(course_root, publication_digest)`` a publish re-validates."""
    return str(definition.root), publication_digest(definition)

