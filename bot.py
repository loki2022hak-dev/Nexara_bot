from osint_username import run_maigret
import os
import json
import html
import asyncio
import sqlite3
import logging
from datetime import datetime

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN not set")

DB_PATH = "nexara_bot.db"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
FREE_DAILY_LIMIT = int(os.getenv("FREE_DAILY_LIMIT", "10"))
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(message)s",
)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()


def now_utc() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")


def esc(value) -> str:
    return html.escape(str(value or ""))


def conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_db() -> None:
    c = conn()
    cur = c.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            tariff TEXT NOT NULL DEFAULT 'FREE',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS searches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            query TEXT NOT NULL,
            query_type TEXT NOT NULL,
            result_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS daily_usage (
            user_id INTEGER NOT NULL,
            day_key TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, day_key)
        )
    """)

    c.commit()
    c.close()


def upsert_user(user_id: int, username: str | None, first_name: str | None) -> None:
    c = conn()
    cur = c.cursor()
    ts = now_utc()

    cur.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()

    if row:
        cur.execute(
            "UPDATE users SET username = ?, first_name = ?, updated_at = ? WHERE user_id = ?",
            (username, first_name, ts, user_id),
        )
    else:
        cur.execute(
            "INSERT INTO users (user_id, username, first_name, tariff, created_at, updated_at) VALUES (?, ?, ?, 'FREE', ?, ?)",
            (user_id, username, first_name, ts, ts),
        )

    c.commit()
    c.close()


def total_searches(user_id: int) -> int:
    c = conn()
    cur = c.cursor()
    cur.execute("SELECT COUNT(*) AS cnt FROM searches WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    c.close()
    return int(row["cnt"]) if row else 0


def get_history(user_id: int, limit: int = 10):
    c = conn()
    cur = c.cursor()
    cur.execute("""
        SELECT query, query_type, created_at
        FROM searches
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT ?
    """, (user_id, limit))
    rows = cur.fetchall()
    c.close()
    return rows


def save_search(user_id: int, query: str, query_type: str, result: dict) -> None:
    c = conn()
    cur = c.cursor()
    cur.execute("""
        INSERT INTO searches (user_id, query, query_type, result_json, created_at)
        VALUES (?, ?, ?, ?, ?)
    """, (
        user_id,
        query,
        query_type,
        json.dumps(result, ensure_ascii=False),
        now_utc(),
    ))
    c.commit()
    c.close()


def day_key() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d")


def daily_used(user_id: int) -> int:
    c = conn()
    cur = c.cursor()
    cur.execute("SELECT count FROM daily_usage WHERE user_id = ? AND day_key = ?", (user_id, day_key()))
    row = cur.fetchone()
    c.close()
    return int(row["count"]) if row else 0


def bump_daily(user_id: int) -> None:
    c = conn()
    cur = c.cursor()
    cur.execute("SELECT count FROM daily_usage WHERE user_id = ? AND day_key = ?", (user_id, day_key()))
    row = cur.fetchone()

    if row:
        cur.execute(
            "UPDATE daily_usage SET count = count + 1 WHERE user_id = ? AND day_key = ?",
            (user_id, day_key()),
        )
    else:
        cur.execute(
            "INSERT INTO daily_usage (user_id, day_key, count) VALUES (?, ?, 1)",
            (user_id, day_key()),
        )

    c.commit()
    c.close()


def menu(user_id: int | None = None) -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton(text="🔎 Новий пошук"), KeyboardButton(text="📁 Мої результати")],
        [KeyboardButton(text="💎 VIP / Тарифи"), KeyboardButton(text="👤 Профіль")],
        [KeyboardButton(text="🆘 Підтримка")],
    ]

    if user_id == OWNER_ID and OWNER_ID:
        keyboard.append([KeyboardButton(text="🛡 Адмін")])

    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
    )


def detect_type(text: str) -> str:
    q = text.strip()
    ql = q.lower()

    if ql.startswith("http://") or ql.startswith("https://"):
        return "url"

    if "@" in q and "." in q and " " not in q:
        return "email"

    parts = q.split(".")
    if len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
        return "ip"

    if "." in q and " " not in q and "/" not in q:
        return "domain"

    if q.startswith("@") or (" " not in q and 3 <= len(q) <= 32):
        return "username"

    return "text"


async def run_analysis(query: str) -> dict:
    qtype = detect_type(query)
    result = {
        "query": query,
        "type": qtype,
        "generated_at": now_utc(),
        "summary": [],
    }

    if qtype == "username":
        result["summary"] = [
            "Виявлено username / nickname",
            "Базова класифікація виконана",
            "Під цей тип можна далі підключати зовнішні OSINT-модулі",
        ]
    elif qtype == "domain":
        result["summary"] = [
            "Виявлено domain",
            "Базова класифікація виконана",
            "Можна підключати DNS / RDAP / reputation сервіси",
        ]
    elif qtype == "ip":
        result["summary"] = [
            "Виявлено IP",
            "Базова класифікація виконана",
            "Можна підключати RDAP / host intelligence",
        ]
    elif qtype == "email":
        local, _, domain = query.partition("@")
        result["summary"] = [
            "Виявлено email",
            f"local-part: {local}",
            f"domain-part: {domain}",
        ]
    elif qtype == "url":
        result["summary"] = [
            "Виявлено URL",
            "Базова класифікація виконана",
            "Можна підключати URL / domain analysis",
        ]
    else:
        result["summary"] = [
            "Отримано text query",
            "Базова класифікація виконана",
        ]

    await asyncio.sleep(0.2)
    return result


def render_result(result: dict) -> str:
    summary = "\n".join(f"• {esc(item)}" for item in result["summary"])
    return (
        "<b>NEXARA RESULT</b>\n\n"
        f"<b>Запит:</b> <code>{esc(result['query'])}</code>\n"
        f"<b>Тип:</b> {esc(result['type'])}\n"
        f"<b>Час:</b> {esc(result['generated_at'])}\n\n"
        f"<b>Summary:</b>\n{summary}"
    )


@dp.message(CommandStart())
async def start_cmd(message: Message):
    upsert_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    used = "∞" if message.from_user.id == OWNER_ID and OWNER_ID else str(daily_used(message.from_user.id))
    await message.answer(
        "<b>NEXARA</b>\n"
        f"Використано: {used}\n\n"
        "Введіть ПІБ, Нікнейм, Email, Телефон, Domain, URL або IP:",
        reply_markup=menu(message.from_user.id),
    )


@dp.message(Command("profile"))
async def profile_cmd(message: Message):
    used = daily_used(message.from_user.id)
    total = total_searches(message.from_user.id)
    used_str = "∞" if message.from_user.id == OWNER_ID and OWNER_ID else f"{used}/{FREE_DAILY_LIMIT}"
    await message.answer(
        "<b>Профіль</b>\n\n"
        f"<b>ID:</b> <code>{message.from_user.id}</code>\n"
        f"<b>Username:</b> @{esc(message.from_user.username or 'none')}\n"
        f"<b>Використано сьогодні:</b> {used_str}\n"
        f"<b>Всього пошуків:</b> {total}",
        reply_markup=menu(message.from_user.id),
    )


@dp.message(F.text == "👤 Профіль")
async def profile_btn(message: Message):
    await profile_cmd(message)


@dp.message(F.text == "🔎 Новий пошук")
async def new_search_btn(message: Message):
    await message.answer(
        "Введіть ПІБ, Нікнейм, Email, Телефон, Domain, URL або IP:",
        reply_markup=menu(message.from_user.id),
    )


@dp.message(F.text == "📁 Мої результати")
async def results_btn(message: Message):
    rows = get_history(message.from_user.id, 10)
    if not rows:
        await message.answer("Немає даних.", reply_markup=menu(message.from_user.id))
        return

    parts = ["<b>Мої результати</b>", ""]
    for row in rows:
        parts.append(f"• <code>{esc(row['query'])}</code> [{esc(row['query_type'])}] — {esc(row['created_at'])}")

    await message.answer("\n".join(parts), reply_markup=menu(message.from_user.id))


@dp.message(F.text == "💎 VIP / Тарифи")
async def vip_btn(message: Message):
    await message.answer(
        "<b>VIP / Тарифи</b>\n\nFREE\nINTEL\nAGENCY\nWARROOM",
        reply_markup=menu(message.from_user.id),
    )


@dp.message(F.text == "🆘 Підтримка")
async def support_btn(message: Message):
    await message.answer(
        "Підтримка активна. Надішли проблему одним повідомленням.",
        reply_markup=menu(message.from_user.id),
    )


@dp.message(F.text == "🛡 Адмін")
async def admin_btn(message: Message):
    if message.from_user.id != OWNER_ID or not OWNER_ID:
        return

    await message.answer(
        "<b>Адмін-панель</b>\n\n"
        f"Users: {count_users()}\n"
        f"Searches: {count_searches()}",
        reply_markup=menu(message.from_user.id),
    )


def count_users() -> int:
    c = conn()
    cur = c.cursor()
    cur.execute("SELECT COUNT(*) AS cnt FROM users")
    row = cur.fetchone()
    c.close()
    return int(row["cnt"]) if row else 0


def count_searches() -> int:
    c = conn()
    cur = c.cursor()
    cur.execute("SELECT COUNT(*) AS cnt FROM searches")
    row = cur.fetchone()
    c.close()
    return int(row["cnt"]) if row else 0


@dp.message(F.text)
async def universal_handler(message: Message):
    text = (message.text or "").strip()
    if not text:
        return

    if text in {"🔎 Новий пошук", "📁 Мої результати", "💎 VIP / Тарифи", "👤 Профіль", "🆘 Підтримка", "🛡 Адмін"}:
        return

    if text.startswith("/") and text not in {"/start", "/profile"}:
        await message.answer("Невідома команда.", reply_markup=menu(message.from_user.id))
        return

    upsert_user(message.from_user.id, message.from_user.username, message.from_user.first_name)

    used = daily_used(message.from_user.id)
    if not (message.from_user.id == OWNER_ID and OWNER_ID) and used >= FREE_DAILY_LIMIT:
        await message.answer(
            f"Ліміт вичерпано: {used}/{FREE_DAILY_LIMIT}",
            reply_markup=menu(message.from_user.id),
        )
        return

    wait = await message.answer("NEXARA: Глибокий пошук...", reply_markup=menu(message.from_user.id))
    result = await run_maigret(text)

    save_search(message.from_user.id, text, result["type"], result)

    if not (message.from_user.id == OWNER_ID and OWNER_ID):
        bump_daily(message.from_user.id)

    await wait.edit_text(render_result(result), reply_markup=menu(message.from_user.id))


async def main():
    init_db()
    me = await bot.get_me()
    logging.info("Start polling")
    logging.info("Authorized as @%s", me.username)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
