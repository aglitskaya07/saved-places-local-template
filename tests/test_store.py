import pytest
from saved_places.store import database,init_db,import_sources,apply_extraction,Extraction


@pytest.fixture
def db_path(tmp_path):
    p=tmp_path/'test.sqlite3'
    init_db(p)
    import_sources({'id':'1','name':'Copenhagen'},'Копенгаген',
        [{'id':'a','url':'https://www.instagram.com/p/a/'},{'id':'b','url':'https://www.instagram.com/p/b/'}],True,p)
    return p


def extracted(address='1 Main Street'):
    return Extraction.model_validate({'coverage':'Видео и описание', 'places':[
        {'name':'Cafe','city':'Копенгаген','category':'еда','address':address,'evidence':'Cafe, 1 Main Street'}]})


def test_same_place_two_reels_and_notes_survive(db_path):
    assert apply_extraction('a',extracted(),'Копенгаген',db_path)==1
    with database(db_path) as db:
        db.execute("UPDATE places SET note='my note',visit_status='visited',edit_name='My cafe'")
    assert apply_extraction('b',extracted(),'Копенгаген',db_path)==0
    assert apply_extraction('a',extracted(),'Копенгаген',db_path)==0
    with database(db_path) as db:
        assert db.execute('SELECT count(*) FROM places').fetchone()[0]==1
        assert db.execute('SELECT count(*) FROM evidence').fetchone()[0]==2
        p=db.execute('SELECT * FROM places').fetchone()
        assert (p['note'],p['visit_status'],p['edit_name'])==('my note','visited','My cafe')
        assert p['verification']=='needs_review'


def test_distinct_branches_and_unknown_addresses(db_path):
    apply_extraction('a',extracted('address 1'),'Копенгаген',db_path)
    apply_extraction('b',extracted('address 2'),'Копенгаген',db_path)
    with database(db_path) as db:
        assert db.execute('SELECT count(*) FROM places').fetchone()[0]==2


def test_unknown_addresses_stay_source_local(db_path):
    apply_extraction('a',extracted(''),'Копенгаген',db_path)
    apply_extraction('b',extracted(''),'Копенгаген',db_path)
    apply_extraction('a',extracted(''),'Копенгаген',db_path)
    with database(db_path) as db:
        assert db.execute('SELECT count(*) FROM places').fetchone()[0]==2
        assert db.execute('SELECT count(*) FROM evidence').fetchone()[0]==2
        assert db.execute("SELECT count(*) FROM places WHERE verification='checked'").fetchone()[0]==0


def test_only_complete_collection_can_mark_removal(db_path):
    import_sources({'id':'1','name':'Copenhagen'},'Копенгаген',[],False,db_path)
    with database(db_path) as db:
        assert db.execute('SELECT sum(present) FROM memberships').fetchone()[0]==2
    import_sources({'id':'1','name':'Copenhagen'},'Копенгаген',[],True,db_path)
    with database(db_path) as db:
        assert db.execute('SELECT sum(present) FROM memberships').fetchone()[0]==0
        assert db.execute('SELECT count(*) FROM sources').fetchone()[0]==2


def test_multiple_places_and_missing_evidence(db_path):
    data=extracted().model_dump()
    data['places'].append({**data['places'][0],'name':'Bakery'})
    assert apply_extraction('a',Extraction.model_validate(data),'Копенгаген',db_path)==2
    data['places'][0]['evidence']=''
    with pytest.raises(ValueError): Extraction.model_validate(data)


def test_address_correction_replaces_stale_source_link(db_path):
    apply_extraction('a',extracted('Wrong address'),'Копенгаген',db_path)
    apply_extraction('a',extracted('Right address'),'Копенгаген',db_path)
    with database(db_path) as db:
        assert db.execute('SELECT count(*) FROM places').fetchone()[0]==1
        assert db.execute('SELECT count(*) FROM evidence').fetchone()[0]==1
        assert db.execute('SELECT address FROM places').fetchone()[0]=='Right address'


def test_removed_assertion_keeps_shared_card_and_owner_edits(db_path):
    apply_extraction('a',extracted(),'Копенгаген',db_path)
    apply_extraction('b',extracted(),'Копенгаген',db_path)
    empty=Extraction(places=[],coverage='No location supported')
    apply_extraction('a',empty,'Копенгаген',db_path)
    with database(db_path) as db:
        assert db.execute('SELECT count(*) FROM places').fetchone()[0]==1
        assert db.execute('SELECT source_id FROM evidence').fetchone()[0]=='b'
        db.execute("UPDATE places SET note='My note', edit_address='My branch', verification='checked', verified_at='old'")
    apply_extraction('b',empty,'Копенгаген',db_path)
    with database(db_path) as db:
        place=db.execute('SELECT * FROM places').fetchone()
        assert place['note']=='My note'
        assert place['edit_address']=='My branch'
        assert place['verification']=='needs_review'
        assert place['verified_at'] is None
        assert db.execute('SELECT count(*) FROM evidence').fetchone()[0]==0


def test_other_city_is_not_silently_misclassified(db_path):
    data=extracted().model_dump()
    data['places'][0]['city']='Нью-Йорк'
    with pytest.raises(ValueError,match='city differs'):
        apply_extraction('a',Extraction.model_validate(data),'Копенгаген',db_path)
    with pytest.raises(ValueError,match='does not belong'):
        apply_extraction('a',extracted(),'Нью-Йорк',db_path)


def test_same_source_two_cities_preserves_both_and_scopes_reprocessing(db_path):
    import_sources({'id': 'nyc', 'name': 'NYC'}, 'Нью-Йорк',
                   [{'id': 'a', 'url': 'https://www.instagram.com/p/a/'}], True, db_path)
    apply_extraction('a', extracted(), 'Копенгаген', db_path)
    with database(db_path) as db:
        copenhagen_id = db.execute('SELECT id FROM places').fetchone()[0]
        db.execute("UPDATE places SET note='Copenhagen memory',visit_status='visited'")
    nyc = extracted().model_dump()
    nyc['places'][0].update(city='Нью-Йорк', address='New York branch')
    apply_extraction('a', Extraction.model_validate(nyc), 'Нью-Йорк', db_path)
    with database(db_path) as db:
        assert db.execute('SELECT count(*) FROM evidence WHERE source_id=?', ('a',)).fetchone()[0] == 2
        assert db.execute('SELECT note FROM places WHERE id=?', (copenhagen_id,)).fetchone()[0] == 'Copenhagen memory'
    apply_extraction('a', Extraction(places=[], coverage='No NYC place'), 'Нью-Йорк', db_path)
    with database(db_path) as db:
        assert db.execute('SELECT count(*) FROM evidence').fetchone()[0] == 1
        assert db.execute('SELECT place_id FROM evidence').fetchone()[0] == copenhagen_id
        assert db.execute('SELECT visit_status FROM places').fetchone()[0] == 'visited'
