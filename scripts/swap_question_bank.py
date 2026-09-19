"""Atomically publish a validated question bank for one course.

Non-atomic publishing (``cp`` over a live file, an editor saving the JSON in
place) can truncate or partially overwrite the file while a worker is starting
up.  The worker then reports ``Invalid JSON in question bank at line 1,
column N`` — a publishing artefact mistaken for a broken bank.  This helper

1. reads the candidate **once** and freezes those bytes;
2. validates exactly those frozen bytes;
3. resolves the target course and runs the read-only preflight (retired-ID
   reuse, destructive diffs) against the database;
4. writes the bytes to an immutable ``versions/<sha256>/questions.json``;
5. re-validates the publication baseline **inside** the course's publication
   lock, then switches the course manifest over with a single ``os.replace``
   rename and fsyncs the directory::

    python scripts/check_question_bank.py --course physical_design candidate.json
    python scripts/swap_question_bank.py --course physical_design candidate.json --db instance/mcq.db

This is the publish half of the unified flow: ``check_question_bank.py`` is the
read-only gate, and this command refuses to switch the manifest over unless that
gate (re-run internally) exits ``0``.  Glossary changes use the same flow with
``check_glossary.py`` + ``publish_course.py --glossary``.

Filesystem publication and database activation are **not** one transaction: the
command reports ``published, pending worker activation``.  It never restarts
anything and never bumps the course generation itself — the startup
reconciliation does that on the next worker activation.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.course_tooling import (  # noqa: E402
    ToolingError,
    build_loader,
    freeze_candidate,
    preflight_baseline,
    publish_questions,
    resolve_definition,
    validate_bytes,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "candidate",
        type=Path,
        help="fully written candidate question bank to publish",
    )
    parser.add_argument(
        "--course",
        default=None,
        help="course_id to publish (required when several courses are declared)",
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
        help="SQLite database used for the read-only preflight (never modified)",
    )
    parser.add_argument(
        "--skip-preflight",
        action="store_true",
        help="publish without diffing against the database (not recommended)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    loader = build_loader(args.courses_dir, args.question_file, args.glossary_file)
    try:
        definition = resolve_definition(loader, args.course)
        payload, digest = freeze_candidate(Path(args.candidate).resolve())
        questions = validate_bytes(payload)
    except (ToolingError, OSError) as exc:
        print(f"候选题库校验失败，未替换任何文件：\n{exc}", file=sys.stderr)
        return 1

    print(f"课程 (course_id): {definition.course_id} [{definition.layout}]")
    print(f"候选内容已冻结：sha256={digest[:16]}… ({len(payload)} bytes)")
    print(f"题库校验通过：{len(questions)} 道题。")

    if not args.skip_preflight:
        from scripts.check_question_bank import main as check_main

        exit_code = check_main(
            [
                str(args.candidate),
                "--course",
                definition.course_id,
                "--courses-dir",
                str(args.courses_dir),
                "--question-file",
                str(args.question_file),
                "--glossary-file",
                str(args.glossary_file),
                "--db",
                str(args.db),
            ]
        )
        if exit_code != 0:
            print(
                f"\n预检返回 {exit_code}：未发布任何内容。先修复或改用 "
                "check_question_bank.py 查看完整报告。",
                file=sys.stderr,
            )
            return exit_code

    baseline = preflight_baseline(definition)
    try:
        published, legacy_layout = publish_questions(
            definition, payload, digest, baseline=baseline
        )
    except (ToolingError, OSError) as exc:
        print(f"\n发布失败，未替换任何文件：\n{exc}", file=sys.stderr)
        return 1

    print(f"\n已发布 {len(questions)} 道题 -> {published}")
    if legacy_layout:
        print("布局：legacy 根目录文件（原子替换单文件，没有 manifest）")
    else:
        print("布局：manifest 已通过单次 rename 原子切换（内容不可变、按 sha256 归档）")
    print(
        "\n状态：published, pending worker activation（已发布，等待 worker 激活）。\n"
        "发布文件系统内容与数据库激活不是同一个事务：请统一重启全部应用工作进程，"
        "让每个 worker 加载同一份内容；未更新的进程只会围栏该课程，其他课程不受影响。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
