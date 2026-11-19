"""Рисунки техники и SIM в карточках."""
import pytest

from conftest import db_exec, db_scalar
from sclad.machine_art import machine_kind


@pytest.mark.parametrize("type_name, model, kind", [
    ("Самосвал", "БелАЗ-75131", "dump_truck"),
    ("Карьерный самосвал", None, "dump_truck"),
    ("Техника", "БелАЗ-7555", "dump_truck"),
    ("Экскаватор", "ЭКГ-10", "rope_shovel"),
    ("", "ЭКГ-5А", "rope_shovel"),
    ("Экскаватор", "Hitachi EX1200", "excavator"),
    ("Экскаватор", "ЭШ-10/70", "dragline"),
    ("Шагающий экскаватор", None, "dragline"),
    ("Роторный экскаватор", "ЭРП-1250", "bucket_wheel"),
    ("Автобус", "ПАЗ-32053", "bus"),
    ("Транспорт", "ПАЗ-4234", "bus"),
    ("Легковой", "УАЗ-469", "jeep"),
    ("Легковой", "УАЗ Патриот", "jeep"),
    ("Автомобиль", "УАЗ-452", "uaz"),
    ("УАЗ", "Буханка", "uaz"),
    ("Автомобиль", "УАЗ-2206", "uaz"),
    ("Пикап", "Toyota Hilux", "pickup"),
    ("Автомобиль", "Mitsubishi L200", "pickup"),
    ("Смешанный транспорт", "Камаз", "other"),
    ("Бульдозер", "Б-10М", "dozer"),
    ("Фронтальный погрузчик", "", "loader"),
    ("Буровой станок", "СБШ-250", "drill"),
    ("Автогрейдер", "ГС-25", "grader"),
    ("Поливочная машина", "ПМ-130", "tanker"),
    ("Топливозаправщик", None, "tanker"),
    ("Тепловоз", "ТЭМ-7А", "locomotive"),
    ("Тяговый агрегат", "НП-1", "locomotive"),
    ("Кран", "КС-45717", "other"),
    (None, None, "other"),
])
def test_machine_kind(type_name, model, kind):
    assert machine_kind(type_name, model) == kind


def test_machine_card_shows_drawing(admin, seed):
    html = admin.get(f"/machines/{seed['machine']}").get_data(as_text=True)
    assert 'class="hero-art"' in html and 'data-kind="dump_truck"' in html


def test_machine_card_drawing_for_other_type(admin, seed):
    t = db_exec("INSERT INTO machine_types (name) VALUES ('Экскаватор')")
    m = db_exec("INSERT INTO machine_models (name, machine_type_id) VALUES ('ЭКГ-10', ?)", (t,))
    mid = db_exec("INSERT INTO machines (inventory_number, machine_model_id) VALUES ('Э-1', ?)", (m,))
    html = admin.get(f"/machines/{mid}").get_data(as_text=True)
    assert 'data-kind="rope_shovel"' in html


def test_sim_card_on_stock_drawing(admin, seed):
    html = admin.get(f"/sim_cards/{seed['sim']}").get_data(as_text=True)
    assert 'class="hero-sim"' in html and "ICCID ···000001" in html
    assert "#3b82f6" in html            # на складе — синяя
    assert "стоит на борту" not in html


def test_sim_card_installed_shows_machine(admin, seed):
    db_exec("INSERT INTO machine_sim_cards (machine_id, sim_card_id) VALUES (?, ?)", (seed["machine"], seed["sim"]))
    db_exec("UPDATE sim_cards SET status_id = 2 WHERE id = ?", (seed["sim"],))
    html = admin.get(f"/sim_cards/{seed['sim']}").get_data(as_text=True)
    assert "стоит на борту" in html and "INV-001" in html and 'data-kind="dump_truck"' in html


@pytest.mark.parametrize("operator, color", [
    ("Beeline", "#ffd84d"), ("Билайн", "#ffd84d"), ("Kcell", "#9b5de5"), ("Activ", "#f04438"), ("АКТИВ", "#f04438"), ("Tele2", "#4b5563"), ("", "#3b82f6"),
])
def test_sim_color_by_operator(admin, seed, operator, color):
    db_exec("UPDATE sim_cards SET operator = ? WHERE id = ?", (operator, seed["sim"]))
    html = admin.get(f"/sim_cards/{seed['sim']}").get_data(as_text=True)
    assert color in html


def test_sim_written_off_is_grey(admin, seed):
    db_exec("UPDATE sim_cards SET status_id = 4 WHERE id = ?", (seed["sim"],))
    html = admin.get(f"/sim_cards/{seed['sim']}").get_data(as_text=True)
    assert "grayscale(1)" in html


def test_sim_operator_is_escaped(admin, seed):
    db_exec("UPDATE sim_cards SET operator = '<script>x</script>' WHERE id = ?", (seed["sim"],))
    html = admin.get(f"/sim_cards/{seed['sim']}").get_data(as_text=True)
    assert "<script>x</script>" not in html


# ---------- Рисунок, выбранный вручную в справочнике моделей ----------

def test_srsk_is_bucket_wheel():
    assert machine_kind("Экскаватор", "СРсК-2000") == "bucket_wheel"
    assert machine_kind("Экскаватор", "SRs 2000") == "bucket_wheel"


def test_chosen_art_wins_over_name():
    assert machine_kind("Экскаватор", "ЭКГ-10", "bucket_wheel") == "bucket_wheel"
    assert machine_kind("Экскаватор", "ЭКГ-10", "нет такого") == "rope_shovel"
    assert machine_kind("Экскаватор", "ЭКГ-10", None) == "rope_shovel"


def test_model_form_saves_art_and_card_uses_it(admin, seed):
    form = admin.get(f"/machine_models/{seed['model']}/edit").get_data(as_text=True)
    assert 'name="art_kind"' in form and "сейчас: Карьерный самосвал" in form
    r = admin.post(f"/machine_models/{seed['model']}/edit",
                   data={"name": "БелАЗ-75131", "machine_type_id": seed["type"], "art_kind": "bucket_wheel"})
    assert r.status_code == 302
    assert db_scalar("SELECT art_kind FROM machine_models WHERE id = ?", (seed["model"],)) == "bucket_wheel"
    html = admin.get(f"/machines/{seed['machine']}").get_data(as_text=True)
    assert 'data-kind="bucket_wheel"' in html and "Сменить рисунок" in html
    # Вернуть «автоматически»; мусор в форме тоже даёт «автоматически»
    admin.post(f"/machine_models/{seed['model']}/edit",
               data={"name": "БелАЗ-75131", "machine_type_id": seed["type"], "art_kind": "<script>"})
    assert db_scalar("SELECT art_kind FROM machine_models WHERE id = ?", (seed["model"],)) is None
    assert 'data-kind="dump_truck"' in admin.get(f"/machines/{seed['machine']}").get_data(as_text=True)


def test_model_add_with_art_and_list_shows_it(admin, seed):
    admin.post("/machine_models/add", data={"name": "СРсК-2000", "machine_type_id": seed["type"], "art_kind": "bucket_wheel"})
    assert db_scalar("SELECT art_kind FROM machine_models WHERE name = 'СРсК-2000'") == "bucket_wheel"
    html = admin.get("/machine_models").get_data(as_text=True)
    assert 'data-kind="bucket_wheel"' in html and 'class="mm-art"' in html


def test_sim_card_uses_chosen_art(admin, seed):
    db_exec("UPDATE machine_models SET art_kind = 'locomotive' WHERE id = ?", (seed["model"],))
    db_exec("INSERT INTO machine_sim_cards (machine_id, sim_card_id) VALUES (?, ?)", (seed["machine"], seed["sim"]))
    html = admin.get(f"/sim_cards/{seed['sim']}").get_data(as_text=True)
    assert 'data-kind="locomotive"' in html


def test_reader_cannot_change_art(client_for, seed):
    reader = client_for("reader")
    reader.post(f"/machine_models/{seed['model']}/edit",
                data={"name": "X", "machine_type_id": seed["type"], "art_kind": "bus"})
    assert db_scalar("SELECT art_kind FROM machine_models WHERE id = ?", (seed["model"],)) is None
