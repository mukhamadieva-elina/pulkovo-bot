import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

import requests

from pulkovo.config import Config
from pulkovo.db import Database
from pulkovo.engine import Engine
from pulkovo.operator import OperatorInbox
from pulkovo.vk_staff import StaffReplySync
from pulkovo.web import create_app
from fastapi.testclient import TestClient
from web_client import authenticated_client


class TicketInactivityTests(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db=Database(Path(folder.name)/'test.sqlite3')
        self.config=Config(self.db.path,group_id=123,admin_password='test',secret_key='test')
        self.engine=Engine(self.db,None,self.config)
        self.db.set_settings({'consent_version':'v1','consent_ready':'true'})
        self.db.conversation(11)
        self.db.record_consent(11,11,'accepted','consent')
        self.engine.receive(11,11,'question','оператор',vk_cmid=10,vk_date=int(time.time())-90010)
        self.db.execute("UPDATE requests SET status='done'")
        self.reply_time=int(time.time())-90000
        self.reply=dict(peer_id=11,from_id=-123,id=12,conversation_message_id=12,date=self.reply_time,admin_author_id=99,text='Ответ сотрудника')
        self.engine.receive_reply(self.reply,'staff-reply')
        self.ticket=self.db.one('SELECT * FROM tickets')
        self.api=Mock()
        self.api.messages.getHistory.return_value={'items':[self.reply]}
        self.sync=StaffReplySync(self.db,self.engine,self.api)
        self.client=authenticated_client(self.config,self.db)

    def state(self):
        return self.db.one('SELECT * FROM tickets WHERE id=?',(self.ticket['id'],))

    def expire(self):
        return self.sync.sync_ticket(self.ticket,close_inactive=True)

    def test_timeout_boundary_and_no_unsolicited_reply(self):
        deadline=self.reply_time+86400
        generation=self.db.conversation(11)['generation']
        self.assertFalse(self.db.expire_waiting_ticket(self.ticket['id'],now=deadline-.01))
        self.assertTrue(self.db.expire_waiting_ticket(self.ticket['id'],now=deadline))
        self.assertFalse(self.db.expire_waiting_ticket(self.ticket['id'],now=deadline+1))
        self.assertEqual(self.state()['closure_reason'],'inactivity')
        self.assertEqual(self.state()['closed_at'],deadline)
        self.assertFalse(self.db.conversation(11)['human_mode'])
        self.assertEqual(self.db.conversation(11)['generation'],generation+1)
        self.assertEqual(self.db.all('SELECT * FROM operator_outbox'),[])
        self.assertEqual(self.db.one('SELECT count(*) n FROM requests')['n'],1)

    def test_periodic_worker_closes_once_and_new_question_has_separate_history(self):
        self.assertEqual(self.sync.sync_open_tickets(threading.Event()),1)
        self.assertEqual(self.state()['status'],'closed')
        self.assertEqual(self.sync.sync_open_tickets(threading.Event()),0)
        self.api.messages.send.assert_not_called()
        self.engine.receive(11,11,'next-question','Новый вопрос',vk_cmid=13,vk_date=int(time.time()))
        self.assertTrue(self.db.allowed(11))
        row=self.db.one("SELECT * FROM requests WHERE event_key='next-question'")
        self.db.handoff(row,'no_matching_answer')
        tickets=self.db.all('SELECT * FROM tickets ORDER BY id')
        self.assertEqual(len(tickets),2)
        inbox=OperatorInbox(self.db)
        self.assertNotIn('Новый вопрос',[m['text'] for m in inbox.timeline(11,tickets[0]['id'])])
        self.assertIn('Новый вопрос',[m['text'] for m in inbox.timeline(11,tickets[1]['id'])])

    def test_followup_before_expiry_stays_with_staff(self):
        self.engine.receive(11,11,'followup','Уточнение',vk_cmid=13,vk_date=self.reply_time+100)
        self.assertFalse(self.db.expire_waiting_ticket(self.ticket['id']))
        self.assertEqual(self.state()['status'],'open')
        self.assertTrue(self.db.conversation(11)['human_mode'])

    def test_vk_followup_not_yet_in_longpoll_cancels_timeout(self):
        self.api.messages.getHistory.return_value={'items':[self.reply,dict(peer_id=11,from_id=11,id=13,conversation_message_id=13,date=self.reply_time+100)]}
        self.expire()
        self.assertEqual(self.state()['status'],'open')
        self.assertTrue(self.db.conversation(11)['human_mode'])

    def test_followup_during_history_lookup_cannot_be_closed(self):
        def history(**kwargs):
            self.engine.receive(11,11,'race','Уточнение',vk_cmid=13,vk_date=int(time.time()))
            return {'items':[self.reply]}
        self.api.messages.getHistory.side_effect=history
        self.expire()
        self.assertEqual(self.state()['status'],'open')

    def test_new_staff_reply_restarts_deadline(self):
        new=self.reply|dict(id=14,conversation_message_id=14,date=int(time.time()))
        self.api.messages.getHistory.return_value={'items':[new,self.reply]}
        self.expire()
        self.assertEqual(self.state()['status'],'waiting_user')
        self.assertEqual(self.state()['last_staff_reply_at'],new['date'])
        self.assertFalse(self.db.expire_waiting_ticket(self.ticket['id']))

    def test_confirmed_admin_reply_also_expires_after_history_sync(self):
        inbox=OperatorInbox(self.db)
        ident=inbox.queue(11,'Ответ из панели','Анна','admin-reply-test-key',inbox.revision(11))
        row=self.db.one('SELECT * FROM operator_outbox WHERE id=?',(ident,))
        reply=self.reply|dict(id=14,conversation_message_id=14,admin_author_id=0,date=int(time.time())-86401,random_id=row['random_id'])
        inbox.confirm(row,reply)
        self.api.messages.getHistory.return_value={'items':[reply]}
        self.assertTrue(self.expire())
        self.assertEqual(self.state()['closure_reason'],'inactivity')

    def test_api_failure_or_unconfirmed_history_does_not_close(self):
        self.api.messages.getHistory.side_effect=requests.ConnectionError()
        self.assertEqual(self.sync.sync_open_tickets(threading.Event()),0)
        self.assertEqual(self.state()['status'],'waiting_user')
        self.api.messages.getHistory.side_effect=None
        self.api.messages.getHistory.return_value={'items':[]}
        self.assertFalse(self.expire())
        self.assertEqual(self.state()['status'],'waiting_user')

    def test_queued_failed_or_sending_staff_answer_prevents_closing(self):
        inbox=OperatorInbox(self.db)
        ident=inbox.queue(11,'Дополнение','Анна','request-key-long-enough',inbox.revision(11))
        for status in ('pending','sending','failed'):
            self.db.execute('UPDATE operator_outbox SET status=? WHERE id=?',(status,ident))
            self.assertFalse(self.db.expire_waiting_ticket(self.ticket['id']))
        self.db.execute("UPDATE operator_outbox SET status='cancelled' WHERE id=?",(ident,))
        self.assertTrue(self.db.expire_waiting_ticket(self.ticket['id']))

    def test_unprocessed_or_unsent_request_prevents_closing(self):
        for status in ('pending','processing','ready','sending'):
            self.db.execute('UPDATE requests SET status=?',(status,))
            self.assertFalse(self.db.expire_waiting_ticket(self.ticket['id']))

    def test_unconfirmed_newer_outgoing_defers_closing(self):
        self.api.messages.getHistory.return_value={'items':[self.reply,self.reply|dict(id=14,conversation_message_id=14,admin_author_id=0,date=int(time.time()))]}
        self.expire()
        self.assertEqual(self.state()['status'],'waiting_user')

    def test_manual_hold_and_global_timeout_setting(self):
        response=self.client.post('/dialogue/11',data={'csrf':'test','action':'auto_close','ticket_id':self.ticket['id'],'paused':'1'})
        self.assertEqual(response.status_code,303)
        self.assertFalse(self.expire())
        self.db.pause_auto_close(11,self.ticket['id'],False)
        for hours in ('0','48'):
            self.db.set_settings({'waiting_user_timeout_hours':hours})
            self.assertFalse(self.expire())
        self.db.set_settings({'waiting_user_timeout_hours':'24'})
        self.assertTrue(self.expire())
        with self.assertRaises(ValueError):
            self.db.pause_auto_close(11,self.ticket['id'],True)

    def test_closing_never_grants_consent(self):
        self.db.record_consent(11,11,'declined','decline')
        self.expire()
        self.assertFalse(self.db.allowed(11))
        self.assertEqual(self.db.conversation(11)['consent_state'],'declined')

    def test_return_statistics_keep_closure_reason_while_dialogue_stays_accessible(self):
        self.expire()
        self.engine.receive(22,22,'second-peer','оператор')
        self.db.resume(22)
        self.db.execute("INSERT INTO tickets(peer_id,request_id,reason,status,created_at,closed_at,closure_reason) VALUES (11,?,'staff_joined','closed',1,1,'inactivity')",(self.ticket['request_id'],))
        stats=self.client.get('/statistics?days=1').text
        self.assertIn('Закрыто по неактивности</span><strong>1</strong>',stats)
        self.assertIn('Завершено сотрудником</span><strong>1</strong>',stats)
        queue=self.client.get('/queue?state=all').text
        self.assertEqual(queue.count('VK ID: 11'),1)
        self.assertIn('У бота',queue)
        page=self.client.get('/dialogue/11').text
        self.assertIn('У бота',page)
        self.assertIn('id="reply-form"',page)
        csv=self.client.get('/tickets.csv?days=1').text
        self.assertEqual(csv.count('Закрыто по неактивности'),1)
        self.assertEqual(csv.count('Завершено сотрудником'),1)

    def test_settings_validate_timeout_without_changing_it_on_error(self):
        for bad in ('-1','721','bad'):
            self.client.post('/settings',data={'csrf':'test','waiting_user_timeout_hours':bad})
            self.assertEqual(self.db.setting('waiting_user_timeout_hours'),'24')
        self.client.post('/settings',data={'csrf':'test','waiting_user_timeout_hours':'48'})
        self.assertEqual(self.db.setting('waiting_user_timeout_hours'),'48')
