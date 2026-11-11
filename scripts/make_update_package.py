"""Сборка пакета обновления для сервера без интернета.

Запускается на рабочем компьютере с интернетом, в git-клоне проекта:

    python scripts/make_update_package.py                  для Debian 12 (Python 3.11)
    python scripts/make_update_package.py --python 3.13    если на сервере другой Python
    python scripts/make_update_package.py --windows        для Windows-сервера

Получается один файл dist/sclad-update-<дата>-<коммит>.zip: код текущей версии (HEAD)
и все пакеты Python для сервера (папка wheels/). Его переносят на сервер и ставят
командой  sudo bash update.sh /путь/к/sclad-update-....zip  (см. docs/DEPLOY_SERVER.md).
"""
import argparse
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = ".sclad-package.json"
LINUX_PLATFORMS = ["manylinux_2_17_x86_64", "manylinux2014_x86_64", "manylinux_2_28_x86_64"]


def git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True).stdout


def server_requirements(windows):
    """requirements.txt для сервера: маркеры платформы pip download считает по этому
    компьютеру, поэтому убираем их и добавляем нужный веб-сервер явно."""
    lines = []
    with open(os.path.join(ROOT, "requirements.txt"), encoding="utf-8") as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            name = re.split(r"[<>=!~;\s\[]", line, maxsplit=1)[0].lower()
            spec = line.split(";", 1)[0].strip()
            if name == "gunicorn" and windows or name == "waitress" and not windows:
                continue
            lines.append(spec)
    return lines


def main():
    parser = argparse.ArgumentParser(description="Пакет обновления склада для сервера без интернета")
    parser.add_argument("--python", default="3.11", help="версия Python на сервере (по умолчанию 3.11, как в Debian 12)")
    parser.add_argument("--windows", action="store_true", help="пакет для Windows-сервера")
    parser.add_argument("--out", default=os.path.join(ROOT, "dist"), help="куда положить пакет")
    args = parser.parse_args()

    if git("status", "--porcelain", "--untracked-files=no").strip():
        print("Внимание: есть незакоммиченные изменения, в пакет попадёт только последний коммит.")
    commit = git("rev-parse", "HEAD").decode().strip()
    subject = git("log", "-1", "--format=%cd %s", "--date=format:%d.%m.%Y").decode("utf-8").strip()
    print(f"Версия: {commit[:7]} ({subject})")

    with tempfile.TemporaryDirectory() as tmp:
        req_path = os.path.join(tmp, "requirements-server.txt")
        with open(req_path, "w", encoding="utf-8") as f:
            f.write("\n".join(server_requirements(args.windows)) + "\n")
        wheels_dir = os.path.join(tmp, "wheels")
        platforms = ["win_amd64"] if args.windows else LINUX_PLATFORMS
        cmd = [sys.executable, "-m", "pip", "download", "-q", "--disable-pip-version-check",
               "-r", req_path, "-d", wheels_dir, "--only-binary=:all:",
               "--python-version", args.python, "--implementation", "cp"]
        for platform in platforms:
            cmd += ["--platform", platform]
        print(f"Скачиваю пакеты Python для {'Windows' if args.windows else 'Linux'}, Python {args.python} ...")
        if subprocess.run(cmd, cwd=ROOT).returncode != 0:
            raise SystemExit("pip download не удался: проверьте интернет и версию Python (--python).")

        code = tarfile.open(fileobj=io.BytesIO(git("archive", "--format=tar", "HEAD")))
        files = []
        os.makedirs(args.out, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d")
        out_path = os.path.join(args.out, f"sclad-update-{stamp}-{commit[:7]}{'-windows' if args.windows else ''}.zip")
        with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for member in code.getmembers():
                if member.isfile():
                    zf.writestr(member.name, code.extractfile(member).read())
                    files.append(member.name)
            wheels = sorted(os.listdir(wheels_dir))
            for name in wheels:
                zf.write(os.path.join(wheels_dir, name), f"wheels/{name}")
            zf.writestr(MANIFEST, json.dumps({
                "commit": commit, "title": subject, "built": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "python": args.python, "platform": "windows" if args.windows else "linux",
                "files": files, "wheels": wheels,
            }, ensure_ascii=False, indent=1))

    size = os.path.getsize(out_path) / 1024 / 1024
    print(f"Готово: {out_path} ({size:.1f} МБ, файлов кода: {len(files)}, пакетов: {len(wheels)})")
    print("Перенесите файл на сервер (например, WinSCP или scp в /tmp) и выполните там:")
    print(f"  cd /opt/sclad && sudo bash update.sh /tmp/{os.path.basename(out_path)}")


if __name__ == "__main__":
    main()
