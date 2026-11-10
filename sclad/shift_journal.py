"""Журнал смены: правила смен, выборка записей, выгрузка в Word и письмо для Outlook.

Перенесено из отдельной программы «Журнал инженеров по АСУ» (PyQt5 + PostgreSQL).
Правила те же:
  * 1-я смена — с 08:30 до 20:30, 2-я — с 20:30 до 08:30 следующего дня;
    записи после полуночи относятся ко 2-й смене предыдущего дня;
  * менять записи и состав смены можно только в текущей смене, прошлые — только просмотр;
  * будущие даты недоступны.
"""
import io
import re
import zipfile
from datetime import date, datetime, time, timedelta
from email.message import EmailMessage
from email.policy import SMTP
from html import escape
from xml.sax.saxutils import escape as xml_escape

DAY_START = time(8, 30)
NIGHT_START = time(20, 30)
SHIFTS = ("day", "night")
SHIFT_LABELS = {"day": "1-я смена", "night": "2-я смена"}

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

# Порядок записей внутри смены: у 2-й смены записи после полуночи (до 08:30) идут в конце.
def entry_order_sql(alias=""):
    p = f"{alias}." if alias else ""
    return f"CASE WHEN {p}shift = 'night' AND {p}entry_time < '08:30' THEN 1 ELSE 0 END, {p}entry_time, {p}id"


def now():
    """Текущее время сервера. Вынесено отдельно, чтобы тесты могли его подменить."""
    return datetime.now()


def current_shift(moment=None):
    """(смена, дата начала смены ISO) для указанного момента."""
    moment = moment or now()
    t = moment.time()
    if DAY_START <= t < NIGHT_START:
        return "day", moment.date().isoformat()
    if t >= NIGHT_START:
        return "night", moment.date().isoformat()
    return "night", (moment.date() - timedelta(days=1)).isoformat()


def is_current(shift, shift_date, moment=None):
    return (shift, shift_date) == current_shift(moment)


def normalize_time(raw):
    """'8:05' -> '08:05'; None, если формат неверный."""
    m = _TIME_RE.match((raw or "").strip())
    if not m:
        return None
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def parse_shift(raw):
    raw = (raw or "").strip().lower()
    if raw in SHIFTS:
        return raw
    # Названия из старой программы
    if raw.startswith("1"):
        return "day"
    if raw.startswith("2"):
        return "night"
    return None


def parse_date(raw):
    try:
        return date.fromisoformat((raw or "").strip()[:10]).isoformat()
    except ValueError:
        return None


def load_shift(db, shift, shift_date):
    engineers = [
        dict(r)
        for r in db.execute(
            "SELECT id, name, user_id FROM journal_shift_engineers WHERE shift_date = ? AND shift = ? ORDER BY name",
            (shift_date, shift),
        ).fetchall()
    ]
    entries = [
        dict(r)
        for r in db.execute(
            f"""
            SELECT e.*, u.surname, u.first_name, u.username
            FROM journal_entries e
            LEFT JOIN users u ON u.id = e.created_by_user_id
            WHERE e.shift_date = ? AND e.shift = ?
            ORDER BY {entry_order_sql('e')}
            """,
            (shift_date, shift),
        ).fetchall()
    ]
    return engineers, entries


def format_date_ru(iso):
    return date.fromisoformat(iso).strftime("%d.%m.%Y")


def title_for(shift, shift_date):
    return f"{SHIFT_LABELS[shift]}, {format_date_ru(shift_date)}"


# ---------- Письмо (та же HTML-таблица, что вставляла старая программа в Outlook) ----------

def mail_html(shift, shift_date, engineers, entries):
    engineers_html = "".join(f"<li>{escape(e['name'])}</li>" for e in engineers) or "<li>—</li>"
    rows_html = "".join(
        "<tr><td>{}</td><td>{}</td><td>{}</td></tr>".format(
            escape(e["entry_time"]),
            escape(e["content"] or "").replace("\n", "<br>"),
            escape(e["note"] or "").replace("\n", "<br>"),
        )
        for e in entries
    )
    return f"""<html>
<head><meta charset="utf-8">
<style>
body {{ font-family: Arial, sans-serif; }}
h2 {{ color: #0056b3; border-bottom: 2px solid #0056b3; padding-bottom: 5px; }}
table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
th, td {{ padding: 8px; border: 1px solid #ddd; vertical-align: top; }}
th {{ background-color: #0056b3; color: white; }}
</style>
</head>
<body>
<h2>Смена: {SHIFT_LABELS[shift]}, Дата: {date.fromisoformat(shift_date).strftime("%d-%m-%Y")}</h2>
<h3>Инженеры на смене:</h3>
<ul>{engineers_html}</ul>
<h3>Записи журнала:</h3>
<table>
<tr><th>Время</th><th>Содержание</th><th>Примечание</th></tr>
{rows_html}
</table>
</body>
</html>"""


def mail_subject(shift, shift_date):
    return f"Сводка - {SHIFT_LABELS[shift]} от {date.fromisoformat(shift_date).strftime('%d-%m-%Y')}"


def build_eml(shift, shift_date, engineers, entries, to="", cc=""):
    """Неотправленное письмо (.eml). Outlook открывает его как новое письмо: остаётся нажать «Отправить»."""
    msg = EmailMessage(policy=SMTP)
    msg["Subject"] = mail_subject(shift, shift_date)
    if to:
        msg["To"] = to
    if cc:
        msg["Cc"] = cc
    # X-Unsent: Outlook открывает файл как черновик с кнопкой «Отправить», а не как полученное письмо
    msg["X-Unsent"] = "1"
    msg.set_content("Журнал смены во вложенной HTML-части письма.")
    msg.add_alternative(mail_html(shift, shift_date, engineers, entries), subtype="html")
    return msg.as_bytes()


# ---------- Word (.docx) без сторонних библиотек ----------

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_BLUE = "0055B3"


def _run(text, bold=False, size=None, color=None):
    props = ""
    if bold:
        props += "<w:b/>"
    if color:
        props += f'<w:color w:val="{color}"/>'
    if size:
        props += f'<w:sz w:val="{size}"/><w:szCs w:val="{size}"/>'
    rpr = f"<w:rPr>{props}</w:rPr>" if props else ""
    parts = (text or "").split("\n")
    body = "<w:br/>".join(f'<w:t xml:space="preserve">{xml_escape(p)}</w:t>' for p in parts)
    return f"<w:r>{rpr}{body}</w:r>"


def _para(text, bold=False, size=None, color=None, spacing_after=None):
    ppr = f'<w:pPr><w:spacing w:after="{spacing_after}"/></w:pPr>' if spacing_after is not None else ""
    return f"<w:p>{ppr}{_run(text, bold, size, color)}</w:p>"


def _cell(text, width, bold=False, color=None):
    return (
        f'<w:tc><w:tcPr><w:tcW w:w="{width}" w:type="dxa"/></w:tcPr>'
        f"<w:p>{_run(text, bold=bold, size=22 if bold else None, color=color)}</w:p></w:tc>"
    )


def build_docx(shift, shift_date, engineers, entries):
    widths = (1100, 5200, 3300)
    border = '<w:{0} w:val="single" w:sz="4" w:space="0" w:color="000000"/>'
    borders = "".join(border.format(b) for b in ("top", "left", "bottom", "right", "insideH", "insideV"))
    rows = [
        "<w:tr>" + "".join(_cell(h, w, bold=True, color=_BLUE) for h, w in zip(("Время", "Содержание", "Примечание"), widths)) + "</w:tr>"
    ]
    for e in entries:
        rows.append(
            "<w:tr>"
            + _cell(e["entry_time"], widths[0])
            + _cell(e["content"] or "", widths[1])
            + _cell(e["note"] or "", widths[2])
            + "</w:tr>"
        )
    table = (
        f'<w:tbl><w:tblPr><w:tblW w:w="{sum(widths)}" w:type="dxa"/><w:tblBorders>{borders}</w:tblBorders></w:tblPr>'
        "<w:tblGrid>" + "".join(f'<w:gridCol w:w="{w}"/>' for w in widths) + "</w:tblGrid>"
        + "".join(rows) + "</w:tbl>"
    )
    body = [
        _para(f"Журнал смены: {SHIFT_LABELS[shift]}, Дата: {format_date_ru(shift_date)}", bold=True, size=32, color=_BLUE, spacing_after=200),
        _para("Инженеры на смене:", bold=True, size=26, color=_BLUE),
    ]
    body += [_para(f"• {e['name']}") for e in engineers] or [_para("—")]
    body.append(_para("Записи журнала:", bold=True, size=26, color=_BLUE))
    body.append(table)
    body.append("<w:p/>")
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{_W_NS}"><w:body>{"".join(body)}'
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="1134" w:right="850" w:bottom="1134" w:left="1134" w:header="708" w:footer="708" w:gutter="0"/>'
        "</w:sectPr></w:body></w:document>"
    )
    styles = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:styles xmlns:w="{_W_NS}"><w:docDefaults><w:rPrDefault><w:rPr>'
        '<w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:cs="Calibri" w:eastAsia="Calibri"/>'
        '<w:sz w:val="22"/><w:szCs w:val="22"/><w:lang w:val="ru-RU"/></w:rPr></w:rPrDefault></w:docDefaults></w:styles>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
        "</Types>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        "</Relationships>"
    )
    doc_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        "</Relationships>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", document)
        z.writestr("word/styles.xml", styles)
        z.writestr("word/_rels/document.xml.rels", doc_rels)
    return buf.getvalue()
