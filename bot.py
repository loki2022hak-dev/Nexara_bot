import os
import re
import html
import json
import time
import asyncio
import logging
import sqlite3
import ipaddress
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, quote

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton

APP_NAME = "NEXARA"
DB_PATH = Path("nexara_bot.db")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

FREE_DAILY_LIMIT = int(os.getenv("FREE_DAILY_LIMIT", "5"))
INTEL_DAILY_LIMIT = int(os.getenv("INTEL_DAILY_LIMIT", "25"))
AGENCY_DAILY_LIMIT = int(os.getenv("AGENCY_DAILY_LIMIT", "100"))
WARROOM_DAILY_LIMIT = int(os.getenv("WARROOM_DAILY_LIMIT", "500"))

VT_API_KEY = os.getenv("VT_API_KEY", "").strip()
SHODAN_API_KEY = os.getenv("SHODAN_API_KEY", "").strip()
CENSYS_BEARER_TOKEN = os.getenv("CENSYS_BEARER_TOKEN", "").strip()
VENICE_API_KEY = os.getenv("VENICE_API_KEY", "").strip()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(APP_NAME)

dp = Dispatcher()

def now_ts() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

def escape(s: str) -> str:
    return html.escape(str(s or ""))

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
        last_name TEXT,
        tariff TEXT NOT NULL DEFAULT 'FREE',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS searches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        query_type TEXT NOT NULL,
        raw_query TEXT NOT NULL,
        normalized_query TEXT NOT NULL,
        result_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS usage_daily (
        user_id INTEGER NOT NULL,
        day_key TEXT NOT NULL,
        count INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (user_id, day_key)
    )""")
    c.commit()
    c.close()

def upsert_user(user_id: int, username: str | None, first_name: str | None, last_name: str | None) -> None:
    c = conn()
    cur = c.cursor()
    ts = now_ts()
    cur.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    if cur.fetchone():
        cur.execute("UPDATE users SET username=?, first_name=?, last_name=?, updated_at=? WHERE user_id=?", (username, first_name, last_name, ts, user_id))
    else:
        cur.execute("INSERT INTO users (user_id, username, first_name, last_name, tariff, created_at, updated_at) VALUES (?, ?, ?, ?, 'FREE', ?, ?)", (user_id, username, first_name, last_name, ts, ts))
    c.commit()
    c.close()

def get_user(user_id: int):
    c = conn(); cur = c.cursor(); cur.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)); row = cur.fetchone(); c.close(); return row

def save_search(user_id: int, q_type: str, raw: str, norm: str, res: dict) -> None:
    c = conn(); cur = c.cursor(); cur.execute("INSERT INTO searches (user_id, query_type, raw_query, normalized_query, result_json, created_at) VALUES (?,?,?,?,?,?)", (user_id, q_type, raw, norm, json.dumps(res, ensure_ascii=False), now_ts())); c.commit(); c.close()

def get_recent_searches(user_id: int, limit: int = 10):
    c = conn(); cur = c.cursor(); cur.execute("SELECT * FROM searches WHERE user_id = ? ORDER BY id DESC LIMIT ?", (user_id, limit)); rows = cur.fetchall(); c.close(); return rows

def get_total_searches(user_id: int) -> int:
    c = conn(); cur = c.cursor(); cur.execute("SELECT COUNT(*) AS cnt FROM searches WHERE user_id = ?", (user_id,)); row = cur.fetchone(); c.close(); return int(row["cnt"]) if row else 0

def day_key() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d")

def get_daily_limit(user_id: int) -> int:
    row = get_user(user_id)
    tariff = (row["tariff"] if row else "FREE").upper()
    if tariff == "INTEL": return INTEL_DAILY_LIMIT
    if tariff == "AGENCY": return AGENCY_DAILY_LIMIT
    if tariff == "WARROOM": return WARROOM_DAILY_LIMIT
    return FREE_DAILY_LIMIT

def get_daily_usage(user_id: int) -> int:
    c = conn(); cur = c.cursor(); cur.execute("SELECT count FROM usage_daily WHERE user_id = ? AND day_key = ?", (user_id, day_key())); row = cur.fetchone(); c.close(); return int(row["count"]) if row else 0

def increment_daily_usage(user_id: int) -> None:
    c = conn(); cur = c.cursor(); cur.execute("SELECT count FROM usage_daily WHERE user_id = ? AND day_key = ?", (user_id, day_key()))
    if cur.fetchone():
        cur.execute("UPDATE usage_daily SET count = count + 1 WHERE user_id = ? AND day_key = ?", (user_id, day_key()))
    else:
        cur.execute("INSERT INTO usage_daily (user_id, day_key, count) VALUES (?, ?, 1)", (user_id, day_key()))
    c.commit(); c.close()

def keyboard():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🔍 Новий пошук"), KeyboardButton(text="📂 Історія")],
        [KeyboardButton(text="👤 Профіль"), KeyboardButton(text="💎 VIP / Тарифи")],
        [KeyboardButton(text="⚙️ Налаштування"), KeyboardButton(text="🆘 Підтримка")]
    ], resize_keyboard=True)

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
DOMAIN_RE = re.compile(r"^(?!-)(?:[a-zA-Z0-9-]{1,63}\.)+[A-Za-z]{2,63}$")
HASH_RE = re.compile(r"^[A-Fa-f0-9]{32}$|^[A-Fa-f0-9]{40}$|^[A-Fa-f0-9]{64}$")
USERNAME_RE = re.compile(r"^@?[A-Za-z0-9_][A-Za-z0-9_.-]{2,31}$")

def detect_type(query: str):
    q = query.strip(); lower = q.lower()
    if lower.startswith(("http://", "https://")): return "url", q
    try:
        ipaddress.ip_address(q); return "ip", q
    except: pass
    if EMAIL_RE.match(q): return "email", q.lower()
    if HASH_RE.match(q): return "hash", q.lower()
    if DOMAIN_RE.match(q.lower()): return "domain", q.lower()
    if USERNAME_RE.match(q) and " " not in q: return "username", q.lstrip("@")
    if len(q.split()) >= 2: return "person_or_company", q
    return "generic", q

async def fetch_json(session, url, headers=None):
    try:
        async with session.get(url, headers=headers or {}, timeout=25) as resp:
            txt = await resp.text()
            if "application/json" in resp.headers.get("content-type", ""):
                return resp.status < 400, json.loads(txt)
            return resp.status < 400, {"status": resp.status, "body": txt[:1000]}
    except Exception as e: return False, {"error": str(e)}

async def dns_lookup(session, domain, rtype):
    ok, data = await fetch_json(session, f"https://cloudflare-dns.com/dns-query?name={quote(domain)}&type={quote(rtype)}", {"accept": "application/dns-json"})
    return {"ok": ok, "answers": data.get("Answer", [])}

async def rdap_domain(session, domain): return await fetch_json(session, f"https://rdap.org/domain/{quote(domain)}")
async def rdap_ip(session, ip): return await fetch_json(session, f"https://rdap.org/ip/{quote(ip)}")

async def vt_domain(session, domain): 
    if not VT_API_KEY: return {"ok": False}
    return await fetch_json(session, f"https://www.virustotal.com/api/v3/domains/{quote(domain)}", {"x-apikey": VT_API_KEY})

async def vt_ip(session, ip):
    if not VT_API_KEY: return {"ok": False}
    return await fetch_json(session, f"https://www.virustotal.com/api/v3/ip_addresses/{quote(ip)}", {"x-apikey": VT_API_KEY})

async def vt_file(session, file_hash):
    if not VT_API_KEY: return {"ok": False}
    return await fetch_json(session, f"https://www.virustotal.com/api/v3/files/{quote(file_hash)}", {"x-apikey": VT_API_KEY})

async def shodan_host(session, ip):
    if not SHODAN_API_KEY: return {"ok": False}
    return await fetch_json(session, f"https://api.shodan.io/shodan/host/{quote(ip)}?key={quote(SHODAN_API_KEY)}")

async def censys_host(session, ip):
    if not CENSYS_BEARER_TOKEN: return {"ok": False}
    return await fetch_json(session, f"https://search.censys.io/api/v2/hosts/{quote(ip)}", {"Authorization": f"Bearer {CENSYS_BEARER_TOKEN}"})

async def venice_summary(payload):
    if not VENICE_API_KEY: return None
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post("https://api.venice.ai/api/v1/chat/completions", headers={"Authorization": f"Bearer {VENICE_API_KEY}"}, json={"model": "llama-3.1-70b", "messages": [{"role": "user", "content": f"Аналіз українською:\n{json.dumps(payload)[:12000]}"}]}, timeout=35) as r:
                d = await r.json(); return d["choices"][0]["message"]["content"]
    except: return None

def summarize_vt(vt_data):
    attrs = (vt_data.get("data", {}) or {}).get("attributes", {}) if isinstance(vt_data.get("data"), dict) else {}
    stats = attrs.get("last_analysis_stats", {})
    if stats: return f"VT malicious: {stats.get('malicious', 0)} | suspicious: {stats.get('suspicious', 0)}"
    return "No VT stats"

async def run_search(raw):
    qtype, norm = detect_type(raw)
    res = {"query_type": qtype, "normalized": norm, "generated_at": now_ts(), "summary": []}
    async with aiohttp.ClientSession() as sess:
        if qtype == "domain":
            a, mx, ns, rdap, vt = await asyncio.gather(dns_lookup(sess, norm, "A"), dns_lookup(sess, norm, "MX"), dns_lookup(sess, norm, "NS"), rdap_domain(sess, norm), vt_domain(sess, norm))
            res.update({"dns_a": a, "dns_mx": mx, "dns_ns": ns, "rdap": rdap, "vt": vt})
            res["summary"] = [f"DNS A: {len(a.get('answers', []))}", summarize_vt(vt)]
        elif qtype == "ip":
            rdap, sh, vt, cs = await asyncio.gather(rdap_ip(sess, norm), shodan_host(sess, norm), vt_ip(sess, norm), censys_host(sess, norm))
            res.update({"rdap": rdap, "shodan": sh, "vt": vt, "censys": cs})
            res["summary"] = [f"Shodan ports: {len(sh.get('ports', [])) if sh.get('ok') else 0}", summarize_vt(vt)]
        elif qtype == "hash":
            vt = await vt_file(sess, norm)
            res["vt"] = vt
            res["summary"] = [summarize_vt(vt)]
    ai = await venice_summary(res); res["ai_summary"] = ai
    return res

def render_result(res):
    blocks = [f"<b>🧠 {APP_NAME} RESULT</b>", f"Запит: <code>{escape(res['normalized'])}</code>", f"Тип: {res['query_type']}", "", "<b>Summary:</b>"]
    blocks.extend([f"• {escape(s)}" for s in res.get("summary", [])])
    if res.get("ai_summary"): blocks.extend(["", "<b>AI Summary:</b>", escape(res["ai_summary"])])
    return "\n".join(blocks)

@dp.message(CommandStart())
async def start_cmd(m: Message):
    upsert_user(m.from_user.id, m.from_user.username, m.from_user.first_name, m.from_user.last_name)
    await m.answer(f"🚀 {APP_NAME} ONLINE", reply_markup=keyboard())

@dp.message(F.text == "📂 Історія")
async def history_btn(m: Message):
    rows = get_recent_searches(m.from_user.id)
    txt = "\n".join([f"• <code>{escape(r['normalized_query'])}</code>" for r in rows]) if rows else "Історія порожня."
    await m.answer(f"📂 Останні пошуки:\n{txt}")

@dp.message(F.text == "👤 Профіль")
async def profile_btn(m: Message):
    u = get_user(m.from_user.id); used = get_daily_usage(m.from_user.id); limit = get_daily_limit(m.from_user.id)
    await m.answer(f"👤 <b>Профіль</b>\nID: <code>{m.from_user.id}</code>\nТариф: {u['tariff']}\nЛіміт: {used}/{limit}")

@dp.message(F.text)
async def handle_text(m: Message):
    if m.text.startswith("/"): return
    used = get_daily_usage(m.from_user.id); limit = get_daily_limit(m.from_user.id)
    if used >= limit: return await m.answer("⛔ Ліміт вичерпано")
    msg = await m.answer("⏳ Аналізую...")
    try:
        res = await run_search(m.text)
        increment_daily_usage(m.from_user.id)
        save_search(m.from_user.id, res["query_type"], m.text, res["normalized"], res)
        await msg.edit_text(render_result(res))
    except Exception as e: await msg.edit_text(f"❌ Помилка: {escape(str(e))}")

async def main():
    init_db()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
