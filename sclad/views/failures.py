"""Маршруты: отчёт «Повторные отказы»."""
from flask import render_template, request

from ..failures import (
    DEFAULT_DAYS, MIN_REPEATS, PERIODS, frequent_positions, machines_by_faults, repeated_faulty_equipment,
)
from ..utils import to_int


def register(app):
    @app.route("/reports/failures")
    def failures_report():
        days = to_int(request.args.get("days"), DEFAULT_DAYS)
        if days not in PERIODS:
            days = DEFAULT_DAYS
        return render_template(
            "failures_report.html",
            days=days,
            periods=PERIODS,
            min_repeats=MIN_REPEATS,
            positions=frequent_positions(days),
            equipment=repeated_faulty_equipment(days),
            machines=machines_by_faults(days),
        )
