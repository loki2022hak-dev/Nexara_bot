import os
import re
import json
import sqlite3
import asyncio
import logging
import shutil
import subprocess
from pathlib import Path
from datetime import datetime
import aiohttp

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, FSInputFile
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

# --- CONFIGURATION ---
ADMIN_ID = 8089452251
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DB_PATH = "nexara_bot.db"
DOSSIER_DIR = Path("dossiers")
DOSSIER_DIR.mkdir(exist_ok=True)

VT_KEY = os.getenv("VT_API_KEY")
SHODAN_KEY = os.getenv("SHODAN_API_KEY")
VENICE_KEY = os.getenv("VENICE_API_KEY")

FORBIDDEN_DATA = ["Тихончук", "Олександр", "Сергійович", "380979218708", "380960391586", "14.09.1998", "tikhonchuk.sasha"]

logging.basicConfig(level=logging.INFO)
dp = Dispatcher()
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

# --- DATABASE ENGINE ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS usage_daily 
        (user_id INTEGER, day_key TEXT, count INTEGER DEFAULT 0, 
         last_query TEXT, last_results TEXT, PRIMARY KEY (user_id, day_key))""")
    conn.commit()
    conn.close()

def get_user_stats(user_id):
    day_key = datetime.utcnow().strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_PATH)
    res = conn.execute("SELECT * FROM usage_daily WHERE user_id = ? AND day_key = ?", (user_id, day_key)).fetchone()
    conn.close()
    return res

def save_search(user_id, query, results):
    day_key = datetime.utcnow().strftime("%Y-%m-%d")
    res_json = json.dumps(results)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""INSERT INTO usage_daily (user_id, day_key, count, last_query, last_results) 
        VALUES (?, ?, 1, ?, ?) ON CONFLICT(user_id, day_key) DO UPDATE SET 
        count = count + 1, last_query = ?, last_results = ?""", 
        (user_id, day_key, query, res_json, query, res_json))
    conn.commit()
    conn.close()

# --- OSINT MODULES ---
async def call_venice_ai(data_summary):
    if not VENICE_KEY: return "AI Offline"
    url = "https://api.venice.ai/api/v1/chat/completions"
    headers = {"Authorization": f"Bearer {VENICE_KEY}"}
    payload = {
        "model": "llama-3.1-8b",
        "messages": [
            {"role": "system", "content": "Ти головний аналітик NEXARA. Зроби стислий OSINT висновок українською."},
            {"role": "user", "content": str(data_summary)}
        ]
    }
    async with aiohttp.ClientSession() as s:
        async with s.post(url, headers=headers, json=payload) as r:
            res = await r.json()
            return res['choices'][0]['message']['content']

async def get_shodan_data(ip):
    if not SHODAN_KEY: return {}
    async with aiohttp.ClientSession() as s:
        async with s.get(f"https://api.shodan.io/shodan/host/{ip}?key={SHODAN_KEY}") as r:
            return await r.json() if r.status == 200 else {}

async def get_vt_data(target, endpoint):
    if not VT_KEY: return {}
    async with aiohttp.ClientSession() as s:
        async with s.get(f"https://www.virustotal.com/api/v3/{endpoint}/{target}", headers={"x-apikey": VT_KEY}) as r:
            return await r.json() if r.status == 200 else {}

async def run_maigret(username):
    m_bin = shutil.which("maigret")
    p = await asyncio.create_subprocess_exec(m_bin, username, "--timeout", "20", "--top-sites", "100", "--no-color", stdout=asyncio.subprocess.PIPE)
    stdout, _ = await p.communicate()
    return [l.split()[-1] for l in stdout.decode().splitlines() if "http" in l and "[+]" in l]

# --- NAVIGATION ---
def main_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🔍 Новий пошук"), KeyboardButton(text="📂 Мої результати")],
        [KeyboardButton(text="📄 PDF досьє"), KeyboardButton(text="👤 Профіль")],
        [KeyboardButton(text="💎 VIP / Тарифи"), KeyboardButton(text="🆘 Підтримка")]
    ], resize_keyboard=True)

# --- HANDLERS ---
@dp.message(CommandStart())
async def start(m: Message):
    await m.answer("🛡️ <b>NEXARA CORE SYSTEM ONLINE</b>", reply_markup=main_kb())

@dp.message(F.text == "🔍 Новий пошук")
async def h_search(m: Message):
    await m.answer("Введіть нікнейм, IP або Hash:")

@dp.message(F.text == "👤 Профіль")
async def h_profile(m: Message):
    s = get_user_stats(m.from_user.id)
    u = s[2] if s else 0
    await m.answer(f"ID: {m.from_user.id}\nЗапити: {u} (Admin: {m.from_user.id == ADMIN_ID})")

@dp.message(F.text == "📂 Мої результати")
async def h_res(m: Message):
    s = get_user_stats(m.from_user.id)
    if not s or not s[3]: return await m.answer("Порожньо.")
    await m.answer(f"Останній: {s[3]}\nЗнайдено: {len(json.loads(s[4]))}")

@dp.message(F.text == "📄 PDF досьє")
async def h_pdf(m: Message):
    s = get_user_stats(m.from_user.id)
    if not s or not s[4]: return await m.answer("Немає даних.")
    path = DOSSIER_DIR / f"{m.from_user.id}.pdf"
    c = canvas.Canvas(str(path), pagesize=A4)
    c.drawString(50, 800, f"NEXARA REPORT: {s[3]}")
    y = 780
    for h in json.loads(s[4])[:50]:
        c.drawString(50, y, str(h)); y -= 15
        if y < 50: c.showPage(); y = 800
    c.save()
    await m.answer_document(FSInputFile(str(path)))

@dp.message(F.text == "💎 VIP / Тарифи")
async def h_vip(m: Message):
    await m.answer("FREE: 1/day\nVIP: Unlimited\nContact: @grim5225")

@dp.message(F.text == "🆘 Підтримка")
async def h_help(m: Message):
    await m.answer("Admin: @grim5225")

@dp.message()
async def core(m: Message):
    if not m.text or m.text.startswith("/"): return
    if any(p.lower() in m.text.lower() for p in FORBIDDEN_DATA) and m.from_user.id != ADMIN_ID:
        return await m.answer("❌ Захищено.")
    s = get_user_stats(m.from_user.id)
    if m.from_user.id != ADMIN_ID and s and s[2] >= 1:
        return await m.answer("⛔ Ліміт вичерпано.")

    msg = await m.answer("📡 <b>Аналіз...</b>")
    q = m.text.strip()
    res = []

    if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", q):
        sh = await get_shodan_data(q)
        vt = await get_vt_data(q, "ip_addresses")
        res = [f"ISP: {sh.get('isp')}", f"City: {sh.get('city')}", f"VT Score: {vt.get('data', {}).get('attributes', {}).get('last_analysis_stats', {}).get('malicious')}"]
    elif len(q) in [32, 40, 64]:
        vt = await get_vt_data(q, "files")
        res = [f"Name: {vt.get('data', {}).get('attributes', {}).get('meaningful_name')}", f"Malicious: {vt.get('data', {}).get('attributes', {}).get('last_analysis_stats', {}).get('malicious')}"]
    else:
        res = await run_maigret(q)

    save_search(m.from_user.id, q, res)
    ai = await call_venice_ai(res)
    await msg.delete()
    await m.answer(f"✅ <b>Знайдено: {len(res)}</b>\n\n🤖 <b>AI:</b> {ai}", reply_markup=main_kb())

async def main():
    init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
