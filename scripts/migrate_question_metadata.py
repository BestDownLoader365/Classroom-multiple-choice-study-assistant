"""Add stable course-material and chapter metadata to a generated question bank.

The existing bank embeds citations such as ``Source: Physical Design2, p.24`` in
explanations.  This migration turns those citations into validated, structured
metadata while preserving question IDs and all existing content.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


SOURCES = [
    {
        "id": "big-picture",
        "title": "THE BIG PICTURE",
        "filename": "THE BIG PICTURE_1stLect_v2026.pdf",
        "lecture": "Lecture 1 · Course overview",
    },
    {
        "id": "physical-design-intro",
        "title": "Physical Design — First Lecture",
        "filename": "Physical Design_1stLect_v2026.pdf",
        "lecture": "Lecture 1 · Physical-design foundations",
    },
    {
        "id": "physical-design-libraries",
        "title": "Physical Design 1 — Libraries",
        "filename": "Physical Design1_v2026.pdf",
        "lecture": "Lecture 2 · Libraries and characterization",
    },
    {
        "id": "physical-design-floorplanning",
        "title": "Physical Design 2 — Floorplanning",
        "filename": "Physical Design2_v2026.pdf",
        "lecture": "Lecture 3 · Floorplanning",
    },
]


CHAPTERS = [
    # THE BIG PICTURE
    ("soc-packaging-chiplets", "big-picture", "SoC, SiP & Chiplet Integration", 1, 9, 20),
    ("successful-chip-design", "big-picture", "Successful Chip Design Requirements", 2, 21, 22),
    ("vdsm-challenges", "big-picture", "VDSM Design Challenges", 3, 23, 24),
    ("transistor-scaling", "big-picture", "Transistor & Packaging Scaling", 4, 25, 26),
    ("semiconductor-value-chain", "big-picture", "Semiconductor Value Chain & VLSI", 5, 27, 31),
    # Physical Design — First Lecture
    ("design-methodology-abstraction", "physical-design-intro", "Design Methodology & Abstraction", 6, 3, 16),
    ("eda-automation", "physical-design-intro", "EDA & Design Automation", 7, 17, 19),
    ("chip-design-flow", "physical-design-intro", "Chip Design Flow, Signoff & Manufacturing", 8, 20, 32),
    ("physical-design-flow", "physical-design-intro", "Physical Design Flow", 9, 33, 36),
    ("vlsi-design-styles", "physical-design-intro", "VLSI Design Styles", 10, 37, 42),
    ("layout-layers-rules", "physical-design-intro", "Layout Layers & Design Rules", 11, 43, 45),
    # Physical Design 1
    ("libraries-views", "physical-design-libraries", "Libraries & Views", 12, 2, 3),
    ("standard-cell-design", "physical-design-libraries", "Standard Cell Library Design", 13, 4, 17),
    ("transistor-sizing-timing", "physical-design-libraries", "Transistor Sizing & Cell Timing", 14, 18, 22),
    ("io-pads-reliability", "physical-design-libraries", "I/O Pads, ESD & Latch-up", 15, 23, 33),
    ("timing-models", "physical-design-libraries", "Library Characterization & Timing Models", 16, 34, 40),
    ("pvt-characterization", "physical-design-libraries", "PVT Characterization", 17, 41, 46),
    ("library-files-summary", "physical-design-libraries", "Library Files & Views", 18, 47, 48),
    # Physical Design 2
    ("floorplanning-overview", "physical-design-floorplanning", "Floorplanning", 19, 2, 2),
    ("technology-files-lef", "physical-design-floorplanning", "Technology Files & LEF", 20, 3, 10),
    ("circuit-description", "physical-design-floorplanning", "Circuit Description", 21, 11, 12),
    ("design-constraints", "physical-design-floorplanning", "Design Constraints", 22, 13, 16),
    ("design-planning-partitioning", "physical-design-floorplanning", "Design Planning & Partitioning", 23, 17, 20),
    ("pad-placement", "physical-design-floorplanning", "Pad Placement & Switching Noise", 24, 21, 23),
    ("power-planning", "physical-design-floorplanning", "Power Planning", 25, 24, 26),
    ("macro-placement", "physical-design-floorplanning", "Macro Placement & Connectivity", 26, 27, 32),
    ("clock-planning", "physical-design-floorplanning", "Clock Planning", 27, 33, 35),
]


SECTION_RANGES = {
    "big-picture": [
        (9, 10, "SoC and on-chip integration"),
        (11, 15, "SiP and advanced packaging"),
        (16, 20, "Monolithic SoCs and chiplets"),
        (21, 22, "Requirements of a successful chip design"),
        (23, 24, "Challenges in VDSM designs"),
        (25, 26, "FinFET, GAAFET, and scaling technologies"),
        (27, 31, "Semiconductor supply and value chains"),
    ],
    "physical-design-intro": [
        (3, 4, "General design approach"),
        (5, 16, "VLSI design abstraction"),
        (17, 19, "Electronic design automation"),
        (20, 25, "Definition, verification, and logic synthesis"),
        (26, 32, "Backend flow, signoff, fabrication, and packaging"),
        (33, 36, "Physical-design stages and objectives"),
        (37, 38, "Full-custom design"),
        (39, 42, "Semi-custom, standard-cell, macro, and FPGA styles"),
        (43, 45, "Layout materials and design rules"),
    ],
    "physical-design-libraries": [
        (2, 3, "Library definition"),
        (4, 8, "Standard-cell physical design guidelines"),
        (9, 17, "Standard-cell types and design rules"),
        (18, 22, "Cell delay and transistor sizing"),
        (23, 30, "I/O pad types, current paths, size, and placement"),
        (31, 33, "ESD protection and latch-up"),
        (34, 38, "Linear and nonlinear delay models"),
        (39, 40, "Polynomial and current-source models"),
        (41, 46, "Temperature, voltage, and process corners"),
        (47, 48, "Standard-cell library files"),
    ],
    "physical-design-floorplanning": [
        (2, 2, "Floorplanning inputs and objectives"),
        (3, 5, "Technology-file rules"),
        (6, 10, "LEF and Technology LEF"),
        (11, 12, "EDIF and structural Verilog"),
        (13, 15, "Timing constraints"),
        (16, 16, "Design-rule constraints"),
        (17, 20, "Implementation styles and physical partitioning"),
        (21, 23, "Pad placement, EM, and switching noise"),
        (24, 26, "Core, macro, and mesh power planning"),
        (27, 32, "Macro placement, congestion, and connectivity"),
        (33, 35, "Clock and hierarchical clock planning"),
    ],
}


SOURCE_ALIASES = {
    "THE BIG PICTURE": "big-picture",
    "Physical Design_1stLect": "physical-design-intro",
    "Physical Design1": "physical-design-libraries",
    "Physical Design2": "physical-design-floorplanning",
}

CITATION = re.compile(
    r"Source:\s*([^,]+),\s*pp?\.(\d+)(?:[-–](\d+))?\.", re.IGNORECASE
)


def migrate(payload: dict[str, Any]) -> dict[str, Any]:
    questions = payload.get("questions")
    if not isinstance(questions, list):
        raise ValueError('Question bank must contain a "questions" array.')

    chapter_catalogue = [
        {"id": chapter_id, "source_id": source_id, "title": title, "order": order}
        for chapter_id, source_id, title, order, _start, _end in CHAPTERS
    ]
    valid_chapters = {chapter["id"] for chapter in chapter_catalogue}
    valid_sources = {source["id"] for source in SOURCES}

    for question in questions:
        existing_chapter_ids = question.get("chapter_ids")
        if (
            existing_chapter_ids is None
            and question.get("chapter_id") in valid_chapters
        ):
            existing_chapter_ids = [question["chapter_id"]]
        if question.get("id") == "q169" and not CITATION.search(
            str(question.get("explanation", ""))
        ):
            source_id = "physical-design-libraries"
            pages = [48]
        else:
            match = CITATION.search(str(question.get("explanation", "")))
            if match:
                source_name, first_page, last_page = match.groups()
                try:
                    source_id = SOURCE_ALIASES[source_name.strip()]
                except KeyError as exc:
                    raise ValueError(
                        f'{question.get("id")}: unknown cited source {source_name!r}'
                    ) from exc
                start = int(first_page)
                end = int(last_page or first_page)
                pages = list(range(start, end + 1))
            elif (
                question.get("source_id") in valid_sources
                and isinstance(existing_chapter_ids, list)
                and bool(existing_chapter_ids)
                and all(
                    chapter_id in valid_chapters
                    for chapter_id in existing_chapter_ids
                )
            ):
                question["chapter_ids"] = existing_chapter_ids
                question.pop("chapter_id", None)
                continue
            else:
                raise ValueError(
                    f'{question.get("id")}: no structured metadata or parseable citation'
                )

        page = pages[0]
        chapter = next(
            (
                item
                for item in CHAPTERS
                if item[1] == source_id and item[4] <= page <= item[5]
            ),
            None,
        )
        if chapter is None:
            raise ValueError(
                f'{question.get("id")}: page {page} is outside the curriculum map'
            )
        section = next(
            title
            for start, end, title in SECTION_RANGES[source_id]
            if start <= page <= end
        )
        question["source_id"] = source_id
        question["chapter_ids"] = [chapter[0]]
        question.pop("chapter_id", None)
        question["section"] = section
        question["pages"] = pages

    known_root_fields = {
        "schema_version",
        "title",
        "title_zh",
        "sources",
        "chapters",
        "questions",
    }
    migrated = {
        "schema_version": 2,
        "title": payload.get("title", "MCQ Practice"),
    }
    if "title_zh" in payload:
        migrated["title_zh"] = payload["title_zh"]
    migrated.update(
        {
            key: value
            for key, value in payload.items()
            if key not in known_root_fields
        }
    )
    migrated["sources"] = SOURCES
    migrated["chapters"] = chapter_catalogue
    migrated["questions"] = questions
    return migrated


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", nargs="?", type=Path, default=Path("questions.json"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or args.input
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    migrated = migrate(payload)
    output.write_text(
        json.dumps(migrated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Migrated {len(migrated['questions'])} questions -> {output}")


if __name__ == "__main__":
    main()
