"""Tests for the roman-statement stem rendering helper and its template wiring."""

from app.web.view_helpers import format_stem
from tests.test_web import make_app, register


class TestFormatStem:
    def test_wraps_roman_numeral_lines(self):
        result = format_stem("Intro:\nI. First.\nII. Second.\nIII. Third.\nWhich?")
        assert '<span class="stem-statement">I. First.</span>' in result
        assert '<span class="stem-statement">II. Second.</span>' in result
        assert '<span class="stem-statement">III. Third.</span>' in result
        assert result.startswith("Intro:\n")
        assert result.endswith("\nWhich?")

    def test_escapes_html_before_wrapping(self):
        result = format_stem('I. <script>alert("x")</script>')
        assert "<script>" not in result
        assert "&lt;script&gt;" in result
        assert 'class="stem-statement"' in result

    def test_plain_single_line_stem_is_unchanged(self):
        assert format_stem("Simple stem?") == "Simple stem?"

    def test_non_roman_prefixes_are_not_wrapped(self):
        result = format_stem("In CMOS circuits\nI am a sentence\n1. numbered\nIV.Drip")
        assert "stem-statement" not in result

    def test_supports_numerals_beyond_three(self):
        result = format_stem("IV. Fourth.\nV. Fifth.\nX. Tenth.")
        assert result.count('class="stem-statement"') == 3

    def test_empty_and_none_input(self):
        assert format_stem("") == ""
        assert format_stem(None) == ""


def test_quiz_page_shrinks_roman_statement_lines(tmp_path, valid_payload):
    valid_payload["questions"] = [valid_payload["questions"][0]]
    valid_payload["questions"][0]["text"] = (
        "Consider statements I–III about testing:\n"
        "I. Alpha is true.\n"
        "II. Beta is false.\n"
        "III. Gamma is true.\n"
        "Which option correctly identifies the true statement(s)?"
    )
    app = make_app(tmp_path, valid_payload)
    client = app.test_client()
    register(client)
    client.post("/course/legacy/quiz/start", data={"quiz_size": "all"})
    page = client.get("/course/legacy/quiz")

    assert page.status_code == 200
    assert '<span class="stem-statement">I. Alpha is true.</span>' in page.text
    assert '<span class="stem-statement">III. Gamma is true.</span>' in page.text
    assert "Which option correctly identifies the true statement(s)?" in page.text
