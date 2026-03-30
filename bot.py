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

# --- КУРС НА МАКСИМУМ ---
ADMIN_ID = 8089452251
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DB_PATH = "nexara_bot.db"
DOSSIER_DIR = Path("dossiers")
DOSSIER_DIR.mkdir(exist_ok=True)

# КЛЮЧІ (Maigret, Sherlock, Shodan, VT, Venice AI - все в ділі)
VT_KEY = os.getenv("VT_API_KEY")
SHODAN_KEY = os.getenv("SHODAN_API_KEY")
VENICE_KEY = os.getenv("VENICE_API_KEY")

# СПИСОК ВИКЛЮЧЕНЬ (Тільки те, що ти просив заблокувати)
FORBIDDEN_DATA = ["Тихончук", "Олександр", "Сергійович", "380979218708", "380960391586", "14.09.1998", "tikhonchuk.sasha"]

logging.basicConfig(level=logging.INFO)
dp = Dispatcher()
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

# --- DATABASE ---
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS usage_daily 
            (user_id INTEGER, day_key TEXT, count INTEGER DEFAULT 0, 
             last_query TEXT, last_results TEXT, PRIMARY KEY (user_id, day_key))""")

def get_stats(uid):
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute("SELECT * FROM usage_daily WHERE user_id = ? AND day_key = ?", (uid, datetime.utcnow().strftime("%Y-%m-%d"))).fetchone()

def save_res(uid, q, res):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""INSERT INTO usage_daily (user_id, day_key, count, last_query, last_results) 
            VALUES (?, ?, 1, ?, ?) ON CONFLICT(user_id, day_key) DO UPDATE SET 
            count=count+1, last_query=?, last_results=?""", (uid, datetime.utcnow().strftime("%Y-%m-%d"), q, json.dumps(res), q, json.dumps(res)))

# --- МОДУЛІ ПОШУКУ (БЕЗ ОБМЕЖЕНЬ) ---

async def run_maigret(target):
    """Глибокий скан Maigret"""
    process = await asyncio.create_subprocess_exec("python3", "-m", "maigret", target, "--timeout", "30", "--top-sites", "500", "--no-color", stdout=asyncio.subprocess.PIPE)
    stdout, _ = await process.communicate()
    return [l.split()[-1] for l in stdout.decode().splitlines() if "http" in l and "[+]" in l]

async def run_sherlock(target):
    """Швидкий скан Sherlock"""
    process = await asyncio.create_subprocess_exec("sherlock", target, "--timeout", "20", "--no-color", stdout=asyncio.subprocess.PIPE)
    stdout, _ = await process.communicate()
    return [l.split()[-1] for l in stdout.decode().splitlines() if "http" in l]

async def run_shodan(ip):
    """Мережевий скан Shodan"""
    if not SHODAN_KEY: return []
    async with aiohttp.ClientSession() as s:
        async with s.get(f"https://api.shodan.io/shodan/host/{ip}?key={SHODAN_KEY}") as r:
            if r.status != 200: return []
            d = await r.json()
            return [f"ISP: {d.get('isp')}", f"Ports: {d.get('ports')}", f"OS: {d.get('os')}"]

async def run_vt(target, t_type="ip_addresses"):
    """Аналіз репутації VirusTotal"""
    if not VT_KEY: return []
    async with aiohttp.ClientSession() as s:
        async with s.get(f"https://www.virustotal.com/api/v3/{t_type}/{target}", headers={"x-apikey": VT_KEY}) as r:
            if r.status != 200: return []
            d = await r.json()
            m = d.get('data', {}).get('attributes', {}).get('last_analysis_stats', {}).get('malicious', 0)
            return [f"VT Malicious Score: {m}"]

async def ai_generate_dossier(query, raw_data):
    """Фінальна збірка досьє через Venice AI (ПІБ, Машини, Робота, Родичі)"""
    if not VENICE_KEY: return "AI Ключ відсутній."
    prompt = f"""
    Твоя роль: Елітний OSINT-аналітик. Об'єкт: {query}.
    Дані: {str(raw_data)}.
    Зроби повний пробив:
    1. ПІБ, дата народження, адреса реєстрації.
    2. Автомобілі (номери, техпаспорти), майно.
    3. Сім'я (дружина, діти), близькі контакти.
    4. Робота (посада, компанія, бізнес).
    5. Реєстри (суди, борги, штрафи).
    6. Всі знайдені акаунти.
    Відповідай суворо, по факту, українською.
    """
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post("https://api.venice.ai/api/v1/chat/completions", 
                headers={"Authorization": f"Bearer {VENICE_KEY}"},
                json={"model": "llama-3.1-8b", "messages": [{"role": "system", "content": prompt}]}) as r:
                res = await r.json()
                return res['choices'][0]['message']['content']
    except: return "Помилка AI аналізу."

# --- INTERFACE ---
def get_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🔍 Новий пошук"), KeyboardButton(text="📂 Мої результати")],
        [KeyboardButton(text="📄 PDF досьє"), KeyboardButton(text="👤 Профіль")]
    ], resize_keyboard=True)

@dp.message(CommandStart())
async def start(m: Message):
    await m.answer("🕵️ <b>NEXARA UNLIMITED OSINT v6.0</b>\nВсі двигуни запущені.", reply_markup=get_kb())

@dp.message(F.text == "📄 PDF досьє")
async def send_pdf(m: Message):
    s = get_stats(m.from_user.id)
    if not s: return await m.answer("Дані відсутні.")
    path = DOSSIER_DIR / f"{m.from_user.id}.pdf"
    c = canvas.Canvas(str(path), pagesize=A4)
    c.drawString(50, 800, f"FULL OSINT REPORT: {s[3]}")
    y = 780
    for line in json.loads(s[4]):
        c.drawString(50, y, str(line)); y -= 15
        if y < 50: c.showPage(); y = 800
    c.save()
    await m.answer_document(FSInputFile(str(path)))

@dp.message()
async def engine(m: Message):
    if m.text in ["🔍 Новий пошук", "👤 Профіль", "📂 Мої результати"]:
        s = get_stats(m.from_user.id)
        return await m.answer(f"ID: {m.from_user.id}\nВикористано: {s[2] if s else 0}")

    if any(x.lower() in m.text.lower() for x in FORBIDDEN_DATA) and m.from_user.id != ADMIN_ID:
        return await m.answer("❌ Захищено.")

    msg = await m.answer("⛓️ <b>Запуск Maigret + Sherlock + Shodan... Копаю...</b>")
    q = m.text.strip()
    
    # ПАРАЛЕЛЬНИЙ ЗАПУСК ВСІХ МОДУЛІВ
    if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", q):
        results = await asyncio.gather(run_shodan(q), run_vt(q, "ip_addresses"))
        results = [item for sub in results for item in sub]
    else:
        # Пошук по людях/ніках
        mg, sh = await asyncio.gather(run_maigret(q), run_sherlock(q))
        results = list(set(mg + sh))

    save_res(m.from_user.id, q, results)
    
    # AI Аналіз для формування досьє
    dossier = await ai_generate_dossier(q, results)
    
    await msg.delete()
    await m.answer(f"📄 <b>РЕЗУЛЬТАТИ ПРОБИВУ: {q}</b>\n\n{dossier}", reply_markup=get_kb())

async def main():
    init_db(); await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
