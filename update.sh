#!/usr/bin/env bash
# Обновление склада на Linux-сервере в один шаг (подробно: docs/DEPLOY_SERVER.md).
#   sudo bash update.sh /tmp/sclad-update-....zip   сервер без интернета: из пакета обновления
#   sudo bash update.sh                             сервер с доступом к GitHub: git pull
#   sudo bash update.sh --rollback                  вернуть прошлую версию кода
#   sudo bash update.sh --rollback --restore-db     и базы на момент обновления
#   sudo bash update.sh --version                   какая версия стоит
set -euo pipefail
cd "$(dirname "$0")"
PY=venv/bin/python
[ -x "$PY" ] || { echo "Нет venv/bin/python. Сначала установите склад по docs/DEPLOY_SERVER.md"; exit 1; }

ARGS=()
for a in "$@"; do
  if [[ "$a" == *.zip ]] && [ -f "$a" ]; then
    # Пакет мог лежать в домашней папке, куда у пользователя службы нет доступа
    TMP_ZIP=$(mktemp --suffix=.zip)
    cp "$a" "$TMP_ZIP" && chmod 644 "$TMP_ZIP"
    trap 'rm -f "$TMP_ZIP"' EXIT
    a=$TMP_ZIP
  fi
  ARGS+=("$a")
done

OWNER=$(stat -c %U .)
if [ "$(id -u)" -eq 0 ] && [ "$OWNER" != "root" ]; then
  # Код, зависимости и базы обновляем от владельца папки (обычно www-data),
  # чтобы не испортить права на файлы; службы перезапускаем от root.
  chown -R "$OWNER:$(stat -c %G .)" .
  sudo -u "$OWNER" -H "$PY" scripts/update.py --no-restart "${ARGS[@]}"
  for a in "${ARGS[@]}"; do [ "$a" = "--version" ] && exit 0; done
  "$PY" scripts/update.py --restart-only
  exit $?
fi
"$PY" scripts/update.py "${ARGS[@]}"
