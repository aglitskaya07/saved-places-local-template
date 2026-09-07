import pytest

from saved_places.review import Review, import_review, parse_reviews
from saved_places.store import database, init_db, import_sources


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / 'guide.sqlite3'
    init_db(path)
    import_sources({'id': 'collection', 'name': 'Copenhagen'}, 'Копенгаген',
                   [{'id': 'a', 'url': 'https://www.instagram.com/p/a/'}], True, path)
    return path


def review(name='Cafe', address='1 Main Street', verified_name=None):
    return Review.model_validate({
        'source_id': 'a', 'city': 'Копенгаген',
        'extraction': {'coverage': 'Описание', 'places': [{
            'name': name, 'city': 'Копенгаген', 'category': 'еда',
            'address': address, 'evidence': name,
        }]},
        'verified': [{'name': verified_name or name, 'summary': 'Address confirmed',
                      'sources': [{'url': 'https://example.com/contact', 'title': 'Contact'}]}],
    })


def test_invalid_verification_rolls_back_entire_source(db_path):
    with pytest.raises(ValueError, match='exactly one place'):
        import_review(review(verified_name='Unknown cafe'), db_path)
    with database(db_path) as db:
        assert db.execute('SELECT count(*) FROM places').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM evidence').fetchone()[0] == 0
        source = db.execute('SELECT status,processed_at FROM sources').fetchone()
        assert tuple(source) == ('pending', None)


def test_failed_reprocessing_restores_previous_review(db_path):
    import_review(review(), db_path)
    with pytest.raises(ValueError):
        import_review(review(address='2 Main Street', verified_name='Unknown'), db_path)
    with database(db_path) as db:
        place = db.execute('SELECT address,verification FROM places').fetchone()
        assert tuple(place) == ('1 Main Street', 'checked')
        assert db.execute('SELECT count(*) FROM evidence').fetchone()[0] == 1


def test_manual_location_cannot_be_verified_by_extracted_references(db_path):
    import_review(review(), db_path)
    with database(db_path) as db:
        db.execute("UPDATE places SET edit_address='Another branch', note='Keep this', visit_status='visited'")
    import_review(review(), db_path)
    with database(db_path) as db:
        place = db.execute('SELECT * FROM places').fetchone()
        assert place['edit_address'] == 'Another branch'
        assert place['note'] == 'Keep this'
        assert place['visit_status'] == 'visited'
        assert place['verification'] == 'needs_review'
        assert place['verification_json'] is None
        assert place['verified_at'] is None


def test_review_file_accepts_list_and_envelope():
    item = review().model_dump()
    assert parse_reviews([item]) == parse_reviews({'reviews': [item]})
    with pytest.raises(ValueError):
        parse_reviews({'reviews': [item], 'unexpected': True})
