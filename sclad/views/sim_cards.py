"""Маршруты: sim_cards."""
import sqlite3

from flask import flash, redirect, render_template, request, url_for
from flask_login import current_user

from ..db import fetch_all, fetch_one, get_db, log_operation, ref_id
from ..utils import to_int
from .item_card import SIM_STATUS_TONE, card_logs, card_summary, install_periods, machines_for_pick


def register(app):
    @app.route("/sim_cards")
    def sim_cards_list():
        try:
            per_page = max(1, min(500, int(request.args.get("per_page", 50) or 50)))
        except (TypeError, ValueError):
            per_page = 50
        try:
            page = max(1, min(100000, to_int(request.args.get("page"), 1)))
        except (TypeError, ValueError):
            page = 1
        status_id = to_int(request.args.get("status"))
        type_id = to_int(request.args.get("type"))
        q = (request.args.get("q") or "").strip()[:100]
        where, params = [], ()
        if status_id is not None:
            where.append("sc.status_id = ?")
            params = (*params, status_id)
        if type_id is not None:
            where.append("msc.id IS NOT NULL AND mt.id = ?")
            params = (*params, type_id)
        if q:
            where.append(
                "(sc.phone_number LIKE ? OR sc.iccid LIKE ? OR sc.ip_address LIKE ? OR sc.operator LIKE ? OR sc.description LIKE ? OR sc.tariff LIKE ?)"
            )
            params = (*params, f"%{q}%", f"%{q}%", f"%{q}%", f"%{q}%", f"%{q}%", f"%{q}%")
        where_sql = (" AND " + " AND ".join(where)) if where else ""
        from_sql = """
            FROM sim_cards sc
            JOIN sim_statuses ss ON sc.status_id = ss.id
            LEFT JOIN machine_sim_cards msc ON msc.sim_card_id = sc.id AND msc.removed_at IS NULL
            LEFT JOIN machines m ON msc.machine_id = m.id
            LEFT JOIN machine_models mm ON m.machine_model_id = mm.id
            LEFT JOIN machine_types mt ON mm.machine_type_id = mt.id
        """
        total = fetch_one(
            f"SELECT COUNT(*) AS c {from_sql} WHERE 1=1{where_sql}",
            params,
        )["c"]
        total_pages = max(1, (total + per_page - 1) // per_page)
        page = min(page, total_pages)
        offset = (page - 1) * per_page
        sim_cards = fetch_all(
            f"""
            SELECT
                sc.*,
                ss.name AS status_name,
                m.id AS machine_id,
                m.inventory_number,
                mm.name AS machine_model_name,
                mt.id AS machine_type_id,
                mt.name AS machine_type_name
            {from_sql}
            WHERE 1=1{where_sql}
            ORDER BY sc.id
            LIMIT ? OFFSET ?
            """,
            (*params, per_page, offset),
        )
        statuses = fetch_all("SELECT * FROM sim_statuses ORDER BY id")
        machine_types = fetch_all("SELECT id, name FROM machine_types ORDER BY name")
        return render_template(
            "sim_cards_list.html",
            sim_cards=sim_cards,
            statuses=statuses,
            machine_types=machine_types,
            pagination={"page": page, "per_page": per_page, "total": total, "total_pages": total_pages},
            filter_status=status_id,
            filter_type=type_id,
            filter_q=q,
        )

    @app.route("/sim_cards/add", methods=["GET", "POST"])
    def sim_card_add():
        statuses = fetch_all("SELECT * FROM sim_statuses ORDER BY id")
        if request.method == "POST":
            phone_number = request.form.get("phone_number", "").strip()
            iccid = request.form.get("iccid", "").strip()
            ip_address = request.form.get("ip_address", "").strip()
            operator = request.form.get("operator", "").strip() or None
            description = request.form.get("description", "").strip() or None
            tariff = request.form.get("tariff", "").strip() or None
            status_id = ref_id("sim_statuses", request.form.get("status_id"))
            if not phone_number or not iccid or not status_id:
                flash("Заполните обязательные поля.", "error")
            else:
                db = get_db()
                try:
                    db.execute(
                        """
                        INSERT INTO sim_cards (phone_number, iccid, ip_address, operator, description, tariff, status_id)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (phone_number, iccid, ip_address, operator, description, tariff, status_id),
                    )
                    db.commit()
                    flash("SIM-карта добавлена.", "success")
                    return redirect(url_for("sim_cards_list"))
                except sqlite3.IntegrityError:
                    flash("ICCID должен быть уникальным.", "error")
        return render_template("sim_card_form.html", sim_card=None, statuses=statuses)

    @app.route("/sim_cards/<int:sim_id>/edit", methods=["GET", "POST"])
    def sim_card_edit(sim_id):
        statuses = fetch_all("SELECT * FROM sim_statuses ORDER BY id")
        sim_card = fetch_one("SELECT * FROM sim_cards WHERE id = ?", (sim_id,))
        if sim_card is None:
            flash("SIM-карта не найдена.", "error")
            return redirect(url_for("sim_cards_list"))
        if request.method == "POST":
            phone_number = request.form.get("phone_number", "").strip()
            iccid = request.form.get("iccid", "").strip()
            ip_address = request.form.get("ip_address", "").strip()
            operator = request.form.get("operator", "").strip() or None
            description = request.form.get("description", "").strip() or None
            tariff = request.form.get("tariff", "").strip() or None
            status_id = ref_id("sim_statuses", request.form.get("status_id"))
            if not phone_number or not iccid or not status_id:
                flash("Заполните обязательные поля.", "error")
            else:
                db = get_db()
                try:
                    db.execute(
                        """
                        UPDATE sim_cards
                        SET phone_number = ?, iccid = ?, ip_address = ?, operator = ?, description = ?, tariff = ?, status_id = ?
                        WHERE id = ?
                        """,
                        (phone_number, iccid, ip_address, operator, description, tariff, status_id, sim_id),
                    )
                    db.commit()
                    flash("SIM-карта обновлена.", "success")
                    return redirect(url_for("sim_cards_list"))
                except sqlite3.IntegrityError:
                    flash("ICCID должен быть уникальным.", "error")
        return render_template("sim_card_form.html", sim_card=sim_card, statuses=statuses)

    @app.route("/sim_cards/<int:sim_id>/delete", methods=["POST"])
    def sim_card_delete(sim_id):
        db = get_db()
        # Проверяем, установлена ли SIM-карта на технику
        active_install = fetch_one(
            """
            SELECT msc.id, m.inventory_number
            FROM machine_sim_cards msc
            JOIN machines m ON msc.machine_id = m.id
            WHERE msc.sim_card_id = ? AND msc.removed_at IS NULL
            LIMIT 1
            """,
            (sim_id,),
        )
        if active_install:
            flash(
                f"Нельзя удалить SIM-карту: она установлена на технику {active_install['inventory_number']}. "
                "Сначала демонтируйте SIM-карту.",
                "error",
            )
            return redirect(url_for("sim_cards_list"))
        db.execute("DELETE FROM sim_cards WHERE id = ?", (sim_id,))
        db.commit()
        flash("SIM-карта удалена.", "success")
        return redirect(url_for("sim_cards_list"))

    @app.route("/sim_cards/<int:sim_id>")
    def sim_card_detail(sim_id):
        sim_card = fetch_one(
            """
            SELECT sc.*, ss.name AS status_name
            FROM sim_cards sc
            JOIN sim_statuses ss ON sc.status_id = ss.id
            WHERE sc.id = ?
            """,
            (sim_id,),
        )
        if sim_card is None:
            flash("SIM-карта не найдена.", "error")
            return redirect(url_for("sim_cards_list"))

        periods = install_periods(
            fetch_all(
                """
                SELECT msc.id AS link_id, msc.machine_id, msc.installed_at, msc.removed_at,
                       m.inventory_number, mm.name AS model_name, mt.name AS type_name
                FROM machine_sim_cards msc
                JOIN machines m ON msc.machine_id = m.id
                LEFT JOIN machine_models mm ON m.machine_model_id = mm.id
                LEFT JOIN machine_types mt ON mm.machine_type_id = mt.id
                WHERE msc.sim_card_id = ?
                ORDER BY msc.installed_at DESC, msc.id DESC
                """,
                (sim_id,),
            )
        )
        can_edit = getattr(current_user, "can_edit", False)
        return render_template(
            "sim_card_detail.html",
            sim_card=sim_card,
            status_tone=SIM_STATUS_TONE.get(sim_card["status_id"], "zero"),
            periods=periods,
            logs=card_logs("sim_card", sim_id),
            machines=machines_for_pick(without_sim=True) if can_edit else [],
            can_edit=can_edit,
            **card_summary(periods),
        )

    @app.route("/sim_cards/<int:sim_id>/install_to")
    def sim_card_install_to(sim_id):
        """«Установить на борт» с карточки SIM: выбрали борт — открываем установку на нём только этой SIM."""
        if fetch_one("SELECT id FROM sim_cards WHERE id = ?", (sim_id,)) is None:
            flash("SIM-карта не найдена.", "error")
            return redirect(url_for("sim_cards_list"))
        inv = (request.args.get("machine") or "").strip()
        machine = fetch_one("SELECT id FROM machines WHERE inventory_number = ?", (inv,)) if inv else None
        if machine is None:
            flash(f"Борт «{inv}» не найден. Выберите борт из списка." if inv else "Укажите борт.", "error")
            return redirect(url_for("sim_card_detail", sim_id=sim_id))
        busy = fetch_one(
            """
            SELECT sc.phone_number FROM machine_sim_cards msc JOIN sim_cards sc ON msc.sim_card_id = sc.id
            WHERE msc.machine_id = ? AND msc.removed_at IS NULL
            """,
            (machine["id"],),
        )
        if busy:
            flash(f"На борту {inv} уже стоит SIM {busy['phone_number']}. Сначала снимите её.", "error")
            return redirect(url_for("sim_card_detail", sim_id=sim_id))
        return redirect(url_for("install_sim", machine_id=machine["id"], sim=sim_id))

    @app.route("/sim_cards/<int:sim_id>/set_status", methods=["POST"])
    def sim_card_set_status(sim_id):
        """Быстрая смена статуса с карточки: на склад, заблокирована, списана (только если SIM не на борту)."""
        sim_card = fetch_one(
            "SELECT sc.id, sc.phone_number, sc.status_id, ss.name AS status_name FROM sim_cards sc "
            "JOIN sim_statuses ss ON sc.status_id = ss.id WHERE sc.id = ?",
            (sim_id,),
        )
        if sim_card is None:
            flash("SIM-карта не найдена.", "error")
            return redirect(url_for("sim_cards_list"))
        new_status = fetch_one("SELECT id, name FROM sim_statuses WHERE id = ?", (to_int(request.form.get("status_id")),))
        installed = fetch_one(
            "SELECT 1 FROM machine_sim_cards WHERE sim_card_id = ? AND removed_at IS NULL", (sim_id,)
        )
        if new_status is None or new_status["id"] == 2:
            flash("Этот статус нельзя поставить вручную.", "error")
        elif installed:
            flash("SIM-карта стоит на борту. Сначала снимите её.", "error")
        elif new_status["id"] != sim_card["status_id"]:
            db = get_db()
            db.execute("UPDATE sim_cards SET status_id = ? WHERE id = ?", (new_status["id"], sim_id))
            db.commit()
            log_operation(
                "edit", "sim_card", sim_id,
                f"Статус SIM {sim_card['phone_number']}: {sim_card['status_name']} → {new_status['name']}",
            )
            flash(f"Статус изменён: {new_status['name']}.", "success")
        return redirect(url_for("sim_card_detail", sim_id=sim_id))

    # --- Equipment installation on machine ---
