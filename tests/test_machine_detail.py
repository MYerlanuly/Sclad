"""Карточка борта: статус ТО, датчики по колёсам, история установок, задания, права."""
from conftest import db_exec


def _install(machine, equipment, position=None, installed="2026-01-10 10:00:00", removed=None):
    return db_exec(
        "INSERT INTO machine_equipment (machine_id, equipment_id, installed_at, sensor_position_id, removed_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (machine, equipment, installed, position, removed),
    )


def test_machine_card_overview(admin, seed):
    m = seed["machine"]
    _install(m, seed["sensor"], position=1)  # ПЛ
    _install(m, seed["equipment"])
    _install(m, seed["sensor2"], position=4, installed="2025-05-01 09:00:00", removed="2025-10-01 09:00:00")
    db_exec("INSERT INTO machine_sim_cards (machine_id, sim_card_id) VALUES (?, ?)", (m, seed["sim"]))
    db_exec(
        "INSERT INTO machine_maintenance (machine_id, performed_at, next_due_at, performed_by) "
        "VALUES (?, date('now', 'localtime', '-95 days'), date('now', 'localtime', '-5 days'), 'Иванов И.')",
        (m,),
    )
    db_exec(
        "INSERT INTO shift_tasks (created_by_user_id, for_shift, effective_date, title, status, machine_id) "
        "VALUES (1, 'day', date('now'), 'Проверить терминал', 'pending', ?)",
        (m,),
    )
    db_exec(
        "INSERT INTO shift_tasks (created_by_user_id, for_shift, effective_date, title, status, machine_id) "
        "VALUES (1, 'day', date('now'), 'Чужое задание', 'pending', ?)",
        (seed["machine2"],),
    )

    html = admin.get(f"/machines/{m}").get_data(as_text=True)
    assert "ТО просрочено на 5 дн." in html
    assert "Иванов И." in html
    # Датчики колёс: 1 из 6, пустые позиции — ссылка на установку именно на эту позицию
    assert "Датчики колёс · 1 из 6" in html
    assert f"/machines/{m}/install_equipment?position=2" in html
    assert f"/machines/{m}/install_equipment?position=1" not in html
    # Прочее оборудование и SIM
    assert "SN-1" in html and "+77001112233" in html
    # История: установка и демонтаж второго датчика
    assert "Снято:" in html and "DS-2" in html
    # Задания только этого борта, кнопка «+ Задание» с выбранным бортом
    assert "Проверить терминал" in html and "Чужое задание" not in html
    assert f"/shift_tasks?machine={m}#add" in html
    assert html.count('class="row-del"') == 3  # датчик, терминал, SIM


def test_machine_card_reader_has_no_actions(client_for, seed):
    _install(seed["machine"], seed["sensor"], position=1)
    html = client_for("reader").get(f"/machines/{seed['machine']}").get_data(as_text=True)
    assert 'class="row-del"' not in html
    assert "Записать ТО" not in html
    assert "install_equipment" not in html
    assert "ТО не проводилось" in html


def test_install_equipment_position_preselect(admin, seed):
    html = admin.get(f"/machines/{seed['machine']}/install_equipment?position=3").get_data(as_text=True)
    assert "на позицию ЗЛН" in html
    assert 'value="3" selected' in html
    assert "SN-1" not in html  # не датчик — скрыт
    assert "DS-1" in html


def test_shift_task_form_prefills_machine(admin, seed):
    html = admin.get(f"/shift_tasks?machine={seed['machine']}").get_data(as_text=True)
    assert f'name="machine_id" value="{seed["machine"]}"' in html
    assert 'value="INV-001 — БелАЗ-75131 (Самосвал)"' in html
