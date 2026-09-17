"""Pre-deploy question-bank check: validate and diff against a database.

Run this before restarting the service with a new ``questions.json`` to see
exactly what the startup reconciliation would do — and to catch retired-ID
reuse *before* it can take the deployment down::

    python scripts/check_question_bank.py
    python scripts/check_question_bank.py path/to/questions.json --db instance/mcq.db

The database is opened read-only and never modified.  Exit codes: ``0`` when
the bank is safe to deploy, ``1`` when the bank fails validation, and ``2``
when it reuses a retired question ID for a different question.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.models import QuestionRegistryEntry, QuestionRegistryStatus  # noqa: E402
from app.repositories import QuestionBankError, QuestionLoader  # noqa: E402
from app.services import diff_questions  # noqa: E402


def _load_registry(database_path: Path) -> dict[str, QuestionRegistryEntry] | None:
    """Read the registry from a database, or ``None`` when it is absent."""
    if not database_path.is_file():
        return None
    connection = sqlite3.connect(
        f"file:{database_path}?mode=ro", uri=True
    )
    connection.row_factory = sqlite3.Row
    try:
        table = connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name = 'question_registry'"
        ).fetchone()
        if table is None:
            return None
        rows = connection.execute("SELECT * FROM question_registry").fetchall()
    finally:
        connection.close()
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
        )
        for row in rows
    }


def _report(title: str, ids: tuple[str, ...]) -> None:
    print(f"{title}: {len(ids)}")
    for question_id in ids:
        print(f"    - {question_id}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "question_file",
        nargs="?",
        default=PROJECT_ROOT / "questions.json",
        type=Path,
        help="questions.json to validate (default: project root file)",
    )
    parser.add_argument(
        "--db",
        default=PROJECT_ROOT / "instance" / "mcq.db",
        type=Path,
        help="SQLite database to diff against (opened read-only)",
    )
    args = parser.parse_args(argv)

    try:
        questions = QuestionLoader(args.question_file).load()
    except QuestionBankError as exc:
        print(f"题库校验失败：\n{exc}", file=sys.stderr)
        return 1
    print(f"题库校验通过：{len(questions)} 道题。")

    registry = _load_registry(args.db)
    if not registry:
        print(
            "数据库中没有 question_registry 记录：首次启动将建立基线，"
            "不会清理任何学习数据。"
        )
        return 0

    diff = diff_questions(questions, registry)
    _report("新增题目 (new)", diff.new_ids)
    _report("内容修改（历史保留）(content-only)", diff.content_changed_ids)
    _report("判题规则变化（仅清理该题历史）(grading-changed)", diff.grading_changed_ids)
    _report("删除题目（attempts 保留，错题/SRS 静默清除）(deleted)", diff.deleted_ids)
    _report("恢复原题 (resurrected)", diff.resurrected_ids)
    if diff.violations:
        print(
            "\n错误：以下已退役的 question ID 被复用于判题规则不同的题目：\n    - "
            + "\n    - ".join(diff.violations)
            + "\n退役 ID 永久保留，请为新题分配新 ID。",
            file=sys.stderr,
        )
        return 2
    print("\n可以安全部署。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())