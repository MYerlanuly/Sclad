"""Нагрузочный тест «Склада АСДУ».

Запуск отдельно (не входит в обычный pytest):

    python tests/load_test.py                 # локальный сервер на копии с тестовыми данными
    python tests/load_test.py --users 50 --seconds 60
    python tests/load_test.py --url http://сервер:8000 --login admin --password ...   # чужой сервер (только чтение)

Без --url скрипт создаёт временную базу с реалистичным объёмом данных
(сотни машин, тысячи единиц оборудования, десятки тысяч записей журнала),
поднимает приложение и имитирует одновременную работу пользователей:
просмотр списков, карточек, отчётов, поиск, а также запись (добавление
оборудования, установка/демонтаж, ТО, задания на смену).
"""
import argparse
import http.cookiejar
import itertools
import logging
import os
import random
import re
import sqlite3
import statistics
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PASSWORD = "Load-test-123"


def build_dataset(db_path, machines=400, equipment=6000, sims=450, ops=30000, tasks=3000, maintenance=1500):
    """Заполнить базу объёмом, похожим на реальный склад (с запасом)."""
    conn = sqlite3.connect(db_path)
    rnd = random.Random(42)
    conn.executemany("INSERT INTO machine_types (name) VALUES (?)", [(n,) for n in ("Самосвал", "Экскаватор", "Бульдозер", "Погрузчик")])
    conn.executemany(
        "INSERT INTO machine_models (name, machine_type_id) VALUES (?, ?)",
        [(f"Модель-{i}", i % 4 + 1) for i in range(1, 21)],
    )
    conn.executemany(
        "INSERT INTO machines (inventory_number, machine_model_id) VALUES (?, ?)",
        [(f"INV-{i:05d}", rnd.randint(1, 20)) for i in range(1, machines + 1)],
    )
    conn.executemany(
        "INSERT INTO equipment (name, serial_number, description, status_id, equipment_kind) VALUES (?, ?, ?, 1, ?)",
        [(rnd.choice(["Терминал", "Датчик давления", "Датчик топлива", "Антенна", "Камера"]), f"SN-{i:06d}", "", "sensor" if i % 3 == 0 else "other")
         for i in range(1, equipment + 1)],
    )
    conn.executemany(
        "INSERT INTO sim_cards (phone_number, iccid, status_id) VALUES (?, ?, 1)",
        [(f"+7700{i:07d}", f"8970{i:015d}") for i in range(1, sims + 1)],
    )
    # установки: по 4 единицы на машину, у трети ещё и история демонтажа
    eq_id = 1
    for m in range(1, machines + 1):
        for _ in range(4):
            conn.execute("INSERT INTO machine_equipment (machine_id, equipment_id, installed_at) VALUES (?, ?, datetime('now', '-200 days'))", (m, eq_id))
            conn.execute("UPDATE equipment SET status_id = 2 WHERE id = ?", (eq_id,))
            eq_id += 1
        if m % 3 == 0:
            conn.execute("INSERT INTO machine_equipment (machine_id, equipment_id, installed_at, removed_at) VALUES (?, ?, datetime('now', '-400 days'), datetime('now', '-300 days'))", (m, eq_id))
            conn.execute("UPDATE equipment SET status_id = 3 WHERE id = ?", (eq_id,))
            eq_id += 1
        if m <= sims - 50:
            conn.execute("INSERT INTO machine_sim_cards (machine_id, sim_card_id) VALUES (?, ?)", (m, m))
            conn.execute("UPDATE sim_cards SET status_id = 2 WHERE id = ?", (m,))
    conn.executemany(
        "INSERT INTO operations_log (created_at, operation_type, entity_type, entity_id, description, username) VALUES (datetime('now', ?), ?, 'equipment', ?, ?, 'admin')",
        [(f"-{rnd.randint(0, 700)} days", rnd.choice(["install", "remove"]), rnd.randint(1, equipment), f"Операция {i}") for i in range(ops)],
    )
    conn.executemany(
        "INSERT INTO machine_maintenance (machine_id, performed_at, next_due_at, performed_by) VALUES (?, date('now', ?), date('now', ?), 'Инженер')",
        [(rnd.randint(1, machines), f"-{d} days", f"{90 - d} days") for d in (rnd.randint(0, 400) for _ in range(maintenance))],
    )
    type_id = conn.execute("SELECT id FROM shift_task_types ORDER BY id LIMIT 1").fetchone()[0]
    conn.executemany(
        "INSERT INTO shift_tasks (created_by_user_id, for_shift, effective_date, title, task_type_id, machine_id, status) VALUES (1, ?, date('now', ?), ?, ?, ?, ?)",
        [(rnd.choice(["day", "night"]), f"-{rnd.randint(0, 60)} days", f"Задание {i}", type_id, rnd.randint(1, machines), rnd.choice(["pending", "done", "not_done"]))
         for i in range(tasks)],
    )
    conn.commit()
    conn.close()
    return {"machines": machines, "equipment": equipment, "sims": sims}


class Client:
    def __init__(self, base):
        self.base = base.rstrip("/")
        self.jar = http.cookiejar.CookieJar()

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None

        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar), NoRedirect)
        self.token = None

    def request(self, path, data=None):
        url = self.base + path
        body = urllib.parse.urlencode(data, doseq=True).encode() if data is not None else None
        start = time.perf_counter()
        try:
            resp = self.opener.open(urllib.request.Request(url, data=body), timeout=60)
            status, text = resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            status, text = e.code, e.read().decode("utf-8", "replace") if e.code < 300 or e.code >= 400 else ""
        elapsed = time.perf_counter() - start
        m = re.search(r'name="csrf_token" value="([^"]+)"', text)
        if m:
            self.token = m.group(1)
        return status, elapsed, text

    def login(self, username, password):
        self.request("/login")
        status, _, _ = self.request("/login", {"username": username, "password": password, "csrf_token": self.token})
        if status != 302:
            raise RuntimeError(f"не удалось войти как {username}: {status}")

    def post(self, path, data, form_page):
        self.request(form_page)  # получить свежий csrf-токен
        data = dict(data, csrf_token=self.token)
        return self.request(path, data)


class Stats:
    def __init__(self):
        self.lock = threading.Lock()
        self.times = {}
        self.errors = {}

    def add(self, name, status, elapsed):
        with self.lock:
            self.times.setdefault(name, []).append(elapsed)
            if status >= 500 or status == 0:
                self.errors[name] = self.errors.get(name, 0) + 1

    def report(self, seconds):
        total = sum(len(v) for v in self.times.values())
        errors = sum(self.errors.values())
        lines = [f"{'Запрос':38} {'кол-во':>7} {'медиана':>8} {'95%':>8} {'макс':>8} {'ошибки':>7}"]
        for name in sorted(self.times):
            t = sorted(self.times[name])
            p95 = t[min(len(t) - 1, int(len(t) * 0.95))]
            lines.append(f"{name:38} {len(t):7d} {statistics.median(t) * 1000:7.0f}мс {p95 * 1000:6.0f}мс {t[-1] * 1000:6.0f}мс {self.errors.get(name, 0):7d}")
        lines.append(f"\nВсего запросов: {total}, ошибок 5xx: {errors}, в среднем {total / seconds:.1f} запросов/с")
        return "\n".join(lines), total, errors


def read_scenario(c, stats, rnd, sizes):
    m = rnd.randint(1, sizes["machines"])
    pages = [
        ("главная", "/"),
        ("список оборудования", f"/equipment?page={rnd.randint(1, 50)}"),
        ("поиск оборудования", f"/equipment?q=SN-{rnd.randint(1, sizes['equipment']):06d}"),
        ("список техники", "/machines"),
        ("карточка техники", f"/machines/{m}"),
        ("карточка оборудования", f"/equipment/{rnd.randint(1, sizes['equipment'])}"),
        ("SIM-карты", "/sim_cards"),
        ("ТО", "/maintenance"),
        ("календарь ТО", "/maintenance/calendar"),
        ("задания на смену", "/shift_tasks"),
        ("журнал операций", f"/operations?page={rnd.randint(1, 100)}"),
        ("отчёты", "/reports"),
        ("быстрый поиск", f"/search?q=INV-{m:05d}"),
    ]
    name, path = rnd.choice(pages)
    status, elapsed, _ = c.request(path)
    stats.add(name, status, elapsed)


def write_scenario(c, stats, rnd, sizes, counter):
    action = rnd.choice(["add_equipment", "install_remove", "maintenance", "shift_task"])
    if action == "add_equipment":
        n = next(counter)
        status, elapsed, _ = c.post("/equipment/add", {"name": "Нагрузка", "serial_number": f"LOAD-{n}", "status_id": "1"}, "/equipment/add")
        stats.add("запись: добавить оборудование", status, elapsed)
    elif action == "install_remove":
        m = rnd.randint(1, sizes["machines"])
        n = next(counter)
        c.post("/equipment/add", {"name": "Нагрузка-уст", "serial_number": f"LOADI-{n}", "status_id": "1"}, "/equipment/add")
        eq = load_test_lookup(c, f"LOADI-{n}")
        if eq:
            status, elapsed, _ = c.post(f"/machines/{m}/install_equipment", {"equipment_id": eq}, f"/machines/{m}/install_equipment")
            stats.add("запись: установка оборудования", status, elapsed)
    elif action == "maintenance":
        m = rnd.randint(1, sizes["machines"])
        status, elapsed, _ = c.post("/maintenance/add", {"machine_id": m, "performed_at": time.strftime("%Y-%m-%d")}, "/maintenance/add")
        stats.add("запись: ТО", status, elapsed)
    else:
        status, elapsed, _ = c.post("/shift_tasks", {"action": "add", "for_shift": "day", "effective_date": time.strftime("%Y-%m-%d"),
                                                      "title": "Нагрузочное задание"}, "/shift_tasks")
        stats.add("запись: задание на смену", status, elapsed)


def load_test_lookup(c, serial):
    """id оборудования по серийному номеру: /search перенаправляет на /equipment/<id>."""
    try:
        c.opener.open(urllib.request.Request(c.base + f"/search?q={urllib.parse.quote(serial)}"), timeout=30).read()
        return None
    except urllib.error.HTTPError as e:
        m = re.search(r"/equipment/(\d+)", e.headers.get("Location", ""))
        return m.group(1) if m else None


def run_load(base, users, seconds, username, password, sizes, writes=True):
    stats = Stats()
    stop_at = time.time() + seconds
    counter = itertools.count()  # next() у itertools.count потокобезопасен в CPython
    failures = []

    def user(i):
        rnd = random.Random(i)
        c = Client(base)
        try:
            c.login(username, password)
        except Exception as e:  # noqa: BLE001
            failures.append(repr(e))
            return
        while time.time() < stop_at:
            try:
                if writes and rnd.random() < 0.2:
                    write_scenario(c, stats, rnd, sizes, counter)
                else:
                    read_scenario(c, stats, rnd, sizes)
            except Exception as e:  # noqa: BLE001
                stats.add("сетевые ошибки", 0, 0)
                failures.append(repr(e))

    threads = [threading.Thread(target=user, args=(i,)) for i in range(users)]
    started = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    text, total, errors = stats.report(time.time() - started)
    return text, total, errors, failures


def start_local_server(db_path, port=0):
    os.environ["SCLAD_DATABASE"] = db_path
    os.environ.setdefault("FLASK_SECRET_KEY", "load-test-" + "x" * 40)
    sys.path.insert(0, ROOT)
    import app as app_module
    from werkzeug.security import generate_password_hash
    from werkzeug.serving import make_server

    app_module.init_database()
    app_module.run_migrations()
    conn = sqlite3.connect(db_path)
    for role in ("admin", "editor", "reader"):
        conn.execute("INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)", (role, generate_password_hash(PASSWORD), role))
    conn.commit()
    conn.close()
    flask_app = app_module.create_app()
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    server = make_server("127.0.0.1", port, flask_app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--users", type=int, default=20, help="одновременных пользователей (по умолчанию 20)")
    ap.add_argument("--seconds", type=int, default=30, help="длительность, сек (по умолчанию 30)")
    ap.add_argument("--url", help="адрес уже запущенного сервера; по умолчанию поднимается локальный с тестовыми данными")
    ap.add_argument("--login", default="admin")
    ap.add_argument("--password", default=PASSWORD)
    ap.add_argument("--read-only", action="store_true", help="только чтение")
    ap.add_argument("--allow-writes", action="store_true",
                    help="разрешить запись на сервер из --url (только для тестового сервера, поднятого на базе из --prepare-db!)")
    ap.add_argument("--prepare-db", metavar="PATH", help="только создать базу с тестовыми данными (пользователи admin/editor/reader) и выйти")
    args = ap.parse_args()

    if args.prepare_db:
        server, _ = start_local_server(os.path.abspath(args.prepare_db))
        server.shutdown()
        print(build_dataset(os.path.abspath(args.prepare_db)), "->", args.prepare_db)
        return

    if args.url:
        base = args.url
        writes = args.allow_writes and not args.read_only
        sizes = {"machines": 400, "equipment": 6000, "sims": 450} if writes else {"machines": 50, "equipment": 200, "sims": 50}
        print("Внешний сервер: " + ("чтение и запись." if writes else "только чтение."))
    else:
        tmp = tempfile.mkdtemp(prefix="sclad-load-")
        db_path = os.path.join(tmp, "load.db")
        _, base = start_local_server(db_path)
        print("Готовлю тестовые данные ...")
        sizes = build_dataset(db_path)
        writes = not args.read_only
        print(f"Сервер {base}, данные: {sizes}")
    print(f"Нагрузка: {args.users} пользователей, {args.seconds} с, запись: {'да' if writes else 'нет'}\n")
    text, total, errors, failures = run_load(base, args.users, args.seconds, args.login, args.password, sizes, writes)
    print(text)
    if failures:
        print(f"\nСетевые сбои ({len(failures)}), первые: {failures[:3]}")
    sys.exit(1 if errors or failures else 0)


if __name__ == "__main__":
    main()
