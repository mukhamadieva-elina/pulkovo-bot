from test_feedback import FeedbackFixture
from pulkovo.db import Database
from pulkovo.dialogue_rules import SESSION_INTRO, GREETING_PROMPT
from pulkovo.engine import Engine
from pulkovo.consent import consent_message


class SessionIntroTests(FeedbackFixture):
    def test_first_question_gets_intro_and_answer_in_one_message(self):
        row=self.message()
        self.assertEqual(row['reply'],SESSION_INTRO+'\n\nПарковка у терминала.')
        self.assertEqual(row['intro_text'],SESSION_INTRO)
        self.assertEqual(row['status'],'sent')
        self.transport.send.assert_called_once()
        second=self.message('Сколько стоит парковка?')
        self.assertEqual(second['reply'],'Парковка у терминала.')
        self.assertEqual(second['intro_text'],'')

    def test_rating_starts_new_session_with_intro_even_within_24_hours(self):
        self.send_survey()
        rated=self.message('5 ⭐',payload=self.payload())
        self.assertEqual(rated['intro_text'],'')
        self.now+=1
        new=self.message('Другой вопрос')
        self.assertNotEqual(new['session_id'],rated['session_id'])
        self.assertEqual(new['intro_text'],SESSION_INTRO)
        self.assertEqual(new['reply'].count(SESSION_INTRO),1)

    def test_inactivity_and_manual_completion_allow_intro_for_next_question(self):
        first=self.message()
        self.now+=86401
        second=self.message()
        self.assertNotEqual(second['session_id'],first['session_id'])
        self.assertEqual(second['intro_text'],SESSION_INTRO)
        self.message('/оператор')
        self.db.resume(11)
        third=self.message()
        self.assertNotEqual(third['session_id'],second['session_id'])
        self.assertEqual(third['intro_text'],SESSION_INTRO)

    def test_consent_has_intro_once_and_following_question_has_none(self):
        self.db.record_consent(11,11,'revoked','revoke')
        first=self.message()
        self.assertEqual(first['outcome'],'consent_required')
        self.assertEqual(first['reply'].count(SESSION_INTRO),1)
        self.assertNotIn('GigaChat',first['reply'])
        self.assertNotIn('ИИ',first['reply'])
        self.message('Согласен',payload={'action':'agree','version':'v1'})
        question=self.message()
        self.assertEqual(question['reply'],'Парковка у терминала.')

    def test_restart_preserves_intro_and_retry_sends_same_text(self):
        first=self.message(deliver=False)
        self.transport.send.side_effect=RuntimeError('offline')
        self.engine.deliver(self.transport)
        self.now+=11
        self.engine=Engine(Database(self.db.path),self.ai,self.cfg)
        self.transport.send.side_effect=None
        self.engine.deliver(self.transport)
        self.assertEqual({call.args[1] for call in self.transport.send.call_args_list},{first['reply']})
        self.assertEqual(self.message('Привет')['reply'],GREETING_PROMPT)

    def test_changed_intro_does_not_invalidate_queued_answer_but_card_change_does(self):
        first=self.message(deliver=False)
        self.db.set_settings({'session_intro':'Добрый день! Помогу с вашим вопросом.'})
        self.engine.deliver(self.transport)
        self.assertEqual(self.db.one('SELECT status FROM requests WHERE id=?',(first['id'],))['status'],'sent')
        self.assertEqual(self.transport.send.call_args.args[1],first['reply'])
        next_request=self.message(deliver=False)
        card=self.db.one('SELECT * FROM cards WHERE id=?',(self.card,))
        self.db.save_card(dict(card,answer='Измененный ответ.'),self.card,card['version'])
        self.transport.send.reset_mock()
        self.engine.deliver(self.transport)
        self.transport.send.assert_not_called()
        self.assertEqual(self.db.one('SELECT status FROM requests WHERE id=?',(next_request['id'],))['status'],'cancelled')


class NewChatIntroTests(FeedbackFixture):
    def setUp(self):
        super().setUp()
        self.db.execute('DELETE FROM consent_events WHERE peer_id=11')
        self.db.execute("UPDATE conversations SET consent_state='unknown',consent_version='' WHERE peer_id=11")

    def test_first_question_consent_and_answer_do_not_repeat_vk_greeting(self):
        offer=self.message('Где парковка?')
        self.assertEqual(offer['outcome'],'consent_required')
        self.assertEqual(offer['reply'],consent_message(self.db.settings()))
        self.assertEqual(offer['intro_text'],'')
        self.assertNotIn(SESSION_INTRO,offer['reply'])
        self.ai.route.assert_not_called()
        self.assertFalse(self.db.allowed(11))

        accepted=self.message('Согласен')
        self.assertEqual(accepted['outcome'],'consent_accepted')
        self.assertEqual(accepted['intro_text'],'')
        self.assertTrue(self.db.allowed(11))
        event=self.db.one("SELECT * FROM consent_events WHERE peer_id=11 AND action='accepted'")
        self.assertEqual(event['text_snapshot'],offer['reply'])
        answer=self.message('Где парковка?')
        self.assertEqual(answer['session_id'],offer['session_id'])
        self.assertEqual(answer['reply'],'Парковка у терминала.')

        self.now+=86401
        returning=self.message('Где парковка?')
        self.assertNotEqual(returning['session_id'],offer['session_id'])
        self.assertEqual(returning['reply'],SESSION_INTRO+'\n\nПарковка у терминала.')
        self.assertEqual(self.message('Еще вопрос')['intro_text'],'')

    def test_first_greeting_and_repeated_consent_request_have_no_intro(self):
        for text in ('Привет','Где парковка?'):
            reply=self.message(text)
            self.assertEqual(reply['reply'],consent_message(self.db.settings()))
            self.assertEqual(reply['intro_text'],'')
        self.ai.route.assert_not_called()

    def test_returning_without_consent_gets_intro_in_next_session_only(self):
        first=self.message('Привет')
        self.now+=86401
        returning=self.message('Привет')
        self.assertNotEqual(returning['session_id'],first['session_id'])
        self.assertEqual(returning['reply'],consent_message(self.db.settings(),include_intro=True))
        self.assertEqual(self.message('Согласна')['intro_text'],'')
        self.assertEqual(self.message()['reply'],'Парковка у терминала.')

    def test_restart_and_retry_do_not_add_greeting_to_first_offer(self):
        first=self.message('Привет',deliver=False)
        self.transport.send.side_effect=RuntimeError('offline')
        self.engine.deliver(self.transport)
        self.now+=11
        self.engine=Engine(Database(self.db.path),self.ai,self.cfg)
        self.transport.send.side_effect=None
        self.engine.deliver(self.transport)
        self.assertEqual({call.args[1] for call in self.transport.send.call_args_list},
            {consent_message(self.db.settings())})
        self.assertEqual(self.db.one('SELECT status FROM requests WHERE id=?',(first['id'],))['status'],'sent')
        self.assertEqual(self.message('Согласен')['intro_text'],'')

    def test_direct_handoff_in_new_chat_does_not_repeat_vk_greeting(self):
        reply=self.message('/оператор')
        self.assertEqual(reply['outcome'],'handoff')
        self.assertEqual(reply['intro_text'],'')
        self.db.resume(11)
        self.assertEqual(self.message()['intro_text'],SESSION_INTRO)
