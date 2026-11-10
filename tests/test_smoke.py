"""Каждая страница открывается под каждой ролью без ошибки 500."""
import pytest

from conftest import ROLES, db_exec


def _full_seed(seed):
    ids = dict(seed)
    ids["maintenance"] = db_exec(
        "INSERT INTO machine_maintenance (machine_id, performed_at, next_due_at, performed_by) VALUES (?, '2026-01-10', '2026-04-10', 'Иванов')",
        (seed["machine"],),
    )
    ids["link_eq"] = db_exec(
        "INSERT INTO machine_equipment (machine_id, equipment_id) VALUES (?, ?)", (seed["machine"], seed["equipment"])
    )
    db_exec("UPDATE equipment SET status_id = 2 WHERE id = ?", (seed["equipment"],))
    ids["link_sim"] = db_exec(
        "INSERT INTO machine_sim_cards (machine_id, sim_card_id) VALUES (?, ?)", (seed["machine"], seed["sim"])
    )
    db_exec("UPDATE sim_cards SET status_id = 2 WHERE id = ?", (seed["sim"],))
    admin_id = 1
    ids["task"] = db_exec(
        "INSERT INTO shift_tasks (created_by_user_id, for_shift, effective_date, title, task_type_id, machine_id) VALUES (?, 'day', date('now'), 'Проверить терминал', ?, ?)",
        (admin_id, seed["task_type"], seed["machine"]),
    )
    db_exec(
        "INSERT INTO operations_log (operation_type, entity_type, entity_id, description, username) VALUES ('install', 'equipment', ?, 'тест', 'admin')",
        (seed["equipment"],),
    )
    return ids


ARG_SOURCES = {
    "user_id": lambda ids: 1,
    "equipment_id": lambda ids: ids["equipment"],
    "position_id": lambda ids: 1,
    "type_id": lambda ids: ids["type"],
    "model_id": lambda ids: ids["model"],
    "machine_id": lambda ids: ids["machine"],
    "maintenance_id": lambda ids: ids["maintenance"],
    "task_id": lambda ids: ids["task"],
    "sim_id": lambda ids: ids["sim"],
    "link_id": lambda ids: ids["link_eq"],
}

# Маршруты, у которых id в URL относится к другой таблице
SPECIAL_ARGS = {
    "shift_task_type_edit": {"type_id": "task_type"},
}


def get_urls(app, ids):
    urls = []
    for rule in app.url_map.iter_rules():
        if "GET" not in rule.methods or rule.endpoint in ("static", "logout"):
            continue
        args = {}
        for a in rule.arguments:
            special = SPECIAL_ARGS.get(rule.endpoint, {})
            args[a] = ids[special[a]] if a in special else ARG_SOURCES[a](ids)
        with app.test_request_context():
            from flask import url_for

            urls.append((rule.endpoint, url_for(rule.endpoint, **args)))
    return sorted(set(urls))


@pytest.mark.parametrize("role", ROLES)
def test_all_pages_open_without_500(app, seed, client_for, role):
    ids = _full_seed(seed)
    client = client_for(role)
    failures = []
    for endpoint, url in get_urls(app, ids):
        resp = client.get(url)
        if resp.status_code >= 500:
            failures.append(f"{url} -> {resp.status_code}")
    assert not failures, "Ошибки 500: " + ", ".join(failures)


def test_admin_list_pages_return_200(admin, seed):
    _full_seed(seed)
    for url in [
        "/", "/equipment", "/machines", "/machine_types", "/machine_models", "/sensor_positions",
        "/sim_cards", "/maintenance", "/maintenance/calendar", "/shift_tasks", "/shift_task_types",
        "/operations", "/reports", "/import", "/settings", "/users", "/profile",
    ]:
        resp = admin.get(url)
        assert resp.status_code == 200, f"{url} -> {resp.status_code}"


def test_detail_pages_show_data(admin, seed):
    ids = _full_seed(seed)
    page = admin.get(f"/machines/{ids['machine']}").get_data(as_text=True)
    assert "INV-001" in page
    assert "SN-1" in page or "Терминал" in page
    page = admin.get(f"/sim_cards/{ids['sim']}").get_data(as_text=True)
    assert "8970000000000000001" in page
