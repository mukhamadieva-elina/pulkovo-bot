import json
import time

from .ai import AIError, AccessStopped, download_image
from .consent import consent_configured, consent_message
from .search import searcher
from .dialogue_rules import is_thanks


def keyboard(buttons):
    return json.dumps({"one_time":True,"buttons":[[{"action":{"type":"text","label":label,"payload":json.dumps(payload,ensure_ascii=False)},"color":"primary" if i == 0 else "secondary"}] for i,(label,payload) in enumerate(buttons)]},ensure_ascii=False)


class Engine:
    def __init__(self, db, ai, config):
        self.db, self.ai, self.config = db, ai, config
        self.searches = {}

    def consent_keyboard(self):
        return keyboard([("Согласен",{"action":"agree","version":self.db.setting("consent_version")}), ("Без ИИ — к сотруднику",{"action":"decline"})])

    def normal_keyboard(self):
        # Пустая клавиатура убирает ранее показанные постоянные кнопки VK.
        return json.dumps({"one_time": True, "buttons": []})

    def receive(self, peer_id, user_id, event_key, text, attachments=None, payload=None, *, vk_cmid=0, vk_date=0):
        if peer_id != user_id or user_id <= 0:
            return None
        if self.db.one("SELECT id FROM requests WHERE event_key=?",(event_key,)):
            return None
        try:
            control = json.loads(payload) if isinstance(payload,str) else payload or {}
            if not isinstance(control,dict):
                control = {}
        except (ValueError,TypeError):
            control = {}
        commands = {"/оператор":"operator","оператор":"operator","сотрудник":"operator","связаться с сотрудником":"operator","/отозвать":"revoke","отозвать согласие":"revoke"}
        if text.strip().lower() in commands:
            control = {"action":commands[text.strip().lower()]}
        if control.get("action") not in ("agree","decline","revoke","operator"):
            control = {}
        if not control and not attachments and is_thanks(text):
            control={'local_intent':'thanks'}
        self.db.enqueue(peer_id,user_id,event_key,text,attachments,json.dumps(control),vk_cmid=vk_cmid,vk_date=vk_date)
        request = self.db.one("SELECT * FROM requests WHERE event_key=?",(event_key,))
        if control.get("action") in ("decline","revoke"):
            self.db.record_consent(peer_id,user_id,"declined" if control["action"]=="decline" else "revoked",event_key)
        if control.get("action") in ("decline","operator"):
            self.db.handoff(request,"user_requested_operator")
        return request["id"]

    def is_own_reply(self, message):
        peer = int(message.get('peer_id') or 0)
        try:
            payload = message.get('payload') or {}
            payload = json.loads(payload) if isinstance(payload, str) else payload
            marker = payload.get('pulkovo_bot') if isinstance(payload, dict) else None
        except (ValueError, TypeError):
            marker = None
        for random_id in (message.get('random_id'), marker):
            if random_id and self.db.one('SELECT id FROM requests WHERE peer_id=? AND random_id=?', (peer, random_id)):
                return True
        return False

    def receive_reply(self, message, event_key, *, expected_ticket_id=None, history_only=False):
        peer = int(message.get('peer_id') or 0)
        if not 0 < peer < 2_000_000_000 or message.get('from_id') != -self.config.group_id or self.is_own_reply(message):
            return False
        # VK явно сообщает ID сотрудника; неизвестные исходящие не считаем ответом человека.
        admin_id = int(message.get('admin_author_id') or 0)
        if admin_id <= 0:
            return False
        return self.db.staff_replied(peer,event_key,admin_id,int(message.get('conversation_message_id') or 0),int(message.get('date') or time.time()),expected_ticket_id=expected_ticket_id,text=message.get('text'),attachments=message.get('display_attachments'),history_only=history_only)

    def prepare(self, request, outcome, reply=None, *, reason="", card=None, decision=None, generation=None, usage=None, keys=None):
        decision, usage = decision or {}, usage or {}
        self.db.execute("UPDATE requests SET status=?,outcome=?,reason=?,reply=?,keyboard=?,card_id=?,card_version=?,topic=?,intent=?,tone=?,ai_generation=?,prompt_tokens=?,completion_tokens=?,updated_at=? WHERE id=?", (
            "ready" if reply else "done",outcome,reason,reply,keys or self.normal_keyboard(),card["id"] if card else None,card["version"] if card else None,card["topic"] if card else decision.get("topic"),str(decision.get("intent",""))[:250],decision.get("tone"),generation,int(usage.get("prompt_tokens",0)),int(usage.get("completion_tokens",0)),time.time(),request["id"]))
        self.db.refresh_ticket_topics(request['peer_id'])

    def to_human(self, request, reason, decision=None, usage=None, card=None):
        self.db.handoff(request,reason)
        self.prepare(request,"handoff",self.db.setting("handoff_text"),reason=reason,decision=decision,usage=usage,card=card)

    def thank(self, request, generation, usage=None):
        self.db.execute('UPDATE conversations SET clarification_count=0 WHERE peer_id=?',(request['peer_id'],))
        self.prepare(request,'thanks','Пожалуйста!',generation=generation,usage=usage)

    def process(self, request):
        start = time.monotonic()
        peer = request["peer_id"]
        try:
            action = json.loads(request["payload"] or "{}").get("action")
            settings = self.db.settings()
            if action == "revoke":
                self.prepare(request,"consent_revoked","Согласие отозвано. Новые сообщения не будут передаваться ИИ. При необходимости выберите общение с сотрудником.",keys=self.consent_keyboard())
                return
            if action in ("operator","decline"):
                self.to_human(request,"consent_declined" if action=="decline" else "user_requested_operator")
                return
            if action == "agree":
                version = json.loads(request["payload"]).get("version","")
                if not consent_configured(settings) or version != settings["consent_version"]:
                    self.prepare(request,"consent_required","Условия согласия обновлены или ещё не опубликованы.\n\n" + consent_message(settings),keys=self.consent_keyboard())
                    return
                self.db.record_consent(peer,request["user_id"],"accepted",request["event_key"])
                message = "Согласие сохранено. Отправьте ваш вопрос или изображение.\n\nПри необходимости: /оператор — обратиться к сотруднику; /отозвать — прекратить обработку через ИИ."
                if self.db.conversation(peer)["human_mode"]:
                    message = "Согласие сохранено. Ваш диалог остаётся у сотрудника до завершения обращения."
                self.prepare(request,"consent_accepted",message)
                return
            conversation = self.db.conversation(peer)
            if conversation["human_mode"]:
                self.prepare(request,"human_mode",reason="awaiting_operator")
                return
            if json.loads(request['payload'] or '{}').get('local_intent')=='thanks':
                self.thank(request,conversation['generation'])
                return
            if not self.db.allowed(peer):
                if not consent_configured(settings):
                    self.to_human(request,"consent_not_configured")
                else:
                    self.prepare(request,"consent_required",consent_message(settings),keys=self.consent_keyboard())
                return
            if not self.config.external_user_data:
                self.to_human(request,"external_processing_not_enabled")
                return
            cards = self.db.cards(eligible=True)
            if not cards:
                self.to_human(request,"no_published_cards")
                return
            generation = conversation["generation"]
            guard = lambda: self.db.allowed(peer,generation) and consent_configured(self.db.settings())
            descriptions, usage = [], {"prompt_tokens":0,"completion_tokens":0}
            attachments = json.loads(request["attachments"])
            if len(attachments) > 3 or any(a.get("type") != "photo" for a in attachments):
                self.to_human(request,"unsupported_attachment")
                return
            for attachment in attachments:
                image = download_image(attachment["url"],guard)
                description, image_usage = self.ai.describe_image(image,request["text"],guard)
                descriptions.append(description)
                for key in usage:
                    usage[key] += int(image_usage.get(key,0))
            if descriptions and (not guard() or not self.db.save_image_descriptions(request,generation,descriptions)):
                raise AccessStopped('Обработка изображения отменена')
            if not request["text"].strip() and not descriptions:
                self.to_human(request,"empty_message")
                return
            strategy = settings["search_strategy"]
            if strategy not in self.searches:
                self.searches[strategy] = searcher(strategy,self.db)
            history = self.db.history(peer)
            query = "\n".join([request["text"]] + descriptions + [h["text"] for h in history] + [description for h in history for description in h.get('image_descriptions',[])])
            if descriptions and not request['text'].strip() and not history:
                # Из одного снимка нельзя вывести цель обращения или факт жалобы.
                candidates=[]
                decision=self.ai.clarify_photo(request['text'],descriptions,history,guard)
            else:
                candidates = self.searches[strategy].select(query,cards,int(settings["search_limit"]))
                for card in candidates:
                    card["rendered_answer"] = self.db.render_answer(card)
                decision = self.ai.route(request["text"],candidates,history,descriptions,guard)
                if strategy != "full" and decision["action"] == "handoff" and decision["kind"] == "question" and len(candidates) < len(cards):
                    for key in usage:
                        usage[key] += int(decision.get("usage",{}).get(key,0))
                    for card in cards:
                        card["rendered_answer"] = self.db.render_answer(card)
                    candidates = cards
                    decision = self.ai.route(request["text"],candidates,history,descriptions,guard)
            if descriptions and decision['action']=='handoff' and decision['kind'] not in ('complaint','thanks') and decision.get('handoff_reason') not in ('complaint','operator_request','out_of_scope','unsafe') and conversation['clarification_count']<int(settings['max_clarifications']):
                # Для фото сначала выясняем вопрос, если модель лишь не нашла ответ.
                for key in usage:
                    usage[key] += int(decision.get("usage",{}).get(key,0))
                decision=self.ai.clarify_photo(request['text'],descriptions,history,guard)
            for key in usage:
                usage[key] += int(decision.get("usage",{}).get(key,0))
            if not guard():
                raise AccessStopped("Диалог изменён во время обработки")
            card = next((c for c in candidates if c["id"] == decision.get("card_id")),None)
            if decision['kind']=='thanks':
                self.thank(request,generation,usage)
            elif decision["kind"] == "complaint" or decision["action"] == "handoff" or card and card["action"] == "handoff":
                self.to_human(request,"complaint" if decision["kind"]=="complaint" else "no_matching_answer",decision,usage,card)
            elif decision["action"] == "clarify":
                if conversation["clarification_count"] >= int(settings["max_clarifications"]):
                    self.to_human(request,"clarification_limit",decision,usage)
                else:
                    self.db.execute("UPDATE conversations SET clarification_count=clarification_count+1 WHERE peer_id=?",(peer,))
                    self.prepare(request,"clarify",decision["question"],decision=decision,generation=generation,usage=usage)
            elif card:
                self.db.execute("UPDATE conversations SET clarification_count=0 WHERE peer_id=?",(peer,))
                self.prepare(request,"answer",card["rendered_answer"],card=card,decision=decision,generation=generation,usage=usage)
            else:
                self.to_human(request,"invalid_decision")
        except AccessStopped:
            self.prepare(request,"cancelled",reason="consent_or_dialogue_changed")
        except (AIError, ValueError, RuntimeError, KeyError,TypeError):
            self.to_human(request,"processing_error")
            self.db.execute("UPDATE requests SET error='Ошибка обработки; проверьте настройки и доступность API' WHERE id=?",(request["id"],))
        finally:
            self.db.execute("UPDATE requests SET duration_ms=? WHERE id=?",(int((time.monotonic()-start)*1000),request["id"]))

    def claim(self):
        with self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM requests r WHERE status='pending' AND NOT EXISTS(SELECT 1 FROM requests p WHERE p.peer_id=r.peer_id AND p.id<r.id AND p.status IN ('pending','processing','ready','sending')) ORDER BY id LIMIT 1").fetchone()
            if not row:
                return None
            con.execute("UPDATE requests SET status='processing',updated_at=? WHERE id=?",(time.time(),row["id"]))
            return dict(row)

    def deliver(self, transport):
        with self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM requests WHERE status='ready' AND (send_attempts=0 OR updated_at<?) ORDER BY id LIMIT 1",(time.time()-10,)).fetchone()
            if not row:
                return False
            request = dict(row)
            con.execute("UPDATE requests SET status='sending' WHERE id=?",(request["id"],))
        if request['outcome']=='thanks':
            current=self.db.conversation(request['peer_id'])
            if current['human_mode'] or current['generation']!=request['ai_generation']:
                self.db.execute("UPDATE requests SET status='cancelled',reply=NULL WHERE id=?",(request['id'],))
                return True
        elif request["ai_generation"] is not None:
            card = self.db.one("SELECT * FROM cards WHERE id=?",(request["card_id"],)) if request["card_id"] else None
            current_mode = "cover" if self.db.setting("cover_mode")=="true" else "normal"
            stale_card = request["card_id"] and (not card or card["status"] != "published" or card["visibility"] != "public" or card["version"] != request["card_version"] or card["mode"] not in ("any",current_mode))
            if card and not stale_card:
                try:
                    stale_card = self.db.render_answer(card) != request["reply"]
                except ValueError:
                    stale_card = True
            if not self.db.allowed(request["peer_id"],request["ai_generation"]) or not consent_configured(self.db.settings()) or stale_card:
                self.db.execute("UPDATE requests SET status='cancelled',reason='state_changed_before_send' WHERE id=?",(request["id"],))
                self.db.handoff(request,"state_changed_before_send")
                transport.mark_human(request["peer_id"])
                return True
        try:
            transport.send(request["peer_id"],request["reply"],request["keyboard"],request["random_id"])
        except Exception:
            attempts = request["send_attempts"]+1
            self.db.execute("UPDATE requests SET status=?,send_attempts=?,error='Ошибка отправки VK',updated_at=? WHERE id=?",("failed" if attempts>=3 else "ready",attempts,time.time(),request["id"]))
            if attempts >= 3:
                self.db.handoff(request,"delivery_failed")
                transport.mark_human(request["peer_id"])
        else:
            self.db.execute("UPDATE requests SET status='sent',send_attempts=send_attempts+1,updated_at=? WHERE id=?",(time.time(),request["id"]))
            if request["outcome"] == "handoff":
                transport.mark_human(request["peer_id"])
        return True
