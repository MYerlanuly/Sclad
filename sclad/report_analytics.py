"""Аналитика для раздела «Отчёты».

Все показатели считаются из базы на момент открытия страницы. Выводы и
рекомендации строятся правилами по этим же числам: текст появляется только
тогда, когда для него есть данные, ничего не подставляется «для красоты».
"""
import re
from datetime import date, timedelta

from .db import fetch_all, fetch_one
from .views.maintenance import MAINTENANCE_STATUS_CTE

PERIODS = (7, 30, 90, 365)
DEFAULT_PERIOD = 30
MONTH_NAMES = ("янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек")

# Статусы оборудования из schema.sql
EQ_WAREHOUSE, EQ_INSTALLED, EQ_REMOVED, EQ_FAULTY, EQ_WRITTEN_OFF = 1, 2, 3, 4, 5
EQ_STATUS_TONES = {EQ_INSTALLED: "blue", EQ_WAREHOUSE: "teal", EQ_REMOVED: "slate", EQ_FAULTY: "red", EQ_WRITTEN_OFF: "dark"}

# Группы причин «не выполнено». Причину пишут вручную, поэтому одна и та же
# ситуация встречается в десятке формулировок; группируем по ключевым словам.
# Порядок важен: первая подходящая группа побеждает.
REASON_GROUPS = (
    ("Причина не указана", ()),
    ("Нет транспорта", ("транспорт", "тс.", " тс", "автотранспорт", "машин")),
    ("Техника в работе", ("в работе", "на линии", "техника занята", "а/с ", "а/с находил")),
    ("Занятость на других работах", ("занят", "другие работ", "другой работ", "другим работ", "направлен", "другого задан",
                                      "другое задан", "приоритет", "в разрезе", "работы проводились", "работа по ")),
    ("Погодные условия", ("погод", "дожд", "ветер", "мороз")),
    ("ТО отменено или перенесено", ("отмен", "перенес")),
)
REASON_OTHER = "Другое"


def _plural(n, one, few, many):
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _pct(part, whole):
    return round(100.0 * part / whole) if whole else None


def _scalar(sql, params=()):
    row = fetch_one(sql, params)
    return (row[0] if row and row[0] is not None else 0) if row is not None else 0


def _delta(cur, prev):
    """Изменение к прошлому периоду той же длины: {'diff', 'pct', 'dir'}."""
    diff = cur - prev
    return {
        "diff": diff,
        "pct": _pct(diff, prev) if prev else None,
        "dir": "up" if diff > 0 else ("down" if diff < 0 else "flat"),
        "prev": prev,
    }


def normalize_reason(text):
    t = re.sub(r"\s+", " ", (text or "").strip().lower()).strip(" .,!")
    return t


def reason_group(text):
    t = " " + normalize_reason(text) + " "
    if not t.strip() or t.strip() in ("не выполнено", "нет", "-", "—"):
        return REASON_GROUPS[0][0]
    for name, keys in REASON_GROUPS[1:]:
        if any(k in t for k in keys):
            return name
    return REASON_OTHER


def _last_months(today, count=12):
    """[(ключ 'YYYY-MM', подпись 'янв 2026'), ...] — последние count месяцев, включая текущий."""
    out = []
    y, m = today.year, today.month
    for _ in range(count):
        out.append((f"{y}-{m:02d}", f"{MONTH_NAMES[m - 1]} {str(y)[2:]}"))
        m -= 1
        if m < 1:
            m, y = 12, y - 1
    return list(reversed(out))


def build_report(days=DEFAULT_PERIOD, today=None):
    today = today or date.today()
    if days not in PERIODS:
        days = DEFAULT_PERIOD
    d_from = (today - timedelta(days=days - 1)).isoformat()
    d_to = today.isoformat()
    prev_from = (today - timedelta(days=2 * days - 1)).isoformat()
    prev_to = (today - timedelta(days=days)).isoformat()
    today_iso = today.isoformat()

    # ---------- Оборудование ----------
    eq_rows = [dict(r) for r in fetch_all(
        """
        SELECT es.id, es.name, COUNT(e.id) AS cnt
        FROM equipment_statuses es LEFT JOIN equipment e ON e.status_id = es.id
        GROUP BY es.id ORDER BY es.id
        """
    )]
    eq_by_status = {r["id"]: r["cnt"] for r in eq_rows}
    eq_total = sum(eq_by_status.values())
    eq_installed = eq_by_status.get(EQ_INSTALLED, 0)
    eq_warehouse = eq_by_status.get(EQ_WAREHOUSE, 0)
    eq_faulty = eq_by_status.get(EQ_FAULTY, 0)
    eq_active = eq_total - eq_by_status.get(EQ_WRITTEN_OFF, 0)
    # Порядок сегментов: установлено → склад → демонтировано → неисправно → списано
    order = [EQ_INSTALLED, EQ_WAREHOUSE, EQ_REMOVED, EQ_FAULTY, EQ_WRITTEN_OFF]
    names = {r["id"]: r["name"] for r in eq_rows}
    eq_segments = [
        {"id": sid, "name": names.get(sid, str(sid)), "cnt": eq_by_status.get(sid, 0),
         "pct": _pct(eq_by_status.get(sid, 0), eq_total) or 0, "tone": EQ_STATUS_TONES.get(sid, "slate")}
        for sid in order if sid in names
    ]
    eq_kinds = {r["equipment_kind"]: r["cnt"] for r in fetch_all(
        "SELECT equipment_kind, COUNT(*) AS cnt FROM equipment GROUP BY equipment_kind")}
    eq_by_name = [dict(r) for r in fetch_all(
        f"""
        SELECT name, COUNT(*) AS total,
               SUM(status_id = {EQ_INSTALLED}) AS installed,
               SUM(status_id = {EQ_WAREHOUSE}) AS warehouse,
               SUM(status_id IN ({EQ_REMOVED}, {EQ_FAULTY}, {EQ_WRITTEN_OFF})) AS other
        FROM equipment GROUP BY name ORDER BY total DESC, name LIMIT 10
        """
    )]
    eq_names_total = _scalar("SELECT COUNT(DISTINCT name) FROM equipment")

    # ---------- Техника и ТО ----------
    machines_total = _scalar("SELECT COUNT(*) FROM machines")
    mnt_status_rows = fetch_all(MAINTENANCE_STATUS_CTE + " SELECT to_status, COUNT(*) AS cnt FROM with_status GROUP BY to_status")
    mnt_status = {"overdue": 0, "due_soon": 0, "ok": 0, "no_data": 0}
    for r in mnt_status_rows:
        mnt_status[r["to_status"]] = r["cnt"]
    mnt_covered = machines_total - mnt_status["no_data"]
    mnt_on_schedule = mnt_status["ok"] + mnt_status["due_soon"]
    mnt_by_type = [dict(r) for r in fetch_all(
        MAINTENANCE_STATUS_CTE + """
        SELECT type_id, type_name, COUNT(*) AS total,
               SUM(to_status = 'overdue') AS overdue, SUM(to_status = 'due_soon') AS due_soon,
               SUM(to_status = 'ok') AS ok, SUM(to_status = 'no_data') AS no_data
        FROM with_status GROUP BY type_id, type_name ORDER BY total DESC, type_name
        """
    )]
    for r in mnt_by_type:
        r["coverage"] = _pct(r["ok"] + r["due_soon"], r["total"]) or 0
    mnt_period = _scalar("SELECT COUNT(*) FROM machine_maintenance WHERE date(performed_at) BETWEEN ? AND ?", (d_from, d_to))
    mnt_prev = _scalar("SELECT COUNT(*) FROM machine_maintenance WHERE date(performed_at) BETWEEN ? AND ?", (prev_from, prev_to))
    mnt_future = _scalar("SELECT COUNT(*) FROM machine_maintenance WHERE date(performed_at) > ?", (today_iso,))
    mnt_engineers = _scalar(
        "SELECT COUNT(DISTINCT TRIM(performed_by)) FROM machine_maintenance WHERE date(performed_at) BETWEEN ? AND ? AND TRIM(COALESCE(performed_by,'')) <> ''",
        (d_from, d_to))

    # ---------- SIM-карты и комплектность ----------
    sim_total = _scalar("SELECT COUNT(*) FROM sim_cards")
    sim_installed = _scalar("SELECT COUNT(*) FROM machine_sim_cards WHERE removed_at IS NULL")
    sim_warehouse = _scalar("SELECT COUNT(*) FROM sim_cards WHERE status_id = 1")
    machines_no_sim = _scalar(
        "SELECT COUNT(*) FROM machines m WHERE NOT EXISTS (SELECT 1 FROM machine_sim_cards s WHERE s.machine_id = m.id AND s.removed_at IS NULL)")
    machines_no_eq = _scalar(
        "SELECT COUNT(*) FROM machines m WHERE NOT EXISTS (SELECT 1 FROM machine_equipment s WHERE s.machine_id = m.id AND s.removed_at IS NULL)")
    machines_types = [dict(r) for r in fetch_all(
        """
        SELECT mt.name AS type_name, COUNT(m.id) AS cnt FROM machines m
        JOIN machine_models mm ON m.machine_model_id = mm.id JOIN machine_types mt ON mm.machine_type_id = mt.id
        GROUP BY mt.id ORDER BY cnt DESC
        """
    )]

    # ---------- Задания на смену ----------
    def task_counts(a, b):
        res = {"pending": 0, "done": 0, "not_done": 0}
        for r in fetch_all("SELECT status, COUNT(*) AS cnt FROM shift_tasks WHERE effective_date BETWEEN ? AND ? GROUP BY status", (a, b)):
            res[r["status"]] = r["cnt"]
        res["total"] = res["pending"] + res["done"] + res["not_done"]
        res["closed"] = res["done"] + res["not_done"]
        res["rate"] = _pct(res["done"], res["closed"])
        return res

    tasks = task_counts(d_from, d_to)
    tasks_prev = task_counts(prev_from, prev_to)
    tasks_overdue = _scalar("SELECT COUNT(*) FROM shift_tasks WHERE status = 'pending' AND effective_date < ?", (today_iso,))
    tasks_by_type = [dict(r) for r in fetch_all(
        """
        SELECT COALESCE(tt.name, '—') AS type_name, COUNT(st.id) AS total,
               SUM(st.status = 'done') AS done, SUM(st.status = 'not_done') AS not_done, SUM(st.status = 'pending') AS pending
        FROM shift_tasks st LEFT JOIN shift_task_types tt ON st.task_type_id = tt.id
        WHERE st.effective_date BETWEEN ? AND ?
        GROUP BY st.task_type_id ORDER BY total DESC
        """,
        (d_from, d_to),
    )]
    for r in tasks_by_type:
        r["rate"] = _pct(r["done"], r["done"] + r["not_done"])

    reason_counts, reason_variants = {}, set()
    for r in fetch_all("SELECT not_done_reason FROM shift_tasks WHERE status = 'not_done' AND effective_date BETWEEN ? AND ?", (d_from, d_to)):
        g = reason_group(r["not_done_reason"])
        reason_counts[g] = reason_counts.get(g, 0) + 1
        if normalize_reason(r["not_done_reason"]):
            reason_variants.add(normalize_reason(r["not_done_reason"]))
    reasons = sorted(({"name": k, "cnt": v} for k, v in reason_counts.items()), key=lambda x: (x["name"] in (REASON_OTHER, REASON_GROUPS[0][0]), -x["cnt"]))
    reasons_total = sum(reason_counts.values())
    for r in reasons:
        r["pct"] = _pct(r["cnt"], reasons_total) or 0

    user_name_sql = "COALESCE(NULLIF(TRIM(COALESCE(u.surname,'') || ' ' || COALESCE(u.first_name,'')), ''), u.username)"
    people = {}

    def _acc(sql, key):
        for r in fetch_all(sql, (d_from, d_to)):
            p = people.setdefault(r["user_id"], {"user_name": r["user_name"], "created": 0, "completed": 0, "not_done": 0})
            p[key] = r["cnt"]

    _acc(f"""SELECT u.id AS user_id, {user_name_sql} AS user_name, COUNT(st.id) AS cnt FROM shift_tasks st
             JOIN users u ON st.created_by_user_id = u.id WHERE st.effective_date BETWEEN ? AND ? GROUP BY u.id""", "created")
    _acc(f"""SELECT u.id AS user_id, {user_name_sql} AS user_name, COUNT(c.id) AS cnt FROM shift_task_completions c
             JOIN users u ON c.user_id = u.id JOIN shift_tasks st ON c.shift_task_id = st.id
             WHERE st.effective_date BETWEEN ? AND ? GROUP BY u.id""", "completed")
    _acc(f"""SELECT u.id AS user_id, {user_name_sql} AS user_name, COUNT(st.id) AS cnt FROM shift_tasks st
             JOIN users u ON st.not_done_by_user_id = u.id WHERE st.effective_date BETWEEN ? AND ? AND st.status = 'not_done'
             GROUP BY u.id""", "not_done")
    tasks_by_user = sorted(people.values(), key=lambda x: (x["completed"], x["created"]), reverse=True)
    max_user = max([p["completed"] + p["not_done"] for p in tasks_by_user] or [0])

    # ---------- Операции ----------
    ops_period = _scalar("SELECT COUNT(*) FROM operations_log WHERE date(created_at, 'localtime') BETWEEN ? AND ?", (d_from, d_to))
    ops_prev = _scalar("SELECT COUNT(*) FROM operations_log WHERE date(created_at, 'localtime') BETWEEN ? AND ?", (prev_from, prev_to))
    flow = {r["operation_type"]: r["cnt"] for r in fetch_all(
        """SELECT operation_type, COUNT(*) AS cnt FROM operations_log
           WHERE operation_type IN ('install', 'remove') AND date(created_at, 'localtime') BETWEEN ? AND ? GROUP BY operation_type""",
        (d_from, d_to))}

    # ---------- Динамика за 12 месяцев ----------
    months = _last_months(today)
    m_from = months[0][0] + "-01"

    def month_map(sql):
        out = {}
        for r in fetch_all(sql, (m_from,)):
            out.setdefault(r["k"], {})[r["s"]] = r["cnt"]
        return out

    m_mnt = month_map("SELECT strftime('%Y-%m', performed_at) AS k, 'all' AS s, COUNT(*) AS cnt FROM machine_maintenance "
                      "WHERE date(performed_at) >= ? AND date(performed_at) <= date('now', 'localtime') GROUP BY k")
    m_tasks = month_map("SELECT substr(effective_date, 1, 7) AS k, status AS s, COUNT(*) AS cnt FROM shift_tasks "
                        "WHERE effective_date >= ? GROUP BY k, status")
    m_flow = month_map("SELECT strftime('%Y-%m', created_at, 'localtime') AS k, operation_type AS s, COUNT(*) AS cnt FROM operations_log "
                       "WHERE operation_type IN ('install', 'remove') AND date(created_at, 'localtime') >= ? GROUP BY k, operation_type")
    trend = {
        "labels": [lbl for _, lbl in months],
        "maintenance": [m_mnt.get(k, {}).get("all", 0) for k, _ in months],
        "tasks_done": [m_tasks.get(k, {}).get("done", 0) for k, _ in months],
        "tasks_not_done": [m_tasks.get(k, {}).get("not_done", 0) for k, _ in months],
        "tasks_pending": [m_tasks.get(k, {}).get("pending", 0) for k, _ in months],
        "install": [m_flow.get(k, {}).get("install", 0) for k, _ in months],
        "remove": [m_flow.get(k, {}).get("remove", 0) for k, _ in months],
    }
    mnt_12m = sum(trend["maintenance"])
    active_months = [v for v in trend["maintenance"] if v]
    mnt_month_avg = round(mnt_12m / len(active_months)) if active_months else 0

    # ---------- Выводы, проблемы, рекомендации ----------
    findings, problems, actions = [], [], []

    if tasks["closed"]:
        tone = "good" if tasks["rate"] >= 80 else ("warn" if tasks["rate"] >= 60 else "bad")
        text = (f"Выполнено {tasks['rate']}% закрытых заданий на смену: {tasks['done']} из {tasks['closed']}. "
                f"Не выполнено {tasks['not_done']}.")
        if tasks_prev["rate"] is not None:
            diff = tasks["rate"] - tasks_prev["rate"]
            if diff:
                text += f" К прошлому периоду: {'+' if diff > 0 else '−'}{abs(diff)} п.п. (было {tasks_prev['rate']}%)."
            else:
                text += " Как и в прошлом периоде."
        findings.append({"tone": tone, "title": "Исполнение заданий", "text": text})
    if machines_total:
        cov = _pct(mnt_on_schedule, machines_total)
        tone = "good" if cov >= 90 else ("warn" if cov >= 60 else "bad")
        findings.append({
            "tone": tone, "title": "Техобслуживание",
            "text": (f"В графике ТО {cov}% техники ({mnt_on_schedule} из {machines_total}). "
                     f"Просрочено {mnt_status['overdue']}, нет ни одной записи о ТО у {mnt_status['no_data']}."),
        })
    if eq_total:
        findings.append({
            "tone": "neutral", "title": "Оборудование",
            "text": (f"Установлено {_pct(eq_installed, eq_active) or 0}% действующего оборудования ({eq_installed} из {eq_active}), "
                     f"на складе {eq_warehouse}. Неисправно {eq_faulty}."),
        })
    top_reason = next((r for r in reasons if r["name"] not in (REASON_OTHER, REASON_GROUPS[0][0])), None)
    if top_reason and tasks["not_done"]:
        findings.append({
            "tone": "warn", "title": "Главная причина срывов",
            "text": f"«{top_reason['name']}» — {top_reason['pct']}% невыполненных заданий ({top_reason['cnt']} из {reasons_total}).",
        })

    def problem(level, title, count, unit, text, href=None, action=None, priority=None):
        problems.append({"level": level, "title": title, "count": count, "unit": unit, "text": text, "href": href})
        if action:
            actions.append({"priority": priority or ("high" if level == "critical" else "medium"), "title": action[0],
                            "text": action[1], "metric": action[2], "href": href})

    if mnt_status["overdue"]:
        problem("critical", "Просроченное ТО", mnt_status["overdue"], _plural(mnt_status["overdue"], "единица", "единицы", "единиц") + " техники",
                "Срок следующего ТО уже прошёл.", "maintenance_overdue",
                ("Закрыть просроченное ТО", "Поставить просроченную технику в задания ближайших смен, начиная с самых давних сроков.",
                 f"Цель: 0 просрочек (сейчас {mnt_status['overdue']})"))
    if tasks["closed"] and tasks["rate"] is not None and tasks["rate"] < 80:
        main = f" Чаще всего: «{top_reason['name']}»." if top_reason else ""
        problem("critical" if tasks["rate"] < 60 else "warning", "Низкая доля выполненных заданий", tasks["not_done"],
                _plural(tasks["not_done"], "задание", "задания", "заданий") + " не выполнено",
                f"Выполнено только {tasks['rate']}% закрытых заданий за период.{main}", "tasks_not_done",
                ("Снять главную причину срывов",
                 (f"Причина «{top_reason['name']}» встречается в {top_reason['pct']}% случаев: её нужно решать организационно "
                  "(планировать задания с учётом загрузки людей и транспорта), а не переносом заданий.") if top_reason else
                 "Разобрать невыполненные задания с ответственными и устранить повторяющиеся причины.",
                 f"Цель: не ниже 80% (сейчас {tasks['rate']}%)"))
    if tasks_overdue:
        problem("warning", "Задания без отметки", tasks_overdue, _plural(tasks_overdue, "задание", "задания", "заданий"),
                "Дата задания прошла, а оно всё ещё «Ожидает»: не отмечено ни выполненным, ни невыполненным.", "tasks_pending",
                ("Закрывать задания в конце смены", "Отмечать каждое задание выполненным или невыполненным до сдачи смены, иначе статистика искажается.",
                 f"Цель: 0 (сейчас {tasks_overdue})"))
    if mnt_status["no_data"]:
        problem("warning", "Нет записей о ТО", mnt_status["no_data"], _plural(mnt_status["no_data"], "единица", "единицы", "единиц") + " техники",
                f"Это {_pct(mnt_status['no_data'], machines_total)}% парка: по ним нельзя посчитать срок следующего ТО.", "maintenance_no_data",
                ("Внести последнее ТО по всему парку", "Внести дату последнего ТО для техники без записей: так она попадёт в график и плитки просрочек.",
                 f"Цель: 0 (сейчас {mnt_status['no_data']})"))
    if eq_faulty:
        problem("warning", "Неисправное оборудование", eq_faulty, "ед.", "Числится со статусом «Неисправно».", "equipment_faulty",
                ("Разобрать неисправное оборудование", "Решить по каждой единице: ремонт, возврат поставщику или списание.",
                 f"Сейчас {eq_faulty} ед."), priority="low")
    if machines_no_sim and machines_total:
        problem("info", "Техника без SIM-карты", machines_no_sim, _plural(machines_no_sim, "единица", "единицы", "единиц") + " техники",
                f"{_pct(machines_no_sim, machines_total)}% парка без установленной SIM-карты. "
                f"На складе {sim_warehouse} {_plural(sim_warehouse, 'свободная SIM-карта', 'свободные SIM-карты', 'свободных SIM-карт')}. "
                "Если технике SIM не нужна, это не проблема.", "machines")
    if mnt_future:
        problem("info", "ТО с датой в будущем", mnt_future, _plural(mnt_future, "запись", "записи", "записей"),
                "Дата проведения ТО позже сегодняшней: вероятно, опечатка при вводе.", "maintenance_history",
                ("Проверить даты ТО", "Исправить записи ТО с датой проведения в будущем и запретить такие даты в форме.",
                 f"Сейчас {mnt_future}"), priority="low")
    if reasons_total and len(reason_variants) >= 10:
        problem("info", "Причины срывов в свободной форме", len(reason_variants), "формулировок",
                f"{reasons_total} причин записаны {len(reason_variants)} разными формулировками; в отчёте они сведены в {len(reasons)} групп "
                "по ключевым словам. Точнее было бы выбирать причину из списка.", None,
                ("Причина невыполнения из списка", "Добавить в форму «Не выполнено» выбор причины из справочника и поле для комментария.",
                 f"Сейчас {len(reason_variants)} вариантов текста"), priority="low")

    prio_order = {"high": 0, "medium": 1, "low": 2}
    actions.sort(key=lambda a: prio_order[a["priority"]])

    return {
        "days": days, "periods": PERIODS, "date_from": d_from, "date_to": d_to,
        "prev_from": prev_from, "prev_to": prev_to,
        "yesterday": (today - timedelta(days=1)).isoformat(), "tomorrow": (today + timedelta(days=1)).isoformat(),
        "equipment": {"total": eq_total, "installed": eq_installed, "warehouse": eq_warehouse, "faulty": eq_faulty,
                      "active": eq_active, "installed_pct": _pct(eq_installed, eq_active) or 0, "segments": eq_segments,
                      "sensors": eq_kinds.get("sensor", 0), "other": eq_kinds.get("other", 0),
                      "by_name": eq_by_name, "names_total": eq_names_total},
        "machines": {"total": machines_total, "types": machines_types, "no_sim": machines_no_sim, "no_equipment": machines_no_eq},
        "maintenance": {"status": mnt_status, "covered": mnt_covered, "on_schedule": mnt_on_schedule,
                        "on_schedule_pct": _pct(mnt_on_schedule, machines_total) or 0, "by_type": mnt_by_type,
                        "period": mnt_period, "delta": _delta(mnt_period, mnt_prev), "engineers": mnt_engineers,
                        "future": mnt_future, "last12": mnt_12m, "month_avg": mnt_month_avg},
        "sim": {"total": sim_total, "installed": sim_installed, "warehouse": sim_warehouse, "installed_pct": _pct(sim_installed, sim_total) or 0},
        "tasks": tasks, "tasks_prev": tasks_prev, "tasks_overdue": tasks_overdue, "tasks_by_type": tasks_by_type,
        "tasks_by_user": tasks_by_user, "max_user": max_user,
        "reasons": reasons, "reasons_total": reasons_total, "reason_variants": len(reason_variants),
        "ops": {"period": ops_period, "delta": _delta(ops_period, ops_prev), "install": flow.get("install", 0), "remove": flow.get("remove", 0)},
        "trend": trend,
        "findings": findings, "problems": problems, "actions": actions,
    }
