import os
import re
import io
import json
import html
import time
import shutil
import sqlite3
import asyncio
import logging
import ipaddress
import subprocess
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, FSInputFile
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN not set")

DB_PATH = "nexara_bot.db"
DOSSIER_DIR = Path("dossiers")
DOSSIER_DIR.mkdir(exist_ok=True)

FREE_DAILY_LIMIT = int(os.getenv("FREE_DAILY_LIMIT", "5"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(message)s"
)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

def now_utc():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

def esc(x):
    return html.escape(str(x or ""))

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    cur = conn.cursor()
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
            pdf_path TEXT,
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
    conn.commit()
    conn.close()

def upsert_user(user_id: int, username: str | None, first_name: str | None):
    conn = db(); cur = conn.cursor(); ts = now_utc()
    cur.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    if cur.fetchone():
        cur.execute("UPDATE users SET username = ?, first_name = ?, updated_at = ? WHERE user_id = ?", (username, first_name, ts, user_id))
    else:
        cur.execute("INSERT INTO users (user_id, username, first_name, tariff, created_at, updated_at) VALUES (?, ?, ?, 'FREE', ?, ?)", (user_id, username, first_name, ts, ts))
    conn.commit(); conn.close()

def total_searches(user_id: int) -> int:
    conn = db(); cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS cnt FROM searches WHERE user_id = ?", (user_id,))
    row = cur.fetchone(); conn.close()
    return int(row["cnt"]) if row else 0

def get_history(user_id: int, limit: int = 10):
    conn = db(); cur = conn.cursor()
    cur.execute("SELECT id, query, query_type, pdf_path, created_at FROM searches WHERE user_id = ? ORDER BY id DESC LIMIT ?", (user_id, limit))
    rows = cur.fetchall(); conn.close(); return rows

def save_search(user_id: int, query: str, query_type: str, result: dict, pdf_path: str | None):
    conn = db(); cur = conn.cursor()
    cur.execute("INSERT INTO searches (user_id, query, query_type, result_json, pdf_path, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, query, query_type, json.dumps(result, ensure_ascii=False), pdf_path, now_utc()))
    conn.commit(); conn.close()

def day_key(): return datetime.utcnow().strftime("%Y-%m-%d")

def daily_used(user_id: int) -> int:
    conn = db(); cur = conn.cursor()
    cur.execute("SELECT count FROM daily_usage WHERE user_id = ? AND day_key = ?", (user_id, day_key()))
    row = cur.fetchone(); conn.close()
    return int(row["count"]) if row else 0

def bump_daily(user_id: int):
    conn = db(); cur = conn.cursor(); dk = day_key()
    cur.execute("SELECT count FROM daily_usage WHERE user_id = ? AND day_key = ?", (user_id, dk))
    if cur.fetchone():
        cur.execute("UPDATE daily_usage SET count = count + 1 WHERE user_id = ? AND day_key = ?", (user_id, dk))
    else:
        cur.execute("INSERT INTO daily_usage (user_id, day_key, count) VALUES (?, ?, 1)", (user_id, dk))
    conn.commit(); conn.close()

def menu():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🔍 Новий пошук"), KeyboardButton(text="📂 Мої результати")],
        [KeyboardButton(text="📄 PDF досьє"), KeyboardButton(text="👤 Профіль")],
        [KeyboardButton(text="💎 VIP / Тарифи"), KeyboardButton(text="🆘 Підтримка")],
    ], resize_keyboard=True)

def detect_type(text: str) -> str:
    q = text.strip(); ql = q.lower()
    if ql.startswith(("http://", "https://")): return "url"
    try: ipaddress.ip_address(q); return "ip"
    except: pass
    if re.fullmatch(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", q): return "email"
    if re.fullmatch(r"(?!-)(?:[a-zA-Z0-9-]{1,63}\.)+[A-Za-z]{2,63}", q): return "domain"
    if re.fullmatch(r"@?[A-Za-z0-9_][A-Za-z0-9_.-]{2,40}", q): return "username"
    return "text"

async def fetch_json(session, url, headers=None, timeout=20):
    try:
        async with session.get(url, headers=headers or {}, timeout=timeout) as r:
            txt = await r.text()
            if "json" in r.headers.get("content-type", ""): return True, json.loads(txt)
            return True, {"raw": txt[:3000], "status": r.status}
    except Exception as e: return False, {"error": str(e)}

async def dns_lookup(session, domain, rtype):
    ok, data = await fetch_json(session, f"https://cloudflare-dns.com/dns-query?name={domain}&type={rtype}", headers={"accept": "application/dns-json"})
    return {"ok": ok, "type": rtype, "data": data}

async def domain_osint(domain: str) -> dict:
    async with aiohttp.ClientSession() as s:
        res = await asyncio.gather(dns_lookup(s, domain, "A"), dns_lookup(s, domain, "MX"), dns_lookup(s, domain, "NS"), dns_lookup(s, domain, "TXT"))
    return {"type": "domain", "query": domain, "generated_at": now_utc(), "dns": {r["type"]: r for r in res}}

async def ip_osint(ip: str) -> dict:
    async with aiohttp.ClientSession() as s:
        ok, data = await fetch_json(s, f"https://rdap.org/ip/{ip}")
    return {"type": "ip", "query": ip, "generated_at": now_utc(), "rdap": {"ok": ok, "data": data}}

async def url_osint(url: str) -> dict:
    p = urlparse(url); dom = p.netloc.lower()
    return {"type": "url", "query": url, "generated_at": now_utc(), "parsed": {"scheme": p.scheme, "domain": dom, "path": p.path}}

async def email_osint(email: str) -> dict:
    local, _, domain = email.partition("@")
    return {"type": "email", "query": email, "generated_at": now_utc(), "local_part": local, "domain_part": domain}

def run_cmd(args, timeout=180):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return {"ok": p.returncode == 0, "stdout": p.stdout[:200000], "stderr": p.stderr[:50000]}
    except: return {"ok": False, "error": "timeout or crash"}

async def username_osint(username: str) -> dict:
    username = username.lstrip("@")
    m_bin, s_bin = shutil.which("maigret"), shutil.which("sherlock")
    m_data = run_cmd([m_bin, username, "--timeout", "20", "--no-color", "--top-sites", "100"]) if m_bin else {"ok": False}
    s_data = run_cmd([s_bin, username]) if s_bin else {"ok": False}
    return {"type": "username", "query": username, "generated_at": now_utc(), "maigret": m_data, "sherlock": s_data}

def build_summary(result: dict) -> list[str]:
    t, lines = result.get("type"), []
    if t == "username":
        lines.append(f"Maigret: {'ok' if result.get('maigret', {}).get('ok') else 'fail/none'}")
        lines.append(f"Sherlock: {'ok' if result.get('sherlock', {}).get('ok') else 'fail/none'}")
    elif t == "domain":
        for k, v in result.get("dns", {}).items(): lines.append(f"{k}: record count check in PDF")
    elif t == "ip": lines.append(f"IP info gathered")
    else: lines.append("Analysis complete")
    return lines

def render_result(result: dict) -> str:
    parts = [f"<b>🧠 {APP_NAME} RESULT</b>", f"Запит: <code>{esc(result.get('query'))}</code>", f"Тип: {result.get('type')}", ""]
    parts.extend([f"• {esc(l)}" for l in build_summary(result)])
    return "\n".join(parts)

def make_pdf(result: dict) -> str:
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    path = DOSSIER_DIR / f"dossier_{ts}.pdf"
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setFont("Helvetica-Bold", 14); c.drawString(40, 800, "NEXARA OSINT REPORT")
    c.setFont("Helvetica", 10); c.drawString(40, 780, f"Query: {result.get('query')}")
    c.drawString(40, 765, f"Type: {result.get('type')}"); c.save()
    return str(path)

async def full_osint(query: str) -> dict:
    qt = detect_type(query)
    if qt == "username": return await username_osint(query)
    if qt == "domain": return await domain_osint(query.lower())
    if qt == "ip": return await ip_osint(query)
    if qt == "url": return await url_osint(query)
    if qt == "email": return await email_osint(query.lower())
    return {"type": "text", "query": query, "generated_at": now_utc()}

@dp.message(CommandStart())
async def start_cmd(m: Message):
    upsert_user(m.from_user.id, m.from_user.username, m.from_user.first_name)
    await m.answer("<b>🚀 NEXARA ONLINE</b>\nНадішли IP/Username/Domain.", reply_markup=menu())

@dp.message(F.text == "👤 Профіль")
async def profile_btn(m: Message):
    used = daily_used(m.from_user.id)
    await m.answer(f"<b>👤 Профіль</b>\nID: <code>{m.from_user.id}</code>\nЗапити сьогодні: {used}/{FREE_DAILY_LIMIT}")

@dp.message(F.text)
async def handle(m: Message):
    if m.text.startswith("/"): return
    used = daily_used(m.from_user.id)
    if used >= FREE_DAILY_LIMIT: return await m.answer("⛔ Ліміт вичерпано.")
    
    wait = await m.answer("⏳ Сканування...")
    try:
        res = await full_osint(m.text)
        pdf = make_pdf(res)
        save_search(m.from_user.id, m.text, res["type"], res, pdf)
        bump_daily(m.from_user.id)
        await wait.edit_text(render_result(res))
        if pdf and os.path.exists(pdf): await m.answer_document(FSInputFile(pdf))
    except Exception as e: await wait.edit_text(f"❌ Помилка: {esc(e)}")

async def main():
    init_db(); await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
