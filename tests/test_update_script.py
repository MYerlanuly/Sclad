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


def test_package_replaces_code_keeps_data_and_rolls_back(tmp_path, monkeypatch):
    import json
    import zipfile

    root = tmp_path / "srv"
    (root / "sclad").mkdir(parents=True)
    (root / "sclad" / "old.py").write_text("old", encoding="utf-8")
    (root / "app.py").write_text("v1", encoding="utf-8")
    (root / "inventory.db").write_bytes(b"data")
    (root / ".env").write_text("FLASK_SECRET_KEY=x", encoding="utf-8")
    (root / ".env.otdel2").write_text("SCLAD_INSTANCE=otdel2", encoding="utf-8")
    monkeypatch.setattr(update, "ROOT", str(root))

    pkg = tmp_path / "pkg.zip"
    with zipfile.ZipFile(pkg, "w") as zf:
        zf.writestr("app.py", "v2")
        zf.writestr("sclad/new.py", "new")
        zf.writestr("wheels/a-1.0-py3-none-any.whl", "whl")
        zf.writestr(update.MANIFEST, json.dumps({"files": ["app.py", "sclad/new.py"], "wheels": ["a-1.0-py3-none-any.whl"]}))

    warehouses = [{"BACKUP_ROOT": str(root / "backups")}]
    code_backup = update.backup_code(warehouses)
    with zipfile.ZipFile(pkg) as zf:
        update.apply_package(zf, json.loads(zf.read(update.MANIFEST)))

    assert (root / "app.py").read_text(encoding="utf-8") == "v2"
    assert (root / "sclad" / "new.py").exists() and not (root / "sclad" / "old.py").exists()
    assert (root / "inventory.db").read_bytes() == b"data"
    assert (root / ".env").exists() and (root / ".env.otdel2").exists()

    # Откат кода из архива: возвращает старые файлы и убирает новые
    with zipfile.ZipFile(code_backup) as zf:
        saved = set(zf.namelist())
        update._remove_files(sorted(set(update.code_files()) - saved))
        update._write_files(zf, sorted(saved))
    assert (root / "app.py").read_text(encoding="utf-8") == "v1"
    assert (root / "sclad" / "old.py").exists() and not (root / "sclad" / "new.py").exists()
    assert not (root / "wheels" / "a-1.0-py3-none-any.whl").exists()
