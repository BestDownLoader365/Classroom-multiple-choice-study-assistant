"""Delete one course: its directory, its database namespace and every reference.

A course is not a directory.  ``rm -rf courses/<course_id>`` leaves live state
behind that the application still reads and serves::

    # 1) see exactly what would be removed, write nothing
    python scripts/delete_course.py --course physical_design --dry-run

    # 2) delete (the database is backed up first)
    python scripts/delete_course.py --course physical_design

    # a course that still owns learner data needs an explicit --force
    python scripts/delete_course.py --course physical_design --force

What a manual ``rm`` misses, and this script removes, is:

* the content directory itself (``course.json``, the published content, the
  immutable ``versions/<sha256>/`` copies and the publication lock).  The
  default working copies (``questions_candidate.json`` /
  ``glossary_candidate.json``) live there too and go with it;
* the permanent ``courses`` row (identity plus the accepted metadata);
* every course-scoped learner row — ``quiz_progress``, ``attempts``,
  ``wrong_questions``, ``weak_knowledge_points``, ``exam_sessions`` and the
  ``exam_questions`` slots those sessions own;
* the course's ``question_bank_state`` row (its generation) and every
  ``question_registry`` row, i.e. the permanent per-course question-ID
  retirement tombstones;
* the navigation preference ``schema_meta.default_course_id`` when it points at
  the deleted course.

Without that cleanup the database keeps claiming the course exists, keeps
owning its learner history, and a later course that reuses the ``course_id``
would silently inherit the old retired-ID tombstones — while git no longer
carries the content, so ``check_courses.py`` reports it ``undeployed`` forever.

Guarantees:

* every statement is scoped to one ``course_id``; before committing, each
  table's total row count is re-read and must have dropped by exactly the
  deleted count, so another course can never lose a row to this command;
* the directory is removed only when it is a strict child of ``--courses-dir``
  and its manifest declares exactly this ``course_id``;
* the persisted ``legacy_course_id`` namespace is refused, because
  ``Database.initialize`` recreates that row on every startup (use
  ``rename_course.py`` to move a namespace instead);
* the legacy root-file layout is refused: there is no course directory to
  delete, so migrate the layout first;
* a course that still owns learner rows is refused without ``--force``;
* ``--dry-run`` writes nothing at all.

A course that exists only in the database (``undeployed`` identity) is handled
too: it has no directory, so only its rows are removed.  Exit codes: ``0``
deleted (or dry run), ``1`` refused (nothing written), ``2`` usage/IO problem.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.models import (  # noqa: E402
    CourseDefinition,
    CourseDefinitionError,
    CourseIdError,
    validate_course_id,
)
from app.repositories import read_meta  # noqa: E402
from app.repositories.schema_migrations import (  # noqa: E402
    COURSE_SCOPED_TABLES,
    DEFAULT_COURSE_KEY,
    LEGACY_COURSE_KEY,
    SCHEMA_META_TABLE,
)
from scripts.course_tooling import build_loader  # noqa: E402

#: Course-scoped tables that hold per-course bookkeeping instead of learner
#: history: the generation row and the permanent ID-retirement tombstones.
BOOKKEEPING_TABLES: tuple[str, ...] = ("question_bank_state", "question_registry")

#: Course-scoped tables holding learner history (everything else that is scoped).
LEARNER_TABLES: tuple[str, ...] = tuple(
    name for name in COURSE_SCOPED_TABLES if name not in BOOKKEEPING_TABLES
)


class DeleteError(RuntimeError):
    """Raised when the deletion cannot be performed safely."""


class Refused(DeleteError):
    """Raised when a precondition refuses the deletion (nothing was written)."""


@dataclass(frozen=True)
class DatabasePlan:
    """What one course owns in one database, computed read-only."""

    course_id: str
    path: Path
    exists: bool = False
    migrated: bool = False
    course_row: bool = False
    learner_rows: tuple[tuple[str, int], ...] = ()
    bookkeeping_rows: tuple[tuple[str, int], ...] = ()
    default_course_id: str | None = None
    legacy_course_id: str | None = None

    @property
    def learner_total(self) -> int:
        return sum(count for _name, count in self.learner_rows)

    @property
    def bookkeeping_total(self) -> int:
        return sum(count for _name, count in self.bookkeeping_rows)

    @property
    def clears_default_course(self) -> bool:
        return self.default_course_id == self.course_id

    @property
    def owns_rows(self) -> bool:
        return bool(self.course_row or self.learner_total or self.bookkeeping_total)

    @property
    def has_namespace(self) -> bool:
        """Whether this database can physically hold the course's rows."""
        return bool(self.exists and self.migrated)


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        row["name"] for row in connection.execute(f'PRAGMA table_info("{table}")')
    }


def _scoped_tables(connection: sqlite3.Connection) -> tuple[str, ...]:
    """Return the course-scoped tables this database actually has."""
    tables = _table_names(connection)
    return tuple(
        name
        for name in COURSE_SCOPED_TABLES
        if name in tables and "course_id" in _columns(connection, name)
    )


def _count(connection: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    return int(connection.execute(sql, params).fetchone()["total"])


def _course_rows(connection: sqlite3.Connection, table: str, course_id: str) -> int:
    return _count(
        connection,
        f'SELECT COUNT(*) AS total FROM "{table}" WHERE course_id = ?',
        (course_id,),
    )


def _total_rows(connection: sqlite3.Connection, table: str) -> int:
    return _count(connection, f'SELECT COUNT(*) AS total FROM "{table}"')


def _exam_slot_count(connection: sqlite3.Connection, course_id: str) -> int:
    """Count the exam slots owned by one course's sessions.

    ``exam_questions`` carries no ``course_id`` of its own, so the count goes
    through the parent sessions.
    """
    return _count(
        connection,
        "SELECT COUNT(*) AS total FROM exam_questions q WHERE q.exam_id IN "
        "(SELECT id FROM exam_sessions WHERE course_id = ?)",
        (course_id,),
    )


def inspect_database(database_path: Path, course_id: str) -> DatabasePlan:
    """Read one course's database footprint without writing anything."""
    if not database_path.is_file():
        return DatabasePlan(course_id=course_id, path=database_path)
    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        tables = _table_names(connection)
        scoped = _scoped_tables(connection)
        learner: tuple[tuple[str, int], ...] = tuple(
            (name, _course_rows(connection, name, course_id))
            for name in LEARNER_TABLES
            if name in scoped
        )
        if "exam_questions" in tables and "exam_sessions" in scoped:
            learner += (("exam_questions", _exam_slot_count(connection, course_id)),)
        bookkeeping: tuple[tuple[str, int], ...] = tuple(
            (name, _course_rows(connection, name, course_id))
            for name in BOOKKEEPING_TABLES
            if name in scoped
        )
        course_row = "courses" in tables and bool(
            _count(
                connection,
                "SELECT COUNT(*) AS total FROM courses WHERE course_id = ?",
                (course_id,),
            )
        )
        has_meta = SCHEMA_META_TABLE in tables
        default_course_id = (
            read_meta(connection, DEFAULT_COURSE_KEY) if has_meta else None
        )
        legacy_course_id = read_meta(connection, LEGACY_COURSE_KEY) if has_meta else None
        # A database without the namespace columns predates the multi-course
        # refactor: it owns no namespaced row, so there is nothing to delete.
        migrated = "courses" in tables and len(scoped) == len(COURSE_SCOPED_TABLES)
    finally:
        connection.close()
    return DatabasePlan(
        course_id=course_id,
        path=database_path,
        exists=True,
        migrated=migrated,
        course_row=course_row,
        learner_rows=learner,
        bookkeeping_rows=bookkeeping,
        default_course_id=default_course_id,
        legacy_course_id=legacy_course_id,
    )


def delete_namespace(database_path: Path, course_id: str) -> dict[str, int]:
    """Delete one course's rows in a single transaction; return deleted counts.

    ``exam_questions`` carries no ``course_id``, so its rows are removed through
    their parent sessions first (the foreign key is ``ON DELETE RESTRICT``).
    Afterwards every touched table's *total* row count must have dropped by
    exactly the deleted count and the namespace must be empty; otherwise the
    transaction is rolled back and nothing is written.
    """
    connection = sqlite3.connect(database_path, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        tables = _table_names(connection)
        scoped = _scoped_tables(connection)
        verified = list(scoped)
        if "exam_questions" in tables:
            verified.append("exam_questions")
        verified.append("courses")
        before = {name: _total_rows(connection, name) for name in verified}

        connection.execute("BEGIN IMMEDIATE")
        try:
            deleted: dict[str, int] = {}
            if "exam_questions" in tables and "exam_sessions" in scoped:
                cursor = connection.execute(
                    "DELETE FROM exam_questions WHERE exam_id IN "
                    "(SELECT id FROM exam_sessions WHERE course_id = ?)",
                    (course_id,),
                )
                deleted["exam_questions"] = int(cursor.rowcount or 0)
            for name in scoped:
                cursor = connection.execute(
                    f'DELETE FROM "{name}" WHERE course_id = ?', (course_id,)
                )
                deleted[name] = int(cursor.rowcount or 0)
            cursor = connection.execute(
                "DELETE FROM courses WHERE course_id = ?", (course_id,)
            )
            deleted["courses"] = int(cursor.rowcount or 0)
            if read_meta(connection, DEFAULT_COURSE_KEY) == course_id:
                connection.execute(
                    "DELETE FROM schema_meta WHERE key = ?", (DEFAULT_COURSE_KEY,)
                )
                deleted[DEFAULT_COURSE_KEY] = 1

            residue = {
                name: _course_rows(connection, name, course_id) for name in scoped
            }
            if any(residue.values()):
                raise DeleteError(
                    "删除后仍有该课程的残留记录："
                    + ", ".join(f"{name}={count}" for name, count in residue.items() if count)
                )
            for name in verified:
                expected = before[name] - deleted.get(name, 0)
                actual = _total_rows(connection, name)
                if actual != expected:
                    raise DeleteError(
                        f'"{name}" 的总行数变化与预期不符（预期 {expected}，实际 {actual}）：'
                        "拒绝提交，避免误删其他课程的数据。"
                    )
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
    finally:
        connection.close()
    return deleted

    return _count(
        connection,
        "SELECT COUNT(*) AS total FROM exam_questions q WHERE q.exam_id IN "
        "(SELECT id FROM exam_sessions WHERE course_id = ?)",
        (course_id,),
    )



def remove_course_directory(definition: CourseDefinition, courses_dir: Path) -> Path:
    """Delete one manifest course's directory after proving it is safe to delete."""
    manifest_path = definition.manifest_path
    if manifest_path is None:
        raise Refused(
            "该课程由根目录 questions.json/glossary.json 的 legacy adapter 提供，"
            "没有课程目录可删除。"
        )
    root = definition.root.resolve()
    base = courses_dir.resolve()
    if root == base or not root.is_relative_to(base):
        raise Refused(f"课程目录不在 --courses-dir 内，拒绝删除：{root}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DeleteError(f"无法读取 manifest {manifest_path}：{exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("course_id") != definition.course_id:
        raise Refused(f"{manifest_path} 声明的 course_id 与解析结果不一致，拒绝删除目录。")
    if not root.is_dir():
        raise DeleteError(f"课程目录不存在：{root}")
    shutil.rmtree(root)
    return root


def _report(
    course_id: str,
    definition: CourseDefinition | None,
    directory: Path | None,
    plan: DatabasePlan,
) -> None:
    """Print exactly what the command is about to remove."""
    print(f"课程 (course_id): {course_id}")
    print(f"声明布局 (layout): {definition.layout if definition else '(未声明)'}")
    print(f"课程目录 (directory): {directory or '(无：数据库里只有未部署的课程身份)'}")
    if not plan.exists:
        print(f"数据库 (database): {plan.path}（不存在，没有记录可清理）")
        return
    print(f"数据库 (database): {plan.path}")
    print(f"  - courses: {'1 行（永久身份与已接受元数据）' if plan.course_row else '0 行'}")
    for name, count in plan.learner_rows:
        print(f"  - {name}: {count} 行（学习数据）")
    for name, count in plan.bookkeeping_rows:
        print(f"  - {name}: {count} 行（题库状态 / 退役记录）")
    if plan.clears_default_course:
        print(
            "  - schema_meta.default_course_id: 指向该课程，将一并清除"
            "（部署默认课程请改用 MCQ_DEFAULT_COURSE）"
        )


def _refuse_unsafe(
    course_id: str,
    plan: DatabasePlan,
    *,
    force: bool,
) -> None:
    """Refuse (before writing anything) the deletions that are not safe."""
    if plan.legacy_course_id == course_id:
        raise Refused(
            f'"{course_id}" 是数据库持久化的 legacy_course_id（schema_meta）：'
            "它的 courses 行会在每次启动时被 Database.initialize 重建，删除该 "
            "namespace 既不会生效，也会让“旧数据属于哪门课”失去归属。\n"
            "如果目的是把历史改挂到真实课程 ID，请使用 scripts/rename_course.py。"
        )
    if plan.learner_total and not force:
        raise Refused(
            f"该课程仍有 {plan.learner_total} 行学习数据（作答、错题、薄弱点、"
            "练习进度、考试记录）。这些数据一旦删除无法恢复。\n"
            "确认可以永久删除后，加 --force 重新执行。"
        )


def _report_deleted(deleted: dict[str, int]) -> None:
    print("已清理数据库记录：")
    for name in sorted(deleted):
        if name == DEFAULT_COURSE_KEY:
            print("  - schema_meta.default_course_id: 已清除")
        else:
            print(f"  - {name}: {deleted[name]} 行")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog=(
            "exit codes: 0 已删除（或 --dry-run）；1 被拒绝（未写入任何内容）；"
            "2 参数或 IO 问题。"
        ),
    )
    parser.add_argument("--course", required=True, help="course_id to delete")
    parser.add_argument(
        "--db",
        default=PROJECT_ROOT / "instance" / "mcq.db",
        type=Path,
        help="SQLite database holding the namespace (default: instance/mcq.db)",
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
        help="root questions.json of the legacy adapter",
    )
    parser.add_argument(
        "--glossary-file",
        default=PROJECT_ROOT / "glossary.json",
        type=Path,
        help="root glossary.json of the legacy adapter",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report everything that would be removed and write nothing",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "also delete a course that still owns learner data "
            "(progress, attempts, mistakes, exams)"
        ),
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="skip the timestamped database backup copy (not recommended)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        course_id = validate_course_id(args.course)
    except CourseIdError as exc:
        print(f"--course 不合法：{exc}", file=sys.stderr)
        return 2

    database_path = args.db.resolve()
    courses_dir = args.courses_dir.resolve()
    loader = build_loader(courses_dir, args.question_file, args.glossary_file)
    try:
        definition = loader.find_definition(course_id)
        declared = [item.course_id for item in loader.discover_definitions()]
    except CourseDefinitionError as exc:
        print(f"课程目录配置错误（全局，应用本身也无法启动）：\n{exc}", file=sys.stderr)
        return 1

    directory = (
        definition.root.resolve()
        if definition is not None and definition.manifest_path is not None
        else None
    )
    try:
        plan = inspect_database(database_path, course_id)
    except sqlite3.Error as exc:
        print(f"无法读取数据库 {database_path}：{exc}", file=sys.stderr)
        return 2

    _report(course_id, definition, directory, plan)

    if definition is not None and definition.manifest_path is None:
        print(
            "\n拒绝删除，未写入任何内容：\n"
            f'"{course_id}" 由根目录 questions.json/glossary.json 的 legacy adapter '
            "提供，没有 courses/<course_id>/ 目录可删除。\n"
            "请先用 python scripts/migrate_courses.py --layout 把根文件迁移到 manifest "
            "布局（或人工确认后处理根文件），再删除课程。",
            file=sys.stderr,
        )
        return 1

    if directory is None and not plan.owns_rows:
        print(
            f'\n未知课程 "{course_id}"：既没有 courses/<course_id>/course.json，'
            "数据库里也没有它的身份或任何数据。",
            file=sys.stderr,
        )
        print(
            f"已声明课程：{', '.join(declared) if declared else '(none)'}",
            file=sys.stderr,
        )
        return 1

    try:
        _refuse_unsafe(course_id, plan, force=args.force)
    except Refused as exc:
        print(f"\n拒绝删除，未写入任何内容：\n{exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        print("\n--dry-run：以上内容都会被删除，但本次没有写入任何内容。")
        return 0

    if plan.exists and not args.no_backup:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = database_path.with_suffix(database_path.suffix + f".bak-{stamp}")
        try:
            shutil.copy2(database_path, backup)
            shutil.copystat(database_path, backup)
        except OSError as exc:
            print(f"备份失败，未删除任何内容：{exc}", file=sys.stderr)
            return 2
        print(f"\n已备份：{backup}")

    if directory is not None:
        try:
            removed = remove_course_directory(definition, courses_dir)
        except (Refused, DeleteError, OSError) as exc:
            print(f"\n删除课程目录失败，未写入任何数据：{exc}", file=sys.stderr)
            return 1
        print(f"已删除课程目录：{removed}")

    if plan.has_namespace:
        try:
            deleted = delete_namespace(database_path, course_id)
        except (DeleteError, sqlite3.Error) as exc:
            print(
                "\n数据库清理失败（目录已删除，数据库保持原样）：\n"
                f"{exc}\n"
                "修复后重新运行本命令即可只清理数据库记录。",
                file=sys.stderr,
            )
            return 1
        _report_deleted(deleted)
    elif plan.exists:
        print(
            "数据库尚未迁移到多课程 schema（没有 course_id 命名空间）："
            "没有该课程的记录可清理。"
        )

    print(
        f"\n课程 {course_id} 已删除：课程目录、默认候选文件、数据库身份、"
        "该课程的学习数据、题库 generation 与退役 ID 记录都已清除。"
    )
    if plan.clears_default_course:
        print(
            "默认课程偏好也已清除：如需固定默认课程，请在部署配置中设置 "
            "MCQ_DEFAULT_COURSE。"
        )
    print(
        "注意：该课程原有的 question ID 退役记录不复存在，若以后重新加入同名课程，"
        "它的 ID 会从零开始，不会继承旧的退役状态。"
    )
    print("下一步：统一重启全部 worker，并用 python scripts/check_courses.py 复核。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

