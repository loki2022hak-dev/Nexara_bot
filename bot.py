import asyncio
from datetime import datetime, timedelta
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, FSInputFile, ReplyKeyboardMarkup, KeyboardButton
from sqlalchemy import select, func

from config import settings
from db import engine, SessionLocal, Base
from models import QueryRun
from pipeline import run_pipeline, check_health

dp = Dispatcher()
TASK_QUEUE: asyncio.Queue[tuple[int, int, str]] = asyncio.Queue()
RUNNING_USERS: set[int] = set()

MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🔍 Новий пошук"), KeyboardButton(text="📄 Останній PDF")],
        [KeyboardButton(text="📜 Останній TXT"), KeyboardButton(text="📊 Мої результати")],
        [KeyboardButton(text="🧪 Health Check")],
    ],
    resize_keyboard=True
)

PLANS = {
    "FREE": {"price": "$0", "daily_limit": settings.free_daily_limit},
    "INTEL": {"price": "$9", "daily_limit": settings.intel_daily_limit},
    "AGENCY": {"price": "$29", "daily_limit": settings.agency_daily_limit},
    "WARROOM": {"price": "$79", "daily_limit": settings.warroom_daily_limit},
}

def plan_text() -> str:
    lines = ["💎 <b>NEXARA PLANS</b>", ""]
    for code, data in PLANS.items():
        lines.append(f"<b>{code}</b> — {data['price']} — {data['daily_limit']} / day")
    return "\n".join(lines)

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

async def get_last_run(user_id: int):
    async with SessionLocal() as session:
        return await session.scalar(select(QueryRun).where(QueryRun.user_id == user_id).order_by(QueryRun.id.desc()).limit(1))

async def worker(bot: Bot):
    while True:
        chat_id, user_id, raw_query = await TASK_QUEUE.get()
        try:
            plan_code = "FREE"
            daily_limit = PLANS[plan_code]["daily_limit"]
            async with SessionLocal() as session:
                qr = await run_pipeline(session, user_id=user_id, raw_query=raw_query, plan_code=plan_code, daily_limit=daily_limit)
            await bot.send_message(
                chat_id,
                f"✅ <b>Done</b>\n\n"
                f"Query: <code>{qr.raw_query}</code>\n"
                f"Type: <b>{qr.entity_type}</b>\n"
                f"Summary: {qr.summary_text}\n"
                f"Confidence: <b>{qr.confidence_score}</b>\n"
                f"Risk: <b>{qr.risk_score}</b>",
                reply_markup=MAIN_KB,
            )
            if qr.pdf_path and Path(qr.pdf_path).exists():
                await bot.send_document(chat_id, FSInputFile(qr.pdf_path, filename=Path(qr.pdf_path).name), caption="📄 Dossier PDF")
            if qr.txt_path and Path(qr.txt_path).exists():
                await bot.send_document(chat_id, FSInputFile(qr.txt_path, filename=Path(qr.txt_path).name), caption="📜 TXT Report")
        except Exception as e:
            await bot.send_message(chat_id, f"❌ <code>{type(e).__name__}: {e}</code>", reply_markup=MAIN_KB)
        finally:
            RUNNING_USERS.discard(user_id)
            TASK_QUEUE.task_done()

async def watchdog(bot: Bot):
    while True:
        await asyncio.sleep(300)
        async with SessionLocal() as session:
            recent = await session.scalar(select(func.count(QueryRun.id)).where(QueryRun.created_at >= datetime.utcnow() - timedelta(hours=1)))
        # keep loop alive, can be extended with notifications
        _ = recent

@dp.message(CommandStart())
async def start(m: Message):
    await m.answer(
        "🔻 <b>NEXARA STABILIZED CORE</b>\n\n"
        "Formats:\n"
        "• <code>username</code>\n"
        "• <code>ip:1.1.1.1</code>\n"
        "• <code>domain:example.com</code>\n"
        "• <code>hash:SHA256</code>\n\n"
        + plan_text(),
        reply_markup=MAIN_KB,
    )

@dp.message(Command("check"))
async def check_cmd(m: Message):
    args = (m.text or "").split(maxsplit=1)
    if len(args) < 2:
        await m.answer("Usage: /check <query>", reply_markup=MAIN_KB)
        return
    raw_query = args[1].strip()
    if m.from_user.id in RUNNING_USERS:
        await m.answer("⏳ Query already running for your account.", reply_markup=MAIN_KB)
        return
    RUNNING_USERS.add(m.from_user.id)
    await TASK_QUEUE.put((m.chat.id, m.from_user.id, raw_query))
    await m.answer(f"🚀 Queued: <code>{raw_query}</code>", reply_markup=MAIN_KB)

@dp.message(Command("export"))
async def export_cmd(m: Message):
    last = await get_last_run(m.from_user.id)
    if not last:
        await m.answer("No results yet.", reply_markup=MAIN_KB)
        return
    if last.pdf_path and Path(last.pdf_path).exists():
        await m.answer_document(FSInputFile(last.pdf_path, filename=Path(last.pdf_path).name), caption="📄 Latest PDF")
    if last.txt_path and Path(last.txt_path).exists():
        await m.answer_document(FSInputFile(last.txt_path, filename=Path(last.txt_path).name), caption="📜 Latest TXT")

@dp.message(F.text == "🔍 Новий пошук")
async def new_search(m: Message):
    await m.answer("Надішли /check <query>", reply_markup=MAIN_KB)

@dp.message(F.text == "📄 Останній PDF")
async def last_pdf(m: Message):
    last = await get_last_run(m.from_user.id)
    if not last or not last.pdf_path:
        await m.answer("PDF not found.", reply_markup=MAIN_KB)
        return
    p = Path(last.pdf_path)
    if not p.exists():
        await m.answer("PDF file missing on disk.", reply_markup=MAIN_KB)
        return
    await m.answer_document(FSInputFile(str(p), filename=p.name), caption="📄 Latest PDF")

@dp.message(F.text == "📜 Останній TXT")
async def last_txt(m: Message):
    last = await get_last_run(m.from_user.id)
    if not last or not last.txt_path:
        await m.answer("TXT not found.", reply_markup=MAIN_KB)
        return
    p = Path(last.txt_path)
    if not p.exists():
        await m.answer("TXT file missing on disk.", reply_markup=MAIN_KB)
        return
    await m.answer_document(FSInputFile(str(p), filename=p.name), caption="📜 Latest TXT")

@dp.message(F.text == "📊 Мої результати")
async def my_results(m: Message):
    last = await get_last_run(m.from_user.id)
    if not last:
        await m.answer("No results yet.", reply_markup=MAIN_KB)
        return
    await m.answer(
        f"ID: <code>{last.id}</code>\n"
        f"Query: <code>{last.raw_query}</code>\n"
        f"Status: <b>{last.status}</b>\n"
        f"Summary: {last.summary_text or '-'}\n"
        f"Created: {last.created_at}",
        reply_markup=MAIN_KB,
    )

@dp.message(F.text == "🧪 Health Check")
async def health_check(m: Message):
    try:
        parts = (m.text or "").split(maxsplit=1)
        sample = "ip:8.8.8.8"
        health = await check_health("ip", "8.8.8.8")
        lines = ["🧪 <b>Health Check</b>", ""]
        for name, item in health.items():
            lines.append(f"{name}: <b>{'OK' if item['ok'] else 'FAIL'}</b> {item.get('error') or ''}")
        await m.answer("\n".join(lines), reply_markup=MAIN_KB)
    except Exception as e:
        await m.answer(f"❌ {type(e).__name__}: {e}", reply_markup=MAIN_KB)

async def main():
    await init_db()
    bot = Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    asyncio.create_task(worker(bot))
    asyncio.create_task(watchdog(bot))
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
