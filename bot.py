import os
import re
import html
import json
import asyncio
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton

# --- CONFIG ---
APP_NAME = "NEXARA"
DB_PATH = Path("nexara_bot.db")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
VT_API_KEY = os.getenv("VT_API_KEY", "").strip()
SHODAN_API_KEY = os.getenv("SHODAN_API_KEY", "").strip()
VENICE_API_KEY = os.getenv("VENICE_API_KEY", "").strip()

LIMITS = {
    "FREE": int(os.getenv("FREE_DAILY_LIMIT", "5")),
    "INTEL": int(os.getenv("INTEL_DAILY_LIMIT", "25")),
    "AGENCY": int(os.getenv("AGENCY_DAILY_LIMIT", "100")),
    "WARROOM": int(os.getenv("WARROOM_DAILY_LIMIT", "500"))
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
dp = Dispatcher()

# --- DATABASE ---
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT, tariff TEXT DEFAULT 'FREE', created_at TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS usage_daily (user_id INTEGER, day_key TEXT, count INTEGER DEFAULT 0, PRIMARY KEY (user_id, day_key))")
        conn.commit()

def upsert_user(user_id, username):
    with get_db() as conn:
        conn.execute("INSERT INTO users (user_id, username, created_at) VALUES (?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET username=?", 
                     (user_id, username, datetime.utcnow().isoformat(), username))
        conn.commit()

def check_limit(user_id):
    day_key = datetime.utcnow().strftime("%Y-%m-%d")
    with get_db() as conn:
        user = conn.execute("SELECT tariff FROM users WHERE user_id = ?", (user_id,)).fetchone()
        tariff = user["tariff"] if user else "FREE"
        usage = conn.execute("SELECT count FROM usage_daily WHERE user_id = ? AND day_key = ?", (user_id, day_key)).fetchone()
        current_count = usage["count"] if usage else 0
        return current_count < LIMITS.get(tariff, 5), current_count, LIMITS.get(tariff, 5)

def increment_usage(user_id):
    day_key = datetime.utcnow().strftime("%Y-%m-%d")
    with get_db() as conn:
        conn.execute("INSERT INTO usage_daily (user_id, day_key, count) VALUES (?, ?, 1) ON CONFLICT(user_id, day_key) DO UPDATE SET count = count + 1", (user_id, day_key))
        conn.commit()

# --- OSINT ENGINE ---
async def fetch(session, url, headers=None):
    try:
        async with session.get(url, headers=headers or {}, timeout=15) as r:
            if r.status == 200: return await r.json()
            return None
    except: return None

async def run_osint(query):
    query = query.strip()
    results = {"query": query, "vt": "N/A", "shodan": "N/A", "ai": "N/A"}
    
    async with aiohttp.ClientSession() as s:
        # VirusTotal
        vt_url = f"https://www.virustotal.com/api/v3/ip_addresses/{query}" if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", query) else f"https://www.virustotal.com/api/v3/domains/{query}"
        vt_data = await fetch(s, vt_url, {"x-apikey": VT_API_KEY})
        if vt_data:
            stats = vt_data.get("data", {}).get("attributes", {}).get("last_analysis_stats", {})
            results["vt"] = f"Malicious: {stats.get('malicious', 0)} | Suspicious: {stats.get('suspicious', 0)}"

        # Shodan (only for IP)
        if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", query):
            sh_data = await fetch(s, f"https://api.shodan.io/shodan/host/{query}?key={SHODAN_API_KEY}")
            if sh_data:
                ports = sh_data.get("ports", [])
                results["shodan"] = f"Open Ports: {', '.join(map(str, ports)) if ports else 'None'}"

        # Venice AI Analysis
        if VENICE_API_KEY:
            try:
                async with s.post("https://api.venice.ai/api/v1/chat/completions", 
                    headers={"Authorization": f"Bearer {VENICE_API_KEY}"},
                    json={"model": "llama-3.1-70b", "messages": [{"role": "system", "content": "You are NEXARA OSINT AI. Analyze data shortly."}, {"role": "user", "content": str(results)}]}) as r:
                    ai_res = await r.json()
                    results["ai"] = ai_res["choices"][0]["message"]["content"]
            except: pass

    return results

# --- HANDLERS ---
@dp.message(CommandStart())
async def cmd_start(m: Message):
    upsert_user(m.from_user.id, m.from_user.username)
    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="👤 Профіль"), KeyboardButton(text="🔍 Допомога")]], resize_keyboard=True)
    await m.answer(f"🛡️ <b>{APP_NAME} OSINT</b> активовано.\nВідправте IP або Домен для аналізу.", reply_markup=kb)

@dp.message(F.text == "👤 Профіль")
async def cmd_profile(m: Message):
    allowed, used, total = check_limit(m.from_user.id)
    await m.answer(f"👤 <b>ID:</b> <code>{m.from_user.id}</code>\n📊 <b>Використано сьогодні:</b> {used}/{total}")

@dp.message()
async def handle_query(m: Message):
    if not m.text or m.text.startswith("/"): return
    
    allowed, used, total = check_limit(m.from_user.id)
    if not allowed:
        return await m.answer("⛔ <b>Ліміт вичерпано.</b> Поверніться завтра або змініть тариф.")

    msg = await m.answer("🔍 <b>Аналіз цілі...</b>")
    res = await run_osint(m.text)
    increment_usage(m.from_user.id)
    
    out = (f"🎯 <b>Ціль:</b> <code>{res['query']}</code>\n"
           f"🛡️ <b>VirusTotal:</b> {res['vt']}\n"
           f"📡 <b>Shodan:</b> {res['shodan']}\n\n"
           f"🤖 <b>AI Аналіз:</b>\n{res['ai']}")
    
    await msg.edit_text(out)

async def main():
    init_db()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
