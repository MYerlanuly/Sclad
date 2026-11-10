"""Пути и константы приложения.

Один запущенный процесс обслуживает один склад. Чтобы рядом работал склад другого
отдела со своей базой, его запускают с отдельным файлом настроек:

    SCLAD_ENV_FILE=.env.otdel2   (в нём SCLAD_INSTANCE=otdel2, SCLAD_NAME=..., SCLAD_PORT=...)

Из SCLAD_INSTANCE выводятся имя базы, папка резервных копий и имена cookie,
поэтому склады не пересекаются ни данными, ни копиями, ни входом.
"""
import hashlib
import os
import re
import tempfile

from dotenv import load_dotenv

# Корень проекта (папка с app.py, schema.sql, templates/, static/)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _resolve(path):
    """Относительный путь считаем от корня проекта, а не от текущей папки."""
    return path if os.path.isabs(path) else os.path.join(BASE_DIR, path)


# Сначала файл настроек склада (если указан), затем общий .env. Уже заданные
# переменные окружения не перезаписываются, поэтому настройки склада важнее общих.
ENV_FILE = (os.environ.get("SCLAD_ENV_FILE") or "").strip()
if ENV_FILE:
    ENV_FILE = _resolve(ENV_FILE)
    if not os.path.isfile(ENV_FILE):
        raise SystemExit(f"Файл настроек склада не найден: {ENV_FILE}")
    load_dotenv(ENV_FILE, override=False)
load_dotenv(os.path.join(BASE_DIR, ".env"), override=False)

# Короткий латинский код склада. Пусто — основной склад (inventory.db, как раньше).
INSTANCE = (os.environ.get("SCLAD_INSTANCE") or "").strip().lower()
if INSTANCE and not re.fullmatch(r"[a-z0-9_-]{1,32}", INSTANCE):
    raise SystemExit(
        "SCLAD_INSTANCE: только латинские буквы, цифры, '-' и '_', до 32 символов "
        f"(сейчас: {INSTANCE!r})"
    )

# Название склада в шапке, на странице входа и в заголовке вкладки
NAME = (os.environ.get("SCLAD_NAME") or "").strip() or "Склад АСДУ"

# Путь к базе: SCLAD_DATABASE, иначе <код склада>.db, иначе inventory.db
DATABASE = _resolve(
    (os.environ.get("SCLAD_DATABASE") or "").strip()
    or (f"{INSTANCE}.db" if INSTANCE else "inventory.db")
)
SCHEMA_PATH = os.path.join(BASE_DIR, "schema.sql")

# Директория, внутри которой разрешено сохранять резервные копии (и её поддиректории).
# У каждого склада своя: backups/ у основного, backups/<код склада>/ у остальных.
BACKUP_ROOT = _resolve(
    (os.environ.get("SCLAD_BACKUP_DIR") or "").strip()
    or (os.path.join("backups", INSTANCE) if INSTANCE else "backups")
)
# Блокировка копирования своя для каждой базы, чтобы склады не ждали друг друга
_db_key = hashlib.sha1(os.path.realpath(DATABASE).encode("utf-8")).hexdigest()[:12]
BACKUP_LOCK_PATH = os.path.join(tempfile.gettempdir(), f"sclad_backup_{_db_key}.lock")
LOCK_STALE_SEC = 300
# Имя файла скачиваемой копии: inventory_backup_... или <код склада>_backup_...
BACKUP_FILE_PREFIX = INSTANCE or "inventory"

# Имена cookie: у основного склада стандартные (чтобы не разлогинить текущих
# пользователей), у остальных свои — иначе вход в один склад выкидывал бы из другого.
SESSION_COOKIE_NAME = f"sclad_{INSTANCE}_session" if INSTANCE else "session"
REMEMBER_COOKIE_NAME = f"sclad_{INSTANCE}_remember" if INSTANCE else "remember_token"

# Порт для запуска через python app.py
try:
    PORT = int(os.environ.get("SCLAD_PORT") or 5000)
except ValueError:
    raise SystemExit("SCLAD_PORT должен быть числом, например 5001")
