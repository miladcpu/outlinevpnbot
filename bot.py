import asyncio
import hashlib
import hmac
import html
import io
import json
import logging
import os
import re
import sqlite3
import time
from pathlib import Path
from urllib.parse import parse_qsl, quote, unquote

import aiohttp
from aiohttp import web
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    InputMediaPhoto,
    MenuButtonWebApp,
    WebAppInfo,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

# ───────────────────────── تنظیمات ─────────────────────────
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_IDS = {int(x) for x in os.environ["ADMIN_IDS"].split(",")}
OUTLINE_API_URL = os.environ["OUTLINE_API_URL"]  # مثلا https://IP:PORT/SECRET
OUTLINE_CERT_SHA256 = os.environ["OUTLINE_CERT_SHA256"]
CARD_INFO = os.environ.get("CARD_INFO", "6037-XXXX-XXXX-XXXX\nبه نام ...")

HOSTS = [h.strip() for h in os.environ.get("HOSTS", "").split(",") if h.strip()]
if not HOSTS:
    raise SystemExit("HOSTS خالی است؛ فایل .env را بررسی کن.")
PRICE_PER_GB = int(os.environ.get("PRICE_PER_GB", "5000"))  # قیمت هر گیگ (تومان)
PLAN_SIZES = [int(x) for x in os.environ.get("PLAN_SIZES", "20,40,60,80,100").split(",") if x.strip()]
ADDON_GB = [int(x) for x in os.environ.get("ADDON_SIZES", "10,20,40,60,100").split(",") if x.strip()]
PLANS = [(g, g * PRICE_PER_GB) for g in PLAN_SIZES]  # (حجم به گیگ, قیمت به تومان)
GRACE_DAYS = int(os.environ.get("GRACE_DAYS", "7"))  # مهلت تمدید بعد از انقضا؛ ۰ یعنی حذف فوری
PLAN_DAYS = 30
# ضریب حجم تانل: کاربر ۱۰ گیگ می‌خرد، روی اوتلاین ۱۰/ضریب گیگ ست می‌شود (پیش‌فرض ۲ → ۵ گیگ)
TRAFFIC_FACTOR = float(os.environ.get("TRAFFIC_FACTOR", "2"))

BTN_USAGE = "📊 حجم مصرفی"
BTN_SUPPORT = "💬 پشتیبانی"
BTN_APPS = "📲 دانلود اپلیکیشن"
BTN_DAILY = "📅 گزارش روزانه"
BTN_BROADCAST = "📢 ارسال همگانی"
BTN_MINIAPP = "🛍 Mini App"

# لینک‌های رسمی دانلود Outline Client (developer.getoutline.org/download-links)
APP_LINKS = [
    ("🤖 اندروید", [
        ("Google Play", "https://play.google.com/store/apps/details?id=org.outline.android.client"),
        ("دانلود مستقیم APK", "https://s3.amazonaws.com/outline-releases/client/android/stable/Outline-Client.apk"),
    ]),
    ("🍎 آیفون و آیپد", [
        ("App Store", "https://itunes.apple.com/us/app/outline-app/id1356177741"),
    ]),
    ("🪟 ویندوز", [
        ("دانلود نسخه x64", "https://s3.amazonaws.com/outline-releases/client/windows/stable/Outline-Client.exe"),
    ]),
    ("🐧 لینوکس", [
        ("راهنمای نصب", "https://support.getoutline.org/client/getting-started/install-linux/"),
        ("بسته deb (amd64)", "https://s3.amazonaws.com/outline-releases/client/linux/stable/outline-client_amd64.deb"),
    ]),
]
BTN_REPORT = "📊 گزارش فروش"
BTN_PENDING_RECEIPTS = "⏳ رسیدهای در انتظار"
BTN_REGS = "👥 درخواست‌های عضویت"
BTN_REJECTED = "🚫 رد صلاحیت‌ها"
ALERT_PERCENT = int(os.environ.get("ALERT_PERCENT", "90"))  # هشدار مصرف حجم
REMIND_DAYS = int(os.environ.get("REMIND_DAYS", "2"))  # یادآوری چند روز قبل از انقضا
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "").lstrip("@")  # یوزرنیم تلگرام ادمین، بدون @
DB_PATH = os.environ.get("DB_PATH", "bot.db")

# ───────────────────────── دیتابیس ─────────────────────────
db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
db.executescript(
    """
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, balance INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS topups(
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount INTEGER,
    status TEXT NOT NULL DEFAULT 'pending', created INTEGER);
CREATE TABLE IF NOT EXISTS configs(
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, key_id TEXT, gb INTEGER,
    access_url TEXT, expires INTEGER, active INTEGER NOT NULL DEFAULT 1);
"""
)
db.commit()


for _ddl in (
    "ALTER TABLE configs ADD COLUMN price INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE configs ADD COLUMN created INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE configs ADD COLUMN warned INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE configs ADD COLUMN reminded INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE topups ADD COLUMN file_id TEXT",
    "ALTER TABLE configs ADD COLUMN confirmed INTEGER NOT NULL DEFAULT 0",
    # کاربران قبلی تایید‌شده حساب می‌شوند؛ کاربران جدید با وضعیت new ثبت می‌شوند
    "ALTER TABLE users ADD COLUMN status TEXT NOT NULL DEFAULT 'approved'",
    "ALTER TABLE users ADD COLUMN note TEXT",
    "ALTER TABLE users ADD COLUMN requested INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE users ADD COLUMN name TEXT",
):
    try:
        db.execute(_ddl)
    except sqlite3.OperationalError:
        pass  # ستون از قبل وجود دارد
db.commit()


def ensure_user(uid: int):
    db.execute("INSERT OR IGNORE INTO users(id, status) VALUES(?, 'new')", (uid,))
    db.commit()


def balance(uid: int) -> int:
    ensure_user(uid)
    return db.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()["balance"]


# ───────────────────────── Outline API ─────────────────────────
class Outline:
    def __init__(self, url: str, fingerprint: str):
        self.url = url.rstrip("/")
        self.ssl = aiohttp.Fingerprint(bytes.fromhex(fingerprint.replace(":", "")))

    async def _req(self, method: str, path: str, **kw):
        async with aiohttp.ClientSession() as s:
            async with s.request(method, self.url + path, ssl=self.ssl, **kw) as r:
                r.raise_for_status()
                if r.content_type == "application/json":
                    return await r.json()
                return None

    async def block_key(self, key_id: str):
        await self._req("PUT", f"/access-keys/{key_id}/data-limit", json={"limit": {"bytes": 1}})

    async def set_limit(self, key_id: str, gb: float):
        await self._req(
            "PUT", f"/access-keys/{key_id}/data-limit",
            json={"limit": {"bytes": int(gb * 1024**3 / TRAFFIC_FACTOR)}},
        )

    async def list_keys(self) -> list:
        data = await self._req("GET", "/access-keys")
        return data["accessKeys"]

    async def rename_key(self, key_id: str, name: str):
        await self._req("PUT", f"/access-keys/{key_id}/name", data={"name": name})

    async def create_key(self, name: str, gb: int) -> dict:
        key = await self._req("POST", "/access-keys")
        kid = key["id"]
        await self._req("PUT", f"/access-keys/{kid}/name", data={"name": name})
        await self._req(
            "PUT", f"/access-keys/{kid}/data-limit", json={"limit": {"bytes": int(gb * 1024**3 / TRAFFIC_FACTOR)}}
        )
        return key

    async def get_usage(self) -> dict:
        data = await self._req("GET", "/metrics/transfer")
        return {str(k): int(v) for k, v in data["bytesTransferredByUserId"].items()}

    async def delete_key(self, key_id: str):
        await self._req("DELETE", f"/access-keys/{key_id}")


outline = Outline(OUTLINE_API_URL, OUTLINE_CERT_SHA256)


from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo

    _TEHRAN = ZoneInfo("Asia/Tehran")
except Exception:  # اگر tzdata نبود: ایران از ۱۴۰۱ ساعت تابستانی ندارد (UTC+3:30)
    _TEHRAN = timezone(timedelta(hours=3, minutes=30))


def _to_jalali(gy: int, gm: int, gd: int):
    g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    jy = 979 if gy > 1600 else 0
    gy -= 1600 if gy > 1600 else 621
    gy2 = gy + 1 if gm > 2 else gy
    days = 365 * gy + (gy2 + 3) // 4 - (gy2 + 99) // 100 + (gy2 + 399) // 400 - 80 + gd + g_d_m[gm - 1]
    jy += 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        return jy, 1 + days // 31, 1 + days % 31
    return jy, 7 + (days - 186) // 30, 1 + (days - 186) % 30


def stamp(ts: int) -> str:
    """تاریخ شمسی و ساعت تهران، مثل 1405/08/23 18:30:05"""
    d = datetime.fromtimestamp(ts, _TEHRAN)
    jy, jm, jd = _to_jalali(d.year, d.month, d.day)
    return f"{jy:04d}/{jm:02d}/{jd:02d} {d:%H:%M:%S}"


def clean_label(t: str) -> str:
    t = re.sub(r"[\x00-\x1f#]", " ", t or "")
    return re.sub(r"\s+", " ", t).strip()[:30]


def config_label(r) -> str:
    base = stamp(r["created"]) if r["created"] else f"config {r['id']}"
    try:
        lab = r["label"]
    except (IndexError, KeyError):
        lab = None
    return base + (f" {lab}" if lab else "")


def build_links(access_url: str, label: str) -> list[str]:
    base = access_url.split("#")[0]
    links = []
    for i, h in enumerate(HOSTS, start=1):
        name = label if len(HOSTS) == 1 else f"{label} ({i})"
        links.append(re.sub(r"@[^:/]+:", f"@{h}:", base) + "#" + quote(name))
    return links


# ───────────────────────── ابزارها ─────────────────────────
DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
router = Router()


def user_status(uid: int) -> str:
    if uid in ADMIN_IDS:
        return "approved"
    ensure_user(uid)
    return db.execute("SELECT status FROM users WHERE id=?", (uid,)).fetchone()["status"]


def is_member(uid: int) -> bool:
    return user_status(uid) == "approved"


async def membership_prompt(m: Message):
    st = user_status(m.from_user.id)
    if st == "approved":
        return
    if st == "pending":
        await m.answer("درخواست عضویتت در انتظار تایید ادمین است ⏳")
    elif st == "rejected":
        await m.answer("متاسفانه صلاحیتت تایید نشد. برای پیگیری از دکمه‌ی «💬 پشتیبانی» استفاده کن.")
    else:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="📝 درخواست عضویت", callback_data="reg:req")]]
        )
        await m.answer(
            "برای خرید و شارژ کیف پول، اول باید عضویتت تایید شود. برای شروع درخواست بده:",
            reply_markup=kb,
        )


class BuyName(StatesGroup):
    label = State()


class Register(StatesGroup):
    note = State()


class TopUp(StatesGroup):
    amount = State()
    receipt = State()


MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🛒 خرید کانفیگ"), KeyboardButton(text="💰 کیف پول")],
        [KeyboardButton(text="➕ شارژ کیف پول"), KeyboardButton(text="📂 کانفیگ‌های من")],
    ],
    resize_keyboard=True,
)


def menu_for(uid: int) -> ReplyKeyboardMarkup:
    labels = [b.text for row in MENU.keyboard for b in row]  # دکمه‌های اصلی
    labels += [BTN_USAGE, BTN_DAILY, BTN_APPS, BTN_SUPPORT]
    if uid in OWNER_IDS:
        labels += [BTN_REPORT, BTN_PENDING_RECEIPTS, BTN_REGS, BTN_REJECTED, BTN_BROADCAST]
    # هر دکمه یک ردیف کامل
    rows = [[KeyboardButton(text=t)] for t in labels]
    if WEBAPP_URL:  # دکمه‌ی مینی‌اپ بالای منو و شاخص
        rows.insert(0, [KeyboardButton(text=BTN_MINIAPP)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def fmt(n: int) -> str:
    return f"{n:,}"


def links_text(links: list[str]) -> str:
    return "\n\n".join(f"<code>{l}</code>" for l in links)


# ───────────────────────── هندلرها ─────────────────────────
TOPUP_CANCEL_MSG = "شارژ کیف پول لغو شد. اگر نیاز به شارژ داشتی، دوباره دکمه‌ی «➕ شارژ کیف پول» را بزن."


class MenuCancelMiddleware(BaseMiddleware):
    """اگر وسط یک فرآیند (شارژ، خرید، درخواست عضویت) دکمه‌ی دیگری از منو زده شد،
    فرآیند لغو می‌شود، پیام لغو می‌آید و همان دکمه کار خودش را انجام می‌دهد."""

    async def __call__(self, handler, event: Message, data):
        state = data.get("state")
        text = event.text or ""
        if state is not None and text and event.from_user and not text.startswith("/"):
            cur = await state.get_state()
            note = {
                TopUp.amount.state: TOPUP_CANCEL_MSG,
                TopUp.receipt.state: TOPUP_CANCEL_MSG,
                BuyName.label.state: "خرید لغو شد. اگر خواستی دوباره از «🛒 خرید کانفیگ» شروع کن.",
                Register.note.state: "درخواست عضویت لغو شد. هر وقت خواستی دوباره دکمه‌ی «📝 درخواست عضویت» را بزن.",
                Broadcast.text.state: "ارسال همگانی لغو شد.",
            }.get(cur)
            labels = {b.text for row in menu_for(event.from_user.id).keyboard for b in row}
            if note and text in labels and not text.startswith("➕") and text != BTN_BROADCAST:  # خود دکمه‌ی شارژ فرآیند را از اول شروع می‌کند
                await state.clear()
                await event.answer(note)
        return await handler(event, data)


router.message.outer_middleware(MenuCancelMiddleware())


@router.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    ensure_user(m.from_user.id)
    touch_name(m.from_user.id, m.from_user.first_name, m.from_user.last_name, m.from_user.username)
    await m.answer("سلام 👋 به YAL VPN خوش آمدی!\nاز منوی زیر انتخاب کن.", reply_markup=menu_for(m.from_user.id))
    row = db.execute("SELECT welcomed FROM users WHERE id=?", (m.from_user.id,)).fetchone()
    if WEBAPP_URL and row and not row["welcomed"]:
        db.execute("UPDATE users SET welcomed=1 WHERE id=?", (m.from_user.id,))
        db.commit()
        await m.answer(
            "🚀 <b>پیشنهاد جدی ما: با مینی‌اپ کار کن!</b>\n"
            "<b>کار با مینی‌اپ واقعاً راحت‌تره.</b> خرید و تمدید، دیدن حجم مصرفی، کپی لینک و QR کد، شارژ کیف پول و پیگیری رسیدها، همه یک‌جا و بدون این‌ور و اون‌ور رفتن توی چت.\n\n"
            "یک بار امتحانش کن؛ روی دکمه‌ی زیر بزن 👇",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="🛍 Mini App", web_app=WebAppInfo(url=WEBAPP_URL))]]
            ),
        )
    if not is_member(m.from_user.id):
        await membership_prompt(m)


@router.message(F.text == "💰 کیف پول")
async def wallet(m: Message):
    await m.answer(f"موجودی کیف پول: {fmt(balance(m.from_user.id))} تومان")


# --- شارژ کیف پول ---
@router.callback_query(F.data == "reg:req")
async def reg_request(c: CallbackQuery, state: FSMContext):
    st = user_status(c.from_user.id)
    if st != "new":
        names = {"pending": "در انتظار تایید", "approved": "تایید شده", "rejected": "رد شده"}
        await c.answer(f"وضعیت درخواستت: {names[st]}", show_alert=True)
        return
    await state.set_state(Register.note)
    await c.message.answer(
        "یک پیام کوتاه برای ادمین بنویس (مثلا اینکه از کجا با ما آشنا شدی یا چه کسی معرفی کرده):"
    )
    await c.answer()


def reg_card(uid: int, mode: str = "pending"):
    r = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    when = stamp(r["requested"] or 0)[:16]
    text = (
        f"کاربر: {html.escape(r['name'] or '-')} (<code>{uid}</code>)\n"
        f"زمان درخواست: {when}\n"
        f"پیام: {html.escape(r['note'] or '-')}"
    )
    if mode == "pending":
        rows = [
            [InlineKeyboardButton(text="✅ تایید", callback_data=f"rg:ok:{uid}")],
            [InlineKeyboardButton(text="❌ رد", callback_data=f"rg:no:{uid}")],
        ]
    else:
        rows = [
            [InlineKeyboardButton(text="✅ تایید صلاحیت", callback_data=f"rg:ok:{uid}")],
            [InlineKeyboardButton(text="🗑 حذف از لیست", callback_data=f"rg:rm:{uid}")],
        ]
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Register.note, F.text)
async def reg_note(m: Message, state: FSMContext, bot: Bot):
    uid = m.from_user.id
    await state.clear()
    if user_status(uid) != "new":
        return
    await submit_registration(bot, uid, m.from_user.full_name, m.from_user.username, m.text)
    await m.answer("درخواستت برای ادمین ارسال شد ✅ بعد از بررسی خبرت می‌کنم.")


@router.message(Register.note)
async def reg_note_invalid(m: Message):
    await m.answer("لطفا یک پیام متنی بفرست.")


@router.callback_query(F.data.startswith("rg:"))
async def reg_decide(c: CallbackQuery, bot: Bot):
    if c.from_user.id not in OWNER_IDS:
        await c.answer("دسترسی نداری", show_alert=True)
        return
    _, action, uid = c.data.split(":")
    uid = int(uid)
    row = db.execute("SELECT status FROM users WHERE id=?", (uid,)).fetchone()
    if not row:
        await c.answer("کاربر پیدا نشد", show_alert=True)
        return
    cur = row["status"]
    if action == "ok":
        allowed, new, label = cur in ("pending", "rejected"), "approved", "✅ تایید شد"
    elif action == "no":
        allowed, new, label = cur == "pending", "rejected", "❌ رد شد"
    else:
        allowed, new, label = cur == "rejected", "new", "🗑 از لیست رد شده‌ها حذف شد"
    if not allowed:
        await c.answer("این درخواست قبلا بررسی شده یا وضعیتش عوض شده", show_alert=True)
        return
    db.execute("UPDATE users SET status=? WHERE id=?", (new, uid))
    db.commit()
    try:
        await c.message.edit_text((c.message.html_text or "") + f"\n\n{label}", reply_markup=None)
    except Exception:
        logging.exception("edit registration message failed")
    msgs = {
        "approved": "عضویتت تایید شد ✅ حالا می‌توانی کیف پولت را شارژ کنی و کانفیگ بخری.",
        "rejected": "متاسفانه صلاحیتت تایید نشد ❌ برای پیگیری از دکمه‌ی «💬 پشتیبانی» استفاده کن.",
        "new": "ادمین درخواستت را از لیست رد شده‌ها حذف کرد؛ می‌توانی دوباره درخواست عضویت بدهی.",
    }
    try:
        await bot.send_message(uid, msgs[new])
    except Exception:
        logging.exception("notify user (registration) failed")
    await c.answer()


async def send_reg_list(bot: Bot, chat_id: int, status: str):
    rows = db.execute(
        "SELECT id FROM users WHERE status=? ORDER BY requested", (status,)
    ).fetchall()
    if not rows:
        await bot.send_message(chat_id, "موردی نیست ✅")
        return
    mode = "pending" if status == "pending" else "rejected"
    for r in rows:
        text, kb = reg_card(r["id"], mode)
        await bot.send_message(chat_id, text, reply_markup=kb)


@router.message(F.text == BTN_REGS)
async def regs_button(m: Message, bot: Bot):
    if m.from_user.id in OWNER_IDS:
        await send_reg_list(bot, m.chat.id, "pending")


@router.message(F.text == BTN_REJECTED)
async def rejected_button(m: Message, bot: Bot):
    if m.from_user.id in OWNER_IDS:
        await send_reg_list(bot, m.chat.id, "rejected")


@router.message(F.text == "➕ شارژ کیف پول")
async def topup_start(m: Message, state: FSMContext):
    if not is_member(m.from_user.id):
        await membership_prompt(m)
        return
    await state.set_state(TopUp.amount)
    await m.answer("مبلغ شارژ را به تومان بفرست (فقط عدد):")


async def _topup_strike(m: Message, state: FSMContext, hint: str):
    """ورودی اشتباه: بار اول راهنما، بار دوم لغو فرآیند شارژ."""
    strikes = (await state.get_data()).get("bad", 0) + 1
    if strikes >= 2:
        await state.clear()
        await m.answer(TOPUP_CANCEL_MSG)
        return
    await state.update_data(bad=strikes)
    await m.answer(hint)


@router.message(TopUp.amount, F.text)
async def topup_amount(m: Message, state: FSMContext):
    txt = m.text.translate(DIGITS).replace(",", "").strip()
    if not txt.isdigit() or int(txt) < MIN_TOPUP:
        await _topup_strike(m, state, "مبلغ نامعتبر است. حداقل ۱۰,۰۰۰ تومان، فقط عدد بفرست:")
        return
    await state.update_data(amount=int(txt), bad=0)
    await state.set_state(TopUp.receipt)
    await m.answer(
        f"مبلغ {fmt(int(txt))} تومان را به کارت زیر واریز کن:\n\n"
        f"<code>{CARD_INFO}</code>\n\nسپس عکس فیش را همین‌جا بفرست."
    )


@router.message(TopUp.amount)
async def topup_amount_other(m: Message, state: FSMContext):
    await _topup_strike(m, state, "مبلغ نامعتبر است. حداقل ۱۰,۰۰۰ تومان، فقط عدد بفرست:")


@router.message(TopUp.receipt, F.photo)
async def topup_receipt(m: Message, state: FSMContext, bot: Bot):
    if not is_member(m.from_user.id):
        await state.clear()
        await membership_prompt(m)
        return
    data = await state.get_data()
    amount = data["amount"]
    cur = db.execute(
        "INSERT INTO topups(user_id, amount, created, file_id) VALUES(?,?,?,?)",
        (m.from_user.id, amount, int(time.time()), m.photo[-1].file_id),
    )
    db.commit()
    tid = cur.lastrowid
    await notify_admins_topup(
        bot, tid, m.from_user.id, m.from_user.full_name, amount, m.photo[-1].file_id
    )
    await state.clear()
    await m.answer("فیش دریافت شد ✅ بعد از تایید مدیر، کیف پولت شارژ می‌شود.")


@router.message(TopUp.receipt)
async def topup_receipt_invalid(m: Message, state: FSMContext):
    await _topup_strike(m, state, "لطفا عکس فیش را بفرست.")


@router.callback_query(F.data.startswith("tx:"))
async def topup_decide(c: CallbackQuery, bot: Bot):
    if c.from_user.id not in OWNER_IDS:
        await c.answer("دسترسی نداری", show_alert=True)
        return
    _, action, tid = c.data.split(":")
    tid = int(tid)
    status = "approved" if action == "ok" else "rejected"
    cur = db.execute(
        "UPDATE topups SET status=? WHERE id=? AND status='pending'", (status, tid)
    )
    if cur.rowcount == 0:
        db.commit()
        await c.answer("این درخواست قبلا بررسی شده", show_alert=True)
        return
    row = db.execute("SELECT user_id, amount FROM topups WHERE id=?", (tid,)).fetchone()
    if status == "approved":
        db.execute(
            "UPDATE users SET balance=balance+? WHERE id=?", (row["amount"], row["user_id"])
        )
    db.commit()
    label = "✅ تایید شد" if status == "approved" else "❌ رد شد"
    try:
        if c.message.photo:
            await c.message.edit_caption(caption=(c.message.caption or "") + f"\n\n{label}")
        else:
            await c.message.edit_text((c.message.text or "") + f"\n\n{label}")
    except Exception:
        logging.exception("edit admin message failed")
    try:
        if status == "approved":
            await bot.send_message(
                row["user_id"],
                f"شارژ {fmt(row['amount'])} تومانی تایید شد ✅\n"
                f"موجودی: {fmt(balance(row['user_id']))} تومان",
            )
        else:
            await bot.send_message(row["user_id"], "درخواست شارژ شما رد شد ❌")
    except Exception:
        logging.exception("notify user failed")
    await c.answer()


# --- خرید ---
@router.message(F.text == "🛒 خرید کانفیگ")
async def buy_menu(m: Message):
    if not is_member(m.from_user.id):
        await membership_prompt(m)
        return
    rows = [
        [
            InlineKeyboardButton(
                text=f"{gb} گیگ - {fmt(price)} تومان", callback_data=f"buy:{gb}"
            )
        ]
        for gb, price in PLANS
    ]
    await m.answer(
        f"پلن‌ها ({PLAN_DAYS} روزه). یکی را انتخاب کن:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("buy:"))
async def buy(c: CallbackQuery, state: FSMContext):
    uid = c.from_user.id
    if not is_member(uid):
        await c.answer("اول باید عضویتت تایید شود.", show_alert=True)
        return
    gb = int(c.data.split(":")[1])
    price = dict(PLANS).get(gb)
    if price is None:
        await c.answer("پلن نامعتبر")
        return
    if balance(uid) < price:
        await c.answer("موجودی کافی نیست. اول کیف پول را شارژ کن.", show_alert=True)
        return
    await state.set_state(BuyName.label)
    await state.update_data(gb=gb)
    await c.answer()
    await c.message.answer("یک اسم برای این اکانت بفرست (مثلا اسم خودت یا دستگاه، حداکثر ۳۰ حرف):")


@router.message(BuyName.label, F.text)
async def buy_label(m: Message, state: FSMContext):
    uid = m.from_user.id
    data = await state.get_data()
    await state.clear()
    if m.text in {b.text for row in menu_for(uid).keyboard for b in row}:
        await m.answer("خرید لغو شد.")
        return
    gb, label = data.get("gb"), clean_label(m.text)
    if not gb or not label or not is_member(uid):
        await m.answer("اسم نامعتبر است. دوباره از منوی خرید شروع کن.")
        return
    try:
        res = await purchase(m.bot, uid, m.from_user.full_name, gb, label)
    except PurchaseError as e:
        await m.answer(str(e))
        return
    await m.answer(
        f"کانفیگ {gb} گیگ ({PLAN_DAYS} روزه) آماده شد ✅\n"
        f"لینک(ها) را در Outline اضافه کن؛ اگر چند لینک هست و یکی کار نکرد، از بعدی استفاده کن:\n\n"
        + links_text(res["links"])
    )
    await send_link_qrs(m.bot, m.chat.id, res["links"])
    await report_delivery(m.bot, uid, m.from_user.full_name, res)


@router.message(F.text == "📂 کانفیگ‌های من")
async def my_configs(m: Message):
    rows = db.execute(
        "SELECT * FROM configs WHERE user_id=? AND active IN (1,2) ORDER BY active, id DESC",
        (m.from_user.id,),
    ).fetchall()
    if not rows:
        await m.answer("کانفیگ فعالی نداری.")
        return
    now = int(time.time())
    for r in rows:
        renew_btn = [InlineKeyboardButton(text=f"🔄 تمدید {PLAN_DAYS} روزه", callback_data=f"rn:{r['id']}")]
        if r["active"] == 2:
            left = max(0, (r["expires"] + GRACE_DAYS * 86400 - now) // 86400)
            await m.answer(
                f"⛔ {config_label(r)}\nمنقضی شده؛ تا {left} روز دیگر می‌توانی با همان لینک تمدیدش کنی.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[renew_btn]),
            )
            continue
        days = max(0, (r["expires"] - now) // 86400)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[renew_btn, [InlineKeyboardButton(text="➕ افزایش حجم", callback_data=f"ad:{r['id']}")]]
        )
        await m.answer(
            f"{r['gb']} گیگ | {days} روز مانده\n\n" + links_text(build_links(r["access_url"], config_label(r))),
            reply_markup=kb,
        )


@router.callback_query(F.data.startswith("rn:"))
async def renew_menu(c: CallbackQuery):
    cid = int(c.data.split(":")[1])
    rows = [
        [InlineKeyboardButton(text=f"{gb} گیگ - {fmt(price)} تومان", callback_data=f"rnp:{cid}:{gb}")]
        for gb, price in PLANS
    ]
    await c.answer()
    await c.message.answer(
        f"تمدید {PLAN_DAYS} روزه با کدام حجم؟ حجم باقی‌مانده‌ی فعلی‌ات حفظ می‌شود و حجم پلن به آن اضافه می‌شود. لینک همان می‌ماند.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("ad:"))
async def addon_menu(c: CallbackQuery):
    cid = int(c.data.split(":")[1])
    rows = [
        [InlineKeyboardButton(text=f"+{gb} گیگ - {fmt(gb * PRICE_PER_GB)} تومان", callback_data=f"adp:{cid}:{gb}")]
        for gb in ADDON_GB
    ]
    await c.answer()
    await c.message.answer("چند گیگ به این اکانت اضافه شود؟", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


async def _order_bot(c: CallbackQuery, kind: str):
    _, cid, gb = c.data.split(":")
    uid = c.from_user.id
    try:  # جلوی دوبار زدن
        await c.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    if not is_member(uid):
        await c.answer("اول باید عضویتت تایید شود.", show_alert=True)
        return
    await c.answer("در حال انجام...")
    try:
        res = await (renew_config if kind == "renew" else add_volume)(uid, int(cid), int(gb))
    except PurchaseError as e:
        await c.message.answer(str(e))
        return
    await c.message.answer(
        ("✅ تمدید انجام شد." if kind == "renew" else "✅ حجم اضافه شد.")
        + f"\nحجم کل: {res['gb']:g} گیگ | انقضا: {stamp(res['expires'])[:16]}\nموجودی کیف پول: {fmt(balance(uid))} تومان"
    )
    await notify_order(c.bot, uid, c.from_user.full_name, res)


@router.callback_query(F.data.startswith("rnp:"))
async def renew_do(c: CallbackQuery):
    await _order_bot(c, "renew")


@router.callback_query(F.data.startswith("adp:"))
async def addon_do(c: CallbackQuery):
    await _order_bot(c, "addon")



# ───────────────────────── انقضا ─────────────────────────
GIB = 1024**3


@router.message(F.text == BTN_USAGE)
async def usage(m: Message):
    rows = db.execute(
        "SELECT * FROM configs WHERE user_id=? AND active=1 ORDER BY id DESC",
        (m.from_user.id,),
    ).fetchall()
    if not rows:
        await m.answer("کانفیگ فعالی نداری.")
        return
    try:
        used_by_key = await outline.get_usage()
    except Exception:
        logging.exception("usage fetch failed")
        await m.answer("الان امکان دریافت مصرف نیست، کمی بعد دوباره امتحان کن.")
        return
    for r in rows:
        total = r["gb"] * GIB
        # مصرف واقعی روی اوتلاین × ضریب = مصرف نمایشی نسبت به حجم خریداری‌شده
        used = min(used_by_key.get(r["key_id"], 0) * TRAFFIC_FACTOR, total)
        pct = int(used * 100 / total)
        filled = pct // 10
        days = max(0, (r["expires"] - int(time.time())) // 86400)
        await m.answer(
            f"📦 پلن {r['gb']} گیگ | {days} روز مانده\n"
            f"{'▓' * filled}{'░' * (10 - filled)} {pct}%\n"
            f"مصرف‌شده: {used / GIB:.2f} گیگ\n"
            f"باقی‌مانده: {(total - used) / GIB:.2f} گیگ"
        )


def rp_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ بله، هر شب برام بفرست", callback_data="rp:1")],
            [InlineKeyboardButton(text="❌ نه، نفرست", callback_data="rp:0")],
        ]
    )


@router.message(F.text == BTN_DAILY)
async def daily_menu(m: Message):
    ensure_user(m.from_user.id)
    pref = db.execute("SELECT report_pref FROM users WHERE id=?", (m.from_user.id,)).fetchone()["report_pref"]
    await m.answer(
        "گزارش مصرف ۲۴ ساعت گذشته‌ات هر شب ساعت ۰۰:۰۰ (به وقت تهران) داخل ربات ارسال می‌شود.\n"
        f"وضعیت فعلی: {'فعال' if pref == 1 else 'غیرفعال'}\n\nمی‌خواهی ارسال بشود؟",
        reply_markup=rp_kb(),
    )


@router.callback_query(F.data.startswith("rp:"))
async def daily_pref(c: CallbackQuery):
    v = 1 if c.data == "rp:1" else 0
    ensure_user(c.from_user.id)
    db.execute("UPDATE users SET report_pref=? WHERE id=?", (v, c.from_user.id))
    db.commit()
    await c.answer("ثبت شد")
    try:
        await c.message.edit_text(
            "✅ از شب بعد، هر شب گزارش مصرفت ارسال می‌شود." if v else "گزارش روزانه غیرفعال شد.",
            reply_markup=None,
        )
    except Exception:
        logging.exception("edit report pref message failed")


class Broadcast(StatesGroup):
    text = State()


AUD_FA = {"all": "همه‌ی کاربران", "act": "دارای اکانت فعال", "noact": "بدون اکانت فعال"}
BG_TASKS: set = set()


def broadcast_audience(aud: str) -> list:
    active = {r["user_id"] for r in db.execute("SELECT DISTINCT user_id FROM configs WHERE active=1")}
    ids = [r["id"] for r in db.execute("SELECT id FROM users WHERE status='approved'") if r["id"] not in ADMIN_IDS]
    if aud == "act":
        return [i for i in ids if i in active]
    if aud == "noact":
        return [i for i in ids if i not in active]
    return ids


async def _broadcast_send(bot: Bot, owner_id: int, ids: list, text: str, photo):
    from aiogram.exceptions import TelegramRetryAfter

    ok = fail = 0
    for uid in ids:
        for _ in range(2):
            try:
                if photo:
                    await bot.send_photo(uid, photo, caption=text or None)
                else:
                    await bot.send_message(uid, text)
                ok += 1
                break
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except Exception:
                fail += 1
                break
        else:
            fail += 1
        await asyncio.sleep(0.06)
    try:
        await bot.send_message(
            owner_id, f"📢 ارسال همگانی تمام شد.\nموفق: {ok} | ناموفق: {fail} (معمولا یعنی ربات را بلاک کرده‌اند)"
        )
    except Exception:
        logging.exception("broadcast summary failed")


@router.message(F.text == BTN_BROADCAST)
@router.message(Command("broadcast"))
async def bc_start(m: Message, state: FSMContext):
    if m.from_user.id not in OWNER_IDS:
        return
    await state.set_state(Broadcast.text)
    await m.answer(
        "متن پیام همگانی را بفرست. می‌توانی عکس همراه کپشن هم بفرستی؛ فرمت‌بندی تلگرام (مثل بولد) حفظ می‌شود.\n"
        "برای لغو، یکی از دکمه‌های منو را بزن."
    )


@router.message(Broadcast.text, F.text | F.photo)
async def bc_content(m: Message, state: FSMContext):
    if m.from_user.id not in OWNER_IDS:
        return
    if m.photo:
        text = m.html_text if m.caption else ""
        if len(text) > 1000:
            await m.answer("کپشن عکس بیشتر از ۱۰۰۰ کاراکتر است؛ کوتاه‌ترش کن.")
            return
        await state.update_data(text=text, photo=m.photo[-1].file_id)
    else:
        if len(m.text) > 4000:
            await m.answer("پیام بیشتر از ۴۰۰۰ کاراکتر است؛ کوتاه‌ترش کن.")
            return
        await state.update_data(text=m.html_text, photo=None)
    rows = [
        [InlineKeyboardButton(text=f"{AUD_FA[a]} ({len(broadcast_audience(a))})", callback_data=f"bca:{a}")]
        for a in AUD_FA
    ]
    rows.append([InlineKeyboardButton(text="❌ لغو", callback_data="bcx")])
    await m.answer("پیام ذخیره شد. برای چه کسانی ارسال شود؟", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.message(Broadcast.text)
async def bc_other(m: Message):
    await m.answer("فقط متن، یا عکس همراه کپشن بفرست.")


@router.callback_query(F.data.startswith("bca:"))
async def bc_audience(c: CallbackQuery, state: FSMContext):
    if c.from_user.id not in OWNER_IDS:
        await c.answer("دسترسی نداری", show_alert=True)
        return
    aud = c.data.split(":")[1]
    if aud not in AUD_FA or "text" not in await state.get_data():
        await c.answer("پیام پیدا نشد؛ دوباره شروع کن.", show_alert=True)
        return
    n = len(broadcast_audience(aud))
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"✅ ارسال به {n} نفر", callback_data=f"bcg:{aud}")],
            [InlineKeyboardButton(text="❌ لغو", callback_data="bcx")],
        ]
    )
    await c.answer()
    await c.message.edit_text(
        f"پیام برای «{AUD_FA[aud]}» ({n} نفر) ارسال شود؟ این کار برگشت‌پذیر نیست.", reply_markup=kb
    )


@router.callback_query(F.data.startswith("bcg:"))
async def bc_go(c: CallbackQuery, state: FSMContext):
    if c.from_user.id not in OWNER_IDS:
        await c.answer("دسترسی نداری", show_alert=True)
        return
    aud = c.data.split(":")[1]
    data = await state.get_data()
    await state.clear()
    if aud not in AUD_FA or "text" not in data:
        await c.answer("پیام پیدا نشد؛ دوباره شروع کن.", show_alert=True)
        return
    ids = broadcast_audience(aud)
    await c.answer()
    await c.message.edit_text(f"در حال ارسال به {len(ids)} نفر… بعد از پایان خلاصه‌اش را می‌فرستم.", reply_markup=None)
    task = asyncio.create_task(_broadcast_send(c.bot, c.from_user.id, ids, data["text"], data.get("photo")))
    BG_TASKS.add(task)
    task.add_done_callback(BG_TASKS.discard)
    log_admin(c.from_user.id, "broadcast", None, None, f"{AUD_FA[aud]}: {len(ids)}")


@router.callback_query(F.data == "bcx")
async def bc_cancel(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.answer()
    try:
        await c.message.edit_text("ارسال همگانی لغو شد.", reply_markup=None)
    except Exception:
        pass


@router.message(F.text == BTN_MINIAPP)
async def open_miniapp(m: Message):
    """دکمه‌ی کیبورد داده‌ی ورود (initData) نمی‌دهد؛ پس دکمه‌ی شیشه‌ای وب‌اپ می‌فرستیم."""
    if not WEBAPP_URL:
        await m.answer("مینی‌اپ فعلا فعال نیست.")
        return
    await m.answer(
        "برای باز شدن مینی‌اپ روی دکمه‌ی زیر بزن 👇",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🛍 Mini App", web_app=WebAppInfo(url=WEBAPP_URL))]]
        ),
    )


@router.message(F.text == BTN_APPS)
async def apps_menu(m: Message):
    rows = [
        [InlineKeyboardButton(text=f"{name} - {label}", url=url)]
        for name, links in APP_LINKS
        for label, url in links
    ]
    await m.answer(
        "اپ Outline را برای دستگاهت دانلود کن، بعد لینک کانفیگ را داخلش اضافه کن:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.message(F.text == BTN_SUPPORT)
async def support(m: Message):
    if not SUPPORT_USERNAME:
        await m.answer("پشتیبانی هنوز تنظیم نشده است.")
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💬 ارتباط با پشتیبانی", url=f"https://t.me/{SUPPORT_USERNAME}")]
        ]
    )
    await m.answer("برای هر مشکلی روی دکمه‌ی زیر بزن تا به پشتیبانی وصل شوی:", reply_markup=kb)


def period_stats(since: int):
    s = db.execute(
        "SELECT COUNT(*) AS c, COALESCE(SUM(price),0) AS t FROM configs WHERE price>0 AND created>=?",
        (since,),
    ).fetchone()
    o = db.execute("SELECT COALESCE(SUM(price),0) AS t FROM orders WHERE created>=?", (since,)).fetchone()
    t = db.execute(
        "SELECT COALESCE(SUM(amount),0) AS t FROM topups WHERE status='approved' AND created>=?",
        (since,),
    ).fetchone()
    return s["c"], s["t"] + o["t"], t["t"]


@router.message(Command("report"))
@router.message(F.text == BTN_REPORT)
async def report(m: Message):
    if m.from_user.id not in OWNER_IDS:
        return
    now = int(time.time())
    lines = ["📊 گزارش فروش", ""]
    for label, since in (
        ("۲۴ ساعت اخیر", now - 86400),
        ("۷ روز اخیر", now - 7 * 86400),
        ("۳۰ روز اخیر", now - 30 * 86400),
        ("کل", 0),
    ):
        n, sales, topup = period_stats(since)
        lines.append(f"{label}: {n} فروش | {fmt(sales)} تومان | شارژ تاییدشده: {fmt(topup)}")
    users = db.execute(
        "SELECT COUNT(*) AS c, COALESCE(SUM(balance),0) AS b FROM users"
    ).fetchone()
    active = db.execute("SELECT COUNT(*) AS c FROM configs WHERE active=1").fetchone()["c"]
    pending = db.execute("SELECT COUNT(*) AS c FROM topups WHERE status='pending'").fetchone()["c"]
    lines += [
        "",
        f"👥 کاربران: {users['c']}",
        f"📂 کانفیگ فعال: {active}",
        f"💰 مجموع موجودی کیف پول‌ها: {fmt(users['b'])} تومان",
        f"⏳ شارژ در انتظار تایید: {pending}",
    ]
    await m.answer("\n".join(lines))


async def expiry_loop(bot: Bot):
    while True:
        try:
            now = int(time.time())
            # ۱) منقضی‌شده: با سقف ۱ بایت مسدود می‌شود تا لینک بماند و قابل تمدید باشد
            for r in db.execute("SELECT * FROM configs WHERE active=1 AND expires<?", (now,)).fetchall():
                try:
                    if GRACE_DAYS > 0:
                        await outline.block_key(r["key_id"])
                    else:
                        await _remove_key(r["key_id"])
                except Exception:
                    logging.exception("expire config failed")
                    continue
                db.execute("UPDATE configs SET active=? WHERE id=?", (2 if GRACE_DAYS > 0 else 0, r["id"]))
                db.commit()
                msg = (
                    f"⛔ اکانت {config_label(r)} منقضی شد. تا {GRACE_DAYS} روز دیگر می‌توانی از «📂 کانفیگ‌های من» با همان لینک تمدیدش کنی."
                    if GRACE_DAYS > 0
                    else f"اکانت {config_label(r)} تمام شد. برای ادامه، خرید جدید بزن."
                )
                try:
                    await bot.send_message(r["user_id"], msg)
                except Exception:
                    pass
            # ۲) بعد از پایان مهلت: حذف کامل کلید
            for r in db.execute(
                "SELECT * FROM configs WHERE active=2 AND expires<?", (now - GRACE_DAYS * 86400,)
            ).fetchall():
                try:
                    await _remove_key(r["key_id"])
                except Exception:
                    logging.exception("delete expired config failed")
                    continue
                db.execute("UPDATE configs SET active=0 WHERE id=?", (r["id"],))
                db.commit()
                try:
                    await bot.send_message(
                        r["user_id"], f"اکانت {config_label(r)} بعد از پایان مهلت تمدید حذف شد. برای ادامه، خرید جدید بزن."
                    )
                except Exception:
                    pass
        except Exception:
            logging.exception("expiry loop error")
        await asyncio.sleep(300)


def report_kb(cfg_id: int, uid: int, confirmed: bool = False) -> InlineKeyboardMarkup:
    rows = []
    if not confirmed:
        rows.append([InlineKeyboardButton(text="✅ تایید کانفیگ", callback_data=f"adm:cfgconf:{cfg_id}")])
    rows.append([InlineKeyboardButton(text="🗑 حذف کانفیگ", callback_data=f"adm:cfg:{cfg_id}")])
    rows.append([InlineKeyboardButton(text="🚫 حذف کاربر", callback_data=f"adm:usr:{uid}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _remove_key(key_id: str):
    try:
        await outline.delete_key(key_id)
    except aiohttp.ClientResponseError as e:
        if e.status != 404:  # 404 = قبلا حذف شده
            raise


async def delete_config(cfg_id: int) -> str:
    r = db.execute("SELECT * FROM configs WHERE id=?", (cfg_id,)).fetchone()
    if not r:
        return "کانفیگ پیدا نشد."
    if not r["active"]:
        return f"کانفیگ #{cfg_id} از قبل غیرفعال بود."
    await _remove_key(r["key_id"])
    db.execute("UPDATE configs SET active=0 WHERE id=?", (cfg_id,))
    db.commit()
    return f"✅ کانفیگ #{cfg_id} ({r['gb']} گیگ) از اوتلاین حذف شد."


async def delete_user(uid: int) -> str:
    rows = db.execute("SELECT * FROM configs WHERE user_id=? AND active IN (1,2)", (uid,)).fetchall()
    failed = 0
    for r in rows:
        try:
            await _remove_key(r["key_id"])
        except Exception:
            logging.exception("delete key failed")
            failed += 1
            continue
        db.execute("UPDATE configs SET active=0 WHERE id=?", (r["id"],))
        db.commit()
    if failed:
        return f"⚠️ {failed} کانفیگ حذف نشد (خطا در اوتلاین). کاربر حذف نشد، دوباره تلاش کن."
    bal = balance(uid)
    db.execute("DELETE FROM users WHERE id=?", (uid,))
    db.commit()
    return (
        f"✅ کاربر <code>{uid}</code> و {len(rows)} کانفیگ فعالش حذف شد "
        f"(موجودی {fmt(bal)} تومان هم صفر شد)."
    )


def cfg_confirm(cfg_id: int):
    r = db.execute("SELECT * FROM configs WHERE id=?", (cfg_id,)).fetchone()
    if not r:
        return "کانفیگ پیدا نشد.", None
    state = "فعال" if r["active"] else "غیرفعال"
    text = (
        f"حذف کانفیگ #{cfg_id}؟\n"
        f"کاربر: <code>{r['user_id']}</code> | پلن {r['gb']} گیگ | {state}\n"
        "کلید از سرور اوتلاین حذف می‌شود."
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ بله، حذف کن", callback_data=f"adm:cfgok:{cfg_id}")],
            [InlineKeyboardButton(text="انصراف", callback_data="adm:no")]
        ]
    )
    return text, kb


def usr_confirm(uid: int):
    n = db.execute(
        "SELECT COUNT(*) AS c FROM configs WHERE user_id=? AND active=1", (uid,)
    ).fetchone()["c"]
    row = db.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()
    if not row and n == 0:
        return "کاربر پیدا نشد.", None
    text = (
        f"حذف کاربر <code>{uid}</code>؟\n"
        f"کانفیگ فعال: {n}\n"
        f"موجودی کیف پول: {fmt(row['balance'] if row else 0)} تومان\n"
        "همه‌ی کانفیگ‌های فعالش از اوتلاین حذف و موجودی صفر می‌شود."
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ بله، حذف کن", callback_data=f"adm:usrok:{uid}")],
            [InlineKeyboardButton(text="انصراف", callback_data="adm:no")]
        ]
    )
    return text, kb


@router.message(Command("delconfig"))
async def cmd_delconfig(m: Message):
    if m.from_user.id not in OWNER_IDS:
        return
    parts = (m.text or "").split()
    arg = parts[1].lstrip("#") if len(parts) == 2 else ""
    if not arg.isdigit():
        await m.answer("استفاده: /delconfig شناسه_کانفیگ\nمثال: /delconfig 12")
        return
    text, kb = cfg_confirm(int(arg))
    await m.answer(text, reply_markup=kb)


@router.message(Command("deluser"))
async def cmd_deluser(m: Message):
    if m.from_user.id not in OWNER_IDS:
        return
    parts = (m.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await m.answer("استفاده: /deluser آیدی_عددی_کاربر\nمثال: /deluser 123456789")
        return
    text, kb = usr_confirm(int(parts[1]))
    await m.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("adm:"))
async def admin_actions(c: CallbackQuery):
    if c.from_user.id not in OWNER_IDS:
        await c.answer("دسترسی نداری", show_alert=True)
        return
    parts = c.data.split(":")
    action = parts[1]
    if action == "no":
        await c.message.edit_text("لغو شد.", reply_markup=None)
        await c.answer()
        return
    arg = int(parts[2])
    if action == "cfg":
        text, kb = cfg_confirm(arg)
        await c.message.answer(text, reply_markup=kb)
    elif action == "usr":
        text, kb = usr_confirm(arg)
        await c.message.answer(text, reply_markup=kb)
    elif action == "cfgconf":
        r = db.execute("SELECT user_id FROM configs WHERE id=?", (arg,)).fetchone()
        if not r:
            await c.answer("کانفیگ پیدا نشد", show_alert=True)
            return
        db.execute("UPDATE configs SET confirmed=1 WHERE id=?", (arg,))
        db.commit()
        try:
            await c.message.edit_text(
                (c.message.html_text or "") + "\n\n✅ کانفیگ توسط ادمین تایید شد",
                reply_markup=report_kb(arg, r["user_id"], confirmed=True),
            )
        except Exception:
            logging.exception("edit report failed")
    elif action in ("cfgok", "usrok"):
        try:
            result = await (delete_config(arg) if action == "cfgok" else delete_user(arg))
        except Exception:
            logging.exception("admin delete failed")
            result = "⚠️ خطا در حذف (ارتباط با اوتلاین). دوباره تلاش کن."
        await c.message.edit_text(result, reply_markup=None)
    await c.answer()


def pending_topups():
    return db.execute("SELECT * FROM topups WHERE status='pending' ORDER BY id").fetchall()


async def send_pending(bot: Bot, chat_id: int):
    rows = pending_topups()
    if not rows:
        await bot.send_message(chat_id, "رسید در انتظاری نیست ✅")
        return
    for r in rows:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✅ تایید", callback_data=f"tx:ok:{r['id']}")],
                [InlineKeyboardButton(text="❌ رد", callback_data=f"tx:no:{r['id']}")]
            ]
        )
        when = stamp(r["created"] or 0)[:16]
        caption = (
            f"درخواست شارژ #{r['id']}\n"
            f"کاربر: <code>{r['user_id']}</code>\n"
            f"مبلغ: {fmt(r['amount'])} تومان\n"
            f"زمان: {when}"
        )
        try:
            if r["file_id"]:
                await bot.send_photo(chat_id, r["file_id"], caption=caption, reply_markup=kb)
            else:
                await bot.send_message(chat_id, caption, reply_markup=kb)
        except Exception:
            logging.exception("send pending failed")


@router.message(F.text == BTN_PENDING_RECEIPTS)
async def pending_button(m: Message, bot: Bot):
    if m.from_user.id not in OWNER_IDS:
        return
    await send_pending(bot, m.chat.id)


@router.callback_query(F.data == "pend:show")
async def pending_cb(c: CallbackQuery, bot: Bot):
    if c.from_user.id not in OWNER_IDS:
        await c.answer("دسترسی نداری", show_alert=True)
        return
    await send_pending(bot, c.message.chat.id)
    await c.answer()


PENDING_ALERT_SECONDS = int(os.environ.get("PENDING_ALERT_SECONDS", "30"))
_last_alert: dict[int, int] = {}


async def pending_alert_loop(bot: Bot):
    while True:
        try:
            n = db.execute("SELECT COUNT(*) AS c FROM topups WHERE status='pending'").fetchone()["c"]
            for admin in OWNER_IDS:
                old = _last_alert.pop(admin, None)
                if old:  # هشدار قبلی پاک می‌شود تا چت شلوغ نشود
                    try:
                        await bot.delete_message(admin, old)
                    except Exception:
                        pass
                if n:
                    kb = InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(text="⏳ نمایش رسیدها", callback_data="pend:show")]
                        ]
                    )
                    try:
                        msg = await bot.send_message(
                            admin, f"⏳ هشدار: {n} رسید در انتظار تایید دارید.", reply_markup=kb
                        )
                        _last_alert[admin] = msg.message_id
                    except Exception:
                        logging.exception("pending alert failed")
        except Exception:
            logging.exception("pending alert loop error")
        await asyncio.sleep(PENDING_ALERT_SECONDS)


class PurchaseError(Exception):
    pass


class InsufficientBalance(PurchaseError):
    pass


async def purchase(bot: Bot, uid: int, full_name: str, gb: int, label: str = "") -> dict:
    """کسر موجودی، ساخت کلید در اوتلاین و ثبت کانفیگ. گزارش ادمین جداگانه با report_delivery ارسال می‌شود."""
    price = dict(PLANS).get(gb)
    if price is None:
        raise PurchaseError("پلن نامعتبر")
    ensure_user(uid)
    cur = db.execute(
        "UPDATE users SET balance=balance-? WHERE id=? AND balance>=?", (price, uid, price)
    )
    db.commit()
    if cur.rowcount == 0:
        raise InsufficientBalance("موجودی کافی نیست. اول کیف پول را شارژ کن.")
    try:
        purchase_ts = int(time.time())
        key_name = stamp(purchase_ts) + (f" {label}" if label else "")
        key = await outline.create_key(key_name, gb)
    except Exception:
        logging.exception("outline create failed")
        db.execute("UPDATE users SET balance=balance+? WHERE id=?", (price, uid))
        db.commit()
        raise PurchaseError("خطا در ساخت کانفیگ. مبلغ به کیف پولت برگشت، دوباره تلاش کن.")
    expires = purchase_ts + PLAN_DAYS * 86400
    cur = db.execute(
        "INSERT INTO configs(user_id, key_id, gb, access_url, expires, price, created, label) VALUES(?,?,?,?,?,?,?,?)",
        (uid, key["id"], gb, key["accessUrl"], expires, price, purchase_ts, label or None),
    )
    cfg_id = cur.lastrowid
    db.commit()
    return {
        "id": cfg_id,
        "key_id": key["id"],
        "name": key_name,
        "gb": gb,
        "price": price,
        "expires": expires,
        "links": build_links(key["accessUrl"], key_name),
    }


async def _charge(uid: int, price: int):
    cur = db.execute(
        "UPDATE users SET balance=balance-? WHERE id=? AND balance>=?", (price, uid, price)
    )
    db.commit()
    if cur.rowcount == 0:
        raise InsufficientBalance("موجودی کافی نیست. اول کیف پول را شارژ کن.")


def _refund(uid: int, price: int):
    db.execute("UPDATE users SET balance=balance+? WHERE id=?", (price, uid))
    db.commit()


def _own_config(uid: int, cfg_id: int, states=(1, 2)):
    r = db.execute("SELECT * FROM configs WHERE id=? AND user_id=?", (cfg_id, uid)).fetchone()
    if not r or r["active"] not in states:
        raise PurchaseError("اکانت پیدا نشد یا برای این کار قابل استفاده نیست.")
    return r


async def renew_config(uid: int, cfg_id: int, plan_gb: int) -> dict:
    """تمدید روی همان کلید: حجم پلن به حجم قبلی اضافه می‌شود و انقضا ۳۰ روز تمدید می‌شود."""
    price = dict(PLANS).get(plan_gb)
    if price is None:
        raise PurchaseError("پلن نامعتبر است.")
    r = _own_config(uid, cfg_id)
    await _charge(uid, price)
    now = int(time.time())
    new_gb = round(r["gb"] + plan_gb, 2)
    new_exp = max(r["expires"], now) + PLAN_DAYS * 86400
    try:
        await outline.set_limit(r["key_id"], new_gb)
    except Exception:
        logging.exception("renew: set limit failed")
        _refund(uid, price)
        raise PurchaseError("خطا در ارتباط با اوتلاین. مبلغ به کیف پولت برگشت، دوباره تلاش کن.")
    db.execute(
        "UPDATE configs SET gb=?, expires=?, active=1, warned=0, reminded=0 WHERE id=?",
        (new_gb, new_exp, cfg_id),
    )
    db.execute(
        "INSERT INTO orders(user_id, config_id, kind, gb, price, created) VALUES(?,?,?,?,?,?)",
        (uid, cfg_id, "renew", plan_gb, price, now),
    )
    db.commit()
    return {"id": cfg_id, "name": config_label(r), "gb": new_gb, "expires": new_exp, "price": price, "kind": "renew"}


async def add_volume(uid: int, cfg_id: int, gb: int) -> dict:
    """افزایش حجم روی همان کلید، بدون تغییر انقضا (فقط برای اکانت فعال)."""
    if gb not in ADDON_GB:
        raise PurchaseError("مقدار نامعتبر است.")
    r = _own_config(uid, cfg_id, states=(1,))
    price = gb * PRICE_PER_GB
    await _charge(uid, price)
    new_gb = round(r["gb"] + gb, 2)
    try:
        await outline.set_limit(r["key_id"], new_gb)
    except Exception:
        logging.exception("addon: set limit failed")
        _refund(uid, price)
        raise PurchaseError("خطا در ارتباط با اوتلاین. مبلغ به کیف پولت برگشت، دوباره تلاش کن.")
    db.execute("UPDATE configs SET gb=?, warned=0 WHERE id=?", (new_gb, cfg_id))
    db.execute(
        "INSERT INTO orders(user_id, config_id, kind, gb, price, created) VALUES(?,?,?,?,?,?)",
        (uid, cfg_id, "addon", gb, price, int(time.time())),
    )
    db.commit()
    return {"id": cfg_id, "name": config_label(r), "gb": new_gb, "expires": r["expires"], "price": price, "kind": "addon"}


async def notify_order(bot: Bot, uid: int, full_name: str, res: dict):
    row = db.execute("SELECT owner_id FROM users WHERE id=?", (uid,)).fetchone()
    sub = row["owner_id"] if row and row["owner_id"] not in (None, *OWNER_IDS) else None
    title = "🔄 تمدید اکانت" if res["kind"] == "renew" else "➕ افزایش حجم"
    text = (
        f"{title}\nکاربر: {html.escape(full_name)} (<code>{uid}</code>)\nاکانت: {html.escape(res['name'])}\n"
        f"مبلغ: {fmt(res['price'])} تومان | حجم کل جدید: {res['gb']:g} گیگ | انقضا: {stamp(res['expires'])[:16]}"
    )
    for a in list(OWNER_IDS) + ([sub] if sub else []):
        try:
            await bot.send_message(a, text)
        except Exception:
            logging.exception("notify order failed")


def qr_bytes(data: str, kind: str = "png"):
    """QR برای یک لینک؛ اگر کتابخانه‌ی segno نصب نباشد None برمی‌گرداند (ربات بدون QR کار می‌کند)."""
    try:
        import segno
    except ImportError:
        logging.warning("segno is not installed; QR skipped")
        return None
    buf = io.BytesIO()
    opts = {"xmldecl": False} if kind == "svg" else {}
    segno.make(data, error="m").save(
        buf, kind=kind, scale=8 if kind == "png" else 6, border=2,
        dark="#000000", light="#ffffff", **opts,
    )
    return buf.getvalue()


async def send_link_qrs(bot: Bot, chat_id: int, links: list[str]):
    """برای هر لینک یک عکس QR می‌فرستد (چند لینک ← آلبوم)."""
    try:
        media = []
        for link in links:
            png = qr_bytes(link)
            if png is None:
                return
            label = unquote(link.split("#", 1)[1]) if "#" in link else "کانفیگ"
            media.append(
                InputMediaPhoto(
                    media=BufferedInputFile(png, filename="qr.png"),
                    caption=f"QR کانفیگ: {html.escape(label)}",
                )
            )
        if len(media) == 1:
            await bot.send_photo(chat_id, media[0].media, caption=media[0].caption)
        elif media:
            await bot.send_media_group(chat_id, media)
    except Exception:
        logging.exception("send QR failed")


async def report_delivery(bot: Bot, uid: int, full_name: str, res: dict):
    report_text = (
        "✅ گزارش تحویل کانفیگ\n"
        f"کاربر: {html.escape(full_name)} (<code>{uid}</code>)\n"
        f"شناسه کانفیگ: #{res['id']}\n"
        f"نام کلید: <code>{res['name']}</code> | شناسه اوتلاین: {res['key_id']}\n"
        f"پلن: {res['gb']} گیگ (سقف روی اوتلاین: {res['gb'] / TRAFFIC_FACTOR:g} گیگ)\n"
        f"قیمت: {fmt(res['price'])} تومان\n"
        f"انقضا: {stamp(res['expires'])[:16]}\n\n"
        + links_text(res["links"])
    )
    admin_kb = report_kb(res["id"], uid)
    row = db.execute("SELECT owner_id FROM users WHERE id=?", (uid,)).fetchone()
    sub = row["owner_id"] if row and row["owner_id"] not in (None, *OWNER_IDS) else None
    for admin in list(OWNER_IDS) + ([sub] if sub else []):
        try:
            await bot.send_message(admin, report_text, reply_markup=admin_kb if admin in OWNER_IDS else None)
            await send_link_qrs(bot, admin, res["links"])
        except Exception:
            logging.exception("notify admin failed")


async def notify_admins_topup(bot: Bot, tid: int, uid: int, full_name: str, amount: int, photo):
    """photo: file_id (str) یا BufferedInputFile. بعد از اولین ارسال موفق، file_id ذخیره می‌شود."""
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ تایید", callback_data=f"tx:ok:{tid}")],
            [InlineKeyboardButton(text="❌ رد", callback_data=f"tx:no:{tid}")],
        ]
    )
    caption = (
        f"درخواست شارژ #{tid}\n"
        f"کاربر: {html.escape(full_name)} (<code>{uid}</code>)\n"
        f"مبلغ: {fmt(amount)} تومان"
    )
    file_id = photo if isinstance(photo, str) else None
    for admin in OWNER_IDS:
        try:
            msg = await bot.send_photo(
                admin, file_id or photo, caption=caption, reply_markup=kb
            )
            if file_id is None:
                file_id = msg.photo[-1].file_id
                db.execute("UPDATE topups SET file_id=? WHERE id=?", (file_id, tid))
                db.commit()
        except Exception:
            logging.exception("send to admin failed")


async def submit_registration(bot: Bot, uid: int, full_name: str, username, note: str) -> bool:
    if user_status(uid) != "new":
        return False
    name = full_name + (f" (@{username})" if username else "")
    db.execute(
        "UPDATE users SET status='pending', note=?, requested=?, name=? WHERE id=?",
        (note.strip()[:500], int(time.time()), name, uid),
    )
    db.commit()
    text, kb = reg_card(uid, "pending")
    for admin in OWNER_IDS:
        try:
            await bot.send_message(admin, "📝 درخواست عضویت جدید\n" + text, reply_markup=kb)
        except Exception:
            logging.exception("notify admin (registration) failed")
    return True


async def reminder_loop(bot: Bot):
    while True:
        try:
            now = int(time.time())
            rows = db.execute(
                "SELECT * FROM configs WHERE active=1 AND reminded=0 AND expires>? AND expires<=?",
                (now, now + REMIND_DAYS * 86400),
            ).fetchall()
            for r in rows:
                db.execute("UPDATE configs SET reminded=1 WHERE id=?", (r["id"],))
                db.commit()
                days = max(1, -(-(r["expires"] - now) // 86400))
                when = stamp(r["expires"])[:16]
                try:
                    await bot.send_message(
                        r["user_id"],
                        f"⏰ یادآوری\n"
                        f"کانفیگ {r['gb']} گیگی‌ات حدود {days} روز دیگر ({when}) منقضی می‌شود.\n"
                        f"برای ادامه، قبل از انقضا از «📂 کانفیگ‌های من» تمدید کن یا حجم اضافه بخر.",
                    )
                except Exception:
                    logging.exception("reminder send failed")
        except Exception:
            logging.exception("reminder loop error")
        await asyncio.sleep(600)


async def usage_alert_loop(bot: Bot):
    while True:
        try:
            rows = db.execute("SELECT * FROM configs WHERE active=1 AND warned=0").fetchall()
            if rows:
                used_by_key = await outline.get_usage()
                for r in rows:
                    total = r["gb"] * GIB
                    used = used_by_key.get(r["key_id"], 0) * TRAFFIC_FACTOR
                    if used * 100 / total >= ALERT_PERCENT:
                        db.execute("UPDATE configs SET warned=1 WHERE id=?", (r["id"],))
                        db.commit()
                        try:
                            await bot.send_message(
                                r["user_id"],
                                f"⚠️ هشدار حجم\n"
                                f"بیش از {ALERT_PERCENT}٪ حجم کانفیگ {r['gb']} گیگی‌ات مصرف شده.\n"
                                f"برای جلوگیری از قطعی، از منوی «📂 کانفیگ‌های من» تمدید کن یا حجم اضافه بخر.",
                            )
                        except Exception:
                            logging.exception("usage alert send failed")
        except Exception:
            logging.exception("usage alert loop error")
        await asyncio.sleep(300)


# ───────────────────────── مینی‌اپ (صفحه + API) ─────────────────────────
WEBAPP_URL = os.environ.get("WEBAPP_URL", "").rstrip("/")
WEB_PORT = int(os.environ.get("WEB_PORT", "8080"))
WEBAPP_DIR = Path(__file__).resolve().parent / "webapp"
db.execute(
    "CREATE TABLE IF NOT EXISTS wallet_log("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, admin_id INTEGER, "
    "delta INTEGER, note TEXT, created INTEGER)"
)
db.execute("CREATE TABLE IF NOT EXISTS admins(id INTEGER PRIMARY KEY, added_by INTEGER, created INTEGER)")
db.commit()
OWNER_IDS = set(ADMIN_IDS)  # ادمین‌های اصلی (.env)؛ فقط آن‌ها می‌توانند ادمین اضافه یا حذف کنند
ADMIN_IDS.update(r["id"] for r in db.execute("SELECT id FROM admins"))
db.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)")
for _ddl in (
    "ALTER TABLE configs ADD COLUMN label TEXT",
    "ALTER TABLE configs ADD COLUMN added_by INTEGER",
    "ALTER TABLE users ADD COLUMN owner_id INTEGER",
    "ALTER TABLE users ADD COLUMN report_pref INTEGER",
    "ALTER TABLE users ADD COLUMN welcomed INTEGER NOT NULL DEFAULT 0",
):
    try:
        db.execute(_ddl)
    except sqlite3.OperationalError:
        pass  # ستون از قبل هست
db.execute("CREATE TABLE IF NOT EXISTS key_assign(key_id TEXT PRIMARY KEY, admin_id INTEGER)")
db.execute("CREATE TABLE IF NOT EXISTS usage_snap(key_id TEXT PRIMARY KEY, bytes INTEGER, ts INTEGER)")
db.execute(
    "CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, config_id INTEGER, "
    "kind TEXT, gb REAL, price INTEGER, created INTEGER)"
)
db.execute(
    "CREATE TABLE IF NOT EXISTS admin_log(id INTEGER PRIMARY KEY AUTOINCREMENT, admin_id INTEGER, "
    "action TEXT, user_id INTEGER, config_id INTEGER, detail TEXT, created INTEGER)"
)
db.commit()


def log_admin(admin_id: int, action: str, user_id=None, config_id=None, detail: str = ""):
    db.execute(
        "INSERT INTO admin_log(admin_id, action, user_id, config_id, detail, created) VALUES(?,?,?,?,?,?)",
        (admin_id, action, user_id, config_id, (detail or "")[:200], int(time.time())),
    )
    db.commit()


def admin_name(aid) -> str:
    r = db.execute("SELECT name FROM users WHERE id=?", (aid,)).fetchone()
    return r["name"] if r and r["name"] else f"ادمین {aid}"


def can_manage(admin_id: int, user_id: int) -> bool:
    """ادمین اصلی همه را مدیریت می‌کند؛ ادمین فرعی فقط مشتری‌هایی که خودش اضافه کرده."""
    if admin_id in OWNER_IDS:
        return True
    r = db.execute("SELECT owner_id FROM users WHERE id=?", (user_id,)).fetchone()
    return bool(r and r["owner_id"] == admin_id)


def claim_allowed(admin_id: int, row) -> bool:
    """آیا این ادمین می‌تواند این مشتری را به‌عنوان مشتری خودش اضافه کند؟"""
    if admin_id in OWNER_IDS or row["owner_id"] == admin_id:
        return True
    return row["owner_id"] is None and row["status"] in ("new", "pending")


def find_user_by_username(username: str):
    pattern = f"%(@{username.replace('_', chr(92) + '_')})"
    return db.execute(
        "SELECT id FROM users WHERE LOWER(name) LIKE ? ESCAPE '" + chr(92) + "'", (pattern,)
    ).fetchone()


def get_setting(key: str, default: str = "") -> str:
    r = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_setting(key: str, value: str):
    db.execute(
        "INSERT INTO settings(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    db.commit()


def verify_init_data(init_data: str, max_age: int = 86400):
    """اعتبارسنجی initData تلگرام؛ در صورت معتبر بودن اطلاعات کاربر را برمی‌گرداند."""
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        received = pairs.pop("hash", None)
        if not received:
            return None
        check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, received):
            return None
        if time.time() - int(pairs.get("auth_date", "0")) > max_age:
            return None
        return json.loads(pairs["user"])
    except Exception:
        return None


def touch_name(user_id: int, first, last, username):
    name = (first or "") + (f" {last}" if last else "") + (f" (@{username})" if username else "")
    db.execute(
        "UPDATE users SET name=? WHERE id=? AND COALESCE(name,'')!=?", (name, user_id, name)
    )
    db.commit()


@web.middleware
async def auth_mw(request: web.Request, handler):
    if request.path.startswith("/api/"):
        header = request.headers.get("Authorization", "")
        user = verify_init_data(header[4:]) if header.startswith("tma ") else None
        if not user:
            return web.json_response({"error": "unauthorized"}, status=401)
        request["tg_user"] = user
        touch_name(user["id"], user.get("first_name"), user.get("last_name"), user.get("username"))
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except Exception:
        logging.exception("web handler error")
        return web.json_response({"error": "خطای داخلی سرور"}, status=500)


async def web_index(request: web.Request):
    return web.FileResponse(WEBAPP_DIR / "index.html", headers={"Cache-Control": "no-store"})


async def web_tgjs(request: web.Request):
    return web.FileResponse(
        WEBAPP_DIR / "telegram-web-app.js", headers={"Cache-Control": "max-age=3600"}
    )


MIN_TOPUP = 10_000
MAX_PENDING_TOPUPS = 5


def full_name_of(u: dict) -> str:
    name = f"{u.get('first_name', '')} {u.get('last_name', '')}".strip()
    return name or str(u["id"])


def err(msg: str, status: int = 400, **extra):
    return web.json_response({"error": msg, **extra}, status=status)


async def read_json(request: web.Request) -> dict:
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def config_json(r, used_by_key):
    total = r["gb"] * GIB
    used = None
    if used_by_key is not None:
        used = min(used_by_key.get(r["key_id"], 0) * TRAFFIC_FACTOR, total)
    return {
        "id": r["id"],
        "gb": r["gb"],
        "name": config_label(r),
        "expires": r["expires"],
        "days_left": max(0, (r["expires"] - int(time.time())) // 86400),
        "used_gb": None if used is None else round(used / GIB, 2),
        "left_gb": None if used is None else round((total - used) / GIB, 2),
        "pct": None if used is None else int(used * 100 / total),
        "links": build_links(r["access_url"], config_label(r)),
    }


async def api_me(request: web.Request):
    u = request["tg_user"]
    uid = u["id"]
    return web.json_response(
        {
            "id": uid,
            "name": u.get("first_name", ""),
            "is_admin": uid in ADMIN_IDS,
            "role": "owner" if uid in OWNER_IDS else ("admin" if uid in ADMIN_IDS else "user"),
            "status": user_status(uid),
            "balance": balance(uid),
            "support": SUPPORT_USERNAME,
            "plans": [{"gb": gb, "price": price} for gb, price in PLANS],
            "plan_days": PLAN_DAYS,
            "min_topup": MIN_TOPUP,
            "addons": [{"gb": g, "price": g * PRICE_PER_GB} for g in ADDON_GB],
            "apps": [
                {"name": n, "links": [{"label": l, "url": u} for l, u in ls]} for n, ls in APP_LINKS
            ],
        }
    )


async def api_register(request: web.Request):
    u = request["tg_user"]
    body = await read_json(request)
    note = str(body.get("note", "")).strip()
    if len(note) < 2:
        return err("یک پیام کوتاه برای ادمین بنویس.")
    ok = await submit_registration(
        request.app["bot"], u["id"], full_name_of(u), u.get("username"), note
    )
    if not ok:
        return err("وضعیت عضویتت قابل تغییر نیست.", 409)
    return web.json_response({"status": "pending"})


async def api_topup_info(request: web.Request):
    if not is_member(request["tg_user"]["id"]):
        return err("اول باید عضویتت تایید شود.", 403)
    return web.json_response({"card": CARD_INFO, "min": MIN_TOPUP})


async def api_topup(request: web.Request):
    u = request["tg_user"]
    uid = u["id"]
    if not is_member(uid):
        return err("اول باید عضویتت تایید شود.", 403)
    form = await request.post()
    try:
        amount = int(str(form.get("amount", "")).translate(DIGITS).replace(",", "").strip())
    except ValueError:
        return err("مبلغ نامعتبر است.")
    if amount < MIN_TOPUP:
        return err(f"حداقل مبلغ شارژ {fmt(MIN_TOPUP)} تومان است.")
    photo = form.get("photo")
    if photo is None or not hasattr(photo, "file"):
        return err("عکس فیش را انتخاب کن.")
    data = photo.file.read()
    if not data or len(data) > 10 * 1024 * 1024:
        return err("حجم عکس نامعتبر است (حداکثر ۱۰ مگابایت).")
    if not (data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n"):
        return err("فقط عکس JPG یا PNG قبول است.")
    pending = db.execute(
        "SELECT COUNT(*) AS c FROM topups WHERE user_id=? AND status='pending'", (uid,)
    ).fetchone()["c"]
    if pending >= MAX_PENDING_TOPUPS:
        return err("چند رسید در انتظار تایید داری؛ لطفا منتظر بررسی ادمین بمان.", 429)
    cur = db.execute(
        "INSERT INTO topups(user_id, amount, created) VALUES(?,?,?)",
        (uid, amount, int(time.time())),
    )
    db.commit()
    tid = cur.lastrowid
    await notify_admins_topup(
        request.app["bot"], tid, uid, full_name_of(u), amount,
        BufferedInputFile(data, filename="receipt.jpg"),
    )
    return web.json_response({"id": tid, "status": "pending"})


async def api_topups(request: web.Request):
    rows = db.execute(
        "SELECT id, amount, status, created FROM topups WHERE user_id=? ORDER BY id DESC LIMIT 20",
        (request["tg_user"]["id"],),
    ).fetchall()
    return web.json_response({"topups": [dict(r) for r in rows]})


async def api_buy(request: web.Request):
    u = request["tg_user"]
    uid = u["id"]
    if not is_member(uid):
        return err("اول باید عضویتت تایید شود.", 403)
    body = await read_json(request)
    try:
        gb = int(body.get("gb", 0))
    except (TypeError, ValueError):
        return err("پلن نامعتبر")
    label = clean_label(str(body.get("label", "")))
    if not label:
        return err("اسم اکانت را وارد کن.")
    try:
        res = await purchase(request.app["bot"], uid, full_name_of(u), gb, label)
    except InsufficientBalance as e:
        return err(str(e), 402, code="balance")
    except PurchaseError as e:
        return err(str(e), 500)
    row = db.execute("SELECT * FROM configs WHERE id=?", (res["id"],)).fetchone()
    await report_delivery(request.app["bot"], uid, full_name_of(u), res)
    return web.json_response({"config": config_json(row, None), "balance": balance(uid)})


async def api_configs(request: web.Request):
    rows = db.execute(
        "SELECT * FROM configs WHERE user_id=? AND active IN (1,2) ORDER BY active, id DESC",
        (request["tg_user"]["id"],),
    ).fetchall()
    used = None
    if any(r["active"] == 1 for r in rows):
        try:
            used = await outline.get_usage()
        except Exception:
            logging.exception("usage fetch failed (api)")
    now, out = int(time.time()), []
    for r in rows:
        d = config_json(r, used if r["active"] == 1 else None)
        if r["active"] == 2:
            d["expired"] = True
            d["grace_days"] = max(0, (r["expires"] + GRACE_DAYS * 86400 - now) // 86400)
            d["links"] = []
        out.append(d)
    return web.json_response({"configs": out})


async def api_qr(request: web.Request):
    data = request.query.get("l", "")
    if not data or len(data) > 600:
        return err("لینک نامعتبر است.")
    svg = qr_bytes(data, "svg")
    if svg is None:
        return err("QR در دسترس نیست.", 503)
    return web.Response(
        body=svg, content_type="image/svg+xml", headers={"Cache-Control": "private, max-age=3600"}
    )


def _wallet_log(uid: int, limit: int = 10):
    rows = db.execute(
        "SELECT delta, note, created FROM wallet_log WHERE user_id=? ORDER BY id DESC LIMIT ?",
        (uid, limit),
    ).fetchall()
    return [dict(r) for r in rows]


async def api_admin_users(request: web.Request):
    if request["tg_user"]["id"] not in ADMIN_IDS:
        return err("دسترسی نداری.", 403)
    q = request.query.get("q", "").strip()
    try:
        offset = max(0, int(request.query.get("offset", "0")))
    except ValueError:
        offset = 0
    sql = (
        "SELECT u.id, u.name, u.status, u.balance, "
        "(SELECT COUNT(*) FROM configs c WHERE c.user_id=u.id AND c.active=1) AS configs "
        "FROM users u"
    )
    args, conds = [], []
    if request["tg_user"]["id"] not in OWNER_IDS:
        conds.append("u.owner_id=?")
        args.append(request["tg_user"]["id"])
    if q:
        conds.append("(u.name LIKE ? OR CAST(u.id AS TEXT) LIKE ?)")
        args += [f"%{q}%", f"%{q}%"]
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY u.requested DESC, u.id LIMIT 31 OFFSET ?"
    rows = db.execute(sql, args + [offset]).fetchall()
    return web.json_response({"users": [dict(r) for r in rows[:30]], "more": len(rows) > 30})


async def api_admin_user(request: web.Request):
    if request["tg_user"]["id"] not in ADMIN_IDS:
        return err("دسترسی نداری.", 403)
    try:
        uid = int(request.query.get("id", ""))
    except ValueError:
        return err("آیدی نامعتبر است.")
    r = db.execute("SELECT id, name, status, balance, owner_id FROM users WHERE id=?", (uid,)).fetchone()
    if not r:
        return err("کاربر پیدا نشد.", 404)
    if not can_manage(request["tg_user"]["id"], uid):
        return err("این مشتری متعلق به تو نیست.", 403)
    n = db.execute(
        "SELECT COUNT(*) AS c FROM configs WHERE user_id=? AND active=1", (uid,)
    ).fetchone()["c"]
    crows = db.execute(
        "SELECT * FROM configs WHERE user_id=? AND active=1 ORDER BY id DESC", (uid,)
    ).fetchall()
    used = None
    if crows:
        try:
            used = await outline.get_usage()
        except Exception:
            logging.exception("usage fetch failed (admin)")
    cfgs = []
    for c in crows:
        d = config_json(c, used)
        d.pop("links")
        d["added_by"] = admin_name(c["added_by"]) if c["added_by"] else None
        cfgs.append(d)
    return web.json_response(
        {**dict(r), "configs": n, "cfgs": cfgs, "log": _wallet_log(uid),
         "can_wallet": request["tg_user"]["id"] in OWNER_IDS}
    )


async def api_admin_wallet(request: web.Request):
    admin_id = request["tg_user"]["id"]
    if admin_id not in OWNER_IDS:
        return err("فقط ادمین اصلی می‌تواند کیف پول را تغییر دهد.", 403)
    body = await read_json(request)
    try:
        uid = int(body["user_id"])
        amount = int(str(body["amount"]).translate(DIGITS).replace(",", "").strip())
    except (KeyError, ValueError, TypeError):
        return err("اطلاعات نامعتبر است.")
    mode = body.get("mode")
    if mode not in ("add", "sub") or not 0 < amount <= 1_000_000_000:
        return err("مبلغ یا نوع عملیات نامعتبر است.")
    if not db.execute("SELECT 1 FROM users WHERE id=?", (uid,)).fetchone():
        return err("کاربر پیدا نشد.", 404)
    if mode == "add":
        db.execute("UPDATE users SET balance=balance+? WHERE id=?", (amount, uid))
    else:
        cur = db.execute(
            "UPDATE users SET balance=balance-? WHERE id=? AND balance>=?", (amount, uid, amount)
        )
        if cur.rowcount == 0:
            db.commit()
            return err("موجودی کاربر برای این کاهش کافی نیست.", 409)
    note = str(body.get("note", "")).strip()[:100]
    db.execute(
        "INSERT INTO wallet_log(user_id, admin_id, delta, note, created) VALUES(?,?,?,?,?)",
        (uid, admin_id, amount if mode == "add" else -amount, note, int(time.time())),
    )
    db.commit()
    bal = balance(uid)
    log_admin(admin_id, "wallet", uid, None, f"{'+' if mode == 'add' else '-'}{amount} | {note}")
    try:
        await request.app["bot"].send_message(
            uid,
            f"💰 کیف پولت توسط ادمین {'افزایش' if mode == 'add' else 'کاهش'} یافت: {fmt(amount)} تومان\n"
            f"موجودی جدید: {fmt(bal)} تومان" + (f"\nتوضیح: {html.escape(note)}" if note else ""),
        )
    except Exception:
        logging.exception("notify wallet change failed")
    return web.json_response({"balance": bal, "log": _wallet_log(uid)})


async def api_admin_configs(request: web.Request):
    if request["tg_user"]["id"] not in ADMIN_IDS:
        return err("دسترسی نداری.", 403)
    q = request.query.get("q", "").strip()
    try:
        offset = max(0, int(request.query.get("offset", "0")))
    except ValueError:
        offset = 0
    sql = (
        "SELECT c.*, u.name AS uname FROM configs c "
        "LEFT JOIN users u ON u.id=c.user_id WHERE c.active=1"
    )
    args = []
    if request["tg_user"]["id"] not in OWNER_IDS:
        sql += " AND u.owner_id=?"
        args.append(request["tg_user"]["id"])
    if q:
        sql += " AND (u.name LIKE ? OR CAST(c.user_id AS TEXT) LIKE ?)"
        args += [f"%{q}%", f"%{q}%"]
    sql += " ORDER BY c.id DESC LIMIT 21 OFFSET ?"
    rows = db.execute(sql, args + [offset]).fetchall()
    used = None
    if rows:
        try:
            used = await outline.get_usage()
        except Exception:
            logging.exception("usage fetch failed (admin configs)")
    out = []
    for r in rows[:20]:
        d = config_json(r, used)
        d.pop("links")
        d["user_id"], d["user"] = r["user_id"], r["uname"]
        d["added_by"] = admin_name(r["added_by"]) if r["added_by"] else None
        out.append(d)
    return web.json_response({"configs": out, "more": len(rows) > 20})


async def api_admin_volume(request: web.Request):
    if request["tg_user"]["id"] not in ADMIN_IDS:
        return err("دسترسی نداری.", 403)
    body = await read_json(request)
    try:
        cid = int(body["config_id"])
        gb = int(str(body["gb"]).translate(DIGITS).strip())
    except (KeyError, ValueError, TypeError):
        return err("اطلاعات نامعتبر است.")
    if not 1 <= gb <= 10000:
        return err("حجم باید بین ۱ تا ۱۰۰۰۰ گیگ باشد.")
    r = db.execute("SELECT * FROM configs WHERE id=? AND active=1", (cid,)).fetchone()
    if not r:
        return err("کانفیگ فعال پیدا نشد.", 404)
    if not can_manage(request["tg_user"]["id"], r["user_id"]):
        return err("این اکانت متعلق به مشتری‌های تو نیست.", 403)
    try:
        await outline.set_limit(r["key_id"], gb)
    except Exception:
        logging.exception("set limit failed")
        return err("خطا در ارتباط با اوتلاین؛ حجم تغییر نکرد.", 502)
    db.execute("UPDATE configs SET gb=?, warned=0 WHERE id=?", (gb, cid))
    db.commit()
    log_admin(request["tg_user"]["id"], "volume", r["user_id"], cid, f"{r['gb']} -> {gb} GB")
    try:
        await request.app["bot"].send_message(
            r["user_id"], f"📦 حجم کانفیگ {config_label(r)} توسط ادمین از {r['gb']} به {gb} گیگ تغییر کرد."
        )
    except Exception:
        logging.exception("notify volume change failed")
    return web.json_response({"ok": True, "gb": gb})


def _owner(request: web.Request) -> bool:
    return request["tg_user"]["id"] in OWNER_IDS


async def api_admin_admins(request: web.Request):
    if request["tg_user"]["id"] not in OWNER_IDS:
        return err("دسترسی نداری.", 403)
    rows = []
    for aid in sorted(ADMIN_IDS):
        r = db.execute("SELECT name FROM users WHERE id=?", (aid,)).fetchone()
        rows.append({"id": aid, "name": r["name"] if r else None, "owner": aid in OWNER_IDS})
    return web.json_response({"admins": rows, "can_manage": _owner(request)})


async def api_admin_add(request: web.Request):
    if not _owner(request):
        return err("فقط ادمین اصلی می‌تواند ادمین اضافه کند.", 403)
    body = await read_json(request)
    try:
        aid = int(str(body["id"]).translate(DIGITS).strip())
    except (KeyError, ValueError, TypeError):
        return err("آیدی عددی نامعتبر است.")
    if aid <= 0 or aid in ADMIN_IDS:
        return err("آیدی نامعتبر است یا این کاربر از قبل ادمین است.")
    db.execute(
        "INSERT OR IGNORE INTO admins(id, added_by, created) VALUES(?,?,?)",
        (aid, request["tg_user"]["id"], int(time.time())),
    )
    db.commit()
    ADMIN_IDS.add(aid)
    log_admin(request["tg_user"]["id"], "add_admin", None, None, str(aid))
    try:
        await request.app["bot"].send_message(
            aid, "شما به‌عنوان ادمین ربات اضافه شدید ✅ برای نمایش منوی ادمین /start بزنید."
        )
    except Exception:
        logging.exception("notify new admin failed")
    return web.json_response({"ok": True})


async def api_admin_remove(request: web.Request):
    if not _owner(request):
        return err("فقط ادمین اصلی می‌تواند ادمین حذف کند.", 403)
    body = await read_json(request)
    try:
        aid = int(body["id"])
    except (KeyError, ValueError, TypeError):
        return err("آیدی نامعتبر است.")
    if aid in OWNER_IDS:
        return err("ادمین اصلی قابل حذف نیست.")
    db.execute("DELETE FROM admins WHERE id=?", (aid,))
    moved = db.execute("UPDATE users SET owner_id=NULL WHERE owner_id=?", (aid,)).rowcount
    db.execute("DELETE FROM key_assign WHERE admin_id=?", (aid,))
    db.commit()
    ADMIN_IDS.discard(aid)
    log_admin(request["tg_user"]["id"], "remove_admin", None, None, f"{aid}: {moved} customers moved to the main admin")
    try:
        await request.app["bot"].send_message(aid, "دسترسی ادمین شما توسط مدیر برداشته شد.")
    except Exception:
        logging.exception("notify dismissed admin failed")
    return web.json_response({"ok": True, "moved": moved})


async def api_home(request: web.Request):
    uid = request["tg_user"]["id"]
    rows = db.execute(
        "SELECT * FROM configs WHERE user_id=? AND active=1 ORDER BY id DESC", (uid,)
    ).fetchall()
    used = None
    if rows:
        try:
            used = await outline.get_usage()
        except Exception:
            logging.exception("usage fetch failed (home)")
    cfgs, alerts = [], []
    for i, r in enumerate(rows):
        d = config_json(r, used)
        d.pop("links")
        if i < 3:
            cfgs.append(d)
        if d["pct"] is not None and d["pct"] >= ALERT_PERCENT:
            alerts.append({"kind": "usage", "name": d["name"], "pct": d["pct"]})
        if d["days_left"] <= REMIND_DAYS:
            alerts.append({"kind": "expiry", "name": d["name"], "days": d["days_left"]})
    topups = [
        {"id": t["id"], "amount": t["amount"], "status": t["status"], "when": stamp(t["created"] or 0)[:16]}
        for t in db.execute(
            "SELECT id, amount, status, created FROM topups WHERE user_id=? ORDER BY id DESC LIMIT 3",
            (uid,),
        ).fetchall()
    ]
    return web.json_response(
        {
            "announcement": get_setting("announcement"),
            "configs": cfgs,
            "total": len(rows),
            "alerts": alerts[:3],
            "topups": topups,
        }
    )


async def api_admin_note_get(request: web.Request):
    if request["tg_user"]["id"] not in OWNER_IDS:
        return err("دسترسی نداری.", 403)
    return web.json_response({"text": get_setting("announcement")})


async def api_admin_note_set(request: web.Request):
    if request["tg_user"]["id"] not in OWNER_IDS:
        return err("دسترسی نداری.", 403)
    body = await read_json(request)
    set_setting("announcement", str(body.get("text", "")).strip()[:300])
    return web.json_response({"ok": True})


async def api_admin_unmanaged(request: web.Request):
    if request["tg_user"]["id"] not in ADMIN_IDS:
        return err("دسترسی نداری.", 403)
    try:
        keys = await outline.list_keys()
        used = await outline.get_usage()
    except Exception:
        logging.exception("list keys failed")
        return err("ارتباط با اوتلاین برقرار نشد.", 502)
    managed = {r["key_id"] for r in db.execute("SELECT key_id FROM configs WHERE active IN (1,2)")}
    me = request["tg_user"]["id"]
    owner = me in OWNER_IDS
    assigned = {r["key_id"]: r["admin_id"] for r in db.execute("SELECT key_id, admin_id FROM key_assign")}
    out = []
    for k in keys:
        kid = str(k["id"])
        if kid in managed:
            continue
        asg = assigned.get(kid)
        if not owner and asg != me:
            continue
        lim = (k.get("dataLimit") or {}).get("bytes")
        out.append(
            {
                "id": kid,
                "name": k.get("name", ""),
                "used_gb": round(used.get(kid, 0) * TRAFFIC_FACTOR / GIB, 2),
                "limit_gb": round(lim / GIB, 2) if lim else None,
                "assigned_id": asg,
                "assigned": admin_name(asg) if asg else None,
            }
        )
    return web.json_response({"keys": out})


async def api_admin_import(request: web.Request):
    if request["tg_user"]["id"] not in ADMIN_IDS:
        return err("دسترسی نداری.", 403)
    body = await read_json(request)
    try:
        kid = str(body["key_id"]).strip()
        username = str(body["username"]).strip().lstrip("@").lower()
        remaining = float(str(body["remaining_gb"]).translate(DIGITS).replace(",", "."))
        days = int(str(body["days"]).translate(DIGITS).strip())
    except (KeyError, ValueError, TypeError):
        return err("اطلاعات نامعتبر است.")
    if not re.fullmatch(r"[a-z0-9_]{3,32}", username):
        return err("یوزرنیم تلگرام نامعتبر است.")
    if not 0 < remaining <= 10000 or not 1 <= days <= 365:
        return err("حجم یا تعداد روز نامعتبر است.")
    if db.execute("SELECT 1 FROM configs WHERE key_id=? AND active IN (1,2)", (kid,)).fetchone():
        return err("این کلید قبلا به ربات متصل شده است.", 409)
    pattern = f"%(@{username.replace('_', chr(92) + '_')})"
    u = db.execute("SELECT id FROM users WHERE LOWER(name) LIKE ? ESCAPE '\\'", (pattern,)).fetchone()
    if not u:
        return err("کاربری با این یوزرنیم پیدا نشد. از مشتری بخواه ربات را /start کند یا مینی‌اپ را باز کند.", 404)
    uid = u["id"]
    admin_id = request["tg_user"]["id"]
    if admin_id not in OWNER_IDS:
        asg = db.execute("SELECT admin_id FROM key_assign WHERE key_id=?", (kid,)).fetchone()
        if not asg or asg["admin_id"] != admin_id:
            return err("این کلید به تو اختصاص داده نشده است.", 403)
        if not can_manage(admin_id, uid):
            return err("این مشتری متعلق به تو نیست. اول از «افزودن مشتری» او را اضافه کن.", 403)
    try:
        keys = await outline.list_keys()
        used = await outline.get_usage()
    except Exception:
        logging.exception("import: outline read failed")
        return err("ارتباط با اوتلاین برقرار نشد.", 502)
    key = next((k for k in keys if str(k["id"]) == kid), None)
    if not key:
        return err("کلید در اوتلاین پیدا نشد.", 404)
    # مصرف قبلی در شمارنده‌ی اوتلاین باقی می‌ماند، پس کل حجم = مصرف تا الان + حجم باقی‌مانده
    total = round(used.get(kid, 0) * TRAFFIC_FACTOR / GIB + remaining, 2)
    ts = int(time.time())
    label = clean_label(str(body.get("label", "")))
    name = stamp(ts) + (f" {label}" if label else "")
    try:
        await outline.rename_key(kid, name)
        await outline.set_limit(kid, total)
    except Exception:
        logging.exception("import: outline write failed")
        return err("خطا در ارتباط با اوتلاین؛ تغییری اعمال نشد.", 502)
    cur = db.execute(
        "INSERT INTO configs(user_id, key_id, gb, access_url, expires, price, created, confirmed, label, added_by) "
        "VALUES(?,?,?,?,?,?,?,1,?,?)",
        (uid, kid, total, key["accessUrl"], ts + days * 86400, 0, ts, label or None, admin_id),
    )
    cfg_id = cur.lastrowid
    db.execute("UPDATE users SET status='approved' WHERE id=?", (uid,))
    db.commit()
    db.execute("DELETE FROM key_assign WHERE key_id=?", (kid,))
    db.commit()
    log_admin(admin_id, "import", uid, cfg_id, f"{name} | {remaining:g} GB | {days} d")
    links = build_links(key["accessUrl"], name)
    bot = request.app["bot"]
    try:
        await bot.send_message(
            uid,
            f"✅ اکانت قبلی شما به ربات متصل شد.\nحجم باقی‌مانده: {remaining:g} گیگ | انقضا: {days} روز دیگر\n\n"
            + links_text(links),
        )
        await send_link_qrs(bot, uid, links)
    except Exception:
        logging.exception("import: notify customer failed")
    return web.json_response({"ok": True, "name": name, "user_id": uid})


USERNAME_RE = re.compile(r"[a-z0-9_]{3,32}")
ACTION_FA = {
    "import": "وارد کردن اکانت", "volume": "تغییر حجم", "wallet": "تغییر کیف پول",
    "delete_config": "حذف اکانت", "add_user": "افزودن مشتری", "assign_key": "اختصاص کلید",
    "remove_admin": "عزل ادمین", "add_admin": "افزودن ادمین", "broadcast": "ارسال همگانی",
}


async def api_admin_adduser(request: web.Request):
    me = request["tg_user"]["id"]
    if me not in ADMIN_IDS:
        return err("دسترسی نداری.", 403)
    body = await read_json(request)
    username = str(body.get("username", "")).strip().lstrip("@").lower()
    if not USERNAME_RE.fullmatch(username):
        return err("یوزرنیم تلگرام نامعتبر است.")
    u = find_user_by_username(username)
    if not u:
        return err("کاربری با این یوزرنیم پیدا نشد. از مشتری بخواه ربات را /start کند یا مینی‌اپ را باز کند.", 404)
    uid = u["id"]
    if uid in ADMIN_IDS:
        return err("این کاربر ادمین است.")
    row = db.execute("SELECT status, owner_id FROM users WHERE id=?", (uid,)).fetchone()
    if not claim_allowed(me, row):
        return err("این مشتری قبلا توسط مدیر یا ادمین دیگری اضافه یا رد شده است.", 403)
    new_owner = row["owner_id"] if me in OWNER_IDS else me
    db.execute("UPDATE users SET status='approved', owner_id=? WHERE id=?", (new_owner, uid))
    db.commit()
    log_admin(me, "add_user", uid, None, f"@{username}")
    try:
        await request.app["bot"].send_message(uid, "عضویتت تایید شد ✅ حالا می‌توانی از ربات و مینی‌اپ استفاده کنی.")
    except Exception:
        logging.exception("notify customer (add user) failed")
    return web.json_response({"ok": True, "user_id": uid})


async def api_admin_assign_key(request: web.Request):
    me = request["tg_user"]["id"]
    if me not in OWNER_IDS:
        return err("فقط ادمین اصلی می‌تواند کلید را به ادمین اختصاص دهد.", 403)
    body = await read_json(request)
    kid = str(body.get("key_id", "")).strip()
    aid = body.get("admin_id")
    if not kid:
        return err("کلید نامعتبر است.")
    if aid in (None, ""):
        db.execute("DELETE FROM key_assign WHERE key_id=?", (kid,))
    else:
        try:
            aid = int(aid)
        except (TypeError, ValueError):
            return err("ادمین نامعتبر است.")
        if aid not in ADMIN_IDS or aid in OWNER_IDS:
            return err("ادمین فرعی نامعتبر است.")
        db.execute("INSERT OR REPLACE INTO key_assign(key_id, admin_id) VALUES(?,?)", (kid, aid))
    db.commit()
    log_admin(me, "assign_key", None, None, f"key {kid} -> {aid or 'owner'}")
    return web.json_response({"ok": True})


async def api_admin_delete_config(request: web.Request):
    me = request["tg_user"]["id"]
    if me not in ADMIN_IDS:
        return err("دسترسی نداری.", 403)
    body = await read_json(request)
    try:
        cid = int(body["config_id"])
    except (KeyError, ValueError, TypeError):
        return err("اطلاعات نامعتبر است.")
    r = db.execute("SELECT user_id FROM configs WHERE id=?", (cid,)).fetchone()
    if not r:
        return err("کانفیگ پیدا نشد.", 404)
    if not can_manage(me, r["user_id"]):
        return err("این اکانت متعلق به مشتری‌های تو نیست.", 403)
    try:
        msg = await delete_config(cid)
    except Exception:
        logging.exception("api delete config failed")
        return err("خطا در ارتباط با اوتلاین؛ حذف نشد.", 502)
    log_admin(me, "delete_config", r["user_id"], cid, msg)
    return web.json_response({"ok": True, "message": msg})


async def api_admin_activity(request: web.Request):
    me = request["tg_user"]["id"]
    if me not in ADMIN_IDS:
        return err("دسترسی نداری.", 403)
    owner = me in OWNER_IDS
    target = request.query.get("admin")
    if target is None:
        out = []
        for aid in (sorted(ADMIN_IDS) if owner else [me]):
            imports = db.execute("SELECT COUNT(*) AS c FROM admin_log WHERE admin_id=? AND action='import'", (aid,)).fetchone()["c"]
            total = db.execute("SELECT COUNT(*) AS c FROM admin_log WHERE admin_id=?", (aid,)).fetchone()["c"]
            out.append({"id": aid, "name": admin_name(aid), "imports": imports, "actions": total})
        out.sort(key=lambda a: -a["actions"])
        return web.json_response({"admins": out})
    try:
        aid = int(target)
    except ValueError:
        return err("آیدی نامعتبر است.")
    if aid != me and not owner:
        return err("فقط ادمین اصلی فعالیت بقیه را می‌بیند.", 403)
    items = []
    for r in db.execute("SELECT * FROM admin_log WHERE admin_id=? ORDER BY id DESC LIMIT 60", (aid,)).fetchall():
        u = db.execute("SELECT name FROM users WHERE id=?", (r["user_id"],)).fetchone() if r["user_id"] else None
        who = (u["name"] if u and u["name"] else (str(r["user_id"]) if r["user_id"] else ""))
        items.append({"action": ACTION_FA.get(r["action"], r["action"]), "user": who, "detail": r["detail"] or "", "when": stamp(r["created"])[:16]})
    return web.json_response({"admin": admin_name(aid), "items": items})


async def _order_api(request: web.Request, kind: str):
    u = request["tg_user"]
    uid = u["id"]
    if not is_member(uid):
        return err("اول باید عضویتت تایید شود.", 403)
    body = await read_json(request)
    try:
        cid, gb = int(body["config_id"]), int(body["gb"])
    except (KeyError, TypeError, ValueError):
        return err("اطلاعات نامعتبر است.")
    try:
        res = await (renew_config if kind == "renew" else add_volume)(uid, cid, gb)
    except InsufficientBalance as e:
        return err(str(e), 402, code="balance")
    except PurchaseError as e:
        return err(str(e), 400)
    await notify_order(request.app["bot"], uid, full_name_of(u), res)
    return web.json_response({"ok": True, "balance": balance(uid)})


async def api_renew(request: web.Request):
    return await _order_api(request, "renew")


async def api_addvolume(request: web.Request):
    return await _order_api(request, "addon")


async def start_web(bot: Bot):
    app = web.Application(middlewares=[auth_mw], client_max_size=12 * 1024 * 1024)
    app["bot"] = bot
    app.router.add_get("/", web_index)
    app.router.add_get("/telegram-web-app.js", web_tgjs)
    app.router.add_get("/api/me", api_me)
    app.router.add_post("/api/register", api_register)
    app.router.add_get("/api/topup-info", api_topup_info)
    app.router.add_post("/api/topup", api_topup)
    app.router.add_get("/api/topups", api_topups)
    app.router.add_post("/api/buy", api_buy)
    app.router.add_get("/api/configs", api_configs)
    app.router.add_get("/api/qr", api_qr)
    app.router.add_get("/api/admin/users", api_admin_users)
    app.router.add_get("/api/admin/user", api_admin_user)
    app.router.add_post("/api/admin/wallet", api_admin_wallet)
    app.router.add_get("/api/admin/configs", api_admin_configs)
    app.router.add_post("/api/admin/volume", api_admin_volume)
    app.router.add_get("/api/admin/admins", api_admin_admins)
    app.router.add_post("/api/admin/admins/add", api_admin_add)
    app.router.add_post("/api/admin/admins/remove", api_admin_remove)
    app.router.add_get("/api/home", api_home)
    app.router.add_get("/api/admin/announcement", api_admin_note_get)
    app.router.add_post("/api/admin/announcement", api_admin_note_set)
    app.router.add_get("/api/admin/unmanaged", api_admin_unmanaged)
    app.router.add_post("/api/admin/import", api_admin_import)
    app.router.add_post("/api/renew", api_renew)
    app.router.add_post("/api/addvolume", api_addvolume)
    app.router.add_post("/api/admin/adduser", api_admin_adduser)
    app.router.add_post("/api/admin/assign-key", api_admin_assign_key)
    app.router.add_post("/api/admin/delete-config", api_admin_delete_config)
    app.router.add_get("/api/admin/activity", api_admin_activity)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", WEB_PORT).start()
    logging.info("web server listening on 127.0.0.1:%s", WEB_PORT)


def jdate(d) -> str:
    return "%04d/%02d/%02d" % _to_jalali(d.year, d.month, d.day)


def day_range(d):
    start = datetime(d.year, d.month, d.day, tzinfo=_TEHRAN)
    return int(start.timestamp()), int((start + timedelta(days=1)).timestamp())


def sales_summary(start: int, end: int) -> dict:
    """فروش واقعی (نه import) در بازه، به تفکیک ادمین مالک مشتری؛ کلید None یعنی ادمین اصلی."""
    rows = db.execute(
        "SELECT u.owner_id AS owner, COUNT(*) AS n, COALESCE(SUM(c.gb),0) AS gb "
        "FROM configs c JOIN users u ON u.id=c.user_id "
        "WHERE c.price>0 AND c.created>=? AND c.created<? GROUP BY u.owner_id",
        (start, end),
    ).fetchall()
    return {r["owner"]: (r["n"], r["gb"]) for r in rows}


def imports_count(admin_id: int, start: int, end: int) -> int:
    return db.execute(
        "SELECT COUNT(*) AS c FROM admin_log WHERE admin_id=? AND action='import' AND created>=? AND created<?",
        (admin_id, start, end),
    ).fetchone()["c"]


def jobs_due(now, last: dict) -> list:
    today = now.date().isoformat()
    due = []
    if (now.hour, now.minute) >= (23, 59) and last.get("owner_report") != today:
        due.append(("owner_report", today))
    if now.hour == 0 and now.minute < 30 and last.get("midnight") != today:
        due.append(("midnight", today))
    if now.weekday() == 4 and 3 <= now.hour < 6:  # جمعه ۰۳:۰۰ تا ۰۶:۰۰ به وقت تهران
        y, w, _ = now.isocalendar()
        if last.get("backup") != f"{y}-{w}":
            due.append(("backup", f"{y}-{w}"))
    return due


def orders_summary(start: int, end: int) -> dict:
    rows = db.execute(
        "SELECT u.owner_id AS owner, COUNT(*) AS n, COALESCE(SUM(o.gb),0) AS gb "
        "FROM orders o JOIN users u ON u.id=o.user_id WHERE o.created>=? AND o.created<? GROUP BY u.owner_id",
        (start, end),
    ).fetchall()
    return {r["owner"]: (r["n"], r["gb"]) for r in rows}


async def send_owner_report(bot: Bot, d):
    subs = sorted(ADMIN_IDS - OWNER_IDS)
    if not subs:
        return
    start, end = day_range(d)
    sales, orders = sales_summary(start, end), orders_summary(start, end)
    lines = [f"📊 گزارش عملکرد ادمین‌های فرعی | {jdate(d)}", ""]
    for aid in subs:
        n, gb = sales.get(aid, (0, 0))
        on, og = orders.get(aid, (0, 0))
        lines.append(
            f"• {html.escape(admin_name(aid))}: {n} اکانت جدید ({gb:g} گیگ) | {on} تمدید یا افزایش حجم ({og:g} گیگ) | "
            f"{imports_count(aid, start, end)} اکانت واردشده"
        )
    n0, gb0 = sales.get(None, (0, 0))
    on0, og0 = orders.get(None, (0, 0))
    lines.append(f"• ادمین اصلی: {n0} اکانت جدید ({gb0:g} گیگ) | {on0} تمدید یا افزایش حجم ({og0:g} گیگ)")
    total_gb = sum(v[1] for v in sales.values()) + sum(v[1] for v in orders.values())
    lines += ["", f"مجموع کل: {sum(v[0] for v in sales.values())} اکانت جدید | حجم کل فروش‌رفته (با تمدیدها): {total_gb:g} گیگ"]
    for o in OWNER_IDS:
        try:
            await bot.send_message(o, "\n".join(lines))
        except Exception:
            logging.exception("owner report failed")


async def send_sub_reports(bot: Bot, d):
    start, end = day_range(d)
    sales, orders = sales_summary(start, end), orders_summary(start, end)
    for aid in sorted(ADMIN_IDS - OWNER_IDS):
        n, gb = sales.get(aid, (0, 0))
        on, og = orders.get(aid, (0, 0))
        try:
            await bot.send_message(
                aid,
                f"📊 گزارش عملکرد شما | {jdate(d)}\nاکانت جدید: {n} ({gb:g} گیگ)\nتمدید یا افزایش حجم: {on} ({og:g} گیگ)\n"
                f"حجم کل فروش‌رفته: {gb + og:g} گیگ\nاکانت واردشده: {imports_count(aid, start, end)}",
            )
        except Exception:
            logging.exception("sub-admin report failed")


async def run_usage_reports(bot: Bot):
    """مصرف ۲۴ ساعت گذشته = اختلاف با snapshot شب قبل (شمارنده‌ی اوتلاین ۳۰ روزه است)."""
    try:
        used = await outline.get_usage()
    except Exception:
        logging.exception("usage reports: usage fetch failed")
        return
    now = int(time.time())
    snaps = {r["key_id"]: r["bytes"] for r in db.execute("SELECT key_id, bytes FROM usage_snap")}
    rows = db.execute(
        "SELECT c.*, u.report_pref AS pref FROM configs c JOIN users u ON u.id=c.user_id WHERE c.active=1"
    ).fetchall()
    per_user = {}
    for r in rows:
        cur = used.get(r["key_id"], 0)
        prev = snaps.get(r["key_id"])
        delta = None if prev is None else max(0, cur - prev) * TRAFFIC_FACTOR / GIB
        db.execute(
            "INSERT INTO usage_snap(key_id, bytes, ts) VALUES(?,?,?) "
            "ON CONFLICT(key_id) DO UPDATE SET bytes=excluded.bytes, ts=excluded.ts",
            (r["key_id"], cur, now),
        )
        per_user.setdefault(r["user_id"], []).append((r, delta))
    db.commit()
    off_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ دیگر نفرست", callback_data="rp:0")]])
    for uid, items in per_user.items():
        pref = items[0][0]["pref"]
        try:
            if pref is None:
                db.execute("UPDATE users SET report_pref=2 WHERE id=?", (uid,))
                db.commit()
                await bot.send_message(
                    uid,
                    "📅 می‌خواهی هر شب ساعت ۰۰:۰۰ گزارش مصرف ۲۴ ساعت گذشته‌ی اکانت‌هایت برایت ارسال شود؟",
                    reply_markup=rp_kb(),
                )
            elif pref == 1 and all(d is not None for _, d in items):
                lines = ["📅 گزارش مصرف ۲۴ ساعت گذشته", ""]
                for r, d in items:
                    j = config_json(r, used)
                    left = f" | باقی‌مانده {j['left_gb']} گیگ" if j.get("left_gb") is not None else ""
                    lines.append(f"• {html.escape(j['name'])}: {d:.2f} گیگ{left}")
                lines += ["", f"مجموع: {sum(d for _, d in items):.2f} گیگ"]
                await bot.send_message(uid, "\n".join(lines), reply_markup=off_kb)
        except Exception:
            logging.exception("usage report to %s failed", uid)
        await asyncio.sleep(0.05)


async def send_weekly_backup(bot: Bot):
    import tempfile

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    try:
        dst = sqlite3.connect(tmp.name)
        db.backup(dst)
        dst.close()
        data = open(tmp.name, "rb").read()
    finally:
        os.unlink(tmp.name)
    name = "bot-" + stamp(int(time.time()))[:10].replace("/", "-") + ".db"
    for o in OWNER_IDS:
        try:
            await bot.send_document(
                o, BufferedInputFile(data, filename=name),
                caption="🗄 بکاپ هفتگی دیتابیس ربات. شامل لینک اکانت‌هاست؛ محرمانه نگهش دار.",
            )
        except Exception:
            logging.exception("weekly backup send failed")


JOBS = {
    "owner": lambda bot: send_owner_report(bot, datetime.now(_TEHRAN).date()),
    "sub": lambda bot: send_sub_reports(bot, datetime.now(_TEHRAN).date()),
    "usage": run_usage_reports,
    "backup": send_weekly_backup,
}


@router.message(Command("runjob"))
async def runjob(m: Message):
    """تست دستی برای ادمین اصلی: /runjob owner | sub | usage | backup"""
    if m.from_user.id not in OWNER_IDS:
        return
    name = (m.text or "").split()[1:2]
    if not name or name[0] not in JOBS:
        await m.answer("استفاده: /runjob owner | sub | usage | backup")
        return
    await JOBS[name[0]](m.bot)
    await m.answer("انجام شد ✅")


async def scheduler_loop(bot: Bot):
    keys = ("owner_report", "midnight", "backup")
    while True:
        try:
            now = datetime.now(_TEHRAN)
            for job, mark in jobs_due(now, {k: get_setting("job_" + k) for k in keys}):
                set_setting("job_" + job, mark)
                if job == "owner_report":
                    await send_owner_report(bot, now.date())
                elif job == "midnight":
                    await send_sub_reports(bot, now.date() - timedelta(days=1))
                    await run_usage_reports(bot)
                else:
                    await send_weekly_backup(bot)
        except Exception:
            logging.exception("scheduler error")
        await asyncio.sleep(20)


async def main():
    logging.basicConfig(level=logging.INFO)
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.include_router(router)
    tasks = [
        asyncio.create_task(expiry_loop(bot)),
        asyncio.create_task(usage_alert_loop(bot)),
        asyncio.create_task(reminder_loop(bot)),
        asyncio.create_task(pending_alert_loop(bot)),
        asyncio.create_task(scheduler_loop(bot)),
    ]
    await start_web(bot)
    if WEBAPP_URL:
        try:
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(text="Mini App", web_app=WebAppInfo(url=WEBAPP_URL))
            )
        except Exception:
            logging.exception("set menu button failed")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
