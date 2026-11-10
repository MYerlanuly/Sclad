# Склад на сервере: установка один раз, обновление одной командой

Это основная инструкция. Остальные файлы в `docs/` (DEPLOY_LINUX, DEPLOY_DEBIAN_NGINX,
DEPLOY_OFFLINE) описывают ручную настройку, они нужны только если хочется сделать иначе.

## Как это устроено

Склад работает на сервере как служба и сам стартует при включении. Новая версия ставится
одной командой. Перед обновлением она:

1. делает копию базы каждого склада (основного и других отделов) и архив текущего кода
   в `backups/before-update/`;
2. ставит пакеты Python и новую версию кода;
3. обновляет схему базы каждого склада (миграции);
4. перезапускает службы складов и проверяет, что сайт открывается.

Если после обновления что-то не так, прошлая версия возвращается одной командой (см. ниже).
Рабочие данные (`*.db`, `.env`, `backups/`, `logs/`) обновление не трогает.

Код можно доставить на сервер двумя способами:

- **сервер без интернета** (корпоративная сеть): на рабочем компьютере собирается
  один файл-пакет обновления, его копируют на сервер и ставят;
- **сервер с доступом к GitHub**: обновление само делает `git pull`.

---

## Debian-сервер без интернета (основной случай)

### Обновление (каждый раз)

**На рабочем компьютере** (там, где git-клон проекта и есть интернет):

```bat
cd C:\Users\tekhnaryad\Desktop\sclad\sclad-git
git pull
python scripts\make_update_package.py
```

Появится файл `dist\sclad-update-<дата>-<версия>.zip` (около 2 МБ): код и все пакеты Python
для сервера. По умолчанию пакет собирается под Python 3.11 (Debian 12). Если на сервере
другой Python, укажите его: `--python 3.13`.

**Перенесите файл на сервер**, например в `/tmp`: через WinSCP или из командной строки Windows:

```bat
scp dist\sclad-update-....zip пользователь@адрес-сервера:/tmp/
```

**На сервере:**

```bash
cd /opt/sclad
sudo bash update.sh /tmp/sclad-update-....zip
```

Скрипт покажет, какая версия стояла и какая ставится, и в конце напишет «Готово».
Проверить версию в любой момент: `sudo bash update.sh --version`.

### Первый переход со старой версии (один раз)

На сервере уже работает старая версия, в которой ещё нет скрипта обновления. Сначала
достаньте его из пакета, дальше всё как обычно:

```bash
cd /opt/sclad                     # папка, где лежит старая версия
sudo python3 -c "import zipfile; zipfile.ZipFile('/tmp/sclad-update-....zip').extractall('.', ['update.sh', 'scripts/update.py'])"
sudo bash update.sh /tmp/sclad-update-....zip
```

Что нужно от старой установки (так её ставили по [DEPLOY_LINUX.md](DEPLOY_LINUX.md)):
папка с проектом, окружение `venv/` внутри неё и служба systemd с именем на `sclad`
(обычно `sclad.service`). Проверить:

```bash
python3 --version
ls /opt/sclad
systemctl status sclad
```

Если служба называется иначе, укажите её: `sudo bash update.sh /tmp/...zip --services имя.service`.
nginx остаётся как был: он передаёт запросы на `127.0.0.1:5000`, а служба продолжает
слушать этот адрес.

### Новая установка с нуля (без интернета)

Нужен Debian 12 с пакетами `python3-venv` (если их нет, поставьте с установочного диска).

```bash
sudo mkdir -p /opt/sclad && cd /opt/sclad
sudo python3 -c "import zipfile; zipfile.ZipFile('/tmp/sclad-update-....zip').extractall('.')"
sudo bash scripts/linux/install.sh
```

Скрипт создаст окружение, поставит пакеты из `wheels/`, создаст `.env` с секретным ключом,
базу и администратора (если базы нет), службу `sclad` и покажет адрес вида
`http://192.168.1.10:5000`. Склад работает от пользователя `www-data`. Чтобы был адрес без
порта, поставьте перед складом nginx по [DEPLOY_DEBIAN_NGINX.md](DEPLOY_DEBIAN_NGINX.md).

Склад другого отдела: `sudo bash scripts/linux/install.sh .env.otdel2` (служба `sclad-otdel2`,
см. [MULTIPLE_WAREHOUSES.md](MULTIPLE_WAREHOUSES.md)). Обновление находит все склады само.

---

## Сервер с доступом к GitHub

Linux: `sudo git clone https://github.com/MYerlanuly/Sclad.git /opt/sclad`, затем
`sudo bash scripts/linux/install.sh`. Обновление: `cd /opt/sclad && sudo bash update.sh`
(без файла пакета скрипт делает `git pull`).

Windows: нужны [Python](https://www.python.org/downloads/) (галочка «Add python.exe to PATH»)
и [Git for Windows](https://git-scm.com/download/win).

```bat
cd C:\
git clone https://github.com/MYerlanuly/Sclad.git sclad
```

Затем двойной щелчок по `C:\sclad\scripts\windows\install.bat` (склад отдела:
`scripts\windows\install.bat .env.otdel2`). Служба — задача «Sclad» в Планировщике заданий,
стартует вместе с Windows, журнал в `C:\sclad\logs\`, порт задаётся `SCLAD_PORT` в `.env`.
Обновление: двойной щелчок по `C:\sclad\update.bat`. Windows-сервер без интернета обновляется
пакетом `python scripts\make_update_package.py --windows --python <версия>`:
`update.bat C:\путь\к\sclad-update-....zip`.

---

## Если после обновления что-то не так

| Что сделать | Linux | Windows |
|---|---|---|
| Вернуть прошлую версию кода (данные остаются) | `sudo bash update.sh --rollback` | `update.bat --rollback` |
| Вернуть и код, и базы на момент обновления | `sudo bash update.sh --rollback --restore-db` | `update.bat --rollback --restore-db` |

Второй вариант теряет всё, что внесли в склад после обновления, поэтому он спрашивает
подтверждение и перед возвратом ещё раз сохраняет текущие базы. Обычно хватает первого:
миграции только добавляют таблицы и поля, старый код с ними работает.

Возможные сообщения:

- **«Пакет собран для Python 3.11, а на сервере Python 3.x»** — соберите пакет с
  `--python 3.x`. Ничего не изменено.
- **«есть изменённые вручную файлы»** (только git) — на сервере кто-то правил код. Посмотрите
  `git diff`; если правки не нужны, выполните `git checkout -- .` и запустите снова.
- **«сайт не ответил»** — журнал службы: `journalctl -u sclad -n 50` (Windows: папка `logs\`).
