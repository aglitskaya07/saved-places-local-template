"""Small, explainable collections built from the owner's visible places."""
import re
from urllib.parse import urlencode

MOODS = (
    ('history', 'Истории города', 'История, архитектура и необычные факты о городе.', ()),
    ('coffee', 'Кофе и выпечка', 'Пауза за чашкой кофе и что-нибудь к ней.', ('кофе и выпечка',)),
    ('food', 'Поесть в городе', 'Ужин, обед или небольшая гастрономическая остановка.', ('еда',)),
    ('evening', 'Вечер в городе', 'Бары и места с музыкой из твоих сохранений.', ('бары',)),
    ('culture', 'Посмотреть что-то новое', 'Музеи, искусство и культурные места.', ('культура',)),
    ('shopping', 'Привезти что-то домой', 'Книжные, небольшие магазины и необычные покупки.', ('магазины',)),
    ('hands', 'Сделать что-то руками', 'Мастер-классы и творческие занятия.', ()),
    ('walk', 'Выйти на прогулку', 'Парки, вода и прогулочные остановки.', ('прогулки',)),
    ('rest', 'Отдохнуть и перезагрузиться', 'Отели, бани, спорт и другие идеи для себя.', ('другие места',)),
)
def mood_matches(place, key, details=None):
    is_story = (details or {}).get(place['id'], {}).get('content_kind') == 'city_story'
    if key == 'history':
        return is_story
    if is_story:
        return False
    if key == 'hands':
        text = (place['title'] + ' ' + place['description']).casefold()
        return any(word in text for word in ('мастер-класс', 'мастер класс', 'создать аромат',
                    'создание аромата', 'сделать аромат', 'своими руками', 'творчеств', 'индивидуальный аромат', 'творческ', 'custom perfume'))
    categories = next((m[3] for m in MOODS if m[0] == key), ())
    return place['category'] in categories


def browse_groups(places, details, city_id):
    def group(key, title, description, members):
        images = [details.get(p['id'], {}).get('image') for p in members
                  if details.get(p['id'], {}).get('image')]
        image = next((im for im in images if 'instagram.com/' not in im.get('source_url', '')), images[0] if images else None)
        return dict(key=key, title=title, description=description, count=len(members), image=image,
                    url=f'/city/{city_id}?' + urlencode({'view': 'moods', 'mood': key}))
    moods = [group(key, title, description, members)
             for key, title, description, _ in MOODS
             if (members := [p for p in places if mood_matches(p, key, details)])]
    return moods


def short_reason(place, details):
    text = details.get(place['id'], {}).get('guide', {}).get('why_go') or place['description']
    sentence = re.split(r'(?<=[.!?])\s+', text.strip(), maxsplit=1)[0]
    return sentence if len(sentence) <= 180 else sentence[:177].rsplit(' ', 1)[0] + '…'
