"""Маршруты: журнал смены (бывшая программа «Журнал инженеров по АСУ»)."""
from datetime import date
from math import ceil

from flask import Response, flash, redirect, render_template, request, url_for
from flask_login import current_user

from .. import shift_journal as sj
from ..auth import require_admin
from ..db import fetch_all, fetch_one, get_db, get_setting, log_operation, set_setting
from ..utils import to_int

WRITE_ROLES = ("admin", "editor", "maintenance")
SEARCH_PER_PAGE = 50


def _can_write():
    return current_user.is_authenticated and getattr(current_user, "role", None) in WRITE_ROLES


def _user_name(row):
    name = f"{row['surname'] or ''} {row['first_name'] or ''}".strip()
    return name or row["username"]


def _selected_shift():
    """Смена и дата из запроса; по умолчанию — текущая. Будущее не показываем."""
    cur_shift, cur_date = sj.current_shift()
    shift = sj.parse_shift(request.values.get("shift")) or cur_shift
    shift_date = sj.parse_date(request.values.get("date")) or cur_date
    if shift_date > cur_date or (shift_date == cur_date and shift == "night" and cur_shift == "day"):
        shift, shift_date = cur_shift, cur_date
    return shift, shift_date


def _back(shift, shift_date):
    return redirect(url_for("journal", date=shift_date, shift=shift))


def _check_current_shift():
    """Изменения разрешены только в текущей смене. Возвращает (смена, дата) или (None, None) с сообщением."""
    if not _can_write():
        flash("Недостаточно прав для изменения журнала.", "error")
        return None, None
    return sj.current_shift()


def register(app):
    @app.route("/journal")
    def journal():
        shift, shift_date = _selected_shift()
        cur_shift, cur_date = sj.current_shift()
        engineers, entries = sj.load_shift(get_db(), shift, shift_date)
        on_shift = {e["name"] for e in engineers}
        users = [
            {"id": r["id"], "name": _user_name(r)}
            for r in fetch_all("SELECT id, username, surname, first_name FROM users ORDER BY surname, first_name, username")
        ]
        users = [u for u in users if u["name"] not in on_shift]
        editable = _can_write() and (shift, shift_date) == (cur_shift, cur_date)
        return render_template(
            "journal.html",
            shift=shift,
            shift_date=shift_date,
            shift_labels=sj.SHIFT_LABELS,
            title=sj.title_for(shift, shift_date),
            is_current=(shift, shift_date) == (cur_shift, cur_date),
            current_shift=cur_shift,
            current_date=cur_date,
            editable=editable,
            can_write=_can_write(),
            engineers=engineers,
            entries=entries,
            users=users,
            now_time=sj.now().strftime("%H:%M"),
            mail_html=sj.mail_html(shift, shift_date, engineers, entries),
            mail_to=get_setting("journal_mail_to", ""),
            mail_cc=get_setting("journal_mail_cc", ""),
        )

    @app.route("/journal/entries/add", methods=["POST"])
    def journal_entry_add():
        shift, shift_date = _check_current_shift()
        if shift is None:
            return redirect(url_for("journal"))
        entry_time = sj.normalize_time(request.form.get("entry_time"))
        content = (request.form.get("content") or "").strip()
        note = (request.form.get("note") or "").strip()
        if sj.parse_date(request.form.get("date")) not in (None, shift_date) or sj.parse_shift(request.form.get("shift")) not in (None, shift):
            flash("Запись можно добавлять только в текущую смену. Страница обновлена на текущую смену.", "error")
        elif not entry_time:
            flash("Укажите время в формате ЧЧ:ММ.", "error")
        elif not content:
            flash("Заполните содержание записи.", "error")
        else:
            db = get_db()
            cur = db.execute(
                """
                INSERT INTO journal_entries (shift_date, shift, entry_time, content, note, created_by_user_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (shift_date, shift, entry_time, content, note, current_user.id),
            )
            db.commit()
            log_operation("create", "journal_entry", cur.lastrowid, f"Журнал смены {sj.title_for(shift, shift_date)}, {entry_time}: {content[:200]}")
            flash("Запись добавлена.", "success")
        return _back(shift, shift_date)

    def _entry_for_change(entry_id):
        """Запись текущей смены, которую можно менять, либо (None, redirect)."""
        entry = fetch_one("SELECT * FROM journal_entries WHERE id = ?", (entry_id,))
        if entry is None:
            flash("Запись не найдена.", "error")
            return None, redirect(url_for("journal"))
        entry = dict(entry)
        if not _can_write():
            flash("Недостаточно прав для изменения журнала.", "error")
            return None, _back(entry["shift"], entry["shift_date"])
        if not sj.is_current(entry["shift"], entry["shift_date"]):
            flash("Изменять можно только записи текущей смены. Прошлые смены доступны только для просмотра.", "error")
            return None, _back(entry["shift"], entry["shift_date"])
        return entry, None

    @app.route("/journal/entries/<int:entry_id>/edit", methods=["GET", "POST"])
    def journal_entry_edit(entry_id):
        entry, resp = _entry_for_change(entry_id)
        if resp is not None:
            return resp
        if request.method == "POST":
            entry_time = sj.normalize_time(request.form.get("entry_time"))
            content = (request.form.get("content") or "").strip()
            note = (request.form.get("note") or "").strip()
            if not entry_time:
                flash("Укажите время в формате ЧЧ:ММ.", "error")
            elif not content:
                flash("Заполните содержание записи.", "error")
            else:
                db = get_db()
                db.execute(
                    """
                    UPDATE journal_entries SET entry_time = ?, content = ?, note = ?,
                        updated_at = datetime('now', 'localtime')
                    WHERE id = ?
                    """,
                    (entry_time, content, note, entry_id),
                )
                db.commit()
                log_operation("edit", "journal_entry", entry_id, f"Журнал смены {sj.title_for(entry['shift'], entry['shift_date'])}, {entry_time}: {content[:200]}")
                flash("Запись изменена.", "success")
                return _back(entry["shift"], entry["shift_date"])
            entry.update(entry_time=request.form.get("entry_time") or "", content=content, note=note)
        return render_template(
            "journal_entry_edit.html",
            entry=entry,
            title=sj.title_for(entry["shift"], entry["shift_date"]),
        )

    @app.route("/journal/entries/<int:entry_id>/delete", methods=["POST"])
    def journal_entry_delete(entry_id):
        entry, resp = _entry_for_change(entry_id)
        if resp is not None:
            return resp
        db = get_db()
        db.execute("DELETE FROM journal_entries WHERE id = ?", (entry_id,))
        db.commit()
        log_operation("delete", "journal_entry", entry_id, f"Журнал смены {sj.title_for(entry['shift'], entry['shift_date'])}, {entry['entry_time']}: {(entry['content'] or '')[:200]}")
        flash("Запись удалена.", "success")
        return _back(entry["shift"], entry["shift_date"])

    @app.route("/journal/engineers/add", methods=["POST"])
    def journal_engineer_add():
        shift, shift_date = _check_current_shift()
        if shift is None:
            return redirect(url_for("journal"))
        user_id = to_int(request.form.get("user_id"))
        name = (request.form.get("name") or "").strip()
        if user_id:
            row = fetch_one("SELECT id, username, surname, first_name FROM users WHERE id = ?", (user_id,))
            if row is None:
                flash("Пользователь не найден.", "error")
                return _back(shift, shift_date)
            name = _user_name(row)
        if not name:
            flash("Выберите инженера из списка или впишите ФИО.", "error")
            return _back(shift, shift_date)
        db = get_db()
        cur = db.execute(
            "INSERT OR IGNORE INTO journal_shift_engineers (shift_date, shift, name, user_id) VALUES (?, ?, ?, ?)",
            (shift_date, shift, name[:255], user_id),
        )
        db.commit()
        if cur.rowcount:
            log_operation("create", "journal_engineer", cur.lastrowid, f"На смену {sj.title_for(shift, shift_date)} добавлен инженер: {name}")
            flash(f"{name} добавлен(а) на смену.", "success")
        else:
            flash(f"{name} уже на смене.", "error")
        return _back(shift, shift_date)

    @app.route("/journal/engineers/<int:row_id>/remove", methods=["POST"])
    def journal_engineer_remove(row_id):
        row = fetch_one("SELECT * FROM journal_shift_engineers WHERE id = ?", (row_id,))
        if row is None:
            flash("Инженер не найден на смене.", "error")
            return redirect(url_for("journal"))
        if not _can_write():
            flash("Недостаточно прав для изменения журнала.", "error")
            return _back(row["shift"], row["shift_date"])
        if not sj.is_current(row["shift"], row["shift_date"]):
            flash("Состав можно менять только в текущей смене.", "error")
            return _back(row["shift"], row["shift_date"])
        db = get_db()
        db.execute("DELETE FROM journal_shift_engineers WHERE id = ?", (row_id,))
        db.commit()
        log_operation("delete", "journal_engineer", row_id, f"Со смены {sj.title_for(row['shift'], row['shift_date'])} убран инженер: {row['name']}")
        flash(f"{row['name']} убран(а) со смены.", "success")
        return _back(row["shift"], row["shift_date"])

    @app.route("/journal/export.docx")
    def journal_export_docx():
        shift, shift_date = _selected_shift()
        engineers, entries = sj.load_shift(get_db(), shift, shift_date)
        data = sj.build_docx(shift, shift_date, engineers, entries)
        filename = f"Журнал_{sj.SHIFT_LABELS[shift]}_{sj.format_date_ru(shift_date)}.docx".replace(" ", "_")
        return _download(data, filename, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")

    @app.route("/journal/mail.eml")
    def journal_mail_eml():
        shift, shift_date = _selected_shift()
        engineers, entries = sj.load_shift(get_db(), shift, shift_date)
        data = sj.build_eml(
            shift, shift_date, engineers, entries,
            to=get_setting("journal_mail_to", ""),
            cc=get_setting("journal_mail_cc", ""),
        )
        filename = f"Сводка_{sj.SHIFT_LABELS[shift]}_{sj.format_date_ru(shift_date)}.eml".replace(" ", "_")
        return _download(data, filename, "message/rfc822")

    @app.route("/journal/search")
    def journal_search():
        q = (request.args.get("q") or "").strip()
        note_q = (request.args.get("note") or "").strip()
        date_from = sj.parse_date(request.args.get("date_from")) or ""
        date_to = sj.parse_date(request.args.get("date_to")) or ""
        shift = sj.parse_shift(request.args.get("shift")) or ""
        page = max(1, to_int(request.args.get("page")) or 1)
        where, params = ["1=1"], []
        if q:
            where.append("CASEFOLD(e.content) LIKE ?")
            params.append(f"%{q.casefold()}%")
        if note_q:
            where.append("CASEFOLD(e.note) LIKE ?")
            params.append(f"%{note_q.casefold()}%")
        if date_from:
            where.append("e.shift_date >= ?")
            params.append(date_from)
        if date_to:
            where.append("e.shift_date <= ?")
            params.append(date_to)
        if shift:
            where.append("e.shift = ?")
            params.append(shift)
        where_sql = " AND ".join(where)
        searched = bool(q or note_q or date_from or date_to or shift)
        total, rows = 0, []
        if searched:
            total = fetch_one(f"SELECT COUNT(*) FROM journal_entries e WHERE {where_sql}", params)[0]
            rows = [
                dict(r)
                for r in fetch_all(
                    f"""
                    SELECT e.* FROM journal_entries e WHERE {where_sql}
                    ORDER BY e.shift_date DESC, CASE e.shift WHEN 'night' THEN 1 ELSE 0 END DESC,
                        {sj.entry_order_sql('e')}
                    LIMIT ? OFFSET ?
                    """,
                    params + [SEARCH_PER_PAGE, (page - 1) * SEARCH_PER_PAGE],
                )
            ]
        return render_template(
            "journal_search.html",
            q=q,
            note_q=note_q,
            date_from=date_from,
            date_to=date_to,
            shift=shift,
            shift_labels=sj.SHIFT_LABELS,
            rows=rows,
            total=total,
            page=page,
            total_pages=max(1, ceil(total / SEARCH_PER_PAGE)),
            searched=searched,
            today=date.today().isoformat(),
        )

    @app.route("/journal/settings", methods=["GET", "POST"])
    def journal_settings():
        r = require_admin()
        if r is not None:
            return r
        if request.method == "POST":
            set_setting("journal_mail_to", (request.form.get("mail_to") or "").strip()[:1000])
            set_setting("journal_mail_cc", (request.form.get("mail_cc") or "").strip()[:1000])
            flash("Адресаты письма сохранены.", "success")
            return redirect(url_for("journal_settings"))
        return render_template(
            "journal_settings.html",
            mail_to=get_setting("journal_mail_to", ""),
            mail_cc=get_setting("journal_mail_cc", ""),
        )


def _download(data, filename, mimetype):
    from urllib.parse import quote

    resp = Response(data, mimetype=mimetype)
    resp.headers["Content-Disposition"] = f"attachment; filename=\"journal{filename[filename.rfind('.'):]}\"; filename*=UTF-8''{quote(filename)}"
    return resp
