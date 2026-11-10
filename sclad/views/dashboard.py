"""Маршруты: dashboard."""
from datetime import date, timedelta

from flask import flash, redirect, render_template, request, url_for

from ..db import fetch_all, fetch_one, get_setting
from ..utils import _format_backup_date, fix_mojibake_text


def register(app):
    @app.route("/")
    def index():
        # Сводка для дашборда
        machines_total = fetch_one("SELECT COUNT(*) AS c FROM machines", ())["c"]
        equipment_total = fetch_one("SELECT COUNT(*) AS c FROM equipment", ())["c"]
        equipment_warehouse = fetch_one(
            "SELECT COUNT(*) AS c FROM equipment WHERE status_id = 1", ()
        )["c"]
        equipment_installed = fetch_one(
            "SELECT COUNT(*) AS c FROM equipment WHERE status_id = 2", ()
        )["c"]
        sim_total = fetch_one("SELECT COUNT(*) AS c FROM sim_cards", ())["c"]
        sim_warehouse = fetch_one(
            "SELECT COUNT(*) AS c FROM sim_cards WHERE status_id = 1", ()
        )["c"]
        sim_installed = fetch_one(
            "SELECT COUNT(*) AS c FROM sim_cards WHERE status_id = 2", ()
        )["c"]
        machines_with_equipment = fetch_one(
            """
            SELECT COUNT(DISTINCT machine_id) AS c
            FROM machine_equipment WHERE removed_at IS NULL
            """,
            (),
        )["c"]
        machines_with_sim = fetch_one(
            """
            SELECT COUNT(*) AS c FROM machine_sim_cards WHERE removed_at IS NULL
            """,
            (),
        )["c"]
        ops_today = fetch_one(
            """
            SELECT COUNT(*) AS c FROM operations_log
            WHERE date(created_at, 'localtime') = date('now', 'localtime')
            """,
            (),
        )["c"]
        ops_week = fetch_one(
            """
            SELECT COUNT(*) AS c FROM operations_log
            WHERE datetime(created_at, 'localtime') >= datetime('now', 'localtime', '-7 days')
            """,
            (),
        )["c"]
        today_iso = date.today().isoformat()
        due_soon_end = (date.today() + timedelta(days=14)).isoformat()
        maintenance_overdue = fetch_one(
            """
            SELECT COUNT(*) AS c FROM machines m
            WHERE (SELECT next_due_at FROM machine_maintenance WHERE machine_id = m.id ORDER BY id DESC LIMIT 1) < ?
            """,
            (today_iso,),
        )["c"]
        maintenance_due_soon = fetch_one(
            """
            SELECT COUNT(*) AS c FROM machines m
            WHERE (SELECT next_due_at FROM machine_maintenance WHERE machine_id = m.id ORDER BY id DESC LIMIT 1) BETWEEN ? AND ?
            """,
            (today_iso, due_soon_end),
        )["c"]
        recent_ops = fetch_all(
            """
            SELECT id, datetime(created_at, 'localtime') AS created_at, operation_type, entity_type, entity_id, description, username
            FROM operations_log
            ORDER BY created_at DESC, id DESC
            LIMIT 10
            """
        )
        recent_ops_fixed = []
        for row in recent_ops:
            op = dict(row)
            if op.get("description"):
                op["description"] = fix_mojibake_text(op["description"])
            recent_ops_fixed.append(op)
        # Ближайшие ТО: техника с датой следующего ТО (сначала просроченные, потом по дате)
        maintenance_upcoming = fetch_all(
            """
            SELECT m.id, m.inventory_number, mm.name AS model_name,
                   (SELECT next_due_at FROM machine_maintenance WHERE machine_id = m.id ORDER BY id DESC LIMIT 1) AS next_due_at
            FROM machines m
            JOIN machine_models mm ON m.machine_model_id = mm.id
            WHERE (SELECT next_due_at FROM machine_maintenance WHERE machine_id = m.id ORDER BY id DESC LIMIT 1) IS NOT NULL
            ORDER BY next_due_at ASC
            LIMIT 7
            """
        )
        maintenance_upcoming = [dict(r) for r in maintenance_upcoming]
        for m in maintenance_upcoming:
            nd = m.get("next_due_at")
            if not nd:
                m["to_status"] = "no_data"
            elif nd < today_iso:
                m["to_status"] = "overdue"
            elif nd <= due_soon_end:
                m["to_status"] = "due_soon"
            else:
                m["to_status"] = "ok"
        # Задания на смену — сегодня
        shift_tasks_today_pending = fetch_one(
            "SELECT COUNT(*) AS c FROM shift_tasks WHERE effective_date = ? AND status = 'pending'",
            (today_iso,),
        )["c"]
        shift_tasks_today_done = fetch_one(
            "SELECT COUNT(*) AS c FROM shift_tasks WHERE effective_date = ? AND status = 'done'",
            (today_iso,),
        )["c"]
        shift_tasks_today_not_done = fetch_one(
            "SELECT COUNT(*) AS c FROM shift_tasks WHERE effective_date = ? AND status = 'not_done'",
            (today_iso,),
        )["c"]
        shift_tasks_today_list = fetch_all(
            """
            SELECT st.id, st.for_shift, st.title, st.status, tt.name AS task_type_name
            FROM shift_tasks st
            LEFT JOIN shift_task_types tt ON st.task_type_id = tt.id
            WHERE st.effective_date = ?
            ORDER BY st.for_shift, st.created_at DESC
            LIMIT 8
            """,
            (today_iso,),
        )
        shift_tasks_today_list = [dict(r) for r in shift_tasks_today_list]
        return render_template(
            "dashboard.html",
            machines_total=machines_total,
            equipment_total=equipment_total,
            equipment_warehouse=equipment_warehouse,
            equipment_installed=equipment_installed,
            sim_total=sim_total,
            sim_warehouse=sim_warehouse,
            sim_installed=sim_installed,
            machines_with_equipment=machines_with_equipment,
            machines_with_sim=machines_with_sim,
            ops_today=ops_today,
            ops_week=ops_week,
            maintenance_overdue=maintenance_overdue,
            maintenance_due_soon=maintenance_due_soon,
            maintenance_upcoming=maintenance_upcoming,
            shift_tasks_today_pending=shift_tasks_today_pending,
            shift_tasks_today_done=shift_tasks_today_done,
            shift_tasks_today_not_done=shift_tasks_today_not_done,
            shift_tasks_today_list=shift_tasks_today_list,
            recent_ops=recent_ops_fixed,
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
