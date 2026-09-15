"""Template and catalogue helpers for the learner-facing pages.

These are presentation-layer concerns kept separate from the route handlers
so the routes read as thin HTTP adapters. ``option_label`` and the catalogue
filters are pure; ``build_curriculum`` only reads from the question
repository it is given.
"""

import re
from datetime import tzinfo
from typing import Any

from flask import abort
from markupsafe import Markup, escape

from app.repositories import QuestionRepository
from app.services import srs_service as _srs
from app.services import to_display


def format_duration(seconds: int | None) -> str:
    """Render an elapsed second count as ``mm:ss`` (or ``h:mm:ss``)."""
    total = max(0, int(seconds or 0))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def format_datetime(iso_timestamp: str | None, *, zone: tzinfo) -> str:
    """Render a stored UTC ISO timestamp in the display timezone."""
    if not iso_timestamp:
        return "—"
    parsed = _srs.parse_timestamp(iso_timestamp)
    return to_display(parsed, zone).strftime("%Y-%m-%d %H:%M")


def option_label(index: int) -> str:
    """Return spreadsheet-style option labels: A..Z, AA..AZ, BA..."""
    if not isinstance(index, int) or isinstance(index, bool) or index < 0:
        raise ValueError("option index must be a non-negative integer")
    label = ""
    value = index + 1
    while value:
        value, remainder = divmod(value - 1, 26)
        label = chr(ord("A") + remainder) + label
    return label


_ROMAN_STATEMENT_LINE = re.compile(r"^(X{1,3}|IX|IV|V?I{0,3})\.\s")


def format_stem(text: str | None) -> Markup:
    """Render a question stem with smaller roman-numeral statement lines.

    Lines such as ``I. ...`` or ``II. ...`` are wrapped in a
    ``stem-statement`` span so CSS can shrink them relative to the intro
    and closing lines. Every line is escaped before wrapping, and the
    newline separators are preserved for the ``pre-line`` white-space
    rendering, so the returned markup is safe to render directly.
    """
    lines = []
    for line in str(text or "").split("\n"):
        if _ROMAN_STATEMENT_LINE.match(line):
            lines.append(
                Markup('<span class="stem-statement">{}</span>').format(escape(line))
            )
        else:
            lines.append(escape(line))
    return Markup("\n").join(lines)


def build_curriculum(question_repository: QuestionRepository) -> list[dict[str, Any]]:
    """Build one dynamic catalogue for selection and filtering templates."""
    return [
        {
            "source": source,
            "chapters": [
                {
                    "chapter": chapter,
                    "question_count": question_repository.question_count_for_chapter(
                        chapter.id
                    ),
                }
                for chapter in question_repository.get_chapters(source.id)
            ],
        }
        for source in question_repository.get_sources()
    ]


def catalogue_filter(
    raw_values: list[str], known_ids: set[str], label: str
) -> set[str] | None:
    """Normalize a multi-select filter; empty or ``all`` means no restriction."""
    values = {value.strip() for value in raw_values if value.strip()}
    if not values or values == {"all"}:
        return None
    values.discard("all")
    unknown = values - known_ids
    if unknown:
        abort(400, description=f"提交的{label}筛选不存在，请重新选择。")
    return values


def single_catalogue_filter(
    raw_value: str, known_ids: set[str], label: str
) -> set[str] | None:
    if not raw_value or raw_value == "all":
        return None
    if raw_value not in known_ids:
        abort(400, description=f"提交的{label}筛选不存在，请重新选择。")
    return {raw_value}