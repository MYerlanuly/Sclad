"""Маршруты: references."""
import sqlite3

from flask import flash, redirect, render_template, request, url_for

from ..db import fetch_all, fetch_one, get_db, ref_id


def register(app):
    @app.route("/machine_types")
    def machine_types_list():
        types_ = fetch_all(
            """
            SELECT mt.*,
                   (SELECT COUNT(*) FROM machines m
                    JOIN machine_models mm ON m.machine_model_id = mm.id
                    WHERE mm.machine_type_id = mt.id) AS machines_count
            FROM machine_types mt
            ORDER BY mt.id
            """
        )
        return render_template("machine_types_list.html", types=types_)

    # --- Sensor positions (позиции установки датчиков) ---

    @app.route("/sensor_positions")
    def sensor_positions_list():
        positions = fetch_all(
            """
            SELECT sp.*,
                   (SELECT COUNT(*) FROM machine_equipment me
                    WHERE me.sensor_position_id = sp.id AND me.removed_at IS NULL) AS used_count
            FROM sensor_positions sp
            ORDER BY COALESCE(sp.sort_order, 99), sp.id
            """
        )
        return render_template("sensor_positions_list.html", positions=positions)

    @app.route("/sensor_positions/add", methods=["GET", "POST"])
    def sensor_position_add():
        if request.method == "POST":
            code = (request.form.get("code") or "").strip()
            position_group = request.form.get("position_group") or "wheel"
            label = (request.form.get("label") or "").strip() or None
            sort_order_raw = (request.form.get("sort_order") or "").strip()
            sort_order = None
            if sort_order_raw:
                try:
                    sort_order = int(sort_order_raw)
                except ValueError:
                    flash("Порядок сортировки должен быть числом.", "error")
                    return render_template("sensor_position_form.html", position=None)
            if not code:
                flash("Введите код позиции.", "error")
            else:
                db = get_db()
                try:
                    db.execute(
                        "INSERT INTO sensor_positions (code, position_group, label, sort_order) VALUES (?, ?, ?, ?)",
                        (code, position_group, label, sort_order),
                    )
                    db.commit()
                    flash("Позиция добавлена.", "success")
                    return redirect(url_for("sensor_positions_list"))
                except sqlite3.IntegrityError:
                    flash("Позиция с таким кодом уже существует.", "error")
        return render_template("sensor_position_form.html", position=None)

    @app.route("/sensor_positions/<int:position_id>/edit", methods=["GET", "POST"])
    def sensor_position_edit(position_id):
        position = fetch_one("SELECT * FROM sensor_positions WHERE id = ?", (position_id,))
        if position is None:
            flash("Позиция не найдена.", "error")
            return redirect(url_for("sensor_positions_list"))
        if request.method == "POST":
            code = (request.form.get("code") or "").strip()
            position_group = request.form.get("position_group") or "wheel"
            label = (request.form.get("label") or "").strip() or None
            sort_order_raw = (request.form.get("sort_order") or "").strip()
            sort_order = None
            if sort_order_raw:
                try:
                    sort_order = int(sort_order_raw)
                except ValueError:
                    flash("Порядок сортировки должен быть числом.", "error")
                    return render_template("sensor_position_form.html", position=position)
            if not code:
                flash("Введите код позиции.", "error")
            else:
                db = get_db()
                try:
                    db.execute(
                        "UPDATE sensor_positions SET code = ?, position_group = ?, label = ?, sort_order = ? WHERE id = ?",
                        (code, position_group, label, sort_order, position_id),
                    )
                    db.commit()
                    flash("Позиция обновлена.", "success")
                    return redirect(url_for("sensor_positions_list"))
                except sqlite3.IntegrityError:
                    flash("Позиция с таким кодом уже существует.", "error")
        return render_template("sensor_position_form.html", position=position)

    @app.route("/sensor_positions/<int:position_id>/delete", methods=["POST"])
    def sensor_position_delete(position_id):
        position = fetch_one("SELECT * FROM sensor_positions WHERE id = ?", (position_id,))
        if position is None:
            flash("Позиция не найдена.", "error")
            return redirect(url_for("sensor_positions_list"))
        used = fetch_one(
            "SELECT 1 FROM machine_equipment WHERE sensor_position_id = ? AND removed_at IS NULL LIMIT 1",
            (position_id,),
        )
        if used:
            flash("Нельзя удалить позицию: она уже используется на технике. Сначала демонтируйте соответствующие датчики.", "error")
            return redirect(url_for("sensor_positions_list"))
        db = get_db()
        db.execute("DELETE FROM sensor_positions WHERE id = ?", (position_id,))
        db.commit()
        flash("Позиция удалена.", "success")
        return redirect(url_for("sensor_positions_list"))

    @app.route("/machine_types/add", methods=["GET", "POST"])
    def machine_type_add():
        if request.method == "POST":
            name = (request.form.get("name") or "").strip()
            if not name:
                flash("Введите название типа.", "error")
            elif fetch_one("SELECT 1 FROM machine_types WHERE name = ?", (name,)):
                flash("Тип техники с таким названием уже есть.", "error")
            else:
                db = get_db()
                db.execute("INSERT INTO machine_types (name) VALUES (?)", (name,))
                db.commit()
                flash("Тип техники добавлен.", "success")
                return redirect(url_for("machine_types_list"))
        return render_template("machine_type_form.html", machine_type=None)

    @app.route("/machine_types/<int:type_id>/edit", methods=["GET", "POST"])
    def machine_type_edit(type_id):
        type_ = fetch_one("SELECT * FROM machine_types WHERE id = ?", (type_id,))
        if type_ is None:
            flash("Тип техники не найден.", "error")
            return redirect(url_for("machine_types_list"))
        if request.method == "POST":
            name = (request.form.get("name") or "").strip()
            if not name:
                flash("Введите название типа.", "error")
            elif fetch_one("SELECT 1 FROM machine_types WHERE name = ? AND id != ?", (name, type_id)):
                flash("Тип техники с таким названием уже есть.", "error")
            else:
                db = get_db()
                db.execute("UPDATE machine_types SET name = ? WHERE id = ?", (name, type_id))
                db.commit()
                flash("Тип техники обновлён.", "success")
                return redirect(url_for("machine_types_list"))
        return render_template("machine_type_form.html", machine_type=type_)

    @app.route("/machine_types/<int:type_id>/delete", methods=["POST"])
    def machine_type_delete(type_id):
        type_ = fetch_one("SELECT name FROM machine_types WHERE id = ?", (type_id,))
        if type_ is None:
            flash("Тип техники не найден.", "error")
            return redirect(url_for("machine_types_list"))
        has_models = fetch_one(
            "SELECT id FROM machine_models WHERE machine_type_id = ? LIMIT 1",
            (type_id,),
        )
        if has_models:
            flash(
                f"Нельзя удалить тип «{type_['name']}»: к нему привязаны модели техники. Сначала удалите или измените модели.",
                "error",
            )
            return redirect(url_for("machine_types_list"))
        db = get_db()
        db.execute("DELETE FROM machine_types WHERE id = ?", (type_id,))
        db.commit()
        flash("Тип техники удалён.", "success")
        return redirect(url_for("machine_types_list"))

    @app.route("/machine_models")
    def machine_models_list():
        models = fetch_all(
            """
            SELECT
                mm.*,
                mt.name AS type_name,
                COUNT(m.id) AS machines_count
            FROM machine_models mm
            JOIN machine_types mt ON mm.machine_type_id = mt.id
            LEFT JOIN machines m ON m.machine_model_id = mm.id
            GROUP BY mm.id
            ORDER BY mm.id
            """
        )
        types_ = fetch_all("SELECT * FROM machine_types ORDER BY id")
        return render_template("machine_models_list.html", models=models, types=types_)

    @app.route("/machine_models/add", methods=["GET", "POST"])
    def machine_model_add():
        types_ = fetch_all("SELECT * FROM machine_types ORDER BY id")
        if request.method == "POST":
            name = request.form.get("name")
            machine_type_id = ref_id("machine_types", request.form.get("machine_type_id"))
            if not name or not machine_type_id:
                flash("Заполните все поля.", "error")
            else:
                db = get_db()
                db.execute(
                    "INSERT INTO machine_models (name, machine_type_id) VALUES (?, ?)",
                    (name, machine_type_id),
                )
                db.commit()
                flash("Модель техники добавлена.", "success")
                return redirect(url_for("machine_models_list"))
        return render_template("machine_model_form.html", model=None, types=types_)

    @app.route("/machine_models/<int:model_id>/edit", methods=["GET", "POST"])
    def machine_model_edit(model_id):
        types_ = fetch_all("SELECT * FROM machine_types ORDER BY id")
        model = fetch_one("SELECT * FROM machine_models WHERE id = ?", (model_id,))
        if model is None:
            flash("Модель не найдена.", "error")
            return redirect(url_for("machine_models_list"))
        if request.method == "POST":
            name = request.form.get("name")
            machine_type_id = ref_id("machine_types", request.form.get("machine_type_id"))
            if not name or not machine_type_id:
                flash("Заполните все поля.", "error")
            else:
                db = get_db()
                db.execute(
                    "UPDATE machine_models SET name = ?, machine_type_id = ? WHERE id = ?",
                    (name, machine_type_id, model_id),
                )
                db.commit()
                flash("Модель обновлена.", "success")
                return redirect(url_for("machine_models_list"))
        return render_template("machine_model_form.html", model=model, types=types_)

    @app.route("/machine_models/<int:model_id>/delete", methods=["POST"])
    def machine_model_delete(model_id):
        model = fetch_one("SELECT id, name FROM machine_models WHERE id = ?", (model_id,))
        if model is None:
            flash("Модель не найдена.", "error")
            return redirect(url_for("machine_models_list"))
        has_machines = fetch_one(
            "SELECT id FROM machines WHERE machine_model_id = ? LIMIT 1",
            (model_id,),
        )
        if has_machines:
            flash(
                f"Нельзя удалить модель «{model['name']}»: к ней привязана техника. Сначала удалите или перепривяжите технику.",
                "error",
            )
            return redirect(url_for("machine_models_list"))
        db = get_db()
        db.execute("DELETE FROM machine_models WHERE id = ?", (model_id,))
        db.commit()
        flash("Модель удалена.", "success")
        return redirect(url_for("machine_models_list"))
