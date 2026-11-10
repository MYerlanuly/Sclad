"""Маршруты: installs."""
import sqlite3
from datetime import datetime

from flask import flash, redirect, render_template, request, url_for

from ..db import fetch_all, fetch_one, get_db, log_operation, ref_id
from ..utils import to_int


def register(app):
    def _insert_equipment_link(machine_id, equipment_id, sensor_position_id):
        """Записать установку оборудования. False, если его/позицию успел занять другой запрос."""
        db = get_db()
        try:
            db.execute(
                """
                INSERT INTO machine_equipment (machine_id, equipment_id, installed_at, sensor_position_id)
                VALUES (?, ?, ?, ?)
                """,
                (machine_id, equipment_id, datetime.now().isoformat(sep=" ", timespec="seconds"), sensor_position_id),
            )
            db.execute("UPDATE equipment SET status_id = 2 WHERE id = ?", (equipment_id,))
            db.commit()
            return True
        except sqlite3.IntegrityError:
            db.rollback()
            return False

    @app.route("/machines/<int:machine_id>/install_equipment", methods=["GET", "POST"])
    def install_equipment(machine_id):
        machine = fetch_one("SELECT * FROM machines WHERE id = ?", (machine_id,))
        if machine is None:
            flash("Техника не найдена.", "error")
            return redirect(url_for("machines_list"))

        # Свободное оборудование "На складе" (без привязки к технике) с постраничным выводом и серверным поиском
        q = (request.args.get("q") or "").strip()
        try:
            page = max(1, min(100000, to_int(request.args.get("page"), 1)))
        except (TypeError, ValueError):
            page = 1
        try:
            per_page = int(request.args.get("per_page", 50) or 50)
        except (TypeError, ValueError):
            per_page = 50
        per_page = max(10, min(200, per_page))
        offset = (page - 1) * per_page

        where_sql = [
            "e.status_id = 1",
            "NOT EXISTS (SELECT 1 FROM machine_equipment me WHERE me.equipment_id = e.id AND me.removed_at IS NULL)",
        ]
        params = []
        if q:
            where_sql.append(
                "(e.name LIKE ? OR e.serial_number LIKE ? OR IFNULL(e.description, '') LIKE ?)"
            )
            like = f"%{q}%"
            params.extend([like, like, like])
        where_clause = " AND ".join(where_sql)

        total_free = fetch_one(
            f"SELECT COUNT(*) AS c FROM equipment e WHERE {where_clause}",
            tuple(params),
        )["c"]

        free_equipment = [
            dict(row)
            for row in fetch_all(
                f"""
                SELECT e.*
                FROM equipment e
                WHERE {where_clause}
                ORDER BY e.id
                LIMIT ? OFFSET ?
                """,
                (*params, per_page, offset),
            )
        ]
        used_positions = {
            row["sensor_position_id"]
            for row in fetch_all(
                "SELECT sensor_position_id FROM machine_equipment WHERE machine_id = ? AND removed_at IS NULL AND sensor_position_id IS NOT NULL",
                (machine_id,),
            )
        }
        positions = [
            dict(r)
            for r in fetch_all(
                "SELECT * FROM sensor_positions ORDER BY COALESCE(sort_order, 99), id"
            )
        ]
        if request.method == "POST":
            equipment_id = to_int(request.form.get("equipment_id"))
            raw_sensor_position = request.form.get("sensor_position_id")
            # Пустое значение или специальное значение "none" означают установку датчика без позиции
            if raw_sensor_position in (None, "", "none"):
                sensor_position_id = None
            else:
                sensor_position_id = ref_id("sensor_positions", raw_sensor_position)
            if not equipment_id:
                flash("Выберите оборудование.", "error")
            elif raw_sensor_position not in (None, "", "none") and sensor_position_id is None:
                flash("Позиция датчика не найдена.", "error")
            else:
                eq = fetch_one("SELECT id, equipment_kind, name, status_id FROM equipment WHERE id = ?", (equipment_id,))
                busy = fetch_one(
                    "SELECT 1 FROM machine_equipment WHERE equipment_id = ? AND removed_at IS NULL", (equipment_id,)
                )
                if eq is None:
                    flash("Оборудование не найдено.", "error")
                elif busy or eq["status_id"] != 1:
                    flash("Это оборудование уже установлено или не числится на складе.", "error")
                elif (eq["equipment_kind"] if "equipment_kind" in eq.keys() else "other") == "sensor":
                    # Для датчиков позиция теперь необязательна, но одна и та же позиция
                    # не может быть использована несколькими датчиками одновременно.
                    if sensor_position_id and sensor_position_id in used_positions:
                        flash("Эта позиция уже занята.", "error")
                    else:
                        if not _insert_equipment_link(machine_id, equipment_id, sensor_position_id):
                            flash("Не удалось установить: оборудование или позиция только что заняты.", "error")
                            return redirect(url_for("machine_detail", machine_id=machine_id))
                        eq_name = (str(eq["name"]).strip() if "name" in eq.keys() and eq["name"] else None) or f"#{equipment_id}"
                        log_operation("install", "equipment", int(equipment_id), f"Установка датчика «{eq_name}» на технику {machine['inventory_number']}")
                        flash("Оборудование установлено.", "success")
                        return redirect(url_for("machine_detail", machine_id=machine_id))
                else:
                    if not _insert_equipment_link(machine_id, equipment_id, None):
                        flash("Не удалось установить: оборудование только что установлено на другую технику.", "error")
                        return redirect(url_for("machine_detail", machine_id=machine_id))
                    eq_name = (str(eq["name"]).strip() if "name" in eq.keys() and eq["name"] else None) or f"#{equipment_id}"
                    log_operation("install", "equipment", int(equipment_id), f"Установка оборудования «{eq_name}» на технику {machine['inventory_number']}")
                    flash("Оборудование установлено.", "success")
                    return redirect(url_for("machine_detail", machine_id=machine_id))
        total_pages = max(1, (total_free + per_page - 1) // per_page)
        pagination = {
            "page": page,
            "per_page": per_page,
            "total": total_free,
            "total_pages": total_pages,
        }
        return render_template(
            "install_equipment.html",
            machine=machine,
            equipment_list=free_equipment,
            positions=positions,
            used_positions=used_positions,
            pagination=pagination,
        )

    @app.route("/machines/<int:machine_id>/remove_equipment/<int:link_id>", methods=["POST"])
    def remove_equipment(machine_id, link_id):
        db = get_db()
        link = fetch_one(
            "SELECT * FROM machine_equipment WHERE id = ? AND machine_id = ? AND removed_at IS NULL",
            (link_id, machine_id),
        )
        if link is None:
            flash("Связь оборудование-техника не найдена или уже демонтирована.", "error")
            return redirect(url_for("machine_detail", machine_id=machine_id))

        now = datetime.now().isoformat(sep=" ", timespec="seconds")
        db.execute(
            "UPDATE machine_equipment SET removed_at = ? WHERE id = ?",
            (now, link_id),
        )
        db.execute(
            "UPDATE equipment SET status_id = 3 WHERE id = ?",
            (link["equipment_id"],),
        )
        db.commit()
        machine = fetch_one("SELECT inventory_number FROM machines WHERE id = ?", (machine_id,))
        inv_num = machine["inventory_number"] if machine else str(machine_id)
        eq = fetch_one("SELECT name FROM equipment WHERE id = ?", (link["equipment_id"],))
        eq_name = (eq["name"] if eq else "") or f"#{link['equipment_id']}"
        log_operation(
            "remove",
            "equipment",
            int(link["equipment_id"]),
            f"Демонтаж оборудования «{eq_name}» с техники {inv_num}",
        )
        flash("Оборудование демонтировано.", "success")
        return redirect(url_for("machine_detail", machine_id=machine_id))

    # --- SIM installation on machine ---
    @app.route("/machines/<int:machine_id>/install_sim", methods=["GET", "POST"])
    def install_sim(machine_id):
        machine = fetch_one("SELECT * FROM machines WHERE id = ?", (machine_id,))
        if machine is None:
            flash("Техника не найдена.", "error")
            return redirect(url_for("machines_list"))

        active_sim = fetch_one(
            "SELECT * FROM machine_sim_cards WHERE machine_id = ? AND removed_at IS NULL",
            (machine_id,),
        )
        if active_sim:
            flash("На этой технике уже установлена SIM-карта. Сначала демонтируйте её.", "error")
            return redirect(url_for("machine_detail", machine_id=machine_id))

        # Свободные SIM: статус "На складе" (id=1) и не установлены ни на одной технике — с постраничным выводом и серверным поиском
        q = (request.args.get("q") or "").strip()
        try:
            page = max(1, min(100000, to_int(request.args.get("page"), 1)))
        except (TypeError, ValueError):
            page = 1
        try:
            per_page = int(request.args.get("per_page", 50) or 50)
        except (TypeError, ValueError):
            per_page = 50
        per_page = max(10, min(200, per_page))
        offset = (page - 1) * per_page

        where_sql = [
            "sc.status_id = 1",
            "NOT EXISTS (SELECT 1 FROM machine_sim_cards msc WHERE msc.sim_card_id = sc.id AND msc.removed_at IS NULL)",
        ]
        params = []
        if q:
            where_sql.append(
                "(sc.phone_number LIKE ? OR sc.iccid LIKE ? OR IFNULL(sc.ip_address, '') LIKE ?)"
            )
            like = f"%{q}%"
            params.extend([like, like, like])
        where_clause = " AND ".join(where_sql)

        total_free = fetch_one(
            f"SELECT COUNT(*) AS c FROM sim_cards sc WHERE {where_clause}",
            tuple(params),
        )["c"]

        free_sims = fetch_all(
            f"""
            SELECT sc.*
            FROM sim_cards sc
            WHERE {where_clause}
            ORDER BY sc.id
            LIMIT ? OFFSET ?
            """,
            (*params, per_page, offset),
        )
        if request.method == "POST":
            sim_id = to_int(request.form.get("sim_card_id"))
            sim_row = fetch_one("SELECT status_id FROM sim_cards WHERE id = ?", (sim_id,)) if sim_id else None
            sim_busy = sim_id and fetch_one(
                "SELECT 1 FROM machine_sim_cards WHERE sim_card_id = ? AND removed_at IS NULL", (sim_id,)
            )
            if not sim_id:
                flash("Выберите SIM-карту.", "error")
            elif sim_row is None:
                flash("SIM-карта не найдена.", "error")
            elif sim_busy or sim_row["status_id"] != 1:
                flash("Эта SIM-карта уже установлена или не числится на складе.", "error")
            else:
                db = get_db()
                try:
                    db.execute(
                        """
                        INSERT INTO machine_sim_cards (machine_id, sim_card_id, installed_at)
                        VALUES (?, ?, ?)
                        """,
                        (machine_id, sim_id, datetime.now().isoformat(sep=" ", timespec="seconds")),
                    )
                    # Статус SIM -> Установлена (id=2)
                    db.execute(
                        "UPDATE sim_cards SET status_id = 2 WHERE id = ?",
                        (sim_id,),
                    )
                    db.commit()
                except sqlite3.IntegrityError:
                    db.rollback()
                    flash("Не удалось установить: SIM-карта или техника только что заняты.", "error")
                    return redirect(url_for("machine_detail", machine_id=machine_id))
                sim_card = fetch_one("SELECT phone_number FROM sim_cards WHERE id = ?", (sim_id,))
                phone = (sim_card["phone_number"] if sim_card else "") or f"#{sim_id}"
                log_operation(
                    "install",
                    "sim_card",
                    int(sim_id),
                    f"Установка SIM-карты {phone} на технику {machine['inventory_number']}",
                )
                flash("SIM-карта установлена.", "success")
                return redirect(url_for("machine_detail", machine_id=machine_id))
        total_pages = max(1, (total_free + per_page - 1) // per_page)
        pagination = {
            "page": page,
            "per_page": per_page,
            "total": total_free,
            "total_pages": total_pages,
        }
        return render_template("install_sim.html", machine=machine, sims=free_sims, pagination=pagination)

    @app.route("/machines/<int:machine_id>/remove_sim/<int:link_id>", methods=["POST"])
    def remove_sim(machine_id, link_id):
        db = get_db()
        link = fetch_one(
            "SELECT * FROM machine_sim_cards WHERE id = ? AND machine_id = ? AND removed_at IS NULL",
            (link_id, machine_id),
        )
        if link is None:
            flash("Связь SIM-карта-техника не найдена или уже демонтирована.", "error")
            return redirect(url_for("machine_detail", machine_id=machine_id))

        now = datetime.now().isoformat(sep=" ", timespec="seconds")
        db.execute(
            "UPDATE machine_sim_cards SET removed_at = ? WHERE id = ?",
            (now, link_id),
        )
        # Статус SIM -> На складе (id=1)
        db.execute(
            "UPDATE sim_cards SET status_id = 1 WHERE id = ?",
            (link["sim_card_id"],),
        )
        db.commit()
        machine = fetch_one("SELECT inventory_number FROM machines WHERE id = ?", (machine_id,))
        inv_num = machine["inventory_number"] if machine else str(machine_id)
        sim_card = fetch_one("SELECT phone_number FROM sim_cards WHERE id = ?", (link["sim_card_id"],))
        phone = (sim_card["phone_number"] if sim_card else "") or f"#{link['sim_card_id']}"
        log_operation(
            "remove",
            "sim_card",
            int(link["sim_card_id"]),
            f"Демонтаж SIM-карты {phone} с техники {inv_num}",
        )
        flash("SIM-карта демонтирована.", "success")
        return redirect(url_for("machine_detail", machine_id=machine_id))

    # --- Operations log ---
