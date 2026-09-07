"""Render a portable HTML guide. No server, account login or network calls."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from urllib.parse import urlencode, urlparse

from jinja2 import Environment, FileSystemLoader, select_autoescape
from PIL import Image

from saved_places.browse import MOODS, mood_matches
from saved_places.enrichment import load_details
from saved_places.probe import DATA_ROOT, PROJECT_ROOT
from saved_places.store import database, init_db


def safe_url(url):
    try:
        parsed = urlparse(url or '')
        return url if parsed.scheme == 'https' and parsed.hostname and not parsed.username and not parsed.password else '#'
    except ValueError:
        return '#'


def atomic_write(path, content):
    fd, temp = tempfile.mkstemp(prefix='.build-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(content)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp): os.unlink(temp)


def build(output=None, db_path=None, data_root=None):
    output = Path(output or PROJECT_ROOT/'guide')
    data_root = Path(data_root or DATA_ROOT)
    db_path = Path(db_path or data_root/'guide.sqlite3')
    marker = output/'.saved-places-output'
    if output.exists() and not marker.exists() and any(output.iterdir()):
        raise ValueError('Папка назначения не пуста и не создана Saved Places. Выбери другую папку.')
    init_db(db_path)
    with database(db_path) as db:
        db.execute('BEGIN')
        details = load_details(db)
        rows = [dict(r) for r in db.execute("""SELECT p.*, c.name city,
            coalesce(edit_name,p.name) title,coalesce(edit_district,district) area,
            coalesce(edit_address,address) full_address
            FROM places p JOIN cities c ON c.id=p.city_id
            WHERE visit_status!='hidden' ORDER BY c.name,p.added_at DESC,p.id""")]
        for p in rows:
            p['detail'] = details.get(p['id'], {})
            p['moods'] = [key for key, *_ in MOODS if mood_matches(p,key,details)]
            p['maps'] = 'https://www.google.com/maps/search/?' + urlencode({'api':1,'query':' '.join([p['title'],p['full_address'],p['city']])})
            p['evidence'] = [dict(r) for r in db.execute('SELECT e.*,s.url FROM evidence e JOIN sources s ON s.id=e.source_id WHERE e.place_id=?',(p['id'],))]
            p['verification_data'] = json.loads(p['verification_json'] or '{}')
    # Validate all media before replacing a working HTML file.
    assets = {}
    for p in rows:
        image = p['detail'].get('image')
        if not image: continue
        identifier = image['asset_id']
        source = data_root/'images'/(identifier+'.jpg')
        if source.is_symlink() or not source.is_file():
            raise ValueError('Отсутствует безопасный файл фотографии; предыдущий HTML сохранён.')
        if source.stat().st_size > 2*1024*1024:
            raise ValueError('Фотография превышает 2 МБ.')
        body = source.read_bytes()
        if hashlib.sha256(body).hexdigest() != identifier:
            raise ValueError('Контрольная сумма фотографии не совпадает.')
        with Image.open(source) as im:
            if im.format != 'JPEG' or im.width*im.height>20000000: raise ValueError('Ожидается JPEG.')
            im.verify()
        assets[identifier+'.jpg'] = body
    env = Environment(loader=FileSystemLoader(Path(__file__).parent/'templates'), autoescape=select_autoescape(['html']))
    env.filters['safe_url'] = safe_url
    html = env.get_template('guide.html').render(places=rows,cities=sorted({p['city'] for p in rows}),moods=MOODS)
    output.mkdir(parents=True,exist_ok=True)
    (output/'assets').mkdir(exist_ok=True)
    (output/'assets/images').mkdir(exist_ok=True)
    shutil.copytree(Path(__file__).parent/'assets',output/'assets',dirs_exist_ok=True)
    for name,body in assets.items(): atomic_write(output/'assets/images'/name,body)
    marker.touch(mode=0o600)
    atomic_write(output/'index.html',html.encode())
    return output/'index.html'


def main():
    parser=argparse.ArgumentParser(description='Собрать локальный HTML-гайд')
    parser.add_argument('--output',type=Path)
    parser.add_argument('--open',action='store_true')
    args=parser.parse_args()
    path=build(args.output)
    print('Гайд сохранён:',path)
    if args.open:
        import webbrowser
        webbrowser.open(path.resolve().as_uri())

if __name__=='__main__': main()
