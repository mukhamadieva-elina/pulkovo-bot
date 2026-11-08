import hashlib
import html
import re
from contextlib import closing
import io
import json
import sqlite3
import tempfile
import time
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from openpyxl import Workbook, load_workbook

from pulkovo.ai import AIError, GigaChat
from pulkovo.config import Config
from pulkovo.db import Database
from pulkovo.engine import Engine
from pulkovo.importer import import_workbook, inspect_workbook, template_workbook
from pulkovo.migrations import migrate
from pulkovo.web import create_app
from fastapi.testclient import TestClient
from web_client import authenticated_client
from pulkovo.vk_staff import StaffReplySync


class FakeAI:
    calls=0
    decision=None

    def route(self,text,cards,history=None,descriptions=None,guard=lambda:True):
        self.calls+=1
        if not guard():
            raise AssertionError("Consent guard failed")
        return self.decision or dict(action="answer",card_id=cards[0]["id"],question="",kind="question",tone="neutral",intent="оплата",topic=cards[0]["topic"],usage={"prompt_tokens":100,"completion_tokens":20})


class Transport:
    def __init__(self, fail=False):
        self.sent=[]
        self.fail=fail
        self.marked=[]

    def send(self,*args):
        self.sent.append(args)
        if self.fail:
            self.fail=False
            raise OSError("lost acknowledgement")

    def mark_human(self,peer):
        self.marked.append(peer)


class SystemTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)/"bot.sqlite3"
        self.db=Database(self.path)
        self.cfg=Config(self.path,group_id=123,external_user_data=True,admin_password="test-password",secret_key="only-test-secret")
        self.ai=FakeAI()
        self.engine=Engine(self.db,self.ai,self.cfg)
        self.db.set_settings({"consent_version":"v1","consent_url":"https://airport.example/consent","consent_ready":"true"})
        self.counter=0

    def tearDown(self):
        self.temp.cleanup()

    def accept(self,peer=11):
        self.db.conversation(peer,peer)
        self.db.record_consent(peer,peer,"accepted",f"accept-{peer}")

    def card(self,**kw):
        data=dict(topic="Парковка",title="Как оплатить парковку?",answer="Инструкция по оплате парковки.",status="published",reviewed=1)
        data.update(kw)
        return self.db.save_card(data)

    def message(self,text="Как оплатить парковку?",peer=11,payload=None,attachments=None):
        self.counter+=1
        event=f"e-{self.counter}"
        self.engine.receive(peer,peer,event,text,attachments,payload)
        row=self.engine.claim()
        self.assertIsNotNone(row)
        self.engine.process(row)
        return self.db.one("SELECT * FROM requests WHERE id=?",(row["id"],))

    def test_migrations_idempotent(self):
        self.card()
        Database(self.path)
        self.assertEqual(len(self.db.all("SELECT * FROM schema_migrations")),9)
        self.assertEqual(len(self.db.cards()),1)

    def test_upgrade_preserves_data_and_backs_up(self):
        folder=Path(self.temp.name)/"sql"
        folder.mkdir()
        (folder/"001_initial.sql").write_text("CREATE TABLE example(id INTEGER PRIMARY KEY, value TEXT);\n")
        path=Path(self.temp.name)/"old.sqlite3"
        migrate(path,folder)
        with closing(sqlite3.connect(path)) as con, con:
            con.execute("INSERT INTO example VALUES (1,'kept')")
        (folder/"002_extra.sql").write_text("ALTER TABLE example ADD COLUMN tag TEXT DEFAULT '';\n")
        migrate(path,folder)
        with closing(sqlite3.connect(path)) as con, con:
            self.assertEqual(con.execute("SELECT value,tag FROM example").fetchone(),("kept",""))
        self.assertTrue(list((path.parent/"backups").glob("*.sqlite3")))

    def test_failed_migration_rolls_back(self):
        folder=Path(self.temp.name)/"sql"
        folder.mkdir()
        (folder/"001_broken.sql").write_text("CREATE TABLE should_rollback(id INTEGER);\nTHIS IS NOT SQL;\n")
        path=Path(self.temp.name)/"broken.sqlite3"
        with self.assertRaises(sqlite3.Error):
            migrate(path,folder)
        with closing(sqlite3.connect(path)) as con, con:
            self.assertIsNone(con.execute("SELECT name FROM sqlite_master WHERE name='should_rollback'").fetchone())
            self.assertEqual(con.execute("SELECT count(*) FROM schema_migrations").fetchone()[0],0)

    def test_changed_migration_rejected(self):
        folder=Path(self.temp.name)/"sql"
        folder.mkdir()
        file=folder/"001_a.sql"
        file.write_text("CREATE TABLE example(id INTEGER);\n")
        path=Path(self.temp.name)/"changed.sqlite3"
        migrate(path,folder)
        file.write_text("CREATE TABLE other(id INTEGER);\n")
        with self.assertRaises(RuntimeError):
            migrate(path,folder)

    def test_drafts_internal_and_wrong_mode_excluded(self):
        good=self.card()
        self.card(status="draft")
        self.card(visibility="internal")
        self.card(mode="cover")
        self.card(status="disabled")
        self.assertEqual([c["id"] for c in self.db.cards(True)],[good])

    def test_cannot_publish_unresolved_template(self):
        for answer in ("Здравствуйте, [имя]", "Сайт: {missing}", "[Вариант из строки 45] Можно"):
            with self.assertRaises(ValueError):
                self.card(answer=answer)

    def test_edit_optimistic_lock_and_history(self):
        ident=self.card()
        card=self.db.cards()[0]
        self.db.save_card(card|{"answer":"Новая инструкция"},ident,1)
        with self.assertRaises(ValueError):
            self.db.save_card(card,ident,1)
        self.assertEqual(len(self.db.all("SELECT * FROM card_versions")),2)

    def test_excel_preserves_merged_variants_and_is_idempotent(self):
        book=Workbook()
        sheet=book.active
        sheet.title="Обновл. таблица"
        sheet.append([None,"Вопрос","Ответ"])
        sheet.append(["Багаж",None,None])
        sheet.merge_cells("A2:C2")
        sheet.append(["Лекарства","Как провезти?","Первый вариант"])
        sheet.append([None,None,"Второй вариант"])
        sheet.merge_cells("B3:B4")
        sheet.append([None,"Нет ответа",None])
        output=io.BytesIO()
        book.save(output)
        report=import_workbook(self.db,output.getvalue(),"test.xlsx")
        self.assertEqual(report["added"],2)
        cards=self.db.cards()
        self.assertTrue(all(c["status"]=="draft" for c in cards))
        self.assertIn("Второй вариант",cards[0]["answer"])
        again=import_workbook(self.db,output.getvalue(),"test.xlsx")
        self.assertTrue(again["already_imported"])
        self.assertEqual(len(self.db.cards()),2)

    def test_no_data_or_ai_before_consent(self):
        row=self.message("Секретное сообщение",attachments=[{"type":"photo","url":"https://example.com/private"}])
        self.assertEqual(row["text"],"")
        self.assertEqual(row["attachments"],"[]")
        self.assertEqual(row["outcome"],"consent_required")
        self.assertEqual(self.ai.calls,0)

    def test_stale_consent_button_does_not_grant_access(self):
        row=self.message("Согласен",payload={"action":"agree","version":"old"})
        self.assertEqual(row["outcome"],"consent_required")
        self.assertFalse(self.db.allowed(11))

    def test_acceptance_is_recorded_with_version(self):
        row=self.message("Согласен",payload={"action":"agree","version":"v1"})
        self.assertEqual(row["outcome"],"consent_accepted")
        self.assertTrue(self.db.allowed(11))
        self.assertEqual(self.db.all("SELECT * FROM consent_events")[0]["version"],"v1")

    def test_inline_consent_keeps_ai_off_until_button(self):
        self.db.set_settings({'consent_url':''})
        self.card()
        first=self.message('Как оплатить парковку?')
        self.assertEqual(first['outcome'],'consent_required')
        self.assertEqual(first['reply'],self.db.setting('consent_text'))
        self.assertEqual(self.ai.calls,0)
        self.engine.deliver(Transport())
        accepted=self.message('Согласен',payload={'action':'agree','version':'v1'})
        self.assertEqual(accepted['outcome'],'consent_accepted')
        self.assertEqual(self.ai.calls,0)
        event=self.db.one('SELECT * FROM consent_events')
        self.assertEqual(event['document_url'],'')
        self.assertEqual(event['text_snapshot'],self.db.setting('consent_text'))
        self.engine.deliver(Transport())
        answer=self.message('Как оплатить парковку?')
        self.assertEqual(answer['outcome'],'answer')
        self.assertEqual(self.ai.calls,1)

    def test_empty_inline_consent_does_not_grant_access(self):
        self.db.set_settings({'consent_url':'','consent_text':' '})
        self.message('Согласен',payload={'action':'agree','version':'v1'})
        self.assertFalse(self.db.allowed(11))
        self.assertEqual(self.ai.calls,0)

    def test_settings_accept_inline_consent_and_reject_invalid_url(self):
        client=authenticated_client(self.cfg,self.db)
        form=self.db.settings() | {'csrf':'test','consent_url':'','consent_version':'v2','consent_ready':'on'}
        client.post('/settings',data=form)
        self.assertEqual(self.db.setting('consent_url'),'')
        version=self.db.setting('consent_version')
        self.assertNotEqual(version,'v1')
        self.assertEqual(self.db.setting('consent_ready'),'true')
        form.update(consent_url='http://airport.example/consent',consent_version='v3')
        client.post('/settings',data=form)
        self.assertEqual(self.db.setting('consent_version'),version)

    def test_external_processing_gate(self):
        self.accept()
        cfg=Config(self.path,external_user_data=False)
        self.engine=Engine(self.db,self.ai,cfg)
        row=self.message()
        self.assertEqual(row["reason"],"external_processing_not_enabled")
        self.assertEqual(self.ai.calls,0)

    def test_image_not_downloaded_before_consent(self):
        with patch('pulkovo.engine.download_image') as download:
            self.message('',attachments=[{'type':'photo','url':'https://sun.userapi.com/example.jpg'}])
            download.assert_not_called()
        self.assertEqual(self.ai.calls,0)
        self.assertEqual(self.db.one('SELECT attachments FROM requests')['attachments'],'[]')

    def test_image_description_used_for_topic_routing(self):
        self.accept()
        self.card()
        self.ai.describe_image=lambda image,caption,guard: ('Знак парковки', {'prompt_tokens':5,'completion_tokens':2})
        with patch('pulkovo.engine.download_image',return_value=b'fake-jpeg'), patch.object(self.ai,'route',wraps=self.ai.route) as route:
            row=self.message('Как оплатить парковку у этого знака?',attachments=[{'type':'photo','url':'https://sun.userapi.com/example.jpg'}])
            self.assertEqual(route.call_args.args[3],['Знак парковки'])
        self.assertEqual(row['outcome'],'answer')
        self.assertEqual(row['prompt_tokens'],105)

    def test_answer_is_saved_template_and_counted_after_delivery(self):
        self.accept()
        self.card()
        row=self.message()
        self.assertEqual(row["outcome"],"answer")
        self.assertEqual(row["status"],"ready")
        self.assertEqual(row["reply"],"Инструкция по оплате парковки.")
        self.engine.deliver(Transport())
        self.assertEqual(self.db.one("SELECT status FROM requests WHERE id=?",(row["id"],))["status"],"sent")

    def test_repeated_vk_event_does_not_duplicate(self):
        self.engine.receive(11,11,"same","hello")
        self.engine.receive(11,11,"same","hello")
        self.assertEqual(len(self.db.all("SELECT * FROM requests")),1)

    def test_one_peer_is_serial_other_peer_can_run(self):
        self.engine.receive(11,11,"a","one")
        self.engine.receive(11,11,"b","two")
        self.engine.receive(12,12,"c","three")
        self.assertEqual(self.engine.claim()["peer_id"],11)
        self.assertEqual(self.engine.claim()["peer_id"],12)
        self.assertIsNone(self.engine.claim())

    def test_complaint_goes_to_queue_even_with_answer_id(self):
        self.accept()
        ident=self.card()
        self.ai.decision=dict(action="answer",card_id=ident,kind="complaint",tone="neutral",intent="",topic="Парковка")
        row=self.message("Списали деньги дважды")
        self.assertEqual(row["outcome"],"handoff")
        self.assertEqual(len(self.db.all("SELECT * FROM tickets")),1)
        self.assertTrue(self.db.conversation(11)["human_mode"])
        transport=Transport()
        self.engine.deliver(transport)
        self.assertEqual(transport.marked,[11])
        followup=self.message("Дополнительные подробности")
        self.assertEqual(followup["outcome"],"human_mode")
        self.assertEqual(followup["text"],"Дополнительные подробности")
        self.assertEqual(self.ai.calls,1)

    def test_operator_request_cancels_prepared_ai_answer(self):
        self.accept()
        self.card()
        row=self.message()
        self.engine.receive(11,11,"operator","оператор")
        transport=Transport()
        self.engine.deliver(transport)
        self.assertEqual(transport.sent,[])
        self.assertEqual(self.db.one("SELECT status FROM requests WHERE id=?",(row["id"],))["status"],"cancelled")

    def test_revoke_clears_content_and_cancels_prepared_answer(self):
        self.accept()
        self.card()
        self.message()
        self.engine.receive(11,11,"revoke","/отозвать")
        self.assertFalse(self.db.allowed(11))
        self.assertTrue(all(not r["text"] for r in self.db.all("SELECT text FROM requests")))
        self.assertFalse(self.engine.deliver(Transport()))

    def test_revoke_during_model_call(self):
        self.accept()
        self.card()
        original=self.ai.route
        def revoke(*args,**kwargs):
            decision=original(*args,**kwargs)
            self.db.record_consent(11,11,"revoked","revoke-midway")
            return decision
        self.ai.route=revoke
        row=self.message()
        self.assertEqual(row["outcome"],"cancelled")
        self.assertIsNone(row["reply"])

    def test_clarification_limit(self):
        self.accept()
        self.card()
        self.db.execute("UPDATE conversations SET clarification_count=2 WHERE peer_id=11")
        self.ai.decision=dict(action="clarify",card_id=None,kind="question",tone="neutral",question="Где вы находитесь?",intent="")
        self.assertEqual(self.message()["reason"],"clarification_limit")

    def test_disable_card_before_delivery(self):
        self.accept()
        ident=self.card()
        self.message()
        self.db.execute("UPDATE cards SET status='disabled' WHERE id=?",(ident,))
        transport=Transport()
        self.engine.deliver(transport)
        self.assertFalse(transport.sent)

    def test_retry_has_same_random_id(self):
        self.accept()
        self.card()
        row=self.message()
        transport=Transport(fail=True)
        self.engine.deliver(transport)
        self.db.execute("UPDATE requests SET updated_at=0 WHERE id=?",(row["id"],))
        self.engine.deliver(transport)
        self.assertEqual(transport.sent[0][-1],transport.sent[1][-1])
        self.assertEqual(self.db.one("SELECT status FROM requests WHERE id=?",(row["id"],))["status"],"sent")

    def test_delivery_failure_creates_staff_ticket(self):
        self.accept()
        self.card()
        row=self.message()
        transport=Transport()
        for _ in range(3):
            transport.fail=True
            self.db.execute("UPDATE requests SET updated_at=0 WHERE id=?",(row["id"],))
            self.engine.deliver(transport)
        self.assertEqual(self.db.one("SELECT status FROM requests WHERE id=?",(row["id"],))["status"],"failed")
        self.assertEqual(self.db.one("SELECT reason FROM tickets")["reason"],"delivery_failed")
        self.assertTrue(self.db.conversation(11)["human_mode"])

    def test_resource_change_cancels_prepared_reply(self):
        self.accept()
        self.db.execute("INSERT INTO resources(key,value,visibility) VALUES ('parking_url','https://airport.example/old','public')")
        self.card(answer='Оплата: {parking_url}')
        self.message()
        self.db.execute("UPDATE resources SET value='https://airport.example/new' WHERE key='parking_url'")
        transport=Transport()
        self.engine.deliver(transport)
        self.assertFalse(transport.sent)
        self.assertEqual(self.db.one("SELECT reason FROM tickets")["reason"],"state_changed_before_send")

    def test_resource_edit_updates_linked_answers_without_republishing(self):
        self.accept()
        self.db.execute("INSERT INTO resources(key,value,visibility,label) VALUES ('parking_url','https://airport.example/old','public','Оплата парковки')")
        first=self.card(answer='Оплата: {parking_url}')
        second=self.card(title='Где оплатить?',answer='Перейдите по ссылке {parking_url}')
        old=self.message()
        self.engine.deliver(Transport())
        client,_,page,_=self.editor(first)
        self.assertIn('class="answer-field"',page)
        self.assertIn('data-resource-key="parking_url"',page)
        resources=client.get('/resources').text
        self.assertIn('Связанные карточки: 2',resources)
        self.assertIn(f'/cards/{second}',resources)
        result=client.post('/resources',data={'csrf':'test','key':'parking_url','label':'Оплата парковки','value':'https://airport.example/new'})
        self.assertEqual(result.status_code,303)
        for card in self.db.cards():
            self.assertEqual((card['version'],card['status']),(1,'published'))
            self.assertIn('https://airport.example/new',self.db.render_answer(card))
            page=client.get(f"/cards/{card['id']}").text
            self.assertIn('https://airport.example/new',page)
        new=self.message()
        transport=Transport();self.engine.deliver(transport)
        self.assertIn('https://airport.example/new',transport.sent[0][1])
        self.assertIn('https://airport.example/new',new['reply'])
        self.assertEqual(self.db.one('SELECT reply FROM requests WHERE id=?',(old['id'],))['reply'],old['reply'])
        self.assertIn('https://airport.example/old',old['reply'])

    def test_short_topic_filters_and_card_edits_preserve_search_context(self):
        original='Багаж | Ручная кладь | Досмотр / Вещи, багаж'
        short='Багаж и ручная кладь'
        first=self.card(topic=original,title='Старый раздел Excel')
        self.card(topic=short,title='Новая карточка')
        self.card(title='Другая тема')
        client,path,page,form=self.editor(first)
        self.assertNotIn(original,page)
        self.assertIn(f'name="topic" value="{short}"',page)
        for query in ({'topic':short},{'topic':original},{'q':short}):
            page=client.get('/cards',params=query).text
            self.assertIn('Старый раздел Excel',page)
            self.assertIn('Новая карточка',page)
            self.assertNotIn('Другая тема',page)
            self.assertNotIn(original,page)
        form.update(title='Старый раздел Excel',topic=short,answer='Обновлённая инструкция о багаже.',intent='publish')
        self.assertEqual(client.post(path,data=form).status_code,303)
        self.assertEqual(self.db.one('SELECT topic FROM cards WHERE id=?',(first,))['topic'],original)
        client,path,_,form=self.editor(first)
        form.update(title='Старый раздел Excel',topic='Моя тема / раздел',answer='Инструкция о багаже.',intent='publish')
        self.assertEqual(client.post(path,data=form).status_code,303)
        self.assertIn('Моя тема / раздел',client.get(path).text)
        self.assertEqual(self.db.one('SELECT topic FROM cards WHERE id=?',(first,))['topic'],'Моя тема / раздел')

    def test_short_topics_are_consistent_in_queue_statistics_and_export(self):
        original='Багаж | Ручная кладь | Досмотр / Вещи, багаж'
        short='Багаж и ручная кладь'
        self.accept();self.card(topic=original)
        first=self.message();self.engine.deliver(Transport())
        second=self.message();self.engine.deliver(Transport())
        self.db.execute('UPDATE requests SET topic=? WHERE id=?',(short,second['id']))
        self.db.handoff(second,'no_matching_answer')
        client,_,_,_=self.editor()
        for path in ('/','/queue','/dialogue/11','/statistics','/statistics.csv'):
            page=client.get(path).text
            self.assertIn(short,page,path)
            self.assertNotIn(original,page,path)
        stats=client.get('/statistics').text
        self.assertEqual(stats.count(f'<span>{short}</span>'),1)
        self.assertIn(f'<span>{short}</span><strong>2</strong>',stats)
        self.db.resume(11)
        self.assertIn(short,client.get('/queue?state=all').text)
        self.assertEqual(self.db.one('SELECT topic FROM requests WHERE id=?',(first['id'],))['topic'],original)

    def test_resources_only_expose_bot_values_without_visibility_choice(self):
        self.db.execute("INSERT INTO resources(key,value,visibility) VALUES ('parking_url','https://airport.example/parking','public')")
        self.db.execute("INSERT INTO resources(key,value,visibility) VALUES ('old_internal','private value','internal')")
        client=authenticated_client(self.cfg,self.db)
        page=client.get('/resources').text
        self.assertIn('https://airport.example/parking',page)
        self.assertNotIn('{parking_url}',page)
        self.assertNotIn('name="visibility"',page)
        self.assertNotIn('private value',page)
        client.post('/resources',data={'csrf':'test','key':'new_contact','value':'Справочная','visibility':'internal'})
        self.assertIsNone(self.db.one("SELECT visibility FROM resources WHERE key='new_contact'"))
        client.post('/resources',data={'csrf':'test','key':'new_contact','value':'Справочная'})
        self.assertEqual(self.db.one("SELECT visibility FROM resources WHERE key='new_contact'")['visibility'],'public')
        client.post('/resources',data={'csrf':'test','key':'old_internal','value':'Попытка изменить'})
        old=self.db.one("SELECT * FROM resources WHERE key='old_internal'")
        self.assertEqual((old['visibility'],old['value']),('internal','private value'))

    def test_internal_excel_links_do_not_become_bot_variables(self):
        book=Workbook(); sheet=book.active; sheet.title='Обновл. таблица'
        sheet.append(['Тема','Вопрос','Ответ'])
        sheet.append(['Рейсы','Где следить за новостями?','Новости: https://max.ru/pulkovo_led'])
        contacts=book.create_sheet('Полезные контакты')
        contacts.append(['Название','Контакт'])
        contacts.append(['Внутренняя служба','https://intranet.example/team'])
        output=io.BytesIO(); book.save(output); book.close()
        import_workbook(self.db,output.getvalue(),'contacts.xlsx')
        self.assertEqual(len(self.db.all('SELECT * FROM resources')),1)
        self.assertEqual(self.db.one('SELECT visibility FROM resources')['visibility'],'public')
        self.assertEqual(self.db.one('SELECT label FROM resources')['label'],'MAX Пулково')
        internal=self.db.one("SELECT answer FROM cards WHERE visibility='internal'")
        self.assertIn('https://intranet.example/team',internal['answer'])

    def editor(self, card_id=None):
        client=authenticated_client(self.cfg,self.db)
        path=f'/cards/{card_id}' if card_id else '/cards/new'
        page=client.get(path).text
        hidden={name:html.unescape(value) for name,value in re.findall(r'type="hidden" name="([^"]+)" value="([^"]*)"',page)}
        return client,path,page,hidden

    def test_editor_publishes_plain_text_and_binds_shared_resources(self):
        self.db.execute("INSERT INTO resources(key,value,visibility,label) VALUES ('phone','+7 812 555-55-55','public','Справочная')")
        client,path,page,form=self.editor()
        for name in ('reviewed','visibility','status'):
            self.assertNotIn(f'name="{name}"',page)
        form.update(title='Оплата парковки',topic='Парковка',answer='Сайт: https://airport.example/pay. Телефон: +7 812 555-55-55.',intent='publish')
        result=client.post(path,data=form)
        self.assertEqual(result.status_code,303)
        card=self.db.cards()[0]
        self.assertEqual((card['status'],card['reviewed']),('published',1))
        self.assertIn('{phone}',card['answer'])
        self.assertNotIn('https://',card['answer'])
        self.assertEqual(self.db.render_answer(card),form['answer'])
        page=client.get(result.headers["location"]).text
        answer=html.unescape(re.search(r'<textarea id="answer-text"[^>]*>(.*?)</textarea>',page,re.S)[1])
        self.assertEqual(answer,form['answer'])
        self.assertNotIn('{phone}',page)

    def test_editor_preserves_resource_binding_after_other_tab_updates_value(self):
        self.db.execute("INSERT INTO resources(key,value,visibility) VALUES ('parking','https://airport.example/old','public')")
        ident=self.card(answer='Оплата: {parking}')
        client,path,_,form=self.editor(ident)
        self.db.execute("UPDATE resources SET value='https://airport.example/new' WHERE key='parking'")
        form.update(title='Оплата',topic='Парковка',answer='Оплата: https://airport.example/old',intent='publish')
        self.assertEqual(client.post(path,data=form).status_code,303)
        card=self.db.cards()[0]
        self.assertEqual(card['answer'],'Оплата: {parking}')
        self.assertEqual(self.db.render_answer(card),'Оплата: https://airport.example/new')

    def test_editor_keeps_draft_and_rejects_internal_publication(self):
        ident=self.card(status='draft',visibility='internal')
        client,path,page,form=self.editor(ident)
        self.assertNotIn('value="publish"',page)
        form.update(title='Контакт',topic='Службы',answer='Для сотрудников',intent='publish',visibility='public')
        page=client.post(path,data=form).text
        self.assertIn('Служебная запись не предназначена',page)
        self.assertEqual(self.db.cards()[0]['version'],1)
        form['intent']='draft'
        self.assertEqual(client.post(path,data=form).status_code,303)
        card=self.db.cards()[0]
        self.assertEqual((card['visibility'],card['status'],card['reviewed']),('internal','draft',0))

    def test_editor_rejects_tampered_snapshot_and_conflicting_save(self):
        ident=self.card()
        client,path,_,form=self.editor(ident)
        valid=dict(form)
        form.update(title='Оплата',topic='Парковка',answer='https://airport.example/new',intent='publish',resource_snapshot='tampered')
        page=client.post(path,data=form).text
        self.assertIn('Форма устарела',page)
        self.assertEqual(self.db.cards()[0]['version'],1)
        self.db.save_card(self.db.cards()[0]|dict(answer='Новое содержание'),ident,1)
        form['resource_snapshot']=valid['resource_snapshot']
        page=client.post(path,data=form).text
        self.assertIn('Карточка уже изменена',page)
        self.assertIn('name="version" value="1"',page)
        self.assertEqual(self.db.cards()[0]['answer'],'Новое содержание')
        self.assertEqual(self.db.all('SELECT * FROM resources'),[])

    def test_visual_resource_fields_keep_distinct_links_with_identical_values(self):
        for key,name in [('arrivals','Прилёт'),('departures','Вылет')]:
            self.db.execute("INSERT INTO resources(key,value,visibility,label) VALUES (?,?,'public',?)",(key,'https://airport.example/board',name))
        ident=self.card(answer='Прилёт: {arrivals}\nВылет: {departures}')
        client,path,page,form=self.editor(ident)
        for key in ('arrivals','departures'):
            self.assertIn(f'data-resource-key="{key}"',page)
        form.update(title='Табло',topic='Рейсы',answer='Прилёт: {arrivals}\nВылет: {departures}',intent='publish')
        self.assertEqual(client.post(path,data=form).status_code,303)
        self.assertEqual(self.db.one('SELECT answer FROM cards WHERE id=?',(ident,))['answer'],form['answer'])
        client.post('/resources',data={'csrf':'test','key':'arrivals','label':'Прилёт','value':'https://airport.example/arrivals'})
        self.assertEqual(self.db.render_answer(self.db.one('SELECT * FROM cards WHERE id=?',(ident,))),'Прилёт: https://airport.example/arrivals\nВылет: https://airport.example/board')

    def test_visual_fields_escape_values_and_survive_validation_errors(self):
        self.db.execute("INSERT INTO resources(key,value,visibility,label) VALUES ('public_info',?,'public',?)",('<img src=x onerror=alert(1)>','<script>Название</script>'))
        self.db.execute("INSERT INTO resources(key,value,visibility,label) VALUES ('private_info','Скрытый контакт','internal','Скрытое имя')")
        ident=self.card(answer='Контакт: {public_info}')
        client,path,page,form=self.editor(ident)
        self.assertIn('&lt;script&gt;Название&lt;/script&gt;',page)
        self.assertNotIn('<img src=x',page)
        self.assertNotIn('data-resource-key="private_info"',page)
        self.assertNotIn('Скрытый контакт',page)
        form.update(title='Контакт',topic='Услуги',answer='Контакт: {public_info}\n[имя]',intent='publish')
        page=client.post(path,data=form).text
        self.assertIn('В ответе остались незаполненные',page)
        self.assertIn('data-resource-key="public_info"',page)
        self.assertNotIn('<img src=x',page)
        self.assertEqual(self.db.one('SELECT version FROM cards WHERE id=?',(ident,))['version'],1)

    def test_editor_draft_removes_answer_from_bot(self):
        ident=self.card()
        client,path,_,form=self.editor(ident)
        form.update(title='Вопрос',topic='Тема',answer='Черновик',intent='draft')
        self.assertEqual(client.post(path,data=form).status_code,303)
        self.assertEqual(self.db.cards(eligible=True),[])
        self.assertEqual(self.db.cards()[0]['reviewed'],0)

    def test_new_resource_uses_readable_name_and_generated_key(self):
        client,_,_,_=self.editor()
        client.post('/resources',data={'csrf':'test','label':'Справочная аэропорта','value':'+7 812 555-55-55'})
        row=self.db.one('SELECT * FROM resources')
        self.assertEqual(row['label'],'Справочная аэропорта')
        self.assertTrue(row['key'].startswith('contact_'))
        self.assertIn('Справочная аэропорта',client.get('/cards/new').text)

    def test_plain_url_binding_does_not_replace_longer_urls_or_phone_substrings(self):
        self.db.execute("INSERT INTO resources(key,value,visibility) VALUES ('url','https://airport.example/pay','public')")
        self.db.execute("INSERT INTO resources(key,value,visibility) VALUES ('phone','555','public')")
        text='https://airport.example/payment, 1555, 555.'
        ident=self.db.save_card(dict(title='Вопрос',topic='Тема',answer=text,status='published',reviewed=1),editor_resources={})
        card=self.db.one('SELECT * FROM cards WHERE id=?',(ident,))
        self.assertNotIn('{url}',card['answer'])
        self.assertIn('1555, {phone}.',card['answer'])
        self.assertEqual(self.db.render_answer(card),text)

    def test_resource_labels_upgrade_preserves_keys_and_answers(self):
        directory=Path(self.temp.name)/'old-migrations'
        directory.mkdir()
        for path in (Path(__file__).parent.parent/'pulkovo/sql').glob('00[123]_*.sql'):
            (directory/path.name).write_bytes(path.read_bytes())
        old=Path(self.temp.name)/'legacy.sqlite3'
        migrate(old,directory)
        with closing(sqlite3.connect(old)) as con,con:
            con.execute("INSERT INTO resources VALUES ('parking','https://airport.example/parking','public')")
            con.execute("INSERT INTO cards(topic,title,answer,updated_at) VALUES ('Парковка','Оплата парковки','Сайт {parking}',0)")
        db=Database(old)
        row=db.one('SELECT * FROM resources')
        self.assertEqual((row['key'],row['label']),('parking','Оплата парковки'))
        self.assertEqual(db.cards()[0]['answer'],'Сайт {parking}')

    def test_navigation_only_announces_active_flight_restrictions(self):
        client,_,page,_=self.editor()
        for phrase in ('Обычный режим','РАБОЧЕЕ МЕСТО','Локальное рабочее место','Ответы при ограничениях полётов'):
            self.assertNotIn(phrase,page)
        self.db.set_settings({'cover_mode':'true'})
        self.assertIn('Ответы при ограничениях полётов',client.get('/cards').text)

    def test_bot_resumes_only_after_staff_action(self):
        self.accept()
        self.message("оператор")
        self.db.resume(11)
        self.assertTrue(self.db.allowed(11))
        self.assertEqual(self.db.all("SELECT * FROM tickets")[0]["status"],"closed")

    def test_group_chat_not_processed(self):
        self.assertIsNone(self.engine.receive(2_000_000_001,11,"group","hi"))
        self.assertEqual(self.db.all("SELECT * FROM requests"),[])

    def test_admin_requires_login_and_csrf(self):
        app=create_app(self.cfg,self.db)
        client=TestClient(app,follow_redirects=False)
        self.assertEqual(client.get("/cards").status_code,303)
        self.assertEqual(client.post("/login",data={"password":"test-password"}).status_code,400)
        token=re.search(r'name="csrf" value="([^"]+)"',client.get("/login").text)[1]
        self.assertEqual(client.post("/login",data={"csrf":token,"password":"test-password"}).status_code,303)
        self.assertEqual(client.get("/cards").status_code,200)

    def test_queue_ui_and_staff_action(self):
        self.accept()
        self.message("оператор")
        app=create_app(self.cfg,self.db)
        client=authenticated_client(self.cfg,self.db)
        page=client.get("/queue")
        self.assertIn(b"gim123?sel=11",page.content)
        client.post("/dialogue/11",data={"csrf":"test","action":"take","staff_name":"Тест"})
        self.assertEqual(self.db.all("SELECT * FROM tickets")[0]["assigned_to"],"Тест")
        client.post("/dialogue/11",data={"csrf":"test","action":"resume"})
        self.assertFalse(self.db.conversation(11)["human_mode"])

    def test_render_escapes_user_text(self):
        self.card(title="<script>alert(1)</script>")
        app=create_app(self.cfg,self.db)
        client=authenticated_client(self.cfg,self.db)
        page=client.get("/cards")
        self.assertNotIn(b"<script>alert",page.content)
        self.assertIn(b"&lt;script&gt;",page.content)

    def test_ai_validates_card_id(self):
        ai=GigaChat(self.cfg)
        bad={"action":"answer","card_id":999,"question":"","kind":"question","tone":"neutral"}
        with patch.object(ai,"call",side_effect=[[{"tokens":10},{"tokens":30}],{"choices":[{"message":{"content":json.dumps(bad)}}]}]):
            with self.assertRaises(AIError):
                ai.route("q",[{"id":1,"topic":"Тема","title":"Вопрос","rendered_answer":"Ответ","conditions":""}])

    def test_context_limit_stops_generation(self):
        ai=GigaChat(self.cfg)
        with patch.object(ai,"call",return_value=[{"tokens":23000}]) as call:
            with self.assertRaises(AIError):
                ai.route("q",[])
            self.assertEqual(call.call_count,1)

    def test_keyboard_is_cleared_but_commands_work(self):
        self.accept()
        self.card()
        row=self.message()
        self.assertEqual(json.loads(row['keyboard'])['buttons'],[])
        self.engine.deliver(Transport())
        self.assertEqual(self.message('/оператор')['outcome'],'handoff')

    def staff_reply(self,cmid=20,admin=99,**kw):
        message=dict(peer_id=11,from_id=-123,conversation_message_id=cmid,date=int(time.time()),admin_author_id=admin)
        message.update(kw)
        return self.engine.receive_reply(message,'reply-'+str(cmid))

    def test_staff_reply_waits_for_user_then_reopens_without_ai(self):
        self.accept()
        self.message('оператор')
        self.engine.deliver(Transport())
        self.assertTrue(self.staff_reply())
        self.assertEqual(self.db.one('SELECT status FROM tickets')['status'],'waiting_user')
        self.assertTrue(self.db.conversation(11)['human_mode'])
        followup=self.message('А где находится стойка?')
        self.assertEqual(followup['outcome'],'human_mode')
        self.assertEqual(self.db.one('SELECT status FROM tickets')['status'],'open')
        self.assertEqual(self.ai.calls,0)
        self.assertEqual(len(self.db.all('SELECT * FROM tickets')),1)

    def test_staff_reply_cancels_ai_and_duplicate_is_ignored(self):
        self.accept()
        self.card()
        answer=self.message()
        self.assertTrue(self.staff_reply())
        generation=self.db.conversation(11)['generation']
        self.assertFalse(self.staff_reply())
        self.assertEqual(self.db.conversation(11)['generation'],generation)
        self.assertEqual(self.db.one('SELECT status FROM requests WHERE id=?',(answer['id'],))['status'],'cancelled')
        self.assertFalse(self.engine.deliver(Transport()))

    def test_own_echo_and_unknown_sender_do_not_count_as_staff(self):
        self.accept()
        self.card()
        row=self.message()
        self.assertFalse(self.staff_reply(random_id=row['random_id']))
        self.assertFalse(self.staff_reply(payload=json.dumps({'pulkovo_bot':row['random_id']})))
        self.assertFalse(self.staff_reply(admin=0))
        self.assertFalse(self.db.conversation(11)['human_mode'])
        self.assertEqual(self.db.all('SELECT * FROM staff_replies'),[])

    def test_staff_reply_cancels_pending_handoff_notice(self):
        self.accept()
        row=self.message('оператор')
        self.staff_reply()
        self.assertEqual(self.db.one('SELECT status FROM requests WHERE id=?',(row['id'],))['status'],'cancelled')
        self.assertFalse(self.engine.deliver(Transport()))

    def test_delayed_staff_event_does_not_hide_newer_question(self):
        self.accept()
        self.engine.receive(11,11,'v100','оператор',vk_cmid=100,vk_date=1000)
        self.engine.process(self.engine.claim())
        self.engine.deliver(Transport())
        self.staff_reply(cmid=101,date=1001)
        self.engine.receive(11,11,'v103','Новый вопрос',vk_cmid=103,vk_date=1003)
        self.staff_reply(cmid=102,date=1002)
        self.assertEqual(self.db.one('SELECT status FROM tickets')['status'],'open')
        self.staff_reply(cmid=104,date=1004)
        self.assertEqual(self.db.one('SELECT status FROM tickets')['status'],'waiting_user')
        self.staff_reply(cmid=99,date=999)
        self.assertEqual(self.db.one('SELECT status FROM tickets')['status'],'waiting_user')
        self.db.resume(11)
        self.assertEqual(self.db.one('SELECT status FROM tickets')['status'],'closed')
        self.assertFalse(self.db.conversation(11)['human_mode'])

    def staff_sync_fixture(self):
        self.accept()
        self.engine.receive(11,11,'in-20','оператор',vk_cmid=20,vk_date=1000)
        self.engine.process(self.engine.claim())
        ticket=self.db.one('SELECT * FROM tickets')
        api=Mock()
        sync=StaffReplySync(self.db,self.engine,api)
        reply=dict(peer_id=11,from_id=-123,id=45,conversation_message_id=26,date=1003,admin_author_id=99,out=1)
        return sync,api,ticket,reply

    def test_staff_event_without_author_is_confirmed_using_vk_message(self):
        sync,api,_,reply=self.staff_sync_fixture()
        event={k:v for k,v in reply.items() if k!='admin_author_id'}
        api.messages.getByConversationMessageId.return_value={'items':[reply]}
        self.assertTrue(sync.receive(event))
        api.messages.getByConversationMessageId.assert_called_once_with(peer_id=11,conversation_message_ids=26,group_id=123)
        self.assertEqual(self.db.one('SELECT status FROM tickets')['status'],'waiting_user')
        self.assertTrue(self.db.conversation(11)['human_mode'])
        self.assertEqual(self.ai.calls,0)

    def test_staff_lookup_falls_back_to_message_id_and_validates_identity(self):
        sync,api,_,reply=self.staff_sync_fixture()
        event={k:v for k,v in reply.items() if k not in ('admin_author_id','conversation_message_id')}
        api.messages.getById.return_value={'items':[reply|dict(peer_id=22)]}
        self.assertFalse(sync.receive(event))
        self.assertEqual(self.db.one('SELECT status FROM tickets')['status'],'open')
        api.messages.getById.return_value={'items':[reply]}
        self.assertTrue(sync.receive(event))
        api.messages.getById.assert_called_with(message_ids=45,group_id=123)

    def test_staff_sync_never_uses_own_echo_or_unconfirmed_author(self):
        sync,api,_,reply=self.staff_sync_fixture()
        own=reply|dict(random_id=self.db.one('SELECT random_id FROM requests')['random_id'])
        self.assertFalse(sync.receive(own))
        api.messages.getByConversationMessageId.assert_not_called()
        unknown=reply|dict(admin_author_id=0)
        api.messages.getByConversationMessageId.return_value={'items':[unknown]}
        self.assertFalse(sync.receive(unknown))
        self.assertEqual(self.db.one('SELECT status FROM tickets')['status'],'open')

    def test_staff_history_recovers_missed_reply_idempotently(self):
        sync,api,ticket,reply=self.staff_sync_fixture()
        api.messages.getHistory.return_value={'items':[reply|dict(text='Служебный текст'),dict(peer_id=11,from_id=11,conversation_message_id=25,date=1002)]}
        self.assertTrue(sync.sync_ticket(ticket))
        self.assertFalse(sync.sync_ticket(ticket))
        self.assertEqual(self.db.one('SELECT status FROM tickets')['status'],'waiting_user')
        self.assertEqual(len(self.db.all('SELECT * FROM staff_replies')),1)
        self.assertNotIn('Служебный текст',str(self.db.all('SELECT * FROM staff_replies')))

    def test_staff_history_does_not_hide_newer_incoming_even_before_longpoll(self):
        sync,api,ticket,reply=self.staff_sync_fixture()
        api.messages.getHistory.return_value={'items':[dict(peer_id=11,from_id=11,conversation_message_id=27,date=1003),reply]}
        self.assertFalse(sync.sync_ticket(ticket))
        self.assertEqual(self.db.one('SELECT status FROM tickets')['status'],'open')

    def test_new_incoming_during_staff_history_lookup_still_needs_reply(self):
        sync,api,ticket,reply=self.staff_sync_fixture()
        def history(**kwargs):
            self.engine.receive(11,11,'in-27','Ещё вопрос',vk_cmid=27,vk_date=1004)
            return {'items':[reply]}
        api.messages.getHistory.side_effect=history
        self.assertTrue(sync.sync_ticket(ticket))
        self.assertEqual(self.db.one('SELECT status FROM tickets')['status'],'open')

    def test_closing_ticket_during_staff_sync_does_not_reopen_it(self):
        sync,api,ticket,reply=self.staff_sync_fixture()
        def history(**kwargs):
            self.db.resume(11)
            return {'items':[reply|dict(date=int(time.time())+1)]}
        api.messages.getHistory.side_effect=history
        self.assertFalse(sync.sync_ticket(ticket))
        self.assertEqual(self.db.one('SELECT status FROM tickets')['status'],'closed')
        self.assertFalse(self.db.conversation(11)['human_mode'])

    def test_staff_sync_only_checks_open_tickets_and_can_stop(self):
        sync,api,ticket,reply=self.staff_sync_fixture()
        reply['date']=int(time.time())
        stop=threading.Event();stop.set()
        self.assertEqual(sync.sync_open_tickets(stop),0)
        api.messages.getHistory.assert_not_called()
        stop.clear()
        api.messages.getHistory.return_value={'items':[reply]}
        self.assertEqual(sync.sync_open_tickets(stop),1)
        self.assertEqual(sync.sync_open_tickets(stop),0)
        self.assertEqual(api.messages.getHistory.call_count,2)
        api.messages.getHistory.assert_called_with(peer_id=11,count=100)

    def test_queue_revision_changes_on_staff_reply_and_user_followup(self):
        sync,api,_,reply=self.staff_sync_fixture()
        client,_,_,_=self.editor()
        first=client.get('/queue/state').json()['revision']
        sync.receive(reply)
        second=client.get('/queue/state').json()['revision']
        self.assertNotEqual(first,second)
        self.assertIn('VK 11',client.get('/queue?state=waiting_user').text)
        self.assertIn('queue.js',client.get('/queue').text)
        self.engine.receive(11,11,'in-27','Ещё вопрос',vk_cmid=27,vk_date=1004)
        third=client.get('/queue/state').json()['revision']
        self.assertNotEqual(second,third)
        self.assertEqual(self.db.one('SELECT status FROM tickets')['status'],'open')
        self.assertEqual(client.get('/queue/state?state=invalid').status_code,400)

    def simple_excel(self,rows):
        book=load_workbook(io.BytesIO(template_workbook()))
        sheet=book['База знаний']
        for i,row in enumerate(rows,2):
            for j,value in enumerate(row,1):
                sheet.cell(i,j,value)
        output=io.BytesIO(); book.save(output); book.close()
        return output.getvalue()

    def test_simple_import_renamed_file_does_not_duplicate_or_overwrite(self):
        raw=self.simple_excel([['Парковка','Как оплатить?','Первый утверждённый ответ про оплату.','parking','Где оплатить?\nЧем оплатить?','Для парковки']])
        self.assertEqual(import_workbook(self.db,raw,'one.xlsx')['added'],1)
        repeated=import_workbook(self.db,raw,'one.xlsx')
        self.assertEqual(repeated['added'],0)
        self.assertEqual(repeated['skipped_existing'],1)
        changed=self.simple_excel([['Парковка','Другой вопрос','Другой текст не должен затереть предыдущий.','parking']])
        self.assertEqual(import_workbook(self.db,changed,'renamed.xlsx')['skipped_existing'],1)
        card=self.db.cards()[0]
        self.assertEqual(card['answer'],'Первый утверждённый ответ про оплату.')
        self.assertEqual(len(json.loads(card['question_variants'])),2)
        self.assertEqual(card['status'],'draft')

    def test_simple_import_rejects_formulas_duplicates_and_missing_topic(self):
        for rows in ([['Тема','Вопрос','=1+1']], [['','Вопрос','Ответ']], [['Тема','Вопрос','Ответ','code'],['Другая','Вопрос 2','Ответ','code']]):
            with self.assertRaises(ValueError):
                inspect_workbook(self.simple_excel(rows),'test.xlsx')
        self.assertEqual(self.db.cards(),[])

    def test_admin_settings_hide_technical_options_and_preserve_search(self):
        client=authenticated_client(self.cfg,self.db)
        page=client.get('/settings').text
        self.assertNotIn('name="search_strategy"',page)
        self.assertNotIn('name="search_limit"',page)
        client.post('/settings',data={'csrf':'test','consent_ready':'on','search_strategy':'bad','search_limit':'999'})
        self.assertEqual(self.db.setting('search_strategy'),'full')
        self.assertEqual(self.db.setting('search_limit'),'8')
        page=client.get('/preview').text
        self.assertNotIn('Лексический',page)
        self.assertNotIn('name="strategy"',page)

    def test_import_help_template_and_waiting_queue(self):
        self.accept(); self.message('оператор'); self.staff_reply()
        client=authenticated_client(self.cfg,self.db)
        self.assertIn('Ждём пользователя',client.get('/queue?state=waiting_user').text)
        self.assertIn('VK 11',client.get('/queue?state=waiting_user').text)
        self.assertNotIn('VK 11',client.get('/queue').text)
        self.assertEqual(client.get('/dialogue/11').status_code,200)
        self.assertIn('Скачать шаблон',client.get('/import').text)
        download=client.get('/import/template.xlsx')
        self.assertEqual(download.status_code,200)
        book=load_workbook(io.BytesIO(download.content))
        self.assertEqual([c.value for c in book.active[1]][:3],['Тема','Вопрос','Ответ'])
        book.close()


if __name__=="__main__":
    unittest.main()
