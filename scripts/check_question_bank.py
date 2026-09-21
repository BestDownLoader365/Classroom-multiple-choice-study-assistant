"""Pre-deploy, per-course question-bank check: validate and diff against a DB.

Run this before publishing a new ``questions.json`` for one course to see exactly
what the startup reconciliation would do — and to catch retired-ID reuse *before*
it can take a course down::

    python scripts/check_question_bank.py --course physical_design
    python scripts/check_question_bank.py --course physical_design other_questions.json --db instance/mcq.db
    python scripts/check_question_bank.py --course physical_design --published
    python scripts/check_question_bank.py --course physical_design --strict
    python scripts/check_question_bank.py --course physical_design --simulate

The candidate is resolved in this order (``--help`` shows the same default):

1. an explicit positional path always wins;
2. ``--published`` re-validates the file the course currently serves;
3. otherwise the course's default working copy
   (``courses/<course_id>/questions_candidate.json``, or
   ``<course_root>/questions_candidate.json`` for the legacy root layout) is
   used when it exists;
4. otherwise the currently published file is checked.

Deleting the working copy therefore restores the old "re-check what is deployed"
behaviour, and ``--published`` makes that explicit while a candidate exists.

A brand-new course cannot be named by ``--course`` before it exists:
``courses/<course_id>/course.json`` is what declares a course, and
``publish_course.py --add`` is what writes it (``--add`` runs the same schema
validation on the frozen candidate bytes).  Until then ``--course <new_id>``
reports ``Unknown course`` and exits ``4``.  The positional-path form without
``--course`` is *not* a substitute for a course that is not declared yet: such a
candidate is read as the legacy deployment's root content location and diffed
against the persisted legacy namespace, so its report is about the wrong course.

Everything is scoped to one course: the course's own registry, its own bank
state row and its own generation.  A course A check never reads or reports
course B's history.

The database is opened **read-only** and never modified.  ``--simulate`` copies
it to a temporary file first and runs the real reconciliation there, so the
impact figures are produced by the production code path without touching live
learner data.

Exit codes:

``0``
    Nothing blocks the deploy.  Either the bank did not change in a
    data-affecting way, or the changes only add/move content while keeping
    every learner record.
``1``
    The candidate bank fails validation (bad JSON, bad schema, duplicate IDs).
``2``
    Blocking: a retired question ID is reused for a different question.  The
    course would be reported ``unavailable``; assign fresh IDs instead.
``3``
    ``--strict`` only: the update is allowed but would clear parts of learners'
    stored state (grading-changed or deleted questions).
``4``
    The request cannot be answered safely: unknown course, ambiguous course, or
    a database that has not been migrated and therefore cannot map a non-legacy
    candidate to the history it would be compared against.

Clearing learner state is *not* a deployment blocker on its own, so the default
run still exits ``0`` for it — but it always reports exactly which questions
lose their attempts/wrong-question/SRS state and never claims the deploy is
data-loss-free.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.models import (  # noqa: E402
    LEGACY_COURSE_ID,
    CourseDefinition,
    CourseDefinitionError,
    CourseLoadError,
    QuestionRegistryEntry,
    QuestionRegistryStatus,
)
from app.repositories import (  # noqa: E402
    CourseLoader,
    Database,
    QuestionBankError,
    QuestionLoader,
    ensure_schema,
    read_meta,
)
from app.repositories.schema_migrations import (  # noqa: E402
    LEGACY_COURSE_KEY,
)
from app.services import catalogue_fingerprint, diff_questions  # noqa: E402
from scripts.course_tooling import (  # noqa: E402
    QUESTIONS_CANDIDATE_NAME,
    new_course_hint,
    preferred_candidate,
)

MIGRATION_TABLE_SUFFIX = "__course_migration"

#: Human labels for where the checked file came from.
CANDIDATE_SOURCES = {
    "explicit": "显式指定",
    "candidate": "默认候选",
    "published": "当前已发布",
}

#: The exit-code contract of this gate.  ``publish_course.py`` forwards whatever
#: this script returns, so the numbers are part of a shared interface: never
#: renumber them, and define them here so no caller has to hard-code a literal.
#: ``EXIT_BLOCKING`` is also the code for a usage-level refusal (argparse's own
#: usage errors and the ``--published`` + explicit path conflict), which is why
#: the constant is named for the outcome rather than for one cause.
EXIT_OK = 0
EXIT_INVALID = 1
EXIT_BLOCKING = 2
EXIT_STRICT = 3
EXIT_UNRESOLVABLE = 4


class ScriptError(RuntimeError):
    """Raised when the request cannot be answered safely (exit code 4)."""


# --------------------------------------------------------------- database reads


def _open_read_only(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _has_table(connection: sqlite3.Connection, name: str) -> bool:
    return (
        connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone()
        is not None
    )


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in connection.execute(f'PRAGMA table_info("{table}")')}


def _require_course_mapping(
    connection: sqlite3.Connection, course_id: str, database_path: Path
) -> None:
    """Refuse to diff a non-legacy course against an unmigrated database.

    A pre-multi-course database has exactly one namespace and no ``course_id``
    column at all.  Comparing any other course's candidate against "the whole
    database's history" would silently report that course's questions as new
    while hiding the real owner of those rows, so the check refuses instead.
    """
    columns = _columns(connection, "question_registry")
    if "course_id" in columns:
        return
    legacy = read_meta(connection, LEGACY_COURSE_KEY) or LEGACY_COURSE_ID
    if course_id != legacy:
        raise ScriptError(
            f"{database_path} has not been migrated to the multi-course schema "
            f'(question_registry has no "course_id"), so it can only be mapped to '
            f"the legacy course (currently {legacy!r}). Run "
            "scripts/migrate_courses.py first, then check each course."
        )


def _load_registry(
    database_path: Path, course_id: str
) -> dict[str, QuestionRegistryEntry] | None:
    """Read one course's registry, or ``None`` when the table has no rows.

    Pre-multi-course databases have no ``course_id`` column; their rows are read
    as the legacy namespace, and :func:`_require_course_mapping` has already
    refused any other course.
    """
    if not database_path.is_file():
        return None
    connection = _open_read_only(database_path)
    try:
        if not _has_table(connection, "question_registry"):
            return None
        _require_course_mapping(connection, course_id, database_path)
        columns = _columns(connection, "question_registry")
        if "course_id" in columns:
            rows = connection.execute(
                "SELECT * FROM question_registry WHERE course_id = ?",
                (course_id,),
            ).fetchall()
        else:
            rows = connection.execute("SELECT * FROM question_registry").fetchall()
    finally:
        connection.close()
    if not rows:
        return None
    return {
        row["question_id"]: QuestionRegistryEntry(
            question_id=row["question_id"],
            status=QuestionRegistryStatus(row["status"]),
            question_type=row["question_type"],
            option_ids=tuple(json.loads(row["option_ids"])),
            correct_answers=tuple(json.loads(row["correct_answers"])),
            content_fingerprint=row["content_fingerprint"],
            first_seen_at=row["first_seen_at"],
            last_seen_at=row["last_seen_at"],
            retired_at=row["retired_at"],
            placement_fingerprint=_placement_of(row),
        )
        for row in rows
    }


def _placement_of(row: sqlite3.Row) -> str:
    """Read the optional placement column of a possibly old registry row."""
    if "placement_fingerprint" not in row.keys():
        return ""
    return row["placement_fingerprint"] or ""


def _load_state(
    database_path: Path, course_id: str
) -> tuple[int, str, str | None] | None:
    """Read this course's ``(generation, bank_version, catalogue_fingerprint)``.

    ``None`` means the course has no recorded bank state at all, which is
    distinct from a recorded generation of ``0``.
    """
    if not database_path.is_file():
        return None
    connection = _open_read_only(database_path)
    try:
        if not _has_table(connection, "question_bank_state"):
            return None
        columns = _columns(connection, "question_bank_state")
        if "course_id" in columns:
            row = connection.execute(
                "SELECT * FROM question_bank_state WHERE course_id = ?", (course_id,)
            ).fetchone()
        elif course_id == (read_meta(connection, LEGACY_COURSE_KEY) or LEGACY_COURSE_ID):
            row = connection.execute(
                "SELECT * FROM question_bank_state LIMIT 1"
            ).fetchone()
        else:
            return None
    finally:
        connection.close()
    if row is None:
        return None
    catalogue = (
        (row["catalogue_fingerprint"] or "")
        if "catalogue_fingerprint" in row.keys()
        else ""
    )
    return int(row["generation"]), row["bank_version"], catalogue or None



def _impact(
    database_path: Path, course_id: str, unusable_ids: tuple[str, ...]
) -> dict[str, int]:
    """Count the learner state a destructive diff would clear in this course.

    The intent is a report, never a mutation: the database stays read-only.
    """
    impact = {
        "attempts": 0,
        "wrong_questions": 0,
        "weak_points": 0,
        "progress_rows": 0,
        "progress_touched": 0,
        "in_progress_exams": 0,
    }
    if not database_path.is_file() or not unusable_ids:
        return impact
    placeholders = ", ".join("?" for _ in unusable_ids)
    markers = [f'%"{question_id}"%' for question_id in unusable_ids]
    weak_clause = " OR ".join("verified_question_ids LIKE ?" for _ in markers)
    progress_clause = " OR ".join("state LIKE ?" for _ in markers)
    connection = _open_read_only(database_path)
    try:
        for key, table in (
            ("attempts", "attempts"),
            ("wrong_questions", "wrong_questions"),
        ):
            if not _has_table(connection, table):
                continue
            impact[key] = int(
                connection.execute(
                    f"SELECT COUNT(*) AS total FROM {table} "
                    f"WHERE course_id = ? AND question_id IN ({placeholders})",
                    (course_id, *unusable_ids),
                ).fetchone()["total"]
            )
        if _has_table(connection, "weak_knowledge_points"):
            impact["weak_points"] = int(
                connection.execute(
                    "SELECT COUNT(*) AS total FROM weak_knowledge_points "
                    f"WHERE course_id = ? AND ({weak_clause})",
                    (course_id, *markers),
                ).fetchone()["total"]
            )
        if _has_table(connection, "quiz_progress"):
            impact["progress_rows"] = int(
                connection.execute(
                    "SELECT COUNT(*) AS total FROM quiz_progress WHERE course_id = ?",
                    (course_id,),
                ).fetchone()["total"]
            )
            impact["progress_touched"] = int(
                connection.execute(
                    "SELECT COUNT(*) AS total FROM quiz_progress "
                    f"WHERE course_id = ? AND ({progress_clause})",
                    (course_id, *markers),
                ).fetchone()["total"]
            )
        if _has_table(connection, "exam_sessions") and _has_table(
            connection, "exam_questions"
        ):
            impact["in_progress_exams"] = int(
                connection.execute(
                    "SELECT COUNT(DISTINCT s.id) AS total FROM exam_sessions s "
                    "JOIN exam_questions q ON q.exam_id = s.id "
                    "WHERE s.course_id = ? AND s.status = 'in_progress' "
                    f"AND q.question_id IN ({placeholders})",
                    (course_id, *unusable_ids),
                ).fetchone()["total"]
            )
    finally:
        connection.close()
    return impact


def _legacy_tombstones(
    registry: dict[str, QuestionRegistryEntry],
) -> tuple[str, ...]:
    """Retired IDs without a recorded grading identity.

    They come from pre-registry databases (the ID only ever appeared in learner
    history) and are adopted instead of rejected if they reappear, which means a
    *different* question could inherit their old history.  The check lists them
    so maintainers can decide to rename the content instead.
    """
    return tuple(
        sorted(
            entry.question_id
            for entry in registry.values()
            if entry.status is QuestionRegistryStatus.RETIRED
            and not entry.option_ids
        )
    )


def _report(title: str, ids: tuple[str, ...]) -> None:
    print(f"{title}: {len(ids)}")
    for question_id in ids:
        print(f"    - {question_id}")


def _flag(title: str, value: bool) -> None:
    """Report one bank-level (not per-question) change classification."""
    print(f"{title}: {'yes' if value else 'no'}")


def _warning(title: str, ids: tuple[str, ...], detail: str) -> None:
    """Print one actionable warning block on stderr."""
    print(f"\n⚠ {title}: {len(ids)}", file=sys.stderr)
    for identifier in ids:
        print(f"    - {identifier}", file=sys.stderr)
    print(f"  {detail}", file=sys.stderr)



# --------------------------------------------------------------- course resolve


def _synthetic_legacy_definition(
    args: argparse.Namespace, candidate: Path
) -> CourseDefinition:
    """Describe a candidate that lives outside every declared course.

    ``scripts/check_question_bank.py candidate.json`` is the documented
    single-course flow: the candidate's directory *is* the deployment's root
    content location, and the namespace to diff against is the **persisted**
    ``legacy_course_id`` (so a deployment whose legacy namespace was renamed
    still compares against its own history).
    """
    from app.models import LEGACY_COURSE_ID, Course

    root = candidate.parent
    glossary_candidate = root / args.glossary_file.name
    legacy_course_id = LEGACY_COURSE_ID
    if args.db.is_file():
        try:
            connection = _open_read_only(args.db)
            try:
                if _has_table(connection, "schema_meta"):
                    legacy_course_id = (
                        read_meta(connection, LEGACY_COURSE_KEY) or LEGACY_COURSE_ID
                    )
            finally:
                connection.close()
        except sqlite3.Error:  # pragma: no cover - diagnostics only
            legacy_course_id = LEGACY_COURSE_ID
    return CourseDefinition(
        course=Course(
            course_id=legacy_course_id,
            title="Candidate question bank",
            enabled=True,
            order=-1_000_000,
        ),
        root=root,
        questions_path=candidate,
        glossary_path=(
            glossary_candidate.resolve() if glossary_candidate.is_file() else None
        ),
        manifest_path=None,
        layout="legacy",
    )


def resolve_definition(args: argparse.Namespace) -> CourseDefinition:
    """Resolve the single course definition this check applies to."""
    loader = CourseLoader(
        args.courses_dir,
        legacy_directory=args.question_file.parent,
        legacy_questions_name=args.question_file.name,
        legacy_glossary_name=args.glossary_file.name,
    )
    if args.course:
        definition = loader.find_definition(args.course)
        if definition is None:
            known = [item.course_id for item in loader.discover_definitions()]
            hint = new_course_hint(loader, args.course)
            raise ScriptError(
                f'Unknown course "{args.course}". Declared courses: '
                f"{', '.join(known) if known else '(none)'}."
                + (f"\n{hint}" if hint else "")
            )
        return definition

    definitions = loader.enabled_definitions()
    candidate = Path(args.candidate).resolve() if args.candidate else None
    if candidate is not None:
        # A candidate inside (or next to) a declared course belongs to it.  The
        # parent comparison also covers a working copy that sits next to the
        # manifest while the published file already lives in versions/<digest>/.
        for definition in definitions:
            if definition.questions_path == candidate:
                return definition
        for definition in definitions:
            if candidate.parent in {
                definition.root,
                definition.questions_path.parent,
            }:
                return definition
        # Otherwise the candidate defines the deployment's root content location.
        return _synthetic_legacy_definition(args, candidate)
    if len(definitions) == 1:
        return definitions[0]
    if not definitions:
        raise ScriptError(
            "No enabled course is declared. Add courses/<course_id>/course.json "
            "or place a root questions.json, or pass --course."
        )
    raise ScriptError(
        "Several courses are declared, so the target is ambiguous. Pass "
        "--course <course_id>."
    )


def candidate_file_for(
    args: argparse.Namespace, definition: CourseDefinition
) -> tuple[Path, str]:
    """Return ``(candidate_file, source)`` for the course being checked.

    Precedence: an explicit positional path, ``--published``, the course's
    default working copy (``questions_candidate.json``), and finally the file the
    course currently publishes.  A bare ``--course <course_id>`` therefore checks
    the file a maintainer is editing while it exists, and the deployed file
    otherwise.
    """
    if args.candidate is not None:
        return Path(args.candidate).resolve(), "explicit"
    if args.published:
        return definition.questions_path, "published"
    candidate, source = preferred_candidate(
        definition.root, QUESTIONS_CANDIDATE_NAME, definition.questions_path
    )
    return candidate, source


def simulate(database_path: Path, candidate_file: Path, definition: CourseDefinition) -> int:
    """Run the real reconciliation against a throwaway copy of the database.

    Returns the generation the real startup path would end up at.  The live
    database is never written: only the copy changes, and it is deleted
    afterwards.
    """
    from datetime import timezone

    from app.repositories import UserRepository
    from app.services.course_service import (
        assemble_course_services,
        synchronize_course,
    )

    with tempfile.TemporaryDirectory() as directory:
        copy = Path(directory) / "simulated.db"
        shutil.copy2(database_path, copy)
        ensure_schema(copy)
        database = Database(copy)
        # Point the definition at the candidate file, exactly as a real
        # publication would.
        simulated_definition = CourseDefinition(
            course=definition.course,
            root=definition.root,
            questions_path=candidate_file,
            glossary_path=definition.glossary_path,
            manifest_path=definition.manifest_path,
            layout=definition.layout,
        )
        loader = CourseLoader(
            candidate_file.parent,
            legacy_directory=None,
            legacy_questions_name=candidate_file.name,
            legacy_glossary_name=candidate_file.name,
        )
        bundle = loader.load_bundle(simulated_definition)
        services = assemble_course_services(
            bundle,
            database=database,
            knowledge_verification_target=2,
            display_timezone=timezone.utc,
            user_repository=UserRepository(database),
        )
        reconciled = synchronize_course(
            services,
            publication_identity=lambda: loader.publication_identity(
                simulated_definition
            ),
        )
        return reconciled.generation




# ------------------------------------------------------------------------ main


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog=(
            "exit codes: 0 可部署（含允许的清理行为，会打印警告）；"
            "1 题库校验失败；2 非法复用退役 ID（阻止启动）；"
            "3 --strict 下检测到学习状态清理；4 课程/数据库无法安全比对。"
        ),
    )
    parser.add_argument(
        "candidate",
        nargs="?",
        default=None,
        help=(
            "candidate questions.json to validate; omit to check the course's "
            f"default working copy ({QUESTIONS_CANDIDATE_NAME}) when it exists, "
            "else its currently published file"
        ),
    )
    parser.add_argument(
        "--published",
        action="store_true",
        help=(
            f"ignore any {QUESTIONS_CANDIDATE_NAME} working copy and re-check the "
            "file the course currently publishes"
        ),
    )
    parser.add_argument(
        "--course",
        default=None,
        help="course_id to check (required when several courses are declared)",
    )
    parser.add_argument(
        "--courses-dir",
        default=PROJECT_ROOT / "courses",
        type=Path,
        help="directory holding one sub-directory per course (default: ./courses)",
    )
    parser.add_argument(
        "--question-file",
        default=PROJECT_ROOT / "questions.json",
        type=Path,
        help="root questions.json used for the legacy course adapter",
    )
    parser.add_argument(
        "--glossary-file",
        default=PROJECT_ROOT / "glossary.json",
        type=Path,
        help="root glossary.json used for the legacy course adapter",
    )
    parser.add_argument(
        "--db",
        default=PROJECT_ROOT / "instance" / "mcq.db",
        type=Path,
        help="SQLite database to diff against (opened read-only)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "exit 3 instead of 0 when the update would clear learner state "
            "(grading-changed or deleted questions)"
        ),
    )
    parser.add_argument(
        "--simulate",
        action="store_true",
        help=(
            "copy the database to a temporary file and run the real "
            "reconciliation there, reporting the resulting generation"
        ),
    )
    return parser



def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.published and args.candidate is not None:
        print(
            "--published 与显式 candidate 路径不能同时使用：请二选一。",
            file=sys.stderr,
        )
        return EXIT_BLOCKING

    try:
        definition = resolve_definition(args)
    except (ScriptError, CourseDefinitionError) as exc:
        print(f"无法解析课程：\n{exc}", file=sys.stderr)
        return EXIT_UNRESOLVABLE
    course_id = definition.course_id
    candidate, source = candidate_file_for(args, definition)
    print(f"课程 (course_id): {course_id} [{definition.layout}]")
    print(f"题库文件 ({CANDIDATE_SOURCES[source]}): {candidate}")

    loader = QuestionLoader(candidate)
    try:
        questions = loader.load()
    except QuestionBankError as exc:
        print(f"题库校验失败：\n{exc}", file=sys.stderr)
        return EXIT_INVALID
    print(f"题库校验通过：{len(questions)} 道题。")

    try:
        registry = _load_registry(args.db, course_id)
        stored_state = _load_state(args.db, course_id)
    except ScriptError as exc:
        print(f"无法比对：\n{exc}", file=sys.stderr)
        return EXIT_UNRESOLVABLE

    generation = stored_state[0] if stored_state else 0
    print(f"当前 generation：{generation}")
    if args.simulate and args.db.is_file():
        try:
            simulated_generation = simulate(args.db, candidate, definition)
        except (CourseLoadError, ScriptError) as exc:
            print(f"模拟失败：\n{exc}", file=sys.stderr)
            return EXIT_UNRESOLVABLE
        print(f"模拟后的 generation：{simulated_generation}")
        print(
            "本次发布会推进 generation："
            f"{'yes' if simulated_generation != generation else 'no'}"
        )

    if not registry:
        print(
            "该课程在数据库中没有 question_registry 记录：首次启动将建立基线，"
            "不会清理任何学习数据。"
        )
        print("\n可以安全部署。")
        return EXIT_OK

    diff = diff_questions(questions, registry)
    stored_catalogue = stored_state[2] if stored_state else None
    candidate_catalogue = catalogue_fingerprint(loader.sources, loader.chapters)
    # A database without a recorded catalogue shape predates catalogue tracking:
    # the sync adopts the candidate as the baseline, so no change is reported.
    catalogue_changed = (
        stored_catalogue is not None and stored_catalogue != candidate_catalogue
    )
    file_changed = (
        stored_state is not None
        and loader.source_fingerprint is not None
        and loader.source_fingerprint != stored_state[1]
    )
    presentation_only = file_changed and not diff.has_changes and not catalogue_changed
    would_bump = bool(diff.structural or catalogue_changed)
    _flag("本次发布将推进该课程 generation (generation would bump)", would_bump)

    _report("新增题目（不清理数据）(new)", diff.new_ids)
    _report("内容修改（历史保留）(content-only)", diff.content_changed_ids)
    _report(
        "章节/来源调整（历史保留，但会推进 generation）(placement-changed)",
        diff.placement_changed_ids,
    )
    _report("判题规则变化（仅清理该题历史）(grading-changed)", diff.grading_changed_ids)
    _report("删除题目（attempts 保留，错题/SRS 静默清除）(deleted)", diff.deleted_ids)
    _report("恢复原题 (resurrected)", diff.resurrected_ids)
    _flag(
        "目录结构变化（课件/章节增删、顺序或归属，会推进 generation）"
        "(catalogue-changed)",
        catalogue_changed,
    )
    _flag("仅文案/格式差异（不影响 generation）(presentation-only)", presentation_only)

    print(
        "\nattempts 处理策略："
        f"判题规则变化删除该题 attempts ({len(diff.grading_changed_ids)} 题)；"
        f"删除题目保留全部 attempts ({len(diff.deleted_ids)} 题)。"
    )
    unusable = tuple(sorted(diff.unusable_ids))
    impact = _impact(args.db, course_id, unusable)
    print(
        "本次发布会清理的本课程学习状态："
        f"attempts {impact['attempts']} 条、错题/SRS {impact['wrong_questions']} 条、"
        f"薄弱知识点 {impact['weak_points']} 条、"
        f"未完成练习 {impact['progress_touched']}/{impact['progress_rows']} 轮、"
        f"进行中的考试 {impact['in_progress_exams']} 场。"
    )

    if diff.violations:
        print(
            "\n错误：以下已退役的 question ID 被复用于判题规则不同的题目：\n    - "
            + "\n    - ".join(diff.violations)
            + "\n退役 ID 永久保留，请为新题分配新 ID。该课程会被标记为不可用。",
            file=sys.stderr,
        )
        return EXIT_BLOCKING

    tombstones = _legacy_tombstones(registry)
    if tombstones:
        _warning(
            "没有判题身份的 legacy 退役记录 "
            "(legacy tombstones without grading identity)",
            tombstones,
            "这些 ID 来自 pre-registry 迁移或仅存在于历史数据中，系统无法判断它们的判题身份；"
            "\n  一旦重新出现在题库中，可能直接继承旧的历史作答。请优先为这些内容使用新的 ID。",
        )

    destructive = tuple(sorted(set(diff.grading_changed_ids) | set(diff.deleted_ids)))
    if diff.clears_learner_state:
        _warning(
            "本次更新将清理作答/错题相关学习状态 (learner state will be cleared)",
            destructive,
            "受影响的账号会丢失这些题的答题历史、错题/SRS 状态、薄弱知识点引用和未完成的练习/考试槽位；"
            "\n  基于 live 题目的统计（累计答题数、正确率等）也会随之变化。",
        )
        if args.strict:
            print(
                "\n--strict：检测到学习状态清理，返回非 0（exit 3）。",
                file=sys.stderr,
            )
            return EXIT_STRICT
        print(
            "\n注意：本次更新会清理上述学习状态；确认可以接受后再部署，"
            "并统一重启全部工作进程。"
        )
        return EXIT_OK

    if would_bump:
        print(
            f"\n本次发布会推进课程 {course_id} 的 generation：部署后请统一重启全部"
            "工作进程；未更新的进程只会围栏该课程的学习页面（其他课程不受影响）。"
        )
    if presentation_only:
        print(
            "\n仅文案/格式差异（题库标题、课件/章节标题、lecture、filename 或 JSON 格式）："
            "学习数据与 worker 围栏都不受影响；这些展示文案在全部 worker 重启前可能短暂不同，"
            "不需要为此统一重启。"
        )
    print("\n可以安全部署。")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())

