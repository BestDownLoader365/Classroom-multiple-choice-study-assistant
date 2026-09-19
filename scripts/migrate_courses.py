"""Transactional multi-course migration / inspection for a SQLite database.

The namespace migration moves every pre-existing row into the fixed
``LEGACY_COURSE_ID`` namespace exactly once.  It is transactional, lossless and
idempotent, and it never runs retention cleanup, question-bank diffing or
learner reconciliation — those are separate steps performed by the workers.

    python scripts/migrate_courses.py --db instance/mcq.db --dry-run
    python scripts/migrate_courses.py --db instance/mcq.db            # backup + migrate
    python scripts/migrate_courses.py --db instance/mcq.db --no-backup

``--dry-run`` only reads.  A real run first copies the database to a timestamped
backup next to it (unless ``--no-backup``) and then migrates.  ``--layout``
additionally materialises the legacy root files as ``courses/<legacy>/``.
Exit codes: ``0`` success, ``1`` migration refused (reported, nothing written),
``2`` usage/IO problem.
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

from app.models import LEGACY_COURSE_ID  # noqa: E402
from app.repositories import CourseLoader, Database  # noqa: E402
from app.repositories.schema_migrations import (  # noqa: E402
    LEGACY_COURSE_KEY,
    SCHEMA_VERSION,
    SCHEMA_VERSION_KEY,
    SchemaMigrationError,
    read_meta,
)


def _inspect(database_path: Path) -> int:
    """Print the current layout without modifying anything."""
    if not database_path.is_file():
        print(f"数据库不存在：{database_path}")
        return 0
    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        tables = sorted(
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        )
        print(f"数据库：{database_path}")
        print(f"表：{', '.join(tables) if tables else '(空)'}")
        print(
            f"schema_version：{read_meta(connection, SCHEMA_VERSION_KEY) or '(未记录)'}"
        )
        print(
            f"legacy_course_id：{read_meta(connection, LEGACY_COURSE_KEY) or '(未记录)'}"
        )
        columns = {
            name: {
                row["name"] for row in connection.execute(f'PRAGMA table_info("{name}")')
            }
            for name in tables
        }
        needs_migration = any(
            "course_id" not in columns.get(name, set())
            for name in (
                "quiz_progress",
                "attempts",
                "wrong_questions",
                "weak_knowledge_points",
                "exam_sessions",
                "question_bank_state",
                "question_registry",
            )
        )
        print(f"需要多课程 namespace migration：{'yes' if needs_migration else 'no'}")
        for name in tables:
            count = connection.execute(
                f'SELECT COUNT(*) AS total FROM "{name}"'
            ).fetchone()["total"]
            print(f"  - {name}: {count} 行")
        if "exam_questions" in tables and "exam_sessions" in tables:
            orphans = connection.execute(
                "SELECT COUNT(*) AS total FROM exam_questions "
                "WHERE exam_id NOT IN (SELECT id FROM exam_sessions)"
            ).fetchone()["total"]
            print(f"孤儿 exam_questions 行：{orphans}")
    finally:
        connection.close()
    return 0


def _migrate_layout(
    courses_dir: Path, question_file: Path, glossary_file: Path
) -> int:
    """Materialise the legacy root files as one manifest course.

    The root files are **copied**, never moved, so a rollback is a plain
    ``rm -rf`` of the new directory.  The ``course_id`` is the persisted legacy
    namespace, which keeps every historical row owned by the same course.
    """
    target = courses_dir / LEGACY_COURSE_ID
    manifest_path = target / "course.json"
    if target.exists() and any(target.iterdir()):
        print(
            f"目标目录已存在且非空：{target}\n"
            "为避免覆盖内容，本次不做任何修改。请人工确认后重试。",
            file=sys.stderr,
        )
        return 1
    if not question_file.is_file():
        print(f"根题库不存在，无需迁移布局：{question_file}", file=sys.stderr)
        return 1
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(question_file, target / "questions.json")
    glossary_name: str | None = None
    if glossary_file.is_file():
        shutil.copy2(glossary_file, target / "glossary.json")
        glossary_name = "glossary.json"
    manifest = {
        "schema_version": 1,
        "course_id": LEGACY_COURSE_ID,
        "title": "Legacy course namespace",
        "title_zh": "旧版课程命名空间",
        "enabled": True,
        "questions": "questions.json",
        "glossary": glossary_name,
        "order": -1000000,
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"已写入 {manifest_path}（内容为根文件的副本；根文件保持原样）")
    print(
        "下一步：在确认新布局的课程能正常加载后删除根 questions.json/"
        "glossary.json —— 两者同时存在会被判定为重复 course_id 并导致启动失败。"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--db",
        default=PROJECT_ROOT / "instance" / "mcq.db",
        type=Path,
        help="SQLite database to migrate (default: instance/mcq.db)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="only inspect the current layout; write nothing",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="skip the timestamped backup copy (not recommended)",
    )
    parser.add_argument(
        "--legacy-course-id",
        default=LEGACY_COURSE_ID,
        help="namespace for the pre-existing rows (default: legacy)",
    )
    parser.add_argument(
        "--courses-dir",
        default=PROJECT_ROOT / "courses",
        type=Path,
        help="course directory used to register accepted metadata",
    )
    parser.add_argument(
        "--question-file",
        default=PROJECT_ROOT / "questions.json",
        type=Path,
        help="root questions.json used for the legacy adapter",
    )
    parser.add_argument(
        "--glossary-file",
        default=PROJECT_ROOT / "glossary.json",
        type=Path,
        help="root glossary.json used for the legacy adapter",
    )
    parser.add_argument(
        "--layout",
        action="store_true",
        help="also materialise the legacy root files as courses/<legacy>/",
    )
    return parser



def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    database_path = args.db.resolve()
    if args.dry_run:
        return _inspect(database_path)
    if not database_path.is_file():
        print(f"数据库不存在：{database_path}", file=sys.stderr)
        return 2

    if not args.no_backup:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = database_path.with_suffix(database_path.suffix + f".bak-{stamp}")
        shutil.copy2(database_path, backup)
        shutil.copystat(database_path, backup)
        print(f"已备份：{backup}")

    definitions = CourseLoader(
        args.courses_dir,
        legacy_directory=args.question_file.parent,
        legacy_questions_name=args.question_file.name,
        legacy_glossary_name=args.glossary_file.name,
    ).discover_definitions()

    database = Database(database_path)
    try:
        info = database.initialize(legacy_course_id=args.legacy_course_id)
    except SchemaMigrationError as exc:
        print(f"迁移被拒绝，未写入任何数据：\n{exc}", file=sys.stderr)
        return 1
    print(f"schema_version：{info.schema_version}（目标 {SCHEMA_VERSION}）")
    print(f"legacy_course_id（已持久化）：{info.legacy_course_id}")
    print(f"本次执行了 namespace migration：{'yes' if info.migrated else 'no'}")
    print("已登记课程：" + ", ".join(item.course_id for item in definitions))
    print(
        "\n注意：旧版应用无法在新的 Schema 上安全运行。回滚请使用上面的备份副本，"
        "并同时回滚应用版本。"
    )
    if args.layout:
        return _migrate_layout(
            args.courses_dir, args.question_file, args.glossary_file
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

