"""Рисунки в карточках: вид техники по типу и модели борта, цвет SIM-карты по оператору.

Рисунки — встроенные SVG в templates/_machine_art.html (без файлов и интернета).
Вид выбирается по ключевым словам в названии типа и модели; неизвестное — общий рисунок.
"""
import re

# (вид, подпись, ключевые слова в нижнем регистре). Порядок важен: первое совпадение,
# поэтому точные виды (ЭКГ, ЭШ, роторный) стоят раньше общего «экскаватора».
# Короткие слова и номера (до 3 знаков: «эш», «паз», «уаз», «469») ищутся только с начала слова.
KINDS = (
    ("locomotive", "Локомотив", ("локомотив", "тепловоз", "электровоз", "тяговый агрегат", "тэм", "тгм", "2тэ", "нп-1", "locomotive")),
    ("dragline", "Шагающий экскаватор", ("шагающ", "драглайн", "эш", "dragline")),
    ("bucket_wheel", "Роторный экскаватор", ("роторн", "эрп", "эр-", "срс", "шрс", "srs", "schrs", "bucket wheel")),
    ("rope_shovel", "Экскаватор ЭКГ", ("экг", "мехлопат", "канатн", "shovel")),
    ("bus", "Автобус", ("автобус", "паз", "вахтов", "bus")),
    ("jeep", "Внедорожник", ("патриот", "хантер", "469", "3151", "внедорож", "джип", "jeep", "patriot", "hunter")),
    ("uaz", "УАЗ «буханка»", ("буханк", "уаз", "452", "2206", "3909", "3741", "3962", "uaz")),
    ("pickup", "Пикап", ("пикап", "pickup", "hilux", "хайлюкс", "l200", "navara", "ranger", "amarok", "амарок", "профи", "2363")),
    ("tanker", "Цистерна", ("полив", "цистерн", "водовоз", "заправщ", "бензовоз", "tanker", "water truck")),
    ("dump_truck", "Карьерный самосвал", ("самосвал", "белаз", "dump", "haul")),
    ("excavator", "Экскаватор", ("экскават", "excavator")),
    ("dozer", "Бульдозер", ("бульдоз", "dozer")),
    ("loader", "Погрузчик", ("погрузч", "loader")),
    ("drill", "Буровой станок", ("буров", "сбш", "drill")),
    ("grader", "Грейдер", ("грейдер", "grader")),
)
OTHER = "other"
KIND_LABELS = {k: label for k, label, _ in KINDS} | {OTHER: "Другая техника"}


def _pattern(word):
    w = re.escape(word)
    return rf"(?<![0-9a-zа-яё]){w}" if len(word) <= 3 else w


_RULES = tuple((kind, re.compile("|".join(_pattern(w) for w in words))) for kind, _label, words in KINDS)


# Порядок рисунков в выборе (справочник моделей)
ART_CHOICES = tuple((k, KIND_LABELS[k]) for k in (
    "dump_truck", "rope_shovel", "excavator", "dragline", "bucket_wheel", "dozer", "loader", "drill", "grader",
    "tanker", "locomotive", "bus", "uaz", "jeep", "pickup", OTHER,
))


def clean_art_kind(value):
    """Выбор рисунка из формы: известный вид или None (подбирать по названию)."""
    value = (value or "").strip()
    return value if value in KIND_LABELS else None


def machine_kind(type_name, model_name=None, chosen=None):
    """Вид техники для рисунка. chosen — выбран вручную в справочнике моделей.

    Иначе по типу и модели вместе: «Экскаватор» + «ЭКГ-10» → ЭКГ.
    """
    if chosen in KIND_LABELS:
        return chosen
    text = f"{type_name or ''} {model_name or ''}".lower().strip()
    if text:
        for kind, rx in _RULES:
            if rx.search(text):
                return kind
    return OTHER


# Цвет SIM-карты по оператору: (светлый, тёмный, цвет текста)
SIM_PALETTES = (
    (("beeline", "билайн", "би-лайн"), ("#ffd84d", "#f2b705", "#1f2937")),
    (("activ", "актив"), ("#f04438", "#b42318", "#ffffff")),
    (("kcell", "кселл", "к-селл"), ("#9b5de5", "#5b2a86", "#ffffff")),
    (("tele2", "теле2", "теле 2"), ("#4b5563", "#111827", "#ffffff")),
    (("altel", "алтел"), ("#f87171", "#b91c1c", "#ffffff")),
)
SIM_DEFAULT_PALETTE = ("#3b82f6", "#1e40af", "#ffffff")


def sim_palette(operator):
    low = (operator or "").lower()
    for words, palette in SIM_PALETTES:
        if any(w in low for w in words):
            return palette
    return SIM_DEFAULT_PALETTE
