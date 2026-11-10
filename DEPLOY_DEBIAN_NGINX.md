# Развёртывание «Склад АСДУ» на Debian с Nginx

Пошаговая инструкция по установке веб-приложения на сервер Debian с Nginx в качестве обратного прокси и Gunicorn в качестве WSGI-сервера.

---

## 1. Подготовка сервера (Debian)

Подключитесь к серверу по SSH и обновите систему:

```bash
sudo apt update
sudo apt upgrade -y
```

Установите базовые пакеты:

```bash
sudo apt install -y python3 python3-pip python3-venv nginx
```

---

## 2. Создание пользователя для приложения (рекомендуется)

Не запускайте приложение от root — создайте отдельного пользователя:

```bash
sudo adduser --disabled-password --gecos "" sclad
sudo su - sclad
```

Дальнейшие шаги с развёртыванием приложения выполняйте от пользователя `sclad` (или от своего пользователя).

---

## 3. Размещение проекта на сервере

**Вариант А: клонирование из Git**

```bash
cd ~
git clone https://github.com/ВАШ_РЕПОЗИТОРИЙ/sclad.git
cd sclad
```

**Вариант Б: копирование файлов**

С вашего компьютера скопируйте папку проекта на сервер (через SCP, SFTP или архив):

```bash
# С вашего ПК (в PowerShell или другом терминале):
scp -r C:\Users\myerl\OneDrive\Desktop\Sclad user@IP_СЕРВЕРА:/home/sclad/
```

На сервере:

```bash
cd /home/sclad  # или куда вы скопировали проект
```

---

## 4. Виртуальное окружение и зависимости

В каталоге проекта:

```bash
cd /home/sclad/sclad   # путь к проекту (где лежат app.py и requirements.txt)

python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
pip install gunicorn
```

Gunicorn не указан в `requirements.txt` — он нужен только для продакшена, поэтому ставится отдельно.

---

## 5. Настройка окружения и базы данных

Создайте файл с переменными окружения (секретный ключ и путь к БД при необходимости):

```bash
nano .env
```

Содержимое (подставьте свой случайный ключ):

```
FLASK_SECRET_KEY=ваш_длинный_случайный_секретный_ключ_для_сессий
```

Сохраните (Ctrl+O, Enter, Ctrl+X).

Инициализация базы (если файла `inventory.db` ещё нет — он создастся при первом запуске; для пустой БД уже используется обновлённый schema.sql):

```bash
source venv/bin/activate
flask init-db
flask create-user -u admin -p "ваш_пароль" -r admin
```

Проверка запуска приложения:

```bash
gunicorn -w 2 -b 127.0.0.1:8000 app:app
```

В браузере откройте `http://IP_СЕРВЕРА:8000` — не должно открыться (gunicorn слушает только localhost). Остановите gunicorn (Ctrl+C). Дальше запросы пойдут через Nginx.

---

## 6. Systemd-сервис для Gunicorn

Создайте юнит systemd, чтобы приложение запускалось автоматически и перезапускалось при сбоях.

Выйдите из пользователя `sclad` (если заходили под ним):

```bash
exit
```

Создайте файл сервиса (путь к проекту и пользователю подставьте свои):

```bash
sudo nano /etc/systemd/system/sclad.service
```

Содержимое (замените `sclad` и пути на свои):

```ini
[Unit]
Description=Склад АСДУ (Gunicorn)
After=network.target

[Service]
User=sclad
Group=sclad
WorkingDirectory=/home/sclad/sclad
Environment="PATH=/home/sclad/sclad/venv/bin"
EnvironmentFile=/home/sclad/sclad/.env
ExecStart=/home/sclad/sclad/venv/bin/gunicorn -w 2 -b 127.0.0.1:8000 --timeout 120 app:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Сохраните файл. Включите и запустите сервис:

```bash
sudo systemctl daemon-reload
sudo systemctl enable sclad
sudo systemctl start sclad
sudo systemctl status sclad
```

Должен быть статус `active (running)`. Логи: `sudo journalctl -u sclad -f`.

---

## 7. Настройка Nginx

Создайте конфигурацию виртуального хоста:

```bash
sudo nano /etc/nginx/sites-available/sclad
```

Пример для доступа по IP или по имени без SSL:

```nginx
server {
    listen 80;
    server_name _;   # или ваш домен, например sklad.example.com

    client_max_body_size 20M;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /static {
        alias /home/sclad/sclad/static;
    }
}
```

Подключите сайт и проверьте конфигурацию:

```bash
sudo ln -s /etc/nginx/sites-available/sclad /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

Откройте в браузере `http://IP_ВАШЕГО_СЕРВЕРА`. Должна открыться страница входа приложения.

Если по умолчанию в `sites-enabled` остался `default`, можно отключить его:

```bash
sudo rm /etc/nginx/sites-enabled/default
sudo systemctl reload nginx
```

---

## 8. HTTPS (Let's Encrypt) — по желанию

Если у вас есть домен, указывающий на этот сервер:

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d ваш-домен.ru
```

Следуйте подсказкам. Certbot сам изменит конфиг Nginx для HTTPS и настроит перенаправление с HTTP на HTTPS.

Проверка обновления сертификата:

```bash
sudo certbot renew --dry-run
```

---

## 9. Права на файлы и папки

Убедитесь, что пользователь сервиса (`sclad`) имеет доступ к проекту и может писать в каталог с БД и в `backups`:

```bash
sudo chown -R sclad:sclad /home/sclad/sclad
chmod 600 /home/sclad/sclad/.env
```

Если приложение создаёт файлы (например, `inventory.db`, папка `backups`), они должны принадлежать пользователю `sclad`.

---

## 10. Полезные команды

| Действие | Команда |
|----------|--------|
| Статус приложения | `sudo systemctl status sclad` |
| Перезапуск приложения | `sudo systemctl restart sclad` |
| Логи приложения | `sudo journalctl -u sclad -f` |
| Проверка Nginx | `sudo nginx -t` |
| Перезагрузка Nginx | `sudo systemctl reload nginx` |
| Обновление кода и перезапуск | `cd /home/sclad/sclad && git pull && sudo systemctl restart sclad` |

---

## 11. Резервное копирование БД

База данных — файл `inventory.db` в каталоге проекта. Рекомендуется регулярно копировать его на другой сервер или в хранилище (cron + скрипт или встроенные настройки «Массового ввода» / резервной копии в приложении).

Пример копирования по cron (раз в день):

```bash
sudo crontab -e
# добавить строку (подставьте путь и место бэка):
0 3 * * * cp /home/sclad/sclad/inventory.db /home/sclad/backups/inventory_$(date +\%Y-\%m-\%d).db
```

---

## Краткая последовательность (чек-лист)

1. `apt update && apt install python3 python3-pip python3-venv nginx`
2. Создать пользователя, разместить проект.
3. `python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt gunicorn`
4. Создать `.env` с `FLASK_SECRET_KEY`, выполнить `flask init-db` и `flask create-user`.
5. Создать и включить unit `sclad.service` для gunicorn.
6. Создать конфиг Nginx в `sites-available/sclad`, включить сайт, перезагрузить nginx.
7. При наличии домена — настроить HTTPS через certbot.

После этого приложение доступно по HTTP (и по HTTPS, если настроен certbot) на вашем сервере Debian.
