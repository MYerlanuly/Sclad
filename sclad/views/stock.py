"""Маршруты: минимальные остатки склада."""
from flask import flash, redirect, render_template, request, url_for
from flask_login import current_user

from ..db import get_db, log_operation
from ..stock import SIM_KEY, stock_rows

MIN_QTY_MAX = 100000


def register(app):
    @app.route("/stock", methods=["GET", "POST"])
    def stock_minimums():
        can_edit = getattr(current_user, "can_edit", False)
        if request.method == "POST":
            if not can_edit:
                flash("Менять минимальные остатки могут только администратор и редактор.", "error")
                return redirect(url_for("stock_minimums"))
            known = {r["key"] for r in stock_rows()}
            db = get_db()
            changed, errors = 0, []
            for key in known:
                raw = request.form.get(f"min__{key}")
                if raw is None:
                    continue
                raw = raw.strip()
                if raw in ("", "0"):
                    cur = db.execute("DELETE FROM stock_minimums WHERE name = ?", (key,))
                    changed += cur.rowcount
                    continue
                try:
                    value = int(raw)
                except ValueError:
                    value = -1
                if value < 0 or value > MIN_QTY_MAX:
                    errors.append(key if key != SIM_KEY else "SIM-карты")
                    continue
                cur = db.execute(
                    """
                    INSERT INTO stock_minimums (name, min_qty) VALUES (?, ?)
                    ON CONFLICT(name) DO UPDATE SET min_qty = excluded.min_qty
                    WHERE stock_minimums.min_qty <> excluded.min_qty
                    """,
                    (key, value),
                )
                changed += cur.rowcount
            db.commit()
            if changed:
                log_operation("edit", "stock", 0, f"Изменены минимальные остатки: {changed}")
            if errors:
                flash("Не сохранено (нужно целое число от 0): " + ", ".join(errors), "error")
            else:
                flash("Минимальные остатки сохранены." if changed else "Изменений нет.", "success")
            return redirect(url_for("stock_minimums", only=request.args.get("only") or None))

        rows = stock_rows()
        short = [r for r in rows if r["deficit"]]
        only_short = request.args.get("only") == "short"
        return render_template(
            "stock_minimums.html",
            rows=short if only_short else rows,
            total_rows=len(rows),
            short_count=len(short),
            only_short=only_short,
            can_edit=can_edit,
        )
