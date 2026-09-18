"""Stale-worker behaviour: generation fencing, logout, readiness, logging.

These tests model the multi-worker window that follows a structural bank
update: one worker still serves the old bank while the database generation has
already advanced.
"""

import copy
import logging

from app import create_app
from tests.conftest import write_json
from tests.test_web import learner_id, make_app, register


def add_question(payload):
    """Return a copy of the bank with one extra question (structural change)."""
    changed = copy.deepcopy(payload)
    changed["questions"].append(
        {
            "id": "q3",
            "text": "Third question",
            "type": "single",
            "options": [
                {"id": "yes", "text": "Yes"},
                {"id": "no", "text": "No"},
            ],
            "correct_answers": ["yes"],
            "explanation": "Yes.",
        }
    )
    return changed


def stale_pair(tmp_path, valid_payload):
    """Return (stale app, signed-in client on it, current app, learner id).

    The first worker keeps the pre-update bank in memory while the database is
    advanced by a structural update performed by the second one.
    """
    stale = make_app(tmp_path, valid_payload)
    client = stale.test_client()
    register(client)
    user = learner_id(client)
    write_json(stale.config["QUESTION_FILE"], add_question(valid_payload))
    current = create_app(dict(stale.config))
    return stale, client, current, user


def test_stale_worker_fences_learning_pages_but_still_allows_sign_out(
    tmp_path, valid_payload
):
    stale, client, current, user = stale_pair(tmp_path, valid_payload)

    learning_paths = (
        "/",
        "/dashboard",
        "/stats",
        "/quiz/setup",
        "/mistakes",
        "/review",
        "/exam",
    )
    for path in learning_paths:
        assert client.get(path).status_code == 503, path
    # The fence is read-write: the stale worker cannot record attempts either.
    attempts_before = stale.extensions["mcq_services"].attempt_repository.count()
    assert client.post("/quiz/start", data={"quiz_size": "all"}).status_code == 503
    assert client.post("/review/start").status_code == 503
    assert (
        stale.extensions["mcq_services"].attempt_repository.count() == attempts_before
    )

    # Reference material stays reachable for a signed-in learner.
    assert client.get("/glossary").status_code == 200

    # Signing out must always work, however stale the worker is.
    response = client.post("/logout", follow_redirects=True)
    assert response.status_code == 200
    assert "你已安全退出" in response.text
    with client.session_transaction() as browser_session:
        assert "user_id" not in browser_session

    # Afterwards the learner is back at the login page, not in a 503 loop.
    assert client.get("/dashboard").status_code == 302
    assert client.get("/login").status_code == 200
    assert client.get("/glossary").status_code == 302


def test_stale_worker_readiness_differs_from_liveness(tmp_path, valid_payload):
    stale, client, current, user = stale_pair(tmp_path, valid_payload)

    health = client.get("/health")
    assert health.status_code == 200
    assert health.get_json() == {"status": "ok"}

    ready = client.get("/ready")
    assert ready.status_code == 503
    assert ready.get_json() == {
        "status": "stale",
        "worker_generation": 0,
        "database_generation": 1,
    }

    fresh = current.test_client()
    assert fresh.get("/health").get_json() == {"status": "ok"}
    fresh_ready = fresh.get("/ready")
    assert fresh_ready.status_code == 200
    assert fresh_ready.get_json() == {"status": "ready"}


def test_stale_503_page_offers_a_way_out_and_logs_once(
    tmp_path, valid_payload, caplog
):
    stale, client, current, user = stale_pair(tmp_path, valid_payload)

    with caplog.at_level(logging.WARNING):
        page = client.get("/dashboard")

    assert page.status_code == 503
    assert page.headers["Retry-After"] == "5"
    assert "题库正在更新" in page.text
    assert "重启" not in page.text
    # No link back into the same 503 (neither the card nor the header brand).
    assert 'href="/"' not in page.text
    assert 'href="/dashboard"' not in page.text
    assert 'href="/logout"' not in page.text
    assert 'action="/logout"' in page.text

    assert (
        "Question bank generation mismatch: worker=0 db=1 path=/dashboard"
        in caplog.text
    )
    assert caplog.text.count("generation mismatch") == 1


def test_stats_page_is_fenced_exactly_like_the_dashboard(tmp_path, valid_payload):
    stale, client, current, user = stale_pair(tmp_path, valid_payload)

    fresh = current.test_client()
    response = fresh.post(
        "/login", data={"username": "learner", "password": "secret1"}
    )
    assert response.status_code == 302
    assert fresh.get("/dashboard").status_code == 200
    assert fresh.get("/stats").status_code == 200
    assert client.get("/dashboard").status_code == 503
    assert client.get("/stats").status_code == 503


def test_stale_login_and_register_never_land_on_a_503(tmp_path, valid_payload):
    """Account pages are exempt from the fence, so they must not jump into it."""
    stale, client, current, user = stale_pair(tmp_path, valid_payload)

    # Already signed in: /login redirects to a page this worker can serve.
    response = client.get("/login")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/glossary")
    page = client.get("/login", follow_redirects=True)
    assert "题库正在更新" in page.text

    # A new device can still sign in, and lands on the glossary with the notice.
    fresh = stale.test_client()
    signed_in = fresh.post(
        "/login", data={"username": "learner", "password": "secret1"}
    )
    assert signed_in.status_code == 302
    assert signed_in.headers["Location"].endswith("/glossary")
    assert "题库正在更新" in fresh.get("/glossary").text

    # Registration behaves the same way.
    newcomer = stale.test_client()
    registered = newcomer.post(
        "/register",
        data={
            "username": "newcomer",
            "password": "secret1",
            "password_confirmation": "secret1",
        },
    )
    assert registered.status_code == 302
    assert registered.headers["Location"].endswith("/glossary")
    assert newcomer.get("/glossary").status_code == 200

    # The healthy worker keeps the original redirects.
    healthy = current.test_client()
    signed_in = healthy.post(
        "/login", data={"username": "learner", "password": "secret1"}
    )
    assert signed_in.headers["Location"].endswith("/")
    assert healthy.get("/login").status_code == 302
    assert healthy.get("/login").headers["Location"].endswith("/")


def test_content_only_update_keeps_the_sibling_worker_serving(
    tmp_path, valid_payload
):
    """Only structural changes fence workers; wording edits do not."""
    stale = make_app(tmp_path, valid_payload)
    client = stale.test_client()
    register(client)
    edited = copy.deepcopy(valid_payload)
    edited["questions"][0]["text"] = "Pick exactly one answer"
    write_json(stale.config["QUESTION_FILE"], edited)
    current = create_app(dict(stale.config))

    def generation_of(app):
        repository = app.extensions["mcq_services"].question_bank_state_repository
        return repository.get_generation()

    assert generation_of(stale) == 0
    assert generation_of(current) == 0
    assert client.get("/dashboard").status_code == 200
    assert client.get("/stats").status_code == 200
    assert client.get("/ready").status_code == 200
