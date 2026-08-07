# Unified news feed for normacs

Проект агрегирует новости из набора источников и публикует единый JSON Feed в `docs/unified.json`.

---

## Что делает скрипт

Сборщик (`scripts/aggregate.py`):

1. Загружает список источников из `sources.json`.
2. Для каждого источника собирает список ссылок (из HTML/XML/API).
3. Загружает страницы новостей, извлекает текст, дату публикации и канонический URL.
4. Фильтрует короткие/пустые материалы.
5. Объединяет с уже существующей лентой (`docs/unified.json`), не теряя историю.
6. Сохраняет результат в формате JSON Feed 1.1.

---

## Структура репозитория

- `scripts/aggregate.py` — основной pipeline агрегации.
- `scripts/http_client.py` — stateful HTTP-клиент с per-host стратегиями, куками, warm-up и Selenium-фолбэком.
- `sources.json` — конфигурация источников.
- `docs/unified.json` — итоговая лента.
- `tests/` — unit-тесты.
- `.cache/state.json` — состояние между запусками (куки, метрики, seen URLs, cooldown, служебные индексы).
- `.cache/pages/` — кеш скачанных страниц.

---

## Запуск

```bash
python scripts/aggregate.py
```

Полезные флаги:

- `--rebuild` — форс-пересборка (игнорируется оптимизация по неизменившемуся index и seen-URL фильтр).
- `--dry-run` — не записывать `docs/unified.json` и state.
- `--smoke` — быстрый прогон (ограниченный набор источников).
- `--sources "Имя 1,Имя 2"` — запуск только выбранных источников.
- `--limit-per-source N` — ограничение deep-fetch на источник (актуально для smoke).
- `--connect-timeout`, `--read-timeout` — глобальные таймауты.
- `--max-runtime` — мягкий лимит общего времени выполнения.
- `--debug` — подробные debug-логи.

Пример targeted rebuild:

```bash
MODE="rebuild" python scripts/aggregate.py --rebuild --sources "Российская газета: Экономика"
```

---

## Как работает сетевой слой (`request_strategy`)

Для каждого источника можно задать `request_strategy` в `sources.json`:

- `connect_timeout`, `read_timeout` — таймауты подключения/чтения.
- `max_attempts`, `backoff_factor` — число попыток и экспоненциальный backoff.
- `retry_statuses` — HTTP-коды, при которых выполняется повтор.
- `extra_headers` — дополнительные заголовки.
- `proxies` — пул прокси (`string` или `{http,https}`), переключение по попыткам.
- `warmup` — прогрев (URL/timeout/delay), обычно для антибота/куки.
- `selenium_fallback` и related (`selenium_wait`, `selenium_wait_for`, `selenium_scroll_steps`) — браузерный фолбэк.
- `capture_cookies` — сохранять куки в state.
- `record_path_on_success` — запоминать последний успешный path.

### Важная текущая логика (после последних изменений)

- При явных сетевых симптомах недоступности (`network unreachable` / `connect timeout`) хост может переводиться во временный cooldown (`NEWSFEED_NETWORK_COOLDOWN_SECONDS`, по умолчанию `900` сек), чтобы не тратить десятки минут на повторные одинаковые таймауты.
- Если у хоста задан **пул прокси**, cooldown не активируется преждевременно: сначала используются оставшиеся retry-итерации/прокси.
- Успешные попытки логируются на уровне `DEBUG`, а ошибки остаются в `WARNING`.

---

## Fallback-поведение для index-страниц

Для источника можно задать:

- `start_url` — основной index.
- `fallback_start_urls` — список альтернативных index URL.

Сборщик проходит кандидаты по очереди:

1. Пробует основной URL.
2. Если запрос упал — переходит к следующему.
3. Если запрос «успешный», но index фактически пустой (0 raw candidates), пробует fallback.
4. Если есть кеш index-страницы и он богаче текущего ответа, берётся кешированная версия.

Это особенно полезно для источников, где основной URL часто отдаёт защитную/пустую страницу.

---

## Кеш, состояние и merge

### `.cache/state.json`

Хранит:

- `host_state` (куки, ошибки, cooldown, warmup flags),
- `stats.metrics` (тайминги/статусы попыток),
- `seen_urls`, `index_hash`,
- `first_seen`, `canonical_item_ids`, `content_hashes` и др.

### `.cache/pages/`

- кеш HTML-страниц index и карточек;
- при сетевой недоступности используется как fallback.

### Merge логика

- новые карточки объединяются с существующим `docs/unified.json`;
- для `content_text` предпочитается более полный текст;
- для времени (`published_at`, `fetched_at`) выбирается более релевантное/новое значение;
- итог ограничивается `FEED_MAX_ITEMS` (по умолчанию 800).

---

## Настройки качества контента

В `sources.json` доступны:

- `min_words` — минимальный порог слов для карточки,
- `content_selectors` — селекторы основного текста,
- `include_patterns`/`include_regex`/`exclude_regex` — фильтрация ссылок,
- `restrict_domain`, `max_links`, `link_min_text_len`, `accept_empty_anchor`.

Есть также поддержка:

- API-источников (`mode: api`, `api_endpoint`),
- fallback API -> HTML (`html_fallback_on_empty_api`),
- AMP/mobile fallback при извлечении текста.

---

## Логи и диагностика

На INFO-уровне обычно видно:

- старт источника,
- выбор index candidate,
- число найденных ссылок (`raw`/`accepted`),
- итог по источнику (`-> N items`),
- сводку `SOURCE_SUMMARY`.

После сборки `docs/source-health.json` сохраняет результат именно текущего
обхода отдельно от ограниченной итоговой ленты. Проверка `scripts/metrics.py
--source-health docs/source-health.json` сообщает о сетевых сбоях, кешированных
fallback и неожиданно пустых index-страницах. Отсутствие источника среди 800
сохранённых карточек само по себе не считается ошибкой: активный источник может
быть вытеснен более частым источником. Для диагностического строгого режима
доступны `--strict-source-health` и `--fail-on-empty-source`.

Строка каждого источника разделяет `retained_item_count` (число карточек в
ограниченной ленте) и, при передаче `--source-health`, показатели текущего
обхода `raw_link_candidates` и `accepted_articles`. Источник без сохранённой
временной метки получает `freshness_status=no_data`, а не считается свежим.
Проверки можно включить точечно в объекте источника в `sources.json`:
`expected_min_candidates` задаёт минимум найденных при обходе кандидатов,
`expected_update_hours` — максимальный возраст последней сохранённой карточки,
а `allow_empty: false` включает проверку отсутствия карточек для
`--fail-on-empty-source`. Первые две проверки становятся ошибками в режиме
`--strict-source-health`; без этих настроек ограниченная лента остаётся
допустимо пустой для любого отдельного источника.

Нормальная сборка использует следующие ограничители ресурсов и качества,
которые можно переопределить переменными окружения:

- `FEED_MIN_ITEMS_PER_SOURCE=5` резервирует небольшую долю ленты для каждого
  представленного источника до заполнения оставшихся мест по общей свежести;
- `NEWSFEED_SELENIUM_BUDGET_SECONDS=90` ограничивает суммарное время браузерных
  fallback в одном запуске;
- `NEWSFEED_CACHE_MAX_BYTES=536870912` и `NEWSFEED_CACHE_MAX_AGE_DAYS=14`
  ограничивают HTML-кеш объёмом и возрастом.

Отчёт также содержит `consecutive_failures` и `future_date_rejections`.
Строгая проверка становится ошибкой только после трёх последовательных
неудачных обходов, поэтому единичный сетевой сбой остаётся предупреждением.

На DEBUG-уровне дополнительно:

- детальные fetch-attempt метрики,
- path извлечения контента (`_content_source`),
- технические детали warm-up/Selenium.

---

## Зависимости

Установить Python-зависимости:

```bash
pip install -r requirements.txt
```

Для Selenium-фолбэков нужен браузер + webdriver (Chromium/Chrome + chromedriver).

---

## CI / публикация

- Автосборка выполняется в GitHub Actions (`.github/workflows/build.yml`).
- Публикуемый файл: `docs/unified.json` (GitHub Pages endpoint `/unified.json`).
