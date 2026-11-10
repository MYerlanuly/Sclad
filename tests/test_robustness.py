"""Некорректный ввод в любой форме не роняет сервер (нет ошибок 500)."""
import pytest

from conftest import db_query, db_scalar
from test_security import post_urls

GARBAGE = ["abc", "-1", "0", "99999999", "", " ", "1.5", "2026-13-45", "x" * 5000, "Ж'\"<>;--", "None", "null"]

FIELDS = [
    "name", "serial_number", "status_id", "description", "equipment_kind", "inventory_number", "machine_model_id",
    "machine_type_id", "code", "position_group", "label", "sort_order", "phone_number", "iccid", "ip_address",
    "operator", "tariff", "username", "password", "new_password", "role", "surname", "first_name", "position",
    "personnel_number", "title", "for_shift", "effective_date", "task_type_id", "machine_id", "equipment_id",
    "sim_card_id", "sensor_position_id", "performed_at", "engineers", "remarks", "reason", "comment",
    "maintenance_interval_months", "backup_dir", "backup_schedule", "backup_time", "backup_weekday",
    "entry_time", "content", "note", "date", "shift", "mail_to", "mail_cc",
]


@pytest.mark.parametrize("value", GARBAGE)
def test_garbage_in_every_form_field(app, seed, admin, value):
    app.config["PROPAGATE_EXCEPTIONS"] = False
    failures = []
    for endpoint, url in post_urls(app, seed):
        if endpoint in ("login",):
            continue
        data = {f: value for f in FIELDS}
        data["action"] = "add"
        resp = admin.post(url, data=data)
        if resp.status_code >= 500:
            failures.append(f"{url} -> {resp.status_code}")
        assert db_scalar("SELECT role FROM users WHERE id = 1") == "admin"
    assert not failures, f"значение {value[:20]!r}: " + ", ".join(failures)
    assert_references_valid()


def assert_references_valid():
    """Все ссылки между таблицами указывают на существующие записи, id хранятся числами."""
    broken = db_query("PRAGMA foreign_key_check")
    assert not broken, "битые ссылки: " + ", ".join(f"{r[0]}.rowid={r[1]} -> {r[2]}" for r in broken)
    checks = {
        "equipment": ["status_id"], "sim_cards": ["status_id"], "machines": ["machine_model_id"],
        "machine_models": ["machine_type_id"], "machine_maintenance": ["machine_id"],
        "machine_equipment": ["machine_id", "equipment_id", "sensor_position_id"],
        "machine_sim_cards": ["machine_id", "sim_card_id"],
        "shift_tasks": ["task_type_id", "machine_id", "equipment_id"],
    }
    for table, cols in checks.items():
        for col in cols:
            bad = db_query(f"SELECT id, {col} FROM {table} WHERE {col} IS NOT NULL AND typeof({col}) != 'integer'")
            assert not bad, f"{table}.{col} содержит не число: {[tuple(r) for r in bad][:3]}"
    for table, col in [("machine_maintenance", "performed_at"), ("shift_tasks", "effective_date")]:
        bad = db_query(f"SELECT id, {col} FROM {table} WHERE date({col}) IS NULL")
        assert not bad, f"{table}.{col} содержит не дату: {[tuple(r) for r in bad][:3]}"


@pytest.mark.parametrize("value", ["abc", "-5", "99999999999999999999", "1e9"])
def test_garbage_in_query_params(app, admin, seed, value):
    app.config["PROPAGATE_EXCEPTIONS"] = False
    failures = []
    for url in ["/equipment", "/machines", "/sim_cards", "/operations", "/maintenance", "/maintenance/calendar",
                "/reports", "/shift_tasks", "/reports/export", "/operations/export", "/maintenance/act-draft",
                f"/machines/{seed['machine']}/install_equipment", f"/machines/{seed['machine']}/install_sim", "/journal", "/journal/search",
                "/journal/export.docx", "/journal/mail.eml"]:
        for param in ("page", "per_page", "status", "year", "month", "date", "machine_id", "shift", "from", "to",
                      "date_from", "date_to", "type", "sort", "order", "q"):
            resp = admin.get(url, query_string={param: value})
            if resp.status_code >= 500:
                failures.append(f"{url}?{param}={value}")
    assert not failures, ", ".join(failures)


def test_missing_ids_in_url_do_not_crash(app, admin):
    app.config["PROPAGATE_EXCEPTIONS"] = False
    for url in ["/equipment/999", "/machines/999", "/sim_cards/999", "/maintenance/999/act", "/equipment/999/edit",
                "/machines/999/edit", "/sim_cards/999/edit", "/users/999/edit", "/shift_tasks/999/edit",
                "/machines/999/install_equipment", "/machines/999/install_sim", "/machine_types/999/edit",
                "/machine_models/999/edit", "/sensor_positions/999/edit", "/shift_task_types/999/edit",
                "/shift_tasks/999/complete", "/shift_tasks/999/not_done", "/maintenance/add/999", "/journal/entries/999/edit"]:
        resp = admin.get(url)
        assert resp.status_code < 500, f"{url} -> {resp.status_code}"


def test_upload_garbage_file_to_import(app, admin):
    app.config["PROPAGATE_EXCEPTIONS"] = False
    import io

    for kind in ("machines", "equipment", "sim_cards"):
        for content in (b"", b"not an excel file", b"PK\x03\x04garbage"):
            resp = admin.post(f"/import/{kind}", data={"file": (io.BytesIO(content), "x.xlsx")},
                              content_type="multipart/form-data")
            assert resp.status_code < 500, f"/import/{kind} -> {resp.status_code}"
        resp = admin.post(f"/import/{kind}", data={}, content_type="multipart/form-data")
        assert resp.status_code < 500
