"""Load and validate a domain-neutral JSON glossary."""

import json
import unicodedata
from pathlib import Path
from typing import Any

from app.models import Glossary, GlossaryTerm


class GlossaryError(RuntimeError):
    """Raised when glossary content cannot be loaded safely."""


def normalize_glossary_label(value: str) -> str:
    """Normalize a term for duplicate/collision checks, not for display."""
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


class GlossaryLoader:
    """Read one glossary JSON file and create immutable domain objects."""

    SUPPORTED_SCHEMA_VERSION = 1

    def __init__(self, glossary_file: Path) -> None:
        self.glossary_file = glossary_file

    def load(self) -> Glossary:
        if not self.glossary_file.is_file():
            raise GlossaryError(
                f"Glossary not found:\n{self.glossary_file}\n\n"
                "Please place glossary.json next to run.py."
            )
        try:
            payload = json.loads(self.glossary_file.read_bytes())
        except json.JSONDecodeError as exc:
            raise GlossaryError(
                "Invalid JSON in glossary "
                f"at line {exc.lineno}, column {exc.colno}: {exc.msg}"
            ) from exc
        except UnicodeDecodeError as exc:
            raise GlossaryError(
                "Glossary must contain valid UTF-8 JSON: "
                f"invalid byte at position {exc.start}."
            ) from exc
        except OSError as exc:
            raise GlossaryError(
                f"Could not read glossary: {self.glossary_file}\n{exc}"
            ) from exc

        if not isinstance(payload, dict):
            raise GlossaryError("Glossary root must be a JSON object.")
        schema_version = payload.get("schema_version")
        if schema_version != self.SUPPORTED_SCHEMA_VERSION or isinstance(
            schema_version, bool
        ):
            raise GlossaryError(
                'Glossary "schema_version" must be the supported integer value 1.'
            )

        title = self._required_string(payload, "title", "Glossary")
        title_zh = self._required_string(payload, "title_zh", "Glossary")
        description = self._optional_string(payload, "description", "Glossary")
        description_zh = self._optional_string(payload, "description_zh", "Glossary")
        raw_terms = payload.get("terms")
        if not isinstance(raw_terms, list):
            raise GlossaryError('Glossary "terms" must be an array.')

        terms: list[GlossaryTerm] = []
        ids: set[str] = set()
        labels: dict[str, tuple[str, str]] = {}
        for index, raw_term in enumerate(raw_terms, start=1):
            context = f"Glossary term #{index}"
            if not isinstance(raw_term, dict):
                raise GlossaryError(f"{context} must be an object.")
            term_id = self._required_string(raw_term, "id", context)
            context = f'Glossary term "{term_id}"'
            if term_id in ids:
                raise GlossaryError(f'{context}: duplicate field "id".')
            ids.add(term_id)
            term = self._required_string(raw_term, "term", context)
            term_zh = self._required_string(raw_term, "term_zh", context)
            aliases = self._aliases(raw_term, context)
            definition = self._optional_string(raw_term, "definition", context)
            definition_zh = self._optional_string(raw_term, "definition_zh", context)
            category = self._optional_string(raw_term, "category", context)

            own_labels: set[str] = set()
            for field, label in (("term", term), *[("aliases", a) for a in aliases]):
                normalized = normalize_glossary_label(label)
                if normalized in own_labels:
                    raise GlossaryError(
                        f'{context}: field "{field}" duplicates another term or alias '
                        f'after normalization ("{label}").'
                    )
                own_labels.add(normalized)
                previous = labels.get(normalized)
                if previous is not None:
                    previous_id, previous_label = previous
                    raise GlossaryError(
                        f'{context}: field "{field}" value "{label}" collides with '
                        f'glossary term "{previous_id}" label "{previous_label}" '
                        "after normalization."
                    )
                labels[normalized] = (term_id, label)

            terms.append(
                GlossaryTerm(
                    id=term_id,
                    term=term,
                    term_zh=term_zh,
                    aliases=aliases,
                    definition=definition,
                    definition_zh=definition_zh,
                    category=category,
                )
            )

        return Glossary(
            schema_version=schema_version,
            title=title,
            title_zh=title_zh,
            description=description,
            description_zh=description_zh,
            terms=tuple(terms),
        )

    @staticmethod
    def _required_string(raw: dict[str, Any], field: str, context: str) -> str:
        value = raw.get(field)
        if not isinstance(value, str) or not value.strip():
            raise GlossaryError(
                f'{context}: field "{field}" must be a non-empty string.'
            )
        return value.strip()

    @staticmethod
    def _optional_string(
        raw: dict[str, Any], field: str, context: str
    ) -> str | None:
        if field not in raw:
            return None
        value = raw[field]
        if not isinstance(value, str) or not value.strip():
            raise GlossaryError(
                f'{context}: optional field "{field}" must be a non-empty string '
                "when provided."
            )
        return value.strip()

    @staticmethod
    def _aliases(raw: dict[str, Any], context: str) -> tuple[str, ...]:
        aliases = raw.get("aliases", [])
        if not isinstance(aliases, list):
            raise GlossaryError(f'{context}: field "aliases" must be an array.')
        normalized: set[str] = set()
        result: list[str] = []
        for index, alias in enumerate(aliases, start=1):
            if not isinstance(alias, str) or not alias.strip():
                raise GlossaryError(
                    f'{context}: field "aliases" item #{index} must be a '
                    "non-empty string."
                )
            display = alias.strip()
            key = normalize_glossary_label(display)
            if key in normalized:
                raise GlossaryError(
                    f'{context}: field "aliases" contains duplicate value '
                    f'"{display}" after normalization.'
                )
            normalized.add(key)
            result.append(display)
        return tuple(result)
