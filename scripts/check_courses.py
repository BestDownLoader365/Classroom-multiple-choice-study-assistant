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

It also reports — informationally, without changing the exit code — content files
that no manifest path points at.  A course created before the content-addressed
layout (or assembled by hand) keeps a plain ``questions.json``/``glossary.json``
next to its manifest; once the manifest points at ``versions/<sha256>/`` those
files are read by nothing (the loader resolves only the declared paths and every
gate follows the manifest), so a stale copy would otherwise sit there unnoticed
while looking exactly like the live one.

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
from app.repositories.course_loader import (  # noqa: E402
    DEFAULT_GLOSSARY_NAME,
    DEFAULT_QUESTIONS_NAME,
)
from scripts.course_tooling import build_loader  # noqa: E402


def _unreferenced_copies(definition) -> list[str]:
    """Return content files in the course directory no manifest path points at.

    The plain-file layout declares ``questions.json``/``glossary.json`` itself, so
    it has nothing to report.  A course-addressed course (manifest pointing at
    ``versions/<sha256>/``) may still carry such a file next to the manifest —
    usually the initial copy of a course created before that layout, kept around
    as rollback material.  It is *not* read by any process, so it is reported for
    visibility only and never treated as a failure.
    """
    if definition.manifest_path is None:  # legacy adapter: the root files are declared
        return []
    declared = {definition.questions_path.resolve()}
    if definition.glossary_path is not None:
        declared.add(definition.glossary_path.resolve())
    root = definition.root
    return [
        str(root / name)
        for name in (DEFAULT_QUESTIONS_NAME, DEFAULT_GLOSSARY_NAME)
        if (root / name).is_file() and (root / name).resolve() not in declared
    ]


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
            "unreferenced_copies": _unreferenced_copies(definition),
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
            for copy in entry["unreferenced_copies"]:
                print(
                    f"  提示：{copy} 未被 manifest 引用 —— 任何进程都不会读取它，"
                    "编辑它不会生效（仅作历史/回滚副本，可删除）"
                )
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
