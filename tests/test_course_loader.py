"""Course manifest discovery, validation, loading, and the worker registry."""

import json

import pytest

from app.models import LEGACY_COURSE_ID, CourseDefinitionError, CourseLoadError
from app.repositories import CourseLoader
from tests.conftest import (
    course_bank,
    course_glossary,
    write_course,
    write_json,
)


def loader_for(tmp_path, legacy: bool = False) -> CourseLoader:
    courses_dir = tmp_path / "courses"
    legacy_dir = tmp_path if legacy else tmp_path / "absent"
    return CourseLoader(
        courses_dir,
        legacy_directory=legacy_dir,
        legacy_questions_name="questions.json",
        legacy_glossary_name="glossary.json",
    )


def test_two_courses_load_with_identical_local_ids(tmp_path):
    """A and B may both own ``q001``/``chapter_1``/``source_1`` independently."""
    courses_dir = tmp_path / "courses"
    write_course(courses_dir, "course_a", course_bank(("a", "alpha")))
    write_course(courses_dir, "course_b", course_bank(("b", "beta")))
    loader = loader_for(tmp_path)

    definitions = {item.course_id: item for item in loader.discover_definitions()}

    assert set(definitions) == {"course_a", "course_b"}
    bundle_a = loader.load_bundle(definitions["course_a"])
    bundle_b = loader.load_bundle(definitions["course_b"])
    assert bundle_a.course_id == "course_a" and bundle_b.course_id == "course_b"
    assert [q.id for q in bundle_a.questions] == ["q001", "q002"]
    assert [q.id for q in bundle_b.questions] == ["q001", "q002"]
    # Same IDs, different content and different correct answers.
    assert bundle_a.question_repository.get_by_id("q001").correct_answers == ("a",)
    assert bundle_b.question_repository.get_by_id("q001").correct_answers == ("b",)
    assert (
        bundle_a.question_repository.get_by_id("q002").text
        != bundle_b.question_repository.get_by_id("q002").text
    )
    # Chapter/source IDs are course-local, not global.
    assert bundle_a.question_repository.get_chapter("chapter_1") is not None
    assert bundle_b.question_repository.get_chapter("chapter_1") is not None


def test_duplicate_course_id_is_a_global_assembly_error(tmp_path):
    courses_dir = tmp_path / "courses"
    write_course(courses_dir, "course_a", course_bank())
    duplicate = courses_dir / "course_a_copy"
    duplicate.mkdir(parents=True)
    write_json(duplicate / "questions.json", course_bank())
    write_json(
        duplicate / "course.json",
        {
            "schema_version": 1,
            "course_id": "course_a",
            "title": "Duplicate",
            "enabled": True,
            "questions": "questions.json",
            "glossary": None,
        },
    )

    with pytest.raises(CourseDefinitionError, match="Duplicate course_id"):
        loader_for(tmp_path).discover_definitions()


@pytest.mark.parametrize(
    "manifest, message",
    [
        ({"schema_version": 2, "course_id": "c", "title": "t", "enabled": True,
          "questions": "questions.json", "glossary": None}, "schema_version"),
        ({"schema_version": 1, "course_id": "Bad Case", "title": "t", "enabled": True,
          "questions": "questions.json", "glossary": None}, "course_id"),
        ({"schema_version": 1, "course_id": "c", "title": "", "enabled": True,
          "questions": "questions.json", "glossary": None}, "title"),
        ({"schema_version": 1, "course_id": "c", "title": "t", "enabled": "yes",
          "questions": "questions.json", "glossary": None}, "enabled"),
        ({"schema_version": 1, "course_id": "c", "title": "t", "enabled": True,
          "glossary": None}, "questions"),
        ({"schema_version": 1, "course_id": "c", "title": "t", "enabled": True,
          "questions": "questions.json"}, "glossary"),
    ],
)
def test_bad_manifest_is_rejected(tmp_path, manifest, message):
    courses_dir = tmp_path / "courses"
    root = courses_dir / "c"
    root.mkdir(parents=True)
    write_json(root / "questions.json", course_bank())
    write_json(root / "course.json", manifest)

    with pytest.raises(CourseDefinitionError, match=message):
        loader_for(tmp_path).discover_definitions()


@pytest.mark.parametrize(
    "declared",
    ["../../etc/passwd", "/etc/passwd", "nested/../../outside.json"],
)
def test_path_escape_is_rejected(tmp_path, declared):
    courses_dir = tmp_path / "courses"
    write_course(courses_dir, "course_a", course_bank())
    manifest = json.loads((courses_dir / "course_a" / "course.json").read_text())
    manifest["questions"] = declared
    write_json(courses_dir / "course_a" / "course.json", manifest)

    with pytest.raises(CourseDefinitionError, match="questions"):
        loader_for(tmp_path).discover_definitions()


def test_missing_questions_file_is_a_course_scoped_failure(tmp_path):
    courses_dir = tmp_path / "courses"
    write_course(courses_dir, "course_a", course_bank())
    (courses_dir / "course_a" / "questions.json").unlink()
    loader = loader_for(tmp_path)

    with pytest.raises(CourseLoadError) as excinfo:
        loader.load_bundle(loader.find_definition("course_a"))
    assert excinfo.value.course_id == "course_a"


def test_declared_missing_glossary_is_a_failure_not_an_absent_glossary(tmp_path):
    courses_dir = tmp_path / "courses"
    write_course(courses_dir, "course_a", course_bank(), glossary=course_glossary("a"))
    (courses_dir / "course_a" / "glossary.json").unlink()
    loader = loader_for(tmp_path)

    with pytest.raises(CourseLoadError, match="declared glossary not found"):
        loader.load_bundle(loader.find_definition("course_a"))


def test_glossary_null_means_explicitly_no_glossary(tmp_path):
    courses_dir = tmp_path / "courses"
    write_course(courses_dir, "course_a", course_bank())
    loader = loader_for(tmp_path)

    bundle = loader.load_bundle(loader.find_definition("course_a"))

    assert bundle.glossary_repository is None
    assert bundle.glossary() is None


def test_disabled_course_is_discovered_but_not_loaded(tmp_path):
    courses_dir = tmp_path / "courses"
    write_course(courses_dir, "course_a", course_bank())
    write_course(courses_dir, "course_b", course_bank(), enabled=False)
    loader = loader_for(tmp_path)

    all_ids = [item.course_id for item in loader.discover_definitions()]
    enabled_ids = [item.course_id for item in loader.enabled_definitions()]

    assert all_ids == ["course_a", "course_b"]
    assert enabled_ids == ["course_a"]


def test_legacy_root_files_become_one_virtual_course(tmp_path):
    write_json(tmp_path / "questions.json", course_bank())
    write_json(tmp_path / "glossary.json", course_glossary("legacy"))
    loader = loader_for(tmp_path, legacy=True)

    definitions = loader.discover_definitions()

    assert [item.course_id for item in definitions] == [LEGACY_COURSE_ID]
    assert definitions[0].layout == "legacy"
    bundle = loader.load_bundle(definitions[0])
    assert len(bundle.questions) == 2
    assert bundle.glossary_repository is not None


def test_legacy_adapter_without_glossary_yields_no_glossary(tmp_path):
    write_json(tmp_path / "questions.json", course_bank())
    loader = loader_for(tmp_path, legacy=True)

    bundle = loader.load_bundle(loader.discover_definitions()[0])

    assert bundle.glossary_repository is None


def test_publication_identity_changes_when_content_changes(tmp_path):
    """The identity re-check that closes the startup/publication race."""
    courses_dir = tmp_path / "courses"
    write_course(courses_dir, "course_a", course_bank())
    loader = loader_for(tmp_path)
    definition = loader.find_definition("course_a")
    first = loader.publication_identity(definition)

    changed = course_bank(("c", "d"))
    write_json(courses_dir / "course_a" / "questions.json", changed)

    assert loader.publication_identity(definition) != first
    assert loader.publication_identity(definition) != first

