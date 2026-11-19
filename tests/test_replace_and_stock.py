"""Причина демонтажа, замена в один шаг, минимальные остатки склада."""
from conftest import db_exec, db_query, db_scalar


def _install_eq(machine, equipment, position=None):
    link = db_exec(
        "INSERT INTO machine_equipment (machine_id, equipment_id, installed_at, sensor_position_id) "
        "VALUES (?, ?, '2026-01-10 10:00:00', ?)",
        (machine, equipment, position),
    )
    db_exec("UPDATE equipment SET status_id = 2 WHERE id = ?", (equipment,))
    return link


def _install_sim(machine, sim):
    link = db_exec(
        "INSERT INTO machine_sim_cards (machine_id, sim_card_id, installed_at) VALUES (?, ?, '2026-01-10 10:00:00')",
        (machine, sim),
    )
    db_exec("UPDATE sim_cards SET status_id = 2 WHERE id = ?", (sim,))
    return link


# ---------- Причина демонтажа ----------

def test_remove_equipment_faulty_sets_status_and_reason(admin, seed):
    link = _install_eq(seed["machine"], seed["sensor"], position=1)
    resp = admin.post(
        f"/machines/{seed['machine']}/remove_equipment/{link}",
        data={"reason": "faulty", "comment": "  не передаёт   давление "},
    )
    assert resp.status_code == 302
    row = db_query("SELECT removed_at, removal_reason, removal_comment FROM machine_equipment WHERE id = ?", (link,))[0]
    assert row["removed_at"] and row["removal_reason"] == "faulty"
    assert row["removal_comment"] == "не передаёт давление"
    assert db_scalar("SELECT status_id FROM equipment WHERE id = ?", (seed["sensor"],)) == 4  # Неисправно
    log = db_scalar("SELECT description FROM operations_log WHERE operation_type = 'remove' ORDER BY id DESC LIMIT 1")
    assert "Причина: Неисправен: не передаёт давление" in log


def test_remove_equipment_planned_and_no_reason_keep_old_status(admin, seed):
    link = _install_eq(seed["machine"], seed["sensor"], position=1)
    admin.post(f"/machines/{seed['machine']}/remove_equipment/{link}", data={"reason": "planned"})
    assert db_scalar("SELECT status_id FROM equipment WHERE id = ?", (seed["sensor"],)) == 3  # Демонтировано
    # Без причины (старая форма, неизвестный код) — как раньше: «Демонтировано», причина пустая
    link2 = _install_eq(seed["machine"], seed["equipment"])
    admin.post(f"/machines/{seed['machine']}/remove_equipment/{link2}", data={"reason": "bogus"})
    row = db_query("SELECT removal_reason FROM machine_equipment WHERE id = ?", (link2,))[0]
    assert row["removal_reason"] is None
    assert db_scalar("SELECT status_id FROM equipment WHERE id = ?", (seed["equipment"],)) == 3


def test_remove_sim_reason_sets_status(admin, seed):
    link = _install_sim(seed["machine"], seed["sim"])
    admin.post(f"/machines/{seed['machine']}/remove_sim/{link}", data={"reason": "faulty", "comment": "нет связи"})
    assert db_scalar("SELECT status_id FROM sim_cards WHERE id = ?", (seed["sim"],)) == 3  # Заблокирована
    assert db_scalar("SELECT removal_reason FROM machine_sim_cards WHERE id = ?", (link,)) == "faulty"
    link2 = _install_sim(seed["machine"], seed["sim2"])
    admin.post(f"/machines/{seed['machine']}/remove_sim/{link2}", data={"reason": "planned"})
    assert db_scalar("SELECT status_id FROM sim_cards WHERE id = ?", (seed["sim2"],)) == 1  # На складе


def test_removal_reason_shown_in_histories(admin, seed):
    link = _install_eq(seed["machine"], seed["sensor"], position=1)
    admin.post(f"/machines/{seed['machine']}/remove_equipment/{link}", data={"reason": "damaged", "comment": "сорван"})
    machine_html = admin.get(f"/machines/{seed['machine']}").get_data(as_text=True)
    assert "· Повреждён: сорван" in machine_html
    card_html = admin.get(f"/equipment/{seed['sensor']}").get_data(as_text=True)
    assert "Повреждён: сорван" in card_html
    sim_link = _install_sim(seed["machine"], seed["sim"])
    admin.post(f"/machines/{seed['machine']}/remove_sim/{sim_link}", data={"reason": "blocked"})
    assert "Заблокирована оператором" in admin.get(f"/sim_cards/{seed['sim']}").get_data(as_text=True)


def test_remove_dialog_on_cards(admin, seed):
    link = _install_eq(seed["machine"], seed["sensor"], position=1)
    _install_sim(seed["machine"], seed["sim"])
    html = admin.get(f"/machines/{seed['machine']}").get_data(as_text=True)
    assert 'id="remove-dialog"' in html
    assert 'data-remove="equipment"' in html and 'data-remove="sim"' in html
    assert '<option value="faulty" data-status="Неисправно">Неисправен</option>' in html
    # «заменить» на колесе — ведёт на замену этой установки
    assert f'href="/machines/{seed["machine"]}/replace_equipment/{link}"' in html


def test_reader_sees_no_remove_or_replace(client_for, seed):
    _install_eq(seed["machine"], seed["sensor"], position=1)
    html = client_for("reader").get(f"/machines/{seed['machine']}").get_data(as_text=True)
    assert "replace_equipment" not in html and "data-remove=" not in html


# ---------- Замена в один шаг ----------

def test_replace_page_lists_same_kind_same_name_first(admin, seed):
    link = _install_eq(seed["machine"], seed["sensor"], position=3)
    db_exec("INSERT INTO equipment (name, serial_number, status_id, equipment_kind) VALUES ('Альфа-датчик', 'A-1', 1, 'sensor')")
    db_exec("INSERT INTO equipment (name, serial_number, status_id, equipment_kind) VALUES ('Датчик давления', 'DS-BAD', 4, 'sensor')")
    html = admin.get(f"/machines/{seed['machine']}/replace_equipment/{link}").get_data(as_text=True)
    assert "Замена датчика на позиции ЗЛН" in html
    assert "DS-2" in html and "A-1" in html
    assert html.index("DS-2") < html.index("A-1")       # то же наименование — первым
    assert "SN-1" not in html                              # терминал — другой вид
    assert "DS-BAD" not in html                            # неисправный — не на складе
    assert "то же наименование" in html


def test_replace_swaps_in_one_step(admin, seed):
    link = _install_eq(seed["machine"], seed["sensor"], position=3)
    resp = admin.post(
        f"/machines/{seed['machine']}/replace_equipment/{link}",
        data={"equipment_id": seed["sensor2"], "reason": "faulty", "comment": "нет данных"},
    )
    assert resp.status_code == 302 and resp.headers["Location"].endswith(f"/machines/{seed['machine']}")
    old = db_query("SELECT * FROM machine_equipment WHERE id = ?", (link,))[0]
    assert old["removed_at"] and old["removal_reason"] == "faulty" and old["removal_comment"] == "нет данных"
    new = db_query(
        "SELECT * FROM machine_equipment WHERE equipment_id = ? AND removed_at IS NULL", (seed["sensor2"],)
    )[0]
    assert new["machine_id"] == seed["machine"] and new["sensor_position_id"] == 3
    assert new["installed_at"] == old["removed_at"]
    assert db_scalar("SELECT status_id FROM equipment WHERE id = ?", (seed["sensor"],)) == 4
    assert db_scalar("SELECT status_id FROM equipment WHERE id = ?", (seed["sensor2"],)) == 2
    logs = [r[0] for r in db_query("SELECT description FROM operations_log ORDER BY id")]
    assert any("Замена: демонтаж" in d and "позиции ЗЛН" in d and "Неисправен: нет данных" in d for d in logs)
    assert any("Замена: установка" in d and "вместо DS-1" in d for d in logs)


def test_replace_validation(admin, seed):
    link = _install_eq(seed["machine"], seed["sensor"], position=3)
    url = f"/machines/{seed['machine']}/replace_equipment/{link}"
    # Без причины
    html = admin.post(url, data={"equipment_id": seed["sensor2"], "reason": ""}).get_data(as_text=True)
    assert "Укажите причину замены" in html
    # Другой вид (терминал вместо датчика)
    html = admin.post(url, data={"equipment_id": seed["equipment"], "reason": "faulty"}).get_data(as_text=True)
    assert "Датчик меняется только на датчик" in html
    # Не на складе
    db_exec("UPDATE equipment SET status_id = 4 WHERE id = ?", (seed["sensor2"],))
    html = admin.post(url, data={"equipment_id": seed["sensor2"], "reason": "faulty"}).get_data(as_text=True)
    assert "не числится на складе" in html
    # Ничего не изменилось
    assert db_scalar("SELECT removed_at FROM machine_equipment WHERE id = ?", (link,)) is None
    assert db_scalar("SELECT status_id FROM equipment WHERE id = ?", (seed["sensor"],)) == 2


def test_replace_already_removed_link(admin, seed):
    link = _install_eq(seed["machine"], seed["sensor"], position=3)
    admin.post(f"/machines/{seed['machine']}/remove_equipment/{link}", data={"reason": "planned"})
    resp = admin.get(f"/machines/{seed['machine']}/replace_equipment/{link}")
    assert resp.status_code == 302
    resp = admin.post(f"/machines/{seed['machine']}/replace_equipment/{link}", data={"equipment_id": seed["sensor2"], "reason": "faulty"})
    assert resp.status_code == 302
    assert db_scalar("SELECT COUNT(*) FROM machine_equipment WHERE equipment_id = ?", (seed["sensor2"],)) == 0


def test_replace_forbidden_for_maintenance_and_reader(client_for, seed):
    link = _install_eq(seed["machine"], seed["sensor"], position=3)
    for role in ("maintenance", "reader"):
        c = client_for(role)
        c.post(f"/machines/{seed['machine']}/replace_equipment/{link}", data={"equipment_id": seed["sensor2"], "reason": "faulty"})
        assert db_scalar("SELECT removed_at FROM machine_equipment WHERE id = ?", (link,)) is None


# ---------- Минимальные остатки ----------

def test_stock_page_counts(admin, seed):
    html = admin.get("/stock").get_data(as_text=True)
    assert "Остатки на складе" in html
    assert "Датчик давления" in html and "Терминал" in html and "SIM-карты" in html
    assert 'name="min__Датчик давления"' in html and 'name="min____sim__"' in html


def test_stock_minimums_save_and_dashboard_tile(admin, seed):
    resp = admin.post("/stock", data={"min__Датчик давления": "5", "min__Терминал": "1", "min____sim__": "3"})
    assert resp.status_code == 302
    rows = {r["name"]: r["min_qty"] for r in db_query("SELECT * FROM stock_minimums")}
    assert rows == {"Датчик давления": 5, "Терминал": 1, "__sim__": 3}
    # Датчиков на складе 2 из 5, SIM 2 из 3; терминал 1 из 1 — норма
    short_html = admin.get("/stock?only=short").get_data(as_text=True)
    assert "не хватает 3" in short_html and "не хватает 1" in short_html
    assert "Терминал" not in short_html
    dash = admin.get("/").get_data(as_text=True)
    assert "Пора заказывать" in dash
    assert "Ниже минимума: Датчик давления, SIM-карты" in dash
    # Установленный датчик уже не «на складе»
    _install_eq(seed["machine"], seed["sensor"], position=1)
    assert "не хватает 4" in admin.get("/stock?only=short").get_data(as_text=True)
    # 0 или пусто — снять контроль
    admin.post("/stock", data={"min__Датчик давления": "", "min____sim__": "0", "min__Терминал": "1"})
    assert {r["name"] for r in db_query("SELECT name FROM stock_minimums")} == {"Терминал"}
    assert "Ниже минимума" not in admin.get("/").get_data(as_text=True)


def test_stock_minimums_bad_value_and_unknown_name(admin, seed):
    html = admin.post("/stock", data={"min__Терминал": "-2", "min__Неизвестно": "4"}, follow_redirects=True).get_data(as_text=True)
    assert "Не сохранено" in html and "Терминал" in html
    assert db_scalar("SELECT COUNT(*) FROM stock_minimums") == 0


def test_stock_minimums_roles(client_for, seed):
    for role in ("reader", "maintenance"):
        c = client_for(role)
        html = c.get("/stock").get_data(as_text=True)
        assert "Остатки на складе" in html and 'name="min__' not in html
        c.post("/stock", data={"min__Терминал": "3"})
        assert db_scalar("SELECT COUNT(*) FROM stock_minimums") == 0
    client_for("editor").post("/stock", data={"min__Терминал": "3"})
    assert db_scalar("SELECT min_qty FROM stock_minimums WHERE name = 'Терминал'") == 3


def test_written_off_not_counted(admin, seed):
    db_exec("INSERT INTO equipment (name, serial_number, status_id, equipment_kind) VALUES ('Старый блок', 'OLD-1', 5, 'other')")
    assert "Старый блок" not in admin.get("/stock").get_data(as_text=True)


def test_remove_healthy_back_to_stock_can_install_again(admin, seed):
    link = _install_eq(seed["machine"], seed["sensor"], position=1)
    admin.post(f"/machines/{seed['machine']}/remove_equipment/{link}", data={"reason": "to_stock"})
    assert db_scalar("SELECT status_id FROM equipment WHERE id = ?", (seed["sensor"],)) == 1  # На складе
    # Сразу виден в списке для установки и ставится на другой борт
    page = admin.get(f"/machines/{seed['machine2']}/install_equipment").get_data(as_text=True)
    assert "DS-1" in page
    admin.post(f"/machines/{seed['machine2']}/install_equipment", data={"equipment_id": seed["sensor"], "sensor_position_id": "2"})
    assert db_scalar("SELECT status_id FROM equipment WHERE id = ?", (seed["sensor"],)) == 2
    sim_link = _install_sim(seed["machine"], seed["sim"])
    admin.post(f"/machines/{seed['machine']}/remove_sim/{sim_link}", data={"reason": "to_stock"})
    assert db_scalar("SELECT status_id FROM sim_cards WHERE id = ?", (seed["sim"],)) == 1


def test_remove_dialog_default_is_back_to_stock(admin, seed):
    _install_eq(seed["machine"], seed["sensor"], position=1)
    html = admin.get(f"/machines/{seed['machine']}").get_data(as_text=True)
    first = html.index('id="rm-reason-equipment"')
    assert html.index('value="to_stock" data-status="На складе"', first) < html.index('value="faulty"', first)
