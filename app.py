"""Точка входа: `python app.py` (разработка) или `gunicorn app:app` (продакшен).

Код приложения находится в пакете sclad/.

Склад другого отдела запускается с его файлом настроек:
    python app.py --env .env.otdel2
(то же самое: переменная окружения SCLAD_ENV_FILE=.env.otdel2)
"""
import os
import sys

if __name__ == "__main__" and "--env" in sys.argv:
    _i = sys.argv.index("--env")
    if _i + 1 >= len(sys.argv):
        raise SystemExit("После --env укажите файл настроек склада, например: python app.py --env .env.otdel2")
    os.environ["SCLAD_ENV_FILE"] = sys.argv[_i + 1]

from sclad import create_app  # noqa: E402
from sclad import config  # noqa: E402
from sclad.migrations import init_database, run_migrations  # noqa: E402

app = create_app()
DATABASE = config.DATABASE  # для тестов и скриптов


if __name__ == "__main__":
    # При первом запуске создаём базу из schema.sql и сразу применяем миграции.
    if not os.path.exists(config.DATABASE):
        print(f"Создаётся база данных {config.DATABASE} из schema.sql ...")
        init_database()
        print("База данных создана.")
    run_migrations()

    print(f"{config.NAME}: база {config.DATABASE}, копии в {config.BACKUP_ROOT}")
    print(f"Откройте http://127.0.0.1:{config.PORT}")
    debug = os.environ.get("FLASK_DEBUG", "0").lower() in ("1", "true", "yes")
    app.run(debug=debug, port=config.PORT)
