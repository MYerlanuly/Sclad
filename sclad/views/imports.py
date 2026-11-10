"""Маршруты: imports."""
import csv
import io

from flask import Response, flash, redirect, render_template, request, url_for

from ..db import fetch_all, fetch_one, get_db


def register(app):
    # ===== Массовый импорт техники и оборудования =====
    @app.route("/import")
    def import_page():
        """Страница массового ввода: шаблоны и загрузка CSV."""
        return render_template("import.html")

    @app.route("/import/machines/template")
    def import_machines_template():
        """Скачать шаблон CSV для импорта техники. Колонки: Инв. номер; Модель."""
        buf = io.StringIO()
        buf.write("\ufeff")
        writer = csv.writer(buf, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        writer.writerow(["Инв. номер", "Модель"])
        writer.writerow(["A-001", "БелАЗ-7555"])
        writer.writerow(["A-002", "БелАЗ-7555"])
        buf.seek(0)
        return Response(
            buf.getvalue().encode("utf-8"),
            mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="import_machines_template.csv"'},
        )

    @app.route("/import/machines", methods=["POST"])
    def import_machines():
        """Импорт техники из CSV. Файл: разделитель ;, UTF-8, первая строка — заголовки (Инв. номер, Модель)."""
        if "file" not in request.files:
            flash("Выберите файл.", "error")
            return redirect(url_for("import_page"))
        f = request.files["file"]
        if not f or not f.filename:
            flash("Выберите файл.", "error")
            return redirect(url_for("import_page"))
        try:
            data = f.read()
            if data.startswith(b"\xef\xbb\xbf"):
                data = data[3:]
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = data.decode("cp1251")
            except Exception:
                flash("Файл должен быть в кодировке UTF-8 или Windows-1251.", "error")
                return redirect(url_for("import_page"))
        models_by_name = {}
        for row in fetch_all("SELECT id, name FROM machine_models"):
            r = dict(row)
            models_by_name[r["name"].strip().lower()] = r["id"]
        lines = [line.strip() for line in text.replace("\r", "\n").split("\n") if line.strip()]
        if not lines:
            flash("В файле нет строк.", "error")
            return redirect(url_for("import_page"))
        reader = csv.reader(io.StringIO("\n".join(lines)), delimiter=";", quoting=csv.QUOTE_MINIMAL)
        rows = list(reader)
        header = rows[0] if rows else []
        h0 = (header[0] or "").lower() if len(header) > 0 else ""
        h1 = (header[1] or "").lower() if len(header) > 1 else ""
        if not header or ("инв" not in h0 and "номер" not in h0 and "модель" not in h1):
            data_rows = rows
        else:
            data_rows = rows[1:]
        added, skipped, errors = 0, 0, []
        db = get_db()
        for i, row in enumerate(data_rows):
            inv = (row[0] or "").strip() if len(row) > 0 else ""
            model_name = (row[1] or "").strip() if len(row) > 1 else ""
            if not inv:
                skipped += 1
                errors.append(f"Строка {i + 1}: не указан инв. номер")
                continue
            if not model_name:
                skipped += 1
                errors.append(f"Строка {i + 1} ({inv}): не указана модель")
                continue
            model_id = models_by_name.get(model_name.lower())
            if not model_id:
                skipped += 1
                errors.append(f"Строка {i + 1} ({inv}): модель «{model_name}» не найдена")
                continue
            existing = fetch_one("SELECT id FROM machines WHERE inventory_number = ?", (inv,))
            if existing:
                skipped += 1
                errors.append(f"Строка {i + 1} ({inv}): такой инв. номер уже есть")
                continue
            try:
                db.execute(
                    "INSERT INTO machines (inventory_number, machine_model_id) VALUES (?, ?)",
                    (inv, model_id),
                )
                db.commit()
                added += 1
            except Exception as e:
                db.rollback()
                skipped += 1
                errors.append(f"Строка {i + 1} ({inv}): {e}")
        if added:
            flash(f"Добавлено единиц техники: {added}.", "success")
        if skipped:
            flash(f"Пропущено: {skipped}. " + ("Первые ошибки: " + "; ".join(errors[:5]) if errors else ""), "error" if errors else "info")
        return redirect(url_for("import_page"))

    @app.route("/import/equipment/template")
    def import_equipment_template():
        """Скачать шаблон CSV для импорта оборудования. Колонки: Наименование; Серийный номер; Описание; Статус; Вид (Датчик/Прочее)."""
        buf = io.StringIO()
        buf.write("\ufeff")
        writer = csv.writer(buf, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        writer.writerow(["Наименование", "Серийный номер", "Описание", "Статус", "Вид"])
        writer.writerow(["Терминал ГЛОНАСС", "SN-100001", "Основной терминал", "На складе", "Прочее"])
        writer.writerow(["Датчик давления", "SN-200001", "Колесо ПЛ", "На складе", "Датчик"])
        writer.writerow(["Датчик температуры", "SN-200002", "", "На складе", "Датчик"])
        buf.seek(0)
        return Response(
            buf.getvalue().encode("utf-8"),
            mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="import_equipment_template.csv"'},
        )

    @app.route("/import/equipment", methods=["POST"])
    def import_equipment():
        """Импорт оборудования из CSV. Колонки: Наименование; Серийный номер; Описание; Статус; Вид (Датчик/Прочее)."""
        if "file" not in request.files:
            flash("Выберите файл.", "error")
            return redirect(url_for("import_page"))
        f = request.files["file"]
        if not f or not f.filename:
            flash("Выберите файл.", "error")
            return redirect(url_for("import_page"))
        try:
            data = f.read()
            if data.startswith(b"\xef\xbb\xbf"):
                data = data[3:]
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = data.decode("cp1251")
            except Exception:
                flash("Файл должен быть в кодировке UTF-8 или Windows-1251.", "error")
                return redirect(url_for("import_page"))
        statuses_by_name = {}
        for row in fetch_all("SELECT id, name FROM equipment_statuses"):
            r = dict(row)
            statuses_by_name[r["name"].strip().lower()] = r["id"]
        default_status_id = statuses_by_name.get("на складе") or next(iter(statuses_by_name.values()), None)
        if not default_status_id:
            flash("В справочнике нет статуса оборудования.", "error")
            return redirect(url_for("import_page"))
        lines = [line.strip() for line in text.replace("\r", "\n").split("\n") if line.strip()]
        if not lines:
            flash("В файле нет строк.", "error")
            return redirect(url_for("import_page"))
        reader = csv.reader(io.StringIO("\n".join(lines)), delimiter=";", quoting=csv.QUOTE_MINIMAL)
        rows = list(reader)
        header = rows[0] if rows else []
        eh0 = (header[0] or "").lower() if len(header) > 0 else ""
        eh1 = (header[1] or "").lower() if len(header) > 1 else ""
        if not header or ("наименование" not in eh0 and "серий" not in eh1):
            data_rows = rows
        else:
            data_rows = rows[1:]
        added, skipped, errors = 0, 0, []
        db = get_db()
        for i, row in enumerate(data_rows):
            name = (row[0] or "").strip() if len(row) > 0 else ""
            serial = (row[1] or "").strip() if len(row) > 1 else ""
            description = (row[2] or "").strip() if len(row) > 2 else ""
            status_name = (row[3] or "").strip() if len(row) > 3 else "на складе"
            kind_raw = (row[4] or "").strip().lower() if len(row) > 4 else ""
            if kind_raw in ("датчик", "да", "sensor", "1", "д"):
                equipment_kind = "sensor"
            else:
                equipment_kind = "other"
            if not name or not serial:
                skipped += 1
                errors.append(f"Строка {i + 1}: не указаны наименование или серийный номер")
                continue
            status_id = statuses_by_name.get(status_name.lower()) if status_name else default_status_id
            if not status_id:
                status_id = default_status_id
            existing = fetch_one("SELECT id FROM equipment WHERE name = ? AND serial_number = ?", (name, serial))
            if existing:
                skipped += 1
                errors.append(f"Строка {i + 1} ({name} / {serial}): такая пара уже есть")
                continue
            try:
                db.execute(
                    "INSERT INTO equipment (name, serial_number, description, status_id, equipment_kind) VALUES (?, ?, ?, ?, ?)",
                    (name, serial, description or None, status_id, equipment_kind),
                )
                db.commit()
                added += 1
            except Exception as e:
                db.rollback()
                skipped += 1
                errors.append(f"Строка {i + 1}: {e}")
        if added:
            flash(f"Добавлено единиц оборудования: {added}.", "success")
        if skipped:
            flash(f"Пропущено: {skipped}. " + ("Первые ошибки: " + "; ".join(errors[:5]) if errors else ""), "error" if errors else "info")
        return redirect(url_for("import_page"))

    @app.route("/import/sim_cards/template")
    def import_sim_cards_template():
        """Скачать шаблон CSV для импорта SIM-карт. Колонки: Номер; ICCID; IP; Оператор; Описание; Тариф; Статус."""
        buf = io.StringIO()
        buf.write("\ufeff")
        writer = csv.writer(buf, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        writer.writerow(["Номер", "ICCID", "IP", "Оператор", "Описание", "Тариф", "Статус"])
        writer.writerow(["+79001234567", "89912345678901234567", "10.0.0.1", "Kcell", "Основная", "Безлимит", "На складе"])
        writer.writerow(["+79009876543", "89912345678901234568", "", "Билайн", "", "", "На складе"])
        buf.seek(0)
        return Response(
            buf.getvalue().encode("utf-8"),
            mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="import_sim_cards_template.csv"'},
        )

    @app.route("/import/sim_cards", methods=["POST"])
    def import_sim_cards():
        """Импорт SIM-карт из CSV. Колонки: Номер; ICCID; IP; Оператор; Описание; Тариф; Статус (по умолчанию На складе)."""
        if "file" not in request.files:
            flash("Выберите файл.", "error")
            return redirect(url_for("import_page"))
        f = request.files["file"]
        if not f or not f.filename:
            flash("Выберите файл.", "error")
            return redirect(url_for("import_page"))
        try:
            data = f.read()
            if data.startswith(b"\xef\xbb\xbf"):
                data = data[3:]
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = data.decode("cp1251")
            except Exception:
                flash("Файл должен быть в кодировке UTF-8 или Windows-1251.", "error")
                return redirect(url_for("import_page"))
        statuses_by_name = {}
        for row in fetch_all("SELECT id, name FROM sim_statuses"):
            r = dict(row)
            statuses_by_name[r["name"].strip().lower()] = r["id"]
        default_status_id = statuses_by_name.get("на складе") or next(iter(statuses_by_name.values()), None)
        if not default_status_id:
            flash("В справочнике нет статуса SIM-карт.", "error")
            return redirect(url_for("import_page"))
        lines = [line.strip() for line in text.replace("\r", "\n").split("\n") if line.strip()]
        if not lines:
            flash("В файле нет строк.", "error")
            return redirect(url_for("import_page"))
        reader = csv.reader(io.StringIO("\n".join(lines)), delimiter=";", quoting=csv.QUOTE_MINIMAL)
        rows = list(reader)
        header = rows[0] if rows else []
        sh0 = (header[0] or "").lower() if len(header) > 0 else ""
        sh1 = (header[1] or "").lower() if len(header) > 1 else ""
        if not header or ("номер" not in sh0 and "iccid" not in sh1 and "номер" not in sh1):
            data_rows = rows
        else:
            data_rows = rows[1:]
        added, skipped, errors = 0, 0, []
        db = get_db()
        for i, row in enumerate(data_rows):
            phone_number = (row[0] or "").strip() if len(row) > 0 else ""
            iccid = (row[1] or "").strip() if len(row) > 1 else ""
            ip_address = ((row[2] or "").strip() or None) if len(row) > 2 else None
            operator = ((row[3] or "").strip() or None) if len(row) > 3 else None
            description = ((row[4] or "").strip() or None) if len(row) > 4 else None
            tariff = ((row[5] or "").strip() or None) if len(row) > 5 else None
            status_name = (row[6] or "").strip() if len(row) > 6 else "на складе"
            if not phone_number or not iccid:
                skipped += 1
                errors.append(f"Строка {i + 1}: не указаны номер или ICCID")
                continue
            status_id = statuses_by_name.get(status_name.lower()) if status_name else default_status_id
            if not status_id:
                status_id = default_status_id
            existing = fetch_one("SELECT id FROM sim_cards WHERE iccid = ?", (iccid,))
            if existing:
                skipped += 1
                errors.append(f"Строка {i + 1} ({iccid}): такой ICCID уже есть")
                continue
            try:
                db.execute(
                    """
                    INSERT INTO sim_cards (phone_number, iccid, ip_address, operator, description, tariff, status_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (phone_number, iccid, ip_address, operator, description, tariff, status_id),
                )
                db.commit()
                added += 1
            except Exception as e:
                db.rollback()
                skipped += 1
                errors.append(f"Строка {i + 1}: {e}")
        if added:
            flash(f"Добавлено SIM-карт: {added}.", "success")
        if skipped:
            flash(f"Пропущено: {skipped}. " + ("Первые ошибки: " + "; ".join(errors[:5]) if errors else ""), "error" if errors else "info")
        return redirect(url_for("import_page"))
