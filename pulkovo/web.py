import csv
import hashlib
import hmac
import io
import json
import re
import secrets
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.datastructures import FormData, UploadFile
from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.middleware.sessions import SessionMiddleware
from itsdangerous import BadSignature, URLSafeSerializer

from .ai import AIError, GigaChat
from .db import Database
from .dialogue_rules import GREETING_TEXT, THANKS_TEXT, is_greeting, is_thanks
from .consent import consent_message, valid_consent_url
from .importer import import_workbook, inspect_workbook, template_workbook
from .search import searcher
from .resources import display_answer, resource_keys, answer_parts
from .flights import parse_query, answer_query
from .topics import topic_label
from .operator import OperatorInbox
from .statistics import MOSCOW, PERIODS, automation_report, feedback_report, statistics_period
from .attachments import safe_image_url, IMAGE_DOMAINS, ATTACHMENT_LABELS
from .routing import handoff_reason, needs_full_catalog
from .protection import Protection

LABELS = {"answer":"Готовый ответ","clarify":"Уточнение","handoff":"Передано сотруднику","human_mode":"Ожидает сотрудника","consent_required":"Ожидает согласия","consent_accepted":"Согласие получено","consent_revoked":"Согласие отозвано","pending":"В очереди","processing":"Обработка","ready":"К отправке","sending":"Отправка","sent":"Отправлено","failed":"Ошибка отправки","cancelled":"Отменено","done":"Обработано","draft":"Черновик","published":"Опубликована","disabled":"Отключена","public":"Публичная","internal":"Для сотрудников","any":"Всегда","normal":"Обычный режим","cover":"КОВЁР","open":"Ожидает","closed":"Закрыто","complaint":"Жалоба","no_matching_answer":"Ответ не найден","user_requested_operator":"Запрос сотрудника","consent_declined":"Отказ от ИИ","consent_not_configured":"Согласие ещё не настроено","external_processing_not_enabled":"Передача в ИИ выключена","no_published_cards":"Нет опубликованных ответов","processing_error":"Ошибка обработки","clarification_limit":"Лимит уточнений","unsupported_attachment":"Неподдерживаемое вложение"}


LABELS.update({"delivery_failed":"Не удалось отправить ответ", "state_changed_before_send":"Ответ отменён после изменения настроек или карточки", "empty_message":"Нет текста или поддерживаемого изображения", "accepted":"Получено", "declined":"Отказ", "revoked":"Отозвано", "unknown":"Не получено", "awaiting_operator":"Ожидает сотрудника"})
LABELS.update({'waiting_user':'Ждём пользователя','staff_joined':'Ответил сотрудник'})
LABELS.update({'thanks':'Благодарность','acknowledge':'Ответ на благодарность','bot':'У бота'})
LABELS['greeting']='Приветствие'
LABELS.update({'feedback':'Оценка помощи бота','feedback_duplicate':'Оценка уже сохранена','feedback_invalid':'Недействительная оценка'})
LABELS.update({'inactivity':'Закрыто по неактивности','manual':'Завершено сотрудником'})
LABELS.update({'normal':'Без ограничений полётов','cover':'При ограничениях полётов (КОВЁР)'})
LABELS.update({'staff_action_required':'Нужно действие сотрудника','answer_not_helpful':'Готовый ответ не помог','unsafe_request':'Нужна помощь сотрудника по безопасности','out_of_scope':'Вопрос вне тематики','card_requires_staff':'Карточка требует участия сотрудника','bot_paused':'Автоответы отключены','rate_limited':'Сработал лимит сообщений','input_rejected':'Сообщение обработано защитой','message_rate':'Слишком частые сообщения','dialogue_backlog':'Много сообщений в ожидании','queue_capacity':'Очередь заполнена','message_size':'Слишком длинное сообщение','instruction_attempt':'Попытка изменить правила бота','user_ai_budget':'Лимит обработки пользователя','global_ai_budget':'Общий лимит обработки'})
LABELS.update({'flight_info':'Информация о табло','flight_board':'Ответ по табло','flight_details_needed':'Уточнение рейса','flight_unavailable':'Табло недоступно или устарело','flight_not_found':'Рейс не найден в табло','flight_outside_board':'Дата вне периода табло','flight_refresh':'Обновление данных рейса'})


def create_app(config, database=None):
    if not config.secret_key or not config.admin_password:
        raise RuntimeError("Сначала выполните python -m pulkovo.cli init-config")
    db = database or Database(config.database)
    inbox = OperatorInbox(db)
    app = FastAPI(title="Пулково: админка бота", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.database = db
    app.add_middleware(SessionMiddleware, secret_key=config.secret_key,
                       session_cookie='pulkovo_admin', max_age=8*3600, same_site='strict')
    app.add_middleware(RequestBodyLimitMiddleware, max_body_size=11*1024*1024)
    directory = Path(__file__).parent
    app.mount('/static', StaticFiles(directory=directory/'static'), name='static')
    templates = Jinja2Templates(directory=directory/'templates')
    editor_signer = URLSafeSerializer(config.secret_key, salt='answer-editor-resources')

    def abort(status, detail=None):
        raise HTTPException(status, detail)

    def redirect(location):
        return RedirectResponse(location, status_code=303)

    def flash(request, message, category='message'):
        request.session['flashes'] = request.session.get('flashes', []) + [(category, message)]

    def url_for(name, **values):
        values = {key: value for key, value in values.items() if value is not None}
        if name == 'static':
            return str(app.url_path_for(name, **values))
        for route in router.routes:
            if route.name != name:
                continue
            path_keys = set(getattr(route, 'param_convertors', {}))
            if not path_keys <= values.keys():
                continue
            path = app.url_path_for(name, **{key: values[key] for key in path_keys})
            query = {key: value for key, value in values.items() if key not in path_keys}
            return str(path) + ('?' + urlencode(query) if query else '')
        raise ValueError(f'Не найден маршрут: {name}')

    def label(value):
        return LABELS.get(value, value or '—')

    templates.env.filters.update(
        vk_image_url=safe_image_url, topic_label=topic_label,
        attachment_label=lambda kind: ATTACHMENT_LABELS.get(kind, 'Вложение'),
        label=label,
        date=lambda value: datetime.fromtimestamp(value, ZoneInfo('Europe/Moscow')).strftime('%d.%m %H:%M') if value else '—',
        jsonlist=lambda value: json.loads(value or '[]'),
    )

    def render(request, name, **context):
        common = dict(session=request.session, csrf=request.session.get('csrf'),
                      queue_count=db.one("SELECT count(*) n FROM tickets WHERE status='open'")['n'],
                      group_id=config.group_id, settings=db.settings(),
                      endpoint=request.scope['route'].name,now=time.time(),
                      flashes=request.session.pop('flashes', []), url_for=url_for)
        response=templates.TemplateResponse(request=request, name=name, context=common | context)
        if context.get('fragment'):
            response.headers['X-Dialogue-Fragment']='1'
        return response

    async def protect(request: Request):
        request.session.setdefault('csrf', secrets.token_urlsafe(32))
        request.state.form = FormData()
        try:
            if request.method == 'POST':
                request.state.form = await request.form(max_files=1, max_fields=100, max_part_size=1024*1024)
                for key, value in request.state.form.multi_items():
                    if not isinstance(value, str) and key != 'file':
                        abort(400, 'Некорректное поле формы.')
                token = request.state.form.get('csrf', '')
                if not isinstance(token, str) or not hmac.compare_digest(token.encode(), request.session['csrf'].encode()):
                    abort(400, 'Некорректный код формы. Обновите страницу.')
            if request.scope['route'].name != 'login' and not request.session.get('authenticated'):
                raise HTTPException(303, headers={'Location': url_for('login')})
            yield
        finally:
            await request.state.form.close()

    router = APIRouter(dependencies=[Depends(protect)])

    @app.middleware('http')
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Cache-Control'] = 'no-store'
        image_sources = ' '.join(f'https://{domain} https://*.{domain}' for domain in IMAGE_DOMAINS)
        response.headers['Content-Security-Policy'] = f"default-src 'self'; style-src 'self'; img-src 'self' data: {image_sources}; form-action 'self'; frame-ancestors 'none'; base-uri 'self'"
        return response

    @router.api_route("/login",methods=["GET","POST"])
    def login(request: Request):
        if request.method == "POST":
            address = (request.client.host if request.client else "local")
            row = db.one("SELECT * FROM login_attempts WHERE address=?",(address,))
            if row and row["attempts"]>=8 and time.time()-row["since"]<300:
                abort(429,"Слишком много попыток. Повторите через 5 минут.")
            if hmac.compare_digest(request.state.form.get("password","").encode(),config.admin_password.encode()):
                request.session.clear()
                request.session.update(authenticated=True,csrf=secrets.token_urlsafe(32),staff_name="Сотрудник")
                db.execute("DELETE FROM login_attempts WHERE address=?",(address,))
                return redirect(url_for("dashboard"))
            count = row["attempts"]+1 if row and time.time()-row["since"]<300 else 1
            since = row["since"] if count>1 else time.time()
            db.execute("INSERT INTO login_attempts VALUES (?,?,?) ON CONFLICT(address) DO UPDATE SET attempts=excluded.attempts,since=excluded.since",(address,count,since))
            flash(request, "Неверный пароль.","error")
        return render(request, "login.html",title="Вход")

    @router.post("/logout")
    def logout(request: Request):
        request.session.clear()
        return redirect(url_for("login"))

    @router.get("/")
    def dashboard(request: Request):
        counts = {r["status"]:r["n"] for r in db.all("SELECT status,count(*) n FROM cards GROUP BY status")}
        tickets = db.dialogue_rows('open',limit=8)
        return render(request, "dashboard.html",title="Обзор",counts=counts,tickets=tickets,external=config.external_user_data,migrations=db.all("SELECT version,applied_at FROM schema_migrations ORDER BY version"))

    @router.get("/cards")
    def cards(request: Request):
        q=request.query_params.get("q","").strip()
        state=request.query_params.get("status","")
        topic=topic_label(request.query_params.get("topic",""))
        rows=db.cards()
        topics=sorted({topic_label(r["topic"]) for r in rows})
        rows=[r for r in rows if (not q or q.lower() in (r["title"]+r["answer"]+r["topic"]+topic_label(r["topic"])).lower()) and (not state or r["status"]==state) and (not topic or topic_label(r["topic"])==topic)]
        return render(request, "cards.html",title="База знаний",cards=rows,q=q,state=state,topic=topic,topics=topics)

    @router.api_route("/cards/new",methods=["GET","POST"])
    def card_new(request: Request):
        return card_edit(request, None)

    @router.api_route("/cards/{card_id}",methods=["GET","POST"])
    def card_edit(request: Request, card_id: int):
        card = db.one("SELECT * FROM cards WHERE id=?",(card_id,)) if card_id else dict(id=None,version=0,topic="",title="",answer="",question_variants="[]",conditions="",mode="any",action="answer",visibility="public",status="draft",reviewed=0,issues="[]",source="")
        if not card:
            abort(404)
        resources = db.all("SELECT * FROM resources WHERE visibility='public' ORDER BY label,value")
        resource_values = {r['key']:r['value'] for r in resources}
        editor_answer = display_answer(card['answer'], resource_values)
        resource_snapshot = editor_signer.dumps(dict(card_id=card_id,version=card['version'],resources=resource_values))
        if request.method=="POST":
            data = {k:request.state.form.get(k,"").strip() for k in ("topic","title","answer","conditions")}
            if data['topic'] == topic_label(card['topic']):
                # Обычное сохранение ответа не меняет исходную тему для поиска.
                data['topic'] = card['topic']
            intent = request.state.form.get('intent')
            data.update(mode=request.state.form.get('mode',card['mode']),action=request.state.form.get('action',card['action']),visibility=card['visibility'],status='published' if intent=='publish' else 'draft',reviewed=int(intent=='publish'),question_variants=[v.strip() for v in request.state.form.get("variants","").splitlines() if v.strip()],issues=card["issues"],source=card["source"])
            editor_answer = display_answer(data['answer'], resource_values)
            try:
                version = int(request.state.form.get('version','0'))
                try:
                    snapshot = editor_signer.loads(request.state.form.get('resource_snapshot',''))
                except BadSignature:
                    raise ValueError('Форма устарела. Скопируйте изменения и обновите страницу перед сохранением.') from None
                if snapshot['card_id']!=card_id or snapshot['version']!=version:
                    raise ValueError('Форма устарела. Обновите страницу перед сохранением.')
                if intent not in ('publish','draft') or data["mode"] not in ("any","normal","cover") or data["action"] not in ("answer","handoff"):
                    raise ValueError("Недопустимые настройки карточки")
                if intent=='publish' and card['visibility']!='public':
                    raise ValueError('Служебная запись не предназначена для ответов пассажирам.')
                saved=db.save_card(data,card_id,version,editor_resources=snapshot['resources'])
                flash(request, 'Ответ опубликован: бот может использовать его.' if intent=='publish' else 'Черновик сохранён. Бот его не использует.')
                return redirect(url_for("card_edit",card_id=saved))
            except ValueError as exc:
                flash(request, str(exc),"error")
                card.update(data)
                card['version']=request.state.form.get('version','0')
                resource_snapshot=request.state.form.get('resource_snapshot','')
                card["question_variants"]=json.dumps(data["question_variants"],ensure_ascii=False)
        versions=db.all("SELECT * FROM card_versions WHERE card_id=? ORDER BY version DESC",(card_id,)) if card_id else []
        for version in versions:
            version['card'] = json.loads(version['snapshot'])
            version['answer'] = display_answer(version['card']['answer'],resource_values)
        topics=sorted({topic_label(r['topic']) for r in db.all("SELECT DISTINCT topic FROM cards WHERE topic!=''")})
        return render(request, "card_edit.html",title="Карточка ответа",card=card,versions=versions,resources=resources,topics=topics,editor_answer=editor_answer,editor_parts=answer_parts(card['answer'],resources),resource_snapshot=resource_snapshot)

    @router.post("/cards/{card_id}/restore/{version}")
    def restore(request: Request, card_id: int, version: int):
        old=db.one("SELECT snapshot FROM card_versions WHERE card_id=? AND version=?",(card_id,version))
        current=db.one("SELECT * FROM cards WHERE id=?",(card_id,))
        if not old or not current:
            abort(404)
        data=json.loads(old["snapshot"])
        data.update(status="draft",reviewed=0)
        db.save_card(data,card_id,current["version"])
        flash(request, "Версия восстановлена как новый черновик.")
        return redirect(url_for("card_edit",card_id=card_id))

    @router.api_route("/import",methods=["GET","POST"])
    def import_page(request: Request):
        if request.method=="POST":
            file=request.state.form.get("file")
            if not isinstance(file, UploadFile) or not (file.filename or "").lower().endswith(".xlsx"):
                flash(request, "Выберите файл .xlsx.","error")
            else:
                try:
                    raw=file.file.read()
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
                    request.session["import_file"]=[name,filename]
                    return render(request, "import.html",title="Проверка импорта",preview=preview)
                except ValueError as exc:
                    flash(request, str(exc), 'error')
                except Exception:
                    flash(request, "Не удалось прочитать таблицу. Проверьте формат, листы и отсутствие формул.","error")
        return render(request, "import.html",title="Импорт Excel",preview=None,reports=db.all("SELECT * FROM import_batches ORDER BY id DESC LIMIT 10"))

    @router.get('/import/template.xlsx')
    def import_template(request: Request):
        return Response(template_workbook(), media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', headers={'Content-Disposition':'attachment; filename=knowledge-base-template.xlsx'})

    @router.post("/import/commit")
    def import_commit(request: Request):
        stored=request.session.pop("import_file",None)
        if not stored:
            abort(400)
        path=config.database.parent/"imports"/stored[0]
        try:
            report=import_workbook(db,path.read_bytes(),stored[1])
            flash(request, f"Добавлено: {report['added']}. Уже существующих: {report['skipped_existing']}. Все новые карточки — черновики.")
        finally:
            path.unlink(missing_ok=True)
        return redirect(url_for("cards"))

    @router.api_route("/resources",methods=["GET","POST"])
    def resources(request: Request):
        if request.method=="POST":
            key=request.state.form.get("key","")
            value=request.state.form.get("value","").strip()
            name=request.state.form.get('label','').strip()
            if not key:
                key='contact_'+secrets.token_hex(12)
            if request.state.form.get('visibility','public') != 'public':
                flash(request, "Форма устарела. Обновите страницу: здесь сохраняются только значения для ответов бота.","error")
            elif not re.fullmatch(r"[a-z][a-z0-9_]{1,80}",key) or not value or len(value)>2000 or len(name)>250:
                flash(request, "Укажите ссылку или контакт (до 2000 символов) и название до 250 символов.","error")
            else:
                with db.connect() as con:
                    con.execute('BEGIN IMMEDIATE')
                    old=con.execute('SELECT label FROM resources WHERE key=?',(key,)).fetchone()
                    name=name or (old['label'] if old else '') or value
                    saved=con.execute("INSERT INTO resources(key,value,visibility,label) VALUES (?,?,'public',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,label=excluded.label WHERE resources.visibility='public' RETURNING key",(key,value,name)).fetchone()
                    if saved:
                        con.execute("INSERT INTO audit(action,target,created_at) VALUES ('resource_changed',?,?)",(key,time.time()))
                        flash(request, "Сохранено. Ссылка или контакт обновлены во всех связанных ответах.")
                    else:
                        flash(request, "Эта запись недоступна для изменения. Обновите страницу.","error")
            return redirect(url_for("resources"))
        resources=db.all("SELECT * FROM resources WHERE visibility='public' ORDER BY label,value")
        usage={r['key']:[] for r in resources}
        for card in db.all("SELECT id,title,status,answer FROM cards WHERE visibility='public' ORDER BY title,id"):
            for key in resource_keys(card['answer']):
                if key in usage:
                    usage[key].append(card)
        return render(request, "resources.html",title="Ссылки и контакты",resources=resources,usage=usage)

    def queue_revision(state):
        rows=db.dialogue_rows(state)
        count=db.one("SELECT count(*) n FROM tickets WHERE status='open'")['n']
        return hashlib.sha256(json.dumps([rows,count],sort_keys=True).encode()).hexdigest()

    @router.get('/queue/state')
    def queue_state(request: Request):
        state=request.query_params.get('state','open')
        if state not in ('open','waiting_user','all','closed'):
            abort(400)
        if state=='closed':
            state='all'
        return dict(revision=queue_revision(state))

    @router.get("/queue")
    def queue(request: Request):
        state=request.query_params.get("state","open")
        if state=='closed':
            return redirect(url_for('queue',state='all'))
        state=state if state in ('open','waiting_user','all') else 'open'
        revision=queue_revision(state)
        rows=db.dialogue_rows(state)
        return render(request, "queue.html",title="Диалоги",tickets=rows,state=state,queue_revision=revision)

    def selected_ticket(request, peer_id):
        value=request.query_params.get('ticket')
        if value is None:
            return None
        if not value.isdecimal() or len(value)>18 or int(value)<=0:
            abort(400)
        ticket=db.ticket_details(peer_id,int(value))
        if not ticket:
            abort(404)
        return ticket

    @router.api_route("/dialogue/{peer_id}",methods=["GET","POST"])
    def dialogue(request: Request, peer_id: int):
        fragment=request.query_params.get('window')=='1'
        c=db.one("SELECT c.*,trim(coalesce(p.first_name,'')||' '||coalesce(p.last_name,'')) user_name FROM conversations c LEFT JOIN vk_profiles p ON p.user_id=c.user_id WHERE c.peer_id=?",(peer_id,))
        if not c:
            abort(404)
        selected=selected_ticket(request, peer_id)
        if selected and request.method=='GET':
            return redirect(url_for('dialogue',peer_id=peer_id))
        ticket_id=selected['id'] if selected else None
        readonly=bool(selected and selected['status']=='closed')
        draft=''
        form_key=secrets.token_urlsafe(24)
        if request.method=="POST":
            action=request.state.form.get("action")
            if readonly and action!='topic':
                abort(409,'Обращение завершено. Для нового ответа откройте текущий диалог.')
            if selected and action=='topic' and request.state.form.get('ticket_id')!=str(ticket_id):
                abort(400)
            try:
                if action=='reply':
                    draft=request.state.form.get('reply','')
                    name=request.state.form.get('staff_name','').strip()[:80] or 'Сотрудник'
                    request.session['staff_name']=name
                    form_key=request.state.form.get('client_key','')
                    inbox.queue(peer_id,draft,name,form_key,request.state.form.get('revision',''))
                    flash(request, 'Ответ поставлен в очередь отправки. Подтверждение появится в переписке.')
                elif action in ('retry','cancel'):
                    inbox.control(peer_id,int(request.state.form.get('outbox_id','0')),action)
                    flash(request, 'Повтор отправки запланирован.' if action=='retry' else 'Неотправленный ответ отменён.')
                elif action=="resume":
                    db.resume(peer_id,request.state.form.get('revision',''))
                    flash(request, "Управление возвращено боту. Он ответит на следующее новое сообщение.")
                elif action=="take":
                    name=request.state.form.get("staff_name","").strip()[:80] or "Сотрудник"
                    request.session["staff_name"]=name
                    db.take_dialogue(peer_id,name,request.state.form.get('revision',''))
                    flash(request, "Диалог закреплен за вами. Автоматические ответы приостановлены.")
                elif action in ('pause_bot','unpause_bot'):
                    db.pause_bot(peer_id,action=='pause_bot',request.state.form.get('revision',''))
                    flash(request,'Автоответы отключены на 24 часа. Вы можете отвечать вручную.' if action=='pause_bot' else 'Ограничение автоответов снято. Текущий режим диалога сохранен.')
                elif action=="note":
                    db.execute("UPDATE tickets SET note=? WHERE peer_id=? AND status IN ('open','waiting_user')",(request.state.form.get("note","")[:2000],peer_id))
                    flash(request, "Заметка сохранена.")
                elif action=='topic':
                    db.set_ticket_topic(peer_id,int(request.state.form.get('ticket_id','0')),request.state.form.get('topic',''),int(request.state.form.get('topic_version','0')))
                    flash(request, 'Тема обращения сохранена.')
                elif action=='auto_close':
                    target_id=int(request.state.form.get('ticket_id','0'))
                    if selected and target_id!=ticket_id:
                        abort(400)
                    db.pause_auto_close(peer_id,target_id,request.state.form.get('paused')=='1')
                    flash(request, 'Диалог оставлен у сотрудника без срока.' if request.state.form.get('paused')=='1' else 'Возврат боту по неактивности включен. Срок считается от последнего ответа сотрудника.')
                else:
                    abort(400,'Неизвестное действие.')
                return redirect(url_for("dialogue",peer_id=peer_id,ticket=ticket_id,window='1' if fragment else None))
            except ValueError as exc:
                flash(request, str(exc),'error')
                if action!='reply':
                    return redirect(url_for('dialogue',peer_id=peer_id,ticket=ticket_id,window='1' if fragment else None))
        tickets=db.all("SELECT * FROM tickets WHERE peer_id=? AND status IN ('open','waiting_user') ORDER BY id DESC",(peer_id,))
        revision=inbox.revision(peer_id)
        topics=sorted({topic_label(row['topic']) for row in db.all("SELECT DISTINCT topic FROM cards WHERE topic!=''")})
        return render(request, '_dialogue.html' if fragment else 'dialogue.html',fragment=fragment,title=c['user_name'] or f"Диалог {peer_id}",conversation=c,tickets=tickets,messages=inbox.timeline(peer_id,limit=30,include_sessions=True),revision=revision,draft=draft,form_key=form_key,peer_id=peer_id,topics=topics,readonly=False,bot_paused=c['bot_paused_until']>time.time(),protection_events=Protection(db).state(peer_id))

    @router.get('/dialogue/{peer_id}/updates')
    def dialogue_updates(request: Request, peer_id: int):
        c=db.one('SELECT * FROM conversations WHERE peer_id=?',(peer_id,))
        if not c:
            abort(404)
        selected_ticket(request, peer_id)
        revision=inbox.revision(peer_id)
        if revision==request.query_params.get('revision'):
            return dict(revision=revision)
        active=db.one("SELECT status FROM tickets WHERE peer_id=? AND status IN ('open','waiting_user')",(peer_id,))
        return dict(revision=revision,html=templates.get_template('_messages.html').render(messages=inbox.timeline(peer_id,limit=30,include_sessions=True),peer_id=peer_id,readonly=False,csrf=request.session['csrf'],group_id=config.group_id),status='Отвечает сотрудник' if c['human_mode'] else 'Автоответы отключены' if c['bot_paused_until']>time.time() else 'Отвечает бот')

    def selected_statistics_period(request):
        try:
            return statistics_period(request.query_params, time.time())
        except ValueError as exc:
            abort(400, str(exc))

    @router.get("/statistics")
    def statistics(request: Request):
        now=time.time()
        today=datetime.fromtimestamp(now,MOSCOW).date().isoformat()
        try:
            period=statistics_period(request.query_params,now)
        except ValueError as exc:
            response=render(request,"statistics.html",title="Статистика",period=None,periods=PERIODS,period_error=str(exc),today=today,from_date=request.query_params.get('from',''),to_date=request.query_params.get('to',''))
            response.status_code=400
            return response
        bounds=(period['since'],period['until'])
        automation=automation_report(db,*bounds)
        outcomes=db.all("SELECT outcome,status,count(*) n FROM requests WHERE created_at>=? AND created_at<? GROUP BY outcome,status ORDER BY n DESC",bounds)
        totals=db.one("SELECT count(*) total,sum(prompt_tokens+completion_tokens) tokens,avg(duration_ms) latency FROM requests WHERE created_at>=? AND created_at<?",bounds)
        counts=Counter()
        for row in db.all("SELECT topic,count(*) n FROM requests WHERE created_at>=? AND created_at<? AND outcome IN ('answer','clarify','handoff') GROUP BY topic",bounds):
            counts[topic_label(row['topic']) or 'Тема не указана']+=row['n']
        topics=[dict(topic=topic,n=n) for topic,n in sorted(counts.items(),key=lambda pair:(-pair[1],pair[0]))]
        closures=db.one("SELECT sum(closure_reason='inactivity') inactivity,sum(closure_reason='manual') manual FROM tickets WHERE status='closed' AND closed_at>=? AND closed_at<?",bounds)
        return render(request, "statistics.html",title="Статистика",totals=totals,outcomes=outcomes,topics=topics,period=period,periods=PERIODS,today=today,from_date=period['from_date'],to_date=period['to_date'],closures=closures,automation=automation['summary'],daily=automation['days'],feedback=feedback_report(db,*bounds),session_closures=db.one("SELECT sum(closure_reason='rating') rating,sum(closure_reason='inactivity') inactivity,sum(closure_reason='manual') manual FROM dialogue_sessions WHERE closed_at>=? AND closed_at<?",bounds))

    @router.get('/feedback.csv')
    def export_feedback(request: Request):
        period=selected_statistics_period(request)
        report=feedback_report(db,period['since'],period['until'])
        output=io.StringIO()
        writer=csv.writer(output)
        writer.writerow(['Дата опроса (Москва)','Отправлено опросов','Хорошо','Плохо','Всего оценок','Доля Хорошо, %','Доля ответивших, %'])
        for row in report['days']:
            writer.writerow([row['day'],row['sent'],row['good'],row['bad'],row['rated'],
                f"{row['percent']:.1f}" if row['percent'] is not None else '',
                f"{row['response_percent']:.1f}" if row['response_percent'] is not None else ''])
        return Response('\ufeff'+output.getvalue(),media_type='text/csv',headers={'Content-Disposition':'attachment; filename=feedback-by-day.csv'})

    @router.get('/automation.csv')
    def export_automation(request: Request):
        period=selected_statistics_period(request)
        report=automation_report(db,period['since'],period['until'])
        output=io.StringIO()
        writer=csv.writer(output)
        writer.writerow(['Дата (Москва)','Ответил бот','Потребовался сотрудник','Всего результатов','Автоматизация, %'])
        for row in report['days']:
            writer.writerow([row['day'],row['automated'],row['handed_off'],row['total'],f"{row['percent']:.1f}" if row['percent'] is not None else ''])
        return Response('\ufeff'+output.getvalue(),media_type='text/csv',headers={'Content-Disposition':'attachment; filename=automation-by-day.csv'})

    @router.get('/tickets.csv')
    def export_tickets(request: Request):
        period=selected_statistics_period(request)
        output=io.StringIO()
        writer=csv.writer(output)
        writer.writerow(['ID записи','VK ID','Тема','Дата возврата UTC','Причина возврата'])
        for row in db.all("SELECT * FROM tickets WHERE status='closed' AND closed_at>=? AND closed_at<? ORDER BY closed_at",(period['since'],period['until'])):
            values=[row['id'],row['peer_id'],topic_label(row['topic']),datetime.fromtimestamp(row['closed_at'],ZoneInfo('UTC')).isoformat(),label(row['closure_reason'])]
            writer.writerow(["'"+v if isinstance(v,str) and v.startswith(('=','+','-','@')) else v for v in values])
        return Response('\ufeff'+output.getvalue(),media_type='text/csv',headers={'Content-Disposition':'attachment; filename=closed-tickets.csv'})

    @router.get("/statistics.csv")
    def export_stats(request: Request):
        output=io.StringIO()
        writer=csv.writer(output)
        writer.writerow(["Дата UTC","Тема","Результат","Доставка","Тип сообщения","Токены","Время мс"])
        if request.query_params:
            period=selected_statistics_period(request)
            rows=db.all("SELECT created_at,topic,outcome,status,input_type,prompt_tokens+completion_tokens tokens,duration_ms FROM requests WHERE created_at>=? AND created_at<? ORDER BY id",(period['since'],period['until']))
        else:
            rows=db.all("SELECT created_at,topic,outcome,status,input_type,prompt_tokens+completion_tokens tokens,duration_ms FROM requests ORDER BY id")
        for r in rows:
            values=[datetime.fromtimestamp(r["created_at"],ZoneInfo("UTC")).isoformat(),topic_label(r["topic"]),r["outcome"],r["status"],r["input_type"],r["tokens"],r["duration_ms"]]
            writer.writerow(["'"+v if isinstance(v,str) and v.startswith(("=","+","-","@")) else v for v in values])
        return Response("\ufeff"+output.getvalue(),media_type="text/csv",headers={"Content-Disposition":"attachment; filename=statistics.csv"})

    @router.api_route("/settings",methods=["GET","POST"])
    def settings_page(request: Request):
        if request.method=="POST":
            old=db.settings()
            values={key:request.state.form.get(key,old[key]).strip() for key in ("max_clarifications","consent_url","consent_text","handoff_text","retention_days","waiting_user_timeout_hours")}
            values.update({key:old[key] for key in ('search_strategy','search_limit','consent_version')})
            values.update(cover_mode=str(request.state.form.get("cover_mode")=="on").lower(),consent_ready=str(request.state.form.get("consent_ready")=="on").lower(),feedback_enabled=str(request.state.form.get('feedback_enabled')=='on').lower())
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
                flash(request, "Настройки сохранены.")
            except ValueError as exc:
                flash(request, str(exc),"error")
            return redirect(url_for("settings_page"))
        return render(request, "settings.html",title="Настройки",external=config.external_user_data)

    @router.api_route("/preview",methods=["GET","POST"])
    def preview(request: Request):
        text=request.state.form.get("text","")[:4000]
        selected=[]
        answer_card=None
        decision=None
        if request.method=="POST":
            candidates=db.cards(eligible=True)
            try:
                selected=searcher(db.setting('search_strategy'),db).select(text,candidates,int(db.setting("search_limit")))
                if request.state.form.get("action")=="ai":
                    if request.state.form.get("synthetic")!="on":
                        raise ValueError("Для теста используйте вымышленные данные и подтвердите это.")
                    if is_thanks(text):
                        decision=dict(action='acknowledge',kind='thanks',reply=THANKS_TEXT,reason='Готовый ответ без вызова ИИ.')
                        return render(request, "preview.html",title="Проверка ответа",text=text,decision=decision,answer_card=None)
                    if is_greeting(text):
                        decision=dict(action='greeting',reply=GREETING_TEXT,reason='Готовое приветствие без вызова ИИ.')
                        return render(request, "preview.html",title="Проверка ответа",text=text,decision=decision,answer_card=None)
                    query = parse_query(text)
                    if query is not None:
                        result = answer_query(query)
                        decision = dict(action=result.outcome, source='flight_board', reply=result.text,
                                        topic='Рейсы', reason='Поиск по табло Пулково без вызова ИИ.')
                        return render(request, "preview.html",title="Проверка ответа",text=text,decision=decision,answer_card=None)
                    if not selected:
                        raise ValueError("Сначала опубликуйте проверенные карточки.")
                    for card in selected:
                        card["rendered_answer"]=db.render_answer(card)
                    ai=GigaChat(config)
                    decision=ai.route(text,selected)
                    if needs_full_catalog(decision) and len(selected)<len(candidates):
                        for card in candidates:
                            card['rendered_answer']=db.render_answer(card)
                        selected=candidates
                        decision=ai.route(text,selected)
                    answer_card=next((c for c in selected if c['id']==decision.get('card_id')),None)
                    if handoff_reason(decision,answer_card):
                        decision['action']='handoff'
            except (AIError,ValueError,RuntimeError) as exc:
                flash(request, str(exc),"error")
        return render(request, "preview.html",title="Проверка ответа",text=text,decision=decision,answer_card=answer_card)

    app.include_router(router)
    return app
