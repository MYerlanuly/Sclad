#!/usr/bin/env bash
# Обновление склада на Linux-сервере в один шаг: sudo ./update.sh
# Откат: sudo ./update.sh --rollback   (и базы: sudo ./update.sh --rollback --restore-db)
# Подробно: docs/DEPLOY_SERVER.md
set -euo pipefail
cd "$(dirname "$0")"
PY=venv/bin/python
[ -x "$PY" ] || { echo "Нет venv/bin/python. Сначала установите склад по docs/DEPLOY_SERVER.md"; exit 1; }
OWNER=$(stat -c %U .)
if [ "$(id -u)" -eq 0 ] && [ "$OWNER" != "root" ]; then
  # Код, зависимости и базы обновляем от владельца папки (обычно www-data),
  # чтобы не испортить права на файлы; службы перезапускаем от root.
  sudo -u "$OWNER" -H "$PY" scripts/update.py --no-restart "$@"
  exec "$PY" scripts/update.py --restart-only
fi
exec "$PY" scripts/update.py "$@"
