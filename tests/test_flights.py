import copy
import json
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path
from unittest.mock import Mock, patch

from pulkovo.flights import (Board, BoardUnavailable, MOSCOW, SOURCE_MAX_AGE,
    answer_query, fetch_board, format_flight, normalize_number, parse_board, parse_query)
from pulkovo.statistics import automation_summary
from pulkovo.sessions import close_session
from pulkovo.operator import OperatorInbox
from test_feedback import FeedbackFixture
from web_client import authenticated_client

FIXTURES = Path(__file__).parent/'fixtures'/'flights'
NOW = datetime(2026,11,22,20,33,tzinfo=MOSCOW).timestamp()


def records(direction):
    return json.loads((FIXTURES/(direction+'.json')).read_text())


def fake_board(clock=lambda: NOW):
    return Board(lambda direction, day, now: parse_board(records(direction),direction), clock)


class FlightSourceTests(unittest.TestCase):
    def test_real_source_fields_and_moscow_clock(self):
        flights = parse_board(records('departure'),'departure')
        shared = next(f for f in flights if 'SU 6401' in f.aliases)
        self.assertEqual(shared.gate,'18')
        self.assertEqual(shared.scheduled.utcoffset().total_seconds(),10800)
        self.assertEqual(shared.scheduled.hour, int(records('departure')[1]['OD_STD'][11:13]))
        cancelled = flights[0]
        self.assertEqual(cancelled.gate,'')
        self.assertEqual(cancelled.status,'Отмена')
        self.assertNotIn('Выход на посадку',format_flight(cancelled,'U6547'))

    def test_codeshare_exact_number_and_leading_zeros(self):
        self.assertEqual(normalize_number(' SU 0025 '),'SU25')
        result=answer_query({'number':'SU6401','date':'2026-11-22'},fake_board(),now=NOW)
        self.assertEqual(result.outcome,'answer')
        self.assertIn('FV 6401',result.text)
        self.assertIn('совместный рейс',result.text)
        self.assertIn('Выход на посадку: 18',result.text)
        self.assertIsNone(result.query.get('card_id'))
        result=answer_query({'number':'SU640','date':'2026-11-22'},fake_board(),now=NOW)
        self.assertEqual(result.reason,'flight_not_found')
        self.assertIn('не означает отмену',result.text)

    def test_estimated_arrival_on_next_day_keeps_scheduled_date(self):
        result=answer_query({'number':'SU2831','date':'2026-11-22'},fake_board(),now=NOW)
        self.assertEqual(result.outcome,'answer')
        self.assertIn('23.11 00:01',result.text)
        self.assertIn('22.11.2026',result.text)
        self.assertIn('Багажная лента: 2',result.text)

    def test_delay_shows_expected_time_before_original_schedule(self):
        flight=next(f for f in parse_board(records('arrival'),'arrival') if normalize_number(f.number)=='SU2831')
        for direction,expected in [('arrival','Ожидаемый прилет'),('departure','Ожидаемый вылет')]:
            with self.subTest(direction=direction):
                flight=copy.deepcopy(flight);flight.direction=direction
                text=format_flight(flight,'SU2831')
                self.assertLess(text.index('Рейс задержан.'),text.index(expected))
                self.assertLess(text.index(expected),text.index('По расписанию было:'))
                self.assertIn(expected+': 23.11 00:01',text)
                self.assertIn('По расписанию было: 22:40',text)
                self.assertNotIn('Обновление источника',text)
                self.assertNotIn('Проверено ботом',text)

    def test_delayed_flight_does_not_present_old_estimate_as_new_time(self):
        flight=parse_board(records('departure'),'departure')[1]
        flight.status='Задержан';flight.actual=None;flight.offblock=None
        for estimated in (None,flight.scheduled):
            with self.subTest(estimated=estimated):
                flight.estimated=estimated
                text=format_flight(flight,flight.number)
                self.assertIn('Новое время вылета пока не указано.',text)
                self.assertNotIn('Ожидаемый вылет:',text)

    def test_completed_flight_shows_actual_time_without_outdated_estimate(self):
        flight=parse_board(records('departure'),'departure')[1]
        self.assertIsNotNone(flight.actual)
        text=format_flight(flight,flight.number)
        self.assertIn('Вылетел в '+flight.actual.strftime('%H:%M'),text)
        self.assertNotIn('Ожидаемый вылет',text)
        self.assertNotIn('по расписанию',text)

    def test_matching_estimate_and_schedule_are_not_duplicated(self):
        flight=parse_board(records('departure'),'departure')[1]
        flight.status='Регистрация';flight.actual=None;flight.offblock=None
        flight.estimated=flight.scheduled
        text=format_flight(flight,flight.number)
        self.assertEqual(text.count(flight.scheduled.strftime('%H:%M')),1)

    def test_invalid_partial_data_fails_closed(self):
        for data in ({'error':'forbidden'},['bad'],[{'OA_FLIGHT_NUMBER':'SU25'}]):
            with self.subTest(data=data),self.assertRaises(BoardUnavailable):
                parse_board(data,'arrival')
        data=records('arrival');data[0]['OA_STA']='broken'
        with self.assertRaises(BoardUnavailable):parse_board(data,'arrival')

    def test_missing_values_do_not_become_dicts_or_invented_times(self):
        data=records('arrival');data[0]['OA_ETA']={};data[0]['OA_BAGGAGEBELTS']={}
        flight=parse_board(data,'arrival')[0]
        self.assertIsNone(flight.estimated)
        self.assertEqual(flight.belt,'')

    def test_cache_shared_under_concurrent_requests(self):
        fetch=Mock(return_value=parse_board(records('arrival'),'arrival'))
        board=Board(fetch,lambda:NOW)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(lambda _:board.get('arrival',date(2026,11,22)),range(8)))
        self.assertEqual(len(results),8)
        fetch.assert_called_once()

    def test_failed_refresh_never_serves_old_gates_and_has_cooldown(self):
        clock=[NOW]
        fetch=Mock(side_effect=[parse_board(records('departure'),'departure'),BoardUnavailable('http_403')])
        board=Board(fetch,lambda:clock[0])
        board.get('departure',date(2026,11,22))
        clock[0]+=61
        for _ in range(3):
            with self.assertRaises(BoardUnavailable):board.get('departure',date(2026,11,22))
        self.assertEqual(fetch.call_count,2)

    def test_empty_old_or_future_source_is_unavailable(self):
        for delta in (-1000,1000):
            result=answer_query({'number':'SU2831','date':'2026-11-22'},fake_board(lambda:NOW+delta),now=NOW+delta)
            self.assertEqual(result.reason,'flight_unavailable')
            self.assertNotIn('Багажная лента: 2',result.text)
        with self.assertRaises(BoardUnavailable):Board(lambda *args:[],lambda:NOW).get('arrival',date(2026,11,22))

    def test_ambiguous_arrival_departure_does_not_pick_first(self):
        arrival=parse_board(records('arrival'),'arrival')[1]
        departure=copy.deepcopy(arrival);departure.direction='departure'
        board=Board(lambda direction,*args:[arrival if direction=='arrival' else departure],lambda:NOW)
        result=answer_query({'number':normalize_number(arrival.number),'date':'2026-11-22'},board,now=NOW)
        self.assertEqual(result.outcome,'clarify')
        self.assertIn('несколько совпадений',result.text)

    def test_http_failure_redirect_large_or_non_json_response(self):
        for status,chunks in [(403,[]),(302,[]),(200,[b'<html>Forbidden</html>']),(200,[b'x'*2_000_001])]:
            response=Mock(status=status);response.stream.return_value=chunks
            manager=Mock();manager.__enter__=Mock(return_value=response);manager.__exit__=Mock(return_value=False)
            with self.subTest(status=status,size=sum(map(len,chunks))),patch('pulkovo.flights.HTTP.request',return_value=manager),self.assertRaises(BoardUnavailable):
                fetch_board('arrival',date(2026,11,22),NOW)

    def test_http_request_has_no_passenger_data_and_does_not_follow_redirects(self):
        response=Mock(status=200);response.stream.return_value=[json.dumps(records('arrival')).encode()]
        manager=Mock();manager.__enter__=Mock(return_value=response);manager.__exit__=Mock(return_value=False)
        with patch('pulkovo.flights.HTTP.request',return_value=manager) as http,patch('time.time',return_value=NOW):
            self.assertEqual(len(fetch_board('arrival',date(2026,11,22),NOW)),3)
        self.assertEqual(http.call_args.kwargs['fields'],{'type':'arrival','when':0})
        self.assertFalse(http.call_args.kwargs['redirect'])


class FlightQueryTests(unittest.TestCase):
    def test_number_date_and_clear_live_intents(self):
        for text in ('SU 0025 сегодня','рейс SU-25 22.11','Когда вылетает SU25 сегодня?',
                     'Где регистрация на SU25 сегодня?','SU25 задерживается сегодня?', 'Информация по рейсу SU25 22 ноября'):
            with self.subTest(text=text):
                query=parse_query(text,now=NOW)
                self.assertEqual(query['number'],'SU25')
                self.assertEqual(query['date'],'2026-11-22')

    def test_missing_date_and_followup(self):
        q=parse_query('SU25',now=NOW)
        self.assertIn('На какую дату',answer_query(q,now=NOW).text)
        self.assertEqual(parse_query('завтра',q,now=NOW)['date'],'2026-11-23')
        self.assertIsNone(parse_query('завтра',now=NOW))
        self.assertEqual(parse_query('а какой гейт?',dict(q,date='2026-11-22'),now=NOW)['direction'],'departure')

    def test_invalid_multiple_and_outside_dates_do_not_guess(self):
        for text in ('SU25 31.11','SU25 сегодня или завтра','SU25 22.11 и 23.11'):
            with self.subTest(text=text):self.assertIn('error',parse_query(text,now=NOW))
        board=Mock()
        result=answer_query(parse_query('SU25 30.11',now=NOW),board,now=NOW)
        self.assertEqual(result.reason,'flight_outside_board')
        board.get.assert_not_called()

    def test_new_flight_does_not_reuse_previous_date(self):
        q=parse_query('DP894',{'number':'SU25','date':'2026-11-22','direction':'departure'},now=NOW)
        self.assertEqual(q,{'number':'DP894'})

    def test_date_given_before_number_is_kept(self):
        q=parse_query('табло завтра',now=NOW)
        self.assertEqual(parse_query('SU25',q,now=NOW),{'number':'SU25','date':'2026-11-23'})

    def test_another_flight_starts_fresh_lookup_with_or_without_history(self):
        for previous in (None, {'number':'SU6401','date':'2026-11-22','direction':'departure'}):
            for text in ('другой рейс', 'А другой рейс?', 'новый рейс', 'Еще один рейс',
                         'Проверь другой рейс, пожалуйста', 'хочу проверить другой рейс'):
                with self.subTest(previous=previous,text=text):
                    query=parse_query(text,previous,now=NOW)
                    self.assertIsNotNone(query)
                    self.assertNotIn('number',query)
                    self.assertNotIn('date',query)
                    self.assertNotIn('direction',query)
                    result=answer_query(query,now=NOW)
                    self.assertEqual(result.outcome,'clarify')
                    self.assertIn('номер другого рейса и дату',result.text)

    def test_another_flight_keeps_only_explicit_new_details(self):
        previous={'number':'SU6401','date':'2026-11-22','direction':'departure'}
        query=parse_query('другой рейс SU2831 завтра',previous,now=NOW)
        self.assertEqual(query,{'number':'SU2831','date':'2026-11-23'})
        query=parse_query('другой рейс SU6401',previous,now=NOW)
        self.assertEqual(query,{'number':'SU6401'})

    def test_another_flight_date_first_then_number_keeps_new_date(self):
        query=answer_query(parse_query('другой рейс',now=NOW),now=NOW).query
        query=parse_query('завтра',query,now=NOW)
        result=answer_query(query,now=NOW)
        self.assertIn('23.11.2026',result.text)
        self.assertEqual(parse_query('SU2831',result.query,now=NOW),{'number':'SU2831','date':'2026-11-23'})

    def test_another_flight_does_not_swallow_rebooking_or_other_questions(self):
        previous={'number':'SU6401','date':'2026-11-22','direction':'departure'}
        for text in ('Как поменять билет на другой рейс?', 'Меня пересадили на другой рейс',
                     'Норма багажа на другой рейс', 'Компенсация за другой рейс', 'Не хочу другой рейс'):
            with self.subTest(text=text):self.assertIsNone(parse_query(text,previous,now=NOW))

    def test_short_live_followups_keep_context(self):
        q={'number':'SU25','date':'2026-11-22'}
        self.assertEqual(parse_query('а какая лента?',q,now=NOW)['direction'],'arrival')
        self.assertEqual(parse_query('а стойка?',q,now=NOW)['direction'],'departure')

    def test_new_year_dates(self):
        now=datetime(2026,12,31,22,tzinfo=MOSCOW).timestamp()
        self.assertEqual(parse_query('SU25 01.01',now=now)['date'],'2027-01-01')
        self.assertEqual(parse_query('SU25 1 января',now=now)['date'],'2027-01-01')

    def test_knowledge_questions_still_go_to_existing_router(self):
        for text in ('Как вернуть билет SU25 сегодня?','Норма багажа на рейсе SU25?',
                     'Где парковка?','Какой статус моего заявления?', 'Где можно купить билет?',
                     'Компенсация за задержку рейса SU25','Можно ли взять нож в ручную кладь?'):
            with self.subTest(text=text):self.assertIsNone(parse_query(text,now=NOW))


class FlightEngineTests(FeedbackFixture):
    def setUp(self):
        super().setUp()
        self.now=NOW
        self.engine.flight_board=fake_board(lambda:self.now)

    def test_flight_answer_without_ai_counts_and_arms_feedback(self):
        row=self.message('SU6401 сегодня')
        self.assertEqual((row['outcome'],row['status'],row['reason']),('answer','sent','flight_board'))
        self.assertEqual(row['topic'],'Рейсы')
        self.ai.route.assert_not_called()
        self.assertEqual(row['prompt_tokens']+row['completion_tokens'],0)
        self.assertEqual(self.survey()['status'],'pending')
        self.assertEqual(automation_summary(self.db,NOW-1,NOW+1)['automated'],1)

    def test_followup_survives_engine_restart_and_has_no_ai_history(self):
        first=self.message('SU6401')
        self.assertEqual(first['outcome'],'clarify')
        from pulkovo.engine import Engine
        self.engine=Engine(self.db,self.ai,self.cfg);self.engine.flight_board=fake_board(lambda:self.now)
        self.now+=1
        second=self.message('сегодня')
        self.assertEqual(second['reason'],'flight_board')
        self.ai.route.assert_not_called()
        self.assertEqual(self.db.history(11),[])

    def test_another_flight_clears_old_details_without_ai_or_operator(self):
        self.message('SU6401 сегодня')
        row=self.message('другой рейс')
        query=json.loads(row['flight_query'])
        self.assertEqual(row['outcome'],'clarify')
        self.assertNotIn('number',query)
        self.assertNotIn('date',query)
        self.assertNotIn('direction',query)
        self.assertEqual(row['prompt_tokens']+row['completion_tokens'],0)
        self.assertEqual(self.survey()['status'],'cancelled')
        from pulkovo.engine import Engine
        self.engine=Engine(self.db,self.ai,self.cfg);self.engine.flight_board=fake_board(lambda:self.now)
        row=self.message('SU2831')
        self.assertEqual(row['outcome'],'clarify')
        self.assertIn('На какую дату',row['reply'])
        row=self.message('сегодня')
        self.assertEqual(row['reason'],'flight_board')
        self.assertEqual(json.loads(row['flight_query'])['direction'],'arrival')
        self.assertIn('SU 2831',row['reply'])
        self.ai.route.assert_not_called()
        self.assertFalse(self.db.conversation(11)['human_mode'])

    def test_new_session_cannot_reuse_flight(self):
        self.message('SU6401')
        with self.db.connect() as con:close_session(con,11,'manual',self.now)
        self.message('сегодня')
        self.ai.route.assert_called_once()

    def test_other_topic_clears_context(self):
        self.message('SU6401')
        self.message('Где парковка?')
        self.ai.route.reset_mock()
        self.message('сегодня')
        self.ai.route.assert_called_once()

    def test_context_expires_after_thirty_minutes(self):
        self.message('SU6401')
        self.now+=1801
        self.message('сегодня')
        self.ai.route.assert_called_once()

    def test_flights_work_when_external_ai_processing_is_disabled(self):
        from dataclasses import replace
        self.engine.config=replace(self.cfg,external_user_data=False)
        row=self.message('SU6401 сегодня')
        self.assertEqual(row['reason'],'flight_board')
        self.ai.route.assert_not_called()

    def test_handoff_before_delivery_cancels_flight(self):
        row=self.message('SU6401 сегодня',deliver=False)
        self.db.take_dialogue(11,OperatorInbox(self.db).revision(11))
        self.engine.deliver(self.transport)
        self.transport.send.assert_not_called()
        self.assertEqual(self.db.one('SELECT status FROM requests WHERE id=?',(row['id'],))['status'],'cancelled')

    def test_no_lookup_without_consent_or_in_human_mode(self):
        board=Mock();self.engine.flight_board=board
        self.db.record_consent(11,11,'revoked','revoke')
        self.message('SU6401 сегодня')
        board.get.assert_not_called()
        self.db.take_dialogue(11,OperatorInbox(self.db).revision(11))
        self.message('SU6401 сегодня')
        board.get.assert_not_called()

    def test_failure_not_counted_as_answer_or_handoff(self):
        self.engine.flight_board=Mock();self.engine.flight_board.get.side_effect=BoardUnavailable()
        row=self.message('SU6401 сегодня')
        self.assertEqual(row['outcome'],'flight_info')
        self.assertFalse(self.db.conversation(11)['human_mode'])
        self.assertIsNone(self.survey())
        self.assertEqual(automation_summary(self.db,NOW-1,NOW+1)['automated'],0)
        self.ai.route.assert_not_called()

    def test_expired_ready_message_refreshes_before_send(self):
        self.message('SU6401 сегодня',deliver=False)
        self.now+=61
        self.engine.deliver(self.transport)
        self.transport.send.assert_not_called()
        self.engine.flight_board=Mock();self.engine.flight_board.get.side_effect=BoardUnavailable()
        self.engine.process(self.engine.claim())
        self.engine.deliver(self.transport)
        text=self.transport.send.call_args.args[1]
        self.assertIn('не удалось получить свежие',text)
        self.assertNotIn('Выход на посадку: 18',text)

    def test_admin_preview_shows_same_local_reply(self):
        client=authenticated_client(self.cfg,self.db)
        with patch('pulkovo.web.answer_query',side_effect=lambda q:answer_query(q,self.engine.flight_board,now=self.now)),patch('pulkovo.web.GigaChat') as ai:
            response=client.post('/preview',data={'csrf':'test','action':'ai','synthetic':'on','text':'SU6401 сегодня'})
        self.assertIn('Выход на посадку: 18',response.text)
        ai.assert_not_called()
