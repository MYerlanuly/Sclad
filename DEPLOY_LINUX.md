# Развёртывание на Linux-сервере (без GUI)

Инструкция для сервера **без графического интерфейса**: все шаги выполняются по SSH в терминале. В итоге приложение доступно **по порту 80** в браузере: `http://IP_СЕРВЕРА` или `http://имя-сервера`.

Подходит для Ubuntu/Debian и аналогичных дистрибутивов.

---

## 0. Виртуальная машина Debian — характеристики и подготовка «под интернетом»

### Характеристики ВМ (рекомендуемые)

Для «Склад АСДУ» (Flask + SQLite, без тяжёлой нагрузки) достаточно:

| Параметр   | Минимум | Рекомендуется |
|-----------|---------|----------------|
| **Ядра CPU** | 1       | 2              |
| **ОЗУ**      | 512 МБ  | 1 ГБ           |
| **Диск**     | 8 ГБ    | 15–20 ГБ       |
| **Сеть**     | NAT или мост | мост (чтобы был свой IP) |

Установка: **Debian 12 (Bookworm)** без графики — при установке выберите «Server» или «Debian system utilities» без рабочего стола.

### Подготовить всё с интернетом и перенести диск

Цель: один раз настроить ВМ там, где есть интернет, затем перенести ВМ/диск в корпоративную сеть без интернета.

**Шаг 0. Установить Debian** (сеть с интернетом), зайти по SSH.

**Шаг 1. Установить системные пакеты (интернет нужен):**

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip nginx git
```

**Шаг 2. Скопировать проект в `/opt/sclad`** (git или архив, см. раздел 2 ниже).

**Шаг 3. Скачать офлайн-ресурсы и зависимости в проекте:**

```bash
cd /opt/sclad
python3 scripts/download_offline_assets.py
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
pip install gunicorn
```

(Если позже сервер будет без интернета, дополнительно выполните:  
`pip download -r requirements.txt -d wheels` и `pip download gunicorn -d wheels`, затем скопируйте папку `wheels` вместе с проектом.)

**Шаг 4. Настроить приложение (один раз):**

```bash
cd /opt/sclad
cp .env.example .env
nano .env   # задать FLASK_SECRET_KEY=случайная_длинная_строка
source venv/bin/activate
python -m flask init-db
python -m flask create-user -u admin -p "ваш_надёжный_пароль"
```

**Шаг 5. Включить службу приложения (systemd)**

Создать файл службы:

```bash
sudo nano /etc/systemd/system/sclad.service
```

Вставить (если проект не в `/opt/sclad` — замените путь):

```ini
[Unit]
Description=Склад АСДУ
After=network.target

[Service]
User=www-data
Group=www-data
WorkingDirectory=/opt/sclad
Environment="PATH=/opt/sclad/venv/bin"
ExecStart=/opt/sclad/venv/bin/gunicorn -w 2 -b 127.0.0.1:5000 "app:create_app()"
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

Сохранить (в nano: Ctrl+O, Enter, Ctrl+X). Затем:

```bash
sudo chown -R www-data:www-data /opt/sclad
sudo systemctl daemon-reload
sudo systemctl enable sclad
sudo systemctl start sclad
sudo systemctl status sclad
```

В статусе должно быть `active (running)`. Выход из статуса: `q`.

**Шаг 6. Настроить Nginx на порт 80**

Создать конфиг сайта:

```bash
sudo nano /etc/nginx/sites-available/sclad
```

Вставить (путь `/opt/sclad` замените, если проект в другой папке):

```nginx
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;

    location / {
        proxy_pass http://127.0.0.1:5000;
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

Сохранить. Включить сайт и перезапустить Nginx:

```bash
sudo ln -sf /etc/nginx/sites-available/sclad /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx
```

Если `nginx -t` выдал ошибку — проверьте путь `/opt/sclad` и кавычки в конфиге.

**Шаг 7. Проверить в браузере:** открыть **http://IP_ВМ** (порт 80) — должна открыться страница входа.

После этого можно выключить ВМ, экспортировать/скопировать диск или саму ВМ и перенести в целевую сеть. После переноса при необходимости поменяйте IP (или настройте DHCP) и при необходимости откройте порт 80 в файрволе. База данных и все настройки останутся на диске.

---

## 1. Требования

- **Python 3.8+** (проверка: `python3 --version`)
- Права на установку пакетов (`pip` или `apt`)

---

## 2. Копирование проекта на сервер

Подойдёт любой способ — **архивом** или **целиком папкой** (по сети, флешкой, общая папка и т.п.).

**Способ А — Git** (если репозиторий доступен с сервера):

```bash
cd /opt
sudo git clone URL_ВАШЕГО_РЕПОЗИТОРИЯ sclad
cd sclad
```

**Способ Б — папкой** (скопировать каталог как есть):

- Через SCP с вашего ПК:  
  `scp -r /путь/к/Sclad user@IP_СЕРВЕРА:/opt/sclad`
- Или: общая папка / флешка — скопировать папку проекта в `/opt/sclad` на сервере.

На сервере затем: `sudo chown -R www-data:www-data /opt/sclad` (если служба от `www-data`).

**Способ В — архивом** (удобно, если много мелких файлов или перенос по почте/флешке):

```bash
# На своём компьютере (в папке с проектом):
zip -r sclad.zip . -x "*.pyc" "__pycache__/*" ".git/*"

# На сервере:
sudo mkdir -p /opt/sclad
# скопировать sclad.zip на сервер, затем:
cd /opt/sclad
sudo unzip /путь/к/sclad.zip
sudo chown -R $USER:$USER /opt/sclad
```

---

## 3. Офлайн-ресурсы (если на сервере нет интернета)

Если развёртывание **без интернета**, сначала на машине **с интернетом** выполните:

```bash
python3 scripts/download_offline_assets.py
pip download -r requirements.txt -d wheels
```

Скопируйте на сервер папки `static/js/` и при необходимости `wheels/`.

Если на сервере **есть интернет**, скрипт можно запустить прямо на сервере:

```bash
python3 scripts/download_offline_assets.py
```

---

## 4. Виртуальное окружение и зависимости

```bash
cd /opt/sclad

# Создать виртуальное окружение
python3 -m venv venv

# Активировать
source venv/bin/activate

# Установить зависимости
# Вариант 1: с интернетом
pip install -r requirements.txt
pip install gunicorn

# Вариант 2: без интернета (если подготовили wheels)
pip install --no-index --find-links wheels -r requirements.txt
pip install --no-index --find-links wheels gunicorn
```

---

## 5. Настройка окружения и БД

```bash
# Скопировать пример настроек и отредактировать
cp .env.example .env
nano .env   # или vi .env
```

В `.env` задайте минимум:

- `FLASK_SECRET_KEY=какая-то-длинная-случайная-строка`

Создать БД и пользователя (один раз):

```bash
source venv/bin/activate
python -m flask init-db
python -m flask create-user -u admin -p "ваш_надёжный_пароль"
```

---

## 6. Проверка запуска (вручную)

```bash
source venv/bin/activate
gunicorn -w 2 -b 127.0.0.1:5000 "app:create_app()"
```

В браузере с **этого же сервера** откройте `http://127.0.0.1:5000`. Если страница входа открывается — приложение работает. Остановка: `Ctrl+C`.

Доступ по порту 80 с других компьютеров будет после шагов 7 и 8 (systemd + Nginx).

---

## 7. Запуск приложения как службы (systemd)

Чтобы приложение работало в фоне и поднималось после перезагрузки сервера.

Создайте файл службы:

```bash
sudo nano /etc/systemd/system/sclad.service
```

Вставьте (путь `/opt/sclad` и пользователь `www-data` при необходимости замените):

```ini
[Unit]
Description=Склад АСДУ
After=network.target

[Service]
User=www-data
Group=www-data
WorkingDirectory=/opt/sclad
Environment="PATH=/opt/sclad/venv/bin"
ExecStart=/opt/sclad/venv/bin/gunicorn -w 2 -b 127.0.0.1:5000 "app:create_app()"
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

Права и запуск:

```bash
sudo chown -R www-data:www-data /opt/sclad
sudo systemctl daemon-reload
sudo systemctl enable sclad
sudo systemctl start sclad
sudo systemctl status sclad
```

Дальше: перезапуск — `sudo systemctl restart sclad`, логи — `sudo journalctl -u sclad -f`.

---

## 8. Доступ по порту 80 через Nginx (обязательно для браузера)

Чтобы заходить в приложение по адресу **http://IP_СЕРВЕРА** (порт 80) с любого компьютера в сети.

Установите Nginx и создайте конфиг:

```bash
sudo apt update
sudo apt install nginx -y
sudo nano /etc/nginx/sites-available/sclad
```

Вставьте (вместо `ВАШ_СЕРВЕР` укажите IP сервера или домен; для доступа по любому имени оставьте `_`):

```nginx
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name ВАШ_СЕРВЕР _;

    location / {
        proxy_pass http://127.0.0.1:5000;
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

Включите сайт и перезапустите Nginx:

```bash
sudo ln -sf /etc/nginx/sites-available/sclad /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx
```

Откройте в браузере с любого ПК в сети: **http://IP_СЕРВЕРА** (порт 80 указывать не нужно). Должна открыться страница входа.

Если порт 80 закрыт файрволом, откройте его, например (Ubuntu/Debian с ufw):

```bash
sudo ufw allow 80/tcp
sudo ufw reload
```

---

## 9. Краткая шпаргалка

| Действие              | Команда |
|-----------------------|--------|
| Войти в каталог      | `cd /opt/sclad` |
| Активировать venv    | `source venv/bin/activate` |
| Статус приложения    | `sudo systemctl status sclad` |
| Перезапуск приложения | `sudo systemctl restart sclad` |
| Логи приложения      | `sudo journalctl -u sclad -f` |
| Статус Nginx         | `sudo systemctl status nginx` |
| Перезапуск Nginx     | `sudo systemctl reload nginx` |
| Резервная копия БД   | `cp /opt/sclad/inventory.db /резерв/inventory_$(date +%Y%m%d).db` |

После шагов 1–8 приложение доступно в браузере по адресу **http://IP_СЕРВЕРА** (порт 80), без GUI на сервере.
