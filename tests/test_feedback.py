import csv
import io
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from pulkovo.config import Config
from pulkovo.db import Database
from pulkovo.engine import Engine
from pulkovo.feedback import Feedback, DELAY, COOLDOWN, QUESTION, survey_keyboard
from pulkovo.migrations import migrate
from pulkovo.operator import OperatorInbox, control_label
from pulkovo.statistics import MOSCOW, automation_summary, feedback_report
from pulkovo.web import create_app
from pulkovo.vk_staff import StaffReplySync
from web_client import authenticated_client


class FeedbackFixture(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        self.now = datetime(2026, 11, 8, 12, tzinfo=MOSCOW).timestamp()
        clock = patch('time.time', side_effect=lambda: self.now)
        clock.start(); self.addCleanup(clock.stop)
        self.db = Database(Path(folder.name)/'test.sqlite3')
        self.cfg = Config(self.db.path, group_id=123, external_user_data=True, admin_password='test', secret_key='test')
        self.db.set_settings({'consent_version': 'v1', 'consent_ready': 'true'})
        self.db.conversation(11)
        self.db.record_consent(11, 11, 'accepted', 'consent')
        self.ai = Mock()
        self.card = self.db.save_card(dict(topic='Парковка', title='Где парковка?', answer='Парковка у терминала.', status='published', reviewed=1))
        self.engine = Engine(self.db, self.ai, self.cfg)
        self.transport = Mock()
        self.transport.send.return_value = 12345
        self.transport.api.messages.getHistory.side_effect = self.history
        self.service = Feedback(self.db)
        self.serial = 0

    def history(self, **kwargs):
        rows = self.db.all('SELECT * FROM requests WHERE peer_id=11 ORDER BY id DESC LIMIT 30')
        return {'items': [dict(peer_id=11, from_id=11, conversation_message_id=r['vk_cmid'], date=int(r['vk_date'])) for r in rows]}

    def receive(self, text='Где парковка?', payload=None):
        self.serial += 1
        return self.engine.receive(11, 11, f'event-{self.serial}', text, payload=payload, vk_cmid=self.serial*2-1, vk_date=int(self.now))

    def message(self, text='Где парковка?', *, outcome='answer', payload=None, deliver=True):
        self.ai.route.return_value = dict(action=outcome, card_id=self.card if outcome=='answer' else None,
                                         kind='question', tone='neutral', question='Что вас интересует?')
        ident = self.receive(text, payload)
        self.engine.process(self.engine.claim())
        if deliver:
            self.engine.deliver(self.transport)
        return self.db.one('SELECT * FROM requests WHERE id=?', (ident,))

    def survey(self):
        return self.db.one('SELECT * FROM feedback_surveys ORDER BY id DESC LIMIT 1')

    def send_survey(self, *, legacy=False):
        self.message()
        if legacy:
            self.db.execute("UPDATE feedback_surveys SET rating_scale='binary',text=? WHERE id=?",
                ('Как вы оцениваете помощь бота?\n\nВыберите «Хорошо» или «Плохо».', self.survey()['id']))
        self.now += DELAY
        self.assertTrue(self.service.deliver(self.transport, self.engine))
        return self.survey()

    def payload(self, survey=None, rating=5):
        return dict(action='feedback', token=(survey or self.survey())['token'], rating=rating)


class FeedbackDeliveryTests(FeedbackFixture):
    def test_sends_after_exactly_one_hour_with_five_inline_buttons(self):
        self.message()
        self.transport.send.reset_mock()
        self.now += DELAY-1
        self.assertFalse(self.service.deliver(self.transport, self.engine))
        self.transport.send.assert_not_called()
        self.now += 1
        self.assertTrue(self.service.deliver(self.transport, self.engine))
        peer, text, raw, random_id = self.transport.send.call_args.args
        self.assertEqual((peer, text, random_id), (11, QUESTION, self.survey()['random_id']))
        keys = json.loads(raw)
        self.assertTrue(keys['inline'])
        self.assertEqual([b['action']['label'] for b in keys['buttons'][0]], [f'{n} ⭐' for n in range(1, 6)])
        for n, button in enumerate(keys['buttons'][0], 1):
            self.assertEqual(button['action']['type'], 'text')
            self.assertEqual(json.loads(button['action']['payload']), self.payload(rating=n))
        self.assertEqual(self.survey()['vk_message_id'], 12345)
        self.now += COOLDOWN
        self.assertFalse(self.service.deliver(self.transport, self.engine))
        self.transport.send.assert_called_once()

    def test_only_successful_answer_arms_survey(self):
        self.message(deliver=False)
        self.assertIsNone(self.survey())
        self.transport.send.side_effect = RuntimeError('network')
        for _ in range(3):
            self.engine.deliver(self.transport); self.now += 11
        self.assertIsNone(self.survey())

    def test_thanks_alone_does_not_create_survey(self):
        self.message('Спасибо')
        self.assertIsNone(self.survey())
        self.ai.route.assert_not_called()

    def test_thanks_after_answer_restarts_timer(self):
        self.message(); original = self.survey()
        self.now += 1200
        self.message('Спасибо')
        self.assertEqual(self.survey()['id'], original['id'])
        self.assertEqual(self.survey()['due_at'], self.now+DELAY)
        self.now += DELAY-1
        self.assertFalse(self.service.deliver(self.transport, self.engine))
        self.now += 1
        self.assertTrue(self.service.deliver(self.transport, self.engine))

    def test_new_question_restarts_timer_before_processing_and_supersedes_answer(self):
        self.message(); old = self.survey()
        self.now += 1200
        self.receive()
        self.assertEqual(self.survey()['due_at'], self.now+DELAY)
        self.now += DELAY
        self.assertFalse(self.service.deliver(self.transport, self.engine))
        self.engine.process(self.engine.claim()); self.engine.deliver(self.transport)
        self.assertNotEqual(old['id'], self.survey()['id'])
        self.assertEqual(self.survey()['due_at'], self.now+DELAY)
        self.assertEqual(self.db.one('SELECT status FROM feedback_surveys WHERE id=?', (old['id'],))['status'], 'cancelled')

    def test_clarification_cancels_invitation(self):
        self.message(); self.now += 30
        self.message(outcome='clarify')
        self.assertEqual(self.survey()['status'], 'cancelled')
        self.now += DELAY
        self.assertFalse(self.service.deliver(self.transport, self.engine))

    def test_handoff_and_return_do_not_rearm_old_answer(self):
        self.message()
        self.message('/оператор')
        self.assertEqual(self.survey()['reason'], 'handoff')
        self.db.resume(11)
        self.now += DELAY
        self.assertFalse(self.service.deliver(self.transport, self.engine))

    def test_staff_reply_and_admin_reply_cancel_pending(self):
        self.message(); self.now += 1
        self.db.staff_replied(11, 'staff', 99, 10, int(self.now), text='Поможем')
        self.assertEqual(self.survey()['status'], 'cancelled')
        self.db.resume(11); self.now += 1
        self.message()
        inbox = OperatorInbox(self.db)
        inbox.queue(11, 'Поможем', 'Сотрудник', 'client-key-1234567890', inbox.revision(11))
        self.assertEqual(self.survey()['status'], 'cancelled')

    def test_consent_change_or_disabling_cancels_pending(self):
        self.message()
        self.db.record_consent(11, 11, 'revoked', 'revoke')
        self.assertEqual(self.survey()['status'], 'cancelled')
        self.db.record_consent(11, 11, 'accepted', 'agree-again')
        self.message()
        self.db.set_settings({'feedback_enabled': 'false'})
        self.assertEqual(self.survey()['status'], 'cancelled')
        self.message()
        self.assertEqual(self.db.one("SELECT count(*) n FROM feedback_surveys WHERE status='pending'")['n'], 0)

    def test_consent_version_change_blocks_due_survey(self):
        self.message(); self.now += DELAY
        self.db.set_settings({'consent_version': 'v2'})
        self.assertFalse(self.service.deliver(self.transport, self.engine))
        self.assertEqual(self.survey()['reason'], 'dialogue_changed')

    def test_once_per_24_hours_even_after_more_answers(self):
        first = self.send_survey()
        self.now += 1; self.message(); self.now += DELAY
        self.assertFalse(self.service.deliver(self.transport, self.engine))
        self.assertEqual(self.survey()['reason'], 'daily_limit')
        self.now = first['sent_at']+COOLDOWN-DELAY
        self.message(); self.now += DELAY
        self.assertTrue(self.service.deliver(self.transport, self.engine))

    def test_stale_invitation_after_downtime_is_not_sent(self):
        self.message(); self.now += COOLDOWN
        self.assertFalse(self.service.deliver(self.transport, self.engine))
        self.assertEqual(self.survey()['reason'], 'stale_invitation')

    def test_missed_incoming_or_history_failure_defers_invitation(self):
        self.message(); self.now += DELAY
        self.transport.api.messages.getHistory.side_effect = None
        self.transport.api.messages.getHistory.return_value = {'items': [dict(peer_id=11, from_id=11, conversation_message_id=50, date=int(self.now))]}
        self.assertFalse(self.service.deliver(self.transport, self.engine))
        self.assertEqual(self.survey()['reason'], 'unseen_activity')
        self.now += 60
        self.transport.api.messages.getHistory.side_effect = RuntimeError('VK down')
        self.assertFalse(self.service.deliver(self.transport, self.engine))
        self.assertEqual(self.survey()['reason'], 'history_unavailable')
        self.assertEqual(self.survey()['attempts'], 0)

    def test_unseen_staff_or_unknown_outgoing_prevents_survey(self):
        for admin in (99, None):
            self.message(); self.now += DELAY
            self.transport.api.messages.getHistory.side_effect = None
            self.transport.api.messages.getHistory.return_value = {'items': [dict(peer_id=11, from_id=-123, date=int(self.now), admin_author_id=admin)]}
            self.assertFalse(self.service.deliver(self.transport, self.engine))
            self.assertEqual(self.survey()['status'], 'cancelled')

    def test_activity_during_history_preflight_cancels_send(self):
        self.message(); self.now += DELAY
        def history(**kwargs):
            items = self.history(**kwargs)
            self.receive('А где терминал?')
            return items
        self.transport.api.messages.getHistory.side_effect = history
        self.transport.send.reset_mock()
        self.assertFalse(self.service.deliver(self.transport, self.engine))
        self.transport.send.assert_not_called()

    def test_technical_retries_use_same_random_id_and_never_handoff(self):
        self.message(); self.now += DELAY
        self.transport.send.reset_mock()
        self.transport.send.side_effect = RuntimeError('timeout')
        for _ in range(3):
            self.assertFalse(self.service.deliver(self.transport, self.engine)); self.now += 60
        self.assertEqual(self.survey()['status'], 'failed')
        self.assertEqual(len({c.args[3] for c in self.transport.send.call_args_list}), 1)
        self.assertEqual(self.db.all('SELECT * FROM tickets'), [])
        self.assertFalse(self.service.deliver(self.transport, self.engine))
        self.assertEqual(self.transport.send.call_count, 3)

    def test_lost_send_response_reconciles_without_repeat(self):
        self.message(); self.now += DELAY
        self.transport.send.side_effect = RuntimeError('lost response')
        self.assertFalse(self.service.deliver(self.transport, self.engine))
        self.transport.send.reset_mock()
        self.now += 60
        self.transport.api.messages.getHistory.side_effect = None
        self.transport.api.messages.getHistory.return_value = {'items': [dict(peer_id=11, from_id=-123, date=int(self.now)-60, id=999, payload=json.dumps({'pulkovo_bot': self.survey()['random_id']}))]}
        self.assertTrue(self.service.deliver(self.transport, self.engine))
        self.transport.send.assert_not_called()
        self.assertEqual(self.survey()['vk_message_id'], 999)

    def test_failed_attempt_cooldown_applies_to_new_answer(self):
        self.message(); self.now += DELAY
        self.transport.send.side_effect = RuntimeError('ambiguous')
        self.service.deliver(self.transport, self.engine)
        self.transport.send.side_effect = None
        self.now += 1; self.message(); self.now += DELAY
        self.assertFalse(self.service.deliver(self.transport, self.engine))
        self.assertEqual(self.survey()['reason'], 'daily_limit')

    def test_pending_survives_restart_and_is_claimed_only_once(self):
        self.message(); self.now += DELAY
        restarted = Feedback(Database(self.db.path))
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(lambda _: restarted._claim(self.now), range(2)))
        self.assertEqual(sum(bool(c) for c in claims), 1)

    def test_interrupted_claim_is_recovered_after_lease(self):
        self.message(); self.now += DELAY
        self.assertIsNotNone(self.service._claim(self.now))
        self.assertFalse(self.service.deliver(self.transport, self.engine))
        self.now += 301
        self.assertTrue(self.service.deliver(self.transport, self.engine))
        self.assertEqual(self.survey()['attempts'], 1)

    def test_random_id_allocation_avoids_existing_survey(self):
        survey = self.send_survey()
        with patch('pulkovo.feedback.secrets.randbelow', side_effect=[survey['random_id']-1, 7654321]):
            ident = self.receive()
        self.assertEqual(self.db.one('SELECT random_id FROM requests WHERE id=?', (ident,))['random_id'], 7654322)

    def test_no_backfill_for_old_answers(self):
        self.db.set_settings({'feedback_enabled': 'false'})
        self.message()
        self.db.set_settings({'feedback_enabled': 'true'})
        Database(self.db.path)
        self.assertIsNone(self.survey())

    def test_survey_is_recognized_as_bot_message_and_appears_in_history(self):
        survey = self.send_survey()
        message = dict(peer_id=11, from_id=-123, admin_author_id=99, random_id=survey['random_id'])
        self.assertTrue(self.engine.is_own_reply(message))
        self.assertFalse(self.engine.receive_reply(message, 'survey-reply'))
        timeline = OperatorInbox(self.db).timeline(11)
        self.assertIn(QUESTION, timeline[-1]['text'])
        self.assertIn('1 ⭐ · 2 ⭐ · 3 ⭐ · 4 ⭐ · 5 ⭐', timeline[-1]['text'])
        self.assertFalse(self.db.conversation(11)['human_mode'])


class FeedbackVoteTests(FeedbackFixture):
    def test_vote_is_local_once_and_excluded_from_automation(self):
        survey = self.send_survey(); self.ai.reset_mock()
        before = automation_summary(self.db, 0, self.now+100)
        row = self.message('5 ⭐', payload=self.payload(survey))
        self.assertEqual((row['outcome'], row['reply']), ('feedback', 'Спасибо за оценку!\n\nЕсли появится новый вопрос, напишите сюда.'))
        self.assertEqual(self.survey()['rating_stars'], 5)
        self.assertIsNone(self.survey()['rating'])
        row = self.message('1 ⭐', payload=self.payload(survey, 1))
        self.assertEqual((row['outcome'], row['reply']), ('feedback_duplicate', None))
        self.assertEqual(self.survey()['rating_stars'], 5)
        self.assertEqual(self.ai.mock_calls, [])
        self.assertEqual(automation_summary(self.db, 0, self.now+100), before)
        self.assertEqual(self.db.all('SELECT * FROM tickets'), [])

    def test_forged_vote_or_wrong_owner_does_not_change_rating(self):
        survey = self.send_survey()
        for peer, payload in [(12, self.payload()), (11, {'token': 'forged', 'rating': 1}),
                              (11, self.payload(rating='unknown')), (11, {'token': [], 'rating': 5})]:
            self.assertEqual(self.service.vote(peer, payload), 'invalid')
        self.assertIsNone(self.survey()['rating'])
        self.assertIsNone(self.survey()['rating_stars'])
        self.ai.reset_mock()
        row = self.message('1 ⭐', payload=dict(action='feedback', token='forged', rating=1))
        self.assertEqual(row['outcome'], 'feedback_invalid')
        self.ai.route.assert_not_called()
        self.assertFalse(self.db.conversation(11)['human_mode'])

    def test_pending_invitation_cannot_be_rated_before_send(self):
        self.message()
        self.assertEqual(self.service.vote(11, self.payload()), 'invalid')

    def test_old_vote_does_not_reopen_staff_waiting_or_require_ai_consent(self):
        survey = self.send_survey(); self.now += 1
        self.db.staff_replied(11, 'staff', 99, 10, int(self.now), text='Поможем')
        self.db.record_consent(11, 11, 'revoked', 'revoke')
        generation = self.db.conversation(11)['generation']
        self.ai.reset_mock()
        self.message('1 ⭐', payload=self.payload(survey, 1))
        ticket = self.db.one('SELECT * FROM tickets')
        self.assertEqual(ticket['status'], 'waiting_user')
        self.assertEqual(self.db.conversation(11)['generation'], generation)
        self.assertTrue(self.db.conversation(11)['human_mode'])
        self.assertEqual(self.ai.mock_calls, [])
        self.assertEqual(self.survey()['rating_stars'], 1)

    def test_vk_history_vote_does_not_reopen_waiting_ticket(self):
        survey = self.send_survey(); self.now += 1
        staff = dict(id=80, peer_id=11, from_id=-123, conversation_message_id=10, date=int(self.now), admin_author_id=99, text='Поможем')
        self.engine.receive_reply(staff, 'staff')
        self.now += 60
        vote = dict(id=81, peer_id=11, from_id=11, conversation_message_id=11, date=int(self.now), payload=json.dumps(self.payload(survey)))
        api = Mock(); api.messages.getHistory.return_value = {'items': [vote, staff]}
        sync = StaffReplySync(self.db, self.engine, api)
        sync.sync_ticket(self.db.one('SELECT * FROM tickets'))
        self.assertEqual(self.db.one('SELECT status FROM tickets')['status'], 'waiting_user')

    def test_late_staff_event_uses_question_instead_of_rating_as_basis(self):
        survey = self.send_survey(); self.now += 60
        self.message('1 ⭐', payload=self.payload(survey, 1))
        self.db.staff_replied(11, 'staff', 99, 2, int(self.now)-30, text='Поможем')
        ticket = self.db.one('SELECT * FROM tickets')
        self.assertEqual(ticket['request_id'], survey['request_id'])
        self.assertEqual(ticket['status'], 'waiting_user')

    def test_rating_does_not_restart_another_pending_invitation(self):
        old = self.send_survey()
        self.now += COOLDOWN
        self.message(); current = self.survey()
        self.now += 600
        self.message('5 ⭐', payload=self.payload(old))
        self.assertEqual(self.survey()['due_at'], current['due_at'])
        self.now = current['due_at']
        self.assertTrue(self.service.deliver(self.transport, self.engine))

    def test_vote_confirms_lost_send_and_ack_failure_does_not_handoff(self):
        self.message(); self.now += DELAY
        self.transport.send.side_effect = RuntimeError('lost response')
        self.service.deliver(self.transport, self.engine)
        self.assertIsNone(self.survey()['sent_at'])
        self.message('5 ⭐', payload=self.payload(), deliver=False)
        self.assertEqual(self.survey()['status'], 'sent')
        self.assertEqual(self.survey()['rating_stars'], 5)
        for _ in range(3):
            self.engine.deliver(self.transport); self.now += 11
        self.assertEqual(self.db.all('SELECT * FROM tickets'), [])

    def test_concurrent_votes_accept_only_first(self):
        self.send_survey()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda rating: self.service.vote(11, self.payload(rating=rating)), [5, 1]))
        self.assertCountEqual(results, ['accepted', 'duplicate'])

    def test_control_label_preserves_button_action_without_saved_text(self):
        survey = self.send_survey()
        self.assertEqual(control_label(json.dumps(self.payload(survey, 2))), 'Оценка: 2 из 5 ⭐')
        row = self.message('', payload=self.payload(survey, 2))
        item = next(m for m in OperatorInbox(self.db).timeline(11) if m['id'] == f"request-{row['id']}")
        self.assertEqual(item['text'], 'Оценка: 2 из 5 ⭐')

    def test_all_five_scores_are_saved_and_survive_restart(self):
        for n in range(1, 6):
            with self.subTest(rating=n):
                survey = self.send_survey()
                self.ai.reset_mock()
                row = self.message(f'{n} ⭐', payload=self.payload(survey, n))
                self.assertEqual(row['outcome'], 'feedback')
                restarted = Database(self.db.path)
                saved = restarted.one('SELECT * FROM feedback_surveys WHERE id=?', (survey['id'],))
                self.assertEqual(saved['rating_stars'], n)
                self.assertIsNone(saved['rating'])
                self.assertEqual(self.ai.mock_calls, [])
                self.now += COOLDOWN

    def test_invalid_star_scores_and_wrong_scale_cannot_be_saved(self):
        self.send_survey()
        for rating in (0, 6, -1, True, False, 1.0, 2.5, '1.0', ' 5', '05', None, [], {}, 'good', 'bad'):
            with self.subTest(rating=rating):
                self.assertEqual(self.service.vote(11, self.payload(rating=rating)), 'invalid')
        self.assertIsNone(self.survey()['rating_stars'])
        self.assertEqual(self.service.vote(11, self.payload(rating='4')), 'accepted')

    def test_old_buttons_keep_binary_scale_and_can_still_be_rated(self):
        survey = self.send_survey(legacy=True)
        self.assertEqual([b['action']['label'] for b in json.loads(survey_keyboard(survey))['buttons'][0]], ['Хорошо', 'Плохо'])
        self.assertEqual(self.service.vote(11, self.payload(survey, 5)), 'invalid')
        self.ai.reset_mock()
        row = self.message('Хорошо', payload=self.payload(survey, 'good'))
        self.assertEqual(row['outcome'], 'feedback')
        self.assertEqual(self.survey()['rating'], 'good')
        self.assertIsNone(self.survey()['rating_stars'])
        self.assertEqual(self.service.vote(11, self.payload(survey, 'bad')), 'duplicate')
        self.assertEqual(self.ai.mock_calls, [])
        self.assertEqual(control_label(json.dumps(self.payload(survey, 'bad'))), 'Оценка «Плохо»')
        history = next(m for m in OperatorInbox(self.db).timeline(11) if m['id'] == f"survey-{survey['id']}")
        self.assertIn('Хорошо · Плохо', history['text'])
        self.assertNotIn('⭐', history['text'])


class FeedbackStatisticsTests(FeedbackFixture):
    def add_survey(self, sent_at, rating=None, *, legacy=False):
        self.message()
        survey = self.survey()
        legacy = legacy or rating in ('good', 'bad')
        self.db.execute("UPDATE feedback_surveys SET status='sent',sent_at=?,rating=?,rating_stars=?,rating_scale=?,rated_at=? WHERE id=?",
            (sent_at, rating if legacy else None, None if legacy else rating, 'binary' if legacy else 'stars', self.now if rating else None, survey['id']))
        return survey

    def test_weighted_percent_response_rate_days_and_late_votes(self):
        start = datetime(2026, 11, 7, tzinfo=MOSCOW).timestamp()
        for _ in range(3): self.add_survey(start+1, 5)
        self.add_survey(start+86401, 1)
        self.add_survey(start+86402)
        self.add_survey(start-1, 1)
        report = feedback_report(self.db, start, self.now)
        self.assertEqual({key: report['summary'][key] for key in ('sent', 'rated', 'average', 'percent', 'response_percent')},
            dict(sent=5, rated=4, average=4, percent=75, response_percent=80))
        self.assertEqual([report['summary'][f'stars_{n}'] for n in range(1, 6)], [1, 0, 0, 0, 3])
        self.assertEqual([row['percent'] for row in report['days']], [0, 100])
        self.assertEqual(len(report['negative']), 1)
        yesterday = feedback_report(self.db, start, start+86400)
        self.assertEqual(yesterday['summary']['stars_5'], 3)
        self.assertEqual(yesterday['summary']['average'], 5)
        self.assertEqual(len(yesterday['days']), 1)

    def test_no_votes_are_not_positive_or_zero_percent(self):
        self.add_survey(self.now, 'good')
        self.add_survey(self.now, 'bad')
        self.add_survey(self.now, legacy=True)
        report = feedback_report(self.db, self.now-86400, self.now+1)
        self.assertEqual((report['summary']['sent'], report['summary']['rated']), (0, 0))
        self.assertEqual(report['negative'], [])
        self.assertIsNone(report['summary']['percent'])
        self.assertIsNone(report['summary']['response_percent'])
        self.assertIsNone(report['summary']['average'])
        self.add_survey(self.now)
        report = feedback_report(self.db, self.now-86400, self.now+1)
        self.assertIsNone(report['summary']['percent'])
        self.assertEqual(report['summary']['response_percent'], 0)
        self.assertIsNone(report['summary']['average'])

    def test_statistics_html_csv_auth_and_safe_negative_reviews(self):
        survey = self.add_survey(self.now-10, 2)
        self.db.execute('UPDATE requests SET text=?,reply=? WHERE id=?', ('<script>bad()</script>', 'Исторический ответ', survey['request_id']))
        client = authenticated_client(self.cfg, self.db); self.addCleanup(client.close)
        page = client.get('/statistics?period=today')
        self.assertEqual(page.status_code, 200)
        self.assertIn('Оценки помощи бота', page.text)
        self.assertIn('Исторический ответ', page.text)
        self.assertIn('&lt;script&gt;', page.text)
        self.assertNotIn('<script>bad()', page.text)
        self.assertIn('/dialogue/11', page.text)
        self.assertIn('Средняя оценка', page.text)
        self.assertIn('2,00', page.text)
        self.assertIn('Распределение оценок', page.text)
        self.assertIn('2 из 5 ⭐', page.text)
        self.assertNotIn('Прежние оценки «Хорошо / Плохо»', page.text)
        exported = client.get('/feedback.csv?period=today')
        self.assertEqual(exported.status_code, 200)
        values = list(csv.DictReader(io.StringIO(exported.text.lstrip('\ufeff'))))[0]
        self.assertEqual(values['Дата опроса (Москва)'], '2026-11-08')
        self.assertEqual(values['2 звезды'], '1')
        self.assertEqual(values['Средняя оценка из 5'], '2.00')
        self.assertEqual(values['Доля ответивших на опрос со звездами, %'], '100.0')
        self.assertFalse(any('Старый формат' in column for column in values))
        self.assertEqual(client.get('/feedback.csv?period=invalid').status_code, 400)
        anonymous = TestClient(create_app(self.cfg, self.db), follow_redirects=False)
        self.addCleanup(anonymous.close)
        self.assertEqual(anonymous.get('/feedback.csv').status_code, 303)

    def test_retention_keeps_metrics_without_question_text(self):
        self.add_survey(self.now, 1)
        self.now += 31*86400
        self.db.purge()
        report = feedback_report(self.db, 0, self.now)
        self.assertEqual(report['summary']['stars_1'], 1)
        self.assertEqual(report['negative'][0]['text'], '')
        self.assertIsNone(report['negative'][0]['reply'])

    def test_legacy_votes_are_excluded_from_reports_html_and_csv(self):
        for score in (1, 2, 3, 4, 5):
            self.add_survey(self.now-10, score)
        self.add_survey(self.now-10)
        before = feedback_report(self.db, self.now-86400, self.now+1)
        self.add_survey(self.now-10, 'good')
        self.add_survey(self.now-10, 'good')
        self.add_survey(self.now-10, 'bad')
        self.add_survey(self.now-10, legacy=True)
        report = feedback_report(self.db, self.now-86400, self.now+1)
        self.assertEqual(report, before)
        result = report['summary']
        self.assertEqual((result['sent'], result['rated'], result['average'], result['percent']), (6, 5, 3, 40))
        self.assertAlmostEqual(result['response_percent'], 100*5/6)
        self.assertEqual([result[f'stars_{n}'] for n in range(1, 6)], [1]*5)
        self.assertEqual(self.db.one("SELECT count(*) n FROM feedback_surveys WHERE rating_scale='binary'")['n'], 4)
        self.assertEqual(len(report['negative']), 2)
        self.assertEqual({row['rating_stars'] for row in report['negative']}, {1, 2})
        client = authenticated_client(self.cfg, self.db); self.addCleanup(client.close)
        page = client.get('/statistics?period=today').text
        self.assertNotIn('Хорошо', page)
        self.assertNotIn('Плохо', page)
        exported = client.get('/feedback.csv?period=today').text
        row = list(csv.DictReader(io.StringIO(exported.lstrip('\ufeff'))))[0]
        self.assertEqual(row['Средняя оценка из 5'], '3.00')
        self.assertEqual(row['Отправлено опросов со звездами'], '6')
        self.assertEqual(row['Всего оценок со звездами'], '5')
        self.assertNotIn('Хорошо', exported)
        self.assertNotIn('Плохо', exported)
        self.assertNotIn('Старый формат', exported)

    def test_late_vote_updates_invitation_day_and_period_end_is_exclusive(self):
        start = datetime(2026, 11, 7, tzinfo=MOSCOW).timestamp()
        old = self.add_survey(start+10)
        self.add_survey(start+86400, 1)
        self.assertEqual(self.service.vote(11, self.payload(old, 4)), 'accepted')
        result = feedback_report(self.db, start, start+86400)['summary']
        self.assertEqual((result['sent'], result['rated'], result['average']), (1, 1, 4))

    def test_setting_form_can_disable_and_enable_surveys(self):
        self.message()
        client = authenticated_client(self.cfg, self.db); self.addCleanup(client.close)
        self.assertIn('name="feedback_enabled" checked', client.get('/settings').text)
        self.assertEqual(client.post('/settings', data={'csrf': 'test', 'consent_ready': 'on'}).status_code, 303)
        self.assertEqual(self.db.setting('feedback_enabled'), 'false')
        self.assertEqual(self.survey()['status'], 'cancelled')
        client.post('/settings', data={'csrf': 'test', 'consent_ready': 'on', 'feedback_enabled': 'on'})
        self.assertEqual(self.db.setting('feedback_enabled'), 'true')


class FeedbackMigrationTests(unittest.TestCase):
    def test_upgrade_preserves_votes_and_uncertain_deliveries_and_converts_unsent_only(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            old_sql = folder/'old-sql'; old_sql.mkdir()
            for path in (Path(__file__).parent.parent/'pulkovo/sql').glob('*.sql'):
                if path.name < '014':
                    (old_sql/path.name).write_bytes(path.read_bytes())
            db_path = folder/'old.sqlite3'
            migrate(db_path, old_sql)
            # Each active survey belongs to a different user, as in production.
            cases = [
                ('sent', 1, 100, 100, 'good'),
                ('sent', 1, 100, 100, None),
                ('pending', 0, None, None, None),
                ('pending', 1, 100, None, None),
                ('sending', 1, 100, None, None),
                ('failed', 3, 100, None, None),
                ('cancelled', 0, None, None, None),
                ('sent', 1, 100, 100, 'bad'),
            ]
            old_text = 'Как вы оцениваете помощь бота?\n\nВыберите «Хорошо» или «Плохо».'
            con = sqlite3.connect(db_path)
            try:
                with con:
                    for ident, (status, attempts, attempted, sent, rating) in enumerate(cases, 1):
                        con.execute('INSERT INTO conversations(peer_id,user_id,updated_at) VALUES (?,?,100)', (ident, ident))
                        con.execute("INSERT INTO requests(id,event_key,peer_id,user_id,input_type,random_id,created_at,updated_at) VALUES (?,?,?,?,'text',?,100,100)", (ident, f'question-{ident}', ident, ident, ident))
                        con.execute("""INSERT INTO feedback_surveys
                            (id,peer_id,request_id,generation,activity_request_id,last_activity_at,due_at,token,random_id,text,status,
                             attempts,attempted_at,sent_at,rating,rated_at,created_at,updated_at)
                            VALUES (?,?,?,0,?,100,3700,?,?,?,?,?,?,?,?,?,100,100)""",
                            (ident, ident, ident, ident, f'token-{ident}', 100+ident, old_text, status, attempts, attempted, sent, rating, 120 if rating else None))
            finally:
                con.close()
            db = Database(db_path)
            self.assertEqual(migrate(db_path), [])
            self.assertTrue(list((folder/'backups').glob('*.sqlite3')))
            self.assertEqual(db.all('PRAGMA foreign_key_check'), [])
            for ident, case in enumerate(cases, 1):
                row = db.one('SELECT * FROM feedback_surveys WHERE id=?', (ident,))
                self.assertEqual((row['status'], row['attempts'], row['attempted_at'], row['sent_at'], row['rating']), case)
                self.assertEqual((row['token'], row['random_id']), (f'token-{ident}', 100+ident))
                self.assertIsNone(row['rating_stars'])
                self.assertEqual(row['rating_scale'], 'stars' if ident == 3 else 'binary')
                self.assertEqual(row['text'], QUESTION if ident == 3 else old_text)
            service = Feedback(db)
            self.assertEqual(service.vote(2, dict(token='token-2', rating='bad')), 'accepted')
            self.assertEqual(service.vote(1, dict(token='token-1', rating='bad')), 'duplicate')
            # A click can confirm an old delivery whose API response was lost.
            self.assertEqual(service.vote(4, dict(token='token-4', rating='good')), 'accepted')
            self.assertEqual(db.one('SELECT rating FROM feedback_surveys WHERE id=4')['rating'], 'good')
            self.assertEqual(service.vote(3, dict(token='token-3', rating=5)), 'invalid')
            db.execute("UPDATE feedback_surveys SET sent_at=200,status='sent' WHERE id=3")
            self.assertEqual(service.vote(3, dict(token='token-3', rating=5)), 'accepted')
            for sql in ("UPDATE feedback_surveys SET rating_stars=0 WHERE id=3",
                        "UPDATE feedback_surveys SET rating_stars=6 WHERE id=3",
                        "UPDATE feedback_surveys SET rating_stars=1.5 WHERE id=3",
                        "UPDATE feedback_surveys SET rating='good' WHERE id=3",
                        "UPDATE feedback_surveys SET rating_stars=5 WHERE id=1"):
                with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                    db.execute(sql)


if __name__ == '__main__':
    unittest.main()
