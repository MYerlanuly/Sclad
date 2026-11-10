"""Несколько складов (отделов) на одном коде: у каждого своя база, копии и вход.

Настройки читаются при импорте, поэтому каждый склад запускается в отдельном
процессе Python — так же, как в реальной работе.
"""
import json
import os
import subprocess
import sys
import textwrap

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SECRET = "shared-secret-for-both-warehouses-" + "z" * 16

# Переменные, которые могут прийти из локального .env разработчика. Пустое значение
# в окружении load_dotenv не перезаписывает, поэтому тест видит значения по умолчанию.
_CLEAN = ("SCLAD_ENV_FILE", "SCLAD_INSTANCE", "SCLAD_NAME", "SCLAD_DATABASE",
          "SCLAD_BACKUP_DIR", "SCLAD_PORT")


def run_py(code, **env):
    full_env = dict(os.environ)
    for key in _CLEAN:
        full_env[key] = ""
    full_env.update(FLASK_SECRET_KEY=SECRET, SCLAD_BACKUP_SCHEDULER="0", PYTHONIOENCODING="utf-8")
    full_env.update(env)
    proc = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        cwd=ROOT, env=full_env, capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    return proc


def run_json(code, **env):
    proc = run_py(code, **env)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


CONFIG_DUMP = """
    import json
    from sclad import config as c
    print(json.dumps({k: getattr(c, k) for k in (
        "INSTANCE", "NAME", "DATABASE", "BACKUP_ROOT", "BACKUP_LOCK_PATH",
        "SESSION_COOKIE_NAME", "REMEMBER_COOKIE_NAME", "PORT", "BACKUP_FILE_PREFIX")}))
"""


def test_main_warehouse_keeps_old_defaults():
    cfg = run_json(CONFIG_DUMP)
    assert cfg["INSTANCE"] == ""
    assert cfg["DATABASE"] == os.path.join(ROOT, "inventory.db")
    assert cfg["BACKUP_ROOT"] == os.path.join(ROOT, "backups")
    assert cfg["SESSION_COOKIE_NAME"] == "session"
    assert cfg["PORT"] == 5000
    assert cfg["BACKUP_FILE_PREFIX"] == "inventory"


def test_second_warehouse_gets_own_paths_and_cookies():
    main = run_json(CONFIG_DUMP)
    second = run_json(CONFIG_DUMP, SCLAD_INSTANCE="otdel2")
    assert second["DATABASE"] == os.path.join(ROOT, "otdel2.db")
    assert second["BACKUP_ROOT"] == os.path.join(ROOT, "backups", "otdel2")
    assert second["BACKUP_FILE_PREFIX"] == "otdel2"
    for key in ("DATABASE", "BACKUP_ROOT", "BACKUP_LOCK_PATH", "SESSION_COOKIE_NAME", "REMEMBER_COOKIE_NAME"):
        assert second[key] != main[key], key


def test_env_file_sets_up_warehouse(tmp_path):
    env_file = tmp_path / "otdel3.env"
    env_file.write_text(
        "SCLAD_INSTANCE=otdel3\nSCLAD_NAME=Склад отдела 3\nSCLAD_PORT=5003\n", encoding="utf-8"
    )
    # пустые значения убираем, иначе они перекрыли бы файл настроек
    env = {k: v for k, v in os.environ.items() if k not in _CLEAN}
    proc = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(CONFIG_DUMP)], cwd=ROOT, capture_output=True,
        text=True, encoding="utf-8", timeout=120,
        env={**env, "SCLAD_ENV_FILE": str(env_file), "PYTHONIOENCODING": "utf-8"},
    )
    assert proc.returncode == 0, proc.stderr
    cfg = json.loads(proc.stdout.strip().splitlines()[-1])
    assert cfg["INSTANCE"] == "otdel3"
    assert cfg["NAME"] == "Склад отдела 3"
    assert cfg["PORT"] == 5003
    assert cfg["DATABASE"] == os.path.join(ROOT, "otdel3.db")


@pytest.mark.parametrize("bad", ["../etc", "отдел", "a b", "x" * 40])
def test_invalid_instance_code_is_rejected(bad):
    proc = run_py("import sclad.config", SCLAD_INSTANCE=bad)
    assert proc.returncode != 0
    assert "SCLAD_INSTANCE" in proc.stderr


def test_missing_env_file_is_reported():
    proc = run_py("import sclad.config", SCLAD_ENV_FILE="no-such-file.env")
    assert proc.returncode != 0
    assert "не найден" in proc.stderr


# Поднимает склад на своей базе: пользователь с id=1 и заданной ролью, вход,
# главная страница. Если передан чужой cookie — проверяет, пускает ли он внутрь.
WAREHOUSE = """
    import json, os, sqlite3
    from werkzeug.security import generate_password_hash
    import app as m
    m.init_database(); m.run_migrations()
    conn = sqlite3.connect(m.DATABASE)
    conn.execute("INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
                 (os.environ["T_USER"], generate_password_hash("Pass-12345"), os.environ["T_ROLE"]))
    conn.commit(); conn.close()
    a = m.create_app(); a.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    cookie_name = a.config["SESSION_COOKIE_NAME"]
    out = {"cookie_name": cookie_name}
    foreign = os.environ.get("T_FOREIGN_COOKIE")
    if foreign:
        c = a.test_client()
        c.set_cookie(cookie_name, foreign)
        out["foreign_users_status"] = c.get("/users").status_code
    c = a.test_client()
    r = c.post("/login", data={"username": os.environ["T_USER"], "password": "Pass-12345"})
    out["login_status"] = r.status_code
    out["cookie"] = c.get_cookie(cookie_name).value
    out["has_name"] = os.environ.get("SCLAD_NAME", "") in c.get("/").get_data(as_text=True)
    print(json.dumps(out))
"""


def test_warehouses_have_separate_data_and_logins(tmp_path):
    second = run_json(
        WAREHOUSE, SCLAD_INSTANCE="otdel2", SCLAD_NAME="Склад отдела 2",
        SCLAD_DATABASE=str(tmp_path / "otdel2.db"), T_USER="ivan", T_ROLE="reader",
    )
    assert second["login_status"] == 302
    assert second["has_name"]
    assert second["cookie_name"] != "session"

    # Тот же FLASK_SECRET_KEY, у основного склада пользователь id=1 — администратор.
    # Cookie читателя со склада 2 не должен открыть ему основной склад.
    main = run_json(
        WAREHOUSE, SCLAD_DATABASE=str(tmp_path / "main.db"), SCLAD_NAME="Основной склад",
        T_USER="boss", T_ROLE="admin", T_FOREIGN_COOKIE=second["cookie"],
    )
    assert main["foreign_users_status"] == 302  # отправлен на вход
    assert main["login_status"] == 302
    assert main["has_name"]

    # Базы физически разные: в каждой только свой пользователь
    import sqlite3
    for db, user in (("otdel2.db", "ivan"), ("main.db", "boss")):
        conn = sqlite3.connect(tmp_path / db)
        try:
            assert [r[0] for r in conn.execute("SELECT username FROM users")] == [user]
        finally:
            conn.close()
