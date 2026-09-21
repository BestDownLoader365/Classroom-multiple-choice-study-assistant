"""``migrate_courses.py --layout`` must agree with ``--legacy-course-id``.

The layout step used to hard-code ``legacy`` for the directory name and the
manifest ``course_id`` while the database step honoured the flag, so a custom
namespace produced a tree the schema did not describe.  These tests pin the
whole chain: directory name, manifest, ``schema_meta`` and the loader's
root-file adapter.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from app.models import LEGACY_COURSE_ID
from app.repositories.schema_migrations import LEGACY_COURSE_KEY, read_meta
from scripts.migrate_courses import main as migrate_main
from tests.conftest import course_bank, write_json


def _legacy_root(tmp_path):
    """A pre-multi-course deployment: root files, an empty courses dir, a v1 db."""
    from tests.test_course_migration import build_legacy_database

    root = tmp_path / "deployment"
    root.mkdir()
    questions = root / "questions.json"
    glossary = root / "glossary.json"
    write_json(questions, course_bank())
    write_json(
        glossary,
        {
            "schema_version": 1,
            "title": "Legacy Glossary",
            "title_zh": "旧版术语表",
            "terms": [
                {"id": "alpha", "term": "Alpha", "term_zh": "阿尔法"},
            ],
        },
    )
    courses_dir = root / "courses"
    courses_dir.mkdir()
    database = build_legacy_database(root / "mcq.db")
    return courses_dir, questions, glossary, database


def _layout_args(courses_dir, questions, glossary, database, *extra: str) -> list[str]:
    return [
        "--db",
        str(database),
        "--courses-dir",
        str(courses_dir),
        "--question-file",
        str(questions),
        "--glossary-file",
        str(glossary),
        *extra,
    ]


def _stored_legacy_id(database) -> str | None:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        return read_meta(connection, LEGACY_COURSE_KEY)
    finally:
        connection.close()


def test_layout_uses_the_requested_legacy_course_id(tmp_path, capsys):
    courses_dir, questions, glossary, database = _legacy_root(tmp_path)

    assert (
        migrate_main(
            _layout_args(
                courses_dir,
                questions,
                glossary,
                database,
                "--layout",
                "--legacy-course-id",
                "custom-id",
            )
        )
        == 0
    )
    capsys.readouterr()

    target = courses_dir / "custom-id"
    assert (target / "course.json").is_file()
    assert (target / "questions.json").is_file()
    manifest = json.loads((target / "course.json").read_text(encoding="utf-8"))
    assert manifest["course_id"] == "custom-id"
    # No legacy-named directory was created behind our back.
    assert not (courses_dir / LEGACY_COURSE_ID).exists()
    assert _stored_legacy_id(database) == "custom-id"


def test_layout_without_the_flag_keeps_the_default_namespace(tmp_path, capsys):
    """No flag: exactly the historical behaviour."""
    courses_dir, questions, glossary, database = _legacy_root(tmp_path)

    assert (
        migrate_main(
            _layout_args(courses_dir, questions, glossary, database, "--layout")
        )
        == 0
    )
    capsys.readouterr()

    target = courses_dir / LEGACY_COURSE_ID
    manifest = json.loads((target / "course.json").read_text(encoding="utf-8"))
    assert manifest["course_id"] == LEGACY_COURSE_ID
    assert _stored_legacy_id(database) == LEGACY_COURSE_ID


@pytest.mark.parametrize(
    "bad_id",
    ["BAD!", "../evil", "a/b", "", "UPPER", "has space", "x" * 65],
)
def test_illegal_legacy_course_id_is_refused_before_any_write(tmp_path, bad_id, capsys):
    """An invalid id must never reach the database, a path or a manifest."""
    courses_dir, questions, glossary, database = _legacy_root(tmp_path)
    before = database.read_bytes()

    assert (
        migrate_main(
            _layout_args(
                courses_dir,
                questions,
                glossary,
                database,
                "--layout",
                "--legacy-course-id",
                bad_id,
            )
        )
        == 2
    )
    err = capsys.readouterr().err
    assert "--legacy-course-id" in err
    # Nothing was written anywhere: no directory, no manifest, no backup, and the
    # database is byte-identical.
    assert list(courses_dir.iterdir()) == []
    assert database.read_bytes() == before
    assert not list(tmp_path.rglob("*.bak-*"))


def test_layout_refuses_when_the_target_directory_is_not_empty(tmp_path, capsys):
    """A pre-existing directory is never overwritten."""
    courses_dir, questions, glossary, database = _legacy_root(tmp_path)
    existing = courses_dir / "custom-id"
    existing.mkdir()
    stray = existing / "leftover.json"
    write_json(stray, {"keep": "me"})

    assert (
        migrate_main(
            _layout_args(
                courses_dir,
                questions,
                glossary,
                database,
                "--layout",
                "--legacy-course-id",
                "custom-id",
            )
        )
        == 1
    )
    capsys.readouterr()
    # The pre-existing directory was not overwritten or removed.
    assert json.loads(stray.read_text(encoding="utf-8")) == {"keep": "me"}
    assert not (existing / "course.json").exists()


def test_layout_refuses_a_traversing_id_without_touching_the_parent(tmp_path, capsys):
    """``../evil`` must not create anything outside ``--courses-dir``."""
    courses_dir, questions, glossary, database = _legacy_root(tmp_path)
    outside = courses_dir.parent / "evil"

    assert (
        migrate_main(
            _layout_args(
                courses_dir,
                questions,
                glossary,
                database,
                "--layout",
                "--legacy-course-id",
                "../evil",
            )
        )
        == 2
    )
    capsys.readouterr()
    assert not outside.exists()


def test_layout_refuses_a_missing_root_bank(tmp_path, capsys):
    courses_dir, questions, glossary, database = _legacy_root(tmp_path)
    questions.unlink()

    assert (
        migrate_main(
            _layout_args(courses_dir, questions, glossary, database, "--layout")
        )
        == 1
    )
    capsys.readouterr()
    assert list(courses_dir.iterdir()) == []


def test_layout_is_not_repeatable_on_a_populated_target(tmp_path, capsys):
    """Second run refuses instead of rewriting the first result.

    The documented next step after a successful ``--layout`` is deleting the root
    files, so that is what the realistic second run looks like.
    """
    courses_dir, questions, glossary, database = _legacy_root(tmp_path)
    args = _layout_args(
        courses_dir,
        questions,
        glossary,
        database,
        "--layout",
        "--legacy-course-id",
        "custom-id",
    )
    assert migrate_main(args) == 0
    capsys.readouterr()
    manifest = (courses_dir / "custom-id" / "course.json").read_bytes()

    # The root files were removed as documented; the populated target is refused.
    questions.unlink()
    glossary.unlink()
    assert migrate_main(args) == 1
    capsys.readouterr()
    assert (courses_dir / "custom-id" / "course.json").read_bytes() == manifest


def test_root_files_next_to_the_layout_are_a_loud_duplicate(tmp_path, capsys):
    """Keeping the root bank after ``--layout`` is reported, never guessed at.

    This is the tightened behaviour agreed for this change: the root-file adapter
    claims the persisted namespace, so the root bank and ``courses/<id>/`` really
    are the same ``course_id`` and the catalogue must fail loudly instead of
    silently serving a second, history-less course.
    """
    courses_dir, questions, glossary, database = _legacy_root(tmp_path)
    args = _layout_args(
        courses_dir,
        questions,
        glossary,
        database,
        "--layout",
        "--legacy-course-id",
        "custom-id",
    )
    assert migrate_main(args) == 0
    capsys.readouterr()

    # Root files still present -> duplicate course_id -> reported, exit 1.
    assert migrate_main(args) == 1
    err = capsys.readouterr().err
    assert "Duplicate course_id" in err
    assert "根 questions.json" in err


def test_layout_custom_id_loader_and_registry_agree(tmp_path, capsys):
    """The root-file adapter claims the persisted id, so the tree loads as one course."""
    from app import create_app
    from app.repositories import CourseLoader

    courses_dir, questions, glossary, database = _legacy_root(tmp_path)
    assert (
        migrate_main(
            _layout_args(
                courses_dir,
                questions,
                glossary,
                database,
                "--layout",
                "--legacy-course-id",
                "custom-id",
            )
        )
        == 0
    )
    capsys.readouterr()

    loader = CourseLoader(
        courses_dir,
        legacy_directory=questions.parent,
        legacy_glossary_name=glossary.name,
        legacy_course_id="custom-id",
    )
    # A custom id plus the still-present root bank is a real duplicate, not a
    # silent second course: the catalogue must refuse to guess.
    with pytest.raises(Exception) as excinfo:
        loader.discover_definitions()
    assert "custom-id" in str(excinfo.value)

    # Removing the root files (the documented next step) leaves exactly one course.
    questions.unlink()
    glossary.unlink()
    definitions = loader.discover_definitions()
    assert [item.course_id for item in definitions] == ["custom-id"]

    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "COURSES_DIR": courses_dir,
            "QUESTION_FILE": questions,
            "GLOSSARY_FILE": glossary,
            "DATABASE": database,
        }
    )
    registry = app.extensions["mcq_services"].course_registry
    assert registry.has("custom-id") is True
    assert registry.has(LEGACY_COURSE_ID) is False
