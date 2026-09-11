from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from app import create_app
from app.models import QuizMode
from tests.test_web import make_app, register, learner_id


def login(app):
    client = app.test_client()
    response = client.post('/login', data={'username': 'learner', 'password': 'secret1'})
    assert response.status_code == 302
    client.get('/')  # Consume the device-local login flash before comparing pages.
    return client


def state(app, user, mode):
    row = app.extensions['mcq_services'].progress_repository.get(user, mode)
    return row[1] if row else None


def setup_devices(tmp_path, payload, mode):
    app = make_app(tmp_path, payload)
    first = app.test_client()
    register(first)
    user = learner_id(first)
    if mode is QuizMode.REVIEW:
        app.extensions['mcq_services'].wrong_question_service.record_attempt(
            user, 'q1', QuizMode.NORMAL, ('1',), False
        )
    path = '/quiz' if mode is QuizMode.NORMAL else '/review'
    first.post(path + '/start', data={'quiz_size': 'all'})
    # Independent application instance models another worker or a server restart.
    other_app = create_app(dict(app.config))
    second = login(other_app)
    return app, first, second, user, path


@pytest.mark.parametrize("mode", [QuizMode.NORMAL, QuizMode.REVIEW])
def test_two_devices_resume_feedback_next_and_completion(tmp_path, valid_payload, mode):
    app, first, second, user, path = setup_devices(tmp_path, valid_payload, mode)
    services = app.extensions['mcq_services']
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
    services = app.extensions['mcq_services']
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
    services = app.extensions['mcq_services']
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
    first.post('/quiz/start')
    normal = state(app, user, QuizMode.NORMAL)
    old = state(app, user, QuizMode.REVIEW)
    second.post('/review/start')
    assert state(app, user, QuizMode.REVIEW)['answer_token'] != old['answer_token']
    assert first.post('/review/answer', data={'answer_token': old['answer_token'], 'answers': '2'}).status_code == 400
    second.post('/mistakes/reset')
    assert first.get('/review').status_code == 302
    assert state(app, user, QuizMode.NORMAL) == normal
    # A pre-upgrade device cannot restore the cleared review queue.
    with first.session_transaction() as cookie:
        cookie['quiz_progress_review'] = old
        cookie['question_bank_version'] = app.extensions['mcq_services'].progress_repository.get(user, QuizMode.REVIEW)[0]
    first.get('/')
    assert state(app, user, QuizMode.REVIEW) is None


def test_progress_save_failure_rolls_back_answer_and_mastery(tmp_path, valid_payload, monkeypatch):
    app, first, second, user, path = setup_devices(tmp_path, valid_payload, QuizMode.REVIEW)
    services = app.extensions['mcq_services']
    original = state(app, user, QuizMode.REVIEW)
    before = services.attempt_repository.count()

    def fail(*args, **kwargs):
        raise RuntimeError('simulated write failure')

    monkeypatch.setattr(services.progress_repository, 'save', fail)
    with pytest.raises(RuntimeError):
        first.post('/review/answer', data={'answer_token': original['answer_token'], 'answers': '2'})
    assert services.attempt_repository.count() == before
    assert services.wrong_question_repository.get_by_id(user, 'q1').review_streak == 0
    assert state(app, user, QuizMode.REVIEW) == original


def test_legacy_cookie_imported_once_without_overwriting_shared_progress(tmp_path, valid_payload):
    app, first, second, user, path = setup_devices(tmp_path, valid_payload, QuizMode.NORMAL)
    repository = app.extensions['mcq_services'].progress_repository
    version, original = repository.get(user, QuizMode.NORMAL)
    with repository.database.connect() as connection:
        connection.execute('DELETE FROM quiz_progress WHERE learner_id = ?', (user,))
    with first.session_transaction() as cookie:
        cookie['question_bank_version'] = version
        cookie['quiz_progress_normal'] = original
    first.get('/quiz')
    assert state(app, user, QuizMode.NORMAL) == original
    second.post('/quiz/start')
    restarted = state(app, user, QuizMode.NORMAL)
    with first.session_transaction() as cookie:
        cookie['question_bank_version'] = version
        cookie['quiz_progress_normal'] = original
    first.get('/quiz')
    assert state(app, user, QuizMode.NORMAL) == restarted


def test_new_device_invalidates_both_modes_after_bank_update(tmp_path, valid_payload):
    app, first, second, user, path = setup_devices(tmp_path, valid_payload, QuizMode.REVIEW)
    first.post('/quiz/start')
    valid_payload['questions'][0]['text'] = 'Updated question'
    restarted = make_app(tmp_path, valid_payload)
    device = restarted.test_client()
    response = device.post('/login', data={'username': 'learner', 'password': 'secret1'}, follow_redirects=True)
    assert '检测到题库更新' in response.text
    for mode in QuizMode:
        assert state(restarted, user, mode) is None
    assert restarted.extensions['mcq_services'].attempt_repository.count() == 0


def test_bank_change_clears_every_account_and_blocks_old_worker(tmp_path, valid_payload):
    app, first, second, user, path = setup_devices(tmp_path, valid_payload, QuizMode.REVIEW)
    outsider = app.test_client()
    register(outsider, 'outsider')
    other_user = learner_id(outsider)
    services = app.extensions['mcq_services']
    services.wrong_question_service.record_attempt(other_user, 'q1', QuizMode.NORMAL, ('1',), False)
    outsider.post('/quiz/start')
    with services.progress_repository.database.connect() as connection:
        accounts = [tuple(row) for row in connection.execute('SELECT * FROM users ORDER BY id')]
    valid_payload['questions'][0]['text'] = 'Another course, reused ID'
    restarted = make_app(tmp_path, valid_payload)
    database = restarted.extensions['mcq_services'].progress_repository.database
    with database.connect() as connection:
        assert [tuple(row) for row in connection.execute('SELECT * FROM users ORDER BY id')] == accounts
        for table in ('attempts', 'wrong_questions', 'quiz_progress'):
            assert connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 0
    assert first.post('/quiz/start').status_code == 503
    new_client = login(restarted)
    new_client.post('/quiz/start')
    restarted.extensions['mcq_services'].wrong_question_service.record_attempt(user, 'q1', QuizMode.NORMAL, ('1',), False)
    another_worker = create_app(dict(restarted.config))
    assert another_worker.extensions['mcq_services'].attempt_repository.count() == 1
    assert state(another_worker, user, QuizMode.NORMAL) is not None


def test_invalid_bank_does_not_clear_learning_data(tmp_path, valid_payload):
    from app.repositories import QuestionBankError
    app, first, second, user, path = setup_devices(tmp_path, valid_payload, QuizMode.REVIEW)
    valid_payload['questions'][0]['type'] = 'invalid'
    with pytest.raises(QuestionBankError):
        make_app(tmp_path, valid_payload)
    assert app.extensions['mcq_services'].attempt_repository.count() == 1
    assert state(app, user, QuizMode.REVIEW) is not None


def test_bank_reset_disables_legacy_cookie_even_when_switching_back(tmp_path, valid_payload):
    import copy
    app, first, second, user, path = setup_devices(tmp_path, valid_payload, QuizMode.NORMAL)
    version, old_state = app.extensions['mcq_services'].progress_repository.get(user, QuizMode.NORMAL)
    changed = copy.deepcopy(valid_payload)
    changed['title'] = 'Another course'
    make_app(tmp_path, changed)
    restored = make_app(tmp_path, valid_payload)
    client = login(restored)
    with client.session_transaction() as cookie:
        cookie['question_bank_version'] = version
        cookie['quiz_progress_normal'] = old_state
    client.get('/')
    assert state(restored, user, QuizMode.NORMAL) is None


@pytest.mark.parametrize('with_progress', [True, False])
def test_upgrade_without_global_fingerprint(tmp_path, valid_payload, with_progress):
    app, first, second, user, path = setup_devices(tmp_path, valid_payload, QuizMode.REVIEW)
    database = app.extensions['mcq_services'].progress_repository.database
    with database.connect() as connection:
        connection.execute('DROP TABLE question_bank_state')
        if not with_progress:
            connection.execute('DELETE FROM quiz_progress')
    restarted = create_app(dict(app.config))
    assert restarted.extensions['mcq_services'].attempt_repository.count() == (1 if with_progress else 0)
    assert login(restarted).get('/').status_code == 200
