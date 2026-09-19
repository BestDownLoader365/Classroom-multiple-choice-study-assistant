#!/usr/bin/env python3
"""Check a glossary: validate it and report its coverage of a question-bank corpus.

One ``check_<subject>.py`` script guards every kind of course content, and all of
them are read-only::

    python scripts/check_courses.py        # manifests, paths, every course loads
    python scripts/check_glossary.py       # glossary schema plus corpus coverage
    python scripts/check_question_bank.py  # question bank schema plus registry diff

The glossary gate is run per course against that course's own bank::

    python scripts/check_glossary.py --course eek5106
    python scripts/check_glossary.py --all
    python scripts/check_glossary.py --questions path/questions.json --glossary path/glossary.json

``--questions``/``--glossary`` run fully offline: no catalogue, no database and no
registry writes.  Every other mode resolves the course(s) through the
application's own loader, so the same validation the workers run is reused here.

Nothing is published by this script.  Run it *before* publishing; the publish
commands (``publish_course.py``, ``swap_question_bank.py``) refuse to switch over
content that does not pass its check.

Exit codes: ``0`` the glossary is valid (orphan/candidate reports are advisory),
``1`` the glossary (or its corpus) fails to load or fails validation.
"""

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.repositories import GlossaryError, GlossaryLoader  # noqa: E402


def corpus_strings(payload: dict[str, Any]) -> Iterable[str]:
    """Yield only learner-facing English course content."""
    for source in payload.get("sources", []):
        if isinstance(source, dict):
            yield source.get("title", "")
    for chapter in payload.get("chapters", []):
        if isinstance(chapter, dict):
            yield chapter.get("title", "")
    for question in payload.get("questions", []):
        if not isinstance(question, dict):
            continue
        yield question.get("section", "")
        yield question.get("text", "")
        explanation = question.get("explanation", "")
        if isinstance(explanation, str):
            explanation = re.sub(r"\s+Source:\s.*$", "", explanation)
        yield explanation
        for option in question.get("options", []):
            if isinstance(option, dict):
                yield option.get("text", "")


def literal_occurs(label: str, corpus: str) -> bool:
    """Apply the same edge-aware literal rule used by the browser matcher."""
    pattern = re.compile(re.escape(label), re.IGNORECASE)
    for match in pattern.finditer(corpus):
        before = corpus[match.start() - 1] if match.start() else ""
        after = corpus[match.end()] if match.end() < len(corpus) else ""
        left_word = label[0].isalnum() or label[0] == "_"
        right_word = label[-1].isalnum() or label[-1] == "_"
        if left_word and before and (before.isalnum() or before == "_"):
            continue
        if right_word and after and (after.isalnum() or after == "_"):
            continue
        return True
    return False


def candidates(corpus: str) -> list[tuple[str, int]]:
    """Find generic, review-only vocabulary candidates; never modify data."""
    found: Counter[str] = Counter()
    found.update(
        re.findall(
            r"(?<![\w])(?:[A-Z][A-Z0-9/+.-]{0,10}[A-Z0-9+])(?![\w])",
            corpus,
        )
    )
    found.update(
        acronym
        for _, acronym in re.findall(
            r"([A-Za-z][A-Za-z0-9 /+&-]{2,80})\s*\(([A-Z][A-Z0-9/+.-]{1,11})\)",
            corpus,
        )
    )
    found.update(re.findall(r"(?<![\w])(?:[A-Za-z0-9]+-){1,3}[A-Za-z0-9]+(?![\w])", corpus))
    ignored = {"I", "II", "III", "I.", "II.", "III."}
    return sorted(
        ((label, count) for label, count in found.items() if label not in ignored),
        key=lambda item: (-item[1], item[0].casefold()),
    )


def check_one(questions_path: Path, glossary_path: Path) -> int:
    """Check one (questions, glossary) pair and print its report."""
    try:
        glossary = GlossaryLoader(glossary_path.resolve()).load()
        question_payload = json.loads(questions_path.read_text(encoding="utf-8"))
    except (GlossaryError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if not isinstance(question_payload, dict):
        print("ERROR: question bank root must be an object.", file=sys.stderr)
        return 1

    corpus = "\n".join(value for value in corpus_strings(question_payload) if isinstance(value, str))
    known_labels = [label for term in glossary.terms for label in (term.term, *term.aliases)]
    orphaned = [
        term.id
        for term in glossary.terms
        if not any(literal_occurs(label, corpus) for label in (term.term, *term.aliases))
    ]
    uncovered = [
        (label, count)
        for label, count in candidates(corpus)
        if count >= 2 and not any(
            literal_occurs(label, known) or literal_occurs(known, label)
            for known in known_labels
        )
    ]
    alias_count = sum(len(term.aliases) for term in glossary.terms)
    category_count = len(dict.fromkeys(term.category for term in glossary.terms if term.category))
    print(
        f"Validated {len(glossary.terms)} canonical terms, {alias_count} aliases, "
        f"and {category_count} categories."
    )
    if orphaned:
        print("Orphan entries (no canonical term or alias found in corpus):")
        for term_id in orphaned:
            print(f"  - {term_id}")
    else:
        print("Orphan entries: none")
    if uncovered:
        print("Possible uncovered glossary candidates (manual review only):")
        for label, count in uncovered[:40]:
            print(f"  - {label} ({count})")
    else:
        print("Possible uncovered glossary candidates: none")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--course",
        default=None,
        help="course_id whose glossary should be checked against its own bank",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="check every declared, enabled course",
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
        help="root questions.json for the legacy adapter or explicit offline mode",
    )
    parser.add_argument(
        "--questions",
        type=Path,
        default=None,
        help="explicit offline question bank (bypasses the course catalogue)",
    )
    parser.add_argument(
        "--glossary",
        type=Path,
        default=None,
        help="explicit offline glossary (bypasses the course catalogue)",
    )
    args = parser.parse_args(argv)

    from scripts.course_tooling import (
        ToolingError,
        build_loader,
        resolve_definition,
    )

    if args.questions is not None or args.glossary is not None:
        # Explicit offline mode: no catalogue, no database, no registry writes.
        questions = (args.questions or args.question_file).resolve()
        glossary = (args.glossary or PROJECT_ROOT / "glossary.json").resolve()
        print(f"offline: {questions} + {glossary}")
        return check_one(questions, glossary)

    loader = build_loader(args.courses_dir, args.question_file, args.question_file.parent / "glossary.json")
    try:
        if args.all:
            definitions = loader.enabled_definitions()
        else:
            definitions = [resolve_definition(loader, args.course)]
    except ToolingError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if not definitions:
        print("ERROR: no enabled course is declared.", file=sys.stderr)
        return 1

    exit_code = 0
    for definition in definitions:
        print(f"\n=== {definition.course_id} ===")
        if definition.glossary_path is None:
            print("该课程没有配置术语表（glossary: null），跳过校验。")
            continue
        exit_code = max(
            exit_code, check_one(definition.questions_path, definition.glossary_path)
        )
    print(
        "\n校验只读取内容，不修改 question_registry、generation 或任何学习数据。"
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

