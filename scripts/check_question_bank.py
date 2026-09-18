"""Pre-deploy question-bank check: validate and diff against a database.

Run this before restarting the service with a new ``questions.json`` to see
exactly what the startup reconciliation would do — and to catch retired-ID
reuse *before* it can take the deployment down::

    python scripts/check_question_bank.py
    python scripts/check_question_bank.py path/to/questions.json --db instance/mcq.db
    python scripts/check_question_bank.py --strict

The database is opened read-only and never modified.

Exit codes:

``0``
    Nothing blocks the deploy.  Either the bank did not change in a
    data-affecting way, or the changes only add/move content while keeping
    every learner record.
``1``
    The bank itself fails validation (bad JSON, bad schema, duplicate IDs...).
``2``
    Blocking: a retired question ID is reused for a different question.
    Startup would refuse to run; assign fresh IDs instead.
``3``
    ``--strict`` only: the update is allowed but would clear parts of
    learners' stored state (grading-changed or deleted questions).

Clearing learner state is *not* a deployment blocker on its own, so the
default run still exits ``0`` for it — but it always reports exactly which
questions lose their attempts/wrong-question/SRS state and never claims the
deploy is data-loss-free.
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
            # Databases created before placement tracking have no column: the
            # sync adopts the current mapping on the next startup, so the
            # preflight cannot (and must not) report a move either.
            placement_fingerprint=_placement_of(row),
        )
        for row in rows
    }


def _placement_of(row: sqlite3.Row) -> str:
    """Read the optional placement column of a possibly old registry row."""
    if "placement_fingerprint" not in row.keys():
        return ""
    return row["placement_fingerprint"] or ""


def _legacy_tombstones(
    registry: dict[str, QuestionRegistryEntry],
) -> tuple[str, ...]:
    """Retired IDs without a recorded grading identity.

    They come from pre-registry databases (the ID only ever appeared in
    learner history) and are adopted instead of rejected if they reappear,
    which means a *different* question could inherit their old history.  The
    check lists them so maintainers can decide to rename the content instead.
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


def _warning(title: str, ids: tuple[str, ...], detail: str) -> None:
    """Print one actionable warning block on stderr."""
    print(f"\n⚠ {title}: {len(ids)}", file=sys.stderr)
    for identifier in ids:
        print(f"    - {identifier}", file=sys.stderr)
    print(f"  {detail}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog=(
            "exit codes: 0 可部署（含允许的清理行为，会打印警告）；"
            "1 题库校验失败；2 非法复用退役 ID（阻止启动）；"
            "3 --strict 下检测到学习状态清理。"
        ),
    )
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
    parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "exit 3 instead of 0 when the update would clear learner state "
            "(grading-changed or deleted questions)"
        ),
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
        print("\n可以安全部署。")
        return 0

    diff = diff_questions(questions, registry)
    _report("新增题目（不清理数据）(new)", diff.new_ids)
    _report("内容修改（历史保留）(content-only)", diff.content_changed_ids)
    _report(
        "章节/来源调整（历史保留，但会推进 generation）(placement-changed)",
        diff.placement_changed_ids,
    )
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
            return 3
        print(
            "\n注意：本次更新会清理上述学习状态；确认可以接受后再部署，"
            "并统一重启全部工作进程。"
        )
        return 0

    if diff.placement_changed_ids:
        print(
            "\n章节/来源调整会推进题库 generation：部署后请统一重启全部工作进程，"
            "旧进程的学习页面会返回 503。"
        )
    print("\n可以安全部署。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())