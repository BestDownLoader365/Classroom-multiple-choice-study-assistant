"""CLI tooling: read-only preflight, atomic publish, bytes frozen once."""

import hashlib
import json

import pytest

from scripts.check_courses import main as check_courses_main
from scripts.check_glossary import main as check_glossary_main
from scripts.check_question_bank import main as check_main
from scripts.course_tooling import (
    ToolingError,
    build_loader,
    freeze_candidate,
    preflight_baseline,
    publish_questions,
    resolve_definition,
)
from scripts.publish_course import main as publish_course_main
from tests.conftest import (
    course_bank,
    course_glossary,
    make_multi_app,
    write_course,
    write_json,
)

A = "course_a"
B = "course_b"


@pytest.fixture
def world(tmp_path):
    app = make_multi_app(
        tmp_path,
        {A: course_bank(("a", "alpha")), B: course_bank(("b", "beta"))},
    )
    courses_dir = tmp_path / "courses"
    database = tmp_path / "mcq.db"
    digest = hashlib.sha256(database.read_bytes()).hexdigest()
    return app, courses_dir, database, digest


def cli_common(courses_dir, database):
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


def _with_extra_question(option_id: str) -> dict:
    changed = course_bank((option_id, "alpha"))
    changed["questions"].append(
        {
            "id": "q003",
            "source_id": "source_1",
            "chapter_ids": ["chapter_1"],
            "text": "Third",
            "type": "single",
            "options": [{"id": "a", "text": "First"}, {"id": "b", "text": "Second"}],
            "correct_answers": ["a"],
            "explanation": "A.",
        }
    )
    return changed


def test_check_question_bank_uses_course_scope_and_is_read_only(
    world, tmp_path, capsys
):
    app, courses_dir, database, digest = world
    del app
    candidate = tmp_path / "candidate.json"
    write_json(candidate, _with_extra_question("a"))

    exit_code = check_main(
        [str(candidate), "--course", A, *cli_common(courses_dir, database)]
    )

    out = capsys.readouterr().out
    assert exit_code == 0
    assert f"课程 (course_id): {A}" in out
    assert "当前 generation：0" in out
    assert "generation would bump): yes" in out
    assert "(new): 1" in out
    assert "attempts 处理策略" in out
    # The live database is untouched.
    assert hashlib.sha256(database.read_bytes()).hexdigest() == digest


def test_check_question_bank_refuses_a_non_legacy_course_on_a_legacy_database(
    tmp_path, capsys
):
    """An unmigrated database can only be mapped to the legacy namespace."""
    import sqlite3

    database = tmp_path / "old.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        "CREATE TABLE question_registry ("
        "question_id TEXT PRIMARY KEY, status TEXT, question_type TEXT, "
        "option_ids TEXT, correct_answers TEXT, content_fingerprint TEXT, "
        "first_seen_at TEXT, last_seen_at TEXT, retired_at TEXT);"
    )
    connection.commit()
    connection.close()
    candidate = tmp_path / "candidate.json"
    write_json(candidate, course_bank())
    # The course must exist in the catalogue for the DB mapping check to run.
    write_course(tmp_path / "courses", "physical_design", course_bank())

    exit_code = check_main(
        [
            str(candidate),
            "--course",
            "physical_design",
            "--db",
            str(database),
            "--courses-dir",
            str(tmp_path / "courses"),
            "--question-file",
            str(tmp_path / "absent" / "questions.json"),
            "--glossary-file",
            str(tmp_path / "absent" / "glossary.json"),
        ]
    )

    assert exit_code == 4
    assert "has not been migrated" in capsys.readouterr().err


def test_publish_course_questions_is_immutable_and_switches_the_manifest(
    world, tmp_path
):
    app, courses_dir, database, _digest = world
    candidate = tmp_path / "candidate.json"
    changed = course_bank(("a", "alpha"))
    changed["questions"][0]["text"] = "Rewritten for alpha"
    write_json(candidate, changed)
    expected_bytes = candidate.read_bytes()
    expected_digest = hashlib.sha256(expected_bytes).hexdigest()

    exit_code = publish_course_main(
        ["--course", A, "--questions", str(candidate), *cli_common(courses_dir, database)]
    )

    assert exit_code == 0
    versioned = courses_dir / A / "versions" / expected_digest / "questions.json"
    assert versioned.read_bytes() == expected_bytes
    manifest = json.loads((courses_dir / A / "course.json").read_text())
    assert manifest["questions"] == f"versions/{expected_digest}/questions.json"
    # B's publication is untouched by an A publish.
    assert "versions" not in (courses_dir / B / "course.json").read_text()
    # The application's own loader reads the new publication.
    from app import create_app

    republished = create_app(dict(app.config))
    text = (
        republished.extensions["mcq_services"]
        .course(A)
        .question_repository.get_by_id("q001")
        .text
    )
    assert text == "Rewritten for alpha"


def test_publish_course_questions_preflight_blocks_unless_skipped(tmp_path):
    """``--questions`` is gated by ``check_question_bank.py``; --skip-preflight opts out."""
    import sqlite3

    courses_dir = tmp_path / "courses"
    write_course(courses_dir, A, course_bank())
    candidate = tmp_path / "candidate.json"
    write_json(candidate, course_bank())

    # An unmigrated database cannot be diffed against a non-legacy course.
    database = tmp_path / "old.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        "CREATE TABLE question_registry ("
        "question_id TEXT PRIMARY KEY, status TEXT, question_type TEXT, "
        "option_ids TEXT, correct_answers TEXT, content_fingerprint TEXT, "
        "first_seen_at TEXT, last_seen_at TEXT, retired_at TEXT);"
    )
    connection.commit()
    connection.close()

    common = [
        "--course",
        A,
        "--courses-dir",
        str(courses_dir),
        "--question-file",
        str(tmp_path / "absent" / "questions.json"),
        "--glossary-file",
        str(tmp_path / "absent" / "glossary.json"),
        "--db",
        str(database),
    ]
    before = (courses_dir / A / "course.json").read_bytes()

    assert publish_course_main(["--questions", str(candidate), *common]) == 4
    assert (courses_dir / A / "course.json").read_bytes() == before

    assert (
        publish_course_main(
            ["--questions", str(candidate), "--skip-preflight", *common]
        )
        == 0
    )
    assert b"versions" in (courses_dir / A / "course.json").read_bytes()


def test_failed_publish_leaves_the_publication_untouched(world, tmp_path):
    app, courses_dir, database, _digest = world
    del app
    before = (courses_dir / A / "course.json").read_bytes()
    broken = tmp_path / "broken.json"
    broken.write_text('{"questions": [}', encoding="utf-8")

    exit_code = publish_course_main(
        ["--course", A, "--questions", str(broken), *cli_common(courses_dir, database)]
    )

    assert exit_code == 1
    assert (courses_dir / A / "course.json").read_bytes() == before


def test_unknown_course_is_refused(world, tmp_path):
    app, courses_dir, database, _digest = world
    del app
    candidate = tmp_path / "candidate.json"
    write_json(candidate, course_bank())

    assert (
        publish_course_main(
            [
                "--course",
                "does_not_exist",
                "--questions",
                str(candidate),
                *cli_common(courses_dir, database),
            ]
        )
        == 1
    )


def test_baseline_revalidation_refuses_a_concurrent_publication(world):
    """A publication that moved under us is never silently overwritten."""
    app, courses_dir, _database, _digest = world
    del app
    loader = build_loader(
        courses_dir,
        courses_dir.parent / "absent" / "questions.json",
        courses_dir.parent / "absent" / "glossary.json",
    )
    definition = resolve_definition(loader, A)
    baseline = preflight_baseline(definition)
    payload, digest = freeze_candidate(courses_dir / A / "questions.json")

    # Another publisher wins the race.
    write_json(courses_dir / A / "questions.json", course_bank(("c", "gamma")))

    with pytest.raises(ToolingError, match="changed while this command was running"):
        publish_questions(definition, payload, digest, baseline=baseline)



def test_check_courses_reports_status_and_exits_nonzero_on_a_broken_course(
    tmp_path, capsys
):
    courses_dir = tmp_path / "courses"
    write_course(courses_dir, A, course_bank())
    write_course(courses_dir, B, course_bank(), enabled=False)
    common = [
        "--courses-dir",
        str(courses_dir),
        "--question-file",
        str(tmp_path / "absent" / "questions.json"),
        "--glossary-file",
        str(tmp_path / "absent" / "glossary.json"),
    ]

    exit_code = check_courses_main(common)
    out = capsys.readouterr().out
    assert exit_code == 0
    assert f"[{A}]" in out and f"[{B}]" in out
    assert "disabled（enabled=false" in out

    write_json(courses_dir / A / "questions.json", {"questions": "broken"})
    assert check_courses_main(common) == 1


def test_check_glossary_validates_and_reports_coverage(tmp_path, capsys):
    """``check_glossary.py`` validates a candidate pair and reports coverage."""
    questions = tmp_path / "questions.json"
    glossary = tmp_path / "glossary.json"
    write_json(questions, course_bank())
    write_json(glossary, course_glossary("alpha"))

    exit_code = check_glossary_main(
        ["--questions", str(questions), "--glossary", str(glossary)]
    )

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "Validated 2 canonical terms" in out
    # The terms never occur in this corpus, so they are reported as orphans -
    # advisory output that must not change the exit code.
    assert "alpha-term-1" in out


def test_check_glossary_rejects_an_invalid_glossary(tmp_path, capsys):
    questions = tmp_path / "questions.json"
    glossary = tmp_path / "glossary.json"
    write_json(questions, course_bank())
    payload = course_glossary("alpha")
    payload["terms"][0].pop("term_zh")
    write_json(glossary, payload)

    exit_code = check_glossary_main(
        ["--questions", str(questions), "--glossary", str(glossary)]
    )

    assert exit_code == 1
    assert "ERROR" in capsys.readouterr().err


def test_check_glossary_is_course_scoped_and_skips_glossary_less_courses(
    tmp_path, capsys
):
    courses_dir = tmp_path / "courses"
    write_course(courses_dir, A, course_bank(), glossary=course_glossary("alpha"))
    write_course(courses_dir, B, course_bank(("b", "beta")))

    exit_code = check_glossary_main(
        [
            "--all",
            "--courses-dir",
            str(courses_dir),
            "--question-file",
            str(tmp_path / "absent" / "questions.json"),
        ]
    )

    out = capsys.readouterr().out
    assert exit_code == 0
    assert f"=== {A} ===" in out
    assert f"=== {B} ===" in out
    assert "没有配置术语表" in out


def test_publish_course_runs_the_glossary_check_before_switching_over(
    tmp_path, capsys
):
    """A glossary publish is gated by ``check_glossary.py``."""
    courses_dir = tmp_path / "courses"
    write_course(courses_dir, A, course_bank())
    glossary = tmp_path / "glossary.json"
    write_json(glossary, course_glossary("alpha"))
    common = [
        "--courses-dir",
        str(courses_dir),
        "--question-file",
        str(tmp_path / "absent" / "questions.json"),
        "--glossary-file",
        str(tmp_path / "absent" / "glossary.json"),
    ]

    assert (
        publish_course_main(
            ["--course", A, "--glossary", str(glossary), *common]
        )
        == 0
    )
    digest = hashlib.sha256(glossary.read_bytes()).hexdigest()
    published = courses_dir / A / "versions" / digest / "glossary.json"
    manifest = json.loads((courses_dir / A / "course.json").read_text())
    assert published.is_file()
    assert manifest["glossary"] == f"versions/{digest}/glossary.json"

    # A glossary that fails the check is never published.
    before = (courses_dir / A / "course.json").read_bytes()
    broken = tmp_path / "broken_glossary.json"
    payload = course_glossary("alpha")
    payload["terms"][0]["aliases"] = "not-an-array"
    write_json(broken, payload)

    assert (
        publish_course_main(["--course", A, "--glossary", str(broken), *common]) == 1
    )
    assert (courses_dir / A / "course.json").read_bytes() == before
    capsys.readouterr()


def test_publish_course_add_validates_the_glossary_before_writing(tmp_path, capsys):
    """``--add`` with a glossary checks it before creating any file."""
    courses_dir = tmp_path / "courses"
    courses_dir.mkdir(parents=True)
    questions = tmp_path / "questions.json"
    glossary = tmp_path / "glossary.json"
    write_json(questions, course_bank())
    write_json(glossary, course_glossary("alpha"))
    common = [
        "--courses-dir",
        str(courses_dir),
        "--question-file",
        str(tmp_path / "absent" / "questions.json"),
        "--glossary-file",
        str(tmp_path / "absent" / "glossary.json"),
    ]

    assert (
        publish_course_main(
            [
                "--course",
                "physical_design",
                "--add",
                "--questions",
                str(questions),
                "--glossary",
                str(glossary),
                *common,
            ]
        )
        == 0
    )
    manifest = json.loads(
        (courses_dir / "physical_design" / "course.json").read_text()
    )
    assert manifest["glossary"] == "glossary.json"
    assert (courses_dir / "physical_design" / "glossary.json").is_file()

    # A failed check creates nothing at all, not even the course directory.
    bad_glossary = tmp_path / "bad_glossary.json"
    payload = course_glossary("alpha")
    payload["schema_version"] = 2
    write_json(bad_glossary, payload)
    assert (
        publish_course_main(
            [
                "--course",
                "another_course",
                "--add",
                "--questions",
                str(questions),
                "--glossary",
                str(bad_glossary),
                *common,
            ]
        )
        == 1
    )
    assert not (courses_dir / "another_course").exists()
    capsys.readouterr()


def test_publish_course_can_add_disable_and_reenable(tmp_path, capsys):
    courses_dir = tmp_path / "courses"
    courses_dir.mkdir(parents=True)
    candidate = tmp_path / "questions.json"
    write_json(candidate, course_bank())
    common = [
        "--courses-dir",
        str(courses_dir),
        "--question-file",
        str(tmp_path / "absent" / "questions.json"),
        "--glossary-file",
        str(tmp_path / "absent" / "glossary.json"),
    ]

    assert (
        publish_course_main(
            [
                "--course",
                "physical_design",
                "--add",
                "--title",
                "Physical Design",
                "--order",
                "20",
                "--questions",
                str(candidate),
                *common,
            ]
        )
        == 0
    )
    manifest_path = courses_dir / "physical_design" / "course.json"
    manifest = json.loads(manifest_path.read_text())
    assert manifest["course_id"] == "physical_design"
    assert manifest["enabled"] is True
    assert manifest["order"] == 20
    assert manifest["glossary"] is None

    # Adding the same course twice is refused instead of overwriting content.
    assert (
        publish_course_main(
            ["--course", "physical_design", "--add", "--questions", str(candidate), *common]
        )
        == 1
    )

    assert (
        publish_course_main(["--course", "physical_design", "--disable", *common]) == 0
    )
    assert json.loads(manifest_path.read_text())["enabled"] is False
    assert publish_course_main(["--course", "physical_design", "--enable", *common]) == 0
    assert json.loads(manifest_path.read_text())["enabled"] is True
    # With two declared courses, a publish without --course is ambiguous.
    write_course(courses_dir, "second_course", course_bank())
    from scripts.course_tooling import build_loader, resolve_definition

    loader = build_loader(
        courses_dir,
        tmp_path / "absent" / "questions.json",
        tmp_path / "absent" / "glossary.json",
    )
    with pytest.raises(ToolingError, match="ambiguous"):
        resolve_definition(loader, None)
    capsys.readouterr()



def test_rename_course_moves_the_namespace_without_touching_history(tmp_path, capsys):
    """``scripts/rename_course.py`` rewrites the key, not the learner data."""
    import sqlite3

    from scripts.rename_course import main as rename_main
    from tests.test_course_migration import build_legacy_database
    from app.repositories import Database
    from app.repositories.schema_migrations import LEGACY_COURSE_KEY, read_meta

    database = build_legacy_database(tmp_path / "legacy.db")
    Database(database).initialize()

    def query(sql, *params):
        connection = sqlite3.connect(database)
        try:
            return connection.execute(sql, params).fetchall()
        finally:
            connection.close()

    before = {
        table: query(f"SELECT * FROM {table} WHERE course_id = 'legacy'")
        for table in (
            "attempts",
            "wrong_questions",
            "weak_knowledge_points",
            "quiz_progress",
            "exam_sessions",
            "question_registry",
            "question_bank_state",
        )
    }

    # A dry run reports the rows and writes nothing.
    assert (
        rename_main(
            [
                "--db",
                str(database),
                "--from",
                "legacy",
                "--to",
                "eek5106",
                "--dry-run",
            ]
        )
        == 0
    )
    assert query("SELECT course_id FROM courses") == [("legacy",)]

    assert (
        rename_main(
            [
                "--db",
                str(database),
                "--from",
                "legacy",
                "--to",
                "eek5106",
                "--no-backup",
            ]
        )
        == 0
    )
    assert query("SELECT course_id FROM courses") == [("eek5106",)]
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        assert read_meta(connection, LEGACY_COURSE_KEY) == "eek5106"
    finally:
        connection.close()
    # Every scoped row moved, and nothing was left behind or lost.
    for table, rows in before.items():
        assert query(f"SELECT * FROM {table} WHERE course_id = 'legacy'") == []
        after = query(f"SELECT * FROM {table} WHERE course_id = 'eek5106'")
        assert len(after) == len(rows)
    # exam_questions has no namespace of its own and must be untouched.
    assert len(query("SELECT * FROM exam_questions")) == 2

    # Restarting must not recreate a phantom ``legacy`` course row.
    Database(database).initialize()
    assert query("SELECT course_id FROM courses") == [("eek5106",)]

    # The target namespace may not already own data.
    assert (
        rename_main(
            [
                "--db",
                str(database),
                "--from",
                "eek5106",
                "--to",
                "legacy",
                "--no-backup",
            ]
        )
        == 0
    )
    assert (
        rename_main(
            [
                "--db",
                str(database),
                "--from",
                "legacy",
                "--to",
                "eek5106",
                "--no-backup",
            ]
        )
        == 0
    )
    assert (
        rename_main(
            ["--db", str(database), "--from", "eek5106", "--to", "legacy", "--no-backup"]
        )
        == 0
    )
    # Now ``legacy`` exists again, so renaming a *different* namespace onto it must fail.
    assert (
        rename_main(
            [
                "--db",
                str(database),
                "--from",
                "eek5106",
                "--to",
                "legacy",
                "--no-backup",
            ]
        )
        == 1
    )
    capsys.readouterr()

