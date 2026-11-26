"""Public data used by Pulkovo's own board; no LLM or passenger data in HTTP requests."""

import json
import re
import ssl
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import truststore
import urllib3

from .flight_cities import city_matches, parse_city_query

MOSCOW = ZoneInfo('Europe/Moscow')
BASE = 'https://pulkovoairport.ru'
CACHE_SECONDS = 60
SOURCE_MAX_AGE = 600
CONTEXT_SECONDS = 1800
MAX_BYTES = 2_000_000
NUMBER = re.compile(r'(?<![\w])([A-Z]{2,3}|[A-Z][0-9]|[0-9][A-Z])[\s-]*(\d{1,4})(?![\w])', re.I)
LIVE_INTENT = re.compile(r'табло|статус|гейт|выход\w* на посадк|стойк\w* регистрац|багажн\w* лент|лент\w* багаж|(?:когда|во сколько|где).*регистрац.*(?:рейс|[a-z]\s*\d)|когда.*(?:рейс|прилет|вылет)|во сколько.*(?:рейс|прилет|вылет)|(?:рейс.*(?:задерж|отмен|прилет|вылет))', re.I)
OTHER_INTENT = re.compile(r'возврат|компенсац|вернуть|обмен|купить|стоимость|цен[аыу]|сколько стоит|норм\w* багаж|ручн\w* клад|перевоз|потерял|потерян|забыл|забыты|опоздал|жалоб|питани|гостиниц|документ|паспорт', re.I)
MONTHS = ['января','февраля','марта','апреля','мая','июня','июля','августа','сентября','октября','ноября','декабря']
HTTP = urllib3.PoolManager(ssl_context=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT))


class BoardUnavailable(Exception):
    pass


def scalar(value):
    # The source represents some missing XML values as {}, not null.
    return ' '.join(value.split())[:200] if isinstance(value, str) else ''


def source_time(value):
    value = scalar(value)
    if not value:
        return None
    try:
        result = datetime.fromisoformat(value)
    except ValueError:
        raise BoardUnavailable('invalid_time') from None
    # Verified against the site's MSK display: naive source values are already MSK.
    return result.replace(tzinfo=MOSCOW) if result.tzinfo is None else result.astimezone(MOSCOW)


def normalize_number(value):
    match = NUMBER.fullmatch(scalar(value))
    return f'{match[1].upper()}{int(match[2])}' if match else ''


@dataclass
class Flight:
    direction: str
    number: str
    aliases: list[str]
    city: str
    airline: str
    scheduled: datetime
    updated: datetime
    status: str
    estimated: datetime | None = None
    actual: datetime | None = None
    offblock: datetime | None = None
    gate: str = ''
    counters: str = ''
    belt: str = ''
    via: str = ''


def parse_board(data, direction):
    if direction not in ('arrival', 'departure') or not isinstance(data, list) or len(data) > 2000:
        raise BoardUnavailable('invalid_board')
    flights = []
    prefix = 'OA_' if direction == 'arrival' else 'OD_'
    arrival = direction == 'arrival'
    for row in data:
        if not isinstance(row, dict):
            raise BoardUnavailable('invalid_row')
        get = lambda name: scalar(row.get(prefix + name))
        dt = lambda name: source_time(row.get(prefix + name))
        number = get('FLIGHT_NUMBER')
        scheduled = dt('STA' if arrival else 'STD')
        updated = source_time(row.get('TimeStamp'))
        city = (get('RAP_PREVIOUS_NAME_RU') or get('RAP_ORIGIN_NAME_RU')) if arrival else get('RAP_DESTINATION_NAME_RU')
        if not normalize_number(number) or not scheduled or not updated or not city:
            # A partially broken feed must not look like a valid "flight not found".
            raise BoardUnavailable('missing_fields')
        actual = dt('ATA' if arrival else 'ATD')
        offblock = None if arrival else dt('OFFBLOCK')
        status = get('STATUS_RU')
        if get('STATUS_EN') == 'Canceled':
            status = 'Отмена'
        elif actual:
            status = 'Прибыл' if arrival else 'Вылетел'
        elif not arrival:
            # Same precedence as dep_get_view on the official website.
            if offblock:
                status = 'Отправлен со стоянки'
            elif dt('BOARDING_END_ACTUAL'):
                status = 'Посадка закончена'
            elif dt('BOARDING_BEGIN_ACTUAL'):
                status = 'Идет посадка'
            elif dt('COUNTER_END_ACTUAL'):
                status = 'Ожидайте посадки'
            elif not status and get('COUNTERS'):
                status = 'Регистрация'
        if not status:
            status = 'Ожидается' if arrival else 'Статус не указан на табло'
        flights.append(Flight(direction, number,
            [get('FLIGHT_NUMBER' + suffix) for suffix in ('', '_K1', '_K2', '_K3') if get('FLIGHT_NUMBER' + suffix)],
            city, get('RAL_NAME_RUS'), scheduled, updated, status,
            dt('ETA' if arrival else 'ETD'), actual, offblock,
            get('GATES') if not arrival else '', get('COUNTERS') if not arrival else '',
            get('BAGGAGEBELTS') if arrival else '', get('RAP_NEXT_NAME_RU')))
    return flights


def fetch_board(direction, day, now):
    offset = (day - datetime.fromtimestamp(now, MOSCOW).date()).days
    if direction not in ('arrival', 'departure') or offset not in (-1, 0, 1, 2):
        raise BoardUnavailable('outside_board')
    try:
        # Fixed host/path, no user text, VK IDs or credentials. Do not follow redirects.
        with HTTP.request('GET', BASE + '/api/', fields={'type': direction, 'when': offset},
                          headers={'User-Agent': 'PulkovoCommunityBot/1.0', 'Accept': 'application/json',
                                   'Referer': f'{BASE}/passengers/{direction}/'},
                          timeout=urllib3.Timeout(connect=4, read=8), retries=False,
                          preload_content=False, redirect=False) as response:
            if response.status != 200:
                raise BoardUnavailable(f'http_{response.status}')
            chunks, size, started = [], 0, time.monotonic()
            for chunk in response.stream(16384):
                size += len(chunk)
                if size > MAX_BYTES or time.monotonic() - started > 12:
                    raise BoardUnavailable('response_limit')
                chunks.append(chunk)
        # The official JSON endpoint sends Content-Type: text/html.
        data = json.loads(b''.join(chunks))
        flights = parse_board(data, direction)
        # Do not cache tomorrow's feed under today's date when midnight crosses the request.
        if datetime.fromtimestamp(time.time(), MOSCOW).date() != datetime.fromtimestamp(now, MOSCOW).date():
            raise BoardUnavailable('day_changed')
        return flights
    except (urllib3.exceptions.HTTPError, OSError, ValueError) as exc:
        raise BoardUnavailable('fetch_failed') from exc


class Board:
    """Shared, bounded in-process cache for all dialogue workers, with failure cooldown."""
    def __init__(self, fetch=fetch_board, clock=None):
        self.fetch, self.clock = fetch, clock or (lambda: time.time())
        self.lock = threading.Lock()
        self.cache = {}

    def get(self, direction, day):
        key = (direction, day)
        with self.lock:
            now = self.clock()
            self.cache = {k:v for k,v in self.cache.items() if now-v[0] < CACHE_SECONDS}
            if key not in self.cache:
                try:
                    flights = self.fetch(direction, day, now)
                    # Empty data cannot establish freshness, so offer a board link.
                    if not flights or any(not -120 <= now-f.updated.timestamp() <= SOURCE_MAX_AGE for f in flights):
                        raise BoardUnavailable('stale_or_empty')
                except BoardUnavailable:
                    self.cache[key] = (self.clock(), None)
                    raise
                self.cache[key] = (now, flights)
            fetched, flights = self.cache[key]
            if flights is None or any(self.clock()-f.updated.timestamp() > SOURCE_MAX_AGE for f in flights):
                raise BoardUnavailable('source_unavailable')
            return flights, min(fetched+CACHE_SECONDS, min(f.updated.timestamp()+SOURCE_MAX_AGE for f in flights))


SHARED_BOARD = Board()


def requested_date(text, today):
    relative = re.findall(r'\b(послезавтра|завтра|сегодня|вчера)\b', text)
    found = [today+timedelta(days={'вчера':-1,'сегодня':0,'завтра':1,'послезавтра':2}[v]) for v in relative]
    rest = re.sub(r'\b(послезавтра|завтра|сегодня|вчера)\b', ' ', text)
    pattern = r'(?<!\d)(\d{1,2})[./](\d{1,2})(?:[./](\d{4}|\d{2}))?(?!\d)'
    try:
        for match in re.finditer(pattern, text):
            year = int(match[3]) if match[3] else today.year
            if year < 100:
                year += 2000
            item = date(year, int(match[2]), int(match[1]))
            if not match[3] and today.month == 12 and item.month == 1:
                item = item.replace(year=today.year+1)
            elif not match[3] and today.month == 1 and item.month == 12:
                item = item.replace(year=today.year-1)
            found.append(item)
        rest = re.sub(pattern, ' ', rest)
        for match in re.finditer(r'\b(\d{1,2})\s+('+'|'.join(MONTHS)+r')(?:\s+(\d{4}))?\b', text):
            month = MONTHS.index(match[2])+1
            year = int(match[3]) if match[3] else today.year+(today.month==12 and month==1)-(today.month==1 and month==12)
            found.append(date(year, month, int(match[1])))
        rest = re.sub(r'\b\d{1,2}\s+(?:'+'|'.join(MONTHS)+r')(?:\s+\d{4})?\b', ' ', rest)
    except ValueError:
        return None, rest, True
    unique = set(found)
    return (next(iter(unique)).isoformat() if len(unique)==1 else None), rest, len(unique)>1


def parse_query(text, previous=None, *, now=None):
    text = text.lower().replace('ё','е').strip()
    now = time.time() if now is None else now
    day, rest, invalid = requested_date(text, datetime.fromtimestamp(now, MOSCOW).date())
    numbers = list(dict.fromkeys(normalize_number(m[0]) for m in NUMBER.finditer(text)))
    live = bool(LIVE_INTENT.search(text))
    if re.search(r'\bстатус\b', text) and not (numbers or previous or re.search(r'рейс|табло|гейт',text)):
        live = False
    if numbers and re.search(r'задерж|отмен|прилет|вылет|регистрац|информац|провер|что с|что по', text):
        live = True
    if invalid and (numbers or previous):
        live = True
    if previous and re.search(r'гейт|стойк|лент|статус|обнови', text):
        live = True
    if re.fullmatch(r'рейс\s+\d{1,4}', text):
        live = True
    if OTHER_INTENT.search(text):
        return None
    # A request to start another lookup must not inherit the old flight or date.
    # Match the whole phrase so rebooking/questions about another flight still use the KB.
    reset_text = re.sub(r'[\W_]+', ' ', NUMBER.sub(' ', rest)).strip()
    new_search = bool(re.fullmatch(
        r'(?:а\s+)?(?:(?:давай|давайте|хочу|можно)\s+)?'
        r'(?:(?:проверить|посмотреть|найти|проверь(?:те)?|покажи(?:те)?)\s+)?'
        r'(?:другой|новый|еще(?:\s+один)?)\s+рейс(?:\s+пожалуйста)?', reset_text))
    if new_search:
        live, previous = True, None
    if not numbers and not new_search:
        city_query = parse_city_query(text, rest, day, invalid, previous)
        if city_query is not None:
            return city_query
    bare = re.sub(r'[\W_]+', ' ', NUMBER.sub(' ', rest)).strip()
    bare = re.sub(r'\b(?:рейс|на|пожалуйста|подскажите|проверь|проверьте|а|за|дата|прилет|вылет)\b', '', bare).strip()
    continuation = previous and (live or not bare) and (day or invalid or numbers or re.search(r'прилет|вылет|гейт|выход|статус|стойк|лент|обнови', text))
    if not (live or numbers and not bare or continuation):
        return None
    query = {'new_search': True} if new_search else dict(previous) if continuation else {}
    if query.get('mode') == 'city':
        query = {key: query[key] for key in ('date', 'direction') if key in query}
    # A new flight starts with its own date; never inherit the old flight's date silently.
    if numbers and query.get('number') and query['number'] not in numbers:
        query = {}
    if len(numbers)>1:
        return dict(error='Укажите один рейс и дату, например: SU 25 сегодня.')
    if numbers:
        query['number'] = numbers[0]
        query.pop('new_search', None)
    if day:
        query['date'] = day
    if invalid:
        query.pop('date', None)
        query['error'] = 'Укажите одну корректную дату, в формате ДД.ММ.ГГГГ или напишите «завтра».'
    # Gate/counter requests are departures; arrival/departure verbs alone may refer to another airport.
    if re.search(r'гейт|выход\w* на посадк|стойк\w* регистрац', text) or previous and re.search(r'стойк',text):
        query['direction'] = 'departure'
    elif re.search(r'багажн\w* лент|лент\w* багаж', text) or previous and re.search(r'лент',text):
        query['direction'] = 'arrival'
    elif re.search(r'\b(?:прилет|прибытие)\b', text) and not re.search(r'\b(?:вылет|отправление)\b', text):
        query['direction'] = 'arrival'
    elif re.search(r'\b(?:вылет|отправление)\b', text) and not re.search(r'\b(?:прилет|прибытие)\b', text):
        query['direction'] = 'departure'
    return query


@dataclass
class FlightReply:
    query: dict
    text: str
    outcome: str = 'clarify'
    reason: str = 'flight_details_needed'
    valid_until: float | None = None


def board_links():
    return f'Вылет: {BASE}/passengers/departure/\nПрилет: {BASE}/passengers/arrival/'


def format_flight(flight, requested):
    def stamp(value):
        return value.strftime('%H:%M') if value.date()==flight.scheduled.date() else value.strftime('%d.%m %H:%M')
    arrival = flight.direction=='arrival'
    label = 'Прилет' if arrival else 'Вылет'
    expected_label = 'Ожидаемый прилет' if arrival else 'Ожидаемый вылет'
    route = f'{flight.city} → Санкт-Петербург' if flight.direction=='arrival' else f'Санкт-Петербург → {flight.city}'
    lines = [f'✈ {flight.number} · {flight.scheduled:%d.%m.%Y}', route, flight.airline]
    if normalize_number(flight.number) != normalize_number(requested):
        lines.append(f'Также обозначается {requested} (совместный рейс).')
    if flight.via and flight.via != flight.city and flight.direction=='departure':
        lines.append(f'Через: {flight.via}')
    lines.append('')
    if flight.status == 'Отмена':
        lines.append('Рейс отменен.')
    elif flight.actual:
        lines.append(f'{"Прибыл" if arrival else "Вылетел"} в {stamp(flight.actual)}')
    elif flight.status == 'Задержан':
        lines.append('Рейс задержан.')
        if flight.estimated and flight.estimated > flight.scheduled:
            lines.append(f'{expected_label}: {stamp(flight.estimated)}')
        else:
            lines.append(f'Новое время {"прилета" if arrival else "вылета"} пока не указано.')
        lines.append(f'По расписанию было: {stamp(flight.scheduled)}')
    else:
        lines.append(f'Статус: {flight.status}')
        if flight.estimated and flight.estimated != flight.scheduled:
            lines += [f'{expected_label}: {stamp(flight.estimated)}',
                      f'По расписанию: {stamp(flight.scheduled)}']
        else:
            lines.append(f'{label} по расписанию: {stamp(flight.scheduled)}')
    if flight.status != 'Отмена':
        if flight.offblock and not flight.actual:
            lines.append(f'Отправлен со стоянки: {stamp(flight.offblock)}')
        if flight.direction=='departure':
            lines += [f'Выход на посадку: {flight.gate or "пока не указан на табло"}',
                      f'Стойки регистрации: {flight.counters or "пока не указаны на табло"}']
        else:
            lines.append(f'Багажная лента: {flight.belt or "пока не указана на табло"}')
    lines += ['', 'Время московское. Данные могут измениться.', f'Табло: {BASE}/passengers/{flight.direction}/']
    return '\n'.join(lines)


def answer_query(query, board=SHARED_BOARD, *, now=None):
    now = time.time() if now is None else now
    query = dict(query)
    error = query.pop('error', '')
    if error:
        return FlightReply(query, error)
    if query.get('mode') == 'city':
        return answer_city_query(query, board, now=now)
    if not query.get('number'):
        if query.get('new_search'):
            if query.get('date'):
                day = date.fromisoformat(query['date'])
                return FlightReply(query, f'Напишите номер другого рейса с кодом авиакомпании на {day:%d.%m.%Y}. Например: SU 25.')
            return FlightReply(query, 'Напишите номер другого рейса и дату по расписанию в Пулково. Например: SU 25 сегодня.')
        return FlightReply(query, 'Напишите номер рейса с кодом авиакомпании и дату по расписанию в Пулково. Например: SU 25 сегодня.')
    if not query.get('date'):
        return FlightReply(query, f'На какую дату проверить рейс {query["number"]}? Укажите дату по расписанию в Пулково, например: сегодня, завтра или в формате ДД.ММ.ГГГГ.')
    day = date.fromisoformat(query['date'])
    if (day-datetime.fromtimestamp(now, MOSCOW).date()).days not in (-1,0,1,2):
        return FlightReply(query, 'В табло доступны вчера, сегодня, завтра и послезавтра. Для другой даты проверьте расписание у авиакомпании.\n\n'+board_links(), 'flight_info', 'flight_outside_board')
    directions = [query['direction']] if query.get('direction') else ['arrival','departure']
    try:
        matches, deadlines = [], []
        for direction in directions:
            flights, deadline = board.get(direction, day)
            deadlines.append(deadline)
            matches += [f for f in flights if f.scheduled.date()==day and query['number'] in {normalize_number(n) for n in f.aliases}]
    except BoardUnavailable:
        return FlightReply(query, 'Сейчас не удалось получить свежие данные табло. Проверьте рейс на сайте аэропорта или повторите запрос позже.\n\n'+board_links(), 'flight_info', 'flight_unavailable')
    if not matches:
        return FlightReply(query, f'Не нашел рейс {query["number"]} на {day:%d.%m.%Y} в доступных данных табло. Это не означает отмену рейса. Проверьте номер с кодом авиакомпании и дату по расписанию в Пулково.\n\n'+board_links(), 'flight_info', 'flight_not_found')
    if len(matches)>1:
        choices = '\n'.join(f'{"Прилет" if f.direction=="arrival" else "Вылет"}: {f.number}, {f.city}, {f.scheduled:%H:%M}' for f in matches[:5])
        return FlightReply(query, 'На эту дату есть несколько совпадений:\n'+choices+'\n\nУточните: прилет или вылет. Если совпадений несколько в одном направлении, проверьте нужный рейс на табло.\n'+board_links())
    query['direction'] = matches[0].direction
    return FlightReply(query, format_flight(matches[0], query['number']), 'answer', 'flight_board', min(deadlines))


def flight_time(flight):
    if flight.actual:
        return flight.actual
    if flight.estimated and (flight.status != 'Задержан' or flight.estimated > flight.scheduled):
        return flight.estimated
    return None if flight.status == 'Задержан' else flight.scheduled


def city_flight_line(flight):
    when = flight_time(flight)
    if flight.status == 'Отмена':
        detail = f'{flight.scheduled:%d.%m %H:%M} по расписанию · отменен'
    elif when is None:
        detail = f'задержан, новое время не указано · по расписанию {flight.scheduled:%d.%m %H:%M}'
    else:
        timing = 'фактически' if flight.actual else 'ожидается' if flight.estimated and flight.estimated != flight.scheduled else 'по расписанию'
        detail = f'{when:%d.%m %H:%M} {timing}'
        if flight.status.lower() != timing:
            detail += f' · {flight.status.lower()}'
        if flight.status == 'Задержан':
            detail += f' (по расписанию {flight.scheduled:%d.%m %H:%M})'
    return f'✈ {flight.number} · {flight.city}\n{detail}'


def answer_city_query(query, board, *, now):
    city = query.get('city')
    if not city:
        return FlightReply(query, 'В какой город ищете вылет или из какого города ожидаете прилет? Например: ближайший рейс в Красноярск.')
    direction = query.get('direction')
    if not direction:
        return FlightReply(query, f'Город: {city.capitalize()}. Вас интересует вылет из Пулково или прилет в Пулково? Напишите «вылет» или «прилет».')
    nearest = query.get('nearest', False)
    today = datetime.fromtimestamp(now, MOSCOW).date()
    if not query.get('date') and not nearest:
        return FlightReply(query, 'На какую дату посмотреть рейсы по этому направлению? Напишите «сегодня», «завтра» или дату по расписанию в Пулково.')
    days = [date.fromisoformat(query['date'])] if query.get('date') else [today+timedelta(days=n) for n in (-1,0,1,2)]
    if any((day-today).days not in (-1,0,1,2) for day in days):
        return FlightReply(query, 'В табло доступны вчера, сегодня, завтра и послезавтра. Для другой даты проверьте расписание у авиакомпании.\n\n'+board_links(), 'flight_info', 'flight_outside_board')
    flights, deadlines, missing = [], [], []
    for day in days:
        try:
            rows, deadline = board.get(direction, day)
        except BoardUnavailable:
            missing.append(day)
            continue
        deadlines.append(deadline)
        flights.extend(f for f in rows if f.direction==direction and f.scheduled.date()==day and city_matches(city, f.city))
    # Codeshares in one row and duplicate source rows must not inflate results.
    unique = {}
    for flight in flights:
        key = (normalize_number(flight.number), flight.scheduled, flight.city)
        if key not in unique or flight.updated > unique[key].updated:
            unique[key] = flight
    flights = list(unique.values())
    display_city = re.sub(r'\s*\([^)]*\)', '', flights[0].city) if flights else city.capitalize()
    uncertain = []
    if nearest:
        upcoming = []
        for flight in flights:
            if flight.actual or flight.offblock or flight.status in ('Отмена', 'Вылетел', 'Прибыл', 'Отправлен со стоянки'):
                continue
            when = flight_time(flight)
            if when is None or when.timestamp() < now:
                # The source has not confirmed departure/arrival, but its time
                # is already in the past. Do not silently call another flight nearest.
                uncertain.append(flight)
            else:
                upcoming.append(flight)
        flights = sorted(upcoming, key=lambda f: (flight_time(f), f.scheduled, f.number))
    else:
        flights.sort(key=lambda f: (f.scheduled, f.number))
    link = f'Табло: {BASE}/passengers/{direction}/'
    scope = f'на {days[0]:%d.%m.%Y}' if query.get('date') else f'сейчас и до {(today+timedelta(days=2)):%d.%m.%Y}'
    route = f'Пулково → {display_city}' if direction=='departure' else f'{display_city} → Пулково'
    deadline = min(deadlines) if deadlines else None
    if not flights and not uncertain:
        if missing:
            text = 'Сейчас не удалось получить все свежие данные табло. Нельзя подтвердить наличие рейсов по этому направлению. Попробуйте позже.\n\n'+link
            return FlightReply(query, text, 'flight_info', 'flight_unavailable', deadline)
        qualifier = 'предстоящих рейсов' if nearest else 'рейсов'
        return FlightReply(query, f'По направлению {route} не нашел {qualifier} {scope} в доступных данных табло. Это не означает, что такого маршрута нет.\n\n'+link, 'flight_info', 'flight_not_found', deadline)
    title = 'Ближайшие рейсы по табло' if nearest and not (missing or uncertain) else 'Рейсы по табло'
    lines = [f'{title}: {route}', scope.capitalize(), '']
    lines.extend(city_flight_line(f)+'\n' for f in flights[:3])
    if len(flights)>3:
        lines.append(f'Показаны 3 из {len(flights)} найденных рейсов. Остальные доступны на табло.')
    if uncertain:
        lines.append('Есть рейсы без подтвержденного будущего времени. Однозначно определить ближайший рейс нельзя:')
        for flight in sorted(uncertain, key=lambda f: f.scheduled)[:2]:
            lines.append(city_flight_line(flight))
        if len(uncertain)>2:
            lines.append(f'Еще рейсов с неуточненным временем: {len(uncertain)-2}. См. табло.')
    if missing:
        lines.append('Часть табло недоступна ('+', '.join(d.strftime('%d.%m') for d in missing)+'). Список может быть неполным; ближайший рейс не подтвержден.')
    lines += ['', 'Время московское. Данные могут измениться.', 'Для выхода, стоек или багажной ленты напишите номер рейса и дату.', link]
    partial = bool(missing or nearest and uncertain)
    return FlightReply(query, '\n'.join(lines), 'flight_info' if partial else 'answer', 'flight_partial' if partial else 'flight_board', deadline)


def previous_query(db, request, generation):
    row = db.one("""SELECT flight_query,ai_generation,updated_at FROM requests
        WHERE peer_id=? AND session_id=? AND id<? AND status='sent' AND outcome NOT LIKE 'feedback%'
        ORDER BY id DESC LIMIT 1""", (request['peer_id'], request['session_id'], request['id']))
    if row and row['flight_query'] and row['ai_generation']==generation and time.time()-row['updated_at']<CONTEXT_SECONDS:
        return json.loads(row['flight_query'])
    return None
