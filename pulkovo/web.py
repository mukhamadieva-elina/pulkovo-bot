import csv
import hashlib
import hmac
import io
import json
import re
import secrets
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, abort, flash, redirect, render_template, request, session, url_for, send_file
from itsdangerous import BadSignature, URLSafeSerializer

from .ai import AIError, GigaChat
from .db import Database
from .dialogue_rules import is_thanks
from .consent import consent_message, valid_consent_url
from .importer import import_workbook, inspect_workbook, template_workbook
from .search import searcher
from .resources import display_answer, resource_keys, answer_parts
from .topics import topic_label
from .operator import OperatorInbox
from .attachments import safe_image_url, IMAGE_DOMAINS, ATTACHMENT_LABELS

LABELS = {"answer":"Готовый ответ","clarify":"Уточнение ИИ","handoff":"Передано сотруднику","human_mode":"Ожидает сотрудника","consent_required":"Ожидает согласия","consent_accepted":"Согласие получено","consent_revoked":"Согласие отозвано","pending":"В очереди","processing":"Обработка","ready":"К отправке","sending":"Отправка","sent":"Отправлено","failed":"Ошибка отправки","cancelled":"Отменено","done":"Обработано","draft":"Черновик","published":"Опубликована","disabled":"Отключена","public":"Публичная","internal":"Для сотрудников","any":"Всегда","normal":"Обычный режим","cover":"КОВЁР","open":"Ожидает","closed":"Закрыто","complaint":"Жалоба","no_matching_answer":"Ответ не найден","user_requested_operator":"Запрос сотрудника","consent_declined":"Отказ от ИИ","consent_not_configured":"Согласие ещё не настроено","external_processing_not_enabled":"Передача в ИИ выключена","no_published_cards":"Нет опубликованных ответов","processing_error":"Ошибка обработки","clarification_limit":"Лимит уточнений","unsupported_attachment":"Неподдерживаемое вложение"}


LABELS.update({"delivery_failed":"Не удалось отправить ответ", "state_changed_before_send":"Ответ отменён после изменения настроек или карточки", "empty_message":"Нет текста или поддерживаемого изображения", "accepted":"Получено", "declined":"Отказ", "revoked":"Отозвано", "unknown":"Не получено", "awaiting_operator":"Ожидает сотрудника"})
LABELS.update({'waiting_user':'Ждём пользователя','staff_joined':'Ответил сотрудник'})
LABELS.update({'thanks':'Благодарность','acknowledge':'Ответ на благодарность','bot':'У бота'})
LABELS.update({'inactivity':'Закрыто по неактивности','manual':'Завершено сотрудником'})
LABELS.update({'normal':'Без ограничений полётов','cover':'При ограничениях полётов (КОВЁР)'})


def create_app(config, database=None):
    if not config.secret_key or not config.admin_password:
        raise RuntimeError("Сначала выполните python -m pulkovo.cli init-config")
    app = Flask(__name__)
    app.config.update(SECRET_KEY=config.secret_key,MAX_CONTENT_LENGTH=11*1024*1024,SESSION_COOKIE_HTTPONLY=True,SESSION_COOKIE_SAMESITE="Strict",PERMANENT_SESSION_LIFETIME=timedelta(hours=8))
    db = database or Database(config.database)
    inbox = OperatorInbox(db)
    app.extensions["database"] = db
    editor_signer = URLSafeSerializer(config.secret_key, salt='answer-editor-resources')
    app.add_template_filter(safe_image_url,'vk_image_url')
    app.add_template_filter(topic_label,'topic_label')
    app.add_template_filter(lambda kind: ATTACHMENT_LABELS.get(kind,'Вложение'),'attachment_label')

    @app.before_request
    def protect():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        if request.method == "POST" and not hmac.compare_digest(request.form.get("csrf",""),session["csrf"]):
            abort(400,"Некорректный код формы. Обновите страницу.")
        if request.endpoint not in ("login","static") and not session.get("authenticated"):
            return redirect(url_for("login"))

    @app.after_request
    def headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Cache-Control"] = "no-store"
        image_sources=' '.join(f'https://{domain} https://*.{domain}' for domain in IMAGE_DOMAINS)
        response.headers["Content-Security-Policy"] = f"default-src 'self'; style-src 'self'; img-src 'self' data: {image_sources}; form-action 'self'; frame-ancestors 'none'; base-uri 'self'"
        return response

    @app.template_filter("label")
    def label(value):
        return LABELS.get(value,value or "—")

    @app.template_filter("date")
    def date(value):
        return datetime.fromtimestamp(value,ZoneInfo("Europe/Moscow")).strftime("%d.%m %H:%M") if value else "—"

    @app.template_filter("jsonlist")
    def jsonlist(value):
        return json.loads(value or "[]")

    @app.context_processor
    def common():
        return dict(csrf=session.get("csrf"),queue_count=db.one("SELECT count(*) n FROM tickets WHERE status='open'")["n"],group_id=config.group_id,settings=db.settings())

    @app.route("/login",methods=["GET","POST"])
    def login():
        if request.method == "POST":
            address = request.remote_addr or "local"
            row = db.one("SELECT * FROM login_attempts WHERE address=?",(address,))
            if row and row["attempts"]>=8 and time.time()-row["since"]<300:
                abort(429,"Слишком много попыток. Повторите через 5 минут.")
            if hmac.compare_digest(request.form.get("password","").encode(),config.admin_password.encode()):
                session.clear()
                session.update(authenticated=True,csrf=secrets.token_urlsafe(32),staff_name="Сотрудник")
                session.permanent = True
                db.execute("DELETE FROM login_attempts WHERE address=?",(address,))
                return redirect(url_for("dashboard"))
            count = row["attempts"]+1 if row and time.time()-row["since"]<300 else 1
            since = row["since"] if count>1 else time.time()
            db.execute("INSERT INTO login_attempts VALUES (?,?,?) ON CONFLICT(address) DO UPDATE SET attempts=excluded.attempts,since=excluded.since",(address,count,since))
            flash("Неверный пароль.","error")
        return render_template("login.html",title="Вход")

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    def dashboard():
        counts = {r["status"]:r["n"] for r in db.all("SELECT status,count(*) n FROM cards GROUP BY status")}
        tickets = db.dialogue_rows('open',limit=8)
        return render_template("dashboard.html",title="Обзор",counts=counts,tickets=tickets,external=config.external_user_data,migrations=db.all("SELECT version,applied_at FROM schema_migrations ORDER BY version"))

    @app.get("/cards")
    def cards():
        q=request.args.get("q","").strip()
        state=request.args.get("status","")
        topic=topic_label(request.args.get("topic",""))
        rows=db.cards()
        topics=sorted({topic_label(r["topic"]) for r in rows})
        rows=[r for r in rows if (not q or q.lower() in (r["title"]+r["answer"]+r["topic"]+topic_label(r["topic"])).lower()) and (not state or r["status"]==state) and (not topic or topic_label(r["topic"])==topic)]
        return render_template("cards.html",title="База знаний",cards=rows,q=q,state=state,topic=topic,topics=topics)

    @app.route("/cards/new",methods=["GET","POST"])
    @app.route("/cards/<int:card_id>",methods=["GET","POST"])
    def card_edit(card_id=None):
        card = db.one("SELECT * FROM cards WHERE id=?",(card_id,)) if card_id else dict(id=None,version=0,topic="",title="",answer="",question_variants="[]",conditions="",mode="any",action="answer",visibility="public",status="draft",reviewed=0,issues="[]",source="")
        if not card:
            abort(404)
        resources = db.all("SELECT * FROM resources WHERE visibility='public' ORDER BY label,value")
        resource_values = {r['key']:r['value'] for r in resources}
        editor_answer = display_answer(card['answer'], resource_values)
        resource_snapshot = editor_signer.dumps(dict(card_id=card_id,version=card['version'],resources=resource_values))
        if request.method=="POST":
            data = {k:request.form.get(k,"").strip() for k in ("topic","title","answer","conditions")}
            if data['topic'] == topic_label(card['topic']):
                # Обычное сохранение ответа не меняет исходную тему для поиска.
                data['topic'] = card['topic']
            intent = request.form.get('intent')
            data.update(mode=request.form.get('mode',card['mode']),action=request.form.get('action',card['action']),visibility=card['visibility'],status='published' if intent=='publish' else 'draft',reviewed=int(intent=='publish'),question_variants=[v.strip() for v in request.form.get("variants","").splitlines() if v.strip()],issues=card["issues"],source=card["source"])
            editor_answer = display_answer(data['answer'], resource_values)
            try:
                version = int(request.form.get('version','0'))
                try:
                    snapshot = editor_signer.loads(request.form.get('resource_snapshot',''))
                except BadSignature:
                    raise ValueError('Форма устарела. Скопируйте изменения и обновите страницу перед сохранением.') from None
                if snapshot['card_id']!=card_id or snapshot['version']!=version:
                    raise ValueError('Форма устарела. Обновите страницу перед сохранением.')
                if intent not in ('publish','draft') or data["mode"] not in ("any","normal","cover") or data["action"] not in ("answer","handoff"):
                    raise ValueError("Недопустимые настройки карточки")
                if intent=='publish' and card['visibility']!='public':
                    raise ValueError('Служебная запись не предназначена для ответов пассажирам.')
                saved=db.save_card(data,card_id,version,editor_resources=snapshot['resources'])
                flash('Ответ опубликован: бот может использовать его.' if intent=='publish' else 'Черновик сохранён. Бот его не использует.')
                return redirect(url_for("card_edit",card_id=saved))
            except ValueError as exc:
                flash(str(exc),"error")
                card.update(data)
                card['version']=request.form.get('version','0')
                resource_snapshot=request.form.get('resource_snapshot','')
                card["question_variants"]=json.dumps(data["question_variants"],ensure_ascii=False)
        versions=db.all("SELECT * FROM card_versions WHERE card_id=? ORDER BY version DESC",(card_id,)) if card_id else []
        for version in versions:
            version['card'] = json.loads(version['snapshot'])
            version['answer'] = display_answer(version['card']['answer'],resource_values)
        topics=sorted({topic_label(r['topic']) for r in db.all("SELECT DISTINCT topic FROM cards WHERE topic!=''")})
        return render_template("card_edit.html",title="Карточка ответа",card=card,versions=versions,resources=resources,topics=topics,editor_answer=editor_answer,editor_parts=answer_parts(card['answer'],resources),resource_snapshot=resource_snapshot)

    @app.post("/cards/<int:card_id>/restore/<int:version>")
    def restore(card_id,version):
        old=db.one("SELECT snapshot FROM card_versions WHERE card_id=? AND version=?",(card_id,version))
        current=db.one("SELECT * FROM cards WHERE id=?",(card_id,))
        if not old or not current:
            abort(404)
        data=json.loads(old["snapshot"])
        data.update(status="draft",reviewed=0)
        db.save_card(data,card_id,current["version"])
        flash("Версия восстановлена как новый черновик.")
        return redirect(url_for("card_edit",card_id=card_id))

    @app.route("/import",methods=["GET","POST"])
    def import_page():
        if request.method=="POST":
            file=request.files.get("file")
            if not file or not file.filename.lower().endswith(".xlsx"):
                flash("Выберите файл .xlsx.","error")
            else:
                try:
                    raw=file.read()
                    filename=Path(file.filename).name
                    preview=inspect_workbook(raw,filename)
                    existing={c['source_key'] for c in db.cards()}
                    for card in preview:
                        card['already_exists']=card['source_key'] in existing
                    folder=config.database.parent/"imports"
                    folder.mkdir(exist_ok=True)
                    name=secrets.token_hex(16)+".xlsx"
                    (folder/name).write_bytes(raw)
                    (folder/name).chmod(0o600)
                    session["import_file"]=[name,filename]
                    return render_template("import.html",title="Проверка импорта",preview=preview)
                except ValueError as exc:
                    flash(str(exc), 'error')
                except Exception:
                    flash("Не удалось прочитать таблицу. Проверьте формат, листы и отсутствие формул.","error")
        return render_template("import.html",title="Импорт Excel",preview=None,reports=db.all("SELECT * FROM import_batches ORDER BY id DESC LIMIT 10"))

    @app.get('/import/template.xlsx')
    def import_template():
        return send_file(io.BytesIO(template_workbook()),mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',as_attachment=True,download_name='knowledge-base-template.xlsx')

    @app.post("/import/commit")
    def import_commit():
        stored=session.pop("import_file",None)
        if not stored:
            abort(400)
        path=config.database.parent/"imports"/stored[0]
        try:
            report=import_workbook(db,path.read_bytes(),stored[1])
            flash(f"Добавлено: {report['added']}. Уже существующих: {report['skipped_existing']}. Все новые карточки — черновики.")
        finally:
            path.unlink(missing_ok=True)
        return redirect(url_for("cards"))

    @app.route("/resources",methods=["GET","POST"])
    def resources():
        if request.method=="POST":
            key=request.form.get("key","")
            value=request.form.get("value","").strip()
            name=request.form.get('label','').strip()
            if not key:
                key='contact_'+secrets.token_hex(12)
            if request.form.get('visibility','public') != 'public':
                flash("Форма устарела. Обновите страницу: здесь сохраняются только значения для ответов бота.","error")
            elif not re.fullmatch(r"[a-z][a-z0-9_]{1,80}",key) or not value or len(value)>2000 or len(name)>250:
                flash("Укажите ссылку или контакт (до 2000 символов) и название до 250 символов.","error")
            else:
                with db.connect() as con:
                    con.execute('BEGIN IMMEDIATE')
                    old=con.execute('SELECT label FROM resources WHERE key=?',(key,)).fetchone()
                    name=name or (old['label'] if old else '') or value
                    saved=con.execute("INSERT INTO resources(key,value,visibility,label) VALUES (?,?,'public',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,label=excluded.label WHERE resources.visibility='public' RETURNING key",(key,value,name)).fetchone()
                    if saved:
                        con.execute("INSERT INTO audit(action,target,created_at) VALUES ('resource_changed',?,?)",(key,time.time()))
                        flash("Сохранено. Ссылка или контакт обновлены во всех связанных ответах.")
                    else:
                        flash("Эта запись недоступна для изменения. Обновите страницу.","error")
            return redirect(url_for("resources"))
        resources=db.all("SELECT * FROM resources WHERE visibility='public' ORDER BY label,value")
        usage={r['key']:[] for r in resources}
        for card in db.all("SELECT id,title,status,answer FROM cards WHERE visibility='public' ORDER BY title,id"):
            for key in resource_keys(card['answer']):
                if key in usage:
                    usage[key].append(card)
        return render_template("resources.html",title="Ссылки и контакты",resources=resources,usage=usage)

    def queue_revision(state):
        rows=db.dialogue_rows(state)
        count=db.one("SELECT count(*) n FROM tickets WHERE status='open'")['n']
        return hashlib.sha256(json.dumps([rows,count],sort_keys=True).encode()).hexdigest()

    @app.get('/queue/state')
    def queue_state():
        state=request.args.get('state','open')
        if state not in ('open','waiting_user','all','closed'):
            abort(400)
        if state=='closed':
            state='all'
        return dict(revision=queue_revision(state))

    @app.get("/queue")
    def queue():
        state=request.args.get("state","open")
        if state=='closed':
            return redirect(url_for('queue',state='all'))
        state=state if state in ('open','waiting_user','all') else 'open'
        revision=queue_revision(state)
        rows=db.dialogue_rows(state)
        return render_template("queue.html",title="Диалоги",tickets=rows,state=state,queue_revision=revision)

    def selected_ticket(peer_id):
        value=request.args.get('ticket')
        if value is None:
            return None
        if not value.isdecimal() or len(value)>18 or int(value)<=0:
            abort(400)
        ticket=db.ticket_details(peer_id,int(value))
        if not ticket:
            abort(404)
        return ticket

    @app.route("/dialogue/<int:peer_id>",methods=["GET","POST"])
    def dialogue(peer_id):
        c=db.one("SELECT c.*,trim(coalesce(p.first_name,'')||' '||coalesce(p.last_name,'')) user_name FROM conversations c LEFT JOIN vk_profiles p ON p.user_id=c.user_id WHERE c.peer_id=?",(peer_id,))
        if not c:
            abort(404)
        selected=selected_ticket(peer_id)
        if selected and request.method=='GET':
            return redirect(url_for('dialogue',peer_id=peer_id))
        ticket_id=selected['id'] if selected else None
        readonly=bool(selected and selected['status']=='closed')
        draft=''
        form_key=secrets.token_urlsafe(24)
        if request.method=="POST":
            action=request.form.get("action")
            if readonly and action!='topic':
                abort(409,'Обращение завершено. Для нового ответа откройте текущий диалог.')
            if selected and action=='topic' and request.form.get('ticket_id')!=str(ticket_id):
                abort(400)
            try:
                if action=='reply':
                    draft=request.form.get('reply','')
                    name=request.form.get('staff_name','').strip()[:80] or 'Сотрудник'
                    session['staff_name']=name
                    form_key=request.form.get('client_key','')
                    inbox.queue(peer_id,draft,name,form_key,request.form.get('revision',''))
                    flash('Ответ поставлен в очередь отправки. Подтверждение появится в переписке.')
                elif action in ('retry','cancel'):
                    inbox.control(peer_id,int(request.form.get('outbox_id','0')),action)
                    flash('Повтор отправки запланирован.' if action=='retry' else 'Неотправленный ответ отменён.')
                elif action=="resume":
                    db.resume(peer_id)
                    flash("Управление возвращено боту. Он ответит на следующее новое сообщение.")
                elif action=="take":
                    name=request.form.get("staff_name","").strip()[:80] or "Сотрудник"
                    session["staff_name"]=name
                    db.execute("UPDATE tickets SET assigned_to=?,taken_at=? WHERE peer_id=? AND status IN ('open','waiting_user') AND assigned_to=''",(name,time.time(),peer_id))
                    flash("Состояние очереди обновлено.")
                elif action=="note":
                    db.execute("UPDATE tickets SET note=? WHERE peer_id=? AND status IN ('open','waiting_user')",(request.form.get("note","")[:2000],peer_id))
                    flash("Заметка сохранена.")
                elif action=='topic':
                    db.set_ticket_topic(peer_id,int(request.form.get('ticket_id','0')),request.form.get('topic',''),int(request.form.get('topic_version','0')))
                    flash('Тема обращения сохранена.')
                elif action=='auto_close':
                    target_id=int(request.form.get('ticket_id','0'))
                    if selected and target_id!=ticket_id:
                        abort(400)
                    db.pause_auto_close(peer_id,target_id,request.form.get('paused')=='1')
                    flash('Диалог оставлен у сотрудника без срока.' if request.form.get('paused')=='1' else 'Возврат боту по неактивности включен. Срок считается от последнего ответа сотрудника.')
                return redirect(url_for("dialogue",peer_id=peer_id,ticket=ticket_id))
            except ValueError as exc:
                flash(str(exc),'error')
                if action!='reply':
                    return redirect(url_for('dialogue',peer_id=peer_id,ticket=ticket_id))
        tickets=db.all("SELECT * FROM tickets WHERE peer_id=? AND status IN ('open','waiting_user') ORDER BY id DESC",(peer_id,))
        revision=inbox.revision(peer_id)
        topics=sorted({topic_label(row['topic']) for row in db.all("SELECT DISTINCT topic FROM cards WHERE topic!=''")})
        return render_template("dialogue.html",title=c['user_name'] or f"Диалог {peer_id}",conversation=c,tickets=tickets,messages=inbox.timeline(peer_id,limit=30),revision=revision,draft=draft,form_key=form_key,peer_id=peer_id,topics=topics,readonly=False)

    @app.get('/dialogue/<int:peer_id>/updates')
    def dialogue_updates(peer_id):
        c=db.one('SELECT * FROM conversations WHERE peer_id=?',(peer_id,))
        if not c:
            abort(404)
        selected_ticket(peer_id)
        revision=inbox.revision(peer_id)
        if revision==request.args.get('revision'):
            return dict(revision=revision)
        active=db.one("SELECT status FROM tickets WHERE peer_id=? AND status IN ('open','waiting_user')",(peer_id,))
        return dict(revision=revision,html=render_template('_messages.html',messages=inbox.timeline(peer_id,limit=30),peer_id=peer_id,readonly=False),status=label(active['status']) if active else 'У бота')

    @app.get("/statistics")
    def statistics():
        days=min(365,max(1,request.args.get("days",30,type=int)))
        cutoff=time.time()-days*86400
        outcomes=db.all("SELECT outcome,status,count(*) n FROM requests WHERE created_at>=? GROUP BY outcome,status ORDER BY n DESC",(cutoff,))
        totals=db.one("SELECT count(*) total,sum(CASE WHEN outcome='answer' AND status='sent' THEN 1 ELSE 0 END) answers,sum(CASE WHEN outcome='handoff' THEN 1 ELSE 0 END) handoffs,sum(prompt_tokens+completion_tokens) tokens,avg(duration_ms) latency FROM requests WHERE created_at>=?",(cutoff,))
        counts=Counter()
        for row in db.all("SELECT topic,count(*) n FROM requests WHERE created_at>=? AND outcome IN ('answer','clarify','handoff') GROUP BY topic",(cutoff,)):
            counts[topic_label(row['topic']) or 'Тема не указана']+=row['n']
        topics=[dict(topic=topic,n=n) for topic,n in sorted(counts.items(),key=lambda pair:(-pair[1],pair[0]))]
        closures=db.one("SELECT sum(closure_reason='inactivity') inactivity,sum(closure_reason='manual') manual FROM tickets WHERE status='closed' AND closed_at>=?",(cutoff,))
        return render_template("statistics.html",title="Статистика",totals=totals,outcomes=outcomes,topics=topics,days=days,closures=closures)

    @app.get('/tickets.csv')
    def export_tickets():
        days=min(365,max(1,request.args.get('days',30,type=int)))
        output=io.StringIO()
        writer=csv.writer(output)
        writer.writerow(['ID записи','VK ID','Тема','Дата возврата UTC','Причина возврата'])
        for row in db.all("SELECT * FROM tickets WHERE status='closed' AND closed_at>=? ORDER BY closed_at",(time.time()-days*86400,)):
            values=[row['id'],row['peer_id'],topic_label(row['topic']),datetime.fromtimestamp(row['closed_at'],ZoneInfo('UTC')).isoformat(),label(row['closure_reason'])]
            writer.writerow(["'"+v if isinstance(v,str) and v.startswith(('=','+','-','@')) else v for v in values])
        return app.response_class('\ufeff'+output.getvalue(),mimetype='text/csv',headers={'Content-Disposition':'attachment; filename=closed-tickets.csv'})

    @app.get("/statistics.csv")
    def export_stats():
        output=io.StringIO()
        writer=csv.writer(output)
        writer.writerow(["Дата UTC","Тема","Результат","Доставка","Тип сообщения","Токены","Время мс"])
        for r in db.all("SELECT created_at,topic,outcome,status,input_type,prompt_tokens+completion_tokens tokens,duration_ms FROM requests ORDER BY id"):
            values=[datetime.fromtimestamp(r["created_at"],ZoneInfo("UTC")).isoformat(),topic_label(r["topic"]),r["outcome"],r["status"],r["input_type"],r["tokens"],r["duration_ms"]]
            writer.writerow(["'"+v if isinstance(v,str) and v.startswith(("=","+","-","@")) else v for v in values])
        return app.response_class("\ufeff"+output.getvalue(),mimetype="text/csv",headers={"Content-Disposition":"attachment; filename=statistics.csv"})

    @app.route("/settings",methods=["GET","POST"])
    def settings_page():
        if request.method=="POST":
            old=db.settings()
            values={key:request.form.get(key,old[key]).strip() for key in ("max_clarifications","consent_url","consent_text","handoff_text","retention_days","waiting_user_timeout_hours")}
            values.update({key:old[key] for key in ('search_strategy','search_limit','consent_version')})
            values.update(cover_mode=str(request.form.get("cover_mode")=="on").lower(),consent_ready=str(request.form.get("consent_ready")=="on").lower())
            try:
                if not 0<=int(values["max_clarifications"])<=5 or not 1<=int(values["retention_days"])<=365:
                    raise ValueError("Проверьте число уточнений и срок хранения.")
                if not 0<=int(values['waiting_user_timeout_hours'])<=720:
                    raise ValueError('Срок ожидания должен быть от 1 до 720 часов, либо 0 для отключения.')
                if not values["consent_version"] or not values["consent_text"] or not values["handoff_text"]:
                    raise ValueError("Заполните тексты и версию согласия.")
                if not valid_consent_url(values["consent_url"]):
                    raise ValueError("Ссылка необязательна. Если заполняете её, укажите HTTPS-адрес документа.")
                if len(consent_message(values)) > 3500:
                    raise ValueError("Текст согласия вместе со ссылкой должен быть не длиннее 3500 символов.")
                if any(values[k]!=old[k] for k in ("consent_text","consent_url")):
                    values['consent_version']='consent-'+str(time.time_ns())
                db.set_settings(values)
                db.execute("INSERT INTO audit(action,target,created_at) VALUES ('settings_changed','settings',?)",(time.time(),))
                flash("Настройки сохранены.")
            except ValueError as exc:
                flash(str(exc),"error")
            return redirect(url_for("settings_page"))
        return render_template("settings.html",title="Настройки",external=config.external_user_data)

    @app.route("/preview",methods=["GET","POST"])
    def preview():
        text=request.form.get("text","")[:4000]
        selected=[]
        answer_card=None
        decision=None
        if request.method=="POST":
            candidates=db.cards(eligible=True)
            try:
                selected=searcher(db.setting('search_strategy'),db).select(text,candidates,int(db.setting("search_limit")))
                if request.form.get("action")=="ai":
                    if request.form.get("synthetic")!="on":
                        raise ValueError("Для теста используйте вымышленные данные и подтвердите это.")
                    if is_thanks(text):
                        decision=dict(action='acknowledge',kind='thanks',reason='Готовый ответ без вызова ИИ.')
                        return render_template("preview.html",title="Проверка ответа",text=text,decision=decision,answer_card=None)
                    if not selected:
                        raise ValueError("Сначала опубликуйте проверенные карточки.")
                    for card in selected:
                        card["rendered_answer"]=db.render_answer(card)
                    decision=GigaChat(config).route(text,selected)
                    if decision.get('kind')=='thanks':
                        decision['action']='acknowledge'
                    answer_card=next((c for c in selected if c['id']==decision.get('card_id')),None)
                    if decision.get('kind')=='complaint' or answer_card and answer_card['action']=='handoff':
                        decision['action']='handoff'
            except (AIError,ValueError,RuntimeError) as exc:
                flash(str(exc),"error")
        return render_template("preview.html",title="Проверка ответа",text=text,decision=decision,answer_card=answer_card)

    return app
