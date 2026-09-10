from scripts.migrate_question_metadata import migrate


def test_migration_preserves_question_and_structures_embedded_citation():
    payload = {
        "title": "Test",
        "questions": [
            {
                "id": "q-test",
                "text": "What is floorplanning?",
                "explanation": "A planning stage. Source: Physical Design2, p.2.",
            }
        ],
    }

    migrated = migrate(payload)
    question = migrated["questions"][0]

    assert question["id"] == "q-test"
    assert question["source_id"] == "physical-design-floorplanning"
    assert question["chapter_ids"] == ["floorplanning-overview"]
    assert question["pages"] == [2]
    assert len(migrated["sources"]) == 4
    assert len(migrated["chapters"]) == 27


def test_migration_preserves_existing_multiple_chapter_ids():
    payload = {
        "title": "Test",
        "questions": [
            {
                "id": "q-test",
                "source_id": "physical-design-floorplanning",
                "chapter_ids": ["floorplanning-overview", "macro-placement"],
                "text": "Shared topic",
            }
        ],
    }

    migrated = migrate(payload)

    assert migrated["questions"][0]["chapter_ids"] == [
        "floorplanning-overview",
        "macro-placement",
    ]
