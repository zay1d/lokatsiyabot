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
import re
import shutil
import time
from pathlib import Path
from urllib.parse import parse_qsl

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
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
FAVORITES_FILE = Path(os.environ.get("FAVORITES_FILE", "favorites.json"))
CATALOG_FILE = Path(os.environ.get("CATALOG_FILE", "catalog.json"))
SEED_CATALOG = Path(os.environ.get("SEED_CATALOG", "../catalog.json"))

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())


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


def load_favorites() -> dict[int, set[str]]:
    """Map user_id -> set of favorited URLs."""
    if FAVORITES_FILE.exists():
        try:
            data = json.loads(FAVORITES_FILE.read_text())
            return {int(uid): set(urls) for uid, urls in data.items()}
        except Exception:
            log.exception("Failed to load favorites, starting fresh")
    return {}


def save_favorites(favs: dict[int, set[str]]) -> None:
    try:
        data = {str(uid): sorted(urls) for uid, urls in favs.items() if urls}
        FAVORITES_FILE.write_text(json.dumps(data, ensure_ascii=False))
    except Exception:
        log.exception("Failed to save favorites")


favorites: dict[int, set[str]] = load_favorites()


def init_catalog_file() -> None:
    """Seed CATALOG_FILE from SEED_CATALOG on first run, if it doesn't exist."""
    if CATALOG_FILE.exists():
        return
    CATALOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    if SEED_CATALOG.exists():
        shutil.copy(SEED_CATALOG, CATALOG_FILE)
        log.info("Seeded catalog from %s", SEED_CATALOG)
    else:
        CATALOG_FILE.write_text("{}", encoding="utf-8")
        log.warning("No seed found at %s, starting with empty catalog", SEED_CATALOG)


def load_catalog() -> dict:
    if CATALOG_FILE.exists():
        try:
            return json.loads(CATALOG_FILE.read_text(encoding="utf-8"))
        except Exception:
            log.exception("Failed to load catalog")
    return {}


def save_catalog(cat: dict) -> None:
    try:
        CATALOG_FILE.write_text(json.dumps(cat, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        log.exception("Failed to save catalog")


init_catalog_file()
catalog_data: dict = load_catalog()


def slugify(name: str) -> str:
    s = name.lower().replace("'", "").replace("`", "").replace("’", "")
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s or f"cat_{int(time.time())}"


def is_admin(msg_or_cb) -> bool:
    chat_id = msg_or_cb.chat.id if hasattr(msg_or_cb, "chat") else msg_or_cb.message.chat.id
    return chat_id == ADMIN_CHAT_ID


def extract_user_id(parsed: dict) -> int | None:
    try:
        user = json.loads(parsed.get("user", "{}"))
        uid = user.get("id")
        return uid if isinstance(uid, int) else None
    except Exception:
        return None


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


async def handle_catalog(request: web.Request) -> web.Response:
    resp = web.json_response(catalog_data)
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


async def handle_favs_list(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    parsed = verify_init_data(data.get("initData", ""), BOT_TOKEN)
    if not parsed:
        return web.json_response({"error": "invalid initData"}, status=403)
    uid = extract_user_id(parsed)
    if not uid:
        return web.json_response({"error": "no user"}, status=400)

    return web.json_response({"favorites": sorted(favorites.get(uid, set()))})


async def handle_favs_toggle(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    parsed = verify_init_data(data.get("initData", ""), BOT_TOKEN)
    if not parsed:
        return web.json_response({"error": "invalid initData"}, status=403)
    uid = extract_user_id(parsed)
    if not uid:
        return web.json_response({"error": "no user"}, status=400)

    url = (data.get("url") or "").strip()
    if not url or len(url) > 500:
        return web.json_response({"error": "bad url"}, status=400)

    user_set = favorites.setdefault(uid, set())
    if url in user_set:
        user_set.remove(url)
        action = "removed"
    else:
        user_set.add(url)
        action = "added"
    save_favorites(favorites)
    return web.json_response({"ok": True, "action": action, "count": len(user_set)})


def html_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# -------------------- Bot handlers --------------------

class AddLocation(StatesGroup):
    waiting_category = State()
    waiting_name = State()
    waiting_url = State()


class AddCategory(StatesGroup):
    waiting_name = State()
    waiting_icon = State()


class DelLocation(StatesGroup):
    waiting_category = State()
    waiting_location = State()


ADMIN_HELP = (
    "<b>Admin buyruqlari:</b>\n\n"
    "/add — yangi joy qo'shish\n"
    "/del — joyni o'chirish\n"
    "/cat_add — yangi kategoriya qo'shish\n"
    "/list — barcha kategoriya va joylar\n"
    "/cancel — joriy jarayonni bekor qilish\n"
    "/help — shu ro'yxat"
)


@dp.message(Command("start"))
async def cmd_start(msg: Message) -> None:
    if is_admin(msg):
        await msg.answer("Salom, admin!\n\n" + ADMIN_HELP)
    else:
        await msg.answer(
            "Salom! Bu bot — Lokatsiyalar mini-appiga aloqa kanali.\n"
            "Savol yoki taklif uchun mini-appda 'Biz bilan bog'laning' tugmasidan foydalaning."
        )


@dp.message(Command("help"))
async def cmd_help(msg: Message) -> None:
    if not is_admin(msg):
        return
    await msg.answer(ADMIN_HELP)


@dp.message(Command("cancel"))
async def cmd_cancel(msg: Message, state: FSMContext) -> None:
    if not is_admin(msg):
        return
    cur = await state.get_state()
    await state.clear()
    if cur:
        await msg.answer("❌ Bekor qilindi")
    else:
        await msg.answer("Faol jarayon yo'q")


@dp.message(Command("list"))
async def cmd_list(msg: Message) -> None:
    if not is_admin(msg):
        return
    if not catalog_data:
        await msg.answer("Catalog bo'sh")
        return
    lines = []
    for cat_id, cat in catalog_data.items():
        cl = cat.get("count_label") or f"{len(cat.get('locations', []))} ta"
        lines.append(
            f"\n📁 <b>{html_escape(cat['name'])}</b> · {html_escape(cl)} "
            f"· <code>{html_escape(cat_id)}</code>"
        )
        for i, loc in enumerate(cat.get("locations", []), 1):
            lines.append(f"   {i}. {html_escape(loc['name'])}")
    text = "\n".join(lines).strip() or "Catalog bo'sh"
    if len(text) > 4000:
        text = text[:3900] + "\n\n... (ro'yxat juda uzun)"
    await msg.answer(text)


# ----- /add flow -----

@dp.message(Command("add"))
async def cmd_add(msg: Message, state: FSMContext) -> None:
    if not is_admin(msg):
        return
    await state.clear()
    if not catalog_data:
        await msg.answer("Catalog bo'sh. Avval /cat_add bilan kategoriya qo'shing.")
        return
    builder = InlineKeyboardBuilder()
    for cat_id, cat in catalog_data.items():
        builder.button(text=cat["name"], callback_data=f"add_cat:{cat_id}")
    builder.adjust(1)
    await msg.answer("Qaysi kategoriyaga qo'shasiz?", reply_markup=builder.as_markup())
    await state.set_state(AddLocation.waiting_category)


@dp.callback_query(F.data.startswith("add_cat:"), AddLocation.waiting_category)
async def add_cat_chosen(cb: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(cb):
        await cb.answer()
        return
    cat_id = cb.data.split(":", 1)[1]
    if cat_id not in catalog_data:
        await cb.answer("Kategoriya yo'q", show_alert=True)
        return
    await state.update_data(cat_id=cat_id)
    await cb.message.edit_text(
        f"📁 {html_escape(catalog_data[cat_id]['name'])}\n\nJoy nomini yuboring:"
    )
    await state.set_state(AddLocation.waiting_name)
    await cb.answer()


@dp.message(AddLocation.waiting_name)
async def add_name_received(msg: Message, state: FSMContext) -> None:
    if not is_admin(msg):
        return
    name = (msg.text or "").strip()
    if not name or len(name) > 100:
        await msg.answer("Nom 1-100 belgi bo'lishi kerak. Qaytadan:")
        return
    await state.update_data(name=name)
    await msg.answer(
        f"📍 {html_escape(name)}\n\n"
        "Endi havolani yuboring (https://t.me/... yoki https://maps...):"
    )
    await state.set_state(AddLocation.waiting_url)


@dp.message(AddLocation.waiting_url)
async def add_url_received(msg: Message, state: FSMContext) -> None:
    if not is_admin(msg):
        return
    url = (msg.text or "").strip()
    if not re.match(r"^https?://", url) or len(url) > 500:
        await msg.answer("Havola noto'g'ri. https:// bilan boshlanishi va 500 belgidan kam bo'lishi kerak:")
        return
    data = await state.get_data()
    cat_id = data.get("cat_id")
    name = data.get("name")
    if not cat_id or cat_id not in catalog_data or not name:
        await msg.answer("Jarayon buzildi, qaytadan /add")
        await state.clear()
        return
    catalog_data[cat_id].setdefault("locations", []).append({"name": name, "url": url})
    save_catalog(catalog_data)
    total = len(catalog_data[cat_id]["locations"])
    await msg.answer(
        "✅ <b>Qo'shildi</b>\n\n"
        f"📁 {html_escape(catalog_data[cat_id]['name'])}\n"
        f"📍 {html_escape(name)}\n"
        f"🔗 {html_escape(url)}\n\n"
        f"Endi kategoriyada {total} ta joy"
    )
    await state.clear()


# ----- /cat_add flow -----

@dp.message(Command("cat_add"))
async def cmd_cat_add(msg: Message, state: FSMContext) -> None:
    if not is_admin(msg):
        return
    await state.clear()
    await msg.answer("Yangi kategoriya nomi (masalan: <code>KAFE</code>):")
    await state.set_state(AddCategory.waiting_name)


@dp.message(AddCategory.waiting_name)
async def cat_name_received(msg: Message, state: FSMContext) -> None:
    if not is_admin(msg):
        return
    name = (msg.text or "").strip()
    if not name or len(name) > 50:
        await msg.answer("Nom 1-50 belgi. Qaytadan:")
        return
    await state.update_data(name=name)
    await msg.answer(
        f"📁 {html_escape(name)}\n\n"
        "Ikona nomini yuboring (lucide.dev/icons).\n\n"
        "Misollar: <code>coffee</code>, <code>utensils</code>, <code>shopping-bag</code>, "
        "<code>store</code>, <code>flame</code>, <code>smile</code>, <code>trees</code>, "
        "<code>plane</code>, <code>tag</code>, <code>sofa</code>, <code>dumbbell</code>, "
        "<code>croissant</code>, <code>cake-slice</code>, <code>stethoscope</code>"
    )
    await state.set_state(AddCategory.waiting_icon)


@dp.message(AddCategory.waiting_icon)
async def cat_icon_received(msg: Message, state: FSMContext) -> None:
    if not is_admin(msg):
        return
    icon = (msg.text or "").strip().lower()
    if not re.match(r"^[a-z0-9-]{1,40}$", icon):
        await msg.answer("Ikona nomi noto'g'ri (lotin harf, raqam, defis). Qaytadan:")
        return
    data = await state.get_data()
    name = data.get("name")
    if not name:
        await msg.answer("Jarayon buzildi, qaytadan /cat_add")
        await state.clear()
        return
    base = slugify(name)
    cat_id = base
    suffix = 2
    while cat_id in catalog_data:
        cat_id = f"{base}_{suffix}"
        suffix += 1
    catalog_data[cat_id] = {"name": name, "icon": icon, "locations": []}
    save_catalog(catalog_data)
    await msg.answer(
        "✅ <b>Kategoriya qo'shildi</b>\n\n"
        f"📁 {html_escape(name)}\n"
        f"🎨 {html_escape(icon)}\n"
        f"ID: <code>{html_escape(cat_id)}</code>\n\n"
        "Endi /add bilan joy qo'shing."
    )
    await state.clear()


# ----- /del flow -----

@dp.message(Command("del"))
async def cmd_del(msg: Message, state: FSMContext) -> None:
    if not is_admin(msg):
        return
    await state.clear()
    if not catalog_data:
        await msg.answer("Catalog bo'sh")
        return
    builder = InlineKeyboardBuilder()
    for cat_id, cat in catalog_data.items():
        count = len(cat.get("locations", []))
        builder.button(text=f"{cat['name']} ({count})", callback_data=f"del_cat:{cat_id}")
    builder.adjust(1)
    await msg.answer("Qaysi kategoriyadan o'chiramiz?", reply_markup=builder.as_markup())
    await state.set_state(DelLocation.waiting_category)


@dp.callback_query(F.data.startswith("del_cat:"), DelLocation.waiting_category)
async def del_cat_chosen(cb: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(cb):
        await cb.answer()
        return
    cat_id = cb.data.split(":", 1)[1]
    if cat_id not in catalog_data:
        await cb.answer("Yo'q", show_alert=True)
        return
    cat = catalog_data[cat_id]
    locs = cat.get("locations", [])
    if not locs:
        await cb.message.edit_text(f"📁 {html_escape(cat['name'])} — bo'sh")
        await state.clear()
        await cb.answer()
        return
    builder = InlineKeyboardBuilder()
    for i, loc in enumerate(locs):
        nm = loc["name"]
        if len(nm) > 32:
            nm = nm[:29] + "..."
        builder.button(text=f"{i+1}. {nm}", callback_data=f"del_loc:{cat_id}:{i}")
    builder.adjust(1)
    await cb.message.edit_text(
        f"📁 {html_escape(cat['name'])}\n\nQaysi joyni o'chirasiz?",
        reply_markup=builder.as_markup()
    )
    await state.set_state(DelLocation.waiting_location)
    await cb.answer()


@dp.callback_query(F.data.startswith("del_loc:"), DelLocation.waiting_location)
async def del_loc_chosen(cb: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(cb):
        await cb.answer()
        return
    parts = cb.data.split(":")
    if len(parts) != 3:
        await cb.answer("Bad data", show_alert=True)
        return
    cat_id = parts[1]
    try:
        idx = int(parts[2])
    except ValueError:
        await cb.answer("Bad index", show_alert=True)
        return
    if cat_id not in catalog_data:
        await cb.answer("Kategoriya yo'q", show_alert=True)
        return
    locs = catalog_data[cat_id].get("locations", [])
    if idx < 0 or idx >= len(locs):
        await cb.answer("Indeks topilmadi", show_alert=True)
        return
    removed = locs.pop(idx)
    save_catalog(catalog_data)
    await cb.message.edit_text(
        "✅ <b>O'chirildi</b>\n\n"
        f"📁 {html_escape(catalog_data[cat_id]['name'])}\n"
        f"📍 {html_escape(removed['name'])}"
    )
    await state.clear()
    await cb.answer()


@dp.message(F.reply_to_message, StateFilter(None))
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
    app.router.add_post("/api/favorites/list", handle_favs_list)
    app.router.add_post("/api/favorites/toggle", handle_favs_toggle)
    app.router.add_get("/api/catalog", handle_catalog)
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
