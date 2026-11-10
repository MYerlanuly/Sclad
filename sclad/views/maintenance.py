"""Маршруты: maintenance."""
from datetime import date, timedelta

from flask import flash, redirect, render_template, request, url_for

from ..db import fetch_all, fetch_one, get_db, get_setting, log_operation, ref_id
from ..utils import maintenance_next_due, to_int, to_iso_date

MAINTENANCE_STATUS_LABELS = {
    "overdue": "Просрочено",
    "due_soon": "Скоро ТО",
    "ok": "Норма",
    "no_data": "Не проводилось",
}


def register(app):
    @app.route("/maintenance")
    def maintenance_list():
        today = date.today().isoformat()
        try:
            per_page = max(1, min(500, int(request.args.get("per_page", 50) or 50)))
        except (TypeError, ValueError):
            per_page = 50
        try:
            page = max(1, min(100000, to_int(request.args.get("page"), 1)))
        except (TypeError, ValueError):
            page = 1
        type_id = to_int(request.args.get("type"))
        status_filter = (request.args.get("status") or "").strip() or None
        if status_filter not in MAINTENANCE_STATUS_LABELS:
            status_filter = None
        filter_q = (request.args.get("q") or "").strip()[:100]
        machine_types = [dict(r) for r in fetch_all("SELECT id, name FROM machine_types ORDER BY name")]
        # Последняя запись ТО по каждой машине берётся одним проходом (а не тремя
        # подзапросами на каждую строку), статус и «дней до ТО» считаются в SQL.
        cte = """
        WITH last_mnt AS (
            SELECT mnt.machine_id, mnt.next_due_at, mnt.performed_at, mnt.performed_by
            FROM machine_maintenance mnt
            JOIN (SELECT machine_id, MAX(id) AS id FROM machine_maintenance GROUP BY machine_id) lm
              ON lm.id = mnt.id
        ),
        to_data AS (
            SELECT m.id, m.inventory_number, mm.name AS model_name, mt.id AS type_id, mt.name AS type_name,
                   l.next_due_at, l.performed_at AS last_performed_at, l.performed_by AS last_performed_by,
                   CAST(julianday(substr(l.next_due_at, 1, 10)) - julianday(date('now', 'localtime')) AS INTEGER) AS days_left
            FROM machines m
            JOIN machine_models mm ON m.machine_model_id = mm.id
            JOIN machine_types mt ON mm.machine_type_id = mt.id
            LEFT JOIN last_mnt l ON l.machine_id = m.id
        ),
        with_status AS (
            SELECT *, CASE
                WHEN next_due_at IS NULL THEN 'no_data'
                WHEN next_due_at < date('now', 'localtime') THEN 'overdue'
                WHEN next_due_at <= date('now', 'localtime', '+14 days') THEN 'due_soon'
                ELSE 'ok'
            END AS to_status
            FROM to_data
        )
        """
        # Фильтры без статуса: по ним считаются плитки-счётчики
        base_where, base_params = [], []
        if type_id is not None:
            base_where.append("type_id = ?")
            base_params.append(type_id)
        if filter_q:
            like = f"%{filter_q.casefold()}%"
            base_where.append("(CASEFOLD(inventory_number) LIKE ? OR CASEFOLD(model_name) LIKE ?)")
            base_params.extend([like, like])
        base_sql = "".join(" AND " + w for w in base_where)
        status_counts = {k: 0 for k in MAINTENANCE_STATUS_LABELS}
        for r in fetch_all(
            f"{cte} SELECT to_status, COUNT(*) AS c FROM with_status WHERE 1=1{base_sql} GROUP BY to_status",
            tuple(base_params),
        ):
            status_counts[r["to_status"]] = r["c"]
        total_all = sum(status_counts.values())

        where_sql, params = base_sql, list(base_params)
        if status_filter:
            where_sql += " AND to_status = ?"
            params.append(status_filter)
        total = status_counts[status_filter] if status_filter else total_all
        total_pages = max(1, (total + per_page - 1) // per_page)
        page = min(page, total_pages)
        offset = (page - 1) * per_page
        order_sql = "ORDER BY CASE WHEN next_due_at IS NULL THEN 1 ELSE 0 END, next_due_at ASC, inventory_number LIMIT ? OFFSET ?"
        machines = [
            dict(r)
            for r in fetch_all(
                f"{cte} SELECT * FROM with_status WHERE 1=1{where_sql} {order_sql}",
                (*params, per_page, offset),
            )
        ]
        for m in machines:
            m["to_status_label"] = MAINTENANCE_STATUS_LABELS.get(m.get("to_status"), "—")
        return render_template(
            "maintenance_list.html",
            machines=machines,
            machine_types=machine_types,
            today=today,
            pagination={"page": page, "per_page": per_page, "total": total, "total_pages": total_pages},
            filter_type=type_id,
            filter_status=status_filter or "",
            filter_q=filter_q,
            status_counts=status_counts,
            total_all=total_all,
            maintenance_interval_months=get_setting("maintenance_interval_months", "3"),
        )

    @app.route("/maintenance/calendar")
    def maintenance_calendar():
        """Календарь ТО: ближайшие даты по дням и по неделям."""
        try:
            weeks = max(1, min(12, int(request.args.get("weeks", 6) or 6)))
        except (TypeError, ValueError):
            weeks = 6
        today = date.today()
        end = today + timedelta(days=weeks * 7)
        start_str = today.isoformat()
        end_str = end.isoformat()
        rows = fetch_all(
            """
            SELECT m.id AS machine_id, m.inventory_number, mm.name AS model_name, mt.name AS type_name,
                   (SELECT next_due_at FROM machine_maintenance mnt WHERE mnt.machine_id = m.id ORDER BY mnt.id DESC LIMIT 1) AS next_due_at
            FROM machines m
            JOIN machine_models mm ON m.machine_model_id = mm.id
            JOIN machine_types mt ON mm.machine_type_id = mt.id
            WHERE (SELECT next_due_at FROM machine_maintenance mnt WHERE mnt.machine_id = m.id ORDER BY mnt.id DESC LIMIT 1) IS NOT NULL
              AND (SELECT next_due_at FROM machine_maintenance mnt WHERE mnt.machine_id = m.id ORDER BY mnt.id DESC LIMIT 1) >= ?
              AND (SELECT next_due_at FROM machine_maintenance mnt WHERE mnt.machine_id = m.id ORDER BY mnt.id DESC LIMIT 1) <= ?
            ORDER BY next_due_at ASC, m.inventory_number
            """,
            (start_str, end_str),
        )
        items = [dict(r) for r in rows]
        for it in items:
            it["next_due_at"] = (it["next_due_at"] or "")[:10]

        by_day = {}
        for it in items:
            d = it["next_due_at"]
            if d:
                by_day.setdefault(d, []).append(it)
        days_sorted = sorted(by_day.keys())

        by_week = []
        week_start = today - timedelta(days=today.weekday())
        while week_start <= end:
            week_end = week_start + timedelta(days=6)
            week_label = f"{week_start.day:02d}.{week_start.month:02d}–{week_end.day:02d}.{week_end.month:02d}"
            day_cells = []
            for d in range(7):
                cell_date = week_start + timedelta(days=d)
                cell_str = cell_date.isoformat()
                day_cells.append({
                    "date": cell_str,
                    "label": cell_date.strftime("%d"),
                    "machines": by_day.get(cell_str, []),
                    "is_past": cell_date < today,
                    "is_today": cell_date == today,
                })
            by_week.append({"label": week_label, "week_start": week_start.isoformat(), "days": day_cells})
            week_start = week_end + timedelta(days=1)

        month_names = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря")
        weekday_names = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")

        # Вид «месяц»: сетка календаря на выбранный месяц
        month_param = request.args.get("month", "").strip() or today.strftime("%Y-%m")
        try:
            year_m, month_m = int(month_param[:4]), int(month_param[5:7])
            month_start = date(year_m, month_m, 1)
            if month_m == 12:
                month_end = date(year_m + 1, 1, 1) - timedelta(days=1)
            else:
                month_end = date(year_m, month_m + 1, 1) - timedelta(days=1)
        except (ValueError, IndexError):
            month_start = date(today.year, today.month, 1)
            if today.month == 12:
                month_end = date(today.year + 1, 1, 1) - timedelta(days=1)
            else:
                month_end = date(today.year, today.month + 1, 1) - timedelta(days=1)
        month_rows = fetch_all(
            """
            SELECT m.id AS machine_id, m.inventory_number, mm.name AS model_name, mt.name AS type_name,
                   (SELECT next_due_at FROM machine_maintenance mnt WHERE mnt.machine_id = m.id ORDER BY mnt.id DESC LIMIT 1) AS next_due_at
            FROM machines m
            JOIN machine_models mm ON m.machine_model_id = mm.id
            JOIN machine_types mt ON mm.machine_type_id = mt.id
            WHERE (SELECT next_due_at FROM machine_maintenance mnt WHERE mnt.machine_id = m.id ORDER BY mnt.id DESC LIMIT 1) IS NOT NULL
              AND (SELECT next_due_at FROM machine_maintenance mnt WHERE mnt.machine_id = m.id ORDER BY mnt.id DESC LIMIT 1) >= ?
              AND (SELECT next_due_at FROM machine_maintenance mnt WHERE mnt.machine_id = m.id ORDER BY mnt.id DESC LIMIT 1) <= ?
            ORDER BY next_due_at ASC
            """,
            (month_start.isoformat(), month_end.isoformat()),
        )
        month_items = [dict(r) for r in month_rows]
        for it in month_items:
            it["next_due_at"] = (it["next_due_at"] or "")[:10]
        by_day_month = {}
        for it in month_items:
            d = it["next_due_at"]
            if d:
                by_day_month.setdefault(d, []).append(it)
        cal_start = month_start - timedelta(days=month_start.weekday())
        month_calendar_weeks = []
        for week_idx in range(6):
            week_days = []
            for d in range(7):
                cell_date = cal_start + timedelta(days=week_idx * 7 + d)
                cell_str = cell_date.isoformat()
                week_days.append({
                    "date_iso": cell_str,
                    "day_num": cell_date.day if cell_date.month == month_start.month and cell_date.year == month_start.year else None,
                    "count": len(by_day_month.get(cell_str, [])),
                    "machines": by_day_month.get(cell_str, []),
                    "is_current_month": cell_date.month == month_start.month and cell_date.year == month_start.year,
                    "is_today": cell_date == today,
                })
            month_calendar_weeks.append(week_days)
        month_title = f"{month_names[month_start.month - 1].capitalize()} {month_start.year}"

        return render_template(
            "maintenance_calendar.html",
            by_day=by_day,
            days_sorted=days_sorted,
            by_week=by_week,
            today_str=today.isoformat(),
            weeks_param=weeks,
            month_names=month_names,
            weekday_names=weekday_names,
            month_calendar_weeks=month_calendar_weeks,
            month_title=month_title,
            month_param=month_start.strftime("%Y-%m"),
        )

    @app.route("/maintenance/add", methods=["GET", "POST"])
    @app.route("/maintenance/add/<int:machine_id>", methods=["GET", "POST"])
    def maintenance_add(machine_id=None):
        machines = fetch_all(
            "SELECT m.id, m.inventory_number, mm.name AS model_name FROM machines m "
            "JOIN machine_models mm ON m.machine_model_id = mm.id ORDER BY m.inventory_number"
        )
        machines = [dict(r) for r in machines]
        # Список пользователей для выбора исполнителей ТО
        engineers_rows = fetch_all(
            "SELECT id, username, surname, first_name FROM users ORDER BY surname, first_name, username"
        )
        engineers = []
        for r in engineers_rows:
            row = dict(r)
            full_name = " ".join(
                part for part in [(row.get("surname") or "").strip(), (row.get("first_name") or "").strip()] if part
            ).strip()
            label = full_name or row.get("username")
            engineers.append({"id": row["id"], "label": label})
        today_str = date.today().isoformat()
        if request.method == "POST":
            mid = ref_id("machines", request.form.get("machine_id") or machine_id)
            performed_at = to_iso_date(request.form.get("performed_at"))
            engineer_ids = request.form.getlist("engineers")
            remarks = request.form.get("remarks", "").strip()
            if not mid or not performed_at:
                flash("Укажите технику и дату проведения ТО.", "error")
            else:
                months_val = get_setting("maintenance_interval_months", "3")
                try:
                    months_int = max(1, min(24, int(months_val)))
                except (TypeError, ValueError):
                    months_int = 3
                next_due = maintenance_next_due(performed_at, months_int)
                # Собираем имена исполнителей по выбранным пользователям
                performed_by = None
                if engineer_ids:
                    labels_by_id = {str(e["id"]): e["label"] for e in engineers}
                    selected_labels = [labels_by_id.get(eid) for eid in engineer_ids if labels_by_id.get(eid)]
                    if selected_labels:
                        performed_by = ", ".join(selected_labels)
                db = get_db()
                db.execute(
                    """
                    INSERT INTO machine_maintenance (machine_id, performed_at, next_due_at, performed_by, remarks)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (mid, performed_at[:10], next_due, performed_by or None, remarks or None),
                )
                db.commit()
                machine = fetch_one("SELECT inventory_number FROM machines WHERE id = ?", (mid,))
                inv = machine["inventory_number"] if machine else str(mid)
                log_operation(
                    "status_change",
                    "machine",
                    mid,
                    f"Техобслуживание техники {inv} проведено {performed_at[:10]}. Следующее ТО: {next_due}. Исполнители: {performed_by or '—'}",
                )
                flash("Запись о ТО сохранена.", "success")
                return redirect(url_for("maintenance_list"))
        return render_template(
            "maintenance_form.html",
            machines=machines,
            preselected_machine_id=machine_id,
            maintenance_interval_months=get_setting("maintenance_interval_months", "3"),
             engineers=engineers,
             today_str=today_str,
        )

    @app.route("/maintenance/act-draft")
    def maintenance_act_draft():
        """Акт для печати перед проведением ТО (без сохранённой записи)."""
        machine_id = to_int(request.args.get("machine_id"))
        performed_at = (request.args.get("performed_at") or "").strip()[:10] or None
        if not machine_id:
            flash("Укажите технику.", "error")
            return redirect(url_for("maintenance_add"))
        machine = fetch_one(
            """
            SELECT m.id AS machine_id, m.inventory_number, mm.name AS model_name, mt.name AS type_name
            FROM machines m
            JOIN machine_models mm ON m.machine_model_id = mm.id
            JOIN machine_types mt ON mm.machine_type_id = mt.id
            WHERE m.id = ?
            """,
            (machine_id,),
        )
        if machine is None:
            flash("Техника не найдена.", "error")
            return redirect(url_for("maintenance_add"))
        rec = dict(machine)
        rec["performed_at"] = performed_at
        rec["next_due_at"] = maintenance_next_due(performed_at, int(get_setting("maintenance_interval_months", "3"))) if performed_at else None
        rec["performed_by"] = None
        rec["remarks"] = None
        equipment_other, equipment_sensors = _maintenance_act_equipment(machine_id)
        is_driver = "самосвал" in (rec.get("type_name") or "").lower()
        return render_template(
            "maintenance_act.html",
            act=rec,
            equipment_other=equipment_other,
            equipment_sensors=equipment_sensors,
            driver_label="Водитель" if is_driver else "Машинист",
            is_draft=True,
        )

    def _maintenance_act_equipment(machine_id):
        """Списки оборудования (не датчики) и датчиков с позициями для акта ТО."""
        rows = fetch_all(
            """
            SELECT e.name, e.serial_number, e.equipment_kind, sp.code AS position_code
            FROM machine_equipment me
            JOIN equipment e ON me.equipment_id = e.id
            LEFT JOIN sensor_positions sp ON me.sensor_position_id = sp.id
            WHERE me.machine_id = ? AND me.removed_at IS NULL
            ORDER BY e.equipment_kind, sp.id, e.id
            """,
            (machine_id,),
        )
        other, sensors = [], []
        for r in rows:
            d = dict(r)
            if d.get("equipment_kind") == "sensor":
                sensors.append(d)
            else:
                other.append(d)
        return other, sensors

    @app.route("/maintenance/<int:maintenance_id>/act")
    def maintenance_act(maintenance_id):
        """Страница акта о проведении ТО для печати (по сохранённой записи)."""
        rec = fetch_one(
            """
            SELECT mnt.id, mnt.machine_id, mnt.performed_at, mnt.next_due_at, mnt.performed_by, mnt.remarks,
                   m.inventory_number, mm.name AS model_name, mt.name AS type_name
            FROM machine_maintenance mnt
            JOIN machines m ON mnt.machine_id = m.id
            JOIN machine_models mm ON m.machine_model_id = mm.id
            JOIN machine_types mt ON mm.machine_type_id = mt.id
            WHERE mnt.id = ?
            """,
            (maintenance_id,),
        )
        if rec is None:
            flash("Запись о ТО не найдена.", "error")
            return redirect(url_for("maintenance_list"))
        rec = dict(rec)
        machine_id = rec["machine_id"]
        equipment_other, equipment_sensors = _maintenance_act_equipment(machine_id)
        is_driver = "самосвал" in (rec.get("type_name") or "").lower()
        return render_template(
            "maintenance_act.html",
            act=rec,
            equipment_other=equipment_other,
            equipment_sensors=equipment_sensors,
            driver_label="Водитель" if is_driver else "Машинист",
            is_draft=False,
        )

    # --- Типы заданий на смену (справочник) ---
