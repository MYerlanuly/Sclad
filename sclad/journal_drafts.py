"""Черновик журнала смены: что сделано за смену по данным склада.

Источники (за время смены, по часам сервера):
  * выполненные задания на смену (отметки исполнителей);
  * установки, снятия и замены оборудования и SIM на бортах;
  * записи ТО.
Инженер отмечает нужные строки, при необходимости правит текст и добавляет их в журнал.
"""
from datetime import date, datetime, timedelta

from .db import fetch_all
from .removal import reason_text
from .shift_journal import DAY_START, NIGHT_START


def shift_window(shift, shift_date):
    """Начало и конец смены строками 'YYYY-MM-DD HH:MM:SS' (конец не включается)."""
    d = date.fromisoformat(shift_date)
    if shift == "day":
        start, end = datetime.combine(d, DAY_START), datetime.combine(d, NIGHT_START)
    else:
        start, end = datetime.combine(d, NIGHT_START), datetime.combine(d + timedelta(days=1), DAY_START)
    fmt = "%Y-%m-%d %H:%M:%S"
    return start.strftime(fmt), end.strftime(fmt)


def _hhmm(ts):
    return str(ts or "")[11:16]


def _equipment_moves(start, end):
    """Установки и снятия оборудования за смену; замена (снятие и установка на то же место в ту же секунду) — одной строкой."""
    removed = [
        dict(r) for r in fetch_all(
            """
            SELECT me.id, me.machine_id, me.sensor_position_id, me.removed_at AS at, me.removal_reason, me.removal_comment,
                   e.name, e.serial_number, m.inventory_number, sp.code AS position_code
            FROM machine_equipment me
            JOIN equipment e ON me.equipment_id = e.id
            JOIN machines m ON me.machine_id = m.id
            LEFT JOIN sensor_positions sp ON me.sensor_position_id = sp.id
            WHERE me.removed_at >= ? AND me.removed_at < ?
            """,
            (start, end),
        )
    ]
    installed = [
        dict(r) for r in fetch_all(
            """
            SELECT me.id, me.machine_id, me.sensor_position_id, me.installed_at AS at,
                   e.name, e.serial_number, m.inventory_number, sp.code AS position_code
            FROM machine_equipment me
            JOIN equipment e ON me.equipment_id = e.id
            JOIN machines m ON me.machine_id = m.id
            LEFT JOIN sensor_positions sp ON me.sensor_position_id = sp.id
            WHERE me.installed_at >= ? AND me.installed_at < ?
            """,
            (start, end),
        )
    ]
    drafts = []
    paired = set()
    for r in removed:
        why = reason_text("equipment", r["removal_reason"], r["removal_comment"])
        where = f"{r['inventory_number']}, {r['position_code']}" if r["position_code"] else r["inventory_number"]
        new = next(
            (i for i in installed
             if i["id"] not in paired and i["machine_id"] == r["machine_id"]
             and i["sensor_position_id"] == r["sensor_position_id"] and i["at"] == r["at"]),
            None,
        )
        if new:
            paired.add(new["id"])
            text = f"{where}: замена {r['name']} {r['serial_number']} → {new['serial_number']}"
            drafts.append({"key": f"rep-{r['id']}", "at": r["at"], "content": text, "note": why})
        else:
            text = f"{where}: снят {r['name']} {r['serial_number']}"
            drafts.append({"key": f"rem-{r['id']}", "at": r["at"], "content": text, "note": why})
    for i in installed:
        if i["id"] in paired:
            continue
        where = f"{i['inventory_number']}, {i['position_code']}" if i["position_code"] else i["inventory_number"]
        drafts.append({"key": f"ins-{i['id']}", "at": i["at"],
                       "content": f"{where}: установлен {i['name']} {i['serial_number']}", "note": ""})
    return drafts


def _sim_moves(start, end):
    drafts = []
    for r in fetch_all(
        """
        SELECT msc.id, msc.installed_at, msc.removed_at, msc.removal_reason, msc.removal_comment,
               sc.phone_number, m.inventory_number
        FROM machine_sim_cards msc
        JOIN sim_cards sc ON msc.sim_card_id = sc.id
        JOIN machines m ON msc.machine_id = m.id
        WHERE (msc.installed_at >= ? AND msc.installed_at < ?) OR (msc.removed_at >= ? AND msc.removed_at < ?)
        """,
        (start, end, start, end),
    ):
        if start <= (r["installed_at"] or "") < end:
            drafts.append({"key": f"sin-{r['id']}", "at": r["installed_at"],
                           "content": f"{r['inventory_number']}: установлена SIM {r['phone_number']}", "note": ""})
        if r["removed_at"] and start <= r["removed_at"] < end:
            drafts.append({"key": f"srm-{r['id']}", "at": r["removed_at"],
                           "content": f"{r['inventory_number']}: снята SIM {r['phone_number']}",
                           "note": reason_text("sim", r["removal_reason"], r["removal_comment"])})
    return drafts


def _maintenance(start, end):
    return [
        {"key": f"to-{r['id']}", "at": r["at"],
         "content": f"{r['inventory_number']}: проведено ТО" + (f" ({r['performed_by']})" if r["performed_by"] else ""),
         "note": r["remarks"] or ""}
        for r in fetch_all(
            """
            SELECT mm.id, datetime(mm.created_at, 'localtime') AS at, mm.performed_by, mm.remarks, m.inventory_number
            FROM machine_maintenance mm JOIN machines m ON mm.machine_id = m.id
            WHERE datetime(mm.created_at, 'localtime') >= ? AND datetime(mm.created_at, 'localtime') < ?
            """,
            (start, end),
        )
    ]


def _tasks(start, end):
    """Выполненные задания: одна строка на задание (первая отметка за смену), исполнители — в примечании."""
    by_task = {}
    for r in fetch_all(
        """
        SELECT c.shift_task_id, c.completed_at, c.comment, st.title, m.inventory_number,
               u.surname, u.first_name, u.username
        FROM shift_task_completions c
        JOIN shift_tasks st ON c.shift_task_id = st.id
        LEFT JOIN machines m ON st.machine_id = m.id
        LEFT JOIN users u ON c.user_id = u.id
        WHERE c.completed_at >= ? AND c.completed_at < ?
        ORDER BY c.completed_at
        """,
        (start, end),
    ):
        who = f"{r['surname'] or ''} {r['first_name'] or ''}".strip() or r["username"] or ""
        t = by_task.setdefault(r["shift_task_id"], {
            "key": f"task-{r['shift_task_id']}", "at": r["completed_at"],
            "title": r["title"], "board": r["inventory_number"], "comments": [], "who": [],
        })
        # Комментарий замены уже есть отдельной строкой замены — не повторяем
        if r["comment"] and not r["comment"].startswith("Замена"):
            t["comments"].append(r["comment"])
        if who and who not in t["who"]:
            t["who"].append(who)
    drafts = []
    for t in by_task.values():
        title = t["title"] or ""
        board = f"{t['board']}: " if t["board"] and t["board"] not in title else ""
        content = f"{board}выполнено задание «{title}»"
        if t["comments"]:
            content += ". " + "; ".join(t["comments"])
        drafts.append({"key": t["key"], "at": t["at"], "content": content[:1].upper() + content[1:],
                       "note": ", ".join(t["who"])})
    return drafts


def collect_drafts(shift, shift_date, entries=()):
    """Строки черновика за смену по времени. already=True — такая запись уже есть в журнале."""
    start, end = shift_window(shift, shift_date)
    drafts = _tasks(start, end) + _equipment_moves(start, end) + _sim_moves(start, end) + _maintenance(start, end)
    # Уже в журнале: добавлена из черновика (тот же source_key) или вписана вручную тем же текстом
    keys = {e.get("source_key") for e in entries if e.get("source_key")}
    texts = {(e.get("content") or "").strip() for e in entries}
    for d in drafts:
        d["time"] = _hhmm(d["at"])
        d["already"] = d["key"] in keys or d["content"] in texts
    drafts.sort(key=lambda d: d["at"] or "")
    return drafts
