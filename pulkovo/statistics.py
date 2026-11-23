"""Operational metrics based on delivered answers and actual staff handoffs."""

from datetime import date, datetime, time, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

MOSCOW = ZoneInfo('Europe/Moscow')
PERIODS = [('today', 'Сегодня'), ('yesterday', 'Вчера'), ('24h', '24 часа'), ('7d', '7 дней'), ('30d', '30 дней')]


def statistics_period(params, now):
    """Resolve a half-open time interval; calendar dates always use Moscow time."""
    current = datetime.fromtimestamp(now, MOSCOW)
    today = current.date()
    midnight = datetime.combine(today, time.min, MOSCOW)
    selected = params.get('period') or ('custom' if params.get('from') or params.get('to') else 'today')
    if 'days' in params and 'period' not in params and selected != 'custom':
        # Keep existing bookmarks and CSV URLs using rolling days working.
        try:
            days = min(365, max(1, int(params['days'])))
        except ValueError:
            days = 30
        start, end = current-timedelta(days=days), current
        selected, title, query = 'legacy', f'Последние {days} дн.', {'days': days}
    else:
        end = current
        query = {'period': selected}
        if selected == 'today':
            start, title = midnight, 'Сегодня'
        elif selected == 'yesterday':
            start, end, title = midnight-timedelta(days=1), midnight, 'Вчера'
        elif selected == '24h':
            start, title = current-timedelta(hours=24), 'Последние 24 часа'
        elif selected in ('7d', '30d'):
            days = int(selected[:-1])
            start, title = midnight-timedelta(days=days-1), f'Последние {days} дней'
        elif selected == 'custom':
            try:
                first, last = date.fromisoformat(params.get('from', '')), date.fromisoformat(params.get('to', ''))
            except (ValueError, TypeError):
                raise ValueError('Укажите начальную и конечную дату.') from None
            if first > last:
                raise ValueError('Начальная дата должна быть не позже конечной.')
            if last > today:
                raise ValueError('Конечная дата не может быть позже сегодняшней.')
            if (last-first).days >= 365:
                raise ValueError('Выберите период не длиннее 365 дней.')
            start = datetime.combine(first, time.min, MOSCOW)
            end = min(datetime.combine(last+timedelta(days=1), time.min, MOSCOW), current)
            title = f'{first:%d.%m.%Y} — {last:%d.%m.%Y}'
            query.update({'from': first.isoformat(), 'to': last.isoformat()})
        else:
            raise ValueError('Выберите период из списка или задайте даты.')
    last_date = end.date()-timedelta(days=1) if end.time() == time.min and end > start else end.date()
    return dict(selected=selected, title=title, since=start.timestamp(), until=end.timestamp(),
                from_date=start.date().isoformat(), to_date=last_date.isoformat(),
                range_label=f'С {start:%d.%m.%Y %H:%M} до {end:%d.%m.%Y %H:%M}, московское время',
                query=urlencode(query), current_day=last_date == today)


_RESULTS = """
    WITH handed AS (SELECT DISTINCT request_id FROM tickets),
    results AS (
        SELECT r.created_at, CASE
            WHEN h.request_id IS NOT NULL THEN 'human'
            WHEN r.outcome='answer' AND r.status='sent' THEN 'bot'
        END result
        FROM requests r LEFT JOIN handed h ON h.request_id=r.id
        WHERE r.created_at>=? AND r.created_at<?
          AND coalesce(r.outcome,'') NOT IN
              ('thanks','greeting','consent_required','consent_accepted','consent_revoked')
          AND coalesce(r.reason,'')!='consent_declined'
          AND coalesce(r.outcome,'') NOT LIKE 'feedback%'
          AND coalesce(json_extract(
              CASE WHEN json_valid(r.payload) THEN r.payload ELSE '{}' END,
              '$.action'),'') NOT IN ('agree','decline','revoke','feedback')
    )
"""
_COUNTS = """coalesce(sum(result='bot'),0) automated,
               coalesce(sum(result='human'),0) handed_off,
               count(result) total"""


def with_percent(row):
    row['percent'] = 100 * row['automated'] / row['total'] if row['total'] else None
    return row


def automation_summary(db, since, until):
    # A handoff overrides an answer to the same incoming message.
    return with_percent(db.one(_RESULTS + 'SELECT ' + _COUNTS + ' FROM results', (since, until)))


def automation_report(db, since, until):
    # Group in SQLite, returning at most one row per day. Use ZoneInfo rather
    # than the database host's timezone, including for historical date ranges.
    with db.connect() as con:
        con.create_function('moscow_day', 1, lambda value: datetime.fromtimestamp(value, MOSCOW).date().isoformat(), deterministic=True)
        found = con.execute(_RESULTS + 'SELECT moscow_day(created_at) day, ' + _COUNTS + ' FROM results WHERE result IS NOT NULL GROUP BY day', (since, until)).fetchall()
    by_day = {row['day']: dict(row) for row in found}
    totals = dict(automated=0, handed_off=0, total=0)
    for row in by_day.values():
        for key in totals:
            totals[key] += row[key]
    rows = []
    first = datetime.fromtimestamp(since, MOSCOW)
    end = datetime.fromtimestamp(until, MOSCOW)
    last = end.date()-timedelta(days=1) if end.time() == time.min and end > first else end.date()
    day = first.date()
    while day <= last:
        row = by_day.get(day.isoformat(), dict(day=day.isoformat(), automated=0, handed_off=0, total=0))
        row.update(label=day.strftime('%d.%m.%Y'))
        rows.append(with_percent(row))
        day += timedelta(days=1)
    return dict(summary=with_percent(totals), days=list(reversed(rows)))


def feedback_percent(row):
    row['rated'] = row['good'] + row['bad']
    row['percent'] = 100 * row['good'] / row['rated'] if row['rated'] else None
    row['response_percent'] = 100 * row['rated'] / row['sent'] if row['sent'] else None
    return row


def feedback_report(db, since, until):
    # Cohort by invitation date. Later votes update that invitation's day.
    with db.connect() as con:
        con.create_function('moscow_day', 1, lambda value: datetime.fromtimestamp(value, MOSCOW).date().isoformat(), deterministic=True)
        found = con.execute("""SELECT moscow_day(sent_at) day, count(*) sent,
            coalesce(sum(rating='good'),0) good, coalesce(sum(rating='bad'),0) bad
            FROM feedback_surveys WHERE sent_at>=? AND sent_at<? GROUP BY day""", (since, until)).fetchall()
    by_day = {row['day']: dict(row) for row in found}
    totals = dict(sent=0, good=0, bad=0)
    for row in by_day.values():
        for key in totals:
            totals[key] += row[key]
    first = datetime.fromtimestamp(since, MOSCOW)
    end = datetime.fromtimestamp(until, MOSCOW)
    last = end.date()-timedelta(days=1) if end.time() == time.min and end > first else end.date()
    day, rows = first.date(), []
    while day <= last:
        row = by_day.get(day.isoformat(), dict(day=day.isoformat(), sent=0, good=0, bad=0))
        row['label'] = day.strftime('%d.%m.%Y')
        rows.append(feedback_percent(row))
        day += timedelta(days=1)
    negative = db.all("""SELECT s.peer_id,s.sent_at,s.rated_at,r.text,r.reply,r.topic,
        trim(coalesce(p.first_name,'')||' '||coalesce(p.last_name,'')) user_name
        FROM feedback_surveys s JOIN requests r ON r.id=s.request_id
        JOIN conversations c ON c.peer_id=s.peer_id
        LEFT JOIN vk_profiles p ON p.user_id=c.user_id
        WHERE s.sent_at>=? AND s.sent_at<? AND s.rating='bad'
        ORDER BY s.rated_at DESC,s.id DESC LIMIT 10""", (since, until))
    return dict(summary=feedback_percent(totals), days=list(reversed(rows)), negative=negative)
