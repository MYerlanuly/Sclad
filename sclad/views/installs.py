"""Маршруты: installs."""
import sqlite3
from datetime import datetime

from flask import flash, redirect, render_template, request, url_for

from ..db import fetch_all, fetch_one, get_db, log_operation, ref_id
from ..removal import EQUIPMENT_REMOVAL_REASONS, parse_removal, reason_text
from ..utils import to_int

# id статусов из schema.sql: для сообщений после демонтажа
EQUIPMENT_STATUS_NAMES = {1: "На складе", 2: "Установлено", 3: "Демонтировано", 4: "Неисправно", 5: "Списано"}
SIM_STATUS_NAMES = {1: "На складе", 2: "Установлена", 3: "Заблокирована", 4: "Списана"}
REPLACE_LIST_LIMIT = 100


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
        # ?position=<id> — «+ Установить» на пустом колесе в карточке борта: показываем только датчики
        # и сразу выбираем эту позицию (если она свободна)
        preselect_position = to_int(request.args.get("position"))
        if preselect_position:
            where_sql.append("e.equipment_kind = 'sensor'")
        # ?equipment=<id> — «Установить на борт» с карточки оборудования: в списке только оно
        only_equipment = to_int(request.args.get("equipment"))
        if only_equipment:
            where_sql.append("e.id = ?")
            params.append(only_equipment)
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
            preselect_position=preselect_position if preselect_position not in used_positions else None,
            preselect_position_code=next((p["code"] for p in positions if p["id"] == preselect_position), None),
            only_equipment=only_equipment,
            pagination=pagination,
        )

    def _remove_equipment_link(db, link, reason, comment, status_id, now):
        """Снять оборудование с борта (без commit): дата, причина и новый статус."""
        db.execute(
            "UPDATE machine_equipment SET removed_at = ?, removal_reason = ?, removal_comment = ? WHERE id = ?",
            (now, reason, comment, link["id"]),
        )
        db.execute("UPDATE equipment SET status_id = ? WHERE id = ?", (status_id, link["equipment_id"]))

    def _removal_log_suffix(kind, reason, comment):
        text = reason_text(kind, reason, comment)
        return f". Причина: {text}" if text else ""

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

        reason, comment, status_id = parse_removal(request.form, "equipment")
        now = datetime.now().isoformat(sep=" ", timespec="seconds")
        _remove_equipment_link(db, link, reason, comment, status_id, now)
        db.commit()
        machine = fetch_one("SELECT inventory_number FROM machines WHERE id = ?", (machine_id,))
        inv_num = machine["inventory_number"] if machine else str(machine_id)
        eq = fetch_one("SELECT name FROM equipment WHERE id = ?", (link["equipment_id"],))
        eq_name = (eq["name"] if eq else "") or f"#{link['equipment_id']}"
        log_operation(
            "remove",
            "equipment",
            int(link["equipment_id"]),
            f"Демонтаж оборудования «{eq_name}» с техники {inv_num}"
            + _removal_log_suffix("equipment", reason, comment),
        )
        status_name = EQUIPMENT_STATUS_NAMES.get(status_id, "")
        flash(f"Оборудование демонтировано. Статус: «{status_name}».", "success")
        if request.form.get("back") == "card":
            return redirect(url_for("equipment_detail", equipment_id=link["equipment_id"]))
        return redirect(url_for("machine_detail", machine_id=machine_id))

    @app.route("/machines/<int:machine_id>/replace_equipment/<int:link_id>", methods=["GET", "POST"])
    def replace_equipment(machine_id, link_id):
        """Замена в один шаг: снять установленное и поставить свободное со склада на то же место."""
        machine = fetch_one("SELECT * FROM machines WHERE id = ?", (machine_id,))
        old = fetch_one(
            """
            SELECT me.id, me.equipment_id, me.sensor_position_id, me.installed_at,
                   e.name, e.serial_number, e.equipment_kind,
                   sp.code AS position_code, sp.label AS position_label
            FROM machine_equipment me
            JOIN equipment e ON me.equipment_id = e.id
            LEFT JOIN sensor_positions sp ON me.sensor_position_id = sp.id
            WHERE me.id = ? AND me.machine_id = ? AND me.removed_at IS NULL
            """,
            (link_id, machine_id),
        )
        if machine is None or old is None:
            flash("Установка не найдена или оборудование уже снято.", "error")
            if machine is None:
                return redirect(url_for("machines_list"))
            return redirect(url_for("machine_detail", machine_id=machine_id))

        kind = old["equipment_kind"] or "other"
        if request.method == "POST":
            new_id = to_int(request.form.get("equipment_id"))
            reason, comment, status_id = parse_removal(request.form, "equipment")
            new = fetch_one("SELECT id, name, serial_number, status_id, equipment_kind FROM equipment WHERE id = ?", (new_id,)) if new_id else None
            new_busy = new_id and fetch_one(
                "SELECT 1 FROM machine_equipment WHERE equipment_id = ? AND removed_at IS NULL", (new_id,)
            )
            if not new_id:
                flash("Выберите, что поставить взамен.", "error")
            elif reason is None:
                flash("Укажите причину замены.", "error")
            elif new is None:
                flash("Оборудование не найдено.", "error")
            elif new_busy or new["status_id"] != 1:
                flash("Это оборудование уже установлено или не числится на складе.", "error")
            elif (new["equipment_kind"] or "other") != kind:
                flash("Датчик меняется только на датчик, прочее оборудование только на прочее.", "error")
            else:
                db = get_db()
                now = datetime.now().isoformat(sep=" ", timespec="seconds")
                try:
                    _remove_equipment_link(db, old, reason, comment, status_id, now)
                    db.execute(
                        """
                        INSERT INTO machine_equipment (machine_id, equipment_id, installed_at, sensor_position_id)
                        VALUES (?, ?, ?, ?)
                        """,
                        (machine_id, new_id, now, old["sensor_position_id"]),
                    )
                    db.execute("UPDATE equipment SET status_id = 2 WHERE id = ?", (new_id,))
                    db.commit()
                except sqlite3.IntegrityError:
                    db.rollback()
                    flash("Не удалось заменить: оборудование только что заняли. Попробуйте ещё раз.", "error")
                    return redirect(url_for("replace_equipment", machine_id=machine_id, link_id=link_id))
                inv = machine["inventory_number"]
                where = f" на позиции {old['position_code']}" if old["position_code"] else ""
                log_operation(
                    "remove",
                    "equipment",
                    int(old["equipment_id"]),
                    f"Замена: демонтаж «{old['name']}» {old['serial_number']} с техники {inv}{where}"
                    + _removal_log_suffix("equipment", reason, comment),
                )
                log_operation(
                    "install",
                    "equipment",
                    int(new_id),
                    f"Замена: установка «{new['name']}» {new['serial_number']} на технику {inv}{where} "
                    f"вместо {old['serial_number']}",
                )
                flash(
                    f"Заменено: {old['serial_number']} → {new['serial_number']}. "
                    f"Снятое получило статус «{EQUIPMENT_STATUS_NAMES.get(status_id, '')}».",
                    "success",
                )
                return redirect(url_for("machine_detail", machine_id=machine_id))

        # Свободное оборудование того же вида; то же наименование — первым
        q = (request.args.get("q") or "").strip()
        where_sql = [
            "e.status_id = 1",
            "COALESCE(e.equipment_kind, 'other') = ?",
            "NOT EXISTS (SELECT 1 FROM machine_equipment me WHERE me.equipment_id = e.id AND me.removed_at IS NULL)",
        ]
        params = [kind]
        if q:
            where_sql.append("(e.name LIKE ? OR e.serial_number LIKE ? OR IFNULL(e.description, '') LIKE ?)")
            like = f"%{q}%"
            params.extend([like, like, like])
        where_clause = " AND ".join(where_sql)
        total = fetch_one(f"SELECT COUNT(*) AS c FROM equipment e WHERE {where_clause}", tuple(params))["c"]
        candidates = fetch_all(
            f"""
            SELECT e.id, e.name, e.serial_number, e.description
            FROM equipment e
            WHERE {where_clause}
            ORDER BY CASE WHEN e.name = ? COLLATE NOCASE THEN 0 ELSE 1 END, e.name, e.serial_number
            LIMIT ?
            """,
            (*params, old["name"], REPLACE_LIST_LIMIT),
        )
        return render_template(
            "replace_equipment.html",
            machine=machine,
            old=old,
            candidates=candidates,
            total=total,
            limit=REPLACE_LIST_LIMIT,
            reasons=EQUIPMENT_REMOVAL_REASONS,
            selected_reason=request.form.get("reason") or "faulty",
            selected_id=to_int(request.form.get("equipment_id")),
            comment=request.form.get("comment") or "",
        )

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
        # ?sim=<id> — «Установить на борт» с карточки SIM: в списке только она
        only_sim = to_int(request.args.get("sim"))
        if only_sim:
            where_sql.append("sc.id = ?")
            params.append(only_sim)
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
        return render_template("install_sim.html", machine=machine, sims=free_sims, pagination=pagination, only_sim=only_sim)

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

        reason, comment, status_id = parse_removal(request.form, "sim")
        now = datetime.now().isoformat(sep=" ", timespec="seconds")
        db.execute(
            "UPDATE machine_sim_cards SET removed_at = ?, removal_reason = ?, removal_comment = ? WHERE id = ?",
            (now, reason, comment, link_id),
        )
        # Статус SIM по причине: обычно «На складе» (1), неисправная или заблокированная — «Заблокирована» (3)
        db.execute(
            "UPDATE sim_cards SET status_id = ? WHERE id = ?",
            (status_id, link["sim_card_id"]),
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
            f"Демонтаж SIM-карты {phone} с техники {inv_num}" + _removal_log_suffix("sim", reason, comment),
        )
        flash(f"SIM-карта демонтирована. Статус: «{SIM_STATUS_NAMES.get(status_id, '')}».", "success")
        if request.form.get("back") == "card":
            return redirect(url_for("sim_card_detail", sim_id=link["sim_card_id"]))
        return redirect(url_for("machine_detail", machine_id=machine_id))

    # --- Operations log ---
