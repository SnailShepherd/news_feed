# Unified News Feed (reboot)

Это полностью готовый «с нуля» репозиторий для сборки `docs/unified.json` из ваших источников.

## Как использовать (быстрый старт)

1) Создайте новый репозиторий на GitHub (пустой).
2) Загрузите **все файлы** из этого архива (кроме `.github`, см. ниже) в корень репозитория.
3) Включите GitHub Pages: **Settings → Pages → Source: Deploy from a branch → Branch: `main` / `docs`** (или та ветка, куда загрузили).
4) Разрешите Actions запись: **Settings → Actions → General → Workflow permissions → Read and write permissions**.
5) Создайте вручную workflow (веб-интерфейс GitHub не принимает папки, начинающиеся с точки при Upload Files):
   - Нажмите **Add file → Create new file**.
   - Введите имя файла: **.github/workflows/build.yml** (с точкой в начале).
   - Вставьте содержимое из файла `__WORKFLOW_build.yml` из этого архива.
   - Сохраните (Commit).
6) Запустите workflow: **Actions → Build unified feed → Run workflow**.
7) Готовый фид будет лежать по адресу: `https://<ваш_логин>.github.io/<repo>/unified.json`.
   Индексная страница: `https://<ваш_логин>.github.io/<repo>/`.

> Если хотите локальный запуск: Python 3.11+ → `pip install -r requirements.txt` → `python -m newsfeed.build`.

## Что внутри

- `src/newsfeed/build.py` — основной скрипт сборки.
- `src/newsfeed/fetch.py` — безопасное скачивание (ограничение частоты, ретраи, заголовки).
- `src/newsfeed/dateparse_ru.py` — улучшенный парсер дат (RU).
- `src/newsfeed/sources/*.py` — адаптеры по сайтам.
- `docs/` — сюда пишется `unified.json` и открывается GitHub Pages.
- `__WORKFLOW_build.yml` — содержимое для `.github/workflows/build.yml` (создаёте вручную).

## Что исправлено по сравнению с предыдущей версией

- Фильтрация НЕ-контентных страниц (категории/ленты вида `/news/section/`) для **stroygaz.ru**, **government.ru**, **metalinfo.ru** и др.
- Нормальные даты публикации для **metalinfo.ru**, **notim.ru**, **gostinfo.ru**, **minfin.gov.ru**, **eec.eaeunion.org**, **erzrf.ru** (искали по правильным селекторам).
- Гибкий rate-limit и корректная обработка **429 Too Many Requests** (экспоненциальная пауза + джиттер).
- Корректная обработка **RSS/XML** (без предупреждений XMLParsedAsHTMLWarning).
- Логирование причин, если сайт заблокирован/требует авторизацию/возвращает 403/401.
- Fallback «ручная очередь» (`manual/queue.csv`) — можно быстро добавить ссылки вручную (скрипт сам подтянет метаданные со страницы, если доступно).

## Блокируемые/проблемные источники

- **Минстрой России** (`minstroyrf.gov.ru/press/?d=all`) — часто отдаёт `401`/`403`. В адаптере включена «мягкая» стратегия (подмена User-Agent, реферер, паузы). Если не пройдёт, появится запись в логе и рекомендации.
- **Главгосэкспертиза** (`gge.ru/press-center/news/`) — периодические `403`. Аналогично.
- **РИА-СТК** — иногда сетевые проблемы из среды GitHub. Можно пользоваться ручной очередью или локальным запуском.
- **АНЦБ** — были таймауты; пробуем HTTPS и увеличенный таймаут.

Подробности и инструкции по каждому кейсу см. в конце `src/newsfeed/sources/_common.py` и в логах Actions.

Удачи! 🧡
