"""Маршруты: settings."""

from flask import flash, redirect, render_template, request, url_for
from flask_login import login_required

from ..auth import require_admin
from ..backup import _is_allowed_backup_path, do_backup_to_dir
from ..config import BACKUP_ROOT
from ..db import get_setting, set_setting


def register(app):
    @app.route("/settings", methods=["GET", "POST"])
    @login_required
    def settings():
        r = require_admin()
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
