r"""Рабочий запуск склада на Windows-сервере (waitress), без nginx и gunicorn.

    venv\Scripts\python serve.py                    основной склад
    venv\Scripts\python serve.py --env .env.otdel2   склад другого отдела

Слушает все сетевые адреса (SCLAD_HOST, по умолчанию 0.0.0.0) на порту SCLAD_PORT
(по умолчанию 5000), так что сайт открывается с других компьютеров: http://имя-сервера:5000.
Журнал пишется в logs/<код склада или sclad>.log.

Как службу (автозапуск при включении сервера) его ставит scripts/windows/install-service.ps1.
На Linux вместо этого используется gunicorn, см. docs/DEPLOY_SERVER.md.
"""
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

if "--env" in sys.argv:
    _i = sys.argv.index("--env")
    if _i + 1 >= len(sys.argv):
        raise SystemExit("После --env укажите файл настроек склада, например: serve.py --env .env.otdel2")
    os.environ["SCLAD_ENV_FILE"] = sys.argv[_i + 1]

from sclad import config  # noqa: E402

os.makedirs(os.path.join(ROOT, "logs"), exist_ok=True)
_log_path = os.path.join(ROOT, "logs", f"{config.INSTANCE or 'sclad'}.log")
_handler = RotatingFileHandler(_log_path, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[_handler, logging.StreamHandler()])

from waitress import serve  # noqa: E402

from app import app  # noqa: E402

if __name__ == "__main__":
    host = (os.environ.get("SCLAD_HOST") or "0.0.0.0").strip()
    logging.getLogger("sclad").info("%s: база %s, адрес http://%s:%s", config.NAME, config.DATABASE, host, config.PORT)
    serve(app, host=host, port=config.PORT, threads=8, ident=None)
