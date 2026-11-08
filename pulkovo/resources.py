"""Обычный текст в редакторе; общие ссылки и контакты связываются при сохранении."""

import hashlib
import re
from urllib.parse import urlsplit

TOKEN = re.compile(r'\{([a-z][a-z0-9_]*)\}')
PARTS = re.compile(r'(\{[a-z][a-z0-9_]*\}|https?://[^\s<>\)\"]+)')


LINK_LABELS = {
    ('max.ru', '/pulkovo_led'): 'MAX Пулково',
    ('t.me', '/pulkovo_led'): 'Telegram Пулково',
    ('t.me', '/pulkovo_led/5458'): 'Пассажирам при задержке рейса — Telegram',
    ('t.me', '/studentspulkovo'): 'Пулково студентам — Telegram',
    ('spb.hh.ru', '/employer/666661'): 'Вакансии Пулково на hh.ru',
    ('customs.gov.ru', ''): 'Федеральная таможенная служба',
    ('ps.fsb.ru', '/fps/contact/department.htm'): 'Контакты пограничной службы',
    ('pulkovoairport.ru', '/passengers/animals-plants'): 'Перевозка животных и растений',
    ('pulkovoairport.ru', '/passengers/services/business-lounge'): 'Бизнес-залы Пулково',
    ('pulkovoairport.ru', '/passengers/map'): 'Схема аэропорта',
    ('pulkovoairport.ru', '/map'): 'Схема аэропорта',
    ('pulkovoairport.ru', '/passengers/baggage/storage'): 'Камера хранения багажа',
    ('pulkovoairport.ru', '/transport'): 'Транспорт и парковки',
    ('pulkovoairport.ru', '/transport/kak-oplatit-parkovku'): 'Оплата парковки',
    ('pulkovoairport.ru', '/about/careers/vzletnaya-polosa'): 'Программа «Взлётная полоса»',
    ('pulkovoairport.ru', '/passengers/services/accommodation'): 'Отели и отдых в аэропорту',
    ('pulkovoairport.ru', '/about/press_center/photography'): 'Фото- и видеосъёмка в аэропорту',
    ('pulkovoairport.ru', '/passengers/services/financial_services/exchange'): 'Обмен валюты',
    ('pulkovoairport.ru', '/passengers/prm'): 'Помощь маломобильным пассажирам',
    ('pulkovoairport.ru', '/passengers/services/fastrack'): 'Услуга Fast Track',
    ('pulkovoairport.ru', '/passengers/security/customs'): 'Таможенный контроль',
    ('pulkovoairport.ru', '/passengers/contact-us'): 'Обратная связь с Пулково',
    ('pulkovoairport.ru', '/about/careers/jobs'): 'Вакансии Пулково',
    ('pulkovoairport.ru', '/passengers/security/screening'): 'Досмотр и правила провоза вещей',
}


def default_resource_label(value):
    try:
        url=urlsplit(value)
        return LINK_LABELS.get((url.hostname,url.path.rstrip('/')),value)
    except ValueError:
        return value


def resource_keys(text):
    return set(TOKEN.findall(text))


def display_answer(text, resources):
    return TOKEN.sub(lambda m: resources.get(m[1], m[0]), text)


def answer_parts(text, resources):
    """Части редактора: обычный текст и ссылки на записи справочника."""
    by_key={r['key']:r for r in resources}
    parts=[]
    start=0
    for match in TOKEN.finditer(text):
        if match[1] not in by_key:
            continue
        if match.start()>start:
            parts.append({'text':text[start:match.start()]})
        parts.append({'resource':by_key[match[1]]})
        start=match.end()
    if start<len(text):
        parts.append({'text':text[start:]})
    return parts


def bind_answer(con, text, snapshot):
    current = {r['key']:r['value'] for r in con.execute("SELECT key,value FROM resources WHERE visibility='public'")}
    # Снимок формы сохраняет связь даже при изменении контакта в соседней вкладке.
    known = {value:key for key,value in current.items() if value}
    known.update({value:key for key,value in (snapshot or {}).items() if key in current and value})
    plain = {value:key for value,key in known.items() if not value.startswith(('http://','https://'))}
    pattern = re.compile(r'(?<!\w)('+'|'.join(re.escape(v) for v in sorted(plain,key=len,reverse=True))+r')(?!\w)') if plain else None
    result = []
    for part in PARTS.split(text):
        if TOKEN.fullmatch(part):
            result.append(part)
        elif part.startswith(('http://','https://')):
            url = part.rstrip('.,;!')
            key = known.get(url)
            if not key:
                key = 'link_' + hashlib.sha256(url.encode()).hexdigest()[:12]
                existing = con.execute('SELECT visibility,value FROM resources WHERE key=?',(key,)).fetchone()
                if existing and (existing['visibility']!='public' or existing['value']!=url):
                    # Не переиспользуем скрытую или изменённую старую запись по её хешу.
                    key = 'link_' + hashlib.sha256(('public:'+url).encode()).hexdigest()
                con.execute("INSERT OR IGNORE INTO resources(key,value,visibility,label) VALUES (?,?,'public',?)",(key,url,default_resource_label(url)))
                known[url] = key
            result.append('{'+key+'}'+part[len(url):])
        else:
            result.append(pattern.sub(lambda m:'{'+plain[m[0]]+'}',part) if pattern else part)
    return ''.join(result)
