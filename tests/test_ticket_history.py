import tempfile
import unittest
from pathlib import Path

from pulkovo.config import Config
from pulkovo.db import Database
from pulkovo.operator import OperatorInbox
from pulkovo.web import create_app


class TicketHistoryTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.db=Database(Path(temp.name)/'test.sqlite3')
        self.cfg=Config(self.db.path,admin_password='test',secret_key='test')
        self.db.conversation(11)
        self.db.record_consent(11,11,'accepted','consent')
        self.inbox=OperatorInbox(self.db)
        self.serial=0
        self.old=self.message('Первый вопрос',100,'Первый ответ бота')
        self.old_handoff=self.message('Первое обращение сотруднику',150)
        self.boundary=self.message('На границе первого обращения',200)
        self.second=self.message('Второй вопрос до передачи сотруднику',210,'Второй ответ бота')
        self.second_handoff=self.message('Второе обращение сотруднику',250)
        self.after=self.message('Сообщение после завершения',500)
        for ident,request_id,start,end in [(1,self.old_handoff,150,200),(2,self.second_handoff,250,400)]:
            self.db.execute("INSERT INTO tickets(id,peer_id,request_id,reason,status,created_at,closed_at) VALUES (?,11,?,'user_requested_operator','closed',?,?)",(ident,request_id,start,end))
            self.db.execute("INSERT INTO operator_outbox(client_key,peer_id,ticket_id,basis_request_id,text,staff_name,random_id,status,created_at,updated_at) VALUES (?,11,?,?,?,'Сотрудник',?,'cancelled',?,?)",(f'cancelled-{ident}',ident,request_id,f'Отменённый ответ {ident}',ident,start+5,start+5))
        for ident,at in [(1,160),(2,300),(3,510)]:
            self.db.execute('INSERT INTO staff_replies(event_key,peer_id,vk_cmid,admin_id,text,created_at,updated_at) VALUES (?,11,?,99,?,?,?)',(f'staff-{ident}',ident,f'Ответ сотрудника {ident}',at,at))
        self.client=create_app(self.cfg,self.db).test_client()
        with self.client.session_transaction() as session:
            session.update(authenticated=True,csrf='test')

    def message(self,text,at,reply=None):
        self.serial+=1
        ident=self.db.enqueue(11,11,f'in-{self.serial}',text)
        self.db.execute('UPDATE requests SET created_at=?,updated_at=?,status=?,reply=? WHERE id=?',(at,at+1,'sent' if reply else 'done',reply,ident))
        return ident

    def test_each_ticket_contains_its_own_incoming_bot_staff_and_outbox(self):
        first='\n'.join(m['text'] for m in self.inbox.timeline(11,1))
        second='\n'.join(m['text'] for m in self.inbox.timeline(11,2))
        for text in ('Первый вопрос','Первый ответ бота','На границе первого обращения','Ответ сотрудника 1','Отменённый ответ 1'):
            self.assertIn(text,first)
            self.assertNotIn(text,second)
        for text in ('Второй вопрос до передачи сотруднику','Второй ответ бота','Ответ сотрудника 2','Отменённый ответ 2'):
            self.assertIn(text,second)
            self.assertNotIn(text,first)
        for text in ('Сообщение после завершения','Ответ сотрудника 3'):
            self.assertNotIn(text,first+second)
        all_messages='\n'.join(m['text'] for m in self.inbox.timeline(11))
        self.assertIn('Первый вопрос',all_messages)
        self.assertIn('Сообщение после завершения',all_messages)
        self.assertIn('Ответ сотрудника 3',all_messages)

    def test_old_ticket_filter_is_applied_before_history_limit(self):
        for index in range(120):
            self.message(f'Новое сообщение {index}',600+index)
        self.assertIn('Первый вопрос',[m['text'] for m in self.inbox.timeline(11,1)])
        self.assertNotIn('Первый вопрос',[m['text'] for m in self.inbox.timeline(11)])

    def test_queue_has_one_dialogue_per_user_and_legacy_links_redirect(self):
        redirect=self.client.get('/queue?state=closed')
        self.assertEqual(redirect.location,'/queue?state=all')
        queue=self.client.get(redirect.location).get_data(as_text=True)
        self.assertNotIn('?ticket=',queue)
        self.assertEqual(queue.count('VK ID: 11'),1)
        self.assertIn('У бота',queue)
        self.assertEqual(self.client.get('/dialogue/11?ticket=2').location,'/dialogue/11')
        page=self.client.get('/dialogue/11').get_data(as_text=True)
        for text in ('Первый вопрос','Второй вопрос до передачи сотруднику','Сообщение после завершения'):
            self.assertIn(text,page)
        self.assertIn('data-updates="/dialogue/11/updates"',page)
        self.assertIn('id="reply-form"',page)
        self.assertNotIn('Обращение №',page)
        revision=self.client.get('/dialogue/11/updates').get_json()['revision']
        self.message('Еще одно сообщение',800)
        update=self.client.get('/dialogue/11/updates',query_string={'ticket':2,'revision':revision}).get_json()
        self.assertIn('Еще одно сообщение',update['html'])
        self.assertEqual(update['status'],'У бота')

    def test_page_and_poll_show_latest_30_messages_across_old_tickets(self):
        for index in range(50):
            self.message(f'Уникальное сообщение [{index}]',600+index)
        page=self.client.get('/dialogue/11').get_data(as_text=True)
        update=self.client.get('/dialogue/11/updates').get_json()['html']
        for html in (page,update):
            self.assertEqual(html.count('data-message-id='),30)
            self.assertNotIn('Уникальное сообщение [19]',html)
            self.assertIn('Уникальное сообщение [20]',html)
            self.assertIn('Уникальное сообщение [49]',html)
            self.assertNotIn('Первый вопрос',html)

    def test_users_never_handed_to_staff_are_visible_and_state_filters_work(self):
        self.db.conversation(22)
        ident=self.db.enqueue(22,22,'bot-only','Вопрос боту')
        self.db.execute("UPDATE requests SET text='Вопрос боту',status='done' WHERE id=?",(ident,))
        self.assertEqual({r['peer_id'] for r in self.db.dialogue_rows('all')},{11,22})
        self.assertEqual(self.db.dialogue_rows('open'),[])
        request=self.db.one('SELECT * FROM requests WHERE id=?',(self.after,))
        self.db.handoff(request,'user_requested_operator')
        self.assertEqual([r['peer_id'] for r in self.db.dialogue_rows('open')],[11])
        self.assertEqual(len(self.db.dialogue_rows('all')),2)
        self.db.staff_replied(11,'staff-waiting',99,20,1000,text='Ответ')
        self.assertEqual([r['peer_id'] for r in self.db.dialogue_rows('waiting_user')],[11])
        self.db.resume(11)
        self.assertEqual(self.db.dialogue_rows('waiting_user'),[])
        self.assertEqual(len(self.db.dialogue_rows('all')),2)
        page=self.client.get('/queue?state=all').get_data(as_text=True)
        self.assertIn('Вопрос боту',page)

    def test_invalid_or_foreign_ticket_never_falls_back_to_all_history(self):
        self.db.conversation(22)
        for suffix in ('','/updates'):
            for ticket,expected in [('bad',400),('-1',400),('',400),('999',404)]:
                self.assertEqual(self.client.get('/dialogue/11'+suffix,query_string={'ticket':ticket}).status_code,expected)
            self.assertEqual(self.client.get('/dialogue/22'+suffix,query_string={'ticket':1}).status_code,404)
        with self.assertRaises(ValueError):
            self.inbox.timeline(22,1)

    def test_closed_view_cannot_send_or_change_current_ticket_but_can_edit_own_topic(self):
        active=self.db.execute("INSERT INTO tickets(peer_id,request_id,reason,status,created_at) VALUES (11,?,'staff_joined','open',500)",(self.after,))
        for action in ('reply','resume','take','note','retry','cancel'):
            result=self.client.post('/dialogue/11?ticket=1',data={'csrf':'test','action':action,'reply':'Не отправлять'})
            self.assertEqual(result.status_code,409)
        self.assertEqual(self.db.one('SELECT status FROM tickets WHERE id=?',(active,))['status'],'open')
        self.assertEqual(self.db.one("SELECT count(*) n FROM operator_outbox WHERE status='pending'")['n'],0)
        result=self.client.post('/dialogue/11?ticket=1',data={'csrf':'test','action':'topic','ticket_id':1,'topic_version':1,'topic':'Первая тема'})
        self.assertEqual(result.status_code,302)
        self.assertEqual(result.location,'/dialogue/11?ticket=1')
        result=self.client.post('/dialogue/11?ticket=1',data={'csrf':'test','action':'topic','ticket_id':2,'topic_version':1,'topic':'Чужая тема'})
        self.assertEqual(result.status_code,400)
        self.db.execute('UPDATE conversations SET human_mode=1 WHERE peer_id=11')
        page=self.client.get(f'/dialogue/11?ticket={active}',follow_redirects=True).get_data(as_text=True)
        self.assertIn('id="reply-form"',page)
        self.assertIn('Сообщение после завершения',page)
        self.assertIn('Второй вопрос до передачи сотруднику',page)
        dashboard=self.client.get('/').get_data(as_text=True)
        self.assertEqual(dashboard.count('/dialogue/11'),2)
        self.assertNotIn('?ticket=',dashboard)
