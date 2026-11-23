"""Запуск VK Long Poll. Админка запускается отдельно: python admin.py."""

import fcntl
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import requests
import vk_api
from vk_api.bot_longpoll import VkBotEventType, VkBotLongPoll

from pulkovo.ai import GigaChat
from pulkovo.config import Config
from pulkovo.consent import consent_configured
from pulkovo.db import Database
from pulkovo.engine import Engine
from pulkovo.vk_staff import StaffReplySync
from pulkovo.operator import OperatorInbox
from pulkovo.attachments import normalize_attachments
from pulkovo.profiles import ProfileSync
from pulkovo.feedback import Feedback
from pulkovo.sessions import expire_sessions


class VKSession(requests.Session):
    def request(self, method, url, **kwargs):
        kwargs.setdefault("timeout", (10, 30))
        return super().request(method, url, **kwargs)


def vk_session(config):
    return vk_api.VkApi(token=config.vk_token, session=VKSession())


class VKTransport:
    def __init__(self, config):
        self.api = vk_session(config).get_api()

    def send(self, peer_id, text, keyboard, random_id):
        return self.api.messages.send(peer_id=peer_id,message=text,keyboard=keyboard,random_id=random_id,payload=json.dumps({'pulkovo_bot':random_id}))

    def mark_human(self, peer_id):
        try:
            self.api.messages.markAsImportantConversation(peer_id=peer_id,important=1)
            self.api.messages.markAsAnsweredConversation(peer_id=peer_id,answered=0)
        except Exception:
            logging.warning("Метка VK не установлена; обращение сохранено в очереди админки.")


def main():
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")
    config = Config.load()
    if not config.vk_token or not config.group_id:
        raise SystemExit("Заполните VK_TOKEN и VK_GROUP_ID в .env")
    db = Database(config.database)
    if config.external_user_data:
        if not config.gigachat_key:
            raise SystemExit("Заполните GIGACHAT_AUTH_TOKEN в .env")
        if not consent_configured(db.settings()):
            raise SystemExit("Настройте и включите текст согласия в админке. Ссылка необязательна.")
        if not db.cards(eligible=True):
            raise SystemExit("Опубликуйте хотя бы одну публичную карточку для текущего режима.")
    lock_file = open(config.database.parent / "bot.lock","w")
    try:
        fcntl.flock(lock_file,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("Бот уже запущен для этой базы.")
    db.execute("UPDATE requests SET status='pending' WHERE status='processing'")
    db.execute("UPDATE requests SET status='ready' WHERE status='sending'")
    db.execute("UPDATE operator_outbox SET status='pending' WHERE status='sending'")
    db.execute("UPDATE feedback_surveys SET status='pending' WHERE status='sending'")
    db.purge()
    stop = threading.Event()

    def worker():
        engine = Engine(db,GigaChat(config),config)
        transport = VKTransport(config)
        while not stop.is_set():
            try:
                if engine.deliver(transport):
                    continue
                request = engine.claim()
                if request:
                    engine.process(request)
                else:
                    stop.wait(.3)
            except Exception:
                logging.error("Ошибка обработчика; содержимое переписки и ключи не выводятся.")
                stop.wait(2)

    ingress = Engine(db,None,config)

    def sync_staff():
        sync = StaffReplySync(db,Engine(db,None,config),vk_session(config).get_api())
        while not stop.is_set():
            try:
                sync.sync_open_tickets(stop)
            except Exception:
                logging.error('Ошибка сверки ответов сотрудников; повтор через 15 секунд.')
            stop.wait(15)

    staff_thread = threading.Thread(target=sync_staff,name='vk-staff-sync',daemon=True)
    staff_thread.start()

    def sync_profiles():
        profiles=ProfileSync(db,vk_session(config).get_api())
        while not stop.is_set():
            try:
                profiles.refresh()
            except Exception:
                logging.warning('Не удалось обновить имена VK; в админке остаются сохранённые данные и ID.')
            stop.wait(15)

    threading.Thread(target=sync_profiles,name='vk-profiles',daemon=True).start()

    def operator_worker():
        inbox=OperatorInbox(db)
        api=vk_session(config).get_api()
        while not stop.is_set():
            try:
                if inbox.deliver(api):
                    continue
            except Exception:
                logging.error('Ошибка отправки ответа сотрудника; содержимое не выводится.')
            stop.wait(.5)

    threading.Thread(target=operator_worker,name='operator-outbox',daemon=True).start()

    def feedback_worker():
        feedback=Feedback(db)
        transport=VKTransport(config)
        while not stop.is_set():
            try:
                expire_sessions(db)
                if feedback.deliver(transport,ingress):
                    continue
            except Exception:
                logging.warning('Не удалось проверить опрос; повтор в следующем цикле.')
            stop.wait(15)

    threading.Thread(target=feedback_worker,name='feedback-surveys',daemon=True).start()
    with ThreadPoolExecutor(max_workers=4,thread_name_prefix="dialogue") as pool:
        for _ in range(4):
            pool.submit(worker)
        logging.info("Бот запущен. Обработчики: 4. Передача данных в ИИ: %s",config.external_user_data)
        try:
            while not stop.is_set():
                try:
                    session = vk_session(config)
                    staff_events = StaffReplySync(db,ingress,session.get_api())
                    poll = VkBotLongPoll(session,config.group_id,wait=20)
                    logging.info("VK Long Poll подключён. Ожидаю входящие сообщения сообщества.")
                    for event in poll.listen():
                        if event.type not in (VkBotEventType.MESSAGE_NEW, VkBotEventType.MESSAGE_REPLY):
                            continue
                        message = event.obj.get("message", event.obj)
                        if event.type == VkBotEventType.MESSAGE_REPLY:
                            staff_events.receive(message)
                            continue
                        attachments = normalize_attachments(message.get('attachments',[]))
                        key = f"{config.group_id}:{message['peer_id']}:{message.get('conversation_message_id') or message['id']}"
                        ingress.receive(message["peer_id"],message["from_id"],key,message.get("text",""),attachments,message.get("payload"),vk_cmid=int(message.get('conversation_message_id') or 0),vk_date=int(message.get('date') or 0))
                except (requests.RequestException,vk_api.exceptions.VkApiError):
                    logging.warning("Соединение с VK прервано. Повтор через 5 секунд.")
                    stop.wait(5)
        except KeyboardInterrupt:
            logging.info("Остановка бота…")
        finally:
            stop.set()
            lock_file.close()


if __name__ == "__main__":
    main()
