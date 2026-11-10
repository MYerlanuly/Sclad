"""Точка входа: `python app.py` (разработка) или `gunicorn app:app` (продакшен).

Код приложения находится в пакете sclad/.
"""
import os

from sclad import create_app
from sclad.config import DATABASE
from sclad.migrations import init_database, run_migrations

app = create_app()


if __name__ == "__main__":
    # При первом запуске создаём базу из schema.sql и сразу применяем миграции.
    if not os.path.exists(DATABASE):
        print("Создаётся база данных из schema.sql ...")
        init_database()
        print("База данных создана.")
    run_migrations()

    debug = os.environ.get("FLASK_DEBUG", "0").lower() in ("1", "true", "yes")
    app.run(debug=debug)
