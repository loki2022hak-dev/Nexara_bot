import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.types import Message

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN not set")

dp = Dispatcher()

@dp.message()
async def echo(message: Message):
    text = message.text or ""
    await message.answer(f"✅ Nexara online\n\n{text}")

async def main():
    bot = Bot(token=BOT_TOKEN)
    await dp.start_polling(bot)

if name == "main":
    asyncio.run(main())
