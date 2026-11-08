import json
import tempfile
import time
import unittest
from pathlib import Path

from pulkovo.config import Config
from pulkovo.db import Database
from pulkovo.statistics import automation_summary
from web_client import authenticated_client


class AutomationStatisticsTests(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        self.db=Database(Path(folder.name)/'test.sqlite3')
        self.cfg=Config(self.db.path,admin_password='test',secret_key='test')
        self.serial=0

    def message(self, outcome='answer', status='sent', *, at=100, human=False, reason='', payload=None, peer=None):
        self.serial+=1
        peer=peer or self.serial
        ident=self.db.enqueue(peer,peer,f'event-{self.serial}',payload=json.dumps(payload or {}))
        self.db.execute('UPDATE requests SET outcome=?,status=?,created_at=?,updated_at=?,reason=? WHERE id=?',(outcome,status,at,at,reason,ident))
        if human:
            self.db.execute("INSERT INTO tickets(peer_id,request_id,reason,status,created_at,closed_at,closure_reason) VALUES (?,?,'no_matching_answer','closed',?,?, 'manual')",(peer,ident,at,at+1))
        return ident

    def test_no_data_has_no_percentage(self):
        self.assertEqual(automation_summary(self.db,0,1000),dict(automated=0,handed_off=0,total=0,percent=None))

    def test_percentage_uses_completed_results_not_all_incoming_messages(self):
        for _ in range(3):self.message()
        self.message('handoff',human=True)
        for outcome in ('consent_required','consent_accepted','consent_revoked','thanks','clarify','human_mode'):
            self.message(outcome)
        self.assertEqual(automation_summary(self.db,0,1000),dict(automated=3,handed_off=1,total=4,percent=75))

    def test_pending_failed_and_cancelled_answers_are_not_successes(self):
        for status in ('pending','processing','ready','sending','failed','cancelled','done'):
            self.message(status=status)
        self.assertEqual(automation_summary(self.db,0,1000)['total'],0)
        self.message(status='failed',human=True,reason='delivery_failed')
        self.assertEqual(automation_summary(self.db,0,1000),dict(automated=0,handed_off=1,total=1,percent=0))

    def test_handoff_is_counted_even_if_notification_not_sent(self):
        for status in ('ready','failed','cancelled'):
            self.message('handoff',status,human=True)
        self.assertEqual(automation_summary(self.db,0,1000)['handed_off'],3)

    def test_staff_joined_after_answer_is_one_human_result(self):
        ident=self.message(human=True)
        self.db.execute("INSERT INTO tickets(peer_id,request_id,reason,status,created_at,closed_at) VALUES (1,?,'staff_joined','closed',200,201)",(ident,))
        self.assertEqual(automation_summary(self.db,0,1000),dict(automated=0,handed_off=1,total=1,percent=0))

    def test_clarifications_and_followup_messages_do_not_multiply_handoffs(self):
        self.message('clarify',peer=11)
        self.message('clarify',peer=11)
        self.message('handoff',human=True,peer=11)
        self.message('human_mode',peer=11)
        self.message('human_mode',peer=11)
        self.assertEqual(automation_summary(self.db,0,1000)['total'],1)

    def test_clarification_then_answer_is_one_automated_result(self):
        self.message('clarify',peer=11)
        self.message('clarify',peer=11)
        self.message(peer=11)
        self.assertEqual(automation_summary(self.db,0,1000),dict(automated=1,handed_off=0,total=1,percent=100))

    def test_control_and_thanks_handoffs_are_excluded(self):
        for action in ('agree','decline','revoke'):
            self.message('handoff',human=True,payload={'action':action})
        self.message('handoff',human=True,reason='consent_declined')
        self.message('thanks','failed',human=True,reason='delivery_failed')
        self.assertEqual(automation_summary(self.db,0,1000)['total'],0)

    def test_period_uses_incoming_date_and_inactivity_does_not_mean_success(self):
        self.message(at=99)
        self.message(at=100)
        self.message('handoff',human=True,at=200)
        self.message(at=201)
        self.db.execute("UPDATE tickets SET closure_reason='inactivity'")
        self.assertEqual(automation_summary(self.db,100,201),dict(automated=1,handed_off=1,total=2,percent=50))

    def test_later_separate_handoff_is_a_separate_processing_result(self):
        self.message(peer=11)
        self.message('handoff',human=True,peer=11,at=110)
        self.assertEqual(automation_summary(self.db,0,1000),dict(automated=1,handed_off=1,total=2,percent=50))

    def test_statistics_page_shows_formula_counts_and_period(self):
        now=time.time()
        self.message(at=now)
        self.message(at=now)
        self.message('handoff',human=True,at=now)
        self.message(at=now-2*86400)
        client=authenticated_client(self.cfg,self.db);self.addCleanup(client.close)
        page=client.get('/statistics?days=1').text
        self.assertIn('66,7%',page)
        self.assertIn('2 из 3 результатов обработки',page)
        self.assertIn('не подтверждение',page)
        self.assertIn('считается отдельно',page)
        self.assertIn('75,0%',client.get('/statistics?days=7').text)

    def test_empty_statistics_does_not_claim_zero_percent(self):
        client=authenticated_client(self.cfg,self.db);self.addCleanup(client.close)
        page=client.get('/statistics').text
        self.assertIn('Пока нет готовых ответов и передач сотруднику',page)
        self.assertNotIn('0,0%',page)
