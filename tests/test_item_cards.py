"""Карточки оборудования и SIM: где стоит сейчас, история, действия по роли, смена статуса."""
from conftest import db_exec, db_scalar


def _install_eq(machine, equipment, position=None, installed="2026-01-10 10:00:00", removed=None):
    return db_exec(
        "INSERT INTO machine_equipment (machine_id, equipment_id, installed_at, sensor_position_id, removed_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (machine, equipment, installed, position, removed),
    )


def _install_sim(machine, sim, installed="2026-01-10 10:00:00", removed=None):
    return db_exec(
        "INSERT INTO machine_sim_cards (machine_id, sim_card_id, installed_at, removed_at) VALUES (?, ?, ?, ?)",
        (machine, sim, installed, removed),
    )


# ---------- Оборудование ----------

def test_equipment_card_installed(admin, seed):
    eq = seed["sensor"]
    _install_eq(seed["machine2"], eq, installed="2025-01-01 09:00:00", removed="2025-01-31 09:00:00")
    link = _install_eq(seed["machine"], eq, position=1)
    db_exec("UPDATE equipment SET status_id = 2 WHERE id = ?", (eq,))
    db_exec(
        "INSERT INTO shift_tasks (created_by_user_id, for_shift, effective_date, title, status, equipment_id) "
        "VALUES (1, 'day', date('now'), 'Проверить датчик', 'pending', ?)",
        (eq,),
    )
    html = admin.get(f"/equipment/{eq}").get_data(as_text=True)
    assert 'class="pill pill-ok"' in html  # «Установлено» — зелёная плашка
    assert "позиция ПЛ · на борту с 10.01.26" in html
    assert "установок, бортов: 2" in html
    assert "01.01.25 — 31.01.25 · 30 дн." in html
    assert "Проверить датчик" in html
    # Снять — с подтверждением и возвратом в карточку оборудования
    assert f'action="/machines/{seed["machine"]}/remove_equipment/{link}"' in html
    assert 'name="back" value="card"' in html
    assert html.count('class="row-del"') == 1
    # На борту: установить нельзя, удалить нельзя
    assert "Установить на борт" not in html
    assert "Удалить можно только снятое с борта" in html
    # «+ Задание» — с выбранным оборудованием и бортом
    assert f"/shift_tasks?equipment={eq}&amp;machine={seed['machine']}#add" in html


def test_equipment_card_free_can_install(admin, seed):
    html = admin.get(f"/equipment/{seed['sensor']}").get_data(as_text=True)
    assert "Установить на борт" in html
    assert 'list="pick-machines"' in html and '<option value="INV-002">' in html
    assert 'name="position"' in html  # датчик — можно сразу выбрать позицию
    assert "ещё не устанавливали" in html
    # Шаг выбора борта открывает установку только этого оборудования на нём
    resp = admin.get(f"/equipment/{seed['sensor']}/install_to?machine=INV-001&position=2")
    assert resp.status_code == 302
    loc = resp.headers["Location"]
    assert f"/machines/{seed['machine']}/install_equipment" in loc and f"equipment={seed['sensor']}" in loc and "position=2" in loc
    page = admin.get(loc).get_data(as_text=True)
    assert "DS-1" in page and "DS-2" not in page and "Назад к оборудованию" in page
    # Неизвестный борт — назад в карточку
    resp = admin.get(f"/equipment/{seed['sensor']}/install_to?machine=NOPE")
    assert resp.headers["Location"].endswith(f"/equipment/{seed['sensor']}")


def test_equipment_remove_returns_to_card(admin, seed):
    link = _install_eq(seed["machine"], seed["equipment"])
    resp = admin.post(f"/machines/{seed['machine']}/remove_equipment/{link}", data={"back": "card"})
    assert resp.headers["Location"].endswith(f"/equipment/{seed['equipment']}")
    assert db_scalar("SELECT status_id FROM equipment WHERE id = ?", (seed["equipment"],)) == 3
    html = admin.get(f"/equipment/{seed['equipment']}").get_data(as_text=True)
    assert "Вернуть на склад" in html and "снято с INV-001" in html


def test_equipment_set_status(admin, seed):
    eq = seed["equipment"]
    db_exec("UPDATE equipment SET status_id = 3 WHERE id = ?", (eq,))
    admin.post(f"/equipment/{eq}/set_status", data={"status_id": "1"})
    assert db_scalar("SELECT status_id FROM equipment WHERE id = ?", (eq,)) == 1
    assert db_scalar("SELECT COUNT(*) FROM operations_log WHERE entity_type = 'equipment' AND entity_id = ?", (eq,)) == 1
    # «Установлено» вручную не ставится; на борту статус не меняется
    admin.post(f"/equipment/{eq}/set_status", data={"status_id": "2"})
    assert db_scalar("SELECT status_id FROM equipment WHERE id = ?", (eq,)) == 1
    _install_eq(seed["machine"], eq)
    admin.post(f"/equipment/{eq}/set_status", data={"status_id": "5"})
    assert db_scalar("SELECT status_id FROM equipment WHERE id = ?", (eq,)) == 1
    # Журнал с исполнителем
    html = admin.get(f"/equipment/{eq}").get_data(as_text=True)
    assert "Демонтировано → На складе" in html and ">admin<" in html


def test_equipment_card_reader_has_no_actions(client_for, seed):
    _install_eq(seed["machine"], seed["equipment"])
    reader = client_for("reader")
    html = reader.get(f"/equipment/{seed['equipment']}").get_data(as_text=True)
    assert 'class="row-del"' not in html
    assert "Изменить данные" not in html and "Удалить из учёта" not in html and "/shift_tasks?equipment=" not in html
    assert "set_status" not in html
    assert reader.post(f"/equipment/{seed['equipment']}/set_status", data={"status_id": "4"}).status_code == 302
    assert db_scalar("SELECT status_id FROM equipment WHERE id = ?", (seed["equipment"],)) == 1


def test_shift_task_form_prefills_equipment(admin, seed):
    html = admin.get(f"/shift_tasks?equipment={seed['sensor']}").get_data(as_text=True)
    assert f'name="equipment_id" value="{seed["sensor"]}"' in html
    assert 'value="Датчик давления (DS-1) — датчик"' in html


# ---------- SIM-карта ----------

def test_sim_card_installed(admin, seed):
    db_exec("UPDATE sim_cards SET ip_address = '10.0.0.5', operator = 'Beeline' WHERE id = ?", (seed["sim"],))
    _install_sim(seed["machine2"], seed["sim"], installed="2025-03-01 10:00:00", removed="2025-03-11 10:00:00")
    link = _install_sim(seed["machine"], seed["sim"])
    db_exec("UPDATE sim_cards SET status_id = 2 WHERE id = ?", (seed["sim"],))
    html = admin.get(f"/sim_cards/{seed['sim']}").get_data(as_text=True)
    assert 'class="pill pill-ok"' in html
    assert "на борту с 10.01.26" in html and "10.0.0.5" in html and "Beeline" in html
    assert "01.03.25 — 11.03.25 · 10 дн." in html
    assert 'data-copy="8970000000000000001"' in html
    assert f'action="/machines/{seed["machine"]}/remove_sim/{link}"' in html
    assert "Установить на борт" not in html
    resp = admin.post(f"/machines/{seed['machine']}/remove_sim/{link}", data={"back": "card"})
    assert resp.headers["Location"].endswith(f"/sim_cards/{seed['sim']}")
    assert db_scalar("SELECT status_id FROM sim_cards WHERE id = ?", (seed["sim"],)) == 1


def test_sim_card_install_to_only_free_machines(admin, seed):
    _install_sim(seed["machine2"], seed["sim2"])
    html = admin.get(f"/sim_cards/{seed['sim']}").get_data(as_text=True)
    assert '<option value="INV-001">' in html and '<option value="INV-002">' not in html
    resp = admin.get(f"/sim_cards/{seed['sim']}/install_to?machine=INV-002")
    assert resp.headers["Location"].endswith(f"/sim_cards/{seed['sim']}")
    resp = admin.get(f"/sim_cards/{seed['sim']}/install_to?machine=INV-001")
    loc = resp.headers["Location"]
    assert f"/machines/{seed['machine']}/install_sim" in loc and f"sim={seed['sim']}" in loc
    page = admin.get(loc).get_data(as_text=True)
    assert "+77001112233" in page and "Назад к SIM-карте" in page


def test_sim_card_set_status(admin, seed):
    admin.post(f"/sim_cards/{seed['sim']}/set_status", data={"status_id": "3"})
    assert db_scalar("SELECT status_id FROM sim_cards WHERE id = ?", (seed["sim"],)) == 3
    html = admin.get(f"/sim_cards/{seed['sim']}").get_data(as_text=True)
    assert 'class="pill pill-critical"' in html and "Разблокирована, на склад" in html
    assert "На складе → Заблокирована" in html


def test_sim_card_reader_has_no_actions(client_for, seed):
    html = client_for("reader").get(f"/sim_cards/{seed['sim']}").get_data(as_text=True)
    assert "Установить на борт" not in html and "Изменить данные" not in html and "set_status" not in html
