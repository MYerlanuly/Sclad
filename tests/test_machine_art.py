"""Рисунки техники и SIM в карточках."""
import pytest

from conftest import db_exec
from sclad.machine_art import machine_kind


@pytest.mark.parametrize("type_name, model, kind", [
    ("Самосвал", "БелАЗ-75131", "dump_truck"),
    ("Карьерный самосвал", None, "dump_truck"),
    ("Техника", "БелАЗ-7555", "dump_truck"),
    ("Экскаватор", "ЭКГ-10", "excavator"),
    ("", "ЭКГ-5А", "excavator"),
    ("Бульдозер", "Б-10М", "dozer"),
    ("Фронтальный погрузчик", "", "loader"),
    ("Буровой станок", "СБШ-250", "drill"),
    ("Автогрейдер", "ГС-25", "grader"),
    ("Поливочная машина", "ПМ-130", "tanker"),
    ("Топливозаправщик", None, "tanker"),
    ("Тепловоз", "ТЭМ-7А", "locomotive"),
    ("Тяговый агрегат", "НП-1", "locomotive"),
    ("Автобус", "ПАЗ", "other"),
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
    assert 'data-kind="excavator"' in html


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
    assert "#22c55e" in html            # стоит на борту — зелёная


def test_sim_operator_is_escaped(admin, seed):
    db_exec("UPDATE sim_cards SET operator = '<script>x</script>' WHERE id = ?", (seed["sim"],))
    html = admin.get(f"/sim_cards/{seed['sim']}").get_data(as_text=True)
    assert "<script>x</script>" not in html
