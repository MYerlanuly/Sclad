"""Раздел «Отчёты»: показатели из базы, выводы и проблемы по правилам, период, выгрузки."""
from datetime import date, timedelta
from io import BytesIO

import pytest
from openpyxl import load_workbook

from conftest import db_exec
from sclad.report_analytics import REASON_OTHER, build_report, reason_group


@pytest.mark.parametrize(
    "text, group",
    [
        ("Отсутствует транспорт.", "Нет транспорта"),
        ("нет автотранспорта", "Нет транспорта"),
        ("Занятость в другом направлении", "Занятость на других работах"),
        ("заняты другой работой", "Занятость на других работах"),
        ("А/С находился в работе.", "Техника в работе"),
        ("ТО отменено.", "ТО отменено или перенесено"),
        ("Непогода", "Погодные условия"),
        ("", "Причина не указана"),
        ("не выполнено", "Причина не указана"),
        ("что-то совсем иное", REASON_OTHER),
    ],
)
def test_reason_group(text, group):
    assert reason_group(text) == group


def _task(seed, day, status, reason=None):
    db_exec(
        "INSERT INTO shift_tasks (created_by_user_id, for_shift, effective_date, title, status, task_type_id, not_done_reason) "
        "VALUES (1, 'day', ?, 'Задание', ?, ?, ?)",
        (day, status, seed["task_type"], reason),
    )


def test_build_report_counts_real_data(app, seed):
    today = date.today()
    d = today.isoformat()
    _task(seed, d, "done")
    _task(seed, d, "not_done", "Нет транспорта")
    _task(seed, d, "not_done", "Отсутствует транспорт.")
    _task(seed, (today - timedelta(days=3)).isoformat(), "pending")
    # ТО: машина 1 просрочена, машина 2 без записей
    db_exec("INSERT INTO machine_maintenance (machine_id, performed_at, next_due_at) VALUES (?, ?, ?)",
            (seed["machine"], (today - timedelta(days=100)).isoformat(), (today - timedelta(days=10)).isoformat()))
    with app.app_context():
        r = build_report(30)
    assert r["tasks"]["done"] == 1 and r["tasks"]["not_done"] == 2 and r["tasks"]["rate"] == 33
    assert r["tasks_overdue"] == 1
    assert r["reasons"][0] == {"name": "Нет транспорта", "cnt": 2, "pct": 100}
    assert r["maintenance"]["status"]["overdue"] == 1
    assert r["maintenance"]["status"]["no_data"] == 1
    assert r["equipment"]["total"] == 3 and r["equipment"]["warehouse"] == 3
    titles = [p["title"] for p in r["problems"]]
    assert "Просроченное ТО" in titles and "Задания без отметки" in titles
    assert r["actions"][0]["priority"] == "high"
    assert len(r["trend"]["labels"]) == 12


def test_empty_database_has_no_invented_findings(app):
    with app.app_context():
        r = build_report(30)
    assert r["findings"] == []
    assert r["problems"] == []
    assert r["tasks"]["rate"] is None


@pytest.mark.parametrize("days", ["7", "30", "90", "365", "abc", "-1", "100000"])
def test_reports_page_periods(admin, seed, days):
    resp = admin.get("/reports", query_string={"days": days})
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Ключевые показатели" in html and "Обнаруженные проблемы" in html


def test_reports_page_shows_problem_links(admin, seed):
    db_exec("INSERT INTO machine_maintenance (machine_id, performed_at, next_due_at) VALUES (?, '2020-01-01', '2020-04-01')",
            (seed["machine"],))
    html = admin.get("/reports").get_data(as_text=True)
    assert "Просроченное ТО" in html
    assert "/maintenance?status=overdue" in html


def test_excel_export_has_styled_header(admin, seed):
    resp = admin.get("/export/excel")
    assert resp.status_code == 200
    wb = load_workbook(BytesIO(resp.data))
    ws = wb["Оборудование"]
    assert ws.freeze_panes == "A2"
    assert ws["A1"].font.bold
