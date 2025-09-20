# news_feed

Сборщик новостных карточек для normacs.info / normacs.ru

## Ключевые принципы
- Временная зона: MSK (UTC+3). Везде используем локаль `ru_RU`.
- Политика запроса: бережная. Пер-доменная задержка, экспоненциальный backoff, 429 — не брутим, а отступаем.
- Окно парсинга: `[WINDOW_START, now]` (MSK).
- Минимум переходов по внутренним ссылкам: **никакого обхода рубрик/тегов** без явной настройки.
- Источники конфигурируются в `data/sources.yaml`.

## Быстрый старт (локально)

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

# Сбор фида:
export PYTHONPATH=src
python -m newsfeed build --window-start "2025-09-18 00:00 MSK" --out docs/unified.json

# Быстрый просмотр (локально откройте docs/index.html)
```

## GitHub Actions
В репозитории уже настроен workflow `.github/workflows/build.yml` — он собирает фид по расписанию и на каждый push.
Результат публикуется в `docs/unified.json` и `docs/index.html` (GitHub Pages).

## Ручные источники
Для источников, которые не отдаются из GitHub Actions (чебурнет/фаерволл/403/401), в `data/sources.yaml` есть `mode: manual`.
Скрипт создаёт файл `docs/manual_queue.json` с перечнем таких источников и советами по ручной проверке.

## Классификаторы
Файлы `data/classifiers.csv` и `data/classifiers_map.csv` — в формате CSV с кавычками вокруг значений. Запятые **разрешены** внутри значений.
