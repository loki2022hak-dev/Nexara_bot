import asyncio
from aiogram import Bot, Dispatcher
from aiogram.types import Message
import os

TOKEN = os.getenv("BOT_TOKEN")

bot = Bot(token=TOKEN)
dp = Dispatcher()

@dp.message()
async def echo(message: Message):
    await message.answer(message.text)

async def main():
    await dp.start_polling(bot)

if name == "main":
    asyncio.run(main())
