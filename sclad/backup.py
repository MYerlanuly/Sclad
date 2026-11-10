"""Резервные копии базы: снимок, проверка пути, межпроцессная блокировка."""
import os
import shutil
import sqlite3
import tempfile
import time
from datetime import datetime

from .config import BACKUP_LOCK_PATH, BACKUP_ROOT, DATABASE, LOCK_STALE_SEC
from .db import set_setting


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
