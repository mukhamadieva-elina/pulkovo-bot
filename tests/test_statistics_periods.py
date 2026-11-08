import csv
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from pulkovo.config import Config
from pulkovo.db import Database
from pulkovo.statistics import MOSCOW, automation_report, statistics_period
from web_client import authenticated_client


def at(value):
    return datetime.fromisoformat(value).replace(tzinfo=MOSCOW).timestamp()


NOW = at('2026-11-08T12:30:00')


class StatisticsPeriodTests(unittest.TestCase):
    def test_calendar_and_rolling_presets_use_moscow(self):
        expected = {
            'today': ('2026-11-08T00:00:00', '2026-11-08T12:30:00'),
            'yesterday': ('2026-11-07T00:00:00', '2026-11-08T00:00:00'),
            '24h': ('2026-11-07T12:30:00', '2026-11-08T12:30:00'),
            '7d': ('2026-11-02T00:00:00', '2026-11-08T12:30:00'),
            '30d': ('2026-10-10T00:00:00', '2026-11-08T12:30:00'),
        }
        for key,(start,end) in expected.items():
            with self.subTest(period=key):
                period=statistics_period({'period':key},NOW)
                self.assertEqual((period['since'],period['until']),(at(start),at(end)))
        start=statistics_period({},NOW)['since']
        self.assertEqual(datetime.fromtimestamp(start,timezone.utc).isoformat(),'2026-11-07T21:00:00+00:00')
        self.assertEqual(statistics_period({'period':'yesterday'},NOW)['to_date'],'2026-11-07')

    def test_custom_range_includes_last_calendar_day(self):
        period=statistics_period({'period':'custom','from':'2026-11-05','to':'2026-11-07'},NOW)
        self.assertEqual((period['since'],period['until']),(at('2026-11-05T00:00:00'),at('2026-11-08T00:00:00')))
        today=statistics_period({'period':'custom','from':'2026-11-08','to':'2026-11-08'},NOW)
        self.assertEqual(today['until'],NOW)
        self.assertTrue(today['current_day'])
        self.assertFalse(period['current_day'])

    def test_custom_range_validation(self):
        for params in (
            {'period':'wrong'},
            {'period':'custom'},
            {'from':'bad','to':'2026-11-07'},
            {'from':'2026-11-08','to':'2026-11-07'},
            {'from':'2026-11-08','to':'2026-11-09'},
            {'from':'2025-11-08','to':'2026-11-08'},
        ):
            with self.subTest(params=params),self.assertRaises(ValueError):
                statistics_period(params,NOW)
        self.assertEqual(statistics_period({'from':'2025-11-09','to':'2026-11-08'},NOW)['selected'],'custom')

    def test_legacy_days_links_keep_rolling_semantics(self):
        for value,days in [('1',1),('7',7),('bad',30),('-1',1),('999999',365)]:
            period=statistics_period({'days':value},NOW)
            self.assertEqual(period['until']-period['since'],days*86400)
        self.assertEqual(statistics_period({},NOW)['selected'],'today')


class DailyStatisticsTests(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        self.db=Database(Path(folder.name)/'test.sqlite3')
        self.cfg=Config(self.db.path,admin_password='test',secret_key='test')
        with patch('pulkovo.web.time.time',return_value=NOW):
            self.client=authenticated_client(self.cfg,self.db)
        self.addCleanup(self.client.close)
        self.serial=0

    def add(self,stamp,*,human=False,outcome=None):
        self.serial+=1
        ident=self.db.enqueue(self.serial,self.serial,f'event-{self.serial}',payload=json.dumps({}))
        self.db.execute("UPDATE requests SET created_at=?,updated_at=?,outcome=?,status='sent' WHERE id=?",(stamp,stamp,outcome or ('handoff' if human else 'answer'),ident))
        if human:
            self.db.execute("INSERT INTO tickets(peer_id,request_id,reason,status,created_at,closed_at,closure_reason) VALUES (?,?,'no_matching_answer','closed',?,?,'manual')",(self.serial,ident,stamp,stamp+1))
        return ident

    def test_exact_midnight_is_counted_once_on_the_correct_moscow_day(self):
        self.add(at('2026-11-06T23:59:59'))
        self.add(at('2026-11-07T00:00:00'),human=True)
        self.add(at('2026-11-08T00:00:00'))
        report=automation_report(self.db,at('2026-11-06T00:00:00'),at('2026-11-08T00:00:00'))
        self.assertEqual([(r['day'],r['automated'],r['handed_off']) for r in report['days']],[('2026-11-07',0,1),('2026-11-06',1,0)])
        self.assertEqual(report['summary']['total'],2)
        yesterday=automation_report(self.db,at('2026-11-07T00:00:00'),at('2026-11-08T00:00:00'))
        today=automation_report(self.db,at('2026-11-08T00:00:00'),NOW)
        self.assertEqual(yesterday['summary']['percent'],0)
        self.assertEqual(today['summary']['percent'],100)

    def test_overall_percentage_is_weighted_and_empty_days_are_not_zero(self):
        self.add(at('2026-11-06T09:00:00'))
        self.add(at('2026-11-06T10:00:00'),human=True)
        for _ in range(9):self.add(at('2026-11-08T09:00:00'))
        self.add(at('2026-11-08T10:00:00'),human=True)
        self.add(at('2026-11-07T10:00:00'),outcome='thanks')
        report=automation_report(self.db,at('2026-11-06T00:00:00'),NOW)
        self.assertEqual([r['percent'] for r in report['days']],[90,None,50])
        self.assertEqual(report['summary']['total'],12)
        self.assertAlmostEqual(report['summary']['percent'],100*10/12)
        self.assertEqual(report['days'][1]['total'],0)

    def test_empty_midnight_today_is_still_visible(self):
        midnight=at('2026-11-08T00:00:00')
        report=automation_report(self.db,midnight,midnight)
        self.assertEqual([r['day'] for r in report['days']],['2026-11-08'])
        self.assertIsNone(report['days'][0]['percent'])

    def test_presets_change_html_and_bound_other_statistics(self):
        self.add(at('2026-11-07T09:00:00'),human=True)
        self.add(at('2026-11-08T09:00:00'))
        self.add(at('2026-11-09T09:00:00'),human=True)
        with patch('pulkovo.web.time.time',return_value=NOW):
            today=self.client.get('/statistics').text
            yesterday=self.client.get('/statistics?period=yesterday').text
            week=self.client.get('/statistics?period=7d').text
        self.assertIn('100,0%',today)
        self.assertIn('Сегодняшний день еще не завершен',today)
        self.assertIn('Закрыто по неактивности</span><strong>0</strong>',today)
        self.assertIn('Завершено сотрудником</span><strong>0</strong>',today)
        self.assertIn('0,0%',yesterday)
        self.assertNotIn('Сегодняшний день еще не завершен',yesterday)
        self.assertIn('Завершено сотрудником</span><strong>1</strong>',yesterday)
        self.assertIn('50,0%',week)
        self.assertIn('Автоматизация по дням',week)
        self.assertIn('/automation.csv?period=7d',week)
        self.assertIn('/tickets.csv?period=7d',week)

    def test_custom_dates_are_preserved_in_exports_and_bad_range_shows_form_error(self):
        with patch('pulkovo.web.time.time',return_value=NOW):
            response=self.client.get('/statistics',params={'period':'custom','from':'2026-11-05','to':'2026-11-07'})
            self.assertEqual(response.status_code,200)
            self.assertIn('/automation.csv?period=custom&amp;from=2026-11-05&amp;to=2026-11-07',response.text)
            error=self.client.get('/statistics',params={'period':'custom','from':'2026-11-07','to':'2026-11-05'})
            self.assertEqual(error.status_code,400)
            self.assertIn('Начальная дата должна быть не позже конечной',error.text)
            self.assertIn('Показать период',error.text)
            self.assertNotIn('Автоматизация ответов</span>',error.text)

    def test_daily_csv_and_other_exports_use_the_selected_interval(self):
        for stamp,human in [('2026-11-06T23:59:59',False),('2026-11-07T00:00:00',True),('2026-11-07T12:00:00',False),('2026-11-08T00:00:00',True)]:
            self.add(at(stamp),human=human)
        with patch('pulkovo.web.time.time',return_value=NOW):
            response=self.client.get('/automation.csv?period=yesterday')
            self.assertEqual(response.status_code,200)
            rows=list(csv.reader(io.StringIO(response.text.lstrip('\ufeff'))))
            self.assertEqual(rows[1:], [['2026-11-07','1','1','2','50.0']])
            events=self.client.get('/statistics.csv?period=yesterday')
            self.assertEqual(len(list(csv.reader(io.StringIO(events.text)))),3)
            closed=self.client.get('/tickets.csv?period=yesterday')
            self.assertEqual(len(list(csv.reader(io.StringIO(closed.text)))),2)
            all_events=self.client.get('/statistics.csv')
            self.assertEqual(len(list(csv.reader(io.StringIO(all_events.text)))),5)
            empty=self.client.get('/automation.csv',params={'period':'custom','from':'2026-11-05','to':'2026-11-05'})
            self.assertEqual(list(csv.reader(io.StringIO(empty.text)))[1],['2026-11-05','0','0','0',''])
            self.assertEqual(self.client.get('/automation.csv?period=wrong').status_code,400)
