"""Безопасность: вход, права ролей, CSRF, инъекции, XSS, редиректы."""
from urllib.parse import urlparse

import pytest

from conftest import PASSWORD, db_exec, db_query, db_scalar, login, make_app

ALL_TABLES = (
    "equipment", "machines", "machine_types", "machine_models", "sim_cards", "machine_equipment",
    "machine_sim_cards", "machine_maintenance", "shift_tasks", "shift_task_types", "sensor_positions",
    "users", "app_settings", "journal_entries", "journal_shift_engineers",
)


def snapshot():
    """Содержимое основных таблиц, чтобы проверить, что запрос ничего не изменил."""
    return {t: [tuple(r) for r in db_query(f"SELECT * FROM {t} ORDER BY 1")] for t in ALL_TABLES}


def post_urls(app, seed):
    task = db_exec(
        "INSERT INTO shift_tasks (created_by_user_id, for_shift, effective_date, title) VALUES (1, 'day', date('now'), 'Чужое задание')"
    )
    link_eq = db_exec("INSERT INTO machine_equipment (machine_id, equipment_id) VALUES (?, ?)", (seed["machine"], seed["equipment"]))
    link_sim = db_exec("INSERT INTO machine_sim_cards (machine_id, sim_card_id) VALUES (?, ?)", (seed["machine"], seed["sim"]))
    from sclad.shift_journal import current_shift

    shift, shift_date = current_shift()
    entry = db_exec(
        "INSERT INTO journal_entries (shift_date, shift, entry_time, content) VALUES (?, ?, '09:00', 'Запись админа')",
        (shift_date, shift),
    )
    engineer = db_exec(
        "INSERT INTO journal_shift_engineers (shift_date, shift, name) VALUES (?, ?, 'Инженер')", (shift_date, shift)
    )
    values = {
        "user_id": 4, "equipment_id": seed["sensor"], "position_id": 9, "type_id": seed["type"],
        "model_id": seed["model"], "machine_id": seed["machine2"], "maintenance_id": 1, "task_id": task,
        "sim_id": seed["sim2"], "link_id": link_eq, "entry_id": entry, "row_id": engineer,
    }
    special = {"shift_task_type_edit": {"type_id": seed["task_type"]}, "shift_task_type_delete": {"type_id": seed["task_type"]},
               "remove_sim": {"machine_id": seed["machine"], "link_id": link_sim},
               "remove_equipment": {"machine_id": seed["machine"], "link_id": link_eq}}
    from flask import url_for

    urls = []
    for rule in app.url_map.iter_rules():
        if "POST" not in rule.methods:
            continue
        args = {a: special.get(rule.endpoint, {}).get(a, values[a]) for a in rule.arguments}
        with app.test_request_context():
            urls.append((rule.endpoint, url_for(rule.endpoint, **args)))
    return urls


GENERIC_FORM = {
    "name": "X", "serial_number": "NEW-SN", "status_id": "1", "inventory_number": "INV-NEW", "machine_model_id": "1",
    "machine_type_id": "1", "code": "NEWPOS", "phone_number": "+7700", "iccid": "899999", "username": "hacker",
    "password": "Pa55word!", "role": "admin", "title": "T", "for_shift": "day", "effective_date": "2026-01-01",
    "action": "add", "machine_id": "1", "performed_at": "2026-01-01", "equipment_id": "2", "sim_card_id": "2",
    "maintenance_interval_months": "7", "reason": "r", "comment": "c", "sort_order": "1",
    "entry_time": "10:00", "content": "взлом", "note": "n", "mail_to": "x@example.kz",
}


# ---------- Без входа ----------

def test_anonymous_redirected_to_login_everywhere(app, seed):
    client = app.test_client()
    for endpoint, url in post_urls(app, seed):
        if endpoint == "login":
            continue
        before = snapshot()
        resp = client.post(url, data=GENERIC_FORM)
        assert resp.status_code == 302 and "/login" in resp.headers["Location"], url
        assert snapshot() == before, f"аноним изменил данные через {url}"
    for url in ["/", "/equipment", "/users", "/settings", "/backup", "/export/excel", "/reports/export", "/operations/export"]:
        resp = client.get(url)
        assert resp.status_code == 302 and "/login" in resp.headers["Location"], url


def test_static_files_available_without_login(app):
    assert app.test_client().get("/static/logo.png").status_code == 200


# ---------- Вход ----------

def test_wrong_password_rejected(app):
    c = app.test_client()
    resp = login(c, "admin", "wrong")
    assert resp.status_code == 200
    assert "Неверный логин или пароль" in resp.get_data(as_text=True)
    assert c.get("/").status_code == 302


def test_sql_injection_in_login(app):
    c = app.test_client()
    for username in ["' OR '1'='1", "admin'--", "admin\" OR 1=1 --"]:
        resp = login(c, username, "' OR '1'='1")
        assert resp.status_code == 200
    assert c.get("/").status_code == 302


def test_lockout_after_5_failed_attempts(app):
    c = app.test_client()
    for _ in range(5):
        login(c, "admin", "wrong")
    resp = login(c, "admin", PASSWORD)  # даже верный пароль не пускает
    assert resp.status_code == 200
    assert "Слишком много неудачных попыток" in resp.get_data(as_text=True)


def test_lockout_behind_nginx_is_per_client(app):
    """За nginx все запросы идут с 127.0.0.1: блокируется только тот, кто ошибался."""
    bad = app.test_client()
    bad.environ_base["HTTP_X_FORWARDED_FOR"] = "10.0.0.1"
    for _ in range(5):
        login(bad, "admin", "wrong")
    assert "Слишком много неудачных попыток" in login(bad, "admin", PASSWORD).get_data(as_text=True)

    good = app.test_client()
    good.environ_base["HTTP_X_FORWARDED_FOR"] = "10.0.0.2"
    assert login(good, "admin", PASSWORD).status_code == 302


def test_forwarded_header_ignored_from_remote_clients(app):
    """Клиент не за прокси не может обойти блокировку, подставляя X-Forwarded-For."""
    c = app.test_client()
    c.environ_base["REMOTE_ADDR"] = "10.0.0.9"
    for i in range(5):
        c.environ_base["HTTP_X_FORWARDED_FOR"] = f"10.1.1.{i}"
        login(c, "admin", "wrong")
    c.environ_base["HTTP_X_FORWARDED_FOR"] = "10.1.1.99"
    assert "Слишком много неудачных попыток" in login(c, "admin", PASSWORD).get_data(as_text=True)


def test_password_stored_as_hash(app):
    stored = db_scalar("SELECT password_hash FROM users WHERE username = 'admin'")
    assert PASSWORD not in stored


@pytest.mark.parametrize("target", [
    "http://evil.example.com/", "//evil.example.com/", "/\\evil.example.com", "https:evil.example.com",
    "javascript:alert(1)", "\\\\evil.example.com",
])
def test_open_redirect_after_login_blocked(app, target):
    c = app.test_client()
    resp = c.post("/login", data={"username": "admin", "password": PASSWORD, "next": target})
    assert resp.status_code == 302
    location = resp.headers["Location"]
    parsed = urlparse(location)
    assert parsed.scheme in ("", "http") and parsed.netloc in ("", "localhost"), location
    assert not location.startswith("//") and "javascript" not in location, location


def test_local_next_redirect_kept(app):
    c = app.test_client()
    resp = c.post("/login", data={"username": "admin", "password": PASSWORD, "next": "/machines?q=1"})
    assert resp.headers["Location"].endswith("/machines?q=1")


def test_logout_ends_session(admin):
    admin.get("/logout")
    assert admin.get("/").status_code == 302


# ---------- Права ролей ----------

@pytest.mark.parametrize("role", ["editor", "maintenance", "reader"])
@pytest.mark.parametrize("url", ["/users", "/users/add", "/users/1/edit", "/settings", "/backup"])
def test_admin_only_pages(client_for, role, url):
    resp = client_for(role).get(url)
    assert resp.status_code == 302, f"{role} открыл {url}"


@pytest.mark.parametrize("role", ["editor", "maintenance", "reader"])
def test_non_admin_cannot_create_users(app, client_for, role):
    c = client_for(role)
    c.post("/users/add", data={"username": "hacker", "password": "Pa55word!", "role": "admin"})
    assert db_scalar("SELECT COUNT(*) FROM users WHERE username = 'hacker'") == 0


def test_reader_cannot_change_anything(app, seed, client_for):
    c = client_for("reader")
    for endpoint, url in post_urls(app, seed):
        if endpoint == "login":
            continue
        before = snapshot()
        c.post(url, data=GENERIC_FORM)
        assert snapshot() == before, f"reader изменил данные через {url}"


MAINTENANCE_ALLOWED = {"login", "maintenance_add", "shift_tasks_list", "shift_task_complete", "shift_task_not_done",
                       "shift_task_edit", "shift_task_delete",
                       "journal_entry_add", "journal_entry_edit", "journal_entry_delete", "journal_engineer_add",
                       "journal_engineer_remove"}


def test_maintenance_limited_to_maintenance_and_shift_tasks(app, seed, client_for):
    c = client_for("maintenance")
    for endpoint, url in post_urls(app, seed):
        if endpoint in MAINTENANCE_ALLOWED:
            continue
        before = snapshot()
        c.post(url, data=GENERIC_FORM)
        assert snapshot() == before, f"maintenance изменил данные через {url}"


def test_maintenance_cannot_edit_or_delete_foreign_task(app, client_for):
    task = db_exec(
        "INSERT INTO shift_tasks (created_by_user_id, for_shift, effective_date, title) VALUES (1, 'day', date('now'), 'Задание админа')"
    )
    c = client_for("maintenance")
    c.post(f"/shift_tasks/{task}/delete")
    c.post(f"/shift_tasks/{task}/edit", data={"for_shift": "night", "effective_date": "2026-01-01", "title": "взлом"})
    row = db_query("SELECT title, for_shift FROM shift_tasks WHERE id = ?", (task,))
    assert row and row[0]["title"] == "Задание админа" and row[0]["for_shift"] == "day"


def test_editor_can_edit_data(client_for, seed):
    c = client_for("editor")
    c.post("/equipment/add", data={"name": "Новый", "serial_number": "ED-1", "status_id": "1"})
    assert db_scalar("SELECT COUNT(*) FROM equipment WHERE serial_number = 'ED-1'") == 1


# ---------- CSRF ----------

def test_csrf_required_for_post(app, seed):
    csrf_app = make_app(csrf=True)
    c = csrf_app.test_client()
    # вход тоже защищён CSRF
    resp = c.post("/login", data={"username": "admin", "password": PASSWORD})
    assert resp.status_code == 400
    # войдём через форму с токеном
    import re

    page = c.get("/login").get_data(as_text=True)
    token = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page).group(1)
    resp = c.post("/login", data={"username": "admin", "password": PASSWORD, "csrf_token": token})
    assert resp.status_code == 302
    resp = c.post("/equipment/add", data={"name": "CSRF", "serial_number": "CSRF-1", "status_id": "1"})
    assert resp.status_code == 400
    assert db_scalar("SELECT COUNT(*) FROM equipment WHERE serial_number = 'CSRF-1'") == 0


# ---------- Инъекции и XSS ----------

INJECTIONS = ["' OR 1=1 --", "\"; DROP TABLE equipment; --", "%' UNION SELECT password_hash FROM users --", "1; DELETE FROM machines"]


def test_sql_injection_in_query_params(admin, seed):
    for payload in INJECTIONS:
        for url in ["/equipment", "/machines", "/sim_cards", "/operations", "/maintenance", "/reports", "/shift_tasks"]:
            for param in ("q", "sort", "order", "status", "page", "per_page", "date", "machine_id", "type"):
                resp = admin.get(url, query_string={param: payload})
                assert resp.status_code < 500, f"{url}?{param}={payload} -> {resp.status_code}"
                assert "scrypt:" not in resp.get_data(as_text=True)
        admin.get("/search", query_string={"q": payload})
    assert db_scalar("SELECT COUNT(*) FROM equipment") == 3
    assert db_scalar("SELECT COUNT(*) FROM machines") == 2


def test_sql_injection_in_forms_stored_as_text(admin, seed):
    payload = "x'); DROP TABLE equipment; --"
    admin.post("/equipment/add", data={"name": payload, "serial_number": "INJ-1", "status_id": "1"})
    assert db_scalar("SELECT name FROM equipment WHERE serial_number = 'INJ-1'") == payload
    assert db_scalar("SELECT COUNT(*) FROM equipment") == 4


def test_xss_is_escaped(admin, seed):
    xss = "<script>alert('xss')</script>"
    admin.post("/equipment/add", data={"name": xss, "serial_number": "XSS-1", "description": xss, "status_id": "1"})
    eq_id = db_scalar("SELECT id FROM equipment WHERE serial_number = 'XSS-1'")
    admin.post(f"/machines/{seed['machine']}/install_equipment", data={"equipment_id": eq_id})
    for url in ["/equipment", f"/equipment/{eq_id}", f"/machines/{seed['machine']}", "/operations", "/"]:
        page = admin.get(url).get_data(as_text=True)
        assert xss not in page, f"XSS не экранирован на {url}"


# ---------- Заголовки и cookie ----------

def test_session_cookie_httponly(app):
    c = app.test_client()
    resp = c.post("/login", data={"username": "admin", "password": PASSWORD})
    cookies = resp.headers.getlist("Set-Cookie")
    session_cookie = [h for h in cookies if h.startswith("session=")]
    assert session_cookie and "HttpOnly" in session_cookie[0]


def test_security_headers(admin):
    resp = admin.get("/")
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp.headers.get("X-Frame-Options") in ("DENY", "SAMEORIGIN")
    assert resp.headers.get("Referrer-Policy")


def test_500_page_does_not_leak_traceback(app):
    fresh = make_app()

    @fresh.route("/__boom")
    def boom():
        raise RuntimeError("secret-internal-detail")

    fresh.config["TESTING"] = False
    fresh.config["PROPAGATE_EXCEPTIONS"] = False
    c = fresh.test_client()
    login(c, "admin")
    resp = c.get("/__boom")
    assert resp.status_code == 500
    assert "secret-internal-detail" not in resp.get_data(as_text=True)
