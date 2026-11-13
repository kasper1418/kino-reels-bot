"""Tariflar, kunlik limit va to'lovlar (SQLite)."""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

PLAN_DAYS = int(os.getenv("PLAN_DAYS", "30") or 30)


def _price(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


# key: (nomi, kuniga kino, eng ko'p variant, navbat ustuvorligi (kichik = oldin), narx so'm)
PLANS = {
    "free": {"name": "Bepul", "daily": 1, "clips": 3, "prio": 2, "price": 0},
    "start": {"name": "Start", "daily": 3, "clips": 5, "prio": 1, "price": _price("PRICE_START", 29000)},
    "pro": {"name": "Pro", "daily": 10, "clips": 5, "prio": 0, "price": _price("PRICE_PRO", 59000)},
}
FREE_DAILY = int(os.getenv("DAILY_LIMIT", "1") or 1)
PLANS["free"]["daily"] = max(1, FREE_DAILY)

_lock = threading.Lock()
_db: sqlite3.Connection | None = None


def init(path: Path) -> None:
    global _db
    path.parent.mkdir(parents=True, exist_ok=True)
    _db = sqlite3.connect(str(path), check_same_thread=False)
    _db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users(
            uid INTEGER PRIMARY KEY, plan TEXT NOT NULL DEFAULT 'free',
            until INTEGER NOT NULL DEFAULT 0, created INTEGER NOT NULL,
            username TEXT, name TEXT, blocked INTEGER NOT NULL DEFAULT 0, last_seen INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS admins(uid INTEGER PRIMARY KEY, added INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS usage(
            uid INTEGER NOT NULL, day TEXT NOT NULL, used INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(uid, day));
        CREATE TABLE IF NOT EXISTS payments(
            id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER NOT NULL, plan TEXT NOT NULL,
            amount INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'new',
            created INTEGER NOT NULL, decided INTEGER, admin INTEGER);
        """
    )
    _db.commit()
    _migrate("users", {"username": "TEXT", "name": "TEXT", "blocked": "INTEGER NOT NULL DEFAULT 0",
                       "last_seen": "INTEGER NOT NULL DEFAULT 0"})
    _migrate("payments", {"receipt_chat": "INTEGER", "receipt_msg": "INTEGER"})
    apply_settings()


def _migrate(table: str, cols: dict) -> None:
    have = {r[1] for r in _db.execute(f"PRAGMA table_info({table})")}
    for c, typ in cols.items():
        if c not in have:
            _db.execute(f"ALTER TABLE {table} ADD COLUMN {c} {typ}")
    _db.commit()


def _q(sql: str, args: tuple = (), one: bool = False):
    with _lock:
        cur = _db.execute(sql, args)
        rows = cur.fetchone() if one else cur.fetchall()
        _db.commit()
        return rows


def today() -> str:
    return datetime.now(timezone(timedelta(hours=5))).strftime("%Y-%m-%d")  # Toshkent vaqti


def plan_of(uid: int) -> str:
    """Faol tarif kaliti; muddati tugagan bo'lsa 'free'."""
    row = _q("SELECT plan, until FROM users WHERE uid=?", (uid,), one=True)
    if row and row[0] in PLANS and row[0] != "free" and row[1] > time.time():
        return row[0]
    return "free"


def until_of(uid: int) -> int:
    row = _q("SELECT until FROM users WHERE uid=?", (uid,), one=True)
    return int(row[0]) if row else 0


def info(uid: int) -> dict:
    return PLANS[plan_of(uid)]


def used_today(uid: int) -> int:
    row = _q("SELECT used FROM usage WHERE uid=? AND day=?", (uid, today()), one=True)
    return int(row[0]) if row else 0


def left_today(uid: int) -> int:
    return max(0, info(uid)["daily"] - used_today(uid))


def add_usage(uid: int, delta: int) -> None:
    _q(
        "INSERT INTO usage(uid, day, used) VALUES(?,?,MAX(0,?)) "
        "ON CONFLICT(uid, day) DO UPDATE SET used=MAX(0, used+?)",
        (uid, today(), delta, delta),
    )


def grant(uid: int, plan: str, days: int = PLAN_DAYS) -> int:
    """Tarifni yoqadi (xuddi shu tarif faol bo'lsa, muddat uzayadi). Tugash vaqtini qaytaradi."""
    now = int(time.time())
    cur = _q("SELECT plan, until FROM users WHERE uid=?", (uid,), one=True)
    base = cur[1] if cur and cur[0] == plan and cur[1] > now else now
    until = int(base + days * 86400)
    _q(
        "INSERT INTO users(uid, plan, until, created) VALUES(?,?,?,?) "
        "ON CONFLICT(uid) DO UPDATE SET plan=?, until=?",
        (uid, plan, until, now, plan, until),
    )
    return until


def fmt_until(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone(timedelta(hours=5))).strftime("%d.%m.%Y")


# ----------------------------------------------------------------- to'lovlar

def new_payment(uid: int, plan: str) -> int:
    with _lock:
        cur = _db.execute(
            "INSERT INTO payments(uid, plan, amount, created) VALUES(?,?,?,?)",
            (uid, plan, PLANS[plan]["price"], int(time.time())),
        )
        _db.commit()
        return int(cur.lastrowid)


def get_payment(pid: int):
    return _q("SELECT id, uid, plan, amount, status FROM payments WHERE id=?", (pid,), one=True)


def set_payment(pid: int, status: str, admin: int | None = None) -> None:
    _q("UPDATE payments SET status=?, decided=?, admin=? WHERE id=?", (status, int(time.time()), admin, pid))


def stats() -> dict:
    now = int(time.time())
    users = _q("SELECT COUNT(*) FROM users", one=True)[0]
    paid = _q("SELECT COUNT(*) FROM users WHERE plan!='free' AND until>?", (now,), one=True)[0]
    films = _q("SELECT COALESCE(SUM(used),0) FROM usage WHERE day=?", (today(),), one=True)[0]
    income = _q("SELECT COALESCE(SUM(amount),0) FROM payments WHERE status='ok'", one=True)[0]
    return {"users": users, "paid": paid, "films_today": films, "income": income}


# ------------------------------------------------------------ sozlamalar (panel)

def get_setting(key: str, default: str = "") -> str:
    row = _q("SELECT v FROM settings WHERE k=?", (key,), one=True)
    return row[0] if row else default


def set_setting(key: str, value: str) -> None:
    _q("INSERT INTO settings(k, v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=?", (key, value, value))
    apply_settings()


def apply_settings() -> None:
    """Paneldan o'zgartirilgan narx va limitlarni tariflarga qo'llaydi."""
    for key in PLANS:
        d = get_setting(f"daily_{key}")
        if d.isdigit() and int(d) > 0:
            PLANS[key]["daily"] = int(d)
        if key != "free":
            pr = get_setting(f"price_{key}")
            if pr.isdigit() and int(pr) > 0:
                PLANS[key]["price"] = int(pr)


def card() -> tuple[str, str]:
    return (get_setting("card_number", os.getenv("CARD_NUMBER", "").strip()),
            get_setting("card_holder", os.getenv("CARD_HOLDER", "").strip()))


# ---------------------------------------------------------- foydalanuvchilar

def touch(uid: int, username: str | None = None, name: str | None = None) -> None:
    now = int(time.time())
    _q(
        "INSERT INTO users(uid, created, username, name, last_seen) VALUES(?,?,?,?,?) "
        "ON CONFLICT(uid) DO UPDATE SET username=COALESCE(?, username), name=COALESCE(?, name), last_seen=?",
        (uid, now, username, name, now, username, name, now),
    )


def is_blocked(uid: int) -> bool:
    row = _q("SELECT blocked FROM users WHERE uid=?", (uid,), one=True)
    return bool(row and row[0])


def set_blocked(uid: int, flag: bool) -> None:
    touch(uid)
    _q("UPDATE users SET blocked=? WHERE uid=?", (1 if flag else 0, uid))


def user_row(uid: int):
    return _q("SELECT uid, username, name, blocked, created, last_seen FROM users WHERE uid=?", (uid,), one=True)


def find_user(query: str):
    q = query.strip().lstrip("@")
    if q.isdigit():
        return user_row(int(q))
    row = _q("SELECT uid FROM users WHERE LOWER(username)=LOWER(?)", (q,), one=True)
    return user_row(row[0]) if row else None


def paid_users(limit: int = 15):
    return _q("SELECT uid, username, name, plan, until FROM users WHERE plan!='free' AND until>? "
              "ORDER BY until DESC LIMIT ?", (int(time.time()), limit))


def all_user_ids() -> list[int]:
    return [r[0] for r in _q("SELECT uid FROM users WHERE blocked=0")]


def db_admins() -> list[int]:
    return [r[0] for r in _q("SELECT uid FROM admins")]


def add_admin(uid: int) -> None:
    _q("INSERT OR IGNORE INTO admins(uid, added) VALUES(?,?)", (uid, int(time.time())))


def remove_admin(uid: int) -> None:
    _q("DELETE FROM admins WHERE uid=?", (uid,))


# ------------------------------------------------------------------- cheklar

def set_receipt(pid: int, chat_id: int, msg_id: int) -> None:
    _q("UPDATE payments SET receipt_chat=?, receipt_msg=? WHERE id=?", (chat_id, msg_id, pid))


def get_receipt(pid: int):
    return _q("SELECT receipt_chat, receipt_msg FROM payments WHERE id=?", (pid,), one=True)


def waiting_payments(limit: int = 10):
    return _q("SELECT p.id, p.uid, p.plan, p.amount, u.name, u.username FROM payments p "
              "LEFT JOIN users u ON u.uid=p.uid WHERE p.status='wait' ORDER BY p.id LIMIT ?", (limit,))


def full_stats() -> dict:
    now = int(time.time())
    day0 = now - 86400
    s = stats()
    s["total_users"] = _q("SELECT COUNT(*) FROM users", one=True)[0]
    s["new_today"] = _q("SELECT COUNT(*) FROM users WHERE created>?", (day0,), one=True)[0]
    s["active_today"] = _q("SELECT COUNT(*) FROM users WHERE last_seen>?", (day0,), one=True)[0]
    s["blocked"] = _q("SELECT COUNT(*) FROM users WHERE blocked=1", one=True)[0]
    s["films_total"] = _q("SELECT COALESCE(SUM(used),0) FROM usage", one=True)[0]
    s["pending"] = _q("SELECT COUNT(*) FROM payments WHERE status='wait'", one=True)[0]
    s["income_30"] = _q("SELECT COALESCE(SUM(amount),0) FROM payments WHERE status='ok' AND decided>?",
                        (now - 30 * 86400,), one=True)[0]
    return s
