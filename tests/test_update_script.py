"""Скрипт обновления на сервере (scripts/update.py): копии баз перед обновлением."""
import importlib.util
import os
import sqlite3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("sclad_update", os.path.join(ROOT, "scripts", "update.py"))
update = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(update)


def _make_db(path, rows):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (v INTEGER)")
    conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(rows)])
    conn.commit()
    conn.close()


def test_backup_copies_each_warehouse_and_keeps_last_copies(tmp_path, monkeypatch):
    monkeypatch.setattr(update, "KEEP_BACKUPS", 3)
    main_db, other_db = tmp_path / "inventory.db", tmp_path / "otdel2.db"
    _make_db(main_db, 5)
    _make_db(other_db, 7)
    warehouses = [
        {"NAME": "Основной", "DATABASE": str(main_db), "BACKUP_ROOT": str(tmp_path / "backups"),
         "BACKUP_FILE_PREFIX": "inventory"},
        {"NAME": "Отдел 2", "DATABASE": str(other_db), "BACKUP_ROOT": str(tmp_path / "backups" / "otdel2"),
         "BACKUP_FILE_PREFIX": "otdel2"},
    ]
    made = {}
    for i in range(5):
        made = update.backup_databases(warehouses, f"v{i}")

    copy = made[str(other_db)]
    assert os.path.dirname(copy) == str(tmp_path / "backups" / "otdel2" / "before-update")
    conn = sqlite3.connect(copy)
    assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 7
    conn.close()
    kept = sorted(os.listdir(tmp_path / "backups" / "before-update"))
    assert len(kept) == 3 and all(name.startswith("inventory_") for name in kept)
