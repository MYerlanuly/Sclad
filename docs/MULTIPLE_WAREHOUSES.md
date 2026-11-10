# Несколько складов (отделов) на одном коде

Каждый отдел получает свой склад: своя база, свои пользователи, свои резервные копии
и свой адрес. Код общий: после `git pull` обновляются все склады сразу.

Основной склад продолжает работать как раньше (`inventory.db`, папка `backups/`),
для него ничего менять не нужно.

## Как это устроено

Склад определяется файлом настроек, например `.env.otdel2`:

```ini
SCLAD_INSTANCE=otdel2          # код склада, латиницей
SCLAD_NAME=Склад отдела 2      # название в шапке, на входе и на актах ТО
SCLAD_PORT=5001                # порт для python app.py
```

Из кода склада выводится остальное:

| Что | Основной склад | Склад `otdel2` |
|---|---|---|
| База | `inventory.db` | `otdel2.db` |
| Папка резервных копий | `backups/` | `backups/otdel2/` |
| Имя скачиваемой копии | `inventory_backup_...db` | `otdel2_backup_...db` |
| Cookie входа | `session` | `sclad_otdel2_session` |

Поэтому вход в один склад не выкидывает из другого, а пользователь одного склада
не может попасть в другой, даже если `FLASK_SECRET_KEY` у них общий. Пути к базе
и копиям можно задать явно: `SCLAD_DATABASE`, `SCLAD_BACKUP_DIR`.

Общие настройки (например, `FLASK_DEBUG`) берутся из `.env`, а значения из файла склада
важнее. Код склада после начала работы не меняйте: по нему находится база.

## Windows (проверка и работа на одном компьютере)

1. Создайте файл настроек из примера и поправьте название:

   ```bat
   copy .env.otdel.example .env.otdel2
   notepad .env.otdel2
   ```

2. Создайте базу и первого пользователя (он станет администратором этого склада):

   ```bat
   set SCLAD_ENV_FILE=.env.otdel2
   flask --app app init-db
   flask --app app create-user -u admin -p ваш_пароль -r admin
   ```

   `set` действует только в этом окне командной строки. В новом окне без него команды
   снова работают с основным складом.

3. Запустите склад:

   ```bat
   python app.py --env .env.otdel2
   ```

   Откроется на http://127.0.0.1:5001. Основной склад в другом окне запускается как
   обычно, `python app.py`, и работает на порту 5000.

Если нужно перенести уже существующую базу отдела, положите её в папку проекта под именем
`otdel2.db` (или укажите путь в `SCLAD_DATABASE`), шаги с `init-db` и `create-user` тогда
не нужны.

## Linux-сервер (gunicorn + systemd + nginx)

Для каждого склада своя служба и свой порт. Пример для `otdel2` рядом с основным
складом из [DEPLOY_LINUX.md](DEPLOY_LINUX.md):

```bash
cd /opt/sclad
cp .env.otdel.example .env.otdel2
nano .env.otdel2                     # название, свой FLASK_SECRET_KEY
SCLAD_ENV_FILE=.env.otdel2 venv/bin/python -m flask --app app init-db
SCLAD_ENV_FILE=.env.otdel2 venv/bin/python -m flask --app app create-user -u admin -p "пароль" -r admin
sudo chown -R www-data:www-data /opt/sclad
```

`/etc/systemd/system/sclad-otdel2.service`:

```ini
[Unit]
Description=Склад отдела 2
After=network.target

[Service]
User=www-data
Group=www-data
WorkingDirectory=/opt/sclad
Environment="PATH=/opt/sclad/venv/bin"
Environment="SCLAD_ENV_FILE=/opt/sclad/.env.otdel2"
ExecStart=/opt/sclad/venv/bin/gunicorn -w 2 -b 127.0.0.1:5001 "app:create_app()"
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now sclad-otdel2
```

В nginx удобнее всего дать складу свой адрес (поддомен), тогда каждому складу свой
`server` с `proxy_pass` на свой порт:

```nginx
server {
    listen 80;
    server_name sklad2.example.com;
    location / {
        proxy_pass http://127.0.0.1:5001;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
    location /static {
        alias /opt/sclad/static;
    }
}
```

После обновления кода (`git pull`) перезапустите все службы складов:
`sudo systemctl restart sclad sclad-otdel2`.
