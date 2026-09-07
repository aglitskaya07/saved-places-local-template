from unittest.mock import Mock

import pytest

from saved_places import store, update
from saved_places.probe import AccessStopped, ProbeError


def post(identifier):
    return {'pk': str(identifier), 'code': f'code{identifier}', 'media_type': 1}


def test_complete_collection_pages_deduplicate():
    client = Mock()
    client.private_request.side_effect = [
        {'items': [{'media': post(1)}], 'more_available': True, 'next_max_id': 'next'},
        {'items': [post(1), post(2)], 'more_available': False},
    ]
    assert [p['pk'] for p in update.read_collection(client, '1')] == ['1', '2']
    assert client.private_request.call_args.kwargs['params']['max_id'] == 'next'


def test_full_read_rejects_repeated_cursor():
    client = Mock()
    client.private_request.return_value = {'items': [], 'more_available': True, 'next_max_id': 'same'}
    with pytest.raises(ProbeError):
        update.read_collection(client, '1')


@pytest.fixture
def local_update(tmp_path, monkeypatch):
    path = tmp_path/'test.sqlite'
    monkeypatch.setattr(update, 'SESSION_ROOT', tmp_path)
    monkeypatch.setattr(update, 'DATA_ROOT', tmp_path)
    (tmp_path/('a'*24 + '.json')).touch()
    monkeypatch.setattr(update, 'init_db', lambda: store.init_db(path))
    monkeypatch.setattr(update, 'database', lambda: store.database(path))
    monkeypatch.setattr(update, 'import_sources', lambda collection, city, sources, complete:
                        store.import_sources(collection, city, sources, complete, path))
    monkeypatch.setattr(update, 'connect', lambda *args: (Mock(), False))
    store.init_db(path)
    return path


def test_failed_read_does_not_mark_source_absent(local_update, monkeypatch):
    collection = {'id': '1', 'name': 'Cafes'}
    store.import_sources(collection, 'Копенгаген', [update.safe_media(post(1))], True, local_update)
    def fail(*args):
        raise AccessStopped('ChallengeRequired')
    monkeypatch.setattr(update, 'read_collection', fail)
    assert update.update(collection, 'Копенгаген') == 1
    with store.database(local_update) as db:
        assert db.execute('SELECT present FROM memberships').fetchone()['present'] == 1
        assert db.execute('SELECT status FROM runs').fetchone()['status'] == 'blocked'


def test_budget_and_repeat_preserve_prepared_sources(local_update, monkeypatch):
    monkeypatch.setattr(update, 'read_collection', lambda *args: [post(n) for n in range(13)])
    prepare = Mock(return_value={'errors': [], 'coverage': {'frames': True}})
    monkeypatch.setattr(update, 'prepare_media', prepare)
    collection = {'id': '1', 'name': 'Cafes'}
    assert update.update(collection, 'Копенгаген') == 0
    assert prepare.call_count == 10
    with store.database(local_update) as db:
        assert db.execute("SELECT count(*) FROM sources WHERE status='prepared'").fetchone()[0] == 10
    assert update.update(collection, 'Копенгаген') == 0
    assert prepare.call_count == 13
    assert update.update(collection, 'Копенгаген') == 0
    assert prepare.call_count == 13


def test_retry_errors_only_and_per_post_failure_continues(local_update, monkeypatch):
    collection = {'id': '1', 'name': 'Cafes'}
    items = [post(n) for n in range(3)]
    store.import_sources(collection, 'Копенгаген', [update.safe_media(p) for p in items], True, local_update)
    with store.database(local_update) as db:
        db.execute("UPDATE sources SET status='error' WHERE id IN ('0','1')")
    monkeypatch.setattr(update, 'read_collection', lambda *args: items)
    prepare = Mock(side_effect=[RuntimeError(), {'errors': [], 'coverage': {}}])
    monkeypatch.setattr(update, 'prepare_media', prepare)
    assert update.update(collection, 'Копенгаген', retry_errors=True) == 1
    assert prepare.call_count == 2
    with store.database(local_update) as db:
        assert db.execute("SELECT status FROM sources WHERE id='2'").fetchone()[0] == 'pending'
        assert db.execute("SELECT status FROM sources WHERE id='1'").fetchone()[0] == 'prepared'


def test_first_configuration_is_saved_and_reused(tmp_path, monkeypatch):
    monkeypatch.setattr(update, 'DATA_ROOT', tmp_path)
    update.atomic_json(tmp_path/'collections.json', {'collections': [{'id': '9', 'name': 'Cafes'}]})
    monkeypatch.setattr(update.sys.stdin, 'isatty', lambda: True)
    answers = iter(['1', 'Копенгаген'])
    monkeypatch.setattr('builtins.input', lambda *args: next(answers))
    assert update.selected_collection() == ({'id': '9', 'name': 'Cafes'}, 'Копенгаген')
    monkeypatch.setattr('builtins.input', lambda *args: pytest.fail('Saved configuration should be reused'))
    assert update.selected_collection() == ({'id': '9', 'name': 'Cafes'}, 'Копенгаген')


def test_interrupt_keeps_prepared_progress(local_update, monkeypatch):
    monkeypatch.setattr(update, 'read_collection', lambda *args: [post(1), post(2)])
    monkeypatch.setattr(update, 'prepare_media', Mock(side_effect=[
        {'errors': [], 'coverage': {}}, KeyboardInterrupt(),
    ]))
    with pytest.raises(KeyboardInterrupt):
        update.update({'id': '1', 'name': 'Cafes'}, 'Копенгаген')
    with store.database(local_update) as db:
        assert db.execute("SELECT status FROM sources WHERE id='1'").fetchone()[0] == 'prepared'
        run = db.execute('SELECT * FROM runs').fetchone()
        assert run['status'] == 'interrupted'
        assert run['processed'] == 1
        assert run['finished_at']


def test_multi_collection_one_session_all_and_cached_resume(local_update, monkeypatch):
    connect = Mock(return_value=(Mock(), False))
    monkeypatch.setattr(update, 'connect', connect)
    read = Mock(side_effect=[[post(n) for n in range(12)], [post(1), post(13)]])
    monkeypatch.setattr(update, 'read_collection', read)
    prepare = Mock(return_value={'errors': [], 'coverage': {}})
    monkeypatch.setattr(update, 'prepare_media', prepare)
    selected = [({'id': '1', 'name': 'A'}, 'Копенгаген'), ({'id': '2', 'name': 'B'}, 'Нью-Йорк')]
    assert update.update_many(selected, all_items=True) == 0
    assert connect.call_count == 1
    assert prepare.call_count == 13
    with store.database(local_update) as db:
        db.execute("UPDATE memberships SET present=0 WHERE source_id='1'")
    assert update.update_many(selected, all_items=True, cached=update.DATA_ROOT/'batches'/'latest.json') == 0
    assert connect.call_count == 1
    assert read.call_count == 2
    assert prepare.call_count == 13
    with store.database(local_update) as db:
        assert db.execute("SELECT sum(present) FROM memberships WHERE source_id='1'").fetchone()[0] == 0


def test_last_collection_failure_does_not_import_partial_census(local_update, monkeypatch):
    selected = [({'id': '1', 'name': 'A'}, 'Копенгаген'), ({'id': '2', 'name': 'B'}, 'Нью-Йорк')]
    monkeypatch.setattr(update, 'read_collection', Mock(side_effect=[[post(1)], AccessStopped('throttled')]))
    assert update.update_many(selected) == 1
    with store.database(local_update) as db:
        assert db.execute('SELECT count(*) FROM sources').fetchone()[0] == 0


def test_multiple_config_entries_and_legacy_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(update, 'DATA_ROOT', tmp_path)
    update.atomic_json(tmp_path/'collections.json', {'collections': [{'id': '1', 'name': 'A'}, {'id': '2', 'name': 'B'}]})
    update.atomic_json(tmp_path/'selected-collection.json', {'collection_id': '1', 'city': 'Копенгаген'})
    assert len(update.selected_collections()) == 1
    update.atomic_json(tmp_path/'selected-collections.json', [
        {'collection_id': '1', 'city': 'Копенгаген'}, {'collection_id': '2', 'city': 'Нью-Йорк'}])
    assert len(update.selected_collections()) == 2


def test_two_workers_are_bounded_unique_and_persist_each_result(local_update, monkeypatch):
    from threading import Barrier, Lock
    from collections import Counter
    items = [post(n) for n in range(6)]
    monkeypatch.setattr(update, 'read_collection', Mock(side_effect=[items, items[:2]]))
    barrier, lock = Barrier(2), Lock()
    calls = []
    active = peak = 0

    def prepare(item):
        nonlocal active, peak
        with lock:
            calls.append(item['pk'])
            active += 1
            peak = max(peak, active)
        try:
            if int(item['pk']) >= 2:
                with store.database(local_update) as db:
                    row = db.execute('SELECT processed,errors FROM runs').fetchone()
                    assert row['processed'] + row['errors'] >= 1
            barrier.wait(timeout=5)
            if item['pk'] == '1':
                raise RuntimeError('single item failure')
            return {'errors': [], 'coverage': {}}
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(update, 'prepare_media', prepare)
    selections = [({'id': '1', 'name': 'A'}, 'Копенгаген'), ({'id': '2', 'name': 'B'}, 'Нью-Йорк')]
    assert update.update_many(selections, all_items=True, workers=2) == 1
    assert peak == 2
    assert Counter(calls) == Counter(str(n) for n in range(6))
    with store.database(local_update) as db:
        row = db.execute('SELECT * FROM runs').fetchone()
        assert row['processed'] == 5
        assert row['errors'] == 1
        assert db.execute("SELECT count(*) FROM sources WHERE status='prepared'").fetchone()[0] == 5


def test_parallel_interrupt_stops_queue_and_keeps_other_running_result(local_update, monkeypatch):
    from threading import Event
    entered = Event()
    calls = []
    monkeypatch.setattr(update, 'read_collection', lambda *args: [post(n) for n in range(6)])

    def prepare(item):
        calls.append(item['pk'])
        if item['pk'] == '0':
            assert entered.wait(timeout=5)
            raise KeyboardInterrupt
        entered.set()
        return {'errors': [], 'coverage': {}}

    monkeypatch.setattr(update, 'prepare_media', prepare)
    with pytest.raises(KeyboardInterrupt):
        update.update_many([({'id': '1', 'name': 'A'}, 'Копенгаген')], all_items=True, workers=2)
    assert '0' in calls and '1' in calls
    # Other work may finish just before the interrupt becomes observable; no work
    # starts after it has been observed and every completed item remains durable.
    with store.database(local_update) as db:
        row = db.execute('SELECT * FROM runs').fetchone()
        assert row['status'] == 'interrupted'
        assert row['processed'] == len(calls)-1
        assert db.execute("SELECT status FROM sources WHERE id='1'").fetchone()[0] == 'prepared'


def test_cached_media_removes_profile_likes_comments_and_nested_metadata():
    raw = {**post(1), 'user': {'username': 'private'}, 'like_count': 10, 'comments': ['private'],
           'caption': {'text': 'Cafe', 'user': {'username': 'private'}},
           'video_versions': [{'url': 'https://cdn.example/video', 'type': 101}],
           'image_versions2': {'candidates': [{'url': 'https://cdn.example/image', 'width': 900}]},
           'carousel_media': [{**post(2), 'user': {'username': 'private'},
                              'image_versions2': {'candidates': [{'url': 'https://cdn.example/child'}]}}]}
    cleaned = update.cached_media(raw)
    assert set(cleaned) == {'pk', 'code', 'media_type', 'caption', 'video_versions', 'image_versions2', 'carousel_media'}
    assert cleaned['caption'] == {'text': 'Cafe'}
    assert cleaned['video_versions'] == [{'url': 'https://cdn.example/video'}]
    assert cleaned['image_versions2'] == {'candidates': [{'url': 'https://cdn.example/image'}]}
    assert 'user' not in cleaned['carousel_media'][0]
    assert 'private' not in str(cleaned)


def test_workers_invalid_rejected_before_session_or_database(monkeypatch):
    monkeypatch.setattr(update, 'init_db', lambda: pytest.fail('must validate first'))
    with pytest.raises(ValueError):
        update.update_many([], workers=3)
