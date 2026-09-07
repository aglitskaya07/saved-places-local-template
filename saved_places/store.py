"""SQLite persistence. Extracted data and the owner's edits have separate ownership."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import unicodedata

from pydantic import BaseModel, Field, ConfigDict
from typing import Literal

from saved_places.probe import DATA_ROOT

CATEGORIES = ('еда', 'кофе и выпечка', 'бары', 'культура', 'магазины', 'прогулки', 'другие места')


class PlaceInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=200)
    city: str = Field(min_length=1, max_length=150)
    category: Literal['еда', 'кофе и выпечка', 'бары', 'культура', 'магазины', 'прогулки', 'другие места']
    district: str = Field(default='', max_length=200)
    address: str = Field(default='', max_length=400)
    description: str = Field(default='', max_length=1500)
    recommendation: str = Field(default='', max_length=1500)
    evidence: str = Field(min_length=1, max_length=1500)
    timestamp: str = Field(default='', max_length=30)


class Extraction(BaseModel):
    model_config = ConfigDict(extra='forbid')
    places: list[PlaceInput] = Field(default_factory=list, max_length=50)
    coverage: str = Field(max_length=500)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def identity(*parts: str) -> str:
    return hashlib.sha256('\0'.join(parts).encode()).hexdigest()[:24]


def normalize(value: str) -> str:
    return ' '.join(unicodedata.normalize('NFKC', value).casefold().split())


@contextmanager
def database(path: Path | None = None):
    path = path or DATA_ROOT / 'guide.sqlite3'
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    conn = sqlite3.connect(path, timeout=15)
    path.chmod(0o600)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('PRAGMA journal_mode=WAL')
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db(path: Path | None = None):
    with database(path) as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS cities(id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL);
        CREATE TABLE IF NOT EXISTS collections(id TEXT PRIMARY KEY, name TEXT NOT NULL,
            city_id TEXT NOT NULL REFERENCES cities(id), checked_at TEXT);
        CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY, url TEXT NOT NULL,
            caption TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending',
            error TEXT NOT NULL DEFAULT '', coverage TEXT NOT NULL DEFAULT '',
            extracted_json TEXT, added_at TEXT NOT NULL, processed_at TEXT);
        CREATE TABLE IF NOT EXISTS memberships(collection_id TEXT REFERENCES collections(id),
            source_id TEXT REFERENCES sources(id), present INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY(collection_id, source_id));
        CREATE TABLE IF NOT EXISTS places(id TEXT PRIMARY KEY, city_id TEXT REFERENCES cities(id),
            name TEXT NOT NULL, category TEXT NOT NULL, district TEXT NOT NULL DEFAULT '',
            address TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '',
            added_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            edit_name TEXT, edit_district TEXT, edit_address TEXT,
            note TEXT NOT NULL DEFAULT '', visit_status TEXT NOT NULL DEFAULT 'want',
            verification TEXT NOT NULL DEFAULT 'needs_review', verification_json TEXT,
            verified_at TEXT);
        CREATE TABLE IF NOT EXISTS evidence(place_id TEXT REFERENCES places(id),
            source_id TEXT REFERENCES sources(id), quote TEXT NOT NULL,
            timestamp TEXT NOT NULL DEFAULT '', recommendation TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(place_id,source_id));
        CREATE TABLE IF NOT EXISTS place_details(
            place_id TEXT PRIMARY KEY REFERENCES places(id) ON DELETE CASCADE,
            payload TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL,
            found INTEGER DEFAULT 0, processed INTEGER DEFAULT 0,
            added INTEGER DEFAULT 0, errors INTEGER DEFAULT 0, message TEXT DEFAULT '');
        ''')


def import_sources(collection: dict, city: str, media: list[dict], complete: bool,
                   path: Path | None = None):
    """Membership removals commit only with a complete, successfully read collection."""
    city_id = identity(normalize(city))
    with database(path) as db:
        db.execute('INSERT OR IGNORE INTO cities VALUES (?,?)', (city_id, city))
        db.execute('''INSERT INTO collections(id,name,city_id,checked_at) VALUES(?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET name=excluded.name,city_id=excluded.city_id,
            checked_at=excluded.checked_at''', (collection['id'], collection['name'], city_id, now()))
        if complete:
            db.execute('UPDATE memberships SET present=0 WHERE collection_id=?', (collection['id'],))
        for m in media:
            db.execute('''INSERT INTO sources(id,url,caption,added_at) VALUES(?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET caption=excluded.caption''',
                (m['id'], m['url'], m.get('caption',''), now()))
            db.execute('''INSERT INTO memberships VALUES(?,?,1) ON CONFLICT(collection_id,source_id)
                DO UPDATE SET present=1''', (collection['id'], m['id']))


def apply_extraction(source_id: str, extraction: Extraction, city: str,
                     path: Path | None = None) -> int:
    with database(path) as db:
        return apply_extraction_in_db(db, source_id, extraction, city)


def apply_extraction_in_db(db: sqlite3.Connection, source_id: str,
                           extraction: Extraction, city: str) -> int:
    """Apply one source in the caller's transaction, including optional verification."""
    added = 0
    if not db.execute('SELECT id FROM sources WHERE id=?', (source_id,)).fetchone():
        raise ValueError('Unknown source')
    city_id = identity(normalize(city))
    if not db.execute('''SELECT 1 FROM memberships m JOIN collections c ON c.id=m.collection_id
        WHERE m.source_id=? AND c.city_id=?''', (source_id, city_id)).fetchone():
        raise ValueError('Source does not belong to this city')
    if any(normalize(p.city) != normalize(city) for p in extraction.places):
        raise ValueError('Extracted place city differs from the collection city')
    previous_ids = {row[0] for row in db.execute(
        '''SELECT e.place_id FROM evidence e JOIN places p ON p.id=e.place_id
           WHERE e.source_id=? AND p.city_id=?''', (source_id, city_id))}
    current_ids = set()
    for p in extraction.places:
        # Without an address, merging names could conflate branches: keep source-local candidates.
        place_id = identity(city_id, normalize(p.name), normalize(p.address) or source_id)
        current_ids.add(place_id)
        exists = db.execute('SELECT 1 FROM places WHERE id=?', (place_id,)).fetchone()
        db.execute('''INSERT INTO places(id,city_id,name,category,district,address,description,added_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
            description=excluded.description,updated_at=excluded.updated_at''',
            (place_id,city_id,p.name,p.category,p.district,p.address,p.description,now(),now()))
        db.execute('''INSERT INTO evidence VALUES(?,?,?,?,?) ON CONFLICT(place_id,source_id)
            DO UPDATE SET quote=excluded.quote,timestamp=excluded.timestamp,
            recommendation=excluded.recommendation''',
            (place_id,source_id,p.evidence,p.timestamp,p.recommendation))
        added += int(not exists)
    # Reprocessing replaces this source's assertions in this city, retaining other cities and shared cards
    # and any card on which the owner has made a personal decision or correction.
    for removed_id in previous_ids - current_ids:
        db.execute('DELETE FROM evidence WHERE place_id=? AND source_id=?',
                   (removed_id, source_id))
        if not db.execute('SELECT 1 FROM evidence WHERE place_id=?', (removed_id,)).fetchone():
            db.execute('DELETE FROM place_details WHERE place_id=?', (removed_id,))
            db.execute('''DELETE FROM places WHERE id=? AND note='' AND visit_status='want'
                AND edit_name IS NULL AND edit_district IS NULL AND edit_address IS NULL''',
                (removed_id,))
            db.execute('''UPDATE places SET verification='needs_review',verification_json=NULL,
                verified_at=NULL WHERE id=?''', (removed_id,))
    db.execute('''UPDATE sources SET status='done',error='',coverage=?,extracted_json=?,
        processed_at=? WHERE id=?''', (extraction.coverage,extraction.model_dump_json(),now(),source_id))
    return added
