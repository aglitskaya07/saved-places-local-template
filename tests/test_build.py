import hashlib
import json
from pathlib import Path
import pytest
from PIL import Image
from saved_places.build import build
from saved_places.enrichment import save_details
from saved_places.store import database,init_db,import_sources,apply_extraction,Extraction


def fixture(root):
    path=root/'guide.sqlite3';init_db(path)
    import_sources({'id':'1','name':'Test'},'Учебный город',[{'id':'s','url':'https://www.instagram.com/p/test/'}],True,path)
    apply_extraction('s',Extraction(coverage='Искусственный пример',places=[dict(name=name,city='Учебный город',category='другие места',evidence='<script>bad()</script>') for name in ['История станции','Secret hidden','<script>alert(1)</script>']]),'Учебный город',path)
    with database(path) as db:
        db.execute("UPDATE places SET visit_status='hidden' WHERE name='Secret hidden'")
        pid=db.execute("SELECT id FROM places WHERE name='История станции'").fetchone()[0]
    return path,pid


def test_empty_build_and_isolated_storage(tmp_path):
    from saved_places.probe import SESSION_ROOT,DATA_ROOT
    assert 'Saved Places Local' in str(DATA_ROOT)
    assert SESSION_ROOT==DATA_ROOT/'private'
    path=build(tmp_path/'site',tmp_path/'data/db',tmp_path/'data')
    assert 'Мест: 0' in path.read_text()
    with database(tmp_path/'data/db') as db:assert db.execute('SELECT COUNT(*) FROM cities').fetchone()[0]==0


def test_static_export_escapes_private_sources_and_preserves_previous_output(tmp_path):
    root=tmp_path/'data';root.mkdir();db,pid=fixture(root)
    (root/'images').mkdir();photo=root/'sample.jpg';Image.new('RGB',(10,10),'white').save(photo)
    body=photo.read_bytes();asset=hashlib.sha256(body).hexdigest();(root/'images'/(asset+'.jpg')).write_bytes(body)
    save_details(pid,{'content_kind':'city_story','image':{'asset_id':asset,'source_url':'https://example.org/photo','caption':'Test image'}},db)
    output=build(tmp_path/'site',db,root);html=output.read_text()
    assert 'Secret hidden' not in html
    assert '<script>bad()' not in html and '&lt;script&gt;' in html
    assert 'data-moods="history"' in html
    assert (output.parent/'assets/images'/(asset+'.jpg')).read_bytes()==body
    assert '/static/' not in (output.parent/'assets/fonts/fonts.css').read_text()
    (root/'images'/(asset+'.jpg')).write_bytes(b'broken')
    with pytest.raises(ValueError):build(output.parent,db,root)
    assert output.read_text()==html


def test_build_does_not_overwrite_an_unrelated_directory(tmp_path):
    out=tmp_path/'existing';out.mkdir();(out/'index.html').write_text('my document')
    with pytest.raises(ValueError):build(out,tmp_path/'db',tmp_path)
    assert (out/'index.html').read_text()=='my document'
