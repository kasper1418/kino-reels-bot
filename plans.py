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
            until INTEGER NOT NULL DEFAULT 0, created INTEGER NOT NULL);
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
