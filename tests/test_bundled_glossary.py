from app.repositories import GlossaryLoader, GlossaryRepository
from tests.conftest import bundled_glossary_file


def test_bundled_glossary_is_valid_and_has_no_label_collisions():
    glossary = GlossaryLoader(bundled_glossary_file()).load()
    repository = GlossaryRepository(glossary)

    assert len(glossary.terms) >= 150
    assert all(term.id and term.term and term.term_zh for term in glossary.terms)
    assert len(repository.categories()) >= 5
