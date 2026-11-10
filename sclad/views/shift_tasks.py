"""Маршруты: shift_tasks."""
import sqlite3
from datetime import date, datetime, timedelta

from flask import flash, redirect, render_template, request, url_for
from flask_login import current_user

from ..db import fetch_all, fetch_one, get_db, log_operation, ref_id
from ..utils import to_int, to_iso_date

def _period_presets(today):
    """Быстрые кнопки периода: (название, с, по). Пустые даты — все задания."""
    yesterday = today - timedelta(days=1)
    return [
        ("Сегодня", today.isoformat(), today.isoformat()),
        ("Вчера", yesterday.isoformat(), yesterday.isoformat()),
        ("7 дней", (today - timedelta(days=6)).isoformat(), today.isoformat()),
        ("30 дней", (today - timedelta(days=29)).isoformat(), today.isoformat()),
        ("Этот месяц", today.replace(day=1).isoformat(), today.isoformat()),
        ("Все", "", ""),
    ]


# Сколько не закрытых заданий с прошлых смен показывать на сегодняшней странице
OPEN_PAST_LIMIT = 50


def register(app):
    @app.route("/shift_task_types")
    def shift_task_types_list():
        types_list = [
            dict(r)
            for r in fetch_all(
                "SELECT * FROM shift_task_types ORDER BY sort_order, id"
            )
        ]
        return render_template("shift_task_types_list.html", types_list=types_list)

    @app.route("/shift_task_types/add", methods=["GET", "POST"])
    def shift_task_type_add():
        if not current_user.is_authenticated or not current_user.can_edit:
            flash("Недостаточно прав.", "error")
            return redirect(url_for("shift_task_types_list"))
        if request.method == "POST":
            name = (request.form.get("name") or "").strip()
            sort_order_raw = (request.form.get("sort_order") or "").strip()
            sort_order = 0
            if sort_order_raw:
                try:
                    sort_order = int(sort_order_raw)
                except ValueError:
                    pass
            if not name:
                flash("Введите название типа.", "error")
            else:
                db = get_db()
                try:
                    db.execute(
                        "INSERT INTO shift_task_types (name, sort_order) VALUES (?, ?)",
                        (name, sort_order),
                    )
                    db.commit()
                    flash("Тип задания добавлен.", "success")
                    return redirect(url_for("shift_task_types_list"))
                except sqlite3.IntegrityError:
                    flash("Тип с таким названием уже есть.", "error")
        return render_template("shift_task_type_form.html", type_item=None)

    @app.route("/shift_task_types/<int:type_id>/edit", methods=["GET", "POST"])
    def shift_task_type_edit(type_id):
        if not current_user.is_authenticated or not current_user.can_edit:
            flash("Недостаточно прав.", "error")
            return redirect(url_for("shift_task_types_list"))
        type_item = fetch_one("SELECT * FROM shift_task_types WHERE id = ?", (type_id,))
        if type_item is None:
            flash("Тип не найден.", "error")
            return redirect(url_for("shift_task_types_list"))
        type_item = dict(type_item)
        if request.method == "POST":
            name = (request.form.get("name") or "").strip()
            sort_order_raw = (request.form.get("sort_order") or "").strip()
            sort_order = 0
            if sort_order_raw:
                try:
                    sort_order = int(sort_order_raw)
                except ValueError:
                    pass
            if not name:
                flash("Введите название типа.", "error")
            else:
                db = get_db()
                try:
                    db.execute(
                        "UPDATE shift_task_types SET name = ?, sort_order = ? WHERE id = ?",
                        (name, sort_order, type_id),
                    )
                    db.commit()
                    flash("Тип задания обновлён.", "success")
                    return redirect(url_for("shift_task_types_list"))
                except sqlite3.IntegrityError:
                    flash("Тип с таким названием уже есть.", "error")
        return render_template("shift_task_type_form.html", type_item=type_item)

    @app.route("/shift_task_types/<int:type_id>/delete", methods=["POST"])
    def shift_task_type_delete(type_id):
        if not current_user.is_authenticated or not current_user.can_edit:
            flash("Недостаточно прав.", "error")
            return redirect(url_for("shift_task_types_list"))
        type_item = fetch_one("SELECT * FROM shift_task_types WHERE id = ?", (type_id,))
        if type_item is None:
            flash("Тип не найден.", "error")
            return redirect(url_for("shift_task_types_list"))
        used = fetch_one(
            "SELECT 1 FROM shift_tasks WHERE task_type_id = ? LIMIT 1",
            (type_id,),
        )
        if used:
            flash("Нельзя удалить тип: по нему есть задания. Сначала измените тип у заданий.", "error")
            return redirect(url_for("shift_task_types_list"))
        db = get_db()
        db.execute("DELETE FROM shift_task_types WHERE id = ?", (type_id,))
        db.commit()
        flash("Тип задания удалён.", "success")
        return redirect(url_for("shift_task_types_list"))

    # --- Задания на смену (1-я / 2-я смена) ---

    @app.route("/shift_tasks", methods=["GET", "POST"])
    def shift_tasks_list():
        today_str = date.today().isoformat()
        filter_shift = request.args.get("shift", "").strip().lower() or None
        if filter_shift not in ("day", "night"):
            filter_shift = None
        # Период: date_from/date_to (пустые — без ограничения, обе пустые — все даты).
        # Старый параметр date=ГГГГ-ММ-ДД оставлен для ссылок на конкретный день.
        # Без параметров — сегодняшний день.
        if "date_from" in request.args or "date_to" in request.args:
            filter_date_from = to_iso_date(request.args.get("date_from")) or ""
            filter_date_to = to_iso_date(request.args.get("date_to")) or ""
        else:
            single = to_iso_date(request.args.get("date")) or today_str
            filter_date_from = filter_date_to = single
        if filter_date_from and filter_date_to and filter_date_from > filter_date_to:
            filter_date_from, filter_date_to = filter_date_to, filter_date_from
        filter_q = (request.args.get("q") or "").strip()[:100]
        # Дата по умолчанию в форме добавления: выбранный день, иначе сегодня
        filter_date = filter_date_from if filter_date_from and filter_date_from == filter_date_to else today_str
        filter_type_id = to_int(request.args.get("type"))
        filter_status = (request.args.get("status") or "").strip().lower() or None
        if filter_status not in ("pending", "done", "not_done"):
            filter_status = None
        filter_args = {
            "date_from": filter_date_from,
            "date_to": filter_date_to,
            "shift": filter_shift or "",
            "type": filter_type_id or "",
            "status": filter_status or "",
            "q": filter_q,
        }

        if request.method == "POST" and request.form.get("action") == "add":
            if not current_user.is_authenticated:
                flash("Войдите в систему.", "error")
                return redirect(url_for("shift_tasks_list"))
            if not current_user.can_edit and getattr(current_user, "role", None) != "maintenance":
                flash("Недостаточно прав для создания заданий.", "error")
                return redirect(url_for("shift_tasks_list"))
            for_shift = (request.form.get("for_shift") or "").strip().lower()
            effective_date = to_iso_date(request.form.get("effective_date"))
            task_type_id = ref_id("shift_task_types", request.form.get("task_type_id"))
            if not task_type_id:
                first_type = fetch_one("SELECT id FROM shift_task_types ORDER BY sort_order, id LIMIT 1")
                task_type_id = first_type["id"] if first_type else None
            machine_id = ref_id("machines", request.form.get("machine_id"))
            equipment_id = ref_id("equipment", request.form.get("equipment_id"))
            title = (request.form.get("title") or "").strip()
            description = (request.form.get("description") or "").strip() or None
            if for_shift not in ("day", "night"):
                flash("Укажите смену (1-я или 2-я).", "error")
            elif not effective_date:
                flash("Укажите корректную дату смены.", "error")
            elif not title:
                flash("Введите заголовок задания.", "error")
            elif not task_type_id:
                flash("Выберите тип задания.", "error")
            else:
                db = get_db()
                cur = db.execute(
                    """
                    INSERT INTO shift_tasks (created_by_user_id, for_shift, effective_date, task_type_id, machine_id, equipment_id, title, description, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                    """,
                    (current_user.id, for_shift, effective_date, task_type_id, machine_id, equipment_id, title, description),
                )
                new_task_id = cur.lastrowid
                db.commit()
                shift_name = "1-я" if for_shift == "day" else "2-я"
                log_operation(
                    "create",
                    "shift_task",
                    new_task_id,
                    f"Создано задание на смену: «{title}» (дата {effective_date}, {shift_name} смена)",
                )
                shift_label = "1-ю" if for_shift == "day" else "2-ю"
                flash(f"Задание для {shift_label} смены добавлено.", "success")
                return redirect(
                    url_for(
                        "shift_tasks_list",
                        **filter_args,
                        page=1,
                        per_page=request.args.get("per_page", 50),
                    )
                )

        where_parts = ["1 = 1"]
        params = []
        if filter_date_from:
            where_parts.append("st.effective_date >= ?")
            params.append(filter_date_from)
        if filter_date_to:
            where_parts.append("st.effective_date <= ?")
            params.append(filter_date_to)
        if filter_q:
            like = f"%{filter_q.casefold()}%"
            where_parts.append(
                """(CASEFOLD(st.title) LIKE ? OR CASEFOLD(COALESCE(st.description, '')) LIKE ?
                    OR st.machine_id IN (SELECT id FROM machines WHERE CASEFOLD(inventory_number) LIKE ?)
                    OR st.equipment_id IN (SELECT id FROM equipment WHERE CASEFOLD(name) LIKE ? OR CASEFOLD(serial_number) LIKE ?))"""
            )
            params.extend([like] * 5)
        if filter_shift:
            where_parts.append("st.for_shift = ?")
            params.append(filter_shift)
        if filter_type_id is not None:
            where_parts.append("st.task_type_id = ?")
            params.append(filter_type_id)
        if filter_status:
            where_parts.append("st.status = ?")
            params.append(filter_status)
        where_sql = " AND ".join(where_parts)

        page = max(1, min(100000, to_int(request.args.get("page"), 1)))
        per_page = to_int(request.args.get("per_page"), 50)
        per_page = max(20, min(200, per_page)) if per_page else 50
        total = fetch_one(
            f"SELECT COUNT(*) AS c FROM shift_tasks st WHERE {where_sql}",
            tuple(params),
        )["c"]
        total_pages = max(1, (total + per_page - 1) // per_page)
        if page > total_pages:
            page = total_pages
        offset = (page - 1) * per_page

        tasks = [
            dict(row)
            for row in fetch_all(
                f"""
                SELECT st.*,
                       tt.name AS task_type_name,
                       COALESCE(NULLIF(TRIM(u1.surname || ' ' || u1.first_name), ''), u1.username) AS created_by_name,
                       COALESCE(NULLIF(TRIM(u_nd.surname || ' ' || u_nd.first_name), ''), u_nd.username) AS not_done_by_name,
                       m.inventory_number AS machine_inventory_number,
                       e.name AS equipment_name,
                       e.serial_number AS equipment_serial,
                       src.effective_date AS carried_from_date,
                       src.for_shift AS carried_from_shift,
                       src.not_done_reason AS carried_from_reason,
                       dst.effective_date AS carried_to_date,
                       dst.for_shift AS carried_to_shift
                FROM shift_tasks st
                LEFT JOIN shift_task_types tt ON st.task_type_id = tt.id
                LEFT JOIN users u1 ON st.created_by_user_id = u1.id
                LEFT JOIN users u_nd ON st.not_done_by_user_id = u_nd.id
                LEFT JOIN machines m ON st.machine_id = m.id
                LEFT JOIN equipment e ON st.equipment_id = e.id
                LEFT JOIN shift_tasks src ON st.carried_from_task_id = src.id
                LEFT JOIN shift_tasks dst ON st.carried_to_task_id = dst.id
                WHERE {where_sql}
                ORDER BY st.effective_date DESC, st.for_shift, st.created_at DESC
                LIMIT ? OFFSET ?
                """,
                (*params, per_page, offset),
            )
        ]
        task_ids = [t["id"] for t in tasks]
        completions_by_task = {}
        if task_ids:
            placeholders = ",".join("?" * len(task_ids))
            rows = fetch_all(
                f"""
                SELECT c.shift_task_id, c.completed_at, c.comment,
                       COALESCE(NULLIF(TRIM(u.surname || ' ' || u.first_name), ''), u.username) AS user_name
                FROM shift_task_completions c
                LEFT JOIN users u ON c.user_id = u.id
                WHERE c.shift_task_id IN ({placeholders})
                ORDER BY c.completed_at
                """,
                tuple(task_ids),
            )
            for r in rows:
                r = dict(r)
                tid = r["shift_task_id"]
                if tid not in completions_by_task:
                    completions_by_task[tid] = []
                completions_by_task[tid].append((r["user_name"] or "—", r["completed_at"] or "", r.get("comment") or ""))

        machines = [
            dict(r)
            for r in fetch_all(
                """
                SELECT m.id, m.inventory_number, mm.name AS model_name, mt.name AS type_name
                FROM machines m
                LEFT JOIN machine_models mm ON m.machine_model_id = mm.id
                LEFT JOIN machine_types mt ON mm.machine_type_id = mt.id
                ORDER BY m.inventory_number
                """
            )
        ]
        # Оборудование в форму не выгружается целиком: оно подбирается поиском (/api/equipment/search)
        equipment = []
        task_types = [dict(r) for r in fetch_all("SELECT id, name FROM shift_task_types ORDER BY sort_order, id")]
        task_ids_completed_by_me = set()
        if current_user.is_authenticated and task_ids:
            task_ids_completed_by_me = {
                r[0]
                for r in fetch_all(
                    "SELECT shift_task_id FROM shift_task_completions WHERE user_id = ? AND shift_task_id IN (" + ",".join("?" * len(task_ids)) + ")",
                    (current_user.id,) + tuple(task_ids),
                )
            }
        # Не закрытые задания с прошлых смен: забытые «Ожидает» и не перенесённые «Не выполнено».
        # Показываются только на сегодняшней странице.
        open_past_tasks = []
        if filter_date_from == today_str and filter_date_to == today_str:
            open_past_tasks = [
                dict(r)
                for r in fetch_all(
                    """
                    SELECT st.id, st.effective_date, st.for_shift, st.title, st.status, st.not_done_reason,
                           tt.name AS task_type_name,
                           m.inventory_number AS machine_inventory_number
                    FROM shift_tasks st
                    LEFT JOIN shift_task_types tt ON st.task_type_id = tt.id
                    LEFT JOIN machines m ON st.machine_id = m.id
                    WHERE st.carried_to_task_id IS NULL
                      AND st.status IN ('pending', 'not_done')
                      AND st.effective_date < ?
                    ORDER BY st.effective_date DESC, st.for_shift DESC, st.id DESC
                    LIMIT ?
                    """,
                    (today_str, OPEN_PAST_LIMIT + 1),
                )
            ]
        open_past_more = len(open_past_tasks) > OPEN_PAST_LIMIT
        open_past_tasks = open_past_tasks[:OPEN_PAST_LIMIT]
        return render_template(
            "shift_tasks_list.html",
            tasks=tasks,
            open_past_tasks=open_past_tasks,
            open_past_more=open_past_more,
            completions_by_task=completions_by_task,
            task_ids_completed_by_me=task_ids_completed_by_me,
            filter_shift=filter_shift,
            filter_date=filter_date,
            filter_date_from=filter_date_from,
            filter_date_to=filter_date_to,
            filter_q=filter_q,
            filter_args=filter_args,
            period_presets=_period_presets(date.today()),
            filter_type_id=filter_type_id,
            filter_status=filter_status,
            today_str=today_str,
            machines=machines,
            equipment=equipment,
            task_types=task_types,
            pagination={"page": page, "per_page": per_page, "total": total, "total_pages": total_pages},
        )

    @app.route("/shift_tasks/<int:task_id>/edit", methods=["GET", "POST"])
    def shift_task_edit(task_id):
        if not current_user.is_authenticated or (not current_user.can_edit and getattr(current_user, "role", None) != "maintenance"):
            flash("Недостаточно прав.", "error")
            return redirect(url_for("shift_tasks_list"))
        task = fetch_one(
            "SELECT id, created_by_user_id, for_shift, effective_date, task_type_id, machine_id, equipment_id, title, description, status FROM shift_tasks WHERE id = ?",
            (task_id,),
        )
        if task is None:
            flash("Задание не найдено.", "error")
            return redirect(url_for("shift_tasks_list"))
        task = dict(task)
        if task["status"] != "pending":
            flash("Редактировать можно только задания в статусе «Ожидает».", "error")
            return redirect(url_for("shift_tasks_list"))
        if not current_user.can_edit and task["created_by_user_id"] != current_user.id:
            flash("Изменять задание может только тот, кто его создал, или редактор/администратор.", "error")
            return redirect(url_for("shift_tasks_list"))
        if request.method == "POST":
            for_shift = (request.form.get("for_shift") or "").strip().lower()
            effective_date = to_iso_date(request.form.get("effective_date"))
            task_type_id = ref_id("shift_task_types", request.form.get("task_type_id"))
            machine_id = ref_id("machines", request.form.get("machine_id"))
            equipment_id = ref_id("equipment", request.form.get("equipment_id"))
            title = (request.form.get("title") or "").strip()
            description = (request.form.get("description") or "").strip() or None
            if for_shift not in ("day", "night"):
                flash("Укажите смену (1-я или 2-я).", "error")
            elif not effective_date:
                flash("Укажите корректную дату смены.", "error")
            elif not title:
                flash("Введите заголовок задания.", "error")
            elif not task_type_id:
                flash("Выберите тип задания.", "error")
            else:
                db = get_db()
                db.execute(
                    """
                    UPDATE shift_tasks SET for_shift = ?, effective_date = ?, task_type_id = ?, machine_id = ?, equipment_id = ?, title = ?, description = ?
                    WHERE id = ? AND status = 'pending'
                    """,
                    (for_shift, effective_date, task_type_id, machine_id, equipment_id, title, description, task_id),
                )
                db.commit()
                log_operation(
                    "edit",
                    "shift_task",
                    task_id,
                    f"Изменено задание на смену #{task_id}: «{title}»",
                )
                flash("Задание обновлено.", "success")
                return redirect(url_for("shift_tasks_list"))
        machines = [
            dict(r)
            for r in fetch_all(
                """
                SELECT m.id, m.inventory_number, mm.name AS model_name, mt.name AS type_name
                FROM machines m
                LEFT JOIN machine_models mm ON m.machine_model_id = mm.id
                LEFT JOIN machine_types mt ON mm.machine_type_id = mt.id
                ORDER BY m.inventory_number
                """
            )
        ]
        # В форму попадает только уже выбранное оборудование, остальное ищется через /api/equipment/search
        equipment = [
            dict(r)
            for r in fetch_all(
                "SELECT id, name, serial_number, equipment_kind FROM equipment WHERE id = ?", (task["equipment_id"],)
            )
        ] if task.get("equipment_id") else []
        task_types = [dict(r) for r in fetch_all("SELECT id, name FROM shift_task_types ORDER BY sort_order, id")]
        return render_template(
            "shift_task_edit.html",
            task=task,
            machines=machines,
            equipment=equipment,
            task_types=task_types,
        )

    @app.route("/api/equipment/search")
    def api_equipment_search():
        """Поиск оборудования по названию или серийному номеру для выпадающих списков (JSON)."""
        q = (request.args.get("q") or "").strip()[:100]
        limit = max(1, min(50, to_int(request.args.get("limit"), 30)))
        if len(q) < 2:
            return {"items": [], "more": False}
        like = f"%{q.casefold()}%"
        rows = fetch_all(
            """
            SELECT id, name, serial_number, equipment_kind
            FROM equipment
            WHERE CASEFOLD(name) LIKE ? OR CASEFOLD(serial_number) LIKE ?
            ORDER BY CASE WHEN CASEFOLD(serial_number) = ? THEN 0 ELSE 1 END, name, serial_number
            LIMIT ?
            """,
            (like, like, q.casefold(), limit + 1),
        )
        items = [
            {
                "id": r["id"],
                "label": f"{r['name']} ({r['serial_number']})" + (" — датчик" if r["equipment_kind"] == "sensor" else ""),
            }
            for r in rows[:limit]
        ]
        return {"items": items, "more": len(rows) > limit}

    @app.route("/shift_tasks/<int:task_id>/delete", methods=["POST"])
    def shift_task_delete(task_id):
        if not current_user.is_authenticated or (not current_user.can_edit and getattr(current_user, "role", None) != "maintenance"):
            flash("Недостаточно прав.", "error")
            return redirect(url_for("shift_tasks_list"))
        task = fetch_one(
            "SELECT id, status, created_by_user_id, title, carried_from_task_id FROM shift_tasks WHERE id = ?", (task_id,)
        )
        if task is None:
            flash("Задание не найдено.", "error")
            return redirect(url_for("shift_tasks_list"))
        if task["status"] != "pending":
            flash("Удалить можно только задания в статусе «Ожидает».", "error")
            return redirect(url_for("shift_tasks_list"))
        if not current_user.can_edit and task["created_by_user_id"] != current_user.id:
            flash("Удалить задание может только тот, кто его создал, или редактор/администратор.", "error")
            return redirect(url_for("shift_tasks_list"))
        task_title = (task["title"] or "")[:80]
        db = get_db()
        db.execute("DELETE FROM shift_task_completions WHERE shift_task_id = ?", (task_id,))
        db.execute("DELETE FROM shift_tasks WHERE id = ?", (task_id,))
        if task["carried_from_task_id"]:
            # Удалили перенесённую копию — исходное задание снова считается не закрытым
            db.execute(
                "UPDATE shift_tasks SET carried_to_task_id = NULL WHERE id = ? AND carried_to_task_id = ?",
                (task["carried_from_task_id"], task_id),
            )
        db.commit()
        log_operation(
            "delete",
            "shift_task",
            task_id,
            f"Удалено задание на смену #{task_id}: «{task_title}»",
        )
        flash("Задание удалено.", "success")
        return redirect(request.referrer or url_for("shift_tasks_list"))

    @app.route("/shift_tasks/<int:task_id>/complete", methods=["GET", "POST"])
    def shift_task_complete(task_id):
        if not current_user.is_authenticated:
            flash("Войдите в систему.", "error")
            return redirect(url_for("shift_tasks_list"))
        if not current_user.can_edit and getattr(current_user, "role", None) != "maintenance":
            flash("Недостаточно прав.", "error")
            return redirect(url_for("shift_tasks_list"))
        task = fetch_one(
            "SELECT id, status, title FROM shift_tasks WHERE id = ?",
            (task_id,),
        )
        if task is None:
            flash("Задание не найдено.", "error")
            return redirect(url_for("shift_tasks_list"))
        if task["status"] == "not_done":
            flash("Задание отмечено как не выполненное. Перенесите его на другую смену.", "error")
            return redirect(url_for("shift_tasks_list"))
        if request.method == "GET":
            return render_template(
                "shift_task_complete.html",
                task=dict(task),
            )
        now = datetime.now().isoformat(sep=" ", timespec="seconds")
        comment = (request.form.get("comment") or "").strip() or None
        db = get_db()
        already = db.execute(
            "SELECT 1 FROM shift_task_completions WHERE shift_task_id = ? AND user_id = ?",
            (task_id, current_user.id),
        ).fetchone()
        if already:
            flash("Вы уже отметили это задание выполненным.", "info")
            return redirect(url_for("shift_tasks_list"))
        db.execute(
            "INSERT INTO shift_task_completions (shift_task_id, user_id, completed_at, comment) VALUES (?, ?, ?, ?)",
            (task_id, current_user.id, now, comment),
        )
        if task["status"] != "done":
            first = db.execute(
                "SELECT user_id, completed_at FROM shift_task_completions WHERE shift_task_id = ? ORDER BY completed_at LIMIT 1",
                (task_id,),
            ).fetchone()
            db.execute(
                "UPDATE shift_tasks SET status = 'done', completed_at = ?, completed_by_user_id = ? WHERE id = ?",
                (first[1], first[0], task_id),
            )
        db.commit()
        desc = f"Отмечено выполненным задание #{task_id}: «{(task["title"] or '')[:50]}»"
        if comment:
            desc += f". Что сделано: {comment[:100]}"
        log_operation("complete", "shift_task", task_id, desc)
        flash("Вы отмечены как исполнитель задания.", "success")
        return redirect(url_for("shift_tasks_list"))

    @app.route("/shift_tasks/<int:task_id>/not_done", methods=["GET", "POST"])
    def shift_task_not_done(task_id):
        if not current_user.is_authenticated:
            flash("Войдите в систему.", "error")
            return redirect(url_for("shift_tasks_list"))
        if not current_user.can_edit and getattr(current_user, "role", None) != "maintenance":
            flash("Недостаточно прав.", "error")
            return redirect(url_for("shift_tasks_list"))
        task = fetch_one(
            "SELECT id, status, title FROM shift_tasks WHERE id = ?",
            (task_id,),
        )
        if task is None:
            flash("Задание не найдено.", "error")
            return redirect(url_for("shift_tasks_list"))
        if task["status"] == "not_done":
            flash("Задание уже отмечено как не выполненное.", "info")
            return redirect(url_for("shift_tasks_list"))
        if task["status"] == "done" and not getattr(current_user, "is_admin", False):
            flash("Задание уже выполнено. Отметить его не выполненным может только администратор.", "error")
            return redirect(url_for("shift_tasks_list"))
        if request.method == "GET":
            return render_template(
                "shift_task_not_done.html",
                task=dict(task),
            )
        reason = (request.form.get("reason") or "").strip()
        if not reason:
            flash("Укажите причину невыполнения.", "error")
            return render_template(
                "shift_task_not_done.html",
                task=dict(task),
            )
        now = datetime.now().isoformat(sep=" ", timespec="seconds")
        db = get_db()
        db.execute(
            "UPDATE shift_tasks SET status = 'not_done', not_done_reason = ?, not_done_at = ?, not_done_by_user_id = ? WHERE id = ? AND status = ?",
            (reason, now, current_user.id, task_id, task["status"]),
        )
        db.commit()
        task_title = (task["title"] or "")[:50]
        reason_short = reason[:80].replace("\n", " ") if reason else ""
        log_operation(
            "not_done",
            "shift_task",
            task_id,
            f"Отмечено не выполненным задание #{task_id}: «{task_title}». Причина: {reason_short}",
        )
        flash("Задание отмечено как не выполненное.", "success")
        return redirect(url_for("shift_tasks_list"))

    @app.route("/shift_tasks/<int:task_id>/carry", methods=["GET", "POST"])
    def shift_task_carry(task_id):
        """Перенос не выполненного (или забытого с прошлой смены) задания на другую смену.

        Создаётся копия со ссылкой на исходное задание; исходное остаётся «Не выполнено»
        с причиной, поэтому история не теряется.
        """
        if not current_user.is_authenticated or (not current_user.can_edit and getattr(current_user, "role", None) != "maintenance"):
            flash("Недостаточно прав.", "error")
            return redirect(url_for("shift_tasks_list"))
        task = fetch_one("SELECT * FROM shift_tasks WHERE id = ?", (task_id,))
        if task is None:
            flash("Задание не найдено.", "error")
            return redirect(url_for("shift_tasks_list"))
        task = dict(task)
        today_str = date.today().isoformat()
        if task["carried_to_task_id"]:
            flash("Это задание уже перенесено.", "info")
            return redirect(url_for("shift_tasks_list"))
        overdue_pending = task["status"] == "pending" and task["effective_date"] < today_str
        if task["status"] != "not_done" and not overdue_pending:
            flash("Перенести можно только не выполненное задание или задание с прошлой смены, которое не закрыли.", "error")
            return redirect(url_for("shift_tasks_list"))

        # По умолчанию — следующая смена после исходной, но не раньше сегодняшнего дня
        if task["for_shift"] == "day":
            default_date, default_shift = task["effective_date"], "night"
        else:
            next_day = date.fromisoformat(task["effective_date"]) + timedelta(days=1)
            default_date, default_shift = next_day.isoformat(), "day"
        if default_date < today_str:
            default_date, default_shift = today_str, task["for_shift"]
        form = {"effective_date": default_date, "for_shift": default_shift, "reason": ""}

        if request.method == "POST":
            form["for_shift"] = (request.form.get("for_shift") or "").strip().lower()
            form["effective_date"] = to_iso_date(request.form.get("effective_date")) or ""
            form["reason"] = (request.form.get("reason") or "").strip()
            if form["for_shift"] not in ("day", "night"):
                flash("Укажите смену (1-я или 2-я).", "error")
            elif not form["effective_date"]:
                flash("Укажите корректную дату смены.", "error")
            elif (form["effective_date"], form["for_shift"] == "night") <= (task["effective_date"], task["for_shift"] == "night"):
                flash("Переносить можно только на более позднюю смену.", "error")
            elif overdue_pending and not form["reason"]:
                flash("Укажите, почему задание не выполнили.", "error")
            else:
                now = datetime.now().isoformat(sep=" ", timespec="seconds")
                db = get_db()
                # Захватываем запись до проверки, чтобы два человека не перенесли задание одновременно
                db.execute("BEGIN IMMEDIATE")
                fresh = db.execute(
                    "SELECT status, carried_to_task_id FROM shift_tasks WHERE id = ?", (task_id,)
                ).fetchone()
                if fresh is None or fresh[1] or fresh[0] != task["status"]:
                    db.rollback()
                    flash("Задание уже изменил кто-то другой. Обновите страницу.", "error")
                    return redirect(url_for("shift_tasks_list"))
                if overdue_pending:
                    db.execute(
                        "UPDATE shift_tasks SET status = 'not_done', not_done_reason = ?, not_done_at = ?, not_done_by_user_id = ? WHERE id = ?",
                        (form["reason"], now, current_user.id, task_id),
                    )
                cur = db.execute(
                    """
                    INSERT INTO shift_tasks (created_by_user_id, for_shift, effective_date, task_type_id, machine_id, equipment_id,
                                             title, description, status, carried_from_task_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                    """,
                    (current_user.id, form["for_shift"], form["effective_date"], task["task_type_id"], task["machine_id"],
                     task["equipment_id"], task["title"], task["description"], task_id),
                )
                new_task_id = cur.lastrowid
                db.execute("UPDATE shift_tasks SET carried_to_task_id = ? WHERE id = ?", (new_task_id, task_id))
                db.commit()
                shift_name = "1-ю" if form["for_shift"] == "day" else "2-ю"
                log_operation(
                    "carry",
                    "shift_task",
                    task_id,
                    f"Задание #{task_id} «{(task['title'] or '')[:50]}» перенесено на {form['effective_date']}, {shift_name} смену (новое задание #{new_task_id})",
                )
                flash(f"Задание перенесено на {shift_name} смену.", "success")
                return redirect(url_for("shift_tasks_list", date=form["effective_date"]))

        return render_template(
            "shift_task_carry.html",
            task=task,
            form=form,
            overdue_pending=overdue_pending,
        )
