"""Course-level operations: add, publish content, enable and disable.

    # add a new course (writes courses/<id>/course.json and copies content in)
    python scripts/publish_course.py --course physical_design --title "Physical Design" \\
        --questions candidate.json --glossary glossary_candidate.json --add

    # publish new content for an existing course
    python scripts/publish_course.py --course physical_design --questions candidate.json

    # publish only the glossary (never affects the learner generation)
    python scripts/publish_course.py --course physical_design --glossary new_glossary.json

    # disable / re-enable a course (manifest is the source of truth)
    python scripts/publish_course.py --course physical_design --disable
    python scripts/publish_course.py --course physical_design --enable

Content publication uses the same frozen-bytes, versioned, atomic-manifest
machinery as ``swap_question_bank.py``: the candidate is read once, validated,
archived under ``versions/<sha256>/`` and switched over with a single rename.
Nothing here restarts a worker or bumps a generation.

Exit codes: ``0`` success, ``1`` validation/publish refused, ``2`` usage.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.models import CourseDefinitionError  # noqa: E402
from scripts.course_tooling import (  # noqa: E402
    ToolingError,
    build_loader,
    freeze_candidate,
    preflight_baseline,
    publish_glossary,
    publish_questions,
    resolve_definition,
    validate_bytes,
    validate_glossary_bytes,
    write_file_atomically,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--course", required=True, help="target course_id")
    parser.add_argument("--questions", type=Path, default=None)
    parser.add_argument("--glossary", type=Path, default=None)
    parser.add_argument("--add", action="store_true", help="create the course")
    parser.add_argument("--title", default=None)
    parser.add_argument("--title-zh", default="")
    parser.add_argument("--order", type=int, default=0)
    parser.add_argument("--enable", action="store_true")
    parser.add_argument("--disable", action="store_true")
    parser.add_argument(
        "--run-preflight",
        action="store_true",
        help="also diff against --db before publishing questions",
    )
    parser.add_argument(
        "--db", default=PROJECT_ROOT / "instance" / "mcq.db", type=Path
    )
    parser.add_argument(
        "--courses-dir", default=PROJECT_ROOT / "courses", type=Path
    )
    parser.add_argument(
        "--question-file", default=PROJECT_ROOT / "questions.json", type=Path
    )
    parser.add_argument(
        "--glossary-file", default=PROJECT_ROOT / "glossary.json", type=Path
    )
    return parser


def _add_course(args: argparse.Namespace) -> int:
    """Create ``courses/<id>/`` with a manifest and the supplied content."""
    from app.models import validate_course_id
    from app.models.course import CourseIdError

    try:
        course_id = validate_course_id(args.course)
    except CourseIdError as exc:
        print(f"--course 不合法：{exc}", file=sys.stderr)
        return 2
    target = (args.courses_dir / course_id).resolve()
    manifest_path = target / "course.json"
    if manifest_path.exists():
        print(f"课程已存在：{manifest_path}", file=sys.stderr)
        return 1
    if args.questions is None:
        print("--add 需要 --questions（新课程必须携带题库）", file=sys.stderr)
        return 2
    payload, digest = freeze_candidate(args.questions.resolve())
    questions = validate_bytes(payload)
    target.mkdir(parents=True, exist_ok=True)
    write_file_atomically(target / "questions.json", payload)
    glossary_name: str | None = None
    if args.glossary is not None:
        glossary_payload, _ = freeze_candidate(args.glossary.resolve())
        validate_glossary_bytes(glossary_payload)
        write_file_atomically(target / "glossary.json", glossary_payload)
        glossary_name = "glossary.json"
    manifest = {
        "schema_version": 1,
        "course_id": course_id,
        "title": args.title or course_id,
        "title_zh": args.title_zh,
        "enabled": not args.disable,
        "questions": "questions.json",
        "glossary": glossary_name,
        "order": args.order,
    }
    write_file_atomically(
        manifest_path,
        json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8") + b"\n",
    )
    print(f"已新增课程 {course_id}：{len(questions)} 道题，manifest={manifest_path}")
    print(
        "状态：created, pending worker activation。请在 worker 启动日志中确认 "
        f'"{course_id}" ready，然后用 /ready/{course_id} 验证。'
    )
    print(f"（内容指纹 sha256={digest[:16]}…）")
    return 0


def _set_enabled(args: argparse.Namespace, definition, enabled: bool) -> int:
    """Flip ``enabled`` in the manifest, which is the source of truth."""
    from scripts.course_tooling import read_manifest

    if definition.manifest_path is None:
        print(
            "legacy 根目录布局没有 manifest，无法单独停用；请改用 "
            "scripts/migrate_courses.py --layout 迁移布局。",
            file=sys.stderr,
        )
        return 1
    manifest = read_manifest(definition)
    manifest["enabled"] = enabled
    write_file_atomically(
        definition.manifest_path,
        json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8") + b"\n",
    )
    action = "启用" if enabled else "停用"
    print(f"已{action}课程 {definition.course_id}（{definition.manifest_path}）")
    print(
        f"状态：{'enabled' if enabled else 'disabled'}, pending worker activation。"
        "学习数据不会被修改。"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.enable and args.disable:
        print("--enable 与 --disable 不能同时使用。", file=sys.stderr)
        return 2
    if args.add and (args.enable or args.disable):
        print("--add 已由 --disable 决定初始状态，不能同时指定。", file=sys.stderr)
        return 2
    if args.add:
        return _add_course(args)

    loader = build_loader(args.courses_dir, args.question_file, args.glossary_file)
    try:
        definition = resolve_definition(loader, args.course)
    except (ToolingError, CourseDefinitionError) as exc:
        print(f"无法解析课程：\n{exc}", file=sys.stderr)
        return 1
    print(f"课程 (course_id): {definition.course_id} [{definition.layout}]")

    if args.enable or args.disable:
        return _set_enabled(args, definition, bool(args.enable))

    if args.questions is None and args.glossary is None:
        print(
            "未指定 --questions/--glossary：没有需要发布的内容。"
            "（--add 用于新增课程，--enable/--disable 用于切换状态）",
            file=sys.stderr,
        )
        return 2

    published_any = False
    if args.questions is not None:
        try:
            payload, digest = freeze_candidate(args.questions.resolve())
            questions = validate_bytes(payload)
        except (ToolingError, OSError) as exc:
            print(f"候选题库校验失败，未替换任何文件：\n{exc}", file=sys.stderr)
            return 1
        if args.run_preflight:
            from scripts.check_question_bank import main as check_main

            exit_code = check_main(
                [
                    str(args.questions),
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
                    f"\n预检返回 {exit_code}：未发布任何内容。", file=sys.stderr
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
        published_any = True
        print(f"题库已发布：{len(questions)} 道题 -> {published}")
        if legacy_layout:
            print("布局：legacy 单文件原子替换（没有 manifest）")

    if args.glossary is not None:
        try:
            payload, digest = freeze_candidate(args.glossary.resolve())
            glossary = validate_glossary_bytes(payload)
        except (ToolingError, OSError) as exc:
            print(f"候选术语表校验失败，未替换任何文件：\n{exc}", file=sys.stderr)
            return 1
        try:
            published = publish_glossary(definition, payload, digest)
        except (ToolingError, OSError) as exc:
            print(f"\n发布失败：\n{exc}", file=sys.stderr)
            return 1
        published_any = True
        print(f"术语表已发布：{len(glossary.terms)} 条 -> {published}")
        print("术语表不参与题库 generation：学习数据与 worker 围栏都不受影响。")

    if not published_any:  # pragma: no cover - guarded above
        return 2
    print(
        "\n状态：published, pending worker activation（已发布，等待 worker 激活）。"
        "\n文件系统发布与数据库激活不是同一个事务：请统一重启全部应用工作进程。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

