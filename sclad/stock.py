"""Минимальные остатки склада: порог по наименованию оборудования и для SIM-карт."""
from .db import fetch_all

SIM_KEY = "__sim__"   # особое «наименование» в stock_minimums — порог для SIM-карт
SIM_LABEL = "SIM-карты"


def stock_rows():
    """Остатки по наименованиям: на складе / установлено / неисправно и минимум.

    Наименование сравнивается точно (пробелы по краям не важны). «На складе» — свободное
    со статусом «На складе» (только его можно установить). Последняя строка — SIM-карты.
    """
    rows = [
        dict(r)
        for r in fetch_all(
            """
            WITH eq AS (
                SELECT TRIM(e.name) AS name, e.status_id,
                       EXISTS (SELECT 1 FROM machine_equipment me
                               WHERE me.equipment_id = e.id AND me.removed_at IS NULL) AS busy
                FROM equipment e
                WHERE e.status_id <> 5
            ),
            names AS (
                SELECT name FROM eq
                UNION
                SELECT name FROM stock_minimums WHERE name <> :sim
            )
            SELECT n.name,
                   (SELECT COUNT(*) FROM eq WHERE eq.name = n.name AND eq.status_id = 1 AND NOT eq.busy) AS free,
                   (SELECT COUNT(*) FROM eq WHERE eq.name = n.name AND eq.status_id = 2) AS installed,
                   (SELECT COUNT(*) FROM eq WHERE eq.name = n.name AND eq.status_id = 4) AS faulty,
                   (SELECT min_qty FROM stock_minimums sm WHERE sm.name = n.name) AS min_qty
            FROM names n
            ORDER BY n.name
            """,
            {"sim": SIM_KEY},
        )
    ]
    sim = fetch_all(
        """
        SELECT
            (SELECT COUNT(*) FROM sim_cards sc WHERE sc.status_id = 1 AND NOT EXISTS (
                SELECT 1 FROM machine_sim_cards msc WHERE msc.sim_card_id = sc.id AND msc.removed_at IS NULL)) AS free,
            (SELECT COUNT(*) FROM sim_cards WHERE status_id = 2) AS installed,
            (SELECT COUNT(*) FROM sim_cards WHERE status_id = 3) AS faulty,
            (SELECT min_qty FROM stock_minimums WHERE name = ?) AS min_qty
        """,
        (SIM_KEY,),
    )[0]
    rows.append(dict(sim, name=SIM_LABEL, key=SIM_KEY, is_sim=True))
    for r in rows:
        r.setdefault("key", r["name"])
        r.setdefault("is_sim", False)
        r["deficit"] = max(0, (r["min_qty"] or 0) - r["free"]) if r["min_qty"] else 0
    return rows


def shortages():
    """Наименования, которых на складе меньше минимума (больший недостаток — выше)."""
    return sorted((r for r in stock_rows() if r["deficit"]), key=lambda r: (-r["deficit"], r["name"].lower()))
