import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from pulkovo.ai import AIError, GigaChat
from pulkovo.config import Config
from pulkovo.db import Database
from pulkovo.engine import Engine


class ImageClarificationTests(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db=Database(Path(folder.name)/'test.sqlite3')
        self.cfg=Config(self.db.path,group_id=123,external_user_data=True)
        self.db.set_settings({'consent_version':'v1','consent_ready':'true'})
        self.db.conversation(11)
        self.db.record_consent(11,11,'accepted','consent')
        self.card=self.db.save_card(dict(topic='Зал ожидания',title='Где можно посидеть?',answer='Опубликованный ответ о местах ожидания.',status='published',reviewed=1))
        self.description='Зал ожидания. Большинство видимых кресел заняты пассажирами, есть свободные кресла.'
        self.question='Вы хотите узнать, где можно найти свободное место для ожидания?'
        self.ai=Mock()
        self.ai.describe_image.return_value=(self.description,{'prompt_tokens':5,'completion_tokens':2})
        self.ai.clarify_photo.return_value=dict(action='clarify',card_id=None,question=self.question,kind='question',tone='neutral',intent='уточнение по фотографии',topic=None,usage={'prompt_tokens':10,'completion_tokens':4})
        self.ai.route.return_value=dict(action='answer',card_id=self.card,question='',kind='question',tone='neutral',intent='места ожидания',topic='Зал ожидания',usage={'prompt_tokens':100,'completion_tokens':20})
        self.engine=Engine(self.db,self.ai,self.cfg)
        self.transport=Mock()
        self.counter=0
        self.photo=[{'type':'photo','url':'https://sun.userapi.com/test.jpg'}]

    def process(self,text='',attachments=None):
        self.counter+=1
        ident=self.engine.receive(11,11,'event-'+str(self.counter),text,attachments)
        request=self.engine.claim()
        self.assertEqual(request['id'],ident)
        with patch('pulkovo.engine.download_image',return_value=b'image'):
            self.engine.process(request)
        return self.db.one('SELECT * FROM requests WHERE id=?',(ident,))

    def test_photo_without_caption_asks_ai_question_instead_of_guessing_answer(self):
        row=self.process(attachments=self.photo)
        self.assertEqual(row['outcome'],'clarify')
        self.assertEqual(row['reply'],self.db.setting('session_intro')+'\n\n'+self.question)
        self.assertEqual(row['prompt_tokens'],15)
        self.assertEqual(row['completion_tokens'],6)
        self.ai.route.assert_not_called()
        self.ai.clarify_photo.assert_called_once()
        self.assertEqual(self.db.all('SELECT * FROM tickets'),[])
        self.assertFalse(self.db.conversation(11)['human_mode'])
        self.assertEqual(json.loads(row['image_descriptions']),[self.description])

    def test_followup_keeps_image_description_in_search_and_model_context(self):
        self.process(attachments=self.photo)
        self.engine.deliver(self.transport)
        search=Mock();search.select.return_value=self.db.cards(True)
        self.engine.searches['full']=search
        row=self.process('Да, где можно сесть?')
        self.assertIn(self.description,search.select.call_args.args[0])
        history=self.ai.route.call_args.args[2]
        self.assertEqual(history[0]['image_descriptions'],[self.description])
        self.assertEqual(history[0]['reply'],self.question)
        self.assertEqual(row['outcome'],'answer')
        self.assertEqual(row['reply'],'Опубликованный ответ о местах ожидания.')
        self.engine.deliver(self.transport)
        self.assertEqual(self.db.history(11)[-1]['reply'],row['reply'])

    def test_photo_can_answer_existing_clarification(self):
        self.ai.route.return_value=self.ai.clarify_photo.return_value
        self.process('Где места ожидания?')
        self.engine.deliver(self.transport)
        self.ai.route.return_value=dict(action='answer',card_id=self.card,kind='question',tone='neutral')
        row=self.process(attachments=self.photo)
        self.assertEqual(row['outcome'],'answer')
        self.ai.clarify_photo.assert_not_called()

    def test_clear_caption_can_receive_published_answer(self):
        row=self.process('Где можно посидеть в ожидании рейса?',self.photo)
        self.assertEqual(row['outcome'],'answer')
        self.ai.clarify_photo.assert_not_called()

    def test_vague_caption_and_no_match_get_generated_clarification(self):
        self.ai.route.return_value=dict(action='handoff',card_id=None,kind='other',tone='neutral',handoff_reason='no_answer',usage={'prompt_tokens':100,'completion_tokens':20})
        row=self.process('Вот',self.photo)
        self.assertEqual(row['outcome'],'clarify')
        self.assertEqual(row['reply'],self.db.setting('session_intro')+'\n\n'+self.question)
        self.assertEqual(row['prompt_tokens'],115)
        self.assertEqual(row['completion_tokens'],26)
        self.assertFalse(self.db.conversation(11)['human_mode'])

    def test_explicit_complaint_and_operator_request_still_go_to_staff(self):
        for reason,kind in (('complaint','complaint'),('operator_request','question'),('unsafe','other'),('out_of_scope','other')):
            with self.subTest(reason=reason):
                self.ai.route.return_value=dict(action='handoff',card_id=None,kind=kind,tone='neutral',handoff_reason=reason)
                row=self.process('Явная причина обращения',self.photo)
                self.assertEqual(row['outcome'],'handoff')
                self.ai.clarify_photo.assert_not_called()
                self.engine.deliver(self.transport)
                self.db.resume(11)

    def test_operator_command_bypasses_photo_analysis(self):
        row=self.process('/оператор',self.photo)
        self.assertEqual(row['outcome'],'handoff')
        self.ai.describe_image.assert_not_called()
        self.ai.clarify_photo.assert_not_called()

    def test_clarification_limit_is_preserved(self):
        self.db.execute('UPDATE conversations SET clarification_count=2 WHERE peer_id=11')
        row=self.process(attachments=self.photo)
        self.assertEqual(row['reason'],'clarification_limit')
        self.assertEqual(row['outcome'],'handoff')

    def test_no_image_processing_without_consent(self):
        self.db.record_consent(11,11,'revoked','revoked')
        row=self.process(attachments=self.photo)
        self.assertEqual(row['outcome'],'consent_required')
        self.ai.describe_image.assert_not_called()
        self.ai.clarify_photo.assert_not_called()
        self.assertEqual(row['image_descriptions'],'[]')

    def test_revocation_during_vision_cannot_save_description(self):
        def describe(*args):
            self.db.record_consent(11,11,'revoked','revoked-during-image')
            return self.description,{}
        self.ai.describe_image.side_effect=describe
        row=self.process(attachments=self.photo)
        self.assertEqual(row['outcome'],'cancelled')
        self.assertEqual(row['image_descriptions'],'[]')
        self.ai.clarify_photo.assert_not_called()

    def test_revocation_during_question_clears_saved_description_and_reply(self):
        def clarify(*args):
            self.db.record_consent(11,11,'revoked','revoked-during-question')
            return dict(action='clarify',question=self.question,kind='question',tone='neutral')
        self.ai.clarify_photo.side_effect=clarify
        row=self.process(attachments=self.photo)
        self.assertEqual(row['outcome'],'cancelled')
        self.assertEqual(row['image_descriptions'],'[]')
        self.assertIsNone(row['reply'])

    def test_retention_removes_derived_image_content(self):
        row=self.process(attachments=self.photo)
        self.engine.deliver(self.transport)
        self.db.execute('UPDATE requests SET created_at=1 WHERE id=?',(row['id'],))
        self.db.purge()
        self.assertEqual(self.db.one('SELECT image_descriptions FROM requests')['image_descriptions'],'[]')

    def test_question_api_validates_generated_question(self):
        ai=GigaChat(self.cfg)
        payload={'choices':[{'message':{'content':json.dumps({'question':self.question})}}],'usage':{'prompt_tokens':10,'completion_tokens':4}}
        with patch.object(ai,'call',return_value=payload) as call:
            result=ai.clarify_photo('',[self.description])
        self.assertEqual(result['action'],'clarify')
        self.assertEqual(result['question'],self.question)
        sent=call.call_args.kwargs['json']['messages'][1]['content']
        self.assertEqual(json.loads(sent)['image_descriptions'],[self.description])
        for bad in ('Пришлите номер телефона?','Укажите паспорт?','Текст без вопроса'):
            payload['choices'][0]['message']['content']=json.dumps({'question':bad})
            with patch.object(ai,'call',return_value=payload),self.assertRaises(AIError):
                ai.clarify_photo('',[self.description])

    def test_ai_failure_uses_existing_staff_fallback(self):
        self.ai.clarify_photo.side_effect=AIError('network')
        row=self.process(attachments=self.photo)
        self.assertEqual(row['reason'],'processing_error')
        self.assertEqual(row['outcome'],'handoff')
