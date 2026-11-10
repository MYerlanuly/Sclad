"""Целостность данных и основные рабочие сценарии."""
import io
import threading

from openpyxl import load_workbook

from conftest import db_exec, db_query, db_scalar, login, make_app


def active_eq_links(eq_id):
    return db_scalar("SELECT COUNT(*) FROM machine_equipment WHERE equipment_id = ? AND removed_at IS NULL", (eq_id,))


# ---------- Уникальность ----------

def test_equipment_name_serial_unique(admin, seed):
    admin.post("/equipment/add", data={"name": "Терминал", "serial_number": "SN-1", "status_id": "1"})
    assert db_scalar("SELECT COUNT(*) FROM equipment WHERE serial_number = 'SN-1'") == 1


def test_machine_inventory_number_unique(admin, seed):
    admin.post("/machines/add", data={"inventory_number": "INV-001", "machine_model_id": seed["model"]})
    admin.post(f"/machines/{seed['machine2']}/edit", data={"inventory_number": "INV-001", "machine_model_id": seed["model"]})
    assert db_scalar("SELECT COUNT(*) FROM machines WHERE inventory_number = 'INV-001'") == 1


def test_sim_iccid_unique(admin, seed):
    admin.post("/sim_cards/add", data={"phone_number": "+7", "iccid": "8970000000000000001", "status_id": "1"})
    assert db_scalar("SELECT COUNT(*) FROM sim_cards WHERE iccid = '8970000000000000001'") == 1


def test_machine_type_name_unique(admin, seed):
    resp = admin.post("/machine_types/add", data={"name": "Самосвал"})
    assert resp.status_code == 200
    assert db_scalar("SELECT COUNT(*) FROM machine_types WHERE name = 'Самосвал'") == 1


def test_username_unique(admin):
    admin.post("/users/add", data={"username": "editor", "password": "Pa55word!", "role": "admin"})
    assert db_scalar("SELECT role FROM users WHERE username = 'editor'") == "editor"


# ---------- Установка и демонтаж оборудования ----------

def test_install_and_remove_equipment(admin, seed):
    m, eq = seed["machine"], seed["equipment"]
    resp = admin.post(f"/machines/{m}/install_equipment", data={"equipment_id": eq})
    assert resp.status_code == 302
    assert active_eq_links(eq) == 1
    assert db_scalar("SELECT status_id FROM equipment WHERE id = ?", (eq,)) == 2
    link = db_scalar("SELECT id FROM machine_equipment WHERE equipment_id = ? AND removed_at IS NULL", (eq,))
    assert db_scalar("SELECT COUNT(*) FROM operations_log WHERE operation_type = 'install' AND entity_id = ?", (eq,)) == 1
    assert db_scalar("SELECT username FROM operations_log WHERE entity_id = ?", (eq,)) == "admin"

    admin.post(f"/machines/{m}/remove_equipment/{link}")
    assert active_eq_links(eq) == 0
    assert db_scalar("SELECT status_id FROM equipment WHERE id = ?", (eq,)) == 3
    assert db_scalar("SELECT removed_at FROM machine_equipment WHERE id = ?", (link,)) is not None
    # повторный демонтаж ничего не ломает
    resp = admin.post(f"/machines/{m}/remove_equipment/{link}")
    assert resp.status_code == 302


def test_equipment_cannot_be_installed_on_two_machines(admin, seed):
    eq = seed["equipment"]
    admin.post(f"/machines/{seed['machine']}/install_equipment", data={"equipment_id": eq})
    admin.post(f"/machines/{seed['machine2']}/install_equipment", data={"equipment_id": eq})
    assert active_eq_links(eq) == 1


def test_written_off_equipment_cannot_be_installed(admin, seed):
    db_exec("UPDATE equipment SET status_id = 5 WHERE id = ?", (seed["equipment"],))
    admin.post(f"/machines/{seed['machine']}/install_equipment", data={"equipment_id": seed["equipment"]})
    assert active_eq_links(seed["equipment"]) == 0


def test_sensor_position_cannot_be_used_twice(admin, seed):
    m = seed["machine"]
    admin.post(f"/machines/{m}/install_equipment", data={"equipment_id": seed["sensor"], "sensor_position_id": "1"})
    admin.post(f"/machines/{m}/install_equipment", data={"equipment_id": seed["sensor2"], "sensor_position_id": "1"})
    assert db_scalar(
        "SELECT COUNT(*) FROM machine_equipment WHERE machine_id = ? AND sensor_position_id = 1 AND removed_at IS NULL", (m,)
    ) == 1
    assert active_eq_links(seed["sensor2"]) == 0


def test_installed_equipment_cannot_be_deleted(admin, seed):
    admin.post(f"/machines/{seed['machine']}/install_equipment", data={"equipment_id": seed["equipment"]})
    admin.post(f"/equipment/{seed['equipment']}/delete")
    assert db_scalar("SELECT COUNT(*) FROM equipment WHERE id = ?", (seed["equipment"],)) == 1


def test_machine_with_installed_items_cannot_be_deleted(admin, seed):
    admin.post(f"/machines/{seed['machine']}/install_equipment", data={"equipment_id": seed["equipment"]})
    admin.post(f"/machines/{seed['machine']}/delete")
    assert db_scalar("SELECT COUNT(*) FROM machines WHERE id = ?", (seed["machine"],)) == 1


def test_used_reference_data_cannot_be_deleted(admin, seed):
    admin.post(f"/machine_types/{seed['type']}/delete")
    admin.post(f"/machine_models/{seed['model']}/delete")
    assert db_scalar("SELECT COUNT(*) FROM machine_types") == 1
    assert db_scalar("SELECT COUNT(*) FROM machine_models") == 1


def test_deleting_equipment_with_history_keeps_pages_working(admin, seed):
    m, eq = seed["machine"], seed["equipment"]
    admin.post(f"/machines/{m}/install_equipment", data={"equipment_id": eq})
    link = db_scalar("SELECT id FROM machine_equipment WHERE equipment_id = ?", (eq,))
    admin.post(f"/machines/{m}/remove_equipment/{link}")
    admin.post(f"/equipment/{eq}/delete")
    for url in [f"/machines/{m}", "/machines", "/equipment", "/reports", "/operations", "/export/excel", "/"]:
        assert admin.get(url).status_code < 500, url


# ---------- SIM-карты ----------

def test_install_and_remove_sim(admin, seed):
    m, sim = seed["machine"], seed["sim"]
    admin.post(f"/machines/{m}/install_sim", data={"sim_card_id": sim})
    assert db_scalar("SELECT status_id FROM sim_cards WHERE id = ?", (sim,)) == 2
    link = db_scalar("SELECT id FROM machine_sim_cards WHERE sim_card_id = ? AND removed_at IS NULL", (sim,))
    assert link
    admin.post(f"/machines/{m}/remove_sim/{link}")
    assert db_scalar("SELECT status_id FROM sim_cards WHERE id = ?", (sim,)) == 1
    assert db_scalar("SELECT COUNT(*) FROM machine_sim_cards WHERE removed_at IS NULL") == 0


def test_one_sim_per_machine_and_one_machine_per_sim(admin, seed):
    admin.post(f"/machines/{seed['machine']}/install_sim", data={"sim_card_id": seed["sim"]})
    admin.post(f"/machines/{seed['machine']}/install_sim", data={"sim_card_id": seed["sim2"]})
    resp = admin.post(f"/machines/{seed['machine2']}/install_sim", data={"sim_card_id": seed["sim"]})
    assert resp.status_code < 500
    assert db_scalar("SELECT COUNT(*) FROM machine_sim_cards WHERE removed_at IS NULL") == 1


def test_installed_sim_cannot_be_deleted(admin, seed):
    admin.post(f"/machines/{seed['machine']}/install_sim", data={"sim_card_id": seed["sim"]})
    admin.post(f"/sim_cards/{seed['sim']}/delete")
    assert db_scalar("SELECT COUNT(*) FROM sim_cards WHERE id = ?", (seed["sim"],)) == 1


# ---------- ТО ----------

def test_maintenance_next_due_uses_interval(admin, seed):
    admin.post("/settings", data={"maintenance_interval_months": "6"})
    admin.post("/maintenance/add", data={"machine_id": seed["machine"], "performed_at": "2026-01-31", "engineers": ["1"]})
    row = db_query("SELECT performed_at, next_due_at, performed_by FROM machine_maintenance")[0]
    assert row["performed_at"] == "2026-01-31"
    assert row["next_due_at"] == "2026-07-31"
    assert row["performed_by"]
    mid = db_scalar("SELECT id FROM machine_maintenance")
    assert admin.get(f"/maintenance/{mid}/act").status_code == 200


def test_maintenance_end_of_month_rollover():
    import app as app_module

    assert app_module.maintenance_next_due("2026-11-30", 3) == "2027-02-28"
    assert app_module.maintenance_next_due("2027-11-30", 3) == "2028-02-29"


def test_maintenance_rejects_unknown_machine_and_bad_date(admin, seed):
    admin.post("/maintenance/add", data={"machine_id": "999", "performed_at": "2026-01-01"})
    admin.post("/maintenance/add", data={"machine_id": seed["machine"], "performed_at": "31.01.2026"})
    assert db_scalar("SELECT COUNT(*) FROM machine_maintenance") == 0


# ---------- Задания на смену ----------

def test_shift_task_lifecycle(client_for, seed):
    c = client_for("maintenance")
    c.post("/shift_tasks", data={"action": "add", "for_shift": "night", "effective_date": "2026-11-10",
                                 "title": "Заменить датчик", "task_type_id": seed["task_type"], "machine_id": seed["machine"]})
    task = db_query("SELECT * FROM shift_tasks")[0]
    assert task["status"] == "pending" and task["machine_id"] == seed["machine"]
    c.post(f"/shift_tasks/{task['id']}/not_done", data={"reason": ""})
    assert db_scalar("SELECT status FROM shift_tasks") == "pending"  # без причины нельзя
    c.post(f"/shift_tasks/{task['id']}/complete", data={"comment": "Сделано"})
    assert db_scalar("SELECT status FROM shift_tasks") == "done"
    assert db_scalar("SELECT COUNT(*) FROM shift_task_completions") >= 1
    # выполненное задание нельзя удалить
    c.post(f"/shift_tasks/{task['id']}/delete")
    assert db_scalar("SELECT COUNT(*) FROM shift_tasks") == 1


def test_shift_task_not_done_requires_reason(admin, seed):
    admin.post("/shift_tasks", data={"action": "add", "for_shift": "day", "effective_date": "2026-11-10", "title": "Т",
                                     "task_type_id": seed["task_type"]})
    tid = db_scalar("SELECT id FROM shift_tasks")
    admin.post(f"/shift_tasks/{tid}/not_done", data={"reason": "Нет запчастей"})
    row = db_query("SELECT status, not_done_reason FROM shift_tasks")[0]
    assert row["status"] == "not_done" and row["not_done_reason"] == "Нет запчастей"


# ---------- Импорт и экспорт ----------

def _csv(rows):
    text = "\ufeff" + "\n".join(";".join(r) for r in rows)
    return io.BytesIO(text.encode("utf-8"))


def test_import_templates_download(admin):
    for kind in ("machines", "equipment", "sim_cards"):
        resp = admin.get(f"/import/{kind}/template")
        assert resp.status_code == 200
        assert resp.mimetype == "text/csv"
        assert ";" in resp.data.decode("utf-8-sig").splitlines()[0]


def test_template_files_can_be_imported_back(admin, seed):
    """Скачанный шаблон без изменений загружается обратно без ошибок."""
    for kind in ("equipment", "sim_cards"):
        tpl = admin.get(f"/import/{kind}/template").data
        resp = admin.post(f"/import/{kind}", data={"file": (io.BytesIO(tpl), "t.csv")}, content_type="multipart/form-data")
        assert resp.status_code == 302
    assert db_scalar("SELECT COUNT(*) FROM equipment") > 3
    assert db_scalar("SELECT COUNT(*) FROM sim_cards") > 2


def test_import_skips_duplicates(admin, seed):
    rows = [["Наименование", "Серийный номер"], ["Терминал Б", "IMP-1"], ["Терминал Б", "IMP-1"], ["Терминал", "SN-1"]]
    admin.post("/import/equipment", data={"file": (_csv(rows), "e.csv")}, content_type="multipart/form-data")
    assert db_scalar("SELECT COUNT(*) FROM equipment WHERE serial_number = 'IMP-1'") == 1
    assert db_scalar("SELECT COUNT(*) FROM equipment WHERE serial_number = 'SN-1'") == 1
    rows = [["Номер", "ICCID"], ["+7701", "ICC-IMP-1"], ["+7702", "8970000000000000001"]]
    admin.post("/import/sim_cards", data={"file": (_csv(rows), "s.csv")}, content_type="multipart/form-data")
    assert db_scalar("SELECT COUNT(*) FROM sim_cards WHERE iccid = 'ICC-IMP-1'") == 1
    assert db_scalar("SELECT COUNT(*) FROM sim_cards") == 3


def test_import_machines_needs_existing_model(admin, seed):
    rows = [["Инв. номер", "Модель"], ["IMP-M-1", "БелАЗ-75131"], ["IMP-M-2", "Несуществующая"], ["INV-001", "БелАЗ-75131"]]
    admin.post("/import/machines", data={"file": (_csv(rows), "m.csv")}, content_type="multipart/form-data")
    assert db_scalar("SELECT COUNT(*) FROM machines WHERE inventory_number = 'IMP-M-1'") == 1
    assert db_scalar("SELECT COUNT(*) FROM machines WHERE inventory_number = 'INV-001'") == 1
    assert db_query("PRAGMA foreign_key_check") == []


def test_import_cp1251_file(admin, seed):
    data = "Наименование;Серийный номер\nТерминал В;CP-1\n".encode("cp1251")
    admin.post("/import/equipment", data={"file": (io.BytesIO(data), "e.csv")}, content_type="multipart/form-data")
    assert db_scalar("SELECT name FROM equipment WHERE serial_number = 'CP-1'") == "Терминал В"


def test_exports(admin, seed):
    admin.post(f"/machines/{seed['machine']}/install_equipment", data={"equipment_id": seed["equipment"]})
    resp = admin.get("/export/excel")
    assert resp.status_code == 200
    wb = load_workbook(io.BytesIO(resp.data))
    assert wb.sheetnames
    resp = admin.get("/operations/export")
    assert resp.status_code == 200 and "Терминал" in resp.data.decode("utf-8-sig")
    for t in ("operations", "equipment", "machines", "sim_cards", "maintenance", "shift_tasks"):
        assert admin.get("/reports/export", query_string={"type": t}).status_code < 500


def test_backup_is_valid_sqlite_copy(admin, seed, tmp_path):
    resp = admin.get("/backup")
    assert resp.status_code == 200
    path = tmp_path / "copy.db"
    path.write_bytes(resp.data)
    import sqlite3

    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("SELECT COUNT(*) FROM machines").fetchone()[0] == 2
    conn.close()


# ---------- Одновременные действия ----------

def _parallel(fn, n):
    errors = []

    def run(i):
        try:
            fn(i)
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return errors


def test_parallel_install_of_same_sim_keeps_single_link(app, seed):
    flask_app = make_app()
    flask_app.config["PROPAGATE_EXCEPTIONS"] = False
    statuses = []

    def worker(i):
        c = flask_app.test_client()
        login(c, "admin")
        machine = seed["machine"] if i % 2 == 0 else seed["machine2"]
        statuses.append(c.post(f"/machines/{machine}/install_sim", data={"sim_card_id": seed["sim"]}).status_code)

    errors = _parallel(worker, 8)
    assert not errors
    assert db_scalar("SELECT COUNT(*) FROM machine_sim_cards WHERE removed_at IS NULL AND sim_card_id = ?", (seed["sim"],)) == 1


def test_parallel_install_of_same_equipment_keeps_single_link(app, seed):
    flask_app = make_app()
    flask_app.config["PROPAGATE_EXCEPTIONS"] = False

    def worker(i):
        c = flask_app.test_client()
        login(c, "admin")
        machine = seed["machine"] if i % 2 == 0 else seed["machine2"]
        c.post(f"/machines/{machine}/install_equipment", data={"equipment_id": seed["equipment"]})

    assert not _parallel(worker, 8)
    assert active_eq_links(seed["equipment"]) == 1


def test_parallel_writes_do_not_lose_data(app, seed):
    flask_app = make_app()
    flask_app.config["PROPAGATE_EXCEPTIONS"] = False
    codes = []

    def worker(i):
        c = flask_app.test_client()
        login(c, "editor")
        for j in range(10):
            codes.append(c.post("/equipment/add", data={"name": "Параллельный", "serial_number": f"P-{i}-{j}", "status_id": "1"}).status_code)

    assert not _parallel(worker, 10)
    assert all(code == 302 for code in codes), set(codes)
    assert db_scalar("SELECT COUNT(*) FROM equipment WHERE name = 'Параллельный'") == 100
    assert db_query("PRAGMA integrity_check")[0][0] == "ok"
