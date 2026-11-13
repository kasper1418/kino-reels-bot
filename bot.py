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
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, MessageNotModifiedError
from telethon.tl.types import DocumentAttributeVideo

import analyze
import render

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("telethon").setLevel(logging.WARNING)
log = logging.getLogger("kino-reels")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


CLIP_COUNT = max(1, min(_env_int("CLIP_COUNT", 10), 20))
DEFAULT_LEN = max(15, min(_env_int("CLIP_LEN", 40), 90))
WATERMARK = os.getenv("WATERMARK", "").strip() or None
FONT_PATH = os.getenv("FONT_PATH", render.DEFAULT_FONT)
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
    "🎬 Salom! Menga kino yuboring — eng qiziq joylaridan {count} ta "
    "vertikal (9:16) reels tayyorlab beraman.\n\n"
    "• Kinoni video yoki fayl qilib yuboring (MP4, MKV, AVI…), 2 GB gacha.\n"
    "• Reels uzunligi hozir: {length} soniya. O'zgartirish: /uzunlik 30 "
    "(15–90 oralig'ida).\n"
    "• Har kuni {daily} ta kino ishlata olasiz.\n\n"
    "Sizning ID: <code>{uid}</code>"
)

client: TelegramClient
queue: asyncio.Queue
busy = False
user_len: dict[int, int] = {}


@dataclass
class Job:
    chat_id: int
    user_id: int
    msg: object        # kino yuborilgan xabar
    status: object     # holat ko'rsatiladigan xabar
    length: int
    title: str | None


class Status:
    """Xabarni tez-tez tahrirlab, Telegram cheklovlariga tushib qolmaslik uchun."""

    def __init__(self, msg, every: float = 4.0):
        self.msg, self.every = msg, every
        self.text, self.last = "", 0.0

    async def set(self, text: str, force: bool = False):
        now = time.monotonic()
        if text == self.text or (not force and now - self.last < self.every):
            return
        self.text, self.last = text, now
        try:
            await self.msg.edit(text)
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
        HELP.format(count=CLIP_COUNT, length=user_len.get(uid, DEFAULT_LEN), uid=uid, daily=DAILY_LIMIT),
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
    if allowed(event.sender_id):
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

    title = None  # reelsda matn yo'q
    status = await event.reply("⏳ Qabul qilindi…")
    await queue.put(Job(event.chat_id, uid, msg, status, user_len.get(uid, DEFAULT_LEN), title))
    add_usage(uid, 1)
    pos = queue.qsize() + (1 if busy else 0)
    if pos > 1:
        await Status(status).set(f"⏳ Navbatdasiz: {pos}-o'rin. Oldingi kino tugashi bilan boshlayman.")
    elif uid not in ADMINS:
        await Status(status).set(f"⏳ Qabul qilindi. Bugun yana {left_today(uid)} ta kino yuborishingiz mumkin.", force=True)


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
    st = Status(job.status)
    jobdir = WORK_DIR / f"{job.chat_id}_{job.msg.id}"
    jobdir.mkdir(parents=True, exist_ok=True)
    try:
        src = jobdir / f"kino{job.msg.file.ext or '.mp4'}"

        async def dl_progress(cur, total):
            pct = cur * 100 // total if total else 0
            await st.set(f"📥 Kino yuklab olinmoqda: {pct}%  ({mb(cur)} / {mb(total)} MB)")

        await st.set("📥 Kino yuklab olinmoqda…", force=True)
        await client.download_media(job.msg, file=str(src), progress_callback=dl_progress)

        loop = asyncio.get_running_loop()

        def an_progress(text: str):  # boshqa oqimdan chaqiriladi
            asyncio.run_coroutine_threadsafe(st.set(f"🔍 {text}…", force=True), loop)

        t0 = time.monotonic()
        res = await asyncio.to_thread(
            analyze.find_highlights, str(src), CLIP_COUNT, job.length, an_progress,
        )
        log.info("Tahlil %.0f s, %d ta joy topildi", time.monotonic() - t0, len(res.clips))
        if not res.clips:
            raise RuntimeError("kinodan mos joy topilmadi")

        total = len(res.clips)
        for i, clip in enumerate(res.clips, 1):
            await st.set(f"✂️ Reels tayyorlanmoqda: {i} / {total}", force=True)
            out = jobdir / f"reel_{i:02d}.mp4"
            await asyncio.to_thread(
                render.render_clip, res.info, clip, out, job.title, WATERMARK, FONT_PATH, ZOOM,
            )
            thumb = await asyncio.to_thread(
                render.make_thumb, out, clip.duration * 0.3, jobdir / f"thumb_{i:02d}.jpg",
            )
            caption = (
                f"🎬 {i}-variant · kinoning {analyze.fmt_time(clip.start)}–"
                f"{analyze.fmt_time(clip.end)} qismi · {round(clip.duration)} s"
            )
            await send_reel(job, out, thumb, caption, clip.duration)
            out.unlink(missing_ok=True)

        await st.set(
            f"✅ Tayyor! {total} ta reels yuborildi.\n"
            "1-variant — bot eng qiziq deb topgan joy, keyingilari kamayib boradi.",
            force=True,
        )
    finally:
        shutil.rmtree(jobdir, ignore_errors=True)


async def worker():
    global busy
    while True:
        job: Job = await queue.get()
        busy = True
        try:
            await process(job)
        except Exception as e:
            log.exception("Ish bajarilmadi")
            add_usage(job.user_id, -1)  # muvaffaqiyatsiz bo'lsa, limit qaytariladi
            await Status(job.status).set(f"❌ Xatolik yuz berdi: {html.escape(str(e))[:300]}", force=True)
        finally:
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

    await client.start(bot_token=os.environ["BOT_TOKEN"])
    me = await client.get_me()
    log.info("Bot ishga tushdi: @%s  (reels: %d ta × %d s)", me.username, CLIP_COUNT, DEFAULT_LEN)
    if not ALLOWED:
        log.warning("ALLOWED_USERS bo'sh — botdan hamma foydalana oladi")

    asyncio.create_task(worker())
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
