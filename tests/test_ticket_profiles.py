import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from pulkovo.config import Config
from pulkovo.db import Database
from pulkovo.engine import Engine
from pulkovo.migrations import migrate
from pulkovo.profiles import ProfileSync
from pulkovo.web import create_app
from fastapi.testclient import TestClient
from web_client import authenticated_client


class TicketProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.db=Database(Path(self.temp.name)/'test.sqlite3')
        self.cfg=Config(self.db.path,group_id=123,secret_key='secret',admin_password='test')
        self.engine=Engine(self.db,None,self.cfg)
        self.db.conversation(11)
        self.db.set_settings({'consent_version':'v1'})
        self.db.record_consent(11,11,'accepted','accept')
        self.client=authenticated_client(self.cfg,self.db)

    def message(self,key,text,topic=None):
        ident=self.engine.receive(11,11,key,text)
        self.db.execute("UPDATE requests SET status='done',topic=? WHERE id=?",(topic,ident))
        return ident

    def ticket(self):
        self.message('classified','Как оплатить?','Парковка')
        self.message('operator','оператор')
        return self.db.one('SELECT * FROM tickets')

    def test_previous_topic_survives_handoff_close_and_newer_ticket(self):
        first=self.ticket()
        self.assertEqual(first['topic'],'Парковка')
        self.assertLess(first['topic_request_id'],first['request_id'])
        self.db.resume(11)
        self.message('later','Другой вопрос','Багаж')
        self.message('operator-again','оператор')
        rows=self.db.all('SELECT * FROM tickets ORDER BY id')
        self.assertEqual([r['topic'] for r in rows],['Парковка','Багаж'])
        closed=self.db.ticket_rows('closed')[0]
        self.assertEqual(closed['last_text'],'оператор')
        self.assertNotEqual(closed['last_text'],'Другой вопрос')

    def test_no_topic_is_invented_for_operator_request(self):
        self.message('operator','оператор')
        self.db.resume(11)
        self.assertEqual(self.db.ticket_rows('closed')[0]['topic'],'')
        self.assertIn('Тема не указана',self.client.get('/queue?state=all').text)

    def test_topic_assigned_during_ai_handoff_is_saved_on_ticket(self):
        ident=self.message('incoming','Вопрос без ответа')
        row=self.db.one('SELECT * FROM requests WHERE id=?',(ident,))
        self.engine.to_human(row,'no_matching_answer',decision={'topic':'Багаж'})
        ticket=self.db.one('SELECT * FROM tickets')
        self.assertEqual((ticket['topic'],ticket['topic_request_id']),('Багаж',ident))

    def test_manual_topic_is_preserved_and_available_after_close(self):
        ticket=self.ticket();self.db.resume(11)
        result=self.client.post('/dialogue/11',data={'csrf':'test','action':'topic','ticket_id':ticket['id'],'topic_version':ticket['topic_version'],'topic':'Доступная среда'})
        self.assertEqual(result.status_code,303)
        self.db.refresh_ticket_topics(11)
        ticket=self.db.one('SELECT * FROM tickets')
        self.assertEqual((ticket['topic'],ticket['topic_source']),('Доступная среда','manual'))
        self.assertIn('Доступная среда',self.client.get('/queue?state=all').text)
        with self.assertRaises(ValueError):
            self.db.set_ticket_topic(22,ticket['id'],'Нельзя',ticket['topic_version'])
        with self.assertRaises(ValueError):
            self.db.set_ticket_topic(11,ticket['id'],'Устаревшая форма',ticket['topic_version']-1)

    def test_profiles_are_cached_and_unrequested_users_are_ignored(self):
        api=Mock();api.users.get.return_value=[dict(id=11,first_name='Анна',last_name='Иванова'),dict(id=22,first_name='Другой',last_name='Пользователь')]
        sync=ProfileSync(self.db,api)
        self.assertEqual(sync.refresh(),1)
        self.assertEqual(sync.refresh(),0)
        api.users.get.assert_called_once_with(user_ids='11')
        self.assertEqual(self.db.one('SELECT first_name FROM vk_profiles')['first_name'],'Анна')
        self.assertIsNone(self.db.one('SELECT * FROM vk_profiles WHERE user_id=22'))

    def test_failed_profile_refresh_retains_names_and_throttles_retries(self):
        api=Mock();api.users.get.return_value=[dict(id=11,first_name='Анна',last_name='Иванова')]
        sync=ProfileSync(self.db,api);sync.refresh()
        with patch('pulkovo.profiles.time.time',return_value=time.time()+86401):
            api.users.get.side_effect=requests.ConnectionError()
            with self.assertRaises(requests.ConnectionError):
                sync.refresh()
            self.assertEqual(sync.refresh(),0)
        self.assertEqual(self.db.one('SELECT first_name FROM vk_profiles')['first_name'],'Анна')

    def test_queue_names_ids_links_escaping_and_revision(self):
        self.ticket()
        old=self.client.get('/queue/state').json()['revision']
        api=Mock();api.users.get.return_value=[dict(id=11,first_name='Анна',last_name='<script>Иванова</script>')]
        ProfileSync(self.db,api).refresh()
        new=self.client.get('/queue/state').json()['revision']
        self.assertNotEqual(old,new)
        for path in ('/queue','/','/dialogue/11'):
            page=self.client.get(path).text
            self.assertIn('Анна &lt;script&gt;Иванова&lt;/script&gt;',page)
            self.assertIn('VK ID: 11',page)
            self.assertIn('https://vk.com/id11',page)
            self.assertNotIn('<script>Иванова</script>',page)

    def test_migration_backfills_each_closed_ticket_with_its_own_topic(self):
        folder=Path(self.temp.name)/'old-sql';folder.mkdir()
        source=Path(__file__).parent.parent/'pulkovo/sql'
        for file in source.glob('00[1-5]_*.sql'):
            (folder/file.name).write_bytes(file.read_bytes())
        path=Path(self.temp.name)/'old.sqlite3';migrate(path,folder)
        with closing(sqlite3.connect(path)) as con,con:
            for ident,date,topic in [(1,100,'Парковка'),(2,200,None),(3,300,'Багаж'),(4,400,None)]:
                con.execute("INSERT INTO requests(id,event_key,peer_id,user_id,input_type,random_id,created_at,updated_at,topic) VALUES (?,?,11,11,'text',?,?,?,?)",(ident,str(ident),ident,date,date,topic))
            con.execute("INSERT INTO tickets(id,peer_id,request_id,reason,status,created_at,closed_at) VALUES (1,11,2,'user_requested_operator','closed',200,250)")
            con.execute("INSERT INTO tickets(id,peer_id,request_id,reason,status,created_at,closed_at) VALUES (2,11,4,'clarification_limit','closed',400,450)")
        db=Database(path)
        rows=db.all('SELECT topic,topic_request_id,status FROM tickets ORDER BY id')
        self.assertEqual([(r['topic'],r['topic_request_id'],r['status']) for r in rows],[('Парковка',1,'closed'),('Багаж',3,'closed')])
        self.assertTrue(list((path.parent/'backups').glob('*.sqlite3')))
