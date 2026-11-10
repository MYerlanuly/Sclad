#!/usr/bin/env python3
"""
Скачивает JS-библиотеки для работы приложения без интернета.
Запустите этот скрипт ОДИН РАЗ на компьютере с доступом в интернет,
затем скопируйте всю папку проекта на сервер в корпоративной сети.

Использование: из корня проекта выполните
  python scripts/download_offline_assets.py
"""
import os
import sys
import urllib.request

# Корень проекта (родитель папки scripts)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
STATIC_JS = os.path.join(PROJECT_ROOT, "static", "js")

ASSETS = [
    (
        "https://cdn.tailwindcss.com",
        "tailwind.min.js",
    ),
    (
        "https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js",
        "chart.umd.min.js",
    ),
]


def main():
    os.makedirs(STATIC_JS, exist_ok=True)
    for url, filename in ASSETS:
        path = os.path.join(STATIC_JS, filename)
        print(f"Скачиваю {filename} ...", end=" ", flush=True)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
            with open(path, "wb") as f:
                f.write(data)
            print(f"OK ({len(data)} байт)")
        except Exception as e:
            print(f"Ошибка: {e}")
            sys.exit(1)
    print("Готово. Папку static/js можно копировать на сервер без интернета.")


if __name__ == "__main__":
    main()
