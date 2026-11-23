import json
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
from pulkovo.feedback import Feedback, DELAY, COOLDOWN, QUESTION
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

    def send_survey(self):
        self.message()
        self.now += DELAY
        self.assertTrue(self.service.deliver(self.transport, self.engine))
        return self.survey()

    def payload(self, survey=None, rating='good'):
        return dict(action='feedback', token=(survey or self.survey())['token'], rating=rating)


class FeedbackDeliveryTests(FeedbackFixture):
    def test_sends_after_exactly_one_hour_with_two_inline_buttons(self):
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
        self.assertEqual([b['action']['label'] for b in keys['buttons'][0]], ['Хорошо', 'Плохо'])
        self.assertEqual(json.loads(keys['buttons'][0][0]['action']['payload']), self.payload())
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
        self.assertFalse(self.db.conversation(11)['human_mode'])


class FeedbackVoteTests(FeedbackFixture):
    def test_vote_is_local_once_and_excluded_from_automation(self):
        survey = self.send_survey(); self.ai.reset_mock()
        before = automation_summary(self.db, 0, self.now+100)
        row = self.message('Хорошо', payload=self.payload(survey))
        self.assertEqual((row['outcome'], row['reply']), ('feedback', 'Спасибо за оценку!\n\nЕсли появится новый вопрос, напишите сюда.'))
        self.assertEqual(self.survey()['rating'], 'good')
        row = self.message('Плохо', payload=self.payload(survey, 'bad'))
        self.assertEqual((row['outcome'], row['reply']), ('feedback_duplicate', None))
        self.assertEqual(self.survey()['rating'], 'good')
        self.assertEqual(self.ai.mock_calls, [])
        self.assertEqual(automation_summary(self.db, 0, self.now+100), before)
        self.assertEqual(self.db.all('SELECT * FROM tickets'), [])

    def test_forged_vote_or_wrong_owner_does_not_change_rating(self):
        survey = self.send_survey()
        for peer, payload in [(12, self.payload()), (11, {'token': 'forged', 'rating': 'bad'}),
                              (11, self.payload(rating='unknown')), (11, {'token': [], 'rating': 'good'})]:
            self.assertEqual(self.service.vote(peer, payload), 'invalid')
        self.assertIsNone(self.survey()['rating'])
        self.ai.reset_mock()
        row = self.message('Плохо', payload=dict(action='feedback', token='forged', rating='bad'))
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
        self.message('Плохо', payload=self.payload(survey, 'bad'))
        ticket = self.db.one('SELECT * FROM tickets')
        self.assertEqual(ticket['status'], 'waiting_user')
        self.assertEqual(self.db.conversation(11)['generation'], generation)
        self.assertTrue(self.db.conversation(11)['human_mode'])
        self.assertEqual(self.ai.mock_calls, [])
        self.assertEqual(self.survey()['rating'], 'bad')

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
        self.message('Плохо', payload=self.payload(survey, 'bad'))
        self.db.staff_replied(11, 'staff', 99, 2, int(self.now)-30, text='Поможем')
        ticket = self.db.one('SELECT * FROM tickets')
        self.assertEqual(ticket['request_id'], survey['request_id'])
        self.assertEqual(ticket['status'], 'waiting_user')

    def test_rating_does_not_restart_another_pending_invitation(self):
        old = self.send_survey()
        self.now += COOLDOWN
        self.message(); current = self.survey()
        self.now += 600
        self.message('Хорошо', payload=self.payload(old))
        self.assertEqual(self.survey()['due_at'], current['due_at'])
        self.now = current['due_at']
        self.assertTrue(self.service.deliver(self.transport, self.engine))

    def test_vote_confirms_lost_send_and_ack_failure_does_not_handoff(self):
        self.message(); self.now += DELAY
        self.transport.send.side_effect = RuntimeError('lost response')
        self.service.deliver(self.transport, self.engine)
        self.assertIsNone(self.survey()['sent_at'])
        self.message('Хорошо', payload=self.payload(), deliver=False)
        self.assertEqual(self.survey()['status'], 'sent')
        self.assertEqual(self.survey()['rating'], 'good')
        for _ in range(3):
            self.engine.deliver(self.transport); self.now += 11
        self.assertEqual(self.db.all('SELECT * FROM tickets'), [])

    def test_concurrent_votes_accept_only_first(self):
        self.send_survey()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda rating: self.service.vote(11, self.payload(rating=rating)), ['good', 'bad']))
        self.assertCountEqual(results, ['accepted', 'duplicate'])

    def test_control_label_preserves_button_action_without_saved_text(self):
        self.assertEqual(control_label(json.dumps(self.payload(self.send_survey(), 'bad'))), 'Оценка «Плохо»')


class FeedbackStatisticsTests(FeedbackFixture):
    def add_survey(self, sent_at, rating=None):
        self.message()
        survey = self.survey()
        self.db.execute("UPDATE feedback_surveys SET status='sent',sent_at=?,rating=?,rated_at=? WHERE id=?", (sent_at, rating, self.now if rating else None, survey['id']))
        return survey

    def test_weighted_percent_response_rate_days_and_late_votes(self):
        start = datetime(2026, 11, 7, tzinfo=MOSCOW).timestamp()
        for _ in range(3): self.add_survey(start+1, 'good')
        self.add_survey(start+86401, 'bad')
        self.add_survey(start+86402)
        self.add_survey(start-1, 'bad')
        report = feedback_report(self.db, start, self.now)
        self.assertEqual(report['summary'], dict(sent=5, good=3, bad=1, rated=4, percent=75, response_percent=80))
        self.assertEqual([row['percent'] for row in report['days']], [0, 100])
        self.assertEqual(len(report['negative']), 1)
        yesterday = feedback_report(self.db, start, start+86400)
        self.assertEqual(yesterday['summary']['good'], 3)
        self.assertEqual(len(yesterday['days']), 1)

    def test_no_votes_are_not_positive_or_zero_percent(self):
        report = feedback_report(self.db, self.now-86400, self.now+1)
        self.assertIsNone(report['summary']['percent'])
        self.assertIsNone(report['summary']['response_percent'])
        self.add_survey(self.now)
        report = feedback_report(self.db, self.now-86400, self.now+1)
        self.assertIsNone(report['summary']['percent'])
        self.assertEqual(report['summary']['response_percent'], 0)

    def test_statistics_html_csv_auth_and_safe_negative_reviews(self):
        survey = self.add_survey(self.now-10, 'bad')
        self.db.execute('UPDATE requests SET text=?,reply=? WHERE id=?', ('<script>bad()</script>', 'Исторический ответ', survey['request_id']))
        client = authenticated_client(self.cfg, self.db); self.addCleanup(client.close)
        page = client.get('/statistics?period=today')
        self.assertEqual(page.status_code, 200)
        self.assertIn('Оценки помощи бота', page.text)
        self.assertIn('Исторический ответ', page.text)
        self.assertIn('&lt;script&gt;', page.text)
        self.assertNotIn('<script>bad()', page.text)
        self.assertIn('/dialogue/11', page.text)
        csv = client.get('/feedback.csv?period=today')
        self.assertEqual(csv.status_code, 200)
        self.assertIn('2026-11-08,1,0,1,1,0.0,100.0', csv.text)
        self.assertEqual(client.get('/feedback.csv?period=invalid').status_code, 400)
        anonymous = TestClient(create_app(self.cfg, self.db), follow_redirects=False)
        self.addCleanup(anonymous.close)
        self.assertEqual(anonymous.get('/feedback.csv').status_code, 303)

    def test_retention_keeps_metrics_without_question_text(self):
        self.add_survey(self.now, 'bad')
        self.now += 31*86400
        self.db.purge()
        report = feedback_report(self.db, 0, self.now)
        self.assertEqual(report['summary']['bad'], 1)
        self.assertEqual(report['negative'][0]['text'], '')
        self.assertIsNone(report['negative'][0]['reply'])

    def test_setting_form_can_disable_and_enable_surveys(self):
        self.message()
        client = authenticated_client(self.cfg, self.db); self.addCleanup(client.close)
        self.assertIn('name="feedback_enabled" checked', client.get('/settings').text)
        self.assertEqual(client.post('/settings', data={'csrf': 'test', 'consent_ready': 'on'}).status_code, 303)
        self.assertEqual(self.db.setting('feedback_enabled'), 'false')
        self.assertEqual(self.survey()['status'], 'cancelled')
        client.post('/settings', data={'csrf': 'test', 'consent_ready': 'on', 'feedback_enabled': 'on'})
        self.assertEqual(self.db.setting('feedback_enabled'), 'true')


if __name__ == '__main__':
    unittest.main()
