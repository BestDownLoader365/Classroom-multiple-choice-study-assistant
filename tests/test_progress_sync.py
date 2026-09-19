from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import copy

import pytest

from app import create_app
from app.models import QuizMode
from tests.test_web import make_app, register, learner_id

LEGACY_HOME = "/course/legacy/"


def login(app):
    client = app.test_client()
    response = client.post('/login', data={'username': 'learner', 'password': 'secret1'})
    assert response.status_code == 302
    client.get(LEGACY_HOME)  # Consume the device-local login flash before comparing pages.
    return client


def state(app, user, mode):
    row = app.extensions['mcq_services'].default_services.progress_repository.get(user, mode)
    return row[1] if row else None


def setup_devices(tmp_path, payload, mode):
    app = make_app(tmp_path, payload)
    first = app.test_client()
    register(first)
    user = learner_id(first)
    if mode is QuizMode.REVIEW:
        app.extensions['mcq_services'].default_services.wrong_question_service.record_attempt(
            user, 'q1', QuizMode.NORMAL, ('1',), False
        )
    path = '/course/legacy/quiz' if mode is QuizMode.NORMAL else '/course/legacy/review'
    first.post(path + '/start', data={'quiz_size': 'all'})
    # Independent application instance models another worker or a server restart.
    other_app = create_app(dict(app.config))
    second = login(other_app)
    return app, first, second, user, path


@pytest.mark.parametrize("mode", [QuizMode.NORMAL, QuizMode.REVIEW])
def test_two_devices_resume_feedback_next_and_completion(tmp_path, valid_payload, mode):
    app, first, second, user, path = setup_devices(tmp_path, valid_payload, mode)
    services = app.extensions['mcq_services'].default_services
    while True:
        current = state(app, user, mode)
        if current['current_index'] == len(current['question_ids']):
            break
        assert first.get(path).data == second.get(path).data
        question = services.question_repository.get_by_id(current['question_ids'][current['current_index']])
        form = {'answer_token': current['answer_token'], 'answers': list(question.correct_answers)}
        assert first.post(path + '/answer', data=form).status_code == 302
        count = services.attempt_repository.count()
        assert second.post(path + '/answer', data=form).status_code == 302
        assert services.attempt_repository.count() == count
        assert first.get(path).data == second.get(path).data
        assert second.post(path + '/next', data={'answer_token': current['answer_token']}).status_code == 302
    assert first.get(path).data == second.get(path).data
    if mode is QuizMode.REVIEW:
        assert services.wrong_question_repository.get_by_id(user, 'q1').corrected
        point = services.weak_knowledge_point_repository.get_by_id(user, 'legacy')
        assert point.active is False
        assert set(point.verified_question_ids) == {'q1', 'q2'}
    first.post('/logout')
    resumed = login(app)
    assert resumed.get(path).status_code == 200
    with resumed.session_transaction() as cookie:
        assert 'quiz_progress_normal' not in cookie
        assert 'quiz_progress_review' not in cookie
    outsider = app.test_client()
    register(outsider, 'outsider')
    assert outsider.get(path).status_code == 302


@pytest.mark.parametrize("mode", [QuizMode.NORMAL, QuizMode.REVIEW])
def test_concurrent_answers_are_recorded_once_across_workers(tmp_path, valid_payload, mode):
    app, first, second, user, path = setup_devices(tmp_path, valid_payload, mode)
    current = state(app, user, mode)
    services = app.extensions['mcq_services'].default_services
    question = services.question_repository.get_by_id(current['question_ids'][0])
    form = {'answer_token': current['answer_token'], 'answers': list(question.correct_answers)}
    before = services.attempt_repository.count()
    barrier = Barrier(2)

    def submit(client):
        barrier.wait(timeout=5)
        return client.post(path + '/answer', data=form).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(submit, [first, second])) == [302, 302]
    assert services.attempt_repository.count() == before + 1
    assert state(app, user, mode)['correct_count'] == 1


@pytest.mark.parametrize("mode", [QuizMode.NORMAL, QuizMode.REVIEW])
def test_stale_next_cannot_skip_an_answered_question(tmp_path, valid_payload, mode):
    app, first, second, user, path = setup_devices(tmp_path, valid_payload, mode)
    services = app.extensions['mcq_services'].default_services
    old_token = state(app, user, mode)['answer_token']
    for index in range(2):
        current = state(app, user, mode)
        question = services.question_repository.get_by_id(current['question_ids'][index])
        first.post(path + '/answer', data={'answer_token': current['answer_token'], 'answers': list(question.correct_answers)})
        if index == 0:
            first.post(path + '/next', data={'answer_token': old_token})
    assert second.post(path + '/next', data={'answer_token': old_token}).status_code == 400
    assert state(app, user, mode)['current_index'] == 1
    assert second.post(path + '/next').status_code == 400


def test_reset_and_restart_are_visible_on_other_device(tmp_path, valid_payload):
    app, first, second, user, path = setup_devices(tmp_path, valid_payload, QuizMode.REVIEW)
    first.post('/course/legacy/quiz/start')
    normal = state(app, user, QuizMode.NORMAL)
    old = state(app, user, QuizMode.REVIEW)
    second.post('/course/legacy/review/start')
    assert state(app, user, QuizMode.REVIEW)['answer_token'] != old['answer_token']
    assert first.post('/course/legacy/review/answer', data={'answer_token': old['answer_token'], 'answers': '2'}).status_code == 400
    second.post('/course/legacy/mistakes/reset')
    assert first.get('/course/legacy/review').status_code == 302
    assert state(app, user, QuizMode.NORMAL) == normal
    # A pre-upgrade device cannot restore the cleared review queue.
    with first.session_transaction() as cookie:
        cookie['quiz_progress_review'] = old
        cookie['question_bank_version'] = app.extensions['mcq_services'].default_services.progress_repository.get(user, QuizMode.REVIEW)[0]
    first.get(LEGACY_HOME)
    assert state(app, user, QuizMode.REVIEW) is None


def test_progress_save_failure_rolls_back_answer_and_mastery(tmp_path, valid_payload, monkeypatch):
    app, first, second, user, path = setup_devices(tmp_path, valid_payload, QuizMode.REVIEW)
    services = app.extensions['mcq_services'].default_services
    original = state(app, user, QuizMode.REVIEW)
    before = services.attempt_repository.count()

    def fail(*args, **kwargs):
        raise RuntimeError('simulated write failure')

    monkeypatch.setattr(services.progress_repository, 'save', fail)
    with pytest.raises(RuntimeError):
        first.post('/course/legacy/review/answer', data={'answer_token': original['answer_token'], 'answers': '2'})
    assert services.attempt_repository.count() == before
    assert services.wrong_question_repository.get_by_id(user, 'q1').review_streak == 0
    assert state(app, user, QuizMode.REVIEW) == original


def test_legacy_cookie_progress_is_never_imported(tmp_path, valid_payload):
    app, first, second, user, path = setup_devices(tmp_path, valid_payload, QuizMode.NORMAL)
    repository = app.extensions['mcq_services'].default_services.progress_repository
    version, original = repository.get(user, QuizMode.NORMAL)
    with repository.database.connect() as connection:
        connection.execute('DELETE FROM quiz_progress WHERE learner_id = ?', (user,))
    with first.session_transaction() as cookie:
        cookie['question_bank_version'] = version
        cookie['quiz_progress_normal'] = original
    first.get('/course/legacy/quiz')
    # The cookie channel is closed: server-side state is the only source of
    # progress, and the ancient keys are purged without ever being read.
    assert state(app, user, QuizMode.NORMAL) is None
    with first.session_transaction() as cookie:
        assert 'quiz_progress_normal' not in cookie
        assert 'question_bank_version' not in cookie
        assert 'quiz_progress' not in cookie


def test_content_only_bank_update_preserves_all_learning_data(tmp_path, valid_payload):
    app, first, second, user, path = setup_devices(tmp_path, valid_payload, QuizMode.REVIEW)
    first.post('/course/legacy/quiz/start')
    valid_payload['questions'][0]['text'] = 'Updated question'
    restarted = make_app(tmp_path, valid_payload)
    device = restarted.test_client()
    response = device.post('/login', data={'username': 'learner', 'password': 'secret1'}, follow_redirects=True)
    assert '检测到题库更新' not in response.text
    services = restarted.extensions['mcq_services'].default_services
    for mode in (QuizMode.NORMAL, QuizMode.REVIEW):
        assert state(restarted, user, mode) is not None
    assert services.attempt_repository.count() == 1
    assert services.wrong_question_repository.get_by_id(user, 'q1') is not None
    # The generation did not move, so a worker running the pre-edit bank
    # keeps serving instead of being fenced off with a 503.
    assert first.post('/course/legacy/quiz/start').status_code == 302
    again = create_app(dict(restarted.config))
    assert again.extensions['mcq_services'].default_services.attempt_repository.count() == 1


def test_structural_bank_change_reconciles_data_and_blocks_old_worker(tmp_path, valid_payload):
    app, first, second, user, path = setup_devices(tmp_path, valid_payload, QuizMode.REVIEW)
    outsider = app.test_client()
    register(outsider, 'outsider')
    other_user = learner_id(outsider)
    services = app.extensions['mcq_services'].default_services
    services.wrong_question_service.record_attempt(other_user, 'q1', QuizMode.NORMAL, ('1',), False)
    outsider.post('/course/legacy/quiz/start')
    with services.progress_repository.database.connect() as connection:
        accounts = [tuple(row) for row in connection.execute('SELECT * FROM users ORDER BY id')]
    valid_payload['questions'][0]['correct_answers'] = ['1']
    restarted = make_app(tmp_path, valid_payload)
    database = restarted.extensions['mcq_services'].default_services.progress_repository.database
    with database.connect() as connection:
        assert [tuple(row) for row in connection.execute('SELECT * FROM users ORDER BY id')] == accounts
        # Only the grading-changed question lost its data, for every account.
        assert connection.execute("SELECT COUNT(*) FROM attempts WHERE question_id = 'q1'").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM wrong_questions").fetchone()[0] == 0
    # The learner's single-question review round is tombstoned; the
    # outsider's normal round silently continues without q1.
    assert state(restarted, user, QuizMode.REVIEW) is None
    outsider_normal = state(restarted, other_user, QuizMode.NORMAL)
    assert outsider_normal is not None
    assert outsider_normal['question_ids'] == ['q2']
    assert first.post('/course/legacy/quiz/start').status_code == 503
    new_client = login(restarted)
    new_client.post('/course/legacy/quiz/start')
    restarted.extensions['mcq_services'].default_services.wrong_question_service.record_attempt(user, 'q1', QuizMode.NORMAL, ('1',), False)
    another_worker = create_app(dict(restarted.config))
    assert another_worker.extensions['mcq_services'].default_services.attempt_repository.count() == 1
    assert state(another_worker, user, QuizMode.NORMAL) is not None


def test_invalid_bank_marks_the_course_unavailable_without_clearing_data(
    tmp_path, valid_payload
):
    """A broken course package must never be read as "this course is empty".

    The old behaviour aborted application assembly.  With per-course runtime
    isolation the correct behaviour is stronger: the worker still starts, the
    broken course becomes ``unavailable``, its stored progress and attempts are
    left byte-for-byte intact, and it is fenced off from learning traffic while
    ``/ready`` reports the degradation.
    """
    app, first, second, user, path = setup_devices(tmp_path, valid_payload, QuizMode.REVIEW)
    del first, second, path
    attempts_before = app.extensions['mcq_services'].default_services.attempt_repository.count()
    state_before = state(app, user, QuizMode.REVIEW)
    assert attempts_before == 1 and state_before is not None

    valid_payload['questions'][0]['type'] = 'invalid'
    degraded = make_app(tmp_path, valid_payload)
    services = degraded.extensions['mcq_services']

    runtime_state = services.course_registry.state('legacy')
    assert runtime_state is not None
    assert runtime_state.status.value == 'unavailable'
    assert runtime_state.servable is False
    assert services.course_registry.ready_course_ids() == []

    # The learner data is untouched: no "all questions deleted" reconciliation.
    stored = services.course_repository.list_accepted()
    assert [record.course.course_id for record in stored] == ['legacy']
    probe = degraded.test_client()
    probe.post(
        '/login', data={'username': 'learner', 'password': 'secret1'}
    )
    assert probe.get('/course/legacy/').status_code == 503
    assert probe.get('/ready').status_code == 503
    assert probe.get('/ready/legacy').status_code == 503

    # Recovering the package restores the course and the preserved history.
    valid_payload['questions'][0]['type'] = 'single'
    recovered = make_app(tmp_path, valid_payload)
    recovered_services = recovered.extensions['mcq_services'].default_services
    assert recovered_services.attempt_repository.count() == attempts_before
    assert state(recovered, user, QuizMode.REVIEW) == state_before


def test_bank_version_only_change_never_rewrites_progress(tmp_path, valid_payload):
    """Wording-only bank differences must not cause pointless progress writes.

    ``bank_version`` is diagnostic: two workers whose banks differ only in
    wording would otherwise keep overwriting each other's ``quiz_progress``
    rows inside the global write lock.
    """
    app = make_app(tmp_path, valid_payload)
    first = app.test_client()
    register(first)
    user = learner_id(first)
    first.post('/course/legacy/quiz/start', data={'quiz_size': 'all'})
    repository = app.extensions['mcq_services'].default_services.progress_repository
    version_before, state_before = repository.get(user, QuizMode.NORMAL)
    assert version_before is not None and state_before is not None

    edited = copy.deepcopy(valid_payload)
    edited['questions'][0]['text'] = 'Pick one option'
    other = make_app(tmp_path, edited)
    other_services = other.extensions['mcq_services'].default_services
    assert other_services.question_bank_state_repository.get_generation() == 0
    second = login(other)

    # Reading a page through the other worker writes nothing: same state, so
    # the stored (diagnostic) bank version is left alone.
    assert second.get(LEGACY_HOME).status_code == 200
    assert repository.get(user, QuizMode.NORMAL) == (version_before, state_before)

    # A real state change is still saved, and only then is the version touched.
    current = state(other, user, QuizMode.NORMAL)
    question = other_services.question_repository.get_by_id(
        current['question_ids'][current['current_index']]
    )
    assert second.post(
        '/course/legacy/quiz/answer',
        data={'answer_token': current['answer_token'],
              'answers': list(question.correct_answers)},
    ).status_code == 302
    version_after, state_after = repository.get(user, QuizMode.NORMAL)
    assert state_after != state_before
    assert version_after != version_before

    # And the sibling worker does not flip the row back on its next request.
    assert first.get(LEGACY_HOME).status_code == 200
    assert repository.get(user, QuizMode.NORMAL) == (version_after, state_after)


@pytest.mark.parametrize('with_progress', [True, False])
def test_upgrade_without_global_fingerprint(tmp_path, valid_payload, with_progress):
    app, first, second, user, path = setup_devices(tmp_path, valid_payload, QuizMode.REVIEW)
    database = app.extensions['mcq_services'].default_services.progress_repository.database
    with database.connect() as connection:
        connection.execute('DROP TABLE question_bank_state')
        connection.execute('DROP TABLE question_registry')
        if not with_progress:
            connection.execute('DELETE FROM quiz_progress')
    restarted = create_app(dict(app.config))
    restarted_services = restarted.extensions['mcq_services'].default_services
    # The bootstrap adopts the current bank as the baseline and preserves
    # every existing learner record instead of wiping it.
    assert restarted_services.attempt_repository.count() == 1
    assert restarted_services.wrong_question_repository.get_by_id(user, 'q1') is not None
    assert restarted_services.question_registry_repository.count() == 2
    assert login(restarted).get(LEGACY_HOME).status_code == 200
    assert (state(restarted, user, QuizMode.REVIEW) is not None) == with_progress
