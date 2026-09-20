"""CLI tooling: read-only preflight, atomic publish, bytes frozen once."""

import hashlib
import json
import shutil
import sqlite3

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
from scripts.delete_course import main as delete_course_main
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


def test_check_courses_reports_a_copy_no_manifest_points_at(tmp_path, capsys):
    """A stale plain copy is reported, but never fails the gate.

    A course created before the content-addressed layout keeps a
    ``questions.json``/``glossary.json`` next to the manifest.  Once the manifest
    points at ``versions/<sha256>/`` nothing reads that file any more, so the gate
    has to show it instead of letting it look like the live content.
    """
    courses_dir = tmp_path / "courses"
    courses_dir.mkdir(parents=True)
    candidate = tmp_path / "candidate.json"
    write_json(candidate, course_bank())
    assert (
        publish_course_main(
            [
                "--course",
                A,
                "--add",
                "--questions",
                str(candidate),
                *cli_common(courses_dir, tmp_path / "mcq.db"),
            ]
        )
        == 0
    )
    capsys.readouterr()

    common = [
        "--courses-dir",
        str(courses_dir),
        "--question-file",
        str(tmp_path / "absent" / "questions.json"),
        "--glossary-file",
        str(tmp_path / "absent" / "glossary.json"),
    ]

    # A plain-file course declares the root copy itself: nothing to report.
    write_course(courses_dir, B, course_bank(("b", "beta")))
    assert check_courses_main(common) == 0
    assert "未被 manifest 引用" not in capsys.readouterr().out

    # The stale copy of a course-addressed course is reported; exit code stays 0.
    stale = courses_dir / A / "questions.json"
    write_json(stale, course_bank(("c", "gamma")))
    assert check_courses_main(common) == 0
    out = capsys.readouterr().out
    assert str(stale) in out
    assert "未被 manifest 引用" in out


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


def test_check_glossary_reports_a_retired_term_field_without_failing(
    tmp_path, capsys
):
    """A leftover ``definition`` is ignored by the loader and reported, not fatal."""
    questions = tmp_path / "questions.json"
    glossary = tmp_path / "glossary.json"
    write_json(questions, course_bank())
    payload = course_glossary("alpha")
    payload["terms"][0]["definition"] = "An English definition."
    write_json(glossary, payload)

    exit_code = check_glossary_main(
        ["--questions", str(questions), "--glossary", str(glossary)]
    )

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "Retired term fields" in out
    assert '"definition" in 1 term(s): alpha-term-1' in out


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
    target = courses_dir / "physical_design"
    manifest = json.loads((target / "course.json").read_text())
    questions_digest = hashlib.sha256(questions.read_bytes()).hexdigest()
    glossary_digest = hashlib.sha256(glossary.read_bytes()).hexdigest()
    assert manifest["questions"] == f"versions/{questions_digest}/questions.json"
    assert manifest["glossary"] == f"versions/{glossary_digest}/glossary.json"
    assert (target / "versions" / questions_digest / "questions.json").is_file()
    assert (target / "versions" / glossary_digest / "glossary.json").is_file()
    # `--add` archives exactly what it validated: no unreferenced copy is left
    # next to the manifest for someone to edit by mistake.
    assert not (target / "questions.json").exists()
    assert not (target / "glossary.json").exists()

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


# --------------------------------------------------- default candidate files


def _changed_bank(tag: str = "alpha", text: str = "Rewritten for alpha") -> dict:
    """A bank whose first question was edited (content-only, no generation bump)."""
    payload = course_bank(("a", tag))
    payload["questions"][0]["text"] = text
    return payload


def test_check_question_bank_defaults_to_the_course_candidate(world, capsys):
    """No positional path: the course's ``questions_candidate.json`` is checked."""
    app, courses_dir, database, _digest = world
    del app
    write_json(courses_dir / A / "questions_candidate.json", _changed_bank())

    assert check_main(["--course", A, *cli_common(courses_dir, database)]) == 0
    out = capsys.readouterr().out
    assert "questions_candidate.json" in out
    assert "(content-only): 1" in out

    # --published ignores the working copy and re-checks what the course serves.
    assert check_main(["--course", A, "--published", *cli_common(courses_dir, database)]) == 0
    out = capsys.readouterr().out
    assert "questions_candidate.json" not in out
    assert "(content-only): 1" not in out


def test_check_question_bank_maps_a_candidate_inside_a_published_course(world, capsys):
    """A working copy next to the manifest belongs to that course, not to legacy."""
    app, courses_dir, database, _digest = world
    del app
    assert (
        publish_course_main(
            [
                "--course",
                A,
                "--questions",
                str(courses_dir / A / "questions.json"),
                *cli_common(courses_dir, database),
            ]
        )
        == 0
    )
    # The manifest now points at versions/<digest>/, so the published file is no
    # longer a sibling of the course's default working copy.
    candidate = write_json(courses_dir / A / "questions_candidate.json", _changed_bank())

    assert check_main([str(candidate), *cli_common(courses_dir, database)]) == 0
    out = capsys.readouterr().out
    assert f"课程 (course_id): {A}" in out


def test_check_question_bank_refuses_published_with_an_explicit_path(world, capsys):
    app, courses_dir, database, _digest = world
    del app
    candidate = courses_dir / A / "questions_candidate.json"
    write_json(candidate, course_bank())
    assert (
        check_main(
            [str(candidate), "--published", *cli_common(courses_dir, database)]
        )
        == 2
    )
    assert "二选一" in capsys.readouterr().err


def test_check_glossary_defaults_to_the_course_candidates(tmp_path, capsys):
    """Course mode reads the two default working copies when they exist."""
    courses_dir = tmp_path / "courses"
    write_course(courses_dir, A, course_bank(), glossary=course_glossary("alpha"))
    app = make_multi_app(
        tmp_path, {A: course_bank()}, glossaries={A: course_glossary("alpha")}
    )
    del app
    candidate_glossary = course_glossary("alpha")
    candidate_glossary["terms"].append(
        {"id": "alpha-term-3", "term": "alpha term three", "term_zh": "三"}
    )
    write_json(courses_dir / A / "glossary_candidate.json", candidate_glossary)
    write_json(courses_dir / A / "questions_candidate.json", _changed_bank())
    common = [
        "--courses-dir",
        str(courses_dir),
        "--question-file",
        str(tmp_path / "absent" / "questions.json"),
    ]

    assert check_glossary_main(["--course", A, *common]) == 0
    out = capsys.readouterr().out
    assert "Validated 3 canonical terms" in out
    assert "glossary_candidate.json" in out
    assert "语料 (candidate)" in out

    # --published falls back to the deployed pair (2 terms).
    assert check_glossary_main(["--course", A, "--published", *common]) == 0
    out = capsys.readouterr().out
    assert "Validated 2 canonical terms" in out
    assert "语料 (published)" in out


def test_publish_course_uses_the_default_candidates(tmp_path, capsys):
    """A bare ``--course`` publishes both default working copies."""
    courses_dir = tmp_path / "courses"
    write_course(courses_dir, A, course_bank(), glossary=course_glossary("alpha"))
    app = make_multi_app(
        tmp_path, {A: course_bank()}, glossaries={A: course_glossary("alpha")}
    )
    del app
    database = tmp_path / "mcq.db"
    questions = write_json(
        courses_dir / A / "questions_candidate.json", _changed_bank()
    )
    glossary = course_glossary("alpha")
    glossary["terms"].append(
        {"id": "alpha-term-3", "term": "alpha term three", "term_zh": "三"}
    )
    glossary_path = write_json(courses_dir / A / "glossary_candidate.json", glossary)

    assert publish_course_main(["--course", A, *cli_common(courses_dir, database)]) == 0

    manifest = json.loads((courses_dir / A / "course.json").read_text())
    questions_digest = hashlib.sha256(questions.read_bytes()).hexdigest()
    glossary_digest = hashlib.sha256(glossary_path.read_bytes()).hexdigest()
    assert manifest["questions"] == f"versions/{questions_digest}/questions.json"
    assert manifest["glossary"] == f"versions/{glossary_digest}/glossary.json"
    assert "使用默认候选" in capsys.readouterr().out



def _publish_bank_versions(courses_dir, database, tmp_path, texts, *extra):
    """Publish one changed question bank per text; return the digests in order."""
    digests = []
    for index, text in enumerate(texts):
        candidate = tmp_path / f"candidate-{index}.json"
        changed = course_bank(("a", "alpha"))
        changed["questions"][0]["text"] = text
        payload = write_json(candidate, changed)
        digests.append(hashlib.sha256(payload.read_bytes()).hexdigest())
        assert (
            publish_course_main(
                [
                    "--course",
                    A,
                    "--questions",
                    str(candidate),
                    *extra,
                    *cli_common(courses_dir, database),
                ]
            )
            == 0
        )
    return digests


def test_publish_keeps_the_current_and_the_previous_version_only(
    world, tmp_path, capsys
):
    """The retention pass keeps one rollback step and deletes the rest."""
    app, courses_dir, database, _digest = world
    del app

    digests = _publish_bank_versions(
        courses_dir, database, tmp_path, ("one", "two", "three")
    )

    versions = courses_dir / A / "versions"
    assert sorted(path.name for path in versions.iterdir()) == sorted(digests[-2:])
    manifest = json.loads((courses_dir / A / "course.json").read_text())
    assert manifest["questions"] == f"versions/{digests[-1]}/questions.json"
    out = capsys.readouterr().out
    assert f"删除 versions/{digests[0]}/questions.json" in out


def test_version_retention_counts_each_content_type_separately(tmp_path, capsys):
    """A glossary publish never evicts the previous question bank."""
    courses_dir = tmp_path / "courses"
    glossary = course_glossary("alpha")
    write_course(courses_dir, A, course_bank(), glossary=glossary)
    app = make_multi_app(tmp_path, {A: course_bank()}, glossaries={A: glossary})
    del app
    database = tmp_path / "mcq.db"

    question_digests = _publish_bank_versions(
        courses_dir, database, tmp_path, ("one", "two")
    )
    glossary_digests = []
    for suffix in ("one", "two"):
        changed = course_glossary("alpha")
        changed["terms"][0]["term"] = f"alpha glossary {suffix}"
        candidate = write_json(courses_dir / A / "glossary_candidate.json", changed)
        glossary_digests.append(hashlib.sha256(candidate.read_bytes()).hexdigest())
        assert (
            publish_course_main(
                [
                    "--course",
                    A,
                    "--glossary",
                    str(candidate),
                    *cli_common(courses_dir, database),
                ]
            )
            == 0
        )

    versions = courses_dir / A / "versions"
    assert sorted(
        path.name for path in versions.iterdir() if (path / "questions.json").is_file()
    ) == sorted(question_digests)
    assert sorted(
        path.name for path in versions.iterdir() if (path / "glossary.json").is_file()
    ) == sorted(glossary_digests)
    capsys.readouterr()


def test_keep_versions_one_leaves_only_the_current_version(world, tmp_path, capsys):
    """``--keep-versions 1`` trades the rollback copy for the least disk use."""
    app, courses_dir, database, _digest = world
    del app

    digests = _publish_bank_versions(
        courses_dir, database, tmp_path, ("one", "two"), "--keep-versions", "1"
    )

    versions = courses_dir / A / "versions"
    assert sorted(path.name for path in versions.iterdir()) == [digests[-1]]
    capsys.readouterr()


def test_keep_versions_must_be_positive(world, tmp_path, capsys):
    _app, courses_dir, database, _digest = world

    assert (
        publish_course_main(
            ["--course", A, "--keep-versions", "0", *cli_common(courses_dir, database)]
        )
        == 2
    )
    assert "keep-versions" in capsys.readouterr().err


def test_prune_is_refused_with_contradictory_flags(world, tmp_path, capsys):
    _app, courses_dir, database, _digest = world
    common = cli_common(courses_dir, database)

    assert publish_course_main(["--course", A, "--prune", "--no-prune", *common]) == 2
    assert "--no-prune" in capsys.readouterr().err
    assert publish_course_main(["--course", A, "--prune", "--enable", *common]) == 2
    assert "--enable" in capsys.readouterr().err
    assert (
        publish_course_main(["--course", A, "--prune", "--add", "--title", "X", *common])
        == 2
    )
    assert "add" in capsys.readouterr().err


def test_prune_only_run_cleans_what_no_prune_left_behind(world, tmp_path, capsys):
    """``--prune`` works without content, and never deletes an unexpected file."""
    app, courses_dir, database, _digest = world
    del app
    digests = _publish_bank_versions(
        courses_dir, database, tmp_path, ("one", "two", "three"), "--no-prune"
    )
    versions = courses_dir / A / "versions"
    assert len(list(versions.iterdir())) == 3
    notes = versions / digests[0] / "notes.txt"
    notes.write_text("keep me", encoding="utf-8")
    capsys.readouterr()

    exit_code = publish_course_main(
        ["--course", A, "--prune", *cli_common(courses_dir, database)]
    )

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "状态：pruned" in out
    remaining = sorted(path.name for path in versions.iterdir())
    assert digests[-1] in remaining
    assert digests[-2] in remaining
    # The superseded copy lost its content, but the directory it shared with an
    # unrelated file is left in place.
    assert digests[0] in remaining
    assert not (versions / digests[0] / "questions.json").exists()
    assert notes.is_file()


def test_prune_is_a_no_op_for_a_course_without_published_copies(
    tmp_path, capsys
):
    """A plain-file course (and a fresh `--add`) has nothing to prune."""
    courses_dir = tmp_path / "courses"
    write_course(courses_dir, A, course_bank(), glossary=course_glossary("alpha"))
    app = make_multi_app(
        tmp_path, {A: course_bank()}, glossaries={A: course_glossary("alpha")}
    )
    del app
    database = tmp_path / "mcq.db"

    exit_code = publish_course_main(
        ["--course", A, "--prune", *cli_common(courses_dir, database)]
    )

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "没有已发布副本" in out
    assert not (courses_dir / A / "versions").exists()


def test_publish_course_add_uses_the_default_candidates(tmp_path, capsys):
    """``--add`` picks the new course's default working copies up."""
    courses_dir = tmp_path / "courses"
    target = courses_dir / "physical_design"
    target.mkdir(parents=True)
    write_json(target / "questions_candidate.json", course_bank())
    write_json(target / "glossary_candidate.json", course_glossary("alpha"))

    assert (
        publish_course_main(
            [
                "--course",
                "physical_design",
                "--add",
                "--title",
                "Physical Design",
                *cli_common(courses_dir, tmp_path / "mcq.db"),
            ]
        )
        == 0
    )

    manifest = json.loads((target / "course.json").read_text())
    assert manifest["title"] == "Physical Design"
    questions_digest = hashlib.sha256(
        (target / "questions_candidate.json").read_bytes()
    ).hexdigest()
    glossary_digest = hashlib.sha256(
        (target / "glossary_candidate.json").read_bytes()
    ).hexdigest()
    assert manifest["questions"] == f"versions/{questions_digest}/questions.json"
    assert manifest["glossary"] == f"versions/{glossary_digest}/glossary.json"
    assert (target / "versions" / questions_digest / "questions.json").is_file()
    assert (target / "versions" / glossary_digest / "glossary.json").is_file()
    # The candidates stay the working copies; the published bytes live only under
    # versions/, so the course directory holds no unreferenced content copy.
    assert (target / "questions_candidate.json").is_file()
    assert not (target / "questions.json").exists()
    assert not (target / "glossary.json").exists()

    # Without any candidate at all, --add points at the path it expected.
    assert (
        publish_course_main(
            [
                "--course",
                "empty_course",
                "--add",
                *cli_common(courses_dir, tmp_path / "mcq.db"),
            ]
        )
        == 2
    )
    err = capsys.readouterr().err
    assert "questions_candidate.json" in err
    assert not (courses_dir / "empty_course").exists()


def test_publish_course_without_content_names_the_default_paths(tmp_path, capsys):
    courses_dir = tmp_path / "courses"
    write_course(courses_dir, A, course_bank())

    assert (
        publish_course_main(
            ["--course", A, *cli_common(courses_dir, tmp_path / "mcq.db")]
        )
        == 2
    )
    err = capsys.readouterr().err
    assert "questions_candidate.json" in err
    assert "glossary_candidate.json" in err


def test_publish_course_ignores_a_glossary_candidate_the_course_never_declared(
    tmp_path, capsys
):
    """Enabling a glossary for a ``glossary: null`` course stays explicit."""
    courses_dir = tmp_path / "courses"
    write_course(courses_dir, A, course_bank())
    app = make_multi_app(tmp_path, {A: course_bank()})
    del app
    database = tmp_path / "mcq.db"
    write_json(courses_dir / A / "questions_candidate.json", _changed_bank())
    write_json(courses_dir / A / "glossary_candidate.json", course_glossary("alpha"))

    assert publish_course_main(["--course", A, *cli_common(courses_dir, database)]) == 0
    manifest = json.loads((courses_dir / A / "course.json").read_text())
    assert manifest["glossary"] is None
    assert "glossary 为 null" in capsys.readouterr().out



# ------------------------------------------------------------- delete course


SCOPED_TABLES = (
    "quiz_progress",
    "attempts",
    "wrong_questions",
    "weak_knowledge_points",
    "exam_sessions",
    "question_bank_state",
    "question_registry",
)


def _scalar(database, sql: str, params: tuple = ()):
    connection = sqlite3.connect(database)
    try:
        return connection.execute(sql, params).fetchone()[0]
    finally:
        connection.close()


def _seed_course_state(database, course_id: str) -> None:
    """Give one course a full database footprint: learner rows, exam, bookkeeping."""
    stamp = "2026-01-01T00:00:00+00:00"
    exam_id = f"exam-{course_id}"
    statements = (
        (
            "INSERT OR REPLACE INTO quiz_progress "
            "(learner_id, course_id, mode, bank_version, state) "
            "VALUES (?, ?, 'normal', 'bank-v1', ?)",
            ("u1", course_id, json.dumps({"position": 0})),
        ),
        (
            "INSERT OR REPLACE INTO attempts (learner_id, course_id, question_id, "
            "mode, selected_answers, is_correct, answered_at) "
            "VALUES (?, ?, 'q001', 'normal', ?, 1, ?)",
            ("u1", course_id, json.dumps(["a"]), stamp),
        ),
        (
            "INSERT OR REPLACE INTO wrong_questions "
            "(learner_id, course_id, question_id, last_wrong_at) "
            "VALUES (?, ?, 'q001', ?)",
            ("u1", course_id, stamp),
        ),
        (
            "INSERT OR REPLACE INTO weak_knowledge_points "
            "(learner_id, course_id, chapter_id, last_wrong_at, updated_at) "
            "VALUES (?, ?, 'chapter_1', ?, ?)",
            ("u1", course_id, stamp, stamp),
        ),
        (
            "INSERT OR REPLACE INTO exam_sessions "
            "(id, course_id, learner_id, status, question_count, option_seed, "
            "created_at, started_at) VALUES (?, ?, 'u1', 'in_progress', 1, '1', ?, ?)",
            (exam_id, course_id, stamp, stamp),
        ),
        (
            "INSERT OR REPLACE INTO exam_questions (exam_id, position, question_id) "
            "VALUES (?, 0, 'q001')",
            (exam_id,),
        ),
        (
            "INSERT OR REPLACE INTO question_bank_state "
            "(course_id, bank_version, generation) VALUES (?, 'bank-v1', 1)",
            (course_id,),
        ),
        (
            "INSERT OR REPLACE INTO question_registry "
            "(course_id, question_id, status, question_type, option_ids, "
            "correct_answers, content_fingerprint, first_seen_at, last_seen_at) "
            "VALUES (?, 'q999', 'retired', 'single', ?, ?, 'c', ?, ?)",
            (course_id, json.dumps(["a", "b"]), json.dumps(["a"]), stamp, stamp),
        ),
    )
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        for sql, params in statements:
            connection.execute(sql, params)
        connection.commit()
    finally:
        connection.close()


def _set_default_course(database, course_id: str) -> None:
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "INSERT INTO schema_meta (key, value) VALUES ('default_course_id', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (course_id,),
        )
        connection.commit()
    finally:
        connection.close()


def _snapshot(database, course_id: str) -> dict[str, int]:
    """Count every row that belongs to one course, across every table."""
    counts = {
        table: _scalar(
            database, f"SELECT COUNT(*) FROM {table} WHERE course_id = ?", (course_id,)
        )
        for table in SCOPED_TABLES
    }
    counts["exam_questions"] = _scalar(
        database,
        "SELECT COUNT(*) FROM exam_questions q JOIN exam_sessions s "
        "ON s.id = q.exam_id WHERE s.course_id = ?",
        (course_id,),
    )
    counts["courses"] = _scalar(
        database, "SELECT COUNT(*) FROM courses WHERE course_id = ?", (course_id,)
    )
    return counts


def _seeded_two_course_world(tmp_path):
    """A two-course deployment whose courses both own learner data."""
    courses_dir = tmp_path / "courses"
    write_course(courses_dir, A, course_bank(), glossary=course_glossary("alpha"))
    write_course(courses_dir, B, course_bank(("b", "beta")))
    app = make_multi_app(
        tmp_path,
        {A: course_bank(), B: course_bank(("b", "beta"))},
        glossaries={A: course_glossary("alpha")},
    )
    database = tmp_path / "mcq.db"
    _seed_course_state(database, A)
    _seed_course_state(database, B)
    _set_default_course(database, A)
    return app, courses_dir, database



def test_delete_course_removes_directory_and_every_namespace_row(tmp_path, capsys):
    """The directory and every database reference go together, and only that one."""
    app, courses_dir, database = _seeded_two_course_world(tmp_path)
    before_b = _snapshot(database, B)

    assert (
        delete_course_main(
            ["--course", A, "--force", *cli_common(courses_dir, database)]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "已删除课程目录" in out

    # The directory is gone; the other course is untouched.
    assert not (courses_dir / A).exists()
    assert (courses_dir / B / "course.json").is_file()

    # No scoped row, no registry tombstone, no generation row is left for A.
    assert _snapshot(database, A) == dict.fromkeys(_snapshot(database, A), 0)
    assert _snapshot(database, B) == before_b
    # Only B and the persisted legacy placeholder identity remain.
    assert (
        _scalar(
            database,
            "SELECT GROUP_CONCAT(course_id) FROM "
            "(SELECT course_id FROM courses ORDER BY course_id)",
        )
        == "course_b,legacy"
    )

    # The navigation preference pointed at A and was cleared with it.
    assert (
        _scalar(
            database,
            "SELECT COUNT(*) FROM schema_meta WHERE key = 'default_course_id'",
        )
        == 0
    )
    # A timestamped backup was written before any deletion.
    assert list(tmp_path.glob("mcq.db.bak-*"))

    # The application still assembles, and no longer serves A.
    from app import create_app

    restarted = create_app(dict(app.config))
    registry = restarted.extensions["mcq_services"].course_registry
    assert registry.has(A) is False
    assert registry.has(B) is True



def test_delete_course_dry_run_writes_nothing(tmp_path, capsys):
    app, courses_dir, database = _seeded_two_course_world(tmp_path)
    del app
    before_a = _snapshot(database, A)
    manifest_before = (courses_dir / A / "course.json").read_bytes()

    assert (
        delete_course_main(
            ["--course", A, "--force", "--dry-run", *cli_common(courses_dir, database)]
        )
        == 0
    )

    out = capsys.readouterr().out
    assert "--dry-run" in out
    # Everything the real run would touch is reported and nothing changed.
    assert "attempts: 1 行" in out
    assert "exam_sessions: 1 行" in out
    assert "exam_questions: 1 行" in out
    # The application's own sync registered q001/q002, and the seed added q999.
    assert "question_registry: 3 行" in out
    assert "schema_meta.default_course_id" in out
    assert (courses_dir / A / "course.json").read_bytes() == manifest_before
    assert _snapshot(database, A) == before_a
    assert (
        _scalar(
            database,
            "SELECT value FROM schema_meta WHERE key = 'default_course_id'",
        )
        == A
    )
    assert not list(tmp_path.glob("mcq.db.bak-*"))


def test_delete_course_requires_force_while_learner_data_exists(tmp_path, capsys):
    app, courses_dir, database = _seeded_two_course_world(tmp_path)
    del app
    before_a = _snapshot(database, A)

    assert delete_course_main(["--course", A, *cli_common(courses_dir, database)]) == 1

    err = capsys.readouterr().err
    assert "--force" in err and "拒绝删除" in err
    assert (courses_dir / A / "course.json").is_file()
    assert _snapshot(database, A) == before_a
    assert not list(tmp_path.glob("mcq.db.bak-*"))


def test_delete_course_refuses_an_unknown_or_legacy_course(tmp_path, capsys):
    app, courses_dir, database = _seeded_two_course_world(tmp_path)
    del app

    # A typo must never silently delete a namespace that happens to exist.
    assert (
        delete_course_main(
            ["--course", "does_not_exist", *cli_common(courses_dir, database)]
        )
        == 1
    )
    err = capsys.readouterr().err
    assert "未知课程" in err and A in err and B in err
    assert _scalar(database, "SELECT COUNT(*) FROM courses") == 3

    # The legacy root-file layout has no course directory to delete.
    root = tmp_path / "legacy_root"
    root.mkdir()
    write_json(root / "questions.json", course_bank())
    assert (
        delete_course_main(
            [
                "--course",
                "legacy",
                "--db",
                str(database),
                "--courses-dir",
                str(tmp_path / "absent_courses"),
                "--question-file",
                str(root / "questions.json"),
                "--glossary-file",
                str(root / "glossary.json"),
            ]
        )
        == 1
    )
    assert "legacy adapter" in capsys.readouterr().err
    assert _scalar(database, "SELECT COUNT(*) FROM courses") == 3


def test_delete_course_cleans_an_undeployed_database_identity(tmp_path, capsys):
    """A manually removed directory can still be finished off from the database."""
    app, courses_dir, database = _seeded_two_course_world(tmp_path)
    del app
    shutil.rmtree(courses_dir / A)  # what a manual rm leaves behind
    before_b = _snapshot(database, B)

    assert (
        delete_course_main(
            ["--course", A, "--force", *cli_common(courses_dir, database)]
        )
        == 0
    )

    out = capsys.readouterr().out
    assert "未部署的课程身份" in out
    assert _snapshot(database, A) == dict.fromkeys(_snapshot(database, A), 0)
    assert _snapshot(database, B) == before_b
    assert (courses_dir / B / "course.json").is_file()


def test_delete_course_refuses_the_persisted_legacy_namespace(tmp_path, capsys):
    """``legacy_course_id`` is recreated on every startup, so it cannot be deleted."""
    from app.repositories import Database
    from tests.test_course_migration import build_legacy_database

    database = build_legacy_database(tmp_path / "legacy.db")
    Database(database).initialize()
    before = _snapshot(database, "legacy")
    assert before["courses"] == 1

    assert (
        delete_course_main(
            [
                "--course",
                "legacy",
                "--force",
                "--db",
                str(database),
                "--courses-dir",
                str(tmp_path / "absent_courses"),
                "--question-file",
                str(tmp_path / "absent" / "questions.json"),
                "--glossary-file",
                str(tmp_path / "absent" / "glossary.json"),
            ]
        )
        == 1
    )

    assert "legacy_course_id" in capsys.readouterr().err
    assert _snapshot(database, "legacy") == before



def test_new_course_gates_require_the_manifest_written_by_add(tmp_path, capsys):
    """``--course`` cannot resolve a directory that only holds candidates yet.

    This is the documented trap for a brand-new course: only ``course.json``
    (written by ``--add``) declares a course, so both read-only gates must
    explain that instead of silently checking some other namespace.
    """
    courses_dir = tmp_path / "courses"
    target = courses_dir / "physical_design"
    target.mkdir(parents=True)
    write_json(target / "questions_candidate.json", course_bank())
    write_json(target / "glossary_candidate.json", course_glossary("alpha"))
    absent_questions = str(courses_dir.parent / "absent" / "questions.json")
    absent_glossary = str(courses_dir.parent / "absent" / "glossary.json")
    courses_args = ["--courses-dir", str(courses_dir)]
    glossary_args = [
        "--course",
        "physical_design",
        *courses_args,
        "--question-file",
        absent_questions,
    ]

    assert check_glossary_main(glossary_args) == 1
    assert "Unknown course" in capsys.readouterr().err

    assert (
        check_main(
            [
                "--course",
                "physical_design",
                "--db",
                str(tmp_path / "mcq.db"),
                *courses_args,
                "--question-file",
                absent_questions,
                "--glossary-file",
                absent_glossary,
            ]
        )
        == 4
    )
    err = capsys.readouterr().err
    assert "Unknown course" in err
    # The hint names the command that declares the course.
    assert "--add" in err and "publish_course.py" in err

    # The documented order: create the course, then the same gates resolve it.
    assert (
        publish_course_main(
            [
                "--course",
                "physical_design",
                "--add",
                "--title",
                "Physical Design",
                *courses_args,
                "--question-file",
                absent_questions,
                "--glossary-file",
                absent_glossary,
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert check_glossary_main(glossary_args) == 0

