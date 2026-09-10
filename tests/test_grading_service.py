import pytest

from app.services import AnswerValidationError, GradingService


def test_single_correct(sample_questions):
    assert GradingService().grade(sample_questions[0], ["2"]) is True


def test_single_wrong(sample_questions):
    assert GradingService().grade(sample_questions[0], ["1"]) is False


def test_multiple_exact_match(sample_questions):
    assert GradingService().grade(sample_questions[1], ["a", "c"]) is True


def test_multiple_missing_answer(sample_questions):
    assert GradingService().grade(sample_questions[1], ["a"]) is False


def test_multiple_extra_answer(sample_questions):
    assert GradingService().grade(sample_questions[1], ["a", "b", "c"]) is False


def test_multiple_order_does_not_matter(sample_questions):
    assert GradingService().grade(sample_questions[1], ["c", "a"]) is True


def test_duplicate_selections_are_removed(sample_questions):
    assert GradingService().grade(sample_questions[1], ["a", "a", "c"]) is True


def test_unknown_option_is_rejected(sample_questions):
    with pytest.raises(AnswerValidationError, match="does not belong"):
        GradingService().grade(sample_questions[0], ["unknown"])
