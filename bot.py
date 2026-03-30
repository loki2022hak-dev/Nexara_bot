import os
import re
import html
import json
import asyncio
import logging
import sqlite3
import ipaddress
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton

APP_NAME = "NEXARA"
DB_PATH = Path("nexara_bot.db")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
VT_API_KEY = os.getenv("VT_API_KEY", "").strip()
SHODAN_API_KEY = os.getenv("SHODAN_API_KEY", "").strip()
CENSYS_BEARER_TOKEN = os.getenv("CENSYS_BEARER_TOKEN", "").strip()
VENICE_API_KEY = os.getenv("VENICE_API_KEY", "").strip()

FREE_DAILY_LIMIT = int(os.getenv("FREE_DAILY_LIMIT", "5"))
INTEL_DAILY_LIMIT = int(os.getenv("INTEL_DAILY_LIMIT", "25"))
AGENCY_DAILY_LIMIT = int(os.getenv("AGENCY_DAILY_LIMIT", "100"))
WARROOM_DAILY_LIMIT = int(os.getenv("WARROOM_DAILY_LIMIT", "500"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(APP_NAME)
dp = Dispatcher()

def now_ts(): return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
def escape(s): return html.escape(str(s or ""))
def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    c = conn(); cur = c.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, last_name TEXT, tariff TEXT DEFAULT 'FREE', created_at TEXT, updated_at TEXT)")
    cur.execute("CREATE TABLE IF NOT EXISTS searches (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, query_type TEXT, raw_query TEXT, normalized_query TEXT, result_json TEXT, created_at TEXT)")
    cur.execute("CREATE TABLE IF NOT EXISTS usage_daily (user_id INTEGER, day_key TEXT, count INTEGER DEFAULT 0, PRIMARY KEY (user_id, day_key))")
    c.commit(); c.close()

def upsert_user(user_id, username, first_name, last_name):
    c = conn(); cur = c.cursor(); ts = now_ts()
    cur.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    if cur.fetchone():
        cur.execute("UPDATE users SET username=?, first_name=?, last_name=?, updated_at=? WHERE user_id=?", (username, first_name, last_name, ts, user_id))
    else:
        cur.execute("INSERT INTO users (user_id, username, first_name, last_name, tariff, created_at, updated_at) VALUES (?, ?, ?, ?, 'FREE', ?, ?)", (user_id, username, first_name, last_name, ts, ts))
    c.commit(); c.close()

def get_user(user_id):
    c = conn(); cur = c.cursor(); cur.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)); row = cur.fetchone(); c.close(); return row

def increment_usage(user_id):
    c = conn(); cur = c.cursor(); dk = datetime.utcnow().strftime("%Y-%m-%d")
    cur.execute("INSERT INTO usage_daily (user_id, day_key, count) VALUES (?, ?, 1) ON CONFLICT(user_id, day_key) DO UPDATE SET count = count + 1", (user_id, dk))
    c.commit(); c.close()

def get_usage(user_id):
    c = conn(); cur = c.cursor(); dk = datetime.utcnow().strftime("%Y-%m-%d")
    cur.execute("SELECT count FROM usage_daily WHERE user_id = ? AND day_key = ?", (user_id, dk)); row = cur.fetchone(); c.close(); return row["count"] if row else 0

def get_limit(tariff):
    t = tariff.upper()
    if t == "INTEL": return INTEL_DAILY_LIMIT
    if t == "AGENCY": return AGENCY_DAILY_LIMIT
    if t == "WARROOM": return WARROOM_DAILY_LIMIT
    return FREE_DAILY_LIMIT

def keyboard():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🔍 Новий пошук"), KeyboardButton(text="📂 Історія")],
        [KeyboardButton(text="👤 Профіль"), KeyboardButton(text="💎 VIP / Тарифи")]
    ], resize_keyboard=True)

async def fetch(session, url, headers=None):
    try:
        async with session.get(url, headers=headers or {}, timeout=20) as r:
            content_type = r.headers.get("Content-Type", "").lower()
            return r.status < 400, await r.json() if "json" in content_type else await r.text()
    except: return False, None

async def venice_ai(data):
    if not VENICE_API_KEY: return None
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post("https://api.venice.ai/api/v1/chat/completions", headers={"Authorization": f"Bearer {VENICE_API_KEY}"}, 
                json={"model": "llama-3.1-70b", "messages": [{"role": "user", "content": f"Аналіз OSINT: {json.dumps(data)[:8000]}"}]}) as r:
                res = await r.json(); return res["choices"][0]["message"]["content"]
    except: return None

def get_vt_stats(vt_data):
    if not vt_data or not isinstance(vt_data, dict): return "Немає даних VT"
    try:
        attrs = vt_data.get("data", {}).get("attributes", {})
        stats = attrs.get("last_analysis_stats", {})
        if not stats: return "VT: Дані відсутні"
        return f"VT: Malicious({stats.get('malicious', 0)}) Suspicious({stats.get('suspicious', 0)})"
    except: return "VT error"

async def run_osint(query):
    q = query.strip()
    res = {"query": q, "ts": now_ts(), "results": {}}
    async with aiohttp.ClientSession() as s:
        if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", q):
            res["type"] = "ip"
            _, vt = await fetch(s, f"https://www.virustotal.com/api/v3/ip_addresses/{q}", {"x-apikey": VT_API_KEY})
            _, sh = await fetch(s, f"https://api.shodan.io/shodan/host/{q}?key={SHODAN_API_KEY}")
            res["results"] = {"vt": vt, "shodan": sh}
        else:
            res["type"] = "domain"
            _, vt = await fetch(s, f"https://www.virustotal.com/api/v3/domains/{q}", {"x-apikey": VT_API_KEY})
            res["results"] = {"vt": vt}
    
    res["vt_summary"] = get_vt_stats(res["results"].get("vt"))
    res["ai_summary"] = await venice_ai(res)
    return res

@dp.message(CommandStart())
async def start(m: Message):
    upsert_user(m.from_user.id, m.from_user.username, m.from_user.first_name, m.from_user.last_name)
    await m.answer(f"🛡️ {APP_NAME} активовано. Чекаю на ціль (IP/Domain).", reply_markup=keyboard())

@dp.message(F.text == "👤 Профіль")
async def profile(m: Message):
    u = get_user(m.from_user.id); used = get_usage(m.from_user.id); limit = get_limit(u["tariff"])
    await m.answer(f"👤 <b>{u['first_name']}</b>\nТариф: {u['tariff']}\nЛіміт: {used}/{limit}")

@dp.message(F.text)
async def handle(m: Message):
    if m.text.startswith("/"): return
    u = get_user(m.from_user.id); used = get_usage(m.from_user.id); limit = get_limit(u["tariff"])
    if used >= limit: return await m.answer("⛔ Ліміт запитів вичерпано.")
    
    proc = await m.answer("📡 Сканування...")
    try:
        res = await run_osint(m.text)
        increment_usage(m.from_user.id)
        out = f"<b>Результат {APP_NAME}:</b>\nЦіль: <code>{res['query']}</code>\n{res['vt_summary']}\n\n<b>AI Аналіз:</b>\n{res['ai_summary'] or 'N/A'}"
        await proc.edit_text(out, parse_mode=ParseMode.HTML)
    except Exception as e:
        await proc.edit_text(f"❌ Помилка: {str(e)}")

async def main():
    init_db()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
