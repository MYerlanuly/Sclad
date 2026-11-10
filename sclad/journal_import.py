"""Перенос данных из старой программы журнала (PostgreSQL, база shift_journal_db).

Таблицы старой программы:
  journal(id, date, shift, time, content, note)      — записи журнала
  engineers(id, shift, date, name)                    — инженеры на смене
  engineers_info(id, full_name, tab_number)           — справочник инженеров

Данные берутся либо напрямую из PostgreSQL (нужен пакет psycopg2), либо из CSV,
выгруженных через pgAdmin (journal.csv, engineers.csv, engineers_info.csv, с заголовками).
Повторный запуск безопасен: уже перенесённые записи пропускаются.
"""
import csv
import os
from datetime import date, datetime, time

from .shift_journal import normalize_time, parse_shift


def _iso_date(value):
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    raw = (str(value or "")).strip()[:10]
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def _hhmm(value):
    if isinstance(value, time):
        return value.strftime("%H:%M")
    return normalize_time(str(value or "").strip()[:5])


def import_journal(conn, journal_rows, engineers_rows=(), info_rows=()):
    """Переносит строки (словари с полями как в старых таблицах) в базу склада. Возвращает отчёт."""
    report = {
        "entries_added": 0,
        "entries_skipped": 0,
        "entries_bad": 0,
        "engineers_added": 0,
        "engineers_skipped": 0,
        "personnel_numbers_set": 0,
        "unmatched_engineers": [],
    }
    for row in journal_rows:
        shift_date = _iso_date(row.get("date"))
        shift = parse_shift(str(row.get("shift") or ""))
        entry_time = _hhmm(row.get("time"))
        if not shift_date or not shift or not entry_time:
            report["entries_bad"] += 1
            continue
        content = (row.get("content") or "").strip()
        note = (row.get("note") or "").strip()
        exists = conn.execute(
            """
            SELECT 1 FROM journal_entries
            WHERE shift_date = ? AND shift = ? AND entry_time = ? AND content = ? AND note = ?
            """,
            (shift_date, shift, entry_time, content, note),
        ).fetchone()
        if exists:
            report["entries_skipped"] += 1
            continue
        conn.execute(
            "INSERT INTO journal_entries (shift_date, shift, entry_time, content, note) VALUES (?, ?, ?, ?, ?)",
            (shift_date, shift, entry_time, content, note),
        )
        report["entries_added"] += 1

    for row in engineers_rows:
        shift_date = _iso_date(row.get("date"))
        shift = parse_shift(str(row.get("shift") or ""))
        name = (row.get("name") or "").strip()
        if not shift_date or not shift or not name:
            report["engineers_skipped"] += 1
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO journal_shift_engineers (shift_date, shift, name) VALUES (?, ?, ?)",
            (shift_date, shift, name),
        )
        report["engineers_added" if cur.rowcount else "engineers_skipped"] += 1

    # Табельные номера из справочника — в профили пользователей склада с тем же ФИО
    users = conn.execute("SELECT id, surname, first_name, personnel_number FROM users").fetchall()
    by_name = {}
    for u in users:
        full = f"{u[1] or ''} {u[2] or ''}".strip().casefold()
        if full:
            by_name[full] = u
    for row in info_rows:
        full_name = (row.get("full_name") or "").strip()
        tab = (str(row.get("tab_number") or "")).strip()
        if not full_name:
            continue
        key = full_name.casefold()
        user = by_name.get(key) or next((u for k, u in by_name.items() if key.startswith(k + " ")), None)
        if user is None:
            report["unmatched_engineers"].append(full_name)
            continue
        if tab and not (user[3] or "").strip():
            conn.execute("UPDATE users SET personnel_number = ? WHERE id = ?", (tab, user[0]))
            report["personnel_numbers_set"] += 1
    return report


def read_csv_dir(path):
    """Читает journal.csv, engineers.csv и engineers_info.csv (последние два — необязательно)."""

    def _read(name):
        file_path = os.path.join(path, name)
        if not os.path.exists(file_path):
            return []
        with open(file_path, encoding="utf-8-sig", newline="") as f:
            sample = f.read(4096)
            f.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
            except csv.Error:
                dialect = csv.excel
            return list(csv.DictReader(f, dialect=dialect))

    journal_path = os.path.join(path, "journal.csv")
    if not os.path.exists(journal_path):
        raise FileNotFoundError(f"Не найден файл {journal_path}")
    return _read("journal.csv"), _read("engineers.csv"), _read("engineers_info.csv")


def read_postgres(dsn):
    try:
        import psycopg2
        import psycopg2.extras
    except ImportError as e:
        raise RuntimeError("Для чтения из PostgreSQL установите пакет: pip install psycopg2-binary") from e
    pg = psycopg2.connect(dsn)
    try:
        cur = pg.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        result = []
        for table, cols in (
            ("journal", "date, shift, time, content, note"),
            ("engineers", "date, shift, name"),
            ("engineers_info", "full_name, tab_number"),
        ):
            try:
                cur.execute(f"SELECT {cols} FROM {table} ORDER BY id")
                result.append([dict(r) for r in cur.fetchall()])
            except psycopg2.Error:
                pg.rollback()
                result.append([])
        return tuple(result)
    finally:
        pg.close()
