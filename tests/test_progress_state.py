"""Characterization tests for the quiz progress-state helpers.

These lock in the existing behavior of the pure functions that were moved out
of the route layer into ``app.services.progress_state`` so the in-request
state machine can be refactored safely later.
"""

import pytest

from app.models import QuizMode
from app.services import progress_state as ps


def _normal_state(**overrides):
    state = {
        "mode": "normal",
        "question_ids": ["q1", "q2", "q3"],
        "current_index": 1,
        "correct_count": 1,
        "incorrect_count": 0,
        "initial_question_count": 3,
        "status": "pending",
        "answer_token": "tok",
        "option_seed": "seed",
        "requested_size": "all",
        "chapter_ids": ["chapter-a"],
        "source_ids": ["source-a"],
    }
    state.update(overrides)
    return state


def _review_state(**overrides):
    state = _normal_state(
        mode="review",
        review_items=[
            {"question_id": "q1", "role": "original_correction", "chapter_id": None},
            {"question_id": "q2", "role": "original_correction", "chapter_id": "chapter-a"},
            {"question_id": "q3", "role": "transfer_verification", "chapter_id": "chapter-a"},
        ],
        corrected_count=0,
        knowledge_completed_count=0,
        review_shortages=[],
    )
    state.update(overrides)
    return state


def test_session_key_and_quiz_limit():
    assert ps.session_key(QuizMode.NORMAL) == "quiz_progress_normal"
    assert ps.session_key(QuizMode.REVIEW) == "quiz_progress_review"
    assert ps.quiz_limit("10") == 10
    assert ps.quiz_limit("all") is None
    assert ps.quiz_limit("anything-else") == 20  # default


@pytest.mark.parametrize("mode", [QuizMode.NORMAL, QuizMode.REVIEW])
def test_valid_state_passes(mode):
    state = _normal_state() if mode is QuizMode.NORMAL else _review_state()
    assert ps.is_valid_progress_state(state, mode) is True


def test_review_state_accepts_srs_review_items():
    state = _review_state()
    state["review_items"][1] = {
        "question_id": "q2",
        "role": "srs_review",
        "chapter_id": None,
    }
    assert ps.is_valid_progress_state(state, QuizMode.REVIEW) is True


def test_review_state_rejects_unknown_roles():
    state = _review_state()
    state["review_items"][1] = {
        "question_id": "q2",
        "role": "spaced",
        "chapter_id": None,
    }
    assert ps.is_valid_progress_state(state, QuizMode.REVIEW) is False


def test_mode_mismatch_is_rejected():
    assert ps.is_valid_progress_state(_normal_state(), QuizMode.REVIEW) is False
    assert ps.is_valid_progress_state(_review_state(), QuizMode.NORMAL) is False


@pytest.mark.parametrize(
    "override",
    [
        {"current_index": -1},
        {"current_index": True},  # bools are not valid indices
        {"current_index": 4},  # beyond len(question_ids)
        {"correct_count": -1},
        {"answer_token": ""},
        {"option_seed": ""},
        {"status": "unknown"},
        {"question_ids": ["q1", 2]},
    ],
)
def test_invalid_normal_states_are_rejected(override):
    assert ps.is_valid_progress_state(_normal_state(**override), QuizMode.NORMAL) is False


@pytest.mark.parametrize(
    "override",
    [
        {"review_items": []},  # length must match question_ids
        {"review_shortages": "not-a-list"},
        {"corrected_count": -1},
    ],
)
def test_invalid_review_states_are_rejected(override):
    assert ps.is_valid_progress_state(_review_state(**override), QuizMode.REVIEW) is False


def test_valid_state_for_drops_invalid_and_returns_valid():
    progress = {ps.session_key(QuizMode.NORMAL): _normal_state()}
    assert ps.valid_state_for(progress, QuizMode.NORMAL) is not None

    bad = {ps.session_key(QuizMode.NORMAL): {"mode": "normal"}}
    assert ps.valid_state_for(bad, QuizMode.NORMAL) is None
    assert ps.session_key(QuizMode.NORMAL) not in bad  # invalid entry is dropped


def test_active_summary_for_unfinished_round():
    progress = {ps.session_key(QuizMode.NORMAL): _normal_state(current_index=1)}
    summary = ps.active_summary(progress, QuizMode.NORMAL)
    assert summary == {
        "current": 2,
        "total": 3,
        "requested_size": "all",
        "chapter_ids": ["chapter-a"],
        "source_ids": ["source-a"],
    }


def test_active_summary_none_when_complete_or_missing():
    assert ps.active_summary({}, QuizMode.NORMAL) is None
    done = {ps.session_key(QuizMode.NORMAL): _normal_state(current_index=3)}
    assert ps.active_summary(done, QuizMode.NORMAL) is None