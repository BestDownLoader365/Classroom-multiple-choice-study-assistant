"""Question-bank incremental reconciliation tests.

Every test drives the real startup path: edit ``questions.json``, rebuild the
app on the same SQLite file, and assert exactly which learner data survived.
"""

import copy
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from app import create_app
from app.models import ExamStatus, QuizMode
from app.repositories import QuestionBankError
from tests.conftest import write_json
from tests.test_web import learner_id, make_app, register


def services(app):
    return app.extensions["mcq_services"]


def state(app, user, mode):
    row = services(app).progress_repository.get(user, mode)
    return row[1] if row else None


def generation(app):
    value = services(app).question_bank_state_repository.get_state()
    return value[1] if value else 0


def registry(app):
    return services(app).question_registry_repository.get_all()


def restart(app, payload):
    """Rewrite the bank file and rebuild the app on the same database."""
    write_json(app.config["QUESTION_FILE"], payload)
    return create_app(dict(app.config))


def restart_raw(app, text):
    """Rewrite the bank file verbatim (formatting changes) and rebuild."""
    app.config["QUESTION_FILE"].write_text(text, encoding="utf-8")
    return create_app(dict(app.config))


def prepare(app, username="learner"):
    client = app.test_client()
    register(client, username)
    return client, learner_id(client)


def seed_learning(app, user):
    """q1: wrong then corrected with an SRS schedule; q2: still wrong.

    The wrong answers come first because a fresh wrong answer in a chapter
    restarts its verification; the correction of q1 therefore lands last and
    leaves the legacy chapter at verified ("q1",).
    """
    svc = services(app)
    svc.wrong_question_service.record_attempt(user, "q2", QuizMode.NORMAL, ("b",), False)
    svc.wrong_question_service.record_attempt(user, "q1", QuizMode.NORMAL, ("1",), False)
    svc.wrong_question_service.record_attempt(user, "q1", QuizMode.REVIEW, ("2",), True)


def exam_bank_payload():
    """A 12-question, two-chapter bank large enough for mock exams."""
    questions = []
    for index in range(12):
        questions.append(
            {
                "id": f"q{index}",
                "source_id": "pd",
                "chapter_ids": ["chapter-a" if index % 2 == 0 else "chapter-b"],
                "text": f"Question {index}",
                "type": "single",
                "options": [
                    {"id": "a", "text": "Alpha"},
                    {"id": "b", "text": "Beta"},
                ],
                "correct_answers": ["b"],
                "explanation": f"Because {index}.",
            }
        )
    return {
        "title": "Exam Bank",
        "sources": [{"id": "pd", "title": "Physical Design", "lecture": "L1"}],
        "chapters": [
            {"id": "chapter-a", "source_id": "pd", "title": "Chapter A", "order": 1},
            {"id": "chapter-b", "source_id": "pd", "title": "Chapter B", "order": 2},
        ],
        "questions": questions,
    }


@pytest.mark.parametrize(
    "edit",
    ["text", "text_zh", "explanation", "option_text", "option_order", "metadata"],
)
def test_content_only_edits_preserve_all_history(tmp_path, valid_payload, edit):
    app = make_app(tmp_path, valid_payload)
    client, user = prepare(app)
    seed_learning(app, user)
    client.post("/quiz/start", data={"quiz_size": "all"})
    generation_before = generation(app)

    changed = copy.deepcopy(valid_payload)
    question = changed["questions"][0]
    if edit == "text":
        question["text"] = "Reworded stem"
    elif edit == "text_zh":
        question["text_zh"] = "新的题干翻译"
    elif edit == "explanation":
        question["explanation"] = "A rewritten explanation."
    elif edit == "option_text":
        question["options"][0]["text"] = "Uno"
        question["options"][0]["text_zh"] = "一"
    elif edit == "option_order":
        question["options"] = list(reversed(question["options"]))
    elif edit == "metadata":
        question["section"] = "Another section"
        question["pages"] = [7, 9]

    restarted = restart(app, changed)

    svc = services(restarted)
    assert svc.attempt_repository.count() == 3
    corrected = svc.wrong_question_repository.get_by_id(user, "q1")
    assert corrected is not None and corrected.corrected
    assert corrected.next_review_at is not None
    pending = svc.wrong_question_repository.get_by_id(user, "q2")
    assert pending is not None and not pending.corrected
    point = svc.weak_knowledge_point_repository.get_by_id(user, "legacy")
    assert point is not None and point.verified_question_ids == ("q1",)
    # The unfinished round resumes untouched and the generation never moved.
    assert state(restarted, user, QuizMode.NORMAL) is not None
    assert generation(restarted) == generation_before
    assert registry(restarted)["q1"].status.value == "active"
    # Cosmetic edits never fence off a sibling worker on the old bytes.
    assert client.get("/").status_code == 200


def test_json_formatting_change_is_a_noop(tmp_path, valid_payload):
    app = make_app(tmp_path, valid_payload)
    client, user = prepare(app)
    seed_learning(app, user)
    client.post("/quiz/start", data={"quiz_size": "all"})
    generation_before = generation(app)

    restarted = restart_raw(
        app, json.dumps(valid_payload, ensure_ascii=False, indent=4) + "\n"
    )

    svc = services(restarted)
    assert svc.attempt_repository.count() == 3
    assert state(restarted, user, QuizMode.NORMAL) is not None
    assert generation(restarted) == generation_before


def test_new_wrong_option_preserves_history(tmp_path, valid_payload):
    app = make_app(tmp_path, valid_payload)
    client, user = prepare(app)
    seed_learning(app, user)

    changed = copy.deepcopy(valid_payload)
    changed["questions"][1]["options"].append({"id": "d", "text": "Delta"})

    generation_before = generation(app)
    restarted = restart(app, changed)

    svc = services(restarted)
    assert svc.attempt_repository.count() == 3
    assert svc.wrong_question_repository.get_by_id(user, "q2") is not None
    assert generation(restarted) == generation_before
    assert set(registry(restarted)["q2"].option_ids) == {"a", "b", "c", "d"}


def test_correct_answers_change_clears_only_that_question(tmp_path, valid_payload):
    app = make_app(tmp_path, valid_payload)
    client, user = prepare(app)
    seed_learning(app, user)
    outsider_client, outsider = prepare(app, "outsider")
    services(app).wrong_question_service.record_attempt(
        outsider, "q2", QuizMode.NORMAL, ("b",), False
    )

    changed = copy.deepcopy(valid_payload)
    changed["questions"][0]["correct_answers"] = ["1"]

    generation_before = generation(app)
    restarted = restart(app, changed)

    svc = services(restarted)
    # q1's history is gone for every account; q2's survives untouched.
    with svc.progress_repository.database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM attempts WHERE question_id = 'q1'"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM attempts WHERE question_id = 'q2'"
        ).fetchone()[0] == 2
    assert svc.wrong_question_repository.get_by_id(user, "q1") is None
    survivor = svc.wrong_question_repository.get_by_id(user, "q2")
    assert survivor is not None and not survivor.corrected
    assert svc.wrong_question_repository.get_by_id(outsider, "q2") is not None
    # q1's verification reference was stripped from the weak chapter.
    point = svc.weak_knowledge_point_repository.get_by_id(user, "legacy")
    assert point is not None and point.verified_question_ids == ()
    # The structural change bumps the generation.
    assert generation(restarted) == generation_before + 1
    assert registry(restarted)["q1"].correct_answers == ("1",)


def test_type_change_clears_only_that_question(tmp_path, valid_payload):
    app = make_app(tmp_path, valid_payload)
    client, user = prepare(app)
    seed_learning(app, user)

    changed = copy.deepcopy(valid_payload)
    changed["questions"][1]["type"] = "single"
    changed["questions"][1]["correct_answers"] = ["a"]

    restarted = restart(app, changed)

    svc = services(restarted)
    assert svc.wrong_question_repository.get_by_id(user, "q2") is None
    with svc.progress_repository.database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM attempts WHERE question_id = 'q2'"
        ).fetchone()[0] == 0
    assert svc.wrong_question_repository.get_by_id(user, "q1") is not None


def test_removed_option_id_clears_question_history(tmp_path, valid_payload):
    app = make_app(tmp_path, valid_payload)
    client, user = prepare(app)
    seed_learning(app, user)

    changed = copy.deepcopy(valid_payload)
    del changed["questions"][1]["options"][1]  # option "b" leaves q2

    restarted = restart(app, changed)

    svc = services(restarted)
    assert svc.wrong_question_repository.get_by_id(user, "q2") is None
    assert svc.wrong_question_repository.get_by_id(user, "q1") is not None
    assert set(registry(restarted)["q2"].option_ids) == {"a", "c"}


def test_renamed_option_id_clears_question_history(tmp_path, valid_payload):
    app = make_app(tmp_path, valid_payload)
    client, user = prepare(app)
    seed_learning(app, user)

    changed = copy.deepcopy(valid_payload)
    changed["questions"][1]["options"][1]["id"] = "x"

    restarted = restart(app, changed)

    svc = services(restarted)
    assert svc.wrong_question_repository.get_by_id(user, "q2") is None
    assert svc.wrong_question_repository.get_by_id(user, "q1") is not None


def test_new_question_leaves_existing_history_untouched(tmp_path, valid_payload):
    app = make_app(tmp_path, valid_payload)
    client, user = prepare(app)
    seed_learning(app, user)
    client.post("/quiz/start", data={"quiz_size": "all"})
    round_before = state(app, user, QuizMode.NORMAL)

    changed = copy.deepcopy(valid_payload)
    changed["questions"].append(
        {
            "id": "q3",
            "text": "Newcomer",
            "type": "single",
            "options": [{"id": "y", "text": "Yes"}, {"id": "n", "text": "No"}],
            "correct_answers": ["y"],
            "explanation": "Yes.",
        }
    )

    generation_before = generation(app)
    restarted = restart(app, changed)

    svc = services(restarted)
    assert svc.attempt_repository.count() == 3
    assert svc.wrong_question_repository.get_by_id(user, "q1") is not None
    assert svc.wrong_question_repository.get_by_id(user, "q2") is not None
    # The in-flight round keeps its original (smaller) queue.
    assert state(restarted, user, QuizMode.NORMAL) == round_before
    assert registry(restarted)["q3"].status.value == "active"
    # Adding a question is structural: sibling workers must restart.
    assert generation(restarted) == generation_before + 1


def test_deleted_question_keeps_attempts_but_clears_state(tmp_path, valid_payload):
    app = make_app(tmp_path, valid_payload)
    client, user = prepare(app)
    seed_learning(app, user)
    dashboard_before = services(app).statistics_service.build_dashboard(user)
    assert dashboard_before.total_attempts == 3

    changed = copy.deepcopy(valid_payload)
    del changed["questions"][1]  # q2 leaves the bank

    restarted = restart(app, changed)

    svc = services(restarted)
    # Attempts of the deleted question stay stored...
    with svc.progress_repository.database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM attempts WHERE question_id = 'q2'"
        ).fetchone()[0] == 1
    # ...but its correction/SRS row is gone without any user-facing trace.
    assert svc.wrong_question_repository.get_by_id(user, "q2") is None
    assert svc.wrong_question_repository.get_by_id(user, "q1") is not None
    tombstone = registry(restarted)["q2"]
    assert tombstone.status.value == "retired"
    assert tombstone.retired_at is not None
    # Statistics exclude the deleted question's stored attempts.
    dashboard = svc.statistics_service.build_dashboard(user)
    assert dashboard.total_attempts == 2
    assert dashboard.correct_attempts == 1
    overview = svc.global_statistics_service.build_overview()
    assert overview.total_attempts == 2
    # The restarted worker serves normally and no page mentions the removal.
    client2 = restarted.test_client()
    client2.post("/login", data={"username": "learner", "password": "secret1"})
    home = client2.get("/")
    assert home.status_code == 200
    assert "检测到题库更新" not in home.text


def test_deleted_question_cleans_weak_verification(tmp_path, valid_payload):
    app = make_app(tmp_path, valid_payload)
    client, user = prepare(app)
    svc = services(app)
    # Complete the legacy weak chapter: q1 corrected, q2 transfer-verified.
    svc.wrong_question_service.record_attempt(user, "q1", QuizMode.NORMAL, ("1",), False)
    svc.wrong_question_service.record_attempt(user, "q1", QuizMode.REVIEW, ("2",), True)
    svc.wrong_question_service.record_attempt(user, "q2", QuizMode.REVIEW, ("a", "c"), True)
    point = svc.weak_knowledge_point_repository.get_by_id(user, "legacy")
    assert point is not None
    assert point.verified_question_ids == ("q1", "q2")
    assert point.active is False

    changed = copy.deepcopy(valid_payload)
    del changed["questions"][1]  # q2 leaves the bank

    restarted = restart(app, changed)

    # The stale verified ID is removed and the chapter reactivates, persisted.
    point = services(restarted).weak_knowledge_point_repository.get_by_id(user, "legacy")
    assert point is not None
    assert point.verified_question_ids == ("q1",)
    assert point.active is True


def test_deleted_question_trims_unfinished_normal_round(tmp_path, valid_payload):
    app = make_app(tmp_path, valid_payload)
    client, user = prepare(app)
    client.post("/quiz/start", data={"quiz_size": "all"})
    current = state(app, user, QuizMode.NORMAL)
    first_id, second_id = current["question_ids"]
    answered = services(app).question_repository.get_by_id(first_id)
    client.post(
        "/quiz/answer",
        data={"answer_token": current["answer_token"], "answers": list(answered.correct_answers)},
    )
    answered_state = state(app, user, QuizMode.NORMAL)
    assert answered_state["status"] == "answered"
    assert answered_state["correct_count"] == 1

    changed = copy.deepcopy(valid_payload)
    changed["questions"] = [
        question for question in changed["questions"] if question["id"] != second_id
    ]

    restarted = restart(app, changed)

    trimmed = state(restarted, user, QuizMode.NORMAL)
    assert trimmed["question_ids"] == [first_id]
    # The answered current question survives with its feedback and counter.
    assert trimmed["status"] == "answered"
    assert trimmed["correct_count"] == 1
    assert trimmed["initial_question_count"] == 1
    client2 = restarted.test_client()
    client2.post("/login", data={"username": "learner", "password": "secret1"})
    response = client2.post(
        "/quiz/next", data={"answer_token": trimmed["answer_token"]}, follow_redirects=True
    )
    assert response.status_code == 200


def test_deleted_answered_question_decrements_round_counters(tmp_path, valid_payload):
    app = make_app(tmp_path, valid_payload)
    client, user = prepare(app)
    client.post("/quiz/start", data={"quiz_size": "all"})
    current = state(app, user, QuizMode.NORMAL)
    first_id, second_id = current["question_ids"]
    answered = services(app).question_repository.get_by_id(first_id)
    client.post(
        "/quiz/answer",
        data={"answer_token": current["answer_token"], "answers": list(answered.correct_answers)},
    )
    assert state(app, user, QuizMode.NORMAL)["correct_count"] == 1

    changed = copy.deepcopy(valid_payload)
    changed["questions"] = [
        question for question in changed["questions"] if question["id"] != first_id
    ]

    restarted = restart(app, changed)

    trimmed = state(restarted, user, QuizMode.NORMAL)
    assert trimmed["question_ids"] == [second_id]
    # The removed answered question takes its counter and feedback along:
    # the round is back to a pending state pointing at the next question.
    assert trimmed["correct_count"] == 0
    assert trimmed["incorrect_count"] == 0
    assert trimmed["status"] == "pending"
    assert "feedback" not in trimmed
    assert trimmed["current_index"] == 0
    assert trimmed["initial_question_count"] == 1
    client2 = restarted.test_client()
    client2.post("/login", data={"username": "learner", "password": "secret1"})
    assert client2.get("/quiz").status_code == 200


def test_legacy_round_without_question_results_uses_attempt_fallback(
    tmp_path, valid_payload
):
    app = make_app(tmp_path, valid_payload)
    client, user = prepare(app)
    client.post("/quiz/start", data={"quiz_size": "all"})
    current = state(app, user, QuizMode.NORMAL)
    first_id = current["question_ids"][0]
    answered = services(app).question_repository.get_by_id(first_id)
    client.post(
        "/quiz/answer",
        data={"answer_token": current["answer_token"], "answers": list(answered.correct_answers)},
    )
    # Simulate a pre-upgrade round that never recorded per-question results.
    svc = services(app)
    version, legacy = svc.progress_repository.get(user, QuizMode.NORMAL)
    legacy.pop("question_results", None)
    svc.progress_repository.save(user, QuizMode.NORMAL, version, legacy)

    changed = copy.deepcopy(valid_payload)
    changed["questions"] = [
        question for question in changed["questions"] if question["id"] != first_id
    ]

    restarted = restart(app, changed)

    trimmed = state(restarted, user, QuizMode.NORMAL)
    assert trimmed["question_ids"] == [legacy["question_ids"][1]]
    assert trimmed["correct_count"] == 0
    assert trimmed["status"] == "pending"


def test_deleted_question_trims_review_round_and_items(tmp_path, valid_payload):
    app = make_app(tmp_path, valid_payload)
    client, user = prepare(app)
    svc = services(app)
    svc.wrong_question_service.record_attempt(user, "q1", QuizMode.NORMAL, ("1",), False)
    svc.wrong_question_service.record_attempt(user, "q2", QuizMode.NORMAL, ("b",), False)
    client.post("/review/start")
    review = state(app, user, QuizMode.REVIEW)
    assert set(review["question_ids"]) == {"q1", "q2"}
    assert len(review["review_items"]) == 2

    changed = copy.deepcopy(valid_payload)
    del changed["questions"][0]  # q1 leaves the bank

    restarted = restart(app, changed)

    trimmed = state(restarted, user, QuizMode.REVIEW)
    assert trimmed["question_ids"] == ["q2"]
    assert [item["question_id"] for item in trimmed["review_items"]] == ["q2"]
    client2 = restarted.test_client()
    client2.post("/login", data={"username": "learner", "password": "secret1"})
    page = client2.get("/review")
    assert page.status_code == 200
    assert "410" not in page.text


def _start_exam(client, app, user):
    client.post("/exam/start", data={"question_count": "10", "time_limit": "none"})
    session = services(app).exam_service.get_active_session(user)
    assert session is not None
    return session


def test_deleted_question_shrinks_unfinished_exam(tmp_path):
    app = make_app(tmp_path, exam_bank_payload())
    client, user = prepare(app)
    exam_session = _start_exam(client, app, user)
    svc = services(app)
    slots = svc.exam_repository.get_questions(exam_session.id)
    answered_id = slots[0].question_id
    deleted_id = slots[3].question_id
    client.post(
        f"/exam/{exam_session.id}/answer",
        data={"position": "0", "answers": ["b"]},
    )

    changed = exam_bank_payload()
    changed["questions"] = [
        question for question in changed["questions"] if question["id"] != deleted_id
    ]

    restarted = restart(app, changed)

    svc2 = services(restarted)
    session = svc2.exam_service.get_session(user, exam_session.id)
    assert session.status is ExamStatus.IN_PROGRESS
    assert session.question_count == 9
    slots2 = svc2.exam_repository.get_questions(exam_session.id)
    assert [slot.position for slot in slots2] == list(range(9))
    assert deleted_id not in {slot.question_id for slot in slots2}
    kept = next(slot for slot in slots2 if slot.question_id == answered_id)
    assert kept.selected_answers == ("b",)
    # Resume renders and submits normally with the shrunken denominator.
    client2 = restarted.test_client()
    client2.post("/login", data={"username": "learner", "password": "secret1"})
    page = client2.get(f"/exam/{exam_session.id}")
    assert page.status_code == 200
    assert "/ 9 题" in page.text
    response = client2.post(f"/exam/{exam_session.id}/submit", follow_redirects=True)
    assert response.status_code == 200
    report = svc2.exam_service.get_report(user, exam_session.id)
    assert report.question_count == 9
    assert report.correct_count == 1


def test_deleted_question_never_breaks_submitted_exam(tmp_path):
    app = make_app(tmp_path, exam_bank_payload())
    client, user = prepare(app)
    exam_session = _start_exam(client, app, user)
    svc = services(app)
    slots = svc.exam_repository.get_questions(exam_session.id)
    answered_id = slots[0].question_id
    client.post(
        f"/exam/{exam_session.id}/answer",
        data={"position": "0", "answers": ["b"]},
    )
    client.post(f"/exam/{exam_session.id}/submit")
    assert svc.exam_service.get_session(user, exam_session.id).correct_count == 1

    changed = exam_bank_payload()
    # Delete the one question that was answered correctly.
    changed["questions"] = [
        question for question in changed["questions"] if question["id"] != answered_id
    ]

    restarted = restart(app, changed)

    svc2 = services(restarted)
    session = svc2.exam_service.get_session(user, exam_session.id)
    assert session.status is ExamStatus.SUBMITTED
    assert session.correct_count == 1  # stored history is never rewritten
    report = svc2.exam_service.get_report(user, exam_session.id)
    # ...but the displayed score is recomputed over the surviving slots.
    assert report.question_count == 9
    assert report.correct_count == 0
    client2 = restarted.test_client()
    client2.post("/login", data={"username": "learner", "password": "secret1"})
    assert client2.get(f"/exam/{exam_session.id}/report").status_code == 200


def test_grading_changed_question_is_dropped_from_unfinished_exam(tmp_path):
    app = make_app(tmp_path, exam_bank_payload())
    client, user = prepare(app)
    exam_session = _start_exam(client, app, user)
    svc = services(app)
    slots = svc.exam_repository.get_questions(exam_session.id)
    changed_id = slots[1].question_id

    changed = exam_bank_payload()
    for question in changed["questions"]:
        if question["id"] == changed_id:
            question["correct_answers"] = ["a"]

    restarted = restart(app, changed)

    svc2 = services(restarted)
    session = svc2.exam_service.get_session(user, exam_session.id)
    assert session.question_count == 9
    slots2 = svc2.exam_repository.get_questions(exam_session.id)
    assert changed_id not in {slot.question_id for slot in slots2}
    client2 = restarted.test_client()
    client2.post("/login", data={"username": "learner", "password": "secret1"})
    assert client2.get(f"/exam/{exam_session.id}").status_code == 200
    assert client2.post(f"/exam/{exam_session.id}/submit").status_code == 302


def test_grading_changed_slot_is_excluded_from_submitted_report(tmp_path):
    app = make_app(tmp_path, exam_bank_payload())
    client, user = prepare(app)
    exam_session = _start_exam(client, app, user)
    svc = services(app)
    slots = svc.exam_repository.get_questions(exam_session.id)
    answered_id = slots[0].question_id
    client.post(
        f"/exam/{exam_session.id}/answer",
        data={"position": "0", "answers": ["b"]},
    )
    client.post(f"/exam/{exam_session.id}/submit")

    changed = exam_bank_payload()
    for question in changed["questions"]:
        if question["id"] == answered_id:
            question["correct_answers"] = ["a"]

    restarted = restart(app, changed)

    report = services(restarted).exam_service.get_report(user, exam_session.id)
    assert report.question_count == 9
    # The drifted slot took its historical verdict out of the display score.
    assert report.correct_count == 0
    assert answered_id not in {item.question.id for item in report.items}
    client2 = restarted.test_client()
    client2.post("/login", data={"username": "learner", "password": "secret1"})
    assert client2.get(f"/exam/{exam_session.id}/report").status_code == 200


def test_exam_emptied_by_deletions_is_finalized_quietly(tmp_path):
    payload = exam_bank_payload()
    app = make_app(tmp_path, payload)
    client, user = prepare(app)
    exam_session = _start_exam(client, app, user)
    svc = services(app)
    slot_ids = {slot.question_id for slot in svc.exam_repository.get_questions(exam_session.id)}

    changed = exam_bank_payload()
    changed["questions"] = [
        question for question in changed["questions"] if question["id"] not in slot_ids
    ]

    restarted = restart(app, changed)

    session = services(restarted).exam_service.get_session(user, exam_session.id)
    assert session.status is ExamStatus.SUBMITTED
    assert session.question_count == 0
    client2 = restarted.test_client()
    client2.post("/login", data={"username": "learner", "password": "secret1"})
    # The zero-question report renders instead of failing.
    assert client2.get(f"/exam/{exam_session.id}/report").status_code == 200
    response = client2.get(f"/exam/{exam_session.id}")
    assert response.status_code == 302
    assert response.headers["Location"].endswith(f"/exam/{exam_session.id}/report")


def test_deleted_question_can_be_resurrected_unchanged(tmp_path, valid_payload):
    app = make_app(tmp_path, valid_payload)
    client, user = prepare(app)
    services(app).wrong_question_service.record_attempt(
        user, "q2", QuizMode.NORMAL, ("b",), False
    )

    changed = copy.deepcopy(valid_payload)
    del changed["questions"][1]
    restarted = restart(app, changed)
    assert registry(restarted)["q2"].status.value == "retired"
    assert services(restarted).attempt_repository.count() == 1

    restored = restart(restarted, valid_payload)

    entry = registry(restored)["q2"]
    assert entry.status.value == "active"
    assert entry.retired_at is None
    # The kept attempt becomes visible again; correction state stays cleared.
    assert services(restored).attempt_repository.count() == 1
    assert services(restored).wrong_question_repository.get_by_id(user, "q2") is None
    dashboard = services(restored).statistics_service.build_dashboard(user)
    assert dashboard.total_attempts == 1


def test_retired_id_reuse_for_a_different_question_fails_startup(tmp_path, valid_payload):
    app = make_app(tmp_path, valid_payload)
    client, user = prepare(app)
    services(app).wrong_question_service.record_attempt(
        user, "q1", QuizMode.NORMAL, ("1",), False
    )
    changed = copy.deepcopy(valid_payload)
    del changed["questions"][1]
    restarted = restart(app, changed)

    reused = copy.deepcopy(valid_payload)
    reused["questions"][1]["correct_answers"] = ["b"]  # same ID, new grading
    with pytest.raises(QuestionBankError, match="retired"):
        restart(restarted, reused)

    # Nothing was applied: the registry and all learner data are untouched.
    assert registry(restarted)["q2"].status.value == "retired"
    assert services(restarted).attempt_repository.count() == 1
    assert services(restarted).wrong_question_repository.get_by_id(user, "q1") is not None


def test_concurrent_startup_reconciles_exactly_once(tmp_path, valid_payload):
    app = make_app(tmp_path, valid_payload)
    client, user = prepare(app)
    seed_learning(app, user)
    changed = copy.deepcopy(valid_payload)
    del changed["questions"][1]
    write_json(app.config["QUESTION_FILE"], changed)
    config = dict(app.config)
    generation_before = generation(app)

    with ThreadPoolExecutor(max_workers=2) as pool:
        apps = list(pool.map(lambda _: create_app(config), range(2)))

    # Both workers agree on one structural bump and one reconciliation.
    assert {generation(worker) for worker in apps} == {generation_before + 1}
    svc = services(apps[0])
    assert svc.attempt_repository.count() == 3
    assert svc.wrong_question_repository.get_by_id(user, "q2") is None
    assert svc.wrong_question_repository.get_by_id(user, "q1") is not None


def test_chapter_reassignment_updates_weak_verification(tmp_path):
    payload = exam_bank_payload()
    app = make_app(tmp_path, payload)
    client, user = prepare(app)
    svc = services(app)
    # Complete chapter-a's verification with q0 (corrected) and q2 (transfer).
    svc.wrong_question_service.record_attempt(user, "q0", QuizMode.NORMAL, ("a",), False)
    svc.wrong_question_service.record_attempt(user, "q0", QuizMode.REVIEW, ("b",), True)
    svc.wrong_question_service.record_attempt(user, "q2", QuizMode.REVIEW, ("b",), True)
    point = svc.weak_knowledge_point_repository.get_by_id(user, "chapter-a")
    assert point is not None and point.active is False
    assert point.verified_question_ids == ("q0", "q2")

    changed = exam_bank_payload()
    for question in changed["questions"]:
        if question["id"] == "q2":
            question["chapter_ids"] = ["chapter-b"]

    generation_before = generation(app)
    restarted = restart(app, changed)

    # Content-only chapter move: q2 no longer counts toward chapter-a.
    point = services(restarted).weak_knowledge_point_repository.get_by_id(
        user, "chapter-a"
    )
    assert point is not None
    assert point.verified_question_ids == ("q0",)
    assert point.active is True
    assert generation(restarted) == generation_before


def test_check_question_bank_script(tmp_path, valid_payload, capsys):
    from scripts.check_question_bank import main

    app = make_app(tmp_path, valid_payload)
    question_file = app.config["QUESTION_FILE"]
    database = tmp_path / "mcq.db"

    assert main([str(question_file), "--db", str(database)]) == 0

    changed = copy.deepcopy(valid_payload)
    del changed["questions"][1]
    restarted = restart(app, changed)
    reused = copy.deepcopy(valid_payload)
    reused["questions"][1]["correct_answers"] = ["b"]
    write_json(question_file, reused)
    assert main([str(question_file), "--db", str(database)]) == 2
    assert "q2" in capsys.readouterr().err

    question_file.write_text('{"questions": [}', encoding="utf-8")
    assert main([str(question_file), "--db", str(database)]) == 1