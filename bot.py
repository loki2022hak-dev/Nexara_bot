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

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN not set")

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
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS searches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        query_type TEXT NOT NULL,
        raw_query TEXT NOT NULL,
        normalized_query TEXT NOT NULL,
        result_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS usage_daily (
        user_id INTEGER NOT NULL,
        day_key TEXT NOT NULL,
        count INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (user_id, day_key)
    )
    """)

    c.commit()
    c.close()

def upsert_user(user_id: int, username: str | None, first_name: str | None, last_name: str | None) -> None:
    c = conn()
    cur = c.cursor()
    ts = now_ts()
    cur.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    exists = cur.fetchone()
    if exists:
        cur.execute("""
            UPDATE users
            SET username = ?, first_name = ?, last_name = ?, updated_at = ?
            WHERE user_id = ?
        """, (username, first_name, last_name, ts, user_id))
    else:
        cur.execute("""
            INSERT INTO users (user_id, username, first_name, last_name, tariff, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'FREE', ?, ?)
        """, (user_id, username, first_name, last_name, ts, ts))
    c.commit()
    c.close()

def get_user(user_id: int) -> sqlite3.Row | None:
    c = conn()
    cur = c.cursor()
    cur.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    c.close()
    return row

def save_search(user_id: int, query_type: str, raw_query: str, normalized_query: str, result: dict) -> None:
    c = conn()
    cur = c.cursor()
    cur.execute("""
        INSERT INTO searches (user_id, query_type, raw_query, normalized_query, result_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (user_id, query_type, raw_query, normalized_query, json.dumps(result, ensure_ascii=False), now_ts()))
    c.commit()
    c.close()
def get_recent_searches(user_id: int, limit: int = 10) -> list[sqlite3.Row]:
    c = conn()
    cur = c.cursor()
    cur.execute("""
        SELECT * FROM searches
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT ?
    """, (user_id, limit))
    rows = cur.fetchall()
    c.close()
    return rows

def get_total_searches(user_id: int) -> int:
    c = conn()
    cur = c.cursor()
    cur.execute("SELECT COUNT(*) AS cnt FROM searches WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    c.close()
    return int(row["cnt"]) if row else 0

def day_key() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d")

def get_daily_limit(user_id: int) -> int:
    row = get_user(user_id)
    tariff = (row["tariff"] if row else "FREE").upper()
    if tariff == "INTEL":
        return INTEL_DAILY_LIMIT
    if tariff == "AGENCY":
        return AGENCY_DAILY_LIMIT
    if tariff == "WARROOM":
        return WARROOM_DAILY_LIMIT
    return FREE_DAILY_LIMIT

def get_daily_usage(user_id: int) -> int:
    c = conn()
    cur = c.cursor()
    cur.execute("SELECT count FROM usage_daily WHERE user_id = ? AND day_key = ?", (user_id, day_key()))
    row = cur.fetchone()
    c.close()
    return int(row["count"]) if row else 0

def increment_daily_usage(user_id: int) -> None:
    c = conn()
    cur = c.cursor()
    cur.execute("SELECT count FROM usage_daily WHERE user_id = ? AND day_key = ?", (user_id, day_key()))
    row = cur.fetchone()
    if row:
        cur.execute("UPDATE usage_daily SET count = count + 1 WHERE user_id = ? AND day_key = ?", (user_id, day_key()))
    else:
        cur.execute("INSERT INTO usage_daily (user_id, day_key, count) VALUES (?, ?, 1)", (user_id, day_key()))
    c.commit()
    c.close()

def keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔍 Новий пошук"), KeyboardButton(text="📂 Історія")],
            [KeyboardButton(text="👤 Профіль"), KeyboardButton(text="💎 VIP / Тарифи")],
            [KeyboardButton(text="⚙️ Налаштування"), KeyboardButton(text="🆘 Підтримка")],
        ],
        resize_keyboard=True,
    )

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
DOMAIN_RE = re.compile(r"^(?!-)(?:[a-zA-Z0-9-]{1,63}\.)+[A-Za-z]{2,63}$")
HASH_RE = re.compile(r"^[A-Fa-f0-9]{32}$|^[A-Fa-f0-9]{40}$|^[A-Fa-f0-9]{64}$")
USERNAME_RE = re.compile(r"^@?[A-Za-z0-9_][A-Za-z0-9_.-]{2,31}$")

def detect_type(query: str) -> tuple[str, str]:
    q = query.strip()
    lower = q.lower()

    if lower.startswith(("http://", "https://")):
        return "url", q

    try:
        ipaddress.ip_address(q)
        return "ip", q
    except Exception:
        pass

    if EMAIL_RE.match(q):
        return "email", q.lower()

    if HASH_RE.match(q):
        return "hash", q.lower()

    if DOMAIN_RE.match(q.lower()):
        return "domain", q.lower()

    if USERNAME_RE.match(q) and " " not in q and not any(sym in q for sym in [":", "/"]):
        return "username", q.lstrip("@")

    if len(q.split()) >= 2 and all("@" not in part and "/" not in part for part in q.split()):
        return "person_or_company", q

    return "generic", q

async def fetch_json(session: aiohttp.ClientSession, url: str, headers: dict | None = None, timeout: int = 25) -> tuple[bool, dict]:
    try:
        async with session.get(url, headers=headers or {}, timeout=timeout) as resp:
            txt = await resp.text()
            if "application/json" in (resp.headers.get("content-type", "")):
                try:
                    return resp.status < 400, json.loads(txt)
                except Exception:
                    return False, {"error": "invalid_json", "status": resp.status, "body": txt[:1000]}
            return resp.status < 400, {"status": resp.status, "body": txt[:1000]}
    except Exception as e:
        return False, {"error": str(e)}
async def dns_lookup(session: aiohttp.ClientSession, domain: str, rtype: str) -> dict:
    ok, data = await fetch_json(session, f"https://cloudflare-dns.com/dns-query?name={quote(domain)}&type={quote(rtype)}", headers={"accept": "application/dns-json"})
    if not ok:
        return {"ok": False, "type": rtype, "error": data}
    return {"ok": True, "type": rtype, "answers": data.get("Answer", []), "status": data.get("Status")}

async def rdap_domain(session: aiohttp.ClientSession, domain: str) -> dict:
    ok, data = await fetch_json(session, f"https://rdap.org/domain/{quote(domain)}")
    return {"ok": ok, "data": data}

async def rdap_ip(session: aiohttp.ClientSession, ip: str) -> dict:
    ok, data = await fetch_json(session, f"https://rdap.org/ip/{quote(ip)}")
    return {"ok": ok, "data": data}

async def vt_domain(session: aiohttp.ClientSession, domain: str) -> dict:
    if not VT_API_KEY:
        return {"ok": False, "skipped": True}
    ok, data = await fetch_json(session, f"https://www.virustotal.com/api/v3/domains/{quote(domain)}", headers={"x-apikey": VT_API_KEY})
    return {"ok": ok, "data": data}

async def vt_ip(session: aiohttp.ClientSession, ip: str) -> dict:
    if not VT_API_KEY:
        return {"ok": False, "skipped": True}
    ok, data = await fetch_json(session, f"https://www.virustotal.com/api/v3/ip_addresses/{quote(ip)}", headers={"x-apikey": VT_API_KEY})
    return {"ok": ok, "data": data}

async def vt_file(session: aiohttp.ClientSession, file_hash: str) -> dict:
    if not VT_API_KEY:
        return {"ok": False, "skipped": True}
    ok, data = await fetch_json(session, f"https://www.virustotal.com/api/v3/files/{quote(file_hash)}", headers={"x-apikey": VT_API_KEY})
    return {"ok": ok, "data": data}

async def shodan_host(session: aiohttp.ClientSession, ip: str) -> dict:
    if not SHODAN_API_KEY:
        return {"ok": False, "skipped": True}
    ok, data = await fetch_json(session, f"https://api.shodan.io/shodan/host/{quote(ip)}?key={quote(SHODAN_API_KEY)}")
    return {"ok": ok, "data": data}

async def censys_host(session: aiohttp.ClientSession, ip: str) -> dict:
    if not CENSYS_BEARER_TOKEN:
        return {"ok": False, "skipped": True}
    ok, data = await fetch_json(session, f"https://search.censys.io/api/v2/hosts/{quote(ip)}", headers={"Authorization": f"Bearer {CENSYS_BEARER_TOKEN}"})
    return {"ok": ok, "data": data}

async def venice_summary(payload: dict) -> str | None:
    if not VENICE_API_KEY:
        return None
    headers = {
        "Authorization": f"Bearer {VENICE_API_KEY}",
        "Content-Type": "application/json",
    }
    body = {
        "model": "llama-3.1-70b",
        "messages": [
            {
                "role": "user",
                "content": "Зроби короткий аналітичний summary українською по JSON нижче. Без фантазій, тільки по даних.\n\n" + json.dumps(payload, ensure_ascii=False)[:12000]
            }
        ]
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post("https://api.venice.ai/api/v1/chat/completions", headers=headers, json=body, timeout=35) as resp:
                data = await resp.json()
                return data["choices"][0]["message"]["content"]
    except Exception:
        return None

def summarize_domain(result: dict) -> list[str]:
    lines = []
    a = result.get("dns", {}).get("A", {})
    mx = result.get("dns", {}).get("MX", {})
    ns = result.get("dns", {}).get("NS", {})
    rdap = result.get("rdap", {}).get("data", {})

    a_count = len(a.get("answers", [])) if a.get("ok") else 0
    mx_count = len(mx.get("answers", [])) if mx.get("ok") else 0
    ns_count = len(ns.get("answers", [])) if ns.get("ok") else 0

    lines.append(f"DNS A: {a_count}")
    lines.append(f"DNS MX: {mx_count}")
    lines.append(f"DNS NS: {ns_count}")

    if isinstance(rdap, dict):
        if rdap.get("handle"):
            lines.append(f"RDAP handle: {rdap.get('handle')}")
        if rdap.get("ldhName"):
            lines.append(f"RDAP name: {rdap.get('ldhName')}")
vt = result.get("virustotal", {}).get("data", {})
    attrs = (vt.get("data") or {}).get("attributes", {}) if isinstance(vt, dict) else {}
    stats = attrs.get("last_analysis_stats", {}) if isinstance(attrs, dict) else {}
    if stats:
        lines.append(f"VT malicious: {stats.get('malicious', 0)} | suspicious: {stats.get('suspicious', 0)}")

    return lines

def summarize_ip(result: dict) -> list[str]:
    lines = []
    rdap = result.get("rdap", {}).get("data", {})
    if isinstance(rdap, dict):
        if rdap.get("name"):
            lines.append(f"Owner: {rdap.get('name')}")
        if rdap.get("handle"):
            lines.append(f"Handle: {rdap.get('handle')}")

    sh = result.get("shodan", {}).get("data", {})
    if isinstance(sh, dict) and sh:
        ports = sh.get("ports", [])
        if ports:
            lines.append(f"Shodan ports: {', '.join(map(str, ports[:15]))}")

    vt = result.get("virustotal", {}).get("data", {})
    attrs = (vt.get("data") or {}).get("attributes", {}) if isinstance(vt, dict) else {}
    stats = attrs.get("last_analysis_stats", {}) if isinstance(attrs, dict) else {}
    if stats:
        lines.append(f"VT malicious: {stats.get('malicious', 0)} | suspicious: {stats.get('suspicious', 0)}")

    return lines

def summarize_email(result: dict) -> list[str]:
    lines = []
    lines.append(f"Syntax valid: {'yes' if result.get('syntax_ok') else 'no'}")
    domain_info = result.get("domain_lookup", {})
    mx = domain_info.get("dns", {}).get("MX", {})
    mx_count = len(mx.get("answers", [])) if mx.get("ok") else 0
    lines.append(f"MX records: {mx_count}")
    lines.append(f"Domain type: {domain_info.get('type', 'unknown')}")
    return lines

def summarize_hash(result: dict) -> list[str]:
    lines = []
    vt = result.get("virustotal", {}).get("data", {})
    attrs = (vt.get("data") or {}).get("attributes", {}) if isinstance(vt, dict) else {}
    stats = attrs.get("last_analysis_stats", {}) if isinstance(attrs, dict) else {}
    if stats:
        lines.append(f"VT malicious: {stats.get('malicious', 0)} | suspicious: {stats.get('suspicious', 0)}")
    if attrs.get("type_description"):
        lines.append(f"Type: {attrs.get('type_description')}")
    if attrs.get("size"):
        lines.append(f"Size: {attrs.get('size')}")
    if not lines:
        lines.append("No external file reputation data available.")
    return lines

async def run_search(raw_query: str) -> dict:
    qtype, normalized = detect_type(raw_query)
    base = {
        "raw_query": raw_query,
        "normalized_query": normalized,
        "query_type": qtype,
        "generated_at": now_ts(),
        "providers": {},
        "summary": [],
    }

    timeout = aiohttp.ClientTimeout(total=35)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        if qtype == "domain":
            dns_a, dns_mx, dns_ns, dns_txt, rdap_data, vt_data = await asyncio.gather(
                dns_lookup(session, normalized, "A"),
                dns_lookup(session, normalized, "MX"),
                dns_lookup(session, normalized, "NS"),
                dns_lookup(session, normalized, "TXT"),
                rdap_domain(session, normalized),
                vt_domain(session, normalized),
            )
            base["dns"] = {"A": dns_a, "MX": dns_mx, "NS": dns_ns, "TXT": dns_txt}
            base["rdap"] = rdap_data
            base["virustotal"] = vt_data
            base["summary"] = summarize_domain(base)

        elif qtype == "ip":
            rdap_data, shodan_data, vt_data, censys_data = await asyncio.gather(
                rdap_ip(session, normalized),
                shodan_host(session, normalized),
                vt_ip(session, normalized),
                censys_host(session, normalized),
            )
            base["rdap"] = rdap_data
            base["shodan"] = shodan_data
            base["virustotal"] = vt_data
            base["censys"] = censys_data
            base["summary"] = summarize_ip(base)
       elif qtype == "url":
            parsed = urlparse(normalized)
            domain = parsed.netloc.lower()
            path = parsed.path or "/"
            base["parsed"] = {
                "scheme": parsed.scheme,
                "domain": domain,
                "path": path,
                "query": parsed.query,
            }
            domain_result = await run_search(domain)
            base["domain_lookup"] = domain_result
            base["summary"] = [
                f"URL domain: {domain}",
                f"Path: {path}",
                "Domain lookup embedded below.",
            ]

        elif qtype == "email":
            local, domain = normalized.split("@", 1)
            domain_result = await run_search(domain)
            base["syntax_ok"] = True
            base["local_part"] = local
            base["domain_part"] = domain
            base["domain_lookup"] = domain_result
            base["summary"] = summarize_email(base)

        elif qtype == "hash":
            vt_data = await vt_file(session, normalized)
            base["virustotal"] = vt_data
            base["summary"] = summarize_hash(base)

        elif qtype == "username":
            base["summary"] = [
                "Username normalization completed.",
                "Local dossier created.",
                "External username-enrichment modules can be attached through API providers."
            ]

        elif qtype == "person_or_company":
            base["summary"] = [
                "Structured entity detected.",
                "Name/company stored in dossier history.",
                "External registry providers can be attached through API providers."
            ]

        else:
            base["summary"] = [
                "Generic text query stored.",
                "No typed indicator detected.",
                "Use domain, IP, URL, email, username, hash or entity name for stronger results."
            ]

    ai = await venice_summary(base)
    if ai:
        base["ai_summary"] = ai

    return base

def render_result(result: dict) -> str:
    q = escape(result["normalized_query"])
    qtype = escape(result["query_type"])
    generated = escape(result["generated_at"])
    summary = "\n".join([f"• {escape(x)}" for x in result.get("summary", [])])

    blocks = [
        f"<b>🧠 {APP_NAME} RESULT</b>",
        "",
        f"<b>Запит:</b> <code>{q}</code>",
        f"<b>Тип:</b> {qtype}",
        f"<b>Час:</b> {generated}",
        "",
        "<b>Summary:</b>",
        summary or "• No summary",
    ]

    if "ai_summary" in result:
        blocks.extend(["", "<b>AI Summary:</b>", escape(result["ai_summary"])])

    if result["query_type"] == "domain":
        dns = result.get("dns", {})
        for rtype in ["A", "MX", "NS", "TXT"]:
            section = dns.get(rtype, {})
            if section.get("ok"):
                answers = section.get("answers", [])
                if answers:
                    blocks.append("")
                    blocks.append(f"<b>DNS {rtype}:</b>")
                    for item in answers[:10]:
                        blocks.append(f"• {escape(item.get('data'))}")
        rdap = result.get("rdap", {}).get("data", {})
        if isinstance(rdap, dict) and rdap:
            blocks.append("")
            blocks.append("<b>RDAP:</b>")
            for key in ["ldhName", "handle", "status"]:
                val = rdap.get(key)
                if val:
                    blocks.append(f"• {escape(key)}: {escape(val)}")

    if result["query_type"] == "ip":
        sh = result.get("shodan", {}).get("data", {})
        if isinstance(sh, dict) and sh.get("ports"):
            blocks.append("")
            blocks.append("<b>Shodan ports:</b>")
            blocks.append("• " + escape(", ".join(map(str, sh.get("ports", [])[:20]))))
    if result["query_type"] == "email":
        domain_lookup = result.get("domain_lookup", {})
        blocks.append("")
        blocks.append("<b>Email domain summary:</b>")
        for line in domain_lookup.get("summary", [])[:6]:
            blocks.append(f"• {escape(line)}")

    if result["query_type"] == "url":
        parsed = result.get("parsed", {})
        blocks.append("")
        blocks.append("<b>Parsed URL:</b>")
        blocks.append(f"• scheme: {escape(parsed.get('scheme'))}")
        blocks.append(f"• domain: {escape(parsed.get('domain'))}")
        blocks.append(f"• path: {escape(parsed.get('path'))}")

    return "\n".join(blocks)

async def answer_or_edit(message: Message, text: str) -> None:
    await message.answer(text, reply_markup=keyboard())

@dp.message(CommandStart())
async def start_cmd(message: Message) -> None:
    upsert_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.first_name,
        message.from_user.last_name,
    )
    text = (
        f"<b>🚀 {APP_NAME} ONLINE</b>\n\n"
        "Меню готове.\n"
        "Пошук, профіль, історія і ліміти — активні.\n\n"
        "<b>Підтримувані типи запитів:</b>\n"
        "• IP\n"
        "• domain\n"
        "• URL\n"
        "• email\n"
        "• hash\n"
        "• username\n"
        "• ПІБ / company text\n"
    )
    await message.answer(text, reply_markup=keyboard())

@dp.message(Command("help"))
async def help_cmd(message: Message) -> None:
    text = (
        "<b>📘 Help</b>\n\n"
        "• /start — головне меню\n"
        "• /profile — профіль\n"
        "• /history — історія\n"
        "• /help — довідка\n\n"
        "Або натисни <b>🔍 Новий пошук</b> і надішли індикатор."
    )
    await answer_or_edit(message, text)

@dp.message(Command("profile"))
async def profile_cmd(message: Message) -> None:
    user = get_user(message.from_user.id)
    if not user:
        upsert_user(
            message.from_user.id,
            message.from_user.username,
            message.from_user.first_name,
            message.from_user.last_name,
        )
        user = get_user(message.from_user.id)

    daily_used = get_daily_usage(message.from_user.id)
    daily_limit = get_daily_limit(message.from_user.id)
    total = get_total_searches(message.from_user.id)

    text = (
        "<b>👤 Профіль</b>\n\n"
        f"<b>ID:</b> <code>{message.from_user.id}</code>\n"
        f"<b>Username:</b> @{escape(message.from_user.username or 'none')}\n"
        f"<b>Тариф:</b> {escape(user['tariff'] if user else 'FREE')}\n"
        f"<b>Пошуків сьогодні:</b> {daily_used}/{daily_limit}\n"
        f"<b>Всього пошуків:</b> {total}\n"
        f"<b>Оновлено:</b> {escape(user['updated_at'] if user else now_ts())}\n"
    )
    await answer_or_edit(message, text)

@dp.message(Command("history"))
async def history_cmd(message: Message) -> None:
    rows = get_recent_searches(message.from_user.id, 10)
    if not rows:
        await answer_or_edit(message, "<b>📂 Історія порожня</b>")
        return

    parts = ["<b>📂 Останні результати</b>\n"]
    for row in rows:
        parts.append(f"• <code>{escape(row['normalized_query'])}</code> [{escape(row['query_type'])}] — {escape(row['created_at'])}")
    await answer_or_edit(message, "\n".join(parts))

@dp.message(F.text == "📂 Історія")
async def history_button(message: Message) -> None:
    await history_cmd(message)

@dp.message(F.text == "👤 Профіль")
async def profile_button(message: Message) -> None:
    await profile_cmd(message)

@dp.message(F.text == "💎 VIP / Тарифи")
async def vip_button(message: Message) -> None:
    text = (
        "<b>💎 VIP / Тарифи</b>\n\n"
        f"FREE — {FREE_DAILY_LIMIT}/день\n"
        f"INTEL — {INTEL_DAILY_LIMIT}/день\n"
        f"AGENCY — {AGENCY_DAILY_LIMIT}/день\n"
        f"WARROOM — {WARROOM_DAILY_LIMIT}/день\n\n"
        "Зараз активний локальний контроль лімітів через SQLite."
    )
    await answer_or_edit(message, text)
@dp.message(F.text == "⚙️ Налаштування")
async def settings_button(message: Message) -> None:
    text = (
        "<b>⚙️ Налаштування</b>\n\n"
        f"LOG_LEVEL: {escape(LOG_LEVEL)}\n"
        f"DB: <code>{escape(str(DB_PATH))}</code>\n"
        f"VT: {'enabled' if VT_API_KEY else 'disabled'}\n"
        f"Shodan: {'enabled' if SHODAN_API_KEY else 'disabled'}\n"
        f"Censys: {'enabled' if CENSYS_BEARER_TOKEN else 'disabled'}\n"
        f"Venice: {'enabled' if VENICE_API_KEY else 'disabled'}\n"
    )
    await answer_or_edit(message, text)

@dp.message(F.text == "🆘 Підтримка")
async def support_button(message: Message) -> None:
    text = (
        "<b>🆘 Підтримка</b>\n\n"
        "Опиши проблему одним повідомленням.\n"
        "У цьому білді логіка підтримки локальна.\n"
        "Для продакшну підключай пересилання адміну / окремий inbox."
    )
    await answer_or_edit(message, text)

@dp.message(F.text == "🔍 Новий пошук")
async def new_search_button(message: Message) -> None:
    await answer_or_edit(
        message,
        "<b>🔍 Новий пошук</b>\n\n"
        "Надішли індикатор одним повідомленням:\n"
        "• 8.8.8.8\n"
        "• example.com\n"
        "• https://example.com/test\n"
        "• admin@example.com\n"
        "• @nickname\n"
        "• d41d8cd98f00b204e9800998ecf8427e\n"
        "• Ім'я Прізвище / Назва компанії"
    )

@dp.message(F.text)
async def text_handler(message: Message) -> None:
    text = (message.text or "").strip()
    if not text:
        await answer_or_edit(message, "Порожній запит.")
        return

    upsert_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.first_name,
        message.from_user.last_name,
    )

    used = get_daily_usage(message.from_user.id)
    limit_value = get_daily_limit(message.from_user.id)
    if used >= limit_value:
        await answer_or_edit(
            message,
            f"<b>⛔ Денний ліміт вичерпано</b>\n\nВикористано {used}/{limit_value}.\nЗміни тариф або чекай наступної доби."
        )
        return

    qtype, normalized = detect_type(text)
    progress = await message.answer(f"⏳ Аналізую <code>{escape(normalized)}</code> [{escape(qtype)}] ...", reply_markup=keyboard())

    try:
        result = await run_search(text)
        increment_daily_usage(message.from_user.id)
        save_search(message.from_user.id, qtype, text, normalized, result)
        await progress.edit_text(render_result(result), reply_markup=keyboard())
    except Exception as e:
        logger.exception("search failed")
        await progress.edit_text(
            "<b>❌ Помилка обробки</b>\n\n"
            f"<code>{escape(str(e))}</code>",
            reply_markup=keyboard(),
        )

async def startup(bot: Bot) -> None:
    init_db()
    me = await bot.get_me()
    logger.info("Starting bot...")
    logger.info("Authorized as @%s (%s)", me.username, me.id)

async def main() -> None:
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await startup(bot)
    await dp.start_polling(bot)

if name == "main":
    asyncio.run(main())
