"""Маршруты: machines."""
import sqlite3

from datetime import date

from flask import flash, redirect, render_template, request, session, url_for
from flask_login import current_user

from ..db import fetch_all, fetch_one, get_db, ref_id
from ..utils import to_int

MAINTENANCE_RECENT_LIMIT = 5
HISTORY_LIMIT = 15
DUE_SOON_DAYS = 14  # как на странице «Техобслуживание»


def _maintenance_state(next_due_at, today=None):
    """Статус ТО и сколько дней осталось (отрицательное — просрочено)."""
    if not next_due_at:
        return "no_data", None
    try:
        due = date.fromisoformat(str(next_due_at)[:10])
    except ValueError:
        return "no_data", None
    days_left = (due - (today or date.today())).days
    if days_left < 0:
        return "overdue", days_left
    if days_left <= DUE_SOON_DAYS:
        return "due_soon", days_left
    return "ok", days_left


def _machine_card_context(machine_id):
    """Данные карточки борта: что установлено, ТО, задания, история установок."""
    active_sim = fetch_one(
        """
        SELECT sc.*, msc.id AS link_id, msc.installed_at
        FROM machine_sim_cards msc
        JOIN sim_cards sc ON msc.sim_card_id = sc.id
        WHERE msc.machine_id = ? AND msc.removed_at IS NULL
        """,
        (machine_id,),
    )

    equipment = [
        dict(row)
        for row in fetch_all(
            """
            SELECT e.id, e.name, e.serial_number, e.equipment_kind, me.id AS link_id, me.installed_at,
                   me.sensor_position_id, sp.code AS position_code, sp.label AS position_label,
                   sp.position_group
            FROM machine_equipment me
            JOIN equipment e ON me.equipment_id = e.id
            LEFT JOIN sensor_positions sp ON me.sensor_position_id = sp.id
            WHERE me.machine_id = ? AND me.removed_at IS NULL
            ORDER BY e.equipment_kind, COALESCE(sp.sort_order, 99), sp.id, e.id
            """,
            (machine_id,),
        )
    ]
    # Позиции колёс (ПЛ, ПП, ЗЛН, ЗЛВ, ЗПН, ЗПВ) — сетка датчиков, если на борту есть колёсные датчики
    # или это автосамосвал. Остальное оборудование, в т.ч. датчики без колёсной позиции, — списком.
    wheel_positions = [
        dict(row)
        for row in fetch_all(
            "SELECT id, code, label FROM sensor_positions WHERE position_group = 'wheel' ORDER BY COALESCE(sort_order, 99), id"
        )
    ]
    wheel_ids = {p["id"] for p in wheel_positions}
    by_position = {e["sensor_position_id"]: e for e in equipment if e["sensor_position_id"] in wheel_ids}
    for p in wheel_positions:
        p["sensor"] = by_position.get(p["id"])
    other_equipment = [e for e in equipment if e["sensor_position_id"] not in wheel_ids]

    maintenance_total = fetch_one(
        "SELECT COUNT(*) AS c FROM machine_maintenance WHERE machine_id = ?", (machine_id,)
    )["c"]
    maintenance_recent = [
        dict(row)
        for row in fetch_all(
            """
            SELECT id, performed_at, next_due_at, performed_by, remarks
            FROM machine_maintenance WHERE machine_id = ? ORDER BY performed_at DESC, id DESC LIMIT ?
            """,
            (machine_id, MAINTENANCE_RECENT_LIMIT),
        )
    ]
    # «Последнее ТО» — последняя внесённая запись (как на странице «Техобслуживание»)
    last_maintenance = fetch_one(
        """
        SELECT id, performed_at, next_due_at, performed_by, remarks
        FROM machine_maintenance WHERE machine_id = ? ORDER BY id DESC LIMIT 1
        """,
        (machine_id,),
    )
    last_maintenance = dict(last_maintenance) if last_maintenance else None
    to_status, to_days_left = _maintenance_state(last_maintenance["next_due_at"] if last_maintenance else None)

    # Задания на смену по борту: открытые (ждут / не выполнены и не перенесены) и последние закрытые
    task_sql = """
        SELECT st.id, st.title, st.status, st.effective_date, st.for_shift, st.not_done_reason,
               tt.name AS task_type_name
        FROM shift_tasks st
        LEFT JOIN shift_task_types tt ON st.task_type_id = tt.id
        WHERE st.machine_id = ? AND {where}
        ORDER BY st.effective_date DESC, st.for_shift DESC, st.id DESC
        LIMIT ?
    """
    open_tasks = [
        dict(r)
        for r in fetch_all(
            task_sql.format(where="st.carried_to_task_id IS NULL AND st.status IN ('pending', 'not_done')"),
            (machine_id, 20),
        )
    ]
    done_tasks = [
        dict(r) for r in fetch_all(task_sql.format(where="st.status = 'done'"), (machine_id, 3))
    ]

    # История установок и демонтажа оборудования и SIM на этом борту
    history = []
    for row in fetch_all(
        """
        SELECT me.installed_at, me.removed_at, e.id AS item_id, e.name, e.serial_number, sp.code AS position_code
        FROM machine_equipment me
        JOIN equipment e ON me.equipment_id = e.id
        LEFT JOIN sensor_positions sp ON me.sensor_position_id = sp.id
        WHERE me.machine_id = ?
        """,
        (machine_id,),
    ):
        item = {
            "kind": "equipment",
            "item_id": row["item_id"],
            "label": row["name"],
            "detail": " · ".join(x for x in (row["serial_number"], row["position_code"]) if x),
        }
        history.append(dict(item, action="install", at=row["installed_at"]))
        if row["removed_at"]:
            history.append(dict(item, action="remove", at=row["removed_at"]))
    for row in fetch_all(
        """
        SELECT msc.installed_at, msc.removed_at, sc.id AS item_id, sc.phone_number, sc.iccid
        FROM machine_sim_cards msc
        JOIN sim_cards sc ON msc.sim_card_id = sc.id
        WHERE msc.machine_id = ?
        """,
        (machine_id,),
    ):
        item = {"kind": "sim", "item_id": row["item_id"], "label": "SIM " + (row["phone_number"] or ""), "detail": row["iccid"] or ""}
        history.append(dict(item, action="install", at=row["installed_at"]))
        if row["removed_at"]:
            history.append(dict(item, action="remove", at=row["removed_at"]))
    history.sort(key=lambda h: (str(h["at"] or ""), h["action"] == "install"), reverse=True)

    return {
        "active_sim": active_sim,
        "equipment": equipment,
        "wheel_positions": wheel_positions,
        "wheel_sensors_count": len(by_position),
        "other_equipment": other_equipment,
        "last_maintenance": last_maintenance,
        "to_status": to_status,
        "to_days_left": to_days_left,
        "maintenance_recent": maintenance_recent,
        "maintenance_total": maintenance_total,
        "open_tasks": open_tasks,
        "done_tasks": done_tasks,
        "history": history[:HISTORY_LIMIT],
        "history_total": len(history),
    }


def register(app):
    @app.route("/machines")
    def machines_list():
        try:
            per_page = max(1, min(500, int(request.args.get("per_page", 50) or 50)))
        except (TypeError, ValueError):
            per_page = 50
        try:
            page = max(1, min(100000, to_int(request.args.get("page"), 1)))
        except (TypeError, ValueError):
            page = 1
        type_id = to_int(request.args.get("type"))
        q = (request.args.get("q") or "").strip()[:100]
        where, params = [], ()
        if type_id is not None:
            where.append("m.machine_model_id IN (SELECT id FROM machine_models WHERE machine_type_id = ?)")
            params = (*params, type_id)
        if q:
            where.append("(m.inventory_number LIKE ? OR mm.name LIKE ? OR mt.name LIKE ?)")
            params = (*params, f"%{q}%", f"%{q}%", f"%{q}%")
        where_sql = (" AND " + " AND ".join(where)) if where else ""
        total = fetch_one(
            f"""
            SELECT COUNT(*) AS c FROM machines m
            JOIN machine_models mm ON m.machine_model_id = mm.id
            JOIN machine_types mt ON mm.machine_type_id = mt.id
            WHERE 1=1{where_sql}
            """,
            params,
        )["c"]
        total_pages = max(1, (total + per_page - 1) // per_page)
        page = min(page, total_pages)
        offset = (page - 1) * per_page
        sort_cols = {"id": "m.id", "inventory_number": "m.inventory_number", "model_name": "mm.name", "type_name": "mt.name"}
        sort_key = request.args.get("sort", "id")
        order_key = request.args.get("order", "asc")
        sort_sql = sort_cols.get(sort_key, "m.id")
        order_sql = "DESC" if order_key == "desc" else "ASC"
        machines = fetch_all(
            f"""
            SELECT m.*, mm.name AS model_name, mt.name AS type_name, mm.id AS model_id, mt.id AS type_id
            FROM machines m
            JOIN machine_models mm ON m.machine_model_id = mm.id
            JOIN machine_types mt ON mm.machine_type_id = mt.id
            WHERE 1=1{where_sql}
            ORDER BY {sort_sql} {order_sql}
            LIMIT ? OFFSET ?
            """,
            (*params, per_page, offset),
        )
        types_ = fetch_all("SELECT * FROM machine_types ORDER BY id")
        models = fetch_all(
            """
            SELECT mm.*, mt.name AS type_name
            FROM machine_models mm
            JOIN machine_types mt ON mm.machine_type_id = mt.id
            ORDER BY mm.id
            """
        )
        return render_template(
            "machines_list.html",
            machines=machines,
            types=types_,
            models=models,
            pagination={"page": page, "per_page": per_page, "total": total, "total_pages": total_pages},
            filter_type=type_id,
            filter_q=q,
            sort=sort_key,
            order=order_key,
        )

    @app.route("/machines/add", methods=["GET", "POST"])
    def machine_add():
        models = fetch_all(
            """
            SELECT mm.*, mt.name AS type_name
            FROM machine_models mm
            JOIN machine_types mt ON mm.machine_type_id = mt.id
            ORDER BY mm.id
            """
        )
        if request.method == "POST":
            inventory_number = (request.form.get("inventory_number") or "").strip()
            machine_model_id = ref_id("machine_models", request.form.get("machine_model_id"))
            if not inventory_number or not machine_model_id:
                flash("Заполните инвентарный номер и выберите модель.", "error")
            else:
                existing = fetch_one(
                    "SELECT id FROM machines WHERE inventory_number = ?",
                    (inventory_number,),
                )
                if existing:
                    flash("Техника с таким инвентарным номером уже есть. Введите другой номер.", "error")
                else:
                    db = get_db()
                    try:
                        db.execute(
                            "INSERT INTO machines (inventory_number, machine_model_id) VALUES (?, ?)",
                            (inventory_number, machine_model_id),
                        )
                        db.commit()
                        flash("Техника добавлена.", "success")
                        return redirect(url_for("machines_list"))
                    except sqlite3.IntegrityError:
                        flash("Не удалось сохранить технику (возможно, такой инв. номер уже есть).", "error")
        return render_template("machine_form.html", machine=None, models=models)

    @app.route("/machines/<int:machine_id>/edit", methods=["GET", "POST"])
    def machine_edit(machine_id):
        models = fetch_all(
            """
            SELECT mm.*, mt.name AS type_name
            FROM machine_models mm
            JOIN machine_types mt ON mm.machine_type_id = mt.id
            ORDER BY mm.id
            """
        )
        machine = fetch_one("SELECT * FROM machines WHERE id = ?", (machine_id,))
        if machine is None:
            flash("Техника не найдена.", "error")
            return redirect(url_for("machines_list"))
        if request.method == "POST":
            inventory_number = (request.form.get("inventory_number") or "").strip()
            machine_model_id = ref_id("machine_models", request.form.get("machine_model_id"))
            if not inventory_number or not machine_model_id:
                flash("Заполните инвентарный номер и выберите модель.", "error")
            else:
                existing = fetch_one(
                    "SELECT id FROM machines WHERE inventory_number = ? AND id != ?",
                    (inventory_number, machine_id),
                )
                if existing:
                    flash("Техника с таким инвентарным номером уже есть. Введите другой номер.", "error")
                else:
                    db = get_db()
                    try:
                        db.execute(
                            """
                            UPDATE machines
                            SET inventory_number = ?, machine_model_id = ?
                            WHERE id = ?
                            """,
                            (inventory_number, machine_model_id, machine_id),
                        )
                        db.commit()
                        flash("Техника обновлена.", "success")
                        return redirect(url_for("machines_list"))
                    except sqlite3.IntegrityError:
                        flash("Не удалось обновить технику (возможно, такой инв. номер уже есть).", "error")
        return render_template("machine_form.html", machine=machine, models=models)

    @app.route("/machines/<int:machine_id>/delete", methods=["POST"])
    def machine_delete(machine_id):
        machine = fetch_one(
            "SELECT m.id, m.inventory_number FROM machines m WHERE m.id = ?",
            (machine_id,),
        )
        if machine is None:
            flash("Техника не найдена.", "error")
            return redirect(url_for("machines_list"))
        has_equipment = fetch_one(
            "SELECT id FROM machine_equipment WHERE machine_id = ? AND removed_at IS NULL LIMIT 1",
            (machine_id,),
        )
        if has_equipment:
            flash(
                f"Нельзя удалить технику {machine['inventory_number']}: на ней установлено оборудование. Сначала демонтируйте оборудование.",
                "error",
            )
            return redirect(url_for("machines_list"))
        has_sim = fetch_one(
            "SELECT id FROM machine_sim_cards WHERE machine_id = ? AND removed_at IS NULL LIMIT 1",
            (machine_id,),
        )
        if has_sim:
            flash(
                f"Нельзя удалить технику {machine['inventory_number']}: на ней установлена SIM-карта. Сначала демонтируйте SIM-карту.",
                "error",
            )
            return redirect(url_for("machines_list"))
        db = get_db()
        db.execute("DELETE FROM machines WHERE id = ?", (machine_id,))
        db.commit()
        flash("Техника удалена.", "success")
        return redirect(url_for("machines_list"))

    @app.route("/machines/<int:machine_id>")
    def machine_detail(machine_id):
        machine = fetch_one(
            """
            SELECT m.*, mm.name AS model_name, mt.name AS type_name
            FROM machines m
            JOIN machine_models mm ON m.machine_model_id = mm.id
            JOIN machine_types mt ON mm.machine_type_id = mt.id
            WHERE m.id = ?
            """,
            (machine_id,),
        )
        if machine is None:
            flash("Техника не найдена.", "error")
            return redirect(url_for("machines_list"))

        ctx = _machine_card_context(machine_id)

        recent = [r for r in (session.get("recent") or []) if not (r.get("type") == "machine" and r.get("id") == machine_id)]
        recent.append({"type": "machine", "id": machine_id, "label": (machine["inventory_number"] or f"#{machine_id}")[:50]})
        session["recent"] = recent[-10:]
        role = getattr(current_user, "role", None)
        return render_template(
            "machine_detail.html",
            machine=machine,
            can_edit=getattr(current_user, "can_edit", False),
            can_work_shift=role in ("admin", "editor", "maintenance"),
            **ctx,
        )

    # --- Техобслуживание (ТО) ---
