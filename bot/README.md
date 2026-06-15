# Lokatsiya bot — contact backend

Минимальный бекенд для фичи "Biz bilan bog'laning":
- HTTP API (`POST /api/contact`) — принимает сообщения из мини-аппа
- Telegram bot — пересылает их в админ-чат, и доставляет ответы админа обратно юзерам

## Что делает

1. Юзер пишет сообщение в мини-аппе → JS POSTит `{initData, message}` на `/api/contact`.
2. Сервер проверяет подпись `initData` (HMAC через bot token) — гарантирует что юзер реальный Telegram-аккаунт.
3. Сообщение уходит ботом в `ADMIN_CHAT_ID` с заголовком, ID юзера и текстом.
4. Админ отвечает реплаем на это сообщение прямо в Telegram.
5. Бот ловит реплай, по `message_id` находит исходного юзера и пересылает ему ответ как «Admin javobi: ...».

Дополнительно бэк отдаёт:
- `GET /api/catalog` — актуальный каталог (правится админ-командами).
- `POST /api/favorites/*` — серверное избранное.
- `POST /api/track` + `/stats` — статистика.
- `GET /api/prayer-info` — следующий намаз в Масджид ан-Набави (имя и фото имама/муаззина, время, дата по хиджре). Данные берутся из публичного JSON API Управления (`haramainflagsapi.prh.gov.sa`), кешируются в памяти на 30 минут. Никакого скрапинга/Playwright — чистый HTTP-фетч. Эндпоинт публичный (без initData), фото отдаются как прямые HTTPS-ссылки на CDN.

## Требования

- Python 3.11+
- Свой VPS с публичным HTTPS-адресом (sslip.io / Contabo hostname / свой домен — любой вариант с валидным сертификатом).
- Бот, созданный через @BotFather (нужен токен).

## Установка локально / на VPS

```bash
cd bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Конфигурация (env vars)

| Переменная | Обязательная | Описание |
|---|---|---|
| `BOT_TOKEN` | да | Токен бота от @BotFather |
| `ADMIN_CHAT_ID` | да | ID чата куда падают **обращения юзеров через мини-апп**. Узнать свой: напиши боту @userinfobot. Если хочешь чтобы обращения видели несколько админов — создай групповой чат, добавь бота туда админом, и поставь сюда ID группы (он будет отрицательный). |
| `ADMIN_USER_IDS` | нет | Список user ID через запятую, кому разрешены команды (`/add`, `/stats` и т.д.). Если не задан — берётся `ADMIN_CHAT_ID`. Пример: `ADMIN_USER_IDS=6309709092,123456789,987654321` |
| `PORT` | нет | HTTP-порт (по умолчанию `8080`) |
| `BIND_HOST` | нет | Адрес для бинда (по умолчанию `127.0.0.1` — только localhost, nginx проксирует снаружи). **Не ставьте `0.0.0.0`** — это откроет API в интернет в обход TLS. |
| `ALLOWED_ORIGIN` | нет | CORS origin (по умолчанию `*`; для прода поставь `https://zay1d.github.io`) |
| `STATE_FILE` | нет | Файл для сохранения мапы admin_msg → user_id (по умолчанию `state.json`) |
| `FAVORITES_FILE` | нет | Файл для хранения избранного юзеров (по умолчанию `favorites.json`) |
| `CATALOG_FILE` | нет | Файл с актуальным каталогом, в который пишут админ-команды (по умолчанию `catalog.json` в папке `bot/`) |
| `SEED_CATALOG` | нет | Откуда брать стартовый каталог при первом запуске (по умолчанию `../catalog.json` — статичный из репо) |
| `TRACKS_FILE` | нет | Файл со статистикой (по умолчанию `tracks.json`) |

## Админ-команды в боте

Все команды доступны **только пользователям, чей Telegram user ID присутствует в `ADMIN_USER_IDS`** (если не задан — то одному человеку из `ADMIN_CHAT_ID`). Каждый админ работает в своей личке с ботом. Обычным юзерам команды просто игнорируются (бот не отвечает).

| Команда | Что делает |
|---|---|
| `/add` | Добавить новую локацию. Бот пришлёт кнопки с категориями → выбираешь → вводишь название → вводишь URL → готово |
| `/del` | Удалить локацию. Кнопки по категориям → кнопки по локациям → удаление |
| `/cat_add` | Добавить новую категорию. Имя → пикер иконок (или ручной ввод имени с lucide.dev) |
| `/cat_icon` | Поменять иконку существующей категории — выбор категории → пикер иконок |
| `/loc_icon` | Установить (или сбросить) персональную иконку для конкретной локации. Если не установлена — используется иконка категории |
| `/list` | Полный список всех категорий и локаций с их ID |
| `/stats` | Статистика: уникальные юзеры, открытия (день/неделя/месяц), топ категорий и локаций, число обращений |
| `/cancel` | Отменить текущий FSM-процесс (если зашёл в `/add` и передумал) |
| `/help` | Показать список команд |

После любой команды правки попадают в `CATALOG_FILE` на диске **и сразу видны** в мини-аппе при следующем открытии (фронт фетчит `GET /api/catalog`). Перезапускать бот не нужно.

Пример запуска вручную:

```bash
BOT_TOKEN='123:ABC...' \
ADMIN_CHAT_ID='123456789' \
ALLOWED_ORIGIN='https://zay1d.github.io' \
python main.py
```

## Деплой на Contabo (systemd-сервис за nginx-reverse-proxy)

### 1. Скопировать код на сервер

```bash
# на VPS
sudo mkdir -p /opt/lokatsiya-bot
sudo chown $USER /opt/lokatsiya-bot
git clone https://github.com/zay1d/lokatsiyabot.git /opt/lokatsiya-bot
cd /opt/lokatsiya-bot/bot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 2. Создать env-файл

```bash
sudo tee /opt/lokatsiya-bot/bot/.env >/dev/null <<EOF
BOT_TOKEN=твой_токен
ADMIN_CHAT_ID=твой_id
ALLOWED_ORIGIN=https://zay1d.github.io
PORT=8080
EOF
sudo chmod 600 /opt/lokatsiya-bot/bot/.env
```

### 3. systemd unit

```bash
sudo tee /etc/systemd/system/lokatsiya-bot.service >/dev/null <<'EOF'
[Unit]
Description=Lokatsiya contact bot + API
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/lokatsiya-bot/bot
EnvironmentFile=/opt/lokatsiya-bot/bot/.env
ExecStart=/opt/lokatsiya-bot/bot/.venv/bin/python main.py
Restart=on-failure
RestartSec=3
User=root

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now lokatsiya-bot
sudo systemctl status lokatsiya-bot
journalctl -u lokatsiya-bot -f
```

### 4. nginx + HTTPS

Поставь nginx и certbot, добавь конфиг:

```nginx
server {
    listen 80;
    server_name 194-163-x-x.sslip.io;  # подставь свой
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name 194-163-x-x.sslip.io;

    ssl_certificate     /etc/letsencrypt/live/194-163-x-x.sslip.io/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/194-163-x-x.sslip.io/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Получить сертификат:

```bash
sudo certbot --nginx -d 194-163-x-x.sslip.io
```

### 5. Прописать API_URL во фронте

В `index.html` найди строку:
```js
const CONFIG = {
  API_URL: '', // e.g., 'https://api.your-server.sslip.io'
};
```
Подставь свой URL, например `https://194-163-x-x.sslip.io`. Закоммить, запушь — GitHub Pages автодеплоит, кнопка заработает.

## Тестирование без VPS

Можно поднять бекенд локально и потестить через ngrok:

```bash
# терминал 1
BOT_TOKEN='...' ADMIN_CHAT_ID='...' python main.py

# терминал 2
ngrok http 8080
# скопируй HTTPS-URL и пропиши в API_URL фронта
```

## Безопасность

- `initData` проверяется HMAC-подписью — подделать не получится. Только реальные Telegram-юзеры могут что-то прислать.
- Боту/процессу нужен доступ только наружу к `api.telegram.org` и к собственному порту. Никакой БД, никаких внешних зависимостей.
- На прод обязательно поставь `ALLOWED_ORIGIN` в адрес твоего фронта (не `*`), и закрой через `ufw` всё кроме 22, 80, 443.
- Файл `.env` с токеном — `chmod 600`, под `root` или отдельным юзером.
- **Python-сервер биндится на `127.0.0.1` по умолчанию** (`BIND_HOST=127.0.0.1`). Снаружи трогать API можно только через nginx → HTTPS. Никогда не ставь `0.0.0.0` — иначе API торчит в интернет без TLS и в обход CORS.

### Проверка что порт 8080 закрыт снаружи

```bash
# На сервере:
ss -ltnp | grep :8080
# Должно показать: 127.0.0.1:8080 (а не 0.0.0.0:8080 и не *:8080)

# Двойная защита — firewall:
sudo ufw allow 22/tcp     # SSH
sudo ufw allow 80/tcp     # nginx HTTP (для certbot renew)
sudo ufw allow 443/tcp    # nginx HTTPS
sudo ufw deny 8080/tcp    # явный запрет голого API-порта
sudo ufw enable
sudo ufw status verbose
```

### Если случайно осталось `0.0.0.0:8080`

С внешней машины проверь:
```bash
curl -v http://167.86.125.229:8080/health
```
- Если отвечает JSON `{"ok":true,...}` → **уязвимость есть**, чини
- Если `connection refused` или timeout → закрыто, всё ок
