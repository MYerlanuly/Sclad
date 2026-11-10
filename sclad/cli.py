"""Команды flask: init-db, create-user."""
import sqlite3

import click
from werkzeug.security import generate_password_hash

from .migrations import init_database, migrate_users, migrate_users_profile_and_roles, run_migrations


def register(app):
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
