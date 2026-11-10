"""Общие фикстуры тестов.

Каждый тест получает свою чистую базу во временной папке: схема из schema.sql
плюс все миграции, как при реальном запуске. Рабочая inventory.db не трогается.
"""
import os
import sqlite3
import sys
import tempfile

import pytest

_TMP_DIR = tempfile.mkdtemp(prefix="sclad-tests-")
os.environ["SCLAD_DATABASE"] = os.path.join(_TMP_DIR, "test.db")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret-key-" + "x" * 32)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

PASSWORD = "Test-pass-123"
ROLES = ("admin", "editor", "maintenance", "reader")


def _reset_database():
    for suffix in ("", "-wal", "-shm"):
        path = app_module.DATABASE + suffix
        if os.path.exists(path):
            os.remove(path)
    app_module.init_database()
    app_module.run_migrations()


def db_connect():
    conn = sqlite3.connect(app_module.DATABASE, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def db_query(sql, params=()):
    conn = db_connect()
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def db_scalar(sql, params=()):
    rows = db_query(sql, params)
    return rows[0][0] if rows else None


def db_exec(sql, params=()):
    conn = db_connect()
    try:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def make_app(csrf=False):
    flask_app = app_module.create_app()
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=csrf)
    return flask_app


@pytest.fixture
def app():
    _reset_database()
    for role in ROLES:
        db_exec(
            "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
            (role, generate_password_hash(PASSWORD), role),
        )
    return make_app()


def login(client, username, password=PASSWORD):
    return client.post("/login", data={"username": username, "password": password})


@pytest.fixture
def client_for(app):
    """client_for("editor") — тестовый клиент, уже вошедший под указанной ролью."""

    def _make(role):
        c = app.test_client()
        resp = login(c, role)
        assert resp.status_code == 302, f"не удалось войти как {role}"
        return c

    return _make


@pytest.fixture
def admin(client_for):
    return client_for("admin")


@pytest.fixture
def seed(app):
    """Минимальный набор данных: тип, модель, 2 машины, оборудование, датчик, 2 SIM."""
    ids = {}
    ids["type"] = db_exec("INSERT INTO machine_types (name) VALUES ('Самосвал')")
    ids["model"] = db_exec(
        "INSERT INTO machine_models (name, machine_type_id) VALUES ('БелАЗ-75131', ?)", (ids["type"],)
    )
    ids["machine"] = db_exec(
        "INSERT INTO machines (inventory_number, machine_model_id) VALUES ('INV-001', ?)", (ids["model"],)
    )
    ids["machine2"] = db_exec(
        "INSERT INTO machines (inventory_number, machine_model_id) VALUES ('INV-002', ?)", (ids["model"],)
    )
    ids["equipment"] = db_exec(
        "INSERT INTO equipment (name, serial_number, status_id, equipment_kind) VALUES ('Терминал', 'SN-1', 1, 'other')"
    )
    ids["sensor"] = db_exec(
        "INSERT INTO equipment (name, serial_number, status_id, equipment_kind) VALUES ('Датчик давления', 'DS-1', 1, 'sensor')"
    )
    ids["sensor2"] = db_exec(
        "INSERT INTO equipment (name, serial_number, status_id, equipment_kind) VALUES ('Датчик давления', 'DS-2', 1, 'sensor')"
    )
    ids["sim"] = db_exec(
        "INSERT INTO sim_cards (phone_number, iccid, status_id) VALUES ('+77001112233', '8970000000000000001', 1)"
    )
    ids["sim2"] = db_exec(
        "INSERT INTO sim_cards (phone_number, iccid, status_id) VALUES ('+77001112244', '8970000000000000002', 1)"
    )
    ids["task_type"] = db_scalar("SELECT id FROM shift_task_types ORDER BY id LIMIT 1")
    if ids["task_type"] is None:
        ids["task_type"] = db_exec("INSERT INTO shift_task_types (name, sort_order) VALUES ('Прочее', 1)")
    return ids
