import copy
import json
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

from pulkovo.flight_cities import city_matches
from pulkovo.flights import Flight, MOSCOW, BoardUnavailable, answer_query, parse_query
from pulkovo.operator import OperatorInbox
from pulkovo.statistics import automation_summary
from test_feedback import FeedbackFixture
from web_client import authenticated_client

NOW = datetime(2026, 11, 26, 12, tzinfo=MOSCOW).timestamp()
TODAY = datetime.fromtimestamp(NOW, MOSCOW).date()


def flight(number='SU 1', *, offset=0, hour=13, city='Красноярск', direction='departure', **values):
    scheduled = datetime(2026, 11, 26, hour, tzinfo=MOSCOW)+timedelta(days=offset)
    row = Flight(direction, number, [number], city, 'Авиакомпания', scheduled,
                 datetime.fromtimestamp(NOW, MOSCOW), 'Регистрация' if direction=='departure' else 'Ожидается')
    for name, value in values.items():
        setattr(row, name, value)
    return row


def board_with(*flights, missing=()):
    def get(direction, day):
        if day in missing:
            raise BoardUnavailable('test_unavailable')
        return [f for f in flights if f.direction==direction and f.scheduled.date()==day], NOW+60
    board = Mock(); board.get.side_effect = get
    return board


class CityQueryTests(unittest.TestCase):
    def test_original_question_and_explicit_city_routes(self):
        q = parse_query('Какой ближайший рейс в Красноярск', now=NOW)
        self.assertEqual(q, dict(mode='city', city='красноярск', nearest=True, direction='departure'))
        for text in ('Рейсы из Москвы завтра', 'Прилет из Москвы завтра', 'Рейсы из Москвы в Пулково завтра'):
            with self.subTest(text=text):
                q = parse_query(text, now=NOW)
                self.assertEqual((q['city'], q['direction'], q['date']), ('москвы', 'arrival', '2026-11-27'))
        self.assertEqual(parse_query('Рейс из Пулково в Красноярск завтра', now=NOW)['direction'], 'departure')
        for text in ('Рейсы из Ростова-на-Дону на завтра', 'Рейсы на завтра из Ростова-на-Дону'):
            q = parse_query(text, now=NOW)
            self.assertTrue(city_matches(q['city'], 'Ростов-на-Дону'))

    def test_date_direction_and_city_followups(self):
        q = parse_query('Рейсы в Москву', now=NOW)
        self.assertIn('На какую дату', answer_query(q, now=NOW).text)
        q = parse_query('а завтра?', q, now=NOW)
        self.assertEqual((q['city'], q['date']), ('москву', '2026-11-27'))
        q = parse_query('а в Красноярск?', q, now=NOW)
        self.assertEqual((q['city'], q['date']), ('красноярск', '2026-11-27'))
        q = parse_query('Ближайший рейс Москва', now=NOW)
        self.assertNotIn('direction', q)
        self.assertEqual(parse_query('прилет', q, now=NOW)['direction'], 'arrival')
        q = parse_query('ближайший рейс', now=NOW)
        self.assertIn('В какой город', answer_query(q, now=NOW).text)
        self.assertEqual(parse_query('в Казань', q, now=NOW)['city'], 'казань')

    def test_invalid_dates_and_multiple_cities_require_clarification(self):
        for text in ('рейсы в Москву 31.11', 'рейсы в Москву сегодня или завтра', 'рейсы в Москву или Казань завтра'):
            with self.subTest(text=text):
                q = parse_query(text, now=NOW)
                self.assertIn('error', q)
                board = Mock()
                self.assertEqual(answer_query(q, board, now=NOW).outcome, 'clarify')
                board.get.assert_not_called()
        board = Mock()
        self.assertEqual(answer_query(parse_query('рейсы в Москву 30.11', now=NOW), board, now=NOW).reason, 'flight_outside_board')
        board.get.assert_not_called()

    def test_unrelated_questions_do_not_become_city_names(self):
        previous = parse_query('рейсы в Москву завтра', now=NOW)
        for text in ('Где парковка?', 'Как купить билет в Красноярск?', 'Можно ли лететь с собакой в Москву?',
                     'Где посмотреть рейс?', 'Где посмотреть расписание рейсов?', 'Как пройти регистрацию на рейс в Москву?',
                     'Не хочу другой рейс', 'Нормы багажа на рейс в Москву', 'Сколько лететь рейсом в Красноярск?',
                     'Поменять рейс в Москву', 'Хочу поговорить с оператором'):
            with self.subTest(text=text):
                self.assertIsNone(parse_query(text, previous, now=NOW))

    def test_number_lookup_does_not_keep_city_filter(self):
        previous = parse_query('рейсы в Москву завтра', now=NOW)
        self.assertEqual(parse_query('SU25', previous, now=NOW), dict(number='SU25', date='2026-11-27', direction='departure'))
        old = dict(number='SU25', date='2026-11-26', direction='arrival')
        q = parse_query('ближайший рейс в Красноярск', old, now=NOW)
        self.assertNotIn('number', q)
        self.assertNotIn('date', q)
        self.assertEqual(q['direction'], 'departure')

    def test_city_inflections_airports_and_exact_matching(self):
        for source, requested in [('Красноярск','Красноярска'), ('Москва (SVO)','Москву'),
                                  ('Москва (DME)','Москвы'), ('Москва (SVO)','Москва Шереметьево'),
                                  ('Москва (VKO)','Внуково'), ('Казань','Казани'), ('Пермь','Перми'),
                                  ('Нижний Новгород','Нижнего Новгорода'), ('Минеральные Воды','Минеральных Вод')]:
            with self.subTest(source=source, requested=requested):
                self.assertTrue(city_matches(requested, source))
        for requested, source in [('Орск','Красноярск'), ('Краснодар','Красноярск'), ('Москва SVO','Москва (DME)')]:
            self.assertFalse(city_matches(requested, source))


class CityAnswerTests(unittest.TestCase):
    def query(self, text='Какой ближайший рейс в Красноярск'):
        return parse_query(text, now=NOW)

    def test_nearest_uses_expected_time_and_excludes_completed_cancelled_and_offblock(self):
        tomorrow = flight('SU 2', offset=1, hour=1)
        delayed = flight('SU 3', offset=-1, hour=23, status='Задержан', estimated=datetime(2026,11,26,14,tzinfo=MOSCOW))
        first = flight('SU 1')
        board = board_with(tomorrow, delayed, flight('SU 4', status='Отмена'),
                           flight('SU 5', actual=first.scheduled), flight('SU 6', offblock=first.scheduled), first, copy.deepcopy(first))
        result = answer_query(self.query(), board, now=NOW)
        self.assertEqual(result.outcome, 'answer')
        self.assertLess(result.text.index('SU 1'), result.text.index('SU 3'))
        self.assertLess(result.text.index('SU 3'), result.text.index('SU 2'))
        self.assertEqual(result.text.count('SU 1'), 1)
        self.assertIn('26.11 14:00 ожидается · задержан', result.text)
        for number in ('SU 4', 'SU 5', 'SU 6'):
            self.assertNotIn(number, result.text)
        self.assertEqual({c.args[1] for c in board.get.call_args_list}, {TODAY+timedelta(days=n) for n in (-1,0,1,2)})
        self.assertEqual(result.valid_until, NOW+60)

    def test_unknown_delay_or_past_time_prevents_claiming_definitive_nearest(self):
        for status in ('Задержан', 'Регистрация'):
            result = answer_query(self.query(), board_with(flight(), flight('SU 2', hour=10, status=status)), now=NOW)
            self.assertEqual((result.outcome, result.reason), ('flight_info', 'flight_partial'))
            self.assertIn('Однозначно определить ближайший рейс нельзя', result.text)
            self.assertIn('SU 2', result.text)
            self.assertNotIn('Ближайшие рейсы по табло', result.text)

    def test_explicit_day_lists_statuses_and_limits_output(self):
        rows = [flight(f'SU {n}', offset=1, hour=n+5, city='Москва (SVO)') for n in range(1,6)]
        rows[0].status='Отмена'; rows[1].city='Москва (DME)'
        result = answer_query(self.query('рейсы в Москву завтра'), board_with(*reversed(rows)), now=NOW)
        self.assertEqual(result.outcome, 'answer')
        self.assertIn('Пулково → Москва', result.text)
        self.assertIn('Москва (DME)', result.text)
        self.assertIn('отменен', result.text)
        self.assertIn('Показаны 3 из 5', result.text)
        self.assertNotIn('SU 4', result.text)

    def test_arrivals_and_no_match_are_not_confused_with_departures(self):
        row = flight(city='Москва (VKO)', direction='arrival')
        result = answer_query(self.query('ближайший рейс из Москвы'), board_with(row), now=NOW)
        self.assertEqual(result.outcome, 'answer')
        self.assertIn('Москва → Пулково', result.text)
        result = answer_query(self.query('ближайший рейс в Москву'), board_with(row), now=NOW)
        self.assertEqual(result.reason, 'flight_not_found')
        self.assertIn('не означает, что такого маршрута нет', result.text)

    def test_partial_or_total_outage_does_not_confirm_nearest_or_no_route(self):
        result = answer_query(self.query(), board_with(flight(), missing=(TODAY+timedelta(days=1),)), now=NOW)
        self.assertEqual(result.reason, 'flight_partial')
        self.assertIn('Список может быть неполным', result.text)
        board = Mock(); board.get.side_effect = BoardUnavailable()
        result = answer_query(self.query(), board, now=NOW)
        self.assertEqual(result.reason, 'flight_unavailable')
        self.assertNotIn('не нашел', result.text)

    def test_arrival_does_not_repeat_expected_status(self):
        row = flight(city='Москва (SVO)', direction='arrival', estimated=datetime(2026,11,26,14,tzinfo=MOSCOW))
        result = answer_query(self.query('ближайший рейс из Москвы'), board_with(row), now=NOW)
        self.assertIn('14:00 ожидается', result.text)
        self.assertNotIn('ожидается · ожидается', result.text)


class CityEngineTests(FeedbackFixture):
    def setUp(self):
        super().setUp()
        self.now = NOW
        self.engine.flight_board = board_with(flight(), flight(offset=1))

    def test_original_case_answers_without_ai_handoff_and_survives_restart(self):
        row = self.message('Какой ближайший рейс в Красноярск')
        self.assertEqual((row['outcome'], row['reason']), ('answer', 'flight_board'))
        self.assertEqual(row['prompt_tokens']+row['completion_tokens'], 0)
        self.assertFalse(self.db.conversation(11)['human_mode'])
        self.assertEqual(self.survey()['status'], 'pending')
        self.assertEqual(automation_summary(self.db, NOW-1, NOW+1)['automated'], 1)
        from pulkovo.engine import Engine
        self.engine = Engine(self.db, self.ai, self.cfg)
        self.engine.flight_board = board_with(flight(), flight(offset=1))
        self.now += 1
        row = self.message('а завтра?')
        self.assertEqual(json.loads(row['flight_query'])['date'], '2026-11-27')
        self.assertEqual(row['outcome'], 'answer')
        self.ai.route.assert_not_called()
        self.assertEqual(self.db.history(11), [])

    def test_partial_result_does_not_count_as_automated_solution(self):
        self.engine.flight_board = board_with(flight(), flight('SU 2', hour=10, status='Задержан'))
        row = self.message('ближайший рейс в Красноярск')
        self.assertEqual(row['outcome'], 'flight_info')
        self.assertIsNone(self.survey())
        self.assertEqual(automation_summary(self.db, NOW-1, NOW+1)['total'], 0)
        self.assertEqual(self.db.all('SELECT * FROM tickets'), [])
        self.ai.route.assert_not_called()

    def test_city_lookup_respects_consent_and_staff(self):
        board = Mock(); self.engine.flight_board = board
        self.db.record_consent(11, 11, 'revoked', 'revoke')
        self.message('ближайший рейс в Красноярск')
        self.db.take_dialogue(11, OperatorInbox(self.db).revision(11))
        self.message('ближайший рейс в Красноярск')
        board.get.assert_not_called()

    def test_expired_city_answer_refreshes_before_delivery(self):
        self.message('ближайший рейс в Красноярск', deliver=False)
        self.now += 61
        self.engine.deliver(self.transport)
        self.transport.send.assert_not_called()
        board = Mock(); board.get.side_effect=BoardUnavailable(); self.engine.flight_board=board
        self.engine.process(self.engine.claim())
        self.engine.deliver(self.transport)
        self.assertIn('не удалось', self.transport.send.call_args.args[1])
        self.assertNotIn('SU 1', self.transport.send.call_args.args[1])

    def test_city_preview_uses_same_source_without_ai(self):
        client = authenticated_client(self.cfg, self.db); self.addCleanup(client.close)
        with patch('pulkovo.web.answer_query', side_effect=lambda q: answer_query(q, self.engine.flight_board, now=self.now)), patch('pulkovo.web.GigaChat') as ai:
            page = client.post('/preview', data={'csrf':'test', 'action':'ai', 'synthetic':'on', 'text':'ближайший рейс в Красноярск'})
        self.assertEqual(page.status_code, 200)
        self.assertIn('SU 1', page.text)
        ai.assert_not_called()
