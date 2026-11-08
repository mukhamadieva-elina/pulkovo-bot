import hashlib
import io
import json
import re
import time
import unicodedata
import zipfile

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from .resources import default_resource_label


SIMPLE_COLUMNS = ('Тема', 'Вопрос', 'Ответ', 'Код', 'Другие формулировки', 'Условия')


def template_workbook():
    book = Workbook()
    sheet = book.active
    sheet.title = 'База знаний'
    sheet.append(SIMPLE_COLUMNS)
    for cell in sheet[1]:
        cell.font = Font(bold=True, color='FFFFFF')
        cell.fill = PatternFill('solid', fgColor='142437')
        cell.alignment = Alignment(wrap_text=True, vertical='center')
    sheet.row_dimensions[1].height = 30
    for col, width in zip('ABCDEF', (27, 48, 85, 24, 50, 45)):
        sheet.column_dimensions[col].width = width
    for row in sheet.iter_rows(min_row=2,max_row=20,max_col=6):
        for cell in row:
            cell.number_format = '@'
            cell.alignment = Alignment(wrap_text=True, vertical='top')
    sheet.freeze_panes = 'A2'
    output = io.BytesIO()
    book.save(output)
    book.close()
    return output.getvalue()


def inspect_simple_sheet(sheet, filename):
    if sheet.max_row > 5000 or sheet.max_column > 30:
        raise ValueError('Превышен допустимый размер листа: 5000 строк, 30 столбцов.')
    if sheet.merged_cells.ranges:
        raise ValueError('На листе «База знаний» не должно быть объединённых ячеек: один вопрос — одна строка.')
    headers = [str(c.value or '').strip().casefold() for c in sheet[1]]
    if len([h for h in headers if h]) != len(set(h for h in headers if h)):
        raise ValueError('В заголовке повторяется название столбца.')
    if any(name.casefold() not in headers for name in SIMPLE_COLUMNS[:3]):
        raise ValueError('В первой строке нужны столбцы «Тема», «Вопрос», «Ответ». Скачайте шаблон.')
    if any(h and h not in {n.casefold() for n in SIMPLE_COLUMNS} for h in headers):
        raise ValueError('Есть неизвестные столбцы. Допустимы: ' + ', '.join(SIMPLE_COLUMNS) + '.')
    cards, seen = [], set()
    for number, cells in enumerate(sheet.iter_rows(min_row=2), 2):
        if any(c.data_type == 'f' for c in cells):
            raise ValueError(f'Строка {number}: вместо формул нужны текстовые значения.')
        values = [str(c.value).strip() if c.value is not None else '' for c in cells]
        if not any(values):
            continue
        if any(value and not headers[i] for i, value in enumerate(values)):
            raise ValueError(f'Строка {number}: есть данные в столбце без заголовка.')
        data = dict(zip(headers, values))
        topic, title, answer = (data.get(n.casefold(), '') for n in SIMPLE_COLUMNS[:3])
        if not topic or not title:
            raise ValueError(f'Строка {number}: заполните тему и вопрос.')
        code = data.get('код', '')
        key = 'simple:code:' + unicodedata.normalize('NFC',code) if code else 'simple:question:' + hashlib.sha256(unicodedata.normalize('NFC',topic.casefold()+'\n'+title.casefold()).encode()).hexdigest()
        if key in seen:
            raise ValueError(f'Строка {number}: повторяется код либо сочетание темы и вопроса.')
        seen.add(key)
        issues = []
        if not answer:
            issues.append('Не заполнен ответ.')
        elif len(answer) < 25:
            issues.append('Слишком краткий ответ: проверьте область применения.')
        if re.search(r'\[имя\]|\(ссылка\)|\b[ХхXx]{2}\b|https?:\s+//|замените этот', answer, re.I):
            issues.append('В ответе остались незаполненные подстановки.')
        cards.append(dict(topic=topic,title=title,answer=answer,question_variants=[v.strip() for v in data.get('другие формулировки','').splitlines() if v.strip()],conditions=data.get('условия',''),mode='any',action='handoff' if topic.casefold()=='жалобы' else 'answer',visibility='public',status='draft',reviewed=0,issues=issues,source=f'{filename} · База знаний · строка {number}',source_key=key))
    if not cards:
        raise ValueError('В файле нет вопросов. Заполните строки под заголовком шаблона.')
    return cards


def inspect_workbook(raw, filename):
    if len(raw) > 10 * 1024 * 1024:
        raise ValueError("Excel должен быть меньше 10 МБ.")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        if sum(f.file_size for f in archive.infolist()) > 50 * 1024 * 1024:
            raise ValueError("Слишком большой распакованный Excel.")
    book = load_workbook(io.BytesIO(raw), data_only=False)
    filename = unicodedata.normalize("NFC", filename)
    if 'База знаний' in book.sheetnames:
        try:
            return inspect_simple_sheet(book['База знаний'], filename)
        finally:
            book.close()
    if "Обновл. таблица" not in book.sheetnames:
        book.close()
        raise ValueError("Нужен лист «База знаний» из шаблона или «Обновл. таблица» из исходного файла заказчика.")
    cards = []
    filename = unicodedata.normalize("NFC", filename)
    for sheet in book:
        if sheet.max_row > 5000 or sheet.max_column > 30:
            raise ValueError("Превышен допустимый размер листа.")
        if sheet.title not in ("Обновл. таблица", "КОВЕР ответы", "Полезные контакты"):
            continue
        parent = "Общее"
        sub = ""
        anchors = {}
        for merge in sheet.merged_cells.ranges:
            if merge.min_col == merge.max_col == 2:
                for row in range(merge.min_row + 1, merge.max_row + 1):
                    anchors[row] = merge.min_row
        by_row = {}
        for number, cells in enumerate(sheet.iter_rows(), 1):
            if any(cell.data_type == "f" for cell in cells):
                raise ValueError(f"Формула на листе {sheet.title}, строка {number}: нужны текстовые значения.")
            row = [str(c.value).strip() if c.value is not None else "" for c in cells]
            row += [""] * (3-len(row))
            a, b, c = row[:3]
            if sheet.title == "Обновл. таблица":
                if number == 1:
                    continue
                if a and not b and not c:
                    parent, sub = a, ""
                    continue
                if a:
                    sub = a
                if not b and c and number in anchors and anchors[number] in by_row:
                    previous = by_row[anchors[number]]
                    previous["answer"] += "\n\n[Вариант из строки " + str(number) + "]\n" + c
                    previous["issues"].append("Несколько вариантов ответа: нужно определить условия и убрать редакторские пометки.")
                    continue
                if not b:
                    continue
                title, answer, topic = b, c, parent + (" / " + sub if sub else "")
                action = "handoff" if parent == "Жалобы" else "answer"
            elif sheet.title == "КОВЕР ответы":
                if not a:
                    continue
                title, answer, topic, action = a, b, "Рейсы / Ограничения", "answer"
            else:
                if number == 1 or not a:
                    continue
                title, answer, topic, action = a, b, "Внутренние контакты", "handoff"
            issues = []
            if not answer:
                issues.append("Не заполнен ответ.")
            if len(answer) < 25:
                issues.append("Слишком краткий ответ: проверьте область применения.")
            if re.search(r"\[имя\]|\(ссылка\)|\b[ХхXx]{2}\b|https?:\s+//|\.\.\.", answer, re.I):
                issues.append("Незавершённый текст, подстановка или некорректная ссылка.")
            if re.search(r"\b20(?:0\d|1\d|2[0-4])\b", answer):
                issues.append("В ответе есть дата прошлых лет: проверить актуальность.")
            if "Потерялись вещи" in title and "правил вашей авиакомпании" in answer:
                issues.append("Возможное несоответствие ответа вопросу.")
            if sheet.title == "КОВЕР ответы":
                issues.append("Ситуационный ответ: проверить факты и условия активации режима.")
            if sheet.title == "Полезные контакты":
                issues.append("Внутренний справочник. Публичность не согласована.")
            record = dict(topic=topic, title=title, answer=answer, question_variants=[], conditions="", mode="cover" if sheet.title == "КОВЕР ответы" else "any", action=action, visibility="internal" if sheet.title == "Полезные контакты" else "public", status="draft", reviewed=0, issues=issues, source=f"{filename} · {sheet.title} · строка {number}", source_key=f"{filename}:{sheet.title}:{number}")
            cards.append(record)
            by_row[number] = record
    book.close()
    return cards


def import_workbook(db, raw, filename):
    digest = hashlib.sha256(raw).hexdigest()
    existing = db.one("SELECT report FROM import_batches WHERE digest=?", (digest,))
    if existing:
        report=json.loads(existing["report"])
        return report | {"added":0,"skipped_existing":report['added']+report['skipped_existing'],"needs_review":0,"already_imported": True}
    cards = inspect_workbook(raw, filename)
    report = {"added": 0, "skipped_existing": 0, "needs_review": 0, "already_imported": False}
    with db.connect() as con:
        con.execute("BEGIN IMMEDIATE")
        if con.execute("SELECT 1 FROM import_batches WHERE digest=?", (digest,)).fetchone():
            return report | {"already_imported": True}
        for card in cards:
            if con.execute("SELECT 1 FROM cards WHERE source_key=?", (card["source_key"],)).fetchone():
                report["skipped_existing"] += 1
                continue
            def resource(match):
                if card["visibility"] != "public":
                    # Внутренние записи сохраняют исходный текст, а не переменные для бота.
                    return match[0]
                url = match[0].rstrip(".,;")
                key = "link_" + hashlib.sha256(url.encode()).hexdigest()[:12]
                con.execute("INSERT OR IGNORE INTO resources(key,value,visibility,label) VALUES (?,?,'public',?)", (key, url, default_resource_label(url)))
                return "{" + key + "}" + match[0][len(url):]
            card["answer"] = re.sub(r"https?://[^\s<>\)]+", resource, card["answer"])
            card["question_variants"] = json.dumps(card["question_variants"], ensure_ascii=False)
            card["issues"] = json.dumps(card["issues"], ensure_ascii=False)
            card["updated_at"] = time.time()
            columns = list(card)
            cur = con.execute("INSERT INTO cards(" + ",".join(columns) + ") VALUES (" + ",".join("?" for _ in columns) + ")", [card[k] for k in columns])
            snapshot = dict(con.execute("SELECT * FROM cards WHERE id=?", (cur.lastrowid,)).fetchone())
            con.execute("INSERT INTO card_versions(card_id,version,snapshot,created_at) VALUES (?,1,?,?)", (cur.lastrowid, json.dumps(snapshot, ensure_ascii=False), time.time()))
            report["added"] += 1
            report["needs_review"] += bool(json.loads(card["issues"]))
        con.execute("INSERT INTO import_batches(digest,filename,created_at,report) VALUES (?,?,?,?)", (digest, filename, time.time(), json.dumps(report)))
    return report
