"""Доступ к базе в рамках запроса и общие операции с данными."""
import sqlite3

from flask import current_app, g
from flask_login import current_user

from .utils import to_int


def get_db():
    """Соединение с базой для текущего запроса.

    timeout — ждать блокировку до 15 с; WAL — один пишущий и несколько читающих без блокировки друг друга.
    """
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE"], timeout=15.0)
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.row_factory = sqlite3.Row
        g.db.create_function("CASEFOLD", 1, lambda s: s.casefold() if isinstance(s, str) else "")
    return g.db


def close_db(error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def fetch_all(query, params=()):
    return get_db().execute(query, params).fetchall()


def fetch_one(query, params=()):
    return get_db().execute(query, params).fetchone()


REF_TABLES = {
    "equipment", "equipment_statuses", "machines", "machine_models", "machine_types", "sim_cards",
    "sim_statuses", "sensor_positions", "shift_task_types",
}


def ref_id(table, raw):
    """id из формы, если такая запись есть в таблице, иначе None."""
    assert table in REF_TABLES
    id_ = to_int(raw)
    if id_ is None:
        return None
    return id_ if fetch_one(f"SELECT 1 FROM {table} WHERE id = ?", (id_,)) else None


def get_setting(key: str, default: str = ""):
    """Значение настройки из app_settings."""
    row = fetch_one("SELECT value FROM app_settings WHERE key = ?", (key,))
    return row["value"] if row else default


def set_setting(key: str, value: str):
    db = get_db()
    db.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)", (key, value))
    db.commit()


def log_operation(operation_type: str, entity_type: str, entity_id: int, description: str):
    """Запись в журнал операций от имени текущего пользователя."""
    username = current_user.username if current_user.is_authenticated else None
    db = get_db()
    db.execute(
        """
        INSERT INTO operations_log (operation_type, entity_type, entity_id, description, username)
        VALUES (?, ?, ?, ?, ?)
        """,
        (operation_type, entity_type, entity_id, description, username),
    )
    db.commit()
