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

# --- CONFIG ---
ADMIN_ID = 8089452251
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DB_PATH = "nexara_bot.db"
DOSSIER_DIR = Path("dossiers")
DOSSIER_DIR.mkdir(exist_ok=True)

# API KEYS
VT_KEY = os.getenv("VT_API_KEY")
SHODAN_KEY = os.getenv("SHODAN_API_KEY")
VENICE_KEY = os.getenv("VENICE_API_KEY")

FORBIDDEN_DATA = ["Тихончук", "Олександр", "Сергійович", "380979218708", "380960391586", "14.09.1998", "tikhonchuk.sasha"]

logging.basicConfig(level=logging.INFO)
dp = Dispatcher()
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

# --- DB ---
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS usage_daily (user_id INTEGER, day_key TEXT, count INTEGER DEFAULT 0, last_query TEXT, last_results TEXT, PRIMARY KEY (user_id, day_key))")

def get_stats(uid):
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute("SELECT * FROM usage_daily WHERE user_id = ? AND day_key = ?", (uid, datetime.utcnow().strftime("%Y-%m-%d"))).fetchone()

def save_res(uid, q, res):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT INTO usage_daily (user_id, day_key, count, last_query, last_results) VALUES (?, ?, 1, ?, ?) ON CONFLICT(user_id, day_key) DO UPDATE SET count=count+1, last_query=?, last_results=?", (uid, datetime.utcnow().strftime("%Y-%m-%d"), q, json.dumps(res), q, json.dumps(res)))

# --- OSINT ENGINES ---
async def ai_analyze(query, raw_data):
    if not VENICE_KEY: return "AI Ключ не знайдено."
    prompt = f"Ти — OSINT експерт. Знайди і структуруй ВСЕ про {query} з цих даних: {str(raw_data)}. ПІБ, авто, робота, соцмережі, родичі. Відповідай чітко українською."
    async with aiohttp.ClientSession() as s:
        async with s.post("https://api.venice.ai/api/v1/chat/completions", 
            headers={"Authorization": f"Bearer {VENICE_KEY}"},
            json={"model": "llama-3.1-8b", "messages": [{"role": "system", "content": prompt}]}) as r:
            res = await r.json()
            return res['choices'][0]['message']['content']

async def run_maigret(target):
    # Запуск Maigret на повну потужність (через всі доступні сайти)
    process = await asyncio.create_subprocess_exec(
        "python3", "-m", "maigret", target, "--timeout", "40", "--no-color", "--top-sites", "500",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, _ = await process.communicate()
    out = stdout.decode()
    # Витягуємо всі знайдені посилання [+]
    return [l.split()[-1] for l in out.splitlines() if "http" in l and "[+]" in l]

async def run_shodan(ip):
    if not SHODAN_KEY: return []
    async with aiohttp.ClientSession() as s:
        async with s.get(f"https://api.shodan.io/shodan/host/{ip}?key={SHODAN_KEY}") as r:
            if r.status != 200: return []
            d = await r.json()
            return [f"ISP: {d.get('isp')}", f"City: {d.get('city')}", f"Ports: {d.get('ports')}"]

# --- UI ---
def get_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🔍 Новий пошук"), KeyboardButton(text="📂 Мої результати")],
        [KeyboardButton(text="📄 PDF досьє"), KeyboardButton(text="👤 Профіль")]
    ], resize_keyboard=True)

# --- HANDLERS ---
@dp.message(CommandStart())
async def start(m: Message):
    await m.answer("🛡️ <b>NEXARA CORE v4.0 ACTIVE</b>\nВведіть ПІБ, Нік або IP.", reply_markup=get_kb())

@dp.message(F.text == "📄 PDF досьє")
async def pdf_cmd(m: Message):
    s = get_stats(m.from_user.id)
    if not s: return await m.answer("Немає даних.")
    path = DOSSIER_DIR / f"{m.from_user.id}.pdf"
    c = canvas.Canvas(str(path), pagesize=A4)
    c.drawString(50, 800, f"FULL DOSSIER: {s[3]}")
    y = 780
    for h in json.loads(s[4]):
        c.drawString(50, y, str(h)); y -= 15
        if y < 50: c.showPage(); y = 800
    c.save()
    await m.answer_document(FSInputFile(str(path)))

@dp.message()
async def engine(m: Message):
    if m.text in ["🔍 Новий пошук", "👤 Профіль", "📂 Мої результати"]:
        s = get_stats(m.from_user.id)
        u = s[2] if s else 0
        return await m.answer(f"ID: {m.from_user.id}\nЗапитів: {u}")

    if any(x.lower() in m.text.lower() for x in FORBIDDEN_DATA) and m.from_user.id != ADMIN_ID:
        return await m.answer("❌ Обмежено.")

    status = await m.answer("🛰️ <b>Глибоке сканування розпочато...</b>")
    query = m.text.strip()
    
    # Визначаємо тип і запускаємо модулі
    if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", query):
        results = await run_shodan(query)
    else:
        # Для ПІБ або Ніка запускаємо Maigret
        results = await run_maigret(query)

    save_res(m.from_user.id, query, results)
    
    # AI Аналіз зібраного
    report = await ai_analyze(query, results)
    
    await status.delete()
    await m.answer(f"📊 <b>ЗНАЙДЕНО ДЛЯ: {query}</b>\n\n{report}", reply_markup=get_kb())

async def main():
    init_db(); await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
