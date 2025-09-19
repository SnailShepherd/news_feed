from __future__ import annotations
import re
from datetime import datetime, timedelta
from dateutil import tz
from dateutil.parser import isoparse

MOSCOW = tz.gettz("Europe/Moscow")

MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
    "янв": 1, "фев": 2, "мар": 3, "апр": 4, "июн": 6, "июл": 7, "авг": 8, "сен": 9, "сент": 9, "окт": 10, "ноя": 11, "дек": 12,
}

def to_msk(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=MOSCOW)
    return dt.astimezone(MOSCOW)

def parse_ru_date(s: str, now: datetime | None = None) -> datetime | None:
    """
    Понимает форматы:
    - 19.09.2025 14:12
    - 19.09.2025
    - 19 сентября 2025, 14:12
    - 19 сентября 2025
    - 2025-09-19T14:12:00+03:00 (ISO)
    - сегодня, HH:MM / вчера, HH:MM
    Возвращает datetime в MSK.
    """
    if not s:
        return None
    s = re.sub(r"\s+", " ", s.strip().lower())

    # ISO
    try:
        if "t" in s or s.startswith("20"):
            dt = isoparse(s)
            return to_msk(dt)
    except Exception:
        pass

    # сегодня/вчера
    if now is None:
        now = datetime.now(MOSCOW)
    m = re.match(r"(сегодня|вчера)[, ]+(\d{1,2}):(\d{2})", s)
    if m:
        hh, mm = int(m.group(2)), int(m.group(3))
        base = now if m.group(1) == "сегодня" else (now - timedelta(days=1))
        return base.replace(hour=hh, minute=mm, second=0, microsecond=0)

    # 19.09.2025 [HH:MM]
    m = re.match(r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})(?:[ ,]+(\d{1,2}):(\d{2}))?$", s)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100: y += 2000
        hh = int(m.group(4)) if m.group(4) else 12
        mm = int(m.group(5)) if m.group(5) else 0
        return datetime(y, mo, d, hh, mm, tzinfo=MOSCOW)

    # 19 сентября 2025[, 14:12]
    m = re.match(r"(\d{1,2}) ([а-яё.]+) (\d{4})(?:[, ]+(\d{1,2}):(\d{2}))?", s)
    if m and m.group(2) in MONTHS:
        d = int(m.group(1)); mo = MONTHS[m.group(2)]; y = int(m.group(3))
        hh = int(m.group(4)) if m.group(4) else 12
        mm = int(m.group(5)) if m.group(5) else 0
        return datetime(y, mo, d, hh, mm, tzinfo=MOSCOW)

    # если не распознали — None
    return None
