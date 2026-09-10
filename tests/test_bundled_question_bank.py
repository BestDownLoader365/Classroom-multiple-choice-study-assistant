import re

from app import QUESTION_FILE
from app.repositories import QuestionLoader


KNOWN_SEMICONDUCTOR_MISTRANSLATIONS = {
    r"\b(?:photo)?masks?\b": ("面具", "面罩", "遮罩", "口罩"),
    r"\bparasit(?:e|es|ic|ics)\b": ("寄生虫",),
    r"\bmint(?:ed|ing)?\b": ("铸币局",),
    r"\bfabless\b": ("无纸化公司",),
    r"\bstandard cells?\b": ("标准电池",),
    r"\bnetlists?\b": ("网状列表", "网格列表"),
    r"\bDRC\b|\bdesign rule checks?\b": ("刚果民主共和国",),
    r"\bfloor[- ]?plann(?:ing|ed?)\b|\bfloor[- ]?plans?\b": ("地板规划",),
    r"\bglobal routing\b": ("全球游击",),
    r"\bdetailed routing\b": ("详细游击",),
    r"\btaps?\b": ("水龙头",),
    r"\bbog(?:ie|ies)\b": ("转向架",),
    r"\bpolynomials?\b": ("波利诺米亚",),
    r"\bcharacteri[sz](?:e|ed|es|ing|ation)\b": ("字符化", "特征化"),
}


def test_bundled_bank_has_complete_chinese_learning_aids():
    questions = QuestionLoader(QUESTION_FILE).load()

    assert questions
    assert all(question.text_zh for question in questions)
    assert all(question.explanation_zh for question in questions)
    assert all(option.text_zh for question in questions for option in question.options)


def test_bundled_bank_has_valid_course_metadata_for_every_question():
    loader = QuestionLoader(QUESTION_FILE)
    questions = loader.load()
    source_ids = {source.id for source in loader.sources}
    chapter_ids = {chapter.id for chapter in loader.chapters}
    chapter_sources = {
        chapter.id: chapter.source_id for chapter in loader.chapters
    }

    assert questions
    assert loader.sources
    assert loader.chapters
    assert len(source_ids) == len(loader.sources)
    assert len(chapter_ids) == len(loader.chapters)
    assert all(chapter.source_id in source_ids for chapter in loader.chapters)
    assert all(question.source_id in source_ids for question in questions)
    assert all(
        question.chapter_ids
        and all(
            chapter_id in chapter_ids
            and chapter_sources[chapter_id] == question.source_id
            for chapter_id in question.chapter_ids
        )
        for question in questions
    )
    assert all(question.section for question in questions)
    assert all(question.pages for question in questions)


def test_bundled_multiple_choice_questions_have_a_distractor():
    questions = QuestionLoader(QUESTION_FILE).load()

    multiple_questions = [
        question for question in questions if question.question_type == "multiple"
    ]

    assert all(
        len(question.correct_answers) < len(question.options)
        for question in multiple_questions
    )


def test_bundled_bank_avoids_known_semiconductor_mistranslations():
    questions = QuestionLoader(QUESTION_FILE).load()

    translation_pairs = [
        pair
        for question in questions
        for pair in (
            (question.text, question.text_zh),
            (question.explanation, question.explanation_zh),
            *((option.text, option.text_zh) for option in question.options),
        )
    ]

    offenders = sorted(
        (pattern, mistranslation)
        for pattern, mistranslations in KNOWN_SEMICONDUCTOR_MISTRANSLATIONS.items()
        for english, chinese in translation_pairs
        if re.search(pattern, english, flags=re.IGNORECASE)
        for mistranslation in mistranslations
        if mistranslation in chinese
    )

    assert offenders == []


def test_single_choice_correct_answers_do_not_have_a_systematic_length_tell():
    questions = QuestionLoader(QUESTION_FILE).load()
    single_questions = [
        question for question in questions if question.question_type == "single"
    ]
    offenders = []

    for question in single_questions:
        lengths = {
            option.id: len(option.text.split()) for option in question.options
        }
        correct_id = question.correct_answers[0]
        correct_length = lengths[correct_id]
        distractor_lengths = [
            length for option_id, length in lengths.items() if option_id != correct_id
        ]
        average_distractor_length = sum(distractor_lengths) / len(distractor_lengths)
        uniquely_longest = (
            correct_length == max(lengths.values())
            and list(lengths.values()).count(correct_length) == 1
        )

        if uniquely_longest and correct_length >= 1.5 * average_distractor_length:
            offenders.append(question.id)

    assert single_questions
    random_longest_baseline = sum(
        1 / len(question.options) for question in single_questions
    )
    assert len(offenders) <= random_longest_baseline, offenders
