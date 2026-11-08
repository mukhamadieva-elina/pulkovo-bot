import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from pulkovo.ai import AIError, GigaChat
from pulkovo.config import Config
from pulkovo.db import Database
from pulkovo.dialogue_rules import is_thanks
from pulkovo.engine import Engine
from pulkovo.web import create_app


class ThanksTests(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        self.db=Database(Path(folder.name)/'test.sqlite3')
        self.cfg=Config(self.db.path,group_id=123,external_user_data=True,admin_password='test',secret_key='test')
        self.db.set_settings({'consent_version':'v1','consent_ready':'true'})
        self.db.conversation(11)
        self.db.record_consent(11,11,'accepted','consent')
        self.ai=Mock()
        self.card=self.db.save_card(dict(topic='Парковка',title='Сколько стоит парковка?',answer='Проверенный ответ.',status='published',reviewed=1))
        self.ai.route.return_value=dict(action='answer',card_id=self.card,kind='question',tone='neutral')
        self.engine=Engine(self.db,self.ai,self.cfg)
        self.transport=Mock();self.serial=0

    def process(self,text,**kwargs):
        self.serial+=1
        ident=self.engine.receive(11,11,f'message-{self.serial}',text,**kwargs)
        self.engine.process(self.engine.claim())
        return self.db.one('SELECT * FROM requests WHERE id=?',(ident,))

    def test_recognizes_only_complete_short_gratitude(self):
        for text in ('Спасибо','  СПАСИБО!!! ','Спасибо большое 🙏','Благодарю вас','Спасибо за помощь','Понятно, спасибо!','Спасибо, все понятно.','🙏','Thank you!'):
            with self.subTest(text=text):self.assertTrue(is_thanks(text))
        for text in ('','👍','Спасибо?','Спасибо, а где парковка?','Спасибо, но это не помогло','Спасибо, все равно не работает','Не спасибо','Оператор','Согласен','Вот спасибо, опять отменили рейс'):
            with self.subTest(text=text):self.assertFalse(is_thanks(text))

    def test_parking_answer_followed_by_thanks_does_not_create_handoff(self):
        row=self.process('Сколько стоит парковка?')
        self.assertEqual(row['outcome'],'answer')
        self.engine.deliver(self.transport)
        self.ai.reset_mock()
        row=self.process('Спасибо')
        self.assertEqual((row['outcome'],row['reply'],row['prompt_tokens']),('thanks','Пожалуйста!',0))
        self.assertEqual(self.ai.mock_calls,[])
        self.assertEqual(self.db.all('SELECT * FROM tickets'),[])
        self.engine.deliver(self.transport)
        self.assertEqual(self.transport.send.call_args.args[1],'Пожалуйста!')
        self.assertFalse(self.db.conversation(11)['human_mode'])

    def test_local_thanks_needs_neither_consent_nor_ai_nor_cards(self):
        self.db.record_consent(11,11,'revoked','revoke')
        self.db.execute("UPDATE cards SET status='draft'")
        self.ai.route.side_effect=AIError('must not be called')
        row=self.process('Спасибо!')
        self.assertEqual(row['text'],'')
        self.assertEqual(row['outcome'],'thanks')
        self.engine.deliver(self.transport)
        self.transport.send.assert_called_once()
        self.assertEqual(self.ai.mock_calls,[])
        self.assertFalse(self.db.allowed(11))

    def test_mixed_thanks_still_routes_question_and_complaint(self):
        row=self.process('Спасибо, а где парковка?')
        self.assertEqual(row['outcome'],'answer')
        self.assertEqual(self.ai.route.call_args.args[0],'Спасибо, а где парковка?')
        self.engine.deliver(self.transport)
        self.ai.route.return_value=dict(action='handoff',card_id=None,kind='complaint',tone='negative')
        row=self.process('Спасибо, но это не помогло')
        self.assertEqual((row['outcome'],row['reason']),('handoff','complaint'))

    def test_bot_does_not_interrupt_staff_on_thanks(self):
        row=self.process('/оператор');self.engine.deliver(self.transport)
        self.transport.reset_mock()
        row=self.process('Спасибо')
        self.assertEqual(row['outcome'],'human_mode')
        self.assertFalse(self.engine.deliver(self.transport))
        self.transport.send.assert_not_called()
        self.assertEqual(self.ai.mock_calls,[])

    def test_staff_joining_before_delivery_cancels_ready_thanks(self):
        row=self.process('Спасибо')
        self.db.staff_replied(11,'staff',99,10,int(row['created_at'])+1,text='Отвечает сотрудник')
        self.engine.deliver(self.transport)
        self.transport.send.assert_not_called()
        self.assertEqual(self.db.one('SELECT status FROM requests WHERE id=?',(row['id'],))['status'],'cancelled')

    def test_conversation_change_before_delivery_cancels_thanks(self):
        row=self.process('Спасибо')
        self.db.execute('UPDATE conversations SET generation=generation+1 WHERE peer_id=11')
        self.engine.deliver(self.transport)
        self.transport.send.assert_not_called()
        self.assertEqual(self.db.one('SELECT status FROM requests WHERE id=?',(row['id'],))['status'],'cancelled')

    def test_forged_local_intent_cannot_hide_question(self):
        row=self.process('Где парковка?',payload=json.dumps({'local_intent':'thanks'}))
        self.assertEqual(row['outcome'],'answer')
        self.ai.route.assert_called_once()

    def test_operator_control_takes_precedence_over_thanks(self):
        row=self.process('Спасибо',payload=json.dumps({'action':'operator'}))
        self.assertEqual(row['reason'],'user_requested_operator')
        self.assertEqual(row['outcome'],'handoff')

    def test_thanks_ends_clarification_context_without_counting_resolved_question(self):
        self.ai.route.return_value=dict(action='clarify',card_id=None,kind='question',tone='neutral',question='Что вас интересует?')
        self.process('Вопрос');self.engine.deliver(self.transport)
        self.assertEqual(len(self.db.history(11)),1)
        self.process('Спасибо');self.engine.deliver(self.transport)
        self.assertEqual(self.db.history(11),[])
        self.assertEqual(self.db.conversation(11)['clarification_count'],0)
        self.assertEqual(self.db.one("SELECT count(*) n FROM requests WHERE outcome='answer'")['n'],0)

    def test_ai_can_recognize_nonstandard_gratitude(self):
        self.ai.route.return_value=dict(action='acknowledge',card_id=None,kind='thanks',tone='positive',usage={'prompt_tokens':10,'completion_tokens':2})
        row=self.process('От души, вы мне очень помогли')
        self.assertEqual((row['outcome'],row['reply'],row['prompt_tokens']),('thanks','Пожалуйста!',10))
        self.assertFalse(self.db.conversation(11)['human_mode'])

    def test_ai_contract_accepts_acknowledge_only_for_thanks(self):
        ai=GigaChat(self.cfg)
        decision=dict(action='acknowledge',card_id=None,kind='thanks',tone='positive')
        for kind,valid in [('thanks',True),('question',False)]:
            decision['kind']=kind
            payload={'choices':[{'message':{'content':json.dumps(decision)}}]}
            with patch.object(ai,'call',side_effect=[[{'tokens':10}],payload]):
                if valid:self.assertEqual(ai.route('От души',[])['action'],'acknowledge')
                else:
                    with self.assertRaises(AIError):ai.route('От души',[])

    def test_admin_preview_matches_local_thanks_without_calling_ai(self):
        client=create_app(self.cfg,self.db).test_client()
        with client.session_transaction() as session:session.update(authenticated=True,csrf='test')
        with patch('pulkovo.web.GigaChat') as ai:
            page=client.post('/preview',data={'csrf':'test','action':'ai','text':'Спасибо','synthetic':'on'}).get_data(as_text=True)
        ai.assert_not_called()
        self.assertIn('<blockquote>Пожалуйста!</blockquote>',page)
        self.assertNotIn('Обращение появится в очереди сотрудника',page)
