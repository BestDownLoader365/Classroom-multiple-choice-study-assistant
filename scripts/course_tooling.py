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

Candidate resolution
--------------------

Every content command works on a *working copy* next to the course manifest
(``courses/<course_id>/questions_candidate.json`` /
``glossary_candidate.json``).  When a command is given no explicit path it uses
that default, and falls back to the file the course currently publishes when the
working copy does not exist, so the same command both drives an edit and
re-validates a deployed course.  ``preferred_candidate`` is the single place that
precedence lives.
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
    validate_course_id,
)
from app.models.course import CourseIdError  # noqa: E402
from app.repositories import CourseLoader  # noqa: E402

#: The CLIs used to each implement their own ``shutil.copy2`` backup.  They now
#: share the application's verified, atomically published snapshot so an operator
#: gets the same artifact (and the same failure behaviour) from every command.
from app.repositories.database_backup import (  # noqa: E402
    BACKUP_MARKER,
    BackupError,
    backup_name,
    timestamped_backup,
)

VERSIONS_DIRECTORY = "versions"
LOCK_FILE_NAME = ".publish.lock"

#: Content file names that live inside a content-addressed ``versions/<sha256>/``
#: directory.  Retention counts each type separately, because one publish may
#: only touch one of them (a glossary publish must never evict the previous
#: question bank, and the other way round).
VERSIONED_CONTENT_FILES = ("questions.json", "glossary.json")

#: How many versions of one content type a publish keeps: the version the
#: manifest currently points at plus the one before it, so exactly one rollback
#: step stays possible without a history journal.  Override per command with
#: ``publish_course.py --keep-versions N``.
DEFAULT_KEPT_VERSIONS = 2

#: Working-copy file names the CLI falls back to when a command is not given an
#: explicit path.  A candidate belongs to exactly one course, so the default is
#: resolved inside that course's own directory (``courses/<course_id>/``) and
#: never against the shell's current directory.
QUESTIONS_CANDIDATE_NAME = "questions_candidate.json"
GLOSSARY_CANDIDATE_NAME = "glossary_candidate.json"


class ToolingError(RuntimeError):
    """Raised when a course operation cannot be performed safely."""


def build_loader(
    courses_dir: Path,
    question_file: Path,
    glossary_file: Path,
    *,
    legacy_course_id: str | None = None,
) -> CourseLoader:
    """Build the application's own course loader for the CLI.

    ``legacy_course_id`` is the namespace the root-file legacy adapter should
    claim.  ``None`` means "use the loader default"; a caller that knows the
    persisted value (``migrate_courses.py``) passes it so the adapter and the
    database agree.
    """
    return CourseLoader(
        courses_dir,
        legacy_directory=question_file.parent,
        legacy_questions_name=question_file.name,
        legacy_glossary_name=glossary_file.name,
        **({} if legacy_course_id is None else {"legacy_course_id": legacy_course_id}),
    )


def default_candidate_path(course_root: Path, name: str) -> Path:
    """Return the default working copy of one content type for a course.

    ``course_root`` is the course directory (``definition.root``), not the
    directory of the currently published file: after a publication the manifest
    points at ``versions/<sha256>/``, while the working copy stays next to the
    manifest where a maintainer edits it.
    """
    return course_root / name


def preferred_candidate(
    course_root: Path, name: str, published: Path | None
) -> tuple[Path | None, str]:
    """Return ``(path, source)`` for one content type of a resolved course.

    The default working copy (``questions_candidate.json`` /
    ``glossary_candidate.json``) wins when it exists, because that is the file a
    maintainer is about to publish; otherwise the currently published file is
    used, so the read-only gates still re-validate a deployed course.  Callers
    print ``source`` (``"candidate"`` or ``"published"``) instead of leaving the
    file that was read implicit.
    """
    candidate = default_candidate_path(course_root, name)
    if candidate.is_file():
        return candidate.resolve(), "candidate"
    return published, "published"


def new_course_hint(loader: CourseLoader, course_id: str) -> str:
    """Explain how to proceed when a directory exists but declares no course.

    ``courses/<course_id>/`` holding only working copies
    (``questions_candidate.json`` / ``glossary_candidate.json``) is *not* a
    declared course: the loader only knows courses with a ``course.json``
    manifest, so every ``--course`` lookup fails until ``publish_course.py
    --add`` has created it.  The requested id is validated as a slug before it
    is ever used in a path, so a mistyped or hostile value cannot escape
    ``courses/``.  Returns ``""`` when there is nothing to explain.
    """
    try:
        slug = validate_course_id(course_id)
    except CourseIdError:
        return ""
    course_root = loader.courses_root / slug
    if not course_root.is_dir():
        return ""
    if (course_root / "course.json").is_file():  # pragma: no cover - defensive
        return ""
    return (
        f"提示：{course_root} 里只有候选文件，还没有 course.json。新课程请先用 "
        "publish_course.py --course <course_id> --add ... 创建（它会校验候选并写入 "
        "manifest），之后 --course 才能解析这门课。"
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
            hint = new_course_hint(loader, course_id)
            raise ToolingError(
                f'Unknown course "{course_id}". Declared courses: '
                f"{declared or '(none)'}."
                + (f"\n{hint}" if hint else "")
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

    Every later step works on the returned payload, never on the path again:
    basic schema validation loads it from a private copy, ``publish_course.py``
    hands the same bytes to the matching gate through that script's in-memory
    entry point, and the archive writes them to ``versions/<sha256>/``.  A file
    that changes mid-command therefore cannot make the preflight and the archive
    disagree — both are the same frozen revision.
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

#: Directory inside ``courses/`` that holds course directories whose database
#: half has not been committed yet, or whose final removal failed.  It is
#: dot-prefixed so the loader's ``entry.name.startswith(".")`` rule ignores it,
#: and it lives inside the course root so a move into it is always a rename on
#: the same filesystem.
ISOLATED_DIRECTORY = ".trash"

#: Separator between the course id and the timestamp in ``.trash/<id>.<stamp>``.
ISOLATED_SEPARATOR = "."


class FilesystemTransactionError(ToolingError):
    """Raised when a filesystem step could not be applied or compensated.

    Callers distinguish two situations with it: the *main* step failed (nothing
    changed, or the change was fully undone) versus the *compensation* failed too,
    which needs a human.  The message always names the paths involved so an
    operator can act on it without re-reading the code.
    """


def contained_path(candidate: Path, root: Path) -> bool:
    """Return whether ``candidate`` is ``root`` itself or lives inside it.

    The one containment check every course CLI uses before it renames or deletes
    anything: a resolved path that escapes the course root is never touched.  It
    mirrors ``app.repositories.course_loader._is_contained`` so the CLI and the
    loader cannot disagree about what "inside the course directory" means.
    """
    try:
        candidate = Path(candidate).resolve()
        root = Path(root).resolve()
    except OSError:  # pragma: no cover - defensive for exotic paths
        return False
    try:
        return candidate == root or candidate.is_relative_to(root)
    except ValueError:  # pragma: no cover - different drives on Windows
        return False


def atomic_rename(source: Path, target: Path) -> None:
    """Rename ``source`` onto ``target`` in one filesystem operation.

    Both sides must be on the same filesystem, which the callers guarantee by
    keeping ``target`` inside the course root.  ``os.rename`` is atomic, so a
    reader either sees the old name or the new one — never a half-moved
    directory.  Every failure mode is translated into a
    :class:`FilesystemTransactionError` that says what to check, because a bare
    ``[Errno 18] Invalid cross-device link`` is not an actionable message.
    """
    import errno

    try:
        os.rename(source, target)
    except OSError as exc:
        hint = {
            errno.EXDEV: "源与目标不在同一个文件系统（不要跨挂载点移动）",
            errno.EACCES: "权限不足（检查目录属主与写权限）",
            errno.EPERM: "权限不足（检查目录属主与写权限）",
            errno.ENOTEMPTY: "目标目录非空（绝不覆盖已有目录）",
            errno.EEXIST: "目标已存在（绝不覆盖已有目录）",
            errno.ENOENT: "源或目标的父目录不存在",
        }.get(exc.errno, "见下面的系统错误")
        raise FilesystemTransactionError(
            f"无法重命名 {source} -> {target}：{hint}（{exc}）"
        ) from exc
    fsync_directory(Path(target).parent)



def isolated_directory(courses_dir: Path) -> Path:
    """Return (creating it if needed) the quarantine directory inside ``courses``."""
    directory = Path(courses_dir) / ISOLATED_DIRECTORY
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory


def unique_isolated_path(
    courses_dir: Path, course_id: str, *, stamp: str | None = None
) -> Path:
    """Return a quarantine path for ``course_id`` that does not exist yet.

    The name is ``<course_id>.<UTC stamp>`` with a ``-N`` ordinal on collision,
    which makes the entry self-describing (whose directory it is, when it was
    quarantined) **and** uniquely attributable back to one course.  An existing
    entry is never reused, so a crashed run's evidence cannot be overwritten.
    """
    from datetime import datetime, timezone

    directory = isolated_directory(courses_dir)
    stamp = stamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for ordinal in range(100):
        suffix = "" if ordinal == 0 else f"-{ordinal}"
        candidate = directory / f"{course_id}{ISOLATED_SEPARATOR}{stamp}{suffix}"
        if not candidate.exists():
            return candidate
    raise FilesystemTransactionError(  # pragma: no cover - 100 same-second runs
        f"隔离目录中同名条目过多，拒绝继续：{directory}（{course_id}{ISOLATED_SEPARATOR}{stamp}）"
    )


def isolated_entries(courses_dir: Path, course_id: str | None = None) -> list[Path]:
    """List quarantined course directories, optionally for one ``course_id``.

    Containment is re-checked on every entry, so nothing outside
    ``courses/<ISOLATED_DIRECTORY>`` can ever be reported or removed by a
    ``--purge``.
    """
    directory = Path(courses_dir) / ISOLATED_DIRECTORY
    if not directory.is_dir():
        return []
    prefix = None if course_id is None else f"{course_id}{ISOLATED_SEPARATOR}"
    entries: list[Path] = []
    for entry in sorted(directory.iterdir(), key=lambda item: item.name):
        if not entry.is_dir() or not contained_path(entry, directory):
            continue
        if prefix is not None and not entry.name.startswith(prefix):
            continue
        entries.append(entry)
    return entries


def adopt_or_report(courses_dir: Path, course_id: str) -> tuple[Path | None, str]:
    """Return ``(course_directory, note)``, adopting a crashed run's quarantine.

    A deletion interrupted between "renamed into ``.trash``" and "the database
    transaction committed" leaves the course with no directory at its documented
    path but a quarantine entry that *is* that directory.  Rather than asking an
    operator to interpret that, the next run adopts the single entry for this
    ``course_id`` and continues where the previous run stopped, which is what
    makes re-running the command idempotent.

    Ambiguity is refused instead of guessed: two entries (or an entry *and* a
    directory at the documented path) means something else happened, and the
    operator is told to inspect ``--purge`` output first.
    """
    documented = Path(courses_dir) / course_id
    entries = isolated_entries(courses_dir, course_id)
    if not entries:
        return (documented if documented.is_dir() else None), ""
    if len(entries) > 1:
        raise FilesystemTransactionError(
            f'课程 "{course_id}" 在隔离目录中有 {len(entries)} 个待处理条目，无法判断该接管哪一个：'
            + "".join(f"\n  - {entry}" for entry in entries)
            + "\n请先用 --purge --dry-run 查看并人工确认后再执行。"
        )
    entry = entries[0]
    if documented.is_dir():
        raise FilesystemTransactionError(
            f'课程 "{course_id}" 同时存在于 {documented} 与隔离目录 {entry}：'
            "这是协议不应产生的状态，请人工确认后再处理。"
        )
    return entry, (
        f"接管上次中断留下的隔离目录：{entry}\n"
        f"（课程目录已不在 {documented}；本次继续删除流程，失败时恢复回该路径）"
    )


def discard_isolated(entry: Path, courses_dir: Path) -> None:
    """Remove one quarantined directory, refusing to escape ``courses/.trash``."""
    import shutil

    directory = Path(courses_dir) / ISOLATED_DIRECTORY
    if not contained_path(entry, directory):
        raise FilesystemTransactionError(
            f"拒绝删除隔离目录之外的路径：{entry}（隔离目录为 {directory}）"
        )
    shutil.rmtree(entry)


# ----------------------------------------------------------------- rename state

#: One in-flight rename per source course is recorded in this file, so an
#: interrupted run can be finished (or undone) deterministically.  Dot-prefixed
#: for the same reason the quarantine directory is: the loader ignores it.
RENAME_STATE_PREFIX = ".rename-state-"

#: Staging directory for a rename in progress.  Dot-prefixed so the loader never
#: sees it as a course directory: at no point does a *half-renamed* course become
#: discoverable.
RENAME_STAGING_PREFIX = ".rename-staging-"


def rename_state_path(courses_dir: Path, source: str) -> Path:
    """Return the state file recording the in-flight rename of ``source``."""
    return Path(courses_dir) / f"{RENAME_STATE_PREFIX}{source}.json"


def _unique_directory(parent: Path, stem: str) -> Path:
    for ordinal in range(100):
        candidate = parent / (stem if ordinal == 0 else f"{stem}-{ordinal}")
        if not candidate.exists():
            return candidate
    raise FilesystemTransactionError(  # pragma: no cover - 100 same-second runs
        f"同名目录过多，拒绝继续：{parent}/{stem}"
    )


def staging_directory(
    courses_dir: Path, source: str, *, stamp: str | None = None
) -> Path:
    """Return a fresh staging directory name for renaming ``source``."""
    from datetime import datetime, timezone

    stamp = stamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return _unique_directory(
        Path(courses_dir), f"{RENAME_STAGING_PREFIX}{source}-{stamp}"
    )


def read_rename_state(path: Path) -> dict | None:
    """Return the recorded rename state, or ``None`` when unreadable/absent."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def write_rename_state(path: Path, payload: dict) -> None:
    """Record the current phase of a rename, atomically."""
    write_file_atomically(
        Path(path),
        json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8") + b"\n",
    )


def list_rename_states(courses_dir: Path) -> list[Path]:
    """Return every recorded in-flight rename, oldest name first."""
    root = Path(courses_dir)
    if not root.is_dir():
        return []
    return sorted(
        entry
        for entry in root.iterdir()
        if entry.is_file()
        and entry.name.startswith(RENAME_STATE_PREFIX)
        and entry.name.endswith(".json")
    )


def remove_rename_state(path: Path) -> None:
    """Forget one finished (or abandoned) rename."""
    Path(path).unlink(missing_ok=True)


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


# ---------------------------------------------------------------- retention


def manifest_version_directories(definition: CourseDefinition) -> set[str]:
    """Return the ``versions/<sha256>`` directory names the manifest points at.

    The manifest is read fresh: the paths cached on ``definition`` are the ones
    the *previous* worker loaded, so right after a switch-over they still name
    the superseded version.  A manifest that points outside ``versions/`` (the
    plain-file layout) protects nothing, because nothing in ``versions/`` is in
    use by that course.
    """
    if definition.manifest_path is None:
        return set()
    manifest = read_manifest(definition)
    versions_root = definition.root / VERSIONS_DIRECTORY
    referenced: set[str] = set()
    for key in ("questions", "glossary"):
        value = manifest.get(key)
        if not isinstance(value, str) or not value:
            continue
        try:
            relative = (definition.root / value).relative_to(versions_root)
        except ValueError:
            continue
        if len(relative.parts) > 1:
            referenced.add(relative.parts[0])
    return referenced


def prune_versions(
    definition: CourseDefinition, *, keep: int = DEFAULT_KEPT_VERSIONS
) -> tuple[list[Path], list[Path]]:
    """Delete superseded ``versions/<sha256>/`` copies and report what happened.

    Retention is per content type and deliberately conservative:

    * the digest directory the manifest points at is **never** deleted;
    * the ``keep - 1`` most recently written other versions of that type are
      kept, so one rollback step is always available (``keep`` defaults to
      ``DEFAULT_KEPT_VERSIONS``: current + previous);
    * only the two known content file names are touched, only directly inside
      ``versions/<sha256>/``; a directory that still holds anything else is left
      in place, and an empty one is removed;
    * recency is the content file's mtime, the only ordering signal the
      content-addressed layout has — a rollback re-points the manifest at an
      existing directory instead of rewriting it, which is why the manifest
      reference outranks mtime.

    Returns ``(kept, removed)`` paths.  Callers hold the course's publication
    lock, so a concurrent publisher cannot slip a new version in mid-prune.
    """
    if keep < 1:
        raise ToolingError(
            "版本保留数量必须是 >= 1 的整数（1 = 只保留当前版本，无法回退）。"
        )
    versions_root = definition.root / VERSIONS_DIRECTORY
    if definition.manifest_path is None or not versions_root.is_dir():
        # Legacy/plain-file layout has no content-addressed copies at all.
        return [], []
    referenced = manifest_version_directories(definition)
    kept: list[Path] = []
    removed: list[Path] = []
    for name in VERSIONED_CONTENT_FILES:
        entries: list[tuple[int, str]] = []
        for digest_directory in sorted(versions_root.iterdir()):
            if not digest_directory.is_dir():
                continue
            content = digest_directory / name
            if content.is_file():
                entries.append((content.stat().st_mtime_ns, digest_directory.name))
        if not entries:
            continue
        entries.sort(key=lambda item: (item[0], item[1]), reverse=True)
        selected = [digest for _, digest in entries if digest in referenced]
        for _, digest in entries:
            if len(selected) >= keep:
                break
            if digest not in selected:
                selected.append(digest)
        for _, digest in entries:
            content = versions_root / digest / name
            if digest in selected:
                kept.append(content)
                continue
            content.unlink()
            removed.append(content)
            try:
                content.parent.rmdir()
            except OSError:
                # Anything else in the directory (an unrelated file) means it is
                # not this command's to delete.
                continue
    if removed:
        fsync_directory(versions_root)
    return kept, removed


