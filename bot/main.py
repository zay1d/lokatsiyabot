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
from datetime import datetime, timedelta, timezone

import aiohttp
from aiohttp import web

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)
log = logging.getLogger("contact-bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_CHAT_ID = int(os.environ["ADMIN_CHAT_ID"])


def _parse_admin_user_ids(raw: str | None, fallback: int) -> set[int]:
    """ADMIN_USER_IDS env var: comma-separated user IDs allowed to run admin commands.
    Always includes ADMIN_CHAT_ID owner as a sane default."""
    out: set[int] = {fallback}
    if raw:
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                out.add(int(part))
            except ValueError:
                log.warning("ADMIN_USER_IDS: skipping invalid value %r", part)
    return out


ADMIN_USER_IDS: set[int] = _parse_admin_user_ids(os.environ.get("ADMIN_USER_IDS"), ADMIN_CHAT_ID)
log.info("Admin user IDs: %s", sorted(ADMIN_USER_IDS))

PORT = int(os.environ.get("PORT", "8080"))
# Bind only to localhost by default — nginx terminates TLS and proxies in.
# DO NOT set to 0.0.0.0 unless you know exactly why (it exposes the API
# bypassing nginx/TLS to anyone on the internet).
BIND_HOST = os.environ.get("BIND_HOST", "127.0.0.1")
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "*")
STATE_FILE = Path(os.environ.get("STATE_FILE", "state.json"))
FAVORITES_FILE = Path(os.environ.get("FAVORITES_FILE", "favorites.json"))
CATALOG_FILE = Path(os.environ.get("CATALOG_FILE", "catalog.json"))
SEED_CATALOG = Path(os.environ.get("SEED_CATALOG", "../catalog.json"))
TRACKS_FILE = Path(os.environ.get("TRACKS_FILE", "tracks.json"))

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


# -------------------- Tracks (analytics) --------------------
# Stored at day granularity so we don't keep exact timestamps.
# {
#   "users": set[str(user_id)],          # all-time unique users
#   "contacts": int,                      # running counter of contact submissions
#   "daily": { "YYYY-MM-DD": {
#       "users": set[str(user_id)],       # unique users that day
#       "opens": int,                      # total opens that day
#       "categories": {cat_id: int},
#       "locations": {url: int}
#   }}
# }

def load_tracks() -> dict:
    base = {"users": set(), "contacts": 0, "daily": {}}
    if not TRACKS_FILE.exists():
        return base
    try:
        d = json.loads(TRACKS_FILE.read_text(encoding="utf-8"))
        return {
            "users": set(d.get("users", [])),
            "contacts": int(d.get("contacts", 0)),
            "daily": {
                day: {
                    "users": set(v.get("users", [])),
                    "opens": int(v.get("opens", 0)),
                    "categories": {k: int(c) for k, c in v.get("categories", {}).items()},
                    "locations": {k: int(c) for k, c in v.get("locations", {}).items()},
                } for day, v in d.get("daily", {}).items()
            },
        }
    except Exception:
        log.exception("Failed to load tracks")
        return base


def save_tracks(t: dict) -> None:
    try:
        serialized = {
            "users": sorted(t["users"]),
            "contacts": t["contacts"],
            "daily": {
                day: {
                    "users": sorted(v["users"]),
                    "opens": v["opens"],
                    "categories": v["categories"],
                    "locations": v["locations"],
                } for day, v in t["daily"].items()
            },
        }
        TRACKS_FILE.write_text(json.dumps(serialized, ensure_ascii=False), encoding="utf-8")
    except Exception:
        log.exception("Failed to save tracks")


tracks: dict = load_tracks()


def _cleanup_admin_from_tracks() -> None:
    """Remove admin IDs from existing tracks so 'unique users' counts are clean.
    Aggregate counters (opens/categories/locations) can't be unwound — they keep
    the pre-fix historical numbers. Runs once at startup."""
    admin_strs = {str(uid) for uid in ADMIN_USER_IDS}
    changed = False
    before = len(tracks["users"])
    tracks["users"].difference_update(admin_strs)
    if len(tracks["users"]) != before:
        changed = True
    for day_data in tracks["daily"].values():
        before = len(day_data["users"])
        day_data["users"].difference_update(admin_strs)
        if len(day_data["users"]) != before:
            changed = True
    if changed:
        save_tracks(tracks)
        log.info("Removed admin user_ids from historical tracks")


_cleanup_admin_from_tracks()


def _today_utc() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _day_bucket(day: str) -> dict:
    if day not in tracks["daily"]:
        tracks["daily"][day] = {"users": set(), "opens": 0, "categories": {}, "locations": {}}
    return tracks["daily"][day]


def slugify(name: str) -> str:
    s = name.lower().replace("'", "").replace("`", "").replace("’", "")
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s or f"cat_{int(time.time())}"


def is_admin(msg_or_cb) -> bool:
    """Admin = the message author is in ADMIN_USER_IDS. Works in both DM and group chats."""
    from_user = getattr(msg_or_cb, "from_user", None)
    if from_user and from_user.id in ADMIN_USER_IDS:
        return True
    return False


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
    # Admins don't pollute the contacts counter (they test the form)
    if user_id not in ADMIN_USER_IDS:
        tracks["contacts"] += 1
        save_tracks(tracks)
    log.info("Forwarded msg from user %s to admin (message_id=%s)", user_id, sent.message_id)
    return web.json_response({"ok": True})


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "service": "lokatsiya-contact"})


async def handle_catalog(request: web.Request) -> web.Response:
    resp = web.json_response(catalog_data)
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


# -------------------- Prayer info (Al-Masjid an-Nabawi imams/muezzins) --------------------
# Public JSON API from the General Presidency for the Affairs of the Two Holy Mosques.
# No scraping needed: https://haramainflagsapi.prh.gov.sa/prayers?datetimestampz=YYYY-MM-DD&mosque=madinah
# Returns next upcoming prayer with imam + muezzin names and portrait photos.

PRAYER_API = "https://haramainflagsapi.prh.gov.sa/prayers"
KSA_TZ = timezone(timedelta(hours=3))  # Asia/Riyadh, no DST
MAIN_PRAYERS = ["fajr", "dhuhr", "asr", "maghrib", "isha"]
PRAYER_LABELS = {
    "fajr": "Bomdod",
    "dhuhr": "Peshin",
    "asr": "Asr",
    "maghrib": "Shom",
    "isha": "Xufton",
}
PRAYER_LABELS_AR = {
    "fajr": "الفجر", "dhuhr": "الظهر", "asr": "العصر",
    "maghrib": "المغرب", "isha": "العشاء",
}
HIJRI_MONTHS = [
    "Muharram", "Safar", "Rabiul-avval", "Rabiul-oxir", "Jumadul-avval",
    "Jumadul-oxir", "Rajab", "Sha'bon", "Ramazon", "Shavvol",
    "Zulqa'da", "Zulhijja",
]

# Simple in-memory cache: date_str -> (fetched_at_epoch, raw_list)
_prayer_cache: dict[str, tuple[float, list]] = {}
_PRAYER_TTL = 30 * 60  # 30 minutes


async def _fetch_prayers_for(date_str: str) -> list | None:
    """Fetch (and cache) the raw prayer list for a KSA date (YYYY-MM-DD)."""
    now = time.time()
    cached = _prayer_cache.get(date_str)
    if cached and now - cached[0] < _PRAYER_TTL:
        return cached[1]
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                PRAYER_API,
                params={"datetimestampz": date_str, "mosque": "madinah"},
                headers={"User-Agent": "Mozilla/5.0"},
            ) as resp:
                if resp.status != 200:
                    log.warning("prayer api HTTP %s for %s", resp.status, date_str)
                    return cached[1] if cached else None
                data = await resp.json()
    except Exception:
        log.exception("prayer api fetch failed for %s", date_str)
        return cached[1] if cached else None
    if not isinstance(data, list):
        return None
    _prayer_cache[date_str] = (now, data)
    return data


def _person_name(p: dict | None) -> str:
    if not p:
        return ""
    parts = [p.get("firstNameEn"), p.get("middleNameEn"), p.get("lastNameEn")]
    return " ".join(x for x in parts if x).strip()


def _person_name_ar(p: dict | None) -> str:
    if not p:
        return ""
    parts = [p.get("firstName"), p.get("middleName"), p.get("lastName")]
    return " ".join(x for x in parts if x).strip()


def _format_prayer(entry: dict) -> dict:
    imam = entry.get("imam") or {}
    muezzin = entry.get("muezzin") or {}
    name = entry.get("prayer", "")
    try:
        hm = HIJRI_MONTHS[int(entry.get("hijriMonth", 0)) - 1]
    except Exception:
        hm = ""
    hijri = f"{entry.get('hijriDay','')} {hm} {entry.get('hijriYear','')}".strip()
    return {
        "available": True,
        "prayer": name,
        "prayer_label": PRAYER_LABELS.get(name, name.title()),
        "prayer_ar": PRAYER_LABELS_AR.get(name, ""),
        "time_utc": entry.get("datetimestampz", ""),
        "imam_name": _person_name(imam) or "—",
        "imam_name_ar": _person_name_ar(imam),
        "imam_photo": imam.get("image") or "",
        "muezzin_name": _person_name(muezzin) or "—",
        "muezzin_name_ar": _person_name_ar(muezzin),
        "muezzin_photo": muezzin.get("image") or "",
        "hijri": hijri,
    }


async def _next_prayer() -> dict | None:
    now_utc = datetime.now(timezone.utc)
    today_ksa = (now_utc + timedelta(hours=3)).date().isoformat()

    today = await _fetch_prayers_for(today_ksa)
    if today:
        upcoming = []
        for entry in today:
            if entry.get("prayer") not in MAIN_PRAYERS:
                continue
            ts = entry.get("datetimestampz")
            if not ts:
                continue
            try:
                pt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except Exception:
                continue
            if pt > now_utc:
                upcoming.append((pt, entry))
        if upcoming:
            upcoming.sort(key=lambda x: x[0])
            return _format_prayer(upcoming[0][1])

    # All of today's prayers passed → tomorrow's fajr
    tomorrow_ksa = (now_utc + timedelta(hours=3) + timedelta(days=1)).date().isoformat()
    tomorrow = await _fetch_prayers_for(tomorrow_ksa)
    if tomorrow:
        for entry in tomorrow:
            if entry.get("prayer") == "fajr":
                return _format_prayer(entry)
    return None


async def handle_prayer_info(request: web.Request) -> web.Response:
    info = await _next_prayer()
    if not info:
        return web.json_response({"available": False})
    resp = web.json_response(info)
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp


async def handle_prayer_today(request: web.Request) -> web.Response:
    """Returns today's full 5-prayer schedule (KSA time) with imam + muezzin per prayer."""
    now_utc = datetime.now(timezone.utc)
    today_ksa = (now_utc + timedelta(hours=3)).date().isoformat()
    raw = await _fetch_prayers_for(today_ksa)
    if not raw:
        return web.json_response({"available": False})

    formatted = []
    for entry in raw:
        if entry.get("prayer") not in MAIN_PRAYERS:
            continue
        formatted.append(_format_prayer(entry))
    formatted.sort(key=lambda p: p.get("time_utc", ""))

    if not formatted:
        return web.json_response({"available": False})

    # Hijri date from the first prayer (same all day)
    hijri = formatted[0].get("hijri", "")

    resp = web.json_response({
        "available": True,
        "date": today_ksa,
        "hijri": hijri,
        "prayers": formatted,
    })
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp


async def handle_track(request: web.Request) -> web.Response:
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

    event = data.get("event") or ""
    target = (data.get("target") or "")[:500]
    if event not in ("open", "category", "location"):
        return web.json_response({"error": "bad event"}, status=400)
    if event in ("category", "location") and not target:
        return web.json_response({"error": "target required"}, status=400)

    # Admins are excluded from statistics so their own testing doesn't skew numbers
    if uid in ADMIN_USER_IDS:
        return web.json_response({"ok": True, "skipped": "admin"})

    str_uid = str(uid)
    bucket = _day_bucket(_today_utc())

    if event == "open":
        tracks["users"].add(str_uid)
        bucket["users"].add(str_uid)
        bucket["opens"] += 1
    elif event == "category":
        bucket["categories"][target] = bucket["categories"].get(target, 0) + 1
    elif event == "location":
        bucket["locations"][target] = bucket["locations"].get(target, 0) + 1

    save_tracks(tracks)
    return web.json_response({"ok": True})


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


class EditCategoryIcon(StatesGroup):
    waiting_category = State()
    waiting_icon = State()


class EditLocationIcon(StatesGroup):
    waiting_category = State()
    waiting_location = State()
    waiting_icon = State()


# (emoji, lucide-name) — emoji is just a visual hint in the button text
COMMON_ICONS: list[tuple[str, str]] = [
    ("🛍️", "shopping-bag"),
    ("🏪", "store"),
    ("🏷️", "tag"),
    ("🎁", "gift"),
    ("☕", "coffee"),
    ("🍽️", "utensils"),
    ("🍲", "soup"),
    ("🍕", "pizza"),
    ("🍰", "cake-slice"),
    ("🥐", "croissant"),
    ("🍨", "ice-cream"),
    ("🩺", "stethoscope"),
    ("😊", "smile"),
    ("➕", "cross"),
    ("💊", "pill"),
    ("✂️", "scissors"),
    ("✈️", "plane"),
    ("🚗", "car"),
    ("⛽", "fuel"),
    ("📍", "map-pin"),
    ("🔥", "flame"),
    ("🏋️", "dumbbell"),
    ("🌳", "trees"),
    ("🛋️", "sofa"),
    ("🏠", "home"),
    ("🔧", "wrench"),
]


def icons_keyboard(prefix: str, *, skip_label: str | None = None):
    """Inline keyboard with curated lucide icons. Optionally add a 'skip' row."""
    builder = InlineKeyboardBuilder()
    for emoji, name in COMMON_ICONS:
        builder.button(text=f"{emoji} {name}", callback_data=f"{prefix}:{name}")
    builder.adjust(2)
    if skip_label:
        from aiogram.types import InlineKeyboardButton
        builder.row(InlineKeyboardButton(text=skip_label, callback_data=f"{prefix}:_skip"))
    return builder.as_markup()


ADMIN_HELP = (
    "<b>Admin buyruqlari:</b>\n\n"
    "/add — yangi joy qo'shish\n"
    "/del — joyni o'chirish\n"
    "/cat_add — yangi kategoriya qo'shish\n"
    "/cat_icon — kategoriya ikonasini o'zgartirish\n"
    "/loc_icon — joy uchun shaxsiy ikona qo'yish\n"
    "/list — barcha kategoriya va joylar\n"
    "/stats — statistika (foydalanuvchilar, ochilishlar, top)\n"
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


@dp.message(Command("stats"))
async def cmd_stats(msg: Message) -> None:
    if not is_admin(msg):
        return
    from datetime import datetime, timedelta, timezone

    today_date = datetime.now(timezone.utc).date()
    today_str = today_date.isoformat()
    last7 = [(today_date - timedelta(days=i)).isoformat() for i in range(7)]
    last30 = [(today_date - timedelta(days=i)).isoformat() for i in range(30)]

    def union_users(days):
        out = set()
        for d in days:
            out.update(tracks["daily"].get(d, {}).get("users", set()))
        return out

    total_users = len(tracks["users"])
    today_users = len(tracks["daily"].get(today_str, {}).get("users", set()))
    week_users = len(union_users(last7))
    month_users = len(union_users(last30))

    today_opens = tracks["daily"].get(today_str, {}).get("opens", 0)
    week_opens = sum(tracks["daily"].get(d, {}).get("opens", 0) for d in last7)
    total_opens = sum(v.get("opens", 0) for v in tracks["daily"].values())

    cat_counts: dict[str, int] = {}
    for d in last30:
        for cat_id, c in tracks["daily"].get(d, {}).get("categories", {}).items():
            cat_counts[cat_id] = cat_counts.get(cat_id, 0) + c
    top_cats = sorted(cat_counts.items(), key=lambda x: -x[1])[:5]

    loc_counts: dict[str, int] = {}
    for d in last30:
        for url, c in tracks["daily"].get(d, {}).get("locations", {}).items():
            loc_counts[url] = loc_counts.get(url, 0) + c
    top_locs = sorted(loc_counts.items(), key=lambda x: -x[1])[:10]

    url_to_name = {}
    for cat in catalog_data.values():
        for loc in cat.get("locations", []):
            if loc.get("url"):
                url_to_name[loc["url"]] = loc.get("name", "")

    lines = ["📊 <b>Statistika</b>", ""]
    lines.append("👥 <b>Foydalanuvchilar:</b>")
    lines.append(f"  Jami: <b>{total_users}</b>")
    lines.append(f"  Bugun: {today_users} · 7 kun: {week_users} · 30 kun: {month_users}")
    lines.append("")
    lines.append("📈 <b>Ochilishlar:</b>")
    lines.append(f"  Bugun: {today_opens} · 7 kun: {week_opens} · Jami: {total_opens}")
    lines.append("")

    if top_cats:
        lines.append("🔝 <b>Top kategoriyalar (30 kun):</b>")
        for i, (cat_id, c) in enumerate(top_cats, 1):
            name = catalog_data.get(cat_id, {}).get("name", cat_id)
            lines.append(f"  {i}. {html_escape(name)} — {c}")
        lines.append("")

    if top_locs:
        lines.append("🔝 <b>Top joylar (30 kun):</b>")
        for i, (url, c) in enumerate(top_locs, 1):
            name = url_to_name.get(url) or url[:40]
            lines.append(f"  {i}. {html_escape(name)} — {c}")
        lines.append("")

    lines.append(f"📩 <b>Aloqa xabarlari:</b> {tracks['contacts']}")

    await msg.answer("\n".join(lines))


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
        "Ikonani tanlang (yoki lucide-nomini yozing):",
        reply_markup=icons_keyboard("addcat_icon"),
    )
    await state.set_state(AddCategory.waiting_icon)


async def _finalize_add_category(message_target, state: FSMContext, icon: str) -> None:
    data = await state.get_data()
    name = data.get("name")
    if not name:
        await message_target.answer("Jarayon buzildi, qaytadan /cat_add")
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
    await message_target.answer(
        "✅ <b>Kategoriya qo'shildi</b>\n\n"
        f"📁 {html_escape(name)}\n"
        f"🎨 {html_escape(icon)}\n"
        f"ID: <code>{html_escape(cat_id)}</code>\n\n"
        "Endi /add bilan joy qo'shing."
    )
    await state.clear()


@dp.callback_query(F.data.startswith("addcat_icon:"), AddCategory.waiting_icon)
async def cat_icon_picked(cb: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(cb):
        await cb.answer()
        return
    icon = cb.data.split(":", 1)[1]
    if icon == "_skip" or not re.match(r"^[a-z0-9-]{1,40}$", icon):
        await cb.answer("Yaroqsiz", show_alert=True)
        return
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await _finalize_add_category(cb.message, state, icon)
    await cb.answer()


@dp.message(AddCategory.waiting_icon)
async def cat_icon_typed(msg: Message, state: FSMContext) -> None:
    if not is_admin(msg):
        return
    icon = (msg.text or "").strip().lower()
    if not re.match(r"^[a-z0-9-]{1,40}$", icon):
        await msg.answer("Ikona nomi noto'g'ri (lotin harf, raqam, defis). Qaytadan yozing yoki tugmadan tanlang:")
        return
    await _finalize_add_category(msg, state, icon)


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


# ----- /cat_icon flow (change icon of existing category) -----

@dp.message(Command("cat_icon"))
async def cmd_cat_icon(msg: Message, state: FSMContext) -> None:
    if not is_admin(msg):
        return
    await state.clear()
    if not catalog_data:
        await msg.answer("Catalog bo'sh")
        return
    builder = InlineKeyboardBuilder()
    for cat_id, cat in catalog_data.items():
        ico = cat.get("icon") or "—"
        builder.button(text=f"{cat['name']} · {ico}", callback_data=f"caticon_cat:{cat_id}")
    builder.adjust(1)
    await msg.answer(
        "Qaysi kategoriya ikonasini o'zgartiramiz?",
        reply_markup=builder.as_markup(),
    )
    await state.set_state(EditCategoryIcon.waiting_category)


@dp.callback_query(F.data.startswith("caticon_cat:"), EditCategoryIcon.waiting_category)
async def cat_icon_cat_chosen(cb: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(cb):
        await cb.answer()
        return
    cat_id = cb.data.split(":", 1)[1]
    if cat_id not in catalog_data:
        await cb.answer("Yo'q", show_alert=True)
        return
    await state.update_data(cat_id=cat_id)
    await cb.message.edit_text(
        f"📁 {html_escape(catalog_data[cat_id]['name'])}\n\n"
        "Yangi ikonani tanlang (yoki lucide-nomini yozing):",
        reply_markup=icons_keyboard("caticon_set"),
    )
    await state.set_state(EditCategoryIcon.waiting_icon)
    await cb.answer()


async def _apply_category_icon(message_target, state: FSMContext, icon: str) -> None:
    data = await state.get_data()
    cat_id = data.get("cat_id")
    if not cat_id or cat_id not in catalog_data:
        await message_target.answer("Jarayon buzildi, qaytadan /cat_icon")
        await state.clear()
        return
    catalog_data[cat_id]["icon"] = icon
    save_catalog(catalog_data)
    await message_target.answer(
        "✅ <b>Ikona o'zgartirildi</b>\n\n"
        f"📁 {html_escape(catalog_data[cat_id]['name'])}\n"
        f"🎨 {html_escape(icon)}"
    )
    await state.clear()


@dp.callback_query(F.data.startswith("caticon_set:"), EditCategoryIcon.waiting_icon)
async def cat_icon_set_picked(cb: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(cb):
        await cb.answer()
        return
    icon = cb.data.split(":", 1)[1]
    if icon == "_skip" or not re.match(r"^[a-z0-9-]{1,40}$", icon):
        await cb.answer("Yaroqsiz", show_alert=True)
        return
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await _apply_category_icon(cb.message, state, icon)
    await cb.answer()


@dp.message(EditCategoryIcon.waiting_icon)
async def cat_icon_set_typed(msg: Message, state: FSMContext) -> None:
    if not is_admin(msg):
        return
    icon = (msg.text or "").strip().lower()
    if not re.match(r"^[a-z0-9-]{1,40}$", icon):
        await msg.answer("Ikona noto'g'ri. Qaytadan:")
        return
    await _apply_category_icon(msg, state, icon)


# ----- /loc_icon flow (set/remove custom icon for a single location) -----

@dp.message(Command("loc_icon"))
async def cmd_loc_icon(msg: Message, state: FSMContext) -> None:
    if not is_admin(msg):
        return
    await state.clear()
    if not catalog_data:
        await msg.answer("Catalog bo'sh")
        return
    builder = InlineKeyboardBuilder()
    for cat_id, cat in catalog_data.items():
        count = len(cat.get("locations", []))
        if count == 0:
            continue
        builder.button(text=f"{cat['name']} ({count})", callback_data=f"locicon_cat:{cat_id}")
    builder.adjust(1)
    await msg.answer(
        "Qaysi kategoriyadagi joy ikonasini o'zgartiramiz?",
        reply_markup=builder.as_markup(),
    )
    await state.set_state(EditLocationIcon.waiting_category)


@dp.callback_query(F.data.startswith("locicon_cat:"), EditLocationIcon.waiting_category)
async def loc_icon_cat_chosen(cb: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(cb):
        await cb.answer()
        return
    cat_id = cb.data.split(":", 1)[1]
    if cat_id not in catalog_data:
        await cb.answer("Yo'q", show_alert=True)
        return
    locs = catalog_data[cat_id].get("locations", [])
    if not locs:
        await cb.answer("Bo'sh", show_alert=True)
        return
    await state.update_data(cat_id=cat_id)
    builder = InlineKeyboardBuilder()
    for i, loc in enumerate(locs):
        cur_icon = loc.get("icon") or "—"
        nm = loc["name"]
        if len(nm) > 26:
            nm = nm[:23] + "..."
        builder.button(text=f"{i+1}. {nm} · {cur_icon}", callback_data=f"locicon_loc:{cat_id}:{i}")
    builder.adjust(1)
    await cb.message.edit_text(
        f"📁 {html_escape(catalog_data[cat_id]['name'])}\n\nQaysi joy ikonasini o'zgartiramiz?",
        reply_markup=builder.as_markup(),
    )
    await state.set_state(EditLocationIcon.waiting_location)
    await cb.answer()


@dp.callback_query(F.data.startswith("locicon_loc:"), EditLocationIcon.waiting_location)
async def loc_icon_loc_chosen(cb: CallbackQuery, state: FSMContext) -> None:
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
        await cb.answer("Yo'q", show_alert=True)
        return
    locs = catalog_data[cat_id].get("locations", [])
    if idx < 0 or idx >= len(locs):
        await cb.answer("Yo'q", show_alert=True)
        return
    await state.update_data(cat_id=cat_id, idx=idx)
    cat_icon = catalog_data[cat_id].get("icon", "—")
    await cb.message.edit_text(
        f"📁 {html_escape(catalog_data[cat_id]['name'])}\n"
        f"📍 {html_escape(locs[idx]['name'])}\n\n"
        "Yangi ikonani tanlang yoki <i>kategoriya ikonasini</i> ishlatish uchun \"Yo'q\":",
        reply_markup=icons_keyboard("locicon_set", skip_label=f"⏭ Yo'q ({cat_icon} — kategoriya ikonasi)"),
    )
    await state.set_state(EditLocationIcon.waiting_icon)
    await cb.answer()


async def _apply_location_icon(message_target, state: FSMContext, icon: str | None) -> None:
    data = await state.get_data()
    cat_id = data.get("cat_id")
    idx = data.get("idx")
    if not cat_id or cat_id not in catalog_data or idx is None:
        await message_target.answer("Jarayon buzildi, qaytadan /loc_icon")
        await state.clear()
        return
    locs = catalog_data[cat_id].get("locations", [])
    if idx >= len(locs):
        await message_target.answer("Joy topilmadi, qaytadan /loc_icon")
        await state.clear()
        return
    if icon is None:
        locs[idx].pop("icon", None)
        msg_icon = f"{catalog_data[cat_id].get('icon', '—')} (kategoriya)"
    else:
        locs[idx]["icon"] = icon
        msg_icon = icon
    save_catalog(catalog_data)
    await message_target.answer(
        "✅ <b>Ikona o'zgartirildi</b>\n\n"
        f"📁 {html_escape(catalog_data[cat_id]['name'])}\n"
        f"📍 {html_escape(locs[idx]['name'])}\n"
        f"🎨 {html_escape(msg_icon)}"
    )
    await state.clear()


@dp.callback_query(F.data.startswith("locicon_set:"), EditLocationIcon.waiting_icon)
async def loc_icon_set_picked(cb: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(cb):
        await cb.answer()
        return
    icon = cb.data.split(":", 1)[1]
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    if icon == "_skip":
        await _apply_location_icon(cb.message, state, None)
    elif re.match(r"^[a-z0-9-]{1,40}$", icon):
        await _apply_location_icon(cb.message, state, icon)
    else:
        await cb.answer("Yaroqsiz", show_alert=True)
        return
    await cb.answer()


@dp.message(EditLocationIcon.waiting_icon)
async def loc_icon_set_typed(msg: Message, state: FSMContext) -> None:
    if not is_admin(msg):
        return
    text = (msg.text or "").strip().lower()
    if text in ("—", "-", "yoq", "yo'q", "skip"):
        await _apply_location_icon(msg, state, None)
        return
    if not re.match(r"^[a-z0-9-]{1,40}$", text):
        await msg.answer("Ikona noto'g'ri. Qaytadan yozing yoki tugmadan tanlang:")
        return
    await _apply_location_icon(msg, state, text)


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
    app.router.add_post("/api/track", handle_track)
    app.router.add_get("/api/prayer-info", handle_prayer_info)
    app.router.add_get("/api/prayer-today", handle_prayer_today)
    app.router.add_get("/health", handle_health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, BIND_HOST, PORT)
    await site.start()
    log.info("HTTP server listening on %s:%s", BIND_HOST, PORT)

    try:
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
