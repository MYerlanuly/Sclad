"""Миграции схемы SQLite. Каждая безопасна при повторном запуске."""
import os
import sqlite3

from .config import DATABASE, SCHEMA_PATH
from .utils import fix_mojibake_text


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


def migrate_shift_journal():
    """Журнал смены (перенесён из отдельной программы «Журнал инженеров по АСУ»)."""
    if not os.path.exists(DATABASE):
        return False
    conn = sqlite3.connect(DATABASE)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS journal_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                shift_date TEXT NOT NULL,
                shift TEXT NOT NULL CHECK (shift IN ('day', 'night')),
                entry_time TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',
                created_by_user_id INTEGER,
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                updated_at TEXT,
                FOREIGN KEY (created_by_user_id) REFERENCES users(id)
            );
            CREATE INDEX IF NOT EXISTS idx_journal_entries_shift ON journal_entries (shift_date, shift);
            CREATE TABLE IF NOT EXISTS journal_shift_engineers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                shift_date TEXT NOT NULL,
                shift TEXT NOT NULL CHECK (shift IN ('day', 'night')),
                name TEXT NOT NULL,
                user_id INTEGER,
                UNIQUE (shift_date, shift, name),
                FOREIGN KEY (user_id) REFERENCES users(id)
            );
            """
        )
        # Адресаты письма по умолчанию — те же, что были в программе журнала
        conn.execute(
            "INSERT OR IGNORE INTO app_settings (key, value) VALUES ('journal_mail_to', 'v.ustimenko@bogatyr.kz')"
        )
        conn.execute(
            "INSERT OR IGNORE INTO app_settings (key, value) VALUES ('journal_mail_cc', 'asutp01@bogatyr.kz')"
        )
        conn.commit()
        return True
    finally:
        conn.close()


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
    migrate_shift_journal,
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
