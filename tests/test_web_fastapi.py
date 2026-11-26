import asyncio
import base64
import html
import io
import json
import re
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx2 as httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from openpyxl import load_workbook

from pulkovo.config import Config
from pulkovo.consent import consent_message
from pulkovo.db import Database
from pulkovo.importer import template_workbook
from pulkovo.web import create_app


def csrf(page):
    return html.unescape(re.search(r'name="csrf" value="([^"]+)"',page)[1])


class FastAPIWebTests(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        self.cfg=Config(Path(folder.name)/'test.sqlite3',admin_password='test-password',secret_key='test-secret')
        self.db=Database(self.cfg.database)
        self.app=create_app(self.cfg,self.db)
        self.client=TestClient(self.app,follow_redirects=False)
        self.addCleanup(self.client.close)

    def login(self):
        token=csrf(self.client.get('/login').text)
        result=self.client.post('/login',data={'csrf':token,'password':self.cfg.admin_password})
        self.assertEqual(result.status_code,303)
        return result

    def test_native_asgi_login_logout_csrf_rotation_and_cookie_flags(self):
        self.assertIsInstance(self.app,FastAPI)
        before=csrf(self.client.get('/login').text)
        result=self.login()
        self.assertEqual(result.headers['location'],'/')
        cookie=result.headers['set-cookie'].lower()
        for value in ('pulkovo_admin=','httponly','samesite=strict','max-age=28800'):
            self.assertIn(value,cookie)
        page=self.client.get('/cards').text
        after=csrf(page)
        self.assertNotEqual(before,after)
        self.assertEqual(self.client.post('/logout',data={'csrf':before}).status_code,400)
        self.assertEqual(self.client.post('/logout',data={'csrf':after}).status_code,303)
        self.assertEqual(self.client.get('/cards').headers['location'],'/login')

    def test_invalid_password_is_flashed_and_rate_limited(self):
        token=csrf(self.client.get('/login').text)
        for _ in range(8):
            result=self.client.post('/login',data={'csrf':token,'password':'wrong'})
            self.assertEqual(result.status_code,200)
            self.assertIn('Неверный пароль',result.text)
        self.assertEqual(self.client.post('/login',data={'csrf':token,'password':self.cfg.admin_password}).status_code,429)
        self.assertEqual(self.client.get('/cards').status_code,303)

    def test_tampered_and_expired_sessions_cannot_access_admin(self):
        self.client.cookies.set('pulkovo_admin','not-a-valid-session')
        self.assertEqual(self.client.get('/statistics.csv').status_code,303)
        session=base64.b64encode(json.dumps({'authenticated':True,'csrf':'old'}).encode())
        with patch('itsdangerous.timed.time.time',return_value=1):
            expired=TimestampSigner(self.cfg.secret_key).sign(session).decode()
        self.client.cookies.clear();self.client.cookies.set('pulkovo_admin',expired)
        self.assertEqual(self.client.get('/cards').status_code,303)

    def test_protected_html_json_exports_and_posts_require_authentication(self):
        for path in ('/','/cards','/resources','/queue/state','/dialogue/11/updates','/statistics.csv','/automation.csv','/tickets.csv','/import/template.xlsx','/settings'):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code,303)
        for path in ('/logout','/settings','/settings/consent-preview','/import/commit','/cards/new','/resources','/dialogue/11'):
            self.assertEqual(self.client.post(path,data={}).status_code,400)
        token=csrf(self.client.get('/login').text)
        self.assertEqual(self.client.post('/settings/consent-preview',data={'csrf':token}).status_code,303)
        self.assertEqual(self.client.get('/docs').status_code,404)
        self.assertEqual(self.client.get('/openapi.json').status_code,404)

    def test_templates_assets_and_security_headers_remain_available(self):
        self.login()
        for path in ('/','/cards/new','/resources','/queue?state=all','/statistics','/settings','/import','/preview','/static/app.css','/static/dialogue.js','/static/settings.js'):
            with self.subTest(path=path):
                result=self.client.get(path)
                self.assertEqual(result.status_code,200)
                self.assertEqual(result.headers['x-content-type-options'],'nosniff')
                self.assertEqual(result.headers['x-frame-options'],'DENY')
                self.assertEqual(result.headers['cache-control'],'no-store')
                self.assertIn("form-action 'self'",result.headers['content-security-policy'])
        self.assertIn('class="active" href="/queue"',self.client.get('/queue').text)
        self.assertIn('text/css',self.client.get('/static/app.css').headers['content-type'])

    def test_consent_preview_shows_saved_text_and_unsaved_draft_without_saving(self):
        self.login()
        page=self.client.get('/settings').text
        before=self.db.settings()
        initial=re.search(r'<p[^>]*data-consent-preview>(.*?)</p>',page,re.S)[1]
        self.assertEqual(html.unescape(initial),consent_message(before))
        self.assertNotIn(before['session_intro'],html.unescape(initial))
        intro_preview=re.search(r'<p[^>]*data-session-intro-preview>(.*?)</p>',page,re.S)[1]
        self.assertEqual(html.unescape(intro_preview),before['session_intro'])
        draft=dict(session_intro='Здравствуйте! ✈\nНовое приветствие.',
            consent_text='Условия обработки <данных>.',consent_url='https://airport.example/terms')
        response=self.client.post('/settings/consent-preview',data={'csrf':csrf(page),**draft})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json(),dict(text=consent_message(before|draft),session_intro=draft['session_intro'],warning=''))
        self.assertNotIn(draft['session_intro'],response.json()['text'])
        self.assertEqual(self.db.settings(),before)
        self.assertFalse(self.db.all("SELECT * FROM audit WHERE action='settings_changed'"))

    def test_consent_preview_warns_about_invalid_drafts(self):
        self.login()
        token=csrf(self.client.get('/settings').text)
        before=self.db.settings()
        for draft in ({'consent_text':'x'*3600},{'consent_url':'http://airport.example/terms'}):
            response=self.client.post('/settings/consent-preview',data={'csrf':token,**draft})
            self.assertEqual(response.status_code,200)
            self.assertTrue(response.json()['warning'])
        response=self.client.post('/settings/consent-preview',data={'csrf':token,'consent_text':'x'*10001})
        self.assertEqual(response.status_code,400)
        self.assertEqual(self.db.settings(),before)

    def test_multipart_excel_preview_commit_and_repeat_import(self):
        self.login()
        workbook=load_workbook(io.BytesIO(template_workbook()))
        sheet=workbook.active
        sheet.delete_rows(2,sheet.max_row)
        sheet.append(['Парковка','Где оплатить?','У терминала'])
        output=io.BytesIO();workbook.save(output);workbook.close()
        token=csrf(self.client.get('/import').text)
        data=output.getvalue()
        for attempt in range(2):
            result=self.client.post('/import',data={'csrf':token},files={'file':('knowledge.xlsx',data,'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')})
            self.assertEqual(result.status_code,200)
            self.assertIn('Где оплатить?',result.text)
            self.assertEqual(len(self.db.cards()),attempt)
            result=self.client.post('/import/commit',data={'csrf':token})
            self.assertEqual(result.status_code,303)
            self.assertEqual(result.headers['location'],'/cards')
            self.assertEqual(len(self.db.cards()),1)
            self.assertEqual(list((self.cfg.database.parent/'imports').glob('*.xlsx')),[])
        self.assertEqual(self.db.cards()[0]['status'],'draft')
        page=self.client.get('/cards').text
        self.assertIn('Все новые карточки',page)
        self.assertNotIn('Все новые карточки',self.client.get('/cards').text)

    def test_upload_validation_and_size_limit(self):
        self.login()
        token=csrf(self.client.get('/import').text)
        for filename,value in [('test.txt',b'wrong'),('test.xlsx',b'not an excel')]:
            result=self.client.post('/import',data={'csrf':token},files={'file':(filename,value)})
            self.assertEqual(result.status_code,200)
            self.assertIn('notice error',result.text)
        result=self.client.post('/import',data={'csrf':token},files={'file':('large.xlsx',b'x'*(11*1024*1024))})
        self.assertEqual(result.status_code,413)
        self.assertEqual(result.headers['x-content-type-options'],'nosniff')
        self.assertEqual(self.db.cards(),[])

    def test_chunked_body_limit_without_content_length(self):
        chunks=(b'x'*1024*1024 for _ in range(12))
        result=self.client.post('/login',content=chunks,headers={'content-type':'application/x-www-form-urlencoded'})
        self.assertEqual(result.status_code,413)

    def test_nontext_form_fields_and_unicode_csrf_fail_cleanly(self):
        self.login()
        token=csrf(self.client.get('/settings').text)
        result=self.client.post('/settings',data={'csrf':token},files={'handoff_text':('x.txt',b'not a form value')})
        self.assertEqual(result.status_code,400)
        self.assertEqual(self.client.post('/settings',data={'csrf':'не токен'}).status_code,400)
        self.assertEqual(self.client.post('/import',data={'csrf':token,'file':'not a file'}).status_code,200)

    def test_export_content_types_and_query_defaults(self):
        self.login()
        for path in ('/statistics.csv','/tickets.csv'):
            result=self.client.get(path)
            self.assertEqual(result.status_code,200)
            self.assertIn('text/csv',result.headers['content-type'])
            self.assertTrue(result.content.startswith(b'\xef\xbb\xbf'))
            self.assertIn('attachment;',result.headers['content-disposition'])
        result=self.client.get('/import/template.xlsx')
        workbook=load_workbook(io.BytesIO(result.content));workbook.close()
        for days in ('bad','-1','999999'):
            self.assertEqual(self.client.get('/statistics',params={'days':days}).status_code,200)

    def test_existing_database_is_reused_without_schema_changes(self):
        self.db.save_card(dict(topic='Тема',title='Вопрос',answer='Ответ'))
        before=self.db.all('SELECT * FROM schema_migrations')
        app=create_app(self.cfg)
        self.assertEqual(app.state.database.cards()[0]['answer'],'Ответ')
        self.assertEqual(app.state.database.all('SELECT * FROM schema_migrations'),before)


class FastAPIConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_ai_preview_does_not_block_queue_polling(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg=Config(Path(folder)/'test.sqlite3',admin_password='test',secret_key='test')
            db=Database(cfg.database)
            card_id=db.save_card(dict(topic='Тема',title='Вопрос',answer='Ответ',status='published',reviewed=1))
            app=create_app(cfg,db)
            signed=TimestampSigner(cfg.secret_key).sign(base64.b64encode(json.dumps({'authenticated':True,'csrf':'test'}).encode())).decode()
            started=threading.Event();release=threading.Event()
            def route(*args):
                started.set();release.wait(5)
                return dict(action='answer',card_id=card_id,kind='question')
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://testserver',cookies={'pulkovo_admin':signed}) as client:
                with patch('pulkovo.web.GigaChat') as ai:
                    ai.return_value.route.side_effect=route
                    preview=asyncio.create_task(client.post('/preview',data={'csrf':'test','synthetic':'on','action':'ai','text':'Вопрос'}))
                    try:
                        self.assertTrue(await asyncio.to_thread(started.wait,2))
                        result=await asyncio.wait_for(client.get('/queue/state'),2)
                        self.assertEqual(result.status_code,200)
                        self.assertIn('revision',result.json())
                        self.assertFalse(preview.done())
                    finally:
                        release.set()
                        self.assertEqual((await preview).status_code,200)
