import copy

import pytest

from app.repositories import GlossaryError, GlossaryLoader
from tests.conftest import write_json

def load(tmp_path, payload):
    return GlossaryLoader(write_json(tmp_path / "glossary.json", payload)).load()


def test_loads_arbitrary_course_into_immutable_objects(tmp_path, statistics_glossary):
    glossary = load(tmp_path, statistics_glossary)

    assert glossary.title == "Statistics Glossary"
    assert glossary.title_zh == "统计学专业词汇"
    assert glossary.terms[0].aliases == ("SD",)
    assert glossary.terms[1].definition.startswith("A hypothesis")
    assert glossary.terms[1].definition_zh is None


@pytest.mark.parametrize("version", [None, 0, 2, True, "1"])
def test_rejects_unsupported_schema_version(tmp_path, statistics_glossary, version):
    statistics_glossary["schema_version"] = version
    with pytest.raises(GlossaryError, match="schema_version"):
        load(tmp_path, statistics_glossary)


def test_rejects_duplicate_id_with_term_context(tmp_path, statistics_glossary):
    statistics_glossary["terms"][1]["id"] = "standard-deviation"
    with pytest.raises(GlossaryError, match='standard-deviation.*duplicate field "id"'):
        load(tmp_path, statistics_glossary)


def test_rejects_normalized_duplicate_canonical_term(tmp_path, statistics_glossary):
    statistics_glossary["terms"][1]["term"] = "  STANDARD   DEVIATION "
    with pytest.raises(GlossaryError, match="collides"):
        load(tmp_path, statistics_glossary)


@pytest.mark.parametrize("aliases", ["SD", [""], [4], ["SD", " sd "]])
def test_rejects_bad_aliases(tmp_path, statistics_glossary, aliases):
    statistics_glossary["terms"][0]["aliases"] = aliases
    with pytest.raises(GlossaryError, match="aliases"):
        load(tmp_path, statistics_glossary)


def test_rejects_alias_collision_between_entries(tmp_path, statistics_glossary):
    statistics_glossary["terms"][1]["aliases"] = ["sd"]
    with pytest.raises(GlossaryError, match='null-hypothesis.*aliases.*collides'):
        load(tmp_path, statistics_glossary)


def test_rejects_empty_translation(tmp_path, statistics_glossary):
    statistics_glossary["terms"][0]["term_zh"] = " "
    with pytest.raises(GlossaryError, match='standard-deviation.*term_zh'):
        load(tmp_path, statistics_glossary)


def test_optional_fields_must_be_nonempty_strings_when_present(
    tmp_path, statistics_glossary
):
    invalid = copy.deepcopy(statistics_glossary)
    invalid["terms"][0]["definition"] = ""
    with pytest.raises(GlossaryError, match='standard-deviation.*definition'):
        load(tmp_path, invalid)

    statistics_glossary["terms"][0]["category"] = "Arbitrary Domain Category"
    glossary = load(tmp_path, statistics_glossary)
    assert glossary.terms[0].category == "Arbitrary Domain Category"
