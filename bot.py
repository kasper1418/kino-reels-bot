"""
Kino Reels Bot — Telegram'ga kino tashlaysiz, bot eng qiziq joylaridan
10 ta vertikal reels tayyorlab qaytaradi.

Ishga tushirish:  python bot.py   (sozlamalar .env faylida)
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from telethon import Button, TelegramClient, events
from telethon.errors import FloodWaitError, MessageNotModifiedError
from telethon.tl.functions.bots import SetBotCommandsRequest
from telethon.tl.types import (BotCommand, BotCommandScopeDefault, DocumentAttributeVideo,
                               ReplyInlineMarkup)

import admin
import analyze
import plans
import render
import textoverlay

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("telethon").setLevel(logging.WARNING)
log = logging.getLogger("kino-reels")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


CLIP_COUNT = max(1, min(_env_int("CLIP_COUNT", 5), 5))   # standart variant soni (1–5)
MAX_CLIPS = 5
DEFAULT_LEN = max(15, min(_env_int("CLIP_LEN", 40), 90))
ZOOM = float(os.getenv("KADR_ZOOM", "1.3") or 1.3)
WORK_DIR = Path(os.getenv("WORK_DIR", "data/work"))
SESSION = os.getenv("SESSION_PATH", "data/bot")
QUEUE_MAX = max(1, _env_int("QUEUE_MAX", 5))       # navbatda eng ko'pi bilan nechta kino
WORKERS = max(1, min(_env_int("WORKERS", 1), 10))  # bir vaqtda nechta kino ishlanadi
PREFETCH = max(0, min(_env_int("PREFETCH", 1), 3))  # navbatdagi nechta kinoni oldindan yuklab qo'yish (0 = o'chiq)
ADMINS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.strip()}
ALLOWED = {int(x) for x in os.getenv("ALLOWED_USERS", "").replace(" ", "").split(",") if x.strip()}

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".ts", ".wmv",
              ".flv", ".mpg", ".mpeg", ".3gp", ".vob"}

# ----------------------------------------------------------- kunlik cheklov (tarifga qarab)

DB_FILE = Path(os.getenv("DB_FILE", str(Path(SESSION).parent / "bot.db")))


def left_today(uid: int) -> int:
    return plans.left_today(uid)


def add_usage(uid: int, delta: int) -> None:
    if uid in ADMINS:
        return
    plans.add_usage(uid, delta)


def max_clips(uid: int) -> int:
    return MAX_CLIPS if uid in ADMINS else min(MAX_CLIPS, plans.info(uid)["clips"])


def daily_of(uid: int) -> int:
    return plans.info(uid)["daily"]


def som(n: int) -> str:
    return f"{n:,}".replace(",", " ")


HELP = (
    "🎬 <b>ReelCut — kinodan reels yasaydigan bot</b>\n\n"
    "Uzun kinoni yuborasiz, men uning <b>eng qiziq joylarini topib</b>, Instagram Reels, TikTok va "
    "YouTube Shorts uchun tayyor <b>vertikal (9:16) videolar</b> qirqib beraman ✂️🔥\n\n"
    "━━━━━━━━━━━━━━\n"
    "📌 <b>Qanday ishlaydi?</b>\n\n"
    "1️⃣ 🎞 Kinoni shu yerga <b>yuboring</b> (MP4, MKV, AVI… 2 GB gacha)\n"
    "2️⃣ 🔢 <b>Kino kodi</b> yozing — videoning pastida chiqadi\n"
    "3️⃣ ✍️ <b>Matn</b> yozing — videoning tepasida chiqadi\n"
    "4️⃣ 🎯 <b>1–{count} ta variant</b> tanlang\n"
    "5️⃣ 🚀 Tayyor reelslarni oling!\n\n"
    "━━━━━━━━━━━━━━\n"
    "✨ <b>Imkoniyatlar</b>\n\n"
    "🎨 Matndagi so'zni rangli qiling: <code>*so'z*</code> — 🟡 sariq, <code>[so'z]</code> — 🔴 qizil\n"
    "🖤 Tepa va pastda toza qora fon, ortiqcha yozuvlarsiz\n"
    "⏱ Reels uzunligi: <b>{length} soniya</b> (⏱ Uzunlik tugmasi orqali o'zgartiring)\n\n"
    "━━━━━━━━━━━━━━\n"
    "💎 Tarifingiz: <b>{plan}</b> — kuniga <b>{daily}</b> ta kino\n"
    "Ko'proq kerak bo'lsa: /tarif\n\n"
    "👇 <b>Boshlash uchun kinoni yuboring!</b>"
)

client: TelegramClient
queue: asyncio.PriorityQueue
seq = 0
active: dict[int, tuple] = {}   # id(job) -> (job, boshlangan vaqt): hozir ishlanayotganlar
user_len: dict[int, int] = {}
jobs: dict[tuple, "Job"] = {}   # (chat_id, status_id) -> ish; bekor qilish uchun
pending: dict[int, dict] = {}   # kino yuborilgan, kod/matn kutilmoqda


@dataclass
class Job:
    chat_id: int
    user_id: int
    msg: object        # kino yuborilgan xabar
    status: object     # holat ko'rsatiladigan xabar
    length: int
    title: str | None
    code: str | None = None
    count: int = 5
    code_color: str = "yellow"
    cancel: threading.Event = field(default_factory=threading.Event)
    confirming: bool = False      # "Rostan bekor qilasizmi?" so'ralmoqda
    refunded: bool = False        # limit qaytarilganmi
    last_text: str = ""
    dl_task: object = None
    src: object = None            # yuklangan kino fayli yo'li
    show_progress: bool = False   # yuklash foizi xabarga yozilsinmi (ish boshlangach)
    st: object = None


NO_BUTTONS = ReplyInlineMarkup(rows=[])   # inline tugmalarni to'liq olib tashlaydi


def cancel_button(job: "Job"):
    return [[Button.inline("❌ Bekor qilish", f"c:{job.status.id}".encode())]]


class Status:
    """Xabarni tez-tez tahrirlab, Telegram cheklovlariga tushib qolmaslik uchun.

    Ish davomida xabar tagida «Bekor qilish» tugmasi turadi. Yakuniy xabarlarda
    (final=True) tugma olib tashlanadi.
    """

    def __init__(self, job_or_msg, every: float = 4.0):
        self.job = job_or_msg if isinstance(job_or_msg, Job) else None
        self.msg = job_or_msg.status if self.job else job_or_msg
        self.every = every
        self.text, self.last = "", 0.0

    async def set(self, text: str, force: bool = False, final: bool = False):
        job = self.job
        if job is not None:
            if not final:
                job.last_text = text
                if job.confirming or job.cancel.is_set():
                    return
        now = time.monotonic()
        if text == self.text or (not force and not final and now - self.last < self.every):
            return
        self.text, self.last = text, now
        if final:
            buttons = NO_BUTTONS
        elif job is not None:
            buttons = cancel_button(job)
        else:
            buttons = None
        try:
            await self.msg.edit(text, buttons=buttons)
        except MessageNotModifiedError:
            pass
        except FloodWaitError as e:
            self.last = now + e.seconds
        except Exception as e:  # holat xabari muhim emas — ish to'xtamasin
            log.warning("Holatni yangilab bo'lmadi: %s", e)


def allowed(uid: int) -> bool:
    if uid not in ADMINS and plans.is_blocked(uid):
        return False
    return not ALLOWED or uid in ALLOWED


def deny_text(uid: int) -> str:
    if uid not in ADMINS and plans.is_blocked(uid):
        return "🚫 Siz botdan foydalanishdan cheklangansiz."
    return f"Kechirasiz, bu bot shaxsiy.\nSizning ID: <code>{uid}</code>"


async def seen(event) -> None:
    """Foydalanuvchini bazaga yozib boradi (statistika, qidiruv, tarqatish uchun)."""
    try:
        s = await event.get_sender()
        name = " ".join(filter(None, [getattr(s, "first_name", None), getattr(s, "last_name", None)])) or None
        plans.touch(event.sender_id, getattr(s, "username", None), name)
    except Exception:
        plans.touch(event.sender_id)


def is_movie(msg) -> bool:
    if msg.file is None or msg.gif or msg.sticker:
        return False
    if msg.video:
        return True
    mime = (msg.file.mime_type or "").lower()
    ext = (msg.file.ext or "").lower()
    name_ext = Path(msg.file.name or "").suffix.lower()
    return mime.startswith("video/") or ext in VIDEO_EXTS or name_ext in VIDEO_EXTS


def mb(n: int) -> str:
    return f"{n / 1024 / 1024:.0f}"


# ------------------------------------------------------------------ handlers

async def on_start(event):
    uid = event.sender_id
    await seen(event)
    if not allowed(uid):
        await event.reply(deny_text(uid), parse_mode="html")
        return
    await event.reply(
        HELP.format(count=max_clips(uid), length=user_len.get(uid, DEFAULT_LEN), uid=uid,
                    daily=daily_of(uid), plan=plans.info(uid)["name"]),
        parse_mode="html", buttons=menu_kb(uid),
    )
    if uid in ADMINS:
        await event.reply("⚙️ Siz adminsiz: boshqaruv paneli — /panel", parse_mode="html")


async def on_length(event):
    uid = event.sender_id
    if not allowed(uid):
        return
    arg = event.pattern_match.group(1)
    if not arg:
        await event.reply(f"Hozirgi uzunlik: {user_len.get(uid, DEFAULT_LEN)} soniya.\nMasalan: /uzunlik 30")
        return
    n = int(arg)
    if not 15 <= n <= 90:
        await event.reply("Uzunlik 15 dan 90 soniyagacha bo'lishi kerak.")
        return
    user_len[uid] = n
    await event.reply(f"✅ Endi har bir reels taxminan {n} soniya bo'ladi.")


async def on_text(event):
    uid = event.sender_id
    if not allowed(uid):
        return
    await seen(event)
    if await on_menu(event):
        return
    if await admin.handle_text(event):
        return
    if uid in awaiting and uid not in pending:
        await event.reply("To'lov chekining skrinshotini rasm qilib yuboring (yoki /tarif orqali bekor qiling).")
        return
    if uid in pending:
        text = (event.raw_text or "").strip()
        if pending[uid]["step"] == "count":
            if text.isdigit() and 1 <= int(text) <= max_clips(uid):
                await set_count(uid, int(text))
            else:
                await event.reply(f"1 dan {max_clips(uid)} gacha raqam yozing yoki tugmani bosing.")
            return
        if pending[uid]["step"] == "color":
            await event.reply("Rangni yuqoridagi tugmalardan tanlang 🎨")
            return
        await advance(uid, text[:120] if text else None)
        return
    await event.reply("Menga kino faylini yuboring 🎬  Yordam: /start", buttons=menu_kb(uid))


async def on_media(event):
    msg = event.message
    uid = event.sender_id
    await seen(event)
    if not allowed(uid):
        await event.reply(deny_text(uid), parse_mode="html")
        return
    if await handle_receipt(event):
        return
    if not is_movie(msg):
        await event.reply("Bu video fayl emas. Iltimos, kinoni video yoki MP4/MKV fayl qilib yuboring.")
        return

    if uid not in ADMINS and left_today(uid) <= 0:
        await event.reply(LIMIT_TEXT.format(n=daily_of(uid)), buttons=TARIF_BTN, parse_mode="html")
        return
    if uid not in ADMINS and queue.qsize() >= QUEUE_MAX:
        await event.reply("⏳ Hozir navbat to'la. Bir necha daqiqadan keyin qayta yuboring.")
        return

    size = msg.file.size or 0
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(WORK_DIR).free
    if free < size * 1.2 + 500 * 1024 * 1024:
        await event.reply("❌ Serverda joy yetarli emas. Keyinroq urinib ko'ring.")
        return

    old = pending.pop(uid, None)
    status = await event.reply(
        "🎬 Kino qabul qilindi.\n\n"
        "1️⃣ <b>Kino kodini</b> yozing (masalan: <code>1234</code>). U videoning pastida chiqadi.\n"
        "Kerak bo'lmasa, tugmani bosing.",
        buttons=[Button.inline("⏭ O'tkazib yuborish", b"skip")],
        parse_mode="html",
    )
    pending[uid] = {"step": "code", "msg": msg, "status": status, "chat_id": event.chat_id,
                    "code": None, "title": None, "code_color": "yellow"}
    if old:
        try:
            await old["status"].edit("↩️ Bekor qilindi: yangi kino yubordingiz.", buttons=None)
        except Exception:
            pass


ASK_TITLE = (
    "2️⃣ Endi videoning tepasiga chiqadigan <b>matnni</b> yozing.\n\n"
    "🎨 <b>So'zni rangli qilish:</b>\n"
    "• <code>*so'z*</code> yozsangiz — <b>sariq</b> bo'ladi\n"
    "• <code>[so'z]</code> yozsangiz — <b>qizil</b> bo'ladi\n\n"
    "Misol:\n"
    "<code>Eng *kuchli* jang [ayovsiz] sahnasi</code>\n"
    "Natija: «kuchli» sariq, «ayovsiz» qizil chiqadi.\n\n"
    "Kerak bo'lmasa, tugmani bosing."
)


# ---------------------------------------------------------- navbat va taxminiy vaqt

AVG_DEFAULT = 360.0          # bitta kino o'rtacha necha soniyada tayyor bo'lishi (dastlabki taxmin)
avg_job = AVG_DEFAULT


def fmt_eta(sec: float) -> str:
    mins = max(1, int(round(sec / 60.0)))
    if mins < 2 and sec < 90:
        return "1–2 daqiqa"
    if mins >= 60:
        h, m = divmod(mins, 60)
        return f"{h} soat" + (f" {m} daqiqa" if m else "")
    return f"{mins} daqiqa"


def waiting_jobs() -> list:
    """Navbatdagi (bekor qilinmagan) ishlar, ustuvorlik tartibida."""
    items = sorted(queue._queue, key=lambda t: (t[0], t[1]))  # noqa: SLF001
    return [t[2] for t in items if not t[2].cancel.is_set()]


def _free_times() -> list[float]:
    """Har bir ishchi qachon bo'shashi (soniya): ishlayotganlarniki + bo'sh ishchilar uchun 0."""
    now = time.monotonic()
    times = [max(avg_job * 0.25, avg_job - (now - started)) for _, started in active.values()]
    times += [0.0] * max(0, WORKERS - len(active))
    return sorted(times)


def queue_text(job) -> str:
    """Ish uchun navbat o'rni va taxminiy vaqt matni."""
    w = waiting_jobs()
    idx = w.index(job) if job in w else 0
    ahead_running = len(active) if WORKERS == 1 else 0
    pos = idx + 1 + ahead_running
    free = _free_times()
    wait = 0.0
    for _ in range(idx + 1):             # navbatdagi har bir ish eng erta bo'shagan ishchiga tushadi
        wait = free.pop(0)
        free.append(wait + avg_job)
        free.sort()
    uid = job.user_id
    if wait <= 0:
        text = f"⏳ <b>Qabul qilindi.</b> Hozir boshlanadi.\n✅ Tayyor bo'lishiga taxminan <b>{fmt_eta(avg_job)}</b>."
    else:
        text = (f"⏳ Navbatdasiz: <b>{pos}-o'rin</b>\n"
                f"🕒 Boshlanishiga taxminan <b>{fmt_eta(wait)}</b>\n"
                f"✅ Tayyor bo'lishiga taxminan <b>{fmt_eta(wait + avg_job)}</b>")
    if uid not in ADMINS:
        text += f"\n\nBugun yana {left_today(uid)} ta kino yuborishingiz mumkin."
    return text


async def refresh_queue():
    """Navbat siljiganda kutayotganlarning xabarini yangilaydi."""
    for j in waiting_jobs():
        if j.confirming or j.cancel.is_set():
            continue
        try:
            await Status(j).set(queue_text(j), force=True)
        except Exception:
            pass


def record_duration(sec: float) -> None:
    global avg_job
    avg_job = max(60.0, min(3600.0, avg_job * 0.7 + sec * 0.3))
    try:
        plans.set_setting("avg_job_sec", str(int(avg_job)))
    except Exception:
        pass


async def enqueue(uid: int):
    """Kod va matn olingach, ishni navbatga qo'yadi."""
    p = pending.pop(uid, None)
    if not p:
        return
    status = p["status"]
    if uid not in ADMINS and left_today(uid) <= 0:
        await status.edit(LIMIT_TEXT.format(n=daily_of(uid)), buttons=TARIF_BTN, parse_mode="html")
        return
    if uid not in ADMINS and queue.qsize() >= QUEUE_MAX:
        await status.edit("⏳ Hozir navbat to'la. Bir necha daqiqadan keyin kinoni qayta yuboring.", buttons=NO_BUTTONS)
        return
    job = Job(p["chat_id"], uid, p["msg"], status, user_len.get(uid, DEFAULT_LEN),
              p["title"], p["code"], p.get("count", CLIP_COUNT))
    job.code_color = p.get("code_color", "yellow")
    jobs[(p["chat_id"], status.id)] = job
    global seq
    seq += 1
    await queue.put((0 if uid in ADMINS else plans.info(uid)["prio"], seq, job))
    add_usage(uid, 1)
    text = queue_text(job)
    job.last_text = text
    await status.edit(text, buttons=cancel_button(job))


ASK_COLOR = (
    "🎨 Kino kodi qaysi <b>rangda</b> chiqsin?\n"
    "Videoning pastidagi kichik nishon rangini tanlang."
)


def color_buttons():
    return [
        [Button.inline("🟡 Sariq", b"col:yellow"), Button.inline("🔴 Qizil", b"col:red"),
         Button.inline("⚪ Oq", b"col:white")],
        [Button.inline("🟢 Yashil", b"col:green"), Button.inline("🔵 Ko'k", b"col:blue")],
    ]


ASK_COUNT = (
    "3️⃣ Nechta <b>variant</b> (reels) kerak?\n"
    "Bot kinoning eng qiziq joylaridan shuncha reels tayyorlaydi. 1 dan 5 gacha tanlang."
)


def count_buttons(uid: int):
    return [[Button.inline(str(i), f"n{i}".encode()) for i in range(1, max_clips(uid) + 1)]]


async def advance(uid: int, value: str | None):
    """Navbatdagi bosqichga o'tadi: kod → matn → variantlar soni → navbat."""
    p = pending.get(uid)
    if not p:
        return
    if p["step"] == "code":
        p["code"] = value
        if value:
            p["step"] = "color"
            await p["status"].edit(ASK_COLOR, buttons=color_buttons(), parse_mode="html")
        else:
            p["step"] = "title"
            await p["status"].edit(ASK_TITLE, buttons=[Button.inline("⏭ O'tkazib yuborish", b"skip")], parse_mode="html")
    elif p["step"] == "color":
        p["code_color"] = value or "yellow"
        p["step"] = "title"
        await p["status"].edit(ASK_TITLE, buttons=[Button.inline("⏭ O'tkazib yuborish", b"skip")], parse_mode="html")
    elif p["step"] == "title":
        p["title"] = value
        p["step"] = "count"
        await p["status"].edit(ASK_COUNT, buttons=count_buttons(uid), parse_mode="html")


async def set_count(uid: int, n: int):
    p = pending.get(uid)
    if not p or p["step"] != "count":
        return
    p["count"] = n
    await enqueue(uid)


async def on_color(event):
    uid = event.sender_id
    p = pending.get(uid)
    if not allowed(uid) or not p or p["step"] != "color":
        await event.answer("Bu so'rov eskirgan. Kinoni qayta yuboring.", alert=True)
        return
    await event.answer()
    await advance(uid, event.data.decode().split(":", 1)[1])


async def on_count(event):
    uid = event.sender_id
    p = pending.get(uid)
    if not allowed(uid) or not p or p["step"] != "count":
        await event.answer("Bu so'rov eskirgan. Kinoni qayta yuboring.", alert=True)
        return
    n = int(event.data[1:])
    if n > max_clips(uid):
        await event.answer("Bu tarifda bunchalik variant yo'q. /tarif", alert=True)
        return
    await event.answer()
    await set_count(uid, n)


# ------------------------------------------------------------------ pastki menyu

M_REELS, M_TARIF, M_PROFIL = "🎬 Reels yasash", "💎 Tariflar", "👤 Profil"
M_LEN, M_HELP, M_PANEL = "⏱ Uzunlik", "ℹ️ Yordam", "⚙️ Panel"
MENU_TEXTS = {M_REELS, M_TARIF, M_PROFIL, M_LEN, M_HELP, M_PANEL}


def menu_kb(uid: int):
    rows = [
        [Button.text(M_REELS, resize=True), Button.text(M_TARIF, resize=True)],
        [Button.text(M_PROFIL, resize=True), Button.text(M_LEN, resize=True)],
        [Button.text(M_HELP, resize=True)] + ([Button.text(M_PANEL, resize=True)] if uid in ADMINS else []),
    ]
    return rows


LEN_CHOICES = (20, 30, 40, 60, 90)


async def on_menu(event) -> bool:
    """Pastki menyu tugmalarini qayta ishlaydi. True — xabar menyu tugmasi edi."""
    text = (event.raw_text or "").strip()
    if text not in MENU_TEXTS:
        return False
    uid = event.sender_id
    admin.state.pop(uid, None)           # admin kiritish bosqichi menyu bosilganda tugaydi
    if text == M_REELS:
        await event.reply("🎬 Kinoni shu yerga video yoki fayl (MP4, MKV, AVI…) qilib yuboring, 2 GB gacha.\n"
                          "Keyin kino kodi, matn va variantlar sonini so'rayman.", buttons=menu_kb(uid))
    elif text == M_TARIF:
        await on_tarif(event)
    elif text == M_PROFIL:
        await on_profile(event)
    elif text == M_LEN:
        cur = user_len.get(uid, DEFAULT_LEN)
        await event.reply(
            f"⏱ Har bir reels necha soniya bo'lsin? Hozir: <b>{cur} soniya</b>.",
            buttons=[[Button.inline(("✅ " if n == cur else "") + f"{n} s", f"len:{n}".encode()) for n in LEN_CHOICES]],
            parse_mode="html")
    elif text == M_HELP:
        await on_start(event)
    elif text == M_PANEL and uid in ADMINS:
        await admin.on_panel(event)
    return True


async def on_len_btn(event):
    uid = event.sender_id
    n = int(event.data.decode().split(":", 1)[1])
    if not allowed(uid) or not 15 <= n <= 90:
        await event.answer()
        return
    user_len[uid] = n
    await event.answer(f"{n} soniya")
    try:
        await event.edit(
            f"⏱ Har bir reels necha soniya bo'lsin? Hozir: <b>{n} soniya</b>.",
            buttons=[[Button.inline(("✅ " if k == n else "") + f"{k} s", f"len:{k}".encode()) for k in LEN_CHOICES]],
            parse_mode="html")
    except MessageNotModifiedError:
        pass


COMMANDS = [
    BotCommand("start", "Botni ishga tushirish"),
    BotCommand("tarif", "Tariflar va to'lov"),
    BotCommand("profil", "Mening tarifim va limitim"),
    BotCommand("uzunlik", "Reels uzunligini o'zgartirish"),
]


# ------------------------------------------------------------------ tariflar va to'lov

LIMIT_TEXT = (
    "⛔ Bugungi limit tugadi: tarifingizda kuniga {n} ta kino. Yangi kun Toshkent vaqti bilan "
    "yarim tundan boshlanadi.\n\n💎 Ko'proq kino kerak bo'lsa, tarifni oshiring."
)
TARIF_BTN = [[Button.inline("💎 Tariflar", b"tarif")]]
awaiting: dict[int, int] = {}     # uid -> kvitansiya kutilayotgan to'lov raqami


def tarif_text(uid: int) -> str:
    cur = plans.plan_of(uid)
    P = plans.PLANS
    lines = ["💎 <b>Tariflar</b>\n"]
    for key in ("free", "start", "pro"):
        p = P[key]
        mark = " ✅ (sizniki)" if key == cur else ""
        price = "bepul" if not p["price"] else f"{som(p['price'])} so'm / {plans.PLAN_DAYS} kun"
        extra = ""
        if key == "start":
            extra = "\n   • navbatda oldinroq"
        elif key == "pro":
            extra = "\n   • navbatda birinchi"
        lines.append(
            f"<b>{p['name']}</b>{mark} — {price}\n"
            f"   • kuniga {p['daily']} ta kino\n"
            f"   • har kinodan {p['clips']} tagacha reels{extra}\n"
        )
    if cur != "free":
        lines.append(f"📅 Tarifingiz {plans.fmt_until(plans.until_of(uid))} gacha amal qiladi.")
    return "\n".join(lines)


def tarif_buttons():
    P = plans.PLANS
    return [
        [Button.inline(f"🚀 Start — {som(P['start']['price'])} so'm", b"buy:start")],
        [Button.inline(f"👑 Pro — {som(P['pro']['price'])} so'm", b"buy:pro")],
    ]


async def on_tarif(event):
    uid = event.sender_id
    if not allowed(uid):
        return
    await event.reply(tarif_text(uid), buttons=tarif_buttons(), parse_mode="html")


async def on_tarif_btn(event):
    uid = event.sender_id
    await event.answer()
    await event.reply(tarif_text(uid), buttons=tarif_buttons(), parse_mode="html")


async def on_profile(event):
    uid = event.sender_id
    if not allowed(uid):
        return
    p = plans.info(uid)
    text = (
        f"👤 <b>Profil</b>\nID: <code>{uid}</code>\n"
        f"Tarif: <b>{p['name']}</b>\n"
    )
    if plans.plan_of(uid) != "free":
        text += f"Amal qilish muddati: {plans.fmt_until(plans.until_of(uid))} gacha\n"
    if uid in ADMINS:
        text += "Bugun: cheksiz (admin)\n"
    else:
        text += f"Bugun qoldi: {left_today(uid)} / {p['daily']} ta kino\n"
    await event.reply(text, buttons=[[Button.inline("💎 Tariflar", b"tarif")]], parse_mode="html")


async def on_buy(event):
    uid = event.sender_id
    plan = event.data.decode().split(":", 1)[1]
    if not allowed(uid) or plan not in ("start", "pro"):
        await event.answer()
        return
    card_no, card_holder = plans.card()
    if not card_no or not ADMINS:
        await event.answer("To'lov hozircha sozlanmagan. Keyinroq urinib ko'ring.", alert=True)
        return
    await event.answer()
    pid = plans.new_payment(uid, plan)
    awaiting[uid] = pid
    P = plans.PLANS[plan]
    holder = f"\nKarta egasi: <b>{html.escape(card_holder)}</b>" if card_holder else ""
    await event.reply(
        f"💳 <b>{P['name']}</b> tarifi — <b>{som(P['price'])} so'm</b> ({plans.PLAN_DAYS} kun)\n\n"
        f"1️⃣ Quyidagi kartaga (Uzcard / Humo) aynan shu summani o'tkazing:\n"
        f"<code>{html.escape(card_no)}</code>{holder}\n\n"
        f"2️⃣ To'lov <b>chekining skrinshotini</b> shu yerga rasm qilib yuboring.\n\n"
        f"3️⃣ Admin tekshirib tasdiqlagach, tarif darhol yoqiladi va sizga xabar keladi.\n\n"
        f"To'lov raqami: #{pid}",
        buttons=[[Button.inline("✖️ Bekor qilish", f"payx:{pid}".encode())]],
        parse_mode="html",
    )


async def on_pay_cancel(event):
    uid = event.sender_id
    pid = int(event.data.decode().split(":", 1)[1])
    pay = plans.get_payment(pid)
    if pay and pay[1] == uid and pay[4] == "new":
        plans.set_payment(pid, "cancel")
        awaiting.pop(uid, None)
        await event.answer("Bekor qilindi")
        await event.edit("✖️ To'lov bekor qilindi.", buttons=NO_BUTTONS)
    else:
        await event.answer()


async def handle_receipt(event) -> bool:
    """Foydalanuvchi to'lov kutayotgan bo'lsa, rasmni admin(lar)ga yuboradi."""
    msg = event.message
    uid = event.sender_id
    pid = awaiting.get(uid)
    if pid is None:
        return False
    is_img = bool(msg.photo) or (msg.file and (msg.file.mime_type or "").startswith("image/"))
    if not is_img:
        await event.reply("To'lov chekining skrinshotini rasm qilib yuboring yoki to'lovni bekor qiling.")
        return True
    pay = plans.get_payment(pid)
    if not pay or pay[4] != "new":
        awaiting.pop(uid, None)
        return False
    plans.set_payment(pid, "wait")
    plans.set_receipt(pid, event.chat_id, msg.id)
    awaiting.pop(uid, None)
    sender = await event.get_sender()
    name = html.escape(" ".join(filter(None, [getattr(sender, "first_name", ""), getattr(sender, "last_name", "")])) or "—")
    uname = f"@{sender.username}" if getattr(sender, "username", None) else "—"
    P = plans.PLANS[pay[2]]
    caption = (
        f"💳 To'lov #{pid}\nFoydalanuvchi: {name} ({uname})\nID: <code>{uid}</code>\n"
        f"Tarif: <b>{P['name']}</b>\nSumma: <b>{som(pay[3])} so'm</b>"
    )
    kb = [[Button.inline("✅ Tasdiqlash", f"pok:{pid}".encode()), Button.inline("❌ Rad etish", f"pno:{pid}".encode())]]
    sent = 0
    for aid in ADMINS:
        try:
            await client.send_file(aid, msg.media, caption=caption, buttons=kb, parse_mode="html")
            sent += 1
        except Exception as e:
            log.warning("Adminga yuborib bo'lmadi (%s): %s", aid, e)
    if sent:
        await event.reply(f"✅ Chek qabul qilindi (#{pid}). Admin tekshirib, tez orada tarifni yoqadi.")
    else:
        plans.set_payment(pid, "new")
        awaiting[uid] = pid
        await event.reply("❌ Chekni adminga yetkazib bo'lmadi. Birozdan keyin qayta yuboring.")
    return True


async def on_pay_decide(event):
    aid = event.sender_id
    if aid not in ADMINS:
        await event.answer("Faqat admin uchun.", alert=True)
        return
    action, pid_s = event.data.decode().split(":", 1)
    pid = int(pid_s)
    pay = plans.get_payment(pid)
    if not pay or pay[4] not in ("wait", "new"):
        await event.answer("Bu to'lov allaqachon hal qilingan.", alert=True)
        return
    uid, plan = pay[1], pay[2]
    P = plans.PLANS[plan]
    if action == "pok":
        plans.set_payment(pid, "ok", aid)
        until = plans.grant(uid, plan)
        await event.answer("Tasdiqlandi")
        try:
            await event.edit(f"✅ Tasdiqlandi: #{pid} — {P['name']} ({som(pay[3])} so'm)", buttons=NO_BUTTONS)
        except Exception:
            pass
        try:
            await client.send_message(
                uid,
                f"🎉 To'lov tasdiqlandi! <b>{P['name']}</b> tarifi {plans.fmt_until(until)} gacha yoqildi.\n"
                f"Endi kuniga {P['daily']} ta kino yubora olasiz. Kinoni yuboring 🎬",
                parse_mode="html",
            )
        except Exception:
            pass
    else:
        plans.set_payment(pid, "no", aid)
        await event.answer("Rad etildi")
        try:
            await event.edit(f"❌ Rad etildi: #{pid}", buttons=NO_BUTTONS)
        except Exception:
            pass
        try:
            await client.send_message(
                uid,
                f"❌ To'lov #{pid} tasdiqlanmadi. Agar to'lov qilgan bo'lsangiz, /tarif orqali qayta urinib, "
                "to'g'ri chek yuboring.",
            )
        except Exception:
            pass


async def on_grant(event):
    if event.sender_id not in ADMINS:
        return
    parts = (event.raw_text or "").split()
    if len(parts) < 3 or not parts[1].isdigit() or parts[2] not in ("start", "pro", "free"):
        await event.reply("Ishlatilishi: /grant ID start|pro|free [kun]")
        return
    uid, plan = int(parts[1]), parts[2]
    days = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else plans.PLAN_DAYS
    if plan == "free":
        plans.grant(uid, "free", 0)
        await event.reply(f"✅ {uid}: Bepul tarifga o'tkazildi.")
        return
    until = plans.grant(uid, plan, days)
    await event.reply(f"✅ {uid}: {plans.PLANS[plan]['name']} — {plans.fmt_until(until)} gacha.")
    try:
        await client.send_message(uid, f"🎁 Sizga <b>{plans.PLANS[plan]['name']}</b> tarifi berildi "
                                       f"({plans.fmt_until(until)} gacha).", parse_mode="html")
    except Exception:
        pass


async def on_stats(event):
    if event.sender_id not in ADMINS:
        return
    s = plans.stats()
    await event.reply(
        f"📊 Foydalanuvchi (tarif olganlar): {s['users']}\nFaol pullik: {s['paid']}\n"
        f"Bugun kinolar: {s['films_today']}\nJami tushum: {som(s['income'])} so'm"
    )


# ------------------------------------------------------------- bekor qilish

def _job_from_event(event):
    sid = int(event.data.split(b":")[1])
    job = jobs.get((event.chat_id, sid))
    if job is None or job.user_id != event.sender_id:
        return None
    return job


async def on_cancel_ask(event):
    """«Bekor qilish» bosildi — tasdiq so'raymiz (ish davom etaveradi)."""
    job = _job_from_event(event)
    if job is None or job.cancel.is_set():
        await event.answer("Bu ish allaqachon tugagan.", alert=True)
        return
    await event.answer()
    job.confirming = True
    sid = job.status.id
    await job.status.edit(
        "❓ <b>Rostan bekor qilasizmi?</b>\n\n"
        "Ish to'xtatiladi, kino o'chiriladi. Limitingiz sarflanmaydi.",
        buttons=[[Button.inline("✅ Ha, bekor qil", f"cy:{sid}".encode()),
                  Button.inline("↩️ Yo'q, davom et", f"cn:{sid}".encode())]],
        parse_mode="html",
    )


async def on_cancel_no(event):
    job = _job_from_event(event)
    if job is None:
        await event.answer("Bu ish allaqachon tugagan.", alert=True)
        return
    await event.answer()
    job.confirming = False
    await job.status.edit(job.last_text or "⏳ Davom etmoqda…", buttons=cancel_button(job))


async def on_cancel_yes(event):
    job = _job_from_event(event)
    if job is None or job.cancel.is_set():
        await event.answer("Bu ish allaqachon tugagan.", alert=True)
        return
    await event.answer()
    job.cancel.set()
    job.confirming = False
    if job.dl_task is not None:
        job.dl_task.cancel()
    if not job.refunded:
        job.refunded = True
        add_usage(job.user_id, -1)
    jobs.pop((job.chat_id, job.status.id), None)
    await job.status.edit(
        "❌ Bekor qilindi. Limitingiz sarflanmadi.\n\n🎬 Boshqa kino yuborishingiz mumkin.",
        buttons=NO_BUTTONS,
    )
    if id(job) not in active:                # navbatda turgan edi: oldindan yuklangan fayl o'chiriladi
        asyncio.create_task(cleanup_job(job))
    asyncio.create_task(refresh_queue())     # orqadagilarning o'rni yangilansin


async def on_skip(event):
    uid = event.sender_id
    if not allowed(uid) or uid not in pending or pending[uid]["step"] == "count":
        await event.answer("Bu so'rov eskirgan. Kinoni qayta yuboring.", alert=True)
        return
    await event.answer()
    await advance(uid, None)


# -------------------------------------------------------------------- worker

async def send_reel(job: Job, path: Path, thumb: Path | None, caption: str, duration: float):
    await client.send_file(
        job.chat_id, str(path), caption=caption,
        thumb=str(thumb) if thumb else None,
        supports_streaming=True, reply_to=job.msg.id,
        attributes=[DocumentAttributeVideo(
            duration=int(round(duration)), w=render.OUT_W, h=render.OUT_H, supports_streaming=True,
        )],
    )


def job_dir(job: Job) -> Path:
    return WORK_DIR / f"{job.chat_id}_{job.msg.id}"


def job_status(job: Job) -> "Status":
    if job.st is None:
        job.st = Status(job)
    return job.st


def start_download(job: Job) -> None:
    """Kinoni yuklashni boshlaydi (navbat kelmasdan oldin ham chaqirilishi mumkin)."""
    jobdir = job_dir(job)
    jobdir.mkdir(parents=True, exist_ok=True)
    job.src = jobdir / f"kino{job.msg.file.ext or '.mp4'}"

    async def dl_progress(cur, total):
        if job.show_progress:
            pct = cur * 100 // total if total else 0
            await job_status(job).set(f"📥 Kino yuklab olinmoqda: {pct}%  ({mb(cur)} / {mb(total)} MB)")

    job.dl_task = asyncio.create_task(
        client.download_media(job.msg, file=str(job.src), progress_callback=dl_progress))


async def cleanup_job(job: Job) -> None:
    """Navbatda turib bekor qilingan ishning yuklangan faylini o'chiradi."""
    t = job.dl_task
    if t is not None:
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass
    shutil.rmtree(job_dir(job), ignore_errors=True)


PREFETCH_TICK = 1.0


async def prefetcher():
    """Navbatdagi dastlabki PREFETCH ta kinoni oldindan yuklab qo'yadi."""
    while True:
        await asyncio.sleep(PREFETCH_TICK)
        if PREFETCH <= 0:
            continue
        try:
            for j in waiting_jobs()[:PREFETCH]:
                if j.dl_task is not None or j.src is not None or j.cancel.is_set():
                    continue
                need = (j.msg.file.size or 0) * 1.2 + 500 * 1024 * 1024
                if shutil.disk_usage(WORK_DIR).free < need:
                    continue                 # joy kam: oldindan yuklamaymiz, navbati kelganda yuklanadi
                start_download(j)
        except Exception:
            log.exception("Oldindan yuklashda xatolik")


async def process(job: Job):
    st = job_status(job)
    jobdir = job_dir(job)
    jobdir.mkdir(parents=True, exist_ok=True)
    try:
        if job.dl_task is None:
            start_download(job)
        src = job.src
        job.show_progress = True
        if not job.dl_task.done():
            await st.set("📥 Kino yuklab olinmoqda…", force=True)
        else:
            await st.set("📦 Kino tayyor, tahlil boshlanmoqda…", force=True)
        try:
            await job.dl_task
        except asyncio.CancelledError:
            if job.cancel.is_set():
                raise analyze.Cancelled()
            raise
        finally:
            job.dl_task = None
        analyze.check_cancel(job.cancel)

        loop = asyncio.get_running_loop()

        def an_progress(text: str):  # boshqa oqimdan chaqiriladi
            asyncio.run_coroutine_threadsafe(st.set(f"🔍 {text}…", force=True), loop)

        t0 = time.monotonic()
        res = await asyncio.to_thread(
            analyze.find_highlights, str(src), job.count, job.length, an_progress, job.cancel,
        )
        log.info("Tahlil %.0f s, %d ta joy topildi", time.monotonic() - t0, len(res.clips))
        if not res.clips:
            raise RuntimeError("kinodan mos joy topilmadi")

        overlay = None
        if job.title or job.code:
            top_h = render.top_bar_height(res.info, ZOOM)
            overlay = await asyncio.to_thread(
                textoverlay.make_overlay, job.title, job.code, top_h, jobdir / "overlay.png", job.code_color)

        total = len(res.clips)
        for i, clip in enumerate(res.clips, 1):
            analyze.check_cancel(job.cancel)
            await st.set(f"✂️ Reels tayyorlanmoqda: {i} / {total}", force=True)
            out = jobdir / f"reel_{i:02d}.mp4"
            await asyncio.to_thread(
                render.render_clip, res.info, clip, out, overlay, ZOOM, 900, job.cancel,
            )
            thumb = await asyncio.to_thread(
                render.make_thumb, out, clip.duration * 0.3, jobdir / f"thumb_{i:02d}.jpg",
            )
            caption = ""  # reelsda ortiqcha yozuv yo'q
            analyze.check_cancel(job.cancel)
            await send_reel(job, out, thumb, caption, clip.duration)
            out.unlink(missing_ok=True)

        await st.set(
            f"✅ Tayyor! {total} ta reels yuborildi.\n"
            "1-variant — bot eng qiziq deb topgan joy, keyingilari kamayib boradi.\n\n"
            "🎬 Boshqa kino yuborishingiz mumkin.",
            force=True, final=True,
        )
    finally:
        shutil.rmtree(jobdir, ignore_errors=True)


async def wait_for_space(job: Job) -> bool:
    """Parallel ishlarda disk to'lib qolmasligi uchun: joy bo'shaguncha kutadi."""
    size = job.msg.file.size or 0
    need = size * 1.2 + 500 * 1024 * 1024
    for _ in range(90):                       # ~30 daqiqagacha
        if job.cancel.is_set() or shutil.disk_usage(WORK_DIR).free >= need:
            return True
        await asyncio.sleep(20)
    return False


async def worker():
    while True:
        _, _, job = await queue.get()
        started = time.monotonic()
        active[id(job)] = (job, started)
        asyncio.create_task(refresh_queue())
        try:
            if job.cancel.is_set():          # navbatda turganida bekor qilingan
                await cleanup_job(job)
                continue
            if job.dl_task is None and not await wait_for_space(job):
                raise RuntimeError("serverda joy yetarli emas, keyinroq urinib ko'ring")
            started = time.monotonic()
            active[id(job)] = (job, started)
            await process(job)
            if not job.cancel.is_set():
                record_duration(time.monotonic() - started)
        except analyze.Cancelled:
            log.info("Ish bekor qilindi: %s", job.status.id)
        except Exception as e:
            log.exception("Ish bajarilmadi")
            if not job.refunded:
                job.refunded = True
                add_usage(job.user_id, -1)   # muvaffaqiyatsiz bo'lsa, limit qaytariladi
            await Status(job).set(
                f"❌ Xatolik yuz berdi: {html.escape(str(e))[:300]}\n\nLimitingiz sarflanmadi. "
                "Kinoni qayta yuborib ko'ring.", force=True, final=True)
        finally:
            jobs.pop((job.chat_id, job.status.id), None)
            active.pop(id(job), None)
            queue.task_done()


# ---------------------------------------------------------------------- main

async def main():
    global client, queue
    missing = [k for k in ("API_ID", "API_HASH", "BOT_TOKEN") if not os.getenv(k)]
    if missing:
        raise SystemExit(f".env faylida yetishmayapti: {', '.join(missing)}")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise SystemExit("ffmpeg o'rnatilmagan. Ubuntu: sudo apt install ffmpeg")

    shutil.rmtree(WORK_DIR, ignore_errors=True)  # oldingi ishdan qolgan fayllar
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    Path(SESSION).parent.mkdir(parents=True, exist_ok=True)

    plans.init(DB_FILE)
    global avg_job
    try:
        avg_job = float(plans.get_setting("avg_job_sec", "") or AVG_DEFAULT)
    except ValueError:
        avg_job = AVG_DEFAULT
    queue = asyncio.PriorityQueue()
    client = TelegramClient(SESSION, int(os.environ["API_ID"]), os.environ["API_HASH"])
    client.parse_mode = "html"
    admin.init(client, ADMINS, set(ADMINS), NO_BUTTONS)
    admin.register(client)

    private = lambda e: e.is_private  # noqa: E731
    client.add_event_handler(on_start, events.NewMessage(incoming=True, pattern=r"^/(start|help)", func=private))
    client.add_event_handler(on_length, events.NewMessage(incoming=True, pattern=r"^/uzunlik(?:@\w+)?(?:\s+(\d+))?\s*$", func=private))
    client.add_event_handler(on_tarif, events.NewMessage(incoming=True, pattern=r"^/(tarif|tariflar|premium)", func=private))
    client.add_event_handler(on_profile, events.NewMessage(incoming=True, pattern=r"^/(profil|balans|me)", func=private))
    client.add_event_handler(on_grant, events.NewMessage(incoming=True, pattern=r"^/grant", func=private))
    client.add_event_handler(on_stats, events.NewMessage(incoming=True, pattern=r"^/stats", func=private))
    client.add_event_handler(on_media, events.NewMessage(
        incoming=True, func=lambda e: e.is_private and e.message.file is not None))
    client.add_event_handler(on_text, events.NewMessage(
        incoming=True, func=lambda e: e.is_private and e.message.file is None
        and not (e.raw_text or "").startswith("/")))

    client.add_event_handler(on_tarif_btn, events.CallbackQuery(data=b"tarif"))
    client.add_event_handler(on_buy, events.CallbackQuery(pattern=rb"^buy:(start|pro)$"))
    client.add_event_handler(on_pay_cancel, events.CallbackQuery(pattern=rb"^payx:\d+$"))
    client.add_event_handler(on_pay_decide, events.CallbackQuery(pattern=rb"^p(ok|no):\d+$"))
    client.add_event_handler(on_len_btn, events.CallbackQuery(pattern=rb"^len:\d+$"))
    client.add_event_handler(on_skip, events.CallbackQuery(data=b"skip"))
    client.add_event_handler(on_color, events.CallbackQuery(pattern=rb"^col:(yellow|red|white|green|blue)$"))
    client.add_event_handler(on_count, events.CallbackQuery(pattern=rb"^n[1-5]$"))
    client.add_event_handler(on_cancel_ask, events.CallbackQuery(pattern=rb"^c:\d+$"))
    client.add_event_handler(on_cancel_yes, events.CallbackQuery(pattern=rb"^cy:\d+$"))
    client.add_event_handler(on_cancel_no, events.CallbackQuery(pattern=rb"^cn:\d+$"))

    await client.start(bot_token=os.environ["BOT_TOKEN"])
    try:
        await client(SetBotCommandsRequest(scope=BotCommandScopeDefault(), lang_code="", commands=COMMANDS))
    except Exception as e:
        log.warning("Buyruqlar menyusini o'rnatib bo'lmadi: %s", e)
    me = await client.get_me()
    log.info("Bot ishga tushdi: @%s  (reels: %d ta × %d s, ishchilar: %d, oldindan yuklash: %d)", me.username, CLIP_COUNT, DEFAULT_LEN, WORKERS, PREFETCH)
    if not ALLOWED:
        log.warning("ALLOWED_USERS bo'sh — botdan hamma foydalana oladi")

    for _ in range(WORKERS):
        asyncio.create_task(worker())
    asyncio.create_task(prefetcher())
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
