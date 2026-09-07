from unittest.mock import Mock

import pytest

from saved_places import prepare


def test_preparation_lock_rejects_second_launch_and_releases(tmp_path, monkeypatch):
    monkeypatch.setattr(prepare, 'DATA_ROOT', tmp_path)
    with prepare.preparation_lock():
        with pytest.raises(SystemExit, match='уже запущена'):
            with prepare.preparation_lock():
                pytest.fail('Concurrent launch acquired lock')
    with prepare.preparation_lock():
        pass


def test_missing_video_is_incomplete_even_with_cover(tmp_path, monkeypatch):
    monkeypatch.setattr(prepare, 'PACKETS', tmp_path)
    monkeypatch.setattr(prepare, 'prepare_images', lambda *args: [tmp_path/'cover.jpg'])
    monkeypatch.setattr(prepare, 'recognize_frames', lambda *args: [{'lines': []}])
    packet = prepare.prepare_media({'pk': '12', 'code': 'abc', 'media_type': 2})
    assert packet['incomplete']
    assert packet['coverage']['frames']
    assert any('только обложка' in error for error in packet['errors'])


def test_carousel_missing_slides_and_ocr_error_are_visible(tmp_path, monkeypatch):
    monkeypatch.setattr(prepare, 'PACKETS', tmp_path)
    monkeypatch.setattr(prepare, 'prepare_images', lambda *args: [tmp_path/'slide.jpg'])
    monkeypatch.setattr(prepare, 'recognize_frames', lambda *args: [{'error': 'unreadable'}])
    packet = prepare.prepare_media({'pk': '12', 'code': 'abc', 'media_type': 8,
                                    'carousel_media': [{}, {}]})
    assert packet['incomplete']
    assert len(packet['errors']) == 2


def test_image_without_audio_is_complete_and_removes_stale_transcript(tmp_path, monkeypatch):
    monkeypatch.setattr(prepare, 'PACKETS', tmp_path)
    (tmp_path/'12').mkdir()
    (tmp_path/'12'/'speech.json').write_text('stale video transcript')
    monkeypatch.setattr(prepare, 'prepare_images', lambda *args: [tmp_path/'slide.jpg'])
    monkeypatch.setattr(prepare, 'recognize_frames', lambda *args: [{'lines': ['Cafe']}])
    packet = prepare.prepare_media({'pk': '12', 'code': 'abc', 'media_type': 1})
    assert not packet['incomplete']
    assert not (tmp_path/'12'/'speech.json').exists()


def test_empty_images_do_not_invoke_ocr(tmp_path, monkeypatch):
    monkeypatch.setattr(prepare, 'PACKETS', tmp_path)
    monkeypatch.setattr(prepare, 'prepare_images', lambda *args: [])
    ocr = Mock()
    monkeypatch.setattr(prepare, 'recognize_frames', ocr)
    packet = prepare.prepare_media({'pk': '12', 'code': 'abc', 'media_type': 1})
    assert packet['incomplete']
    ocr.assert_not_called()


def test_preparation_does_not_swallow_interrupt(tmp_path, monkeypatch):
    import sys
    from saved_places import store
    monkeypatch.setattr(prepare, 'DATA_ROOT', tmp_path)
    monkeypatch.setattr(prepare, 'SESSION_ROOT', tmp_path)
    (tmp_path/('a'*24 + '.json')).touch()
    monkeypatch.setattr(prepare, 'init_db', lambda: store.init_db(tmp_path/'db.sqlite'))
    monkeypatch.setattr(prepare, 'database', lambda: store.database(tmp_path/'db.sqlite'))
    monkeypatch.setattr(prepare, 'read_json', lambda path: {'collections': [{'id': '1'}]})
    def interrupt(*args):
        raise KeyboardInterrupt
    monkeypatch.setattr(prepare, 'connect', interrupt)
    monkeypatch.setattr(sys, 'argv', ['prepare', '--collection', '1', '--city', 'Копенгаген'])
    with pytest.raises(KeyboardInterrupt):
        prepare.main()
    with store.database(tmp_path/'db.sqlite') as db:
        run = db.execute('SELECT * FROM runs').fetchone()
        assert run['status'] == 'interrupted'
        assert run['finished_at']


def test_whisper_cpp_offsets_are_seconds_and_text_is_joined(tmp_path, monkeypatch):
    import json
    monkeypatch.setattr(prepare.shutil, 'which', lambda name: '/mock/whisper-cli')
    monkeypatch.setattr(prepare.Path, 'is_file', lambda path: path.name == 'ggml-small.bin')
    def command(args, timeout):
        assert args[0] == 'whisper-cli'
        assert args[args.index('-l')+1] == 'auto'
        output = prepare.Path(args[args.index('-of')+1]).with_suffix('.json')
        output.write_text(json.dumps({'transcription': [
            {'text': ' First place ', 'offsets': {'from': 1230, 'to': 4560}},
            {'text': 'Second place', 'offsets': {'from': 5010, 'to': 10025}},
        ]}))
        return ''
    monkeypatch.setattr(prepare, 'command', command)
    transcript, model = prepare.transcribe_audio(tmp_path/'audio.wav', tmp_path)
    assert transcript['text'] == 'First place Second place'
    assert transcript['segments'] == [
        {'text': ' First place ', 'start': 1.23, 'end': 4.56},
        {'text': 'Second place', 'start': 5.01, 'end': 10.025},
    ]
    assert model == 'whisper.cpp small (Metal)'


@pytest.mark.parametrize('cli_present,model_present', [(False, True), (True, False)])
def test_whisper_cpu_fallback_when_cpp_requirements_missing(tmp_path, monkeypatch, cli_present, model_present):
    import json
    monkeypatch.setattr(prepare.shutil, 'which', lambda name: '/mock/whisper-cli' if cli_present else None)
    monkeypatch.setattr(prepare.Path, 'is_file', lambda path: model_present)
    expected = {'text': 'Cafe name', 'segments': [{'text': 'Cafe name', 'start': 0.5, 'end': 2.3}]}
    def command(args, timeout):
        assert args[0] == 'whisper'
        assert args[args.index('--device')+1] == 'cpu'
        assert args[args.index('--condition_on_previous_text')+1] == 'False'
        (tmp_path/'audio.json').write_text(json.dumps(expected))
        return ''
    monkeypatch.setattr(prepare, 'command', command)
    transcript, model = prepare.transcribe_audio(tmp_path/'audio.wav', tmp_path)
    assert transcript == expected
    assert model == 'Whisper tiny (CPU)'
