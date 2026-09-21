"""Contract tests: source comments must describe the behaviour they document.

Two classes of documentation drift are guarded here, because both are cheap to
get wrong and expensive to notice:

* an exit-code table in a module docstring that no longer matches the codes the
  command actually returns (``publish_course.py`` forwards a failing preflight's
  own code, so its real range is wider than a plain ``0``/``1``/``2``);
* a directory-layout diagram in a module docstring that no longer matches the
  files the loader resolves (the content-addressed ``versions/<sha256>/`` layout
  replaced the old "``questions.json`` next to the manifest" one).

The tests compare the *documented* text against behaviour and against real
artifacts on disk (built in a temporary directory by the real tooling), never
against a hand-written expectation of what the tree "should" look like.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import check_question_bank, publish_course
from scripts.publish_course import build_parser
from tests.conftest import course_bank, write_json


def _common(courses_dir: Path, database: Path) -> list[str]:
    """The course/database arguments every CLI call in this module passes."""
    return [
        "--courses-dir",
        str(courses_dir),
        "--db",
        str(database),
        "--question-file",
        str(courses_dir.parent / "absent" / "questions.json"),
        "--glossary-file",
        str(courses_dir.parent / "absent" / "glossary.json"),
    ]


# --------------------------------------------------------------- publish_course


def test_publish_course_exit_codes_match_the_check_script_they_forward() -> None:
    """The documented range must equal the range the preflight can hand back."""
    assert publish_course.EXIT_OK == check_question_bank.EXIT_OK
    assert publish_course.EXIT_REFUSED != publish_course.EXIT_OK
    assert (
        publish_course.EXIT_USAGE_OR_BLOCKING_PREFLIGHT
        == check_question_bank.EXIT_BLOCKING
    )
    assert publish_course.EXIT_PREFLIGHT_STRICT == check_question_bank.EXIT_STRICT
    assert (
        publish_course.EXIT_PREFLIGHT_UNRESOLVABLE
        == check_question_bank.EXIT_UNRESOLVABLE
    )


def test_publish_course_docstring_and_help_list_every_exit_code() -> None:
    """Every code the command can return is spelled out in docstring *and* --help."""
    docstring = publish_course.__doc__ or ""
    assert "Exit codes" in docstring
    epilog = build_parser().epilog or ""
    for code in sorted(publish_course.EXIT_CODES):
        # The docstring table is prose ("``0`` ..."), so only the digit is
        # asserted there; --help prints EXIT_CODE_SUMMARY verbatim.
        assert f"``{code}``" in docstring, f"docstring is missing exit code {code}"
        assert f"{code}:" in epilog, f"--help is missing exit code {code}"
    # The single-source-of-truth string is what --help shows.
    assert publish_course.EXIT_CODE_SUMMARY in epilog


def test_publish_course_forwards_each_preflight_code_unchanged(
    tmp_path, monkeypatch, capsys
):
    """A failing preflight's own 2/3/4 is returned as-is, not remapped to 1/2."""
    courses_dir = tmp_path / "courses"
    database = tmp_path / "mcq.db"
    candidate = courses_dir / "course_a" / "questions_candidate.json"
    candidate.parent.mkdir(parents=True)
    write_json(candidate, course_bank())
    assert (
        publish_course.main(
            [
                "--course",
                "course_a",
                "--add",
                "--title",
                "A",
                *_common(courses_dir, database),
            ]
        )
        == 0
    )
    capsys.readouterr()

    forwarded: list[int] = []
    monkeypatch.setattr(check_question_bank, "main", lambda argv: forwarded.pop(0))

    for code in (
        check_question_bank.EXIT_BLOCKING,
        check_question_bank.EXIT_STRICT,
        check_question_bank.EXIT_UNRESOLVABLE,
    ):
        forwarded.append(code)
        assert (
            publish_course.main(
                [
                    "--course",
                    "course_a",
                    "--questions",
                    str(candidate),
                    *_common(courses_dir, database),
                ]
            )
            == code
        )
        assert f"预检返回 {code}" in capsys.readouterr().err

    # A successful preflight publishes and returns 0.
    forwarded.append(check_question_bank.EXIT_OK)
    assert (
        publish_course.main(
            [
                "--course",
                "course_a",
                "--questions",
                str(candidate),
                *_common(courses_dir, database),
            ]
        )
        == publish_course.EXIT_OK
    )
    capsys.readouterr()


def test_publish_course_usage_errors_stay_two(tmp_path) -> None:
    """The command's own usage refusals keep their pre-existing code."""
    courses_dir = tmp_path / "courses"
    database = tmp_path / "mcq.db"
    candidate = courses_dir / "course_a" / "questions_candidate.json"
    candidate.parent.mkdir(parents=True)
    write_json(candidate, course_bank())
    assert (
        publish_course.main(
            [
                "--course",
                "course_a",
                "--add",
                "--title",
                "A",
                *_common(courses_dir, database),
            ]
        )
        == 0
    )

    assert (
        publish_course.main(
            ["--course", "course_a", "--prune", "--no-prune", *_common(courses_dir, database)]
        )
        == publish_course.EXIT_USAGE_OR_BLOCKING_PREFLIGHT
    )
    assert (
        publish_course.main(
            ["--course", "course_a", "--keep-versions", "0", *_common(courses_dir, database)]
        )
        == publish_course.EXIT_USAGE_OR_BLOCKING_PREFLIGHT
    )


# --------------------------------------------------------------- course_loader


@pytest.fixture
def published_course(tmp_path):
    """A course built by the real publish tooling, in the content-addressed layout."""
    courses_dir = tmp_path / "courses"
    database = tmp_path / "mcq.db"
    root = courses_dir / "physical_design"
    root.mkdir(parents=True)
    write_json(root / "questions_candidate.json", course_bank())
    assert (
        publish_course.main(
            [
                "--course",
                "physical_design",
                "--add",
                "--title",
                "Physical Design",
                *_common(courses_dir, database),
            ]
        )
        == 0
    )
    return root


def test_course_loader_docstring_describes_the_real_disk_layout(published_course):
    """The layout diagram must match what the tooling writes and the loader reads."""
    import app.repositories.course_loader as module
    from app.repositories import CourseLoader

    digest_directories = sorted(
        (published_course / "versions").iterdir(), key=lambda item: item.name
    )
    assert digest_directories, "the publish tooling must archive under versions/"
    assert all(len(item.name) == 64 for item in digest_directories)
    assert any((item / "questions.json").is_file() for item in digest_directories)
    # The manifest is the only pointer, and it points into versions/.
    manifest = json.loads(
        (published_course / "course.json").read_text(encoding="utf-8")
    )
    assert manifest["questions"].startswith("versions/")
    # The loader resolves exactly that path.
    definition = CourseLoader(published_course.parent).find_definition(
        "physical_design"
    )
    assert definition is not None
    assert definition.questions_path == (
        published_course / manifest["questions"]
    ).resolve()

    docstring = module.__doc__ or ""
    for token in (
        "course.json",
        "questions_candidate.json",
        "glossary_candidate.json",
        "versions/<sha256>/",
        ".publish.lock",
        "questions.json",
    ):
        assert token in docstring, f"layout diagram is missing {token!r}"
    # The stale diagram showed a published questions.json sitting next to the
    # manifest with no versions/ directory anywhere.
    assert "│   ├── questions.json\n" not in docstring
    assert "versions/" in docstring


def test_course_loader_docstring_tokens_exist_as_real_code_names(published_course):
    """Every fixed name the diagram shows is a name the code actually uses."""
    del published_course  # the diagram is compared against code constants only
    import app.repositories.course_loader as module
    from scripts.course_tooling import (
        GLOSSARY_CANDIDATE_NAME,
        LOCK_FILE_NAME,
        QUESTIONS_CANDIDATE_NAME,
        VERSIONS_DIRECTORY,
    )

    assert module.MANIFEST_NAME == "course.json"
    assert module.DEFAULT_QUESTIONS_NAME == "questions.json"
    assert QUESTIONS_CANDIDATE_NAME == "questions_candidate.json"
    assert GLOSSARY_CANDIDATE_NAME == "glossary_candidate.json"
    assert LOCK_FILE_NAME == ".publish.lock"
    assert VERSIONS_DIRECTORY == "versions"
    docstring = module.__doc__ or ""
    for name in (
        module.MANIFEST_NAME,
        module.DEFAULT_QUESTIONS_NAME,
        QUESTIONS_CANDIDATE_NAME,
        GLOSSARY_CANDIDATE_NAME,
        LOCK_FILE_NAME,
        f"{VERSIONS_DIRECTORY}/<sha256>/",
    ):
        assert name in docstring
