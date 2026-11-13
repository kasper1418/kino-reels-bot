"""Admin panel (/panel): statistika, do'kon (karta va tariflar), cheklar, foydalanuvchilar,
tarqatish va adminlar. Bitta xabar tahrirlanib turadi, har bosqichda «Orqaga» bor.
Huquq har bosqichda serverda tekshiriladi."""
from __future__ import annotations

import asyncio
import html
import logging
import re

from telethon import Button, events
from telethon.errors import FloodWaitError, MessageNotModifiedError

import plans

log = logging.getLogger("kino-reels.admin")

client = None
ADMINS: set[int] = set()
OWNERS: set[int] = set()
NO_BUTTONS = None
state: dict[int, dict] = {}      # admin -> kutilayotgan kiritish
drafts: dict[int, str] = {}      # admin -> tarqatish matni
bcast_task: asyncio.Task | None = None


def som(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def init(tg_client, admins: set, owners: set, no_buttons) -> None:
    global client, ADMINS, OWNERS, NO_BUTTONS
    client, ADMINS, OWNERS, NO_BUTTONS = tg_client, admins, owners, no_buttons
    for a in plans.db_admins():
        ADMINS.add(a)


def is_admin(uid: int) -> bool:
    return uid in ADMINS


def back(to: str = "home"):
    return [Button.inline("⬅️ Orqaga", f"ap:{to}".encode())]


async def render(event, text: str, rows):
    """Mavjud xabarni yangilaydi (callback), buyruq bo'lsa yangi xabar yuboradi."""
    try:
        if isinstance(event, events.CallbackQuery.Event):
            await event.edit(text, buttons=rows, parse_mode="html")
        else:
            await event.reply(text, buttons=rows, parse_mode="html")
    except MessageNotModifiedError:
        pass


# ------------------------------------------------------------------- sahifalar

async def page_home(event):
    rows = [
        [Button.inline("📊 Statistika", b"ap:stats"), Button.inline("💳 Do'kon", b"ap:shop")],
        [Button.inline("🧾 Cheklar", b"ap:rcpts"), Button.inline("👥 Foydalanuvchilar", b"ap:users")],
        [Button.inline("📣 Tarqatish", b"ap:bc"), Button.inline("🛡 Adminlar", b"ap:admins")],
    ]
    pend = plans.full_stats()["pending"]
    note = f"\n\n🧾 Tekshirilmagan cheklar: <b>{pend}</b>" if pend else ""
    await render(event, "⚙️ <b>Admin panel</b>\nBo'limni tanlang." + note, rows)


async def page_stats(event):
    s = plans.full_stats()
    text = (
        "📊 <b>Statistika</b>\n\n"
        f"👥 Jami foydalanuvchi: <b>{s['total_users']}</b>\n"
        f"🆕 Oxirgi 24 soatda yangi: {s['new_today']}\n"
        f"🟢 Oxirgi 24 soatda faol: {s['active_today']}\n"
        f"🚫 Bloklangan: {s['blocked']}\n\n"
        f"🎞 Bugungi kinolar: {s['films_today']}\n"
        f"🎞 Jami kinolar: {s['films_total']}\n\n"
        f"💎 Faol pullik tarif: {s['paid']}\n"
        f"🧾 Kutilayotgan cheklar: {s['pending']}\n"
        f"💰 Oxirgi 30 kun tushum: {som(s['income_30'])} so'm\n"
        f"💰 Jami tushum: {som(s['income'])} so'm"
    )
    await render(event, text, [back()])


def shop_text() -> str:
    num, holder = plans.card()
    P = plans.PLANS
    card_line = f"<code>{html.escape(num)}</code>" if num else "<i>kiritilmagan</i>"
    return (
        "💳 <b>Do'kon</b>\n\n"
        f"Karta: {card_line}\n"
        f"Karta egasi: {html.escape(holder) if holder else '<i>kiritilmagan</i>'}\n\n"
        f"🚀 Start: <b>{som(P['start']['price'])} so'm</b> · kuniga {P['start']['daily']} kino\n"
        f"👑 Pro: <b>{som(P['pro']['price'])} so'm</b> · kuniga {P['pro']['daily']} kino\n"
        f"🆓 Bepul: kuniga {P['free']['daily']} kino\n"
        f"📅 Muddat: {plans.PLAN_DAYS} kun"
    )


async def page_shop(event):
    rows = [
        [Button.inline("💳 Karta raqami", b"ap:set:card"), Button.inline("👤 Karta egasi", b"ap:set:holder")],
        [Button.inline("💰 Start narxi", b"ap:set:price_start"), Button.inline("💰 Pro narxi", b"ap:set:price_pro")],
        [Button.inline("🎞 Bepul", b"ap:set:daily_free"), Button.inline("🎞 Start", b"ap:set:daily_start"),
         Button.inline("🎞 Pro", b"ap:set:daily_pro")],
        back(),
    ]
    await render(event, shop_text(), rows)


PROMPTS = {
    "card": "💳 Yangi <b>karta raqamini</b> yuboring (16 raqam, Uzcard yoki Humo).",
    "holder": "👤 Karta <b>egasining ism-familiyasini</b> yuboring (o'chirish uchun «-» yozing).",
    "price_start": "💰 <b>Start</b> tarifi narxini so'mda yuboring (masalan: 29000).",
    "price_pro": "💰 <b>Pro</b> tarifi narxini so'mda yuboring (masalan: 59000).",
    "daily_free": "🎞 <b>Bepul</b> tarifda kuniga nechta kino bo'lsin? (raqam)",
    "daily_start": "🎞 <b>Start</b> tarifida kuniga nechta kino bo'lsin? (raqam)",
    "daily_pro": "🎞 <b>Pro</b> tarifida kuniga nechta kino bo'lsin? (raqam)",
}


async def page_set(event, key: str):
    if key not in PROMPTS:
        return
    state[event.sender_id] = {"act": "set", "key": key}
    await render(event, PROMPTS[key], [[Button.inline("✖️ Bekor qilish", b"ap:shop")]])


async def page_receipts(event):
    rows_db = plans.waiting_payments()
    if not rows_db:
        await render(event, "🧾 <b>Cheklar</b>\n\nTekshirilmagan chek yo'q.", [back()])
        return
    rows = []
    for pid, uid, plan, amount, name, uname in rows_db:
        who = (name or (f"@{uname}" if uname else str(uid)))[:18]
        rows.append([Button.inline(f"#{pid} · {who} · {plans.PLANS[plan]['name']} {som(amount)}",
                                   f"ap:rc:{pid}".encode())])
    rows.append(back())
    await render(event, "🧾 <b>Cheklar</b>\nTekshirilmagan to'lovlar:", rows)


async def page_receipt(event, pid: int):
    pay = plans.get_payment(pid)
    if not pay or pay[4] != "wait":
        await event.answer("Bu chek allaqachon hal qilingan.", alert=True)
        await page_receipts(event)
        return
    rec = plans.get_receipt(pid)
    kb = [[Button.inline("✅ Tasdiqlash", f"pok:{pid}".encode()), Button.inline("❌ Rad etish", f"pno:{pid}".encode())]]
    caption = (f"💳 To'lov #{pid}\nID: <code>{pay[1]}</code>\n"
               f"Tarif: <b>{plans.PLANS[pay[2]]['name']}</b>\nSumma: <b>{som(pay[3])} so'm</b>")
    sent = False
    if rec and rec[0] and rec[1]:
        try:
            msg = await client.get_messages(rec[0], ids=rec[1])
            if msg and msg.media:
                await client.send_file(event.sender_id, msg.media, caption=caption, buttons=kb, parse_mode="html")
                sent = True
        except Exception as e:
            log.warning("Chekni ochib bo'lmadi: %s", e)
    if not sent:
        await client.send_message(event.sender_id, caption + "\n\n⚠️ Chek rasmi topilmadi.", buttons=kb, parse_mode="html")
    await event.answer("Chek yuborildi")


def user_text(uid: int) -> str:
    row = plans.user_row(uid)
    p = plans.info(uid)
    uname = f"@{row[1]}" if row and row[1] else "—"
    name = html.escape(row[2]) if row and row[2] else "—"
    text = (
        f"👤 <b>Foydalanuvchi</b>\nID: <code>{uid}</code>\nIsm: {name}\nUsername: {uname}\n"
        f"Tarif: <b>{p['name']}</b>"
    )
    if plans.plan_of(uid) != "free":
        text += f" ({plans.fmt_until(plans.until_of(uid))} gacha)"
    text += f"\nBugun ishlatgan: {plans.used_today(uid)} / {p['daily']}"
    if row and row[3]:
        text += "\n🚫 <b>Bloklangan</b>"
    return text


async def page_user(event, uid: int):
    blocked = plans.is_blocked(uid)
    rows = [
        [Button.inline("🚀 Start berish", f"ap:g:{uid}:start".encode()),
         Button.inline("👑 Pro berish", f"ap:g:{uid}:pro".encode())],
        [Button.inline("🆓 Bepulga o'tkazish", f"ap:g:{uid}:free".encode())],
        [Button.inline("✅ Blokdan chiqarish" if blocked else "🚫 Bloklash", f"ap:b:{uid}".encode())],
        back("users"),
    ]
    await render(event, user_text(uid), rows)


async def page_users(event):
    state[event.sender_id] = {"act": "find"}
    rows = [[Button.inline("💎 Pullik foydalanuvchilar", b"ap:paid")], back()]
    await render(event, "👥 <b>Foydalanuvchilar</b>\n\nQidirish uchun <b>ID</b> yoki <b>@username</b> yuboring.", rows)


async def page_paid(event):
    rows_db = plans.paid_users()
    if not rows_db:
        await render(event, "💎 Faol pullik foydalanuvchi yo'q.", [back("users")])
        return
    rows = []
    for uid, uname, name, plan, until in rows_db:
        who = (name or (f"@{uname}" if uname else str(uid)))[:16]
        rows.append([Button.inline(f"{who} · {plans.PLANS[plan]['name']} · {plans.fmt_until(until)}",
                                   f"ap:u:{uid}".encode())])
    rows.append(back("users"))
    await render(event, "💎 <b>Pullik foydalanuvchilar</b>", rows)


async def page_broadcast(event):
    state[event.sender_id] = {"act": "bc"}
    n = len(plans.all_user_ids())
    await render(
        event,
        f"📣 <b>Tarqatish</b>\n\nXabar <b>{n}</b> ta foydalanuvchiga yuboriladi.\n"
        "Xabar matnini yuboring (HTML belgilar: <code>&lt;b&gt;qalin&lt;/b&gt;</code>). "
        "Yuborishdan oldin ko'rinishini ko'rasiz.",
        [[Button.inline("✖️ Bekor qilish", b"ap:home")]],
    )


async def page_admins(event):
    uid = event.sender_id
    lines = ["🛡 <b>Adminlar</b>\n"]
    for a in sorted(ADMINS):
        lines.append(f"• <code>{a}</code>" + (" — egasi" if a in OWNERS else ""))
    rows = []
    if uid in OWNERS:
        rows.append([Button.inline("➕ Admin qo'shish", b"ap:addadm")])
        for a in sorted(ADMINS - OWNERS):
            rows.append([Button.inline(f"➖ {a}", f"ap:rm:{a}".encode())])
    else:
        lines.append("\nAdminlarni faqat bot egasi boshqaradi.")
    rows.append(back())
    await render(event, "\n".join(lines), rows)


# --------------------------------------------------------------- tarqatish

async def _broadcast(admin_id: int, text: str, status_msg):
    ids = plans.all_user_ids()
    ok = fail = 0
    for i, uid in enumerate(ids, 1):
        try:
            await client.send_message(uid, text, parse_mode="html")
            ok += 1
        except FloodWaitError as e:
            await asyncio.sleep(e.seconds + 1)
            try:
                await client.send_message(uid, text, parse_mode="html")
                ok += 1
            except Exception:
                fail += 1
        except Exception:
            fail += 1
        if i % 25 == 0:
            try:
                await status_msg.edit(f"📣 Yuborilmoqda… {i} / {len(ids)}")
            except Exception:
                pass
        await asyncio.sleep(0.06)
    try:
        await status_msg.edit(f"✅ Tarqatish tugadi.\nYetkazildi: {ok}\nYetkazilmadi: {fail}",
                              buttons=[back()])
    except Exception:
        pass


# -------------------------------------------------------- kiritilgan matnlar

async def handle_text(event) -> bool:
    """Admin matn kiritayotgan bo'lsa, shuni qayta ishlaydi. True — xabar ishlatildi."""
    uid = event.sender_id
    st = state.get(uid)
    if not st or not is_admin(uid):
        return False
    text = (event.raw_text or "").strip()
    act = st["act"]

    if act == "set":
        key = st["key"]
        if key == "card":
            digits = re.sub(r"\D", "", text)
            if len(digits) != 16:
                await event.reply("Karta raqami 16 ta raqamdan iborat bo'lishi kerak. Qayta yuboring.")
                return True
            plans.set_setting("card_number", " ".join(digits[i:i + 4] for i in range(0, 16, 4)))
        elif key == "holder":
            plans.set_setting("card_holder", "" if text == "-" else text[:60])
        else:
            if not text.isdigit() or int(text) <= 0 or (key.startswith("daily") and int(text) > 1000):
                await event.reply("Musbat butun raqam yuboring.")
                return True
            kind, plan = key.split("_", 1)
            plans.set_setting(f"{kind}_{plan}", text)
        state.pop(uid, None)
        await event.reply("✅ Saqlandi.\n\n" + shop_text(), buttons=[[Button.inline("💳 Do'kon", b"ap:shop")], back()],
                          parse_mode="html")
        return True

    if act == "find":
        row = plans.find_user(text)
        if not row:
            await event.reply("Foydalanuvchi topilmadi. ID yoki @username yuboring (u botga kamida bir marta yozgan bo'lishi kerak).")
            return True
        state.pop(uid, None)
        await page_user(event, row[0])
        return True

    if act == "bc":
        if not text:
            return True
        drafts[uid] = text
        state.pop(uid, None)
        try:
            await event.reply(text, parse_mode="html")
        except Exception:
            await event.reply("⚠️ Matndagi HTML belgilar noto'g'ri. Qayta yuboring.")
            state[uid] = {"act": "bc"}
            return True
        await event.reply(
            f"👆 Xabar ko'rinishi. <b>{len(plans.all_user_ids())}</b> ta foydalanuvchiga yuborilsinmi?",
            buttons=[[Button.inline("✅ Yuborish", b"ap:bcgo"), Button.inline("✖️ Bekor", b"ap:home")]],
            parse_mode="html",
        )
        return True

    if act == "addadm":
        if uid not in OWNERS:
            state.pop(uid, None)
            return True
        if not text.isdigit():
            await event.reply("Admin ID raqamini yuboring.")
            return True
        plans.add_admin(int(text))
        ADMINS.add(int(text))
        state.pop(uid, None)
        await event.reply(f"✅ {text} admin qilindi.", buttons=[back("admins")])
        return True
    return False


# ------------------------------------------------------------------ handlers

async def on_panel(event):
    if not is_admin(event.sender_id):
        return
    state.pop(event.sender_id, None)
    await page_home(event)


async def on_cb(event):
    uid = event.sender_id
    if not is_admin(uid):
        await event.answer("Faqat admin uchun.", alert=True)
        return
    parts = event.data.decode().split(":")
    page = parts[1]
    if page not in ("set", "users", "bc", "addadm"):
        state.pop(uid, None)
    await event.answer()

    if page == "home":
        await page_home(event)
    elif page == "stats":
        await page_stats(event)
    elif page == "shop":
        await page_shop(event)
    elif page == "set":
        await page_set(event, parts[2])
    elif page == "rcpts":
        await page_receipts(event)
    elif page == "rc":
        await page_receipt(event, int(parts[2]))
    elif page == "users":
        await page_users(event)
    elif page == "paid":
        await page_paid(event)
    elif page == "u":
        await page_user(event, int(parts[2]))
    elif page == "g":
        target, plan = int(parts[2]), parts[3]
        if plan == "free":
            plans.grant(target, "free", 0)
        elif plan in ("start", "pro"):
            until = plans.grant(target, plan)
            try:
                await client.send_message(target, f"🎁 Sizga <b>{plans.PLANS[plan]['name']}</b> tarifi berildi "
                                                  f"({plans.fmt_until(until)} gacha).", parse_mode="html")
            except Exception:
                pass
        await page_user(event, target)
    elif page == "b":
        target = int(parts[2])
        plans.set_blocked(target, not plans.is_blocked(target))
        await page_user(event, target)
    elif page == "bc":
        await page_broadcast(event)
    elif page == "bcgo":
        global bcast_task
        text = drafts.pop(uid, None)
        if not text:
            await event.answer("Xabar topilmadi.", alert=True)
            return
        if bcast_task and not bcast_task.done():
            await event.answer("Boshqa tarqatish hali tugamadi.", alert=True)
            drafts[uid] = text
            return
        status = await client.send_message(uid, "📣 Yuborilmoqda…")
        bcast_task = asyncio.create_task(_broadcast(uid, text, status))
        await event.edit("📣 Tarqatish boshlandi.", buttons=[back()])
    elif page == "admins":
        await page_admins(event)
    elif page == "addadm":
        if uid not in OWNERS:
            await event.answer("Faqat bot egasi.", alert=True)
            return
        state[uid] = {"act": "addadm"}
        await render(event, "🛡 Yangi adminning Telegram <b>ID</b> raqamini yuboring.",
                     [[Button.inline("✖️ Bekor qilish", b"ap:admins")]])
    elif page == "rm":
        if uid not in OWNERS:
            await event.answer("Faqat bot egasi.", alert=True)
            return
        target = int(parts[2])
        if target not in OWNERS:
            plans.remove_admin(target)
            ADMINS.discard(target)
        await page_admins(event)


def register(tg_client):
    private = lambda e: e.is_private  # noqa: E731
    tg_client.add_event_handler(on_panel, events.NewMessage(incoming=True, pattern=r"^/(panel|admin)\b", func=private))
    tg_client.add_event_handler(on_cb, events.CallbackQuery(pattern=rb"^ap:"))
