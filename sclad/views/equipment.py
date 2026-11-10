"""Маршруты: equipment."""
import sqlite3

from flask import flash, redirect, render_template, request, session, url_for

from ..db import fetch_all, fetch_one, get_db, ref_id
from ..utils import to_int


def register(app):
    @app.route("/equipment")
    def equipment_list():
        try:
            per_page = max(1, min(500, int(request.args.get("per_page", 50) or 50)))
        except (TypeError, ValueError):
            per_page = 50
        try:
            page = max(1, min(100000, to_int(request.args.get("page"), 1)))
        except (TypeError, ValueError):
            page = 1
        status_id = to_int(request.args.get("status"))
        q = (request.args.get("q") or "").strip()[:100]
        where, params = [], ()
        if status_id is not None:
            where.append("e.status_id = ?")
            params = (*params, status_id)
        if q:
            q_lower = q.lower().strip()
            # По наименованию: как есть и с заглавной (SQLite LOWER не работает для кириллицы)
            # По серийному: как есть и в нижнем регистре
            where.append(
                "(e.name LIKE ? OR e.name LIKE ? OR e.serial_number LIKE ? OR LOWER(e.serial_number) LIKE ?)"
            )
            params = (*params, f"%{q}%", f"%{q.capitalize()}%", f"%{q}%", f"%{q_lower}%")
        where_sql = (" AND " + " AND ".join(where)) if where else ""
        total = fetch_one(
            f"SELECT COUNT(*) AS c FROM equipment e JOIN equipment_statuses s ON e.status_id = s.id WHERE 1=1{where_sql}",
            params,
        )["c"]
        total_pages = max(1, (total + per_page - 1) // per_page)
        page = min(page, total_pages)
        offset = (page - 1) * per_page
        sort_cols = {"id": "e.id", "name": "e.name", "serial_number": "e.serial_number", "status": "s.name", "added_at": "e.added_at"}
        sort_key = request.args.get("sort", "id")
        order_key = request.args.get("order", "desc")
        sort_sql = sort_cols.get(sort_key, "e.id")
        order_sql = "DESC" if order_key == "desc" else "ASC"
        equipment = fetch_all(
            f"""
            SELECT
                e.*,
                s.name AS status_name,
                m.id AS machine_id,
                m.inventory_number,
                mm.name AS machine_model_name
            FROM equipment e
            JOIN equipment_statuses s ON e.status_id = s.id
            LEFT JOIN machine_equipment me
                ON me.equipment_id = e.id AND me.removed_at IS NULL
            LEFT JOIN machines m ON me.machine_id = m.id
            LEFT JOIN machine_models mm ON m.machine_model_id = mm.id
            WHERE 1=1{where_sql}
            ORDER BY {sort_sql} {order_sql}
            LIMIT ? OFFSET ?
            """,
            (*params, per_page, offset),
        )
        statuses = fetch_all("SELECT * FROM equipment_statuses ORDER BY id")
        return render_template(
            "equipment_list.html",
            equipment=equipment,
            statuses=statuses,
            pagination={"page": page, "per_page": per_page, "total": total, "total_pages": total_pages},
            filter_status=status_id,
            filter_q=q,
            sort=sort_key,
            order=order_key,
        )

    @app.route("/equipment/add", methods=["GET", "POST"])
    def equipment_add():
        statuses = fetch_all("SELECT * FROM equipment_statuses ORDER BY id")
        if request.method == "POST":
            name = request.form.get("name")
            serial_number = request.form.get("serial_number")
            description = request.form.get("description")
            status_id = ref_id("equipment_statuses", request.form.get("status_id"))
            if not name or not serial_number or not status_id:
                flash("Заполните обязательные поля.", "error")
            else:
                db = get_db()
                # Проверка: комбинация (наименование, серийный номер) должна быть уникальной
                existing = fetch_one(
                    "SELECT id FROM equipment WHERE name = ? AND serial_number = ?",
                    (name, serial_number),
                )
                if existing:
                    flash("Оборудование с таким наименованием и серийным номером уже существует.", "error")
                else:
                    try:
                        equipment_kind = request.form.get("equipment_kind") or "other"
                        if equipment_kind not in ("sensor", "other"):
                            equipment_kind = "other"
                        db.execute(
                            """
                            INSERT INTO equipment (name, serial_number, description, status_id, equipment_kind)
                            VALUES (?, ?, ?, ?, ?)
                            """,
                            (name, serial_number, description, status_id, equipment_kind),
                        )
                        db.commit()
                        flash("Оборудование добавлено.", "success")
                        return redirect(url_for("equipment_list"))
                    except sqlite3.IntegrityError:
                        flash("Ошибка при добавлении оборудования.", "error")
        return render_template("equipment_form.html", statuses=statuses, equipment=None)

    @app.route("/equipment/<int:equipment_id>/edit", methods=["GET", "POST"])
    def equipment_edit(equipment_id):
        statuses = fetch_all("SELECT * FROM equipment_statuses ORDER BY id")
        equipment = fetch_one("SELECT * FROM equipment WHERE id = ?", (equipment_id,))
        if equipment is None:
            flash("Оборудование не найдено.", "error")
            return redirect(url_for("equipment_list"))

        if request.method == "POST":
            name = request.form.get("name")
            serial_number = request.form.get("serial_number")
            description = request.form.get("description")
            status_id = ref_id("equipment_statuses", request.form.get("status_id"))
            if not name or not serial_number or not status_id:
                flash("Заполните обязательные поля.", "error")
            else:
                db = get_db()
                # Проверка: комбинация (наименование, серийный номер) должна быть уникальной
                # (исключая текущее редактируемое оборудование)
                existing = fetch_one(
                    "SELECT id FROM equipment WHERE name = ? AND serial_number = ? AND id != ?",
                    (name, serial_number, equipment_id),
                )
                if existing:
                    flash("Оборудование с таким наименованием и серийным номером уже существует.", "error")
                else:
                    try:
                        equipment_kind = request.form.get("equipment_kind") or "other"
                        if equipment_kind not in ("sensor", "other"):
                            equipment_kind = "other"
                        db.execute(
                            """
                            UPDATE equipment
                            SET name = ?, serial_number = ?, description = ?, status_id = ?, equipment_kind = ?
                            WHERE id = ?
                            """,
                            (name, serial_number, description, status_id, equipment_kind, equipment_id),
                        )
                        db.commit()
                        flash("Оборудование обновлено.", "success")
                        return redirect(url_for("equipment_list"))
                    except sqlite3.IntegrityError:
                        flash("Ошибка при обновлении оборудования.", "error")

        return render_template("equipment_form.html", statuses=statuses, equipment=equipment)

    @app.route("/equipment/<int:equipment_id>/delete", methods=["POST"])
    def equipment_delete(equipment_id):
        db = get_db()
        # Проверяем, установлено ли оборудование на технику
        active_install = fetch_one(
            """
            SELECT me.id, m.inventory_number
            FROM machine_equipment me
            JOIN machines m ON me.machine_id = m.id
            WHERE me.equipment_id = ? AND me.removed_at IS NULL
            LIMIT 1
            """,
            (equipment_id,),
        )
        if active_install:
            flash(
                f"Нельзя удалить оборудование: оно установлено на технику {active_install['inventory_number']}. "
                "Сначала демонтируйте оборудование.",
                "error",
            )
            return redirect(url_for("equipment_list"))
        db.execute("DELETE FROM equipment WHERE id = ?", (equipment_id,))
        db.commit()
        flash("Оборудование удалено.", "success")
        return redirect(url_for("equipment_list"))

    @app.route("/equipment/<int:equipment_id>")
    def equipment_detail(equipment_id):
        equipment = fetch_one(
            """
            SELECT e.*, s.name AS status_name
            FROM equipment e
            JOIN equipment_statuses s ON e.status_id = s.id
            WHERE e.id = ?
            """,
            (equipment_id,),
        )
        if equipment is None:
            flash("Оборудование не найдено.", "error")
            return redirect(url_for("equipment_list"))

        installs = fetch_all(
            """
            SELECT me.*, m.inventory_number
            FROM machine_equipment me
            JOIN machines m ON me.machine_id = m.id
            WHERE me.equipment_id = ?
            ORDER BY me.installed_at DESC
            """,
            (equipment_id,),
        )
        logs = fetch_all(
            """
            SELECT * FROM operations_log
            WHERE entity_type = 'equipment' AND entity_id = ?
            ORDER BY created_at DESC
            """,
            (equipment_id,),
        )
        recent = [r for r in (session.get("recent") or []) if not (r.get("type") == "equipment" and r.get("id") == equipment_id)]
        recent.append({"type": "equipment", "id": equipment_id, "label": (equipment["name"] or equipment["serial_number"] or f"#{equipment_id}")[:50]})
        session["recent"] = recent[-10:]
        return render_template("equipment_detail.html", equipment=equipment, installs=installs, logs=logs)

    # --- Machine types & models & machines ---
