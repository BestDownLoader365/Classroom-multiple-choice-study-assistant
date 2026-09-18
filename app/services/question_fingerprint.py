"""Content, grading, and placement fingerprints for bank reconciliation.

The canonical identity of a question is always its ``question.id``; these
fingerprints never replace it.  They only classify what changed between two
versions of the same ID:

- the *grading fingerprint* covers exactly the fields that decide whether a
  stored historical answer keeps its meaning: the question type, the set of
  option IDs, and the set of correct answers;
- the *content fingerprint* covers every validated field of the question and
  merely separates cosmetic maintenance (wording, translations, formatting,
  option order, metadata) from a true no-op;
- the *placement fingerprint* covers exactly the fields that decide where a
  question is filed — its ``source_id`` and its ``chapter_ids``.  Those drive
  chapter filtering, review/weak-knowledge selection and chapter progress, so
  a change to them is a structural bank change even though it keeps the
  question's history.

All three are computed from the normalized domain model, never from raw JSON
bytes, so whitespace or field-order edits can never trigger any of them.
"""

import hashlib
import json

from app.models import Question


def _canonical(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def grading_fingerprint(question: Question) -> str:
    """Hash the grading identity: type, option-ID set, correct-answer set."""
    payload = {
        "type": question.question_type,
        "option_ids": sorted(option.id for option in question.options),
        "correct_answers": sorted(question.correct_answers),
    }
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def placement_fingerprint(question: Question) -> str:
    """Hash the filing identity: the source and chapter assignments.

    Chapter membership decides which practice filters, review reinforcements
    and chapter progress a question belongs to, so moving one between
    chapters (or sources) changes the bank's structure even when nothing
    about the question text or grading rule changed.
    """
    payload = {
        "source_id": question.source_id,
        "chapter_ids": list(question.chapter_ids),
    }
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def content_fingerprint(question: Question) -> str:
    """Hash every validated content field of the question model."""
    payload = {
        "id": question.id,
        "text": question.text,
        "text_zh": question.text_zh,
        "type": question.question_type,
        "options": [
            {"id": option.id, "text": option.text, "text_zh": option.text_zh}
            for option in question.options
        ],
        "correct_answers": sorted(question.correct_answers),
        "explanation": question.explanation,
        "explanation_zh": question.explanation_zh,
        "source_id": question.source_id,
        "chapter_ids": list(question.chapter_ids),
        "section": question.section,
        "pages": list(question.pages),
    }
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()