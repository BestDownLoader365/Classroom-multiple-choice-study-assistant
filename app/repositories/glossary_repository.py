"""Read-only access to the startup-loaded course glossary."""

from types import MappingProxyType
from typing import Any

from app.models import Glossary, GlossaryTerm


class GlossaryRepository:
    """Expose immutable glossary content without knowing its subject domain."""

    def __init__(self, glossary: Glossary) -> None:
        self._glossary = glossary
        self._terms = glossary.terms
        self._by_id = MappingProxyType({term.id: term for term in self._terms})
        self._categories = tuple(
            dict.fromkeys(term.category for term in self._terms if term.category)
        )
        self._serialized = {
            "schema_version": glossary.schema_version,
            "title": glossary.title,
            "title_zh": glossary.title_zh,
            "description": glossary.description,
            "description_zh": glossary.description_zh,
            "terms": [self._serialize_term(term) for term in self._terms],
        }

    @property
    def glossary(self) -> Glossary:
        """Return the immutable glossary this repository was built from."""
        return self._glossary

    @property
    def title(self) -> str:
        return self._glossary.title

    @property
    def title_zh(self) -> str:
        return self._glossary.title_zh

    @property
    def description(self) -> str | None:
        return self._glossary.description

    @property
    def description_zh(self) -> str | None:
        return self._glossary.description_zh

    def all_terms(self) -> tuple[GlossaryTerm, ...]:
        return self._terms

    def get(self, term_id: str) -> GlossaryTerm | None:
        return self._by_id.get(term_id)

    def categories(self) -> tuple[str, ...]:
        """Return unique categories in stable first-appearance order."""
        return self._categories

    def to_dict(self) -> dict[str, Any]:
        """Return JSON-ready data without exposing mutable repository internals."""
        terms = []
        for stored in self._serialized["terms"]:
            term = dict(stored)
            term["aliases"] = list(stored["aliases"])
            terms.append(term)
        return {**self._serialized, "terms": terms}

    @staticmethod
    def _serialize_term(term: GlossaryTerm) -> dict[str, Any]:
        return {
            "id": term.id,
            "term": term.term,
            "term_zh": term.term_zh,
            "aliases": list(term.aliases),
            "definition": term.definition,
            "definition_zh": term.definition_zh,
            "category": term.category,
        }
