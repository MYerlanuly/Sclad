"""Причины демонтажа оборудования и SIM-карт.

Причина определяет статус, который получает снятый объект: неисправное оборудование
не возвращается в «На складе» и не попадает в списки для установки.
"""

# (код, подпись, id статуса оборудования после снятия)
EQUIPMENT_REMOVAL_REASONS = (
    ("to_stock", "Исправен, вернуть на склад", 1),  # На складе: сразу можно ставить снова
    ("faulty", "Неисправен", 4),           # Неисправно
    ("damaged", "Повреждён", 4),           # Неисправно
    ("planned", "Плановая замена, нужна проверка", 3),  # Демонтировано
    ("machine_out", "Борт выводится из работы", 3),
    ("other", "Другая причина", 3),
)

# (код, подпись, id статуса SIM после снятия)
SIM_REMOVAL_REASONS = (
    ("to_stock", "Исправна, вернуть на склад", 1),  # На складе
    ("planned", "Перестановка или замена", 1),
    ("faulty", "Нет связи, неисправна", 3),      # Заблокирована: на установку не выдаётся
    ("blocked", "Заблокирована оператором", 3),
    ("machine_out", "Борт выводится из работы", 1),
    ("other", "Другая причина", 1),
)

# Статус по умолчанию, если причина не указана (как было до причин)
EQUIPMENT_DEFAULT_STATUS = 3
SIM_DEFAULT_STATUS = 1

COMMENT_MAX = 500

EQUIPMENT_REASON_LABELS = {code: label for code, label, _ in EQUIPMENT_REMOVAL_REASONS}
SIM_REASON_LABELS = {code: label for code, label, _ in SIM_REMOVAL_REASONS}


def parse_removal(form, kind):
    """Причина, комментарий и новый статус из формы демонтажа.

    kind — 'equipment' или 'sim'. Неизвестная или пустая причина → (None, comment, статус по умолчанию).
    """
    reasons = EQUIPMENT_REMOVAL_REASONS if kind == "equipment" else SIM_REMOVAL_REASONS
    default_status = EQUIPMENT_DEFAULT_STATUS if kind == "equipment" else SIM_DEFAULT_STATUS
    code = (form.get("reason") or "").strip()
    comment = " ".join((form.get("comment") or "").split())[:COMMENT_MAX] or None
    for c, _label, status_id in reasons:
        if c == code:
            return c, comment, status_id
    return None, comment, default_status


def reason_text(kind, code, comment):
    """Причина для журнала и истории: «Неисправен: не передаёт давление»."""
    labels = EQUIPMENT_REASON_LABELS if kind == "equipment" else SIM_REASON_LABELS
    label = labels.get(code or "")
    if label and comment:
        return f"{label}: {comment}"
    return label or comment or ""
