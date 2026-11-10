"""Обновление склада на сервере в один шаг.

Запускается обёрткой из корня проекта: update.bat (Windows) или ./update.sh (Linux).
Напрямую: venv/bin/python scripts/update.py (Linux), venv\\Scripts\\python scripts\\update.py (Windows).

Что делает, по порядку:
  1. Делает копию базы каждого склада (основного и всех из файлов .env.<код>)
     в backups/.../before-update/. Хранятся последние KEEP_BACKUPS копий.
  2. Скачивает новую версию кода: git pull --ff-only.
  3. Останавливает службы складов (Windows) и ставит зависимости из requirements.txt
     (из папки wheels/, если она есть, то есть без интернета).
  4. Применяет миграции базы каждого склада.
  5. Запускает (перезапускает) службы складов и проверяет, что сайт отвечает.

Если после обновления что-то не так: update.bat --rollback / ./update.sh --rollback
вернёт код к версии до обновления, а с --restore-db ещё и базы из копий.

Службы ищутся сами: на Linux это systemd-службы sclad*.service, на Windows
задачи планировщика Sclad* (их создаёт scripts/windows/install-service.ps1).
Другие имена можно указать через --services.
"""
import argparse
import glob
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_FILE = os.path.join(ROOT, ".update-state.json")
KEEP_BACKUPS = 15
IS_WINDOWS = os.name == "nt"
ENV_EXAMPLES = {".env.example", ".env.otdel.example"}
# Переменные, которые не должны протекать из окружения в настройки отдельного склада
SCLAD_VARS = ("SCLAD_ENV_FILE", "SCLAD_INSTANCE", "SCLAD_NAME", "SCLAD_DATABASE",
              "SCLAD_BACKUP_DIR", "SCLAD_PORT")


class UpdateError(Exception):
    pass


def say(text=""):
    print(text, flush=True)


def step(text):
    say()
    say(f"== {text}")


def run(cmd, check=True, capture=False, env=None):
    proc = subprocess.run(
        cmd, cwd=ROOT, env=env, text=True, encoding="utf-8", errors="replace",
        capture_output=capture,
    )
    if check and proc.returncode != 0:
        details = (proc.stderr or proc.stdout or "").strip() if capture else ""
        raise UpdateError(f"Команда не выполнена: {' '.join(cmd)}" + (f"\n{details}" if details else ""))
    return proc


def git(*args, check=True):
    return run(["git", *args], check=check, capture=True)


def warehouse_env(env_file):
    env = dict(os.environ)
    for key in SCLAD_VARS:
        env.pop(key, None)  # пустое значение не дало бы файлу склада задать своё
    env["SCLAD_BACKUP_SCHEDULER"] = "0"
    env["PYTHONIOENCODING"] = "utf-8"
    if env_file:
        env["SCLAD_ENV_FILE"] = env_file
    return env


def find_warehouses():
    """Основной склад и склады из файлов .env.<код>. Настройки читает сам код склада."""
    env_files = [None]
    for path in sorted(glob.glob(os.path.join(ROOT, ".env.*"))):
        name = os.path.basename(path)
        if name not in ENV_EXAMPLES and os.path.isfile(path):
            env_files.append(name)
    code = (
        "import json\n"
        "from sclad import config as c\n"
        "print(json.dumps({k: getattr(c, k) for k in "
        "('INSTANCE', 'NAME', 'DATABASE', 'BACKUP_ROOT', 'PORT', 'BACKUP_FILE_PREFIX')}))"
    )
    result = []
    for env_file in env_files:
        proc = run([sys.executable, "-c", code], check=False, capture=True, env=warehouse_env(env_file))
        if proc.returncode != 0:
            say(f"  пропущен {env_file}: не удалось прочитать настройки\n  {proc.stderr.strip()}")
            continue
        info = json.loads(proc.stdout.strip().splitlines()[-1])
        info["ENV_FILE"] = env_file
        if not os.path.exists(info["DATABASE"]):
            if env_file:
                say(f"  {env_file}: базы {info['DATABASE']} нет, склад пропущен")
            continue
        result.append(info)
    return result


def backup_databases(warehouses, label):
    """Согласованная копия каждой базы (SQLite online backup, можно при работающем сайте)."""
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    made = {}
    for wh in warehouses:
        folder = os.path.join(wh["BACKUP_ROOT"], "before-update")
        os.makedirs(folder, exist_ok=True)
        dest = os.path.join(folder, f"{wh['BACKUP_FILE_PREFIX']}_{stamp}_{label}.db")
        src = sqlite3.connect(wh["DATABASE"])
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            src.close()
            dst.close()
        made[wh["DATABASE"]] = dest
        say(f"  {wh['NAME']}: {dest}")
        # Старые копии перед обновлением удаляем, оставляем последние KEEP_BACKUPS
        old = sorted(glob.glob(os.path.join(folder, f"{wh['BACKUP_FILE_PREFIX']}_*.db")))
        for path in old[:-KEEP_BACKUPS]:
            try:
                os.remove(path)
            except OSError:
                pass
    return made


def current_commit():
    return git("rev-parse", "HEAD").stdout.strip()


def pull_code():
    if not shutil.which("git"):
        raise UpdateError("Не найден git. Установите его или обновите файлы вручную и запустите с --no-pull.")
    if not os.path.isdir(os.path.join(ROOT, ".git")):
        raise UpdateError("Папка проекта не является git-клоном. Обновите файлы вручную и запустите с --no-pull.")
    changed = git("status", "--porcelain", "--untracked-files=no").stdout.strip()
    if changed:
        raise UpdateError(
            "В папке проекта есть изменённые вручную файлы, git не будет их перезаписывать:\n"
            f"{changed}\n"
            "Посмотрите, что изменено (git diff). Если правки не нужны: git checkout -- .  и запустите снова."
        )
    before = current_commit()
    proc = git("pull", "--ff-only", check=False)
    if proc.returncode != 0:
        raise UpdateError(
            "git pull не удался (нет связи с GitHub или ветка разошлась):\n"
            + (proc.stderr or proc.stdout).strip()
        )
    after = current_commit()
    if before == after:
        say("  новых изменений нет, код уже последней версии")
    else:
        log = git("log", "--oneline", "--no-merges", f"{before}..{after}").stdout.strip()
        say("  получены изменения:")
        for line in log.splitlines():
            say(f"    {line}")
    return before, after


def install_requirements():
    cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-q",
           "-r", "requirements.txt"]
    wheels = os.path.join(ROOT, "wheels")
    if os.path.isdir(wheels):
        cmd += ["--no-index", "--find-links", wheels]
        say("  из папки wheels/ (без интернета)")
    run(cmd)
    say("  зависимости в порядке")


def migrate(warehouses):
    code = "from sclad.migrations import run_migrations\nrun_migrations()\nprint('ok')"
    for wh in warehouses:
        proc = run([sys.executable, "-c", code], check=False, capture=True,
                   env=warehouse_env(wh["ENV_FILE"]))
        output = (proc.stdout + proc.stderr).strip()
        problems = [line for line in output.splitlines() if line.strip() and line.strip() != "ok"]
        if proc.returncode != 0:
            raise UpdateError(f"Миграции склада «{wh['NAME']}» не выполнены:\n{output}")
        say(f"  {wh['NAME']}: схема базы обновлена")
        for line in problems:
            say(f"    {line}")


# ---------- службы ----------

def find_services():
    if IS_WINDOWS:
        proc = run(["schtasks", "/Query", "/FO", "CSV", "/NH"], check=False, capture=True)
        names = set()
        for line in proc.stdout.splitlines():
            name = line.split('","')[0].strip('"')
            if name.startswith("\\") and name.count("\\") == 1 and name[1:].lower().startswith("sclad"):
                names.add(name[1:])
        return sorted(names)
    if not shutil.which("systemctl"):
        return []
    proc = run(["systemctl", "list-unit-files", "--type=service", "--no-legend", "--plain", "sclad*"],
               check=False, capture=True)
    return sorted(line.split()[0] for line in proc.stdout.splitlines() if line.strip())


def _sudo():
    return [] if IS_WINDOWS or os.geteuid() == 0 else ["sudo"]


def stop_services(services):
    for name in services:
        if IS_WINDOWS:
            run(["schtasks", "/End", "/TN", name], check=False, capture=True)
            say(f"  остановлена {name}")
    if IS_WINDOWS and services:
        time.sleep(2)  # даём процессу освободить файлы и порт


def start_services(services):
    for name in services:
        if IS_WINDOWS:
            proc = run(["schtasks", "/Run", "/TN", name], check=False, capture=True)
        else:
            proc = run(_sudo() + ["systemctl", "restart", name], check=False, capture=True)
        if proc.returncode == 0:
            say(f"  запущена {name}")
        else:
            say(f"  НЕ удалось запустить {name}: {(proc.stderr or proc.stdout).strip()}")
            if IS_WINDOWS:
                say("  Запустите update.bat от имени администратора.")


def check_sites(warehouses, timeout=40):
    ok = True
    for wh in warehouses:
        url = f"http://127.0.0.1:{wh['PORT']}/login"
        deadline = time.time() + timeout
        status = None
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=5) as resp:
                    status = resp.status
            except urllib.error.HTTPError as e:
                status = e.code
            except OSError:
                status = None
            if status == 200:
                break
            time.sleep(2)
        if status == 200:
            say(f"  {wh['NAME']}: сайт отвечает ({url})")
        else:
            ok = False
            say(f"  {wh['NAME']}: сайт на {url} не ответил. Проверьте службу и журнал (logs/).")
    return ok


# ---------- сценарии ----------

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def do_update(args, services):
    step("Склады на этом сервере")
    warehouses = find_warehouses()
    if not warehouses:
        say("  ни одной базы не найдено (новая установка?) — копии и миграции пропускаются")
    for wh in warehouses:
        say(f"  {wh['NAME']}: {wh['DATABASE']}, порт {wh['PORT']}")

    label = current_commit()[:7] if os.path.isdir(os.path.join(ROOT, ".git")) and shutil.which("git") else "manual"
    step("1. Копии баз перед обновлением")
    backups = backup_databases(warehouses, label)

    before = after = label
    if args.no_pull:
        step("2. Код: пропущено (--no-pull)")
    else:
        step("2. Скачивание новой версии")
        before, after = pull_code()
    # Повторный запуск без новых изменений не затирает сведения для отката прошлого обновления
    if before != after or args.no_pull or not load_state():
        save_state({"date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "commit_before": before,
                    "commit_after": after, "backups": backups})

    if IS_WINDOWS and services and not args.no_restart:
        step("Остановка служб")
        stop_services(services)

    step("3. Зависимости")
    install_requirements()
    step("4. Миграции базы")
    migrate(warehouses)
    return warehouses


def do_rollback(args, services):
    state = load_state()
    if not state:
        raise UpdateError("Нет сведений о прошлом обновлении (.update-state.json), откатывать нечего.")
    step(f"Откат обновления от {state['date']}")
    warehouses = find_warehouses()
    if args.restore_db:
        say("  ВНИМАНИЕ: базы вернутся к состоянию на момент обновления,")
        say("  всё, что внесли в склад после него, пропадёт (текущие базы сохраню в копию).")
        if not args.yes and input("  Продолжить? Введите да: ").strip().lower() not in ("да", "y", "yes"):
            raise UpdateError("Откат отменён.")
    if services and not args.no_restart:
        stop_services(services)
    if state["commit_before"] != state["commit_after"] and state["commit_before"] != "manual":
        step("Возврат кода")
        git("reset", "--hard", state["commit_before"])
        say(f"  код возвращён к {state['commit_before'][:7]}")
        install_requirements()
    if args.restore_db:
        step("Возврат баз")
        backup_databases(warehouses, "before-rollback")
        for db_path, backup in state["backups"].items():
            src = sqlite3.connect(backup)
            dst = sqlite3.connect(db_path)
            try:
                src.backup(dst)
            finally:
                src.close()
                dst.close()
            say(f"  {db_path} <- {backup}")
    return warehouses


def main():
    parser = argparse.ArgumentParser(description="Обновление склада на сервере")
    parser.add_argument("--no-pull", action="store_true", help="не скачивать код (файлы обновлены вручную)")
    parser.add_argument("--no-restart", action="store_true", help="не трогать службы")
    parser.add_argument("--restart-only", action="store_true", help="только перезапустить службы и проверить сайт")
    parser.add_argument("--rollback", action="store_true", help="вернуть код к версии до последнего обновления")
    parser.add_argument("--restore-db", action="store_true", help="при откате вернуть и базы из копий")
    parser.add_argument("--yes", action="store_true", help="не спрашивать подтверждение")
    parser.add_argument("--services", help="имена служб через запятую (по умолчанию ищутся sclad*)")
    args = parser.parse_args()

    services = [s.strip() for s in args.services.split(",") if s.strip()] if args.services else find_services()
    try:
        if args.restart_only:
            warehouses = find_warehouses()
        elif args.rollback:
            warehouses = do_rollback(args, services)
        else:
            warehouses = do_update(args, services)

        if args.no_restart:
            say("\nСлужбы не перезапускались (--no-restart).")
            return 0
        step("5. Запуск и проверка")
        if not services:
            say("  службы склада не найдены: перезапустите сайт вручную")
            return 0
        start_services(services)
        ok = check_sites(warehouses)
        say()
        say("Готово." if ok else "Готово, но не все сайты ответили — см. выше.")
        if not args.rollback and not args.restart_only:
            say("Если что-то не так: update --rollback (вернуть код), update --rollback --restore-db (и базы).")
        return 0 if ok else 1
    except UpdateError as e:
        say()
        say(f"ОШИБКА: {e}")
        if not args.rollback and not args.restart_only and services and IS_WINDOWS and not args.no_restart:
            # На Windows службы могли быть остановлены перед установкой зависимостей
            start_services(services)
        return 2


if __name__ == "__main__":
    sys.exit(main())
