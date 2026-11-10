"""Команды flask: init-db, create-user, import-journal."""
import sqlite3

import click
from werkzeug.security import generate_password_hash

from .migrations import init_database, migrate_users, migrate_users_profile_and_roles, run_migrations


def register(app):
    @app.cli.command("init-db")
    def init_db_command():
        """Инициализация базы данных из schema.sql и применение миграций."""
        init_database()
        run_migrations()
        print("База данных инициализирована.")

    @app.cli.command("create-user")
    @click.option("--username", "-u", required=True, help="Логин пользователя")
    @click.option("--password", "-p", required=True, help="Пароль")
    @click.option("--role", "-r", default="reader", type=click.Choice(["admin", "editor", "maintenance", "reader"]), help="Роль: admin, editor, maintenance, reader")
    def create_user_command(username, password, role):
        """Создать пользователя для входа в систему."""
        migrate_users()
        migrate_users_profile_and_roles()
        conn = sqlite3.connect(app.config["DATABASE"])
        existing = conn.execute("SELECT id FROM users WHERE username = ?", (username.strip(),)).fetchone()
        if existing:
            print(f"Пользователь '{username}' уже существует.")
            conn.close()
            return
        count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if count == 0:
            role = "admin"
            print("Первый пользователь создаётся с ролью «Администратор».")
        pw_hash = generate_password_hash(password, method="scrypt")
        conn.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
            (username.strip(), pw_hash, role),
        )
        conn.commit()
        conn.close()
        print(f"Пользователь '{username}' создан (роль: {role}).")

    @app.cli.command("import-journal")
    @click.option("--csv-dir", type=click.Path(exists=True, file_okay=False), help="Папка с journal.csv, engineers.csv, engineers_info.csv (выгрузка из pgAdmin)")
    @click.option("--pg", "pg_dsn", help='Подключение к PostgreSQL, например: "host=localhost dbname=shift_journal_db user=postgres password=..."')
    @click.option("--dry-run", is_flag=True, help="Только показать, что будет перенесено, без записи")
    def import_journal_command(csv_dir, pg_dsn, dry_run):
        """Перенести записи из старой программы «Журнал инженеров по АСУ»."""
        from .journal_import import import_journal, read_csv_dir, read_postgres
        from .migrations import migrate_shift_journal

        if bool(csv_dir) == bool(pg_dsn):
            raise click.UsageError("Укажите ровно один источник: --csv-dir или --pg.")
        journal_rows, engineers_rows, info_rows = read_csv_dir(csv_dir) if csv_dir else read_postgres(pg_dsn)
        migrate_shift_journal()
        conn = sqlite3.connect(app.config["DATABASE"])
        try:
            report = import_journal(conn, journal_rows, engineers_rows, info_rows)
            if dry_run:
                conn.rollback()
            else:
                conn.commit()
        finally:
            conn.close()
        print(("Проверка (ничего не записано):" if dry_run else "Перенос журнала завершён:"))
        print(f"  записей добавлено: {report['entries_added']}, уже были: {report['entries_skipped']}, с ошибками в дате/смене/времени: {report['entries_bad']}")
        print(f"  инженеров на сменах добавлено: {report['engineers_added']}, пропущено: {report['engineers_skipped']}")
        print(f"  табельных номеров записано в профили: {report['personnel_numbers_set']}")
        if report["unmatched_engineers"]:
            print("  нет пользователя склада с таким ФИО (табельный номер не перенесён):")
            for name in report["unmatched_engineers"]:
                print(f"    - {name}")
