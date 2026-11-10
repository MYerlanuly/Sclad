"""Поиск оборудования по мере ввода на страницах заданий на смену."""
from conftest import db_exec


def _many_equipment(n=3000):
    import sqlite3
    import app as app_module

    conn = sqlite3.connect(app_module.DATABASE)
    conn.executemany(
        "INSERT INTO equipment (name, serial_number, status_id, equipment_kind) VALUES (?, ?, 1, 'other')",
        [(f"Антенна ГНСС {i}", f"ANT-{i:05d}") for i in range(n)],
    )
    conn.commit()
    conn.close()


def test_search_requires_login(app):
    resp = app.test_client().get("/api/equipment/search?q=SN")
    assert resp.status_code == 302


def test_search_by_serial_and_cyrillic_name_any_case(admin, seed):
    data = admin.get("/api/equipment/search?q=sn-1").get_json()
    assert [i["id"] for i in data["items"]] == [seed["equipment"]]
    data = admin.get("/api/equipment/search?q=ДАТЧИК").get_json()
    labels = [i["label"] for i in data["items"]]
    assert len(labels) == 2 and all("датчик" in label for label in labels)


def test_search_short_query_returns_nothing(admin, seed):
    assert admin.get("/api/equipment/search?q=S").get_json() == {"items": [], "more": False}


def test_search_limit_and_more_flag(admin, seed):
    _many_equipment(100)
    data = admin.get("/api/equipment/search?q=ант&limit=20").get_json()
    assert len(data["items"]) == 20 and data["more"] is True
    data = admin.get("/api/equipment/search?q=ANT-00042").get_json()
    assert data["items"][0]["label"].startswith("Антенна ГНСС 42 ")


def test_search_is_injection_safe(admin, seed):
    for q in ["%' OR 1=1 --", "__", "%%"]:
        resp = admin.get("/api/equipment/search", query_string={"q": q})
        assert resp.status_code == 200


def test_reader_can_search(client_for, seed):
    assert client_for("reader").get("/api/equipment/search?q=SN").status_code == 200


def test_shift_tasks_page_stays_small_with_large_stock(admin, seed):
    _many_equipment(3000)
    page = admin.get("/shift_tasks")
    assert page.status_code == 200
    assert len(page.data) < 150_000, f"страница {len(page.data) // 1024} КБ"
    assert b"ANT-02999" not in page.data
    assert b"api/equipment/search" in page.data


def test_task_created_with_equipment_from_search(admin, seed):
    eq_id = admin.get("/api/equipment/search?q=SN-1").get_json()["items"][0]["id"]
    admin.post("/shift_tasks", data={"action": "add", "for_shift": "day", "effective_date": "2026-11-10",
                                     "title": "Проверить терминал", "task_type_id": seed["task_type"], "equipment_id": eq_id})
    from conftest import db_scalar

    assert db_scalar("SELECT equipment_id FROM shift_tasks") == seed["equipment"]
    page = admin.get("/shift_tasks?date=2026-11-10").get_data(as_text=True)
    assert "SN-1" in page


def test_edit_page_keeps_selected_equipment_only(admin, seed):
    _many_equipment(500)
    task = db_exec(
        "INSERT INTO shift_tasks (created_by_user_id, for_shift, effective_date, title, equipment_id) VALUES (1, 'day', '2026-11-10', 'Т', ?)",
        (seed["equipment"],),
    )
    page = admin.get(f"/shift_tasks/{task}/edit").get_data(as_text=True)
    assert f'value="{seed["equipment"]}"' in page and "selected" in page
    assert "ANT-00499" not in page
