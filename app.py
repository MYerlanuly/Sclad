import calendar
import csv
import io
import os
import shutil
import sqlite3
import tempfile
import threading
import time
from datetime import date, datetime, timedelta
from urllib.parse import urlparse

from openpyxl import Workbook
from openpyxl.styles import Font, Border, Side

from dotenv import load_dotenv

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    g,
    Response,
    session,
)
from flask_login import (
    LoginManager,
    UserMixin,
    login_user,
    logout_user,
    login_required,
    current_user,
)
from werkzeug.security import check_password_hash, generate_password_hash
from flask_wtf.csrf import CSRFProtect
import click

load_dotenv()

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
# Путь к базе можно переопределить переменной окружения SCLAD_DATABASE (используется в тестах).
DATABASE = os.environ.get("SCLAD_DATABASE") or os.path.join(BASE_DIR, "inventory.db")
SCHEMA_PATH = os.path.join(BASE_DIR, "schema.sql")
# Директория, внутри которой разрешено сохранять резервные копии (и её поддиректории)
BACKUP_ROOT = os.path.join(BASE_DIR, "backups")
BACKUP_LOCK_PATH = os.path.join(tempfile.gettempdir(), "sclad_backup.lock")
LOCK_STALE_SEC = 300


def fix_mojibake_text(text):
    """Исправляет кракозябры: текст в UTF-8 был прочитан как Latin-1 (cp1252)."""
    if not text or not isinstance(text, str):
        return text
    try:
        return text.encode("latin1").decode("utf8")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return text


def maintenance_next_due(performed_at_str, months=3):
    """Следующая дата ТО: performed_at + months (календарные месяцы)."""
    if not performed_at_str:
        return None
    d = date.fromisoformat(str(performed_at_str)[:10])
    month = d.month + months
    year = d.year
    while month > 12:
        month -= 12
        year += 1
    while month < 1:
        month += 12
        year -= 1
    maxday = calendar.monthrange(year, month)[1]
    day = min(d.day, maxday)
    return date(year, month, day).strftime("%Y-%m-%d")


def migrate_equipment_unique_constraint():
    """Миграция: изменение UNIQUE constraint для equipment с serial_number на (name, serial_number)."""
    if not os.path.exists(DATABASE):
        return False

    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    try:
        # Проверяем, существует ли таблица equipment
        table_info = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='equipment'"
        ).fetchone()
        if not table_info:
            conn.close()
            return False  # Таблицы нет, миграция не нужна

        # Проверяем структуру таблицы через CREATE TABLE statement
        create_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='equipment'"
        ).fetchone()

        if create_sql and create_sql[0]:
            sql_text = create_sql[0].upper()
            # Проверяем, есть ли уже составной UNIQUE(name, serial_number)
            sql_normalized = sql_text.replace(" ", "").replace("\n", "").replace("\t", "")
            if "UNIQUE(NAME,SERIAL_NUMBER)" in sql_normalized or "UNIQUE(NAME,SERIAL_NUMBER)" in sql_text:
                conn.close()
                return False  # Миграция уже выполнена

        # Проверяем индексы
        indexes = conn.execute(
            """
            SELECT name, sql FROM sqlite_master 
            WHERE type='index' AND tbl_name='equipment'
            """
        ).fetchall()

        has_composite_unique = False
        for idx in indexes:
            if idx[1]:
                idx_sql = idx[1].upper().replace(" ", "")
                if 'UNIQUE' in idx_sql and 'NAME' in idx_sql and 'SERIAL_NUMBER' in idx_sql:
                    has_composite_unique = True
                    break

        if has_composite_unique:
            conn.close()
            return False  # Миграция уже выполнена

        print("Выполняется миграция таблицы equipment: изменение UNIQUE constraint на (name, serial_number)...")

        # Проверяем на дубликаты по комбинации (name, serial_number)
        duplicates = conn.execute("""
            SELECT name, serial_number, COUNT(*) as cnt
            FROM equipment
            GROUP BY name, serial_number
            HAVING COUNT(*) > 1
        """).fetchall()

        if duplicates:
            print("ВНИМАНИЕ: Найдены дубликаты по комбинации (name, serial_number):")
            for dup in duplicates:
                print(f"  - {dup[0]} / {dup[1]} ({dup[2]} записей)")
            print("Миграция будет прервана. Удалите дубликаты перед миграцией.")
            conn.close()
            return False

        # Сохраняем данные
        old_data = conn.execute("SELECT id, name, serial_number, description, added_at, status_id FROM equipment").fetchall()

        # Создаём временную таблицу с новой структурой
        conn.execute("""
            CREATE TABLE equipment_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                serial_number TEXT NOT NULL,
                description TEXT,
                added_at TEXT NOT NULL DEFAULT (datetime('now')),
                status_id INTEGER NOT NULL,
                FOREIGN KEY (status_id) REFERENCES equipment_statuses(id),
                UNIQUE(name, serial_number)
            )
        """)

        # Копируем данные
        for row in old_data:
            conn.execute(
                """
                INSERT INTO equipment_new (id, name, serial_number, description, added_at, status_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                row,
            )

        # Удаляем старую таблицу
        conn.execute("DROP TABLE equipment")

        # Переименовываем новую таблицу
        conn.execute("ALTER TABLE equipment_new RENAME TO equipment")

        conn.commit()
        conn.close()
        print("Миграция выполнена успешно: UNIQUE constraint изменён на (name, serial_number).")
        return True

    except Exception as e:
        conn.rollback()
        conn.close()
        print(f"Ошибка при миграции: {e}")
        import traceback
        traceback.print_exc()
        return False


def migrate_sensor_positions_group():
    """Добавляет колонку position_group для позиций датчиков (одноразовая миграция)."""
    if not os.path.exists(DATABASE):
        return False
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    try:
        info = conn.execute(
            "PRAGMA table_info(sensor_positions)"
        ).fetchall()
        columns = [row[1] for row in info]
        if "position_group" not in columns:
            conn.execute("ALTER TABLE sensor_positions ADD COLUMN position_group TEXT DEFAULT 'wheel'")
            conn.execute("UPDATE sensor_positions SET position_group = 'wheel' WHERE position_group IS NULL")
            conn.commit()
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        conn.rollback()
        conn.close()
        print(f"Ошибка миграции sensor_positions: {e}")
        return False


def migrate_sensor_positions_labels():
    """Добавляет колонки label и sort_order, исправляет коды позиций колёс (ЗЛ→ПП, ЗП→ЗПВ), задаёт названия."""
    if not os.path.exists(DATABASE):
        return False
    try:
        conn = sqlite3.connect(DATABASE)
        cur = conn.execute("PRAGMA table_info(sensor_positions)").fetchall()
        columns = [row[1] for row in cur]
        if "label" not in columns:
            conn.execute("ALTER TABLE sensor_positions ADD COLUMN label TEXT")
        if "sort_order" not in columns:
            conn.execute("ALTER TABLE sensor_positions ADD COLUMN sort_order INTEGER")
        conn.commit()
        # Позиции колёс: по текущему коду задаём label и sort_order; ЗЛ→ПП, ЗП→ЗПВ
        conn.execute(
            "UPDATE sensor_positions SET label = 'Передний левый', sort_order = 1 WHERE code = 'ПЛ'"
        )
        conn.execute(
            "UPDATE sensor_positions SET code = 'ПП', label = 'Передний правый', sort_order = 2 WHERE code = 'ЗЛ'"
        )
        conn.execute(
            "UPDATE sensor_positions SET label = 'Задний левый наружный', sort_order = 3 WHERE code = 'ЗЛН'"
        )
        conn.execute(
            "UPDATE sensor_positions SET label = 'Задний левый внутренний', sort_order = 4 WHERE code = 'ЗЛВ'"
        )
        conn.execute(
            "UPDATE sensor_positions SET label = 'Задний правый наружный', sort_order = 5 WHERE code = 'ЗПН'"
        )
        conn.execute(
            "UPDATE sensor_positions SET code = 'ЗПВ', label = 'Задний правый внутренний', sort_order = 6 WHERE code = 'ЗП'"
        )
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"Ошибка миграции sensor_positions labels: {e}")
        return False


def migrate_sensors_into_equipment():
    """Объединение датчиков с оборудованием: equipment_kind, sensor_position_id, перенос данных."""
    if not os.path.exists(DATABASE):
        return False
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    try:
        # Добавить equipment_kind в equipment
        info = conn.execute("PRAGMA table_info(equipment)").fetchall()
        if not any(r[1] == "equipment_kind" for r in info):
            conn.execute("ALTER TABLE equipment ADD COLUMN equipment_kind TEXT NOT NULL DEFAULT 'other'")
            conn.commit()
        # Добавить sensor_position_id в machine_equipment
        info = conn.execute("PRAGMA table_info(machine_equipment)").fetchall()
        if not any(r[1] == "sensor_position_id" for r in info):
            conn.execute("ALTER TABLE machine_equipment ADD COLUMN sensor_position_id INTEGER REFERENCES sensor_positions(id)")
            conn.commit()
        # Если есть таблица sensors — переносим данные
        if conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='sensors'").fetchone():
            sensor_to_equipment = {}
            for row in conn.execute("SELECT id, name, serial_number, sensor_type, status_id FROM sensors").fetchall():
                cur = conn.execute(
                    "INSERT INTO equipment (name, serial_number, description, status_id, equipment_kind) VALUES (?, ?, ?, ?, 'sensor')",
                    (row["name"], row["serial_number"], row["sensor_type"] or "", row["status_id"]),
                )
                new_id = cur.lastrowid
                sensor_to_equipment[row["id"]] = new_id
            for row in conn.execute(
                "SELECT machine_id, sensor_id, sensor_position_id, installed_at, removed_at FROM machine_sensors"
            ).fetchall():
                eq_id = sensor_to_equipment.get(row["sensor_id"])
                if eq_id is not None:
                    conn.execute(
                        """INSERT INTO machine_equipment (machine_id, equipment_id, installed_at, removed_at, sensor_position_id)
                           VALUES (?, ?, ?, ?, ?)""",
                        (row["machine_id"], eq_id, row["installed_at"], row["removed_at"], row["sensor_position_id"]),
                    )
            conn.execute("DROP TABLE IF EXISTS machine_sensors")
            conn.execute("DROP TABLE IF EXISTS sensors")
            conn.execute("DROP TABLE IF EXISTS sensor_statuses")
            conn.commit()
        # Уникальный индекс для одной позиции датчика на машину
        try:
            conn.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS idx_machine_equipment_sensor_position
                   ON machine_equipment (machine_id, sensor_position_id)
                   WHERE sensor_position_id IS NOT NULL AND removed_at IS NULL"""
            )
            conn.commit()
        except sqlite3.OperationalError:
            pass
        conn.close()
        return True
    except Exception as e:
        conn.rollback()
        conn.close()
        print(f"Ошибка миграции sensors->equipment: {e}")
        import traceback
        traceback.print_exc()
        return False


def migrate_machine_maintenance():
    """Создаёт таблицу техобслуживания machine_maintenance, если её нет."""
    if not os.path.exists(DATABASE):
        return False
    try:
        conn = sqlite3.connect(DATABASE)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS machine_maintenance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                machine_id INTEGER NOT NULL,
                performed_at TEXT NOT NULL,
                next_due_at TEXT NOT NULL,
                performed_by TEXT,
                remarks TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (machine_id) REFERENCES machines(id)
            )
            """
        )
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"Ошибка миграции machine_maintenance: {e}")
        return False


def migrate_indexes():
    """Создаёт индексы для ускорения фильтрации и сортировки."""
    if not os.path.exists(DATABASE):
        return False
    indexes = [
        "CREATE INDEX IF NOT EXISTS idx_equipment_status_id ON equipment(status_id)",
        "CREATE INDEX IF NOT EXISTS idx_equipment_serial_number ON equipment(serial_number)",
        "CREATE INDEX IF NOT EXISTS idx_machine_equipment_machine_id ON machine_equipment(machine_id)",
        "CREATE INDEX IF NOT EXISTS idx_machine_equipment_equipment_id ON machine_equipment(equipment_id)",
        "CREATE INDEX IF NOT EXISTS idx_machine_maintenance_machine_id ON machine_maintenance(machine_id)",
        "CREATE INDEX IF NOT EXISTS idx_machine_maintenance_next_due ON machine_maintenance(next_due_at)",
        "CREATE INDEX IF NOT EXISTS idx_operations_log_created_at ON operations_log(created_at)",
        "CREATE INDEX IF NOT EXISTS idx_sim_cards_status_id ON sim_cards(status_id)",
        "CREATE INDEX IF NOT EXISTS idx_machines_model_id ON machines(machine_model_id)",
        "CREATE INDEX IF NOT EXISTS idx_machines_inventory_number ON machines(inventory_number)",
    ]
    try:
        conn = sqlite3.connect(DATABASE)
        for sql in indexes:
            try:
                conn.execute(sql)
            except sqlite3.OperationalError:
                pass
        # Одно оборудование может быть активно установлено только на одну технику.
        # Если в старой базе уже есть такие дубли, индекс не создаётся и выводится предупреждение.
        try:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_machine_equipment_active_equipment "
                "ON machine_equipment (equipment_id) WHERE removed_at IS NULL"
            )
        except (sqlite3.IntegrityError, sqlite3.OperationalError) as e:
            print(f"Предупреждение: оборудование активно установлено на несколько машин, проверьте machine_equipment: {e}")
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"Ошибка миграции indexes: {e}")
        return False


def migrate_sim_cards_operator():
    """Добавляет колонку оператор связи в sim_cards."""
    if not os.path.exists(DATABASE):
        return False
    try:
        conn = sqlite3.connect(DATABASE)
        cur = conn.execute("PRAGMA table_info(sim_cards)")
        columns = [row[1] for row in cur.fetchall()]
        if "operator" not in columns:
            conn.execute("ALTER TABLE sim_cards ADD COLUMN operator TEXT")
            conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"Ошибка миграции sim_cards operator: {e}")
        return False


def migrate_sim_cards_description_tariff():
    """Добавляет колонки описание и тариф в sim_cards."""
    if not os.path.exists(DATABASE):
        return False
    try:
        conn = sqlite3.connect(DATABASE)
        cur = conn.execute("PRAGMA table_info(sim_cards)")
        columns = [row[1] for row in cur.fetchall()]
        if "description" not in columns:
            conn.execute("ALTER TABLE sim_cards ADD COLUMN description TEXT")
        if "tariff" not in columns:
            conn.execute("ALTER TABLE sim_cards ADD COLUMN tariff TEXT")
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"Ошибка миграции sim_cards description/tariff: {e}")
        return False


def migrate_app_settings():
    """Создаёт таблицу настроек app_settings и задаёт периодичность ТО по умолчанию."""
    if not os.path.exists(DATABASE):
        return False
    try:
        conn = sqlite3.connect(DATABASE)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO app_settings (key, value) VALUES ('maintenance_interval_months', '3')"
        )
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"Ошибка миграции app_settings: {e}")
        return False


def migrate_operations_log_username():
    """Добавляет колонку username в operations_log (кто выполнил операцию)."""
    if not os.path.exists(DATABASE):
        return False
    try:
        conn = sqlite3.connect(DATABASE)
        cur = conn.execute("PRAGMA table_info(operations_log)")
        columns = [row[1] for row in cur.fetchall()]
        if "username" not in columns:
            conn.execute("ALTER TABLE operations_log ADD COLUMN username TEXT")
            conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"Ошибка миграции operations_log username: {e}")
        return False


def migrate_operations_log_encoding():
    """Исправляет кракозябры в operations_log.description (UTF-8 был прочитан как Latin-1)."""
    if not os.path.exists(DATABASE):
        return False
    try:
        conn = sqlite3.connect(DATABASE)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, description FROM operations_log WHERE description IS NOT NULL AND description != ''"
        ).fetchall()
        updated = 0
        for row in rows:
            fixed = fix_mojibake_text(row["description"])
            if fixed != row["description"]:
                conn.execute(
                    "UPDATE operations_log SET description = ? WHERE id = ?",
                    (fixed, row["id"]),
                )
                updated += 1
        conn.commit()
        conn.close()
        if updated:
            print(f"Исправлено кодировок в журнале операций: {updated}")
        return True
    except Exception as e:
        print(f"Ошибка миграции operations_log encoding: {e}")
        return False


def migrate_users():
    """Создаёт таблицу пользователей users для авторизации."""
    if not os.path.exists(DATABASE):
        return False
    try:
        conn = sqlite3.connect(DATABASE)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"Ошибка миграции users: {e}")
        return False


def migrate_users_profile_and_roles():
    """Добавляет в users: фамилия, имя, должность, табельный номер, роль (admin/editor/reader)."""
    if not os.path.exists(DATABASE):
        return False
    try:
        conn = sqlite3.connect(DATABASE)
        cur = conn.execute("PRAGMA table_info(users)").fetchall()
        columns = [row[1] for row in cur]
        for col, default in [
            ("surname", "TEXT"),
            ("first_name", "TEXT"),
            ("position", "TEXT"),
            ("personnel_number", "TEXT"),
            ("role", "TEXT"),
        ]:
            if col not in columns:
                conn.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT")
        conn.execute(
            "UPDATE users SET role = 'admin' WHERE role IS NULL OR role = ''"
        )
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"Ошибка миграции users profile/roles: {e}")
        return False


def migrate_login_attempts():
    """Таблица для блокировки входа по IP (вместо хранения в памяти)."""
    if not os.path.exists(DATABASE):
        return False
    try:
        conn = sqlite3.connect(DATABASE)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS login_attempts (
                ip TEXT PRIMARY KEY,
                count INTEGER NOT NULL DEFAULT 0,
                lock_until REAL
            )
            """
        )
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"Ошибка миграции login_attempts: {e}")
        return False


def migrate_shift_tasks():
    """Таблица заданий на смену (дневная/ночная)."""
    if not os.path.exists(DATABASE):
        return False
    try:
        conn = sqlite3.connect(DATABASE)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS shift_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                created_by_user_id INTEGER NOT NULL,
                for_shift TEXT NOT NULL CHECK (for_shift IN ('day', 'night')),
                effective_date TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT,
                status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'done')),
                completed_at TEXT,
                completed_by_user_id INTEGER,
                FOREIGN KEY (created_by_user_id) REFERENCES users(id),
                FOREIGN KEY (completed_by_user_id) REFERENCES users(id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_shift_tasks_effective_for ON shift_tasks (effective_date, for_shift)"
        )
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"Ошибка миграции shift_tasks: {e}")
        return False


def migrate_shift_tasks_type_and_links():
    """Добавляет в shift_tasks: тип задания (ТО, замена датчика, неисправность), привязку к технике/оборудованию."""
    if not os.path.exists(DATABASE):
        return False
    try:
        conn = sqlite3.connect(DATABASE)
        cur = conn.execute("PRAGMA table_info(shift_tasks)").fetchall()
        columns = [row[1] for row in cur]
        if "task_type" not in columns:
            conn.execute(
                "ALTER TABLE shift_tasks ADD COLUMN task_type TEXT NOT NULL DEFAULT 'other'"
            )
        if "machine_id" not in columns:
            conn.execute(
                "ALTER TABLE shift_tasks ADD COLUMN machine_id INTEGER REFERENCES machines(id)"
            )
        if "equipment_id" not in columns:
            conn.execute(
                "ALTER TABLE shift_tasks ADD COLUMN equipment_id INTEGER REFERENCES equipment(id)"
            )
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"Ошибка миграции shift_tasks task_type/links: {e}")
        return False


def migrate_shift_task_types_table():
    """Справочник типов заданий (ТО, замена датчика и т.д.) и привязка заданий к нему."""
    if not os.path.exists(DATABASE):
        return False
    try:
        conn = sqlite3.connect(DATABASE)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS shift_task_types (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                sort_order INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        if conn.execute("SELECT COUNT(*) FROM shift_task_types").fetchone()[0] == 0:
            conn.execute(
                "INSERT INTO shift_task_types (id, name, sort_order) VALUES (1, 'ТО', 1), (2, 'Замена датчика', 2), (3, 'Неисправность', 3), (4, 'Другое', 99)"
            )
        cur = conn.execute("PRAGMA table_info(shift_tasks)").fetchall()
        columns = [row[1] for row in cur]
        if "task_type_id" not in columns:
            conn.execute(
                "ALTER TABLE shift_tasks ADD COLUMN task_type_id INTEGER REFERENCES shift_task_types(id)"
            )
            conn.execute(
                "UPDATE shift_tasks SET task_type_id = CASE task_type WHEN 'maintenance' THEN 1 WHEN 'replace_sensor' THEN 2 WHEN 'malfunction' THEN 3 ELSE 4 END WHERE task_type_id IS NULL"
            )
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"Ошибка миграции shift_task_types: {e}")
        return False


def migrate_shift_task_completions():
    """Таблица исполнителей задания (несколько человек могут отметить выполнение)."""
    if not os.path.exists(DATABASE):
        return False
    try:
        conn = sqlite3.connect(DATABASE)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS shift_task_completions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                shift_task_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                completed_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                FOREIGN KEY (shift_task_id) REFERENCES shift_tasks(id),
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_shift_task_completions_task ON shift_task_completions (shift_task_id)"
        )
        conn.execute(
            """
            INSERT INTO shift_task_completions (shift_task_id, user_id, completed_at)
            SELECT id, completed_by_user_id, COALESCE(completed_at, datetime('now','localtime'))
            FROM shift_tasks
            WHERE status = 'done' AND completed_by_user_id IS NOT NULL
            AND id NOT IN (SELECT shift_task_id FROM shift_task_completions)
            """
        )
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"Ошибка миграции shift_task_completions: {e}")
        return False


def migrate_shift_task_done_comment_and_not_done():
    """Комментарий к выполнению (что сделано) и статус «не выполнено» с причиной."""
    if not os.path.exists(DATABASE):
        return False
    try:
        conn = sqlite3.connect(DATABASE)
        cur = conn.execute("PRAGMA table_info(shift_task_completions)").fetchall()
        cols = [row[1] for row in cur]
        if "comment" not in cols:
            conn.execute("ALTER TABLE shift_task_completions ADD COLUMN comment TEXT")
        cur = conn.execute("PRAGMA table_info(shift_tasks)").fetchall()
        cols = [row[1] for row in cur]
        if "not_done_reason" in cols:
            conn.commit()
            conn.close()
            return True
        conn.execute("ALTER TABLE shift_tasks ADD COLUMN not_done_reason TEXT")
        conn.execute("ALTER TABLE shift_tasks ADD COLUMN not_done_at TEXT")
        conn.execute("ALTER TABLE shift_tasks ADD COLUMN not_done_by_user_id INTEGER REFERENCES users(id)")
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"Ошибка миграции shift_task_done_comment_and_not_done: {e}")
        return False


def migrate_shift_tasks_status_not_done():
    """Разрешить status = 'not_done' в shift_tasks (SQLite: пересоздать таблицу)."""
    if not os.path.exists(DATABASE):
        return False
    try:
        conn = sqlite3.connect(DATABASE)
        cur = conn.execute("PRAGMA table_info(shift_tasks)").fetchall()
        cols = [r[1] for r in cur]
        if "not_done_reason" not in cols:
            conn.close()
            return True
        # Пересоздаём таблицу только если CHECK ещё не разрешает 'not_done'.
        # Раньше таблица пересоздавалась при каждом запуске, а с несколькими
        # воркерами gunicorn это могло происходить одновременно.
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'shift_tasks'"
        ).fetchone()
        if row and row[0] and "'not_done'" in row[0]:
            conn.execute("ROLLBACK")
            conn.close()
            return True
        conn.execute("DROP TABLE IF EXISTS shift_tasks_new")
        conn.execute(
            """
            CREATE TABLE shift_tasks_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                created_by_user_id INTEGER NOT NULL,
                for_shift TEXT NOT NULL CHECK (for_shift IN ('day', 'night')),
                effective_date TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT,
                status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'done', 'not_done')),
                completed_at TEXT,
                completed_by_user_id INTEGER,
                task_type TEXT NOT NULL DEFAULT 'other',
                machine_id INTEGER REFERENCES machines(id),
                equipment_id INTEGER REFERENCES equipment(id),
                task_type_id INTEGER REFERENCES shift_task_types(id),
                not_done_reason TEXT,
                not_done_at TEXT,
                not_done_by_user_id INTEGER REFERENCES users(id),
                FOREIGN KEY (created_by_user_id) REFERENCES users(id),
                FOREIGN KEY (completed_by_user_id) REFERENCES users(id)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO shift_tasks_new (id, created_at, created_by_user_id, for_shift, effective_date, title, description,
                status, completed_at, completed_by_user_id, task_type, machine_id, equipment_id, task_type_id, not_done_reason, not_done_at, not_done_by_user_id)
            SELECT id, created_at, created_by_user_id, for_shift, effective_date, title, description,
                status, completed_at, completed_by_user_id, task_type, machine_id, equipment_id, task_type_id, not_done_reason, not_done_at, not_done_by_user_id
            FROM shift_tasks
            """
        )
        conn.execute("DROP TABLE shift_tasks")
        conn.execute("ALTER TABLE shift_tasks_new RENAME TO shift_tasks")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_shift_tasks_effective_for ON shift_tasks (effective_date, for_shift)")
        conn.execute("COMMIT")
        conn.close()
        return True
    except Exception as e:
        try:
            conn.execute("ROLLBACK")
            conn.close()
        except Exception:
            pass
        print(f"Ошибка миграции shift_tasks_status_not_done: {e}")
        return False


MIGRATIONS = (
    migrate_equipment_unique_constraint,
    migrate_sensor_positions_group,
    migrate_sensor_positions_labels,
    migrate_sensors_into_equipment,
    migrate_operations_log_encoding,
    migrate_machine_maintenance,
    migrate_app_settings,
    migrate_operations_log_username,
    migrate_indexes,
    migrate_sim_cards_operator,
    migrate_sim_cards_description_tariff,
    migrate_users,
    migrate_users_profile_and_roles,
    migrate_login_attempts,
    migrate_shift_tasks,
    migrate_shift_tasks_type_and_links,
    migrate_shift_task_types_table,
    migrate_shift_task_completions,
    migrate_shift_task_done_comment_and_not_done,
    migrate_shift_tasks_status_not_done,
)


def run_migrations():
    """Применить все миграции схемы по порядку (каждая безопасна при повторном запуске)."""
    for migration in MIGRATIONS:
        migration()


def init_database():
    """Создать базу из schema.sql."""
    with open(SCHEMA_PATH, encoding="utf-8") as f:
        conn = sqlite3.connect(DATABASE)
        conn.executescript(f.read())
        conn.commit()
        conn.close()


# Роли:
# - admin — полный доступ и управление пользователями
# - editor — изменение данных (склад, техника, SIM, ТО)
# - maintenance — только операции ТО (запись техобслуживания), без прав на склад
# - reader — только просмотр
def _user_role(row):
    """Принимает sqlite3.Row или dict."""
    if not row:
        return "reader"
    try:
        role = row.get("role", "")
    except AttributeError:
        role = row["role"] if "role" in row else ""
    r = (role or "").strip().lower()
    return r if r in ("admin", "editor", "maintenance", "reader") else "reader"


SQLITE_INT_MAX = 2**63 - 1


def to_int(value, default=None):
    """Безопасно превращает строку из формы/URL в целое число (иначе default)."""
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    if abs(number) > SQLITE_INT_MAX:
        return default
    return number


def to_iso_date(value):
    """Строка 'YYYY-MM-DD' (допускается хвост со временем) -> 'YYYY-MM-DD' или None, если это не дата."""
    try:
        return date.fromisoformat(str(value or "").strip()[:10]).isoformat()
    except ValueError:
        return None


def _is_safe_next_url(url):
    """Разрешён только относительный путь внутри сайта: /machines?q=1.

    Отсекает //host, /\\host, https:host, javascript: и подобные варианты,
    которыми можно увести пользователя на чужой сайт после входа.
    """
    if not url or not url.startswith("/") or url.startswith("//") or "\\" in url:
        return False
    parsed = urlparse(url)
    return not parsed.scheme and not parsed.netloc


def row_to_dict(row):
    """Приводит sqlite3.Row к dict; для None возвращает None."""
    return dict(row) if row else None


def _is_allowed_backup_path(path):
    """Путь разрешён только внутри BACKUP_ROOT (и его поддиректорий)."""
    if not path or not path.strip():
        return False
    try:
        os.makedirs(BACKUP_ROOT, exist_ok=True)
        real_path = os.path.realpath(os.path.abspath(path.strip()))
        if not os.path.isdir(real_path):
            return False
        root_real = os.path.realpath(BACKUP_ROOT)
        return real_path == root_real or real_path.startswith(root_real + os.sep)
    except OSError:
        return False


def snapshot_database(dest_path):
    """Согласованная копия базы (SQLite online backup) в файл dest_path."""
    src = sqlite3.connect(DATABASE)
    dst = sqlite3.connect(dest_path)
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()


def _acquire_backup_lock():
    """Захват блокировки для создания бэкапа (один процесс на машину). Возвращает открытый fd или None."""
    deadline = time.time() + 60
    while time.time() < deadline:
        if os.path.exists(BACKUP_LOCK_PATH):
            try:
                if time.time() - os.path.getmtime(BACKUP_LOCK_PATH) > LOCK_STALE_SEC:
                    os.unlink(BACKUP_LOCK_PATH)
            except OSError:
                pass
        try:
            fd = os.open(BACKUP_LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            return fd
        except FileExistsError:
            time.sleep(0.5)
            continue
    return None


def _release_backup_lock(fd):
    """Освобождение блокировки бэкапа."""
    if fd is not None:
        try:
            os.close(fd)
            os.unlink(BACKUP_LOCK_PATH)
        except OSError:
            pass


class User(UserMixin):
    def __init__(self, id_, username, role="reader", surname=None, first_name=None, position=None, personnel_number=None):
        self.id = id_
        self.username = username
        self.role = role
        self.surname = surname or ""
        self.first_name = first_name or ""
        self.position = position or ""
        self.personnel_number = personnel_number or ""

    @property
    def is_admin(self):
        return self.role == "admin"

    @property
    def can_edit(self):
        return self.role in ("admin", "editor")

    def display_name(self):
        if self.surname or self.first_name:
            return f"{self.surname} {self.first_name}".strip() or self.username
        return self.username


def _format_date_ddmmyy(value):
    """Форматирует дату в дд.мм.гг. Принимает строку YYYY-MM-DD или datetime с датой."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return ""
    s = str(value).strip()[:10]
    if len(s) != 10 or s[4] != "-" or s[7] != "-":
        return value
    try:
        y, m, d = s[:4], s[5:7], s[8:10]
        return f"{d}.{m}.{y[2:]}"  # дд.мм.гг
    except (IndexError, TypeError):
        return value


def _format_datetime_ddmmyy_hhmm(value):
    """Форматирует дату и время: YYYY-MM-DD HH:MM:SS -> дд.мм.гг ЧЧ:ММ."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return ""
    s = str(value).strip()
    if len(s) < 10 or s[4] != "-" or s[7] != "-":
        return value
    try:
        d, m, y = s[8:10], s[5:7], s[2:4]  # дд, мм, гг (год из YYYY -> последние 2 цифры)
        time_part = s[11:16] if len(s) >= 16 and s[10] in " T" else ""
        return f"{d}.{m}.{y}" + (f" {time_part}" if time_part else "")
    except (IndexError, TypeError):
        return value


def _format_backup_date(value):
    """Форматирует дату/время резервной копии: YYYY-MM-DD HH:MM -> DD-MM-YYYY HH:MM."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return ""
    s = str(value).strip()
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        d, m, y = s[8:10], s[5:7], s[:4]
        time_part = s[11:16] if len(s) >= 16 and s[10] == " " else ""
        return f"{d}-{m}-{y}" + (f" {time_part}" if time_part else "")
    return value


def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY") or os.environ.get(
        "SECRET_KEY"
    ) or "dev-change-in-production"
    if app.config["SECRET_KEY"] == "dev-change-in-production" or len(app.config["SECRET_KEY"]) < 32:
        print(
            "ВНИМАНИЕ: FLASK_SECRET_KEY не задан или короче 32 символов. "
            "Задайте длинный случайный ключ в .env, например: "
            "python -c \"import secrets; print(secrets.token_hex(32))\""
        )
    app.config["DATABASE"] = DATABASE
    CSRFProtect(app)

    app.jinja_env.filters["datefmt"] = _format_date_ddmmyy
    app.jinja_env.filters["datetimefmt"] = _format_datetime_ddmmyy_hhmm

    login_manager = LoginManager(app)
    login_manager.login_view = "login"
    login_manager.login_message = "Войдите в систему для доступа к этой странице."

    @login_manager.user_loader
    def load_user(user_id):
        if not user_id:
            return None
        conn = sqlite3.connect(app.config["DATABASE"])
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT id, username, role, surname, first_name, position, personnel_number FROM users WHERE id = ?",
            (int(user_id),),
        ).fetchone()
        conn.close()
        if row is None:
            return None
        row = dict(row)
        role = _user_role(row)
        return User(
            id_=row["id"],
            username=row["username"],
            role=role,
            surname=row.get("surname"),
            first_name=row.get("first_name"),
            position=row.get("position"),
            personnel_number=row.get("personnel_number"),
        )

    # ===== Database helpers =====
    # Для одновременной работы нескольких пользователей: timeout — ждать блокировку до 15 с,
    # WAL — один пишущий и несколько читающих без блокировки друг друга.
    def get_db():
        if "db" not in g:
            g.db = sqlite3.connect(
                app.config["DATABASE"],
                timeout=15.0,  # ждать освобождения блокировки (сек)
            )
            g.db.execute("PRAGMA journal_mode=WAL")  # лучше при конкурентном доступе
            g.db.row_factory = sqlite3.Row
            g.db.create_function(
                "CASEFOLD",
                1,
                lambda s: s.casefold() if isinstance(s, str) else "",
            )
        return g.db

    @app.teardown_appcontext
    def close_db(error):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    @app.cli.command("init-db")
    def init_db_command():
        """Инициализация базы данных из schema.sql и применение миграций."""
        init_database()
        run_migrations()
        print("База данных инициализирована.")

    @app.cli.command("create-user")
    @click.option("--username", "-u", required=True, help="Логин пользователя")
    @click.option("--password", "-p", required=True, help="Пароль")
    @click.option("--role", "-r", default="reader", type=click.Choice(["admin", "editor", "maintenance", "reader"]), help="Роль: admin, editor, maintenance, reader")
    def create_user_command(username, password, role):
        """Создать пользователя для входа в систему."""
        migrate_users()
        migrate_users_profile_and_roles()
        conn = sqlite3.connect(app.config["DATABASE"])
        existing = conn.execute("SELECT id FROM users WHERE username = ?", (username.strip(),)).fetchone()
        if existing:
            print(f"Пользователь '{username}' уже существует.")
            conn.close()
            return
        count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if count == 0:
            role = "admin"
            print("Первый пользователь создаётся с ролью «Администратор».")
        pw_hash = generate_password_hash(password, method="scrypt")
        conn.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
            (username.strip(), pw_hash, role),
        )
        conn.commit()
        conn.close()
        print(f"Пользователь '{username}' создан (роль: {role}).")

    def log_operation(operation_type: str, entity_type: str, entity_id: int, description: str):
        from flask_login import current_user
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

    @app.context_processor
    def inject_tooltips_and_recent():
        """Подсказки для статусов и список недавно открытых для шапки."""
        hints_equipment = {
            "На складе": "Оборудование на складе, не установлено на технику",
            "Установлено": "Установлено на технику",
            "Демонтировано": "Снято с техники",
            "Неисправно": "Требует ремонта или диагностики",
            "Списано": "Снято с учёта",
        }
        hints_sim = {
            "На складе": "SIM-карта на складе",
            "Установлена": "Установлена на технику",
            "Заблокирована": "SIM заблокирована оператором",
            "Списана": "Снята с учёта",
        }
        recent = session.get("recent") or []
        recent_items = []
        for item in recent[-10:]:  # последние 10
            t, iid, label = item.get("type"), item.get("id"), item.get("label") or ""
            if t == "machine" and iid:
                recent_items.append({"url": url_for("machine_detail", machine_id=iid), "label": label or f"Техника {iid}"})
            elif t == "equipment" and iid:
                recent_items.append({"url": url_for("equipment_detail", equipment_id=iid), "label": label or f"Оборудование {iid}"})
        return {
            "status_hints_equipment": hints_equipment,
            "status_hints_sim": hints_sim,
            "recent_items": list(reversed(recent_items)),  # новые сверху
        }

    # ===== Simple helpers for lookups =====

    def fetch_all(query, params=()):
        db = get_db()
        return db.execute(query, params).fetchall()

    def fetch_one(query, params=()):
        db = get_db()
        return db.execute(query, params).fetchone()

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
        db.execute(
            "INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)",
            (key, value),
        )
        db.commit()

    # ===== Auth: require login for all routes except login =====
    @app.before_request
    def require_login():
        if not current_user.is_authenticated:
            if request.endpoint == "login" or (request.endpoint and request.endpoint.startswith("static")):
                return None
            return redirect(url_for("login", next=request.url))
        # Ограничение прав по ролям
        role = getattr(current_user, "role", None)
        if request.method not in ("GET", "HEAD"):
            # Роль reader — только просмотр (запрет любых изменений)
            if role == "reader":
                flash("Доступ только для просмотра. Изменения запрещены.", "error")
                return redirect(request.referrer or url_for("index"))
            # Роль maintenance — разрешено записывать ТО и работать с заданиями на смену (создавать, выполнять, отмечать не выполнено, редактировать/удалять свои)
            if role == "maintenance":
                allowed_endpoints = {
                    "maintenance_add",
                    "shift_tasks_list",
                    "shift_task_complete",
                    "shift_task_not_done",
                    "shift_task_edit",
                    "shift_task_delete",
                }
                if request.endpoint not in allowed_endpoints:
                    flash("Этому пользователю разрешено только записывать ТО и работать с заданиями на смену.", "error")
                    return redirect(request.referrer or url_for("index"))
        return None

    @app.after_request
    def set_security_headers(resp):
        """Базовые заголовки безопасности для всех ответов."""
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        return resp

    @app.errorhandler(500)
    def handle_500(err):
        """Централизованная обработка внутренних ошибок сервера."""
        try:
            return render_template("500.html"), 500
        except Exception:
            return "<h1>Ошибка сервера</h1><p>Произошла внутренняя ошибка. Попробуйте позже или обратитесь к администратору.</p>", 500

    # ===== Routes =====

    # Ограничение попыток входа: по IP в БД (таблица login_attempts), после 5 неудач блокировка на 2 минуты

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if current_user.is_authenticated:
            return redirect(url_for("index"))
        error = None
        ip = request.remote_addr or "unknown"
        now_ts = time.time()
        try:
            db = get_db()
            db.execute("DELETE FROM login_attempts WHERE lock_until IS NOT NULL AND lock_until < ?", (now_ts,))
            db.commit()
            rec = db.execute("SELECT count, lock_until FROM login_attempts WHERE ip = ?", (ip,)).fetchone()
            if rec and rec["lock_until"] and rec["lock_until"] > now_ts:
                left = int((rec["lock_until"] - now_ts) / 60) + 1
                return render_template("login.html", error=f"Слишком много неудачных попыток. Попробуйте через {left} мин.")
        except Exception:
            pass
        if request.method == "POST":
            username = (request.form.get("username") or "").strip()
            password = request.form.get("password") or ""
            if not username or not password:
                error = "Введите логин и пароль."
            else:
                conn = sqlite3.connect(app.config["DATABASE"])
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT id, username, password_hash, role, surname, first_name, position, personnel_number FROM users WHERE username = ?",
                    (username,),
                ).fetchone()
                conn.close()
                if row is None or not check_password_hash(row["password_hash"], password):
                    try:
                        db = get_db()
                        rec = db.execute("SELECT count, lock_until FROM login_attempts WHERE ip = ?", (ip,)).fetchone()
                        count = (rec["count"] + 1) if rec else 1
                        lock_until = (now_ts + 120) if count >= 5 else (rec["lock_until"] if rec else None)
                        db.execute(
                            "INSERT OR REPLACE INTO login_attempts (ip, count, lock_until) VALUES (?, ?, ?)",
                            (ip, count, lock_until),
                        )
                        db.commit()
                    except Exception:
                        pass
                    error = "Неверный логин или пароль."
                else:
                    try:
                        db = get_db()
                        db.execute("DELETE FROM login_attempts WHERE ip = ?", (ip,))
                        db.commit()
                    except Exception:
                        pass
                    row = dict(row)
                    user = User(
                        id_=row["id"],
                        username=row["username"],
                        role=_user_role(row),
                        surname=row.get("surname"),
                        first_name=row.get("first_name"),
                        position=row.get("position"),
                        personnel_number=row.get("personnel_number"),
                    )
                    login_user(user, remember=bool(request.form.get("remember")))
                    next_url = request.form.get("next") or request.args.get("next") or ""
                    if not _is_safe_next_url(next_url):
                        next_url = url_for("index")
                    return redirect(next_url)
        return render_template("login.html", error=error)

    @app.route("/logout")
    def logout():
        logout_user()
        flash("Вы вышли из системы.", "success")
        return redirect(url_for("login"))

    # ===== Пользователи и профиль (admin — управление пользователями) =====
    def _require_admin():
        if not current_user.is_authenticated or not getattr(current_user, "is_admin", False):
            flash("Доступ только для администратора.", "error")
            return redirect(url_for("index"))

    @app.route("/users")
    @login_required
    def users_list():
        r = _require_admin()
        if r is not None:
            return r
        users_rows = fetch_all(
            "SELECT id, username, surname, first_name, position, personnel_number, role, created_at FROM users ORDER BY username"
        )
        users = [dict(r) for r in users_rows]
        return render_template("users_list.html", users=users)

    @app.route("/users/add", methods=["GET", "POST"])
    @login_required
    def user_add():
        r = _require_admin()
        if r is not None:
            return r
        if request.method == "POST":
            username = (request.form.get("username") or "").strip()
            password = request.form.get("password") or ""
            surname = (request.form.get("surname") or "").strip() or None
            first_name = (request.form.get("first_name") or "").strip() or None
            position = (request.form.get("position") or "").strip() or None
            personnel_number = (request.form.get("personnel_number") or "").strip() or None
            role = (request.form.get("role") or "").strip().lower()
            if role not in ("admin", "editor", "maintenance", "reader"):
                role = "reader"
            if not username or not password:
                flash("Укажите логин и пароль.", "error")
            else:
                existing = fetch_one("SELECT id FROM users WHERE username = ?", (username,))
                if existing:
                    flash("Пользователь с таким логином уже существует.", "error")
                else:
                    db = get_db()
                    db.execute(
                        """
                        INSERT INTO users (username, password_hash, surname, first_name, position, personnel_number, role)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (username, generate_password_hash(password, method="scrypt"), surname, first_name, position, personnel_number, role),
                    )
                    db.commit()
                    flash("Пользователь создан.", "success")
                    return redirect(url_for("users_list"))
        return render_template("user_form.html", user=None)

    @app.route("/users/<int:user_id>/edit", methods=["GET", "POST"])
    @login_required
    def user_edit(user_id):
        r = _require_admin()
        if r is not None:
            return r
        user = fetch_one(
            "SELECT id, username, surname, first_name, position, personnel_number, role FROM users WHERE id = ?",
            (user_id,),
        )
        if user is None:
            flash("Пользователь не найден.", "error")
            return redirect(url_for("users_list"))
        user = dict(user)
        if request.method == "POST":
            surname = (request.form.get("surname") or "").strip() or None
            first_name = (request.form.get("first_name") or "").strip() or None
            position = (request.form.get("position") or "").strip() or None
            personnel_number = (request.form.get("personnel_number") or "").strip() or None
            role = (request.form.get("role") or "").strip().lower()
            if role not in ("admin", "editor", "maintenance", "reader"):
                role = "reader"
            new_password = request.form.get("new_password") or ""
            db = get_db()
            if new_password:
                db.execute(
                    "UPDATE users SET surname=?, first_name=?, position=?, personnel_number=?, role=?, password_hash=? WHERE id=?",
                    (surname, first_name, position, personnel_number, role, generate_password_hash(new_password, method="scrypt"), user_id),
                )
                flash("Пользователь обновлён (пароль изменён).", "success")
            else:
                db.execute(
                    "UPDATE users SET surname=?, first_name=?, position=?, personnel_number=?, role=? WHERE id=?",
                    (surname, first_name, position, personnel_number, role, user_id),
                )
                flash("Пользователь обновлён.", "success")
            db.commit()
            return redirect(url_for("users_list"))
        return render_template("user_form.html", user=user)

    @app.route("/profile")
    @login_required
    def profile():
        return render_template("profile.html")

    @app.route("/profile/edit", methods=["GET", "POST"])
    @login_required
    def profile_edit():
        if request.method == "POST":
            surname = (request.form.get("surname") or "").strip() or None
            first_name = (request.form.get("first_name") or "").strip() or None
            position = (request.form.get("position") or "").strip() or None
            personnel_number = (request.form.get("personnel_number") or "").strip() or None
            db = get_db()
            db.execute(
                "UPDATE users SET surname=?, first_name=?, position=?, personnel_number=? WHERE id=?",
                (surname, first_name, position, personnel_number, current_user.id),
            )
            db.commit()
            flash("Профиль сохранён.", "success")
            return redirect(url_for("profile"))
        return render_template("profile_edit.html")

    @app.route("/")
    def index():
        # Сводка для дашборда
        machines_total = fetch_one("SELECT COUNT(*) AS c FROM machines", ())["c"]
        equipment_total = fetch_one("SELECT COUNT(*) AS c FROM equipment", ())["c"]
        equipment_warehouse = fetch_one(
            "SELECT COUNT(*) AS c FROM equipment WHERE status_id = 1", ()
        )["c"]
        equipment_installed = fetch_one(
            "SELECT COUNT(*) AS c FROM equipment WHERE status_id = 2", ()
        )["c"]
        sim_total = fetch_one("SELECT COUNT(*) AS c FROM sim_cards", ())["c"]
        sim_warehouse = fetch_one(
            "SELECT COUNT(*) AS c FROM sim_cards WHERE status_id = 1", ()
        )["c"]
        sim_installed = fetch_one(
            "SELECT COUNT(*) AS c FROM sim_cards WHERE status_id = 2", ()
        )["c"]
        machines_with_equipment = fetch_one(
            """
            SELECT COUNT(DISTINCT machine_id) AS c
            FROM machine_equipment WHERE removed_at IS NULL
            """,
            (),
        )["c"]
        machines_with_sim = fetch_one(
            """
            SELECT COUNT(*) AS c FROM machine_sim_cards WHERE removed_at IS NULL
            """,
            (),
        )["c"]
        ops_today = fetch_one(
            """
            SELECT COUNT(*) AS c FROM operations_log
            WHERE date(created_at, 'localtime') = date('now', 'localtime')
            """,
            (),
        )["c"]
        ops_week = fetch_one(
            """
            SELECT COUNT(*) AS c FROM operations_log
            WHERE datetime(created_at, 'localtime') >= datetime('now', 'localtime', '-7 days')
            """,
            (),
        )["c"]
        today_iso = date.today().isoformat()
        due_soon_end = (date.today() + timedelta(days=14)).isoformat()
        maintenance_overdue = fetch_one(
            """
            SELECT COUNT(*) AS c FROM machines m
            WHERE (SELECT next_due_at FROM machine_maintenance WHERE machine_id = m.id ORDER BY id DESC LIMIT 1) < ?
            """,
            (today_iso,),
        )["c"]
        maintenance_due_soon = fetch_one(
            """
            SELECT COUNT(*) AS c FROM machines m
            WHERE (SELECT next_due_at FROM machine_maintenance WHERE machine_id = m.id ORDER BY id DESC LIMIT 1) BETWEEN ? AND ?
            """,
            (today_iso, due_soon_end),
        )["c"]
        recent_ops = fetch_all(
            """
            SELECT id, datetime(created_at, 'localtime') AS created_at, operation_type, entity_type, entity_id, description, username
            FROM operations_log
            ORDER BY created_at DESC, id DESC
            LIMIT 10
            """
        )
        recent_ops_fixed = []
        for row in recent_ops:
            op = dict(row)
            if op.get("description"):
                op["description"] = fix_mojibake_text(op["description"])
            recent_ops_fixed.append(op)
        # Ближайшие ТО: техника с датой следующего ТО (сначала просроченные, потом по дате)
        maintenance_upcoming = fetch_all(
            """
            SELECT m.id, m.inventory_number, mm.name AS model_name,
                   (SELECT next_due_at FROM machine_maintenance WHERE machine_id = m.id ORDER BY id DESC LIMIT 1) AS next_due_at
            FROM machines m
            JOIN machine_models mm ON m.machine_model_id = mm.id
            WHERE (SELECT next_due_at FROM machine_maintenance WHERE machine_id = m.id ORDER BY id DESC LIMIT 1) IS NOT NULL
            ORDER BY next_due_at ASC
            LIMIT 7
            """
        )
        maintenance_upcoming = [dict(r) for r in maintenance_upcoming]
        for m in maintenance_upcoming:
            nd = m.get("next_due_at")
            if not nd:
                m["to_status"] = "no_data"
            elif nd < today_iso:
                m["to_status"] = "overdue"
            elif nd <= due_soon_end:
                m["to_status"] = "due_soon"
            else:
                m["to_status"] = "ok"
        # Задания на смену — сегодня
        shift_tasks_today_pending = fetch_one(
            "SELECT COUNT(*) AS c FROM shift_tasks WHERE effective_date = ? AND status = 'pending'",
            (today_iso,),
        )["c"]
        shift_tasks_today_done = fetch_one(
            "SELECT COUNT(*) AS c FROM shift_tasks WHERE effective_date = ? AND status = 'done'",
            (today_iso,),
        )["c"]
        shift_tasks_today_not_done = fetch_one(
            "SELECT COUNT(*) AS c FROM shift_tasks WHERE effective_date = ? AND status = 'not_done'",
            (today_iso,),
        )["c"]
        shift_tasks_today_list = fetch_all(
            """
            SELECT st.id, st.for_shift, st.title, st.status, tt.name AS task_type_name
            FROM shift_tasks st
            LEFT JOIN shift_task_types tt ON st.task_type_id = tt.id
            WHERE st.effective_date = ?
            ORDER BY st.for_shift, st.created_at DESC
            LIMIT 8
            """,
            (today_iso,),
        )
        shift_tasks_today_list = [dict(r) for r in shift_tasks_today_list]
        return render_template(
            "dashboard.html",
            machines_total=machines_total,
            equipment_total=equipment_total,
            equipment_warehouse=equipment_warehouse,
            equipment_installed=equipment_installed,
            sim_total=sim_total,
            sim_warehouse=sim_warehouse,
            sim_installed=sim_installed,
            machines_with_equipment=machines_with_equipment,
            machines_with_sim=machines_with_sim,
            ops_today=ops_today,
            ops_week=ops_week,
            maintenance_overdue=maintenance_overdue,
            maintenance_due_soon=maintenance_due_soon,
            maintenance_upcoming=maintenance_upcoming,
            shift_tasks_today_pending=shift_tasks_today_pending,
            shift_tasks_today_done=shift_tasks_today_done,
            shift_tasks_today_not_done=shift_tasks_today_not_done,
            shift_tasks_today_list=shift_tasks_today_list,
            recent_ops=recent_ops_fixed,
            last_backup_at=_format_backup_date(get_setting("last_backup_at", "")),
        )

    @app.route("/search")
    def quick_search():
        """Быстрый поиск по инв. номеру техники или серийному номеру оборудования."""
        q = (request.args.get("q") or "").strip()[:100]
        if not q:
            return redirect(url_for("index"))
        machine = fetch_one("SELECT id FROM machines WHERE inventory_number = ?", (q,))
        if machine:
            return redirect(url_for("machine_detail", machine_id=machine["id"]))
        equip = fetch_one("SELECT id FROM equipment WHERE serial_number = ?", (q,))
        if equip:
            return redirect(url_for("equipment_detail", equipment_id=equip["id"]))
        machines = fetch_all("SELECT id FROM machines WHERE inventory_number LIKE ?", (f"%{q}%",))
        if len(machines) == 1:
            return redirect(url_for("machine_detail", machine_id=machines[0]["id"]))
        if len(machines) > 1:
            return redirect(url_for("machines_list", q=q))
        equip_list = fetch_all(
            "SELECT id FROM equipment WHERE serial_number LIKE ? OR name LIKE ?",
            (f"%{q}%", f"%{q}%"),
        )
        if len(equip_list) == 1:
            return redirect(url_for("equipment_detail", equipment_id=equip_list[0]["id"]))
        if len(equip_list) > 1:
            return redirect(url_for("equipment_list", q=q))
        flash(f"По запросу «{q}» ничего не найдено.", "error")
        return redirect(url_for("index"))

    # --- Equipment ---

    @app.route("/equipment")
    def equipment_list():
        try:
            per_page = max(1, min(500, int(request.args.get("per_page", 50) or 50)))
        except (TypeError, ValueError):
            per_page = 50
        try:
            page = max(1, min(100000, to_int(request.args.get("page"), 1)))
        except (TypeError, ValueError):
            page = 1
        status_id = to_int(request.args.get("status"))
        q = (request.args.get("q") or "").strip()[:100]
        where, params = [], ()
        if status_id is not None:
            where.append("e.status_id = ?")
            params = (*params, status_id)
        if q:
            q_lower = q.lower().strip()
            # По наименованию: как есть и с заглавной (SQLite LOWER не работает для кириллицы)
            # По серийному: как есть и в нижнем регистре
            where.append(
                "(e.name LIKE ? OR e.name LIKE ? OR e.serial_number LIKE ? OR LOWER(e.serial_number) LIKE ?)"
            )
            params = (*params, f"%{q}%", f"%{q.capitalize()}%", f"%{q}%", f"%{q_lower}%")
        where_sql = (" AND " + " AND ".join(where)) if where else ""
        total = fetch_one(
            f"SELECT COUNT(*) AS c FROM equipment e JOIN equipment_statuses s ON e.status_id = s.id WHERE 1=1{where_sql}",
            params,
        )["c"]
        total_pages = max(1, (total + per_page - 1) // per_page)
        page = min(page, total_pages)
        offset = (page - 1) * per_page
        sort_cols = {"id": "e.id", "name": "e.name", "serial_number": "e.serial_number", "status": "s.name", "added_at": "e.added_at"}
        sort_key = request.args.get("sort", "id")
        order_key = request.args.get("order", "desc")
        sort_sql = sort_cols.get(sort_key, "e.id")
        order_sql = "DESC" if order_key == "desc" else "ASC"
        equipment = fetch_all(
            f"""
            SELECT
                e.*,
                s.name AS status_name,
                m.id AS machine_id,
                m.inventory_number,
                mm.name AS machine_model_name
            FROM equipment e
            JOIN equipment_statuses s ON e.status_id = s.id
            LEFT JOIN machine_equipment me
                ON me.equipment_id = e.id AND me.removed_at IS NULL
            LEFT JOIN machines m ON me.machine_id = m.id
            LEFT JOIN machine_models mm ON m.machine_model_id = mm.id
            WHERE 1=1{where_sql}
            ORDER BY {sort_sql} {order_sql}
            LIMIT ? OFFSET ?
            """,
            (*params, per_page, offset),
        )
        statuses = fetch_all("SELECT * FROM equipment_statuses ORDER BY id")
        return render_template(
            "equipment_list.html",
            equipment=equipment,
            statuses=statuses,
            pagination={"page": page, "per_page": per_page, "total": total, "total_pages": total_pages},
            filter_status=status_id,
            filter_q=q,
            sort=sort_key,
            order=order_key,
        )

    @app.route("/equipment/add", methods=["GET", "POST"])
    def equipment_add():
        statuses = fetch_all("SELECT * FROM equipment_statuses ORDER BY id")
        if request.method == "POST":
            name = request.form.get("name")
            serial_number = request.form.get("serial_number")
            description = request.form.get("description")
            status_id = ref_id("equipment_statuses", request.form.get("status_id"))
            if not name or not serial_number or not status_id:
                flash("Заполните обязательные поля.", "error")
            else:
                db = get_db()
                # Проверка: комбинация (наименование, серийный номер) должна быть уникальной
                existing = fetch_one(
                    "SELECT id FROM equipment WHERE name = ? AND serial_number = ?",
                    (name, serial_number),
                )
                if existing:
                    flash("Оборудование с таким наименованием и серийным номером уже существует.", "error")
                else:
                    try:
                        equipment_kind = request.form.get("equipment_kind") or "other"
                        if equipment_kind not in ("sensor", "other"):
                            equipment_kind = "other"
                        db.execute(
                            """
                            INSERT INTO equipment (name, serial_number, description, status_id, equipment_kind)
                            VALUES (?, ?, ?, ?, ?)
                            """,
                            (name, serial_number, description, status_id, equipment_kind),
                        )
                        db.commit()
                        flash("Оборудование добавлено.", "success")
                        return redirect(url_for("equipment_list"))
                    except sqlite3.IntegrityError:
                        flash("Ошибка при добавлении оборудования.", "error")
        return render_template("equipment_form.html", statuses=statuses, equipment=None)

    @app.route("/equipment/<int:equipment_id>/edit", methods=["GET", "POST"])
    def equipment_edit(equipment_id):
        statuses = fetch_all("SELECT * FROM equipment_statuses ORDER BY id")
        equipment = fetch_one("SELECT * FROM equipment WHERE id = ?", (equipment_id,))
        if equipment is None:
            flash("Оборудование не найдено.", "error")
            return redirect(url_for("equipment_list"))

        if request.method == "POST":
            name = request.form.get("name")
            serial_number = request.form.get("serial_number")
            description = request.form.get("description")
            status_id = ref_id("equipment_statuses", request.form.get("status_id"))
            if not name or not serial_number or not status_id:
                flash("Заполните обязательные поля.", "error")
            else:
                db = get_db()
                # Проверка: комбинация (наименование, серийный номер) должна быть уникальной
                # (исключая текущее редактируемое оборудование)
                existing = fetch_one(
                    "SELECT id FROM equipment WHERE name = ? AND serial_number = ? AND id != ?",
                    (name, serial_number, equipment_id),
                )
                if existing:
                    flash("Оборудование с таким наименованием и серийным номером уже существует.", "error")
                else:
                    try:
                        equipment_kind = request.form.get("equipment_kind") or "other"
                        if equipment_kind not in ("sensor", "other"):
                            equipment_kind = "other"
                        db.execute(
                            """
                            UPDATE equipment
                            SET name = ?, serial_number = ?, description = ?, status_id = ?, equipment_kind = ?
                            WHERE id = ?
                            """,
                            (name, serial_number, description, status_id, equipment_kind, equipment_id),
                        )
                        db.commit()
                        flash("Оборудование обновлено.", "success")
                        return redirect(url_for("equipment_list"))
                    except sqlite3.IntegrityError:
                        flash("Ошибка при обновлении оборудования.", "error")

        return render_template("equipment_form.html", statuses=statuses, equipment=equipment)

    @app.route("/equipment/<int:equipment_id>/delete", methods=["POST"])
    def equipment_delete(equipment_id):
        db = get_db()
        # Проверяем, установлено ли оборудование на технику
        active_install = fetch_one(
            """
            SELECT me.id, m.inventory_number
            FROM machine_equipment me
            JOIN machines m ON me.machine_id = m.id
            WHERE me.equipment_id = ? AND me.removed_at IS NULL
            LIMIT 1
            """,
            (equipment_id,),
        )
        if active_install:
            flash(
                f"Нельзя удалить оборудование: оно установлено на технику {active_install['inventory_number']}. "
                "Сначала демонтируйте оборудование.",
                "error",
            )
            return redirect(url_for("equipment_list"))
        db.execute("DELETE FROM equipment WHERE id = ?", (equipment_id,))
        db.commit()
        flash("Оборудование удалено.", "success")
        return redirect(url_for("equipment_list"))

    @app.route("/equipment/<int:equipment_id>")
    def equipment_detail(equipment_id):
        equipment = fetch_one(
            """
            SELECT e.*, s.name AS status_name
            FROM equipment e
            JOIN equipment_statuses s ON e.status_id = s.id
            WHERE e.id = ?
            """,
            (equipment_id,),
        )
        if equipment is None:
            flash("Оборудование не найдено.", "error")
            return redirect(url_for("equipment_list"))

        installs = fetch_all(
            """
            SELECT me.*, m.inventory_number
            FROM machine_equipment me
            JOIN machines m ON me.machine_id = m.id
            WHERE me.equipment_id = ?
            ORDER BY me.installed_at DESC
            """,
            (equipment_id,),
        )
        logs = fetch_all(
            """
            SELECT * FROM operations_log
            WHERE entity_type = 'equipment' AND entity_id = ?
            ORDER BY created_at DESC
            """,
            (equipment_id,),
        )
        recent = [r for r in (session.get("recent") or []) if not (r.get("type") == "equipment" and r.get("id") == equipment_id)]
        recent.append({"type": "equipment", "id": equipment_id, "label": (equipment["name"] or equipment["serial_number"] or f"#{equipment_id}")[:50]})
        session["recent"] = recent[-10:]
        return render_template("equipment_detail.html", equipment=equipment, installs=installs, logs=logs)

    # --- Machine types & models & machines ---

    @app.route("/machine_types")
    def machine_types_list():
        types_ = fetch_all(
            """
            SELECT mt.*,
                   (SELECT COUNT(*) FROM machines m
                    JOIN machine_models mm ON m.machine_model_id = mm.id
                    WHERE mm.machine_type_id = mt.id) AS machines_count
            FROM machine_types mt
            ORDER BY mt.id
            """
        )
        return render_template("machine_types_list.html", types=types_)

    # --- Sensor positions (позиции установки датчиков) ---

    @app.route("/sensor_positions")
    def sensor_positions_list():
        positions = fetch_all(
            """
            SELECT sp.*,
                   (SELECT COUNT(*) FROM machine_equipment me
                    WHERE me.sensor_position_id = sp.id AND me.removed_at IS NULL) AS used_count
            FROM sensor_positions sp
            ORDER BY COALESCE(sp.sort_order, 99), sp.id
            """
        )
        return render_template("sensor_positions_list.html", positions=positions)

    @app.route("/sensor_positions/add", methods=["GET", "POST"])
    def sensor_position_add():
        if request.method == "POST":
            code = (request.form.get("code") or "").strip()
            position_group = request.form.get("position_group") or "wheel"
            label = (request.form.get("label") or "").strip() or None
            sort_order_raw = (request.form.get("sort_order") or "").strip()
            sort_order = None
            if sort_order_raw:
                try:
                    sort_order = int(sort_order_raw)
                except ValueError:
                    flash("Порядок сортировки должен быть числом.", "error")
                    return render_template("sensor_position_form.html", position=None)
            if not code:
                flash("Введите код позиции.", "error")
            else:
                db = get_db()
                try:
                    db.execute(
                        "INSERT INTO sensor_positions (code, position_group, label, sort_order) VALUES (?, ?, ?, ?)",
                        (code, position_group, label, sort_order),
                    )
                    db.commit()
                    flash("Позиция добавлена.", "success")
                    return redirect(url_for("sensor_positions_list"))
                except sqlite3.IntegrityError:
                    flash("Позиция с таким кодом уже существует.", "error")
        return render_template("sensor_position_form.html", position=None)

    @app.route("/sensor_positions/<int:position_id>/edit", methods=["GET", "POST"])
    def sensor_position_edit(position_id):
        position = fetch_one("SELECT * FROM sensor_positions WHERE id = ?", (position_id,))
        if position is None:
            flash("Позиция не найдена.", "error")
            return redirect(url_for("sensor_positions_list"))
        if request.method == "POST":
            code = (request.form.get("code") or "").strip()
            position_group = request.form.get("position_group") or "wheel"
            label = (request.form.get("label") or "").strip() or None
            sort_order_raw = (request.form.get("sort_order") or "").strip()
            sort_order = None
            if sort_order_raw:
                try:
                    sort_order = int(sort_order_raw)
                except ValueError:
                    flash("Порядок сортировки должен быть числом.", "error")
                    return render_template("sensor_position_form.html", position=position)
            if not code:
                flash("Введите код позиции.", "error")
            else:
                db = get_db()
                try:
                    db.execute(
                        "UPDATE sensor_positions SET code = ?, position_group = ?, label = ?, sort_order = ? WHERE id = ?",
                        (code, position_group, label, sort_order, position_id),
                    )
                    db.commit()
                    flash("Позиция обновлена.", "success")
                    return redirect(url_for("sensor_positions_list"))
                except sqlite3.IntegrityError:
                    flash("Позиция с таким кодом уже существует.", "error")
        return render_template("sensor_position_form.html", position=position)

    @app.route("/sensor_positions/<int:position_id>/delete", methods=["POST"])
    def sensor_position_delete(position_id):
        position = fetch_one("SELECT * FROM sensor_positions WHERE id = ?", (position_id,))
        if position is None:
            flash("Позиция не найдена.", "error")
            return redirect(url_for("sensor_positions_list"))
        used = fetch_one(
            "SELECT 1 FROM machine_equipment WHERE sensor_position_id = ? AND removed_at IS NULL LIMIT 1",
            (position_id,),
        )
        if used:
            flash("Нельзя удалить позицию: она уже используется на технике. Сначала демонтируйте соответствующие датчики.", "error")
            return redirect(url_for("sensor_positions_list"))
        db = get_db()
        db.execute("DELETE FROM sensor_positions WHERE id = ?", (position_id,))
        db.commit()
        flash("Позиция удалена.", "success")
        return redirect(url_for("sensor_positions_list"))

    @app.route("/machine_types/add", methods=["GET", "POST"])
    def machine_type_add():
        if request.method == "POST":
            name = (request.form.get("name") or "").strip()
            if not name:
                flash("Введите название типа.", "error")
            elif fetch_one("SELECT 1 FROM machine_types WHERE name = ?", (name,)):
                flash("Тип техники с таким названием уже есть.", "error")
            else:
                db = get_db()
                db.execute("INSERT INTO machine_types (name) VALUES (?)", (name,))
                db.commit()
                flash("Тип техники добавлен.", "success")
                return redirect(url_for("machine_types_list"))
        return render_template("machine_type_form.html", machine_type=None)

    @app.route("/machine_types/<int:type_id>/edit", methods=["GET", "POST"])
    def machine_type_edit(type_id):
        type_ = fetch_one("SELECT * FROM machine_types WHERE id = ?", (type_id,))
        if type_ is None:
            flash("Тип техники не найден.", "error")
            return redirect(url_for("machine_types_list"))
        if request.method == "POST":
            name = (request.form.get("name") or "").strip()
            if not name:
                flash("Введите название типа.", "error")
            elif fetch_one("SELECT 1 FROM machine_types WHERE name = ? AND id != ?", (name, type_id)):
                flash("Тип техники с таким названием уже есть.", "error")
            else:
                db = get_db()
                db.execute("UPDATE machine_types SET name = ? WHERE id = ?", (name, type_id))
                db.commit()
                flash("Тип техники обновлён.", "success")
                return redirect(url_for("machine_types_list"))
        return render_template("machine_type_form.html", machine_type=type_)

    @app.route("/machine_types/<int:type_id>/delete", methods=["POST"])
    def machine_type_delete(type_id):
        type_ = fetch_one("SELECT name FROM machine_types WHERE id = ?", (type_id,))
        if type_ is None:
            flash("Тип техники не найден.", "error")
            return redirect(url_for("machine_types_list"))
        has_models = fetch_one(
            "SELECT id FROM machine_models WHERE machine_type_id = ? LIMIT 1",
            (type_id,),
        )
        if has_models:
            flash(
                f"Нельзя удалить тип «{type_['name']}»: к нему привязаны модели техники. Сначала удалите или измените модели.",
                "error",
            )
            return redirect(url_for("machine_types_list"))
        db = get_db()
        db.execute("DELETE FROM machine_types WHERE id = ?", (type_id,))
        db.commit()
        flash("Тип техники удалён.", "success")
        return redirect(url_for("machine_types_list"))

    @app.route("/machine_models")
    def machine_models_list():
        models = fetch_all(
            """
            SELECT
                mm.*,
                mt.name AS type_name,
                COUNT(m.id) AS machines_count
            FROM machine_models mm
            JOIN machine_types mt ON mm.machine_type_id = mt.id
            LEFT JOIN machines m ON m.machine_model_id = mm.id
            GROUP BY mm.id
            ORDER BY mm.id
            """
        )
        types_ = fetch_all("SELECT * FROM machine_types ORDER BY id")
        return render_template("machine_models_list.html", models=models, types=types_)

    @app.route("/machine_models/add", methods=["GET", "POST"])
    def machine_model_add():
        types_ = fetch_all("SELECT * FROM machine_types ORDER BY id")
        if request.method == "POST":
            name = request.form.get("name")
            machine_type_id = ref_id("machine_types", request.form.get("machine_type_id"))
            if not name or not machine_type_id:
                flash("Заполните все поля.", "error")
            else:
                db = get_db()
                db.execute(
                    "INSERT INTO machine_models (name, machine_type_id) VALUES (?, ?)",
                    (name, machine_type_id),
                )
                db.commit()
                flash("Модель техники добавлена.", "success")
                return redirect(url_for("machine_models_list"))
        return render_template("machine_model_form.html", model=None, types=types_)

    @app.route("/machine_models/<int:model_id>/edit", methods=["GET", "POST"])
    def machine_model_edit(model_id):
        types_ = fetch_all("SELECT * FROM machine_types ORDER BY id")
        model = fetch_one("SELECT * FROM machine_models WHERE id = ?", (model_id,))
        if model is None:
            flash("Модель не найдена.", "error")
            return redirect(url_for("machine_models_list"))
        if request.method == "POST":
            name = request.form.get("name")
            machine_type_id = ref_id("machine_types", request.form.get("machine_type_id"))
            if not name or not machine_type_id:
                flash("Заполните все поля.", "error")
            else:
                db = get_db()
                db.execute(
                    "UPDATE machine_models SET name = ?, machine_type_id = ? WHERE id = ?",
                    (name, machine_type_id, model_id),
                )
                db.commit()
                flash("Модель обновлена.", "success")
                return redirect(url_for("machine_models_list"))
        return render_template("machine_model_form.html", model=model, types=types_)

    @app.route("/machine_models/<int:model_id>/delete", methods=["POST"])
    def machine_model_delete(model_id):
        model = fetch_one("SELECT id, name FROM machine_models WHERE id = ?", (model_id,))
        if model is None:
            flash("Модель не найдена.", "error")
            return redirect(url_for("machine_models_list"))
        has_machines = fetch_one(
            "SELECT id FROM machines WHERE machine_model_id = ? LIMIT 1",
            (model_id,),
        )
        if has_machines:
            flash(
                f"Нельзя удалить модель «{model['name']}»: к ней привязана техника. Сначала удалите или перепривяжите технику.",
                "error",
            )
            return redirect(url_for("machine_models_list"))
        db = get_db()
        db.execute("DELETE FROM machine_models WHERE id = ?", (model_id,))
        db.commit()
        flash("Модель удалена.", "success")
        return redirect(url_for("machine_models_list"))

    @app.route("/machines")
    def machines_list():
        try:
            per_page = max(1, min(500, int(request.args.get("per_page", 50) or 50)))
        except (TypeError, ValueError):
            per_page = 50
        try:
            page = max(1, min(100000, to_int(request.args.get("page"), 1)))
        except (TypeError, ValueError):
            page = 1
        type_id = to_int(request.args.get("type"))
        q = (request.args.get("q") or "").strip()[:100]
        where, params = [], ()
        if type_id is not None:
            where.append("m.machine_model_id IN (SELECT id FROM machine_models WHERE machine_type_id = ?)")
            params = (*params, type_id)
        if q:
            where.append("(m.inventory_number LIKE ? OR mm.name LIKE ? OR mt.name LIKE ?)")
            params = (*params, f"%{q}%", f"%{q}%", f"%{q}%")
        where_sql = (" AND " + " AND ".join(where)) if where else ""
        total = fetch_one(
            f"""
            SELECT COUNT(*) AS c FROM machines m
            JOIN machine_models mm ON m.machine_model_id = mm.id
            JOIN machine_types mt ON mm.machine_type_id = mt.id
            WHERE 1=1{where_sql}
            """,
            params,
        )["c"]
        total_pages = max(1, (total + per_page - 1) // per_page)
        page = min(page, total_pages)
        offset = (page - 1) * per_page
        sort_cols = {"id": "m.id", "inventory_number": "m.inventory_number", "model_name": "mm.name", "type_name": "mt.name"}
        sort_key = request.args.get("sort", "id")
        order_key = request.args.get("order", "asc")
        sort_sql = sort_cols.get(sort_key, "m.id")
        order_sql = "DESC" if order_key == "desc" else "ASC"
        machines = fetch_all(
            f"""
            SELECT m.*, mm.name AS model_name, mt.name AS type_name, mm.id AS model_id, mt.id AS type_id
            FROM machines m
            JOIN machine_models mm ON m.machine_model_id = mm.id
            JOIN machine_types mt ON mm.machine_type_id = mt.id
            WHERE 1=1{where_sql}
            ORDER BY {sort_sql} {order_sql}
            LIMIT ? OFFSET ?
            """,
            (*params, per_page, offset),
        )
        types_ = fetch_all("SELECT * FROM machine_types ORDER BY id")
        models = fetch_all(
            """
            SELECT mm.*, mt.name AS type_name
            FROM machine_models mm
            JOIN machine_types mt ON mm.machine_type_id = mt.id
            ORDER BY mm.id
            """
        )
        return render_template(
            "machines_list.html",
            machines=machines,
            types=types_,
            models=models,
            pagination={"page": page, "per_page": per_page, "total": total, "total_pages": total_pages},
            filter_type=type_id,
            filter_q=q,
            sort=sort_key,
            order=order_key,
        )

    @app.route("/machines/add", methods=["GET", "POST"])
    def machine_add():
        models = fetch_all(
            """
            SELECT mm.*, mt.name AS type_name
            FROM machine_models mm
            JOIN machine_types mt ON mm.machine_type_id = mt.id
            ORDER BY mm.id
            """
        )
        if request.method == "POST":
            inventory_number = (request.form.get("inventory_number") or "").strip()
            machine_model_id = ref_id("machine_models", request.form.get("machine_model_id"))
            if not inventory_number or not machine_model_id:
                flash("Заполните инвентарный номер и выберите модель.", "error")
            else:
                existing = fetch_one(
                    "SELECT id FROM machines WHERE inventory_number = ?",
                    (inventory_number,),
                )
                if existing:
                    flash("Техника с таким инвентарным номером уже есть. Введите другой номер.", "error")
                else:
                    db = get_db()
                    try:
                        db.execute(
                            "INSERT INTO machines (inventory_number, machine_model_id) VALUES (?, ?)",
                            (inventory_number, machine_model_id),
                        )
                        db.commit()
                        flash("Техника добавлена.", "success")
                        return redirect(url_for("machines_list"))
                    except sqlite3.IntegrityError:
                        flash("Не удалось сохранить технику (возможно, такой инв. номер уже есть).", "error")
        return render_template("machine_form.html", machine=None, models=models)

    @app.route("/machines/<int:machine_id>/edit", methods=["GET", "POST"])
    def machine_edit(machine_id):
        models = fetch_all(
            """
            SELECT mm.*, mt.name AS type_name
            FROM machine_models mm
            JOIN machine_types mt ON mm.machine_type_id = mt.id
            ORDER BY mm.id
            """
        )
        machine = fetch_one("SELECT * FROM machines WHERE id = ?", (machine_id,))
        if machine is None:
            flash("Техника не найдена.", "error")
            return redirect(url_for("machines_list"))
        if request.method == "POST":
            inventory_number = (request.form.get("inventory_number") or "").strip()
            machine_model_id = ref_id("machine_models", request.form.get("machine_model_id"))
            if not inventory_number or not machine_model_id:
                flash("Заполните инвентарный номер и выберите модель.", "error")
            else:
                existing = fetch_one(
                    "SELECT id FROM machines WHERE inventory_number = ? AND id != ?",
                    (inventory_number, machine_id),
                )
                if existing:
                    flash("Техника с таким инвентарным номером уже есть. Введите другой номер.", "error")
                else:
                    db = get_db()
                    try:
                        db.execute(
                            """
                            UPDATE machines
                            SET inventory_number = ?, machine_model_id = ?
                            WHERE id = ?
                            """,
                            (inventory_number, machine_model_id, machine_id),
                        )
                        db.commit()
                        flash("Техника обновлена.", "success")
                        return redirect(url_for("machines_list"))
                    except sqlite3.IntegrityError:
                        flash("Не удалось обновить технику (возможно, такой инв. номер уже есть).", "error")
        return render_template("machine_form.html", machine=machine, models=models)

    @app.route("/machines/<int:machine_id>/delete", methods=["POST"])
    def machine_delete(machine_id):
        machine = fetch_one(
            "SELECT m.id, m.inventory_number FROM machines m WHERE m.id = ?",
            (machine_id,),
        )
        if machine is None:
            flash("Техника не найдена.", "error")
            return redirect(url_for("machines_list"))
        has_equipment = fetch_one(
            "SELECT id FROM machine_equipment WHERE machine_id = ? AND removed_at IS NULL LIMIT 1",
            (machine_id,),
        )
        if has_equipment:
            flash(
                f"Нельзя удалить технику {machine['inventory_number']}: на ней установлено оборудование. Сначала демонтируйте оборудование.",
                "error",
            )
            return redirect(url_for("machines_list"))
        has_sim = fetch_one(
            "SELECT id FROM machine_sim_cards WHERE machine_id = ? AND removed_at IS NULL LIMIT 1",
            (machine_id,),
        )
        if has_sim:
            flash(
                f"Нельзя удалить технику {machine['inventory_number']}: на ней установлена SIM-карта. Сначала демонтируйте SIM-карту.",
                "error",
            )
            return redirect(url_for("machines_list"))
        db = get_db()
        db.execute("DELETE FROM machines WHERE id = ?", (machine_id,))
        db.commit()
        flash("Техника удалена.", "success")
        return redirect(url_for("machines_list"))

    @app.route("/machines/<int:machine_id>")
    def machine_detail(machine_id):
        machine = fetch_one(
            """
            SELECT m.*, mm.name AS model_name, mt.name AS type_name
            FROM machines m
            JOIN machine_models mm ON m.machine_model_id = mm.id
            JOIN machine_types mt ON mm.machine_type_id = mt.id
            WHERE m.id = ?
            """,
            (machine_id,),
        )
        if machine is None:
            flash("Техника не найдена.", "error")
            return redirect(url_for("machines_list"))

        # Active SIM
        active_sim = fetch_one(
            """
            SELECT sc.*, msc.id AS link_id, msc.installed_at, msc.removed_at
            FROM machine_sim_cards msc
            JOIN sim_cards sc ON msc.sim_card_id = sc.id
            WHERE msc.machine_id = ? AND msc.removed_at IS NULL
            """,
            (machine_id,),
        )

        # Всё оборудование на технике (включая датчики), с позицией для датчиков
        equipment = [
            dict(row)
            for row in fetch_all(
                """
                SELECT e.*, me.id AS link_id, me.installed_at, me.removed_at,
                       me.sensor_position_id, sp.code AS position_code
                FROM machine_equipment me
                JOIN equipment e ON me.equipment_id = e.id
                LEFT JOIN sensor_positions sp ON me.sensor_position_id = sp.id
                WHERE me.machine_id = ? AND me.removed_at IS NULL
                ORDER BY e.equipment_kind, sp.id, e.id
                """,
                (machine_id,),
            )
        ]
        sensors_by_position = {
            e["position_code"]: e
            for e in equipment
            if e.get("equipment_kind") == "sensor" and e.get("position_code")
        }
        # Только позиции колёс для автосамосвала, порядок: ПЛ, ПП, ЗЛН, ЗЛВ, ЗПН, ЗПВ
        positions_all = [
            dict(row)
            for row in fetch_all(
                """
                SELECT * FROM sensor_positions
                WHERE position_group = 'wheel'
                ORDER BY COALESCE(sort_order, 99), id
                """
            )
        ]

        last_maintenance = fetch_one(
            """
            SELECT id, performed_at, next_due_at, performed_by, remarks
            FROM machine_maintenance WHERE machine_id = ? ORDER BY id DESC LIMIT 1
            """,
            (machine_id,),
        )
        if last_maintenance:
            last_maintenance = dict(last_maintenance)

        limit_param = request.args.get("maintenance_limit", "50").strip().lower()
        if limit_param == "all" or limit_param == "":
            maintenance_history_limit = None
            maintenance_history_total = fetch_one(
                "SELECT COUNT(*) AS c FROM machine_maintenance WHERE machine_id = ?",
                (machine_id,),
            )["c"]
            maintenance_history = [
                dict(row)
                for row in fetch_all(
                    """
                    SELECT id, performed_at, next_due_at, performed_by, remarks, created_at
                    FROM machine_maintenance WHERE machine_id = ? ORDER BY performed_at DESC, id DESC
                    """,
                    (machine_id,),
                )
            ]
        else:
            try:
                maintenance_history_limit = max(1, min(500, int(limit_param)))
            except ValueError:
                maintenance_history_limit = 50
            maintenance_history_total = fetch_one(
                "SELECT COUNT(*) AS c FROM machine_maintenance WHERE machine_id = ?",
                (machine_id,),
            )["c"]
            maintenance_history = [
                dict(row)
                for row in fetch_all(
                    """
                    SELECT id, performed_at, next_due_at, performed_by, remarks, created_at
                    FROM machine_maintenance WHERE machine_id = ? ORDER BY performed_at DESC, id DESC
                    LIMIT ?
                    """,
                    (machine_id, maintenance_history_limit),
                )
            ]

        recent = [r for r in (session.get("recent") or []) if not (r.get("type") == "machine" and r.get("id") == machine_id)]
        recent.append({"type": "machine", "id": machine_id, "label": (machine["inventory_number"] or f"#{machine_id}")[:50]})
        session["recent"] = recent[-10:]
        return render_template(
            "machine_detail.html",
            machine=machine,
            active_sim=active_sim,
            sensors_by_position=sensors_by_position,
            positions_all=positions_all,
            equipment=equipment,
            last_maintenance=last_maintenance,
            maintenance_history=maintenance_history,
            maintenance_history_total=maintenance_history_total,
            maintenance_history_limit=maintenance_history_limit,
        )

    # --- Техобслуживание (ТО) ---

    @app.route("/maintenance")
    def maintenance_list():
        today = date.today().isoformat()
        try:
            per_page = max(1, min(500, int(request.args.get("per_page", 50) or 50)))
        except (TypeError, ValueError):
            per_page = 50
        try:
            page = max(1, min(100000, to_int(request.args.get("page"), 1)))
        except (TypeError, ValueError):
            page = 1
        type_id = to_int(request.args.get("type"))
        status_filter = request.args.get("status", "").strip() or None  # overdue, due_soon, ok, no_data
        machine_types = [dict(r) for r in fetch_all("SELECT id, name FROM machine_types ORDER BY name")]
        # CTE: машины с датами ТО и вычисленным to_status в SQL
        cte = """
        WITH to_data AS (
            SELECT m.id, m.inventory_number, mm.name AS model_name, mt.id AS type_id, mt.name AS type_name,
                   (SELECT next_due_at FROM machine_maintenance mnt WHERE mnt.machine_id = m.id ORDER BY mnt.id DESC LIMIT 1) AS next_due_at,
                   (SELECT performed_at FROM machine_maintenance mnt WHERE mnt.machine_id = m.id ORDER BY mnt.id DESC LIMIT 1) AS last_performed_at,
                   (SELECT performed_by FROM machine_maintenance mnt WHERE mnt.machine_id = m.id ORDER BY mnt.id DESC LIMIT 1) AS last_performed_by
            FROM machines m
            JOIN machine_models mm ON m.machine_model_id = mm.id
            JOIN machine_types mt ON mm.machine_type_id = mt.id
        ),
        with_status AS (
            SELECT *, CASE
                WHEN next_due_at IS NULL THEN 'no_data'
                WHEN next_due_at < date('now', 'localtime') THEN 'overdue'
                WHEN next_due_at <= date('now', 'localtime', '+14 days') THEN 'due_soon'
                ELSE 'ok'
            END AS to_status
            FROM to_data
        )
        """
        where, params = [], ()
        if type_id is not None:
            where.append("type_id = ?")
            params = (*params, type_id)
        if status_filter:
            where.append("to_status = ?")
            params = (*params, status_filter)
        where_sql = (" AND " + " AND ".join(where)) if where else ""
        count_sql = f"""
            WITH to_data AS (
                SELECT m.id, mt.id AS type_id,
                    (SELECT next_due_at FROM machine_maintenance mnt WHERE mnt.machine_id = m.id ORDER BY mnt.id DESC LIMIT 1) AS next_due_at
                FROM machines m
                JOIN machine_models mm ON m.machine_model_id = mm.id
                JOIN machine_types mt ON mm.machine_type_id = mt.id
            ),
            with_status AS (
                SELECT *, CASE
                    WHEN next_due_at IS NULL THEN 'no_data'
                    WHEN next_due_at < date('now', 'localtime') THEN 'overdue'
                    WHEN next_due_at <= date('now', 'localtime', '+14 days') THEN 'due_soon'
                    ELSE 'ok'
                END AS to_status
                FROM to_data
            )
            SELECT COUNT(*) AS c FROM with_status WHERE 1=1{where_sql}
        """
        total = fetch_one(count_sql, params)["c"]
        total_pages = max(1, (total + per_page - 1) // per_page)
        page = min(page, total_pages)
        offset = (page - 1) * per_page
        order_sql = "ORDER BY CASE WHEN next_due_at IS NULL THEN 1 ELSE 0 END, next_due_at ASC, inventory_number LIMIT ? OFFSET ?"
        list_sql = f"""
            {cte}
            SELECT * FROM with_status WHERE 1=1{where_sql} {order_sql}
        """
        machines = fetch_all(list_sql, (*params, per_page, offset))
        machines = [dict(r) for r in machines]
        for m in machines:
            m["to_status_label"] = {"overdue": "Просрочено", "due_soon": "Скоро ТО", "ok": "Норма", "no_data": "ТО не проводилось"}.get(m.get("to_status"), "—")
        # Данные для графика: ТО по месяцам (последние 12 месяцев)
        maintenance_by_month = fetch_all(
            """
            SELECT strftime('%Y-%m', performed_at) AS month, COUNT(*) AS cnt
            FROM machine_maintenance
            WHERE performed_at >= date('now', 'localtime', '-12 months')
            GROUP BY strftime('%Y-%m', performed_at)
            ORDER BY month
            """
        )
        maintenance_by_month = [dict(r) for r in maintenance_by_month]
        # Заполняем все месяцы за последний год (чтобы на графике не было пропусков)
        months_ago = []
        for i in range(11, -1, -1):
            d = date.today()
            month = d.month - i
            year = d.year
            while month < 1:
                month += 12
                year -= 1
            months_ago.append(f"{year}-{month:02d}")
        month_counts = {r["month"]: r["cnt"] for r in maintenance_by_month}
        chart_labels = []
        chart_data = []
        for m in months_ago:
            year, month = m.split("-")
            month_names = (
                "янв", "фев", "мар", "апр", "май", "июн",
                "июл", "авг", "сен", "окт", "ноя", "дек",
            )
            chart_labels.append(f"{month_names[int(month) - 1]} {year}")
            chart_data.append(month_counts.get(m, 0))
        return render_template(
            "maintenance_list.html",
            machines=machines,
            machine_types=machine_types,
            today=today,
            chart_labels=chart_labels,
            chart_data=chart_data,
            pagination={"page": page, "per_page": per_page, "total": total, "total_pages": total_pages},
            filter_type=type_id,
            filter_status=status_filter or "",
            maintenance_interval_months=get_setting("maintenance_interval_months", "3"),
        )

    @app.route("/maintenance/calendar")
    def maintenance_calendar():
        """Календарь ТО: ближайшие даты по дням и по неделям."""
        try:
            weeks = max(1, min(12, int(request.args.get("weeks", 6) or 6)))
        except (TypeError, ValueError):
            weeks = 6
        today = date.today()
        end = today + timedelta(days=weeks * 7)
        start_str = today.isoformat()
        end_str = end.isoformat()
        rows = fetch_all(
            """
            SELECT m.id AS machine_id, m.inventory_number, mm.name AS model_name, mt.name AS type_name,
                   (SELECT next_due_at FROM machine_maintenance mnt WHERE mnt.machine_id = m.id ORDER BY mnt.id DESC LIMIT 1) AS next_due_at
            FROM machines m
            JOIN machine_models mm ON m.machine_model_id = mm.id
            JOIN machine_types mt ON mm.machine_type_id = mt.id
            WHERE (SELECT next_due_at FROM machine_maintenance mnt WHERE mnt.machine_id = m.id ORDER BY mnt.id DESC LIMIT 1) IS NOT NULL
              AND (SELECT next_due_at FROM machine_maintenance mnt WHERE mnt.machine_id = m.id ORDER BY mnt.id DESC LIMIT 1) >= ?
              AND (SELECT next_due_at FROM machine_maintenance mnt WHERE mnt.machine_id = m.id ORDER BY mnt.id DESC LIMIT 1) <= ?
            ORDER BY next_due_at ASC, m.inventory_number
            """,
            (start_str, end_str),
        )
        items = [dict(r) for r in rows]
        for it in items:
            it["next_due_at"] = (it["next_due_at"] or "")[:10]

        by_day = {}
        for it in items:
            d = it["next_due_at"]
            if d:
                by_day.setdefault(d, []).append(it)
        days_sorted = sorted(by_day.keys())

        by_week = []
        week_start = today - timedelta(days=today.weekday())
        while week_start <= end:
            week_end = week_start + timedelta(days=6)
            week_label = f"{week_start.day:02d}.{week_start.month:02d}–{week_end.day:02d}.{week_end.month:02d}"
            day_cells = []
            for d in range(7):
                cell_date = week_start + timedelta(days=d)
                cell_str = cell_date.isoformat()
                day_cells.append({
                    "date": cell_str,
                    "label": cell_date.strftime("%d"),
                    "machines": by_day.get(cell_str, []),
                    "is_past": cell_date < today,
                    "is_today": cell_date == today,
                })
            by_week.append({"label": week_label, "week_start": week_start.isoformat(), "days": day_cells})
            week_start = week_end + timedelta(days=1)

        month_names = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря")
        weekday_names = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")

        # Вид «месяц»: сетка календаря на выбранный месяц
        month_param = request.args.get("month", "").strip() or today.strftime("%Y-%m")
        try:
            year_m, month_m = int(month_param[:4]), int(month_param[5:7])
            month_start = date(year_m, month_m, 1)
            if month_m == 12:
                month_end = date(year_m + 1, 1, 1) - timedelta(days=1)
            else:
                month_end = date(year_m, month_m + 1, 1) - timedelta(days=1)
        except (ValueError, IndexError):
            month_start = date(today.year, today.month, 1)
            if today.month == 12:
                month_end = date(today.year + 1, 1, 1) - timedelta(days=1)
            else:
                month_end = date(today.year, today.month + 1, 1) - timedelta(days=1)
        month_rows = fetch_all(
            """
            SELECT m.id AS machine_id, m.inventory_number, mm.name AS model_name, mt.name AS type_name,
                   (SELECT next_due_at FROM machine_maintenance mnt WHERE mnt.machine_id = m.id ORDER BY mnt.id DESC LIMIT 1) AS next_due_at
            FROM machines m
            JOIN machine_models mm ON m.machine_model_id = mm.id
            JOIN machine_types mt ON mm.machine_type_id = mt.id
            WHERE (SELECT next_due_at FROM machine_maintenance mnt WHERE mnt.machine_id = m.id ORDER BY mnt.id DESC LIMIT 1) IS NOT NULL
              AND (SELECT next_due_at FROM machine_maintenance mnt WHERE mnt.machine_id = m.id ORDER BY mnt.id DESC LIMIT 1) >= ?
              AND (SELECT next_due_at FROM machine_maintenance mnt WHERE mnt.machine_id = m.id ORDER BY mnt.id DESC LIMIT 1) <= ?
            ORDER BY next_due_at ASC
            """,
            (month_start.isoformat(), month_end.isoformat()),
        )
        month_items = [dict(r) for r in month_rows]
        for it in month_items:
            it["next_due_at"] = (it["next_due_at"] or "")[:10]
        by_day_month = {}
        for it in month_items:
            d = it["next_due_at"]
            if d:
                by_day_month.setdefault(d, []).append(it)
        cal_start = month_start - timedelta(days=month_start.weekday())
        month_calendar_weeks = []
        for week_idx in range(6):
            week_days = []
            for d in range(7):
                cell_date = cal_start + timedelta(days=week_idx * 7 + d)
                cell_str = cell_date.isoformat()
                week_days.append({
                    "date_iso": cell_str,
                    "day_num": cell_date.day if cell_date.month == month_start.month and cell_date.year == month_start.year else None,
                    "count": len(by_day_month.get(cell_str, [])),
                    "machines": by_day_month.get(cell_str, []),
                    "is_current_month": cell_date.month == month_start.month and cell_date.year == month_start.year,
                    "is_today": cell_date == today,
                })
            month_calendar_weeks.append(week_days)
        month_title = f"{month_names[month_start.month - 1].capitalize()} {month_start.year}"

        return render_template(
            "maintenance_calendar.html",
            by_day=by_day,
            days_sorted=days_sorted,
            by_week=by_week,
            today_str=today.isoformat(),
            weeks_param=weeks,
            month_names=month_names,
            weekday_names=weekday_names,
            month_calendar_weeks=month_calendar_weeks,
            month_title=month_title,
            month_param=month_start.strftime("%Y-%m"),
        )

    @app.route("/maintenance/add", methods=["GET", "POST"])
    @app.route("/maintenance/add/<int:machine_id>", methods=["GET", "POST"])
    def maintenance_add(machine_id=None):
        machines = fetch_all(
            "SELECT m.id, m.inventory_number, mm.name AS model_name FROM machines m "
            "JOIN machine_models mm ON m.machine_model_id = mm.id ORDER BY m.inventory_number"
        )
        machines = [dict(r) for r in machines]
        # Список пользователей для выбора исполнителей ТО
        engineers_rows = fetch_all(
            "SELECT id, username, surname, first_name FROM users ORDER BY surname, first_name, username"
        )
        engineers = []
        for r in engineers_rows:
            row = dict(r)
            full_name = " ".join(
                part for part in [(row.get("surname") or "").strip(), (row.get("first_name") or "").strip()] if part
            ).strip()
            label = full_name or row.get("username")
            engineers.append({"id": row["id"], "label": label})
        today_str = date.today().isoformat()
        if request.method == "POST":
            mid = ref_id("machines", request.form.get("machine_id") or machine_id)
            performed_at = to_iso_date(request.form.get("performed_at"))
            engineer_ids = request.form.getlist("engineers")
            remarks = request.form.get("remarks", "").strip()
            if not mid or not performed_at:
                flash("Укажите технику и дату проведения ТО.", "error")
            else:
                months_val = get_setting("maintenance_interval_months", "3")
                try:
                    months_int = max(1, min(24, int(months_val)))
                except (TypeError, ValueError):
                    months_int = 3
                next_due = maintenance_next_due(performed_at, months_int)
                # Собираем имена исполнителей по выбранным пользователям
                performed_by = None
                if engineer_ids:
                    labels_by_id = {str(e["id"]): e["label"] for e in engineers}
                    selected_labels = [labels_by_id.get(eid) for eid in engineer_ids if labels_by_id.get(eid)]
                    if selected_labels:
                        performed_by = ", ".join(selected_labels)
                db = get_db()
                db.execute(
                    """
                    INSERT INTO machine_maintenance (machine_id, performed_at, next_due_at, performed_by, remarks)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (mid, performed_at[:10], next_due, performed_by or None, remarks or None),
                )
                db.commit()
                machine = fetch_one("SELECT inventory_number FROM machines WHERE id = ?", (mid,))
                inv = machine["inventory_number"] if machine else str(mid)
                log_operation(
                    "status_change",
                    "machine",
                    mid,
                    f"Техобслуживание техники {inv} проведено {performed_at[:10]}. Следующее ТО: {next_due}. Исполнители: {performed_by or '—'}",
                )
                flash("Запись о ТО сохранена.", "success")
                return redirect(url_for("maintenance_list"))
        return render_template(
            "maintenance_form.html",
            machines=machines,
            preselected_machine_id=machine_id,
            maintenance_interval_months=get_setting("maintenance_interval_months", "3"),
             engineers=engineers,
             today_str=today_str,
        )

    @app.route("/maintenance/act-draft")
    def maintenance_act_draft():
        """Акт для печати перед проведением ТО (без сохранённой записи)."""
        machine_id = to_int(request.args.get("machine_id"))
        performed_at = (request.args.get("performed_at") or "").strip()[:10] or None
        if not machine_id:
            flash("Укажите технику.", "error")
            return redirect(url_for("maintenance_add"))
        machine = fetch_one(
            """
            SELECT m.id AS machine_id, m.inventory_number, mm.name AS model_name, mt.name AS type_name
            FROM machines m
            JOIN machine_models mm ON m.machine_model_id = mm.id
            JOIN machine_types mt ON mm.machine_type_id = mt.id
            WHERE m.id = ?
            """,
            (machine_id,),
        )
        if machine is None:
            flash("Техника не найдена.", "error")
            return redirect(url_for("maintenance_add"))
        rec = dict(machine)
        rec["performed_at"] = performed_at
        rec["next_due_at"] = maintenance_next_due(performed_at, int(get_setting("maintenance_interval_months", "3"))) if performed_at else None
        rec["performed_by"] = None
        rec["remarks"] = None
        equipment_other, equipment_sensors = _maintenance_act_equipment(machine_id)
        is_driver = "самосвал" in (rec.get("type_name") or "").lower()
        return render_template(
            "maintenance_act.html",
            act=rec,
            equipment_other=equipment_other,
            equipment_sensors=equipment_sensors,
            driver_label="Водитель" if is_driver else "Машинист",
            is_draft=True,
        )

    def _maintenance_act_equipment(machine_id):
        """Списки оборудования (не датчики) и датчиков с позициями для акта ТО."""
        rows = fetch_all(
            """
            SELECT e.name, e.serial_number, e.equipment_kind, sp.code AS position_code
            FROM machine_equipment me
            JOIN equipment e ON me.equipment_id = e.id
            LEFT JOIN sensor_positions sp ON me.sensor_position_id = sp.id
            WHERE me.machine_id = ? AND me.removed_at IS NULL
            ORDER BY e.equipment_kind, sp.id, e.id
            """,
            (machine_id,),
        )
        other, sensors = [], []
        for r in rows:
            d = dict(r)
            if d.get("equipment_kind") == "sensor":
                sensors.append(d)
            else:
                other.append(d)
        return other, sensors

    @app.route("/maintenance/<int:maintenance_id>/act")
    def maintenance_act(maintenance_id):
        """Страница акта о проведении ТО для печати (по сохранённой записи)."""
        rec = fetch_one(
            """
            SELECT mnt.id, mnt.machine_id, mnt.performed_at, mnt.next_due_at, mnt.performed_by, mnt.remarks,
                   m.inventory_number, mm.name AS model_name, mt.name AS type_name
            FROM machine_maintenance mnt
            JOIN machines m ON mnt.machine_id = m.id
            JOIN machine_models mm ON m.machine_model_id = mm.id
            JOIN machine_types mt ON mm.machine_type_id = mt.id
            WHERE mnt.id = ?
            """,
            (maintenance_id,),
        )
        if rec is None:
            flash("Запись о ТО не найдена.", "error")
            return redirect(url_for("maintenance_list"))
        rec = dict(rec)
        machine_id = rec["machine_id"]
        equipment_other, equipment_sensors = _maintenance_act_equipment(machine_id)
        is_driver = "самосвал" in (rec.get("type_name") or "").lower()
        return render_template(
            "maintenance_act.html",
            act=rec,
            equipment_other=equipment_other,
            equipment_sensors=equipment_sensors,
            driver_label="Водитель" if is_driver else "Машинист",
            is_draft=False,
        )

    # --- Типы заданий на смену (справочник) ---

    @app.route("/shift_task_types")
    def shift_task_types_list():
        types_list = [
            dict(r)
            for r in fetch_all(
                "SELECT * FROM shift_task_types ORDER BY sort_order, id"
            )
        ]
        return render_template("shift_task_types_list.html", types_list=types_list)

    @app.route("/shift_task_types/add", methods=["GET", "POST"])
    def shift_task_type_add():
        if not current_user.is_authenticated or not current_user.can_edit:
            flash("Недостаточно прав.", "error")
            return redirect(url_for("shift_task_types_list"))
        if request.method == "POST":
            name = (request.form.get("name") or "").strip()
            sort_order_raw = (request.form.get("sort_order") or "").strip()
            sort_order = 0
            if sort_order_raw:
                try:
                    sort_order = int(sort_order_raw)
                except ValueError:
                    pass
            if not name:
                flash("Введите название типа.", "error")
            else:
                db = get_db()
                try:
                    db.execute(
                        "INSERT INTO shift_task_types (name, sort_order) VALUES (?, ?)",
                        (name, sort_order),
                    )
                    db.commit()
                    flash("Тип задания добавлен.", "success")
                    return redirect(url_for("shift_task_types_list"))
                except sqlite3.IntegrityError:
                    flash("Тип с таким названием уже есть.", "error")
        return render_template("shift_task_type_form.html", type_item=None)

    @app.route("/shift_task_types/<int:type_id>/edit", methods=["GET", "POST"])
    def shift_task_type_edit(type_id):
        if not current_user.is_authenticated or not current_user.can_edit:
            flash("Недостаточно прав.", "error")
            return redirect(url_for("shift_task_types_list"))
        type_item = fetch_one("SELECT * FROM shift_task_types WHERE id = ?", (type_id,))
        if type_item is None:
            flash("Тип не найден.", "error")
            return redirect(url_for("shift_task_types_list"))
        type_item = dict(type_item)
        if request.method == "POST":
            name = (request.form.get("name") or "").strip()
            sort_order_raw = (request.form.get("sort_order") or "").strip()
            sort_order = 0
            if sort_order_raw:
                try:
                    sort_order = int(sort_order_raw)
                except ValueError:
                    pass
            if not name:
                flash("Введите название типа.", "error")
            else:
                db = get_db()
                try:
                    db.execute(
                        "UPDATE shift_task_types SET name = ?, sort_order = ? WHERE id = ?",
                        (name, sort_order, type_id),
                    )
                    db.commit()
                    flash("Тип задания обновлён.", "success")
                    return redirect(url_for("shift_task_types_list"))
                except sqlite3.IntegrityError:
                    flash("Тип с таким названием уже есть.", "error")
        return render_template("shift_task_type_form.html", type_item=type_item)

    @app.route("/shift_task_types/<int:type_id>/delete", methods=["POST"])
    def shift_task_type_delete(type_id):
        if not current_user.is_authenticated or not current_user.can_edit:
            flash("Недостаточно прав.", "error")
            return redirect(url_for("shift_task_types_list"))
        type_item = fetch_one("SELECT * FROM shift_task_types WHERE id = ?", (type_id,))
        if type_item is None:
            flash("Тип не найден.", "error")
            return redirect(url_for("shift_task_types_list"))
        used = fetch_one(
            "SELECT 1 FROM shift_tasks WHERE task_type_id = ? LIMIT 1",
            (type_id,),
        )
        if used:
            flash("Нельзя удалить тип: по нему есть задания. Сначала измените тип у заданий.", "error")
            return redirect(url_for("shift_task_types_list"))
        db = get_db()
        db.execute("DELETE FROM shift_task_types WHERE id = ?", (type_id,))
        db.commit()
        flash("Тип задания удалён.", "success")
        return redirect(url_for("shift_task_types_list"))

    # --- Задания на смену (1-я / 2-я смена) ---

    @app.route("/shift_tasks", methods=["GET", "POST"])
    def shift_tasks_list():
        today_str = date.today().isoformat()
        filter_shift = request.args.get("shift", "").strip().lower() or None
        if filter_shift not in ("day", "night"):
            filter_shift = None
        filter_date = (request.args.get("date") or "").strip()[:10] or today_str
        filter_type_id = to_int(request.args.get("type"))
        filter_status = (request.args.get("status") or "").strip().lower() or None
        if filter_status not in ("pending", "done", "not_done"):
            filter_status = None

        if request.method == "POST" and request.form.get("action") == "add":
            if not current_user.is_authenticated:
                flash("Войдите в систему.", "error")
                return redirect(url_for("shift_tasks_list"))
            if not current_user.can_edit and getattr(current_user, "role", None) != "maintenance":
                flash("Недостаточно прав для создания заданий.", "error")
                return redirect(url_for("shift_tasks_list"))
            for_shift = (request.form.get("for_shift") or "").strip().lower()
            effective_date = to_iso_date(request.form.get("effective_date"))
            task_type_id = ref_id("shift_task_types", request.form.get("task_type_id"))
            if not task_type_id:
                first_type = fetch_one("SELECT id FROM shift_task_types ORDER BY sort_order, id LIMIT 1")
                task_type_id = first_type["id"] if first_type else None
            machine_id = ref_id("machines", request.form.get("machine_id"))
            equipment_id = ref_id("equipment", request.form.get("equipment_id"))
            title = (request.form.get("title") or "").strip()
            description = (request.form.get("description") or "").strip() or None
            if for_shift not in ("day", "night"):
                flash("Укажите смену (1-я или 2-я).", "error")
            elif not effective_date:
                flash("Укажите корректную дату смены.", "error")
            elif not title:
                flash("Введите заголовок задания.", "error")
            elif not task_type_id:
                flash("Выберите тип задания.", "error")
            else:
                db = get_db()
                cur = db.execute(
                    """
                    INSERT INTO shift_tasks (created_by_user_id, for_shift, effective_date, task_type_id, machine_id, equipment_id, title, description, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                    """,
                    (current_user.id, for_shift, effective_date, task_type_id, machine_id, equipment_id, title, description),
                )
                new_task_id = cur.lastrowid
                db.commit()
                shift_name = "1-я" if for_shift == "day" else "2-я"
                log_operation(
                    "create",
                    "shift_task",
                    new_task_id,
                    f"Создано задание на смену: «{title}» (дата {effective_date}, {shift_name} смена)",
                )
                shift_label = "1-ю" if for_shift == "day" else "2-ю"
                flash(f"Задание для {shift_label} смены добавлено.", "success")
                return redirect(
                    url_for(
                        "shift_tasks_list",
                        shift=filter_shift,
                        date=filter_date,
                        type=filter_type_id or "",
                        status=filter_status or "",
                        page=1,
                        per_page=request.args.get("per_page", 50),
                    )
                )

        where_parts = ["st.effective_date = ?"]
        params = [filter_date]
        if filter_shift:
            where_parts.append("st.for_shift = ?")
            params.append(filter_shift)
        if filter_type_id is not None:
            where_parts.append("st.task_type_id = ?")
            params.append(filter_type_id)
        if filter_status:
            where_parts.append("st.status = ?")
            params.append(filter_status)
        where_sql = " AND ".join(where_parts)

        page = max(1, min(100000, to_int(request.args.get("page"), 1)))
        per_page = to_int(request.args.get("per_page"), 50)
        per_page = max(20, min(200, per_page)) if per_page else 50
        total = fetch_one(
            f"SELECT COUNT(*) AS c FROM shift_tasks st WHERE {where_sql}",
            tuple(params),
        )["c"]
        total_pages = max(1, (total + per_page - 1) // per_page)
        if page > total_pages:
            page = total_pages
        offset = (page - 1) * per_page

        tasks = [
            dict(row)
            for row in fetch_all(
                f"""
                SELECT st.*,
                       tt.name AS task_type_name,
                       COALESCE(NULLIF(TRIM(u1.surname || ' ' || u1.first_name), ''), u1.username) AS created_by_name,
                       COALESCE(NULLIF(TRIM(u_nd.surname || ' ' || u_nd.first_name), ''), u_nd.username) AS not_done_by_name,
                       m.inventory_number AS machine_inventory_number,
                       e.name AS equipment_name,
                       e.serial_number AS equipment_serial
                FROM shift_tasks st
                LEFT JOIN shift_task_types tt ON st.task_type_id = tt.id
                LEFT JOIN users u1 ON st.created_by_user_id = u1.id
                LEFT JOIN users u_nd ON st.not_done_by_user_id = u_nd.id
                LEFT JOIN machines m ON st.machine_id = m.id
                LEFT JOIN equipment e ON st.equipment_id = e.id
                WHERE {where_sql}
                ORDER BY st.for_shift, st.created_at DESC
                LIMIT ? OFFSET ?
                """,
                (*params, per_page, offset),
            )
        ]
        task_ids = [t["id"] for t in tasks]
        completions_by_task = {}
        if task_ids:
            placeholders = ",".join("?" * len(task_ids))
            rows = fetch_all(
                f"""
                SELECT c.shift_task_id, c.completed_at, c.comment,
                       COALESCE(NULLIF(TRIM(u.surname || ' ' || u.first_name), ''), u.username) AS user_name
                FROM shift_task_completions c
                LEFT JOIN users u ON c.user_id = u.id
                WHERE c.shift_task_id IN ({placeholders})
                ORDER BY c.completed_at
                """,
                tuple(task_ids),
            )
            for r in rows:
                r = dict(r)
                tid = r["shift_task_id"]
                if tid not in completions_by_task:
                    completions_by_task[tid] = []
                completions_by_task[tid].append((r["user_name"] or "—", r["completed_at"] or "", r.get("comment") or ""))

        machines = [
            dict(r)
            for r in fetch_all(
                """
                SELECT m.id, m.inventory_number, mm.name AS model_name, mt.name AS type_name
                FROM machines m
                LEFT JOIN machine_models mm ON m.machine_model_id = mm.id
                LEFT JOIN machine_types mt ON mm.machine_type_id = mt.id
                ORDER BY m.inventory_number
                """
            )
        ]
        equipment = [
            dict(r)
            for r in fetch_all(
                "SELECT id, name, serial_number, equipment_kind FROM equipment ORDER BY name"
            )
        ]
        task_types = [dict(r) for r in fetch_all("SELECT id, name FROM shift_task_types ORDER BY sort_order, id")]
        task_ids_completed_by_me = set()
        if current_user.is_authenticated and task_ids:
            task_ids_completed_by_me = {
                r[0]
                for r in fetch_all(
                    "SELECT shift_task_id FROM shift_task_completions WHERE user_id = ? AND shift_task_id IN (" + ",".join("?" * len(task_ids)) + ")",
                    (current_user.id,) + tuple(task_ids),
                )
            }
        return render_template(
            "shift_tasks_list.html",
            tasks=tasks,
            completions_by_task=completions_by_task,
            task_ids_completed_by_me=task_ids_completed_by_me,
            filter_shift=filter_shift,
            filter_date=filter_date,
            filter_type_id=filter_type_id,
            filter_status=filter_status,
            today_str=today_str,
            machines=machines,
            equipment=equipment,
            task_types=task_types,
            pagination={"page": page, "per_page": per_page, "total": total, "total_pages": total_pages},
        )

    @app.route("/shift_tasks/<int:task_id>/edit", methods=["GET", "POST"])
    def shift_task_edit(task_id):
        if not current_user.is_authenticated or (not current_user.can_edit and getattr(current_user, "role", None) != "maintenance"):
            flash("Недостаточно прав.", "error")
            return redirect(url_for("shift_tasks_list"))
        task = fetch_one(
            "SELECT id, created_by_user_id, for_shift, effective_date, task_type_id, machine_id, equipment_id, title, description, status FROM shift_tasks WHERE id = ?",
            (task_id,),
        )
        if task is None:
            flash("Задание не найдено.", "error")
            return redirect(url_for("shift_tasks_list"))
        task = dict(task)
        if task["status"] != "pending":
            flash("Редактировать можно только задания в статусе «Ожидает».", "error")
            return redirect(url_for("shift_tasks_list"))
        if not current_user.can_edit and task["created_by_user_id"] != current_user.id:
            flash("Изменять задание может только тот, кто его создал, или редактор/администратор.", "error")
            return redirect(url_for("shift_tasks_list"))
        if request.method == "POST":
            for_shift = (request.form.get("for_shift") or "").strip().lower()
            effective_date = to_iso_date(request.form.get("effective_date"))
            task_type_id = ref_id("shift_task_types", request.form.get("task_type_id"))
            machine_id = ref_id("machines", request.form.get("machine_id"))
            equipment_id = ref_id("equipment", request.form.get("equipment_id"))
            title = (request.form.get("title") or "").strip()
            description = (request.form.get("description") or "").strip() or None
            if for_shift not in ("day", "night"):
                flash("Укажите смену (1-я или 2-я).", "error")
            elif not effective_date:
                flash("Укажите корректную дату смены.", "error")
            elif not title:
                flash("Введите заголовок задания.", "error")
            elif not task_type_id:
                flash("Выберите тип задания.", "error")
            else:
                db = get_db()
                db.execute(
                    """
                    UPDATE shift_tasks SET for_shift = ?, effective_date = ?, task_type_id = ?, machine_id = ?, equipment_id = ?, title = ?, description = ?
                    WHERE id = ? AND status = 'pending'
                    """,
                    (for_shift, effective_date, task_type_id, machine_id, equipment_id, title, description, task_id),
                )
                db.commit()
                log_operation(
                    "edit",
                    "shift_task",
                    task_id,
                    f"Изменено задание на смену #{task_id}: «{title}»",
                )
                flash("Задание обновлено.", "success")
                return redirect(url_for("shift_tasks_list"))
        machines = [
            dict(r)
            for r in fetch_all(
                """
                SELECT m.id, m.inventory_number, mm.name AS model_name, mt.name AS type_name
                FROM machines m
                LEFT JOIN machine_models mm ON m.machine_model_id = mm.id
                LEFT JOIN machine_types mt ON mm.machine_type_id = mt.id
                ORDER BY m.inventory_number
                """
            )
        ]
        equipment = [
            dict(r)
            for r in fetch_all("SELECT id, name, serial_number, equipment_kind FROM equipment ORDER BY name")
        ]
        task_types = [dict(r) for r in fetch_all("SELECT id, name FROM shift_task_types ORDER BY sort_order, id")]
        return render_template(
            "shift_task_edit.html",
            task=task,
            machines=machines,
            equipment=equipment,
            task_types=task_types,
        )

    @app.route("/shift_tasks/<int:task_id>/delete", methods=["POST"])
    def shift_task_delete(task_id):
        if not current_user.is_authenticated or (not current_user.can_edit and getattr(current_user, "role", None) != "maintenance"):
            flash("Недостаточно прав.", "error")
            return redirect(url_for("shift_tasks_list"))
        task = fetch_one("SELECT id, status, created_by_user_id, title FROM shift_tasks WHERE id = ?", (task_id,))
        if task is None:
            flash("Задание не найдено.", "error")
            return redirect(url_for("shift_tasks_list"))
        if task["status"] != "pending":
            flash("Удалить можно только задания в статусе «Ожидает».", "error")
            return redirect(url_for("shift_tasks_list"))
        if not current_user.can_edit and task["created_by_user_id"] != current_user.id:
            flash("Удалить задание может только тот, кто его создал, или редактор/администратор.", "error")
            return redirect(url_for("shift_tasks_list"))
        task_title = (task["title"] or "")[:80]
        db = get_db()
        db.execute("DELETE FROM shift_task_completions WHERE shift_task_id = ?", (task_id,))
        db.execute("DELETE FROM shift_tasks WHERE id = ?", (task_id,))
        db.commit()
        log_operation(
            "delete",
            "shift_task",
            task_id,
            f"Удалено задание на смену #{task_id}: «{task_title}»",
        )
        flash("Задание удалено.", "success")
        return redirect(request.referrer or url_for("shift_tasks_list"))

    @app.route("/shift_tasks/<int:task_id>/complete", methods=["GET", "POST"])
    def shift_task_complete(task_id):
        if not current_user.is_authenticated:
            flash("Войдите в систему.", "error")
            return redirect(url_for("shift_tasks_list"))
        if not current_user.can_edit and getattr(current_user, "role", None) != "maintenance":
            flash("Недостаточно прав.", "error")
            return redirect(url_for("shift_tasks_list"))
        task = fetch_one(
            "SELECT id, status, title FROM shift_tasks WHERE id = ?",
            (task_id,),
        )
        if task is None:
            flash("Задание не найдено.", "error")
            return redirect(url_for("shift_tasks_list"))
        if request.method == "GET":
            return render_template(
                "shift_task_complete.html",
                task=dict(task),
            )
        now = datetime.now().isoformat(sep=" ", timespec="seconds")
        comment = (request.form.get("comment") or "").strip() or None
        db = get_db()
        already = db.execute(
            "SELECT 1 FROM shift_task_completions WHERE shift_task_id = ? AND user_id = ?",
            (task_id, current_user.id),
        ).fetchone()
        if already:
            flash("Вы уже отметили это задание выполненным.", "info")
            return redirect(url_for("shift_tasks_list"))
        db.execute(
            "INSERT INTO shift_task_completions (shift_task_id, user_id, completed_at, comment) VALUES (?, ?, ?, ?)",
            (task_id, current_user.id, now, comment),
        )
        if task["status"] != "done":
            first = db.execute(
                "SELECT user_id, completed_at FROM shift_task_completions WHERE shift_task_id = ? ORDER BY completed_at LIMIT 1",
                (task_id,),
            ).fetchone()
            db.execute(
                "UPDATE shift_tasks SET status = 'done', completed_at = ?, completed_by_user_id = ? WHERE id = ?",
                (first[1], first[0], task_id),
            )
        db.commit()
        desc = f"Отмечено выполненным задание #{task_id}: «{(task["title"] or '')[:50]}»"
        if comment:
            desc += f". Что сделано: {comment[:100]}"
        log_operation("complete", "shift_task", task_id, desc)
        flash("Вы отмечены как исполнитель задания.", "success")
        return redirect(url_for("shift_tasks_list"))

    @app.route("/shift_tasks/<int:task_id>/not_done", methods=["GET", "POST"])
    def shift_task_not_done(task_id):
        if not current_user.is_authenticated:
            flash("Войдите в систему.", "error")
            return redirect(url_for("shift_tasks_list"))
        if not current_user.can_edit and getattr(current_user, "role", None) != "maintenance":
            flash("Недостаточно прав.", "error")
            return redirect(url_for("shift_tasks_list"))
        task = fetch_one(
            "SELECT id, status, title FROM shift_tasks WHERE id = ?",
            (task_id,),
        )
        if task is None:
            flash("Задание не найдено.", "error")
            return redirect(url_for("shift_tasks_list"))
        if task["status"] not in ("pending", "done"):
            flash("Задание уже отмечено как не выполненное.", "info")
            return redirect(url_for("shift_tasks_list"))
        if request.method == "GET":
            return render_template(
                "shift_task_not_done.html",
                task=dict(task),
            )
        reason = (request.form.get("reason") or "").strip()
        if not reason:
            flash("Укажите причину невыполнения.", "error")
            return render_template(
                "shift_task_not_done.html",
                task=dict(task),
            )
        now = datetime.now().isoformat(sep=" ", timespec="seconds")
        db = get_db()
        db.execute(
            "UPDATE shift_tasks SET status = 'not_done', not_done_reason = ?, not_done_at = ?, not_done_by_user_id = ? WHERE id = ?",
            (reason, now, current_user.id, task_id),
        )
        db.commit()
        task_title = (task["title"] or "")[:50]
        reason_short = reason[:80].replace("\n", " ") if reason else ""
        log_operation(
            "not_done",
            "shift_task",
            task_id,
            f"Отмечено не выполненным задание #{task_id}: «{task_title}». Причина: {reason_short}",
        )
        flash("Задание отмечено как не выполненное.", "success")
        return redirect(url_for("shift_tasks_list"))

    @app.route("/settings", methods=["GET", "POST"])
    @login_required
    def settings():
        r = _require_admin()
        if r is not None:
            return r
        if request.method == "POST":
            raw = (request.form.get("maintenance_interval_months") or "").strip()
            try:
                val = max(1, min(24, int(raw)))
                set_setting("maintenance_interval_months", str(val))
            except (TypeError, ValueError):
                pass
            backup_dir = (request.form.get("backup_dir") or "").strip()
            if backup_dir and not _is_allowed_backup_path(backup_dir):
                flash("Директория для копий должна быть внутри папки «backups» приложения.", "error")
                backup_dir = get_setting("backup_dir", "")
            set_setting("backup_dir", backup_dir)
            backup_schedule = (request.form.get("backup_schedule") or "off").strip()
            if backup_schedule not in ("off", "daily", "weekly"):
                backup_schedule = "off"
            set_setting("backup_schedule", backup_schedule)
            backup_time = (request.form.get("backup_time") or "03:00").strip()[:5]
            if len(backup_time) == 5 and ":" in backup_time:
                try:
                    h, m = map(int, backup_time.split(":"))
                    if 0 <= h <= 23 and 0 <= m <= 59:
                        set_setting("backup_time", backup_time)
                except ValueError:
                    pass
            backup_weekday = request.form.get("backup_weekday", "0")
            try:
                wd = max(0, min(6, int(backup_weekday)))
                set_setting("backup_weekday", str(wd))
            except (TypeError, ValueError):
                pass
            if request.form.get("backup_now"):
                if backup_dir and _is_allowed_backup_path(backup_dir):
                    if do_backup_to_dir(backup_dir):
                        flash("Резервная копия создана в указанную директорию.", "success")
                    else:
                        flash("Ошибка создания резервной копии.", "error")
                else:
                    flash("Укажите существующую директорию для копий и нажмите «Сохранить настройки».", "error")
            else:
                flash("Настройки сохранены.", "success")
            return redirect(url_for("settings"))
        interval = get_setting("maintenance_interval_months", "3")
        backup_dir = get_setting("backup_dir", "")
        backup_schedule = get_setting("backup_schedule", "off")
        backup_time = get_setting("backup_time", "03:00")
        if len(backup_time) != 5 or ":" not in backup_time:
            backup_time = "03:00"
        backup_weekday = get_setting("backup_weekday", "0")
        try:
            backup_weekday = str(max(0, min(6, int(backup_weekday))))
        except (TypeError, ValueError):
            backup_weekday = "0"
        return render_template(
            "settings.html",
            maintenance_interval_months=interval,
            backup_dir=backup_dir,
            backup_schedule=backup_schedule,
            backup_time=backup_time,
            backup_weekday=backup_weekday,
            last_backup_at=get_setting("last_backup_at", ""),
            backup_root=BACKUP_ROOT,
        )

    # --- SIM cards ---

    @app.route("/sim_cards")
    def sim_cards_list():
        try:
            per_page = max(1, min(500, int(request.args.get("per_page", 50) or 50)))
        except (TypeError, ValueError):
            per_page = 50
        try:
            page = max(1, min(100000, to_int(request.args.get("page"), 1)))
        except (TypeError, ValueError):
            page = 1
        status_id = to_int(request.args.get("status"))
        type_id = to_int(request.args.get("type"))
        q = (request.args.get("q") or "").strip()[:100]
        where, params = [], ()
        if status_id is not None:
            where.append("sc.status_id = ?")
            params = (*params, status_id)
        if type_id is not None:
            where.append("msc.id IS NOT NULL AND mt.id = ?")
            params = (*params, type_id)
        if q:
            where.append(
                "(sc.phone_number LIKE ? OR sc.iccid LIKE ? OR sc.ip_address LIKE ? OR sc.operator LIKE ? OR sc.description LIKE ? OR sc.tariff LIKE ?)"
            )
            params = (*params, f"%{q}%", f"%{q}%", f"%{q}%", f"%{q}%", f"%{q}%", f"%{q}%")
        where_sql = (" AND " + " AND ".join(where)) if where else ""
        from_sql = """
            FROM sim_cards sc
            JOIN sim_statuses ss ON sc.status_id = ss.id
            LEFT JOIN machine_sim_cards msc ON msc.sim_card_id = sc.id AND msc.removed_at IS NULL
            LEFT JOIN machines m ON msc.machine_id = m.id
            LEFT JOIN machine_models mm ON m.machine_model_id = mm.id
            LEFT JOIN machine_types mt ON mm.machine_type_id = mt.id
        """
        total = fetch_one(
            f"SELECT COUNT(*) AS c {from_sql} WHERE 1=1{where_sql}",
            params,
        )["c"]
        total_pages = max(1, (total + per_page - 1) // per_page)
        page = min(page, total_pages)
        offset = (page - 1) * per_page
        sim_cards = fetch_all(
            f"""
            SELECT
                sc.*,
                ss.name AS status_name,
                m.id AS machine_id,
                m.inventory_number,
                mm.name AS machine_model_name,
                mt.id AS machine_type_id,
                mt.name AS machine_type_name
            {from_sql}
            WHERE 1=1{where_sql}
            ORDER BY sc.id
            LIMIT ? OFFSET ?
            """,
            (*params, per_page, offset),
        )
        statuses = fetch_all("SELECT * FROM sim_statuses ORDER BY id")
        machine_types = fetch_all("SELECT id, name FROM machine_types ORDER BY name")
        return render_template(
            "sim_cards_list.html",
            sim_cards=sim_cards,
            statuses=statuses,
            machine_types=machine_types,
            pagination={"page": page, "per_page": per_page, "total": total, "total_pages": total_pages},
            filter_status=status_id,
            filter_type=type_id,
            filter_q=q,
        )

    @app.route("/sim_cards/add", methods=["GET", "POST"])
    def sim_card_add():
        statuses = fetch_all("SELECT * FROM sim_statuses ORDER BY id")
        if request.method == "POST":
            phone_number = request.form.get("phone_number", "").strip()
            iccid = request.form.get("iccid", "").strip()
            ip_address = request.form.get("ip_address", "").strip()
            operator = request.form.get("operator", "").strip() or None
            description = request.form.get("description", "").strip() or None
            tariff = request.form.get("tariff", "").strip() or None
            status_id = ref_id("sim_statuses", request.form.get("status_id"))
            if not phone_number or not iccid or not status_id:
                flash("Заполните обязательные поля.", "error")
            else:
                db = get_db()
                try:
                    db.execute(
                        """
                        INSERT INTO sim_cards (phone_number, iccid, ip_address, operator, description, tariff, status_id)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (phone_number, iccid, ip_address, operator, description, tariff, status_id),
                    )
                    db.commit()
                    flash("SIM-карта добавлена.", "success")
                    return redirect(url_for("sim_cards_list"))
                except sqlite3.IntegrityError:
                    flash("ICCID должен быть уникальным.", "error")
        return render_template("sim_card_form.html", sim_card=None, statuses=statuses)

    @app.route("/sim_cards/<int:sim_id>/edit", methods=["GET", "POST"])
    def sim_card_edit(sim_id):
        statuses = fetch_all("SELECT * FROM sim_statuses ORDER BY id")
        sim_card = fetch_one("SELECT * FROM sim_cards WHERE id = ?", (sim_id,))
        if sim_card is None:
            flash("SIM-карта не найдена.", "error")
            return redirect(url_for("sim_cards_list"))
        if request.method == "POST":
            phone_number = request.form.get("phone_number", "").strip()
            iccid = request.form.get("iccid", "").strip()
            ip_address = request.form.get("ip_address", "").strip()
            operator = request.form.get("operator", "").strip() or None
            description = request.form.get("description", "").strip() or None
            tariff = request.form.get("tariff", "").strip() or None
            status_id = ref_id("sim_statuses", request.form.get("status_id"))
            if not phone_number or not iccid or not status_id:
                flash("Заполните обязательные поля.", "error")
            else:
                db = get_db()
                try:
                    db.execute(
                        """
                        UPDATE sim_cards
                        SET phone_number = ?, iccid = ?, ip_address = ?, operator = ?, description = ?, tariff = ?, status_id = ?
                        WHERE id = ?
                        """,
                        (phone_number, iccid, ip_address, operator, description, tariff, status_id, sim_id),
                    )
                    db.commit()
                    flash("SIM-карта обновлена.", "success")
                    return redirect(url_for("sim_cards_list"))
                except sqlite3.IntegrityError:
                    flash("ICCID должен быть уникальным.", "error")
        return render_template("sim_card_form.html", sim_card=sim_card, statuses=statuses)

    @app.route("/sim_cards/<int:sim_id>/delete", methods=["POST"])
    def sim_card_delete(sim_id):
        db = get_db()
        # Проверяем, установлена ли SIM-карта на технику
        active_install = fetch_one(
            """
            SELECT msc.id, m.inventory_number
            FROM machine_sim_cards msc
            JOIN machines m ON msc.machine_id = m.id
            WHERE msc.sim_card_id = ? AND msc.removed_at IS NULL
            LIMIT 1
            """,
            (sim_id,),
        )
        if active_install:
            flash(
                f"Нельзя удалить SIM-карту: она установлена на технику {active_install['inventory_number']}. "
                "Сначала демонтируйте SIM-карту.",
                "error",
            )
            return redirect(url_for("sim_cards_list"))
        db.execute("DELETE FROM sim_cards WHERE id = ?", (sim_id,))
        db.commit()
        flash("SIM-карта удалена.", "success")
        return redirect(url_for("sim_cards_list"))

    @app.route("/sim_cards/<int:sim_id>")
    def sim_card_detail(sim_id):
        sim_card = fetch_one(
            """
            SELECT sc.*, ss.name AS status_name
            FROM sim_cards sc
            JOIN sim_statuses ss ON sc.status_id = ss.id
            WHERE sc.id = ?
            """,
            (sim_id,),
        )
        if sim_card is None:
            flash("SIM-карта не найдена.", "error")
            return redirect(url_for("sim_cards_list"))

        mounts = fetch_all(
            """
            SELECT msc.*, m.inventory_number
            FROM machine_sim_cards msc
            JOIN machines m ON msc.machine_id = m.id
            WHERE msc.sim_card_id = ?
            ORDER BY msc.installed_at DESC
            """,
            (sim_id,),
        )
        logs = fetch_all(
            """
            SELECT * FROM operations_log
            WHERE entity_type = 'sim_card' AND entity_id = ?
            ORDER BY created_at DESC
            """,
            (sim_id,),
        )
        return render_template("sim_card_detail.html", sim_card=sim_card, mounts=mounts, logs=logs)

    # --- Equipment installation on machine ---

    def _insert_equipment_link(machine_id, equipment_id, sensor_position_id):
        """Записать установку оборудования. False, если его/позицию успел занять другой запрос."""
        db = get_db()
        try:
            db.execute(
                """
                INSERT INTO machine_equipment (machine_id, equipment_id, installed_at, sensor_position_id)
                VALUES (?, ?, ?, ?)
                """,
                (machine_id, equipment_id, datetime.now().isoformat(sep=" ", timespec="seconds"), sensor_position_id),
            )
            db.execute("UPDATE equipment SET status_id = 2 WHERE id = ?", (equipment_id,))
            db.commit()
            return True
        except sqlite3.IntegrityError:
            db.rollback()
            return False

    @app.route("/machines/<int:machine_id>/install_equipment", methods=["GET", "POST"])
    def install_equipment(machine_id):
        machine = fetch_one("SELECT * FROM machines WHERE id = ?", (machine_id,))
        if machine is None:
            flash("Техника не найдена.", "error")
            return redirect(url_for("machines_list"))

        # Свободное оборудование "На складе" (без привязки к технике) с постраничным выводом и серверным поиском
        q = (request.args.get("q") or "").strip()
        try:
            page = max(1, min(100000, to_int(request.args.get("page"), 1)))
        except (TypeError, ValueError):
            page = 1
        try:
            per_page = int(request.args.get("per_page", 50) or 50)
        except (TypeError, ValueError):
            per_page = 50
        per_page = max(10, min(200, per_page))
        offset = (page - 1) * per_page

        where_sql = [
            "e.status_id = 1",
            "NOT EXISTS (SELECT 1 FROM machine_equipment me WHERE me.equipment_id = e.id AND me.removed_at IS NULL)",
        ]
        params = []
        if q:
            where_sql.append(
                "(e.name LIKE ? OR e.serial_number LIKE ? OR IFNULL(e.description, '') LIKE ?)"
            )
            like = f"%{q}%"
            params.extend([like, like, like])
        where_clause = " AND ".join(where_sql)

        total_free = fetch_one(
            f"SELECT COUNT(*) AS c FROM equipment e WHERE {where_clause}",
            tuple(params),
        )["c"]

        free_equipment = [
            dict(row)
            for row in fetch_all(
                f"""
                SELECT e.*
                FROM equipment e
                WHERE {where_clause}
                ORDER BY e.id
                LIMIT ? OFFSET ?
                """,
                (*params, per_page, offset),
            )
        ]
        used_positions = {
            row["sensor_position_id"]
            for row in fetch_all(
                "SELECT sensor_position_id FROM machine_equipment WHERE machine_id = ? AND removed_at IS NULL AND sensor_position_id IS NOT NULL",
                (machine_id,),
            )
        }
        positions = [
            dict(r)
            for r in fetch_all(
                "SELECT * FROM sensor_positions ORDER BY COALESCE(sort_order, 99), id"
            )
        ]
        if request.method == "POST":
            equipment_id = to_int(request.form.get("equipment_id"))
            raw_sensor_position = request.form.get("sensor_position_id")
            # Пустое значение или специальное значение "none" означают установку датчика без позиции
            if raw_sensor_position in (None, "", "none"):
                sensor_position_id = None
            else:
                sensor_position_id = ref_id("sensor_positions", raw_sensor_position)
            if not equipment_id:
                flash("Выберите оборудование.", "error")
            elif raw_sensor_position not in (None, "", "none") and sensor_position_id is None:
                flash("Позиция датчика не найдена.", "error")
            else:
                eq = fetch_one("SELECT id, equipment_kind, name, status_id FROM equipment WHERE id = ?", (equipment_id,))
                busy = fetch_one(
                    "SELECT 1 FROM machine_equipment WHERE equipment_id = ? AND removed_at IS NULL", (equipment_id,)
                )
                if eq is None:
                    flash("Оборудование не найдено.", "error")
                elif busy or eq["status_id"] != 1:
                    flash("Это оборудование уже установлено или не числится на складе.", "error")
                elif (eq["equipment_kind"] if "equipment_kind" in eq.keys() else "other") == "sensor":
                    # Для датчиков позиция теперь необязательна, но одна и та же позиция
                    # не может быть использована несколькими датчиками одновременно.
                    if sensor_position_id and sensor_position_id in used_positions:
                        flash("Эта позиция уже занята.", "error")
                    else:
                        if not _insert_equipment_link(machine_id, equipment_id, sensor_position_id):
                            flash("Не удалось установить: оборудование или позиция только что заняты.", "error")
                            return redirect(url_for("machine_detail", machine_id=machine_id))
                        eq_name = (str(eq["name"]).strip() if "name" in eq.keys() and eq["name"] else None) or f"#{equipment_id}"
                        log_operation("install", "equipment", int(equipment_id), f"Установка датчика «{eq_name}» на технику {machine['inventory_number']}")
                        flash("Оборудование установлено.", "success")
                        return redirect(url_for("machine_detail", machine_id=machine_id))
                else:
                    if not _insert_equipment_link(machine_id, equipment_id, None):
                        flash("Не удалось установить: оборудование только что установлено на другую технику.", "error")
                        return redirect(url_for("machine_detail", machine_id=machine_id))
                    eq_name = (str(eq["name"]).strip() if "name" in eq.keys() and eq["name"] else None) or f"#{equipment_id}"
                    log_operation("install", "equipment", int(equipment_id), f"Установка оборудования «{eq_name}» на технику {machine['inventory_number']}")
                    flash("Оборудование установлено.", "success")
                    return redirect(url_for("machine_detail", machine_id=machine_id))
        total_pages = max(1, (total_free + per_page - 1) // per_page)
        pagination = {
            "page": page,
            "per_page": per_page,
            "total": total_free,
            "total_pages": total_pages,
        }
        return render_template(
            "install_equipment.html",
            machine=machine,
            equipment_list=free_equipment,
            positions=positions,
            used_positions=used_positions,
            pagination=pagination,
        )

    @app.route("/machines/<int:machine_id>/remove_equipment/<int:link_id>", methods=["POST"])
    def remove_equipment(machine_id, link_id):
        db = get_db()
        link = fetch_one(
            "SELECT * FROM machine_equipment WHERE id = ? AND machine_id = ? AND removed_at IS NULL",
            (link_id, machine_id),
        )
        if link is None:
            flash("Связь оборудование-техника не найдена или уже демонтирована.", "error")
            return redirect(url_for("machine_detail", machine_id=machine_id))

        now = datetime.now().isoformat(sep=" ", timespec="seconds")
        db.execute(
            "UPDATE machine_equipment SET removed_at = ? WHERE id = ?",
            (now, link_id),
        )
        db.execute(
            "UPDATE equipment SET status_id = 3 WHERE id = ?",
            (link["equipment_id"],),
        )
        db.commit()
        machine = fetch_one("SELECT inventory_number FROM machines WHERE id = ?", (machine_id,))
        inv_num = machine["inventory_number"] if machine else str(machine_id)
        eq = fetch_one("SELECT name FROM equipment WHERE id = ?", (link["equipment_id"],))
        eq_name = (eq["name"] if eq else "") or f"#{link['equipment_id']}"
        log_operation(
            "remove",
            "equipment",
            int(link["equipment_id"]),
            f"Демонтаж оборудования «{eq_name}» с техники {inv_num}",
        )
        flash("Оборудование демонтировано.", "success")
        return redirect(url_for("machine_detail", machine_id=machine_id))

    # --- SIM installation on machine ---
    @app.route("/machines/<int:machine_id>/install_sim", methods=["GET", "POST"])
    def install_sim(machine_id):
        machine = fetch_one("SELECT * FROM machines WHERE id = ?", (machine_id,))
        if machine is None:
            flash("Техника не найдена.", "error")
            return redirect(url_for("machines_list"))

        active_sim = fetch_one(
            "SELECT * FROM machine_sim_cards WHERE machine_id = ? AND removed_at IS NULL",
            (machine_id,),
        )
        if active_sim:
            flash("На этой технике уже установлена SIM-карта. Сначала демонтируйте её.", "error")
            return redirect(url_for("machine_detail", machine_id=machine_id))

        # Свободные SIM: статус "На складе" (id=1) и не установлены ни на одной технике — с постраничным выводом и серверным поиском
        q = (request.args.get("q") or "").strip()
        try:
            page = max(1, min(100000, to_int(request.args.get("page"), 1)))
        except (TypeError, ValueError):
            page = 1
        try:
            per_page = int(request.args.get("per_page", 50) or 50)
        except (TypeError, ValueError):
            per_page = 50
        per_page = max(10, min(200, per_page))
        offset = (page - 1) * per_page

        where_sql = [
            "sc.status_id = 1",
            "NOT EXISTS (SELECT 1 FROM machine_sim_cards msc WHERE msc.sim_card_id = sc.id AND msc.removed_at IS NULL)",
        ]
        params = []
        if q:
            where_sql.append(
                "(sc.phone_number LIKE ? OR sc.iccid LIKE ? OR IFNULL(sc.ip_address, '') LIKE ?)"
            )
            like = f"%{q}%"
            params.extend([like, like, like])
        where_clause = " AND ".join(where_sql)

        total_free = fetch_one(
            f"SELECT COUNT(*) AS c FROM sim_cards sc WHERE {where_clause}",
            tuple(params),
        )["c"]

        free_sims = fetch_all(
            f"""
            SELECT sc.*
            FROM sim_cards sc
            WHERE {where_clause}
            ORDER BY sc.id
            LIMIT ? OFFSET ?
            """,
            (*params, per_page, offset),
        )
        if request.method == "POST":
            sim_id = to_int(request.form.get("sim_card_id"))
            sim_row = fetch_one("SELECT status_id FROM sim_cards WHERE id = ?", (sim_id,)) if sim_id else None
            sim_busy = sim_id and fetch_one(
                "SELECT 1 FROM machine_sim_cards WHERE sim_card_id = ? AND removed_at IS NULL", (sim_id,)
            )
            if not sim_id:
                flash("Выберите SIM-карту.", "error")
            elif sim_row is None:
                flash("SIM-карта не найдена.", "error")
            elif sim_busy or sim_row["status_id"] != 1:
                flash("Эта SIM-карта уже установлена или не числится на складе.", "error")
            else:
                db = get_db()
                try:
                    db.execute(
                        """
                        INSERT INTO machine_sim_cards (machine_id, sim_card_id, installed_at)
                        VALUES (?, ?, ?)
                        """,
                        (machine_id, sim_id, datetime.now().isoformat(sep=" ", timespec="seconds")),
                    )
                    # Статус SIM -> Установлена (id=2)
                    db.execute(
                        "UPDATE sim_cards SET status_id = 2 WHERE id = ?",
                        (sim_id,),
                    )
                    db.commit()
                except sqlite3.IntegrityError:
                    db.rollback()
                    flash("Не удалось установить: SIM-карта или техника только что заняты.", "error")
                    return redirect(url_for("machine_detail", machine_id=machine_id))
                sim_card = fetch_one("SELECT phone_number FROM sim_cards WHERE id = ?", (sim_id,))
                phone = (sim_card["phone_number"] if sim_card else "") or f"#{sim_id}"
                log_operation(
                    "install",
                    "sim_card",
                    int(sim_id),
                    f"Установка SIM-карты {phone} на технику {machine['inventory_number']}",
                )
                flash("SIM-карта установлена.", "success")
                return redirect(url_for("machine_detail", machine_id=machine_id))
        total_pages = max(1, (total_free + per_page - 1) // per_page)
        pagination = {
            "page": page,
            "per_page": per_page,
            "total": total_free,
            "total_pages": total_pages,
        }
        return render_template("install_sim.html", machine=machine, sims=free_sims, pagination=pagination)

    @app.route("/machines/<int:machine_id>/remove_sim/<int:link_id>", methods=["POST"])
    def remove_sim(machine_id, link_id):
        db = get_db()
        link = fetch_one(
            "SELECT * FROM machine_sim_cards WHERE id = ? AND machine_id = ? AND removed_at IS NULL",
            (link_id, machine_id),
        )
        if link is None:
            flash("Связь SIM-карта-техника не найдена или уже демонтирована.", "error")
            return redirect(url_for("machine_detail", machine_id=machine_id))

        now = datetime.now().isoformat(sep=" ", timespec="seconds")
        db.execute(
            "UPDATE machine_sim_cards SET removed_at = ? WHERE id = ?",
            (now, link_id),
        )
        # Статус SIM -> На складе (id=1)
        db.execute(
            "UPDATE sim_cards SET status_id = 1 WHERE id = ?",
            (link["sim_card_id"],),
        )
        db.commit()
        machine = fetch_one("SELECT inventory_number FROM machines WHERE id = ?", (machine_id,))
        inv_num = machine["inventory_number"] if machine else str(machine_id)
        sim_card = fetch_one("SELECT phone_number FROM sim_cards WHERE id = ?", (link["sim_card_id"],))
        phone = (sim_card["phone_number"] if sim_card else "") or f"#{link['sim_card_id']}"
        log_operation(
            "remove",
            "sim_card",
            int(link["sim_card_id"]),
            f"Демонтаж SIM-карты {phone} с техники {inv_num}",
        )
        flash("SIM-карта демонтирована.", "success")
        return redirect(url_for("machine_detail", machine_id=machine_id))

    # --- Operations log ---

    @app.route("/operations")
    def operations_list():
        try:
            per_page = max(1, min(500, int(request.args.get("per_page") or 50)))
        except (TypeError, ValueError):
            per_page = 50
        try:
            page = max(1, min(100000, to_int(request.args.get("page"), 1)))
        except (TypeError, ValueError):
            page = 1
        try:
            row = fetch_one("SELECT COUNT(*) AS c FROM operations_log")
            total = int(row["c"]) if row and row["c"] is not None else 0
        except (sqlite3.OperationalError, TypeError, ValueError):
            total = 0
        total_pages = max(1, (total + per_page - 1) // per_page) if total else 1
        page = min(page, total_pages)
        offset = (page - 1) * per_page
        order_key = request.args.get("order") or "desc"
        if order_key not in ("asc", "desc"):
            order_key = "desc"
        order_sql = "DESC" if order_key == "desc" else "ASC"
        # Колонка username может отсутствовать (добавляется миграцией migrate_operations_log_username)
        has_username = False
        try:
            for row in fetch_all("PRAGMA table_info(operations_log)"):
                if len(row) >= 2 and row[1] == "username":
                    has_username = True
                    break
        except Exception:
            pass
        select_cols = "id, datetime(created_at, 'localtime') AS created_at, operation_type, entity_type, entity_id, description"
        if has_username:
            select_cols += ", username"
        rows = fetch_all(
            f"""
            SELECT {select_cols}
            FROM operations_log
            ORDER BY created_at {order_sql}, id {order_sql}
            LIMIT ? OFFSET ?
            """,
            (per_page, offset),
        )
        operations = []
        for row in rows:
            op = dict(row)
            if "username" not in op:
                op["username"] = None
            if op.get("description"):
                op["description"] = fix_mojibake_text(op["description"])
            operations.append(op)
        return render_template(
            "operations_list.html",
            operations=operations,
            pagination={"page": page, "per_page": per_page, "total": total, "total_pages": total_pages},
            order=order_key,
        )

    @app.route("/operations/export")
    def operations_export_csv():
        has_username = False
        try:
            for r in fetch_all("PRAGMA table_info(operations_log)"):
                if len(r) >= 2 and r[1] == "username":
                    has_username = True
                    break
        except Exception:
            pass
        select_cols = "datetime(created_at, 'localtime') AS created_at, operation_type, entity_type, entity_id, description"
        if has_username:
            select_cols += ", username"
        rows = fetch_all(
            f"""
            SELECT {select_cols}
            FROM operations_log
            ORDER BY created_at DESC, id DESC
            LIMIT 10000
            """
        )
        buf = io.StringIO()
        buf.write("\ufeff")  # BOM для корректного открытия в Excel
        writer = csv.writer(buf, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        writer.writerow(["Дата", "Операция", "Сущность", "ID сущности", "Описание", "Кто"])
        for row in rows:
            r = dict(row)
            desc = r.get("description") or ""
            if desc:
                desc = fix_mojibake_text(desc)
            writer.writerow(
                [
                    r.get("created_at") or "",
                    r.get("operation_type") or "",
                    r.get("entity_type") or "",
                    r.get("entity_id") or "",
                    desc,
                    (r.get("username") or ""),
                ]
            )
        filename = f"operations_{datetime.now().strftime('%Y-%m-%d_%H-%M')}.csv"
        return Response(
            buf.getvalue(),
            mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.route("/export/excel")
    def export_inventory_excel():
        """Экспорт в Excel: оборудование, установленное оборудование, SIM-карты, техника."""
        wb = Workbook()
        header_font = Font(bold=True)
        thin_border = Border(
            left=Side(style="thin"),
            right=Side(style="thin"),
            top=Side(style="thin"),
            bottom=Side(style="thin"),
        )

        # Лист 1: Оборудование (все, с указанием где установлено)
        ws_eq = wb.active
        ws_eq.title = "Оборудование"
        eq_headers = ["ID", "Наименование", "Серийный номер", "Описание", "Статус", "Вид", "Установлено на (инв. №)", "Модель техники", "Позиция", "Дата установки"]
        for col, h in enumerate(eq_headers, 1):
            c = ws_eq.cell(row=1, column=col, value=h)
            c.font = header_font
            c.border = thin_border
        equipment = fetch_all(
            """
            SELECT e.id, e.name, e.serial_number, e.description, e.equipment_kind,
                   es.name AS status_name,
                   m.inventory_number AS machine_inv, mm.name AS machine_model_name,
                   sp.code AS position_code, me.installed_at
            FROM equipment e
            JOIN equipment_statuses es ON e.status_id = es.id
            LEFT JOIN machine_equipment me ON me.equipment_id = e.id AND me.removed_at IS NULL
            LEFT JOIN machines m ON me.machine_id = m.id
            LEFT JOIN machine_models mm ON m.machine_model_id = mm.id
            LEFT JOIN sensor_positions sp ON me.sensor_position_id = sp.id
            ORDER BY e.name, e.serial_number
            """
        )
        for row_idx, row in enumerate(equipment, 2):
            r = dict(row)
            ws_eq.cell(row=row_idx, column=1, value=r.get("id"))
            ws_eq.cell(row=row_idx, column=2, value=r.get("name") or "")
            ws_eq.cell(row=row_idx, column=3, value=r.get("serial_number") or "")
            ws_eq.cell(row=row_idx, column=4, value=r.get("description") or "")
            ws_eq.cell(row=row_idx, column=5, value=r.get("status_name") or "")
            ws_eq.cell(row=row_idx, column=6, value="Датчик" if r.get("equipment_kind") == "sensor" else "Прочее")
            ws_eq.cell(row=row_idx, column=7, value=r.get("machine_inv") or "—")
            ws_eq.cell(row=row_idx, column=8, value=r.get("machine_model_name") or "")
            ws_eq.cell(row=row_idx, column=9, value=r.get("position_code") or "")
            ws_eq.cell(row=row_idx, column=10, value=r.get("installed_at") or "")

        # Лист 2: Установлено на технике (одна строка — одна установка)
        ws_inst = wb.create_sheet("Установлено на технике")
        inst_headers = ["Инв. номер техники", "Модель", "Тип техники", "Наименование оборудования", "Серийный номер", "Позиция", "Дата установки"]
        for col, h in enumerate(inst_headers, 1):
            c = ws_inst.cell(row=1, column=col, value=h)
            c.font = header_font
            c.border = thin_border
        installed = fetch_all(
            """
            SELECT m.inventory_number, mm.name AS model_name, mt.name AS type_name,
                   e.name AS equipment_name, e.serial_number,
                   sp.code AS position_code, me.installed_at
            FROM machine_equipment me
            JOIN machines m ON me.machine_id = m.id
            JOIN machine_models mm ON m.machine_model_id = mm.id
            JOIN machine_types mt ON mm.machine_type_id = mt.id
            JOIN equipment e ON me.equipment_id = e.id
            LEFT JOIN sensor_positions sp ON me.sensor_position_id = sp.id
            WHERE me.removed_at IS NULL
            ORDER BY m.inventory_number, e.name
            """
        )
        for row_idx, row in enumerate(installed, 2):
            r = dict(row)
            ws_inst.cell(row=row_idx, column=1, value=r.get("inventory_number") or "")
            ws_inst.cell(row=row_idx, column=2, value=r.get("model_name") or "")
            ws_inst.cell(row=row_idx, column=3, value=r.get("type_name") or "")
            ws_inst.cell(row=row_idx, column=4, value=r.get("equipment_name") or "")
            ws_inst.cell(row=row_idx, column=5, value=r.get("serial_number") or "")
            ws_inst.cell(row=row_idx, column=6, value=r.get("position_code") or "")
            ws_inst.cell(row=row_idx, column=7, value=r.get("installed_at") or "")

        # Лист 3: SIM-карты
        ws_sim = wb.create_sheet("SIM-карты")
        sim_headers = ["Номер", "ICCID", "Оператор", "Тариф", "Описание", "IP", "Статус", "Установлена на (инв. №)", "Модель техники", "Дата установки"]
        for col, h in enumerate(sim_headers, 1):
            c = ws_sim.cell(row=1, column=col, value=h)
            c.font = header_font
            c.border = thin_border
        sim_cards = fetch_all(
            """
            SELECT sc.phone_number, sc.iccid, sc.operator, sc.tariff, sc.description, sc.ip_address, ss.name AS status_name,
                   m.inventory_number, mm.name AS machine_model_name, msc.installed_at
            FROM sim_cards sc
            JOIN sim_statuses ss ON sc.status_id = ss.id
            LEFT JOIN machine_sim_cards msc ON msc.sim_card_id = sc.id AND msc.removed_at IS NULL
            LEFT JOIN machines m ON msc.machine_id = m.id
            LEFT JOIN machine_models mm ON m.machine_model_id = mm.id
            ORDER BY sc.phone_number
            """
        )
        for row_idx, row in enumerate(sim_cards, 2):
            r = dict(row)
            ws_sim.cell(row=row_idx, column=1, value=r.get("phone_number") or "")
            ws_sim.cell(row=row_idx, column=2, value=r.get("iccid") or "")
            ws_sim.cell(row=row_idx, column=3, value=r.get("operator") or "")
            ws_sim.cell(row=row_idx, column=4, value=r.get("tariff") or "")
            ws_sim.cell(row=row_idx, column=5, value=r.get("description") or "")
            ws_sim.cell(row=row_idx, column=6, value=r.get("ip_address") or "")
            ws_sim.cell(row=row_idx, column=7, value=r.get("status_name") or "")
            ws_sim.cell(row=row_idx, column=8, value=r.get("inventory_number") or "—")
            ws_sim.cell(row=row_idx, column=9, value=r.get("machine_model_name") or "")
            ws_sim.cell(row=row_idx, column=10, value=r.get("installed_at") or "")

        # Лист 4: Техника (справочник)
        ws_mach = wb.create_sheet("Техника")
        mach_headers = ["Инв. номер", "Модель", "Тип техники"]
        for col, h in enumerate(mach_headers, 1):
            c = ws_mach.cell(row=1, column=col, value=h)
            c.font = header_font
            c.border = thin_border
        machines = fetch_all(
            """
            SELECT m.inventory_number, mm.name AS model_name, mt.name AS type_name
            FROM machines m
            JOIN machine_models mm ON m.machine_model_id = mm.id
            JOIN machine_types mt ON mm.machine_type_id = mt.id
            ORDER BY m.inventory_number
            """
        )
        for row_idx, row in enumerate(machines, 2):
            r = dict(row)
            ws_mach.cell(row=row_idx, column=1, value=r.get("inventory_number") or "")
            ws_mach.cell(row=row_idx, column=2, value=r.get("model_name") or "")
            ws_mach.cell(row=row_idx, column=3, value=r.get("type_name") or "")

        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        filename = f"inventory_export_{datetime.now().strftime('%Y-%m-%d_%H-%M')}.xlsx"
        return Response(
            buf.getvalue(),
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    # --- Отчёты и аналитика ---
    @app.route("/reports")
    def reports():
        """Отчёты и аналитика: сводка по складу, графики, выгрузка по периоду."""
        # Оборудование по статусам (для круговой диаграммы)
        equipment_by_status = fetch_all(
            """
            SELECT es.name AS status_name, COUNT(e.id) AS cnt
            FROM equipment e
            JOIN equipment_statuses es ON e.status_id = es.id
            GROUP BY e.status_id
            ORDER BY cnt DESC
            """
        )
        equipment_by_status = [dict(r) for r in equipment_by_status]
        eq_chart_labels = [r["status_name"] for r in equipment_by_status]
        eq_chart_data = [r["cnt"] for r in equipment_by_status]

        # ТО по месяцам (последние 12 месяцев)
        maintenance_by_month = fetch_all(
            """
            SELECT strftime('%Y-%m', performed_at) AS month, COUNT(*) AS cnt
            FROM machine_maintenance
            WHERE performed_at >= date('now', 'localtime', '-12 months')
            GROUP BY strftime('%Y-%m', performed_at)
            ORDER BY month
            """
        )
        month_counts = {r["month"]: r["cnt"] for r in maintenance_by_month}
        month_names = ("янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек")
        maint_months = []
        maint_data = []
        for i in range(11, -1, -1):
            d = date.today()
            month = d.month - i
            year = d.year
            while month < 1:
                month += 12
                year -= 1
            m_str = f"{year}-{month:02d}"
            maint_months.append(f"{month_names[month - 1]} {year}")
            maint_data.append(month_counts.get(m_str, 0))

        # Сводка по складу: числа для блоков
        warehouse_summary = {
            "equipment_total": fetch_one("SELECT COUNT(*) AS c FROM equipment", ())["c"],
            "equipment_warehouse": fetch_one(
                "SELECT COUNT(*) AS c FROM equipment WHERE status_id = 1", ()
            )["c"],
            "equipment_installed": fetch_one(
                "SELECT COUNT(*) AS c FROM equipment WHERE status_id = 2",
                (),
            )["c"],
            "equipment_other": fetch_one(
                "SELECT COUNT(*) AS c FROM equipment WHERE status_id NOT IN (1, 2)", ()
            )["c"],
            "machines_total": fetch_one("SELECT COUNT(*) AS c FROM machines", ())["c"],
            "sim_total": fetch_one("SELECT COUNT(*) AS c FROM sim_cards", ())["c"],
            "sim_warehouse": fetch_one(
                "SELECT COUNT(*) AS c FROM sim_cards WHERE status_id = 1", ()
            )["c"],
        }
        # Оборудование по видам (датчики / прочее)
        equipment_by_kind = fetch_all(
            """
            SELECT equipment_kind, COUNT(*) AS cnt
            FROM equipment
            GROUP BY equipment_kind
            """
        )
        equipment_by_kind = [dict(r) for r in equipment_by_kind]
        # Техника по типам
        machines_by_type = fetch_all(
            """
            SELECT mt.name AS type_name, COUNT(m.id) AS cnt
            FROM machines m
            JOIN machine_models mm ON m.machine_model_id = mm.id
            JOIN machine_types mt ON mm.machine_type_id = mt.id
            GROUP BY mt.id
            ORDER BY cnt DESC
            """
        )
        machines_by_type = [dict(r) for r in machines_by_type]

        today_iso = date.today().isoformat()
        date_from_default = (date.today() - timedelta(days=30)).isoformat()
        date_30_ago = (date.today() - timedelta(days=30)).isoformat()
        # Задания на смену за последние 30 дней: по статусам
        shift_tasks_by_status = fetch_all(
            """
            SELECT status, COUNT(*) AS cnt
            FROM shift_tasks
            WHERE effective_date >= ?
            GROUP BY status
            ORDER BY cnt DESC
            """,
            (date_30_ago,),
        )
        shift_tasks_by_status = [dict(r) for r in shift_tasks_by_status]
        status_labels_ru = {"pending": "Ожидает", "done": "Выполнено", "not_done": "Не выполнено"}
        tasks_status_labels = [status_labels_ru.get(r["status"], r["status"]) for r in shift_tasks_by_status]
        tasks_status_data = [r["cnt"] for r in shift_tasks_by_status]
        # Задания на смену за последние 30 дней: по типам
        shift_tasks_by_type = fetch_all(
            """
            SELECT tt.name AS type_name, COUNT(st.id) AS cnt
            FROM shift_tasks st
            LEFT JOIN shift_task_types tt ON st.task_type_id = tt.id
            WHERE st.effective_date >= ?
            GROUP BY st.task_type_id, tt.name
            ORDER BY cnt DESC
            """,
            (date_30_ago,),
        )
        shift_tasks_by_type = [dict(r) for r in shift_tasks_by_type]
        tasks_type_labels = [r["type_name"] or "—" for r in shift_tasks_by_type]
        tasks_type_data = [r["cnt"] for r in shift_tasks_by_type]
        # Задания по пользователям за 30 дней: создал / выполнил / отметил не выполнено
        created_by_user = fetch_all(
            """
            SELECT u.id AS user_id, COALESCE(NULLIF(TRIM(u.surname || ' ' || u.first_name), ''), u.username) AS user_name, COUNT(st.id) AS cnt
            FROM shift_tasks st
            JOIN users u ON st.created_by_user_id = u.id
            WHERE st.effective_date >= ?
            GROUP BY u.id
            """,
            (date_30_ago,),
        )
        completed_by_user = fetch_all(
            """
            SELECT u.id AS user_id, COALESCE(NULLIF(TRIM(u.surname || ' ' || u.first_name), ''), u.username) AS user_name, COUNT(c.id) AS cnt
            FROM shift_task_completions c
            JOIN users u ON c.user_id = u.id
            JOIN shift_tasks st ON c.shift_task_id = st.id
            WHERE st.effective_date >= ?
            GROUP BY u.id
            """,
            (date_30_ago,),
        )
        not_done_by_user = fetch_all(
            """
            SELECT u.id AS user_id, COALESCE(NULLIF(TRIM(u.surname || ' ' || u.first_name), ''), u.username) AS user_name, COUNT(st.id) AS cnt
            FROM shift_tasks st
            JOIN users u ON st.not_done_by_user_id = u.id
            WHERE st.effective_date >= ? AND st.status = 'not_done'
            GROUP BY u.id
            """,
            (date_30_ago,),
        )
        user_ids = set()
        for r in created_by_user:
            user_ids.add(r["user_id"])
        for r in completed_by_user:
            user_ids.add(r["user_id"])
        for r in not_done_by_user:
            user_ids.add(r["user_id"])
        created_map = {r["user_id"]: (r["user_name"], r["cnt"]) for r in created_by_user}
        completed_map = {r["user_id"]: (r["user_name"], r["cnt"]) for r in completed_by_user}
        not_done_map = {r["user_id"]: (r["user_name"], r["cnt"]) for r in not_done_by_user}
        tasks_by_user = []
        for uid in user_ids:
            name = (created_map.get(uid) or completed_map.get(uid) or not_done_map.get(uid))[0]
            created = created_map[uid][1] if uid in created_map else 0
            completed = completed_map[uid][1] if uid in completed_map else 0
            not_done = not_done_map[uid][1] if uid in not_done_map else 0
            tasks_by_user.append(
                {"user_id": uid, "user_name": name, "created": created, "completed": completed, "not_done": not_done}
            )
        tasks_by_user.sort(key=lambda x: (x["completed"], x["created"]), reverse=True)
        return render_template(
            "reports.html",
            equipment_by_status=equipment_by_status,
            eq_chart_labels=eq_chart_labels,
            eq_chart_data=eq_chart_data,
            maint_chart_labels=maint_months,
            maint_chart_data=maint_data,
            warehouse_summary=warehouse_summary,
            equipment_by_kind=equipment_by_kind,
            machines_by_type=machines_by_type,
            date_from_default=date_from_default,
            date_to_default=today_iso,
            tasks_status_labels=tasks_status_labels,
            tasks_status_data=tasks_status_data,
            tasks_type_labels=tasks_type_labels,
            tasks_type_data=tasks_type_data,
            tasks_by_user=tasks_by_user,
        )

    @app.route("/reports/export")
    def reports_export():
        """Выгрузка отчёта за период: операции, ТО, техника, оборудование."""
        report_type = (request.args.get("type") or "operations").strip().lower()
        if report_type not in ("operations", "maintenance", "machines", "equipment"):
            report_type = "operations"
        date_from = (request.args.get("from") or "").strip()[:10]
        date_to = (request.args.get("to") or "").strip()[:10]
        if not date_from:
            date_from = (date.today() - timedelta(days=30)).isoformat()
        if not date_to:
            date_to = date.today().isoformat()

        if report_type == "operations":
            rows = fetch_all(
                """
                SELECT datetime(created_at, 'localtime') AS created_at, operation_type, entity_type, entity_id, description, username
                FROM operations_log
                WHERE date(created_at) BETWEEN ? AND ?
                ORDER BY created_at DESC
                """,
                (date_from, date_to),
            )
            rows = [dict(r) for r in rows]
            buf = io.StringIO()
            buf.write("\ufeff")
            writer = csv.writer(buf, delimiter=";", quoting=csv.QUOTE_MINIMAL)
            writer.writerow(["Дата", "Операция", "Сущность", "ID", "Описание", "Кто"])
            for r in rows:
                writer.writerow([
                    r.get("created_at") or "",
                    r.get("operation_type") or "",
                    r.get("entity_type") or "",
                    r.get("entity_id") or "",
                    (r.get("description") or "").replace("\n", " "),
                    r.get("username") or "",
                ])
            filename = f"operations_{date_from}_{date_to}.csv"
            mimetype = "text/csv; charset=utf-8"
        elif report_type == "maintenance":
            rows = fetch_all(
                """
                SELECT m.inventory_number, mm.name AS model_name, mt.name AS type_name,
                       mmt.performed_at, mmt.next_due_at, mmt.performed_by, mmt.remarks
                FROM machine_maintenance mmt
                JOIN machines m ON mmt.machine_id = m.id
                JOIN machine_models mm ON m.machine_model_id = mm.id
                JOIN machine_types mt ON mm.machine_type_id = mt.id
                WHERE date(mmt.performed_at) BETWEEN ? AND ?
                ORDER BY mmt.performed_at DESC
                """,
                (date_from, date_to),
            )
            rows = [dict(r) for r in rows]
            buf = io.StringIO()
            buf.write("\ufeff")
            writer = csv.writer(buf, delimiter=";", quoting=csv.QUOTE_MINIMAL)
            writer.writerow(["Инв. номер", "Модель", "Тип", "Дата проведения", "Следующее ТО", "Исполнитель", "Замечания"])
            for r in rows:
                writer.writerow([
                    r.get("inventory_number") or "",
                    r.get("model_name") or "",
                    r.get("type_name") or "",
                    r.get("performed_at") or "",
                    r.get("next_due_at") or "",
                    r.get("performed_by") or "",
                    (r.get("remarks") or "").replace("\n", " "),
                ])
            filename = f"maintenance_{date_from}_{date_to}.csv"
            mimetype = "text/csv; charset=utf-8"
        elif report_type == "machines":
            rows = fetch_all(
                """
                SELECT m.inventory_number, mm.name AS model_name, mt.name AS type_name
                FROM machines m
                JOIN machine_models mm ON m.machine_model_id = mm.id
                JOIN machine_types mt ON mm.machine_type_id = mt.id
                ORDER BY m.inventory_number
                """
            )
            rows = [dict(r) for r in rows]
            buf = io.StringIO()
            buf.write("\ufeff")
            writer = csv.writer(buf, delimiter=";", quoting=csv.QUOTE_MINIMAL)
            writer.writerow(["Инв. номер", "Модель", "Тип техники"])
            for r in rows:
                writer.writerow([r.get("inventory_number") or "", r.get("model_name") or "", r.get("type_name") or ""])
            filename = f"machines_{date.today().isoformat()}.csv"
            mimetype = "text/csv; charset=utf-8"
        else:
            rows = fetch_all(
                """
                SELECT e.name, e.serial_number, es.name AS status_name, e.equipment_kind, e.added_at
                FROM equipment e
                JOIN equipment_statuses es ON e.status_id = es.id
                ORDER BY e.name
                """
            )
            rows = [dict(r) for r in rows]
            buf = io.StringIO()
            buf.write("\ufeff")
            writer = csv.writer(buf, delimiter=";", quoting=csv.QUOTE_MINIMAL)
            writer.writerow(["Наименование", "Серийный номер", "Статус", "Вид", "Дата добавления"])
            for r in rows:
                writer.writerow([
                    r.get("name") or "",
                    r.get("serial_number") or "",
                    r.get("status_name") or "",
                    "Датчик" if r.get("equipment_kind") == "sensor" else "Прочее",
                    r.get("added_at") or "",
                ])
            filename = f"equipment_{date.today().isoformat()}.csv"
            mimetype = "text/csv; charset=utf-8"

        return Response(
            buf.getvalue(),
            mimetype=mimetype,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    # ===== Массовый импорт техники и оборудования =====
    @app.route("/import")
    def import_page():
        """Страница массового ввода: шаблоны и загрузка CSV."""
        return render_template("import.html")

    @app.route("/import/machines/template")
    def import_machines_template():
        """Скачать шаблон CSV для импорта техники. Колонки: Инв. номер; Модель."""
        buf = io.StringIO()
        buf.write("\ufeff")
        writer = csv.writer(buf, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        writer.writerow(["Инв. номер", "Модель"])
        writer.writerow(["A-001", "БелАЗ-7555"])
        writer.writerow(["A-002", "БелАЗ-7555"])
        buf.seek(0)
        return Response(
            buf.getvalue().encode("utf-8"),
            mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="import_machines_template.csv"'},
        )

    @app.route("/import/machines", methods=["POST"])
    def import_machines():
        """Импорт техники из CSV. Файл: разделитель ;, UTF-8, первая строка — заголовки (Инв. номер, Модель)."""
        if "file" not in request.files:
            flash("Выберите файл.", "error")
            return redirect(url_for("import_page"))
        f = request.files["file"]
        if not f or not f.filename:
            flash("Выберите файл.", "error")
            return redirect(url_for("import_page"))
        try:
            data = f.read()
            if data.startswith(b"\xef\xbb\xbf"):
                data = data[3:]
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = data.decode("cp1251")
            except Exception:
                flash("Файл должен быть в кодировке UTF-8 или Windows-1251.", "error")
                return redirect(url_for("import_page"))
        models_by_name = {}
        for row in fetch_all("SELECT id, name FROM machine_models"):
            r = dict(row)
            models_by_name[r["name"].strip().lower()] = r["id"]
        lines = [line.strip() for line in text.replace("\r", "\n").split("\n") if line.strip()]
        if not lines:
            flash("В файле нет строк.", "error")
            return redirect(url_for("import_page"))
        reader = csv.reader(io.StringIO("\n".join(lines)), delimiter=";", quoting=csv.QUOTE_MINIMAL)
        rows = list(reader)
        header = rows[0] if rows else []
        h0 = (header[0] or "").lower() if len(header) > 0 else ""
        h1 = (header[1] or "").lower() if len(header) > 1 else ""
        if not header or ("инв" not in h0 and "номер" not in h0 and "модель" not in h1):
            data_rows = rows
        else:
            data_rows = rows[1:]
        added, skipped, errors = 0, 0, []
        db = get_db()
        for i, row in enumerate(data_rows):
            inv = (row[0] or "").strip() if len(row) > 0 else ""
            model_name = (row[1] or "").strip() if len(row) > 1 else ""
            if not inv:
                skipped += 1
                errors.append(f"Строка {i + 1}: не указан инв. номер")
                continue
            if not model_name:
                skipped += 1
                errors.append(f"Строка {i + 1} ({inv}): не указана модель")
                continue
            model_id = models_by_name.get(model_name.lower())
            if not model_id:
                skipped += 1
                errors.append(f"Строка {i + 1} ({inv}): модель «{model_name}» не найдена")
                continue
            existing = fetch_one("SELECT id FROM machines WHERE inventory_number = ?", (inv,))
            if existing:
                skipped += 1
                errors.append(f"Строка {i + 1} ({inv}): такой инв. номер уже есть")
                continue
            try:
                db.execute(
                    "INSERT INTO machines (inventory_number, machine_model_id) VALUES (?, ?)",
                    (inv, model_id),
                )
                db.commit()
                added += 1
            except Exception as e:
                db.rollback()
                skipped += 1
                errors.append(f"Строка {i + 1} ({inv}): {e}")
        if added:
            flash(f"Добавлено единиц техники: {added}.", "success")
        if skipped:
            flash(f"Пропущено: {skipped}. " + ("Первые ошибки: " + "; ".join(errors[:5]) if errors else ""), "error" if errors else "info")
        return redirect(url_for("import_page"))

    @app.route("/import/equipment/template")
    def import_equipment_template():
        """Скачать шаблон CSV для импорта оборудования. Колонки: Наименование; Серийный номер; Описание; Статус; Вид (Датчик/Прочее)."""
        buf = io.StringIO()
        buf.write("\ufeff")
        writer = csv.writer(buf, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        writer.writerow(["Наименование", "Серийный номер", "Описание", "Статус", "Вид"])
        writer.writerow(["Терминал ГЛОНАСС", "SN-100001", "Основной терминал", "На складе", "Прочее"])
        writer.writerow(["Датчик давления", "SN-200001", "Колесо ПЛ", "На складе", "Датчик"])
        writer.writerow(["Датчик температуры", "SN-200002", "", "На складе", "Датчик"])
        buf.seek(0)
        return Response(
            buf.getvalue().encode("utf-8"),
            mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="import_equipment_template.csv"'},
        )

    @app.route("/import/equipment", methods=["POST"])
    def import_equipment():
        """Импорт оборудования из CSV. Колонки: Наименование; Серийный номер; Описание; Статус; Вид (Датчик/Прочее)."""
        if "file" not in request.files:
            flash("Выберите файл.", "error")
            return redirect(url_for("import_page"))
        f = request.files["file"]
        if not f or not f.filename:
            flash("Выберите файл.", "error")
            return redirect(url_for("import_page"))
        try:
            data = f.read()
            if data.startswith(b"\xef\xbb\xbf"):
                data = data[3:]
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = data.decode("cp1251")
            except Exception:
                flash("Файл должен быть в кодировке UTF-8 или Windows-1251.", "error")
                return redirect(url_for("import_page"))
        statuses_by_name = {}
        for row in fetch_all("SELECT id, name FROM equipment_statuses"):
            r = dict(row)
            statuses_by_name[r["name"].strip().lower()] = r["id"]
        default_status_id = statuses_by_name.get("на складе") or next(iter(statuses_by_name.values()), None)
        if not default_status_id:
            flash("В справочнике нет статуса оборудования.", "error")
            return redirect(url_for("import_page"))
        lines = [line.strip() for line in text.replace("\r", "\n").split("\n") if line.strip()]
        if not lines:
            flash("В файле нет строк.", "error")
            return redirect(url_for("import_page"))
        reader = csv.reader(io.StringIO("\n".join(lines)), delimiter=";", quoting=csv.QUOTE_MINIMAL)
        rows = list(reader)
        header = rows[0] if rows else []
        eh0 = (header[0] or "").lower() if len(header) > 0 else ""
        eh1 = (header[1] or "").lower() if len(header) > 1 else ""
        if not header or ("наименование" not in eh0 and "серий" not in eh1):
            data_rows = rows
        else:
            data_rows = rows[1:]
        added, skipped, errors = 0, 0, []
        db = get_db()
        for i, row in enumerate(data_rows):
            name = (row[0] or "").strip() if len(row) > 0 else ""
            serial = (row[1] or "").strip() if len(row) > 1 else ""
            description = (row[2] or "").strip() if len(row) > 2 else ""
            status_name = (row[3] or "").strip() if len(row) > 3 else "на складе"
            kind_raw = (row[4] or "").strip().lower() if len(row) > 4 else ""
            if kind_raw in ("датчик", "да", "sensor", "1", "д"):
                equipment_kind = "sensor"
            else:
                equipment_kind = "other"
            if not name or not serial:
                skipped += 1
                errors.append(f"Строка {i + 1}: не указаны наименование или серийный номер")
                continue
            status_id = statuses_by_name.get(status_name.lower()) if status_name else default_status_id
            if not status_id:
                status_id = default_status_id
            existing = fetch_one("SELECT id FROM equipment WHERE name = ? AND serial_number = ?", (name, serial))
            if existing:
                skipped += 1
                errors.append(f"Строка {i + 1} ({name} / {serial}): такая пара уже есть")
                continue
            try:
                db.execute(
                    "INSERT INTO equipment (name, serial_number, description, status_id, equipment_kind) VALUES (?, ?, ?, ?, ?)",
                    (name, serial, description or None, status_id, equipment_kind),
                )
                db.commit()
                added += 1
            except Exception as e:
                db.rollback()
                skipped += 1
                errors.append(f"Строка {i + 1}: {e}")
        if added:
            flash(f"Добавлено единиц оборудования: {added}.", "success")
        if skipped:
            flash(f"Пропущено: {skipped}. " + ("Первые ошибки: " + "; ".join(errors[:5]) if errors else ""), "error" if errors else "info")
        return redirect(url_for("import_page"))

    @app.route("/import/sim_cards/template")
    def import_sim_cards_template():
        """Скачать шаблон CSV для импорта SIM-карт. Колонки: Номер; ICCID; IP; Оператор; Описание; Тариф; Статус."""
        buf = io.StringIO()
        buf.write("\ufeff")
        writer = csv.writer(buf, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        writer.writerow(["Номер", "ICCID", "IP", "Оператор", "Описание", "Тариф", "Статус"])
        writer.writerow(["+79001234567", "89912345678901234567", "10.0.0.1", "Kcell", "Основная", "Безлимит", "На складе"])
        writer.writerow(["+79009876543", "89912345678901234568", "", "Билайн", "", "", "На складе"])
        buf.seek(0)
        return Response(
            buf.getvalue().encode("utf-8"),
            mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="import_sim_cards_template.csv"'},
        )

    @app.route("/import/sim_cards", methods=["POST"])
    def import_sim_cards():
        """Импорт SIM-карт из CSV. Колонки: Номер; ICCID; IP; Оператор; Описание; Тариф; Статус (по умолчанию На складе)."""
        if "file" not in request.files:
            flash("Выберите файл.", "error")
            return redirect(url_for("import_page"))
        f = request.files["file"]
        if not f or not f.filename:
            flash("Выберите файл.", "error")
            return redirect(url_for("import_page"))
        try:
            data = f.read()
            if data.startswith(b"\xef\xbb\xbf"):
                data = data[3:]
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = data.decode("cp1251")
            except Exception:
                flash("Файл должен быть в кодировке UTF-8 или Windows-1251.", "error")
                return redirect(url_for("import_page"))
        statuses_by_name = {}
        for row in fetch_all("SELECT id, name FROM sim_statuses"):
            r = dict(row)
            statuses_by_name[r["name"].strip().lower()] = r["id"]
        default_status_id = statuses_by_name.get("на складе") or next(iter(statuses_by_name.values()), None)
        if not default_status_id:
            flash("В справочнике нет статуса SIM-карт.", "error")
            return redirect(url_for("import_page"))
        lines = [line.strip() for line in text.replace("\r", "\n").split("\n") if line.strip()]
        if not lines:
            flash("В файле нет строк.", "error")
            return redirect(url_for("import_page"))
        reader = csv.reader(io.StringIO("\n".join(lines)), delimiter=";", quoting=csv.QUOTE_MINIMAL)
        rows = list(reader)
        header = rows[0] if rows else []
        sh0 = (header[0] or "").lower() if len(header) > 0 else ""
        sh1 = (header[1] or "").lower() if len(header) > 1 else ""
        if not header or ("номер" not in sh0 and "iccid" not in sh1 and "номер" not in sh1):
            data_rows = rows
        else:
            data_rows = rows[1:]
        added, skipped, errors = 0, 0, []
        db = get_db()
        for i, row in enumerate(data_rows):
            phone_number = (row[0] or "").strip() if len(row) > 0 else ""
            iccid = (row[1] or "").strip() if len(row) > 1 else ""
            ip_address = ((row[2] or "").strip() or None) if len(row) > 2 else None
            operator = ((row[3] or "").strip() or None) if len(row) > 3 else None
            description = ((row[4] or "").strip() or None) if len(row) > 4 else None
            tariff = ((row[5] or "").strip() or None) if len(row) > 5 else None
            status_name = (row[6] or "").strip() if len(row) > 6 else "на складе"
            if not phone_number or not iccid:
                skipped += 1
                errors.append(f"Строка {i + 1}: не указаны номер или ICCID")
                continue
            status_id = statuses_by_name.get(status_name.lower()) if status_name else default_status_id
            if not status_id:
                status_id = default_status_id
            existing = fetch_one("SELECT id FROM sim_cards WHERE iccid = ?", (iccid,))
            if existing:
                skipped += 1
                errors.append(f"Строка {i + 1} ({iccid}): такой ICCID уже есть")
                continue
            try:
                db.execute(
                    """
                    INSERT INTO sim_cards (phone_number, iccid, ip_address, operator, description, tariff, status_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (phone_number, iccid, ip_address, operator, description, tariff, status_id),
                )
                db.commit()
                added += 1
            except Exception as e:
                db.rollback()
                skipped += 1
                errors.append(f"Строка {i + 1}: {e}")
        if added:
            flash(f"Добавлено SIM-карт: {added}.", "success")
        if skipped:
            flash(f"Пропущено: {skipped}. " + ("Первые ошибки: " + "; ".join(errors[:5]) if errors else ""), "error" if errors else "info")
        return redirect(url_for("import_page"))

    def do_backup_to_dir(backup_dir):
        """Создать резервную копию БД в указанную директорию. Вызывать в app context."""
        if not os.path.exists(DATABASE):
            return False
        backup_dir = (backup_dir or "").strip()
        if not _is_allowed_backup_path(backup_dir):
            return False
        real_backup_dir = os.path.realpath(os.path.abspath(backup_dir))
        lock_fd = _acquire_backup_lock()
        if lock_fd is None:
            return False
        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(suffix=".db")
            os.close(fd)
            snapshot_database(tmp_path)
            filename = f"inventory_backup_{datetime.now().strftime('%Y-%m-%d_%H-%M')}.db"
            dest_path = os.path.join(real_backup_dir, filename)
            shutil.copy2(tmp_path, dest_path)
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
            set_setting("last_backup_at", now_str)
            return True
        except Exception:
            return False
        finally:
            _release_backup_lock(lock_fd)
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    def _scheduled_backup_loop():
        """Фоновый поток: раз в минуту проверяет расписание и создаёт бэкап при совпадении."""
        while True:
            time.sleep(60)
            try:
                with app.app_context():
                    backup_dir = get_setting("backup_dir", "").strip()
                    schedule = get_setting("backup_schedule", "off")
                    if schedule == "off" or not backup_dir:
                        continue
                    backup_time_str = get_setting("backup_time", "03:00").strip()
                    try:
                        th, tm = map(int, backup_time_str.split(":")[:2])
                    except (ValueError, TypeError):
                        continue
                    now = datetime.now()
                    if now.hour != th or now.minute != tm:
                        continue
                    last = get_setting("last_backup_at", "")[:10]
                    today_str = now.strftime("%Y-%m-%d")
                    if schedule == "daily":
                        if last == today_str:
                            continue
                    elif schedule == "weekly":
                        weekday = int(get_setting("backup_weekday", "0"))
                        if now.weekday() != weekday:
                            continue
                        if last:
                            try:
                                last_d = datetime.strptime(last, "%Y-%m-%d").date()
                                if (now.date() - last_d).days < 7:
                                    continue
                            except ValueError:
                                pass
                    do_backup_to_dir(backup_dir)
            except Exception:
                pass

    _backup_thread = threading.Thread(target=_scheduled_backup_loop, daemon=True)
    _backup_thread.start()

    @app.route("/backup")
    @login_required
    def backup_db():
        """Скачать резервную копию базы данных (консистентный снимок через SQLite backup). Только admin."""
        r = _require_admin()
        if r is not None:
            return r
        if not os.path.exists(DATABASE):
            flash("База данных не найдена.", "error")
            return redirect(url_for("index"))
        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(suffix=".db")
            os.close(fd)
            snapshot_database(tmp_path)
            with open(tmp_path, "rb") as f:
                data = f.read()
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
            set_setting("last_backup_at", now_str)
            filename = f"inventory_backup_{datetime.now().strftime('%Y-%m-%d_%H-%M')}.db"
            return Response(
                data,
                mimetype="application/octet-stream",
                headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            )
        except Exception as e:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            flash(f"Ошибка создания резервной копии: {e}", "error")
            return redirect(url_for("index"))

    # Миграции при старте приложения (в т.ч. при запуске через gunicorn/uwsgi)
    with app.app_context():
        if os.path.exists(app.config["DATABASE"]):
            try:
                run_migrations()
            except Exception as e:
                import sys
                print(f"Предупреждение: миграции при старте не выполнены: {e}", file=sys.stderr)

    return app


app = create_app()


if __name__ == "__main__":
    # При первом запуске создаём базу из schema.sql и сразу применяем миграции.
    if not os.path.exists(DATABASE):
        print("Создаётся база данных из schema.sql ...")
        init_database()
        print("База данных создана.")
    run_migrations()

    debug = os.environ.get("FLASK_DEBUG", "0").lower() in ("1", "true", "yes")
    app.run(debug=debug)
