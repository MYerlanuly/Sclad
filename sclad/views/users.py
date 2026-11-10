"""Маршруты: users."""

from flask import flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from werkzeug.security import generate_password_hash

from ..auth import require_admin
from ..db import fetch_all, fetch_one, get_db


def register(app):
    @app.route("/users")
    @login_required
    def users_list():
        r = require_admin()
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
        r = require_admin()
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
        r = require_admin()
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
