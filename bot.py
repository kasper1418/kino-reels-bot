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
from telethon.tl.types import DocumentAttributeVideo, ReplyInlineMarkup

import analyze
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
DAILY_LIMIT = max(1, _env_int("DAILY_LIMIT", 3))   # bir odamga kuniga nechta kino
QUEUE_MAX = max(1, _env_int("QUEUE_MAX", 5))       # navbatda eng ko'pi bilan nechta kino
ADMINS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.strip()}
LIMITS_FILE = Path(os.getenv("LIMITS_FILE", str(Path(SESSION).parent / "limits.json")))
ALLOWED = {int(x) for x in os.getenv("ALLOWED_USERS", "").replace(" ", "").split(",") if x.strip()}

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".ts", ".wmv",
              ".flv", ".mpg", ".mpeg", ".3gp", ".vob"}

# ----------------------------------------------------------- kunlik cheklov

def _today() -> str:
    return datetime.now(timezone(timedelta(hours=5))).strftime("%Y-%m-%d")  # Toshkent vaqti


def _load_usage() -> dict:
    try:
        data = json.loads(LIMITS_FILE.read_text("utf-8"))
        if data.get("date") == _today():
            return data
    except (OSError, ValueError):
        pass
    return {"date": _today(), "users": {}}


def used_today(uid: int) -> int:
    return int(_load_usage()["users"].get(str(uid), 0))


def left_today(uid: int) -> int:
    return max(0, DAILY_LIMIT - used_today(uid))


def add_usage(uid: int, delta: int) -> None:
    if uid in ADMINS:
        return
    data = _load_usage()
    data["users"][str(uid)] = max(0, int(data["users"].get(str(uid), 0)) + delta)
    try:
        LIMITS_FILE.parent.mkdir(parents=True, exist_ok=True)
        LIMITS_FILE.write_text(json.dumps(data), "utf-8")
    except OSError as e:
        log.warning("Limitni saqlab bo'lmadi: %s", e)


HELP = (
    "🎬 Salom! Menga kino yuboring — eng qiziq joylaridan {count} tagacha "
    "vertikal (9:16) reels tayyorlab beraman.\n\n"
    "• Kinoni video yoki fayl qilib yuboring (MP4, MKV, AVI…), 2 GB gacha.\n"
    "• Kinoni yuborgach, bot <b>kino kodi</b> va <b>matn</b> so'raydi (ixtiyoriy). Yozsangiz, "
    "matn videoning tepasida, kod pastida chiroyli shriftda chiqadi. Ba'zi so'zlarni rangli qilish uchun "
    "<code>*so'z*</code> (sariq) yoki <code>[so'z]</code> (qizil) deb yozing.\n"
    "• Reels uzunligi hozir: {length} soniya. O'zgartirish: /uzunlik 30 "
    "(15–90 oralig'ida).\n"
    "• Har kuni {daily} ta kino ishlata olasiz.\n\n"
    "Sizning ID: <code>{uid}</code>"
)

client: TelegramClient
queue: asyncio.Queue
busy = False
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
    cancel: threading.Event = field(default_factory=threading.Event)
    confirming: bool = False      # "Rostan bekor qilasizmi?" so'ralmoqda
    refunded: bool = False        # limit qaytarilganmi
    last_text: str = ""
    dl_task: object = None


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
    return not ALLOWED or uid in ALLOWED


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
    if not allowed(uid):
        await event.reply(f"Kechirasiz, bu bot shaxsiy.\nSizning ID: <code>{uid}</code>", parse_mode="html")
        return
    await event.reply(
        HELP.format(count=MAX_CLIPS, length=user_len.get(uid, DEFAULT_LEN), uid=uid, daily=DAILY_LIMIT),
        parse_mode="html",
    )


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
    if uid in pending:
        text = (event.raw_text or "").strip()
        if pending[uid]["step"] == "count":
            if text.isdigit() and 1 <= int(text) <= MAX_CLIPS:
                await set_count(uid, int(text))
            else:
                await event.reply(f"1 dan {MAX_CLIPS} gacha raqam yozing yoki tugmani bosing.")
            return
        await advance(uid, text[:120] if text else None)
        return
    await event.reply("Menga kino faylini yuboring 🎬  Yordam: /start")


async def on_media(event):
    msg = event.message
    uid = event.sender_id
    if not allowed(uid):
        await event.reply(f"Kechirasiz, bu bot shaxsiy.\nSizning ID: <code>{uid}</code>", parse_mode="html")
        return
    if not is_movie(msg):
        await event.reply("Bu video fayl emas. Iltimos, kinoni video yoki MP4/MKV fayl qilib yuboring.")
        return

    if uid not in ADMINS and left_today(uid) <= 0:
        await event.reply(
            f"⛔ Bugungi limit tugadi: kuniga {DAILY_LIMIT} ta kino. Ertaga yana urinib ko'ring "
            "(yangi kun Toshkent vaqti bilan yarim tundan boshlanadi)."
        )
        return
    if queue.qsize() >= QUEUE_MAX:
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
                    "code": None, "title": None}
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


async def enqueue(uid: int):
    """Kod va matn olingach, ishni navbatga qo'yadi."""
    p = pending.pop(uid, None)
    if not p:
        return
    status = p["status"]
    if uid not in ADMINS and left_today(uid) <= 0:
        await status.edit(
            f"⛔ Bugungi limit tugadi: kuniga {DAILY_LIMIT} ta kino. Ertaga yana urinib ko'ring "
            "(yangi kun Toshkent vaqti bilan yarim tundan boshlanadi).", buttons=NO_BUTTONS)
        return
    if queue.qsize() >= QUEUE_MAX:
        await status.edit("⏳ Hozir navbat to'la. Bir necha daqiqadan keyin kinoni qayta yuboring.", buttons=NO_BUTTONS)
        return
    job = Job(p["chat_id"], uid, p["msg"], status, user_len.get(uid, DEFAULT_LEN),
              p["title"], p["code"], p.get("count", CLIP_COUNT))
    jobs[(p["chat_id"], status.id)] = job
    await queue.put(job)
    add_usage(uid, 1)
    pos = queue.qsize() + (1 if busy else 0)
    if pos > 1:
        text = f"⏳ Navbatdasiz: {pos}-o'rin. Oldingi kino tugashi bilan boshlayman."
    elif uid not in ADMINS:
        text = f"⏳ Qabul qilindi. Bugun yana {left_today(uid)} ta kino yuborishingiz mumkin."
    else:
        text = "⏳ Qabul qilindi."
    job.last_text = text
    await status.edit(text, buttons=cancel_button(job))


ASK_COUNT = (
    "3️⃣ Nechta <b>variant</b> (reels) kerak?\n"
    "Bot kinoning eng qiziq joylaridan shuncha reels tayyorlaydi. 1 dan 5 gacha tanlang."
)


def count_buttons():
    return [[Button.inline(str(i), f"n{i}".encode()) for i in range(1, MAX_CLIPS + 1)]]


async def advance(uid: int, value: str | None):
    """Navbatdagi bosqichga o'tadi: kod → matn → variantlar soni → navbat."""
    p = pending.get(uid)
    if not p:
        return
    if p["step"] == "code":
        p["code"] = value
        p["step"] = "title"
        await p["status"].edit(ASK_TITLE, buttons=[Button.inline("⏭ O'tkazib yuborish", b"skip")], parse_mode="html")
    elif p["step"] == "title":
        p["title"] = value
        p["step"] = "count"
        await p["status"].edit(ASK_COUNT, buttons=count_buttons(), parse_mode="html")


async def set_count(uid: int, n: int):
    p = pending.get(uid)
    if not p or p["step"] != "count":
        return
    p["count"] = n
    await enqueue(uid)


async def on_count(event):
    uid = event.sender_id
    p = pending.get(uid)
    if not allowed(uid) or not p or p["step"] != "count":
        await event.answer("Bu so'rov eskirgan. Kinoni qayta yuboring.", alert=True)
        return
    await event.answer()
    await set_count(uid, int(event.data[1:]))


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


async def process(job: Job):
    st = Status(job)
    jobdir = WORK_DIR / f"{job.chat_id}_{job.msg.id}"
    jobdir.mkdir(parents=True, exist_ok=True)
    try:
        src = jobdir / f"kino{job.msg.file.ext or '.mp4'}"

        async def dl_progress(cur, total):
            pct = cur * 100 // total if total else 0
            await st.set(f"📥 Kino yuklab olinmoqda: {pct}%  ({mb(cur)} / {mb(total)} MB)")

        await st.set("📥 Kino yuklab olinmoqda…", force=True)
        job.dl_task = asyncio.create_task(
            client.download_media(job.msg, file=str(src), progress_callback=dl_progress))
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
                textoverlay.make_overlay, job.title, job.code, top_h, jobdir / "overlay.png")

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


async def worker():
    global busy
    while True:
        job: Job = await queue.get()
        busy = True
        try:
            if job.cancel.is_set():          # navbatda turganida bekor qilingan
                continue
            await process(job)
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
            busy = False
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

    queue = asyncio.Queue()
    client = TelegramClient(SESSION, int(os.environ["API_ID"]), os.environ["API_HASH"])
    client.parse_mode = "html"

    private = lambda e: e.is_private  # noqa: E731
    client.add_event_handler(on_start, events.NewMessage(incoming=True, pattern=r"^/(start|help)", func=private))
    client.add_event_handler(on_length, events.NewMessage(incoming=True, pattern=r"^/uzunlik(?:@\w+)?(?:\s+(\d+))?\s*$", func=private))
    client.add_event_handler(on_media, events.NewMessage(
        incoming=True, func=lambda e: e.is_private and e.message.file is not None))
    client.add_event_handler(on_text, events.NewMessage(
        incoming=True, func=lambda e: e.is_private and e.message.file is None
        and not (e.raw_text or "").startswith("/")))

    client.add_event_handler(on_skip, events.CallbackQuery(data=b"skip"))
    client.add_event_handler(on_count, events.CallbackQuery(pattern=rb"^n[1-5]$"))
    client.add_event_handler(on_cancel_ask, events.CallbackQuery(pattern=rb"^c:\d+$"))
    client.add_event_handler(on_cancel_yes, events.CallbackQuery(pattern=rb"^cy:\d+$"))
    client.add_event_handler(on_cancel_no, events.CallbackQuery(pattern=rb"^cn:\d+$"))

    await client.start(bot_token=os.environ["BOT_TOKEN"])
    me = await client.get_me()
    log.info("Bot ishga tushdi: @%s  (reels: %d ta × %d s)", me.username, CLIP_COUNT, DEFAULT_LEN)
    if not ALLOWED:
        log.warning("ALLOWED_USERS bo'sh — botdan hamma foydalana oladi")

    asyncio.create_task(worker())
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
