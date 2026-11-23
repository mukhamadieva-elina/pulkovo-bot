from test_feedback import FeedbackFixture
from pulkovo.db import Database
from pulkovo.engine import Engine
from pulkovo.operator import OperatorInbox
from pulkovo.sessions import IDLE_SECONDS, expire_sessions
from web_client import authenticated_client


class SessionTests(FeedbackFixture):
    def latest(self):
        return self.db.one('SELECT * FROM dialogue_sessions ORDER BY id DESC LIMIT 1')

    def test_rating_closes_and_new_question_starts_fresh_context(self):
        original=self.message()
        self.now+=3600
        self.service.deliver(self.transport,self.engine)
        self.assertEqual(self.service.vote(11,self.payload()),'accepted')
        self.assertEqual(self.latest()['closure_reason'],'rating')
        self.assertEqual(self.db.history(11),[])
        self.now+=1
        new=self.message('А где остановка?')
        self.assertNotEqual(new['session_id'],original['session_id'])
        self.assertIsNone(self.latest()['closed_at'])
        self.assertEqual(len(self.db.all('SELECT * FROM conversations')),1)

    def test_bad_rating_also_closes_without_claiming_success_or_handoff(self):
        self.send_survey()
        self.message('Плохо',payload=self.payload(rating='bad'))
        self.assertEqual(self.latest()['closure_reason'],'rating')
        self.assertFalse(self.db.conversation(11)['human_mode'])
        self.assertEqual(self.db.all('SELECT * FROM tickets'),[])

    def test_idle_boundary_at_24_hours_survey_does_not_extend_it(self):
        row=self.message()
        started=self.now
        self.now+=3600
        self.service.deliver(self.transport,self.engine)
        self.now=started+IDLE_SECONDS-1
        self.assertEqual(expire_sessions(self.db),0)
        self.now+=1
        self.assertEqual(expire_sessions(self.db),1)
        self.assertEqual(self.latest()['closed_at'],self.now)
        self.assertEqual(self.latest()['closure_reason'],'inactivity')
        self.assertEqual(expire_sessions(self.db),0)
        self.assertEqual(self.db.history(11),[])
        self.assertEqual(self.db.one('SELECT status FROM requests WHERE id=?',(row['id'],))['status'],'sent')

    def test_restart_and_incoming_after_gap_expire_before_creating_new_session(self):
        old=self.message()
        deadline=self.now+IDLE_SECONDS
        self.now=deadline+10
        self.db=Database(self.db.path)
        self.engine=Engine(self.db,self.ai,self.cfg)
        new=self.message()
        self.assertNotEqual(old['session_id'],new['session_id'])
        previous=self.db.one('SELECT * FROM dialogue_sessions WHERE id=?',(old['session_id'],))
        self.assertEqual(previous['closed_at'],deadline)

    def test_regular_activity_moves_deadline(self):
        self.message()
        self.now+=3600
        self.message('Спасибо')
        last=self.now
        self.now+=IDLE_SECONDS-1
        self.assertEqual(expire_sessions(self.db),0)
        self.now+=1
        self.assertEqual(expire_sessions(self.db),1)
        self.assertEqual(self.latest()['closed_at'],last+IDLE_SECONDS)

    def test_pending_answer_is_not_closed_by_timer(self):
        self.message(deliver=False)
        self.now+=IDLE_SECONDS+5
        self.assertEqual(expire_sessions(self.db),0)
        self.engine.deliver(self.transport)
        self.assertEqual(self.latest()['last_activity_at'],self.now)
        self.assertEqual(expire_sessions(self.db),0)

    def test_late_rating_cannot_close_new_work_same_session(self):
        survey=self.send_survey()
        self.message('Другой вопрос')
        self.assertEqual(self.service.vote(11,self.payload(survey)),'accepted')
        self.assertIsNone(self.latest()['closed_at'])

    def test_thanks_after_invitation_still_allows_rating_to_finish_consultation(self):
        survey=self.send_survey()
        self.message('Спасибо')
        self.assertEqual(self.service.vote(11,self.payload(survey)),'accepted')
        self.assertEqual(self.latest()['closure_reason'],'rating')

    def test_late_rating_cannot_close_another_session(self):
        survey=self.send_survey()
        self.now+=IDLE_SECONDS
        self.message('Новый вопрос')
        self.assertEqual(self.service.vote(11,self.payload(survey)),'accepted')
        self.assertIsNone(self.latest()['closed_at'])

    def test_staff_mode_and_paused_auto_close_are_preserved(self):
        survey=self.send_survey()
        self.message('/оператор')
        self.db.staff_replied(11,'staff',99,99,self.now,text='Ответ')
        ticket=self.db.one("SELECT * FROM tickets WHERE status='waiting_user'")
        self.db.pause_auto_close(11,ticket['id'],True)
        self.now+=IDLE_SECONDS+1
        self.assertEqual(expire_sessions(self.db),0)
        self.assertFalse(self.db.expire_waiting_ticket(ticket['id']))
        self.assertEqual(self.service.vote(11,self.payload(survey)),'accepted')
        self.assertIsNone(self.latest()['closed_at'])
        self.db.pause_auto_close(11,ticket['id'],False)
        self.assertTrue(self.db.expire_waiting_ticket(ticket['id']))
        self.assertEqual(self.latest()['closure_reason'],'inactivity')

    def test_staff_manual_completion_closes_session(self):
        self.message('/оператор')
        self.db.resume(11)
        self.assertEqual(self.latest()['closure_reason'],'manual')

    def test_boundaries_and_statistics_are_visible_in_admin(self):
        self.send_survey()
        self.message('Хорошо',payload=self.payload())
        self.now+=1
        self.message('Новый вопрос')
        inbox=OperatorInbox(self.db)
        history=inbox.timeline(11,limit=30,include_sessions=True)
        boundaries=[m for m in history if m['kind']=='boundary']
        self.assertEqual(len(boundaries),3)
        self.assertIn('После оценки',boundaries[1]['text'])
        client=authenticated_client(self.cfg,self.db)
        page=client.get('/dialogue/11').text
        self.assertIn('session-start-',page)
        self.assertIn('После оценки',page)
        self.assertIn('Завершенные сессии',client.get('/statistics?period=today').text)

    def test_cropped_history_keeps_session_heading_without_fabricating_old_sessions(self):
        self.message()
        self.now+=100
        self.message('Спасибо')
        history=OperatorInbox(self.db).timeline(11,limit=1,include_sessions=True)
        self.assertEqual(len(history),2)
        self.assertIn('продолжение',history[0]['text'])
        self.db.execute('UPDATE requests SET session_id=NULL')
        self.db.execute('DELETE FROM dialogue_sessions')
        self.assertFalse(any(m['kind']=='boundary' for m in OperatorInbox(self.db).timeline(11,include_sessions=True)))
