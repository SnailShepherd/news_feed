# -*- coding: utf-8 -*-
from datetime import datetime, timedelta, timezone
import re

MSK = timezone(timedelta(hours=3))

RU_MONTHS = {
    "января":1, "январь":1,
    "февраля":2, "февраль":2,
    "марта":3, "март":3,
    "апреля":4, "апрель":4,
    "мая":5, "май":5,
    "июня":6, "июнь":6,
    "июля":7, "июль":7,
    "августа":8, "август":8,
    "сентября":9, "сентябрь":9,
    "октября":10, "октябрь":10,
    "ноября":11, "ноябрь":11,
    "декабря":12, "декабрь":12,
}

def _clamp_year(y: int) -> int:
    return y if (1900 <= y <= 2100) else datetime.now(tz=MSK).year

def parse_ru_date(text: str):
    """
    Robust parser for Russian date strings.
    Supports patterns like:
      - 19.09.2025 14:32
      - 19.09.2025
      - 19 сентября 2025, 14:32
      - 19 сентября 2025 года
      - сегодня 14:32 / вчера 09:12
    Returns timezone-aware datetime in MSK.
    """
    if not text:
        return None
    t = text.strip().lower()
    now = datetime.now(tz=MSK)

    # relative
    if t.startswith("сегодня"):
        m = re.search(r"(\d{1,2}):(\d{2})", t)
        hh, mm = (int(m.group(1)), int(m.group(2))) if m else (12, 0)
        return now.replace(hour=hh, minute=mm, second=0, microsecond=0)

    if t.startswith("вчера"):
        m = re.search(r"(\d{1,2}):(\d{2})", t)
        hh, mm = (int(m.group(1)), int(m.group(2))) if m else (12, 0)
        yday = (now - timedelta(days=1)).date()
        return datetime(yday.year, yday.month, yday.day, hh, mm, tzinfo=MSK)

    # 19.09.2025 14:32  or  19.09.2025
    m = re.search(r"(\d{1,2})[./](\d{1,2})[./](\d{2,4})(?:[ T](\d{1,2}):(\d{2}))?", t)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        y = 2000 + y if y < 100 else y
        hh, mm = (int(m.group(4)), int(m.group(5))) if m.group(4) else (12, 0)
        return datetime(_clamp_year(y), mo, d, hh, mm, tzinfo=MSK)

    # 19 сентября 2025, 14:32  /  19 сентября 2025 года
    m = re.search(r"(\d{1,2})\s+([а-яё]+)\s+(\d{4})(?:\s*(?:г\.|года)?)?(?:\s*,?\s*(\d{1,2}):(\d{2}))?", t, re.I)
    if m:
        d, mon_str, y = int(m.group(1)), m.group(2), int(m.group(3))
        mo = RU_MONTHS.get(mon_str, None)
        if mo is None:
            return None
        hh, mm = (int(m.group(4)), int(m.group(5))) if m.group(4) else (12, 0)
        return datetime(_clamp_year(y), mo, d, hh, mm, tzinfo=MSK)

    # ISO or RFC-like (fallback)
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=MSK)
        return dt.astimezone(MSK)
    except Exception:
        pass

    return None
