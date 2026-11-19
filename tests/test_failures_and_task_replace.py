"""Повторные отказы и замена прямо из задания на смену."""
from datetime import datetime, timedelta

from conftest import db_exec, db_query, db_scalar


def _ago(days):
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def _removed(machine, equipment, position, days_ago, reason="faulty", comment=None):
    return db_exec(
        "INSERT INTO machine_equipment (machine_id, equipment_id, installed_at, removed_at, sensor_position_id, "
        "removal_reason, removal_comment) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (machine, equipment, _ago(days_ago + 5), _ago(days_ago), position, reason, comment),
    )


def _sensor(sn, status=1):
    return db_exec(
        "INSERT INTO equipment (name, serial_number, status_id, equipment_kind) VALUES ('Датчик давления', ?, ?, 'sensor')",
        (sn, status),
    )


# ---------- Отчёт ----------

def test_failures_report_positions_equipment_machines(admin, seed):
    a, b, c = _sensor("F-1", 4), _sensor("F-2", 4), _sensor("F-3", 1)
    _removed(seed["machine"], a, 3, 10, "faulty", "нет данных")
    _removed(seed["machine"], b, 3, 30, "damaged")
    _removed(seed["machine"], c, 3, 50, "to_stock")         # исправный на склад — не замена
    _removed(seed["machine"], a, 1, 200, "faulty")          # вне 90 дней
    _removed(seed["machine2"], b, 2, 20, "faulty")          # F-2 отказал второй раз, на другом борту
    html = admin.get("/reports/failures").get_data(as_text=True)
    assert "Позиции с частыми заменами" in html
    # ЗЛН на INV-001: 2 замены, обе отказы; исправный возврат не считается
    pos = html[html.index("Позиции с частыми заменами"):html.index("Оборудование, которое снимали")]
    assert "INV-001" in pos and "ЗЛН" in pos and "Неисправен: нет данных" in pos and "F-3" not in pos
    eq = html[html.index("Оборудование, которое снимали"):html.index("Борта с наибольшим")]
    assert "F-2" in eq and "INV-001" in eq and "INV-002" in eq
    assert "F-1" not in eq  # второй отказ F-1 был 200 дней назад
    # За год F-1 тоже попадает
    year = admin.get("/reports/failures?days=365").get_data(as_text=True)
    assert "F-1" in year[year.index("Оборудование, которое снимали"):]


def test_failures_report_empty_and_bad_period(admin, seed):
    html = admin.get("/reports/failures?days=7").get_data(as_text=True)
    assert "Таких позиций нет" in html and "Отказов за период не было" in html
    assert 'class="on">90 дней' in html


def test_wheel_tile_shows_repeated_swaps(admin, seed):
    a, b = _sensor("F-1", 4), _sensor("F-2", 4)
    _removed(seed["machine"], a, 3, 5)
    _removed(seed["machine"], b, 3, 15, None)   # старая запись без причины — тоже замена
    html = admin.get(f"/machines/{seed['machine']}").get_data(as_text=True)
    assert "2 замены за 90 дн." in html
    assert html.count('class="wheel-swaps"') == 1


def test_reports_page_links_failures(admin, seed):
    assert "/reports/failures" in admin.get("/reports").get_data(as_text=True)


def test_reader_can_open_failures(client_for, seed):
    assert client_for("reader").get("/reports/failures").status_code == 200


# ---------- Замена из задания ----------

def _task(machine, equipment=None, status="pending"):
    return db_exec(
        "INSERT INTO shift_tasks (created_by_user_id, for_shift, effective_date, title, status, machine_id, equipment_id) "
        "VALUES (1, 'day', date('now'), 'Заменить датчик ЗЛН', ?, ?, ?)",
        (status, machine, equipment),
    )


def _install(machine, equipment, position):
    link = db_exec(
        "INSERT INTO machine_equipment (machine_id, equipment_id, installed_at, sensor_position_id) VALUES (?, ?, ?, ?)",
        (machine, equipment, _ago(30), position),
    )
    db_exec("UPDATE equipment SET status_id = 2 WHERE id = ?", (equipment,))
    return link


def test_complete_page_offers_replace_for_machine_task(admin, seed):
    link_term = _install(seed["machine"], seed["equipment"], None)
    link = _install(seed["machine"], seed["sensor"], 3)
    task = _task(seed["machine"], seed["sensor"])
    html = admin.get(f"/shift_tasks/{task}/complete").get_data(as_text=True)
    assert "Нужна замена на борту INV-001?" in html
    url = f"/machines/{seed['machine']}/replace_equipment/{link}?task={task}"
    assert url in html
    assert f"/replace_equipment/{link_term}?task={task}" in html
    assert html.index(url) < html.index(f"/replace_equipment/{link_term}")  # оборудование задания — первым
    # Задание без борта — блока нет
    plain = db_exec("INSERT INTO shift_tasks (created_by_user_id, for_shift, effective_date, title, status) VALUES (1, 'day', date('now'), 'Уборка', 'pending')")
    assert "Нужна замена" not in admin.get(f"/shift_tasks/{plain}/complete").get_data(as_text=True)


def test_maintenance_replaces_from_task(client_for, seed):
    """Инженер смены (maintenance) меняет датчик из задания; задание закрывается."""
    link = _install(seed["machine"], seed["sensor"], 3)
    task = _task(seed["machine"], seed["sensor"])
    c = client_for("maintenance")
    html = c.get(f"/shift_tasks/{task}/complete").get_data(as_text=True)
    assert "Нужна замена на борту INV-001?" in html
    assert c.get(f"/machines/{seed['machine']}/replace_equipment/{link}?task={task}").status_code == 200
    resp = c.post(
        f"/machines/{seed['machine']}/replace_equipment/{link}?task={task}",
        data={"equipment_id": seed["sensor2"], "reason": "faulty", "task": task},
    )
    assert resp.status_code == 302 and "/shift_tasks" in resp.headers["Location"]
    assert db_scalar("SELECT removed_at FROM machine_equipment WHERE id = ?", (link,)) is not None
    assert db_scalar("SELECT status FROM shift_tasks WHERE id = ?", (task,)) == "done"
    assert db_scalar("SELECT username FROM operations_log WHERE operation_type = 'install' ORDER BY id DESC LIMIT 1") == "maintenance"


def test_maintenance_cannot_replace_without_task(client_for, seed):
    link = _install(seed["machine"], seed["sensor"], 3)
    other_task = _task(seed["machine2"])          # задание по другому борту — не пропуск
    c = client_for("maintenance")
    for q in ("", f"?task={other_task}", "?task=99999"):
        url = f"/machines/{seed['machine']}/replace_equipment/{link}{q}"
        assert c.get(url).status_code == 302
        c.post(url, data={"equipment_id": seed["sensor2"], "reason": "faulty", "task": q[6:] if q else ""})
    assert db_scalar("SELECT removed_at FROM machine_equipment WHERE id = ?", (link,)) is None
    # Обычное «снять» и установка для maintenance по-прежнему закрыты
    c.post(f"/machines/{seed['machine']}/remove_equipment/{link}", data={"reason": "to_stock"})
    assert db_scalar("SELECT removed_at FROM machine_equipment WHERE id = ?", (link,)) is None
    html = c.get(f"/machines/{seed['machine']}").get_data(as_text=True)
    assert "replace_equipment" not in html and "data-remove=" not in html


def test_replace_from_task_completes_it(admin, seed):
    link = _install(seed["machine"], seed["sensor"], 3)
    task = _task(seed["machine"], seed["sensor"])
    page = admin.get(f"/machines/{seed['machine']}/replace_equipment/{link}?task={task}").get_data(as_text=True)
    assert "По заданию на смену «Заменить датчик ЗЛН»" in page and f'name="task" value="{task}"' in page
    resp = admin.post(
        f"/machines/{seed['machine']}/replace_equipment/{link}?task={task}",
        data={"equipment_id": seed["sensor2"], "reason": "faulty", "comment": "нет данных", "task": task},
    )
    assert resp.status_code == 302 and "/shift_tasks" in resp.headers["Location"]
    assert db_scalar("SELECT status FROM shift_tasks WHERE id = ?", (task,)) == "done"
    comp = db_query("SELECT user_id, comment FROM shift_task_completions WHERE shift_task_id = ?", (task,))
    assert len(comp) == 1
    assert comp[0]["comment"] == "Замена на позиции ЗЛН: DS-1 → DS-2 (Неисправен: нет данных)"
    assert db_scalar("SELECT status_id FROM equipment WHERE id = ?", (seed["sensor2"],)) == 2


def test_replace_with_not_done_task_ignores_task(admin, seed):
    link = _install(seed["machine"], seed["sensor"], 3)
    task = _task(seed["machine"], seed["sensor"], status="not_done")
    resp = admin.post(
        f"/machines/{seed['machine']}/replace_equipment/{link}",
        data={"equipment_id": seed["sensor2"], "reason": "faulty", "task": task},
    )
    assert resp.headers["Location"].endswith(f"/machines/{seed['machine']}")
    assert db_scalar("SELECT status FROM shift_tasks WHERE id = ?", (task,)) == "not_done"
    assert db_scalar("SELECT COUNT(*) FROM shift_task_completions WHERE shift_task_id = ?", (task,)) == 0


def test_plain_complete_still_works(client_for, seed):
    task = _task(seed["machine"])
    c = client_for("maintenance")
    c.post(f"/shift_tasks/{task}/complete", data={"comment": "сделано"})
    assert db_scalar("SELECT status FROM shift_tasks WHERE id = ?", (task,)) == "done"
    c.post(f"/shift_tasks/{task}/complete", data={"comment": "ещё раз"})
    assert db_scalar("SELECT COUNT(*) FROM shift_task_completions WHERE shift_task_id = ?", (task,)) == 1
