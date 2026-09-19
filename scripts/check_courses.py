"""Validate and list the course catalogue declared under ``courses/``.

    python scripts/check_courses.py
    python scripts/check_courses.py --json

This is the course-level gate of the read-only ``check_<subject>.py`` set
(with ``check_glossary.py`` and ``check_question_bank.py``): it validates every
manifest (schema version, ``course_id`` slug, required paths, path containment,
declared glossary) and loads each enabled course's question bank *and* glossary
through the application's own loader.  A *global* catalogue ambiguity (duplicate
``course_id``, invalid manifest) exits ``2``; a single broken course package
exits ``1`` but still reports the other courses.

Nothing is written and no database is touched.  Run it after changing any course
content and before publishing; never publish while it reports a failure.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.models import CourseDefinitionError, CourseLoadError  # noqa: E402
from scripts.course_tooling import build_loader  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
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
        "--json",
        action="store_true",
        help="emit machine-readable JSON instead of a human report",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    loader = build_loader(args.courses_dir, args.question_file, args.glossary_file)
    try:
        definitions = loader.discover_definitions()
    except CourseDefinitionError as exc:
        print(f"课程目录配置错误（全局，应用无法启动）：\n{exc}", file=sys.stderr)
        return 2

    report: list[dict] = []
    failures = 0
    for definition in definitions:
        entry = {
            "course_id": definition.course_id,
            "layout": definition.layout,
            "enabled": definition.course.enabled,
            "order": definition.course.order,
            "title": definition.course.title,
            "title_zh": definition.course.title_zh,
            "questions": str(definition.questions_path),
            "glossary": (
                str(definition.glossary_path)
                if definition.glossary_path is not None
                else None
            ),
            "status": "ok",
            "reason": "",
            "question_count": 0,
            "glossary_terms": None,
        }
        if not definition.course.enabled:
            entry["status"] = "disabled"
        else:
            try:
                bundle = loader.load_bundle(definition)
            except CourseLoadError as exc:
                entry["status"] = "unavailable"
                entry["reason"] = exc.reason
                failures += 1
            else:
                entry["question_count"] = len(bundle.questions)
                glossary = bundle.glossary()
                entry["glossary_terms"] = None if glossary is None else len(glossary.terms)
        report.append(entry)

    if args.json:
        print(json.dumps({"courses": report}, ensure_ascii=False, indent=2))
    else:
        print(f"课程数量：{len(report)}")
        for entry in report:
            print(
                f"\n[{entry['course_id']}] {entry['title_zh'] or entry['title']} "
                f"({entry['layout']}, order={entry['order']}, "
                f"enabled={'yes' if entry['enabled'] else 'no'})"
            )
            print(f"  questions: {entry['questions']}")
            print(f"  glossary : {entry['glossary'] or '(none declared)'}")
            if entry["status"] == "ok":
                terms = entry["glossary_terms"]
                print(
                    f"  状态：ok（{entry['question_count']} 道题，"
                    f"术语 {terms if terms is not None else 'n/a'} 条）"
                )
            elif entry["status"] == "disabled":
                print("  状态：disabled（enabled=false，不会加载也不会同步）")
            else:
                print(f"  状态：{entry['status']} — {entry['reason']}")
        if failures:
            print(
                f"\n{failures} 门课程无法加载：它们会被运行时标记为 unavailable，"
                "其他课程仍可服务。",
                file=sys.stderr,
            )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
