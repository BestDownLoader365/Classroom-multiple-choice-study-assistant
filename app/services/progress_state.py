"""Validation and helpers for the resumable per-mode quiz progress payload.

The progress state is a JSON-serializable dict stored server-side per
(learner, mode). These pure helpers describe and validate that shape so the
HTTP layer does not have to. They are deliberately free of Flask imports so
they can be unit-tested in isolation.
"""

from typing import Any

from app.models import QuizMode

from .quiz_service import ORIGINAL_CORRECTION, TRANSFER_VERIFICATION


def session_key(mode: QuizMode) -> str:
    """Return the in-request storage key for one mode's progress state."""
    return f"quiz_progress_{mode.value}"


def quiz_limit(raw_size: str) -> int | None:
    """Map the requested quiz size to a question limit (``None`` means all)."""
    return {"10": 10, "20": 20, "50": 50, "all": None}.get(raw_size, 20)


def is_nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def is_valid_progress_state(state: Any, mode: QuizMode) -> bool:
    """Reject incomplete or stale session structures before routes use them."""
    if not isinstance(state, dict) or state.get("mode") != mode.value:
        return False

    question_ids = state.get("question_ids")
    current_index = state.get("current_index")
    if not isinstance(question_ids, list) or not all(
        isinstance(question_id, str) for question_id in question_ids
    ):
        return False
    if not is_nonnegative_int(current_index) or current_index > len(question_ids):
        return False

    for field in (
        "correct_count",
        "incorrect_count",
        "initial_question_count",
    ):
        if not is_nonnegative_int(state.get(field)):
            return False

    if mode is QuizMode.REVIEW:
        for field in ("corrected_count", "knowledge_completed_count"):
            if not is_nonnegative_int(state.get(field)):
                return False
        review_items = state.get("review_items")
        if (
            not isinstance(review_items, list)
            or len(review_items) != len(question_ids)
        ):
            return False
        for item, question_id in zip(review_items, question_ids):
            if not isinstance(item, dict) or item.get("question_id") != question_id:
                return False
            if item.get("role") not in {
                ORIGINAL_CORRECTION,
                TRANSFER_VERIFICATION,
            }:
                return False
            chapter_id = item.get("chapter_id")
            if chapter_id is not None and not isinstance(chapter_id, str):
                return False
        if not isinstance(state.get("review_shortages", []), list):
            return False

    if state.get("status") not in {"pending", "answered"}:
        return False
    if not isinstance(state.get("answer_token"), str) or not state["answer_token"]:
        return False
    if not isinstance(state.get("option_seed"), str) or not state["option_seed"]:
        return False
    if not isinstance(state.get("requested_size"), str):
        return False

    for field in ("chapter_ids", "source_ids"):
        values = state.get(field, [])
        if not isinstance(values, list) or not all(
            isinstance(value, str) for value in values
        ):
            return False

    if state["status"] == "answered":
        feedback = state.get("feedback")
        if current_index >= len(question_ids) or not isinstance(feedback, dict):
            return False
        if not isinstance(feedback.get("is_correct"), bool):
            return False
        selected_answers = feedback.get("selected_answers")
        if not isinstance(selected_answers, list) or not all(
            isinstance(answer, str) for answer in selected_answers
        ):
            return False
    return True