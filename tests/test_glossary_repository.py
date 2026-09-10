from app.repositories import GlossaryLoader, GlossaryRepository
from tests.conftest import write_json


def test_repository_is_domain_neutral_and_categories_follow_first_appearance(
    tmp_path, statistics_glossary
):
    statistics_glossary["terms"].append(
        {
            "id": "variance",
            "term": "Variance",
            "term_zh": "方差",
            "category": "Descriptive Statistics",
        }
    )
    repository = GlossaryRepository(
        GlossaryLoader(write_json(tmp_path / "glossary.json", statistics_glossary)).load()
    )

    assert repository.categories() == (
        "Descriptive Statistics",
        "Hypothesis Testing",
    )
    assert repository.get("variance").term_zh == "方差"
    assert repository.get("missing") is None
    assert isinstance(repository.all_terms(), tuple)


def test_serialization_returns_independent_json_ready_data(
    tmp_path, statistics_glossary
):
    repository = GlossaryRepository(
        GlossaryLoader(write_json(tmp_path / "glossary.json", statistics_glossary)).load()
    )
    first = repository.to_dict()
    first["terms"][0]["aliases"].append("changed")
    second = repository.to_dict()

    assert second["title"] == "Statistics Glossary"
    assert second["terms"][0]["aliases"] == ["SD"]
