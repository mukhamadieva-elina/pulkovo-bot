import argparse
import json
import secrets
import time
from pathlib import Path

from dotenv import dotenv_values, set_key

from .config import ROOT, Config
from .db import Database
from .importer import import_workbook
from .search import searcher


def main():
    parser=argparse.ArgumentParser(description="Управление базой VK-бота")
    sub=parser.add_subparsers(dest="command",required=True)
    sub.add_parser("init-config")
    sub.add_parser("migrate")
    sub.add_parser("purge")
    imp=sub.add_parser("import")
    imp.add_argument("file",type=Path)
    backup=sub.add_parser("backup")
    backup.add_argument("file",type=Path)
    evaluate=sub.add_parser("evaluate")
    evaluate.add_argument("dataset",type=Path)
    evaluate.add_argument("--strategy",choices=["full","lexical","local"],default="full")
    evaluate.add_argument("--live",action="store_true",help="Отправить обезличенные тесты в GigaChat")
    args=parser.parse_args()
    if args.command=="init-config":
        path=ROOT/".env"
        path.touch(exist_ok=True)
        current=dotenv_values(path)
        defaults={"ADMIN_PASSWORD":secrets.token_urlsafe(18),"ADMIN_SECRET_KEY":secrets.token_hex(32),"DATABASE_PATH":"data/bot.sqlite3","ADMIN_PORT":"8080","GIGACHAT_MODEL":"GigaChat-2-Pro","ALLOW_EXTERNAL_USER_DATA":"false"}
        for key,value in defaults.items():
            if not current.get(key):
                set_key(path,key,value,quote_mode="always")
        path.chmod(0o600)
        print("Настройки созданы. Пароль находится в ADMIN_PASSWORD файла .env; значения секретов скрыты.")
        return
    config=Config.load()
    db=Database(config.database)
    if args.command=="migrate":
        for row in db.all("SELECT version FROM schema_migrations ORDER BY version"):
            print(row["version"])
    elif args.command=="import":
        print(json.dumps(import_workbook(db,args.file.read_bytes(),args.file.name),ensure_ascii=False,indent=2))
    elif args.command=="backup":
        if args.file.exists():
            raise SystemExit("Файл резервной копии уже существует; выберите новое имя.")
        args.file.parent.mkdir(parents=True,exist_ok=True)
        db.backup(args.file)
        print("Резервная копия создана.")
    elif args.command=="purge":
        db.purge()
        print("Содержание старых обработанных сообщений очищено; агрегатные метаданные сохранены.")
    elif args.command=="evaluate":
        from .ai import GigaChat
        cards=db.cards(eligible=True)
        if not cards:
            raise SystemExit("Нет опубликованных карточек для оценки.")
        for card in cards:
            card["rendered_answer"]=db.render_answer(card)
        by_title={c["title"]:c["id"] for c in cards}
        retriever=searcher(args.strategy,db)
        ai=GigaChat(config) if args.live else None
        rows=[]
        for line in args.dataset.read_text().splitlines():
            if not line.strip():
                continue
            case=json.loads(line)
            expected=by_title.get(case.get("expected_title"))
            if case.get("expected_title") and expected is None:
                raise SystemExit("В тесте указана отсутствующая опубликованная карточка: " + case["expected_title"])
            start=time.monotonic()
            selected=retriever.select(case["text"],cards,int(db.setting("search_limit")))
            decision=ai.route(case["text"],selected) if ai else None
            rows.append({"case":case.get("id",len(rows)+1),"candidate_count":len(selected),"retrieval_hit":None if expected is None else expected in {c["id"] for c in selected},"correct":None if not decision else decision["action"]==case["expected_action"] and (expected is None or decision.get("card_id")==expected),"decision":decision,"elapsed_ms":round((time.monotonic()-start)*1000)})
        print(json.dumps({"strategy":args.strategy,"live":args.live,"cases":rows,"correct":sum(r["correct"] is True for r in rows),"total":len(rows)},ensure_ascii=False,indent=2))


if __name__=="__main__":
    main()
