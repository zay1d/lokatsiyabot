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
| `ADMIN_CHAT_ID` | да | ID чата куда падают сообщения. Узнать свой: напиши боту @userinfobot. Если нужен групповой чат — добавь бота в группу админом, ID будет отрицательный. |
| `PORT` | нет | HTTP-порт (по умолчанию `8080`) |
| `ALLOWED_ORIGIN` | нет | CORS origin (по умолчанию `*`; для прода поставь `https://zay1d.github.io`) |
| `STATE_FILE` | нет | Файл для сохранения мапы admin_msg → user_id (по умолчанию `state.json`) |

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
