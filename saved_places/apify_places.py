"""Bounded Apify lookup; returned candidates require explicit place matching by the caller.

API budget parameter: https://docs.apify.com/api/v2/actors-runs-post
Actor input: https://apify.com/compass/crawler-google-places/input-schema
"""
from __future__ import annotations

import json
import math
import re
import time
from urllib.parse import urlparse

import requests

from saved_places.probe import SESSION_ROOT

API_ROOT = 'https://api.apify.com/v2'
TOKEN_FILE = SESSION_ROOT / 'providers' / 'apify.json'
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_RESULTS = 500


class ApifyError(RuntimeError):
    """Safe operational error, excluding credentials and provider response bodies."""


def _token() -> str:
    try:
        content = json.loads(TOKEN_FILE.read_text())
        token = content['token']
        if not isinstance(token, str) or not token or any(c.isspace() for c in token):
            raise ValueError
        return token
    except (OSError, ValueError, KeyError, TypeError):
        raise ApifyError('Apify credentials are unavailable or invalid') from None


def _request(method: str, route: str, *, params=None, payload=None):
    # No retries: an uncertain POST can have created a billable run already.
    token = _token()
    try:
        with requests.request(method, API_ROOT + route,
                              headers={'Authorization': 'Bearer ' + token},
                              params=params, json=payload, timeout=(10, 30),
                              allow_redirects=False, stream=True) as response:
            if response.status_code < 200 or response.status_code >= 300:
                raise ApifyError(f'Apify request failed (HTTP {response.status_code})')
            chunks = []
            size = 0
            deadline = time.monotonic() + 60
            for chunk in response.iter_content(chunk_size=65536):
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES or time.monotonic() > deadline:
                    raise ApifyError('Apify response exceeded the size or time limit')
                chunks.append(chunk)
            return json.loads(b''.join(chunks))
    except requests.RequestException:
        raise ApifyError('Apify request failed; do not automatically repeat a run submission') from None
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ApifyError('Apify returned an invalid JSON response') from None


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', value):
        raise ValueError('Invalid Apify identifier')
    return value


def _text(value, limit):
    return value[:limit] if isinstance(value, str) else None


def _number(value, minimum, maximum):
    if type(value) in (int, float) and math.isfinite(value) and minimum <= value <= maximum:
        return value
    return None


def _run_metadata(response):
    data = response.get('data') if isinstance(response, dict) else None
    if not isinstance(data, dict):
        raise ApifyError('Apify run metadata is missing')
    try:
        run_id = _identifier(data.get('id'))
        dataset_id = _identifier(data['defaultDatasetId']) if data.get('defaultDatasetId') else None
    except ValueError:
        raise ApifyError('Apify run metadata has invalid identifiers') from None
    status = data.get('status')
    if status not in {'READY', 'RUNNING', 'SUCCEEDED', 'FAILED', 'TIMING-OUT', 'TIMED-OUT', 'ABORTING', 'ABORTED'}:
        raise ApifyError('Apify run metadata has an unknown status')
    return {'id': run_id, 'status': status, 'defaultDatasetId': dataset_id,
            'startedAt': _text(data.get('startedAt'), 80), 'finishedAt': _text(data.get('finishedAt'), 80),
            'usageTotalUsd': _number(data.get('usageTotalUsd'), 0, 1000000)}


def start_run(queries: list[str], max_charge_usd=2) -> dict:
    """Submit one batch once. Caller records its ID before polling or submitting another."""
    if (not isinstance(queries, list) or not 1 <= len(queries) <= 50
            or any(not isinstance(q, str) or not q.strip() or len(q) > 500 for q in queries)):
        raise ValueError('Provide 1–50 nonempty place queries of at most 500 characters')
    if _number(max_charge_usd, 0.01, 2) is None:
        raise ValueError('The pilot run budget must be between 0.01 and 2 USD')
    payload = {'searchStringsArray': queries, 'maxCrawledPlacesPerSearch': 1,
               'maxReviews': 10, 'reviewsSort': 'newest', 'scrapeReviewsPersonalData': False,
               'maxImages': 0, 'scrapePlaceDetailPage': True, 'language': 'en'}
    return _run_metadata(_request('POST', '/acts/compass~crawler-google-places/runs',
                                 params={'maxTotalChargeUsd': max_charge_usd}, payload=payload))


def get_run(run_id: str) -> dict:
    """Fetch status once. The caller controls polling frequency and when to stop."""
    return _run_metadata(_request('GET', '/actor-runs/' + _identifier(run_id)))


def _https_url(value):
    if not isinstance(value, str) or len(value) > 3000:
        return None
    try:
        parsed = urlparse(value)
        if (parsed.scheme == 'https' and parsed.hostname and not parsed.username and not parsed.password
                and parsed.port in (None, 443) and not any(char.isspace() for char in value)):
            return value
    except ValueError:
        pass
    return None


def _place(row):
    if not isinstance(row, dict):
        raise ApifyError('Apify returned an invalid place record')
    location = row.get('location')
    location = location if isinstance(location, dict) else {}
    latitude = _number(location.get('lat'), -90, 90)
    longitude = _number(location.get('lng'), -180, 180)
    if latitude is None or longitude is None:
        latitude = longitude = None
    reviews = row.get('reviews')
    safe_reviews = []
    if isinstance(reviews, list):
        for review in reviews[:10]:
            if isinstance(review, dict):
                safe_reviews.append({'text': _text(review.get('text'), 20000),
                                     'stars': _number(review.get('stars'), 0, 5),
                                     'publishedAtDate': _text(review.get('publishedAtDate'), 80)})
    count = row.get('reviewsCount')
    return {'title': _text(row.get('title'), 400), 'address': _text(row.get('address'), 1000),
            'latitude': latitude, 'longitude': longitude, 'url': _https_url(row.get('url')),
            'totalScore': _number(row.get('totalScore'), 0, 5),
            'reviewsCount': count if type(count) is int and count >= 0 else None,
            'searchString': _text(row.get('searchString'), 500), 'reviews': safe_reviews}


def get_results(dataset_id: str) -> list[dict]:
    """Read a small finished batch. No reviewer names, IDs, photos, or raw records escape."""
    rows = _request('GET', '/datasets/' + _identifier(dataset_id) + '/items',
                    params={'format': 'json', 'clean': 'true', 'limit': MAX_RESULTS + 1})
    if not isinstance(rows, list) or len(rows) > MAX_RESULTS:
        raise ApifyError('Apify dataset exceeded the pilot result limit or has an invalid format')
    return [_place(row) for row in rows]
