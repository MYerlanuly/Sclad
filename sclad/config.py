"""Пути и константы приложения."""
import os
import tempfile

# Корень проекта (папка с app.py, schema.sql, templates/, static/)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Путь к базе можно переопределить переменной окружения SCLAD_DATABASE (используется в тестах).
DATABASE = os.environ.get("SCLAD_DATABASE") or os.path.join(BASE_DIR, "inventory.db")
SCHEMA_PATH = os.path.join(BASE_DIR, "schema.sql")
# Директория, внутри которой разрешено сохранять резервные копии (и её поддиректории)
BACKUP_ROOT = os.path.join(BASE_DIR, "backups")
BACKUP_LOCK_PATH = os.path.join(tempfile.gettempdir(), "sclad_backup.lock")
LOCK_STALE_SEC = 300
