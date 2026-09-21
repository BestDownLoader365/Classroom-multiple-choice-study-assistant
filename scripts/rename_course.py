"""Rename one course namespace across the whole SQLite database and its directory.

A course's `course_id` is its permanent namespace: it is the first column of every
learner table and the key of `question_registry` / `question_bank_state`.  Renaming
a course therefore means rewriting that key consistently — otherwise the course
would look brand new (empty history) while the old rows stayed behind under the
old namespace, which reads as "all my progress disappeared".

    python scripts/rename_course.py --db instance/mcq.db --from legacy --to eek5106 --dry-run
    python scripts/rename_course.py --db instance/mcq.db --from legacy --to eek5106
    python scripts/rename_course.py --db instance/mcq.db --from legacy --to eek5106 \\
        --courses-dir courses --rename-directory
    python scripts/rename_course.py --db instance/mcq.db --courses-dir courses --recover

Guarantees:

* every precondition is verified read-only *before* anything is touched: the
  source namespace exists, the target namespace owns no metadata and no learner
  row, and (with ``--rename-directory``) the source directory exists, is a strict
  child of ``--courses-dir``, declares this ``course_id`` in its manifest, and the
  target directory does not exist;
* the database is copied to a timestamped, verified backup first (unless
  ``--no-backup``);
* ``--rename-directory`` runs as a three-phase protocol whose commit point is the
  database transaction:

      stage    courses/<from> -> courses/.rename-staging-<from>-<stamp>
               and rewrite that manifest's course_id to <to> (atomic replace)
      commit   one BEGIN IMMEDIATE transaction rewriting every course_id
      finalize courses/.rename-staging-... -> courses/<to>

  A failure before the commit is compensated (manifest restored from the bytes
  recorded in the state file, staging moved back).  A failure after the commit
  leaves a recorded ``db_committed`` state and exits ``3`` with the exact
  ``--recover`` command;
* the rename runs inside one ``BEGIN IMMEDIATE`` transaction on a dedicated
  connection, re-counts every table before and after, and rolls back on mismatch;
* ``schema_meta.default_course_id`` follows the rename, so the navigation
  preference never points at a namespace that no longer exists;
* ``exam_questions`` is deliberately untouched — it carries no ``course_id`` and
  always follows its parent ``exam_sessions`` row;
* when the renamed namespace is the persisted ``legacy_course_id``, that key is
  updated too, so a later startup does not recreate a phantom ``legacy`` course;
* ``--recover`` finishes (or undoes) an interrupted rename from its recorded
  state, and states that the protocol cannot produce are refused rather than
  guessed at.

This is an administrative operation.  It does not re-run retention, does not touch
the question-bank generation, and never recalculates a fingerprint.  In-flight
browser forms for the renamed course carry a signed course id valid for up to 12
hours, so reload the page after a rename.

Exit codes: ``0`` success (or dry run, or a fully resolved recovery), ``1`` refused
(nothing written, or fully compensated), ``2`` usage/IO, ``3`` the database change
is committed but the filesystem half still needs ``--recover``.
"""

from __future__ import annotations

import argparse
import json
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
    DEFAULT_COURSE_KEY,
    LEGACY_COURSE_KEY,
    read_meta,
)
from scripts.course_tooling import (  # noqa: E402
    BackupError,
    FilesystemTransactionError,
    ToolingError,
    atomic_rename,
    contained_path,
    list_rename_states,
    read_rename_state,
    remove_rename_state,
    rename_state_path,
    staging_directory,
    timestamped_backup,
    write_file_atomically,
    write_rename_state,
)

#: Tables whose rows are keyed by ``course_id`` (``exam_questions`` follows its
#: parent session and therefore stores no namespace of its own).
SCOPED_TABLES: tuple[str, ...] = COURSE_SCOPED_TABLES

#: The three recorded phases of a ``--rename-directory`` run.
PHASE_STAGING = "staging"
PHASE_STAGED = "staged"
PHASE_DB_COMMITTED = "db_committed"


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


def validate_rename(
    database_path: Path, source: str, target: str
) -> dict[str, int]:
    """Prove a rename is safe and return the rows that will move (read-only).

    Split out of :func:`rename_namespace` so the *whole* precondition set can be
    checked before any file is moved: the CLI runs this before it stages a
    directory, and ``--dry-run`` reports exactly its result.  Raises ``ValueError``
    for a precondition that refuses the rename, which the caller maps to exit 1.
    """
    if not database_path.is_file():
        raise ValueError(f"Database not found: {database_path}")
    connection = _open_rw(database_path)
    try:
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
        return _counts(connection, source)
    finally:
        connection.close()


def rename_namespace(
    database_path: Path,
    source: str,
    target: str,
    *,
    dry_run: bool = False,
) -> dict[str, int]:
    """Rewrite every ``course_id`` row from ``source`` to ``target`` atomically.

    ``schema_meta.default_course_id`` follows the rename when it pointed at the
    renamed namespace: leaving it behind would make the navigation preference
    point at a course that no longer exists (``delete_course.py`` clears the same
    key for the same reason).
    """
    before = validate_rename(database_path, source, target)
    connection = _open_rw(database_path)
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        tables = _table_names(connection)
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
            if read_meta(connection, DEFAULT_COURSE_KEY) == source:
                connection.execute(
                    "UPDATE schema_meta SET value = ? WHERE key = ?",
                    (target, DEFAULT_COURSE_KEY),
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


def validate_directory_rename(
    courses_dir: Path, source: str, target: str
) -> tuple[Path, Path]:
    """Prove the filesystem half is safe and return ``(source_dir, target_dir)``.

    Read-only, and deliberately strict: the source directory must be a strict
    child of ``--courses-dir``, must carry a manifest that declares exactly
    ``source`` (a directory whose manifest disagrees is not this course's to
    rename, and a directory *without* a manifest is not a course at all), the
    target directory must not exist, and no rename of ``source`` may already be in
    flight.
    """
    base = Path(courses_dir).resolve()
    source_dir = base / source
    target_dir = base / target
    if not source_dir.is_dir():
        raise ToolingError(f"课程目录不存在，无法改名：{source_dir}")
    if source_dir == base or not contained_path(source_dir, base):
        raise ToolingError(f"课程目录不在 --courses-dir 内，拒绝改名：{source_dir}")
    if target_dir.exists():
        raise ToolingError(
            f"目标课程目录已存在，拒绝改名（不会合并两个目录）：{target_dir}"
        )
    manifest_path = source_dir / "course.json"
    if not manifest_path.is_file():
        raise ToolingError(
            f"课程目录缺少 manifest，拒绝改名：{manifest_path}\n"
            "只有 courses/<course_id>/course.json 才声明一门课程；"
            "请先 publish_course.py --add，或去掉 --rename-directory 只改数据库。"
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ToolingError(f"无法读取 manifest {manifest_path}：{exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("course_id") != source:
        raise ToolingError(
            f'{manifest_path} 声明的 course_id 不是 "{source}"，拒绝改名目录。'
        )
    state_path = rename_state_path(base, source)
    if state_path.exists():
        raise ToolingError(
            f"检测到未完成的改名记录 {state_path}：\n"
            "请先运行 python scripts/rename_course.py --db <db> --courses-dir "
            f"{base} --recover 收尾或回滚，再重新执行。"
        )
    return source_dir, target_dir


def stage_directory(
    source_dir: Path,
    courses_dir: Path,
    source: str,
    target: str,
    state_path: Path,
    payload: dict,
) -> tuple[Path, str]:
    """Phase 1: move the directory aside and rewrite its manifest.

    Returns ``(staging_dir, original_manifest_text)``.  The original bytes are
    recorded in the state file (and returned) so compensation — here, or later by
    ``--recover`` — restores the manifest exactly rather than approximately.

    A failure *after* the move is compensated here: the manifest and the directory
    are put back and the state file is removed, so a failed run leaves no trace to
    clean up.  Only a failure of that compensation keeps the state file, which is
    exactly when an operator needs it.
    """
    staging = staging_directory(courses_dir, source)
    payload = dict(payload, phase=PHASE_STAGING, staging=str(staging))
    manifest_path = source_dir / "course.json"
    try:
        original_manifest = manifest_path.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - validate_* already read it
        raise ToolingError(f"无法读取 manifest {manifest_path}：{exc}") from exc
    payload["original_manifest"] = original_manifest
    write_rename_state(state_path, payload)

    atomic_rename(source_dir, staging)
    try:
        manifest = json.loads(original_manifest)
        manifest["course_id"] = target
        write_file_atomically(
            staging / "course.json",
            json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8") + b"\n",
        )
    except (OSError, ValueError, UnicodeError) as exc:
        try:
            restore_directory(staging, source_dir, original_manifest)
        except FilesystemTransactionError:
            # Leave the state file: this is the case that needs a human.
            raise
        remove_rename_state(state_path)
        raise ToolingError(
            f"写入 manifest 失败，已恢复课程目录与 manifest：{exc}"
        ) from exc
    payload["phase"] = PHASE_STAGED
    write_rename_state(state_path, payload)
    return staging, original_manifest


def finalize_directory(staging: Path, target_dir: Path) -> None:
    """Phase 3: move the staged directory onto its final name (idempotent)."""
    atomic_rename(staging, target_dir)


def restore_directory(staging: Path, source_dir: Path, original_manifest: str) -> None:
    """Compensation: put the manifest and the directory back as they were."""
    if original_manifest:
        write_file_atomically(
            staging / "course.json", original_manifest.encode("utf-8")
        )
    atomic_rename(staging, source_dir)


def _manifest_declares(directory: Path, course_id: str) -> bool:
    """Whether ``directory/course.json`` exists and declares ``course_id``."""
    try:
        manifest = json.loads((directory / "course.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return isinstance(manifest, dict) and manifest.get("course_id") == course_id


def _namespace_exists(database_path: Path, course_id: str) -> bool:
    """Whether the database still owns ``course_id`` (metadata or any row)."""
    connection = _open_rw(database_path)
    try:
        if "courses" not in _table_names(connection):
            return False
        row = connection.execute(
            "SELECT course_id FROM courses WHERE course_id = ?", (course_id,)
        ).fetchone()
        return row is not None or any(_counts(connection, course_id).values())
    finally:
        connection.close()



def _recover_one(
    state_path: Path,
    payload: dict,
    courses_dir: Path,
    database_path: Path,
    *,
    dry_run: bool,
) -> bool:
    """Resolve one recorded rename; return ``False`` when a human must look."""
    source = str(payload.get("source", ""))
    target = str(payload.get("target", ""))
    staging = Path(str(payload.get("staging", "")))
    original_manifest = str(payload.get("original_manifest") or "")
    source_dir = Path(courses_dir) / source
    target_dir = Path(courses_dir) / target
    db_source = _namespace_exists(database_path, source)
    db_target = _namespace_exists(database_path, target)
    staging_exists = staging.is_dir()

    print(
        f"\n状态文件：{state_path}\n"
        f"  source={source} target={target} phase={payload.get('phase')}\n"
        f"  数据库：source={'yes' if db_source else 'no'} "
        f"target={'yes' if db_target else 'no'}\n"
        f"  目录：courses/{source}={'yes' if source_dir.is_dir() else 'no'} "
        f"staging={'yes' if staging_exists else 'no'}"
    )

    if db_source and db_target:
        print(
            "  拒绝：两个 namespace 同时存在，这不是本协议能产生的状态，请人工核对："
            f"\n    source={source} target={target} 备份={payload.get('backup')}",
            file=sys.stderr,
        )
        return False
    if not db_source and not db_target:
        print(
            "  拒绝：两个 namespace 都不存在，无法判断该回滚还是收尾，请人工核对"
            f"（备份：{payload.get('backup')}）",
            file=sys.stderr,
        )
        return False

    if db_source:  # the commit never happened -> undo phase 1
        if staging_exists and not source_dir.is_dir():
            if dry_run:
                print(f"  将要回滚：{staging} -> {source_dir}（并恢复 manifest）")
                return True
            restore_directory(staging, source_dir, original_manifest)
            remove_rename_state(state_path)
            print(f"  已回滚：{staging} -> {source_dir}；请重新运行完整的改名命令。")
            return True
        if source_dir.is_dir() and not staging_exists:
            if dry_run:
                print("  将要删除状态文件（改名尚未开始，目录仍在原位）")
                return True
            remove_rename_state(state_path)
            print("  已清理状态文件：改名尚未开始，目录仍在原位。")
            return True
        print(
            "  拒绝：目录位置与数据库状态不一致，需要人工确认："
            f"\n    staging={staging} source_dir={source_dir}",
            file=sys.stderr,
        )
        return False

    # db_target: the commit happened -> finish phase 3
    if staging_exists and not target_dir.is_dir():
        if dry_run:
            print(f"  将要收尾：{staging} -> {target_dir}")
            return True
        if not _manifest_declares(staging, target):
            print(
                f'  拒绝：{staging}/course.json 未声明 "{target}"，需要人工确认。',
                file=sys.stderr,
            )
            return False
        finalize_directory(staging, target_dir)
        remove_rename_state(state_path)
        print(f"  已收尾：{staging} -> {target_dir}")
        return True
    if target_dir.is_dir() and not staging_exists:
        if not _manifest_declares(target_dir, target):
            print(
                f'  拒绝：{target_dir}/course.json 未声明 "{target}"，需要人工确认。',
                file=sys.stderr,
            )
            return False
        if dry_run:
            print("  将要删除状态文件（改名已完成）")
            return True
        remove_rename_state(state_path)
        print("  已清理状态文件：改名已完成，目录与 manifest 都正确。")
        return True
    print(
        "  拒绝：目录位置与数据库状态不一致，需要人工确认："
        f"\n    staging={staging} target_dir={target_dir}",
        file=sys.stderr,
    )
    return False


def recover(courses_dir: Path, database_path: Path, *, dry_run: bool = False) -> int:
    """Finish or undo every interrupted ``--rename-directory`` run.

    The decision comes from the *observable* state (which namespace the database
    owns, where the directory currently is), with the state file supplying the
    intent: source, target, staging path and the original manifest bytes.
    Combinations the protocol cannot produce are refused, never guessed.
    """
    states = list_rename_states(courses_dir)
    if not states:
        print(f"没有未完成的改名记录：{courses_dir}")
        return 0

    failures = 0
    for state_path in states:
        payload = read_rename_state(state_path)
        if payload is None:
            print(f"无法解析状态文件，需要人工检查：{state_path}", file=sys.stderr)
            failures += 1
            continue
        if not _recover_one(
            state_path, payload, courses_dir, database_path, dry_run=dry_run
        ):
            failures += 1
    if failures:
        print(f"\n{failures} 个状态需要人工介入，未自动处理。", file=sys.stderr)
        return 1
    if dry_run:
        print("\n--dry-run：以上操作都没有执行。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog=(
            "exit codes: 0 成功（含 --dry-run 与已解决的 --recover）；"
            "1 被拒绝（未写入任何内容，或已完整补偿 / --recover 需要人工介入）；"
            "2 参数或 IO 问题；"
            "3 数据库已改名但目录改名尚未完成，请运行 --recover。"
        ),
    )
    parser.add_argument(
        "--db",
        default=PROJECT_ROOT / "instance" / "mcq.db",
        type=Path,
        help="SQLite database holding the namespace (default: instance/mcq.db)",
    )
    parser.add_argument("--from", dest="source", help="current course_id")
    parser.add_argument("--to", dest="target", help="new course_id")
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
        help="course directory used by --rename-directory and --recover",
    )
    parser.add_argument(
        "--rename-directory",
        action="store_true",
        help=(
            "also move courses/<from> to courses/<to> and update its manifest, as a "
            "staged protocol whose commit point is the database transaction"
        ),
    )
    parser.add_argument(
        "--recover",
        action="store_true",
        help=(
            "finish or undo an interrupted --rename-directory run from its recorded "
            "state; it never renames a namespace itself"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    courses_dir = args.courses_dir.resolve()
    database_path = args.db.resolve()

    if args.recover:
        if not database_path.is_file():
            print(f"数据库不存在：{database_path}", file=sys.stderr)
            return 2
        return recover(courses_dir, database_path, dry_run=args.dry_run)

    if not args.source or not args.target:
        print("--from 与 --to 都是必填（--recover 除外）。", file=sys.stderr)
        return 2
    try:
        source = validate_course_id(args.source)
        target = validate_course_id(args.target)
    except CourseIdError as exc:
        print(f"course_id 不合法：{exc}", file=sys.stderr)
        return 2
    if source == target:
        print("源与目标相同，无需重命名。")
        return 0
    if not database_path.is_file():
        print(f"数据库不存在：{database_path}", file=sys.stderr)
        return 2

    # ---- Phase 0: every precondition, read-only, before anything is touched.
    try:
        counts = validate_rename(database_path, source, target)
    except ValueError as exc:
        print(f"重命名被拒绝，未写入任何数据：\n{exc}", file=sys.stderr)
        return 1
    source_dir = target_dir = None
    if args.rename_directory:
        try:
            source_dir, target_dir = validate_directory_rename(
                courses_dir, source, target
            )
        except ToolingError as exc:
            print(f"重命名被拒绝，未写入任何数据：\n{exc}", file=sys.stderr)
            return 1

    verb = "将重命名" if args.dry_run else "已重命名"
    print(f"{verb}课程 namespace：{source} -> {target}")
    for table, count in sorted(counts.items()):
        if count:
            print(f"  - {table}: {count} 行")
    if source_dir is not None and target_dir is not None:
        print(f"课程目录：{source_dir} -> {target_dir}（暂存后转正）")
    if args.dry_run:
        print("\n--dry-run：没有写入任何内容。")
        return 0

    backup: Path | None = None
    if not args.no_backup:
        try:
            backup = timestamped_backup(database_path)
        except BackupError as exc:
            print(f"备份失败，未重命名任何内容：{exc}", file=sys.stderr)
            return 2
        print(f"已备份（quick_check 通过）：{backup}")

    state_path = rename_state_path(courses_dir, source)
    payload = {
        "tool": "rename_course.py",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "phase": None,
        "source": source,
        "target": target,
        "courses_dir": str(courses_dir),
        "staging": None,
        "database": str(database_path),
        "backup": None if backup is None else str(backup),
        "original_manifest": None,
    }

    # ---- Phase 1 (reversible): stage the directory and rewrite its manifest.
    staging: Path | None = None
    original_manifest = ""
    if source_dir is not None:
        try:
            staging, original_manifest = stage_directory(
                source_dir, courses_dir, source, target, state_path, payload
            )
        except (ToolingError, FilesystemTransactionError, OSError) as exc:
            print(f"暂存课程目录失败，数据库未改动：{exc}", file=sys.stderr)
            return 1
        print(f"已暂存课程目录：{source_dir} -> {staging}")

    # ---- Phase 2 (transactional): the commit point.
    try:
        rename_namespace(database_path, source, target)
    except ValueError as exc:
        if staging is None:
            print(f"重命名被拒绝，未写入任何数据：\n{exc}", file=sys.stderr)
            return 1
        try:
            restore_directory(staging, source_dir, original_manifest)
        except FilesystemTransactionError as restore_exc:
            print(
                "\n数据库改名失败，且补偿（恢复课程目录）也失败，需要人工介入：\n"
                f"  主操作失败：{exc}\n"
                f"  补偿失败：{restore_exc}\n"
                f"  暂存目录：{staging}\n"
                f"  期望位置：{source_dir}\n"
                f"  请手工执行：mv {staging} {source_dir}\n"
                f"  状态文件：{state_path}",
                file=sys.stderr,
            )
            return 3
        remove_rename_state(state_path)
        print(
            "数据库改名失败，已回滚并恢复课程目录，未改名任何内容：\n"
            f"{exc}",
            file=sys.stderr,
        )
        return 1

    # ---- Phase 3: promote the staged directory (the only step left).
    if staging is not None:
        write_rename_state(state_path, dict(payload, phase=PHASE_DB_COMMITTED,
                                           staging=str(staging),
                                           original_manifest=original_manifest))
        try:
            finalize_directory(staging, target_dir)
        except (FilesystemTransactionError, OSError) as exc:
            print(
                "\n数据库已改名，但目录尚未转正（需要收尾）：\n"
                f"  暂存目录：{staging}\n"
                f"  目标位置：{target_dir}\n"
                f"  原因：{exc}\n"
                "请运行："
                f"python scripts/rename_course.py --db {database_path} "
                f"--courses-dir {courses_dir} --recover",
                file=sys.stderr,
            )
            return 3
        remove_rename_state(state_path)
        print(f"已重命名课程目录：{staging} -> {target_dir}")

    print(
        "\n注意：历史数据只是换了 namespace，没有被清理，也没有重算成绩或 fingerprint。"
    )
    if source_dir is None:
        print(
            "下一步：确认课程目录（courses/<course_id>/）与 manifest 的 course_id 一致，"
            "然后重启 worker。"
        )
    else:
        print(
            "提示：manifest 的 course_id 已更新；title/title_zh 属于内容决定，请按需修改。"
            "\n下一步：统一重启全部 worker，并用 GET /ready/<course_id> 确认；"
            "浏览器中已打开的旧表单需要刷新（签名上下文最长 12 小时有效）。"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

