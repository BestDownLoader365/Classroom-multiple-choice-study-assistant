"""Validation and helpers for the resumable per-mode quiz progress payload.

The progress state is a JSON-serializable dict stored server-side per
(learner, mode). These pure helpers describe and validate that shape so the
HTTP layer does not have to. They are deliberately free of Flask imports so
they can be unit-tested in isolation.
"""

import secrets
from collections.abc import Callable
from typing import Any

from app.models import QuizMode

from .quiz_service import ORIGINAL_CORRECTION, SRS_REVIEW, TRANSFER_VERIFICATION

# Modes whose rounds are persisted in ``quiz_progress``. Mock exams keep
# their own server-side state and deliberately stay out of this cycle.
PRACTICE_MODES: tuple[QuizMode, ...] = (QuizMode.NORMAL, QuizMode.REVIEW)


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
    if not _has_valid_core_fields(state):
        return False
    if mode is QuizMode.REVIEW and not _has_valid_review_fields(state):
        return False
    return _has_valid_feedback(state)


def _has_valid_core_fields(state: dict[str, Any]) -> bool:
    """Check the round structure shared by every practice mode."""
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
    return True


def _has_valid_review_fields(state: dict[str, Any]) -> bool:
    """Check the correction and reinforcement bookkeeping of a review round."""
    for field in ("corrected_count", "knowledge_completed_count"):
        if not is_nonnegative_int(state.get(field)):
            return False
    question_ids = state["question_ids"]
    review_items = state.get("review_items")
    if not isinstance(review_items, list) or len(review_items) != len(question_ids):
        return False
    for item, question_id in zip(review_items, question_ids):
        if not isinstance(item, dict) or item.get("question_id") != question_id:
            return False
        if item.get("role") not in {
            ORIGINAL_CORRECTION,
            TRANSFER_VERIFICATION,
            SRS_REVIEW,
        }:
            return False
        chapter_id = item.get("chapter_id")
        if chapter_id is not None and not isinstance(chapter_id, str):
            return False
    return isinstance(state.get("review_shortages", []), list)


def _has_valid_feedback(state: dict[str, Any]) -> bool:
    """Check the stored answer feedback of an answered round."""
    if state.get("status") != "answered":
        return True
    feedback = state.get("feedback")
    if state["current_index"] >= len(state["question_ids"]) or not isinstance(
        feedback, dict
    ):
        return False
    if not isinstance(feedback.get("is_correct"), bool):
        return False
    selected_answers = feedback.get("selected_answers")
    return isinstance(selected_answers, list) and all(
        isinstance(answer, str) for answer in selected_answers
    )


def valid_state_for(
    progress: dict[str, Any], mode: QuizMode
) -> dict[str, Any] | None:
    """Return the stored state for ``mode``, dropping it if it is invalid."""
    key = session_key(mode)
    state = progress.get(key)
    if not is_valid_progress_state(state, mode):
        progress.pop(key, None)
        return None
    return state


def reconcile_state(
    state: dict[str, Any],
    mode: QuizMode,
    *,
    is_usable: Callable[[str], bool],
    lookup_correct: Callable[[str], bool | None] | None = None,
    is_live_chapter: Callable[[str], bool] | None = None,
) -> tuple[dict[str, Any] | None, bool]:
    """Remove unusable question IDs from an unfinished round, keeping the rest.

    ``is_usable`` decides whether a queued question can still be answered
    (it exists in the loaded bank and its grading identity is unchanged).
    Removed questions keep their already recorded attempts; only the round
    bookkeeping is adjusted.  ``lookup_correct`` supplies a removed answered
    question's verdict for legacy states without ``question_results`` so the
    round counters can be decremented precisely.  ``is_live_chapter`` prunes
    stale review-shortage entries.  Returns ``(new_state, changed)``; the new
    state is ``None`` when the round has nothing left worth resuming.
    """
    question_ids = state["question_ids"]
    shortages = state.get("review_shortages", [])
    live_shortages = (
        [
            shortage
            for shortage in shortages
            if not isinstance(shortage, dict)
            or is_live_chapter(shortage.get("chapter_id", ""))
        ]
        if is_live_chapter is not None and isinstance(shortages, list)
        else shortages
    )
    if all(is_usable(question_id) for question_id in question_ids):
        if live_shortages == shortages:
            return state, False

    keep = [
        index
        for index, question_id in enumerate(question_ids)
        if is_usable(question_id)
    ]
    kept = set(keep)
    removed = [
        index for index in range(len(question_ids)) if index not in kept
    ]
    current_index = state["current_index"]
    new_state = dict(state)
    new_state["question_ids"] = [question_ids[index] for index in keep]
    if live_shortages != shortages:
        new_state["review_shortages"] = live_shortages

    results = state.get("question_results")
    if not isinstance(results, list) or len(results) != len(question_ids):
        results = None
    if mode is QuizMode.REVIEW:
        review_items = state["review_items"]
        new_state["review_items"] = [review_items[index] for index in keep]
    if results is not None:
        new_state["question_results"] = [results[index] for index in keep]

    def _entry_outcome(index: int) -> tuple[bool, bool, int] | None:
        """Return (correct, corrected, knowledge_completed) for one answered slot."""
        if results is not None and isinstance(results[index], dict):
            entry = results[index]
            return (
                bool(entry.get("correct")),
                bool(entry.get("corrected")),
                int(entry.get("knowledge", 0) or 0),
            )
        if index == current_index and isinstance(state.get("feedback"), dict):
            feedback = state["feedback"]
            knowledge = 0
            for update in feedback.get("knowledge_updates", []):
                if isinstance(update, dict):
                    knowledge += int(bool(update.get("newly_completed")))
            return (
                bool(feedback.get("is_correct")),
                bool(feedback.get("corrected_now")),
                knowledge,
            )
        if lookup_correct is not None:
            correct = lookup_correct(question_ids[index])
            if correct is not None:
                return (bool(correct), False, 0)
        return None

    answered_removed = [
        index for index in removed if index < current_index
    ]
    current_removed = (
        state["status"] == "answered"
        and current_index < len(question_ids)
        and not is_usable(question_ids[current_index])
    )
    if current_removed:
        answered_removed = answered_removed + [current_index]

    correct_delta = incorrect_delta = corrected_delta = knowledge_delta = 0
    for index in answered_removed:
        outcome = _entry_outcome(index)
        if outcome is None:
            continue
        correct, corrected, knowledge = outcome
        correct_delta += int(correct)
        incorrect_delta += int(not correct)
        corrected_delta += int(corrected)
        knowledge_delta += knowledge

    new_state["correct_count"] = max(0, state["correct_count"] - correct_delta)
    new_state["incorrect_count"] = max(
        0, state["incorrect_count"] - incorrect_delta
    )
    if mode is QuizMode.REVIEW:
        new_state["corrected_count"] = max(
            0, state["corrected_count"] - corrected_delta
        )
        new_state["knowledge_completed_count"] = max(
            0, state["knowledge_completed_count"] - knowledge_delta
        )
    new_state["current_index"] = sum(1 for index in keep if index < current_index)
    new_state["initial_question_count"] = max(
        0, state["initial_question_count"] - len(removed)
    )
    fairness = state.get("fairness_remaining_ids")
    if isinstance(fairness, list):
        new_state["fairness_remaining_ids"] = [
            question_id for question_id in fairness if is_usable(question_id)
        ]

    if current_removed:
        new_state["status"] = "pending"
        new_state["answer_token"] = secrets.token_urlsafe(24)
        new_state.pop("feedback", None)

    if not new_state["question_ids"] and not (
        new_state["correct_count"] + new_state["incorrect_count"]
    ):
        return None, True
    return new_state, True


def active_summary(
    progress: dict[str, Any], mode: QuizMode
) -> dict[str, Any] | None:
    """Return a small resume summary for an unfinished mode, else ``None``."""
    state = valid_state_for(progress, mode)
    if state is None:
        return None
    question_ids = state.get("question_ids")
    current_index = state.get("current_index")
    if (
        not isinstance(question_ids, list)
        or not isinstance(current_index, int)
        or current_index >= len(question_ids)
    ):
        return None
    return {
        "current": current_index + 1,
        "total": len(question_ids),
        "requested_size": state.get("requested_size", "all"),
        "chapter_ids": state.get("chapter_ids", []),
        "source_ids": state.get("source_ids", []),
    }