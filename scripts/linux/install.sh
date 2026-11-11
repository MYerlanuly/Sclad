#!/usr/bin/env bash
# Установка склада на Linux-сервер (Debian/Ubuntu) как службы systemd.
# Запуск из папки с проектом (например, /opt/sclad):
#   sudo scripts/linux/install.sh                 основной склад
#   sudo scripts/linux/install.sh .env.otdel2     склад другого отдела
# Можно запускать повторно: что уже сделано, пропускается.
# Подробно: docs/DEPLOY_SERVER.md
set -euo pipefail
cd "$(dirname "$0")/../.."
ROOT=$(pwd)
ENV_FILE=${1:-}
APP_USER=${SCLAD_USER:-www-data}

step() { echo; echo "== $1"; }
fail() { echo; echo "ОШИБКА: $1"; exit 1; }
as_app() { sudo -u "$APP_USER" -H env SCLAD_ENV_FILE="$ENV_FILE" SCLAD_BACKUP_SCHEDULER=0 "$@"; }

[ "$(id -u)" -eq 0 ] || fail "Запустите через sudo: sudo scripts/linux/install.sh"

step "1. Системные пакеты"
if ! command -v python3 >/dev/null || ! python3 -c "import venv, ensurepip" 2>/dev/null || ! command -v git >/dev/null; then
  apt-get update -q && apt-get install -y -q python3 python3-venv python3-pip git
fi
echo "  $(python3 --version)"

step "2. Права на папку"
id "$APP_USER" >/dev/null 2>&1 || fail "Нет пользователя $APP_USER (задайте другого: SCLAD_USER=имя)"
chown -R "$APP_USER:$APP_USER" "$ROOT"
echo "  владелец $ROOT: $APP_USER"

step "3. Зависимости"
[ -x venv/bin/python ] || as_app python3 -m venv venv
PIP_ARGS=(-m pip install --disable-pip-version-check -q -r requirements.txt)
[ -d wheels ] && PIP_ARGS+=(--no-index --find-links wheels)
as_app venv/bin/python "${PIP_ARGS[@]}"
echo "  готово"

step "4. Настройки"
if [ ! -f .env ]; then
  SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
  sed -e "s/^FLASK_SECRET_KEY=.*/FLASK_SECRET_KEY=$SECRET/" -e "s/^FLASK_DEBUG=.*/FLASK_DEBUG=0/" .env.example > .env
  chown "$APP_USER:$APP_USER" .env && chmod 600 .env
  echo "  создан .env с новым секретным ключом"
else
  echo "  .env уже есть"
fi
if [ -n "$ENV_FILE" ] && [ ! -f "$ENV_FILE" ]; then
  fail "Файл настроек склада $ENV_FILE не найден (создайте из .env.otdel.example)"
fi
read -r INSTANCE NAME_DB PORT < <(as_app venv/bin/python -c \
  "from sclad import config as c; print(c.INSTANCE or '-', c.DATABASE, c.PORT)" | tail -n 1)
[ "$INSTANCE" = "-" ] && INSTANCE=""
echo "  база: $NAME_DB, порт: $PORT"

step "5. База данных"
if [ -f "$NAME_DB" ]; then
  echo "  база уже есть, оставляю как есть"
else
  as_app venv/bin/python -m flask --app app init-db
  echo "  Создадим администратора склада."
  read -r -p "  Логин: " LOGIN
  read -r -s -p "  Пароль: " PASS; echo
  as_app venv/bin/python -m flask --app app create-user -u "$LOGIN" -p "$PASS" -r admin
fi

step "6. Служба"
SERVICE=sclad${INSTANCE:+-$INSTANCE}
ENV_LINE=""
[ -n "$ENV_FILE" ] && ENV_LINE="Environment=\"SCLAD_ENV_FILE=$ROOT/$ENV_FILE\""
cat > "/etc/systemd/system/$SERVICE.service" <<EOF
[Unit]
Description=Склад АСДУ ($SERVICE)
After=network.target

[Service]
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$ROOT
Environment="PATH=$ROOT/venv/bin"
$ENV_LINE
ExecStart=$ROOT/venv/bin/gunicorn -w 2 -b 0.0.0.0:$PORT --access-logfile - "app:create_app()"
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null 2>&1
systemctl restart "$SERVICE"
echo "  служба $SERVICE включена и запущена"
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow "$PORT/tcp" >/dev/null && echo "  открыт порт $PORT в ufw"
fi

step "7. Проверка"
for _ in $(seq 1 20); do
  if python3 -c "import sys, urllib.request; urllib.request.urlopen(sys.argv[1], timeout=5)" "http://127.0.0.1:$PORT/login" 2>/dev/null; then
    echo
    echo "Готово. Склад открывается с других компьютеров: http://$(hostname -I | awk '{print $1}'):$PORT"
    echo "Обновление в будущем: cd $ROOT && sudo ./update.sh"
    exit 0
  fi
  sleep 2
done
fail "Сайт не ответил. Журнал службы: journalctl -u $SERVICE -n 50"
