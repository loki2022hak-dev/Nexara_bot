import asyncio
import logging
import os
from aiogram import Bot, Dispatcher
from aiogram.types import Message

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

@dp.message()
async def echo(message: Message):
    await message.answer(f"✅ Nexara online\n\n{message.text or ''}")

async def main():
    if not BOT_TOKEN:
        logging.error("BOT_TOKEN IS MISSING! Use: fly secrets set BOT_TOKEN='your_token'")
        return
    logging.info("Starting bot...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Bot stopped")
