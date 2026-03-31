import os
import asyncio
import html
import logging
from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import Message

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN not set")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

async def run_maigret(username: str) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            "maigret",
            username,
            "--top-sites", "50",
            "--timeout", "10",
            "--no-color",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return "❌ Помилка: maigret завис більше 15 секунд"

        out = stdout.decode(errors="ignore")
        err = stderr.decode(errors="ignore").strip()

        links = []
        for line in out.splitlines():
            line = line.strip()
            if "http://" in line or "https://" in line:
                links.append(line)

        unique_links = []
        seen = set()
        for x in links:
            if x not in seen:
                seen.add(x)
                unique_links.append(x)

        if unique_links:
            body = "\n".join(f"{i}. {x}" for i, x in enumerate(unique_links[:20], 1))
            return f"🔎 РЕЗУЛЬТАТ:\n\n{body}"

        if err:
            return f"❌ maigret stderr:\n{err[:3500]}"

        return "❌ Нічого не знайдено"
    except Exception as e:
        return f"❌ Помилка запуску: {html.escape(str(e))}"

@dp.message(CommandStart())
async def start_cmd(message: Message):
    await message.answer("NEXARA ONLINE\nВведи нікнейм для пошуку")

@dp.message()
async def search_cmd(message: Message):
    query = (message.text or "").strip()
    if not query:
        return
    wait = await message.answer(f"🔎 Пошук: {query}")
    result = await run_maigret(query)
    await wait.edit_text(result[:4000])

async def main():
    me = await bot.get_me()
    logging.info("Authorized as @%s", me.username)
    await dp.start_polling(bot)

if name == "main":
    asyncio.run(main())
