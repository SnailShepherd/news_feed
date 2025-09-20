import dateparser
MOSCOW = "Europe/Moscow"
def parse_date_ru(text: str):
    if not text:
        return None
    cleaned = text.strip().replace("\u2013", "-")
    if "-" in cleaned:
        cleaned = cleaned.split("-")[0].strip()
    settings = {
        "TIMEZONE": MOSCOW,
        "RETURN_AS_TIMEZONE_AWARE": True,
        "PREFER_DAY_OF_MONTH": "first",
        "PREFER_DATES_FROM": "past",
    }
    return dateparser.parse(cleaned, languages=["ru"], settings=settings)

def to_iso8601(dtobj):
    if not dtobj:
        return None
    return dtobj.isoformat()
