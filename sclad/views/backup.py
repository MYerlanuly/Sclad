"""Маршруты: backup."""
import os
import tempfile
import threading
import time
from datetime import datetime

from flask import Response, flash, redirect, url_for
from flask_login import login_required

from ..auth import require_admin
from ..backup import do_backup_to_dir, snapshot_database
from ..config import BACKUP_FILE_PREFIX, DATABASE
from ..db import get_setting, set_setting


def register(app):
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

    # Поток расписания копий можно отключить (SCLAD_BACKUP_SCHEDULER=0) — так делают тесты,
    # иначе каждый create_app() в тестах запускал бы свой поток, держащий базу открытой.
    if os.environ.get("SCLAD_BACKUP_SCHEDULER", "1") != "0":
        _backup_thread = threading.Thread(target=_scheduled_backup_loop, daemon=True)
        _backup_thread.start()

    @app.route("/backup")
    @login_required
    def backup_db():
        """Скачать резервную копию базы данных (консистентный снимок через SQLite backup). Только admin."""
        r = require_admin()
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
            filename = f"{BACKUP_FILE_PREFIX}_backup_{datetime.now().strftime('%Y-%m-%d_%H-%M')}.db"
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
