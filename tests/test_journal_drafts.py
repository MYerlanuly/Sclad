"""Журнал смены: черновик «Выполнено за смену» из заданий, замен, установок и ТО."""
import re
from datetime import datetime

import pytest

from conftest import db_exec, db_query, db_scalar
from sclad import shift_journal as sj
from sclad.journal_drafts import shift_window

DAY_NOW = datetime(2026, 11, 10, 15, 0)       # 1-я смена 10.11
NIGHT_NOW = datetime(2026, 11, 11, 3, 0)      # 2-я смена 10.11 (после полуночи)


@pytest.fixture
def clock(monkeypatch):
    def _set(moment):
        monkeypatch.setattr(sj, "now", lambda: moment)
    _set(DAY_NOW)
    return _set


def _link(machine, equipment, installed, removed=None, position=None, reason=None, comment=None):
    return db_exec(
        "INSERT INTO machine_equipment (machine_id, equipment_id, installed_at, removed_at, sensor_position_id, "
        "removal_reason, removal_comment) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (machine, equipment, installed, removed, position, reason, comment),
    )


def _done_task(title, machine, completed_at, user_id=1, comment=None):
    t = db_exec(
        "INSERT INTO shift_tasks (created_by_user_id, for_shift, effective_date, title, status, machine_id, completed_at, "
        "completed_by_user_id) VALUES (1, 'day', '2026-11-10', ?, 'done', ?, ?, ?)",
        (title, machine, completed_at, user_id),
    )
    db_exec(
        "INSERT INTO shift_task_completions (shift_task_id, user_id, completed_at, comment) VALUES (?, ?, ?, ?)",
        (t, user_id, completed_at, comment),
    )
    return t


def test_shift_window():
    assert shift_window("day", "2026-11-10") == ("2026-11-10 08:30:00", "2026-11-10 20:30:00")
    assert shift_window("night", "2026-11-10") == ("2026-11-10 20:30:00", "2026-11-11 08:30:00")


def _seed_shift(seed):
    db_exec("UPDATE users SET surname = 'Иванов', first_name = 'Пётр' WHERE username = 'maintenance'")
    uid = db_scalar("SELECT id FROM users WHERE username = 'maintenance'")
    # Замена на ЗЛН в 11:05 (снятие и установка в одну секунду)
    _link(seed["machine"], seed["sensor"], "2026-10-01 10:00:00", "2026-11-10 11:05:00", 3, "faulty", "нет данных")
    _link(seed["machine"], seed["sensor2"], "2026-11-10 11:05:00", None, 3)
    # Просто снят терминал в 12:00 и установлен на другой борт в 13:00
    _link(seed["machine"], seed["equipment"], "2026-10-01 10:00:00", "2026-11-10 12:00:00", None, "to_stock")
    _link(seed["machine2"], seed["equipment"], "2026-11-10 13:00:00")
    # SIM поставили в 14:00
    db_exec("INSERT INTO machine_sim_cards (machine_id, sim_card_id, installed_at) VALUES (?, ?, '2026-11-10 14:00:00')",
            (seed["machine2"], seed["sim"]))
    # Задание выполнено в 11:06 (из замены) и ещё одно в 09:40 с комментарием
    _done_task("Б-1: не передаёт давление ЗЛН", seed["machine"], "2026-11-10 11:06:00", uid,
               "Замена на позиции ЗЛН: DS-1 → DS-2 (Неисправен: нет данных)")
    _done_task("Проверить антенну", seed["machine2"], "2026-11-10 09:40:00", uid, "Подтянул разъём")
    # Вне смены: вчера и ночью
    _link(seed["machine2"], seed["sensor"], "2026-11-09 11:00:00", "2026-11-09 12:00:00", 1, "faulty")
    _done_task("Ночное задание", seed["machine"], "2026-11-10 21:00:00", uid)


def test_journal_shows_drafts_for_current_shift(clock, client_for, seed):
    _seed_shift(seed)
    html = client_for("maintenance").get("/journal").get_data(as_text=True)
    assert "Выполнено за смену" in html and "не в журнале" in html
    body = html[html.index("Выполнено за смену"):html.index("Записи журнала")]
    assert 'value="INV-001, ЗЛН: замена Датчик давления DS-1 → DS-2"' in body
    assert 'value="Неисправен: нет данных"' in body
    assert 'value="INV-001: снят Терминал SN-1"' in body and 'value="Исправен, вернуть на склад"' in body
    assert 'value="INV-002: установлен Терминал SN-1"' in body
    assert 'value="INV-002: установлена SIM +77001112233"' in body
    assert 'value="INV-002: выполнено задание «Проверить антенну». Подтянул разъём"' in body
    # Комментарий замены не повторяется: замена уже есть отдельной строкой
    assert 'value="INV-001: выполнено задание «Б-1: не передаёт давление ЗЛН»"' in body
    assert 'value="Иванов Пётр"' in body
    assert "Ночное задание" not in body and "2026-11-09" not in body
    # По времени: 09:40 первым
    assert body.index("09:40") < body.index("11:05") < body.index("12:00") < body.index("13:00")


def test_add_drafts_to_journal(clock, client_for, seed):
    _seed_shift(seed)
    c = client_for("maintenance")
    html = c.get("/journal").get_data(as_text=True)
    keys = re.findall(r'name="pick" value="([^"]+)"', html)
    rep = next(k for k in keys if k.startswith("rep-"))
    task = next(k for k in keys if k.startswith("task-"))
    resp = c.post("/journal/drafts/add", data={
        "date": "2026-11-10", "shift": "day", "pick": [rep, task],
        f"content__{rep}": "Б-112 ЗЛН: замена датчика DS-1 на DS-2, старый не передавал давление",
        f"note__{rep}": "нет данных",
    })
    assert resp.status_code == 302
    rows = db_query("SELECT entry_time, content, note, source_key FROM journal_entries ORDER BY entry_time")
    assert [(r["entry_time"], r["source_key"]) for r in rows] == [("09:40", task), ("11:05", rep)]
    assert rows[1]["content"].startswith("Б-112 ЗЛН: замена датчика") and rows[1]["note"] == "нет данных"
    # Повторно не добавляются; в панели помечены «уже в журнале»
    c.post("/journal/drafts/add", data={"date": "2026-11-10", "shift": "day", "pick": [rep, task]})
    assert db_scalar("SELECT COUNT(*) FROM journal_entries") == 2
    html = c.get("/journal").get_data(as_text=True)
    assert html.count("уже в журнале") == 2
    assert f'name="pick" value="{rep}" disabled' in html


def test_drafts_night_shift_after_midnight(clock, client_for, seed):
    clock(NIGHT_NOW)
    _link(seed["machine"], seed["sensor"], "2026-11-11 01:15:00", None, 2)
    _link(seed["machine"], seed["sensor2"], "2026-11-10 21:10:00", None, 1)
    html = client_for("editor").get("/journal").get_data(as_text=True)
    body = html[html.index("Выполнено за смену"):html.index("Записи журнала")]
    assert "INV-001, ПЛ: установлен" in body and "INV-001, ПП: установлен" in body
    assert body.index("21:10") < body.index("01:15")
    c = client_for("editor")
    keys = re.findall(r'name="pick" value="([^"]+)"', c.get("/journal").get_data(as_text=True))
    c.post("/journal/drafts/add", data={"date": "2026-11-10", "shift": "night", "pick": keys})
    rows = db_query("SELECT shift_date, shift, entry_time FROM journal_entries ORDER BY id")
    assert {(r["shift_date"], r["shift"]) for r in rows} == {("2026-11-10", "night")}


def test_no_drafts_panel_for_reader_and_past_shift(clock, client_for, seed):
    _seed_shift(seed)
    assert "Выполнено за смену" not in client_for("reader").get("/journal").get_data(as_text=True)
    assert "Выполнено за смену" not in client_for("editor").get("/journal?date=2026-11-09&shift=day").get_data(as_text=True)
    # reader не может добавить
    client_for("reader").post("/journal/drafts/add", data={"date": "2026-11-10", "shift": "day", "pick": ["task-1"]})
    assert db_scalar("SELECT COUNT(*) FROM journal_entries") == 0


def test_drafts_add_rejects_other_shift_and_unknown_keys(clock, client_for, seed):
    _seed_shift(seed)
    c = client_for("editor")
    c.post("/journal/drafts/add", data={"date": "2026-11-09", "shift": "day", "pick": ["task-1"]})
    c.post("/journal/drafts/add", data={"date": "2026-11-10", "shift": "day", "pick": ["task-999", "bogus"]})
    assert db_scalar("SELECT COUNT(*) FROM journal_entries") == 0


def test_empty_shift_has_no_panel(clock, client_for, seed):
    assert "Выполнено за смену" not in client_for("editor").get("/journal").get_data(as_text=True)
