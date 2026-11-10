"""Склад АСДУ: учёт оборудования, техники и SIM-карт.

create_app() собирает приложение из модулей:
  config      — пути и константы
  db          — соединение с базой и общие операции
  migrations  — схема и миграции
  auth        — пользователи, вход, права ролей
  views/      — разделы сайта (оборудование, техника, ТО, задания, отчёты ...)
"""
import os
import sys

from flask import Flask
from flask_wtf.csrf import CSRFProtect

# config первым: он читает .env (и файл настроек склада), остальные модули берут пути из него
from . import config  # noqa: E402
from . import auth, cli, context, views  # noqa: E402
from .config import BASE_DIR, DATABASE  # noqa: E402
from .db import close_db  # noqa: E402
from .migrations import run_migrations  # noqa: E402
from .utils import _format_date_ddmmyy, _format_datetime_ddmmyy_hhmm  # noqa: E402


def create_app():
    # root_path — корень проекта, чтобы templates/ и static/ остались на своих местах
    app = Flask("app", root_path=BASE_DIR)
    app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY") or os.environ.get(
        "SECRET_KEY"
    ) or "dev-change-in-production"
    if app.config["SECRET_KEY"] == "dev-change-in-production" or len(app.config["SECRET_KEY"]) < 32:
        print(
            "ВНИМАНИЕ: FLASK_SECRET_KEY не задан или короче 32 символов. "
            "Задайте длинный случайный ключ в .env, например: "
            "python -c \"import secrets; print(secrets.token_hex(32))\""
        )
    if config.INSTANCE:
        # Ключ подписи свой у каждого склада, даже если FLASK_SECRET_KEY общий:
        # cookie одного склада не подходит к другому (там у пользователя с тем же id другие права).
        app.config["SECRET_KEY"] = f"{app.config['SECRET_KEY']}:{config.INSTANCE}"
    app.config["SESSION_COOKIE_NAME"] = config.SESSION_COOKIE_NAME
    app.config["REMEMBER_COOKIE_NAME"] = config.REMEMBER_COOKIE_NAME
    app.config["SCLAD_NAME"] = config.NAME
    app.config["SCLAD_INSTANCE"] = config.INSTANCE
    app.config["DATABASE"] = DATABASE
    CSRFProtect(app)

    app.jinja_env.filters["datefmt"] = _format_date_ddmmyy
    app.jinja_env.filters["datetimefmt"] = _format_datetime_ddmmyy_hhmm
    app.teardown_appcontext(close_db)

    auth.register(app)
    cli.register(app)
    context.register(app)
    views.register_all(app)

    # Миграции при старте приложения (в т.ч. при запуске через gunicorn)
    with app.app_context():
        if os.path.exists(app.config["DATABASE"]):
            try:
                run_migrations()
            except Exception as e:
                print(f"Предупреждение: миграции при старте не выполнены: {e}", file=sys.stderr)

    return app
