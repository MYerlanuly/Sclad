"""Пользователи, вход и проверка прав."""
import sqlite3
import time

from flask import flash, redirect, render_template, request, url_for
from flask_login import LoginManager, UserMixin, current_user, login_user, logout_user
from werkzeug.security import check_password_hash

from .db import get_db
from .utils import _is_safe_next_url


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


def require_admin():
    """Для обработчиков только для admin: вернёт redirect, если текущий пользователь не администратор."""
    if not current_user.is_authenticated or not getattr(current_user, "is_admin", False):
        flash("Доступ только для администратора.", "error")
        return redirect(url_for("index"))


def register(app):
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
                    # Журнал смены
                    "journal_entry_add",
                    "journal_entry_edit",
                    "journal_entry_delete",
                    "journal_engineer_add",
                    "journal_engineer_remove",
                }
                if request.endpoint not in allowed_endpoints:
                    flash("Этому пользователю разрешено только записывать ТО, работать с заданиями на смену и журналом смены.", "error")
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
