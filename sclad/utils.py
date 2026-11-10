"""Небольшие вспомогательные функции: разбор ввода, даты, форматирование."""
import calendar
from datetime import date
from urllib.parse import urlparse


def fix_mojibake_text(text):
    """Исправляет кракозябры: текст в UTF-8 был прочитан как Latin-1 (cp1252)."""
    if not text or not isinstance(text, str):
        return text
    try:
        return text.encode("latin1").decode("utf8")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return text


def maintenance_next_due(performed_at_str, months=3):
    """Следующая дата ТО: performed_at + months (календарные месяцы)."""
    if not performed_at_str:
        return None
    d = date.fromisoformat(str(performed_at_str)[:10])
    month = d.month + months
    year = d.year
    while month > 12:
        month -= 12
        year += 1
    while month < 1:
        month += 12
        year -= 1
    maxday = calendar.monthrange(year, month)[1]
    day = min(d.day, maxday)
    return date(year, month, day).strftime("%Y-%m-%d")


SQLITE_INT_MAX = 2**63 - 1


def to_int(value, default=None):
    """Безопасно превращает строку из формы/URL в целое число (иначе default)."""
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    if abs(number) > SQLITE_INT_MAX:
        return default
    return number


def to_iso_date(value):
    """Строка 'YYYY-MM-DD' (допускается хвост со временем) -> 'YYYY-MM-DD' или None, если это не дата."""
    try:
        return date.fromisoformat(str(value or "").strip()[:10]).isoformat()
    except ValueError:
        return None


def _is_safe_next_url(url):
    """Разрешён только относительный путь внутри сайта: /machines?q=1.

    Отсекает //host, /\\host, https:host, javascript: и подобные варианты,
    которыми можно увести пользователя на чужой сайт после входа.
    """
    if not url or not url.startswith("/") or url.startswith("//") or "\\" in url:
        return False
    parsed = urlparse(url)
    return not parsed.scheme and not parsed.netloc


def row_to_dict(row):
    """Приводит sqlite3.Row к dict; для None возвращает None."""
    return dict(row) if row else None


def _format_date_ddmmyy(value):
    """Форматирует дату в дд.мм.гг. Принимает строку YYYY-MM-DD или datetime с датой."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return ""
    s = str(value).strip()[:10]
    if len(s) != 10 or s[4] != "-" or s[7] != "-":
        return value
    try:
        y, m, d = s[:4], s[5:7], s[8:10]
        return f"{d}.{m}.{y[2:]}"  # дд.мм.гг
    except (IndexError, TypeError):
        return value


def _format_datetime_ddmmyy_hhmm(value):
    """Форматирует дату и время: YYYY-MM-DD HH:MM:SS -> дд.мм.гг ЧЧ:ММ."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return ""
    s = str(value).strip()
    if len(s) < 10 or s[4] != "-" or s[7] != "-":
        return value
    try:
        d, m, y = s[8:10], s[5:7], s[2:4]  # дд, мм, гг (год из YYYY -> последние 2 цифры)
        time_part = s[11:16] if len(s) >= 16 and s[10] in " T" else ""
        return f"{d}.{m}.{y}" + (f" {time_part}" if time_part else "")
    except (IndexError, TypeError):
        return value


def _format_backup_date(value):
    """Форматирует дату/время резервной копии: YYYY-MM-DD HH:MM -> DD-MM-YYYY HH:MM."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return ""
    s = str(value).strip()
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        d, m, y = s[8:10], s[5:7], s[:4]
        time_part = s[11:16] if len(s) >= 16 and s[10] == " " else ""
        return f"{d}-{m}-{y}" + (f" {time_part}" if time_part else "")
    return value
