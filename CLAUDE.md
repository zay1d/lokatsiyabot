# Lokatsiyalar — production reference

Telegram Mini App: каталог локаций в Саудовской Аравии (Медина/Джидда) для
узбекоязычных паломников. Каждая локация — кнопка, открывающая пост в
Telegram-канале `@lokatsiyamed`. Интерфейс на узбекском (латиница).

## Quick facts

| | |
|---|---|
| Фронт (Mini App) | https://zay1d.github.io/lokatsiyabot/ (GitHub Pages, без build-шага) |
| Репо | https://github.com/zay1d/lokatsiyabot |
| Рабочая ветка | `claude/review-tg-mini-app-JR4ll` → PR в `main` |
| Бэкенд | Contabo VPS, `ssh root@167.86.125.229` |
| App dir на VPS | `/opt/lokatsiya-bot/` |
| Backend API | `https://167-86-125-229.sslip.io` (nginx HTTPS → `127.0.0.1:8080`) |
| systemd unit | `lokatsiya-bot` |
| Автор | @zayd_usamah |

## Архитектура

- **Фронт** — один файл `index.html` (vanilla HTML/CSS/JS, ~1600 строк),
  отдаётся GitHub Pages. Иконки: lucide. Шрифт: Quicksand.
- **Бэк** — `bot/main.py`, единый процесс: aiohttp (HTTP API) + aiogram
  (Telegram-бот) в одной `asyncio.run`. Хранение — плоские JSON-файлы рядом
  с `main.py`, без БД.
- Каталог **живёт на сервере** (`bot/catalog.json`), редактируется
  командами бота, сидится один раз из репового `catalog.json`. Фронт
  фетчит `GET /api/catalog`, при недоступности — фолбэк на статичный
  `catalog.json` с Pages.

## Деплой

**Фронт** (`index.html`, `catalog.json`): push в `main` → GitHub Pages
сам выкатывает за ~1 мин. Рестарт не нужен.

**Бэк** (`bot/*.py`):
```
ssh root@167.86.125.229
cd /opt/lokatsiya-bot && git pull
sudo systemctl restart lokatsiya-bot
journalctl -u lokatsiya-bot -n 30 --no-pager
```
Изменения только в `catalog.json` через бот-команды — рестарт НЕ нужен.

## HTTP endpoints (все на бэке)

| Endpoint | Назначение |
|---|---|
| `GET /api/catalog` | актуальный каталог |
| `POST /api/contact` | сообщение от юзера → админ-чат (initData HMAC) |
| `POST /api/favorites/list` `/toggle` | серверное избранное (initData HMAC) |
| `POST /api/track` | статистика open/category/location (initData HMAC) |
| `GET /api/prayer-info` | следующий намаз Масджид ан-Набави (имам/муаззин/фото/время). Источник: публичный JSON API `haramainflagsapi.prh.gov.sa`, кеш 30 мин, без скрапинга. Публичный (без initData) |
| `GET /health` | `{"ok":true}` |

## Админ-команды бота (только для `ADMIN_USER_IDS`)

`/add` `/del` `/cat_add` `/cat_icon` `/loc_icon` `/list` `/stats`
`/cancel` `/help`. Не-админам бот молча не отвечает. Иконки — имена с
lucide.dev (есть пикер + ручной ввод).

## Env (`/opt/lokatsiya-bot/bot/.env`, chmod 600, в `.gitignore`)

```
BOT_TOKEN=<от @BotFather>
ADMIN_CHAT_ID=6309709092          # куда падают обращения юзеров
ADMIN_USER_IDS=6309709092,5798132845   # кому разрешены команды
ALLOWED_ORIGIN=https://zay1d.github.io
PORT=8080
BIND_HOST=127.0.0.1               # НИКОГДА не 0.0.0.0 (торчит мимо TLS)
```

Runtime-файлы (в `.gitignore`, не коммитить): `.env`, `state.json`,
`favorites.json`, `tracks.json`, `catalog.json` (живой).

## Дизайн ("Friendly Card", из Stitch)

Фон `#0c141b`, акцент мятный `#3ddc97` / яркий `#61f9b1`, текст `#dbe3ed`,
шрифт Quicksand, карточки 22px, мятное свечение вместо теней. Главная —
сетка категорий 3 колонки + нижний таб-бар (Asosiy / Sevimli / Aloqa).
Drill-down в категорию скрывает таб-бар. Есть скрытый `hero-promo` блок
(`showHeroPromo()` / `hideHeroPromo()`) под будущие промо/новости.

## Безопасность

- `BOT_TOKEN` только в `.env`, не в коде. Если засветился — `/revoke` в
  @BotFather.
- HMAC-валидация `initData` на всех приватных endpoints.
- `BIND_HOST=127.0.0.1` — API наружу только через nginx/TLS. ufw: allow
  22/80/443, deny 8080.
- Публичный репо — это нормально (security by design, не obscurity).
  Секреты в `.env`, личные данные юзеров в gitignored JSON.
- Админы (`ADMIN_USER_IDS`) исключены из статистики.

## Рабочий процесс

- Работаем на ветке `claude/review-tg-mini-app-JR4ll`, мёржим в `main`
  через PR (`gh pr create` / `gh pr merge --merge`).
- Перед коммитом — `git status`, убедиться что `.env`/runtime-JSON не
  попадают в коммит.
- Git identity на этом Mac: `zay1d` / `ziyodullaqudratullayev@gmail.com`.
- На этом Mac нет node; есть python3 (anaconda) и Google Chrome (для
  headless-скриншотов: `--headless --screenshot`).
- Mini App URL в @BotFather (Menu Button) → `https://zay1d.github.io/lokatsiyabot/`.
