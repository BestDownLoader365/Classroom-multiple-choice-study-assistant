"""In-memory access to questions loaded at process startup."""

from collections.abc import Iterable

from app.models import Chapter, Question, SourceDocument


LEGACY_SOURCE = SourceDocument(
    id="legacy", title="Uncategorized course material", lecture="Legacy question bank"
)
LEGACY_CHAPTER = Chapter(
    id="legacy", source_id="legacy", title="Uncategorized", order=1
)


class QuestionRepository:
    """Keep the process-wide, read-only question collection."""

    def __init__(
        self,
        questions: list[Question],
        title: str = "MCQ Practice",
        title_zh: str = "",
        sources: tuple[SourceDocument, ...] = (),
        chapters: tuple[Chapter, ...] = (),
    ) -> None:
        self._questions = tuple(questions)
        self._by_id = {question.id: question for question in questions}
        self.title = title
        self.title_zh = title_zh
        self._sources = sources or (LEGACY_SOURCE,)
        self._chapters = chapters or (LEGACY_CHAPTER,)
        self._source_by_id = {source.id: source for source in self._sources}
        self._chapter_by_id = {chapter.id: chapter for chapter in self._chapters}

    def get_all(self) -> list[Question]:
        """Return all questions in their original JSON order."""
        return list(self._questions)

    def get_by_id(self, question_id: str) -> Question | None:
        """Return one question, if it exists."""
        return self._by_id.get(question_id)

    def get_by_ids(self, question_ids: Iterable[str]) -> list[Question]:
        """Return existing questions in the requested order."""
        return [
            question
            for question_id in question_ids
            if (question := self._by_id.get(question_id)) is not None
        ]

    def get_sources(self) -> list[SourceDocument]:
        """Return source documents in catalogue order."""
        return list(self._sources)

    def get_chapters(self, source_id: str | None = None) -> list[Chapter]:
        """Return chapters in curriculum order, optionally for one source."""
        chapters = [
            chapter
            for chapter in self._chapters
            if source_id is None or chapter.source_id == source_id
        ]
        return sorted(chapters, key=lambda chapter: (chapter.order, chapter.title))

    def get_source(self, source_id: str) -> SourceDocument | None:
        return self._source_by_id.get(source_id)

    def get_chapter(self, chapter_id: str) -> Chapter | None:
        return self._chapter_by_id.get(chapter_id)

    def get_filtered(
        self,
        *,
        chapter_ids: set[str] | None = None,
        source_ids: set[str] | None = None,
    ) -> list[Question]:
        """Filter questions by catalogue IDs while preserving bank order."""
        return [
            question
            for question in self._questions
            if (
                chapter_ids is None
                or not chapter_ids.isdisjoint(question.chapter_ids)
            )
            and (source_ids is None or question.source_id in source_ids)
        ]

    def question_count_for_chapter(self, chapter_id: str) -> int:
        return sum(chapter_id in question.chapter_ids for question in self._questions)
