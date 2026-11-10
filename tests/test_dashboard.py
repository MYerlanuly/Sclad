"""Главная страница: текущая смена, плитки «требует внимания», ТО, операции, кнопки по ролям."""
from datetime import datetime

import pytest

from conftest import db_exec


def _set_now(monkeypatch, moment):
    from sclad import shift_journal

    monkeypatch.setattr(shift_journal, "now", lambda: moment)


def _task(effective_date, for_shift, title, status="pending", seed=None, machine_id=None):
    return db_exec(
        "INSERT INTO shift_tasks (created_by_user_id, for_shift, effective_date, title, status, task_type_id, machine_id) "
        "VALUES (1, ?, ?, ?, ?, ?, ?)",
        (for_shift, effective_date, title, status, seed["task_type"], machine_id),
    )


def test_dashboard_shows_current_night_shift_after_midnight(admin, seed, monkeypatch):
    # 02:00 11-го — это ещё 2-я смена 10-го
    _set_now(monkeypatch, datetime(2026, 11, 11, 2, 0))
    _task("2026-11-10", "night", "Ночное задание", seed=seed, machine_id=seed["machine"])
    _task("2026-11-10", "day", "Дневное не закрыто", seed=seed)
    _task("2026-11-09", "night", "Позавчерашнее не выполнено", status="not_done", seed=seed)
    _task("2026-11-09", "day", "Старое выполнено", status="done", seed=seed)
    _task("2026-11-11", "day", "Завтрашнее", seed=seed)

    html = admin.get("/").get_data(as_text=True)
    assert "2-я смена, 10.11.2026" in html
    assert "Ночное задание" in html
    assert "INV-001" in html
    assert "Дневное не закрыто" not in html
    assert "Завтрашнее" not in html
    # Не закрыто с прошлых смен: дневное 10-го и позавчерашнее
    assert '<span class="dash-tile-value tabular-nums">2</span>\n    <span class="dash-tile-label">не закрыто с прошлых смен' in html.replace("\r\n", "\n")


def test_dashboard_maintenance_and_operations(admin, seed):
    db_exec(
        "INSERT INTO machine_maintenance (machine_id, performed_at, next_due_at) "
        "VALUES (?, date('now', 'localtime', '-95 days'), date('now', 'localtime', '-5 days'))",
        (seed["machine"],),
    )
    db_exec(
        "INSERT INTO machine_maintenance (machine_id, performed_at, next_due_at) "
        "VALUES (?, date('now', 'localtime', '-85 days'), date('now', 'localtime', '+3 days'))",
        (seed["machine2"],),
    )
    db_exec(
        "INSERT INTO operations_log (operation_type, entity_type, entity_id, description, username) "
        "VALUES ('install', 'equipment', ?, 'Установлен терминал SN-1', 'admin')",
        (seed["equipment"],),
    )
    html = admin.get("/").get_data(as_text=True)
    assert "просрочено 5 дн." in html
    assert "через 3 дн." in html
    assert "Установка" in html
    assert f'href="/equipment/{seed["equipment"]}">Оборудование</a>: Установлен терминал SN-1' in html


@pytest.mark.parametrize(
    "role, sees_shift, sees_edit",
    [("admin", True, True), ("editor", True, True), ("maintenance", True, False), ("reader", False, False)],
)
def test_dashboard_actions_follow_role(client_for, seed, role, sees_shift, sees_edit):
    html = client_for(role).get("/").get_data(as_text=True)
    assert ("+</span> Записать ТО" in html) is sees_shift
    assert ("+</span> Оборудование" in html) is sees_edit
    assert ("Скачать копию базы" in html) is (role == "admin")
