import os
import re
import json
import html
import sqlite3
import asyncio
import logging
import ipaddress
import subprocess
import urllib.parse
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse

import aiohttp
import phonenumbers
from phonenumbers import geocoder, carrier, timezone, PhoneNumberType, number_type, is_possible_number, is_valid_number

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

OWNER_ID = int(os.getenv("OWNER_ID", "0"))
FREE_DAILY_LIMIT = int(os.getenv("FREE_DAILY_LIMIT", "10"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

SHODAN_API_KEY = os.getenv("SHODAN_API_KEY", "").strip()
HIBP_API_KEY = os.getenv("HIBP_API_KEY", "").strip()

DB_PATH = "nexara_bot.db"
DOSSIER_DIR = Path("dossiers")
DOSSIER_DIR.mkdir(exist_ok=True)

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

def reset_all_daily_limits():
    c = conn()
    cur = c.cursor()
    cur.execute("DELETE FROM daily_usage")
    c.commit()
    c.close()

def owner_stats():
    c = conn()
    cur = c.cursor()
    cur.execute("SELECT COUNT(*) AS cnt FROM users")
    users = int(cur.fetchone()["cnt"])
    cur.execute("SELECT COUNT(*) AS cnt FROM searches")
    searches = int(cur.fetchone()["cnt"])
    cur.execute("SELECT COUNT(*) AS cnt FROM searches WHERE query_type = 'username'")
    usernames = int(cur.fetchone()["cnt"])
    cur.execute("SELECT COUNT(*) AS cnt FROM searches WHERE query_type = 'domain'")
    domains = int(cur.fetchone()["cnt"])
    cur.execute("SELECT COUNT(*) AS cnt FROM searches WHERE query_type = 'ip'")
    ips = int(cur.fetchone()["cnt"])
    cur.execute("SELECT COUNT(*) AS cnt FROM searches WHERE query_type = 'email'")
    emails = int(cur.fetchone()["cnt"])
    cur.execute("SELECT COUNT(*) AS cnt FROM searches WHERE query_type = 'phone'")
    phones = int(cur.fetchone()["cnt"])
    cur.execute("SELECT COUNT(*) AS cnt FROM searches WHERE created_at LIKE ?", (datetime.utcnow().strftime("%Y-%m-%d") + "%",))
    today = int(cur.fetchone()["cnt"])
    c.close()
    return {
        "users": users,
        "searches": searches,
        "today": today,
        "username": usernames,
        "domain": domains,
        "ip": ips,
        "email": emails,
        "phone": phones,
    }

def secrets_status():
    return {
        "BOT_TOKEN": bool(BOT_TOKEN),
        "OWNER_ID": bool(OWNER_ID),
        "SHODAN_API_KEY": bool(SHODAN_API_KEY),
        "HIBP_API_KEY": bool(HIBP_API_KEY),
    }

def owner_row():
    return [KeyboardButton(text="🛡 Адмін")] if OWNER_ID else []

def menu(user_id: int | None = None):
    keyboard = [
        [KeyboardButton(text="🔎 Новий пошук"), KeyboardButton(text="📁 Мої результати")],
        [KeyboardButton(text="💎 VIP / Тарифи"), KeyboardButton(text="👤 Профіль")],
        [KeyboardButton(text="📄 PDF досьє"), KeyboardButton(text="🆘 Підтримка")],
    ]
    if user_id == OWNER_ID and OWNER_ID:
        keyboard.append([KeyboardButton(text="🛡 Адмін")])
    return ReplyKeyboardMarkup(
        keyboard=keyboard,
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

    try:
        pn = phonenumbers.parse(q, None if q.startswith("+") else "UA")
        if is_possible_number(pn):
            return "phone"
    except Exception:
        pass

    if re.fullmatch(r"(?!-)(?:[a-zA-Z0-9-]{1,63}\.)+[A-Za-z]{2,63}", q):
        return "domain"

    if re.fullmatch(r"@?[A-Za-z0-9_][A-Za-z0-9_.-]{2,40}", q):
        return "username"

    return "text"

async def fetch_json(session, url, headers=None, timeout=20):
    try:
        async with session.get(url, headers=headers or {}, timeout=timeout) as r:
            txt = await r.text()
            ct = r.headers.get("content-type", "")
            if "json" in ct:
                return True, json.loads(txt), r.status
            return True, {"raw": txt[:4000]}, r.status
    except Exception as e:
        return False, {"error": str(e)}, 0

async def dns_lookup(session, domain, rtype):
    ok, data, status = await fetch_json(
        session,
        f"https://cloudflare-dns.com/dns-query?name={domain}&type={rtype}",
        headers={"accept": "application/dns-json"}
    )
    return {"ok": ok, "status": status, "type": rtype, "data": data}

async def rdap_domain(session, domain):
    ok, data, status = await fetch_json(session, f"https://rdap.org/domain/{domain}")
    return {"ok": ok, "status": status, "data": data}

async def rdap_ip(session, ip):
    ok, data, status = await fetch_json(session, f"https://rdap.org/ip/{ip}")
    return {"ok": ok, "status": status, "data": data}

async def shodan_host(session, ip):
    if not SHODAN_API_KEY:
        return {"enabled": False, "ok": False, "error": "SHODAN_API_KEY not set"}
    ok, data, status = await fetch_json(
        session,
        f"https://api.shodan.io/shodan/host/{ip}?key={urllib.parse.quote(SHODAN_API_KEY)}&minify=true"
    )
    return {"enabled": True, "ok": ok and status == 200, "status": status, "data": data}

async def hibp_breached_account(session, email):
    if not HIBP_API_KEY:
        return {"enabled": False, "ok": False, "error": "HIBP_API_KEY not set"}
    encoded = urllib.parse.quote(email.strip().lower(), safe="")
    headers = {
        "hibp-api-key": HIBP_API_KEY,
        "user-agent": "NEXARA-OSINT/1.0"
    }
    ok, data, status = await fetch_json(
        session,
        f"https://haveibeenpwned.com/api/v3/breachedaccount/{encoded}",
        headers=headers,
        timeout=25
    )
    return {"enabled": True, "ok": ok and status in (200, 404), "status": status, "data": data}

def extract_a_records(dns_block):
    data = (dns_block or {}).get("data", {})
    if not isinstance(data, dict):
        return []
    answers = data.get("Answer", []) or []
    vals = []
    for a in answers:
        val = a.get("data")
        if val:
            vals.append(val)
    return vals

async def domain_osint(domain: str) -> dict:
    async with aiohttp.ClientSession() as session:
        a, mx, ns, txt, rdap = await asyncio.gather(
            dns_lookup(session, domain, "A"),
            dns_lookup(session, domain, "MX"),
            dns_lookup(session, domain, "NS"),
            dns_lookup(session, domain, "TXT"),
            rdap_domain(session, domain),
        )
        shodan_hits = []
        for ip in extract_a_records(a)[:3]:
            shodan_hits.append(await shodan_host(session, ip))
    return {
        "type": "domain",
        "query": domain,
        "generated_at": now_utc(),
        "dns": {"A": a, "MX": mx, "NS": ns, "TXT": txt},
        "rdap": rdap,
        "shodan_hosts": shodan_hits,
    }

async def ip_osint(ip: str) -> dict:
    async with aiohttp.ClientSession() as session:
        rdap, shodan = await asyncio.gather(
            rdap_ip(session, ip),
            shodan_host(session, ip)
        )
    return {
        "type": "ip",
        "query": ip,
        "generated_at": now_utc(),
        "rdap": rdap,
        "shodan": shodan,
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
    async with aiohttp.ClientSession() as session:
        hibp = await hibp_breached_account(session, email)
    embedded = await domain_osint(domain) if domain else {}
    return {
        "type": "email",
        "query": email,
        "generated_at": now_utc(),
        "local_part": local,
        "domain_part": domain,
        "hibp": hibp,
        "domain_lookup": embedded,
    }

def phone_type_name(t):
    mapping = {
        PhoneNumberType.MOBILE: "mobile",
        PhoneNumberType.FIXED_LINE: "fixed_line",
        PhoneNumberType.FIXED_LINE_OR_MOBILE: "fixed_or_mobile",
        PhoneNumberType.TOLL_FREE: "toll_free",
        PhoneNumberType.PREMIUM_RATE: "premium_rate",
        PhoneNumberType.SHARED_COST: "shared_cost",
        PhoneNumberType.VOIP: "voip",
        PhoneNumberType.PERSONAL_NUMBER: "personal_number",
        PhoneNumberType.PAGER: "pager",
        PhoneNumberType.UAN: "uan",
        PhoneNumberType.VOICEMAIL: "voicemail",
        PhoneNumberType.UNKNOWN: "unknown",
    }
    return mapping.get(t, str(t))

async def phone_osint(phone_text: str) -> dict:
    pn = phonenumbers.parse(phone_text, None if phone_text.startswith("+") else "UA")
    e164 = phonenumbers.format_number(pn, phonenumbers.PhoneNumberFormat.E164)
    international = phonenumbers.format_number(pn, phonenumbers.PhoneNumberFormat.INTERNATIONAL)
    national = phonenumbers.format_number(pn, phonenumbers.PhoneNumberFormat.NATIONAL)

    result = {
        "type": "phone",
        "query": phone_text,
        "generated_at": now_utc(),
        "e164": e164,
        "international": international,
        "national": national,
        "valid": is_valid_number(pn),
        "possible": is_possible_number(pn),
        "region_code": phonenumbers.region_code_for_number(pn),
        "country_code": pn.country_code,
        "location": geocoder.description_for_number(pn, "en"),
        "carrier": carrier.name_for_number(pn, "en"),
        "timezones": list(timezone.time_zones_for_number(pn)),
        "number_type": phone_type_name(number_type(pn)),
    }
    return result

def which_bin(name: str):
    return shutil.which(name)

def run_cmd(args, timeout=45):
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
    sherlock_result = await run_sherlock(username)
    return {
        "type": "username",
        "query": username,
        "generated_at": now_utc(),
        "sherlock": sherlock_result,
    }

def build_summary(result: dict) -> list[str]:
    t = result.get("type")
    lines = []

    if t == "username":
        s = result.get("sherlock", {})
        lines.append(f"Sherlock: {'installed' if s.get('installed') else 'missing'}")
        lines.append(f"Sherlock hits: {len(s.get('parsed', []))}")
        if s.get("timeout"):
            lines.append("Sherlock timeout")
        if not s.get("parsed"):
            lines.append("Хітів не знайдено")

    elif t == "domain":
        dns = result.get("dns", {})
        for rtype in ["A", "MX", "NS", "TXT"]:
            block = dns.get(rtype, {})
            answers = (block.get("data") or {}).get("Answer", []) if isinstance(block.get("data"), dict) else []
            lines.append(f"{rtype}: {len(answers)} records")
        sh = result.get("shodan_hosts", [])
        if sh:
            lines.append(f"Shodan host checks: {len(sh)}")

    elif t == "ip":
        rdap = result.get("rdap", {}).get("data", {})
        if isinstance(rdap, dict):
            lines.append(f"Owner: {rdap.get('name', 'unknown')}")
            lines.append(f"Handle: {rdap.get('handle', 'unknown')}")
        sh = result.get("shodan", {})
        if sh.get("enabled"):
            lines.append(f"Shodan status: {sh.get('status')}")

    elif t == "url":
        parsed = result.get("parsed", {})
        lines.append(f"Scheme: {parsed.get('scheme')}")
        lines.append(f"Domain: {parsed.get('domain')}")
        lines.append(f"Path: {parsed.get('path')}")

    elif t == "email":
        lines.append(f"Local-part: {result.get('local_part')}")
        lines.append(f"Domain-part: {result.get('domain_part')}")
        hibp = result.get("hibp", {})
        if hibp.get("enabled"):
            lines.append(f"HIBP status: {hibp.get('status')}")

    elif t == "phone":
        lines.append(f"E164: {result.get('e164')}")
        lines.append(f"Region: {result.get('region_code')}")
        lines.append(f"Carrier: {result.get('carrier') or 'unknown'}")
        lines.append(f"Type: {result.get('number_type')}")
        lines.append(f"Valid: {result.get('valid')}")

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
        s = result.get("sherlock", {})
        if s.get("parsed"):
            parts.append("")
            parts.append("<b>Sherlock:</b>")
            for x in s["parsed"][:15]:
                parts.append(f"• {esc(x)}")
        elif s.get("stderr"):
            parts.append("")
            parts.append("<b>Технічні деталі:</b>")
            parts.append(f"• {esc(s['stderr'][:600])}")

    elif t == "ip":
        sh = result.get("shodan", {})
        data = sh.get("data", {}) if isinstance(sh, dict) else {}
        if isinstance(data, dict) and data:
            parts.append("")
            parts.append("<b>Shodan:</b>")
            for key in ["ip_str", "org", "isp", "os"]:
                if data.get(key):
                    parts.append(f"• {esc(key)}: {esc(data.get(key))}")
            ports = data.get("ports", [])
            if ports:
                parts.append(f"• ports: {esc(', '.join(map(str, ports[:20])))}")

    elif t == "email":
        hibp = result.get("hibp", {})
        data = hibp.get("data", [])
        if isinstance(data, list) and data:
            parts.append("")
            parts.append("<b>Email leaks / breaches:</b>")
            for item in data[:15]:
                name = item.get("Name") or item.get("Title") or "unknown"
                domain = item.get("Domain") or "n/a"
                breach_date = item.get("BreachDate") or "n/a"
                parts.append(f"• {esc(name)} | {esc(domain)} | {esc(breach_date)}")
        elif hibp.get("enabled") and hibp.get("status") == 404:
            parts.append("")
            parts.append("• HIBP: no breaches found")

    elif t == "phone":
        parts.append("")
        parts.append("<b>Phone OSINT:</b>")
        for key in ["international", "national", "location", "carrier", "number_type"]:
            val = result.get(key)
            if val:
                parts.append(f"• {esc(key)}: {esc(val)}")
        tz = result.get("timezones") or []
        if tz:
            parts.append(f"• timezones: {esc(', '.join(tz))}")

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
        line("Sherlock results:")
        for x in result.get("sherlock", {}).get("parsed", [])[:25]:
            line(f"- {x}", 13)

    if result.get("type") == "email":
        line("")
        line("Email leaks:")
        hibp = result.get("hibp", {}).get("data", [])
        if isinstance(hibp, list):
            for item in hibp[:25]:
                line(f"- {(item.get('Name') or 'unknown')} | {(item.get('Domain') or 'n/a')}", 13)

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
    if qtype == "phone":
        return await phone_osint(query)
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
    used = "∞" if message.from_user.id == OWNER_ID else str(daily_used(message.from_user.id))
    await message.answer(
        "<b>NEXARA</b>\n"
        f"Використано: {used}\n\n"
        "Введіть ПІБ, Нікнейм, Email, Телефон, Domain, URL або IP:",
        reply_markup=menu(message.from_user.id)
    )

@dp.message(Command("profile"))
async def profile_cmd(message: Message):
    used = daily_used(message.from_user.id)
    total = total_searches(message.from_user.id)
    used_str = "∞" if message.from_user.id == OWNER_ID else f"{used}/{FREE_DAILY_LIMIT}"
    await message.answer(
        f"<b>Профіль</b>\n\n"
        f"<b>ID:</b> <code>{message.from_user.id}</code>\n"
        f"<b>Username:</b> @{esc(message.from_user.username or 'none')}\n"
        f"<b>Використано сьогодні:</b> {used_str}\n"
        f"<b>Всього пошуків:</b> {total}",
        reply_markup=menu(message.from_user.id)
    )

@dp.message(F.text == "👤 Профіль")
async def profile_btn(message: Message):
    await profile_cmd(message)

@dp.message(F.text == "🔎 Новий пошук")
async def new_search_btn(message: Message):
    WAITING_FOR_QUERY[message.from_user.id] = True
    await message.answer("Введіть ПІБ, Нікнейм, Email, Телефон, Domain, URL або IP:", reply_markup=menu(message.from_user.id))

@dp.message(F.text == "📁 Мої результати")
async def results_btn(message: Message):
    rows = get_history(message.from_user.id, 10)
    if not rows:
        await message.answer("Немає даних.", reply_markup=menu(message.from_user.id))
        return
    out = ["<b>Мої результати</b>", ""]
    for r in rows:
        out.append(f"• <code>{esc(r['query'])}</code> [{esc(r['query_type'])}] — {esc(r['created_at'])}")
    await message.answer("\n".join(out), reply_markup=menu(message.from_user.id))

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

    await message.answer("Немає PDF досьє.", reply_markup=menu(message.from_user.id))

@dp.message(F.text == "💎 VIP / Тарифи")
async def vip_btn(message: Message):
    await message.answer(
        "<b>VIP / Тарифи</b>\n\nFREE\nINTEL\nAGENCY\nWARROOM",
        reply_markup=menu(message.from_user.id)
    )

@dp.message(F.text == "🆘 Підтримка")
async def support_btn(message: Message):
    await message.answer(
        "Підтримка активна. Надішли проблему одним повідомленням.",
        reply_markup=menu(message.from_user.id)
    )


@dp.message(F.text == "🛡 Адмін")
async def admin_btn(message: Message):
    if message.from_user.id != OWNER_ID:
        return
    await message.answer(
        "<b>Адмін-панель</b>\n\n"
        "Команди:\n"
        "• 📊 Статистика\n"
        "• ♻️ Скинути ліміти\n"
        "• 🧪 Health\n"
        "• 🔐 Secrets status",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="♻️ Скинути ліміти")],
                [KeyboardButton(text="🧪 Health"), KeyboardButton(text="🔐 Secrets status")],
                [KeyboardButton(text="🔎 Новий пошук")]
            ],
            resize_keyboard=True
        )
    )

@dp.message(F.text == "📊 Статистика")
async def admin_stats_btn(message: Message):
    if message.from_user.id != OWNER_ID:
        return
    st = owner_stats()
    await message.answer(
        "<b>Статистика</b>\n\n"
        f"Users: {st['users']}\n"
        f"Searches total: {st['searches']}\n"
        f"Searches today: {st['today']}\n"
        f"Username: {st['username']}\n"
        f"Domain: {st['domain']}\n"
        f"IP: {st['ip']}\n"
        f"Email: {st['email']}\n"
        f"Phone: {st['phone']}",
        reply_markup=menu(message.from_user.id)
    )

@dp.message(F.text == "♻️ Скинути ліміти")
async def admin_reset_limits_btn(message: Message):
    if message.from_user.id != OWNER_ID:
        return
    reset_all_daily_limits()
    await message.answer("Ліміти скинуто.", reply_markup=menu(message.from_user.id))

@dp.message(F.text == "🧪 Health")
async def admin_health_btn(message: Message):
    if message.from_user.id != OWNER_ID:
        return
    await message.answer(
        "<b>Health</b>\n\n"
        f"Bot: online\n"
        f"DB: {esc(DB_PATH)}\n"
        f"Dossier dir exists: {DOSSIER_DIR.exists()}\n"
        f"Owner bypass: {'on' if OWNER_ID else 'off'}\n"
        f"Daily limit: {FREE_DAILY_LIMIT}",
        reply_markup=menu(message.from_user.id)
    )

@dp.message(F.text == "🔐 Secrets status")
async def admin_secrets_btn(message: Message):
    if message.from_user.id != OWNER_ID:
        return
    st = secrets_status()
    await message.answer(
        "<b>Secrets status</b>\n\n"
        f"BOT_TOKEN: {'set' if st['BOT_TOKEN'] else 'missing'}\n"
        f"OWNER_ID: {'set' if st['OWNER_ID'] else 'missing'}\n"
        f"SHODAN_API_KEY: {'set' if st['SHODAN_API_KEY'] else 'missing'}\n"
        f"HIBP_API_KEY: {'set' if st['HIBP_API_KEY'] else 'missing'}",
        reply_markup=menu(message.from_user.id)
    )

@dp.message(F.text)
async def universal_handler(message: Message):
    text = (message.text or "").strip()
    if not text:
        return

    if text.startswith("/") and text not in ["/start", "/profile"]:
        await message.answer("Невідома команда.", reply_markup=menu(message.from_user.id))
        return

    if text in {"🔎 Новий пошук", "📁 Мої результати", "📄 PDF досьє", "👤 Профіль", "💎 VIP / Тарифи", "🆘 Підтримка"}:
        return

    upsert_user(message.from_user.id, message.from_user.username, message.from_user.first_name)

    used = daily_used(message.from_user.id)
    if message.from_user.id != OWNER_ID and used >= FREE_DAILY_LIMIT:
        await message.answer(f"Ліміт вичерпано: {used}/{FREE_DAILY_LIMIT}", reply_markup=menu(message.from_user.id))
        return

    wait = await message.answer("NEXARA: Глибокий пошук...", reply_markup=menu(message.from_user.id))
    try:
        result = await full_osint(text)
        pdf_path = make_pdf(result)
        LAST_PDF[message.from_user.id] = pdf_path
        save_search(message.from_user.id, text, result.get("type", "text"), result, pdf_path)
        if message.from_user.id != OWNER_ID:
            bump_daily(message.from_user.id)

        await wait.edit_text(render_result(result), reply_markup=menu(message.from_user.id))

        if pdf_path and os.path.exists(pdf_path):
            await message.answer_document(FSInputFile(pdf_path), caption="PDF досьє")

    except Exception as e:
        logging.exception("search failed")
        await wait.edit_text(f"Помилка: <code>{esc(str(e))}</code>", reply_markup=menu(message.from_user.id))

async def main():
    init_db()
    me = await bot.get_me()
    logging.info("Start polling")
    logging.info("Authorized as @%s", me.username)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
