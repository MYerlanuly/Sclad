"""Повторные отказы: позиции, где датчики меняют часто, и оборудование, которое снимали неисправным не раз.

Отказ — демонтаж с причиной «Неисправен» или «Повреждён». Замена — любой демонтаж с позиции,
кроме возврата исправного на склад и вывода борта из работы (старые записи без причины — тоже замены).
"""
from datetime import date, timedelta

from .db import fetch_all
from .removal import reason_text

FAULT_REASONS = ("faulty", "damaged")
NOT_REPLACEMENT = ("to_stock", "machine_out")
PERIODS = (30, 90, 180, 365)
DEFAULT_DAYS = 90
MIN_REPEATS = 2


def period_start(days):
    return (date.today() - timedelta(days=days)).isoformat()


def _in(values):
    return ", ".join(f"'{v}'" for v in values)  # константы модуля, не ввод пользователя


def frequent_positions(days, machine_id=None):
    """Позиции бортов, с которых за период сняли 2 и больше датчиков."""
    since = period_start(days)
    extra = "AND me.machine_id = ?" if machine_id else ""
    params = (since, machine_id, MIN_REPEATS) if machine_id else (since, MIN_REPEATS)
    rows = fetch_all(
        f"""
        SELECT me.machine_id, m.inventory_number, me.sensor_position_id AS position_id,
               sp.code AS position_code, sp.label AS position_label,
               COUNT(*) AS replacements,
               SUM(CASE WHEN me.removal_reason IN ({_in(FAULT_REASONS)}) THEN 1 ELSE 0 END) AS faults,
               MAX(me.removed_at) AS last_at
        FROM machine_equipment me
        JOIN machines m ON me.machine_id = m.id
        JOIN sensor_positions sp ON me.sensor_position_id = sp.id
        WHERE me.removed_at IS NOT NULL AND me.removed_at >= ?
          AND IFNULL(me.removal_reason, '') NOT IN ({_in(NOT_REPLACEMENT)}) {extra}
        GROUP BY me.machine_id, me.sensor_position_id
        HAVING COUNT(*) >= ?
        ORDER BY replacements DESC, faults DESC, last_at DESC
        """,
        params,
    )
    result = []
    for r in rows:
        item = dict(r)
        item["notes"] = _position_notes(item["machine_id"], item["position_id"], since)
        result.append(item)
    return result


def _position_notes(machine_id, position_id, since):
    """Что снимали с позиции за период: серийный номер и причина (новые сверху)."""
    return [
        {"serial": r["serial_number"], "equipment_id": r["equipment_id"], "at": r["removed_at"],
         "reason": reason_text("equipment", r["removal_reason"], r["removal_comment"])}
        for r in fetch_all(
            f"""
            SELECT me.equipment_id, e.serial_number, me.removed_at, me.removal_reason, me.removal_comment
            FROM machine_equipment me JOIN equipment e ON me.equipment_id = e.id
            WHERE me.machine_id = ? AND me.sensor_position_id = ? AND me.removed_at >= ?
              AND IFNULL(me.removal_reason, '') NOT IN ({_in(NOT_REPLACEMENT)})
            ORDER BY me.removed_at DESC
            """,
            (machine_id, position_id, since),
        )
    ]


def repeated_faulty_equipment(days):
    """Оборудование, которое за период снимали неисправным 2 и больше раз."""
    return [
        dict(r)
        for r in fetch_all(
            f"""
            SELECT e.id, e.name, e.serial_number, es.name AS status_name,
                   COUNT(*) AS faults, COUNT(DISTINCT me.machine_id) AS machines, MAX(me.removed_at) AS last_at,
                   GROUP_CONCAT(DISTINCT m.inventory_number) AS boards
            FROM machine_equipment me
            JOIN equipment e ON me.equipment_id = e.id
            LEFT JOIN equipment_statuses es ON e.status_id = es.id
            JOIN machines m ON me.machine_id = m.id
            WHERE me.removal_reason IN ({_in(FAULT_REASONS)}) AND me.removed_at >= ?
            GROUP BY e.id
            HAVING COUNT(*) >= ?
            ORDER BY faults DESC, last_at DESC
            """,
            (period_start(days), MIN_REPEATS),
        )
    ]


def machines_by_faults(days, limit=10):
    """Борта с наибольшим числом отказов за период."""
    return [
        dict(r)
        for r in fetch_all(
            f"""
            SELECT m.id, m.inventory_number, mm.name AS model_name, COUNT(*) AS faults,
                   COUNT(DISTINCT me.sensor_position_id) AS positions, MAX(me.removed_at) AS last_at
            FROM machine_equipment me
            JOIN machines m ON me.machine_id = m.id
            LEFT JOIN machine_models mm ON m.machine_model_id = mm.id
            WHERE me.removal_reason IN ({_in(FAULT_REASONS)}) AND me.removed_at >= ?
            GROUP BY m.id
            ORDER BY faults DESC, last_at DESC
            LIMIT ?
            """,
            (period_start(days), limit),
        )
    ]


def position_replacement_counts(machine_id, days=DEFAULT_DAYS):
    """{id позиции: число замен за период} для плиток колёс в карточке борта (только 2 и больше)."""
    return {r["position_id"]: r["replacements"] for r in frequent_positions(days, machine_id)}
