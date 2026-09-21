"""Course deletion: the quarantine/compensate protocol, purge and idempotency.

The protocol under test replaced "delete the directory, then try the database".

    phase 1  move courses/<id> -> courses/.trash/<id>.<stamp>   (atomic, reversible)
    phase 2  one database transaction                          (the commit point)
    phase 3  remove the quarantined directory                  (after the commit)

Every test asserts the three states that matter — database, directory, exit code —
plus the trace the operator would see, because "recoverable" is a claim about what
the next run and the log can tell you.
"""

from __future__ import annotations

import errno
import os
import shutil

import pytest

from scripts import course_tooling, delete_course
from scripts.course_tooling import ISOLATED_DIRECTORY
from scripts.delete_course import DeleteError, main as delete_main
from tests.test_course_scripts import (
    A,
    B,
    _scalar,
    _seeded_two_course_world,
    _snapshot,
    cli_common,
)


def _trash(courses_dir):
    return courses_dir / ISOLATED_DIRECTORY


def _quarantined(courses_dir):
    directory = _trash(courses_dir)
    if not directory.is_dir():
        return []
    return sorted(entry for entry in directory.iterdir() if entry.is_dir())


def _delete(courses_dir, database, *extra):
    """Run the deletion for course A with the usual CLI plumbing."""
    return delete_main(
        ["--course", A, "--force", *cli_common(courses_dir, database), *extra]
    )


def test_successful_delete_removes_rows_directory_and_quarantine(tmp_path, capsys):
    """The happy path leaves nothing behind, in either store."""
    app, courses_dir, database = _seeded_two_course_world(tmp_path)
    del app
    before_b = _snapshot(database, B)

    assert _delete(courses_dir, database) == 0
    out = capsys.readouterr().out
    assert "已隔离课程目录" in out
    assert (courses_dir / A).exists() is False
    assert _quarantined(courses_dir) == []
    assert (courses_dir / B / "course.json").is_file()
    assert _snapshot(database, A) == dict.fromkeys(_snapshot(database, A), 0)
    assert _snapshot(database, B) == before_b
    # A verified timestamped backup was taken before anything moved.
    assert list(tmp_path.glob("mcq.db.bak-*"))


def test_cleanup_failure_keeps_the_quarantine_and_exits_three(
    tmp_path, monkeypatch, capsys
):
    """A failed phase 3 must not undo the commit, but must not look like success."""
    app, courses_dir, database = _seeded_two_course_world(tmp_path)
    del app

    def refuse(path, *args, **kwargs):  # noqa: ANN001
        raise OSError(errno.EACCES, "permission denied")

    monkeypatch.setattr(shutil, "rmtree", refuse)
    assert _delete(courses_dir, database) == 3

    err = capsys.readouterr().err
    assert "数据库变更已提交" in err and "--purge" in err
    # The database change stands; the course directory is only in quarantine.
    assert _snapshot(database, A) == dict.fromkeys(_snapshot(database, A), 0)
    assert (courses_dir / A).exists() is False
    entries = _quarantined(courses_dir)
    assert len(entries) == 1
    assert (entries[0] / "course.json").is_file(), "the content must stay recoverable"


def test_database_failure_restores_the_directory_and_reports_compensation(
    tmp_path, monkeypatch, capsys
):
    """Phase 2 failing is fully compensated: the course looks untouched."""
    app, courses_dir, database = _seeded_two_course_world(tmp_path)
    del app
    before_a = _snapshot(database, A)
    manifest = (courses_dir / A / "course.json").read_bytes()

    def explode(*args, **kwargs):  # noqa: ANN001
        raise DeleteError("simulated constraint violation")

    monkeypatch.setattr(delete_course, "delete_namespace", explode)
    assert _delete(courses_dir, database) == 1

    err = capsys.readouterr().err
    assert "已回滚并恢复课程目录" in err
    assert (courses_dir / A / "course.json").read_bytes() == manifest
    assert _quarantined(courses_dir) == []
    assert _snapshot(database, A) == before_a


def test_failed_compensation_exits_three_and_names_both_paths(
    tmp_path, monkeypatch, capsys
):
    """When even the restore fails the operator must get exact instructions."""
    app, courses_dir, database = _seeded_two_course_world(tmp_path)
    del app

    def explode(*args, **kwargs):  # noqa: ANN001
        raise DeleteError("simulated constraint violation")

    def restore_fails(entry, root):  # noqa: ANN001
        raise course_tooling.FilesystemTransactionError("simulated restore failure")

    monkeypatch.setattr(delete_course, "delete_namespace", explode)
    monkeypatch.setattr(delete_course, "restore_quarantined_directory", restore_fails)
    assert _delete(courses_dir, database) == 3

    err = capsys.readouterr().err
    assert "需要人工介入" in err
    assert "主操作失败" in err and "补偿失败" in err
    assert "mv " in err
    # The quarantined directory is deliberately kept so nothing is lost.
    assert len(_quarantined(courses_dir)) == 1
    assert (courses_dir / A).exists() is False


def test_directory_rename_failure_leaves_everything_untouched(
    tmp_path, monkeypatch, capsys
):
    """Phase 1 failing is a clean refusal: no database change at all."""
    app, courses_dir, database = _seeded_two_course_world(tmp_path)
    del app
    before_a = _snapshot(database, A)
    manifest = (courses_dir / A / "course.json").read_bytes()

    def crossing_filesystems(source, target, *args, **kwargs):  # noqa: ANN001
        raise course_tooling.FilesystemTransactionError(
            "无法重命名：源与目标不在同一个文件系统（Invalid cross-device link）"
        )

    monkeypatch.setattr(
        delete_course, "quarantine_course_directory", crossing_filesystems
    )
    assert _delete(courses_dir, database) == 1

    err = capsys.readouterr().err
    assert "移动课程目录失败" in err and "数据库未改动" in err
    assert (courses_dir / A / "course.json").read_bytes() == manifest
    assert _snapshot(database, A) == before_a
    assert _quarantined(courses_dir) == []


def test_a_run_interrupted_between_the_phases_is_resumed(tmp_path, capsys):
    """The documented crash state is adopted instead of misread as undeployed."""
    app, courses_dir, database = _seeded_two_course_world(tmp_path)
    del app
    before_b = _snapshot(database, B)

    # What a killed process leaves behind: directory quarantined, database intact.
    trash = course_tooling.isolated_directory(courses_dir)
    entry = trash / f"{A}.20260101T000000Z"
    os.rename(courses_dir / A, entry)
    assert _snapshot(database, A)["courses"] == 1

    assert _delete(courses_dir, database) == 0
    out = capsys.readouterr().out
    assert "接管上次中断留下的隔离目录" in out
    assert _quarantined(courses_dir) == []
    assert (courses_dir / A).exists() is False
    assert _snapshot(database, A) == dict.fromkeys(_snapshot(database, A), 0)
    assert _snapshot(database, B) == before_b


def test_two_quarantine_entries_are_refused_rather_than_guessed(
    tmp_path, capsys
):
    """Ambiguity needs a human; the command must not pick one."""
    app, courses_dir, database = _seeded_two_course_world(tmp_path)
    del app
    before_a = _snapshot(database, A)

    trash = course_tooling.isolated_directory(courses_dir)
    os.rename(courses_dir / A, trash / f"{A}.20260101T000000Z")
    (trash / f"{A}.20260102T000000Z").mkdir()

    assert _delete(courses_dir, database) == 1
    err = capsys.readouterr().err
    assert "无法判断该接管哪一个" in err and "--purge" in err
    assert _snapshot(database, A) == before_a
    assert len(_quarantined(courses_dir)) == 2


def test_directory_and_quarantine_together_are_refused(tmp_path, capsys):
    """Both present means the protocol was not followed: report, do not merge."""
    app, courses_dir, database = _seeded_two_course_world(tmp_path)
    del app
    before_a = _snapshot(database, A)

    trash = course_tooling.isolated_directory(courses_dir)
    shutil.copytree(courses_dir / A, trash / f"{A}.20260101T000000Z")

    assert _delete(courses_dir, database) == 1
    err = capsys.readouterr().err
    assert "同时存在" in err
    assert _snapshot(database, A) == before_a
    assert (courses_dir / A).is_dir()


def test_dry_run_writes_nothing_and_plans_the_quarantine(tmp_path, capsys):
    """``--dry-run`` must leave no trace: no move, no backup, no database change."""
    app, courses_dir, database = _seeded_two_course_world(tmp_path)
    del app
    before_a = _snapshot(database, A)
    manifest = (courses_dir / A / "course.json").read_bytes()

    assert _delete(courses_dir, database, "--dry-run") == 0

    out = capsys.readouterr().out
    assert "--dry-run" in out and ISOLATED_DIRECTORY in out
    assert (courses_dir / A / "course.json").read_bytes() == manifest
    assert _snapshot(database, A) == before_a
    assert _trash(courses_dir).exists() is False
    assert not list(tmp_path.glob("mcq.db.bak-*"))


def test_the_command_is_idempotent_on_a_second_run(tmp_path, capsys):
    """Re-running after success finds an undeployed identity and changes nothing."""
    app, courses_dir, database = _seeded_two_course_world(tmp_path)
    del app
    assert _delete(courses_dir, database) == 0
    capsys.readouterr()
    after = _snapshot(database, A)
    before_b = _snapshot(database, B)

    # The rows are gone and the directory is gone, so this is the "unknown course"
    # branch for a course nobody declared.
    assert _delete(courses_dir, database) == 1
    err = capsys.readouterr().err
    assert "未知课程" in err
    assert _snapshot(database, A) == after
    assert _snapshot(database, B) == before_b
    assert (courses_dir / B / "course.json").is_file()


# ---------------------------------------------------------------------- purge


def _purge(courses_dir, database, *extra):
    return delete_main(
        ["--purge", "--courses-dir", str(courses_dir), "--db", str(database), *extra]
    )


def test_purge_removes_only_quarantine_children(tmp_path, capsys):
    """A file inside the quarantine directory is not a course directory."""
    app, courses_dir, database = _seeded_two_course_world(tmp_path)
    del app
    trash = course_tooling.isolated_directory(courses_dir)
    (trash / f"{A}.20260101T000000Z" / "sub").mkdir(parents=True)
    stray = trash / "NOTES.txt"
    stray.write_text("manual notes", encoding="utf-8")
    outside = courses_dir / "real-course"
    outside.mkdir()

    assert _purge(courses_dir, database) == 0
    out = capsys.readouterr().out
    assert "已清理" in out
    assert _quarantined(courses_dir) == []
    assert stray.read_text(encoding="utf-8") == "manual notes"
    assert outside.is_dir()


def test_purge_dry_run_writes_nothing(tmp_path, capsys):
    app, courses_dir, database = _seeded_two_course_world(tmp_path)
    del app
    trash = course_tooling.isolated_directory(courses_dir)
    entry = trash / f"{A}.20260101T000000Z"
    entry.mkdir()
    (entry / "course.json").write_text("{}", encoding="utf-8")

    assert _purge(courses_dir, database, "--dry-run") == 0
    out = capsys.readouterr().out
    assert "--dry-run" in out and str(entry) in out
    assert entry.is_dir()


def test_purge_can_be_limited_to_one_course(tmp_path, capsys):
    app, courses_dir, database = _seeded_two_course_world(tmp_path)
    del app
    trash = course_tooling.isolated_directory(courses_dir)
    mine = trash / f"{A}.20260101T000000Z"
    other = trash / f"{B}.20260101T000000Z"
    mine.mkdir()
    other.mkdir()

    assert _purge(courses_dir, database, "--course", A) == 0
    capsys.readouterr()
    assert mine.exists() is False
    assert other.is_dir()


def test_purge_on_an_empty_quarantine_is_a_no_op(tmp_path, capsys):
    app, courses_dir, database = _seeded_two_course_world(tmp_path)
    del app
    assert _purge(courses_dir, database) == 0
    assert "没有" in capsys.readouterr().out


def test_purge_reports_entries_it_could_not_remove(tmp_path, monkeypatch, capsys):
    app, courses_dir, database = _seeded_two_course_world(tmp_path)
    del app
    trash = course_tooling.isolated_directory(courses_dir)
    (trash / f"{A}.20260101T000000Z").mkdir()

    def refuse(path, *args, **kwargs):  # noqa: ANN001
        raise OSError(errno.EACCES, "permission denied")

    monkeypatch.setattr(shutil, "rmtree", refuse)
    assert _purge(courses_dir, database) == 1
    err = capsys.readouterr().err
    assert "无法删除" in err and str(trash / f"{A}.20260101T000000Z") in err


def test_purge_never_touches_a_path_outside_the_quarantine(tmp_path, capsys):
    """A symlink inside the quarantine directory must not become an escape hatch."""
    app, courses_dir, database = _seeded_two_course_world(tmp_path)
    del app
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    trash = course_tooling.isolated_directory(courses_dir)
    (trash / f"{A}.20260101T000000Z").symlink_to(outside)

    assert _purge(courses_dir, database) == 0
    capsys.readouterr()
    assert marker.read_text(encoding="utf-8") == "keep"


def test_course_is_required_without_purge(tmp_path, capsys):
    app, courses_dir, database = _seeded_two_course_world(tmp_path)
    del app
    assert (
        delete_main(
            ["--courses-dir", str(courses_dir), "--db", str(database)]
        )
        == 2
    )
    assert "--course 必填" in capsys.readouterr().err
