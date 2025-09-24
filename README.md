# Unified news feed for normacs

- Автосборка GitHub Actions ежечасно и по кнопке (Actions → *Build unified feed* → Run).
- Результат публикуется как GitHub Pages: `docs/unified.json` по адресу `/unified.json`.

## Быстрый старт
1. Включите **Actions** и **Pages** (Source: `Deploy from a branch`, Branch: `main` / `docs`).
2. Дайте Actions права на запись (Repository → Settings → Actions → General → Workflow permissions → *Read and write*).
3. При необходимости запустите вручную: Actions → *Build unified feed* → Run workflow.
   Для локальной проверки без записи итоговых файлов выполните `python scripts/aggregate.py --dry-run`.

## Файлы
- `.github/workflows/build.yml` — пайплайн.
- `scripts/aggregate.py` — сборщик.
- `sources.json` — конфиг источников.
- `docs/index.html` — заглушка-страница с ссылкой на JSON.
- `docs/unified.json` — результат.

## Обновление
Меняйте `sources.json` (правила ссылок) или `scripts/aggregate.py` (логика парсинга), коммитьте — сборка запустится автоматически.

## Заметки по сложным источникам
- **Главгосэкспертиза.** Сайт возвращает 403 без белого списка. Варианты: запросить разрешённый IP, перейти на подписные рассылки/архив пресс-релизов, либо получить технический доступ к внутренним API.

## Новые стратегии запросов

Некоторые источники требуют особой сетевой логики (переходы через антиботы, разные таймауты, списки прокси и пр.). Для них в `sources.json` можно задать поле `request_strategy` со следующими параметрами:

- `connect_timeout`, `read_timeout` — раздельные таймауты в секундах.
- `max_attempts`, `backoff_factor` — количество попыток и экспоненциальная задержка между ними.
- `proxies` — список строк вида `http://user:pass@host:port` или словарей с ключами `http`/`https`; при сетевых ошибках клиент перебирает их по кругу.
- `selenium_fallback` — включает запасной прогрев headless-браузером (нужен установленный webdriver).
- `warmup` — объект с полями `url`, `delay` (число или `[min,max]`) и `timeout`; выполняет «прогревочный» запрос для получения куки/сессии. Если сервер вернул антибот-куки (например, `__ddg*`, `cf_clearance`), прогрев считается успешным даже при ответах `401/403`, куки сохраняются в `.cache/state.json`, а в статистике источника фиксируется тип решения (HTTP, Selenium, повторное использование кеша).
- `retry_statuses` — HTTP-коды, требующие сброса сессии и повторной попытки (например, 403).
- `extra_headers` — дополнительные заголовки, добавляемые ко всем запросам источника.

Состояние по каждому хосту (куки, метрики, статистика отказов) сохраняется в `.cache/state.json` и повторно используется в последующих прогонах.

### Зависящие пакеты

Для описанных стратегий требуется `selenium`. Убедитесь, что webdriver (Chromium/Chrome) доступен, если используется selenium-фолбэк. По умолчанию клиент пытается найти бинарь в `CHROME_BINARY` или через `chromium-browser` / `chromium` / `google-chrome` в `$PATH`. В CI-пайплайне устанавливается `chromium-browser` + `chromium-chromedriver`.
