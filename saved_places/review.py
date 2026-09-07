"""Import a grounded Codex review. Files are data, never executable instructions."""
import argparse
import json
from pathlib import Path
from urllib.parse import urlparse

from pydantic import BaseModel,Field,ConfigDict,field_validator
from saved_places.store import Extraction,apply_extraction_in_db,database,init_db,now,normalize


class Reference(BaseModel):
    model_config=ConfigDict(extra='forbid')
    url:str
    title:str=Field(max_length=200)

    @field_validator('url')
    @classmethod
    def valid_url(cls,value):
        u=urlparse(value)
        if u.scheme!='https' or not u.hostname or u.username or u.password:
            raise ValueError('An HTTPS source URL is required')
        return value


class Verification(BaseModel):
    model_config=ConfigDict(extra='forbid')
    name:str
    summary:str=Field(min_length=1,max_length=1500)
    sources:list[Reference]=Field(min_length=1,max_length=10)


class Review(BaseModel):
    model_config=ConfigDict(extra='forbid')
    source_id:str
    city:str
    extraction:Extraction
    verified:list[Verification]=Field(default_factory=list)


def import_review(review:Review,path:Path|None=None):
    with database(path) as db:
        added=apply_extraction_in_db(db,review.source_id,review.extraction,review.city)
        for verification in review.verified:
            # Match only a place attached to this exact source; never confirm unrelated names.
            rows=db.execute('''SELECT p.* FROM places p JOIN evidence e ON e.place_id=p.id
                WHERE e.source_id=? AND p.name=?''',(review.source_id,verification.name)).fetchall()
            if len(rows)!=1: raise ValueError('Verification must identify exactly one place')
            place=rows[0]
            if any(place['edit_'+field] is not None and
                   normalize(place['edit_'+field]) != normalize(place[field])
                   for field in ('name','address','district')):
                # These references verified the extracted location, not an owner's override.
                db.execute('''UPDATE places SET verification='needs_review',verification_json=NULL,
                    verified_at=NULL WHERE id=?''',(place['id'],))
                continue
            db.execute("UPDATE places SET verification='checked',verification_json=?,verified_at=? WHERE id=?",
                       (verification.model_dump_json(),now(),rows[0]['id']))
    return added


def parse_reviews(content):
    if isinstance(content,dict) and set(content)=={'reviews'}:
        content=content['reviews']
    if not isinstance(content,list):
        raise ValueError('Expected a list of reviews or an object containing reviews')
    return [Review.model_validate(r) for r in content]


def main():
    parser=argparse.ArgumentParser(description='Добавить разбор сохранений на сайт')
    parser.add_argument('file',type=Path)
    args=parser.parse_args()
    if args.file.stat().st_size>2*1024*1024: raise SystemExit('Файл слишком большой')
    reviews=parse_reviews(json.loads(args.file.read_text()))
    init_db()
    total=sum(import_review(r) for r in reviews)
    print(f'Разобрано публикаций: {len(reviews)}. Новых мест: {total}.')


if __name__=='__main__':main()
