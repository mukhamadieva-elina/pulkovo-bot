"""Последовательные транзакционные миграции SQLite. Применённые файлы неизменяемы."""

import hashlib
import sqlite3
import time
from pathlib import Path


def migrate(path, directory=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=10)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("CREATE TABLE IF NOT EXISTS schema_migrations(version TEXT PRIMARY KEY, checksum TEXT NOT NULL, applied_at REAL NOT NULL)")
        con.commit()
        applied = dict(con.execute("SELECT version,checksum FROM schema_migrations"))
        files = sorted((Path(directory) if directory else Path(__file__).parent / "sql").glob("[0-9][0-9][0-9]_*.sql"))
        known = {f.stem for f in files}
        if set(applied) - known:
            raise RuntimeError("База новее приложения: отсутствуют файлы применённых миграций.")
        pending = []
        for file in files:
            raw = file.read_bytes()
            checksum = hashlib.sha256(raw).hexdigest()
            if file.stem in applied:
                if applied[file.stem] != checksum:
                    raise RuntimeError(f"Изменена применённая миграция {file.name}. Создайте новую миграцию.")
            else:
                pending.append((file, raw.decode(), checksum))
        if pending and applied:
            backup_dir = path.parent / "backups"
            backup_dir.mkdir(exist_ok=True)
            backup = backup_dir / f"before-migration-{time.time_ns()}.sqlite3"
            destination = sqlite3.connect(backup)
            try:
                con.backup(destination)
            finally:
                destination.close()
            backup.chmod(0o600)
        for file, sql, checksum in pending:
            # executescript не используется: он фиксирует транзакцию неявно.
            con.execute("BEGIN IMMEDIATE")
            try:
                current = con.execute("SELECT checksum FROM schema_migrations WHERE version=?", (file.stem,)).fetchone()
                if current:
                    if current[0] != checksum:
                        raise RuntimeError("Конфликт контрольной суммы миграции.")
                    con.commit()
                    continue
                statement = ""
                for line in sql.splitlines(keepends=True):
                    statement += line
                    if sqlite3.complete_statement(statement):
                        con.execute(statement)
                        statement = ""
                if statement.strip():
                    raise RuntimeError(f"Незавершённый SQL в {file.name}")
                if con.execute("PRAGMA foreign_key_check").fetchone():
                    raise RuntimeError("Нарушены внешние ключи после миграции.")
                con.execute("INSERT INTO schema_migrations VALUES (?,?,?)", (file.stem, checksum, time.time()))
                con.commit()
            except Exception:
                con.rollback()
                raise
        return [file.stem for file, _, _ in pending]
    finally:
        con.close()
