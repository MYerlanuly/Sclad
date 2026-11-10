"""Маршруты: reports."""
import csv
import io
import sqlite3
from datetime import date, datetime, timedelta

from flask import Response, render_template, request
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from ..db import fetch_all, fetch_one
from ..report_analytics import DEFAULT_PERIOD, build_report
from ..utils import fix_mojibake_text, to_int


def _polish_sheet(ws):
    """Единый вид листов выгрузки: шапка, закреплённая строка, автофильтр, ширина колонок."""
    head_fill = PatternFill("solid", fgColor="0F172A")
    head_font = Font(bold=True, color="FFFFFF")
    line = Side(style="thin", color="E2E8F0")
    for cell in ws[1]:
        cell.fill = head_fill
        cell.font = head_font
        cell.alignment = Alignment(vertical="center", wrap_text=True)
        cell.border = Border(bottom=line)
    ws.row_dimensions[1].height = 30
    ws.freeze_panes = "A2"
    if ws.max_row > 1:
        ws.auto_filter.ref = ws.dimensions
    for idx, col in enumerate(ws.iter_cols(min_row=1, max_row=min(ws.max_row, 500)), 1):
        width = max((len(str(c.value)) for c in col if c.value is not None), default=8)
        ws.column_dimensions[get_column_letter(idx)].width = max(8, min(48, width + 2))


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

        for ws in wb.worksheets:
            _polish_sheet(ws)
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
        """Аналитический отчёт: KPI, динамика, проблемы и рекомендации за выбранный период."""
        days = to_int(request.args.get("days"), DEFAULT_PERIOD)
        report = build_report(days)
        return render_template(
            "reports.html",
            r=report,
            generated_at=datetime.now(),
            date_from_default=report["date_from"],
            date_to_default=report["date_to"],
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
