"""Подтверждение автора исходящих сообщений и восстановление пропущенных ответов."""

import logging
import time

import requests
import vk_api
from .operator import OperatorInbox
from .attachments import normalize_attachments
from .feedback import is_feedback_payload


def metadata(message):
    # Технические поля; содержание для общей истории добавляет display_message.
    return {key:message[key] for key in (
        'id','peer_id','from_id','conversation_message_id','date','out',
        'admin_author_id','random_id','payload',
    ) if key in message}


def display_message(message):
    result=metadata(message)
    result['text']=message.get('text','')[:12000]
    result['display_attachments']=normalize_attachments(message.get('attachments',[]))
    return result


class StaffReplySync:
    def __init__(self, db, engine, api):
        self.db, self.engine, self.api = db, engine, api
        self.group_id = engine.config.group_id
        self.inbox = OperatorInbox(db)

    def event_key(self, message):
        cmid = message.get('conversation_message_id')
        identity = f'cmid:{cmid}' if cmid else f"id:{message['id']}"
        return f"reply:{self.group_id}:{message['peer_id']}:{identity}"

    def is_community_reply(self, message):
        return (0 < int(message.get('peer_id') or 0) < 2_000_000_000
                and message.get('from_id') == -self.group_id)

    def receive(self, message):
        message = display_message(message)
        if not self.is_community_reply(message) or self.engine.is_own_reply(message):
            return False
        own=self.inbox.match(message)
        if own:
            self.inbox.confirm(own,message)
            return True
        if not message.get('conversation_message_id') and not message.get('id'):
            return False
        if not message.get('admin_author_id'):
            # Поле автора необязательно в исходящем событии. Подтверждаем его
            # по самому сообщению; отсутствие метки бота не доказывает авторство.
            cmid, peer = message.get('conversation_message_id'), message['peer_id']
            if cmid:
                result = self.api.messages.getByConversationMessageId(
                    peer_id=peer,conversation_message_ids=cmid,group_id=self.group_id)
            else:
                result = self.api.messages.getById(message_ids=message['id'],group_id=self.group_id)
            matching = [item for item in result.get('items',[])
                        if item.get('peer_id') == peer and
                        (item.get('conversation_message_id') == cmid if cmid else item.get('id') == message['id'])]
            if not matching:
                return False
            message = display_message(matching[0])
            own=self.inbox.match(message)
            if own:
                self.inbox.confirm(own,message)
                return True
            if message.get('admin_author_id'):
                logging.info('Автор ответа сотрудника подтверждён через VK API.')
        return self.engine.receive_reply(message,self.event_key(message))

    def sync_ticket(self, ticket, *, close_inactive=False):
        # Берём последние сообщения только диалогов с активным обращением.
        result = self.api.messages.getHistory(peer_id=ticket['peer_id'],count=100)
        items = [display_message(m) for m in result.get('items',[]) if m.get('peer_id') == ticket['peer_id']]
        cutoff=time.time()-max(1,int(self.db.setting('retention_days')))*86400
        for message in items:
            if message.get('date',0)<cutoff:
                message['text']='';message['display_attachments']=[]
            if self.is_community_reply(message):
                own=self.inbox.match(message)
                if own and message.get('date',0)>=cutoff:
                    self.inbox.confirm(own,message)
        replies = [m for m in items if self.is_community_reply(m)
                   and (int(m.get('admin_author_id') or 0)>0 or self.inbox.match(m)) and not self.engine.is_own_reply(m)]
        if not replies:
            return False
        newest = max(replies,key=lambda m:(m.get('conversation_message_id',0),m.get('date',0)))
        incoming = [m for m in items if m.get('from_id') == ticket['peer_id'] and not is_feedback_payload(m.get('payload'))]
        unconfirmed_newer=any(self.is_community_reply(m) and not m.get('admin_author_id')
                              and not self.engine.is_own_reply(m) and not self.inbox.match(m)
                              and (m.get('conversation_message_id',0)>newest.get('conversation_message_id',0)
                                   or m.get('date',0)>newest.get('date',0)) for m in items)
        newer=False
        for message in incoming:
            if message.get('conversation_message_id') and newest.get('conversation_message_id'):
                newer = message['conversation_message_id'] > newest['conversation_message_id']
            else:
                newer = message.get('date',0) >= newest.get('date',0)
            if newer:
                # В истории уже есть новый вопрос, ещё не доставленный Long Poll.
                break
        changed=False
        if not newer:
            changed=self.engine.receive_reply(newest,self.event_key(newest),expected_ticket_id=ticket['id'])
        else:
            # Не ждём доставки входящего события Long Poll для отмены таймера.
            changed=bool(self.db.reopen_waiting_ticket(ticket['id'],newest.get('date',0)))
        for reply in replies:
            self.engine.receive_reply(reply,self.event_key(reply),expected_ticket_id=ticket['id'],history_only=True)
        if close_inactive and not newer and not unconfirmed_newer and self.db.expire_waiting_ticket(ticket['id']):
            logging.info('Обращение закрыто по неактивности; бот ответит только на новое сообщение.')
            changed=True
        return changed

    def sync_open_tickets(self, stop):
        changed = 0
        for ticket in self.db.all("SELECT id,peer_id FROM tickets WHERE status IN ('open','waiting_user') ORDER BY id"):
            if stop.is_set():
                break
            try:
                if self.sync_ticket(ticket,close_inactive=True):
                    changed += 1
                    logging.info('Статус обращения обновлён по подтверждённому ответу сотрудника в VK.')
            except (requests.RequestException,vk_api.exceptions.VkApiError):
                logging.warning('Не удалось сверить ответ сотрудника с VK; повтор в следующем цикле.')
            if stop.wait(.4):
                break
        return changed
