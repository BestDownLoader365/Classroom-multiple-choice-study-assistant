"""Course rename: the staged protocol, compensation and ``--recover``.

    phase 0  every precondition, read-only
    phase 1  courses/<from> -> courses/.rename-staging-…  + manifest rewrite
    phase 2  one database transaction                      (the commit point)
    phase 3  staging -> courses/<to>

Each test asserts the four states a rename has to keep consistent — database id,
directory name, manifest ``course_id``, and what a fresh loader resolves — plus
the exit code and the recovery path, because "two-phase" is only meaningful if
every interruption has a deterministic answer.
"""

from __future__ import annotations

import json

import pytest

from app.repositories import CourseLoader
from app.repositories.schema_migrations import (
    DEFAULT_COURSE_KEY,
    LEGACY_COURSE_KEY,
    read_meta,
)
from scripts import course_tooling, rename_course
from scripts.course_tooling import RENAME_STAGING_PREFIX, rename_state_path
from scripts.rename_course import main as rename_main
from tests.conftest import course_bank, course_glossary, write_course


def _world(tmp_path):
    """A declared two-course deployment with one shared database."""
    from app import create_app

    courses_dir = tmp_path / "courses"
    write_course(courses_dir, "course_a", course_bank(), glossary=course_glossary("a"))
    write_course(courses_dir, "course_b", course_bank())
    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "COURSES_DIR": courses_dir,
            "QUESTION_FILE": tmp_path / "absent" / "questions.json",
            "GLOSSARY_FILE": tmp_path / "absent" / "glossary.json",
            "DATABASE": tmp_path / "mcq.db",
        }
    )
    return app, courses_dir, tmp_path / "mcq.db"


def _common(courses_dir, database):
    return [
        "--db",
        str(database),
        "--courses-dir",
        str(courses_dir),
    ]


def _manifest(courses_dir, course_id) -> dict:
    return json.loads(
        (courses_dir / course_id / "course.json").read_text(encoding="utf-8")
    )


def _namespace(database) -> list[str]:
    import sqlite3

    connection = sqlite3.connect(database)
    try:
        return [row[0] for row in connection.execute("SELECT course_id FROM courses")]
    finally:
        connection.close()


def _meta(database, key):
    import sqlite3

    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        return read_meta(connection, key)
    finally:
        connection.close()


def _scoped_rows(database, course_id) -> dict[str, int]:
    """Per-table row counts of one namespace, for "nothing moved" assertions."""
    import sqlite3

    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        return {
            table: int(
                connection.execute(
                    f'SELECT COUNT(*) AS total FROM "{table}" WHERE course_id = ?',
                    (course_id,),
                ).fetchone()["total"]
            )
            for table in rename_course.SCOPED_TABLES
        }
    finally:
        connection.close()


def _staging_entries(courses_dir):
    return sorted(
        entry
        for entry in courses_dir.iterdir()
        if entry.is_dir() and entry.name.startswith(RENAME_STAGING_PREFIX)
    )


# ------------------------------------------------------------------- happy path


def test_successful_rename_keeps_database_directory_and_manifest_in_step(
    tmp_path, capsys
):
    app, courses_dir, database = _world(tmp_path)
    del app
    before_b = _manifest(courses_dir, "course_b")

    assert (
        rename_main(
            [
                "--from",
                "course_a",
                "--to",
                "course_c",
                "--rename-directory",
                *_common(courses_dir, database),
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert sorted(_namespace(database)) == ["course_b", "course_c", "legacy"]
    assert (courses_dir / "course_a").exists() is False
    assert (courses_dir / "course_c" / "course.json").is_file()
    assert _manifest(courses_dir, "course_c")["course_id"] == "course_c"
    assert _manifest(courses_dir, "course_b") == before_b
    # No staging directory and no state file survive a complete run.
    assert _staging_entries(courses_dir) == []
    assert rename_state_path(courses_dir, "course_a").exists() is False
    # A fresh loader resolves the renamed course and nothing else.
    loader = CourseLoader(courses_dir, legacy_directory=tmp_path / "absent")
    assert [d.course_id for d in loader.discover_definitions()] == [
        "course_b",
        "course_c",
    ]
    assert list(tmp_path.glob("mcq.db.bak-*"))


def test_default_navigation_preference_follows_the_rename(tmp_path, capsys):
    """``schema_meta.default_course_id`` must never point at a missing course."""
    import sqlite3

    app, courses_dir, database = _world(tmp_path)
    del app
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "INSERT INTO schema_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (DEFAULT_COURSE_KEY, "course_a"),
        )
        connection.commit()
    finally:
        connection.close()

    assert (
        rename_main(
            ["--from", "course_a", "--to", "course_c", *_common(courses_dir, database)]
        )
        == 0
    )
    capsys.readouterr()
    assert _meta(database, DEFAULT_COURSE_KEY) == "course_c"


def test_rename_moves_every_history_row_and_leaves_exam_slots_alone(tmp_path, capsys):
    import sqlite3

    from tests.test_course_scripts import _seed_course_state

    app, courses_dir, database = _world(tmp_path)
    del app
    _seed_course_state(database, "course_a")

    connection = sqlite3.connect(database)
    try:
        slots = connection.execute("SELECT COUNT(*) FROM exam_questions").fetchone()[0]
    finally:
        connection.close()

    assert (
        rename_main(
            ["--from", "course_a", "--to", "course_c", *_common(courses_dir, database)]
        )
        == 0
    )
    capsys.readouterr()

    connection = sqlite3.connect(database)
    try:
        assert (
            connection.execute("SELECT COUNT(*) FROM exam_questions").fetchone()[0]
            == slots
        )
        for table in ("attempts", "wrong_questions", "question_registry"):
            assert (
                connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE course_id = 'course_a'"
                ).fetchone()[0]
                == 0
            )
    finally:
        connection.close()


def test_rename_without_the_directory_flag_leaves_the_tree_alone(tmp_path, capsys):
    """The documented default: database only, no filesystem change at all."""
    app, courses_dir, database = _world(tmp_path)
    del app
    before = (courses_dir / "course_a" / "course.json").read_bytes()

    assert (
        rename_main(
            ["--from", "course_a", "--to", "course_c", *_common(courses_dir, database)]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "已重命名课程目录" not in out
    assert (courses_dir / "course_a" / "course.json").read_bytes() == before
    assert (courses_dir / "course_c").exists() is False
    assert _staging_entries(courses_dir) == []


# ------------------------------------------------------------ phase 0 refusals


def test_target_namespace_already_owns_data_is_refused_before_anything(tmp_path, capsys):
    app, courses_dir, database = _world(tmp_path)
    del app
    before = (courses_dir / "course_a" / "course.json").read_bytes()

    assert (
        rename_main(
            [
                "--from",
                "course_a",
                "--to",
                "course_b",
                "--rename-directory",
                *_common(courses_dir, database),
            ]
        )
        == 1
    )
    assert "already exists" in capsys.readouterr().err
    assert (courses_dir / "course_a" / "course.json").read_bytes() == before
    assert _staging_entries(courses_dir) == []
    assert rename_state_path(courses_dir, "course_a").exists() is False


def test_target_directory_already_exists_is_refused_before_anything(tmp_path, capsys):
    app, courses_dir, database = _world(tmp_path)
    del app
    (courses_dir / "course_c").mkdir()
    (courses_dir / "course_c" / "keep.txt").write_text("keep", encoding="utf-8")

    assert (
        rename_main(
            [
                "--from",
                "course_a",
                "--to",
                "course_c",
                "--rename-directory",
                *_common(courses_dir, database),
            ]
        )
        == 1
    )
    assert "目标课程目录已存在" in capsys.readouterr().err
    assert "course_c" not in _namespace(database)
    assert (courses_dir / "course_a").is_dir()
    assert (courses_dir / "course_c" / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_missing_manifest_is_refused(tmp_path, capsys):
    """A directory without a manifest is not a course, so it cannot be renamed."""
    app, courses_dir, database = _world(tmp_path)
    del app
    (courses_dir / "course_a" / "course.json").unlink()

    assert (
        rename_main(
            [
                "--from",
                "course_a",
                "--to",
                "course_c",
                "--rename-directory",
                *_common(courses_dir, database),
            ]
        )
        == 1
    )
    err = capsys.readouterr().err
    assert "缺少 manifest" in err and "--rename-directory" in err
    assert (courses_dir / "course_a").is_dir()
    assert "course_c" not in _namespace(database)


# ------------------------------------------------------------------ compensation


def test_manifest_write_failure_restores_the_directory_and_the_manifest(
    tmp_path, monkeypatch, capsys
):
    app, courses_dir, database = _world(tmp_path)
    del app
    before = (courses_dir / "course_a" / "course.json").read_bytes()

    calls = {"count": 0}
    original = course_tooling.write_file_atomically

    def fail_the_first_write(target, payload, **kwargs):  # noqa: ANN001
        calls["count"] += 1
        if calls["count"] == 1:  # the staged manifest rewrite
            raise OSError("simulated disk full")
        return original(target, payload, **kwargs)

    # Only the manifest write fails; the compensating write must succeed, otherwise
    # the test would be checking a different (and much rarer) scenario.
    monkeypatch.setattr(rename_course, "write_file_atomically", fail_the_first_write)
    assert (
        rename_main(
            [
                "--from",
                "course_a",
                "--to",
                "course_c",
                "--rename-directory",
                *_common(courses_dir, database),
            ]
        )
        == 1
    )
    assert "暂存课程目录失败" in capsys.readouterr().err
    # The directory is back with its original bytes and nothing is left to clean.
    assert (courses_dir / "course_a" / "course.json").read_bytes() == before
    assert _staging_entries(courses_dir) == []
    assert rename_state_path(courses_dir, "course_a").exists() is False
    assert "course_c" not in _namespace(database)


def test_database_failure_restores_directory_and_manifest(tmp_path, monkeypatch, capsys):
    app, courses_dir, database = _world(tmp_path)
    del app
    before = (courses_dir / "course_a" / "course.json").read_bytes()

    def explode(*args, **kwargs):  # noqa: ANN001
        raise ValueError("simulated row-count mismatch")

    monkeypatch.setattr(rename_course, "rename_namespace", explode)
    assert (
        rename_main(
            [
                "--from",
                "course_a",
                "--to",
                "course_c",
                "--rename-directory",
                *_common(courses_dir, database),
            ]
        )
        == 1
    )
    err = capsys.readouterr().err
    assert "已回滚并恢复课程目录" in err
    assert (courses_dir / "course_a" / "course.json").read_bytes() == before
    assert _staging_entries(courses_dir) == []
    assert rename_state_path(courses_dir, "course_a").exists() is False
    assert sorted(_namespace(database)) == ["course_a", "course_b", "legacy"]


def test_failed_compensation_reports_manual_intervention(tmp_path, monkeypatch, capsys):
    app, courses_dir, database = _world(tmp_path)
    del app

    def explode(*args, **kwargs):  # noqa: ANN001
        raise ValueError("simulated row-count mismatch")

    def restore_fails(staging, source_dir, original_manifest):  # noqa: ANN001
        raise course_tooling.FilesystemTransactionError("simulated restore failure")

    monkeypatch.setattr(rename_course, "rename_namespace", explode)
    monkeypatch.setattr(rename_course, "restore_directory", restore_fails)
    assert (
        rename_main(
            [
                "--from",
                "course_a",
                "--to",
                "course_c",
                "--rename-directory",
                *_common(courses_dir, database),
            ]
        )
        == 3
    )
    err = capsys.readouterr().err
    assert "需要人工介入" in err and "mv " in err and "状态文件" in err
    assert len(_staging_entries(courses_dir)) == 1


# --------------------------------------------------- in-transaction refusals


def test_foreign_key_violation_rolls_the_rename_back(tmp_path, monkeypatch, capsys):
    """The full parent/child check runs before COMMIT, so it can still roll back.

    The row-count and leftover checks cannot see a *parent* row (``courses`` is
    not a course-scoped table), so dropping it inside the transaction models the
    violation the explicit ``PRAGMA foreign_key_check`` exists for.  The test only
    reaches the check when real child rows point at that parent.
    """
    app, courses_dir, database = _world(tmp_path)
    del app
    before = _scoped_rows(database, "course_a")
    assert sum(before.values()) > 0, "the scenario needs rows that reference the parent"
    real_counts = rename_course._counts

    def drop_the_parent(connection, course_id):  # noqa: ANN001
        counts = real_counts(connection, course_id)
        # Only inside the rename transaction: phase 0 validates on its own,
        # non-transactional connection and has to see the real state.
        if course_id == "course_c" and connection.in_transaction:
            connection.execute("DELETE FROM courses WHERE course_id = ?", (course_id,))
        return counts

    monkeypatch.setattr(rename_course, "_counts", drop_the_parent)

    assert (
        rename_main(
            ["--from", "course_a", "--to", "course_c", *_common(courses_dir, database)]
        )
        == 1
    )
    err = capsys.readouterr().err
    assert "Foreign key check failed during the rename" in err
    assert "No rows were moved" in err
    # That claim is now a fact: the check ran inside the transaction, so every row
    # is still under the old namespace and the parent row is still there.
    assert sorted(_namespace(database)) == ["course_a", "course_b", "legacy"]
    assert _scoped_rows(database, "course_a") == before
    assert not any(_scoped_rows(database, "course_c").values())
    # Only then does a deliberate repair make the rename succeed.
    monkeypatch.setattr(rename_course, "_counts", real_counts)
    assert (
        rename_main(
            ["--from", "course_a", "--to", "course_c", *_common(courses_dir, database)]
        )
        == 0
    )
    capsys.readouterr()
    assert sorted(_namespace(database)) == ["course_b", "course_c", "legacy"]
    assert _scoped_rows(database, "course_c") == before


# -------------------------------------------------- phase 3 failure and --recover


def test_failure_after_the_commit_leaves_a_recoverable_state(
    tmp_path, monkeypatch, capsys
):
    """The last step failing must be finishable, not a permanent undeployed course."""
    app, courses_dir, database = _world(tmp_path)
    del app

    calls = {"count": 0}
    original_rename = course_tooling.atomic_rename

    def fail_the_promote(staging, target_dir):  # noqa: ANN001
        calls["count"] += 1
        if calls["count"] == 1:  # only the original run's phase 3 fails
            raise course_tooling.FilesystemTransactionError(
                "simulated promote failure"
            )
        return original_rename(staging, target_dir)

    monkeypatch.setattr(rename_course, "finalize_directory", fail_the_promote)
    assert (
        rename_main(
            [
                "--from",
                "course_a",
                "--to",
                "course_c",
                "--rename-directory",
                *_common(courses_dir, database),
            ]
        )
        == 3
    )
    err = capsys.readouterr().err
    assert "目录尚未转正" in err and "--recover" in err

    # The database committed; the directory is still staged; the state says so.
    assert sorted(_namespace(database)) == ["course_b", "course_c", "legacy"]
    assert (courses_dir / "course_a").exists() is False
    assert (courses_dir / "course_c").exists() is False
    assert len(_staging_entries(courses_dir)) == 1
    state = course_tooling.read_rename_state(
        rename_state_path(courses_dir, "course_a")
    )
    assert state is not None
    assert state["phase"] == rename_course.PHASE_DB_COMMITTED

    # --dry-run says what would happen without doing it.
    assert rename_main(["--recover", "--dry-run", *_common(courses_dir, database)]) == 0
    assert "将要收尾" in capsys.readouterr().out
    assert len(_staging_entries(courses_dir)) == 1

    # --recover finishes phase 3 and clears the record.
    assert rename_main(["--recover", *_common(courses_dir, database)]) == 0
    assert "已收尾" in capsys.readouterr().out
    assert (courses_dir / "course_c" / "course.json").is_file()
    assert _manifest(courses_dir, "course_c")["course_id"] == "course_c"
    assert _staging_entries(courses_dir) == []
    assert rename_state_path(courses_dir, "course_a").exists() is False
    loader = CourseLoader(courses_dir, legacy_directory=tmp_path / "absent")
    assert "course_c" in {d.course_id for d in loader.discover_definitions()}


def test_crash_before_the_commit_is_rolled_back_by_recover(tmp_path, monkeypatch, capsys):
    """A process killed between the phases is undone, not left half-renamed."""
    app, courses_dir, database = _world(tmp_path)
    del app
    before = (courses_dir / "course_a" / "course.json").read_bytes()

    def crash(*args, **kwargs):  # noqa: ANN001
        raise SystemExit(9)

    monkeypatch.setattr(rename_course, "rename_namespace", crash)
    with pytest.raises(SystemExit):
        rename_main(
            [
                "--from",
                "course_a",
                "--to",
                "course_c",
                "--rename-directory",
                *_common(courses_dir, database),
            ]
        )
    capsys.readouterr()

    # The crash state: staged and recorded, database untouched.
    assert (courses_dir / "course_a").exists() is False
    assert len(_staging_entries(courses_dir)) == 1
    assert sorted(_namespace(database)) == ["course_a", "course_b", "legacy"]

    assert rename_main(["--recover", *_common(courses_dir, database)]) == 0
    assert "已回滚" in capsys.readouterr().out
    assert (courses_dir / "course_a" / "course.json").read_bytes() == before
    assert _staging_entries(courses_dir) == []
    assert sorted(_namespace(database)) == ["course_a", "course_b", "legacy"]


def test_recover_with_nothing_to_do_is_a_no_op(tmp_path, capsys):
    app, courses_dir, database = _world(tmp_path)
    del app
    assert rename_main(["--recover", *_common(courses_dir, database)]) == 0
    assert "没有未完成的改名记录" in capsys.readouterr().out


def test_recover_refuses_an_impossible_state(tmp_path, capsys):
    """A state the protocol cannot produce needs a human, not a guess."""
    app, courses_dir, database = _world(tmp_path)
    del app
    state_path = rename_state_path(courses_dir, "course_a")
    course_tooling.write_rename_state(
        state_path,
        {
            "source": "ghost_source",
            "target": "ghost_target",
            "staging": str(courses_dir / ".rename-staging-ghost-deadbeef"),
            "backup": None,
            "original_manifest": None,
            "phase": rename_course.PHASE_DB_COMMITTED,
        },
    )

    # Neither namespace exists, so nothing can be finished or undone automatically.
    assert rename_main(["--recover", *_common(courses_dir, database)]) == 1
    err = capsys.readouterr().err
    assert "拒绝" in err and "人工" in err
    assert state_path.exists() is True


def test_running_the_main_command_while_a_state_file_exists_is_refused(
    tmp_path, capsys
):
    """The operator is told to recover first, instead of silently starting over."""
    app, courses_dir, database = _world(tmp_path)
    del app
    state_path = rename_state_path(courses_dir, "course_a")
    course_tooling.write_rename_state(
        state_path,
        {
            "source": "course_a",
            "target": "course_c",
            "staging": None,
            "backup": None,
            "original_manifest": None,
            "phase": rename_course.PHASE_STAGING,
        },
    )

    assert (
        rename_main(
            [
                "--from",
                "course_a",
                "--to",
                "course_c",
                "--rename-directory",
                *_common(courses_dir, database),
            ]
        )
        == 1
    )
    err = capsys.readouterr().err
    assert "--recover" in err
    assert (courses_dir / "course_a").is_dir()
    assert sorted(_namespace(database)) == ["course_a", "course_b", "legacy"]


def test_rename_is_idempotent_when_already_done(tmp_path, capsys):
    app, courses_dir, database = _world(tmp_path)
    del app
    args = [
        "--from",
        "course_a",
        "--to",
        "course_c",
        "--rename-directory",
        *_common(courses_dir, database),
    ]
    assert rename_main(args) == 0
    capsys.readouterr()
    manifest = _manifest(courses_dir, "course_c")

    # Re-running cannot find the source namespace any more, and says so.
    assert rename_main(args) == 1
    assert "被拒绝" in capsys.readouterr().err
    assert _manifest(courses_dir, "course_c") == manifest
    assert sorted(_namespace(database)) == ["course_b", "course_c", "legacy"]
    assert (courses_dir / "course_a").exists() is False
    assert _staging_entries(courses_dir) == []


def test_legacy_namespace_rename_updates_the_persisted_key(tmp_path, capsys):
    """The documented use case: move the legacy namespace onto a real course id."""
    app, courses_dir, database = _world(tmp_path)
    del app
    assert _meta(database, LEGACY_COURSE_KEY) == "legacy"

    assert (
        rename_main(
            ["--from", "legacy", "--to", "physical_design", *_common(courses_dir, database)]
        )
        == 0
    )
    capsys.readouterr()
    assert _meta(database, LEGACY_COURSE_KEY) == "physical_design"
    assert sorted(_namespace(database)) == ["course_a", "course_b", "physical_design"]
