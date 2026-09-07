import json

import pytest
import requests

from saved_places import apify_places as apify


class Response:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def iter_content(self, chunk_size):
        yield json.dumps(self.payload).encode()


@pytest.fixture
def calls(monkeypatch):
    collected = []
    monkeypatch.setattr(apify, '_token', lambda: 'test-only-secret')

    def request(method, url, **kwargs):
        collected.append((method, url, kwargs))
        return Response({'data': {'id': 'run123', 'status': 'RUNNING', 'defaultDatasetId': 'dataset123',
                                  'token': 'provider-secret', 'containerUrl': 'https://private.invalid'}})

    monkeypatch.setattr(apify.requests, 'request', request)
    return collected


def test_start_is_bounded_private_and_submitted_once(calls):
    result = apify.start_run(['Cafe Copenhagen'])
    assert result['id'] == 'run123'
    assert 'token' not in result
    assert 'containerUrl' not in result
    assert len(calls) == 1
    method, url, options = calls[0]
    assert method == 'POST'
    assert url == apify.API_ROOT + '/acts/compass~crawler-google-places/runs'
    assert 'test-only-secret' not in url
    assert options['headers']['Authorization'] == 'Bearer test-only-secret'
    assert options['params'] == {'maxTotalChargeUsd': 2}
    assert options['json']['maxReviews'] == 10
    assert options['json']['maxCrawledPlacesPerSearch'] == 1
    assert options['json']['scrapeReviewsPersonalData'] is False
    assert options['json']['reviewsSort'] == 'newest'
    assert options['allow_redirects'] is False
    assert options['timeout'] == (10, 30)


@pytest.mark.parametrize('budget', [-1, 0, 3, float('nan'), True, '2'])
def test_invalid_budget_never_sends_request(calls, budget):
    with pytest.raises(ValueError):
        apify.start_run(['Cafe Copenhagen'], budget)
    assert calls == []


def test_get_run_does_not_poll_automatically(calls):
    assert apify.get_run('run123')['status'] == 'RUNNING'
    assert len(calls) == 1
    assert calls[0][0] == 'GET'


def test_network_failure_is_sanitized_and_not_retried(monkeypatch):
    count = 0
    monkeypatch.setattr(apify, '_token', lambda: 'test-only-secret')

    def fail(*args, **kwargs):
        nonlocal count
        count += 1
        raise requests.Timeout('test-only-secret and private provider details')

    monkeypatch.setattr(apify.requests, 'request', fail)
    with pytest.raises(apify.ApifyError) as error:
        apify.start_run(['Cafe'])
    assert 'test-only-secret' not in str(error.value)
    assert count == 1


def test_results_remove_reviewer_identifiers_and_do_not_match_places(monkeypatch):
    row = {'title': 'Different Cafe', 'address': 'Another address', 'location': {'lat': 55.1, 'lng': 12.2},
           'url': 'https://www.google.com/maps/place/example', 'totalScore': 4.2, 'reviewsCount': 50,
           'searchString': 'Cafe Copenhagen', 'reviewerId': 'private',
           'reviews': [{'text': 'Coffee was good', 'stars': 4, 'publishedAtDate': '2026-01-01',
                        'reviewId': 'private', 'name': 'Person', 'reviewerUrl': 'https://private.invalid'}]}
    monkeypatch.setattr(apify, '_request', lambda *args, **kwargs: [row])
    results = apify.get_results('dataset123')
    assert len(results) == 1
    assert results[0]['title'] == 'Different Cafe'
    assert results[0]['searchString'] == 'Cafe Copenhagen'
    assert results[0]['latitude'] == 55.1
    assert results[0]['reviews'] == [{'text': 'Coffee was good', 'stars': 4, 'publishedAtDate': '2026-01-01'}]
    assert 'private' not in json.dumps(results)
    assert 'matched' not in results[0]


def test_malformed_coordinates_and_unavailable_ratings_stay_unknown(monkeypatch):
    monkeypatch.setattr(apify, '_request', lambda *args, **kwargs: [
        {'title': 'Cafe', 'location': {'lat': 55.0}, 'totalScore': float('nan'),
         'url': 'javascript:alert(1)', 'reviewsCount': '50'}])
    row = apify.get_results('dataset123')[0]
    assert row['latitude'] is None and row['longitude'] is None
    assert row['totalScore'] is None and row['reviewsCount'] is None and row['url'] is None


def test_bad_identifier_never_sends_request(calls):
    with pytest.raises(ValueError):
        apify.get_results('../secret?token=bad')
    assert calls == []


def test_http_error_does_not_return_provider_body(monkeypatch):
    monkeypatch.setattr(apify, '_token', lambda: 'test-only-secret')
    monkeypatch.setattr(apify.requests, 'request', lambda *args, **kwargs: Response({'secret': 'private'}, 401))
    with pytest.raises(apify.ApifyError, match='HTTP 401') as error:
        apify.get_run('run123')
    assert 'private' not in str(error.value)
