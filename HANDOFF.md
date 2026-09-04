# Handoff — Lokatsiyalar

Снимок состояния проекта. Для деталей архитектуры см. `CLAUDE.md`.
Последнее обновление: после переезда на новый VPS (2026-09-04).

## Текущее состояние

- **Фронт** — задеплоен, работает: https://zay1d.github.io/lokatsiyabot/
- **Бэк** — работает на VPS, НО см. «Требует действия» ниже.
- Дизайн «Friendly Card», сетка категорий 3 кол. + нижний таб-бар.
- Каталог: 13 категорий, источник — `bot/catalog.json` на сервере.

## Переезд на новый VPS (2026-09-04)

Старый сервер `167.86.125.229` был уничтожен. Бэкенд поднят заново на
`62.169.26.149` (тот же VPS, что и Saudia Service, соседний nginx-сайт):
`/opt/lokatsiya-bot`, юнит `lokatsiya-bot` теперь под пользователем
`lokatsiya` с sandbox-hardening, nginx + Let's Encrypt на
`62-169-26-149.sslip.io`. Фронт на Pages не пострадал, только `API_URL`
в `index.html` перебит на новый хост. Prayer-карточка, которая раньше
ждала деплоя, теперь задеплоена (свежий клон `main`).

**Потеряно вместе со старым сервером (не восстановить):** живой
`catalog.json` с локациями, добавленными через `/add` сверх сида из репо,
`favorites.json`, `tracks.json` (статистика с нуля), `state.json`
(связка ответов админа с юзерами). Каталог засеян из репозиторного
`catalog.json`, 13 категорий.

**Осталось владельцу:**
1. Перевыпустить токен бота: он был вставлен в чат при переезде.
   BotFather → `/revoke` → новый токен в `/opt/lokatsiya-bot/bot/.env`
   на сервере → `systemctl restart lokatsiya-bot`.
2. Заново добавить через `/add` локации, которых нет в репозиторном сиде.

## Что сделано за последние сессии (хронология)

1. Hash-роутинг, поиск, избранное, вынос данных в `catalog.json`.
2. Каталог переведён на t.me-посты канала `@lokatsiyamed` (8→13 категорий).
3. Бэкенд: контактная форма (юзер→админ→ответ), серверное избранное,
   статистика + `/stats`, админ-команды `/add /del /cat_add /cat_icon
   /loc_icon /list`, мульти-админ (`ADMIN_USER_IDS`).
4. Безопасность: `BIND_HOST=127.0.0.1` (была дыра `0.0.0.0:8080`), админы
   исключены из статистики.
5. Редизайн «Friendly Card» (Variant A: 3-кол. сетка + таб-бар).
6. **Prayer-карточка** — следующий намаз Масджид ан-Набави с имамом,
   муаззином, фото, временем, отсчётом. Источник — публичный JSON API
   `haramainflagsapi.prh.gov.sa` (без скрапинга).

## Бэклог / nice-to-have (не начато)

- **hero-promo** — блок в DOM есть, скрыт. Включить под промо/новости:
  либо ручной `showHeroPromo({...})`, либо эндпоинт `GET /api/promo` +
  бот-команда `/promo set ...`.
- **`short_name`** для категорий — чтобы длинные имена («O'ZBEK
  OSHXONALAR») коротко влезали в плитку.
- **Фото к локациям** — обсуждалось (фото-баннер сверху карточки), не
  делалось; нужен хостинг картинок (можно отдавать с VPS `/static/`).
- **Группа для обращений** — сейчас контакты падают в личку
  `ADMIN_CHAT_ID`. Для нескольких админов — создать группу, бот туда
  админом, `ADMIN_CHAT_ID` = id группы (отрицательный).
- **Полный сброс статистики** — `rm /opt/lokatsiya-bot/bot/tracks.json`
  перед рестартом.

## Как продолжать работу

- Окружение: этот Mac телепортирован в сессию. Сайты доступны (sandbox
  нет). node НЕ установлен; python3 (anaconda) и Chrome есть.
- Проверка фронта локально: `python3 -m http.server`, скриншот через
  `"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
  --headless --screenshot=out.png --window-size=440,900 URL`.
- Ветка `claude/review-tg-mini-app-JR4ll`, PR через `gh pr create` /
  `gh pr merge --merge`. Git identity уже настроена локально в репо.
- `index.html` JS-синтаксис проверять загрузкой в headless Chrome и
  грепом `Uncaught|SyntaxError` в `--enable-logging=stderr` логе (node
  для `node --check` тут нет).
- `bot/main.py`: `python3 -m py_compile bot/main.py`.

## Контрольные проверки после деплоя

```
curl -s https://62-169-26-149.sslip.io/health           # {"ok":true,...}
curl -s https://62-169-26-149.sslip.io/api/catalog | head
curl -s https://62-169-26-149.sslip.io/api/prayer-info | head
ss -ltnp | grep :8080                                     # 127.0.0.1:8080
```
