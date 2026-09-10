"""Answer validation and grading rules."""

from collections.abc import Iterable

from app.models import Question


class AnswerValidationError(ValueError):
    """Raised when an answer contains an option not owned by the question."""


class GradingService:
    """Grade both choice types using option identifiers from the JSON file."""

    def normalize(
        self, question: Question, selected_answers: Iterable[str]
    ) -> tuple[str, ...]:
        """Validate option IDs and remove duplicates while preserving order."""
        valid_ids = {option.id for option in question.options}
        normalized: list[str] = []
        seen: set[str] = set()
        for answer in selected_answers:
            if answer not in valid_ids:
                raise AnswerValidationError(
                    f'Option "{answer}" does not belong to question "{question.id}".'
                )
            if answer not in seen:
                normalized.append(answer)
                seen.add(answer)
        return tuple(normalized)

    def grade(self, question: Question, selected_answers: list[str]) -> bool:
        """Return whether the validated selection exactly matches the answer."""
        normalized = self.normalize(question, selected_answers)
        selected = set(normalized)
        correct = set(question.correct_answers)
        if question.question_type == "single" and len(selected) > 1:
            return False
        return selected == correct
