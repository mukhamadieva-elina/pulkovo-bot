import json
import time

from .ai import AIError, AccessStopped, download_image
from .consent import consent_configured, consent_message, is_typed_consent
from .search import searcher
from .dialogue_rules import SESSION_INTRO, GREETING_PROMPT, THANKS_TEXT, FAREWELL_TEXT, FEEDBACK_THANKS_TEXT, is_greeting, is_thanks, is_farewell, without_opening_greeting
from .feedback import Feedback
from .flights import SHARED_BOARD, parse_query, answer_query, previous_query
from .routing import STAFF_REASONS, handoff_reason, needs_full_catalog
from .protection import Protection, instruction_attack, UNSAFE_TEXT, TOO_LONG_TEXT, AI_LIMIT_TEXT


def keyboard(buttons):
    return json.dumps({"one_time":True,"buttons":[[{"action":{"type":"text","label":label,"payload":json.dumps(payload,ensure_ascii=False)},"color":"primary" if i == 0 else "secondary"}] for i,(label,payload) in enumerate(buttons)]},ensure_ascii=False)


class Engine:
    def __init__(self, db, ai, config):
        self.db, self.ai, self.config = db, ai, config
        self.searches = {}
        self.flight_board = SHARED_BOARD

    def consent_keyboard(self, settings=None):
        settings=self.db.settings() if settings is None else settings
        return keyboard([("Согласен",{"action":"agree","version":settings['consent_version']}), ("Написать сотруднику",{"action":"decline"})])

    def normal_keyboard(self):
        # Пустая клавиатура убирает ранее показанные постоянные кнопки VK.
        return json.dumps({"one_time": True, "buttons": []})

    def receive(self, peer_id, user_id, event_key, text, attachments=None, payload=None, *, vk_cmid=0, vk_date=0):
        if peer_id != user_id or user_id <= 0:
            return None
        if self.db.one("SELECT id FROM requests WHERE event_key=?",(event_key,)):
            return None
        try:
            control = json.loads(payload) if isinstance(payload,str) and len(payload)<=4096 else payload if isinstance(payload,dict) else {}
            if not isinstance(control,dict):
                control = {}
        except (ValueError,TypeError):
            control = {}
        commands = {"/оператор":"operator","оператор":"operator","сотрудник":"operator","связаться с сотрудником":"operator","/отозвать":"revoke","отозвать согласие":"revoke"}
        if text.strip().lower() in commands:
            control = {"action":commands[text.strip().lower()]}
        if control.get("action") not in ("agree","decline","revoke","operator","feedback"):
            control = {}
        elif control.get('action')=='agree':
            control={'action':'agree','version':str(control.get('version',''))[:100]}
        elif control.get('action')!='feedback':
            control={'action':control['action']}
        if control.get('action') == 'feedback':
            control={key:str(control.get(key,''))[:100] for key in ('action','token','rating')}
        if not control and not attachments and is_typed_consent(text):
            offer=self.db.consent_offer(peer_id)
            if offer:
                control={'action':'agree','version':offer['consent_prompt_version'],'source':'text','prompt_id':offer['id']}
        if not control and not attachments and is_farewell(text):
            control={'local_intent':'farewell'}
        elif not control and not attachments and is_thanks(text):
            control={'local_intent':'thanks'}
        elif not control and not attachments and is_greeting(text):
            control={'local_intent':'greeting'}
        if control.get("action") in ("decline","revoke"):
            state='declined' if control['action']=='decline' else 'revoked'
            if self.db.conversation(peer_id,user_id)['consent_state']!=state:
                self.db.record_consent(peer_id,user_id,state,event_key)
        if len(text)>4096:
            text=text[:4096]
            if not control:
                control={'input_too_long':True}
        ident=self.db.enqueue(peer_id,user_id,event_key,text,attachments,json.dumps(control),vk_cmid=vk_cmid,vk_date=vk_date,is_feedback=control.get('action')=='feedback',protect=True)
        if ident is None:
            return None
        request = self.db.one("SELECT * FROM requests WHERE id=?",(ident,))
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
            if random_id and self.db.one('SELECT id FROM feedback_surveys WHERE peer_id=? AND random_id=?', (peer, random_id)):
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
        intro=''
        session_intro=self.db.settings().get('session_intro',SESSION_INTRO).strip()
        if reply and outcome in ('answer','clarify','greeting','handoff'):
            reply = without_opening_greeting(reply)
        if reply and outcome in ('answer','clarify','greeting','handoff','flight_info','consent_required','consent_accepted') and self.db.needs_session_intro(request):
            intro=session_intro
            if intro:
                reply=intro+'\n\n'+reply
        self.db.execute("UPDATE requests SET status=?,outcome=?,reason=?,reply=?,intro_text=?,keyboard=?,card_id=?,card_version=?,topic=?,intent=?,tone=?,ai_generation=?,prompt_tokens=?,completion_tokens=?,updated_at=? WHERE id=?", (
            "ready" if reply else "done",outcome,reason,reply,intro,keys or self.normal_keyboard(),card["id"] if card else None,card["version"] if card else None,card["topic"] if card else decision.get("topic"),str(decision.get("intent",""))[:250],decision.get("tone"),generation,int(usage.get("prompt_tokens",0)),int(usage.get("completion_tokens",0)),time.time(),request["id"]))
        self.db.refresh_ticket_topics(request['peer_id'])
        if outcome=='consent_required':
            shown=json.loads(json.loads(keys)['buttons'][0][0]['action']['payload'])['version']
            self.db.execute('UPDATE requests SET consent_prompt_version=?,consent_prompt_generation=? WHERE id=?',
                (shown,self.db.conversation(request['peer_id'])['generation'],request['id']))

    def to_human(self, request, reason, decision=None, usage=None, card=None):
        self.db.handoff(request,reason)
        self.prepare(request,"handoff",self.db.setting("handoff_text"),reason=reason,decision=decision,usage=usage,card=card)

    def thank(self, request, generation, usage=None):
        self.db.execute('UPDATE conversations SET clarification_count=0 WHERE peer_id=?',(request['peer_id'],))
        self.prepare(request,'thanks',THANKS_TEXT,generation=generation,usage=usage)

    def process(self, request):
        current=self.db.one('SELECT status FROM requests WHERE id=?',(request['id'],))
        if not current or current['status'] not in ('pending','processing'):
            return
        start = time.monotonic()
        peer = request["peer_id"]
        try:
            action = json.loads(request["payload"] or "{}").get("action")
            if action == 'feedback':
                result=Feedback(self.db).vote(peer,json.loads(request['payload']))
                self.prepare(request,'feedback' if result=='accepted' else 'feedback_'+result,FEEDBACK_THANKS_TEXT if result=='accepted' else None)
                return
            settings = self.db.settings()
            if action == "revoke":
                self.prepare(request,"consent_revoked","Согласие отозвано. Новые сообщения не будут передаваться ИИ. При необходимости выберите общение с сотрудником.",keys=self.consent_keyboard())
                return
            if action in ("operator","decline"):
                self.to_human(request,"consent_declined" if action=="decline" else "user_requested_operator")
                return
            if action == "agree":
                payload=json.loads(request['payload'])
                version = payload.get("version","")
                offer=self.db.consent_offer(peer)
                stale_text=payload.get('source')=='text' and (not offer or offer['id']!=payload.get('prompt_id'))
                if not consent_configured(settings) or version != settings["consent_version"] or stale_text:
                    self.prepare(request,"consent_required","Условия согласия обновлены или ещё не опубликованы.\n\n" + consent_message(settings),keys=self.consent_keyboard(settings))
                    return
                accepted=self.db.record_consent(peer,request["user_id"],"accepted",request["event_key"],text_snapshot=offer['reply'] if offer else None,expected_version=version)
                if not accepted:
                    settings=self.db.settings()
                    self.prepare(request,'consent_required',consent_message(settings),keys=self.consent_keyboard(settings))
                    return
                message = "✅ Согласие сохранено.\n\nНапишите ваш вопрос или отправьте фотографию.\n\n/оператор: обратиться к сотруднику\n/отозвать: отозвать согласие"
                if self.db.conversation(peer)["human_mode"]:
                    message = "Согласие сохранено. Ваш диалог остаётся у сотрудника до завершения обращения."
                elif self.db.conversation(peer)['bot_paused_until']>time.time():
                    message = 'Согласие сохранено. Автоматические ответы временно отключены. При необходимости напишите /оператор.'
                self.prepare(request,"consent_accepted",message)
                return
            conversation = self.db.conversation(peer)
            if conversation["human_mode"]:
                self.prepare(request,"human_mode",reason="awaiting_operator")
                return
            if conversation['bot_paused_until']>time.time():
                self.prepare(request,'bot_paused',reason='bot_paused')
                return
            protection=Protection(self.db)
            if json.loads(request['payload'] or '{}').get('input_too_long'):
                self.prepare(request,'input_rejected',TOO_LONG_TEXT if protection.notice(peer,'message_size') else None,reason='message_size')
                return
            if json.loads(request['payload'] or '{}').get('local_intent')=='farewell' and conversation['consent_state']!='unknown':
                self.db.execute('UPDATE conversations SET clarification_count=0 WHERE peer_id=?',(peer,))
                self.prepare(request,'farewell',FAREWELL_TEXT,generation=conversation['generation'])
                return
            if json.loads(request['payload'] or '{}').get('local_intent')=='thanks' and conversation['consent_state']!='unknown':
                self.thank(request,conversation['generation'])
                return
            if not self.db.allowed(peer):
                if not consent_configured(settings):
                    self.to_human(request,"consent_not_configured")
                else:
                    self.prepare(request,"consent_required",consent_message(settings),keys=self.consent_keyboard(settings))
                return
            if json.loads(request['payload'] or '{}').get('local_intent')=='greeting':
                self.prepare(request,'greeting',GREETING_PROMPT,generation=conversation['generation'])
                return
            if instruction_attack(request['text']):
                self.prepare(request,'input_rejected',UNSAFE_TEXT if protection.notice(peer,'instruction_attempt') else None,reason='instruction_attempt')
                return
            if not json.loads(request['attachments']):
                query = (json.loads(request['flight_query']) if request.get('flight_query') and request['reason']=='flight_refresh'
                         else parse_query(request['text'], previous_query(self.db, request, conversation['generation'])))
                if query is not None:
                    result = answer_query(query, self.flight_board)
                    if not self.db.allowed(peer, conversation['generation']) or not consent_configured(self.db.settings()):
                        self.prepare(request, 'cancelled', reason='state_changed_before_send')
                        return
                    self.db.execute('UPDATE requests SET flight_query=?,flight_valid_until=? WHERE id=?',
                        (json.dumps(result.query), result.valid_until, request['id']))
                    self.db.execute('UPDATE conversations SET clarification_count=0 WHERE peer_id=?', (peer,))
                    self.prepare(request, result.outcome, result.text, reason=result.reason,
                        decision={'topic':'Рейсы','intent':'flight_board'}, generation=conversation['generation'])
                    return
            if not self.config.external_user_data:
                self.to_human(request,"external_processing_not_enabled")
                return
            limit=protection.reserve_ai(peer)
            if limit:
                self.prepare(request,'rate_limited',AI_LIMIT_TEXT if protection.notice(peer,limit) else None,reason=limit)
                return
            cards = self.db.cards(eligible=True)
            if not cards:
                self.to_human(request,"no_published_cards")
                return
            generation = conversation["generation"]
            guard = lambda: self.db.allowed(peer,generation) and consent_configured(self.db.settings()) and self.db.one("SELECT 1 FROM requests WHERE id=? AND status IN ('pending','processing')",(request['id'],)) is not None
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
                if strategy != "full" and needs_full_catalog(decision) and len(candidates) < len(cards):
                    for key in usage:
                        usage[key] += int(decision.get("usage",{}).get(key,0))
                    for card in cards:
                        card["rendered_answer"] = self.db.render_answer(card)
                    candidates = cards
                    decision = self.ai.route(request["text"],candidates,history,descriptions,guard)
            if descriptions and decision['action']=='handoff' and decision['kind']!='thanks' and decision.get('handoff_reason') not in STAFF_REASONS and conversation['clarification_count']<int(settings['max_clarifications']):
                # Для фото сначала выясняем вопрос, если модель лишь не нашла ответ.
                for key in usage:
                    usage[key] += int(decision.get("usage",{}).get(key,0))
                decision=self.ai.clarify_photo(request['text'],descriptions,history,guard)
            for key in usage:
                usage[key] += int(decision.get("usage",{}).get(key,0))
            if not guard():
                raise AccessStopped("Диалог изменён во время обработки")
            card = next((c for c in candidates if c["id"] == decision.get("card_id")),None)
            if decision['action']=='acknowledge' and decision['kind']=='thanks':
                self.thank(request,generation,usage)
            elif reason := handoff_reason(decision,card):
                self.to_human(request,reason,decision,usage,card)
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
        if self.db.conversation(request['peer_id'])['bot_paused_until']>time.time() and request['outcome'] not in ('consent_revoked','consent_accepted','feedback','handoff'):
            self.db.execute("UPDATE requests SET status='cancelled',reply=NULL,reason='bot_paused' WHERE id=?",(request['id'],))
            return True
        if request['outcome'] in ('thanks','greeting','farewell'):
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
                    rendered = self.db.render_answer(card)
                    # The saved prefix must not mask a changed card or resource.
                    body=request['reply']
                    prefix=request['intro_text']+'\n\n' if request.get('intro_text') else ''
                    stale_card=bool(prefix and not body.startswith(prefix))
                    if prefix and body.startswith(prefix):
                        body=body[len(prefix):]
                    stale_card=stale_card or body not in (rendered, without_opening_greeting(rendered))
                except ValueError:
                    stale_card = True
            if not self.db.allowed(request["peer_id"],request["ai_generation"]) or not consent_configured(self.db.settings()) or stale_card:
                self.db.execute("UPDATE requests SET status='cancelled',reason='state_changed_before_send' WHERE id=?",(request["id"],))
                self.db.handoff(request,"state_changed_before_send")
                transport.mark_human(request["peer_id"])
                return True
        try:
            if request.get('flight_valid_until') and time.time() >= request['flight_valid_until']:
                # A queued/retried VK message must refresh operational data before sending.
                self.db.execute("UPDATE requests SET status='pending',reply=NULL,reason='flight_refresh',flight_valid_until=NULL WHERE id=?", (request['id'],))
                return True
            transport.send(request["peer_id"],request["reply"],request["keyboard"],request["random_id"])
        except Exception:
            attempts = request["send_attempts"]+1
            self.db.execute("UPDATE requests SET status=?,send_attempts=?,error='Ошибка отправки VK',updated_at=? WHERE id=?",("failed" if attempts>=3 else "ready",attempts,time.time(),request["id"]))
            if attempts >= 3 and not request['outcome'].startswith('feedback'):
                self.db.handoff(request,"delivery_failed")
                transport.mark_human(request["peer_id"])
        else:
            Feedback(self.db).record_bot_delivery(request)
            if request["outcome"] == "handoff":
                transport.mark_human(request["peer_id"])
        return True
