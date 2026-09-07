"""Manual collection refresh and bounded local preparation for review in Codex."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from threading import Lock
import json
import logging
from pathlib import Path
import re
import sys

from saved_places.prepare import preparation_lock, prepare_media
from saved_places.probe import (
    DATA_ROOT, SESSION_ROOT, AccessStopped, ProbeError, atomic_json, connect,
    cursor_after, read_json, safe_media,
)
from saved_places.store import database, import_sources, init_db, now

BUDGET = 10


def read_collection(client, collection_id: str) -> list[dict]:
    """Return a complete snapshot, or fail without implying any removal."""
    if not collection_id.isdigit():
        raise ProbeError('Некорректный идентификатор коллекции.')
    media, seen = {}, set()
    cursor = ''
    for _ in range(1000):
        result = client.private_request(f'feed/collection/{collection_id}/', params={
            'include_igtv_preview': 'false', 'max_id': cursor,
        })
        for entry in result['items']:
            item = entry.get('media', entry)
            identifier = str(item['pk'])
            if not identifier.isdigit():
                raise ProbeError('Некорректный идентификатор публикации.')
            media.setdefault(identifier, item)
        cursor = cursor_after(result, seen)
        if not cursor:
            return list(media.values())
    raise ProbeError('Превышен предел страниц. Полнота коллекции не подтверждена.')


def cached_media(item: dict) -> dict:
    """Retain only fields consumed by local preparation, including carousel covers."""
    result = {key: item[key] for key in ('pk', 'code', 'media_type') if key in item}
    if 'caption' in item:
        result['caption'] = {'text': (item.get('caption') or {}).get('text', '')}
    if 'video_versions' in item:
        result['video_versions'] = [{'url': v['url']} for v in item.get('video_versions') or [] if v.get('url')]
    if 'image_versions2' in item:
        result['image_versions2'] = {'candidates': [
            {'url': candidate['url']} for candidate in (item.get('image_versions2') or {}).get('candidates', [])
            if candidate.get('url')
        ]}
    if 'carousel_media' in item:
        result['carousel_media'] = [cached_media(child) for child in item.get('carousel_media') or []]
    return result


def prepare_parallel(items: list[dict], workers: int, process):
    """Bound in-flight work; finish running items before releasing the process lock."""
    if workers == 1:
        for item in items:
            process(item)
        return
    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='saved-places')
    remaining = iter(items)
    pending = set()
    try:
        for _ in range(workers):
            item = next(remaining, None)
            if item is not None:
                pending.add(executor.submit(process, item))
        while pending:
            finished, pending = wait(pending, return_when=FIRST_COMPLETED)
            # Read every finished result before scheduling any replacements.
            for future in finished:
                future.result()
            for _ in finished:
                item = next(remaining, None)
                if item is not None:
                    pending.add(executor.submit(process, item))
    finally:
        for future in pending:
            future.cancel()
        # Active subprocesses are allowed to finish and persist their own result.
        # No next publication starts after cancellation, and the directory lock stays held.
        executor.shutdown(wait=True, cancel_futures=True)


def selected_collections(configure: bool = False, config_path: Path | None = None) -> list[tuple[dict, str]]:
    path = config_path or DATA_ROOT/'selected-collections.json'
    cached = read_json(DATA_ROOT/'collections.json').get('collections', [])
    if not cached:
        raise SystemExit('Сначала запусти «Проверить Instagram», чтобы получить коллекции.')
    saved = read_json(path)
    if not saved and not config_path:
        legacy = read_json(DATA_ROOT/'selected-collection.json')
        saved = [legacy] if legacy else []
    if isinstance(saved, dict):
        saved = saved.get('collections', [])
    if saved and not configure:
        chosen = []
        seen = set()
        for entry in saved:
            collection = next((c for c in cached if c['id'] == entry.get('collection_id')), None)
            city = entry.get('city')
            if not collection or not isinstance(city, str) or not 1 <= len(city.strip()) <= 120:
                raise SystemExit('Сохранённая настройка недействительна. Запусти с --configure.')
            if collection['id'] in seen:
                raise SystemExit('Коллекция указана в настройке дважды.')
            seen.add(collection['id'])
            chosen.append((collection, city.strip()))
        return chosen
    if config_path and not configure:
        raise SystemExit('Файл настройки пуст или отсутствует.')
    if not sys.stdin.isatty():
        raise SystemExit('Первый запуск требует выбора коллекций в Терминале.')
    print('Выбери коллекции для обновления (номера через запятую):')
    for index, collection in enumerate(cached, 1):
        print(f"{index}. {collection['name']}")
    choices = [part.strip() for part in input('Номера коллекций: ').split(',')]
    if not choices or any(not c.isdigit() or not 1 <= int(c) <= len(cached) for c in choices):
        raise SystemExit('Нужно ввести номера из списка. Настройка не изменена.')
    chosen = []
    for index in dict.fromkeys(int(c)-1 for c in choices):
        collection = cached[index]
        city = input(f"Город для «{collection['name']}» [{collection['name']}]: ").strip() or collection['name']
        if not 1 <= len(city) <= 120:
            raise SystemExit('Название города должно содержать от 1 до 120 символов.')
        chosen.append((collection, city))
    atomic_json(path, [{'collection_id': c['id'], 'city': city} for c, city in chosen])
    return chosen


def selected_collection(configure: bool = False) -> tuple[dict, str]:
    """Compatibility for the previous single-collection entry point."""
    return selected_collections(configure)[0]


def update(collection: dict, city: str, retry_errors: bool = False) -> int:
    return update_many([(collection, city)], retry_errors=retry_errors)


def update_many(selections: list[tuple[dict, str]], retry_errors: bool = False,
                all_items: bool = False, cached: Path | None = None, snapshot_only: bool = False,
                workers: int = 1) -> int:
    if workers not in (1, 2):
        raise ValueError('Допустимо 1 или 2 параллельных обработчика.')
    sessions = [p for p in SESSION_ROOT.glob('*.json')
                if re.fullmatch(r'[a-f0-9]{24}', p.stem)]
    if not cached and len(sessions) != 1:
        raise SystemExit('Нужна одна подключённая Instagram-сессия. Запусти «Проверить Instagram».')
    init_db()
    with database() as db:
        run = db.execute("INSERT INTO runs(started_at,status) VALUES(?,'running')", (now(),)).lastrowid
    found = processed = errors = 0
    status = 'interrupted'
    message = 'Материалы подготовлены для разбора в Codex; карточки автоматически не создаются.'
    try:
        if cached:
            snapshot = read_json(cached)
            if snapshot.get('complete') is not True:
                raise ProbeError('Снимок не подтверждает полное чтение коллекций.')
            snapshots = snapshot['collections']
            expected = {(c['id'], city) for c, city in selections}
            actual = {(entry['collection']['id'], entry['city']) for entry in snapshots}
            if expected != actual or len(snapshots) != len(expected):
                raise ProbeError('Снимок не соответствует выбранным коллекциям.')
            snapshots = [{
                'collection': {'id': entry['collection']['id'], 'name': entry['collection']['name']},
                'city': entry['city'], 'media': [cached_media(item) for item in entry['media']],
            } for entry in snapshots]
            # Upgrade older snapshots that retained unnecessary Instagram metadata.
            atomic_json(cached, {'complete': True, 'read_at': snapshot.get('read_at'), 'collections': snapshots})
        else:
            client, _ = connect(sessions[0], '')
            snapshots = []
            for collection, city in selections:
                print(f"Читаю все страницы: {collection['name']}…", flush=True)
                items = [cached_media(item) for item in read_collection(client, collection['id'])]
                for item in items:
                    safe_media(item)
                snapshots.append({'collection': collection, 'city': city, 'media': items})
            atomic_json(DATA_ROOT/'batches'/'latest.json', {
                'complete': True, 'read_at': now(), 'collections': snapshots,
            })
        unique = {}
        for entry in snapshots:
            sanitized = [safe_media(item) for item in entry['media']]
            # Cached resumes must not resurrect old membership snapshots.
            if not cached:
                import_sources(entry['collection'], entry['city'], sanitized, complete=True)
            for item in entry['media']:
                unique.setdefault(str(item['pk']), cached_media(item))
        media = list(unique.values())
        found = len(media)
        if snapshot_only:
            status = 'finished'
            message = 'Все выбранные коллекции прочитаны; материалы сохранены для подготовки.'
            print(f'Найдено уникальных публикаций: {found}. Снимок сохранён.', flush=True)
            return 0
        with database() as db:
            states = {row['id']: row['status'] for row in db.execute('SELECT id,status FROM sources')}
        eligible = {'error'} if retry_errors else {'pending', 'error'}
        if cached and any(str(item['pk']) not in states for item in media):
            raise ProbeError('Снимок ещё не импортирован. Сначала выполни полный запуск со чтением коллекций.')
        queue = [item for item in media if states.get(str(item['pk'])) in eligible]
        limit = len(queue) if all_items else BUDGET
        print(f'Найдено: {found}. Требует подготовки: {len(queue)}. Лимит запуска: {limit}.', flush=True)
        progress_lock = Lock()

        def process(item):
            nonlocal processed, errors
            mid = str(item['pk'])
            try:
                packet = prepare_media(item)
            except AccessStopped:
                raise
            except Exception as exc:
                with progress_lock:
                    with database() as db:
                        db.execute("UPDATE sources SET status='error',error=? WHERE id=?", (type(exc).__name__, mid))
                        db.execute('UPDATE runs SET found=?,processed=?,errors=? WHERE id=?',
                                   (found, processed, errors+1, run))
                    errors += 1
                    print('Ошибка публикации:', type(exc).__name__, flush=True)
            else:
                with progress_lock:
                    with database() as db:
                        db.execute("UPDATE sources SET status='prepared',error=?,coverage=? WHERE id=?",
                                   ('; '.join(packet['errors']), json.dumps(packet['coverage'], ensure_ascii=False), mid))
                        db.execute('UPDATE runs SET found=?,processed=?,errors=? WHERE id=?',
                                   (found, processed+1, errors, run))
                    processed += 1
            with progress_lock:
                print(f'Завершено {processed+errors}/{min(len(queue), limit)}…', flush=True)

        prepare_parallel(queue[:limit], workers, process)
        remaining = max(0, len(queue)-limit)
        message += f' Осталось в очереди: {remaining}.'
        status = 'partial' if errors or remaining else 'finished'
        print(f'Подготовлено: {processed}. Ошибок: {errors}. Осталось: {remaining}.', flush=True)
        print('Передай подготовленные материалы Codex для извлечения и проверки мест.', flush=True)
        return 1 if errors else 0
    except KeyboardInterrupt:
        message = 'Запуск прерван. Успешно подготовленные публикации сохранены.'
        raise
    except AccessStopped:
        status = 'blocked'
        message = 'Instagram остановил доступ. Открой Instagram и заверши подтверждение входа; затем повтори подключение.'
        print(message, flush=True)
        return 1
    except Exception as exc:
        status = 'blocked'
        message = f'Обновление остановлено: {type(exc).__name__}. Успешные результаты сохранены.'
        print(message, flush=True)
        return 1
    finally:
        with database() as db:
            db.execute('UPDATE runs SET finished_at=?,status=?,found=?,processed=?,errors=?,message=? WHERE id=?',
                       (now(), status, found, processed, errors, message, run))


def main():
    parser = argparse.ArgumentParser(description='Обновить выбранные коллекции Saved Places')
    parser.add_argument('--configure', action='store_true', help='Выбрать коллекцию и город заново')
    parser.add_argument('--retry-errors', action='store_true', help='Подготовить только ранее неудачные публикации')
    parser.add_argument('--all', action='store_true', help='Подготовить все оставшиеся материалы без лимита 10')
    parser.add_argument('--config', type=Path, help='Файл списка выбранных коллекций')
    parser.add_argument('--cached', type=Path, help='Продолжить подготовку полного снимка без чтения Instagram')
    parser.add_argument('--snapshot-only', action='store_true', help='Только прочитать и сохранить полный снимок')
    parser.add_argument('--workers', type=int, choices=(1, 2), default=1, help='Число параллельных локальных обработчиков')
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    with preparation_lock():
        selections = selected_collections(args.configure, args.config)
        raise SystemExit(update_many(selections, args.retry_errors, args.all, args.cached, args.snapshot_only, args.workers))


if __name__ == '__main__':
    main()
