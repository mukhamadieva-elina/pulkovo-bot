import io
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from openpyxl import Workbook
from pulkovo.ai import AIError, GigaChat, validate_decision
from pulkovo.config import Config
from pulkovo.db import Database
from pulkovo.engine import Engine
from pulkovo.importer import inspect_workbook
from pulkovo.migrations import migrate
from web_client import authenticated_client


class RoutingTests(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        self.db=Database(Path(folder.name)/'test.sqlite3')
        self.cfg=Config(self.db.path,group_id=123,external_user_data=True,admin_password='test',secret_key='test')
        self.db.set_settings({'consent_version':'v1','consent_ready':'true'})
        self.db.conversation(11);self.db.record_consent(11,11,'accepted','consent')
        self.card=self.db.save_card(dict(topic='Жалобы',title='Мало мест',answer='Зоны ожидания есть на первом и втором этажах.',status='published',reviewed=1))
        self.ai=Mock();self.transport=Mock();self.engine=Engine(self.db,self.ai,self.cfg);self.serial=0

    def decision(self, action='answer', **changes):
        return dict(action=action,card_id=self.card if action=='answer' else None,kind='complaint',tone='negative',question='Сумка была сдана в багаж или осталась в терминале?',**changes)

    def message(self,text,decision=None):
        self.serial+=1
        self.ai.route.return_value=decision or self.decision()
        ident=self.engine.receive(11,11,f'event-{self.serial}',text)
        self.engine.process(self.engine.claim())
        return self.db.one('SELECT * FROM requests WHERE id=?',(ident,))

    def test_negative_complaint_with_answer_never_creates_staff_ticket(self):
        row=self.message('Все кресла заняты, где можно сесть 😡')
        self.assertEqual((row['outcome'],row['card_id']),('answer',self.card))
        self.assertEqual(self.db.all('SELECT * FROM tickets'),[])
        self.assertFalse(self.db.conversation(11)['human_mode'])

    def test_unclear_complaint_can_clarify(self):
        row=self.message('Потеряла сумку блин',self.decision('clarify'))
        self.assertEqual(row['outcome'],'clarify')
        self.assertIn('Сумка',row['reply'])
        self.assertFalse(self.db.conversation(11)['human_mode'])

    def test_staff_required_card_still_overrides_model(self):
        card=self.db.cards()[0]
        self.db.save_card(card|{'action':'handoff'},self.card,card['version'])
        row=self.message('Нужна помощь')
        self.assertEqual((row['outcome'],row['reason']),('handoff','card_requires_staff'))

    def test_genuine_staff_actions_have_distinct_reasons(self):
        for reason,saved in [('requires_staff','staff_action_required'),('operator_request','user_requested_operator'),('unresolved','answer_not_helpful'),('no_answer','no_matching_answer'),('unsafe','unsafe_request')]:
            with self.subTest(reason=reason):
                row=self.message('Нужна помощь',self.decision('handoff',handoff_reason=reason))
                self.assertEqual((row['outcome'],row['reason']),('handoff',saved))
                self.engine.deliver(self.transport);self.db.resume(11)

    def test_complaint_no_answer_checks_full_catalog_before_handoff(self):
        second=self.db.save_card(dict(topic='Парковка',title='Парковка',answer='Оплата на сайте.',status='published',reviewed=1))
        self.db.set_settings({'search_strategy':'lexical','search_limit':'1'})
        search=Mock();search.select.return_value=[c for c in self.db.cards(True) if c['id']==second]
        self.engine.searches['lexical']=search
        self.ai.route.side_effect=[self.decision('handoff',handoff_reason='no_answer'),self.decision()]
        row=self.message('Все места заняты')
        self.assertEqual(row['outcome'],'answer')
        self.assertEqual(self.ai.route.call_count,2)
        self.assertEqual(len(self.ai.route.call_args_list[1].args[1]),2)

    def test_operator_request_does_not_retry_full_catalog(self):
        self.db.save_card(dict(topic='Еще тема',title='Другое',answer='Другой ответ',status='published',reviewed=1))
        self.db.set_settings({'search_strategy':'lexical','search_limit':'1'})
        row=self.message('Соедините с человеком',self.decision('handoff',handoff_reason='operator_request'))
        self.assertEqual(row['reason'],'user_requested_operator')
        self.ai.route.assert_called_once()

    def test_followup_receives_recent_answer_but_not_feedback_or_staff_messages(self):
        first=self.message('Где можно сесть?');self.engine.deliver(self.transport)
        self.message('А на первом этаже?',self.decision('clarify'))
        self.assertEqual(self.ai.route.call_args.args[2],[{'text':first['text'],'reply':first['reply']}])
        self.engine.deliver(self.transport)
        self.db.execute('UPDATE requests SET updated_at=?',(time.time()-1801,))
        self.assertEqual(self.db.history(11),[])

    def test_return_to_bot_does_not_reuse_previous_staff_context(self):
        self.message('Где можно сесть?');self.engine.deliver(self.transport)
        self.assertEqual(len(self.db.history(11)),1)
        self.db.resume(11)
        self.assertEqual(self.db.history(11),[])

    def test_staff_mode_is_not_silently_overridden(self):
        self.message('Нужен сотрудник',self.decision('handoff',handoff_reason='operator_request'))
        self.engine.deliver(self.transport);self.ai.reset_mock()
        row=self.message('Хочу работать в Пулково')
        self.assertEqual(row['outcome'],'human_mode')
        self.ai.route.assert_not_called()

    def test_preview_uses_same_complaint_and_card_action_rules(self):
        client=authenticated_client(self.cfg,self.db);self.addCleanup(client.close)
        with patch('pulkovo.web.GigaChat',return_value=self.ai):
            self.ai.route.return_value=self.decision()
            response=client.post('/preview',data={'csrf':'test','synthetic':'on','action':'ai','text':'Все места заняты'})
        self.assertIn('Зоны ожидания есть',response.text)
        self.assertNotIn('Обращение появится в очереди сотрудника',response.text)

    def test_model_gets_card_action_and_supports_staff_reasons(self):
        ai=GigaChat(self.cfg);card=self.db.cards()[0];card['rendered_answer']=self.db.render_answer(card)
        for reason in ('requires_staff','unresolved'):
            result={'choices':[{'message':{'content':json.dumps(self.decision('handoff',handoff_reason=reason))}}]}
            with patch.object(ai,'call',side_effect=[[{'tokens':100}],result]) as call:
                self.assertEqual(ai.route('Нужна помощь',[card])['handoff_reason'],reason)
                context=json.loads(call.call_args.kwargs['json']['messages'][1]['content'])
                self.assertEqual(context['cards'][0]['action'],'answer')

    def test_mismatched_id_title_and_quote_retry_before_any_answer(self):
        ai=GigaChat(self.cfg);card=self.db.cards()[0];card['rendered_answer']=self.db.render_answer(card)
        correct=self.decision()|{'card_title':card['title'],'answer_quote':card['rendered_answer']}
        for changes in ({'card_title':'Другая карточка'},{'answer_quote':'Такого утверждения нет в утвержденном ответе.'},{'card_id':999},{'card_title':None,'answer_quote':None}):
            invalid=correct|changes
            def result(value):return {'choices':[{'message':{'content':json.dumps(value)}}],'usage':{'prompt_tokens':100,'completion_tokens':20}}
            with patch.object(ai,'call',side_effect=[[{'tokens':100}],result(invalid),result(correct)]) as call:
                selected=ai.route('Где сесть?',[card])
            self.assertEqual(selected['card_id'],self.card)
            self.assertEqual(selected['usage'],{'prompt_tokens':200,'completion_tokens':40})
            self.assertEqual(call.call_count,3)

    def test_long_exact_quote_is_not_a_reason_to_handoff(self):
        card=self.db.cards()[0]
        card['rendered_answer']='Опубликованный ответ с инструкцией для пассажира. '*10
        decision=self.decision()|{'card_title':card['title'],'answer_quote':card['rendered_answer']}
        self.assertEqual(validate_decision(json.dumps(decision),[card])['action'],'answer')

    def test_quote_formatting_does_not_change_facts_or_outgoing_answer(self):
        card=self.db.cards()[0]
        card['rendered_answer']='Не более 3 часов. Подробнее: https://example.org/info/'
        decision=self.decision()|{'card_title':card['title'],'answer_quote':'Не более 3 часов.Подробнее: https://example.org/info'}
        self.assertEqual(validate_decision(json.dumps(decision),[card])['action'],'answer')
        for quote in ('Не более 5 часов. Подробнее: https://example.org/info/', 'Не менее 3 часов. Подробнее: https://example.org/info/'):
            with self.subTest(quote=quote),self.assertRaises(ValueError):
                validate_decision(json.dumps(decision|{'answer_quote':quote}),[card])

    def test_invalid_response_twice_is_bounded(self):
        ai=GigaChat(self.cfg)
        invalid={'choices':[{'message':{'content':'not JSON'}}]}
        with patch.object(ai,'call',side_effect=[[{'tokens':100}],invalid,invalid]) as call:
            with self.assertRaises(AIError):ai.route('Вопрос',[])
        self.assertEqual(call.call_count,3)

    def test_request_intent_label_does_not_mask_real_staff_action(self):
        decision=self.decision('handoff',handoff_reason='requires_staff')|{'kind':'request'}
        normalized=validate_decision(json.dumps(decision),[])
        self.assertEqual((normalized['kind'],normalized['action'],normalized['handoff_reason']),('question','handoff','requires_staff'))

    def test_handoff_preserves_reason_when_model_omits_analytics(self):
        decision=validate_decision(json.dumps({'action':'handoff','card_id':None,'handoff_reason':'requires_staff'}),[])
        self.assertEqual(decision['handoff_reason'],'requires_staff')
        self.assertEqual(decision['kind'],'other')
        self.assertIsNone(decision['tone'])

    def test_verified_answer_does_not_need_analytics_to_be_sent(self):
        card=self.db.cards()[0];card['rendered_answer']=self.db.render_answer(card)
        raw=json.dumps({'action':'answer','card_id':card['id'],'card_title':card['title'],'answer_quote':card['rendered_answer']})
        result=validate_decision(raw,[card])
        self.assertEqual((result['action'],result['card_id']),('answer',card['id']))
        self.assertIsNone(result['tone'])


class ComplaintImportMigrationTests(unittest.TestCase):
    def test_new_complaint_import_does_not_force_staff(self):
        for original in (False,True):
            book=Workbook();sheet=book.active
            if original:
                sheet.title='Обновл. таблица';sheet.append([None,'Вопрос','Ответ'])
                sheet.append(['Жалобы',None,None]);sheet.merge_cells('A2:C2')
                sheet.append([None,'Мало мест','Готовый ответ'])
            else:
                sheet.title='База знаний';sheet.append(['Тема','Вопрос','Ответ'])
                sheet.append(['Жалобы','Мало мест','Готовый ответ'])
            data=io.BytesIO();book.save(data)
            cards=inspect_workbook(data.getvalue(),'test.xlsx')
            self.assertEqual(len(cards),1)
            self.assertEqual((cards[0]['action'],cards[0]['status']),('answer','draft'))

    def test_upgrade_preserves_manual_actions_drafts_and_history(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);old=root/'old';old.mkdir();path=root/'test.sqlite3'
            for file in (Path(__file__).resolve().parents[1]/'pulkovo/sql').glob('*.sql'):
                if file.name<'010':shutil.copy2(file,old/file.name)
            migrate(path,old)
            db=object.__new__(Database);db.path=path
            def card(**changes):
                return db.save_card(dict(topic='Жалобы',title='Мало мест',answer='Ответ',action='handoff',status='published',reviewed=1)|changes)
            imported=card(source_key='test:1',source='test.xlsx')
            draft=card(source_key='test:2',status='draft')
            manual=card()
            internal=card(source_key='test:3',visibility='internal')
            changed=card(source_key='test:4')
            row=db.one('SELECT * FROM cards WHERE id=?',(changed,))
            db.save_card(row|{'action':'answer'},changed,1)
            db.save_card(row|{'action':'handoff'},changed,2)
            upgraded=Database(path)
            for ident,action in [(imported,'answer'),(draft,'answer'),(manual,'handoff'),(internal,'handoff'),(changed,'handoff')]:
                self.assertEqual(upgraded.one('SELECT action FROM cards WHERE id=?',(ident,))['action'],action)
            self.assertEqual(upgraded.one('SELECT status FROM cards WHERE id=?',(draft,))['status'],'draft')
            versions=upgraded.all('SELECT snapshot FROM card_versions WHERE card_id=? ORDER BY version',(imported,))
            self.assertEqual([json.loads(v['snapshot'])['action'] for v in versions],['handoff','answer'])
            self.assertEqual(json.loads(versions[-1]['snapshot']),upgraded.one('SELECT * FROM cards WHERE id=?',(imported,)))
            self.assertEqual(len(upgraded.all("SELECT * FROM audit WHERE action='allow_imported_complaint_answer'")),2)
            Database(path)
            self.assertEqual(len(upgraded.all('SELECT * FROM card_versions WHERE card_id=?',(imported,))),2)
            self.assertTrue(list((root/'backups').glob('*.sqlite3')))


if __name__=='__main__':
    unittest.main()
