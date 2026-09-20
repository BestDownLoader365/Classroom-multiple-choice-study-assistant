"""Load and validate the JSON question bank."""

import hashlib
import json
from pathlib import Path
from typing import Any

from app.models import (
    LEGACY_CHAPTER,
    LEGACY_SOURCE,
    Chapter,
    Option,
    Question,
    SourceDocument,
)


class QuestionBankError(RuntimeError):
    """Raised when the question bank cannot be loaded safely."""


class QuestionLoader:
    """Read one JSON file and convert it into immutable questions."""

    def __init__(self, question_file: Path) -> None:
        self.question_file = question_file
        self.source_fingerprint: str | None = None
        self.title = "MCQ Practice"
        self.title_zh = ""
        self.schema_version = 1
        self.sources: tuple[SourceDocument, ...] = ()
        self.chapters: tuple[Chapter, ...] = ()

    def load(self) -> list[Question]:
        """Load and validate the complete question bank once."""
        if not self.question_file.is_file():
            raise QuestionBankError(
                f"Question bank not found:\n{self.question_file}\n\n"
                "Please place questions.json next to run.py."
            )

        try:
            raw_content = self.question_file.read_bytes()
            self.source_fingerprint = hashlib.sha256(raw_content).hexdigest()
            payload = json.loads(raw_content)
        except json.JSONDecodeError as exc:
            raise QuestionBankError(
                "Invalid JSON in question bank "
                f"at line {exc.lineno}, column {exc.colno}: {exc.msg}"
            ) from exc
        except UnicodeDecodeError as exc:
            raise QuestionBankError(
                "Question bank must contain valid UTF-8 JSON: "
                f"invalid byte at position {exc.start}."
            ) from exc
        except OSError as exc:
            raise QuestionBankError(
                f"Could not read question bank: {self.question_file}\n{exc}"
            ) from exc

        if not isinstance(payload, dict):
            raise QuestionBankError("Question bank root must be a JSON object.")
        schema_version = payload.get("schema_version", 1)
        if (
            not isinstance(schema_version, int)
            or isinstance(schema_version, bool)
            or schema_version < 1
        ):
            raise QuestionBankError(
                'Question bank "schema_version" must be a positive integer.'
            )
        self.schema_version = schema_version
        title = payload.get("title", self.title)
        title_zh = payload.get("title_zh", "")
        if not isinstance(title, str) or not title.strip():
            raise QuestionBankError('Question bank "title" must be a non-empty string.')
        if not isinstance(title_zh, str) or (
            "title_zh" in payload and not title_zh.strip()
        ):
            raise QuestionBankError(
                'Question bank "title_zh" must be a non-empty string when provided.'
            )
        self.title = title.strip()
        self.title_zh = title_zh.strip()
        self.sources, self.chapters = self._parse_catalogue(payload)
        raw_questions = payload.get("questions")
        if not isinstance(raw_questions, list):
            raise QuestionBankError(
                'Question bank must contain a "questions" array.'
            )

        questions: list[Question] = []
        seen_ids: set[str] = set()
        for index, raw_question in enumerate(raw_questions, start=1):
            question = self._parse_question(
                raw_question,
                index,
                require_metadata=bool(self.sources or self.chapters),
            )
            if question.id in seen_ids:
                raise QuestionBankError(
                    f'Duplicate question id "{question.id}".'
                )
            seen_ids.add(question.id)
            questions.append(question)
        self._validate_question_references(questions)
        if not self.sources:
            # A bank without a catalogue gets the shared legacy defaults.
            self.sources = (LEGACY_SOURCE,)
            self.chapters = (LEGACY_CHAPTER,)
        return questions

    def _parse_question(
        self, raw: Any, index: int, *, require_metadata: bool
    ) -> Question:
        if not isinstance(raw, dict):
            raise QuestionBankError(f"Question #{index} must be an object.")

        question_id = self._required_string(raw, "id", f"Question #{index}")
        context = f'Question "{question_id}"'
        text = self._required_string(raw, "text", context)
        question_type = raw.get("type")
        if question_type not in {"single", "multiple"}:
            raise QuestionBankError(
                f'{context}: type must be "single" or "multiple".'
            )

        raw_options = raw.get("options")
        if not isinstance(raw_options, list) or len(raw_options) < 2:
            raise QuestionBankError(f"{context}: options must contain at least two items.")

        options: list[Option] = []
        option_ids: set[str] = set()
        for option_index, raw_option in enumerate(raw_options, start=1):
            option = self._parse_option(raw_option, context, option_index)
            if option.id in option_ids:
                raise QuestionBankError(
                    f'{context}: duplicate option id "{option.id}".'
                )
            option_ids.add(option.id)
            options.append(option)

        raw_answers = raw.get("correct_answers")
        if not isinstance(raw_answers, list) or not raw_answers:
            raise QuestionBankError(
                f"{context}: correct_answers must contain at least one option id."
            )
        if not all(isinstance(answer, str) and answer for answer in raw_answers):
            raise QuestionBankError(
                f"{context}: every correct answer must be a non-empty string."
            )
        if len(raw_answers) != len(set(raw_answers)):
            raise QuestionBankError(f"{context}: correct_answers contains duplicates.")
        if question_type == "single" and len(raw_answers) != 1:
            raise QuestionBankError(
                f"{context}: a single-choice question must have exactly one correct answer."
            )
        for answer in raw_answers:
            if answer not in option_ids:
                raise QuestionBankError(
                    f'{context}: correct answer "{answer}" does not exist in options.'
                )

        explanation = self._optional_string(raw, "explanation", context)
        text_zh = self._optional_string(raw, "text_zh", context)
        explanation_zh = self._optional_string(raw, "explanation_zh", context)
        if require_metadata:
            source_id = self._required_string(raw, "source_id", context)
            chapter_ids = self._parse_chapter_ids(raw, context)
        else:
            source_id = "legacy"
            chapter_ids = ("legacy",)
        section = self._optional_string(raw, "section", context)
        raw_pages = raw.get("pages", [])
        if not isinstance(raw_pages, list) or not all(
            isinstance(page, int) and not isinstance(page, bool) and page >= 1
            for page in raw_pages
        ):
            raise QuestionBankError(
                f'{context}: "pages" must be an array of positive integers.'
            )
        if len(raw_pages) != len(set(raw_pages)):
            raise QuestionBankError(f'{context}: "pages" contains duplicates.')

        return Question(
            id=question_id,
            text=text,
            question_type=question_type,
            options=tuple(options),
            correct_answers=tuple(raw_answers),
            explanation=explanation,
            text_zh=text_zh,
            explanation_zh=explanation_zh,
            source_id=source_id,
            chapter_ids=chapter_ids,
            section=section,
            pages=tuple(raw_pages),
        )

    def _parse_chapter_ids(
        self, raw: dict[str, Any], context: str
    ) -> tuple[str, ...]:
        """Read the plural catalogue field while accepting legacy singular banks."""
        has_plural = "chapter_ids" in raw
        has_singular = "chapter_id" in raw
        if has_plural and has_singular:
            raise QuestionBankError(
                f'{context}: provide either "chapter_ids" or legacy '
                '"chapter_id", not both.'
            )
        if has_singular:
            return (self._required_string(raw, "chapter_id", context),)

        values = raw.get("chapter_ids")
        if not isinstance(values, list) or not values:
            raise QuestionBankError(
                f'{context}: "chapter_ids" must be a non-empty array.'
            )
        if not all(isinstance(value, str) and value.strip() for value in values):
            raise QuestionBankError(
                f'{context}: every chapter id must be a non-empty string.'
            )
        if len(values) != len(set(values)):
            raise QuestionBankError(f'{context}: "chapter_ids" contains duplicates.')
        return tuple(values)

    def _parse_catalogue(
        self, payload: dict[str, Any]
    ) -> tuple[tuple[SourceDocument, ...], tuple[Chapter, ...]]:
        raw_sources = payload.get("sources")
        raw_chapters = payload.get("chapters")
        if raw_sources is None and raw_chapters is None:
            return (), ()
        if not isinstance(raw_sources, list) or not raw_sources:
            raise QuestionBankError('Question bank "sources" must be a non-empty array.')
        if not isinstance(raw_chapters, list) or not raw_chapters:
            raise QuestionBankError('Question bank "chapters" must be a non-empty array.')

        sources: list[SourceDocument] = []
        source_ids: set[str] = set()
        for index, raw in enumerate(raw_sources, start=1):
            context = f"Source #{index}"
            if not isinstance(raw, dict):
                raise QuestionBankError(f"{context} must be an object.")
            source_id = self._required_string(raw, "id", context)
            if source_id in source_ids:
                raise QuestionBankError(f'Duplicate source id "{source_id}".')
            source_ids.add(source_id)
            sources.append(
                SourceDocument(
                    id=source_id,
                    title=self._required_string(raw, "title", context),
                    filename=self._optional_string(raw, "filename", context),
                    lecture=self._optional_string(raw, "lecture", context),
                )
            )

        chapters: list[Chapter] = []
        chapter_ids: set[str] = set()
        for index, raw in enumerate(raw_chapters, start=1):
            context = f"Chapter #{index}"
            if not isinstance(raw, dict):
                raise QuestionBankError(f"{context} must be an object.")
            chapter_id = self._required_string(raw, "id", context)
            if chapter_id in chapter_ids:
                raise QuestionBankError(f'Duplicate chapter id "{chapter_id}".')
            chapter_ids.add(chapter_id)
            source_id = self._required_string(raw, "source_id", context)
            if source_id not in source_ids:
                raise QuestionBankError(
                    f'{context}: source_id "{source_id}" does not exist.'
                )
            order = raw.get("order", index)
            if not isinstance(order, int) or isinstance(order, bool) or order < 0:
                raise QuestionBankError(
                    f'{context}: "order" must be a non-negative integer.'
                )
            chapters.append(
                Chapter(
                    id=chapter_id,
                    source_id=source_id,
                    title=self._required_string(raw, "title", context),
                    order=order,
                )
            )
        return tuple(sources), tuple(chapters)

    def _validate_question_references(self, questions: list[Question]) -> None:
        if not self.sources:
            return
        source_ids = {source.id for source in self.sources}
        chapter_by_id = {chapter.id: chapter for chapter in self.chapters}
        for question in questions:
            if question.source_id not in source_ids:
                raise QuestionBankError(
                    f'Question "{question.id}": source_id "{question.source_id}" does not exist.'
                )
            for chapter_id in question.chapter_ids:
                chapter = chapter_by_id.get(chapter_id)
                if chapter is None:
                    raise QuestionBankError(
                        f'Question "{question.id}": chapter_id "{chapter_id}" does not exist.'
                    )
                if chapter.source_id != question.source_id:
                    raise QuestionBankError(
                        f'Question "{question.id}": chapter "{chapter.id}" belongs to a different source.'
                    )

    @staticmethod
    def _parse_option(raw: Any, context: str, index: int) -> Option:
        if not isinstance(raw, dict):
            raise QuestionBankError(f"{context}: option #{index} must be an object.")
        option_id = QuestionLoader._required_string(
            raw, "id", f"{context}, option #{index}"
        )
        text = QuestionLoader._required_string(
            raw, "text", f"{context}, option \"{option_id}\""
        )
        text_zh = QuestionLoader._optional_string(
            raw, "text_zh", f'{context}, option "{option_id}"'
        )
        return Option(id=option_id, text=text, text_zh=text_zh)

    @staticmethod
    def _required_string(raw: dict[str, Any], field: str, context: str) -> str:
        value = raw.get(field)
        if not isinstance(value, str) or not value.strip():
            raise QuestionBankError(
                f'{context}: "{field}" must be a non-empty string.'
            )
        return value

    @staticmethod
    def _optional_string(raw: dict[str, Any], field: str, context: str) -> str:
        value = raw.get(field, "")
        if not isinstance(value, str):
            raise QuestionBankError(f'{context}: "{field}" must be a string.')
        return value.strip()
