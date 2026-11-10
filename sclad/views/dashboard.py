"""Маршруты: главная страница и быстрый поиск."""
from datetime import date, timedelta

from flask import flash, redirect, render_template, request, url_for
from flask_login import current_user

from .. import shift_journal as sj
from ..db import fetch_all, fetch_one, get_setting
from ..utils import _format_backup_date, fix_mojibake_text
from .maintenance import MAINTENANCE_STATUS_CTE

# Сколько строк показывать в блоках главной
SHIFT_TASKS_LIMIT = 10
MAINTENANCE_LIMIT = 8
RECENT_OPS_LIMIT = 8

ENTITY_LABELS = {
    "machine": "Техника",
    "equipment": "Оборудование",
    "sim_card": "SIM-карта",
    "sensor": "Датчик",
    "shift_task": "Задание на смену",
    "journal_entry": "Журнал смены",
    "journal_engineer": "Журнал смены",
}
OPERATION_LABELS = {
    "create": "Создание",
    "edit": "Изменение",
    "delete": "Удаление",
    "complete": "Выполнено",
    "not_done": "Не выполнено",
    "carry": "Перенос",
    "install": "Установка",
    "remove": "Демонтаж",
    "status_change": "ТО",
}
# Цвет метки операции: зелёный — установка/выполнение, красный — удаление/демонтаж и т.п.
OPERATION_TONES = {
    "install": "green",
    "complete": "green",
    "create": "blue",
    "remove": "red",
    "delete": "red",
    "not_done": "red",
    "edit": "slate",
    "carry": "amber",
    "status_change": "amber",
}
# Порядок заданий в блоке смены: сначала те, что ещё надо сделать
_TASK_STATUS_ORDER = {"pending": 0, "not_done": 1, "done": 2}


def _entity_url(entity_type, entity_id):
    if entity_type == "machine":
        return url_for("machine_detail", machine_id=entity_id)
    if entity_type == "equipment":
        return url_for("equipment_detail", equipment_id=entity_id)
    if entity_type == "sim_card":
        return url_for("sim_card_detail", sim_id=entity_id)
    return None


def _inventory_summary():
    """Все счётчики склада одним запросом."""
    row = fetch_one(
        """
        SELECT
            (SELECT COUNT(*) FROM machines) AS machines_total,
            (SELECT COUNT(DISTINCT machine_id) FROM machine_equipment WHERE removed_at IS NULL) AS machines_with_equipment,
            (SELECT COUNT(*) FROM machine_sim_cards WHERE removed_at IS NULL) AS machines_with_sim,
            (SELECT COUNT(*) FROM equipment) AS equipment_total,
            (SELECT COUNT(*) FROM equipment WHERE status_id = 1) AS equipment_warehouse,
            (SELECT COUNT(*) FROM equipment WHERE status_id = 2) AS equipment_installed,
            (SELECT COUNT(*) FROM equipment WHERE status_id = 4) AS equipment_broken,
            (SELECT COUNT(*) FROM sim_cards) AS sim_total,
            (SELECT COUNT(*) FROM sim_cards WHERE status_id = 1) AS sim_warehouse,
            (SELECT COUNT(*) FROM sim_cards WHERE status_id = 2) AS sim_installed,
            (SELECT COUNT(*) FROM sim_cards WHERE status_id = 3) AS sim_blocked
        """
    )
    return dict(row)


def _maintenance_summary():
    """Счётчики ТО по статусам и ближайшие машины, которым нужно ТО (просрочено или в 14 дней)."""
    counts = {"overdue": 0, "due_soon": 0, "ok": 0, "no_data": 0}
    for r in fetch_all(f"{MAINTENANCE_STATUS_CTE} SELECT to_status, COUNT(*) AS c FROM with_status GROUP BY to_status"):
        counts[r["to_status"]] = r["c"]
    rows = [
        dict(r)
        for r in fetch_all(
            f"""{MAINTENANCE_STATUS_CTE}
            SELECT id, inventory_number, model_name, next_due_at, days_left, to_status
            FROM with_status
            WHERE to_status IN ('overdue', 'due_soon')
            ORDER BY next_due_at, inventory_number
            LIMIT ?
            """,
            (MAINTENANCE_LIMIT,),
        )
    ]
    return counts, rows


def _shift_tasks(cur_shift, cur_date):
    """Задания текущей смены и число не закрытых с прошлых смен."""
    tasks = [
        dict(r)
        for r in fetch_all(
            """
            SELECT st.id, st.title, st.status, st.not_done_reason,
                   tt.name AS task_type_name,
                   m.id AS machine_id, m.inventory_number AS machine_inventory_number
            FROM shift_tasks st
            LEFT JOIN shift_task_types tt ON st.task_type_id = tt.id
            LEFT JOIN machines m ON st.machine_id = m.id
            WHERE st.effective_date = ? AND st.for_shift = ?
            ORDER BY st.created_at, st.id
            """,
            (cur_date, cur_shift),
        )
    ]
    counts = {"pending": 0, "done": 0, "not_done": 0}
    for t in tasks:
        counts[t["status"]] = counts.get(t["status"], 0) + 1
    tasks.sort(key=lambda t: _TASK_STATUS_ORDER.get(t["status"], 9))
    # «Прошлые» — всё, что раньше текущей смены: более ранние даты и 1-я смена, если сейчас 2-я
    open_past = fetch_one(
        """
        SELECT COUNT(*) AS c FROM shift_tasks
        WHERE carried_to_task_id IS NULL AND status IN ('pending', 'not_done')
          AND (effective_date < ? OR (effective_date = ? AND ? = 'night' AND for_shift = 'day'))
        """,
        (cur_date, cur_date, cur_shift),
    )["c"]
    return tasks, counts, open_past


def _journal_summary(cur_shift, cur_date):
    row = fetch_one(
        """
        SELECT
            (SELECT COUNT(*) FROM journal_entries WHERE shift_date = ? AND shift = ?) AS entries,
            (SELECT GROUP_CONCAT(name, ', ') FROM
                (SELECT name FROM journal_shift_engineers WHERE shift_date = ? AND shift = ? ORDER BY name)) AS engineers
        """,
        (cur_date, cur_shift, cur_date, cur_shift),
    )
    return {"entries": row["entries"] or 0, "engineers": row["engineers"] or ""}


def _when_label(created_at, today):
    """«сегодня 14:05», «вчера 22:10» или «08.11.26 09:00»."""
    day, time_part = (created_at or "")[:10], (created_at or "")[11:16]
    if day == today.isoformat():
        return f"сегодня {time_part}"
    if day == (today - timedelta(days=1)).isoformat():
        return f"вчера {time_part}"
    return f"{day[8:10]}.{day[5:7]}.{day[2:4]} {time_part}".strip()


def _recent_operations():
    today = date.today()
    ops = []
    for r in fetch_all(
        """
        SELECT id, datetime(created_at, 'localtime') AS created_at, operation_type, entity_type, entity_id,
               description, username
        FROM operations_log
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        (RECENT_OPS_LIMIT,),
    ):
        op = dict(r)
        op["description"] = fix_mojibake_text(op["description"]) if op.get("description") else ""
        op["entity_label"] = ENTITY_LABELS.get(op["entity_type"], op["entity_type"])
        op["operation_label"] = OPERATION_LABELS.get(op["operation_type"], op["operation_type"])
        op["tone"] = OPERATION_TONES.get(op["operation_type"], "slate")
        op["url"] = _entity_url(op["entity_type"], op["entity_id"])
        op["when"] = _when_label(op["created_at"], today)
        ops.append(op)
    return ops


def register(app):
    @app.route("/")
    def index():
        cur_shift, cur_date = sj.current_shift()
        role = getattr(current_user, "role", None)
        maintenance_counts, maintenance_rows = _maintenance_summary()
        shift_tasks, shift_counts, open_past = _shift_tasks(cur_shift, cur_date)
        return render_template(
            "dashboard.html",
            cur_shift=cur_shift,
            cur_date=cur_date,
            shift_title=sj.title_for(cur_shift, cur_date),
            inv=_inventory_summary(),
            maintenance_counts=maintenance_counts,
            maintenance_rows=maintenance_rows,
            shift_tasks=shift_tasks[:SHIFT_TASKS_LIMIT],
            shift_tasks_more=max(0, len(shift_tasks) - SHIFT_TASKS_LIMIT),
            shift_counts=shift_counts,
            open_past=open_past,
            journal=_journal_summary(cur_shift, cur_date),
            recent_ops=_recent_operations(),
            can_edit=getattr(current_user, "can_edit", False),
            can_work_shift=role in ("admin", "editor", "maintenance"),
            last_backup_at=_format_backup_date(get_setting("last_backup_at", "")),
        )

    @app.route("/search")
    def quick_search():
        """Быстрый поиск по инв. номеру техники или серийному номеру оборудования."""
        q = (request.args.get("q") or "").strip()[:100]
        if not q:
            return redirect(url_for("index"))
        machine = fetch_one("SELECT id FROM machines WHERE inventory_number = ?", (q,))
        if machine:
            return redirect(url_for("machine_detail", machine_id=machine["id"]))
        equip = fetch_one("SELECT id FROM equipment WHERE serial_number = ?", (q,))
        if equip:
            return redirect(url_for("equipment_detail", equipment_id=equip["id"]))
        machines = fetch_all("SELECT id FROM machines WHERE inventory_number LIKE ?", (f"%{q}%",))
        if len(machines) == 1:
            return redirect(url_for("machine_detail", machine_id=machines[0]["id"]))
        if len(machines) > 1:
            return redirect(url_for("machines_list", q=q))
        equip_list = fetch_all(
            "SELECT id FROM equipment WHERE serial_number LIKE ? OR name LIKE ?",
            (f"%{q}%", f"%{q}%"),
        )
        if len(equip_list) == 1:
            return redirect(url_for("equipment_detail", equipment_id=equip_list[0]["id"]))
        if len(equip_list) > 1:
            return redirect(url_for("equipment_list", q=q))
        flash(f"По запросу «{q}» ничего не найдено.", "error")
        return redirect(url_for("index"))

    # --- Equipment ---
