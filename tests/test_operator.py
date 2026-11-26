import html
import json
import re
import tempfile
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
from pulkovo.attachments import normalize_attachments, safe_image_url


class OperatorTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db=Database(Path(self.temp.name)/'test.sqlite3')
        self.cfg=Config(self.db.path,group_id=123,admin_password='test',secret_key='test-secret')
        self.db.set_settings({'consent_version':'v1','consent_ready':'true'})
        self.db.conversation(11,11);self.db.record_consent(11,11,'accepted','accepted')
        self.engine=Engine(self.db,None,self.cfg)
        self.engine.receive(11,11,'in-10','оператор',vk_cmid=10,vk_date=int(time.time())-10)
        self.inbox=OperatorInbox(self.db)
        self.api=Mock()
        self.api.messages.send.return_value=45
        self.api.messages.getById.return_value={'items':[dict(peer_id=11,from_id=-123,id=45,conversation_message_id=12,date=int(time.time()))]}
        self.client=authenticated_client(self.cfg,self.db,staff_name='Анна')

    def queue(self,**changes):
        data=dict(peer_id=11,text='Ответ сотрудника',name='Анна',client_key='first-request-key-123',expected_revision=self.inbox.revision(11))
        data.update(changes)
        return self.inbox.queue(**data)

    def form(self):
        page=self.client.get('/dialogue/11').text
        form=re.search(r'<form method="post" id="reply-form".*?</form>',page,re.S)[0]
        return {k:html.unescape(v) for k,v in re.findall(r'name="([^"]+)" value="([^"]*)"',form)}

    def test_web_reply_queues_once_and_shows_confirmed_history(self):
        form=self.form();form['reply']='Здравствуйте, ответ <script>без выполнения</script>'
        self.assertNotIn('staff_name',form)
        form['staff_name']='Имя из старой формы'
        self.assertEqual(self.client.post('/dialogue/11',data=form).status_code,303)
        self.assertEqual(self.client.post('/dialogue/11',data=form).status_code,303)
        self.assertEqual(len(self.db.all('SELECT * FROM operator_outbox')),1)
        self.assertEqual(self.db.one('SELECT status FROM tickets')['status'],'open')
        self.assertTrue(self.inbox.deliver(self.api))
        self.assertEqual(self.db.one('SELECT status FROM operator_outbox')['status'],'sent')
        self.assertEqual(self.db.one('SELECT status FROM tickets')['status'],'waiting_user')
        self.assertTrue(self.db.conversation(11)['human_mode'])
        page=self.client.get('/dialogue/11').text
        self.assertIn('Здравствуйте, ответ &lt;script&gt;',page)
        self.assertNotIn('<script>без выполнения',page)
        self.assertIn('Сотрудник',page)
        self.assertNotIn('Имя из старой формы',page)
        self.assertEqual(self.db.one('SELECT staff_name FROM staff_replies')['staff_name'],'Сотрудник')
        self.api.messages.send.assert_called_once()
        self.assertNotIn('Анна',self.api.messages.send.call_args.kwargs['message'])

    def test_web_stale_draft_survives_new_incoming_and_cannot_send_yet(self):
        form=self.form();form['reply']='Сохраните мой черновик'
        self.engine.receive(11,11,'in-11','Другой вопрос',vk_cmid=11,vk_date=int(time.time()))
        page=self.client.post('/dialogue/11',data=form).text
        self.assertIn('Переписка изменилась',page)
        self.assertIn('Сохраните мой черновик</textarea>',page)
        self.assertEqual(self.db.all('SELECT * FROM operator_outbox'),[])

    def test_late_user_message_keeps_ticket_open_after_queued_answer(self):
        self.queue()
        self.engine.receive(11,11,'in-11','Ещё вопрос',vk_cmid=11,vk_date=int(time.time()))
        self.inbox.deliver(self.api)
        self.assertEqual(self.db.one('SELECT status FROM tickets')['status'],'open')

    def test_failures_retry_with_stable_random_id_and_do_not_show_sent(self):
        ident=self.queue()
        self.api.messages.send.side_effect=requests.ConnectionError()
        for _ in range(3):
            self.db.execute('UPDATE operator_outbox SET updated_at=0')
            self.assertTrue(self.inbox.deliver(self.api))
        row=self.db.one('SELECT * FROM operator_outbox')
        self.assertEqual(row['status'],'failed')
        self.assertEqual(self.db.one('SELECT status FROM tickets')['status'],'open')
        self.assertEqual(self.db.all('SELECT * FROM staff_replies'),[])
        self.inbox.control(11,ident,'retry')
        self.api.messages.send.side_effect=None
        self.inbox.deliver(self.api)
        self.assertEqual({call.kwargs['random_id'] for call in self.api.messages.send.call_args_list},{row['random_id']})
        self.assertEqual(len(self.db.all('SELECT * FROM staff_replies')),1)

    def test_pending_reply_prevents_finishing_until_cancelled(self):
        ident=self.queue()
        with self.assertRaises(ValueError):
            self.db.resume(11)
        self.inbox.control(11,ident,'cancel')
        self.db.resume(11)
        self.assertFalse(self.inbox.deliver(self.api))
        self.assertEqual(self.db.one('SELECT status FROM tickets')['status'],'closed')

    def test_echo_can_confirm_send_even_when_http_acknowledgement_is_lost(self):
        ident=self.queue()
        row=self.db.one('SELECT * FROM operator_outbox')
        sync=StaffReplySync(self.db,self.engine,self.api)
        echo=dict(peer_id=11,from_id=-123,id=45,conversation_message_id=12,date=int(time.time()),random_id=row['random_id'],text=row['text'])
        def send(**kwargs):
            sync.receive(echo)
            raise requests.ConnectionError()
        self.api.messages.send.side_effect=send
        self.inbox.deliver(self.api)
        self.assertEqual(self.db.one('SELECT status FROM operator_outbox')['status'],'sent')
        self.assertEqual(self.db.one('SELECT status FROM tickets')['status'],'waiting_user')
        sync.receive(echo)
        self.assertEqual(len(self.db.all('SELECT * FROM staff_replies')),1)

    def test_native_vk_answer_is_visible_and_not_ai_context(self):
        sync=StaffReplySync(self.db,self.engine,self.api)
        reply=dict(peer_id=11,from_id=-123,id=45,conversation_message_id=12,date=int(time.time()),admin_author_id=99,text='Ответ из приложения VK')
        self.assertTrue(sync.receive(reply))
        self.assertIn('Ответ из приложения VK',self.client.get('/dialogue/11').text)
        self.assertNotIn('Ответ из приложения VK',str(self.db.history(11)))
        self.assertFalse(sync.receive(reply))
        self.assertEqual(len(self.db.all('SELECT * FROM staff_replies')),1)

    def test_existing_staff_metadata_gets_text_from_history_without_reopening(self):
        self.db.staff_replied(11,'old-key',99,12,int(time.time()))
        generation=self.db.conversation(11)['generation']
        sync=StaffReplySync(self.db,self.engine,self.api)
        self.api.messages.getHistory.return_value={'items':[dict(peer_id=11,from_id=-123,id=45,conversation_message_id=12,date=int(time.time()),admin_author_id=99,text='Текст подгрузился')]}
        sync.sync_ticket(self.db.one('SELECT * FROM tickets'))
        self.assertEqual(self.db.one('SELECT text FROM staff_replies')['text'],'Текст подгрузился')
        self.assertEqual(self.db.conversation(11)['generation'],generation)

    def test_failed_or_sent_answers_cannot_be_duplicated_by_other_staff(self):
        self.queue()
        with self.assertRaises(ValueError):
            self.queue(client_key='second-request-key-123')
        self.db.execute("UPDATE operator_outbox SET status='sending',updated_at=0")
        self.assertTrue(self.inbox.deliver(self.api))
        self.assertEqual(len(self.db.all('SELECT * FROM staff_replies')),1)

    def test_updates_authentication_and_revision(self):
        old=self.inbox.revision(11)
        self.assertEqual(self.client.get('/dialogue/11/updates?revision='+old).json(),{'revision':old})
        self.queue()
        data=self.client.get('/dialogue/11/updates?revision='+old).json()
        self.assertIn('Ответ сотрудника',data['html'])
        self.assertNotEqual(data['revision'],old)
        client=TestClient(create_app(self.cfg,self.db),follow_redirects=False)
        self.assertEqual(client.get('/dialogue/11/updates').status_code,303)
        self.assertEqual(client.post('/dialogue/11',data={'action':'reply','reply':'X'}).status_code,400)

    def test_retention_removes_operator_text(self):
        self.queue();self.inbox.deliver(self.api)
        self.db.execute('UPDATE staff_replies SET created_at=1')
        self.db.execute('UPDATE operator_outbox SET created_at=1')
        self.db.purge()
        self.assertEqual(self.db.one('SELECT text FROM staff_replies')['text'],'')
        self.assertEqual(self.db.one('SELECT text FROM operator_outbox')['text'],'')

    def test_human_conversation_is_visible_without_permission_for_ai(self):
        self.db.record_consent(11,11,'declined','declined')
        self.engine.receive(11,11,'human-in-11','Вопрос сотруднику',vk_cmid=11,vk_date=int(time.time()))
        row=self.db.one("SELECT * FROM requests WHERE event_key='human-in-11'")
        self.assertEqual(row['text'],'Вопрос сотруднику')
        self.assertFalse(self.db.allowed(11))
        self.engine.process(row)
        self.assertEqual(self.db.one('SELECT outcome FROM requests WHERE id=?',(row['id'],))['outcome'],'human_mode')

    def test_incoming_sticker_is_rendered_without_lost_text_warning(self):
        raw=[{'type':'sticker','sticker':{'sticker_id':123,'images':[{'width':64,'height':64,'url':'https://vk.ru/sticker-small.png'},{'width':256,'height':256,'url':'https://vk.ru/sticker.png'},{'width':512,'height':512,'url':'https://vk.ru/sticker-large.png'}]}}]
        attachments=normalize_attachments(raw)
        self.assertEqual(attachments[0]['url'],'https://vk.ru/sticker.png')
        self.engine.receive(11,11,'sticker-11','',attachments,vk_cmid=11)
        page=self.client.get('/dialogue/11').text
        self.assertIn('src="https://vk.ru/sticker.png"',page)
        self.assertIn('alt="Стикер"',page)
        self.assertNotIn('Текст не сохранён',page)
        self.assertNotIn('Вложение: доступно в VK',page)
        update=self.client.get('/dialogue/11/updates').json()
        self.assertIn('alt="Стикер"',update['html'])

    def test_consent_button_before_permission_is_visible_without_saving_text(self):
        ident=self.engine.receive(22,22,'agree-new','Согласен',payload={'action':'agree','version':'v1'})
        row=self.db.one('SELECT * FROM requests WHERE id=?',(ident,))
        self.assertEqual(row['text'],'')
        self.engine.process(row)
        self.db.execute("UPDATE requests SET status='sent' WHERE id=?",(ident,))
        messages=self.inbox.timeline(22)
        self.assertEqual(messages[0]['text'],'Выбрано «Согласен»')
        self.assertEqual(messages[0]['id'],f'request-{ident}')
        self.assertEqual(messages[1]['id'],f'bot-{ident}')
        for path in ('/dialogue/22','/dialogue/22/updates'):
            result=self.client.get(path)
            page=result.json()['html'] if path.endswith('/updates') else result.text
            self.assertIn('Выбрано «Согласен»',page)
            self.assertNotIn('Текст не сохранён',page)
        self.assertEqual(self.db.one('SELECT text FROM requests WHERE id=?',(ident,))['text'],'')

    def test_stale_consent_button_is_shown_as_choice_not_granted_consent(self):
        ident=self.engine.receive(22,22,'agree-stale','Согласен',payload={'action':'agree','version':'old'})
        self.engine.process(self.db.one('SELECT * FROM requests WHERE id=?',(ident,)))
        self.assertEqual(self.inbox.timeline(22)[0]['text'],'Выбрано «Согласен»')
        self.assertFalse(self.db.allowed(22))
        self.assertEqual(self.db.one('SELECT outcome FROM requests WHERE id=?',(ident,))['outcome'],'consent_required')

    def test_control_actions_survive_text_removal_and_bad_payloads_stay_unknown(self):
        for index,(action,expected) in enumerate([('decline','Выбрано общение с сотрудником'),('operator','Запрос помощи сотрудника'),('revoke','Запрос на отзыв согласия')],30):
            self.engine.receive(index,index,f'control-{index}','',payload={'action':action})
            self.assertEqual(self.inbox.timeline(index)[0]['text'],expected)
        for index,payload in enumerate(['not json','[]','{"action": []}','{"action": "unrecognized"}','{}'],40):
            self.db.enqueue(index,index,f'bad-{index}','private text',payload=payload)
            messages=self.inbox.timeline(index)
            self.assertEqual(messages[0]['text'],'Текст не сохранён')
            self.assertNotIn('private text',str(messages))

    def test_native_staff_sticker_and_photo_share_attachment_renderer(self):
        raw=[{'type':'sticker','sticker':{'images_with_background':[{'width':256,'height':256,'url':'https://vk.ru/sticker.png'}]}},{'type':'photo','photo':{'sizes':[{'width':128,'height':128,'url':'https://photo.userapi.com/small.jpg'},{'width':800,'height':600,'url':'https://photo.userapi.com/photo.jpg'}]}}]
        sync=StaffReplySync(self.db,self.engine,self.api)
        sync.receive(dict(peer_id=11,from_id=-123,id=45,conversation_message_id=12,date=int(time.time()),admin_author_id=99,text='',attachments=raw))
        page=self.client.get('/dialogue/11').text
        self.assertIn('alt="Стикер"',page)
        self.assertIn('src="https://photo.userapi.com/photo.jpg"',page)
        self.assertNotIn('Ответ сотрудника — содержание доступно в VK',page)

    def test_unsafe_image_urls_are_not_embedded_even_from_existing_rows(self):
        for url in ('javascript:alert(1)','https://vk.ru.evil.example/image.png','https://vk.ru@evil.example/image.png','https://user:secret@vk.ru/image.png','http://vk.ru/image.png','https://vk.ru:8080/image.png'):
            self.assertEqual(safe_image_url(url),'')
        self.engine.receive(11,11,'unsafe-11','',[{'type':'sticker','url':'https://evil.example/image.png'}],vk_cmid=11)
        page=self.client.get('/dialogue/11')
        self.assertNotIn('https://evil.example',page.text)
        self.assertIn('https://*.vk.ru',page.headers['Content-Security-Policy'])
        self.assertNotIn('img-src *',page.headers['Content-Security-Policy'])

    def test_other_attachments_show_type_and_link_to_correct_vk_dialogue(self):
        self.engine.receive(11,11,'doc-11','',[{'type':'doc'}],vk_cmid=11)
        page=self.client.get('/dialogue/11/updates').json()['html']
        self.assertIn('Документ',page)
        self.assertIn('https://vk.com/gim123?sel=11',page)
        self.assertNotIn('Текст не сохранён',page)

