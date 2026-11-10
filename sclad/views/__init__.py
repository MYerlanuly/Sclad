"""Разделы сайта. Каждый модуль регистрирует свои маршруты функцией register(app).

Имена маршрутов (endpoint) остались прежними, поэтому шаблоны и url_for не менялись.
"""
from . import backup, dashboard, equipment, imports, installs, maintenance, machines, references, reports
from . import settings, shift_tasks, sim_cards, users

MODULES = (
    dashboard, users, equipment, references, machines, installs, sim_cards, maintenance, shift_tasks,
    reports, imports, settings, backup,
)


def register_all(app):
    for module in MODULES:
        module.register(app)
