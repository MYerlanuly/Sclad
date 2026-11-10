"""Журнал смены, перенесённый из программы «Журнал инженеров по АСУ»."""
import email
import io
import zipfile
from datetime import datetime
from email.policy import default as default_policy

import pytest

from conftest import db_connect, db_exec, db_query, db_scalar
from sclad import shift_journal as sj
from sclad.journal_import import import_journal

DAY_NOW = datetime(2026, 11, 10, 10, 15)  # 1-я смена 10.11
NIGHT_AFTER_MIDNIGHT = datetime(2026, 11, 11, 2, 0)  # 2-я смена 10.11


@pytest.fixture
def clock(monkeypatch):
    """clock(datetime) — подменить текущее время для правил смен."""

    def _set(moment):
        monkeypatch.setattr(sj, "now", lambda: moment)

    _set(DAY_NOW)
    return _set


def _add(client, time_="09:00", content="Проверка связи", note="", **extra):
    data = {"entry_time": time_, "content": content, "note": note}
    data.update(extra)
    return client.post("/journal/entries/add", data=data)


# ---------- правила смен ----------

@pytest.mark.parametrize(
    "moment, expected",
    [
        (datetime(2026, 11, 10, 8, 29), ("night", "2026-11-09")),
        (datetime(2026, 11, 10, 8, 30), ("day", "2026-11-10")),
        (datetime(2026, 11, 10, 20, 29), ("day", "2026-11-10")),
        (datetime(2026, 11, 10, 20, 30), ("night", "2026-11-10")),
        (datetime(2026, 11, 11, 0, 5), ("night", "2026-11-10")),
    ],
)
def test_current_shift_boundaries(moment, expected):
    assert sj.current_shift(moment) == expected


def test_normalize_time():
    assert sj.normalize_time("8:05") == "08:05"
    assert sj.normalize_time("23:59") == "23:59"
    assert sj.normalize_time("24:00") is None
    assert sj.normalize_time("abc") is None


# ---------- записи ----------

def test_page_opens_current_shift(admin, clock):
    resp = admin.get("/journal")
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "1-я смена, 10.11.2026" in html and "Текущая смена" in html


def test_add_edit_delete_in_current_shift(client_for, clock):
    c = client_for("maintenance")
    assert _add(c, "9:40", "Перезагрузка сервера", "по заявке").status_code == 302
    row = db_query("SELECT * FROM journal_entries")[0]
    assert (row["shift_date"], row["shift"], row["entry_time"], row["note"]) == ("2026-11-10", "day", "09:40", "по заявке")

    c.post(f"/journal/entries/{row['id']}/edit", data={"entry_time": "09:45", "content": "Перезагрузка", "note": ""})
    assert db_scalar("SELECT entry_time || content FROM journal_entries") == "09:45Перезагрузка"

    c.post(f"/journal/entries/{row['id']}/delete")
    assert db_scalar("SELECT COUNT(*) FROM journal_entries") == 0
    ops = [r["operation_type"] for r in db_query("SELECT operation_type FROM operations_log WHERE entity_type = 'journal_entry'")]
    assert ops == ["create", "edit", "delete"]


def test_reader_can_view_but_not_add(client_for, clock):
    c = client_for("reader")
    assert c.get("/journal").status_code == 200
    _add(c)
    assert db_scalar("SELECT COUNT(*) FROM journal_entries") == 0


def test_validation(admin, clock):
    _add(admin, time_="25:00")
    _add(admin, content="   ")
    assert db_scalar("SELECT COUNT(*) FROM journal_entries") == 0


def test_past_shift_is_read_only(admin, clock):
    old = db_exec(
        "INSERT INTO journal_entries (shift_date, shift, entry_time, content) VALUES ('2026-11-09', 'night', '23:00', 'старое')"
    )
    html = admin.get("/journal?date=2026-11-09&shift=night").get_data(as_text=True)
    assert "только просмотр" in html and "Новая запись" not in html

    admin.post(f"/journal/entries/{old}/edit", data={"entry_time": "23:00", "content": "подмена", "note": ""})
    admin.post(f"/journal/entries/{old}/delete")
    assert db_scalar("SELECT content FROM journal_entries WHERE id = ?", (old,)) == "старое"
    assert admin.get(f"/journal/entries/{old}/edit").status_code == 302


def test_add_from_stale_page_after_shift_change_is_rejected(admin, clock):
    # Страница открыта в 1-й смене, а запись отправлена уже во 2-й
    clock(datetime(2026, 11, 10, 21, 0))
    _add(admin, date="2026-11-10", shift="day")
    assert db_scalar("SELECT COUNT(*) FROM journal_entries") == 0
    _add(admin, date="2026-11-10", shift="night")
    assert db_scalar("SELECT shift FROM journal_entries") == "night"


def test_future_shift_not_shown(admin, clock):
    html = admin.get("/journal?date=2026-11-12&shift=day").get_data(as_text=True)
    assert "1-я смена, 10.11.2026" in html
    html = admin.get("/journal?date=2026-11-10&shift=night").get_data(as_text=True)
    assert "1-я смена, 10.11.2026" in html


def test_night_entries_after_midnight_go_last(admin, clock):
    clock(NIGHT_AFTER_MIDNIGHT)
    for t in ("01:30", "21:00", "07:59", "23:10"):
        _add(admin, time_=t, content=f"запись {t}")
    conn = db_connect()
    try:
        _, entries = sj.load_shift(conn, "night", "2026-11-10")
    finally:
        conn.close()
    assert [e["entry_time"] for e in entries] == ["21:00", "23:10", "01:30", "07:59"]


# ---------- инженеры на смене ----------

def test_engineers_add_and_remove(admin, clock):
    user_id = db_scalar("SELECT id FROM users WHERE username = 'editor'")
    db_exec("UPDATE users SET surname = 'Иванов', first_name = 'Пётр' WHERE id = ?", (user_id,))
    admin.post("/journal/engineers/add", data={"user_id": user_id})
    admin.post("/journal/engineers/add", data={"user_id": user_id})  # повтор не дублирует
    admin.post("/journal/engineers/add", data={"name": "Сидоров А.А."})
    names = [r["name"] for r in db_query("SELECT name FROM journal_shift_engineers ORDER BY name")]
    assert names == ["Иванов Пётр", "Сидоров А.А."]

    row_id = db_scalar("SELECT id FROM journal_shift_engineers WHERE name = 'Сидоров А.А.'")
    admin.post(f"/journal/engineers/{row_id}/remove")
    assert db_scalar("SELECT COUNT(*) FROM journal_shift_engineers") == 1


def test_engineers_of_past_shift_cannot_be_removed(admin, clock):
    row_id = db_exec(
        "INSERT INTO journal_shift_engineers (shift_date, shift, name) VALUES ('2026-11-09', 'day', 'Петров')"
    )
    admin.post(f"/journal/engineers/{row_id}/remove")
    assert db_scalar("SELECT COUNT(*) FROM journal_shift_engineers") == 1


# ---------- выгрузки ----------

def _seed_shift():
    db_exec("INSERT INTO journal_shift_engineers (shift_date, shift, name) VALUES ('2026-11-10', 'day', 'Иванов П.')")
    db_exec(
        "INSERT INTO journal_entries (shift_date, shift, entry_time, content, note) VALUES ('2026-11-10', 'day', '09:00', 'Связь <восстановлена>', 'ок')"
    )


def test_word_export(admin, clock):
    _seed_shift()
    resp = admin.get("/journal/export.docx?date=2026-11-10&shift=day")
    assert resp.status_code == 200
    assert "attachment" in resp.headers["Content-Disposition"]
    with zipfile.ZipFile(io.BytesIO(resp.data)) as z:
        assert z.testzip() is None
        xml = z.read("word/document.xml").decode("utf-8")
    assert "Иванов П." in xml and "Связь &lt;восстановлена&gt;" in xml and "1-я смена" in xml


def test_outlook_mail(admin, clock):
    _seed_shift()
    resp = admin.get("/journal/mail.eml?date=2026-11-10&shift=day")
    assert resp.status_code == 200
    msg = email.message_from_bytes(resp.data, policy=default_policy)
    assert msg["X-Unsent"] == "1"
    assert msg["Subject"] == "Сводка - 1-я смена от 10-11-2026"
    assert msg["To"] == "v.ustimenko@bogatyr.kz" and msg["Cc"] == "asutp01@bogatyr.kz"
    html = msg.get_body(("html",)).get_content()
    assert "<li>Иванов П.</li>" in html and "Связь &lt;восстановлена&gt;" in html


def test_mail_recipients_admin_only(client_for, admin, clock):
    editor = client_for("editor")
    editor.post("/journal/settings", data={"mail_to": "x@example.kz", "mail_cc": ""})
    assert db_scalar("SELECT value FROM app_settings WHERE key = 'journal_mail_to'") == "v.ustimenko@bogatyr.kz"
    admin.post("/journal/settings", data={"mail_to": "boss@example.kz", "mail_cc": ""})
    assert db_scalar("SELECT value FROM app_settings WHERE key = 'journal_mail_to'") == "boss@example.kz"
    msg = email.message_from_bytes(admin.get("/journal/mail.eml").data, policy=default_policy)
    assert msg["To"] == "boss@example.kz" and msg["Cc"] is None


# ---------- поиск ----------

def test_search_any_case_and_filters(client_for, clock):
    db_exec("INSERT INTO journal_entries (shift_date, shift, entry_time, content, note) VALUES ('2026-10-01', 'day', '09:00', 'Замена Антенны', 'склад')")
    db_exec("INSERT INTO journal_entries (shift_date, shift, entry_time, content, note) VALUES ('2026-11-01', 'night', '22:00', 'антенна ГНСС', '')")
    db_exec("INSERT INTO journal_entries (shift_date, shift, entry_time, content, note) VALUES ('2026-11-02', 'day', '10:00', 'другое', '')")
    c = client_for("reader")
    html = c.get("/journal/search?q=АНТЕНН").get_data(as_text=True)
    assert "Найдено записей: 2" in html
    html = c.get("/journal/search?q=антенн&date_from=2026-10-15").get_data(as_text=True)
    assert "Найдено записей: 1" in html and "ГНСС" in html
    html = c.get("/journal/search?note=СКЛАД&shift=day").get_data(as_text=True)
    assert "Найдено записей: 1" in html


# ---------- перенос старых данных ----------

OLD_JOURNAL = [
    {"date": "2024-05-01", "shift": "1-я смена", "time": "08:45:00", "content": "Приём смены", "note": None},
    {"date": "2024-05-01", "shift": "2-я смена", "time": "02:10:00", "content": "Авария связи", "note": "устранено"},
    {"date": "", "shift": "1-я смена", "time": "09:00:00", "content": "битая строка", "note": ""},
]
OLD_ENGINEERS = [
    {"date": "2024-05-01", "shift": "1-я смена", "name": "Иванов Пётр Сергеевич"},
    {"date": "2024-05-01", "shift": "1-я смена", "name": "Иванов Пётр Сергеевич"},
]
OLD_INFO = [
    {"full_name": "Иванов Пётр Сергеевич", "tab_number": "1234"},
    {"full_name": "Неизвестный Инженер", "tab_number": "9999"},
]


def test_import_rows_is_idempotent(app):
    user_id = db_scalar("SELECT id FROM users WHERE username = 'editor'")
    db_exec("UPDATE users SET surname = 'Иванов', first_name = 'Пётр' WHERE id = ?", (user_id,))
    conn = db_connect()
    try:
        report = import_journal(conn, OLD_JOURNAL, OLD_ENGINEERS, OLD_INFO)
        conn.commit()
        again = import_journal(conn, OLD_JOURNAL, OLD_ENGINEERS, OLD_INFO)
        conn.commit()
    finally:
        conn.close()
    assert (report["entries_added"], report["entries_bad"], report["engineers_added"]) == (2, 1, 1)
    assert report["personnel_numbers_set"] == 1
    assert report["unmatched_engineers"] == ["Неизвестный Инженер"]
    assert again["entries_added"] == 0 and again["entries_skipped"] == 2
    rows = db_query("SELECT shift, entry_time, note FROM journal_entries ORDER BY id")
    assert [tuple(r) for r in rows] == [("day", "08:45", ""), ("night", "02:10", "устранено")]
    assert db_scalar("SELECT personnel_number FROM users WHERE id = ?", (user_id,)) == "1234"


def test_import_cli_from_csv(app, tmp_path):
    (tmp_path / "journal.csv").write_text(
        "id;date;shift;time;content;note\n1;2024-05-01;1-я смена;08:45:00;Приём смены;\n", encoding="utf-8"
    )
    runner = app.test_cli_runner()
    result = runner.invoke(args=["import-journal", "--csv-dir", str(tmp_path), "--dry-run"])
    assert result.exit_code == 0, result.output
    assert db_scalar("SELECT COUNT(*) FROM journal_entries") == 0
    result = runner.invoke(args=["import-journal", "--csv-dir", str(tmp_path)])
    assert "записей добавлено: 1" in result.output
    assert db_scalar("SELECT content FROM journal_entries") == "Приём смены"
