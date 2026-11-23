import json

from test_feedback import FeedbackFixture
from pulkovo.consent import INTRODUCTION, is_typed_consent


class TypedConsentTests(FeedbackFixture):
    def setUp(self):
        super().setUp()
        self.db.record_consent(11,11,'revoked','revoke')
        self.ai.reset_mock()

    def test_only_explicit_standalone_words(self):
        for text in ('Согласен',' согласна! ','СОГЛАСЕН.','Согласна'):
            self.assertTrue(is_typed_consent(text))
        for text in ('не согласен','да','Ок','согласен, но не на ИИ','согласен где парковка','согласен?', 'согласен '*100):
            self.assertFalse(is_typed_consent(text))

    def test_delivered_offer_accepts_text_and_records_exact_shown_conditions(self):
        offer=self.message('Привет')
        self.assertIn(INTRODUCTION,offer['reply'])
        self.assertIn('Согласна',offer['reply'])
        self.assertNotIn('Здравствуйте!',offer['reply'])
        self.assertTrue(self.db.setting('consent_text').startswith('Здравствуйте!'))
        accepted=self.message('Согласна!')
        self.assertEqual(accepted['outcome'],'consent_accepted')
        self.assertTrue(self.db.allowed(11))
        self.ai.route.assert_not_called()
        payload=json.loads(accepted['payload'])
        self.assertEqual(payload['source'],'text')
        event=self.db.one('SELECT * FROM consent_events ORDER BY id DESC LIMIT 1')
        self.assertEqual(event['text_snapshot'],offer['reply'])
        self.assertEqual(event['version'],'v1')
        self.assertEqual(accepted['text'],'')

    def test_word_before_offer_does_not_authorize_ai(self):
        row=self.message('Согласен')
        self.assertEqual(row['outcome'],'consent_required')
        self.assertFalse(self.db.allowed(11))
        self.ai.route.assert_not_called()

    def test_first_thanks_also_shows_introduction_to_new_user(self):
        self.db.execute("UPDATE conversations SET consent_state='unknown' WHERE peer_id=11")
        row=self.message('Спасибо')
        self.assertEqual(row['outcome'],'consent_required')
        self.assertIn(INTRODUCTION,row['reply'])

    def test_ready_and_failed_offer_are_not_proof_of_delivery(self):
        self.message('Привет',deliver=False)
        ident=self.receive('Согласен')
        row=self.db.one('SELECT * FROM requests WHERE id=?',(ident,))
        self.assertNotIn('action',json.loads(row['payload']))
        self.db.execute("UPDATE requests SET status='failed'")
        row=self.message('Согласна',deliver=False)
        self.assertEqual(row['outcome'],'consent_required')
        self.assertFalse(self.db.allowed(11))

    def test_changed_conditions_require_a_new_offer(self):
        self.message('Привет')
        self.db.set_settings({'consent_version':'v2'})
        row=self.message('Согласен')
        self.assertEqual(row['outcome'],'consent_required')
        self.assertFalse(self.db.allowed(11))
        self.assertEqual(self.message('Согласен')['outcome'],'consent_accepted')

    def test_change_between_receipt_and_processing_is_rechecked(self):
        self.message('Привет')
        ident=self.receive('Согласен')
        self.db.set_settings({'consent_version':'v2'})
        self.engine.process(self.engine.claim())
        row=self.db.one('SELECT * FROM requests WHERE id=?',(ident,))
        self.assertEqual(row['outcome'],'consent_required')
        self.assertFalse(self.db.allowed(11))

    def test_mixed_message_and_photo_do_not_grant_consent(self):
        self.message('Привет')
        self.assertEqual(self.message('Согласен, но не на ИИ')['outcome'],'consent_required')
        self.now+=1
        ident=self.engine.receive(11,11,'photo-consent','Согласен',[{'type':'photo'}])
        self.engine.process(self.engine.claim())
        self.assertEqual(self.db.one('SELECT outcome FROM requests WHERE id=?',(ident,))['outcome'],'consent_required')
        self.assertFalse(self.db.allowed(11))

    def test_text_cannot_restart_ai_during_staff_conversation(self):
        self.message('Привет')
        self.message('/оператор')
        self.assertEqual(self.message('Согласен')['outcome'],'human_mode')
        self.assertFalse(self.db.allowed(11))

    def test_user_cannot_forge_server_side_text_proof(self):
        row=self.message('Произвольный текст',payload={'source':'text','prompt_id':1,'local_intent':'agree'})
        self.assertEqual(row['outcome'],'consent_required')
        self.assertFalse(self.db.allowed(11))

    def test_duplicate_event_does_not_record_consent_twice(self):
        self.message('Привет')
        row=self.message('Согласен')
        self.assertIsNone(self.engine.receive(11,11,row['event_key'],'Согласен'))
        self.assertEqual(len(self.db.all("SELECT * FROM consent_events WHERE action='accepted'")),2)
