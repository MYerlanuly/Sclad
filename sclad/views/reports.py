"""Маршруты: reports."""
import csv
import io
import sqlite3
from datetime import date, datetime, timedelta

from flask import Response, render_template, request
from openpyxl import Workbook
from openpyxl.styles import Border, Font, Side

from ..db import fetch_all, fetch_one
from ..utils import fix_mojibake_text, to_int


def register(app):
    @app.route("/operations")
    def operations_list():
        try:
            per_page = max(1, min(500, int(request.args.get("per_page") or 50)))
        except (TypeError, ValueError):
            per_page = 50
        try:
            page = max(1, min(100000, to_int(request.args.get("page"), 1)))
        except (TypeError, ValueError):
            page = 1
        try:
            row = fetch_one("SELECT COUNT(*) AS c FROM operations_log")
            total = int(row["c"]) if row and row["c"] is not None else 0
        except (sqlite3.OperationalError, TypeError, ValueError):
            total = 0
        total_pages = max(1, (total + per_page - 1) // per_page) if total else 1
        page = min(page, total_pages)
        offset = (page - 1) * per_page
        order_key = request.args.get("order") or "desc"
        if order_key not in ("asc", "desc"):
            order_key = "desc"
        order_sql = "DESC" if order_key == "desc" else "ASC"
        # Колонка username может отсутствовать (добавляется миграцией migrate_operations_log_username)
        has_username = False
        try:
            for row in fetch_all("PRAGMA table_info(operations_log)"):
                if len(row) >= 2 and row[1] == "username":
                    has_username = True
                    break
        except Exception:
            pass
        select_cols = "id, datetime(created_at, 'localtime') AS created_at, operation_type, entity_type, entity_id, description"
        if has_username:
            select_cols += ", username"
        rows = fetch_all(
            f"""
            SELECT {select_cols}
            FROM operations_log
            ORDER BY created_at {order_sql}, id {order_sql}
            LIMIT ? OFFSET ?
            """,
            (per_page, offset),
        )
        operations = []
        for row in rows:
            op = dict(row)
            if "username" not in op:
                op["username"] = None
            if op.get("description"):
                op["description"] = fix_mojibake_text(op["description"])
            operations.append(op)
        return render_template(
            "operations_list.html",
            operations=operations,
            pagination={"page": page, "per_page": per_page, "total": total, "total_pages": total_pages},
            order=order_key,
        )

    @app.route("/operations/export")
    def operations_export_csv():
        has_username = False
        try:
            for r in fetch_all("PRAGMA table_info(operations_log)"):
                if len(r) >= 2 and r[1] == "username":
                    has_username = True
                    break
        except Exception:
            pass
        select_cols = "datetime(created_at, 'localtime') AS created_at, operation_type, entity_type, entity_id, description"
        if has_username:
            select_cols += ", username"
        rows = fetch_all(
            f"""
            SELECT {select_cols}
            FROM operations_log
            ORDER BY created_at DESC, id DESC
            LIMIT 10000
            """
        )
        buf = io.StringIO()
        buf.write("\ufeff")  # BOM для корректного открытия в Excel
        writer = csv.writer(buf, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        writer.writerow(["Дата", "Операция", "Сущность", "ID сущности", "Описание", "Кто"])
        for row in rows:
            r = dict(row)
            desc = r.get("description") or ""
            if desc:
                desc = fix_mojibake_text(desc)
            writer.writerow(
                [
                    r.get("created_at") or "",
                    r.get("operation_type") or "",
                    r.get("entity_type") or "",
                    r.get("entity_id") or "",
                    desc,
                    (r.get("username") or ""),
                ]
            )
        filename = f"operations_{datetime.now().strftime('%Y-%m-%d_%H-%M')}.csv"
        return Response(
            buf.getvalue(),
            mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.route("/export/excel")
    def export_inventory_excel():
        """Экспорт в Excel: оборудование, установленное оборудование, SIM-карты, техника."""
        wb = Workbook()
        header_font = Font(bold=True)
        thin_border = Border(
            left=Side(style="thin"),
            right=Side(style="thin"),
            top=Side(style="thin"),
            bottom=Side(style="thin"),
        )

        # Лист 1: Оборудование (все, с указанием где установлено)
        ws_eq = wb.active
        ws_eq.title = "Оборудование"
        eq_headers = ["ID", "Наименование", "Серийный номер", "Описание", "Статус", "Вид", "Установлено на (инв. №)", "Модель техники", "Позиция", "Дата установки"]
        for col, h in enumerate(eq_headers, 1):
            c = ws_eq.cell(row=1, column=col, value=h)
            c.font = header_font
            c.border = thin_border
        equipment = fetch_all(
            """
            SELECT e.id, e.name, e.serial_number, e.description, e.equipment_kind,
                   es.name AS status_name,
                   m.inventory_number AS machine_inv, mm.name AS machine_model_name,
                   sp.code AS position_code, me.installed_at
            FROM equipment e
            JOIN equipment_statuses es ON e.status_id = es.id
            LEFT JOIN machine_equipment me ON me.equipment_id = e.id AND me.removed_at IS NULL
            LEFT JOIN machines m ON me.machine_id = m.id
            LEFT JOIN machine_models mm ON m.machine_model_id = mm.id
            LEFT JOIN sensor_positions sp ON me.sensor_position_id = sp.id
            ORDER BY e.name, e.serial_number
            """
        )
        for row_idx, row in enumerate(equipment, 2):
            r = dict(row)
            ws_eq.cell(row=row_idx, column=1, value=r.get("id"))
            ws_eq.cell(row=row_idx, column=2, value=r.get("name") or "")
            ws_eq.cell(row=row_idx, column=3, value=r.get("serial_number") or "")
            ws_eq.cell(row=row_idx, column=4, value=r.get("description") or "")
            ws_eq.cell(row=row_idx, column=5, value=r.get("status_name") or "")
            ws_eq.cell(row=row_idx, column=6, value="Датчик" if r.get("equipment_kind") == "sensor" else "Прочее")
            ws_eq.cell(row=row_idx, column=7, value=r.get("machine_inv") or "—")
            ws_eq.cell(row=row_idx, column=8, value=r.get("machine_model_name") or "")
            ws_eq.cell(row=row_idx, column=9, value=r.get("position_code") or "")
            ws_eq.cell(row=row_idx, column=10, value=r.get("installed_at") or "")

        # Лист 2: Установлено на технике (одна строка — одна установка)
        ws_inst = wb.create_sheet("Установлено на технике")
        inst_headers = ["Инв. номер техники", "Модель", "Тип техники", "Наименование оборудования", "Серийный номер", "Позиция", "Дата установки"]
        for col, h in enumerate(inst_headers, 1):
            c = ws_inst.cell(row=1, column=col, value=h)
            c.font = header_font
            c.border = thin_border
        installed = fetch_all(
            """
            SELECT m.inventory_number, mm.name AS model_name, mt.name AS type_name,
                   e.name AS equipment_name, e.serial_number,
                   sp.code AS position_code, me.installed_at
            FROM machine_equipment me
            JOIN machines m ON me.machine_id = m.id
            JOIN machine_models mm ON m.machine_model_id = mm.id
            JOIN machine_types mt ON mm.machine_type_id = mt.id
            JOIN equipment e ON me.equipment_id = e.id
            LEFT JOIN sensor_positions sp ON me.sensor_position_id = sp.id
            WHERE me.removed_at IS NULL
            ORDER BY m.inventory_number, e.name
            """
        )
        for row_idx, row in enumerate(installed, 2):
            r = dict(row)
            ws_inst.cell(row=row_idx, column=1, value=r.get("inventory_number") or "")
            ws_inst.cell(row=row_idx, column=2, value=r.get("model_name") or "")
            ws_inst.cell(row=row_idx, column=3, value=r.get("type_name") or "")
            ws_inst.cell(row=row_idx, column=4, value=r.get("equipment_name") or "")
            ws_inst.cell(row=row_idx, column=5, value=r.get("serial_number") or "")
            ws_inst.cell(row=row_idx, column=6, value=r.get("position_code") or "")
            ws_inst.cell(row=row_idx, column=7, value=r.get("installed_at") or "")

        # Лист 3: SIM-карты
        ws_sim = wb.create_sheet("SIM-карты")
        sim_headers = ["Номер", "ICCID", "Оператор", "Тариф", "Описание", "IP", "Статус", "Установлена на (инв. №)", "Модель техники", "Дата установки"]
        for col, h in enumerate(sim_headers, 1):
            c = ws_sim.cell(row=1, column=col, value=h)
            c.font = header_font
            c.border = thin_border
        sim_cards = fetch_all(
            """
            SELECT sc.phone_number, sc.iccid, sc.operator, sc.tariff, sc.description, sc.ip_address, ss.name AS status_name,
                   m.inventory_number, mm.name AS machine_model_name, msc.installed_at
            FROM sim_cards sc
            JOIN sim_statuses ss ON sc.status_id = ss.id
            LEFT JOIN machine_sim_cards msc ON msc.sim_card_id = sc.id AND msc.removed_at IS NULL
            LEFT JOIN machines m ON msc.machine_id = m.id
            LEFT JOIN machine_models mm ON m.machine_model_id = mm.id
            ORDER BY sc.phone_number
            """
        )
        for row_idx, row in enumerate(sim_cards, 2):
            r = dict(row)
            ws_sim.cell(row=row_idx, column=1, value=r.get("phone_number") or "")
            ws_sim.cell(row=row_idx, column=2, value=r.get("iccid") or "")
            ws_sim.cell(row=row_idx, column=3, value=r.get("operator") or "")
            ws_sim.cell(row=row_idx, column=4, value=r.get("tariff") or "")
            ws_sim.cell(row=row_idx, column=5, value=r.get("description") or "")
            ws_sim.cell(row=row_idx, column=6, value=r.get("ip_address") or "")
            ws_sim.cell(row=row_idx, column=7, value=r.get("status_name") or "")
            ws_sim.cell(row=row_idx, column=8, value=r.get("inventory_number") or "—")
            ws_sim.cell(row=row_idx, column=9, value=r.get("machine_model_name") or "")
            ws_sim.cell(row=row_idx, column=10, value=r.get("installed_at") or "")

        # Лист 4: Техника (справочник)
        ws_mach = wb.create_sheet("Техника")
        mach_headers = ["Инв. номер", "Модель", "Тип техники"]
        for col, h in enumerate(mach_headers, 1):
            c = ws_mach.cell(row=1, column=col, value=h)
            c.font = header_font
            c.border = thin_border
        machines = fetch_all(
            """
            SELECT m.inventory_number, mm.name AS model_name, mt.name AS type_name
            FROM machines m
            JOIN machine_models mm ON m.machine_model_id = mm.id
            JOIN machine_types mt ON mm.machine_type_id = mt.id
            ORDER BY m.inventory_number
            """
        )
        for row_idx, row in enumerate(machines, 2):
            r = dict(row)
            ws_mach.cell(row=row_idx, column=1, value=r.get("inventory_number") or "")
            ws_mach.cell(row=row_idx, column=2, value=r.get("model_name") or "")
            ws_mach.cell(row=row_idx, column=3, value=r.get("type_name") or "")

        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        filename = f"inventory_export_{datetime.now().strftime('%Y-%m-%d_%H-%M')}.xlsx"
        return Response(
            buf.getvalue(),
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    # --- Отчёты и аналитика ---
    @app.route("/reports")
    def reports():
        """Отчёты и аналитика: сводка по складу, графики, выгрузка по периоду."""
        # Оборудование по статусам (для круговой диаграммы)
        equipment_by_status = fetch_all(
            """
            SELECT es.name AS status_name, COUNT(e.id) AS cnt
            FROM equipment e
            JOIN equipment_statuses es ON e.status_id = es.id
            GROUP BY e.status_id
            ORDER BY cnt DESC
            """
        )
        equipment_by_status = [dict(r) for r in equipment_by_status]
        eq_chart_labels = [r["status_name"] for r in equipment_by_status]
        eq_chart_data = [r["cnt"] for r in equipment_by_status]

        # ТО по месяцам (последние 12 месяцев)
        maintenance_by_month = fetch_all(
            """
            SELECT strftime('%Y-%m', performed_at) AS month, COUNT(*) AS cnt
            FROM machine_maintenance
            WHERE performed_at >= date('now', 'localtime', '-12 months')
            GROUP BY strftime('%Y-%m', performed_at)
            ORDER BY month
            """
        )
        month_counts = {r["month"]: r["cnt"] for r in maintenance_by_month}
        month_names = ("янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек")
        maint_months = []
        maint_data = []
        for i in range(11, -1, -1):
            d = date.today()
            month = d.month - i
            year = d.year
            while month < 1:
                month += 12
                year -= 1
            m_str = f"{year}-{month:02d}"
            maint_months.append(f"{month_names[month - 1]} {year}")
            maint_data.append(month_counts.get(m_str, 0))

        # Сводка по складу: числа для блоков
        warehouse_summary = {
            "equipment_total": fetch_one("SELECT COUNT(*) AS c FROM equipment", ())["c"],
            "equipment_warehouse": fetch_one(
                "SELECT COUNT(*) AS c FROM equipment WHERE status_id = 1", ()
            )["c"],
            "equipment_installed": fetch_one(
                "SELECT COUNT(*) AS c FROM equipment WHERE status_id = 2",
                (),
            )["c"],
            "equipment_other": fetch_one(
                "SELECT COUNT(*) AS c FROM equipment WHERE status_id NOT IN (1, 2)", ()
            )["c"],
            "machines_total": fetch_one("SELECT COUNT(*) AS c FROM machines", ())["c"],
            "sim_total": fetch_one("SELECT COUNT(*) AS c FROM sim_cards", ())["c"],
            "sim_warehouse": fetch_one(
                "SELECT COUNT(*) AS c FROM sim_cards WHERE status_id = 1", ()
            )["c"],
        }
        # Оборудование по видам (датчики / прочее)
        equipment_by_kind = fetch_all(
            """
            SELECT equipment_kind, COUNT(*) AS cnt
            FROM equipment
            GROUP BY equipment_kind
            """
        )
        equipment_by_kind = [dict(r) for r in equipment_by_kind]
        # Техника по типам
        machines_by_type = fetch_all(
            """
            SELECT mt.name AS type_name, COUNT(m.id) AS cnt
            FROM machines m
            JOIN machine_models mm ON m.machine_model_id = mm.id
            JOIN machine_types mt ON mm.machine_type_id = mt.id
            GROUP BY mt.id
            ORDER BY cnt DESC
            """
        )
        machines_by_type = [dict(r) for r in machines_by_type]

        today_iso = date.today().isoformat()
        date_from_default = (date.today() - timedelta(days=30)).isoformat()
        date_30_ago = (date.today() - timedelta(days=30)).isoformat()
        # Задания на смену за последние 30 дней: по статусам
        shift_tasks_by_status = fetch_all(
            """
            SELECT status, COUNT(*) AS cnt
            FROM shift_tasks
            WHERE effective_date >= ?
            GROUP BY status
            ORDER BY cnt DESC
            """,
            (date_30_ago,),
        )
        shift_tasks_by_status = [dict(r) for r in shift_tasks_by_status]
        status_labels_ru = {"pending": "Ожидает", "done": "Выполнено", "not_done": "Не выполнено"}
        tasks_status_labels = [status_labels_ru.get(r["status"], r["status"]) for r in shift_tasks_by_status]
        tasks_status_data = [r["cnt"] for r in shift_tasks_by_status]
        # Задания на смену за последние 30 дней: по типам
        shift_tasks_by_type = fetch_all(
            """
            SELECT tt.name AS type_name, COUNT(st.id) AS cnt
            FROM shift_tasks st
            LEFT JOIN shift_task_types tt ON st.task_type_id = tt.id
            WHERE st.effective_date >= ?
            GROUP BY st.task_type_id, tt.name
            ORDER BY cnt DESC
            """,
            (date_30_ago,),
        )
        shift_tasks_by_type = [dict(r) for r in shift_tasks_by_type]
        tasks_type_labels = [r["type_name"] or "—" for r in shift_tasks_by_type]
        tasks_type_data = [r["cnt"] for r in shift_tasks_by_type]
        # Задания по пользователям за 30 дней: создал / выполнил / отметил не выполнено
        created_by_user = fetch_all(
            """
            SELECT u.id AS user_id, COALESCE(NULLIF(TRIM(u.surname || ' ' || u.first_name), ''), u.username) AS user_name, COUNT(st.id) AS cnt
            FROM shift_tasks st
            JOIN users u ON st.created_by_user_id = u.id
            WHERE st.effective_date >= ?
            GROUP BY u.id
            """,
            (date_30_ago,),
        )
        completed_by_user = fetch_all(
            """
            SELECT u.id AS user_id, COALESCE(NULLIF(TRIM(u.surname || ' ' || u.first_name), ''), u.username) AS user_name, COUNT(c.id) AS cnt
            FROM shift_task_completions c
            JOIN users u ON c.user_id = u.id
            JOIN shift_tasks st ON c.shift_task_id = st.id
            WHERE st.effective_date >= ?
            GROUP BY u.id
            """,
            (date_30_ago,),
        )
        not_done_by_user = fetch_all(
            """
            SELECT u.id AS user_id, COALESCE(NULLIF(TRIM(u.surname || ' ' || u.first_name), ''), u.username) AS user_name, COUNT(st.id) AS cnt
            FROM shift_tasks st
            JOIN users u ON st.not_done_by_user_id = u.id
            WHERE st.effective_date >= ? AND st.status = 'not_done'
            GROUP BY u.id
            """,
            (date_30_ago,),
        )
        user_ids = set()
        for r in created_by_user:
            user_ids.add(r["user_id"])
        for r in completed_by_user:
            user_ids.add(r["user_id"])
        for r in not_done_by_user:
            user_ids.add(r["user_id"])
        created_map = {r["user_id"]: (r["user_name"], r["cnt"]) for r in created_by_user}
        completed_map = {r["user_id"]: (r["user_name"], r["cnt"]) for r in completed_by_user}
        not_done_map = {r["user_id"]: (r["user_name"], r["cnt"]) for r in not_done_by_user}
        tasks_by_user = []
        for uid in user_ids:
            name = (created_map.get(uid) or completed_map.get(uid) or not_done_map.get(uid))[0]
            created = created_map[uid][1] if uid in created_map else 0
            completed = completed_map[uid][1] if uid in completed_map else 0
            not_done = not_done_map[uid][1] if uid in not_done_map else 0
            tasks_by_user.append(
                {"user_id": uid, "user_name": name, "created": created, "completed": completed, "not_done": not_done}
            )
        tasks_by_user.sort(key=lambda x: (x["completed"], x["created"]), reverse=True)
        return render_template(
            "reports.html",
            equipment_by_status=equipment_by_status,
            eq_chart_labels=eq_chart_labels,
            eq_chart_data=eq_chart_data,
            maint_chart_labels=maint_months,
            maint_chart_data=maint_data,
            warehouse_summary=warehouse_summary,
            equipment_by_kind=equipment_by_kind,
            machines_by_type=machines_by_type,
            date_from_default=date_from_default,
            date_to_default=today_iso,
            tasks_status_labels=tasks_status_labels,
            tasks_status_data=tasks_status_data,
            tasks_type_labels=tasks_type_labels,
            tasks_type_data=tasks_type_data,
            tasks_by_user=tasks_by_user,
        )

    @app.route("/reports/export")
    def reports_export():
        """Выгрузка отчёта за период: операции, ТО, техника, оборудование."""
        report_type = (request.args.get("type") or "operations").strip().lower()
        if report_type not in ("operations", "maintenance", "machines", "equipment"):
            report_type = "operations"
        date_from = (request.args.get("from") or "").strip()[:10]
        date_to = (request.args.get("to") or "").strip()[:10]
        if not date_from:
            date_from = (date.today() - timedelta(days=30)).isoformat()
        if not date_to:
            date_to = date.today().isoformat()

        if report_type == "operations":
            rows = fetch_all(
                """
                SELECT datetime(created_at, 'localtime') AS created_at, operation_type, entity_type, entity_id, description, username
                FROM operations_log
                WHERE date(created_at) BETWEEN ? AND ?
                ORDER BY created_at DESC
                """,
                (date_from, date_to),
            )
            rows = [dict(r) for r in rows]
            buf = io.StringIO()
            buf.write("\ufeff")
            writer = csv.writer(buf, delimiter=";", quoting=csv.QUOTE_MINIMAL)
            writer.writerow(["Дата", "Операция", "Сущность", "ID", "Описание", "Кто"])
            for r in rows:
                writer.writerow([
                    r.get("created_at") or "",
                    r.get("operation_type") or "",
                    r.get("entity_type") or "",
                    r.get("entity_id") or "",
                    (r.get("description") or "").replace("\n", " "),
                    r.get("username") or "",
                ])
            filename = f"operations_{date_from}_{date_to}.csv"
            mimetype = "text/csv; charset=utf-8"
        elif report_type == "maintenance":
            rows = fetch_all(
                """
                SELECT m.inventory_number, mm.name AS model_name, mt.name AS type_name,
                       mmt.performed_at, mmt.next_due_at, mmt.performed_by, mmt.remarks
                FROM machine_maintenance mmt
                JOIN machines m ON mmt.machine_id = m.id
                JOIN machine_models mm ON m.machine_model_id = mm.id
                JOIN machine_types mt ON mm.machine_type_id = mt.id
                WHERE date(mmt.performed_at) BETWEEN ? AND ?
                ORDER BY mmt.performed_at DESC
                """,
                (date_from, date_to),
            )
            rows = [dict(r) for r in rows]
            buf = io.StringIO()
            buf.write("\ufeff")
            writer = csv.writer(buf, delimiter=";", quoting=csv.QUOTE_MINIMAL)
            writer.writerow(["Инв. номер", "Модель", "Тип", "Дата проведения", "Следующее ТО", "Исполнитель", "Замечания"])
            for r in rows:
                writer.writerow([
                    r.get("inventory_number") or "",
                    r.get("model_name") or "",
                    r.get("type_name") or "",
                    r.get("performed_at") or "",
                    r.get("next_due_at") or "",
                    r.get("performed_by") or "",
                    (r.get("remarks") or "").replace("\n", " "),
                ])
            filename = f"maintenance_{date_from}_{date_to}.csv"
            mimetype = "text/csv; charset=utf-8"
        elif report_type == "machines":
            rows = fetch_all(
                """
                SELECT m.inventory_number, mm.name AS model_name, mt.name AS type_name
                FROM machines m
                JOIN machine_models mm ON m.machine_model_id = mm.id
                JOIN machine_types mt ON mm.machine_type_id = mt.id
                ORDER BY m.inventory_number
                """
            )
            rows = [dict(r) for r in rows]
            buf = io.StringIO()
            buf.write("\ufeff")
            writer = csv.writer(buf, delimiter=";", quoting=csv.QUOTE_MINIMAL)
            writer.writerow(["Инв. номер", "Модель", "Тип техники"])
            for r in rows:
                writer.writerow([r.get("inventory_number") or "", r.get("model_name") or "", r.get("type_name") or ""])
            filename = f"machines_{date.today().isoformat()}.csv"
            mimetype = "text/csv; charset=utf-8"
        else:
            rows = fetch_all(
                """
                SELECT e.name, e.serial_number, es.name AS status_name, e.equipment_kind, e.added_at
                FROM equipment e
                JOIN equipment_statuses es ON e.status_id = es.id
                ORDER BY e.name
                """
            )
            rows = [dict(r) for r in rows]
            buf = io.StringIO()
            buf.write("\ufeff")
            writer = csv.writer(buf, delimiter=";", quoting=csv.QUOTE_MINIMAL)
            writer.writerow(["Наименование", "Серийный номер", "Статус", "Вид", "Дата добавления"])
            for r in rows:
                writer.writerow([
                    r.get("name") or "",
                    r.get("serial_number") or "",
                    r.get("status_name") or "",
                    "Датчик" if r.get("equipment_kind") == "sensor" else "Прочее",
                    r.get("added_at") or "",
                ])
            filename = f"equipment_{date.today().isoformat()}.csv"
            mimetype = "text/csv; charset=utf-8"

        return Response(
            buf.getvalue(),
            mimetype=mimetype,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
