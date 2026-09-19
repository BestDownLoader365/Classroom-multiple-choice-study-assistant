"""Rename one course namespace across the whole SQLite database.

A course's `course_id` is its permanent namespace: it is the first column of every
learner table and the key of `question_registry` / `question_bank_state`.  Renaming
a course therefore means rewriting that key consistently — otherwise the course
would look brand new (empty history) while the old rows stayed behind under the
old namespace, which reads as "all my progress disappeared".

    python scripts/rename_course.py --db instance/mcq.db --from legacy --to eek5106 --dry-run
    python scripts/rename_course.py --db instance/mcq.db --from legacy --to eek5106
    python scripts/rename_course.py --db instance/mcq.db --from legacy --to eek5106 \\
        --courses-dir courses --rename-directory

Guarantees:

* the database is copied to a timestamped backup first (unless ``--no-backup``);
* the rename runs inside one ``BEGIN IMMEDIATE`` transaction on a dedicated
  connection, re-counts every table before and after, and rolls back on mismatch;
* it refuses to run when the target namespace already owns course metadata or any
  learner row, so two identities can never be merged by accident;
* ``exam_questions`` is deliberately untouched — it carries no ``course_id`` and
  always follows its parent ``exam_sessions`` row;
* when the renamed namespace is the persisted ``legacy_course_id``, that key is
  updated too, so a later startup does not recreate a phantom ``legacy`` course.

This is an administrative operation.  It does not re-run retention, does not touch
the question-bank generation, and never recalculates a fingerprint.

Exit codes: ``0`` success, ``1`` refused (nothing written), ``2`` usage/IO.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.models import validate_course_id  # noqa: E402
from app.models.course import CourseIdError  # noqa: E402
from app.repositories.schema_migrations import (  # noqa: E402
    COURSE_SCOPED_TABLES,
    LEGACY_COURSE_KEY,
    read_meta,
)

#: Tables whose rows are keyed by ``course_id`` (``exam_questions`` follows its
#: parent session and therefore stores no namespace of its own).
SCOPED_TABLES: tuple[str, ...] = COURSE_SCOPED_TABLES


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in connection.execute(f'PRAGMA table_info("{table}")')}


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def _counts(connection: sqlite3.Connection, course_id: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in SCOPED_TABLES:
        if table not in _table_names(connection) or "course_id" not in _columns(
            connection, table
        ):
            continue
        counts[table] = int(
            connection.execute(
                f'SELECT COUNT(*) AS total FROM "{table}" WHERE course_id = ?',
                (course_id,),
            ).fetchone()["total"]
        )
    return counts


def _open_rw(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    return connection


def rename_namespace(
    database_path: Path,
    source: str,
    target: str,
    *,
    dry_run: bool = False,
) -> dict[str, int]:
    """Rewrite every ``course_id`` row from ``source`` to ``target`` atomically."""
    if not database_path.is_file():
        raise ValueError(f"Database not found: {database_path}")
    connection = _open_rw(database_path)
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        tables = _table_names(connection)
        if "courses" not in tables:
            raise ValueError(
                f"{database_path} has no 'courses' table: run "
                "scripts/migrate_courses.py first."
            )
        target_metadata = connection.execute(
            "SELECT course_id FROM courses WHERE course_id = ?", (target,)
        ).fetchone()
        target_rows = _counts(connection, target)
        if target_metadata is not None or any(target_rows.values()):
            raise ValueError(
                f'Namespace "{target}" already exists (metadata '
                f"{'yes' if target_metadata else 'no'}, rows "
                f"{sum(target_rows.values())}). Refusing to merge two identities."
            )
        source_metadata = connection.execute(
            "SELECT course_id, title FROM courses WHERE course_id = ?", (source,)
        ).fetchone()
        if source_metadata is None:
            raise ValueError(f'Namespace "{source}" has no course metadata row.')
        before = _counts(connection, source)
        if dry_run:
            return before

        connection.execute("BEGIN IMMEDIATE")
        try:
            for table in SCOPED_TABLES:
                if table not in tables or "course_id" not in _columns(connection, table):
                    continue
                connection.execute(
                    f'UPDATE "{table}" SET course_id = ? WHERE course_id = ?',
                    (target, source),
                )
            cursor = connection.execute(
                "UPDATE courses SET course_id = ?, updated_at = ? WHERE course_id = ?",
                (target, datetime.now(timezone.utc).isoformat(), source),
            )
            if cursor.rowcount != 1:  # pragma: no cover - guarded above
                raise ValueError("course metadata row vanished mid-rename")
            if read_meta(connection, LEGACY_COURSE_KEY) == source:
                connection.execute(
                    "UPDATE schema_meta SET value = ? WHERE key = ?",
                    (target, LEGACY_COURSE_KEY),
                )
            after = _counts(connection, target)
            mismatch = {
                table: (count, after.get(table))
                for table, count in before.items()
                if after.get(table) != count
            }
            if mismatch:
                raise ValueError(f"Rename lost rows {mismatch}. Rolled back.")
            leftover = {k: v for k, v in _counts(connection, source).items() if v}
            if leftover:  # pragma: no cover - defensive
                raise ValueError(f'Rows remained under "{source}": {leftover}')
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:  # pragma: no cover - defensive
            raise ValueError(f"Foreign key check failed after the rename: {violations}")
        return before
    finally:
        connection.close()


def _rename_directory(courses_dir: Path, source: str, target: str) -> int:
    """Move ``courses/<source>`` to ``courses/<target>`` and fix its manifest."""
    source_dir = courses_dir / source
    target_dir = courses_dir / target
    if not source_dir.is_dir():
        print(f"课程目录不存在，跳过：{source_dir}", file=sys.stderr)
        return 1
    if target_dir.exists():
        print(f"目标课程目录已存在，跳过：{target_dir}", file=sys.stderr)
        return 1
    source_dir.rename(target_dir)
    manifest_path = target_dir / "course.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["course_id"] = target
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(f"已重命名课程目录：{source_dir} -> {target_dir}")
    print("提示：manifest 的 course_id 已更新；title/title_zh 属于内容决定，请按需修改。")
    return 0



def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--db",
        default=PROJECT_ROOT / "instance" / "mcq.db",
        type=Path,
        help="SQLite database holding the namespace (default: instance/mcq.db)",
    )
    parser.add_argument("--from", dest="source", required=True, help="current course_id")
    parser.add_argument("--to", dest="target", required=True, help="new course_id")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report the rows that would move and write nothing",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="skip the timestamped backup copy (not recommended)",
    )
    parser.add_argument(
        "--courses-dir",
        default=PROJECT_ROOT / "courses",
        type=Path,
        help="course directory used by --rename-directory",
    )
    parser.add_argument(
        "--rename-directory",
        action="store_true",
        help="also move courses/<from> to courses/<to> and update its manifest",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        source = validate_course_id(args.source)
        target = validate_course_id(args.target)
    except CourseIdError as exc:
        print(f"course_id 不合法：{exc}", file=sys.stderr)
        return 2
    if source == target:
        print("源与目标相同，无需重命名。")
        return 0

    database_path = args.db.resolve()
    if not database_path.is_file():
        print(f"数据库不存在：{database_path}", file=sys.stderr)
        return 2

    if not args.dry_run and not args.no_backup:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = database_path.with_suffix(database_path.suffix + f".bak-{stamp}")
        shutil.copy2(database_path, backup)
        shutil.copystat(database_path, backup)
        print(f"已备份：{backup}")

    try:
        counts = rename_namespace(database_path, source, target, dry_run=args.dry_run)
    except ValueError as exc:
        print(f"重命名被拒绝，未写入任何数据：\n{exc}", file=sys.stderr)
        return 1

    verb = "将重命名" if args.dry_run else "已重命名"
    print(f"{verb}课程 namespace：{source} -> {target}")
    for table, count in sorted(counts.items()):
        if count:
            print(f"  - {table}: {count} 行")
    if args.dry_run:
        print("\n--dry-run：没有写入任何内容。")
        return 0
    print(
        "\n注意：历史数据只是换了 namespace，没有被清理，也没有重算成绩或 fingerprint。"
        "\n下一步：确认课程目录（courses/<course_id>/）与 manifest 的 course_id 一致，"
        "然后重启 worker。"
    )
    if args.rename_directory:
        if _rename_directory(args.courses_dir.resolve(), source, target) != 0:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

