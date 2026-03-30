import os
import re
import sqlite3
import asyncio
import logging
import shutil
import subprocess
from pathlib import Path
from datetime import datetime

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, FSInputFile

# --- CONFIG ---
ADMIN_ID = 8089452251
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DB_PATH = "nexara_bot.db"
DOSSIER_DIR = Path("dossiers")
DOSSIER_DIR.mkdir(exist_ok=True)

FORBIDDEN_DATA = ["Тихончук", "Олександр", "Сергійович", "380979218708", "380960391586", "14.09.1998", "tikhonchuk.sasha"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
dp = Dispatcher()
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

# --- UI MENU ---
def main_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔍 Новий пошук"), KeyboardButton(text="📂 Мої результати")],
            [KeyboardButton(text="📄 PDF досьє"), KeyboardButton(text="👤 Профіль")],
            [KeyboardButton(text="💎 VIP / Тарифи"), KeyboardButton(text="🆘 Підтримка")],
        ],
        resize_keyboard=True
    )

# --- DB LOGIC ---
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS usage_daily 
            (user_id INTEGER, day_key TEXT, count INTEGER DEFAULT 0, last_query TEXT, last_results TEXT, 
            PRIMARY KEY (user_id, day_key))""")
        conn.commit()

def get_user_data(user_id):
    day_key = datetime.utcnow().strftime("%Y-%m-%d")
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute("SELECT * FROM usage_daily WHERE user_id = ? AND day_key = ?", (user_id, day_key)).fetchone()

def update_user_query(user_id, query, results_json):
    day_key = datetime.utcnow().strftime("%Y-%m-%d")
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""INSERT INTO usage_daily (user_id, day_key, count, last_query, last_results) 
            VALUES (?, ?, 1, ?, ?) 
            ON CONFLICT(user_id, day_key) DO UPDATE SET 
            count = count + 1, last_query = ?, last_results = ?""", 
            (user_id, day_key, query, results_json, query, results_json))
        conn.commit()

# --- OSINT ENGINE ---
async def perform_search(query):
    query = query.lstrip("@")
    m_bin = shutil.which("maigret")
    if not m_bin: return []
    # Швидкий пошук по топ-сайтах
    proc = await asyncio.create_subprocess_exec(
        m_bin, query, "--timeout", "15", "--no-color", "--top-sites", "100",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, _ = await proc.communicate()
    out = stdout.decode()
    return [line.split()[-1] for line in out.splitlines() if "http" in line and "[+]" in line]

def generate_pdf(query, hits):
    filename = f"report_{datetime.utcnow().strftime('%H%M%S')}.pdf"
    path = DOSSIER_DIR / filename
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setFont("Helvetica-Bold", 16)
    c.drawString(50, 800, f"NEXARA OSINT REPORT: {query}")
    c.setFont("Helvetica", 10)
    y = 770
    for h in hits:
        c.drawString(50, y, f"FOUND: {h}")
        y -= 15
        if y < 50: c.showPage(); y = 800
    c.save()
    return str(path)

# --- HANDLERS ---
@dp.message(CommandStart())
async def cmd_start(m: Message):
    await m.answer("🛡️ <b>NEXARA CORE SYSTEM v2.0</b>\nВведіть дані для пошуку або скористайтеся меню.", reply_markup=main_menu())

@dp.message(F.text == "🔍 Новий пошук")
async def nav_search(m: Message):
    await m.answer("📝 Надішліть нікнейм, email або телефон для аналізу:", reply_markup=main_menu())

@dp.message(F.text == "📂 Мої результати")
async def nav_res(m: Message):
    data = get_user_data(m.from_user.id)
    if not data or not data[3]: # last_query
        return await m.answer("📭 Ви ще не робили запитів сьогодні.")
    res = json.loads(data[4]) if data[4] else []
    await m.answer(f"📊 <b>Останній запит:</b> <code>{data[3]}</code>\n<b>Знайдено посилань:</b> {len(res)}")

@dp.message(F.text == "📄 PDF досьє")
async def nav_pdf(m: Message):
    data = get_user_data(m.from_user.id)
    if not data or not data[4]:
        return await m.answer("❌ Немає даних для генерації PDF. Спочатку виконайте пошук.")
    hits = json.loads(data[4])
    path = generate_pdf(data[3], hits)
    await m.answer_document(FSInputFile(path), caption=f"📄 Звіт по запиту: {data[3]}")

@dp.message(F.text == "👤 Профіль")
async def nav_profile(m: Message):
    data = get_user_data(m.from_user.id)
    used = data[2] if data else 0
    limit = "Безліміт" if m.from_user.id == ADMIN_ID else os.getenv("FREE_DAILY_LIMIT", "1")
    await m.answer(f"👤 <b>Профіль користувача</b>\n\nID: <code>{m.from_user.id}</code>\nЗапитів сьогодні: {used} / {limit}")

@dp.message(F.text == "💎 VIP / Тарифи")
async def nav_vip(m: Message):
    await m.answer("💎 <b>Тарифні плани:</b>\n\n1. <b>FREE:</b> 1 запит/день (доступно)\n2. <b>INTEL:</b> 5 запитів/день\n3. <b>WARROOM:</b> Безліміт\n\nДля активації зверніться в підтримку.")

@dp.message(F.text == "🆘 Підтримка")
async def nav_help(m: Message):
    await m.answer("🆘 <b>Технічна підтримка:</b>\nЗв'язок з оператором: @grim5225")

@dp.message()
async def process_osint(m: Message):
    if not m.text or m.text.startswith("/"): return
    
    # Privacy check
    if any(x.lower() in m.text.lower() for x in FORBIDDEN_DATA) and m.from_user.id != ADMIN_ID:
        return await m.answer("❌ Доступ до цих даних обмежений протоколом безпеки.")

    # Limit check
    data = get_user_data(m.from_user.id)
    used = data[2] if data else 0
    if m.from_user.id != ADMIN_ID and used >= int(os.getenv("FREE_DAILY_LIMIT", "1")):
        return await m.answer("⛔ Ліміт безкоштовних запитів вичерпано.")

    msg = await m.answer("🔍 <b>Запуск OSINT-модулів...</b>")
    hits = await perform_search(m.text)
    update_user_query(m.from_user.id, m.text, json.dumps(hits))

    if not hits:
        return await msg.edit_text("❌ За вказаними даними нічого не знайдено в публічних джерелах.")

    await msg.edit_text(f"✅ Знайдено результатів: <b>{len(hits)}</b>\nВи можете завантажити повний звіт натиснувши <b>📄 PDF досьє</b>")

async def main():
    init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    import json
    asyncio.run(main())
