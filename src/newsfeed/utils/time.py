from datetime import datetime
import pytz

MSK = pytz.timezone("Europe/Moscow")

def now_msk():
    return datetime.now(tz=MSK)

def parse_window_start(s: str | None):
    # ожидаем формат "YYYY-MM-DD HH:MM MSK" или "DD.MM.YYYY HH:MM MSK"
    if not s:
        return None
    s = s.strip().replace("MSK","").strip()
    for fmt in ("%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M", "%Y-%m-%d", "%d.%m.%Y"):
        try:
            dt = datetime.strptime(s, fmt)
            return MSK.localize(dt)
        except Exception:
            pass
    return None
