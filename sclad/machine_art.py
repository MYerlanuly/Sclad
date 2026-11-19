"""Рисунок техники в карточках: вид машины по типу и модели борта.

Рисунки — встроенные SVG в templates/_machine_art.html (без файлов и интернета).
Вид выбирается по ключевым словам в названии типа, затем модели; неизвестное — «другая техника».
"""

# (вид, подпись, ключевые слова в нижнем регистре). Порядок важен: первое совпадение.
KINDS = (
    ("locomotive", "Локомотив", ("локомотив", "тепловоз", "электровоз", "тяговый агрегат", "тэм", "тгм", "2тэ", "нп-1", "locomotive")),
    ("tanker", "Цистерна", ("полив", "цистерн", "водовоз", "заправщ", "бензовоз", "tanker", "water truck")),
    ("dump_truck", "Карьерный самосвал", ("самосвал", "белаз", "dump", "haul")),
    ("excavator", "Экскаватор", ("экскават", "экг", "shovel", "excavator")),
    ("dozer", "Бульдозер", ("бульдоз", "dozer")),
    ("loader", "Погрузчик", ("погрузч", "loader")),
    ("drill", "Буровой станок", ("буров", "сбш", "drill")),
    ("grader", "Грейдер", ("грейдер", "grader")),
)
OTHER = "other"
KIND_LABELS = {k: label for k, label, _ in KINDS} | {OTHER: "Техника"}


def machine_kind(type_name, model_name=None):
    """Вид техники для рисунка: сначала по типу, затем по модели."""
    for text in (type_name, model_name):
        low = (text or "").lower()
        if not low:
            continue
        for kind, _label, words in KINDS:
            if any(w in low for w in words):
                return kind
    return OTHER
