import json
from unittest.mock import Mock

from test_feedback import FeedbackFixture
from pulkovo.feedback import Feedback
from pulkovo.operator import OperatorInbox
from pulkovo.statistics import feedback_report


class FeedbackRecoveryTests(FeedbackFixture):
    def setUp(self):
        super().setUp()
        self.sent = self.send_survey()
        self.now += 30
        self.api = Mock()
        self.transport.send.reset_mock()
        self.ai.reset_mock()

    def click(self, rating=5, cmid=9, **changes):
        return dict(dict(peer_id=11, from_id=11, conversation_message_id=cmid,
                         id=cmid+100, date=int(self.now), text=f'{rating} ⭐',
                         payload=json.dumps(self.payload(self.sent, rating))), **changes)

    def recover(self, items, service=None):
        self.api.messages.getHistory.return_value = {'items':items}
        return (service or self.service).sync_votes(self.api, self.engine)

    def test_missed_vote_restores_history_rating_and_session_without_sending(self):
        click=self.click()
        self.assertEqual(self.recover([click]), 1)
        saved=self.survey()
        self.assertEqual(saved['rating_stars'], 5)
        self.assertEqual(saved['rated_at'], click['date'])
        request=self.db.one("SELECT * FROM requests WHERE event_key='123:11:9'")
        self.assertEqual((request['status'],request['outcome'],request['text']), ('done','feedback',''))
        self.assertIn('Оценка: 5 из 5 ⭐', [m['text'] for m in OperatorInbox(self.db).timeline(11)])
        self.assertEqual(self.db.one('SELECT closure_reason FROM dialogue_sessions')['closure_reason'],'rating')
        self.assertEqual(feedback_report(self.db,0,self.now+1)['summary']['rated'],1)
        self.assertIsNone(self.engine.receive(11,11,'123:11:9','5 ⭐',payload=click['payload']))
        self.assertEqual(Feedback(self.db).sync_votes(self.api,self.engine),0)
        self.api.messages.getHistory.assert_called_once_with(peer_id=11,count=100)
        self.transport.send.assert_not_called()
        self.ai.route.assert_not_called()

    def test_first_vote_wins_even_when_history_is_newest_first(self):
        first=self.click(2)
        second=self.click(5,cmid=10,date=int(self.now)+1)
        self.now+=2
        self.assertEqual(self.recover([second,first,first]),2)
        self.assertEqual(self.survey()['rating_stars'],2)
        self.assertEqual(self.db.one("SELECT outcome FROM requests WHERE event_key='123:11:10'")['outcome'],'feedback_duplicate')

    def test_only_actual_buttons_for_this_user_and_delivered_survey_are_recovered(self):
        cases=[self.click(from_id=22),self.click(peer_id=22),
               self.click(payload=json.dumps(dict(action='feedback',token='forged',rating=5))),
               self.click(payload=None),self.click(payload='{'),self.click(rating='good'),
               self.click(date=int(self.sent['sent_at'])-1),self.click(date=int(self.now)+120),
               self.click(cmid=0,id=0)]
        self.assertEqual(self.recover(cases),0)
        self.assertIsNone(self.survey()['rating_stars'])
        self.assertEqual(self.db.one('SELECT count(*) n FROM requests')['n'],1)

    def test_failure_is_retried_with_rate_limit(self):
        self.api.messages.getHistory.side_effect=RuntimeError('offline')
        with self.assertLogs(level='WARNING'):
            self.assertEqual(self.service.sync_votes(self.api,self.engine),0)
        self.now+=30
        self.assertEqual(self.service.sync_votes(self.api,self.engine),0)
        self.api.messages.getHistory.assert_called_once()
        self.now+=31
        self.api.messages.getHistory.side_effect=None
        self.assertEqual(self.recover([self.click()]),1)

    def test_existing_long_poll_request_is_left_for_normal_processing(self):
        click=self.click()
        self.engine.receive(11,11,'123:11:9','5 ⭐',payload=click['payload'])
        self.assertEqual(self.recover([click]),0)
        self.assertIsNone(self.survey()['rating_stars'])
        self.engine.process(self.engine.claim())
        self.assertEqual(self.survey()['rating_stars'],5)

    def test_unseen_new_question_does_not_close_session_or_get_replayed(self):
        question=self.click(cmid=10,date=int(self.now)+1,payload=None,text='Новый вопрос')
        self.now+=2
        self.assertEqual(self.recover([question,self.click()]),1)
        self.assertIsNone(self.db.one('SELECT closed_at FROM dialogue_sessions')['closed_at'])
        self.assertIsNone(self.db.one("SELECT id FROM requests WHERE event_key='123:11:10'"))
        self.ai.route.assert_not_called()

    def test_staff_mode_is_preserved_and_old_consent_does_not_block_vote(self):
        self.db.take_dialogue(11,OperatorInbox(self.db).revision(11))
        self.db.record_consent(11,11,'revoked','revoke')
        self.assertEqual(self.recover([self.click()]),1)
        self.assertTrue(self.db.conversation(11)['human_mode'])
        self.assertEqual(self.db.one('SELECT status FROM tickets')['status'],'open')
        self.assertEqual(self.db.one('SELECT consent_state FROM conversations')['consent_state'],'revoked')

    def test_lost_survey_delivery_confirmation_can_be_recovered(self):
        self.db.execute("UPDATE feedback_surveys SET status='pending',sent_at=NULL WHERE id=?",(self.sent['id'],))
        self.assertEqual(self.recover([self.click()]),1)
        self.assertEqual(self.survey()['status'],'sent')
        self.assertEqual(self.survey()['rating_stars'],5)

    def test_old_surveys_outside_retention_are_not_requested(self):
        self.db.set_settings({'retention_days':'1'})
        self.now+=86400
        self.assertEqual(self.recover([self.click()]),0)
        self.api.messages.getHistory.assert_not_called()
