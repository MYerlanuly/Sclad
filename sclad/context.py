"""Данные, доступные во всех шаблонах."""
from flask import session, url_for


def register(app):
    @app.context_processor
    def inject_tooltips_and_recent():
        """Подсказки для статусов и список недавно открытых для шапки."""
        hints_equipment = {
            "На складе": "Оборудование на складе, не установлено на технику",
            "Установлено": "Установлено на технику",
            "Демонтировано": "Снято с техники",
            "Неисправно": "Требует ремонта или диагностики",
            "Списано": "Снято с учёта",
        }
        hints_sim = {
            "На складе": "SIM-карта на складе",
            "Установлена": "Установлена на технику",
            "Заблокирована": "SIM заблокирована оператором",
            "Списана": "Снята с учёта",
        }
        recent = session.get("recent") or []
        recent_items = []
        for item in recent[-10:]:  # последние 10
            t, iid, label = item.get("type"), item.get("id"), item.get("label") or ""
            if t == "machine" and iid:
                recent_items.append({"url": url_for("machine_detail", machine_id=iid), "label": label or f"Техника {iid}"})
            elif t == "equipment" and iid:
                recent_items.append({"url": url_for("equipment_detail", equipment_id=iid), "label": label or f"Оборудование {iid}"})
        return {
            "status_hints_equipment": hints_equipment,
            "status_hints_sim": hints_sim,
            "recent_items": list(reversed(recent_items)),  # новые сверху
        }

    # ===== Simple helpers for lookups =====
