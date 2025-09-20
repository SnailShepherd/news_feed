# Unified news feed for normacs

- Автосборка GitHub Actions ежечасно и по кнопке (Actions → *Build unified feed* → Run).
- Результат публикуется как GitHub Pages: `docs/unified.json` по адресу `/unified.json`.

## Быстрый старт
1. Включите **Actions** и **Pages** (Source: `Deploy from a branch`, Branch: `main` / `docs`).
2. Дайте Actions права на запись (Repository → Settings → Actions → General → Workflow permissions → *Read and write*).
3. При необходимости запустите вручную: Actions → *Build unified feed* → Run workflow.

## Файлы
- `.github/workflows/build.yml` — пайплайн.
- `scripts/aggregate.py` — сборщик.
- `sources.json` — конфиг источников.
- `docs/index.html` — заглушка-страница с ссылкой на JSON.
- `docs/unified.json` — результат.

## Обновление
Меняйте `sources.json` (правила ссылок) или `scripts/aggregate.py` (логика парсинга), коммитьте — сборка запустится автоматически.
