"""
Backend for lokatsiyabot mini app contact feature.

Responsibilities:
- Receive contact messages from the mini app via HTTP (POST /api/contact)
- Verify Telegram WebApp initData signature (so only real Telegram users can post)
- Forward messages to the admin chat via the bot
- Listen for admin replies (reply-to-message in admin chat) and relay them back to the original user

Single-process: runs both the aiohttp web server and the aiogram bot polling loop.

Env vars required:
    BOT_TOKEN       Telegram bot token from @BotFather
    ADMIN_CHAT_ID   Telegram chat id where admin reads incoming messages
                    (your own user id, or a group id where you're a member)

Optional:
    PORT            HTTP port to bind (default 8080)
    ALLOWED_ORIGIN  CORS origin to allow (default '*'; in prod set to your frontend URL)
    STATE_FILE      Where to persist user_id ↔ admin_message_id map (default state.json)
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
from urllib.parse import parse_qsl

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import Message
from aiohttp import web

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)
log = logging.getLogger("contact-bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_CHAT_ID = int(os.environ["ADMIN_CHAT_ID"])
PORT = int(os.environ.get("PORT", "8080"))
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "*")
STATE_FILE = Path(os.environ.get("STATE_FILE", "state.json"))

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()


def load_state() -> dict[int, int]:
    """Map admin_message_id -> user_id. Persisted across restarts."""
    if STATE_FILE.exists():
        try:
            return {int(k): int(v) for k, v in json.loads(STATE_FILE.read_text()).items()}
        except Exception:
            log.exception("Failed to load state, starting fresh")
    return {}


def save_state(state: dict[int, int]) -> None:
    try:
        STATE_FILE.write_text(json.dumps({str(k): v for k, v in state.items()}))
    except Exception:
        log.exception("Failed to save state")


admin_msg_to_user: dict[int, int] = load_state()


def verify_init_data(init_data: str, bot_token: str) -> dict | None:
    """Verify Telegram WebApp initData and return parsed fields, or None if invalid."""
    if not init_data:
        return None
    try:
        parsed = dict(parse_qsl(init_data, strict_parsing=True))
    except Exception:
        return None
    received_hash = parsed.pop("hash", "")
    if not received_hash:
        return None
    data_check = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret_key, data_check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        return None
    return parsed


# -------------------- HTTP --------------------

@web.middleware
async def cors_middleware(request: web.Request, handler):
    if request.method == "OPTIONS":
        return web.Response(
            status=204,
            headers={
                "Access-Control-Allow-Origin": ALLOWED_ORIGIN,
                "Access-Control-Allow-Methods": "POST, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type",
                "Access-Control-Max-Age": "86400",
            },
        )
    response = await handler(request)
    response.headers["Access-Control-Allow-Origin"] = ALLOWED_ORIGIN
    return response


async def handle_contact(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    init_data = data.get("initData", "")
    message_text = (data.get("message") or "").strip()

    if not message_text:
        return web.json_response({"error": "empty message"}, status=400)
    if len(message_text) > 4000:
        return web.json_response({"error": "message too long"}, status=400)

    parsed = verify_init_data(init_data, BOT_TOKEN)
    if not parsed:
        return web.json_response({"error": "invalid initData"}, status=403)

    try:
        user = json.loads(parsed.get("user", "{}"))
    except Exception:
        user = {}
    user_id = user.get("id")
    if not isinstance(user_id, int):
        return web.json_response({"error": "no user"}, status=400)

    first = (user.get("first_name") or "").strip()
    last = (user.get("last_name") or "").strip()
    username = user.get("username")
    full_name = (first + " " + last).strip() or "—"

    header = f"📩 <b>Yangi xabar</b>\n👤 {html_escape(full_name)}"
    if username:
        header += f" (@{html_escape(username)})"
    header += f"\n🆔 <code>{user_id}</code>\n\n{html_escape(message_text)}"

    try:
        sent = await bot.send_message(ADMIN_CHAT_ID, header)
    except Exception:
        log.exception("Failed to forward to admin")
        return web.json_response({"error": "telegram error"}, status=502)

    admin_msg_to_user[sent.message_id] = user_id
    save_state(admin_msg_to_user)
    log.info("Forwarded msg from user %s to admin (message_id=%s)", user_id, sent.message_id)
    return web.json_response({"ok": True})


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "service": "lokatsiya-contact"})


def html_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# -------------------- Bot handlers --------------------

@dp.message(Command("start"))
async def cmd_start(msg: Message) -> None:
    await msg.answer(
        "Salom! Bu bot — Lokatsiyalar mini-appiga aloqa kanali.\n"
        "Savol yoki taklif uchun mini-appda 'Biz bilan bog'laning' tugmasidan foydalaning."
    )


@dp.message(F.reply_to_message)
async def admin_reply(msg: Message) -> None:
    """When admin replies to a forwarded message in the admin chat, relay to the original user."""
    if msg.chat.id != ADMIN_CHAT_ID:
        return
    target_user_id = admin_msg_to_user.get(msg.reply_to_message.message_id)
    if not target_user_id:
        return

    text = msg.text or msg.caption or ""
    if not text.strip():
        await msg.reply("Matnli javob yuboring (rasm/fayl hozircha qo'llanmaydi).")
        return

    try:
        await bot.send_message(
            target_user_id,
            f"💬 <b>Admin javobi:</b>\n\n{html_escape(text)}",
        )
        await msg.reply("✅ Yuborildi")
    except Exception:
        log.exception("Failed to relay admin reply to user %s", target_user_id)
        await msg.reply("⚠️ Yuborib bo'lmadi (foydalanuvchi botni blok qilgan bo'lishi mumkin).")


# -------------------- Run --------------------

async def main() -> None:
    app = web.Application(middlewares=[cors_middleware])
    app.router.add_post("/api/contact", handle_contact)
    app.router.add_get("/health", handle_health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("HTTP server listening on :%s", PORT)

    try:
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
