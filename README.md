# Склад АСДУ

Веб-приложение для учёта оборудования АСДУ (бортовые терминалы, датчики, SIM-карты) на карьерной технике:
склад, установка на технику и демонтаж, техобслуживание с печатными актами, задания на смену,
журнал смены инженеров, журнал операций, отчёты, импорт и экспорт, резервные копии базы.

**Технологии:** Python 3.13+, Flask, SQLite, Jinja2, Tailwind, Chart.js. В продакшене: gunicorn за nginx.

## Быстрый старт (Windows, для разработки и проверки)

```bat
git clone https://github.com/MYerlanuly/Sclad.git sclad-git
cd sclad-git
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt

flask --app app init-db
flask --app app create-user -u admin -p ваш_пароль -r admin
python app.py
```

Откройте http://127.0.0.1:5000. Чтобы работать с реальными данными, положите копию рабочей
`inventory.db` в папку проекта (в git она не попадает).

На Linux команды те же, только окружение активируется через `source venv/bin/activate`.

## Настройки (`.env`)

Скопируйте `.env.example` в `.env` и задайте значения:

| Переменная | Назначение |
|---|---|
| `FLASK_SECRET_KEY` | Секрет для подписи сессий. Обязательно длинный случайный: `python -c "import secrets; print(secrets.token_hex(32))"` |
| `FLASK_DEBUG` | `1` только для разработки, в продакшене `0` |
| `SCLAD_DATABASE` | Необязательно: путь к файлу базы (по умолчанию `inventory.db` в папке проекта) |
| `SCLAD_INSTANCE`, `SCLAD_NAME`, `SCLAD_PORT`, `SCLAD_BACKUP_DIR` | Для склада другого отдела, см. ниже |

## Склад для другого отдела

Второй склад со своей базой, пользователями и копиями запускается из той же папки
с отдельным файлом настроек:

```bat
copy .env.otdel.example .env.otdel2
python app.py --env .env.otdel2
```

Подробно, включая сервер: [docs/MULTIPLE_WAREHOUSES.md](docs/MULTIPLE_WAREHOUSES.md).

## Роли пользователей

| Роль | Что может |
|---|---|
| `admin` | Всё, включая пользователей, настройки и скачивание резервной копии базы |
| `editor` | Изменять данные: склад, техника, SIM, ТО, задания, журнал смены |
| `maintenance` | Записывать ТО, работать с заданиями на смену (свои задания может править и удалять) и вести журнал смены |
| `reader` | Только просмотр |

Первый созданный пользователь всегда становится `admin`.

## Структура

```
app.py                 точка входа: python app.py или gunicorn app:app
sclad/                 код приложения
  __init__.py          create_app(): сборка приложения из модулей
  config.py            пути и константы (база, схема, папка копий)
  db.py                соединение с базой, fetch_*, настройки, журнал операций
  migrations.py        миграции схемы, init_database(), run_migrations()
  auth.py              пользователи, вход/выход, права ролей, заголовки безопасности
  backup.py            резервные копии базы
  cli.py               команды flask init-db / create-user / import-journal
  shift_journal.py     журнал смены: правила смен, выгрузка в Word, письмо для Outlook
  journal_import.py    перенос данных из старой программы журнала (PostgreSQL / CSV)
  context.py           данные для всех шаблонов (подсказки, «Недавние»)
  utils.py             разбор ввода, даты, форматирование
  views/               разделы сайта, каждый в своём файле:
    dashboard.py       главная, быстрый поиск
    users.py           пользователи и профиль
    equipment.py       оборудование
    references.py      типы и модели техники, позиции датчиков
    machines.py        техника
    installs.py        установка и демонтаж оборудования и SIM
    sim_cards.py       SIM-карты
    maintenance.py     ТО, календарь, акты
    shift_tasks.py     задания на смену, поиск оборудования
    journal.py         журнал смены инженеров (см. docs/JOURNAL.md)
    reports.py         журнал операций, отчёты, экспорт
    imports.py         импорт из CSV
    settings.py        настройки
    backup.py          скачивание копии базы, копии по расписанию
schema.sql             базовая схема и справочники
templates/             HTML-шаблоны (Jinja2)
static/                логотип, Tailwind и Chart.js (локально, для работы без интернета)
scripts/               вспомогательные скрипты
tests/                 автотесты и нагрузочный тест
docs/                  инструкции по развёртыванию и заметки
```

Новый раздел добавляется так: создайте `sclad/views/<раздел>.py` с функцией `register(app)`,
внутри которой объявлены маршруты, и добавьте модуль в `sclad/views/__init__.py`.

Схема базы обновляется автоматически при каждом запуске (`run_migrations()`),
каждая миграция безопасна при повторном выполнении. Новую миграцию добавьте в
`sclad/migrations.py` и в список `MIGRATIONS`.

## Тесты

```bat
pip install -r requirements-dev.txt
python -m pytest
```

Тесты создают отдельную временную базу и не трогают рабочую `inventory.db`. Проверяется:

- все страницы открываются под каждой ролью без ошибок;
- права ролей, CSRF, SQL-инъекции, XSS, редиректы после входа, блокировка подбора пароля;
- целостность данных: уникальность, установка и демонтаж, одна SIM на машину, одно место на датчик,
  запрет удаления используемых записей, одновременные действия нескольких пользователей;
- устойчивость к некорректному вводу в любой форме и параметре адреса;
- импорт и экспорт, резервная копия.

Нагрузочный тест (запускается отдельно, создаёт свою базу на 400 машин и 6000 единиц оборудования):

```bat
python tests/load_test.py --users 20 --seconds 30
```

## Развёртывание

- [docs/DEPLOY_LINUX.md](docs/DEPLOY_LINUX.md): Linux, venv, gunicorn, systemd
- [docs/DEPLOY_DEBIAN_NGINX.md](docs/DEPLOY_DEBIAN_NGINX.md): Debian с nginx
- [docs/DEPLOY_OFFLINE.md](docs/DEPLOY_OFFLINE.md): установка на сервер без интернета
- [docs/MULTIPLE_WAREHOUSES.md](docs/MULTIPLE_WAREHOUSES.md): несколько складов для разных отделов
