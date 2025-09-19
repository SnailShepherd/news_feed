# News Feed (Starter)

Единая лента новостей из 18 источников. Сбор — каждый час на GitHub Actions, публикация — через GitHub Pages из папки `/docs` ветки `main`.

## Состав репозитория
- `sources.json` — список источников (URL листингов) и правила фильтрации ссылок.
- `scripts/aggregate.py` — парсер и сборщик ленты.
- `requirements.txt` — зависимости Python.
- `docs/index.html` — страница, рядом будет `docs/unified.json`.
- `.github/workflows/build.yml` — крон-воркфлоу, который строит ленту и коммитит результат.

---

## Как это работает

1. `scripts/aggregate.py` читает `sources.json`.
2. Для каждого листинга:
   - качает HTML (условный GET, кэш заголовков в `.cache/state.json`),
   - парсит ссылки (`<a href=...>`) и **фильтрует** по `include_patterns`,
   - у заголовка ищет дату рядом (в тексте родителя/соседей) — поддержаны форматы `dd.mm.yyyy [hh:mm]` и `18 сентября 2025`,
   - нормализует дату в MSK, собирает карточку.
3. Строит JSON Feed → `docs/unified.json`.

> **Примечание:** Файлы кэша (`.cache/`) сохраняются между запусками через `actions/cache`. Это уменьшает трафик и ускоряет сбор.

---

**Q: Некоторые страницы рендерятся скриптами (JS) и парсер не видит новости.**  
A: Для 1–2 таких источников добавьте точечные `include_patterns` или подключите промежуточный RSS-Bridge (можно бесплатный хостинг). В большинстве случаев достаточно уточнить `include_patterns` в `sources.json`.

**Q: Как добавить/удалить источник?**  
A: Откройте `sources.json` и добавьте/удалите блок с полями:
```json
{
  "name": "Название",
  "base_url": "https://example.com",
  "start_url": "https://example.com/news",
  "include_patterns": ["/news"],
  "link_min_text_len": 20
}
```
Сохраните. Воркфлоу запустится сам (триггер по `push`).

---

## Локальный запуск (по желанию)
```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python scripts/aggregate.py
# результат будет в docs/unified.json
```

## Тонкая настройка
- Скорость: измените `SLEEP_BETWEEN_REQUESTS` в `aggregate.py` (по умолчанию 2 сек).
- Таймаут HTTP: `REQUEST_TIMEOUT` (по умолчанию 30 сек).
- Фильтры ссылок: `include_patterns` в `sources.json`.
- Минимальная длина текста ссылки: `link_min_text_len` (защита от "читать далее").

---

## Безопасность и бережность
- Пользуемся `If-Modified-Since/ETag`, кэшируем ответы, делаем паузы между обращениями.
- `User-Agent` явно указан как технический бот с контактной ссылкой.
