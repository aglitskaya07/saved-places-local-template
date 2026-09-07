import pytest

from saved_places.enrichment import Details, load_details, save_details
from saved_places.store import Extraction, apply_extraction, database, import_sources, init_db


@pytest.fixture
def place_db(tmp_path):
    path = tmp_path / 'guide.sqlite3'
    init_db(path)
    import_sources({'id': 'c', 'name': 'Copenhagen'}, 'Копенгаген',
                   [{'id': 's', 'url': 'https://www.instagram.com/p/example/'}], True, path)
    apply_extraction('s', Extraction.model_validate({'coverage': 'Caption', 'places': [{
        'name': 'Cafe', 'city': 'Копенгаген', 'category': 'еда', 'address': 'One Street', 'evidence': 'Cafe',
    }]}), 'Копенгаген', path)
    with database(path) as db:
        place_id = db.execute('SELECT id FROM places').fetchone()[0]
    return path, place_id


def coordinates():
    return {'latitude': 55.67, 'longitude': 12.56, 'coordinate_source': 'https://example.com/location'}


def reviews():
    return {'rating': 4.2, 'count': 50, 'summary': 'One reviewer mentions coffee.',
            'sample_count': 1, 'source_url': 'https://example.com/reviews', 'checked_at': '2026-09-07',
            'excerpts': [{'text': 'Lovely coffee.', 'rating': 4.0}]}


def test_valid_details_round_trip(place_db):
    path, place_id = place_db
    payload = {**coordinates(), 'guide': {'why_go': 'Coffee'}, 'reviews': reviews(),
               'image': {'asset_id': 'a'*64, 'source_url': 'https://example.com/image', 'caption': 'Entrance'}}
    save_details(place_id, payload, path)
    with database(path) as db:
        result = load_details(db)[place_id]
    assert result['latitude'] == 55.67
    assert result['reviews']['sample_count'] == 1
    assert result['guide']['planning_tip'] == ''


@pytest.mark.parametrize('payload', [
    {'latitude': 55.0},
    {'latitude': 55.0, 'longitude': 12.0},
    {**coordinates(), 'latitude': 100.0},
    {**coordinates(), 'longitude': float('nan')},
    {**coordinates(), 'coordinate_source': 'http://example.com'},
    {'image': {'asset_id': '../../secret', 'source_url': 'https://example.com'}},
    {'image': {'asset_id': 'a'*64, 'source_url': 'javascript:alert(1)'}},
])
def test_invalid_coordinates_and_assets_rejected(payload):
    with pytest.raises(ValueError):
        Details.model_validate(payload)


@pytest.mark.parametrize('change', [
    {'sample_count': 2}, {'count': 0}, {'rating': 6.0}, {'checked_at': 'sometime'},
    {'source_url': 'file:///secret'}, {'excerpts': []},
])
def test_review_summary_requires_consistent_saved_evidence(change):
    with pytest.raises(ValueError):
        Details.model_validate({'reviews': {**reviews(), **change}})


def test_aggregate_rating_without_review_text_is_allowed_but_summary_is_not():
    aggregate = {**reviews(), 'sample_count': 0, 'excerpts': [], 'summary': ''}
    Details.model_validate({'reviews': aggregate})
    with pytest.raises(ValueError):
        Details.model_validate({'reviews': {**aggregate, 'summary': 'Visitors love it'}})


def test_coordinates_cleared_when_location_is_overridden(place_db):
    path, place_id = place_db
    save_details(place_id, coordinates(), path)
    with database(path) as db:
        db.execute("UPDATE places SET edit_address='Another branch'")
        assert load_details(db)[place_id]['latitude'] is None
    save_details(place_id, coordinates(), path)
    with database(path) as db:
        details = Details.model_validate_json(db.execute('SELECT payload FROM place_details').fetchone()[0])
        assert details.longitude is None
        assert details.coordinate_source is None


def test_unknown_place_is_rejected_and_migration_is_repeatable(place_db):
    path, _ = place_db
    init_db(path)
    with pytest.raises(ValueError, match='Unknown place'):
        save_details('missing', {}, path)


def test_reprocessing_can_remove_enriched_unedited_place(place_db):
    path, place_id = place_db
    save_details(place_id, coordinates(), path)
    apply_extraction('s', Extraction(coverage='No supported place', places=[]), 'Копенгаген', path)
    with database(path) as db:
        assert load_details(db) == {}


def test_owner_note_survives_reprocessing_without_old_enrichment(place_db):
    path, place_id = place_db
    save_details(place_id, {**coordinates(), 'guide': {'why_go': 'Old recommendation'}}, path)
    with database(path) as db:
        db.execute("UPDATE places SET note='Personal memory'")
    apply_extraction('s', Extraction(coverage='No source place', places=[]), 'Копенгаген', path)
    with database(path) as db:
        assert load_details(db) == {}
        assert db.execute('SELECT count(*) FROM place_details').fetchone()[0] == 0
        assert db.execute('SELECT note FROM places').fetchone()[0] == 'Personal memory'
    with pytest.raises(ValueError, match='requires source evidence'):
        save_details(place_id, coordinates(), path)


def test_reading_legacy_orphan_details_does_not_render_claims(place_db):
    path, place_id = place_db
    save_details(place_id, coordinates(), path)
    with database(path) as db:
        db.execute('DELETE FROM evidence')
        assert load_details(db) == {}


def test_owner_location_override_hides_every_enrichment_claim(place_db):
    path, place_id = place_db
    save_details(place_id, {**coordinates(), 'guide': {'why_go': 'Old place', 'what_to_order': 'Old dish'},
                           'reviews': reviews(), 'image': {'asset_id': 'a'*64,
                           'source_url': 'https://example.com/photo', 'caption': 'Old interior'}}, path)
    with database(path) as db:
        db.execute("UPDATE places SET edit_name='Different establishment', note='Keep my note'")
        details = load_details(db)[place_id]
        assert details == Details().model_dump()
        assert db.execute('SELECT count(*) FROM evidence').fetchone()[0] == 1
        assert db.execute('SELECT note FROM places').fetchone()[0] == 'Keep my note'
