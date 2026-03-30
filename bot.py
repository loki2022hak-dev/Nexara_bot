import os
import re
import json
import html
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
ADMIN_ID = 8089452251  # Оновлено на твій ID
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DB_PATH = "nexara_bot.db"
DOSSIER_DIR = Path("dossiers")
DOSSIER_DIR.mkdir(exist_ok=True)

# Чорний список (дані, які бот ігнорує для всіх, крім адміна)
FORBIDDEN_DATA = [
    "Тихончук", "Олександр", "Сергійович", 
    "380979218708", "380960391586", "14.09.1998",
    "tikhonchuk.sasha"
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
dp = Dispatcher()
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

def is_forbidden(text, user_id):
    if user_id == ADMIN_ID:
        return False
    t = text.lower()
    return any(item.lower() in t for item in FORBIDDEN_DATA)

# --- DB LOGIC ---
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS usage_daily (user_id INTEGER, day_key TEXT, count INTEGER DEFAULT 0, PRIMARY KEY (user_id, day_key))")
        conn.commit()

def check_limit(user_id):
    if user_id == ADMIN_ID:
        return True, 0, "Unlimited"
    day_key = datetime.utcnow().strftime("%Y-%m-%d")
    limit = int(os.getenv("FREE_DAILY_LIMIT", "1"))
    with get_db() as conn:
        usage = conn.execute("SELECT count FROM usage_daily WHERE user_id = ? AND day_key = ?", (user_id, day_key)).fetchone()
        current_count = usage["count"] if usage else 0
        return current_count < limit, current_count, limit

def increment_usage(user_id):
    if user_id == ADMIN_ID: return
    day_key = datetime.utcnow().strftime("%Y-%m-%d")
    with get_db() as conn:
        conn.execute("INSERT INTO usage_daily (user_id, day_key, count) VALUES (?, ?, 1) ON CONFLICT(user_id, day_key) DO UPDATE SET count = count + 1", (user_id, day_key))
        conn.commit()

# --- OSINT ENGINE ---
def run_cmd(args):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=120)
        return p.stdout if p.returncode == 0 else ""
    except: return ""

async def username_search(username):
    username = username.lstrip("@")
    m_bin = shutil.which("maigret")
    out = run_cmd([m_bin, username, "--timeout", "10", "--no-color", "--top-sites", "50"]) if m_bin else ""
    hits = [line.split()[-1] for line in out.splitlines() if "http" in line and "[+]" in line]
    return hits

def make_pdf(query, hits):
    path = DOSSIER_DIR / f"dossier_{datetime.utcnow().strftime('%H%M%S')}.pdf"
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setFont("Helvetica-Bold", 16)
    c.drawString(50, 800, f"NEXARA REPORT: {query}")
    c.setFont("Helvetica", 12)
    y = 770
    if not hits: c.drawString(50, y, "No public profiles found.")
    for h in hits[:30]:
        c.drawString(50, y, f"- {h}")
        y -= 20
        if y < 50: c.showPage(); y = 800
    c.save()
    return str(path)

# --- HANDLERS ---
@dp.message(CommandStart())
async def start(m: Message):
    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="👤 Профіль")]], resize_keyboard=True)
    await m.answer("🛡️ <b>NEXARA SYSTEM ACTIVE</b>", reply_markup=kb)

@dp.message(F.text == "👤 Профіль")
async def profile(m: Message):
    _, used, total = check_limit(m.from_user.id)
    status = "ADMIN" if m.from_user.id == ADMIN_ID else "USER"
    await m.answer(f"Status: {status}\nID: <code>{m.from_user.id}</code>\nUsage: {used}/{total}")

@dp.message()
async def handle_all(m: Message):
    if not m.text or m.text.startswith("/"): return
    
    if is_forbidden(m.text, m.from_user.id):
        return await m.answer("❌ <b>Помилка:</b> Дані захищені або доступ обмежений.")

    allowed, used, total = check_limit(m.from_user.id)
    if not allowed:
        return await m.answer(f"⛔ Ліміт: {total} запит/день.")

    status_msg = await m.answer("⏳ <b>Обробка...</b>")
    hits = await username_search(m.text)
    increment_usage(m.from_user.id)
    
    if not hits:
        return await status_msg.edit_text("❌ Результатів не знайдено.")

    pdf_path = make_pdf(m.text, hits)
    await status_msg.delete()
    await m.answer_document(FSInputFile(pdf_path), caption="✅ Звіт сформовано.")

async def main():
    init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
