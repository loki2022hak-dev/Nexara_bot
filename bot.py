import os
import re
import json
import html
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

FREE_DAILY_LIMIT = int(os.getenv("FREE_DAILY_LIMIT", "10"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(message)s"
)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

LAST_PDF = {}
WAITING_FOR_QUERY = {}

def now_utc():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

def esc(x):
    return html.escape(str(x or ""))

def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

def init_db():
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

    c.commit()
    c.close()

def upsert_user(user_id: int, username: str | None, first_name: str | None):
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
        SELECT id, query, query_type, pdf_path, created_at
        FROM searches
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT ?
    """, (user_id, limit))
    rows = cur.fetchall()
    c.close()
    return rows

def get_last_search(user_id: int):
    c = conn()
    cur = c.cursor()
    cur.execute("""
        SELECT id, query, query_type, result_json, pdf_path, created_at
        FROM searches
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT 1
    """, (user_id,))
    row = cur.fetchone()
    c.close()
    return row

def save_search(user_id: int, query: str, query_type: str, result: dict, pdf_path: str | None):
    c = conn()
    cur = c.cursor()
    cur.execute("""
        INSERT INTO searches (user_id, query, query_type, result_json, pdf_path, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        user_id,
        query,
        query_type,
        json.dumps(result, ensure_ascii=False),
        pdf_path,
        now_utc()
    ))
    c.commit()
    c.close()

def day_key():
    return datetime.utcnow().strftime("%Y-%m-%d")

def daily_used(user_id: int) -> int:
    c = conn()
    cur = c.cursor()
    cur.execute("SELECT count FROM daily_usage WHERE user_id = ? AND day_key = ?", (user_id, day_key()))
    row = cur.fetchone()
    c.close()
    return int(row["count"]) if row else 0

def bump_daily(user_id: int):
    c = conn()
    cur = c.cursor()
    cur.execute("SELECT count FROM daily_usage WHERE user_id = ? AND day_key = ?", (user_id, day_key()))
    row = cur.fetchone()
    if row:
        cur.execute("UPDATE daily_usage SET count = count + 1 WHERE user_id = ? AND day_key = ?", (user_id, day_key()))
    else:
        cur.execute("INSERT INTO daily_usage (user_id, day_key, count) VALUES (?, ?, 1)", (user_id, day_key()))
    c.commit()
    c.close()

def menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔎 Новий пошук"), KeyboardButton(text="📁 Мої результати")],
            [KeyboardButton(text="💎 VIP / Тарифи"), KeyboardButton(text="👤 Профіль")],
            [KeyboardButton(text="📄 PDF досьє"), KeyboardButton(text="🆘 Підтримка")],
        ],
        resize_keyboard=True
    )

def detect_type(text: str) -> str:
    q = text.strip()
    ql = q.lower()

    if ql.startswith("http://") or ql.startswith("https://"):
        return "url"

    try:
        ipaddress.ip_address(q)
        return "ip"
    except Exception:
        pass

    if re.fullmatch(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", q):
        return "email"

    if re.fullmatch(r"(?!-)(?:[a-zA-Z0-9-]{1,63}\.)+[A-Za-z]{2,63}", q):
        return "domain"

    if re.fullmatch(r"@?[A-Za-z0-9_][A-Za-z0-9_.-]{2,40}", q):
        return "username"

    return "text"

async def fetch_json(session, url, headers=None, timeout=15):
    try:
        async with session.get(url, headers=headers or {}, timeout=timeout) as r:
            txt = await r.text()
            ct = r.headers.get("content-type", "")
            if "json" in ct:
                return True, json.loads(txt)
            return True, {"raw": txt[:3000], "status": r.status}
    except Exception as e:
        return False, {"error": str(e)}

async def dns_lookup(session, domain, rtype):
    ok, data = await fetch_json(
        session,
        f"https://cloudflare-dns.com/dns-query?name={domain}&type={rtype}",
        headers={"accept": "application/dns-json"}
    )
    return {"ok": ok, "type": rtype, "data": data}

async def rdap_domain(session, domain):
    ok, data = await fetch_json(session, f"https://rdap.org/domain/{domain}")
    return {"ok": ok, "data": data}

async def rdap_ip(session, ip):
    ok, data = await fetch_json(session, f"https://rdap.org/ip/{ip}")
    return {"ok": ok, "data": data}

async def domain_osint(domain: str) -> dict:
    async with aiohttp.ClientSession() as session:
        a, mx, ns, txt, rdap = await asyncio.gather(
            dns_lookup(session, domain, "A"),
            dns_lookup(session, domain, "MX"),
            dns_lookup(session, domain, "NS"),
            dns_lookup(session, domain, "TXT"),
            rdap_domain(session, domain),
        )
    return {
        "type": "domain",
        "query": domain,
        "generated_at": now_utc(),
        "dns": {"A": a, "MX": mx, "NS": ns, "TXT": txt},
        "rdap": rdap,
    }

async def ip_osint(ip: str) -> dict:
    async with aiohttp.ClientSession() as session:
        rdap = await rdap_ip(session, ip)
    return {
        "type": "ip",
        "query": ip,
        "generated_at": now_utc(),
        "rdap": rdap,
    }

async def url_osint(url: str) -> dict:
    p = urlparse(url)
    domain = p.netloc.lower()
    embedded = await domain_osint(domain) if domain else {}
    return {
        "type": "url",
        "query": url,
        "generated_at": now_utc(),
        "parsed": {
            "scheme": p.scheme,
            "domain": domain,
            "path": p.path or "/",
            "query": p.query,
        },
        "domain_lookup": embedded,
    }

async def email_osint(email: str) -> dict:
    local, _, domain = email.partition("@")
    embedded = await domain_osint(domain) if domain else {}
    return {
        "type": "email",
        "query": email,
        "generated_at": now_utc(),
        "local_part": local,
        "domain_part": domain,
        "domain_lookup": embedded,
    }

def which_bin(name: str):
    return shutil.which(name)

def run_cmd(args, timeout=60):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return {
            "ok": p.returncode == 0,
            "returncode": p.returncode,
            "stdout": p.stdout[:80000],
            "stderr": p.stderr[:20000],
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "timeout": True, "stdout": "", "stderr": "timeout"}
    except Exception as e:
        return {"ok": False, "error": str(e), "stdout": "", "stderr": ""}

def parse_sherlock_output(stdout: str):
    found = []
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("[+] "):
            found.append(line[4:].strip())
        elif line.startswith("http://") or line.startswith("https://"):
            found.append(line)
    return found

def parse_maigret_output(stdout: str):
    found = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("[+] "):
            found.append(line[4:].strip())
        elif line.startswith("http://") or line.startswith("https://"):
            found.append(line)
        elif "http" in line and ("found" in line.lower() or "available" in line.lower()):
            found.append(line[:300])
    return found

async def run_maigret(target: str) -> dict:
    binpath = which_bin("maigret")
    if not binpath:
        return {"installed": False, "ok": False, "parsed": [], "stderr": "maigret not installed"}
    result = await asyncio.to_thread(
        run_cmd,
        [binpath, target, "--timeout", "8", "--top-sites", "20", "--no-color"],
        45
    )
    result["installed"] = True
    result["parsed"] = parse_maigret_output(result.get("stdout", ""))
    return result

async def run_sherlock(target: str) -> dict:
    binpath = which_bin("sherlock")
    if not binpath:
        return {"installed": False, "ok": False, "parsed": [], "stderr": "sherlock not installed"}
    result = await asyncio.to_thread(
        run_cmd,
        [binpath, target, "--timeout", "6", "--print-found"],
        35
    )
    result["installed"] = True
    result["parsed"] = parse_sherlock_output(result.get("stdout", ""))
    return result

async def username_osint(username: str) -> dict:
    username = username.lstrip("@")
    maigret_result = await run_maigret(username)
    sherlock_result = await run_sherlock(username)
    return {
        "type": "username",
        "query": username,
        "generated_at": now_utc(),
        "maigret": maigret_result,
        "sherlock": sherlock_result,
    }

def build_summary(result: dict) -> list[str]:
    t = result.get("type")
    lines = []

    if t == "username":
        m = result.get("maigret", {})
        s = result.get("sherlock", {})
        lines.append(f"Maigret: {'installed' if m.get('installed') else 'missing'}")
        lines.append(f"Sherlock: {'installed' if s.get('installed') else 'missing'}")
        lines.append(f"Maigret hits: {len(m.get('parsed', []))}")
        lines.append(f"Sherlock hits: {len(s.get('parsed', []))}")
        if m.get("timeout") or s.get("timeout"):
            lines.append("Один із модулів пішов у timeout")
        if not m.get("parsed") and not s.get("parsed"):
            lines.append("Хітів не знайдено")

    elif t == "domain":
        dns = result.get("dns", {})
        for rtype in ["A", "MX", "NS", "TXT"]:
            block = dns.get(rtype, {})
            answers = (block.get("data") or {}).get("Answer", []) if isinstance(block.get("data"), dict) else []
            lines.append(f"{rtype}: {len(answers)} records")

    elif t == "ip":
        rdap = result.get("rdap", {}).get("data", {})
        if isinstance(rdap, dict):
            lines.append(f"Owner: {rdap.get('name', 'unknown')}")
            lines.append(f"Handle: {rdap.get('handle', 'unknown')}")

    elif t == "url":
        parsed = result.get("parsed", {})
        lines.append(f"Scheme: {parsed.get('scheme')}")
        lines.append(f"Domain: {parsed.get('domain')}")
        lines.append(f"Path: {parsed.get('path')}")

    elif t == "email":
        lines.append(f"Local-part: {result.get('local_part')}")
        lines.append(f"Domain-part: {result.get('domain_part')}")

    else:
        lines.append("Структурований OSINT для цього тексту не визначено")

    return lines

def render_result(result: dict) -> str:
    t = result.get("type")
    summary = build_summary(result)

    parts = [
        "<b>NEXARA: Глибокий пошук</b>",
        "",
        f"<b>Запит:</b> <code>{esc(result.get('query'))}</code>",
        f"<b>Тип:</b> {esc(t)}",
        f"<b>Час:</b> {esc(result.get('generated_at'))}",
        "",
        "<b>Summary:</b>",
    ]
    for line in summary:
        parts.append(f"• {esc(line)}")

    if t == "username":
        m = result.get("maigret", {})
        s = result.get("sherlock", {})

        if m.get("parsed"):
            parts.append("")
            parts.append("<b>Maigret:</b>")
            for x in m["parsed"][:12]:
                parts.append(f"• {esc(x)}")

        if s.get("parsed"):
            parts.append("")
            parts.append("<b>Sherlock:</b>")
            for x in s["parsed"][:12]:
                parts.append(f"• {esc(x)}")

        if not m.get("parsed") and not s.get("parsed"):
            raw = []
            if m.get("stderr"):
                raw.append("Maigret stderr: " + m["stderr"][:400])
            if s.get("stderr"):
                raw.append("Sherlock stderr: " + s["stderr"][:400])
            if raw:
                parts.append("")
                parts.append("<b>Технічні деталі:</b>")
                for x in raw:
                    parts.append(f"• {esc(x)}")

    return "\n".join(parts)

def make_pdf(result: dict) -> str:
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    safe_query = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(result.get("query", "query")))[:50]
    pdf_path = DOSSIER_DIR / f"dossier_{safe_query}_{ts}.pdf"

    c = canvas.Canvas(str(pdf_path), pagesize=A4)
    w, h = A4
    y = h - 40

    def line(text, step=15):
        nonlocal y
        if y < 60:
            c.showPage()
            y = h - 40
        c.drawString(40, y, text[:120])
        y -= step

    c.setFont("Helvetica-Bold", 14)
    line("NEXARA DOSSIER", 20)
    c.setFont("Helvetica", 10)
    line(f"Query: {result.get('query')}")
    line(f"Type: {result.get('type')}")
    line(f"Generated: {result.get('generated_at')}")
    line("")

    for item in build_summary(result):
        line(f"- {item}")

    if result.get("type") == "username":
        line("")
        line("Maigret results:")
        for x in result.get("maigret", {}).get("parsed", [])[:20]:
            line(f"- {x}", 13)
        line("")
        line("Sherlock results:")
        for x in result.get("sherlock", {}).get("parsed", [])[:20]:
            line(f"- {x}", 13)

    c.save()
    return str(pdf_path)

async def full_osint(query: str) -> dict:
    qtype = detect_type(query)
    if qtype == "username":
        return await username_osint(query)
    if qtype == "domain":
        return await domain_osint(query.lower())
    if qtype == "ip":
        return await ip_osint(query)
    if qtype == "url":
        return await url_osint(query)
    if qtype == "email":
        return await email_osint(query.lower())
    return {
        "type": "text",
        "query": query,
        "generated_at": now_utc(),
        "raw_text": query,
    }

@dp.message(CommandStart())
async def start_cmd(message: Message):
    upsert_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    WAITING_FOR_QUERY[message.from_user.id] = True
    await message.answer(
        "<b>NEXARA</b>\n"
        "Використано: 0\n\n"
        "Введіть ПІБ, Нікнейм або IP:",
        reply_markup=menu()
    )

@dp.message(Command("profile"))
async def profile_cmd(message: Message):
    used = daily_used(message.from_user.id)
    total = total_searches(message.from_user.id)
    await message.answer(
        f"<b>Профіль</b>\n\n"
        f"<b>ID:</b> <code>{message.from_user.id}</code>\n"
        f"<b>Username:</b> @{esc(message.from_user.username or 'none')}\n"
        f"<b>Використано сьогодні:</b> {used}/{FREE_DAILY_LIMIT}\n"
        f"<b>Всього пошуків:</b> {total}",
        reply_markup=menu()
    )

@dp.message(F.text == "👤 Профіль")
async def profile_btn(message: Message):
    await profile_cmd(message)

@dp.message(F.text == "🔎 Новий пошук")
async def new_search_btn(message: Message):
    WAITING_FOR_QUERY[message.from_user.id] = True
    await message.answer("Введіть ПІБ, Нікнейм або IP:", reply_markup=menu())

@dp.message(F.text == "📁 Мої результати")
async def results_btn(message: Message):
    rows = get_history(message.from_user.id, 10)
    if not rows:
        await message.answer("Немає даних.", reply_markup=menu())
        return
    out = ["<b>Мої результати</b>", ""]
    for r in rows:
        out.append(f"• <code>{esc(r['query'])}</code> [{esc(r['query_type'])}] — {esc(r['created_at'])}")
    await message.answer("\n".join(out), reply_markup=menu())

@dp.message(F.text == "📄 PDF досьє")
async def pdf_btn(message: Message):
    pdf_path = LAST_PDF.get(message.from_user.id)
    if pdf_path and os.path.exists(pdf_path):
        await message.answer_document(FSInputFile(pdf_path), caption="PDF досьє")
        return

    row = get_last_search(message.from_user.id)
    if row and row["pdf_path"] and os.path.exists(row["pdf_path"]):
        await message.answer_document(FSInputFile(row["pdf_path"]), caption="PDF досьє")
        return

    await message.answer("Немає PDF досьє.", reply_markup=menu())

@dp.message(F.text == "💎 VIP / Тарифи")
async def vip_btn(message: Message):
    await message.answer(
        "<b>VIP / Тарифи</b>\n\nFREE\nINTEL\nAGENCY\nWARROOM",
        reply_markup=menu()
    )

@dp.message(F.text == "🆘 Підтримка")
async def support_btn(message: Message):
    await message.answer(
        "Підтримка активна. Надішли проблему одним повідомленням.",
        reply_markup=menu()
    )

@dp.message(F.text)
async def universal_handler(message: Message):
    text = (message.text or "").strip()
    if not text:
        return

    if text.startswith("/") and text not in ["/start", "/profile"]:
        await message.answer("Невідома команда.", reply_markup=menu())
        return

    if text in {"🔎 Новий пошук", "📁 Мої результати", "📄 PDF досьє", "👤 Профіль", "💎 VIP / Тарифи", "🆘 Підтримка"}:
        return

    upsert_user(message.from_user.id, message.from_user.username, message.from_user.first_name)

    used = daily_used(message.from_user.id)
    if used >= FREE_DAILY_LIMIT:
        await message.answer(f"Ліміт вичерпано: {used}/{FREE_DAILY_LIMIT}", reply_markup=menu())
        return

    wait = await message.answer("NEXARA: Глибокий пошук...", reply_markup=menu())
    try:
        result = await full_osint(text)
        pdf_path = make_pdf(result)
        LAST_PDF[message.from_user.id] = pdf_path
        save_search(message.from_user.id, text, result.get("type", "text"), result, pdf_path)
        bump_daily(message.from_user.id)

        await wait.edit_text(render_result(result), reply_markup=menu())

        if pdf_path and os.path.exists(pdf_path):
            await message.answer_document(FSInputFile(pdf_path), caption="PDF досьє")

    except Exception as e:
        logging.exception("search failed")
        await wait.edit_text(f"Помилка: <code>{esc(str(e))}</code>", reply_markup=menu())

async def main():
    init_db()
    me = await bot.get_me()
    logging.info("Start polling")
    logging.info("Authorized as @%s", me.username)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
