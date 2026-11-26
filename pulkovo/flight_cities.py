"""Local route parsing and exact city forms matched against the live board."""

import re


FLIGHT_WORD = re.compile(r'\b(?:рейс\w*|вылет\w*|прилет\w*|прибыт\w*|самолет\w*|расписание)\b')
NEAREST = re.compile(r'\b(?:ближайш\w*|следующ\w*|первый)\b')
OTHER_QUESTION = re.compile(r'багаж|билет|животн|собак|кошк|ребен|деть|правил|провез|поменять|пересад|сотрудник|оператор|почему|сколько лететь|сколько длится|длительност|времени занимает')
NOISE = re.compile(r'\b(?:какой|какие|когда|во сколько|ближайш\w*|следующ\w*|первый|рейс\w*|вылет\w*|прилет\w*|прибыт\w*|самолет\w*|расписание|летит|летят|лететь|покажи(?:те)?|посмотри(?:те)?|проверь(?:те)?|найди(?:те)?|подскажи(?:те)?|пожалуйста|хочу|узнать|есть|будет|будут|можно|ли|мне|а|дата)\b')
MULTI_FORMS = {
    'нижний новгород': ('нижнего новгорода', 'нижнему новгороду', 'нижнем новгороде'),
    'новый уренгой': ('нового уренгоя', 'новому уренгою', 'новом уренгое'),
    'минеральные воды': ('минеральных вод', 'минеральным водам', 'минводы', 'минвод'),
    'великий устюг': ('великого устюга', 'великом устюге'),
    'набережные челны': ('набережных челнов', 'набережных челнах'),
    'нарьян мар': ('нарьян мара',),
    'ростов на дону': ('ростова на дону',),
    'шарм эль шейх': ('шарм эль шейха',),
}
AIRPORT_NAMES = {
    'svo': ('шереметьево',), 'dme': ('домодедово',), 'vko': ('внуково',),
    'ist': ('стамбул новый', 'новый аэропорт стамбула'),
    'saw': ('сабиха гекчен',),
}


def city_key(value):
    return re.sub(r'[^a-zа-я0-9]+', ' ', value.casefold().replace('ё', 'е')).strip()


def city_forms(value):
    key = city_key(value)
    forms = {key}
    if key in MULTI_FORMS:
        return forms | set(MULTI_FORMS[key])
    if ' ' in key:
        return forms
    if key.endswith('а'):
        forms.update(key[:-1]+end for end in ('у', 'ы', 'и', 'е'))
    elif key.endswith('я'):
        forms.update(key[:-1]+end for end in ('ю', 'и', 'е'))
    elif key.endswith('й'):
        forms.update(key[:-1]+end for end in ('я', 'ю', 'е'))
    elif key.endswith('ь'):
        forms.update(key[:-1]+end for end in ('и', 'ю'))
    elif key and key[-1] in 'бвгджзклмнпрстфхцчшщ':
        forms.update(key+end for end in ('а', 'у', 'е'))
    return forms


def city_matches(requested, source):
    # Parenthesized airport identifiers stay visible in results, but a request
    # for Moscow matches all its airports. Never use fuzzy substring matching.
    base = re.sub(r'\s*\([^)]*\)', '', source)
    forms = city_forms(base) | {city_key(source)}
    airports = re.findall(r'\(([A-Z]{3})\)', source, re.I)
    for airport in airports:
        aliases = (airport.lower(), *AIRPORT_NAMES.get(airport.lower(), ()))
        forms.update(aliases)
        forms.update(f'{city} {alias}' for city in city_forms(base) for alias in aliases)
    return city_key(requested) in forms


def parse_city_query(text, rest, day, invalid, previous=None):
    context = previous if previous and previous.get('mode') == 'city' else None
    explicit = bool(FLIGHT_WORD.search(text))
    if OTHER_QUESTION.search(text) or re.search(r'\b(?:не|нет|другой|как|где|почему|кто|что|зачем)\b', text) or not (explicit or context):
        return None
    # A gate/counter needs a particular flight, not the whole city listing.
    if re.search(r'гейт|выход|стойк|лент|статус|обнови', text):
        return None
    if context and not explicit and not (day or invalid or NEAREST.search(text)
            or re.match(r'^(?:а\s+)?(?:в|во|из|от|до)\s+', text)
            or not context.get('city') and not re.search(r'\b(?:где|как|почему|кто|что|можно)\b', text)):
        return None
    nearest = bool(NEAREST.search(text) or not day and re.search(r'\bкогда\b|во сколько', text))
    direction = 'arrival' if re.search(r'прилет|прибыт', text) else 'departure' if re.search(r'вылет', text) else None
    # The timetable is anchored at Pulkovo; remove only this explicit endpoint.
    route = re.sub(r'\b(?:из|в|до)\s+(?:санкт[ -]петербург[ае]?|пулково|спб)\b', ' ', rest)
    if day or invalid:
        route = re.sub(r'\bна\b(?![ -]+дону\b)', ' ', route)
    if invalid:
        route = re.sub(r'\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?|\b(?:или|и)\b', ' ', route)
    value = ' '.join(NOISE.sub(' ', route).split()).strip(' ,.!?;:')
    prep = re.match(r'^(из|от|в|во|до)\s+', value)
    if prep:
        route_direction = 'arrival' if prep[1] in ('из', 'от') else 'departure'
        value = value[prep.end():].strip(' ,.!?;:')
        if direction and direction != route_direction:
            return dict(mode='city', city=value, error='Табло показывает время в Пулково. Уточните: вылет из Пулково в этот город или прилет из него в Пулково?')
        direction = route_direction
    if value and re.search(r'\b(?:из|в|до|или|и)\b', value):
        return dict(mode='city', error='Укажите один город: вылет из Пулково в этот город или прилет из него в Пулково.')
    if value and (len(value) > 80 or not re.fullmatch(r'[а-яa-z ()-]+', value)):
        return None
    if not value and not context and not nearest and not direction:
        return None
    query = dict(context) if context else {'mode': 'city'}
    if value:
        query['city'] = value
    if direction:
        query['direction'] = direction
    if nearest:
        query['nearest'] = True
    if day:
        query['date'] = day
    if invalid:
        query.pop('date', None)
        query['error'] = 'Укажите одну корректную дату или напишите «сегодня», «завтра», «послезавтра».'
    return query
