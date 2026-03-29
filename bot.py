import os
import asyncio
import logging
from aiogram import Bot, Dispatcher, types

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN not set")

dp = Dispatcher()

@dp.message()
async def echo(message: types.Message):
    await message.answer("✅ Nexara працює")

async def main():
    bot = Bot(token=BOT_TOKEN)
    await dp.start_polling(bot)

if name == "main":
    asyncio.run(main())
