"""Stage 0 only. Read a bounded collection sample; never publish or change saves."""
from __future__ import annotations

import argparse
import contextlib
from datetime import datetime, timezone
import fcntl
from functools import partial
import getpass
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from urllib.parse import urlparse

from instagrapi import Client
from instagrapi.exceptions import TwoFactorRequired
import requests


# Each checkout has its own local data and authentication namespace.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROJECT_KEY = hashlib.sha256(str(PROJECT_ROOT).encode()).hexdigest()[:12]
DATA_ROOT = Path(os.environ.get("SAVED_PLACES_DATA_DIR", str(Path.home() / "Library" / "Application Support" / "Saved Places Local" / PROJECT_KEY)))
SESSION_ROOT = DATA_ROOT / "private"



class AccessStopped(Exception):
    """Safe exception carrying only a class name, never an API response or token."""


class ProbeError(ValueError):
    """A user-facing error written by this app, safe to include in reports."""


class ProbeClient(Client):
    """Pinned SDK's request encoder, without its implicit retries or challenges."""

    def __init__(self):
        super().__init__(session_retry_total=0, public_request_retries_count=1)
        self.private.request = partial(self.private.request, timeout=(10, 20), allow_redirects=False)

    def private_request(self, endpoint, data=None, params=None, login=False,
                        with_signature=True, headers=None, extra_sig=None, domain=None):
        headers = dict(headers or {})
        if self.authorization:
            headers.setdefault("Authorization", self.authorization)
        # SDK errors sometimes contain response bodies. Never expose those to the terminal.
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.private_requests_count += 1
                self._send_private_request(
                    endpoint, data=data, params=params, login=login,
                    with_signature=with_signature, headers=headers,
                    extra_sig=extra_sig, domain=domain,
                )
        except TwoFactorRequired:
            raise
        except Exception as exc:
            raise AccessStopped(type(exc).__name__) from None
        return self.last_json

    def login_flow(self):
        # The SDK normally fetches the personal feed after login. We need only collections.
        return True


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def cursor_after(result: dict, seen: set[str]) -> str:
    cursor = str(result.get("next_max_id") or result.get("max_id") or "")
    more = result.get("more_available", bool(cursor))
    if not more:
        return ""
    if not cursor or cursor in seen:
        raise ProbeError("Instagram вернул неполную или зацикленную страницу. Чтение остановлено.")
    seen.add(cursor)
    return cursor


def list_collections(client: Client) -> list[dict]:
    # SDK collections() swallows any error and returns a partial list. Fail closed instead.
    collections: dict[str, dict] = {}
    cursor, seen = "", set()
    for _ in range(100):
        result = client.private_request("collections/list/", params={
            "collection_types": '["MEDIA"]', "max_id": cursor,
        })
        for item in result["items"]:
            identifier = str(item["collection_id"])
            if not identifier.isdigit():
                continue
            collections[identifier] = {"id": identifier, "name": item["collection_name"]}
        cursor = cursor_after(result, seen)
        if not cursor:
            return list(collections.values())
    raise ProbeError("Слишком много страниц коллекций. Чтение остановлено.")


def read_sample(client: Client, collection_id: str, limit: int = 5) -> tuple[list[dict], bool]:
    if not collection_id.isdigit() or not 1 <= limit <= 15:
        raise ProbeError("Для проверки нужна конкретная коллекция и от 1 до 15 публикаций.")
    items: dict[str, dict] = {}
    cursor, seen = "", set()
    for _ in range(3):
        result = client.private_request(f"feed/collection/{collection_id}/", params={
            "include_igtv_preview": "false", "max_id": cursor,
        })
        for entry in result["items"]:
            media = entry.get("media", entry)
            items[str(media["pk"])] = media
        cursor = cursor_after(result, seen)
        if len(items) >= limit or not cursor:
            complete = not cursor and len(items) <= limit
            return list(items.values())[:limit], complete
    return list(items.values())[:limit], False


def safe_media(media: dict) -> dict:
    code = str(media.get("code", ""))
    if not re.fullmatch(r"[A-Za-z0-9_-]+", code):
        raise ProbeError("Некорректная ссылка публикации в ответе Instagram.")
    caption = media.get("caption") or {}
    return {
        "id": str(media["pk"]), "url": f"https://www.instagram.com/p/{code}/",
        "caption": caption.get("text", ""), "media_type": media.get("media_type"),
        "video_present": bool(media.get("video_versions")),
    }


def record_sample(path: Path, media: list[dict]) -> list[str]:
    state = read_json(path)
    known = state.get("seen", {})
    sanitized = [safe_media(item) for item in media]
    new = [item["id"] for item in sanitized if item["id"] not in known]
    for item in sanitized:
        known[item["id"]] = item
    # A bounded sample NEVER establishes removal from a collection.
    atomic_json(path, {"seen": known, "last_checked": datetime.now(timezone.utc).isoformat()})
    return new


def validate_cdn_url(url: str) -> None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if (parsed.scheme != "https" or parsed.username or parsed.password
            or parsed.port not in (None, 443)
            or not any(host.endswith("." + domain) for domain in ("cdninstagram.com", "fbcdn.net"))):
        raise ProbeError("Видео недоступно по ожидаемому адресу Instagram.")


@contextlib.contextmanager
def downloaded_video(url: str):
    validate_cdn_url(url)
    # No Instagram session is attached to the CDN request. Download is temporary and bounded.
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="saved-places-video-") as folder:
        with requests.get(url, stream=True, timeout=(10, 20), allow_redirects=False) as response:
            if response.status_code != 200:
                raise ProbeError(f"CDN вернул HTTP {response.status_code}.")
            total, prefix = 0, b""
            with (Path(folder) / "sample.mp4").open("wb") as stream:
                for chunk in response.iter_content(64 * 1024):
                    total += len(chunk)
                    if total > 100 * 1024 * 1024 or time.monotonic() - started > 90:
                        raise ProbeError("Видео превысило лимит проверки: 100 МБ или 90 секунд.")
                    if len(prefix) < 32:
                        prefix += chunk[:32 - len(prefix)]
                    stream.write(chunk)
            if total < 12 or prefix[4:8] != b"ftyp":
                raise ProbeError("Ответ не содержит ожидаемый MP4-файл.")
            yield Path(folder) / 'sample.mp4'


def probe_video(url: str) -> int:
    with downloaded_video(url) as path:
        return path.stat().st_size


def terminal_text(value: str) -> str:
    return "".join(char if char.isprintable() else " " for char in str(value))


def connect(session_file: Path, account: str, force_login: bool = False) -> tuple[ProbeClient, bool]:
    client = ProbeClient()
    if session_file.exists() and not force_login:
        client.load_settings(session_file)
        client.set_retry_config(session_retry_total=0, public_request_retries_count=1)
        if not client.user_id:
            raise ProbeError("Сохранённая сессия неполная. Нужен запуск с --login.")
        print("Проверяю сохранённый вход…", flush=True)
        client.account_info()  # Do not call login(), which can silently attempt a new login.
        reused = True
    else:
        if session_file.exists():
            # Keep device identity on explicit reauthentication, clear only expired auth.
            settings = read_json(session_file)
            settings.update(authorization_data={}, cookies={}, last_login=None)
            client.set_settings(settings)
            client.set_retry_config(session_retry_total=0, public_request_retries_count=1)
        password = getpass.getpass("Пароль Instagram (символы не отображаются): ")
        code = getpass.getpass("Код из приложения 2FA, если включено; иначе Enter: ").strip()
        if not password:
            raise ProbeError("Пароль не введён.")
        print("Выполняю один вход…", flush=True)
        try:
            if not client.login(account, password, verification_code=code):
                raise ProbeError("Instagram не подтвердил вход.")
        finally:
            password, code = "", ""
            client.password = None
        reused = False
    atomic_json(session_file, client.get_settings())
    return client, reused


def run(args: argparse.Namespace, report: dict) -> None:
    if not sys.stdin.isatty():
        raise ProbeError("Откройте «Проверить Instagram.command»: ввод входа нужен в локальном Терминале.")
    print("Saved Places · проверка сохранённых коллекций\n")
    print(f"Проверим до {args.limit} публикаций и загрузим временно одно видео. Ничего не публикуется.")
    account = input("Имя пользователя Instagram: ").strip().lstrip("@").lower()
    if not re.fullmatch(r"[a-z0-9._]{1,30}", account):
        raise ProbeError("Некорректное имя пользователя.")
    account_key = hashlib.sha256(account.encode()).hexdigest()[:24]
    session_file = SESSION_ROOT / f"{account_key}.json"
    report["step"] = "login"
    client, reused = connect(session_file, account, args.login)
    report["session_reused"] = reused
    report["step"] = "collections"
    collections = list_collections(client)
    report["collections_read"] = len(collections)
    atomic_json(DATA_ROOT / "collections.json", {"collections": collections})
    if not collections:
        raise ProbeError("Instagram вернул пустой список коллекций.")
    print("\nДоступные коллекции:")
    for number, collection in enumerate(collections, 1):
        print(f"  {number}. {terminal_text(collection['name'])}")
    choice = input("\nНомер коллекции для проверки: ").strip()
    if not choice.isdigit() or not 1 <= int(choice) <= len(collections):
        raise ProbeError("Нужно выбрать номер из списка.")
    collection = collections[int(choice) - 1]
    report["collection"] = collection
    report["step"] = "sample"
    media, complete = read_sample(client, collection["id"], args.limit)
    report.update(sample_count=len(media), collection_complete=complete)
    report["media"] = [safe_media(item) for item in media]
    state_path = DATA_ROOT / "snapshots" / account_key / f"{collection['id']}.json"
    report["new_ids"] = record_sample(state_path, media)
    print(f"\nПрочитано: {len(media)}. Впервые обнаружено в выборке: {len(report['new_ids'])}.")
    video = next((item for item in media if item.get("video_versions")), None)
    report["step"] = "video"
    if video:
        print("Проверяю загрузку одного видео…", flush=True)
        try:
            report["video_bytes"] = probe_video(video["video_versions"][0]["url"])
            report["video_downloaded"] = True
        except Exception as exc:
            report["video_downloaded"] = False
            report["video_error"] = type(exc).__name__
            if isinstance(exc, ProbeError):
                report["video_error_hint"] = str(exc)
    else:
        report["video_downloaded"] = False
        report["video_error"] = "NoVideoInSample"
    report["status"] = "sample_passed" if report["video_downloaded"] else "partial"
    report["step"] = "finished"
    print("Видео получено и удалено после проверки." if report["video_downloaded"]
          else "Видео не получено. Причина отмечена в отчёте; проверка доступа пока неполная.")
    print("Для проверки изменений сохрани новый рилс в эту папку и запусти инструмент ещё раз.")
    print("Разбор мест и сайт ещё не запускались: сначала проверяем доступ.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Проверка доступа к своей Instagram-коллекции")
    parser.add_argument("--limit", type=int, default=5, choices=range(1, 16), metavar="1..15")
    parser.add_argument("--login", action="store_true", help="Выполнить новый вход вместо сохранённой сессии")
    args = parser.parse_args()
    os.umask(0o077)
    DATA_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    report = {"started_at": datetime.now(timezone.utc).isoformat(), "status": "started"}
    # Raw SDK logging can include personal data. Reports below are explicitly whitelisted.
    logging.disable(logging.CRITICAL)
    with (DATA_ROOT / "probe.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Проверка уже запущена в другом окне.")
            return 1
        exit_code = 0
        try:
            run(args, report)
        except (KeyboardInterrupt, EOFError):
            report["status"] = "cancelled"
            print("\nПроверка остановлена пользователем.")
            exit_code = 1
        except Exception as exc:
            report.update(status="blocked", error_type=(str(exc) if isinstance(exc, AccessStopped)
                                                       else type(exc).__name__))
            if isinstance(exc, ProbeError):
                report["error_hint"] = str(exc)
                print(str(exc))
            elif isinstance(exc, TwoFactorRequired):
                print("Нужен код 2FA. Повтори запуск с актуальным кодом из приложения.")
            else:
                print("Instagram или сеть остановили проверку. Автоматических повторов не будет.")
                print("Проверь вход в приложении Instagram. При ограничении запросов повтори позже.")
            print(f"Шаг: {report.get('step', 'start')}. Причина: {report['error_type']}.")
            exit_code = 1
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        report_path = DATA_ROOT / "reports" / f"{stamp}.json"
        atomic_json(report_path, report)
        atomic_json(DATA_ROOT / "latest-report.json", report)
        print(f"\nОтчёт сохранён: {report_path}")
        return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
