"""Validated, source-attributed place enrichment without invented coordinates or reviews."""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from saved_places.store import database, normalize, now


class EnrichmentModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


def https_url(value: str) -> str:
    parsed = urlparse(value)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.port not in (None, 443) or any(char.isspace() for char in value)):
        raise ValueError('An HTTPS source URL is required')
    return value


class PlaceImage(EnrichmentModel):
    asset_id: Annotated[str, Field(pattern=r'^[a-f0-9]{64}$')]
    source_url: Annotated[str, Field(max_length=2000)]
    caption: Annotated[str, Field(max_length=1000)] = ''

    _source_url = field_validator('source_url')(https_url)


class PracticalGuide(EnrichmentModel):
    why_go: Annotated[str, Field(max_length=2000)] = ''
    what_to_order: Annotated[str, Field(max_length=2000)] = ''
    planning_tip: Annotated[str, Field(max_length=2000)] = ''


class ReviewExcerpt(EnrichmentModel):
    text: Annotated[str, Field(min_length=1, max_length=1500)]
    rating: Annotated[float, Field(ge=0, le=5, allow_inf_nan=False)] | None = None
    date: Annotated[str, Field(max_length=80)] | None = None

    @field_validator('text')
    @classmethod
    def nonempty_excerpt(cls, value):
        if not value.strip():
            raise ValueError('A sampled review requires actual excerpt text')
        return value


class ReviewSummary(EnrichmentModel):
    rating: Annotated[float, Field(ge=0, le=5, allow_inf_nan=False)] | None = None
    count: Annotated[int, Field(ge=0)]
    summary: Annotated[str, Field(max_length=2500)] = ''
    sample_count: Annotated[int, Field(ge=0, le=100)] = 0
    source_url: Annotated[str, Field(max_length=2000)]
    checked_at: Annotated[str, Field(min_length=1, max_length=80)]
    excerpts: list[ReviewExcerpt] = Field(default_factory=list, max_length=100)

    _source_url = field_validator('source_url')(https_url)

    @field_validator('checked_at')
    @classmethod
    def checked_date(cls, value):
        try:
            date.fromisoformat(value)
        except ValueError:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if parsed.tzinfo is None:
                raise ValueError('Review check timestamp requires a timezone')
        return value

    @model_validator(mode='after')
    def sample_is_evidenced(self):
        if self.sample_count != len(self.excerpts):
            raise ValueError('Each sampled review requires its own saved excerpt')
        if self.sample_count > self.count:
            raise ValueError('Sample count cannot exceed total review count')
        if self.summary.strip() and self.sample_count == 0:
            raise ValueError('A review summary requires fetched review excerpts')
        if self.rating is not None and self.count == 0:
            raise ValueError('A rating requires at least one counted review')
        return self


class Details(EnrichmentModel):
    latitude: Annotated[float, Field(ge=-90, le=90, allow_inf_nan=False)] | None = None
    longitude: Annotated[float, Field(ge=-180, le=180, allow_inf_nan=False)] | None = None
    coordinate_source: Annotated[str, Field(max_length=2000)] | None = None
    image: PlaceImage | None = None
    guide: PracticalGuide = Field(default_factory=PracticalGuide)
    reviews: ReviewSummary | None = None
    review_note: Annotated[str, Field(max_length=1000)] = ''
    content_kind: Literal['place', 'city_story'] = 'place'

    @field_validator('coordinate_source')
    @classmethod
    def valid_coordinate_source(cls, value):
        return https_url(value) if value is not None else None

    @model_validator(mode='after')
    def coordinates_have_source(self):
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError('Latitude and longitude must be supplied together')
        if (self.latitude is None) != (self.coordinate_source is None):
            raise ValueError('Coordinates require their source URL')
        return self


def without_overridden_coordinates(details: Details, place) -> Details:
    """Enrichment describes the extracted place, never an owner's different location."""
    if any(place['edit_'+field] is not None and
           normalize(place['edit_'+field]) != normalize(place[field])
           for field in ('name', 'address', 'district')):
        return Details()
    return details


def load_details(db) -> dict[str, dict]:
    result = {}
    for row in db.execute('''SELECT d.place_id,d.payload,p.name,p.address,p.district,
        p.edit_name,p.edit_address,p.edit_district FROM place_details d
        JOIN places p ON p.id=d.place_id
        WHERE EXISTS (SELECT 1 FROM evidence e WHERE e.place_id=p.id)'''):
        details = Details.model_validate_json(row['payload'])
        result[row['place_id']] = without_overridden_coordinates(details, row).model_dump()
    return result


def save_details(place_id: str, payload: dict | Details, path: Path | None = None):
    details = payload if isinstance(payload, Details) else Details.model_validate(payload)
    with database(path) as db:
        place = db.execute('SELECT * FROM places WHERE id=?', (place_id,)).fetchone()
        if place is None:
            raise ValueError('Unknown place')
        if not db.execute('SELECT 1 FROM evidence WHERE place_id=?', (place_id,)).fetchone():
            raise ValueError('Place requires source evidence before enrichment')
        details = without_overridden_coordinates(details, place)
        db.execute('''INSERT INTO place_details(place_id,payload,updated_at) VALUES(?,?,?)
            ON CONFLICT(place_id) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at''',
            (place_id, details.model_dump_json(), now()))
