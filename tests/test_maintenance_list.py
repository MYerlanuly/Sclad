"""Страница «Техобслуживание»: плитки-счётчики, фильтр по статусу, поиск, «осталось дней»."""
import re

from conftest import db_exec


def _add_mnt(machine_id, performed_offset, due_offset):
    db_exec(
        "INSERT INTO machine_maintenance (machine_id, performed_at, next_due_at) "
        "VALUES (?, date('now', 'localtime', ?), date('now', 'localtime', ?))",
        (machine_id, performed_offset, due_offset),
    )


def _rows(html):
    return re.findall(r'href="/machines/\d+">([^<]+)</a>', html)


def test_maintenance_list_tiles_filters_and_days_left(admin, seed):
    m3 = db_exec("INSERT INTO machines (inventory_number, machine_model_id) VALUES ('ЭКГ-777', ?)", (seed["model"],))
    # INV-001: старая запись «норма», последняя — просрочена на 5 дней (берётся последняя по id)
    _add_mnt(seed["machine"], "-200 days", "+60 days")
    _add_mnt(seed["machine"], "-95 days", "-5 days")
    # ЭКГ-777: скоро ТО (через 3 дня); INV-002 без ТО
    _add_mnt(m3, "-87 days", "+3 days")

    html = admin.get("/maintenance").get_data(as_text=True)
    assert "maintenance-chart" not in html
    assert "просрочено на 5 дн." in html
    assert "через 3 дн." in html
    assert _rows(html) == ["INV-001", "ЭКГ-777", "INV-002"]

    html = admin.get("/maintenance?status=overdue").get_data(as_text=True)
    assert _rows(html) == ["INV-001"]
    assert "Найдено: 1" in html

    html = admin.get("/maintenance?status=no_data").get_data(as_text=True)
    assert _rows(html) == ["INV-002"]

    # Поиск без учёта регистра, в т.ч. по кириллице; плитки считают с учётом поиска
    html = admin.get("/maintenance?q=экг").get_data(as_text=True)
    assert _rows(html) == ["ЭКГ-777"]
    assert "Найдено: 1" in html

    # Неизвестный статус игнорируется
    html = admin.get("/maintenance?status=bogus").get_data(as_text=True)
    assert len(_rows(html)) == 3


def test_maintenance_history_filters(client_for, seed):
    m3 = db_exec("INSERT INTO machines (inventory_number, machine_model_id) VALUES ('ЭКГ-777', ?)", (seed["model"],))
    for mid, performed, who, remarks in (
        (seed["machine"], "2026-01-15", "Иванов Пётр", None),
        (seed["machine"], "2026-04-15", "Петров Иван, Иванов Пётр", "Заменён датчик"),
        (m3, "2026-03-01", "Петров Иван", None),
    ):
        db_exec(
            "INSERT INTO machine_maintenance (machine_id, performed_at, next_due_at, performed_by, remarks) "
            "VALUES (?, ?, date(?, '+3 months'), ?, ?)",
            (mid, performed, performed, who, remarks),
        )
    c = client_for("reader")

    html = c.get("/maintenance/history").get_data(as_text=True)
    assert "Найдено ТО: 3" in html
    # Сначала самые свежие
    assert html.index("15.04.26") < html.index("01.03.26") < html.index("15.01.26")
    assert html.count('/act"') == 3

    html = c.get("/maintenance/history?date_from=2026-02-01&date_to=2026-03-31").get_data(as_text=True)
    assert "Найдено ТО: 1" in html and "ЭКГ-777" in html

    html = c.get("/maintenance/history?engineer=иванов пётр").get_data(as_text=True)
    assert "Найдено ТО: 2" in html

    html = c.get("/maintenance/history?q=датчик").get_data(as_text=True)
    assert "Найдено ТО: 1" in html

    # Даты перепутаны местами — меняются, а не дают пустой список
    html = c.get("/maintenance/history?date_from=2026-12-31&date_to=2026-01-01").get_data(as_text=True)
    assert "Найдено ТО: 3" in html


def test_maintenance_list_extra_filters_and_sort(admin, seed):
    other_type = db_exec("INSERT INTO machine_types (name) VALUES ('Экскаватор')")
    other_model = db_exec("INSERT INTO machine_models (name, machine_type_id) VALUES ('ЭКГ-10', ?)", (other_type,))
    m3 = db_exec("INSERT INTO machines (inventory_number, machine_model_id) VALUES ('A-100', ?)", (other_model,))
    for mid, due, who in ((seed["machine"], "+40 days", "Иванов Пётр"), (m3, "+5 days", "Петров Иван")):
        db_exec(
            "INSERT INTO machine_maintenance (machine_id, performed_at, next_due_at, performed_by) "
            "VALUES (?, date('now', 'localtime', '-50 days'), date('now', 'localtime', ?), ?)",
            (mid, due, who),
        )

    assert _rows(admin.get(f"/maintenance?model={other_model}").get_data(as_text=True)) == ["A-100"]
    assert _rows(admin.get("/maintenance?engineer=иванов").get_data(as_text=True)) == ["INV-001"]
    html = admin.get("/maintenance?next_from=2000-01-01&next_to=" + "2999-01-01").get_data(as_text=True)
    assert _rows(html) == ["A-100", "INV-001"]  # без ТО не попадает в диапазон дат

    # Сортировка по инв. номеру, в обе стороны; пустые даты всегда в конце
    assert _rows(admin.get("/maintenance?sort=inv&dir=asc").get_data(as_text=True)) == ["A-100", "INV-001", "INV-002"]
    assert _rows(admin.get("/maintenance?sort=inv&dir=desc").get_data(as_text=True)) == ["INV-002", "INV-001", "A-100"]
    assert _rows(admin.get("/maintenance?sort=next&dir=desc").get_data(as_text=True)) == ["INV-001", "A-100", "INV-002"]
    # Неизвестная колонка — порядок по умолчанию; ссылки страниц сохраняют сортировку
    assert _rows(admin.get("/maintenance?sort=drop&dir=x").get_data(as_text=True)) == ["A-100", "INV-001", "INV-002"]
    html = admin.get("/maintenance?sort=inv&dir=desc&per_page=1").get_data(as_text=True)
    assert "sort=inv" in html and "dir=desc" in html


def test_maintenance_history_sort(admin, seed):
    for mid, performed in ((seed["machine2"], "2026-02-01"), (seed["machine"], "2026-03-01")):
        db_exec(
            "INSERT INTO machine_maintenance (machine_id, performed_at, next_due_at) VALUES (?, ?, date(?, '+3 months'))",
            (mid, performed, performed),
        )
    assert _rows(admin.get("/maintenance/history").get_data(as_text=True)) == ["INV-001", "INV-002"]
    assert _rows(admin.get("/maintenance/history?sort=date&dir=asc").get_data(as_text=True)) == ["INV-002", "INV-001"]
    assert _rows(admin.get("/maintenance/history?sort=inv&dir=desc").get_data(as_text=True)) == ["INV-002", "INV-001"]
