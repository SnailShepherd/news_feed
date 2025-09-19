# news_feed (updated scrapers)

Ключевые изменения:
- Исправлен Минстрой (`?d=news` вместо `?d=all`).
- Главгосэкспертиза — официальный RSS `https://gge.ru/rss/` (обходит 403).
- АНЦБ — переведено на HTTPS.
- Металлоснабжение и сбыт — только RSS + фильтр статей.
- Кэширование результатов источников (`cache/items/*.json`): при 304/сбоях данные не пропадают.
- Улучшенный парсер дат на русском + извлечение из `<meta>`/`<time>` на карточках.

Локальный запуск:
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python src/build.py
```
Результат: `docs/unified.json`
