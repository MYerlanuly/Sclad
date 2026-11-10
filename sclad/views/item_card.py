"""Общее для карточек оборудования и SIM-карты: периоды установок, журнал, выбор борта."""
from datetime import date

from ..db import fetch_all
from .dashboard import OPERATION_LABELS

# Тон плашки статуса (как на карточке борта): ok — зелёный, info — синий, warning — жёлтый,
# critical — красный, zero — серый. Ключ — id статуса (id фиксированы в schema.sql).
EQUIPMENT_STATUS_TONE = {1: "info", 2: "ok", 3: "warning", 4: "critical", 5: "zero"}
SIM_STATUS_TONE = {1: "info", 2: "ok", 3: "critical", 4: "zero"}

# Операции, которые в журнале карточки называются иначе, чем на главной
_CARD_OPERATION_LABELS = dict(OPERATION_LABELS, status_change="Статус")


def _day(value):
    try:
        return date.fromisoformat(str(value or "").strip()[:10])
    except ValueError:
        return None


def install_periods(rows):
    """Установки на борта (новые сверху) со сроком в днях; текущая — с active=True.

    rows — строки machine_equipment / machine_sim_cards с inventory_number, model_name, type_name.
    """
    today = date.today()
    periods = []
    for r in rows:
        p = dict(r)
        start, end = _day(p.get("installed_at")), _day(p.get("removed_at"))
        p["active"] = not p.get("removed_at")
        if start and p["active"]:
            p["days"] = max(0, (today - start).days)
        elif start and end:
            p["days"] = max(0, (end - start).days)
        else:
            p["days"] = None
        periods.append(p)
    return periods


def card_summary(periods):
    """Плитки: текущая установка, сколько раз ставили, на скольких бортах, дней на технике всего."""
    current = next((p for p in periods if p["active"]), None)
    return {
        "current": current,
        "installs_total": len(periods),
        "machines_total": len({p["machine_id"] for p in periods}),
        "days_on_machines": sum(p["days"] or 0 for p in periods),
    }


def card_logs(entity_type, entity_id, limit=50):
    """Журнал операций по объекту (время — местное, как на главной)."""
    logs = []
    for r in fetch_all(
        """
        SELECT id, datetime(created_at, 'localtime') AS created_at, operation_type, description, username
        FROM operations_log
        WHERE entity_type = ? AND entity_id = ?
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        (entity_type, entity_id, limit),
    ):
        op = dict(r)
        op["label"] = _CARD_OPERATION_LABELS.get(op["operation_type"], op["operation_type"])
        logs.append(op)
    return logs


def machines_for_pick(without_sim=False):
    """Борта для «Установить на борт»: инв. номер и модель (для SIM — только борта без SIM)."""
    extra = (
        "WHERE NOT EXISTS (SELECT 1 FROM machine_sim_cards msc WHERE msc.machine_id = m.id AND msc.removed_at IS NULL)"
        if without_sim
        else ""
    )
    return fetch_all(
        f"""
        SELECT m.id, m.inventory_number, mm.name AS model_name, mt.name AS type_name
        FROM machines m
        LEFT JOIN machine_models mm ON m.machine_model_id = mm.id
        LEFT JOIN machine_types mt ON mm.machine_type_id = mt.id
        {extra}
        ORDER BY m.inventory_number
        """
    )
